"""`tret/services/budgets.py` (window math, spend query, the pre-run gate and
the alert post-run hook) and `tret/api/budgets.py` (GET/PUT/DELETE
`/api/workspace/settings/budget`).

Two suites, same split `test_reconcile.py` and `test_emissions_settings_api.py`
use for their own neighbouring features:

* a unit-level one (real sqlite ORM, no HTTP) that seeds workspaces/projects/
  harnesses/runs directly and calls `period_spend`/`budget_status`/
  `budget_pre_run_gate`/`budget_alert_post_run_hook` straight — the query
  shapes here (a workspace-scoped join through `projects`, running-vs-finished
  cost, window boundaries) are exactly the kind a hand-rolled fake session
  gets subtly wrong;
* an API-level one (real sqlite via `httpx.ASGITransport`, the same setup
  `test_emissions_settings_api.py` uses) that drives the router through a real
  dependency chain — role gating and 422 shapes are a router concern, not a
  service one.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest
import pytest_asyncio
from argon2 import PasswordHasher
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from tests.evals.golden_world import install_sqlite_type_shims
from tret.api import auth, budgets as budgets_api
from tret.db.engine import get_db
from tret.db.models import Base, Harness, Project, Run, User, Workspace, WorkspaceMember
from tret.engine import extensions as extensions_module
from tret.services import budgets as budgets_module
from tret.services.budgets import (
    budget_alert_post_run_hook,
    budget_pre_run_gate,
    budget_status,
    period_spend,
    window_bounds,
)

HASHER = PasswordHasher()
PASSWORD = "correct-horse-battery-1"

# A fixed Thursday, arbitrary but stable across every test that needs "now".
NOW = datetime(2026, 9, 10, 15, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _reset_extension_registry():
    """Every test starts with no registry set — the same isolation
    test_extensions.py and test_workspaces_api.py give their own tests. Not
    strictly needed by the unit suite (it calls the gate/hook directly, never
    through the registry), but the API suite's `client` fixture builds a bare
    FastAPI app without ever calling `load_extensions`, so this keeps a
    leftover registry from an earlier test out of the way regardless."""
    extensions_module._registry = None
    yield
    extensions_module._registry = None


# ── unit suite: real sqlite ORM, no HTTP ────────────────────────────────────
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
async def db(engine):
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session


async def _seed_workspace(db, *, max_cost_usd: float = 5.0) -> tuple[Workspace, Project, Harness]:
    workspace = Workspace(name="Acme", kind="team")
    db.add(workspace)
    await db.flush()

    project = Project(workspace_id=workspace.id, name="Acme Project")
    harness = Harness(
        workspace_id=workspace.id,
        name="Acme Harness",
        task_profile="freeform",
        model_policy={"mode": "auto"},
        tool_names=[],
        loop_config={
            "max_iterations": 24,
            "max_output_tokens": 8192,
            "temperature": 0.2,
            "max_cost_usd": max_cost_usd,
        },
    )
    db.add_all([project, harness])
    await db.flush()
    await db.commit()
    return workspace, project, harness


def _run(
    project_id: uuid.UUID,
    harness_id: uuid.UUID,
    *,
    status: str,
    started_at: datetime | None = None,
    cost_usd: Decimal = Decimal("0"),
    reported_cost_usd: Decimal | None = None,
) -> Run:
    return Run(
        project_id=project_id,
        harness_id=harness_id,
        task_type="freeform",
        task_input={},
        document_ids=[],
        status=status,
        messages=[],
        started_at=started_at,
        cost_usd=cost_usd,
        reported_cost_usd=reported_cost_usd,
    )


# ── window_bounds ────────────────────────────────────────────────────────────
def test_window_bounds_daily_is_the_calendar_day():
    start, end = window_bounds("daily", NOW)
    assert start == NOW.replace(hour=0, minute=0, second=0, microsecond=0)
    assert end == start + timedelta(days=1)
    assert start <= NOW < end


def test_window_bounds_weekly_starts_monday_utc():
    start, end = window_bounds("weekly", NOW)
    assert start.weekday() == 0  # Monday
    assert start.hour == 0
    assert end == start + timedelta(days=7)
    assert start <= NOW < end
    # Every day in the same ISO week resolves to the identical window.
    for offset in range(7):
        day = start + timedelta(days=offset, hours=3)
        assert window_bounds("weekly", day) == (start, end)


def test_window_bounds_monthly_is_the_calendar_month():
    start, end = window_bounds("monthly", NOW)
    assert start == NOW.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    assert end == datetime(2026, 10, 1, tzinfo=timezone.utc)


def test_window_bounds_monthly_rolls_the_year_over_in_december():
    start, end = window_bounds("monthly", datetime(2026, 12, 15, tzinfo=timezone.utc))
    assert start == datetime(2026, 12, 1, tzinfo=timezone.utc)
    assert end == datetime(2027, 1, 1, tzinfo=timezone.utc)


def test_window_bounds_rejects_an_unknown_period():
    with pytest.raises(ValueError):
        window_bounds("yearly", NOW)


# ── period_spend ─────────────────────────────────────────────────────────────
async def test_period_spend_sums_reported_over_catalog_for_finished_plus_running(db):
    workspace, project, harness = await _seed_workspace(db)
    window_start, _ = window_bounds("daily", NOW)

    db.add_all(
        [
            # Finished, provider-reported actual wins over the catalog price.
            _run(
                project.id, harness.id, status="completed",
                started_at=window_start + timedelta(hours=1),
                cost_usd=Decimal("2.00"), reported_cost_usd=Decimal("1.50"),
            ),
            # Finished, no reported actual: falls back to the catalog price.
            _run(
                project.id, harness.id, status="failed",
                started_at=window_start + timedelta(hours=2),
                cost_usd=Decimal("3.00"), reported_cost_usd=None,
            ),
            # Still running: counts its cost-so-far, reported or not.
            _run(
                project.id, harness.id, status="running",
                started_at=window_start + timedelta(hours=3),
                cost_usd=Decimal("0.75"),
            ),
            # Queued: no started_at yet, contributes nothing.
            _run(project.id, harness.id, status="queued", started_at=None, cost_usd=Decimal("9")),
        ]
    )
    await db.commit()

    spent = await period_spend(db, workspace.id, "daily", NOW)
    assert spent == Decimal("1.50") + Decimal("3.00") + Decimal("0.75")


async def test_period_spend_ignores_runs_outside_the_window(db):
    workspace, project, harness = await _seed_workspace(db)
    window_start, window_end = window_bounds("daily", NOW)

    db.add_all(
        [
            _run(
                project.id, harness.id, status="completed",
                started_at=window_start - timedelta(seconds=1), cost_usd=Decimal("100"),
            ),
            _run(
                project.id, harness.id, status="completed",
                started_at=window_end, cost_usd=Decimal("100"),
            ),
            _run(
                project.id, harness.id, status="completed",
                started_at=window_start, cost_usd=Decimal("5"),
            ),
        ]
    )
    await db.commit()

    assert await period_spend(db, workspace.id, "daily", NOW) == Decimal("5")


async def test_period_spend_ignores_other_workspaces(db):
    workspace, project, harness = await _seed_workspace(db)
    other_workspace, other_project, other_harness = await _seed_workspace(db)
    window_start, _ = window_bounds("daily", NOW)

    db.add_all(
        [
            _run(
                project.id, harness.id, status="completed",
                started_at=window_start, cost_usd=Decimal("5"),
            ),
            _run(
                other_project.id, other_harness.id, status="completed",
                started_at=window_start, cost_usd=Decimal("999"),
            ),
        ]
    )
    await db.commit()

    assert await period_spend(db, workspace.id, "daily", NOW) == Decimal("5")
    assert await period_spend(db, other_workspace.id, "daily", NOW) == Decimal("999")


# ── budget_status ────────────────────────────────────────────────────────────
async def test_budget_status_is_none_when_no_budget_configured(db):
    workspace, _project, _harness = await _seed_workspace(db)
    assert await budget_status(db, workspace.id, NOW) is None


async def test_budget_status_is_none_for_an_unknown_workspace(db):
    assert await budget_status(db, uuid.uuid4(), NOW) is None


async def test_budget_status_reports_fraction_and_remaining(db):
    workspace, project, harness = await _seed_workspace(db)
    workspace.settings = {"budget": {"period": "monthly", "cap_usd": 100.0, "alerts": [0.5, 0.75, 1.0]}}
    await db.commit()
    window_start, window_end = window_bounds("monthly", NOW)
    db.add(
        _run(
            project.id, harness.id, status="completed",
            started_at=window_start + timedelta(days=1), cost_usd=Decimal("40"),
        )
    )
    await db.commit()

    status = await budget_status(db, workspace.id, NOW)
    assert status == {
        "period": "monthly",
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "cap_usd": 100.0,
        "alerts": [0.5, 0.75, 1.0],
        "spent_usd": 40.0,
        "remaining_usd": 60.0,
        "fraction": 0.4,
        "alerts_crossed": [],
    }


async def test_budget_status_lists_every_crossed_alert(db):
    workspace, project, harness = await _seed_workspace(db)
    workspace.settings = {"budget": {"period": "monthly", "cap_usd": 100.0, "alerts": [0.5, 0.75, 1.0]}}
    await db.commit()
    window_start, _ = window_bounds("monthly", NOW)
    db.add(
        _run(
            project.id, harness.id, status="completed",
            started_at=window_start, cost_usd=Decimal("80"),
        )
    )
    await db.commit()

    status = await budget_status(db, workspace.id, NOW)
    assert status["fraction"] == 0.8
    assert status["alerts_crossed"] == [0.5, 0.75]


# ── budget_pre_run_gate ──────────────────────────────────────────────────────
async def test_gate_allows_when_no_budget_is_configured(db):
    workspace, project, harness = await _seed_workspace(db)
    run = _run(project.id, harness.id, status="queued")
    db.add(run)
    await db.commit()

    result = await budget_pre_run_gate(db, run, workspace.id)
    assert result.allowed is True


async def test_gate_allows_when_spend_plus_reservation_stays_under_cap(db, monkeypatch):
    workspace, project, harness = await _seed_workspace(db, max_cost_usd=5.0)
    workspace.settings = {"budget": {"period": "daily", "cap_usd": 10.0, "alerts": [0.5]}}
    await db.commit()
    monkeypatch.setattr(budgets_module, "utcnow", lambda: NOW)
    window_start, _ = window_bounds("daily", NOW)
    db.add(_run(project.id, harness.id, status="completed", started_at=window_start, cost_usd=Decimal("3")))
    run = _run(project.id, harness.id, status="queued")
    db.add(run)
    await db.commit()

    # spent(3) + max_cost(5) = 8 <= cap(10)
    result = await budget_pre_run_gate(db, run, workspace.id)
    assert result.allowed is True


async def test_gate_refuses_once_spend_alone_reaches_the_cap(db, monkeypatch):
    workspace, project, harness = await _seed_workspace(db, max_cost_usd=5.0)
    workspace.settings = {"budget": {"period": "daily", "cap_usd": 10.0, "alerts": [0.5]}}
    await db.commit()
    monkeypatch.setattr(budgets_module, "utcnow", lambda: NOW)
    window_start, _ = window_bounds("daily", NOW)
    db.add(_run(project.id, harness.id, status="completed", started_at=window_start, cost_usd=Decimal("10")))
    run = _run(project.id, harness.id, status="queued")
    db.add(run)
    await db.commit()

    result = await budget_pre_run_gate(db, run, workspace.id)
    assert result.allowed is False
    assert result.reason == "budget_exhausted"
    assert "exhausted" in result.detail


async def test_gate_refuses_when_the_runs_own_reservation_would_exceed_the_cap(db, monkeypatch):
    """Under the cap on spend alone (7 < 10), but the run's own max_cost_usd
    reservation (5) would push it over (12 > 10) — the soft-reservation case
    the plain `spent >= cap` check alone would miss."""
    workspace, project, harness = await _seed_workspace(db, max_cost_usd=5.0)
    workspace.settings = {"budget": {"period": "daily", "cap_usd": 10.0, "alerts": [0.5]}}
    await db.commit()
    monkeypatch.setattr(budgets_module, "utcnow", lambda: NOW)
    window_start, _ = window_bounds("daily", NOW)
    db.add(_run(project.id, harness.id, status="completed", started_at=window_start, cost_usd=Decimal("7")))
    run = _run(project.id, harness.id, status="queued")
    db.add(run)
    await db.commit()

    result = await budget_pre_run_gate(db, run, workspace.id)
    assert result.allowed is False
    assert result.reason == "budget_exhausted"
    assert "would exceed" in result.detail


async def test_gate_allows_when_the_runs_own_reservation_exceeds_the_cap_itself(db, monkeypatch):
    """A cap smaller than the run's own `max_cost_usd` reservation (a $3 cap
    against the $5 `DEFAULT_MAX_COST_USD` every seeded harness reserves,
    here) must not brick every run against it forever — the reservation is
    meaningless once it alone is bigger than the whole cap, so this allows
    on spend alone (1 < 3) instead of refusing regardless of actual spend."""
    workspace, project, harness = await _seed_workspace(db, max_cost_usd=5.0)
    workspace.settings = {"budget": {"period": "daily", "cap_usd": 3.0, "alerts": [0.5]}}
    await db.commit()
    monkeypatch.setattr(budgets_module, "utcnow", lambda: NOW)
    window_start, _ = window_bounds("daily", NOW)
    db.add(_run(project.id, harness.id, status="completed", started_at=window_start, cost_usd=Decimal("1")))
    run = _run(project.id, harness.id, status="queued")
    db.add(run)
    await db.commit()

    result = await budget_pre_run_gate(db, run, workspace.id)
    assert result.allowed is True
    assert result.detail is not None
    assert "meaningless" in result.detail


async def test_gate_still_refuses_on_spend_alone_when_the_reservation_exceeds_the_cap(db, monkeypatch):
    """Same oversized reservation as above, but spend alone has now reached
    the cap: case 3 (reservation bigger than cap) only skips the
    *reservation* check, it must not also skip the plain `spent >= cap`
    refusal."""
    workspace, project, harness = await _seed_workspace(db, max_cost_usd=5.0)
    workspace.settings = {"budget": {"period": "daily", "cap_usd": 3.0, "alerts": [0.5]}}
    await db.commit()
    monkeypatch.setattr(budgets_module, "utcnow", lambda: NOW)
    window_start, _ = window_bounds("daily", NOW)
    db.add(_run(project.id, harness.id, status="completed", started_at=window_start, cost_usd=Decimal("3")))
    run = _run(project.id, harness.id, status="queued")
    db.add(run)
    await db.commit()

    result = await budget_pre_run_gate(db, run, workspace.id)
    assert result.allowed is False
    assert result.reason == "budget_exhausted"
    assert "exhausted" in result.detail


async def test_gate_fails_open_on_a_db_error(db, monkeypatch, caplog):
    workspace, project, harness = await _seed_workspace(db)
    workspace.settings = {"budget": {"period": "daily", "cap_usd": 10.0, "alerts": [0.5]}}
    await db.commit()
    run = _run(project.id, harness.id, status="queued")
    db.add(run)
    await db.commit()

    async def _raises(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(budgets_module, "period_spend", _raises)

    result = await budget_pre_run_gate(db, run, workspace.id)
    assert result.allowed is True


# ── budget_alert_post_run_hook ───────────────────────────────────────────────
class _RecordingBus:
    def __init__(self) -> None:
        self.published: list[tuple[uuid.UUID, str, dict]] = []

    async def publish(self, run_id, event) -> None:
        self.published.append((run_id, event.type, dict(event.data)))


async def test_alert_hook_is_a_no_op_with_no_budget_configured(db):
    workspace, project, harness = await _seed_workspace(db)
    run = _run(project.id, harness.id, status="completed", started_at=NOW, cost_usd=Decimal("999"))
    db.add(run)
    await db.commit()

    await budget_alert_post_run_hook(db, run, workspace.id)  # must not raise


async def test_alert_hook_fires_once_per_threshold_and_resets_on_a_new_window(db, monkeypatch, caplog):
    workspace, project, harness = await _seed_workspace(db)
    workspace.settings = {"budget": {"period": "daily", "cap_usd": 100.0, "alerts": [0.5, 0.75, 1.0]}}
    await db.commit()

    bus = _RecordingBus()
    monkeypatch.setattr(budgets_module, "get_event_bus", lambda: bus)
    monkeypatch.setattr(budgets_module, "utcnow", lambda: NOW)
    window_start, _ = window_bounds("daily", NOW)

    # Run 1: total spend 60/100 = 0.6 -> crosses 0.5 only.
    run1 = _run(
        project.id, harness.id, status="completed",
        started_at=window_start + timedelta(hours=1), cost_usd=Decimal("60"),
    )
    db.add(run1)
    await db.commit()
    with caplog.at_level("WARNING", logger="tret.budgets"):
        await budget_alert_post_run_hook(db, run1, workspace.id)
    assert [e[1] for e in bus.published] == ["budget_alert"]
    assert bus.published[0][0] == run1.id
    assert bus.published[0][2]["fraction"] == 0.5
    assert "50%" in caplog.text

    # Re-read `settings` with an explicit `refresh`, not a plain attribute
    # access on the same in-memory object: `workspace.settings` here would
    # just hand back whatever the hook's own `workspace.settings = settings`
    # assignment left in memory regardless of whether that ever reached the
    # database — an in-place `dict.update` (which SQLAlchemy would never
    # flush, since it only detects a JSONB column change on assignment)
    # would pass a check like that right up until the process restarted.
    # `refresh` re-issues a SELECT for this column specifically, so only an
    # actually-committed write survives it.
    await db.refresh(workspace, attribute_names=["settings"])
    assert workspace.settings["budget_state"]["alerted"] == [0.5]

    # Run 2: total spend 61/100 = 0.61 -> no new threshold crossed.
    bus.published.clear()
    run2 = _run(
        project.id, harness.id, status="completed",
        started_at=window_start + timedelta(hours=2), cost_usd=Decimal("1"),
    )
    db.add(run2)
    await db.commit()
    await budget_alert_post_run_hook(db, run2, workspace.id)
    assert bus.published == []

    # Run 3: total spend 111/100 = 1.11 -> crosses 0.75 and 1.0, in order.
    run3 = _run(
        project.id, harness.id, status="completed",
        started_at=window_start + timedelta(hours=3), cost_usd=Decimal("50"),
    )
    db.add(run3)
    await db.commit()
    await budget_alert_post_run_hook(db, run3, workspace.id)
    assert [e[2]["fraction"] for e in bus.published] == [0.75, 1.0]

    # A new window (the next day): already-alerted thresholds reset.
    bus.published.clear()
    tomorrow = NOW + timedelta(days=1)
    monkeypatch.setattr(budgets_module, "utcnow", lambda: tomorrow)
    tomorrow_start, _ = window_bounds("daily", tomorrow)
    run4 = _run(
        project.id, harness.id, status="completed",
        started_at=tomorrow_start + timedelta(hours=1), cost_usd=Decimal("60"),
    )
    db.add(run4)
    await db.commit()
    await budget_alert_post_run_hook(db, run4, workspace.id)
    assert [e[2]["fraction"] for e in bus.published] == [0.5]


async def test_alert_hook_logs_but_does_not_publish_when_the_run_did_not_finish_normally(
    db, monkeypatch, caplog,
):
    """The crash handler and `services/reconcile.py`'s orphan sweep both call
    this hook against a run left `status="failed"` — by the time that
    happens, the run's SSE stream is very likely already terminal and
    forgotten (see the hook's own docstring), so this only publishes on the
    normal `"completed"` finish path. The WARNING still fires either way,
    since the log line is the record of the crossing on every other path."""
    workspace, project, harness = await _seed_workspace(db)
    workspace.settings = {"budget": {"period": "daily", "cap_usd": 100.0, "alerts": [0.5]}}
    await db.commit()

    bus = _RecordingBus()
    monkeypatch.setattr(budgets_module, "get_event_bus", lambda: bus)
    monkeypatch.setattr(budgets_module, "utcnow", lambda: NOW)
    window_start, _ = window_bounds("daily", NOW)

    run = _run(
        project.id, harness.id, status="failed",
        started_at=window_start, cost_usd=Decimal("60"),
    )
    db.add(run)
    await db.commit()

    with caplog.at_level("WARNING", logger="tret.budgets"):
        await budget_alert_post_run_hook(db, run, workspace.id)

    assert bus.published == []
    assert "50%" in caplog.text

    # See the sibling test above for why this is `refresh`, not a plain
    # attribute read on the same in-memory object.
    await db.refresh(workspace, attribute_names=["settings"])
    assert workspace.settings["budget_state"]["alerted"] == [0.5]


async def test_alert_hook_fails_open_on_a_db_error(db, monkeypatch):
    workspace, project, harness = await _seed_workspace(db)
    workspace.settings = {"budget": {"period": "daily", "cap_usd": 100.0, "alerts": [0.5]}}
    await db.commit()
    run = _run(project.id, harness.id, status="completed", started_at=NOW, cost_usd=Decimal("60"))
    db.add(run)
    await db.commit()

    async def _raises(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(budgets_module, "period_spend", _raises)

    await budget_alert_post_run_hook(db, run, workspace.id)  # must not raise


# ── API: GET/PUT/DELETE /api/workspace/settings/budget ──────────────────────
@pytest_asyncio.fixture
async def api_engine():
    install_sqlite_type_shims()
    eng = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(api_engine):
    return async_sessionmaker(api_engine, expire_on_commit=False)


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
    app.include_router(budgets_api.router)

    async def _get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _get_db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def make_user(email: str, *, password: str = PASSWORD, role: str = "analyst") -> User:
    return User(
        id=uuid.uuid4(),
        email=email,
        display_name=email.split("@")[0].title(),
        password_hash=HASHER.hash(password),
        role=role,
    )


def make_workspace(name: str, *, settings: dict | None = None) -> Workspace:
    return Workspace(id=uuid.uuid4(), name=name, kind="team", settings=settings or {})


def make_member(user: User, workspace: Workspace, *, role: str) -> WorkspaceMember:
    return WorkspaceMember(user_id=user.id, workspace_id=workspace.id, role=role)


async def login(client: httpx.AsyncClient, email: str, password: str = PASSWORD) -> httpx.Response:
    response = await client.post("/api/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response


async def test_get_with_no_budget_returns_null(client, seed):
    team = make_workspace("Acme")
    owner = make_user("owner1@example.com")
    await seed(team, owner, make_member(owner, team, role="owner"))
    await login(client, owner.email)

    response = await client.get("/api/workspace/settings/budget")
    assert response.status_code == 200, response.text
    assert response.json() == {"budget": None}


async def test_get_is_open_to_any_member(client, seed):
    team = make_workspace("Acme")
    owner = make_user("owner2@example.com")
    analyst = make_user("analyst2@example.com")
    await seed(
        team, owner, analyst,
        make_member(owner, team, role="owner"),
        make_member(analyst, team, role="analyst"),
    )
    await login(client, analyst.email)

    response = await client.get("/api/workspace/settings/budget")
    assert response.status_code == 200


async def test_put_sets_the_budget_and_returns_live_status(client, seed):
    team = make_workspace("Acme")
    owner = make_user("owner3@example.com")
    await seed(team, owner, make_member(owner, team, role="owner"))
    await login(client, owner.email)

    response = await client.put(
        "/api/workspace/settings/budget",
        json={"period": "monthly", "cap_usd": 250, "alerts": [0.5, 0.9]},
    )
    assert response.status_code == 200, response.text
    body = response.json()["budget"]
    assert body["period"] == "monthly"
    assert body["cap_usd"] == 250.0
    assert body["spent_usd"] == 0.0
    assert body["fraction"] == 0.0


async def test_get_degrades_to_config_only_when_period_spend_raises(client, seed, monkeypatch):
    """A `period_spend` failure inside `budget_status` must not 500 a plain
    GET — it degrades to the stored config with the live spend fields
    nulled out, same as PUT below."""
    team = make_workspace(
        "Acme", settings={"budget": {"period": "daily", "cap_usd": 10.0, "alerts": [0.5]}}
    )
    owner = make_user("owner9@example.com")
    await seed(team, owner, make_member(owner, team, role="owner"))
    await login(client, owner.email)

    async def _raises(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(budgets_module, "period_spend", _raises)

    response = await client.get("/api/workspace/settings/budget")
    assert response.status_code == 200, response.text
    body = response.json()["budget"]
    assert body["period"] == "daily"
    assert body["cap_usd"] == 10.0
    assert body["alerts"] == [0.5]
    assert body["spent_usd"] is None
    assert body["remaining_usd"] is None
    assert body["fraction"] is None
    assert body["alerts_crossed"] is None


async def test_put_degrades_to_config_only_when_period_spend_raises_after_commit(
    client, seed, monkeypatch
):
    """The write itself must not 500 just because computing the fresh status
    to hand back afterward fails — the config is already committed by the
    time `period_spend` blows up."""
    team = make_workspace("Acme")
    owner = make_user("owner10@example.com")
    await seed(team, owner, make_member(owner, team, role="owner"))
    await login(client, owner.email)

    async def _raises(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(budgets_module, "period_spend", _raises)

    response = await client.put(
        "/api/workspace/settings/budget",
        json={"period": "monthly", "cap_usd": 250, "alerts": [0.5, 0.9]},
    )
    assert response.status_code == 200, response.text
    body = response.json()["budget"]
    assert body["period"] == "monthly"
    assert body["cap_usd"] == 250.0
    assert body["alerts"] == [0.5, 0.9]
    assert body["spent_usd"] is None
    assert body["fraction"] is None

    # The write itself landed even though the status computed alongside it
    # was degraded — a still-broken `period_spend` degrades this GET too.
    get_response = await client.get("/api/workspace/settings/budget")
    assert get_response.json()["budget"]["cap_usd"] == 250.0


async def test_put_bad_period_is_422(client, seed):
    team = make_workspace("Acme")
    owner = make_user("owner4@example.com")
    await seed(team, owner, make_member(owner, team, role="owner"))
    await login(client, owner.email)

    response = await client.put(
        "/api/workspace/settings/budget", json={"period": "yearly", "cap_usd": 10}
    )
    assert response.status_code == 422, response.text
    assert isinstance(response.json()["detail"], str)


async def test_put_non_positive_cap_is_422(client, seed):
    team = make_workspace("Acme")
    owner = make_user("owner5@example.com")
    await seed(team, owner, make_member(owner, team, role="owner"))
    await login(client, owner.email)

    response = await client.put(
        "/api/workspace/settings/budget", json={"period": "daily", "cap_usd": 0}
    )
    assert response.status_code == 422, response.text
    assert "cap_usd" in response.json()["detail"]


async def test_put_out_of_range_alert_is_422(client, seed):
    team = make_workspace("Acme")
    owner = make_user("owner6@example.com")
    await seed(team, owner, make_member(owner, team, role="owner"))
    await login(client, owner.email)

    response = await client.put(
        "/api/workspace/settings/budget",
        json={"period": "daily", "cap_usd": 10, "alerts": [1.5]},
    )
    assert response.status_code == 422, response.text
    assert "alerts" in response.json()["detail"]


async def test_put_requires_admin(client, seed):
    team = make_workspace("Acme")
    owner = make_user("owner7@example.com")
    analyst = make_user("analyst7@example.com")
    await seed(
        team, owner, analyst,
        make_member(owner, team, role="owner"),
        make_member(analyst, team, role="analyst"),
    )
    await login(client, analyst.email)

    response = await client.put(
        "/api/workspace/settings/budget", json={"period": "daily", "cap_usd": 10}
    )
    assert response.status_code == 403


async def test_delete_clears_a_previously_set_budget(client, seed):
    team = make_workspace("Acme")
    owner = make_user("owner8@example.com")
    await seed(team, owner, make_member(owner, team, role="owner"))
    await login(client, owner.email)

    put_response = await client.put(
        "/api/workspace/settings/budget", json={"period": "daily", "cap_usd": 10}
    )
    assert put_response.status_code == 200

    delete_response = await client.delete("/api/workspace/settings/budget")
    assert delete_response.status_code == 200, delete_response.text
    assert delete_response.json() == {"budget": None}

    get_response = await client.get("/api/workspace/settings/budget")
    assert get_response.json() == {"budget": None}
