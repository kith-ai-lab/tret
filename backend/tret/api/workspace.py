"""Workspace context: which workspace a request operates on, and whether the
current user's role in it is strong enough for what they are asking to do.

Every user has at least one membership by construction (services/bootstrap.py
guarantees it for self-host; services/workspace.py's create_workspace and the
invite-redemption flow, Phase C, maintain it going forward), so self-host's
single-workspace deployment always takes the sole-membership path below and
never sees the 409 — one code path serves both self-host and multi-tenant.
"""
from __future__ import annotations

import uuid

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tret.api.auth import _session_payload, current_user
from tret.db.engine import get_db
from tret.db.models import Project, User, Workspace, WorkspaceMember

# Weakest to strongest. A workspace's `owner` role belongs to whoever created
# it (or, for a personal workspace, the person it is for) and is otherwise
# ordinary — `admin` is the operational ceiling for member/invite management,
# matching self-host's global `admin` today.
ROLE_RANK: dict[str, int] = {"analyst": 0, "approver": 1, "admin": 2, "owner": 3}


class WorkspaceContext:
    """The workspace a request resolved to, plus the caller's role in it."""

    __slots__ = ("workspace", "role")

    def __init__(self, workspace: Workspace, role: str) -> None:
        self.workspace = workspace
        self.role = role

    @property
    def id(self) -> uuid.UUID:
        return self.workspace.id


async def user_memberships(db: AsyncSession, user_id: uuid.UUID) -> list[WorkspaceMember]:
    """Every workspace this user belongs to. A single-table, single-equality
    select on purpose — see the module docstring in services/workspace.py's
    sibling helpers for why this stays joinless: it is the lowest common
    query shape every test double in this codebase already knows how to
    fake, and the per-user membership count is always small.
    """
    return (
        await db.execute(select(WorkspaceMember).where(WorkspaceMember.user_id == user_id))
    ).scalars().all()


async def current_workspace(
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> WorkspaceContext:
    """Resolve the workspace a request operates on.

    Session `wid` -> validate the membership still exists -> else, if the user
    belongs to exactly one workspace, use that (the self-host path, and the
    path every cookie minted before `wid` existed takes) -> else 409 naming
    that a workspace must be chosen (`POST /api/auth/workspace`; the frontend
    switcher — Phase E — is what actually presents that choice).

    A `wid` that no longer names a membership (the user was removed from that
    workspace since the cookie was minted, or the cookie is simply stale) is
    not fatal: it falls through to the sole-membership path exactly as if no
    `wid` were present, rather than 401ing a session that is otherwise valid.

    A bearer caller (api/auth.py::current_user's other door) carries no
    session cookie at all, so `payload` is always `None` for one and it would
    409 on this exact "more than one workspace, nothing selected" branch on
    every request. `X-Tret-Workspace` is the bearer equivalent of `wid`: read
    only when the session payload didn't already supply one, and validated
    against this user's own memberships exactly as `wid` is below — it lets a
    caller pick among workspaces they already belong to, never grants
    membership in one they don't. A malformed or non-member value falls
    through to the same sole-membership/409 path a bad `wid` already does,
    not a 500.
    """
    memberships = await user_memberships(db, user.id)
    payload = _session_payload(request)
    wid = (payload.get("wid") if payload else None) or request.headers.get("X-Tret-Workspace")
    if wid:
        try:
            workspace_id = uuid.UUID(wid)
        except ValueError:
            workspace_id = None
        if workspace_id is not None:
            member = next((m for m in memberships if m.workspace_id == workspace_id), None)
            if member is not None:
                workspace = await db.get(Workspace, workspace_id)
                if workspace is not None:
                    return WorkspaceContext(workspace, member.role)

    if len(memberships) == 1:
        workspace = await db.get(Workspace, memberships[0].workspace_id)
        if workspace is not None:
            return WorkspaceContext(workspace, memberships[0].role)
    if not memberships:
        # Should not happen by construction (see module docstring), but a 409
        # naming the real problem beats a 500 or a silent wrong-workspace read.
        raise HTTPException(
            409,
            "This account belongs to no workspace. Contact an administrator.",
        )
    raise HTTPException(
        409,
        "Workspace selection is required: this account belongs to more than one "
        "workspace and no active one is selected. POST /api/auth/workspace with "
        "a workspace_id from GET /api/auth/me's `workspaces` list.",
    )


async def current_project(db: AsyncSession, workspace_id: uuid.UUID) -> Project | None:
    """The project this workspace operates on.

    Each workspace is single-project today (services/workspace.py's
    create_workspace seeds exactly one, and the UI has no project picker).
    This is the one place that assumption is written down; every
    project-scoped router (findings, runs, chat, documents) resolves "the
    current project" through here rather than re-deriving it, so they can
    never disagree about which project a workspace means.
    """
    return (
        await db.execute(
            select(Project).where(Project.workspace_id == workspace_id).order_by(Project.created_at)
        )
    ).scalars().first()


async def project_in_workspace(
    db: AsyncSession, project_id: uuid.UUID, workspace_id: uuid.UUID
) -> Project | None:
    """The project, only if it belongs to `workspace_id` — else None, which
    every caller here turns into a 404 rather than a 403: a member of one
    workspace must not be able to tell that a project/run/finding/document in
    another workspace exists at all."""
    project = await db.get(Project, project_id)
    if project is None or project.workspace_id != workspace_id:
        return None
    return project


def require_workspace_role(min_role: str):
    """Dependency factory: the caller's role in the current workspace must
    rank at or above `min_role` (owner > admin > approver > analyst)."""
    if min_role not in ROLE_RANK:
        raise ValueError(f"min_role must be one of {sorted(ROLE_RANK)}, got {min_role!r}")

    async def _dependency(
        ctx: WorkspaceContext = Depends(current_workspace),
    ) -> WorkspaceContext:
        if ROLE_RANK[ctx.role] < ROLE_RANK[min_role]:
            raise HTTPException(
                403, f"'{min_role}' role or higher is required in this workspace"
            )
        return ctx

    return _dependency


require_workspace_admin = require_workspace_role("admin")
require_workspace_approver = require_workspace_role("approver")
