"""Local model provider: registry enablement, catalog discovery + tool-calling
probe, router candidacy, local-only fallback, and the settings connection test.

No network: httpx is faked at the module level exactly as in
test_prompt_caching.py, and LocalProvider.complete_json is monkeypatched
directly for probe outcomes rather than faking a full SSE/JSON round trip.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from bench.api import settings as settings_api
from bench.api.auth import require_admin
from bench.api.harnesses import _validate_policy
from bench.config import Settings
from bench.providers import catalog as catalog_module
from bench.providers.catalog import ModelCatalog, ModelInfo, ProviderRegistry
from bench.providers.local import LocalProvider
from bench.router_llm.fallback import fallback_model
from bench.router_llm.router import TIER_ORDER, ModelRouter


def _settings(**over) -> Settings:
    base = {"local_base_url": "http://localhost:11434/v1"}
    base.update(over)
    return Settings(**base)


def _patch_settings(monkeypatch, settings: Settings) -> None:
    monkeypatch.setattr(catalog_module, "get_settings", lambda: settings)


# ── ProviderRegistry: key-optional "local" ─────────────────────────────────────
def test_local_not_available_without_base_url(monkeypatch):
    _patch_settings(monkeypatch, _settings(local_base_url=""))
    registry = ProviderRegistry()
    assert registry.has_key("local") is False
    assert "local" not in registry.available_providers()
    with pytest.raises(KeyError):
        registry.get("local")


def test_local_available_from_base_url_alone_no_key(monkeypatch):
    _patch_settings(monkeypatch, _settings(local_api_key=""))
    registry = ProviderRegistry()
    assert registry.has_key("local") is True
    assert "local" in registry.available_providers()
    provider = registry.get("local")
    assert isinstance(provider, LocalProvider)


def test_local_registry_is_independent_of_cloud_keys(monkeypatch):
    # A local-only installation: no cloud keys anywhere, base URL only.
    _patch_settings(
        monkeypatch,
        _settings(anthropic_api_key="", moonshot_api_key="", openrouter_api_key=""),
    )
    registry = ProviderRegistry()
    assert registry.available_providers() == ["local"]


def test_env_still_wins_over_db_for_cloud_providers(monkeypatch):
    # Unrelated to local, but the task guards this invariant explicitly.
    _patch_settings(monkeypatch, _settings(anthropic_api_key="env-key"))
    registry = ProviderRegistry(db_keys={"anthropic": "db-key"})
    assert registry.has_key("anthropic") is True
    assert registry._keys["anthropic"] == "env-key"


# ── fake httpx transport for GET {base_url}/models ─────────────────────────────
class _FakeModelsResponse:
    def __init__(self, payload=None, status=200):
        self._payload = payload or {}
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)

    def json(self):
        return self._payload


class _FakeGetClient:
    """Stands in for httpx.AsyncClient for a single GET call."""

    def __init__(self, payload=None, exc: Exception | None = None, delay: float = 0.0):
        self._payload = payload
        self._exc = exc
        self._delay = delay
        self.last_headers: dict | None = None
        self.urls: list[str] = []

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None):
        self.last_headers = headers
        self.urls.append(url)
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._exc:
            raise self._exc
        return _FakeModelsResponse(self._payload)


async def _always_ok(*args, **kwargs) -> dict:
    return {"ok": True}


async def _always_fails(*args, **kwargs):
    from bench.providers.base import ProviderError

    raise ProviderError("local", "model ignored tool_choice")


# ── catalog discovery ────────────────────────────────────────────────────────
async def test_refresh_local_noop_without_base_url(monkeypatch):
    _patch_settings(monkeypatch, _settings(local_base_url=""))
    catalog = ModelCatalog()
    await catalog.refresh_local()
    assert catalog.all() == catalog.all(curated_only=True)  # nothing added


async def test_refresh_local_parses_discovered_models(monkeypatch):
    _patch_settings(monkeypatch, _settings())
    monkeypatch.setattr(
        catalog_module.LocalProvider, "complete_json", _always_ok
    )
    fake_client = _FakeGetClient(
        payload={"data": [{"id": "qwen2.5:14b-instruct", "context_length": 32768}]}
    )
    monkeypatch.setattr(catalog_module.httpx, "AsyncClient", fake_client)

    catalog = ModelCatalog()
    await catalog.refresh_local()

    info = catalog.get("local/qwen2.5:14b-instruct")
    assert info is not None
    assert info.provider == "local"
    assert info.wire_id == "qwen2.5:14b-instruct"
    assert info.cost_tier == "local"
    assert info.curated is False
    assert info.context_window == 32768
    assert info.input_price_per_mtok == Decimal("0")
    assert info.output_price_per_mtok == Decimal("0")
    assert info.supports_tools is True


async def test_refresh_local_handles_unreachable_server(monkeypatch):
    _patch_settings(monkeypatch, _settings())
    fake_client = _FakeGetClient(exc=httpx.ConnectError("connection refused"))
    monkeypatch.setattr(catalog_module.httpx, "AsyncClient", fake_client)

    catalog = ModelCatalog()
    await catalog.refresh_local()  # must not raise

    assert catalog.all() == catalog.all(curated_only=True)


async def test_refresh_local_ignores_entries_without_an_id(monkeypatch):
    _patch_settings(monkeypatch, _settings())
    monkeypatch.setattr(catalog_module.LocalProvider, "complete_json", _always_ok)
    fake_client = _FakeGetClient(payload={"data": [{"id": ""}, {"foo": "bar"}]})
    monkeypatch.setattr(catalog_module.httpx, "AsyncClient", fake_client)

    catalog = ModelCatalog()
    await catalog.refresh_local()
    assert catalog.all() == catalog.all(curated_only=True)


async def test_refresh_local_defaults_context_window_to_zero(monkeypatch):
    _patch_settings(monkeypatch, _settings())
    monkeypatch.setattr(catalog_module.LocalProvider, "complete_json", _always_ok)
    fake_client = _FakeGetClient(payload={"data": [{"id": "llama3.1:8b"}]})
    monkeypatch.setattr(catalog_module.httpx, "AsyncClient", fake_client)

    catalog = ModelCatalog()
    await catalog.refresh_local()
    assert catalog.get("local/llama3.1:8b").context_window == 0


# ── tool-calling probe ──────────────────────────────────────────────────────
async def test_probe_marks_supports_tools_true_on_success(monkeypatch):
    _patch_settings(monkeypatch, _settings())
    monkeypatch.setattr(catalog_module.LocalProvider, "complete_json", _always_ok)
    fake_client = _FakeGetClient(payload={"data": [{"id": "good-model"}]})
    monkeypatch.setattr(catalog_module.httpx, "AsyncClient", fake_client)

    catalog = ModelCatalog()
    await catalog.refresh_local()
    assert catalog.get("local/good-model").supports_tools is True


async def test_probe_marks_supports_tools_false_on_failure(monkeypatch):
    _patch_settings(monkeypatch, _settings())
    monkeypatch.setattr(catalog_module.LocalProvider, "complete_json", _always_fails)
    fake_client = _FakeGetClient(payload={"data": [{"id": "bad-model"}]})
    monkeypatch.setattr(catalog_module.httpx, "AsyncClient", fake_client)

    catalog = ModelCatalog()
    await catalog.refresh_local()
    assert catalog.get("local/bad-model").supports_tools is False


async def test_probe_skipped_when_disabled(monkeypatch):
    _patch_settings(monkeypatch, _settings(local_probe_tools=False))
    monkeypatch.setattr(catalog_module.LocalProvider, "complete_json", _always_fails)
    fake_client = _FakeGetClient(payload={"data": [{"id": "unverified-model"}]})
    monkeypatch.setattr(catalog_module.httpx, "AsyncClient", fake_client)

    catalog = ModelCatalog()
    await catalog.refresh_local()
    # Probe disabled: even a model that would fail the probe is trusted.
    assert catalog.get("local/unverified-model").supports_tools is True


async def test_probe_result_is_cached_per_model_id(monkeypatch):
    _patch_settings(monkeypatch, _settings())
    calls = {"n": 0}

    async def counting_probe(*args, **kwargs):
        calls["n"] += 1
        return {"ok": True}

    monkeypatch.setattr(catalog_module.LocalProvider, "complete_json", counting_probe)
    fake_client = _FakeGetClient(payload={"data": [{"id": "cached-model"}]})
    monkeypatch.setattr(catalog_module.httpx, "AsyncClient", fake_client)

    catalog = ModelCatalog()
    catalog._local_fetched_at = 0.0
    await catalog.refresh_local()
    # Force a second refresh past the TTL without touching the probe cache.
    catalog._local_fetched_at = 0.0
    await catalog.refresh_local()
    assert calls["n"] == 1  # probed once, cached thereafter


async def test_force_bypasses_both_the_ttl_and_the_probe_cache(monkeypatch):
    """The diagnostic path: fresh discovery + fresh probe, TTL notwithstanding."""
    _patch_settings(monkeypatch, _settings())
    calls = {"n": 0}

    async def counting_probe(*args, **kwargs):
        calls["n"] += 1
        return {"ok": True}

    monkeypatch.setattr(catalog_module.LocalProvider, "complete_json", counting_probe)
    fake_client = _FakeGetClient(payload={"data": [{"id": "m"}]})
    monkeypatch.setattr(catalog_module.httpx, "AsyncClient", fake_client)

    catalog = ModelCatalog()
    await catalog.refresh_local()
    assert len(fake_client.urls) == 1 and calls["n"] == 1
    # Well inside the TTL: the normal path re-uses everything...
    await catalog.refresh_local()
    assert len(fake_client.urls) == 1 and calls["n"] == 1
    # ...and force re-fetches and re-probes anyway.
    result = await catalog.refresh_local(force=True)
    assert len(fake_client.urls) == 2 and calls["n"] == 2
    assert result.reachable is True
    assert result.cached is False
    assert [m.id for m in result.models] == ["local/m"]


async def test_discovery_result_reports_why_nothing_was_found(monkeypatch):
    _patch_settings(monkeypatch, _settings())
    fake_client = _FakeGetClient(exc=httpx.ConnectError("[Errno 61] Connection refused"))
    monkeypatch.setattr(catalog_module.httpx, "AsyncClient", fake_client)

    result = await ModelCatalog().refresh_local(force=True)
    assert result.configured is True
    assert result.reachable is False
    assert result.error is not None
    assert result.error.startswith("ConnectError:")
    assert result.models == []


async def test_discovery_result_when_no_base_url_is_configured(monkeypatch):
    _patch_settings(monkeypatch, _settings(local_base_url=""))
    result = await ModelCatalog().refresh_local(force=True)
    assert result.configured is False
    assert result.reachable is False
    assert result.error is None


# ── POST /api/settings/providers/local/test ──────────────────────────────────
def _test_client(monkeypatch, settings: Settings, catalog: ModelCatalog | None = None):
    """The settings router with auth stubbed and a throwaway catalog."""
    _patch_settings(monkeypatch, settings)  # what catalog.py reads
    monkeypatch.setattr(settings_api, "get_settings", lambda: settings)
    catalog = catalog or ModelCatalog()
    monkeypatch.setattr(settings_api, "get_catalog", lambda: catalog)
    app = FastAPI()
    app.include_router(settings_api.router)
    app.dependency_overrides[require_admin] = lambda: None
    return TestClient(app)


LOCAL_TEST_PATH = "/api/settings/providers/local/test"


def test_local_test_reports_unconfigured(monkeypatch):
    client = _test_client(monkeypatch, _settings(local_base_url=""))
    body = client.post(LOCAL_TEST_PATH).json()
    assert body["configured"] is False
    assert body["base_url"] is None
    assert body["reachable"] is False
    assert body["error"] is None
    assert body["models"] == []
    assert body["counts"] == {"models": 0, "tool_capable": 0, "no_tools": 0}


def test_local_test_reports_an_unreachable_server(monkeypatch):
    fake_client = _FakeGetClient(exc=httpx.ConnectError("[Errno 61] Connection refused"))
    monkeypatch.setattr(catalog_module.httpx, "AsyncClient", fake_client)
    client = _test_client(monkeypatch, _settings())

    body = client.post(LOCAL_TEST_PATH).json()
    assert body["configured"] is True
    assert body["base_url"] == "http://localhost:11434/v1"
    assert body["reachable"] is False
    assert "ConnectError" in body["error"]
    assert "Connection refused" in body["error"]
    assert body["models"] == []


def test_local_test_happy_path_with_mixed_probe_results(monkeypatch):
    async def probe_by_model(*args, **kwargs):
        if kwargs.get("model", "").startswith("good"):
            return {"ok": True}
        raise RuntimeError("model ignored tool_choice")

    monkeypatch.setattr(catalog_module.LocalProvider, "complete_json", probe_by_model)
    fake_client = _FakeGetClient(
        payload={
            "data": [
                {"id": "good-model", "context_length": 32768},
                {"id": "toolless-model"},
            ]
        }
    )
    monkeypatch.setattr(catalog_module.httpx, "AsyncClient", fake_client)
    client = _test_client(monkeypatch, _settings())

    body = client.post(LOCAL_TEST_PATH).json()
    assert body["reachable"] is True
    assert body["error"] is None
    assert body["counts"] == {"models": 2, "tool_capable": 1, "no_tools": 1}
    good, toolless = body["models"]  # sorted by id
    assert good == {
        "id": "local/good-model",
        "display_name": "Local: good-model",
        "supports_tools": True,
        "context_window": 32768,
    }
    assert toolless["id"] == "local/toolless-model"
    assert toolless["supports_tools"] is False
    assert toolless["context_window"] == 0  # unreported, never guessed


def test_local_test_never_accepts_a_client_supplied_url(monkeypatch):
    """SSRF stance: the only URL this endpoint fetches is the server's own config."""
    monkeypatch.setattr(catalog_module.LocalProvider, "complete_json", _always_ok)
    fake_client = _FakeGetClient(payload={"data": [{"id": "good-model"}]})
    monkeypatch.setattr(catalog_module.httpx, "AsyncClient", fake_client)
    client = _test_client(monkeypatch, _settings())

    attacks = [
        {"base_url": "http://169.254.169.254/latest/meta-data/v1"},
        {"url": "http://internal-admin.svc.cluster.local/v1"},
        {"local_base_url": "http://127.0.0.1:5432/v1"},
    ]
    for attack in attacks:
        res = client.post(LOCAL_TEST_PATH, json=attack)
        assert res.status_code == 200  # extra JSON is ignored, not honored
        assert res.json()["base_url"] == "http://localhost:11434/v1"
    # Also as query params, and every fetch went to the configured server only.
    res = client.post(f"{LOCAL_TEST_PATH}?base_url=http://169.254.169.254/v1")
    assert res.json()["base_url"] == "http://localhost:11434/v1"
    assert set(fake_client.urls) == {"http://localhost:11434/v1/models"}

    # And the contract is declared, not just enforced: no body, no parameters.
    spec = client.app.openapi()["paths"][LOCAL_TEST_PATH]["post"]
    assert "requestBody" not in spec
    assert spec.get("parameters", []) == []


def test_local_test_caps_total_time(monkeypatch):
    fake_client = _FakeGetClient(payload={"data": []}, delay=5.0)
    monkeypatch.setattr(catalog_module.httpx, "AsyncClient", fake_client)
    monkeypatch.setattr(settings_api, "LOCAL_TEST_TIMEOUT_SECONDS", 0.05)
    client = _test_client(monkeypatch, _settings())

    body = client.post(LOCAL_TEST_PATH).json()
    assert body["configured"] is True
    assert body["reachable"] is False
    assert "Timeout" in body["error"]


# ── router candidacy ─────────────────────────────────────────────────────────
def _policy(**over) -> dict:
    base = {"mode": "auto", "max_cost_tier": "premium"}
    base.update(over)
    return base


class _Registry(ProviderRegistry):
    def __init__(self, providers: set[str]):
        self._providers = providers

    def has_key(self, provider: str) -> bool:
        return provider in self._providers


def _local_model(supports_tools: bool = True) -> ModelInfo:
    return ModelInfo(
        id="local/qwen2.5:14b-instruct",
        provider="local",
        wire_id="qwen2.5:14b-instruct",
        display_name="Local: qwen2.5:14b-instruct",
        context_window=32768,
        input_price_per_mtok=Decimal("0"),
        output_price_per_mtok=Decimal("0"),
        cost_tier="local",
        supports_tools=supports_tools,
        curated=False,
    )


def test_local_tier_ranks_below_economy():
    assert TIER_ORDER["local"] < TIER_ORDER["economy"]


def test_tool_capable_local_model_is_a_router_candidate():
    catalog = ModelCatalog()
    catalog._local = {"local/qwen2.5:14b-instruct": _local_model(supports_tools=True)}
    registry = _Registry({"local"})
    router = ModelRouter(catalog, registry)
    ids = [m.id for m in router._candidates(_policy(max_cost_tier="economy"))]
    assert "local/qwen2.5:14b-instruct" in ids  # local tier bypasses the economy cap


def test_local_is_a_valid_max_cost_tier():
    """`max_cost_tier: local` is the zero-cloud policy, so the API must accept it."""
    _validate_policy({"mode": "auto", "max_cost_tier": "local"})
    for tier in TIER_ORDER:
        _validate_policy({"mode": "auto", "max_cost_tier": tier})


def test_an_unknown_cost_tier_is_still_rejected():
    with pytest.raises(HTTPException) as exc:
        _validate_policy({"mode": "auto", "max_cost_tier": "free"})
    assert exc.value.status_code == 422
    assert "local" in exc.value.detail  # the message lists the allowed values


def test_capping_at_local_leaves_only_local_models():
    """The point of the tier: no cloud candidate can survive the cap."""
    catalog = ModelCatalog()
    catalog._local = {"local/qwen2.5:14b-instruct": _local_model()}
    registry = _Registry({"local", "anthropic", "openrouter", "kimi"})
    router = ModelRouter(catalog, registry)

    ids = [m.id for m in router._candidates(_policy(max_cost_tier="local"))]
    assert ids == ["local/qwen2.5:14b-instruct"]
    # ...and the same catalog does offer cloud models when the cap is lifted.
    assert len([m for m in router._candidates(_policy()) if m.provider != "local"]) > 0


def test_probe_failed_local_model_is_excluded_from_candidacy():
    catalog = ModelCatalog()
    catalog._local = {"local/bad-model": _local_model(supports_tools=False)}
    registry = _Registry({"local"})
    router = ModelRouter(catalog, registry)
    ids = [m.id for m in router._candidates(_policy())]
    assert "local/bad-model" not in ids


def test_local_model_excluded_without_a_registered_key():
    catalog = ModelCatalog()
    catalog._local = {"local/qwen2.5:14b-instruct": _local_model(supports_tools=True)}
    registry = _Registry(set())  # no "local" key/base_url registered
    router = ModelRouter(catalog, registry)
    ids = [m.id for m in router._candidates(_policy())]
    assert ids == []


# ── local-only fallback ──────────────────────────────────────────────────────
def test_fallback_picks_local_model_when_it_is_the_only_thing_available():
    catalog = ModelCatalog()
    catalog._local = {"local/qwen2.5:14b-instruct": _local_model(supports_tools=True)}
    registry = _Registry({"local"})  # no cloud providers at all
    chosen = fallback_model("verdict", catalog, registry)
    assert chosen == "local/qwen2.5:14b-instruct"


def test_fallback_still_prefers_curated_cloud_models_when_available():
    catalog = ModelCatalog()
    catalog._local = {"local/qwen2.5:14b-instruct": _local_model(supports_tools=True)}
    registry = _Registry({"local", "openrouter"})
    chosen = fallback_model("verdict", catalog, registry)
    assert chosen == "openrouter/openai/gpt-5.6-terra"  # curated FALLBACK_TABLE entry wins


def test_fallback_skips_local_model_that_failed_the_probe():
    catalog = ModelCatalog()
    catalog._local = {"local/bad-model": _local_model(supports_tools=False)}
    registry = _Registry({"local"})
    assert fallback_model("verdict", catalog, registry) is None
