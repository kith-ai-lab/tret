"""runs.parent_run_id / root_run_id / delegation_kind / delegation_batch_id —
persisted delegation lineage (columns only; `run_harness_task`,
engine/tools.py, is what will eventually set them on a freshly-created
child) and the read APIs built on it: `_run_summary`'s four new fields,
`GET /api/runs/{run_id}/children`, `GET /api/runs?top_level_only=true`, and
the `tree` aggregate on `GET /api/runs/{run_id}`.

Real sqlite database, `httpx.AsyncClient` + `ASGITransport` directly against
the app — same harness as test_runs_api.py/test_conversation_spend.py.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest_asyncio
from argon2 import PasswordHasher
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from tests.evals.golden_world import install_sqlite_type_shims
from tret.api import auth
from tret.api import runs as runs_api
from tret.db.engine import get_db
from tret.db.models import Base, Harness, Project, Run, User, Workspace, WorkspaceMember

HASHER = PasswordHasher()
PASSWORD = "correct-horse-battery-1"


class _NoopEngine:
    async def execute(self, run_id):  # pragma: no cover - never actually asserted on
        return None


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
async def seed(session_factory):
    async def _seed(*rows):
        async with session_factory() as db:
            db.add_all(rows)
            await db.commit()

    return _seed


@pytest_asyncio.fixture
async def client(session_factory, monkeypatch):
    monkeypatch.setattr(runs_api, "get_harness_engine", lambda: _NoopEngine())

    app = FastAPI()
    app.include_router(auth.router)
    app.include_router(runs_api.router)

    async def _get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _get_db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def make_user(email: str) -> User:
    return User(
        id=uuid.uuid4(),
        email=email,
        display_name=email.split("@")[0].title(),
        password_hash=HASHER.hash(PASSWORD),
        role="analyst",
    )


def make_workspace(name: str) -> Workspace:
    return Workspace(id=uuid.uuid4(), name=name, kind="team")


def make_member(user: User, workspace: Workspace, *, role: str) -> WorkspaceMember:
    return WorkspaceMember(user_id=user.id, workspace_id=workspace.id, role=role)


async def login(client: httpx.AsyncClient, email: str) -> None:
    response = await client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text


async def _setup_workspace(seed, *, workspace_name: str, email: str):
    """One workspace/project/user/harness, seeded and ready to own runs."""
    team = make_workspace(workspace_name)
    project = Project(id=uuid.uuid4(), workspace_id=team.id, name="P")
    user = make_user(email)
    harness = Harness(
        workspace_id=team.id, name="H", task_profile="freeform",
        model_policy={"mode": "auto"}, tool_names=[],
    )
    await seed(team, project, user, make_member(user, team, role="analyst"), harness)
    return team, project, user, harness


def make_run(project, harness, **kwargs) -> Run:
    kwargs.setdefault("id", uuid.uuid4())
    return Run(project_id=project.id, harness_id=harness.id, task_type="freeform", task_input={}, **kwargs)


# ── _run_summary fields ──────────────────────────────────────────────────────


async def test_run_summary_lineage_fields_are_null_for_an_ordinary_run(client, seed, session_factory):
    team, project, user, harness = await _setup_workspace(seed, workspace_name="Co", email="a@example.com")
    run = make_run(project, harness)
    async with session_factory() as db:
        db.add(run)
        await db.commit()

    await login(client, user.email)
    response = await client.get(f"/api/runs/{run.id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["parent_run_id"] is None
    assert body["root_run_id"] is None
    assert body["delegation_kind"] is None
    assert body["delegation_batch_id"] is None
    # A lone run's tree is not worth reporting beyond its own summary.
    assert body["tree"] is None


async def test_run_summary_lineage_fields_are_populated_for_a_delegated_run(
    client, seed, session_factory
):
    team, project, user, harness = await _setup_workspace(seed, workspace_name="Co", email="a@example.com")
    batch_id = uuid.uuid4()
    root = make_run(project, harness)
    async with session_factory() as db:
        db.add(root)
        await db.commit()
    child = make_run(
        project, harness,
        parent_run_id=root.id, root_run_id=root.id,
        delegation_kind="task", delegation_batch_id=batch_id,
    )
    async with session_factory() as db:
        db.add(child)
        await db.commit()

    await login(client, user.email)
    response = await client.get(f"/api/runs/{child.id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["parent_run_id"] == str(root.id)
    assert body["root_run_id"] == str(root.id)
    assert body["delegation_kind"] == "task"
    assert body["delegation_batch_id"] == str(batch_id)


# ── GET /api/runs/{run_id}/children ─────────────────────────────────────────


async def test_children_returns_direct_children_only_oldest_first(client, seed, session_factory):
    team, project, user, harness = await _setup_workspace(seed, workspace_name="Co", email="a@example.com")
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    root = make_run(project, harness, created_at=base)
    async with session_factory() as db:
        db.add(root)
        await db.commit()

    child_2 = make_run(
        project, harness, parent_run_id=root.id, root_run_id=root.id,
        created_at=base + timedelta(seconds=20),
    )
    child_1 = make_run(
        project, harness, parent_run_id=root.id, root_run_id=root.id,
        created_at=base + timedelta(seconds=10),
    )
    grandchild = make_run(
        project, harness, parent_run_id=child_1.id, root_run_id=root.id,
        created_at=base + timedelta(seconds=30),
    )
    async with session_factory() as db:
        db.add_all([child_2, child_1, grandchild])
        await db.commit()

    await login(client, user.email)
    response = await client.get(f"/api/runs/{root.id}/children")
    assert response.status_code == 200, response.text
    body = response.json()
    # Direct children only — the grandchild (parent is child_1, not root) is
    # excluded — and ordered oldest-created first.
    assert [item["id"] for item in body] == [str(child_1.id), str(child_2.id)]


async def test_children_404s_for_a_run_in_another_workspace(client, seed, session_factory):
    _, project_a, user_a, harness_a = await _setup_workspace(
        seed, workspace_name="Alpha Co", email="a@example.com"
    )
    _, _, user_b, _ = await _setup_workspace(seed, workspace_name="Bravo Co", email="b@example.com")
    run_a = make_run(project_a, harness_a)
    async with session_factory() as db:
        db.add(run_a)
        await db.commit()

    await login(client, user_b.email)
    response = await client.get(f"/api/runs/{run_a.id}/children")
    assert response.status_code == 404


async def test_children_404s_for_an_unknown_run_id(client, seed, session_factory):
    team, project, user, harness = await _setup_workspace(seed, workspace_name="Co", email="a@example.com")

    await login(client, user.email)
    response = await client.get(f"/api/runs/{uuid.uuid4()}/children")
    assert response.status_code == 404


# ── GET /api/runs?top_level_only ─────────────────────────────────────────────


async def test_top_level_only_hides_delegated_runs(client, seed, session_factory):
    team, project, user, harness = await _setup_workspace(seed, workspace_name="Co", email="a@example.com")
    root = make_run(project, harness)
    async with session_factory() as db:
        db.add(root)
        await db.commit()
    child = make_run(project, harness, parent_run_id=root.id, root_run_id=root.id)
    async with session_factory() as db:
        db.add(child)
        await db.commit()

    await login(client, user.email)

    default_response = await client.get("/api/runs")
    assert default_response.status_code == 200, default_response.text
    default_body = default_response.json()
    assert isinstance(default_body, list)  # bare list shape, unchanged
    assert {item["id"] for item in default_body} == {str(root.id), str(child.id)}

    top_level_response = await client.get("/api/runs", params={"top_level_only": "true"})
    assert top_level_response.status_code == 200, top_level_response.text
    top_level_body = top_level_response.json()
    assert isinstance(top_level_body, list)
    assert [item["id"] for item in top_level_body] == [str(root.id)]


# ── GET /api/runs/{run_id} tree aggregate ────────────────────────────────────


async def test_tree_totals_and_run_count_for_a_root_with_a_grandchild(client, seed, session_factory):
    team, project, user, harness = await _setup_workspace(seed, workspace_name="Co", email="a@example.com")

    root = make_run(project, harness, cost_usd="1.00", reported_cost_usd=None, energy_wh="10")
    async with session_factory() as db:
        db.add(root)
        await db.commit()

    child_1 = make_run(
        project, harness, parent_run_id=root.id, root_run_id=root.id,
        cost_usd="2.00", reported_cost_usd="2.50", energy_wh="20",
    )
    child_2 = make_run(
        project, harness, parent_run_id=root.id, root_run_id=root.id,
        cost_usd="0.50", reported_cost_usd=None, energy_wh=None,
    )
    async with session_factory() as db:
        db.add_all([child_1, child_2])
        await db.commit()

    grandchild = make_run(
        project, harness, parent_run_id=child_1.id, root_run_id=root.id,
        cost_usd="0.25", reported_cost_usd=None, energy_wh="5",
    )
    async with session_factory() as db:
        db.add(grandchild)
        await db.commit()

    await login(client, user.email)

    for run_id in (root.id, grandchild.id):
        response = await client.get(f"/api/runs/{run_id}")
        assert response.status_code == 200, response.text
        tree = response.json()["tree"]
        assert tree is not None, f"expected a tree for {run_id}"
        assert tree["root_run_id"] == str(root.id)
        assert tree["run_count"] == 4
        assert tree["cost_usd"] == 3.75  # 1.00 + 2.00 + 0.50 + 0.25
        # coalesce(reported_cost_usd, cost_usd) per row: 1.00 + 2.50 + 0.50 + 0.25
        assert tree["reported_cost_usd"] == 4.25
        assert tree["energy_wh"] == 35.0  # 10 + 20 + 5, child_2's null ignored


# ── delegation plumbing is the engine's to stamp ─────────────────────────────


async def test_create_run_strips_caller_supplied_delegation_keys(client, seed, session_factory):
    """A hand-set `_delegation_depth` would make a root run look like a
    delegated child (tret-cloud skips the credit hold for those), and a
    negative one would buy extra hops; `_cost_cap_usd` is likewise only ever
    the engine's to set."""
    team, project, user, harness = await _setup_workspace(seed, workspace_name="Co", email="a@example.com")

    await login(client, user.email)
    response = await client.post(
        "/api/runs",
        json={
            "harness_id": str(harness.id),
            "task_input": {"message": "hi", "_delegation_depth": -5, "_cost_cap_usd": "nan"},
        },
    )
    assert response.status_code in (200, 201), response.text

    async with session_factory() as db:
        run = await db.get(Run, uuid.UUID(response.json()["run_id"]))
    assert run.task_input == {"message": "hi"}
    assert run.parent_run_id is None
