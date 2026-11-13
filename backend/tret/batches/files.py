"""Serving the files of a batch: one file's bytes, a stored record file, and the whole
batch as a streamed zip. Shared by the owner routes (`tret/api/batches.py`) and the
routes of a workspace a batch is shared with (`tret/api/batch_shares.py`), so the two
cannot drift apart on content types, caps or headers."""
from __future__ import annotations

import hashlib
import json
import zipfile
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import HTTPException, Response

from tret.storage import document_bytes

# Text-like written files over this size are download-only (the web viewer renders them inline).
INLINE_TEXT_MAX_BYTES = 5 * 1024 * 1024

FILE_TYPES = {
    ".pdf": "application/pdf",
    ".md": "text/markdown; charset=utf-8",
    ".json": "application/json",
    ".txt": "text/plain; charset=utf-8",
}


async def serve_file(
    doc, path: str, size: int | None, *, download: bool, inline_max: int = INLINE_TEXT_MAX_BYTES
) -> Response:
    """The bytes of one written file. `path` is the file's recorded filename; the content
    type comes from a fixed allowlist by extension, never from the stored row."""
    name = path.rsplit("/", 1)[-1].replace('"', "").replace("\r", "").replace("\n", "")
    ext = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
    media_type = FILE_TYPES.get(ext, "application/octet-stream")
    capped = not download and media_type != "application/pdf"

    def too_big(n: int) -> HTTPException:
        return HTTPException(
            413, f"{name} is {n / 1048576:.1f} MB, over the {inline_max // 1048576} MB inline view limit; download it instead."
        )

    if capped and isinstance(size, int) and size > inline_max:
        raise too_big(size)
    # No M365 fallback: written_files keep only web_url, not the drive/item id.
    try:
        data = await document_bytes(doc)
    except FileNotFoundError as e:
        raise HTTPException(404, "The file is no longer in storage") from e
    if capped and len(data) > inline_max:
        raise too_big(len(data))
    disposition = "attachment" if download else "inline"
    ascii_name = name.encode("ascii", "replace").decode("ascii")  # header values are latin-1
    return Response(
        content=data,
        media_type=media_type,
        headers={
            "Content-Disposition": f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name, safe='')}",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )


async def serve_record(doc) -> Response:
    """A finalized record file kept as a Document."""
    try:
        data = await document_bytes(doc)
    except FileNotFoundError as e:
        raise HTTPException(404, "The record file is no longer in storage") from e
    return Response(
        content=data,
        media_type=doc.content_type,
        headers={"Content-Disposition": f'attachment; filename="{doc.filename}"'},
    )


# ── the zip ──────────────────────────────────────────────────────────────────
@dataclass
class ArchiveFile:
    """One file for the archive. Plain data (no ORM objects): the generator runs after
    the request's database session may be gone."""

    path: str  # inside the zip
    storage_path: str
    storage_key: str | None
    target_slug: str | None = None


class _Sink:
    """A write-only buffer the ZipFile writes into; `take()` hands back what it has
    accumulated so each file's bytes leave memory as soon as they are yielded."""

    def __init__(self) -> None:
        self._chunks: list[bytes] = []
        self._pos = 0

    def write(self, data) -> int:
        b = bytes(data)
        self._chunks.append(b)
        self._pos += len(b)
        return len(b)

    def tell(self) -> int:
        return self._pos

    def flush(self) -> None:
        pass

    def take(self) -> bytes:
        out = b"".join(self._chunks)
        self._chunks = []
        return out


@dataclass
class _Ref:
    storage_path: str
    storage_key: str | None


def safe_zip_name(name: str) -> str:
    """A zip entry name that cannot escape the extraction directory: no leading slash,
    no `.`/`..`/empty parts, no backslashes."""
    parts = [p.strip() for p in str(name).replace("\\", "/").split("/")]
    parts = [p for p in parts if p not in ("", ".", "..")]
    return "/".join(parts) or "file"


def unique_zip_names(entries: list[tuple[str, str]]) -> list[str]:
    """`entries` are (folder, filename); the result is `folder/filename` for each, with
    ` (2)`, ` (3)` ... added to a name already taken."""
    taken: set[str] = set()
    out: list[str] = []
    for folder, filename in entries:
        base = safe_zip_name(f"{folder}/{filename}" if folder else filename)
        candidate = base
        n = 1
        while candidate.casefold() in taken:
            n += 1
            stem, dot, ext = base.rpartition(".")
            candidate = f"{stem} ({n}).{ext}" if dot and "/" not in ext else f"{base} ({n})"
        taken.add(candidate.casefold())
        out.append(candidate)
    return out


async def stream_zip(batch_info: dict, files: list[ArchiveFile]) -> AsyncIterator[bytes]:
    """The archive as it is built: STORED entries (the files are PDFs and text, which
    deflate poorly or are small), one file's bytes in memory at a time. `manifest.json`
    comes last and lists every file written and every file missing from storage."""
    sink = _Sink()
    zf = zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_STORED)  # type: ignore[arg-type]
    listed: list[dict] = []
    missing: list[dict] = []
    now = datetime.now(timezone.utc)
    stamp = (max(now.year, 1980), now.month, now.day, now.hour, now.minute, now.second)
    try:
        for f in files:
            try:
                data = await document_bytes(_Ref(f.storage_path, f.storage_key))
            except Exception:  # noqa: BLE001 - a storage error must not cut the stream short
                missing.append({"path": f.path, "target_slug": f.target_slug})
                continue
            info = zipfile.ZipInfo(f.path, date_time=stamp)
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = 0o600 << 16
            zf.writestr(info, data)
            listed.append(
                {
                    "path": f.path,
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "byte_size": len(data),
                    "target_slug": f.target_slug,
                }
            )
            del data
            chunk = sink.take()
            if chunk:
                yield chunk
        manifest = {
            "batch": batch_info,
            "generated_at": now.isoformat(),
            "files": listed,
            "missing": missing,
        }
        info = zipfile.ZipInfo("manifest.json", date_time=stamp)
        info.compress_type = zipfile.ZIP_STORED
        info.external_attr = 0o600 << 16
        zf.writestr(info, json.dumps(manifest, indent=2).encode())
    finally:
        zf.close()
    chunk = sink.take()
    if chunk:
        yield chunk
