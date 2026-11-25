"""PR 7 second review: restore acknowledgements, refit guards, anchors in the preview, the anchor ledger
(unknown cost, duplicate votes, cancel), kappa orientation and the agreement floor."""
# ruff: noqa: F811 - imported fixtures are used by name as test arguments
from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from tests.test_batches_core import world  # noqa: F401 - fixture
from tests.test_eval_diagnostics import (  # noqa: F401 - fixtures and helpers
    COST,
    an,
    anti_strength,
    client,
    corpus,
    create,
    dg,
    env,
    fetch,
    gates,
    get_set,
    install,
    make_set,
    no_router_call,
    play,
    resume,
    state,
)
from tret.db.models import EvalAnchorRun, EvalAnchorSet, EvalTournament, ModuleRating
from tret.engine import direct
from tret.evaluation import tournament as tn


# ── N1: kappa orientation and the agreement floor ────────────────────────────────────────────────
def _doubles(agree: int, total: int, *, double_verdict_when_disagree="b") -> list[dict]:
    out = []
    for i in range(total):
        oid, did = uuid.uuid4(), uuid.uuid4()
        base = {"pool_key": "p", "round": 1, "a": f"member-{i}-a", "b": f"member-{i}-b", "status": "done", "swap_of": None}
        out.append({**base, "id": oid, "kind": "primary", "verdict": "a", "family": "x", "double_of": None})
        out.append({**base, "id": did, "kind": "double", "verdict": "a" if i < agree else double_verdict_when_disagree,
                    "family": "y", "double_of": oid})
    return out


def _gate(pooled):
    th = {**dg.thresholds(), "min_doubles": 12}
    length = {"overall": {"n": 0, "spearman": None}}
    return {g["key"]: g for g in dg.build_gates(length, [], [], None, th, None, pooled)}["juror_agreement"]


def test_eleven_of_twelve_agreeing_does_not_trip_but_a_contrarian_family_does():
    # the prevalence paradox: every verdict "a", one disagreement: plain kappa is below zero although 92% agree
    assert dg.cohen_kappa([("a", "a")] * 11 + [("a", "b")]) < 0.3
    pooled = dg._pooled_kappa(_doubles(11, 12))
    assert pooled["n"] == 12 and pooled["raw_agreement"] == pytest.approx(11 / 12)
    g = _gate(pooled)
    assert g["state"] != "tripped" and g["n"] == 12 and "raw agreement 0.92" in g["detail"]
    # a family that answers against the other one: agreement ~0 and kappa far below the floor
    contrarian = dg._pooled_kappa(_doubles(0, 12))
    assert contrarian["raw_agreement"] == 0 and contrarian["kappa"] < 0
    assert _gate(contrarian)["state"] == "tripped"
    # a low kappa with the floor met only warns
    th = {**dg.thresholds(), "min_doubles": 12}
    low_but_agreeing = {"n": 12, "kappa": 0.1, "raw_agreement": 0.9}
    assert _gate(low_but_agreeing)["state"] == "warn"
    assert th["agreement_floor"] == 0.75


def test_orientation_is_deterministic_balances_the_categories_and_keeps_agreement():
    assert dg.orient("a", "b", "a", "b") == dg.orient("a", "b", "a", "b")
    flips = sum(1 for i in range(400) if dg.orient(f"x{i}", f"y{i}", "a", "a")[0] == "b")
    assert 140 < flips < 260  # about half the pairs are flipped
    for i in range(50):  # both verdicts of a pair move together: agreement never changes, ties stay ties
        x, y = dg.orient(f"x{i}", f"y{i}", "a", "tie")
        assert (x, y) in (("a", "tie"), ("b", "tie"))
    pairs = [dg.orient(f"x{i}", f"y{i}", "a", "a") for i in range(60)]
    assert {p[0] for p in pairs} == {"a", "b"} and all(p[0] == p[1] for p in pairs)


def test_history_min_kappa_is_the_pooled_kappa_of_the_gate(env, gates, monkeypatch):
    monkeypatch.setattr(tn, "DOUBLE_RATE", 1.0)
    corpus(env.world)
    tid = play(env)
    d = fetch(env.world, lambda db: dg.compute_diagnostics(db, tid))
    g = {x["key"]: x for x in d["gates"]}["juror_agreement"]
    assert g["n"] >= 2 and g["value"] is not None
    assert d["history"][-1]["min_kappa"] == pytest.approx(g["value"])


# ── M2 / L5: refit ───────────────────────────────────────────────────────────────────────────────
def test_a_refit_never_overwrites_a_tournament_that_changed_or_has_a_runner(env, gates):
    corpus(env.world)
    tid = play(env)
    t, _m, ratings = state(env, tid)
    assert t.status == "completed" and t.summary["partial"] is False and len(ratings) == 14
    factory = env.world.factory
    stale = ("paused", t.updated_at)  # what a refit read before the tournament finished

    async def go():
        return await tn.compute_ratings(factory, tid, finish=False, expect=stale)

    assert asyncio.run(go()) is False  # status and updated_at do not match: nothing is written
    t2, _m2, ratings2 = state(env, tid)
    assert t2.summary["partial"] is False and t2.summary == t.summary and sorted(r.rating for r in ratings2) == sorted(r.rating for r in ratings)

    async def go_ok():
        return await tn.compute_ratings(factory, tid, finish=False, expect=("completed", t.updated_at))

    assert asyncio.run(go_ok()) is True
    # a live lease on the row: refit is a 409 whatever the status
    async def lease():
        async with factory() as db:
            row = await db.get(EvalTournament, tid)
            row.status, row.lease_owner, row.lease_until = "paused", "runner", tn._now() + tn.timedelta(seconds=60)
            await db.commit()

    asyncio.run(lease())
    with client(env) as c:
        r = c.post(f"/api/eval/tournaments/{tid}/refit")
        assert r.status_code == 409 and "lease" in r.json()["detail"]
    assert fetch(env.world, lambda db: db.get(EvalTournament, tid)).summary["partial"] is False


def test_refit_with_only_anchor_matches_has_nothing_to_fit(env, gates):
    corpus(env.world)
    first = play(env)
    make_set(env, first)
    install(env, anti_strength)
    second = play(env)  # paused by anchor drift: only anchor matches were played
    t, matches, ratings = state(env, second)
    assert t.pause_reason == "anchor_drift" and {m.kind for m in matches} == {"anchor"} and ratings == []
    with client(env) as c:
        r = c.post(f"/api/eval/tournaments/{second}/refit")
        assert r.status_code == 409 and "nothing to fit" in r.json()["detail"]
    assert fetch(env.world, lambda db: db.execute(select(ModuleRating).where(ModuleRating.tournament_id == second))).scalars().all() == []


# ── M3: anchors in the preview, frozen at start ──────────────────────────────────────────────────
def _preview(env):
    async def go():
        async with env.world.factory() as db:
            t = await create(env, db)
            await db.commit()
            return t.id, t.estimates

    return asyncio.run(go())


def test_the_preview_counts_the_anchor_matches_and_start_freezes_the_set(env, gates):
    corpus(env.world)
    first = play(env)
    _id, none = _preview(env)
    assert none["anchor_set"] is None and none["matches"]["anchor"] == 0
    aid = make_set(env, first)
    draft, est = _preview(env)
    assert est["anchor_set"] == {"id": str(aid), "pairs": 6, "matches": 18} and est["matches"]["anchor"] == 18
    assert est["matches"]["total"] == none["matches"]["total"] + 18
    assert est["cost_usd"]["expected"] > none["cost_usd"]["expected"] and est["tokens"]["input"] > none["tokens"]["input"]
    from tret import cli_eval

    assert "18 anchors" in cli_eval.render_preview(tn.tournament_out(fetch(env.world, lambda db: db.get(EvalTournament, draft))))
    newer = make_set(env, first)  # a newer ready set appears after the preview
    assert newer != aid

    async def start():
        async with env.world.factory() as db:
            await tn.start_tournament(db, workspace_id=env.world.ws.id, tournament_id=draft, budget_usd=100, services=env.svc)
        assert await tn.drain(120)

    asyncio.run(start())
    t, matches, _r = state(env, draft)
    assert t.diagnostics["anchor_set_id"] == str(aid)  # what the preview named, not the newer set
    assert {m.anchor_set_id for m in matches if m.kind == "anchor"} == {aid} and len([m for m in matches if m.kind == "anchor"]) == 18
    assert Decimal(t.spent_usd) == COST * len(matches)


def test_a_started_tournament_without_a_ready_set_records_none_even_if_one_appears(env, gates):
    corpus(env.world)
    first = play(env)
    draft, est = _preview(env)
    assert est["anchor_set"] is None
    make_set(env, first)  # ready only after the preview and before the start: the start looks again

    async def start():
        async with env.world.factory() as db:
            await tn.start_tournament(db, workspace_id=env.world.ws.id, tournament_id=draft, budget_usd=100, services=env.svc)
        assert await tn.drain(120)

    asyncio.run(start())
    t = state(env, draft)[0]
    assert t.diagnostics["anchor_set_id"] is not None  # decided at start, recorded, used
    assert any(m.kind == "anchor" for m in state(env, draft)[1])


# ── L1-L3: the anchor ledger ─────────────────────────────────────────────────────────────────────
def _new_set(env, tid, budget=100):
    async def go():
        async with env.world.factory() as db:
            a = await an.create_anchor_set(db, workspace_id=env.world.ws.id, tournament_id=tid, budget_usd=budget, services=env.svc, spawn=False)
            return a.id, a.lease_owner

    return asyncio.run(go())


def test_a_run_whose_cost_could_not_be_read_stays_unbooked_until_recovery_books_its_real_cost(env, gates, monkeypatch):
    corpus(env.world)
    tid = play(env)
    aid, owner = _new_set(env, tid)
    real = direct.execute_direct
    seen = []

    async def glitch(*a, **kw):
        res = await real(*a, **kw)
        seen.append(1)
        if len(seen) == 1:
            raise RuntimeError("the database blinked after the call")  # the run spent; this runner never read its cost
        return res

    monkeypatch.setattr(direct, "execute_direct", glitch)
    asyncio.run(an.run_baseline(aid, owner))
    rows = fetch(env.world, lambda db: db.execute(select(EvalAnchorRun).where(EvalAnchorRun.anchor_set_id == aid))).scalars().all()
    unbooked = [r for r in rows if not r.booked]
    assert len(unbooked) == 1 and Decimal(unbooked[0].cost_usd) == 0 and unbooked[0].verdict is None  # not settled with a guess
    a = get_set(env, aid)
    assert a.status == "baseline_pending" and "juror_failures" in a.pause_reason
    b = an._Baseline(aid, "takeover")
    asyncio.run(b._recover())
    row = fetch(env.world, lambda db: db.get(EvalAnchorRun, unbooked[0].run_id))
    assert row.booked and Decimal(row.cost_usd) == COST  # the real cost, read from the run
    spent, votes = asyncio.run(b._ledger())
    assert spent == COST * len(rows) and sum(len(v) for v in votes.values()) == len(rows) - 1  # no vote from the unread call
    monkeypatch.setattr(direct, "execute_direct", real)

    async def finish():
        async with env.world.factory() as db:
            await an.resume_anchor_set(db, workspace_id=env.world.ws.id, set_id=aid, spawn=False)
            owner2 = (await db.get(EvalAnchorSet, aid)).lease_owner
        await an.run_baseline(aid, owner2)

    asyncio.run(finish())
    a = get_set(env, aid)
    assert a.status == "ready" and all(len(p["votes"]) == 3 for p in a.pairs)
    rows = fetch(env.world, lambda db: db.execute(select(EvalAnchorRun).where(EvalAnchorRun.anchor_set_id == aid))).scalars().all()
    assert Decimal(a.spent_usd) == sum(Decimal(r.cost_usd) for r in rows if r.booked) == COST * len(rows) and all(r.booked for r in rows)


def test_a_second_vote_of_a_juror_on_a_pair_is_refused_and_the_cost_still_booked(env, gates):
    corpus(env.world)
    tid = play(env)
    aid = make_set(env, tid)
    rows = fetch(env.world, lambda db: db.execute(select(EvalAnchorRun).where(EvalAnchorRun.anchor_set_id == aid))).scalars().all()
    first = rows[0]

    async def dup():
        from tret.db.models import Run

        async with env.world.factory() as db:
            donor = (await db.execute(select(Run).limit(1))).scalar_one()
            run = Run(project_id=donor.project_id, harness_id=donor.harness_id, task_type="direct", task_input={}, document_ids=[],
                      status="completed", cost_usd=Decimal("0.01"))
            db.add(run)
            await db.flush()
            db.add(EvalAnchorRun(run_id=run.id, anchor_set_id=aid, pair_index=first.pair_index, juror_index=first.juror_index,
                                 family=first.family, model=first.model))
            await db.commit()
            return run.id

    rid = asyncio.run(dup())
    b = an._Baseline(aid, "x")
    with pytest.raises(IntegrityError):  # the database allows one settled verdict per (set, pair, juror)
        asyncio.run(b._settle(rid, dict(booked=True, cost_usd=Decimal("0.01"), verdict="a", strength="clear", error=None)))
    asyncio.run(b._settle(rid, dict(booked=True, cost_usd=Decimal("0.01"), verdict=None, strength=None, error="duplicate vote ignored")))
    spent, votes = asyncio.run(b._ledger())
    assert spent == COST * 18 + Decimal("0.01") and sum(len(v) for v in votes.values()) == 18  # booked, not counted as a second vote
    assert all(len({v["juror_index"] for v in vs}) == len(vs) for vs in votes.values())


def test_a_cancelled_baseline_gives_its_lease_back_at_once(env, gates):
    corpus(env.world)
    tid = play(env)
    aid, owner = _new_set(env, tid)

    async def go():
        started = asyncio.Event()

        async def block(n):
            started.set()
            await asyncio.sleep(30)

        env.juror.on_call = block
        task = asyncio.create_task(an.run_baseline(aid, owner))
        await asyncio.wait_for(started.wait(), 20)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # the shielded release finishes on its own task: wait for it (never end the loop with it pending)
        for _ in range(200):
            async with env.world.factory() as db:
                if (await db.get(EvalAnchorSet, aid)).lease_owner is None:
                    break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.2)

    asyncio.run(go())
    a = get_set(env, aid)
    assert a.status == "baseline_pending" and a.lease_owner is None and a.lease_until is None and a.pause_reason == "interrupted"
    env.juror.on_call = None

    async def again():
        async with env.world.factory() as db:  # resumable now, without waiting out a 15 minute lease
            await an.resume_anchor_set(db, workspace_id=env.world.ws.id, set_id=aid)
        assert await an.drain(120)

    asyncio.run(again())
    assert get_set(env, aid).status == "ready"
