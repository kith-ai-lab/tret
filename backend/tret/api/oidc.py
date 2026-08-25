"""Generic OIDC login: `GET /login` starts the authorization-code + PKCE
dance, `GET /callback` finishes it. Mounted in `main.py` only when
`settings.oidc_issuer` is set — every self-hosted deployment never imports
this module's router at all.

Deliberately never an Auth0 SDK: authlib's issuer-agnostic building blocks
(`authlib.jose` for the id_token, plain discovery + token exchange over
`tret.net`) so any spec-compliant IdP works, and the one Auth0-specific
surface (the `/v2/logout` fallback) is isolated and commented where it lives,
in `api/auth.py::_oidc_logout_url`.

**No server-side session for the OAuth dance itself.** tret has no session
store to put one in (see `api/auth.py`'s module docstring), so the nonce,
PKCE verifier and the post-login redirect target travel in a signed,
timestamped `state` parameter (`itsdangerous`, its own salt, its own short
`max_age`) instead of a server-side row. Anyone can *read* `state` off the
URL — it round-trips through the browser and the IdP in the clear — so it
must never carry anything an attacker reading it could use; a signature is
what stops them *writing* one that verifies. The nonce itself travels in two
forms for that reason (see `_login` below): raw, to the IdP, where the id_
token must echo it back; hashed, in `state`, so the callback can check the
echo without repeating the raw value across two different unauthenticated
channels.

**`state`'s signature alone does not bind it to a browser.** A signed
`state` only proves *tret* minted it at some point — not that it was minted
*for the browser presenting it*. Without more, an attacker can start their
own login (getting back a validly-signed `state` plus a `code` for their own
IdP account), then hand that `(code, state)` pair to a victim — e.g. embedded
as the query string of a link the victim clicks. The victim's browser
carries no cookie the callback checks, so nothing distinguishes "the browser
that started this login" from "any browser that received the URL": the
callback happily logs the victim into the attacker's account (session
fixation/planting), or, worse, replays a code that ends up linking the
attacker's IdP identity to whatever the victim's browser was already
authenticated as. The fix is a random per-attempt `sid`, minted in `_login`,
carried in *two* independent channels that must agree: inside the signed
`state` (so it can't be forged) and in a short-lived, `httpOnly` cookie
scoped to this router's own path (so it can only be presented by the same
browser `_login` set it in — an attacker relaying a URL cannot also plant
their target's cookie jar). `_callback` requires the two to match before
doing anything else, the same "fail before any expensive or externally
observable work" ordering `nonce_hash`/`code_verifier` already follow, and
clears the cookie once the login completes so a captured, already-used URL
cannot be replayed a second time even by the browser that legitimately
started it.

**Egress.** Every outbound call here (discovery, JWKS, token exchange) goes
through `tret.net.build_client` with a policy scoped to that one call's own
URL — never bare `httpx` (see `tret/net/client.py`'s module docstring and
`tests/test_egress_chokepoint.py`, which fails the suite on anything that
opens a connection outside `tret/net/`). This is deliberately *not* one of
the five operator-facing egress classes `GET /api/settings/egress` reports:
those switch a model's ability to reach the wider internet, and this is a
fixed, operator-configured destination (the IdP an admin registered tret with
— nobody's document text ever chooses this URL) with nothing to switch
independently of "is this deployment on the internet at all", which is
exactly what the master switch (`TRET_EGRESS`) already answers.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import time
from urllib.parse import urlencode, urlsplit

import httpx
from authlib.jose import JsonWebKey, jwt
from authlib.jose.errors import JoseError
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy.ext.asyncio import AsyncSession

from tret.api.auth import _issue_session
from tret.config import get_settings
from tret.db.engine import get_db
from tret.net import EgressDenied, build_client
from tret.net.policy import MODE_OFF, MODE_ON, VERIFY_NONE, ClassPolicy
from tret.services.identity import match_or_provision

router = APIRouter(prefix="/api/auth/oidc", tags=["auth"])

STATE_SALT = "tret-oidc-state"
STATE_MAX_AGE = 600  # 10 minutes: long enough for a login page, short enough that a captured URL is stale fast
SID_COOKIE = "tret_oidc_sid"  # binds `state` to the browser that started this login — see module docstring
_EGRESS_CLASS = "oidc"  # audit-log label only — see module docstring for why this is not one of EGRESS_CLASSES

# Cached module-level: an issuer's discovery document and JWKS almost never
# change, and fetching either on every login would make every sign-in pay for
# a round trip nothing about the request needs. Per-process, like every other
# in-memory cache in this codebase (tret/api/auth.py's login limiter, tret/net's
# runtime egress overrides) — correct for the single-worker deployment tret
# ships (docs/hardening.md), and a restart just refetches.
_CACHE_TTL_SECONDS = 3600.0
_discovery_cache: dict[str, tuple[float, dict]] = {}
_jwks_cache: dict[str, tuple[float, dict]] = {}


def _state_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().secret_key, salt=STATE_SALT)


def _discovery_url(issuer: str) -> str:
    issuer = issuer.strip()
    if not issuer.startswith(("http://", "https://")):
        issuer = f"https://{issuer}"
    return issuer.rstrip("/") + "/.well-known/openid-configuration"


def _oidc_policy(url: str) -> ClassPolicy:
    """A one-URL allowlist scoped to whatever host this particular call is
    reaching — see the module docstring's Egress section for why this is not
    a `TRET_EGRESS_*`-switchable class. `VERIFY_NONE`: this is a destination
    named by operator config (the issuer) or by that same issuer's own
    discovery document, never by a document a user uploaded or a model chose,
    so the SSRF address-resolution check `research` needs does not apply —
    the same reasoning `tret/net/policy.py` already gives for `local` and
    `search`.
    """
    host = (urlsplit(url).hostname or "").lower()
    return ClassPolicy(
        name=_EGRESS_CLASS,
        mode=MODE_OFF if get_settings().egress.strip().lower() == "off" else MODE_ON,
        allow_hosts=frozenset({host}) if host else frozenset(),
        allow_http=False,
        standard_ports_only=True,
        verify_addresses=VERIFY_NONE,
        max_bytes=0,
        timeout_seconds=15.0,
    )


async def _get_json(url: str) -> dict:
    try:
        async with build_client(_EGRESS_CLASS, policy=_oidc_policy(url), timeout=15.0) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.json()
    except EgressDenied as exc:
        raise HTTPException(503, f"OIDC login is unavailable: {exc}") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Could not reach the identity provider: {exc}") from exc


async def _discovery(issuer: str) -> dict:
    now = time.monotonic()
    cached = _discovery_cache.get(issuer)
    if cached is not None and now - cached[0] < _CACHE_TTL_SECONDS:
        return cached[1]
    metadata = await _get_json(_discovery_url(issuer))
    for required in ("authorization_endpoint", "token_endpoint", "jwks_uri", "issuer"):
        if not metadata.get(required):
            raise HTTPException(
                502, f"The identity provider's discovery document is missing '{required}'"
            )
    _discovery_cache[issuer] = (now, metadata)
    return metadata


async def _jwks(jwks_uri: str) -> dict:
    now = time.monotonic()
    cached = _jwks_cache.get(jwks_uri)
    if cached is not None and now - cached[0] < _CACHE_TTL_SECONDS:
        return cached[1]
    keys = await _get_json(jwks_uri)
    _jwks_cache[jwks_uri] = (now, keys)
    return keys


def _redirect_uri(request: Request) -> str:
    """The callback URL registered with the IdP — see config.py's
    `oidc_redirect_url` docstring for why the explicit setting wins."""
    explicit = get_settings().oidc_redirect_url.strip()
    if explicit:
        return explicit
    return str(request.url_for("oidc_callback"))


def _safe_next(path: str | None) -> str:
    """Only a same-origin relative path may be redirected to after login —
    never an absolute URL or a protocol-relative one (`//evil.example`,
    which browsers resolve against the *scheme*, not the host, so it is an
    open redirect too). Anything else falls back to `/` silently rather than
    erroring: `next` is a convenience, not something worth failing a login
    over.
    """
    if not path or not path.startswith("/") or path.startswith("//") or path.startswith("/\\"):
        return "/"
    return path


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


@router.get("/login")
async def oidc_login(request: Request, next: str = Query(default="/")):
    settings = get_settings()
    if not (settings.oidc_issuer and settings.oidc_client_id):
        raise HTTPException(503, "OIDC login is not configured")

    metadata = await _discovery(settings.oidc_issuer)

    nonce = secrets.token_urlsafe(24)
    nonce_hash = hashlib.sha256(nonce.encode()).hexdigest()
    code_verifier = secrets.token_urlsafe(64)  # 86 chars: within PKCE's 43-128 bound
    code_challenge = _b64url(hashlib.sha256(code_verifier.encode()).digest())
    sid = secrets.token_urlsafe(24)  # binds `state` to this browser — see module docstring

    state = _state_serializer().dumps(
        {
            "nonce_hash": nonce_hash,
            "code_verifier": code_verifier,
            "next": _safe_next(next),
            "sid": sid,
        }
    )

    params = {
        "response_type": "code",
        "client_id": settings.oidc_client_id,
        "redirect_uri": _redirect_uri(request),
        "scope": "openid profile email",
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    response = RedirectResponse(f"{metadata['authorization_endpoint']}?{urlencode(params)}")
    response.set_cookie(
        SID_COOKIE,
        sid,
        max_age=STATE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/api/auth/oidc",
    )
    return response


@router.get("/callback")
async def oidc_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    settings = get_settings()
    if not (settings.oidc_issuer and settings.oidc_client_id):
        raise HTTPException(503, "OIDC login is not configured")
    if error:
        raise HTTPException(401, f"The identity provider refused the login: {error_description or error}")
    if not code or not state:
        raise HTTPException(401, "Missing code or state")

    try:
        unpacked = _state_serializer().loads(state, max_age=STATE_MAX_AGE)
    except BadSignature:
        raise HTTPException(401, "Login session expired or was tampered with — try signing in again")
    if not isinstance(unpacked, dict):
        raise HTTPException(401, "Invalid login state")
    nonce_hash = unpacked.get("nonce_hash")
    code_verifier = unpacked.get("code_verifier")
    next_path = _safe_next(unpacked.get("next"))
    if not nonce_hash or not code_verifier:
        raise HTTPException(401, "Invalid login state")

    # `state`'s signature proves tret minted it; it does not prove this
    # browser is the one `_login` minted it for. The `sid` cookie `_login`
    # set (httpOnly, scoped to this router) is the second, independent
    # channel that does — see the module docstring. Checked before any
    # external call so a replayed (code, state) from a different browser
    # fails immediately rather than after a wasted token exchange.
    sid = unpacked.get("sid")
    sid_cookie = request.cookies.get(SID_COOKIE)
    if not sid or not sid_cookie or not secrets.compare_digest(sid_cookie, sid):
        raise HTTPException(401, "Missing or mismatched login cookie — try signing in again")

    metadata = await _discovery(settings.oidc_issuer)

    try:
        async with build_client(
            _EGRESS_CLASS, policy=_oidc_policy(metadata["token_endpoint"]), timeout=15.0
        ) as client:
            token_response = await client.post(
                metadata["token_endpoint"],
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": _redirect_uri(request),
                    "client_id": settings.oidc_client_id,
                    "client_secret": settings.oidc_client_secret,
                    "code_verifier": code_verifier,
                },
                headers={"Accept": "application/json"},
            )
            token_response.raise_for_status()
    except EgressDenied as exc:
        raise HTTPException(503, f"OIDC login is unavailable: {exc}") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(401, f"Could not exchange the authorization code: {exc}") from exc

    id_token = token_response.json().get("id_token")
    if not id_token:
        raise HTTPException(401, "The identity provider did not return an id_token")

    jwks = await _jwks(metadata["jwks_uri"])
    key_set = JsonWebKey.import_key_set(jwks)
    try:
        claims = jwt.decode(
            id_token,
            key_set,
            claims_options={
                "iss": {"essential": True, "values": [metadata["issuer"]]},
                "aud": {"essential": True, "values": [settings.oidc_client_id]},
            },
        )
        claims.validate()  # exp/iat/iss/aud — everything but nonce, checked by hand below
    except JoseError as exc:
        raise HTTPException(401, f"Could not verify the identity provider's token: {exc}") from exc

    token_nonce = claims.get("nonce")
    if not token_nonce or hashlib.sha256(str(token_nonce).encode()).hexdigest() != nonce_hash:
        raise HTTPException(401, "Invalid nonce — this login attempt may have been replayed")

    sub = claims.get("sub")
    email = claims.get("email")
    if not sub or not email:
        raise HTTPException(401, "The identity provider's token is missing sub or email")
    email_verified = bool(claims.get("email_verified", False))
    display_name = claims.get("name") or ""

    user, wid = await match_or_provision(
        db, sub=sub, email=email, email_verified=email_verified, display_name=display_name
    )

    response = RedirectResponse(next_path, status_code=302)
    _issue_session(response, user, wid=wid)
    response.delete_cookie(SID_COOKIE, path="/api/auth/oidc")
    return response
