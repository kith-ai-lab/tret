"""Harness policy history: PUT records who/when/previous value, GET lists it,
an admin can revert. Real sqlite database + ASGI client, same harness as
test_harnesses_api.py."""
from __future__ import annotations

import uuid
from datetime import timedelta

import httpx
import pytest
import pytest_asyncio
from argon2 import PasswordHasher
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from tests.evals.golden_world import install_sqlite_type_shims
from tret.api import auth
from tret.api import harnesses as harnesses_api
from tret.db.engine import get_db
from tret.db.models import (
    Base,
    Harness,
    HarnessPolicyHistory,
    User,
    Workspace,
    WorkspaceMember,
    utcnow,
)
from tret.services import harness_policy
from tret.services.workspace import (
    COLLECTION_HARNESS_MODEL_POLICY,
    COLLECTION_HARNESS_NAME,
    _PREVIOUS_COLLECTION_MODEL_POLICIES,
    ensure_collection_harness,
)

HASHER = PasswordHasher()
PASSWORD = "correct-horse-battery-1"

POLICY_A = {"mode": "auto", "max_cost_tier": "standard"}
POLICY_B = {"mode": "auto", "max_cost_tier": "economy", "objective": "eco"}
LOOP_A = {"max_iterations": 24, "max_output_tokens": 8192, "temperature": 0.2}
LOOP_B = {"max_iterations": 10, "max_output_tokens": 4096, "temperature": 0.1}


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
    async def _seed(*rows):
        async with session_factory() as db:
            db.add_all(rows)
            await db.commit()

    return _seed


@pytest_asyncio.fixture
async def client(session_factory):
    app = FastAPI()
    app.include_router(auth.router)
    app.include_router(harnesses_api.router)

    async def _get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _get_db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def make_user(email: str) -> User:
    return User(
        id=uuid.uuid4(),
        email=email,
        display_name=email.split("@")[0].title(),
        password_hash=HASHER.hash(PASSWORD),
        role="analyst",
    )


def make_workspace(name: str) -> Workspace:
    return Workspace(id=uuid.uuid4(), name=name, kind="team")


def make_harness(ws: Workspace, *, name: str = "H", policy=None, loop=None) -> Harness:
    return Harness(
        id=uuid.uuid4(),
        workspace_id=ws.id,
        name=name,
        task_profile="freeform",
        model_policy=dict(policy or POLICY_A),
        tool_names=[],
        loop_config=dict(loop or LOOP_A),
    )


async def login(client: httpx.AsyncClient, email: str) -> None:
    response = await client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text


@pytest_asyncio.fixture
async def world(seed):
    team = make_workspace("Co")
    admin = make_user("admin@example.com")
    approver = make_user("approver@example.com")
    harness = make_harness(team)
    await seed(
        team,
        admin,
        approver,
        WorkspaceMember(user_id=admin.id, workspace_id=team.id, role="admin"),
        WorkspaceMember(user_id=approver.id, workspace_id=team.id, role="approver"),
        harness,
    )
    return {"team": team, "admin": admin, "approver": approver, "harness": harness}


def put_body(harness: Harness, *, policy, loop, name: str | None = None) -> dict:
    return {
        "name": name or harness.name,
        "task_profile": "freeform",
        "model_policy": policy,
        "tool_names": [],
        "loop_config": loop,
    }


async def put(client, harness, *, policy, loop, name=None):
    return await client.put(
        f"/api/harnesses/{harness.id}", json=put_body(harness, policy=policy, loop=loop, name=name)
    )


async def history(client, harness) -> list[dict]:
    response = await client.get(f"/api/harnesses/{harness.id}/policy-history")
    assert response.status_code == 200, response.text
    return response.json()["history"]


async def row_count(session_factory) -> int:
    async with session_factory() as db:
        return (await db.execute(select(func.count()).select_from(HarnessPolicyHistory))).scalar_one()


# ── recording on PUT ─────────────────────────────────────────────────────────


async def test_put_records_a_row_only_when_policy_or_loop_changes(client, world, session_factory):
    h, admin = world["harness"], world["admin"]
    await login(client, admin.email)

    # Neither changed (only the name): no row.
    assert (await put(client, h, policy=POLICY_A, loop=LOOP_A, name="Renamed")).status_code == 200
    assert await row_count(session_factory) == 0

    assert (await put(client, h, policy=POLICY_B, loop=LOOP_A, name="Renamed")).status_code == 200
    assert await row_count(session_factory) == 1
    # Same body again: still one row.
    assert (await put(client, h, policy=POLICY_B, loop=LOOP_A, name="Renamed")).status_code == 200
    assert await row_count(session_factory) == 1

    # loop_config alone also counts.
    assert (await put(client, h, policy=POLICY_B, loop=LOOP_B, name="Renamed")).status_code == 200
    assert await row_count(session_factory) == 2


async def test_row_contents(client, world, session_factory):
    h, admin = world["harness"], world["admin"]
    await login(client, admin.email)
    before = utcnow()
    response = await put(client, h, policy=POLICY_B, loop=LOOP_B)
    assert response.status_code == 200
    # The PUT response shape is unchanged.
    assert response.json()["model_policy"] == POLICY_B

    rows = await history(client, h)
    assert len(rows) == 1
    row = rows[0]
    assert row["source"] == "edit" and row["source_id"] is None and row["note"] is None
    assert row["harness_id"] == str(h.id)
    assert row["changed_by"] == str(admin.id)
    assert row["changed_by_name"] == "Admin"
    assert row["previous_model_policy"] == POLICY_A and row["new_model_policy"] == POLICY_B
    assert row["previous_loop_config"] == LOOP_A and row["new_loop_config"] == LOOP_B
    async with session_factory() as db:
        stored = await db.get(HarnessPolicyHistory, uuid.UUID(row["id"]))
        assert stored.workspace_id == world["team"].id
        changed_at = stored.changed_at
    if changed_at.tzinfo is None:  # sqlite drops the zone
        changed_at = changed_at.replace(tzinfo=before.tzinfo)
    assert before - timedelta(seconds=1) <= changed_at <= utcnow() + timedelta(seconds=1)


async def test_previous_and_new_values_are_independent_copies(session_factory, world):
    h = world["harness"]
    async with session_factory() as db:
        live = await db.get(Harness, h.id)
        previous = {"mode": "auto", "allowed": ["x"]}
        live.model_policy = {"mode": "auto", "allowed": ["y"]}
        row = await harness_policy.record_policy_change(
            db,
            live,
            previous_model_policy=previous,
            previous_loop_config=dict(LOOP_A),
            user_id=None,
            source="edit",
        )
        previous["allowed"].append("mutated")
        live.model_policy["allowed"].append("mutated")
        assert row.previous_model_policy == {"mode": "auto", "allowed": ["x"]}
        assert row.new_model_policy == {"mode": "auto", "allowed": ["y"]}
        assert row.changed_by is None


async def test_record_policy_change_rejects_unknown_source(session_factory, world):
    async with session_factory() as db:
        live = await db.get(Harness, world["harness"].id)
        with pytest.raises(ValueError):
            await harness_policy.record_policy_change(
                db,
                live,
                previous_model_policy={},
                previous_loop_config={},
                user_id=None,
                source="magic",
            )


def test_policy_changed_is_plain_dict_equality():
    h = Harness(model_policy={"mode": "auto"}, loop_config={"max_iterations": 3})
    assert not harness_policy.policy_changed({"mode": "auto"}, {"max_iterations": 3}, h)
    assert harness_policy.policy_changed({"mode": "pinned"}, {"max_iterations": 3}, h)
    assert harness_policy.policy_changed({"mode": "auto"}, {"max_iterations": 4}, h)


# ── listing ──────────────────────────────────────────────────────────────────


async def test_list_is_newest_first_with_names(client, world, session_factory):
    h, admin = world["harness"], world["admin"]
    await login(client, admin.email)
    await put(client, h, policy=POLICY_B, loop=LOOP_A)
    await put(client, h, policy=POLICY_A, loop=LOOP_A)
    # A system row with no user.
    async with session_factory() as db:
        live = await db.get(Harness, h.id)
        await harness_policy.record_policy_change(
            db,
            live,
            previous_model_policy=POLICY_A,
            previous_loop_config=LOOP_A,
            user_id=None,
            source="experiment",
            source_id=uuid.uuid4(),
            note="winner",
        )
        await db.commit()

    rows = await history(client, h)
    assert [r["source"] for r in rows] == ["experiment", "edit", "edit"]
    assert rows[0]["changed_by"] is None and rows[0]["changed_by_name"] is None
    assert rows[0]["note"] == "winner" and rows[0]["source_id"]
    assert rows[1]["new_model_policy"] == POLICY_A and rows[2]["new_model_policy"] == POLICY_B
    assert rows[1]["changed_by_name"] == "Admin"


async def test_any_member_can_list(client, world):
    await login(client, world["approver"].email)
    assert await history(client, world["harness"]) == []


# ── revert ───────────────────────────────────────────────────────────────────


async def test_revert_restores_and_a_second_revert_goes_forward(client, world):
    h, admin = world["harness"], world["admin"]
    await login(client, admin.email)
    await put(client, h, policy=POLICY_B, loop=LOOP_B)
    edit_row = (await history(client, h))[0]

    response = await client.post(f"/api/harnesses/{h.id}/policy-history/{edit_row['id']}/revert")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == str(h.id)
    assert body["model_policy"] == POLICY_A and body["loop_config"] == LOOP_A

    rows = await history(client, h)
    assert len(rows) == 2
    revert_row = rows[0]
    assert revert_row["source"] == "revert" and revert_row["source_id"] == edit_row["id"]
    assert revert_row["changed_by"] == str(admin.id)
    assert revert_row["previous_model_policy"] == POLICY_B
    assert revert_row["new_model_policy"] == POLICY_A

    # Reverting the revert row goes forward again.
    response = await client.post(f"/api/harnesses/{h.id}/policy-history/{revert_row['id']}/revert")
    assert response.status_code == 200, response.text
    assert response.json()["model_policy"] == POLICY_B
    assert response.json()["loop_config"] == LOOP_B
    assert (await history(client, h))[0]["source_id"] == revert_row["id"]


async def test_revert_requires_admin(client, world):
    h = world["harness"]
    await login(client, world["admin"].email)
    await put(client, h, policy=POLICY_B, loop=LOOP_A)
    row = (await history(client, h))[0]
    await client.post("/api/auth/logout")
    await login(client, world["approver"].email)
    response = await client.post(f"/api/harnesses/{h.id}/policy-history/{row['id']}/revert")
    assert response.status_code == 403
    assert (await client.get(f"/api/harnesses/{h.id}")).json()["model_policy"] == POLICY_B


async def test_unknown_or_foreign_history_id_is_404(client, world, seed):
    team, h = world["team"], world["harness"]
    other = make_harness(team, name="Other")
    await seed(other)
    await login(client, world["admin"].email)
    await put(client, other, policy=POLICY_B, loop=LOOP_A)
    foreign = (await history(client, other))[0]

    for history_id in (uuid.uuid4(), foreign["id"]):
        response = await client.post(f"/api/harnesses/{h.id}/policy-history/{history_id}/revert")
        assert response.status_code == 404
    assert (await client.get(f"/api/harnesses/{other.id}")).json()["model_policy"] == POLICY_B


async def test_cross_workspace_is_404(client, world, seed, session_factory):
    other_ws = make_workspace("Elsewhere")
    foreign_harness = make_harness(other_ws, name="Foreign")
    await seed(other_ws, foreign_harness)
    async with session_factory() as db:
        row = await harness_policy.record_policy_change(
            db,
            await db.get(Harness, foreign_harness.id),
            previous_model_policy=POLICY_B,
            previous_loop_config=LOOP_B,
            user_id=None,
            source="edit",
        )
        await db.commit()
        row_id = row.id
    await login(client, world["admin"].email)
    assert (
        await client.get(f"/api/harnesses/{foreign_harness.id}/policy-history")
    ).status_code == 404
    response = await client.post(
        f"/api/harnesses/{foreign_harness.id}/policy-history/{row_id}/revert"
    )
    assert response.status_code == 404
    assert (await put(client, foreign_harness, policy=POLICY_B, loop=LOOP_B)).status_code == 404


async def test_invalid_previous_policy_is_422_and_changes_nothing(client, world, session_factory):
    h = world["harness"]
    async with session_factory() as db:
        db.add(
            HarnessPolicyHistory(
                workspace_id=world["team"].id,
                harness_id=h.id,
                changed_by=None,
                changed_at=utcnow(),
                source="edit",
                previous_model_policy={"mode": "pinned", "model": "gone/no-such-model"},
                previous_loop_config=dict(LOOP_B),
                new_model_policy=dict(POLICY_A),
                new_loop_config=dict(LOOP_A),
            )
        )
        await db.commit()
    await login(client, world["admin"].email)
    row = (await history(client, h))[0]
    response = await client.post(f"/api/harnesses/{h.id}/policy-history/{row['id']}/revert")
    assert response.status_code == 422
    assert "gone/no-such-model" in response.json()["detail"]
    current = (await client.get(f"/api/harnesses/{h.id}")).json()
    assert current["model_policy"] == POLICY_A and current["loop_config"] == LOOP_A
    assert len(await history(client, h)) == 1


# ── seeded-harness boot upgrade ──────────────────────────────────────────────


async def test_boot_upgrade_does_not_overwrite_a_policy_changed_through_put(
    client, session_factory, seed
):
    team = make_workspace("Seeded")
    admin = make_user("seedadmin@example.com")
    stale = _PREVIOUS_COLLECTION_MODEL_POLICIES[0]
    h = make_harness(team, name=COLLECTION_HARNESS_NAME, policy=stale)
    await seed(team, admin, WorkspaceMember(user_id=admin.id, workspace_id=team.id, role="admin"), h)
    await login(client, admin.email)
    assert (await put(client, h, policy=POLICY_B, loop=LOOP_A)).status_code == 200

    async with session_factory() as db:
        await ensure_collection_harness(db, team.id)
        await db.commit()
        live = await db.get(Harness, h.id)
        assert live.model_policy == POLICY_B
        assert live.model_policy != COLLECTION_HARNESS_MODEL_POLICY


async def test_revert_that_changes_nothing_is_409_and_writes_no_row(client, world, session_factory):
    h = world["harness"]
    await login(client, world["admin"].email)
    await put(client, h, policy=POLICY_B, loop=LOOP_B)
    row = (await history(client, h))[0]
    # Put the harness back by hand, so the entry's previous values are already live.
    await put(client, h, policy=POLICY_A, loop=LOOP_A)
    count = await row_count(session_factory)
    response = await client.post(f"/api/harnesses/{h.id}/policy-history/{row['id']}/revert")
    assert response.status_code == 409
    assert "already has these settings" in response.json()["detail"]
    assert await row_count(session_factory) == count


async def test_history_chain_is_consistent_across_changes(client, world):
    h = world["harness"]
    await login(client, world["admin"].email)
    await put(client, h, policy=POLICY_B, loop=LOOP_A)
    await put(client, h, policy=POLICY_B, loop=LOOP_B)
    first = (await history(client, h))[-1]
    assert (await client.post(f"/api/harnesses/{h.id}/policy-history/{first['id']}/revert")).status_code == 200
    await put(client, h, policy=POLICY_A, loop=LOOP_B)

    rows = list(reversed(await history(client, h)))  # oldest first
    assert len(rows) == 4
    assert rows[0]["previous_model_policy"] == POLICY_A
    for older, newer in zip(rows, rows[1:]):
        assert newer["previous_model_policy"] == older["new_model_policy"]
        assert newer["previous_loop_config"] == older["new_loop_config"]
    live = (await client.get(f"/api/harnesses/{h.id}")).json()
    assert rows[-1]["new_model_policy"] == live["model_policy"]
    assert rows[-1]["new_loop_config"] == live["loop_config"]
