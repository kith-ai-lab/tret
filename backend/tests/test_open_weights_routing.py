"""Open-weights policy, ZDR exclusion, uncurated-model gating, new-model seats,
and widened exploration. Offline: catalogs are built by hand."""
from __future__ import annotations

import random
from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException

import tret.router_llm.router as router_module
from tret.adaptive import MAX_EXPLORATION
from tret.api._policy import validate_policy
from tret.config import Settings
from tret.providers.catalog import ModelCatalog, ModelInfo, ProviderRegistry
from tret.router_llm.fallback import fallback_model
from tret.router_llm.objectives import candidate_sort_key
from tret.router_llm.priors_base import ModelPrior
from tret.router_llm.prompts import render_router_prompt
from tret.router_llm.router import CANDIDATE_LIMIT, ModelRouter, RoutingUnavailable

TODAY = date(2026, 11, 6)


class _Registry(ProviderRegistry):
    def __init__(self, providers=("anthropic", "openrouter", "kimi")):
        self._providers = set(providers)

    def has_key(self, provider: str) -> bool:
        return provider in self._providers


def _m(
    name: str,
    *,
    provider: str = "openrouter",
    price: str = "1",
    open_weights: bool | None = None,
    curated: bool = True,
    released: str | None = "2026-10",
    created_ts: int | None = None,
    tier: str = "economy",
    zdr: bool | None = None,
    wire: str | None = None,
) -> ModelInfo:
    wire_id = wire or name
    return ModelInfo(
        id=f"{provider}/{wire_id}",
        provider=provider,
        wire_id=wire_id,
        display_name=name,
        context_window=128_000,
        input_price_per_mtok=Decimal(price),
        output_price_per_mtok=Decimal(price),
        cost_tier=tier,
        supports_tools=True,
        curated=curated,
        released=released,
        open_weights=open_weights,
        created_ts=created_ts,
        zdr_tools_endpoint=zdr,
    )


def _catalog(static=(), dynamic=()) -> ModelCatalog:
    c = ModelCatalog()
    c._static = {m.id: m for m in static}
    c._dynamic = {m.id: m for m in dynamic}
    return c


def _policy(**over) -> dict:
    base = {"mode": "auto", "max_cost_tier": "premium"}
    base.update(over)
    return base


def _prior(model_id: str, quality: float, floor: float | None = None) -> ModelPrior:
    return ModelPrior(
        model_id=model_id, runs=40, effective_n=30.0, quality_mean=quality, quality_raw=quality,
        quality_ci_low=quality if floor is None else floor, delivered_rate=quality,
        failure_rate=1 - quality, mean_cost_usd=0.02, mean_output_tokens=800,
        mean_iterations=5.0, mean_energy_wh=0.4, approvals=0, rejections=0,
    )


def _ids(router: ModelRouter, policy: dict, priors=None) -> list[str]:
    return [m.id for m in router._candidates(policy, priors)]


@pytest.fixture(autouse=True)
def _frozen_today(monkeypatch):
    monkeypatch.setattr(router_module, "_today", lambda: TODAY)
    # No ZDR demand unless a test asks for it.
    monkeypatch.setattr(router_module, "_zdr_required", lambda: False)


# ── policy validation + setting ──────────────────────────────────────────────
def test_policy_validation():
    for ok in (None, "prefer", "require", "any"):
        body = _policy()
        if ok is not None:
            body["open_weights"] = ok
        validate_policy(body)
    for bad in ("always", "", 3, True):
        with pytest.raises(HTTPException) as exc:
            validate_policy(_policy(open_weights=bad))
        assert exc.value.status_code == 422


def test_setting_default_and_validation(monkeypatch):
    assert Settings().router_open_weights == "prefer"
    assert Settings(router_open_weights="Require").router_open_weights == "require"
    with pytest.raises(ValueError, match="prefer\\|require\\|any"):
        Settings(router_open_weights="bogus")
    monkeypatch.setenv("TRET_ROUTER_OPEN_WEIGHTS", "sometimes")
    with pytest.raises(ValueError, match="prefer\\|require\\|any"):
        Settings()
    monkeypatch.setenv("TRET_ROUTER_OPEN_WEIGHTS", "any")
    assert Settings().router_open_weights == "any"
    assert Settings().router_new_model_slots == 6
    assert Settings().router_new_model_window_days == 120


def test_policy_beats_setting(monkeypatch):
    monkeypatch.setattr(router_module, "get_settings", lambda: Settings(router_open_weights="require"))
    assert router_module._open_weights_policy({}) == "require"
    assert router_module._open_weights_policy({"open_weights": "any"}) == "any"


# ── require ──────────────────────────────────────────────────────────────────
def test_require_excludes_closed_and_unknown_and_counts_them():
    openm = _m("open-a", open_weights=True)
    closed = _m("closed-a", open_weights=False)
    unknown = _m("unknown-a", open_weights=None)
    local = _m("loc", provider="local", open_weights=True, tier="local", curated=False)
    router = ModelRouter(_catalog([openm, closed, unknown, local]), _Registry(["openrouter", "local"]))
    assert set(_ids(router, _policy(open_weights="require"))) == {openm.id, local.id}
    assert set(_ids(router, _policy(open_weights="any"))) == {openm.id, closed.id, unknown.id, local.id}


def test_require_with_nothing_open_explains_itself():
    router = ModelRouter(_catalog([_m("c1", open_weights=False)]), _Registry())
    detail = router._no_candidates_detail(_policy(open_weights="require"))
    assert "1 closed-weight" in detail


async def test_require_unroutable_raises():
    router = ModelRouter(_catalog([_m("c1", open_weights=False)]), _Registry())
    with pytest.raises(RoutingUnavailable) as exc:
        await router.route(
            model_policy=_policy(open_weights="require"), task_type="t", task_shape="freeform",
            task_description="d", output_contract="o", n_documents=0, est_input_tokens=10,
        )
    assert "closed-weight" in str(exc.value)


# ── prefer ordering ──────────────────────────────────────────────────────────
def test_prefer_orders_open_before_closed_without_priors():
    cheap_closed = _m("cheap-closed", price="1", open_weights=False)
    dear_open = _m("dear-open", price="9", open_weights=True)
    mid_unknown = _m("mid-unknown", price="5", open_weights=None)
    router = ModelRouter(_catalog([cheap_closed, dear_open, mid_unknown]), _Registry())
    assert _ids(router, _policy(open_weights="prefer")) == [
        dear_open.id, cheap_closed.id, mid_unknown.id,
    ]
    assert _ids(router, _policy(open_weights="any")) == [
        cheap_closed.id, mid_unknown.id, dear_open.id,
    ]


def test_prefer_keeps_evidence_in_front_of_openness():
    proven_closed = _m("proven-closed", price="9", open_weights=False)
    open_untried = _m("open-untried", price="1", open_weights=True)
    router = ModelRouter(_catalog([proven_closed, open_untried]), _Registry())
    priors = {proven_closed.id: _prior(proven_closed.id, 0.9, floor=0.8)}
    assert _ids(router, _policy(open_weights="prefer"), priors) == [proven_closed.id, open_untried.id]
    # Among equal evidence, open still beats closed.
    priors = {
        proven_closed.id: _prior(proven_closed.id, 0.9, floor=0.8),
        open_untried.id: _prior(open_untried.id, 0.9, floor=0.8),
    }
    assert _ids(router, _policy(open_weights="prefer"), priors)[0] == open_untried.id


def test_any_is_byte_for_byte_the_objective_key():
    models = [
        _m("a", price="3", open_weights=True), _m("b", price="1", open_weights=False),
        _m("c", price="2", open_weights=None), _m("d", price="2", open_weights=True),
    ]
    for objective in ("balanced", "quality", "eco", "token_conservation"):
        base = sorted(models, key=candidate_sort_key(objective))
        assert sorted(models, key=candidate_sort_key(objective, None, "any")) == base
        priors = {"openrouter/a": _prior("openrouter/a", 0.9, floor=0.8)}
        assert sorted(models, key=candidate_sort_key(objective, priors, "any")) == sorted(
            models, key=candidate_sort_key(objective, priors)
        )


# ── ZDR ─────────────────────────────────────────────────────────────────────
def test_zdr_exclusion(monkeypatch):
    monkeypatch.setattr(router_module, "_zdr_required", lambda: True)
    yes = _m("yes", zdr=True)
    no = _m("no", zdr=False)
    unknown = _m("unknown", zdr=None)
    direct = _m("direct", provider="anthropic", zdr=False)  # not an OpenRouter model
    router = ModelRouter(_catalog([yes, no, unknown, direct]), _Registry())
    assert set(_ids(router, _policy())) == {yes.id, unknown.id, direct.id}
    assert "1 lacked a zero-data-retention endpoint" in router._no_candidates_detail(_policy())


def test_zdr_not_required_excludes_nothing():
    no = _m("no", zdr=False)
    router = ModelRouter(_catalog([no]), _Registry())
    assert _ids(router, _policy()) == [no.id]


def test_zdr_required_reads_effective_provider_prefs(monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr(router_module, "get_settings", lambda: Settings(openrouter_provider_prefs={"zdr": True}))
    assert router_module._zdr_required() is True
    monkeypatch.setattr(router_module, "get_settings", lambda: Settings(openrouter_provider_prefs={}))
    assert router_module._zdr_required() is False


async def test_override_is_not_excluded_by_zdr(monkeypatch):
    monkeypatch.setattr(router_module, "_zdr_required", lambda: True)
    no = _m("no", zdr=False)
    router = ModelRouter(_catalog([no]), _Registry())
    decision = await router.route(
        model_policy=_policy(), run_override=no.id, task_type="t", task_shape="freeform",
        task_description="d", output_contract="o", n_documents=0, est_input_tokens=10,
    )
    assert decision.chosen_model == no.id and decision.override == "run_override"
    assert decision.open_weights is None
    pinned = await router.route(
        model_policy={"mode": "pinned", "model": no.id}, task_type="t", task_shape="freeform",
        task_description="d", output_contract="o", n_documents=0, est_input_tokens=10,
    )
    assert pinned.chosen_model == no.id


# ── prompt ───────────────────────────────────────────────────────────────────
def _prompt(models, open_weights):
    return render_router_prompt(
        task_type="t", task_shape="freeform", task_description="d", output_contract="o",
        n_documents=0, est_input_tokens=10, max_cost_tier="premium", candidates=models,
        open_weights=open_weights,
    )


def test_prompt_weights_lines():
    models = [_m("o", open_weights=True), _m("c", open_weights=False), _m("u", open_weights=None)]
    for mode in ("prefer", "require"):
        lines = _prompt(models, mode).splitlines()
        assert "weights: open" in next(ln for ln in lines if "openrouter/o " in ln)
        assert "weights: closed" in next(ln for ln in lines if "openrouter/c " in ln)
        assert "weights:" not in next(ln for ln in lines if "openrouter/u " in ln)
    assert "OPEN WEIGHTS: preferred. Choose an open-weight candidate whenever one can do the job; " \
        "choose a closed model only when the task clearly needs it, and say why." in _prompt(models, "prefer")
    assert "OPEN WEIGHTS: required; every candidate is open-weight." in _prompt(models, "require")
    plain = _prompt(models, "any")
    assert "weights" not in plain and "OPEN WEIGHTS" not in plain
    assert _prompt(models, "any") == render_router_prompt(
        task_type="t", task_shape="freeform", task_description="d", output_contract="o",
        n_documents=0, est_input_tokens=10, max_cost_tier="premium", candidates=models,
    )


# ── decision field ───────────────────────────────────────────────────────────
async def test_decision_records_the_applied_value():
    a, b = _m("a"), _m("b", price="2")
    # Seeded rng and no outcome store: nothing here can roll an exploration pick.
    router = ModelRouter(_catalog([a, b]), _Registry(), rng=random.Random(0))
    router._resolve_router_model = lambda max_tier: None  # deterministic fallback path
    for value in ("prefer", "any"):
        decision = await router.route(
            model_policy=_policy(open_weights=value), task_type="t", task_shape="freeform",
            task_description="d", output_contract="o", n_documents=0, est_input_tokens=10,
        )
        assert decision.fallback_used and decision.open_weights == value
    single = ModelRouter(_catalog([a]), _Registry())
    d = await single.route(
        model_policy=_policy(open_weights="prefer"), task_type="t", task_shape="freeform",
        task_description="d", output_contract="o", n_documents=0, est_input_tokens=10,
    )
    assert d.open_weights == "prefer"


# ── fallback ─────────────────────────────────────────────────────────────────
def test_fallback_respects_require_prefer_and_zdr():
    closed = _m("closed", price="1", open_weights=False)
    openm = _m("openm", price="5", open_weights=True)
    cat = _catalog([closed, openm])
    reg = _Registry()
    kw = dict(objective="eco")
    assert fallback_model("freeform", cat, reg, **kw, open_weights="any") == closed.id
    assert fallback_model("freeform", cat, reg, **kw, open_weights="prefer") == openm.id
    assert fallback_model("freeform", cat, reg, **kw, open_weights="require") == openm.id
    assert fallback_model("freeform", _catalog([closed]), reg, **kw, open_weights="require") is None
    # prefer with nothing open leaves the pool alone.
    assert fallback_model("freeform", _catalog([closed]), reg, **kw, open_weights="prefer") == closed.id
    nozdr = _m("nozdr", zdr=False)
    assert fallback_model("freeform", _catalog([nozdr]), reg, require_zdr=True) is None
    assert fallback_model("freeform", _catalog([nozdr]), reg, require_zdr=False) == nozdr.id


# ── uncurated models: pool gate ──────────────────────────────────────────────
def test_uncurated_enters_the_pool_only_with_a_non_poor_prior():
    cur = [_m(f"c{i}", price=str(i + 1)) for i in range(3)]
    plain = _m("lab/plain", curated=False, price="0.1", released="2020-01")
    earned = _m("lab/earned", curated=False, price="0.2", released="2020-01")
    poor = _m("lab/poor", curated=False, price="0.3", released="2020-01")
    router = ModelRouter(_catalog(cur, [plain, earned, poor]), _Registry())
    assert _ids(router, _policy(open_weights="any")) == [m.id for m in cur]
    priors = {earned.id: _prior(earned.id, 0.6, 0.5), poor.id: _prior(poor.id, 0.1, 0.05)}
    ids = _ids(router, _policy(open_weights="any"), priors)
    assert earned.id in ids and plain.id not in ids and poor.id not in ids


def test_hundreds_of_cheap_uncurated_models_cannot_flood_eco():
    cur = [_m(f"c{i}", price=str(i + 10)) for i in range(25)]
    flood = [_m(f"lab{i}/tiny", curated=False, price="0.01", released="2020-01") for i in range(300)]
    router = ModelRouter(_catalog(cur, flood), _Registry())
    ids = _ids(router, _policy(objective="eco", open_weights="any"))
    assert len(ids) == CANDIDATE_LIMIT and all(i.startswith("openrouter/c") for i in ids)


# ── seats ────────────────────────────────────────────────────────────────────
def _seat_ids(router, policy, priors=None):
    return [m.id for m in router._candidates(policy, priors) if not m.curated]


def test_seats_drop_specialists_and_free_variants():
    names = ["lab/model-coder", "lab/model-vl-8b", "lab/vision-x", "lab/OCR-pro", "lab/Guard-1",
             "lab/embed-2", "lab/math-3", "lab/rerank-1", "lab/omni-4", "lab/tts-5",
             "lab/speech-6", "lab/image-7", "lab/audio-8", "lab/legal-9", "lab/code-10"]
    specs = [_m(n, curated=False) for n in names]
    free = _m("lab2/general:free", curated=False)
    ok = _m("lab3/general", curated=False)
    router = ModelRouter(_catalog([_m("c0", price="9")], specs + [free, ok]), _Registry())
    assert _seat_ids(router, _policy(open_weights="any")) == [ok.id]


def test_one_seat_per_author_newest_then_higher_price():
    old = _m("lab/old", curated=False, created_ts=1_000, released="2026-09")
    new_small = _m("lab/new-small", curated=False, created_ts=2_000, released="2026-10", price="1")
    new_big = _m("lab/new-big", curated=False, created_ts=2_000, released="2026-10", price="8")
    other = _m("other/x", curated=False, created_ts=500, released="2026-09")
    router = ModelRouter(_catalog([_m("c0")], [old, new_small, new_big, other]), _Registry())
    assert _seat_ids(router, _policy(open_weights="any")) == [new_big.id, other.id]


def test_created_ts_beats_released_month_for_newest():
    a = _m("lab/a", curated=False, created_ts=1_762_000_000, released="2026-11")
    b = _m("lab2/b", curated=False, created_ts=1_762_500_000, released="2026-11")
    router = ModelRouter(_catalog([_m("c0")], [a, b]), _Registry())
    assert _seat_ids(router, _policy(open_weights="any")) == [b.id, a.id]


def test_open_weight_seats_come_first_under_prefer_and_slots_cap(monkeypatch):
    closed_new = _m("zlab/closed", curated=False, created_ts=3_000, open_weights=False)
    open_old = _m("alab/open", curated=False, created_ts=1_000, open_weights=True)
    cur = [_m(f"c{i}", price=str(i + 1)) for i in range(25)]
    router = ModelRouter(_catalog(cur, [closed_new, open_old]), _Registry())
    ids = _ids(router, _policy(open_weights="prefer"))
    assert len(ids) == CANDIDATE_LIMIT
    assert ids[-2:] == [open_old.id, closed_new.id]
    ids_any = _ids(router, _policy(open_weights="any"))
    assert ids_any[-2:] == [closed_new.id, open_old.id]
    # Slot count caps the reservation.
    monkeypatch.setattr(router_module, "get_settings", lambda: Settings(router_new_model_slots=1))
    assert [i for i in _ids(router, _policy(open_weights="prefer")) if "lab/" in i] == [open_old.id]
    # Require drops closed ones entirely.
    assert closed_new.id not in _ids(router, _policy(open_weights="require"))


def test_seats_respect_window_and_poor_prior():
    stale = _m("lab/stale", curated=False, released="2025-01")
    poor = _m("lab2/poor", curated=False, released="2026-10")
    router = ModelRouter(_catalog([_m("c0")], [stale, poor]), _Registry())
    assert _seat_ids(router, _policy(open_weights="any"), {poor.id: _prior(poor.id, 0.1, 0.05)}) == []


# ── exploration ──────────────────────────────────────────────────────────────
class _Stub:
    def __init__(self, priors):
        self.priors = priors

    async def for_key(self, *, task_shape, objective, size_band=None):
        return self.priors

    def invalidate(self):
        pass


async def _explore(models, policy, *, shape="freeform", priors=None):
    router = ModelRouter(_catalog(models), _Registry(), _Stub(priors or {}), rng=random.Random(1))
    return await router.route(
        model_policy=policy, task_type="t", task_shape=shape, task_description="d",
        output_contract="o", n_documents=0, est_input_tokens=10,
    )


@pytest.mark.parametrize("objective", ["balanced", "eco"])
@pytest.mark.parametrize("shape", ["extraction", "freeform"])
async def test_exploration_runs_for_widened_gates(objective, shape):
    d = await _explore(
        [_m("a", price="1"), _m("b", price="2")],
        _policy(objective=objective, open_weights="any", adaptive={"exploration": MAX_EXPLORATION}),
        shape=shape,
    )
    assert d.exploration["explored"] is True and d.chosen_model == "openrouter/a"


@pytest.mark.parametrize("objective,shape", [("quality", "freeform"), ("token_conservation", "extraction"),
                                              ("balanced", "drafting"), ("eco", "verdict")])
async def test_exploration_off_outside_gates(objective, shape):
    router = ModelRouter(_catalog([_m("a")]), _Registry())
    from tret.adaptive import adaptive_of

    reason = router._exploration_off_reason(
        objective, shape, adaptive_of({"adaptive": {"exploration": MAX_EXPLORATION}})
    )
    assert reason is not None


async def test_exploration_when_floor_is_above_the_exploration_cap():
    std_a = _m("std-a", tier="standard", price="6")
    std_b = _m("std-b", tier="standard", price="7")
    eco = _m("eco-x", tier="economy", price="1")
    policy = _policy(
        open_weights="any", min_cost_tier="standard",
        adaptive={"exploration": MAX_EXPLORATION, "exploration_max_cost_tier": "economy"},
    )
    d = await _explore([std_a, std_b, eco], policy)
    assert d.exploration["explored"] is True
    assert d.chosen_model == std_a.id  # cheapest within the policy's own (standard) band


async def test_exploration_prefers_open_weights_under_prefer():
    cheap_closed = _m("cheap-closed", price="1", open_weights=False)
    dear_open = _m("dear-open", price="3", open_weights=True)
    base = dict(adaptive={"exploration": MAX_EXPLORATION})
    d = await _explore([cheap_closed, dear_open], _policy(open_weights="prefer", **base))
    assert d.chosen_model == dear_open.id
    d = await _explore([cheap_closed, dear_open], _policy(open_weights="any", **base))
    assert d.chosen_model == cheap_closed.id


# ── review fixes (2026-11-06) ────────────────────────────────────────────────
def _windowed(m: ModelInfo, window: int) -> ModelInfo:
    from dataclasses import replace

    return replace(m, context_window=window)


def _fit(router: ModelRouter, policy: dict, min_window: int, priors=None):
    cands, fit = router._candidates_with_fit(policy, priors, min_window)
    return [m.id for m in cands], fit


def test_seats_are_held_to_the_context_window():
    big = _windowed(_m("big"), 1_000_000)
    small_seat = _windowed(_m("small-seat", curated=False, released="2026-10"), 32_000)
    big_seat = _windowed(_m("big-seat", curated=False, released="2026-10"), 1_000_000)
    router = ModelRouter(_catalog([big], [small_seat, big_seat]), _Registry())
    ids, fit = _fit(router, _policy(), 200_000)
    assert small_seat.id not in ids and big_seat.id in ids and big.id in ids
    assert small_seat.id in fit["excluded"]  # exploration reads this


def test_seat_that_fits_survives_when_the_earned_pool_cannot_fit():
    small = _windowed(_m("small"), 32_000)
    big_seat = _windowed(_m("big-seat", curated=False, released="2026-10"), 1_000_000)
    router = ModelRouter(_catalog([small], [big_seat]), _Registry())
    ids, fit = _fit(router, _policy(), 200_000)
    assert ids == [big_seat.id]
    assert fit["mode"] == "fit" and small.id in fit["excluded"]


def test_best_effort_only_when_nothing_anywhere_fits():
    small = _windowed(_m("small"), 32_000)
    small_seat = _windowed(_m("small-seat", curated=False, released="2026-10"), 16_000)
    router = ModelRouter(_catalog([small], [small_seat]), _Registry())
    ids, fit = _fit(router, _policy(), 200_000)
    assert fit["mode"] == "best_effort" and ids == [small.id]
    assert small_seat.id in fit["excluded"]


def test_allowed_listing_only_uncurated_models_still_routes_under_a_window():
    seat = _windowed(_m("only-one", curated=False, released="2026-10"), 1_000_000)
    router = ModelRouter(_catalog([], [seat]), _Registry())
    ids, _ = _fit(router, _policy(allowed=[seat.id]), 200_000)
    assert ids == [seat.id]


def test_no_outcome_store_means_no_exploration():
    from types import SimpleNamespace

    adaptive = SimpleNamespace(learn_from_outcomes=True, exploration=MAX_EXPLORATION)
    router = ModelRouter(_catalog([_m("a")]), _Registry())  # NoPriors
    reason = router._exploration_off_reason("balanced", "freeform", adaptive)
    assert reason and "no outcome store" in reason
    recording = ModelRouter(_catalog([_m("a")]), _Registry(), _Stub({}))
    assert recording._exploration_off_reason("balanced", "freeform", adaptive) is None


def test_fallback_drops_proven_poor_before_preferring_open():
    poor_open = _m("poor-open", price="1", open_weights=True)
    closed = _m("closed", price="2", open_weights=False)
    priors = {poor_open.id: _prior(poor_open.id, 0.1)}
    pick = fallback_model(
        "freeform", _catalog([poor_open, closed]), _Registry(),
        objective="eco", priors=priors, open_weights="prefer",
    )
    assert pick == closed.id


def test_fallback_prefer_open_does_not_beat_better_evidence():
    proven_closed = _m("proven-closed", price="5", open_weights=False)
    unknown_open = _m("unknown-open", price="1", open_weights=True)
    priors = {proven_closed.id: _prior(proven_closed.id, 0.95)}
    cat = _catalog([proven_closed, unknown_open])
    kw = dict(objective="eco", priors=priors)
    assert fallback_model("freeform", cat, _Registry(), **kw, open_weights="prefer") == proven_closed.id
    # Same evidence tier: open still wins.
    tied = {proven_closed.id: _prior(proven_closed.id, 0.95), unknown_open.id: _prior(unknown_open.id, 0.95)}
    assert (
        fallback_model("freeform", cat, _Registry(), objective="eco", priors=tied, open_weights="prefer")
        == unknown_open.id
    )


def test_operator_named_uncurated_model_routes_without_a_record():
    named = _m("llama-x", curated=False, open_weights=True)
    other = _m("other-x", curated=False, open_weights=True)
    router = ModelRouter(_catalog([], [named, other]), _Registry())
    assert _ids(router, _policy(allowed=[named.id])) == [named.id]
    pick = fallback_model("freeform", _catalog([], [named, other]), _Registry(), allowed=[named.id])
    assert pick == named.id


def test_earned_gate_alone_never_empties_the_pool():
    only_uncurated = [_m("u1", curated=False), _m("u2", curated=False)]
    router = ModelRouter(_catalog([], only_uncurated), _Registry())
    assert set(_ids(router, _policy())) == {m.id for m in only_uncurated}


def test_fallback_prefer_open_does_not_pick_an_open_model_that_cannot_fit():
    from dataclasses import replace

    small_open = replace(_m("small-open", open_weights=True), context_window=128_000)
    big_closed = replace(_m("big-closed", open_weights=False), context_window=1_000_000)
    cat = _catalog([small_open, big_closed])
    pick = fallback_model(
        "freeform", cat, _Registry(), objective="eco",
        open_weights="prefer", min_context_window=300_000,
    )
    assert pick == big_closed.id
    # Both fit: open is preferred again.
    assert fallback_model(
        "freeform", cat, _Registry(), objective="eco",
        open_weights="prefer", min_context_window=100_000,
    ) == small_open.id
    # Nothing fits: best effort, largest window.
    assert fallback_model(
        "freeform", cat, _Registry(), objective="eco",
        open_weights="prefer", min_context_window=5_000_000,
    ) == big_closed.id
