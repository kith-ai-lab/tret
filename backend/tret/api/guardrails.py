"""`GET`/`PUT /api/guardrails` — the guardrails panel (R25).

Any workspace member can read the catalog (locked rules, member settings,
operator state, 30-day repair overhead). Only a workspace admin or owner can
change the member settings, which are stored on
`Workspace.settings["guardrails"]` and read by the run loop through
`services/guardrails.resolve_guardrails`.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tret.api.workspace import WorkspaceContext, current_workspace, require_workspace_admin
from tret.config import get_settings
from tret.db.engine import get_db
from tret.db.models import Harness, Project, Run, Workspace, utcnow
from tret.services.guardrails import (
    OVERHEAD_WINDOW_DAYS,
    GuardrailValidationError,
    aggregate_overhead,
    apply_update,
    build_catalog,
    validate_update,
)

router = APIRouter(prefix="/api", tags=["guardrails"])

#: Most runs read for the overhead roll-up.
_OVERHEAD_SCAN_LIMIT = 5000


async def _overhead(db: AsyncSession, workspace_id) -> dict | None:
    since = utcnow() - timedelta(days=OVERHEAD_WINDOW_DAYS)
    q = (
        select(Run.guardrails, Run.input_tokens, Run.output_tokens, Run.cost_usd, Run.energy_wh)
        .where(
            Run.project_id.in_(select(Project.id).where(Project.workspace_id == workspace_id)),
            Run.created_at >= since,
        )
        .order_by(Run.created_at.desc())
        .limit(_OVERHEAD_SCAN_LIMIT)
    )
    rows = [
        {
            "guardrails": g,
            "input_tokens": tin,
            "output_tokens": tout,
            "cost_usd": cost,
            "energy_wh": wh,
        }
        for g, tin, tout, cost, wh in (await db.execute(q)).all()
        if g is not None
    ]
    return aggregate_overhead(rows)


async def _output_limit(db: AsyncSession, workspace_id) -> int | None:
    """The workspace chat harness's `max_output_tokens`, when it has one."""
    loop_config = (
        await db.execute(
            select(Harness.loop_config)
            .where(Harness.workspace_id == workspace_id, Harness.task_profile == "chat")
            .order_by(Harness.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()
    value = (loop_config or {}).get("max_output_tokens") if isinstance(loop_config, dict) else None
    return int(value) if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


async def _view(db: AsyncSession, workspace_id, workspace_settings: dict | None) -> dict[str, Any]:
    return build_catalog(
        get_settings(),
        workspace_settings,
        overhead=await _overhead(db, workspace_id),
        output_limit=await _output_limit(db, workspace_id),
    )


@router.get("/guardrails")
async def get_guardrails(
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    return await _view(db, ctx.id, ctx.workspace.settings)


@router.put("/guardrails")
async def put_guardrails(
    body: Any = Body(...),
    ctx: WorkspaceContext = Depends(require_workspace_admin),
    db: AsyncSession = Depends(get_db),
):
    try:
        update = validate_update(body, get_settings())
    except GuardrailValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors) from exc
    workspace = await db.get(Workspace, ctx.id)
    # Reassigned, not mutated in place: SQLAlchemy only detects a change to a
    # JSONB column on assignment.
    settings = dict(workspace.settings or {})
    settings["guardrails"] = apply_update(settings, update)
    workspace.settings = settings
    await db.commit()
    return await _view(db, ctx.id, settings)
