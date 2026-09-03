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
from authlib.jose import JsonWebKey, JsonWebToken, jwt
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

# authlib's own default `jwt` (imported above) accepts this fixed set — never
# "none" (OIDC Core 3.1.3.7 #1: an id_token MUST be signed; there is no
# registered "none" JWS algorithm here to accept even if an IdP's discovery
# document listed it). Used as the fallback when an issuer's discovery
# document does not advertise `id_token_signing_alg_values_supported` (that
# field is optional in the discovery spec even though every real IdP sends
# it), and as the full universe `_ASYMMETRIC_ALGS` below is drawn from.
_AUTHLIB_SUPPORTED_ALGS = frozenset(
    {
        "HS256", "HS384", "HS512",
        "RS256", "RS384", "RS512",
        "ES256", "ES256K", "ES384", "ES512",
        "PS256", "PS384", "PS512",
        "EdDSA",
    }
)

# The subset of the above this module will ever actually pin to. `jwks` here
# is always a public-key set (an IdP's JWKS endpoint never publishes an HMAC
# secret), so an HS* algorithm is never legitimate against it — selecting one
# would mean handing authlib a public asymmetric key and asking it to treat
# those bytes as a symmetric secret, which is not a real verification path,
# only a crash waiting to happen (see the `except` in `_callback` around the
# `token_verifier.decode` call). Dropped here, at the universe every alg list
# is filtered against, rather than trusted to an issuer's discovery document
# — or an attacker's — to never advertise "HS256" alongside the real ones.
_ASYMMETRIC_ALGS = frozenset(alg for alg in _AUTHLIB_SUPPORTED_ALGS if not alg.startswith("HS"))


def _signing_algorithms(metadata: dict) -> list[str] | None:
    """Which JWS `alg` values an id_token from this issuer may use, per its
    own discovery document — the algorithm-confusion defence OIDC Core
    3.1.3.7 #8 asks for: an attacker able to influence the header (e.g. by
    getting a relying party to accept any registered algorithm) should not
    be able to pick a scheme the issuer never advertised. `None` means the
    issuer didn't advertise the (optional) field at all, in which case the
    caller falls back to authlib's own default algorithm set rather than
    failing every IdP that omits it.
    """
    supported = metadata.get("id_token_signing_alg_values_supported")
    if not supported:
        return None
    # Filtered through the asymmetric subset of what authlib supports (never
    # HS*/symmetric — see `_ASYMMETRIC_ALGS`), and "none" dropped explicitly
    # even though it was never in that set — defence in depth against a
    # future authlib version registering it.
    return [alg for alg in supported if alg in _ASYMMETRIC_ALGS and alg != "none"]


def _state_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().secret_key, salt=STATE_SALT)


def _discovery_url(issuer: str) -> str:
    issuer = issuer.strip()
    if not issuer.startswith(("http://", "https://")):
        issuer = f"https://{issuer}"
    return issuer.rstrip("/") + "/.well-known/openid-configuration"


def _normalized_issuer(issuer: str) -> tuple[str, str, str]:
    """(scheme, host, path) for comparing two issuer strings per OIDC Core
    4.3: the discovery document's `issuer` must equal the issuer URL it was
    fetched from. A bare trailing slash is not a real difference (`_discovery_url`
    strips one to build the fetch URL in the first place), so it is stripped
    here too, on both sides, before anything else. Scheme and path compare
    case-sensitively, as the spec's URL-equality rule does; the host does not,
    since DNS names are case-insensitive."""
    issuer = issuer.strip()
    if not issuer.startswith(("http://", "https://")):
        issuer = f"https://{issuer}"
    if issuer.endswith("/"):
        issuer = issuer[:-1]
    parsed = urlsplit(issuer)
    return (parsed.scheme, parsed.netloc.lower(), parsed.path)


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
        # Not just truthiness: a discovery document is attacker- or
        # misconfiguration-controlled JSON, and `_normalized_issuer` below
        # calls `.strip()` on this value unconditionally. A non-string here
        # (e.g. an IdP that serves `"issuer": 12345`) must fail the same way
        # a missing field does (502), not blow up as an unhandled 500.
        value = metadata.get(required)
        if not isinstance(value, str) or not value:
            raise HTTPException(
                502, f"The identity provider's discovery document is missing '{required}'"
            )
    # OIDC Core 4.3: the discovery document's `issuer` MUST equal the issuer
    # URL it was fetched from — presence alone (checked above) is not enough,
    # because the `iss` claim value this module trusts at id_token-verification
    # time (below, in `_callback`) comes straight from this field. Without this
    # check, an IdP (or a document served from an unexpected host on the way
    # to it) could claim to be a different issuer than the one an operator
    # configured, and that claim would be trusted uncritically. Not cached on
    # mismatch: caching a bad document would keep serving the mismatch for the
    # full TTL even after the underlying problem is fixed.
    if _normalized_issuer(metadata["issuer"]) != _normalized_issuer(issuer):
        raise HTTPException(
            502,
            "The identity provider's discovery document issuer does not match "
            "TRET_OIDC_ISSUER",
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
    # Pinned to the issuer's own advertised algorithms when it advertises
    # any (see `_signing_algorithms`); otherwise the same fixed default set
    # authlib's shared `jwt` object already accepts. Passing `algorithms`
    # makes authlib reject a header `alg` outside this set — including
    # "none" — before it ever looks at the key, rather than after a
    # signature it might (with the wrong key type) still appear to verify.
    allowed_algs = _signing_algorithms(metadata)
    token_verifier = jwt if allowed_algs is None else JsonWebToken(allowed_algs)
    try:
        claims = token_verifier.decode(
            id_token,
            key_set,
            claims_options={
                "iss": {"essential": True, "values": [metadata["issuer"]]},
                "aud": {"essential": True, "values": [settings.oidc_client_id]},
                # authlib's own `validate_exp` silently no-ops when "exp" is
                # simply absent from the payload (it only checks the value
                # *if* the key is present) — `essential` forces
                # `_validate_essential_claims` to reject a token missing it
                # outright, ahead of every other check.
                "exp": {"essential": True},
            },
        )
        claims.validate()  # exp/iat/iss/aud — everything but nonce, checked by hand below
    except (JoseError, KeyError, ValueError, TypeError) as exc:
        # Not just `JoseError`: authlib's own internals raise plain
        # `KeyError`/`ValueError`/`TypeError` — not a `JoseError` subclass —
        # when a header `alg` and the key material fundamentally don't match
        # in kind, rather than failing the signature check cleanly. Repro: an
        # HS256-headed token verified against this `key_set`, which (being a
        # JWKS) holds only asymmetric public keys — authlib tries to coerce
        # a public key into an HMAC secret and blows up in its own
        # key-preparation code with a bare `KeyError`. That must still surface
        # as a 401 (untrusted token, rejected), not an unhandled 500.
        raise HTTPException(401, f"Could not verify the identity provider's token: {exc}") from exc

    # OIDC Core 3.1.3.7 #4/#5: an id_token naming more than one audience must
    # carry `azp` (authorized party) naming *us*, so a token an attacker
    # legitimately obtained for some other audience — which also lists our
    # client_id, e.g. because that other party requested it — cannot be
    # replayed here. A single-audience token has nothing to disambiguate, but
    # the spec still requires `azp`, when present at all, to name us — an
    # `azp` naming some other party is itself evidence the token was minted
    # for someone else, so it is checked whenever the claim is present, not
    # only when a multi-valued `aud` forces the issue.
    aud = claims.get("aud")
    azp = claims.get("azp")
    if isinstance(aud, list) and len(aud) > 1:
        if not azp or azp != settings.oidc_client_id:
            raise HTTPException(401, "Token audience is ambiguous — missing or mismatched azp")
    elif azp is not None and azp != settings.oidc_client_id:
        raise HTTPException(401, "Token azp does not match this client")

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
