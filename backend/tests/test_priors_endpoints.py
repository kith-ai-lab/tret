"""Endpoint-aware routing priors: telling a bad quantized endpoint apart from a
bad model.

`RunOutcome.served_by` (see db/models.py) records which upstream endpoint —
an OpenRouter provider slug, read from `openrouter_metadata` — actually served
a segment. This module tests the half of `priors_base.summarize` that turns
that column into `ModelPrior.endpoints`, and the `poor_endpoints` helper that
reads it, both pure and offline like the rest of `priors_base`.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from tret.db.models import RunOutcome
from tret.router_llm.objectives import EVIDENCE_POOR_MEAN
from tret.router_llm.priors import (
    ENDPOINT_POOR_MARGIN,
    EndpointPrior,
    poor_endpoints,
    summarize,
)

NOW = datetime(2026, 9, 10, tzinfo=timezone.utc)


def _row(
    model_id: str,
    quality: float,
    *,
    served_by: str | None = None,
    outcome_class: str = "delivered",
) -> RunOutcome:
    return RunOutcome(
        task_shape="verdict",
        objective="balanced",
        max_cost_tier="premium",
        task_type="assess",
        size_band="m",
        model_id=model_id,
        provider="openrouter",
        served_by=served_by,
        outcome_class=outcome_class,
        quality_score=Decimal(str(quality)),
        score_version="outcome-v1",
        components={},
        iterations=4,
        cost_usd=Decimal("0.01"),
        input_tokens=1000,
        output_tokens=500,
        energy_wh=Decimal("0.5"),
        duration_ms=1000,
        findings_created=0,
        findings_approved=0,
        findings_rejected=0,
        observed_at=NOW,
    )


def _many(model_id: str, quality: float, n: int, **kw) -> list[RunOutcome]:
    return [_row(model_id, quality, **kw) for _ in range(n)]


# ── two endpoints, divergent outcomes ─────────────────────────────────────────
def test_two_endpoints_with_divergent_outcomes_are_ordered_by_their_own_record():
    rows = _many("m/x", 0.95, 10, served_by="clean-endpoint") + _many(
        "m/x", 0.1, 10, served_by="quantized-endpoint"
    )
    prior = summarize(rows, now=NOW)["m/x"]

    assert set(prior.endpoints) == {"clean-endpoint", "quantized-endpoint"}
    clean = prior.endpoints["clean-endpoint"]
    bad = prior.endpoints["quantized-endpoint"]
    assert isinstance(clean, EndpointPrior)
    assert clean.quality_mean > bad.quality_mean
    assert clean.quality_ci_low > bad.quality_ci_low
    assert clean.delivered_rate == 1.0
    assert clean.runs == 10 and bad.runs == 10


def test_the_top_level_prior_is_unaffected_by_the_endpoint_breakdown():
    """Adding `served_by` to rows must not change the model-level numbers at
    all — the top-level prior is exactly what `summarize` computed before
    endpoints existed, so existing callers (route-v5 prompt rendering, the
    router's own ordering) see no change.
    """
    rows = _many("m/x", 0.95, 10, served_by="clean-endpoint") + _many(
        "m/x", 0.1, 10, served_by="quantized-endpoint"
    )
    with_endpoints = summarize(rows, now=NOW)["m/x"]

    unlabelled = [_row("m/x", r.quality_score.__float__()) for r in rows]
    without_endpoints = summarize(unlabelled, now=NOW)["m/x"]

    assert with_endpoints.quality_mean == without_endpoints.quality_mean
    assert with_endpoints.quality_ci_low == without_endpoints.quality_ci_low
    assert with_endpoints.quality_raw == without_endpoints.quality_raw
    assert with_endpoints.delivered_rate == without_endpoints.delivered_rate
    assert with_endpoints.effective_n == without_endpoints.effective_n
    assert with_endpoints.runs == without_endpoints.runs
    assert without_endpoints.endpoints == {}


def test_endpoints_are_shrunk_toward_the_models_own_mean_not_the_pooled_mean():
    """A thin endpoint record is pulled toward this model's own average, not
    toward the shape-wide pooled mean another, much worse, model would drag
    down — the whole reason `EndpointPrior` shrinks toward `raw_mean` rather
    than reusing `pooled`.
    """
    rows = (
        _many("m/good", 0.9, 20, served_by="a")
        # Exactly MIN_EFFECTIVE_SAMPLES — thin enough that shrinkage dominates,
        # but not so thin `summarize`'s own effective_n floor drops it (see
        # test_a_stale_single_row_endpoint_is_filtered_before_the_two_
        # endpoint_rule in the poor_endpoints section below).
        + _many("m/good", 0.9, 3, served_by="b")
        + _many("m/bad", 0.1, 20)
    )
    prior = summarize(rows, now=NOW)["m/good"]
    # Endpoint "b"'s thin record shrinks toward ~0.9 (m/good's own mean), not
    # toward the pooled mean across m/good and m/bad, which sits far lower.
    assert prior.endpoints["b"].quality_mean > 0.6


# ── single-endpoint models ────────────────────────────────────────────────────
def test_a_single_named_endpoint_has_empty_endpoints():
    prior = summarize(_many("m/x", 0.8, 10, served_by="only-endpoint"), now=NOW)["m/x"]
    assert prior.endpoints == {}


def test_no_served_by_at_all_has_empty_endpoints():
    prior = summarize(_many("m/x", 0.8, 10), now=NOW)["m/x"]
    assert prior.endpoints == {}


def test_rows_with_no_served_by_do_not_count_as_a_second_endpoint():
    rows = _many("m/x", 0.8, 10, served_by="only-endpoint") + _many("m/x", 0.8, 10)
    prior = summarize(rows, now=NOW)["m/x"]
    assert prior.endpoints == {}


# ── poor_endpoints ────────────────────────────────────────────────────────────
# Driven entirely off real `summarize()` output rather than hand-built
# `EndpointPrior`/`ModelPrior` objects: a hand-built prior can assert a
# `quality_ci_low` gap between model and endpoint that the actual shrinkage
# math can never produce, which is exactly how the old absolute-floor bug
# ("a model with mean ~0.45 names every endpoint poor") went unnoticed —
# every hand-built fixture here happened to already have the model's own
# mean comfortably above the floor.


def test_poor_endpoints_is_empty_when_both_endpoints_match_the_models_own_record():
    """The bug this whole rule exists to fix: a model whose own record is
    mediocre (mean ~0.45, well below `EVIDENCE_GOOD_FLOOR` but above
    `EVIDENCE_POOR_MEAN`) shrinks *every* endpoint's mean toward that same
    ~0.45 (`EndpointPrior`'s docstring). Judged against the absolute floor
    alone, both endpoints' `quality_ci_low` sit under `EVIDENCE_POOR_MEAN`
    purely because the *model* is mediocre — the old bug named every endpoint
    on it poor. Judged relative to the model's own `quality_ci_low`, neither
    endpoint trails the other (they are identical), so neither clears
    `ENDPOINT_POOR_MARGIN` and the result is empty.
    """
    rows = _many("m/x", 0.45, 10, served_by="a") + _many("m/x", 0.45, 10, served_by="b")
    prior = summarize(rows, now=NOW)["m/x"]

    # The bug this test pins: both endpoints genuinely sit under the absolute
    # floor on their own, which is exactly why the relative margin is needed.
    assert all(ep.quality_ci_low <= EVIDENCE_POOR_MEAN for ep in prior.endpoints.values())
    assert poor_endpoints(prior) == []


def test_poor_endpoints_flags_only_the_endpoint_that_trails_by_the_margin():
    rows = _many("m/x", 0.85, 10, served_by="good") + _many("m/x", 0.15, 10, served_by="bad")
    prior = summarize(rows, now=NOW)["m/x"]

    assert poor_endpoints(prior) == ["bad"]


def test_a_single_bad_run_among_many_good_does_not_tip_the_endpoint_below_the_floor():
    """One outlier in a deep record moves the mean, but shrinkage and the
    tighter confidence interval that comes with more evidence keep the lower
    bound well clear of `EVIDENCE_POOR_MEAN` — the floor exists precisely so
    a single bad run is not mistaken for a bad endpoint.
    """
    rows = (
        _many("m/x", 0.9, 20, served_by="mostly-good")
        + _many("m/x", 0.0, 1, served_by="mostly-good")
        + _many("m/x", 0.9, 21, served_by="other")
    )
    prior = summarize(rows, now=NOW)["m/x"]

    assert prior.endpoints["mostly-good"].quality_ci_low > EVIDENCE_POOR_MEAN
    assert poor_endpoints(prior) == []


def test_a_stale_single_row_endpoint_is_filtered_before_the_two_endpoint_rule():
    """`summarize` now gates endpoints on `MIN_EFFECTIVE_SAMPLES`, the same
    floor models get, before deciding whether at least two distinct endpoints
    exist. A single row 150 days old has decayed to a small fraction of one
    effective sample — it must not count as one of the "at least two"
    endpoints needed to show a breakdown at all, and it must not appear in
    the breakdown once there are enough real endpoints to show one.
    """
    rows = (
        _many("m/x", 0.9, 20, served_by="fresh-a")
        + _many("m/x", 0.9, 20, served_by="fresh-b")
        + [_row("m/x", 0.9, served_by="stale")]
    )
    rows[-1].observed_at = NOW - timedelta(days=150)
    prior = summarize(rows, now=NOW)["m/x"]

    assert "stale" not in prior.endpoints
    assert set(prior.endpoints) == {"fresh-a", "fresh-b"}


def test_poor_endpoints_never_returns_every_one_of_a_models_endpoints():
    """However bad a model's endpoints look, `poor_endpoints` never names all
    of them: if every endpoint would qualify, that is the model's record, not
    any one endpoint's, and the model-level demotion already covers it.

    25 identical, badly-performing endpoints is enough for each one's
    `quality_ci_low` to clear both the absolute floor and the margin against
    the model's own (much better-evidenced, so tighter) `quality_ci_low` —
    without the guard, all 25 would come back.
    """
    rows = []
    for i in range(25):
        rows += _many("m/x", 0.38, 3, served_by=f"endpoint-{i}")
    prior = summarize(rows, now=NOW)["m/x"]

    # Pins the premise: every endpoint individually clears both bars.
    assert all(
        ep.quality_ci_low <= EVIDENCE_POOR_MEAN
        and ep.quality_ci_low <= prior.quality_ci_low - ENDPOINT_POOR_MARGIN
        for ep in prior.endpoints.values()
    )
    assert poor_endpoints(prior) == []


def test_poor_endpoints_is_empty_with_no_endpoint_breakdown_at_all():
    prior = summarize(_many("m/x", 0.8, 10), now=NOW)["m/x"]
    assert prior.endpoints == {}
    assert poor_endpoints(prior) == []


def test_the_margin_and_floor_constants_are_pinned():
    """`poor_endpoints` reads `EVIDENCE_POOR_MEAN` from `objectives` with a
    function-local import (a module-level one would cycle, since
    `objectives.py` imports `ModelPrior` from `priors_base`) rather than
    duplicating it — the old `_ENDPOINT_POOR_FLOOR` constant this replaced was
    exactly that duplication, and it is deleted. Pinned here so a change to
    either constant is a deliberate, reviewed edit.
    """
    assert ENDPOINT_POOR_MARGIN == 0.15
    assert EVIDENCE_POOR_MEAN == 0.35
