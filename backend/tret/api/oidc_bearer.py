"""OIDC bearer-token authentication: a second, opt-in way into the API
alongside `api/auth.py`'s `tret_session` cookie. A non-interactive caller
already holding an OIDC *access* token — e.g. an external admin console
acting on behalf of a signed-in human — presents it as `Authorization:
Bearer <jwt>` instead of a cookie. Disabled by default: `current_user`
(api/auth.py) only reaches this module at all once `settings.oidc_api_audience`
is set, so every self-hosted deployment that doesn't opt in is unaffected.

**Reuses `api/oidc.py`'s discovery/JWKS fetch, cache, and algorithm
filtering rather than re-deriving any of it.** That module's `_discovery`/
`_jwks` already cache per-issuer and go through the egress chokepoint, and
`_signing_algorithms` already excludes HMAC algorithms so a JWKS (always
asymmetric public keys) can never be asked to verify an HS*-signed token —
the alg-confusion defence OIDC Core 3.1.3.7 #8 calls for. Hand-rolling that
here, even to verify a differently-audienced token, would be exactly how
that protection quietly stops applying to one of the two ways in.

**No JIT provisioning.** `services/identity.py::match_or_provision` creates
a `User` on first sign-in because a human just proved control of a verified
email through the IdP's own login UI — the interactive OIDC callback *is*
that proof. A bearer token arriving at an API endpoint is not: it proves
only that some access token exists for some `sub`, not that anyone owns the
email tret would otherwise trust. Minting an account from that would turn a
leaked or overscoped API token into an account factory, so an unresolved
`sub` is a 401, full stop, `oidc_allowed_email_domains`/`multi_tenant`
notwithstanding — a bearer caller must already have a linked account, the
same way `match_or_provision`'s own outcome 1 (sub already on file) works,
without outcomes 2/3 (email-link, JIT-provision) that presuppose a human
just came through the login screen.

**Admin elevation never touches the database.** A verified token whose
roles claim contains `oidc_admin_role` is recorded on `request.state`
(`oidc_admin`), not on the `User` ORM object — see `authenticate_bearer`'s
docstring for why setting `user.role` directly is the wrong shortcut here.
"""
from __future__ import annotations

from authlib.jose import JsonWebKey, JsonWebToken, jwt
from authlib.jose.errors import JoseError
from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tret.config import Settings
from tret.db.models import User


def bearer_auth_enabled(settings: Settings) -> bool:
    """Whether `current_user` should even look at an `Authorization` header.
    A single, empty-by-default switch — see this module's docstring."""
    return bool(settings.oidc_api_audience.strip())


def _roles_from_claims(claims: dict, roles_claim: str) -> list[str]:
    """The caller's roles, from whichever claim `oidc_roles_claim` names.

    Tolerates a single bare string (a token with exactly one role) alongside
    the list shape the setting's docstring documents as the expected one;
    anything else (missing claim, wrong type) is just "no roles" rather than
    a validation error — a malformed roles claim should cost the caller
    admin elevation, not authentication itself.
    """
    if not roles_claim:
        return []
    value = claims.get(roles_claim)
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value]
    return []


def _check_bearer_client(claims: dict, settings: Settings) -> None:
    """Enforce `settings.oidc_bearer_client_ids`, or raise `HTTPException(401)`.

    The interactive callback (api/oidc.py::oidc_callback) pins an id_token's
    `azp` to `oidc_client_id` — a fixed value it already knows, because tret
    itself is the only party that should ever complete that flow. A bearer
    access token has no such fixed expectation: it was minted by whatever
    application asked the IdP for one against `oidc_api_audience`, and
    without this setting nothing here distinguishes "an application tret's
    operator actually registered" from "any application in the same tenant
    the IdP admin separately authorized for this audience" — the latter can
    mint a token this endpoint accepts just as readily, admin role claim and
    all. See `oidc_bearer_client_ids`'s docstring in config.py for why empty
    means trusting every client in the tenant rather than failing closed.
    """
    allowed = [c.strip() for c in settings.oidc_bearer_client_ids.split(",") if c.strip()]
    if not allowed:
        return
    # `azp` (authorized party) is the standard claim for this; not every IdP
    # emits it on an access token, so `client_id` — the claim several major
    # IdPs (Auth0 included) send instead — is the fallback, never the other
    # way around: an `azp` present and simply wrong must not be excused by a
    # `client_id` that happens to also be on the token.
    client = claims.get("azp") or claims.get("client_id")
    if not client or client not in allowed:
        raise HTTPException(401, "Not authenticated")


async def _verify_access_token(token: str, settings: Settings) -> dict:
    """Verify `token` as an OIDC access token for `settings.oidc_issuer` /
    `settings.oidc_api_audience`, or raise `HTTPException(401)`.

    Same shape as `api/oidc.py::oidc_callback`'s id_token verification
    (discovery -> JWKS -> algorithm-pinned decode -> `claims.validate()`),
    reusing that module's cache and algorithm filtering rather than
    duplicating it. The one difference is the audience: an access token's
    `aud` is checked against `oidc_api_audience` (the API identifier), never
    `oidc_client_id` (which names the login *client*, a different party).
    `validate_aud` (authlib) already handles `aud` being either a single
    string or an array and checks membership either way, so no separate
    array/string branch is needed here.
    """
    # Imported lazily, not at module load: `oidc.py` imports `api/auth.py`'s
    # `_issue_session`, and `api/auth.py::current_user` reaches this module —
    # a top-level `from tret.api.oidc import ...` here would make loading
    # `auth` require a not-yet-finished `auth` module by way of `oidc`. By
    # call time (an actual request) both modules are already fully loaded.
    from tret.api.oidc import _discovery, _jwks, _signing_algorithms

    if not settings.oidc_issuer:
        # A bearer audience configured with no issuer to verify it against
        # is a misconfiguration, not a request that could ever succeed —
        # fails the same as any other unverifiable token, never revealing
        # which.
        raise HTTPException(401, "Not authenticated")
    metadata = await _discovery(settings.oidc_issuer)
    jwks = await _jwks(metadata["jwks_uri"])
    allowed_algs = _signing_algorithms(metadata)
    token_verifier = jwt if allowed_algs is None else JsonWebToken(allowed_algs)
    try:
        # `import_key_set` belongs inside this `try` alongside `.decode()`,
        # not before it: a `jwks_uri` returning something malformed (missing
        # "keys", a key missing "kty", ...) makes authlib raise a bare
        # `ValueError`/`KeyError` building the key set, before verification
        # even starts. That document is already cached for `_CACHE_TTL_SECONDS`
        # by `_jwks` above, so outside this guard it would be an unhandled 500
        # on every bearer request for up to an hour, not just this one.
        key_set = JsonWebKey.import_key_set(jwks)
        claims = token_verifier.decode(
            token,
            key_set,
            claims_options={
                "iss": {"essential": True, "values": [metadata["issuer"]]},
                "aud": {"essential": True, "values": [settings.oidc_api_audience]},
                "exp": {"essential": True},
            },
        )
        claims.validate()
    except (JoseError, KeyError, ValueError, TypeError) as exc:
        # Not just `JoseError` — see api/oidc.py::oidc_callback's identical
        # `except` clause: authlib's own internals raise a bare `KeyError`
        # trying to coerce this asymmetric JWKS into an HMAC secret for an
        # HS*-headed token, and that must come back as a 401 (untrusted
        # token, rejected), never an unhandled 500. Never echoes `exc` or
        # any token contents back to the caller.
        raise HTTPException(401, "Could not verify the bearer token") from exc
    return dict(claims)


async def authenticate_bearer(
    request: Request, token: str, db: AsyncSession, settings: Settings
) -> User:
    """Resolve a verified bearer token to an existing, active `User`.

    Called from `api/auth.py::current_user` only when there is no session
    cookie, `bearer_auth_enabled(settings)` is true, and an `Authorization:
    Bearer` header was actually presented — the cookie path is untouched by
    this module's existence otherwise.
    """
    claims = await _verify_access_token(token, settings)
    _check_bearer_client(claims, settings)
    sub = claims.get("sub")
    if not sub:
        raise HTTPException(401, "Not authenticated")

    # Match only — deliberately not `services/identity.py::match_or_provision`.
    # See this module's docstring for why a bearer token never creates or
    # links an account: an unresolved `sub` is unconditionally a 401.
    user = (await db.execute(select(User).where(User.oidc_sub == sub))).scalar_one_or_none()
    if user is None:
        raise HTTPException(401, "Not authenticated")
    # Same check, same message, as api/auth.py::current_user's cookie path —
    # a deactivated account is refused identically regardless of which door
    # it came through.
    if user.disabled:
        raise HTTPException(401, "Account deactivated")

    if settings.oidc_admin_role:
        roles = _roles_from_claims(claims, settings.oidc_roles_claim)
        if settings.oidc_admin_role in roles:
            # `request.state`, never `user.role`: `user` is attached to `db`
            # (an `AsyncSession`), and SQLAlchemy's autoflush would turn
            # `user.role = "admin"` into a real, committed privilege change
            # on the very next query this request happens to run — a write
            # to the database as a side effect of authenticating a read.
            # This is exactly the kind of thing someone will later try to
            # "simplify" by setting the attribute directly; don't.
            request.state.oidc_admin = True

    return user
