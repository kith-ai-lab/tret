"""Local auth v1: email/password (argon2) + signed httpOnly session cookie.

Kept behind small dependencies (`current_user`, `require_admin`) so an OIDC
implementation can replace this module without touching other routers.

**Credential lifecycle.** Sessions are stateless signed cookies, so there is no
session table to delete rows from. Revocation is instead bound to the credential:
every cookie carries a short fingerprint of the password hash it was minted
against (`credential_version`), and `current_user` refuses a cookie whose
fingerprint no longer matches. Setting a password therefore invalidates every
session for that account — a self-service change (`POST /api/auth/password`), an
admin rotation (`POST /api/auth/users/{id}/password`), or a deactivation that
clears the credential entirely (`POST /api/auth/users/{id}/deactivate`). This is
what docs/hardening.md's instruction to rotate credentials after an incident
needs in order to mean anything.

Login is rate limited by a small in-memory sliding window. It is per-process —
correct for the single-worker deployment tret ships, and a speed bump rather
than a defence against a distributed attacker. Put a WAF/proxy limit in front for
anything internet-facing.
"""
from __future__ import annotations

import hashlib
import time
import uuid

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tret.config import get_settings
from tret.db.engine import get_db
from tret.db.models import User

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

    argon2 salts every hash, so setting a password always changes this value and
    every cookie carrying the old one stops resolving. Only the fingerprint
    travels in the cookie, never the hash: it is a truncated digest of an already
    high-entropy string, and it is not a credential in itself.

    A user with no password hash (deactivated, or an OIDC-only user later) has no
    valid credential version, so no cookie can be minted against one.
    """
    return hashlib.sha256((user.password_hash or "").encode()).hexdigest()[:16]


def _issue_session(response: Response, user: User) -> None:
    token = _serializer().dumps({"uid": str(user.id), "cv": credential_version(user)})
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=get_settings().cookie_secure,
    )


async def current_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(401, "Not authenticated")
    try:
        payload = _serializer().loads(token, max_age=SESSION_MAX_AGE)
    except BadSignature:
        raise HTTPException(401, "Invalid session")
    # Cookies minted before credential-bound sessions carried a bare user id
    # string. They are refused rather than accepted: honouring the old shape
    # would be a way around revocation, and the cost is one re-login per user
    # after the upgrade that introduced this.
    if not isinstance(payload, dict) or not isinstance(payload.get("uid"), str):
        raise HTTPException(401, "Invalid session")
    try:
        user_id = uuid.UUID(payload["uid"])
    except ValueError:
        raise HTTPException(401, "Invalid session")
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(401, "Unknown user")
    if not user.password_hash:
        raise HTTPException(401, "Account deactivated")
    if payload.get("cv") != credential_version(user):
        raise HTTPException(401, "Session ended: this account's password was changed")
    return user


async def require_admin(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(403, "Admin role required")
    return user


async def require_approver(user: User = Depends(current_user)) -> User:
    if user.role not in ("admin", "approver"):
        raise HTTPException(403, "Approver role required")
    return user


class LoginBody(BaseModel):
    email: str
    password: str


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str
    role: str
    # False once an admin has cleared the account's credential: the user cannot
    # log in and holds no valid session. Reversible by setting a new password.
    active: bool = True


def _user_out(user: User) -> UserOut:
    return UserOut(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=user.role,
        active=bool(user.password_hash),
    )


@router.post("/login", response_model=UserOut)
async def login(
    body: LoginBody,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    settings = get_settings()
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
    # No password hash: never set, or cleared by a deactivation. Indistinguishable
    # from "no such user" on purpose.
    if user is None or not user.password_hash:
        raise _reject()
    try:
        _hasher.verify(user.password_hash, body.password)
    except VerifyMismatchError:
        raise _reject()
    for key, _limit in buckets:  # a success clears the window for this account
        login_limiter.reset(key)
    _issue_session(response, user)
    return _user_out(user)


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(current_user)):
    return _user_out(user)


class CreateUserBody(BaseModel):
    email: str
    display_name: str
    password: str
    role: str = "analyst"  # admin | analyst | approver


@router.get("/users")
async def list_users(user: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    users = (await db.execute(select(User).order_by(User.email))).scalars().all()
    return [_user_out(u) for u in users]


@router.post("/users", response_model=UserOut)
async def create_user(
    body: CreateUserBody, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)
):
    if body.role not in ("admin", "analyst", "approver"):
        raise HTTPException(422, "role must be admin|analyst|approver")
    _check_password_strength(body.password)
    dupe = (await db.execute(select(User).where(User.email == body.email))).scalar_one_or_none()
    if dupe:
        raise HTTPException(409, f"A user with email '{body.email}' already exists")
    user = User(
        email=body.email,
        display_name=body.display_name,
        password_hash=_hasher.hash(body.password),
        role=body.role,
    )
    db.add(user)
    await db.commit()
    return _user_out(user)


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
            .where(User.role == "admin", User.password_hash.is_not(None))
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
    deactivated account. Ends every session that account currently holds."""
    _check_password_strength(body.new_password)
    target = await db.get(User, user_id)
    if target is None:
        raise HTTPException(404, "User not found")
    target.password_hash = _hasher.hash(body.new_password)
    await db.commit()
    return _user_out(target)


@router.post("/users/{user_id}/deactivate", response_model=UserOut)
async def deactivate_user(
    user_id: uuid.UUID,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Deactivate a user: clears the credential and ends every session it holds.

    The row is kept, not deleted — approvals and runs point at it, and an audit
    trail that can lose the name of the human who signed something is not an
    audit trail. Reversible with `POST /users/{id}/password`.
    """
    target = await db.get(User, user_id)
    if target is None:
        raise HTTPException(404, "User not found")
    if target.id == admin.id:
        raise HTTPException(
            422, "You cannot deactivate your own account — ask another admin to do it"
        )
    if not target.password_hash:
        return _user_out(target)  # already deactivated: idempotent
    if target.role == "admin" and await _active_admin_count(db) <= 1:
        raise HTTPException(
            409,
            "Refusing to deactivate the last active admin: nobody would be able to "
            "manage users, provider keys or packs afterwards.",
        )
    target.password_hash = None
    await db.commit()
    return _user_out(target)


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
