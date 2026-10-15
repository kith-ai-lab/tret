"""First-boot seeding: admin user, default workspace + sample project, packs.

Idempotent — safe to run on every startup.

The heavy lifting (a workspace's project, packs and standard harnesses) lives
in services/workspace.py, extracted so a multi-tenant deployment's
"create a new workspace" flow (Phase C) builds a workspace exactly the way
self-host's Default workspace always has. What is left here is specific to
*this* one bootstrap workspace — always fully seeded with demo content,
exactly as before `TRET_MULTI_TENANT` existed — plus a safety net: every user
must be a member of at least one workspace by construction (api/workspace.py's
`current_workspace` dependency relies on that invariant), so a user who
somehow ended up with none (a hand-edited database, a user row inserted
outside the API) is added to the oldest workspace here rather than 401ing
against a workspace-scoped endpoint forever.
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tret.api.auth import bootstrap_admin
from tret.config import get_settings
from tret.db.models import Project, User, Workspace, WorkspaceMember
from tret.services.workspace import (
    create_workspace,
    seed_chat_harness,
    seed_subagent_harness,
    seed_workspace_content,
)

log = logging.getLogger("tret.bootstrap")


async def bootstrap(db: AsyncSession) -> None:
    await bootstrap_admin(db)

    # Live workspaces only: an archived team is not the bootstrap workspace and
    # not a place to seed into or re-home anyone.
    workspace = (
        await db.execute(
            select(Workspace)
            .where(Workspace.archived_at.is_(None))
            .order_by(Workspace.created_at)
        )
    ).scalars().first()
    if workspace is None:
        # First boot ever: create_workspace seeds the project, packs and
        # harnesses in one call. Demo content is unconditional here — this is
        # self-host's single bootstrap workspace, not a multi-tenant user's
        # fresh one, so `TRET_MULTI_TENANT` does not change what it looks like.
        workspace = await create_workspace(db, "Default", kind="team", seed_demo_content=True)
    else:
        # Steady state: the workspace already exists (every boot after the
        # first). Re-run the idempotent seeding so a pack added to
        # TRET_PACKS_DIR after first boot gets installed, and an upgraded
        # install's chat harness gains any newly added builtin tool.
        project = (
            await db.execute(select(Project).where(Project.workspace_id == workspace.id))
        ).scalars().first()
        if project is None:
            project = Project(
                workspace_id=workspace.id,
                name="Sample Engagement",
                description="Seeded demo project with the climate-risk sample data.",
            )
            db.add(project)
            await db.flush()
            # A pack failing to install below must not be able to roll this
            # just-created project back out from under the workspace — see
            # workspace.py's _install_configured_packs.
            await db.commit()
        await seed_workspace_content(db, workspace.id, project.id)
        await db.commit()

    # Multi-tenant carries the chat-harness backfill above to every OTHER
    # workspace too — the oldest-workspace reseed above only ever touches the
    # one workspace found at line 36, but a default added to
    # `seed_chat_harness` after a tenant's workspace was created (a new
    # builtin tool, or the default-pack-links behavior itself) still needs to
    # reach that tenant's existing Chat Assistant. `seed_workspace_content`
    # is deliberately NOT called per-workspace here: it would install packs
    # and seed harnesses into every tenant's workspace on every boot, and
    # that already happened once, at each workspace's own creation
    # (`create_workspace`). Only the chat-harness step reruns. That is cheap
    # (one harness lookup per workspace) and safe to repeat every boot for
    # every workspace, forever, because it is idempotent on its own: the
    # builtin-tool backfill is a per-tool `if not in` check, and the
    # pack-link backfill is gated on `Harness.packs_linked_at`. Each
    # workspace's attempt runs in its own SAVEPOINT so one workspace's
    # failure can't abort boot for the rest, or leave the session unusable
    # for the next iteration — see workspace.py's `_install_configured_packs`
    # docstring for why a plain `db.rollback()` would be wrong here.
    other_workspaces = (
        await db.execute(
            select(Workspace).where(Workspace.id != workspace.id).order_by(Workspace.created_at)
        )
    ).scalars().all()
    for other in other_workspaces:
        try:
            async with db.begin_nested():
                await seed_chat_harness(db, other.id)
                # Same reasoning as the chat-harness backfill above: a tenant
                # workspace created before the Subagent harness existed still
                # needs one, since it is the seeded target `spawn_subagent`
                # (once added) resolves against, not something a workspace
                # can be usably retrofitted without.
                await seed_subagent_harness(db, other.id)
        except Exception:  # noqa: BLE001 - one workspace's failure must not abort boot
            log.exception(
                "chat/subagent-harness backfill failed for workspace %s and was skipped",
                other.id,
            )
    await db.commit()

    # Self-host only: in multi-tenant mode this backstop must never run. Its
    # job is to catch a database that predates workspaces (or a hand-inserted
    # user row) and drop the orphan into the *oldest* workspace so self-host's
    # single-workspace invariant holds. In multi-tenant mode "the oldest
    # workspace" is just some tenant's paid workspace, and a user removed from
    # every workspace they belonged to (a deliberate offboarding) would be
    # silently re-added to it on the next boot — a tenancy leak masquerading
    # as a safety net. Multi-tenant relies on create_workspace and invite
    # redemption alone to keep every user membered; a user with none simply
    # gets the "belongs to no workspace" 409 from current_workspace, which is
    # correct there.
    if not get_settings().multi_tenant:
        await _ensure_every_user_has_a_membership(db, workspace.id)
    else:
        await _rehome_users_stranded_by_archive(db)


def _live_membership_user_ids():
    """User ids holding at least one membership in a non-archived workspace."""
    return (
        select(WorkspaceMember.user_id)
        .join(Workspace, Workspace.id == WorkspaceMember.workspace_id)
        .where(Workspace.archived_at.is_(None))
    )


async def _rehome_users_stranded_by_archive(db: AsyncSession) -> None:
    """Multi-tenant counterpart of the backstop below, narrowed on purpose.

    A user with *no* membership at all is a deliberate offboarding and stays
    that way (see the call site). A user whose memberships all point at
    archived workspaces is different: nobody chose that outcome for them, and
    `current_workspace` would answer 409 "belongs to no workspace" forever. Give
    each a personal workspace, the way first sign-in does, unless they already
    own one (it would have been a live membership, so this is only reachable
    for a user whose personal row went missing).
    """
    stranded = (
        await db.execute(
            select(User).where(
                User.id.in_(select(WorkspaceMember.user_id)),
                ~User.id.in_(_live_membership_user_ids()),
            )
        )
    ).scalars().all()
    for user in stranded:
        # `create_workspace` commits on its own, so no savepoint here: a
        # failure rolls back whatever it left pending and moves on.
        try:
            first_name = (user.display_name or user.email.split("@")[0]).split(" ")[0]
            await create_workspace(
                db, f"{first_name}'s workspace", kind="personal", owner=user,
                seed_demo_content=False,
            )
        except Exception:  # noqa: BLE001 - one user's failure must not abort boot
            await db.rollback()
            log.exception("could not re-home user %s after their workspace was archived", user.id)


async def _ensure_every_user_has_a_membership(db: AsyncSession, fallback_workspace_id) -> None:
    """Every user must belong to at least one workspace (api/workspace.py's
    `current_workspace` dependency assumes this). `create_workspace` and the
    invite-redemption flow (Phase C) both maintain the invariant going
    forward; this is the backstop for anything that slipped past them — a
    user row inserted directly, or a database that predates the
    backfill migration and was never re-migrated through it.

    Only live memberships count: a user whose sole workspace was archived is
    re-homed to `fallback_workspace_id` (the oldest live workspace) rather than
    left to 409 against `current_workspace` on every request.

    Self-host only — the caller gates this on `not multi_tenant`. See the
    call site for why it must never run in multi-tenant mode.
    """
    users_without_membership = (
        await db.execute(
            select(User).where(~User.id.in_(_live_membership_user_ids()))
        )
    ).scalars().all()
    if not users_without_membership:
        return
    for user in users_without_membership:
        db.add(
            WorkspaceMember(user_id=user.id, workspace_id=fallback_workspace_id, role=user.role)
        )
    await db.commit()
