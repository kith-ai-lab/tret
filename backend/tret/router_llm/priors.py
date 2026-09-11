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

The types, weighting, and pure aggregation (`summarize`) live in
`priors_base.py` so that the SDK path (`router.py`, `objectives.py`,
`fallback.py`) can depend on them without pulling in SQLAlchemy. This module
adds the one piece that needs a database: `OutcomePriors`, which reads
`run_outcomes` and hands the rows to `summarize`. Re-exported here so existing
importers of `tret.router_llm.priors` keep working unchanged.
"""
from __future__ import annotations

import time
from datetime import timedelta

from sqlalchemy import select

from tret.db.models import RunOutcome
from tret.router_llm.outcomes import NON_QUALITY_CLASSES
from tret.router_llm.priors_base import (
    ENDPOINT_POOR_MARGIN,
    HALF_LIFE_DAYS,
    MIN_EFFECTIVE_SAMPLES,
    NEUTRAL_QUALITY,
    OFF_BAND_WEIGHT,
    PRIOR_BOUNDARY_GUARD,
    PRIORS_VERSION,
    SHRINKAGE_STRENGTH,
    Z_CONSERVATIVE,
    EndpointPrior,
    ModelPrior,
    NoPriors,
    PriorsProvider,
    _utcnow,
    poor_endpoints,
    summarize,
)

__all__ = [
    "ENDPOINT_POOR_MARGIN",
    "HALF_LIFE_DAYS",
    "MIN_EFFECTIVE_SAMPLES",
    "NEUTRAL_QUALITY",
    "OFF_BAND_WEIGHT",
    "PRIOR_BOUNDARY_GUARD",
    "PRIORS_VERSION",
    "SHRINKAGE_STRENGTH",
    "Z_CONSERVATIVE",
    "EndpointPrior",
    "ModelPrior",
    "NoPriors",
    "OutcomePriors",
    "PriorsProvider",
    "poor_endpoints",
    "summarize",
]

# Evidence older than this is not read at all. Beyond a few months the catalog
# has usually moved under the data anyway.
WINDOW_DAYS = 180
# Bound on the rows read for one key, so this stays cheap on a long-lived
# install. The window and the ordering mean the cap drops the oldest evidence
# first, which is also the evidence that decay has already made nearly weightless.
ROW_SCAN_LIMIT = 2000
# How long an aggregate is reused before it is recomputed.
CACHE_TTL_SECONDS = 60.0


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
