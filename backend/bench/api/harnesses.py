from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bench.api.auth import current_user
from bench.db.engine import get_db
from bench.db.models import Harness, Pack, User, Workspace
from bench.engine.context import assemble_system_prompt
from bench.engine.tools import get_builtin_tools
from bench.providers.catalog import get_catalog
from bench.router_llm.objectives import DEFAULT_OBJECTIVE, OBJECTIVES
from bench.router_llm.router import TIER_ORDER

router = APIRouter(prefix="/api/harnesses", tags=["harnesses"])


class HarnessBody(BaseModel):
    name: str
    description: str | None = None
    pack_id: uuid.UUID | None = None
    task_profile: str = "freeform"
    system_prompt_extra: str | None = None
    model_policy: dict = Field(default_factory=lambda: {"mode": "auto"})
    tool_names: list[str] = Field(default_factory=list)
    loop_config: dict = Field(default_factory=dict)


def _out(h: Harness, pack: Pack | None = None) -> dict:
    return {
        "id": str(h.id),
        "name": h.name,
        "description": h.description,
        "pack_id": str(h.pack_id) if h.pack_id else None,
        "pack_slug": pack.slug if pack else None,
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


@router.get("")
async def list_harnesses(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    harnesses = (
        (await db.execute(select(Harness).where(Harness.is_archived.is_(False)).order_by(Harness.name)))
        .scalars()
        .all()
    )
    packs = {p.id: p for p in (await db.execute(select(Pack))).scalars().all()}
    return [_out(h, packs.get(h.pack_id)) for h in harnesses]


@router.get("/{harness_id}")
async def get_harness(
    harness_id: uuid.UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    h = await db.get(Harness, harness_id)
    if h is None:
        raise HTTPException(404, "Harness not found")
    pack = await db.get(Pack, h.pack_id) if h.pack_id else None
    out = _out(h, pack)
    # Assembled-prompt preview for the builder UI.
    schemas = pack.manifest.get("schemas", {}) if pack else {}
    out["assembled_system_prompt"] = assemble_system_prompt(h, pack, h.task_profile, schemas)
    if pack:
        out["task_types"] = pack.manifest.get("task_types", [])
    return out


@router.post("")
async def create_harness(
    body: HarnessBody, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    _validate_policy(body.model_policy)
    _validate_tool_names(body.tool_names)
    workspace = (await db.execute(select(Workspace))).scalars().first()
    dupe = (
        await db.execute(
            select(Harness).where(Harness.name == body.name, Harness.is_archived.is_(False))
        )
    ).scalars().first()
    if dupe:
        raise HTTPException(409, f"A harness named '{body.name}' already exists")
    h = Harness(
        workspace_id=workspace.id,
        created_by=user.id,
        **body.model_dump(),
    )
    if not h.loop_config:
        h.loop_config = {"max_iterations": 24, "max_output_tokens": 8192, "temperature": 0.2}
    db.add(h)
    await db.commit()
    return _out(h)


@router.put("/{harness_id}")
async def update_harness(
    harness_id: uuid.UUID,
    body: HarnessBody,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    h = await db.get(Harness, harness_id)
    if h is None:
        raise HTTPException(404, "Harness not found")
    _validate_policy(body.model_policy)
    _validate_tool_names(body.tool_names)
    for field, value in body.model_dump().items():
        setattr(h, field, value)
    await db.commit()
    return _out(h)


@router.delete("/{harness_id}")
async def archive_harness(
    harness_id: uuid.UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    h = await db.get(Harness, harness_id)
    if h is None:
        raise HTTPException(404, "Harness not found")
    h.is_archived = True
    await db.commit()
    return {"ok": True}
