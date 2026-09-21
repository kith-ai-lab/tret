"""`spawn_subagent` and `delegate_parallel`'s `kind: "subagent"` items
(engine/tools.py) — the ad-hoc-brief delegation path built on the same
`_prepare_child`/`_await_child`/`_record_delegated_cost` split
`test_delegation_phases.py` pins, plus `_subagent_task_input`,
`_summarize_child`'s subagent branch, and `_adopt_retrieved_values`.

Reuses the sqlite `db` fixture and `_ctx`/`_setup` helpers from
`test_run_harness_task_selection.py` (same pattern `test_subagent_runs.py`
and `test_delegate_parallel.py` use), and the settings-override /
zero-stagger fixtures from `test_delegate_parallel.py`.
"""
from __future__ import annotations

import json
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

import tret.engine.tools as tools_module
from tret.db.models import Harness, Run, Workspace
from tret.engine.delegation import (
    ALLOWED_TOOLS_KEY,
    DELEGATION_TOOLS,
    PROJECT_DOCS_KEY,
    SUBAGENT_ALLOWED_TOOLS,
    SUBAGENT_TASK_PROFILE,
    SUBAGENT_TASK_TYPE,
)
from tret.engine.tools import (
    SUBAGENT_REPORT_MAX_CHARS,
    ToolError,
    _subagent_task_input,
    delegate_parallel,
    spawn_subagent,
)
from tret.services.workspace import seed_chat_harness, seed_subagent_harness
from tests.test_chat_api import _assistant_message
from tests.test_run_harness_task_selection import _ctx, _setup, db  # noqa: F401 (fixture)

# A grant that mixes ordinary read tools with a write tool (record_verdict)
# and a delegation tool (run_harness_task) — every test that sets
# `ctx.enabled_tools` to this is checking that the write/delegation tools
# never survive the SUBAGENT_ALLOWED_TOOLS intersection no matter what the
# parent run itself was offered.
STANDARD_TOOLS = frozenset(
    {"read_document", "search_documents", "lookup_dataset", "run_harness_task", "record_verdict"}
)


@pytest.fixture(autouse=True)
def _no_stagger(monkeypatch):
    monkeypatch.setattr(tools_module, "FANOUT_STAGGER_SECONDS", 0)
    monkeypatch.setattr(tools_module, "FANOUT_JITTER_SECONDS", 0)


class _SubagentEngine:
    """Fake `HarnessEngine` for the subagent path: `execute` writes a
    terminal status, a cost, and (unless `empty_output`) an assistant report
    message straight onto the child through its own session — mirroring
    `_BatchEngine`/`_FakeEngine` in the sibling delegation test files.
    `take_retrieved_values` pops whatever `retrieved` was seeded with on its
    FIRST call, regardless of run_id, and `[]` on every call after — the same
    one-shot-pop contract the real handoff makes."""

    def __init__(self, *, report="REPORT: nothing further to check.", cost=Decimal("0.02"),
                 grounding=None, retrieved=None, empty_output=False):
        self.report = report
        self.cost = cost
        self.grounding = grounding
        self.empty_output = empty_output
        self._pending_values = list(retrieved or [])

    def register_delegation(self, *, child_id, parent_id):
        pass

    def unregister_delegation(self, child_id):
        pass

    async def execute(self, run_id):
        from tret.db.engine import get_session_factory

        async with get_session_factory()() as s:
            run = await s.get(Run, run_id)
            run.status = "completed_without_output" if self.empty_output else "completed"
            run.cost_usd = self.cost
            if not self.empty_output:
                run.messages = [
                    {"role": "user", "content": "Brief: ..."},
                    {"role": "assistant", "content": self.report},
                ]
            if self.grounding is not None:
                run.grounding = self.grounding
            await s.commit()

    def take_retrieved_values(self, run_id):
        vals, self._pending_values = self._pending_values, []
        return vals


async def _seeded_ctx(db, monkeypatch, engine, *, enabled_tools=STANDARD_TOOLS, pack_id=None):  # noqa: F811
    """A workspace with the usual chat/specialist scaffolding PLUS a seeded
    Subagent harness, a RunContext bound to the chat run, and `engine`
    installed as what `get_harness_engine()` returns everywhere it's looked
    up lazily (both `_await_child` and `spawn_subagent`/`delegate_parallel`
    themselves import it that way)."""
    workspace_id, project_id, parent_id, _specialist_id = await _setup(db)
    await seed_subagent_harness(db, workspace_id)
    await db.commit()
    import tret.engine.harness as harness_module

    monkeypatch.setattr(harness_module, "get_harness_engine", lambda: engine)
    ctx = _ctx(db, workspace_id=workspace_id, project_id=project_id, run_id=parent_id)
    ctx.enabled_tools = enabled_tools
    ctx.pack_id = pack_id
    return ctx, workspace_id


LONG_ENOUGH = "Look up Acme Corp's Q3 emissions in the attached filing and report the figure."


# ── _subagent_task_input: pure validation (no child run involved) ───────────


async def test_subagent_task_input_grants_only_the_read_only_intersection(db):  # noqa: F811
    _workspace_id, project_id, parent_id, _s = await _setup(db)
    ctx = _ctx(db, workspace_id=_workspace_id, project_id=project_id, run_id=parent_id)
    ctx.enabled_tools = STANDARD_TOOLS

    task_input, _doc_ids = await _subagent_task_input(ctx, {"instructions": LONG_ENOUGH})

    assert task_input[ALLOWED_TOOLS_KEY] == sorted(STANDARD_TOOLS & SUBAGENT_ALLOWED_TOOLS)
    assert "run_harness_task" not in task_input[ALLOWED_TOOLS_KEY]
    assert "record_verdict" not in task_input[ALLOWED_TOOLS_KEY]
    assert task_input["instructions"] == LONG_ENOUGH


async def test_subagent_task_input_does_not_copy_stray_model_supplied_keys(db):  # noqa: F811
    _workspace_id, project_id, parent_id, _s = await _setup(db)
    ctx = _ctx(db, workspace_id=_workspace_id, project_id=project_id, run_id=parent_id)
    ctx.enabled_tools = STANDARD_TOOLS

    task_input, _doc_ids = await _subagent_task_input(
        ctx, {"instructions": LONG_ENOUGH, "_evil": "smuggled", "label": "ignored-here-too"}
    )

    assert "_evil" not in task_input
    assert "label" not in task_input


async def test_subagent_task_input_refuses_a_tool_outside_the_grant(db):  # noqa: F811
    _workspace_id, project_id, parent_id, _s = await _setup(db)
    ctx = _ctx(db, workspace_id=_workspace_id, project_id=project_id, run_id=parent_id)
    ctx.enabled_tools = frozenset({"read_document"})

    with pytest.raises(ToolError) as excinfo:
        await _subagent_task_input(ctx, {"instructions": LONG_ENOUGH, "tools": ["lookup_dataset"]})
    assert "lookup_dataset" in str(excinfo.value)
    assert "read_document" in str(excinfo.value)  # what IS available


async def test_subagent_task_input_document_ids_outside_scope_raises(db, monkeypatch):  # noqa: F811
    _workspace_id, project_id, parent_id, _s = await _setup(db)
    ctx = _ctx(db, workspace_id=_workspace_id, project_id=project_id, run_id=parent_id)
    ctx.enabled_tools = STANDARD_TOOLS
    visible_id = uuid.uuid4()

    async def _fake_scope(_ctx):
        return [visible_id], False

    monkeypatch.setattr(tools_module, "_document_scope", _fake_scope)
    outside_id = str(uuid.uuid4())

    with pytest.raises(ToolError, match="not visible"):
        await _subagent_task_input(ctx, {"instructions": LONG_ENOUGH, "document_ids": [outside_id]})


async def test_subagent_task_input_inherits_attached_only_scope(db, monkeypatch):  # noqa: F811
    _workspace_id, project_id, parent_id, _s = await _setup(db)
    ctx = _ctx(db, workspace_id=_workspace_id, project_id=project_id, run_id=parent_id)
    ctx.enabled_tools = STANDARD_TOOLS
    id1, id2 = uuid.uuid4(), uuid.uuid4()

    async def _fake_scope(_ctx):
        return [id1, id2], False

    monkeypatch.setattr(tools_module, "_document_scope", _fake_scope)

    task_input, document_ids = await _subagent_task_input(ctx, {"instructions": LONG_ENOUGH})

    assert document_ids == [id1, id2]
    assert PROJECT_DOCS_KEY not in task_input


async def test_subagent_task_input_inherits_project_wide_scope(db, monkeypatch):  # noqa: F811
    _workspace_id, project_id, parent_id, _s = await _setup(db)
    ctx = _ctx(db, workspace_id=_workspace_id, project_id=project_id, run_id=parent_id)
    ctx.enabled_tools = STANDARD_TOOLS

    async def _fake_scope(_ctx):
        return [], True

    monkeypatch.setattr(tools_module, "_document_scope", _fake_scope)

    task_input, document_ids = await _subagent_task_input(ctx, {"instructions": LONG_ENOUGH})

    assert document_ids is None
    assert task_input[PROJECT_DOCS_KEY] is True


async def test_subagent_task_input_effort_light_stamps_objective(db):  # noqa: F811
    _workspace_id, project_id, parent_id, _s = await _setup(db)
    ctx = _ctx(db, workspace_id=_workspace_id, project_id=project_id, run_id=parent_id)
    ctx.enabled_tools = STANDARD_TOOLS

    task_input, _ = await _subagent_task_input(ctx, {"instructions": LONG_ENOUGH, "effort": "light"})
    assert task_input["_objective"] == "token_conservation"

    task_input2, _ = await _subagent_task_input(ctx, {"instructions": LONG_ENOUGH, "effort": "standard"})
    assert "_objective" not in task_input2


async def test_subagent_task_input_refuses_too_short_or_too_long_instructions(db):  # noqa: F811
    _workspace_id, project_id, parent_id, _s = await _setup(db)
    ctx = _ctx(db, workspace_id=_workspace_id, project_id=project_id, run_id=parent_id)
    ctx.enabled_tools = STANDARD_TOOLS

    with pytest.raises(ToolError, match="too short"):
        await _subagent_task_input(ctx, {"instructions": "too short"})
    with pytest.raises(ToolError, match="too long"):
        await _subagent_task_input(ctx, {"instructions": "x" * 6001})


# ── spawn_subagent: end to end against a fake engine ─────────────────────────


async def test_spawn_subagent_no_subagent_harness_raises(db):  # noqa: F811
    workspace_id, project_id, parent_id, _s = await _setup(db)
    ctx = _ctx(db, workspace_id=workspace_id, project_id=project_id, run_id=parent_id)
    ctx.enabled_tools = STANDARD_TOOLS

    with pytest.raises(ToolError, match="no Subagent harness"):
        await spawn_subagent(ctx, instructions=LONG_ENOUGH)


async def test_spawn_subagent_happy_path(db, monkeypatch):  # noqa: F811
    pack_id = uuid.uuid4()
    ctx, workspace_id = await _seeded_ctx(
        db, monkeypatch, _SubagentEngine(report="REPORT: Acme emitted 42 tCO2e in Q3."), pack_id=pack_id
    )

    result = json.loads(
        await spawn_subagent(
            ctx,
            instructions=LONG_ENOUGH,
            context="Acme's CIK is 123.",
            expected_output="One number with its source.",
            label="acme lookup",
        )
    )

    child = await db.get(Run, uuid.UUID(result["child_run_id"]))
    subagent_harness = (
        await db.execute(
            select(Harness).where(
                Harness.workspace_id == workspace_id, Harness.task_profile == SUBAGENT_TASK_PROFILE
            )
        )
    ).scalars().first()

    assert child.task_type == SUBAGENT_TASK_TYPE
    assert child.delegation_kind == "subagent"
    assert child.harness_id == subagent_harness.id
    assert child.parent_run_id == ctx.run_id
    assert child.root_run_id == ctx.run_id
    assert child.pack_id == pack_id
    assert child.task_input[ALLOWED_TOOLS_KEY] == sorted(STANDARD_TOOLS & SUBAGENT_ALLOWED_TOOLS)
    assert child.task_input["instructions"] == LONG_ENOUGH
    assert child.task_input["context"] == "Acme's CIK is 123."
    assert child.task_input["expected_output"] == "One number with its source."

    assert "findings" not in result
    assert result["output"] == "REPORT: Acme emitted 42 tCO2e in Q3."
    assert "evidence to weigh" in result["note"]
    assert "recorded no findings" in result["note"]


async def test_spawn_subagent_report_capped_and_truncated(db, monkeypatch):  # noqa: F811
    long_report = "x" * (SUBAGENT_REPORT_MAX_CHARS + 500)
    ctx, _ws = await _seeded_ctx(db, monkeypatch, _SubagentEngine(report=long_report))

    result = json.loads(await spawn_subagent(ctx, instructions=LONG_ENOUGH))

    assert len(result["output"]) == SUBAGENT_REPORT_MAX_CHARS
    assert result["output_truncated"] is True


async def test_spawn_subagent_completed_without_output_says_so_plainly(db, monkeypatch):  # noqa: F811
    ctx, _ws = await _seeded_ctx(db, monkeypatch, _SubagentEngine(empty_output=True))

    result = json.loads(await spawn_subagent(ctx, instructions=LONG_ENOUGH))

    assert result["output"] == ""
    assert "finished without writing a report" in result["note"]


async def test_spawn_subagent_unresolved_grounding_adds_warning(db, monkeypatch):  # noqa: F811
    grounding = {"checked": True, "status": "unresolved", "unsupported": ["42 tCO2e"], "attempts": 2}
    ctx, _ws = await _seeded_ctx(db, monkeypatch, _SubagentEngine(grounding=grounding))

    result = json.loads(await spawn_subagent(ctx, instructions=LONG_ENOUGH))

    assert result["grounding"] == {"status": "unresolved", "unsupported": ["42 tCO2e"]}
    assert "did not resolve" in result["note"]
    assert "do not repeat them as fact" in result["note"]


async def test_spawn_subagent_clean_grounding_adds_no_warning(db, monkeypatch):  # noqa: F811
    grounding = {"checked": True, "status": "clean", "unsupported": [], "attempts": 0}
    ctx, _ws = await _seeded_ctx(db, monkeypatch, _SubagentEngine(grounding=grounding))

    result = json.loads(await spawn_subagent(ctx, instructions=LONG_ENOUGH))

    assert result["grounding"] == {"status": "clean", "unsupported": []}
    assert "did not resolve" not in result["note"]


async def test_spawn_subagent_repaired_grounding_adds_no_warning(db, monkeypatch):  # noqa: F811
    """'repaired' means the child's own rewrite cleared what the check flagged;
    `unsupported` is empty, so a warning would point the parent at nothing."""
    grounding = {"checked": True, "status": "repaired", "unsupported": [], "attempts": 1}
    ctx, _ws = await _seeded_ctx(db, monkeypatch, _SubagentEngine(grounding=grounding))

    result = json.loads(await spawn_subagent(ctx, instructions=LONG_ENOUGH))

    assert "did not resolve" not in result["note"]
    assert "unverified" not in result["note"]


async def test_spawn_subagent_that_retrieved_nothing_is_flagged_unverified(db, monkeypatch):  # noqa: F811
    """The parent's grounding check accepts any tool result as evidence, this
    report included — so a child that never used a tool must say so, or a figure
    it made up becomes 'retrieved' one hop later."""
    grounding = {"checked": False, "status": "skipped"}
    ctx, _ws = await _seeded_ctx(db, monkeypatch, _SubagentEngine(grounding=grounding))

    result = json.loads(await spawn_subagent(ctx, instructions=LONG_ENOUGH))

    assert "unverified" in result["note"]


async def test_spawn_subagent_adopts_retrieved_values_with_via_run_id_once(db, monkeypatch):  # noqa: F811
    seeded = [{"dataset": "emissions", "row_ref": "r1", "column": "co2e", "value": "42"}]
    engine = _SubagentEngine(retrieved=seeded)
    ctx, _ws = await _seeded_ctx(db, monkeypatch, engine)

    result = json.loads(await spawn_subagent(ctx, instructions=LONG_ENOUGH))

    assert len(ctx.retrieved_values) == 1
    assert ctx.retrieved_values[0]["via_run_id"] == result["child_run_id"]
    assert ctx.retrieved_values[0]["value"] == "42"
    # One-shot pop: nothing left for a second take.
    assert engine.take_retrieved_values(uuid.uuid4()) == []


async def test_spawn_subagent_records_cost_on_parent(db, monkeypatch):  # noqa: F811
    ctx, _ws = await _seeded_ctx(db, monkeypatch, _SubagentEngine(cost=Decimal("0.30")))

    await spawn_subagent(ctx, instructions=LONG_ENOUGH)

    parent = await db.get(Run, ctx.run_id)
    assert parent.delegated_cost_usd == Decimal("0.30")


# ── delegate_parallel: a task item and a subagent item in one batch ─────────


async def test_delegate_parallel_mixes_task_and_subagent_items(db, monkeypatch):  # noqa: F811
    ctx, _ws = await _seeded_ctx(db, monkeypatch, _SubagentEngine(report="REPORT: side lookup done."))

    result = json.loads(
        await delegate_parallel(
            ctx,
            [
                {"task_type": "assess_risk", "task_input": {}, "label": "task-item"},
                {"kind": "subagent", "instructions": LONG_ENOUGH, "label": "subagent-item"},
            ],
        )
    )

    assert len(result["results"]) == 2
    task_result, subagent_result = result["results"]
    assert task_result["index"] == 0
    assert task_result["task_type"] == "assess_risk"
    assert "findings" in task_result
    assert subagent_result["index"] == 1
    assert subagent_result["task_type"] == "subagent"
    assert "findings" not in subagent_result
    assert subagent_result["output"] == "REPORT: side lookup done."
    # Both children share one batch.
    child_a = await db.get(Run, uuid.UUID(task_result["child_run_id"]))
    child_b = await db.get(Run, uuid.UUID(subagent_result["child_run_id"]))
    assert child_a.delegation_batch_id == child_b.delegation_batch_id


# ── wiring: constants, chat summary, workspace backfill ──────────────────────


def test_spawn_subagent_is_a_delegation_tool():
    assert "spawn_subagent" in DELEGATION_TOOLS


def test_chat_activity_summary_for_spawn_subagent():
    run = Run(
        project_id=uuid.uuid4(),
        harness_id=uuid.uuid4(),
        task_type="chat",
        task_input={},
        messages=[
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "c1",
                        "name": "spawn_subagent",
                        "arguments": {"instructions": LONG_ENOUGH, "label": "acme lookup"},
                    }
                ],
            }
        ],
        status="completed",
    )
    msg = _assistant_message(run)
    assert msg["activity"][0]["summary"] == "briefed a subagent: acme lookup"


def test_chat_activity_summary_for_delegate_parallel_lists_subagent_kind():
    run = Run(
        project_id=uuid.uuid4(),
        harness_id=uuid.uuid4(),
        task_type="chat",
        task_input={},
        messages=[
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "c1",
                        "name": "delegate_parallel",
                        "arguments": {
                            "tasks": [
                                {"task_type": "assess_risk", "task_input": {}},
                                {"kind": "subagent", "instructions": LONG_ENOUGH},
                            ]
                        },
                    }
                ],
            }
        ],
        status="completed",
    )
    msg = _assistant_message(run)
    assert "subagent" in msg["activity"][0]["summary"]
    assert "assess_risk" in msg["activity"][0]["summary"]


async def test_workspace_backfill_adds_spawn_subagent_only_alongside_run_harness_task(db):  # noqa: F811
    workspace = Workspace(name="W-backfill")
    db.add(workspace)
    await db.flush()
    chat_harness = Harness(
        workspace_id=workspace.id,
        name="Chat Assistant",
        task_profile="chat",
        model_policy={"mode": "auto"},
        tool_names=["read_document"],  # no run_harness_task at all
    )
    db.add(chat_harness)
    await db.commit()

    await seed_chat_harness(db, workspace.id)
    await db.commit()
    await db.refresh(chat_harness)
    assert "spawn_subagent" not in chat_harness.tool_names

    chat_harness.tool_names = [*chat_harness.tool_names, "run_harness_task"]
    await db.commit()

    await seed_chat_harness(db, workspace.id)
    await db.commit()
    await db.refresh(chat_harness)
    assert "spawn_subagent" in chat_harness.tool_names
    assert "delegate_parallel" in chat_harness.tool_names
