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
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tret.config import get_settings
from tret.db.models import Harness, Project, User, Workspace, WorkspaceMember
from tret.engine.extensions import GateResult, get_extension_registry
from tret.packs.loader import PackValidationError, install_pack

log = logging.getLogger("tret.workspace")

WORKSPACE_KINDS = ("team", "personal")


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
    # a failure in the *unprotected* steps below — `_seed_chat_harness` /
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
    `_seed_chat_harness` / `_seed_default_harnesses` below is left free to
    roll back everything, including whatever the caller had pending. Callers
    that need their own writes durable before *that* case (there are none
    today) would need their own commit first.
    """
    if get_settings().seed_default_packs:
        await db.commit()
        await _install_configured_packs(db, workspace_id, project_id)
    await _seed_chat_harness(db, workspace_id)
    await _seed_default_harnesses(db, workspace_id)


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


async def _seed_chat_harness(db: AsyncSession, workspace_id) -> None:
    """The conversational front door. Every workspace gets exactly one."""
    chat_harness = (
        await db.execute(
            select(Harness).where(
                Harness.workspace_id == workspace_id, Harness.task_profile == "chat"
            )
        )
    ).scalars().first()
    # Tools added to the seed after a workspace's chat harness already exists
    # are backfilled here, one `if` per tool, so an upgrade gives existing
    # workspaces the same defaults a fresh one gets rather than only new
    # workspaces going forward.
    if chat_harness is not None:
        if "run_method" not in (chat_harness.tool_names or []):
            chat_harness.tool_names = [*chat_harness.tool_names, "run_method"]
        missing_connector_tools = [
            n
            for n in ("list_connected_sources", "search_connected_files", "read_connected_file")
            if n not in (chat_harness.tool_names or [])
        ]
        if missing_connector_tools:
            chat_harness.tool_names = [*chat_harness.tool_names, *missing_connector_tools]
        return
    db.add(
        Harness(
            workspace_id=workspace_id,
            name="Chat Assistant",
            description="Conversational front door: answers directly from documents and "
            "datasets, and delegates structured work to specialist harnesses.",
            task_profile="chat",
            model_policy={"mode": "auto", "max_cost_tier": "standard"},
            tool_names=[
                "run_harness_task",
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
    )


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
