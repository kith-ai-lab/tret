"""The migration chain, exercised against a real Postgres. **This is the suite
that would have caught the create_all/Alembic split-brain bug.**

Skipped unless `TRET_TEST_POSTGRES_URL` points at a Postgres server the tests may
create and drop databases on — CI sets it to the service container, and locally:

    TRET_TEST_POSTGRES_URL=postgresql+asyncpg://tret:tret@localhost:5432/postgres \\
        .venv/bin/python -m pytest tests/test_migrations_postgres.py -q

Every test runs against its own freshly created database and drops it afterwards,
so nothing here can touch a development database. The URL's own database name is
used only as the maintenance connection for CREATE/DROP DATABASE.

What is asserted, and why each one matters:

* `alembic upgrade head` from **empty** builds the whole schema — the state every
  new install is in, and the one nothing verified before.
* the built schema has **no autogenerate drift** against `Base.metadata`. This is
  the root-cause check: a model column added without a migration fails here, which
  is exactly how `packs.content_hash` came to exist in the models while no
  upgraded database ever grew it.
* a **legacy create_all database** (tables present, `alembic_version` absent) is
  adopted by `ensure_schema`: baseline inferred, stamped, migrated to head, rows
  left intact.
* **downgrade one step and upgrade again** works, so the downgrades are real.
* the whole thing is **idempotent** and safe to run concurrently.
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tret.db.migrate import (
    alembic_config,
    current_revisions,
    ensure_schema,
    plan_schema_upgrade,
    read_database_state,
)
from tret.db.models import Base

ADMIN_URL = os.environ.get("TRET_TEST_POSTGRES_URL", "")

pytestmark = pytest.mark.skipif(
    not ADMIN_URL,
    reason="set TRET_TEST_POSTGRES_URL to a Postgres server where tests may create databases",
)

# The pre-sprint head — the shape of a v0.1 install, i.e. the release early
# adopters are upgrading *from*.
LEGACY_V01 = "f18e4f74fc33"


def head_revision() -> str:
    (head,) = ScriptDirectory.from_config(alembic_config()).get_heads()
    return head


def _url_for(database: str) -> str:
    base, _, _ = ADMIN_URL.rpartition("/")
    return f"{base}/{database}"


@pytest.fixture
async def database():
    """A throwaway database, dropped afterwards. Yields its URL."""
    name = f"tret_test_{uuid.uuid4().hex[:12]}"
    admin = create_async_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{name}"'))
        yield _url_for(name)
    finally:
        async with admin.connect() as conn:
            await conn.execute(
                text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :n"),
                {"n": name},
            )
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        await admin.dispose()


@pytest.fixture
async def engine(database):
    eng = create_async_engine(database)
    try:
        yield eng
    finally:
        await eng.dispose()


# ── helpers, all run through Connection.run_sync ──────────────────────────────
def _autogenerate_diff(sync_conn):
    return compare_metadata(MigrationContext.configure(sync_conn), Base.metadata)


def _upgrade(sync_conn, revision):
    command.upgrade(alembic_config(sync_conn), revision)


def _downgrade(sync_conn, revision):
    command.downgrade(alembic_config(sync_conn), revision)


def _stamp(sync_conn, revision):
    command.stamp(alembic_config(sync_conn), revision)


async def _heads(engine) -> frozenset[str]:
    async with engine.connect() as conn:
        return await conn.run_sync(current_revisions)


async def _diff(engine) -> list:
    async with engine.connect() as conn:
        return await conn.run_sync(_autogenerate_diff)


async def _make_legacy_create_all_database(engine, revision: str = LEGACY_V01) -> None:
    """Reproduce what an older tret release left behind.

    Migrating to `revision` and then removing `alembic_version` gives a database
    with exactly that release's schema and no stamp — which is what
    `Base.metadata.create_all` produced when it ran under that release's models.
    """
    async with engine.connect() as conn:
        await conn.run_sync(_upgrade, revision)
        await conn.commit()
        await conn.execute(text("DROP TABLE alembic_version"))
        await conn.commit()


# ── empty → head ──────────────────────────────────────────────────────────────
async def test_empty_database_migrates_to_head(engine):
    plan = await ensure_schema(engine)
    assert plan is not None and plan.kind == "empty"
    assert await _heads(engine) == frozenset({head_revision()})
    async with engine.connect() as conn:
        state = await conn.run_sync(read_database_state)
    assert "runs" in state.tables
    assert {"energy_wh", "energy_accounting", "cache_read_tokens"} <= state.columns["runs"]
    assert "content_hash" in state.columns["packs"]


async def test_migrated_schema_has_no_drift_from_the_models(engine):
    """Models vs migrations. An empty autogenerate diff is the whole point.

    If this fails, someone changed `tret/db/models.py` without writing the
    matching migration: fresh installs (which used to run create_all) would have
    the column and every upgraded install would not.
    """
    await ensure_schema(engine)
    diff = await _diff(engine)
    assert diff == [], (
        "models and migrations have drifted apart. Generate the missing migration:\n"
        "    cd backend && alembic revision --autogenerate -m '<what changed>'\n"
        f"  Alembic reports: {diff}"
    )


async def test_second_startup_is_a_no_op(engine):
    await ensure_schema(engine)
    plan = await ensure_schema(engine)
    assert plan is not None and plan.kind == "stamped"
    assert await _heads(engine) == frozenset({head_revision()})


async def test_concurrent_startups_serialise_on_the_advisory_lock(database):
    """Two instances booting together must not both try to migrate."""
    engines = [create_async_engine(database) for _ in range(3)]
    try:
        plans = await asyncio.gather(*(ensure_schema(e) for e in engines))
        kinds = sorted(p.kind for p in plans)
        assert kinds == ["empty", "stamped", "stamped"], kinds
        assert await _heads(engines[0]) == frozenset({head_revision()})
        assert await _diff(engines[0]) == []
    finally:
        for e in engines:
            await e.dispose()


# ── legacy create_all database → adopted ──────────────────────────────────────
async def test_legacy_create_all_database_is_stamped_and_upgraded(engine):
    await _make_legacy_create_all_database(engine)

    async with engine.connect() as conn:
        state = await conn.run_sync(read_database_state)
    assert state.stamped == frozenset()  # no stamp: this is the broken state
    assert "content_hash" not in state.columns["packs"]
    assert plan_schema_upgrade(state).stamp == LEGACY_V01

    plan = await ensure_schema(engine)
    assert plan is not None and plan.kind == "legacy" and plan.stamp == LEGACY_V01
    assert await _heads(engine) == frozenset({head_revision()})
    assert await _diff(engine) == []


async def test_legacy_recovery_preserves_existing_rows(engine):
    """The migrations must adopt real data, not just an empty shell."""
    await _make_legacy_create_all_database(engine)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, email, display_name, role, created_at) "
                "VALUES (gen_random_uuid(), 'legacy@example.com', 'Legacy', 'analyst', now())"
            )
        )

    await ensure_schema(engine)

    async with engine.connect() as conn:
        survivors = (
            await conn.execute(
                text("SELECT count(*) FROM users WHERE email = 'legacy@example.com'")
            )
        ).scalar()
        # The column whose absence crashed the old code is now queryable.
        await conn.execute(text("SELECT content_hash FROM packs"))
    assert survivors == 1


async def test_legacy_database_already_at_head_is_stamped_without_migrating(engine):
    """A create_all database built by the *current* release: schema is right,
    stamp is missing. Stamp head; run nothing."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    plan = await ensure_schema(engine)
    assert plan is not None and plan.kind == "legacy"
    assert plan.stamp == head_revision()
    assert await _heads(engine) == frozenset({head_revision()})
    assert await _diff(engine) == []


async def test_the_documented_manual_recovery_actually_works(engine):
    """docs/upgrading.md tells operators to run `alembic stamp <rev>` then
    `alembic upgrade head`. Assert that path, not just the automatic one."""
    await _make_legacy_create_all_database(engine)
    async with engine.connect() as conn:
        await conn.run_sync(_stamp, LEGACY_V01)
        await conn.commit()
        await conn.run_sync(_upgrade, "head")
        await conn.commit()
    assert await _heads(engine) == frozenset({head_revision()})
    assert await _diff(engine) == []


# ── downgrades are real ───────────────────────────────────────────────────────
async def test_downgrade_one_step_then_upgrade_again(engine):
    await ensure_schema(engine)
    script = ScriptDirectory.from_config(alembic_config())
    previous = script.get_revision(head_revision()).down_revision
    assert previous, "head has no down_revision; adjust this test"

    async with engine.connect() as conn:
        await conn.run_sync(_downgrade, "-1")
        await conn.commit()
    assert await _heads(engine) == frozenset({previous})

    async with engine.connect() as conn:
        await conn.run_sync(_upgrade, "head")
        await conn.commit()
    assert await _heads(engine) == frozenset({head_revision()})
    assert await _diff(engine) == []


# ── the app itself boots on both ──────────────────────────────────────────────
@pytest.fixture
def app_against(monkeypatch, tmp_path):
    """Point the app's cached engine/settings at `url`, and return create_app()."""

    def build(url: str):
        from tret import config
        from tret.db import engine as engine_module

        config.get_settings.cache_clear()
        monkeypatch.setenv("TRET_DATABASE_URL", url)
        monkeypatch.setenv("TRET_STORAGE_DIR", str(tmp_path / "storage"))
        monkeypatch.setattr(engine_module, "_engine", None)
        monkeypatch.setattr(engine_module, "_session_factory", None)

        from tret.main import create_app

        return create_app()

    yield build
    from tret import config

    config.get_settings.cache_clear()


def _boot_and_check(app) -> None:
    from fastapi.testclient import TestClient

    with TestClient(app) as client:  # entering the context runs the lifespan
        assert client.get("/api/healthz").json() == {"ok": True}


async def test_app_boots_against_an_empty_database(database, app_against):
    await asyncio.to_thread(_boot_and_check, app_against(database))
    engine = create_async_engine(database)
    try:
        assert await _heads(engine) == frozenset({head_revision()})
    finally:
        await engine.dispose()


async def test_app_boots_against_a_legacy_create_all_database(database, app_against):
    """The crash-loop scenario, end to end: an unstamped v0.1 database, the real
    startup path, and a working app afterwards."""
    engine = create_async_engine(database)
    try:
        await _make_legacy_create_all_database(engine)
    finally:
        await engine.dispose()

    await asyncio.to_thread(_boot_and_check, app_against(database))

    engine = create_async_engine(database)
    try:
        assert await _heads(engine) == frozenset({head_revision()})
        assert await _diff(engine) == []
    finally:
        await engine.dispose()
