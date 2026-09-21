"""The three-phase split of `run_harness_task` (engine/tools.py).

`run_harness_task` was split into `_prepare_child` (touches `ctx.db`),
`_await_child` (must never touch `ctx.db` — a future batch tool will run
several of these concurrently via `asyncio.gather`) and `_summarize_child`
(pure). This is a refactor-safety net, not a re-test of delegation itself —
the existing `run_harness_task` tests (test_run_harness_task_selection.py,
test_delegation_lineage.py, evals/test_delegation_cancel.py) already cover
the end-to-end behaviour through the public function, unchanged.

Reuses the sqlite fixture and setup helpers from
test_run_harness_task_selection.py rather than inventing a new DB pattern.
"""
from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from tret.config import get_settings
from tret.db.models import Run
from tret.engine.compaction import ELIDABLE_TOOLS
from tret.engine.events import RunEvent
from tret.engine.harness import _effective_max_cost, _spent
from tret.engine.tools import (
    COST_CAP_KEY,
    DELEGATION_TOOLS,
    PreparedChild,
    ToolError,
    _await_child,
    _child_slot,
    _prepare_child,
    _record_delegated_cost,
    _summarize_child,
)
from tests.test_run_harness_task_selection import _ctx, _setup, db  # noqa: F401 (fixture)


@pytest.fixture()
def _settings_override(monkeypatch):
    """Set one or more `TRET_`-prefixed env vars and clear the `get_settings`
    cache so `tools.py`'s (same, module-level) `get_settings` picks them up —
    matches the pattern in tests/test_connected_tools.py."""

    def _set(**env: object) -> None:
        for key, value in env.items():
            monkeypatch.setenv(f"TRET_{key.upper()}", str(value))
        get_settings.cache_clear()

    yield _set
    get_settings.cache_clear()


def test_delegation_tools_contains_run_harness_task():
    assert "run_harness_task" in DELEGATION_TOOLS


def test_elidable_tools_still_contains_run_harness_task():
    # ELIDABLE_TOOLS folds DELEGATION_TOOLS in via `| DELEGATION_TOOLS` now,
    # instead of repeating the literal — this pins that the union still
    # contains it.
    assert "run_harness_task" in ELIDABLE_TOOLS


def _prepared() -> PreparedChild:
    return PreparedChild(child_id=uuid.uuid4(), harness_name="Climate Analyst", task_type="assess_risk")


def _finding(**overrides) -> SimpleNamespace:
    defaults = dict(
        id=uuid.uuid4(),
        schema_slug="risk_verdict",
        subject={"entity": "acme"},
        status="draft",
        payload={"score": 1},
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _done(**overrides) -> SimpleNamespace:
    defaults = dict(
        status="completed",
        model_used="gpt-test",
        cost_usd=0.5,
        delegated_cost_usd=0,
        energy_wh=1.0,
        energy_accounting={"co2e_g": 2.0},
        error=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_summarize_child_completed_with_findings():
    prepared = _prepared()
    done = _done(status="completed")
    findings = [_finding()]

    result = _summarize_child(prepared, done, findings)

    assert result["child_run_id"] == str(prepared.child_id)
    assert result["status"] == "completed"
    assert len(result["findings"]) == 1
    assert result["delegated_cost_usd"] == 0.0
    assert result["findings"][0]["finding_id"] == str(findings[0].id)
    assert result["note"] == (
        "Findings are DRAFTS awaiting human approval — say so when you report them."
    )


def test_summarize_child_reports_the_childs_own_delegated_cost():
    # What the child itself caused through further delegation, alongside its
    # own cost_usd — the two together are what this delegation cost in total.
    prepared = _prepared()
    done = _done(status="completed", cost_usd=0.5, delegated_cost_usd=1.25)

    result = _summarize_child(prepared, done, [])

    assert result["cost_usd"] == 0.5
    assert result["delegated_cost_usd"] == 1.25


def test_summarize_child_completed_without_findings():
    prepared = _prepared()
    done = _done(status="completed")

    result = _summarize_child(prepared, done, [])

    assert result["findings"] == []
    assert result["note"] == "The run completed without recording a finding."


def test_summarize_child_completed_without_output():
    prepared = _prepared()
    done = _done(status="completed_without_output")

    result = _summarize_child(prepared, done, [])

    assert "never recorded a valid result" in result["note"]


def test_summarize_child_failed():
    prepared = _prepared()
    done = _done(status="failed", error="boom")

    result = _summarize_child(prepared, done, [])

    assert result["error"] == "boom"
    assert result["note"] == "The delegated run did not complete; tell the user honestly what failed."


async def test_prepare_child_commit_false_flushes_without_committing(db):  # noqa: F811  (pytest fixture, not a redefinition)
    workspace_id, project_id, parent_id, _specialist_id = await _setup(db)
    ctx = _ctx(db, workspace_id=workspace_id, project_id=project_id, run_id=parent_id)

    prepared = await _prepare_child(ctx, "assess_risk", {}, commit=False)

    # Flushed: visible within the still-open transaction on `ctx.db`.
    assert await ctx.db.get(Run, prepared.child_id) is not None

    # Not committed: invisible through a separate connection/session.
    import tret.db.engine as db_engine

    async with db_engine.get_session_factory()() as other:
        assert await other.get(Run, prepared.child_id) is None

    await ctx.db.rollback()
    assert await ctx.db.get(Run, prepared.child_id) is None


async def test_prepare_child_commit_true_commits(db):  # noqa: F811  (pytest fixture, not a redefinition)
    workspace_id, project_id, parent_id, _specialist_id = await _setup(db)
    ctx = _ctx(db, workspace_id=workspace_id, project_id=project_id, run_id=parent_id)

    prepared = await _prepare_child(ctx, "assess_risk", {}, commit=True)

    import tret.db.engine as db_engine

    async with db_engine.get_session_factory()() as other:
        assert await other.get(Run, prepared.child_id) is not None


# ── lineage columns ──────────────────────────────────────────────────────────


async def test_prepare_child_stamps_lineage_on_a_root_parent(db):  # noqa: F811
    workspace_id, project_id, parent_id, specialist_id = await _setup(db)
    ctx = _ctx(db, workspace_id=workspace_id, project_id=project_id, run_id=parent_id)

    prepared = await _prepare_child(ctx, "assess_risk", {})

    child = await db.get(Run, prepared.child_id)
    assert child.parent_run_id == parent_id
    # The parent is itself a root (no root_run_id of its own), so the child's
    # root is the parent's own id.
    assert child.root_run_id == parent_id
    assert child.delegation_kind == "task"
    assert child.harness_id == specialist_id


async def test_prepare_child_root_run_id_comes_from_the_parents_root_not_the_parent(
    db,  # noqa: F811
):
    workspace_id, project_id, parent_id, _specialist_id = await _setup(db)
    root_ctx = _ctx(db, workspace_id=workspace_id, project_id=project_id, run_id=parent_id)
    first = await _prepare_child(root_ctx, "assess_risk", {})

    # A second hop, delegating FROM the child `_prepare_child` just created —
    # its root must be the original root, not the immediate parent.
    child_ctx = _ctx(db, workspace_id=workspace_id, project_id=project_id, run_id=first.child_id)
    child_ctx.delegation_depth = 1
    second = await _prepare_child(child_ctx, "assess_risk", {})

    grandchild = await db.get(Run, second.child_id)
    assert grandchild.parent_run_id == first.child_id
    assert grandchild.root_run_id == parent_id


async def test_prepare_child_kind_and_batch_id_are_stamped_when_given(db):  # noqa: F811
    workspace_id, project_id, parent_id, _specialist_id = await _setup(db)
    ctx = _ctx(db, workspace_id=workspace_id, project_id=project_id, run_id=parent_id)
    batch_id = uuid.uuid4()

    prepared = await _prepare_child(ctx, "assess_risk", {}, kind="subagent", batch_id=batch_id)

    child = await db.get(Run, prepared.child_id)
    assert child.delegation_kind == "subagent"
    assert child.delegation_batch_id == batch_id


# ── budget carve-up ──────────────────────────────────────────────────────────


async def test_prepare_child_skips_the_carve_up_when_ctx_has_no_max_cost(db):  # noqa: F811
    """`ctx.max_cost_usd is None` (the default, and what every RunContext a
    test builds directly gets unless it says otherwise) means the engine
    never computed an effective cap for this run — so delegation must not
    guess one. No `_cost_cap_usd` key, no refusal, regardless of how much the
    parent has already spent."""
    workspace_id, project_id, parent_id, _specialist_id = await _setup(db)
    parent = await db.get(Run, parent_id)
    parent.cost_usd = Decimal("999")  # would blow any real cap, if one applied
    await db.commit()
    ctx = _ctx(db, workspace_id=workspace_id, project_id=project_id, run_id=parent_id)
    assert ctx.max_cost_usd is None

    prepared = await _prepare_child(ctx, "assess_risk", {})

    child = await db.get(Run, prepared.child_id)
    assert COST_CAP_KEY not in child.task_input


async def test_prepare_child_cap_is_the_min_of_harness_cap_and_remaining_share(
    db,  # noqa: F811
):
    workspace_id, project_id, parent_id, _specialist_id = await _setup(db)
    parent = await db.get(Run, parent_id)
    parent.cost_usd = Decimal("1.0")
    parent.delegated_cost_usd = Decimal("0.5")
    await db.commit()
    ctx = _ctx(db, workspace_id=workspace_id, project_id=project_id, run_id=parent_id)
    ctx.max_cost_usd = Decimal("5.0")  # remaining = 5.0 - 1.5 = 3.5

    prepared = await _prepare_child(ctx, "assess_risk", {})

    child = await db.get(Run, prepared.child_id)
    # The specialist harness has no loop_config of its own, so its cap is the
    # engine default (5.0) — bigger than the 3.5 remaining, so the remaining
    # share wins.
    assert child.task_input[COST_CAP_KEY] == "3.500000"


async def test_prepare_child_budget_share_splits_the_remaining_budget(db):  # noqa: F811
    workspace_id, project_id, parent_id, _specialist_id = await _setup(db)
    ctx = _ctx(db, workspace_id=workspace_id, project_id=project_id, run_id=parent_id)
    ctx.max_cost_usd = Decimal("3.0")  # remaining = 3.0 - 0 = 3.0

    prepared = await _prepare_child(ctx, "assess_risk", {}, budget_share=3)

    child = await db.get(Run, prepared.child_id)
    assert child.task_input[COST_CAP_KEY] == "1.000000"  # 3.0 / 3


async def test_prepare_child_refuses_below_the_floor_and_inserts_no_row(db):  # noqa: F811
    workspace_id, project_id, parent_id, _specialist_id = await _setup(db)
    ctx = _ctx(db, workspace_id=workspace_id, project_id=project_id, run_id=parent_id)
    ctx.max_cost_usd = Decimal("0.10")  # split 3 ways: 0.0333... < MIN_CHILD_BUDGET_USD

    before = (await db.execute(select(func.count()).select_from(Run))).scalar()

    with pytest.raises(ToolError) as excinfo:
        await _prepare_child(ctx, "assess_risk", {}, budget_share=3)
    assert "not enough to delegate" in str(excinfo.value)

    after = (await db.execute(select(func.count()).select_from(Run))).scalar()
    assert after == before  # no row inserted


# ── _record_delegated_cost ───────────────────────────────────────────────────


async def _write_child_spend(child_id, *, cost: str, delegated: str) -> None:
    """Spend lands on the child the way the engine writes it: through a
    session of its own, never the parent's `ctx.db`."""
    from tret.db.engine import get_session_factory

    async with get_session_factory()() as other:
        child = await other.get(Run, child_id)
        child.cost_usd = Decimal(cost)
        child.delegated_cost_usd = Decimal(delegated)
        await other.commit()


async def test_record_delegated_cost_adds_childs_own_plus_its_own_delegated_cost(
    db,  # noqa: F811
):
    workspace_id, project_id, parent_id, _specialist_id = await _setup(db)
    parent = await db.get(Run, parent_id)
    parent.delegated_cost_usd = Decimal("1.0")
    await db.commit()
    ctx = _ctx(db, workspace_id=workspace_id, project_id=project_id, run_id=parent_id)
    prepared = await _prepare_child(ctx, "assess_risk", {})
    await _write_child_spend(prepared.child_id, cost="0.25", delegated="0.10")

    await _record_delegated_cost(ctx, prepared.child_id)

    # Durable, not just pending in the session: the engine rolls `ctx.db` back
    # when a tool call errors, and a child's spend must survive that.
    await db.rollback()
    parent = await db.get(Run, parent_id)
    await db.refresh(parent)
    assert parent.delegated_cost_usd == Decimal("1.35")


async def test_record_delegated_cost_is_a_noop_for_an_unknown_child(db):  # noqa: F811
    workspace_id, project_id, parent_id, _specialist_id = await _setup(db)
    ctx = _ctx(db, workspace_id=workspace_id, project_id=project_id, run_id=parent_id)

    await _record_delegated_cost(ctx, uuid.uuid4())

    parent = await db.get(Run, parent_id)
    assert parent.delegated_cost_usd == Decimal("0")


async def test_child_spend_is_recorded_even_when_the_await_phase_raises(
    db, monkeypatch  # noqa: F811
):
    import tret.engine.tools as tools_module

    workspace_id, project_id, parent_id, _specialist_id = await _setup(db)
    ctx = _ctx(db, workspace_id=workspace_id, project_id=project_id, run_id=parent_id)

    async def _spend_then_raise(_ctx_arg, prepared):
        await _write_child_spend(prepared.child_id, cost="0.40", delegated="0")
        raise RuntimeError("read-back failed")

    monkeypatch.setattr(tools_module, "_await_child", _spend_then_raise)

    with pytest.raises(RuntimeError):
        await tools_module.run_harness_task(ctx, "assess_risk", {})

    await db.rollback()
    parent = await db.get(Run, parent_id)
    await db.refresh(parent)
    assert parent.delegated_cost_usd == Decimal("0.40")


async def test_await_child_never_touches_the_shared_session(db):  # noqa: F811
    """The invariant the whole split exists for: a batch caller gathers several
    `_await_child`s at once, and `ctx.db` is one AsyncSession that cannot be
    used concurrently."""
    workspace_id, project_id, parent_id, _specialist_id = await _setup(db)
    ctx = _ctx(db, workspace_id=workspace_id, project_id=project_id, run_id=parent_id)
    prepared = await _prepare_child(ctx, "assess_risk", {})

    class _Forbidden:
        def __getattr__(self, name):
            raise AssertionError(f"_await_child touched ctx.db.{name}")

    ctx.db = _Forbidden()
    done, _findings = await _await_child(ctx, prepared)
    assert done is not None and done.id == prepared.child_id


async def test_tool_result_carries_delegated_cost(db):  # noqa: F811
    import json

    from tret.engine.tools import run_harness_task

    workspace_id, project_id, parent_id, _specialist_id = await _setup(db)
    ctx = _ctx(db, workspace_id=workspace_id, project_id=project_id, run_id=parent_id)
    result = json.loads(await run_harness_task(ctx, "assess_risk", {}))
    assert result["delegated_cost_usd"] == 0.0


# ── engine cap math, as pure functions ───────────────────────────────────────
#
# A genuine end-to-end engine test (a real HarnessEngine.execute() run
# actually bounded by a stamped _cost_cap_usd, or one already over cap on
# delegated_cost_usd alone) needs the full `golden_world`/ReplayProvider
# harness (see tests/evals/test_engine_loop.py's delegation-depth suite) to
# script a provider and a pack; wiring that up was not practical in this
# batch. `_effective_max_cost` and `_spent` are exactly the two pieces
# harness.py's cap check was refactored to use, so they are covered directly
# and completely as pure functions instead.


def test_effective_max_cost_a_lower_stamped_cap_wins():
    assert _effective_max_cost(Decimal("5.0"), {COST_CAP_KEY: "2.5"}) == Decimal("2.5")


def test_effective_max_cost_never_widens_the_harness_cap():
    assert _effective_max_cost(Decimal("5.0"), {COST_CAP_KEY: "999"}) == Decimal("5.0")


def test_effective_max_cost_returns_the_harness_cap_when_no_key_is_present():
    assert _effective_max_cost(Decimal("5.0"), {}) == Decimal("5.0")


@pytest.mark.parametrize("raw", ["not-a-number", "0", "-1", "0.0", "", "NaN", "sNaN", "-Infinity", [], {}])
def test_effective_max_cost_ignores_garbage_zero_and_negative_values(raw):
    task_input = {COST_CAP_KEY: raw}
    assert _effective_max_cost(Decimal("5.0"), task_input) == Decimal("5.0")


def test_spent_adds_cost_usd_and_delegated_cost_usd():
    run = SimpleNamespace(cost_usd=Decimal("1.5"), delegated_cost_usd=Decimal("0.5"))
    assert _spent(run) == Decimal("2.0")


def test_spent_treats_none_fields_as_zero():
    run = SimpleNamespace(cost_usd=None, delegated_cost_usd=None)
    assert _spent(run) == Decimal("0")


# ── PreparedChild: depth/kind/batch_id/index/label ──────────────────────────
# Plumbing for the coming `delegate_parallel` tool — `run_harness_task`'s own
# behaviour is unchanged (it never passes index/label and lets depth/kind
# default).


async def test_prepare_child_depth_is_parent_depth_plus_one(db):  # noqa: F811
    workspace_id, project_id, parent_id, _specialist_id = await _setup(db)
    ctx = _ctx(db, workspace_id=workspace_id, project_id=project_id, run_id=parent_id)
    ctx.delegation_depth = 1

    prepared = await _prepare_child(ctx, "assess_risk", {})

    assert prepared.depth == 2


async def test_prepare_child_defaults_kind_batch_index_label(db):  # noqa: F811
    workspace_id, project_id, parent_id, _specialist_id = await _setup(db)
    ctx = _ctx(db, workspace_id=workspace_id, project_id=project_id, run_id=parent_id)

    prepared = await _prepare_child(ctx, "assess_risk", {})

    assert prepared.depth == 1
    assert prepared.kind == "task"
    assert prepared.batch_id is None
    assert prepared.index is None
    assert prepared.label is None


async def test_prepare_child_carries_index_and_label(db):  # noqa: F811
    workspace_id, project_id, parent_id, _specialist_id = await _setup(db)
    ctx = _ctx(db, workspace_id=workspace_id, project_id=project_id, run_id=parent_id)

    prepared = await _prepare_child(ctx, "assess_risk", {}, index=2, label="fetch invoices")

    assert prepared.index == 2
    assert prepared.label == "fetch invoices"


async def test_prepare_child_truncates_label_to_80_chars(db):  # noqa: F811
    workspace_id, project_id, parent_id, _specialist_id = await _setup(db)
    ctx = _ctx(db, workspace_id=workspace_id, project_id=project_id, run_id=parent_id)

    prepared = await _prepare_child(ctx, "assess_risk", {}, label="x" * 200)

    assert prepared.label == "x" * 80


async def test_prepare_child_persists_label_on_child_task_input(db):  # noqa: F811
    """The label must survive past the turn that started it (completed-turn
    activity and the run-detail children table only have the child row to
    read it back from — see api/runs.py::_run_summary)."""
    from tret.engine.tools import LABEL_KEY

    workspace_id, project_id, parent_id, _specialist_id = await _setup(db)
    ctx = _ctx(db, workspace_id=workspace_id, project_id=project_id, run_id=parent_id)

    prepared = await _prepare_child(ctx, "assess_risk", {}, label="Cedar Landing Flood Assessment")

    child = await db.get(Run, prepared.child_id)
    assert child.task_input[LABEL_KEY] == "Cedar Landing Flood Assessment"


async def test_prepare_child_persisted_label_is_truncated_to_80_chars(db):  # noqa: F811
    from tret.engine.tools import LABEL_KEY

    workspace_id, project_id, parent_id, _specialist_id = await _setup(db)
    ctx = _ctx(db, workspace_id=workspace_id, project_id=project_id, run_id=parent_id)

    prepared = await _prepare_child(ctx, "assess_risk", {}, label="x" * 200)

    child = await db.get(Run, prepared.child_id)
    assert child.task_input[LABEL_KEY] == "x" * 80


async def test_prepare_child_model_supplied_label_in_task_input_does_not_survive_without_arg(
    db,  # noqa: F811
):
    """A model-written `_label` inside a TASK child's free-form task_input
    must never masquerade as the engine-stamped label — with no `label`
    argument given, it is dropped."""
    from tret.engine.tools import LABEL_KEY

    workspace_id, project_id, parent_id, _specialist_id = await _setup(db)
    ctx = _ctx(db, workspace_id=workspace_id, project_id=project_id, run_id=parent_id)

    prepared = await _prepare_child(ctx, "assess_risk", {LABEL_KEY: "sneaky"})

    assert prepared.label is None
    child = await db.get(Run, prepared.child_id)
    assert LABEL_KEY not in child.task_input


async def test_prepare_child_engine_label_overrides_model_supplied_task_input_label(
    db,  # noqa: F811
):
    """When a label argument IS given, it wins over whatever the model put at
    `_label` in its own task_input dict."""
    from tret.engine.tools import LABEL_KEY

    workspace_id, project_id, parent_id, _specialist_id = await _setup(db)
    ctx = _ctx(db, workspace_id=workspace_id, project_id=project_id, run_id=parent_id)

    prepared = await _prepare_child(
        ctx, "assess_risk", {LABEL_KEY: "sneaky"}, label="Real Label"
    )

    assert prepared.label == "Real Label"
    child = await db.get(Run, prepared.child_id)
    assert child.task_input[LABEL_KEY] == "Real Label"


# ── delegation event payloads ────────────────────────────────────────────────


async def test_await_child_events_carry_kind_batch_index_label(db, monkeypatch):  # noqa: F811
    import tret.engine.tools as tools_module

    workspace_id, project_id, parent_id, _specialist_id = await _setup(db)
    ctx = _ctx(db, workspace_id=workspace_id, project_id=project_id, run_id=parent_id)
    batch_id = uuid.uuid4()
    prepared = await _prepare_child(
        ctx, "assess_risk", {}, kind="subagent", batch_id=batch_id, index=1, label="fetch"
    )

    published: list[RunEvent] = []

    class _Recorder:
        async def publish(self, run_id, event):
            published.append(event)

    monkeypatch.setattr(tools_module, "get_event_bus", lambda: _Recorder())

    done, _findings = await _await_child(ctx, prepared)

    started = next(e for e in published if e.type == "delegation_started")
    finished = next(e for e in published if e.type == "delegation_finished")
    for event in (started, finished):
        assert event.data["kind"] == "subagent"
        assert event.data["batch_id"] == str(batch_id)
        assert event.data["index"] == 1
        assert event.data["label"] == "fetch"
    # `delegation_started` never had a cost to report; `delegation_finished`
    # gains the child's own `cost_usd` once `done` is known.
    assert "cost_usd" not in started.data
    assert finished.data["cost_usd"] == float(done.cost_usd or 0)


async def test_await_child_events_batch_id_is_none_outside_a_batch(db, monkeypatch):  # noqa: F811
    import tret.engine.tools as tools_module

    workspace_id, project_id, parent_id, _specialist_id = await _setup(db)
    ctx = _ctx(db, workspace_id=workspace_id, project_id=project_id, run_id=parent_id)
    prepared = await _prepare_child(ctx, "assess_risk", {})

    published: list[RunEvent] = []

    class _Recorder:
        async def publish(self, run_id, event):
            published.append(event)

    monkeypatch.setattr(tools_module, "get_event_bus", lambda: _Recorder())

    await _await_child(ctx, prepared)

    for event in published:
        assert event.data["batch_id"] is None
        assert event.data["index"] is None
        assert event.data["label"] is None


# ── per-depth child-run slots ────────────────────────────────────────────────


class _ConcurrencyEngine:
    """Fake `HarnessEngine`: `execute` holds its "slot" long enough for two
    concurrent calls to be observed, and `register`/`unregister_delegation`
    just record call order — matched to the real engine's own lineage
    bookkeeping, which `_await_child` calls unconditionally around `execute`.
    """

    def __init__(self):
        self.current = 0
        self.max_seen = 0
        self.events: list[tuple[str, uuid.UUID]] = []

    def register_delegation(self, *, child_id, parent_id):
        self.events.append(("register", child_id))

    def unregister_delegation(self, child_id):
        self.events.append(("unregister", child_id))

    async def execute(self, run_id):
        self.events.append(("execute_start", run_id))
        self.current += 1
        self.max_seen = max(self.max_seen, self.current)
        await asyncio.sleep(0.02)
        self.current -= 1
        self.events.append(("execute_end", run_id))


async def _prepared_pair(db, *, depths=(0, 0)):  # noqa: F811
    """Two `(ctx, PreparedChild)` pairs off the same parent, at the given
    `ctx.delegation_depth`s — so `prepared.depth` is `depth + 1` for each."""
    workspace_id, project_id, parent_id, _specialist_id = await _setup(db)
    pairs = []
    for depth in depths:
        ctx = _ctx(db, workspace_id=workspace_id, project_id=project_id, run_id=parent_id)
        ctx.delegation_depth = depth
        pairs.append((ctx, await _prepare_child(ctx, "assess_risk", {})))
    return pairs


async def test_child_slots_serialize_same_depth_children_at_limit_one(
    db, monkeypatch, _settings_override  # noqa: F811
):
    _settings_override(max_concurrent_child_runs=1)
    import tret.engine.harness as harness_module

    engine = _ConcurrencyEngine()
    monkeypatch.setattr(harness_module, "get_harness_engine", lambda: engine)

    pairs = await _prepared_pair(db)
    await asyncio.gather(*(_await_child(ctx, prepared) for ctx, prepared in pairs))

    assert engine.max_seen == 1


async def test_child_slots_allow_two_at_once_at_limit_two(
    db, monkeypatch, _settings_override  # noqa: F811
):
    _settings_override(max_concurrent_child_runs=2)
    import tret.engine.harness as harness_module

    engine = _ConcurrencyEngine()
    monkeypatch.setattr(harness_module, "get_harness_engine", lambda: engine)

    pairs = await _prepared_pair(db)
    await asyncio.gather(*(_await_child(ctx, prepared) for ctx, prepared in pairs))

    assert engine.max_seen == 2


async def test_child_slots_are_per_depth_not_shared(
    db, monkeypatch, _settings_override  # noqa: F811
):
    """Even at limit 1, children at DIFFERENT depths run concurrently — one
    shared pool would deadlock a depth-1 parent that is itself waiting inside
    `execute()` for a depth-2 slot only a depth-1 finish could ever free."""
    _settings_override(max_concurrent_child_runs=1)
    import tret.engine.harness as harness_module

    engine = _ConcurrencyEngine()
    monkeypatch.setattr(harness_module, "get_harness_engine", lambda: engine)

    pairs = await _prepared_pair(db, depths=(0, 1))
    assert [prepared.depth for _ctx, prepared in pairs] == [1, 2]

    await asyncio.gather(*(_await_child(ctx, prepared) for ctx, prepared in pairs))

    assert engine.max_seen == 2


async def test_child_slot_limit_zero_is_unlimited(
    db, monkeypatch, _settings_override  # noqa: F811
):
    _settings_override(max_concurrent_child_runs=0)
    import tret.engine.harness as harness_module

    engine = _ConcurrencyEngine()
    monkeypatch.setattr(harness_module, "get_harness_engine", lambda: engine)
    assert _child_slot(1) is None

    pairs = await _prepared_pair(db)
    await asyncio.gather(*(_await_child(ctx, prepared) for ctx, prepared in pairs))

    assert engine.max_seen == 2


async def test_register_delegation_runs_before_the_slot_is_acquired(
    db, monkeypatch, _settings_override  # noqa: F811
):
    """A child still queued for a slot is already registered, so a cancel of
    the parent reaches it — see the comment in `_await_child`."""
    _settings_override(max_concurrent_child_runs=1)
    import tret.engine.harness as harness_module

    engine = _ConcurrencyEngine()
    monkeypatch.setattr(harness_module, "get_harness_engine", lambda: engine)

    workspace_id, project_id, parent_id, _specialist_id = await _setup(db)
    ctx = _ctx(db, workspace_id=workspace_id, project_id=project_id, run_id=parent_id)
    prepared = await _prepare_child(ctx, "assess_risk", {})

    slot = _child_slot(prepared.depth)
    await slot.acquire()  # hold the only slot before the child even starts
    try:
        task = asyncio.create_task(_await_child(ctx, prepared))
        await asyncio.sleep(0)  # let it run up to the (blocking) acquire
        assert ("register", prepared.child_id) in engine.events
        assert ("execute_start", prepared.child_id) not in engine.events
    finally:
        slot.release()
    await task
    assert ("execute_start", prepared.child_id) in engine.events


# ── lifetime children cap ────────────────────────────────────────────────────


async def test_prepare_child_raises_when_the_lifetime_cap_is_reached(
    db, _settings_override  # noqa: F811
):
    _settings_override(max_children_per_run=2)
    workspace_id, project_id, parent_id, _specialist_id = await _setup(db)
    ctx = _ctx(db, workspace_id=workspace_id, project_id=project_id, run_id=parent_id)

    await _prepare_child(ctx, "assess_risk", {})
    await _prepare_child(ctx, "assess_risk", {})
    assert ctx.children_started == 2

    before = (await db.execute(select(func.count()).select_from(Run))).scalar()
    with pytest.raises(ToolError) as excinfo:
        await _prepare_child(ctx, "assess_risk", {})
    assert "Delegation limit reached" in str(excinfo.value)

    after = (await db.execute(select(func.count()).select_from(Run))).scalar()
    assert after == before  # no row inserted
    assert ctx.children_started == 2  # unchanged by the refused attempt


async def test_prepare_child_reserve_refuses_up_front(db, _settings_override):  # noqa: F811
    _settings_override(max_children_per_run=2)
    workspace_id, project_id, parent_id, _specialist_id = await _setup(db)
    ctx = _ctx(db, workspace_id=workspace_id, project_id=project_id, run_id=parent_id)

    with pytest.raises(ToolError):
        await _prepare_child(ctx, "assess_risk", {}, reserve=3)

    assert ctx.children_started == 0


async def test_prepare_child_lifetime_cap_zero_is_unlimited(
    db, _settings_override  # noqa: F811
):
    _settings_override(max_children_per_run=0)
    workspace_id, project_id, parent_id, _specialist_id = await _setup(db)
    ctx = _ctx(db, workspace_id=workspace_id, project_id=project_id, run_id=parent_id)

    for _ in range(5):
        await _prepare_child(ctx, "assess_risk", {})

    assert ctx.children_started == 5


# ── run_harness_task's public shape is unchanged ─────────────────────────────


async def test_run_harness_task_result_keys_are_unchanged(db):  # noqa: F811
    import json

    from tret.engine.tools import run_harness_task

    workspace_id, project_id, parent_id, _specialist_id = await _setup(db)
    ctx = _ctx(db, workspace_id=workspace_id, project_id=project_id, run_id=parent_id)

    result = json.loads(await run_harness_task(ctx, "assess_risk", {}))

    assert set(result.keys()) == {
        "child_run_id",
        "status",
        "model_used",
        "cost_usd",
        "delegated_cost_usd",
        "energy_wh",
        "co2e_g",
        "error",
        "findings",
        "note",
    }
