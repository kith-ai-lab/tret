"""Stage 2 module extraction results (`tret/evaluation/extract.py`): read-only routes.

* `GET /api/disclosure-reports/{report_id}/extractions`: the extractions of one report.
* `GET /api/report-extractions/{id}`: one extraction with its modules (spans, pages, word counts).
* `GET /api/report-extractions/{id}/modules/{module}/text`: the module's verbatim text from
  the blob store, for the side-by-side hand check against
  `/api/disclosure-reports/{id}/markdown`.
* `GET /api/report-extractions/review?batch_id=&kind=&include_resolved=&limit=&offset=`: the
  computed review list (`tret/evaluation/review.py`), most severe first, paged, with a `total`.
* `PUT /api/report-extractions/{id}/review`: an approver's decision (accepted | dismissed |
  fixed) on one item, so it does not come back. NACE fixes go through
  `PUT /api/company-classifications/manual`.

Every route is a 404 while `TRET_DISCLOSURE_EXTRACT` is off. Rows are workspace-scoped; another
workspace's id is a 404. Extractions are made by the `disclosure_extract` batch profile.
"""
from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tret.api.auth import current_user
from tret.api.workspace import WorkspaceContext, current_workspace, require_workspace_approver
from tret.batches.profiles.disclosure_extract import extract_enabled
from tret.config import get_settings
from tret.db.engine import get_db
from tret.db.models import Batch, ReportExtraction, ReportModule
from tret.evaluation import review as rv
from tret.storage import get_store


def _require_enabled() -> None:
    if not extract_enabled(get_settings()):
        raise HTTPException(404, "Not found")


router = APIRouter(prefix="/api", tags=["report-extractions"], dependencies=[Depends(_require_enabled)])


def _ext_out(e: ReportExtraction) -> dict[str, Any]:
    return {
        "id": str(e.id),
        "disclosure_report_id": str(e.disclosure_report_id),
        "batch_target_id": str(e.batch_target_id) if e.batch_target_id else None,
        "run_id": str(e.run_id) if e.run_id else None,
        "extractor_version": e.extractor_version,
        "module_set": e.module_set,
        "model": e.model,
        "models": e.models or [],
        "status": e.status,
        "metadata": e.metadata_json or {},
        "windows": e.windows,
        "issues": e.issues or [],
        "created_at": e.created_at.isoformat() if e.created_at else None,
        "updated_at": e.updated_at.isoformat() if e.updated_at else None,
    }


def _mod_out(m: ReportModule) -> dict[str, Any]:
    return {
        "module": m.module,
        "present": m.present,
        "spans": m.spans or [],
        "word_count": m.word_count,
        "confidence": m.confidence,
        "truncated": m.truncated,
        "note": m.note,
        "text_sha256": m.text_sha256,
        "has_text": bool(m.text_key),
        "claim_count": m.claim_count,
        "claim_density": m.claim_density,
        "claim_counts": m.claim_counts,
        "density_version": m.density_version,
    }


async def _extraction(db: AsyncSession, extraction_id: uuid.UUID, ctx: WorkspaceContext) -> ReportExtraction:
    e = await db.get(ReportExtraction, extraction_id)
    if e is None or e.workspace_id != ctx.id:
        raise HTTPException(404, "Extraction not found")
    return e


class ReviewDecisionIn(BaseModel):
    item_key: str = Field(min_length=1, max_length=200, description="`item_key` of a review item")
    decision: Literal["accepted", "dismissed", "fixed"]
    note: str | None = Field(default=None, max_length=2000)


@router.get("/report-extractions/review")
async def get_review_list(
    batch_id: uuid.UUID | None = None,
    kind: list[str] = Query(default=[]),
    include_resolved: bool = False,
    limit: int = Query(rv.DEFAULT_LIMIT, ge=1, le=rv.MAX_LIMIT),
    offset: int = Query(0, ge=0),
    user=Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """What a human should look at: computed from the stored extractions, minus the items with a
    decision unless `include_resolved`. `batch_id` is the source collection batch or the
    extraction batch; `kind` (repeatable) limits the item kinds; `limit` (default 500, max 5000) and
    `offset` page through the list, `total` counts all of it."""
    if batch_id is not None:
        b = await db.get(Batch, batch_id)
        if b is None or b.workspace_id != ctx.id:
            raise HTTPException(404, "Batch not found")
    try:
        items, total = await rv.review_page(
            db, workspace_id=ctx.id, batch_id=batch_id, include_resolved=include_resolved, kinds=kind or None,
            limit=limit, offset=offset,
        )
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    return {"items": [i.as_dict() for i in items], "count": len(items), "total": total, "limit": limit, "offset": offset}


@router.put("/report-extractions/{extraction_id}/review")
async def put_review_decision(
    extraction_id: uuid.UUID,
    body: ReviewDecisionIn,
    user=Depends(current_user),
    ctx: WorkspaceContext = Depends(require_workspace_approver),
    db: AsyncSession = Depends(get_db),
):
    try:
        decision = await rv.set_decision(
            db, workspace_id=ctx.id, extraction_id=extraction_id, item_key=body.item_key,
            decision=body.decision, note=body.note, reviewed_by=user.email,
        )
    except rv.ReviewNotFound as e:
        raise HTTPException(404, str(e)) from None
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    await db.commit()
    return {"extraction_id": str(extraction_id), "item_key": body.item_key, **decision}


@router.get("/disclosure-reports/{report_id}/extractions")
async def list_report_extractions(
    report_id: uuid.UUID,
    user=Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    rows = (
        await db.execute(
            select(ReportExtraction)
            .where(ReportExtraction.disclosure_report_id == report_id, ReportExtraction.workspace_id == ctx.id)
            .order_by(ReportExtraction.created_at.desc())
        )
    ).scalars().all()
    return {"extractions": [_ext_out(e) for e in rows]}


@router.get("/report-extractions/{extraction_id}")
async def get_report_extraction(
    extraction_id: uuid.UUID,
    user=Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    e = await _extraction(db, extraction_id, ctx)
    mods = (
        await db.execute(
            select(ReportModule).where(ReportModule.extraction_id == e.id).order_by(ReportModule.module)
        )
    ).scalars().all()
    return {**_ext_out(e), "modules": [_mod_out(m) for m in mods]}


@router.get("/report-extractions/{extraction_id}/modules/{module}/text")
async def get_module_text(
    extraction_id: uuid.UUID,
    module: str,
    user=Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    e = await _extraction(db, extraction_id, ctx)
    m = (
        await db.execute(
            select(ReportModule).where(ReportModule.extraction_id == e.id, ReportModule.module == module)
        )
    ).scalar_one_or_none()
    if m is None:
        raise HTTPException(404, "Module not found")
    if not m.text_key:
        raise HTTPException(404, "This module is not present in the report, so it has no text")
    try:
        data = await get_store().get(m.text_key)
    except FileNotFoundError:
        raise HTTPException(404, "The module text is missing from storage") from None
    return Response(content=data, media_type="text/markdown; charset=utf-8")
