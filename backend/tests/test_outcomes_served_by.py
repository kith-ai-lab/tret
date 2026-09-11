"""`services/outcomes.build_outcomes` carries `served_by` — the upstream
endpoint that served each segment (see db/models.py RunOutcome.served_by,
populated from `ModelSegment.served_by` in engine/harness.py) — onto the
`RunOutcome` rows it derives.

Real sqlite database, same harness as test_workspace_service.py /
test_connections_service.py: it is what asserts the migration's column
actually exists on the model the ORM queries against (the suite's
migration-backed tests run schema through `Base.metadata.create_all`, not
Alembic — see test_schema_migrations.py's own docstring). The `Run` rows
below are built and passed straight to `build_outcomes` without ever being
added to the session: everything it reads either comes off the object
directly or is short-circuited (an explicit `routing["task_shape"]` skips the
pack lookup, `harness_id=None` skips the iteration-ceiling lookup), so the
only real query it makes — the finding-status count — runs safely against an
empty, schema-only database.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from tests.evals.golden_world import install_sqlite_type_shims
from tret.db.models import Base, Run, RunOutcome
from tret.services.outcomes import build_outcomes

NOW = datetime(2026, 9, 10, tzinfo=timezone.utc)


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
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session


def _base_run(**over) -> Run:
    run = Run(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        harness_id=None,  # skips the max_iterations lookup entirely
        task_type="assess",
        status="completed",
        error=None,
        messages=[],
        iterations=3,
        cost_usd=Decimal("0.03"),
        input_tokens=1000,
        output_tokens=200,
        energy_wh=Decimal("0.4"),
        routing={"task_shape": "verdict", "objective": "balanced", "max_cost_tier": "premium"},
        model_timeline=None,
        context_composition=None,
        created_at=NOW,
        started_at=NOW,
        finished_at=NOW,
    )
    for key, value in over.items():
        setattr(run, key, value)
    return run


def test_the_column_exists_on_the_model_the_orm_queries_against():
    """The functional half of "the migration applies on the test DB path the
    suite uses" (see this file's own docstring): `Base.metadata.create_all`
    builds the schema from the declared columns, so a `served_by` missing
    from the model — even with a correct migration file — would show up here
    as a plain `AttributeError`, not a schema mismatch.
    """
    row = RunOutcome(served_by="deepinfra/fp8")
    assert row.served_by == "deepinfra/fp8"


@pytest.mark.asyncio
async def test_the_ordinary_single_model_run_carries_served_by_from_its_segment(db):
    # For an ordinary (non-switching) run the engine persists `model_timeline`
    # when a turn was estimated or, at the finish path, when a segment carries
    # a `served_by` (engine/harness.py) — one segment, same shape as a real one.
    run = _base_run(
        model_used="openrouter/big",
        provider_used="openrouter",
        model_timeline=[
            {
                "model": "openrouter/big",
                "provider": "openrouter",
                "served_by": "deepinfra/fp8",
            }
        ],
    )
    rows = await build_outcomes(db, run)
    assert len(rows) == 1
    assert rows[0].served_by == "deepinfra/fp8"
    assert rows[0].model_id == "openrouter/big"


@pytest.mark.asyncio
async def test_an_ordinary_run_with_no_timeline_records_served_by_as_none(db):
    # The common case: no model switch, no estimated turn, so `model_timeline`
    # was never even set — nothing to guess a serving endpoint from.
    run = _base_run(model_used="anthropic/fable", provider_used="anthropic")
    rows = await build_outcomes(db, run)
    assert len(rows) == 1
    assert rows[0].served_by is None


@pytest.mark.asyncio
async def test_a_local_or_direct_segment_with_no_served_by_stays_none(db):
    # A segment engine/harness.py never got a served_by for — a local
    # deployment, the only provider today that never reports one (Anthropic
    # direct always sets the constant SERVED_BY = "anthropic", see
    # providers/anthropic.py) — carries an explicit None through, same as
    # absent.
    run = _base_run(
        model_used="local/small",
        provider_used="local",
        model_timeline=[
            {"model": "local/small", "provider": "local", "served_by": None}
        ],
    )
    rows = await build_outcomes(db, run)
    assert rows[0].served_by is None


@pytest.mark.asyncio
async def test_each_segment_of_a_model_switching_run_carries_its_own_served_by(db):
    run = _base_run(
        model_used="openrouter/big",
        provider_used="openrouter",
        model_timeline=[
            {
                "model": "local/small",
                "provider": "local",
                "served_by": None,
                "reason": "capability_stall",
                "from_iteration": 1,
                "to_iteration": 2,
            },
            {
                "model": "openrouter/big",
                "provider": "openrouter",
                "served_by": "deepinfra/fp8",
                "from_iteration": 3,
                "to_iteration": 5,
            },
        ],
    )
    rows = await build_outcomes(db, run)
    assert len(rows) == 2
    by_index = {row.segment_index: row for row in rows}
    assert by_index[0].model_id == "local/small"
    assert by_index[0].served_by is None
    assert by_index[1].model_id == "openrouter/big"
    assert by_index[1].served_by == "deepinfra/fp8"
