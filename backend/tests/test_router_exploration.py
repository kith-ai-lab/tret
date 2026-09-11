"""Bounded exploration: giving an untried model a chance to earn a prior.

`candidate_sort_key` (objectives.py) keeps an untried model (`TIER_UNKNOWN`,
no prior) from being ranked last, but nothing before this ever routed to one
*on purpose* — the LLM router reads the candidate list top-down and is free to
always pick the same proven leader, so a model with no track record could sit
in the catalog forever without ever earning one.

`ModelRouter._maybe_explore` (router_llm/router.py) is the fix: on a narrow,
low-stakes slice of decisions — the default `balanced` objective, the
`extraction` task shape, which terminates in a schema-validated tool call
rather than open-ended judgment — a seeded coin flip may pick an untried
candidate directly, skipping the router LLM call entirely. Every other
decision runs exactly as it always has.

Fixtures follow `test_router_context_fit.py`'s pattern: a `ProviderRegistry`
fake keyed on which providers have keys, and a `ModelCatalog` whose `_static`
table is replaced outright so every test controls exactly which models,
prices, and cost tiers exist.
"""
from __future__ import annotations

import random
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import HTTPException

from tret.adaptive import (
    DEFAULT_EXPLORATION,
    MAX_EXPLORATION,
    STATIC_ADAPTIVE,
    adaptive_of,
    validation_error,
)
from tret.api.harnesses import _validate_policy
from tret.db.models import Harness
from tret.providers.base import JsonCompletion, ProviderError, Usage
from tret.providers.catalog import ModelCatalog, ModelInfo, ProviderRegistry
from tret.router_llm.priors_base import ModelPrior
from tret.router_llm.router import ModelRouter


class _Registry(ProviderRegistry):
    def __init__(self, providers: set[str]):
        self._providers = providers

    def has_key(self, provider: str) -> bool:
        return provider in self._providers


class _NeverCalledRegistry(_Registry):
    """Proves a decision never contacted a router: `.get()` fails the test
    outright rather than returning something that could quietly serve a call
    exploration was supposed to skip."""

    def get(self, provider: str):
        raise AssertionError(
            f"router contacted provider {provider!r} on a decision exploration should "
            "have decided without any router call"
        )


class _AnsweringRegistry(_Registry):
    """Keys, and a provider that actually answers the routing call — same
    shape as test_adaptive_routing.py's fixture of the same name."""

    def __init__(self, providers: set[str], pick: str):
        super().__init__(providers)
        self._pick = pick

    def get(self, provider: str):
        pick = self._pick

        class _P:
            async def complete_json(self, **kwargs):
                return JsonCompletion(
                    payload={"model_id": pick, "reasoning": "because", "confidence": "high"},
                    usage=Usage(input_tokens=900, output_tokens=40),
                    model=kwargs.get("model", ""),
                )

        return _P()


class _StubPriors:
    def __init__(self, priors: dict[str, ModelPrior]):
        self.priors = priors

    async def for_key(self, *, task_shape, objective, size_band=None):
        return self.priors

    def invalidate(self) -> None:
        pass


def _model(
    name: str,
    *,
    cost_tier: str = "economy",
    provider: str = "openrouter",
    price: str = "1",
) -> ModelInfo:
    return ModelInfo(
        id=f"test/{name}",
        provider=provider,
        wire_id=name,
        display_name=name,
        context_window=200_000,
        input_price_per_mtok=Decimal(price),
        output_price_per_mtok=Decimal(price),
        cost_tier=cost_tier,
        supports_tools=True,
        curated=True,
    )


def _catalog(*models: ModelInfo) -> ModelCatalog:
    catalog = ModelCatalog()
    catalog._static = {m.id: m for m in models}
    return catalog


def _prior(model_id: str, quality: float, *, floor: float | None = None, effective_n=30.0):
    return ModelPrior(
        model_id=model_id,
        runs=40,
        effective_n=effective_n,
        quality_mean=quality,
        quality_raw=quality,
        quality_ci_low=quality if floor is None else floor,
        delivered_rate=quality,
        failure_rate=1 - quality,
        mean_cost_usd=0.02,
        mean_output_tokens=800,
        mean_iterations=5.0,
        mean_energy_wh=0.4,
        approvals=0,
        rejections=0,
    )


ROUTE_ARGS = dict(
    task_type="line_item_extraction",
    task_shape="extraction",
    task_description="Pull the reported figures out of the filing.",
    output_contract="extraction",
    n_documents=1,
    est_input_tokens=4000,
)


def _policy(**over) -> dict:
    base = {"mode": "auto", "max_cost_tier": "premium"}
    base.update(over)
    return base


async def _route(router: ModelRouter, policy: dict, **over):
    args = dict(model_policy=policy, **ROUTE_ARGS)
    args.update(over)
    return await router.route(**args)


# A fixed seed whose first draw (0.134...) sits inside the max allowed
# exploration rate (0.2) but above the default (0.05) — one seed serves both
# "fires" (at a raised rate) and "does not fire" (at the default rate) tests.
FIRES_AT_MAX_SEED = 1
DOES_NOT_FIRE_AT_DEFAULT_SEED = 0


# ── it fires: no router call, the pick is recorded ──────────────────────────
async def test_a_fired_roll_picks_the_untried_model_with_no_router_call():
    untried = _model("untried-economy")
    proven = _model("proven-economy", price="2")
    priors = _StubPriors({proven.id: _prior(proven.id, 0.9, floor=0.8)})
    router = ModelRouter(
        _catalog(untried, proven),
        _NeverCalledRegistry({"openrouter"}),
        priors,
        rng=random.Random(FIRES_AT_MAX_SEED),
    )

    decision = await _route(router, _policy(adaptive={"exploration": MAX_EXPLORATION}))

    assert decision.chosen_model == untried.id
    assert decision.router_model is None
    assert decision.router_prompt is None
    assert decision.fallback_used is False
    assert decision.exploration == {
        "explored": True,
        "candidate": untried.id,
        "probability": MAX_EXPLORATION,
        "eligible": 1,
        "reason": "untried model within exploration tier",
    }
    assert "no router call" in decision.reasoning or "router overhead" in decision.reasoning


async def test_ties_are_broken_deterministically_cheapest_then_id():
    a = _model("z-model", price="1")
    b = _model("a-model", price="1")  # same price, lower id — must win the tie
    c = _model("mid-model", price="5")  # pricier — never wins
    router = ModelRouter(
        _catalog(a, b, c), _NeverCalledRegistry({"openrouter"}), rng=random.Random(FIRES_AT_MAX_SEED)
    )

    decision = await _route(router, _policy(adaptive={"exploration": MAX_EXPLORATION}))

    assert decision.chosen_model == b.id


# ── it does not fire: the LLM path runs exactly as before ───────────────────
async def test_when_the_roll_does_not_fire_the_llm_router_runs_as_before():
    untried = _model("untried-economy")
    proven = _model("proven-economy", price="2")
    priors = _StubPriors({proven.id: _prior(proven.id, 0.9, floor=0.8)})
    router = ModelRouter(
        _catalog(untried, proven),
        _AnsweringRegistry({"openrouter"}, proven.id),
        priors,
        rng=random.Random(DOES_NOT_FIRE_AT_DEFAULT_SEED),
    )

    decision = await _route(router, _policy())  # default exploration = 0.05

    assert decision.chosen_model == proven.id
    assert decision.router_model is not None
    assert decision.fallback_used is False
    assert decision.exploration == {
        "explored": False,
        "candidate": None,
        "probability": 0.05,
        "eligible": 1,
        "reason": "exploration roll did not fire",
    }


async def test_a_declined_roll_still_lets_the_fallback_run_when_the_router_fails():
    untried = _model("untried-economy")
    other = _model("other-economy", price="2")

    class _KeyedButUnreachable(_Registry):
        def get(self, provider: str):
            raise ProviderError(provider, "router model unreachable in this test")

    router = ModelRouter(
        _catalog(untried, other),
        _KeyedButUnreachable({"openrouter"}),
        rng=random.Random(DOES_NOT_FIRE_AT_DEFAULT_SEED),
    )

    decision = await _route(router, _policy())

    assert decision.fallback_used is True
    assert decision.exploration is not None
    assert decision.exploration["explored"] is False
    assert decision.exploration["eligible"] == 2


# ── never fires: objective, task shape, and a zero rate ─────────────────────
@pytest.mark.parametrize("objective", ["quality", "eco", "token_conservation"])
async def test_never_fires_under_a_non_balanced_objective(objective):
    untried = _model("untried-economy")
    other = _model("other-economy", price="2")
    router = ModelRouter(
        _catalog(untried, other),
        _AnsweringRegistry({"openrouter"}, other.id),
        rng=random.Random(FIRES_AT_MAX_SEED),  # would fire if anything let it
    )

    decision = await _route(
        router, _policy(objective=objective, adaptive={"exploration": MAX_EXPLORATION})
    )

    assert decision.exploration["explored"] is False
    assert decision.exploration["eligible"] is None
    assert "objective" in decision.exploration["reason"]


@pytest.mark.parametrize("task_shape", ["verdict", "drafting", "freeform", "qa_review"])
async def test_never_fires_outside_the_extraction_task_shape(task_shape):
    untried = _model("untried-economy")
    other = _model("other-economy", price="2")
    router = ModelRouter(
        _catalog(untried, other),
        _AnsweringRegistry({"openrouter"}, other.id),
        rng=random.Random(FIRES_AT_MAX_SEED),
    )

    decision = await _route(
        router,
        _policy(adaptive={"exploration": MAX_EXPLORATION}),
        task_shape=task_shape,
    )

    assert decision.exploration["explored"] is False
    assert decision.exploration["eligible"] is None
    assert "task shape" in decision.exploration["reason"]


async def test_never_fires_when_the_harness_exploration_rate_is_zero():
    untried = _model("untried-economy")
    other = _model("other-economy", price="2")
    router = ModelRouter(
        _catalog(untried, other),
        _AnsweringRegistry({"openrouter"}, other.id),
        rng=random.Random(FIRES_AT_MAX_SEED),
    )

    decision = await _route(router, _policy(adaptive={"exploration": 0.0}))

    assert decision.exploration == {
        "explored": False,
        "candidate": None,
        "probability": 0.0,
        "eligible": None,
        "reason": "harness exploration rate is 0",
    }


async def test_never_fires_with_learning_off_even_with_a_stored_poor_prior():
    """The guardrail bypass this fixes: with `learn_from_outcomes: False`,
    `route()` never fetches `priors` at all (it stays `{}`), so a model this
    deployment's own evidence already marked poor would otherwise read as
    plain `TIER_UNKNOWN` — indistinguishable from genuinely untried — and
    could be explored anyway. `_exploration_off_reason` must refuse before it
    ever gets that far, on a seeded roll that would otherwise fire, and the
    LLM router must actually run instead.
    """
    poor = _model("poor-economy")
    other = _model("other-economy", price="2")
    priors = _StubPriors({poor.id: _prior(poor.id, 0.1, floor=0.05)})
    router = ModelRouter(
        _catalog(poor, other),
        _AnsweringRegistry({"openrouter"}, poor.id),
        priors,
        rng=random.Random(FIRES_AT_MAX_SEED),  # would fire if anything let it
    )

    decision = await _route(
        router,
        _policy(adaptive={"exploration": MAX_EXPLORATION, "learn_from_outcomes": False}),
    )

    assert decision.exploration == {
        "explored": False,
        "candidate": None,
        "probability": MAX_EXPLORATION,
        "eligible": None,
        "reason": "outcome learning is off, so an untried model has nothing to earn",
    }
    # The LLM router was actually consulted, not skipped — `_resolve_router_model`
    # is never stubbed out in this test, so a null `router_model` would mean
    # the decision took some other path than the one under test.
    assert decision.router_model is not None
    assert decision.fallback_used is False


async def test_a_poor_prior_model_is_never_explored():
    """With learning on (the ordinary case), a model whose prior already
    crossed into `TIER_POOR` must never be treated as untried, however the
    roll lands — distinct from `test_never_fires_with_learning_off_...`
    above, which covers the case where there is no prior to consult at all.
    """
    poor = _model("poor-economy")
    other = _model("other-economy", price="2")
    priors = _StubPriors({poor.id: _prior(poor.id, 0.1, floor=0.05)})
    router = ModelRouter(
        _catalog(poor, other),
        _AnsweringRegistry({"openrouter"}, other.id),
        priors,
        rng=random.Random(FIRES_AT_MAX_SEED),
    )

    decision = await _route(router, _policy(adaptive={"exploration": MAX_EXPLORATION}))

    # `other` (no prior at all) is the only untried candidate; `poor` must
    # never be counted or chosen.
    assert decision.exploration["eligible"] == 1
    assert decision.exploration["explored"] is True
    assert decision.exploration["candidate"] == other.id


async def test_exploration_is_none_on_the_single_candidate_path():
    only = _model("only-model")
    router = ModelRouter(
        _catalog(only), _NeverCalledRegistry({"openrouter"}), rng=random.Random(FIRES_AT_MAX_SEED)
    )

    decision = await _route(router, _policy(adaptive={"exploration": MAX_EXPLORATION}))

    assert decision.chosen_model == only.id
    assert decision.exploration is None


async def test_static_adaptive_has_exploration_zero(tmp_path):
    # What the golden-run evals and both benchmark arms pin.
    assert STATIC_ADAPTIVE.exploration == 0.0

    # `golden_world.create_harness` (tests/evals/golden_world.py) wires
    # STATIC_ADAPTIVE in as the default `model_policy.adaptive` whenever a
    # caller does not pass its own — built directly here (not via the
    # `tests/evals/conftest.py` `world` fixture, which is scoped to that
    # directory) so this file stays self-contained about what it asserts.
    evals_dir = str(Path(__file__).resolve().parent / "evals")
    if evals_dir not in sys.path:
        sys.path.insert(0, evals_dir)
    from golden_world import build_world  # local import: see sys.path above

    world = await build_world(tmp_path / "golden.db")
    try:
        harness_id = await world.create_harness()
        async with world.session_factory() as db:
            harness = await db.get(Harness, harness_id)
        assert harness.model_policy["adaptive"]["exploration"] == 0.0
    finally:
        await world.aclose()


# ── never fires: pins and overrides bypass exploration entirely ─────────────
async def test_never_fires_under_a_harness_pin():
    pinned = _model("pinned-model")
    router = ModelRouter(
        _catalog(pinned), _NeverCalledRegistry({"openrouter"}), rng=random.Random(FIRES_AT_MAX_SEED)
    )

    decision = await _route(
        router,
        {
            "mode": "pinned",
            "model": pinned.id,
            "adaptive": {"exploration": MAX_EXPLORATION},
        },
    )

    assert decision.override == "user_pin"
    assert decision.exploration is None


async def test_never_fires_under_a_run_override():
    picked = _model("picked-model")
    other = _model("other-model", price="2")
    router = ModelRouter(
        _catalog(picked, other), _NeverCalledRegistry({"openrouter"}), rng=random.Random(FIRES_AT_MAX_SEED)
    )

    decision = await _route(
        router,
        _policy(adaptive={"exploration": MAX_EXPLORATION}),
        run_override=picked.id,
    )

    assert decision.override == "run_override"
    assert decision.exploration is None


# ── never fires: nothing untried survives the tier / ceiling filters ────────
async def test_never_fires_when_the_only_untried_model_is_above_the_exploration_tier():
    untried_standard = _model("untried-standard", cost_tier="standard")
    proven_economy = _model("proven-economy", price="2")
    priors = _StubPriors({proven_economy.id: _prior(proven_economy.id, 0.9, floor=0.8)})
    router = ModelRouter(
        _catalog(untried_standard, proven_economy),
        _AnsweringRegistry({"openrouter"}, proven_economy.id),
        priors,
        rng=random.Random(FIRES_AT_MAX_SEED),
    )

    decision = await _route(
        router,
        _policy(adaptive={"exploration": MAX_EXPLORATION, "exploration_max_cost_tier": "economy"}),
    )

    assert decision.exploration["explored"] is False
    assert decision.exploration["eligible"] == 0
    assert decision.chosen_model == proven_economy.id


async def test_never_fires_when_the_only_untried_model_is_above_the_harness_ceiling():
    untried_premium = _model("untried-premium", cost_tier="premium")
    # Two economy candidates, both already *tried*, so the harness ceiling is
    # the only thing under test here rather than the single-candidate path
    # (which never touches exploration at all — see the pin/override tests).
    economy_leader = _model("economy-leader", cost_tier="economy", price="1")
    economy_second = _model("economy-second", cost_tier="economy", price="2")
    priors = _StubPriors(
        {
            economy_leader.id: _prior(economy_leader.id, 0.9, floor=0.8),
            economy_second.id: _prior(economy_second.id, 0.5, floor=0.4),
        }
    )
    router = ModelRouter(
        _catalog(untried_premium, economy_leader, economy_second),
        _AnsweringRegistry({"openrouter"}, economy_leader.id),
        priors,
        rng=random.Random(FIRES_AT_MAX_SEED),
    )

    decision = await _route(
        router,
        _policy(max_cost_tier="economy", adaptive={"exploration": MAX_EXPLORATION}),
    )

    # The harness ceiling excludes the premium model before exploration ever
    # runs, so it is not even visible as a candidate, let alone as untried.
    assert untried_premium.id not in decision.candidates
    assert decision.exploration["explored"] is False
    assert decision.exploration["eligible"] == 0


def _model_with_window(name: str, *, context_window: int, price: str = "1") -> ModelInfo:
    return ModelInfo(
        id=f"test/{name}",
        provider="openrouter",
        wire_id=name,
        display_name=name,
        context_window=context_window,
        input_price_per_mtok=Decimal(price),
        output_price_per_mtok=Decimal(price),
        cost_tier="economy",
        supports_tools=True,
        curated=True,
    )


async def test_never_fires_when_context_fit_would_exclude_the_untried_model():
    # Two candidates whose window can hold the call (both *tried*, so the
    # thing under test — context fit, not the single-candidate shortcut — is
    # isolated) and one untried model too small to survive `_apply_context_fit`.
    small_untried = _model_with_window("small-untried", context_window=8_000)
    big_leader = _model_with_window("big-leader", context_window=200_000, price="1")
    big_second = _model_with_window("big-second", context_window=200_000, price="2")
    priors = _StubPriors(
        {
            big_leader.id: _prior(big_leader.id, 0.9, floor=0.8),
            big_second.id: _prior(big_second.id, 0.5, floor=0.4),
        }
    )
    router = ModelRouter(
        _catalog(small_untried, big_leader, big_second),
        _AnsweringRegistry({"openrouter"}, big_leader.id),
        priors,
        rng=random.Random(FIRES_AT_MAX_SEED),
    )

    decision = await _route(
        router,
        _policy(adaptive={"exploration": MAX_EXPLORATION}),
        min_context_window=100_000,
    )

    # Only the two big models fit; the small untried one is excluded by
    # context fit, not merely deprioritized, and must never be explored
    # regardless of the roll.
    assert decision.context_fit["mode"] == "fit"
    assert small_untried.id not in decision.candidates
    assert decision.exploration["eligible"] == 0
    assert decision.exploration["explored"] is False


# ── a model with a prior is never "untried", however the tier landed ────────
def test_a_model_with_a_full_but_mediocre_prior_is_not_untried():
    # Enough effective samples to have earned a verdict, and that verdict
    # happens to land back on TIER_UNKNOWN (neither proven nor poor) — this is
    # a *tried*, merely middling model, not a candidate for exploration, even
    # though `evidence_tier` alone cannot tell it apart from a truly untried
    # one (both are TIER_UNKNOWN).
    tried_mediocre = _model("tried-mediocre")
    untried = _model("untried-economy", price="2")
    priors = {tried_mediocre.id: _prior(tried_mediocre.id, 0.5, floor=0.4)}
    router = ModelRouter(_catalog(tried_mediocre, untried), _Registry({"openrouter"}))

    eligible = router._untried_exploration_candidates(
        [tried_mediocre, untried], priors, {"excluded": []}, "economy"
    )

    assert [m.id for m in eligible] == [untried.id]


async def test_a_model_with_a_full_but_mediocre_prior_is_never_explored_end_to_end():
    tried_mediocre = _model("tried-mediocre")
    proven = _model("proven-economy", price="2")
    priors = _StubPriors(
        {
            tried_mediocre.id: _prior(tried_mediocre.id, 0.5, floor=0.4),
            proven.id: _prior(proven.id, 0.9, floor=0.8),
        }
    )
    router = ModelRouter(
        _catalog(tried_mediocre, proven),
        _AnsweringRegistry({"openrouter"}, proven.id),
        priors,
        rng=random.Random(FIRES_AT_MAX_SEED),
    )

    decision = await _route(router, _policy(adaptive={"exploration": MAX_EXPLORATION}))

    assert decision.exploration["explored"] is False
    assert decision.exploration["eligible"] == 0


async def test_a_proven_model_is_never_explored():
    proven = _model("proven-economy")
    other = _model("other-economy", price="2")
    priors = _StubPriors({proven.id: _prior(proven.id, 0.9, floor=0.8)})
    router = ModelRouter(
        _catalog(proven, other),
        _AnsweringRegistry({"openrouter"}, other.id),
        priors,
        rng=random.Random(FIRES_AT_MAX_SEED),
    )

    decision = await _route(router, _policy(adaptive={"exploration": MAX_EXPLORATION}))

    # `other` (no prior at all) is the only untried candidate; `proven` must
    # never be counted or chosen. FIRES_AT_MAX_SEED's first draw is always
    # below MAX_EXPLORATION (see its own docstring), so this roll always
    # fires — the assertion below is unconditional, not "if it happened to".
    assert decision.exploration["eligible"] == 1
    assert decision.exploration["explored"] is True
    assert decision.exploration["candidate"] == other.id


# ── validation ────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "block",
    [
        {"exploration": -0.01},
        {"exploration": 0.21},
        {"exploration": "0.1"},
        {"exploration": True},
        {"exploration_max_cost_tier": "ultra"},
        {"exploration_max_cost_tier": 5},
    ],
)
def test_a_bad_exploration_block_is_refused_at_the_door(block):
    assert validation_error(block) is not None
    with pytest.raises(HTTPException) as e:
        _validate_policy(_policy(adaptive=block))
    assert e.value.status_code == 422


def test_in_range_exploration_values_validate():
    assert validation_error({"exploration": 0.0}) is None
    assert validation_error({"exploration": MAX_EXPLORATION}) is None
    assert validation_error({"exploration_max_cost_tier": "standard"}) is None


# ── a stored out-of-range exploration fails closed, not to the default ─────
@pytest.mark.parametrize("stored", [MAX_EXPLORATION + 0.5, -0.3, 5.0])
def test_out_of_range_stored_exploration_fails_closed_to_zero(stored):
    """`adaptive_of` reads values the API's own write-time validation would
    now refuse (`validation_error`), the same way a row written before
    validation existed, or one written when `MAX_EXPLORATION` was higher,
    would read today. Every other out-of-range field here falls back to its
    *default*, but exploration is a probability that gates a live model pick
    with no router call at all — reviving 0.05 for a value this deployment's
    own policy never actually asked for would silently turn exploration back
    on. It must fail *closed* to 0.0 instead.
    """
    resolved = adaptive_of({"adaptive": {"exploration": stored}}).exploration
    assert resolved == 0.0
    assert resolved != DEFAULT_EXPLORATION


async def test_provider_error_on_the_llm_path_still_falls_through_to_fallback_when_declined():
    # Belt and braces: a declined roll must not somehow swallow the ordinary
    # ProviderError-retry-then-fallback behavior the ungated path already had.
    untried = _model("untried-economy")
    other = _model("other-economy", price="2")

    class _FailingThenNothing(_Registry):
        def get(self, provider: str):
            raise ProviderError(provider, "boom")

    router = ModelRouter(
        _catalog(untried, other),
        _FailingThenNothing({"openrouter"}),
        rng=random.Random(DOES_NOT_FIRE_AT_DEFAULT_SEED),
    )

    decision = await _route(router, _policy())

    assert decision.fallback_used is True
    assert decision.exploration["explored"] is False
