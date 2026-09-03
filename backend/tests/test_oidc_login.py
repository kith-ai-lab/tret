"""Generic OIDC login (tret/api/oidc.py + tret/services/identity.py).

The IdP is entirely faked: respx mocks discovery, JWKS and the token
endpoint, and id_tokens are signed with a locally generated RSA key exposed
through that same mocked JWKS — nothing here reaches a real network (the
egress chokepoint, tests/test_egress_chokepoint.py, would fail the suite if
oidc.py ever tried to). `TRET_OIDC_ISSUER` names a host that resolves to
nothing; that is fine, because `oidc.py`'s policy is `VERIFY_NONE` — see its
module docstring — so no DNS lookup ever happens either.

Real dependency chain (FastAPI, itsdangerous, authlib.jose), fake database
— same shape as tests/test_auth_credentials.py's FakeSession, extended with
the handful of models `services/workspace.py::create_workspace` and
`services/identity.py::match_or_provision` touch (Workspace, WorkspaceMember,
Project, Harness, Invite).
"""
from __future__ import annotations

import base64
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx
from argon2 import PasswordHasher
from authlib.jose import JsonWebKey
from authlib.jose import jwt as jose_jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.sql.elements import False_, Null, True_

from tret.api import auth, oidc
from tret.api.auth import login_limiter
from tret.config import get_settings
from tret.db.engine import get_db
from tret.db.models import Invite, User, Workspace, WorkspaceMember

# ── fixed test IdP ───────────────────────────────────────────────────────────
ISSUER_HOST = "issuer.example.test"
ISSUER_URL = f"https://{ISSUER_HOST}"
CLIENT_ID = "test-client-id"
CLIENT_SECRET = "test-client-secret"
REDIRECT_URL = "https://tret.example.test/api/auth/oidc/callback"
KID = "test-kid"

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


_OMIT = object()  # `_sign_id_token(exp=_OMIT)` drops a claim the default payload sets


def _sign_id_token(**claims) -> str:
    now = int(time.time())
    payload = {
        "iss": f"{ISSUER_URL}/",
        "aud": CLIENT_ID,
        "sub": "sub-1",
        "email": "person@example.com",
        "email_verified": True,
        "name": "Person One",
        "iat": now,
        "exp": now + 300,
    }
    payload.update(claims)
    payload = {k: v for k, v in payload.items() if v is not _OMIT}
    token: bytes = jose_jwt.encode({"alg": "RS256", "kid": KID}, payload, _RSA_KEY)
    return token.decode("ascii")


def _sign_alg_none_id_token(**claims) -> str:
    """A hand-built, entirely unsigned JWT (`alg: none`) — authlib's own
    `jwt.encode` refuses to produce one (`none` was never in the algorithm
    list `authlib.jose.jwt` was constructed with), which is exactly why this
    needs to be crafted by hand: it is the forgery the pinned-algorithms
    check must reject, not something the library would let a legitimate
    caller generate by accident.
    """
    now = int(time.time())
    payload = {
        "iss": f"{ISSUER_URL}/",
        "aud": CLIENT_ID,
        "sub": "sub-1",
        "email": "person@example.com",
        "email_verified": True,
        "name": "Person One",
        "iat": now,
        "exp": now + 300,
    }
    payload.update(claims)

    def _b64(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode("ascii")

    return f"{_b64({'alg': 'none', 'typ': 'JWT'})}.{_b64(payload)}."


def _sign_hs256_id_token(**claims) -> str:
    """A validly-*signed* (HS256, not RS256) id_token — the algorithm-
    confusion repro: the secret below is known only to whoever signs this
    token, never published anywhere the real IdP's JWKS is, but the header's
    `alg` alone is what tells authlib which key-shape to expect. Used to
    confirm `_callback` rejects this cleanly (401) rather than crashing
    (authlib's own internals raise a bare `KeyError` trying to coerce this
    callback's asymmetric JWKS keys into an HMAC secret for HS256)."""
    now = int(time.time())
    payload = {
        "iss": f"{ISSUER_URL}/",
        "aud": CLIENT_ID,
        "sub": "sub-1",
        "email": "person@example.com",
        "email_verified": True,
        "name": "Person One",
        "iat": now,
        "exp": now + 300,
    }
    payload.update(claims)
    token: bytes = jose_jwt.encode({"alg": "HS256"}, payload, "attacker-controlled-hs256-secret")
    return token.decode("ascii")


# ── fake database (see test_auth_credentials.py's FakeSession — same shape,
# extended with the models create_workspace/match_or_provision also touch) ──
def _backfill_defaults(obj) -> None:
    """What a real flush against Postgres would assign — id / created_at —
    for a Python-side `default=` that never fires on a plain object nobody
    ever attaches to a real session."""
    if hasattr(obj, "id") and getattr(obj, "id", None) is None:
        obj.id = uuid.uuid4()
    if hasattr(obj, "created_at") and getattr(obj, "created_at", None) is None:
        obj.created_at = datetime.now(timezone.utc)


def _matches(row, stmt) -> bool:
    """Evaluate a simple `where` (== / != / IS NOT NULL / IS NULL / IS
    TRUE/FALSE) against a row — test_auth_credentials.py's `_matches`, plus
    `!=` (`Harness.task_profile != "chat"`, in `_seed_default_harnesses`)."""
    where = stmt.whereclause
    if where is None:
        return True
    for clause in getattr(where, "clauses", [where]):
        left, right = getattr(clause, "left", None), getattr(clause, "right", None)
        if left is None:
            continue
        actual = getattr(row, left.key, None)
        op = getattr(clause.operator, "__name__", "")
        if isinstance(right, True_):
            if actual is not True:
                return False
        elif isinstance(right, False_):
            if actual is not False:
                return False
        elif isinstance(right, Null) or op in ("is_", "is_not"):
            is_null = actual is None
            if op == "is_not" and is_null:
                return False
            if op == "is_" and not is_null:
                return False
        elif op == "ne":
            if actual == getattr(right, "value", None):
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
    def __init__(self, *, users=(), workspaces=(), members=(), invites=()):
        self.store: dict[str, list] = {
            "User": list(users),
            "Workspace": list(workspaces),
            "WorkspaceMember": list(members),
            "Invite": list(invites),
            "Project": [],
            "Harness": [],
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
        if rows and hasattr(rows[0], "created_at"):
            epoch = datetime.min.replace(tzinfo=timezone.utc)
            rows = sorted(rows, key=lambda r: r.created_at or epoch)
        return FakeResult(rows)

    def add(self, obj):
        _backfill_defaults(obj)
        self.store.setdefault(type(obj).__name__, []).append(obj)

    async def flush(self):  # add() already backfills; nothing deferred here
        pass

    async def commit(self):
        self.commits += 1


# ── fixture builders ─────────────────────────────────────────────────────────
def _password_user(email: str, password: str, *, oidc_sub: str | None = None) -> User:
    return User(
        id=uuid.uuid4(),
        email=email,
        display_name=email.split("@")[0].title(),
        password_hash=_HASHER.hash(password),
        role="analyst",
        oidc_sub=oidc_sub,
        session_epoch=0,
        disabled=False,
        created_at=datetime.now(timezone.utc),
    )


def _workspace(name: str, *, kind: str = "team") -> Workspace:
    return Workspace(id=uuid.uuid4(), name=name, kind=kind, created_at=datetime.now(timezone.utc))


def _invite(*, workspace_id, email: str, role: str = "analyst", status: str = "pending", hours=24) -> Invite:
    return Invite(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        email=email,
        role=role,
        token=uuid.uuid4().hex,
        status=status,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=hours),
        created_at=datetime.now(timezone.utc),
    )


# ── fixtures ─────────────────────────────────────────────────────────────────
@pytest.fixture
def db() -> FakeSession:
    return FakeSession()


@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setenv("TRET_OIDC_ISSUER", ISSUER_HOST)
    monkeypatch.setenv("TRET_OIDC_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("TRET_OIDC_CLIENT_SECRET", CLIENT_SECRET)
    monkeypatch.setenv("TRET_OIDC_REDIRECT_URL", REDIRECT_URL)
    # Every claims email in this file is @example.com — allowlisting that
    # domain by default keeps every test above the dedicated
    # "── JIT-provisioning gate ──" section exercising what it already did
    # before that gate existed (the gate's own tests below override this).
    monkeypatch.setenv("TRET_OIDC_ALLOWED_EMAIL_DOMAINS", "example.com")
    get_settings.cache_clear()
    oidc._discovery_cache.clear()
    oidc._jwks_cache.clear()
    login_limiter.reset()

    app = FastAPI()
    app.include_router(auth.router)
    app.include_router(oidc.router)
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()
    oidc._discovery_cache.clear()
    oidc._jwks_cache.clear()
    login_limiter.reset()


# ── round-trip helpers ───────────────────────────────────────────────────────
def _login_redirect(
    client, *, next_path: str = "/", time_offset: float = 0.0, discovery: dict | None = None
) -> tuple[str, str]:
    """GET /api/auth/oidc/login; returns (state, raw_nonce) parsed off the
    redirect to the (mocked) authorization endpoint.

    `assert_all_called=False`: the token/JWKS routes other helpers register
    in the same test are not necessarily hit by this call alone (discovery
    gets cached after the first fetch), and respx's default would otherwise
    fail the block over routes that were legitimately never exercised here.
    """
    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.get(f"{ISSUER_URL}/.well-known/openid-configuration").mock(
            return_value=httpx.Response(200, json=discovery or _discovery_doc())
        )
        if time_offset:
            with mock.patch("time.time", return_value=time.time() + time_offset):
                resp = client.get(
                    "/api/auth/oidc/login", params={"next": next_path}, follow_redirects=False
                )
        else:
            resp = client.get(
                "/api/auth/oidc/login", params={"next": next_path}, follow_redirects=False
            )
    assert resp.status_code in (302, 307), resp.text
    qs = parse_qs(urlsplit(resp.headers["location"]).query)
    return qs["state"][0], qs["nonce"][0]


def _callback(
    client, state: str, id_token: str, *, code: str = "test-code", discovery: dict | None = None
):
    """GET /api/auth/oidc/callback against a mocked JWKS + token exchange."""
    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.get(f"{ISSUER_URL}/.well-known/openid-configuration").mock(
            return_value=httpx.Response(200, json=discovery or _discovery_doc())
        )
        respx_mock.get(f"{ISSUER_URL}/.well-known/jwks.json").mock(
            return_value=httpx.Response(200, json=_JWKS)
        )
        respx_mock.post(f"{ISSUER_URL}/oauth/token").mock(
            return_value=httpx.Response(200, json={"id_token": id_token})
        )
        return client.get(
            "/api/auth/oidc/callback", params={"code": code, "state": state}, follow_redirects=False
        )


def _tamper_state_payload(state: str) -> str:
    """Deterministically corrupt a signed `state` — flipping a byte inside
    the decoded *payload*, leaving the timestamp and signature segments
    exactly as `_login` produced them, so the (unchanged) old signature can
    never match the (changed) payload it is checked against.

    Flipping the raw `state` string's last character (the naive approach)
    is not reliable: `itsdangerous`'s payload segment is URL-safe base64,
    and its final character can carry redundant bits the decoder ignores —
    some single-character flips there decode to the *identical* bytes, so
    the original signature legitimately still verifies and the test flakes
    (~40% locally). Corrupting a decoded byte has no such ambiguity: every
    byte value change is a real, unrecoverable payload change.

    The token is `<payload>.<timestamp>.<signature>`, but the payload
    segment is not simply "everything before the first dot": when
    itsdangerous's zlib-compressed form is smaller than the plain one (as it
    often is here — `URLSafeSerializerMixin.dump_payload`), it prepends a
    literal "." to the payload segment itself as a compression marker, so
    the token can contain a leading `.` that is *part of the payload*, not a
    field separator. Splitting from the right (the timestamp and signature
    segments never contain a compression marker) instead of naively on the
    first "." avoids that trap.
    """
    *payload_parts, timestamp_b64, sig_b64 = state.split(".")
    payload_segment = ".".join(payload_parts)
    compressed_marker = payload_segment.startswith(".")
    body = payload_segment[1:] if compressed_marker else payload_segment
    padded = body + "=" * (-len(body) % 4)
    raw = bytearray(base64.urlsafe_b64decode(padded))
    raw[0] ^= 0xFF  # guaranteed to differ from the original byte
    tampered_body = base64.urlsafe_b64encode(bytes(raw)).rstrip(b"=").decode("ascii")
    tampered_segment = f".{tampered_body}" if compressed_marker else tampered_body
    return f"{tampered_segment}.{timestamp_b64}.{sig_b64}"


def _round_trip(
    client,
    *,
    claims: dict | None = None,
    next_path: str = "/",
    discovery: dict | None = None,
    token_signer=_sign_id_token,
):
    """login -> callback, with the id_token's nonce wired to what login sent."""
    state, nonce = _login_redirect(client, next_path=next_path, discovery=discovery)
    token = token_signer(nonce=nonce, **(claims or {}))
    return _callback(client, state, token, discovery=discovery)


# ── full round trip ──────────────────────────────────────────────────────────
def test_full_round_trip_sets_a_working_session(client, db):
    """login redirect -> callback -> cookie set -> /me works, with a
    self-host (multi_tenant unset) sole workspace already seeded."""
    sole = _workspace("Default")
    db.add(sole)

    response = _round_trip(client, claims={"sub": "sub-alice", "email": "alice@example.com"})
    assert response.status_code == 302
    assert response.headers["location"] == "/"
    assert "tret_session" in response.cookies or any(
        "tret_session" in v for v in response.headers.get_list("set-cookie")
    )

    me = client.get("/api/auth/me")
    assert me.status_code == 200
    body = me.json()
    assert body["email"] == "alice@example.com"
    assert body["global_role"] == "analyst"
    assert [w["kind"] for w in body["workspaces"]] == ["team"]
    assert body["current_workspace_id"] == str(sole.id)

    # The provisioned user really landed in the fake store, with no password.
    (created,) = [u for u in db.store["User"] if u.email == "alice@example.com"]
    assert created.password_hash is None
    assert created.oidc_sub == "sub-alice"


def test_next_carries_through_the_round_trip(client, db):
    db.add(_workspace("Default"))
    response = _round_trip(client, claims={"sub": "sub-bob"}, next_path="/dashboard")
    assert response.headers["location"] == "/dashboard"


# ── nonce / state ─────────────────────────────────────────────────────────────
def test_bad_nonce_is_refused(client, db):
    db.add(_workspace("Default"))
    state, _real_nonce = _login_redirect(client)
    token = _sign_id_token(nonce="not-the-nonce-that-was-sent")
    response = _callback(client, state, token)
    assert response.status_code == 401
    assert "nonce" in response.json()["detail"].lower()


def test_a_tampered_state_is_refused(client, db):
    db.add(_workspace("Default"))
    state, nonce = _login_redirect(client)
    tampered = _tamper_state_payload(state)
    token = _sign_id_token(nonce=nonce)
    response = _callback(client, tampered, token)
    assert response.status_code == 401


def test_an_expired_state_is_refused(client, db):
    db.add(_workspace("Default"))
    # Minted with the clock ~11 minutes in the past: itsdangerous embeds the
    # signing time in the token itself, so this survives to the real-time
    # unsign() call in the callback below.
    state, nonce = _login_redirect(client, time_offset=-(oidc.STATE_MAX_AGE + 60))
    token = _sign_id_token(nonce=nonce)
    response = _callback(client, state, token)
    assert response.status_code == 401


# ── sid cookie: state-to-browser binding ──────────────────────────────────────
def test_missing_sid_cookie_is_refused(client, db):
    """A `(code, state)` pair presented by a browser that never went through
    `_login` (no `tret_oidc_sid` cookie at all) — the session-planting shape
    this finding fixes: an attacker relays a URL to a victim whose browser
    never received the cookie `_login` would have set."""
    db.add(_workspace("Default"))
    state, nonce = _login_redirect(client)
    token = _sign_id_token(nonce=nonce)
    client.cookies.pop(oidc.SID_COOKIE, None)  # the cookie _login set, dropped
    response = _callback(client, state, token)
    assert response.status_code == 401
    assert "cookie" in response.json()["detail"].lower()


def test_mismatched_sid_cookie_is_refused(client, db):
    """The cookie is present but does not match the `sid` signed into
    `state` — e.g. a second, unrelated login attempt's cookie still sitting
    in the jar."""
    db.add(_workspace("Default"))
    state, nonce = _login_redirect(client)
    token = _sign_id_token(nonce=nonce)
    client.cookies.set(oidc.SID_COOKIE, "not-the-sid-that-was-issued")
    response = _callback(client, state, token)
    assert response.status_code == 401
    assert "cookie" in response.json()["detail"].lower()


def test_sid_cookie_is_set_on_login_and_cleared_on_successful_callback(client, db):
    db.add(_workspace("Default"))
    state, nonce = _login_redirect(client)
    assert oidc.SID_COOKIE in client.cookies  # _login set it

    token = _sign_id_token(nonce=nonce)
    response = _callback(client, state, token)
    assert response.status_code == 302

    # Cleared on success — a captured, already-used URL cannot be replayed
    # even by the browser that legitimately started the login.
    set_cookie_headers = response.headers.get_list("set-cookie")
    cleared = [h for h in set_cookie_headers if h.startswith(f"{oidc.SID_COOKIE}=")]
    assert cleared, set_cookie_headers
    assert 'Max-Age=0' in cleared[0] or 'max-age=0' in cleared[0].lower()


# ── id_token claim hardening ──────────────────────────────────────────────────
# These pin authlib's own gaps rather than tret's logic: `validate_exp` silently
# no-ops when "exp" is simply absent (RFC 7519 makes it OPTIONAL; OIDC id_tokens
# do not have that luxury), and a bare `aud` check never looks at `azp` at all.
# All of these are exploitable only with control of the token endpoint's
# response — defence in depth, not a reachable-today attacker path.
def test_id_token_without_exp_is_refused(client, db):
    db.add(_workspace("Default"))
    state, nonce = _login_redirect(client)
    token = _sign_id_token(nonce=nonce, exp=_OMIT)
    response = _callback(client, state, token)
    assert response.status_code == 401


def test_multi_audience_id_token_without_azp_is_refused(client, db):
    """OIDC Core 3.1.3.7 #4: more than one audience without an `azp` naming
    us is ambiguous — could this token have been intended for someone else
    who then leaked/replayed it?"""
    db.add(_workspace("Default"))
    state, nonce = _login_redirect(client)
    token = _sign_id_token(nonce=nonce, aud=[CLIENT_ID, "some-other-client"])
    response = _callback(client, state, token)
    assert response.status_code == 401
    assert "azp" in response.json()["detail"].lower()


def test_multi_audience_id_token_with_correct_azp_is_accepted(client, db):
    """OIDC Core 3.1.3.7 #5: `azp` present and equal to our client_id
    resolves the ambiguity multiple audiences raise — login proceeds."""
    db.add(_workspace("Default"))
    response = _round_trip(
        client,
        claims={
            "sub": "sub-azp-ok",
            "email": "azpok@example.com",
            "aud": [CLIENT_ID, "some-other-client"],
            "azp": CLIENT_ID,
        },
    )
    assert response.status_code == 302


def test_single_audience_id_token_with_wrong_azp_is_refused(client, db):
    """`azp`, when present at all, must name us — even on a single-audience
    token that would otherwise need no `azp` to disambiguate anything: an
    `azp` naming some other client is itself evidence this token was minted
    for someone else, not something a single-valued `aud` should excuse."""
    db.add(_workspace("Default"))
    response = _round_trip(client, claims={"azp": "some-other-client"})
    assert response.status_code == 401
    assert "azp" in response.json()["detail"].lower()


def test_id_token_with_alg_outside_the_advertised_set_is_refused(client, db):
    """The issuer's discovery document says id_tokens are only ever signed
    with ES256; a token signed RS256 (otherwise a perfectly valid signature
    from the same key set) must be refused rather than accepted because
    RS256 happens to be in authlib's own default algorithm list."""
    db.add(_workspace("Default"))
    discovery = _discovery_doc(id_token_signing_alg_values_supported=["ES256"])
    response = _round_trip(client, discovery=discovery)
    assert response.status_code == 401


def test_id_token_with_alg_none_is_refused(client, db):
    """`alg: none` (an entirely unsigned token) must never be accepted —
    already true before this change (authlib's default algorithm set never
    included "none"), kept here as a regression test."""
    db.add(_workspace("Default"))
    response = _round_trip(client, token_signer=_sign_alg_none_id_token)
    assert response.status_code == 401


def test_id_token_with_hs256_alg_is_refused_with_401_not_500(client, db):
    """Repro for the algorithm-confusion crash: an HS256-headed id_token
    verified against this callback's `key_set` (a JWKS — asymmetric public
    keys only) makes authlib try to coerce a public key into an HMAC secret
    and raise a bare `KeyError` from its own internals, not a `JoseError`.
    That must still come back as a 401 (untrusted token, rejected), never an
    unhandled 500."""
    db.add(_workspace("Default"))
    response = _round_trip(client, token_signer=_sign_hs256_id_token)
    assert response.status_code == 401


def test_issuer_advertising_hs256_and_rs256_pins_to_rs256_only(client, db):
    """`_signing_algorithms` intersects the issuer's advertised list with
    authlib's *asymmetric* algorithms only — HS256 is dropped even though the
    issuer listed it, because this callback's key material (a JWKS) can never
    legitimately verify a symmetric signature. An RS256-signed token (the
    only alg left after the intersection) is still accepted; an HS256-signed
    one, under that same discovery document, is refused."""
    db.add(_workspace("Default"))
    discovery = _discovery_doc(id_token_signing_alg_values_supported=["HS256", "RS256"])

    response = _round_trip(client, discovery=discovery)
    assert response.status_code == 302

    response = _round_trip(client, discovery=discovery, token_signer=_sign_hs256_id_token)
    assert response.status_code == 401


def test_authlib_supported_algs_pin_matches_installed_authlib():
    """Guards `_AUTHLIB_SUPPORTED_ALGS` against silently drifting out of sync
    with the installed authlib's own default `jwt` algorithm set — the
    fallback `_signing_algorithms` falls back to when an issuer's discovery
    document doesn't advertise anything, and the universe `_ASYMMETRIC_ALGS`
    is drawn from. If a future authlib release adds or drops an algorithm
    from its default `jwt` object, this must fail loudly rather than let the
    two lists quietly disagree."""
    from authlib.jose import jwt as installed_default_jwt

    assert oidc._AUTHLIB_SUPPORTED_ALGS == frozenset(installed_default_jwt._jws._algorithms)


# ── discovery document issuer must match TRET_OIDC_ISSUER (OIDC Core 4.3) ────


def test_discovery_issuer_mismatch_is_refused_at_login(client, db):
    """The discovery document's `issuer` naming a different host than the
    configured TRET_OIDC_ISSUER must refuse login up front (502), not be
    trusted as the `iss` an id_token is later checked against."""
    db.add(_workspace("Default"))
    discovery = _discovery_doc(issuer="https://attacker.example.test/")

    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.get(f"{ISSUER_URL}/.well-known/openid-configuration").mock(
            return_value=httpx.Response(200, json=discovery)
        )
        resp = client.get("/api/auth/oidc/login", params={"next": "/"}, follow_redirects=False)
    assert resp.status_code == 502
    assert "issuer" in resp.text.lower()
    # Not cached: a bad document must not keep being served for the TTL after
    # the underlying mismatch is fixed.
    assert ISSUER_HOST not in oidc._discovery_cache


def test_discovery_non_string_issuer_is_refused_with_502(client, db):
    """A discovery document is attacker- or misconfiguration-controlled JSON;
    the required-fields check must not just check truthiness (`12345` is
    truthy) but also type, because `_normalized_issuer` unconditionally calls
    `.strip()` on this value. Before the fix this raised an unhandled
    AttributeError (500); it must instead be refused the same way a missing
    field is (502)."""
    db.add(_workspace("Default"))
    discovery = _discovery_doc(issuer=12345)

    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.get(f"{ISSUER_URL}/.well-known/openid-configuration").mock(
            return_value=httpx.Response(200, json=discovery)
        )
        resp = client.get("/api/auth/oidc/login", params={"next": "/"}, follow_redirects=False)
    assert resp.status_code == 502
    assert "issuer" in resp.text.lower()
    assert ISSUER_HOST not in oidc._discovery_cache


def test_discovery_issuer_differing_only_by_trailing_slash_is_accepted(client, db):
    """OIDC Core's URL-equality rule tolerates a single trailing slash — the
    configured issuer (bare host, no scheme) normalizes to
    `https://issuer.example.test`, and the fixture's default discovery
    document already advertises `https://issuer.example.test/`, so the round
    trip must keep working exactly as it did before this check existed."""
    db.add(_workspace("Default"))
    discovery = _discovery_doc(issuer=f"{ISSUER_URL}/")

    response = _round_trip(client, discovery=discovery)
    assert response.status_code == 302
    assert ISSUER_HOST in oidc._discovery_cache


# ── verified-email link / collisions ─────────────────────────────────────────
def test_verified_email_auto_links_and_keeps_the_password(client, db):
    sole = _workspace("Default")
    existing = _password_user("carol@example.com", "correct-horse-1")
    db.add(sole)
    db.add(existing)
    db.add(WorkspaceMember(user_id=existing.id, workspace_id=sole.id, role="analyst"))
    original_hash = existing.password_hash

    response = _round_trip(
        client, claims={"sub": "sub-carol", "email": "carol@example.com", "email_verified": True}
    )
    assert response.status_code == 302

    assert existing.oidc_sub == "sub-carol"
    assert existing.password_hash == original_hash  # untouched — link, not replace
    assert len(db.store["User"]) == 1  # no duplicate account created

    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["email"] == "carol@example.com"


def test_unverified_email_collision_is_refused_with_409(client, db):
    db.add(_workspace("Default"))
    db.add(_password_user("dave@example.com", "correct-horse-2"))

    response = _round_trip(
        client, claims={"sub": "sub-dave", "email": "dave@example.com", "email_verified": False}
    )
    assert response.status_code == 409
    assert "verify" in response.json()["detail"].lower()


def test_a_different_oidc_sub_already_on_that_email_is_refused_with_409(client, db):
    db.add(_workspace("Default"))
    db.add(_password_user("erin@example.com", "correct-horse-3", oidc_sub="already-linked-sub"))

    response = _round_trip(
        client, claims={"sub": "a-different-sub", "email": "erin@example.com", "email_verified": True}
    )
    assert response.status_code == 409
    assert "different" in response.json()["detail"].lower()


def test_disabled_account_is_refused_even_via_oidc(client, db):
    sole = _workspace("Default")
    disabled = _password_user("frank@example.com", "correct-horse-4", oidc_sub="sub-frank")
    disabled.disabled = True
    db.add(sole)
    db.add(disabled)
    db.add(WorkspaceMember(user_id=disabled.id, workspace_id=sole.id, role="analyst"))

    response = _round_trip(client, claims={"sub": "sub-frank", "email": "frank@example.com"})
    assert response.status_code == 403


# ── JIT provisioning ──────────────────────────────────────────────────────────
def test_jit_provisioning_with_multi_tenant_creates_a_personal_workspace(client, db, monkeypatch):
    monkeypatch.setenv("TRET_MULTI_TENANT", "true")
    get_settings.cache_clear()

    response = _round_trip(
        client, claims={"sub": "sub-grace", "email": "grace@example.com", "name": "Grace Hopper"}
    )
    assert response.status_code == 302

    me = client.get("/api/auth/me")
    body = me.json()
    assert len(body["workspaces"]) == 1
    workspace = body["workspaces"][0]
    assert workspace["kind"] == "personal"
    assert workspace["role"] == "owner"  # owner of their own personal workspace
    assert body["current_workspace_id"] == workspace["id"]
    assert body["global_role"] == "analyst"  # the account's global role stays analyst
    assert "grace" in workspace["name"].lower()

    monkeypatch.delenv("TRET_MULTI_TENANT", raising=False)
    get_settings.cache_clear()


def test_jit_provisioning_without_multi_tenant_joins_the_sole_workspace(client, db):
    sole = _workspace("Default", kind="team")
    db.add(sole)

    response = _round_trip(client, claims={"sub": "sub-henry", "email": "henry@example.com"})
    assert response.status_code == 302

    me = client.get("/api/auth/me")
    body = me.json()
    assert len(body["workspaces"]) == 1
    assert body["workspaces"][0]["id"] == str(sole.id)
    assert body["workspaces"][0]["kind"] == "team"
    assert body["workspaces"][0]["role"] == "analyst"
    assert body["current_workspace_id"] == str(sole.id)


# ── JIT-provisioning gate ─────────────────────────────────────────────────────
# The finding this section pins: with `multi_tenant=False` and no domain
# allowlist configured, *any* IdP-authenticated stranger used to land
# straight in the sole workspace. `client`'s own `TRET_OIDC_ALLOWED_EMAIL_
# DOMAINS=example.com` is overridden or cleared in each test below to
# exercise the gate itself rather than the default every other test in this
# file relies on.
def test_a_domain_outside_the_allowlist_is_refused(client, db, monkeypatch):
    db.add(_workspace("Default"))
    monkeypatch.setenv("TRET_OIDC_ALLOWED_EMAIL_DOMAINS", "kithailab.com,other.example")
    get_settings.cache_clear()
    try:
        response = _round_trip(
            client, claims={"sub": "sub-liam", "email": "liam@example.com"}
        )
        assert response.status_code == 403
        assert "domain" in response.json()["detail"].lower()
    finally:
        get_settings.cache_clear()


def test_a_domain_inside_the_allowlist_is_provisioned(client, db, monkeypatch):
    db.add(_workspace("Default"))
    monkeypatch.setenv("TRET_OIDC_ALLOWED_EMAIL_DOMAINS", "kithailab.com,example.com")
    get_settings.cache_clear()
    try:
        response = _round_trip(
            client, claims={"sub": "sub-maya", "email": "maya@example.com"}
        )
        assert response.status_code == 302
    finally:
        get_settings.cache_clear()


def test_no_allowlist_and_no_multi_tenant_requires_an_invitation(client, db, monkeypatch):
    """No pending invite for this email, self-host, no domain allowlist —
    the tenancy hole this whole gate closes."""
    db.add(_workspace("Default"))
    monkeypatch.delenv("TRET_OIDC_ALLOWED_EMAIL_DOMAINS", raising=False)
    get_settings.cache_clear()
    try:
        response = _round_trip(
            client, claims={"sub": "sub-nora", "email": "nora@example.com"}
        )
        assert response.status_code == 403
        assert "invitation" in response.json()["detail"].lower()
        assert not [u for u in db.store["User"] if u.email == "nora@example.com"]
    finally:
        get_settings.cache_clear()


def test_no_allowlist_and_no_multi_tenant_but_a_pending_invite_still_provisions(
    client, db, monkeypatch
):
    sole = _workspace("Default", kind="team")
    team = _workspace("Climate Co", kind="team")
    invite = _invite(workspace_id=team.id, email="oscar@example.com", role="approver")
    db.add(sole)
    db.add(team)
    db.add(invite)
    monkeypatch.delenv("TRET_OIDC_ALLOWED_EMAIL_DOMAINS", raising=False)
    get_settings.cache_clear()
    try:
        response = _round_trip(
            client, claims={"sub": "sub-oscar", "email": "oscar@example.com", "email_verified": True}
        )
        assert response.status_code == 302
        assert invite.status == "accepted"
    finally:
        get_settings.cache_clear()


def test_no_allowlist_and_multi_tenant_keeps_open_signup(client, db, monkeypatch):
    """multi_tenant=True is the one combination the invitation requirement
    never applies to — a paying tenant's own IdP has already vetted who
    signs in."""
    monkeypatch.delenv("TRET_OIDC_ALLOWED_EMAIL_DOMAINS", raising=False)
    monkeypatch.setenv("TRET_MULTI_TENANT", "true")
    get_settings.cache_clear()
    try:
        response = _round_trip(
            client, claims={"sub": "sub-priya", "email": "priya@example.com"}
        )
        assert response.status_code == 302
        me = client.get("/api/auth/me")
        assert me.json()["workspaces"][0]["kind"] == "personal"
    finally:
        monkeypatch.delenv("TRET_MULTI_TENANT", raising=False)
        get_settings.cache_clear()


def test_an_already_linked_account_signs_in_despite_a_later_allowlist(client, db, monkeypatch):
    """The gate is a JIT-provisioning precondition, not a login precondition
    — outcome 1 (sub already on file) never re-checks it, so tightening the
    allowlist after the fact does not lock out an existing account."""
    sole = _workspace("Default")
    existing = _password_user("quinn@other.example", "correct-horse-6", oidc_sub="sub-quinn")
    db.add(sole)
    db.add(existing)
    db.add(WorkspaceMember(user_id=existing.id, workspace_id=sole.id, role="analyst"))
    monkeypatch.setenv("TRET_OIDC_ALLOWED_EMAIL_DOMAINS", "example.com")  # excludes other.example
    get_settings.cache_clear()
    try:
        response = _round_trip(client, claims={"sub": "sub-quinn", "email": "quinn@other.example"})
        assert response.status_code == 302
    finally:
        get_settings.cache_clear()


# ── invite redemption ─────────────────────────────────────────────────────────
def test_invite_redemption_lands_the_session_on_the_team_workspace(client, db):
    sole = _workspace("Default", kind="team")
    team = _workspace("Climate Co", kind="team")
    invite = _invite(workspace_id=team.id, email="Ivy@Example.com", role="approver")
    db.add(sole)
    db.add(team)
    db.add(invite)

    response = _round_trip(client, claims={"sub": "sub-ivy", "email": "ivy@example.com"})
    assert response.status_code == 302

    assert invite.status == "accepted"
    (membership,) = [m for m in db.store["WorkspaceMember"] if m.workspace_id == team.id]
    assert membership.role == "approver"

    me = client.get("/api/auth/me")
    body = me.json()
    assert {w["id"] for w in body["workspaces"]} == {str(sole.id), str(team.id)}
    # Landed on the redeemed team workspace, not the sole/default one.
    assert body["current_workspace_id"] == str(team.id)
    assert body["role"] == "approver"


def test_expired_invite_is_not_redeemed(client, db):
    sole = _workspace("Default", kind="team")
    team = _workspace("Climate Co", kind="team")
    invite = _invite(workspace_id=team.id, email="jan@example.com", role="approver", hours=-1)
    db.add(sole)
    db.add(team)
    db.add(invite)

    _round_trip(client, claims={"sub": "sub-jan", "email": "jan@example.com"})

    assert invite.status == "pending"  # untouched
    me = client.get("/api/auth/me")
    assert len(me.json()["workspaces"]) == 1  # sole workspace only


def test_a_seat_gate_blocking_redemption_leaves_the_invite_pending_and_still_logs_in(client, db):
    """The Phase C wiring identity.py's TODO used to mark: a blocked
    `check_workspace_gate(..., "invite_redeem")` skips that invite — no
    membership, invite left pending — without failing the login itself."""
    from tret.engine import extensions as extensions_module
    from tret.engine.extensions import ExtensionAPI, GateResult

    sole = _workspace("Default", kind="team")
    team = _workspace("Climate Co", kind="team")
    invite = _invite(workspace_id=team.id, email="kelly@example.com", role="approver")
    db.add(sole)
    db.add(team)
    db.add(invite)

    ext = ExtensionAPI(None)

    async def veto(ext_db, workspace_id, action):
        assert action == "invite_redeem"
        assert workspace_id == team.id
        return GateResult(allowed=False, reason="seat_limit", detail="no seats left")

    ext.add_workspace_gate(veto)
    extensions_module._registry = ext
    try:
        response = _round_trip(client, claims={"sub": "sub-kelly", "email": "kelly@example.com"})
        assert response.status_code == 302  # login still succeeds

        assert invite.status == "pending"  # left untouched, not consumed
        me = client.get("/api/auth/me")
        body = me.json()
        assert len(body["workspaces"]) == 1  # sole workspace only — team was blocked
        assert body["workspaces"][0]["id"] == str(sole.id)
    finally:
        extensions_module._registry = None


# ── auth_mode gating ──────────────────────────────────────────────────────────
def test_password_login_is_disabled_in_oidc_mode(client, monkeypatch):
    monkeypatch.setenv("TRET_AUTH_MODE", "oidc")
    get_settings.cache_clear()
    try:
        response = client.post(
            "/api/auth/login", json={"email": "anyone@example.com", "password": "whatever1"}
        )
        assert response.status_code == 403
        assert "single sign-on" in response.json()["detail"].lower()
    finally:
        get_settings.cache_clear()


def test_password_login_still_works_in_both_mode(client, db, monkeypatch):
    sole = _workspace("Default")
    user = _password_user("kim@example.com", "correct-horse-5")
    db.add(sole)
    db.add(user)
    db.add(WorkspaceMember(user_id=user.id, workspace_id=sole.id, role="analyst"))

    monkeypatch.setenv("TRET_AUTH_MODE", "both")
    get_settings.cache_clear()
    try:
        response = client.post(
            "/api/auth/login", json={"email": "kim@example.com", "password": "correct-horse-5"}
        )
        assert response.status_code == 200
    finally:
        get_settings.cache_clear()


# ── GET /api/auth/config ──────────────────────────────────────────────────────
def test_auth_config_password_only(client, monkeypatch):
    monkeypatch.delenv("TRET_OIDC_ISSUER", raising=False)
    monkeypatch.delenv("TRET_OIDC_CLIENT_ID", raising=False)
    monkeypatch.delenv("TRET_OIDC_CLIENT_SECRET", raising=False)
    get_settings.cache_clear()
    try:
        body = client.get("/api/auth/config").json()
        assert body == {"auth_mode": "password", "oidc_configured": False, "oidc_login_url": None}
    finally:
        get_settings.cache_clear()


def test_auth_config_oidc_configured(client, monkeypatch):
    monkeypatch.setenv("TRET_AUTH_MODE", "oidc")
    get_settings.cache_clear()
    try:
        body = client.get("/api/auth/config").json()
        assert body == {
            "auth_mode": "oidc",
            "oidc_configured": True,
            "oidc_login_url": "/api/auth/oidc/login",
        }
    finally:
        get_settings.cache_clear()


def test_auth_config_both_mode(client, monkeypatch):
    monkeypatch.setenv("TRET_AUTH_MODE", "both")
    get_settings.cache_clear()
    try:
        body = client.get("/api/auth/config").json()
        assert body["auth_mode"] == "both"
        assert body["oidc_configured"] is True
        assert body["oidc_login_url"] == "/api/auth/oidc/login"
    finally:
        get_settings.cache_clear()


def test_auth_config_issuer_without_client_credentials_is_not_configured(client, monkeypatch):
    monkeypatch.delenv("TRET_OIDC_CLIENT_SECRET", raising=False)
    get_settings.cache_clear()
    try:
        body = client.get("/api/auth/config").json()
        assert body["oidc_configured"] is False
        assert body["oidc_login_url"] is None
    finally:
        get_settings.cache_clear()


# ── open redirect ─────────────────────────────────────────────────────────────
def test_an_absolute_next_url_is_rejected(client):
    state, _nonce = _login_redirect(client, next_path="https://evil.example.com/steal")
    unpacked = oidc._state_serializer().loads(state)
    assert unpacked["next"] == "/"


def test_a_protocol_relative_next_url_is_rejected(client):
    state, _nonce = _login_redirect(client, next_path="//evil.example.com/steal")
    unpacked = oidc._state_serializer().loads(state)
    assert unpacked["next"] == "/"


def test_a_same_origin_next_path_is_preserved(client):
    state, _nonce = _login_redirect(client, next_path="/reports/42")
    unpacked = oidc._state_serializer().loads(state)
    assert unpacked["next"] == "/reports/42"
