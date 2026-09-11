"""`RoutingDecision.provider_ignore`: routing evidence wired into OpenRouter's
`provider.ignore`.

`priors_base.poor_endpoints()` already decides which of a model's endpoints
look bad on their own record (tested in `test_priors_endpoints.py`, offline).
What is under test here is the router's own half: that every automatic
decision path — single candidate, and the deterministic fallback (the LLM
router path is exercised the same way in `test_adaptive_routing.py`'s
`_StubPriors` pattern, and shares the same `_provider_ignore_for` call, so it
is not re-verified here) — reads `priors[chosen_model]` and records the
result as `provider_ignore`, and that the override/pin paths, which never
read `priors` at all, always leave it empty.

Fixtures follow `test_router_context_fit.py`'s pattern: a `ProviderRegistry`
fake keyed on which providers have keys, and a `ModelCatalog` whose `_static`
table is replaced outright.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from tret.providers.catalog import ModelCatalog, ModelInfo, ProviderRegistry
from tret.router_llm.priors_base import EndpointPrior, ModelPrior
from tret.router_llm.router import ModelRouter


class _Registry(ProviderRegistry):
    def __init__(self, providers: set[str]):
        self._providers = providers

    def has_key(self, provider: str) -> bool:
        return provider in self._providers


class _StubPriors:
    """A fixed track record, so a decision's evidence is stated by the test —
    same shape as `test_adaptive_routing.py`'s fixture of the same name.
    """

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
) -> ModelInfo:
    return ModelInfo(
        id=f"test/{name}",
        provider=provider,
        wire_id=name,
        display_name=name,
        context_window=100_000,
        input_price_per_mtok=Decimal("1"),
        output_price_per_mtok=Decimal("1"),
        cost_tier=cost_tier,
        supports_tools=True,
        curated=True,
    )


def _catalog(*models: ModelInfo) -> ModelCatalog:
    catalog = ModelCatalog()
    catalog._static = {m.id: m for m in models}
    return catalog


def _endpoint(quality_ci_low: float) -> EndpointPrior:
    return EndpointPrior(
        runs=20,
        effective_n=15.0,
        quality_mean=quality_ci_low + 0.1,
        quality_ci_low=quality_ci_low,
        delivered_rate=0.9,
    )


def _prior(model_id: str, *, quality_ci_low: float = 0.7, endpoints=None) -> ModelPrior:
    return ModelPrior(
        model_id=model_id,
        runs=40,
        effective_n=30.0,
        quality_mean=quality_ci_low + 0.1,
        quality_raw=quality_ci_low + 0.1,
        quality_ci_low=quality_ci_low,
        delivered_rate=0.9,
        failure_rate=0.1,
        mean_cost_usd=0.02,
        mean_output_tokens=800,
        mean_iterations=5.0,
        mean_energy_wh=0.4,
        approvals=0,
        rejections=0,
        endpoints=endpoints or {},
    )


ROUTE_ARGS = dict(
    task_type="divergence_assessment",
    task_shape="verdict",
    task_description="Compare disclosed emissions against the dataset.",
    output_contract="verdict",
    n_documents=2,
    est_input_tokens=8000,
)


@pytest.fixture()
def no_llm_router(monkeypatch):
    """Force the deterministic fallback path — same fixture as
    `test_router_context_fit.py`: no usable router model, so `route()` always
    decides through `fallback_model()`."""
    monkeypatch.setattr(ModelRouter, "_resolve_router_model", lambda self, max_tier: None)


# ── single candidate ─────────────────────────────────────────────────────────
async def test_single_candidate_carries_provider_ignore_from_a_poor_endpoint(no_llm_router):
    only = _model("only")
    catalog = _catalog(only)
    prior = _prior(
        only.id,
        quality_ci_low=0.7,
        endpoints={
            "clean-endpoint": _endpoint(0.75),
            "quantized-endpoint": _endpoint(0.1),
        },
    )
    router = ModelRouter(catalog, _Registry({"openrouter"}), priors=_StubPriors({only.id: prior}))

    decision = await router.route(model_policy={"mode": "auto"}, **ROUTE_ARGS)

    assert decision.chosen_model == only.id
    assert decision.provider_ignore == ["quantized-endpoint"]


async def test_single_candidate_has_no_provider_ignore_with_no_prior(no_llm_router):
    only = _model("only")
    catalog = _catalog(only)
    router = ModelRouter(catalog, _Registry({"openrouter"}))  # NoPriors by default

    decision = await router.route(model_policy={"mode": "auto"}, **ROUTE_ARGS)

    assert decision.chosen_model == only.id
    assert decision.provider_ignore == []


async def test_single_candidate_has_no_provider_ignore_when_nothing_stands_out(no_llm_router):
    only = _model("only")
    catalog = _catalog(only)
    # Two endpoints, both comparably good — nothing for poor_endpoints() to name.
    prior = _prior(
        only.id,
        quality_ci_low=0.7,
        endpoints={"a": _endpoint(0.72), "b": _endpoint(0.68)},
    )
    router = ModelRouter(catalog, _Registry({"openrouter"}), priors=_StubPriors({only.id: prior}))

    decision = await router.route(model_policy={"mode": "auto"}, **ROUTE_ARGS)

    assert decision.provider_ignore == []


async def test_single_candidate_has_no_provider_ignore_with_a_single_endpoint(no_llm_router):
    only = _model("only")
    catalog = _catalog(only)
    # Fewer than two endpoints on record: ModelPrior.endpoints is empty by the
    # same rule priors_base.summarize() itself applies.
    prior = _prior(only.id, quality_ci_low=0.7, endpoints={})
    router = ModelRouter(catalog, _Registry({"openrouter"}), priors=_StubPriors({only.id: prior}))

    decision = await router.route(model_policy={"mode": "auto"}, **ROUTE_ARGS)

    assert decision.provider_ignore == []


# ── deterministic fallback, two candidates ──────────────────────────────────
async def test_fallback_path_carries_provider_ignore_for_the_chosen_model_only(no_llm_router):
    # No shape-table entry matches these ids, so fallback_model() falls to its
    # last resort — the lexicographically-first curated id — which is "big".
    big = _model("big")
    small = _model("small")
    catalog = _catalog(big, small)
    priors = {
        big.id: _prior(
            big.id,
            quality_ci_low=0.7,
            endpoints={"clean-endpoint": _endpoint(0.75), "quantized-endpoint": _endpoint(0.1)},
        ),
        # Given a prior at all, but no per-endpoint breakdown — must not leak
        # into the chosen model's own provider_ignore.
        small.id: _prior(small.id, quality_ci_low=0.7, endpoints={}),
    }
    router = ModelRouter(catalog, _Registry({"openrouter"}), priors=_StubPriors(priors))

    decision = await router.route(model_policy={"mode": "auto"}, **ROUTE_ARGS)

    assert decision.chosen_model == big.id
    assert decision.provider_ignore == ["quantized-endpoint"]


async def test_fallback_path_has_no_provider_ignore_without_evidence(no_llm_router):
    big = _model("big")
    small = _model("small")
    catalog = _catalog(big, small)
    router = ModelRouter(catalog, _Registry({"openrouter"}))  # NoPriors by default

    decision = await router.route(model_policy={"mode": "auto"}, **ROUTE_ARGS)

    assert decision.chosen_model == big.id
    assert decision.provider_ignore == []


# ── override / pin: never read priors, so never carry provider_ignore ──────
async def test_a_pin_never_carries_provider_ignore_even_with_poor_evidence_on_record(
    no_llm_router,
):
    only = _model("only")
    catalog = _catalog(only)
    poor_prior = _prior(
        only.id,
        quality_ci_low=0.7,
        endpoints={"clean-endpoint": _endpoint(0.75), "quantized-endpoint": _endpoint(0.1)},
    )
    router = ModelRouter(
        catalog, _Registry({"openrouter"}), priors=_StubPriors({only.id: poor_prior})
    )

    decision = await router.route(
        model_policy={"mode": "pinned", "model": only.id}, **ROUTE_ARGS
    )

    assert decision.override == "user_pin"
    assert decision.provider_ignore == []


async def test_a_run_override_never_carries_provider_ignore_even_with_poor_evidence_on_record(
    no_llm_router,
):
    only = _model("only")
    catalog = _catalog(only)
    poor_prior = _prior(
        only.id,
        quality_ci_low=0.7,
        endpoints={"clean-endpoint": _endpoint(0.75), "quantized-endpoint": _endpoint(0.1)},
    )
    router = ModelRouter(
        catalog, _Registry({"openrouter"}), priors=_StubPriors({only.id: poor_prior})
    )

    decision = await router.route(
        model_policy={"mode": "auto"}, run_override=only.id, **ROUTE_ARGS
    )

    assert decision.override == "run_override"
    assert decision.provider_ignore == []
