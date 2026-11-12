"""Writing run outcomes down — the bookkeeping half of routing evidence.

`router_llm/outcomes.py` decides what a run was worth; this module is what puts
that verdict in the database, and it is deliberately the only place that does.

Two things happen here that are worth stating plainly:

**Recording never fails a run.** Every entry point swallows its own errors and
logs them. An outcome row is derived bookkeeping — it can be rebuilt from
`runs`, `findings` and `approvals` at any time with `tret outcomes backfill` —
and letting a bookkeeping bug mark a successful run `failed`, or roll back the
transcript the audit trail depends on, would trade something irreplaceable for
something reproducible.

**A run is scored twice.** Once when it ends, and again whenever an approval
lands on one of its findings, because the human verdict is the strongest signal
tret has and it arrives minutes or days after the run is over. The second write
is an update of the same row, so a run always has exactly one outcome.
"""

from __future__ import annotations

from datetime import timezone

import contextlib
import logging
import uuid
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tret.batches.rubrics import is_evaluator_run
from tret.db.models import Approval, Batch, BatchTarget, Finding, Harness, Pack, Run, RunOutcome
from tret.engine.context import task_config
from tret.router_llm.objectives import DEFAULT_MAX_COST_TIER, DEFAULT_OBJECTIVE
from tret.router_llm.outcomes import (
    UNSCORED,
    evaluated_quality,
    handoff_score,
    score,
    size_band,
)

log = logging.getLogger("tret.outcomes")

DEFAULT_MAX_ITERATIONS = 24
# Mirrors engine/harness.py `MAX_ITERATIONS_CEILING` (importing the engine from
# here would be circular).
MAX_ITERATIONS_CEILING = 50


async def latest_approval_purposes(
    db: AsyncSession, finding_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, str]:
    """`{finding_id: purpose}` of each finding's most recent approval row (ordered
    by `created_at`, then `id`). Findings with no approval row are absent, so a
    caller treats a missing key as a legacy review. One query; none for no ids."""
    ids = list(finding_ids)
    if not ids:
        return {}
    purposes: dict[uuid.UUID, str] = {}
    for fid, purpose in (
        await db.execute(
            select(Approval.finding_id, Approval.purpose)
            .where(Approval.finding_id.in_(ids))
            .order_by(Approval.created_at, Approval.id)
        )
    ).all():
        purposes[fid] = purpose  # ascending, so the last write is the latest
    return purposes


async def _finding_counts(db: AsyncSession, run_id: uuid.UUID) -> dict[str, int]:
    """`{status: count}` over the run's findings, where "approved"/"rejected"
    count only human *reviews*. A finding approved with purpose "packaging" (a
    page map approved just so it could be packaged) is counted under
    "approved_packaging" instead, so it is not quality evidence. A decided
    finding with no approvals row (legacy data) counts as a review; with several,
    the most recent one decides."""
    rows = (
        await db.execute(select(Finding.id, Finding.status).where(Finding.run_id == run_id))
    ).all()
    decided = [fid for fid, status in rows if status in ("approved", "rejected")]
    purposes = await latest_approval_purposes(db, decided)
    counts: dict[str, int] = {}
    for fid, status in rows:
        key = status
        if status == "approved" and purposes.get(fid, "review") != "review":
            key = "approved_packaging"
        counts[key] = counts.get(key, 0) + 1
    return counts


def _explored(routing: dict) -> bool:
    """Whether the run's initial model was chosen by bounded exploration."""
    return bool((routing.get("exploration") or {}).get("explored"))


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
    This is the ceiling `quality_score` has always been judged against (the
    harness's own); see `_effective_max_iterations` for the engine-effective one.
    """
    if run.harness_id is None:
        return DEFAULT_MAX_ITERATIONS
    harness = await db.get(Harness, run.harness_id)
    if harness is None:
        return DEFAULT_MAX_ITERATIONS
    return int((harness.loop_config or {}).get("max_iterations") or DEFAULT_MAX_ITERATIONS)


async def _effective_max_iterations(db: AsyncSession, run: Run) -> int:
    """The ceiling the engine actually enforced: a positive int `_max_iterations`
    stamped on `task_input` (batches do) replaces the harness value, clamped 1..50
    (`engine/harness.py::_effective_max_iterations`). Used ONLY for the automatic
    term of `evaluated_quality_score`; `quality_score` keeps the harness ceiling."""
    stamped = (run.task_input or {}).get("_max_iterations")
    if isinstance(stamped, int) and not isinstance(stamped, bool) and stamped > 0:
        return max(1, min(stamped, MAX_ITERATIONS_CEILING))
    return await _max_iterations(db, run)


async def task_family_for_run(db: AsyncSession, run: Run) -> str | None:
    """`<family_key>@<workspace_id>` for a batch target run, else None.

    The family is DERIVED, never stamped: only the engine-stamped `_collection_id`
    (callers cannot set it, `api/runs.py` strips it) names a batch, and the batch's
    workspace must equal the workspace of the run's own project, so one workspace
    cannot write evidence into another's family. Any error reads as no family.
    Used by both the outcome writer and the engine before routing."""
    try:
        collection_id = (run.task_input or {}).get("_collection_id")
        if not collection_id or run.project_id is None:
            return None
        from tret.batches import scoring
        from tret.db.models import Batch, Project

        batch = await db.get(Batch, uuid.UUID(str(collection_id)))
        project = await db.get(Project, run.project_id)
        if batch is None or project is None or batch.workspace_id != project.workspace_id:
            return None
        return f"{scoring.family_key(batch)}@{batch.workspace_id}"
    except Exception:  # noqa: BLE001 - the family is bookkeeping / an optimisation
        log.exception("could not derive the task family for run %s", run.id)
        return None


async def _evaluator_term(db: AsyncSession, run: Run) -> dict | None:
    """The evaluator's verdict on this run as a blend term, or None.

    Only a scored evaluation of the batch's CURRENT LIVE pass (never the dead pass
    `current_pass` falls back to) that is not stale counts. A human "disagree"
    cancels the term; an "adjust" replaces the judge's number with the person's.
    Carries what validated it (rubric, judge, pass) so family evidence can be
    limited to the gate's own judge and rubric.
    """
    try:
        from tret.batches import scoring
        from tret.db.models import BatchEvaluation, BatchTarget, TargetEvaluation

        rows = (
            (
                await db.execute(
                    select(TargetEvaluation).where(
                        TargetEvaluation.evaluated_run_id == run.id,
                        TargetEvaluation.status == "scored",
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            if row.quality is None:
                continue
            current = await scoring.current_pass(db, row.collection_id)
            if (
                current is None
                or current.id != row.evaluation_id
                or current.status in scoring.DEAD_PASS_STATUSES
            ):
                continue
            target = await db.get(BatchTarget, row.target_id)
            if target is None or scoring.is_stale(row, target):
                continue
            review = row.review if isinstance(row.review, dict) else None
            verdict = (review or {}).get("verdict")
            if verdict == "disagree":
                return None
            source, quality = "judge", float(row.quality)
            if verdict == "adjust" and review.get("adjusted_quality") is not None:
                source, quality = "human_adjusted", float(review["adjusted_quality"])
            evaluation = await db.get(BatchEvaluation, row.evaluation_id)
            return {
                "quality": round(min(1.0, max(0.0, quality / 100.0)), 4),
                "source": source,
                "rubric_key": evaluation.rubric_key if evaluation else None,
                "rubric_version": evaluation.rubric_version if evaluation else None,
                "judge_model": evaluation.judge_model if evaluation else None,
                "evaluation_id": str(row.evaluation_id),
            }
    except Exception:  # noqa: BLE001 - never lets an evaluation lookup fail a run
        log.exception("could not look up the evaluation of run %s", run.id)
    return None


def _duration_ms(run: Run) -> int:
    if not run.started_at or not run.finished_at:
        return 0
    # SQLite hands back naive datetimes after a refresh while the engine
    # stamps aware ones; treat naive as UTC so the two never fail to subtract
    # (which would drop the whole outcome row, and with it the priors and the
    # cooldown breaker that read it).
    started, finished = run.started_at, run.finished_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    if finished.tzinfo is None:
        finished = finished.replace(tzinfo=timezone.utc)
    return max(0, int((finished - started).total_seconds() * 1000))


def _est_input_tokens(run: Run) -> int:
    """How much input this run carried, for the size band.

    The composition report is the right number — it is what the router itself
    was shown. `input_tokens` is the fallback for runs that never recorded one,
    and it undercounts (cache reads are excluded by design, see providers.base
    Usage), which is acceptable for bucketing but not for anything finer.
    """
    composition = run.context_composition or {}
    return int(composition.get("total_est_tokens") or run.input_tokens or 0)


async def build_outcomes(db: AsyncSession, run: Run) -> list[RunOutcome]:
    """Every outcome row this run produced — one per model it used.

    Almost always exactly one. A run that changed model part-way produced
    evidence about each of them, and the pair is the most useful thing tret can
    record: a within-task comparison, where one model stalled on a specific
    problem and another finished it. Averages across different tasks cannot say
    that.

    Not persisted here — `record_outcome` owns the write, and the backfill reuses
    this to build rows in bulk.
    """
    if is_evaluator_run(run.task_input):
        # A batch evaluator run (judging another run, `batches/evaluation.py`): the
        # judge's performance at judging says nothing about how it does the task
        # the router is learning, and its rows would skew those priors.
        #
        # The engine reads the run's harness again right after this call, and a tool
        # error earlier in the run (its rollback) expired it. Building outcomes
        # normally reloads the harness (`_max_iterations`); returning early must not
        # skip that, or the engine's next attribute read crashes with MissingGreenlet.
        await _max_iterations(db, run)
        return []
    if run.model_used is None:
        # Never routed: failed before the first token (unknown task type, no
        # candidates). Real failures, but not this model's — there wasn't one.
        return []

    routing = run.routing or {}
    counts = await _finding_counts(db, run.id)
    approved = counts.get("approved", 0)
    rejected = counts.get("rejected", 0)

    final = score(
        status=run.status,
        error=run.error,
        messages=run.messages or [],
        iterations=run.iterations or 0,
        max_iterations=await _max_iterations(db, run),
        findings_approved=approved,
        findings_rejected=rejected,
    )
    if final.outcome_class == UNSCORED:
        return []

    task_family = await task_family_for_run(db, run)
    evaluator = await _evaluator_term(db, run) if task_family else None
    evaluated_score = None
    if evaluator is not None:
        # The automatic term is judged against the ceiling the engine enforced; the
        # run's own `quality_score` (above) keeps the harness ceiling, unchanged.
        effective = await _effective_max_iterations(db, run)
        auto_parts = final
        if effective != await _max_iterations(db, run):
            auto_parts = score(
                status=run.status,
                error=run.error,
                messages=run.messages or [],
                iterations=run.iterations or 0,
                max_iterations=effective,
                findings_approved=approved,
                findings_rejected=rejected,
            )
        evaluated_score = evaluated_quality(
            auto=float(auto_parts.components.get("automatic_score", auto_parts.quality_score)),
            human_rate=(auto_parts.components.get("human") or {}).get("smoothed_rate"),
            evaluator=evaluator["quality"],
        )

    shared = {
        "task_family": task_family,
        "project_id": run.project_id,
        "harness_id": run.harness_id,
        "task_type": run.task_type,
        "task_shape": await _task_shape(db, run, routing),
        "objective": routing.get("objective") or DEFAULT_OBJECTIVE,
        "max_cost_tier": routing.get("max_cost_tier") or DEFAULT_MAX_COST_TIER,
        "size_band": size_band(_est_input_tokens(run)),
        "fallback_used": bool(routing.get("fallback_used")),
        "override": routing.get("override"),
        "observed_at": run.created_at,
    }

    timeline = run.model_timeline or []
    if len(timeline) < 2:
        # The ordinary run: one model, the run's own outcome, everything it spent.
        return [
            RunOutcome(
                run_id=run.id,
                segment_index=0,
                model_id=run.model_used,
                provider=run.provider_used,
                # `model_timeline` is persisted for the ordinary run whenever a
                # turn was estimated or a segment picked up a `served_by` (see
                # engine/harness.py) — read it off the timeline when it is
                # there, and leave the routing evidence honestly blank (not
                # guessed) otherwise: a local run has no serving upstream to
                # disambiguate, and a run that predates this column has no way
                # to recover it. Anthropic-direct runs *do* carry one — every
                # `TurnComplete` from providers/anthropic.py sets the constant
                # `SERVED_BY = "anthropic"` — so a null here means local or
                # pre-column, never Anthropic.
                served_by=timeline[0].get("served_by") if timeline else None,
                outcome_class=final.outcome_class,
                quality_score=final.quality_score,
                score_version=final.score_version,
                error_kind=final.error_kind,
                # `explored` marks a row the router chose by bounded exploration
                # (an untried model picked without consulting the router LLM —
                # router_llm/router.py `_maybe_explore`), so priors can be read
                # with and without those deliberate trials.
                components={
                    **final.components,
                    "explored": _explored(routing),
                    **({"evaluator": evaluator} if evaluator is not None else {}),
                },
                evaluated_quality_score=evaluated_score,
                iterations=run.iterations or 0,
                cost_usd=run.cost_usd or 0,
                input_tokens=run.input_tokens or 0,
                output_tokens=run.output_tokens or 0,
                energy_wh=run.energy_wh,
                duration_ms=_duration_ms(run),
                findings_created=sum(counts.values()),
                findings_approved=approved,
                findings_rejected=rejected,
                **shared,
            )
        ]

    rows = []
    for index, segment in enumerate(timeline):
        last = index == len(timeline) - 1
        if last:
            # The model that finished the work owns the run's outcome, including
            # its human verdict: the approved finding is the one it produced.
            scored = final
            findings = (sum(counts.values()), approved, rejected)
        else:
            # A model the run moved on from. Why it moved on is what decides
            # whether this is evidence at all — `handoff_score`.
            scored = handoff_score(timeline[index + 1].get("reason", "capability_stall"))
            findings = (0, 0, 0)
        rows.append(
            RunOutcome(
                run_id=run.id,
                segment_index=index,
                model_id=segment.get("model"),
                provider=segment.get("provider"),
                served_by=segment.get("served_by"),
                outcome_class=scored.outcome_class,
                quality_score=scored.quality_score,
                score_version=scored.score_version,
                error_kind=scored.error_kind,
                # Only the first segment is the model exploration chose; later
                # segments come from supervisor switches.
                components={
                    **scored.components,
                    "explored": _explored(routing) if index == 0 else False,
                    **({"evaluator": evaluator} if evaluator is not None and last else {}),
                },
                evaluated_quality_score=evaluated_score if last else None,
                # Each segment's own spend, never the run's totals — that is the
                # whole reason model_timeline exists (engine/harness.py).
                iterations=max(
                    0, (segment.get("to_iteration") or 0) - (segment.get("from_iteration") or 0) + 1
                ),
                cost_usd=segment.get("cost_usd") or 0,
                input_tokens=segment.get("input_tokens") or 0,
                output_tokens=segment.get("output_tokens") or 0,
                energy_wh=segment.get("energy_wh"),
                duration_ms=_duration_ms(run) if last else 0,
                findings_created=findings[0],
                findings_approved=findings[1],
                findings_rejected=findings[2],
                **shared,
            )
        )
    return rows


# Columns rewritten when a run is re-scored. The routing key is not among them:
# it describes the decision that was made, and that never changes.
_MUTABLE = (
    "task_family",
    "outcome_class",
    "quality_score",
    "score_version",
    "error_kind",
    "components",
    "evaluated_quality_score",
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


async def record_outcome(db: AsyncSession, run: Run, *, commit: bool = False) -> list[RunOutcome]:
    """Write (or rewrite) this run's outcomes. Never raises.

    `commit=False` by default so the engine can fold this into the write it is
    already about to make. The approval path passes True, because by then the
    request's own transaction has been committed and there is nothing to join.
    """
    try:
        fresh = await build_outcomes(db, run)
        if not fresh:
            return []
        recorded = []
        for row in fresh:
            existing = await db.get(RunOutcome, (run.id, row.segment_index))
            if existing is None:
                db.add(row)
                recorded.append(row)
            else:
                for column in _MUTABLE:
                    setattr(existing, column, getattr(row, column))
                recorded.append(existing)
        # A re-score can produce *fewer* segments than a previous one (a run
        # re-scored after its timeline was rebuilt, or after a backfill under new
        # weights). Leaving the surplus behind would keep counting evidence for a
        # segment that no longer exists.
        stale = (
            (
                await db.execute(
                    select(RunOutcome).where(
                        RunOutcome.run_id == run.id,
                        RunOutcome.segment_index >= len(fresh),
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in stale:
            await db.delete(row)
        if commit:
            await db.commit()
        return recorded
    except Exception:  # noqa: BLE001 - bookkeeping must never break a run
        log.exception("failed to record run outcome for run %s", run.id)
        return []


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
    finally:
        # The approval changed the evidence; the engine only drops its cache at run end.
        _invalidate_priors()


def _invalidate_priors() -> None:
    from tret.router_llm.priors import invalidate_all_priors

    invalidate_all_priors()


async def rescore_evaluated_runs(db: AsyncSession, run_ids) -> None:
    """Re-score the runs an evaluation just scored, reviewed or re-pass-ranked, so
    their outcome rows pick up (or drop) the evaluator term, then drop the priors
    caches. Never raises: it runs inside an evaluator tool call, a review request
    and a pass's completion, none of which may fail on bookkeeping.

    Works in its OWN session on the caller's engine, so neither a failure here nor
    its commits can expire or poison the caller's objects."""
    try:
        ids = {r for r in run_ids if r is not None}
        if ids:
            async with AsyncSession(db.bind, expire_on_commit=False) as own:
                for run_id in ids:
                    # An update of what the run's end already wrote, never a first
                    # write: recording a run's outcome the first time is the engine's job.
                    if await own.get(RunOutcome, (run_id, 0)) is None:
                        continue
                    run = await own.get(Run, run_id)
                    if run is not None:
                        await record_outcome(own, run, commit=True)
    except Exception:  # noqa: BLE001
        log.exception("failed to re-score evaluated runs")
    finally:
        _invalidate_priors()


def _batch_run_ids(targets: list[BatchTarget]) -> set[uuid.UUID]:
    """The current run and every earlier attempt's run (`history`) of the targets."""
    ids: set[uuid.UUID] = set()
    for t in targets:
        if t.run_id:
            ids.add(t.run_id)
        for h in t.history or []:
            try:
                if h.get("run_id"):
                    ids.add(uuid.UUID(str(h["run_id"])))
            except (AttributeError, ValueError):
                continue
    return ids


async def _quality_by_model(
    db: AsyncSession, run_ids: set[uuid.UUID]
) -> dict[str, dict[str, list[float]]]:
    """Per model: `quality` (every outcome row's quality_score) and `evaluated`
    (the non-null evaluated_quality_score values)."""
    rows = (
        await db.execute(
            select(
                RunOutcome.model_id, RunOutcome.quality_score, RunOutcome.evaluated_quality_score
            ).where(RunOutcome.run_id.in_(run_ids))
        )
    ).all()
    out: dict[str, dict[str, list[float]]] = {}
    for model_id, q, e in rows:
        m = out.setdefault(model_id, {"quality": [], "evaluated": []})
        m["quality"].append(float(q))
        if e is not None:
            m["evaluated"].append(float(e))
    return out


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


async def mark_packaging_approvals(
    db: AsyncSession, batch_ids: list[str], *, dry_run: bool
) -> dict:
    """Re-mark the 'review' approvals of approved `package_map` findings in the
    named batches as 'packaging', so they stop counting as quality evidence, then
    re-score every run of those batches that holds a packaging approval (whether
    re-marked now or by an earlier call). Safe to re-run: a second run changes no
    approvals and re-scores the same runs again, which repairs a re-score that
    failed the first time. A dry run performs no writes.

    A re-score is checked, not trusted: `record_outcome` swallows its own errors,
    so the stored `findings_approved` must equal the run's review-only count or the
    run is reported in `runs_failed`. A run with no outcome rows gets none.

    Returns `{"dry_run", "batches": [{"batch_id", "found", "approvals_changed",
    "runs_rescored", "runs_failed"}], "models": [{"model_id", "rows",
    "mean_before", "mean_after", "evaluated_rows", "evaluated_mean_before",
    "evaluated_mean_after"}]}`; the "after" values are None on a dry run. The
    evaluated means cover only rows whose evaluated_quality_score is not null."""
    per_batch: list[dict] = []
    marks: dict[uuid.UUID, Approval] = {}  # approval id -> row to flip
    rescore: dict[str, list[uuid.UUID]] = {}  # batch id -> runs to re-score
    names: list[str] = []
    for given in batch_ids:
        try:
            name = str(uuid.UUID(str(given)))  # one spelling per batch
        except ValueError:
            name = str(given)
        if name not in names:
            names.append(name)
    for raw in names:
        entry = {
            "batch_id": raw, "found": False, "approvals_changed": 0,
            "runs_rescored": 0, "runs_failed": [],
        }
        per_batch.append(entry)
        try:
            batch = await db.get(Batch, uuid.UUID(str(raw)))
        except ValueError:
            batch = None
        if batch is None:
            continue
        entry["found"] = True
        targets = (
            await db.execute(select(BatchTarget).where(BatchTarget.collection_id == batch.id))
        ).scalars().all()
        run_ids = _batch_run_ids(list(targets))
        if not run_ids:
            continue
        rows = (
            await db.execute(
                select(Finding.id, Finding.run_id).where(
                    Finding.run_id.in_(run_ids),
                    Finding.schema_slug == "package_map",
                    Finding.status == "approved",
                )
            )
        ).all()
        finding_run = {fid: rid for fid, rid in rows}
        deciding: dict[uuid.UUID, Approval] = {}
        if finding_run:
            for a in (
                await db.execute(
                    select(Approval)
                    .where(Approval.finding_id.in_(finding_run))
                    .order_by(Approval.created_at, Approval.id)
                )
            ).scalars():
                deciding[a.finding_id] = a  # latest wins
        touched: dict[uuid.UUID, None] = {}
        for fid, a in deciding.items():
            if a.action != "approve":
                continue
            if a.purpose == "review":
                marks[a.id] = a
                entry["approvals_changed"] += 1
                touched[finding_run[fid]] = None
            elif a.purpose == "packaging":
                touched[finding_run[fid]] = None  # re-marked earlier: still re-score
        # Only runs that already have outcome rows: a first write is the engine's job.
        # Plain ids in a deterministic order (never ORM objects, which a rollback
        # in a later re-score would expire).
        rescore[raw] = [
            r for r in sorted(touched, key=str) if await db.get(RunOutcome, (r, 0)) is not None
        ]

    affected = {r for runs in rescore.values() for r in runs}
    before = await _quality_by_model(db, affected) if affected else {}
    after: dict[str, dict[str, list[float]]] | None = None
    if not dry_run:
        for a in marks.values():
            a.purpose = "packaging"
        if marks:
            await db.commit()
        done: dict[uuid.UUID, bool] = {}
        for entry in per_batch:
            for run_id in rescore.get(entry["batch_id"], []):
                if run_id not in done:
                    done[run_id] = await _rescore_checked(db, run_id)
                if done[run_id]:
                    entry["runs_rescored"] += 1
                else:
                    entry["runs_failed"].append(str(run_id))
        if marks or affected:
            _invalidate_priors()
        after = await _quality_by_model(db, affected) if affected else {}
    else:
        for entry in per_batch:
            entry["runs_rescored"] = len(rescore.get(entry["batch_id"], []))
    models = []
    for m, vals in sorted(before.items()):
        post = None if after is None else after.get(m, {"quality": [], "evaluated": []})
        models.append(
            {
                "model_id": m,
                "rows": len(vals["quality"]),
                "mean_before": _mean(vals["quality"]),
                "mean_after": None if post is None else _mean(post["quality"]),
                # evaluated_quality_score is what batch-family routing evidence reads;
                # means are over the rows where it is not null.
                "evaluated_rows": len(vals["evaluated"]),
                "evaluated_mean_before": _mean(vals["evaluated"]),
                "evaluated_mean_after": None if post is None else _mean(post["evaluated"]),
            }
        )
    return {"dry_run": dry_run, "batches": per_batch, "models": models}


async def _rescore_checked(db: AsyncSession, run_id: uuid.UUID) -> bool:
    """Re-score one run in isolation and verify the stored result counts only
    review approvals. Rolls back and returns False on any failure."""
    try:
        run = await db.get(Run, run_id)
        if run is None:
            return False
        rows = await record_outcome(db, run, commit=True)
        if not rows:
            raise RuntimeError("no outcome rows were written")
        expected = (await _finding_counts(db, run_id)).get("approved", 0)
        stored = (
            await db.execute(
                select(RunOutcome.findings_approved).where(RunOutcome.run_id == run_id)
            )
        ).scalars().all()
        if sum(stored) != expected:
            raise RuntimeError(f"stored findings_approved {sum(stored)} != review count {expected}")
        return True
    except Exception:  # noqa: BLE001 - reported per run, never aborts the others
        log.exception("re-score after marking packaging approvals failed for run %s", run_id)
        with contextlib.suppress(Exception):
            await db.rollback()
        return False


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
        rows = await record_outcome(db, run)
        if rows:
            written += len(rows)
        else:
            skipped += 1
    await db.commit()
    return {"scanned": len(runs), "written": written, "skipped": skipped}
