"""Per-workspace period spend budgets: an operator-set, informational cap on
top of the per-run cost cap the engine already enforces (`loop_config.
max_cost_usd`, see `engine/harness.py`).

A budget is a `Workspace.settings["budget"]` document —
`{"period": "daily"|"weekly"|"monthly", "cap_usd": <number > 0>,
"alerts": [<fraction>, ...]}` — validated on write by
`services/workspace.py::validate_budget_settings` against `BudgetSettings`
below. Absent (no `"budget"` key, or an empty dict) means no budget: every
function here treats that as "nothing to enforce or report", never as a zero
cap.

Three things live here:

  * `period_spend` / `budget_status` — read-only: what has this workspace
    spent in its current window, and how does that compare to its cap.
  * `budget_pre_run_gate` — a core `PreRunGate` (see `engine/extensions.py`)
    that refuses a new run once spend plus that run's own `max_cost_usd`
    reservation would exceed the cap. Registered by `load_extensions`
    (`engine/extensions.py`) ahead of any proprietary extension's own gate
    (e.g. a hosting extension's credit hold), so a deployment always has both checks
    in the same order. Explicitly a **soft** reservation: two runs racing
    past the same gate can both be admitted and both spend, exactly like the
    seat-limit gates in `services/workspace.py` — a hosted deployment's own
    credit hold is the hard limit; this is early warning, not enforcement
    with teeth.
  * `budget_alert_post_run_hook` — a core `PostRunHook` that, when a
    finished run pushes the workspace's fraction-of-cap past an alert
    threshold not yet crossed this window, logs a WARNING and publishes a
    `budget_alert` event. Which thresholds already fired for the current
    window is tracked in `Workspace.settings["budget_state"]`
    (`{"window_start": <iso>, "alerted": [<fraction>, ...]}`) so each fires
    once per threshold per period and resets when the window rolls over.

Both the gate and the hook follow the fail-open, own-exception-handling
convention every other extension seam in `engine/extensions.py` documents:
`check_pre_run`/`run_post_run_hooks` already catch and log whatever a
registered gate/hook raises, but each function here additionally guards its
own body — a DB error must never take a run down with it, and a unit test
should be able to exercise that without going through the registry.

No workspace-level event bus exists (`engine/events.py`'s `RunEventBus` is
keyed by run id, for SSE streaming to whoever is watching one run) — a
`budget_alert` is published on the *finishing run's own* id, the same bus and
mechanism `engine/harness.py` already uses for its own per-run
`budget_warning` event, rather than inventing a second channel.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tret.db.models import Harness, Project, Run, Workspace, utcnow
from tret.engine.events import RunEvent, get_event_bus
from tret.engine.extensions import GateResult

log = logging.getLogger("tret.budgets")

BUDGET_PERIODS = ("daily", "weekly", "monthly")
DEFAULT_ALERT_THRESHOLDS: list[float] = [0.5, 0.75, 1.0]

# Keys inside `Workspace.settings`.
BUDGET_SETTINGS_KEY = "budget"
BUDGET_STATE_KEY = "budget_state"

# A run counts toward spend once it has actually run — `queued` has accrued
# nothing and has no `started_at` yet. `running` contributes its cost so far;
# every terminal status contributes its final cost, failed and cancelled
# runs included, because they still spent money.
FINISHED_STATUSES = ("completed", "completed_without_output", "failed", "cancelled")


class BudgetSettings(BaseModel):
    """The shape of `Workspace.settings["budget"]`. `services/workspace.py::
    validate_budget_settings` is the actual write-path validator; this is the
    schema it validates against, kept here alongside the query/gate/alert
    logic that reads the same shape back."""

    period: Literal["daily", "weekly", "monthly"]
    cap_usd: float
    alerts: list[float] = Field(default_factory=lambda: list(DEFAULT_ALERT_THRESHOLDS))

    @field_validator("cap_usd")
    @classmethod
    def _cap_positive(cls, v: float) -> float:
        if not (v > 0):
            raise ValueError("cap_usd must be greater than 0")
        return v

    @field_validator("alerts")
    @classmethod
    def _alerts_valid(cls, v: list[float]) -> list[float]:
        if not v:
            raise ValueError("alerts must not be empty")
        for a in v:
            if not (0 < a <= 1):
                raise ValueError("alerts: each threshold must be greater than 0 and at most 1")
        return sorted(set(v))


def window_bounds(period: str, now: datetime) -> tuple[datetime, datetime]:
    """[window_start, window_end) for `period`, containing `now` — UTC
    throughout. Daily is the calendar day; weekly is the ISO week (Monday
    00:00 to the following Monday 00:00); monthly is the calendar month.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "daily":
        return day_start, day_start + timedelta(days=1)
    if period == "weekly":
        start = day_start - timedelta(days=now.weekday())  # Monday=0
        return start, start + timedelta(days=7)
    if period == "monthly":
        start = day_start.replace(day=1)
        end = (
            start.replace(year=start.year + 1, month=1)
            if start.month == 12
            else start.replace(month=start.month + 1)
        )
        return start, end
    raise ValueError(f"period must be one of {BUDGET_PERIODS}, got {period!r}")


async def period_spend(
    db: AsyncSession, workspace_id: uuid.UUID, period: str, now: datetime
) -> Decimal:
    """Sum of this workspace's spend in `period`'s current window (the one
    containing `now`): `coalesce(reported_cost_usd, cost_usd)` for every run
    that finished in the window, plus `cost_usd` accrued so far by any run
    still `running` that started in it. Rows are fetched and summed in
    Python (not a SQL aggregate) so this behaves identically on sqlite and
    Postgres regardless of either dialect's own `Numeric` handling.
    """
    window_start, window_end = window_bounds(period, now)
    rows = (
        await db.execute(
            select(Run.status, Run.cost_usd, Run.reported_cost_usd)
            .select_from(Run)
            .join(Project, Project.id == Run.project_id)
            .where(
                Project.workspace_id == workspace_id,
                Run.started_at.is_not(None),
                Run.started_at >= window_start,
                Run.started_at < window_end,
                Run.status.in_((*FINISHED_STATUSES, "running")),
            )
        )
    ).all()

    total = Decimal("0")
    for status, cost_usd, reported_cost_usd in rows:
        cost = Decimal(str(cost_usd)) if cost_usd is not None else Decimal("0")
        if status == "running":
            total += cost
            continue
        reported = Decimal(str(reported_cost_usd)) if reported_cost_usd is not None else None
        total += reported if reported is not None else cost
    return total


def _budget_doc(workspace: Workspace) -> dict | None:
    doc = (workspace.settings or {}).get(BUDGET_SETTINGS_KEY)
    return doc if isinstance(doc, dict) and doc else None


async def budget_status(db: AsyncSession, workspace_id: uuid.UUID, now: datetime) -> dict | None:
    """The live view of a workspace's budget — `None` when none is
    configured (or the workspace does not exist). Never raises on a bad
    stored document (an operator hand-editing `Workspace.settings`, or a
    schema this version no longer accepts): fails open to `None`, the same
    "no budget" a caller who never set one sees, rather than 500ing a
    read-only status check.
    """
    workspace = await db.get(Workspace, workspace_id)
    if workspace is None:
        return None
    raw = _budget_doc(workspace)
    if raw is None:
        return None
    try:
        budget = BudgetSettings(**raw)
    except Exception:
        log.warning("workspace %s has an invalid stored budget document; ignoring", workspace_id)
        return None

    window_start, window_end = window_bounds(budget.period, now)
    cap = Decimal(str(budget.cap_usd))
    spent = await period_spend(db, workspace_id, budget.period, now)
    fraction = float(spent / cap) if cap > 0 else 0.0
    alerts_crossed = [a for a in budget.alerts if fraction >= a]
    return {
        "period": budget.period,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "cap_usd": float(cap),
        "alerts": list(budget.alerts),
        "spent_usd": float(spent),
        "remaining_usd": float(cap - spent),
        "fraction": fraction,
        "alerts_crossed": alerts_crossed,
    }


async def budget_pre_run_gate(db: AsyncSession, run: Run, workspace_id: uuid.UUID) -> GateResult:
    """Core's own `PreRunGate` (`engine/extensions.py`): refuse a new run
    once this workspace's current-period spend, plus the run's own
    `max_cost_usd` reservation, would exceed its configured cap. A soft
    reservation, not a hold — see the module docstring — and a no-op
    (`allowed=True`) whenever no budget is configured, the workspace can't be
    found, or anything about the check itself fails; this must never be the
    thing that takes a run down.

    Three-way check, in order:

      1. Spend alone has already reached the cap: refuse outright
         (`"exhausted"`) regardless of this run's own reservation.
      2. Otherwise, a reservation no bigger than the cap itself would still
         push spend over it: refuse (`"would exceed"`) — the ordinary soft
         reservation case.
      3. Otherwise, if the run's own reservation is *larger than the cap*,
         the reservation is meaningless for this check (every run would
         always "exceed" a cap smaller than its own reservation, bricking
         any cap below `DEFAULT_MAX_COST_USD` — core never writes
         `max_cost_usd` on a seeded harness, so that default is what every
         chat turn reserves): allow on spend alone and say so in `detail`.

    A cap under the per-run default (a $5 or smaller cap with a harness that
    never sets its own `max_cost_usd`, most commonly) would otherwise refuse
    every run before it starts and never let the last slice of the cap be
    spent — case 3 exists so a cap like that still enforces on actual spend
    instead of bricking outright.
    """
    try:
        workspace = await db.get(Workspace, workspace_id)
        if workspace is None:
            return GateResult(allowed=True)
        raw = _budget_doc(workspace)
        if raw is None:
            return GateResult(allowed=True)
        budget = BudgetSettings(**raw)

        now = utcnow()
        spent = await period_spend(db, workspace_id, budget.period, now)
        cap = Decimal(str(budget.cap_usd))

        harness = await db.get(Harness, run.harness_id)
        loop_cfg = (harness.loop_config or {}) if harness is not None else {}
        # Imported lazily: this is the one place `services/budgets` needs the
        # engine's own default, and a module-level import would pull the
        # whole engine package into every caller of `services/workspace`
        # (which imports `BudgetSettings` from this module at import time,
        # for its own write-path validation) — see the module docstring's
        # import-edge note.
        from tret.engine.harness import DEFAULT_MAX_COST_USD

        max_cost = Decimal(str(loop_cfg.get("max_cost_usd", DEFAULT_MAX_COST_USD)))

        if spent >= cap:
            return GateResult(
                allowed=False,
                reason="budget_exhausted",
                detail=(
                    f"This workspace's {budget.period} spend budget is exhausted: "
                    f"${spent:.2f} spent against a ${cap:.2f} cap. This is a soft, "
                    "informational reservation, not a hard hold — a hosted deployment's "
                    "own credit hold, if any, is the hard limit."
                ),
            )
        if max_cost <= cap and spent + max_cost > cap:
            return GateResult(
                allowed=False,
                reason="budget_exhausted",
                detail=(
                    f"This workspace's {budget.period} spend budget would exceed: "
                    f"${spent:.2f} spent plus a ${max_cost:.2f} reservation for this run "
                    f"against a ${cap:.2f} cap. This is a soft, informational reservation, "
                    "not a hard hold — a hosted deployment's own credit hold, if any, is "
                    "the hard limit."
                ),
            )
        if max_cost > cap:
            # The run's own reservation alone is bigger than the whole cap —
            # refusing here would brick every run against this cap forever,
            # reservation or not. Allow on spend alone; the cap still
            # enforces once actual spend (not a reservation) reaches it.
            return GateResult(
                allowed=True,
                detail=(
                    f"This run's ${max_cost:.2f} max_cost_usd reservation exceeds the "
                    f"${cap:.2f} {budget.period} cap, so the reservation is meaningless "
                    f"here; allowed on ${spent:.2f} spent alone."
                ),
            )
        return GateResult(allowed=True)
    except Exception:
        log.exception("budget pre-run gate raised for workspace %s; failing open", workspace_id)
        return GateResult(allowed=True)


async def budget_alert_post_run_hook(db: AsyncSession, run: Run, workspace_id: uuid.UUID) -> None:
    """Core's own `PostRunHook` (`engine/extensions.py`): when this run's
    finish pushes the workspace's current-period fraction-of-cap past an
    alert threshold not yet crossed this window, log a WARNING and — only on
    the normal finish path, see below — publish a `budget_alert` event on
    the run's own event stream (see the module docstring for why that bus
    rather than a dedicated one). No-op whenever no budget is configured;
    never raises.

    The WARNING always fires; the event publish is gated on
    `run.status == "completed"`. `RunEventBus` (`engine/events.py`) exposes
    no cheap "is this run's stream still open" query — `subscriber_count`/
    `tracked_runs` don't answer it either — and this hook can run well after
    a run's SSE stream has already gone terminal and been forgotten
    (`RunEventBus.forget`, once its last subscriber detaches): the crash
    handler in `engine/harness.py`'s `execute()` and `services/reconcile.py`'s
    sweep of orphaned runs both call this hook against a run whose bus
    channel nobody is plausibly still attached to, and both always leave
    `run.status == "failed"`. Rather than publish into a channel that is
    very likely already gone, this only publishes on `status == "completed"`
    and accepts the log line above as the record of an alert crossed on
    every other path — crash, reconcile, or even a normal finish that lands
    on `"completed_without_output"` instead. That last case is a
    conservative trade: its bus channel is probably still live too, but
    there is no cheap way to tell that from here, and the status string is.
    """
    try:
        workspace = await db.get(Workspace, workspace_id)
        if workspace is None:
            return
        raw = _budget_doc(workspace)
        if raw is None:
            return
        budget = BudgetSettings(**raw)

        now = utcnow()
        window_start, _ = window_bounds(budget.period, now)
        window_key = window_start.isoformat()
        spent = await period_spend(db, workspace_id, budget.period, now)
        cap = Decimal(str(budget.cap_usd))
        fraction = float(spent / cap) if cap > 0 else 0.0

        state = dict((workspace.settings or {}).get(BUDGET_STATE_KEY) or {})
        # A stale (or absent) window_start means a new period has started
        # since anything was last alerted — treat it as nothing alerted yet,
        # so the same thresholds can fire again in the new window.
        already_alerted = set(state.get("alerted") or []) if state.get("window_start") == window_key else set()

        newly_crossed = sorted(a for a in budget.alerts if fraction >= a and a not in already_alerted)
        if not newly_crossed:
            return

        already_alerted.update(newly_crossed)
        settings = dict(workspace.settings or {})
        settings[BUDGET_STATE_KEY] = {"window_start": window_key, "alerted": sorted(already_alerted)}
        workspace.settings = settings
        await db.commit()

        for threshold in newly_crossed:
            log.warning(
                "workspace %s crossed %.0f%% of its %s spend budget: $%.2f of $%.2f",
                workspace_id, threshold * 100, budget.period, spent, cap,
            )
            # See the docstring: only the normal finish path gets an event
            # published, since that is the only status this hook sees where
            # the run's bus channel is expected to still have somewhere to
            # go. Every other path still got its WARNING above.
            if run.status != "completed":
                continue
            await get_event_bus().publish(
                run.id,
                RunEvent(
                    "budget_alert",
                    {
                        "workspace_id": str(workspace_id),
                        "period": budget.period,
                        "fraction": threshold,
                        "spent_usd": float(spent),
                        "cap_usd": float(cap),
                    },
                ),
            )
    except Exception:
        log.exception("budget alert post-run hook raised for workspace %s", workspace_id)
