"""Batches: a table of targets run one engine run per target, with an optional
review gate, retry and a finalize step that writes run-level records. The
orchestration lives in `tret/batches/` (re-exported by
`tret/services/collections.py`); this module is its HTTP surface.

Two profiles: `disclosure` (the Kith Climate disclosure collection: a register
of companies, package maps reviewed, SharePoint write-back) and `prompt` (any
harness over the rows of a CSV). Every route is served under `/api/batches` and,
for one release, under `/api/collections` (marked deprecated); the only
difference is the list key (`batches` vs `collections`).

Reads are open to any workspace member; every state change needs the approver
role, like the approvals it drives.
"""
from __future__ import annotations

import uuid
from urllib.parse import quote
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Discriminator, Field, Tag, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tret.api.auth import current_user
from tret.api.findings import _finding_out
from tret.api.workspace import (
    WorkspaceContext,
    current_workspace,
    require_workspace_admin,
    require_workspace_approver,
)
from tret.batches import advice, evaluation, experiments, families, pairwise, scoring
from tret.batches.core import written_files_latest
from tret.batches.rubrics import default_rubric
from tret.batches.profiles.prompt import CreatePromptBatchBody
from tret.db.engine import get_db
from tret.db.models import Batch, BatchTarget, Document, Harness, User
from tret.services import collections as service
from tret.services import lifecycle
from tret.storage import document_bytes

router = APIRouter(prefix="/api", tags=["batches"])

# Text-like written files over this size are download-only (the web viewer renders them inline).
INLINE_TEXT_MAX_BYTES = 5 * 1024 * 1024

_FILE_TYPES = {
    ".pdf": "application/pdf",
    ".md": "text/markdown; charset=utf-8",
    ".json": "application/json",
    ".txt": "text/plain; charset=utf-8",
}


def _both(method: str, path: str):
    """Register a handler under `/batches{path}` and, deprecated, under
    `/collections{path}`."""

    def deco(fn):
        getattr(router, method)(f"/batches{path}")(fn)
        getattr(router, method)(f"/collections{path}", deprecated=True)(fn)
        return fn

    return deco


def _profile_tag(value: Any) -> str:
    """The create body's discriminator: `profile`, defaulting to "disclosure" so
    a body without it is today's collection body."""
    raw = value.get("profile") if isinstance(value, dict) else getattr(value, "profile", None)
    return raw or "disclosure"


CreateBatchBody = Annotated[
    Annotated[service.CreateCollectionBody, Tag("disclosure")]
    | Annotated[CreatePromptBatchBody, Tag("prompt")],
    Discriminator(_profile_tag),
]


async def _name_harnesses(db: AsyncSession, items: list[dict]) -> None:
    """Fill `settings.harness_name` of prompt batches with the harness's current
    name (one query for the whole response)."""
    wanted: dict[str, list[dict]] = {}
    for item in items:
        hid = (item.get("settings") or {}).get("harness_id")
        if item.get("profile") == "prompt" and hid:
            wanted.setdefault(hid, []).append(item)
    if not wanted:
        return
    rows = (
        await db.execute(select(Harness.id, Harness.name).where(Harness.id.in_([uuid.UUID(h) for h in wanted])))
    ).all()
    for hid, name in rows:
        for item in wanted.get(str(hid), []):
            item["settings"]["harness_name"] = name


async def _collection_detail(db: AsyncSession, c: Batch) -> dict:
    """The batch + `targets` + `pending_reviews` (draft review findings of this
    batch, each with its `target_id`) + `pending_maps`, the same list under its
    old name."""
    await db.refresh(c)
    targets = await service.load_targets(db, [c.id])
    runs = await service.load_runs(db, targets)
    profile = service.profile_for(c)
    maps = await service.load_review_findings(db, profile, targets, c)
    overview = await evaluation.evaluation_overview(db, [c.id])
    out = service.collection_out(c, targets, runs, overview.get(c.id))
    briefs = await evaluation.target_briefs(db, c, targets)
    out["targets"] = [
        {
            **service.target_out(t, runs.get(t.run_id), maps.get(t.run_id, []) if t.run_id else [], profile),
            "evaluation": briefs.get(str(t.id)),
        }
        for t in targets
    ]
    pending = []
    for t in targets:
        for f in maps.get(t.run_id, []) if t.run_id else []:
            if f.status == "draft":
                pending.append({**_finding_out(f), "target_id": str(t.id)})
    out["pending_reviews"] = pending
    out["pending_maps"] = pending
    await _name_harnesses(db, [out])
    return out


async def _target_detail(db: AsyncSession, t: BatchTarget) -> dict:
    await db.refresh(t)
    runs = await service.load_runs(db, [t])
    c = await db.get(Batch, t.collection_id)
    profile = service.profile_for(c)
    maps = await service.load_review_findings(db, profile, [t], c)
    return service.target_out(
        t, runs.get(t.run_id), maps.get(t.run_id, []) if t.run_id else [], profile
    )


async def _target_in(db: AsyncSession, c: Batch, target_id: uuid.UUID) -> BatchTarget:
    t = await db.get(BatchTarget, target_id)
    if t is None or t.collection_id != c.id:
        raise HTTPException(404, "Target not found")
    return t


@_both("post", "")
async def create_batch(
    body: CreateBatchBody,
    request: Request,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(require_workspace_approver),
    db: AsyncSession = Depends(get_db),
):
    lifecycle.refuse_if_draining(request)
    c, _ = await service.create_batch(
        db, workspace_id=ctx.id, user_id=user.id, body=body, profile_slug=body.profile
    )
    return await _collection_detail(db, c)


async def _list_rows(db: AsyncSession, ctx: WorkspaceContext) -> list[dict]:
    batches = list(
        (
            await db.execute(
                select(Batch)
                .where(Batch.workspace_id == ctx.id)
                .order_by(Batch.created_at.desc())
            )
        ).scalars()
    )
    targets = await service.load_targets(db, [c.id for c in batches])
    runs = await service.load_runs(db, targets)
    by_batch: dict[uuid.UUID, list[BatchTarget]] = {}
    for t in targets:
        by_batch.setdefault(t.collection_id, []).append(t)
    overview = await evaluation.evaluation_overview(db, [c.id for c in batches])
    rows = [service.collection_out(c, by_batch.get(c.id, []), runs, overview.get(c.id)) for c in batches]
    await _name_harnesses(db, rows)
    return rows


@router.get("/batches")
async def list_batches(
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    return {"batches": await _list_rows(db, ctx)}


@router.get("/collections", deprecated=True)
async def list_collections(
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    return {"collections": await _list_rows(db, ctx)}


class EvaluateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    judge_model: str | None = Field(default=None, max_length=200)
    budget_usd: float | None = Field(default=None, gt=0, le=100_000)
    only_missing: bool = True
    target_ids: list[uuid.UUID] | None = None


class ReviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: Literal["agree", "disagree", "adjust"]
    adjusted_quality: float | None = Field(default=None, ge=0, le=100)
    note: str | None = Field(default=None, max_length=500)


# `/compare` and `/rubrics/default` are declared before the `/{collection_id}`
# routes: a UUID path parameter would otherwise answer 422 for these words.
@_both("get", "/compare")
async def compare_batches(
    a: uuid.UUID,
    b: uuid.UUID,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    if a == b:
        raise HTTPException(422, "Pick two different batches to compare")
    batch_a = await service.get_collection(db, a, ctx.id)
    batch_b = await service.get_collection(db, b, ctx.id)
    return await scoring.compare_batches(db, batch_a, batch_b)


class PairwiseStartBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    a: uuid.UUID
    b: uuid.UUID
    scope: Literal["near_ties", "both_delivered"] = "near_ties"
    max_pairs: int = Field(default=20, ge=1, le=100)
    budget_usd: float | None = Field(default=None, gt=0, le=100_000)


class PairwiseCancelBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    a: uuid.UUID
    b: uuid.UUID


@_both("post", "/compare/pairwise")
async def start_pairwise(
    body: PairwiseStartBody,
    request: Request,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(require_workspace_approver),
    db: AsyncSession = Depends(get_db),
):
    """Start the blind pairwise judge over the shared rows of two batches (see
    `tret/batches/pairwise.py`); returns the pair's status."""
    lifecycle.refuse_if_draining(request)
    if body.a == body.b:
        raise HTTPException(422, "Pick two different batches to compare")
    batch_a = await service.get_collection(db, body.a, ctx.id)
    batch_b = await service.get_collection(db, body.b, ctx.id)
    return await pairwise.start_pairwise(
        db, batch_a, batch_b, user_id=user.id, scope=body.scope,
        max_pairs=body.max_pairs, budget_usd=body.budget_usd,
    )


@_both("get", "/compare/pairwise")
async def get_pairwise(
    a: uuid.UUID,
    b: uuid.UUID,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    if a == b:
        raise HTTPException(422, "Pick two different batches to compare")
    batch_a = await service.get_collection(db, a, ctx.id)
    batch_b = await service.get_collection(db, b, ctx.id)
    return await pairwise.pairwise_status(db, batch_a, batch_b, with_rows=True)


@_both("post", "/compare/pairwise/cancel")
async def cancel_pairwise(
    body: PairwiseCancelBody,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(require_workspace_approver),
    db: AsyncSession = Depends(get_db),
):
    batch_a = await service.get_collection(db, body.a, ctx.id)
    batch_b = await service.get_collection(db, body.b, ctx.id)
    return await pairwise.cancel_pairwise(db, batch_a, batch_b)


@_both("get", "/families")
async def list_batch_families(
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    return {"families": await families.list_families(db, ctx.id)}


@_both("get", "/family")
async def get_batch_family(
    key: str,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    if not await families.family_batches(db, ctx.id, key):
        raise HTTPException(404, "No batch of that family in this workspace")
    return await families.family_leaderboard(db, ctx.id, key)


class AdviceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: Literal["disclosure", "prompt"]
    fiscal_year: int | None = Field(default=None, ge=2000, le=2100)
    harness_id: uuid.UUID | None = None
    template: str | None = Field(default=None, max_length=50_000)


class ExperimentArmBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str | None = Field(default=None, max_length=80)
    model_override: str | None = Field(default=None, max_length=200)
    objective: str | None = Field(default=None, max_length=40)
    max_iterations: int | None = None
    weight: float | None = None

    @field_validator("weight", mode="before")
    @classmethod
    def _weight_is_a_number(cls, v):
        if isinstance(v, (bool, str)):
            raise ValueError("weight must be a number")
        return v


class CreateExperimentBody(BaseModel):
    """`batch` is the normal create body (either profile); every arm batch is a copy of it."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    mode: Literal["replicate", "split"]
    arms: list[ExperimentArmBody] = Field(min_length=2, max_length=3)
    sample_n: int | None = None
    batch: CreateBatchBody


class PromoteExperimentBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    arm: Literal["A", "B", "C"]
    # The `token` of the arm's `promotion` as the person saw it; a stale one is a 409.
    token: str | None = Field(default=None, max_length=64)


@router.post("/batches/experiments")
async def create_experiment(
    body: CreateExperimentBody,
    request: Request,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(require_workspace_approver),
    db: AsyncSession = Depends(get_db),
):
    lifecycle.refuse_if_draining(request)
    spec = experiments.ExperimentSpec(
        name=body.name,
        mode=body.mode,
        arms=[a.model_dump(exclude_none=True) for a in body.arms],
        batch=body.batch,
        sample_n=body.sample_n,
    )
    e = await experiments.create_experiment(db, ctx, user, spec)
    return await experiments.experiment_result(db, e)


@router.get("/batches/experiments")
async def list_experiments(
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    return {"experiments": await experiments.list_experiments(db, ctx.id)}


@router.get("/batches/experiments/{experiment_id}")
async def get_experiment(
    experiment_id: uuid.UUID,
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    e = await experiments.get_experiment(db, experiment_id, ctx.id)
    return await experiments.experiment_result(db, e)


@router.post("/batches/experiments/{experiment_id}/start")
async def start_experiment(
    experiment_id: uuid.UUID,
    request: Request,
    ctx: WorkspaceContext = Depends(require_workspace_approver),
    db: AsyncSession = Depends(get_db),
):
    lifecycle.refuse_if_draining(request)
    e = await experiments.get_experiment(db, experiment_id, ctx.id)
    await experiments.start_experiment(db, e)
    await db.refresh(e)
    return await experiments.experiment_result(db, e)


@router.post("/batches/experiments/{experiment_id}/promote")
async def promote_experiment(
    experiment_id: uuid.UUID,
    body: PromoteExperimentBody,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(require_workspace_admin),
    db: AsyncSession = Depends(get_db),
):
    e = await experiments.get_experiment(db, experiment_id, ctx.id)
    return await experiments.promote(db, e, body.arm, user, body.token)


@router.post("/batches/advice")
async def batch_advice(
    body: AdviceBody,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """What earlier batches of the family a new batch would join suggest for it.
    Read-only; nothing is applied. See docs/batch-runs.md."""
    if body.profile == "prompt" and (body.harness_id is None or not (body.template or "").strip()):
        raise HTTPException(422, "A prompt batch's advice needs harness_id and template")
    fiscal_year = body.fiscal_year if body.fiscal_year is not None else (2025 if body.profile == "disclosure" else None)
    return await advice.batch_advice(
        db,
        ctx.id,
        profile=body.profile,
        fiscal_year=fiscal_year,
        harness_id=body.harness_id,
        template=body.template,
    )


@_both("get", "/rubrics/default")
async def default_batch_rubric(
    profile: Literal["prompt", "disclosure"] = "prompt",
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
):
    return default_rubric(profile)


@_both("get", "/{collection_id}")
async def get_collection(
    collection_id: uuid.UUID,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    c = await service.get_collection(db, collection_id, ctx.id)
    return await _collection_detail(db, c)


@_both("get", "/{collection_id}/scorecard")
async def get_scorecard(
    collection_id: uuid.UUID,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    c = await service.get_collection(db, collection_id, ctx.id)
    return await scoring.batch_scorecard(db, c)


@_both("get", "/{collection_id}/evaluation")
async def get_evaluation(
    collection_id: uuid.UUID,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    c = await service.get_collection(db, collection_id, ctx.id)
    return await evaluation.evaluation_detail(db, c)


@_both("post", "/{collection_id}/evaluate")
async def evaluate_batch(
    collection_id: uuid.UUID,
    request: Request,
    body: EvaluateBody | None = None,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(require_workspace_approver),
    db: AsyncSession = Depends(get_db),
):
    """Start an evaluation pass over the batch, whatever its status (a finalized
    batch can be evaluated after the fact)."""
    lifecycle.refuse_if_draining(request)
    c = await service.get_collection(db, collection_id, ctx.id)
    body = body or EvaluateBody()
    ev = await evaluation.start_evaluation(
        db,
        c,
        user_id=user.id,
        judge_model=body.judge_model,
        budget_usd=body.budget_usd,
        only_missing=body.only_missing,
        target_ids=body.target_ids,
    )
    return await evaluation.pass_summary(db, ev)


@_both("post", "/{collection_id}/evaluation/cancel")
async def cancel_evaluation(
    collection_id: uuid.UUID,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(require_workspace_approver),
    db: AsyncSession = Depends(get_db),
):
    c = await service.get_collection(db, collection_id, ctx.id)
    ev = await evaluation.cancel_evaluation(db, c)
    return await evaluation.pass_summary(db, ev)


@_both("post", "/{collection_id}/targets/{target_id}/evaluation/review")
async def review_target_evaluation(
    collection_id: uuid.UUID,
    target_id: uuid.UUID,
    body: ReviewBody,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(require_workspace_approver),
    db: AsyncSession = Depends(get_db),
):
    c = await service.get_collection(db, collection_id, ctx.id)
    t = await _target_in(db, c, target_id)
    return await evaluation.review_target(
        db, c, t, user, verdict=body.verdict, adjusted_quality=body.adjusted_quality, note=body.note
    )


@_both("post", "/{collection_id}/start")
async def start_collection(
    collection_id: uuid.UUID,
    request: Request,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(require_workspace_approver),
    db: AsyncSession = Depends(get_db),
):
    lifecycle.refuse_if_draining(request)
    c = await service.get_collection(db, collection_id, ctx.id)
    await service.start(db, c)
    return await _collection_detail(db, c)


@_both("post", "/{collection_id}/pause")
async def pause_collection(
    collection_id: uuid.UUID,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(require_workspace_approver),
    db: AsyncSession = Depends(get_db),
):
    c = await service.get_collection(db, collection_id, ctx.id)
    await service.pause(db, c)
    return await _collection_detail(db, c)


@_both("post", "/{collection_id}/resume")
async def resume_collection(
    collection_id: uuid.UUID,
    request: Request,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(require_workspace_approver),
    db: AsyncSession = Depends(get_db),
):
    lifecycle.refuse_if_draining(request)
    c = await service.get_collection(db, collection_id, ctx.id)
    await service.resume(db, c)
    return await _collection_detail(db, c)


@_both("post", "/{collection_id}/release")
async def release_collection(
    collection_id: uuid.UUID,
    request: Request,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(require_workspace_approver),
    db: AsyncSession = Depends(get_db),
):
    lifecycle.refuse_if_draining(request)
    c = await service.get_collection(db, collection_id, ctx.id)
    await service.release(db, c)
    return await _collection_detail(db, c)


@_both("post", "/{collection_id}/targets/{target_id}/retry")
async def retry_target(
    collection_id: uuid.UUID,
    target_id: uuid.UUID,
    request: Request,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(require_workspace_approver),
    db: AsyncSession = Depends(get_db),
):
    lifecycle.refuse_if_draining(request)
    c = await service.get_collection(db, collection_id, ctx.id)
    t = await _target_in(db, c, target_id)
    await service.retry_target(db, c, t)
    return await _target_detail(db, t)


@_both("post", "/{collection_id}/targets/{target_id}/retry-write")
async def retry_target_write(
    collection_id: uuid.UUID,
    target_id: uuid.UUID,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(require_workspace_approver),
    db: AsyncSession = Depends(get_db),
):
    c = await service.get_collection(db, collection_id, ctx.id)
    t = await _target_in(db, c, target_id)
    await service.retry_write(db, c, t)
    return await _target_detail(db, t)


@_both("get", "/{collection_id}/records")
async def preview_records(
    collection_id: uuid.UUID,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    c = await service.get_collection(db, collection_id, ctx.id)
    return await service.profile_for(c).records(db, c)


@_both("post", "/{collection_id}/finalize")
async def finalize_collection(
    collection_id: uuid.UUID,
    body: service.FinalizeBody | None = None,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(require_workspace_approver),
    db: AsyncSession = Depends(get_db),
):
    c = await service.get_collection(db, collection_id, ctx.id)
    await service.finalize(db, c, force=bool(body and body.force))
    return await _collection_detail(db, c)


@_both("get", "/{collection_id}/records/{name}")
async def download_record(
    collection_id: uuid.UUID,
    name: str,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """A finalized record file kept as a Document (a batch with no write target)."""
    c = await service.get_collection(db, collection_id, ctx.id)
    entry = next(
        (f for f in (c.finalize or {}).get("files") or [] if f.get("name") == name and f.get("document_id")),
        None,
    )
    doc = await db.get(Document, uuid.UUID(entry["document_id"])) if entry else None
    if doc is None or (doc.meta or {}).get("batch_id") != str(c.id):
        raise HTTPException(404, "This batch has no stored record file with that name")
    try:
        data = await document_bytes(doc)
    except FileNotFoundError as e:
        raise HTTPException(404, "The record file is no longer in storage") from e
    return Response(
        content=data,
        media_type=doc.content_type,
        headers={"Content-Disposition": f'attachment; filename="{doc.filename}"'},
    )


@_both("get", "/{collection_id}/targets/{target_id}/files")
async def get_target_file(
    collection_id: uuid.UUID,
    target_id: uuid.UUID,
    path: str,
    download: bool = False,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """The bytes of one file written for a target (a package file uploaded to
    SharePoint), for the in-app viewer. `path` must exactly match a live
    `written_files` filename; the content type comes from a fixed allowlist by
    extension, never from the stored row."""
    c = await service.get_collection(db, collection_id, ctx.id)
    t = await _target_in(db, c, target_id)
    entry = next((w for w in written_files_latest(t) if w.get("filename") == path), None)
    if entry is None:
        raise HTTPException(404, "This target has no written file at that path")
    try:
        doc_id = uuid.UUID(str(entry.get("document_id")))
    except ValueError as e:
        raise HTTPException(404, "This target has no written file at that path") from e
    doc = await db.get(Document, doc_id)
    if doc is None or doc.project_id != c.project_id or not doc.storage_path:
        raise HTTPException(404, "This target has no written file at that path")
    name = path.rsplit("/", 1)[-1].replace('"', "").replace("\r", "").replace("\n", "")
    ext = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
    media_type = _FILE_TYPES.get(ext, "application/octet-stream")
    capped = not download and media_type != "application/pdf"
    size = entry.get("byte_size")

    def too_big(n: int) -> HTTPException:
        return HTTPException(
            413, f"{name} is {n / 1048576:.1f} MB, over the {INLINE_TEXT_MAX_BYTES // 1048576} MB inline view limit; download it instead."
        )

    if capped and isinstance(size, int) and size > INLINE_TEXT_MAX_BYTES:
        raise too_big(size)
    # No M365 fallback: written_files keep only web_url, not the drive/item id.
    try:
        data = await document_bytes(doc)
    except FileNotFoundError as e:
        raise HTTPException(404, "The file is no longer in storage") from e
    if capped and len(data) > INLINE_TEXT_MAX_BYTES:
        raise too_big(len(data))
    disposition = "attachment" if download else "inline"
    ascii_name = name.encode("ascii", "replace").decode("ascii")  # header values are latin-1
    return Response(
        content=data,
        media_type=media_type,
        headers={
            "Content-Disposition": f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name, safe='')}",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )
