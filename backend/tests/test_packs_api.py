"""`POST /api/packs/install`: instance-admin gated, not workspace-admin.

`path` names a directory on the *server's* own filesystem (`Path(body.path)`,
`install_pack`, which reads doctrine files off disk) — any workspace admin
being able to call it would turn it into a directory-existence oracle and a
doctrine-file reader over the whole host, and any user can become a
workspace admin simply by creating their own team workspace
(`POST /api/workspaces`, `services/workspace.py::create_workspace`). Gating
on the caller's *global* `role == "admin"` (`api/auth.py::require_admin`)
closes that; the data the endpoint writes still lands in the caller's
*current* workspace (`ctx.id`), exercised below alongside the gate itself.

Same real-sqlite-database harness as test_workspaces_api.py — see that
module's docstring for why a real database, not a fake session.
"""
from __future__ import annotations

import uuid
from pathlib import Path

import httpx
import pytest_asyncio
from argon2 import PasswordHasher
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from tests.evals.golden_world import install_sqlite_type_shims
from tret.api import auth, packs as packs_api
from tret.db.engine import get_db
from tret.db.models import Base, Project, User, Workspace, WorkspaceMember

PACKS_DIR = Path(__file__).parent.parent.parent / "packs"

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
    app.include_router(packs_api.router)

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


async def login(client: httpx.AsyncClient, email: str) -> None:
    response = await client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text


async def test_a_workspace_owner_who_is_not_an_instance_admin_is_refused(client, seed):
    """The bug this finding fixes: a plain user creates their own team
    workspace (owner/admin *there*) and must still not be able to install a
    pack from an arbitrary server path."""
    team = make_workspace("Climate Co")
    project = Project(id=uuid.uuid4(), workspace_id=team.id, name="Sample")
    owner = make_user("owner@example.com", global_role="analyst")
    await seed(team, project, owner, make_member(owner, team, role="owner"))
    await login(client, owner.email)

    response = await client.post(
        "/api/packs/install", json={"path": str(PACKS_DIR / "climate-risk")}
    )
    assert response.status_code == 403


async def test_a_workspace_admin_who_is_not_an_instance_admin_is_refused(client, seed):
    team = make_workspace("Climate Co")
    project = Project(id=uuid.uuid4(), workspace_id=team.id, name="Sample")
    admin = make_user("wsadmin@example.com", global_role="analyst")
    await seed(team, project, admin, make_member(admin, team, role="admin"))
    await login(client, admin.email)

    response = await client.post(
        "/api/packs/install", json={"path": str(PACKS_DIR / "climate-risk")}
    )
    assert response.status_code == 403


async def test_an_instance_admin_can_install_into_their_current_workspace(client, seed):
    team = make_workspace("Climate Co")
    project = Project(id=uuid.uuid4(), workspace_id=team.id, name="Sample")
    admin = make_user("instanceadmin@example.com", global_role="admin")
    await seed(team, project, admin, make_member(admin, team, role="analyst"))
    await login(client, admin.email)

    response = await client.post(
        "/api/packs/install", json={"path": str(PACKS_DIR / "climate-risk")}
    )
    assert response.status_code == 200, response.text
    assert response.json()["slug"] == "climate-risk"

    listed = await client.get("/api/packs")
    assert response.json()["id"] in {p["id"] for p in listed.json()}


async def test_an_instance_admin_with_no_workspace_membership_gets_the_409(client, seed):
    """`current_workspace` itself is the thing that decides which workspace
    `ctx` names — an instance admin belonging to none gets the same 409
    every other unscoped endpoint gives, not a silent write to nothing."""
    admin = make_user("lonelyadmin@example.com", global_role="admin")
    await seed(admin)
    await login(client, admin.email)

    response = await client.post(
        "/api/packs/install", json={"path": str(PACKS_DIR / "climate-risk")}
    )
    assert response.status_code == 409


async def test_installed_pack_is_scoped_to_the_admins_current_workspace_only(client, seed):
    """Cross-workspace isolation: installing into workspace A must not make
    the pack visible from workspace B."""
    team_a = make_workspace("Alpha Co")
    team_b = make_workspace("Bravo Co")
    project_a = Project(id=uuid.uuid4(), workspace_id=team_a.id, name="Sample A")
    project_b = Project(id=uuid.uuid4(), workspace_id=team_b.id, name="Sample B")
    admin = make_user("crossadmin@example.com", global_role="admin")
    await seed(
        team_a, team_b, project_a, project_b, admin,
        make_member(admin, team_a, role="analyst"),
        make_member(admin, team_b, role="analyst"),
    )
    await login(client, admin.email)
    await client.post("/api/auth/workspace", json={"workspace_id": str(team_a.id)})

    response = await client.post(
        "/api/packs/install", json={"path": str(PACKS_DIR / "climate-risk")}
    )
    assert response.status_code == 200, response.text
    pack_id = response.json()["id"]

    await client.post("/api/auth/workspace", json={"workspace_id": str(team_b.id)})
    listed_b = await client.get("/api/packs")
    assert pack_id not in {p["id"] for p in listed_b.json()}
