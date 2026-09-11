"""`GET/PUT/DELETE /api/workspace/settings/budget` — a workspace's own period
spend budget: a soft, informational cap plus alert thresholds on top of the
per-run cost cap the engine already enforces.

Same convention as its sibling `api/emissions_settings.py`: read is open to
any member (`current_workspace`), write requires admin/owner
(`require_workspace_admin`) — "same role gating as other workspace
settings". Enforcement itself is `tret/services/budgets.py`'s
`budget_pre_run_gate`, registered at startup (`engine/extensions.py::
load_extensions`); this router only reads and writes the configuration and
reports the same live status that gate computes.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from tret.api.workspace import WorkspaceContext, current_workspace, require_workspace_admin
from tret.db.engine import get_db
from tret.db.models import Workspace, utcnow
from tret.services.budgets import (
    BUDGET_SETTINGS_KEY,
    BUDGET_STATE_KEY,
    BudgetSettings,
    budget_status,
    window_bounds,
)
from tret.services.emission_settings import validation_detail as _validation_detail
from tret.services.workspace import validate_budget_settings

router = APIRouter(prefix="/api", tags=["budgets"])

log = logging.getLogger("tret.budgets_api")


def _degraded_budget(config: dict | None) -> dict | None:
    """The fallback view when `budget_status` raises after we already know
    `config` (a stored or just-validated `BudgetSettings` document) is
    configured — `period_spend`'s DB query hiccupping is the likely cause.
    Returns the stored config with the live spend/window fields nulled out
    rather than letting the caller 500: a PUT has already committed its new
    config by the time it calls this, and a spend-query failure must never
    make that write look like it failed.

    `None` in, `None` out — mirrors `budget_status`'s own "no budget
    configured" case — and a `config` that no longer validates degrades the
    same way rather than raising a second exception on top of the first.
    """
    if not config:
        return None
    try:
        budget = BudgetSettings(**config)
    except Exception:
        log.warning("stored budget document no longer validates while degrading", exc_info=True)
        return None
    window_start, window_end = window_bounds(budget.period, utcnow())
    return {
        "period": budget.period,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "cap_usd": budget.cap_usd,
        "alerts": list(budget.alerts),
        "spent_usd": None,
        "remaining_usd": None,
        "fraction": None,
        "alerts_crossed": None,
    }


async def _budget_status_or_degraded(
    db: AsyncSession, workspace_id, config: dict | None
) -> dict | None:
    """`budget_status()`, degrading to `_degraded_budget(config)` instead of
    propagating if it raises. `budget_status` itself already fails open to
    `None` for a workspace that doesn't exist or a stored document that
    doesn't validate — the case this guards against is `period_spend`'s own
    query blowing up, which `budget_status` does not catch.
    """
    try:
        return await budget_status(db, workspace_id, utcnow())
    except Exception:
        log.exception("budget_status failed for workspace %s; returning config only", workspace_id)
        return _degraded_budget(config)


@router.get("/workspace/settings/budget")
async def get_budget_settings(
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    try:
        return {"budget": await budget_status(db, ctx.id, utcnow())}
    except Exception:
        log.exception("budget_status failed for workspace %s; returning config only", ctx.id)
        workspace = await db.get(Workspace, ctx.id)
        raw = (workspace.settings or {}).get(BUDGET_SETTINGS_KEY) if workspace is not None else None
        return {"budget": _degraded_budget(raw if isinstance(raw, dict) else None)}


@router.put("/workspace/settings/budget")
async def put_budget_settings(
    body: dict = Body(default_factory=dict),
    ctx: WorkspaceContext = Depends(require_workspace_admin),
    db: AsyncSession = Depends(get_db),
):
    try:
        validated = validate_budget_settings(body)
    except ValidationError as exc:
        raise HTTPException(422, detail=_validation_detail(exc)) from exc

    workspace = await db.get(Workspace, ctx.id)
    # Reassigned, not mutated in place — see emissions_settings.py's identical
    # note: SQLAlchemy only detects a JSONB column change on assignment.
    settings = dict(workspace.settings or {})
    settings[BUDGET_SETTINGS_KEY] = validated
    # A new cap or period starts a fresh alert window immediately, so an
    # edit doesn't leave a stale "already alerted at 100%" marker from
    # before the change sitting against the new configuration.
    settings.pop(BUDGET_STATE_KEY, None)
    workspace.settings = settings
    await db.commit()

    # The write already landed: a failure computing the live status below
    # (period_spend's own query, most likely) must not turn a successful PUT
    # into a 500 — see `_budget_status_or_degraded`.
    return {"budget": await _budget_status_or_degraded(db, ctx.id, validated)}


@router.delete("/workspace/settings/budget")
async def delete_budget_settings(
    ctx: WorkspaceContext = Depends(require_workspace_admin),
    db: AsyncSession = Depends(get_db),
):
    workspace = await db.get(Workspace, ctx.id)
    settings = dict(workspace.settings or {})
    settings.pop(BUDGET_SETTINGS_KEY, None)
    settings.pop(BUDGET_STATE_KEY, None)
    workspace.settings = settings
    await db.commit()
    return {"budget": None}
