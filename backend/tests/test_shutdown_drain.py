"""tret/main.py's `_drain_in_flight_runs`: the shutdown-time wait for runs
still executing to reach their own terminal state before this process exits.

D2: two live deploys each marked a run still genuinely in progress with
`reconcile.ORPHAN_ERROR`, because the old process exited (and the run's
background task died with it) before the run had actually finished — the
new process's startup sweep (`sweep_orphaned_runs`) then found the row still
`running` and closed it out as orphaned. This is the unit-level test for the
fix: a fake engine's background task standing in for `tret.api.runs`'s own
`_background_tasks` registry, exercised directly against `_drain_in_flight_runs`
rather than through a full app boot (test_reconcile.py already covers that
sweep itself, and the app-level "does a real boot fail an orphan" case).
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from tests.evals.golden_world import install_sqlite_type_shims
from tret.db.models import Base, Harness, Project, Run, Workspace
from tret.engine import extensions as extensions_module
from tret.main import _drain_in_flight_runs
from tret.services.reconcile import ORPHAN_ERROR


@pytest.fixture(autouse=True)
def _reset_extension_registry():
    """Same isolation test_reconcile.py gives its own tests: `sweep_orphaned_runs`
    (called by `_drain_in_flight_runs` after the wait) reads the process-wide
    registry, so a leftover one from another test must never leak in here."""
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


async def _seed_running_run(db) -> Run:
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
        status="running",
        messages=[],
    )
    db.add(run)
    await db.commit()
    return run


async def test_a_run_that_finishes_within_the_deadline_is_drained_not_marked(db):
    """A fake engine task standing in for one of `tret.api.runs`'s own
    `_background_tasks`: it finishes on its own (as a real `HarnessEngine.execute`
    would, in its own db session) well inside the drain deadline. The run must
    come out exactly as the fake engine left it — never touched by the
    process_restart marking, since `_drain_in_flight_runs` must not even see
    it as non-terminal by the time it looks."""
    run = await _seed_running_run(db)

    async def fake_engine_execute() -> None:
        await asyncio.sleep(1)
        run.status = "completed"
        run.finished_at = datetime.now(timezone.utc)
        await db.commit()

    task = asyncio.create_task(fake_engine_execute())
    try:
        # A deadline generous next to the fake engine's 1s completion — this
        # is what "drained" means: the wait ends because the task finished,
        # not because the clock ran out.
        await _drain_in_flight_runs(db, {task}, deadline_seconds=10)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    assert run.status == "completed"
    assert run.error is None


async def test_a_run_still_executing_past_the_deadline_is_marked_orphaned(db):
    """The other half: a fake engine task that never finishes (standing in
    for a run genuinely still executing, or one whose task really did die
    with the old process) is left running — never cancelled — and the run's
    own row is what gets marked, once the (short, for the test) deadline
    passes."""
    run = await _seed_running_run(db)

    async def fake_engine_execute_that_never_finishes() -> None:
        await asyncio.sleep(100)

    task = asyncio.create_task(fake_engine_execute_that_never_finishes())
    try:
        await _drain_in_flight_runs(db, {task}, deadline_seconds=1)

        assert not task.done()  # never cancelled by the drain, only left behind
        await db.refresh(run)
        assert run.status == "failed"
        assert run.error == ORPHAN_ERROR
        assert run.finished_at is not None
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


# ── new work is refused once the drain has begun ─────────────────────────────


def _request_for(app):
    from starlette.requests import Request as _Request

    return _Request({"type": "http", "app": app, "method": "POST", "path": "/", "headers": []})


def test_refuse_if_draining_is_a_no_op_until_shutdown_begins():
    from fastapi import FastAPI

    from tret.services import lifecycle

    app = FastAPI()
    lifecycle.refuse_if_draining(_request_for(app))  # no flag at all: no raise
    lifecycle.mark_draining(app, False)
    lifecycle.refuse_if_draining(_request_for(app))


def test_refuse_if_draining_returns_503_with_retry_after():
    import pytest
    from fastapi import FastAPI, HTTPException

    from tret.services import lifecycle

    app = FastAPI()
    lifecycle.mark_draining(app)
    with pytest.raises(HTTPException) as info:
        lifecycle.refuse_if_draining(_request_for(app))
    assert info.value.status_code == 503
    assert info.value.headers["Retry-After"] == str(lifecycle.RETRY_AFTER_SECONDS)


def test_draining_is_per_app_not_global():
    from fastapi import FastAPI

    from tret.services import lifecycle

    a, b = FastAPI(), FastAPI()
    lifecycle.mark_draining(a)
    lifecycle.refuse_if_draining(_request_for(b))  # untouched app: no raise
