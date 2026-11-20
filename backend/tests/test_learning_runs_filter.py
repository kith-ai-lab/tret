"""Learning round (A, R23): GET /api/runs?conversation_id= and conversation_title."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from tests.evals.golden_world import install_sqlite_type_shims
from tests.test_chat_api import PASSWORD, make_member, make_user, make_workspace
from tret.api import auth
from tret.api import runs as runs_api
from tret.db.engine import get_db
from tret.db.models import Base, Conversation, Harness, Project, Run

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def session_factory():
    install_sqlite_type_shims()
    eng = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(eng, expire_on_commit=False)
    await eng.dispose()


@pytest_asyncio.fixture
async def client(session_factory):
    app = FastAPI()
    app.include_router(auth.router)
    app.include_router(runs_api.router)

    async def _get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _get_db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def world(client, session_factory):
    team = make_workspace("Co")
    project = Project(id=uuid.uuid4(), workspace_id=team.id, name="P")
    user = make_user("a@example.com")
    harness = Harness(
        id=uuid.uuid4(), workspace_id=team.id, name="Chat", task_profile="chat",
        model_policy={"mode": "auto"}, tool_names=[],
    )
    conv1 = Conversation(id=uuid.uuid4(), project_id=project.id, harness_id=harness.id, title="Week 3", messages=[])
    conv2 = Conversation(id=uuid.uuid4(), project_id=project.id, harness_id=harness.id, title="Week 4", messages=[])
    async with session_factory() as db:
        db.add_all([team, project, user, make_member(user, team, role="analyst"), harness])
        await db.flush()
        db.add_all([conv1, conv2])
        await db.commit()
    r = await client.post("/api/auth/login", json={"email": user.email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return {"project": project, "harness": harness, "c1": conv1, "c2": conv2}


def _mk(world, minutes: int, conv=None, parent=None, **kw) -> Run:
    return Run(
        id=uuid.uuid4(),
        project_id=world["project"].id,
        harness_id=world["harness"].id,
        conversation_id=conv.id if conv else None,
        parent_run_id=parent.id if parent else None,
        root_run_id=parent.id if parent else None,
        task_type="chat",
        task_input={},
        status="completed",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=minutes),
        **kw,
    )


async def _seed_runs(world, session_factory):
    p1 = _mk(world, 10, world["c1"])
    kid = _mk(world, 9, world["c1"], parent=p1, stop_reason="out_of_steps")
    p2 = _mk(world, 8, world["c2"])
    loose = _mk(world, 7)
    async with session_factory() as db:
        db.add_all([p1, p2, loose])
        await db.flush()
        db.add(kid)
        await db.commit()
    return p1, kid, p2, loose


async def test_filter_by_conversation_includes_children(client, session_factory, world):
    p1, kid, p2, loose = await _seed_runs(world, session_factory)
    r = await client.get(f"/api/runs?conversation_id={world['c1'].id}")
    assert r.status_code == 200, r.text
    assert {x["id"] for x in r.json()} == {str(p1.id), str(kid.id)}

    r = await client.get(f"/api/runs?conversation_id={world['c1'].id}&top_level_only=true")
    assert [x["id"] for x in r.json()] == [str(p1.id)]

    r = await client.get(f"/api/runs?conversation_id={uuid.uuid4()}")
    assert r.json() == []
    assert len((await client.get("/api/runs")).json()) == 4


async def test_filter_works_with_paging(client, session_factory, world):
    p1, kid, *_ = await _seed_runs(world, session_factory)
    first = (await client.get(f"/api/runs?conversation_id={world['c1'].id}&limit=1")).json()
    assert [x["id"] for x in first["items"]] == [str(kid.id)] and first["next_cursor"]
    second = (
        await client.get(f"/api/runs?conversation_id={world['c1'].id}&limit=1&cursor={first['next_cursor']}")
    ).json()
    assert [x["id"] for x in second["items"]] == [str(p1.id)] and second["next_cursor"] is None


async def test_summaries_carry_conversation_title_and_stop_reason(client, session_factory, world):
    p1, kid, p2, loose = await _seed_runs(world, session_factory)
    by_id = {x["id"]: x for x in (await client.get("/api/runs")).json()}
    assert by_id[str(p1.id)]["conversation_title"] == "Week 3"
    assert by_id[str(kid.id)]["conversation_title"] == "Week 3"
    assert by_id[str(p2.id)]["conversation_title"] == "Week 4"
    assert by_id[str(loose.id)]["conversation_title"] is None
    assert by_id[str(kid.id)]["stop_reason"] == "out_of_steps"
    assert by_id[str(p1.id)]["stop_reason"] is None

    paged = (await client.get("/api/runs?limit=10")).json()["items"]
    assert {x["conversation_title"] for x in paged} == {"Week 3", "Week 4", None}


async def test_run_detail_has_stop_reason_guardrails_and_title(client, session_factory, world):
    guard = {"settings": {"number_check": True}, "overhead": {"repair_turns": 1}}
    run = _mk(world, 1, world["c2"], stop_reason="max_tokens", guardrails=guard)
    async with session_factory() as db:
        db.add(run)
        await db.commit()
    body = (await client.get(f"/api/runs/{run.id}")).json()
    assert body["stop_reason"] == "max_tokens"
    assert body["guardrails"] == guard
    assert body["conversation_title"] == "Week 4"

    plain = _mk(world, 2)
    async with session_factory() as db:
        db.add(plain)
        await db.commit()
    body = (await client.get(f"/api/runs/{plain.id}")).json()
    assert body["guardrails"] is None and body["conversation_title"] is None
