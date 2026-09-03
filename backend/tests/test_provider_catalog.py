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

from decimal import Decimal

import httpx
import pytest

from tret.providers.base import JsonCompletion
from tret.config import Settings
from tret.providers import catalog as catalog_module
from tret.providers.anthropic import AnthropicProvider
from tret.providers.catalog import (
    KEY_PROVIDERS,
    PROVIDER_NAMES,
    PROVIDER_SPECS,
    ModelCatalog,
    ModelInfo,
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

    monkeypatch.setattr(main_module, "get_catalog", lambda: catalog)
    monkeypatch.setattr(main_module, "get_engine", lambda: None)
    monkeypatch.setattr(main_module, "ensure_schema", _no_schema)
    monkeypatch.setattr(main_module, "get_session_factory", lambda: _FakeSession)
    monkeypatch.setattr(bootstrap_module, "bootstrap", _no_bootstrap)
    # Both are real lifespan steps now (single-instance enforcement, sweeping
    # runs a prior process left running) — irrelevant to what this test is
    # about, but real enough to need the same `_FakeSession`-style stubbing as
    # bootstrap above, since `_FakeSession` supports no actual queries.
    monkeypatch.setattr(instance_lock_module, "acquire_instance_lock", _no_instance_lock)
    monkeypatch.setattr(reconcile_module, "sweep_orphaned_runs", _no_sweep)

    async with main_module.lifespan(None):
        # Startup completed: the pass is running, and nothing waited for it.
        await asyncio.wait_for(started.wait(), timeout=1.0)
        assert catalog.finished is False
    # Shutdown cancelled it instead of hanging on `never`.
    assert catalog.finished is False
