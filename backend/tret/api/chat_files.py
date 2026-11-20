"""Saving a chat reply's file to the member's OneDrive/SharePoint (R19, R02).

Two endpoints under a conversation, used by the "Save to folder" button on an
HTML deliverable:

  GET  /api/chat/{id}/write-targets   where a file from this session may be saved
  POST /api/chat/{id}/files           save an assistant message's HTML block as `<filename>` there

A save is NOT a parallel upload path. It records the same `connected_write`
Finding the `propose_connected_write` tool records (create-only into the
target's `tret/` folder, sha256 of the bytes, audit sidecar) and runs it through
the same `_decide` -> `_upload_connected_write` pipeline. The bytes are never
client-supplied: the server reads them from the named assistant message (an HTML
block, or the whole reply) and attributes them to that message's own run, so a
member cannot forge a model's provenance onto content they wrote. The requesting
member must hold the approver role and is the approver: clicking Save is their own
act, stamped on the approval. It is
recorded with purpose "auto" (an approval that is not a review), so saving a file
never counts as quality evidence about the model that wrote it.
"""
from __future__ import annotations

import hashlib
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from tret.api.auth import current_user
from tret.api.findings import _decide
from tret.api.chat_preview import html_blocks
from tret.api.workspace import (
    WorkspaceContext,
    current_workspace,
    project_in_workspace,
    require_workspace_approver,
)
from tret.config import get_settings
from tret.db.engine import get_db
from tret.db.models import Conversation, Finding, Run, User, Workspace
from tret.engine.tools import MAX_CONNECTED_WRITE_CONTENT_BYTES
from tret.services import connections as connections_service
from tret.services.edition import resolve_edition

router = APIRouter(prefix="/api/chat", tags=["chat"])

# What the member sees (and the 409 body) when there is nowhere to save.
NO_OUTPUT_FOLDER_MESSAGE = (
    "No output folder set. Attach a folder to this session (+ → Add OneDrive folder) or add an "
    "output folder in Settings → Connections."
)


class SaveFileBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: str = Field(min_length=1, max_length=400)
    # Index into the conversation's messages (an assistant message), and the
    # 0-based HTML block within it (null = the whole message content).
    message: int = Field(ge=0)
    block: int | None = Field(default=None, ge=0)
    target_slug: str | None = None


async def _conversation(db: AsyncSession, ctx: WorkspaceContext, conversation_id: uuid.UUID) -> Conversation:
    conv = await db.get(Conversation, conversation_id)
    if conv is None or await project_in_workspace(db, conv.project_id, ctx.id) is None:
        raise HTTPException(404, "Conversation not found")
    return conv


async def _targets_for(
    db: AsyncSession, ctx: WorkspaceContext, conv: Conversation
) -> tuple[list[tuple[connections_service.WriteTarget, str]], str | None]:
    """`(targets, reason_if_none)`: the session's folders (cohort only) then the
    workspace's output folders, each tagged "session" / "workspace". Empty with
    a reason when the connection is unusable, lacks write access, or there is
    simply no folder."""
    workspace = await db.get(Workspace, ctx.id)
    cohort = resolve_edition(workspace.settings if workspace else None, get_settings().edition) == "cohort"
    try:
        conn = await connections_service.ensure_connection_usable(db, ctx.id, connections_service.M365)
        if not connections_service.connection_has_write_scopes(conn):
            return [], (
                "This workspace's Microsoft 365 connection does not have write access. An admin "
                "needs to reconnect it with write permission in Settings → Connections."
            )
        resolved = await connections_service.resolve_write_targets(
            db, ctx.id, session_folders=conv.connected_folders, include_session=cohort
        )
    except connections_service.ConnectionUnavailable as e:
        return [], e.reason
    if not resolved:
        return [], NO_OUTPUT_FOLDER_MESSAGE
    return resolved, None


@router.get("/{conversation_id}/write-targets")
async def get_write_targets(
    conversation_id: uuid.UUID,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    conv = await _conversation(db, ctx, conversation_id)
    resolved, reason = await _targets_for(db, ctx, conv)
    return {
        "targets": [
            {"slug": t.slug, "label": t.label, "path": t.path, "kind": kind} for t, kind in resolved
        ],
        "reason_if_none": reason,
    }


@router.post("/{conversation_id}/files")
async def save_file(
    conversation_id: uuid.UUID,
    body: SaveFileBody,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(require_workspace_approver),
    db: AsyncSession = Depends(get_db),
):
    conv = await _conversation(db, ctx, conversation_id)
    resolved, reason = await _targets_for(db, ctx, conv)
    if not resolved:
        # One code for "nowhere to save" however it came about; the message says which.
        message = NO_OUTPUT_FOLDER_MESSAGE if reason == NO_OUTPUT_FOLDER_MESSAGE else (reason or NO_OUTPUT_FOLDER_MESSAGE)
        raise HTTPException(409, {"code": "no_output_folder", "message": message})
    by_slug = {t.slug: (t, kind) for t, kind in resolved}
    if body.target_slug is not None:
        if body.target_slug not in by_slug:
            raise HTTPException(404, f"Unknown target '{body.target_slug}'. Allowed: {sorted(by_slug)}")
        target, kind = by_slug[body.target_slug]
    else:
        target, kind = resolved[0]

    try:
        safe_name = connections_service.safe_upload_path(body.filename)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e

    # The bytes come from the stored message, never from the request.
    messages = conv.messages or []
    if body.message >= len(messages) or messages[body.message].get("role") != "assistant":
        raise HTTPException(404, "Message not found")
    msg = messages[body.message]
    text = msg.get("content") or ""
    if body.block is None:
        content = text
    else:
        blocks = html_blocks(text)
        if body.block >= len(blocks):
            raise HTTPException(404, "HTML block not found")
        content = blocks[body.block]
    data = content.encode("utf-8")
    if not data:
        raise HTTPException(422, "content is empty")
    if len(data) > MAX_CONNECTED_WRITE_CONTENT_BYTES:
        raise HTTPException(413, f"content is {len(data)} bytes, over the {MAX_CONNECTED_WRITE_CONTENT_BYTES} byte limit")

    # The finding's run is the message's own run, never a client choice.
    run: Run | None = None
    raw_run_id = msg.get("run_id")
    if raw_run_id:
        try:
            run = await db.get(Run, uuid.UUID(str(raw_run_id)))
        except ValueError:
            run = None
    if run is None or run.conversation_id != conv.id:
        raise HTTPException(409, {"code": "no_run", "message": "This reply has no run to attribute a saved file to."})

    from tret.engine.tools import _connected_write_content_type  # content type by extension

    sha = hashlib.sha256(data).hexdigest()
    finding = Finding(
        run_id=run.id,
        project_id=conv.project_id,
        pack_id=None,
        schema_slug="connected_write",
        subject={"target": target.slug, "filename": safe_name},
        payload={
            "target_slug": target.slug,
            "target_label": target.label,
            "target_path": target.path,
            "filename": safe_name,
            "relative_dir": safe_name.rpartition("/")[0],
            "content_type": _connected_write_content_type(safe_name),
            "source": {"kind": "inline", "content": content},
            "size": len(data),
            "content_sha256": sha,
            "target_kind": kind,
            **(
                {"target_resolved": connections_service.write_target_to_dict(target, kind)}
                if kind == "session"
                else {}
            ),
            "conflict": "fail",
            "saved_from": "chat_save",
            "upload": None,
        },
        provenance={
            "model": run.model_used,
            "saved_by_user": str(user.id),
            "via": "POST /api/chat/{id}/files",
            "retrieved_values": [],
            "document_ids": [],
        },
        status="draft",
    )
    db.add(finding)
    await db.flush()
    finding_id = finding.id
    await db.commit()

    await _decide(
        db, finding_id, "approve", "Saved by the member from the chat",
        approver_id=user.id, approver_name=user.display_name, workspace_id=ctx.id, auto=True,
    )
    saved = await db.get(Finding, finding_id, populate_existing=True)
    payload = saved.payload or {}
    upload = payload.get("upload") or {}
    audit = payload.get("audit") or {}
    return {
        "finding_id": str(finding_id),
        "status": "uploaded" if upload.get("status") == "uploaded" else "failed",
        "filename": safe_name,
        "path": upload.get("path"),
        "web_url": upload.get("web_url"),
        "sha256": upload.get("sha256") or sha,
        "audit_filename": audit.get("filename") if audit and not audit.get("error") else None,
        "error": upload.get("error"),
    }
