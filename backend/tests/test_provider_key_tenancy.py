"""`GET /api/settings/providers` and `GET /api/models`: both call
`load_db_keys(db)` — the finding this file pins is that calling it
*unscoped* leaked a workspace's provider-key presence, last4 and model
"available" flags to every other workspace on the deployment, since
`load_db_keys()`'s default (`workspace_id=None`) is "every stored key across
every workspace, last one wins per provider" — a deliberate default for
callers that legitimately want that (see its own docstring), just not these
two.

Real sqlite database, same harness as test_packs_api.py/test_workspaces_api.py
— provider-key scoping is exactly the kind of query shape a fake session
would get subtly wrong.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import httpx
import pytest_asyncio
from argon2 import PasswordHasher
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from tests.evals.golden_world import install_sqlite_type_shims
from tret.api import auth, settings as settings_api
from tret.db.engine import get_db
from tret.db.models import Base, ProviderCredential, User, Workspace, WorkspaceMember
from tret.providers.catalog import ModelInfo
from tret.services.credentials import get_fernet

HASHER = PasswordHasher()
PASSWORD = "correct-horse-battery-1"


def _model(model_id: str, provider: str) -> ModelInfo:
    return ModelInfo(
        id=model_id,
        provider=provider,
        wire_id=model_id.split("/")[-1],
        display_name=model_id,
        context_window=200_000,
        input_price_per_mtok=Decimal("3"),
        output_price_per_mtok=Decimal("15"),
        cost_tier="standard",
        energy_class="L",
    )


class FakeCatalog:
    """No network: `list_models` only needs `.all()` here, not real
    discovery — the discovery/timeout behaviour is test_models_endpoint.py's
    concern, not this file's."""

    def __init__(self):
        self.models = [_model("kimi/k2", "kimi")]

    async def refresh_dynamic(self):
        pass

    async def refresh_local(self, *, force: bool = False):
        pass

    def all(self):
        return list(self.models)


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
    monkeypatch.setattr(settings_api, "get_catalog", lambda: FakeCatalog())
    app = FastAPI()
    app.include_router(auth.router)
    app.include_router(settings_api.router)

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


def make_member(user: User, workspace: Workspace, *, role: str = "owner") -> WorkspaceMember:
    return WorkspaceMember(user_id=user.id, workspace_id=workspace.id, role=role)


def make_credential(workspace: Workspace, *, provider: str, api_key: str) -> ProviderCredential:
    return ProviderCredential(
        workspace_id=workspace.id,
        provider=provider,
        encrypted_key=get_fernet().encrypt(api_key.encode()),
    )


async def login(client: httpx.AsyncClient, email: str) -> None:
    response = await client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text


async def _seeded_tenants(seed):
    a = make_workspace("Alpha")
    b = make_workspace("Bravo")
    user_a = make_user("keytenancy-a@example.com")
    user_b = make_user("keytenancy-b@example.com")
    await seed(
        a, b, user_a, user_b,
        make_member(user_a, a),
        make_member(user_b, b),
        make_credential(b, provider="kimi", api_key="sk-bravo-secret-0000"),
    )
    return a, b, user_a, user_b


async def test_provider_status_does_not_leak_a_foreign_workspaces_key(client, seed):
    _a, _b, user_a, _user_b = await _seeded_tenants(seed)
    await login(client, user_a.email)

    body = (await client.get("/api/settings/providers")).json()
    (kimi,) = [p for p in body if p["provider"] == "kimi"]
    assert kimi["configured"] is False
    assert kimi["source"] is None
    assert kimi["last4"] is None  # not bravo's "0000"


async def test_provider_status_reports_the_actual_owning_workspaces_key(client, seed):
    _a, _b, _user_a, user_b = await _seeded_tenants(seed)
    await login(client, user_b.email)

    body = (await client.get("/api/settings/providers")).json()
    (kimi,) = [p for p in body if p["provider"] == "kimi"]
    assert kimi["configured"] is True
    assert kimi["source"] == "db"
    assert kimi["last4"] == "0000"


async def test_models_availability_does_not_leak_a_foreign_workspaces_key(client, seed):
    _a, _b, user_a, _user_b = await _seeded_tenants(seed)
    await login(client, user_a.email)

    body = (await client.get("/api/models")).json()
    (kimi_model,) = [m for m in body if m["provider"] == "kimi"]
    assert kimi_model["available"] is False


async def test_models_availability_reflects_the_current_workspaces_own_key(client, seed):
    _a, _b, _user_a, user_b = await _seeded_tenants(seed)
    await login(client, user_b.email)

    body = (await client.get("/api/models")).json()
    (kimi_model,) = [m for m in body if m["provider"] == "kimi"]
    assert kimi_model["available"] is True
