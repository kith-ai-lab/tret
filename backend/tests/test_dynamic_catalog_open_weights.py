"""Live OpenRouter catalog: fetched without a global key, TTL setting, open
weights from `hugging_face_id`, and the zero-data-retention (ZDR) endpoint set.

No network: `open_client` is replaced with a fake that answers by URL.
"""
from __future__ import annotations

import asyncio

import pytest

import tret.providers.catalog as catalog_module
from tret.config import Settings
from tret.providers.catalog import ModelCatalog, _zdr_tool_model_ids

MODELS_URL = "https://openrouter.ai/api/v1/models"
ZDR_URL = "https://openrouter.ai/api/v1/endpoints/zdr"


def _entry(model_id: str, **over) -> dict:
    base = {
        "id": model_id,
        "name": model_id,
        "created": 1_760_000_000,
        "context_length": 128_000,
        "supported_parameters": ["tools"],
        "pricing": {"prompt": "0.000001", "completion": "0.000002"},
    }
    base.update(over)
    return base


class _Resp:
    def __init__(self, payload, error: Exception | None = None):
        self._payload = payload
        self._error = error

    def raise_for_status(self):
        if self._error:
            raise self._error

    def json(self):
        return self._payload


class _Client:
    def __init__(self, routes: dict, calls: list):
        self._routes = routes
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, **kwargs):
        self._calls.append((url, kwargs))
        route = self._routes[url]
        if isinstance(route, Exception):
            raise route
        return _Resp(route)


def _curated_zdr_rows(except_wire: str | None = None, tag: str | None = None) -> list[dict]:
    rows = []
    for info in ModelCatalog()._static.values():
        if info.provider == "openrouter" and info.wire_id != except_wire:
            row = {"model_id": info.wire_id, "supported_parameters": ["tools"]}
            if tag:
                row["tag"] = tag
            rows.append(row)
    return rows


def _install(monkeypatch, routes: dict, **settings_kwargs) -> list:
    calls: list = []
    settings_kwargs.setdefault("openrouter_catalog_public_fetch", True)
    settings = Settings(openrouter_catalog=True, openrouter_api_key="", **settings_kwargs)
    monkeypatch.setattr(catalog_module, "get_settings", lambda: settings)
    monkeypatch.setattr(catalog_module, "open_client", lambda *a, **k: _Client(routes, calls))
    return calls


async def test_fetches_without_a_global_openrouter_key(monkeypatch):
    calls = _install(
        monkeypatch, {MODELS_URL: {"data": [_entry("vendor/new")]}, ZDR_URL: {"data": []}}
    )
    catalog = ModelCatalog()
    await catalog.refresh_dynamic()
    assert "openrouter/vendor/new" in catalog._dynamic
    assert [c[0] for c in calls] == [MODELS_URL, ZDR_URL]


async def test_keyless_install_makes_no_call_unless_public_fetch_is_enabled(monkeypatch):
    calls = _install(monkeypatch, {}, openrouter_catalog_public_fetch=False)
    catalog = ModelCatalog()
    await catalog.refresh_dynamic()
    assert calls == [] and catalog._dynamic == {}
    assert Settings().openrouter_catalog_public_fetch is False
    monkeypatch.setenv("TRET_OPENROUTER_CATALOG_PUBLIC_FETCH", "true")
    assert Settings().openrouter_catalog_public_fetch is True


async def test_a_global_openrouter_key_enables_the_fetch(monkeypatch):
    calls = _install(
        monkeypatch,
        {MODELS_URL: {"data": [_entry("vendor/new")]}, ZDR_URL: {"data": []}},
        openrouter_catalog_public_fetch=False,
    )
    keyed = Settings(
        openrouter_catalog=True, openrouter_api_key="sk-test", openrouter_catalog_public_fetch=False
    )
    monkeypatch.setattr(catalog_module, "get_settings", lambda: keyed)
    catalog = ModelCatalog()
    await catalog.refresh_dynamic()
    assert "openrouter/vendor/new" in catalog._dynamic and calls


async def test_catalog_flag_off_fetches_nothing(monkeypatch):
    calls = _install(monkeypatch, {})
    monkeypatch.setattr(
        catalog_module, "get_settings", lambda: Settings(openrouter_catalog=False)
    )
    catalog = ModelCatalog()
    await catalog.refresh_dynamic()
    assert calls == [] and catalog._dynamic == {}


def test_ttl_setting_default_env_and_clamp(monkeypatch):
    assert Settings().openrouter_catalog_ttl_seconds == 21600
    assert Settings(openrouter_catalog_ttl_seconds=10).openrouter_catalog_ttl_seconds == 300
    monkeypatch.setenv("TRET_OPENROUTER_CATALOG_TTL_SECONDS", "900")
    assert Settings().openrouter_catalog_ttl_seconds == 900


async def test_ttl_controls_the_cache(monkeypatch):
    calls = _install(
        monkeypatch,
        {MODELS_URL: {"data": [_entry("vendor/new")]}, ZDR_URL: {"data": []}},
        openrouter_catalog_ttl_seconds=600,
    )
    now = [1000.0]
    monkeypatch.setattr(catalog_module.time, "monotonic", lambda: now[0])
    catalog = ModelCatalog()
    await catalog.refresh_dynamic()
    n = len(calls)
    now[0] += 599
    await catalog.refresh_dynamic()
    assert len(calls) == n  # still cached
    now[0] += 2
    await catalog.refresh_dynamic()
    assert len(calls) > n  # expired


async def test_hugging_face_id_sets_open_weights_and_hf_id(monkeypatch):
    _install(
        monkeypatch,
        {
            MODELS_URL: {
                "data": [
                    _entry("lab/open-one", hugging_face_id="lab/Open-One"),
                    _entry("lab/closed-one", hugging_face_id=""),
                    _entry("lab/closed-two"),
                ]
            },
            ZDR_URL: {"data": []},
        },
    )
    catalog = ModelCatalog()
    await catalog.refresh_dynamic()
    open_one = catalog._dynamic["openrouter/lab/open-one"]
    assert open_one.open_weights is True and open_one.hf_id == "lab/Open-One"
    assert open_one.created_ts == 1_760_000_000
    for name in ("closed-one", "closed-two"):
        m = catalog._dynamic[f"openrouter/lab/{name}"]
        assert m.open_weights is False and m.hf_id is None


async def test_static_entry_unset_open_weights_is_filled_from_the_listing(monkeypatch):
    catalog = ModelCatalog()
    static = catalog.get("openrouter/google/gemini-3.6-flash")
    static.open_weights = None
    kimi = catalog.get("openrouter/moonshotai/kimi-k3")
    assert kimi.open_weights is True and kimi.hf_id == "moonshotai/Kimi-K3"
    _install(
        monkeypatch,
        {
            MODELS_URL: {"data": [_entry("google/gemini-3.6-flash", hugging_face_id="g/x")]},
            ZDR_URL: {"data": []},
        },
    )
    await catalog.refresh_dynamic()
    assert static.open_weights is True and static.hf_id == "g/x"


def test_yaml_open_weights_flags():
    c = ModelCatalog()
    assert c.get("anthropic/claude-sonnet-5").open_weights is False
    assert c.get("openrouter/openai/gpt-5.6-terra").open_weights is False
    assert c.get("kimi/kimi-k2").open_weights is True
    assert c.get("kimi/kimi-k2").hf_id == "moonshotai/Kimi-K2-Instruct"
    assert c.get("kimi/kimi-latest").open_weights is None
    assert c.get("openrouter/deepseek/deepseek-v4-pro").hf_id == "deepseek-ai/DeepSeek-V4-Pro"


def test_zdr_payload_parsing():
    payload = {
        "data": [
            {"model_id": "a/with-tools", "provider_name": "P1", "supported_parameters": ["tools", "x"]},
            {"model_id": "a/with-tools", "provider_name": "P2", "supported_parameters": []},
            {"model_id": "b/no-tools", "provider_name": "P1", "supported_parameters": ["temperature"]},
            {"model_id": "c/missing-params"},
            "junk",
        ]
    }
    assert _zdr_tool_model_ids(payload) == {"a/with-tools"}
    # Unusable payloads are "unknown", never "empty".
    assert _zdr_tool_model_ids({"data": []}) is None
    assert _zdr_tool_model_ids({"data": [{"model_id": "b/x", "supported_parameters": []}]}) is None
    assert _zdr_tool_model_ids([]) is None
    assert _zdr_tool_model_ids({"data": "x"}) is None


async def test_zdr_flag_true_false_and_static_entries(monkeypatch):
    _install(
        monkeypatch,
        {
            MODELS_URL: {"data": [_entry("a/with-tools"), _entry("b/without")]},
            ZDR_URL: {
                "data": [
                    {"model_id": "a/with-tools", "supported_parameters": ["tools"]},
                    {"model_id": "other/x", "supported_parameters": ["tools"]},
                    # Every curated OpenRouter model but one has a ZDR endpoint,
                    # so the list passes the format sanity check.
                    *_curated_zdr_rows(except_wire="openai/gpt-5.6-terra"),
                ]
            },
        },
    )
    catalog = ModelCatalog()
    assert catalog.get("openrouter/moonshotai/kimi-k3").zdr_tools_endpoint is None
    await catalog.refresh_dynamic()
    assert catalog._dynamic["openrouter/a/with-tools"].zdr_tools_endpoint is True
    assert catalog._dynamic["openrouter/b/without"].zdr_tools_endpoint is False
    assert catalog.get("openrouter/moonshotai/kimi-k3").zdr_tools_endpoint is True
    assert catalog.get("openrouter/openai/gpt-5.6-terra").zdr_tools_endpoint is False
    # Non-OpenRouter static entries are untouched.
    assert catalog.get("anthropic/claude-sonnet-5").zdr_tools_endpoint is None


@pytest.mark.parametrize("zdr_route", [RuntimeError("never"), {"data": []}, {"nope": 1}])
async def test_zdr_failure_leaves_the_flag_unknown(monkeypatch, zdr_route):
    import httpx

    route = httpx.ConnectError("boom") if isinstance(zdr_route, RuntimeError) else zdr_route
    _install(monkeypatch, {MODELS_URL: {"data": [_entry("a/m")]}, ZDR_URL: route})
    catalog = ModelCatalog()
    await catalog.refresh_dynamic()
    assert catalog._dynamic["openrouter/a/m"].zdr_tools_endpoint is None


def test_to_json_exposes_open_weights_fields():
    body = ModelCatalog().get("openrouter/moonshotai/kimi-k3").to_json()
    assert body["open_weights"] is True
    assert body["hf_id"] == "moonshotai/Kimi-K3"
    assert "zdr_tools_endpoint" in body


def test_zdr_list_that_misses_the_curated_models_is_unknown():
    curated = {i.wire_id for i in ModelCatalog()._static.values() if i.provider == "openrouter"}
    # A format change upstream: rows parse, but none are our models.
    drift = {"data": [{"model_id": "zz/other", "supported_parameters": ["tools"]}]}
    assert _zdr_tool_model_ids(drift, None, curated) is None
    # Fewer than half of the curated ids present: still unknown.
    few = {"data": [{"model_id": w, "supported_parameters": ["tools"]} for w in sorted(curated)[:1]]}
    assert _zdr_tool_model_ids(few, None, curated) is None
    full = {"data": _curated_zdr_rows()}
    assert _zdr_tool_model_ids(full, None, curated) == curated


async def test_zdr_drift_leaves_flags_unknown_instead_of_false(monkeypatch):
    _install(
        monkeypatch,
        {
            MODELS_URL: {"data": [_entry("a/m")]},
            ZDR_URL: {"data": [{"model_id": "zz/other", "supported_parameters": ["tools"]}]},
        },
    )
    catalog = ModelCatalog()
    await catalog.refresh_dynamic()
    assert catalog._dynamic["openrouter/a/m"].zdr_tools_endpoint is None
    assert catalog.get("openrouter/openai/gpt-5.6-terra").zdr_tools_endpoint is None


def test_zdr_honours_provider_ignore_and_only():
    rows = {
        "data": [
            {"model_id": "a/x", "tag": "badco/us", "supported_parameters": ["tools"]},
            {"model_id": "b/y", "tag": "goodco", "supported_parameters": ["tools"]},
            {"model_id": "c/z", "provider_name": "GoodCo", "supported_parameters": ["tools"]},
            {"model_id": "d/w", "tag": "goodco", "supported_parameters": ["tools"]},
            {"model_id": "d/w", "tag": "BadCo", "supported_parameters": ["tools"]},
        ]
    }
    assert _zdr_tool_model_ids(rows, {"ignore": ["badco"]}) == {"b/y", "c/z", "d/w"}
    assert _zdr_tool_model_ids(rows, {"only": ["goodco"]}) == {"b/y", "c/z", "d/w"}
    assert _zdr_tool_model_ids(rows, {"only": "badco"}) == {"a/x", "d/w"}
    assert _zdr_tool_model_ids(rows, None) == {"a/x", "b/y", "c/z", "d/w"}


async def test_concurrent_refreshes_share_one_download(monkeypatch):
    gate = asyncio.Event()
    calls = _install(
        monkeypatch, {MODELS_URL: {"data": [_entry("vendor/new")]}, ZDR_URL: {"data": []}}
    )

    class _SlowClient(_Client):
        async def get(self, url, **kwargs):
            await gate.wait()
            return await super().get(url, **kwargs)

    monkeypatch.setattr(
        catalog_module,
        "open_client",
        lambda *a, **k: _SlowClient(
            {MODELS_URL: {"data": [_entry("vendor/new")]}, ZDR_URL: {"data": []}}, calls
        ),
    )
    catalog = ModelCatalog()
    tasks = [asyncio.create_task(catalog.refresh_dynamic()) for _ in range(5)]
    await asyncio.sleep(0.05)
    gate.set()
    await asyncio.gather(*tasks)
    assert [c[0] for c in calls] == [MODELS_URL, ZDR_URL]  # one download, not five
    assert "openrouter/vendor/new" in catalog._dynamic


async def test_a_failed_download_is_shared_by_waiters_and_retried_next_call(monkeypatch):
    import httpx

    routes = {MODELS_URL: httpx.ConnectError("boom"), ZDR_URL: {"data": []}}
    calls = _install(monkeypatch, routes)

    class _SlowClient(_Client):
        async def get(self, url, **kwargs):
            await asyncio.sleep(0.02)  # a real network wait, so callers overlap
            return await super().get(url, **kwargs)

    monkeypatch.setattr(
        catalog_module, "open_client", lambda *a, **k: _SlowClient(routes, calls)
    )
    catalog = ModelCatalog()
    await asyncio.gather(catalog.refresh_dynamic(), catalog.refresh_dynamic())
    assert len(calls) == 1
    routes[MODELS_URL] = {"data": [_entry("vendor/new")]}
    await catalog.refresh_dynamic()
    assert "openrouter/vendor/new" in catalog._dynamic


async def test_background_loop_refreshes_every_ttl_and_survives_failures(monkeypatch):
    _install(monkeypatch, {})
    catalog = ModelCatalog()
    ticks: list[str] = []

    async def fake_refresh():
        ticks.append("tick")
        if len(ticks) == 1:
            raise RuntimeError("upstream down")

    real_sleep = asyncio.sleep
    sleeps: list[float] = []

    async def fast_sleep(seconds):
        sleeps.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(catalog, "refresh_dynamic", fake_refresh)
    monkeypatch.setattr(catalog_module.asyncio, "sleep", fast_sleep)
    task = asyncio.create_task(catalog_module.dynamic_refresh_loop(catalog))
    for _ in range(20):
        await real_sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(ticks) >= 2  # the first failure did not kill the loop
    assert sleeps[0] == catalog.dynamic_ttl_seconds()


def test_single_flight_lock_works_across_separate_event_loops(monkeypatch):
    calls = _install(
        monkeypatch, {MODELS_URL: {"data": [_entry("vendor/new")]}, ZDR_URL: {"data": []}}
    )

    class _SlowClient(_Client):
        async def get(self, url, **kwargs):
            await asyncio.sleep(0.01)
            return await super().get(url, **kwargs)

    routes = {MODELS_URL: {"data": [_entry("vendor/new")]}, ZDR_URL: {"data": []}}
    monkeypatch.setattr(
        catalog_module, "open_client", lambda *a, **k: _SlowClient(routes, calls)
    )
    catalog = ModelCatalog()

    async def contend():
        catalog._dynamic_fetched_at = 0.0
        catalog._dynamic = {}
        await asyncio.gather(*(catalog.refresh_dynamic() for _ in range(3)))

    asyncio.run(contend())
    asyncio.run(contend())  # a different loop, same singleton
    assert len(calls) == 4  # one download (models + zdr) per run


async def test_background_loop_first_tick_follows_the_warmup_fetch(monkeypatch):
    _install(monkeypatch, {})
    catalog = ModelCatalog()
    ttl = catalog.dynamic_ttl_seconds()
    now = [10_000.0]
    monkeypatch.setattr(catalog_module.time, "monotonic", lambda: now[0])
    catalog._dynamic_fetched_at = now[0] - 60  # warm-up fetched a minute ago
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(catalog_module.asyncio, "sleep", fake_sleep)
    task = asyncio.create_task(catalog_module.dynamic_refresh_loop(catalog))
    for _ in range(5):
        await real_sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # Wakes just after the cache goes stale, so that tick actually refreshes.
    assert ttl - 60 < sleeps[0] <= ttl - 60 + 5
