from __future__ import annotations

import asyncio
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bench.api.auth import current_user
from bench.db.engine import get_db
from bench.db.models import Harness, Project, Run, User
from bench.engine.events import get_event_bus
from bench.engine.harness import get_harness_engine

router = APIRouter(prefix="/api/runs", tags=["runs"])

_background_tasks: set[asyncio.Task] = set()


class CreateRunBody(BaseModel):
    harness_id: uuid.UUID
    project_id: uuid.UUID | None = None
    task_type: str = "freeform"
    task_input: dict = Field(default_factory=dict)
    document_ids: list[uuid.UUID] = Field(default_factory=list)
    model_override: str | None = None


def _run_summary(run: Run) -> dict:
    return {
        "id": str(run.id),
        "project_id": str(run.project_id),
        "harness_id": str(run.harness_id),
        "task_type": run.task_type,
        "status": run.status,
        "model_used": run.model_used,
        "provider_used": run.provider_used,
        "routing": run.routing,
        "input_tokens": run.input_tokens,
        "output_tokens": run.output_tokens,
        "cost_usd": float(run.cost_usd or 0),
        "iterations": run.iterations,
        "error": run.error,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }


@router.post("")
async def create_run(
    body: CreateRunBody, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    harness = await db.get(Harness, body.harness_id)
    if harness is None or harness.is_archived:
        raise HTTPException(404, "Harness not found")
    project_id = body.project_id
    if project_id is None:
        project = (await db.execute(select(Project))).scalars().first()
        if project is None:
            raise HTTPException(500, "No project exists")
        project_id = project.id

    task_input = dict(body.task_input)
    if body.model_override:
        task_input["_model_override"] = body.model_override

    run = Run(
        project_id=project_id,
        harness_id=harness.id,
        pack_id=harness.pack_id,
        task_type=body.task_type,
        task_input=task_input,
        document_ids=body.document_ids,
        created_by=user.id,
    )
    db.add(run)
    await db.commit()

    engine = get_harness_engine()
    task = asyncio.create_task(engine.execute(run.id))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return {"run_id": str(run.id)}


@router.get("")
async def list_runs(
    limit: int = 50, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    runs = (
        (await db.execute(select(Run).order_by(Run.created_at.desc()).limit(min(limit, 200))))
        .scalars()
        .all()
    )
    return [_run_summary(r) for r in runs]


@router.get("/{run_id}")
async def get_run(
    run_id: uuid.UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    run = await db.get(Run, run_id)
    if run is None:
        raise HTTPException(404, "Run not found")
    return {**_run_summary(run), "task_input": run.task_input, "messages": run.messages,
            "document_ids": [str(d) for d in (run.document_ids or [])],
            "doctrine_sha": run.doctrine_sha}


@router.get("/method-runs/{method_run_id}")
async def get_method_run(
    method_run_id: uuid.UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    """Resolve a method/<slug>/<id> citation to its full execution manifest."""
    from bench.db.models import MethodRun

    mr = await db.get(MethodRun, method_run_id)
    if mr is None:
        raise HTTPException(404, "Method run not found")
    return {
        "id": str(mr.id),
        "method_slug": mr.method_slug,
        "run_id": str(mr.run_id) if mr.run_id else None,
        "params": mr.params,
        "code_sha": mr.code_sha,
        "input_summary": mr.input_summary,
        "output_hash": mr.output_hash,
        "row_count": mr.row_count,
        "duration_ms": mr.duration_ms,
        "status": mr.status,
        "error": mr.error,
        "output": mr.output,
        "created_at": mr.created_at.isoformat() if mr.created_at else None,
    }


@router.get("/{run_id}/events")
async def run_events(run_id: uuid.UUID, user: User = Depends(current_user)):
    bus = get_event_bus()

    async def stream():
        async for event in bus.subscribe(run_id):
            yield event.to_sse()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/{run_id}/cancel")
async def cancel_run(
    run_id: uuid.UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    run = await db.get(Run, run_id)
    if run is None:
        raise HTTPException(404, "Run not found")
    get_harness_engine().cancel(run_id)
    return {"ok": True}
