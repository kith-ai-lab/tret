"""`engine/tools.py::run_harness_task`'s harness-candidate selection.

The chat front door (`task_profile == "chat"`) now links every installed
pack by default (`services.workspace._seed_chat_harness`), which means the
harness query `run_harness_task` builds its delegation candidates from could
land on the chat harness itself unless it is filtered out — see F2 in the
review this file pins. A real sqlite database (not a hand-rolled fake
session) because what's under test is the query shape itself; `db.engine`'s
module globals are swapped the same way `tests/test_connected_tools.py`
does it, since `run_harness_task` reads the child run back through
`get_session_factory()` in a fresh session.
"""
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import ARRAY
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from tret.db.models import Base, Harness, Pack, Project, Run, Workspace
from tret.engine.tools import RunContext, ToolError, run_harness_task
from tret.packs.links import set_harness_packs


@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(type_, compiler, **kw):  # pragma: no cover - DDL only
    return "JSON"


@compiles(ARRAY, "sqlite")
def _array_on_sqlite(type_, compiler, **kw):  # pragma: no cover - DDL only
    return "JSON"


class _FakeEngine:
    """Stands in for `HarnessEngine`: `execute` never actually runs the
    child — its `Run` row is left exactly as `run_harness_task` created it
    (`status="queued"`), which is all this file's assertions need — and
    `register_delegation`/`unregister_delegation` are no-ops, matched to the
    real engine's own lineage-bookkeeping methods `run_harness_task` calls
    unconditionally around `execute`."""

    async def execute(self, run_id):
        return None

    def register_delegation(self, *, child_id, parent_id):
        pass

    def unregister_delegation(self, child_id):
        pass


@pytest.fixture()
async def db(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'delegation.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    import tret.db.engine as db_engine

    saved = (db_engine._engine, db_engine._session_factory)
    db_engine._engine, db_engine._session_factory = engine, factory

    import tret.engine.harness as harness_module

    monkeypatch.setattr(harness_module, "get_harness_engine", lambda: _FakeEngine())

    async with factory() as session:
        yield session

    db_engine._engine, db_engine._session_factory = saved
    await engine.dispose()


def _pack(workspace_id, *, slug: str, task_slug: str) -> Pack:
    return Pack(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        slug=slug,
        version="1.0.0",
        doctrine_sha="deadbeef",
        manifest={
            "pack": slug,
            "version": "1.0.0",
            "display_name": slug.title(),
            "task_types": [{"slug": task_slug, "display_name": task_slug.title()}],
        },
        source_path=f"/tmp/{slug}",
    )


async def _setup(db) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """A workspace with a chat harness and a specialist harness, both linked
    to the same pack (the chat harness's default-link-everything behaviour).
    Returns (workspace_id, project_id, chat_harness_id, specialist_id, and
    the parent run's own id, all bound to the chat harness — a chat turn
    delegating out is exactly the scenario F2 is about)."""
    workspace = Workspace(name="W")
    db.add(workspace)
    await db.flush()
    project = Project(workspace_id=workspace.id, name="P")
    db.add(project)
    await db.flush()

    pack = _pack(workspace.id, slug="climate-risk", task_slug="assess_risk")
    db.add(pack)
    await db.flush()

    chat_harness = Harness(
        workspace_id=workspace.id,
        name="Chat Assistant",
        task_profile="chat",
        model_policy={"mode": "auto"},
        tool_names=["run_harness_task"],
    )
    specialist = Harness(
        workspace_id=workspace.id,
        name="Climate Analyst",
        task_profile="assess_risk",
        model_policy={"mode": "auto"},
        tool_names=[],
    )
    db.add_all([chat_harness, specialist])
    await db.flush()
    await set_harness_packs(db, chat_harness, [pack.id])
    await set_harness_packs(db, specialist, [pack.id])

    parent = Run(
        project_id=project.id,
        harness_id=chat_harness.id,
        task_type="chat",
        task_input={},
    )
    db.add(parent)
    await db.commit()

    return workspace.id, project.id, parent.id, specialist.id


def _ctx(db, *, workspace_id, project_id, run_id) -> RunContext:
    return RunContext(
        db=db,
        run_id=run_id,
        project_id=project_id,
        pack_id=None,
        doctrine_sha=None,
        model_used=None,
        document_ids=[],
        output_schemas={},
        workspace_id=workspace_id,
    )


async def test_delegation_picks_the_specialist_harness_not_the_chat_front_door(db):
    """Both harnesses declare `assess_risk` (the chat harness only because
    it auto-links every pack) — the child run must land on the specialist,
    never on chat."""
    workspace_id, project_id, parent_id, specialist_id = await _setup(db)
    ctx = _ctx(db, workspace_id=workspace_id, project_id=project_id, run_id=parent_id)

    result = json.loads(await run_harness_task(ctx, "assess_risk", {}))

    child = await db.get(Run, uuid.UUID(result["child_run_id"]))
    assert child.harness_id == specialist_id


async def test_naming_the_chat_harness_explicitly_raises_tool_error(db):
    """`harness_name="Chat Assistant"` must not resolve at all — the chat
    harness is filtered out of the candidate pool before the name filter
    ever runs, so this is the same "no harness supports" error as naming any
    other nonexistent harness."""
    workspace_id, project_id, parent_id, _specialist_id = await _setup(db)
    ctx = _ctx(db, workspace_id=workspace_id, project_id=project_id, run_id=parent_id)

    with pytest.raises(ToolError) as excinfo:
        await run_harness_task(ctx, "assess_risk", {}, harness_name="Chat Assistant")
    assert "No harness supports" in str(excinfo.value)
