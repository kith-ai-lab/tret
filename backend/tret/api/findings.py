"""Findings, approvals (the blessing gate), and data requests.

The approver identity is stamped from the session — the API accepts no
approver field, by design.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tret.api.auth import current_user
from tret.api.workspace import (
    WorkspaceContext,
    current_project,
    current_workspace,
    project_in_workspace,
    require_workspace_approver,
)
from tret.db.engine import get_db
from tret.db.models import Approval, DataRequest, Finding, User
from tret.services import connections as connections_service
from tret.services.outcomes import record_outcome_for_finding

router = APIRouter(prefix="/api", tags=["findings"])

APPROVAL_ACTIONS = ("approve", "reject")


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
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    project = await current_project(db, ctx.id)
    if project is None:
        return []
    q = (
        select(Finding)
        .where(Finding.project_id == project.id)
        .order_by(Finding.created_at.desc())
        .limit(min(limit, 500))
    )
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
    finding_id: uuid.UUID,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    f = await db.get(Finding, finding_id)
    if f is None or await project_in_workspace(db, f.project_id, ctx.id) is None:
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
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(require_workspace_approver),
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
    if f is None or await project_in_workspace(db, f.project_id, ctx.id) is None:
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
    # Approving a `connected_write` finding is the trigger for the actual
    # SharePoint upload — `propose_connected_write` (engine/tools.py) never
    # touches Graph itself, only this does, and only once the status flip
    # above has already committed. A rejected finding is never uploaded.
    if body.action == "approve" and f.schema_slug == "connected_write":
        await _upload_connected_write(db, f, ctx.id, actor_user_id=user.id)
    # A human just said whether this output was right, which is the strongest
    # signal tret has about the model that produced it — re-score the run behind
    # it so routing can learn from the verdict. After the commit, deliberately:
    # the approval is the thing that must succeed, and outcome bookkeeping is
    # rebuildable (`tret outcomes backfill`). It never raises.
    await record_outcome_for_finding(db, f.id)
    return {"ok": True, "status": f.status, "approver": user.display_name}


async def _render_write_bytes(
    db: AsyncSession, project_id: uuid.UUID, payload: dict, source: dict
) -> tuple[bytes, str]:
    """The bytes + content_type for a `connected_write` finding's payload
    `source`. Raises `ValueError` for anything that keeps the upload from
    happening — an unrecognized source kind, a deliverable with nothing
    approved to render, or WeasyPrint being unavailable for a `pdf` deliverable
    — so `_upload_connected_write`'s single except clause can record all three
    the same way it records a `ConnectionWriteError`.
    """
    if source.get("kind") == "inline":
        content = source.get("content") or ""
        return content.encode("utf-8"), payload.get("content_type") or "application/octet-stream"
    if source.get("kind") == "deliverable":
        from tret.services.export import DeliverableEmpty, PdfUnavailable, render_deliverable_bytes

        slug = source.get("slug")
        try:
            return await render_deliverable_bytes(
                db, project_id, slug, source.get("format") or "markdown"
            )
        except DeliverableEmpty as e:
            raise ValueError(f"No approved sections exist for deliverable '{slug}'.") from e
        except PdfUnavailable as e:
            raise ValueError(
                "PDF rendering unavailable: WeasyPrint's native libraries are missing in "
                f"this environment ({e})."
            ) from e
    raise ValueError(f"Unknown write source kind '{source.get('kind')}'")


async def _upload_connected_write(
    db: AsyncSession, finding: Finding, workspace_id: uuid.UUID, *, actor_user_id: uuid.UUID
) -> None:
    """Perform the SharePoint upload behind an approved `connected_write`
    finding, recording the outcome into its payload either way. Never raises:
    approval has already committed by the time this runs, and a failed upload
    is a recorded fact about the finding, not a reason to fail the request
    that approved it (retry it via `upload-retry` instead).
    """
    payload = dict(finding.payload)
    source = payload.get("source") or {}
    status_, item_id, web_url, uploaded_at, error = "failed", None, None, None, None
    try:
        data, content_type = await _render_write_bytes(db, finding.project_id, payload, source)
        # Inline content was hashed at proposal time (`content_sha256`); a
        # deliverable-sourced write has none (rendered fresh at approval, so
        # there is nothing fixed to have drifted from). Recomputing here for
        # the inline case catches a payload tampered with between proposal
        # and approval — never uploads bytes that no longer match what was
        # actually approved.
        expected_sha256 = payload.get("content_sha256")
        if source.get("kind") == "inline" and expected_sha256:
            if hashlib.sha256(data).hexdigest() != expected_sha256:
                raise ValueError(
                    "Content hash mismatch; the proposal was altered after it was made."
                )
        result = await connections_service.upload_connected_file(
            db,
            workspace_id=workspace_id,
            target_slug=payload.get("target_slug"),
            filename=payload.get("filename"),
            data=data,
            content_type=content_type,
            actor_user_id=actor_user_id,
        )
        status_ = "uploaded"
        item_id, web_url = result.item_id, result.web_url
        uploaded_at = datetime.now(timezone.utc).isoformat()
    except (
        connections_service.ConnectionWriteError,
        connections_service.ConnectionUnavailable,
        ValueError,
    ) as e:
        error = getattr(e, "reason", None) or str(e)
    except Exception as e:  # noqa: BLE001 - the upload must never break approval
        error = f"Unexpected error: {type(e).__name__}: {e}"
    payload["upload"] = {
        "status": status_,
        "item_id": item_id,
        "web_url": web_url,
        "uploaded_at": uploaded_at,
        "error": error,
        "approver_id": str(actor_user_id),
    }
    finding.payload = payload  # a new dict, so SQLAlchemy sees the JSONB change
    await db.commit()


@router.post("/findings/{finding_id}/upload-retry")
async def retry_finding_upload(
    finding_id: uuid.UUID,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(require_workspace_approver),
    db: AsyncSession = Depends(get_db),
):
    """Re-run the upload behind an approved `connected_write` finding whose
    first attempt failed (a transient Graph error, a connection that has
    since been reconnected) — or whose outcome was never recorded at all:
    `payload["upload"]` is `None`/missing when the process died between the
    approval's status commit and `_upload_connected_write`'s own commit, and
    that finding would otherwise be stuck forever, since nothing else ever
    retries it. Retry accepts either. Only a finding whose upload is known
    to have succeeded (`status == "uploaded"`) has nothing to retry.

    Because `upload_connected_file` never overwrites (Graph's own `rename`
    conflict behaviour is create-only — see that module), retrying an
    upload that actually went through but was never recorded creates a
    *second*, renamed file rather than overwriting or deduplicating the
    first. There is no way to tell "never ran" apart from "ran and the
    record was lost" from here, so that duplicate is an accepted cost of
    recovering an unobserved outcome, not a bug in this endpoint.
    """
    # Locked for the length of the retry, same reasoning as decide_finding's
    # own lock: two concurrent retries of the same finding must not both
    # read 'failed'/None and both upload — the second must see the first's
    # committed result instead.
    f = await db.get(Finding, finding_id, with_for_update=True)
    if f is None or await project_in_workspace(db, f.project_id, ctx.id) is None:
        raise HTTPException(404, "Finding not found")
    if f.schema_slug != "connected_write":
        raise HTTPException(409, "Only connected_write findings support upload-retry")
    if f.status != "approved":
        raise HTTPException(409, f"Finding is '{f.status}', not 'approved'")
    upload = (f.payload or {}).get("upload")
    if upload is not None and upload.get("status") != "failed":
        raise HTTPException(409, "This finding's upload has not failed; nothing to retry")
    await _upload_connected_write(db, f, ctx.id, actor_user_id=user.id)
    return _finding_out(f)


@router.get("/deliverables")
async def list_deliverables(
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """Deliverables = draft_section findings grouped by subject.deliverable,
    latest finding per section winning (same rule as assembly)."""
    project = await current_project(db, ctx.id)
    if project is None:
        return []
    findings = (
        (
            await db.execute(
                select(Finding)
                .where(
                    # Same project as the export resolves — see api.workspace.current_project.
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
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    from fastapi.responses import HTMLResponse, PlainTextResponse, Response

    from tret.services.export import (
        DeliverableEmpty,
        PdfUnavailable,
        assemble_deliverable,
        render_deliverable_bytes,
    )

    if format not in ("markdown", "html", "json", "pdf"):
        raise HTTPException(422, "format must be markdown|html|json|pdf")
    project = await current_project(db, ctx.id)
    if project is None:
        raise HTTPException(404, "No approved sections exist for this deliverable")
    if format == "json":
        result = await assemble_deliverable(db, project.id, deliverable_slug, include_draft)
        if not result["sections"]:
            raise HTTPException(404, "No approved sections exist for this deliverable")
        return result
    try:
        data, content_type = await render_deliverable_bytes(
            db, project.id, deliverable_slug, format, include_draft
        )
    except DeliverableEmpty:
        raise HTTPException(404, "No approved sections exist for this deliverable")
    except PdfUnavailable as e:
        raise HTTPException(
            501,
            "PDF rendering unavailable: WeasyPrint's native libraries are missing "
            f"in this environment ({e}). The Docker image includes them; for local "
            "dev install pango (e.g. `brew install pango`).",
        )
    if format == "html":
        # The body is rendered from model-authored markdown. It is sanitized at
        # render time (services/html_sanitize), and these headers are the second
        # layer: a sandboxed, script-and-fetch-free context on an origin that
        # holds the reviewer's session, so a future renderer regression cannot
        # turn a drafted section into same-origin script or a tracking beacon.
        return HTMLResponse(
            data.decode("utf-8"),
            headers={
                "Content-Security-Policy": "sandbox; default-src 'none'; style-src 'unsafe-inline'",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
            },
        )
    if format == "pdf":
        return Response(
            data,
            media_type=content_type,
            headers={
                "Content-Disposition": f'attachment; filename="{deliverable_slug}.pdf"'
            },
        )
    return PlainTextResponse(data.decode("utf-8"), media_type="text/markdown")


class PublishDeliverableBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_slug: str
    filename: str
    format: str  # markdown | html | pdf


@router.post("/deliverables/{deliverable_slug}/publish")
async def publish_deliverable(
    deliverable_slug: str,
    body: PublishDeliverableBody,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(require_workspace_approver),
    db: AsyncSession = Depends(get_db),
):
    """The human route for writing a deliverable to a connected write target
    directly — no `connected_write` finding, no approval step of its own: the
    approver role required to call this endpoint at all *is* the blessing.
    Shares its rendering with `GET .../export` and with the approved-finding
    upload path via `services.export.render_deliverable_bytes`. Always
    approved-only content: unlike `GET .../export`'s own preview, there is
    no `include_draft` here — this route ships something, and what ships
    must never be an unapproved draft.
    """
    from tret.services.export import DeliverableEmpty, PdfUnavailable, render_deliverable_bytes

    if body.format not in ("markdown", "html", "pdf"):
        raise HTTPException(422, "format must be markdown|html|pdf")
    project = await current_project(db, ctx.id)
    if project is None:
        raise HTTPException(404, "No approved sections exist for this deliverable")
    try:
        data, content_type = await render_deliverable_bytes(db, project.id, deliverable_slug, body.format)
    except DeliverableEmpty:
        raise HTTPException(404, "No approved sections exist for this deliverable")
    except PdfUnavailable as e:
        raise HTTPException(
            501,
            "PDF rendering unavailable: WeasyPrint's native libraries are missing "
            f"in this environment ({e}). The Docker image includes them; for local "
            "dev install pango (e.g. `brew install pango`).",
        )
    try:
        safe_name = connections_service.safe_upload_filename(body.filename)
    except ValueError as e:
        raise HTTPException(400, str(e))
    try:
        result = await connections_service.upload_connected_file(
            db,
            workspace_id=ctx.id,
            target_slug=body.target_slug,
            filename=safe_name,
            data=data,
            content_type=content_type,
            actor_user_id=user.id,
        )
    except (connections_service.ConnectionWriteError, connections_service.ConnectionUnavailable) as e:
        # upload_connected_file already flushed the refusal's ConnectionActivity
        # row (an "upload_failed" one) onto this session — commit it now, before
        # raising, so it survives past this request's session close instead of
        # being rolled back along with it (see finding write-up: a refusal that
        # vanishes from the log is as wrong as a success that does).
        await db.commit()
        raise HTTPException(409, e.reason)
    # Same reasoning as the refusal branch above, for the success path: the
    # "upload" ConnectionActivity row upload_connected_file flushed must
    # actually be committed, not silently rolled back when this request's
    # session closes.
    await db.commit()
    return {
        "web_url": result.web_url,
        "item_id": result.item_id,
        "name": result.name,
        "size": result.size,
        "target_slug": result.target_slug,
        "path": result.path,
    }


@router.get("/data-requests")
async def list_data_requests(
    status: str | None = None,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    project = await current_project(db, ctx.id)
    if project is None:
        return []
    q = (
        select(DataRequest)
        .where(DataRequest.project_id == project.id)
        .order_by(DataRequest.created_at.desc())
        .limit(200)
    )
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
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    if body.status not in ("fulfilled", "dismissed", "open"):
        raise HTTPException(422, "status must be open|fulfilled|dismissed")
    r = await db.get(DataRequest, request_id)
    if r is None or await project_in_workspace(db, r.project_id, ctx.id) is None:
        raise HTTPException(404, "Data request not found")
    r.status = body.status
    await db.commit()
    return {"ok": True}
