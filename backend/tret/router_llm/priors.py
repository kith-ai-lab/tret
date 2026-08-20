"""What the track record says — turning recorded outcomes into routing evidence.

`run_outcomes` holds one row per model a finished run used — usually one, and
two or more for a run that changed model part-way. This module answers the only
question routing actually has: *for this shape of task, under this objective, how
has each candidate model actually done?* Everything here exists to stop that
question being answered badly, because a naive average over that table is
actively misleading in four separate ways.

**A lucky run is not a track record.** Scores are shrunk toward the pooled mean
of every model on the same key, so a model with two runs sits near its peers and
a model with fifty moves away from them on its own evidence. Shrinkage is toward
*peers on the same task*, not toward a fixed constant, which is also the only
cheap correction available for the fact that some shapes are simply harder.

**Unknown is not bad.** Below `MIN_EFFECTIVE_SAMPLES` a model has no prior at
all — it is omitted, and callers fall back to their existing ordering. A model
that has never been tried must not be ranked last for never having been tried,
or nothing new is ever tried again.

**Old evidence is weaker evidence.** Weights decay with a half-life
(`HALF_LIFE_DAYS`), so a model that was poor a year ago and has since been
updated is not condemned by its own history.

**Size matters more than it looks.** A model that handles a 2k-token chat turn
well may be the wrong answer at 300k. Rows from other size bands still count —
throwing them away leaves nothing to learn from — but at `OFF_BAND_WEIGHT`.

What this module does **not** do is decide anything. It reports numbers with
their uncertainty attached; `router.py`, `objectives.py` and `fallback.py` decide
what to do about them, and none of them may use a prior to widen a policy.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Protocol

from sqlalchemy import select

from tret.db.models import RunOutcome
from tret.router_llm.outcomes import NON_QUALITY_CLASSES

PRIORS_VERSION = "priors-v1"

# Evidence older than this is not read at all. Beyond a few months the catalog
# has usually moved under the data anyway.
WINDOW_DAYS = 180
# Weight halves every this many days.
HALF_LIFE_DAYS = 30.0
# Rows from a different input-size band still count, but not equally.
OFF_BAND_WEIGHT = 0.25
# Below this decayed sample count a model has no prior. Deliberately expressed in
# *effective* samples, so five runs from last year do not qualify.
MIN_EFFECTIVE_SAMPLES = 5.0
# How hard shrinkage pulls toward the pooled mean, in units of effective samples.
SHRINKAGE_STRENGTH = 4.0
# Where a key with no pooled evidence at all shrinks to. Mid-scale on purpose:
# it is an admission of ignorance, not an opinion.
NEUTRAL_QUALITY = 0.5
# One-sided ~90%. Used wherever evidence is allowed to *override* an existing
# rule rather than merely break a tie — see fallback.py.
Z_CONSERVATIVE = 1.2816
# Bound on the rows read for one key, so this stays cheap on a long-lived
# install. The window and the ordering mean the cap drops the oldest evidence
# first, which is also the evidence that decay has already made nearly weightless.
ROW_SCAN_LIMIT = 2000
# How long an aggregate is reused before it is recomputed.
CACHE_TTL_SECONDS = 60.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ModelPrior:
    """One model's record on one routing key, with its uncertainty attached.

    `quality_mean` is shrunk and `quality_ci_low` is its conservative lower
    bound. Callers that merely order candidates should read the mean; callers
    that override an existing rule should read the lower bound, because being
    wrong there costs more.
    """

    model_id: str
    runs: int  # rows actually observed
    effective_n: float  # after time decay and off-band discounting
    quality_mean: float  # shrunk toward the pooled mean for this key
    quality_raw: float  # unshrunk, for display
    quality_ci_low: float
    delivered_rate: float
    failure_rate: float
    mean_cost_usd: float
    mean_output_tokens: float
    mean_iterations: float
    mean_energy_wh: float | None
    approvals: int
    rejections: int
    error_kinds: dict[str, int] = field(default_factory=dict)
    last_seen: str | None = None

    def to_json(self) -> dict:
        return {
            "model_id": self.model_id,
            "runs": self.runs,
            "effective_n": round(self.effective_n, 2),
            "quality_mean": round(self.quality_mean, 4),
            "quality_raw": round(self.quality_raw, 4),
            "quality_ci_low": round(self.quality_ci_low, 4),
            "delivered_rate": round(self.delivered_rate, 4),
            "failure_rate": round(self.failure_rate, 4),
            "mean_cost_usd": round(self.mean_cost_usd, 6),
            "mean_output_tokens": round(self.mean_output_tokens),
            "mean_iterations": round(self.mean_iterations, 2),
            "mean_energy_wh": (
                round(self.mean_energy_wh, 4) if self.mean_energy_wh is not None else None
            ),
            "approvals": self.approvals,
            "rejections": self.rejections,
            "error_kinds": self.error_kinds,
            "last_seen": self.last_seen,
        }


class PriorsProvider(Protocol):
    """What the router and the engine need. Implemented by both classes below."""

    async def for_key(
        self, *, task_shape: str, objective: str, size_band: str | None = None
    ) -> dict[str, ModelPrior]:
        ...

    def invalidate(self) -> None:
        """Drop any cached aggregate. Called when a run adds new evidence."""
        ...


class NoPriors:
    """The cold-start provider: no evidence, ever.

    Injected wherever routing must be reproducible regardless of what the
    database has seen — the golden-run evals and both benchmark arms — and used
    as the default so that a caller which never wires up priors behaves exactly
    as tret did before this module existed.
    """

    async def for_key(
        self, *, task_shape: str, objective: str, size_band: str | None = None
    ) -> dict[str, ModelPrior]:
        return {}

    def invalidate(self) -> None:
        """No cache, nothing to drop — but the engine calls this on every run."""


# ── aggregation ──────────────────────────────────────────────────────────────
@dataclass
class _Accumulator:
    """Weighted running totals for one model on one key."""

    runs: int = 0
    weight: float = 0.0
    quality: float = 0.0
    quality_sq: float = 0.0
    delivered: float = 0.0
    failed: float = 0.0
    cost: float = 0.0
    output_tokens: float = 0.0
    iterations: float = 0.0
    energy: float = 0.0
    energy_weight: float = 0.0
    approvals: int = 0
    rejections: int = 0
    error_kinds: dict[str, int] = field(default_factory=dict)
    last_seen: datetime | None = None


# A prior located exactly at 0 or 1 asserts that the other outcome is
# impossible, which is never something to believe on the strength of one key's
# worth of data — and on a key where a single model has scored perfectly every
# time, the pooled mean *is* 1.0. Pseudo-counts are placed no closer to the
# boundary than this.
PRIOR_BOUNDARY_GUARD = 1.0 / (2.0 * SHRINKAGE_STRENGTH)


def _posterior(
    quality_sum: float, quality_sq: float, weight: float, pooled: float
) -> tuple[float, float]:
    """(shrunk mean, standard deviation) for one model's quality record.

    The mean is a Beta posterior: a quality score lives in [0, 1], so each unit
    of weight contributes `q` to alpha and `1 - q` to beta, with the shrinkage
    strength entering as pseudo-observations placed at the pooled mean. The
    posterior mean is then exactly the shrunk average.

    The standard deviation is the **larger** of two estimates, because they fail
    in opposite directions and overstating confidence is the expensive mistake —
    it routes every run to a model on evidence that did not support it:

    * The **Beta posterior sd** is driven by the mean and the sample count, and
      knows nothing about spread. It is the one that correctly distrusts a small
      record whose every run scored identically — six 1.0s have no observed
      variance at all, and a sample estimator reads that as certainty.
    * The **sample standard error** measures actual spread. It is the one that
      correctly distrusts a model that alternates between excellent and useless,
      which the Beta model smooths into an unremarkable middle.

    Taking the maximum means a record has to look good under both readings before
    its lower bound is allowed to displace anything.
    """
    lo, hi = PRIOR_BOUNDARY_GUARD, 1.0 - PRIOR_BOUNDARY_GUARD
    location = min(max(pooled, lo), hi)
    alpha = quality_sum + SHRINKAGE_STRENGTH * location
    beta = (weight - quality_sum) + SHRINKAGE_STRENGTH * (1.0 - location)
    total = alpha + beta
    if total <= 0 or weight <= 0:
        return NEUTRAL_QUALITY, 0.0
    mean = alpha / total
    beta_sd = math.sqrt(max(0.0, (alpha * beta) / (total * total * (total + 1.0))))

    raw_mean = quality_sum / weight
    sample_variance = max(0.0, quality_sq / weight - raw_mean * raw_mean)
    sample_sd = math.sqrt(sample_variance / weight)

    return mean, max(beta_sd, sample_sd)


def _decay(age_days: float) -> float:
    return 0.5 ** (max(0.0, age_days) / HALF_LIFE_DAYS)


def _row_weight(row: RunOutcome, now: datetime, size_band: str | None) -> float:
    observed = row.observed_at
    if observed is not None and observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    age_days = ((now - observed).total_seconds() / 86400.0) if observed else 0.0
    weight = _decay(age_days)
    if size_band and row.size_band != size_band:
        weight *= OFF_BAND_WEIGHT
    return weight


def summarize(
    rows: list[RunOutcome], *, size_band: str | None = None, now: datetime | None = None
) -> dict[str, ModelPrior]:
    """Aggregate raw outcome rows into per-model priors. Pure — no I/O.

    Separated from the query so the whole weighting scheme can be tested against
    hand-built rows and a fixed clock, which is the only way any of this is
    checkable at all.
    """
    now = now or _utcnow()
    acc: dict[str, _Accumulator] = {}

    for row in rows:
        if row.outcome_class in NON_QUALITY_CLASSES:
            # Recorded, and deliberately not evidence: a model handed off
            # because the conversation outgrew its window was the wrong size,
            # not a poor performer, and the router already reasons about context
            # windows directly. Filtered in the query too — this is the guard for
            # rows reaching `summarize` by another route.
            continue
        weight = _row_weight(row, now, size_band)
        if weight <= 0.0:
            continue
        a = acc.setdefault(row.model_id, _Accumulator())
        quality = float(row.quality_score)
        a.runs += 1
        a.weight += weight
        a.quality += weight * quality
        a.quality_sq += weight * quality * quality
        a.delivered += weight if row.outcome_class == "delivered" else 0.0
        a.failed += weight if row.outcome_class == "failed" else 0.0
        a.cost += weight * float(row.cost_usd or 0)
        a.output_tokens += weight * (row.output_tokens or 0)
        a.iterations += weight * (row.iterations or 0)
        if row.energy_wh is not None:
            a.energy += weight * float(row.energy_wh)
            a.energy_weight += weight
        a.approvals += row.findings_approved or 0
        a.rejections += row.findings_rejected or 0
        if row.error_kind:
            a.error_kinds[row.error_kind] = a.error_kinds.get(row.error_kind, 0) + 1
        observed = row.observed_at
        if observed is not None:
            if observed.tzinfo is None:
                observed = observed.replace(tzinfo=timezone.utc)
            if a.last_seen is None or observed > a.last_seen:
                a.last_seen = observed

    # The pooled mean across every model on this key is what individual models
    # are shrunk toward — not a fixed constant, because "a typical score" is a
    # property of the task, not of the world: 0.4 is a poor record on an easy
    # shape and a good one on a shape nothing handles well. A thin record
    # therefore defaults to "typical for this task" instead of to a number that
    # may be nowhere near what this task affords.
    #
    # Note what this does and does not do. Ordering *within* a key is preserved
    # (every model is pulled toward the same point, thin records further than
    # thick ones), which is the only comparison routing ever makes. It is not a
    # correction for cross-key difficulty, and priors from different keys must
    # not be compared.
    total_weight = sum(a.weight for a in acc.values())
    pooled = (
        sum(a.quality for a in acc.values()) / total_weight if total_weight else NEUTRAL_QUALITY
    )

    out: dict[str, ModelPrior] = {}
    for model_id, a in acc.items():
        if a.weight < MIN_EFFECTIVE_SAMPLES:
            # Not enough evidence to say anything. Omitted rather than reported
            # as a weak signal: a caller cannot accidentally act on a prior that
            # is not there, but it can very easily act on one that is.
            continue
        raw_mean = a.quality / a.weight
        shrunk, stderr = _posterior(a.quality, a.quality_sq, a.weight, pooled)
        out[model_id] = ModelPrior(
            model_id=model_id,
            runs=a.runs,
            effective_n=a.weight,
            quality_mean=shrunk,
            quality_raw=raw_mean,
            quality_ci_low=max(0.0, shrunk - Z_CONSERVATIVE * stderr),
            delivered_rate=a.delivered / a.weight,
            failure_rate=a.failed / a.weight,
            mean_cost_usd=a.cost / a.weight,
            mean_output_tokens=a.output_tokens / a.weight,
            mean_iterations=a.iterations / a.weight,
            mean_energy_wh=(a.energy / a.energy_weight) if a.energy_weight else None,
            approvals=a.approvals,
            rejections=a.rejections,
            error_kinds=dict(sorted(a.error_kinds.items())),
            last_seen=a.last_seen.isoformat() if a.last_seen else None,
        )
    return out


class OutcomePriors:
    """Reads `run_outcomes` and caches the aggregate briefly.

    Opens its own short-lived session rather than borrowing the caller's. The
    engine's session is mid-run and mid-transaction when routing happens, and a
    read-only aggregate has no business inside it — nor should a slow analytics
    query be able to hold a run's write transaction open.
    """

    def __init__(self, session_factory=None, *, ttl: float = CACHE_TTL_SECONDS, now=None):
        self._session_factory = session_factory
        self._ttl = ttl
        self._now = now or _utcnow
        self._cache: dict[tuple, tuple[float, dict[str, ModelPrior]]] = {}

    def invalidate(self) -> None:
        self._cache.clear()

    async def _rows(self, task_shape: str, objective: str) -> list[RunOutcome]:
        factory = self._session_factory
        if factory is None:
            from tret.db.engine import get_session_factory

            factory = get_session_factory()
        since = self._now() - timedelta(days=WINDOW_DAYS)
        async with factory() as db:
            q = (
                select(RunOutcome)
                .where(
                    RunOutcome.task_shape == task_shape,
                    RunOutcome.objective == objective,
                    RunOutcome.observed_at >= since,
                    RunOutcome.outcome_class.notin_(NON_QUALITY_CLASSES),
                )
                .order_by(RunOutcome.observed_at.desc())
                .limit(ROW_SCAN_LIMIT)
            )
            return list((await db.execute(q)).scalars().all())

    async def for_key(
        self, *, task_shape: str, objective: str, size_band: str | None = None
    ) -> dict[str, ModelPrior]:
        key = (task_shape, objective, size_band)
        hit = self._cache.get(key)
        if hit and (time.monotonic() - hit[0]) < self._ttl:
            return hit[1]
        try:
            rows = await self._rows(task_shape, objective)
        except Exception:  # noqa: BLE001
            # Evidence is an improvement to routing, never a dependency of it. A
            # database that cannot answer this query (missing table on a
            # part-migrated install, a timeout) must degrade to the behavior
            # tret had before priors existed, not fail the run.
            import logging

            logging.getLogger("tret.priors").exception("failed to read routing priors")
            return {}
        priors = summarize(rows, size_band=size_band, now=self._now())
        self._cache[key] = (time.monotonic(), priors)
        return priors
