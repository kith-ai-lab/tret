"""Workspace creation: the one place a new workspace's starting shape is
decided, whether that is self-host's boot-time Default workspace, a
multi-tenant user's personal workspace, or a team workspace (Phase C).

Extracted from services/bootstrap.py, which becomes a thin caller of this
plus a safety net (see bootstrap.py's docstring). Every call seeds the same
non-negotiable minimum — one project, the Chat Assistant and General
Assistant harnesses, and (TRET_SEED_DEFAULT_PACKS permitting) every pack
found in TRET_PACKS_DIR — so a workspace is always immediately usable and a
signup lands on a working pack rather than an empty shell.

Demo *content* beyond the pack itself — the "Sample Engagement" project
framing — stays additional and opt-in by default in multi-tenant mode: a
paying tenant's fresh workspace should not open framed as a stranger's
sample project. The Climate Analyst harness is no longer part of that
opt-in bundle: it now ships as the climate-risk pack's own `harnesses:`
preset (`packs/climate-risk/pack.yaml`, installed by
`tret.packs.loader.install_pack`), so it arrives in every workspace the pack
installs into — which, like the pack itself, is every workspace regardless
of `seed_demo_content`/`TRET_MULTI_TENANT`, gated only by
`TRET_SEED_DEFAULT_PACKS`. `_seed_default_harnesses` below now seeds only
the pack-agnostic General Assistant harness.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tret.config import get_settings
from tret.db.models import Harness, Project, User, Workspace, WorkspaceMember
from tret.engine.delegation import SUBAGENT_ALLOWED_TOOLS, SUBAGENT_TASK_PROFILE
from tret.engine.extensions import GateResult, get_extension_registry
import tret.engine.collection_tools  # noqa: F401 - registers report_collection_outcome
import tret.engine.evaluation_tools  # noqa: F401 - registers submit_evaluation, submit_batch_review
import tret.engine.batch_tools  # noqa: F401 - registers start_batch, batch_status, batch_results
from tret.engine.batch_tools import BATCH_TOOLS, BATCH_TOOLS_SEEDED_KEY
from tret.net.policy import CLASS_RESEARCH, MODE_OFF, effective_mode
from tret.packs.links import link_all_workspace_packs
from tret.packs.loader import PackValidationError, install_pack
from tret.services.budgets import BudgetSettings

log = logging.getLogger("tret.workspace")

WORKSPACE_KINDS = ("team", "personal")


def validate_budget_settings(doc: dict) -> dict:
    """Validate a `Workspace.settings["budget"]` document (`api/budgets.py`'s
    PUT), returning the normalized dict to store. Raises
    `pydantic.ValidationError` on anything invalid — period not one of
    daily/weekly/monthly, `cap_usd` not a positive number, or an empty/
    out-of-range `alerts` list — which the API turns into a 422 with a plain
    message (`services/emission_settings.py::validation_detail`, the same
    helper the emissions settings endpoint uses for its own 422s).

    The schema itself (`BudgetSettings`) lives in `services/budgets.py`
    alongside the spend query and gate that read the same shape back; this
    is the one write-path call site, matching the module's own docstring
    ("Validate on write in services/workspace.py").
    """
    validated = BudgetSettings(**doc)
    return validated.model_dump()


async def create_workspace(
    db: AsyncSession,
    name: str,
    *,
    kind: str = "team",
    owner: User | None = None,
    seed_demo_content: bool | None = None,
) -> Workspace:
    """Create a workspace, seed it, and (if `owner` is given) add them as its
    `owner` member.

    `kind='personal'` requires `owner` (personal workspaces are always tied to
    one user — `workspaces.personal_owner_id`, UNIQUE, is what makes "at most
    one personal workspace per user" a database guarantee rather than an
    application promise).

    `seed_demo_content`: None (the default) follows `TRET_MULTI_TENANT` — demo
    content ships unless multi-tenant mode is on, matching every self-hosted
    install today. True/False overrides that for a specific call (Phase C's
    "create a team workspace" endpoint always passes False; a future "give me
    a sample project" action could pass True even in multi-tenant mode).

    Note this no longer controls pack installation — every new workspace gets
    every pack in `TRET_PACKS_DIR` installed whenever `Settings.seed_default_packs`
    (`TRET_SEED_DEFAULT_PACKS`, true by default) is set, regardless of this
    parameter or `TRET_MULTI_TENANT`. Nor does it control the Climate Analyst
    harness any more — that now ships as the climate-risk pack's own
    `harnesses:` preset (see `packs/climate-risk/pack.yaml`,
    `tret.packs.loader.install_pack`), so it arrives wherever the pack does,
    on the same `seed_default_packs` gate. `seed_demo_content` now controls
    only the project's "Sample Engagement" framing right below —
    `seed_workspace_content` (which this calls next) no longer takes it at
    all.
    """
    if kind not in WORKSPACE_KINDS:
        raise ValueError(f"kind must be one of {WORKSPACE_KINDS}, got {kind!r}")
    if kind == "personal" and owner is None:
        raise ValueError("a personal workspace requires an owner")

    workspace = Workspace(
        name=name, kind=kind, personal_owner_id=owner.id if kind == "personal" else None
    )
    db.add(workspace)
    await db.flush()

    if owner is not None:
        db.add(WorkspaceMember(user_id=owner.id, workspace_id=workspace.id, role="owner"))

    demo = get_settings().multi_tenant is False if seed_demo_content is None else seed_demo_content

    project = Project(
        workspace_id=workspace.id,
        name="Sample Engagement" if demo else "General",
        description="Seeded demo project with the climate-risk sample data."
        if demo
        else "Default project.",
    )
    db.add(project)
    await db.flush()

    # No commit here (there used to be one, right after the project above) —
    # see seed_workspace_content's docstring for why that protection now
    # lives there instead, immediately before the one step it actually
    # protects against. Everything created so far (workspace, owner
    # membership, project) stays uncommitted until then, which is what makes
    # a failure in the *unprotected* steps below — `seed_chat_harness` /
    # `_seed_default_harnesses`, which run after packs and have no per-step
    # savepoint of their own — roll back the whole creation instead of
    # leaving a half-seeded workspace sitting around counting against a
    # workspace cap.
    await seed_workspace_content(db, workspace.id, project.id)
    await db.commit()
    return workspace


async def seed_workspace_content(db: AsyncSession, workspace_id, project_id) -> None:
    """The idempotent part of workspace seeding: packs + the standard
    harnesses. Safe to call on every boot against an already-seeded
    workspace (that is exactly what `bootstrap.bootstrap()` does for
    self-host's existing Default workspace — a pack added to
    `TRET_PACKS_DIR` after first boot still gets installed, and an upgraded
    install's chat harness still gains a newly added builtin tool), and it is
    what `create_workspace` calls right after creating a workspace's project.

    No `seed_demo_content` parameter here (there used to be one): every new
    workspace gets the default pack(s) — gated only on `Settings.
    seed_default_packs`/`TRET_SEED_DEFAULT_PACKS`, regardless of deployment
    mode — and, with them, each pack's own `harnesses:` presets
    (`tret.packs.loader.install_pack`). The Climate Analyst harness is one of
    those now: it ships as the climate-risk pack's own preset rather than
    being hardcoded here, so it is no longer demo-content-gated either.
    `_seed_default_harnesses` below seeds only the pack-agnostic General
    Assistant harness — `create_workspace`'s own `seed_demo_content` still
    decides the "Sample Engagement" project framing, just nothing
    harness-shaped downstream of it any more.

    This function commits, itself, immediately before attempting any pack
    install — not before (a caller's still-pending writes, e.g.
    `create_workspace`'s freshly-created workspace/owner/project, stay
    protected right up to the last possible moment) and not after (a pack
    that fails must never take the caller's other writes down with it — see
    `_install_configured_packs`'s own SAVEPOINT protection, unchanged). When
    `seed_default_packs` is off, nothing here commits at all: there is
    nothing pack-related to protect against, so a failure in
    `seed_chat_harness` / `_seed_default_harnesses` below is left free to
    roll back everything, including whatever the caller had pending. Callers
    that need their own writes durable before *that* case (there are none
    today) would need their own commit first.
    """
    if get_settings().seed_default_packs:
        await db.commit()
        await _install_configured_packs(db, workspace_id, project_id)
    await seed_chat_harness(db, workspace_id)
    await _seed_default_harnesses(db, workspace_id)
    await seed_subagent_harness(db, workspace_id)
    await ensure_collection_harness(db, workspace_id)
    await ensure_evaluator_harness(db, workspace_id)


async def _install_configured_packs(db: AsyncSession, workspace_id, project_id) -> None:
    """Auto-install every pack found in TRET_PACKS_DIR into this workspace.

    A pack that fails never takes the workspace down with it: a validation
    failure (PackValidationError) and any other unexpected failure (a bad
    dataset file, a DB constraint) are both logged and skipped. Each attempt
    runs inside its own SAVEPOINT (`db.begin_nested()`) so a failure rolls
    back only that pack's own partial writes — necessary because a failed
    write mid-install can otherwise leave the transaction unusable for
    whatever runs next (Postgres aborts a transaction after a failed
    statement; SQLite is more forgiving, but the fix must hold on the backend
    this actually runs on). A plain `db.rollback()` would "work" for that but
    is wrong here: it unconditionally *expires* every object already loaded
    in the session (workspace, project, ...), and AsyncSession cannot
    transparently re-fetch an expired attribute outside an explicit
    `await`— the very next unguarded `workspace.id` access anywhere after
    that raises `MissingGreenlet`. A SAVEPOINT rollback only reverts (and
    only expires) what happened inside it.
    """
    settings = get_settings()
    for packs_root in settings.packs_dir.split(":"):
        root = Path(packs_root)
        if not root.is_dir():
            continue
        for pack_dir in sorted(root.iterdir()):
            if not (pack_dir / "pack.yaml").exists():
                continue
            try:
                async with db.begin_nested():
                    pack = await install_pack(db, pack_dir, workspace_id, project_id)
                log.info(
                    "pack ready: %s@%s (content %s)",
                    pack.slug,
                    pack.version,
                    (pack.content_hash or "unpinned")[:16],
                )
            except PackValidationError as e:
                log.error("pack %s failed validation and was NOT installed:", pack_dir.name)
                for error in e.errors:
                    log.error("  - %s", error)
            except Exception:  # noqa: BLE001 - a pack failure must never break workspace creation
                log.exception(
                    "pack %s failed to install and was skipped; the workspace continues "
                    "without it",
                    pack_dir.name,
                )


async def seed_chat_harness(db: AsyncSession, workspace_id) -> None:
    """The conversational front door. Every workspace gets exactly one, and
    by default it is linked to every pack installed in its workspace (see
    `tret.packs.links.link_all_workspace_packs`) rather than shipping
    pack-less — so a workspace that has packs gets a Chat Assistant that can
    already resolve a primary pack (`api/chat.py::send_message`), with no
    separate step required to link one by hand. A workspace with no packs
    installed still ends up with none linked.

    When several packs end up linked, only the *primary* one (position 0 —
    the earliest-installed pack; see `link_all_workspace_packs`'s own
    docstring, which for a fresh boot means `TRET_PACKS_DIR`'s directory sort
    order) has its doctrine loaded into a chat turn, via
    `resolve_pack_for_task` (`api/chat.py::send_message`). The other links
    still matter — they are what makes those packs' task types and methods
    show up in the chat harness's capability catalog / `run_method` surface
    — just not for doctrine.

    `Harness.packs_linked_at` is the one-time marker that makes the default
    link a *default*, not a standing rule the seed keeps re-imposing: it is
    set the moment this function links anything (on first creation, or on
    the backfill below), and from then on an operator's own edits — including
    unlinking everything — are left alone no matter how many more times
    seeding runs. Without it, "linked to zero packs" would be indistinguishable
    from "never linked", and a deliberate unlink-all would silently come back
    at the next boot.
    """
    chat_harness = (
        await db.execute(
            select(Harness)
            .where(Harness.workspace_id == workspace_id, Harness.task_profile == "chat")
            .order_by(Harness.created_at)
        )
    ).scalars().first()
    # Tools added to the seed after a workspace's chat harness already exists
    # are backfilled here, one `if` per tool, so an upgrade gives existing
    # workspaces the same defaults a fresh one gets rather than only new
    # workspaces going forward.
    if chat_harness is not None:
        if "run_method" not in (chat_harness.tool_names or []):
            chat_harness.tool_names = [*chat_harness.tool_names, "run_method"]
        # `delegate_parallel` rides along with `run_harness_task`, never on its
        # own: an operator who deliberately removed delegation entirely should
        # not have half of it silently reappear.
        if "run_harness_task" in (chat_harness.tool_names or []) and "delegate_parallel" not in (
            chat_harness.tool_names or []
        ):
            chat_harness.tool_names = [*chat_harness.tool_names, "delegate_parallel"]
        # `spawn_subagent` rides along with `run_harness_task` on the same
        # terms as `delegate_parallel` above — never added on its own to a
        # harness that never had delegation at all.
        if "run_harness_task" in (chat_harness.tool_names or []) and "spawn_subagent" not in (
            chat_harness.tool_names or []
        ):
            chat_harness.tool_names = [*chat_harness.tool_names, "spawn_subagent"]
        # The batch tools ride along with `run_harness_task` too (same rule as
        # above), but ONCE per workspace: `Workspace.settings[BATCH_TOOLS_SEEDED_KEY]`
        # is the marker, so an operator who removes one stays removed at the next boot.
        workspace = await db.get(Workspace, workspace_id)
        if (
            workspace is not None
            and not (workspace.settings or {}).get(BATCH_TOOLS_SEEDED_KEY)
            and "run_harness_task" in (chat_harness.tool_names or [])
        ):
            missing_batch_tools = [n for n in BATCH_TOOLS if n not in (chat_harness.tool_names or [])]
            if missing_batch_tools:
                chat_harness.tool_names = [*chat_harness.tool_names, *missing_batch_tools]
            workspace.settings = {**(workspace.settings or {}), BATCH_TOOLS_SEEDED_KEY: True}
        missing_connector_tools = [
            n
            for n in ("list_connected_sources", "search_connected_files", "read_connected_file")
            if n not in (chat_harness.tool_names or [])
        ]
        if missing_connector_tools:
            chat_harness.tool_names = [*chat_harness.tool_names, *missing_connector_tools]
        # Pack-link backfill: only for a chat harness that has never been
        # defaulted before (`packs_linked_at is None`) — e.g. one seeded by
        # an older boot, before this default existed. An operator's own
        # choice, once made (including unlinking everything), is marked by
        # `packs_linked_at` being set and is never touched again — link
        # *count* is deliberately not consulted here (a curated list can be
        # legitimately empty).
        if chat_harness.packs_linked_at is None and not chat_harness.is_archived:
            await link_all_workspace_packs(db, chat_harness)
            chat_harness.packs_linked_at = datetime.now(timezone.utc)
        return
    new_chat_harness = Harness(
        workspace_id=workspace_id,
        name="Chat Assistant",
        description="Conversational front door: answers directly from documents and "
        "datasets, and delegates structured work to specialist harnesses.",
        task_profile="chat",
        model_policy={"mode": "auto", "max_cost_tier": "standard"},
        tool_names=[
            "run_harness_task",
            "delegate_parallel",
            "spawn_subagent",
            *BATCH_TOOLS,
            "run_method",
            "read_document",
            "search_documents",
            "list_connected_sources",
            "search_connected_files",
            "read_connected_file",
            "lookup_dataset",
            "list_prior_findings",
            "file_data_request",
        ],
        loop_config={"max_iterations": 16, "max_output_tokens": 4096, "temperature": 0.3},
    )
    db.add(new_chat_harness)
    await db.flush()  # populate new_chat_harness.id for the pack links below
    await link_all_workspace_packs(db, new_chat_harness)
    new_chat_harness.packs_linked_at = datetime.now(timezone.utc)
    workspace = await db.get(Workspace, workspace_id)
    if workspace is not None:
        workspace.settings = {**(workspace.settings or {}), BATCH_TOOLS_SEEDED_KEY: True}


COLLECTION_HARNESS_NAME = "Disclosure Collector"

# The Disclosure Collector's full tool list, in the order they are seeded.
# It is the one seeded harness that lists the web tools: the chat front door
# deliberately never does (it answers from what is already in the workspace),
# so collecting public disclosures lives on a harness of its own.
COLLECTION_HARNESS_TOOLS = [
    "web_search",
    "fetch_url",
    "capture_source",
    "read_document",
    "search_documents",
    "propose_package_map",
    "list_connected_sources",
    "search_connected_files",
    "read_connected_file",
    "propose_connected_write",
    "list_prior_findings",
    "delegate_parallel",
    "spawn_subagent",
    "run_harness_task",
    "file_data_request",
    # How a collection run's target run scores itself (engine/collection_tools.py);
    # refuses harmlessly in any run that is not part of a collection.
    "report_collection_outcome",
]


# Collection runs are tool-heavy (search, capture, page-boundary proposals, write-back
# over up to 40 iterations) and a weak model loops: on 2026-11-04 the router picked an
# economy-tier model that spent 22 of 24 iterations on web_search. The collector is
# therefore held to exactly the standard tier: `min_cost_tier` is a hard routing floor
# (the `quality` objective only reorders candidates, it excludes none), and the
# ceiling stays at standard so a collection run cannot silently climb to premium spend.
# Within that tier the objective is `eco`: on 2026-11-06 the same 12 registers ran
# under `balanced` (GPT-5.6 Terra, prior-driven) and `eco` (GPT-5.6 Luna, low
# effort) with 12/12 written both times, at a quarter of the cost and energy.
COLLECTION_HARNESS_MODEL_POLICY = {
    "mode": "auto",
    "min_cost_tier": "standard",
    "max_cost_tier": "standard",
    "objective": "eco",
}
# What collectors seeded by earlier releases carry. A harness whose policy equals
# any of these exactly was never edited, so boot upgrades it; any other policy is
# an operator's and is left alone.
_PREVIOUS_COLLECTION_MODEL_POLICIES = (
    {"mode": "auto", "max_cost_tier": "standard"},
    {"mode": "auto", "min_cost_tier": "standard", "max_cost_tier": "standard"},
)
# A 380-page annual report with an embedded sustainability statement needs more
# than 24 iterations to locate, read and map; the seed is raised to 40, and a run
# is capped at $8 (the engine default of $5 stopped long reports mid-map). A
# harness whose loop_config equals any previous seed was never edited, so boot
# upgrades it.
COLLECTION_HARNESS_LOOP_CONFIG = {
    "max_iterations": 40,
    "max_output_tokens": 4096,
    "temperature": 0.3,
    "max_cost_usd": 8.0,
}
_PREVIOUS_COLLECTION_HARNESS_LOOP_CONFIGS = (
    {"max_iterations": 24, "max_output_tokens": 4096, "temperature": 0.3},
    {"max_iterations": 40, "max_output_tokens": 4096, "temperature": 0.3},
)


async def ensure_collection_harness(db: AsyncSession, workspace_id) -> None:
    """Seed the "Disclosure Collector" harness: the web-capable, human-gated
    harness for collecting public sustainability disclosures with provenance.

    `task_profile` is "freeform", deliberately not "chat". "chat" is the
    reserved front-door profile: `api/harnesses.py` refuses to create or
    switch to it, and `seed_chat_harness`, `api/chat.py::create_conversation`,
    `packs/links.py::chat_harness_for_workspace` and the Chat view's default
    all resolve "the first non-archived `task_profile == 'chat'` harness" — a
    second chat-profile harness would be a hazard, not a feature. It is also
    unnecessary: the Chat view's harness dropdown lists every harness in the
    workspace and `create_conversation` accepts any non-archived `harness_id`
    (a chat turn is a run with `task_type == "chat"` on whichever harness the
    conversation names), so a user converses with this one by picking
    "harness: Disclosure Collector" on a new chat. As a freeform harness it is
    also runnable from the Workbench / `POST /api/runs`.

    Created only while the research egress class is not off at seed time: a
    deployment with research off never gets a harness whose purpose is web
    collection. Looked up by name (as `_seed_default_harnesses` does), so the
    chat selection is untouched whether or not it exists. An existing harness
    is backfilled with any listed tool it lacks, regardless of the egress
    mode, by the same convention as `seed_chat_harness`: every missing tool
    is re-added unconditionally (the `missing_connector_tools` pattern, one
    membership check per tool). There is no per-tool marker, so a tool an
    operator removed from the seeded harness reappears at the next boot;
    archiving the harness is how to turn it off.

    The seeded `model_policy` carries a routing floor
    (`COLLECTION_HARNESS_MODEL_POLICY`). An existing harness whose policy is
    still exactly a previous seed value is moved to it; an edited policy is
    never touched. The same holds, independently, for `loop_config`
    (`COLLECTION_HARNESS_LOOP_CONFIG`, 40 iterations, $8 cap).
    """
    existing = (
        await db.execute(
            select(Harness)
            .where(Harness.workspace_id == workspace_id, Harness.name == COLLECTION_HARNESS_NAME)
            .order_by(Harness.created_at)
        )
    ).scalars().first()
    if existing is not None:
        missing = [n for n in COLLECTION_HARNESS_TOOLS if n not in (existing.tool_names or [])]
        if missing:
            existing.tool_names = [*(existing.tool_names or []), *missing]
        if existing.model_policy in _PREVIOUS_COLLECTION_MODEL_POLICIES:
            existing.model_policy = dict(COLLECTION_HARNESS_MODEL_POLICY)
        if existing.loop_config in _PREVIOUS_COLLECTION_HARNESS_LOOP_CONFIGS:
            existing.loop_config = dict(COLLECTION_HARNESS_LOOP_CONFIG)
        return
    if effective_mode(CLASS_RESEARCH) == MODE_OFF:
        return
    db.add(
        Harness(
            workspace_id=workspace_id,
            name=COLLECTION_HARNESS_NAME,
            description="Collects public sustainability disclosures with provenance: "
            "searches, captures originals, proposes page boundaries for human review, and "
            "writes approved packages back to connected storage.",
            task_profile="freeform",
            model_policy=dict(COLLECTION_HARNESS_MODEL_POLICY),
            tool_names=list(COLLECTION_HARNESS_TOOLS),
            loop_config=dict(COLLECTION_HARNESS_LOOP_CONFIG),
        )
    )


EVALUATOR_HARNESS_NAME = "Batch Evaluator"

# The Batch Evaluator's tools. `submit_evaluation` and `submit_batch_review`
# (engine/evaluation_tools.py) refuse in any run that is not an evaluator run, and
# are on no other harness; it reads attached documents but never reaches the web.
EVALUATOR_HARNESS_TOOLS = [
    "read_document",
    "search_documents",
    "submit_evaluation",
    "submit_batch_review",
]

# Premium on the ceiling so a judge chosen per pass (an Opus-class model by
# default) passes `_assert_override_within_policy`; the objective only orders the
# candidates when no judge is named. Every pass names its judge (a pinned
# override), so the objective's real effect is the reasoning effort the run
# gets: `quality` means "high", and on 2026-11-06 a Gemini 3.8 Flash judge at
# "high" spent its whole 4096-token output budget reasoning about ambiguous rows
# and never called submit_evaluation. `balanced` gives "medium" on standard-tier
# models, and the output cap leaves room for reasoning plus the tool call.
EVALUATOR_HARNESS_MODEL_POLICY = {
    "mode": "auto",
    "objective": "balanced",
    "min_cost_tier": "standard",
    "max_cost_tier": "premium",
}
EVALUATOR_HARNESS_LOOP_CONFIG = {
    "max_iterations": 8,
    "max_output_tokens": 8192,
    "temperature": 0.0,
    "max_cost_usd": 0.5,
}
EVALUATOR_DOCTRINE = (
    "You are the Batch Evaluator. You score one finished run of a batch against a fixed rubric, "
    "using the evidence pack in the task message and the documents attached to this run.\n"
    "1. Score only what the evidence shows. Every score cites its evidence: page numbers, a short "
    "quote, or the field of the evidence pack it rests on. If you cannot tell, give your best "
    "estimate and set confidence to low.\n"
    "2. Use the anchors. A 5 and a 1 are defined in the rubric; a 3 is a usable result with a clear "
    "shortcoming. Do not reward length or effort: a short, precise result beats a long, padded one.\n"
    "3. An honest gap is a good result. If the task could not be done (the document does not exist, "
    "or the site refused access) and the run said so specifically, with the URLs it tried, it scores "
    "well on the criteria that apply. Criteria that do not apply get a null score.\n"
    "4. You are not told which model produced the run or what it cost, and you must not guess. Judge "
    "the result, not the writing style.\n"
    "5. Rate the row's difficulty from 1 to 5, separately from quality: how hard this task is for any "
    "competent run (1: a standalone report at a known URL; 5: the disclosure is buried in a very long "
    "filing, or the site blocks automated access).\n"
    "6. Use only the flags listed in the rubric, and only when the evidence shows them.\n"
    "7. Open attached documents only to check a specific doubt, such as whether the retained pages "
    "contain the emissions tables. Use at most four tool calls before submitting.\n"
    "8. Finish by calling submit_evaluation exactly once. Do not write a report in the chat; the tool "
    "call is the result."
)


async def ensure_evaluator_harness(db: AsyncSession, workspace_id) -> Harness:
    """Seed the "Batch Evaluator" harness (`tret/batches/evaluation.py` runs every
    evaluation pass on it) and return it. Looked up by name like the collector;
    an existing one is backfilled with any listed tool it lacks and otherwise left
    as the operator has it. Unlike the collector it is not gated on research
    egress: it never leaves the deployment. Flushes, does not commit."""
    existing = (
        await db.execute(
            select(Harness)
            .where(
                Harness.workspace_id == workspace_id,
                Harness.name == EVALUATOR_HARNESS_NAME,
                Harness.is_archived.is_(False),
            )
            .order_by(Harness.created_at)
        )
    ).scalars().first()
    if existing is not None:
        missing = [n for n in EVALUATOR_HARNESS_TOOLS if n not in (existing.tool_names or [])]
        if missing:
            existing.tool_names = [*(existing.tool_names or []), *missing]
            await db.flush()
        return existing
    harness = Harness(
        workspace_id=workspace_id,
        name=EVALUATOR_HARNESS_NAME,
        description="Scores finished batch runs against the batch's rubric. Used by Batches; read-only.",
        task_profile="freeform",
        model_policy=dict(EVALUATOR_HARNESS_MODEL_POLICY),
        tool_names=list(EVALUATOR_HARNESS_TOOLS),
        loop_config=dict(EVALUATOR_HARNESS_LOOP_CONFIG),
        system_prompt_extra=EVALUATOR_DOCTRINE,
    )
    db.add(harness)
    await db.flush()
    return harness


async def _seed_default_harnesses(db: AsyncSession, workspace_id) -> None:
    """The one pack-agnostic harness left here: General Assistant. The
    Climate Analyst harness that used to be seeded from here (gated on
    `seed_demo_content`) has moved to the climate-risk pack's own
    `harnesses:` preset — see `tret.packs.loader.install_pack`, which
    `_install_configured_packs` (running earlier in `seed_workspace_content`)
    already invokes for every pack in `TRET_PACKS_DIR`.

    Checked by `name`, not by "any harness other than chat exists" (the
    check this used before pack harness presets existed): that broader check
    would now wrongly skip General Assistant's own creation on a workspace's
    very first seeding, since `_install_configured_packs` may already have
    created a pack's preset harness (e.g. Climate Analyst, task_profile
    'divergence_assessment' — not 'chat') by the time this runs.
    """
    existing = (
        await db.execute(
            select(Harness).where(
                Harness.workspace_id == workspace_id, Harness.name == "General Assistant"
            )
        )
    ).scalars().first()
    if existing is not None:
        return
    db.add(
        Harness(
            workspace_id=workspace_id,
            name="General Assistant",
            description="Freeform analyst assistant with document and dataset tools.",
            task_profile="freeform",
            model_policy={"mode": "auto", "max_cost_tier": "standard"},
            tool_names=["read_document", "search_documents", "lookup_dataset", "list_prior_findings"],
        )
    )


async def seed_subagent_harness(db: AsyncSession, workspace_id) -> None:
    """The seeded target for ad-hoc subagent runs (a child run whose brief is
    written by another run's model — `spawn_subagent`, not yet added — rather
    than declared by a pack). Modelled exactly on `_seed_default_harnesses`
    above: checked by name, idempotent, one per workspace.

    `tool_names` is the FULL allowlist, sorted — the actual per-run grant is
    narrowed further by the spawning tool and enforced again by the engine
    (`engine.harness._subagent_tool_names`) no matter what this harness lists,
    so there is no reason to under-provision it here.
    """
    existing = (
        await db.execute(
            select(Harness).where(
                Harness.workspace_id == workspace_id,
                # By profile, not by name: an unrelated harness an operator
                # happened to call "Subagent" must not suppress seeding, and a
                # renamed or archived seeded one must not be seeded again
                # (archiving it is how an operator turns subagents off).
                Harness.task_profile == SUBAGENT_TASK_PROFILE,
            )
        )
    ).scalars().first()
    if existing is not None:
        return
    db.add(
        Harness(
            workspace_id=workspace_id,
            name="Subagent",
            description="Short-lived worker another run briefs for a bounded piece of "
            "reading or lookup. Reports text only: it cannot record findings, propose "
            "writes or delegate.",
            task_profile=SUBAGENT_TASK_PROFILE,
            model_policy={"mode": "auto", "max_cost_tier": "standard"},
            loop_config={
                "max_iterations": 12,
                "max_output_tokens": 4096,
                "temperature": 0.2,
                "max_cost_usd": 1.0,
            },
            tool_names=sorted(SUBAGENT_ALLOWED_TOOLS),
        )
    )


# ── seat-limit gates: lock -> gate -> write, as one unit ─────────────────────
#
# `check_workspace_gate` (engine/extensions.py) counts seats against a *fresh*
# session, isolated from the caller's own transaction by design — necessary
# so a broken extension gate can never poison the caller's transaction on
# Postgres, but it means the count and the caller's own write (a new
# WorkspaceMember, or a new Invite) are not one atomic read-then-write: two
# concurrent callers at seats-minus-one can each see "one seat free" from
# their own fresh gate session, both get `allowed=True`, and both insert —
# one seat over the limit, seen only under real concurrency (SQLite/test
# concurrency never reproduces it; two callers there run in the same
# process, never truly overlapping).
#
# The fix does not touch the gate's session isolation (that guarantee is
# worth keeping). Instead, `lock_workspace_for_seat_gate` takes a `SELECT ...
# FOR UPDATE` on the target Workspace row in the *caller's* transaction,
# before the gate is asked anything. On Postgres this makes the whole
# "lock -> gate -> write -> commit" span for one workspace serialize across
# concurrent callers: a second caller's own lock acquisition blocks until
# the first commits (releasing it), so by the time the second reaches the
# gate, the first's write is already visible to the gate's fresh-session
# count. On SQLite, `.with_for_update()` compiles to no row locking at all
# (the dialect has none) — a no-op, which is fine: every SQLite deployment
# here is self-host's single-worker process (docs/hardening.md), so there is
# no second concurrent request to race against in the first place.
#
# Both invite-redemption call sites (`api/workspaces.py::accept_invite` and
# `services/identity.py::_redeem_invites_and_resolve_workspace`) go through
# `redeem_workspace_seat` so this exact sequence cannot drift between them;
# `create_invite`'s own "invite" gate (api/workspaces.py) takes the same lock
# directly, since it writes an Invite, not a WorkspaceMember.
async def lock_workspace_for_seat_gate(db: AsyncSession, workspace_id: uuid.UUID) -> None:
    """Row-lock the target workspace in the caller's own transaction, ahead
    of asking a seat-limit workspace gate about it. Must be called (and the
    lock held, i.e. the transaction kept open) until whatever the gate
    permitted is committed — see the section docstring above."""
    await db.execute(select(Workspace.id).where(Workspace.id == workspace_id).with_for_update())


async def redeem_workspace_seat(
    db: AsyncSession, workspace_id: uuid.UUID, user_id: uuid.UUID, role: str
) -> GateResult:
    """Lock the workspace, ask the `invite_redeem` seat gate, and — only if
    it allows — `db.add()` the new WorkspaceMember. The caller still commits
    (this never commits itself, matching every other write helper in this
    module): both invite-acceptance call sites have their own surrounding
    transaction and their own "what to do on refusal" behavior (leave the
    invite pending; try the next one; etc).

    Returns the `GateResult` either way so the caller can react to a refusal
    exactly as it did before this helper existed.
    """
    await lock_workspace_for_seat_gate(db, workspace_id)
    gate = await get_extension_registry().check_workspace_gate(db, workspace_id, "invite_redeem")
    if gate.allowed:
        db.add(WorkspaceMember(user_id=user_id, workspace_id=workspace_id, role=role))
    return gate
