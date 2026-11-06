"""Collection runs: a register of target companies collected one engine run per
target, with a review gate, batch approval, retry and a finalize step that
writes run-level records to SharePoint. The orchestration lives in
`tret/batches/` (re-exported by `tret/services/collections.py`); this module is
its HTTP surface.

Reads are open to any workspace member; every state change needs the approver
role, like the approvals it drives.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tret.api.auth import current_user
from tret.api.findings import _finding_out
from tret.api.workspace import WorkspaceContext, current_workspace, require_workspace_approver
from tret.db.engine import get_db
from tret.db.models import Batch, BatchTarget, User
from tret.services import collections as service
from tret.services import lifecycle

router = APIRouter(prefix="/api", tags=["collections"])


async def _collection_detail(db: AsyncSession, c: Batch) -> dict:
    """`Collection` + `targets` + `pending_maps` (draft package_map findings of
    this collection, each with its `target_id`)."""
    await db.refresh(c)
    targets = await service.load_targets(db, [c.id])
    runs = await service.load_runs(db, targets)
    profile = service.profile_for(c)
    maps = await service.load_review_findings(db, profile, targets)
    out = service.collection_out(c, targets, runs)
    out["targets"] = [
        service.target_out(t, runs.get(t.run_id), maps.get(t.run_id, []) if t.run_id else [], profile)
        for t in targets
    ]
    pending = []
    for t in targets:
        for f in maps.get(t.run_id, []) if t.run_id else []:
            if f.status == "draft":
                pending.append({**_finding_out(f), "target_id": str(t.id)})
    out["pending_maps"] = pending
    return out


async def _target_detail(db: AsyncSession, t: BatchTarget) -> dict:
    await db.refresh(t)
    runs = await service.load_runs(db, [t])
    c = await db.get(Batch, t.collection_id)
    profile = service.profile_for(c)
    maps = await service.load_review_findings(db, profile, [t])
    return service.target_out(
        t, runs.get(t.run_id), maps.get(t.run_id, []) if t.run_id else [], profile
    )


async def _target_in(db: AsyncSession, c: Batch, target_id: uuid.UUID) -> BatchTarget:
    t = await db.get(BatchTarget, target_id)
    if t is None or t.collection_id != c.id:
        raise HTTPException(404, "Target not found")
    return t


@router.post("/collections")
async def create_collection(
    body: service.CreateCollectionBody,
    request: Request,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(require_workspace_approver),
    db: AsyncSession = Depends(get_db),
):
    lifecycle.refuse_if_draining(request)
    c, _ = await service.create_batch(
        db, workspace_id=ctx.id, user_id=user.id, body=body, profile_slug=body.profile
    )
    return await _collection_detail(db, c)


@router.get("/collections")
async def list_collections(
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    collections = list(
        (
            await db.execute(
                select(Batch)
                .where(Batch.workspace_id == ctx.id)
                .order_by(Batch.created_at.desc())
            )
        ).scalars()
    )
    targets = await service.load_targets(db, [c.id for c in collections])
    runs = await service.load_runs(db, targets)
    by_collection: dict[uuid.UUID, list[BatchTarget]] = {}
    for t in targets:
        by_collection.setdefault(t.collection_id, []).append(t)
    return {
        "collections": [
            service.collection_out(c, by_collection.get(c.id, []), runs) for c in collections
        ]
    }


@router.get("/collections/{collection_id}")
async def get_collection(
    collection_id: uuid.UUID,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    c = await service.get_collection(db, collection_id, ctx.id)
    return await _collection_detail(db, c)


@router.post("/collections/{collection_id}/start")
async def start_collection(
    collection_id: uuid.UUID,
    request: Request,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(require_workspace_approver),
    db: AsyncSession = Depends(get_db),
):
    lifecycle.refuse_if_draining(request)
    c = await service.get_collection(db, collection_id, ctx.id)
    await service.start(db, c)
    return await _collection_detail(db, c)


@router.post("/collections/{collection_id}/pause")
async def pause_collection(
    collection_id: uuid.UUID,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(require_workspace_approver),
    db: AsyncSession = Depends(get_db),
):
    c = await service.get_collection(db, collection_id, ctx.id)
    await service.pause(db, c)
    return await _collection_detail(db, c)


@router.post("/collections/{collection_id}/resume")
async def resume_collection(
    collection_id: uuid.UUID,
    request: Request,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(require_workspace_approver),
    db: AsyncSession = Depends(get_db),
):
    lifecycle.refuse_if_draining(request)
    c = await service.get_collection(db, collection_id, ctx.id)
    await service.resume(db, c)
    return await _collection_detail(db, c)


@router.post("/collections/{collection_id}/release")
async def release_collection(
    collection_id: uuid.UUID,
    request: Request,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(require_workspace_approver),
    db: AsyncSession = Depends(get_db),
):
    lifecycle.refuse_if_draining(request)
    c = await service.get_collection(db, collection_id, ctx.id)
    await service.release(db, c)
    return await _collection_detail(db, c)


@router.post("/collections/{collection_id}/targets/{target_id}/retry")
async def retry_target(
    collection_id: uuid.UUID,
    target_id: uuid.UUID,
    request: Request,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(require_workspace_approver),
    db: AsyncSession = Depends(get_db),
):
    lifecycle.refuse_if_draining(request)
    c = await service.get_collection(db, collection_id, ctx.id)
    t = await _target_in(db, c, target_id)
    await service.retry_target(db, c, t)
    return await _target_detail(db, t)


@router.post("/collections/{collection_id}/targets/{target_id}/retry-write")
async def retry_target_write(
    collection_id: uuid.UUID,
    target_id: uuid.UUID,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(require_workspace_approver),
    db: AsyncSession = Depends(get_db),
):
    c = await service.get_collection(db, collection_id, ctx.id)
    t = await _target_in(db, c, target_id)
    await service.retry_write(db, c, t)
    return await _target_detail(db, t)


@router.get("/collections/{collection_id}/records")
async def preview_records(
    collection_id: uuid.UUID,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    c = await service.get_collection(db, collection_id, ctx.id)
    return await service.profile_for(c).records(db, c)


@router.post("/collections/{collection_id}/finalize")
async def finalize_collection(
    collection_id: uuid.UUID,
    body: service.FinalizeBody | None = None,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(require_workspace_approver),
    db: AsyncSession = Depends(get_db),
):
    c = await service.get_collection(db, collection_id, ctx.id)
    await service.finalize(db, c, force=bool(body and body.force))
    return await _collection_detail(db, c)
