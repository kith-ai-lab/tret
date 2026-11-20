"""R25 API: GET/PUT /api/guardrails, overhead aggregation, validation, and the
R28 `checks` block of /api/analytics/guardrails."""
from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from argon2 import PasswordHasher
from fastapi import FastAPI
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from tests.evals.golden_world import install_sqlite_type_shims
from tret.api import auth
from tret.api import guardrails as guardrails_api
from tret.api.analytics import _check_stats, _guardrails_response
from tret.config import Settings, get_settings
from tret.db.engine import get_db
from tret.db.models import (
    Base, Harness, Project, Run, User, Workspace, WorkspaceMember, utcnow,
)
from tret.services.guardrails import (
    GuardrailValidationError, aggregate_overhead, apply_update, build_catalog,
    resolve_guardrails, validate_update,
)

HASHER = PasswordHasher()
PASSWORD = "correct-horse-battery-1"
URL = "/api/guardrails"


@pytest.fixture(autouse=True)
def _fresh_settings():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def session_factory():
    install_sqlite_type_shims()
    eng = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(eng, expire_on_commit=False)
    await eng.dispose()


@pytest_asyncio.fixture
async def world(session_factory):
    ws = Workspace(id=uuid.uuid4(), name="WS", kind="team", settings={})
    project = Project(id=uuid.uuid4(), workspace_id=ws.id, name="P")
    harness = Harness(id=uuid.uuid4(), workspace_id=ws.id, name="Chat", task_profile="chat",
                      loop_config={"max_iterations": 24, "max_output_tokens": 12345})
    async with session_factory() as db:
        db.add(ws)
        await db.flush()
        db.add_all([project, harness])
        await db.commit()
    return SimpleNamespace(ws=ws, project=project, harness=harness)


@pytest_asyncio.fixture
async def client(session_factory):
    app = FastAPI()
    app.include_router(auth.router)
    app.include_router(guardrails_api.router)

    async def _get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _get_db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _login_as(client, session_factory, world, role):
    user = User(id=uuid.uuid4(), email=f"{role}-{uuid.uuid4().hex[:6]}@example.com", display_name=role,
                password_hash=HASHER.hash(PASSWORD), role="analyst")
    async with session_factory() as db:
        db.add_all([user, WorkspaceMember(user_id=user.id, workspace_id=world.ws.id, role=role)])
        await db.commit()
    r = await client.post("/api/auth/login", json={"email": user.email, "password": PASSWORD})
    assert r.status_code == 200, r.text


def _run(world, *, guardrails=None, grounding=None, stop_reason=None, tin=1000, tout=1000,
         cost="1.0", wh="2.0", age_days=1):
    return Run(
        id=uuid.uuid4(), project_id=world.project.id, harness_id=world.harness.id, task_type="chat",
        task_input={}, document_ids=[], status="completed", messages=[], guardrails=guardrails,
        grounding=grounding, stop_reason=stop_reason, input_tokens=tin, output_tokens=tout,
        cost_usd=Decimal(cost), energy_wh=Decimal(wh), created_at=utcnow() - timedelta(days=age_days),
    )


# ── GET ──────────────────────────────────────────────────────────────────────
async def test_get_shape(client, session_factory, world):
    await _login_as(client, session_factory, world, "analyst")
    body = (await client.get(URL)).json()
    assert [t["id"] for t in body["tiers"]] == ["locked", "member", "operator"]
    by_tier: dict[str, list[str]] = {}
    for item in body["items"]:
        by_tier.setdefault(item["tier"], []).append(item["id"])
        assert set(item) == {"id", "tier", "title", "description", "adjustable", "value", "state",
                             "bounds", "overhead"}
    assert by_tier["locked"] == ["numbers_from_lookups_or_methods", "provenance",
                                 "draft_until_approved", "create_only_writes"]
    assert by_tier["member"] == ["number_check", "repair_attempts", "html_check", "web_research",
                                 "spend_cap_usd", "max_subagents"]
    assert by_tier["operator"] == ["code_sandbox_network_isolation", "pack_integrity",
                                   "unvetted_analysis", "research_allowlist_hosts",
                                   "degenerate_output_guard", "output_limit"]
    items = {i["id"]: i for i in body["items"]}
    assert not any(items[i]["adjustable"] for i in by_tier["locked"] + by_tier["operator"])
    assert items["repair_attempts"]["bounds"] == {"min": 0, "max": 3}
    assert items["output_limit"]["value"] == 12345  # the chat harness's own limit
    assert body["member"]["repair_attempts"] == 2 and body["member"]["number_check"] is True
    assert items["number_check"]["overhead"] is None


async def test_overhead_from_last_30_days_only(client, session_factory, world):
    oh = {"repair_turns": 2, "repair_input_tokens": 300, "repair_output_tokens": 200}
    async with session_factory() as db:
        db.add_all([
            _run(world, guardrails={"overhead": oh}, tin=1000, tout=1000, cost="1.0", wh="2.0"),
            _run(world, guardrails={"overhead": {"repair_turns": 0, "repair_input_tokens": 0,
                                                 "repair_output_tokens": 0}}),
            _run(world, guardrails=None),  # predates the record: not counted
            _run(world, guardrails={"overhead": oh}, age_days=45),  # outside the window
        ])
        await db.commit()
    await _login_as(client, session_factory, world, "owner")
    item = {i["id"]: i for i in (await client.get(URL)).json()["items"]}["number_check"]
    assert item["overhead"]["window_days"] == 30
    assert item["overhead"]["runs"] == 2
    assert item["overhead"]["repair_turns"] == 2
    assert item["overhead"]["input_tokens"] == 300 and item["overhead"]["output_tokens"] == 200
    # 500 of 2000 tokens -> a quarter of the run's cost and energy
    assert item["overhead"]["cost_usd"] == pytest.approx(0.25)
    assert item["overhead"]["energy_wh"] == pytest.approx(0.5)


# ── PUT ──────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("role,code", [("analyst", 403), ("approver", 403), ("admin", 200), ("owner", 200)])
async def test_put_requires_workspace_admin(client, session_factory, world, role, code):
    await _login_as(client, session_factory, world, role)
    r = await client.put(URL, json={"number_check": False})
    assert r.status_code == code, r.text
    assert (await client.get(URL)).status_code == 200


async def test_put_stores_clamps_and_merges(client, session_factory, world):
    await _login_as(client, session_factory, world, "admin")
    r = await client.put(URL, json={"repair_attempts": 99, "html_check": False, "max_subagents": 99})
    assert r.status_code == 200, r.text
    member = r.json()["member"]
    assert member["repair_attempts"] == 3 and member["html_check"] is False
    assert member["max_subagents"] == get_settings().max_fanout
    # A later partial update keeps earlier keys and None clears one.
    r = await client.put(URL, json={"spend_cap_usd": 2.5})
    assert r.json()["member"]["spend_cap_usd"] == 2.5 and r.json()["member"]["html_check"] is False
    r = await client.put(URL, json={"spend_cap_usd": None, "repair_attempts": -4})
    assert r.json()["member"]["spend_cap_usd"] is None and r.json()["member"]["repair_attempts"] == 0
    async with session_factory() as db:
        ws = await db.get(Workspace, world.ws.id)
        assert ws.settings["guardrails"]["html_check"] is False
        assert "spend_cap_usd" not in ws.settings["guardrails"]


@pytest.mark.parametrize("body", [
    {"number_check": "yes"},
    {"repair_attempts": 1.5},
    {"repair_attempts": True},
    {"spend_cap_usd": 0},
    {"spend_cap_usd": "5"},
    {"max_subagents": "all"},
    {"bogus": 1},
])
async def test_put_rejects_bad_values(client, session_factory, world, body):
    await _login_as(client, session_factory, world, "owner")
    r = await client.put(URL, json=body)
    assert r.status_code == 422, r.text
    async with session_factory() as db:
        assert "guardrails" not in ((await db.get(Workspace, world.ws.id)).settings or {})


async def test_put_rejects_non_object(client, session_factory, world):
    await _login_as(client, session_factory, world, "owner")
    assert (await client.put(URL, json=[1, 2])).status_code == 422


# ── service helpers ──────────────────────────────────────────────────────────
def test_validate_update_unit():
    s = Settings()
    assert validate_update({}, s) == {}
    assert validate_update({"spend_cap_usd": 0.0001}, s) == {"spend_cap_usd": 0.01}
    assert validate_update({"spend_cap_usd": 10**9}, s)["spend_cap_usd"] == 10_000.0
    with pytest.raises(GuardrailValidationError) as exc:
        validate_update({"number_check": 1, "zzz": 1}, s)
    assert len(exc.value.errors) == 2


def test_apply_update_does_not_mutate_input():
    ws = {"guardrails": {"html_check": False}, "other": 1}
    out = apply_update(ws, {"number_check": False, "html_check": None})
    assert out == {"number_check": False} and ws["guardrails"] == {"html_check": False}


def test_web_research_item_not_adjustable_when_operator_disallows():
    items = {i["id"]: i for i in build_catalog(Settings(egress_research="off"), {})["items"]}
    assert items["web_research"]["adjustable"] is False
    assert items["web_research"]["value"] is False
    assert resolve_guardrails(Settings(egress_research="off"), {"guardrails": {"web_research": True}}).web_research is False


def test_aggregate_overhead_prorates_and_handles_zero_tokens():
    rows = [
        {"guardrails": {"overhead": {"repair_turns": 1, "repair_input_tokens": 100, "repair_output_tokens": 100}},
         "input_tokens": 500, "output_tokens": 500, "cost_usd": Decimal("2"), "energy_wh": None},
        {"guardrails": {"overhead": {"repair_turns": 1, "repair_input_tokens": 10, "repair_output_tokens": 0}},
         "input_tokens": 0, "output_tokens": 0, "cost_usd": Decimal("9"), "energy_wh": Decimal("9")},
        {"guardrails": None},
    ]
    out = aggregate_overhead(rows)
    assert out["runs"] == 2 and out["repair_turns"] == 2
    assert out["cost_usd"] == pytest.approx(0.4) and out["energy_wh"] == 0.0
    assert aggregate_overhead([{"guardrails": None}]) is None


# ── R28: analytics checks ────────────────────────────────────────────────────
class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def scalars(self):
        return self

    def unique(self):
        return self


class _CheckSession:
    def __init__(self, rows):
        self.rows = rows
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _Rows(self.rows if "runs.grounding" in str(statement) else [])


ROWS = [
    ({"checked": True, "status": "clean"}, None, {}),
    ({"checked": True, "status": "repaired", "unvetted": [{"value": "5"}]}, None,
     {"html_check": {"status": "issues", "repaired": True}}),
    ({"checked": True, "status": "unresolved"}, "max_tokens", {"truncated": {"model": "m"}}),
    ({"checked": False, "status": "skipped"}, "out_of_steps", {}),
    ({"checked": False, "status": "off"}, "degenerate_output", {"degenerate": {"pattern": "[PAD]"}}),
    (None, None, None),  # run that predates the check
    ({"checked": True, "status": "clean"}, None, {"html_check": {"status": "ok"}}),
]


async def test_checks_counts():
    out = await _check_stats(_CheckSession(ROWS), None, None, 30)
    assert out == {
        "window_days": 30, "runs_checked": 6, "clean": 2, "repaired": 1, "unresolved": 1,
        "skipped": 1, "off": 1, "unvetted": 1, "html_issues": 1, "html_repaired": 1,
        "truncated": 1, "out_of_steps": 1, "degenerate": 1,
    }


async def test_guardrails_response_carries_checks_and_query_compiles():
    db = _CheckSession(ROWS)
    out = await _guardrails_response(project_id=None, days=7, user=None, db=db)
    assert out["checks"]["runs_checked"] == 6 and out["checks"]["window_days"] == 7
    for statement in db.statements:
        assert "SELECT" in str(statement.compile(dialect=postgresql.dialect()))
