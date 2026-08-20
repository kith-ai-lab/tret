"""The harness cost ceiling holds on *every* routing path, not just the LLM one.

`max_cost_tier: local` is documented (docs/local-models.md) as a confidentiality
control: "a harness capped at `local` cannot run at all if no tool-capable local
model is available — routing fails loudly (`RoutingUnavailable`) instead of
quietly falling back to the cloud, which is the whole point."

The load-bearing detail is that the deterministic fallback is not an exotic path:
it decides whenever no usable router model exists, which for a harness capped at
`local` on a machine with no local model is *every* run. These tests drive
`route()` end to end with the LLM step unavailable, and `fallback_model()`
directly.

The second half of the file covers which model performs the routing decision at
all — a choice that is itself a model call, and therefore also subject to the
ceiling.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from tret.providers.catalog import ModelCatalog, ModelInfo, ProviderRegistry
from tret.router_llm import router as router_module
from tret.router_llm.fallback import fallback_model
from tret.router_llm.objectives import TIER_ORDER, within_cost_tier
from tret.router_llm.router import ModelRouter, RoutingUnavailable


class _Registry(ProviderRegistry):
    def __init__(self, providers: set[str]):
        self._providers = providers

    def has_key(self, provider: str) -> bool:
        return provider in self._providers


def _local(name: str = "m1") -> ModelInfo:
    return ModelInfo(
        id=f"local/{name}",
        provider="local",
        wire_id=name,
        display_name=f"Local: {name}",
        context_window=32768,
        input_price_per_mtok=Decimal("0"),
        output_price_per_mtok=Decimal("0"),
        cost_tier="local",
        supports_tools=True,
        curated=False,
    )


def _catalog_with_locals(*names: str) -> ModelCatalog:
    catalog = ModelCatalog()
    catalog._local = {f"local/{n}": _local(n) for n in names}
    return catalog


ROUTE_ARGS = dict(
    task_type="divergence_assessment",
    task_shape="verdict",
    task_description="Compare disclosed emissions against the dataset.",
    output_contract="verdict",
    n_documents=2,
    est_input_tokens=8000,
)


def _tier_of(catalog: ModelCatalog, model_id: str) -> str:
    return catalog.get(model_id).cost_tier


# ── fallback_model respects the ceiling ──────────────────────────────────────
def test_fallback_capped_at_local_never_returns_a_cloud_model():
    """The reported bug: cloud keys present, ceiling `local`, cloud model chosen."""
    catalog = _catalog_with_locals("m1", "m2")
    registry = _Registry({"local", "openrouter", "anthropic", "kimi"})

    chosen = fallback_model("verdict", catalog, registry, max_cost_tier="local")

    assert chosen is not None
    assert chosen.startswith("local/")
    assert _tier_of(catalog, chosen) == "local"


def test_fallback_capped_at_local_with_no_local_model_returns_none():
    """Fails loudly rather than escalating: None is what makes route() raise."""
    catalog = ModelCatalog()  # curated cloud catalog only
    registry = _Registry({"openrouter", "anthropic", "kimi"})
    assert fallback_model("verdict", catalog, registry, max_cost_tier="local") is None


@pytest.mark.parametrize("shape", ["verdict", "extraction", "drafting", "qa_review", "freeform"])
def test_no_task_shape_can_escape_the_ceiling(shape):
    """Each shape has its own preference list; all of them are gated."""
    catalog = _catalog_with_locals("m1")
    registry = _Registry({"local", "openrouter", "anthropic", "kimi"})
    assert fallback_model(shape, catalog, registry, max_cost_tier="local") == "local/m1"


@pytest.mark.parametrize("objective", ["balanced", "quality", "token_conservation", "eco"])
def test_no_objective_can_escape_the_ceiling(objective):
    """The table walk, the `quality` climb and the thrift ranking are separate
    code paths inside fallback_model — the cap has to apply to all four."""
    catalog = _catalog_with_locals("m1")
    registry = _Registry({"local", "openrouter", "anthropic", "kimi"})
    chosen = fallback_model(
        "verdict", catalog, registry, objective=objective, max_cost_tier="local"
    )
    assert chosen == "local/m1"


def test_fallback_capped_at_economy_never_returns_premium_or_standard():
    catalog = ModelCatalog()
    registry = _Registry({"openrouter", "anthropic", "kimi"})
    for objective in ["balanced", "quality", "token_conservation", "eco"]:
        chosen = fallback_model(
            "verdict", catalog, registry, objective=objective, max_cost_tier="economy"
        )
        assert chosen is not None, objective
        assert TIER_ORDER[_tier_of(catalog, chosen)] <= TIER_ORDER["economy"], (
            objective,
            chosen,
            _tier_of(catalog, chosen),
        )


def test_unrestricted_fallback_behaviour_is_unchanged():
    """The default is still no ceiling: same picks as before the fix."""
    catalog = ModelCatalog()
    registry = _Registry({"openrouter"})
    assert fallback_model("verdict", catalog, registry) == "openrouter/openai/gpt-5.6-terra"

    # And a local-only installation still routes to its local model.
    local_only = _catalog_with_locals("m1")
    assert fallback_model("verdict", local_only, _Registry({"local"})) == "local/m1"

    # Curated cloud still beats an available local model when nothing is capped.
    mixed = _catalog_with_locals("m1")
    assert (
        fallback_model("verdict", mixed, _Registry({"local", "openrouter"}))
        == "openrouter/openai/gpt-5.6-terra"
    )


# ── which model performs the routing decision ────────────────────────────────
def _with_router_model(monkeypatch, model_id: str) -> None:
    settings = router_module.get_settings()
    monkeypatch.setattr(
        router_module,
        "get_settings",
        lambda: settings.model_copy(update={"router_model": model_id}),
    )


def test_configured_router_model_is_used_when_its_provider_has_a_key(monkeypatch):
    _with_router_model(monkeypatch, "anthropic/claude-haiku-4-5")
    router = ModelRouter(ModelCatalog(), _Registry({"anthropic", "openrouter"}))
    assert router._resolve_router_model("premium").id == "anthropic/claude-haiku-4-5"


def test_router_model_resolves_to_a_configured_provider_when_the_default_has_no_key(monkeypatch):
    """The documented quickstart is "OpenRouter alone works". With the shipped
    Anthropic default and only an OpenRouter key, the LLM router step used to be
    skipped on every run — while the audit record still named the Anthropic model
    it never called."""
    _with_router_model(monkeypatch, "anthropic/claude-haiku-4-5")
    catalog = ModelCatalog()
    router = ModelRouter(catalog, _Registry({"openrouter"}))

    resolved = router._resolve_router_model("premium")

    assert resolved is not None
    assert resolved.provider == "openrouter"
    # Small and cheap: routing is a one-line answer, not a premium task.
    assert TIER_ORDER[resolved.cost_tier] <= TIER_ORDER["economy"]


def test_router_model_resolution_respects_the_ceiling(monkeypatch):
    """Choosing a model is itself a model call: a local-capped harness must not
    hand its task description to a cloud router."""
    _with_router_model(monkeypatch, "anthropic/claude-haiku-4-5")
    catalog = _catalog_with_locals("m1")
    router = ModelRouter(catalog, _Registry({"local", "anthropic", "openrouter", "kimi"}))

    resolved = router._resolve_router_model("local")

    assert resolved is not None
    assert resolved.provider == "local"


def test_no_router_model_when_a_local_cap_has_no_local_model(monkeypatch):
    _with_router_model(monkeypatch, "anthropic/claude-haiku-4-5")
    router = ModelRouter(ModelCatalog(), _Registry({"anthropic", "openrouter"}))
    assert router._resolve_router_model("local") is None


def test_no_router_model_without_any_keys(monkeypatch):
    _with_router_model(monkeypatch, "anthropic/claude-haiku-4-5")
    assert ModelRouter(ModelCatalog(), _Registry(set()))._resolve_router_model("premium") is None


def test_a_local_router_model_is_not_substituted_for_cloud_work(monkeypatch):
    """A machine that happens to have Ollama configured must not start using a
    small local model to route cloud runs — only an explicit cap does that."""
    _with_router_model(monkeypatch, "anthropic/claude-haiku-4-5")
    catalog = _catalog_with_locals("m1")
    router = ModelRouter(catalog, _Registry({"local", "openrouter"}))
    assert router._resolve_router_model("premium").provider == "openrouter"


# ── route() end to end on the fallback path ──────────────────────────────────
@pytest.fixture()
def no_llm_router(monkeypatch):
    """No usable router model, so the deterministic fallback decides.

    This is the shape the ceiling bug lived in, and it is not exotic: it happens
    whenever no configured provider offers a small model — and it is the only
    path at all for a harness capped at `local` with no local model to route
    with.
    """
    monkeypatch.setattr(ModelRouter, "_resolve_router_model", lambda self, max_tier: None)


async def test_route_with_local_ceiling_and_cloud_keys_stays_local(no_llm_router):
    catalog = _catalog_with_locals("m1", "m2")
    router = ModelRouter(catalog, _Registry({"local", "openrouter", "kimi"}))

    decision = await router.route(
        model_policy={"mode": "auto", "max_cost_tier": "local"}, **ROUTE_ARGS
    )

    assert decision.fallback_used is True
    assert decision.chosen_model.startswith("local/")
    assert _tier_of(catalog, decision.chosen_model) == "local"
    # The audit record must agree with itself: the chosen model was a candidate.
    assert decision.chosen_model in decision.candidates
    assert all(c.startswith("local/") for c in decision.candidates)
    # No cloud router was contacted to make a local-only decision.
    assert decision.router_model is None
    assert "local" in decision.reasoning


async def test_route_with_local_ceiling_and_no_local_model_raises(no_llm_router):
    router = ModelRouter(ModelCatalog(), _Registry({"openrouter", "kimi"}))
    with pytest.raises(RoutingUnavailable):
        await router.route(
            model_policy={"mode": "auto", "max_cost_tier": "local"}, **ROUTE_ARGS
        )


async def test_route_with_economy_ceiling_never_returns_premium(no_llm_router):
    # No Anthropic key, so the configured router model is unavailable and the
    # deterministic fallback decides — the shape this bug lived in.
    catalog = ModelCatalog()
    router = ModelRouter(catalog, _Registry({"openrouter", "kimi"}))

    decision = await router.route(
        model_policy={"mode": "auto", "max_cost_tier": "economy"}, **ROUTE_ARGS
    )

    assert TIER_ORDER[_tier_of(catalog, decision.chosen_model)] <= TIER_ORDER["economy"]
    assert decision.chosen_model in decision.candidates


async def test_route_without_a_ceiling_is_unchanged(no_llm_router):
    catalog = ModelCatalog()
    router = ModelRouter(catalog, _Registry({"openrouter"}))
    decision = await router.route(model_policy={"mode": "auto"}, **ROUTE_ARGS)
    assert decision.fallback_used is True
    assert decision.chosen_model == "openrouter/openai/gpt-5.6-terra"


async def test_an_unknown_ceiling_is_not_read_as_unrestricted(no_llm_router):
    """A policy that somehow carries a bad tier must not become 'premium' by
    accident — it is normalized, and the run still routes within a real tier."""
    catalog = _catalog_with_locals("m1")
    router = ModelRouter(catalog, _Registry({"local", "openrouter"}))
    decision = await router.route(
        model_policy={"mode": "auto", "max_cost_tier": "not-a-tier"}, **ROUTE_ARGS
    )
    assert decision.chosen_model in decision.candidates


async def test_a_pin_above_the_ceiling_is_still_honoured_and_recorded(no_llm_router):
    """Pins are explicit operator choices, not silent escalation: they still
    work, and the decision says it was an override."""
    router = ModelRouter(ModelCatalog(), _Registry({"openrouter"}))
    decision = await router.route(
        model_policy={
            "mode": "pinned",
            "model": "openrouter/openai/gpt-5.6-terra",
            "max_cost_tier": "local",
        },
        **ROUTE_ARGS,
    )
    assert decision.chosen_model == "openrouter/openai/gpt-5.6-terra"
    assert decision.override == "user_pin"


# ── per-request overrides are bounded by the harness policy ──────────────────
# `_model_override` reaches route() from POST /api/runs and the chat composer,
# i.e. from any signed-in caller with no admin check and no harness edit. A
# harness pin and a harness ceiling are written on the harness itself. The two
# must therefore not have the same power — see ModelRouter's module docstring and
# `_assert_override_within_policy`.
CLOUD_MODEL = "openrouter/openai/gpt-5.6-terra"


async def test_a_per_run_override_cannot_lift_the_cost_ceiling(no_llm_router):
    """`max_cost_tier: local` is a confidentiality control: no request body may
    talk a local-only harness into a cloud call."""
    catalog = _catalog_with_locals("m1")
    router = ModelRouter(catalog, _Registry({"local", "openrouter"}))

    with pytest.raises(RoutingUnavailable) as exc:
        await router.route(
            model_policy={"mode": "auto", "max_cost_tier": "local"},
            run_override=CLOUD_MODEL,
            **ROUTE_ARGS,
        )
    assert "cost ceiling" in str(exc.value)
    assert CLOUD_MODEL in str(exc.value)


async def test_a_per_run_override_within_the_ceiling_is_honoured(no_llm_router):
    catalog = _catalog_with_locals("m1", "m2")
    router = ModelRouter(catalog, _Registry({"local", "openrouter"}))

    decision = await router.route(
        model_policy={"mode": "auto", "max_cost_tier": "local"},
        run_override="local/m2",
        **ROUTE_ARGS,
    )

    assert decision.chosen_model == "local/m2"
    assert decision.override == "run_override"  # still recorded as an override
    assert decision.fallback_used is False


async def test_a_per_run_override_cannot_escape_the_allowed_list(no_llm_router):
    router = ModelRouter(ModelCatalog(), _Registry({"openrouter", "kimi"}))

    with pytest.raises(RoutingUnavailable) as exc:
        await router.route(
            model_policy={"mode": "auto", "allowed": ["kimi/kimi-k2"]},
            run_override=CLOUD_MODEL,
            **ROUTE_ARGS,
        )
    assert "allowed" in str(exc.value)


async def test_a_per_run_override_on_the_allowed_list_is_honoured(no_llm_router):
    router = ModelRouter(ModelCatalog(), _Registry({"openrouter", "kimi"}))
    decision = await router.route(
        model_policy={"mode": "auto", "allowed": ["kimi/kimi-k2", CLOUD_MODEL]},
        run_override=CLOUD_MODEL,
        **ROUTE_ARGS,
    )
    assert decision.chosen_model == CLOUD_MODEL
    assert decision.override == "run_override"


async def test_an_unrestricted_harness_still_takes_any_per_run_override(no_llm_router):
    """No `allowed` list and no ceiling: the override behaves exactly as before."""
    router = ModelRouter(ModelCatalog(), _Registry({"openrouter"}))
    decision = await router.route(
        model_policy={"mode": "auto"}, run_override=CLOUD_MODEL, **ROUTE_ARGS
    )
    assert decision.chosen_model == CLOUD_MODEL
    assert decision.override == "run_override"


async def test_a_harness_pin_keeps_its_exemption(no_llm_router):
    """The asymmetry is the point: a pin is a harness setting and may exceed that
    harness's own ceiling and allowed list; a request body may not."""
    router = ModelRouter(ModelCatalog(), _Registry({"openrouter"}))
    decision = await router.route(
        model_policy={
            "mode": "pinned",
            "model": CLOUD_MODEL,
            "max_cost_tier": "local",
            "allowed": ["kimi/kimi-k2"],
        },
        **ROUTE_ARGS,
    )
    assert decision.chosen_model == CLOUD_MODEL
    assert decision.override == "user_pin"


# ── cold start: routing does not require someone to open the UI ──────────────
class _ColdCatalog(ModelCatalog):
    """A catalog whose local models exist only once a discovery pass runs."""

    def __init__(self) -> None:
        super().__init__()
        self.passes = 0

    async def warm(self) -> None:
        self.passes += 1
        self._warmed = True
        self._local = {"local/m1": _local("m1")}


async def test_route_runs_a_discovery_pass_on_a_cold_catalog(no_llm_router):
    """Local (and dynamic) models reach the catalog only through discovery, which
    used to happen exclusively in GET /api/models — so a harness capped at `local`
    could not route on a fresh process until an operator opened the UI."""
    catalog = _ColdCatalog()
    router = ModelRouter(catalog, _Registry({"local"}))

    decision = await router.route(
        model_policy={"mode": "auto", "max_cost_tier": "local"}, **ROUTE_ARGS
    )

    assert decision.chosen_model == "local/m1"
    assert catalog.passes == 1
    # And the pass is not repeated on the next run of the same process.
    await router.route(model_policy={"mode": "auto", "max_cost_tier": "local"}, **ROUTE_ARGS)
    assert catalog.passes == 1


# ── the shared predicate ─────────────────────────────────────────────────────
def test_within_cost_tier_places_local_below_every_cap():
    local = _local()
    for tier in TIER_ORDER:
        assert within_cost_tier(local, tier), tier
