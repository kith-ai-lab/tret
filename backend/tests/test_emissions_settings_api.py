"""`tret/api/emissions_settings.py`: GET/PUT/DELETE
`/api/workspace/settings/emissions` — a workspace's own override document for
the emissions factor ladder, what it resolves to right now, and what tret
ships when nothing is configured.

Real dependency chain, real database — the same sqlite-via-`ASGITransport`
setup `test_workspaces_api.py` uses, for the identical reason: the query
shapes here (workspace settings JSONB, role checks) are exactly the kind a
hand-rolled fake session gets subtly wrong.
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
from tret.api import auth, emissions_settings as emissions_settings_api
from tret.db.engine import get_db
from tret.db.models import Base, User, Workspace, WorkspaceMember
from tret.engine import extensions as extensions_module
from tret.engine.extensions import ExtensionAPI, GateResult

HASHER = PasswordHasher()
PASSWORD = "correct-horse-battery-1"


@pytest.fixture(autouse=True)
def _reset_extension_registry():
    """Same isolation test_workspaces_api.py gives its own tests — a couple
    below register a workspace gate to exercise the refusal wiring."""
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
    app.include_router(emissions_settings_api.router)

    async def _get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _get_db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def make_user(email: str, *, password: str = PASSWORD, role: str = "analyst") -> User:
    return User(
        id=uuid.uuid4(),
        email=email,
        display_name=email.split("@")[0].title(),
        password_hash=HASHER.hash(password),
        role=role,
    )


def make_workspace(name: str, *, settings: dict | None = None) -> Workspace:
    return Workspace(id=uuid.uuid4(), name=name, kind="team", settings=settings or {})


def make_member(user: User, workspace: Workspace, *, role: str) -> WorkspaceMember:
    return WorkspaceMember(user_id=user.id, workspace_id=workspace.id, role=role)


async def login(client: httpx.AsyncClient, email: str, password: str = PASSWORD) -> httpx.Response:
    response = await client.post("/api/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response


VALID_DOC = {
    "grid": {
        "providers": {
            "anthropic": {"g_per_kwh": 120, "basis": "market_based", "label": "provider PPA disclosure"}
        }
    }
}


# ── GET ───────────────────────────────────────────────────────────────────────
async def test_get_default_shape_has_four_providers_and_global_default_layers(client, seed):
    team = make_workspace("Climate Co")
    owner = make_user("owner1@example.com")
    await seed(team, owner, make_member(owner, team, role="owner"))
    await login(client, owner.email)

    response = await client.get("/api/workspace/settings/emissions")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["overrides"] == {}
    assert set(body["effective"]) == {"local", "anthropic", "kimi", "openrouter"}
    for factors in body["effective"].values():
        assert factors["grid"]["layer"] == "global_default"
        assert factors["pue"]["layer"] == "global_default"
        assert factors["embodied_g"]["layer"] == "global_default"
        assert factors["band_low"]["layer"] == "global_default"
        assert factors["band_high"]["layer"] == "global_default"
        assert factors["baseline_model"]["layer"] == "global_default"
    shipped = body["shipped_defaults"]
    assert shipped["grid_default"]["value"] == 470.0
    assert shipped["pue_cloud"]["value"] == 1.2
    assert shipped["pue_local"]["value"] == 1.05
    assert shipped["pue_onprem"]["value"] == 1.56
    assert shipped["embodied_g_per_run"]["value"] == 0.0
    assert shipped["band_low"]["value"] == 2.5
    assert shipped["band_high"]["value"] == 2.5
    assert shipped["baseline_model"]["value"] == ""


async def test_get_with_a_broken_stored_document_fails_open(client, seed):
    """A stored `Workspace.settings["emissions"]` that no longer validates (a
    downgrade, a hand-edited row) must not 500 the read: `overrides` still
    carries the raw document (so the panel can show it and offer "clear
    overrides"), `effective` is withheld as `null`, and `error` names why.
    """
    team = make_workspace(
        "Climate Co",
        # Missing the required `label` — fails `GridBlock._labels_required`.
        settings={"emissions": {"grid": {"default": {"g_per_kwh": 42, "basis": "location_based"}}}},
    )
    owner = make_user("owner1b@example.com")
    await seed(team, owner, make_member(owner, team, role="owner"))
    await login(client, owner.email)

    response = await client.get("/api/workspace/settings/emissions")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["overrides"]["grid"]["default"]["g_per_kwh"] == 42
    assert body["effective"] is None
    assert "grid.default.label" in body["error"]
    # shipped_defaults is unaffected by the broken document.
    assert body["shipped_defaults"]["grid_default"]["value"] == 470.0


async def test_get_is_open_to_any_member(client, seed):
    team = make_workspace("Climate Co")
    owner = make_user("owner2@example.com")
    analyst = make_user("analyst2@example.com")
    await seed(
        team, owner, analyst,
        make_member(owner, team, role="owner"),
        make_member(analyst, team, role="analyst"),
    )
    await login(client, analyst.email)

    response = await client.get("/api/workspace/settings/emissions")
    assert response.status_code == 200


# ── PUT ───────────────────────────────────────────────────────────────────────
async def test_put_valid_doc_shows_workspace_layer_for_the_overridden_factor(client, seed):
    team = make_workspace("Climate Co")
    owner = make_user("owner3@example.com")
    await seed(team, owner, make_member(owner, team, role="owner"))
    await login(client, owner.email)

    response = await client.put("/api/workspace/settings/emissions", json=VALID_DOC)
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["overrides"]["grid"]["providers"]["anthropic"]["g_per_kwh"] == 120
    assert body["overrides"]["updated_by"] == owner.email
    assert body["overrides"]["updated_at"]

    anthropic = body["effective"]["anthropic"]
    assert anthropic["grid"]["layer"] == "workspace"
    assert anthropic["grid"]["value"] == 120.0
    # Every other factor for this provider still falls through beneath it.
    assert anthropic["pue"]["layer"] == "global_default"
    assert anthropic["embodied_g"]["layer"] == "global_default"
    assert anthropic["band_low"]["layer"] == "global_default"
    assert anthropic["baseline_model"]["layer"] == "global_default"
    # A provider the doc never mentioned is untouched.
    assert body["effective"]["kimi"]["grid"]["layer"] == "global_default"


async def test_put_a_number_with_no_label_is_422_naming_the_field(client, seed):
    team = make_workspace("Climate Co")
    owner = make_user("owner4@example.com")
    await seed(team, owner, make_member(owner, team, role="owner"))
    await login(client, owner.email)

    response = await client.put(
        "/api/workspace/settings/emissions",
        json={"grid": {"default": {"g_per_kwh": 42, "basis": "location_based"}}},
    )
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert isinstance(detail, str)
    assert "grid.default.label" in detail


async def test_put_pue_cloud_below_one_is_422(client, seed):
    team = make_workspace("Climate Co")
    owner = make_user("owner5@example.com")
    await seed(team, owner, make_member(owner, team, role="owner"))
    await login(client, owner.email)

    response = await client.put(
        "/api/workspace/settings/emissions",
        json={"pue": {"cloud": 0.9, "label": "too low"}},
    )
    assert response.status_code == 422, response.text
    assert "pue.cloud" in response.json()["detail"]


async def test_put_an_unknown_key_is_422(client, seed):
    team = make_workspace("Climate Co")
    owner = make_user("owner6@example.com")
    await seed(team, owner, make_member(owner, team, role="owner"))
    await login(client, owner.email)

    response = await client.put(
        "/api/workspace/settings/emissions", json={"not_a_real_field": True}
    )
    assert response.status_code == 422, response.text
    assert "not_a_real_field" in response.json()["detail"]


async def test_put_baseline_model_not_in_catalog_is_422(client, seed):
    team = make_workspace("Climate Co")
    owner = make_user("owner7@example.com")
    await seed(team, owner, make_member(owner, team, role="owner"))
    await login(client, owner.email)

    response = await client.put(
        "/api/workspace/settings/emissions",
        json={"baseline_model": "nonexistent/does-not-exist"},
    )
    assert response.status_code == 422, response.text
    assert "baseline_model" in response.json()["detail"]


async def test_put_baseline_model_that_is_local_is_422(client, seed):
    from tret.providers.catalog import get_catalog

    catalog = get_catalog()
    local_model = next((m for m in catalog.all() if m.provider == "local"), None)
    if local_model is None:
        pytest.skip("no local model configured in this catalog build")

    team = make_workspace("Climate Co")
    owner = make_user("owner7b@example.com")
    await seed(team, owner, make_member(owner, team, role="owner"))
    await login(client, owner.email)

    response = await client.put(
        "/api/workspace/settings/emissions",
        json={"baseline_model": local_model.id},
    )
    assert response.status_code == 422, response.text
    assert "baseline_model" in response.json()["detail"]


async def test_an_analyst_cannot_put(client, seed):
    team = make_workspace("Climate Co")
    owner = make_user("owner8@example.com")
    analyst = make_user("analyst8@example.com")
    await seed(
        team, owner, analyst,
        make_member(owner, team, role="owner"),
        make_member(analyst, team, role="analyst"),
    )
    await login(client, analyst.email)

    response = await client.put("/api/workspace/settings/emissions", json=VALID_DOC)
    assert response.status_code == 403


async def test_an_admin_can_put(client, seed):
    team = make_workspace("Climate Co")
    admin = make_user("admin9@example.com")
    await seed(team, admin, make_member(admin, team, role="admin"))
    await login(client, admin.email)

    response = await client.put("/api/workspace/settings/emissions", json=VALID_DOC)
    assert response.status_code == 200, response.text


async def test_a_registered_gate_that_refuses_is_a_403_with_its_reason(client, seed):
    team = make_workspace("Climate Co")
    owner = make_user("owner10@example.com")
    await seed(team, owner, make_member(owner, team, role="owner"))
    await login(client, owner.email)

    ext = ExtensionAPI(None)

    async def veto(db, workspace_id, action):
        assert action == "emissions_factors_edit"
        return GateResult(allowed=False, reason="plan_locked", detail="upgrade required")

    ext.add_workspace_gate(veto)
    extensions_module._registry = ext

    response = await client.put("/api/workspace/settings/emissions", json=VALID_DOC)
    assert response.status_code == 403
    assert response.json()["detail"]["reason"] == "plan_locked"


async def test_a_gated_delete_is_also_refused(client, seed):
    team = make_workspace("Climate Co")
    owner = make_user("owner10b@example.com")
    await seed(team, owner, make_member(owner, team, role="owner"))
    await login(client, owner.email)

    ext = ExtensionAPI(None)

    async def veto(db, workspace_id, action):
        return GateResult(allowed=False, reason="plan_locked")

    ext.add_workspace_gate(veto)
    extensions_module._registry = ext

    response = await client.delete("/api/workspace/settings/emissions")
    assert response.status_code == 403
    assert response.json()["detail"]["reason"] == "plan_locked"


# ── DELETE ────────────────────────────────────────────────────────────────────
async def test_delete_clears_the_override(client, seed):
    team = make_workspace("Climate Co")
    owner = make_user("owner11@example.com")
    await seed(team, owner, make_member(owner, team, role="owner"))
    await login(client, owner.email)

    put = await client.put("/api/workspace/settings/emissions", json=VALID_DOC)
    assert put.status_code == 200, put.text

    response = await client.delete("/api/workspace/settings/emissions")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["overrides"] == {}
    assert body["effective"]["anthropic"]["grid"]["layer"] == "global_default"


async def test_delete_requires_admin_or_owner(client, seed):
    team = make_workspace("Climate Co")
    owner = make_user("owner12@example.com")
    analyst = make_user("analyst12@example.com")
    await seed(
        team, owner, analyst,
        make_member(owner, team, role="owner"),
        make_member(analyst, team, role="analyst"),
    )
    await login(client, analyst.email)

    response = await client.delete("/api/workspace/settings/emissions")
    assert response.status_code == 403


# ── updated_by / updated_at ──────────────────────────────────────────────────
async def test_updated_by_and_updated_at_are_stamped(client, seed):
    team = make_workspace("Climate Co")
    owner = make_user("owner13@example.com")
    await seed(team, owner, make_member(owner, team, role="owner"))
    await login(client, owner.email)

    response = await client.put("/api/workspace/settings/emissions", json=VALID_DOC)
    overrides = response.json()["overrides"]
    assert overrides["updated_by"] == owner.email
    # ISO 8601 with a timezone offset — parseable, and definitely not blank.
    from datetime import datetime

    datetime.fromisoformat(overrides["updated_at"])


# ── managed layer ─────────────────────────────────────────────────────────────
async def test_a_registered_factor_layer_provider_shows_up_as_managed(client, seed):
    team = make_workspace("Climate Co")
    owner = make_user("owner14@example.com")
    await seed(team, owner, make_member(owner, team, role="owner"))
    await login(client, owner.email)

    ext = ExtensionAPI(None)

    async def managed(db, workspace_id):
        return {
            "grid": {"default": {"g_per_kwh": 55, "label": "plan-wide default"}},
            "source_name": "acme-cloud",
        }

    ext.add_factor_layer_provider(managed)
    extensions_module._registry = ext

    response = await client.get("/api/workspace/settings/emissions")
    assert response.status_code == 200, response.text
    grid = response.json()["effective"]["anthropic"]["grid"]
    assert grid["layer"] == "managed"
    assert grid["source"] == "managed:acme-cloud"
    assert grid["value"] == 55.0


async def test_a_workspace_value_beats_the_managed_layer(client, seed):
    team = make_workspace("Climate Co")
    owner = make_user("owner15@example.com")
    await seed(team, owner, make_member(owner, team, role="owner"))
    await login(client, owner.email)

    ext = ExtensionAPI(None)

    async def managed(db, workspace_id):
        return {
            "grid": {"default": {"g_per_kwh": 55, "label": "plan-wide default"}},
            "source_name": "acme-cloud",
        }

    ext.add_factor_layer_provider(managed)
    extensions_module._registry = ext

    put = await client.put(
        "/api/workspace/settings/emissions",
        json={"grid": {"default": {"g_per_kwh": 90, "basis": "location_based", "label": "our own meter"}}},
    )
    assert put.status_code == 200, put.text
    grid = put.json()["effective"]["anthropic"]["grid"]
    assert grid["layer"] == "workspace"
    assert grid["value"] == 90.0
