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
from tret.services.workspace import create_workspace, seed_workspace_content

log = logging.getLogger("tret.bootstrap")


async def bootstrap(db: AsyncSession) -> None:
    await bootstrap_admin(db)

    workspace = (
        await db.execute(select(Workspace).order_by(Workspace.created_at))
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


async def _ensure_every_user_has_a_membership(db: AsyncSession, fallback_workspace_id) -> None:
    """Every user must belong to at least one workspace (api/workspace.py's
    `current_workspace` dependency assumes this). `create_workspace` and the
    invite-redemption flow (Phase C) both maintain the invariant going
    forward; this is the backstop for anything that slipped past them — a
    user row inserted directly, or a database that predates the
    backfill migration and was never re-migrated through it.

    Self-host only — the caller gates this on `not multi_tenant`. See the
    call site for why it must never run in multi-tenant mode.
    """
    users_without_membership = (
        await db.execute(
            select(User).where(
                ~User.id.in_(select(WorkspaceMember.user_id))
            )
        )
    ).scalars().all()
    if not users_without_membership:
        return
    for user in users_without_membership:
        db.add(
            WorkspaceMember(user_id=user.id, workspace_id=fallback_workspace_id, role=user.role)
        )
    await db.commit()
