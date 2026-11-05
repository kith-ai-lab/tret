"""`report_collection_outcome` (engine/collection_tools.py): how a collection
target's run scores itself. Real sqlite database."""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from tests.evals.golden_world import install_sqlite_type_shims
from tret.db.models import Base, Collection, CollectionTarget, Harness, Project, Run, Workspace
from tret.engine.collection_tools import report_collection_outcome
from tret.engine.tools import RunContext, ToolError, execute_tool, get_builtin_tools
from tret.services.workspace import COLLECTION_HARNESS_TOOLS


@pytest_asyncio.fixture
async def world():
    install_sqlite_type_shims()
    eng = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(eng, expire_on_commit=False)() as db:
        ws = Workspace(id=uuid.uuid4(), name="W")
        other = Workspace(id=uuid.uuid4(), name="O")
        db.add_all([ws, other])
        await db.flush()
        project = Project(id=uuid.uuid4(), workspace_id=ws.id, name="P")
        harness = Harness(id=uuid.uuid4(), workspace_id=ws.id, name="H")
        db.add_all([project, harness])
        await db.flush()
        c = Collection(id=uuid.uuid4(), workspace_id=ws.id, project_id=project.id, name="C",
                       run_slug="run-one", status="running", settings={}, warnings=[])
        db.add(c)
        await db.flush()
        run = Run(id=uuid.uuid4(), project_id=project.id, harness_id=harness.id, task_type="freeform",
                  task_input={}, document_ids=[])
        db.add(run)
        await db.flush()
        t = CollectionTarget(id=uuid.uuid4(), collection_id=c.id, position=1, slug="allianz",
                             company="Allianz SE", market="EU", fiscal_year=2025, run_id=run.id,
                             known_urls=[], finding_ids=[], written_files=[])
        db.add(t)
        run.task_input = {"_collection_target_id": str(t.id), "_collection_id": str(c.id)}
        await db.commit()
        ctx = RunContext(db=db, run_id=run.id, project_id=project.id, pack_id=None, doctrine_sha=None,
                         model_used="m", document_ids=[], output_schemas={}, workspace_id=ws.id)
        yield type("W", (), {"db": db, "ctx": ctx, "run": run, "target": t, "collection": c, "other": other})
    await eng.dispose()


def test_registered_and_on_the_collector_harness():
    spec = get_builtin_tools()["report_collection_outcome"]
    assert spec.parameters["required"] == ["status", "summary"]
    assert spec.parameters["properties"]["status"]["enum"] == ["captured", "substituted", "gap", "blocked"]
    assert "report_collection_outcome" in COLLECTION_HARNESS_TOOLS


async def test_records_the_outcome_and_a_second_call_overwrites(world):
    out = await report_collection_outcome(
        world.ctx, status="captured", summary=" Found the FY2025 statement. ",
        reporting_period_label="FY2025", period_start="2025-01-01", period_end="2025-12-31",
        framework="ESRS", fy2025_gap=False, blocked_urls=["https://x.example/a"], language="",
    )
    assert out == "Outcome recorded for allianz: captured."
    await world.db.refresh(world.target)
    o = world.target.outcome
    assert o["status"] == "captured" and o["summary"] == "Found the FY2025 statement."
    assert o["framework"] == "ESRS" and o["language"] is None and o["fy2025_gap"] is False
    assert o["blocked_urls"] == ["https://x.example/a"] and o["candidate_urls"] == []
    assert o["reported_at"]
    await report_collection_outcome(world.ctx, status="gap", summary="actually none")
    await world.db.refresh(world.target)
    assert world.target.outcome["status"] == "gap"
    assert world.target.outcome["framework"] is None


async def test_refuses_outside_a_collection(world):
    world.run.task_input = {}
    await world.db.commit()
    with pytest.raises(ToolError, match="not part of a collection"):
        await report_collection_outcome(world.ctx, status="gap", summary="x")


async def test_refuses_a_target_of_another_workspace(world):
    world.collection.workspace_id = world.other.id
    await world.db.commit()
    with pytest.raises(ToolError, match="not found"):
        await report_collection_outcome(world.ctx, status="gap", summary="x")
    assert world.target.outcome is None


async def test_refuses_a_target_now_owned_by_another_run(world):
    world.target.run_id = None
    await world.db.commit()
    with pytest.raises(ToolError, match="not found"):
        await report_collection_outcome(world.ctx, status="gap", summary="x")


async def test_bad_arguments_are_tool_errors(world):
    spec = get_builtin_tools()["report_collection_outcome"]
    text, is_error = await execute_tool(world.ctx, spec, {"status": "found", "summary": "x"})
    assert is_error and "status must be one of" in text
    text, is_error = await execute_tool(world.ctx, spec, {"status": "gap", "summary": "x" * 2001})
    assert is_error and "2000" in text
    text, is_error = await execute_tool(
        world.ctx, spec, {"status": "gap", "summary": "x", "candidate_urls": ["u"] * 21}
    )
    assert is_error and "at most 20" in text
