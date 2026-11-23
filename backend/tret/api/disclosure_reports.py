"""Stage 1 normalised reports (`tret/services/normalise.py`): read-only routes.

`GET /api/disclosure-reports?batch_id=...` lists reports with their metrics (never the
Markdown), `GET /api/disclosure-reports/{id}` returns one, and
`GET /api/disclosure-reports/{id}/markdown` serves the stored Markdown from the blob
store. Every route is a 404 while `TRET_DISCLOSURE_NORMALISER` is off, as though the
feature did not exist. Reads are open to any workspace member; reports are found
through their project (or their batch), so another workspace's report is a 404 too.
There are no write endpoints: reports are made by `tret disclosure normalise`.
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tret.api.auth import current_user
from tret.api.workspace import WorkspaceContext, current_project, current_workspace, project_in_workspace
from tret.config import get_settings
from tret.db.engine import get_db
from tret.db.models import Batch, BatchTarget, DisclosureReport, User
from tret.services.normalise import normaliser_enabled
from tret.storage import get_store

router = APIRouter(prefix="/api", tags=["disclosure-reports"])

LIST_LIMIT = 500


def _require_enabled() -> None:
    if not normaliser_enabled(get_settings()):
        raise HTTPException(404, "Not found")


def _out(r: DisclosureReport, target: BatchTarget | None = None) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "document_id": str(r.document_id),
        "batch_target_id": str(r.batch_target_id) if r.batch_target_id else None,
        "target_slug": target.slug if target is not None else None,
        "target_label": (target.label or target.company) if target is not None else None,
        "run_id": str(r.run_id) if r.run_id else None,
        "is_primary": r.is_primary,
        "source_kind": r.source_kind,
        "status": r.status,
        "quarantine_reason": r.quarantine_reason,
        "metrics": r.metrics or {},
        "markdown_sha256": r.markdown_sha256,
        "markdown_chars": r.markdown_chars,
        "has_markdown": bool(r.markdown_key),
        "normaliser_version": r.normaliser_version,
        "error": r.error,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }


async def _report_in_workspace(db: AsyncSession, report_id: uuid.UUID, ctx: WorkspaceContext) -> DisclosureReport:
    report = await db.get(DisclosureReport, report_id)
    if report is None or await project_in_workspace(db, report.project_id, ctx.id) is None:
        raise HTTPException(404, "Report not found")
    return report


@router.get("/disclosure-reports")
async def list_disclosure_reports(
    batch_id: uuid.UUID | None = None,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    _require_enabled()
    q = select(DisclosureReport, BatchTarget).outerjoin(
        BatchTarget, BatchTarget.id == DisclosureReport.batch_target_id
    )
    if batch_id is not None:
        batch = await db.get(Batch, batch_id)
        if batch is None or batch.workspace_id != ctx.id:
            raise HTTPException(404, "Batch not found")
        q = q.where(BatchTarget.collection_id == batch.id).order_by(
            BatchTarget.position, DisclosureReport.created_at
        )
    else:
        project = await current_project(db, ctx.id)
        if project is None:
            return {"reports": []}
        q = q.where(DisclosureReport.project_id == project.id).order_by(DisclosureReport.created_at.desc())
    rows = (await db.execute(q.limit(LIST_LIMIT))).all()
    return {"reports": [_out(r, t) for r, t in rows]}


@router.get("/disclosure-reports/{report_id}")
async def get_disclosure_report(
    report_id: uuid.UUID,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    _require_enabled()
    report = await _report_in_workspace(db, report_id, ctx)
    target = await db.get(BatchTarget, report.batch_target_id) if report.batch_target_id else None
    return _out(report, target)


@router.get("/disclosure-reports/{report_id}/markdown")
async def get_disclosure_report_markdown(
    report_id: uuid.UUID,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    _require_enabled()
    report = await _report_in_workspace(db, report_id, ctx)
    if not report.markdown_key:
        raise HTTPException(404, "This report has no Markdown")
    try:
        data = await get_store().get(report.markdown_key)
    except FileNotFoundError:
        raise HTTPException(404, "The report's Markdown is missing from storage") from None
    return Response(content=data, media_type="text/markdown; charset=utf-8")
