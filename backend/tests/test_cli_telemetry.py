"""`tret telemetry` at the command line — status, preview, enable, disable.

Same dispatch harness as test_cli_egress.py: every assertion drives
`tret.cli.main()` through `sys.argv`, not the handler functions directly, so
argparse's own subcommand wiring is exercised too.

Unlike egress (pure in-memory state), the telemetry commands read and write
real rows through `tret.db.engine.get_session_factory` — the exact function
`_telemetry_status`/`_telemetry_preview`/`_telemetry_toggle` import lazily
inside their handlers, same as `_backfill_outcomes` does for `outcomes
backfill`. So this test patches that one function to hand out sessions
against a real, schema-created sqlite database instead of talking to
whatever TRET_DATABASE_URL points at — the same "real sqlite, schema via
Base.metadata.create_all" harness tests/test_telemetry.py uses for the
service layer directly, one level up at the CLI boundary.
"""
from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from tests.evals.golden_world import install_sqlite_type_shims
from tret.cli import main
from tret.config import get_settings
from tret.db.models import Base
from tret.engine import extensions as extensions_module
from tret.net import policy as policy_module
from tret.services.telemetry import PREVIEW_PLACEHOLDER


@pytest.fixture
def session_factory():
    """A fresh, schema-created sqlite database per test. Built and torn down
    with plain `asyncio.run` calls rather than a pytest-asyncio fixture,
    since these tests drive `main()` synchronously (it makes its own,
    separate `asyncio.run` call per subcommand — see `_run` below) and a
    StaticPool sqlite connection is safe to reuse across unrelated event
    loops (confirmed against this exact pool/dialect combination)."""
    install_sqlite_type_shims()
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )

    async def _create_schema() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_create_schema())
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        asyncio.run(engine.dispose())


@pytest.fixture(autouse=True)
def _clean(monkeypatch, session_factory):
    """Point the CLI at the test database, and start every test from the
    same clean posture test_telemetry.py's own `_clean` fixture does: no
    extension registry, no leaked egress runtime overrides, no ambient
    DO_NOT_TRACK, and the master egress switch ON so the `telemetry` class
    is reachable (default state, matching `TRET_EGRESS`'s own code default)."""
    import tret.db.engine as db_engine

    monkeypatch.setattr(db_engine, "get_session_factory", lambda: session_factory)
    monkeypatch.delenv("DO_NOT_TRACK", raising=False)
    monkeypatch.setenv("TRET_EGRESS", "on")
    extensions_module._registry = None
    policy_module.clear_all_runtime_overrides()
    get_settings.cache_clear()
    yield
    extensions_module._registry = None
    policy_module.clear_all_runtime_overrides()
    get_settings.cache_clear()


def _run(monkeypatch, *argv: str) -> None:
    """Drive `tret.cli.main()` the way a real invocation would: through
    argv, not by calling the handler function directly."""
    monkeypatch.setattr("sys.argv", ["tret", *argv])
    main()


# ── status ────────────────────────────────────────────────────────────────────


def test_status_prints_json_disabled_by_default(monkeypatch, capsys):
    _run(monkeypatch, "telemetry", "status")
    body = json.loads(capsys.readouterr().out)
    assert set(body.keys()) == {
        "enabled", "env_mode", "db_enabled", "locked", "locked_reason",
        "instance_id", "last_sent_at", "endpoint", "recent",
    }
    assert body["enabled"] is False
    assert body["env_mode"] == "admin"
    assert body["locked"] is False
    assert body["instance_id"] is None
    assert body["recent"] == []


# ── enable / disable ─────────────────────────────────────────────────────────


def test_enable_then_status_shows_enabled_and_an_instance_id(monkeypatch, capsys):
    _run(monkeypatch, "telemetry", "enable")
    enable_out = capsys.readouterr().out
    assert "telemetry enabled" in enable_out
    assert "enabled=True" in enable_out

    _run(monkeypatch, "telemetry", "status")
    body = json.loads(capsys.readouterr().out)
    assert body["enabled"] is True
    assert body["db_enabled"] is True
    assert body["instance_id"] is not None
    assert uuid.UUID(body["instance_id"]).version == 4


def test_disable_clears_the_instance_id(monkeypatch, capsys):
    _run(monkeypatch, "telemetry", "enable")
    capsys.readouterr()

    _run(monkeypatch, "telemetry", "disable")
    disable_out = capsys.readouterr().out
    assert "telemetry disabled" in disable_out
    assert "enabled=False" in disable_out

    _run(monkeypatch, "telemetry", "status")
    body = json.loads(capsys.readouterr().out)
    assert body["enabled"] is False
    assert body["instance_id"] is None


def test_enable_when_locked_off_exits_2_and_names_the_reason(monkeypatch, capsys):
    monkeypatch.setattr(get_settings(), "telemetry", "off")

    with pytest.raises(SystemExit) as excinfo:
        _run(monkeypatch, "telemetry", "enable")
    assert excinfo.value.code == 2

    err = capsys.readouterr().err
    assert "env_off" in err

    # Locked off before it ever got a chance to mint anything.
    _run(monkeypatch, "telemetry", "status")
    body = json.loads(capsys.readouterr().out)
    assert body["enabled"] is False
    assert body["instance_id"] is None


def test_disable_when_locked_on_exits_2_and_names_the_reason(monkeypatch, capsys):
    monkeypatch.setattr(get_settings(), "telemetry", "on")

    with pytest.raises(SystemExit) as excinfo:
        _run(monkeypatch, "telemetry", "disable")
    assert excinfo.value.code == 2

    err = capsys.readouterr().err
    assert "env_on" in err


# ── preview ───────────────────────────────────────────────────────────────────


def test_preview_uses_the_placeholder_and_mints_nothing(monkeypatch, capsys):
    _run(monkeypatch, "telemetry", "preview")
    body = json.loads(capsys.readouterr().out)
    assert body["payload"]["instance_id"] == PREVIEW_PLACEHOLDER
    assert body["would_send"] is False

    # Nothing left behind: preview never mints an id, even to build a payload.
    _run(monkeypatch, "telemetry", "status")
    status_body = json.loads(capsys.readouterr().out)
    assert status_body["instance_id"] is None
