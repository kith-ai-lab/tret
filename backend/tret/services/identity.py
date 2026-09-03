"""Match-or-provision: turn a verified (or founder-permitted unverified)
OIDC identity into a tret `User`, mirroring the same self-host assumptions
`api/auth.py::create_user` and `services/bootstrap.py` already make.

Three outcomes, checked in this order:

1. `oidc_sub` already on a `User` row — this identity has signed in before.
2. A verified email matches an existing (password or OIDC) account — link by
   setting `oidc_sub` on it. An *unverified* email match is refused (409):
   tret cannot tell an attacker's freshly-registered lookalike account from
   the real owner without a verified claim, so it will not silently attach a
   new SSO identity to someone else's account. A `sub` already claimed by a
   *different* `oidc_sub` on that row is refused the same way (409) — same
   email, but this IdP identity is not the one already linked.
3. Neither matches — JIT-provision a new `analyst` with no password, gated by
   `oidc_allowed_email_domains` and, when that is unset, by
   `multi_tenant`/invitation — see `match_or_provision`'s own docstring for
   the exact rule. Outcomes 1 and 2 above are never gated this way: a user
   who already has an account keeps signing in even if this configuration
   tightens later.

`multi_tenant` (tret/config.py) decides what a fresh JIT user's home
workspace is: their own personal workspace, or self-host's sole workspace —
exactly the switch `services/workspace.py::create_workspace` documents.

Invite redemption rides along on every path (not just JIT): a verified email
with pending, unexpired invites joins those workspaces too, and the session
lands in the most recently redeemed team workspace rather than the user's own
— the whole point of following an invite link is to end up on the team you
were invited to, not on your personal one.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tret.config import get_settings
from tret.db.models import Invite, User, Workspace, WorkspaceMember
from tret.services.workspace import create_workspace, redeem_workspace_seat


def _as_utc(dt: datetime) -> datetime:
    """Postgres always returns a tz-aware TIMESTAMPTZ; a naive value only
    shows up under sqlite (dev/test), which drops tzinfo on the round trip —
    the same normalisation `api/workspaces.py`'s invite-expiry check applies
    (`accept_invite`), needed here for the same reason: comparing it directly
    against `datetime.now(timezone.utc)` raises under sqlite otherwise."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _disabled_check(user: User) -> None:
    if user.disabled:
        # Unlike password login (api/auth.py::login), which folds "wrong
        # password" and "deactivated" into one indistinguishable answer on
        # purpose (nothing to guess here — trying a password IS the guess),
        # OIDC has no password to guess: the IdP already vouches for the
        # identity, so naming the real reason costs nothing and saves a
        # confused support ticket.
        raise HTTPException(403, "This account has been deactivated. Contact an administrator.")


async def _redeem_invites_and_resolve_workspace(
    db: AsyncSession,
    user: User,
    email: str,
    email_verified: bool,
    *,
    fallback_workspace_id: uuid.UUID | None,
) -> uuid.UUID | None:
    """Redeem every pending, unexpired invite addressed to `email` (only when
    verified — the founder decision that lets Auth0 allow unverified login
    stops short of letting an unverified email pull someone else's invite),
    then decide which workspace the session should land in: the most recently
    redeemed team workspace if any, else `fallback_workspace_id` (the
    caller's own workspace, when they just have one to fall back to), else
    whatever this user's sole existing membership already is.

    A plain `select(Invite).where(status == 'pending')` rather than an
    email-filtered query on purpose: Invite.email is compared case-
    insensitively in Python (`.lower()`), and the pending set is small — an
    operator's whole outstanding-invite backlog, not a table this filters
    into a slice worth pushing into SQL.

    Ordered by `workspace_id`: the loop below takes a `SELECT ... FOR UPDATE`
    on each invite's Workspace (inside `redeem_workspace_seat`) within this
    one transaction. Two concurrent logins each redeeming invites to the same
    two workspaces in opposite orders would otherwise lock them in opposite
    orders too and deadlock on Postgres; a fixed, shared order across every
    caller rules that out.
    """
    redeemed_workspace_id: uuid.UUID | None = None
    if email_verified:
        now = datetime.now(timezone.utc)
        pending = (
            await db.execute(
                select(Invite).where(Invite.status == "pending").order_by(Invite.workspace_id)
            )
        ).scalars().all()
        matching = [
            inv for inv in pending
            if inv.email.strip().lower() == email and _as_utc(inv.expires_at) > now
        ]
        for invite in matching:
            member = await db.get(WorkspaceMember, (user.id, invite.workspace_id))
            if member is None:
                # tret_cloud's team-plan seat limit hooks in at exactly this
                # point: a blocked gate skips this invite entirely — no
                # membership, invite left `pending`, nothing redeemed for it —
                # rather than failing the login. The invite link still works
                # later if a seat frees up; the sign-in that happened to
                # arrive while the workspace was full just doesn't jump the
                # queue. Same lock-then-gate-then-insert helper
                # `api/workspaces.py::accept_invite` uses, so the two
                # redemption paths cannot drift apart on this sequence.
                gate = await redeem_workspace_seat(db, invite.workspace_id, user.id, invite.role)
                if not gate.allowed:
                    continue
            invite.status = "accepted"
            redeemed_workspace_id = invite.workspace_id
        if matching:
            await db.commit()

    if redeemed_workspace_id is not None:
        return redeemed_workspace_id
    if fallback_workspace_id is not None:
        return fallback_workspace_id
    memberships = (
        await db.execute(select(WorkspaceMember).where(WorkspaceMember.user_id == user.id))
    ).scalars().all()
    if len(memberships) == 1:
        return memberships[0].workspace_id
    return None  # more than one (or zero) membership: no single answer, same as _resolve_workspaces


async def _has_pending_invite(db: AsyncSession, email: str) -> bool:
    """Whether `email` has a live (pending, unexpired) invite waiting —
    the same set `_redeem_invites_and_resolve_workspace` above would redeem,
    checked earlier here as a precondition for JIT-provisioning at all
    rather than as something to act on."""
    now = datetime.now(timezone.utc)
    pending = (await db.execute(select(Invite).where(Invite.status == "pending"))).scalars().all()
    return any(
        inv.email.strip().lower() == email and _as_utc(inv.expires_at) > now for inv in pending
    )


async def _join_sole_workspace(db: AsyncSession, user: User) -> uuid.UUID:
    """Self-host's path for a freshly JIT-provisioned user: join the one
    workspace that already exists, mirroring `api/auth.py::create_user`'s
    single-workspace inference (and, like it, at the account's own role —
    every self-host user's workspace role has always been its global role).

    Oldest first, same tiebreak `services/bootstrap.py`'s membership backstop
    uses, for the same reason: if an operator hand-created more than one
    workspace without ever wiring a picker to it (multi_tenant is off, so
    nothing in this deployment expects more than one), there is no signal to
    prefer a newer one over the original.
    """
    workspace = (
        await db.execute(select(Workspace).order_by(Workspace.created_at))
    ).scalars().first()
    if workspace is None:
        raise HTTPException(
            500, "No workspace exists for a new account to join. Contact an administrator."
        )
    db.add(WorkspaceMember(user_id=user.id, workspace_id=workspace.id, role=user.role))
    await db.commit()
    return workspace.id


async def match_or_provision(
    db: AsyncSession,
    *,
    sub: str,
    email: str,
    email_verified: bool,
    display_name: str,
) -> tuple[User, uuid.UUID | None]:
    """Resolve a verified-by-the-IdP OIDC identity to a `User`, provisioning
    one if this is its first sign-in.

    Returns `(user, workspace_id)` — the workspace the session's `wid` should
    carry, ready for `api/auth.py::_issue_session`. `workspace_id` is `None`
    only when it cannot be resolved to exactly one (no membership, or more
    than one with nothing redeemed) — same "no single answer" case
    `_resolve_workspaces` already handles for password login, and for the
    same reason: this endpoint must always succeed, leaving workspace
    selection to `POST /api/auth/workspace`.
    """
    email = email.strip().lower()

    existing = (await db.execute(select(User).where(User.oidc_sub == sub))).scalar_one_or_none()
    if existing is not None:
        _disabled_check(existing)
        workspace_id = await _redeem_invites_and_resolve_workspace(
            db, existing, email, email_verified, fallback_workspace_id=None
        )
        return existing, workspace_id

    users = (await db.execute(select(User))).scalars().all()
    by_email = next((u for u in users if u.email.strip().lower() == email), None)
    if by_email is not None:
        _disabled_check(by_email)
        if not email_verified:
            raise HTTPException(
                409,
                "An account with this email already exists. Verify your email with your "
                "identity provider to link single sign-on, or sign in with your password.",
            )
        if by_email.oidc_sub is not None and by_email.oidc_sub != sub:
            raise HTTPException(
                409, "This email is already linked to a different single sign-on identity."
            )
        by_email.oidc_sub = sub
        await db.commit()
        workspace_id = await _redeem_invites_and_resolve_workspace(
            db, by_email, email, email_verified, fallback_workspace_id=None
        )
        return by_email, workspace_id

    # Neither matched: JIT-provision. Auth0 is configured to allow unverified
    # login (founder decision), so this happens even for an unverified email
    # — there is nothing to link or collide with yet, unlike the two branches
    # above. It just gets no invite redemption below (email_verified gates
    # that), same as it gets no auto-link.
    #
    # Two independent gates before a brand-new account is created — neither
    # applies to the sub/email-match branches above, so an existing linked
    # account keeps signing in even if this configuration changes later:
    #
    # 1. A configured domain allowlist (`oidc_allowed_email_domains`) is
    #    absolute: any IdP-authenticated stranger at an unlisted domain is
    #    refused, in every deployment mode.
    # 2. With no allowlist configured, self-host (`multi_tenant=False`) falls
    #    back to requiring an invitation — the sole-workspace deployment used
    #    to JIT-provision *any* IdP-authenticated stranger into its one
    #    workspace, which is the tenancy hole this whole gate closes.
    #    `multi_tenant=True` keeps today's open signup (a paying tenant's own
    #    IdP has already done the vetting tret would otherwise be redoing).
    settings = get_settings()
    if settings.oidc_allowed_email_domains:
        domain = email.rsplit("@", 1)[-1]
        if domain not in settings.oidc_allowed_email_domains:
            raise HTTPException(
                403, "Your email domain is not permitted to sign in to this deployment."
            )
    elif not settings.multi_tenant and not (
        email_verified and await _has_pending_invite(db, email)
    ):
        # `email_verified` gates the invite fallback for the same reason it
        # gates redemption and account linking: an unverified claim is just a
        # string the IdP user typed, so honoring it here would let a stranger
        # squat an invited address — provisioning a User bound to *their* sub
        # and locking the real invitee out with a 409 at the linking branch.
        raise HTTPException(403, "This deployment requires an invitation.")

    user = User(
        email=email,
        display_name=display_name.strip() if display_name and display_name.strip() else email.split("@")[0],
        password_hash=None,
        role="analyst",
        oidc_sub=sub,
        disabled=False,
    )
    db.add(user)
    await db.flush()

    if settings.multi_tenant:
        first_name = user.display_name.split(" ")[0] if user.display_name else email.split("@")[0]
        workspace = await create_workspace(
            db, f"{first_name}'s workspace", kind="personal", owner=user, seed_demo_content=False
        )
        home_workspace_id = workspace.id
    else:
        home_workspace_id = await _join_sole_workspace(db, user)

    workspace_id = await _redeem_invites_and_resolve_workspace(
        db, user, email, email_verified, fallback_workspace_id=home_workspace_id
    )
    return user, workspace_id
