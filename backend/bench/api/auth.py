"""Local auth v1: email/password (argon2) + signed httpOnly session cookie.

Kept behind small dependencies (`current_user`, `require_admin`) so an OIDC
implementation can replace this module without touching other routers.
"""
from __future__ import annotations

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
async def login(body: LoginBody, response: Response, db: AsyncSession = Depends(get_db)):
    user = (await db.execute(select(User).where(User.email == body.email))).scalar_one_or_none()
    if user is None or not user.password_hash:
        raise HTTPException(401, "Invalid credentials")
    try:
        _hasher.verify(user.password_hash, body.password)
    except VerifyMismatchError:
        raise HTTPException(401, "Invalid credentials")
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
