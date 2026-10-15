"""`POST /api/runs/{id}/cancel` for a run nothing in this process is executing.

The engine's cancel only flips an in-memory flag, which does nothing for a
run wedged at queued/running with no task behind it (crashed task, a prior
process the startup sweep missed, another machine). The endpoint now reports
whether the run was live here and, for a non-live run past the grace period,
closes the row out with the sweep's convention so chat's `active_run_id`
stops blocking the conversation.

Reuses the sqlite fixtures and chat seeding from test_chat_api.py.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from tests import test_chat_api as _chat_tests
from tret.api import auth
from tret.api import chat as chat_api
from tret.api import runs as runs_api
from tret.db.engine import get_db
from tret.db.models import Run
from tret.engine.events import get_event_bus
from tret.engine.harness import HarnessEngine


# Fixtures shared with test_chat_api.py (pytest finds them by module attribute).
engine = _chat_tests.engine
seed = _chat_tests.seed
session_factory = _chat_tests.session_factory
_seed_chat = _chat_tests._seed_chat


class _NoopEngine:
    async def execute(self, run_id):  # pragma: no cover
        return None


@pytest_asyncio.fixture
async def harness_engine(monkeypatch):
    eng = HarnessEngine()
    monkeypatch.setattr(runs_api, "get_harness_engine", lambda: eng)
    return eng


@pytest_asyncio.fixture
async def client(session_factory, monkeypatch, harness_engine):
    monkeypatch.setattr(chat_api, "get_harness_engine", lambda: _NoopEngine())
    app = FastAPI()
    app.include_router(auth.router)
    app.include_router(chat_api.router)
    app.include_router(runs_api.router)

    async def _get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _get_db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _send(client, conversation_id: str) -> uuid.UUID:
    sent = await client.post(f"/api/chat/{conversation_id}/messages", json={"text": "Hello"})
    assert sent.status_code == 200, sent.text
    return uuid.UUID(sent.json()["run_id"])


async def _age(session_factory, run_id: uuid.UUID, *, seconds: float, status: str = "queued"):
    async with session_factory() as db:
        run = await db.get(Run, run_id)
        run.status = status
        run.created_at = datetime.now(timezone.utc) - timedelta(seconds=seconds)
        await db.commit()


async def test_cancel_of_a_live_run_reports_live_and_leaves_the_row_alone(
    client, seed, session_factory, harness_engine
):
    conversation_id = await _seed_chat(client, seed, session_factory)
    run_id = await _send(client, conversation_id)
    await _age(session_factory, run_id, seconds=3600, status="running")
    harness_engine._live.add(run_id)  # what execute() does while it runs

    res = await client.post(f"/api/runs/{run_id}/cancel")
    assert res.status_code == 200, res.text
    assert res.json() == {"ok": True, "live": True, "marked": False}
    assert harness_engine._is_cancelled(run_id)  # the running loop will see it
    async with session_factory() as db:
        run = await db.get(Run, run_id)
    assert run.status == "running"
    assert run.error is None


async def test_cancel_of_a_wedged_run_marks_it_failed_and_unblocks_chat(
    client, seed, session_factory, harness_engine
):
    conversation_id = await _seed_chat(client, seed, session_factory)
    run_id = await _send(client, conversation_id)
    await _age(session_factory, run_id, seconds=runs_api.WEDGED_RUN_GRACE.total_seconds() + 60)
    assert (await client.get(f"/api/chat/{conversation_id}")).json()["active_run_id"] == str(run_id)
    assert (
        await client.post(f"/api/chat/{conversation_id}/messages", json={"text": "Two"})
    ).status_code == 409

    res = await client.post(f"/api/runs/{run_id}/cancel")
    assert res.status_code == 200, res.text
    assert res.json() == {"ok": True, "live": False, "marked": True}

    async with session_factory() as db:
        run = await db.get(Run, run_id)
    # Same status as sweep_orphaned_runs, error in its "<reason>: <detail>" shape.
    assert run.status == "failed"
    assert run.error == "cancelled: no live process for this run"
    assert run.finished_at is not None
    # The flag stays: a task that has not entered execute() yet must still see it.
    assert run_id in harness_engine._cancelled

    # An open SSE stream gets the terminal event and ends.
    events = [e async for e in get_event_bus().subscribe(run_id)]
    assert [e.type for e in events if e.type != "ping"] == ["error"]

    assert (await client.get(f"/api/chat/{conversation_id}")).json()["active_run_id"] is None
    again = await client.post(f"/api/chat/{conversation_id}/messages", json={"text": "Two"})
    assert again.status_code == 200, again.text


async def test_cancel_of_a_terminal_run_is_a_noop(client, seed, session_factory):
    conversation_id = await _seed_chat(client, seed, session_factory)
    run_id = await _send(client, conversation_id)
    await _age(session_factory, run_id, seconds=3600, status="completed")

    res = await client.post(f"/api/runs/{run_id}/cancel")
    assert res.status_code == 200, res.text
    assert res.json() == {"ok": True, "live": False, "marked": False}
    async with session_factory() as db:
        run = await db.get(Run, run_id)
    assert run.status == "completed"
    assert run.error is None


async def test_cancel_of_a_young_wedged_run_is_not_marked(client, seed, session_factory):
    conversation_id = await _seed_chat(client, seed, session_factory)
    run_id = await _send(client, conversation_id)
    await _age(session_factory, run_id, seconds=2)

    res = await client.post(f"/api/runs/{run_id}/cancel")
    assert res.status_code == 200, res.text
    assert res.json() == {"ok": True, "live": False, "marked": False}
    async with session_factory() as db:
        run = await db.get(Run, run_id)
    assert run.status == "queued"
    assert run.error is None


async def test_engine_cancel_reports_liveness_including_descendants():
    eng = HarnessEngine()
    parent, child, stranger = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    assert eng.cancel(stranger) is False
    assert not eng.is_live(parent)

    eng._live.add(parent)
    assert eng.is_live(parent)
    assert eng.cancel(parent) is True

    eng._live.discard(parent)
    eng._live.add(child)
    eng._parent_of[child] = parent
    assert eng.cancel(parent) is True  # a live descendant counts


async def test_queued_delegated_child_past_the_grace_period_is_live_and_left_alone(
    client, seed, session_factory, harness_engine
):
    """A `delegate_parallel` child waiting for a slot is registered in
    `_parent_of` but not `_live`; the endpoint must not close it out."""
    conversation_id = await _seed_chat(client, seed, session_factory)
    run_id = await _send(client, conversation_id)
    await _age(session_factory, run_id, seconds=runs_api.WEDGED_RUN_GRACE.total_seconds() + 60)
    parent = uuid.uuid4()
    harness_engine._live.add(parent)
    harness_engine.register_delegation(child_id=run_id, parent_id=parent)
    assert harness_engine.is_live(run_id)

    res = await client.post(f"/api/runs/{run_id}/cancel")
    assert res.json() == {"ok": True, "live": True, "marked": False}
    async with session_factory() as db:
        run = await db.get(Run, run_id)
    assert run.status == "queued"
    assert harness_engine._is_cancelled(run_id)  # stops it when it gets its slot


def test_a_descendant_of_a_live_run_is_live_even_if_only_its_ancestor_is_registered():
    eng = HarnessEngine()
    parent, child, grandchild = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    stranger = uuid.uuid4()
    eng._live.add(parent)
    eng._parent_of[child] = parent
    eng._parent_of[grandchild] = child
    assert eng.is_live(child) and eng.is_live(grandchild)
    assert eng.cancel(grandchild) is True
    assert not eng.is_live(stranger)
    eng.unregister_delegation(child)
    eng.unregister_delegation(grandchild)
    assert not eng.is_live(child)


async def test_cancel_loses_the_race_against_a_run_that_finishes_first(
    client, seed, session_factory, harness_engine, monkeypatch
):
    conversation_id = await _seed_chat(client, seed, session_factory)
    run_id = await _send(client, conversation_id)
    await _age(session_factory, run_id, seconds=runs_api.WEDGED_RUN_GRACE.total_seconds() + 60)

    fired: list = []

    class _Registry:
        async def run_post_run_hooks(self, db, run, workspace_id):
            fired.append(run.id)

    monkeypatch.setattr(runs_api, "get_extension_registry", lambda: _Registry())

    # The run completes in another session after the endpoint loaded it and
    # before its conditional UPDATE is issued.
    orig_execute = AsyncSession.execute

    async def execute(self, statement, *a, **kw):
        if getattr(statement, "is_update", False):
            async with session_factory() as other:
                row = await other.get(Run, run_id)
                row.status = "completed"
                await other.commit()
        return await orig_execute(self, statement, *a, **kw)

    monkeypatch.setattr(AsyncSession, "execute", execute)
    res = await client.post(f"/api/runs/{run_id}/cancel")
    assert res.json() == {"ok": True, "live": False, "marked": False}
    async with session_factory() as db:
        run = await db.get(Run, run_id)
    assert run.status == "completed"
    assert run.error is None
    assert fired == []
