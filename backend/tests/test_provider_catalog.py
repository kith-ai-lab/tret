"""Catalog robustness and the provider registry.

Four things the catalog is the single source of truth for, and therefore must not
get wrong quietly:

* the dynamic OpenRouter fetch is *best-effort* — a bad response degrades to "no
  dynamic models", it never takes GET /api/models (and the curated catalog with
  it) down;
* a price tret cannot bill against is not a price: an entry carrying
  OpenRouter's "-1" variable-pricing sentinel would land in a cost tier by
  accident and then feed a negative number into cost accounting;
* one registry row per provider drives key lookup, availability and
  construction, so a new provider cannot be half-added;
* local and dynamic models exist only after a discovery pass, so something other
  than "an operator opened the UI" has to trigger one.

No network: httpx is faked at the module level as elsewhere in the suite.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import httpx
import pytest

from tret.providers.base import JsonCompletion
from tret.config import Settings
from tret.providers import catalog as catalog_module
from tret.providers.anthropic import AnthropicProvider
from tret.providers.catalog import (
    CACHE_READ_MULTIPLIER,
    CACHE_WRITE_MULTIPLIER,
    KEY_PROVIDERS,
    PROVIDER_NAMES,
    PROVIDER_SPECS,
    ModelCatalog,
    ModelInfo,
    PriceChange,
    PricingTier,
    ProviderRegistry,
)
from tret.providers.local import LocalProvider
from tret.providers.openai_compat import KimiProvider, OpenRouterProvider


def _settings(**over) -> Settings:
    base = {
        "openrouter_api_key": "k",
        "openrouter_catalog": True,
        "local_base_url": "",
        "anthropic_api_key": "",
        "moonshot_api_key": "",
    }
    base.update(over)
    return Settings(**base)


def _patch_settings(monkeypatch, settings: Settings) -> None:
    monkeypatch.setattr(catalog_module, "get_settings", lambda: settings)


class _FakeResponse:
    def __init__(self, payload=None, json_exc: Exception | None = None, status: int = 200):
        self._payload = payload
        self._json_exc = json_exc
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)

    def json(self):
        if self._json_exc is not None:
            raise self._json_exc
        return self._payload


class _FakeClient:
    def __init__(self, response: _FakeResponse):
        self._response = response
        self.calls = 0

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None):
        self.calls += 1
        return self._response


def _patch_http(monkeypatch, response: _FakeResponse) -> _FakeClient:
    client = _FakeClient(response)
    monkeypatch.setattr(catalog_module.httpx, "AsyncClient", client)
    return client


# ── the dynamic fetch degrades, never explodes ────────────────────────────────
@pytest.mark.parametrize(
    "response",
    [
        # A 200 whose body is not JSON at all: a proxy error page, a captive
        # portal, an HTML maintenance notice.
        _FakeResponse(json_exc=ValueError("Expecting value: line 1 column 1 (char 0)")),
        _FakeResponse(payload=["not", "an", "object"]),
        _FakeResponse(payload={"data": "not-a-list"}),
        _FakeResponse(payload={}),
        _FakeResponse(payload={"data": ["not-an-object"]}),
    ],
)
async def test_a_bad_dynamic_response_leaves_the_curated_catalog_working(monkeypatch, response):
    _patch_settings(monkeypatch, _settings())
    _patch_http(monkeypatch, response)
    catalog = ModelCatalog()

    await catalog.refresh_dynamic()  # must not raise: GET /api/models depends on it

    assert catalog.all() == catalog.all(curated_only=True)
    assert catalog.all(curated_only=True), "the curated catalog must still be there"


async def test_a_good_dynamic_response_is_still_parsed(monkeypatch):
    _patch_settings(monkeypatch, _settings())
    _patch_http(
        monkeypatch,
        _FakeResponse(
            payload={
                "data": [
                    {
                        "id": "vendor/good",
                        "supported_parameters": ["tools"],
                        "pricing": {"prompt": "0.000001", "completion": "0.000002"},
                        "context_length": 128000,
                    }
                ]
            }
        ),
    )
    catalog = ModelCatalog()
    await catalog.refresh_dynamic()

    info = catalog.get("openrouter/vendor/good")
    assert info is not None
    assert info.input_price_per_mtok == Decimal("1")
    assert info.output_price_per_mtok == Decimal("2")
    assert info.context_window == 128000
    assert info.curated is False


# ── prices tret can actually bill against ─────────────────────────────────────
@pytest.mark.parametrize(
    "pricing",
    [
        {"prompt": "-1", "completion": "-1"},  # OpenRouter's variable-pricing sentinel
        {"prompt": "0.000001", "completion": "-1"},
        {"prompt": "-0.5", "completion": "0.000002"},
        {"prompt": "NaN", "completion": "0.000002"},  # Decimal() accepts these
        {"prompt": "Infinity", "completion": "0.000002"},
        {"prompt": "0.000001", "completion": "-Infinity"},
    ],
)
async def test_an_unbillable_price_keeps_the_model_out_of_the_catalog(monkeypatch, pricing):
    """A negative or sentinel price defeats both cost tiering and cost accounting;
    an absent model is honest, a model priced at -1 is not."""
    _patch_settings(monkeypatch, _settings())
    _patch_http(
        monkeypatch,
        _FakeResponse(
            payload={
                "data": [
                    {
                        "id": "vendor/mystery-price",
                        "supported_parameters": ["tools"],
                        "pricing": pricing,
                    }
                ]
            }
        ),
    )
    catalog = ModelCatalog()
    await catalog.refresh_dynamic()
    assert catalog.get("openrouter/vendor/mystery-price") is None


async def test_a_free_model_is_still_admitted(monkeypatch):
    """Zero is a real price. Only negative/non-finite is rejected."""
    _patch_settings(monkeypatch, _settings())
    _patch_http(
        monkeypatch,
        _FakeResponse(
            payload={
                "data": [
                    {
                        "id": "vendor/free",
                        "supported_parameters": ["tools"],
                        "pricing": {"prompt": "0", "completion": "0"},
                    }
                ]
            }
        ),
    )
    catalog = ModelCatalog()
    await catalog.refresh_dynamic()

    info = catalog.get("openrouter/vendor/free")
    assert info is not None
    assert info.output_price_per_mtok == Decimal("0")
    assert info.cost_tier == "economy"
    # Free in dollars is never free in watts.
    assert info.energy_wh(0, 1_000_000) > 0


async def test_a_missing_pricing_object_does_not_raise(monkeypatch):
    _patch_settings(monkeypatch, _settings())
    _patch_http(
        monkeypatch,
        _FakeResponse(
            payload={
                "data": [
                    {"id": "vendor/no-pricing", "supported_parameters": ["tools"]},
                    {
                        "id": "vendor/junk-pricing",
                        "supported_parameters": ["tools"],
                        "pricing": "free!",
                    },
                ]
            }
        ),
    )
    catalog = ModelCatalog()
    await catalog.refresh_dynamic()
    # An absent pricing object reads as zero (a free model), a junk one is skipped.
    assert catalog.get("openrouter/vendor/no-pricing") is not None
    assert catalog.get("openrouter/vendor/junk-pricing") is None


# ── one registry row per provider ─────────────────────────────────────────────
def test_every_spec_is_reachable_through_has_key_and_get(monkeypatch):
    """The single-registry property: for each row, a configured credential makes
    `has_key` true, puts the provider in `available_providers`, and `get` builds
    it. A provider added to PROVIDER_SPECS alone therefore cannot be half-wired."""
    _patch_settings(
        monkeypatch,
        _settings(
            anthropic_api_key="a",
            moonshot_api_key="m",
            openrouter_api_key="o",
            local_base_url="http://localhost:11434/v1",
        ),
    )
    registry = ProviderRegistry()
    assert list(PROVIDER_NAMES) == [spec.name for spec in PROVIDER_SPECS]
    assert registry.available_providers() == list(PROVIDER_NAMES)
    for name in PROVIDER_NAMES:
        assert registry.has_key(name) is True, name
        provider = registry.get(name)
        assert provider.name == name
        assert registry.get(name) is provider  # constructed once, cached


def test_specs_build_the_expected_provider_classes(monkeypatch):
    _patch_settings(
        monkeypatch,
        _settings(
            anthropic_api_key="a",
            moonshot_api_key="m",
            openrouter_api_key="o",
            local_base_url="http://localhost:11434/v1",
            local_display_name="Workstation",
            openrouter_referer="https://example.test",
            openrouter_title="tret-test",
        ),
    )
    registry = ProviderRegistry()
    assert isinstance(registry.get("anthropic"), AnthropicProvider)
    assert isinstance(registry.get("kimi"), KimiProvider)
    openrouter = registry.get("openrouter")
    assert isinstance(openrouter, OpenRouterProvider)
    # The referer/title headers still come from settings, as before consolidation.
    assert openrouter._headers["HTTP-Referer"] == "https://example.test"
    assert openrouter._headers["X-Title"] == "tret-test"
    local = registry.get("local")
    assert isinstance(local, LocalProvider)
    assert local.display_name == "Workstation"


def test_key_providers_excludes_the_key_optional_one():
    assert "local" in PROVIDER_NAMES
    assert "local" not in KEY_PROVIDERS
    assert set(KEY_PROVIDERS) == {"anthropic", "kimi", "openrouter"}


def test_an_unknown_provider_is_a_key_error(monkeypatch):
    _patch_settings(monkeypatch, _settings())
    registry = ProviderRegistry()
    assert registry.has_key("nope") is False
    with pytest.raises(KeyError):
        registry.get("nope")


def test_a_configured_but_keyless_cloud_provider_refuses_to_build(monkeypatch):
    _patch_settings(monkeypatch, _settings(anthropic_api_key=""))
    registry = ProviderRegistry()
    with pytest.raises(KeyError) as exc:
        registry.get("anthropic")
    assert "anthropic" in str(exc.value)


# ── cold start: something other than the UI triggers discovery ────────────────
async def test_warm_discovers_local_models_without_a_ui_visit(monkeypatch):
    _patch_settings(monkeypatch, _settings(local_base_url="http://localhost:11434/v1"))
    monkeypatch.setattr(
        catalog_module.LocalProvider,
        "complete_json",
        lambda *a, **k: _ok_probe(),
    )
    _patch_http(monkeypatch, _FakeResponse(payload={"data": [{"id": "llama3.1:8b"}]}))

    catalog = ModelCatalog()
    assert catalog.get("local/llama3.1:8b") is None  # cold: static entries only

    await catalog.warm()

    assert catalog.get("local/llama3.1:8b") is not None


async def _ok_probe() -> JsonCompletion:
    return JsonCompletion(payload={"ok": True})


async def test_warm_once_runs_a_single_pass(monkeypatch):
    _patch_settings(monkeypatch, _settings(local_base_url="http://localhost:11434/v1"))
    monkeypatch.setattr(
        catalog_module.LocalProvider, "complete_json", lambda *a, **k: _ok_probe()
    )
    client = _patch_http(monkeypatch, _FakeResponse(payload={"data": [{"id": "m"}]}))

    catalog = ModelCatalog()
    await catalog.warm_once()
    calls_after_first = client.calls
    await catalog.warm_once()
    assert client.calls == calls_after_first


async def test_warm_never_clears_a_local_catalog_it_cannot_refresh(monkeypatch):
    """With no base URL configured there is nothing to discover — and nothing to
    throw away either (refresh_local() alone would empty the local catalog)."""
    _patch_settings(monkeypatch, _settings(local_base_url=""))
    catalog = ModelCatalog()
    catalog._local = {
        "local/m": ModelInfo(
            id="local/m",
            provider="local",
            wire_id="m",
            display_name="Local: m",
            context_window=32768,
            input_price_per_mtok=Decimal("0"),
            output_price_per_mtok=Decimal("0"),
            cost_tier="local",
            curated=False,
        )
    }
    await catalog.warm()
    assert catalog.get("local/m") is not None


async def test_warm_survives_a_failing_refresh(monkeypatch):
    _patch_settings(monkeypatch, _settings(local_base_url="http://localhost:11434/v1"))

    async def boom(*args, **kwargs):
        raise RuntimeError("discovery exploded")

    monkeypatch.setattr(ModelCatalog, "refresh_local", boom)
    monkeypatch.setattr(ModelCatalog, "refresh_dynamic", boom)
    await ModelCatalog().warm()  # logged, never raised


async def test_startup_warms_the_catalog_without_blocking_the_boot(monkeypatch):
    """A boot must not depend on a reachable Ollama or on openrouter.ai, so the
    discovery pass is scheduled and never awaited — and it is cancelled on
    shutdown rather than holding the process open."""
    import asyncio

    from tret import main as main_module
    from tret.services import bootstrap as bootstrap_module
    from tret.services import emission_settings as emission_settings_module
    from tret.services import instance_lock as instance_lock_module
    from tret.services import reconcile as reconcile_module

    started = asyncio.Event()
    never = asyncio.Event()  # deliberately never set: a hanging model server

    class _HangingCatalog:
        finished = False

        async def warm(self):
            started.set()
            await never.wait()
            self.finished = True

    catalog = _HangingCatalog()

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    async def _no_schema(engine):
        return None

    async def _no_bootstrap(db):
        return None

    class _NoOpInstanceLock:
        # Read by main.py's lifespan to decide whether to sweep orphaned runs
        # (services/instance_lock.py) — irrelevant to what this test is about,
        # but the attribute has to exist for the lifespan to run at all.
        state = "not_applicable"

        async def release(self):
            return None

    async def _no_instance_lock():
        return _NoOpInstanceLock()

    async def _no_sweep(db):
        return 0

    async def _no_emissions_check(db):
        return 0

    monkeypatch.setattr(main_module, "get_catalog", lambda: catalog)
    monkeypatch.setattr(main_module, "get_engine", lambda: None)
    monkeypatch.setattr(main_module, "ensure_schema", _no_schema)
    monkeypatch.setattr(main_module, "get_session_factory", lambda: _FakeSession)
    monkeypatch.setattr(bootstrap_module, "bootstrap", _no_bootstrap)
    # All real lifespan steps now (single-instance enforcement, sweeping runs
    # a prior process left running, the M2 emissions-document boot check) —
    # irrelevant to what this test is about, but real enough to need the same
    # `_FakeSession`-style stubbing as bootstrap above, since `_FakeSession`
    # supports no actual queries.
    monkeypatch.setattr(instance_lock_module, "acquire_instance_lock", _no_instance_lock)
    monkeypatch.setattr(reconcile_module, "sweep_orphaned_runs", _no_sweep)
    monkeypatch.setattr(
        emission_settings_module, "check_workspace_emissions_documents", _no_emissions_check
    )

    async with main_module.lifespan(None):
        # Startup completed: the pass is running, and nothing waited for it.
        await asyncio.wait_for(started.wait(), timeout=1.0)
        assert catalog.finished is False
    # Shutdown cancelled it instead of hanging on `never`.
    assert catalog.finished is False


# ── active_params_b: legal, optional, validated — read by nothing yet ────────
def test_active_params_b_defaults_to_none_and_accepts_a_positive_value():
    base = dict(
        provider="anthropic",
        wire_id="m",
        display_name="M",
        context_window=1000,
        input_price_per_mtok=Decimal("1"),
        output_price_per_mtok=Decimal("2"),
        cost_tier="economy",
    )
    assert ModelInfo(id="a/m", **base).active_params_b is None
    info = ModelInfo(id="a/m2", active_params_b=70, **base)
    assert info.active_params_b == 70


@pytest.mark.parametrize("bad", [-1, 0, float("nan"), float("inf"), float("-inf")])
def test_active_params_b_rejects_non_positive_or_non_finite_values(bad):
    with pytest.raises(ValueError):
        ModelInfo(
            id="a/m",
            provider="anthropic",
            wire_id="m",
            display_name="M",
            context_window=1000,
            input_price_per_mtok=Decimal("1"),
            output_price_per_mtok=Decimal("2"),
            cost_tier="economy",
            active_params_b=bad,
        )


_MODEL_YAML_ENTRY = """\
id: anthropic/test-model
provider: anthropic
wire_id: test-model
display_name: Test Model
context_window: 100000
input_price_per_mtok: 1.0
output_price_per_mtok: 2.0
cost_tier: economy
"""


def test_a_catalog_entry_with_active_params_b_loads_with_the_field_set(monkeypatch, tmp_path):
    yaml_path = tmp_path / "models.yaml"
    yaml_path.write_text("models:\n  - " + _MODEL_YAML_ENTRY.replace("\n", "\n    ").rstrip() + "\n    active_params_b: 70\n")
    monkeypatch.setattr(catalog_module, "_MODELS_YAML", yaml_path)

    catalog = ModelCatalog()

    info = catalog.get("anthropic/test-model")
    assert info is not None
    assert info.active_params_b == 70.0


def test_a_catalog_entry_with_a_negative_active_params_b_is_rejected(monkeypatch, tmp_path):
    yaml_path = tmp_path / "models.yaml"
    yaml_path.write_text("models:\n  - " + _MODEL_YAML_ENTRY.replace("\n", "\n    ").rstrip() + "\n    active_params_b: -1\n")
    monkeypatch.setattr(catalog_module, "_MODELS_YAML", yaml_path)

    with pytest.raises(ValueError):
        ModelCatalog()


# ── per-model cache multipliers ────────────────────────────────────────────
def _model_with(**over) -> ModelInfo:
    base = dict(
        id="anthropic/test-cache",
        provider="anthropic",
        wire_id="test-cache",
        display_name="Test Cache",
        context_window=200_000,
        input_price_per_mtok=Decimal("10"),
        output_price_per_mtok=Decimal("50"),
        cost_tier="premium",
    )
    base.update(over)
    return ModelInfo(**base)


def test_a_models_own_cache_read_multiplier_overrides_the_default():
    """Claude Fable 5.1's real ratio: cache reads at 0.025x input price."""
    model = _model_with(cache_read_multiplier=Decimal("0.025"))
    assert model.cost_usd(0, 0, cache_read_tokens=1_000_000) == Decimal("0.25")


def test_a_model_with_no_override_still_uses_the_module_default():
    model = _model_with()
    assert model.cache_read_multiplier == CACHE_READ_MULTIPLIER
    assert model.cache_write_multiplier == CACHE_WRITE_MULTIPLIER
    assert model.cost_usd(0, 0, cache_read_tokens=1_000_000) == Decimal("1")  # 10 * 0.1
    assert model.cost_usd(0, 0, cache_write_tokens=1_000_000) == Decimal("12.5")  # 10 * 1.25


def test_an_explicit_cost_usd_argument_still_overrides_the_models_own_field():
    model = _model_with(cache_read_multiplier=Decimal("0.025"))
    doubled = model.cost_usd(
        0, 0, cache_read_tokens=1_000_000, cache_read_multiplier=Decimal("0.2")
    )
    assert doubled == Decimal("2")  # 10 * 0.2, not 10 * 0.025


# ── tiered pricing: whole-request multipliers above a prompt-size threshold ──
def _tiered_model(tiers: list[PricingTier]) -> ModelInfo:
    return _model_with(pricing_tiers=tiers)


def test_below_the_tier_threshold_pricing_is_linear():
    model = _tiered_model([PricingTier(272_000, Decimal("2.0"), Decimal("1.5"))])
    cost = model.cost_usd(272_000, 1_000)  # exactly at the threshold: not past it
    expected = (Decimal("10") * 272_000 + Decimal("50") * 1_000) / Decimal(1_000_000)
    assert cost == expected


def test_one_token_past_the_threshold_multiplies_the_whole_request():
    """GPT-6 Astra's own shape: 2x input/cache, 1.5x output, for the entire
    request once the prompt crosses 272K tokens — not just the excess."""
    model = _tiered_model([PricingTier(272_000, Decimal("2.0"), Decimal("1.5"))])
    cost = model.cost_usd(272_001, 1_000)
    expected = (
        Decimal("10") * Decimal("2.0") * 272_001 + Decimal("50") * Decimal("1.5") * 1_000
    ) / Decimal(1_000_000)
    assert cost == expected


def test_the_tier_threshold_counts_cache_tokens_toward_prompt_size():
    model = _tiered_model([PricingTier(1_000, Decimal("2.0"), Decimal("1.5"))])
    # 600 input + 500 cache read = 1,100 prompt tokens: past the 1,000 threshold,
    # so the cache-read term is scaled by the tier's input_multiplier too.
    cost = model.cost_usd(600, 0, cache_read_tokens=500)
    expected = (
        Decimal("10") * Decimal("2.0") * 600
        + Decimal("10") * Decimal("2.0") * Decimal("0.1") * 500
    ) / Decimal(1_000_000)
    assert cost == expected


def test_two_tiers_pick_the_largest_threshold_still_below_prompt_size():
    model = _tiered_model(
        [
            PricingTier(1_000, Decimal("1.5"), Decimal("1.2")),
            PricingTier(5_000, Decimal("3.0"), Decimal("2.0")),
        ]
    )
    past_first_only = model.cost_usd(2_000, 0)
    assert past_first_only == (Decimal("10") * Decimal("1.5") * 2_000) / Decimal(1_000_000)
    past_both = model.cost_usd(6_000, 0)
    assert past_both == (Decimal("10") * Decimal("3.0") * 6_000) / Decimal(1_000_000)


def test_descending_tier_thresholds_fail_at_load():
    with pytest.raises(ValueError, match="ascending"):
        _tiered_model(
            [
                PricingTier(5_000, Decimal("2.0"), Decimal("1.5")),
                PricingTier(1_000, Decimal("2.0"), Decimal("1.5")),
            ]
        )


def test_equal_tier_thresholds_also_fail_at_load():
    with pytest.raises(ValueError, match="ascending"):
        _tiered_model(
            [
                PricingTier(1_000, Decimal("2.0"), Decimal("1.5")),
                PricingTier(1_000, Decimal("3.0"), Decimal("1.5")),
            ]
        )


@pytest.mark.parametrize(
    "input_mult,output_mult",
    [
        (Decimal("0"), Decimal("1.5")),
        (Decimal("-1"), Decimal("1.5")),
        (Decimal("1.5"), Decimal("0")),
        (Decimal("1.5"), Decimal("-2")),
    ],
)
def test_a_zero_or_negative_tier_multiplier_fails_at_load(input_mult, output_mult):
    with pytest.raises(ValueError, match="positive"):
        _tiered_model([PricingTier(1_000, input_mult, output_mult)])


def test_a_curated_yaml_entry_with_invalid_pricing_tiers_fails_the_whole_catalog_load(
    monkeypatch, tmp_path
):
    yaml_path = tmp_path / "models.yaml"
    yaml_path.write_text(
        "models:\n  - "
        + _MODEL_YAML_ENTRY.replace("\n", "\n    ").rstrip()
        + "\n    pricing_tiers:\n"
        + "      - above_prompt_tokens: 5000\n        input_multiplier: 2.0\n        output_multiplier: 1.5\n"
        + "      - above_prompt_tokens: 1000\n        input_multiplier: 2.0\n        output_multiplier: 1.5\n"
    )
    monkeypatch.setattr(catalog_module, "_MODELS_YAML", yaml_path)

    with pytest.raises(ValueError, match="ascending"):
        ModelCatalog()


# ── dated pricing: price_changes resolved live via prices_at()/cost_usd() ────
# input_price_per_mtok/output_price_per_mtok are always the model's *base*
# (yaml) price — never baked against a date at load. `ModelInfo.prices_at()`
# resolves whichever `price_changes` entry is due as of a given date, and
# `cost_usd` calls it fresh (with `_today()`) on every invocation, so one
# long-lived `ModelInfo`/`ModelCatalog` bills correctly across the effective
# date with no reload — the bug this whole section exists to pin down.
_PRICE_CHANGE_YAML = """\
models:
  - id: openrouter/test/dated
    provider: openrouter
    wire_id: test/dated
    display_name: Dated Test Model
    context_window: 100000
    input_price_per_mtok: 0.75
    output_price_per_mtok: 3.75
    cost_tier: standard
    price_changes:
      - effective: "2027-01-01"
        input_price_per_mtok: 1.5
        output_price_per_mtok: 7.5
"""


def _write_dated_yaml(monkeypatch, tmp_path) -> None:
    yaml_path = tmp_path / "models.yaml"
    yaml_path.write_text(_PRICE_CHANGE_YAML)
    monkeypatch.setattr(catalog_module, "_MODELS_YAML", yaml_path)


def test_input_and_output_price_per_mtok_are_always_the_base_price(monkeypatch, tmp_path):
    _write_dated_yaml(monkeypatch, tmp_path)

    model = ModelCatalog().get("openrouter/test/dated")
    assert model.input_price_per_mtok == Decimal("0.75")
    assert model.output_price_per_mtok == Decimal("3.75")


def test_prices_at_before_the_effective_date_returns_the_base_price(monkeypatch, tmp_path):
    _write_dated_yaml(monkeypatch, tmp_path)

    model = ModelCatalog().get("openrouter/test/dated")
    assert model.prices_at(date(2026, 12, 31)) == (Decimal("0.75"), Decimal("3.75"))


def test_prices_at_on_and_after_the_effective_date_returns_the_new_price(monkeypatch, tmp_path):
    _write_dated_yaml(monkeypatch, tmp_path)

    model = ModelCatalog().get("openrouter/test/dated")
    assert model.prices_at(date(2027, 1, 1)) == (Decimal("1.5"), Decimal("7.5"))
    assert model.prices_at(date(2027, 6, 1)) == (Decimal("1.5"), Decimal("7.5"))


def test_price_changes_are_preserved_on_the_model_for_display(monkeypatch, tmp_path):
    _write_dated_yaml(monkeypatch, tmp_path)

    model = ModelCatalog().get("openrouter/test/dated")
    assert model.price_changes == [PriceChange(date(2027, 1, 1), Decimal("1.5"), Decimal("7.5"))]
    # The raw schedule never touches the base price fields.
    assert model.input_price_per_mtok == Decimal("0.75")


def test_prices_at_defaults_to_today_via_the_injectable_seam(monkeypatch, tmp_path):
    """Production code calls `_today()`, not `date.today()` — freezing that
    module function must move `prices_at()`'s default the same way an
    explicit `at=` would."""
    _write_dated_yaml(monkeypatch, tmp_path)
    monkeypatch.setattr(catalog_module, "_today", lambda: date(2027, 1, 1))

    model = ModelCatalog().get("openrouter/test/dated")
    assert model.prices_at() == (Decimal("1.5"), Decimal("7.5"))


def test_cost_usd_bills_the_effective_price_not_the_base_price(monkeypatch, tmp_path):
    _write_dated_yaml(monkeypatch, tmp_path)
    monkeypatch.setattr(catalog_module, "_today", lambda: date(2027, 1, 1))

    model = ModelCatalog().get("openrouter/test/dated")
    assert model.cost_usd(1_000_000, 0) == Decimal("1.5")  # new price, not the base 0.75


def test_the_same_loaded_model_bills_old_then_new_price_without_reloading(
    monkeypatch, tmp_path
):
    """The exact bug this fixes: a long-running process must not need to
    reconstruct or reload the catalog to pick up a scheduled price change on
    the day it becomes due — the same `ModelInfo`, fetched once, bills
    correctly before and after as "today" moves across the effective date."""
    _write_dated_yaml(monkeypatch, tmp_path)
    catalog = ModelCatalog()
    model = catalog.get("openrouter/test/dated")

    monkeypatch.setattr(catalog_module, "_today", lambda: date(2026, 12, 31))
    assert model.cost_usd(1_000_000, 0) == Decimal("0.75")

    # Only the clock moved — no new ModelCatalog(), no re-fetch from it.
    monkeypatch.setattr(catalog_module, "_today", lambda: date(2027, 1, 1))
    assert model.cost_usd(1_000_000, 0) == Decimal("1.5")

    # get() from the same catalog object agrees too.
    assert catalog.get("openrouter/test/dated").cost_usd(1_000_000, 0) == Decimal("1.5")


# ── price_changes validation (mirrors pricing_tiers validation above) ───────
def test_a_non_positive_price_change_price_fails_at_load():
    with pytest.raises(ValueError, match="positive"):
        _model_with(
            price_changes=[PriceChange(date(2027, 1, 1), Decimal("0"), Decimal("75"))]
        )
    with pytest.raises(ValueError, match="positive"):
        _model_with(
            price_changes=[PriceChange(date(2027, 1, 1), Decimal("20"), Decimal("-1"))]
        )


def test_descending_price_change_dates_fail_at_load():
    with pytest.raises(ValueError, match="ascending"):
        _model_with(
            price_changes=[
                PriceChange(date(2027, 6, 1), Decimal("20"), Decimal("75")),
                PriceChange(date(2027, 1, 1), Decimal("20"), Decimal("75")),
            ]
        )


def test_equal_price_change_dates_also_fail_at_load():
    with pytest.raises(ValueError, match="ascending"):
        _model_with(
            price_changes=[
                PriceChange(date(2027, 1, 1), Decimal("20"), Decimal("75")),
                PriceChange(date(2027, 1, 1), Decimal("25"), Decimal("80")),
            ]
        )


def test_a_price_change_that_would_cross_a_cost_tier_boundary_fails_at_load():
    """`_model_with`'s default cost_tier is "premium" (output_price_per_mtok
    50, which is >= 30); a scheduled drop to $7.50 output is "standard"
    territory — letting it load would silently mis-tier the model the day
    the change lands."""
    with pytest.raises(ValueError, match="standard"):
        _model_with(
            price_changes=[PriceChange(date(2027, 1, 1), Decimal("1.5"), Decimal("7.5"))]
        )


def test_a_curated_yaml_entry_with_a_malformed_price_changes_date_fails_the_whole_catalog_load(
    monkeypatch, tmp_path
):
    yaml_path = tmp_path / "models.yaml"
    yaml_path.write_text(
        "models:\n  - "
        + _MODEL_YAML_ENTRY.replace("\n", "\n    ").rstrip()
        + "\n    price_changes:\n"
        + "      - effective: \"not-a-date\"\n        input_price_per_mtok: 2.0\n"
        + "        output_price_per_mtok: 4.0\n"
    )
    monkeypatch.setattr(catalog_module, "_MODELS_YAML", yaml_path)

    with pytest.raises(ValueError, match="valid ISO date"):
        ModelCatalog()


def test_a_curated_yaml_entry_with_a_non_numeric_price_changes_price_fails_the_whole_catalog_load(
    monkeypatch, tmp_path
):
    yaml_path = tmp_path / "models.yaml"
    yaml_path.write_text(
        "models:\n  - "
        + _MODEL_YAML_ENTRY.replace("\n", "\n    ").rstrip()
        + "\n    price_changes:\n"
        + "      - effective: \"2027-01-01\"\n        input_price_per_mtok: not-a-number\n"
        + "        output_price_per_mtok: 4.0\n"
    )
    monkeypatch.setattr(catalog_module, "_MODELS_YAML", yaml_path)

    with pytest.raises(ValueError, match="not a valid number"):
        ModelCatalog()


# ── the September 2026 refresh: new curated entries ───────────────────────────
def test_new_september_models_load_with_the_expected_cost_model():
    catalog = ModelCatalog()

    fable_51 = catalog.get("anthropic/claude-fable-5-1")
    assert fable_51 is not None
    assert fable_51.cost_tier == "premium"
    assert fable_51.cache_read_multiplier == Decimal("0.025")
    assert fable_51.cost_usd(0, 0, cache_read_tokens=1_000_000) == Decimal("0.25")
    # Fable 5 itself must be untouched by the 5.1 override.
    assert catalog.get("anthropic/claude-fable-5").cache_read_multiplier == CACHE_READ_MULTIPLIER

    opus_5 = catalog.get("anthropic/claude-opus-5")
    assert opus_5 is not None
    assert opus_5.cost_tier == "premium"
    assert opus_5.input_price_per_mtok == Decimal("5")
    assert opus_5.output_price_per_mtok == Decimal("25")

    opus_55 = catalog.get("anthropic/claude-opus-5-5")
    assert opus_55 is not None
    assert opus_55.wire_id == "claude-opus-5-5"
    assert opus_55.input_price_per_mtok == Decimal("4")
    assert opus_55.output_price_per_mtok == Decimal("20")
    # cost_tier is declared "premium" even though a bare $20 output price
    # would classify as "standard" under _tier_from_price ($4 <= x < $30) —
    # that function is only enforced against price_changes entries and live
    # OpenRouter fetches, not a curated entry's own base price, so this loads
    # without error.
    assert opus_55.cost_tier == "premium"
    assert opus_55.cache_read_multiplier == Decimal("0.05")
    assert opus_55.cache_write_multiplier == Decimal("1.25")
    assert opus_55.energy_class == "XL"
    assert opus_55.supports_effort is True
    assert opus_55.supports_tools is True

    astra = catalog.get("openrouter/openai/gpt-6-astra")
    assert astra is not None
    assert astra.cost_tier == "premium"
    assert astra.supports_tools is True
    assert astra.pricing_tiers == [PricingTier(272_000, Decimal("2.0"), Decimal("1.5"))]
    # The exact boundary, against the real catalog entry, not a stand-in:
    # OpenRouter's own `min_prompt_tokens: 272000` override is exclusive
    # ("requests over 272K input tokens"), matching models.yaml's comment.
    at_threshold = astra.cost_usd(272_000, 1_000)
    one_past = astra.cost_usd(272_001, 1_000)
    assert at_threshold == (
        astra.input_price_per_mtok * 272_000 + astra.output_price_per_mtok * 1_000
    ) / Decimal(1_000_000)
    assert one_past == (
        astra.input_price_per_mtok * Decimal("2.0") * 272_001
        + astra.output_price_per_mtok * Decimal("1.5") * 1_000
    ) / Decimal(1_000_000)

    gemini_38 = catalog.get("openrouter/google/gemini-3.8-flash")
    assert gemini_38 is not None
    assert gemini_38.cost_tier == "standard"
    assert gemini_38.supports_tools is True
    assert gemini_38.price_changes == [
        PriceChange(date(2027, 1, 1), Decimal("1.5"), Decimal("7.5"))
    ]


def test_models_yaml_loads_cleanly_and_every_entry_has_the_required_fields():
    catalog = ModelCatalog()
    entries = catalog.all(curated_only=True)
    assert len(entries) >= 16  # 12 pre-existing + the 4 added this refresh
    for model in entries:
        assert model.id
        assert model.provider
        assert model.wire_id
        assert model.display_name
        assert model.context_window > 0
        assert model.input_price_per_mtok >= 0
        assert model.output_price_per_mtok >= 0
        assert model.cost_tier in {"economy", "standard", "premium", "local"}
        assert model.cache_read_multiplier > 0
        assert model.cache_write_multiplier > 0


# ── dynamic fetch: cache-read multiplier enrichment for uncurated entries ────
async def test_dynamic_fetch_reads_cache_read_multiplier_from_openrouter_pricing(monkeypatch):
    _patch_settings(monkeypatch, _settings())
    _patch_http(
        monkeypatch,
        _FakeResponse(
            payload={
                "data": [
                    {
                        "id": "vendor/cached",
                        "supported_parameters": ["tools"],
                        "pricing": {
                            "prompt": "0.00001",
                            "completion": "0.00005",
                            "input_cache_read": "0.00000025",
                        },
                        "context_length": 128000,
                    }
                ]
            }
        ),
    )
    catalog = ModelCatalog()
    await catalog.refresh_dynamic()

    info = catalog.get("openrouter/vendor/cached")
    assert info is not None
    assert info.cache_read_multiplier == Decimal("0.025")


async def test_dynamic_fetch_falls_back_to_the_default_multiplier_without_that_field(monkeypatch):
    _patch_settings(monkeypatch, _settings())
    _patch_http(
        monkeypatch,
        _FakeResponse(
            payload={
                "data": [
                    {
                        "id": "vendor/uncached",
                        "supported_parameters": ["tools"],
                        "pricing": {"prompt": "0.00001", "completion": "0.00005"},
                    }
                ]
            }
        ),
    )
    catalog = ModelCatalog()
    await catalog.refresh_dynamic()

    info = catalog.get("openrouter/vendor/uncached")
    assert info is not None
    assert info.cache_read_multiplier == CACHE_READ_MULTIPLIER


# ── dynamic fetch: supports_effort derived from supported_parameters ────────
async def test_dynamic_fetch_sets_supports_effort_when_reasoning_is_supported(monkeypatch):
    _patch_settings(monkeypatch, _settings())
    _patch_http(
        monkeypatch,
        _FakeResponse(
            payload={
                "data": [
                    {
                        "id": "vendor/thinker",
                        "supported_parameters": ["tools", "reasoning"],
                        "pricing": {"prompt": "0.00001", "completion": "0.00005"},
                    }
                ]
            }
        ),
    )
    catalog = ModelCatalog()
    await catalog.refresh_dynamic()

    info = catalog.get("openrouter/vendor/thinker")
    assert info is not None
    assert info.supports_effort is True


async def test_dynamic_fetch_leaves_supports_effort_false_without_reasoning(monkeypatch):
    _patch_settings(monkeypatch, _settings())
    _patch_http(
        monkeypatch,
        _FakeResponse(
            payload={
                "data": [
                    {
                        "id": "vendor/non-thinker",
                        "supported_parameters": ["tools"],
                        "pricing": {"prompt": "0.00001", "completion": "0.00005"},
                    }
                ]
            }
        ),
    )
    catalog = ModelCatalog()
    await catalog.refresh_dynamic()

    info = catalog.get("openrouter/vendor/non-thinker")
    assert info is not None
    assert info.supports_effort is False
