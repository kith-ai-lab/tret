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

import logging
import time
import weakref
from types import SimpleNamespace
from datetime import timedelta, timezone

from sqlalchemy import select

from tret.db.models import BatchTarget, Run, RunOutcome
from tret.router_llm.outcomes import NON_QUALITY_CLASSES
from tret.router_llm.scoring import AgentRow
from tret.router_llm.priors_base import (
    ENDPOINT_POOR_MARGIN,
    HALF_LIFE_DAYS,
    MIN_EFFECTIVE_SAMPLES,
    NEUTRAL_QUALITY,
    OFF_BAND_WEIGHT,
    PRIOR_BOUNDARY_GUARD,
    PRIORS_VERSION,
    FAMILY_MAX_SHIFT,
    FAMILY_MIN_ROWS,
    FAMILY_SHRINKAGE_STRENGTH,
    SHRINKAGE_STRENGTH,
    Z_CONSERVATIVE,
    EndpointPrior,
    ModelPrior,
    NoPriors,
    PriorsProvider,
    _utcnow,
    poor_endpoints,
    family_adjusted,
    summarize,
)

__all__ = [
    "FAMILY_MAX_SHIFT",
    "FAMILY_MIN_ROWS",
    "FAMILY_SHRINKAGE_STRENGTH",
    "family_adjusted",
    "invalidate_all_priors",
    "reset_priors_registry",
    "COOLDOWN_ERROR_KIND",
    "COOLDOWN_PROVIDERS",
    "COOLDOWN_TRIP_COUNT",
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

# ── model-level circuit breaker ─────────────────────────────────────────────
# gpt-5.6-luna was demoted by priors for chat shapes (its own key) but kept
# getting chosen for extraction and verdict runs and failing there too (the
# 2026-09-11 S1) — priors are keyed per (task_shape, objective, size_band), so
# an endpoint that rejects every request from this model never demotes it
# outside the one key it happened to fail on. `OutcomePriors.cooldown_for`
# answers a shape/objective-free question instead: has this model's endpoint
# just started rejecting it, full stop?
#
# How many of a model's most recent runs (within the cooldown window) must
# all show the same failure before the breaker trips. 2 rather than 1: a
# single failed run is not yet a pattern (it could be one bad request), and
# 2 is cheap to reach for a genuinely broken endpoint (which fails on every
# call) while still requiring an actual repeat, not a coincidence.
COOLDOWN_TRIP_COUNT = 2
# What "a provider error" means here, matching `router_llm.outcomes.
# error_kind`'s classification of an error string starting with "[" (a
# `ProviderError`'s own `str()` shape, "[provider] message") — the wire-level
# rejection this breaker exists to catch, not a task the model merely
# answered badly.
COOLDOWN_ERROR_KIND = "provider_error"
# Restricted to `RunOutcome.provider` values matching the two providers this
# was scoped against (the 2026-09-11 incident, and Anthropic's own transient
# failures) — a `provider_error` on Kimi or a local server is a different
# signal (a single self-hosted upstream, not one of several OpenRouter
# endpoints) and stays out of scope for this specific breaker.
COOLDOWN_PROVIDERS = frozenset({"openrouter", "anthropic"})


# Every live `OutcomePriors`, so code outside the engine (an evaluation landing, a
# spot check, an approval) can drop their caches without holding a reference.
_INSTANCES: "weakref.WeakSet[OutcomePriors]" = weakref.WeakSet()


def invalidate_all_priors() -> None:
    """Drop every `OutcomePriors` cache, family gates included, in this process.
    Called by the review / evaluation / approval hooks (the run-end `invalidate()`
    deliberately keeps the gate cache). Only clears in-memory dicts: no I/O, so a
    provider from another event loop or with a closed session factory cannot make it
    raise; each instance is guarded anyway. Never raises."""
    for instance in list(_INSTANCES):
        try:
            instance.invalidate()
            instance.invalidate_gates()
        except Exception:  # noqa: BLE001 - cache hygiene must not fail its caller
            logging.getLogger("tret.priors").exception("failed to invalidate routing priors")


def reset_priors_registry() -> None:
    """Forget every registered provider (test isolation)."""
    _INSTANCES.clear()


def _aware(observed_at):
    """`observed_at`, guaranteed tz-aware — same defensive coercion
    `priors_base._row_weight` applies, needed here too since a naive and an
    aware datetime cannot be compared or added to a `timedelta` together."""
    if observed_at is not None and observed_at.tzinfo is None:
        return observed_at.replace(tzinfo=timezone.utc)
    return observed_at


class OutcomePriors:
    """Reads `run_outcomes` and caches the aggregate briefly.

    Opens its own short-lived session rather than borrowing the caller's. The
    engine's session is mid-run and mid-transaction when routing happens, and a
    read-only aggregate has no business inside it — nor should a slow analytics
    query be able to hold a run's write transaction open.
    """

    def __init__(
        self,
        session_factory=None,
        *,
        ttl: float = CACHE_TTL_SECONDS,
        now=None,
        cooldown_minutes: float | None = None,
    ):
        self._session_factory = session_factory
        self._ttl = ttl
        self._now = now or _utcnow
        self._cache: dict[tuple, tuple[float, dict[str, ModelPrior]]] = {}
        # None (the default) means "read TRET_ROUTER_COOLDOWN_MINUTES fresh on
        # every call" — the same env-driven default every other caller gets.
        # Set explicitly (tests do this) to pin it independent of settings.
        self._cooldown_minutes = cooldown_minutes
        # task_family -> (monotonic stamp, gate dict): its own TTL, NOT cleared by the
        # run-end `invalidate()` (a busy batch would keep it cold); cleared by the
        # review / evaluation hooks through `invalidate_all_priors()`.
        self._gates: dict[str, tuple[float, dict]] = {}
        self._family_cache: dict[tuple, tuple[float, tuple[dict[str, ModelPrior], dict | None]]] = {}
        # harness id -> (monotonic stamp, rows): the per-agent record (`for_agent`).
        self._agent_cache: dict[str, tuple[float, list]] = {}
        _INSTANCES.add(self)

    def invalidate(self) -> None:
        self._cache.clear()
        self._family_cache.clear()
        self._agent_cache.clear()

    def invalidate_gates(self) -> None:
        self._gates.clear()
        self._family_cache.clear()

    async def _gate(self, task_family: str) -> dict:
        """The family's gate (`batches.gate.family_gate`), cached for the TTL. A
        failure reads as closed and is not cached."""
        hit = self._gates.get(task_family)
        if hit and (time.monotonic() - hit[0]) < self._ttl:
            return hit[1]
        from tret.batches.gate import family_gate

        factory = self._session_factory
        if factory is None:
            from tret.db.engine import get_session_factory

            factory = get_session_factory()
        async with factory() as db:
            gate = await family_gate(db, task_family)
        self._gates[task_family] = (time.monotonic(), gate)
        return gate

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

    async def _family_rows(self, task_family: str, objective: str) -> list[RunOutcome]:
        factory = self._session_factory
        if factory is None:
            from tret.db.engine import get_session_factory

            factory = get_session_factory()
        since = self._now() - timedelta(days=WINDOW_DAYS)
        async with factory() as db:
            q = (
                select(RunOutcome)
                .where(
                    RunOutcome.task_family == task_family,
                    RunOutcome.objective == objective,
                    RunOutcome.evaluated_quality_score.is_not(None),
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

    async def _agent_rows(self, harness_id) -> list:
        factory = self._session_factory
        if factory is None:
            from tret.db.engine import get_session_factory

            factory = get_session_factory()
        since = self._now() - timedelta(days=WINDOW_DAYS)
        async with factory() as db:
            q = (
                select(
                    RunOutcome.model_id,
                    RunOutcome.objective,
                    RunOutcome.observed_at,
                    RunOutcome.outcome_class,
                    RunOutcome.cost_usd,
                    RunOutcome.energy_wh,
                    RunOutcome.input_tokens,
                    RunOutcome.output_tokens,
                    RunOutcome.findings_rejected,
                    RunOutcome.task_family,
                    Run.task_input["_collection_id"].as_string().label("batch_stamp"),
                    BatchTarget.id.label("target_id"),
                    BatchTarget.status.label("target_status"),
                    BatchTarget.error.label("target_error"),
                    BatchTarget.outcome.label("target_outcome"),
                )
                .select_from(RunOutcome)
                .outerjoin(Run, Run.id == RunOutcome.run_id)
                .outerjoin(BatchTarget, BatchTarget.run_id == RunOutcome.run_id)
                .where(
                    RunOutcome.harness_id == harness_id,
                    RunOutcome.observed_at >= since,
                    RunOutcome.outcome_class.notin_(NON_QUALITY_CLASSES),
                )
                .order_by(RunOutcome.observed_at.desc())
                .limit(ROW_SCAN_LIMIT)
            )
            return list((await db.execute(q)).all())

    async def for_agent(self, harness_id) -> list[AgentRow] | None:
        """One agent's (harness's) own outcome rows, each with its batch target's
        class, for the scored ranking (`router_llm.scoring`). Cached per harness for
        the TTL. `None` for no harness id or on any failure (logged, not cached):
        like `for_key`, evidence is never a dependency of routing."""
        if harness_id is None:
            return None
        key = str(harness_id)
        hit = self._agent_cache.get(key)
        if hit and (time.monotonic() - hit[0]) < self._ttl:
            return hit[1]
        try:
            from tret.batches.scoring import target_class
            raw = await self._agent_rows(harness_id)
            out = []
            for r in raw:
                matched = r.target_id is not None
                out.append(
                    AgentRow(
                        model_id=r.model_id,
                        objective=r.objective,
                        observed_at=r.observed_at,
                        outcome_class=r.outcome_class,
                        cost_usd=float(r.cost_usd or 0.0),
                        energy_wh=float(r.energy_wh) if r.energy_wh is not None else None,
                        input_tokens=int(r.input_tokens or 0),
                        output_tokens=int(r.output_tokens or 0),
                        findings_rejected=int(r.findings_rejected or 0),
                        # The engine's batch stamp on the run, so a row written before
                        # `task_family` existed is still recognised as a batch row.
                        is_batch=(
                            r.task_family is not None
                            or matched
                            or r.batch_stamp not in (None, "", "null")
                        ),
                        target_class=(
                            target_class(
                                SimpleNamespace(
                                    status=r.target_status,
                                    error=r.target_error,
                                    outcome=r.target_outcome,
                                )
                            )
                            if matched
                            else None
                        ),
                    )
                )
        except Exception:  # noqa: BLE001
            logging.getLogger("tret.priors").exception("failed to read the per-agent record")
            return None
        self._agent_cache[key] = (time.monotonic(), out)
        return out

    async def for_key_family(
        self,
        *,
        task_shape: str,
        objective: str,
        size_band: str | None = None,
        task_family: str,
    ) -> tuple[dict[str, ModelPrior], dict | None]:
        """`for_key`, re-ranked by the family's own evidence when its gate is open.

        Returns `(priors, status)`; `status` is None when the feature is off, else
        `{"used", "gate": {open, spot_checks, agreement}, "adjusted": {model: delta}}`
        where `used` is true only when an adjustment was computed and applied in THIS
        call. Nothing is remembered on the provider except time-limited caches.
        Any failure returns the base priors with `{"used": False, "error": True}`."""
        base = await self.for_key(task_shape=task_shape, objective=objective, size_band=size_band)
        from tret.config import get_settings

        if get_settings().router_family_priors == "off":
            return base, None
        key = (task_shape, objective, size_band, task_family)
        hit = self._family_cache.get(key)
        if hit and (time.monotonic() - hit[0]) < self._ttl:
            return hit[1]
        summary = {"open": False, "spot_checks": 0, "agreement": None}
        try:
            gate = await self._gate(task_family)
            summary = {
                "open": bool(gate.get("open")),
                "spot_checks": gate.get("spot_checks", 0),
                "agreement": gate.get("agreement"),
            }
            if not summary["open"]:
                result = (base, {"used": False, "gate": summary, "adjusted": {}})
            else:
                rows = [
                    r
                    for r in await self._family_rows(task_family, objective)
                    # only what the gate's own judge and rubric produced
                    if (r.components or {}).get("evaluator", {}).get("rubric_version")
                    == gate.get("rubric_version")
                    and (r.components or {}).get("evaluator", {}).get("judge_model")
                    == gate.get("judge_model")
                ]
                priors, info = family_adjusted(base, rows, now=self._now())
                adjusted = info["adjusted"]
                result = (priors, {"used": bool(adjusted), "gate": summary, "adjusted": adjusted})
        except Exception:  # noqa: BLE001
            logging.getLogger("tret.priors").exception("failed to read family routing priors")
            return base, {"used": False, "error": True, "gate": summary, "adjusted": {}}
        self._family_cache[key] = (time.monotonic(), result)
        return result

    async def _cooldown_rows(self, candidate_ids: list[str], since) -> list[RunOutcome]:
        """Every outcome for `candidate_ids` since `since`, newest first.

        Deliberately *not* filtered to failures alone: whether a model's last
        two runs both failed depends on knowing what its last two runs *were*
        — a model with [failed, delivered, failed] in the window has a
        delivered run between the two failures and must not trip the
        breaker, which a query pre-filtered to `outcome_class == 'failed'`
        could not tell apart from two failures back to back.
        """
        factory = self._session_factory
        if factory is None:
            from tret.db.engine import get_session_factory

            factory = get_session_factory()
        async with factory() as db:
            q = (
                select(RunOutcome)
                .where(RunOutcome.model_id.in_(candidate_ids), RunOutcome.observed_at >= since)
                .order_by(RunOutcome.observed_at.desc())
                .limit(ROW_SCAN_LIMIT)
            )
            return list((await db.execute(q)).scalars().all())

    async def cooldown_for(self, candidate_ids: list[str]) -> list[dict]:
        """Which of `candidate_ids` should sit out routing right now — the
        model-level circuit breaker (see the module-level comment above
        `COOLDOWN_TRIP_COUNT`). One query, cheap by construction: bounded to
        `candidate_ids` and to a window of minutes, not days.

        Returns `[{"model", "until", "reason"}, ...]`, sorted by model id —
        what `router_llm.router._apply_cooldown` excludes from candidates and
        what `RoutingDecision.evidence["cooldown"]` records. `[]` whenever the
        breaker is disabled (`TRET_ROUTER_COOLDOWN_MINUTES` <= 0), there are
        no candidates to check, or nothing qualifies. Never raises: like
        `for_key`, a failing query degrades to "no evidence" rather than
        failing the run it was about to help route.
        """
        minutes = self._cooldown_minutes
        if minutes is None:
            from tret.config import get_settings

            minutes = get_settings().router_cooldown_minutes
        if minutes <= 0 or not candidate_ids:
            return []
        since = self._now() - timedelta(minutes=minutes)
        try:
            rows = await self._cooldown_rows(list(candidate_ids), since)
        except Exception:  # noqa: BLE001 - a safety net must never become a dependency
            import logging

            logging.getLogger("tret.priors").exception("failed to read the router cooldown")
            return []

        # `rows` is newest-first (the query's own ORDER BY); keep each
        # model's most recent COOLDOWN_TRIP_COUNT rows only — later rows for
        # a model that already has enough add nothing.
        recent: dict[str, list[RunOutcome]] = {}
        for row in rows:
            bucket = recent.setdefault(row.model_id, [])
            if len(bucket) < COOLDOWN_TRIP_COUNT:
                bucket.append(row)

        out: list[dict] = []
        for model_id, last in recent.items():
            if len(last) < COOLDOWN_TRIP_COUNT:
                continue
            if not all(
                row.outcome_class == "failed"
                and row.iterations == 0
                and row.error_kind == COOLDOWN_ERROR_KIND
                and row.provider in COOLDOWN_PROVIDERS
                for row in last
            ):
                continue
            # The cooldown lifts on its own once the older of the two rows
            # ages out of the window (assuming no new failure resets it) —
            # `until` names that moment rather than "minutes from now", so it
            # stays accurate however long it takes this method to be called
            # again.
            oldest = min(_aware(row.observed_at) for row in last)
            out.append(
                {
                    "model": model_id,
                    "until": (oldest + timedelta(minutes=minutes)).isoformat(),
                    "reason": (
                        f"last {COOLDOWN_TRIP_COUNT} runs failed at iteration 0 with a "
                        f"provider error within the last {minutes:.0f} minutes"
                    ),
                }
            )
        return sorted(out, key=lambda e: e["model"])
