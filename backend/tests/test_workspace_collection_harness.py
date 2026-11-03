"""`services/workspace.py::ensure_collection_harness`: the seeded "Disclosure
Collector" harness. Real sqlite database, same fixtures as
`test_workspace_service.py`."""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from tests.evals.golden_world import install_sqlite_type_shims
from tret.config import get_settings
from tret.db.models import Base, Harness, Workspace
from tret.net import policy
from tret.packs.links import chat_harness_for_workspace
from tret.services.workspace import (
    COLLECTION_HARNESS_NAME,
    COLLECTION_HARNESS_TOOLS,
    ensure_collection_harness,
    seed_chat_harness,
    seed_workspace_content,
)


@pytest_asyncio.fixture
async def db():
    install_sqlite_type_shims()
    eng = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(eng, expire_on_commit=False)() as session:
        yield session
    await eng.dispose()


@pytest.fixture(autouse=True)
def _no_default_packs(monkeypatch):
    monkeypatch.setenv("TRET_SEED_DEFAULT_PACKS", "false")
    get_settings.cache_clear()
    policy.clear_all_runtime_overrides()
    yield
    policy.clear_all_runtime_overrides()
    get_settings.cache_clear()


def _research(monkeypatch, mode: str) -> None:
    monkeypatch.setenv("TRET_EGRESS", "on")
    monkeypatch.setenv("TRET_EGRESS_RESEARCH", mode)
    get_settings.cache_clear()


async def _workspace(db) -> Workspace:
    ws = Workspace(id=uuid.uuid4(), name="Acme", kind="team")
    db.add(ws)
    await db.flush()
    return ws


async def _collectors(db, workspace_id) -> list[Harness]:
    return list(
        (
            await db.execute(
                select(Harness).where(
                    Harness.workspace_id == workspace_id,
                    Harness.name == COLLECTION_HARNESS_NAME,
                )
            )
        ).scalars()
    )


async def test_seeded_when_research_is_on(db, monkeypatch):
    _research(monkeypatch, "on")
    ws = await _workspace(db)
    await seed_workspace_content(db, ws.id, uuid.uuid4())
    await db.flush()

    (h,) = await _collectors(db, ws.id)
    assert h.task_profile == "freeform"
    assert h.tool_names == COLLECTION_HARNESS_TOOLS
    assert h.model_policy == {"mode": "auto", "max_cost_tier": "standard"}
    assert h.loop_config["max_iterations"] == 24
    assert not h.is_archived

    # Idempotent: a second boot adds nothing.
    await ensure_collection_harness(db, ws.id)
    await db.flush()
    assert len(await _collectors(db, ws.id)) == 1


@pytest.mark.parametrize("mode", ["off", "replay"])
async def test_not_seeded_unless_research_is_fully_on(db, monkeypatch, mode):
    _research(monkeypatch, mode)
    ws = await _workspace(db)
    await seed_workspace_content(db, ws.id, uuid.uuid4())
    await db.flush()
    # `replay` serves snapshots and fetches nothing new; only `off` is gated
    # by the spec, so `replay` still seeds.
    assert len(await _collectors(db, ws.id)) == (1 if mode == "replay" else 0)


async def test_not_seeded_when_master_switch_is_off(db, monkeypatch):
    monkeypatch.setenv("TRET_EGRESS", "off")
    monkeypatch.setenv("TRET_EGRESS_RESEARCH", "on")
    get_settings.cache_clear()
    ws = await _workspace(db)
    await ensure_collection_harness(db, ws.id)
    await db.flush()
    assert await _collectors(db, ws.id) == []


async def test_backfills_missing_tools_on_existing_harness(db, monkeypatch):
    # Research is off now: an existing collector is still backfilled, only
    # creation is gated.
    _research(monkeypatch, "off")
    ws = await _workspace(db)
    existing = Harness(
        workspace_id=ws.id,
        name=COLLECTION_HARNESS_NAME,
        task_profile="freeform",
        model_policy={"mode": "auto"},
        tool_names=["web_search", "custom_operator_tool"],
    )
    db.add(existing)
    await db.flush()

    await ensure_collection_harness(db, ws.id)
    await db.flush()

    assert existing.tool_names[:2] == ["web_search", "custom_operator_tool"]
    assert set(existing.tool_names) == set(COLLECTION_HARNESS_TOOLS) | {"custom_operator_tool"}
    assert len(existing.tool_names) == len(set(existing.tool_names))
    assert len(await _collectors(db, ws.id)) == 1


async def test_chat_front_door_selection_is_unchanged(db, monkeypatch):
    _research(monkeypatch, "on")
    ws = await _workspace(db)
    # Collector first so it is also the oldest harness in the workspace.
    await ensure_collection_harness(db, ws.id)
    await db.flush()
    await seed_chat_harness(db, ws.id)
    await db.flush()

    chat = await chat_harness_for_workspace(db, ws.id)
    assert chat is not None and chat.name == "Chat Assistant"
    # The chat harness still never lists the web tools; the collector does.
    assert not {"web_search", "fetch_url", "capture_source"} & set(chat.tool_names)

    # Re-seeding the chat harness picks no collector tool up, and neither
    # harness is mistaken for the other.
    await seed_chat_harness(db, ws.id)
    await db.flush()
    again = await chat_harness_for_workspace(db, ws.id)
    assert again is not None and again.id == chat.id
    (collector,) = await _collectors(db, ws.id)
    assert collector.task_profile != "chat"
