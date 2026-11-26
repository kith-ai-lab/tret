"""Divergence and human spot checks (PR 9, `tret/evaluation/divergence.py`): Engine 1's parts against Engine 2's whole.

Plain code over two engines' finished results plus human answers; no model is called.

* `GET /api/eval/divergence/inputs`: the workspace's completed credibility passes and completed tournaments.
* `POST .../preview` (approver): `{pass_id, tournament_ids, arm_key?}` -> stats, the parts-vs-whole gate,
  the reading-list size and warnings. Writes nothing.
* `POST ...` (approver): `{name, pass_id, tournament_ids, arm_key?}` freezes the join and samples the spot
  checks. `GET ...`, `GET .../{id}`, `GET .../{id}/rows`, `GET .../{id}/reading-list`.
* `GET .../{id}/rows` and `.../reading-list` are a 409 while any spot check is pending (they show module bands).
* `GET .../{id}/spot-checks`: blind triads and critiques (see the module docstring for what stays hidden).
  `POST .../spot-checks/{check_id}/answer` (approver): the answer is stamped with the session's user.
* `POST .../{id}/acknowledge` (approver): a tripped gate, with a note. `POST .../{id}/review` (approver):
  closes the set. `DELETE .../{id}` (approver): an open set only.

Every route is a 404 while `TRET_EVAL_DIVERGENCE` is off. Rows are workspace-scoped; another workspace's id is a 404.
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from tret.api.auth import current_user
from tret.api.workspace import WorkspaceContext, current_workspace, require_workspace_approver
from tret.db.engine import get_db
from tret.db.models import DivergenceSet
from tret.evaluation import divergence as dv


def _require_enabled() -> None:
    if not dv.divergence_enabled():
        raise HTTPException(404, "Not found")


router = APIRouter(prefix="/api/eval/divergence", tags=["eval-divergence"], dependencies=[Depends(_require_enabled)])


class PreviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pass_id: uuid.UUID
    tournament_ids: list[uuid.UUID] = Field(min_length=1, max_length=dv.MAX_TOURNAMENTS)
    arm_key: str | None = Field(default=None, max_length=32)


class CreateIn(PreviewIn):
    name: str = Field(default="", max_length=120)


class AnswerIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: dict[str, Any]
    note: str | None = Field(default=None, max_length=2000)


class AcknowledgeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gate: str = Field(max_length=40)
    note: str = Field(default="", max_length=500)


class ReviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str | None = Field(default=None, max_length=500)


def _raise(e: dv.DivergenceError) -> None:
    raise HTTPException(e.status, str(e)) from None


async def _get(db: AsyncSession, ctx: WorkspaceContext, set_id: uuid.UUID) -> DivergenceSet:
    try:
        return await dv.get_set(db, workspace_id=ctx.id, set_id=set_id)
    except dv.DivergenceError as e:
        _raise(e)


@router.get("/inputs")
async def inputs(user=Depends(current_user), ctx: WorkspaceContext = Depends(current_workspace), db: AsyncSession = Depends(get_db)):
    return await dv.inputs_out(db, workspace_id=ctx.id)


@router.post("/preview")
async def preview(body: PreviewIn, user=Depends(current_user), ctx: WorkspaceContext = Depends(require_workspace_approver),
                  db: AsyncSession = Depends(get_db)):
    try:
        return await dv.preview_set(db, workspace_id=ctx.id, pass_id=body.pass_id, tournament_ids=body.tournament_ids, arm_key=body.arm_key)
    except dv.DivergenceError as e:
        _raise(e)


@router.post("")
async def create(body: CreateIn, user=Depends(current_user), ctx: WorkspaceContext = Depends(require_workspace_approver),
                 db: AsyncSession = Depends(get_db)):
    try:
        s = await dv.create_set(db, workspace=ctx.workspace, user=user, name=body.name, pass_id=body.pass_id,
                                tournament_ids=body.tournament_ids, arm_key=body.arm_key)
    except dv.DivergenceError as e:
        await db.rollback()
        _raise(e)
    return await dv.set_detail(db, s)


@router.get("")
async def list_sets(user=Depends(current_user), ctx: WorkspaceContext = Depends(current_workspace), db: AsyncSession = Depends(get_db)):
    return await dv.list_sets(db, workspace_id=ctx.id)


@router.post("/spot-checks/{check_id}/answer")
async def answer(check_id: uuid.UUID, body: AnswerIn, user=Depends(current_user),
                 ctx: WorkspaceContext = Depends(require_workspace_approver), db: AsyncSession = Depends(get_db)):
    try:
        check = await dv.get_check(db, workspace_id=ctx.id, check_id=check_id)
        check = await dv.answer_spot_check(db, check, user=user, answer=body.answer, note=body.note)
    except dv.DivergenceError as e:
        await db.rollback()
        _raise(e)
    return await dv.spot_check_out(db, check)


@router.get("/{set_id}")
async def read(set_id: uuid.UUID, user=Depends(current_user), ctx: WorkspaceContext = Depends(current_workspace),
               db: AsyncSession = Depends(get_db)):
    return await dv.set_detail(db, await _get(db, ctx, set_id))


@router.get("/{set_id}/rows")
async def rows(
    set_id: uuid.UUID,
    quadrant: str | None = Query(None, pattern="^(both_strong|parts_over_whole|whole_over_parts|both_weak)$"),
    complete: bool | None = Query(None),
    limit: int = Query(500, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    user=Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await dv.rows_page(db, await _get(db, ctx, set_id), quadrant=quadrant, complete=complete, limit=limit, offset=offset)
    except dv.DivergenceError as e:
        _raise(e)


@router.get("/{set_id}/reading-list")
async def reading_list(set_id: uuid.UUID, user=Depends(current_user), ctx: WorkspaceContext = Depends(current_workspace),
                       db: AsyncSession = Depends(get_db)):
    try:
        return await dv.reading_list_out(db, await _get(db, ctx, set_id))
    except dv.DivergenceError as e:
        _raise(e)


@router.get("/{set_id}/spot-checks")
async def spot_checks(set_id: uuid.UUID, user=Depends(current_user), ctx: WorkspaceContext = Depends(current_workspace),
                      db: AsyncSession = Depends(get_db)):
    return await dv.spot_checks_out(db, await _get(db, ctx, set_id))


@router.post("/{set_id}/acknowledge")
async def acknowledge(set_id: uuid.UUID, body: AcknowledgeIn, user=Depends(current_user),
                      ctx: WorkspaceContext = Depends(require_workspace_approver), db: AsyncSession = Depends(get_db)):
    s = await _get(db, ctx, set_id)
    try:
        s = await dv.acknowledge_gate(db, s, user, body.gate, body.note)
    except dv.DivergenceError as e:
        await db.rollback()
        _raise(e)
    return await dv.set_detail(db, s)


@router.post("/{set_id}/review")
async def review(set_id: uuid.UUID, body: ReviewIn | None = None, user=Depends(current_user),
                 ctx: WorkspaceContext = Depends(require_workspace_approver), db: AsyncSession = Depends(get_db)):
    s = await _get(db, ctx, set_id)
    try:
        s = await dv.mark_reviewed(db, s, user, body.note if body else None)
    except dv.DivergenceError as e:
        await db.rollback()
        _raise(e)
    return await dv.set_detail(db, s)


@router.delete("/{set_id}", status_code=204)
async def delete(set_id: uuid.UUID, user=Depends(current_user), ctx: WorkspaceContext = Depends(require_workspace_approver),
                 db: AsyncSession = Depends(get_db)):
    s = await _get(db, ctx, set_id)
    try:
        await dv.delete_set(db, s)
    except dv.DivergenceError as e:
        await db.rollback()
        _raise(e)
    return Response(status_code=204)
