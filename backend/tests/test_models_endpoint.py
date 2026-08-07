"""`GET /api/models`: the model picker's catalog, and its time cap.

Discovery talks to OpenRouter and to the operator's local inference server, and
local discovery probes every model it finds for tool support. That is a lot of
other people's latency inside one request, so the endpoint caps it — the
diagnostic endpoint beside it (`POST /api/settings/providers/local/test`) has had
a cap for exactly this reason, and this one had none.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bench.api import settings as settings_api
from bench.api.auth import current_user
from bench.db.engine import get_db
from bench.providers.catalog import ModelInfo


def _model(model_id: str, provider: str) -> ModelInfo:
    return ModelInfo(
        id=model_id,
        provider=provider,
        wire_id=model_id.split("/")[-1],
        display_name=model_id,
        context_window=200_000,
        input_price_per_mtok=Decimal("3"),
        output_price_per_mtok=Decimal("15"),
        cost_tier="standard",
        energy_class="L",
    )


class FakeCatalog:
    """Records what was called, and can hang on demand."""

    def __init__(self, *, dynamic_delay=0.0, local_delay=0.0):
        self.models = [_model("anthropic/one", "anthropic"), _model("local/two", "local")]
        self.dynamic_delay = dynamic_delay
        self.local_delay = local_delay
        self.finished: list[str] = []

    async def refresh_dynamic(self):
        await asyncio.sleep(self.dynamic_delay)
        self.finished.append("dynamic")

    async def refresh_local(self, *, force: bool = False):
        await asyncio.sleep(self.local_delay)
        self.finished.append("local")

    def all(self):
        return list(self.models)


class FakeResult:
    def scalars(self):
        return self

    def all(self):
        return []


class FakeSession:
    async def execute(self, *_a, **_kw):
        return FakeResult()


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(settings_api.router)
    app.dependency_overrides[get_db] = lambda: FakeSession()
    app.dependency_overrides[current_user] = lambda: None
    return TestClient(app)


def _install(monkeypatch, catalog: FakeCatalog) -> FakeCatalog:
    monkeypatch.setattr(settings_api, "get_catalog", lambda: catalog)
    return catalog


def test_the_catalog_is_returned_with_availability_per_provider(client, monkeypatch):
    catalog = _install(monkeypatch, FakeCatalog())
    response = client.get("/api/models")
    assert response.status_code == 200
    body = response.json()
    assert [m["id"] for m in body] == ["anthropic/one", "local/two"]
    assert all("available" in m for m in body)
    assert set(catalog.finished) == {"dynamic", "local"}  # both refreshes ran


def test_a_hung_local_server_does_not_hang_the_endpoint(client, monkeypatch):
    """The regression: no cap meant one unresponsive server stalled the picker."""
    catalog = _install(monkeypatch, FakeCatalog(local_delay=30.0))
    monkeypatch.setattr(settings_api, "MODELS_DISCOVERY_TIMEOUT_SECONDS", 0.05)

    response = client.get("/api/models")
    assert response.status_code == 200
    assert [m["id"] for m in response.json()] == ["anthropic/one", "local/two"]
    assert "local" not in catalog.finished  # abandoned, not awaited


def test_a_hung_openrouter_fetch_does_not_hang_the_endpoint(client, monkeypatch):
    catalog = _install(monkeypatch, FakeCatalog(dynamic_delay=30.0))
    monkeypatch.setattr(settings_api, "MODELS_DISCOVERY_TIMEOUT_SECONDS", 0.05)
    assert client.get("/api/models").status_code == 200
    assert "dynamic" not in catalog.finished


def test_the_timeout_is_logged_so_a_degraded_catalog_is_explicable(client, monkeypatch, caplog):
    _install(monkeypatch, FakeCatalog(local_delay=30.0))
    monkeypatch.setattr(settings_api, "MODELS_DISCOVERY_TIMEOUT_SECONDS", 0.05)
    with caplog.at_level("WARNING", logger="bench.settings"):
        client.get("/api/models")
    assert "model discovery did not finish" in caplog.text
    assert "BENCH_LOCAL_BASE_URL" in caplog.text


def test_the_two_refreshes_run_concurrently(client, monkeypatch):
    """Serial refreshes would make the cap the sum of two servers' patience."""
    catalog = _install(monkeypatch, FakeCatalog(dynamic_delay=0.15, local_delay=0.15))
    monkeypatch.setattr(settings_api, "MODELS_DISCOVERY_TIMEOUT_SECONDS", 0.25)
    assert client.get("/api/models").status_code == 200
    assert set(catalog.finished) == {"dynamic", "local"}


def test_an_anonymous_request_cannot_read_the_catalog(monkeypatch):
    _install(monkeypatch, FakeCatalog())
    app = FastAPI()
    app.include_router(settings_api.router)
    app.dependency_overrides[get_db] = lambda: FakeSession()
    assert TestClient(app).get("/api/models").status_code == 401
