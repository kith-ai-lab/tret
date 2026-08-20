"""Writing run outcomes down — the bookkeeping half of routing evidence.

`router_llm/outcomes.py` decides what a run was worth; this module is what puts
that verdict in the database, and it is deliberately the only place that does.

Two things happen here that are worth stating plainly:

**Recording never fails a run.** Every entry point swallows its own errors and
logs them. An outcome row is derived bookkeeping — it can be rebuilt from
`runs`, `findings` and `approvals` at any time with `bench outcomes backfill` —
and letting a bookkeeping bug mark a successful run `failed`, or roll back the
transcript the audit trail depends on, would trade something irreplaceable for
something reproducible.

**A run is scored twice.** Once when it ends, and again whenever an approval
lands on one of its findings, because the human verdict is the strongest signal
bench has and it arrives minutes or days after the run is over. The second write
is an update of the same row, so a run always has exactly one outcome.
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bench.db.models import Finding, Harness, Pack, Run, RunOutcome
from bench.engine.context import task_config
from bench.router_llm.objectives import DEFAULT_MAX_COST_TIER, DEFAULT_OBJECTIVE
from bench.router_llm.outcomes import UNSCORED, score, size_band

log = logging.getLogger("bench.outcomes")

DEFAULT_MAX_ITERATIONS = 24


async def _finding_counts(db: AsyncSession, run_id: uuid.UUID) -> dict[str, int]:
    rows = (
        await db.execute(
            select(Finding.status, func.count())
            .where(Finding.run_id == run_id)
            .group_by(Finding.status)
        )
    ).all()
    return {status: count for status, count in rows}


async def _task_shape(db: AsyncSession, run: Run, routing: dict) -> str:
    """The shape this run was routed for.

    Read from the persisted routing decision first, which is the only source
    that stays correct after the pack is upgraded. Older runs predate that field
    and are resolved from the pack as it stands today — approximate, and the
    honest best available for a backfill.
    """
    shape = routing.get("task_shape")
    if shape:
        return shape
    if run.pack_id:
        pack = await db.get(Pack, run.pack_id)
        task = task_config(pack, run.task_type) if pack else None
        if task and task.get("shape"):
            return task["shape"]
    return "freeform"


async def _max_iterations(db: AsyncSession, run: Run) -> int:
    """The iteration ceiling this run was working against.

    Needed because "used 20 iterations" means nothing on its own — it is
    excellent against a ceiling of 50 and desperate against a ceiling of 24.
    """
    if run.harness_id is None:
        return DEFAULT_MAX_ITERATIONS
    harness = await db.get(Harness, run.harness_id)
    if harness is None:
        return DEFAULT_MAX_ITERATIONS
    return int((harness.loop_config or {}).get("max_iterations") or DEFAULT_MAX_ITERATIONS)


def _duration_ms(run: Run) -> int:
    if not run.started_at or not run.finished_at:
        return 0
    return max(0, int((run.finished_at - run.started_at).total_seconds() * 1000))


def _est_input_tokens(run: Run) -> int:
    """How much input this run carried, for the size band.

    The composition report is the right number — it is what the router itself
    was shown. `input_tokens` is the fallback for runs that never recorded one,
    and it undercounts (cache reads are excluded by design, see providers.base
    Usage), which is acceptable for bucketing but not for anything finer.
    """
    composition = run.context_composition or {}
    return int(composition.get("total_est_tokens") or run.input_tokens or 0)


async def build_outcome(db: AsyncSession, run: Run) -> RunOutcome | None:
    """The outcome row for `run`, or None if this run carries no lesson.

    Not persisted here — `record_outcome` owns the write, and the backfill reuses
    this to build rows in bulk.
    """
    if run.model_used is None:
        # Never routed: failed before the first token (unknown task type, no
        # candidates). Real failures, but not this model's — there wasn't one.
        return None

    routing = run.routing or {}
    counts = await _finding_counts(db, run.id)
    approved = counts.get("approved", 0)
    rejected = counts.get("rejected", 0)

    scored = score(
        status=run.status,
        error=run.error,
        messages=run.messages or [],
        iterations=run.iterations or 0,
        max_iterations=await _max_iterations(db, run),
        findings_approved=approved,
        findings_rejected=rejected,
    )
    if scored.outcome_class == UNSCORED:
        return None

    return RunOutcome(
        run_id=run.id,
        project_id=run.project_id,
        harness_id=run.harness_id,
        task_type=run.task_type,
        task_shape=await _task_shape(db, run, routing),
        objective=routing.get("objective") or DEFAULT_OBJECTIVE,
        max_cost_tier=routing.get("max_cost_tier") or DEFAULT_MAX_COST_TIER,
        size_band=size_band(_est_input_tokens(run)),
        model_id=run.model_used,
        provider=run.provider_used,
        fallback_used=bool(routing.get("fallback_used")),
        override=routing.get("override"),
        outcome_class=scored.outcome_class,
        quality_score=scored.quality_score,
        score_version=scored.score_version,
        error_kind=scored.error_kind,
        components=scored.components,
        iterations=run.iterations or 0,
        cost_usd=run.cost_usd or 0,
        input_tokens=run.input_tokens or 0,
        output_tokens=run.output_tokens or 0,
        energy_wh=run.energy_wh,
        duration_ms=_duration_ms(run),
        findings_created=sum(counts.values()),
        findings_approved=approved,
        findings_rejected=rejected,
        observed_at=run.created_at,
    )


# Columns rewritten when a run is re-scored. The routing key is not among them:
# it describes the decision that was made, and that never changes.
_MUTABLE = (
    "outcome_class",
    "quality_score",
    "score_version",
    "error_kind",
    "components",
    "iterations",
    "cost_usd",
    "input_tokens",
    "output_tokens",
    "energy_wh",
    "duration_ms",
    "findings_created",
    "findings_approved",
    "findings_rejected",
)


async def record_outcome(db: AsyncSession, run: Run, *, commit: bool = False) -> RunOutcome | None:
    """Write (or rewrite) this run's outcome. Never raises.

    `commit=False` by default so the engine can fold this into the write it is
    already about to make. The approval path passes True, because by then the
    request's own transaction has been committed and there is nothing to join.
    """
    try:
        fresh = await build_outcome(db, run)
        if fresh is None:
            return None
        existing = await db.get(RunOutcome, run.id)
        if existing is None:
            db.add(fresh)
            recorded = fresh
        else:
            for column in _MUTABLE:
                setattr(existing, column, getattr(fresh, column))
            recorded = existing
        if commit:
            await db.commit()
        return recorded
    except Exception:  # noqa: BLE001 - bookkeeping must never break a run
        log.exception("failed to record run outcome for run %s", run.id)
        return None


async def record_outcome_for_finding(db: AsyncSession, finding_id: uuid.UUID) -> None:
    """Re-score the run behind a finding whose approval status just changed."""
    try:
        finding = await db.get(Finding, finding_id)
        if finding is None:
            return
        run = await db.get(Run, finding.run_id)
        if run is None:
            return
        await record_outcome(db, run, commit=True)
    except Exception:  # noqa: BLE001 - an approval must succeed regardless
        log.exception("failed to re-score outcome for finding %s", finding_id)


async def backfill(db: AsyncSession, *, limit: int | None = None) -> dict:
    """Rebuild outcomes for every already-finished run.

    The point of a derived table is that it can be thrown away, so this is the
    repair path as well as the migration path: it rewrites existing rows rather
    than skipping them, which is what makes re-running it after a change to the
    scoring weights do the right thing.
    """
    q = select(Run).where(Run.status.in_(("completed", "completed_without_output", "failed")))
    q = q.order_by(Run.created_at.desc())
    if limit:
        q = q.limit(limit)
    runs = (await db.execute(q)).scalars().all()

    written = skipped = 0
    for run in runs:
        if await record_outcome(db, run) is None:
            skipped += 1
        else:
            written += 1
    await db.commit()
    return {"scanned": len(runs), "written": written, "skipped": skipped}
