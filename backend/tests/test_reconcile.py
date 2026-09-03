"""tret/services/reconcile.py: sweeping runs a killed process left mid-flight.

Two suites:

* a unit-level one (real sqlite ORM, fake extension registry) that seeds a
  `running` run and a `completed` one, calls `sweep_orphaned_runs` directly,
  and checks exactly the running one flips and fires its post-run hook;
* an app-level one that boots the real app (`create_app()`, real lifespan)
  against a real database and checks a pre-seeded `running` run comes out
  `failed` on the other side of startup — the thing an operator actually
  restarting a killed process gets, not just the function in isolation.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from tests.evals.golden_world import install_sqlite_type_shims
from tret.db.models import Base, Harness, Project, Run, Workspace
from tret.engine import extensions as extensions_module
from tret.engine.extensions import ExtensionAPI
from tret.services.reconcile import HOOK_ELIGIBILITY_WINDOW, ORPHAN_ERROR, sweep_orphaned_runs


@pytest.fixture(autouse=True)
def _reset_extension_registry():
    """Every test starts with no registry set, same isolation
    test_extensions.py and test_workspaces_api.py give their own tests."""
    extensions_module._registry = None
    yield
    extensions_module._registry = None


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
async def db(engine):
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session


async def _seed(
    db, *, run_status: str, started_at: datetime | None = None
) -> tuple[Workspace, Harness, Run]:
    workspace = Workspace(name="Acme", kind="team")
    db.add(workspace)
    await db.flush()

    project = Project(workspace_id=workspace.id, name="Acme Project")
    harness = Harness(
        workspace_id=workspace.id,
        name="Acme Harness",
        task_profile="freeform",
        model_policy={"mode": "auto"},
        tool_names=[],
    )
    db.add_all([project, harness])
    await db.flush()

    run = Run(
        project_id=project.id,
        harness_id=harness.id,
        task_type="freeform",
        task_input={},
        document_ids=[],
        status=run_status,
        messages=[],
        started_at=started_at,
    )
    db.add(run)
    await db.commit()
    return workspace, harness, run


# ── the unit-level sweep ──────────────────────────────────────────────────────
async def test_sweeps_only_the_running_run_and_leaves_completed_alone(db):
    workspace, harness, running = await _seed(db, run_status="running")
    _, _, completed = await _seed(db, run_status="completed")

    hook_calls = []

    async def hook(ext_db, run, workspace_id):
        hook_calls.append((run.id, workspace_id))

    ext = ExtensionAPI(None)
    ext.add_post_run_hook(hook)
    extensions_module._registry = ext

    count = await sweep_orphaned_runs(db)

    assert count == 1
    await db.refresh(running)
    await db.refresh(completed)

    assert running.status == "failed"
    assert running.error == ORPHAN_ERROR
    assert running.finished_at is not None

    assert completed.status == "completed"
    assert completed.error is None
    assert completed.finished_at is None

    assert hook_calls == [(running.id, workspace.id)]


async def test_queued_runs_are_also_swept(db):
    """queued is non-terminal too: a process can die between insert and the
    engine ever picking the run up."""
    _, _, queued = await _seed(db, run_status="queued")

    count = await sweep_orphaned_runs(db)

    assert count == 1
    await db.refresh(queued)
    assert queued.status == "failed"
    assert queued.error == ORPHAN_ERROR


async def test_no_orphans_is_a_true_no_op(db):
    _, _, completed = await _seed(db, run_status="completed")
    count = await sweep_orphaned_runs(db)
    assert count == 0
    await db.refresh(completed)
    assert completed.status == "completed"


async def test_a_hook_is_fired_for_every_orphan_swept(db):
    _, harness_a, run_a = await _seed(db, run_status="running")
    workspace_b, harness_b, run_b = await _seed(db, run_status="queued")

    seen = []

    async def hook(ext_db, run, workspace_id):
        seen.append(run.id)

    ext = ExtensionAPI(None)
    ext.add_post_run_hook(hook)
    extensions_module._registry = ext

    count = await sweep_orphaned_runs(db)

    assert count == 2
    assert sorted(seen) == sorted([run_a.id, run_b.id])


async def test_a_run_started_before_the_hook_eligibility_window_is_still_failed_but_gets_no_hook(
    db, caplog
):
    """The sweep itself stays unbounded — a stuck row is exactly as wrong at
    a year old as at an hour old, so it still flips to `failed` — but the
    first boot after an upgrade (or after any long enough gap) can find rows
    that have sat `running` since well before this process was ever watching
    them, and re-firing a billing hook for every one of those on one boot is
    not reconciliation, it is re-metering history. Only the recent run's hook
    fires; the old one is closed silently (as far as hooks go).
    """
    now = datetime.now(timezone.utc)
    old_started = now - HOOK_ELIGIBILITY_WINDOW - timedelta(hours=1)
    _, _, old_run = await _seed(db, run_status="running", started_at=old_started)
    _, _, recent_run = await _seed(db, run_status="running", started_at=now)

    seen = []

    async def hook(ext_db, run, workspace_id):
        seen.append(run.id)

    ext = ExtensionAPI(None)
    ext.add_post_run_hook(hook)
    extensions_module._registry = ext

    with caplog.at_level(logging.WARNING, logger="tret.reconcile"):
        count = await sweep_orphaned_runs(db)

    assert count == 2
    await db.refresh(old_run)
    await db.refresh(recent_run)
    # Both are closed out — age never excuses a stuck row from being failed.
    assert old_run.status == "failed"
    assert old_run.error == ORPHAN_ERROR
    assert recent_run.status == "failed"
    # Only the recent one's hook fires.
    assert seen == [recent_run.id]
    assert "closed 1 of those without firing post-run hooks" in caplog.text


async def test_a_queued_run_with_no_started_at_uses_created_at_as_its_age(db):
    """A run that died between insert and `HarnessEngine.execute` ever picking
    it up has no `started_at` at all — `created_at` (never null) must still
    give it an age rather than blow up the comparison."""
    _, _, queued = await _seed(db, run_status="queued")  # started_at stays None

    seen = []

    async def hook(ext_db, run, workspace_id):
        seen.append(run.id)

    ext = ExtensionAPI(None)
    ext.add_post_run_hook(hook)
    extensions_module._registry = ext

    count = await sweep_orphaned_runs(db)

    assert count == 1
    # created_at defaults to "now", so this freshly-seeded row is well inside
    # the eligibility window.
    assert seen == [queued.id]


async def test_a_naive_datetime_from_sqlites_missing_tz_support_still_compares(db):
    """Every timestamp column here is declared tz-aware (`TIMESTAMP(timezone=
    True)`, db/models.py), but SQLite has no genuine timezone-aware storage —
    a value actually round-tripped through it (as opposed to read straight
    back off the ORM's own in-memory object, which `expire_on_commit=False`
    would otherwise hand back unchanged) comes back *naive*. This is exactly
    what a real app boot sees, and what used to raise
    `TypeError: can't compare offset-naive and offset-aware datetimes` the
    moment the eligibility-window comparison touched it.
    """
    _, _, run = await _seed(db, run_status="running")
    db.expire(run)
    await db.refresh(run)
    assert run.created_at.tzinfo is None  # sqlite's actual, naive round-trip

    count = await sweep_orphaned_runs(db)  # must not raise

    assert count == 1


async def test_with_no_extensions_loaded_the_sweep_still_completes(db):
    """The registry's own default is a true no-op (test_extensions.py) — this
    just checks the sweep does not depend on one being registered."""
    _, _, running = await _seed(db, run_status="running")
    count = await sweep_orphaned_runs(db)
    assert count == 1
    await db.refresh(running)
    assert running.status == "failed"


# ── app-level: a killed process's run is failed by the next boot ────────────
@pytest.fixture()
def app_database(tmp_path, monkeypatch):
    """Points tret's process-global engine/session-factory (tret/db/engine.py)
    at a throwaway sqlite file for the duration of one test, then restores
    them — those are lru_cache-like module globals, not something a fresh
    Settings() call alone resets.
    """
    import tret.db.engine as db_engine_module
    from tret.config import get_settings

    install_sqlite_type_shims()
    db_path = tmp_path / "reconcile.db"
    monkeypatch.setenv("TRET_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    # No real pack directory: bootstrap's pack install is a no-op for a
    # missing dir (services/workspace.py), so this just keeps boot from
    # touching the repo's own packs/ tree.
    monkeypatch.setenv("TRET_PACKS_DIR", str(tmp_path / "no-packs"))
    get_settings.cache_clear()
    db_engine_module._engine = None
    db_engine_module._session_factory = None
    yield db_path
    db_engine_module._engine = None
    db_engine_module._session_factory = None
    get_settings.cache_clear()


async def _seed_orphan(db_path) -> uuid.UUID:
    """A `running` run in a freshly created schema, via a throwaway engine
    disposed before the app ever boots — test_tenancy_isolation.py's
    `tenants` fixture does the same thing for the same reason: the app's own
    engine must be the only thing touching this file once it starts."""
    eng = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    try:
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_factory = async_sessionmaker(eng, expire_on_commit=False)
        async with session_factory() as db:
            workspace = Workspace(name="Acme", kind="team")
            db.add(workspace)
            await db.flush()
            project = Project(workspace_id=workspace.id, name="Acme Project")
            harness = Harness(
                workspace_id=workspace.id,
                name="Acme Harness",
                task_profile="freeform",
                model_policy={"mode": "auto"},
                tool_names=[],
            )
            db.add_all([project, harness])
            await db.flush()
            run = Run(
                id=uuid.uuid4(),
                project_id=project.id,
                harness_id=harness.id,
                task_type="freeform",
                task_input={},
                document_ids=[],
                status="running",
                messages=[],
            )
            db.add(run)
            await db.commit()
            return run.id
    finally:
        await eng.dispose()


async def test_a_prior_processs_running_run_is_failed_by_the_next_boot(app_database):
    from fastapi.testclient import TestClient

    run_id = await _seed_orphan(app_database)

    from tret.main import create_app

    # Entering the context manager runs the real lifespan: schema creation
    # (sqlite create_all fallback), bootstrap, and — the thing under test —
    # the orphaned-run sweep, all before the client ever issues a request.
    with TestClient(create_app()):
        pass

    import tret.db.engine as db_engine_module

    async with db_engine_module.get_session_factory()() as db:
        run = await db.get(Run, run_id)
        assert run.status == "failed"
        assert run.error == ORPHAN_ERROR
        assert run.finished_at is not None


# ── the lifespan's sweep is gated on the instance lock's actual state ────────
class _NoOpLockConn:
    """A `held` lock's connection, for a test that never touches Postgres:
    the lifespan's shutdown calls `instance_lock.release()` unconditionally,
    so this just needs to survive that without raising."""

    async def execute(self, *args, **kwargs):
        pass

    async def commit(self):
        pass

    async def close(self):
        pass


@pytest.mark.parametrize(
    "lock_state, sweep_expected",
    [
        # "lost": Postgres, TRET_INSTANCE_LOCK=warn, another live process
        # already held the key — that other instance may still legitimately
        # be finishing these very runs, so sweeping here would fail them out
        # from under it and double-meter them once it does.
        ("lost", False),
        # "held": this process really is the only one — sweep as ever.
        ("held", True),
        # "not_applicable": SQLite, or TRET_INSTANCE_LOCK=off — the lock check
        # never ran at all, which is not the same thing as losing it.
        ("not_applicable", True),
    ],
)
async def test_the_lifespan_sweeps_only_when_the_lock_was_not_lost(
    app_database, monkeypatch, lock_state, sweep_expected
):
    """Before `InstanceLock` carried an explicit state, `InstanceLock(None)`
    looked identical whether the lock was never attempted or attempted and
    lost — main.py's lifespan swept unconditionally on both, which is exactly
    backwards for "lost": a second machine that just lost the race is not
    "the only instance, nothing else could still be executing these runs".
    """
    import tret.services.instance_lock as instance_lock_module
    import tret.services.reconcile as reconcile_module
    from tret.services.instance_lock import InstanceLock

    async def fake_acquire_instance_lock():
        conn = _NoOpLockConn() if lock_state == "held" else None
        return InstanceLock(conn, lock_state)

    real_sweep = reconcile_module.sweep_orphaned_runs
    sweep_calls = []

    async def spy_sweep(db):
        sweep_calls.append(True)
        return await real_sweep(db)

    monkeypatch.setattr(instance_lock_module, "acquire_instance_lock", fake_acquire_instance_lock)
    monkeypatch.setattr(reconcile_module, "sweep_orphaned_runs", spy_sweep)

    from fastapi.testclient import TestClient

    from tret.main import create_app

    with TestClient(create_app()):
        pass

    assert bool(sweep_calls) is sweep_expected
