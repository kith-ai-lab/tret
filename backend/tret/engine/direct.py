"""Direct batch targets: a `Run` row without the agent harness.

A batch profile that sets `direct = True` (`batches/profiles/__init__.py`) gets one `Run` per
target exactly like an agent profile (`batches/core.py::_launch`), so cost, budgets, workspace
credit gates, pause-on-credit, post-run hooks, the runs UI and billing all keep working. What
differs is who executes it: `execute_direct` runs the profile's `run_direct(ctx)` instead of the
harness loop. The profile makes its model calls through `ctx.call_json(...)`, which is
`engine/routed_call.routed_json_call` plus the bookkeeping below.

Accounting (one source, the harness's): each call's usage goes into a `ModelSegment` of the
model that answered (`engine/harness.py`), cost is `ModelInfo.cost_usd` (`Run.cost_usd`, the
catalog price, used for caps) and `Run.reported_cost_usd` is the provider-reported cost when
there is one, else that call's catalog price (what billing charges), and energy and carbon come
from the same `_combine_segments` / `energy_accounting` the harness runs per turn. The router's
own call goes into `Run.overhead`. The run's `task_type` is `structured_call`; its task input is
stamped `_structured_call`, which keeps it out of `run_outcomes` (`services/outcomes.py`).

Lifecycle, as the harness does it: the pre-run gates (`engine/extensions.py`) may refuse before any
spend; the run goes `running`, ends `completed`, `failed` or `cancelled`; the post-run hooks
run exactly once on every path except a refusal by the gate itself.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from tret.config import get_settings
from tret.engine.accounting import build_emissions_context, factors_for
from tret.engine.collection_tools import COLLECTION_ID_KEY, COLLECTION_TARGET_KEY
from tret.engine.extensions import get_extension_registry
from tret.engine.routed_call import (
    CallServices,
    RoutedCallError,
    RoutedJsonResult,
    get_call_services,
    routed_json_call,
)

log = logging.getLogger("tret.direct")

TASK_TYPE = "structured_call"
TERMINAL = ("completed", "failed", "cancelled")


class CostCapExceeded(Exception):
    """The run has already spent its cost cap; no further call is made."""


@dataclass
class DirectResult:
    """What a profile's `run_direct` returns. `error` set = the run is `failed` with that text
    (what was already spent stays booked); otherwise it is `completed`. `outcome` is free-form
    and is kept on `Run.task_input["_direct_outcome"]` for the profile's `settle_outcome`."""

    outcome: dict | None = None
    error: str | None = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RunAccounting:
    """Folds routed calls into one `Run`'s totals, the way `HarnessEngine._book_usage` does."""

    def __init__(self, run, emissions, catalog) -> None:
        self.run = run
        self.emissions = emissions
        self.catalog = catalog
        self.segments: list[Any] = []
        self._by_model: dict[str, Any] = {}
        self.overhead_calls: list[dict] = []
        self.decisions: list[dict] = []
        self.calls = 0

    def _segment(self, model):
        from tret.engine.harness import ModelSegment  # private to the harness; see tests/test_direct_internals.py

        seg = self._by_model.get(model.id)
        if seg is None:
            factors = factors_for(model.provider, model.id, self.emissions)
            seg = ModelSegment(model, reason="structured_call", factors=factors)
            self._by_model[model.id] = seg
            self.segments.append(seg)
        return seg

    def book(self, calls, decision=None, *, purpose: str = "") -> None:
        """Book every call of a routed result (or of a failed one) and the router's own spend."""
        from tret.engine.harness import _combine_segments
        from tret.services.emissions import overhead_block

        run = self.run
        for rec in calls:
            self.calls += 1
            usage = rec.usage
            seg = self._segment(rec.model)
            seg.add(
                usage, self.calls, estimated=rec.estimated, served_by=rec.served_by,
                usage_status="estimated" if rec.estimated else "reported",
            )
            turn_cost = rec.model.cost_usd(
                usage.input_tokens, usage.output_tokens, usage.cache_read_tokens, usage.cache_write_tokens
            )
            run.input_tokens = (run.input_tokens or 0) + usage.input_tokens
            run.output_tokens = (run.output_tokens or 0) + usage.output_tokens
            run.cache_read_tokens = (run.cache_read_tokens or 0) + usage.cache_read_tokens
            run.cache_write_tokens = (run.cache_write_tokens or 0) + usage.cache_write_tokens
            run.cost_usd = (run.cost_usd or Decimal(0)) + turn_cost
            run.reported_cost_usd = (run.reported_cost_usd or Decimal(0)) + (
                usage.reported_cost_usd if usage.reported_cost_usd is not None else turn_cost
            )
            run.model_used = rec.model.id
            run.provider_used = rec.model.provider
        run.iterations = self.calls
        if decision is not None:
            if decision.spend:
                self.overhead_calls.append(decision.spend)
            self.overhead_calls.extend(decision.failed_call_spend)
            self.decisions.append(
                {
                    "purpose": purpose,
                    "chosen_model": decision.chosen_model,
                    "objective": decision.objective,
                    "reasoning": decision.reasoning,
                    "candidates": list(decision.candidates),
                    "served_by": next((c.served_by for c in reversed(calls) if c.ok), None),
                    "attempts": len(calls),
                    "context_fit": decision.context_fit,
                }
            )
            routing = decision.to_json()
            if run.routing:  # keep the first decision as the run's routing record
                routing = {k: v for k, v in run.routing.items() if k != "structured_calls"}
            run.routing = {**routing, "structured_calls": list(self.decisions)}
        if self.segments:
            accounting = _combine_segments(self.segments, self.emissions)
            run.energy_wh = Decimal(str(accounting["energy_wh"]))
            run.energy_accounting = accounting
            run.model_timeline = [s.to_json(self.emissions) for s in self.segments]
        run.overhead = overhead_block(self.overhead_calls)


@dataclass
class DirectContext:
    """What `run_direct` gets: the rows, the settings, and `call_json`."""

    db: Any
    run: Any
    batch: Any
    target: Any
    workspace_id: uuid.UUID
    model_policy: dict
    emissions: Any
    services: CallServices
    accounting: RunAccounting
    max_cost: Decimal
    settings: dict = field(default_factory=dict)

    async def call_json(
        self,
        *,
        purpose: str,
        system: str,
        prompt: str,
        schema: dict,
        est_input_tokens: int,
        max_output_tokens: int = 4096,
        **kw: Any,
    ) -> RoutedJsonResult:
        """A routed structured call booked on this run (cost, tokens, energy, routing). Refuses
        once the run has spent its cap. A failed call's billed attempts are booked too, then the
        `RoutedCallError` propagates."""
        spent = self.run.cost_usd or Decimal(0)
        if spent >= self.max_cost:
            raise CostCapExceeded(f"cost_cap_exceeded: run cost ${spent} >= cap ${self.max_cost}")
        record: dict = {}
        try:
            result = await routed_json_call(
                self.db,
                workspace_id=self.workspace_id,
                model_policy=self.model_policy,
                purpose=purpose,
                system=system,
                prompt=prompt,
                schema=schema,
                est_input_tokens=est_input_tokens,
                max_output_tokens=max_output_tokens,
                emissions=self.emissions,
                services=self.services,
                project_id=self.run.project_id,
                record=record,
                **kw,
            )
        except asyncio.CancelledError:
            # A call that was sent and then cancelled (the run's time limit) was still paid for.
            self.accounting.book(record.get("calls", []), record.get("decision"), purpose=purpose)
            with contextlib.suppress(Exception):
                await asyncio.shield(self.db.commit())
            raise
        except RoutedCallError as e:
            self.accounting.book(e.calls, e.decision, purpose=purpose)
            await self.db.commit()
            raise
        self.accounting.book(result.calls, result.decision, purpose=purpose)
        await self.db.commit()
        return result


def _max_cost(run, harness, workspace) -> Decimal:
    from tret.engine.harness import DEFAULT_MAX_COST_USD, _effective_max_cost
    from tret.services.guardrails import resolve_guardrails

    loop_cfg = dict(getattr(harness, "loop_config", None) or {})
    cap = _effective_max_cost(Decimal(str(loop_cfg.get("max_cost_usd", DEFAULT_MAX_COST_USD))), run.task_input or {})
    guard = resolve_guardrails(get_settings(), workspace.settings if workspace is not None else None)
    if guard.spend_cap_usd is not None:
        cap = min(cap, Decimal(str(guard.spend_cap_usd)))
    return cap


CANCEL_ERROR = "timeout: stopped by the collection run's time limit"


async def execute_direct(run_id: uuid.UUID, profile: Any, session_factory: Any) -> None:
    """Run one direct target's `Run` to a terminal state. Never raises except for cancellation
    (which is re-raised after the run is closed out and its hooks have run). Every path after the
    run row exists ends in `_close`, so the post-run hooks fire exactly once, except when the
    pre-run gate itself refused (it placed no hold)."""
    from tret.db.models import Batch, BatchTarget, Harness, Run, Workspace

    async with session_factory() as db:
        run = await db.get(Run, run_id)
        if run is None or run.status in TERMINAL:
            return
        harness = await db.get(Harness, run.harness_id)
        if harness is None:
            run.status = "failed"
            run.error = "the run's harness no longer exists"
            run.finished_at = _utcnow()
            await db.commit()
            return
        workspace_id = harness.workspace_id
        registry = get_extension_registry()

        gate = await registry.check_pre_run(db, run, workspace_id)
        if not gate.allowed:
            run.status = "failed"
            run.error = f"{gate.reason}: {gate.detail}" if gate.detail else gate.reason
            run.finished_at = _utcnow()
            await db.commit()
            return

        status, error = "failed", None
        try:
            ti = run.task_input or {}
            batch = await db.get(Batch, uuid.UUID(str(ti[COLLECTION_ID_KEY]))) if ti.get(COLLECTION_ID_KEY) else None
            target = (
                await db.get(BatchTarget, uuid.UUID(str(ti[COLLECTION_TARGET_KEY])))
                if ti.get(COLLECTION_TARGET_KEY)
                else None
            )
            workspace = await db.get(Workspace, workspace_id)
            run.status = "running"
            run.started_at = _utcnow()
            await db.commit()
            emissions = await build_emissions_context(db, workspace_id, run.created_at)
            services = await get_call_services(db, workspace_id)
            ctx = DirectContext(
                db=db,
                run=run,
                batch=batch,
                target=target,
                workspace_id=workspace_id,
                model_policy=dict(((batch.settings or {}) if batch else {}).get("model_policy") or {"mode": "auto"}),
                emissions=emissions,
                services=services,
                accounting=RunAccounting(run, emissions, services.catalog),
                max_cost=_max_cost(run, harness, workspace),
                settings=dict((batch.settings or {}) if batch else {}),
            )
            result = await profile.run_direct(ctx)
            if result is not None and result.outcome is not None:
                # Committed here: `_close` starts by discarding whatever is uncommitted.
                run.task_input = {**(run.task_input or {}), "_direct_outcome": result.outcome}
                await db.commit()
            if result is not None and result.error:
                error = result.error
            else:
                status = "completed"
        except asyncio.CancelledError:
            await _close(db, run_id, "cancelled", CANCEL_ERROR, workspace_id)
            raise
        except CostCapExceeded as e:
            error = str(e)
        except RoutedCallError as e:
            error = f"{e.kind}: {e}"
        except Exception as e:  # noqa: BLE001 - a failed target, never a crashed worker
            log.exception("direct run %s crashed", run_id)
            error = f"{type(e).__name__}: {e}"
        await _close(db, run_id, status, error, workspace_id)


async def _close(db, run_id: uuid.UUID, status: str, error: str | None, workspace_id: uuid.UUID) -> None:
    """Write the terminal state (after discarding any half-written transaction) and fire the
    post-run hooks. Shielded, so a cancellation cannot skip the billing hook."""
    from tret.db.models import Run

    async def go() -> None:
        with contextlib.suppress(Exception):
            await db.rollback()
        run = await db.get(Run, run_id, populate_existing=True)
        if run is None:
            return
        if run.status not in TERMINAL or status == "cancelled":
            run.status = status
        run.error = error if status != "completed" else None
        run.finished_at = run.finished_at or _utcnow()
        await db.commit()
        await get_extension_registry().run_post_run_hooks(db, run, workspace_id)

    await asyncio.shield(asyncio.ensure_future(go()))
