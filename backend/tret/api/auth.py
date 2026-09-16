"""Local auth v1: email/password (argon2) + signed httpOnly session cookie.

Kept behind small dependencies (`current_user`, `require_admin`) so an OIDC
implementation can replace this module without touching other routers.

**Credential lifecycle.** Sessions are stateless signed cookies, so there is no
session table to delete rows from. Revocation is instead bound to a fingerprint
carried in the cookie (`credential_version`), refused by `current_user` the
moment it no longer matches. Two independent things can move that fingerprint:

* the password hash — changed by a self-service change (`POST /api/auth/password`)
  or an admin rotation (`POST /api/auth/users/{id}/password`);
* `session_epoch` — bumped explicitly by `POST /api/auth/users/{id}/revoke-sessions`,
  for ending every *cookie* session an account holds *without* touching its
  password (and the only lever an OIDC-only account, which has no password
  hash at all, will ever have).

**This story is cookie-only.** `session_epoch` is an opaque counter (see
`db/models.py`'s `User.session_epoch`) with no time semantics — nothing on a
bearer access token can be compared against it, unlike, say, an `iat` against
a stored timestamp. A bearer caller (`api/oidc_bearer.py::authenticate_bearer`)
is therefore not covered by either lever above: it checks only `sub` and
`disabled`, so an access token already issued keeps working, unaffected by a
password change *or* a revoke-sessions call, until its own `exp`. That is a
real gap between what this module's revocation story promises and what it
delivers for that door — see `revoke_sessions`'s docstring — and the fix, if
tret ever needs one, is short-lived access tokens plus IdP-side revocation
(e.g. Auth0's client-grant/refresh-token revocation), not a mapping invented
here between an opaque counter and a token that carries no equivalent field.

Deactivation (`POST /api/auth/users/{id}/deactivate`) is a separate signal,
`User.disabled`, checked before the fingerprint at all: the account's password
is kept (a later `POST /api/auth/users/{id}/password` both restores access and
rotates the credential, exactly as it always has), but no cookie for it —
however fresh — is honoured while it is set. Grep this module for `disabled`
if a "deactivated" or "active" check ever needs to move: `password_hash is
None` stopped being that signal the day this was written, and this is a very
easy check to reintroduce by accident.

This is what docs/hardening.md's instruction to rotate credentials after an
incident needs in order to mean anything.

Login is rate limited by a small in-memory sliding window. It is per-process —
correct for the single-worker deployment tret ships, and a speed bump rather
than a defence against a distributed attacker. Put a WAF/proxy limit in front for
anything internet-facing.
"""
from __future__ import annotations

import hashlib
import time
import uuid
from urllib.parse import quote

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tret.api.oidc_bearer import authenticate_bearer, bearer_auth_enabled
from tret.config import Settings, get_settings
from tret.db.engine import get_db
from tret.db.models import User, Workspace, WorkspaceMember

router = APIRouter(prefix="/api/auth", tags=["auth"])

_hasher = PasswordHasher()
SESSION_COOKIE = "tret_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 14  # 14 days


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().secret_key, salt="tret-session")


class SlidingWindowLimiter:
    """Fixed-cost in-memory sliding window: at most `limit` hits per `window`.

    Bounded by pruning empty keys on every check, so a spray of distinct keys
    cannot grow the map without also aging out.
    """

    def __init__(self) -> None:
        self._hits: dict[str, list[float]] = {}

    def _prune(self, now: float, window: float) -> None:
        for key in list(self._hits):
            recent = [t for t in self._hits[key] if now - t < window]
            if recent:
                self._hits[key] = recent
            else:
                del self._hits[key]

    def check(self, key: str, limit: int, window: float, *, now: float | None = None) -> float:
        """Seconds to wait before another attempt is allowed (0.0 = allowed now)."""
        now = time.monotonic() if now is None else now
        self._prune(now, window)
        hits = self._hits.get(key, [])
        if len(hits) < limit:
            return 0.0
        return max(0.0, window - (now - hits[0]))

    def record(self, key: str, *, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self._hits.setdefault(key, []).append(now)

    def reset(self, key: str | None = None) -> None:
        if key is None:
            self._hits.clear()
        else:
            self._hits.pop(key, None)


login_limiter = SlidingWindowLimiter()

# The account-wide bucket's limit, as a multiple of the per-source one. See
# `_login_buckets` for why there are two.
ACCOUNT_BURST_MULTIPLE = 5

MIN_PASSWORD_LENGTH = 8


def _login_buckets(request: Request, email: str, settings) -> list[tuple[str, int]]:
    """The (key, limit) pairs a login attempt is counted against.

    `request.client.host` is the *socket* peer. Behind the reverse proxy
    docs/hardening.md requires, that is the proxy for every request, so an
    IP-keyed bucket silently collapses into one bucket for the whole internet.
    `X-Forwarded-For` is deliberately **not** consulted: it is client-settable, so
    trusting it would let an attacker mint a fresh bucket per attempt by varying a
    header — a limiter that can be stepped around is worse than one that is merely
    uninformative, and there is no way to tell a forged hop from a real one
    without a trusted-proxy configuration tret does not have.

    So the window is keyed twice, and both buckets survive a proxy:

    * `src|<peer>|<email>` at the configured limit — one host guessing at one
      account. Directly exposed this is exactly the old behaviour; behind a proxy
      it degrades into a per-account bucket, which is the guarantee actually worth
      keeping (it stops credential stuffing against one account).
    * `acct|<email>` at `ACCOUNT_BURST_MULTIPLE` times the limit — the ceiling
      that the first bucket loses when an attacker has many source addresses.

    Neither key contains anything the client controls except the email it is
    already attacking, and a lockout can still only ever affect one account.
    """
    peer = request.client.host if request.client else "unknown"
    account = email.strip().lower()
    return [
        (f"src|{peer}|{account}", settings.login_max_attempts),
        (f"acct|{account}", settings.login_max_attempts * ACCOUNT_BURST_MULTIPLE),
    ]


def credential_version(user: User) -> str:
    """Fingerprint of the credential a session was minted against.

    Two things move it: the password hash (argon2 salts every hash, so setting
    one — even to the same password — always changes this value) and
    `session_epoch` (bumped explicitly by `POST /api/auth/users/{id}/revoke-
    sessions` to end every session without touching the password at all — the
    only revocation lever an OIDC-only account, which has no password hash,
    will ever have). Either one changing invalidates every cookie carrying the
    old fingerprint.

    Only the fingerprint travels in the cookie, never the hash: it is a
    truncated digest of an already high-entropy string plus a small integer,
    and it is not a credential in itself.
    """
    epoch = user.session_epoch or 0
    return hashlib.sha256(f"{user.password_hash or ''}|{epoch}".encode()).hexdigest()[:16]


def _issue_session(response: Response, user: User, wid: uuid.UUID | None = None) -> None:
    payload: dict = {"uid": str(user.id), "cv": credential_version(user)}
    if wid is not None:
        payload["wid"] = str(wid)
    token = _serializer().dumps(payload)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=get_settings().cookie_secure,
    )


def _session_payload(request: Request) -> dict | None:
    """The signed cookie's payload, or None if there is none / it does not
    verify / it does not have the expected shape. Shared with
    api/workspace.py::current_workspace, which reads `wid` out of the same
    payload `current_user` below already validated for `uid`/`cv`.
    """
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    try:
        payload = _serializer().loads(token, max_age=SESSION_MAX_AGE)
    except BadSignature:
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("uid"), str):
        return None
    return payload


async def current_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        # No session cookie: the only other door in is a bearer access token,
        # and only once an operator has opted into it (`TRET_OIDC_API_
        # AUDIENCE` — see oidc_bearer.py's module docstring). Both gates
        # (enabled, header actually present) must hold before this ever does
        # anything network- or database-visible, so a deployment that hasn't
        # set the setting is byte-for-byte unaffected by its existence.
        settings = get_settings()
        scheme, _, credential = request.headers.get("Authorization", "").partition(" ")
        if bearer_auth_enabled(settings) and scheme.lower() == "bearer" and credential:
            return await authenticate_bearer(request, credential, db, settings)
        raise HTTPException(401, "Not authenticated")
    # Cookies minted before credential-bound sessions carried a bare user id
    # string, and any signature failure, land here as one 401: honouring the
    # old shape would be a way around revocation, and the cost is one
    # re-login per user after the upgrade that introduced this.
    payload = _session_payload(request)
    if payload is None:
        raise HTTPException(401, "Invalid session")
    try:
        user_id = uuid.UUID(payload["uid"])
    except ValueError:
        raise HTTPException(401, "Invalid session")
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(401, "Unknown user")
    if user.disabled:
        raise HTTPException(401, "Account deactivated")
    if payload.get("cv") != credential_version(user):
        raise HTTPException(401, "Session ended: this account's password was changed")
    return user


async def require_admin(request: Request, user: User = Depends(current_user)) -> User:
    """Instance-wide admin. Gates only /api/auth/users* (creating, listing and
    managing user accounts across the whole deployment) — anything scoped to
    one workspace's own data uses api/workspace.py's `require_workspace_admin`
    instead, even for a user whose global role also happens to be admin.

    `request.state.oidc_admin` is the other way to satisfy this: a bearer
    caller whose token roles claim contains `oidc_admin_role`
    (oidc_bearer.py::authenticate_bearer). That flag is request-scoped only
    — never written to `user.role` — so this check, not a database column,
    is where that elevation actually takes effect.

    This elevation is instance-scope only: `require_approver` below and
    `api/workspace.py`'s `require_workspace_admin` deliberately do not
    consult `request.state.oidc_admin` either, since an instance-admin role
    claim says nothing about a workspace membership or an approver role, so
    both intentionally keep requiring the real thing from a bearer caller —
    fails closed on purpose, not an oversight to "fix" into consistency.
    """
    if user.role != "admin" and not getattr(request.state, "oidc_admin", False):
        raise HTTPException(403, "Admin role required")
    return user


async def require_approver(user: User = Depends(current_user)) -> User:
    if user.role not in ("admin", "approver"):
        raise HTTPException(403, "Approver role required")
    return user


def _reject_if_password_disabled(settings=None) -> None:
    """Refuse a password-lifecycle endpoint outright once `auth_mode=oidc`
    has made password login unreachable: `login` itself and every endpoint
    that only exists to set or rotate a password (`create_user`'s password
    field included — an admin-created *password* account makes no sense once
    nothing can authenticate with one). `auth_mode="both"` leaves all of
    these alone; only `"oidc"` (SSO-only) gates them.
    """
    settings = settings or get_settings()
    if settings.auth_mode == "oidc":
        raise HTTPException(
            403,
            "Password authentication is disabled on this deployment — sign in with "
            "single sign-on instead (GET /api/auth/config for the login URL).",
        )


def _oidc_logout_url(settings: Settings, request: Request) -> str | None:
    """Where the browser should go after logout, once tret's own cookie is
    already cleared — `None` when there is nothing OIDC-specific to add.

    `settings.oidc_logout_url` wins verbatim when set (any IdP). Left blank,
    tret builds Auth0's own `/v2/logout` URL as a convenience — **this one
    fallback is Auth0-specific**: there is no OIDC-standard end-session
    endpoint to build instead, so any other IdP must set
    `TRET_OIDC_LOGOUT_URL` explicitly rather than get a URL that 404s. The
    fallback only applies in `oidc` mode (SSO-only) — in `both`, a logged-out
    user still has a password fallback on this same app, so there is no
    single "next place" to send them without asking, and the frontend's own
    post-logout screen is that ask.
    """
    if not settings.oidc_issuer:
        return None
    if settings.oidc_logout_url:
        return settings.oidc_logout_url
    if settings.auth_mode != "oidc":
        return None
    issuer = settings.oidc_issuer.strip()
    if not issuer.startswith(("http://", "https://")):
        issuer = f"https://{issuer}"
    return_to = str(request.base_url).rstrip("/")
    return (
        f"{issuer.rstrip('/')}/v2/logout"
        f"?client_id={quote(settings.oidc_client_id)}&returnTo={quote(return_to)}"
    )


class AuthConfigOut(BaseModel):
    auth_mode: str
    oidc_configured: bool
    oidc_login_url: str | None = None


@router.get("/config", response_model=AuthConfigOut)
async def auth_config():
    """Public, unauthenticated: what the login screen needs to decide which
    form(s) to show, before there is any session to check. `main.py` mounts
    `api/oidc.py`'s router once `oidc_issuer` alone is set; `oidc_configured`
    here is deliberately stricter (issuer *and* client id *and* client
    secret) — an issuer with no client credentials would 503 on the first
    real call, so 'configured' means 'this will actually work', not just
    'the router exists'.
    """
    settings = get_settings()
    configured = bool(settings.oidc_issuer and settings.oidc_client_id and settings.oidc_client_secret)
    return AuthConfigOut(
        auth_mode=settings.auth_mode,
        oidc_configured=configured,
        oidc_login_url="/api/auth/oidc/login" if configured else None,
    )


class LoginBody(BaseModel):
    email: str
    password: str


class WorkspaceSummary(BaseModel):
    id: uuid.UUID
    name: str
    kind: str
    role: str  # this user's role in this workspace


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str
    # This user's role *in the resolved current workspace* — see
    # `_resolve_workspaces` for exactly how that is resolved, and
    # `global_role` below for the meaning this field used to carry.
    role: str
    # The instance-wide role (admin | analyst | approver): what `role` meant
    # before workspaces had their own roles, and still what gates
    # /api/auth/users*. Breaking change from the pre-tenancy shape, called out
    # in CHANGELOG.md.
    global_role: str
    # False once an admin has deactivated the account: it holds no valid
    # session and cannot log in, regardless of whether its password is still
    # set (see this module's docstring). Reversible with
    # `POST /users/{id}/password`.
    active: bool = True
    workspaces: list[WorkspaceSummary] = []
    # Null when it cannot be resolved to exactly one workspace (no membership
    # at all, or more than one with nothing selected) — this endpoint always
    # succeeds regardless; a null here is what tells a client it must call
    # `POST /api/auth/workspace` before anything workspace-scoped will work.
    current_workspace_id: uuid.UUID | None = None


async def _resolve_workspaces(
    db: AsyncSession, user: User, *, preferred_wid: uuid.UUID | None = None
) -> tuple[list[WorkspaceSummary], str, uuid.UUID | None]:
    """The `(workspaces, role, current_workspace_id)` triple behind UserOut.

    `preferred_wid`, when given and it names one of this user's memberships,
    wins outright — this is `/me` reading the session's `wid`, and login /
    `POST /api/auth/workspace` reflecting the workspace a fresh cookie was
    just minted against. Absent a preference (or an unresolvable one — the
    membership was removed since the cookie was minted), a sole membership is
    used unambiguously; with zero or more than one membership there is no
    single answer, so `role` falls back to the user's global role and
    `current_workspace_id` is null. This must always produce *something* —
    unlike `api.workspace.current_workspace`, it never 409s, because `/me` is
    exactly the endpoint a client needs a clean answer from before it can
    even offer a workspace picker.
    """
    memberships = (
        await db.execute(select(WorkspaceMember).where(WorkspaceMember.user_id == user.id))
    ).scalars().all()
    workspaces: list[WorkspaceSummary] = []
    for member in memberships:
        workspace = await db.get(Workspace, member.workspace_id)
        if workspace is None:  # a dangling FK should not happen, but skip rather than 500
            continue
        workspaces.append(
            WorkspaceSummary(id=workspace.id, name=workspace.name, kind=workspace.kind, role=member.role)
        )

    if preferred_wid is not None:
        match = next((m for m in memberships if m.workspace_id == preferred_wid), None)
        if match is not None:
            return workspaces, match.role, preferred_wid
    if len(memberships) == 1:
        member = memberships[0]
        return workspaces, member.role, member.workspace_id
    return workspaces, user.role, None


async def _user_out(
    user: User, db: AsyncSession, *, preferred_wid: uuid.UUID | None = None
) -> UserOut:
    workspaces, role, current_workspace_id = await _resolve_workspaces(
        db, user, preferred_wid=preferred_wid
    )
    return UserOut(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=role,
        global_role=user.role,
        active=not user.disabled,
        workspaces=workspaces,
        current_workspace_id=current_workspace_id,
    )


@router.post("/login", response_model=UserOut)
async def login(
    body: LoginBody,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    settings = get_settings()
    _reject_if_password_disabled(settings)
    buckets = _login_buckets(request, body.email, settings)
    retry_after = max(
        login_limiter.check(key, limit, settings.login_window_seconds) for key, limit in buckets
    )
    if retry_after > 0:
        raise HTTPException(
            429,
            "Too many failed login attempts. Try again later.",
            headers={"Retry-After": str(int(retry_after) + 1)},
        )

    def _reject() -> HTTPException:
        for key, _limit in buckets:
            login_limiter.record(key)
        return HTTPException(401, "Invalid credentials")

    user = (await db.execute(select(User).where(User.email == body.email))).scalar_one_or_none()
    # No such user, no password hash (never set, or an OIDC-only account —
    # password login has nothing to verify against), or deactivated: all one
    # answer, on purpose (see test_login_never_reveals_whether_an_account_is_
    # deactivated). Checked before the password so a disabled account cannot
    # be distinguished from a wrong password by trying the right one.
    if user is None or not user.password_hash or user.disabled:
        raise _reject()
    try:
        _hasher.verify(user.password_hash, body.password)
    except VerifyMismatchError:
        raise _reject()
    for key, _limit in buckets:  # a success clears the window for this account
        login_limiter.reset(key)
    workspaces, role, current_workspace_id = await _resolve_workspaces(db, user)
    _issue_session(response, user, wid=current_workspace_id)
    return UserOut(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=role,
        global_role=user.role,
        active=True,
        workspaces=workspaces,
        current_workspace_id=current_workspace_id,
    )


@router.post("/logout")
async def logout(request: Request, response: Response):
    response.delete_cookie(SESSION_COOKIE)
    result: dict = {"ok": True}
    oidc_url = _oidc_logout_url(get_settings(), request)
    if oidc_url:
        result["oidc_logout_url"] = oidc_url
    return result


@router.get("/me", response_model=UserOut)
async def me(request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    payload = _session_payload(request)
    wid = payload.get("wid") if payload else None
    preferred_wid = None
    if wid:
        try:
            preferred_wid = uuid.UUID(wid)
        except ValueError:
            preferred_wid = None
    return await _user_out(user, db, preferred_wid=preferred_wid)


class SwitchWorkspaceBody(BaseModel):
    workspace_id: uuid.UUID


@router.post("/workspace", response_model=UserOut)
async def switch_workspace(
    body: SwitchWorkspaceBody,
    response: Response,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Switch the session's active workspace. Validates membership, then
    re-mints the cookie with the new `wid` — the same re-mint `login` does,
    just against a workspace the caller picked instead of inferred."""
    member = (
        await db.execute(
            select(WorkspaceMember).where(
                WorkspaceMember.user_id == user.id, WorkspaceMember.workspace_id == body.workspace_id
            )
        )
    ).scalar_one_or_none()
    if member is None:
        raise HTTPException(404, "Not a member of that workspace")
    _issue_session(response, user, wid=body.workspace_id)
    return await _user_out(user, db, preferred_wid=body.workspace_id)


class CreateUserBody(BaseModel):
    email: str
    display_name: str
    password: str
    role: str = "analyst"  # admin | analyst | approver
    # Which workspace this user joins. Required only when the instance has
    # more than one workspace to choose from (self-host's single workspace is
    # inferred, unchanged from before workspaces existed); the join happens at
    # this same `role` — the account's global role and its role in the one
    # workspace admin-created accounts join are the same thing, exactly as
    # every self-host user's role has always worked.
    workspace_id: uuid.UUID | None = None


@router.get("/users")
async def list_users(user: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    users = (await db.execute(select(User).order_by(User.email))).scalars().all()
    return [await _user_out(u, db) for u in users]


@router.post("/users", response_model=UserOut)
async def create_user(
    body: CreateUserBody, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)
):
    _reject_if_password_disabled()
    if body.role not in ("admin", "analyst", "approver"):
        raise HTTPException(422, "role must be admin|analyst|approver")
    _check_password_strength(body.password)
    dupe = (await db.execute(select(User).where(User.email == body.email))).scalar_one_or_none()
    if dupe:
        raise HTTPException(409, f"A user with email '{body.email}' already exists")

    workspaces = (await db.execute(select(Workspace))).scalars().all()
    if body.workspace_id is not None:
        target = next((w for w in workspaces if w.id == body.workspace_id), None)
        if target is None:
            raise HTTPException(422, f"workspace '{body.workspace_id}' not found")
    elif len(workspaces) == 1:
        target = workspaces[0]
    else:
        raise HTTPException(
            422,
            "workspace_id is required: this instance has more than one workspace, so "
            "which one this user joins cannot be inferred.",
        )

    user = User(
        email=body.email,
        display_name=body.display_name,
        password_hash=_hasher.hash(body.password),
        role=body.role,
    )
    db.add(user)
    await db.flush()
    db.add(WorkspaceMember(user_id=user.id, workspace_id=target.id, role=body.role))
    await db.commit()
    return await _user_out(user, db)


# ── credential lifecycle ─────────────────────────────────────────────────────
def _check_password_strength(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(
            422, f"password must be at least {MIN_PASSWORD_LENGTH} characters"
        )


class PasswordChangeBody(BaseModel):
    current_password: str
    new_password: str


@router.post("/password")
async def change_password(
    body: PasswordChangeBody,
    response: Response,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Change your own password. Every other session for the account ends.

    docs/hardening.md tells operators to change the bootstrap admin password
    after first boot; this is the endpoint that makes that instruction
    executable. The current password is required, so a stolen cookie alone
    cannot take the account over, and the check is rate limited so this cannot
    be used as an unthrottled oracle for the existing password.
    """
    settings = get_settings()
    _reject_if_password_disabled(settings)
    key = f"pwchange|{user.id}"
    retry_after = login_limiter.check(
        key, settings.login_max_attempts, settings.login_window_seconds
    )
    if retry_after > 0:
        raise HTTPException(
            429,
            "Too many failed attempts. Try again later.",
            headers={"Retry-After": str(int(retry_after) + 1)},
        )
    try:
        _hasher.verify(user.password_hash or "", body.current_password)
    except (VerifyMismatchError, InvalidHashError, VerificationError):
        login_limiter.record(key)
        raise HTTPException(403, "Current password is incorrect")
    _check_password_strength(body.new_password)
    if body.new_password == body.current_password:
        raise HTTPException(422, "The new password must differ from the current one")

    login_limiter.reset(key)
    user.password_hash = _hasher.hash(body.new_password)
    await db.commit()
    # Cookies minted against the old hash no longer resolve (see
    # credential_version), which is the revocation. Re-issue this one so the
    # person who just changed their password is not logged out by doing so.
    _issue_session(response, user)
    return {"ok": True, "sessions_invalidated": True}


class SetPasswordBody(BaseModel):
    new_password: str


async def _active_admin_count(db: AsyncSession) -> int:
    return (
        await db.execute(
            select(func.count())
            .select_from(User)
            .where(User.role == "admin", User.disabled.is_(False))
        )
    ).scalar_one()


@router.post("/users/{user_id}/password", response_model=UserOut)
async def admin_set_password(
    user_id: uuid.UUID,
    body: SetPasswordBody,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Set another user's password: rotation after an incident, or restoring a
    deactivated account. Ends every session that account currently holds, and
    clears `disabled` — exactly the "restore" half of deactivation, unchanged
    since before `disabled` existed as its own column."""
    _reject_if_password_disabled()
    _check_password_strength(body.new_password)
    target = await db.get(User, user_id)
    if target is None:
        raise HTTPException(404, "User not found")
    target.password_hash = _hasher.hash(body.new_password)
    target.disabled = False
    await db.commit()
    return await _user_out(target, db)


@router.post("/users/{user_id}/revoke-sessions", response_model=UserOut)
async def revoke_sessions(
    user_id: uuid.UUID,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """End every *cookie* session this account currently holds, without
    touching its password or its `disabled` state. Bumps `session_epoch`,
    which `credential_version` folds in — every outstanding cookie's
    fingerprint stops matching immediately. The lever an OIDC-only account
    (no password to rotate) will have for "sign me out everywhere", and the
    one to reach for after an incident when the password itself is not
    suspected.

    Does NOT revoke an OIDC bearer access token already issued for this
    account (`api/oidc_bearer.py::authenticate_bearer`) — `session_epoch` is
    an opaque counter with no time semantics to compare a token's `iat`
    against, so a bearer caller has nothing here to check it against. Such a
    token keeps working until its own `exp`. An incident responder relying
    on this endpoint to cut a compromised account off entirely must also
    revoke the token at the IdP (or wait out its lifetime) when bearer auth
    is enabled — this endpoint alone is not sufficient in that case.
    """
    target = await db.get(User, user_id)
    if target is None:
        raise HTTPException(404, "User not found")
    target.session_epoch = (target.session_epoch or 0) + 1
    await db.commit()
    return await _user_out(target, db)


@router.post("/users/{user_id}/deactivate", response_model=UserOut)
async def deactivate_user(
    user_id: uuid.UUID,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Deactivate a user: `current_user` refuses every cookie for the account
    from this point on, and its password is left alone rather than cleared —
    `POST /users/{id}/password` is what restores it, and restoring no longer
    requires picking a new password to go with it.

    The row is kept, not deleted — approvals and runs point at it, and an audit
    trail that can lose the name of the human who signed something is not an
    audit trail.
    """
    target = await db.get(User, user_id)
    if target is None:
        raise HTTPException(404, "User not found")
    if target.id == admin.id:
        raise HTTPException(
            422, "You cannot deactivate your own account — ask another admin to do it"
        )
    if target.disabled:
        return await _user_out(target, db)  # already deactivated: idempotent
    if target.role == "admin" and await _active_admin_count(db) <= 1:
        raise HTTPException(
            409,
            "Refusing to deactivate the last active admin: nobody would be able to "
            "manage users, provider keys or packs afterwards.",
        )
    target.disabled = True
    # Belt and braces alongside the `disabled` check in `current_user`: a
    # session minted before this call stops working even if `disabled` were
    # ever bypassed or the account is later re-enabled without a password
    # rotation (there is no such path today, but this is what keeps that safe
    # to add later).
    target.session_epoch = (target.session_epoch or 0) + 1
    await db.commit()
    return await _user_out(target, db)


@router.post("/users/{user_id}/reactivate", response_model=UserOut)
async def reactivate_user(
    user_id: uuid.UUID,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Reverse `deactivate_user`: clear `disabled`, nothing else.

    Deliberately NOT behind `_reject_if_password_disabled()` the way
    `admin_set_password` is — that endpoint is gated because *setting a
    password* is meaningless once `auth_mode=oidc` (nothing authenticates
    with one), but before this endpoint existed it was the only way to clear
    `disabled`, making deactivation irreversible on an OIDC-only deployment:
    an admin locked out under `auth_mode=oidc` had no password to be set and
    therefore no way back in. Reactivation itself is not a password
    operation — it does not touch `password_hash` or `session_epoch` — so it
    stays available regardless of `auth_mode`.
    """
    target = await db.get(User, user_id)
    if target is None:
        raise HTTPException(404, "User not found")
    target.disabled = False
    await db.commit()
    return await _user_out(target, db)


async def bootstrap_admin(db: AsyncSession) -> None:
    """First boot: create the admin user from env if no users exist."""
    settings = get_settings()
    count = (await db.execute(select(func.count()).select_from(User))).scalar_one()
    if count == 0:
        db.add(
            User(
                email=settings.admin_email,
                display_name="Admin",
                password_hash=_hasher.hash(settings.admin_password),
                role="admin",
            )
        )
        await db.commit()
