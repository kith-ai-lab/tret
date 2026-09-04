"""Core service layer behind workspace connections (tret/services/connections.py),
independent of the router: OAuth client credential resolution (env vs. the
extension seam), the Fernet envelope it borrows from services/credentials.py
verbatim, and the refresh-token lifecycle (`get_access_token`) — including
Microsoft's refresh-token rotation and `invalid_grant` flipping a connection
to `status='error'`.

Real sqlite database, same harness as test_workspace_service.py /
test_provider_key_tenancy.py: the query shape under test (one row per
(workspace, provider), read back after a commit made through a *different*
session — the same cross-request pattern the real router uses) is exactly
what a hand-rolled fake session gets subtly wrong.

Provider HTTP is mocked with respx (test_oidc_login.py's own tool for the
same job) — nothing here reaches a real network; tests/test_egress_chokepoint.py
would fail the suite if services/connections.py ever used bare httpx.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import uuid

import httpx
import pytest
import pytest_asyncio
import respx
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from tests.evals.golden_world import install_sqlite_type_shims
from tret.config import get_settings
from tret.db.models import Base, Workspace, WorkspaceConnection
from tret.engine import extensions as extensions_module
from tret.engine.extensions import ExtensionAPI
from tret.services import connections as connections_module
from tret.services import credentials as credentials_module
from tret.services import documents as documents_service
from tret.services.connections import (
    GRAPH_API_BASE,
    IMPORT_MAX_BYTES,
    ConnectionAuthError,
    OAuthClientConfig,
    browse_m365,
    clear_access_token_cache,
    exchange_code,
    get_access_token,
    get_access_token_with_expiry,
    get_connection,
    get_oauth_client,
    invalidate_access_token,
    m365_item_metadata,
)
from tret.services.credentials import get_fernet

GDRIVE_TOKEN_URL = "https://oauth2.googleapis.com/token"
M365_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
GRAPH_ME_URL = "https://graph.microsoft.com/v1.0/me"


# ── fixtures ─────────────────────────────────────────────────────────────────
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


@pytest_asyncio.fixture(autouse=True)
def _clean_settings_and_registry(monkeypatch):
    """Every test starts with a clean Settings cache and no extension
    registry, same as test_extensions.py's own autouse fixture. Also clears
    the module-level access-token cache: without this, a token cached by one
    test (same provider, and — unlikely but not impossible with random
    UUIDs — colliding workspace ids) could be served to a later one that
    never refreshed anything."""
    extensions_module._registry = None
    get_settings.cache_clear()
    clear_access_token_cache()
    yield
    extensions_module._registry = None
    get_settings.cache_clear()
    clear_access_token_cache()


@pytest.fixture
def configured_clients(monkeypatch):
    """Both providers' OAuth client env vars set — the state every
    `get_access_token`/`exchange_code` test needs so `get_oauth_client`
    resolves to something before the (respx-mocked) HTTP call it guards."""
    monkeypatch.setenv("TRET_GDRIVE_CLIENT_ID", "gdrive-cid")
    monkeypatch.setenv("TRET_GDRIVE_CLIENT_SECRET", "gdrive-secret")
    monkeypatch.setenv("TRET_M365_CLIENT_ID", "m365-cid")
    monkeypatch.setenv("TRET_M365_CLIENT_SECRET", "m365-secret")
    get_settings.cache_clear()


def _b64(obj: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode("ascii")


def _unsigned_id_token(**claims) -> str:
    """A `services/connections.py::_decode_gdrive_email`-shaped id_token: no
    signature verification happens on the connections path (see that
    function's docstring — the token just came back from Google's own token
    endpoint over TLS), so an unsigned three-segment JWT is exactly what it
    is built to read."""
    header = _b64({"alg": "none", "typ": "JWT"})
    body = _b64(claims)
    return f"{header}.{body}."


def make_workspace(name: str = "Alpha") -> Workspace:
    return Workspace(id=uuid.uuid4(), name=name, kind="team")


def make_connection(
    workspace: Workspace,
    *,
    provider: str,
    refresh_token: str = "initial-refresh-token",
    status: str = "active",
) -> WorkspaceConnection:
    return WorkspaceConnection(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        provider=provider,
        account_label="person@example.com",
        encrypted_refresh_token=get_fernet().encrypt(refresh_token.encode()),
        granted_scopes=["openid", "email"],
        status=status,
    )


def _encrypt_with_a_different_key(refresh_token: str) -> bytes:
    """A refresh token Fernet-sealed under a key that is not the one
    `get_fernet()` currently derives — simulates a row that survived a
    `TRET_SECRET_KEY` rotation. `get_fernet().decrypt` on this raises
    `cryptography.fernet.InvalidToken`."""
    digest = hashlib.sha256(b"a-different-secret-key-entirely").digest()
    return Fernet(base64.urlsafe_b64encode(digest)).encrypt(refresh_token.encode())


# ── Fernet envelope: reused, not re-derived ──────────────────────────────────
def test_connections_reuses_credentials_get_fernet_verbatim():
    """The exact function object, not a look-alike with its own key
    derivation — the property the contract asks for ("do not duplicate the
    key derivation")."""
    assert connections_module.get_fernet is credentials_module.get_fernet


def test_encryption_round_trip():
    f = get_fernet()
    token = f.encrypt(b"a-refresh-token-value")
    assert f.decrypt(token) == b"a-refresh-token-value"
    assert token != b"a-refresh-token-value"  # actually sealed, not a no-op


# ── OAuth client credentials: env first, then the extension seam ────────────
def test_get_oauth_client_reads_env_vars(monkeypatch):
    monkeypatch.setenv("TRET_GDRIVE_CLIENT_ID", "env-client-id")
    monkeypatch.setenv("TRET_GDRIVE_CLIENT_SECRET", "env-client-secret")
    get_settings.cache_clear()

    client = get_oauth_client("gdrive")
    assert client == OAuthClientConfig(client_id="env-client-id", client_secret="env-client-secret")


def test_get_oauth_client_is_none_when_unconfigured():
    assert get_oauth_client("gdrive") is None
    assert get_oauth_client("m365") is None


def test_get_oauth_client_falls_back_to_the_extension_registry():
    ext = ExtensionAPI(None)

    def provide(provider: str):
        if provider == "m365":
            return OAuthClientConfig(client_id="ext-id", client_secret="ext-secret")
        return None

    ext.add_oauth_client_provider(provide)
    extensions_module._registry = ext

    assert get_oauth_client("m365") == OAuthClientConfig(client_id="ext-id", client_secret="ext-secret")
    assert get_oauth_client("gdrive") is None  # provide() named m365 only


def test_env_vars_win_over_the_extension_registry(monkeypatch):
    monkeypatch.setenv("TRET_GDRIVE_CLIENT_ID", "env-id")
    monkeypatch.setenv("TRET_GDRIVE_CLIENT_SECRET", "env-secret")
    get_settings.cache_clear()

    ext = ExtensionAPI(None)
    calls = []

    def provide(provider: str):
        calls.append(provider)
        return OAuthClientConfig(client_id="ext-id", client_secret="ext-secret")

    ext.add_oauth_client_provider(provide)
    extensions_module._registry = ext

    client = get_oauth_client("gdrive")
    assert client == OAuthClientConfig(client_id="env-id", client_secret="env-secret")
    assert calls == []  # never even asked — env already answered it


# ── exchange_code: gdrive email-from-id_token, m365 UPN-from-Graph ──────────
async def test_exchange_code_gdrive_decodes_email_from_id_token():
    id_token = _unsigned_id_token(email="drive-person@example.com", sub="123")
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
        result = await exchange_code(
            "gdrive",
            client=OAuthClientConfig(client_id="cid", client_secret="csecret"),
            code="auth-code",
            redirect_uri="https://tret.example.test/api/connections/callback",
        )
    assert result.refresh_token == "gdrive-refresh-token"
    assert result.account_label == "drive-person@example.com"
    assert result.granted_scopes == [
        "openid", "email", "https://www.googleapis.com/auth/drive.file",
    ]


async def test_exchange_code_m365_fetches_upn_from_graph():
    with respx.mock(assert_all_called=True) as mock:
        mock.post(M365_TOKEN_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "m365-access-token",
                    "refresh_token": "m365-refresh-token",
                    "scope": "offline_access User.Read Files.Read.All Sites.Read.All",
                },
            )
        )
        mock.get(GRAPH_ME_URL).mock(
            return_value=httpx.Response(200, json={"userPrincipalName": "person@tenant.onmicrosoft.com"})
        )
        result = await exchange_code(
            "m365",
            client=OAuthClientConfig(client_id="cid", client_secret="csecret"),
            code="auth-code",
            redirect_uri="https://tret.example.test/api/connections/callback",
        )
    assert result.refresh_token == "m365-refresh-token"
    assert result.account_label == "person@tenant.onmicrosoft.com"


async def test_exchange_code_without_a_refresh_token_raises():
    with respx.mock(assert_all_called=True) as mock:
        mock.post(GDRIVE_TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": "only-an-access-token"})
        )
        try:
            await exchange_code(
                "gdrive",
                client=OAuthClientConfig(client_id="cid", client_secret="csecret"),
                code="auth-code",
                redirect_uri="https://tret.example.test/api/connections/callback",
            )
            assert False, "expected ConnectionAuthError"
        except ConnectionAuthError:
            pass


# ── get_access_token: refresh, rotation, invalid_grant ───────────────────────
async def test_get_access_token_happy_path(configured_clients, session_factory, seed):
    workspace = make_workspace()
    conn = make_connection(workspace, provider="gdrive", refresh_token="stored-refresh-token")
    await seed(workspace, conn)

    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(GDRIVE_TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": "fresh-access-token", "expires_in": 3600})
        )
        async with session_factory() as db:
            token = await get_access_token(db, workspace.id, "gdrive")
    assert token == "fresh-access-token"
    body = route.calls.last.request.read().decode()
    assert "grant_type=refresh_token" in body
    assert "refresh_token=stored-refresh-token" in body

    # refreshed_at was stamped and committed by a *different* session.
    async with session_factory() as db:
        reloaded = await get_connection(db, workspace.id, "gdrive")
        assert reloaded.refreshed_at is not None
        assert reloaded.status == "active"


async def test_get_access_token_persists_microsofts_rotated_refresh_token(
    configured_clients, session_factory, seed
):
    workspace = make_workspace()
    conn = make_connection(workspace, provider="m365", refresh_token="old-refresh-token")
    await seed(workspace, conn)

    with respx.mock(assert_all_called=True) as mock:
        mock.post(M365_TOKEN_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "fresh-access-token",
                    "expires_in": 3600,
                    "refresh_token": "rotated-refresh-token",
                },
            )
        )
        async with session_factory() as db:
            token = await get_access_token(db, workspace.id, "m365")
    assert token == "fresh-access-token"

    async with session_factory() as db:
        reloaded = await get_connection(db, workspace.id, "m365")
        decrypted = get_fernet().decrypt(reloaded.encrypted_refresh_token).decode()
        assert decrypted == "rotated-refresh-token"  # old one is gone


async def test_get_access_token_with_expiry_returns_the_providers_expires_in(
    configured_clients, session_factory, seed
):
    workspace = make_workspace()
    conn = make_connection(workspace, provider="gdrive")
    await seed(workspace, conn)

    with respx.mock(assert_all_called=True) as mock:
        mock.post(GDRIVE_TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 1800})
        )
        async with session_factory() as db:
            token, expires_in = await get_access_token_with_expiry(db, workspace.id, "gdrive")
    assert token == "tok"
    assert expires_in == 1800


async def test_invalid_grant_flips_status_to_error_and_raises(configured_clients, session_factory, seed):
    workspace = make_workspace()
    conn = make_connection(workspace, provider="gdrive", refresh_token="revoked-refresh-token")
    await seed(workspace, conn)

    with respx.mock(assert_all_called=True) as mock:
        mock.post(GDRIVE_TOKEN_URL).mock(
            return_value=httpx.Response(
                400, json={"error": "invalid_grant", "error_description": "Token has been expired or revoked."}
            )
        )
        async with session_factory() as db:
            try:
                await get_access_token(db, workspace.id, "gdrive")
                assert False, "expected ConnectionAuthError"
            except ConnectionAuthError as exc:
                assert exc.error_code == "invalid_grant"

    # Committed by that call, visible from a fresh session — not just mutated
    # in-memory on an object nobody flushed.
    async with session_factory() as db:
        reloaded = await get_connection(db, workspace.id, "gdrive")
        assert reloaded.status == "error"
        assert reloaded.error_detail and "invalid_grant" in reloaded.error_detail


async def test_undecryptable_refresh_token_flips_status_to_error_and_raises(
    configured_clients, session_factory, seed
):
    """A stored refresh token that no longer decrypts under the current
    TRET_SECRET_KEY (most commonly: the key was rotated since it was
    written) must behave exactly like `invalid_grant` — flip the connection
    to status='error' with a non-empty error_detail and raise
    ConnectionAuthError — rather than let `InvalidToken` propagate raw and
    turn every browse/import/token call into an unhandled 500."""
    workspace = make_workspace()
    conn = make_connection(workspace, provider="m365")
    conn.encrypted_refresh_token = _encrypt_with_a_different_key("stored-refresh-token")
    await seed(workspace, conn)

    async with session_factory() as db:
        try:
            await browse_m365(db, workspace.id)
            assert False, "expected ConnectionAuthError"
        except ConnectionAuthError as exc:
            assert "TRET_SECRET_KEY" in str(exc)

    # Committed by that call, visible from a fresh session.
    async with session_factory() as db:
        reloaded = await get_connection(db, workspace.id, "m365")
        assert reloaded.status == "error"
        assert reloaded.error_detail


async def test_a_non_invalid_grant_failure_does_not_flip_status(configured_clients, session_factory, seed):
    """Only invalid_grant freezes the connection — a transient 5xx from the
    provider must not make an otherwise-healthy connection look broken."""
    workspace = make_workspace()
    conn = make_connection(workspace, provider="gdrive")
    await seed(workspace, conn)

    with respx.mock(assert_all_called=True) as mock:
        mock.post(GDRIVE_TOKEN_URL).mock(return_value=httpx.Response(503, json={"error": "server_error"}))
        async with session_factory() as db:
            try:
                await get_access_token(db, workspace.id, "gdrive")
                assert False, "expected ConnectionAuthError"
            except ConnectionAuthError:
                pass

    async with session_factory() as db:
        reloaded = await get_connection(db, workspace.id, "gdrive")
        assert reloaded.status == "active"  # untouched


async def test_get_access_token_with_no_connection_raises(session_factory, seed):
    workspace = make_workspace()
    await seed(workspace)
    async with session_factory() as db:
        try:
            await get_access_token(db, workspace.id, "gdrive")
            assert False, "expected ConnectionAuthError"
        except ConnectionAuthError:
            pass


# ── caller-supplied ids are percent-escaped before hitting Graph/Drive ──────
async def test_m365_item_metadata_percent_escapes_traversal_in_ids():
    """`drive_id`/`item_id` are query params on `GET /api/connections/m365/
    browse` — attacker-controlled strings that land straight in a Graph URL
    *path* segment. `quote(..., safe="")` must turn every `/` into `%2F` so
    a value like `../evil-drive` stays inert text inside its own segment
    rather than resolving `..` up a level of the URL path Graph receives.
    """
    escaped_url = f"{GRAPH_API_BASE}/drives/..%2Fevil-drive/items/also%2Fbad-item"
    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(escaped_url).mock(return_value=httpx.Response(200, json={"id": "x"}))
        await m365_item_metadata(
            "access-token", drive_id="../evil-drive", item_id="also/bad-item"
        )
    requested_url = str(route.calls.last.request.url)
    assert "..%2Fevil-drive" in requested_url
    assert "also%2Fbad-item" in requested_url
    # The literal, unescaped traversal shape never reaches the request line.
    assert "/../" not in requested_url
    assert "/drives/../" not in requested_url


async def test_get_access_token_with_no_oauth_client_configured_raises(session_factory, seed):
    """A connection can exist (it was made while a client was configured)
    even after an operator unsets the env vars — the next refresh must fail
    cleanly rather than crash on a None client."""
    workspace = make_workspace()
    conn = make_connection(workspace, provider="gdrive")
    await seed(workspace, conn)
    async with session_factory() as db:
        try:
            await get_access_token(db, workspace.id, "gdrive")
            assert False, "expected ConnectionAuthError"
        except ConnectionAuthError:
            pass


# ── errored connections are refused without contacting the provider ─────────
async def test_get_access_token_on_an_errored_connection_raises_without_calling_the_provider(
    configured_clients, session_factory, seed
):
    """Once a connection is in `status='error'`, every subsequent call must
    refuse it before ever reaching the provider's token endpoint — not just
    fail *after* a wasted (and, for a revoked grant, guaranteed-to-fail)
    refresh attempt."""
    workspace = make_workspace()
    conn = make_connection(workspace, provider="gdrive", status="error")
    conn.error_detail = "the gdrive connection is in an error state and must be reconnected"
    await seed(workspace, conn)

    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(GDRIVE_TOKEN_URL).mock(return_value=httpx.Response(200))
        async with session_factory() as db:
            try:
                await get_access_token(db, workspace.id, "gdrive")
                assert False, "expected ConnectionAuthError"
            except ConnectionAuthError as exc:
                assert "reconnect" in str(exc).lower()
        assert route.call_count == 0


# ── one size cap shared by upload and import ─────────────────────────────────
def test_import_max_bytes_matches_the_shared_document_cap():
    """`IMPORT_MAX_BYTES` (services/connections.py), `MAX_UPLOAD_BYTES`
    (api/documents.py) and `MAX_DOCUMENT_BYTES` (services/documents.py,
    the source of truth) must never drift apart — both consumers are meant
    to be aliases of the one number."""
    from tret.api.documents import MAX_UPLOAD_BYTES

    assert IMPORT_MAX_BYTES == MAX_UPLOAD_BYTES == documents_service.MAX_DOCUMENT_BYTES


# ── access-token cache ───────────────────────────────────────────────────────
async def test_second_call_within_expiry_is_served_from_cache(configured_clients, session_factory, seed):
    workspace = make_workspace()
    conn = make_connection(workspace, provider="gdrive")
    await seed(workspace, conn)

    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(GDRIVE_TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": "cached-token", "expires_in": 3600})
        )
        async with session_factory() as db:
            first = await get_access_token(db, workspace.id, "gdrive")
        async with session_factory() as db:
            second = await get_access_token(db, workspace.id, "gdrive")
    assert first == "cached-token"
    assert second == "cached-token"
    assert route.call_count == 1


async def test_expiry_at_or_below_the_safety_margin_is_never_cached(configured_clients, session_factory, seed):
    """A token whose own `expires_in` doesn't clear the safety margin isn't
    worth caching at all — every call must refresh, same as before the cache
    existed."""
    workspace = make_workspace()
    conn = make_connection(workspace, provider="gdrive")
    await seed(workspace, conn)

    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(GDRIVE_TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": "short-lived-token", "expires_in": 30})
        )
        async with session_factory() as db:
            await get_access_token(db, workspace.id, "gdrive")
        async with session_factory() as db:
            await get_access_token(db, workspace.id, "gdrive")
    assert route.call_count == 2


async def test_concurrent_calls_for_the_same_connection_perform_one_refresh(
    configured_clients, session_factory, seed, monkeypatch
):
    """Two callers asking for the same (workspace, provider) token at the
    same time must serialize on one refresh, not each start their own — the
    concurrent-refresh race this cache exists to close."""
    workspace = make_workspace()
    conn = make_connection(workspace, provider="m365")
    await seed(workspace, conn)

    calls = 0

    async def slow_token_request(token_url, data):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return {"access_token": "concurrent-token", "expires_in": 3600}

    monkeypatch.setattr(connections_module, "_token_request", slow_token_request)

    async def _one_call():
        async with session_factory() as db:
            return await get_access_token(db, workspace.id, "m365")

    results = await asyncio.gather(_one_call(), _one_call())

    assert results == ["concurrent-token", "concurrent-token"]
    assert calls == 1


async def test_invalidate_access_token_forces_the_next_call_to_refresh(
    configured_clients, session_factory, seed
):
    workspace = make_workspace()
    conn = make_connection(workspace, provider="gdrive")
    await seed(workspace, conn)

    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(GDRIVE_TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
        )
        async with session_factory() as db:
            await get_access_token(db, workspace.id, "gdrive")
        assert route.call_count == 1

        invalidate_access_token(workspace.id, "gdrive")

        async with session_factory() as db:
            await get_access_token(db, workspace.id, "gdrive")
        assert route.call_count == 2


async def test_invalid_grant_clears_any_cached_access_token(configured_clients, session_factory, seed):
    """`_refresh` flipping a connection to `status='error'` must not leave a
    stale cache entry behind for it — otherwise a *different* code path that
    only reads the cache (there isn't one today, but the invariant is what
    makes the cache safe at all) could still hand out a token for a
    connection that just proved its refresh token no longer works."""
    workspace = make_workspace()
    conn = make_connection(workspace, provider="gdrive", refresh_token="revoked-refresh-token")
    await seed(workspace, conn)

    # Seed the cache directly with an *expired* entry — expired so the miss
    # path actually runs `_refresh` (a still-valid entry would just be
    # served, never reaching the invalid_grant branch at all) while still
    # proving `invalidate_access_token` clears an entry that was genuinely
    # sitting in the dict, not just that the dict happened to be empty.
    key = (workspace.id, "gdrive")
    connections_module._access_token_cache[key] = connections_module._CachedAccessToken(
        access_token="stale-token", expires_at=connections_module.time.monotonic() - 1
    )

    with respx.mock(assert_all_called=True) as mock:
        mock.post(GDRIVE_TOKEN_URL).mock(
            return_value=httpx.Response(
                400, json={"error": "invalid_grant", "error_description": "revoked"}
            )
        )
        async with session_factory() as db:
            try:
                await get_access_token(db, workspace.id, "gdrive")
                assert False, "expected ConnectionAuthError"
            except ConnectionAuthError:
                pass

    assert key not in connections_module._access_token_cache


async def test_invalidation_landing_during_an_in_flight_refresh_is_not_lost_to_a_stale_write(
    configured_clients, session_factory, seed, monkeypatch
):
    """Caller A misses the cache and blocks inside `_token_request`; while it
    is blocked, a reconnect (or disconnect) lands and calls
    `invalidate_access_token` against a cache that — because A hasn't
    written to it yet — is still empty for this key, so the pop is a no-op.
    A's refresh then returns and must not have its result cached anyway: the
    epoch bump `invalidate_access_token` makes even on a miss is what closes
    this, since a plain pop cannot protect an entry that doesn't exist yet.

    The caller whose refresh raced the invalidation still gets its own
    freshly-minted token back — only the *cache write* is skipped. A second
    call afterward must therefore perform another real refresh rather than
    serving what would otherwise have been served as a stale hit for up to
    the cache's full window.
    """
    workspace = make_workspace()
    conn = make_connection(workspace, provider="gdrive")
    await seed(workspace, conn)

    provider = "gdrive"
    calls = 0

    async def racing_token_request(token_url, data):
        nonlocal calls
        calls += 1
        if calls == 1:
            # Simulates the OAuth callback (or disconnect) landing while
            # this refresh is still in flight.
            connections_module.invalidate_access_token(workspace.id, provider)
            return {"access_token": "OLD", "expires_in": 3600}
        return {"access_token": "NEW", "expires_in": 3600}

    monkeypatch.setattr(connections_module, "_token_request", racing_token_request)

    async with session_factory() as db:
        first = await get_access_token(db, workspace.id, provider)
    assert first == "OLD"
    assert (workspace.id, provider) not in connections_module._access_token_cache

    async with session_factory() as db:
        second = await get_access_token(db, workspace.id, provider)
    assert second == "NEW"
    assert calls == 2


async def test_get_access_token_with_expiry_on_a_cache_hit_returns_remaining_seconds(
    configured_clients, session_factory, seed
):
    workspace = make_workspace()
    conn = make_connection(workspace, provider="gdrive")
    await seed(workspace, conn)

    with respx.mock(assert_all_called=True) as mock:
        mock.post(GDRIVE_TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 1800})
        )
        async with session_factory() as db:
            _token, first_expiry = await get_access_token_with_expiry(db, workspace.id, "gdrive")
        async with session_factory() as db:
            _token, second_expiry = await get_access_token_with_expiry(db, workspace.id, "gdrive")
    assert first_expiry == 1800  # the miss: the provider's own expires_in, verbatim
    # the hit: expires_in (1800) less the 60s safety margin, not the original
    # 1800 — a range this tight also catches the hit path wrongly returning
    # the uncached expires_in verbatim.
    assert 1700 < second_expiry <= 1740
