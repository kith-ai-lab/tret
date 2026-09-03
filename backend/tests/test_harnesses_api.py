"""`tret/api/harnesses.py`: a harness now links zero, one, or several packs
(`pack_ids`, ordered — position 0 is primary), not just the old single
nullable `pack_id`.

Real sqlite database, `httpx.AsyncClient` + `ASGITransport` directly against
the app — same harness as test_packs_api.py; the query shapes under test
(batched pack-link loads, the task_type-collision check) are exactly the kind
a hand-rolled fake session would get subtly wrong.
"""
from __future__ import annotations

import uuid

import httpx
import pytest_asyncio
from argon2 import PasswordHasher
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from tests.evals.golden_world import install_sqlite_type_shims
from tret.api import auth
from tret.api import harnesses as harnesses_api
from tret.db.engine import get_db
from tret.db.models import Base, Pack, Project, User, Workspace, WorkspaceMember

HASHER = PasswordHasher()
PASSWORD = "correct-horse-battery-1"


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
async def client(session_factory):
    app = FastAPI()
    app.include_router(auth.router)
    app.include_router(harnesses_api.router)

    async def _get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _get_db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def make_user(email: str, *, global_role: str = "analyst") -> User:
    return User(
        id=uuid.uuid4(),
        email=email,
        display_name=email.split("@")[0].title(),
        password_hash=HASHER.hash(PASSWORD),
        role=global_role,
    )


def make_workspace(name: str) -> Workspace:
    return Workspace(id=uuid.uuid4(), name=name, kind="team")


def make_member(user: User, workspace: Workspace, *, role: str) -> WorkspaceMember:
    return WorkspaceMember(user_id=user.id, workspace_id=workspace.id, role=role)


def make_pack(workspace: Workspace, *, slug: str, task_types: list[dict] | None = None) -> Pack:
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
            "task_types": task_types or [],
        },
        source_path=f"/tmp/{slug}",
    )


async def login(client: httpx.AsyncClient, email: str) -> None:
    response = await client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text


def _task_type(slug: str) -> dict:
    return {"slug": slug, "display_name": slug.title(), "shape": "freeform"}


# ── multi-pack create/update ─────────────────────────────────────────────────


async def test_create_with_multiple_pack_ids_returns_them_ordered(client, seed):
    team = make_workspace("Co")
    project = Project(id=uuid.uuid4(), workspace_id=team.id, name="P")
    admin = make_user("admin@example.com")
    pack_a = make_pack(team, slug="pack-a", task_types=[_task_type("task_a")])
    pack_b = make_pack(team, slug="pack-b", task_types=[_task_type("task_b")])
    await seed(team, project, admin, make_member(admin, team, role="admin"), pack_a, pack_b)
    await login(client, admin.email)

    response = await client.post(
        "/api/harnesses",
        json={
            "name": "Multi-Pack Analyst",
            "pack_ids": [str(pack_a.id), str(pack_b.id)],
            "tool_names": [],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    # Ordered, exactly as sent.
    assert body["pack_ids"] == [str(pack_a.id), str(pack_b.id)]
    assert body["pack_slugs"] == ["pack-a", "pack-b"]
    # Legacy single-pack fields mirror the primary (first) linked pack.
    assert body["pack_id"] == str(pack_a.id)
    assert body["pack_slug"] == "pack-a"


async def test_update_can_reorder_and_change_the_primary_pack(client, seed):
    team = make_workspace("Co")
    project = Project(id=uuid.uuid4(), workspace_id=team.id, name="P")
    admin = make_user("admin@example.com")
    pack_a = make_pack(team, slug="pack-a", task_types=[_task_type("task_a")])
    pack_b = make_pack(team, slug="pack-b", task_types=[_task_type("task_b")])
    await seed(team, project, admin, make_member(admin, team, role="admin"), pack_a, pack_b)
    await login(client, admin.email)

    created = await client.post(
        "/api/harnesses",
        json={"name": "Reorderable", "pack_ids": [str(pack_a.id), str(pack_b.id)], "tool_names": []},
    )
    harness_id = created.json()["id"]

    updated = await client.put(
        f"/api/harnesses/{harness_id}",
        json={"name": "Reorderable", "pack_ids": [str(pack_b.id), str(pack_a.id)], "tool_names": []},
    )
    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert body["pack_ids"] == [str(pack_b.id), str(pack_a.id)]
    assert body["pack_id"] == str(pack_b.id)  # primary flipped along with position 0


async def test_legacy_pack_id_is_treated_as_a_one_element_list(client, seed):
    team = make_workspace("Co")
    project = Project(id=uuid.uuid4(), workspace_id=team.id, name="P")
    admin = make_user("admin@example.com")
    pack = make_pack(team, slug="solo-pack", task_types=[_task_type("solo_task")])
    await seed(team, project, admin, make_member(admin, team, role="admin"), pack)
    await login(client, admin.email)

    response = await client.post(
        "/api/harnesses",
        json={"name": "Legacy Caller", "pack_id": str(pack.id), "tool_names": []},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["pack_ids"] == [str(pack.id)]
    assert body["pack_slugs"] == ["solo-pack"]
    assert body["pack_id"] == str(pack.id)
    assert body["pack_slug"] == "solo-pack"


async def test_unknown_pack_id_is_404(client, seed):
    team = make_workspace("Co")
    project = Project(id=uuid.uuid4(), workspace_id=team.id, name="P")
    admin = make_user("admin@example.com")
    await seed(team, project, admin, make_member(admin, team, role="admin"))
    await login(client, admin.email)

    missing = uuid.uuid4()
    response = await client.post(
        "/api/harnesses",
        json={"name": "Dangling", "pack_ids": [str(missing)], "tool_names": []},
    )
    assert response.status_code == 404
    assert str(missing) in response.text


async def test_a_task_type_collision_between_two_linked_packs_is_422(client, seed):
    team = make_workspace("Co")
    project = Project(id=uuid.uuid4(), workspace_id=team.id, name="P")
    admin = make_user("admin@example.com")
    # Both packs declare `shared_task` — an operator linking both onto one
    # harness has created an ambiguous run_harness_task target.
    pack_a = make_pack(team, slug="pack-a", task_types=[_task_type("shared_task")])
    pack_b = make_pack(team, slug="pack-b", task_types=[_task_type("shared_task")])
    await seed(team, project, admin, make_member(admin, team, role="admin"), pack_a, pack_b)
    await login(client, admin.email)

    response = await client.post(
        "/api/harnesses",
        json={
            "name": "Colliding",
            "pack_ids": [str(pack_a.id), str(pack_b.id)],
            "tool_names": [],
        },
    )
    assert response.status_code == 422
    assert "shared_task" in response.text
    assert "pack-a" in response.text
    assert "pack-b" in response.text


async def test_a_duplicate_pack_id_is_422(client, seed):
    """The same pack twice must be refused at the door — unchecked, the
    duplicate reaches the composite PK on harness_packs and surfaces as a
    500."""
    team = make_workspace("Co")
    project = Project(id=uuid.uuid4(), workspace_id=team.id, name="P")
    admin = make_user("admin@example.com")
    pack = make_pack(team, slug="pack-a", task_types=[_task_type("task_a")])
    await seed(team, project, admin, make_member(admin, team, role="admin"), pack)
    await login(client, admin.email)

    response = await client.post(
        "/api/harnesses",
        json={"name": "Doubled", "pack_ids": [str(pack.id), str(pack.id)], "tool_names": []},
    )
    assert response.status_code == 422
    assert str(pack.id) in response.text


async def test_a_pack_from_another_workspace_is_404(client, seed):
    """Linking must be scoped to the caller's workspace: a real pack id that
    belongs to a different workspace reads as not-found, never as linkable."""
    team = make_workspace("Co")
    other = make_workspace("Rival")
    project = Project(id=uuid.uuid4(), workspace_id=team.id, name="P")
    admin = make_user("admin@example.com")
    foreign_pack = make_pack(other, slug="foreign-pack", task_types=[_task_type("task_f")])
    await seed(team, other, project, admin, make_member(admin, team, role="admin"), foreign_pack)
    await login(client, admin.email)

    response = await client.post(
        "/api/harnesses",
        json={"name": "Reacher", "pack_ids": [str(foreign_pack.id)], "tool_names": []},
    )
    assert response.status_code == 404
    assert str(foreign_pack.id) in response.text


# ── GET detail: task_types is the union of every linked pack ────────────────


async def test_get_detail_unions_task_types_across_linked_packs(client, seed):
    team = make_workspace("Co")
    project = Project(id=uuid.uuid4(), workspace_id=team.id, name="P")
    admin = make_user("admin@example.com")
    pack_a = make_pack(team, slug="pack-a", task_types=[_task_type("task_a")])
    pack_b = make_pack(team, slug="pack-b", task_types=[_task_type("task_b")])
    await seed(team, project, admin, make_member(admin, team, role="admin"), pack_a, pack_b)
    await login(client, admin.email)

    created = await client.post(
        "/api/harnesses",
        json={
            "name": "Union Analyst",
            "pack_ids": [str(pack_a.id), str(pack_b.id)],
            "tool_names": [],
        },
    )
    harness_id = created.json()["id"]

    detail = await client.get(f"/api/harnesses/{harness_id}")
    assert detail.status_code == 200, detail.text
    body = detail.json()
    slugs = [(t["slug"], t["pack_slug"], t["pack_id"]) for t in body["task_types"]]
    assert slugs == [
        ("task_a", "pack-a", str(pack_a.id)),
        ("task_b", "pack-b", str(pack_b.id)),
    ]


# ── authoring requires workspace-admin ──────────────────────────────────────


async def test_analyst_cannot_create_harness(client, seed):
    team = make_workspace("Co")
    project = Project(id=uuid.uuid4(), workspace_id=team.id, name="P")
    analyst = make_user("analyst@example.com")
    await seed(team, project, analyst, make_member(analyst, team, role="analyst"))
    await login(client, analyst.email)

    response = await client.post(
        "/api/harnesses",
        json={"name": "Should Not Exist", "tool_names": []},
    )
    assert response.status_code == 403


async def test_analyst_cannot_update_harness(client, seed):
    team = make_workspace("Co")
    project = Project(id=uuid.uuid4(), workspace_id=team.id, name="P")
    admin = make_user("admin@example.com")
    analyst = make_user("analyst@example.com")
    await seed(
        team,
        project,
        admin,
        analyst,
        make_member(admin, team, role="admin"),
        make_member(analyst, team, role="analyst"),
    )
    await login(client, admin.email)
    created = await client.post(
        "/api/harnesses", json={"name": "Existing", "tool_names": []}
    )
    harness_id = created.json()["id"]

    await login(client, analyst.email)
    response = await client.put(
        f"/api/harnesses/{harness_id}",
        json={"name": "Renamed By Analyst", "tool_names": []},
    )
    assert response.status_code == 403


async def test_analyst_cannot_archive_harness(client, seed):
    team = make_workspace("Co")
    project = Project(id=uuid.uuid4(), workspace_id=team.id, name="P")
    admin = make_user("admin@example.com")
    analyst = make_user("analyst@example.com")
    await seed(
        team,
        project,
        admin,
        analyst,
        make_member(admin, team, role="admin"),
        make_member(analyst, team, role="analyst"),
    )
    await login(client, admin.email)
    created = await client.post(
        "/api/harnesses", json={"name": "Existing", "tool_names": []}
    )
    harness_id = created.json()["id"]

    await login(client, analyst.email)
    response = await client.delete(f"/api/harnesses/{harness_id}")
    assert response.status_code == 403


async def test_owner_can_create_update_and_archive_harness(client, seed):
    """A personal workspace's sole member is `owner`, above `admin` in
    ROLE_RANK, so self-host/single-user authoring is unaffected."""
    team = make_workspace("Co")
    project = Project(id=uuid.uuid4(), workspace_id=team.id, name="P")
    owner = make_user("owner@example.com")
    await seed(team, project, owner, make_member(owner, team, role="owner"))
    await login(client, owner.email)

    created = await client.post(
        "/api/harnesses", json={"name": "Owned", "tool_names": []}
    )
    assert created.status_code == 200, created.text
    harness_id = created.json()["id"]

    updated = await client.put(
        f"/api/harnesses/{harness_id}",
        json={"name": "Owned Renamed", "tool_names": []},
    )
    assert updated.status_code == 200, updated.text

    archived = await client.delete(f"/api/harnesses/{harness_id}")
    assert archived.status_code == 200, archived.text


async def test_analyst_can_still_list_and_get_harnesses(client, seed):
    team = make_workspace("Co")
    project = Project(id=uuid.uuid4(), workspace_id=team.id, name="P")
    admin = make_user("admin@example.com")
    analyst = make_user("analyst@example.com")
    await seed(
        team,
        project,
        admin,
        analyst,
        make_member(admin, team, role="admin"),
        make_member(analyst, team, role="analyst"),
    )
    await login(client, admin.email)
    created = await client.post(
        "/api/harnesses", json={"name": "Readable", "tool_names": []}
    )
    harness_id = created.json()["id"]

    await login(client, analyst.email)
    listed = await client.get("/api/harnesses")
    assert listed.status_code == 200
    assert any(h["id"] == harness_id for h in listed.json())

    detail = await client.get(f"/api/harnesses/{harness_id}")
    assert detail.status_code == 200
