"""tar.gz extraction for pack archives: install-from-upload's only path onto disk.

Archive bytes are third-party input from an HTTP upload (or, later, a
marketplace download) — the same trust level `api/documents.py` gives an
uploaded file. `extract_pack_archive` treats every byte and every tar header
field as hostile: a member name can smuggle a path that escapes the
destination (zip-slip), a member can be a symlink/hardlink/device node
pointing anywhere reachable from the process, and a header can declare
gigabytes of content behind a few compressed kilobytes (a decompression
bomb). Every one of these is a hard rejection, not a warning — see
safety.py's own docstring for the same posture on method code: install a pack
like you would deploy code you reviewed, and let this module be the reason a
hostile *archive* is not also a way in before that review ever happens.

What this checks and does not check: it is purely a mechanical filter over
tar structure and sizes. It says nothing about whether the *content* it
extracts is a safe pack — that is `packs/loader.py::validate_pack` (schema,
files-exist, doctrine selectors) and `packs/safety.py` (the method AST scan),
both of which `install_pack_from_archive` runs unchanged against whatever
lands here.
"""
from __future__ import annotations

import io
import tarfile
from pathlib import Path, PurePosixPath
from typing import NoReturn

# The archive itself, compressed, before a single byte is decompressed —
# checked before `tarfile.open` is even called. Mirrors api/documents.py's
# MAX_UPLOAD_BYTES posture: refuse an oversized transfer before doing any work
# with it. Packs are markdown, YAML, a few small CSVs and short Python
# scripts — the real packs/climate-risk fixture is ~70KB; 10MB compressed is
# already generous headroom, not a tight budget.
MAX_COMPRESSED_BYTES = 10 * 1024 * 1024

# Caps on what a member is allowed to *decompress to*, checked against the tar
# header's declared size before a single byte of that member is extracted.
# This is what stops a bomb: gzip can turn a few KB of highly-compressible
# input into gigabytes of output, but only if the declared size is allowed to
# ask for that much — checking the header first means the bomb is never
# actually detonated.
MAX_MEMBER_DECOMPRESSED_BYTES = 25 * 1024 * 1024
MAX_TOTAL_DECOMPRESSED_BYTES = 50 * 1024 * 1024

# A real pack is a handful of doctrine files, schemas, datasets, and methods.
# A five-figure member count is not a pack; it is an attempt to exhaust
# inodes or memory on the walk itself, independent of any one member's size.
MAX_MEMBER_COUNT = 2000

_COPY_CHUNK_BYTES = 256 * 1024


class PackArchiveError(Exception):
    """Any hostile or malformed archive. The message is safe to show a caller —
    it never echoes archive bytes, only structural facts (a name, a size, a
    count) about the member that tripped the check."""


def _reject(message: str) -> NoReturn:
    raise PackArchiveError(message)


def _check_member_type(member: tarfile.TarInfo) -> None:
    """Only regular files and directories may exist in a pack archive."""
    if member.isdir() or member.isreg():
        return
    if member.issym():
        kind = "a symlink"
    elif member.islnk():
        kind = "a hard link"
    elif member.ischr():
        kind = "a character device"
    elif member.isblk():
        kind = "a block device"
    elif member.isfifo():
        kind = "a FIFO"
    else:
        kind = "a special file"
    _reject(f"archive member '{member.name}' is {kind}, which pack archives may not contain")


def _safe_member_path(dest: Path, dest_resolved: Path, member: tarfile.TarInfo) -> Path:
    """The on-disk path for `member` inside `dest`, or a PackArchiveError if it
    would land outside `dest` (zip-slip). Checked twice: once on the name
    string itself (absolute paths, drive letters, NUL bytes, `..` segments —
    catches the obvious cases without touching the filesystem) and once on the
    resolved path (belt and braces over any one string check missing a case,
    e.g. a member name using an OS-specific alias for `dest`'s own ancestry)."""
    name = member.name
    # Belt-and-braces: `tarfile` already truncates a member name at its first
    # NUL byte while parsing the header, so `"\x00" in name` should never be
    # true by the time it reaches here. Checked anyway in case that parsing
    # behavior ever changes underneath this — cheap insurance, not a check
    # this code depends on tripping today.
    if not name or "\x00" in name:
        _reject(f"archive member has an invalid name: {name!r}")
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or (len(normalized) > 1 and normalized[1] == ":"):
        _reject(f"archive member has an absolute or drive-rooted path: {name!r}")
    posix = PurePosixPath(normalized)
    if posix.is_absolute() or ".." in posix.parts:
        _reject(f"archive member path escapes the pack directory: {name!r}")
    target = (dest / posix).resolve()
    if target != dest_resolved and dest_resolved not in target.parents:
        _reject(f"archive member resolves outside the pack directory: {name!r}")
    return target


def extract_pack_archive(archive_bytes: bytes, dest: Path) -> None:
    """Extract a tar.gz pack archive into `dest`.

    `dest` is created (with parents) if it does not exist. Raises
    `PackArchiveError` for anything hostile or malformed; on any failure,
    `dest` may contain a partial extraction — cleanup is the caller's job (the
    loader rmtrees its staging directory on any failure; see
    `install_pack_from_archive`).
    """
    if len(archive_bytes) > MAX_COMPRESSED_BYTES:
        _reject(
            f"archive is too large ({len(archive_bytes)} bytes compressed, "
            f"{MAX_COMPRESSED_BYTES} max)"
        )

    dest.mkdir(parents=True, exist_ok=True)
    dest_resolved = dest.resolve()

    # Tracked so the except block below can name which member (a relative,
    # already-untrusted-but-safe-to-echo name) was being processed when an
    # OSError hit, without falling back to `str(e)` — an OSError's own message
    # usually embeds the absolute path it was operating on (`target`, under
    # `dest`), which is exactly the server-filesystem detail a 4xx body must
    # never leak.
    current_member_name: str | None = None

    try:
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as tar:
            member_count = 0
            total_decompressed = 0
            for member in tar:
                current_member_name = member.name
                member_count += 1
                if member_count > MAX_MEMBER_COUNT:
                    _reject(f"archive has too many members (> {MAX_MEMBER_COUNT})")

                _check_member_type(member)
                target = _safe_member_path(dest, dest_resolved, member)

                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue

                if member.size > MAX_MEMBER_DECOMPRESSED_BYTES:
                    _reject(
                        f"archive member '{member.name}' declares {member.size} decompressed "
                        f"bytes (> {MAX_MEMBER_DECOMPRESSED_BYTES} max)"
                    )
                total_decompressed += member.size
                if total_decompressed > MAX_TOTAL_DECOMPRESSED_BYTES:
                    _reject(
                        "archive's total decompressed size exceeds "
                        f"{MAX_TOTAL_DECOMPRESSED_BYTES} bytes"
                    )

                source = tar.extractfile(member)
                if source is None:
                    _reject(f"archive member '{member.name}' has no extractable content")

                target.parent.mkdir(parents=True, exist_ok=True)
                written = 0
                with target.open("wb") as sink:
                    while chunk := source.read(_COPY_CHUNK_BYTES):
                        written += len(chunk)
                        # Defense in depth over the header check above: a
                        # conforming tarfile read never returns more than
                        # member.size bytes for this member, but nothing here
                        # relies on that being airtight.
                        if written > member.size:
                            _reject(
                                f"archive member '{member.name}' produced more data than its "
                                "declared size — malformed or hostile archive"
                            )
                        sink.write(chunk)
    except PackArchiveError:
        raise
    except (tarfile.TarError, OSError, EOFError) as e:
        # Never `str(e)`: an OSError's message normally embeds the absolute
        # path it failed on (`target`, built from `dest` — a server-side
        # staging directory), which a 4xx body must not echo back to the
        # caller. `e.strerror` is the OS-provided reason with no path in it
        # ("Is a directory", "No space left on device", ...); TarError/EOFError
        # have no `strerror`, so those fall back to just their exception name.
        reason = getattr(e, "strerror", None) or type(e).__name__
        member_note = f" (member {current_member_name!r})" if current_member_name else ""
        _reject(f"not a valid tar.gz pack archive{member_note}: {reason}")
