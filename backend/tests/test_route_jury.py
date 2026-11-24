"""ModelRouter.route_jury: one model per family, deterministic, policy-bound, and never a call."""
from __future__ import annotations

from decimal import Decimal

import pytest

from tret.config import get_settings
from tret.engine.routed_call import RoutedCallError
from tret.providers.catalog import ModelCatalog, ModelInfo
from tret.router_llm.jury_select import JurySelection
from tret.router_llm.priors_base import ModelPrior
from tret.router_llm.router import JuryUnavailable, ModelRouter, RoutingUnavailable


class Registry:
    def __init__(self, provider=None, providers=("openrouter",)):
        self.provider = provider
        self.providers = set(providers)

    def has_key(self, p):
        return p in self.providers

    def get(self, name):
        return self.provider


def model(wire, *, price="1", tier="economy", window=100_000, open_weights=None, curated=True):
    return ModelInfo(
        id=f"openrouter/{wire}", provider="openrouter", wire_id=wire, display_name=wire,
        context_window=window, input_price_per_mtok=Decimal(price), output_price_per_mtok=Decimal(price),
        cost_tier=tier, supports_tools=True, curated=curated, open_weights=open_weights,
    )


def catalog(*models):
    c = ModelCatalog()
    c._static = {m.id: m for m in models}
    return c


FIVE = (
    model("openai/gpt-a", price="1"),
    model("openai/gpt-b", price="2"),
    model("google/gem-a", price="3"),
    model("meta-llama/llama-a", price="4", open_weights=True),
    model("x-ai/grok-a", price="5"),
)


def make(*models, priors=None, registry=None):
    return ModelRouter(catalog(*models), registry or Registry(), priors)


@pytest.fixture(autouse=True)
def no_router_call(monkeypatch):
    monkeypatch.setattr(get_settings(), "router_open_weights", "any")
    """Anything that would route or explore, or resolve a router model, fails the test."""
    def boom(*a, **k):
        raise AssertionError("route_jury must not route, explore or call a router model")

    monkeypatch.setattr(ModelRouter, "_route", boom)
    monkeypatch.setattr(ModelRouter, "route", boom)
    monkeypatch.setattr(ModelRouter, "_maybe_explore", boom)
    monkeypatch.setattr(ModelRouter, "_resolve_router_model", boom)


async def test_one_model_per_family_best_ranked_first():
    sel = await make(*FIVE).route_jury({"mode": "auto"}, 3)
    assert isinstance(sel, JurySelection)
    assert sel.models == ["openrouter/openai/gpt-a", "openrouter/google/gem-a", "openrouter/meta-llama/llama-a"]
    assert [p.family for p in sel.picks] == ["openai", "google", "meta"]
    assert [p.rank for p in sel.picks] == [1, 3, 4]
    assert sel.llm_call is False and sel.exploration == "off" and sel.ranking_version
    p0 = sel.picks[0]
    assert "Ranked 1 of 5" in p0.why and "'openai' family" in p0.why and p0.alternatives == [{"model": "openrouter/openai/gpt-b", "rank": 2}]
    assert [s["family"] for s in sel.skipped_families] == ["xai"]
    assert sel.families_eligible == 4 and sel.eligible == 5 and sel.created_at
    assert JurySelection.from_json(sel.to_json()).models == sel.models


async def test_ties_break_by_model_id_whatever_the_catalog_order():
    a = [model("a/m1"), model("b/m2"), model("c/m3")]
    first = (await make(*a).route_jury({}, 3)).models
    second = (await make(*reversed(a)).route_jury({}, 3)).models
    assert first == second == ["openrouter/a/m1", "openrouter/b/m2", "openrouter/c/m3"]


async def test_same_input_same_selection():
    r = make(*FIVE)
    one, two = await r.route_jury({"objective": "eco"}, 3), await r.route_jury({"objective": "eco"}, 3)
    assert one.models == two.models and [p.why for p in one.picks] == [p.why for p in two.picks]


async def test_too_few_families_says_how_many_and_why_not_the_rest():
    ms = (*FIVE[:2], model("deep/big", tier="premium", price="50"))
    with pytest.raises(JuryUnavailable) as e:
        await make(*ms).route_jury({"max_cost_tier": "economy"}, 2)
    text = str(e.value)
    assert isinstance(e.value, RoutingUnavailable)
    assert "Only 1 distinct model family eligible" in text and "2 needed" in text and "openai: 2" in text
    assert "1 above the policy's economy cost tier" in text


async def test_repeats_allowed_takes_the_top_k_even_from_one_family():
    sel = await make(*FIVE).route_jury({}, 2, distinct_authors=False)
    assert sel.models == ["openrouter/openai/gpt-a", "openrouter/openai/gpt-b"]
    assert sel.picks[1].alternatives == [{"model": "openrouter/openai/gpt-a", "rank": 1}]
    with pytest.raises(JuryUnavailable):
        await make(FIVE[0]).route_jury({}, 2, distinct_authors=False)


@pytest.mark.parametrize("policy", [{"mode": "pinned", "model": "x/y"}, {"model": "x/y"}])
async def test_pinned_policy_is_refused(policy):
    with pytest.raises(RoutedCallError) as e:
        await make(*FIVE).route_jury(policy, 1)
    assert e.value.kind == "pinned_policy"


async def test_policy_ceiling_allowed_list_and_open_weights_are_respected():
    ms = (*FIVE, model("anthropic/big", tier="premium", price="30"))
    sel = await make(*ms).route_jury({"max_cost_tier": "standard"}, 4)
    assert "openrouter/anthropic/big" not in sel.models
    only = await make(*ms).route_jury({"allowed": ["openrouter/google/gem-a", "openrouter/x-ai/grok-a"]}, 2)
    assert sorted(only.models) == ["openrouter/google/gem-a", "openrouter/x-ai/grok-a"]
    with pytest.raises(JuryUnavailable):
        await make(*FIVE).route_jury({"open_weights": "require"}, 2)
    pref = await make(*FIVE).route_jury({"open_weights": "prefer"}, 1)
    assert pref.models == ["openrouter/meta-llama/llama-a"] and "prefers open weights" in pref.picks[0].why


async def test_context_window_excludes_small_models_and_says_so():
    ms = (model("a/small", window=4000), model("b/big", window=200_000), model("c/big", window=200_000))
    sel = await make(*ms).route_jury({}, 2, min_context_window=50_000)
    assert sel.models == ["openrouter/b/big", "openrouter/c/big"]
    assert sel.excluded_models[0]["model"] == "openrouter/a/small" and "context window" in sel.excluded_models[0]["reason"]
    with pytest.raises(JuryUnavailable):
        await make(*ms).route_jury({}, 3, min_context_window=50_000)
    with pytest.raises(JuryUnavailable):  # nothing fits at all: no best-effort juror
        await make(*ms).route_jury({}, 1, min_context_window=900_000)


async def test_missing_provider_key_excludes_models():
    with pytest.raises(JuryUnavailable) as e:
        await make(*FIVE, registry=Registry(providers=())).route_jury({}, 1)
    assert "lacked a provider key" in str(e.value)


class Priors:
    def __init__(self, table, cooldown=()):
        self.table, self.cool = table, list(cooldown)

    async def for_key(self, **kw):
        return self.table

    def invalidate(self, *a, **k):
        pass

    async def cooldown_for(self, ids):
        return [c for c in self.cool if c["model"] in ids]


def prior(mid, quality=0.9, runs=40):
    return ModelPrior(
        model_id=mid, runs=runs, effective_n=runs, quality_mean=quality, quality_raw=quality,
        quality_ci_low=quality - 0.05, delivered_rate=1.0, failure_rate=0.0, mean_cost_usd=0.01,
        mean_output_tokens=100, mean_iterations=2, mean_energy_wh=None, approvals=runs, rejections=0,
    )


async def test_evidence_leads_and_is_recorded_on_the_pick():
    pri = Priors({"openrouter/x-ai/grok-a": prior("openrouter/x-ai/grok-a")})
    sel = await make(*FIVE, priors=pri).route_jury({}, 2)
    assert sel.picks[0].model == "openrouter/x-ai/grok-a" and sel.picks[0].rank == 1
    assert sel.picks[0].evidence["runs"] == 40 and "40 recorded run" in sel.picks[0].why
    assert sel.picks[1].evidence == {"tier": sel.picks[1].evidence["tier"], "runs": 0}
    assert "no recorded runs" in sel.picks[1].why


async def test_cooling_models_sit_out_and_are_listed():
    pri = Priors({}, cooldown=[{"model": "openrouter/openai/gpt-a", "until": "x", "reason": "http_429"}])
    sel = await make(*FIVE, priors=pri).route_jury({}, 3)
    assert sel.models[0] == "openrouter/openai/gpt-b"
    assert any(e["model"] == "openrouter/openai/gpt-a" and "cooling down" in e["reason"] for e in sel.excluded_models)


async def test_rejects_bad_k():
    with pytest.raises(ValueError):
        await make(*FIVE).route_jury({}, 0)


def local(name, window):
    return ModelInfo(
        id=f"local/{name}", provider="local", wire_id=name, display_name=name, context_window=window,
        input_price_per_mtok=Decimal(0), output_price_per_mtok=Decimal(0), cost_tier="local",
        supports_tools=True, curated=True,
    )


async def test_local_models_must_be_known_to_hold_the_prompt():
    ms = (local("llama-small", 8000), local("qwen-unknown", 0), local("mistral-big", 200_000))
    r = ModelRouter(catalog(*ms), Registry(providers=("local",)))
    sel = await r.route_jury({}, 1, min_context_window=100_000)
    assert sel.models == ["local/mistral-big"]
    reasons = {e["model"]: e["reason"] for e in sel.excluded_models}
    assert "8000 below" in reasons["local/llama-small"] and "unknown context window" in reasons["local/qwen-unknown"]
    with pytest.raises(JuryUnavailable):
        await r.route_jury({}, 2, min_context_window=100_000)
    # no window requirement: local models are as eligible as before
    assert len((await r.route_jury({}, 3)).picks) == 3


async def test_size_band_comes_from_the_window_when_no_prompt_size_is_known():
    seen = []

    class Spy(Priors):
        async def for_key(self, **kw):
            seen.append(kw["size_band"])
            return {}

    from tret.router_llm.outcomes import size_band

    await make(*FIVE, priors=Spy({})).route_jury({}, 1, min_context_window=90_000)
    await make(*FIVE, priors=Spy({})).route_jury({}, 1)
    assert seen[0] == size_band(90_000) and seen[1] == size_band(0)
