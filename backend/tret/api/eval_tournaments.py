"""Engine 1 tournament (`tret/evaluation/tournament.py`): create a free preview, start it with a
budget, watch it, read the ratings.

* `POST /api/eval/tournaments` (approver): build the peer-pool snapshot and the preview: pools, N,
  rounds, estimated matches, a cost range and the jury the router would seat, with reasons. Spends
  nothing and calls no model; a `draft`.
* `GET /api/eval/tournaments`, `GET /api/eval/tournaments/{id}`.
* `POST .../{id}/start` (approver): `budget_usd` required. Freezes the jury and runs.
* `POST .../{id}/cancel`, `POST .../{id}/resume` (approver; resume may raise the budget; it works
  from any pause reason, and from a `running` tournament whose runner has stopped). A tripped gate
  (`length_bias`, `juror_agreement`, `anchor_drift`; `flip_rate` after `jury_degraded`) must be
  acknowledged to continue: `acknowledge_gates` + `note`, else 409 naming the gate(s).
* `GET .../{id}/diagnostics`: the gates (length bias, flip rate, position bias, juror agreement, anchor
  drift) with their evidence. A running or paused tournament serves the block its last round stored; a
  completed one is computed once and cached on the row. Only a gate listed in the tournament's
  `required_acknowledgements` can be acknowledged, and with a `note`. `POST .../{id}/anchor-set` (approver, `budget_usd`): take an anchor set
  from a completed tournament. `GET /api/eval/anchor-sets`, `GET .../{id}`, `POST .../{id}/resume`
  (approver): the anchor sets and their baseline judging.
* `POST .../{id}/refit` (approver): recompute the ratings of a completed or failed tournament from its
  stored matches. No model call, no spend.
* `DELETE .../{id}` (approver): a draft only, with its snapshot.
* `GET .../{id}/matches` (paged; staff view with the jurors' models and rationales),
  `GET .../{id}/ratings` (per pool, best first).

Every route is a 404 while `TRET_EVAL_TOURNAMENT` is off; creating and starting also need
`TRET_EVAL_JURY` on (409 otherwise). Rows are workspace-scoped; another workspace's id is a 404. A
model is never named in a request: the policy constrains, the router picks.
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tret.api.auth import current_user
from tret.api.workspace import WorkspaceContext, current_workspace, require_workspace_approver
from tret.db.engine import get_db
from tret.db.models import EvalTournament, PeerPoolSnapshot, RoutingFreeze
from tret.evaluation import anchors as an
from tret.evaluation import diagnostics as dg
from tret.evaluation import tournament as tn
from tret.evaluation.pools import DEFAULT_MIN_N


def _require_enabled() -> None:
    if not tn.tournament_enabled():
        raise HTTPException(404, "Not found")


router = APIRouter(prefix="/api/eval/tournaments", tags=["eval-tournaments"], dependencies=[Depends(_require_enabled)])
anchors_router = APIRouter(prefix="/api/eval/anchor-sets", tags=["eval-tournaments"], dependencies=[Depends(_require_enabled)])


class TournamentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    module: str = Field(min_length=1, max_length=80)
    module_set: str | None = Field(default=None, max_length=120, description="slug@version; default: the configured module set")
    batch_ids: list[uuid.UUID] | None = Field(default=None, max_length=200)
    min_n: int = Field(default=DEFAULT_MIN_N, ge=2, le=1000)
    model_policy: dict[str, Any] | None = Field(default=None, description="An auto policy for the jury (objective, allowed, open_weights, max_cost_tier); never a pin")


class StartIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    budget_usd: float = Field(gt=0, le=100_000)


class ResumeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    budget_usd: float | None = Field(default=None, gt=0, le=100_000)
    acknowledge_gates: list[str] | None = Field(default=None, max_length=10, description="Tripped gates the approver has looked at; they will not pause this tournament again")
    note: str | None = Field(default=None, max_length=500)


class AnchorIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    budget_usd: float = Field(gt=0, le=100_000, description="The most the baseline judging of the anchor set may spend")


class AnchorResumeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    budget_usd: float | None = Field(default=None, gt=0, le=100_000)


def _raise(e: tn.TournamentError) -> None:
    raise HTTPException(e.status, str(e)) from None


async def _out(db: AsyncSession, t: EvalTournament) -> dict[str, Any]:
    freeze = await db.get(RoutingFreeze, t.freeze_id) if t.freeze_id else None
    return tn.tournament_out(t, await db.get(PeerPoolSnapshot, t.snapshot_id), freeze)


@router.post("")
async def create(
    body: TournamentIn,
    user=Depends(current_user),
    ctx: WorkspaceContext = Depends(require_workspace_approver),
    db: AsyncSession = Depends(get_db),
):
    try:
        t = await tn.create_tournament(
            db, workspace_id=ctx.id, created_by=user.email, **body.model_dump(),
        )
    except tn.TournamentError as e:
        await db.rollback()
        _raise(e)
    await db.commit()
    return await _out(db, t)


@router.get("")
async def list_tournaments(
    limit: int = Query(50, ge=1, le=200),
    user=Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    rows = (await db.execute(
        select(EvalTournament).where(EvalTournament.workspace_id == ctx.id).order_by(EvalTournament.created_at.desc()).limit(limit)
    )).scalars().all()
    ids = [t.freeze_id for t in rows if t.freeze_id]
    freezes = {f.id: f for f in (await db.execute(select(RoutingFreeze).where(RoutingFreeze.id.in_(ids)))).scalars()} if ids else {}
    return {"tournaments": [tn.tournament_out(t, None, freezes.get(t.freeze_id)) for t in rows]}


async def _get(db: AsyncSession, ctx: WorkspaceContext, tournament_id: uuid.UUID) -> EvalTournament:
    try:
        return await tn.get_tournament(db, workspace_id=ctx.id, tournament_id=tournament_id)
    except tn.TournamentError as e:
        _raise(e)


@router.get("/{tournament_id}")
async def read(
    tournament_id: uuid.UUID,
    user=Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    return await _out(db, await _get(db, ctx, tournament_id))


@router.post("/{tournament_id}/start")
async def start(
    tournament_id: uuid.UUID,
    body: StartIn,
    user=Depends(current_user),
    ctx: WorkspaceContext = Depends(require_workspace_approver),
    db: AsyncSession = Depends(get_db),
):
    try:
        t = await tn.start_tournament(
            db, workspace_id=ctx.id, tournament_id=tournament_id, budget_usd=body.budget_usd, started_by=user.email
        )
    except tn.TournamentError as e:
        await db.rollback()
        _raise(e)
    return await _out(db, t)


@router.post("/{tournament_id}/cancel")
async def cancel(
    tournament_id: uuid.UUID,
    user=Depends(current_user),
    ctx: WorkspaceContext = Depends(require_workspace_approver),
    db: AsyncSession = Depends(get_db),
):
    try:
        t = await tn.cancel_tournament(db, workspace_id=ctx.id, tournament_id=tournament_id)
    except tn.TournamentError as e:
        await db.rollback()
        _raise(e)
    return await _out(db, t)


@router.post("/{tournament_id}/resume")
async def resume(
    tournament_id: uuid.UUID,
    body: ResumeIn | None = None,
    user=Depends(current_user),
    ctx: WorkspaceContext = Depends(require_workspace_approver),
    db: AsyncSession = Depends(get_db),
):
    try:
        t = await tn.resume_tournament(
            db, workspace_id=ctx.id, tournament_id=tournament_id, budget_usd=body.budget_usd if body else None,
            acknowledge_gates=body.acknowledge_gates if body else None, note=body.note if body else None,
            acknowledged_by=user.email,
        )
    except tn.TournamentError as e:
        await db.rollback()
        _raise(e)
    return await _out(db, t)


@router.post("/{tournament_id}/refit")
async def refit(
    tournament_id: uuid.UUID,
    user=Depends(current_user),
    ctx: WorkspaceContext = Depends(require_workspace_approver),
    db: AsyncSession = Depends(get_db),
):
    try:
        t = await tn.refit_tournament(db, workspace_id=ctx.id, tournament_id=tournament_id)
    except tn.TournamentError as e:
        await db.rollback()
        _raise(e)
    return await _out(db, t)


@router.delete("/{tournament_id}")
async def delete(
    tournament_id: uuid.UUID,
    user=Depends(current_user),
    ctx: WorkspaceContext = Depends(require_workspace_approver),
    db: AsyncSession = Depends(get_db),
):
    try:
        await tn.delete_draft(db, workspace_id=ctx.id, tournament_id=tournament_id)
    except tn.TournamentError as e:
        await db.rollback()
        _raise(e)
    return {"deleted": str(tournament_id)}


@router.get("/{tournament_id}/matches")
async def matches(
    tournament_id: uuid.UUID,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    status: str | None = Query(None, pattern="^(pending|running|done|failed)$"),
    kind: str | None = Query(None, pattern="^(primary|swap|double|anchor)$"),
    pool: str | None = Query(None, max_length=200),
    user=Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    t = await _get(db, ctx, tournament_id)
    return await tn.list_matches(db, t, limit=limit, offset=offset, status=status, kind=kind, pool=pool)


@router.get("/{tournament_id}/ratings")
async def ratings(
    tournament_id: uuid.UUID,
    user=Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    return await tn.list_ratings(db, await _get(db, ctx, tournament_id))


@router.get("/{tournament_id}/diagnostics")
async def diagnostics(
    tournament_id: uuid.UUID,
    user=Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    t = await _get(db, ctx, tournament_id)
    return await dg.serve_diagnostics(db, t)


@router.post("/{tournament_id}/anchor-set")
async def create_anchor_set(
    tournament_id: uuid.UUID,
    body: AnchorIn,
    user=Depends(current_user),
    ctx: WorkspaceContext = Depends(require_workspace_approver),
    db: AsyncSession = Depends(get_db),
):
    try:
        a = await an.create_anchor_set(
            db, workspace_id=ctx.id, tournament_id=tournament_id, budget_usd=body.budget_usd, created_by=user.email
        )
    except tn.TournamentError as e:
        await db.rollback()
        _raise(e)
    return await an.anchor_set_out(db, a, detail=True)


@anchors_router.get("")
async def list_anchor_sets(
    limit: int = Query(50, ge=1, le=200),
    user=Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    return {"anchor_sets": await an.list_anchor_sets(db, workspace_id=ctx.id, limit=limit)}


@anchors_router.get("/{set_id}")
async def read_anchor_set(
    set_id: uuid.UUID,
    user=Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    try:
        a = await an.get_anchor_set(db, workspace_id=ctx.id, set_id=set_id)
    except tn.TournamentError as e:
        _raise(e)
    return await an.anchor_set_out(db, a, detail=True)


@anchors_router.post("/{set_id}/resume")
async def resume_anchor_set(
    set_id: uuid.UUID,
    body: AnchorResumeIn | None = None,
    user=Depends(current_user),
    ctx: WorkspaceContext = Depends(require_workspace_approver),
    db: AsyncSession = Depends(get_db),
):
    try:
        a = await an.resume_anchor_set(db, workspace_id=ctx.id, set_id=set_id, budget_usd=body.budget_usd if body else None)
    except tn.TournamentError as e:
        await db.rollback()
        _raise(e)
    return await an.anchor_set_out(db, a, detail=True)
