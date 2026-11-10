"""The batch evaluator: scores each finished target of a batch with a judge model.

An evaluation pass (`BatchEvaluation`) freezes a rubric (`tret/batches/rubrics.py`),
a judge model and a budget, and holds one `TargetEvaluation` row per target. The
judge is an ordinary engine run of the seeded "Batch Evaluator" harness
(`services/workspace.py`): it is handed a blind evidence pack (the profile's
`evaluation_evidence`), may open the attached documents, and ends by calling
`submit_evaluation` (`engine/evaluation_tools.py`). When every target has been
judged, one more run reviews the whole batch (`submit_batch_review`).

How a pass moves:

- `start_evaluation` creates the pass and its rows (settled targets `pending`,
  infrastructure failures and timeouts `skipped`, unsettled targets `pending`
  until they settle) and schedules `_pump`. With `settings.evaluation.enabled`
  `core.start` creates the pass itself, and `core._settle_target` calls
  `on_target_settled` after each settle, so targets are judged as they finish.
- `_pump` launches pending rows of settled targets up to
  `TRET_BATCH_EVALUATOR_CONCURRENCY` evaluator runs process-wide, stopping at the
  budget (the rest become `not_evaluated_budget`). Each finished run adds its cost
  to the row and, atomically, to the pass, then re-ranks the pass
  (`scoring.recompute_relative`) and pumps again.
- A pass completes when nothing is pending or running and no target is
  unsettled (targets held at a gate do not count); a batch review runs first when
  at least two rows are scored. A later settle (a release, a retry) sets a
  completed pass back to `running`.

Evaluator runs carry `_batch_evaluation_id` in their task input, so
`services/outcomes.py` records nothing for them and the router learns nothing
from judging. Their cost is the pass's, never the targets': `core.rollup_parent_cost`
adds it to the parent run, `collection_out.cost_usd` stays target-only.

Test patch points are the batch orchestrator's: the session factory, the engine
and the cancel grace come from `core._compat()`.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import statistics
import uuid
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tret.batches import core, scoring
from tret.batches.profiles import profile_for
from tret.batches.rubrics import (
    EvaluationIn,
    RubricError,
    default_rubric,
    rubric_text,
    validate_custom_rubric,
)
from tret.config import get_settings
from tret.db.models import Batch, BatchEvaluation, BatchTarget, Run, TargetEvaluation, User
from tret.engine.evaluation_tools import (
    BATCH_REVIEW_KEY,
    EVALUATION_ID_KEY,
    TARGET_EVALUATION_ID_KEY,
)
from tret.packs.links import packs_for_harness, resolve_pack_for_task
from tret.providers.catalog import get_catalog
from tret.router_llm.outcomes import is_repeated_failure_stop

log = logging.getLogger("tret.evaluation")

ROW_STATUSES = ("pending", "running", "scored", "skipped", "failed", "not_evaluated_budget")
INPUT_TOKENS_PER_TARGET = 25_000
OUTPUT_TOKENS_PER_TARGET = 1_500
BUDGET_HEADROOM = 1.5
BUDGET_FLOOR_USD = 1.0
CONCURRENCY_MIN, CONCURRENCY_MAX = 1, 4
# Eight, not six: on cloud (2026-11-09) Gemini 3.8 Flash judges of large embedded
# annual reports (Barclays, NatWest) spent all six turns checking page-map
# boundaries with read_document/search_documents and never scored. The evidence
# pack now caps document reads at three, and a judge that still runs out of turns
# gets one relaunch that scores from the pack alone (OUT_OF_TURNS_INSTRUCTION).
EVALUATOR_MAX_ITERATIONS = 8
# Stamped on the one relaunch of an evaluator run whose judge answered in text.
RETRY_KEY = "_evaluator_retry"
RETRY_INSTRUCTION = (
    "\n\nYour previous reply did not call submit_evaluation, so no score was recorded. Call "
    "submit_evaluation now with your scores, difficulty, flags, confidence and rationale, and write "
    "no other reply. Call no other tool first: everything you need is in the evidence pack above."
)
# Stamped instead on the one relaunch of an evaluator run that used every turn on
# document tools without scoring.
OUT_OF_TURNS_INSTRUCTION = (
    "\n\nYour previous attempt used all its turns and recorded no score. Score "
    "from the evidence pack above now: call submit_evaluation with your scores, difficulty, flags, "
    "confidence and rationale, and write no other reply. Call no other tool first; where the pack "
    "leaves a doubt, give your best estimate and set confidence to low."
)
# Stamped instead on the one relaunch of an evaluator run the engine stopped for
# repeating the same failing tool call (`stopped: N identical failing calls`,
# engine/harness.py `_FailureStreak`). Before that stop such a judge ran on to
# its turn limit and was relaunched with the instruction above; it still gets
# exactly one relaunch.
STUCK_CALL_INSTRUCTION = (
    "\n\nYour previous attempt kept repeating a tool call that failed the same way and "
    "recorded no score. Score from the evidence pack above now: call submit_evaluation with "
    "your scores, difficulty, flags, confidence and rationale, and write no other reply. Call "
    "no other tool first; where the pack leaves a doubt, give your best estimate and set "
    "confidence to low."
)
# Turn limit of a relaunch: room for the score and one stray call, no more. A
# relaunch inherits the documents of the run it replaces, so without this a judge
# that ignores "call no other tool first" could spend a second full run on exactly
# the reports that are expensive to read, past what the budget projection counted.
RELAUNCH_MAX_ITERATIONS = 3
REVIEW_MAX_ITERATIONS = 3
AGREE_WITHIN_POINTS = 15.0
CARDS_MAX_CHARS = 12_000
CARD_RATIONALE_CHARS = 200
REVIEW_INSTRUCTION = (
    "Review the scores of this whole batch. Point out inconsistent scoring between similar cases, "
    "name the patterns behind low scores, and propose up to five lessons for future runs of this "
    "kind: concrete, actionable, each tied to evidence in the cards. Finish with submit_batch_review."
)
VERDICTS = ("agree", "disagree", "adjust")

# Per-loop state (see core.py: a restarted loop never inherits one bound to a dead loop).
_loop_state: dict[str, Any] = {
    "loop": None, "locks": {}, "inflight": 0, "active": set(), "reviewing": set(), "book": None,
    "unbooked": {},
}


def _state() -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    if _loop_state["loop"] is not loop:
        _loop_state.update(
            loop=loop, locks={}, inflight=0, active=set(), reviewing=set(), book=asyncio.Lock(),
            unbooked={},
        )
    return _loop_state


def _pass_lock(evaluation_id: uuid.UUID) -> asyncio.Lock:
    locks = _state()["locks"]
    if evaluation_id not in locks:
        locks[evaluation_id] = asyncio.Lock()
    return locks[evaluation_id]


def concurrency() -> int:
    try:
        n = int(get_settings().batch_evaluator_concurrency)
    except (TypeError, ValueError):
        n = 2
    return max(CONCURRENCY_MIN, min(CONCURRENCY_MAX, n))


def _session_factory():
    return core._session_factory()


def _now():
    return core._now()


def _f(value: Any) -> float | None:
    return None if value is None else float(value)


# ══════════════════════════════════════════════════════════════════════════
# judge, budget, settings
# ══════════════════════════════════════════════════════════════════════════
def judge_preferences() -> list[str]:
    """`TRET_BATCH_EVALUATOR_JUDGE`: an ordered, comma-separated preference list."""
    return [m.strip() for m in str(get_settings().batch_evaluator_judge or "").split(",") if m.strip()]


def check_judge_model(model: str) -> None:
    """422 when the catalog does not know `model` (no key check: that is made when a
    pass starts, against the keys of the day)."""
    if get_catalog().get(model.strip()) is None:
        raise HTTPException(422, f"unknown judge model '{model.strip()}'")


async def resolve_judge(db: AsyncSession, workspace_id: uuid.UUID, requested: str | None) -> str:
    """The judge for a pass. A named model must be in the catalog and its provider
    must have a key (the question the router asks of a per-run override); otherwise
    the first entry of the preference list that passes both. 422 with the reason
    when none does."""
    from tret.providers.catalog import ProviderRegistry
    from tret.services.credentials import load_db_keys

    registry = ProviderRegistry(await load_db_keys(db, workspace_id))
    catalog = get_catalog()
    named = (requested or "").strip()
    if named:
        info = catalog.get(named)
        if info is None:
            raise HTTPException(422, f"unknown judge model '{named}'")
        if not registry.has_key(info.provider):
            raise HTTPException(
                422, f"Judge model '{named}' is not available here: provider '{info.provider}' has no API key."
            )
        return named
    for candidate in judge_preferences():
        info = catalog.get(candidate)
        if info is not None and registry.has_key(info.provider):
            return candidate
    raise HTTPException(
        422,
        "No judge model is available: set TRET_BATCH_EVALUATOR_JUDGE to a model whose provider has a key.",
    )


def estimate_evaluation_cost(judge_model: str, n: int) -> float:
    """`n x (25,000 input + 1,500 output tokens)` at the judge's catalog prices."""
    info = get_catalog().get(judge_model)
    if info is None:
        return 0.0
    per_target = (
        INPUT_TOKENS_PER_TARGET * float(info.input_price_per_mtok)
        + OUTPUT_TOKENS_PER_TARGET * float(info.output_price_per_mtok)
    ) / 1_000_000
    return n * per_target


def default_budget(judge_model: str, n: int) -> float:
    return round(max(BUDGET_FLOOR_USD, BUDGET_HEADROOM * estimate_evaluation_cost(judge_model, n)), 2)


def build_settings(profile_slug: str, body: EvaluationIn | None) -> dict | None:
    """The `settings.evaluation` block stored on a batch at create time, or None
    when evaluation is off (nothing is stored then, so nothing else changes)."""
    if body is None or not body.enabled:
        return None
    if profile_slug not in ("disclosure", "prompt"):
        raise HTTPException(422, f"'{profile_slug}' batches cannot be evaluated")
    if body.rubric is not None:
        if profile_slug == "disclosure":
            raise HTTPException(422, "a disclosure batch always uses the disclosure-v1 rubric")
        try:
            rubric = validate_custom_rubric(body.rubric)
        except RubricError as e:
            raise HTTPException(422, str(e)) from e
    else:
        rubric = default_rubric(profile_slug)
    judge = (body.judge_model or "").strip() or None
    if judge is not None:
        check_judge_model(judge)
    return {
        "enabled": True,
        # None unless the creator named one: the pass resolves it when it starts, so a
        # later Re-evaluate follows the keys and the preference list of the day.
        "judge_model": judge,
        "budget_usd": body.budget_usd,
        "rubric_key": rubric["key"],
        "rubric": rubric,
    }


def batch_rubric(batch: Batch) -> dict:
    """The rubric a new pass over `batch` is frozen with: the batch's own, else the
    profile's default."""
    stored = ((batch.settings or {}).get("evaluation") or {}).get("rubric")
    if isinstance(stored, dict) and stored.get("criteria"):
        return stored
    try:
        return default_rubric(batch.profile or "disclosure")
    except KeyError as e:
        raise HTTPException(409, f"'{batch.profile}' batches cannot be evaluated") from e


# ══════════════════════════════════════════════════════════════════════════
# serializers
# ══════════════════════════════════════════════════════════════════════════
def review_agrees(row: TargetEvaluation) -> bool | None:
    review = row.review
    if not isinstance(review, dict):
        return None
    verdict = review.get("verdict")
    if verdict == "agree":
        return True
    if verdict == "adjust" and row.quality is not None and review.get("adjusted_quality") is not None:
        return abs(float(review["adjusted_quality"]) - float(row.quality)) <= AGREE_WITHIN_POINTS
    return False


async def pass_summary(
    db: AsyncSession, ev: BatchEvaluation, rows: list[TargetEvaluation] | None = None
) -> dict:
    rows = rows if rows is not None else await scoring.pass_rows(db, ev.id)
    counts = {s: 0 for s in ROW_STATUSES}
    for r in rows:
        counts[r.status] = counts.get(r.status, 0) + 1
    reviewed = [r for r in rows if r.status == "scored" and isinstance(r.review, dict)]
    agreeing = sum(1 for r in reviewed if review_agrees(r))
    to_judge = sum(1 for r in rows if r.status != "skipped")
    return {
        "id": str(ev.id),
        "status": ev.status,
        "judge_model": ev.judge_model,
        "rubric_key": ev.rubric_key,
        "rubric_version": ev.rubric_version,
        "budget_usd": _f(ev.budget_usd),
        "cost_usd": round(float(ev.cost_usd or 0), 6),
        "estimated_cost_usd": round(estimate_evaluation_cost(ev.judge_model, to_judge), 4),
        "counts": counts,
        "spot_checks": len(reviewed),
        "agreement": round(agreeing / len(reviewed), 4) if reviewed else None,
        "summary": ev.summary,
        "error": ev.error,
        "created_at": core._iso(ev.created_at),
        "finished_at": core._iso(ev.finished_at),
    }


def target_evaluation_out(row: TargetEvaluation, target: BatchTarget | None) -> dict:
    return {
        "id": str(row.id),
        "target_id": str(row.target_id),
        "position": target.position if target is not None else None,
        "label": target.label if target is not None else None,
        "row_key": row.row_key,
        "attempt": row.attempt,
        "stale": scoring.is_stale(row, target) if target is not None else False,
        "status": row.status,
        "skip_reason": row.skip_reason,
        "error": row.error,
        "scores": row.scores or {},
        "quality": _f(row.quality),
        "difficulty": row.difficulty,
        "flags": list(row.flags or []),
        "confidence": row.confidence,
        "rationale": row.rationale,
        "relative": row.relative,
        "review": row.review,
        "cost_usd": round(float(row.cost_usd or 0), 6),
        "evaluator_run_id": str(row.evaluator_run_id) if row.evaluator_run_id else None,
        "finished_at": core._iso(row.finished_at),
    }


def target_brief(row: TargetEvaluation, target: BatchTarget) -> dict:
    rel = row.relative if isinstance(row.relative, dict) else {}
    return {
        "status": row.status,
        "quality": _f(row.quality),
        "rank": rel.get("rank"),
        "of": rel.get("of"),
        "percentile": rel.get("percentile"),
        "residual": rel.get("residual"),
        "flags": list(row.flags or []),
        "confidence": row.confidence,
        "stale": scoring.is_stale(row, target),
    }


async def target_briefs(db: AsyncSession, batch: Batch, targets: list[BatchTarget]) -> dict[str, dict]:
    """The per-target `evaluation` block of the batch detail, by target id."""
    current = await scoring.current_pass(db, batch.id)
    if current is None:
        return {}
    by_id = {t.id: t for t in targets}
    return {
        str(r.target_id): target_brief(r, by_id[r.target_id])
        for r in await scoring.pass_rows(db, current.id)
        if r.target_id in by_id
    }


async def evaluation_overview(db: AsyncSession, batch_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict]:
    """`evaluation_cost_usd`, `evaluation_status` and `quality_mean` for each batch
    (the list and detail serializers), in three queries for any number of batches."""
    if not batch_ids:
        return {}
    passes = list(
        (
            await db.execute(
                select(BatchEvaluation)
                .where(BatchEvaluation.collection_id.in_(batch_ids))
                .order_by(BatchEvaluation.created_at.desc())
                .execution_options(populate_existing=True)
            )
        ).scalars()
    )
    if not passes:
        return {}
    cost: dict[uuid.UUID, float] = {}
    by_batch: dict[uuid.UUID, list[BatchEvaluation]] = {}
    for p in passes:
        cost[p.collection_id] = cost.get(p.collection_id, 0.0) + float(p.cost_usd or 0)
        by_batch.setdefault(p.collection_id, []).append(p)
    current: dict[uuid.UUID, BatchEvaluation] = {}
    for cid, items in by_batch.items():
        current[cid] = next((p for p in items if p.status not in scoring.DEAD_PASS_STATUSES), items[0])
    rows = list(
        (
            await db.execute(
                select(TargetEvaluation)
                .where(
                    TargetEvaluation.evaluation_id.in_([p.id for p in current.values()]),
                    TargetEvaluation.status == "scored",
                )
                .execution_options(populate_existing=True)
            )
        ).scalars()
    )
    attempts = {
        tid: n
        for tid, n in (
            await db.execute(select(BatchTarget.id, BatchTarget.attempts).where(BatchTarget.collection_id.in_(batch_ids)))
        ).all()
    }
    qualities: dict[uuid.UUID, list[float]] = {}
    for r in rows:
        if r.quality is not None and (r.attempt or 0) == (attempts.get(r.target_id) or 0):
            qualities.setdefault(r.evaluation_id, []).append(float(r.quality))
    out: dict[uuid.UUID, dict] = {}
    for cid in by_batch:
        q = qualities.get(current[cid].id)
        out[cid] = {
            "cost": round(cost[cid], 6),
            "status": current[cid].status,
            "quality_mean": round(statistics.fmean(q), 1) if q else None,
        }
    return out


async def evaluation_detail(db: AsyncSession, batch: Batch) -> dict:
    """`GET /batches/{id}/evaluation`."""
    passes = list(
        (
            await db.execute(
                select(BatchEvaluation)
                .where(BatchEvaluation.collection_id == batch.id)
                .order_by(BatchEvaluation.created_at.desc())
                .execution_options(populate_existing=True)
            )
        ).scalars()
    )
    current = await scoring.current_pass(db, batch.id)
    rubric = None
    targets_out: list[dict] = []
    summary = None
    if current is not None:
        rubric = current.rubric
        rows = await scoring.pass_rows(db, current.id)
        targets = {t.id: t for t in await core.load_targets(db, [batch.id])}
        summary = await pass_summary(db, current, rows)
        targets_out = [
            target_evaluation_out(r, targets.get(r.target_id))
            for r in sorted(rows, key=lambda r: targets[r.target_id].position if r.target_id in targets else 0)
        ]
    elif ((batch.settings or {}).get("evaluation") or {}).get("enabled"):
        rubric = batch_rubric(batch)
    return {
        "evaluation": summary,
        "rubric": rubric,
        "passes": [
            {"id": str(p.id), "status": p.status, "judge_model": p.judge_model, "created_at": core._iso(p.created_at)}
            for p in passes
        ],
        "targets": targets_out,
    }


# ══════════════════════════════════════════════════════════════════════════
# starting a pass
# ══════════════════════════════════════════════════════════════════════════
def _new_row(ev: BatchEvaluation, batch: Batch, t: BatchTarget, **over: Any) -> TargetEvaluation:
    return TargetEvaluation(
        id=uuid.uuid4(),
        evaluation_id=ev.id,
        collection_id=batch.id,
        workspace_id=batch.workspace_id,
        target_id=t.id,
        row_key=scoring.row_key(batch, t),
        rubric_key=ev.rubric_key,
        family_key=ev.family_key,
        attempt=t.attempts or 0,
        evaluated_run_id=t.run_id,
        status="pending",
        scores={},
        flags=[],
        cost_usd=Decimal(0),
        created_at=_now(),
        **over,
    )


INFRA_SKIP = "infrastructure failure: not the run's fault"


async def start_evaluation(
    db: AsyncSession,
    batch: Batch,
    *,
    user_id: uuid.UUID | None,
    judge_model: str | None = None,
    budget_usd: float | None = None,
    only_missing: bool = True,
    target_ids: list[uuid.UUID] | None = None,
    record_judge_failure: bool = False,
) -> BatchEvaluation:
    """Create an evaluation pass over `batch` and schedule its work. 409 while the
    batch has a running pass; 422 for an unknown judge. `only_missing` keeps the
    scores of the previous pass that still stand (same judge, same rubric, same
    attempt of the target); `target_ids` limits the re-judging to those targets and
    keeps the others as they were."""
    from tret.services.workspace import ensure_evaluator_harness

    # Serialise starters on the batch row (a double click, or the auto pass racing a
    # manual Evaluate); the partial unique index on running passes is the backstop.
    await db.execute(select(Batch.id).where(Batch.id == batch.id).with_for_update())
    running = (
        await db.execute(
            select(BatchEvaluation.id).where(
                BatchEvaluation.collection_id == batch.id, BatchEvaluation.status == "running"
            )
        )
    ).first()
    if running is not None:
        raise HTTPException(409, "An evaluation pass is already running for this batch; stop it first")
    profile = profile_for(batch)
    stored = (batch.settings or {}).get("evaluation") or {}
    rubric = batch_rubric(batch)
    judge_error: str | None = None
    try:
        judge = await resolve_judge(db, batch.workspace_id, judge_model or stored.get("judge_model"))
    except HTTPException as e:
        if not record_judge_failure:
            raise
        # The auto pass of a starting batch: the batch must start, and the pass
        # shows why it cannot judge.
        judge_error = str(e.detail)
        judge = (judge_model or stored.get("judge_model") or (judge_preferences() or ["unavailable"])[0]).strip()
    targets = await core.load_targets(db, [batch.id])
    selected = set(target_ids) if target_ids is not None else None
    if selected is not None and not selected <= {t.id for t in targets}:
        raise HTTPException(404, "Target not found")
    if judge_error is not None:
        failed = BatchEvaluation(
            id=uuid.uuid4(), collection_id=batch.id, workspace_id=batch.workspace_id,
            family_key=scoring.family_key(batch), rubric_key=rubric["key"], rubric_version=rubric["version"],
            rubric=rubric, judge_model=judge, status="failed", cost_usd=Decimal(0), error=judge_error,
            created_by=user_id or batch.created_by, created_at=_now(), finished_at=_now(),
        )
        db.add(failed)
        await db.commit()
        return failed
    previous = await scoring.current_pass(db, batch.id)
    previous_rows: dict[uuid.UUID, TargetEvaluation] = {}
    if (
        previous is not None
        and previous.judge_model == judge
        and previous.rubric_key == rubric["key"]
        and previous.rubric_version == rubric["version"]
    ):
        previous_rows = {r.target_id: r for r in await scoring.pass_rows(db, previous.id)}

    ev = BatchEvaluation(
        id=uuid.uuid4(),
        collection_id=batch.id,
        workspace_id=batch.workspace_id,
        family_key=scoring.family_key(batch),
        rubric_key=rubric["key"],
        rubric_version=rubric["version"],
        rubric=rubric,
        judge_model=judge,
        status="running",
        cost_usd=Decimal(0),
        created_by=user_id or batch.created_by,
        created_at=_now(),
    )
    can_evaluate = getattr(profile, "evaluation_evidence", None) is not None
    rows: list[TargetEvaluation] = []
    for t in targets:
        cls = scoring.target_class(t)
        row = _new_row(ev, batch, t)
        prior = previous_rows.get(t.id)
        keep = (only_missing and selected is None) or (selected is not None and t.id not in selected)
        if not can_evaluate:
            row.status, row.skip_reason = "skipped", "profile has no evidence pack"
        elif cls in ("infra", "timeout"):
            row.status, row.skip_reason = "skipped", INFRA_SKIP
        elif (
            keep
            and prior is not None
            and prior.status == "scored"
            and prior.attempt == (t.attempts or 0)
            and prior.evaluated_run_id == t.run_id
        ):
            for field in ("scores", "quality", "difficulty", "flags", "confidence", "rationale", "review", "evaluator_run_id", "finished_at"):
                setattr(row, field, getattr(prior, field))
            row.status = "scored"
        elif selected is not None and t.id not in selected:
            row.status, row.skip_reason = "skipped", "not selected for this pass"
        rows.append(row)
    evaluable = sum(1 for r in rows if r.status in ("pending",))
    budget = budget_usd if budget_usd is not None else stored.get("budget_usd")
    if budget is None:
        budget = default_budget(judge, evaluable or len(targets))
    ev.budget_usd = Decimal(str(round(float(budget), 4)))
    if not any(r.status == "pending" for r in rows):
        # Nothing left to judge: complete at once.
        ev.status, ev.finished_at = "completed", _now()
    db.add(ev)
    try:
        await db.flush()
        for r in rows:
            db.add(r)
        await ensure_evaluator_harness(db, batch.workspace_id)
        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        raise HTTPException(409, "An evaluation pass is already running for this batch; stop it first") from e
    if ev.status == "running":
        core._spawn(_pump(ev.id))
    else:
        core._spawn(_recompute_in_background(ev.id))
    return ev


async def _recompute_in_background(evaluation_id: uuid.UUID) -> None:
    with contextlib.suppress(Exception):
        async with _session_factory()() as db:
            ev = await db.get(BatchEvaluation, evaluation_id)
            if ev is not None:
                await scoring.recompute_relative(db, ev)


# ══════════════════════════════════════════════════════════════════════════
# the pump
# ══════════════════════════════════════════════════════════════════════════
async def _pump(evaluation_id: uuid.UUID) -> None:
    """Launch what can be launched for a pass and finish it when nothing is left.
    Never raises."""
    try:
        async with _pass_lock(evaluation_id):
            await _pump_locked(evaluation_id)
    except Exception:  # noqa: BLE001 - a background task has no caller to raise to
        log.exception("evaluation pass %s could not be pumped", evaluation_id)


async def _pump_locked(evaluation_id: uuid.UUID) -> None:
    state = _state()
    launches: list[tuple[uuid.UUID, uuid.UUID, uuid.UUID]] = []
    try:
        finish_review = await _pump_inner(evaluation_id, launches)
    except BaseException:
        # Nothing was committed for these launches: give their slots back.
        state["inflight"] = max(0, state["inflight"] - len(launches))
        for _ev, _row, run_id in launches:
            state["unbooked"].get(evaluation_id, set()).discard(run_id)
        state["reviewing"].discard(evaluation_id)
        raise
    for evaluation, row_id, run_id in launches:
        core._spawn(_run_evaluator(evaluation, row_id, run_id))
    if finish_review:
        core._spawn(_run_batch_review(evaluation_id))


async def _pump_inner(evaluation_id: uuid.UUID, launches: list[tuple[uuid.UUID, uuid.UUID, uuid.UUID]]) -> bool:
    """One pump pass under the pass lock; fills `launches` and returns whether the
    batch review should start."""
    state = _state()
    finish_review = False
    async with _session_factory()() as db:
        ev = await db.get(BatchEvaluation, evaluation_id, populate_existing=True)
        if ev is None or ev.status != "running":
            _forget_pass(evaluation_id)
            return False
        state["active"].add(evaluation_id)
        batch = await db.get(Batch, ev.collection_id)
        if batch is None:
            return False
        profile = profile_for(batch)
        targets = {t.id: t for t in await core.load_targets(db, [batch.id])}
        rows = sorted(
            await scoring.pass_rows(db, ev.id),
            key=lambda r: targets[r.target_id].position if r.target_id in targets else 0,
        )
        if not core.draining():
            from tret.services.workspace import ensure_evaluator_harness

            harness = await ensure_evaluator_harness(db, ev.workspace_id)
            pack = resolve_pack_for_task(await packs_for_harness(db, harness), "freeform")
            budget = float(ev.budget_usd) if ev.budget_usd is not None else None
            per_target = estimate_evaluation_cost(ev.judge_model, 1)
            # Committed spend, plus an estimate for every judge whose cost is not on the
            # pass yet and for each one launched below. A judge that has already scored
            # its row (the tool commits before the run ends) is still unbooked, so this
            # counts runs, not row statuses.
            #
            # Order matters: snapshot the unbooked runs first, then read the cost. A run
            # is booked (committed) before it leaves the set, so a run missing from the
            # snapshot is already in the cost read after it; reading the cost earlier
            # (before the awaits above) could miss a run on both sides and let a second
            # judge slip past the budget.
            unbooked = len(state["unbooked"].get(evaluation_id, ()))
            await db.refresh(ev)
            projected = float(ev.cost_usd or 0) + per_target * unbooked
            for idx, row in enumerate(rows):
                if row.status != "pending":
                    continue
                t = targets.get(row.target_id)
                if t is None or t.status not in core.TERMINAL_TARGET_STATUSES:
                    continue
                if row.attempt != (t.attempts or 0) or row.evaluated_run_id != t.run_id:
                    # The target settled outside the settle hook (a crash, a task that
                    # could not be built) or was retried: arm the row for its current run.
                    row.attempt, row.evaluated_run_id = t.attempts or 0, t.run_id
                if scoring.target_class(t) in ("infra", "timeout"):
                    row.status, row.skip_reason, row.finished_at = "skipped", INFRA_SKIP, _now()
                    continue
                if state["inflight"] >= concurrency():
                    break
                if budget is not None and projected + per_target > budget:
                    for rest in rows[idx:]:
                        if rest.status == "pending":
                            rest.status = "not_evaluated_budget"
                            rest.finished_at = _now()
                    break
                state["inflight"] += 1  # reserved before any await, released when the run ends
                projected += per_target
                try:
                    evaluator_run = await _create_evaluator_run(db, ev, batch, profile, t, row, harness, pack)
                except Exception as e:  # noqa: BLE001 - one row's evidence must not stop the pass
                    state["inflight"] -= 1
                    log.exception("evaluation row %s could not build its evidence pack", row.id)
                    row.status = "failed"
                    row.error = core.summarise_run_error(f"evidence pack failed: {type(e).__name__}: {e}")
                    row.finished_at = _now()
                    continue
                state["unbooked"].setdefault(evaluation_id, set()).add(evaluator_run.id)
                launches.append((ev.id, row.id, evaluator_run.id))
            await db.commit()
        else:
            await db.commit()
        # Completion check on fresh facts.
        if not launches:
            fresh_rows = rows
            running = any(r.status == "running" for r in fresh_rows)
            blocking = any(
                r.status == "pending" and targets.get(r.target_id) is not None and targets[r.target_id].status != "held"
                for r in fresh_rows
            )
            unsettled = any(
                t.status in scoring.UNSETTLED_STATUSES and t.status != "held" for t in targets.values()
            )
            # A draining process leaves the pass running for the boot pump.
            if (
                not running and not blocking and not unsettled
                and evaluation_id not in state["reviewing"] and not core.draining()
            ):
                scored = sum(1 for r in fresh_rows if r.status == "scored")
                if scored >= 2:
                    state["reviewing"].add(evaluation_id)
                    finish_review = True
                else:
                    ev.status = "completed"
                    ev.finished_at = _now()
                    await db.commit()
                    _forget_pass(evaluation_id)
                    await scoring.recompute_relative(db, ev)
    return finish_review


async def _create_evaluator_run(
    db: AsyncSession, ev: BatchEvaluation, batch: Batch, profile: Any, t: BatchTarget,
    row: TargetEvaluation, harness: Any, pack: Any,
) -> Run:
    target_run = await db.get(Run, t.run_id) if t.run_id else None
    findings = (await core.load_review_findings(db, profile, [t], batch)).get(t.run_id, []) if t.run_id else []
    text, document_ids = await profile.evaluation_evidence(
        db, batch, t, target_run, findings, rubric=ev.rubric
    )
    run = Run(
        id=uuid.uuid4(),
        project_id=batch.project_id,
        harness_id=harness.id,
        pack_id=pack.id if pack else None,
        task_type="freeform",
        task_input={
            "message": text,
            "_label": f"evaluate:{t.slug}",
            EVALUATION_ID_KEY: str(ev.id),
            TARGET_EVALUATION_ID_KEY: str(row.id),
            "_model_override": ev.judge_model,
            "_max_iterations": EVALUATOR_MAX_ITERATIONS,
        },
        document_ids=list(document_ids),
        created_by=ev.created_by or batch.created_by,
    )
    db.add(run)
    await db.flush()  # the run first: the row points at it
    row.status = "running"
    row.evaluator_run_id = run.id
    row.evaluated_run_id = t.run_id
    row.attempt = t.attempts or 0
    row.error = None
    row.skip_reason = None
    row.finished_at = None
    return run


async def _execute_run(run_id: uuid.UUID) -> bool:
    """Execute an evaluator run under the evaluator time limit; True when it timed
    out (the run is then closed out as cancelled). The same cancel/teardown dance
    as `core._run_target`."""
    engine = core._compat().get_harness_engine()
    timeout = max(1, int(get_settings().batch_evaluator_run_timeout_seconds))
    task = asyncio.create_task(engine.execute(run_id))
    done, _ = await asyncio.wait({task}, timeout=timeout)
    if done:
        task.result()
        return False
    engine.cancel(run_id)
    done, _ = await asyncio.wait({task}, timeout=core._compat().CANCEL_GRACE_SECONDS)
    if not done:
        task.cancel()
        await asyncio.wait({task}, timeout=core._compat().CANCEL_GRACE_SECONDS)
    await core._close_out_run(run_id, "timeout: stopped by the evaluator's time limit")
    forget = getattr(engine, "forget_cancelled", None)
    if forget is not None:
        forget(run_id)
    return True


async def _book_cost(
    db: AsyncSession, evaluation_id: uuid.UUID, run: Run | None, row: TargetEvaluation | None = None
) -> Decimal:
    """Put an evaluator run's spend on the pass (atomically, in SQL) and on its row,
    once: the pass remembers the runs it has booked, so the normal end of a run, a
    cancel and the restart sweep can all call this without double counting. Does not
    commit the row change itself; the pass's booking is committed here, under a lock,
    so two runs booking at once never overwrite each other's entry."""
    if run is None:
        return Decimal(0)
    _state()
    async with _loop_state["book"]:
        ev = await db.get(BatchEvaluation, evaluation_id, populate_existing=True, with_for_update=True)
        key = str(run.id)
        if ev is None or key in (ev.booked_runs or []):
            return Decimal(0)
        cost = Decimal(str(round(core.run_cost(run), 6)))
        await db.execute(
            update(BatchEvaluation)
            .where(BatchEvaluation.id == evaluation_id)
            .values(cost_usd=BatchEvaluation.cost_usd + cost, booked_runs=[*(ev.booked_runs or []), key])
            .execution_options(synchronize_session=False)
        )
        if row is not None and row.evaluator_run_id == run.id:
            row.cost_usd = (row.cost_usd or Decimal(0)) + cost
        await db.commit()
    return cost


async def _book_after_cancel(evaluation_id: uuid.UUID, row_id: uuid.UUID, run_id: uuid.UUID) -> None:
    """The cost of an evaluator run whose task was cancelled (shutdown): the work
    was paid for even though nothing will read it."""
    with contextlib.suppress(Exception):
        async with _session_factory()() as db:
            run = await db.get(Run, run_id, populate_existing=True)
            row = await db.get(TargetEvaluation, row_id, populate_existing=True)
            await _book_cost(db, evaluation_id, run, row)
            await db.commit()


async def _run_evaluator(evaluation_id: uuid.UUID, row_id: uuid.UUID, run_id: uuid.UUID) -> None:
    """One target's evaluator run, start to settle. Never raises (except
    cancellation)."""
    cancelled = False
    state = _state()
    try:
        if not await _pass_still_running(evaluation_id, run_id):
            return  # cancelled between the pump's commit and this start
        await _execute_run(run_id)
    except asyncio.CancelledError:
        cancelled = True
        await asyncio.shield(_book_after_cancel(evaluation_id, row_id, run_id))
        raise
    except Exception:  # noqa: BLE001 - recorded on the row below
        log.exception("evaluator run %s crashed", run_id)
    finally:
        # The slot is held until the run's cost is on the pass, so a pump that
        # slips in between never budgets against a spend it cannot see yet.
        retry_id = None
        if not cancelled:
            try:
                retry_id = await _after_evaluator_run(evaluation_id, row_id, run_id)
            except Exception:  # noqa: BLE001
                log.exception("evaluation row %s could not be settled", row_id)
        if retry_id is not None:
            state["inflight"] += 1  # the relaunch keeps a slot of its own
            state["unbooked"].setdefault(evaluation_id, set()).add(retry_id)
            core._spawn(_run_evaluator(evaluation_id, row_id, retry_id))
        state["inflight"] = max(0, state["inflight"] - 1)
        state["unbooked"].get(evaluation_id, set()).discard(run_id)  # booked, or never will be
        if not cancelled:
            await _pump_all(evaluation_id)


async def _pass_still_running(evaluation_id: uuid.UUID, run_id: uuid.UUID) -> bool:
    """False (and the run closed out) when the pass was cancelled before this
    evaluator run began."""
    async with _session_factory()() as db:
        ev = await db.get(BatchEvaluation, evaluation_id, populate_existing=True)
        if ev is not None and ev.status == "running":
            return True
    await core._close_out_run(run_id, "evaluation cancelled before this run started")
    return False


async def _pump_all(first: uuid.UUID) -> None:
    """Pump this pass, then every other active pass (a freed evaluator slot may
    belong to any of them)."""
    await _pump(first)
    for other in list(_state()["active"]):
        if other != first:
            await _pump(other)


async def _after_evaluator_run(
    evaluation_id: uuid.UUID, row_id: uuid.UUID, run_id: uuid.UUID
) -> uuid.UUID | None:
    """Settle a finished evaluator run: book its cost, fail the row when it never
    scored. A judge that finished cleanly but answered in text (no
    `submit_evaluation`), or that ran out of turns before scoring, gets one relaunch
    with an instruction appended (if the budget has room for it); the id of that run
    is returned for the caller to start.

    The row's fate is decided under the pass lock on a fresh read, and only if the
    row still belongs to this run: a retry re-arm or a cancel that committed while the
    run was finishing wins."""
    async with _session_factory()() as db:
        run = await db.get(Run, run_id, populate_existing=True)
        row = await db.get(TargetEvaluation, row_id, populate_existing=True)
        await _book_cost(db, evaluation_id, run, row)
        await db.commit()
    relaunch: uuid.UUID | None = None
    async with _pass_lock(evaluation_id), _session_factory()() as db:
        run = await db.get(Run, run_id, populate_existing=True)
        row = await db.get(TargetEvaluation, row_id, populate_existing=True)
        ev = await db.get(BatchEvaluation, evaluation_id, populate_existing=True)
        if row is not None and ev is not None and row.evaluator_run_id == run_id and row.status == "running":
            target = await db.get(BatchTarget, row.target_id, populate_existing=True)
            current = (
                target is not None
                and row.attempt == (target.attempts or 0)
                and row.evaluated_run_id == target.run_id
            )
            first_try = not (run.task_input or {}).get(RETRY_KEY)
            out_of_turns = run.status == "failed" and str(run.error or "").startswith("max_iterations")
            stuck = run.status == "failed" and is_repeated_failure_stop(run.error)
            if not current:
                # The target moved on (a retry is under way): hand the row back to the
                # pump, which arms it for the target's current run once it settles.
                row.status, row.evaluator_run_id = "pending", None
                if target is not None:
                    row.attempt, row.evaluated_run_id = target.attempts or 0, target.run_id
            elif (
                first_try and ev.status == "running" and not core.draining()
                and (run.status in core.SUCCESS_RUN_STATUSES or out_of_turns or stuck)
            ):
                per_target = estimate_evaluation_cost(ev.judge_model, 1)
                others = len(_state()["unbooked"].get(evaluation_id, set()) - {run_id})
                if ev.budget_usd is not None and float(ev.cost_usd or 0) + per_target * (others + 1) > float(
                    ev.budget_usd
                ):
                    row.status, row.finished_at = "not_evaluated_budget", _now()
                else:
                    again = Run(
                        id=uuid.uuid4(),
                        project_id=run.project_id,
                        harness_id=run.harness_id,
                        pack_id=run.pack_id,
                        task_type=run.task_type,
                        task_input={
                            **run.task_input,
                            "message": str(run.task_input.get("message") or "")
                            + (
                                OUT_OF_TURNS_INSTRUCTION
                                if out_of_turns
                                else STUCK_CALL_INSTRUCTION
                                if stuck
                                else RETRY_INSTRUCTION
                            ),
                            RETRY_KEY: True,
                            "_max_iterations": RELAUNCH_MAX_ITERATIONS,
                        },
                        document_ids=list(run.document_ids or []),
                        created_by=run.created_by,
                    )
                    db.add(again)
                    await db.flush()
                    row.evaluator_run_id = again.id
                    relaunch = again.id
            else:
                row.status = "failed"
                row.error = (
                    core.summarise_run_error(run.error) if run is not None and run.error else None
                ) or "the evaluator did not submit a score"
                row.finished_at = _now()
        await db.commit()
    if relaunch is not None:
        return relaunch
    async with _session_factory()() as db:
        ev = await db.get(BatchEvaluation, evaluation_id, populate_existing=True)
        batch = await db.get(Batch, ev.collection_id) if ev is not None else None
        if ev is not None:
            await scoring.recompute_relative(db, ev)
        if batch is not None:
            await core.rollup_parent_cost(db, batch)
    return None


# ══════════════════════════════════════════════════════════════════════════
# the batch review
# ══════════════════════════════════════════════════════════════════════════
def review_cards(ev: BatchEvaluation, rows: list[TargetEvaluation], targets: dict[uuid.UUID, BatchTarget]) -> str:
    """The rubric and one compact block per scored target (no model, no spend),
    cut at 12,000 characters."""
    parts = [rubric_text(ev.rubric or {}), "", "SCORED TARGETS"]
    used = len(parts[0])
    for r in sorted(rows, key=lambda r: targets[r.target_id].position if r.target_id in targets else 0):
        if r.status != "scored":
            continue
        t = targets.get(r.target_id)
        scores = ", ".join(
            f"{k}={'n/a' if (v or {}).get('score') is None else v['score']}" for k, v in (r.scores or {}).items()
        )
        rel = r.relative or {}
        card = (
            f"- {t.label if t else r.row_key} [{scoring.target_class(t) if t else '?'}] "
            f"quality {float(r.quality):.1f}; rank {rel.get('rank')}/{rel.get('of')}; "
            f"difficulty {r.difficulty}; confidence {r.confidence}; flags: {', '.join(r.flags or []) or 'none'}\n"
            f"  scores: {scores}\n"
            f"  why: {' '.join((r.rationale or '').split())[:CARD_RATIONALE_CHARS]}"
        )
        if used + len(card) > CARDS_MAX_CHARS:
            parts.append("[further targets not shown: size limit]")
            break
        parts.append(card)
        used += len(card) + 1
    return "\n".join(parts) + f"\n\n{REVIEW_INSTRUCTION}"


async def _run_batch_review(evaluation_id: uuid.UUID) -> None:
    """The whole-batch review run, then the pass completes (or goes back to
    `running` when targets settled meanwhile). Never raises (except
    cancellation)."""
    state = _state()
    cancelled = False
    error: str | None = None
    run_id: uuid.UUID | None = None
    try:
        async with _session_factory()() as db:
            ev = await db.get(BatchEvaluation, evaluation_id, populate_existing=True)
            batch = await db.get(Batch, ev.collection_id) if ev is not None else None
            if ev is None or batch is None or ev.status != "running":
                return
            from tret.services.workspace import ensure_evaluator_harness

            harness = await ensure_evaluator_harness(db, ev.workspace_id)
            pack = resolve_pack_for_task(await packs_for_harness(db, harness), "freeform")
            targets = {t.id: t for t in await core.load_targets(db, [batch.id])}
            cards = review_cards(ev, await scoring.pass_rows(db, ev.id), targets)
            run = Run(
                id=uuid.uuid4(),
                project_id=batch.project_id,
                harness_id=harness.id,
                pack_id=pack.id if pack else None,
                task_type="freeform",
                task_input={
                    "message": cards,
                    "_label": "evaluate:batch",
                    EVALUATION_ID_KEY: str(ev.id),
                    BATCH_REVIEW_KEY: True,
                    "_model_override": ev.judge_model,
                    "_max_iterations": REVIEW_MAX_ITERATIONS,
                },
                document_ids=[],
                created_by=ev.created_by or batch.created_by,
            )
            db.add(run)
            await db.flush()
            ev.summary = None
            ev.summary_run_id = run.id  # the only run `submit_batch_review` will accept
            ev.error = None
            await db.commit()
            run_id = run.id
        await _execute_run(run_id)
    except asyncio.CancelledError:
        cancelled = True
        raise
    except Exception as e:  # noqa: BLE001 - the scores stay; the review is the extra
        log.exception("batch review of evaluation %s crashed", evaluation_id)
        error = core.summarise_run_error(f"{type(e).__name__}: {e}")
    finally:
        if not cancelled:
            try:
                await _finish_review(evaluation_id, run_id, error)
            except Exception:  # noqa: BLE001
                log.exception("evaluation pass %s could not finish its review", evaluation_id)
            state["reviewing"].discard(evaluation_id)
            await _pump_all(evaluation_id)


async def _finish_review(evaluation_id: uuid.UUID, run_id: uuid.UUID | None, error: str | None) -> None:
    async with _pass_lock(evaluation_id), _session_factory()() as db:
        ev = await db.get(BatchEvaluation, evaluation_id, populate_existing=True)
        if ev is None:
            return
        run = await db.get(Run, run_id, populate_existing=True) if run_id else None
        await _book_cost(db, evaluation_id, run)
        await db.commit()
        await db.refresh(ev)
        if ev.status != "running":
            return
        if ev.summary is None:
            ev.error = error or (core.summarise_run_error(run.error) if run is not None and run.error else None) or (
                "the evaluator did not submit a batch review"
            )
        # Targets that settled during the review keep the pass open: the pump
        # reruns the review at the end and replaces this summary.
        rows = await scoring.pass_rows(db, ev.id)
        targets = await core.load_targets(db, [ev.collection_id])
        by_id = {t.id: t for t in targets}
        waiting = any(
            r.status in ("pending", "running") and by_id.get(r.target_id) is not None and by_id[r.target_id].status != "held"
            for r in rows
        ) or any(t.status in scoring.UNSETTLED_STATUSES and t.status != "held" for t in targets)
        # A draining process leaves the pass running for the boot pump.
        if not waiting and not core.draining():
            ev.status = "completed"
            ev.finished_at = _now()
        await db.commit()
        if ev.status != "running":
            _forget_pass(evaluation_id)
        await scoring.recompute_relative(db, ev)
        batch = await db.get(Batch, ev.collection_id)
        if batch is not None:
            await core.rollup_parent_cost(db, batch)


# ══════════════════════════════════════════════════════════════════════════
# settle hook, cancel, review, boot
# ══════════════════════════════════════════════════════════════════════════
async def has_running_pass(db: AsyncSession, collection_id: uuid.UUID) -> bool:
    return (
        await db.execute(
            select(BatchEvaluation.id).where(
                BatchEvaluation.collection_id == collection_id, BatchEvaluation.status == "running"
            )
        )
    ).first() is not None


def _forget_pass(evaluation_id: uuid.UUID) -> None:
    """Drop a finished pass's in-memory bookkeeping (it is recreated if the pass is
    ever reopened)."""
    state = _state()
    state["locks"].pop(evaluation_id, None)
    state["unbooked"].pop(evaluation_id, None)
    state["active"].discard(evaluation_id)


async def pass_is_open(db: AsyncSession, collection_id: uuid.UUID) -> bool:
    """Is the batch's current pass one that should hear about settled targets
    (running, or completed and re-openable)?"""
    current = await scoring.current_pass(db, collection_id)
    return current is not None and current.status in ("running", "completed")


async def on_target_settled(batch_id: uuid.UUID, target_id: uuid.UUID) -> None:
    """A target reached a terminal status (`core._settle_target`): arm its row in
    the batch's current pass, or re-arm it after a retry, then pump whenever the
    pass is running (a pass started while targets were unsettled has nothing to
    change here but still has work to launch). Never raises."""
    try:
        async with _session_factory()() as db:
            ev = await scoring.current_pass(db, batch_id)
            if ev is None or ev.status not in ("running", "completed"):
                return
            evaluation_id = ev.id
        async with _pass_lock(evaluation_id):
            async with _session_factory()() as db:
                t = await db.get(BatchTarget, target_id, populate_existing=True)
                batch = await db.get(Batch, batch_id)
                ev = await db.get(BatchEvaluation, evaluation_id, populate_existing=True)
                if t is None or batch is None or ev is None or ev.status not in ("running", "completed"):
                    return
                if t.status not in core.TERMINAL_TARGET_STATUSES:
                    return
                row = (
                    await db.execute(
                        select(TargetEvaluation).where(
                            TargetEvaluation.evaluation_id == ev.id, TargetEvaluation.target_id == t.id
                        )
                    )
                ).scalars().first()
                skipped = scoring.target_class(t) in ("infra", "timeout")
                reopen = False
                if row is None:
                    row = _new_row(ev, batch, t)
                    if skipped:
                        row.status, row.skip_reason = "skipped", INFRA_SKIP
                    db.add(row)
                    reopen = not skipped
                elif row.attempt != (t.attempts or 0) or row.evaluated_run_id != t.run_id:
                    row.attempt = t.attempts or 0
                    row.evaluated_run_id = t.run_id
                    row.evaluator_run_id = None  # a judge still working on the old attempt is late
                    row.status, row.skip_reason, row.error = "pending", None, None
                    row.scores, row.flags = {}, []
                    row.quality = row.difficulty = row.confidence = row.rationale = None
                    row.relative = row.review = None
                    row.finished_at = None
                    if skipped:
                        row.status, row.skip_reason = "skipped", INFRA_SKIP
                    reopen = not skipped
                # A skipped row has nothing to judge: it never reopens a finished pass.
                if reopen and ev.status == "completed":
                    ev.status, ev.finished_at = "running", None
                await db.commit()
                running = ev.status == "running"
        if running:
            await _pump(evaluation_id)
    except Exception:  # noqa: BLE001 - never into the settle
        log.exception("evaluation could not hear about target %s", target_id)


async def cancel_evaluation(db: AsyncSession, batch: Batch) -> BatchEvaluation:
    first = (
        await db.execute(
            select(BatchEvaluation.id)
            .where(BatchEvaluation.collection_id == batch.id, BatchEvaluation.status == "running")
            .order_by(BatchEvaluation.created_at.desc())
        )
    ).scalars().first()
    if first is None:
        raise HTTPException(409, "No evaluation pass is running for this batch")
    live: list[uuid.UUID] = []
    async with _pass_lock(first):
        ev = await db.get(BatchEvaluation, first, populate_existing=True)
        if ev.status != "running":
            raise HTTPException(409, "No evaluation pass is running for this batch")
        ev.status, ev.finished_at = "cancelled", _now()
        for row in await scoring.pass_rows(db, ev.id):
            if row.status == "pending":
                row.status, row.skip_reason = "skipped", "cancelled"
            elif row.status == "running":
                row.status, row.skip_reason = "skipped", "cancelled"
                if row.evaluator_run_id:
                    live.append(row.evaluator_run_id)
                row.evaluator_run_id = None  # a late `submit_evaluation` is refused
        if ev.summary is None and ev.summary_run_id:
            live.append(ev.summary_run_id)  # a batch review in flight
        await db.commit()
    engine = core._compat().get_harness_engine()
    for run_id in live:
        with contextlib.suppress(Exception):
            engine.cancel(run_id)
    _forget_pass(ev.id)
    return ev


async def review_target(
    db: AsyncSession, batch: Batch, target: BatchTarget, user: User, *,
    verdict: str, adjusted_quality: float | None, note: str | None,
) -> dict:
    """A human spot check of a scored row in the batch's current pass."""
    if verdict not in VERDICTS:
        raise HTTPException(422, f"verdict must be one of {', '.join(VERDICTS)}")
    if verdict == "adjust" and adjusted_quality is None:
        raise HTTPException(422, "adjusted_quality is required to adjust a score")
    current = await scoring.current_pass(db, batch.id)
    row = None
    if current is not None:
        row = (
            await db.execute(
                select(TargetEvaluation).where(
                    TargetEvaluation.evaluation_id == current.id, TargetEvaluation.target_id == target.id
                )
            )
        ).scalars().first()
    if row is None or row.status != "scored":
        raise HTTPException(409, "Only a scored target of the current evaluation can be reviewed")
    row.review = {
        "verdict": verdict,
        "adjusted_quality": adjusted_quality if verdict == "adjust" else None,
        "note": (note or "").strip() or None,
        "by": str(user.id),
        "by_name": user.display_name,
        "at": _now().isoformat(),
    }
    await db.commit()
    return target_evaluation_out(row, target)


async def sweep_orphaned_evaluations(db: AsyncSession) -> int:
    """Boot sweep (after the batch sweep). A row left `running` whose evaluator run
    is no longer queued/running becomes `failed` ("interrupted by a restart");
    passes still `running` are then pumped (or completed when nothing is left).
    Returns how many rows were failed."""
    rows = list(
        (await db.execute(select(TargetEvaluation).where(TargetEvaluation.status == "running"))).scalars()
    )
    run_ids = [r.evaluator_run_id for r in rows if r.evaluator_run_id]
    runs = (
        {r.id: r for r in (await db.execute(select(Run).where(Run.id.in_(run_ids)))).scalars()} if run_ids else {}
    )
    failed = 0
    for r in rows:
        run = runs.get(r.evaluator_run_id) if r.evaluator_run_id else None
        if run is not None and run.status in ("queued", "running"):
            continue  # still live (only possible without the instance lock)
        r.status, r.error, r.finished_at = "failed", "interrupted by a restart", _now()
        await _book_cost(db, r.evaluation_id, run, r)  # the work was paid for
        failed += 1
    # A process that died after `submit_evaluation` left its row `scored`, with the run's
    # cost still unbooked: book every run of a running pass that started inside the pass
    # (a row copied from an older pass points at that pass's run, which is not ours).
    for ev in (await db.execute(select(BatchEvaluation).where(BatchEvaluation.status == "running"))).scalars():
        started = core._aware(ev.created_at)
        for r in await scoring.pass_rows(db, ev.id):
            if not r.evaluator_run_id or str(r.evaluator_run_id) in (ev.booked_runs or []):
                continue
            run = await db.get(Run, r.evaluator_run_id)
            if run is None or run.status in ("queued", "running") or core._aware(run.created_at) < started:
                continue
            await _book_cost(db, ev.id, run, r)
    # A review run the restart interrupted: book its spend; the pump starts a new one.
    for ev in (
        await db.execute(
            select(BatchEvaluation).where(
                BatchEvaluation.status == "running",
                BatchEvaluation.summary.is_(None),
                BatchEvaluation.summary_run_id.is_not(None),
            )
        )
    ).scalars():
        await _book_cost(db, ev.id, await db.get(Run, ev.summary_run_id))
    await db.commit()
    passes = list(
        (await db.execute(select(BatchEvaluation.id).where(BatchEvaluation.status == "running"))).scalars()
    )
    for pid in passes:
        core._spawn(_pump(pid))
    if failed:
        log.warning("boot sweep: failed %d interrupted evaluation row(s)", failed)
    return failed


# ══════════════════════════════════════════════════════════════════════════
# records
# ══════════════════════════════════════════════════════════════════════════
async def records_context(db: AsyncSession, batch: Batch) -> SimpleNamespace | None:
    """What `finalize`'s records add when the batch's current pass scored at least
    one fresh row: `by_target` (target id -> the per-row evaluation block) and the
    manifest block. None otherwise, so a batch without a pass writes the records it
    always wrote."""
    current = await scoring.current_pass(db, batch.id)
    if current is None:
        return None
    targets = {t.id: t for t in await core.load_targets(db, [batch.id])}
    by_target: dict[uuid.UUID, dict] = {}
    for r in await scoring.pass_rows(db, current.id):
        t = targets.get(r.target_id)
        if t is None or r.status != "scored" or r.quality is None or scoring.is_stale(r, t):
            continue
        rel = r.relative if isinstance(r.relative, dict) else {}
        by_target[r.target_id] = {
            "quality": float(r.quality),
            "rank": rel.get("rank"),
            "residual": rel.get("residual"),
            "flags": list(r.flags or []),
            "scores": r.scores or {},
            "difficulty": r.difficulty,
            "confidence": r.confidence,
        }
    if not by_target:
        return None
    qualities = [v["quality"] for v in by_target.values()]
    return SimpleNamespace(
        by_target=by_target,
        manifest={
            "judge_model": current.judge_model,
            "rubric_key": current.rubric_key,
            "rubric_version": current.rubric_version,
            "scored": len(by_target),
            "quality_mean": round(statistics.fmean(qualities), 1),
            "cost_usd": round(float(current.cost_usd or 0), 6),
        },
    )


async def evaluation_cost(db: AsyncSession, collection_id: uuid.UUID) -> float:
    """The spend of every pass over the batch (what the parent run is charged on
    top of the targets')."""
    total = (
        await db.execute(
            select(BatchEvaluation.cost_usd).where(BatchEvaluation.collection_id == collection_id)
        )
    ).scalars()
    return round(sum(float(c or 0) for c in total), 6)

