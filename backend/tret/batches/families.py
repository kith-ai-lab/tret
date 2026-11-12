"""Families of batches: the leaderboard and the per-model aggregate.

A family (`scoring.family_key`) is a set of batches that ran the same task. The family
page lists them side by side; `family_models` reduces them to one row per model, over
the batches whose current evaluation pass shares the family's COMPARABLE SET (the most
common rubric key + version + judge). The aggregate is reusable: the model-advice
endpoint reads it. No model calls; on-demand only.
"""
from __future__ import annotations

import math
import statistics
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tret.batches import scoring
from tret.db.models import Batch, BatchEvaluation

PARETO_MIN_SCORED = 3
INTERVAL_MIN = 3


async def _workspace_batches(db: AsyncSession, workspace_id: uuid.UUID, profile: str | None = None) -> list[Batch]:
    query = select(Batch).where(Batch.workspace_id == workspace_id).order_by(Batch.created_at.desc())
    if profile is not None:
        query = query.where(Batch.profile == profile)
    return list((await db.execute(query.execution_options(populate_existing=True))).scalars())


async def list_families(db: AsyncSession, workspace_id: uuid.UUID) -> list[dict]:
    """`[{family_key, family_label, profile, batches, evaluated_batches, last_created_at}]`,
    newest first. `evaluated_batches` counts batches with a pass that is not cancelled or failed."""
    batches = await _workspace_batches(db, workspace_id)
    evaluated: set[uuid.UUID] = set()
    if batches:
        evaluated = set(
            (
                await db.execute(
                    select(BatchEvaluation.collection_id)
                    .where(
                        BatchEvaluation.workspace_id == workspace_id,
                        BatchEvaluation.status.not_in(scoring.DEAD_PASS_STATUSES),
                    )
                    .distinct()
                )
            ).scalars()
        )
    groups: dict[str, list[Batch]] = {}
    for b in batches:
        groups.setdefault(scoring.family_key(b), []).append(b)
    out = []
    for key, members in groups.items():
        newest = max(members, key=lambda b: b.created_at)
        out.append(
            {
                "family_key": key,
                "family_label": scoring.family_label(newest),
                "profile": newest.profile or "disclosure",
                "batches": len(members),
                "evaluated_batches": sum(1 for b in members if b.id in evaluated),
                "last_created_at": newest.created_at.isoformat() if newest.created_at else None,
            }
        )
    out.sort(key=lambda f: f["last_created_at"] or "", reverse=True)
    return out


async def family_batches(db: AsyncSession, workspace_id: uuid.UUID, key: str) -> list[Batch]:
    """The workspace's batches of one family, newest first (the key is computed, so the
    profile's batches are loaded and filtered here)."""
    profile = key.split(":", 1)[0]
    return [b for b in await _workspace_batches(db, workspace_id, profile) if scoring.family_key(b) == key]


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def _live(data: scoring.BatchData):
    """The batch's current pass only when it is not cancelled or failed."""
    c = data.current
    return c if c is not None and c.status not in scoring.DEAD_PASS_STATUSES else None


def _comparable_set(datas: list[scoring.BatchData]) -> tuple[tuple[str, str, str] | None, list[scoring.BatchData]]:
    """The most common (rubric_key, rubric_version, judge_model) among current passes
    (ties: the one with the newest pass) and the batches that share it."""
    sets: dict[tuple[str, str, str], list[scoring.BatchData]] = {}
    for d in datas:
        c = _live(d)
        if c is not None:
            sets.setdefault((c.rubric_key, c.rubric_version, c.judge_model), []).append(d)
    if not sets:
        return None, []
    best = max(
        sets,
        key=lambda k: (len(sets[k]), max(scoring.core._aware(_live(d).created_at) for d in sets[k])),
    )
    return best, sets[best]


def group_stats(ds: list[scoring.TargetData], *, intervals: bool = True) -> dict:
    """Aggregate of one group of targets (a model's, or an experiment arm's): yield, quality
    with its 90% interval, cost and energy per acceptable result."""
    classes = [d.cls for d in ds]
    denominator = len(ds) - sum(1 for c in classes if c in scoring.EXCLUDED_FROM_YIELD)
    acceptable_ds = [d for d in ds if d.acceptable and d.cls not in scoring.EXCLUDED_FROM_YIELD]
    qualities = [d.quality for d in ds if d.quality is not None]
    residuals = [
        float(d.evaluation.relative["residual"])
        for d in ds
        if d.quality is not None
        and d.evaluation is not None
        and isinstance(d.evaluation.relative, dict)
        and d.evaluation.relative.get("residual") is not None
    ]
    cost = sum(d.metrics["cost_usd"] for d in ds)
    energy = scoring._sum_known([d.metrics["energy_wh"] for d in ds])
    n_ok = len(acceptable_ds)
    iterations = [d.metrics["iterations"] for d in ds if d.metrics["iterations"] is not None]
    return {
        "targets": len(ds),
        "delivered": sum(1 for c in classes if c == "success"),
        "scored": len(qualities),
        "acceptable": n_ok,
        "yield": scoring._ratio(n_ok, denominator),
        "quality_mean": round(statistics.fmean(qualities), 1) if qualities else None,
        "quality_interval90": scoring.bootstrap_interval(qualities) if intervals and len(qualities) >= INTERVAL_MIN else None,
        "mean_residual": round(statistics.fmean(residuals), 1) if residuals else None,
        "cost_usd": round(cost, 6),
        "cost_per_acceptable_usd": round(cost / n_ok, 6) if n_ok else None,
        "energy_per_acceptable_wh": round(energy / n_ok, 4) if n_ok and energy is not None else None,
        "mean_iterations": round(statistics.fmean(iterations), 1) if iterations else None,
        "p95_iterations": _p95([d.metrics["iterations"] for d in acceptable_ds if d.metrics["iterations"] is not None]),
        "p95_cost_usd": _p95([d.metrics["cost_usd"] for d in acceptable_ds]),
        "infra_failures": sum(1 for c in classes if c == "infra"),
        "pareto": False,
    }


def models_from(datas: list[scoring.BatchData]) -> dict:
    """`{comparable, models, excluded_batches}` over already-collected batches."""
    best, included = _comparable_set(datas)
    comparable = (
        {"rubric_key": best[0], "rubric_version": best[1], "judge_model": best[2]} if best is not None else None
    )
    per_model: dict[str, list[tuple[scoring.BatchData, scoring.TargetData]]] = {}
    for data in included:
        for d in data.targets:
            model = d.metrics.get("model_used")
            if model:
                per_model.setdefault(model, []).append((data, d))
    models = []
    for model, items in per_model.items():
        ds = [d for _, d in items]
        models.append(
            {
                "model": model,
                "batches": len({data.batch.id for data, _ in items}),
                **group_stats(ds),
            }
        )
    frontier = [
        m
        for m in models
        if m["scored"] >= PARETO_MIN_SCORED and m["quality_mean"] is not None and m["cost_per_acceptable_usd"] is not None
    ]
    for m in frontier:
        m["pareto"] = not any(
            o is not m
            and o["quality_mean"] >= m["quality_mean"]
            and o["cost_per_acceptable_usd"] <= m["cost_per_acceptable_usd"]
            and (o["quality_mean"] > m["quality_mean"] or o["cost_per_acceptable_usd"] < m["cost_per_acceptable_usd"])
            for o in frontier
        )
    models.sort(key=lambda m: (m["quality_mean"] is None, -(m["quality_mean"] or 0.0), m["model"]))
    return {"comparable": comparable, "models": models, "excluded_batches": len(datas) - len(included)}


async def family_models(db: AsyncSession, workspace_id: uuid.UUID, key: str) -> dict:
    """Per-model aggregate over the family's comparable batches:
    `{comparable: {rubric_key, rubric_version, judge_model} | None, models: [...],
    excluded_batches: n}`. Targets with no `model_used` are left out."""
    batches = await family_batches(db, workspace_id, key)
    return models_from([await scoring.collect(db, b) for b in batches])


async def family_leaderboard(db: AsyncSession, workspace_id: uuid.UUID, key: str) -> dict:
    """The family page's payload (a flat object; `models` is the per-model list)."""
    batches = await family_batches(db, workspace_id, key)
    datas = [await scoring.collect(db, b) for b in batches]
    agg = models_from(datas)
    best = agg["comparable"]
    rows: list[dict[str, Any]] = []
    row_keys: set[str] = set()
    for data in datas:
        card = await scoring.scorecard_from(db, data)
        row_keys.update(d.row for d in data.targets)
        ev = _live(data)
        quality = card["quality"] or {}
        evaluation = card["evaluation"] or {}
        rows.append(
            {
                "batch_id": card["batch_id"],
                "name": card["name"],
                "status": card["status"],
                "created_at": data.batch.created_at.isoformat() if data.batch.created_at else None,
                "started_at": data.batch.started_at.isoformat() if data.batch.started_at else None,
                "targets": card["targets"],
                "classes": card["classes"],
                "acceptable": card["acceptable"],
                "yield": card["yield"],
                "written_share": card["written_share"],
                "quality_mean": quality.get("mean"),
                "quality_median": quality.get("median"),
                "mean_residual": quality.get("mean_residual"),
                "cost_usd": card["cost_usd"],
                "cost_per_acceptable_usd": card["cost_per_acceptable_usd"],
                "energy_wh": card["energy_wh"],
                "energy_per_acceptable_wh": card["energy_per_acceptable_wh"],
                "wall_seconds": card["wall_seconds"],
                "models": card["models"],
                "judge_model": ev.judge_model if ev is not None else None,
                "rubric_version": ev.rubric_version if ev is not None else None,
                "comparable": ev is not None
                and best is not None
                and (ev.rubric_key, ev.rubric_version, ev.judge_model)
                == (best["rubric_key"], best["rubric_version"], best["judge_model"]),
                "spot_checks": evaluation.get("spot_checks", 0),
                "agreement": evaluation.get("agreement"),
            }
        )
    newest = batches[0] if batches else None
    return {
        "family_key": key,
        "family_label": scoring.family_label(newest) if newest is not None else key,
        "profile": (newest.profile or "disclosure") if newest is not None else key.split(":", 1)[0],
        "comparable": agg["comparable"],
        "excluded_batches": agg["excluded_batches"],
        "row_keys": len(row_keys),
        "batches": rows,
        "models": agg["models"],
    }
