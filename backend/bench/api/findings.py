"""Findings, approvals (the blessing gate), and data requests.

The approver identity is stamped from the session — the API accepts no
approver field, by design.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bench.api.auth import current_user, require_approver
from bench.db.engine import get_db
from bench.db.models import Approval, DataRequest, Finding, User

router = APIRouter(prefix="/api", tags=["findings"])


def _finding_out(f: Finding, approvals: list[Approval] | None = None) -> dict:
    out = {
        "id": str(f.id),
        "run_id": str(f.run_id),
        "schema_slug": f.schema_slug,
        "subject": f.subject,
        "payload": f.payload,
        "provenance": f.provenance,
        "status": f.status,
        "created_at": f.created_at.isoformat() if f.created_at else None,
    }
    if approvals is not None:
        out["approvals"] = [
            {
                "action": a.action,
                "approver_id": str(a.approver_id),
                "note": a.note,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
            for a in approvals
        ]
    return out


@router.get("/findings")
async def list_findings(
    status: str | None = None,
    schema_slug: str | None = None,
    run_id: uuid.UUID | None = None,
    limit: int = 100,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    q = select(Finding).order_by(Finding.created_at.desc()).limit(min(limit, 500))
    if status:
        q = q.where(Finding.status == status)
    if schema_slug:
        q = q.where(Finding.schema_slug == schema_slug)
    if run_id:
        q = q.where(Finding.run_id == run_id)
    findings = (await db.execute(q)).scalars().all()
    return [_finding_out(f) for f in findings]


@router.get("/findings/{finding_id}")
async def get_finding(
    finding_id: uuid.UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    f = await db.get(Finding, finding_id)
    if f is None:
        raise HTTPException(404, "Finding not found")
    approvals = (
        (await db.execute(select(Approval).where(Approval.finding_id == finding_id)))
        .scalars()
        .all()
    )
    return _finding_out(f, approvals)


class ApprovalBody(BaseModel):
    action: str  # approve | reject
    note: str | None = None
    # Deliberately NO approver field — identity comes from the session.


@router.post("/findings/{finding_id}/approval")
async def decide_finding(
    finding_id: uuid.UUID,
    body: ApprovalBody,
    user: User = Depends(require_approver),
    db: AsyncSession = Depends(get_db),
):
    if body.action not in ("approve", "reject"):
        raise HTTPException(422, "action must be 'approve' or 'reject'")
    f = await db.get(Finding, finding_id)
    if f is None:
        raise HTTPException(404, "Finding not found")
    if f.status != "draft":
        raise HTTPException(409, f"Finding is already '{f.status}'")
    approval = Approval(
        finding_id=f.id,
        action=body.action,
        approver_id=user.id,  # session-stamped
        note=body.note,
    )
    db.add(approval)
    f.status = "approved" if body.action == "approve" else "rejected"
    await db.commit()
    return {"ok": True, "status": f.status, "approver": user.display_name}


@router.get("/deliverables/{deliverable_slug}/export")
async def export_deliverable(
    deliverable_slug: str,
    format: str = "markdown",
    include_draft: bool = False,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    from fastapi.responses import HTMLResponse, PlainTextResponse
    from sqlalchemy import select as _select

    from bench.db.models import Project
    from bench.services.export import assemble_deliverable

    project = (await db.execute(_select(Project))).scalars().first()
    result = await assemble_deliverable(db, project.id, deliverable_slug, include_draft)
    if not result["sections"]:
        raise HTTPException(404, "No approved sections exist for this deliverable")
    if format == "html":
        return HTMLResponse(result["html"])
    if format == "json":
        return result
    return PlainTextResponse(result["markdown"], media_type="text/markdown")


@router.get("/data-requests")
async def list_data_requests(
    status: str | None = None, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    q = select(DataRequest).order_by(DataRequest.created_at.desc()).limit(200)
    if status:
        q = q.where(DataRequest.status == status)
    reqs = (await db.execute(q)).scalars().all()
    return [
        {
            "id": str(r.id),
            "run_id": str(r.run_id),
            "subject": r.subject,
            "what_is_missing": r.what_is_missing,
            "why_needed": r.why_needed,
            "status": r.status,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in reqs
    ]


class DataRequestUpdate(BaseModel):
    status: str  # fulfilled | dismissed


@router.post("/data-requests/{request_id}/status")
async def update_data_request(
    request_id: uuid.UUID,
    body: DataRequestUpdate,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    if body.status not in ("fulfilled", "dismissed", "open"):
        raise HTTPException(422, "status must be open|fulfilled|dismissed")
    r = await db.get(DataRequest, request_id)
    if r is None:
        raise HTTPException(404, "Data request not found")
    r.status = body.status
    await db.commit()
    return {"ok": True}
