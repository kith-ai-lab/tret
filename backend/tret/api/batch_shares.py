"""Sharing a batch, read-only, with another workspace (`tret/batches/sharing.py`).

Owner routes live under `/api/batches/{id}/shares` and use the ordinary workspace
access check (the batch must be the caller's own). Recipient routes live under
`/api/batch-shares` (the invitation) and `/api/shared-batches` (the shared batch) and
never go through the owner's `/api/batches*` routes, which keep answering 404 to a
workspace that does not own the batch. Reads are open to any member; creating,
revoking, accepting, removing and copying need the approver role.

Not served under the deprecated `/api/collections` alias.
"""
from __future__ import annotations

import re
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from tret.api.auth import current_user
from tret.api.workspace import WorkspaceContext, current_workspace, require_workspace_approver
from tret.batches import sharing
from tret.batches.files import INLINE_TEXT_MAX_BYTES, ArchiveFile, serve_file, stream_zip
from tret.db.engine import get_db
from tret.db.models import Batch, BatchTarget, User
from tret.services import collections as service

router = APIRouter(prefix="/api", tags=["batch-shares"])


class CreateShareBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str | None = None
    workspace_id: uuid.UUID | None = None


class CopyFilesBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_ids: list[uuid.UUID] | None = None


def _zip_response(batch: Batch, files: list[ArchiveFile]) -> StreamingResponse:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", batch.run_slug or "batch").strip("-.") or "batch"
    info = {"id": str(batch.id), "name": batch.name, "run_slug": batch.run_slug}
    return StreamingResponse(
        stream_zip(info, files),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{slug}.zip"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )


# ── owner side ───────────────────────────────────────────────────────────────
@router.post("/batches/{batch_id}/shares")
async def create_batch_share(
    batch_id: uuid.UUID,
    body: CreateShareBody,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(require_workspace_approver),
    db: AsyncSession = Depends(get_db),
):
    batch = await service.get_collection(db, batch_id, ctx.id)
    share = await sharing.create_share(db, batch, user=user, email=body.email, workspace_id=body.workspace_id)
    return await sharing.share_out(db, share)


@router.get("/batches/{batch_id}/shares")
async def list_batch_shares(
    batch_id: uuid.UUID,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    batch = await service.get_collection(db, batch_id, ctx.id)
    return {"shares": [await sharing.share_out(db, s) for s in await sharing.list_shares(db, batch)]}


@router.delete("/batches/{batch_id}/shares/{share_id}")
async def revoke_batch_share(
    batch_id: uuid.UUID,
    share_id: uuid.UUID,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(require_workspace_approver),
    db: AsyncSession = Depends(get_db),
):
    batch = await service.get_collection(db, batch_id, ctx.id)
    await sharing.revoke_share(db, batch, share_id)
    return {"ok": True}


@router.get("/batches/{batch_id}/archive")
async def download_own_archive(
    batch_id: uuid.UUID,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """The owner's own batch as a zip: every live row file and the stored record files
    (`records/`). The recipient's archive has no records."""
    batch = await service.get_collection(db, batch_id, ctx.id)
    return _zip_response(batch, await sharing.archive_files(db, batch, include_records=True))


# ── recipient side ───────────────────────────────────────────────────────────
@router.get("/batch-shares/incoming")
async def incoming_batch_shares(
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    return {"shares": await sharing.incoming_shares(db, user)}


@router.post("/batch-shares/{share_id}/accept")
async def accept_batch_share(
    share_id: uuid.UUID,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(require_workspace_approver),
    db: AsyncSession = Depends(get_db),
):
    share, batch = await sharing.accept_share(db, share_id, user=user, workspace_id=ctx.id)
    return await sharing.build_shared_batch(db, share, batch, detail=False)


@router.post("/batch-shares/{share_id}/decline")
async def decline_batch_share(
    share_id: uuid.UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    await sharing.decline_share(db, share_id, user=user)
    return {"ok": True}


@router.get("/shared-batches")
async def list_shared_batches(
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    return {
        "batches": [
            await sharing.build_shared_batch(db, share, batch, detail=False)
            for share, batch in await sharing.shared_batches(db, ctx.id)
        ]
    }


@router.get("/shared-batches/{batch_id}")
async def get_shared_batch(
    batch_id: uuid.UUID,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    share, batch = await sharing.get_shared_batch(db, batch_id, ctx.id)
    return await sharing.build_shared_batch(db, share, batch, detail=True)


@router.delete("/shared-batches/{batch_id}")
async def remove_shared_batch(
    batch_id: uuid.UUID,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(require_workspace_approver),
    db: AsyncSession = Depends(get_db),
):
    await sharing.remove_share(db, batch_id, workspace_id=ctx.id)
    return {"ok": True}


@router.get("/shared-batches/{batch_id}/targets/{target_id}/files")
async def get_shared_target_file(
    batch_id: uuid.UUID,
    target_id: uuid.UUID,
    path: str,
    download: bool = False,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    share, batch = await sharing.get_shared_batch(db, batch_id, ctx.id)
    target = await db.get(BatchTarget, target_id)
    if target is None or target.collection_id != batch.id:
        raise HTTPException(404, "This target has no file at that path")
    found = next(
        ((e, d) for e, d in await sharing.target_files(db, batch, target) if e.get("filename") == path), None
    )
    if found is None:
        raise HTTPException(404, "This target has no file at that path")
    entry, doc = found
    return await serve_file(
        doc, path, entry.get("byte_size"), download=download, inline_max=INLINE_TEXT_MAX_BYTES
    )


@router.get("/shared-batches/{batch_id}/archive")
async def download_shared_archive(
    batch_id: uuid.UUID,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    share, batch = await sharing.get_shared_batch(db, batch_id, ctx.id)
    return _zip_response(batch, await sharing.archive_files(db, batch))


@router.post("/shared-batches/{batch_id}/copy-files")
async def copy_shared_files(
    batch_id: uuid.UUID,
    body: CopyFilesBody | None = None,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(require_workspace_approver),
    db: AsyncSession = Depends(get_db),
):
    share, batch = await sharing.get_shared_batch(db, batch_id, ctx.id)
    return await sharing.copy_files(
        db, batch, share, dest_workspace_id=ctx.id,
        target_ids=body.target_ids if body else None, user=user,
    )
