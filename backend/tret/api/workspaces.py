"""Teams: creating one, its members, and invitations onto it.

`api/workspace.py` resolves *the current request's* workspace from the
session cookie (`current_workspace`); every endpoint here instead operates on
an explicit `{workspace_id}` path segment, because managing a workspace's
membership is not the same operation as working inside it — accepting an
invite, for one, has to name the workspace before the session has ever been
there. `_workspace_member`/`_workspace_admin` below are this file's own
path-scoped equivalent of `current_workspace`/`require_workspace_admin`.

Two routers: `router` (`/api/workspaces/...`) for everything that operates on
a workspace by id, and `invite_accept_router` (`/api/auth/invites/...`) for
the one endpoint that doesn't — accepting an invite is keyed by the token in
the link, not a workspace id the accepting user doesn't have yet — mounted
alongside `api/auth.py`'s own router in `main.py` for that reason.
"""
from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tret.api.auth import UserOut, _issue_session, _user_out, current_user
from tret.api.workspace import ROLE_RANK, WorkspaceContext
from tret.config import get_settings
from tret.db.engine import get_db
from tret.db.models import WORKSPACE_ROLES, Invite, User, Workspace, WorkspaceMember
from tret.engine.extensions import get_extension_registry
from tret.services.mailer import send_invite_email
from tret.services.workspace import (
    create_workspace,
    lock_workspace_for_seat_gate,
    redeem_workspace_seat,
)

router = APIRouter(prefix="/api/workspaces", tags=["workspaces"])
invite_accept_router = APIRouter(prefix="/api/auth", tags=["auth"])

# Invites never grant `owner` — that role belongs to whoever created the
# workspace (or, for a personal one, the person it's for), never to someone
# who merely followed a link. Same ceiling PATCH .../members/{user_id} applies
# to a role *change* below.
INVITABLE_ROLES = ("analyst", "approver", "admin")

INVITE_LIFETIME = timedelta(days=14)


# ── path-scoped workspace context (see module docstring) ────────────────────
async def _workspace_member(
    workspace_id: uuid.UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> WorkspaceContext:
    member = await db.get(WorkspaceMember, (user.id, workspace_id))
    if member is None:
        raise HTTPException(404, "Not a member of that workspace")
    workspace = await db.get(Workspace, workspace_id)
    if workspace is None:
        raise HTTPException(404, "Workspace not found")
    return WorkspaceContext(workspace, member.role)


async def _workspace_admin(ctx: WorkspaceContext = Depends(_workspace_member)) -> WorkspaceContext:
    if ROLE_RANK[ctx.role] < ROLE_RANK["admin"]:
        raise HTTPException(403, "'admin' role or higher is required in this workspace")
    return ctx


async def _owner_count(db: AsyncSession, workspace_id: uuid.UUID) -> int:
    # `with_for_update`, same as findings.py's decide_finding: two concurrent
    # demote/remove-last-owner requests must not both read "2 owners" and
    # both proceed — the second must block until the first commits (or rolls
    # back), then re-read the count it actually left behind.
    owners = (
        await db.execute(
            select(WorkspaceMember)
            .where(
                WorkspaceMember.workspace_id == workspace_id, WorkspaceMember.role == "owner"
            )
            .with_for_update()
        )
    ).scalars().all()
    return len(owners)


# ── responses ─────────────────────────────────────────────────────────────
class MemberOut(BaseModel):
    user_id: uuid.UUID
    email: str
    display_name: str
    role: str
    joined_at: datetime


class InviteOut(BaseModel):
    id: uuid.UUID
    email: str
    role: str
    status: str
    expires_at: datetime
    created_at: datetime


class InviteCreateOut(InviteOut):
    # The path only — `/invite/{token}` — for the frontend's copy-link
    # button to prefix with its own origin. Always present, whether or not
    # `email_sent` is true: copy-link is the fallback, not an afterthought.
    invite_url: str
    token: str
    email_sent: bool


async def _member_out(db: AsyncSession, member: WorkspaceMember) -> MemberOut:
    person = await db.get(User, member.user_id)
    return MemberOut(
        user_id=member.user_id,
        email=person.email if person else "",
        display_name=person.display_name if person else "",
        role=member.role,
        joined_at=member.created_at,
    )


def _invite_out(invite: Invite) -> InviteOut:
    return InviteOut(
        id=invite.id,
        email=invite.email,
        role=invite.role,
        status=invite.status,
        expires_at=invite.expires_at,
        created_at=invite.created_at,
    )


# ── bodies ────────────────────────────────────────────────────────────────
class CreateWorkspaceBody(BaseModel):
    name: str


class CreateInviteBody(BaseModel):
    email: str
    role: str = "analyst"


class UpdateMemberRoleBody(BaseModel):
    role: str


# ── create a team ────────────────────────────────────────────────────────
@router.post("", response_model=UserOut)
async def create_team_workspace(
    body: CreateWorkspaceBody,
    response: Response,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a new `kind="team"` workspace owned by the caller, and switch
    the session to it — the same re-mint `POST /api/auth/workspace` does,
    just against a workspace that did not exist a moment ago. No demo
    content: a team someone just created wants an empty workspace, not a
    stranger's sample project (`create_workspace`'s `seed_demo_content`)."""
    name = body.name.strip()
    if not name:
        raise HTTPException(422, "name is required")
    cap = get_settings().max_team_workspaces_per_user
    if cap > 0:
        owned = (
            await db.execute(
                select(WorkspaceMember)
                .join(Workspace, Workspace.id == WorkspaceMember.workspace_id)
                .where(
                    WorkspaceMember.user_id == user.id,
                    WorkspaceMember.role == "owner",
                    Workspace.kind == "team",
                )
            )
        ).scalars().all()
        if len(owned) >= cap:
            raise HTTPException(
                403,
                f"You already own {len(owned)} team workspace(s), the limit for this "
                "deployment. Ask an existing team's owner to add you instead of "
                "creating a new one.",
            )
    workspace = await create_workspace(db, name, kind="team", owner=user, seed_demo_content=False)
    _issue_session(response, user, wid=workspace.id)
    return await _user_out(user, db, preferred_wid=workspace.id)


# ── members ───────────────────────────────────────────────────────────────
@router.get("/{workspace_id}/members", response_model=list[MemberOut])
async def list_members(
    ctx: WorkspaceContext = Depends(_workspace_member),
    db: AsyncSession = Depends(get_db),
):
    members = (
        await db.execute(select(WorkspaceMember).where(WorkspaceMember.workspace_id == ctx.id))
    ).scalars().all()
    return [await _member_out(db, m) for m in members]


@router.patch("/{workspace_id}/members/{user_id}", response_model=MemberOut)
async def update_member_role(
    user_id: uuid.UUID,
    body: UpdateMemberRoleBody,
    ctx: WorkspaceContext = Depends(_workspace_admin),
    db: AsyncSession = Depends(get_db),
):
    if ctx.workspace.kind == "personal":
        raise HTTPException(
            403, "Personal workspaces have exactly one member and cannot be changed."
        )
    new_role = body.role.strip().lower()
    if new_role not in WORKSPACE_ROLES:
        raise HTTPException(422, f"role must be one of {'|'.join(WORKSPACE_ROLES)}")
    # Owner is grantable only by an owner — an admin managing membership must
    # not be able to promote themselves (or anyone) past their own ceiling.
    if new_role == "owner" and ctx.role != "owner":
        raise HTTPException(403, "Only an owner can grant the owner role.")

    member = await db.get(WorkspaceMember, (user_id, ctx.id))
    if member is None:
        raise HTTPException(404, "Not a member of this workspace")

    # Symmetric with the grant check above: an admin ranks high enough to
    # manage membership in general, but demoting an owner is itself an
    # owner-only action — the frontend already enforces this client-side
    # (owner-only demote/remove controls), this is the same rule server-side.
    if member.role == "owner" and ctx.role != "owner":
        raise HTTPException(403, "Only an owner can change another owner's role.")

    if member.role == "owner" and new_role != "owner" and await _owner_count(db, ctx.id) <= 1:
        raise HTTPException(409, "Refusing to demote the last owner of this workspace.")

    member.role = new_role
    await db.commit()
    return await _member_out(db, member)


@router.delete("/{workspace_id}/members/{user_id}")
async def remove_member(
    user_id: uuid.UUID,
    ctx: WorkspaceContext = Depends(_workspace_member),
    caller: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Self-leave is always allowed (for anyone but the last owner); removing
    someone else requires admin. Personal workspaces refuse this outright —
    `personal_owner_id` can never leave or be removed, by founder decision
    (see db/models.py's Workspace docstring)."""
    if ctx.workspace.kind == "personal":
        raise HTTPException(
            403, "Personal workspaces have exactly one member, who can never leave or be removed."
        )
    is_self = user_id == caller.id
    if not is_self and ROLE_RANK[ctx.role] < ROLE_RANK["admin"]:
        raise HTTPException(403, "'admin' role or higher is required to remove another member")

    member = await db.get(WorkspaceMember, (user_id, ctx.id))
    if member is None:
        raise HTTPException(404, "Not a member of this workspace")

    # Same owner-only ceiling as update_member_role: removing an owner is an
    # owner-only action. Never blocks an owner leaving on their own — is_self
    # here means ctx.role is that same owner's own role.
    if member.role == "owner" and ctx.role != "owner":
        raise HTTPException(403, "Only an owner can remove another owner.")

    if member.role == "owner" and await _owner_count(db, ctx.id) <= 1:
        raise HTTPException(409, "Refusing to remove the last owner of this workspace.")

    await db.delete(member)
    await db.commit()
    return {"ok": True}


# ── invites ───────────────────────────────────────────────────────────────
@router.post("/{workspace_id}/invites", response_model=InviteCreateOut)
async def create_invite(
    body: CreateInviteBody,
    ctx: WorkspaceContext = Depends(_workspace_admin),
    caller: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    if ctx.workspace.kind == "personal":
        raise HTTPException(
            403, "Personal workspaces cannot be invited to — create a team workspace instead."
        )
    role = body.role.strip().lower()
    if role not in INVITABLE_ROLES:
        raise HTTPException(422, f"role must be one of {'|'.join(INVITABLE_ROLES)}")
    email = body.email.strip().lower()
    if not email or "@" not in email:
        raise HTTPException(422, "a valid email is required")

    # Same lock as seat redemption (services/workspace.py::redeem_workspace_seat)
    # ahead of its own gate, for the same reason: without it, two concurrent
    # invite creations at seats-minus-one could each see one seat "free" from
    # their own fresh gate session and both be allowed.
    await lock_workspace_for_seat_gate(db, ctx.id)
    gate = await get_extension_registry().check_workspace_gate(db, ctx.id, "invite")
    if not gate.allowed:
        raise HTTPException(403, detail={"reason": gate.reason, "detail": gate.detail})

    invite = Invite(
        workspace_id=ctx.id,
        email=email,
        role=role,
        token=secrets.token_urlsafe(32),
        invited_by=caller.id,
        status="pending",
        expires_at=datetime.now(timezone.utc) + INVITE_LIFETIME,
    )
    db.add(invite)
    await db.commit()

    invite_path = f"/invite/{invite.token}"
    absolute_url = f"{get_settings().app_url.rstrip('/')}{invite_path}"
    send_result = await send_invite_email(email, ctx.workspace.name, absolute_url, caller.display_name)

    out = _invite_out(invite)
    return InviteCreateOut(
        **out.model_dump(), invite_url=invite_path, token=invite.token, email_sent=send_result.sent
    )


@router.get("/{workspace_id}/invites", response_model=list[InviteOut])
async def list_invites(
    ctx: WorkspaceContext = Depends(_workspace_admin),
    db: AsyncSession = Depends(get_db),
):
    invites = (
        await db.execute(
            select(Invite)
            .where(Invite.workspace_id == ctx.id)
            .order_by(Invite.created_at.desc())
        )
    ).scalars().all()
    return [_invite_out(i) for i in invites]


@router.delete("/{workspace_id}/invites/{invite_id}")
async def revoke_invite(
    invite_id: uuid.UUID,
    ctx: WorkspaceContext = Depends(_workspace_admin),
    db: AsyncSession = Depends(get_db),
):
    invite = await db.get(Invite, invite_id)
    if invite is None or invite.workspace_id != ctx.id:
        raise HTTPException(404, "Invite not found")
    invite.status = "revoked"
    await db.commit()
    return {"ok": True}


@invite_accept_router.post("/invites/{token}/accept", response_model=UserOut)
async def accept_invite(
    token: str,
    response: Response,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Redeem an invite link as the logged-in user it was sent to.

    404 for anything that is not a live, pending invite — unknown, revoked,
    expired, already accepted — rather than distinguishing those, the same
    "existence itself must not leak" reasoning `api/workspace.py` gives for
    cross-workspace reads: a used-up invite link should not tell whoever is
    holding it *why* it stopped working. Email mismatch is a 403 instead,
    because the invite plainly does exist and the signed-in account is simply
    not who it was sent to — a different, nameable problem.
    """
    invite = (await db.execute(select(Invite).where(Invite.token == token))).scalar_one_or_none()
    if invite is None or invite.status != "pending":
        raise HTTPException(404, "This invite link is invalid or has expired.")
    # Postgres always returns a tz-aware TIMESTAMPTZ; a naive value only shows
    # up under sqlite (dev/test), which drops tzinfo on the round trip — the
    # same normalisation a hosting extension's cursor decode applies for the
    # same reason. Treated as UTC either way, since that's what every
    # `expires_at` this codebase writes always is.
    expires_at = invite.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= datetime.now(timezone.utc):
        raise HTTPException(404, "This invite link is invalid or has expired.")
    if invite.email.strip().lower() != user.email.strip().lower():
        raise HTTPException(403, "This invite was sent to a different email address.")

    member = await db.get(WorkspaceMember, (user.id, invite.workspace_id))
    if member is None:
        # Same seat-limit checkpoint (now the same helper, too — see
        # services/workspace.py::redeem_workspace_seat) services/identity.py's
        # OIDC-login redemption asks — a link visited by an already-logged-in
        # user is otherwise a second, ungated door into a seat-limited team.
        # The invite is left pending (not consumed): a freed-up seat later,
        # or a different account, can still use it.
        gate = await redeem_workspace_seat(db, invite.workspace_id, user.id, invite.role)
        if not gate.allowed:
            raise HTTPException(403, detail={"reason": gate.reason, "detail": gate.detail})
    invite.status = "accepted"
    await db.commit()

    _issue_session(response, user, wid=invite.workspace_id)
    return await _user_out(user, db, preferred_wid=invite.workspace_id)


__all__ = ["router", "invite_accept_router"]
