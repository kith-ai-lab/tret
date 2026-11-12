"""The blind pairwise judge: which of two batches delivered a row better.

The rubric scorer (`tret/batches/evaluation.py`) grades each target alone, so two
batches that both score 80 on a row are "tied" even when one of them holds far more
of the required disclosure. A pairwise judgment shows ONE judge the same register
row's output from two batches of a family, side by side, blind (no model, provider,
cost, batch name or which batch is which), twice with the order swapped, and
records which output is the better delivery. The verdict is shown BESIDE the rubric
numbers; code never rewrites a score from it.

A pair is stored once, whichever way it is compared: "low" is the target with the
smaller `str(id)`. A judgment is stale when either target's attempt or run moved on.
Judge runs are ordinary runs of the Batch Evaluator harness, carry
`_pairwise_judgment_id` in their task input (so `services/outcomes.py` records
nothing for them) and end by calling `submit_pairwise` (`engine/evaluation_tools.py`).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from decimal import Decimal
from typing import Any

from fastapi import HTTPException
from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tret.batches import core, evaluation, evidence, scoring
from tret.batches.profiles import profile_for
from tret.batches.rubrics import PAIRWISE_ID_KEY, PAIRWISE_ORDER_KEY, criterion_applies
from tret.db.models import Batch, BatchTarget, Document, PairwiseJudgment, Run
from tret.packs.links import packs_for_harness, resolve_pack_for_task

log = logging.getLogger("tret.pairwise")

SCOPES = ("near_ties", "both_delivered")
PAIRWISE_MAX_ITERATIONS = 6
MESSAGE_LIMIT = 40_000
TASK_LIMIT = 12_000
PAIRS_BUDGET_HEADROOM = 1.5
BUDGET_FLOOR_USD = 1.0
ORDERS = ("low_first", "high_first")
LIVE = ("pending", "running")

INSTRUCTION = (
    "This is a pairwise comparison, not a scoring run: do not call submit_evaluation; give no scores, "
    "difficulty or flags. Decide which output is the better delivery for THIS task under the rubric, criterion by "
    "criterion. Judge only what is shown. Breadth matters: an output that holds more of the "
    "required disclosure with little extra is better than a thinner one that scores the same on "
    "each criterion. Say \"tie\" only when there is no material difference. You are not told who "
    "or what produced either output; do not guess. Finish by calling submit_pairwise exactly "
    "once (preferred: first, second or tie; criteria: one entry per criterion above; confidence; "
    "rationale), and write no other reply."
)

_state: dict[str, Any] = {"loop": None, "sem": None}


def _sem() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    if _state["loop"] is not loop:
        _state.update(loop=loop, sem=asyncio.Semaphore(evaluation.concurrency()))
    return _state["sem"]


def _session_factory():
    return core._session_factory()


# ══════════════════════════════════════════════════════════════════════════
# orientation, staleness, verdicts
# ══════════════════════════════════════════════════════════════════════════
def orient(ta: BatchTarget, tb: BatchTarget) -> tuple[BatchTarget, BatchTarget]:
    """(low, high): the target with the smaller `str(id)` first."""
    return (ta, tb) if str(ta.id) < str(tb.id) else (tb, ta)


def is_stale(row: PairwiseJudgment, low: BatchTarget | None, high: BatchTarget | None) -> bool:
    if low is None or high is None:
        return True
    return (
        (row.attempt_low or 0) != (low.attempts or 0)
        or (row.attempt_high or 0) != (high.attempts or 0)
        or row.run_low_id != low.run_id
        or row.run_high_id != high.run_id
    )


def side_of(call: dict) -> str | None:
    """The side (low | high | tie) a call preferred, or None while unanswered."""
    pref = call.get("preferred")
    if pref == "tie":
        return "tie"
    if pref not in ("first", "second"):
        return None
    first = "low" if call.get("order") == "low_first" else "high"
    second = "high" if first == "low" else "low"
    return first if pref == "first" else second


def verdict_from(calls: list[dict]) -> tuple[str | None, str | None]:
    """(verdict, strength) from both calls: the same side twice is clear; a side and a
    tie is a lean; two ties a tie; opposite sides inconsistent."""
    sides = [s for c in calls if (s := side_of(c)) is not None]
    if len(sides) < 2:
        return None, None
    a, b = sides[:2]
    if a == b:
        return (a, None) if a == "tie" else (a, "clear")
    if "tie" in (a, b):
        return (a if b == "tie" else b), "lean"
    return "inconsistent", None


# ══════════════════════════════════════════════════════════════════════════
# data (light: no runs or findings)
# ══════════════════════════════════════════════════════════════════════════
async def light_collect(db: AsyncSession, batch: Batch) -> scoring.BatchData:
    """A `BatchData` with the targets, their class and fresh scored quality, but no
    runs, findings or metrics beyond the class: all eligibility and comparability need."""
    targets = await core.load_targets(db, [batch.id])
    current = await scoring.current_pass(db, batch.id)
    rows = await scoring.pass_rows(db, current.id) if current is not None else []
    by_target = {r.target_id: r for r in rows}
    out = []
    for t in targets:
        d = scoring.TargetData(
            target=t, run=None, findings=[], metrics={"class": scoring.target_class(t)},
            row=scoring.row_key(batch, t),
        )
        ev = by_target.get(t.id)
        d.evaluation = ev
        if ev is not None and ev.status == "scored" and ev.quality is not None and not scoring.is_stale(ev, t):
            d.quality = float(ev.quality)
        out.append(d)
    return scoring.BatchData(batch=batch, targets=out, current=current, rows=rows)


def eligible_pairs(
    data_a: scoring.BatchData, data_b: scoring.BatchData, scope: str
) -> list[tuple[scoring.TargetData, scoring.TargetData]]:
    """Shared row keys whose picked targets are both success class with a fresh scored
    quality; `near_ties` keeps |quality_b - quality_a| < TIE_POINTS. Smallest |delta|
    first, then row position."""
    if scope not in SCOPES:
        raise HTTPException(422, f"scope must be one of {', '.join(SCOPES)}")
    pa, pb = scoring.picked_rows(data_a), scoring.picked_rows(data_b)
    found = []
    for key, da in pa.items():
        db_ = pb.get(key)
        if db_ is None:
            continue
        if da.cls != "success" or db_.cls != "success" or da.quality is None or db_.quality is None:
            continue
        delta = abs(db_.quality - da.quality)
        if scope == "near_ties" and not delta < scoring.TIE_POINTS:
            continue
        found.append((delta, da.target.position, da, db_))
    found.sort(key=lambda x: (x[0], x[1]))
    return [(da, db_) for _, _, da, db_ in found]


def _pair_filter(workspace_id: uuid.UUID, a_id: uuid.UUID, b_id: uuid.UUID):
    return and_(
        PairwiseJudgment.workspace_id == workspace_id,
        or_(
            and_(PairwiseJudgment.batch_low_id == a_id, PairwiseJudgment.batch_high_id == b_id),
            and_(PairwiseJudgment.batch_low_id == b_id, PairwiseJudgment.batch_high_id == a_id),
        ),
    )


async def pair_rows(
    db: AsyncSession, a: Batch, b: Batch, current: Any | None = None
) -> list[PairwiseJudgment]:
    """The judgments of this batch pair (either orientation) made with `current`'s
    judge and rubric version (the pair's comparable pass); none without one."""
    if current is None:
        return []
    return list(
        (
            await db.execute(
                select(PairwiseJudgment)
                .where(
                    _pair_filter(a.workspace_id, a.id, b.id),
                    PairwiseJudgment.judge_model == current.judge_model,
                    PairwiseJudgment.rubric_version == current.rubric_version,
                )
                .execution_options(populate_existing=True)
            )
        ).scalars()
    )


def _comparable_pass(data_a: scoring.BatchData, data_b: scoring.BatchData):
    return data_a.current if scoring._quality_note(data_a, data_b) is None else None


def abs_verdict(row: PairwiseJudgment, a_id: uuid.UUID) -> str | None:
    """The row's verdict as a | b | tie | inconsistent."""
    if row.verdict in ("tie", "inconsistent"):
        return row.verdict
    if row.verdict in ("low", "high"):
        low_is_a = row.batch_low_id == a_id
        return "a" if (row.verdict == "low") == low_is_a else "b"
    return None


# ══════════════════════════════════════════════════════════════════════════
# start / cancel
# ══════════════════════════════════════════════════════════════════════════
def default_budget(judge_model: str, pairs: int) -> float:
    est = evaluation.estimate_evaluation_cost(judge_model, 1)
    return round(max(BUDGET_FLOOR_USD, PAIRS_BUDGET_HEADROOM * pairs * 2 * est), 2)


async def start_pairwise(
    db: AsyncSession,
    a: Batch,
    b: Batch,
    *,
    user_id: uuid.UUID | None,
    scope: str = "near_ties",
    max_pairs: int = 20,
    budget_usd: float | None = None,
) -> dict:
    """Create `pending` judgments for the eligible pairs that have no standing one and
    spawn the runner. 422 unless the comparison is quality-comparable; 409 while one of
    this pair's judgments is pending or running."""
    from tret.services.workspace import ensure_evaluator_harness

    if a.id == b.id or a.workspace_id != b.workspace_id:
        raise HTTPException(422, "Pick two different batches of one workspace")
    data_a, data_b = await light_collect(db, a), await light_collect(db, b)
    note = scoring._quality_note(data_a, data_b)
    if note is not None:
        raise HTTPException(422, note)
    current = data_a.current
    pairs = eligible_pairs(data_a, data_b, scope)
    live = (
        await db.execute(
            select(PairwiseJudgment.id)
            .where(_pair_filter(a.workspace_id, a.id, b.id), PairwiseJudgment.status.in_(LIVE))
            .limit(1)
        )
    ).first()
    if live is not None:
        raise HTTPException(409, "A pairwise judge is already running for these batches; stop it first")
    judge = await evaluation.resolve_judge(db, a.workspace_id, current.judge_model)
    existing = {
        (r.target_low_id, r.target_high_id): r for r in await pair_rows(db, a, b, current)
    }
    chosen: list[PairwiseJudgment] = []
    for da, db_ in pairs:
        if len(chosen) >= max_pairs:
            break
        low, high = orient(da.target, db_.target)
        row = existing.get((low.id, high.id))
        if row is not None and row.status == "judged" and not is_stale(row, low, high):
            continue
        if row is None:
            row = PairwiseJudgment(
                id=uuid.uuid4(),
                workspace_id=a.workspace_id,
                family_key=scoring.family_key(a),
                rubric_key=current.rubric_key,
                rubric_version=current.rubric_version,
                judge_model=judge,
                row_key=da.row,
                batch_low_id=low.collection_id,
                batch_high_id=high.collection_id,
                target_low_id=low.id,
                target_high_id=high.id,
                cost_usd=Decimal(0),
                created_by=user_id,
                created_at=core._now(),
            )
            db.add(row)
        row.attempt_low, row.attempt_high = low.attempts or 0, high.attempts or 0
        row.run_low_id, row.run_high_id = low.run_id, high.run_id
        row.status, row.verdict, row.strength = "pending", None, None
        row.calls, row.error, row.finished_at = [], None, None
        chosen.append(row)
    budget = budget_usd if budget_usd is not None else default_budget(judge, len(chosen))
    for row in chosen:
        row.budget_usd = Decimal(str(round(float(budget), 4)))
    try:
        if chosen:
            await ensure_evaluator_harness(db, a.workspace_id)
        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        raise HTTPException(409, "A pairwise judge is already running for these batches; stop it first") from e
    if chosen:
        core._spawn(_run_pass([r.id for r in chosen], float(budget)))
    return await pairwise_status(db, a, b)


async def cancel_pairwise(db: AsyncSession, a: Batch, b: Batch) -> dict:
    """Cancel the in-flight judge runs of this pair and mark its pending/running
    judgments skipped."""
    rows = list(
        (
            await db.execute(
                select(PairwiseJudgment)
                .where(_pair_filter(a.workspace_id, a.id, b.id), PairwiseJudgment.status.in_(LIVE))
                .execution_options(populate_existing=True)
            )
        ).scalars()
    )
    live_runs: list[uuid.UUID] = []
    for r in rows:
        for c in r.calls or []:
            if isinstance(c, dict) and c.get("preferred") is None and c.get("evaluator_run_id"):
                live_runs.append(uuid.UUID(c["evaluator_run_id"]))
        r.status, r.error, r.finished_at = "skipped", "cancelled", core._now()
    await db.commit()
    engine = core._compat().get_harness_engine()
    for run_id in live_runs:
        with contextlib.suppress(Exception):
            engine.cancel(run_id)
    return await pairwise_status(db, a, b)


async def sweep_orphaned_pairwise(db: AsyncSession) -> int:
    """Boot sweep: a judgment left pending/running with no live judge run is failed
    ("interrupted by a restart")."""
    rows = list(
        (await db.execute(select(PairwiseJudgment).where(PairwiseJudgment.status.in_(LIVE)))).scalars()
    )
    failed = 0
    for r in rows:
        ids = [
            uuid.UUID(c["evaluator_run_id"])
            for c in r.calls or []
            if isinstance(c, dict) and c.get("evaluator_run_id")
        ]
        runs = (await db.execute(select(Run).where(Run.id.in_(ids)))).scalars().all() if ids else []
        if any(run.status in ("queued", "running") for run in runs):
            continue
        answered = [c for c in r.calls or [] if isinstance(c, dict) and c.get("preferred") is not None]
        verdict, strength = verdict_from(answered)
        if len(answered) >= 2 and verdict is not None:
            r.verdict, r.strength, r.status, r.finished_at = verdict, strength, "judged", core._now()
            continue
        r.status, r.error, r.finished_at = "failed", "interrupted by a restart", core._now()
        failed += 1
    await db.commit()
    if failed:
        log.warning("boot sweep: failed %d interrupted pairwise judgment(s)", failed)
    return failed


async def rubric_for_row(db: AsyncSession, row: PairwiseJudgment) -> dict | None:
    """The rubric of the version the judgment was started with (from either batch's
    current pass); None when neither pass carries that version any more."""
    for batch_id in (row.batch_low_id, row.batch_high_id):
        ev = await scoring.current_pass(db, batch_id)
        if ev is not None and ev.rubric_version == row.rubric_version:
            return ev.rubric or {}
    return None


# ══════════════════════════════════════════════════════════════════════════
# the message
# ══════════════════════════════════════════════════════════════════════════
def rubric_block(rubric: dict) -> str:
    """The rubric's criteria that apply to a delivered result, with anchors."""
    lines = ["Criteria (a 5 and a 1 are defined for each):"]
    for c in rubric.get("criteria") or []:
        if not criterion_applies(c.get("applies", "all"), "success"):
            continue
        lines.append(f"- {c['key']} ({c['label']}, weight {c['weight']})")
        lines.append(f"    5: {c['five']}")
        lines.append(f"    1: {c['one']}")
    return "\n".join(lines)


async def build_message(
    db: AsyncSession,
    rubric: dict,
    batch_first: Batch,
    first: BatchTarget,
    batch_second: Batch,
    second: BatchTarget,
    task_batch: Batch,
    task_target: BatchTarget,
) -> tuple[str, list[uuid.UUID]]:
    """The judge's message and the documents to attach (each id once). The task block is
    shown once (from `task_target`, the same for both orders) and clipped; the outputs
    share what room is left, so the instruction always survives."""
    outputs: list[str] = []
    attach: list[list[uuid.UUID]] = []
    for batch, target in ((batch_first, first), (batch_second, second)):
        profile = profile_for(batch)
        run = await db.get(Run, target.run_id) if target.run_id else None
        findings = (
            (await core.load_review_findings(db, profile, [target], batch)).get(target.run_id, [])
            if target.run_id
            else []
        )
        text, doc_ids = await profile.pairwise_evidence(db, batch, target, run, findings)
        outputs.append(text)
        attach.append(list(dict.fromkeys(doc_ids))[:4])
    owners: dict[uuid.UUID, set[int]] = {}
    for n, ids in enumerate(attach, start=1):
        for doc_id in ids:
            owners.setdefault(doc_id, set()).add(n)
    docs_lines: list[str] = []
    for doc_id, who in owners.items():
        doc = await db.get(Document, doc_id)
        if doc is not None:
            label = "given to both runs" if len(who) > 1 else f"attached to Output {min(who)}"
            docs_lines.append(f"- {doc.id}: {doc.filename} ({label})")
    documents = (
        "\n".join(docs_lines) + "\n\nOpen these only to check a specific doubt, at most three reads or "
        "searches in all, then call submit_pairwise."
        if docs_lines
        else "None. No documents are attached: judge from the outputs shown and call submit_pairwise."
    )
    task = profile_for(task_batch).pairwise_task(task_batch, task_target)
    if len(task) > TASK_LIMIT:
        task = task[: TASK_LIMIT - 40].rstrip() + "\n[task cut at the size limit]"
    fixed = [
        ("Rubric", rubric_block(rubric)[:8000]),
        ("Task", task),
        ("Attached documents", documents),
        ("Instruction", INSTRUCTION),
    ]
    room = MESSAGE_LIMIT - sum(len(h) + len(b) + 12 for h, b in fixed) - 400
    per = max(1000, room // 2)

    def fit(text: str) -> str:
        return text if len(text) <= per else text[: per - 40].rstrip() + "\n[output cut at the size limit]"

    sections = [fixed[0], fixed[1], ("Output 1", fit(outputs[0])), ("Output 2", fit(outputs[1])), fixed[2], fixed[3]]
    text = evidence.assemble(sections, limit=MESSAGE_LIMIT)
    if INSTRUCTION not in text:  # never lose the instruction
        text = text[: MESSAGE_LIMIT - len(INSTRUCTION) - 30].rstrip() + f"\n\n## Instruction\n{INSTRUCTION}\n"
    return text, list(owners)


# ══════════════════════════════════════════════════════════════════════════
# the runner
# ══════════════════════════════════════════════════════════════════════════
async def _run_pass(row_ids: list[uuid.UUID], budget: float) -> None:
    sem = _sem()
    spent = [0.0]

    async def one(row_id: uuid.UUID) -> None:
        async with sem:
            if core.draining():
                return
            if spent[0] >= budget:
                await _mark(row_id, "skipped", "budget")
                return
            try:
                spent[0] += await _judge_pair(row_id)
            except Exception as e:  # noqa: BLE001 - one pair must not stop the pass
                log.exception("pairwise judgment %s crashed", row_id)
                await _mark(row_id, "failed", core.summarise_run_error(f"{type(e).__name__}: {e}"))

    await asyncio.gather(*(one(r) for r in row_ids), return_exceptions=True)


async def _mark(row_id: uuid.UUID, status: str, error: str | None) -> None:
    with contextlib.suppress(Exception):
        async with _session_factory()() as db:
            row = await db.get(PairwiseJudgment, row_id, populate_existing=True)
            if row is not None and row.status in LIVE:
                row.status, row.error, row.finished_at = status, error, core._now()
                await db.commit()


async def _add_run_cost(row_id: uuid.UUID, run_id: uuid.UUID) -> float:
    """Put a judge run's recorded spend on the row (fresh session; never raises)."""
    with contextlib.suppress(Exception):
        async with _session_factory()() as db:
            run = await db.get(Run, run_id, populate_existing=True)
            row = await db.get(PairwiseJudgment, row_id, populate_existing=True)
            cost = core.run_cost(run)
            if row is not None:
                row.cost_usd = (row.cost_usd or Decimal(0)) + Decimal(str(round(cost, 6)))
                await db.commit()
            return cost
    return 0.0


async def _judge_pair(row_id: uuid.UUID) -> float:
    """Both ordered judge calls of one pair, then the verdict. Returns the spend."""
    from tret.services.workspace import ensure_evaluator_harness

    spent = 0.0
    async with _session_factory()() as db:
        # Claim the row atomically: only one runner can move it pending -> running.
        claimed = await db.execute(
            update(PairwiseJudgment)
            .where(PairwiseJudgment.id == row_id, PairwiseJudgment.status == "pending")
            .values(status="running", error=None)
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        if claimed.rowcount != 1:
            return 0.0
        row = await db.get(PairwiseJudgment, row_id, populate_existing=True)
        low = await db.get(BatchTarget, row.target_low_id, populate_existing=True)
        high = await db.get(BatchTarget, row.target_high_id, populate_existing=True)
        batch_low, batch_high = await db.get(Batch, row.batch_low_id), await db.get(Batch, row.batch_high_id)
        if None in (low, high, batch_low, batch_high):
            row.status, row.error, row.finished_at = "failed", "the pair no longer exists", core._now()
            await db.commit()
            return 0.0
        rubric = await rubric_for_row(db, row)
        if rubric is None:
            row.status, row.finished_at = "failed", core._now()
            row.error = "the rubric changed; start the pairwise judge again"
            await db.commit()
            return 0.0
        harness = await ensure_evaluator_harness(db, row.workspace_id)
        pack = resolve_pack_for_task(await packs_for_harness(db, harness), "freeform")
        my_runs: list[str] = []  # this runner's own judge runs, in order
        for order in ORDERS:
            first, second = (low, high) if order == "low_first" else (high, low)
            b_first, b_second = (batch_low, batch_high) if order == "low_first" else (batch_high, batch_low)
            message, doc_ids = await build_message(
                db, rubric, b_first, first, b_second, second, batch_low, low
            )
            run = Run(
                id=uuid.uuid4(),
                project_id=batch_low.project_id,
                harness_id=harness.id,
                pack_id=pack.id if pack else None,
                task_type="freeform",
                task_input={
                    "message": message,
                    "_label": f"pairwise:{low.slug}",
                    PAIRWISE_ID_KEY: str(row.id),
                    PAIRWISE_ORDER_KEY: order,
                    "_model_override": row.judge_model,
                    "_max_iterations": PAIRWISE_MAX_ITERATIONS,
                },
                document_ids=list(dict.fromkeys(doc_ids)),
                created_by=row.created_by,
            )
            db.add(run)
            await db.flush()
            # Re-read under the claim: never append to a stale in-memory list.
            row = await db.get(PairwiseJudgment, row_id, populate_existing=True)
            ids = {c.get("evaluator_run_id") for c in row.calls or [] if isinstance(c, dict)}
            if row.status != "running" or not set(my_runs) <= ids:
                # cancelled, or reset and claimed by a newer pass, while the message was built
                await db.commit()
                await core._close_out_run(run.id, "pairwise judgment cancelled before this run started")
                return spent
            row.calls = [
                *(row.calls or []),
                {
                    "order": order, "preferred": None, "criteria": {}, "confidence": None,
                    "rationale": None, "evaluator_run_id": str(run.id),
                },
            ]
            await db.commit()
            my_runs.append(str(run.id))
            try:
                timed_out = await evaluation._execute_run(run.id)
            except asyncio.CancelledError:
                await asyncio.shield(_add_run_cost(row_id, run.id))
                raise
            run = await db.get(Run, run.id, populate_existing=True)
            row = await db.get(PairwiseJudgment, row_id, populate_existing=True)
            cost = core.run_cost(run)
            spent += cost
            row.cost_usd = (row.cost_usd or Decimal(0)) + Decimal(str(round(cost, 6)))
            entry = next((c for c in row.calls or [] if c.get("evaluator_run_id") == str(run.id)), None)
            if entry is None:  # the row was reset by a newer start: this run is not its business
                await db.commit()
                return spent
            if row.status != "running":  # cancelled while the judge ran
                await db.commit()
                return spent
            if entry.get("preferred") is None:
                why = "timed out" if timed_out else core.summarise_run_error(run.error) or "no answer"
                row.status, row.finished_at = "failed", core._now()
                row.error = f"the judge did not call submit_pairwise ({order}): {why}"
                await db.commit()
                return spent
            await db.commit()
        row = await db.get(PairwiseJudgment, row_id, populate_existing=True)
        if row.status == "running":
            row.verdict, row.strength = verdict_from(row.calls)
            row.status, row.finished_at = "judged", core._now()
            await db.commit()
    return spent


# ══════════════════════════════════════════════════════════════════════════
# status
# ══════════════════════════════════════════════════════════════════════════
async def pairwise_status(db: AsyncSession, a: Batch, b: Batch, *, with_rows: bool = False) -> dict:
    """The pair's judge state oriented to the REQUESTED a / b."""
    data_a, data_b = await light_collect(db, a), await light_collect(db, b)
    comparable = _comparable_pass(data_a, data_b)
    rows = await pair_rows(db, a, b, comparable)
    tids = {r.target_low_id for r in rows} | {r.target_high_id for r in rows}
    targets = (
        {
            t.id: t
            for t in (
                await db.execute(
                    select(BatchTarget).where(BatchTarget.id.in_(sorted(tids, key=str))).execution_options(populate_existing=True)
                )
            ).scalars()
        }
        if tids
        else {}
    )
    counts = {s: 0 for s in ("judged", "pending", "running", "failed", "skipped")}
    verdicts = {"a": 0, "b": 0, "tie": 0, "inconsistent": 0}
    stale = 0
    out_rows = []
    for r in sorted(rows, key=lambda r: (r.row_key, str(r.id))):
        counts[r.status] = counts.get(r.status, 0) + 1
        is_old = r.status == "judged" and is_stale(r, targets.get(r.target_low_id), targets.get(r.target_high_id))
        verdict = abs_verdict(r, a.id) if r.status == "judged" else None
        if r.status == "judged":
            if is_old:
                stale += 1
            elif verdict:
                verdicts[verdict] += 1
        if with_rows:
            low_is_a = r.batch_low_id == a.id
            target = targets.get(r.target_low_id if low_is_a else r.target_high_id)
            calls = []
            for c in r.calls or []:
                if not isinstance(c, dict) or c.get("preferred") is None:
                    continue
                first_is_low = c.get("order") == "low_first"
                calls.append(
                    {
                        "order": "a_first" if first_is_low == low_is_a else "b_first",
                        "preferred": c.get("preferred"),
                        "criteria": c.get("criteria") or {},
                        "confidence": c.get("confidence"),
                        "rationale": c.get("rationale"),
                    }
                )
            out_rows.append(
                {
                    "row_key": r.row_key,
                    "label": target.label if target is not None else r.row_key,
                    "status": r.status,
                    "verdict": verdict,
                    "strength": r.strength,
                    "stale": is_old,
                    "calls": calls,
                    "cost_usd": float(r.cost_usd or 0),
                    "error": r.error,
                    "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                }
            )
    budgets = [float(r.budget_usd) for r in rows if r.budget_usd is not None]
    out = {
        "a": str(a.id),
        "b": str(b.id),
        "status": "running" if counts["pending"] or counts["running"] else "idle",
        "scope_eligible": {s: len(eligible_pairs(data_a, data_b, s)) if comparable else 0 for s in SCOPES},
        **counts,
        "stale": stale,
        "verdicts": verdicts,
        "cost_usd": round(sum(float(r.cost_usd or 0) for r in rows), 6),
        "budget_usd": max(budgets) if budgets else None,
    }
    if with_rows:
        out["rows"] = out_rows
    return out


async def compare_overlay(
    db: AsyncSession,
    a: Batch,
    b: Batch,
    data_a: scoring.BatchData,
    data_b: scoring.BatchData,
    picked_a: dict[str, scoring.TargetData],
    picked_b: dict[str, scoring.TargetData],
    comparable: bool,
) -> tuple[dict[str, dict], dict | None]:
    """(per-row `pairwise` blocks by row key, the summary block) for `compare_batches`:
    one query. The summary is None when the comparison is not quality-comparable."""
    if not comparable:
        return {}, None
    rows = await pair_rows(db, a, b, data_a.current)
    by_pair = {(r.target_low_id, r.target_high_id): r for r in rows}
    per_row: dict[str, dict] = {}
    tally = {"a": 0, "b": 0, "tie": 0, "inconsistent": 0}
    for key, da in picked_a.items():
        db_ = picked_b.get(key)
        if db_ is None:
            continue
        low, high = orient(da.target, db_.target)
        r = by_pair.get((low.id, high.id))
        if r is None or is_stale(r, low, high):
            continue
        if r.status == "judged":
            v = abs_verdict(r, a.id)
            per_row[key] = {"status": "judged", "verdict": v, "strength": r.strength, "stale": False}
            if v:
                tally[v] += 1
        elif r.status in LIVE:
            per_row[key] = {"status": r.status, "verdict": None, "strength": None, "stale": False}
    summary = {
        "eligible_near_ties": len(eligible_pairs(data_a, data_b, "near_ties")),
        "judged": sum(1 for p in per_row.values() if p["status"] == "judged"),
        "wins_a": tally["a"],
        "wins_b": tally["b"],
        "ties": tally["tie"],
        "inconsistent": tally["inconsistent"],
        "running": any(r.status in LIVE for r in rows),
        "cost_usd": round(sum(float(r.cost_usd or 0) for r in rows), 6),
    }
    return per_row, summary
