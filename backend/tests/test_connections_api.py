"""`tret/api/connections.py`: the workspace-connections HTTP surface —
providers, authorize -> callback (signed, cookie-less state; see that
module's own docstring for why no server session or sid cookie is needed
here, unlike `api/oidc.py`), list, delete, and workspace isolation.

Real dependency chain, real sqlite database, respx-mocked provider HTTP —
`test_workspaces_api.py`'s harness (session-cookie login, `current_
workspace`'s sole-membership resolution, the extension-registry reset
fixture, the gate-refusal shape) crossed with `test_oidc_login.py`'s (respx
mocking, the itsdangerous state-tamper technique) since this router borrows
a piece of each.
"""
from __future__ import annotations

import base64
import json
import uuid
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import pytest_asyncio
import respx
from argon2 import PasswordHasher
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from tests.evals.golden_world import install_sqlite_type_shims
from tret.api import auth, connections as connections_api
from tret.config import get_settings
from tret.db.engine import get_db
from tret.db.models import Base, User, Workspace, WorkspaceConnection, WorkspaceMember
from tret.engine import extensions as extensions_module
from tret.engine.extensions import ExtensionAPI, GateResult
from tret.services import connections as connections_module
from tret.services.connections import GRAPH_API_BASE, clear_access_token_cache
from tret.services.credentials import get_fernet

HASHER = PasswordHasher()
PASSWORD = "correct-horse-battery-1"

GDRIVE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GDRIVE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
M365_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
GRAPH = GRAPH_API_BASE
APP_URL = "https://tret.example.test"


@pytest.fixture(autouse=True)
def _reset_extension_registry():
    """Same isolation test_workspaces_api.py gives itself — a couple of
    tests below register a workspace gate to exercise the 403 wiring."""
    extensions_module._registry = None
    clear_access_token_cache()
    yield
    extensions_module._registry = None
    clear_access_token_cache()


@pytest.fixture(autouse=True)
def _configured_gdrive_client(monkeypatch):
    """gdrive configured, m365 deliberately left unset — `test_providers_*`
    below exercises both states off the same fixture. `TRET_APP_URL` is set
    so `_redirect_uri()` builds a fixed, assertable callback URL rather than
    the http://localhost:8000 default."""
    monkeypatch.setenv("TRET_GDRIVE_CLIENT_ID", "gdrive-cid")
    monkeypatch.setenv("TRET_GDRIVE_CLIENT_SECRET", "gdrive-secret")
    monkeypatch.setenv("TRET_APP_URL", APP_URL)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def _configured_m365_client(monkeypatch):
    """m365 configured too — only the `m365/sources` and `m365/resources`
    tests below need this; every other test in this file deliberately
    leaves m365 unset (see `_configured_gdrive_client`)."""
    monkeypatch.setenv("TRET_M365_CLIENT_ID", "m365-cid")
    monkeypatch.setenv("TRET_M365_CLIENT_SECRET", "m365-secret")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


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
    app.include_router(connections_api.router)

    async def _get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _get_db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ── fixture builders ─────────────────────────────────────────────────────────
def make_user(email: str, *, role: str = "analyst") -> User:
    return User(
        id=uuid.uuid4(),
        email=email,
        display_name=email.split("@")[0].title(),
        password_hash=HASHER.hash(PASSWORD),
        role=role,
    )


def make_workspace(name: str) -> Workspace:
    return Workspace(id=uuid.uuid4(), name=name, kind="team")


def make_member(user: User, workspace: Workspace, *, role: str) -> WorkspaceMember:
    return WorkspaceMember(user_id=user.id, workspace_id=workspace.id, role=role)


def make_connection(
    workspace: Workspace, *, provider: str = "gdrive", account_label: str = "person@example.com"
) -> WorkspaceConnection:
    return WorkspaceConnection(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        provider=provider,
        account_label=account_label,
        encrypted_refresh_token=get_fernet().encrypt(b"stored-refresh-token"),
        granted_scopes=["openid", "email"],
        status="active",
    )


async def login(client: httpx.AsyncClient, email: str) -> None:
    response = await client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text


def mock_m365_refresh(mock: respx.MockRouter, *, access_token: str = "m365-access-token") -> None:
    mock.post(M365_TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": access_token, "expires_in": 3600})
    )


def _b64(obj: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode("ascii")


def _gdrive_id_token(email: str) -> str:
    """Unsigned id_token payload — `services/connections.py::_decode_gdrive_
    email` decodes only, no signature check (see its own docstring)."""
    return f"{_b64({'alg': 'none', 'typ': 'JWT'})}.{_b64({'email': email, 'sub': 'gdrive-sub'})}."


def _tamper_state_payload(state: str) -> str:
    """test_oidc_login.py's own technique (see its docstring for why): flip a
    byte inside the decoded itsdangerous *payload* segment, never the raw
    string's tail — a tail flip can decode to identical bytes and the test
    would flake."""
    *payload_parts, timestamp_b64, sig_b64 = state.split(".")
    payload_segment = ".".join(payload_parts)
    compressed_marker = payload_segment.startswith(".")
    body = payload_segment[1:] if compressed_marker else payload_segment
    padded = body + "=" * (-len(body) % 4)
    raw = bytearray(base64.urlsafe_b64decode(padded))
    raw[0] ^= 0xFF
    tampered_body = base64.urlsafe_b64encode(bytes(raw)).rstrip(b"=").decode("ascii")
    tampered_segment = f".{tampered_body}" if compressed_marker else tampered_body
    return f"{tampered_segment}.{timestamp_b64}.{sig_b64}"


async def _authorize_state(client: httpx.AsyncClient, provider: str = "gdrive") -> tuple[str, str]:
    """POST /{provider}/authorize while already logged in as an admin;
    returns (authorize_url, state)."""
    response = await client.post(f"/api/connections/{provider}/authorize")
    assert response.status_code == 200, response.text
    authorize_url = response.json()["authorize_url"]
    state = parse_qs(urlsplit(authorize_url).query)["state"][0]
    return authorize_url, state


# ── GET /api/connections/providers ───────────────────────────────────────────
async def test_providers_reflects_configured_and_picker(client, seed, monkeypatch):
    workspace = make_workspace("Alpha")
    user = make_user("provider-view@example.com")
    await seed(workspace, user, make_member(user, workspace, role="analyst"))
    await login(client, user.email)

    body = (await client.get("/api/connections/providers")).json()
    by_provider = {p["provider"]: p for p in body["providers"]}
    assert by_provider["gdrive"]["configured"] is True  # env set by the autouse fixture
    assert by_provider["gdrive"]["connected"] is False
    assert by_provider["gdrive"]["picker"] is None  # no picker key/app id set yet
    assert by_provider["m365"]["configured"] is False  # deliberately left unset
    assert by_provider["m365"]["connected"] is False
    assert by_provider["m365"]["picker"] is None

    monkeypatch.setenv("TRET_GDRIVE_PICKER_API_KEY", "picker-key")
    monkeypatch.setenv("TRET_GDRIVE_APP_ID", "gcp-project-number")
    get_settings.cache_clear()

    body = (await client.get("/api/connections/providers")).json()
    by_provider = {p["provider"]: p for p in body["providers"]}
    assert by_provider["gdrive"]["picker"] == {
        "api_key": "picker-key", "app_id": "gcp-project-number",
    }
    assert by_provider["m365"]["picker"] is None  # picker is gdrive-only


async def test_providers_connected_reflects_an_existing_row(client, seed):
    workspace = make_workspace("Alpha")
    user = make_user("provider-connected@example.com")
    conn = make_connection(workspace)
    await seed(workspace, user, make_member(user, workspace, role="analyst"), conn)
    await login(client, user.email)

    body = (await client.get("/api/connections/providers")).json()
    by_provider = {p["provider"]: p for p in body["providers"]}
    assert by_provider["gdrive"]["connected"] is True
    assert by_provider["m365"]["connected"] is False


# ── GET /api/connections ──────────────────────────────────────────────────────
async def test_list_connections_never_exposes_the_token(client, seed):
    workspace = make_workspace("Alpha")
    user = make_user("list-view@example.com")
    conn = make_connection(workspace, account_label="listed@example.com")
    await seed(workspace, user, make_member(user, workspace, role="analyst"), conn)
    await login(client, user.email)

    body = (await client.get("/api/connections")).json()
    assert len(body["connections"]) == 1
    entry = body["connections"][0]
    assert entry["provider"] == "gdrive"
    assert entry["account_label"] == "listed@example.com"
    assert entry["status"] == "active"
    assert "encrypted_refresh_token" not in entry
    assert "refresh_token" not in json.dumps(entry)


# ── authorize -> callback ─────────────────────────────────────────────────────
async def test_authorize_requires_admin(client, seed):
    workspace = make_workspace("Alpha")
    analyst = make_user("analyst-auth@example.com", role="analyst")
    await seed(workspace, analyst, make_member(analyst, workspace, role="analyst"))
    await login(client, analyst.email)

    response = await client.post("/api/connections/gdrive/authorize")
    assert response.status_code == 403


async def test_authorize_returns_a_signed_state_and_the_configured_redirect_uri(client, seed):
    workspace = make_workspace("Alpha")
    admin = make_user("auth-admin@example.com")
    await seed(workspace, admin, make_member(admin, workspace, role="admin"))
    await login(client, admin.email)

    authorize_url, state = await _authorize_state(client)
    qs = parse_qs(urlsplit(authorize_url).query)
    assert qs["client_id"][0] == "gdrive-cid"
    assert qs["redirect_uri"][0] == f"{APP_URL}/api/connections/callback"
    assert qs["access_type"][0] == "offline"
    assert qs["prompt"][0] == "consent"

    unpacked = connections_api._state_serializer().loads(state)
    assert unpacked["workspace_id"] == str(workspace.id)
    assert unpacked["user_id"] == str(admin.id)
    assert unpacked["provider"] == "gdrive"


async def test_authorize_for_an_unconfigured_provider_is_503(client, seed):
    workspace = make_workspace("Alpha")
    admin = make_user("m365-admin@example.com")
    await seed(workspace, admin, make_member(admin, workspace, role="admin"))
    await login(client, admin.email)

    response = await client.post("/api/connections/m365/authorize")  # m365 has no env vars set
    assert response.status_code == 503


async def test_authorize_is_blocked_by_a_registered_workspace_gate(client, seed):
    workspace = make_workspace("Alpha")
    admin = make_user("gate-admin@example.com")
    await seed(workspace, admin, make_member(admin, workspace, role="admin"))
    await login(client, admin.email)

    ext = ExtensionAPI(None)

    async def veto(db, workspace_id, action):
        assert action == "connections.connect"
        assert workspace_id == workspace.id
        return GateResult(
            allowed=False, reason="plan_required", detail="Connections require an active plan."
        )

    ext.add_workspace_gate(veto)
    extensions_module._registry = ext

    response = await client.post("/api/connections/gdrive/authorize")
    assert response.status_code == 403
    assert response.json()["detail"]["reason"] == "plan_required"


async def test_authorize_then_callback_upserts_the_connection(client, seed, session_factory):
    workspace = make_workspace("Alpha")
    admin = make_user("callback-admin@example.com")
    await seed(workspace, admin, make_member(admin, workspace, role="admin"))
    await login(client, admin.email)

    _authorize_url, state = await _authorize_state(client)

    id_token = _gdrive_id_token("connected-account@example.com")
    with respx.mock(assert_all_called=True) as mock:
        mock.post(GDRIVE_TOKEN_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "gdrive-access-token",
                    "refresh_token": "gdrive-refresh-token",
                    "scope": "openid email https://www.googleapis.com/auth/drive.file",
                    "id_token": id_token,
                },
            )
        )
        response = await client.get(
            "/api/connections/callback",
            params={"code": "auth-code", "state": state},
            follow_redirects=False,
        )
    assert response.status_code == 302
    assert response.headers["location"] == "/settings/connections?connected=gdrive"

    async with session_factory() as db:
        rows = (
            await db.execute(
                select(WorkspaceConnection).where(WorkspaceConnection.workspace_id == workspace.id)
            )
        ).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.provider == "gdrive"
    assert row.account_label == "connected-account@example.com"
    assert row.status == "active"
    assert row.connected_by == admin.id
    assert get_fernet().decrypt(row.encrypted_refresh_token).decode() == "gdrive-refresh-token"
    assert row.granted_scopes == ["openid", "email", "https://www.googleapis.com/auth/drive.file"]


async def test_callback_on_an_existing_connection_upserts_rather_than_duplicates(
    client, seed, session_factory
):
    """A second connect (reconnecting after an error, or just running consent
    again) must update the one row, not create a second — the unique
    `(workspace_id, provider)` constraint the model declares."""
    workspace = make_workspace("Alpha")
    admin = make_user("reconnect-admin@example.com")
    stale = make_connection(workspace, account_label="stale@example.com")
    stale.status = "error"
    stale.error_detail = "invalid_grant"
    await seed(workspace, admin, make_member(admin, workspace, role="admin"), stale)
    await login(client, admin.email)

    _authorize_url, state = await _authorize_state(client)
    id_token = _gdrive_id_token("fresh-account@example.com")
    with respx.mock(assert_all_called=True) as mock:
        mock.post(GDRIVE_TOKEN_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "tok",
                    "refresh_token": "fresh-refresh-token",
                    "scope": "openid email https://www.googleapis.com/auth/drive.file",
                    "id_token": id_token,
                },
            )
        )
        response = await client.get(
            "/api/connections/callback",
            params={"code": "auth-code", "state": state},
            follow_redirects=False,
        )
    assert response.status_code == 302

    async with session_factory() as db:
        rows = (
            await db.execute(
                select(WorkspaceConnection).where(WorkspaceConnection.workspace_id == workspace.id)
            )
        ).scalars().all()
    assert len(rows) == 1  # updated, not duplicated
    assert rows[0].id == stale.id
    assert rows[0].account_label == "fresh-account@example.com"
    assert rows[0].status == "active"
    assert rows[0].error_detail is None


async def test_callback_invalidates_any_cached_access_token_for_the_connection(
    client, seed, session_factory
):
    """A reconnect may attach a different provider account entirely, so a
    token this process cached under the connection's old grant must not
    survive the callback that (re)establishes it."""
    workspace = make_workspace("Alpha")
    admin = make_user("callback-cache-admin@example.com")
    stale = make_connection(workspace, account_label="stale@example.com")
    await seed(workspace, admin, make_member(admin, workspace, role="admin"), stale)
    await login(client, admin.email)

    key = (workspace.id, "gdrive")
    connections_module._access_token_cache[key] = connections_module._CachedAccessToken(
        access_token="cached-under-the-old-grant", expires_at=connections_module.time.monotonic() + 3600
    )

    _authorize_url, state = await _authorize_state(client)
    id_token = _gdrive_id_token("fresh-account@example.com")
    with respx.mock(assert_all_called=True) as mock:
        mock.post(GDRIVE_TOKEN_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "tok",
                    "refresh_token": "fresh-refresh-token",
                    "scope": "openid email https://www.googleapis.com/auth/drive.file",
                    "id_token": id_token,
                },
            )
        )
        response = await client.get(
            "/api/connections/callback",
            params={"code": "auth-code", "state": state},
            follow_redirects=False,
        )
    assert response.status_code == 302
    assert key not in connections_module._access_token_cache


# ── sid cookie: browser binding ──────────────────────────────────────────────
async def test_callback_missing_sid_cookie_is_bad_state(client, seed):
    """A `(code, state)` pair presented by a browser that never went through
    `authorize` (no `tret_conn_sid` cookie at all) — the lure shape this
    finding fixes: an attacker starts their own `/authorize`, then hands the
    resulting `(code, state)` to a victim admin whose browser never received
    the cookie `authorize` would have set."""
    workspace = make_workspace("Alpha")
    admin = make_user("sid-missing-admin@example.com")
    await seed(workspace, admin, make_member(admin, workspace, role="admin"))
    await login(client, admin.email)

    _authorize_url, state = await _authorize_state(client)
    client.cookies.pop(connections_api.SID_COOKIE, None)  # the cookie authorize() set, dropped

    response = await client.get(
        "/api/connections/callback",
        params={"code": "auth-code", "state": state},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert response.headers["location"] == "/settings/connections?error=bad_state"


async def test_callback_mismatched_sid_cookie_is_bad_state(client, seed):
    """The cookie is present but does not match the `sid` signed into
    `state` — e.g. a second, unrelated authorize attempt's cookie still
    sitting in the jar."""
    workspace = make_workspace("Alpha")
    admin = make_user("sid-mismatch-admin@example.com")
    await seed(workspace, admin, make_member(admin, workspace, role="admin"))
    await login(client, admin.email)

    _authorize_url, state = await _authorize_state(client)
    client.cookies.set(connections_api.SID_COOKIE, "not-the-sid-that-was-issued")

    response = await client.get(
        "/api/connections/callback",
        params={"code": "auth-code", "state": state},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert response.headers["location"] == "/settings/connections?error=bad_state"


async def test_sid_cookie_is_set_on_authorize_and_cleared_on_callback(client, seed):
    """Happy path with the cookie present: authorize() sets it, and the
    successful callback clears it — a captured, already-used callback URL
    cannot be replayed a second time even by the browser that legitimately
    started it."""
    workspace = make_workspace("Alpha")
    admin = make_user("sid-lifecycle-admin@example.com")
    await seed(workspace, admin, make_member(admin, workspace, role="admin"))
    await login(client, admin.email)

    authorize_response = await client.post("/api/connections/gdrive/authorize")
    assert authorize_response.status_code == 200
    assert connections_api.SID_COOKIE in client.cookies  # authorize() set it
    state = parse_qs(urlsplit(authorize_response.json()["authorize_url"]).query)["state"][0]

    id_token = _gdrive_id_token("cookie-lifecycle@example.com")
    with respx.mock(assert_all_called=True) as mock:
        mock.post(GDRIVE_TOKEN_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "tok",
                    "refresh_token": "refresh-tok",
                    "scope": "openid email https://www.googleapis.com/auth/drive.file",
                    "id_token": id_token,
                },
            )
        )
        response = await client.get(
            "/api/connections/callback",
            params={"code": "auth-code", "state": state},
            follow_redirects=False,
        )
    assert response.status_code == 302
    assert response.headers["location"] == "/settings/connections?connected=gdrive"

    set_cookie_headers = response.headers.get_list("set-cookie")
    cleared = [h for h in set_cookie_headers if h.startswith(f"{connections_api.SID_COOKIE}=")]
    assert cleared, set_cookie_headers
    assert "Max-Age=0" in cleared[0] or "max-age=0" in cleared[0].lower()


# ── admin re-check at callback time ──────────────────────────────────────────
async def test_callback_with_a_demoted_user_is_bad_state(client, seed, session_factory):
    """`state`'s user_id was an admin when `/authorize` minted it, but is
    demoted to a weaker role before the provider redirects back — the
    re-check must catch this before ever touching the provider (no respx
    mock is installed here, so a token exchange attempt would blow up the
    test rather than the callback quietly proceeding)."""
    workspace = make_workspace("Alpha")
    admin = make_user("demoted-admin@example.com")
    await seed(workspace, admin, make_member(admin, workspace, role="admin"))
    await login(client, admin.email)

    _authorize_url, state = await _authorize_state(client)

    async with session_factory() as db:
        row = await db.get(WorkspaceMember, (admin.id, workspace.id))
        row.role = "analyst"
        await db.commit()

    response = await client.get(
        "/api/connections/callback",
        params={"code": "auth-code", "state": state},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert response.headers["location"] == "/settings/connections?error=bad_state"


async def test_callback_with_a_removed_membership_is_bad_state(client, seed, session_factory):
    """`state`'s user_id was an admin of the workspace at authorize time but
    has since been removed from it entirely."""
    workspace = make_workspace("Alpha")
    admin = make_user("removed-admin@example.com")
    await seed(workspace, admin, make_member(admin, workspace, role="admin"))
    await login(client, admin.email)

    _authorize_url, state = await _authorize_state(client)

    async with session_factory() as db:
        row = await db.get(WorkspaceMember, (admin.id, workspace.id))
        await db.delete(row)
        await db.commit()

    response = await client.get(
        "/api/connections/callback",
        params={"code": "auth-code", "state": state},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert response.headers["location"] == "/settings/connections?error=bad_state"


async def test_callback_with_a_tampered_state_redirects_with_an_error(client, seed):
    workspace = make_workspace("Alpha")
    admin = make_user("tamper-admin@example.com")
    await seed(workspace, admin, make_member(admin, workspace, role="admin"))
    await login(client, admin.email)

    _authorize_url, state = await _authorize_state(client)
    tampered = _tamper_state_payload(state)

    response = await client.get(
        "/api/connections/callback",
        params={"code": "auth-code", "state": tampered},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert response.headers["location"] == "/settings/connections?error=bad_state"


async def test_callback_missing_code_or_state_redirects_with_an_error(client):
    response = await client.get(
        "/api/connections/callback", params={"state": "whatever-state"}, follow_redirects=False
    )
    assert response.status_code == 302
    assert response.headers["location"] == "/settings/connections?error=missing_code_or_state"


async def test_callback_with_a_provider_error_redirects_with_an_error(client):
    response = await client.get(
        "/api/connections/callback",
        params={"error": "access_denied", "state": "whatever-state"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert response.headers["location"] == "/settings/connections?error=provider_denied"


# ── DELETE /api/connections/{provider} ───────────────────────────────────────
async def test_disconnect_removes_the_row_and_best_effort_revokes(client, seed, session_factory):
    workspace = make_workspace("Alpha")
    admin = make_user("disconnect-admin@example.com")
    conn = make_connection(workspace)
    await seed(workspace, admin, make_member(admin, workspace, role="admin"), conn)
    await login(client, admin.email)

    with respx.mock(assert_all_called=True) as mock:
        mock.post(GDRIVE_REVOKE_URL).mock(return_value=httpx.Response(200))
        response = await client.delete("/api/connections/gdrive")
    assert response.status_code == 200
    assert response.json() == {"ok": True}

    async with session_factory() as db:
        rows = (
            await db.execute(
                select(WorkspaceConnection).where(WorkspaceConnection.workspace_id == workspace.id)
            )
        ).scalars().all()
    assert rows == []


async def test_disconnect_with_no_connection_is_a_no_op(client, seed):
    workspace = make_workspace("Alpha")
    admin = make_user("noop-admin@example.com")
    await seed(workspace, admin, make_member(admin, workspace, role="admin"))
    await login(client, admin.email)

    response = await client.delete("/api/connections/gdrive")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


async def test_disconnect_clears_any_cached_access_token(client, seed):
    """A token this process cached for the connection must not survive its
    disconnect — nothing should be servable for a connection that no longer
    exists."""
    workspace = make_workspace("Alpha")
    admin = make_user("disconnect-cache-admin@example.com")
    conn = make_connection(workspace)
    await seed(workspace, admin, make_member(admin, workspace, role="admin"), conn)
    await login(client, admin.email)

    key = (workspace.id, "gdrive")
    connections_module._access_token_cache[key] = connections_module._CachedAccessToken(
        access_token="cached-before-disconnect", expires_at=connections_module.time.monotonic() + 3600
    )

    with respx.mock(assert_all_called=True) as mock:
        mock.post(GDRIVE_REVOKE_URL).mock(return_value=httpx.Response(200))
        response = await client.delete("/api/connections/gdrive")
    assert response.status_code == 200

    assert key not in connections_module._access_token_cache


async def test_disconnect_requires_admin(client, seed):
    workspace = make_workspace("Alpha")
    analyst = make_user("analyst-disconnect@example.com", role="analyst")
    conn = make_connection(workspace)
    await seed(workspace, analyst, make_member(analyst, workspace, role="analyst"), conn)
    await login(client, analyst.email)

    response = await client.delete("/api/connections/gdrive")
    assert response.status_code == 403


# ── workspace isolation ───────────────────────────────────────────────────────
async def test_workspace_isolation_cannot_see_or_disconnect_another_workspaces_connection(
    client, seed, session_factory
):
    a = make_workspace("Alpha")
    b = make_workspace("Bravo")
    user_a = make_user("iso-a@example.com")
    user_b = make_user("iso-b@example.com")
    conn_a = make_connection(a, account_label="alpha-account@example.com")
    await seed(
        a, b, user_a, user_b,
        make_member(user_a, a, role="admin"),
        make_member(user_b, b, role="admin"),
        conn_a,
    )
    await login(client, user_b.email)

    # B's list is empty — A's connection does not leak in.
    list_body = (await client.get("/api/connections")).json()
    assert list_body["connections"] == []

    # B's providers view reports gdrive as not connected — A's row must not
    # leak through the wrong workspace's connected flag.
    providers_body = (await client.get("/api/connections/providers")).json()
    by_provider = {p["provider"]: p for p in providers_body["providers"]}
    assert by_provider["gdrive"]["connected"] is False

    # B (an admin of B, not of A) disconnecting gdrive is a no-op against A's
    # row: A's connection must still exist afterwards.
    response = await client.delete("/api/connections/gdrive")
    assert response.status_code == 200
    assert response.json() == {"ok": True}

    async with session_factory() as db:
        rows = (
            await db.execute(
                select(WorkspaceConnection).where(WorkspaceConnection.workspace_id == a.id)
            )
        ).scalars().all()
    assert len(rows) == 1
    assert rows[0].id == conn_a.id


# ── GET /api/connections/{provider}/token: the "use" gate ────────────────────
async def test_token_is_blocked_by_a_registered_workspace_gate(client, seed):
    """A workspace whose plan lapsed after it connected gdrive must lose the
    picker token on the very next call — no admin required (any member may
    call token), and the refresh must never reach the provider: the gate is
    checked before `get_connection` even loads the row."""
    workspace = make_workspace("Alpha")
    member = make_user("token-gate-member@example.com")
    conn = make_connection(workspace)
    await seed(workspace, member, make_member(member, workspace, role="analyst"), conn)
    await login(client, member.email)

    ext = ExtensionAPI(None)

    async def veto(db, workspace_id, action):
        assert action == "connections.use"
        assert workspace_id == workspace.id
        return GateResult(
            allowed=False,
            reason="connections_plan_required",
            detail="Connections require an active plan.",
        )

    ext.add_workspace_gate(veto)
    extensions_module._registry = ext

    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(GDRIVE_TOKEN_URL).mock(return_value=httpx.Response(200))
        response = await client.get("/api/connections/gdrive/token")
        assert route.called is False  # gate refuses before any refresh is attempted

    assert response.status_code == 403
    assert response.json()["detail"]["reason"] == "connections_plan_required"


async def test_token_passes_the_use_action_when_the_gate_allows(client, seed):
    """A gate that allows the call gets asked `connections.use`, never
    `connections.connect` — token exercises an existing connection, it does
    not create one."""
    workspace = make_workspace("Alpha")
    member = make_user("token-gate-allow@example.com")
    conn = make_connection(workspace)
    await seed(workspace, member, make_member(member, workspace, role="analyst"), conn)
    await login(client, member.email)

    asked_actions: list[str] = []
    ext = ExtensionAPI(None)

    async def recorder(db, workspace_id, action):
        asked_actions.append(action)
        return GateResult(allowed=True)

    ext.add_workspace_gate(recorder)
    extensions_module._registry = ext

    with respx.mock(assert_all_called=True) as mock:
        mock.post(GDRIVE_TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        )
        response = await client.get("/api/connections/gdrive/token")

    assert response.status_code == 200, response.text
    assert asked_actions == ["connections.use"]


# ── GET /api/connections: now carries selected_resources ────────────────────
async def test_list_connections_includes_selected_resources(client, seed):
    workspace = make_workspace("Alpha")
    user = make_user("selected-resources-view@example.com")
    conn = make_connection(workspace)
    conn.selected_resources = {"read": [{"drive_id": "drive-1", "label": "Finance", "kind": "site_drive"}]}
    await seed(workspace, user, make_member(user, workspace, role="analyst"), conn)
    await login(client, user.email)

    body = (await client.get("/api/connections")).json()
    entry = body["connections"][0]
    assert entry["selected_resources"] == {
        "read": [{"drive_id": "drive-1", "label": "Finance", "kind": "site_drive"}]
    }


# ── GET /api/connections/m365/sources ────────────────────────────────────────
async def test_m365_sources_reports_restricted_true_for_an_explicit_allowlist(
    client, seed, _configured_m365_client
):
    workspace = make_workspace("Alpha")
    user = make_user("sources-restricted@example.com")
    conn = make_connection(workspace, provider="m365")
    conn.selected_resources = {
        "read": [{"drive_id": "drive-1", "label": "Finance", "kind": "site_drive", "site_id": "site-1"}]
    }
    await seed(workspace, user, make_member(user, workspace, role="analyst"), conn)
    await login(client, user.email)

    response = await client.get("/api/connections/m365/sources")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["restricted"] is True
    assert body["sources"] == [
        {
            "slug": body["sources"][0]["slug"],
            "provider": "m365",
            "kind": "site_drive",
            "label": "Finance",
            "site_id": "site-1",
            "drive_id": "drive-1",
            "web_url": None,
        }
    ]


async def test_m365_sources_derives_and_reports_unrestricted(client, seed, _configured_m365_client):
    workspace = make_workspace("Alpha")
    user = make_user("sources-derived@example.com")
    conn = make_connection(workspace, provider="m365")  # selected_resources default {}
    await seed(workspace, user, make_member(user, workspace, role="analyst"), conn)
    await login(client, user.email)

    with respx.mock(assert_all_called=True) as mock:
        mock_m365_refresh(mock)
        mock.get(f"{GRAPH}/sites", params={"search": "*"}).mock(
            return_value=httpx.Response(200, json={"value": []})
        )
        mock.get(f"{GRAPH}/me/drive").mock(
            return_value=httpx.Response(200, json={"id": "drive-me", "webUrl": None})
        )
        mock.get(f"{GRAPH}/me").mock(
            return_value=httpx.Response(200, json={"userPrincipalName": "person@tenant.onmicrosoft.com"})
        )
        response = await client.get("/api/connections/m365/sources")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["restricted"] is False
    assert len(body["sources"]) == 1
    assert body["sources"][0]["kind"] == "onedrive"
    assert body["sources"][0]["label"] == "OneDrive (person@tenant.onmicrosoft.com)"


async def test_m365_sources_with_no_connection_is_409(client, seed, _configured_m365_client):
    workspace = make_workspace("Alpha")
    user = make_user("sources-no-conn@example.com")
    await seed(workspace, user, make_member(user, workspace, role="analyst"))
    await login(client, user.email)

    response = await client.get("/api/connections/m365/sources")
    assert response.status_code == 409
    assert "no" in response.json()["detail"].lower()


async def test_m365_sources_requires_no_admin_role(client, seed, _configured_m365_client):
    """Any workspace member, same as `m365_browse` — reading the source list
    does not mutate the connection."""
    workspace = make_workspace("Alpha")
    member = make_user("sources-any-member@example.com", role="analyst")
    conn = make_connection(workspace, provider="m365")
    conn.selected_resources = {"read": [{"drive_id": "drive-1", "label": "Finance", "kind": "site_drive"}]}
    await seed(workspace, member, make_member(member, workspace, role="analyst"), conn)
    await login(client, member.email)

    response = await client.get("/api/connections/m365/sources")
    assert response.status_code == 200, response.text


# ── PUT /api/connections/m365/resources ──────────────────────────────────────
async def test_update_m365_resources_requires_admin(client, seed, _configured_m365_client):
    workspace = make_workspace("Alpha")
    analyst = make_user("resources-analyst@example.com", role="analyst")
    conn = make_connection(workspace, provider="m365")
    await seed(workspace, analyst, make_member(analyst, workspace, role="analyst"), conn)
    await login(client, analyst.email)

    response = await client.put(
        "/api/connections/m365/resources",
        json={"read": [{"drive_id": "drive-1", "label": "Finance", "kind": "site_drive"}]},
    )
    assert response.status_code == 403


async def test_update_m365_resources_sets_the_allowlist_and_persists_it(
    client, seed, session_factory, _configured_m365_client
):
    workspace = make_workspace("Alpha")
    admin = make_user("resources-admin@example.com")
    conn = make_connection(workspace, provider="m365")
    await seed(workspace, admin, make_member(admin, workspace, role="admin"), conn)
    await login(client, admin.email)

    response = await client.put(
        "/api/connections/m365/resources",
        json={
            "read": [
                {
                    "site_id": "site-1",
                    "drive_id": "drive-1",
                    "label": "Finance",
                    "kind": "site_drive",
                    "web_url": "https://contoso.sharepoint.com/sites/finance",
                }
            ]
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["selected_resources"]["read"][0]["drive_id"] == "drive-1"

    async with session_factory() as db:
        row = await db.get(WorkspaceConnection, conn.id)
        assert row.selected_resources["read"][0]["label"] == "Finance"

    # And GET /api/connections/m365/sources now reports the restricted view
    # without any Graph call — the whole point of setting an allowlist.
    with respx.mock(assert_all_called=False):
        response = await client.get("/api/connections/m365/sources")
    assert response.status_code == 200
    assert response.json()["restricted"] is True
    assert response.json()["sources"][0]["drive_id"] == "drive-1"


async def test_update_m365_resources_empty_list_clears_the_restriction(
    client, seed, session_factory, _configured_m365_client
):
    workspace = make_workspace("Alpha")
    admin = make_user("resources-clear-admin@example.com")
    conn = make_connection(workspace, provider="m365")
    conn.selected_resources = {"read": [{"drive_id": "drive-1", "label": "Finance", "kind": "site_drive"}]}
    await seed(workspace, admin, make_member(admin, workspace, role="admin"), conn)
    await login(client, admin.email)

    response = await client.put("/api/connections/m365/resources", json={"read": []})
    assert response.status_code == 200, response.text

    async with session_factory() as db:
        row = await db.get(WorkspaceConnection, conn.id)
        assert row.selected_resources["read"] == []

    with respx.mock(assert_all_called=True) as mock:
        mock_m365_refresh(mock)
        mock.get(f"{GRAPH}/sites", params={"search": "*"}).mock(
            return_value=httpx.Response(200, json={"value": []})
        )
        mock.get(f"{GRAPH}/me/drive").mock(return_value=httpx.Response(200, json={"id": "drive-me"}))
        mock.get(f"{GRAPH}/me").mock(return_value=httpx.Response(200, json={}))
        response = await client.get("/api/connections/m365/sources")
    assert response.json()["restricted"] is False  # fell back to derived


async def test_update_m365_resources_without_a_connection_is_404(client, seed, _configured_m365_client):
    workspace = make_workspace("Alpha")
    admin = make_user("resources-no-conn-admin@example.com")
    await seed(workspace, admin, make_member(admin, workspace, role="admin"))
    await login(client, admin.email)

    response = await client.put(
        "/api/connections/m365/resources",
        json={"read": [{"drive_id": "drive-1", "label": "Finance", "kind": "site_drive"}]},
    )
    assert response.status_code == 404


async def test_update_m365_resources_rejects_unknown_fields(client, seed, _configured_m365_client):
    workspace = make_workspace("Alpha")
    admin = make_user("resources-extra-admin@example.com")
    conn = make_connection(workspace, provider="m365")
    await seed(workspace, admin, make_member(admin, workspace, role="admin"), conn)
    await login(client, admin.email)

    response = await client.put(
        "/api/connections/m365/resources",
        json={"read": [{"drive_id": "drive-1", "label": "Finance", "kind": "site_drive", "unexpected": "x"}]},
    )
    assert response.status_code == 422


async def test_update_m365_resources_rejects_more_than_fifty_entries(client, seed, _configured_m365_client):
    workspace = make_workspace("Alpha")
    admin = make_user("resources-too-many-admin@example.com")
    conn = make_connection(workspace, provider="m365")
    await seed(workspace, admin, make_member(admin, workspace, role="admin"), conn)
    await login(client, admin.email)

    entries = [
        {"drive_id": f"drive-{i}", "label": f"Drive {i}", "kind": "site_drive"} for i in range(51)
    ]
    response = await client.put("/api/connections/m365/resources", json={"read": entries})
    assert response.status_code == 422


async def test_update_m365_resources_rejects_an_empty_drive_id(client, seed, _configured_m365_client):
    workspace = make_workspace("Alpha")
    admin = make_user("resources-empty-drive-admin@example.com")
    conn = make_connection(workspace, provider="m365")
    await seed(workspace, admin, make_member(admin, workspace, role="admin"), conn)
    await login(client, admin.email)

    response = await client.put(
        "/api/connections/m365/resources",
        json={"read": [{"drive_id": "", "label": "Finance", "kind": "site_drive"}]},
    )
    assert response.status_code == 422


async def test_update_m365_resources_invalidates_the_sources_cache(
    client, seed, _configured_m365_client
):
    """A cached derived sources list from before the allowlist was set must
    not survive the PUT — the very next `GET /m365/sources` reflects the new
    allowlist, not up to 300s of staleness."""
    workspace = make_workspace("Alpha")
    admin = make_user("resources-cache-admin@example.com")
    conn = make_connection(workspace, provider="m365")
    await seed(workspace, admin, make_member(admin, workspace, role="admin"), conn)
    await login(client, admin.email)

    with respx.mock(assert_all_called=True) as mock:
        mock_m365_refresh(mock)
        mock.get(f"{GRAPH}/sites", params={"search": "*"}).mock(
            return_value=httpx.Response(200, json={"value": []})
        )
        mock.get(f"{GRAPH}/me/drive").mock(return_value=httpx.Response(200, json={"id": "drive-me"}))
        mock.get(f"{GRAPH}/me").mock(return_value=httpx.Response(200, json={}))
        first = await client.get("/api/connections/m365/sources")
    assert first.json()["restricted"] is False

    put_response = await client.put(
        "/api/connections/m365/resources",
        json={"read": [{"drive_id": "drive-1", "label": "Finance", "kind": "site_drive"}]},
    )
    assert put_response.status_code == 200

    with respx.mock(assert_all_called=False):
        # No Graph route registered: the restricted branch must not call
        # Graph at all, and the stale derived-list cache entry must not be
        # served instead.
        second = await client.get("/api/connections/m365/sources")
    assert second.json()["restricted"] is True
    assert second.json()["sources"][0]["drive_id"] == "drive-1"


# ── workspace isolation: m365 sources/resources ──────────────────────────────
async def test_m365_sources_and_resources_are_isolated_per_workspace(
    client, seed, session_factory, _configured_m365_client
):
    a = make_workspace("Alpha")
    b = make_workspace("Bravo")
    admin_a = make_user("iso-sources-a@example.com")
    admin_b = make_user("iso-sources-b@example.com")
    conn_a = make_connection(a, provider="m365")
    conn_a.selected_resources = {"read": [{"drive_id": "drive-a", "label": "A", "kind": "site_drive"}]}
    conn_b = make_connection(b, provider="m365")
    await seed(
        a, b, admin_a, admin_b,
        make_member(admin_a, a, role="admin"),
        make_member(admin_b, b, role="admin"),
        conn_a, conn_b,
    )
    await login(client, admin_b.email)

    # B's admin narrowing B's own allowlist must never touch A's row.
    response = await client.put(
        "/api/connections/m365/resources",
        json={"read": [{"drive_id": "drive-b", "label": "B", "kind": "site_drive"}]},
    )
    assert response.status_code == 200, response.text

    async with session_factory() as db:
        row_a = await db.get(WorkspaceConnection, conn_a.id)
        row_b = await db.get(WorkspaceConnection, conn_b.id)
    assert row_a.selected_resources["read"][0]["drive_id"] == "drive-a"  # untouched
    assert row_b.selected_resources["read"][0]["drive_id"] == "drive-b"

    # B's sources view never sees A's allowlist.
    sources_response = await client.get("/api/connections/m365/sources")
    assert sources_response.json()["sources"][0]["drive_id"] == "drive-b"
