"""`tret/api/workspaces.py`: creating a team, its members, and invitations.

Real dependency chain, real database — a real (sqlite, via
`tests.evals.golden_world.install_sqlite_type_shims`) engine and the actual
ORM models, driven through `httpx.AsyncClient` + `ASGITransport` directly
against the app rather than `TestClient`'s portal thread (the same reason
a hosting extension's own `api_client_factory` fixture does this: every fixture and
every request then share one asyncio event loop, so there is no cross-loop
asyncpg-style restriction to work around). The query shapes under test here
— membership existence, owner counts, invite status/expiry filters — are
exactly the kind a hand-rolled fake session gets subtly wrong, so a real
database is worth the extra fixture weight.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import pytest_asyncio
from argon2 import PasswordHasher
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from tests.evals.golden_world import install_sqlite_type_shims
from tret.api import auth, workspaces as workspaces_api
from tret.db.engine import get_db
from tret.db.models import Base, Invite, Pack, User, Workspace, WorkspaceMember
from tret.engine import extensions as extensions_module
from tret.engine.extensions import ExtensionAPI, GateResult

HASHER = PasswordHasher()
PASSWORD = "correct-horse-battery-1"


@pytest.fixture(autouse=True)
def _reset_extension_registry():
    """Every test starts with no gates registered — the same isolation
    test_extensions.py gives its own tests, needed here because a couple of
    tests below register one to exercise the gate-blocks-the-endpoint wiring."""
    extensions_module._registry = None
    yield
    extensions_module._registry = None


@pytest_asyncio.fixture
async def engine():
    install_sqlite_type_shims()
    eng = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def seed(session_factory):
    """`await seed(row, row, ...)` — inserts and commits, in its own session,
    entirely separate from the sessions the app's own requests use."""

    async def _seed(*rows):
        async with session_factory() as db:
            db.add_all(rows)
            await db.commit()

    return _seed


@pytest_asyncio.fixture
async def client(session_factory):
    app = FastAPI()
    app.include_router(auth.router)
    app.include_router(workspaces_api.router)
    app.include_router(workspaces_api.invite_accept_router)

    async def _get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _get_db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ── fixture builders ─────────────────────────────────────────────────────────
def make_user(email: str, *, password: str = PASSWORD, role: str = "analyst") -> User:
    return User(
        id=uuid.uuid4(),
        email=email,
        display_name=email.split("@")[0].title(),
        password_hash=HASHER.hash(password),
        role=role,
    )


def make_workspace(name: str, *, kind: str = "team", personal_owner_id=None) -> Workspace:
    return Workspace(id=uuid.uuid4(), name=name, kind=kind, personal_owner_id=personal_owner_id)


def make_member(user: User, workspace: Workspace, *, role: str) -> WorkspaceMember:
    return WorkspaceMember(user_id=user.id, workspace_id=workspace.id, role=role)


def make_invite(
    workspace: Workspace, *, email: str, role: str = "analyst", status: str = "pending", hours: float = 24
) -> Invite:
    return Invite(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        email=email,
        role=role,
        token=uuid.uuid4().hex,
        status=status,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=hours),
    )


async def login(client: httpx.AsyncClient, email: str, password: str = PASSWORD) -> httpx.Response:
    response = await client.post("/api/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response


# ── creating a team ──────────────────────────────────────────────────────────
async def test_creating_a_team_switches_the_session_to_it(client, seed):
    home = make_workspace("Solo")
    user = make_user("ada@example.com")
    await seed(home, user, make_member(user, home, role="owner"))
    await login(client, user.email)

    response = await client.post("/api/workspaces", json={"name": "Climate Co"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["role"] == "owner"
    new_workspace_id = body["current_workspace_id"]
    assert new_workspace_id != str(home.id)
    assert {w["id"] for w in body["workspaces"]} == {str(home.id), new_workspace_id}

    me = await client.get("/api/auth/me")
    assert me.json()["current_workspace_id"] == new_workspace_id


async def test_creating_a_team_gets_the_default_pack_in_multi_tenant_mode(
    client, seed, session_factory, monkeypatch
):
    """services/workspace.py::create_workspace's pack installation now
    follows TRET_SEED_DEFAULT_PACKS (true by default), independent of
    TRET_MULTI_TENANT — a team created on a multi-tenant deployment still
    gets the flagship pack, even though it still gets none of the *other*
    demo content (seed_demo_content=False here, unaffected by this change)."""
    from tret.config import get_settings

    home = make_workspace("Solo")
    user = make_user("teampack@example.com")
    await seed(home, user, make_member(user, home, role="owner"))
    await login(client, user.email)

    monkeypatch.setenv("TRET_MULTI_TENANT", "true")
    get_settings.cache_clear()
    try:
        response = await client.post("/api/workspaces", json={"name": "Climate Co"})
        assert response.status_code == 200, response.text
        new_workspace_id = response.json()["current_workspace_id"]
    finally:
        monkeypatch.delenv("TRET_MULTI_TENANT", raising=False)
        get_settings.cache_clear()

    async with session_factory() as db:
        pack = (
            await db.execute(
                select(Pack).where(
                    Pack.workspace_id == uuid.UUID(new_workspace_id),
                    Pack.slug == "climate-risk",
                )
            )
        ).scalars().first()
        assert pack is not None


async def test_creating_a_team_is_capped_when_configured(client, seed, monkeypatch):
    from tret.config import get_settings

    home = make_workspace("Solo", kind="personal")
    user = make_user("cap@example.com")
    await seed(home, user, make_member(user, home, role="owner"))
    await login(client, user.email)

    monkeypatch.setenv("TRET_MAX_TEAM_WORKSPACES_PER_USER", "1")
    get_settings.cache_clear()
    try:
        first = await client.post("/api/workspaces", json={"name": "Climate Co"})
        assert first.status_code == 200, first.text

        second = await client.post("/api/workspaces", json={"name": "Second Co"})
        assert second.status_code == 403
    finally:
        monkeypatch.delenv("TRET_MAX_TEAM_WORKSPACES_PER_USER", raising=False)
        get_settings.cache_clear()


async def test_the_cap_only_counts_workspaces_this_user_owns(client, seed, monkeypatch):
    """Being a mere member (not owner) of other team workspaces, and owning
    a *personal* workspace, must not count against the cap."""
    from tret.config import get_settings

    home = make_workspace("Personal", kind="personal", personal_owner_id=None)
    other_team = make_workspace("Someone Else's Co")
    user = make_user("capmember@example.com")
    other_owner = make_user("otherowner@example.com")
    await seed(
        home, other_team, user, other_owner,
        make_member(user, home, role="owner"),
        make_member(user, other_team, role="analyst"),  # member, not owner
        make_member(other_owner, other_team, role="owner"),
    )
    await login(client, user.email)

    monkeypatch.setenv("TRET_MAX_TEAM_WORKSPACES_PER_USER", "1")
    get_settings.cache_clear()
    try:
        response = await client.post("/api/workspaces", json={"name": "My First Team"})
        assert response.status_code == 200, response.text
    finally:
        monkeypatch.delenv("TRET_MAX_TEAM_WORKSPACES_PER_USER", raising=False)
        get_settings.cache_clear()


async def test_the_default_cap_is_unlimited(client, seed):
    home = make_workspace("Solo")
    user = make_user("uncapped@example.com")
    await seed(home, user, make_member(user, home, role="owner"))
    await login(client, user.email)

    for i in range(3):
        response = await client.post("/api/workspaces", json={"name": f"Team {i}"})
        assert response.status_code == 200, response.text


async def test_a_blank_name_is_rejected(client, seed):
    home = make_workspace("Solo")
    user = make_user("blank@example.com")
    await seed(home, user, make_member(user, home, role="owner"))
    await login(client, user.email)

    response = await client.post("/api/workspaces", json={"name": "   "})
    assert response.status_code == 422


# ── owner-count row locking (concurrent-double-demotion TOCTOU) ────────────
async def test_owner_count_query_requests_row_locking():
    """`with_for_update`, same defense test_approval_gate.py's
    `test_the_finding_row_is_locked_while_it_is_decided` gives `decide_
    finding`'s own row: without it, two concurrent requests to demote/remove
    a workspace's last owner could both read "2 owners" and both proceed —
    only reachable with two live Postgres transactions, so (like that other
    test) this checks the lock is actually requested rather than racing two
    real transactions."""
    captured = {}

    class RecordingSession:
        async def execute(self, stmt):
            captured["stmt"] = stmt

            class _Result:
                def scalars(self):
                    return self

                def all(self):
                    return []

            return _Result()

    await workspaces_api._owner_count(RecordingSession(), uuid.uuid4())
    assert captured["stmt"]._for_update_arg is not None


# ── members ───────────────────────────────────────────────────────────────
async def test_members_are_listed_for_any_member(client, seed):
    team = make_workspace("Climate Co")
    owner = make_user("owner@example.com")
    analyst = make_user("analyst@example.com")
    await seed(
        team, owner, analyst,
        make_member(owner, team, role="owner"),
        make_member(analyst, team, role="analyst"),
    )
    await login(client, analyst.email)

    response = await client.get(f"/api/workspaces/{team.id}/members")
    assert response.status_code == 200
    emails = {m["email"] for m in response.json()}
    assert emails == {owner.email, analyst.email}


async def test_a_non_member_cannot_list_members(client, seed):
    team = make_workspace("Climate Co")
    owner = make_user("owner2@example.com")
    outsider = make_user("outsider@example.com")
    outsider_home = make_workspace("Outsider's home")
    await seed(
        team, owner, outsider, outsider_home,
        make_member(owner, team, role="owner"),
        make_member(outsider, outsider_home, role="owner"),
    )
    await login(client, outsider.email)

    response = await client.get(f"/api/workspaces/{team.id}/members")
    assert response.status_code == 404


# ── role changes ──────────────────────────────────────────────────────────
async def test_an_admin_can_promote_and_demote_a_member(client, seed):
    team = make_workspace("Climate Co")
    owner = make_user("owner3@example.com")
    admin = make_user("admin3@example.com")
    analyst = make_user("analyst3@example.com")
    await seed(
        team, owner, admin, analyst,
        make_member(owner, team, role="owner"),
        make_member(admin, team, role="admin"),
        make_member(analyst, team, role="analyst"),
    )
    await login(client, admin.email)

    response = await client.patch(
        f"/api/workspaces/{team.id}/members/{analyst.id}", json={"role": "approver"}
    )
    assert response.status_code == 200
    assert response.json()["role"] == "approver"


async def test_only_an_owner_can_grant_the_owner_role(client, seed):
    team = make_workspace("Climate Co")
    owner = make_user("owner4@example.com")
    admin = make_user("admin4@example.com")
    await seed(
        team, owner, admin,
        make_member(owner, team, role="owner"),
        make_member(admin, team, role="admin"),
    )
    await login(client, admin.email)

    response = await client.patch(
        f"/api/workspaces/{team.id}/members/{admin.id}", json={"role": "owner"}
    )
    assert response.status_code == 403

    await client.post("/api/auth/logout")
    await login(client, owner.email)
    response = await client.patch(
        f"/api/workspaces/{team.id}/members/{admin.id}", json={"role": "owner"}
    )
    assert response.status_code == 200
    assert response.json()["role"] == "owner"


async def test_the_last_owner_cannot_be_demoted(client, seed):
    team = make_workspace("Climate Co")
    owner = make_user("owner5@example.com")
    admin = make_user("admin5@example.com")
    await seed(
        team, owner, admin,
        make_member(owner, team, role="owner"),
        make_member(admin, team, role="admin"),
    )
    await login(client, owner.email)

    response = await client.patch(
        f"/api/workspaces/{team.id}/members/{owner.id}", json={"role": "admin"}
    )
    assert response.status_code == 409


async def test_an_unknown_role_is_rejected(client, seed):
    team = make_workspace("Climate Co")
    owner = make_user("owner6@example.com")
    await seed(team, owner, make_member(owner, team, role="owner"))
    await login(client, owner.email)

    response = await client.patch(
        f"/api/workspaces/{team.id}/members/{owner.id}", json={"role": "superuser"}
    )
    assert response.status_code == 422


# ── leaving / removing ──────────────────────────────────────────────────────
async def test_a_member_can_leave_on_their_own(client, seed):
    team = make_workspace("Climate Co")
    owner = make_user("owner7@example.com")
    analyst = make_user("analyst7@example.com")
    await seed(
        team, owner, analyst,
        make_member(owner, team, role="owner"),
        make_member(analyst, team, role="analyst"),
    )
    await login(client, analyst.email)

    response = await client.delete(f"/api/workspaces/{team.id}/members/{analyst.id}")
    assert response.status_code == 200

    await client.post("/api/auth/logout")
    await login(client, owner.email)
    emails = {m["email"] for m in (await client.get(f"/api/workspaces/{team.id}/members")).json()}
    assert analyst.email not in emails


async def test_removing_someone_else_requires_admin(client, seed):
    team = make_workspace("Climate Co")
    owner = make_user("owner8@example.com")
    analyst_a = make_user("analyst8a@example.com")
    analyst_b = make_user("analyst8b@example.com")
    await seed(
        team, owner, analyst_a, analyst_b,
        make_member(owner, team, role="owner"),
        make_member(analyst_a, team, role="analyst"),
        make_member(analyst_b, team, role="analyst"),
    )
    await login(client, analyst_a.email)

    response = await client.delete(f"/api/workspaces/{team.id}/members/{analyst_b.id}")
    assert response.status_code == 403


async def test_the_last_owner_cannot_be_removed(client, seed):
    """Self-removal by the sole owner — an admin is never authorized to
    remove an owner at all (see test_only_an_owner_can_remove_another_owner
    below), so the last-owner guard has to be exercised by an owner."""
    team = make_workspace("Climate Co")
    owner = make_user("owner9@example.com")
    admin = make_user("admin9@example.com")
    await seed(
        team, owner, admin,
        make_member(owner, team, role="owner"),
        make_member(admin, team, role="admin"),
    )
    await login(client, owner.email)

    response = await client.delete(f"/api/workspaces/{team.id}/members/{owner.id}")
    assert response.status_code == 409


async def test_only_an_owner_can_demote_another_owner(client, seed):
    """An admin ranks high enough to manage membership in general, but not
    to touch an owner's role at all — only another owner may. Two owners
    here so the last-owner guard (409) never enters the picture."""
    team = make_workspace("Climate Co")
    owner_a = make_user("ownerA@example.com")
    owner_b = make_user("ownerB@example.com")
    admin = make_user("admin10@example.com")
    await seed(
        team, owner_a, owner_b, admin,
        make_member(owner_a, team, role="owner"),
        make_member(owner_b, team, role="owner"),
        make_member(admin, team, role="admin"),
    )
    await login(client, admin.email)

    response = await client.patch(
        f"/api/workspaces/{team.id}/members/{owner_b.id}", json={"role": "analyst"}
    )
    assert response.status_code == 403

    await client.post("/api/auth/logout")
    await login(client, owner_a.email)
    response = await client.patch(
        f"/api/workspaces/{team.id}/members/{owner_b.id}", json={"role": "analyst"}
    )
    assert response.status_code == 200
    assert response.json()["role"] == "analyst"


async def test_only_an_owner_can_remove_another_owner(client, seed):
    team = make_workspace("Climate Co")
    owner_a = make_user("ownerC@example.com")
    owner_b = make_user("ownerD@example.com")
    admin = make_user("admin11@example.com")
    await seed(
        team, owner_a, owner_b, admin,
        make_member(owner_a, team, role="owner"),
        make_member(owner_b, team, role="owner"),
        make_member(admin, team, role="admin"),
    )
    await login(client, admin.email)

    response = await client.delete(f"/api/workspaces/{team.id}/members/{owner_b.id}")
    assert response.status_code == 403

    await client.post("/api/auth/logout")
    await login(client, owner_a.email)
    response = await client.delete(f"/api/workspaces/{team.id}/members/{owner_b.id}")
    assert response.status_code == 200


# ── invite lifecycle ─────────────────────────────────────────────────────────
async def test_invite_create_list_revoke(client, seed):
    team = make_workspace("Climate Co")
    owner = make_user("owner10@example.com")
    await seed(team, owner, make_member(owner, team, role="owner"))
    await login(client, owner.email)

    create = await client.post(
        f"/api/workspaces/{team.id}/invites", json={"email": "New@Example.com", "role": "approver"}
    )
    assert create.status_code == 200, create.text
    body = create.json()
    assert body["email"] == "new@example.com"
    assert body["role"] == "approver"
    assert body["status"] == "pending"
    assert body["invite_url"] == f"/invite/{body['token']}"
    assert body["email_sent"] is False  # TRET_EMAIL_MODE=off in this test env

    listed = await client.get(f"/api/workspaces/{team.id}/invites")
    assert listed.status_code == 200
    assert [i["email"] for i in listed.json()] == ["new@example.com"]
    assert "token" not in listed.json()[0]  # listing never leaks the token

    revoke = await client.delete(f"/api/workspaces/{team.id}/invites/{body['id']}")
    assert revoke.status_code == 200
    listed_again = await client.get(f"/api/workspaces/{team.id}/invites")
    assert listed_again.json()[0]["status"] == "revoked"


async def test_invite_role_cannot_be_owner(client, seed):
    team = make_workspace("Climate Co")
    owner = make_user("owner11@example.com")
    await seed(team, owner, make_member(owner, team, role="owner"))
    await login(client, owner.email)

    response = await client.post(
        f"/api/workspaces/{team.id}/invites", json={"email": "x@example.com", "role": "owner"}
    )
    assert response.status_code == 422


async def test_only_admin_or_above_can_invite(client, seed):
    team = make_workspace("Climate Co")
    owner = make_user("owner12@example.com")
    analyst = make_user("analyst12@example.com")
    await seed(
        team, owner, analyst,
        make_member(owner, team, role="owner"),
        make_member(analyst, team, role="analyst"),
    )
    await login(client, analyst.email)

    response = await client.post(
        f"/api/workspaces/{team.id}/invites", json={"email": "x@example.com"}
    )
    assert response.status_code == 403


async def test_accepting_an_invite_creates_membership_and_switches_workspace(client, seed):
    team = make_workspace("Climate Co")
    owner = make_user("owner13@example.com")
    invitee = make_user("invitee13@example.com")
    home = make_workspace("Invitee's home")
    invite = make_invite(team, email=invitee.email, role="approver")
    await seed(
        team, owner, invitee, home,
        make_member(owner, team, role="owner"),
        make_member(invitee, home, role="owner"),
        invite,
    )
    await login(client, invitee.email)

    response = await client.post(f"/api/auth/invites/{invite.token}/accept")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["current_workspace_id"] == str(team.id)
    assert body["role"] == "approver"
    assert {w["id"] for w in body["workspaces"]} == {str(team.id), str(home.id)}

    # Double-accept: the invite is no longer pending.
    again = await client.post(f"/api/auth/invites/{invite.token}/accept")
    assert again.status_code == 404


async def test_accepting_an_invite_sent_to_a_different_email_is_refused(client, seed):
    team = make_workspace("Climate Co")
    owner = make_user("owner14@example.com")
    invitee = make_user("invitee14@example.com")
    invite = make_invite(team, email="someone-else@example.com")
    await seed(team, owner, invitee, make_member(owner, team, role="owner"), invite)
    await login(client, invitee.email)

    response = await client.post(f"/api/auth/invites/{invite.token}/accept")
    assert response.status_code == 403


async def test_an_expired_invite_cannot_be_accepted(client, seed):
    team = make_workspace("Climate Co")
    owner = make_user("owner15@example.com")
    invitee = make_user("invitee15@example.com")
    invite = make_invite(team, email=invitee.email, hours=-1)
    await seed(team, owner, invitee, make_member(owner, team, role="owner"), invite)
    await login(client, invitee.email)

    response = await client.post(f"/api/auth/invites/{invite.token}/accept")
    assert response.status_code == 404


async def test_accept_is_blocked_by_a_registered_workspace_gate(client, seed):
    """The seat-gate checkpoint OIDC-login redemption already has
    (services/identity.py) applies here too: a logged-in user visiting an
    issued invite link must not be able to join a seat-limited team a gate
    would refuse. Blocked -> 403 naming the reason, invite stays pending, no
    membership is created."""
    team = make_workspace("Climate Co")
    owner = make_user("owner16b@example.com")
    invitee = make_user("invitee16b@example.com")
    home = make_workspace("Invitee's home")
    invite = make_invite(team, email=invitee.email, role="approver")
    await seed(
        team, owner, invitee, home,
        make_member(owner, team, role="owner"),
        make_member(invitee, home, role="owner"),
        invite,
    )
    await login(client, invitee.email)

    ext = ExtensionAPI(None)

    async def veto(db, workspace_id, action):
        assert action == "invite_redeem"
        return GateResult(allowed=False, reason="seat_limit", detail="no seats left")

    ext.add_workspace_gate(veto)
    extensions_module._registry = ext

    response = await client.post(f"/api/auth/invites/{invite.token}/accept")
    assert response.status_code == 403
    assert response.json()["detail"]["reason"] == "seat_limit"

    # The invite was not consumed: it is still pending and can still be
    # accepted once a seat frees up (or the gate is removed).
    extensions_module._registry = None
    again = await client.post(f"/api/auth/invites/{invite.token}/accept")
    assert again.status_code == 200, again.text
    assert again.json()["role"] == "approver"


async def test_an_unknown_token_is_a_404(client, seed):
    user = make_user("solo16@example.com")
    home = make_workspace("Solo")
    await seed(user, home, make_member(user, home, role="owner"))
    await login(client, user.email)

    response = await client.post("/api/auth/invites/not-a-real-token/accept")
    assert response.status_code == 404


async def test_invite_creation_is_blocked_by_a_registered_workspace_gate(client, seed):
    """Wiring, not the gate's own logic (a hosting extension's suite covers that): a
    blocked `check_workspace_gate` result becomes a 403 naming the reason."""
    team = make_workspace("Climate Co")
    owner = make_user("owner17@example.com")
    await seed(team, owner, make_member(owner, team, role="owner"))
    await login(client, owner.email)

    ext = ExtensionAPI(None)

    async def veto(db, workspace_id, action):
        assert action == "invite"
        return GateResult(allowed=False, reason="seat_limit", detail="no seats left")

    ext.add_workspace_gate(veto)
    extensions_module._registry = ext

    response = await client.post(
        f"/api/workspaces/{team.id}/invites", json={"email": "x@example.com"}
    )
    assert response.status_code == 403
    assert response.json()["detail"]["reason"] == "seat_limit"


# ── seat-gate race: lock -> gate -> write is one shared helper ─────────────
#
# `check_workspace_gate` counts seats against its own fresh session, isolated
# from the caller's transaction — two concurrent redemptions at
# seats-minus-one could otherwise both see "one seat free" and both be
# allowed. The fix (services/workspace.py::lock_workspace_for_seat_gate /
# redeem_workspace_seat) needs two live transactions to actually race, which
# sqlite/httpx-in-one-process cannot reproduce — same limitation
# `test_owner_count_query_requests_row_locking` above already accepts for the
# sibling TOCTOU on owner counts. What these tests check instead: the lock is
# actually requested (`with_for_update`), and both invite-redemption call
# sites — `api/workspaces.py::accept_invite` and
# `services/identity.py::_redeem_invites_and_resolve_workspace` — go through
# the exact same helper rather than each rolling their own version of it.


async def test_lock_workspace_for_seat_gate_requests_row_locking():
    """`with_for_update`, same defense `test_owner_count_query_requests_row_
    locking` above gives `_owner_count`'s query: without it, two concurrent
    seat redemptions for one workspace could both read the same "seats
    remaining" snapshot from the gate's own fresh session and both proceed."""
    from tret.services import workspace as workspace_service

    captured = {}

    class RecordingSession:
        async def execute(self, stmt):
            captured["stmt"] = stmt

            class _Result:
                def scalars(self):
                    return self

                def all(self):
                    return []

            return _Result()

    await workspace_service.lock_workspace_for_seat_gate(RecordingSession(), uuid.uuid4())
    assert captured["stmt"]._for_update_arg is not None


async def test_accept_invite_uses_the_shared_seat_redemption_helper(client, seed, monkeypatch):
    """API-side invite redemption (accept_invite) must go through
    `redeem_workspace_seat` — not a copy of its lock/gate/insert sequence —
    so the two redemption paths cannot drift apart. The real implementation
    still runs underneath (this only records the call), so this also proves
    behaviour is unchanged on sqlite: the accept still succeeds normally."""
    team = make_workspace("Climate Co")
    owner = make_user("owner-helper@example.com")
    invitee = make_user("invitee-helper@example.com")
    invite = make_invite(team, email=invitee.email, role="approver")
    await seed(team, owner, invitee, make_member(owner, team, role="owner"), invite)
    await login(client, invitee.email)

    calls = []
    original = workspaces_api.redeem_workspace_seat

    async def recording(db, workspace_id, user_id, role):
        calls.append((workspace_id, user_id, role))
        return await original(db, workspace_id, user_id, role)

    monkeypatch.setattr(workspaces_api, "redeem_workspace_seat", recording)

    response = await client.post(f"/api/auth/invites/{invite.token}/accept")
    assert response.status_code == 200, response.text
    assert calls == [(team.id, invitee.id, "approver")]


async def test_create_invite_locks_the_workspace_before_the_gate(client, seed, monkeypatch):
    """Invite *creation* (create_invite) takes the same row lock ahead of
    its own "invite" gate, for the identical reason — two concurrent invite
    creations at seats-minus-one must not both pass."""
    team = make_workspace("Climate Co")
    owner = make_user("owner-lock@example.com")
    await seed(team, owner, make_member(owner, team, role="owner"))
    await login(client, owner.email)

    calls = []
    original = workspaces_api.lock_workspace_for_seat_gate

    async def recording(db, workspace_id):
        calls.append(workspace_id)
        return await original(db, workspace_id)

    monkeypatch.setattr(workspaces_api, "lock_workspace_for_seat_gate", recording)

    response = await client.post(
        f"/api/workspaces/{team.id}/invites", json={"email": "x@example.com"}
    )
    assert response.status_code == 200, response.text
    assert calls == [team.id]


async def test_oidc_login_redemption_uses_the_shared_seat_redemption_helper(session_factory, seed, monkeypatch):
    """The OIDC-login redemption path (services/identity.py::_redeem_invites_
    and_resolve_workspace) must go through the exact same
    `redeem_workspace_seat` helper as the API path above — not its own
    hand-rolled lock/gate/insert — so the two cannot drift apart. Driven
    directly against the real function (rather than through a full OIDC
    round trip) since this file has no OIDC router mounted; the real
    implementation still runs underneath, so a normal (unblocked) redemption
    on sqlite is still proven to work exactly as before."""
    from tret.services import identity as identity_module

    team = make_workspace("Climate Co")
    invitee = make_user("invitee-oidc-helper@example.com")
    invite = make_invite(team, email=invitee.email, role="approver")
    await seed(team, invitee, invite)

    calls = []
    original = identity_module.redeem_workspace_seat

    async def recording(db, workspace_id, user_id, role):
        calls.append((workspace_id, user_id, role))
        return await original(db, workspace_id, user_id, role)

    monkeypatch.setattr(identity_module, "redeem_workspace_seat", recording)

    async with session_factory() as db:
        user = await db.get(User, invitee.id)
        resolved = await identity_module._redeem_invites_and_resolve_workspace(
            db, user, invitee.email.strip().lower(), True, fallback_workspace_id=None
        )

    assert calls == [(team.id, invitee.id, "approver")]
    assert resolved == team.id


async def test_oidc_login_redemption_locks_workspaces_in_workspace_id_order(
    session_factory, seed, monkeypatch
):
    """Each invite redeemed here takes a `SELECT ... FOR UPDATE` on its
    Workspace (via `redeem_workspace_seat`), all inside one transaction. Two
    concurrent OIDC logins each redeeming invites to the same two workspaces
    in opposite orders could lock them in opposite orders too and deadlock
    on Postgres — the pending-invite query is ordered by `Invite.workspace_id`
    so every caller locks in the same sequence, not creation order.

    The invite to the higher-id workspace is seeded first on purpose: if
    redemption followed insertion order instead of `workspace_id`, it would
    be redeemed (and land the session) first too."""
    from tret.services import identity as identity_module

    ws_low, ws_high = make_workspace("Low"), make_workspace("High")
    if ws_low.id > ws_high.id:
        ws_low, ws_high = ws_high, ws_low
    assert ws_low.id < ws_high.id

    invitee = make_user("invitee-order@example.com")
    invite_high = make_invite(ws_high, email=invitee.email, role="approver")
    invite_low = make_invite(ws_low, email=invitee.email, role="approver")
    await seed(ws_low, ws_high, invitee, invite_high, invite_low)

    locked_order = []
    original = identity_module.redeem_workspace_seat

    async def recording(db, workspace_id, user_id, role):
        locked_order.append(workspace_id)
        return await original(db, workspace_id, user_id, role)

    monkeypatch.setattr(identity_module, "redeem_workspace_seat", recording)

    async with session_factory() as db:
        user = await db.get(User, invitee.id)
        resolved = await identity_module._redeem_invites_and_resolve_workspace(
            db, user, invitee.email.strip().lower(), True, fallback_workspace_id=None
        )

    assert locked_order == [ws_low.id, ws_high.id]
    # Lands on the last-redeemed workspace, i.e. the higher id, given this order.
    assert resolved == ws_high.id


# ── personal workspaces: permanently single-member ──────────────────────────
async def test_a_personal_workspace_can_never_be_invited_to(client, seed):
    user = make_user("me18@example.com")
    personal = make_workspace("me18's workspace", kind="personal", personal_owner_id=user.id)
    await seed(user, personal, make_member(user, personal, role="owner"))
    await login(client, user.email)

    response = await client.post(
        f"/api/workspaces/{personal.id}/invites", json={"email": "x@example.com"}
    )
    assert response.status_code == 403


async def test_the_personal_owner_can_never_leave_or_be_removed(client, seed):
    user = make_user("me19@example.com")
    personal = make_workspace("me19's workspace", kind="personal", personal_owner_id=user.id)
    await seed(user, personal, make_member(user, personal, role="owner"))
    await login(client, user.email)

    response = await client.delete(f"/api/workspaces/{personal.id}/members/{user.id}")
    assert response.status_code == 403


async def test_a_personal_workspaces_role_can_never_be_changed(client, seed):
    user = make_user("me20@example.com")
    personal = make_workspace("me20's workspace", kind="personal", personal_owner_id=user.id)
    await seed(user, personal, make_member(user, personal, role="owner"))
    await login(client, user.email)

    response = await client.patch(
        f"/api/workspaces/{personal.id}/members/{user.id}", json={"role": "admin"}
    )
    assert response.status_code == 403
