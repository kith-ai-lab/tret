"""The kill switch over HTTP, and what the tool list says about it.

The property under test is the asymmetry: an admin can narrow egress from a
running deployment and cannot widen it. If that ever inverts, the environment
stops being the ceiling and the switch is only as strong as the weakest admin
session.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tret.api import settings as settings_api
from tret.api.auth import current_user, require_admin
from tret.config import get_settings
from tret.db.engine import get_db
from tret.net import policy


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("TRET_EGRESS", "on")
    monkeypatch.setenv("TRET_EGRESS_RESEARCH", "on")
    get_settings.cache_clear()
    policy.clear_all_runtime_overrides()

    class _Admin:
        email = "admin@example.com"

    app = FastAPI()
    app.include_router(settings_api.router)
    app.dependency_overrides[current_user] = lambda: _Admin()
    app.dependency_overrides[require_admin] = lambda: _Admin()
    app.dependency_overrides[get_db] = lambda: None
    with TestClient(app) as c:
        yield c
    policy.clear_all_runtime_overrides()
    get_settings.cache_clear()


def test_status_reports_every_class(client):
    body = client.get("/api/settings/egress").json()
    assert set(body["classes"]) == {
        "provider", "catalog", "local", "research", "search", "telemetry",
    }
    assert body["classes"]["research"]["mode"] == "on"
    # Status must report the mode actually ENFORCED, not just what `search`
    # mirrors from `research`. With research on but no search backend
    # configured, policy_for() pins `search`'s mode to off (nothing to
    # reach) — the status endpoint has to agree with that, not with
    # effective_mode()'s "search rides research" rule in isolation.
    assert body["classes"]["search"]["mode"] == "off"
    assert body["search_backend"] == "none"


def test_status_reports_search_as_on_once_a_backend_is_configured(client, monkeypatch):
    monkeypatch.setenv("TRET_SEARCH_PROVIDER", "searxng")
    monkeypatch.setenv("TRET_SEARXNG_BASE_URL", "http://searxng:8080")
    get_settings.cache_clear()
    try:
        body = client.get("/api/settings/egress").json()
        assert body["classes"]["search"]["mode"] == "on"
        assert body["classes"]["search"]["allow_hosts"] == ["searxng"]
    finally:
        get_settings.cache_clear()


def test_an_admin_can_cut_a_class(client):
    assert client.post(
        "/api/settings/egress", json={"egress_class": "research", "mode": "off"}
    ).json()["mode"] == "off"
    assert client.get("/api/settings/egress").json()["classes"]["research"]["mode"] == "off"


def test_widening_past_the_environment_is_reported_honestly(client, monkeypatch):
    """Accepted, ineffective, and *said so* — the response carries the mode in
    force, never the mode requested, so a UI cannot show egress that is not on."""
    monkeypatch.setenv("TRET_EGRESS_RESEARCH", "off")
    get_settings.cache_clear()
    body = client.post(
        "/api/settings/egress", json={"egress_class": "research", "mode": "on"}
    ).json()
    assert body["requested"] == "on"
    assert body["mode"] == "off"


def test_clearing_an_override_returns_to_the_environment(client):
    client.post("/api/settings/egress", json={"egress_class": "research", "mode": "off"})
    assert client.delete("/api/settings/egress/research").json()["mode"] == "on"


def test_unknown_classes_and_modes_are_refused(client):
    assert client.post(
        "/api/settings/egress", json={"egress_class": "everything", "mode": "off"}
    ).status_code == 422
    assert client.post(
        "/api/settings/egress", json={"egress_class": "research", "mode": "maybe"}
    ).status_code == 422


def test_the_tool_list_marks_web_tools_unavailable_when_research_is_off(client):
    client.post("/api/settings/egress", json={"egress_class": "research", "mode": "off"})
    tools = {t["name"]: t for t in client.get("/api/tools").json()}
    # Still listed — a harness that names it is valid config, just not runnable here.
    assert tools["web_search"]["available"] is False
    assert "TRET_EGRESS_RESEARCH" in tools["web_search"]["unavailable_reason"]
    assert tools["read_document"]["available"] is True
    assert tools["read_document"]["unavailable_reason"] is None
