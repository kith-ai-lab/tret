"""Routing priors: shrinkage, decay, size bands, and the cold-start guarantee.

`summarize` is pure — rows in, priors out, with an injected clock — so every
weighting rule here is checkable without a database. That matters more than
usual: these numbers decide which model gets chosen, and a weighting scheme
nobody can test is a weighting scheme nobody can argue with.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from tret.db.models import RunOutcome
from tret.router_llm.priors import (
    COOLDOWN_TRIP_COUNT,
    HALF_LIFE_DAYS,
    MIN_EFFECTIVE_SAMPLES,
    OFF_BAND_WEIGHT,
    NoPriors,
    OutcomePriors,
    summarize,
)

NOW = datetime(2026, 8, 20, tzinfo=timezone.utc)


def _row(
    model_id: str,
    quality: float,
    *,
    age_days: float = 0.0,
    outcome_class: str = "delivered",
    size_band: str = "m",
    cost: str = "0.01",
    error_kind: str | None = None,
    approved: int = 0,
    rejected: int = 0,
) -> RunOutcome:
    return RunOutcome(
        task_shape="verdict",
        objective="balanced",
        max_cost_tier="premium",
        task_type="assess",
        size_band=size_band,
        model_id=model_id,
        provider="anthropic",
        outcome_class=outcome_class,
        quality_score=Decimal(str(quality)),
        score_version="outcome-v1",
        error_kind=error_kind,
        components={},
        iterations=4,
        cost_usd=Decimal(cost),
        input_tokens=1000,
        output_tokens=500,
        energy_wh=Decimal("0.5"),
        duration_ms=1000,
        findings_created=approved + rejected,
        findings_approved=approved,
        findings_rejected=rejected,
        observed_at=NOW - timedelta(days=age_days),
    )


def _many(model_id: str, quality: float, n: int, **kw) -> list[RunOutcome]:
    return [_row(model_id, quality, **kw) for _ in range(n)]


# ── the cold-start guarantee ─────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_no_priors_provider_never_returns_evidence():
    # The provider the evals and both benchmark arms inject, so routing there is
    # reproducible no matter what the database has accumulated.
    assert await NoPriors().for_key(task_shape="verdict", objective="balanced") == {}


def test_no_rows_means_no_priors_rather_than_neutral_ones():
    assert summarize([], now=NOW) == {}


# ── unknown is not bad ───────────────────────────────────────────────────────
def test_a_model_below_the_sample_floor_has_no_prior_at_all():
    # Omitted, not reported as weak. A caller cannot act on a prior that isn't
    # there, but it can very easily act on one that is — and ranking an untried
    # model last for being untried means nothing new is ever tried again.
    assert summarize(_many("m/thin", 0.1, 2), now=NOW) == {}


def test_the_floor_is_measured_in_effective_samples_not_row_count():
    # Ten runs from two years ago are not ten samples.
    fresh = summarize(_many("m/x", 0.8, 10), now=NOW)
    stale = summarize(_many("m/x", 0.8, 10, age_days=365), now=NOW)
    assert "m/x" in fresh
    assert stale == {}
    assert fresh["m/x"].effective_n >= MIN_EFFECTIVE_SAMPLES


# ── time decay ───────────────────────────────────────────────────────────────
def test_evidence_halves_in_weight_over_the_half_life():
    fresh = summarize(_many("m/x", 0.8, 20), now=NOW)["m/x"]
    aged = summarize(_many("m/x", 0.8, 20, age_days=HALF_LIFE_DAYS), now=NOW)["m/x"]
    assert aged.effective_n == pytest.approx(fresh.effective_n / 2, rel=1e-6)
    assert aged.runs == fresh.runs  # the count is not what decayed


def test_recent_evidence_outweighs_old_evidence_of_the_opposite():
    # A model that was poor and has since improved must be able to recover.
    rows = _many("m/x", 0.2, 10, age_days=120) + _many("m/x", 0.9, 10)
    assert summarize(rows, now=NOW)["m/x"].quality_raw > 0.7


# ── size bands ───────────────────────────────────────────────────────────────
def test_off_band_evidence_counts_for_less_but_still_counts():
    on = summarize(_many("m/x", 0.8, 10, size_band="m"), size_band="m", now=NOW)["m/x"]
    off = summarize(_many("m/x", 0.8, 10, size_band="xl"), size_band="m", now=NOW)
    # Ten off-band rows at a quarter weight fall under the floor, which is the
    # point: they inform, they do not carry a key on their own.
    assert off == {}
    mixed = summarize(
        _many("m/x", 0.8, 10, size_band="m") + _many("m/x", 0.8, 10, size_band="xl"),
        size_band="m",
        now=NOW,
    )["m/x"]
    assert mixed.effective_n == pytest.approx(on.effective_n * (1 + OFF_BAND_WEIGHT), rel=1e-6)


def test_asking_for_no_band_weighs_every_band_equally():
    rows = _many("m/x", 0.8, 5, size_band="xs") + _many("m/x", 0.8, 5, size_band="xl")
    assert summarize(rows, size_band=None, now=NOW)["m/x"].effective_n == pytest.approx(10.0)


# ── shrinkage ────────────────────────────────────────────────────────────────
def test_a_thin_record_is_pulled_toward_its_peers_and_a_thick_one_is_not():
    # Both models score 0.9 raw; one has 6 runs, the other 200. The thin record
    # should read as "probably good"; the thick one as "good".
    rows = _many("m/thin", 0.9, 6) + _many("m/thick", 0.9, 200) + _many("m/peer", 0.3, 200)
    priors = summarize(rows, now=NOW)
    assert priors["m/thin"].quality_raw == pytest.approx(priors["m/thick"].quality_raw)
    assert priors["m/thin"].quality_mean < priors["m/thick"].quality_mean


def test_shrinkage_pulls_toward_peers_on_the_same_key_not_a_fixed_constant():
    # A thin record defaults to "typical for this task", because a typical score
    # is a property of the task: identical evidence lands lower on a key where
    # everything does badly than on one where everything does well.
    hard = summarize(_many("m/x", 0.3, 8) + _many("m/y", 0.25, 200), now=NOW)
    easy = summarize(_many("m/x", 0.3, 8) + _many("m/y", 0.95, 200), now=NOW)
    assert hard["m/x"].quality_mean < easy["m/x"].quality_mean


def test_shrinkage_preserves_the_ordering_routing_actually_reads():
    # Every model on a key is pulled toward the same point, so the comparison
    # routing makes — which of these candidates is better *here* — survives it.
    priors = summarize(
        _many("m/good", 0.9, 30) + _many("m/mid", 0.6, 30) + _many("m/bad", 0.2, 30), now=NOW
    )
    ranked = sorted(priors.values(), key=lambda p: -p.quality_mean)
    assert [p.model_id for p in ranked] == ["m/good", "m/mid", "m/bad"]


def test_an_unvarying_thin_record_is_still_an_uncertain_one():
    # A model whose every run scored identically has no observed spread, and a
    # sample standard error reads that as certainty — so six runs came back as
    # unarguable. Uncertainty at small n is the entire job of this number.
    thin = summarize(_many("m/thin", 1.0, 6), now=NOW)["m/thin"]
    thick = summarize(_many("m/thick", 1.0, 300), now=NOW)["m/thick"]
    assert thin.quality_ci_low < thin.quality_mean
    assert thin.quality_ci_low < thick.quality_ci_low


def test_the_conservative_bound_never_exceeds_the_mean():
    rows = _many("m/x", 0.9, 10) + _many("m/x", 0.2, 10)
    prior = summarize(rows, now=NOW)["m/x"]
    assert prior.quality_ci_low <= prior.quality_mean
    assert prior.quality_ci_low >= 0.0


def test_a_noisy_record_has_a_lower_floor_than_a_consistent_one_of_equal_mean():
    # This is what stops a model with wild variance from displacing a steady one
    # on the strength of its average alone.
    steady = summarize(_many("m/s", 0.6, 30), now=NOW)["m/s"]
    noisy = summarize(_many("m/n", 1.0, 15) + _many("m/n", 0.2, 15), now=NOW)["m/n"]
    assert noisy.quality_raw == pytest.approx(steady.quality_raw, abs=0.01)
    assert noisy.quality_ci_low < steady.quality_ci_low


# ── the reported record ──────────────────────────────────────────────────────
def test_rates_and_averages_describe_the_weighted_evidence():
    rows = _many("m/x", 0.7, 15) + _many(
        "m/x", 0.0, 5, outcome_class="failed", error_kind="provider_error"
    )
    prior = summarize(rows, now=NOW)["m/x"]
    assert prior.delivered_rate == pytest.approx(0.75)
    assert prior.failure_rate == pytest.approx(0.25)
    assert prior.error_kinds == {"provider_error": 5}
    assert prior.mean_cost_usd == pytest.approx(0.01)
    assert prior.last_seen == NOW.isoformat()


def test_human_decisions_are_carried_through_as_counts():
    prior = summarize(_many("m/x", 0.8, 10, approved=2, rejected=1), now=NOW)["m/x"]
    assert (prior.approvals, prior.rejections) == (20, 10)


def test_a_prior_serializes_to_plain_json_types():
    prior = summarize(_many("m/x", 0.8, 10), now=NOW)["m/x"]
    assert set(prior.to_json()) >= {"model_id", "runs", "quality_mean", "quality_ci_low"}
    assert isinstance(prior.to_json()["mean_cost_usd"], float)


# ── failure is not a dependency ──────────────────────────────────────────────
@pytest.mark.asyncio
async def test_an_unreadable_outcomes_table_degrades_to_no_evidence():
    # Evidence improves routing; it must never be something routing needs. A
    # part-migrated install or a timed-out query routes exactly as tret did
    # before priors existed.
    class _Exploding(OutcomePriors):
        async def _rows(self, task_shape, objective):
            raise RuntimeError("relation \"run_outcomes\" does not exist")

    priors = _Exploding(session_factory=object(), now=lambda: NOW)
    assert await priors.for_key(task_shape="verdict", objective="balanced") == {}


@pytest.mark.asyncio
async def test_an_aggregate_is_reused_within_its_ttl_and_recomputed_after_invalidation():
    calls = []

    class _Counting(OutcomePriors):
        async def _rows(self, task_shape, objective):
            calls.append((task_shape, objective))
            return _many("m/x", 0.8, 10)

    priors = _Counting(session_factory=object(), now=lambda: NOW)
    first = await priors.for_key(task_shape="verdict", objective="balanced")
    await priors.for_key(task_shape="verdict", objective="balanced")
    assert len(calls) == 1
    assert first["m/x"].runs == 10
    priors.invalidate()
    await priors.for_key(task_shape="verdict", objective="balanced")
    assert len(calls) == 2


# ── handoffs ─────────────────────────────────────────────────────────────────
def test_a_stall_handoff_drags_a_models_record_down():
    # Which is the point: it is the most direct evidence tret has that a model
    # was not up to a piece of work.
    clean = summarize(_many("m/x", 0.7, 20), now=NOW)["m/x"]
    with_handoffs = summarize(
        _many("m/x", 0.7, 20) + _many("m/x", 0.05, 10, outcome_class="handed_off"), now=NOW
    )["m/x"]
    assert with_handoffs.quality_mean < clean.quality_mean


def test_a_capacity_handoff_is_not_counted_at_all():
    # Filtered in the query and again here, because a model handed off for
    # running out of window was the wrong size, not a poor performer.
    clean = summarize(_many("m/x", 0.7, 20), now=NOW)["m/x"]
    with_capacity = summarize(
        _many("m/x", 0.7, 20) + _many("m/x", 0.0, 30, outcome_class="handed_off_capacity"),
        now=NOW,
    )["m/x"]
    assert with_capacity.quality_mean == clean.quality_mean
    assert with_capacity.runs == clean.runs


def test_a_model_seen_only_in_capacity_handoffs_has_no_prior():
    assert summarize(_many("m/small", 0.0, 40, outcome_class="handed_off_capacity"), now=NOW) == {}


# ── model-level circuit breaker (2026-09-11) ─────────────────────────────────
# gpt-5.6-luna was demoted by priors for chat shapes but kept getting chosen
# for extraction and verdict runs and failing there too — priors are keyed
# per (task_shape, objective, size_band), so a model whose *endpoint* rejects
# every request never demotes itself outside the one key it happened to fail
# on. `OutcomePriors.cooldown_for` answers a shape/objective-free question
# instead, from the model's most recent runs alone.
def _cooldown_row(
    model_id: str,
    *,
    minutes_ago: float = 5.0,
    outcome_class: str = "failed",
    iterations: int = 0,
    error_kind: str | None = "provider_error",
    provider: str = "openrouter",
) -> RunOutcome:
    return RunOutcome(
        task_shape="verdict",
        objective="balanced",
        max_cost_tier="premium",
        task_type="assess",
        size_band="m",
        model_id=model_id,
        provider=provider,
        outcome_class=outcome_class,
        quality_score=Decimal("0"),
        score_version="outcome-v1",
        error_kind=error_kind,
        components={},
        iterations=iterations,
        cost_usd=Decimal("0"),
        input_tokens=0,
        output_tokens=0,
        energy_wh=None,
        duration_ms=0,
        findings_created=0,
        findings_approved=0,
        findings_rejected=0,
        observed_at=NOW - timedelta(minutes=minutes_ago),
    )


class _ScriptedCooldownPriors(OutcomePriors):
    """Overrides `_cooldown_rows` to return hand-built rows instead of
    querying a real database — same pattern `_Exploding`/`_Counting` above
    use for `_rows`. Also records every `since` cutoff it was asked to
    query with, so a test can confirm the query is skipped entirely when it
    should be."""

    def __init__(self, rows: list[RunOutcome], **kw):
        super().__init__(session_factory=object(), now=lambda: NOW, **kw)
        self._scripted_rows = rows
        self.queried_since: list = []

    async def _cooldown_rows(self, candidate_ids, since):
        self.queried_since.append(since)
        return [r for r in self._scripted_rows if r.model_id in candidate_ids]


assert COOLDOWN_TRIP_COUNT == 2  # every test below assumes exactly two


@pytest.mark.asyncio
async def test_two_provider_errors_at_iteration_0_trip_the_breaker():
    rows = [_cooldown_row("m/bad", minutes_ago=1), _cooldown_row("m/bad", minutes_ago=2)]
    priors = _ScriptedCooldownPriors(rows, cooldown_minutes=30)
    cooldown = await priors.cooldown_for(["m/bad"])
    assert len(cooldown) == 1
    entry = cooldown[0]
    assert entry["model"] == "m/bad"
    assert "provider error" in entry["reason"]
    assert "iteration 0" in entry["reason"]
    # `until` is the *older* of the two failures plus the cooldown window —
    # when this cooldown actually lifts on its own, evidence permitting, not
    # simply "now plus 30 minutes".
    assert entry["until"] == (NOW - timedelta(minutes=2) + timedelta(minutes=30)).isoformat()


@pytest.mark.asyncio
async def test_a_single_failure_does_not_trip_the_breaker():
    # One bad request is not yet a pattern.
    rows = [_cooldown_row("m/bad", minutes_ago=1)]
    priors = _ScriptedCooldownPriors(rows, cooldown_minutes=30)
    assert await priors.cooldown_for(["m/bad"]) == []


@pytest.mark.asyncio
async def test_a_delivered_run_between_two_failures_does_not_trip_it():
    """The model's most recent two runs are what matters — [failed, delivered,
    failed] has a success between the failures and must not trip the breaker,
    which a query pre-filtered to `outcome_class == 'failed'` alone could not
    tell apart from two failures back to back."""
    rows = [
        _cooldown_row("m/bad", minutes_ago=1),
        _cooldown_row("m/bad", minutes_ago=2, outcome_class="delivered", error_kind=None),
        _cooldown_row("m/bad", minutes_ago=3),
    ]
    priors = _ScriptedCooldownPriors(rows, cooldown_minutes=30)
    assert await priors.cooldown_for(["m/bad"]) == []


@pytest.mark.asyncio
async def test_a_non_zero_iteration_failure_does_not_count():
    # A failure well into a run is not the fast, wire-level rejection this
    # breaker exists to catch.
    rows = [
        _cooldown_row("m/bad", minutes_ago=1, iterations=3),
        _cooldown_row("m/bad", minutes_ago=2, iterations=0),
    ]
    priors = _ScriptedCooldownPriors(rows, cooldown_minutes=30)
    assert await priors.cooldown_for(["m/bad"]) == []


@pytest.mark.asyncio
async def test_a_non_provider_error_does_not_count():
    rows = [
        _cooldown_row("m/bad", minutes_ago=1, error_kind="output_budget_exceeded"),
        _cooldown_row("m/bad", minutes_ago=2, error_kind="output_budget_exceeded"),
    ]
    priors = _ScriptedCooldownPriors(rows, cooldown_minutes=30)
    assert await priors.cooldown_for(["m/bad"]) == []


@pytest.mark.asyncio
async def test_a_provider_outside_scope_does_not_count():
    # Scoped to openrouter/anthropic (the 2026-09-11 incident and Anthropic's
    # own transient failures) — a single self-hosted upstream like Kimi is a
    # different signal.
    rows = [
        _cooldown_row("m/bad", minutes_ago=1, provider="kimi"),
        _cooldown_row("m/bad", minutes_ago=2, provider="kimi"),
    ]
    priors = _ScriptedCooldownPriors(rows, cooldown_minutes=30)
    assert await priors.cooldown_for(["m/bad"]) == []


@pytest.mark.asyncio
async def test_only_qualifying_models_are_flagged_sorted_by_model_id():
    rows = [
        _cooldown_row("m/z", minutes_ago=1),
        _cooldown_row("m/z", minutes_ago=2),
        _cooldown_row("m/a", minutes_ago=1),
        _cooldown_row("m/a", minutes_ago=2),
        _cooldown_row("m/fine", minutes_ago=1, outcome_class="delivered", error_kind=None),
    ]
    priors = _ScriptedCooldownPriors(rows, cooldown_minutes=30)
    cooldown = await priors.cooldown_for(["m/z", "m/a", "m/fine"])
    assert [c["model"] for c in cooldown] == ["m/a", "m/z"]


@pytest.mark.asyncio
async def test_cooldown_disabled_returns_nothing_without_querying():
    priors = _ScriptedCooldownPriors(
        [_cooldown_row("m/bad", minutes_ago=1), _cooldown_row("m/bad", minutes_ago=2)],
        cooldown_minutes=0,
    )
    assert await priors.cooldown_for(["m/bad"]) == []
    assert priors.queried_since == []  # the query is skipped entirely, not just filtered


@pytest.mark.asyncio
async def test_no_candidates_returns_nothing_without_querying():
    priors = _ScriptedCooldownPriors([], cooldown_minutes=30)
    assert await priors.cooldown_for([]) == []
    assert priors.queried_since == []


@pytest.mark.asyncio
async def test_an_unreadable_cooldown_query_degrades_to_no_evidence():
    # Same contract as `for_key`: a safety net must never become a dependency.
    class _Exploding(OutcomePriors):
        async def _cooldown_rows(self, candidate_ids, since):
            raise RuntimeError('relation "run_outcomes" does not exist')

    priors = _Exploding(session_factory=object(), now=lambda: NOW, cooldown_minutes=30)
    assert await priors.cooldown_for(["m/bad"]) == []


@pytest.mark.asyncio
async def test_cooldown_minutes_reads_settings_when_not_pinned(monkeypatch):
    import tret.config as config_module

    calls = []

    class _Settings:
        router_cooldown_minutes = 0  # disabled via "settings"

    monkeypatch.setattr(config_module, "get_settings", lambda: _Settings())

    class _Counting(OutcomePriors):
        async def _cooldown_rows(self, candidate_ids, since):
            calls.append(since)
            return []

    # No `cooldown_minutes` passed to the constructor: reads the settings
    # value fresh on every call, same as `TRET_ROUTER_COOLDOWN_MINUTES` does
    # for every other caller.
    priors = _Counting(session_factory=object(), now=lambda: NOW)
    assert await priors.cooldown_for(["m/bad"]) == []
    assert calls == []  # the settings value (0) disabled it before any query
