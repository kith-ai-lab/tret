"""Document upload, listing, and the dataset read endpoints.

Uploads are third-party bytes with a third-party filename, so this module treats
both as hostile: the size cap is enforced *while* the body is consumed rather
than after it is in memory, the stored name is derived from the client's only by
sanitisation, and text extraction is bounded in output and in wall time.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import uuid
from pathlib import Path, PurePosixPath

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bench.api.auth import current_user
from bench.config import get_settings
from bench.db.engine import get_db
from bench.db.models import Dataset, DatasetRow, Document, Project, User
from bench.services.documents import extract_text

log = logging.getLogger("bench.documents")

router = APIRouter(prefix="/api", tags=["documents"])

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
# The body is consumed in chunks this size, so an oversized upload is refused
# after one chunk over the cap rather than after the whole thing is a single
# bytes object in the process.
UPLOAD_CHUNK_BYTES = 1024 * 1024
# A declared body larger than the cap is refused before any hashing or disk
# write. The allowance covers the multipart envelope (boundaries and part
# headers) so a file *at* the cap is not refused for its wrapper.
MULTIPART_OVERHEAD_ALLOWANCE = 64 * 1024
# Extraction bounds. A 25MB CSV of one-character rows, or a PDF crafted to
# expand, must not turn into an unbounded string in a JSONB column or an
# unbounded stretch of CPU inside a request.
MAX_EXTRACTED_CHARS = 2_000_000
EXTRACTION_TIMEOUT_SECONDS = 30.0

DEFAULT_CONTENT_TYPE = "application/octet-stream"
# Deliberately narrow: word characters in any script (so a German or Japanese
# filename survives intact) plus a short list of punctuation. Path separators,
# quotes, shell metacharacters, control characters, NUL and the bidi overrides
# are all outside it, because none of them belong in a name bench writes to a
# filesystem and renders back to a reviewer.
_UNSAFE_FILENAME_CHARS = re.compile(r"[^\w.() \[\]+&,-]")
_MEDIA_TYPE = re.compile(r"[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,63}")


def safe_filename(raw: str | None) -> str:
    """A filename that can only ever name a file *inside* the storage directory.

    The client controls this string. `../../etc/cron.d/x`, `C:\\evil`, a name with
    a NUL, an all-dots name and a 4KB name are all things a multipart client can
    send, so the basename is taken first and then everything outside a small
    allowlist is replaced. Leading dots go too: a stored name should not be
    hidden, and `.`/`..` must not survive as a name at all.
    """
    name = PurePosixPath((raw or "").replace("\\", "/")).name
    name = _UNSAFE_FILENAME_CHARS.sub("_", name.replace("\x00", ""))
    name = name.strip(". ")
    if not name:
        return "upload"
    stem, dot, extension = name.rpartition(".")
    if dot and 0 < len(extension) <= 12:
        # Keep the extension: extraction dispatches on it.
        return f"{stem[:100]}.{extension}"
    return name[:120]


def safe_content_type(raw: str | None) -> str:
    """The client's declared type, or the generic one if it is not a media type.

    Never trusted for dispatch (extraction goes by extension) — but it is stored
    and rendered, so it does not get to carry newlines, markup or 4KB of prose.
    """
    candidate = (raw or "").split(";", 1)[0].strip().lower()
    return candidate if _MEDIA_TYPE.fullmatch(candidate) else DEFAULT_CONTENT_TYPE


def _doc_out(d: Document) -> dict:
    return {
        "id": str(d.id),
        "project_id": str(d.project_id),
        "filename": d.filename,
        "content_type": d.content_type,
        "byte_size": d.byte_size,
        "extraction_status": d.extraction_status,
        "meta": d.meta,
        "text_chars": len(d.extracted_text or ""),
        "created_at": d.created_at.isoformat() if d.created_at else None,
    }


def _too_large() -> HTTPException:
    return HTTPException(413, f"File too large ({MAX_UPLOAD_BYTES // (1024 * 1024)}MB max)")


async def _spool_upload(file: UploadFile, storage_dir: Path) -> tuple[Path, str, int]:
    """Stream the upload to a temp file under `storage_dir`, hashing as it goes.

    Returns (temp path, sha256 hex, byte count) and enforces the cap mid-stream:
    the process never holds more than one chunk of the body, and an oversized
    upload leaves nothing behind. Callers must move or unlink the temp file.
    """
    digest = hashlib.sha256()
    total = 0
    temp_path = storage_dir / f".incoming-{uuid.uuid4().hex}.part"
    try:
        with temp_path.open("wb") as sink:
            while chunk := await file.read(UPLOAD_CHUNK_BYTES):
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    raise _too_large()
                digest.update(chunk)
                sink.write(chunk)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
    return temp_path, digest.hexdigest(), total


async def _extract_bounded(filename: str, data: bytes) -> tuple[str, dict, str]:
    """(text, meta, status). Bounded in wall time and in characters returned.

    Extraction is third-party parsing of third-party bytes: it runs off the event
    loop so one slow document cannot stall every other request, it is abandoned
    after EXTRACTION_TIMEOUT_SECONDS, its output is truncated, and *any* parser
    failure is recorded on the row rather than raised — a corrupt PDF is a
    document with no text, not a 500 on upload.

    "Abandoned" is precise: Python cannot cancel a running thread, so a pathological
    parser finishes in the background while the request moves on. What is bounded is
    the request, and the memory that thread holds (one upload, at most the size cap).
    """
    try:
        text, meta = await asyncio.wait_for(
            asyncio.to_thread(extract_text, filename, data), EXTRACTION_TIMEOUT_SECONDS
        )
    except (asyncio.TimeoutError, TimeoutError):
        return "", {"error": f"text extraction timed out after {EXTRACTION_TIMEOUT_SECONDS:.0f}s"}, "failed"
    except ValueError as e:  # unsupported type: the message is for the user
        return "", {"error": str(e)}, "failed"
    except Exception as e:  # a parser blowing up on hostile bytes
        log.warning("text extraction failed for %r: %s", filename, e)
        return "", {"error": f"text extraction failed: {type(e).__name__}"}, "failed"
    if len(text) > MAX_EXTRACTED_CHARS:
        meta = {**meta, "truncated": True, "extracted_chars": MAX_EXTRACTED_CHARS}
        text = text[:MAX_EXTRACTED_CHARS]
    return text, meta, "done"


@router.post("/documents")
async def upload_document(
    request: Request,
    file: UploadFile,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Store an uploaded document and extract its text.

    On the size cap: a declared `Content-Length` over the cap is refused before
    any work, and the body is otherwise consumed in chunks so the cap is hit
    mid-stream. What this cannot do is refuse the *transfer*: Starlette parses
    the multipart body (spooling parts to a temp file, not to memory) before this
    function is entered, so a client can still push bytes at the server. A hard
    body limit belongs in the reverse proxy in front of bench — see
    docs/hardening.md. What is guaranteed here is that an oversized upload is
    never materialised in memory, never hashed, and never stored.
    """
    declared = request.headers.get("content-length")
    if declared and declared.isdigit():
        if int(declared) > MAX_UPLOAD_BYTES + MULTIPART_OVERHEAD_ALLOWANCE:
            raise _too_large()

    project = (await db.execute(select(Project))).scalars().first()
    if project is None:
        raise HTTPException(500, "No project exists")

    filename = safe_filename(file.filename)
    storage_dir = Path(get_settings().storage_dir).resolve()
    storage_dir.mkdir(parents=True, exist_ok=True)
    temp_path, sha, size = await _spool_upload(file, storage_dir)
    if size == 0:
        temp_path.unlink(missing_ok=True)
        raise HTTPException(422, "The uploaded file is empty")

    storage_path = (storage_dir / f"{sha}-{filename}").resolve()
    # Belt and braces over safe_filename: if a name ever escapes it, the write
    # still refuses to land outside the storage directory.
    if storage_path.parent != storage_dir:
        temp_path.unlink(missing_ok=True)
        raise HTTPException(400, "Invalid filename")
    temp_path.replace(storage_path)

    doc = Document(
        project_id=project.id,
        filename=filename,
        content_type=safe_content_type(file.content_type),
        byte_size=size,
        storage_path=str(storage_path),
        sha256=sha,
        uploaded_by=user.id,
    )
    text, meta, status = await _extract_bounded(filename, storage_path.read_bytes())
    doc.extracted_text = text or None  # None = nothing was extracted, as before
    doc.meta = meta
    doc.extraction_status = status
    db.add(doc)
    await db.commit()
    return _doc_out(doc)


@router.get("/documents")
async def list_documents(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    docs = (
        (await db.execute(select(Document).order_by(Document.created_at.desc()).limit(200)))
        .scalars()
        .all()
    )
    return [_doc_out(d) for d in docs]


@router.get("/documents/{document_id}")
async def get_document(
    document_id: uuid.UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    d = await db.get(Document, document_id)
    if d is None:
        raise HTTPException(404, "Document not found")
    return {**_doc_out(d), "extracted_text": (d.extracted_text or "")[:100_000]}


@router.get("/datasets")
async def list_datasets(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    datasets = (await db.execute(select(Dataset).order_by(Dataset.name))).scalars().all()
    return [
        {
            "id": str(ds.id),
            "name": ds.name,
            "columns": ds.schema_json.get("columns", []),
            "row_count": ds.row_count,
            "pack_seeded": ds.pack_id is not None,
        }
        for ds in datasets
    ]


@router.get("/datasets/{dataset_id}/rows")
async def dataset_rows(
    dataset_id: uuid.UUID,
    limit: int = 20,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    rows = (
        await db.execute(
            select(DatasetRow)
            .where(DatasetRow.dataset_id == dataset_id)
            .order_by(DatasetRow.row_index)
            .limit(min(limit, 200))
        )
    ).scalars().all()
    return [r.data for r in rows]
