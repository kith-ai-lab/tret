"""The human-agreement gate on router learning from a batch family.

The batch evaluator is a model judging a model. Letting its scores steer routing
on its own would let a model's opinion move which model runs next, so a family's
evaluator-blended evidence (`run_outcomes.evaluated_quality_score`) is read by the
router only after people have spot-checked that family's scores and mostly agreed
(`evaluation.review_agrees`).

The gate counts reviews over the CURRENT passes of the WORKSPACE's batches of the
family, restricted to the family's comparable set (the same most-common
rubric / rubric version / judge selection the family leaderboard uses), so a review
of a pass scored under another rubric or judge does not vouch for this one. A
family key embeds the workspace id (`<family_key>@<workspace_id>`): another
workspace's reviews are never visible here. Nothing a model does can open it: the
inputs are `review` payloads written only by `evaluation.review_target`, a person's
spot check.
"""

from __future__ import annotations

import logging
import uuid
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tret.config import get_settings

log = logging.getLogger("tret.gate")


def split_task_family(task_family: str) -> tuple[str, uuid.UUID] | None:
    """`(family_key, workspace_id)` from `<family_key>@<workspace_id>`, or None."""
    key, sep, workspace = (task_family or "").rpartition("@")
    if not sep or not key:
        return None
    try:
        return key, uuid.UUID(workspace)
    except ValueError:
        return None


def _closed(mode: str, min_checks: int, min_agreement: float, **extra) -> dict:
    return {
        "open": False,
        "spot_checks": 0,
        "agreeing": 0,
        "agreement": None,
        "needed_checks": min_checks,
        "needed_agreement": min_agreement,
        "mode": mode,
        "rubric_version": None,
        "judge_model": None,
        **extra,
    }


async def family_gate(db: AsyncSession, task_family: str) -> dict:
    """`{open, spot_checks, agreeing, agreement, needed_checks, needed_agreement,
    mode, rubric_version, judge_model}` for one `<family_key>@<workspace_id>`.

    Two light queries, no run or finding is ever loaded: (1) the workspace's
    evaluation passes of the family (columns only), from which each batch's current
    LIVE pass and the comparable set (the family leaderboard's own rule,
    `families._comparable_set`) are picked in Python; (2) the scored, reviewed,
    non-stale target rows of those passes. Open iff the mode is "gated", at least
    `needed_checks` such rows carry a review and at least `needed_agreement` of them
    agree. Never raises: any failure reads as a closed gate."""
    from tret.batches import evaluation, families, scoring  # lazy: they import the engine
    from tret.db.models import BatchEvaluation, BatchTarget, TargetEvaluation

    settings = get_settings()
    mode = settings.router_family_priors
    min_checks = int(settings.router_family_gate_min_checks)
    min_agreement = float(settings.router_family_gate_min_agreement)
    parsed = split_task_family(task_family)
    if parsed is None:
        return _closed(mode, min_checks, min_agreement)
    key, workspace_id = parsed
    try:
        passes = (
            await db.execute(
                select(
                    BatchEvaluation.id,
                    BatchEvaluation.collection_id,
                    BatchEvaluation.created_at,
                    BatchEvaluation.status,
                    BatchEvaluation.rubric_key,
                    BatchEvaluation.rubric_version,
                    BatchEvaluation.judge_model,
                ).where(BatchEvaluation.workspace_id == workspace_id, BatchEvaluation.family_key == key)
            )
        ).all()
        current: dict = {}
        for p in sorted(passes, key=lambda p: scoring.core._aware(p.created_at), reverse=True):
            if p.status not in scoring.DEAD_PASS_STATUSES and p.collection_id not in current:
                current[p.collection_id] = p  # a batch with only dead passes has none
        comparable, members = families._comparable_set([SimpleNamespace(current=p) for p in current.values()])
        checks = agreeing = 0
        if members:
            ids = [d.current.id for d in members]
            rows = (
                await db.execute(
                    select(
                        TargetEvaluation.quality,
                        TargetEvaluation.review,
                        TargetEvaluation.attempt,
                        BatchTarget.attempts,
                    )
                    .join(BatchTarget, BatchTarget.id == TargetEvaluation.target_id)
                    .where(TargetEvaluation.evaluation_id.in_(ids), TargetEvaluation.status == "scored")
                )
            ).all()
            for quality, review, attempt, target_attempts in rows:
                if not isinstance(review, dict) or (attempt or 0) != (target_attempts or 0):
                    continue
                verdict = evaluation.review_agrees(SimpleNamespace(review=review, quality=quality))
                if verdict is None:
                    continue
                checks += 1
                agreeing += 1 if verdict else 0
        agreement = round(agreeing / checks, 4) if checks else None
        return {
            "open": bool(
                mode == "gated" and checks >= min_checks and checks > 0 and agreeing / checks >= min_agreement
            ),
            "spot_checks": checks,
            "agreeing": agreeing,
            "agreement": agreement,
            "needed_checks": min_checks,
            "needed_agreement": min_agreement,
            "mode": mode,
            "rubric_version": comparable[1] if comparable else None,
            "judge_model": comparable[2] if comparable else None,
        }
    except Exception:  # noqa: BLE001 - a gate that cannot be read is a closed gate
        log.exception("could not read the router gate for %s", task_family)
        return _closed(mode, min_checks, min_agreement)


async def safe_family_gate(db: AsyncSession, task_family: str) -> dict | None:
    """`family_gate` for display surfaces: never raises, None when unreadable."""
    try:
        return await family_gate(db, task_family)
    except Exception:  # noqa: BLE001
        log.exception("could not build the router gate summary")
        return None
