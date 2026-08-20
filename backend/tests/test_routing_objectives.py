"""Routing objectives: validation, candidate ordering, prompt rules, fallback.

Offline: the real curated catalog is used with a fake provider registry, and the
LLM router is never reachable (no key for the router model), so `route()` lands on
the deterministic fallback — which is exactly the path an objective has to steer
when the router is unavailable.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi import HTTPException

from bench.api.harnesses import _validate_policy
from bench.providers.catalog import ModelCatalog, ModelInfo, ProviderRegistry
from bench.engine.harness import effective_model_policy
from bench.router_llm.fallback import fallback_model
from bench.router_llm.objectives import (
    DEFAULT_OBJECTIVE,
    OBJECTIVES,
    THRIFT_OBJECTIVES,
    candidate_sort_key,
    objective_of,
    released_rank,
)
from bench.router_llm.prompts import (
    OBJECTIVE_RULES,
    ROUTING_PROMPT_VERSION,
    render_router_prompt,
)
from bench.router_llm.router import ModelRouter, RoutingDecision

ECO_MODEL = "openrouter/google/gemini-3.5-flash-lite"  # cheapest energy in the catalog
THRIFT_MODEL = "openrouter/deepseek/deepseek-v4-pro"  # cheapest output tokens


class _Registry(ProviderRegistry):
    def __init__(self, providers: set[str]):
        self._providers = providers

    def has_key(self, provider: str) -> bool:
        return provider in self._providers


def _policy(**over) -> dict:
    base = {"mode": "auto", "max_cost_tier": "premium"}
    base.update(over)
    return base


def _all_keys() -> _Registry:
    return _Registry({"anthropic", "kimi", "openrouter"})


def _candidates(objective: str | None = None, catalog: ModelCatalog | None = None) -> list[str]:
    policy = _policy() if objective is None else _policy(objective=objective)
    router = ModelRouter(catalog or ModelCatalog(), _all_keys())
    return [m.id for m in router._candidates(policy)]


def _local_model(**over) -> ModelInfo:
    base = dict(
        id="local/qwen2.5:14b-instruct",
        provider="local",
        wire_id="qwen2.5:14b-instruct",
        display_name="Local: qwen2.5:14b-instruct",
        context_window=32768,
        input_price_per_mtok=Decimal("0"),
        output_price_per_mtok=Decimal("0"),
        cost_tier="local",
        curated=False,
        energy_class="S",
    )
    base.update(over)
    return ModelInfo(**base)


# ── the objective vocabulary ──────────────────────────────────────────────────
def test_objectives_and_default():
    assert OBJECTIVES == ("quality", "balanced", "token_conservation", "eco")
    assert DEFAULT_OBJECTIVE == "balanced"
    assert set(THRIFT_OBJECTIVES) == {"token_conservation", "eco"}


def test_objective_of_defaults_but_never_invents():
    assert objective_of(None) == "balanced"
    assert objective_of({}) == "balanced"
    assert objective_of({"objective": None}) == "balanced"
    assert objective_of({"objective": "eco"}) == "eco"
    # Unknown values are the API's job to reject; the engine must not act on them.
    assert objective_of({"objective": "ecological"}) == "balanced"


def test_released_rank_puts_newer_first_and_unknown_last():
    newer = ModelCatalog().get("anthropic/claude-sonnet-5")  # 2026-06
    older = ModelCatalog().get("anthropic/claude-haiku-4-5")  # 2025-10
    assert released_rank(newer) < released_rank(older) < released_rank(_local_model())


# ── validation ────────────────────────────────────────────────────────────────
def test_valid_objectives_are_accepted():
    for objective in OBJECTIVES:
        _validate_policy({"mode": "auto", "objective": objective})


def test_unset_objective_is_the_documented_default():
    _validate_policy({"mode": "auto"})
    _validate_policy({"mode": "auto", "objective": None})


def test_an_unknown_objective_is_rejected_rather_than_defaulted():
    with pytest.raises(HTTPException) as exc:
        _validate_policy({"mode": "auto", "objective": "ecological"})
    assert exc.value.status_code == 422
    assert "objective" in exc.value.detail
    assert "eco" in exc.value.detail  # the message lists the allowed values


def test_objective_validation_is_independent_of_the_other_policy_fields():
    with pytest.raises(HTTPException):
        _validate_policy({"mode": "auto", "max_cost_tier": "economy", "objective": "cheapest"})


# ── candidate ordering ────────────────────────────────────────────────────────
def test_balanced_ordering_is_unchanged_from_the_historical_default():
    catalog = ModelCatalog()
    historical = [
        m.id
        for m in sorted(
            catalog.all(curated_only=True), key=lambda m: (not m.curated, m.output_price_per_mtok)
        )
    ]
    assert _candidates("balanced") == historical
    assert _candidates(None) == historical  # unset policy routes exactly as before


def test_eco_orders_by_estimated_energy_then_price():
    catalog = ModelCatalog()
    ids = _candidates("eco", catalog)
    energies = [catalog.get(i).energy_wh_per_mtok for i in ids]
    assert energies == sorted(energies)
    assert ids[0] == ECO_MODEL
    assert ids[-1] in ("anthropic/claude-fable-5", "anthropic/claude-opus-4-8")
    # Within one energy class, the cheaper model comes first.
    same_class = [i for i in ids if catalog.get(i).energy_class == "M"]
    prices = [catalog.get(i).output_price_per_mtok for i in same_class]
    assert prices == sorted(prices)


def test_token_conservation_orders_by_output_price():
    catalog = ModelCatalog()
    ids = _candidates("token_conservation", catalog)
    prices = [catalog.get(i).output_price_per_mtok for i in ids]
    assert prices == sorted(prices)
    assert ids[0] == THRIFT_MODEL


def test_quality_orders_most_capable_first_within_the_tier_cap():
    catalog = ModelCatalog()
    ids = _candidates("quality", catalog)
    prices = [catalog.get(i).output_price_per_mtok for i in ids]
    assert prices == sorted(prices, reverse=True)
    assert ids[0] == "anthropic/claude-fable-5"

    # The cost ceiling still wins: quality cannot climb past it.
    router = ModelRouter(catalog, _all_keys())
    economy = _policy(objective="quality", max_cost_tier="economy")
    capped = [m.id for m in router._candidates(economy)]
    assert capped
    assert all(catalog.get(i).cost_tier in ("economy", "local") for i in capped)


def test_quality_breaks_price_ties_toward_the_newer_model():
    catalog = ModelCatalog()
    ids = _candidates("quality", catalog)
    # Both 2.5/Mtok out: the 2026 model must precede the 2025 one.
    assert ids.index("openrouter/google/gemini-3.5-flash-lite") < ids.index("kimi/kimi-k2")


def test_thrift_objectives_let_an_uncurated_model_outrank_curated_ones():
    """Otherwise the catalog quietly overrules what the operator asked for."""
    catalog = ModelCatalog()
    catalog._dynamic = {
        "openrouter/vendor/tiny": ModelInfo(
            id="openrouter/vendor/tiny",
            provider="openrouter",
            wire_id="vendor/tiny",
            display_name="Tiny",
            context_window=32768,
            input_price_per_mtok=Decimal("0.05"),
            output_price_per_mtok=Decimal("0.1"),
            cost_tier="economy",
            curated=False,
            energy_class="S",
        )
    }
    assert _candidates("eco", catalog)[0] == "openrouter/vendor/tiny"
    assert _candidates("token_conservation", catalog)[0] == "openrouter/vendor/tiny"
    # ...while the capability objectives keep the curated-first preference.
    for objective in ("balanced", "quality"):
        ids = _candidates(objective, catalog)
        assert ids[-1] == "openrouter/vendor/tiny"


def test_eco_prefers_the_local_tier_over_equally_classed_cloud_models():
    catalog = ModelCatalog()
    catalog._local = {"local/qwen2.5:14b-instruct": _local_model()}
    registry = _Registry({"anthropic", "kimi", "openrouter", "local"})
    router = ModelRouter(catalog, registry)
    ids = [m.id for m in router._candidates(_policy(objective="eco"))]
    assert ids[0] == "local/qwen2.5:14b-instruct"
    assert ids.index("local/qwen2.5:14b-instruct") < ids.index(ECO_MODEL)


def test_ordering_is_deterministic():
    for objective in OBJECTIVES:
        assert _candidates(objective) == _candidates(objective)


def test_sort_key_is_shared_by_router_and_fallback():
    catalog = ModelCatalog()
    cheapest = min(catalog.all(curated_only=True), key=candidate_sort_key("eco"))
    assert cheapest.id == ECO_MODEL


# ── the rendered prompt ───────────────────────────────────────────────────────
def _prompt(objective: str | None = None, n: int = 3) -> str:
    candidates = ModelCatalog().all(curated_only=True)[:n]
    kwargs = dict(
        task_type="divergence_assessment",
        task_shape="verdict",
        task_description="Signal divergence assessment",
        output_contract="divergence_verdict",
        n_documents=2,
        est_input_tokens=1234,
        max_cost_tier="premium",
        candidates=candidates,
    )
    if objective is not None:
        kwargs["objective"] = objective
    return render_router_prompt(**kwargs)


def test_prompt_version_was_bumped():
    assert ROUTING_PROMPT_VERSION == "route-v4"


def test_the_default_objective_renders_the_historical_prompt_bytes():
    """A balanced harness must not see a single changed byte."""
    candidates = ModelCatalog().all(curated_only=True)[:3]
    legacy_lines = [
        "TASK",
        "  type: divergence_assessment",
        "  shape: verdict",
        "  description: Signal divergence assessment",
        "  output_contract: divergence_verdict",
        "  input_size: ~2 documents, est. 1234 input tokens",
        "",
        "CANDIDATES",
    ]
    for m in candidates:
        legacy_lines.append(
            f"  - id: {m.id} | released: {m.released} | tier: {m.cost_tier} | "
            f"ctx: {m.context_window} | strengths: {', '.join(m.strengths)}"
        )
    legacy_lines += ["", "CONSTRAINTS", "  max_cost_tier: premium"]
    legacy = "\n".join(legacy_lines)

    assert _prompt("balanced") == legacy
    assert _prompt(None) == legacy  # the default argument, too
    assert "OBJECTIVE" not in legacy and "energy" not in legacy


def test_each_non_default_objective_states_its_rules():
    for objective, rules in OBJECTIVE_RULES.items():
        prompt = _prompt(objective)
        assert "\nOBJECTIVE\n" in prompt
        assert f"  objective: {objective}" in prompt
        for rule in rules:
            assert rule in prompt
    assert "balanced" not in OBJECTIVE_RULES  # its rules are the system prompt's


def test_objective_rules_say_what_each_objective_means():
    assert "least estimated energy" in " ".join(OBJECTIVE_RULES["eco"])
    assert "prefer the local tier" in " ".join(OBJECTIVE_RULES["eco"]).lower()
    assert "smallest model" in " ".join(OBJECTIVE_RULES["token_conservation"])
    assert "Penalize premium models" in " ".join(OBJECTIVE_RULES["token_conservation"])
    assert "most capable candidate" in " ".join(OBJECTIVE_RULES["quality"])


def test_energy_is_shown_only_where_the_objective_reasons_about_it():
    for objective in ("eco", "token_conservation"):
        prompt = _prompt(objective)
        # L is the calibrated class fitted from Claude 3.7 Sonnet, in Wh per
        # million output-equivalent tokens (services/emissions.py).
        assert "| energy: L (~2600 Wh/Mtok, est.)" in prompt
    for objective in ("balanced", "quality"):
        assert "Wh/Mtok" not in _prompt(objective)


# ── deterministic fallback ────────────────────────────────────────────────────
def test_fallback_default_objective_still_walks_the_shape_table():
    assert fallback_model("verdict", ModelCatalog(), _all_keys()) == "anthropic/claude-sonnet-5"


def test_eco_fallback_picks_the_lowest_energy_available_model():
    catalog = ModelCatalog()
    chosen = fallback_model("verdict", catalog, _all_keys(), objective="eco")
    assert chosen == ECO_MODEL
    assert catalog.get(chosen).energy_wh_per_mtok == min(
        m.energy_wh_per_mtok for m in catalog.all(curated_only=True)
    )


def test_eco_fallback_prefers_a_local_model_when_one_is_installed():
    catalog = ModelCatalog()
    catalog._local = {"local/qwen2.5:14b-instruct": _local_model()}
    registry = _Registry({"anthropic", "kimi", "openrouter", "local"})
    assert fallback_model("verdict", catalog, registry, objective="eco") == (
        "local/qwen2.5:14b-instruct"
    )


def test_token_conservation_fallback_picks_the_cheapest_output():
    assert (
        fallback_model("drafting", ModelCatalog(), _all_keys(), objective="token_conservation")
        == THRIFT_MODEL
    )


def test_quality_fallback_climbs_the_shape_table_instead_of_walking_it():
    # The verdict table's first working entry is Sonnet; the most capable entry
    # the keys allow is GPT-5.6 Terra.
    assert (
        fallback_model("verdict", ModelCatalog(), _all_keys(), objective="quality")
        == "openrouter/openai/gpt-5.6-terra"
    )


def test_thrift_fallback_still_respects_the_allowed_list():
    chosen = fallback_model(
        "verdict", ModelCatalog(), _all_keys(), allowed=["kimi/kimi-k2"], objective="eco"
    )
    assert chosen == "kimi/kimi-k2"


def test_thrift_fallback_returns_none_when_nothing_is_available():
    assert fallback_model("verdict", ModelCatalog(), _Registry(set()), objective="eco") is None


# ── the persisted decision ────────────────────────────────────────────────────
def test_routing_decision_defaults_to_the_default_objective():
    decision = RoutingDecision(
        router_model=None,
        routing_prompt_version=ROUTING_PROMPT_VERSION,
        candidates=["a"],
        chosen_model="a",
        reasoning="x",
    )
    assert decision.objective == DEFAULT_OBJECTIVE
    assert decision.to_json()["objective"] == DEFAULT_OBJECTIVE


async def test_route_persists_the_objective_on_the_fallback_path(monkeypatch):
    # No usable router model at all, so the deterministic fallback decides.
    # (Having *no* key for the configured router model is no longer enough: the
    # router now resolves to a small model from a provider that does have one.)
    monkeypatch.setattr(ModelRouter, "_resolve_router_model", lambda self, max_tier: None)
    router = ModelRouter(ModelCatalog(), _Registry({"openrouter"}))
    decision = await router.route(
        model_policy=_policy(objective="eco"),
        task_type="divergence_assessment",
        task_shape="verdict",
        task_description="Signal divergence assessment",
        output_contract="divergence_verdict",
        n_documents=0,
        est_input_tokens=100,
    )
    assert decision.fallback_used is True
    assert decision.objective == "eco"
    assert decision.chosen_model == ECO_MODEL
    assert "objective 'eco'" in decision.reasoning
    assert decision.to_json()["objective"] == "eco"
    # And the candidate list the decision records is the eco ordering.
    assert decision.candidates[0] == ECO_MODEL


async def test_route_persists_the_objective_on_a_pinned_override():
    router = ModelRouter(ModelCatalog(), _all_keys())
    decision = await router.route(
        model_policy={"mode": "pinned", "model": "anthropic/claude-sonnet-5", "objective": "eco"},
        task_type="divergence_assessment",
        task_shape="verdict",
        task_description="Signal divergence assessment",
        output_contract="divergence_verdict",
        n_documents=0,
        est_input_tokens=100,
    )
    assert decision.override == "user_pin"
    assert decision.objective == "eco"  # logged even though it did not decide anything


async def test_route_records_the_objective_for_a_single_candidate():
    router = ModelRouter(ModelCatalog(), _Registry({"kimi"}))
    decision = await router.route(
        model_policy=_policy(objective="token_conservation", allowed=["kimi/kimi-k2"]),
        task_type="divergence_assessment",
        task_shape="verdict",
        task_description="Signal divergence assessment",
        output_contract="divergence_verdict",
        n_documents=0,
        est_input_tokens=100,
    )
    assert decision.chosen_model == "kimi/kimi-k2"
    assert decision.objective == "token_conservation"


# ── per-run objective override (chat composer control) ───────────────────────
# The composer can ask for a different objective than the harness default. The
# harness row must never be mutated, and omitting the override must reproduce
# the harness policy exactly.
def test_per_run_objective_overrides_the_harness_default():
    harness_policy = {"mode": "auto", "max_cost_tier": "premium", "objective": "balanced"}
    policy = effective_model_policy(harness_policy, {"_objective": "eco"})
    assert policy["objective"] == "eco"
    assert policy["max_cost_tier"] == "premium"  # the rest of the policy survives
    assert harness_policy["objective"] == "balanced"  # caller's dict untouched


def test_no_override_leaves_the_harness_policy_unchanged():
    harness_policy = {"mode": "auto", "objective": "quality"}
    assert effective_model_policy(harness_policy, {}) == harness_policy
    assert effective_model_policy(harness_policy, {"_model_override": "kimi/kimi-k2"}) == (
        harness_policy
    )
    assert effective_model_policy(harness_policy, None) == harness_policy


def test_an_absent_harness_policy_still_accepts_a_per_run_objective():
    assert effective_model_policy(None, {"_objective": "token_conservation"}) == {
        "mode": "auto",
        "objective": "token_conservation",
    }
