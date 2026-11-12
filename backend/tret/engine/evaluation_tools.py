"""`submit_evaluation` and `submit_batch_review`: how the Batch Evaluator harness
reports a score (`tret/batches/evaluation.py`).

An evaluator run is launched by the batch evaluator with
`task_input["_target_evaluation_id"]` naming the `TargetEvaluation` row it is to
fill (or `_batch_review` for the whole-batch review). The judge ends its run by
calling `submit_evaluation` once with a score and its evidence for each rubric
criterion; the tool validates the answer against the pass's frozen rubric,
computes the 0-100 quality and stores both on the row. Anywhere else, in any
run that is not an evaluator run of a row it owns, the tools refuse.

Imported for its registration side effect by `services/workspace.py` (not by
`engine/harness.py`), so `get_builtin_tools()` lists the tools wherever the
Batch Evaluator harness can name them. They are on no other seeded harness.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from tret.batches.rubrics import (
    EVALUATION_ID_KEY,
    PAIRWISE_ID_KEY,
    PAIRWISE_ORDER_KEY,
    compute_quality,
    criterion_applies,
)
from tret.db.models import Batch, BatchEvaluation, BatchTarget, PairwiseJudgment, Run, TargetEvaluation
from tret.engine.collection_tools import COLLECTION_ID_KEY, COLLECTION_TARGET_KEY
from tret.engine.tools import RunContext, ToolError, builtin

logger = logging.getLogger(__name__)

TARGET_EVALUATION_ID_KEY = "_target_evaluation_id"
BATCH_REVIEW_KEY = "_batch_review"

# Task-input keys the engine stamps on batch runs; a run started by a caller
# (`POST /api/runs`) never carries them, or it could pose as a batch's run.
ENGINE_STAMPED_KEYS = (
    EVALUATION_ID_KEY,
    TARGET_EVALUATION_ID_KEY,
    BATCH_REVIEW_KEY,
    PAIRWISE_ID_KEY,
    PAIRWISE_ORDER_KEY,
    COLLECTION_ID_KEY,
    COLLECTION_TARGET_KEY,
)
EVALUATION_TOOLS = ("submit_evaluation", "submit_batch_review", "submit_pairwise")
PAIRWISE_PREFERENCES = ("first", "second", "tie")
PAIRWISE_ORDERS = ("low_first", "high_first")
CONFIDENCES = ("low", "medium", "high")
MAX_EVIDENCE_CHARS = 600
MAX_RATIONALE_CHARS = 1500
MAX_FLAGS = 10
MAX_NARRATIVE_CHARS = 4000
MAX_NOTES, MAX_NOTE_CHARS = 10, 400
MAX_LESSONS, MAX_LESSON_CHARS = 5, 300


def _uuid(value, what: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except ValueError as e:
        raise ToolError(f"This run's {what} id is malformed; nothing was recorded.") from e


async def _evaluator_row(ctx: RunContext) -> tuple[TargetEvaluation, BatchEvaluation]:
    run = await ctx.db.get(Run, ctx.run_id)
    raw = (run.task_input or {}).get(TARGET_EVALUATION_ID_KEY) if run is not None else None
    if not raw and run is not None and (run.task_input or {}).get(PAIRWISE_ID_KEY):
        raise ToolError(
            "This run is a pairwise comparison, not a scoring run: call submit_pairwise instead; "
            "submit_evaluation records nothing here."
        )
    if not raw:
        raise ToolError("This run is not a batch evaluation; submit_evaluation does nothing here.")
    row = await ctx.db.get(TargetEvaluation, _uuid(raw, "target evaluation"))
    ev = await ctx.db.get(BatchEvaluation, row.evaluation_id) if row is not None else None
    if (
        row is None
        or ev is None
        or ctx.workspace_id is None
        or ev.workspace_id != ctx.workspace_id
        or row.evaluator_run_id != ctx.run_id
    ):
        raise ToolError("This run's evaluation row was not found; nothing was recorded.")
    if row.status != "running":
        # Cancelled, re-armed for a retry or already failed: a late judge records nothing.
        raise ToolError("This evaluation is no longer waiting for a score; nothing was recorded.")
    return row, ev


def _clean_scores(rubric: dict, scores: object) -> dict[str, dict]:
    if not isinstance(scores, dict):
        raise ToolError("scores must be an object keyed by criterion.")
    keys = [c["key"] for c in rubric.get("criteria") or []]
    unknown = sorted(set(scores) - set(keys))
    if unknown:
        raise ToolError(f"Unknown criterion key(s): {', '.join(unknown)}. Criteria: {', '.join(keys)}.")
    missing = [k for k in keys if k not in scores]
    if missing:
        raise ToolError(f"scores must include every criterion; missing: {', '.join(missing)}.")
    out: dict[str, dict] = {}
    for key in keys:
        entry = scores[key]
        if not isinstance(entry, dict):
            raise ToolError(f"scores.{key} must be an object with score and evidence.")
        score = entry.get("score")
        if score is not None and (isinstance(score, bool) or not isinstance(score, int) or not 1 <= score <= 5):
            raise ToolError(f"scores.{key}.score must be an integer from 1 to 5, or null when it does not apply.")
        evidence = entry.get("evidence", "")
        if not isinstance(evidence, str):
            raise ToolError(f"scores.{key}.evidence must be a string.")
        evidence = evidence.strip()
        if score is not None and not evidence:
            raise ToolError(f"scores.{key}.evidence is required with a score: cite pages, a short quote or a field.")
        if len(evidence) > MAX_EVIDENCE_CHARS:
            raise ToolError(f"scores.{key}.evidence must be at most {MAX_EVIDENCE_CHARS} characters.")
        out[key] = {"score": score, "evidence": evidence}
    return out


@builtin(
    "submit_evaluation",
    "Record the score for the run you are evaluating. Call exactly once, at the end. scores has "
    "one entry per rubric criterion: {score: integer 1-5 or null when the criterion does not "
    "apply, evidence: pages, a short quote or the evidence-pack field it rests on}. difficulty "
    "(1-5) rates how hard the task is for any competent run, separately from quality. flags are "
    "keys from the rubric's flag list, only when the evidence shows them. confidence is low, "
    "medium or high. rationale is a few sentences.",
    {
        "type": "object",
        "properties": {
            "scores": {
                "type": "object",
                "description": "criterion key -> {score, evidence}; every rubric criterion present.",
                "additionalProperties": {
                    "type": "object",
                    "properties": {
                        "score": {"type": ["integer", "null"], "minimum": 1, "maximum": 5},
                        "evidence": {"type": "string", "maxLength": MAX_EVIDENCE_CHARS},
                    },
                    "required": ["score", "evidence"],
                },
            },
            "difficulty": {"type": "integer", "minimum": 1, "maximum": 5},
            "flags": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_FLAGS},
            "confidence": {"type": "string", "enum": list(CONFIDENCES)},
            "rationale": {"type": "string", "maxLength": MAX_RATIONALE_CHARS},
        },
        "required": ["scores", "difficulty", "confidence", "rationale"],
    },
)
async def submit_evaluation(
    ctx: RunContext,
    scores: dict,
    difficulty: int,
    confidence: str,
    rationale: str,
    flags: list[str] | None = None,
) -> str:
    row, ev = await _evaluator_row(ctx)
    rubric = ev.rubric or {}
    cleaned = _clean_scores(rubric, scores)
    if isinstance(difficulty, bool) or not isinstance(difficulty, int) or not 1 <= difficulty <= 5:
        raise ToolError("difficulty must be an integer from 1 to 5.")
    if confidence not in CONFIDENCES:
        raise ToolError(f"confidence must be one of {', '.join(CONFIDENCES)}.")
    if not isinstance(rationale, str) or not rationale.strip():
        raise ToolError("rationale is required.")
    if len(rationale) > MAX_RATIONALE_CHARS:
        raise ToolError(f"rationale must be at most {MAX_RATIONALE_CHARS} characters.")
    flag_list = flags or []
    if not isinstance(flag_list, list) or not all(isinstance(f, str) for f in flag_list):
        raise ToolError("flags must be an array of strings.")
    if len(flag_list) > MAX_FLAGS:
        raise ToolError(f"flags may list at most {MAX_FLAGS} entries.")
    allowed = {f["key"] for f in rubric.get("flags") or []}
    unknown = sorted(set(flag_list) - allowed)
    if unknown:
        raise ToolError(
            f"Unknown flag(s): {', '.join(unknown)}. Allowed: {', '.join(sorted(allowed)) or 'none'}."
        )
    from tret.batches.scoring import target_class  # lazy: scoring imports the batch orchestrator

    target = await ctx.db.get(BatchTarget, row.target_id)
    row_class = target_class(target) if target is not None else None
    applicable = [c for c in rubric.get("criteria") or [] if criterion_applies(c.get("applies", "all"), row_class)]
    quality = compute_quality(rubric, cleaned, row_class)
    now = datetime.now(timezone.utc)
    if quality is None:
        if applicable:
            raise ToolError(
                "Score at least one criterion that applies to this result (a non-null score): "
                + ", ".join(c["key"] for c in applicable)
                + "."
            )
        row.status = "failed"
        row.error = "no applicable scores"
        row.finished_at = now
        await ctx.db.commit()
        return "No rubric criterion applies to this result; nothing was scored."
    row.scores = cleaned
    row.quality = quality
    row.difficulty = difficulty
    row.flags = list(dict.fromkeys(flag_list))
    row.confidence = confidence
    row.rationale = rationale.strip()
    row.status = "scored"
    row.error = None
    row.skip_reason = None
    row.finished_at = now
    await ctx.db.commit()
    return f"Evaluation recorded: quality {quality:.1f} of 100."


@builtin(
    "submit_batch_review",
    "Record the review of a whole batch's scores. Call exactly once, at the end. narrative is "
    "the overall picture; consistency_notes point at inconsistent scoring between similar cases; "
    "lessons are up to five concrete, actionable lessons for future runs, each tied to evidence "
    "in the cards.",
    {
        "type": "object",
        "properties": {
            "narrative": {"type": "string", "maxLength": MAX_NARRATIVE_CHARS},
            "consistency_notes": {
                "type": "array",
                "items": {"type": "string", "maxLength": MAX_NOTE_CHARS},
                "maxItems": MAX_NOTES,
            },
            "lessons": {
                "type": "array",
                "items": {"type": "string", "maxLength": MAX_LESSON_CHARS},
                "maxItems": MAX_LESSONS,
            },
        },
        "required": ["narrative"],
    },
)
async def submit_batch_review(
    ctx: RunContext,
    narrative: str,
    consistency_notes: list[str] | None = None,
    lessons: list[str] | None = None,
) -> str:
    run = await ctx.db.get(Run, ctx.run_id)
    task_input = (run.task_input or {}) if run is not None else {}
    raw = task_input.get(EVALUATION_ID_KEY)
    if not raw or not task_input.get(BATCH_REVIEW_KEY):
        raise ToolError("This run is not a batch review; submit_batch_review does nothing here.")
    ev = await ctx.db.get(BatchEvaluation, _uuid(raw, "batch evaluation"))
    if ev is None or ctx.workspace_id is None or ev.workspace_id != ctx.workspace_id:
        raise ToolError("This run's evaluation pass was not found; nothing was recorded.")
    # The task input is the caller's to write on a run started by hand; the pass
    # itself records which run it launched for the review, and only that run counts.
    if ev.summary_run_id != ctx.run_id or ev.status != "running":
        raise ToolError("This run is not the review of a running evaluation pass; nothing was recorded.")
    if not isinstance(narrative, str) or not narrative.strip():
        raise ToolError("narrative is required.")
    if len(narrative) > MAX_NARRATIVE_CHARS:
        raise ToolError(f"narrative must be at most {MAX_NARRATIVE_CHARS} characters.")
    notes = consistency_notes or []
    lesson_list = lessons or []
    for name, values, count, size in (
        ("consistency_notes", notes, MAX_NOTES, MAX_NOTE_CHARS),
        ("lessons", lesson_list, MAX_LESSONS, MAX_LESSON_CHARS),
    ):
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            raise ToolError(f"{name} must be an array of strings.")
        if len(values) > count:
            raise ToolError(f"{name} may list at most {count} entries.")
        if any(len(v) > size for v in values):
            raise ToolError(f"each of {name} must be at most {size} characters.")
    ev.summary = {
        "narrative": narrative.strip(),
        "consistency_notes": [n.strip() for n in notes if n.strip()],
        "lessons": [x.strip() for x in lesson_list if x.strip()],
    }
    await ctx.db.commit()
    await _propose_review_lessons(ev.id, run.harness_id if run is not None else None)
    try:
        await ctx.db.refresh(ev)
    except Exception:
        logger.exception("refreshing the evaluation pass failed")
    return "Batch review recorded."


async def _review_harness_id(
    db, ev: BatchEvaluation, batch: Batch, evaluator_harness_id: uuid.UUID | None
) -> uuid.UUID | None:
    """The harness the batch's targets actually ran on (most common
    `Run.harness_id` among its current target runs, this workspace, never the
    evaluator's); only when no target has run, the profile's default harness."""
    from sqlalchemy import func, select

    from tret.batches.profiles import profile_for
    from tret.db.models import Harness

    counts = (
        await db.execute(
            select(Run.harness_id, func.count())
            .join(BatchTarget, BatchTarget.run_id == Run.id)
            .where(BatchTarget.collection_id == batch.id)
            .group_by(Run.harness_id)
            .order_by(func.count().desc(), Run.harness_id)
        )
    ).all()
    for harness_id, _n in counts:
        if harness_id is None or harness_id == evaluator_harness_id:
            continue
        h = await db.get(Harness, harness_id)
        if h is not None and h.workspace_id == ev.workspace_id:
            return h.id
    if counts:
        return None
    default = await profile_for(batch).harness_for(db, batch)
    if default is None or default.id == evaluator_harness_id:
        return None
    return default.id


async def _propose_review_lessons(
    ev_id: uuid.UUID, evaluator_harness_id: uuid.UUID | None
) -> None:
    """Turn the review's lessons into pending, harness-scoped proposals for a
    person to approve (`services/lessons.py::sync_review_proposals`), and store
    one result per lesson on the pass (`summary["proposals"]`,
    `summary["lessons_harness_id"]`). Runs in its OWN session, so a failure here
    can never roll back or expire the engine's session; the review itself is
    already committed and the judge is not told."""
    from tret.batches import core
    from tret.services import lessons as lessons_service

    try:
        async with core._session_factory()() as db:
            ev = await db.get(BatchEvaluation, ev_id)
            batch = await db.get(Batch, ev.collection_id) if ev is not None else None
            if ev is None or batch is None:
                return
            texts = list((ev.summary or {}).get("lessons") or [])
            harness_id = await _review_harness_id(db, ev, batch, evaluator_harness_id)
            if harness_id is None:
                # Still clear this batch's earlier pending proposals.
                await lessons_service.clear_pending_review_proposals(db, ev)
                proposals = [
                    {
                        "text": t,
                        "lesson_id": None,
                        "outcome": "unresolved",
                        "reason": "no harness could be resolved for this batch",
                    }
                    for t in texts
                ]
            else:
                proposals = await lessons_service.sync_review_proposals(
                    db, ev, harness_id, texts, batch.name
                )
            # Reassigned (not mutated) so SQLAlchemy sees the JSONB change.
            ev.summary = {
                **(ev.summary or {}),
                "proposals": proposals,
                "lessons_harness_id": str(harness_id) if harness_id else None,
            }
            await db.commit()
    except Exception:
        logger.exception("proposing batch-review lessons failed")


async def _pairwise_row(ctx: RunContext) -> tuple[PairwiseJudgment, str, dict]:
    """The judgment this run answers, the presentation order of this run and its
    (still unanswered) call entry. Refuses anywhere but a pairwise judge run the
    judgment itself launched."""
    run = await ctx.db.get(Run, ctx.run_id)
    task_input = (run.task_input or {}) if run is not None else {}
    raw = task_input.get(PAIRWISE_ID_KEY)
    order = task_input.get(PAIRWISE_ORDER_KEY)
    if not raw and task_input.get(TARGET_EVALUATION_ID_KEY):
        raise ToolError(
            "This run scores one target, not a pairwise comparison: call submit_evaluation instead."
        )
    if not raw or order not in PAIRWISE_ORDERS:
        raise ToolError("This run is not a pairwise judgment; submit_pairwise does nothing here.")
    row = await ctx.db.get(PairwiseJudgment, _uuid(raw, "pairwise judgment"))
    if row is None or ctx.workspace_id is None or row.workspace_id != ctx.workspace_id:
        raise ToolError("This run's pairwise judgment was not found; nothing was recorded.")
    if row.status != "running":
        raise ToolError("This pairwise judgment is no longer waiting for an answer; nothing was recorded.")
    entry = next(
        (
            c
            for c in row.calls or []
            if isinstance(c, dict) and c.get("order") == order and c.get("evaluator_run_id") == str(ctx.run_id)
        ),
        None,
    )
    if entry is None:
        raise ToolError("This run is not the judge of this pairwise judgment; nothing was recorded.")
    if entry.get("preferred") is not None:
        raise ToolError("This order was already answered; nothing more was recorded.")
    return row, order, entry


@builtin(
    "submit_pairwise",
    "Record which of the two outputs shown (Output 1 and Output 2) is the better delivery for the "
    "task under the rubric. Call exactly once, at the end. preferred is first, second or tie (tie "
    "only when there is no material difference). criteria has one entry per rubric criterion that "
    "applies: first, second or tie. confidence is low, medium or high. rationale is a few sentences "
    "naming what decided it.",
    {
        "type": "object",
        "properties": {
            "preferred": {"type": "string", "enum": list(PAIRWISE_PREFERENCES)},
            "criteria": {
                "type": "object",
                "description": "criterion key -> first | second | tie, every applicable rubric criterion.",
                "additionalProperties": {"type": "string", "enum": list(PAIRWISE_PREFERENCES)},
            },
            "confidence": {"type": "string", "enum": list(CONFIDENCES)},
            "rationale": {"type": "string", "maxLength": MAX_RATIONALE_CHARS},
        },
        "required": ["preferred", "criteria", "confidence", "rationale"],
    },
)
async def submit_pairwise(
    ctx: RunContext, preferred: str, criteria: dict, confidence: str, rationale: str
) -> str:
    row, _order, entry = await _pairwise_row(ctx)
    if preferred not in PAIRWISE_PREFERENCES:
        raise ToolError(f"preferred must be one of {', '.join(PAIRWISE_PREFERENCES)}.")
    if confidence not in CONFIDENCES:
        raise ToolError(f"confidence must be one of {', '.join(CONFIDENCES)}.")
    if not isinstance(rationale, str) or not rationale.strip():
        raise ToolError("rationale is required.")
    if len(rationale) > MAX_RATIONALE_CHARS:
        raise ToolError(f"rationale must be at most {MAX_RATIONALE_CHARS} characters.")
    if not isinstance(criteria, dict):
        raise ToolError("criteria must be an object keyed by criterion.")
    from tret.batches.pairwise import rubric_for_row  # lazy: pairwise imports the batch orchestrator

    rubric = await rubric_for_row(ctx.db, row)
    if rubric is None:
        raise ToolError("The rubric this judgment started with has changed; nothing was recorded.")
    applicable = [
        c["key"] for c in rubric.get("criteria") or [] if criterion_applies(c.get("applies", "all"), "success")
    ]
    unknown = sorted(set(criteria) - set(applicable))
    if unknown:
        raise ToolError(f"Unknown criterion key(s): {', '.join(unknown)}. Criteria: {', '.join(applicable)}.")
    missing = [k for k in applicable if k not in criteria]
    if missing:
        raise ToolError(f"criteria must include every applicable criterion; missing: {', '.join(missing)}.")
    for key, value in criteria.items():
        if value not in PAIRWISE_PREFERENCES:
            raise ToolError(f"criteria.{key} must be one of {', '.join(PAIRWISE_PREFERENCES)}.")
    done = {
        **entry,
        "preferred": preferred,
        "criteria": {k: criteria[k] for k in applicable},
        "confidence": confidence,
        "rationale": rationale.strip(),
    }
    row.calls = [done if c is entry else c for c in row.calls]
    await ctx.db.commit()
    return "Pairwise judgment recorded."
