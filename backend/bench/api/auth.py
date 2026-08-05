"""Local auth v1: email/password (argon2) + signed httpOnly session cookie.

Kept behind small dependencies (`current_user`, `require_admin`) so an OIDC
implementation can replace this module without touching other routers.

Login is rate limited by a small in-memory sliding window (per client IP +
email). It is per-process — correct for the single-worker deployment bench
ships, and a speed bump rather than a defence against a distributed attacker.
Put a WAF/proxy limit in front for anything internet-facing.
"""
from __future__ import annotations

import time
import uuid

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bench.config import get_settings
from bench.db.engine import get_db
from bench.db.models import User

router = APIRouter(prefix="/api/auth", tags=["auth"])

_hasher = PasswordHasher()
SESSION_COOKIE = "bench_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 14  # 14 days


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().secret_key, salt="bench-session")


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


def _login_key(request: Request, email: str) -> str:
    client = request.client.host if request.client else "unknown"
    return f"{client}|{email.strip().lower()}"


async def current_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(401, "Not authenticated")
    try:
        user_id = _serializer().loads(token, max_age=SESSION_MAX_AGE)
    except BadSignature:
        raise HTTPException(401, "Invalid session")
    user = await db.get(User, uuid.UUID(user_id))
    if user is None:
        raise HTTPException(401, "Unknown user")
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


@router.post("/login", response_model=UserOut)
async def login(
    body: LoginBody,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    settings = get_settings()
    key = _login_key(request, body.email)
    retry_after = login_limiter.check(
        key, settings.login_max_attempts, settings.login_window_seconds
    )
    if retry_after > 0:
        raise HTTPException(
            429,
            "Too many failed login attempts. Try again later.",
            headers={"Retry-After": str(int(retry_after) + 1)},
        )

    def _reject() -> HTTPException:
        login_limiter.record(key)
        return HTTPException(401, "Invalid credentials")

    user = (await db.execute(select(User).where(User.email == body.email))).scalar_one_or_none()
    if user is None or not user.password_hash:
        raise _reject()
    try:
        _hasher.verify(user.password_hash, body.password)
    except VerifyMismatchError:
        raise _reject()
    login_limiter.reset(key)  # a success clears the window for this IP + email
    token = _serializer().dumps(str(user.id))
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=get_settings().cookie_secure,
    )
    return UserOut(id=user.id, email=user.email, display_name=user.display_name, role=user.role)


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(current_user)):
    return UserOut(id=user.id, email=user.email, display_name=user.display_name, role=user.role)


class CreateUserBody(BaseModel):
    email: str
    display_name: str
    password: str
    role: str = "analyst"  # admin | analyst | approver


@router.get("/users")
async def list_users(user: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    users = (await db.execute(select(User).order_by(User.email))).scalars().all()
    return [
        UserOut(id=u.id, email=u.email, display_name=u.display_name, role=u.role) for u in users
    ]


@router.post("/users", response_model=UserOut)
async def create_user(
    body: CreateUserBody, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)
):
    if body.role not in ("admin", "analyst", "approver"):
        raise HTTPException(422, "role must be admin|analyst|approver")
    if len(body.password) < 8:
        raise HTTPException(422, "password must be at least 8 characters")
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
    return UserOut(id=user.id, email=user.email, display_name=user.display_name, role=user.role)


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
