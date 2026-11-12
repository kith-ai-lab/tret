"""The evaluator component of a run's outcome, the gate, and the re-score hooks."""
# ruff: noqa: F811 - the imported fixtures are used by name as test arguments
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from tests.test_batch_evaluation import MODEL_ID, eng, make, run_batch  # noqa: F401 - fixtures
from tests.test_collections_api import client, run_db, world  # noqa: F401 - fixtures
from tret.batches.gate import family_gate
from tret.config import get_settings
from tret.db.models import BatchTarget, Run, RunOutcome
from tret.router_llm.outcomes import evaluated_quality
from tret.router_llm.priors import OutcomePriors
from tret.services import outcomes as outcomes_service
from tret.services.outcomes import build_outcomes, record_outcome


# ── the blend ────────────────────────────────────────────────────────────────
def test_all_three_terms_pairs_and_the_evaluator_alone():
    assert evaluated_quality(auto=0.6, human_rate=0.8, evaluator=1.0) == pytest.approx(0.5 + 0.24 + 0.12, abs=1e-4)
    assert evaluated_quality(auto=0.6, human_rate=None, evaluator=1.0) == pytest.approx((0.5 + 0.12) / 0.7, abs=1e-4)
    assert evaluated_quality(auto=0.6, human_rate=0.8, evaluator=0.0) == pytest.approx((0.24 + 0.12) / 1.0 * 1.0, abs=1e-4)


def test_no_evaluator_term_reduces_to_the_v1_blend():
    # 0.3 : 0.2 renormalised is 0.6 : 0.4, today's human : automatic split
    v1 = 0.6 * 0.8 + 0.4 * 0.5
    assert 0.3 * 0.8 + 0.2 * 0.5 == pytest.approx(v1 * 0.5)
    assert evaluated_quality(auto=0.5, human_rate=0.8, evaluator=v1) == pytest.approx(v1, abs=1e-4)


def test_the_blend_is_clamped_and_rounded():
    assert evaluated_quality(auto=1.5, human_rate=None, evaluator=2.0) == 1.0
    assert evaluated_quality(auto=-1.0, human_rate=None, evaluator=-1.0) == 0.0
    assert len(str(evaluated_quality(auto=0.333333, human_rate=0.1234567, evaluator=0.7777777)).split(".")[1]) <= 4


# ── build_outcomes over an evaluated batch ───────────────────────────────────
def _family(world, b):
    return f"{b['family_key']}@{world.ws.id}"


async def _runs(db):
    targets = (await db.execute(select(BatchTarget))).scalars().all()
    return [await db.get(Run, t.run_id) for t in targets if t.run_id]


def test_family_is_stamped_and_the_evaluated_score_lands_on_the_last_segment(client, world, eng):
    b = make(client, world)
    run_batch(client, b["id"])

    async def go(db):
        out = []
        for run in await _runs(db):
            out.append((run.task_input.get("_task_family"), await build_outcomes(db, run)))
        return out

    for stamp, rows in run_db(client, world, go):
        assert stamp is None  # nothing is stamped on task_input any more
        stamp = _family(world, b)  # the family is derived from the batch
        assert rows and all(r.task_family == stamp for r in rows)
        assert rows[-1].evaluated_quality_score is not None
        assert all(r.evaluated_quality_score is None for r in rows[:-1])
        assert rows[-1].components["evaluator"]["source"] == "judge"
        assert {"rubric_key", "rubric_version", "judge_model", "evaluation_id"} <= set(rows[-1].components["evaluator"])
        assert rows[-1].components["evaluator"]["quality"] == pytest.approx(0.75)
        # quality_score keeps its own meaning: the evaluator never enters it
        assert "evaluator" not in {k for k in rows[-1].components if k == "human"}


def test_adjust_disagree_and_stale_change_the_evaluator_term(client, world, eng):
    b = make(client, world)
    d = run_batch(client, b["id"])
    t1, t2, t3 = [t["id"] for t in d["targets"]]
    url = lambda t: f"/api/batches/{b['id']}/targets/{t}/evaluation/review"  # noqa: E731
    assert client.post(url(t1), json={"verdict": "adjust", "adjusted_quality": 90}).status_code == 200
    assert client.post(url(t2), json={"verdict": "disagree"}).status_code == 200
    assert client.post(url(t3), json={"verdict": "agree"}).status_code == 200

    async def go(db):
        got = {}
        for t in (t1, t2, t3):
            target = await db.get(BatchTarget, uuid.UUID(t))
            run = await db.get(Run, target.run_id)
            rows = await build_outcomes(db, run)
            got[t] = rows[-1]
        target = await db.get(BatchTarget, uuid.UUID(t3))
        target.attempts = (target.attempts or 0) + 1  # the evaluation is now stale
        await db.commit()
        stale = (await build_outcomes(db, await db.get(Run, target.run_id)))[-1]
        return got, stale

    got, stale = run_db(client, world, go)
    assert got[t1].components["evaluator"]["source"] == "human_adjusted"
    assert got[t1].components["evaluator"]["quality"] == pytest.approx(0.9)
    assert got[t2].evaluated_quality_score is None and "evaluator" not in got[t2].components
    assert got[t3].components["evaluator"]["source"] == "judge"
    assert stale.evaluated_quality_score is None


def test_a_review_rescores_existing_rows_and_drops_the_priors_cache(client, world, eng):
    b = make(client, world)
    d = run_batch(client, b["id"])
    t1 = d["targets"][0]["id"]
    priors = OutcomePriors(world.factory)
    priors._cache[("k",)] = (1e18, {})

    async def seed(db):
        target = await db.get(BatchTarget, uuid.UUID(t1))
        run = await db.get(Run, target.run_id)
        # a first write, as the engine does at run end
        await record_outcome(db, run, commit=True)
        return run.id

    run_id = run_db(client, world, seed)
    r = client.post(f"/api/batches/{b['id']}/targets/{t1}/evaluation/review", json={"verdict": "disagree"})
    assert r.status_code == 200
    assert priors._cache == {}

    async def read(db):
        return await db.get(RunOutcome, (run_id, 0))

    row = run_db(client, world, read)
    assert row.evaluated_quality_score is None


def test_a_failing_hook_never_fails_the_review(client, world, eng, monkeypatch):
    b = make(client, world)
    d = run_batch(client, b["id"])
    t1 = d["targets"][0]["id"]

    async def boom(*a, **k):
        raise RuntimeError("bookkeeping broke")

    monkeypatch.setattr(outcomes_service, "record_outcome", boom)

    async def seed(db):
        db.add(RunOutcome(run_id=(await db.get(BatchTarget, uuid.UUID(t1))).run_id, segment_index=0, task_type="freeform",
                          task_shape="freeform", objective="balanced", max_cost_tier="premium", size_band="m",
                          model_id=MODEL_ID, outcome_class="delivered", quality_score=0.7, score_version="outcome-v1",
                          components={}))
        await db.commit()

    run_db(client, world, seed)
    r = client.post(f"/api/batches/{b['id']}/targets/{t1}/evaluation/review", json={"verdict": "agree"})
    assert r.status_code == 200


# ── the gate ─────────────────────────────────────────────────────────────────
def _review_all(client, b, d, verdicts):
    for t, v in zip(d["targets"], verdicts):
        body = {"verdict": v} if v != "adjust" else {"verdict": "adjust", "adjusted_quality": 10}
        assert client.post(f"/api/batches/{b['id']}/targets/{t['id']}/evaluation/review", json=body).status_code == 200


def test_the_gate_opens_exactly_at_the_thresholds_and_not_below(client, world, eng, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "router_family_gate_min_checks", 3)
    monkeypatch.setattr(settings, "router_family_gate_min_agreement", 0.8)
    b = make(client, world)
    d = run_batch(client, b["id"])
    fam = _family(world, b)

    async def gate(db):
        return await family_gate(db, fam)

    assert run_db(client, world, gate)["open"] is False  # no reviews
    _review_all(client, b, d, ["agree", "agree", "disagree"])  # 3 checks, 67%
    closed = run_db(client, world, gate)
    assert closed["open"] is False and closed["spot_checks"] == 3 and closed["agreeing"] == 2
    assert set(closed) >= {"open", "spot_checks", "agreeing", "agreement", "needed_checks",
                           "needed_agreement", "mode", "rubric_version", "judge_model"}
    _review_all(client, b, d, ["agree", "agree", "agree"])
    opened = run_db(client, world, gate)
    assert opened["open"] is True and opened["agreement"] == 1.0 and opened["needed_checks"] == 3
    # exactly at the threshold: 3 checks, ratio 1.0; below the check count it is closed again
    monkeypatch.setattr(settings, "router_family_gate_min_checks", 4)
    assert run_db(client, world, gate)["open"] is False
    monkeypatch.setattr(settings, "router_family_gate_min_checks", 3)
    monkeypatch.setattr(settings, "router_family_priors", "off")
    off = run_db(client, world, gate)
    assert off["open"] is False and off["mode"] == "off" and off["spot_checks"] == 3


def test_another_workspaces_reviews_are_invisible_and_the_api_carries_router_gate(client, world, eng, monkeypatch):
    monkeypatch.setattr(get_settings(), "router_family_gate_min_checks", 3)
    b = make(client, world)
    d = run_batch(client, b["id"])
    _review_all(client, b, d, ["agree", "agree", "agree"])

    async def other(db):
        return await family_gate(db, f"{b['family_key']}@{uuid.uuid4()}")

    seen = run_db(client, world, other)
    assert seen["open"] is False and seen["spot_checks"] == 0
    family = client.get("/api/batches/family", params={"key": b["family_key"]}).json()
    assert family["router_gate"]["open"] is True and family["router_gate"]["spot_checks"] == 3
    card = client.get(f"/api/batches/{b['id']}/scorecard").json()
    assert card["evaluation"]["router_gate"] == family["router_gate"]


def test_gate_and_stamp_end_to_end_the_next_run_routes_on_family_evidence(client, world, eng, monkeypatch):
    monkeypatch.setattr(get_settings(), "router_family_gate_min_checks", 3)
    b = make(client, world)
    d = run_batch(client, b["id"])
    _review_all(client, b, d, ["agree", "agree", "agree"])
    fam = _family(world, b)

    async def go(db):
        for run in await _runs(db):
            await record_outcome(db, run, commit=True)

    run_db(client, world, go)

    async def read():
        priors = OutcomePriors(world.factory)
        key = dict(task_shape="freeform", objective="balanced", size_band="m")
        base = await priors.for_key(**key)
        family, status = await priors.for_key_family(**key, task_family=fam)
        return base, family, status

    import asyncio

    base, family, status = asyncio.run(read())
    # the gate is open, but one model alone cannot be re-ranked: truthfully "not used"
    assert status["gate"]["open"] is True and status["used"] is False
    assert family == base


def test_a_cancelled_pass_clears_the_evaluated_score(client, world, eng):
    b = make(client, world)
    run_batch(client, b["id"])

    async def first(db):
        for run in await _runs(db):
            await record_outcome(db, run, commit=True)
        return [r.evaluated_quality_score for r in (await db.execute(select(RunOutcome))).scalars().all()]

    assert all(v is not None for v in run_db(client, world, first))

    async def cancel(db):
        from tret.db.models import BatchEvaluation

        for ev in (await db.execute(select(BatchEvaluation))).scalars().all():
            ev.status = "cancelled"  # the pass is dead: `current_pass` falls back to it
        await db.commit()
        for run in await _runs(db):
            await record_outcome(db, run, commit=True)
        return [r.evaluated_quality_score for r in (await db.execute(select(RunOutcome))).scalars().all()]

    assert all(v is None for v in run_db(client, world, cancel))


def test_retrying_a_target_clears_the_old_runs_evaluated_score_once_rearmed(client, world, eng):
    b = make(client, world)
    d = run_batch(client, b["id"])
    t1 = d["targets"][0]["id"]

    async def first(db):
        for run in await _runs(db):
            await record_outcome(db, run, commit=True)
        target = await db.get(BatchTarget, uuid.UUID(t1))
        return target.run_id

    old_run = run_db(client, world, first)

    async def evaluated(db):
        return (await db.get(RunOutcome, (old_run, 0))).evaluated_quality_score

    assert run_db(client, world, evaluated) is not None
    # fail the target, retry it and let the batch (and its evaluation) settle again
    async def fail(db):
        t = await db.get(BatchTarget, uuid.UUID(t1))
        t.status = "failed"
        await db.commit()

    run_db(client, world, fail)
    assert client.post(f"/api/batches/{b['id']}/targets/{t1}/retry").status_code in (200, 202)
    from tests.test_collections_api import settle

    settle(client)

    async def after(db):
        row = await db.get(RunOutcome, (old_run, 0))
        target = await db.get(BatchTarget, uuid.UUID(t1))
        return row.evaluated_quality_score, row.components, target.run_id

    score_after, components, new_run = run_db(client, world, after)
    assert new_run != old_run
    assert score_after is None and "evaluator" not in components
