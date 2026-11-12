"""The scored ranking as the live pick (`TRET_ROUTER_AGENT_SCORING=live` plus
`adaptive.ranking: scored`): two switches, no router call, today's path otherwise."""
from __future__ import annotations

import json
import random

import pytest

from tests.test_router_exploration import (
    ROUTE_ARGS,
    _AnsweringRegistry,
    _catalog,
    _model,
    _policy,
)
from tests.test_router_shadow import HARNESS, OBJ, NOW, _NoAgent, _Stub, _comparable
from tret.adaptive import (
    DEFAULT_ADAPTIVE,
    RANKING_MODES,
    STATIC_ADAPTIVE,
    adaptive_of,
    validation_error,
)
from tret.config import Settings, get_settings
from tret.router_llm import scoring
from tret.router_llm.priors import reset_priors_registry
from tret.router_llm.router import ModelRouter

SCORED = {"ranking": "scored"}


@pytest.fixture(autouse=True)
def _fresh_registry():
    reset_priors_registry()
    yield
    reset_priors_registry()


def _rows(model_id, cost=0.05, n=12, ok=True):
    return [
        scoring.AgentRow(
            model_id=model_id, objective=OBJ, observed_at=NOW,
            outcome_class="delivered" if ok else "failed", cost_usd=cost, energy_wh=None,
            input_tokens=1000, output_tokens=500, findings_rejected=0, is_batch=False,
            target_class=None,
        )
        for _ in range(n)
    ]


class _CountingRegistry(_AnsweringRegistry):
    def __init__(self, providers, pick):
        super().__init__(providers, pick)
        self.calls = 0

    def get(self, provider):
        self.calls += 1
        return super().get(provider)


def _router(stub, *models, pick="test/alpha", seed=0, **kw):
    models = models or (_model("alpha"), _model("beta", price="2"))
    registry = _CountingRegistry({"openrouter"}, pick)
    router = ModelRouter(
        _catalog(*models), registry, stub, rng=random.Random(seed), **kw
    )
    return router, registry


def _uncurated(name, **kw):
    m = _model(name, **kw)
    m.curated = False  # never a live candidate; reachable only through a record
    return m


def _pol(adaptive=None, **over):
    return _policy(objective=over.pop("objective", OBJ), adaptive=adaptive, **over) if adaptive is not None else _policy(objective=over.pop("objective", OBJ), **over)


async def _decide(router, mode, monkeypatch, *, policy=None, **over):
    monkeypatch.setattr(get_settings(), "router_agent_scoring", mode)
    args = dict(model_policy=policy or _pol(), harness_id=HARNESS, **ROUTE_ARGS)
    args.update(over)
    return await router.route(**args)


def _today(d):
    """The decision as today's router makes it: no shadow, no scored."""
    j = _comparable(d)
    j.pop("shadow", None)
    j.pop("scored", None)
    return j


def _disagreeing():
    """alpha is the router stub's pick; beta is far cheaper on this agent's record."""
    return _Stub(rows=_rows("test/alpha", cost=0.20) + _rows("test/beta", cost=0.01))


# 1 ─ live without the opt-in is shadow ───────────────────────────────────────
@pytest.mark.parametrize("adaptive", [None, {"ranking": "tiered"}])
async def test_live_without_the_policy_opt_in_equals_shadow(monkeypatch, adaptive):
    policy = _pol(adaptive)
    live, _ = _router(_disagreeing(), shadow_rng=random.Random(4))
    shadow, _ = _router(_disagreeing(), shadow_rng=random.Random(4))
    a = await _decide(live, "live", monkeypatch, policy=policy)
    b = await _decide(shadow, "shadow", monkeypatch, policy=policy)
    assert _comparable(a) == _comparable(b)
    assert a.shadow is not None and a.scored is None and "scored" not in a.to_json()


# 2 ─ shadow with the opt-in has no live effect ───────────────────────────────
async def test_shadow_with_the_opt_in_has_no_live_effect(monkeypatch):
    sh, reg = _router(_disagreeing())
    off, _ = _router(_disagreeing())
    a = await _decide(sh, "shadow", monkeypatch, policy=_pol(SCORED))
    b = await _decide(off, "off", monkeypatch, policy=_pol(SCORED))
    assert reg.calls > 0 and a.chosen_model == "test/alpha"
    assert a.shadow is not None and a.scored is None and "scored" not in a.to_json()
    assert _today(a) == _today(b)


# 3 ─ off with the opt-in is today ────────────────────────────────────────────
async def test_off_with_the_opt_in_is_today(monkeypatch):
    on, _ = _router(_disagreeing())
    base, _ = _router(_disagreeing())
    a = await _decide(on, "off", monkeypatch, policy=_pol(SCORED))
    b = await _decide(base, "off", monkeypatch, policy=_pol())
    j = a.to_json()
    assert a.shadow is None and "shadow" not in j and "scored" not in j
    assert _comparable(a) == _comparable(b)


# 4 ─ the live pick ───────────────────────────────────────────────────────────
async def test_live_scored_picks_the_ranked_model_with_no_router_call(monkeypatch):
    router, reg = _router(_disagreeing(), pick="test/alpha")
    d = await _decide(router, "live", monkeypatch, policy=_pol(SCORED))
    assert d.chosen_model == "test/beta"
    assert reg.calls == 0 and d.router_model is None and d.spend is None
    assert "test/beta" in d.reasoning and "No router call was made." in d.reasoning
    assert d.exploration["explored"] is False
    j = d.to_json()
    assert "shadow" not in j and j["scored"]["live"] is True and j["scored"]["status"] == "scored"
    assert j["scored"]["live_choice"] == "test/beta" and j["scored"]["harness_id"] == str(HARNESS)
    assert d.candidates[0] == d.chosen_model
    assert sorted(d.candidates) == ["test/alpha", "test/beta"]


# 5 ─ a pick outside the live candidates ──────────────────────────────────────
async def test_a_model_with_a_record_outside_the_live_candidates_can_be_picked(monkeypatch):
    a, b, extra = _model("alpha"), _model("beta", price="2"), _uncurated("extra")
    stub = _Stub(rows=_rows("test/alpha", 0.20) + _rows("test/beta", 0.20) + _rows(extra.id, 0.01))
    router, reg = _router(stub, a, b, extra)
    d = await _decide(router, "live", monkeypatch, policy=_pol(SCORED))
    assert d.chosen_model == extra.id and d.candidates[0] == extra.id
    assert sorted(d.candidates) == sorted([a.id, b.id, extra.id])
    assert reg.calls == 0


# 6 ─ the scored path runs before the single-candidate return ─────────────────
async def test_one_live_candidate_plus_a_better_extra_picks_the_extra(monkeypatch):
    a, extra = _model("alpha"), _uncurated("extra")
    stub = _Stub(rows=_rows(a.id, 0.20) + _rows(extra.id, 0.01))
    router, _ = _router(stub, a, extra)
    d = await _decide(router, "live", monkeypatch, policy=_pol(SCORED))
    assert d.chosen_model == extra.id and d.scored is not None


async def test_one_live_candidate_and_no_record_is_todays_single_candidate_decision(monkeypatch):
    a, extra = _model("alpha"), _uncurated("extra")
    live, _ = _router(_Stub(rows=[]), a, extra)
    off, _ = _router(_Stub(rows=[]), a, extra)
    d = await _decide(live, "live", monkeypatch, policy=_pol(SCORED))
    today = await _decide(off, "off", monkeypatch, policy=_pol(SCORED))
    assert d.reasoning.startswith("Only one candidate model available.") and d.scored is None
    assert _today(d) == _today(today)


# 7 ─ every fall-through is today's decision ──────────────────────────────────
async def _fall_through(monkeypatch, stub, policy, **over):
    live, reg = _router(stub)
    off, _ = _router(stub)
    d = await _decide(live, "live", monkeypatch, policy=policy, **over)
    today = await _decide(off, "off", monkeypatch, policy=policy, **over)
    assert d.scored is None and "scored" not in d.to_json()
    assert _today(d) == _today(today)
    return d, reg


async def test_fall_through_no_agent_record(monkeypatch):
    d, reg = await _fall_through(monkeypatch, _Stub(rows=[]), _pol(SCORED))
    assert reg.calls > 0 and d.shadow["status"] == "no_agent_record"


@pytest.mark.parametrize("objective", ["quality", "token_conservation"])
async def test_fall_through_objective_not_scored(monkeypatch, objective):
    rows = _rows("test/alpha", 0.20) + _rows("test/beta", 0.01)
    d, reg = await _fall_through(monkeypatch, _Stub(rows=rows), _pol(SCORED, objective=objective))
    assert reg.calls > 0 and d.shadow["status"] == "objective_not_scored"


async def test_fall_through_learning_off(monkeypatch):
    policy = _pol({"ranking": "scored", "learn_from_outcomes": False})
    d, reg = await _fall_through(monkeypatch, _disagreeing(), policy)
    assert reg.calls > 0 and d.shadow["status"] == "learning_off"


async def test_fall_through_priors_without_for_agent(monkeypatch):
    d, reg = await _fall_through(monkeypatch, _NoAgent(), _pol(SCORED))
    assert reg.calls > 0 and d.shadow["status"] == "unavailable"


async def test_fall_through_for_agent_raising(monkeypatch):
    d, reg = await _fall_through(monkeypatch, _Stub(boom=True), _pol(SCORED))
    assert reg.calls > 0 and d.shadow["status"] == "error"


async def test_fall_through_decide_raising(monkeypatch):
    def boom(**kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(scoring, "decide", boom)
    d, reg = await _fall_through(monkeypatch, _disagreeing(), _pol(SCORED))
    assert reg.calls > 0 and d.shadow["status"] == "error"


async def test_fall_through_best_effort_context_fit(monkeypatch):
    d, reg = await _fall_through(
        monkeypatch, _disagreeing(), _pol(SCORED), min_context_window=10_000_000
    )
    assert d.context_fit["mode"] == "best_effort" and reg.calls > 0


# 8 ─ override and pin ────────────────────────────────────────────────────────
async def test_override_and_pinned_decisions_are_unchanged(monkeypatch):
    stub = _disagreeing()
    router, reg = _router(stub)
    over = await _decide(router, "live", monkeypatch, policy=_pol(SCORED), run_override="test/alpha")
    pinned = await _decide(
        router, "live", monkeypatch, policy=_pol(SCORED, mode="pinned", model="test/alpha")
    )
    for d in (over, pinned):
        assert d.chosen_model == "test/alpha" and d.scored is None and d.shadow is None
        assert "scored" not in d.to_json() and "shadow" not in d.to_json()
    assert stub.agent_calls == 0 and reg.calls == 0


# 9 ─ cooldown ────────────────────────────────────────────────────────────────
async def test_a_cooling_model_with_a_record_is_not_picked(monkeypatch):
    # a live candidate under cooldown
    stub = _Stub(rows=_rows("test/alpha", 0.20) + _rows("test/beta", 0.01), cooling={"test/beta"})
    router, _ = _router(stub)
    d = await _decide(router, "live", monkeypatch, policy=_pol(SCORED))
    assert d.chosen_model == "test/alpha" and "test/beta" not in d.candidates
    # an extra (not a live candidate) under cooldown
    a, extra = _model("alpha"), _uncurated("extra")
    stub = _Stub(rows=_rows(a.id, 0.20) + _rows(extra.id, 0.01), cooling={extra.id})
    router, _ = _router(stub, a, extra)
    d = await _decide(router, "live", monkeypatch, policy=_pol(SCORED))
    assert extra.id not in d.candidates and d.chosen_model == a.id


# 10 ─ the draw ───────────────────────────────────────────────────────────────
FIRES_SEED = next(s for s in range(500) if random.Random(s).random() < scoring.EXPLORATION_SHARE)


async def test_the_draw_is_recorded_but_not_applied_and_the_live_rng_is_untouched(monkeypatch):
    alpha, beta, gamma = _model("alpha"), _model("beta", price="2"), _model("gamma")
    stub = _Stub(rows=_rows(alpha.id, 0.20) + _rows(beta.id, 0.01))
    policy = _pol({"ranking": "scored", "exploration": 0.2})
    router, reg = _router(
        stub, alpha, beta, gamma, seed=7, shadow_rng=random.Random(FIRES_SEED)
    )
    for i in range(3):
        d = await _decide(router, "live", monkeypatch, policy=policy)
        ex = d.scored["exploration"]
        assert ex["draw"] is not None and ex["applied"] is False and ex["candidate"] == gamma.id
        assert ex["fired"] is (i == 0)  # only the first draw of the seeded stream is under the share
        assert d.chosen_model == beta.id == d.scored["ranked_pick"]
    assert reg.calls == 0
    assert router._rng.random() == random.Random(7).random()


async def test_with_exploration_off_there_is_no_draw(monkeypatch):
    shadow_rng = random.Random(3)
    router, _ = _router(_disagreeing(), shadow_rng=shadow_rng)
    d = await _decide(router, "live", monkeypatch, policy=_pol({"ranking": "scored", "exploration": 0}))
    assert d.scored["exploration"]["draw"] is None and d.scored["exploration"]["share"] == 0.0
    assert shadow_rng.random() == random.Random(3).random()


# 11 ─ the open-weights margin ────────────────────────────────────────────────
async def test_the_open_weights_margin_can_pick_a_lower_ranked_model_first(monkeypatch):
    closed, openm = _model("alpha"), _model("beta", price="2")
    openm.open_weights = True
    stub = _Stub(rows=_rows(closed.id, 0.050) + _rows(openm.id, 0.055))
    router, reg = _router(stub, closed, openm)
    d = await _decide(router, "live", monkeypatch, policy=_pol(SCORED, open_weights="prefer"))
    assert d.scored["ranking"][0] == closed.id and d.scored["open_weights"]["applied"] is True
    assert d.chosen_model == openm.id and d.candidates[0] == openm.id
    assert sorted(d.candidates) == sorted([closed.id, openm.id]) and reg.calls == 0


# ── the policy field ─────────────────────────────────────────────────────────
def test_adaptive_ranking_defaults_to_tiered_and_reads_scored():
    assert DEFAULT_ADAPTIVE.ranking == "tiered" and STATIC_ADAPTIVE.ranking == "tiered"
    assert adaptive_of(None).ranking == "tiered" and adaptive_of({"adaptive": {}}).ranking == "tiered"
    assert adaptive_of({"adaptive": {"ranking": "scored"}}).ranking == "scored"
    assert DEFAULT_ADAPTIVE.to_json()["ranking"] == "tiered"


@pytest.mark.parametrize("bad", ["Scored", "", None, 3, ["scored"], True])
def test_a_bad_stored_ranking_falls_back_to_tiered(bad):
    assert adaptive_of({"adaptive": {"ranking": bad}}).ranking == "tiered"


def test_validation_accepts_both_modes_and_refuses_others():
    for mode in RANKING_MODES:
        assert validation_error({"ranking": mode}) is None
    for bad in ("auto", "", None, 1):
        assert "ranking must be one of tiered|scored" in validation_error({"ranking": bad})


# ── the setting ──────────────────────────────────────────────────────────────
def test_the_setting_accepts_live_and_refuses_an_unknown_value():
    assert Settings(router_agent_scoring=" Live ").router_agent_scoring == "live"
    for ok in ("off", "shadow"):
        assert Settings(router_agent_scoring=ok).router_agent_scoring == ok
    with pytest.raises(ValueError):
        Settings(router_agent_scoring="bogus")


# ── follow-ups ───────────────────────────────────────────────────────────────
def _forbidden_case(kind):
    a, b, bad = _model("alpha"), _model("beta", price="2"), _uncurated("bad", price="0.1")
    policy = _pol()
    if kind == "open_weights_require":
        a.open_weights = b.open_weights = True
        bad.open_weights = False
        policy = _pol(open_weights="require")
    elif kind == "min_cost_tier":
        a.cost_tier = b.cost_tier = "standard"
        bad.cost_tier = "economy"
        policy = _pol(min_cost_tier="standard")
    elif kind == "no_key":
        bad.provider = "nokey"
    return a, b, bad, policy


@pytest.mark.parametrize("kind", ["open_weights_require", "min_cost_tier", "no_key", "zdr"])
async def test_live_never_picks_a_model_the_policy_forbids(monkeypatch, kind):
    a, b, bad, policy = _forbidden_case(kind)
    if kind == "zdr":
        bad.zdr_tools_endpoint = False
        monkeypatch.setattr(get_settings(), "openrouter_provider_prefs", {"zdr": True})
    policy = {**policy, "adaptive": {"ranking": "scored"}}
    stub = _Stub(rows=_rows(a.id, 0.20) + _rows(b.id, 0.20) + _rows(bad.id, 0.001))
    router, _ = _router(stub, a, b, bad)
    d = await _decide(router, "live", monkeypatch, policy=policy)
    assert d.scored is not None and d.chosen_model in (a.id, b.id)
    assert bad.id not in d.candidates


async def test_a_scored_decision_is_strict_json(monkeypatch):
    router, _ = _router(_disagreeing())
    d = await _decide(router, "live", monkeypatch, policy=_pol(SCORED))
    assert d.scored is not None
    json.dumps(d.to_json(), allow_nan=False)


class _FamilyStub(_Stub):
    async def for_key_family(self, *, task_shape, objective, size_band=None, task_family):
        return self.priors, {"used": True, "gate": {"open": True}, "adjusted": {}}


async def test_the_scored_pick_still_carries_the_family_fields(monkeypatch):
    stub = _FamilyStub(rows=_rows("test/alpha", cost=0.20) + _rows("test/beta", cost=0.01))
    router, reg = _router(stub)
    d = await _decide(router, "live", monkeypatch, policy=_pol(SCORED), task_family="f@w")
    assert d.scored is not None and d.chosen_model == "test/beta" and reg.calls == 0
    assert d.task_family == "f@w"
    assert d.family_priors == {"used": True, "gate": {"open": True}, "adjusted": {}}


async def test_live_shadow_blocks_for_a_non_opted_agent_match_shadow_over_a_sequence(monkeypatch):
    async def run(mode):
        router, _ = _router(_disagreeing(), shadow_rng=random.Random(11))
        return [(await _decide(router, mode, monkeypatch)).shadow for _ in range(10)]

    live, shadow = await run("live"), await run("shadow")
    assert live == shadow and all(b is not None for b in live)
