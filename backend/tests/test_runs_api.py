"""`POST /api/runs`: which pack a new run's `pack_id` resolves to now that a
harness may link more than one pack (one harness, many packs). Also `GET
/api/runs`'s keyset pagination (A3).

Real sqlite database, `httpx.AsyncClient` + `ASGITransport` directly against
the app — same harness as test_harnesses_api.py/test_packs_api.py. The engine
itself is never invoked: `get_harness_engine` is monkeypatched to a no-op so
these tests exercise only `create_run`'s own pack-resolution logic, not a
real model call.
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
from tret.db.models import Base, Harness, Pack, Project, Run, User, Workspace, WorkspaceMember
from tret.packs.links import set_harness_packs

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


def make_pack(workspace: Workspace, *, slug: str, task_slug: str) -> Pack:
    return Pack(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        slug=slug,
        version="1.0.0",
        doctrine_sha="deadbeef",
        manifest={
            "pack": slug,
            "version": "1.0.0",
            "display_name": slug.title(),
            "task_types": [{"slug": task_slug, "display_name": task_slug, "shape": "freeform"}],
        },
        source_path=f"/tmp/{slug}",
    )


async def login(client: httpx.AsyncClient, email: str) -> None:
    response = await client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text


async def test_run_on_a_two_pack_harness_resolves_to_the_declaring_pack(client, seed, session_factory):
    """`task_type` "task_b" is declared only by the harness's *second* linked
    pack — the created run's `pack_id` must name that pack, not the primary
    (first-linked) one."""
    team = make_workspace("Co")
    project = Project(id=uuid.uuid4(), workspace_id=team.id, name="P")
    user = make_user("analyst@example.com")
    pack_a = make_pack(team, slug="pack-a", task_slug="task_a")
    pack_b = make_pack(team, slug="pack-b", task_slug="task_b")
    await seed(team, project, user, make_member(user, team, role="analyst"), pack_a, pack_b)

    async with session_factory() as db:
        harness = Harness(
            workspace_id=team.id,
            name="Two-Pack Analyst",
            task_profile="freeform",
            model_policy={"mode": "auto"},
            tool_names=[],
        )
        db.add(harness)
        await db.flush()
        await set_harness_packs(db, harness, [pack_a.id, pack_b.id])
        await db.commit()
        harness_id = harness.id

    await login(client, user.email)
    response = await client.post(
        "/api/runs",
        json={"harness_id": str(harness_id), "task_type": "task_b", "task_input": {}},
    )
    assert response.status_code == 200, response.text
    run_id = uuid.UUID(response.json()["run_id"])

    async with session_factory() as db:
        run = await db.get(Run, run_id)
    assert run.pack_id == pack_b.id


async def test_a_freeform_run_on_a_two_pack_harness_resolves_to_the_primary_pack(
    client, seed, session_factory
):
    """No linked pack declares `freeform` — it falls back to the harness's
    primary (first-linked) pack, matching `resolve_pack_for_task`'s
    chat/freeform fallback."""
    team = make_workspace("Co")
    project = Project(id=uuid.uuid4(), workspace_id=team.id, name="P")
    user = make_user("analyst@example.com")
    pack_a = make_pack(team, slug="pack-a", task_slug="task_a")
    pack_b = make_pack(team, slug="pack-b", task_slug="task_b")
    await seed(team, project, user, make_member(user, team, role="analyst"), pack_a, pack_b)

    async with session_factory() as db:
        harness = Harness(
            workspace_id=team.id,
            name="Two-Pack Analyst",
            task_profile="freeform",
            model_policy={"mode": "auto"},
            tool_names=[],
        )
        db.add(harness)
        await db.flush()
        await set_harness_packs(db, harness, [pack_a.id, pack_b.id])
        await db.commit()
        harness_id = harness.id

    await login(client, user.email)
    response = await client.post(
        "/api/runs",
        json={"harness_id": str(harness_id), "task_type": "freeform", "task_input": {}},
    )
    assert response.status_code == 200, response.text
    run_id = uuid.UUID(response.json()["run_id"])

    async with session_factory() as db:
        run = await db.get(Run, run_id)
    assert run.pack_id == pack_a.id


async def test_an_undeclared_task_type_still_carries_the_primary_pack(
    client, seed, session_factory
):
    """A task_type no linked pack declares resolves to the primary pack, not
    None — the run still fails at execution (`unknown_task_type`), but because
    it carries the primary pack, the engine's refusal can list that pack's
    declared slugs. This is the pre-join-table behavior, when a run always
    inherited the harness's single pack."""
    team = make_workspace("Co")
    project = Project(id=uuid.uuid4(), workspace_id=team.id, name="P")
    user = make_user("analyst@example.com")
    pack_a = make_pack(team, slug="pack-a", task_slug="task_a")
    pack_b = make_pack(team, slug="pack-b", task_slug="task_b")
    await seed(team, project, user, make_member(user, team, role="analyst"), pack_a, pack_b)

    async with session_factory() as db:
        harness = Harness(
            workspace_id=team.id,
            name="Two-Pack Analyst",
            task_profile="freeform",
            model_policy={"mode": "auto"},
            tool_names=[],
        )
        db.add(harness)
        await db.flush()
        await set_harness_packs(db, harness, [pack_a.id, pack_b.id])
        await db.commit()
        harness_id = harness.id

    await login(client, user.email)
    response = await client.post(
        "/api/runs",
        json={"harness_id": str(harness_id), "task_type": "task_a_misspelled", "task_input": {}},
    )
    assert response.status_code == 200, response.text
    run_id = uuid.UUID(response.json()["run_id"])

    async with session_factory() as db:
        run = await db.get(Run, run_id)
    assert run.pack_id == pack_a.id


# ── A3: GET /api/runs keyset pagination ─────────────────────────────────────


async def test_list_runs_with_neither_limit_nor_cursor_returns_the_legacy_bare_list(
    client, seed, session_factory
):
    """No caller of the old shape breaks: omitting both params still returns
    a plain list, not `{"items": ...}`."""
    team = make_workspace("Co")
    project = Project(id=uuid.uuid4(), workspace_id=team.id, name="P")
    user = make_user("analyst@example.com")
    harness = Harness(
        workspace_id=team.id, name="H", task_profile="freeform",
        model_policy={"mode": "auto"}, tool_names=[],
    )
    await seed(team, project, user, make_member(user, team, role="analyst"), harness)
    async with session_factory() as db:
        db.add_all(
            Run(project_id=project.id, harness_id=harness.id, task_type="freeform", task_input={})
            for _ in range(3)
        )
        await db.commit()

    await login(client, user.email)
    response = await client.get("/api/runs")
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, list)
    assert len(body) == 3


async def test_list_runs_pages_through_all_sixty_with_no_duplicates(client, seed, session_factory):
    """60 runs, paged 25 at a time: every page's `next_cursor` must lead to
    the next page with no row repeated and no row skipped, until the union of
    every page is exactly the 60 that were created."""
    team = make_workspace("Co")
    project = Project(id=uuid.uuid4(), workspace_id=team.id, name="P")
    user = make_user("analyst@example.com")
    harness = Harness(
        workspace_id=team.id, name="H", task_profile="freeform",
        model_policy={"mode": "auto"}, tool_names=[],
    )
    await seed(team, project, user, make_member(user, team, role="analyst"), harness)

    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    run_ids = set()
    async with session_factory() as db:
        for i in range(60):
            run = Run(
                id=uuid.uuid4(),
                project_id=project.id,
                harness_id=harness.id,
                task_type="freeform",
                task_input={},
                # Deliberately not all distinct: several rows share a second,
                # same as real bulk-created runs would under sqlite's
                # second-level CURRENT_TIMESTAMP resolution — the id tiebreak
                # is what keeps paging correct here, not distinct timestamps.
                created_at=base + timedelta(seconds=i // 5),
            )
            db.add(run)
            run_ids.add(run.id)
        await db.commit()
    assert len(run_ids) == 60

    await login(client, user.email)

    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        params = {"limit": 25}
        if cursor is not None:
            params["cursor"] = cursor
        response = await client.get("/api/runs", params=params)
        assert response.status_code == 200, response.text
        body = response.json()
        assert "items" in body and "next_cursor" in body
        assert len(body["items"]) <= 25
        seen.extend(item["id"] for item in body["items"])
        pages += 1
        cursor = body["next_cursor"]
        if cursor is None:
            break
        assert pages < 10  # guard against an infinite loop on a real bug

    assert pages == 3  # 25 + 25 + 10
    assert len(seen) == len(set(seen)) == 60
    assert set(seen) == {str(rid) for rid in run_ids}


async def test_list_runs_paged_shape_when_only_limit_is_passed(client, seed, session_factory):
    """Passing `limit` alone (no `cursor`) also switches to the paged shape —
    matching `next_cursor` being how a caller is meant to get the rest."""
    team = make_workspace("Co")
    project = Project(id=uuid.uuid4(), workspace_id=team.id, name="P")
    user = make_user("analyst@example.com")
    harness = Harness(
        workspace_id=team.id, name="H", task_profile="freeform",
        model_policy={"mode": "auto"}, tool_names=[],
    )
    await seed(team, project, user, make_member(user, team, role="analyst"), harness)
    async with session_factory() as db:
        db.add_all(
            Run(project_id=project.id, harness_id=harness.id, task_type="freeform", task_input={})
            for _ in range(5)
        )
        await db.commit()

    await login(client, user.email)
    response = await client.get("/api/runs", params={"limit": 2})
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["items"]) == 2
    assert body["next_cursor"] is not None


async def test_list_runs_rejects_an_out_of_range_limit(client, seed, session_factory):
    team = make_workspace("Co")
    project = Project(id=uuid.uuid4(), workspace_id=team.id, name="P")
    user = make_user("analyst@example.com")
    await seed(team, project, user, make_member(user, team, role="analyst"))

    await login(client, user.email)
    response = await client.get("/api/runs", params={"limit": 0})
    assert response.status_code == 422
    response = await client.get("/api/runs", params={"limit": 201})
    assert response.status_code == 422
