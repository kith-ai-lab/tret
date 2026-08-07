"""Findings, approvals (the blessing gate), and data requests.

The approver identity is stamped from the session — the API accepts no
approver field, by design.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bench.api.auth import current_user, require_approver
from bench.db.engine import get_db
from bench.db.models import Approval, DataRequest, Finding, Project, User

router = APIRouter(prefix="/api", tags=["findings"])

APPROVAL_ACTIONS = ("approve", "reject")


async def _current_project(db: AsyncSession) -> Project | None:
    """The project bench operates on.

    bench is single-project today (bootstrap seeds exactly one, and the UI has no
    project picker). This is the one place that assumption is written down, so the
    deliverable listing and the deliverable export agree on *which* project they
    mean — before this existed the listing spanned every project while the export
    read the first one, so in a two-project database the UI offered deliverables
    whose export could only 404.
    """
    return (await db.execute(select(Project).order_by(Project.created_at))).scalars().first()


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
    # Deliberately NO approver field — identity comes from the session. `forbid`
    # makes that refusal audible: a client that sends `approver`, `approver_id`
    # or `user_id` gets a 422 naming the field instead of a silently ignored
    # claim, so an attempt to sign someone else's name cannot look like a
    # success. Keep this in step with the frontend, which sends {action, note}.
    model_config = ConfigDict(extra="forbid")

    action: str  # approve | reject
    note: str | None = None


@router.post("/findings/{finding_id}/approval")
async def decide_finding(
    finding_id: uuid.UUID,
    body: ApprovalBody,
    user: User = Depends(require_approver),
    db: AsyncSession = Depends(get_db),
):
    if body.action not in APPROVAL_ACTIONS:
        raise HTTPException(422, "action must be 'approve' or 'reject'")
    # Locked for the length of the decision: two approvers deciding the same
    # draft at the same moment would otherwise both read 'draft', both write an
    # approvals row, and the later commit would decide the status — so a reject
    # could be overwritten by a concurrent approve with no 409 anywhere. The
    # lock makes the second request re-read the committed status and lose to the
    # 'already decided' branch below.
    f = await db.get(Finding, finding_id, with_for_update=True)
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


@router.get("/deliverables")
async def list_deliverables(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Deliverables = draft_section findings grouped by subject.deliverable,
    latest finding per section winning (same rule as assembly)."""
    project = await _current_project(db)
    if project is None:
        return []
    findings = (
        (
            await db.execute(
                select(Finding)
                .where(
                    # Same project as the export resolves — see _current_project.
                    Finding.project_id == project.id,
                    Finding.schema_slug == "draft_section",
                )
                .order_by(Finding.created_at)
            )
        )
        .scalars()
        .all()
    )
    groups: dict[str, dict] = {}
    for f in findings:
        slug = f.subject.get("deliverable", "untitled")
        section = f.subject.get("section", "untitled")
        g = groups.setdefault(slug, {"slug": slug, "sections": {}, "updated_at": None})
        g["sections"][section] = {
            "section": section,
            "status": f.status,
            "finding_id": str(f.id),
            "updated_at": f.created_at.isoformat() if f.created_at else None,
        }
        g["updated_at"] = f.created_at.isoformat() if f.created_at else g["updated_at"]
    out = []
    for g in groups.values():
        sections = list(g["sections"].values())
        out.append(
            {
                "slug": g["slug"],
                "sections": sections,
                "approved_count": sum(1 for s in sections if s["status"] == "approved"),
                "draft_count": sum(1 for s in sections if s["status"] == "draft"),
                "updated_at": g["updated_at"],
            }
        )
    out.sort(key=lambda d: d["updated_at"] or "", reverse=True)
    return out


@router.get("/deliverables/{deliverable_slug}/export")
async def export_deliverable(
    deliverable_slug: str,
    format: str = "markdown",
    include_draft: bool = False,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    from fastapi.responses import HTMLResponse, PlainTextResponse, Response

    from bench.services.export import PdfUnavailable, assemble_deliverable, render_pdf

    if format not in ("markdown", "html", "json", "pdf"):
        raise HTTPException(422, "format must be markdown|html|json|pdf")
    project = await _current_project(db)
    if project is None:
        raise HTTPException(404, "No approved sections exist for this deliverable")
    result = await assemble_deliverable(db, project.id, deliverable_slug, include_draft)
    if not result["sections"]:
        raise HTTPException(404, "No approved sections exist for this deliverable")
    if format == "html":
        # The body is rendered from model-authored markdown. It is sanitized at
        # render time (services/html_sanitize), and these headers are the second
        # layer: a sandboxed, script-and-fetch-free context on an origin that
        # holds the reviewer's session, so a future renderer regression cannot
        # turn a drafted section into same-origin script or a tracking beacon.
        return HTMLResponse(
            result["html"],
            headers={
                "Content-Security-Policy": "sandbox; default-src 'none'; style-src 'unsafe-inline'",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
            },
        )
    if format == "json":
        return result
    if format == "pdf":
        shas = {s.get("doctrine_sha") for s in result["sections"] if s.get("doctrine_sha")}
        try:
            pdf = render_pdf(
                result["html"],
                deliverable_slug,
                result["sections"],
                shas.pop() if len(shas) == 1 else None,
            )
        except PdfUnavailable as e:
            raise HTTPException(
                501,
                "PDF rendering unavailable: WeasyPrint's native libraries are missing "
                f"in this environment ({e}). The Docker image includes them; for local "
                "dev install pango (e.g. `brew install pango`).",
            )
        return Response(
            pdf,
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'attachment; filename="{deliverable_slug}.pdf"'
            },
        )
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
