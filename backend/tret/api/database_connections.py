"""`/api/connections/database`: read-only Postgres / Supabase connections.

List (any member), add / test / remove (workspace admin or owner). Everything
that decides what is allowed lives in `services/database_connections.py`; this
module is HTTP shape. The DSN is accepted once, on POST, and is never echoed back
or readable again: responses carry host/port/database/user and status only.

With `TRET_DB_CONNECTIONS` off every route here is a 404 (checked before
authentication, so the routes simply do not exist).

Creating and testing hold no app-database transaction while they contact the
remote database, run one at a time per workspace and at most 6 a minute (429).
Gates (same extension seam as the OAuth connections): creating asks
`connections.connect`, testing asks `connections.use`. Removing is NOT gated, on
purpose: a workspace whose plan lapsed must still be able to delete a stored
database secret, the same as `DELETE /api/connections/{provider}`.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tret.api.auth import current_user
from tret.api.workspace import WorkspaceContext, current_workspace, require_workspace_admin
from tret.db.engine import get_db
from tret.db.models import User
from tret.services import database_connections as svc

router = APIRouter(prefix="/api/connections/database", tags=["database-connections"])

_STATUS_BY_KIND = {
    "input": 422,
    "policy": 422,
    "connect": 400,
    "query": 400,
    "limit": 409,
    "unavailable": 409,
    "auth": 400,
    "busy": 429,
    "rate": 429,
}


def _require_feature() -> None:
    if not svc.feature_enabled():
        raise HTTPException(404, "Not Found")


def _raise(exc: svc.DatabaseConnectionError) -> None:
    if exc.kind == "gate":
        raise HTTPException(403, detail={"reason": "plan_gate", "detail": exc.message}) from None
    raise HTTPException(_STATUS_BY_KIND.get(exc.kind, 400), exc.message) from None


class DatabaseConnectionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(max_length=80)
    # Length and shape are checked by `parse_dsn`, not here: a pydantic length
    # error would echo the offending input (the DSN) back in the 422 body.
    dsn: str = Field(repr=False)


async def _read_body(request: Request) -> DatabaseConnectionBody:
    """Parse the JSON body by hand. FastAPI's own validation errors carry an
    `input` field (the whole body when a field is missing), which would send the
    connection string back to the client; this reports field names only."""
    try:
        payload = await request.json()
    except ValueError:
        raise HTTPException(422, "The request body must be JSON.") from None
    try:
        return DatabaseConnectionBody.model_validate(payload)
    except ValidationError as exc:
        problems = sorted({".".join(str(p) for p in e["loc"]) or "body" for e in exc.errors()})
        raise HTTPException(422, "Invalid request body; check: " + ", ".join(problems)) from None


@router.get("")
async def list_database_connections(
    _: None = Depends(_require_feature),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    rows = await svc.list_rows(db, ctx.id)
    return [svc.connection_view(r) for r in rows]


@router.post("")
async def add_database_connection(
    request: Request,
    _: None = Depends(_require_feature),
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(require_workspace_admin),
    db: AsyncSession = Depends(get_db),
):
    body = await _read_body(request)
    # Plain ids up front: the service ends the app-DB transaction around the
    # remote connect, which expires ORM objects such as `user`.
    workspace_id, user_id = ctx.id, user.id
    try:
        row = await svc.create_connection(
            db, workspace_id=workspace_id, user_id=user_id, label=body.label, dsn=body.dsn
        )
    except svc.DatabaseConnectionError as exc:
        await db.rollback()
        _raise(exc)
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "A database connection with that name was just created; try again.") from None
    return svc.connection_view(row)


@router.post("/{slug}/test")
async def test_database_connection(
    slug: str,
    _: None = Depends(_require_feature),
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(require_workspace_admin),
    db: AsyncSession = Depends(get_db),
):
    workspace_id, user_id = ctx.id, user.id
    try:
        row = await svc.retest_connection(db, workspace_id=workspace_id, slug=slug, user_id=user_id)
    except svc.DatabaseConnectionError as exc:
        await db.rollback()
        _raise(exc)
    if row is None:
        raise HTTPException(404, "No such database connection")
    return svc.connection_view(row)


@router.delete("/{slug}")
async def remove_database_connection(
    slug: str,
    _: None = Depends(_require_feature),
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(require_workspace_admin),
    db: AsyncSession = Depends(get_db),
):
    row = await svc.get_row(db, ctx.id, slug)
    if row is None:
        raise HTTPException(404, "No such database connection")
    await db.delete(row)
    await svc.record_activity(db, ctx.id, slug, "disconnect", actor_user_id=user.id)
    await db.commit()
    return {"ok": True}
