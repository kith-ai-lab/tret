"""`tret/api/routing.py`: a dry-run preview of what `ModelRouter.route()` would
pick for a harness — the model, the effort, why, and a cost range — without
ever starting a run.

`engine/harness.py` is the only place a `RoutingDecision` is normally made and
persisted (`runs.routing`). This endpoint calls the exact same router with the
same inputs a run would compute, but never persists anything and never touches
`engine.execute`: no `runs` row is created, no provider is called to actually
generate text. It exists so a harness author can see "what will this cost and
which model will run it" *before* committing to a run, the same question
`docs/architecture.md`'s routing section already answers only in hindsight.

Cost note: each preview costs up to four real router-model LLM calls, `
max_tokens=512` each, on the calling workspace's own provider key (the router
is a model call like any other — see `ModelRouter.route`). A plain preview is
one call; `compare: true` evaluates all four known objectives
(`router_llm.objectives.OBJECTIVES`) and is therefore capped at exactly four
router calls, never more — there is no way to ask this endpoint for a fifth.
This spend is real and lands on the workspace's own key, but — unlike a run's
`overhead` block (`engine/harness.py`) — it is **not recorded anywhere**: no
`runs` row exists for a preview to attach it to, so it appears on the
provider's own bill and nowhere in tret's own accounting. That is the
operator's cost to carry, not a bug to route around; `router_overhead_usd` on
each result at least says how much of it a given preview spent, and one INFO
line per request logs the total.
"""
from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from tret.adaptive import adaptive_of
from tret.api._policy import validate_policy
from tret.api.auth import current_user
from tret.api.workspace import ROLE_RANK, WorkspaceContext, current_workspace
from tret.db.engine import get_db
from tret.db.models import Harness, Pack, User
from tret.engine.compaction import required_context_window
from tret.engine.context import assemble_context, composition_report, task_config
from tret.engine.harness import DEFAULT_MAX_OUTPUT_TOKENS, GENERIC_TASK_TYPES, get_harness_engine
from tret.engine.tools import WEB_TOOL_NAMES, withheld_web_tools
from tret.packs.links import packs_for_harness, resolve_pack_for_task
from tret.providers.catalog import ProviderRegistry, get_catalog
from tret.router_llm.objectives import DEFAULT_MAX_COST_TIER, OBJECTIVES, objective_of
from tret.router_llm.router import TIER_ORDER, ModelRouter, RoutingUnavailable
from tret.services import lessons as lessons_service
from tret.services.credentials import load_db_keys

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/routing", tags=["routing"])

# No catalog model publishes its own per-model max-output figure (`ModelInfo`
# has no such field — see `providers/catalog.py`), so `max_output_tokens`
# below is bounded against this constant instead: comfortably above every
# real model's actual output ceiling, just there to stop the field from
# being unbounded (it feeds straight into this endpoint's own cost_usd_high
# estimate).
MAX_OUTPUT_TOKENS_CEILING = 200_000


class RoutingPreviewBody(BaseModel):
    # Either name a saved harness (its own model_policy, loop_config,
    # system_prompt_extra and pack links are used unless overridden below) or
    # go fully inline — a form previewing edits it has not saved yet has no
    # harness row to point at.
    harness_id: uuid.UUID | None = None
    # Overrides the named harness's model_policy when both are given (the
    # "preview my unsaved edits" case); required when harness_id is not given.
    # Validated the same way the harness write path validates a saved policy
    # (`api._policy.validate_policy`) — an inline policy is not a lighter-
    # weight thing than a saved one just because it is never persisted.
    # Permission rule: with harness_id, a non-admin caller is held to that
    # harness's own saved ceiling — see `_check_inline_policy_permission`;
    # without harness_id there is no saved ceiling to hold anyone to, so that
    # path requires the workspace's admin role or higher outright (see
    # `preview_routing`'s own no-harness_id branch) — harness authoring is
    # already admin-gated, and "preview an unsaved new harness" is that same
    # workflow.
    model_policy: dict | None = None
    # Overrides the pack the harness would otherwise resolve for task_type
    # (`resolve_pack_for_task`); also how an inline (no harness_id) preview
    # names a pack at all. None is valid — a "chat"/"freeform" task type needs
    # no pack.
    pack_id: uuid.UUID | None = None
    task_type: str = "freeform"
    n_documents: int = Field(default=0, ge=0)
    # The next three mirror exactly what a real run passes into the same
    # assembly/routing calls (`engine/harness.py` ~L804-906:
    # max_output_tokens from loop_config, system_prompt_extra off the
    # harness row, web_tools_enabled from the run's own enabled tool names).
    # Read via `model_fields_set` (see `preview_routing` below), not "is not
    # None": for a saved harness each defaults to that harness's own value
    # when omitted, and `system_prompt_extra: null` is itself a meaningful
    # override (clearing it) that "omitted" must not be confused with. An
    # inline preview (no harness_id) has no saved value to fall back to, so
    # an omitted field there takes the same default a fresh harness/run would
    # (DEFAULT_MAX_OUTPUT_TOKENS, no extra prompt, no web tools).
    # Capped at MAX_OUTPUT_TOKENS_CEILING (no catalog model publishes its own
    # per-model output limit, so there is no per-model figure to bound this
    # against) — otherwise this field alone sizes a preview's `cost_usd_high`
    # (`model_info.cost_usd(est_input_tokens, max_output_tokens)` below), and
    # an unbounded one lets a caller inflate that estimate arbitrarily.
    max_output_tokens: int | None = Field(default=None, gt=0, le=MAX_OUTPUT_TOKENS_CEILING)
    system_prompt_extra: str | None = None
    # The enabled tool names a run of this preview would carry — same field a
    # harness/run's own tool_names is, so web_tools_enabled below can be
    # derived the same way `api/harnesses.py::get_harness` derives it rather
    # than trusting whatever a client computed client-side (which can drift
    # from the server's own precedence rule the moment a pack task declares
    # its own tools). `web_tools_enabled` is still accepted for a caller that
    # has already worked out the boolean itself; when both are given,
    # `tool_names` wins — see `preview_routing` for the precedence.
    tool_names: list[str] | None = None
    web_tools_enabled: bool | None = None
    # Also route every other objective in router_llm.objectives.OBJECTIVES
    # (four total, always — see the module docstring's cost note). Gated to
    # the workspace's approver role or higher (`ROLE_RANK`): this limits one
    # click to one router call for non-approvers rather than four; it is not
    # a spend or rate control on this endpoint more broadly — there is no
    # per-caller rate limit or spend accounting here at all, so a non-approver
    # can still call plain preview (one router call each) as many times as
    # they like.
    compare: bool = False


class _PreviewHarness:
    """Duck-typed stand-in for `Harness` in `assemble_context` — see
    `preview_routing` below. Carries only the two attributes that function
    reads off a harness (`name`, `system_prompt_extra`); used for a fully
    inline preview (no saved harness at all) and for a saved harness whose
    `system_prompt_extra` the caller is overriding with unsaved form text.
    Never persisted or returned.
    """

    def __init__(self, name: str, system_prompt_extra: str | None):
        self.name = name
        self.system_prompt_extra = system_prompt_extra


def _task_routing_fields(pack: Pack | None, task_type: str) -> tuple[str, str, str]:
    """`(task_shape, task_description, output_contract)` for `task_type`.

    The minimal replica of what `engine/harness.py` builds at routing time
    (~L957-961) from the same `task_config()` lookup — shape, display name and
    the router-facing one-line output contract, each with the same default a
    run falls back to for a task_type no pack declares (freeform/chat).
    """
    task = task_config(pack, task_type)
    if task is None:
        return "freeform", task_type, "free text"
    return (
        task.get("shape", "freeform"),
        task.get("display_name", task_type),
        task.get("output_contract", "free text"),
    )


def _web_tools_enabled_for(tool_names: list[str], task: dict | None) -> bool:
    """The `web_tools_enabled` a run carrying `tool_names` would compute on its
    own — same derivation as `api/harnesses.py::get_harness`'s
    `assembled_system_prompt` and `engine/harness.py`'s real run, from
    whichever tool list actually governs (the task's own `tools`, else
    `tool_names`), minus whatever a deployment withholds. Never trust a
    client-computed boolean over this: a pack task can declare its own tools
    (including a web one) regardless of what the harness's own tool_names
    says, and only this precedence rule knows that.
    """
    run_tools = list((task or {}).get("tools") or tool_names or [])
    withheld = withheld_web_tools(run_tools)
    return any(name in WEB_TOOL_NAMES for name in run_tools if name not in withheld)


def _check_inline_policy_permission(
    inline_policy: dict, saved_policy: dict, ctx: WorkspaceContext
) -> None:
    """A non-admin previewing a saved harness with an inline `model_policy`
    override must not be able to preview under a looser policy than the
    harness itself allows.

    `max_cost_tier` is not only a spending control — `TIER_ORDER` ranks
    `local` below every cloud tier specifically so a harness can express
    "never leaves this machine" as a cost ceiling (see
    `router_llm.objectives.TIER_ORDER`'s own docstring), which makes it a
    confidentiality control too. Letting any workspace member lift that cap
    for "just a preview" would let them see what a cloud model does with
    data the harness's author deliberately kept off the network — the same
    reasoning applies to widening `allowed` past the saved list. Workspace
    admins already hold the role that can edit the harness's real policy
    (`require_workspace_admin` in `api/harnesses.py`), so they may preview
    anything.

    Only reachable with a `harness_id` at all: without one there is no saved
    policy to hold a non-admin to in the first place, so `preview_routing`
    requires the workspace's admin role or higher up front on that path
    instead of calling this function — see its own no-`harness_id` branch.
    The rule in full: with `harness_id`, a non-admin's inline override is held
    to that harness's own saved ceiling (this function); without one, only an
    admin may preview at all.

    Called only after `validate_policy(inline_policy)` — so `mode`/`model`
    are already known-good and a `mode: pinned` model is already confirmed to
    exist in the catalog, which is what makes the catalog lookups below safe.
    """
    if ROLE_RANK[ctx.role] >= ROLE_RANK["admin"]:
        return
    saved_tier = saved_policy.get("max_cost_tier", DEFAULT_MAX_COST_TIER)
    saved_allowed = saved_policy.get("allowed")
    saved_is_pinned = saved_policy.get("mode") == "pinned"
    if saved_is_pinned:
        # Same reasoning as the inline-pin branch below, applied to the saved
        # side: a pin's own `max_cost_tier` is cosmetic and a pinned harness
        # usually never sets it at all, which left `saved_tier` defaulting to
        # DEFAULT_MAX_COST_TIER ("premium") — no effective ceiling whatsoever,
        # regardless of how cheap or how local-only the actually-pinned model
        # is. Derive the real ceiling from what the saved pin resolves to.
        saved_model_info = get_catalog().get(saved_policy.get("model", ""))
        if saved_model_info is not None:
            saved_tier = saved_model_info.cost_tier
    saved_tier_rank = TIER_ORDER.get(saved_tier, TIER_ORDER[DEFAULT_MAX_COST_TIER])

    if inline_policy.get("mode") == "pinned":
        # A pin's own `max_cost_tier` field is cosmetic in pinned mode —
        # `ModelRouter.route()` never filters a pin by it (router_llm/router.py:
        # "A harness pin may exceed the harness's own ceiling"), it goes
        # straight to the named model. Checking the *field* the way the
        # auto-mode branch below does would let a non-admin dress a premium
        # pin as `max_cost_tier: "local"` and sail through the check while
        # still routing to the premium model. Check what the pin actually
        # resolves to instead.
        pinned_model = inline_policy.get("model", "")
        pinned_tier = get_catalog().get(pinned_model).cost_tier
        if TIER_ORDER.get(pinned_tier, TIER_ORDER[DEFAULT_MAX_COST_TIER]) > saved_tier_rank:
            raise HTTPException(
                403,
                f"inline pinned model '{pinned_model}' (cost_tier '{pinned_tier}') exceeds "
                f"this harness's own '{saved_tier}' ceiling; only a workspace admin may "
                "preview above it",
            )
        # A pin carries no `allowed` list of its own to inherit — when the
        # saved policy doesn't set one either, treat the saved pin's own model
        # as that list of one, so an inline pin is held to *this specific
        # harness*, not just its tier: a non-admin may preview the saved pin
        # again, but not quietly swap it for a different model the tier check
        # alone would let through (e.g. another model in the same cost tier).
        pin_allowed = saved_allowed or ([saved_policy.get("model")] if saved_is_pinned else None)
        if pin_allowed and pinned_model not in pin_allowed:
            raise HTTPException(
                403,
                f"inline pinned model '{pinned_model}' is outside this harness's own "
                "allowed list; only a workspace admin may preview it",
            )
        return

    inline_tier = inline_policy.get("max_cost_tier", DEFAULT_MAX_COST_TIER)
    if TIER_ORDER.get(inline_tier, TIER_ORDER[DEFAULT_MAX_COST_TIER]) > saved_tier_rank:
        raise HTTPException(
            403,
            f"inline max_cost_tier '{inline_tier}' exceeds this harness's own "
            f"'{saved_tier}' ceiling; only a workspace admin may preview above it",
        )
    if saved_allowed:
        # A missing/empty `allowed` here is not "same as the harness's" — the
        # router reads a missing `allowed` as "every model in the catalog is a
        # candidate" (no allow-list filter at all), so omitting it would
        # silently drop the harness's own restriction rather than inherit it.
        # A non-admin must name an explicit, non-empty subset.
        inline_allowed = inline_policy.get("allowed")
        if not inline_allowed:
            raise HTTPException(
                403,
                "this harness restricts `allowed`; a non-admin inline override must supply "
                f"a non-empty subset of {sorted(saved_allowed)}",
            )
        outside = sorted(set(inline_allowed) - set(saved_allowed))
        if outside:
            raise HTTPException(
                403,
                f"inline allowed model(s) {outside} are outside this harness's own "
                "allowed list; only a workspace admin may preview them",
            )


def _resolved_task_or_422(pack: Pack | None, task_type: str) -> dict | None:
    """`task_config(pack, task_type)`, but 422s exactly the way a real run's
    `engine/harness.py` `unknown_task_type` refusal does when nobody declares
    `task_type` and it isn't one of the engine's own generic types — rather
    than silently previewing under a freeform default for a typo'd or
    uninstalled task type. Both preview paths call this immediately after
    resolving `pack`; the harness-id path used to 422 only the explicit
    `pack_id: null` case (any other unresolved task_type fell through), and
    the inline (no-harness_id) path never checked at all.
    """
    task = task_config(pack, task_type)
    if task is None and task_type not in GENERIC_TASK_TYPES:
        declared = sorted(t["slug"] for t in (pack.manifest.get("task_types", []) if pack else []))
        raise HTTPException(
            422,
            f"unknown_task_type: '{task_type}' is not declared by this preview's pack "
            f"(declared: {declared or 'none'}; the engine's own task types are "
            f"{list(GENERIC_TASK_TYPES)})",
        )
    return task


@router.post("/preview")
async def preview_routing(
    body: RoutingPreviewBody,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """Dry-run `ModelRouter.route()` for a saved harness or an inline policy.

    Spends up to four short router calls (`max_tokens=512` each — one per
    objective evaluated) on the workspace's own provider key. That spend is
    real but is never recorded as run spend anywhere in tret's own
    accounting — no `runs` row exists here to attach it to — so it is the
    operator's cost to carry, visible only on the provider's own bill and in
    this endpoint's own `router_overhead_usd`/INFO log. See the module
    docstring for why that is a documented tradeoff rather than a gap to
    close: building a billing path for a call that produces no artifact would
    be scaffolding for a feature (metered previews) nobody has asked for.

    Deterministic by construction: bounded exploration's coin flip
    (`ModelRouter._maybe_explore`) is forced off for every objective this
    preview evaluates — `adaptive.exploration` is overridden to 0 on the copy
    of the policy handed to the router, never on `model_policy` itself — so
    calling this endpoint twice with the same policy, and a real run later
    made under that same (untouched) policy, are unaffected by it.
    """
    fields_set = body.model_fields_set
    if body.compare and ROLE_RANK[ctx.role] < ROLE_RANK["approver"]:
        # Four router calls on the workspace's own key instead of one — real,
        # unrecorded spend (see the module/field docstrings) that an analyst
        # should not be able to trigger just by ticking a box.
        raise HTTPException(
            403, "compare: true requires the workspace's approver role or higher"
        )
    harness: Harness | None = None
    pack: Pack | None = None

    if body.harness_id is not None:
        harness = await db.get(Harness, body.harness_id)
        if harness is None or harness.is_archived or harness.workspace_id != ctx.id:
            raise HTTPException(404, "Harness not found")
        inline_policy = body.model_policy
        if inline_policy is not None:
            validate_policy(inline_policy)
            _check_inline_policy_permission(inline_policy, harness.model_policy, ctx)
        model_policy = inline_policy if inline_policy is not None else harness.model_policy
        max_output_tokens = (
            body.max_output_tokens
            if "max_output_tokens" in fields_set and body.max_output_tokens is not None
            else int((harness.loop_config or {}).get("max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS))
        )
        system_prompt_extra = (
            body.system_prompt_extra
            if "system_prompt_extra" in fields_set
            else harness.system_prompt_extra
        )
        # `pack_id` is resolved exactly like the inline (no-harness_id) path
        # below — any pack in this workspace, not only one already linked to
        # the saved harness — because a form previewing unsaved edits (a pack
        # just added, not yet Saved) has no link row to be found by yet.
        # "omitted" and "pack_id: null" are different requests: omitted means
        # "use the harness's own resolution", explicit null means "no pack at
        # all", which only a freeform/chat task type can run without one —
        # same rule `engine/harness.py` enforces for a real run.
        if "pack_id" in fields_set:
            if body.pack_id is not None:
                pack = await db.get(Pack, body.pack_id)
                if pack is None or pack.workspace_id != ctx.id:
                    raise HTTPException(404, "pack not found")
        else:
            pack = resolve_pack_for_task(await packs_for_harness(db, harness), body.task_type)
        # Below also covers the explicit `pack_id: null` + non-generic
        # task_type case the dedicated check above used to raise on directly
        # (`pack` is None either way, so `_resolved_task_or_422` 422s it the
        # same) — as well as every other way `body.task_type` can fail to
        # resolve on this branch, which nothing used to 422 on at all.
        task = _resolved_task_or_422(pack, body.task_type)
        if "tool_names" in fields_set and body.tool_names is not None:
            web_tools_enabled = _web_tools_enabled_for(body.tool_names, task)
        elif "web_tools_enabled" in fields_set and body.web_tools_enabled is not None:
            web_tools_enabled = body.web_tools_enabled
        else:
            web_tools_enabled = _web_tools_enabled_for(harness.tool_names, task)
        harness_for_assembly: Harness | _PreviewHarness = (
            harness
            if "system_prompt_extra" not in fields_set
            else _PreviewHarness(harness.name, system_prompt_extra)
        )
    else:
        # No harness_id means no saved policy exists to hold a non-admin to
        # `_check_inline_policy_permission`'s ceiling — that check runs only
        # on the harness_id branch above. Without a saved row to check
        # against, "preview this policy" is simply "preview whatever policy I
        # hand you", on the workspace's own provider key. Harness authoring
        # is already admin-gated (`require_workspace_admin`,
        # `api/harnesses.py`), so "preview an unsaved new harness" — the only
        # legitimate reason to take this branch — is an admin workflow too. A
        # non-admin previewing a *saved* harness is unaffected and stays
        # governed by the ceiling check above.
        if ROLE_RANK[ctx.role] < ROLE_RANK["admin"]:
            raise HTTPException(
                403,
                "previewing without harness_id requires the workspace's admin role or "
                "higher; a non-admin may only preview a saved harness (pass harness_id)",
            )
        if body.model_policy is None:
            raise HTTPException(
                422, "model_policy is required when harness_id is not given"
            )
        model_policy = body.model_policy
        validate_policy(model_policy)
        max_output_tokens = body.max_output_tokens or DEFAULT_MAX_OUTPUT_TOKENS
        if body.pack_id is not None:
            pack = await db.get(Pack, body.pack_id)
            if pack is None or pack.workspace_id != ctx.id:
                raise HTTPException(404, "pack not found")
        task = _resolved_task_or_422(pack, body.task_type)
        if "tool_names" in fields_set and body.tool_names is not None:
            web_tools_enabled = _web_tools_enabled_for(body.tool_names, task)
        elif "web_tools_enabled" in fields_set and body.web_tools_enabled is not None:
            web_tools_enabled = bool(body.web_tools_enabled)
        else:
            web_tools_enabled = False
        harness_for_assembly = _PreviewHarness("preview", body.system_prompt_extra)

    output_schemas: dict = pack.manifest.get("schemas", {}) if pack else {}
    assembled = assemble_context(
        harness_for_assembly,
        pack,
        body.task_type,
        output_schemas,
        web_tools_enabled=web_tools_enabled,
        # Preview must match a real run's prompt — `harness` (the saved row,
        # None on the no-harness_id branch) is where a real run's own
        # `loop_config.lessons` opt-out would live too.
        lessons=(
            await lessons_service.approved_lessons(db, ctx.id, pack.slug)
            if pack is not None
            and lessons_service.lessons_enabled(harness.loop_config if harness else None)
            else None
        ),
    )
    # Minimal by design: this is the system-prompt assembly alone (platform
    # preamble + doctrine + task instructions/output contract [+ web-evidence
    # rules when web_tools_enabled] + harness extra), the same inputs
    # `engine/harness.py` composes before a model is chosen. It omits tool
    # specs, conversation history and the eventual user message — none of
    # which exist yet for a form that has not been run — so the real run's
    # est_input_tokens will read higher once documents, tools and a task
    # input are attached. Good enough to size a model and a cost range; not a
    # promise of the exact token count a run will report.
    composition = composition_report(assembled.blocks)
    est_input_tokens = composition["total_est_tokens"]

    task_shape, task_description, output_contract = _task_routing_fields(
        pack, body.task_type
    )

    adaptive = adaptive_of(model_policy)
    min_context_window = required_context_window(
        est_input_tokens, max_output_tokens, adaptive.context_headroom
    )

    engine = get_harness_engine()
    # Scoped to this workspace's own stored provider keys, exactly like a real
    # run (`engine/harness.py`'s `execute`) — never another workspace's.
    registry = ProviderRegistry(await load_db_keys(db, ctx.id))
    model_router = ModelRouter(engine.catalog, registry, engine.priors)

    objectives = list(OBJECTIVES) if body.compare else [objective_of(model_policy)]

    results = []
    total_router_overhead_usd = 0.0
    try:
        for objective in objectives:
            # Exploration forced to 0 on the copy sent to the router only —
            # never on `model_policy` itself, so the saved/inline policy this
            # preview describes, and any real run later made under it, is
            # untouched. A preview exists to answer "what will this pick", and
            # `_maybe_explore`'s coin flip (router_llm/router.py) would let two
            # calls to this same endpoint, with the same policy, return two
            # different decisions — a preview and `compare` must be
            # deterministic (see the module/endpoint docstrings).
            policy_for_objective = {
                **model_policy,
                "objective": objective,
                "adaptive": {**(model_policy.get("adaptive") or {}), "exploration": 0},
            }
            decision = await model_router.route(
                model_policy=policy_for_objective,
                task_type=body.task_type,
                task_shape=task_shape,
                task_description=task_description,
                output_contract=output_contract,
                n_documents=body.n_documents,
                est_input_tokens=est_input_tokens,
                min_context_window=min_context_window,
            )
            model_info = engine.catalog.get(decision.chosen_model)
            # Two ends of a range, not a point estimate — the actual bill
            # depends on prompt caching and how much of max_output_tokens the
            # model actually uses, neither of which is known before the call:
            #   low  = the whole input priced as a cache *read* (the cheapest
            #          this call could possibly be, i.e. an already-warm
            #          cache) plus only 10% of the output budget used.
            #   high = the whole input priced uncached, plus the full output
            #          budget used.
            low = model_info.cost_usd(
                0, round(max_output_tokens * 0.1), cache_read_tokens=est_input_tokens
            )
            high = model_info.cost_usd(est_input_tokens, max_output_tokens)
            router_overhead_usd = decision.spend["cost_usd"] if decision.spend else 0.0
            total_router_overhead_usd += router_overhead_usd
            results.append(
                {
                    "objective": objective,
                    "decision": decision.to_json(),
                    "estimate": {
                        "input_tokens": est_input_tokens,
                        "max_output_tokens": max_output_tokens,
                        "cost_usd_low": float(low),
                        "cost_usd_high": float(high),
                        "router_overhead_usd": router_overhead_usd,
                    },
                }
            )
    except RoutingUnavailable as e:
        raise HTTPException(409, str(e)) from None

    # Never billed anywhere (see module/endpoint docstrings) — this is the
    # only record of what a preview actually spent.
    logger.info(
        "routing preview: workspace=%s harness_id=%s objectives=%d router_overhead_usd=%.6f",
        ctx.id,
        harness.id if harness else None,
        len(objectives),
        total_router_overhead_usd,
    )

    return {"results": results}
