"""Chat: the conversational front door.

Each user turn executes as a Run (task_type='chat') through the normal
engine — same routing, same audit trail, same SSE stream. The chat agent
carries a capability catalog of installed pack task types and delegates
structured work via the run_harness_task tool; delegated findings remain
drafts behind the approval gate.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from tret.api.auth import current_user
from tret.api.connections import USE_ACTION, require_connections_gate
from tret.api.workspace import WorkspaceContext, current_project, current_workspace, project_in_workspace
from tret.db.engine import get_db, get_session_factory
from tret.db.models import (
    AnalysisRun,
    Conversation,
    ConversationGroup,
    Dataset,
    Document,
    Finding,
    Harness,
    Pack,
    Project,
    Run,
    User,
)
from tret.engine.harness import get_harness_engine
from tret.engine.tools import DELEGATION_TOOLS
from tret.packs.links import pack_map_for_harnesses, packs_for_harness, resolve_pack_for_task
from tret.router_llm.objectives import OBJECTIVES
from tret.services import lifecycle
from tret.services.connections import (
    MAX_SESSION_FOLDERS,
    ConnectionAuthError,
    ConnectionUnavailable,
    resolve_session_folders,
)
from tret.services.emissions import emission_summary_fields, energy_wh_field
from tret.services.emissions_v3_wiring import method_v3_of, public_routing
from tret.services.reconcile import NON_TERMINAL_STATUSES

router = APIRouter(prefix="/api/chat", tags=["chat"])

_background_tasks: set[asyncio.Task] = set()

MAX_HISTORY_TURNS = 30

# A queued/running run older than this no longer counts as "live" for the chat
# page or the one-reply-at-a-time 409, so a single wedged run (orphaned on
# another machine, stuck in a provider call) cannot lock a conversation forever.
# Far beyond any real chat turn; the run itself is left alone.
ACTIVE_RUN_MAX_AGE = timedelta(hours=1)


def _conversation_out(c: Conversation, full: bool = False) -> dict:
    out = {
        "id": str(c.id),
        "title": c.title,
        "harness_id": str(c.harness_id),
        "message_count": len(c.messages or []),
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
        "folders": c.connected_folders or [],
        # R20-R22: the session's group (null = ungrouped) and when it was
        # archived (null = live). Archive hides, never deletes.
        "group_id": str(c.group_id) if c.group_id else None,
        "archived_at": c.archived_at.isoformat() if c.archived_at else None,
    }
    if full:
        out["messages"] = c.messages
    return out


async def _active_run_id(db: AsyncSession, conversation_id: uuid.UUID) -> str | None:
    """Id of the newest still-live top-level run (queued/running) on this
    conversation, or None. The chat page uses it to resume after a reload, and
    `send_message` uses it to refuse a second paid run while a reply is still
    being generated. Statuses come from `reconcile.NON_TERMINAL_STATUSES`.

    Sub-agent runs share the parent's conversation_id, so children are skipped
    (`parent_run_id IS NULL`): the page waits for an assistant message tagged
    with the top-level run's id and Stop must cancel that run. Runs older than
    `ACTIVE_RUN_MAX_AGE` are ignored so a wedged run cannot lock the
    conversation."""
    cutoff = datetime.now(timezone.utc) - ACTIVE_RUN_MAX_AGE
    run_id = (
        await db.execute(
            select(Run.id)
            .where(
                Run.conversation_id == conversation_id,
                Run.parent_run_id.is_(None),
                Run.status.in_(NON_TERMINAL_STATUSES),
                Run.created_at >= cutoff,
            )
            .order_by(Run.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return str(run_id) if run_id is not None else None


async def _capability_catalog(db: AsyncSession, workspace_id, project_id) -> str:
    """Snapshot of this workspace's installed pack task types, persisted with
    each chat run.

    Excludes chat-front-door harnesses (`task_profile == "chat"`): the chat
    harness now links every installed pack by default
    (`services.workspace._seed_chat_harness`), so without this filter every
    specialist task type would also be listed as reachable `[harness: Chat
    Assistant]` — a delegation target that both duplicates the real
    specialist harness's entry and is wrong besides (`run_harness_task`
    itself refuses `task_type in ("chat", "freeform")`, and a chat harness is
    not a valid delegation target — see `engine/tools.py`)."""
    harnesses = (
        (
            await db.execute(
                select(Harness).where(
                    Harness.workspace_id == workspace_id,
                    Harness.is_archived.is_(False),
                    Harness.task_profile != "chat",
                )
            )
        )
        .scalars()
        .all()
    )
    pack_map = await pack_map_for_harnesses(db, [h.id for h in harnesses])
    lines = ["## Capability catalog (for run_harness_task and delegate_parallel)"]
    seen = set()
    for h in harnesses:
        for pack in pack_map.get(h.id, []):
            for t in pack.manifest.get("task_types", []):
                key = (t["slug"], h.name)
                if key in seen:
                    continue
                seen.add(key)
                fields = ", ".join(
                    f"{name} ({spec.get('type', 'string')}"
                    + (f": {'|'.join(spec['enum'])}" if spec.get("enum") else "")
                    + ")"
                    + (f" — {spec['description']}" if spec.get("description") else "")
                    for name, spec in (t.get("input_schema") or {}).items()
                )
                lines.append(
                    f"- task_type: {t['slug']} — {t.get('display_name', t['slug'])} "
                    f"[harness: {h.name}] — inputs: {fields or '(none)'} — {t.get('output_contract', '').strip()}"
                )
    if len(lines) == 1:
        lines.append("(no specialist tasks installed)")

    all_packs = (
        await db.execute(select(Pack).where(Pack.workspace_id == workspace_id))
    ).scalars().all()
    method_lines: list[str] = []
    for p in all_packs:
        for m in p.manifest.get("methods", []):
            fields = ", ".join(
                f"{name} ({spec.get('type', 'string')}"
                + (f": {'|'.join(spec['enum'])}" if spec.get("enum") else "")
                + ")"
                + (f" — {spec['description']}" if spec.get("description") else "")
                for name, spec in (m.get("params_schema") or {}).items()
            )
            method_lines.append(
                f"- method: {m['slug']} — {m.get('display_name', m['slug'])} — "
                f"params: {fields or '(none)'} — {m.get('description', '').strip()}"
            )
    if method_lines:
        lines.append(
            "\n## Deterministic methods (for run_method — use for ANY computed number)"
        )
        lines.extend(method_lines)

    datasets = (
        await db.execute(
            select(Dataset).where(Dataset.project_id == project_id).order_by(Dataset.name)
        )
    ).scalars().all()
    if datasets:
        lines.append("\n## Datasets available via lookup_dataset")
        for ds in datasets:
            cols = ", ".join(ds.schema_json.get("columns", []))
            lines.append(f"- {ds.name} ({ds.row_count} rows; columns: {cols})")
    return "\n".join(lines)


class FolderRef(BaseModel):
    """A OneDrive/SharePoint folder the client picked. Only the ids are taken
    from the client; name/path are re-read from Graph by `resolve_session_folders`."""

    model_config = ConfigDict(extra="forbid")

    drive_id: str = Field(min_length=1, max_length=512)
    item_id: str = Field(min_length=1, max_length=512)


class CreateConversationBody(BaseModel):
    harness_id: uuid.UUID | None = None  # default: the seeded Chat Assistant
    # OneDrive/SharePoint folders this conversation's connected-file tools are
    # restricted to (max 10); empty leaves them workspace-wide.
    # None = not given: a session created in a group then inherits the group's
    # folders (R20). An explicit [] means "no folders".
    folders: list[FolderRef] | None = Field(default=None, max_length=MAX_SESSION_FOLDERS)
    # Start the session inside this group (R20/R21); 404 if not in this project.
    group_id: uuid.UUID | None = None


class SetFoldersBody(BaseModel):
    folders: list[FolderRef] = Field(default_factory=list, max_length=MAX_SESSION_FOLDERS)


async def _resolve_folders(
    db: AsyncSession, ctx: WorkspaceContext, folders: list[FolderRef]
) -> list[dict]:
    """Gate, then resolve a non-empty folder list against Graph; the status
    mapping is shared by create and PUT /folders: connection unusable or
    needing reconnect 409, a bad folder 422, Graph failing 502."""
    await require_connections_gate(db, ctx.id, USE_ACTION)
    try:
        return await resolve_session_folders(
            db, ctx.id, [{"drive_id": f.drive_id, "item_id": f.item_id} for f in folders]
        )
    except ConnectionUnavailable as exc:
        raise HTTPException(409, exc.reason) from exc
    except ConnectionAuthError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc


class SendMessageBody(BaseModel):
    text: str
    # Per-turn overrides, both optional. Omitting both leaves routing exactly as
    # before (the harness's configured default policy). `model_override` is
    # threaded onto the run's task_input exactly the way api/runs.py threads its
    # own `model_override` — an invalid/unavailable model is not validated here
    # either; it surfaces as the same RoutingUnavailable run failure runs.py
    # produces, recorded on the run and then on this turn's assistant message.
    model_override: str | None = None
    # `objective` is validated at the door (see `_validate_objective`) the same
    # way api/harnesses.py validates a harness's model_policy.objective, and is
    # persisted onto the run's task_input for the audit trail. The engine reads
    # it back out in `engine/harness.effective_model_policy`, which overlays it
    # on the harness policy for this run only — the harness row is never
    # mutated, and the routing decision records the objective that actually
    # applied.
    objective: str | None = None
    # Ids of documents already uploaded via POST /api/documents to attach to
    # this turn. The turn's run becomes attached-only for document tools;
    # later turns sent without attachments are project-wide again.
    document_ids: list[uuid.UUID] = Field(default_factory=list)


def _validate_objective(objective: str | None) -> None:
    """422 on an unrecognized objective rather than falling through to a default.

    Mirrors api/harnesses.py::_validate_policy's identical check on a harness's
    model_policy.objective: a bad value must be rejected at the door, not read
    as the default.
    """
    if objective is not None and objective not in OBJECTIVES:
        raise HTTPException(422, f"objective must be one of {'|'.join(OBJECTIVES)}")


def _run_task_input(
    text: str,
    history: list[dict],
    capabilities: str,
    model_override: str | None,
    objective: str | None,
) -> dict:
    """The task_input for one chat turn's delegated run.

    `_model_override`/`_objective` are only added when set, so a turn that
    supplies neither produces byte-for-byte the task_input this endpoint always
    built — the default behavior is unchanged.
    """
    task_input: dict = {"message": text, "_history": history, "_capabilities": capabilities}
    if model_override:
        task_input["_model_override"] = model_override
    if objective:
        task_input["_objective"] = objective
    return task_input


@router.get("")
async def list_conversations(
    archived: bool = False,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    project = await current_project(db, ctx.id)
    if project is None:
        return []
    convs = (
        (
            await db.execute(
                select(Conversation)
                .where(
                    Conversation.project_id == project.id,
                    Conversation.archived_at.is_not(None)
                    if archived
                    else Conversation.archived_at.is_(None),
                )
                .order_by(Conversation.updated_at.desc())
                .limit(100)
            )
        )
        .scalars()
        .all()
    )
    return [_conversation_out(c) for c in convs]


@router.post("")
async def create_conversation(
    body: CreateConversationBody,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    harness = None
    if body.harness_id:
        harness = await db.get(Harness, body.harness_id)
        # Archived is treated like unknown: fall back to the default chat
        # harness rather than 404. The chat composer persists its harness
        # choice in localStorage, so a stale id from a since-archived harness
        # is an expected input here, not a client bug — and POST /api/runs
        # already refuses archived harnesses, so this keeps chat consistent.
        if harness is not None and (harness.workspace_id != ctx.id or harness.is_archived):
            harness = None
    if harness is None:
        harness = (
            (
                await db.execute(
                    select(Harness)
                    .where(
                        Harness.workspace_id == ctx.id,
                        Harness.task_profile == "chat",
                        Harness.is_archived.is_(False),
                    )
                    .order_by(Harness.created_at)
                )
            )
            .scalars()
            .first()
        )
    if harness is None:
        raise HTTPException(500, "No chat harness exists")
    project = await current_project(db, ctx.id)
    if project is None:
        raise HTTPException(500, "No project exists")
    group_id: uuid.UUID | None = None
    if body.group_id is not None:
        group_id = (await _group_in_project(db, body.group_id, project.id)).id
    # Validated before the row exists, so a rejected folder creates nothing.
    if body.folders is None and group_id is not None:
        # R20: the stored entries were resolved server-side when first
        # attached, so they are copied as-is; the harness re-checks the
        # allowlist at run time.
        folders = await _group_inherited_folders(db, group_id)
    else:
        folders = await _resolve_folders(db, ctx, body.folders) if body.folders else []
    conv = Conversation(
        project_id=project.id,
        harness_id=harness.id,
        created_by=user.id,
        messages=[],
        connected_folders=folders,
        group_id=group_id,
    )
    db.add(conv)
    await db.commit()
    return _conversation_out(conv, full=True)


# --- Learning round: sessions (R20-R22) ---
#
# Groups are registered BEFORE `/{conversation_id}` so "groups" is never read
# as a conversation id.


class CreateGroupBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=200)


class PatchGroupBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=200)
    position: int | None = None


class PatchConversationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=200)
    # Omitted = untouched; explicit null = ungroup.
    group_id: uuid.UUID | None = None


def _clean_title(raw: str | None) -> str:
    title = (raw or "").strip()
    if not title:
        raise HTTPException(422, "Title must be 1-200 characters")
    return title


async def _group_in_project(db: AsyncSession, group_id: uuid.UUID, project_id: uuid.UUID) -> ConversationGroup:
    group = await db.get(ConversationGroup, group_id)
    if group is None or group.project_id != project_id:
        raise HTTPException(404, "Group not found")
    return group


async def _group_inherited_folders(db: AsyncSession, group_id: uuid.UUID) -> list[dict]:
    """The folders a new session in this group starts with: those of the most
    recently updated non-archived session in the group that has any (R20)."""
    rows = (
        await db.execute(
            select(Conversation.connected_folders)
            .where(Conversation.group_id == group_id, Conversation.archived_at.is_(None))
            .order_by(Conversation.updated_at.desc())
        )
    ).scalars().all()
    for folders in rows:
        if folders:
            return list(folders)
    return []


def _group_out(g: ConversationGroup, session_count: int, folders: list[dict]) -> dict:
    return {
        "id": str(g.id),
        "title": g.title,
        "position": g.position,
        "created_at": g.created_at.isoformat() if g.created_at else None,
        "updated_at": g.updated_at.isoformat() if g.updated_at else None,
        "session_count": session_count,
        "folders": folders,
    }


async def _project_for_groups(db: AsyncSession, ctx: WorkspaceContext) -> Project:
    project = await current_project(db, ctx.id)
    if project is None:
        raise HTTPException(500, "No project exists")
    return project


@router.get("/groups")
async def list_groups(
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    project = await current_project(db, ctx.id)
    if project is None:
        return []
    groups = (
        await db.execute(
            select(ConversationGroup)
            .where(ConversationGroup.project_id == project.id)
            .order_by(ConversationGroup.position, ConversationGroup.created_at)
        )
    ).scalars().all()
    if not groups:
        return []
    # One query for every grouped, live session (columns only, no messages).
    members = (
        await db.execute(
            select(Conversation.group_id, Conversation.connected_folders)
            .where(
                Conversation.project_id == project.id,
                Conversation.group_id.in_([g.id for g in groups]),
                Conversation.archived_at.is_(None),
            )
            .order_by(Conversation.updated_at.desc())
        )
    ).all()
    counts: dict[uuid.UUID, int] = {}
    inherited: dict[uuid.UUID, list[dict]] = {}
    for gid, folders in members:
        counts[gid] = counts.get(gid, 0) + 1
        if folders and gid not in inherited:
            inherited[gid] = list(folders)
    return [_group_out(g, counts.get(g.id, 0), inherited.get(g.id, [])) for g in groups]


@router.post("/groups")
async def create_group(
    body: CreateGroupBody,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    project = await _project_for_groups(db, ctx)
    title = "New group"
    if body.title is not None and body.title.strip():
        title = body.title.strip()
    existing = (
        await db.execute(
            select(ConversationGroup.position).where(ConversationGroup.project_id == project.id)
        )
    ).scalars().all()
    group = ConversationGroup(
        project_id=project.id,
        title=title,
        position=(max(existing) + 1) if existing else 0,
        created_by=user.id,
    )
    db.add(group)
    await db.commit()
    return _group_out(group, 0, [])


@router.patch("/groups/{group_id}")
async def patch_group(
    group_id: uuid.UUID,
    body: PatchGroupBody,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    project = await _project_for_groups(db, ctx)
    group = await _group_in_project(db, group_id, project.id)
    if "title" in body.model_fields_set:
        group.title = _clean_title(body.title)
    if "position" in body.model_fields_set and body.position is not None:
        group.position = body.position
    await db.commit()
    members = (
        await db.execute(
            select(Conversation.connected_folders)
            .where(Conversation.group_id == group.id, Conversation.archived_at.is_(None))
            .order_by(Conversation.updated_at.desc())
        )
    ).scalars().all()
    folders = next((list(f) for f in members if f), [])
    return _group_out(group, len(members), folders)


@router.delete("/groups/{group_id}", status_code=204)
async def delete_group(
    group_id: uuid.UUID,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """Delete the group only; its sessions become ungrouped (never deleted)."""
    project = await _project_for_groups(db, ctx)
    group = await _group_in_project(db, group_id, project.id)
    members = (
        await db.execute(select(Conversation).where(Conversation.group_id == group.id))
    ).scalars().all()
    for conv in members:
        conv.group_id = None
        _keep_updated_at(conv)
    await db.flush()
    await db.delete(group)
    await db.commit()
    return Response(status_code=204)


def _keep_updated_at(conv: Conversation) -> None:
    """Rail metadata edits (rename, move, archive, ungroup) are not activity:
    setting `updated_at` explicitly to its current value stops the column's
    `onupdate` from firing, so ordering and folder inheritance are untouched."""
    flag_modified(conv, "updated_at")


async def _enrich_messages(db: AsyncSession, conv: Conversation) -> list[dict]:
    """The conversation's messages with each assistant turn's live run facts
    (R01 stop_reason/truncated, R25 guardrails, R08 analysis_runs, R24
    saved_files). Two lookups for the whole conversation, never per message:
    one over its runs, one each for analysis runs and connected writes. A
    delegated child's rows are attributed to the top-level run it hangs off.
    Stored messages are copied, never mutated."""
    messages = [dict(m) for m in (conv.messages or [])]
    run_ids = {m["run_id"] for m in messages if m.get("role") == "assistant" and m.get("run_id")}
    if not run_ids:
        return messages

    run_rows = (
        await db.execute(
            select(Run.id, Run.parent_run_id, Run.root_run_id, Run.stop_reason, Run.guardrails).where(
                Run.conversation_id == conv.id
            )
        )
    ).all()
    parent_of = {str(r.id): (str(r.parent_run_id) if r.parent_run_id else None) for r in run_rows}
    root_of: dict[str, str] = {}
    for r in run_rows:
        rid = str(r.id)
        top = str(r.root_run_id) if r.root_run_id else None
        if top is None:
            top = rid
            seen = {rid}
            while parent_of.get(top) and parent_of[top] not in seen:
                seen.add(top)
                top = parent_of[top]
        root_of[rid] = top
    own = {str(r.id): r for r in run_rows}
    all_ids = [r.id for r in run_rows]

    analysis: dict[str, list[dict]] = {}
    saved: dict[str, list[dict]] = {}
    if all_ids:
        for a in (
            await db.execute(
                select(AnalysisRun)
                .where(AnalysisRun.run_id.in_(all_ids))
                .order_by(AnalysisRun.created_at, AnalysisRun.id)
            )
        ).scalars():
            top = root_of.get(str(a.run_id), str(a.run_id))
            analysis.setdefault(top, []).append(
                {
                    "id": str(a.id),
                    "purpose": a.purpose,
                    "code_sha": a.code_sha,
                    "row_count": a.row_count,
                    "status": a.status,
                }
            )
        for f in (
            await db.execute(
                select(Finding)
                .where(Finding.run_id.in_(all_ids), Finding.schema_slug == "connected_write")
                .order_by(Finding.created_at, Finding.id)
            )
        ).scalars():
            top = root_of.get(str(f.run_id), str(f.run_id))
            saved.setdefault(top, []).append(_saved_file(f))

    for m in messages:
        rid = m.get("run_id")
        if m.get("role") != "assistant" or not rid:
            continue
        run = own.get(rid)
        if run is not None:
            m["stop_reason"] = run.stop_reason
            m["guardrails"] = run.guardrails
        else:
            m.setdefault("stop_reason", None)
            m.setdefault("guardrails", None)
        m["truncated"] = m.get("stop_reason") == "max_tokens"
        m["analysis_runs"] = analysis.get(rid, [])
        m["saved_files"] = saved.get(rid, [])
    return messages


def _saved_file_status(f: Finding, upload: dict) -> str:
    """uploaded | failed | pending_approval | rejected. `failed` only when an
    upload was actually attempted and did not succeed."""
    if upload.get("status"):
        return "uploaded" if upload["status"] == "uploaded" else "failed"
    if f.status == "rejected":
        return "rejected"
    return "pending_approval"


def _saved_file(f: Finding) -> dict:
    """One `connected_write` finding as the chat chip needs it. Read
    defensively: `upload` is None until the write happens and the `audit`
    block (C1) may be absent on older rows."""
    payload = f.payload or {}
    upload = payload.get("upload") or {}
    audit = payload.get("audit") or {}
    filename = payload.get("filename")
    path = upload.get("path") or upload.get("target_path")
    if not path and filename:
        parts = [payload.get("target_path"), payload.get("relative_dir"), filename]
        path = "/".join(str(p).strip("/") for p in parts if p)
    return {
        "finding_id": str(f.id),
        "filename": filename,
        "path": path,
        "web_url": upload.get("web_url") or audit.get("web_url"),
        "status": _saved_file_status(f, upload),
        "sha256": payload.get("content_sha256") or audit.get("sha256"),
        "audit_filename": audit.get("filename"),
        "audit_web_url": audit.get("web_url"),
    }


@router.patch("/{conversation_id}")
async def patch_conversation(
    conversation_id: uuid.UUID,
    body: PatchConversationBody,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """Rename a session and/or move it between groups (R21). Omitted keys are
    left alone; `group_id: null` ungroups."""
    conv = await db.get(Conversation, conversation_id)
    if conv is None or await project_in_workspace(db, conv.project_id, ctx.id) is None:
        raise HTTPException(404, "Conversation not found")
    if "title" in body.model_fields_set:
        conv.title = _clean_title(body.title)
    if "group_id" in body.model_fields_set:
        if body.group_id is None:
            conv.group_id = None
        else:
            conv.group_id = (await _group_in_project(db, body.group_id, conv.project_id)).id
    _keep_updated_at(conv)
    await db.commit()
    return _conversation_out(conv)


async def _set_archived(
    db: AsyncSession, ctx: WorkspaceContext, conversation_id: uuid.UUID, archived: bool
) -> dict:
    conv = await db.get(Conversation, conversation_id)
    if conv is None or await project_in_workspace(db, conv.project_id, ctx.id) is None:
        raise HTTPException(404, "Conversation not found")
    conv.archived_at = datetime.now(timezone.utc) if archived else None
    _keep_updated_at(conv)
    await db.commit()
    return _conversation_out(conv)


@router.post("/{conversation_id}/archive")
async def archive_conversation(
    conversation_id: uuid.UUID,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """Archive only (R22): hidden from the default list, runs/emissions/audit
    trail untouched, restorable. There is no delete."""
    return await _set_archived(db, ctx, conversation_id, True)


@router.post("/{conversation_id}/unarchive")
async def unarchive_conversation(
    conversation_id: uuid.UUID,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    return await _set_archived(db, ctx, conversation_id, False)


@router.get("/{conversation_id}")
async def get_conversation(
    conversation_id: uuid.UUID,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    conv = await db.get(Conversation, conversation_id)
    if conv is None or await project_in_workspace(db, conv.project_id, ctx.id) is None:
        raise HTTPException(404, "Conversation not found")
    out = _conversation_out(conv, full=True)
    out["messages"] = await _enrich_messages(db, conv)
    out["active_run_id"] = await _active_run_id(db, conv.id)
    return out


@router.put("/{conversation_id}/folders")
async def set_conversation_folders(
    conversation_id: uuid.UUID,
    body: SetFoldersBody,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """Replace the OneDrive/SharePoint folders this conversation is restricted
    to. An empty list clears them and needs no connection or gate. Any
    workspace member, same as browsing."""
    conv = await db.get(Conversation, conversation_id)
    if conv is None or await project_in_workspace(db, conv.project_id, ctx.id) is None:
        raise HTTPException(404, "Conversation not found")
    conv.connected_folders = await _resolve_folders(db, ctx, body.folders) if body.folders else []
    await db.commit()
    return _conversation_out(conv, full=True)


@router.post("/{conversation_id}/messages")
async def send_message(
    conversation_id: uuid.UUID,
    body: SendMessageBody,
    request: Request,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    lifecycle.refuse_if_draining(request)
    conv = await db.get(Conversation, conversation_id)
    if conv is None or await project_in_workspace(db, conv.project_id, ctx.id) is None:
        raise HTTPException(404, "Conversation not found")
    text = body.text.strip()
    if not text:
        raise HTTPException(422, "Empty message")
    _validate_objective(body.objective)

    # Every attached document must belong to this workspace (same join as
    # api/runs.py; documents carry no workspace_id of their own). Validated
    # before the 409 check / row lock so a rejected send writes nothing.
    document_ids = list(dict.fromkeys(body.document_ids))
    attached_docs: list[Document] = []
    if document_ids:
        found = (
            await db.execute(
                select(Document)
                .join(Project, Document.project_id == Project.id)
                .where(Document.id.in_(document_ids), Project.workspace_id == ctx.id)
            )
        ).scalars().all()
        by_id = {d.id: d for d in found}
        missing = [i for i in document_ids if i not in by_id]
        if missing:
            raise HTTPException(404, f"Document not found: {', '.join(str(i) for i in missing)}")
        attached_docs = [by_id[i] for i in document_ids]

    # One reply at a time: a second send while a run is live would create a
    # second paid run (the client resumes the live one from `active_run_id`).
    # The conversation row is locked first (a no-op on sqlite) so two
    # concurrent sends serialise here instead of both passing the check; the
    # lock is released by the commit below.
    await db.execute(select(Conversation).where(Conversation.id == conv.id).with_for_update())
    active_run_id = await _active_run_id(db, conv.id)
    if active_run_id is not None:
        return JSONResponse(
            status_code=409,
            content={
                "detail": "A reply is still in progress for this conversation",
                "active_run_id": active_run_id,
            },
        )

    # Engine history: prior user/assistant turns, lean (no tool detail).
    history = [
        {"role": m["role"], "content": m["content"], "tool_calls": [], "tool_call_id": None, "meta": {}}
        for m in (conv.messages or [])[-MAX_HISTORY_TURNS:]
        if m.get("role") in ("user", "assistant") and m.get("content")
    ]

    # A pack-linked harness's primary pack loads its doctrine into this chat
    # turn — deliberate: the seeded Chat Assistant is linked to every
    # installed pack by default (`services.workspace._seed_chat_harness`), so
    # a workspace with packs resolves its primary pack here, while a
    # workspace with no packs installed still resolves to None, byte-identical
    # to before this harness could carry a pack.
    harness = await db.get(Harness, conv.harness_id)
    pack = resolve_pack_for_task(await packs_for_harness(db, harness), "chat") if harness else None

    run = Run(
        project_id=conv.project_id,
        harness_id=conv.harness_id,
        pack_id=pack.id if pack else None,
        conversation_id=conv.id,
        task_type="chat",
        task_input=_run_task_input(
            text,
            history,
            await _capability_catalog(db, ctx.id, conv.project_id),
            body.model_override,
            body.objective,
        ),
        created_by=user.id,
        document_ids=document_ids,
    )
    if conv.connected_folders:
        # Only when set: a conversation without folders produces exactly the
        # task_input it always did.
        run.task_input["_session_folders"] = conv.connected_folders
    db.add(run)

    now = datetime.now(timezone.utc).isoformat()
    user_message: dict = {"role": "user", "content": text, "run_id": None, "ts": now}
    if attached_docs:
        # Display-only: stored on the message so the UI can show attachments
        # after reload; never re-sent on later turns.
        user_message["attachments"] = [
            {"id": str(d.id), "filename": d.filename, "byte_size": d.byte_size, "content_type": d.content_type}
            for d in attached_docs
        ]
    conv.messages = [*(conv.messages or []), user_message]
    # A new message restores an archived session (R22).
    conv.archived_at = None
    if len(conv.messages) == 1:
        conv.title = text[:60] + ("…" if len(text) > 60 else "")
    await db.commit()

    task = asyncio.create_task(_execute_and_record(run.id, conv.id))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return {"run_id": str(run.id), "conversation_id": str(conv.id)}


def _own_messages(run: Run) -> list[dict]:
    """`run.messages` with the injected conversation-history prefix stripped off.

    `send_message` seeds every chat run's `task_input["_history"]` with the
    conversation's prior turns, and the engine puts that history straight onto
    the front of `run.messages` before this run's own turn begins (`history =
    [Msg.from_json(m) for m in run.task_input.get("_history", [])]` then
    `messages: list[Msg] = [*history, Msg(role="user", content=user_message)]`
    — see `engine/harness.py`). Scanning `run.messages` for "the last assistant
    text" without skipping that prefix means a run that fails before writing
    anything of its own surfaces the *previous* turn's reply as if it were
    this run's own (seen on cloud: runs 3ccb2fd2, 8474f908).

    The skip count starts at `len(_history)` but is reduced by whatever the
    engine's own context-budget trim dropped off the front of that history
    before it ever reached `run.messages` (`engine/compaction.py::
    trim_history`, recorded on `run.compactions` as `{"kind": "history_trim",
    "dropped_history_turns": N}`) — otherwise a trimmed run would over-skip
    into its own turn's messages.
    """
    history_len = len(run.task_input.get("_history") or [])
    for record in run.compactions or []:
        if record.get("kind") == "history_trim":
            history_len -= record.get("dropped_history_turns", 0)
    history_len = max(history_len, 0)
    return (run.messages or [])[history_len:]


def _tool_result_summary(result: dict | None) -> str:
    """Read a tool call's own result and say, in the activity pill, when it
    came back empty — a lookup or search that silently found nothing used to
    render exactly like one that found something (A2)."""
    if result is None:
        return ""
    if (result.get("meta") or {}).get("error"):
        return "error"
    content = result.get("content") or ""
    if content.startswith("No rows in"):
        return "no rows matched"
    if content.startswith("No matches") or content.startswith("No connected-source matches"):
        return "no matches"
    return ""


def _assistant_message(run: Run) -> dict:
    """The persisted assistant turn: text/activity, plus the full cost, carbon
    and routing record behind it.

    Same shape as a run summary/detail (api/runs.py::_run_summary, get_run) so a
    chat turn is as legible as the run behind it — the compact chip reads the
    top-line fields (via `emission_summary_fields`) and the expanded view reuses
    `energy`/`routing` verbatim with the same `EmissionsCalc`/`RoutingBadge`
    components a run detail page uses. Nullable fields stay null, never 0, when
    the run has no estimate or was never routed.
    """
    own_messages = _own_messages(run)
    tool_results = {
        m.get("tool_call_id"): m for m in own_messages if m.get("role") == "tool"
    }
    assistant_text = ""
    activity = []
    for m in own_messages:
        if m.get("role") == "assistant":
            if m.get("content"):
                assistant_text = m["content"]  # last assistant text wins
            for tc in m.get("tool_calls") or []:
                entry = {"tool": tc.get("name"), "summary": ""}
                result = tool_results.get(tc.get("id"))
                if tc.get("name") in DELEGATION_TOOLS and _tool_result_summary(result) == "error":
                    # The call was refused (e.g. `delegate_parallel`'s "at most
                    # 3 tasks" cap) before any child run started — summarizing
                    # from the call's own arguments, as below, would read like
                    # the delegation happened. Same error detection
                    # `_tool_result_summary` uses, so this never drifts from
                    # what an ordinary tool's activity pill calls an error.
                    content = (result or {}).get("content") or ""
                    if content.startswith("Tool error: "):
                        content = content[len("Tool error: ") :]
                    entry["summary"] = f"refused: {content[:100]}"
                elif tc.get("name") in DELEGATION_TOOLS:
                    args = tc.get("arguments") or {}
                    if tc.get("name") == "delegate_parallel":
                        batch = args.get("tasks") or []
                        # De-duplicated, in call order — a batch that repeats
                        # a task_type over several inputs should read as one
                        # kind of work, not a wall of the same word. A
                        # subagent item has no task_type of its own, so it
                        # reads as the literal word "subagent" instead.
                        types: list[str] = []
                        for t in batch:
                            slug = "subagent" if t.get("kind") == "subagent" else t.get("task_type", "?")
                            if slug not in types:
                                types.append(slug)
                        shown = ", ".join(types[:4]) + ("…" if len(types) > 4 else "")
                        entry["summary"] = f"delegated {len(batch)} tasks in parallel: {shown}"
                    elif tc.get("name") == "spawn_subagent":
                        brief = args.get("label") or (args.get("instructions") or "")[:60]
                        entry["summary"] = f"briefed a subagent: {brief}"
                    else:
                        entry["summary"] = f"delegated {args.get('task_type', '?')}"
                else:
                    entry["summary"] = _tool_result_summary(result)
                if tc.get("name") == "start_batch" and result is not None:
                    # The chat's batch card is built from this call's result
                    # JSON (`batch_id`); no other tool's result is persisted here.
                    content = result.get("content")
                    entry["result"] = (content if isinstance(content, str) else "")[:2000]
                activity.append(entry)
    if run.status == "completed_without_output" and not assistant_text:
        # The engine's empty-reply guard: the model finished its tool calls and
        # never wrote a reply, even after a nudge. Say that in the person's
        # terms, not the status enum's.
        assistant_text = "(The model returned no reply. Try sending the message again.)"
    elif run.status != "completed" and not assistant_text:
        assistant_text = f"(run {run.status}: {run.error or 'no output'})"
    return {
        "role": "assistant",
        "content": assistant_text,
        "run_id": str(run.id),
        "ts": datetime.now(timezone.utc).isoformat(),
        "activity": activity,
        "status": run.status,
        "model_used": run.model_used,
        "cost_usd": float(run.cost_usd or 0),
        "input_tokens": run.input_tokens,
        "output_tokens": run.output_tokens,
        "cache_read_tokens": run.cache_read_tokens,
        "cache_write_tokens": run.cache_write_tokens,
        "energy_wh": energy_wh_field(run.energy_wh),
        # co2e_g, scope2_g, scope3_g, avoided_co2e_g — read as recorded, never
        # recomputed at today's factors; null wherever the run has no figure.
        **emission_summary_fields(run.energy_accounting),
        # Full derivation (scope split, frontier-baseline counterfactual, every
        # factor) and the full routing decision (chosen model, objective,
        # reasoning, fallback_used, candidates), exactly as recorded. Both null
        # when the run never estimated/routed.
        "energy": run.energy_accounting,
        "routing": public_routing(run.routing),
        # Parallel preview under the revised emissions method; null on older runs.
        "method_v3": method_v3_of(run.routing),
        # The prose grounding check's verdict on this reply (engine/
        # grounding.py) — null for anything the check does not apply to.
        "grounding": run.grounding,
        # R01/R25: why the run stopped short and what its guardrails did.
        # Persisted here and refreshed from the run on every GET.
        "stop_reason": run.stop_reason,
        "truncated": run.stop_reason == "max_tokens",
        "guardrails": run.guardrails,
    }


async def _execute_and_record(run_id: uuid.UUID, conversation_id: uuid.UUID) -> None:
    """Run the chat turn, then append the assistant entry to the conversation."""
    await get_harness_engine().execute(run_id)
    async with get_session_factory()() as db:
        run = await db.get(Run, run_id)
        conv = await db.get(Conversation, conversation_id)
        if run is None or conv is None:
            return
        conv.messages = [*(conv.messages or []), _assistant_message(run)]
        await db.commit()
