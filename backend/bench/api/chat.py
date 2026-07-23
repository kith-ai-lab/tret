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

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bench.api.auth import current_user
from bench.db.engine import get_db, get_session_factory
from bench.db.models import Conversation, Dataset, Harness, Pack, Project, Run, User
from bench.engine.harness import get_harness_engine

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


async def _capability_catalog(db: AsyncSession) -> str:
    """Snapshot of installed pack task types, persisted with each chat run."""
    harnesses = (
        (await db.execute(select(Harness).where(Harness.is_archived.is_(False)))).scalars().all()
    )
    packs = {p.id: p for p in (await db.execute(select(Pack))).scalars().all()}
    lines = ["## Capability catalog (for run_harness_task)"]
    seen = set()
    for h in harnesses:
        pack = packs.get(h.pack_id)
        if pack is None:
            continue
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

    method_lines: list[str] = []
    for p in packs.values():
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

    datasets = (await db.execute(select(Dataset).order_by(Dataset.name))).scalars().all()
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


@router.get("")
async def list_conversations(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    convs = (
        (await db.execute(select(Conversation).order_by(Conversation.updated_at.desc()).limit(100)))
        .scalars()
        .all()
    )
    return [_conversation_out(c) for c in convs]


@router.post("")
async def create_conversation(
    body: CreateConversationBody,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    harness = None
    if body.harness_id:
        harness = await db.get(Harness, body.harness_id)
    if harness is None:
        harness = (
            (
                await db.execute(
                    select(Harness).where(
                        Harness.task_profile == "chat", Harness.is_archived.is_(False)
                    )
                )
            )
            .scalars()
            .first()
        )
    if harness is None:
        raise HTTPException(500, "No chat harness exists")
    project = (await db.execute(select(Project))).scalars().first()
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
    db: AsyncSession = Depends(get_db),
):
    conv = await db.get(Conversation, conversation_id)
    if conv is None:
        raise HTTPException(404, "Conversation not found")
    return _conversation_out(conv, full=True)


@router.post("/{conversation_id}/messages")
async def send_message(
    conversation_id: uuid.UUID,
    body: SendMessageBody,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    conv = await db.get(Conversation, conversation_id)
    if conv is None:
        raise HTTPException(404, "Conversation not found")
    text = body.text.strip()
    if not text:
        raise HTTPException(422, "Empty message")

    # Engine history: prior user/assistant turns, lean (no tool detail).
    history = [
        {"role": m["role"], "content": m["content"], "tool_calls": [], "tool_call_id": None, "meta": {}}
        for m in (conv.messages or [])[-MAX_HISTORY_TURNS:]
        if m.get("role") in ("user", "assistant") and m.get("content")
    ]

    run = Run(
        project_id=conv.project_id,
        harness_id=conv.harness_id,
        pack_id=None,
        task_type="chat",
        task_input={
            "message": text,
            "_history": history,
            "_capabilities": await _capability_catalog(db),
        },
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


async def _execute_and_record(run_id: uuid.UUID, conversation_id: uuid.UUID) -> None:
    """Run the chat turn, then append the assistant entry to the conversation."""
    await get_harness_engine().execute(run_id)
    async with get_session_factory()() as db:
        run = await db.get(Run, run_id)
        conv = await db.get(Conversation, conversation_id)
        if run is None or conv is None:
            return
        assistant_text = ""
        activity = []
        for m in run.messages or []:
            if m.get("role") == "assistant":
                if m.get("content"):
                    assistant_text = m["content"]  # last assistant text wins
                for tc in m.get("tool_calls") or []:
                    entry = {"tool": tc.get("name"), "summary": ""}
                    if tc.get("name") == "run_harness_task":
                        args = tc.get("arguments") or {}
                        entry["summary"] = f"delegated {args.get('task_type', '?')}"
                    activity.append(entry)
        if run.status != "completed" and not assistant_text:
            assistant_text = f"(run {run.status}: {run.error or 'no output'})"
        conv.messages = [
            *(conv.messages or []),
            {
                "role": "assistant",
                "content": assistant_text,
                "run_id": str(run.id),
                "ts": datetime.now(timezone.utc).isoformat(),
                "activity": activity,
                "status": run.status,
                "model_used": run.model_used,
                "cost_usd": float(run.cost_usd or 0),
            },
        ]
        await db.commit()
