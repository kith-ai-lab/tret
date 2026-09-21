"""Opt-in anonymous telemetry (tret/services/telemetry.py, tret/api/telemetry.py).

Real sqlite database, same harness as test_connections_service.py /
test_conversation_spend.py: state resolution, payload construction and the
admin API all touch real SQL (GROUP BY, streamed batches, InstanceState
reads/writes), which a fake session cannot stand in for. The network is
mocked by monkeypatching `tret.services.telemetry.open_client` with a fake
async context manager that records every call — the property every "zero
calls" test below defends is that a locked-off or disabled state never even
constructs one.
"""
from __future__ import annotations

import asyncio
import json
import math
import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import get_args

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from tests.evals.golden_world import install_sqlite_type_shims
from tret.api import telemetry as telemetry_api
from tret.api.auth import current_user, require_admin
from tret.config import get_settings
from tret.db.engine import get_db
from tret.db.models import Base, Harness, Pack, Project, Run, User, Workspace
from tret.engine import extensions as extensions_module
from tret.engine.extensions import ExtensionAPI
from tret.net import policy as policy_module
from tret.net.client import open_client as real_open_client
from tret.net.policy import EgressDenied

import tret.services.telemetry as telemetry

SNAPSHOT_PATH = Path(__file__).parent / "snapshots" / "telemetry_payload_v1.schema.json"

MARKER = "CANARY-MARKER-0xDEADBEEF"

# The ingest Worker's own closed factor-rung set (telemetry-ingest/src/schema.js) —
# pinned here so a change to either side is caught by this test, not discovered
# in production as a silent 400 on every report.
EXPECTED_FACTOR_RUNGS = frozenset(
    {
        "run_override", "harness", "workspace", "managed", "env", "dataset",
        "provider", "local_setting", "global_default",
    }
)


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def engine():
    install_sqlite_type_shims()
    eng = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def db(session_factory):
    async with session_factory() as session:
        yield session


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """Every test starts with no extension registry, a clean Settings cache,
    no runtime egress overrides, and the master switch ON so the `telemetry`
    egress class is reachable by default — same posture test_egress_api.py's
    fixture takes. Individual tests narrow from here."""
    extensions_module._registry = None
    monkeypatch.delenv("DO_NOT_TRACK", raising=False)
    monkeypatch.setenv("TRET_EGRESS", "on")
    get_settings.cache_clear()
    policy_module.clear_all_runtime_overrides()
    yield
    extensions_module._registry = None
    policy_module.clear_all_runtime_overrides()
    get_settings.cache_clear()


def make_workspace(name: str = "WS") -> Workspace:
    return Workspace(id=uuid.uuid4(), name=name, kind="team")


def make_project(workspace: Workspace, name: str = "P") -> Project:
    return Project(id=uuid.uuid4(), workspace_id=workspace.id, name=name)


def make_harness(workspace: Workspace, name: str = "H") -> Harness:
    return Harness(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        name=name,
        task_profile="chat",
        model_policy={"mode": "auto"},
        tool_names=[],
    )


def make_run(project_id, harness_id, **over) -> Run:
    defaults = dict(
        id=uuid.uuid4(),
        project_id=project_id,
        harness_id=harness_id,
        task_type="chat",
        task_input={},
        status="completed",
        model_used="claude-3-5-sonnet",
        provider_used="anthropic",
        input_tokens=100,
        output_tokens=50,
        cost_usd=Decimal("0"),
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(over)
    return Run(**defaults)


def _install_fake_open_client(monkeypatch, calls: list, *, status: int = 204, raise_exc: Exception | None = None):
    """Replaces `tret.services.telemetry.open_client` with a fake that records
    `(url, content, headers)` for every call and returns (or raises) exactly
    what the test asks for — never touches the network."""

    class _FakeClient:
        async def post(self, url, *, content, headers):
            calls.append({"url": url, "content": content, "headers": headers})
            if raise_exc is not None:
                raise raise_exc
            return SimpleNamespace(status_code=status)

    class _FakeCM:
        async def __aenter__(self):
            return _FakeClient()

        async def __aexit__(self, *exc):
            return False

    def _fake_open_client(egress_class, *, timeout=None):
        assert egress_class == telemetry.CLASS_TELEMETRY
        return _FakeCM()

    monkeypatch.setattr(telemetry, "open_client", _fake_open_client)


def _make_api_client(session_factory, *, forbidden: bool = False) -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(telemetry_api.router)

    class _Admin:
        email = "admin@example.com"

    async def _get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[current_user] = lambda: _Admin()
    if forbidden:

        async def _forbidden():
            raise HTTPException(status_code=403, detail="not an admin")

        app.dependency_overrides[require_admin] = _forbidden
    else:
        app.dependency_overrides[require_admin] = lambda: _Admin()

    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


# ── schema snapshot ──────────────────────────────────────────────────────────


def test_payload_schema_matches_snapshot():
    schema = telemetry.TelemetryPayload.model_json_schema()
    expected = json.loads(SNAPSHOT_PATH.read_text())
    assert schema == expected, (
        "telemetry payload v1 schema changed — update docs/telemetry.md and "
        "telemetry-ingest/src/schema.js too, then regenerate "
        "tests/snapshots/telemetry_payload_v1.schema.json"
    )


def test_factor_rung_set_matches_ingest_worker_contract():
    assert telemetry.FACTOR_RUNGS == EXPECTED_FACTOR_RUNGS


# ── bucket / sig-fig unit tests ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "n,expected",
    [(0, "0"), (1, "1"), (2, "2-5"), (5, "2-5"), (6, "6-20"), (20, "6-20"), (21, "21-100"), (100, "21-100"), (101, "100+")],
)
def test_bucket_small(n, expected):
    assert telemetry._bucket_small(n) == expected


@pytest.mark.parametrize(
    "n,expected",
    [
        (0, "0"), (1, "1-10"), (10, "1-10"), (11, "11-100"), (100, "11-100"),
        (101, "101-1000"), (1000, "101-1000"), (1001, "1001-10000"),
        (10000, "1001-10000"), (10001, "10000+"),
    ],
)
def test_bucket_runs(n, expected):
    assert telemetry._bucket_runs(n) == expected


@pytest.mark.parametrize(
    "x,expected",
    [(0, 0.0), (1234, 1200.0), (999, 1000.0), (0.001234, 0.0012), (2100.4, 2100.0)],
)
def test_sig2(x, expected):
    assert telemetry._sig2(x) == expected


# ── instance id lifecycle ────────────────────────────────────────────────────


async def test_instance_id_lifecycle(db):
    state = await telemetry.set_enabled(db, True)
    assert state.enabled is True
    id_a = await telemetry._get_value(db, "telemetry_instance_id", None)
    assert id_a is not None and uuid.UUID(id_a).version == 4

    state = await telemetry.set_enabled(db, False)
    assert state.enabled is False
    assert await telemetry._get_value(db, "telemetry_instance_id", None) is None

    await telemetry.set_enabled(db, True)
    id_b = await telemetry._get_value(db, "telemetry_instance_id", None)
    assert id_b is not None and id_b != id_a


async def test_preview_never_mints(db):
    result = await telemetry.preview(db)
    assert result["payload"]["instance_id"] == telemetry.PREVIEW_PLACEHOLDER
    assert await telemetry._get_value(db, "telemetry_instance_id", None) is None
    assert result["would_send"] is False


# ── PII canary ────────────────────────────────────────────────────────────────


async def test_pii_canary_never_appears_and_folds_correctly(db, session_factory):
    ws = make_workspace(f"{MARKER}-workspace")
    project = make_project(ws, f"{MARKER}-project")
    harness = make_harness(ws, f"{MARKER}-harness")
    user = User(
        id=uuid.uuid4(),
        email=f"{MARKER}@example.com",
        display_name=f"{MARKER} Name",
        password_hash="x",
        role="analyst",
    )
    pack_run = make_run(
        project.id,
        harness.id,
        task_type="CANARY-task",
        task_input={"prompt": f"do something about {MARKER}"},
        model_used="acme-CANARY-model",
        provider_used="local",
    )
    accounted_run = make_run(
        project.id,
        harness.id,
        task_type="chat",
        model_used="acme-CANARY-model",
        provider_used="local",
        energy_accounting={
            "energy_wh": 1.0,
            "co2e_g": 0.5,
            "grid_co2e_source": "zone:CANARY-ZONE",
        },
    )
    async with session_factory() as seeder:
        seeder.add_all([ws, project, harness, user, pack_run, accounted_run])
        await seeder.commit()

    await telemetry.set_enabled(db, True)
    result = await telemetry.preview(db)
    dumped = json.dumps(result["payload"])
    assert MARKER not in dumped

    payload = result["payload"]
    # model_used never appearing directly: both runs fold to the "local/other"
    # family (provider "local", no known substring in "acme-CANARY-model").
    assert payload["model_families"] == {"local/other": 1.0}
    # "CANARY-task" is not chat/freeform, so it folds to "pack"; the other
    # run is an ordinary "chat" — task_types is an even split.
    assert payload["task_types"] == {"pack": 0.5, "chat": 0.5}
    # "zone:CANARY-ZONE" is not one of the nine known rungs -> "other"; the
    # pack run carries no energy_accounting at all -> "legacy".
    assert payload["factor_rungs"] == {"legacy": 0.5, "other": 0.5}
    # Also validates against the model itself (extra="forbid", closed Literals).
    telemetry.TelemetryPayload.model_validate(payload)


# ── send_once: failures, caps, preview/send parity ───────────────────────────


async def test_send_once_success_records_sent_and_last_sent_at(db, monkeypatch):
    await telemetry.set_enabled(db, True)
    calls: list = []
    _install_fake_open_client(monkeypatch, calls, status=204)
    now = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)

    result = await telemetry.send_once(db, now=now)

    assert result == {"sent": True, "http_status": 204}
    assert len(calls) == 1
    body = json.loads(calls[0]["content"])
    telemetry.TelemetryPayload.model_validate(body)
    status = await telemetry.status(db)
    assert status["last_sent_at"] == now.isoformat()
    assert len(status["recent"]) == 1
    assert status["recent"][0]["status"] == "sent"
    assert status["recent"][0]["http_status"] == 204


async def test_send_once_swallows_transport_exception(db, monkeypatch):
    await telemetry.set_enabled(db, True)
    calls: list = []
    _install_fake_open_client(monkeypatch, calls, raise_exc=RuntimeError("boom"))

    result = await telemetry.send_once(db)  # must not raise

    assert result == {"sent": False, "http_status": None}
    status = await telemetry.status(db)
    assert status["last_sent_at"] is None
    assert status["recent"][0]["status"] == "failed"
    assert status["recent"][0]["http_status"] is None


async def test_send_once_http_500_is_recorded_as_failed(db, monkeypatch):
    await telemetry.set_enabled(db, True)
    calls: list = []
    _install_fake_open_client(monkeypatch, calls, status=500)

    result = await telemetry.send_once(db)

    assert result == {"sent": False, "http_status": 500}
    status = await telemetry.status(db)
    assert status["last_sent_at"] is None
    assert status["recent"][0]["status"] == "failed"
    assert status["recent"][0]["http_status"] == 500


async def test_recent_is_capped_at_ten_newest_first(db, monkeypatch):
    await telemetry.set_enabled(db, True)
    calls: list = []
    _install_fake_open_client(monkeypatch, calls, status=204)

    base = datetime(2026, 9, 21, tzinfo=timezone.utc)
    for i in range(12):
        await telemetry.send_once(db, now=base + timedelta(hours=i))

    status = await telemetry.status(db)
    assert len(status["recent"]) == telemetry.RECENT_CAP == 10
    sent_ats = [entry["sent_at"] for entry in status["recent"]]
    assert sent_ats == sorted(sent_ats, reverse=True)
    assert sent_ats[0] == (base + timedelta(hours=11)).isoformat()


async def test_preview_bytes_match_what_send_once_transmits(db, session_factory, monkeypatch):
    ws = make_workspace()
    project = make_project(ws)
    harness = make_harness(ws)
    run = make_run(project.id, harness.id)
    async with session_factory() as seeder:
        seeder.add_all([ws, project, harness, run])
        await seeder.commit()

    await telemetry.set_enabled(db, True)
    preview_result = await telemetry.preview(db)

    calls: list = []
    _install_fake_open_client(monkeypatch, calls, status=204)
    await telemetry.send_once(db)

    assert len(calls) == 1
    sent_body = json.loads(calls[0]["content"])
    assert preview_result["payload"] == sent_body
    assert calls[0]["headers"] == {
        "Content-Type": "application/json",
        "User-Agent": "tret-telemetry/1",
    }


# ── zero network calls when telemetry is not enabled ─────────────────────────


async def test_zero_calls_default_admin_toggle_false(db, monkeypatch):
    calls: list = []
    _install_fake_open_client(monkeypatch, calls)
    result = await telemetry.send_once(db)
    assert calls == []
    assert result["sent"] is False


async def test_zero_calls_env_off(db, monkeypatch):
    monkeypatch.setenv("TRET_TELEMETRY", "off")
    get_settings.cache_clear()
    calls: list = []
    _install_fake_open_client(monkeypatch, calls)
    result = await telemetry.send_once(db)
    assert calls == []
    assert result["reason"] == "env_off"


async def test_zero_calls_do_not_track_beats_env_on(db, monkeypatch):
    monkeypatch.setenv("TRET_TELEMETRY", "on")
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    get_settings.cache_clear()
    calls: list = []
    _install_fake_open_client(monkeypatch, calls)
    result = await telemetry.send_once(db)
    assert calls == []
    assert result["reason"] == "do_not_track"


async def test_zero_calls_blank_url(db, monkeypatch):
    monkeypatch.setenv("TRET_TELEMETRY", "on")
    monkeypatch.setenv("TRET_TELEMETRY_URL", "")
    get_settings.cache_clear()
    calls: list = []
    _install_fake_open_client(monkeypatch, calls)
    result = await telemetry.send_once(db)
    assert calls == []
    assert result["reason"] == "no_url"


async def test_zero_calls_extension_override_says_off(db, monkeypatch):
    monkeypatch.setenv("TRET_TELEMETRY", "on")
    get_settings.cache_clear()
    extensions_module._registry = ExtensionAPI(None)
    extensions_module._registry.add_telemetry_override(lambda: "off")
    calls: list = []
    _install_fake_open_client(monkeypatch, calls)
    result = await telemetry.send_once(db)
    assert calls == []
    assert result["reason"] == "extension"


async def test_zero_calls_extension_override_raises(db, monkeypatch):
    monkeypatch.setenv("TRET_TELEMETRY", "on")
    get_settings.cache_clear()
    extensions_module._registry = ExtensionAPI(None)

    def _boom():
        raise RuntimeError("misbehaving extension")

    extensions_module._registry.add_telemetry_override(_boom)
    calls: list = []
    _install_fake_open_client(monkeypatch, calls)
    result = await telemetry.send_once(db)
    assert calls == []
    assert result["reason"] == "extension"


async def test_zero_calls_master_egress_off(db, monkeypatch):
    monkeypatch.setenv("TRET_TELEMETRY", "on")
    monkeypatch.setenv("TRET_EGRESS", "off")
    get_settings.cache_clear()
    calls: list = []
    _install_fake_open_client(monkeypatch, calls)
    result = await telemetry.send_once(db)
    assert calls == []
    assert result["reason"] == "egress_off"


# ── admin API ─────────────────────────────────────────────────────────────────


async def test_get_shape_keys_exact(session_factory):
    async with _make_api_client(session_factory) as client:
        resp = await client.get("/api/admin/telemetry")
        assert resp.status_code == 200
        body = resp.json()
        assert set(body.keys()) == {
            "enabled", "env_mode", "db_enabled", "locked", "locked_reason",
            "instance_id", "last_sent_at", "endpoint", "recent",
        }
        assert body == {
            "enabled": False,
            "env_mode": "admin",
            "db_enabled": False,
            "locked": False,
            "locked_reason": None,
            "instance_id": None,
            "last_sent_at": None,
            "endpoint": get_settings().telemetry_url,
            "recent": [],
        }


async def test_non_admin_is_refused_on_every_route(session_factory):
    async with _make_api_client(session_factory, forbidden=True) as client:
        assert (await client.get("/api/admin/telemetry")).status_code == 403
        assert (await client.get("/api/admin/telemetry/preview")).status_code == 403
        assert (
            await client.put("/api/admin/telemetry", json={"enabled": True})
        ).status_code == 403


async def test_put_toggles_and_mints_an_id(session_factory):
    async with _make_api_client(session_factory) as client:
        resp = await client.put("/api/admin/telemetry", json={"enabled": True})
        assert resp.status_code == 200
        body = resp.json()
        assert body["enabled"] is True
        assert body["db_enabled"] is True
        assert body["instance_id"] is not None
        assert uuid.UUID(body["instance_id"]).version == 4


async def test_put_409_when_locked_env_off(session_factory, monkeypatch):
    monkeypatch.setenv("TRET_TELEMETRY", "off")
    get_settings.cache_clear()
    async with _make_api_client(session_factory) as client:
        resp = await client.put("/api/admin/telemetry", json={"enabled": True})
        assert resp.status_code == 409
        assert "env_off" in resp.json()["detail"]


async def test_put_409_when_locked_env_on(session_factory, monkeypatch):
    monkeypatch.setenv("TRET_TELEMETRY", "on")
    get_settings.cache_clear()
    async with _make_api_client(session_factory) as client:
        resp = await client.put("/api/admin/telemetry", json={"enabled": False})
        assert resp.status_code == 409
        assert "env_on" in resp.json()["detail"]


async def test_put_409_when_locked_do_not_track(session_factory, monkeypatch):
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    get_settings.cache_clear()
    async with _make_api_client(session_factory) as client:
        resp = await client.put("/api/admin/telemetry", json={"enabled": True})
        assert resp.status_code == 409
        assert "do_not_track" in resp.json()["detail"]


# ── B1: set_enabled while locked ──────────────────────────────────────────────


async def test_set_enabled_false_while_locked_off_clears_a_stale_id_without_raising(db, monkeypatch):
    # Mint an id while unlocked, then lock off (DO_NOT_TRACK set later, same
    # as an operator setting it after the fact) — the motivating B1 scenario.
    await telemetry.set_enabled(db, True)
    assert await telemetry._get_value(db, "telemetry_instance_id", None) is not None

    monkeypatch.setenv("DO_NOT_TRACK", "1")
    get_settings.cache_clear()

    state = await telemetry.set_enabled(db, False)  # must not raise
    assert state.enabled is False
    assert state.locked_reason == "do_not_track"
    assert await telemetry._get_value(db, "telemetry_instance_id", None) is None


async def test_set_enabled_true_while_locked_off_still_raises(db, monkeypatch):
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    get_settings.cache_clear()
    with pytest.raises(telemetry.TelemetryLocked) as excinfo:
        await telemetry.set_enabled(db, True)
    assert excinfo.value.reason == "do_not_track"


async def test_set_enabled_false_while_locked_env_on_still_raises(db, monkeypatch):
    monkeypatch.setenv("TRET_TELEMETRY", "on")
    get_settings.cache_clear()
    with pytest.raises(telemetry.TelemetryLocked) as excinfo:
        await telemetry.set_enabled(db, False)
    assert excinfo.value.reason == "env_on"


# ── B1: _tick / sender_loop ───────────────────────────────────────────────────


async def test_tick_locked_off_deletes_id_with_zero_transport_calls(db, monkeypatch):
    await telemetry.set_enabled(db, True)
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    get_settings.cache_clear()
    calls: list = []
    _install_fake_open_client(monkeypatch, calls)

    await telemetry._tick(db)

    assert calls == []
    assert await telemetry._get_value(db, "telemetry_instance_id", None) is None


async def test_tick_not_due_yet_makes_zero_calls(db, monkeypatch):
    await telemetry.set_enabled(db, True)
    now = datetime.now(timezone.utc)
    await telemetry._set_value(db, "telemetry_last_sent_at", now.isoformat())
    await db.commit()
    calls: list = []
    _install_fake_open_client(monkeypatch, calls)

    await telemetry._tick(db, now=now + timedelta(hours=1))

    assert calls == []


async def test_tick_backs_off_on_a_recent_failed_attempt_even_when_last_sent_is_stale(db, monkeypatch):
    """S4: last_sent_at alone being >7 days old is not enough to be 'due' —
    a failed attempt inside the last 24h must still hold it back, or a
    permanently-failing collector gets re-POSTed to on every 6h tick."""
    await telemetry.set_enabled(db, True)
    now = datetime.now(timezone.utc)
    await telemetry._set_value(db, "telemetry_last_sent_at", (now - timedelta(days=10)).isoformat())
    await telemetry._set_value(db, "telemetry_last_attempt_at", (now - timedelta(hours=1)).isoformat())
    await db.commit()
    calls: list = []
    _install_fake_open_client(monkeypatch, calls)

    await telemetry._tick(db, now=now)

    assert calls == []


async def test_tick_retries_once_the_attempt_backoff_window_has_passed(db, monkeypatch):
    await telemetry.set_enabled(db, True)
    now = datetime.now(timezone.utc)
    await telemetry._set_value(db, "telemetry_last_sent_at", (now - timedelta(days=10)).isoformat())
    await telemetry._set_value(db, "telemetry_last_attempt_at", (now - timedelta(hours=25)).isoformat())
    await db.commit()
    calls: list = []
    _install_fake_open_client(monkeypatch, calls, status=204)

    await telemetry._tick(db, now=now)

    assert len(calls) == 1


async def test_send_once_always_records_last_attempt_at_even_on_failure(db, monkeypatch):
    await telemetry.set_enabled(db, True)
    calls: list = []
    _install_fake_open_client(monkeypatch, calls, raise_exc=RuntimeError("boom"))
    now = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)

    await telemetry.send_once(db, now=now)

    assert await telemetry._get_value(db, "telemetry_last_attempt_at", None) == now.isoformat()
    # ...but last_sent_at only ever advances on success.
    assert await telemetry._get_value(db, "telemetry_last_sent_at", None) is None


async def test_sender_loop_startup_cleanup_runs_before_the_first_sleep(session_factory, monkeypatch):
    import tret.db.engine as db_engine

    monkeypatch.setattr(db_engine, "get_session_factory", lambda: session_factory)

    async with session_factory() as seed:
        await telemetry.set_enabled(seed, True)
    async with session_factory() as check:
        assert await telemetry._get_value(check, "telemetry_instance_id", None) is not None

    # Lock off only now, simulating a state that turned locked-off while the
    # id from an earlier enabled period was still sitting there.
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    get_settings.cache_clear()

    calls: list = []
    _install_fake_open_client(monkeypatch, calls)

    sleep_calls: list = []

    async def _fake_sleep(_seconds):
        sleep_calls.append(_seconds)
        raise asyncio.CancelledError()

    monkeypatch.setattr(telemetry.asyncio, "sleep", _fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await telemetry.sender_loop()

    # The initial 5-60 minute delay was reached (and only then cancelled) —
    # meaning the locked-off cleanup pass ran, and completed, before it.
    assert len(sleep_calls) == 1
    assert calls == []
    async with session_factory() as check:
        assert await telemetry._get_value(check, "telemetry_instance_id", None) is None


async def test_sender_loop_cancellation_propagates(session_factory, monkeypatch):
    import tret.db.engine as db_engine

    monkeypatch.setattr(db_engine, "get_session_factory", lambda: session_factory)

    async def _fake_sleep(_seconds):
        raise asyncio.CancelledError()

    monkeypatch.setattr(telemetry.asyncio, "sleep", _fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await telemetry.sender_loop()


# ── window computation ────────────────────────────────────────────────────────


async def test_window_first_send_is_the_last_seven_days(db):
    now = datetime(2026, 9, 21, tzinfo=timezone.utc)
    payload = await telemetry.build_payload(db, instance_id=str(uuid.uuid4()), now=now)
    assert payload.window_start == (now - timedelta(days=7)).date().isoformat()
    assert payload.window_end == now.date().isoformat()


async def test_window_is_capped_at_thirty_five_days(db):
    now = datetime(2026, 9, 21, tzinfo=timezone.utc)
    await telemetry._set_value(db, "telemetry_last_sent_at", (now - timedelta(days=90)).isoformat())
    await db.commit()
    payload = await telemetry.build_payload(db, instance_id=str(uuid.uuid4()), now=now)
    assert payload.window_start == (now - timedelta(days=35)).date().isoformat()


async def test_window_naive_stored_timestamp_is_parsed_as_utc(db):
    now = datetime(2026, 9, 21, tzinfo=timezone.utc)
    naive = (now - timedelta(days=3)).replace(tzinfo=None)
    await telemetry._set_value(db, "telemetry_last_sent_at", naive.isoformat())
    await db.commit()
    payload = await telemetry.build_payload(db, instance_id=str(uuid.uuid4()), now=now)
    assert payload.window_start == (now - timedelta(days=3)).date().isoformat()


async def test_window_future_last_sent_at_is_clamped_to_now(db):
    now = datetime(2026, 9, 21, tzinfo=timezone.utc)
    await telemetry._set_value(db, "telemetry_last_sent_at", (now + timedelta(days=5)).isoformat())
    await db.commit()
    payload = await telemetry.build_payload(db, instance_id=str(uuid.uuid4()), now=now)
    assert payload.window_start == now.date().isoformat()
    assert payload.window_end == now.date().isoformat()


# ── S5: mixed GHG Protocol basis ──────────────────────────────────────────────


async def test_mixed_grid_co2e_basis_nulls_co2e_g_but_not_energy_wh(db, session_factory):
    ws = make_workspace()
    project = make_project(ws)
    harness = make_harness(ws)
    run_location = make_run(
        project.id, harness.id,
        energy_accounting={"energy_wh": 10.0, "co2e_g": 5.0, "grid_co2e_basis": "location_based"},
    )
    run_market = make_run(
        project.id, harness.id,
        energy_accounting={"energy_wh": 10.0, "co2e_g": 5.0, "grid_co2e_basis": "market_based"},
    )
    async with session_factory() as seeder:
        seeder.add_all([ws, project, harness, run_location, run_market])
        await seeder.commit()

    payload = await telemetry.build_payload(db, instance_id=str(uuid.uuid4()))
    assert payload.co2e_g is None
    assert payload.energy_wh == 20.0


async def test_single_grid_co2e_basis_still_sums_co2e_g(db, session_factory):
    ws = make_workspace()
    project = make_project(ws)
    harness = make_harness(ws)
    run_a = make_run(
        project.id, harness.id,
        energy_accounting={"energy_wh": 10.0, "co2e_g": 5.0, "grid_co2e_basis": "location_based"},
    )
    run_b = make_run(
        project.id, harness.id,
        energy_accounting={"energy_wh": 10.0, "co2e_g": 5.0, "grid_co2e_basis": "location_based"},
    )
    async with session_factory() as seeder:
        seeder.add_all([ws, project, harness, run_a, run_b])
        await seeder.commit()

    payload = await telemetry.build_payload(db, instance_id=str(uuid.uuid4()))
    assert payload.co2e_g == 10.0


# ── S6: share maps never overstate the total ──────────────────────────────────


def test_share_map_caps_at_one_with_the_reviewers_counts():
    counts = {
        "k1": 38, "k2": 34, "k3": 34, "k4": 26, "k5": 22, "k6": 26, "k7": 22,
        "k8": 26, "k9": 10, "k10": 22, "k11": 26, "k12": 2, "k13": 34, "k14": 78,
    }
    assert sum(counts.values()) == 400
    result = telemetry._share_map(counts, 400)
    assert sum(result.values()) <= 1.0
    assert set(result) <= set(counts)


def test_share_map_drops_a_key_the_excess_zeroes_out():
    # A tiny key whose entire rounded share is consumed by the excess must be
    # dropped outright, not left at a negative or zero share.
    result = telemetry._share_map({"big": 199, "tiny": 1}, 200)
    assert "tiny" not in result or result.get("tiny", 0) > 0
    assert sum(result.values()) <= 1.0


# ── N11: _sig2 totality ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "x", [-1234.5, 0.0, 1e-320, 1e308, float("nan"), float("inf"), float("-inf")]
)
def test_sig2_never_raises_and_stays_finite(x):
    result = telemetry._sig2(x)
    assert isinstance(result, float)
    assert math.isfinite(result)


def test_sig2_negative_rounds_like_its_positive_counterpart():
    assert telemetry._sig2(-1234.0) == -1200.0


def test_sig2_nan_and_inf_fold_to_zero():
    assert telemetry._sig2(float("nan")) == 0.0
    assert telemetry._sig2(float("inf")) == 0.0
    assert telemetry._sig2(float("-inf")) == 0.0


# ── S9: `db` field validator ───────────────────────────────────────────────────


def _base_payload_kwargs(**over) -> dict:
    base = dict(
        instance_id=str(uuid.uuid4()),
        window_start="2026-09-14",
        window_end="2026-09-21",
        tret_version="0.1.0",
        deploy="docker",
        db="sqlite",
        users_bucket="0",
        workspaces_bucket="0",
        runs_bucket="0",
        tokens_in=0,
        tokens_out=0,
        providers={},
        model_families={},
        task_types={},
        run_status={},
        energy_wh=None,
        co2e_g=None,
        factor_rungs={},
        features={"packs": False, "connections": False, "delegation": False, "local_models": False},
    )
    base.update(over)
    return base


@pytest.mark.parametrize("db_value", ["postgres", "postgres-", "Postgres-16", "mysql-8", ""])
def test_payload_rejects_a_malformed_db_label(db_value):
    with pytest.raises(ValidationError):
        telemetry.TelemetryPayload.model_validate(_base_payload_kwargs(db=db_value))


@pytest.mark.parametrize("db_value", ["postgres-16", "postgres-0", "postgres-9", "sqlite"])
def test_payload_accepts_a_well_shaped_db_label(db_value):
    payload = telemetry.TelemetryPayload.model_validate(_base_payload_kwargs(db=db_value))
    assert payload.db == db_value


async def test_db_label_fallback_on_an_unparseable_version_is_postgres_zero(db, monkeypatch):
    class _FakeDialect:
        name = "postgresql"

    class _FakeBind:
        dialect = _FakeDialect()

    monkeypatch.setattr(db, "get_bind", lambda: _FakeBind())

    async def _fake_execute(*_args, **_kwargs):
        class _Result:
            def scalar(self_inner):
                return "not-a-number"

        return _Result()

    monkeypatch.setattr(db, "execute", _fake_execute)
    label = await telemetry._db_label(db)
    assert label == "postgres-0"
    # Must itself pass the payload's own validator — never a bare "postgres".
    telemetry.TelemetryPayload.model_validate(_base_payload_kwargs(db=label))


# ── S7: egress class `telemetry` — runtime override and defence in depth ──────


async def test_runtime_override_can_narrow_telemetry_to_egress_off(db, monkeypatch):
    monkeypatch.setenv("TRET_TELEMETRY", "on")
    get_settings.cache_clear()
    policy_module.set_runtime_override(policy_module.CLASS_TELEMETRY, "off")
    try:
        state = await telemetry.resolve_state(db)
        assert state.locked_reason == "egress_off"
        assert state.enabled is False
    finally:
        policy_module.clear_all_runtime_overrides()


async def test_open_client_denies_telemetry_when_the_class_is_off(monkeypatch):
    """Defence in depth (contract §4's egress chokepoint): even if some
    future caller reached `open_client` directly instead of going through
    `send_once`, a switched-off `telemetry` class refuses the connection at
    construction, before any request is made."""
    monkeypatch.setenv("TRET_TELEMETRY", "off")
    get_settings.cache_clear()
    with pytest.raises(EgressDenied):
        async with real_open_client(telemetry.CLASS_TELEMETRY, timeout=5):
            pass


# ── S8: features.packs — non-built-in pack only ────────────────────────────────


async def test_packs_flag_false_for_a_builtin_pack_only(db, session_factory, monkeypatch, tmp_path):
    builtin_dir = tmp_path / "builtin-packs"
    builtin_dir.mkdir()
    monkeypatch.setenv("TRET_PACKS_DIR", str(builtin_dir))
    get_settings.cache_clear()

    ws = make_workspace()
    builtin_pack = Pack(
        id=uuid.uuid4(),
        workspace_id=ws.id,
        slug="flagship",
        version="1.0.0",
        doctrine_sha="x",
        manifest={},
        source_path=str(builtin_dir / "flagship"),
    )
    async with session_factory() as seeder:
        seeder.add_all([ws, builtin_pack])
        await seeder.commit()

    assert await telemetry._packs_flag(db) is False


async def test_packs_flag_true_once_a_non_builtin_pack_is_installed(db, session_factory, monkeypatch, tmp_path):
    builtin_dir = tmp_path / "builtin-packs"
    builtin_dir.mkdir()
    monkeypatch.setenv("TRET_PACKS_DIR", str(builtin_dir))
    get_settings.cache_clear()

    ws = make_workspace()
    operator_pack = Pack(
        id=uuid.uuid4(),
        workspace_id=ws.id,
        slug="custom",
        version="1.0.0",
        doctrine_sha="x",
        manifest={},
        source_path=str(tmp_path / "storage" / "packs" / "some-uuid"),
    )
    async with session_factory() as seeder:
        seeder.add_all([ws, operator_pack])
        await seeder.commit()

    assert await telemetry._packs_flag(db) is True


# ── Cross-boundary pins: telemetry-ingest/src/schema.js's closed sets ─────────


def _parse_js_string_set(text: str, const_name: str) -> set[str] | None:
    match = re.search(rf"const\s+{re.escape(const_name)}\s*=\s*new Set\(\[(.*?)\]\)", text, re.DOTALL)
    if match is None:
        return None
    return set(re.findall(r"'([^']*)'", match.group(1)))


def test_ingest_worker_closed_sets_match_core():
    schema_path = (
        Path(__file__).resolve().parents[2] / "telemetry-ingest" / "src" / "schema.js"
    )
    if not schema_path.exists():
        pytest.skip(
            f"{schema_path} not present in this checkout — cross-boundary pin skipped"
        )
    text = schema_path.read_text()

    checks = [
        ("PROVIDERS_SET", set(get_args(telemetry.ProviderKey))),
        ("MODEL_FAMILIES_SET", set(get_args(telemetry.FamilyKey))),
        ("TASK_TYPES_SET", set(get_args(telemetry.TaskTypeKey))),
        ("RUN_STATUS_SET", set(get_args(telemetry.RunStatusKey))),
        ("FACTOR_RUNGS_SET", set(get_args(telemetry.FactorRungKey))),
        ("DEPLOY_SET", set(get_args(telemetry.DeployKey))),
        ("USERS_WORKSPACES_BUCKET_SET", set(get_args(telemetry.SmallBucketKey))),
        ("RUNS_BUCKET_SET", set(get_args(telemetry.RunsBucketKey))),
    ]
    for const_name, core_set in checks:
        js_set = _parse_js_string_set(text, const_name)
        assert js_set is not None, f"could not find `{const_name}` in {schema_path}"
        assert js_set == core_set, (
            f"{const_name} in {schema_path} drifted from core: "
            f"worker={sorted(js_set)} core={sorted(core_set)}"
        )
