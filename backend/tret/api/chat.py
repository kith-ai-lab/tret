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
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tret.api.auth import current_user
from tret.api.workspace import WorkspaceContext, current_project, current_workspace, project_in_workspace
from tret.db.engine import get_db, get_session_factory
from tret.db.models import Conversation, Dataset, Harness, Pack, Run, User
from tret.engine.harness import get_harness_engine
from tret.engine.tools import DELEGATION_TOOLS
from tret.packs.links import pack_map_for_harnesses, packs_for_harness, resolve_pack_for_task
from tret.router_llm.objectives import OBJECTIVES
from tret.services import lifecycle
from tret.services.emissions import emission_summary_fields, energy_wh_field

router = APIRouter(prefix="/api/chat", tags=["chat"])

_background_tasks: set[asyncio.Task] = set()

MAX_HISTORY_TURNS = 30


def _conversation_out(c: Conversation, full: bool = False) -> dict:
    out = {
        "id": str(c.id),
        "title": c.title,
        "harness_id": str(c.harness_id),
        "message_count": len(c.messages or []),
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
    }
    if full:
        out["messages"] = c.messages
    return out


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


class CreateConversationBody(BaseModel):
    harness_id: uuid.UUID | None = None  # default: the seeded Chat Assistant


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
                .where(Conversation.project_id == project.id)
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
    conv = Conversation(
        project_id=project.id, harness_id=harness.id, created_by=user.id, messages=[]
    )
    db.add(conv)
    await db.commit()
    return _conversation_out(conv, full=True)


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
    )
    db.add(run)

    now = datetime.now(timezone.utc).isoformat()
    conv.messages = [*(conv.messages or []), {"role": "user", "content": text, "run_id": None, "ts": now}]
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
        "routing": run.routing,
        # The prose grounding check's verdict on this reply (engine/
        # grounding.py) — null for anything the check does not apply to.
        "grounding": run.grounding,
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
