"""Jury selection and the per-pass freeze (`tret/evaluation/jury.py`).

* `POST /api/eval/jury/preview`: who the router would seat on a jury and why, before any spend.
  Writes nothing, calls no model.
* `POST /api/eval/jury/freezes` (approver): pick once and store under a `pass_ref`; a repeat with
  the same `pass_ref` returns the stored selection and never re-picks.
* `GET /api/eval/jury/freezes/{id}`, `POST /api/eval/jury/freezes/{id}/release` (approver).

Every route is a 404 while `TRET_EVAL_JURY` is off. Rows are workspace-scoped; another
workspace's id is a 404. A model is never named in a request: the policy constrains, the router picks.
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from tret.api.auth import current_user
from tret.api.workspace import WorkspaceContext, current_workspace, require_workspace_approver
from tret.config import get_settings
from tret.db.engine import get_db
from tret.evaluation import jury


def _require_enabled() -> None:
    if (getattr(get_settings(), "eval_jury", "off") or "off").strip().lower() != "on":
        raise HTTPException(404, "Not found")


router = APIRouter(prefix="/api/eval/jury", tags=["eval-jury"], dependencies=[Depends(_require_enabled)])


class JuryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_policy: dict[str, Any] | None = Field(default=None, description="An auto policy (objective, allowed, open_weights, max_cost_tier); never a pin")
    k: int = Field(default=3, ge=1, le=jury.MAX_K)
    distinct_authors: bool = True
    min_context_window: int = Field(default=0, ge=0, le=10_000_000)
    purpose: str = "eval_jury"


class FreezeIn(JuryIn):
    pass_ref: str = Field(min_length=1, max_length=200)


def _raise(e: jury.JuryError) -> None:
    raise HTTPException(e.status, str(e)) from None


@router.post("/preview")
async def preview(
    body: JuryIn,
    user=Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    try:
        sel = await jury.preview_jury(db, workspace_id=ctx.id, **body.model_dump())
    except jury.JuryError as e:
        _raise(e)
    return sel.to_json()


@router.post("/freezes")
async def create_freeze(
    body: FreezeIn,
    user=Depends(current_user),
    ctx: WorkspaceContext = Depends(require_workspace_approver),
    db: AsyncSession = Depends(get_db),
):
    try:
        row, created = await jury.freeze_jury(
            db, workspace_id=ctx.id, created_by=user.email, **body.model_dump()
        )
    except jury.JuryError as e:
        _raise(e)
    await db.commit()
    return {**jury.freeze_out(row), "created": created}


@router.get("/freezes/{freeze_id}")
async def read_freeze(
    freeze_id: uuid.UUID,
    user=Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    try:
        return jury.freeze_out(await jury.get_freeze(db, workspace_id=ctx.id, freeze_id=freeze_id))
    except jury.JuryError as e:
        _raise(e)


@router.post("/freezes/{freeze_id}/release")
async def release(
    freeze_id: uuid.UUID,
    user=Depends(current_user),
    ctx: WorkspaceContext = Depends(require_workspace_approver),
    db: AsyncSession = Depends(get_db),
):
    try:
        row = await jury.release_freeze(db, workspace_id=ctx.id, freeze_id=freeze_id)
    except jury.JuryError as e:
        _raise(e)
    await db.commit()
    return jury.freeze_out(row)
