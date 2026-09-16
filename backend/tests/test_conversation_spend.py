"""runs.conversation_id — set on a chat turn, inherited by delegation, and
rolled up by `GET /api/analytics/spend/conversations` (dollar spend grouped
by conversation, aggregated in SQL — see api/analytics.py's spend-by-
conversation section).

Real sqlite database, `httpx.AsyncClient` + `ASGITransport` directly against
the app — same harness as test_chat_api.py/test_runs_api.py.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest_asyncio
from argon2 import PasswordHasher
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from tests.evals.golden_world import install_sqlite_type_shims
from tret.api import analytics as analytics_api
from tret.api import auth
from tret.api import chat as chat_api
from tret.db.engine import get_db
from tret.db.models import (
    Base,
    Conversation,
    Harness,
    Project,
    Run,
    User,
    Workspace,
    WorkspaceMember,
)

HASHER = PasswordHasher()
PASSWORD = "correct-horse-battery-1"


class _NoopEngine:
    async def execute(self, run_id):  # pragma: no cover - never actually asserted on
        return None


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
async def client(session_factory, monkeypatch):
    # The engine is never actually invoked in the rollup tests, and the one
    # chat-turn test here only asserts on the Run row `send_message` creates
    # before the background task starts — same posture as test_chat_api.py.
    monkeypatch.setattr(chat_api, "get_harness_engine", lambda: _NoopEngine())

    app = FastAPI()
    app.include_router(auth.router)
    app.include_router(chat_api.router)
    app.include_router(analytics_api.router)

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


def make_member(user: User, workspace: Workspace, *, role: str) -> WorkspaceMember:
    return WorkspaceMember(user_id=user.id, workspace_id=workspace.id, role=role)


def make_harness(workspace: Workspace) -> Harness:
    return Harness(
        workspace_id=workspace.id,
        name="Chat",
        task_profile="chat",
        model_policy={"mode": "auto"},
        tool_names=[],
    )


async def login(client: httpx.AsyncClient, email: str) -> None:
    response = await client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text


def make_run(
    *,
    project_id,
    harness_id,
    conversation_id=None,
    cost_usd: str = "0",
    reported: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    created_at=None,
) -> Run:
    return Run(
        id=uuid.uuid4(),
        project_id=project_id,
        harness_id=harness_id,
        conversation_id=conversation_id,
        task_type="chat",
        task_input={},
        cost_usd=Decimal(cost_usd),
        reported_cost_usd=Decimal(reported) if reported is not None else None,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        created_at=created_at or datetime.now(timezone.utc),
    )


# ── a chat turn's Run gets conversation_id set ───────────────────────────────


async def test_a_chat_turns_run_is_stamped_with_its_conversation_id(client, seed, session_factory):
    team = make_workspace("Co")
    project = Project(id=uuid.uuid4(), workspace_id=team.id, name="P")
    user = make_user("analyst@example.com")
    harness = make_harness(team)
    await seed(team, project, user, make_member(user, team, role="analyst"), harness)

    await login(client, user.email)
    created = await client.post("/api/chat", json={"harness_id": str(harness.id)})
    assert created.status_code == 200, created.text
    conversation_id = created.json()["id"]

    sent = await client.post(f"/api/chat/{conversation_id}/messages", json={"text": "Hello"})
    assert sent.status_code == 200, sent.text
    run_id = uuid.UUID(sent.json()["run_id"])

    async with session_factory() as db:
        run = await db.get(Run, run_id)
    assert str(run.conversation_id) == conversation_id


# ── GET /api/analytics/spend/conversations ───────────────────────────────────


async def test_rollup_groups_by_conversation_with_correct_sums(client, seed, session_factory):
    team = make_workspace("Co")
    project = Project(id=uuid.uuid4(), workspace_id=team.id, name="P")
    user = make_user("analyst@example.com")
    harness = make_harness(team)
    await seed(team, project, user, make_member(user, team, role="analyst"), harness)

    conv_a = Conversation(id=uuid.uuid4(), project_id=project.id, harness_id=harness.id, title="A")
    conv_b = Conversation(id=uuid.uuid4(), project_id=project.id, harness_id=harness.id, title="B")
    await seed(conv_a, conv_b)

    async with session_factory() as db:
        db.add_all(
            [
                make_run(project_id=project.id, harness_id=harness.id, conversation_id=conv_a.id,
                         cost_usd="0.10", reported="0.09", input_tokens=100, output_tokens=50),
                make_run(project_id=project.id, harness_id=harness.id, conversation_id=conv_a.id,
                         cost_usd="0.20", reported="0.18", input_tokens=200, output_tokens=80),
                make_run(project_id=project.id, harness_id=harness.id, conversation_id=conv_b.id,
                         cost_usd="0.50", reported="0.45", input_tokens=10, output_tokens=5),
                # No conversation — a workbench run alongside the chat spend.
                make_run(project_id=project.id, harness_id=harness.id, conversation_id=None,
                         cost_usd="0.05", reported=None, input_tokens=1, output_tokens=1),
            ]
        )
        await db.commit()

    await login(client, user.email)
    response = await client.get("/api/analytics/spend/conversations")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["period_days"] == 30
    by_id = {c["conversation_id"]: c for c in body["conversations"]}

    a = by_id[str(conv_a.id)]
    assert a["run_count"] == 2
    assert a["input_tokens"] == 300
    assert a["output_tokens"] == 130
    assert a["cost_usd"] == 0.30
    assert a["reported_cost_usd"] == 0.27
    assert a["title"] == "A"
    assert a["first_run_at"] and a["last_run_at"]

    b = by_id[str(conv_b.id)]
    assert b["run_count"] == 1
    assert b["reported_cost_usd"] == 0.45

    null_row = by_id[None]
    assert null_row["run_count"] == 1
    assert null_row["title"] is None
    # The null run's own reported_cost_usd is None ("no bill posted yet"),
    # and a group where every run is like that has accrued nothing *yet*
    # either — which is unknown-so-far, not a confirmed zero, so the group
    # SUM (also None) is reported as None rather than 0.0.
    assert null_row["reported_cost_usd"] is None
    assert null_row["cost_usd"] == 0.05

    # Ordered by reported_cost_usd descending, null (unknown-so-far) last:
    # B (0.45) > A (0.27) > null.
    assert [c["conversation_id"] for c in body["conversations"]] == [
        str(conv_b.id),
        str(conv_a.id),
        None,
    ]

    # totals sum every group in the window, independent of `limit` and of
    # which rows the response happened to return.
    assert body["totals"] == {
        "conversation_count": 2,
        "run_count": 4,
        "input_tokens": 311,
        "output_tokens": 136,
        "cost_usd": 0.85,
        "reported_cost_usd": 0.72,
    }


async def test_rollup_is_scoped_to_the_callers_workspace(client, seed, session_factory):
    """The one that matters most: another workspace's runs — and its
    conversation's title — must never appear, not folded into a total, not
    named, not even counted."""
    team_a = make_workspace("A Co")
    project_a = Project(id=uuid.uuid4(), workspace_id=team_a.id, name="PA")
    user_a = make_user("a@example.com")
    harness_a = make_harness(team_a)

    team_b = make_workspace("B Co")
    project_b = Project(id=uuid.uuid4(), workspace_id=team_b.id, name="PB")
    user_b = make_user("b@example.com")
    harness_b = make_harness(team_b)

    await seed(
        team_a, project_a, user_a, make_member(user_a, team_a, role="analyst"), harness_a,
        team_b, project_b, user_b, make_member(user_b, team_b, role="analyst"), harness_b,
    )
    conv_b = Conversation(
        id=uuid.uuid4(),
        project_id=project_b.id,
        harness_id=harness_b.id,
        title="Other tenant's secret",
    )
    await seed(conv_b)

    async with session_factory() as db:
        db.add_all(
            [
                make_run(project_id=project_a.id, harness_id=harness_a.id,
                         cost_usd="0.01", reported="0.01"),
                make_run(project_id=project_b.id, harness_id=harness_b.id, conversation_id=conv_b.id,
                         cost_usd="999", reported="999"),
            ]
        )
        await db.commit()

    await login(client, user_a.email)
    response = await client.get("/api/analytics/spend/conversations")
    assert response.status_code == 200, response.text
    body = response.json()
    titles = {c["title"] for c in body["conversations"]}
    assert "Other tenant's secret" not in titles
    total_reported = sum(c["reported_cost_usd"] for c in body["conversations"])
    assert total_reported == 0.01  # not 999.01 — B's spend never leaked in


async def test_in_flight_conversation_reports_null_not_zero_cost(client, seed, session_factory):
    """A conversation whose only run has not billed yet (`reported_cost_usd`
    is None on the Run) must report None — "—" in the console — not 0.0,
    which would be indistinguishable from a conversation confirmed free."""
    team = make_workspace("Co")
    project = Project(id=uuid.uuid4(), workspace_id=team.id, name="P")
    user = make_user("analyst@example.com")
    harness = make_harness(team)
    await seed(team, project, user, make_member(user, team, role="analyst"), harness)

    conv = Conversation(id=uuid.uuid4(), project_id=project.id, harness_id=harness.id, title="A")
    await seed(conv)

    async with session_factory() as db:
        db.add(
            make_run(project_id=project.id, harness_id=harness.id, conversation_id=conv.id,
                     cost_usd="0.10", reported=None)
        )
        await db.commit()

    await login(client, user.email)
    response = await client.get("/api/analytics/spend/conversations")
    assert response.status_code == 200, response.text
    body = response.json()
    row = body["conversations"][0]
    assert row["conversation_id"] == str(conv.id)
    assert row["reported_cost_usd"] is None
    assert row["cost_usd"] == 0.10
    assert body["totals"]["reported_cost_usd"] is None


async def test_days_and_limit_are_bounded(client, seed):
    team = make_workspace("Co")
    project = Project(id=uuid.uuid4(), workspace_id=team.id, name="P")
    user = make_user("analyst@example.com")
    await seed(team, project, user, make_member(user, team, role="analyst"))

    await login(client, user.email)
    for params in ({"days": 0}, {"days": 3651}, {"limit": 0}, {"limit": 201}):
        response = await client.get("/api/analytics/spend/conversations", params=params)
        assert response.status_code == 422, (params, response.text)


async def test_default_days_and_limit_are_applied_with_no_data(client, seed):
    team = make_workspace("Co")
    project = Project(id=uuid.uuid4(), workspace_id=team.id, name="P")
    user = make_user("analyst@example.com")
    await seed(team, project, user, make_member(user, team, role="analyst"))

    await login(client, user.email)
    response = await client.get("/api/analytics/spend/conversations")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "period_days": 30,
        "conversations": [],
        "totals": {
            "conversation_count": 0,
            "run_count": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_usd": 0.0,
            "reported_cost_usd": None,
        },
    }


async def test_limit_caps_named_conversations_but_the_null_row_always_reconciles(
    client, seed, session_factory
):
    """`limit` bounds how many *named* conversations come back; the
    no-conversation reconciliation row is never dropped by it, even when it
    would otherwise miss the cut on rank alone."""
    team = make_workspace("Co")
    project = Project(id=uuid.uuid4(), workspace_id=team.id, name="P")
    user = make_user("analyst@example.com")
    harness = make_harness(team)
    await seed(team, project, user, make_member(user, team, role="analyst"), harness)

    convs = [
        Conversation(id=uuid.uuid4(), project_id=project.id, harness_id=harness.id, title=f"C{i}")
        for i in range(3)
    ]
    await seed(*convs)

    async with session_factory() as db:
        db.add_all(
            [
                make_run(project_id=project.id, harness_id=harness.id, conversation_id=convs[i].id,
                         cost_usd="1", reported=str(10 - i))
                for i in range(3)
            ]
            # Costs the least of everything here — if `limit` truncated
            # every row by rank uniformly, this would be the first dropped.
            + [
                make_run(project_id=project.id, harness_id=harness.id, conversation_id=None,
                         cost_usd="0.01", reported="0.01")
            ]
        )
        await db.commit()

    await login(client, user.email)
    response = await client.get("/api/analytics/spend/conversations", params={"limit": 2})
    assert response.status_code == 200, response.text
    body = response.json()
    ids = [c["conversation_id"] for c in body["conversations"]]
    assert ids == [str(convs[0].id), str(convs[1].id), None]

    # `limit` truncated the *rows*, but totals still cover all 3 named
    # conversations plus the null bucket — the number a bill gets reconciled
    # against must never shrink just because fewer rows came back.
    assert body["totals"]["conversation_count"] == 3
    assert body["totals"]["run_count"] == 4
    assert body["totals"]["reported_cost_usd"] == 10 + 9 + 8 + 0.01


async def test_tied_reported_cost_breaks_ties_on_conversation_id(client, seed, session_factory):
    """Two conversations spending the identical amount must still come back
    in a fixed order — on conversation_id, ascending — rather than swapping
    places between otherwise-identical requests."""
    team = make_workspace("Co")
    project = Project(id=uuid.uuid4(), workspace_id=team.id, name="P")
    user = make_user("analyst@example.com")
    harness = make_harness(team)
    await seed(team, project, user, make_member(user, team, role="analyst"), harness)

    convs = sorted(
        [
            Conversation(id=uuid.uuid4(), project_id=project.id, harness_id=harness.id, title="X"),
            Conversation(id=uuid.uuid4(), project_id=project.id, harness_id=harness.id, title="Y"),
        ],
        key=lambda c: c.id,
    )
    await seed(*convs)

    async with session_factory() as db:
        db.add_all(
            [
                make_run(project_id=project.id, harness_id=harness.id, conversation_id=c.id,
                         cost_usd="1", reported="1")
                for c in convs
            ]
        )
        await db.commit()

    await login(client, user.email)
    for _ in range(3):
        response = await client.get("/api/analytics/spend/conversations")
        assert response.status_code == 200, response.text
        ids = [c["conversation_id"] for c in response.json()["conversations"]]
        assert ids == [str(convs[0].id), str(convs[1].id)]
