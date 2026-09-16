"""OIDC bearer-token authentication (tret/api/oidc_bearer.py), wired into
`api/auth.py::current_user`/`require_admin` and `api/workspace.py::
current_workspace`.

Same faking strategy as tests/test_oidc_login.py: respx mocks discovery and
JWKS, access tokens are signed locally against an RSA key exposed through
that mocked JWKS, and the database is a small in-memory fake — nothing here
reaches a real network (tests/test_egress_chokepoint.py would fail the suite
if `oidc_bearer.py` ever tried to open one itself; it doesn't, it goes
through `api/oidc.py`'s own discovery/JWKS helpers).

`/api/auth/me` (authentication), `/api/auth/users` (`require_admin`) and a
tiny test-only route built on `current_workspace` are the three real
dependencies this exercises — no bearer-specific endpoint exists, on
purpose: the whole point is that every existing route keeps working
unmodified once a bearer caller reaches it.
"""
from __future__ import annotations

import base64
import json
import time
import uuid
from datetime import datetime, timezone

import httpx
import pytest
import respx
from argon2 import PasswordHasher
from authlib.jose import JsonWebKey
from authlib.jose import jwt as jose_jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.sql.elements import False_, Null, True_

from tret.api import auth, oidc
from tret.api.auth import login_limiter
from tret.api.workspace import WorkspaceContext, current_workspace
from tret.config import get_settings
from tret.db.engine import get_db
from tret.db.models import User, Workspace, WorkspaceMember

# ── fixed test IdP ───────────────────────────────────────────────────────────
ISSUER_HOST = "bearer-issuer.example.test"
ISSUER_URL = f"https://{ISSUER_HOST}"
API_AUDIENCE = "https://api.tret.example.test"
KID = "bearer-test-kid"
ROLES_CLAIM = "https://tret.example.test/roles"
ADMIN_ROLE = "tret-instance-admin"

_HASHER = PasswordHasher()
_RSA_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_JWKS = {
    "keys": [
        JsonWebKey.import_key(
            _RSA_KEY.public_key(), {"kty": "RSA", "kid": KID, "use": "sig", "alg": "RS256"}
        ).as_dict()
    ]
}


def _discovery_doc(**overrides) -> dict:
    doc = {
        "issuer": f"{ISSUER_URL}/",
        "authorization_endpoint": f"{ISSUER_URL}/authorize",
        "token_endpoint": f"{ISSUER_URL}/oauth/token",
        "jwks_uri": f"{ISSUER_URL}/.well-known/jwks.json",
    }
    doc.update(overrides)
    return doc


def _sign_access_token(**claims) -> str:
    now = int(time.time())
    payload = {
        "iss": f"{ISSUER_URL}/",
        "aud": API_AUDIENCE,
        "sub": "sub-1",
        "iat": now,
        "exp": now + 300,
    }
    payload.update(claims)
    token: bytes = jose_jwt.encode({"alg": "RS256", "kid": KID}, payload, _RSA_KEY)
    return token.decode("ascii")


def _sign_alg_none_access_token(**claims) -> str:
    """A hand-built, entirely unsigned JWT — see test_oidc_login.py's sibling
    helper for why this must be crafted by hand rather than produced by
    authlib (which never registers "none" in the first place)."""
    now = int(time.time())
    payload = {
        "iss": f"{ISSUER_URL}/",
        "aud": API_AUDIENCE,
        "sub": "sub-1",
        "iat": now,
        "exp": now + 300,
    }
    payload.update(claims)

    def _b64(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode("ascii")

    return f"{_b64({'alg': 'none', 'typ': 'JWT'})}.{_b64(payload)}."


def _sign_hs256_access_token(**claims) -> str:
    """A validly-*signed* (HS256) access token — the algorithm-confusion
    repro: this callback's key material is a JWKS (asymmetric public keys
    only), so an HS256-headed token must be rejected cleanly rather than
    crash authlib's own key-coercion code. See test_oidc_login.py's sibling
    helper — the same forgery, verified through the bearer path instead of
    the id_token callback."""
    now = int(time.time())
    payload = {
        "iss": f"{ISSUER_URL}/",
        "aud": API_AUDIENCE,
        "sub": "sub-1",
        "iat": now,
        "exp": now + 300,
    }
    payload.update(claims)
    token: bytes = jose_jwt.encode({"alg": "HS256"}, payload, "attacker-controlled-hs256-secret")
    return token.decode("ascii")


def _bearer_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _with_idp_mock(fn, *, discovery=None):
    """Run `fn()` with discovery + JWKS mocked. `assert_all_called=False`
    matches test_oidc_login.py's own helper: discovery/JWKS are cached after
    the first fetch within a test module run, so a later call in the same
    test may legitimately never re-hit either route."""
    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.get(f"{ISSUER_URL}/.well-known/openid-configuration").mock(
            return_value=httpx.Response(200, json=discovery or _discovery_doc())
        )
        respx_mock.get(f"{ISSUER_URL}/.well-known/jwks.json").mock(
            return_value=httpx.Response(200, json=_JWKS)
        )
        return fn()


# ── fake database (see test_oidc_login.py's FakeSession — same shape) ───────
def _matches(row, stmt) -> bool:
    where = stmt.whereclause
    if where is None:
        return True
    for clause in getattr(where, "clauses", [where]):
        left, right = getattr(clause, "left", None), getattr(clause, "right", None)
        if left is None:
            continue
        actual = getattr(row, left.key, None)
        if isinstance(right, True_):
            if actual is not True:
                return False
        elif isinstance(right, False_):
            if actual is not False:
                return False
        elif isinstance(right, Null):
            if actual is not None:
                return False
        elif getattr(right, "value", None) is not None:
            if actual != right.value:
                return False
    return True


class FakeResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class FakeSession:
    def __init__(self, *, users=(), workspaces=(), members=()):
        self.store: dict[str, list] = {
            "User": list(users),
            "Workspace": list(workspaces),
            "WorkspaceMember": list(members),
        }
        self.commits = 0

    async def get(self, model, ident, **_kw):
        rows = self.store.get(model.__name__, [])
        if model.__name__ == "WorkspaceMember":
            user_id, workspace_id = ident
            return next(
                (m for m in rows if m.user_id == user_id and m.workspace_id == workspace_id), None
            )
        return next((r for r in rows if r.id == ident), None)

    async def execute(self, stmt):
        entity = stmt.column_descriptions[0]["entity"]
        entity_name = entity.__name__ if entity is not None else "User"
        rows = [r for r in self.store.get(entity_name, []) if _matches(r, stmt)]
        return FakeResult(rows)

    def add(self, obj):
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()
        self.store.setdefault(type(obj).__name__, []).append(obj)

    async def commit(self):
        self.commits += 1


# ── fixture builders ─────────────────────────────────────────────────────────
def _user(*, email: str, sub: str, role: str = "analyst", disabled: bool = False) -> User:
    return User(
        id=uuid.uuid4(),
        email=email,
        display_name=email.split("@")[0].title(),
        password_hash=None,
        role=role,
        oidc_sub=sub,
        session_epoch=0,
        disabled=disabled,
        created_at=datetime.now(timezone.utc),
    )


def _workspace(name: str, *, kind: str = "team") -> Workspace:
    return Workspace(id=uuid.uuid4(), name=name, kind=kind, created_at=datetime.now(timezone.utc))


# ── fixtures ─────────────────────────────────────────────────────────────────
@pytest.fixture
def db() -> FakeSession:
    return FakeSession()


@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setenv("TRET_OIDC_ISSUER", ISSUER_HOST)
    # Every test opts into bearer auth explicitly (TRET_OIDC_API_AUDIENCE),
    # except the ones checking that leaving it unset is the entire switch.
    monkeypatch.delenv("TRET_OIDC_API_AUDIENCE", raising=False)
    monkeypatch.delenv("TRET_OIDC_ROLES_CLAIM", raising=False)
    monkeypatch.delenv("TRET_OIDC_ADMIN_ROLE", raising=False)
    monkeypatch.delenv("TRET_OIDC_BEARER_CLIENT_IDS", raising=False)
    get_settings.cache_clear()
    oidc._discovery_cache.clear()
    oidc._jwks_cache.clear()
    login_limiter.reset()

    app = FastAPI()
    app.include_router(auth.router)

    # No production route exercises `current_workspace` with nothing else in
    # front of it — every real one also requires a workspace role. This
    # test-only route isolates that one dependency so the X-Tret-Workspace
    # tests below aren't also exercising unrelated authorization logic.
    @app.get("/api/_test/workspace")
    async def _workspace_probe(ctx: WorkspaceContext = Depends(current_workspace)):
        return {"workspace_id": str(ctx.id), "role": ctx.role}

    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()
    oidc._discovery_cache.clear()
    oidc._jwks_cache.clear()
    login_limiter.reset()


# ── disabled by default ───────────────────────────────────────────────────────
def test_bearer_disabled_by_default_ignores_the_header(client, db):
    """No TRET_OIDC_API_AUDIENCE (the `client` fixture's default): a bearer
    header must be ignored outright — no network call, no lookup, just the
    same 401 a request with no credentials at all gets."""
    db.add(_user(email="alice@example.com", sub="sub-alice"))
    token = _sign_access_token(sub="sub-alice")
    response = client.get("/api/auth/me", headers=_bearer_headers(token))
    assert response.status_code == 401
    assert response.json()["detail"] == "Not authenticated"


# ── valid token ────────────────────────────────────────────────────────────────
def test_a_valid_bearer_token_authenticates_and_resolves_the_user(client, db, monkeypatch):
    monkeypatch.setenv("TRET_OIDC_API_AUDIENCE", API_AUDIENCE)
    get_settings.cache_clear()
    db.add(_user(email="alice@example.com", sub="sub-alice"))
    token = _sign_access_token(sub="sub-alice")

    response = _with_idp_mock(lambda: client.get("/api/auth/me", headers=_bearer_headers(token)))
    assert response.status_code == 200
    assert response.json()["email"] == "alice@example.com"


# ── claim validation ─────────────────────────────────────────────────────────
def test_wrong_audience_is_refused(client, db, monkeypatch):
    monkeypatch.setenv("TRET_OIDC_API_AUDIENCE", API_AUDIENCE)
    get_settings.cache_clear()
    db.add(_user(email="alice@example.com", sub="sub-alice"))
    token = _sign_access_token(sub="sub-alice", aud="https://some-other-api.example.test")

    response = _with_idp_mock(lambda: client.get("/api/auth/me", headers=_bearer_headers(token)))
    assert response.status_code == 401


def test_wrong_issuer_is_refused(client, db, monkeypatch):
    monkeypatch.setenv("TRET_OIDC_API_AUDIENCE", API_AUDIENCE)
    get_settings.cache_clear()
    db.add(_user(email="alice@example.com", sub="sub-alice"))
    token = _sign_access_token(sub="sub-alice", iss="https://attacker.example.test/")

    response = _with_idp_mock(lambda: client.get("/api/auth/me", headers=_bearer_headers(token)))
    assert response.status_code == 401


def test_expired_token_is_refused(client, db, monkeypatch):
    monkeypatch.setenv("TRET_OIDC_API_AUDIENCE", API_AUDIENCE)
    get_settings.cache_clear()
    db.add(_user(email="alice@example.com", sub="sub-alice"))
    now = int(time.time())
    token = _sign_access_token(sub="sub-alice", iat=now - 1000, exp=now - 500)

    response = _with_idp_mock(lambda: client.get("/api/auth/me", headers=_bearer_headers(token)))
    assert response.status_code == 401


def test_bad_signature_is_refused(client, db, monkeypatch):
    """Signed with a key that is not the one published in the mocked
    JWKS — a forged token an attacker minted with their own keypair."""
    monkeypatch.setenv("TRET_OIDC_API_AUDIENCE", API_AUDIENCE)
    get_settings.cache_clear()
    db.add(_user(email="alice@example.com", sub="sub-alice"))
    forged_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = int(time.time())
    token_bytes = jose_jwt.encode(
        {"alg": "RS256", "kid": KID},
        {"iss": f"{ISSUER_URL}/", "aud": API_AUDIENCE, "sub": "sub-alice", "iat": now, "exp": now + 300},
        forged_key,
    )

    response = _with_idp_mock(
        lambda: client.get("/api/auth/me", headers=_bearer_headers(token_bytes.decode("ascii")))
    )
    assert response.status_code == 401


# ── bearer client allowlist (azp / client_id pinning) ─────────────────────────
def test_bearer_client_ids_unset_accepts_any_azp(client, db, monkeypatch):
    """Back-compat: TRET_OIDC_BEARER_CLIENT_IDS unset (the `client` fixture's
    default) means every client in the tenant is still trusted, exactly as
    before this setting existed — see `oidc_bearer_client_ids`'s docstring in
    config.py for why that is the operator's decision to make, not this
    module's to assume."""
    monkeypatch.setenv("TRET_OIDC_API_AUDIENCE", API_AUDIENCE)
    get_settings.cache_clear()
    db.add(_user(email="alice@example.com", sub="sub-alice"))
    token = _sign_access_token(sub="sub-alice", azp="some-unlisted-client")

    response = _with_idp_mock(lambda: client.get("/api/auth/me", headers=_bearer_headers(token)))
    assert response.status_code == 200


def test_bearer_client_ids_set_azp_in_list_authenticates(client, db, monkeypatch):
    monkeypatch.setenv("TRET_OIDC_API_AUDIENCE", API_AUDIENCE)
    monkeypatch.setenv("TRET_OIDC_BEARER_CLIENT_IDS", "allowed-client-1,allowed-client-2")
    get_settings.cache_clear()
    db.add(_user(email="alice@example.com", sub="sub-alice"))
    token = _sign_access_token(sub="sub-alice", azp="allowed-client-2")

    response = _with_idp_mock(lambda: client.get("/api/auth/me", headers=_bearer_headers(token)))
    assert response.status_code == 200


def test_bearer_client_ids_set_azp_missing_is_refused(client, db, monkeypatch):
    monkeypatch.setenv("TRET_OIDC_API_AUDIENCE", API_AUDIENCE)
    monkeypatch.setenv("TRET_OIDC_BEARER_CLIENT_IDS", "allowed-client-1")
    get_settings.cache_clear()
    db.add(_user(email="alice@example.com", sub="sub-alice"))
    token = _sign_access_token(sub="sub-alice")  # no azp, no client_id claim at all

    response = _with_idp_mock(lambda: client.get("/api/auth/me", headers=_bearer_headers(token)))
    assert response.status_code == 401


def test_bearer_client_ids_set_azp_not_in_list_is_refused(client, db, monkeypatch):
    """The exact vulnerability this setting closes: some other application
    registered in the same tenant and authorized for this API's audience,
    minting a token this API would otherwise have accepted."""
    monkeypatch.setenv("TRET_OIDC_API_AUDIENCE", API_AUDIENCE)
    monkeypatch.setenv("TRET_OIDC_BEARER_CLIENT_IDS", "allowed-client-1")
    get_settings.cache_clear()
    db.add(_user(email="alice@example.com", sub="sub-alice"))
    token = _sign_access_token(sub="sub-alice", azp="some-other-app-in-the-same-tenant")

    response = _with_idp_mock(lambda: client.get("/api/auth/me", headers=_bearer_headers(token)))
    assert response.status_code == 401


def test_bearer_client_ids_client_id_fallback_authenticates(client, db, monkeypatch):
    """Not every IdP emits `azp` on an access token — `client_id` is the
    fallback several major IdPs send instead."""
    monkeypatch.setenv("TRET_OIDC_API_AUDIENCE", API_AUDIENCE)
    monkeypatch.setenv("TRET_OIDC_BEARER_CLIENT_IDS", "allowed-client-1")
    get_settings.cache_clear()
    db.add(_user(email="alice@example.com", sub="sub-alice"))
    token = _sign_access_token(sub="sub-alice", client_id="allowed-client-1")

    response = _with_idp_mock(lambda: client.get("/api/auth/me", headers=_bearer_headers(token)))
    assert response.status_code == 200


def test_bearer_client_ids_azp_wins_over_a_mismatched_client_id(client, db, monkeypatch):
    """`client_id` is only a fallback for an absent `azp`, never a second
    chance for one that is present and wrong."""
    monkeypatch.setenv("TRET_OIDC_API_AUDIENCE", API_AUDIENCE)
    monkeypatch.setenv("TRET_OIDC_BEARER_CLIENT_IDS", "allowed-client-1")
    get_settings.cache_clear()
    db.add(_user(email="alice@example.com", sub="sub-alice"))
    token = _sign_access_token(sub="sub-alice", azp="attacker-app", client_id="allowed-client-1")

    response = _with_idp_mock(lambda: client.get("/api/auth/me", headers=_bearer_headers(token)))
    assert response.status_code == 401


# ── malformed JWKS ─────────────────────────────────────────────────────────────
def test_malformed_jwks_is_refused_not_a_500(client, db, monkeypatch):
    """A `jwks_uri` returning something `JsonWebKey.import_key_set` chokes on
    (authlib raises a bare ValueError/KeyError building the key set, before
    signature verification even starts — see e.g. a key with an unrecognized
    "kty") must come back as a clean 401, never an unhandled 500. The
    document is still cached for `_CACHE_TTL_SECONDS` by `_jwks` either way
    (this fix is not about the caching) — the point is that every request
    during that window now fails cleanly instead of 500ing with a stack
    trace."""
    monkeypatch.setenv("TRET_OIDC_API_AUDIENCE", API_AUDIENCE)
    get_settings.cache_clear()
    db.add(_user(email="alice@example.com", sub="sub-alice"))
    token = _sign_access_token(sub="sub-alice")

    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.get(f"{ISSUER_URL}/.well-known/openid-configuration").mock(
            return_value=httpx.Response(200, json=_discovery_doc())
        )
        respx_mock.get(f"{ISSUER_URL}/.well-known/jwks.json").mock(
            return_value=httpx.Response(200, json={"keys": [{"kty": "not-a-real-key-type"}]})
        )
        response = client.get("/api/auth/me", headers=_bearer_headers(token))
    assert response.status_code == 401
    assert "detail" in response.json()


# ── algorithm-confusion regression guard ──────────────────────────────────────
def test_alg_none_token_is_refused(client, db, monkeypatch):
    monkeypatch.setenv("TRET_OIDC_API_AUDIENCE", API_AUDIENCE)
    get_settings.cache_clear()
    db.add(_user(email="alice@example.com", sub="sub-alice"))
    token = _sign_alg_none_access_token(sub="sub-alice")

    response = _with_idp_mock(lambda: client.get("/api/auth/me", headers=_bearer_headers(token)))
    assert response.status_code == 401


def test_hs256_forged_with_the_public_key_is_refused_with_401_not_500(client, db, monkeypatch):
    monkeypatch.setenv("TRET_OIDC_API_AUDIENCE", API_AUDIENCE)
    get_settings.cache_clear()
    db.add(_user(email="alice@example.com", sub="sub-alice"))
    token = _sign_hs256_access_token(sub="sub-alice")

    response = _with_idp_mock(lambda: client.get("/api/auth/me", headers=_bearer_headers(token)))
    assert response.status_code == 401


# ── identity resolution: no JIT provisioning ──────────────────────────────────
def test_unknown_subject_is_refused_and_creates_no_user(client, db, monkeypatch):
    monkeypatch.setenv("TRET_OIDC_API_AUDIENCE", API_AUDIENCE)
    get_settings.cache_clear()
    token = _sign_access_token(sub="sub-nobody")

    response = _with_idp_mock(lambda: client.get("/api/auth/me", headers=_bearer_headers(token)))
    assert response.status_code == 401
    assert db.store["User"] == []


def test_a_deactivated_user_is_refused(client, db, monkeypatch):
    monkeypatch.setenv("TRET_OIDC_API_AUDIENCE", API_AUDIENCE)
    get_settings.cache_clear()
    db.add(_user(email="alice@example.com", sub="sub-alice", disabled=True))
    token = _sign_access_token(sub="sub-alice")

    response = _with_idp_mock(lambda: client.get("/api/auth/me", headers=_bearer_headers(token)))
    assert response.status_code == 401
    assert "deactivated" in response.json()["detail"].lower()


# ── admin elevation ────────────────────────────────────────────────────────────
def test_admin_role_claim_passes_require_admin(client, db, monkeypatch):
    monkeypatch.setenv("TRET_OIDC_API_AUDIENCE", API_AUDIENCE)
    monkeypatch.setenv("TRET_OIDC_ROLES_CLAIM", ROLES_CLAIM)
    monkeypatch.setenv("TRET_OIDC_ADMIN_ROLE", ADMIN_ROLE)
    get_settings.cache_clear()
    db.add(_user(email="alice@example.com", sub="sub-alice", role="analyst"))
    token = _sign_access_token(sub="sub-alice", **{ROLES_CLAIM: [ADMIN_ROLE, "something-else"]})

    response = _with_idp_mock(lambda: client.get("/api/auth/users", headers=_bearer_headers(token)))
    assert response.status_code == 200


def test_without_the_admin_role_claim_require_admin_refuses(client, db, monkeypatch):
    monkeypatch.setenv("TRET_OIDC_API_AUDIENCE", API_AUDIENCE)
    monkeypatch.setenv("TRET_OIDC_ROLES_CLAIM", ROLES_CLAIM)
    monkeypatch.setenv("TRET_OIDC_ADMIN_ROLE", ADMIN_ROLE)
    get_settings.cache_clear()
    db.add(_user(email="alice@example.com", sub="sub-alice", role="analyst"))
    token = _sign_access_token(sub="sub-alice", **{ROLES_CLAIM: ["some-other-role"]})

    response = _with_idp_mock(lambda: client.get("/api/auth/users", headers=_bearer_headers(token)))
    assert response.status_code == 403


def test_admin_elevation_does_not_persist_to_the_database(client, db, monkeypatch):
    """The elevation must be visible to `require_admin` for this request and
    nowhere else — no write, and the user's own `role` column untouched."""
    monkeypatch.setenv("TRET_OIDC_API_AUDIENCE", API_AUDIENCE)
    monkeypatch.setenv("TRET_OIDC_ROLES_CLAIM", ROLES_CLAIM)
    monkeypatch.setenv("TRET_OIDC_ADMIN_ROLE", ADMIN_ROLE)
    get_settings.cache_clear()
    user = _user(email="alice@example.com", sub="sub-alice", role="analyst")
    db.add(user)
    token = _sign_access_token(sub="sub-alice", **{ROLES_CLAIM: [ADMIN_ROLE]})

    response = _with_idp_mock(lambda: client.get("/api/auth/users", headers=_bearer_headers(token)))
    assert response.status_code == 200
    assert user.role == "analyst"
    assert db.commits == 0


# ── X-Tret-Workspace ──────────────────────────────────────────────────────────
def test_x_tret_workspace_header_selects_among_memberships(client, db, monkeypatch):
    monkeypatch.setenv("TRET_OIDC_API_AUDIENCE", API_AUDIENCE)
    get_settings.cache_clear()
    user = _user(email="alice@example.com", sub="sub-alice")
    ws_a = _workspace("Alpha")
    ws_b = _workspace("Beta")
    db.add(user)
    db.add(ws_a)
    db.add(ws_b)
    db.add(WorkspaceMember(user_id=user.id, workspace_id=ws_a.id, role="analyst"))
    db.add(WorkspaceMember(user_id=user.id, workspace_id=ws_b.id, role="admin"))
    token = _sign_access_token(sub="sub-alice")

    # No header, no session `wid`, and more than one membership: today's
    # unchanged 409, exactly as a cookie caller in the same spot would see.
    response = _with_idp_mock(
        lambda: client.get("/api/_test/workspace", headers=_bearer_headers(token))
    )
    assert response.status_code == 409

    response = _with_idp_mock(
        lambda: client.get(
            "/api/_test/workspace",
            headers={**_bearer_headers(token), "X-Tret-Workspace": str(ws_b.id)},
        )
    )
    assert response.status_code == 200
    assert response.json() == {"workspace_id": str(ws_b.id), "role": "admin"}


def test_x_tret_workspace_header_naming_a_non_member_workspace_is_refused(client, db, monkeypatch):
    monkeypatch.setenv("TRET_OIDC_API_AUDIENCE", API_AUDIENCE)
    get_settings.cache_clear()
    user = _user(email="alice@example.com", sub="sub-alice")
    ws_a = _workspace("Alpha")
    ws_b = _workspace("Beta")
    other = _workspace("NotMine")
    db.add(user)
    db.add(ws_a)
    db.add(ws_b)
    db.add(other)
    db.add(WorkspaceMember(user_id=user.id, workspace_id=ws_a.id, role="analyst"))
    db.add(WorkspaceMember(user_id=user.id, workspace_id=ws_b.id, role="analyst"))
    token = _sign_access_token(sub="sub-alice")

    response = _with_idp_mock(
        lambda: client.get(
            "/api/_test/workspace",
            headers={**_bearer_headers(token), "X-Tret-Workspace": str(other.id)},
        )
    )
    # A workspace the caller does not belong to must behave exactly as an
    # invalid `wid` does today — falls through, and with two real
    # memberships and nothing resolved, that is the same 409 a missing
    # header already produces. Never access to `other`.
    assert response.status_code == 409


def test_a_malformed_x_tret_workspace_header_does_not_500(client, db, monkeypatch):
    monkeypatch.setenv("TRET_OIDC_API_AUDIENCE", API_AUDIENCE)
    get_settings.cache_clear()
    user = _user(email="alice@example.com", sub="sub-alice")
    ws_a = _workspace("Alpha")
    db.add(user)
    db.add(ws_a)
    db.add(WorkspaceMember(user_id=user.id, workspace_id=ws_a.id, role="analyst"))
    token = _sign_access_token(sub="sub-alice")

    response = _with_idp_mock(
        lambda: client.get(
            "/api/_test/workspace",
            headers={**_bearer_headers(token), "X-Tret-Workspace": "not-a-uuid"},
        )
    )
    # Falls through to the sole-membership path, same as a malformed `wid`.
    assert response.status_code == 200
    assert response.json()["workspace_id"] == str(ws_a.id)


# ── cookie path regression ────────────────────────────────────────────────────
def test_cookie_session_still_works_and_ignores_a_bearer_header(client, db, monkeypatch):
    """A session cookie wins outright, bearer enabled or not: `current_user`
    must not even look at `Authorization` once a cookie is present."""
    monkeypatch.setenv("TRET_OIDC_API_AUDIENCE", API_AUDIENCE)
    get_settings.cache_clear()
    workspace = _workspace("Default")
    password_user = User(
        id=uuid.uuid4(),
        email="carol@example.com",
        display_name="Carol",
        password_hash=_HASHER.hash("correct-horse-1"),
        role="analyst",
        session_epoch=0,
        disabled=False,
        created_at=datetime.now(timezone.utc),
    )
    db.add(workspace)
    db.add(password_user)
    db.add(WorkspaceMember(user_id=password_user.id, workspace_id=workspace.id, role="analyst"))

    login_response = client.post(
        "/api/auth/login", json={"email": "carol@example.com", "password": "correct-horse-1"}
    )
    assert login_response.status_code == 200

    # A garbage bearer header alongside the now-set session cookie must be
    # ignored entirely, not merely fail open to a 401.
    me = client.get("/api/auth/me", headers={"Authorization": "Bearer not-a-real-jwt-at-all"})
    assert me.status_code == 200
    assert me.json()["email"] == "carol@example.com"


# ── revocation semantics (documented, not invented) ───────────────────────────
def test_revoke_sessions_does_not_revoke_an_already_issued_bearer_token(client, db, monkeypatch):
    """Security-review finding: `POST /api/auth/users/{id}/revoke-sessions`
    bumps `session_epoch`, which only ever invalidates *cookie* sessions
    through `credential_version`. `session_epoch` is an opaque counter with
    no time semantics — nothing on a bearer access token can be compared
    against it the way an `iat` could be compared to a stored timestamp — so
    a bearer token already issued for the account must keep authenticating
    after this call, until its own `exp`. This is the documented behavior
    (auth.py's module docstring and `revoke_sessions`'s own docstring), not
    an oversight: asserting it here is what would catch someone "fixing" it
    into a mapping that doesn't actually exist.
    """
    monkeypatch.setenv("TRET_OIDC_API_AUDIENCE", API_AUDIENCE)
    get_settings.cache_clear()
    workspace = _workspace("Default")
    admin = User(
        id=uuid.uuid4(),
        email="admin@example.com",
        display_name="Admin",
        password_hash=_HASHER.hash("admin-password-1"),
        role="admin",
        session_epoch=0,
        disabled=False,
        created_at=datetime.now(timezone.utc),
    )
    alice = _user(email="alice@example.com", sub="sub-alice")
    db.add(workspace)
    db.add(admin)
    db.add(alice)
    db.add(WorkspaceMember(user_id=admin.id, workspace_id=workspace.id, role="admin"))
    db.add(WorkspaceMember(user_id=alice.id, workspace_id=workspace.id, role="analyst"))
    token = _sign_access_token(sub="sub-alice")

    pre = _with_idp_mock(lambda: client.get("/api/auth/me", headers=_bearer_headers(token)))
    assert pre.status_code == 200

    login = client.post(
        "/api/auth/login", json={"email": "admin@example.com", "password": "admin-password-1"}
    )
    assert login.status_code == 200
    revoke = client.post(f"/api/auth/users/{alice.id}/revoke-sessions")
    assert revoke.status_code == 200
    assert alice.session_epoch == 1
    client.post("/api/auth/logout")  # drop the admin's cookie before the bearer-only call below

    post = _with_idp_mock(lambda: client.get("/api/auth/me", headers=_bearer_headers(token)))
    assert post.status_code == 200
    assert post.json()["email"] == "alice@example.com"
