"""The blind pairwise judge (`tret/batches/pairwise.py`, `submit_pairwise`), its
compare overlay and its API.

Rows are seeded straight into sqlite (the helpers of `test_batch_cross_check.py`);
the judge is a fake engine that calls the real `submit_pairwise` tool on a
`RunContext`, so the tool's guards, the message and the runner are the real ones.
"""
# ruff: noqa: F811 - the imported fixtures are used by name as test arguments
from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.evals.golden_world import install_sqlite_type_shims
from tests.test_batch_cross_check import SECRET_BATCH, T0, mk_batch, mk_row, mk_target
from tret.api import batches as batches_api
from tret.api.auth import current_user
from tret.api.workspace import WorkspaceContext, current_workspace
from tret.batches import core, pairwise, scoring
from tret.batches.rubrics import criterion_applies, default_rubric
from tret.config import get_settings
from tret.db.engine import get_db
from tret.db.models import (
    Base,
    Batch,
    BatchEvaluation,
    Harness,
    PairwiseJudgment,
    Project,
    Run,
    RunOutcome,
    User,
    Workspace,
)
from tret.engine.evaluation_tools import submit_pairwise
from tret.engine.tools import RunContext, ToolError
from tret.services import collections as service
from tret.services.outcomes import build_outcomes

JUDGE = "anthropic/claude-opus-5-5"
RUBRIC = default_rubric("disclosure")
CRITERIA = [c["key"] for c in RUBRIC["criteria"] if criterion_applies(c["applies"], "success")]
MODEL = "zeta/model-9"


class PairEngine:
    """Judge runs call the real `submit_pairwise`. `better` is a marker: the judge
    prefers the output that carries it (tie when neither does)."""

    def __init__(self, factory, ws_id):
        self.factory, self.ws_id = factory, ws_id
        self.better = "BETTER"
        self.mode = "ok"  # ok | silent | hang | first | tie_second_order
        self.cost = "0.02"
        self.inputs: list[dict] = []
        self.cancelled: list[uuid.UUID] = []

    def cancel(self, run_id):
        self.cancelled.append(run_id)

    def forget_cancelled(self, run_id):
        pass

    def preference(self, ti) -> str:
        message = ti["message"]
        one = message.split("## Output 1", 1)[1].split("## Output 2", 1)[0]
        two = message.split("## Output 2", 1)[1]
        if self.mode == "first":
            return "first"
        if self.mode == "tie_second_order" and ti["_pairwise_order"] == "high_first":
            return "tie"
        if self.better in one and self.better not in two:
            return "first"
        if self.better in two and self.better not in one:
            return "second"
        return "tie"

    async def execute(self, run_id):
        async with self.factory() as db:
            run = await db.get(Run, run_id)
            ti = dict(run.task_input or {})
            self.inputs.append(ti)
            run.status, run.model_used, run.cost_usd = "completed", "judge/model", Decimal(self.cost)
            await db.commit()
            if self.mode == "hang":
                while run_id not in self.cancelled:
                    await asyncio.sleep(0.02)
                return
            if self.mode == "silent":
                return
            pref = self.preference(ti)
            ctx = RunContext(
                db=db, run_id=run_id, project_id=run.project_id, pack_id=None, doctrine_sha=None,
                model_used="judge/model", document_ids=[], output_schemas={}, workspace_id=self.ws_id,
            )
            await submit_pairwise(
                ctx, preferred=pref, criteria={k: pref for k in CRITERIA}, confidence="high",
                rationale=f"decided {pref}",
            )


@pytest.fixture
async def w(tmp_path, monkeypatch):
    from tret.providers.catalog import ProviderRegistry

    install_sqlite_type_shims()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'pairwise.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    ws = Workspace(id=uuid.uuid4(), name="Kith", kind="team")
    other_ws = Workspace(id=uuid.uuid4(), name="Other", kind="team")
    user = User(id=uuid.uuid4(), email="p@example.com", display_name="P", role="approver")
    async with factory() as db:
        db.add_all([ws, other_ws, user])
        await db.flush()
        project = Project(id=uuid.uuid4(), workspace_id=ws.id, name="P")
        other_project = Project(id=uuid.uuid4(), workspace_id=other_ws.id, name="Q")
        harness = Harness(id=uuid.uuid4(), workspace_id=ws.id, name="H", tool_names=[])
        db.add_all([project, other_project, harness])
        await db.commit()
    eng = PairEngine(factory, ws.id)
    tasks: list[asyncio.Task] = []

    def spawn(coro):
        t = asyncio.create_task(coro)
        tasks.append(t)
        return t

    monkeypatch.setattr(service, "get_session_factory", lambda: factory)
    monkeypatch.setattr(service, "get_harness_engine", lambda: eng)
    monkeypatch.setattr(core, "_app", None)
    monkeypatch.setattr(core, "_spawn", spawn)
    monkeypatch.setattr(ProviderRegistry, "has_key", lambda self, provider: True)
    monkeypatch.setattr(get_settings(), "batch_evaluator_concurrency", 1)
    pairwise._state.update(loop=None, sem=None)
    async with factory() as db:
        yield SimpleNamespace(
            db=db, factory=factory, ws=ws, other_ws=other_ws, user=user, project=project,
            other_project=other_project, harness=harness, eng=eng, tasks=tasks,
            clock=iter(range(1, 10_000)),
        )
    await engine.dispose()


async def drain(w):
    if w.tasks:
        await asyncio.gather(*w.tasks)
        w.tasks.clear()


async def mk_pass(w, batch, *, judge=JUDGE, rubric=None, status="completed", minutes=0):
    rubric = rubric or RUBRIC
    from datetime import timedelta

    ev = BatchEvaluation(
        id=uuid.uuid4(), collection_id=batch.id, workspace_id=batch.workspace_id,
        family_key=scoring.family_key(batch), rubric_key=rubric["key"], rubric_version=rubric["version"],
        rubric=rubric, judge_model=judge, status=status, cost_usd=Decimal(0),
        created_at=T0 + timedelta(minutes=minutes),
    )
    w.db.add(ev)
    await w.db.commit()
    return ev


async def scored(w, name, slug, rows, *, judge=JUDGE, rubric=None, **kw):
    """A batch with a current pass; rows are (slug, status, quality, outcome-text)."""
    batch = await mk_batch(w, name, slug, **kw)
    ev = await mk_pass(w, batch, judge=judge, rubric=rubric)
    out = {}
    for i, (tslug, status, quality, text) in enumerate(rows):
        t = await mk_target(
            w, batch, tslug, status, position=i, outcome={"summary": text} if text else None
        )
        await mk_row(w, ev, batch, t, quality)
        out[tslug] = t
    return batch, ev, out


async def pair(w, rows_a=None, rows_b=None):
    rows_a = rows_a or [
        ("acme", "completed", 80, f"BETTER holdings of {MODEL}"),
        ("globex", "completed", 70, "thin result"),
        ("initech", "completed", 60, "thin"),
    ]
    rows_b = rows_b or [
        ("acme", "completed", 82, "thinner result"),
        ("globex", "completed", 90, "BETTER much fuller"),
        ("initech", "completed", 63, "thinner"),
    ]
    a, _, ta = await scored(w, SECRET_BATCH, "a", rows_a)
    b, _, tb = await scored(w, "Batch B", "b", rows_b)
    return a, b, ta, tb


async def rows_of(w):
    return list(
        (await w.db.execute(select(PairwiseJudgment).execution_options(populate_existing=True))).scalars()
    )


# ── eligibility ──────────────────────────────────────────────────────────────
async def test_eligible_pairs_for_both_scopes_ordered_by_smallest_gap(w):
    a, b, _, _ = await pair(
        w,
        [("acme", "completed", 80, "x"), ("globex", "completed", 70, "x"), ("initech", "completed", 60, "x"),
         ("hooli", "gap", 90, "x"), ("umbrella", "completed", None, "x")],
        [("acme", "completed", 83, "x"), ("globex", "completed", 90, "x"), ("initech", "completed", 61, "x"),
         ("hooli", "completed", 90, "x"), ("umbrella", "completed", 70, "x")],
    )
    da, db_ = await pairwise.light_collect(w.db, a), await pairwise.light_collect(w.db, b)
    near = pairwise.eligible_pairs(da, db_, "near_ties")
    assert [x.row for x, _ in near] == ["initech", "acme"]  # |delta| 1 then 3
    both = pairwise.eligible_pairs(da, db_, "both_delivered")
    assert [x.row for x, _ in both] == ["initech", "acme", "globex"]  # gap row and unscored row never
    with pytest.raises(HTTPException) as e:
        pairwise.eligible_pairs(da, db_, "bogus")
    assert e.value.status_code == 422


# ── starting: refusals ───────────────────────────────────────────────────────
async def test_not_comparable_and_same_batch_are_refused(w):
    a, b, _, _ = await pair(w)
    with pytest.raises(HTTPException) as e:
        await pairwise.start_pairwise(w.db, a, a, user_id=w.user.id)
    assert e.value.status_code == 422
    other_judge, _, _ = await scored(w, "C", "c", [("acme", "completed", 80, "x")], judge="anthropic/claude-sonnet-5-5")
    with pytest.raises(HTTPException) as e:
        await pairwise.start_pairwise(w.db, a, other_judge, user_id=w.user.id)
    assert e.value.status_code == 422 and "judge" in e.value.detail
    unscored = await mk_batch(w, "D", "d")
    with pytest.raises(HTTPException) as e:
        await pairwise.start_pairwise(w.db, a, unscored, user_id=w.user.id)
    assert e.value.status_code == 422
    other_fy, _, _ = await scored(w, "E", "e", [("acme", "completed", 80, "x")], fy=2024)
    with pytest.raises(HTTPException) as e:
        await pairwise.start_pairwise(w.db, a, other_fy, user_id=w.user.id)
    assert e.value.status_code == 422 and "family" in e.value.detail
    assert await rows_of(w) == []


# ── verdicts ─────────────────────────────────────────────────────────────────
def _calls(first_order_pref, second_order_pref):
    return [
        {"order": "low_first", "preferred": first_order_pref},
        {"order": "high_first", "preferred": second_order_pref},
    ]


def test_the_verdict_table():
    # low_first: "first" is low. high_first: "first" is high.
    assert pairwise.verdict_from(_calls("first", "second")) == ("low", "clear")
    assert pairwise.verdict_from(_calls("second", "first")) == ("high", "clear")
    assert pairwise.verdict_from(_calls("first", "tie")) == ("low", "lean")
    assert pairwise.verdict_from(_calls("tie", "first")) == ("high", "lean")
    assert pairwise.verdict_from(_calls("tie", "tie")) == ("tie", None)
    assert pairwise.verdict_from(_calls("first", "first")) == ("inconsistent", None)
    assert pairwise.verdict_from(_calls("first", None)) == (None, None)


# ── the run: message, orientation, outcomes ──────────────────────────────────
async def test_a_pass_judges_blind_in_both_orders_and_mirrors_for_b_a(w):
    a, b, _, _ = await pair(w)
    status = await pairwise.start_pairwise(w.db, a, b, user_id=w.user.id, scope="both_delivered")
    assert status["status"] == "running" and status["pending"] + status["running"] == 3
    await drain(w)
    st = await pairwise.pairwise_status(w.db, a, b, with_rows=True)
    assert st["status"] == "idle" and st["judged"] == 3 and st["verdicts"] == {"a": 1, "b": 1, "tie": 1, "inconsistent": 0}
    by_row = {r["row_key"]: r for r in st["rows"]}
    assert by_row["acme"]["verdict"] == "a" and by_row["acme"]["strength"] == "clear"
    assert by_row["globex"]["verdict"] == "b" and by_row["initech"]["verdict"] == "tie"
    assert {c["order"] for c in by_row["acme"]["calls"]} == {"a_first", "b_first"}
    assert by_row["acme"]["calls"][0]["rationale"].startswith("decided")
    assert st["cost_usd"] == pytest.approx(0.12) and st["budget_usd"] >= 1.0
    # The same pair read the other way round: mirrored verdicts, same rows.
    mirrored = await pairwise.pairwise_status(w.db, b, a, with_rows=True)
    assert mirrored["verdicts"] == {"a": 1, "b": 1, "tie": 1, "inconsistent": 0}
    mrow = {r["row_key"]: r for r in mirrored["rows"]}
    assert mrow["acme"]["verdict"] == "b" and mrow["globex"]["verdict"] == "a"
    assert {c["order"] for c in mrow["acme"]["calls"]} == {"a_first", "b_first"}
    assert len(await rows_of(w)) == 3
    # Judging (b, a) afterwards finds every pair already judged: no new calls.
    calls_before = len(w.eng.inputs)
    again = await pairwise.start_pairwise(w.db, b, a, user_id=w.user.id, scope="both_delivered")
    await drain(w)
    assert again["status"] == "idle" and len(w.eng.inputs) == calls_before and len(await rows_of(w)) == 3


async def test_the_judge_message_is_blind_and_ordered(w):
    a, b, _, _ = await pair(w)
    await pairwise.start_pairwise(w.db, a, b, user_id=w.user.id, scope="both_delivered")
    await drain(w)
    inputs = w.eng.inputs
    assert len(inputs) == 6
    for ti in inputs:
        m = ti["message"]
        assert ti["_model_override"] == JUDGE and ti["_max_iterations"] == 6 and ti["_label"].startswith("pairwise:")
        assert "_batch_evaluation_id" not in ti
        for secret in (MODEL, "model-9", "Zeta", SECRET_BATCH, "Batch B", "judge/model", "$", "0.0137"):
            assert secret not in m, secret
        assert m.index("## Rubric") < m.index("## Task") < m.index("## Output 1") < m.index("## Output 2") < m.index("## Instruction")
        assert m.count("## Task") == 1 and "submit_pairwise" in m and "Breadth matters" in m
        assert any(k in m for k in CRITERIA)
    # The same row in both presentation orders swaps the outputs.
    acme = [t["message"] for t in inputs if "BETTER holdings" in t["message"]]
    assert len(acme) == 2
    positions = sorted(x.index("BETTER holdings") < x.index("## Output 2") for x in acme)
    assert positions == [False, True]
    assert "[redacted]" in acme[0]  # the run's own model name, scrubbed


async def test_pairwise_runs_write_no_run_outcomes(w):
    a, b, _, _ = await pair(w)
    await pairwise.start_pairwise(w.db, a, b, user_id=w.user.id, scope="both_delivered")
    await drain(w)
    runs = [r for r in (await w.db.execute(select(Run))).scalars() if (r.task_input or {}).get("_pairwise_judgment_id")]
    assert len(runs) == 6
    for r in runs:
        assert await build_outcomes(w.db, r) == []
    assert (await w.db.execute(select(RunOutcome))).scalars().all() == []


async def test_inconsistent_and_lean_verdicts_come_through_the_runner(w):
    a, b, _, _ = await pair(w)
    w.eng.mode = "first"
    await pairwise.start_pairwise(w.db, a, b, user_id=w.user.id, scope="both_delivered")
    await drain(w)
    st = await pairwise.pairwise_status(w.db, a, b)
    assert st["verdicts"]["inconsistent"] == 3
    w.eng.mode = "tie_second_order"
    for r in await rows_of(w):
        r.status = "failed"
    await w.db.commit()
    await pairwise.start_pairwise(w.db, a, b, user_id=w.user.id, scope="both_delivered")
    await drain(w)
    rows = await rows_of(w)
    assert sorted(r.strength or "-" for r in rows) == ["-", "lean", "lean"]  # the tie row has no strength


# ── budget, failures, cancel, sweep, staleness ───────────────────────────────
async def test_budget_stop_skips_the_rest(w):
    a, b, _, _ = await pair(w)
    w.eng.cost = "5"
    await pairwise.start_pairwise(w.db, a, b, user_id=w.user.id, scope="both_delivered", budget_usd=1)
    await drain(w)
    rows = await rows_of(w)
    assert sorted(r.status for r in rows) == ["judged", "skipped", "skipped"]
    assert {r.error for r in rows if r.status == "skipped"} == {"budget"}


async def test_a_run_without_submit_fails_the_pair(w):
    a, b, _, _ = await pair(w, [("acme", "completed", 80, "x")], [("acme", "completed", 81, "y")])
    w.eng.mode = "silent"
    await pairwise.start_pairwise(w.db, a, b, user_id=w.user.id)
    await drain(w)
    (row,) = await rows_of(w)
    assert row.status == "failed" and "submit_pairwise" in row.error and row.verdict is None
    assert len(w.eng.inputs) == 1  # the second order is not spent on a failed pair


async def test_cancel_stops_in_flight_runs_and_skips_the_rest(w):
    a, b, _, _ = await pair(w)
    w.eng.mode = "hang"
    await pairwise.start_pairwise(w.db, a, b, user_id=w.user.id, scope="both_delivered")
    for _ in range(100):
        if w.eng.inputs:
            break
        await asyncio.sleep(0.02)
    assert (await pairwise.pairwise_status(w.db, a, b))["status"] == "running"
    after = await pairwise.cancel_pairwise(w.db, a, b)
    await drain(w)
    assert after["status"] == "idle" and len(w.eng.cancelled) == 1
    rows = await rows_of(w)
    assert {r.status for r in rows} == {"skipped"} and {r.error for r in rows} == {"cancelled"}
    assert len(w.eng.inputs) == 1  # nothing else started


async def test_a_second_start_while_running_is_a_409(w):
    a, b, _, _ = await pair(w)
    w.eng.mode = "hang"
    await pairwise.start_pairwise(w.db, a, b, user_id=w.user.id, scope="both_delivered")
    for _ in range(100):
        if w.eng.inputs:
            break
        await asyncio.sleep(0.02)
    with pytest.raises(HTTPException) as e:
        await pairwise.start_pairwise(w.db, b, a, user_id=w.user.id, scope="both_delivered")
    assert e.value.status_code == 409
    await pairwise.cancel_pairwise(w.db, a, b)
    await drain(w)


async def test_the_boot_sweep_fails_orphans(w):
    a, b, ta, tb = await pair(w)
    low, high = pairwise.orient(ta["acme"], tb["acme"])
    run = Run(id=uuid.uuid4(), project_id=w.project.id, harness_id=w.harness.id, task_type="freeform",
              task_input={}, document_ids=[], status="completed")
    w.db.add(run)
    await w.db.flush()

    def row(status, calls, tl, th):
        return PairwiseJudgment(
            id=uuid.uuid4(), workspace_id=w.ws.id, family_key="disclosure:fy2025", rubric_key="k",
            rubric_version="v", judge_model=JUDGE, row_key="acme", batch_low_id=a.id, batch_high_id=b.id,
            target_low_id=tl.id, target_high_id=th.id, status=status, calls=calls, created_at=T0,
        )

    running = row("running", [{"order": "low_first", "preferred": None, "evaluator_run_id": str(run.id)}], low, high)
    pending = row("pending", [], ta["globex"], tb["globex"])
    done = row("judged", [], ta["initech"], tb["initech"])
    w.db.add_all([running, pending, done])
    await w.db.commit()
    assert await pairwise.sweep_orphaned_pairwise(w.db) == 2
    for r in (running, pending, done):
        await w.db.refresh(r)
    assert (running.status, pending.status, done.status) == ("failed", "failed", "judged")
    assert "restart" in running.error


async def test_a_retried_target_makes_the_judgment_stale_and_it_is_rejudged(w):
    a, b, ta, tb = await pair(w, [("acme", "completed", 80, "BETTER a")], [("acme", "completed", 82, "b")])
    await pairwise.start_pairwise(w.db, a, b, user_id=w.user.id)
    await drain(w)
    assert (await pairwise.pairwise_status(w.db, a, b))["stale"] == 0
    ta["acme"].attempts = 2
    await w.db.commit()
    st = await pairwise.pairwise_status(w.db, a, b)
    assert st["stale"] == 1 and st["verdicts"]["a"] == 0
    # The target's old score is stale too, so it is not eligible until re-scored.
    assert st["scope_eligible"]["near_ties"] == 0
    cmp = await scoring.compare_batches(w.db, a, b)
    assert all(r["pairwise"] is None for r in cmp["rows"])
    from sqlalchemy import update
    from tret.db.models import TargetEvaluation

    await w.db.execute(update(TargetEvaluation).where(TargetEvaluation.target_id == ta["acme"].id).values(attempt=2))
    await w.db.commit()
    before = len(w.eng.inputs)
    await pairwise.start_pairwise(w.db, a, b, user_id=w.user.id)
    await drain(w)
    assert len(w.eng.inputs) == before + 2
    st = await pairwise.pairwise_status(w.db, a, b)
    assert st["stale"] == 0 and st["judged"] == 1 and len(await rows_of(w)) == 1  # overwritten, not duplicated


# ── submit_pairwise guards ───────────────────────────────────────────────────
async def _armed(w, *, status="running"):
    a, b, ta, tb = await pair(w)
    low, high = pairwise.orient(ta["acme"], tb["acme"])
    run = Run(id=uuid.uuid4(), project_id=w.project.id, harness_id=w.harness.id, task_type="freeform",
              task_input={}, document_ids=[], status="running")
    w.db.add(run)
    await w.db.flush()
    row = PairwiseJudgment(
        id=uuid.uuid4(), workspace_id=w.ws.id, family_key=scoring.family_key(a), rubric_key=RUBRIC["key"],
        rubric_version=RUBRIC["version"], judge_model=JUDGE, row_key="acme", batch_low_id=low.collection_id,
        batch_high_id=high.collection_id, target_low_id=low.id, target_high_id=high.id, status=status,
        calls=[{"order": "low_first", "preferred": None, "criteria": {}, "confidence": None, "rationale": None,
                "evaluator_run_id": str(run.id)}],
        created_at=T0,
    )
    w.db.add(row)
    run.task_input = {"_pairwise_judgment_id": str(row.id), "_pairwise_order": "low_first"}
    await w.db.commit()
    return row, run


def _ctx(w, run, ws=None):
    return RunContext(db=w.db, run_id=run.id, project_id=run.project_id, pack_id=None, doctrine_sha=None,
                      model_used="j", document_ids=[], output_schemas={}, workspace_id=ws or w.ws.id)


GOOD = dict(confidence="high", rationale="r")


async def test_submit_pairwise_guards(w):
    row, run = await _armed(w)
    crit = {k: "first" for k in CRITERIA}
    with pytest.raises(ToolError, match="not a pairwise"):
        plain = Run(id=uuid.uuid4(), project_id=w.project.id, harness_id=w.harness.id, task_type="freeform",
                    task_input={}, document_ids=[], status="running")
        w.db.add(plain)
        await w.db.commit()
        await submit_pairwise(_ctx(w, plain), preferred="first", criteria=crit, **GOOD)
    with pytest.raises(ToolError, match="not found"):
        await submit_pairwise(_ctx(w, run, ws=w.other_ws.id), preferred="first", criteria=crit, **GOOD)
    with pytest.raises(ToolError, match="Unknown criterion"):
        await submit_pairwise(_ctx(w, run), preferred="first", criteria={**crit, "nope": "first"}, **GOOD)
    with pytest.raises(ToolError, match="missing"):
        await submit_pairwise(_ctx(w, run), preferred="first", criteria={CRITERIA[0]: "first"}, **GOOD)
    with pytest.raises(ToolError, match="preferred"):
        await submit_pairwise(_ctx(w, run), preferred="best", criteria=crit, **GOOD)
    # A run the judgment never launched cannot answer it.
    stranger = Run(id=uuid.uuid4(), project_id=w.project.id, harness_id=w.harness.id, task_type="freeform",
                   task_input={"_pairwise_judgment_id": str(row.id), "_pairwise_order": "low_first"},
                   document_ids=[], status="running")
    w.db.add(stranger)
    await w.db.commit()
    with pytest.raises(ToolError, match="not the judge"):
        await submit_pairwise(_ctx(w, stranger), preferred="first", criteria=crit, **GOOD)
    assert "recorded" in await submit_pairwise(_ctx(w, run), preferred="first", criteria=crit, **GOOD)
    with pytest.raises(ToolError, match="already answered"):
        await submit_pairwise(_ctx(w, run), preferred="second", criteria=crit, **GOOD)
    await w.db.refresh(row)
    assert row.calls[0]["preferred"] == "first" and row.calls[0]["criteria"] == crit


async def test_submit_pairwise_refuses_a_judgment_that_is_not_running(w):
    row, run = await _armed(w, status="skipped")
    with pytest.raises(ToolError, match="no longer waiting"):
        await submit_pairwise(_ctx(w, run), preferred="first", criteria={k: "first" for k in CRITERIA}, **GOOD)


def test_the_tool_is_registered_and_on_the_evaluator_harness_only():
    from tret.engine.evaluation_tools import ENGINE_STAMPED_KEYS, EVALUATION_TOOLS
    from tret.engine.tools import get_builtin_tools
    from tret.services.workspace import EVALUATOR_HARNESS_TOOLS, seed_chat_harness  # noqa: F401

    assert "submit_pairwise" in get_builtin_tools() and "submit_pairwise" in EVALUATION_TOOLS
    assert "submit_pairwise" in EVALUATOR_HARNESS_TOOLS
    assert "_pairwise_judgment_id" in ENGINE_STAMPED_KEYS and "_pairwise_order" in ENGINE_STAMPED_KEYS


# ── compare overlay ──────────────────────────────────────────────────────────
class QueryCounter:
    def __init__(self, engine):
        self.n = 0
        event.listen(engine.sync_engine, "before_cursor_execute", self._hit)
        self.engine = engine

    def _hit(self, *a, **k):
        self.n += 1

    def stop(self):
        event.remove(self.engine.sync_engine, "before_cursor_execute", self._hit)


async def test_compare_carries_pairwise_per_row_and_in_the_summary_with_a_constant_query_count(w):
    a, b, _, _ = await pair(w)
    counter = QueryCounter(w.db.bind)
    before_n = counter.n
    empty = await scoring.compare_batches(w.db, a, b)
    empty_queries = counter.n - before_n
    assert empty["summary"]["pairwise"] == {
        "eligible_near_ties": 2, "judged": 0, "wins_a": 0, "wins_b": 0, "ties": 0, "inconsistent": 0,
        "running": False, "cost_usd": 0,
    }
    assert all(r["pairwise"] is None for r in empty["rows"])
    await pairwise.start_pairwise(w.db, a, b, user_id=w.user.id, scope="both_delivered")
    await drain(w)
    before_n = counter.n
    cmp = await scoring.compare_batches(w.db, a, b)
    full_queries = counter.n - before_n
    counter.stop()
    assert full_queries == empty_queries
    rows = {r["row_key"]: r["pairwise"] for r in cmp["rows"]}
    assert rows["acme"] == {"status": "judged", "verdict": "a", "strength": "clear", "stale": False}
    assert rows["globex"]["verdict"] == "b" and rows["initech"]["verdict"] == "tie"
    s = cmp["summary"]["pairwise"]
    assert (s["judged"], s["wins_a"], s["wins_b"], s["ties"], s["inconsistent"], s["running"]) == (3, 1, 1, 1, 0, False)
    assert s["cost_usd"] == pytest.approx(0.12)
    # Scores are untouched by any verdict.
    assert {r["row_key"]: (r["a"]["quality"], r["b"]["quality"]) for r in cmp["rows"]}["acme"] == (80.0, 82.0)
    # Mirrored request: the verdicts are oriented to the requested a / b.
    mirror = await scoring.compare_batches(w.db, b, a)
    assert {r["row_key"]: r["pairwise"]["verdict"] for r in mirror["rows"]}["acme"] == "b"


async def test_compare_has_no_pairwise_summary_when_not_comparable(w):
    a, _, _ = await scored(w, "A", "a", [("acme", "completed", 80, "x")])
    other, _, _ = await scored(w, "O", "o", [("acme", "completed", 80, "x")], judge="anthropic/claude-sonnet-5-5")
    cmp = await scoring.compare_batches(w.db, a, other)
    assert cmp["quality_comparable"] is False and cmp["summary"]["pairwise"] is None
    assert all(r["pairwise"] is None for r in cmp["rows"])


# ── the API ──────────────────────────────────────────────────────────────────
@pytest.fixture
async def api(w):
    app = FastAPI()
    app.include_router(batches_api.router)
    role = {"v": "approver"}

    async def _db():
        async with w.factory() as session:
            yield session

    async def _ws():
        return WorkspaceContext(Workspace(id=w.ws.id, name=w.ws.name, kind="team"), role["v"])

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[current_user] = lambda: w.user
    app.dependency_overrides[current_workspace] = _ws
    from tret.api.workspace import require_workspace_approver

    async def _approver():
        if role["v"] not in ("approver", "admin", "owner"):
            raise HTTPException(403, "approver required")
        return await _ws()

    app.dependency_overrides[require_workspace_approver] = _approver
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        c.role = role
        yield c


async def test_the_pairwise_api_shapes_roles_and_isolation(w, api):
    a, b, _, _ = await pair(w)
    foreign = Batch(id=uuid.uuid4(), workspace_id=w.other_ws.id, project_id=w.other_project.id, name="X",
                    run_slug="x-1", status="draft", settings={"fiscal_year": 2025}, created_at=T0)
    w.db.add(foreign)
    await w.db.commit()
    # validation
    r = await api.post("/api/batches/compare/pairwise", json={"a": str(a.id), "b": str(a.id)})
    assert r.status_code == 422
    assert (await api.post("/api/batches/compare/pairwise", json={"a": str(a.id), "b": str(foreign.id)})).status_code == 404
    assert (await api.get(f"/api/batches/compare/pairwise?a={a.id}&b={foreign.id}")).status_code == 404
    assert (await api.post("/api/batches/compare/pairwise/cancel", json={"a": str(a.id), "b": str(foreign.id)})).status_code == 404
    bad = await api.post("/api/batches/compare/pairwise", json={"a": str(a.id), "b": str(b.id), "max_pairs": 0})
    assert bad.status_code == 422
    unscored = await mk_batch(w, "U", "u")
    assert (await api.post("/api/batches/compare/pairwise", json={"a": str(a.id), "b": str(unscored.id)})).status_code == 422
    api.role["v"] = "analyst"
    assert (await api.post("/api/batches/compare/pairwise", json={"a": str(a.id), "b": str(b.id)})).status_code == 403
    assert (await api.get(f"/api/batches/compare/pairwise?a={a.id}&b={b.id}")).status_code == 200
    api.role["v"] = "approver"

    # shapes
    started = await api.post("/api/batches/compare/pairwise", json={"a": str(a.id), "b": str(b.id), "scope": "both_delivered"})
    assert started.status_code == 200, started.text
    body = started.json()
    assert set(body) == {"a", "b", "status", "scope_eligible", "judged", "pending", "running", "failed", "skipped",
                         "stale", "verdicts", "cost_usd", "budget_usd"}
    assert body["status"] == "running" and body["scope_eligible"] == {"near_ties": 2, "both_delivered": 3}
    assert "rows" not in body
    await drain(w)
    got = (await api.get(f"/api/batches/compare/pairwise?a={a.id}&b={b.id}")).json()
    assert got["status"] == "idle" and got["judged"] == 3 and len(got["rows"]) == 3
    row = got["rows"][0]
    assert set(row) == {"row_key", "label", "status", "verdict", "strength", "stale", "calls", "cost_usd", "error",
                        "finished_at"}
    assert row["status"] in ("pending", "running", "judged", "failed", "skipped")
    assert set(row["calls"][0]) == {"order", "preferred", "criteria", "confidence", "rationale"}
    # the cancel endpoint returns the GET's status object (rows included only on GET)
    cancelled = await api.post("/api/batches/compare/pairwise/cancel", json={"a": str(a.id), "b": str(b.id)})
    assert cancelled.status_code == 200
    assert set(cancelled.json()) == set(body) and cancelled.json()["status"] == "idle"
    # the deprecated alias serves the same routes; static routes win over /{collection_id}
    assert (await api.get(f"/api/collections/compare/pairwise?a={a.id}&b={b.id}")).status_code == 200
    # the compare route carries the overlay (summary block always present when comparable)
    cmp = (await api.get(f"/api/batches/compare?a={a.id}&b={b.id}")).json()
    assert cmp["summary"]["pairwise"]["judged"] == 3 and cmp["rows"][0]["pairwise"]["status"] == "judged"
    unc = await api.get(f"/api/batches/compare?a={a.id}&b={unscored.id}")
    assert unc.json()["summary"]["pairwise"] is None


# ── round 2: races, long tasks, prompt profile, doctrine, rubric version ─────
from tests.test_batch_cross_check import mk_batch as _mk_batch  # noqa: E402,F401
from tret.db.models import Document  # noqa: E402


async def prompt_batch(w, name, slug, template, answer, *, quality=70):
    b = Batch(
        id=uuid.uuid4(), workspace_id=w.ws.id, project_id=w.project.id, name=name, run_slug=slug,
        status="completed", profile="prompt", settings={"template": template, "harness_id": str(w.harness.id)},
        warnings=[], created_at=T0,
    )
    w.db.add(b)
    await w.db.commit()
    ev = await mk_pass(w, b, rubric=default_rubric("prompt"))
    t = await mk_target(w, b, "acme", "completed", outcome={"answer": answer})
    t.task_input = {"company": "Acme"}
    t.label = "Acme"
    await mk_row(w, ev, b, t, quality)
    return b, t


async def mk_docs(w, n):
    out = []
    for i in range(n):
        d = Document(
            id=uuid.uuid4(), project_id=w.project.id, filename=f"doc{i}.pdf", content_type="application/pdf",
            byte_size=1, storage_path="x", sha256=f"s{i}",
        )
        w.db.add(d)
        out.append(d)
    await w.db.commit()
    return out


async def test_prompt_profile_pairwise_evidence_task_and_attachment_cap(w):
    a, ta = await prompt_batch(w, "A", "pa", "Assess {company} carefully.", f"Answer from {MODEL} with $5 spent")
    b, tb = await prompt_batch(w, "B", "pb", "Assess {company} carefully.", "Other answer")
    docs = await mk_docs(w, 6)
    ra, rb = await w.db.get(Run, ta.run_id), await w.db.get(Run, tb.run_id)
    ra.document_ids = [d.id for d in docs]  # six inputs on A: only four may be shown
    rb.document_ids = [docs[0].id, docs[1].id]  # two of them shared with B
    ra.model_used = MODEL
    await w.db.commit()
    from tret.batches.profiles import profile_for

    prof = profile_for(a)
    assert prof.pairwise_task(a, ta) == "Assess Acme carefully."
    text, ids = await prof.pairwise_evidence(w.db, a, ta, ra, [])
    assert "Answer from [redacted]" in text and MODEL not in text and len(ids) == 4
    ev = await pairwise.scoring.current_pass(w.db, a.id)
    msg, doc_ids = await pairwise.build_message(w.db, ev.rubric, a, ta, b, tb, a, ta)
    assert len(doc_ids) == len(set(doc_ids)) == 4  # d1-d4 of A, B's two are among them
    assert msg.count("given to both runs") == 2 and "attached to Output 1" in msg
    assert docs[4].id.hex not in msg.replace("-", "") and str(docs[4].id) not in msg


async def test_a_very_long_task_does_not_push_out_the_outputs_or_the_instruction(w):
    template = "Assess {company}. " + "background " * 4100  # about 45,000 characters
    a, ta = await prompt_batch(w, "A", "la", template, "Answer A " * 50)
    b, tb = await prompt_batch(w, "B", "lb", template, "Answer B " * 50)
    ev = await pairwise.scoring.current_pass(w.db, a.id)
    ta.task_input = {"company": "Acme"}
    msg, _ = await pairwise.build_message(w.db, ev.rubric, a, ta, b, tb, a, ta)
    assert len(pairwise.profile_for(a).pairwise_task(a, ta)) > 40_000
    assert "## Output 1" in msg and "## Output 2" in msg and "[task cut at the size limit]" in msg
    assert "submit_pairwise" in msg and pairwise.INSTRUCTION in msg and len(msg) <= pairwise.MESSAGE_LIMIT
    assert "do not call submit_evaluation" in msg


async def test_an_old_run_finishing_after_cancel_and_restart_does_not_fail_the_new_judgment(w):
    a, b, _, _ = await pair(w, [("acme", "completed", 80, "BETTER a")], [("acme", "completed", 82, "b")])
    w.eng.mode = "hang"
    await pairwise.start_pairwise(w.db, a, b, user_id=w.user.id)
    for _ in range(100):
        if w.eng.inputs:
            break
        await asyncio.sleep(0.02)
    old_run = w.eng.inputs[0]
    old_tasks = list(w.tasks)
    await pairwise.cancel_pairwise(w.db, a, b)
    # Start again straight away while the old run is still hanging, then let both finish.
    w.eng.mode = "ok"
    w.tasks.clear()
    await pairwise.start_pairwise(w.db, a, b, user_id=w.user.id)
    await asyncio.gather(*w.tasks, *old_tasks)
    (row,) = await rows_of(w)
    assert old_run["_pairwise_judgment_id"] == str(row.id)
    assert row.status == "judged" and row.verdict in ("low", "high")
    assert len([c for c in row.calls if c["preferred"]]) == 2


async def test_two_runners_cannot_both_take_a_row(w):
    a, b, _, _ = await pair(w, [("acme", "completed", 80, "BETTER a")], [("acme", "completed", 82, "b")])
    w.tasks.clear()
    await pairwise.start_pairwise(w.db, a, b, user_id=w.user.id)
    (row,) = await rows_of(w)
    await asyncio.gather(*w.tasks, pairwise._judge_pair(row.id), pairwise._judge_pair(row.id))
    assert len(w.eng.inputs) == 2  # one claim, two ordered calls


async def test_the_sweep_judges_a_row_whose_two_calls_were_answered_before_the_restart(w):
    a, b, ta, tb = await pair(w)
    low, high = pairwise.orient(ta["acme"], tb["acme"])
    done = {"preferred": "first", "criteria": {}, "confidence": "high", "rationale": "r"}
    row = PairwiseJudgment(
        id=uuid.uuid4(), workspace_id=w.ws.id, family_key="f", rubric_key="k", rubric_version="v", judge_model=JUDGE,
        row_key="acme", batch_low_id=low.collection_id, batch_high_id=high.collection_id, target_low_id=low.id,
        target_high_id=high.id, status="running", created_at=T0,
        calls=[{**done, "order": "low_first", "evaluator_run_id": str(uuid.uuid4())},
               {**done, "preferred": "second", "order": "high_first", "evaluator_run_id": str(uuid.uuid4())}],
    )
    w.db.add(row)
    await w.db.commit()
    assert await pairwise.sweep_orphaned_pairwise(w.db) == 0
    await w.db.refresh(row)
    assert (row.status, row.verdict, row.strength) == ("judged", "low", "clear")


async def test_a_changed_rubric_fails_the_judgment_with_a_clear_error(w):
    a, b, ta, tb = await pair(w, [("acme", "completed", 80, "BETTER a")], [("acme", "completed", 82, "b")])
    low, high = pairwise.orient(ta["acme"], tb["acme"])
    row = PairwiseJudgment(
        id=uuid.uuid4(), workspace_id=w.ws.id, family_key="f", rubric_key=RUBRIC["key"],
        rubric_version="an-older-version", judge_model=JUDGE, row_key="acme", batch_low_id=low.collection_id,
        batch_high_id=high.collection_id, target_low_id=low.id, target_high_id=high.id, status="pending",
        calls=[], created_at=T0,
    )
    w.db.add(row)
    await w.db.commit()
    await pairwise._judge_pair(row.id)
    await w.db.refresh(row)
    assert row.status == "failed" and "rubric changed" in row.error and w.eng.inputs == []


async def test_guards_point_each_tool_at_the_right_run_kind(w):
    from tret.engine.evaluation_tools import submit_evaluation

    row, run = await _armed(w)
    with pytest.raises(ToolError, match="submit_pairwise"):
        await submit_evaluation(_ctx(w, run), scores={}, difficulty=3, confidence="low", rationale="r")
    plain = Run(id=uuid.uuid4(), project_id=w.project.id, harness_id=w.harness.id, task_type="freeform",
                task_input={"_target_evaluation_id": str(uuid.uuid4())}, document_ids=[], status="running")
    w.db.add(plain)
    await w.db.commit()
    with pytest.raises(ToolError, match="submit_evaluation"):
        await submit_pairwise(_ctx(w, plain), preferred="first", criteria={}, **GOOD)
    # criteria are checked against the rubric of the stored version
    row.rubric_version = "gone"
    await w.db.commit()
    with pytest.raises(ToolError, match="rubric"):
        await submit_pairwise(_ctx(w, run), preferred="first", criteria={k: "first" for k in CRITERIA}, **GOOD)


async def test_the_evaluator_doctrine_is_upgraded_only_when_unedited(w):
    from tret.services.workspace import (
        _PREVIOUS_EVALUATOR_DOCTRINES,
        EVALUATOR_DOCTRINE,
        ensure_evaluator_harness,
    )

    assert "submit_pairwise" not in _PREVIOUS_EVALUATOR_DOCTRINES[0] and _PREVIOUS_EVALUATOR_DOCTRINES[0] != EVALUATOR_DOCTRINE
    h = await ensure_evaluator_harness(w.db, w.ws.id)
    h.system_prompt_extra = _PREVIOUS_EVALUATOR_DOCTRINES[0]
    await w.db.commit()
    await ensure_evaluator_harness(w.db, w.ws.id)
    assert h.system_prompt_extra == EVALUATOR_DOCTRINE
    h.system_prompt_extra = "operator's own words"
    await w.db.commit()
    await ensure_evaluator_harness(w.db, w.ws.id)
    assert h.system_prompt_extra == "operator's own words"


async def test_get_pairwise_with_the_same_batch_twice_is_422(w, api):
    a, _, _ = await scored(w, "A", "a", [("acme", "completed", 80, "x")])
    assert (await api.get(f"/api/batches/compare/pairwise?a={a.id}&b={a.id}")).status_code == 422


async def test_an_old_runner_between_its_two_calls_appends_nothing_after_a_restart(w, monkeypatch):
    a, b, _, _ = await pair(w, [("acme", "completed", 80, "BETTER a")], [("acme", "completed", 82, "b")])
    pairwise._state.update(loop=asyncio.get_running_loop(), sem=asyncio.Semaphore(2))
    reached, release = asyncio.Event(), asyncio.Event()
    real = pairwise.build_message
    calls = {"n": 0}

    async def gated(*args, **kw):
        calls["n"] += 1
        if calls["n"] == 2:  # the old runner's second order
            reached.set()
            await release.wait()
        return await real(*args, **kw)

    monkeypatch.setattr(pairwise, "build_message", gated)
    await pairwise.start_pairwise(w.db, a, b, user_id=w.user.id)
    old_tasks = list(w.tasks)
    await asyncio.wait_for(reached.wait(), 10)  # order 1 is answered; the old runner is building order 2
    await pairwise.cancel_pairwise(w.db, a, b)
    w.tasks.clear()
    await pairwise.start_pairwise(w.db, a, b, user_id=w.user.id)
    await asyncio.gather(*w.tasks)  # the new runner claims the row and finishes both of its calls
    release.set()
    await asyncio.gather(*old_tasks)
    (row,) = await rows_of(w)
    answered = [c for c in row.calls if c["preferred"]]
    assert row.status == "judged" and len(row.calls) == 2 and len(answered) == 2
    assert len(w.eng.inputs) == 3  # the old order-1 run plus the new pass's two; the old order 2 never ran
