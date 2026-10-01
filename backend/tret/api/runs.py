from __future__ import annotations

import asyncio
import base64
import binascii
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from tret.api.auth import current_user
from tret.api.workspace import WorkspaceContext, current_project, current_workspace, project_in_workspace
from tret.db.engine import get_db
from tret.db.models import Document, Harness, Project, Run, User
from tret.engine.delegation import COST_CAP_KEY, DELEGATION_DEPTH_KEY, LABEL_KEY
from tret.engine.events import get_event_bus
from tret.engine.harness import get_harness_engine
from tret.packs.links import packs_for_harness, resolve_pack_for_task
from tret.services import lifecycle
from tret.services.emissions import emission_summary_fields, energy_wh_field
from tret.services.emissions_v3_wiring import method_v3_of, public_routing

router = APIRouter(prefix="/api/runs", tags=["runs"])

_background_tasks: set[asyncio.Task] = set()


class CreateRunBody(BaseModel):
    harness_id: uuid.UUID
    project_id: uuid.UUID | None = None
    task_type: str = "freeform"
    task_input: dict = Field(default_factory=dict)
    document_ids: list[uuid.UUID] = Field(default_factory=list)
    model_override: str | None = None


def _run_summary(run: Run) -> dict:
    return {
        "id": str(run.id),
        "project_id": str(run.project_id),
        "harness_id": str(run.harness_id),
        # The chat turn (or delegation off one) that produced this run. Null
        # for a run that never began as a chat turn (workbench, a scheduled
        # task) — permanently, not a gap to be backfilled later.
        "conversation_id": str(run.conversation_id) if run.conversation_id else None,
        # Delegation lineage (`run_harness_task`, engine/tools.py). All four
        # null for a run nothing delegated to and that delegated nothing.
        "parent_run_id": str(run.parent_run_id) if run.parent_run_id else None,
        "root_run_id": str(run.root_run_id) if run.root_run_id else None,
        "delegation_kind": run.delegation_kind,
        "delegation_batch_id": str(run.delegation_batch_id) if run.delegation_batch_id else None,
        # The model-written name the caller gave this child via the
        # delegation tool's `label` argument (`_prepare_child`, engine/
        # tools.py), stamped onto task_input under LABEL_KEY. None for a run
        # nothing delegated to, or one delegated without a label.
        "delegation_label": (run.task_input or {}).get(LABEL_KEY),
        "task_type": run.task_type,
        "status": run.status,
        "model_used": run.model_used,
        "provider_used": run.provider_used,
        # `method_v3` lives inside the persisted routing blob but is its own
        # field (get_run); `routing` here is exactly what it was before v3.
        "routing": public_routing(run.routing),
        "input_tokens": run.input_tokens,
        "output_tokens": run.output_tokens,
        "cache_read_tokens": run.cache_read_tokens,
        "cache_write_tokens": run.cache_write_tokens,
        "cost_usd": float(run.cost_usd or 0),
        # Best-known actual cost, alongside the catalog-priced cost_usd above:
        # per turn, the provider-reported figure when one exists (OpenRouter
        # only today), the catalog price otherwise. Equals cost_usd when no
        # turn reported. None only when the run spent nothing at all.
        "reported_cost_usd": (
            float(run.reported_cost_usd) if run.reported_cost_usd is not None else None
        ),
        # What this run has caused OTHER runs to spend through delegation —
        # never rolled into cost_usd/reported_cost_usd above. See
        # Run.delegated_cost_usd's own comment (db/models.py).
        "delegated_cost_usd": float(run.delegated_cost_usd or 0),
        # Estimated ecological cost alongside the dollar cost. None (not 0) for
        # runs that predate eco accounting: no estimate is not the same as none
        # drawn. Full derivation is in the detail view's "energy" block.
        "energy_wh": energy_wh_field(run.energy_wh),
        # co2e_g (run total), scope2_g, scope3_g, avoided_co2e_g, avoided_usd and
        # the judgment band (co2e_g_low/high) — read as recorded from the run's
        # own accounting block, never recomputed at today's factors, and null
        # wherever the run has no figure. Runs recorded before scopes, the
        # baseline, money or the band existed report null for those, not 0.
        **emission_summary_fields(run.energy_accounting),
        "iterations": run.iterations,
        "error": run.error,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }


@router.post("")
async def create_run(
    body: CreateRunBody,
    request: Request,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    lifecycle.refuse_if_draining(request)
    harness = await db.get(Harness, body.harness_id)
    if harness is None or harness.is_archived or harness.workspace_id != ctx.id:
        raise HTTPException(404, "Harness not found")
    project_id = body.project_id
    if project_id is None:
        project = await current_project(db, ctx.id)
        if project is None:
            raise HTTPException(500, "No project exists")
        project_id = project.id
    elif await project_in_workspace(db, project_id, ctx.id) is None:
        raise HTTPException(404, "Project not found")

    if body.document_ids:
        # Every attached document must belong to *this* workspace — the engine
        # loads them by id alone at harness.py:288, with no workspace filter
        # of its own, so an unvalidated id here is a cross-workspace document
        # read. A join through the document's project, not a workspace_id on
        # Document itself: documents don't carry one directly.
        found_ids = (
            await db.execute(
                select(Document.id)
                .join(Project, Document.project_id == Project.id)
                .where(Document.id.in_(body.document_ids), Project.workspace_id == ctx.id)
            )
        ).scalars().all()
        if set(found_ids) != set(body.document_ids):
            raise HTTPException(404, "Document not found")

    # Delegation plumbing is the engine's to stamp (engine/tools.py::
    # `_prepare_child`), never a caller's: a hand-set `_delegation_depth`
    # would make a root run look like a delegated child — which a hosting
    # extension's pre-run gate reads as "the parent already holds credit for this" — a
    # negative one would buy extra hops, and a hand-set `_label` would let a
    # caller forge a delegation label onto a run nothing actually delegated to.
    task_input = {
        k: v
        for k, v in body.task_input.items()
        if k not in (DELEGATION_DEPTH_KEY, COST_CAP_KEY, LABEL_KEY)
    }
    if body.model_override:
        task_input["_model_override"] = body.model_override

    packs = await packs_for_harness(db, harness)
    pack = resolve_pack_for_task(packs, body.task_type)

    run = Run(
        project_id=project_id,
        harness_id=harness.id,
        pack_id=pack.id if pack else None,
        task_type=body.task_type,
        task_input=task_input,
        document_ids=body.document_ids,
        created_by=user.id,
    )
    db.add(run)
    await db.commit()

    engine = get_harness_engine()
    task = asyncio.create_task(engine.execute(run.id))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return {"run_id": str(run.id)}


def _encode_run_cursor(run: Run) -> str:
    """Opaque keyset cursor: this row's own (created_at, id) — the point
    `list_runs` resumes strictly after on the next page."""
    raw = f"{run.created_at.isoformat()}|{run.id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_run_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        ts_raw, id_raw = raw.rsplit("|", 1)
        ts = datetime.fromisoformat(ts_raw)
        # Postgres always returns a tz-aware TIMESTAMPTZ; sqlite (dev/test)
        # drops tzinfo on the round trip — same normalisation api/workspaces.py
        # applies to `expires_at` for the same reason. Every `created_at` this
        # codebase writes is UTC either way.
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts, uuid.UUID(id_raw)
    except (ValueError, binascii.Error) as exc:
        raise HTTPException(422, "Invalid cursor") from exc


@router.get("")
async def list_runs(
    limit: int | None = Query(default=None, ge=1, le=200),
    cursor: str | None = None,
    top_level_only: bool = False,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """List this workspace's runs, newest first.

    Passing neither `limit` nor `cursor` returns the original bare list
    (capped at 50), byte-identical to every caller before paging existed.
    Passing either switches to `{"items": [...], "next_cursor": ...}`: keyset
    pagination on (created_at desc, id desc) — an id tiebreak because
    `created_at` alone is not unique (bulk-seeded or same-second rows) —
    rather than an OFFSET, so a run created mid-page can never shift
    already-seen rows into the next page or duplicate one across pages.
    `next_cursor` is omitted (null) once the last page is reached.
    `top_level_only=true` hides delegated runs (`parent_run_id IS NOT NULL`),
    leaving only runs a person, a schedule, or the API started directly;
    it defaults to false, which keeps today's behaviour unchanged.
    """
    paged = limit is not None or cursor is not None
    effective_limit = limit if limit is not None else 50

    project = await current_project(db, ctx.id)
    if project is None:
        return {"items": [], "next_cursor": None} if paged else []

    query = select(Run).where(Run.project_id == project.id)
    if top_level_only:
        query = query.where(Run.parent_run_id.is_(None))
    if cursor is not None:
        last_created_at, last_id = _decode_run_cursor(cursor)
        query = query.where(
            or_(
                Run.created_at < last_created_at,
                and_(Run.created_at == last_created_at, Run.id < last_id),
            )
        )
    query = query.order_by(Run.created_at.desc(), Run.id.desc())

    if not paged:
        runs = (await db.execute(query.limit(effective_limit))).scalars().all()
        return [_run_summary(r) for r in runs]

    # Fetch one extra row to learn whether a next page exists without a
    # second round trip.
    rows = (await db.execute(query.limit(effective_limit + 1))).scalars().all()
    page = rows[:effective_limit]
    next_cursor = _encode_run_cursor(page[-1]) if len(rows) > effective_limit and page else None
    return {"items": [_run_summary(r) for r in page], "next_cursor": next_cursor}


async def _run_tree(db: AsyncSession, run: Run) -> dict | None:
    """Aggregate totals over `run`'s whole delegation tree — root plus every
    descendant — in one indexed query. None when `run` is a root with no
    descendants (`run_count == 1`): a lone run has no tree worth reporting
    beyond its own summary.

    Per-run `cost_usd`/`reported_cost_usd`/`energy_wh` are deliberately never
    rolled up into a parent row's own columns — `api/analytics.py` sums
    `Run.cost_usd` (and friends) straight across every row in a window, so a
    parent that already carried its children's totals would double-count
    them the moment analytics scanned both the parent and the children.
    Tree totals are computed here, on read, instead.

    `reported_cost_usd`: sums `coalesce(reported_cost_usd, cost_usd)` per
    row — the simpler of the two acceptable roll-ups (falling back to the
    catalog price for any row that has accrued cost but no provider-reported
    actual yet), rather than only summing rows that already have a reported
    figure. It reports None only when the whole tree has neither a reported
    figure anywhere nor any catalog cost at all — i.e. nothing has spent
    anything yet — matching `reported_cost_usd`'s own "null means no cost
    accrued" meaning at the per-run level.
    """
    root = run.root_run_id or run.id
    reported_expr = func.coalesce(Run.reported_cost_usd, Run.cost_usd)
    has_reported = case((Run.reported_cost_usd.is_not(None), 1), else_=0)
    row = (
        await db.execute(
            select(
                func.count().label("run_count"),
                func.sum(Run.cost_usd).label("cost_usd"),
                func.sum(reported_expr).label("reported_cost_usd"),
                func.sum(has_reported).label("reported_count"),
                func.sum(Run.energy_wh).label("energy_wh"),
            ).where(
                # A child always inherits its parent's project (`_prepare_child`),
                # so this predicate changes no result today; it is here so tenant
                # scoping is a property of the query, not of how rows get written.
                Run.project_id == run.project_id,
                or_(Run.id == root, Run.root_run_id == root),
            )
        )
    ).one()

    if row.run_count == 1:
        return None

    cost_usd = row.cost_usd or Decimal(0)
    reported_cost_usd = (
        None
        if not row.reported_count and cost_usd == 0
        else float(row.reported_cost_usd or 0)
    )
    return {
        "root_run_id": str(root),
        "run_count": row.run_count,
        "cost_usd": float(cost_usd),
        "reported_cost_usd": reported_cost_usd,
        "energy_wh": energy_wh_field(row.energy_wh),
    }


@router.get("/{run_id}")
async def get_run(
    run_id: uuid.UUID,
    v3: str | None = Query(None, description="'full' returns the whole stored method_v3 block"),
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    run = await db.get(Run, run_id)
    if run is None or await project_in_workspace(db, run.project_id, ctx.id) is None:
        raise HTTPException(404, "Run not found")
    return {**_run_summary(run), "task_input": run.task_input, "messages": run.messages,
            # Aggregate totals over this run's whole delegation tree (root plus
            # every descendant), or None when this run is a root with none.
            # See `_run_tree` for why these are computed on read rather than
            # rolled up into any row's own cost/energy columns.
            "tree": await _run_tree(db, run),
            "document_ids": [str(d) for d in (run.document_ids or [])],
            "doctrine_sha": run.doctrine_sha,
            # What the context was made of, per component, in estimated tokens.
            "context_composition": run.context_composition,
            # How the energy/carbon estimate was arrived at: energy class,
            # Wh/Mtok, the separate input/output token weighting, PUE and its
            # deployment profile, grid intensity and its GHG Protocol basis, the
            # scope split, the same-token carbon *and* dollar counterfactuals, the
            # uncertainty band with its per-factor sensitivity, every constant's
            # provenance under `factors`, and the named biases under `caveats`.
            # All estimates, exactly as recorded when the run happened.
            "energy": run.energy_accounting,
            # Every model this run used, with each one's own full accounting.
            # Null for the ordinary single-model run, where `energy` above is
            # already that model's block. When it is present, `energy` is a
            # roll-up whose per-model factors are null wherever the segments
            # disagreed — this is where the un-nulled detail lives.
            "model_timeline": run.model_timeline,
            # What the run had to stop showing the model to stay inside its
            # context window. `messages` above is always the complete
            # transcript; this is the record of the gap between the two.
            "compactions": run.compactions,
            # Model calls the run made about itself — choosing its model, and
            # summarizing what it elided. Reported beside the run's own cost and
            # energy, never added into them: these ran on different models and
            # possibly different providers, so their energy class and grid basis
            # are their own (services/emissions.overhead_call).
            "overhead": run.overhead,
            # Parallel preview under the revised emissions method (services/
            # emissions_v3_wiring) — never the reported figure. Null for runs
            # that predate it; there is no backfill.
            "method_v3": method_v3_of(run.routing, full=(v3 == "full")),
            # The prose grounding check's verdict (engine/grounding.py):
            # {checked, status, attempts, unsupported, first_unsupported}.
            # Null for any run the check does not apply to.
            "grounding": run.grounding}


@router.get("/{run_id}/children")
async def list_run_children(
    run_id: uuid.UUID,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """Direct children only — a run this one's `run_harness_task` call spawned
    (`parent_run_id == run_id`), not grandchildren. Oldest first, matching how
    a delegation batch's children were created."""
    run = await db.get(Run, run_id)
    if run is None or await project_in_workspace(db, run.project_id, ctx.id) is None:
        raise HTTPException(404, "Run not found")
    children = (
        await db.execute(
            select(Run)
            .where(Run.parent_run_id == run_id, Run.project_id == run.project_id)
            .order_by(Run.created_at.asc(), Run.id.asc())
        )
    ).scalars().all()
    return [_run_summary(c) for c in children]


@router.get("/method-runs/{method_run_id}")
async def get_method_run(
    method_run_id: uuid.UUID,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """Resolve a method/<slug>/<id> citation to its full execution manifest."""
    from tret.db.models import MethodRun

    mr = await db.get(MethodRun, method_run_id)
    if mr is None or await project_in_workspace(db, mr.project_id, ctx.id) is None:
        raise HTTPException(404, "Method run not found")
    return {
        "id": str(mr.id),
        "method_slug": mr.method_slug,
        "run_id": str(mr.run_id) if mr.run_id else None,
        "params": mr.params,
        "code_sha": mr.code_sha,
        "input_summary": mr.input_summary,
        "output_hash": mr.output_hash,
        "row_count": mr.row_count,
        "duration_ms": mr.duration_ms,
        "status": mr.status,
        "error": mr.error,
        "output": mr.output,
        "created_at": mr.created_at.isoformat() if mr.created_at else None,
    }


@router.get("/{run_id}/events")
async def run_events(
    run_id: uuid.UUID,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    run = await db.get(Run, run_id)
    if run is None or await project_in_workspace(db, run.project_id, ctx.id) is None:
        raise HTTPException(404, "Run not found")
    bus = get_event_bus()

    async def stream():
        async for event in bus.subscribe(run_id):
            yield event.to_sse()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/{run_id}/cancel")
async def cancel_run(
    run_id: uuid.UUID,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    run = await db.get(Run, run_id)
    if run is None or await project_in_workspace(db, run.project_id, ctx.id) is None:
        raise HTTPException(404, "Run not found")
    get_harness_engine().cancel(run_id)
    return {"ok": True}
