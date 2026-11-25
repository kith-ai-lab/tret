"""Engine 2, the sealed Credibility call (`tret/evaluation/credibility.py`): preview it for free, create a
draft, start it with a budget, watch it, read the reviews.

* `POST /api/eval/credibility/preview` (approver): the report selection (`disclosure_report_ids`, at most
  2000, or a `batch_id` with an optional `limit` and `sample_seed`), the `arms` (1-4 of `{key, label,
  weight, model_policy}`; a policy constrains, the router picks, a pin is a 422) and an optional
  `budget_usd`. Returns the report count, what is skipped and why, whole vs digest, the estimated tokens and
  cost per arm and what each arm WOULD pick and why. Writes nothing, calls no model.
* `POST /api/eval/credibility` (approver): the same body plus `name`; a `draft` with every report assigned to
  an arm. `GET /api/eval/credibility`, `GET /api/eval/credibility/{id}` (gates, per-arm statistics, the
  frozen models once started).
* `POST .../{id}/start` (approver): `budget_usd` required. Freezes each arm's model and runs.
  `POST .../{id}/cancel`, `POST .../{id}/resume` (approver; `budget_usd`, `acknowledge_gates` + `note`: a
  tripped gate must be acknowledged with a note, 409 naming the gate(s) otherwise, 422 without a note).
  `DELETE .../{id}`: a draft or cancelled pass only.
* `GET .../{id}/reviews` (paged; filter by `arm`, `status`, `score`, `min_score`, `max_score`),
  `GET .../reviews/{review_id}`, `POST .../reviews/{review_id}/verify` (the payload audit: rebuild the
  prompt from the report and compare the stored hashes).

Every route is a 404 while `TRET_EVAL_CREDIBILITY` is off; creating and starting also need
`TRET_EVAL_JURY` on (409 otherwise). Rows are workspace-scoped; another workspace's id is a 404. A review
may carry the company name for the human reader: the model's payload never does.
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
from tret.db.models import CredibilityPass
from tret.evaluation import credibility as cr


def _require_enabled() -> None:
    if not cr.credibility_enabled():
        raise HTTPException(404, "Not found")


router = APIRouter(prefix="/api/eval/credibility", tags=["eval-credibility"], dependencies=[Depends(_require_enabled)])


class ArmIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=32, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    label: str | None = Field(default=None, max_length=80)
    weight: float = Field(default=1.0, gt=0, le=1000)
    model_policy: dict[str, Any] | None = Field(default=None, description="An auto policy (objective, allowed, open_weights, max_cost_tier); never a pin")


class PreviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    disclosure_report_ids: list[uuid.UUID] | None = Field(default=None, max_length=cr.MAX_REPORTS)
    batch_id: uuid.UUID | None = None
    limit: int | None = Field(default=None, ge=1, le=cr.MAX_REPORTS, description="With batch_id: read at most this many reports")
    sample_seed: int | None = Field(default=None, description="With limit: draw a seeded random sample instead of the first reports")
    arms: list[ArmIn] = Field(min_length=1, max_length=cr.MAX_ARMS)
    budget_usd: float | None = Field(default=None, gt=0, le=100_000)


class CreateIn(PreviewIn):
    name: str = Field(default="", max_length=120)


class StartIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    budget_usd: float = Field(gt=0, le=100_000)


class ResumeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    budget_usd: float | None = Field(default=None, gt=0, le=100_000)
    acknowledge_gates: list[str] | None = Field(default=None, max_length=10, description="Tripped gates the approver has looked at; they will not pause this pass again")
    note: str | None = Field(default=None, max_length=500)


def _raise(e: cr.CredibilityError) -> None:
    raise HTTPException(e.status, str(e)) from None


def _arms(body: PreviewIn) -> list[dict]:
    return [a.model_dump() for a in body.arms]


def _selection(body: PreviewIn) -> dict[str, Any]:
    return {"disclosure_report_ids": body.disclosure_report_ids, "batch_id": body.batch_id, "limit": body.limit,
            "sample_seed": body.sample_seed}


async def _get(db: AsyncSession, ctx: WorkspaceContext, pass_id: uuid.UUID) -> CredibilityPass:
    try:
        return await cr.get_pass(db, workspace_id=ctx.id, pass_id=pass_id)
    except cr.CredibilityError as e:
        _raise(e)


@router.post("/preview")
async def preview(
    body: PreviewIn,
    user=Depends(current_user),
    ctx: WorkspaceContext = Depends(require_workspace_approver),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await cr.preview_pass(db, workspace_id=ctx.id, arms=_arms(body), budget_usd=body.budget_usd, **_selection(body))
    except cr.CredibilityError as e:
        _raise(e)


@router.post("")
async def create(
    body: CreateIn,
    user=Depends(current_user),
    ctx: WorkspaceContext = Depends(require_workspace_approver),
    db: AsyncSession = Depends(get_db),
):
    try:
        p = await cr.create_pass(
            db, workspace_id=ctx.id, arms=_arms(body), name=body.name, budget_usd=body.budget_usd, created_by=user.email, **_selection(body)
        )
    except cr.CredibilityError as e:
        await db.rollback()
        _raise(e)
    await db.commit()
    return await cr.pass_detail(db, p)


@router.get("")
async def list_passes(
    limit: int = Query(50, ge=1, le=200),
    user=Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    rows = (await db.execute(
        select(CredibilityPass).where(CredibilityPass.workspace_id == ctx.id).order_by(CredibilityPass.created_at.desc()).limit(limit)
    )).scalars().all()
    counts = await cr.counts_for(db, [p.id for p in rows])
    return {"passes": [cr.pass_out(p, counts.get(p.id)) for p in rows]}


@router.get("/reviews/{review_id}")
async def read_review(
    review_id: uuid.UUID,
    user=Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    try:
        r, _p = await cr.get_review(db, workspace_id=ctx.id, review_id=review_id)
    except cr.CredibilityError as e:
        _raise(e)
    return await cr.review_detail(db, r)


@router.post("/reviews/{review_id}/verify")
async def verify(
    review_id: uuid.UUID,
    user=Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await cr.verify_review(db, workspace_id=ctx.id, review_id=review_id)
    except cr.CredibilityError as e:
        _raise(e)


@router.get("/{pass_id}")
async def read(
    pass_id: uuid.UUID,
    user=Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    return await cr.pass_detail(db, await _get(db, ctx, pass_id))


@router.get("/{pass_id}/reviews")
async def reviews(
    pass_id: uuid.UUID,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    arm: str | None = Query(None, max_length=32),
    status: str | None = Query(None, pattern="^(pending|running|done|failed|skipped)$"),
    score: int | None = Query(None, ge=1, le=10),
    min_score: int | None = Query(None, ge=1, le=10),
    max_score: int | None = Query(None, ge=1, le=10),
    user=Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    p = await _get(db, ctx, pass_id)
    return await cr.list_reviews(db, p, limit=limit, offset=offset, arm=arm, status=status, score=score, min_score=min_score, max_score=max_score)


@router.post("/{pass_id}/start")
async def start(
    pass_id: uuid.UUID,
    body: StartIn,
    user=Depends(current_user),
    ctx: WorkspaceContext = Depends(require_workspace_approver),
    db: AsyncSession = Depends(get_db),
):
    try:
        p = await cr.start_pass(db, workspace_id=ctx.id, pass_id=pass_id, budget_usd=body.budget_usd, started_by=user.email)
    except cr.CredibilityError as e:
        await db.rollback()
        _raise(e)
    return await cr.pass_detail(db, p)


@router.post("/{pass_id}/cancel")
async def cancel(
    pass_id: uuid.UUID,
    user=Depends(current_user),
    ctx: WorkspaceContext = Depends(require_workspace_approver),
    db: AsyncSession = Depends(get_db),
):
    try:
        p = await cr.cancel_pass(db, workspace_id=ctx.id, pass_id=pass_id)
    except cr.CredibilityError as e:
        await db.rollback()
        _raise(e)
    return await cr.pass_detail(db, p)


@router.post("/{pass_id}/resume")
async def resume(
    pass_id: uuid.UUID,
    body: ResumeIn | None = None,
    user=Depends(current_user),
    ctx: WorkspaceContext = Depends(require_workspace_approver),
    db: AsyncSession = Depends(get_db),
):
    try:
        p = await cr.resume_pass(
            db, workspace_id=ctx.id, pass_id=pass_id, budget_usd=body.budget_usd if body else None,
            acknowledge_gates=body.acknowledge_gates if body else None, note=body.note if body else None, acknowledged_by=user.email,
        )
    except cr.CredibilityError as e:
        await db.rollback()
        _raise(e)
    return await cr.pass_detail(db, p)


@router.delete("/{pass_id}")
async def delete(
    pass_id: uuid.UUID,
    user=Depends(current_user),
    ctx: WorkspaceContext = Depends(require_workspace_approver),
    db: AsyncSession = Depends(get_db),
):
    try:
        await cr.delete_pass(db, workspace_id=ctx.id, pass_id=pass_id)
    except cr.CredibilityError as e:
        await db.rollback()
        _raise(e)
    return {"deleted": str(pass_id)}
