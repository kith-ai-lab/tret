from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tret.adaptive import validation_error as adaptive_validation_error
from tret.api.auth import current_user
from tret.api.workspace import WorkspaceContext, current_workspace
from tret.db.engine import get_db
from tret.db.models import Harness, Pack, User
from tret.engine.context import assemble_system_prompt, task_config
from tret.engine.tools import WEB_TOOL_NAMES, get_builtin_tools, withheld_web_tools
from tret.packs.links import (
    pack_map_for_harnesses,
    packs_for_harness,
    resolve_pack_for_task,
    set_harness_packs,
    task_slug_collision,
)
from tret.providers.catalog import get_catalog
from tret.router_llm.objectives import DEFAULT_OBJECTIVE, OBJECTIVES
from tret.router_llm.router import TIER_ORDER

router = APIRouter(prefix="/api/harnesses", tags=["harnesses"])


class HarnessBody(BaseModel):
    name: str
    description: str | None = None
    # Ordered — position 0 is the harness's primary pack. `pack_id` is the
    # legacy single-pack field: when `pack_ids` is empty and `pack_id` is set,
    # it is treated as a one-element `pack_ids` list (see `_resolve_pack_ids`).
    pack_id: uuid.UUID | None = None
    pack_ids: list[uuid.UUID] = Field(default_factory=list)
    task_profile: str = "freeform"
    system_prompt_extra: str | None = None
    model_policy: dict = Field(default_factory=lambda: {"mode": "auto"})
    tool_names: list[str] = Field(default_factory=list)
    loop_config: dict = Field(default_factory=dict)


async def _resolve_pack_ids(
    db: AsyncSession, ctx: WorkspaceContext, body: HarnessBody
) -> list[uuid.UUID]:
    """The ordered pack ids a create/update body names, validated to exist in
    this workspace and to declare no colliding task_type slug. 404s on an
    unknown pack id (named explicitly); 422s on a duplicate pack id or a
    task_type collision (naming the slug and both offending pack slugs).
    """
    pack_ids = list(body.pack_ids) if body.pack_ids else ([body.pack_id] if body.pack_id else [])
    if not pack_ids:
        return []
    # Refused, not silently deduped: the list is ordered (position 0 is the
    # primary pack), so dropping a duplicate would quietly change which
    # position later packs land at. Without this check the duplicate reaches
    # the composite PK on harness_packs and surfaces as a 500.
    dupes = sorted({str(pid) for pid in pack_ids if pack_ids.count(pid) > 1})
    if dupes:
        raise HTTPException(422, f"pack_ids lists the same pack more than once: {', '.join(dupes)}")
    found = {
        p.id: p
        for p in (
            await db.execute(
                select(Pack).where(Pack.workspace_id == ctx.id, Pack.id.in_(pack_ids))
            )
        )
        .scalars()
        .all()
    }
    missing = [str(pid) for pid in pack_ids if pid not in found]
    if missing:
        raise HTTPException(404, f"pack not found: {', '.join(missing)}")
    ordered_packs = [found[pid] for pid in pack_ids]
    slug = task_slug_collision(ordered_packs)
    if slug:
        owners = sorted(
            p.slug for p in ordered_packs if any(t.get("slug") == slug for t in p.manifest.get("task_types", []))
        )
        raise HTTPException(
            422,
            f"task_type '{slug}' is declared by more than one linked pack: {', '.join(owners)}",
        )
    return pack_ids


def _out(h: Harness, packs: list[Pack] | None = None) -> dict:
    packs = packs or []
    return {
        "id": str(h.id),
        "name": h.name,
        "description": h.description,
        # Legacy single-pack fields: the primary (first) linked pack, or null.
        "pack_id": str(packs[0].id) if packs else None,
        "pack_slug": packs[0].slug if packs else None,
        "pack_ids": [str(p.id) for p in packs],
        "pack_slugs": [p.slug for p in packs],
        "task_profile": h.task_profile,
        "system_prompt_extra": h.system_prompt_extra,
        "model_policy": h.model_policy,
        "tool_names": h.tool_names,
        "loop_config": h.loop_config,
        "is_archived": h.is_archived,
        "updated_at": h.updated_at.isoformat() if h.updated_at else None,
    }


def _validate_tool_names(tool_names: list[str]) -> None:
    """Reject a harness tool the engine has no builtin for.

    The engine offers a run exactly the tools it can resolve
    (`engine/harness.py`: `builtins[n] for n in enabled_names`), so before this
    check a single typo — `read_documents` for `read_document` — silently removed
    a capability rather than failing: the harness saved, the builder UI echoed the
    name back, and the model simply never saw the tool. That is the same failure
    mode `packs/loader.py` already rejects at install for a *pack*-declared tool
    list; a harness is the other place a tool name is written by hand, so it gets
    the identical treatment.

    Duplicates are left alone — they are harmless (the engine builds a spec list
    the provider dedupes by name) and are not evidence of a mistake the way an
    unknown name is.
    """
    builtins = get_builtin_tools()
    unknown = [n for n in tool_names if n not in builtins]
    if unknown:
        raise HTTPException(
            422,
            f"unknown tool name(s): {', '.join(sorted(set(unknown)))}. "
            f"Available tools: {', '.join(sorted(builtins))}",
        )


def _validate_policy(policy: dict) -> None:
    mode = policy.get("mode")
    if mode not in ("auto", "pinned"):
        raise HTTPException(422, "model_policy.mode must be 'auto' or 'pinned'")
    catalog = get_catalog()
    if mode == "pinned":
        model = policy.get("model")
        if not model or catalog.get(model) is None:
            raise HTTPException(422, f"model_policy.model '{model}' is not in the catalog")
    for m in policy.get("allowed") or []:
        if catalog.get(m) is None:
            raise HTTPException(422, f"allowed model '{m}' is not in the catalog")
    # `local` is a real ceiling, not a floor: TIER_ORDER ranks it below economy,
    # so capping there leaves local models as the only candidates — the
    # zero-cloud policy (docs/local-models.md), expressed as a cost tier.
    tier = policy.get("max_cost_tier", "premium")
    if tier not in TIER_ORDER:
        raise HTTPException(
            422, f"max_cost_tier must be one of {'|'.join(TIER_ORDER)}"
        )
    # An unrecognized objective must never fall through to the default: silently
    # routing on "balanced" when the operator asked for "eco" is exactly the kind
    # of quiet substitution this platform exists to rule out.
    # `or` (not a get default) so unset/None reads as the default exactly the way
    # router_llm.objectives.objective_of reads it at run time.
    objective = policy.get("objective") or DEFAULT_OBJECTIVE
    if objective not in OBJECTIVES:
        raise HTTPException(422, f"model_policy.objective must be one of {'|'.join(OBJECTIVES)}")
    # The adaptive block, same rule as everything above it: refused at the door,
    # never read as a default. A misspelled key here would silently leave a
    # behavior on that the operator believed they had turned off — and two of
    # them (`escalation`, `compaction`) let a run change what it is doing
    # mid-flight, which is exactly the kind of thing to be sure about.
    problem = adaptive_validation_error(policy.get("adaptive"))
    if problem:
        raise HTTPException(422, problem)


@router.get("")
async def list_harnesses(
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    harnesses = (
        (
            await db.execute(
                select(Harness)
                .where(Harness.workspace_id == ctx.id, Harness.is_archived.is_(False))
                .order_by(Harness.name)
            )
        )
        .scalars()
        .all()
    )
    pack_map = await pack_map_for_harnesses(db, [h.id for h in harnesses])
    return [_out(h, pack_map.get(h.id)) for h in harnesses]


@router.get("/{harness_id}")
async def get_harness(
    harness_id: uuid.UUID,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    h = await db.get(Harness, harness_id)
    # Cross-workspace: 404, not 403 — a member of one workspace must not be
    # able to tell a harness in another exists at all.
    if h is None or h.workspace_id != ctx.id:
        raise HTTPException(404, "Harness not found")
    packs = await packs_for_harness(db, h)
    out = _out(h, packs)
    # The pack this harness's own task_profile resolves to — the doctrine and
    # schemas a *run of this harness's default task* would load. A pack task
    # type can widen the tool list beyond the harness's, so this reads both.
    pack = resolve_pack_for_task(packs, h.task_profile)
    schemas = pack.manifest.get("schemas", {}) if pack else {}
    task = task_config(pack, h.task_profile)
    run_tools = list((task or {}).get("tools") or h.tool_names or [])
    out["assembled_system_prompt"] = assemble_system_prompt(
        h,
        pack,
        h.task_profile,
        schemas,
        web_tools_enabled=any(
            name in WEB_TOOL_NAMES for name in run_tools if name not in withheld_web_tools(run_tools)
        ),
    )
    # Union of every linked pack's task types, in link order, each entry
    # tagged with the pack it came from — a two-pack harness can run task
    # types from either.
    if packs:
        out["task_types"] = [
            {**t, "pack_slug": p.slug, "pack_id": str(p.id)}
            for p in packs
            for t in p.manifest.get("task_types", [])
        ]
    return out


@router.post("")
async def create_harness(
    body: HarnessBody,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    _validate_policy(body.model_policy)
    _validate_tool_names(body.tool_names)
    pack_ids = await _resolve_pack_ids(db, ctx, body)
    dupe = (
        await db.execute(
            select(Harness).where(
                Harness.workspace_id == ctx.id,
                Harness.name == body.name,
                Harness.is_archived.is_(False),
            )
        )
    ).scalars().first()
    if dupe:
        raise HTTPException(409, f"A harness named '{body.name}' already exists")
    h = Harness(
        workspace_id=ctx.id,
        created_by=user.id,
        **body.model_dump(exclude={"pack_id", "pack_ids"}),
    )
    if not h.loop_config:
        h.loop_config = {"max_iterations": 24, "max_output_tokens": 8192, "temperature": 0.2}
    db.add(h)
    await db.flush()  # populate h.id for the links below
    await set_harness_packs(db, h, pack_ids)
    await db.commit()
    packs = await packs_for_harness(db, h)
    return _out(h, packs)


@router.put("/{harness_id}")
async def update_harness(
    harness_id: uuid.UUID,
    body: HarnessBody,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    h = await db.get(Harness, harness_id)
    if h is None or h.workspace_id != ctx.id:
        raise HTTPException(404, "Harness not found")
    _validate_policy(body.model_policy)
    _validate_tool_names(body.tool_names)
    pack_ids = await _resolve_pack_ids(db, ctx, body)
    for field, value in body.model_dump(exclude={"pack_id", "pack_ids"}).items():
        setattr(h, field, value)
    await set_harness_packs(db, h, pack_ids)
    await db.commit()
    packs = await packs_for_harness(db, h)
    return _out(h, packs)


@router.delete("/{harness_id}")
async def archive_harness(
    harness_id: uuid.UUID,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    h = await db.get(Harness, harness_id)
    if h is None or h.workspace_id != ctx.id:
        raise HTTPException(404, "Harness not found")
    h.is_archived = True
    await db.commit()
    return {"ok": True}
