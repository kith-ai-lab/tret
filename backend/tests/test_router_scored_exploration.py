"""The exploration share on the scored path (`TRET_ROUTER_AGENT_SCORING=live` plus
`adaptive.ranking: scored`): a set share of runs goes to the most promising model the
agent has not tried, within the policy's rules."""
from __future__ import annotations

import json
import random

import pytest

from tests.test_router_exploration import _catalog, _model
from tests.test_router_scored_live import (
    _CountingRegistry,
    _decide,
    _pol,
    _rows,
    _uncurated,
)
from tests.test_router_shadow import _Stub
from tret.router_llm.priors import reset_priors_registry
from tret.router_llm.router import ModelRouter

FIRES = 0.01  # a draw under any share used here
HELD = 0.5


@pytest.fixture(autouse=True)
def _fresh_registry():
    reset_priors_registry()
    yield
    reset_priors_registry()


class _Rng:
    """A generator that returns scripted draws and counts how many were taken."""

    def __init__(self, *draws):
        self.draws = list(draws)
        self.taken = 0

    def random(self):
        self.taken += 1
        return self.draws.pop(0)


def _router(stub, *models, draw=FIRES, seed=0, pick="test/alpha"):
    registry = _CountingRegistry({"openrouter"}, pick)
    shadow = _Rng(draw, draw, draw)
    router = ModelRouter(
        _catalog(*models), registry, stub, rng=random.Random(seed), shadow_rng=shadow
    )
    return router, registry, shadow


def _scored(exploration=0.1, **over):
    return _pol({"ranking": "scored", "exploration": exploration}, **over)


def _world(*extra):
    """alpha and beta have a record (beta is the better one); the rest are untried."""
    alpha, beta = _model("alpha"), _model("beta", price="2")
    models = (alpha, beta, *extra)
    return models, _Stub(rows=_rows(alpha.id, 0.20) + _rows(beta.id, 0.01))


async def test_a_fired_roll_picks_the_candidate_with_no_router_call(monkeypatch):
    gamma = _model("gamma")
    models, stub = _world(gamma)
    router, reg, _ = _router(stub, *models)
    d = await _decide(router, "live", monkeypatch, policy=_scored(0.1))
    assert d.chosen_model == gamma.id and d.candidates[0] == d.chosen_model
    assert d.exploration == {
        "explored": True, "candidate": gamma.id, "probability": 0.1, "eligible": 1,
        "reason": "scored ranking: exploration share",
    }
    s = d.scored
    assert s["exploration"]["applied"] is True and s["live"] is True
    assert s["live_choice"] == gamma.id and s["agrees_with_live"] is True
    assert d.router_model is None and reg.calls == 0
    assert gamma.id in d.reasoning and "No router call was made." in d.reasoning
    assert sorted(d.candidates) == sorted(m.id for m in models)
    json.dumps(d.to_json(), allow_nan=False)


async def test_a_roll_at_or_over_the_share_picks_the_ranked_model(monkeypatch):
    gamma = _model("gamma")
    models, stub = _world(gamma)
    for draw in (0.1, HELD):
        router, reg, _ = _router(stub, *models, draw=draw)
        d = await _decide(router, "live", monkeypatch, policy=_scored(0.1))
        assert d.chosen_model == "test/beta" and reg.calls == 0
        assert d.exploration["explored"] is False and d.exploration["candidate"] is None
        assert d.exploration["probability"] == 0.1 and d.exploration["eligible"] == 1
        assert "exploration roll did not fire" in d.exploration["reason"]
        assert d.scored["exploration"]["applied"] is False


async def test_exploration_zero_takes_no_draw(monkeypatch):
    models, stub = _world(_model("gamma"))
    router, _, shadow = _router(stub, *models)
    d = await _decide(router, "live", monkeypatch, policy=_scored(0))
    assert shadow.taken == 0 and d.scored["exploration"]["draw"] is None
    assert d.chosen_model == "test/beta" and d.exploration["explored"] is False
    assert d.exploration["reason"] == "scored ranking: exploration is off"


@pytest.mark.parametrize(
    "adaptive, draw, explores",
    [
        ({"ranking": "scored", "exploration": 0.2}, 0.15, True),
        ({"ranking": "scored", "exploration": 0.1}, 0.15, False),
        ({"ranking": "scored"}, 0.04, True),  # the default share is 0.05
        ({"ranking": "scored"}, 0.06, False),
    ],
)
async def test_the_share_is_the_policys_value(monkeypatch, adaptive, draw, explores):
    models, stub = _world(_model("gamma"))
    router, _, _ = _router(stub, *models, draw=draw)
    d = await _decide(router, "live", monkeypatch, policy=_pol(adaptive))
    assert d.exploration["explored"] is explores


async def test_the_shadow_block_of_a_non_opted_agent_uses_the_policys_share(monkeypatch):
    models, stub = _world(_model("gamma"))
    router, _, _ = _router(stub, *models, draw=0.15)
    d = await _decide(router, "live", monkeypatch, policy=_pol({"exploration": 0.2}))
    ex = d.shadow["exploration"]
    assert d.scored is None and ex["share"] == 0.2 and ex["fired"] is True
    router, _, _ = _router(stub, *models, draw=0.15)
    d = await _decide(router, "live", monkeypatch, policy=_pol({"exploration": 0.1}))
    assert d.shadow["exploration"]["share"] == 0.1 and d.shadow["exploration"]["fired"] is False


async def test_the_cost_ceiling_limits_the_candidate(monkeypatch):
    cheap_std = _model("cheapstd", price="0.1", cost_tier="standard")
    dear_eco = _model("dear", price="3")
    models, stub = _world(cheap_std, dear_eco)
    router, _, _ = _router(stub, *models)
    d = await _decide(router, "live", monkeypatch, policy=_scored(0.1))
    assert d.chosen_model == dear_eco.id and d.exploration["eligible"] == 1
    # A higher ceiling lets the cheaper standard-tier model in.
    policy = _pol({"ranking": "scored", "exploration": 0.1, "exploration_max_cost_tier": "standard"})
    router, _, _ = _router(stub, *models)
    d = await _decide(router, "live", monkeypatch, policy=policy)
    assert d.chosen_model == cheap_std.id


async def test_a_policy_floor_above_the_exploration_ceiling_lifts_it(monkeypatch):
    alpha, beta = _model("alpha", cost_tier="standard"), _model("beta", price="2", cost_tier="standard")
    gamma, delta = _model("gamma", cost_tier="standard"), _model("delta", cost_tier="premium")
    stub = _Stub(rows=_rows(alpha.id, 0.20) + _rows(beta.id, 0.01))
    router, _, _ = _router(stub, alpha, beta, gamma, delta)
    policy = _scored(0.1, min_cost_tier="standard", max_cost_tier="premium")
    d = await _decide(router, "live", monkeypatch, policy=policy)
    # The ceiling (economy) lifts to the floor (standard): gamma is in, premium delta is not.
    assert d.chosen_model == gamma.id and d.exploration["eligible"] == 1


async def test_a_specialist_variant_is_never_the_candidate(monkeypatch):
    coder = _model("super-coder", price="0.1")
    models, stub = _world(coder)
    router, _, _ = _router(stub, *models)
    d = await _decide(router, "live", monkeypatch, policy=_scored(0.1))
    assert d.chosen_model == "test/beta" and d.exploration["eligible"] == 0
    assert d.exploration["reason"] == "scored ranking: no untried model was eligible"
    assert "No untried model was eligible for exploration." in d.reasoning


@pytest.mark.parametrize("explored", [True, False])
async def test_the_live_rng_is_never_consumed(monkeypatch, explored):
    models, stub = _world(_model("gamma"))
    router, _, _ = _router(stub, *models, seed=7, draw=FIRES if explored else HELD)
    d = await _decide(router, "live", monkeypatch, policy=_scored(0.1))
    assert d.exploration["explored"] is explored
    assert router._rng.random() == random.Random(7).random()


async def test_an_explored_pick_still_respects_the_policy(monkeypatch):
    # outside `allowed`: the cheapest untried model is not permitted, a dearer one is
    gamma, delta = _model("gamma", price="0.1"), _model("delta", price="3")
    models, stub = _world(gamma, delta)
    router, _, _ = _router(stub, *models)
    d = await _decide(
        router, "live", monkeypatch,
        policy=_scored(0.1, allowed=["test/alpha", "test/beta", delta.id]),
    )
    assert gamma.id not in d.candidates and d.chosen_model == delta.id
    gamma = _model("gamma")
    # closed under open_weights: require
    alpha, beta = _model("alpha"), _model("beta", price="2")
    alpha.open_weights = beta.open_weights = True
    closed = _model("closed")
    closed.open_weights = False
    stub = _Stub(rows=_rows(alpha.id, 0.20) + _rows(beta.id, 0.01))
    router, _, _ = _router(stub, alpha, beta, closed)
    d = await _decide(router, "live", monkeypatch, policy=_scored(0.1, open_weights="require"))
    assert closed.id not in d.candidates and d.chosen_model == beta.id
    # under cooldown
    models, _ = _world(gamma)
    stub = _Stub(rows=_rows(models[0].id, 0.20) + _rows(models[1].id, 0.01), cooling={gamma.id})
    router, _, _ = _router(stub, *models)
    d = await _decide(router, "live", monkeypatch, policy=_scored(0.1))
    assert gamma.id not in d.candidates and d.chosen_model == "test/beta"
    # an uncurated model with no record is not in the pool at all
    models, stub = _world(_uncurated("ghost"))
    router, _, _ = _router(stub, *models)
    d = await _decide(router, "live", monkeypatch, policy=_scored(0.1))
    assert d.chosen_model == "test/beta"


async def test_no_untried_model_eligible_gives_the_ranked_pick(monkeypatch):
    models, stub = _world()
    router, reg, _ = _router(stub, *models)
    d = await _decide(router, "live", monkeypatch, policy=_scored(0.1))
    assert d.chosen_model == "test/beta" and reg.calls == 0
    assert d.exploration["eligible"] == 0 and d.exploration["explored"] is False
    assert "no untried model was eligible" in d.exploration["reason"]
    assert "No untried model was eligible for exploration." in d.reasoning
