"""UI edition: deployment default (`TRET_EDITION`), per-workspace override
(`PUT /api/workspace/settings/edition`, instance admins only) and how both
surface on `/api/auth/config` and `/api/auth/me`.

Same real-sqlite `ASGITransport` setup as `test_emissions_settings_api.py`.
"""
from __future__ import annotations

import uuid

import httpx
import pytest
import pytest_asyncio
from argon2 import PasswordHasher
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from tests.evals.golden_world import install_sqlite_type_shims
from tret.api import auth, edition as edition_api
from tret.config import Settings, get_settings
from tret.db.engine import get_db
from tret.db.models import Base, User, Workspace, WorkspaceMember
from tret.services.edition import resolve_edition

HASHER = PasswordHasher()
PASSWORD = "correct-horse-battery-1"
URL = "/api/workspace/settings/edition"


@pytest.fixture(autouse=True)
def _fresh_settings(monkeypatch):
    monkeypatch.delenv("TRET_EDITION", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


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
    app.include_router(edition_api.router)

    async def _get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _get_db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _seed(session_factory, *, role="admin", settings=None, ws_role="owner"):
    user = User(
        id=uuid.uuid4(), email=f"{role}@example.com", display_name="U",
        password_hash=HASHER.hash(PASSWORD), role=role,
    )
    ws = Workspace(id=uuid.uuid4(), name="WS", kind="team", settings=settings or {})
    async with session_factory() as db:
        db.add_all([user, ws, WorkspaceMember(user_id=user.id, workspace_id=ws.id, role=ws_role)])
        await db.commit()
    return user, ws


async def _login(client, user):
    r = await client.post("/api/auth/login", json={"email": user.email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return r


def test_resolve_edition_unit():
    assert resolve_edition(None, "standard") == "standard"
    assert resolve_edition({}, "cohort") == "cohort"
    assert resolve_edition({"edition": "cohort"}, "standard") == "cohort"
    assert resolve_edition({"edition": "bogus"}, "cohort") == "cohort"
    assert resolve_edition({"edition": 3}, "standard") == "standard"


def test_config_blank_edition_is_standard():
    assert Settings(edition="").edition == "standard"
    assert Settings(edition="   ").edition == "standard"


def test_config_rejects_unknown_edition():
    with pytest.raises(ValueError):
        Settings(edition="enterprise")


async def test_default_is_standard(client, session_factory):
    user, _ = await _seed(session_factory)
    assert (await client.get("/api/auth/config")).json()["edition"] == "standard"
    login = await _login(client, user)
    assert login.json()["edition"] == "standard"
    assert (await client.get("/api/auth/me")).json()["edition"] == "standard"


async def test_deployment_setting_changes_config_and_me(client, session_factory, monkeypatch):
    monkeypatch.setenv("TRET_EDITION", "cohort")
    get_settings.cache_clear()
    user, _ = await _seed(session_factory)
    assert (await client.get("/api/auth/config")).json()["edition"] == "cohort"
    await _login(client, user)
    assert (await client.get("/api/auth/me")).json()["edition"] == "cohort"


async def test_workspace_override_wins_for_that_workspace_only(client, session_factory):
    user, ws = await _seed(session_factory)
    other = Workspace(id=uuid.uuid4(), name="Other", kind="team", settings={})
    async with session_factory() as db:
        db.add_all([other, WorkspaceMember(user_id=user.id, workspace_id=other.id, role="owner")])
        await db.commit()
    await _login(client, user)
    switched = await client.post("/api/auth/workspace", json={"workspace_id": str(ws.id)})
    assert switched.status_code == 200
    r = await client.put(URL, json={"edition": "cohort"})
    assert r.status_code == 200, r.text
    assert r.json() == {"override": "cohort", "edition": "cohort"}
    assert (await client.get("/api/auth/me")).json()["edition"] == "cohort"
    other_me = await client.post("/api/auth/workspace", json={"workspace_id": str(other.id)})
    assert other_me.json()["edition"] == "standard"
    assert (await client.get("/api/auth/me")).json()["edition"] == "standard"
    # The public config stays the deployment default regardless.
    assert (await client.get("/api/auth/config")).json()["edition"] == "standard"


async def test_invalid_value_is_422(client, session_factory):
    user, _ = await _seed(session_factory)
    await _login(client, user)
    assert (await client.put(URL, json={"edition": "enterprise"})).status_code == 422
    assert (await client.put(URL, json={"edition": "cohort", "x": 1})).status_code == 422
    assert (await client.put(URL, json={})).status_code == 422


async def test_non_global_admin_is_403(client, session_factory):
    # A workspace owner who is not a global admin may not set it.
    user, ws = await _seed(session_factory, role="analyst", ws_role="owner")
    await _login(client, user)
    assert (await client.put(URL, json={"edition": "cohort"})).status_code == 403
    async with session_factory() as db:
        assert not (await db.get(Workspace, ws.id)).settings.get("edition")


async def test_null_clears_override(client, session_factory, monkeypatch):
    monkeypatch.setenv("TRET_EDITION", "standard")
    get_settings.cache_clear()
    user, _ = await _seed(session_factory, settings={"edition": "cohort"})
    await _login(client, user)
    assert (await client.get("/api/auth/me")).json()["edition"] == "cohort"
    r = await client.put(URL, json={"edition": None})
    assert r.status_code == 200
    assert r.json() == {"override": None, "edition": "standard"}
    assert (await client.get("/api/auth/me")).json()["edition"] == "standard"


async def test_unknown_stored_value_falls_back_to_default(client, session_factory, monkeypatch):
    monkeypatch.setenv("TRET_EDITION", "cohort")
    get_settings.cache_clear()
    user, _ = await _seed(session_factory, settings={"edition": "bogus"})
    await _login(client, user)
    assert (await client.get("/api/auth/me")).json()["edition"] == "cohort"
