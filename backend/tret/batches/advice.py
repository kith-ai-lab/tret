"""Advice for a batch that is about to be created: what earlier batches of the same
family suggest for the NEXT one. Read-only; nothing here is applied unless a person
(or the calling agent) picks it. Built on `families.family_models`, the per-model
aggregate over the family's comparable batches. No model calls.
"""
from __future__ import annotations

import math
import uuid

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from tret.batches import core, families, scoring
from tret.db.models import Harness
from tret.router_llm.objectives import objective_of

MIN_ADVICE_ROWS = 5
ITERATION_HEADROOM = 1.25
COST_HEADROOM = 1.5
MAX_ITERATIONS_CEILING = 50
# Quality points a model may trail the best lower bound by and still be considered,
# per objective ("quality" takes the best outright).
BAND_POINTS = {"eco": 5.0}
DEFAULT_BAND_POINTS = 3.0
QUALITY_FIRST_OBJECTIVES = ("quality",)
INFRA_FAILURE_SHARE = 0.10


def _lower(m: dict) -> float | None:
    interval = m.get("quality_interval90")
    return float(interval[0]) if interval else None


def _eligible(m: dict) -> bool:
    return (m.get("scored") or 0) >= MIN_ADVICE_ROWS and _lower(m) is not None


def _fmt(x: float) -> str:
    return f"{x:.1f}"


def _recommend_model(models: list[dict], objective: str) -> tuple[dict | None, str]:
    eligible = [m for m in models if _eligible(m)]
    if not eligible:
        return None, (
            f"No model has at least {MIN_ADVICE_ROWS} scored rows with a quality interval yet, "
            "so there is nothing to recommend."
        )
    best = max(eligible, key=lambda m: (_lower(m), -(m["cost_per_acceptable_usd"] if m["cost_per_acceptable_usd"] is not None else math.inf)))
    best_lower = _lower(best)
    if objective in QUALITY_FIRST_OBJECTIVES:
        return best, (
            f"best quality lower bound ({_fmt(best_lower)}) of the models with enough scored rows; "
            f"{best['scored']} scored rows"
        )
    band = BAND_POINTS.get(objective, DEFAULT_BAND_POINTS)
    close = [
        m
        for m in eligible
        if _lower(m) >= best_lower - band and m.get("cost_per_acceptable_usd") is not None
    ]
    if not close:
        return best, (
            f"best quality lower bound ({_fmt(best_lower)}); no model within {band:g} points has a "
            f"known cost per acceptable result; {best['scored']} scored rows"
        )
    pick = min(close, key=lambda m: (m["cost_per_acceptable_usd"] if m["cost_per_acceptable_usd"] is not None else math.inf, -_lower(m), m["model"]))
    lower = _lower(pick)
    if pick is best:
        return pick, (
            f"best quality lower bound ({_fmt(lower)}) and the cheapest within {band:g} points of it: "
            f"${pick['cost_per_acceptable_usd']:.2f} per acceptable result over {pick['scored']} scored rows"
        )
    return pick, (
        f"cheapest model whose quality lower bound ({_fmt(lower)}) is within {band:g} points of the best "
        f"({_fmt(best_lower)}): ${pick['cost_per_acceptable_usd']:.2f} per acceptable result "
        f"over {pick['scored']} scored rows"
    )


async def _harness_for(db: AsyncSession, workspace_id: uuid.UUID, profile: str, harness_id) -> Harness | None:
    if profile == "prompt":
        harness = await db.get(Harness, harness_id) if harness_id is not None else None
        if harness is None or harness.workspace_id != workspace_id:
            return None
        return harness
    from tret.batches.profiles.disclosure import _collector_harness

    return await _collector_harness(db, workspace_id)


async def _concurrency_advice(db: AsyncSession, batches: list) -> tuple[int | None, str | None]:
    """A lower concurrency when the family's most recent batch lost more than 10% of
    its targets to infrastructure failures or timeouts."""
    if not batches:
        return None, None
    latest = batches[0]
    targets = await core.load_targets(db, [latest.id])
    if not targets:
        return None, None
    bad = sum(1 for t in targets if scoring.target_class(t) in ("infra", "timeout"))
    share = bad / len(targets)
    if share <= INFRA_FAILURE_SHARE:
        return None, None
    current = core.clamp_concurrency((latest.settings or {}).get("concurrency"))
    suggested = max(1, current - 1)
    if suggested >= current:
        return None, None
    return suggested, (
        f"the most recent batch lost {bad} of {len(targets)} targets ({share * 100:.0f}%) to "
        f"infrastructure failures or timeouts at concurrency {current}"
    )


async def batch_advice(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    profile: str,
    fiscal_year: int | None = None,
    harness_id: uuid.UUID | None = None,
    template: str | None = None,
) -> dict:
    """What earlier batches of the family a new batch would join suggest for it:
    `{family_key, family_label, objective, evidence: {batches, comparable, models},
    recommendation: {...} | None, notes}`. See docs/batch-runs.md."""
    key = scoring.family_key_for(
        profile, fiscal_year=fiscal_year, harness_id=harness_id, template=template or ""
    )
    harness = await _harness_for(db, workspace_id, profile, harness_id)
    if profile == "prompt" and (harness is None or harness.is_archived):
        raise HTTPException(422, "harness_id: no such harness in this workspace (or it is archived)")
    objective = objective_of(harness.model_policy if harness is not None else None)
    if profile == "disclosure":
        label = f"Disclosure FY{fiscal_year}"
    else:
        label = f"Prompt · {(harness.name if harness is not None else None) or 'harness'} · " + " ".join(
            (template or "").split()
        )[:40]

    batches = await families.family_batches(db, workspace_id, key)
    agg = families.models_from([await scoring.collect(db, b) for b in batches])
    models = agg["models"]
    notes: list[str] = []
    recommendation: dict | None = None

    if not batches:
        notes.append("No earlier batch of this kind in this workspace.")
    elif len(batches) == 1:
        notes.append("Only one earlier batch of this kind: treat the numbers as a first impression.")
    if agg["excluded_batches"]:
        notes.append(
            f"{agg['excluded_batches']} earlier batch(es) were left out because they were scored with a "
            "different rubric or judge (or not scored at all)."
        )
    thin = [m["model"] for m in models if (m.get("scored") or 0) < MIN_ADVICE_ROWS]
    if thin and models:
        notes.append(
            f"{len(thin)} model(s) have fewer than {MIN_ADVICE_ROWS} scored rows and are never recommended."
        )

    pick, reason = _recommend_model(models, objective)
    concurrency, concurrency_reason = await _concurrency_advice(db, batches)
    if pick is None:
        if batches:
            notes.append(reason)
        if concurrency is not None:
            recommendation = {
                "model": None,
                "reason": reason,
                "max_iterations": None,
                "max_iterations_basis": None,
                "cost_cap_usd": None,
                "cost_cap_basis": None,
                "concurrency": concurrency,
                "concurrency_reason": concurrency_reason,
            }
    else:
        p95_iterations = pick.get("p95_iterations")
        max_iterations = (
            min(MAX_ITERATIONS_CEILING, math.ceil(ITERATION_HEADROOM * p95_iterations))
            if p95_iterations is not None
            else None
        )
        p95_cost = pick.get("p95_cost_usd")
        cost_cap = (
            max(0.01, math.ceil(round(COST_HEADROOM * 100 * p95_cost, 6)) / 100) if p95_cost else None
        )  # a free or unreported-cost model (p95 0) gets no cap
        recommendation = {
            "model": pick["model"],
            "reason": reason,
            "max_iterations": max_iterations,
            "max_iterations_basis": (
                f"1.25 x the model's 95th-percentile of {p95_iterations:g} iterations on acceptable runs"
                + (f", capped at {MAX_ITERATIONS_CEILING}" if max_iterations == MAX_ITERATIONS_CEILING else "")
                if max_iterations is not None
                else None
            ),
            "cost_cap_usd": cost_cap,
            "cost_cap_basis": (
                f"1.5 x the model's 95th-percentile cost of ${p95_cost:.3f} per acceptable run, rounded up to the cent"
                if cost_cap is not None
                else None
            ),
            "concurrency": concurrency,
            "concurrency_reason": concurrency_reason,
        }
    return {
        "family_key": key,
        "family_label": label,
        "objective": objective,
        "evidence": {"batches": len(batches), "comparable": agg["comparable"], "models": models},
        "recommendation": recommendation,
        "notes": notes,
    }
