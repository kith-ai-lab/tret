"""`delegate_parallel` (engine/tools.py): the parallel-batch delegation tool
built on the same `_prepare_child`/`_await_child`/`_record_delegated_cost`
split `test_delegation_phases.py` pins.

Reuses the sqlite fixture and setup helpers from
`test_run_harness_task_selection.py`, exactly as `test_delegation_phases.py`
does, plus the chat-activity-summary fixtures from `test_chat_api.py` and the
plain `db` session from that same module for the two small
`seed_chat_harness` backfill checks (a fresh, empty schema — no need for
`_setup`'s chat/specialist harness scaffolding there).
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from tret.config import get_settings
from tret.db.models import Harness, Run, Workspace
from tret.engine.tools import COST_CAP_KEY, DELEGATION_TOOLS, ToolError, delegate_parallel
from tret.services.workspace import seed_chat_harness
from tests.test_chat_api import _assistant_message, _run
from tests.test_run_harness_task_selection import _ctx, _setup, db  # noqa: F401 (fixture)


@pytest.fixture()
def _settings_override(monkeypatch):
    """Same pattern as test_delegation_phases.py: set TRET_ env vars and
    clear the module-level `get_settings` cache `tools.py` reads from."""

    def _set(**env: object) -> None:
        for key, value in env.items():
            monkeypatch.setenv(f"TRET_{key.upper()}", str(value))
        get_settings.cache_clear()

    yield _set
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _no_stagger(monkeypatch):
    """Zero the between-child start stagger AND its random jitter. With the
    jitter left in, three children whose fake work lasts 20ms started up to
    100ms apart and the overlap assertion failed about one run in five."""
    import tret.engine.tools as tools_module

    monkeypatch.setattr(tools_module, "FANOUT_STAGGER_SECONDS", 0)
    monkeypatch.setattr(tools_module, "FANOUT_JITTER_SECONDS", 0)


class _BatchEngine:
    """Fake `HarnessEngine` for `delegate_parallel`.

    `execute` looks the child run up by id (through its own session, like the
    real engine) and branches on `task_input["marker"]`: a marker in
    `raise_markers` fails after recording a partial spend; a marker in
    `hang_markers` blocks on an `asyncio.Event` until `cancel()` sets it (the
    cooperative-cancel contract `delegate_parallel`'s timeout path relies on),
    then finishes as `cancelled`; anything else completes normally. `events`
    and `max_seen`/`current` pin concurrency and lineage-bookkeeping order,
    matching `_ConcurrencyEngine` in test_delegation_phases.py.
    """

    def __init__(self, *, raise_markers=frozenset(), hang_markers=frozenset()):
        self.raise_markers = set(raise_markers)
        self.hang_markers = set(hang_markers)
        self.current = 0
        self.max_seen = 0
        self.events: list[tuple[str, uuid.UUID]] = []
        self.cancelled: set[uuid.UUID] = set()
        self._hang_events: dict[uuid.UUID, asyncio.Event] = {}

    def register_delegation(self, *, child_id, parent_id):
        self.events.append(("register", child_id))

    def unregister_delegation(self, child_id):
        self.events.append(("unregister", child_id))

    def cancel(self, run_id):
        self.cancelled.add(run_id)
        ev = self._hang_events.get(run_id)
        if ev is not None:
            ev.set()

    async def execute(self, run_id):
        from tret.db.engine import get_session_factory

        factory = get_session_factory()
        async with factory() as s:
            run = await s.get(Run, run_id)
            marker = (run.task_input or {}).get("marker")

        self.events.append(("execute_start", run_id))
        self.current += 1
        self.max_seen = max(self.max_seen, self.current)
        try:
            if marker in self.hang_markers:
                ev = asyncio.Event()
                self._hang_events[run_id] = ev
                await ev.wait()  # released by `cancel()` above
                async with factory() as s:
                    run = await s.get(Run, run_id)
                    run.status = "cancelled"
                    run.cost_usd = Decimal("0.02")
                    await s.commit()
                return
            await asyncio.sleep(0.02)
            if marker in self.raise_markers:
                async with factory() as s:
                    run = await s.get(Run, run_id)
                    run.status = "failed"
                    run.error = "boom"
                    run.cost_usd = Decimal("0.05")
                    await s.commit()
                raise RuntimeError("boom")
            async with factory() as s:
                run = await s.get(Run, run_id)
                run.status = "completed"
                run.cost_usd = Decimal("0.10")
                await s.commit()
        finally:
            self.current -= 1
            self.events.append(("execute_end", run_id))


def _tasks(n: int, **overrides) -> list[dict]:
    return [
        {"task_type": "assess_risk", "task_input": {"marker": i}, "label": f"task-{i}", **overrides}
        for i in range(n)
    ]


# ── happy path: concurrency, input order, lineage, budget split ─────────────


async def test_delegate_parallel_runs_concurrently_in_order_with_shared_lineage_and_equal_budget(
    db, monkeypatch  # noqa: F811
):
    import tret.engine.harness as harness_module

    workspace_id, project_id, parent_id, _specialist_id = await _setup(db)
    ctx = _ctx(db, workspace_id=workspace_id, project_id=project_id, run_id=parent_id)
    ctx.max_cost_usd = Decimal("3.0")  # remaining 3.0, split 3 ways = 1.0 each

    engine = _BatchEngine()
    monkeypatch.setattr(harness_module, "get_harness_engine", lambda: engine)

    result = json.loads(await delegate_parallel(ctx, _tasks(3)))

    # Ran concurrently, not one child fully finishing before the next starts.
    assert engine.max_seen >= 2
    assert [r["index"] for r in result["results"]] == [0, 1, 2]
    assert [r["label"] for r in result["results"]] == ["task-0", "task-1", "task-2"]
    assert all(r["status"] == "completed" for r in result["results"])

    batch_id = uuid.UUID(result["batch_id"])
    caps = set()
    for r in result["results"]:
        child = await db.get(Run, uuid.UUID(r["child_run_id"]))
        assert child.delegation_batch_id == batch_id
        assert child.parent_run_id == parent_id
        assert child.root_run_id == parent_id  # parent is itself a root
        caps.add(child.task_input[COST_CAP_KEY])
    assert caps == {"1.000000"}


# ── validation: all-or-nothing prepare phase ─────────────────────────────────


async def test_delegate_parallel_invalid_item_rolls_back_and_restores_children_started(
    db, monkeypatch  # noqa: F811
):
    import tret.engine.harness as harness_module

    workspace_id, project_id, parent_id, _specialist_id = await _setup(db)
    ctx = _ctx(db, workspace_id=workspace_id, project_id=project_id, run_id=parent_id)
    monkeypatch.setattr(harness_module, "get_harness_engine", lambda: _BatchEngine())

    before = (await db.execute(select(func.count()).select_from(Run))).scalar()
    tasks = [
        {"task_type": "assess_risk", "task_input": {}},
        {"task_type": "no-such-task", "task_input": {}},
        {"task_type": "assess_risk", "task_input": {}},
    ]

    with pytest.raises(ToolError) as excinfo:
        await delegate_parallel(ctx, tasks)
    assert "tasks[1]" in str(excinfo.value)

    # Nothing beyond a flush was ever committed; the engine's own rollback
    # (harness.py, right after a tool raises) is simulated here directly.
    await db.rollback()
    after = (await db.execute(select(func.count()).select_from(Run))).scalar()
    assert after == before
    assert ctx.children_started == 0


async def test_delegate_parallel_refuses_fewer_than_two_tasks(db):  # noqa: F811
    workspace_id, project_id, parent_id, _specialist_id = await _setup(db)
    ctx = _ctx(db, workspace_id=workspace_id, project_id=project_id, run_id=parent_id)

    with pytest.raises(ToolError) as excinfo:
        await delegate_parallel(ctx, _tasks(1))
    assert "run_harness_task" in str(excinfo.value)


async def test_delegate_parallel_refuses_more_than_max_fanout(
    db, _settings_override  # noqa: F811
):
    _settings_override(max_fanout=2)
    workspace_id, project_id, parent_id, _specialist_id = await _setup(db)
    ctx = _ctx(db, workspace_id=workspace_id, project_id=project_id, run_id=parent_id)

    with pytest.raises(ToolError) as excinfo:
        await delegate_parallel(ctx, _tasks(3))
    assert "at most 2" in str(excinfo.value)


async def test_delegate_parallel_thrift_objective_caps_width_at_two(
    db, _settings_override  # noqa: F811
):
    _settings_override(max_fanout=10)  # would otherwise allow all 3
    workspace_id, project_id, parent_id, _specialist_id = await _setup(db)
    ctx = _ctx(db, workspace_id=workspace_id, project_id=project_id, run_id=parent_id)
    ctx.objective = "eco"

    with pytest.raises(ToolError) as excinfo:
        await delegate_parallel(ctx, _tasks(3))
    assert "at most 2" in str(excinfo.value)
    assert "eco" in str(excinfo.value)


async def test_delegate_parallel_lifetime_cap_refuses_the_whole_batch(
    db, _settings_override  # noqa: F811
):
    _settings_override(max_children_per_run=2)
    workspace_id, project_id, parent_id, _specialist_id = await _setup(db)
    ctx = _ctx(db, workspace_id=workspace_id, project_id=project_id, run_id=parent_id)

    with pytest.raises(ToolError) as excinfo:
        await delegate_parallel(ctx, _tasks(3))
    assert "Delegation limit reached" in str(excinfo.value)
    assert ctx.children_started == 0


# ── per-child execution outcomes are independent ─────────────────────────────


async def test_delegate_parallel_isolates_one_childs_failure_and_still_costs_every_child(
    db, monkeypatch  # noqa: F811
):
    import tret.engine.harness as harness_module

    workspace_id, project_id, parent_id, _specialist_id = await _setup(db)
    ctx = _ctx(db, workspace_id=workspace_id, project_id=project_id, run_id=parent_id)
    engine = _BatchEngine(raise_markers={1})
    monkeypatch.setattr(harness_module, "get_harness_engine", lambda: engine)

    result = json.loads(await delegate_parallel(ctx, _tasks(3)))

    assert result["results"][0]["status"] == "completed"
    assert result["results"][1]["status"] == "error"
    assert result["results"][2]["status"] == "completed"
    assert "1 failed" in result["note"]

    await db.rollback()
    parent = await db.get(Run, parent_id)
    await db.refresh(parent)
    # 0.10 + 0.05 (spent before raising) + 0.10 — every child's cost lands
    # on the parent, failure included.
    assert parent.delegated_cost_usd == Decimal("0.25")


async def test_delegate_parallel_times_out_a_hanging_child_via_cooperative_cancel(
    db, monkeypatch, _settings_override  # noqa: F811
):
    _settings_override(delegation_timeout_seconds=1)
    import tret.engine.harness as harness_module

    workspace_id, project_id, parent_id, _specialist_id = await _setup(db)
    ctx = _ctx(db, workspace_id=workspace_id, project_id=project_id, run_id=parent_id)
    engine = _BatchEngine(hang_markers={1})
    monkeypatch.setattr(harness_module, "get_harness_engine", lambda: engine)

    result = json.loads(await delegate_parallel(ctx, _tasks(3)))

    assert result["results"][0]["status"] == "completed"
    assert result["results"][1].get("timed_out") is True
    assert result["results"][2]["status"] == "completed"
    assert engine.cancelled  # engine.cancel() reached the hanging child

    await db.rollback()
    parent = await db.get(Run, parent_id)
    await db.refresh(parent)
    assert parent.delegated_cost_usd == Decimal("0.22")  # 0.10 + 0.10 + 0.02


async def test_delegate_parallel_closes_out_a_child_that_ignores_the_cooperative_cancel(
    db, monkeypatch, _settings_override  # noqa: F811
):
    """A child that never notices `engine.cancel()` has its task torn down,
    which `execute()` cannot see — so the tool itself gives the row a terminal
    status instead of leaving it `running` until the next boot's orphan sweep."""
    import tret.engine.harness as harness_module
    import tret.engine.tools as tools_module

    _settings_override(delegation_timeout_seconds=1)
    monkeypatch.setattr(tools_module, "FANOUT_CANCEL_GRACE_SECONDS", 0.05)

    class _DeafEngine(_BatchEngine):
        def __init__(self):
            super().__init__(hang_markers={1})
            self.forgotten: set[uuid.UUID] = set()

        def cancel(self, run_id):
            self.cancelled.add(run_id)  # noted, but the hang is never released

        def forget_cancelled(self, run_id):
            self.forgotten.add(run_id)

        async def execute(self, run_id):
            from tret.db.engine import get_session_factory

            async with get_session_factory()() as s:
                run = await s.get(Run, run_id)
                if (run.task_input or {}).get("marker") in self.hang_markers:
                    run.status = "running"
                    await s.commit()
            await super().execute(run_id)

    workspace_id, project_id, parent_id, _specialist_id = await _setup(db)
    ctx = _ctx(db, workspace_id=workspace_id, project_id=project_id, run_id=parent_id)
    engine = _DeafEngine()
    monkeypatch.setattr(harness_module, "get_harness_engine", lambda: engine)

    result = json.loads(await delegate_parallel(ctx, _tasks(2)))

    stuck = result["results"][1]
    assert stuck["status"] == "error" and "timeout" in stuck["error"]
    child_id = uuid.UUID(stuck["child_run_id"])
    assert child_id in engine.cancelled and child_id in engine.forgotten

    await db.rollback()
    child = await db.get(Run, child_id)
    await db.refresh(child)
    assert child.status == "cancelled"
    assert child.error.startswith("delegation_timeout")
    assert child.finished_at is not None


async def test_delegate_parallel_malformed_label_does_not_burn_the_children_allowance(
    db, monkeypatch  # noqa: F811
):
    import tret.engine.harness as harness_module

    workspace_id, project_id, parent_id, _specialist_id = await _setup(db)
    ctx = _ctx(db, workspace_id=workspace_id, project_id=project_id, run_id=parent_id)
    monkeypatch.setattr(harness_module, "get_harness_engine", lambda: _BatchEngine())

    with pytest.raises(ToolError):
        await delegate_parallel(ctx, _tasks(2, label=5))
    assert ctx.children_started == 0


async def test_delegate_parallel_restores_children_started_when_the_commit_fails(
    db, monkeypatch  # noqa: F811
):
    import tret.engine.harness as harness_module

    workspace_id, project_id, parent_id, _specialist_id = await _setup(db)
    ctx = _ctx(db, workspace_id=workspace_id, project_id=project_id, run_id=parent_id)
    monkeypatch.setattr(harness_module, "get_harness_engine", lambda: _BatchEngine())

    async def _boom():
        raise RuntimeError("commit failed")

    monkeypatch.setattr(db, "commit", _boom)
    with pytest.raises(RuntimeError):
        await delegate_parallel(ctx, _tasks(2))
    assert ctx.children_started == 0


def test_fit_batch_results_trims_largest_payloads_until_the_batch_fits():
    from tret.engine.tools import _fit_batch_results

    def finding(fid: str, size: int) -> dict:
        return {"finding_id": fid, "payload": {"text": "x" * size}}

    results = [
        {"index": 0, "findings": [finding("a", 4000), finding("b", 300)]},
        {"index": 1, "findings": [finding("c", 3000), finding("d", 200)]},
    ]
    _fit_batch_results(results, 2500)

    assert len(json.dumps(results).encode()) <= 2500
    # Largest first, and only as many as it takes: both children are still
    # present, and the small findings survive intact.
    assert results[0]["findings"][0]["payload"] == {"truncated": True, "finding_id": "a"}
    assert results[1]["findings"][0]["payload"] == {"truncated": True, "finding_id": "c"}
    assert results[0]["findings"][1]["payload"] == {"text": "x" * 300}
    assert results[1]["findings"][1]["payload"] == {"text": "x" * 200}


def test_fit_batch_results_gives_up_cleanly_when_nothing_is_left_to_trim():
    from tret.engine.tools import _fit_batch_results

    results = [{"index": 0, "error": "e" * 5000, "findings": []}]
    _fit_batch_results(results, 100)  # must terminate
    assert results[0]["findings"] == []


# ── chat activity summary ────────────────────────────────────────────────────


def test_delegate_parallel_tool_is_a_delegation_tool():
    assert "delegate_parallel" in DELEGATION_TOOLS


def test_chat_activity_summary_for_delegate_parallel_lists_task_types_deduplicated():
    run = _run(
        messages=[
            {
                "role": "assistant",
                "content": "done",
                "tool_calls": [
                    {
                        "name": "delegate_parallel",
                        "arguments": {
                            "tasks": [
                                {"task_type": "assess_risk", "task_input": {}},
                                {"task_type": "assess_risk", "task_input": {}},
                                {"task_type": "evidence_extraction", "task_input": {}},
                            ]
                        },
                    }
                ],
            }
        ]
    )
    message = _assistant_message(run)
    assert message["activity"] == [
        {
            "tool": "delegate_parallel",
            "summary": "delegated 3 tasks in parallel: assess_risk, evidence_extraction",
        }
    ]


def test_chat_activity_summary_for_delegate_parallel_caps_the_listed_types_at_four():
    types = [f"type_{i}" for i in range(6)]
    run = _run(
        messages=[
            {
                "role": "assistant",
                "content": "done",
                "tool_calls": [
                    {
                        "name": "delegate_parallel",
                        "arguments": {
                            "tasks": [{"task_type": t, "task_input": {}} for t in types]
                        },
                    }
                ],
            }
        ]
    )
    message = _assistant_message(run)
    summary = message["activity"][0]["summary"]
    assert summary.startswith("delegated 6 tasks in parallel: ")
    assert summary.endswith("…")
    assert "type_4" not in summary and "type_5" not in summary


# ── workspace backfill: only alongside run_harness_task ──────────────────────


async def test_seed_chat_harness_backfills_delegate_parallel_when_run_harness_task_present(
    db,  # noqa: F811
):
    workspace = Workspace(id=uuid.uuid4(), name="Acme", kind="team")
    db.add(workspace)
    await db.flush()
    chat_harness = Harness(
        workspace_id=workspace.id,
        name="Chat Assistant",
        task_profile="chat",
        model_policy={"mode": "auto"},
        tool_names=["run_harness_task"],
        packs_linked_at=datetime.now(timezone.utc),  # skip the unrelated pack-link backfill
    )
    db.add(chat_harness)
    await db.commit()

    await seed_chat_harness(db, workspace.id)

    assert "delegate_parallel" in chat_harness.tool_names


async def test_seed_chat_harness_leaves_delegate_parallel_out_without_run_harness_task(
    db,  # noqa: F811
):
    workspace = Workspace(id=uuid.uuid4(), name="Acme", kind="team")
    db.add(workspace)
    await db.flush()
    chat_harness = Harness(
        workspace_id=workspace.id,
        name="Chat Assistant",
        task_profile="chat",
        model_policy={"mode": "auto"},
        tool_names=[],  # operator removed run_harness_task (or never had it)
        packs_linked_at=datetime.now(timezone.utc),
    )
    db.add(chat_harness)
    await db.commit()

    await seed_chat_harness(db, workspace.id)

    assert "delegate_parallel" not in chat_harness.tool_names
