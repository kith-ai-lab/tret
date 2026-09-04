"""Document upload, listing, and the dataset read endpoints.

Uploads are third-party bytes with a third-party filename, so this module treats
both as hostile: the size cap is enforced *while* the body is consumed rather
than after it is in memory, the stored name is derived from the client's only by
sanitisation, and text extraction is bounded in output and in wall time.
"""
from __future__ import annotations

import hashlib
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tret.api.auth import current_user
from tret.api.workspace import WorkspaceContext, current_project, current_workspace, project_in_workspace
from tret.config import get_settings
from tret.db.engine import get_db
from tret.db.models import Dataset, DatasetRow, Document, User
from tret.services.connections import (
    GDRIVE,
    M365,
    ConnectionAuthError,
    IMPORT_MAX_BYTES,
    DownloadTooLargeError,
    download_gdrive_file,
    download_m365_file,
    gdrive_download_content_type,
    gdrive_export_filename,
    gdrive_file_metadata,
    get_access_token,
    m365_item_metadata,
)
from tret.services.documents import ingest_document

log = logging.getLogger("tret.documents")

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

DEFAULT_CONTENT_TYPE = "application/octet-stream"
# Deliberately narrow: word characters in any script (so a German or Japanese
# filename survives intact) plus a short list of punctuation. Path separators,
# quotes, shell metacharacters, control characters, NUL and the bidi overrides
# are all outside it, because none of them belong in a name tret writes to a
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
        # The trust tier, surfaced wherever documents are listed: a reviewer
        # scanning the document list should be able to see that three of these
        # were fetched off the web by an agent, not handed over by a person.
        "source_kind": d.source_kind,
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


@router.post("/documents")
async def upload_document(
    request: Request,
    file: UploadFile,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """Store an uploaded document and extract its text.

    On the size cap: a declared `Content-Length` over the cap is refused before
    any work, and the body is otherwise consumed in chunks so the cap is hit
    mid-stream. What this cannot do is refuse the *transfer*: Starlette parses
    the multipart body (spooling parts to a temp file, not to memory) before this
    function is entered, so a client can still push bytes at the server. A hard
    body limit belongs in the reverse proxy in front of tret — see
    docs/hardening.md. What is guaranteed here is that an oversized upload is
    never materialised in memory, never hashed, and never stored.
    """
    declared = request.headers.get("content-length")
    if declared and declared.isdigit():
        if int(declared) > MAX_UPLOAD_BYTES + MULTIPART_OVERHEAD_ALLOWANCE:
            raise _too_large()

    project = await current_project(db, ctx.id)
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

    doc = await ingest_document(
        project_id=project.id,
        filename=filename,
        content_type=safe_content_type(file.content_type),
        storage_path=storage_path,
        sha256=sha,
        byte_size=size,
        data=storage_path.read_bytes(),
        uploaded_by=user.id,
    )
    db.add(doc)
    await db.commit()
    return _doc_out(doc)


class ImportItem(BaseModel):
    id: str
    name: str
    drive_id: str | None = None


class ImportBody(BaseModel):
    provider: str
    # Bound the batch so one request can't queue an unbounded pile of imports.
    items: list[ImportItem] = Field(..., max_length=50)


def _import_too_large(name: str, num_bytes: int) -> ValueError:
    return ValueError(
        f"{name} is {num_bytes // (1024 * 1024)}MB, over the "
        f"{IMPORT_MAX_BYTES // (1024 * 1024)}MB import limit"
    )


async def _import_one(
    *,
    access_token: str,
    provider: str,
    item: ImportItem,
    project_id: uuid.UUID,
    uploaded_by: uuid.UUID,
    storage_dir: Path,
    imported_at: str,
) -> Document:
    """Download one picked item and ingest it exactly like an upload.

    A metadata call precedes the download for both providers: it is where the
    declared size (the "prefer a Content-Length/size check before download"
    half of the cap), the mime type (gdrive: raw vs. `files.export`), and the
    `modified_at` provenance field all come from — none of the three are in
    the request body, which only ever names *which* file was picked.
    """
    if provider == GDRIVE:
        meta = await gdrive_file_metadata(access_token, file_id=item.id)
        size = meta.get("size")
        if size is not None and int(size) > IMPORT_MAX_BYTES:
            raise _import_too_large(item.name, int(size))
        mime_type = meta.get("mimeType")
        data = await download_gdrive_file(access_token, file_id=item.id, mime_type=mime_type)
        filename = gdrive_export_filename(item.name, mime_type)
        content_type = safe_content_type(gdrive_download_content_type(mime_type))
        modified_at = meta.get("modifiedTime")
        drive_id = None
    else:
        drive_id = item.drive_id
        if not drive_id:
            raise ValueError(f"{item.name}: m365 items require a drive_id")
        meta = await m365_item_metadata(access_token, drive_id=drive_id, item_id=item.id)
        size = meta.get("size")
        if size is not None and int(size) > IMPORT_MAX_BYTES:
            raise _import_too_large(item.name, int(size))
        data = await download_m365_file(access_token, drive_id=drive_id, item_id=item.id)
        filename = item.name
        content_type = safe_content_type((meta.get("file") or {}).get("mimeType"))
        modified_at = meta.get("lastModifiedDateTime")

    filename = safe_filename(filename)
    sha = hashlib.sha256(data).hexdigest()
    storage_path = (storage_dir / f"{sha}-{filename}").resolve()
    if storage_path.parent != storage_dir:
        raise ValueError("Invalid filename")
    storage_path.write_bytes(data)

    return await ingest_document(
        project_id=project_id,
        filename=filename,
        content_type=content_type,
        storage_path=storage_path,
        sha256=sha,
        byte_size=len(data),
        data=data,
        uploaded_by=uploaded_by,
        meta_extra={
            "source": {
                "provider": provider,
                "file_id": item.id,
                "drive_id": drive_id,
                "name": item.name,
                "modified_at": modified_at,
                "imported_at": imported_at,
            }
        },
    )


@router.post("/projects/{project_id}/documents/import")
async def import_documents(
    project_id: uuid.UUID,
    body: ImportBody,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """Import files picked from a connected provider straight into a
    project's documents, without the bytes ever passing through the caller's
    browser: each item is downloaded server-side with the workspace
    connection's own access token and fed into `ingest_document` — the exact
    ingestion the upload endpoint above uses.

    One request shares one provider and one token for every item in it, so a
    connection that cannot refresh (`ConnectionAuthError`) fails the whole
    request with 409 before any item is touched. Once past that, a single
    item failing — too big, gone, an unreadable export, or a DB-level
    failure adding/committing its row (e.g. a rare sha256 collision tripping
    the unique constraint) — is recorded in `errors` and must not fail the
    rest of the batch. `db.add`/`commit` happen inside the same per-item
    guard as the download/ingest for that reason: a failure there is rolled
    back before the loop moves on, so the batch's own AsyncSession is never
    left in the "commit failed, transaction still open" state SQLAlchemy
    would otherwise carry into the next item.
    """
    if body.provider not in (GDRIVE, M365):
        raise HTTPException(404, f"unknown provider {body.provider!r}")
    project = await project_in_workspace(db, project_id, ctx.id)
    if project is None:
        raise HTTPException(404, "Project not found")

    try:
        access_token = await get_access_token(db, ctx.id, body.provider)
    except ConnectionAuthError as exc:
        raise HTTPException(
            409, f"the {body.provider} connection needs to be reconnected: {exc}"
        ) from exc

    storage_dir = Path(get_settings().storage_dir).resolve()
    storage_dir.mkdir(parents=True, exist_ok=True)
    imported_at = datetime.now(timezone.utc).isoformat()

    # Captured as plain values, not read off `project`/`user` inside the loop
    # below: `db.rollback()` (a per-item DB failure) expires every object
    # still attached to the session, and re-reading an expired ORM attribute
    # from inside an async endpoint outside of an awaited refresh blows up
    # with SQLAlchemy's `MissingGreenlet` rather than transparently
    # reloading it. `project_id` is the path parameter itself — the same
    # value `project.id` already equals, since `project_in_workspace` only
    # returned a row at all if its id matched it.
    uploaded_by = user.id

    documents = []
    errors = []
    for item in body.items:
        try:
            doc = await _import_one(
                access_token=access_token,
                provider=body.provider,
                item=item,
                project_id=project_id,
                uploaded_by=uploaded_by,
                storage_dir=storage_dir,
                imported_at=imported_at,
            )
            db.add(doc)
            await db.commit()
        except (ValueError, DownloadTooLargeError, RuntimeError, IntegrityError) as exc:
            log.warning("document import failed for %s %r: %s", body.provider, item.id, exc)
            await db.rollback()
            errors.append({"id": item.id, "name": item.name, "detail": str(exc)})
            continue
        documents.append(_doc_out(doc))
    return {"documents": documents, "errors": errors}


@router.get("/documents")
async def list_documents(
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    project = await current_project(db, ctx.id)
    if project is None:
        return []
    docs = (
        (
            await db.execute(
                select(Document)
                .where(Document.project_id == project.id)
                .order_by(Document.created_at.desc())
                .limit(200)
            )
        )
        .scalars()
        .all()
    )
    return [_doc_out(d) for d in docs]


@router.get("/documents/{document_id}")
async def get_document(
    document_id: uuid.UUID,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    d = await db.get(Document, document_id)
    if d is None or await project_in_workspace(db, d.project_id, ctx.id) is None:
        raise HTTPException(404, "Document not found")
    return {**_doc_out(d), "extracted_text": (d.extracted_text or "")[:100_000]}


@router.get("/datasets")
async def list_datasets(
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    project = await current_project(db, ctx.id)
    if project is None:
        return []
    datasets = (
        await db.execute(
            select(Dataset).where(Dataset.project_id == project.id).order_by(Dataset.name)
        )
    ).scalars().all()
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
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    ds = await db.get(Dataset, dataset_id)
    if ds is None or await project_in_workspace(db, ds.project_id, ctx.id) is None:
        raise HTTPException(404, "Dataset not found")
    rows = (
        await db.execute(
            select(DatasetRow)
            .where(DatasetRow.dataset_id == dataset_id)
            .order_by(DatasetRow.row_index)
            .limit(min(limit, 200))
        )
    ).scalars().all()
    return [r.data for r in rows]
