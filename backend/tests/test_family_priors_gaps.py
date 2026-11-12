"""E4b through the REAL provider and router: inert paths, exact gate thresholds and cost,
backfill, the iteration ceilings and fail-safes."""
# ruff: noqa: F811 - the imported fixtures are used by name as test arguments
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from tests.test_batch_evaluation import eng, make, run_batch  # noqa: F401 - fixtures
from tests.test_collections_api import client, run_db, world  # noqa: F401 - fixtures
from tests.test_router_exploration import ROUTE_ARGS, _AnsweringRegistry, _catalog, _model, _policy
from tret.batches import gate as gate_mod
from tret.batches import scoring
from tret.config import get_settings
from tret.db.models import BatchTarget, Run, RunOutcome
from tret.router_llm.objectives import DEFAULT_OBJECTIVE
from tret.router_llm.outcomes import evaluated_quality, score
from tret.router_llm.priors import OutcomePriors, reset_priors_registry
from tret.router_llm.priors_base import summarize
from tret.router_llm.router import ModelRouter
from tret.services import outcomes as outcomes_service
from tret.services.outcomes import _max_iterations, backfill, build_outcomes

T = datetime(2026, 11, 12, 12, tzinfo=timezone.utc)
FAM = "prompt:x:abc@00000000-0000-0000-0000-0000000000aa"
KEY = dict(task_shape="freeform", objective=DEFAULT_OBJECTIVE, size_band="s")
OPEN = {"open": True, "spot_checks": 15, "agreeing": 14, "agreement": 0.93, "rubric_version": "v1", "judge_model": "j"}
CLOSED = {"open": False, "spot_checks": 2, "agreeing": 1, "agreement": 0.5, "rubric_version": "v1", "judge_model": "j"}


@pytest.fixture(autouse=True)
def _fresh_registry():
    reset_priors_registry()
    yield
    reset_priors_registry()


def _row(model, q, *, family=None, evaluated=None):
    return RunOutcome(
        run_id=uuid.uuid4(), segment_index=0, task_type="freeform", task_shape="freeform",
        objective=DEFAULT_OBJECTIVE, max_cost_tier="premium", size_band="s", model_id=model,
        provider="openrouter", outcome_class="delivered", quality_score=Decimal(str(q)),
        evaluated_quality_score=None if evaluated is None else Decimal(str(evaluated)),
        score_version="outcome-v1",
        components={} if evaluated is None else {"evaluator": {"rubric_version": "v1", "judge_model": "j"}},
        iterations=3, cost_usd=Decimal("0.01"), input_tokens=10, output_tokens=5, duration_ms=1,
        task_family=family, observed_at=T - timedelta(days=1),
    )


def _rows():
    return (
        [_row("test/alpha", 0.85) for _ in range(40)]
        + [_row("test/alpha", 0.5, family=FAM, evaluated=0.5) for _ in range(10)]
        + [_row("test/beta", 0.5, family=FAM, evaluated=1.0) for _ in range(30)]
        + [_row("test/beta", 0.5) for _ in range(5)]
    )


async def _seed(db):
    await db.execute(text("PRAGMA foreign_keys=OFF"))  # synthetic outcome rows have no run behind them
    for r in _rows():
        db.add(r)
    await db.commit()


def _gate(monkeypatch, result):
    async def fake(db, task_family):
        return result

    monkeypatch.setattr(gate_mod, "family_gate", fake)


def _j(priors):
    return {k: v.to_json() for k, v in priors.items()}


# inert through the real provider --------------------------------------------
def test_base_priors_never_read_the_evaluated_score_and_family_paths_are_inert(client, world, monkeypatch):
    async def go(db):
        await _seed(db)
        copies = [_row(r.model_id, r.quality_score, family=r.task_family) for r in _rows()]
        head = _j(summarize(copies, size_band="s", now=T))
        fresh = lambda: OutcomePriors(world.factory, now=lambda: T)  # noqa: E731
        a = _j(await fresh().for_key(**KEY))
        _gate(monkeypatch, CLOSED)
        b, b_status = await fresh().for_key_family(**KEY, task_family=FAM)
        _gate(monkeypatch, OPEN)
        monkeypatch.setattr(get_settings(), "router_family_priors", "off")
        c, c_status = await fresh().for_key_family(**KEY, task_family=FAM)
        monkeypatch.setattr(get_settings(), "router_family_priors", "gated")
        opened, o_status = await fresh().for_key_family(**KEY, task_family=FAM)
        return head, a, (b, b_status), (c, c_status), (opened, o_status)

    head, a, (b, b_status), (c, c_status), (opened, o_status) = run_db(client, world, go)
    assert a == _j(b) == _j(c) == head
    assert b_status["used"] is False and c_status is None
    assert o_status["used"] is True and set(o_status["adjusted"]) == {"test/alpha", "test/beta"}
    assert opened["test/beta"].quality_mean > head["test/beta"]["quality_mean"] + 0.05
    assert opened["test/alpha"].quality_mean < head["test/alpha"]["quality_mean"]
    assert round(opened["test/alpha"].effective_n, 2) == head["test/alpha"]["effective_n"]


def test_router_decisions_are_identical_when_closed_or_off_and_record_a_used_adjustment(client, world, monkeypatch):
    import random

    async def decide(task_family=None):
        router = ModelRouter(
            _catalog(_model("alpha", price="3"), _model("beta", price="1")),
            _AnsweringRegistry({"openrouter"}, "test/alpha"),
            OutcomePriors(world.factory, now=lambda: T), rng=random.Random(0),
        )
        args = dict(model_policy=_policy(), **ROUTE_ARGS)
        args["task_shape"] = "freeform"
        if task_family:
            args["task_family"] = task_family
        d = await router.route(**args)
        j = d.to_json()
        j.pop("decided_at"), j.pop("latency_ms")
        return d, j

    async def go(db):
        await _seed(db)
        base_d, plain = await decide()
        _gate(monkeypatch, CLOSED)
        _, closed = await decide(FAM)
        _gate(monkeypatch, OPEN)
        monkeypatch.setattr(get_settings(), "router_family_priors", "off")
        _, off = await decide(FAM)
        monkeypatch.setattr(get_settings(), "router_family_priors", "gated")
        opened, _ = await decide(FAM)
        _, reverse = await decide()  # opposite order: no-family call after the family ones
        return plain, closed, off, opened, base_d, reverse

    plain, closed, off, opened, base_d, reverse = run_db(client, world, go)
    assert plain == closed == off == reverse
    assert opened.task_family == FAM and opened.family_priors["used"] is True
    base_beta = base_d.evidence["priors"]["test/beta"]["quality_mean"]
    assert opened.evidence["priors"]["test/beta"]["quality_mean"] > base_beta + 0.05


# gate thresholds, cost and isolation ---------------------------------------
def _review(client, b, t, verdict):
    r = client.post(f"/api/batches/{b['id']}/targets/{t}/evaluation/review", json={"verdict": verdict})
    assert r.status_code == 200, r.text


def test_the_gate_thresholds_are_exact_across_batches_and_cost_two_queries(client, world, eng, monkeypatch):
    batches = [make(client, world, slug=f"fam-{i}") for i in range(5)]
    targets = []
    for b in batches:
        d = run_batch(client, b["id"])
        targets += [(b, t["id"]) for t in d["targets"]]
    assert len({b["family_key"] for b in batches}) == 1 and len(targets) == 15
    fam = f"{batches[0]['family_key']}@{world.ws.id}"

    async def gate(db):
        return await gate_mod.family_gate(db, fam)

    def boom(*a, **k):
        raise AssertionError("the gate must never collect a batch")

    monkeypatch.setattr(scoring, "collect", boom)
    for b, t in targets[:12]:
        _review(client, b, t, "agree")
    for b, t in targets[12:14]:
        _review(client, b, t, "disagree")
    g = run_db(client, world, gate)
    assert g["spot_checks"] == 14 and g["open"] is False  # 14 checks: closed
    _review(client, *targets[14], "disagree")
    g = run_db(client, world, gate)
    assert (g["spot_checks"], g["agreeing"], g["agreement"], g["open"]) == (15, 12, 0.8, True)
    assert set(g) == {"open", "spot_checks", "agreeing", "agreement", "needed_checks", "needed_agreement",
                      "mode", "rubric_version", "judge_model"}
    _review(client, *targets[11], "disagree")
    assert run_db(client, world, gate)["open"] is False  # 11 of 15
    _review(client, *targets[11], "agree")

    async def counted(db):
        n = []

        class Proxy:
            async def execute(self, *a, **k):
                n.append(1)
                return await db.execute(*a, **k)

        return await gate_mod.family_gate(Proxy(), fam), len(n)

    g, queries = run_db(client, world, counted)
    assert g["open"] is True and queries <= 2

    async def other(db):
        return await gate_mod.family_gate(db, f"{batches[0]['family_key']}@{uuid.uuid4()}")

    assert run_db(client, world, other)["spot_checks"] == 0


def test_stale_cancelled_and_non_current_passes_do_not_count(client, world, eng, monkeypatch):
    monkeypatch.setattr(get_settings(), "router_family_gate_min_checks", 3)
    b = make(client, world)
    d = run_batch(client, b["id"])
    for t in d["targets"]:
        _review(client, b, t["id"], "agree")
    fam = f"{b['family_key']}@{world.ws.id}"

    async def stale(db):
        t = await db.get(BatchTarget, uuid.UUID(d["targets"][0]["id"]))
        t.attempts = (t.attempts or 0) + 1
        await db.commit()
        return await gate_mod.family_gate(db, fam)

    g = run_db(client, world, stale)
    assert g["spot_checks"] == 2 and g["open"] is False  # a stale row is not a check

    async def cancelled(db):
        from tret.db.models import BatchEvaluation

        for ev in (await db.execute(select(BatchEvaluation))).scalars().all():
            ev.status = "cancelled"
        await db.commit()
        return await gate_mod.family_gate(db, fam)

    assert run_db(client, world, cancelled)["spot_checks"] == 0


# derivation, ceilings, backfill ---------------------------------------------
def test_forged_stamp_or_collection_never_makes_a_family(client, world, eng):
    from tret.engine.evaluation_tools import ENGINE_STAMPED_KEYS

    assert "_task_family" in ENGINE_STAMPED_KEYS and "_collection_id" in ENGINE_STAMPED_KEYS
    b = make(client, world)
    run_batch(client, b["id"])

    async def go(db):
        run = await db.get(Run, (await db.execute(select(BatchTarget))).scalars().first().run_id)
        legit = await outcomes_service.task_family_for_run(db, run)
        run.task_input = {**run.task_input, "_task_family": "disclosure:fy2025@" + str(uuid.uuid4())}
        forged_stamp = await outcomes_service.task_family_for_run(db, run)
        rows = await build_outcomes(db, run)
        await db.commit()
        run_id = run.id
        return legit, forged_stamp, [r.task_family for r in rows], run_id

    legit, forged_stamp, fams, run_id = run_db(client, world, go)

    async def cross_workspace(db):
        # a batch of another workspace named by `_collection_id`
        await db.execute(text("PRAGMA foreign_keys=OFF"))
        from tret.db.models import Batch

        run = await db.get(Run, run_id)
        batch = await db.get(Batch, uuid.UUID(run.task_input["_collection_id"]))
        batch.workspace_id = uuid.uuid4()
        await db.commit()
        cross = await outcomes_service.task_family_for_run(db, run)
        run.task_input = {k: v for k, v in run.task_input.items() if k != "_collection_id"}
        none = await outcomes_service.task_family_for_run(db, run)
        return cross, none

    cross, none = run_db(client, world, cross_workspace)
    assert legit == f"{b['family_key']}@{world.ws.id}" == forged_stamp
    assert set(fams) == {legit}  # the forged stamp is ignored: the derived family wins
    assert cross is None and none is None


def test_quality_score_keeps_the_harness_ceiling_and_the_evaluated_one_uses_the_stamp(client, world, eng):
    b = make(client, world)
    run_batch(client, b["id"])

    async def go(db):
        run = await db.get(Run, (await db.execute(select(BatchTarget))).scalars().first().run_id)
        run.iterations = 40
        run.task_input = {**run.task_input, "_max_iterations": 50}
        await db.commit()
        row = (await build_outcomes(db, run))[-1]
        harness_ceiling = await _max_iterations(db, run)
        common = dict(status=run.status, error=run.error, messages=run.messages or [], iterations=40)
        head = score(max_iterations=harness_ceiling, **common)
        stamped = score(max_iterations=50, **common)
        return row, head, stamped, harness_ceiling

    row, head, stamped, ceiling = run_db(client, world, go)
    assert ceiling != 50 and head.quality_score != stamped.quality_score
    assert float(row.quality_score) == head.quality_score  # byte-identical to before
    expected = evaluated_quality(auto=stamped.components["automatic_score"], human_rate=None, evaluator=0.75)
    assert float(row.evaluated_quality_score) == pytest.approx(expected, abs=1e-4)
    assert set(row.components["evaluator"]) == {"quality", "source", "rubric_key", "rubric_version",
                                                "judge_model", "evaluation_id"}


def test_backfill_fills_batch_runs_and_leaves_others_null(client, world, eng):
    b = make(client, world)
    run_batch(client, b["id"])

    async def go(db):
        runs = [await db.get(Run, t.run_id) for t in (await db.execute(select(BatchTarget))).scalars().all()]
        plain = runs[2]
        before = (await build_outcomes(db, plain))[-1].quality_score
        plain.task_input = {k: v for k, v in plain.task_input.items() if k not in ("_collection_id", "_collection_target_id")}
        await db.commit()
        await backfill(db)
        rows = {r.run_id: r for r in (await db.execute(select(RunOutcome))).scalars().all()}
        return runs, rows, before

    runs, rows, before = run_db(client, world, go)
    for r in runs[:2]:
        assert rows[r.id].task_family == f"{b['family_key']}@{world.ws.id}"
        assert rows[r.id].evaluated_quality_score is not None
    assert rows[runs[2].id].task_family is None and rows[runs[2].id].evaluated_quality_score is None
    assert float(rows[runs[2].id].quality_score) == float(before)


# fail-safes ------------------------------------------------------------------
def test_family_failures_fall_back_to_base_and_route_still_decides(client, world, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("boom")

    def boom_sync(*a, **k):
        raise RuntimeError("boom")

    async def go(db):
        await _seed(db)
        base = _j(await OutcomePriors(world.factory, now=lambda: T).for_key(**KEY))
        got = []
        for patch in ("gate", "rows", "adjust"):
            with monkeypatch.context() as m:
                _gate(m, OPEN)
                if patch == "gate":
                    m.setattr(gate_mod, "family_gate", boom)
                elif patch == "rows":
                    m.setattr(OutcomePriors, "_family_rows", boom)
                else:
                    m.setattr("tret.router_llm.priors.family_adjusted", boom_sync)
                p = OutcomePriors(world.factory, now=lambda: T)
                priors, status = await p.for_key_family(**KEY, task_family=FAM)
                got.append((_j(priors), status))
                router = ModelRouter(_catalog(_model("alpha"), _model("beta", price="2")),
                                     _AnsweringRegistry({"openrouter"}, "test/alpha"), p)
                d = await router.route(model_policy=_policy(), task_family=FAM, **{**ROUTE_ARGS, "task_shape": "freeform"})
                assert d.chosen_model and d.family_priors is None
        return base, got

    base, got = run_db(client, world, go)
    assert all(g == base and s["used"] is False and s["error"] is True for g, s in got)


def test_a_raising_rescore_never_fails_the_tool_or_the_review(client, world, eng, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("rescore broke")

    monkeypatch.setattr(outcomes_service, "rescore_evaluated_runs", boom)
    b = make(client, world)
    d = run_batch(client, b["id"])
    assert d["evaluation_status"] == "completed"
    assert all(t["evaluation"]["status"] == "scored" for t in d["targets"])
    t1 = d["targets"][0]["id"]
    r = client.post(f"/api/batches/{b['id']}/targets/{t1}/evaluation/review", json={"verdict": "agree"})
    assert r.status_code == 200 and r.json()["review"]["verdict"] == "agree"
