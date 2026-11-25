"""PR 7 review fixes: acknowledgement contract, dead ends, gates at realistic sizes, the anchor baseline's
ledger and heartbeat, stored diagnostics, the frozen jury in the detail."""
# ruff: noqa: F811 - imported fixtures are used by name as test arguments
from __future__ import annotations

import asyncio
from decimal import Decimal

from sqlalchemy import select

from tests.test_batches_core import world  # noqa: F401 - fixture
from tests.test_eval_diagnostics import (  # noqa: F401 - fixtures and helpers
    COST,
    an,
    client,
    corpus,
    create,
    dg,
    env,
    fetch,
    gate_map,
    gates,
    get_set,
    install,
    longer,
    make_set,
    no_router_call,
    play,
    position,
    resume,
    state,
)
from tret.config import get_settings
from tret.db.models import EvalAnchorRun, EvalAnchorSet, EvalTournament, ModuleRating
from tret.evaluation import tournament as tn


# ── B: dead ends ──────────────────────────────────────────────────────────────────────────────────
def test_every_family_dropped_then_an_acknowledged_resume_restores_the_jury(env, gates, monkeypatch):
    monkeypatch.setattr(tn, "SWAP_RATE", 1.0)
    corpus(env.world)
    install(env, gpt=position, gem=position, llama=position)
    tid = play(env)
    t, matches, _r = state(env, tid)
    assert t.status == "paused" and t.pause_reason == "jury_degraded" and set(t.dropped_families) == {"openai", "google", "meta"}
    resume(env, tid, acknowledge_gates=["flip_rate"], note="no better jury available")
    t, matches, _r = state(env, tid)
    assert t.dropped_families == [] and any(n.startswith("jury restored") for n in t.diagnostics["notes"])
    assert t.diagnostics["jury_restored"] is True
    # the pooled position-bias gate sees the bias of the restored jury and asks for its own acknowledgement
    assert t.status == "paused" and t.pause_reason == "position_bias" and tn.required_gates(t) == ["position_bias"]
    seen = {t.pause_reason}
    for _ in range(4):  # the positional jurors also disagree with each other: each gate is acknowledged on its own
        req = tn.required_gates(t)
        if t.status != "paused" or not req:
            break
        resume(env, tid, acknowledge_gates=req, note="all jurors are positional on this corpus")
        t = state(env, tid)[0]
        seen.add(t.pause_reason)
    assert {"position_bias"} <= seen
    t, matches, ratings = state(env, tid)
    assert t.status == "completed" and len(ratings) == 14 and t.dropped_families == []  # not dropped again once restored
    assert {m.juror_family for m in matches if m.round >= 2} == {"openai", "google", "meta"}  # the original jurors sit again


def test_refit_works_for_paused_and_cancelled_tournaments_as_partial(env, gates):
    corpus(env.world)
    install(env, longer)
    tid = play(env)
    t, matches, ratings = state(env, tid)
    assert t.status == "paused" and ratings == []
    calls = len(env.juror.calls)

    async def refit():
        async with env.world.factory() as db:
            return await tn.refit_tournament(db, workspace_id=env.world.ws.id, tournament_id=tid)

    t = asyncio.run(refit())
    assert t.status == "paused" and t.summary["partial"] is True and t.lease_owner is None  # no runner, no spend
    assert len(env.juror.calls) == calls and not tn._tasks
    assert fetch(env.world, lambda db: db.execute(select(ModuleRating))).scalars().all()
    with client(env) as c:
        r = c.get(f"/api/eval/tournaments/{tid}/ratings").json()
        assert r["partial"] is True and r["pools"][0]["rows"]
        assert c.post(f"/api/eval/tournaments/{tid}/cancel").json()["status"] == "cancelled"  # cancel works from paused
        assert c.post(f"/api/eval/tournaments/{tid}/refit").json()["summary"]["partial"] is True  # and refit after it
    assert len(env.juror.calls) == calls
    install(env)
    done = play(env)  # a completed one is not partial
    with client(env) as c:
        assert c.get(f"/api/eval/tournaments/{done}/ratings").json()["partial"] is False
    assert state(env, done)[0].summary["partial"] is False


# ── C: gates at realistic sizes ──────────────────────────────────────────────────────────────────
def test_the_pooled_position_bias_gate_fires_without_per_family_samples(env, gates, monkeypatch):
    monkeypatch.setattr(tn, "SWAP_RATE", 1.0)
    monkeypatch.setattr(get_settings(), "eval_gate_min_swaps", 50)  # no family reaches it: only the pooled gate can fire
    corpus(env.world)
    install(env, gpt=position)
    tid = play(env)
    t, matches, ratings = state(env, tid)
    assert t.status == "paused" and t.pause_reason == "position_bias" and t.dropped_families == [] and ratings == []
    g = gate_map(t)["position_bias"]
    assert g["state"] == "tripped" and 0.15 < g["value"] < 1 and g["n"] >= 2
    assert gate_map(t)["flip_rate"]["state"] == "insufficient_data"
    assert tn.required_gates(t) == ["position_bias"]
    for _ in range(4):  # the positional juror may also trip the agreement gate later: acknowledge what blocks
        req = tn.required_gates(state(env, tid)[0])
        if not req:
            break
        resume(env, tid, acknowledge_gates=req, note="seen")
    assert state(env, tid)[0].status == "completed"


def test_the_preview_says_whether_the_gates_can_fire(env):
    corpus(env.world)

    async def go():
        async with env.world.factory() as db:
            t = await create(env, db)
            await db.commit()
            return t.estimates

    cov = asyncio.run(go())["gate_coverage"]
    assert set(cov) == {"swaps_pooled", "swaps_per_family", "doubles", "rated", "all_ok", "message"}
    assert cov["rated"] == {"expected": 14, "minimum": 30, "ok": False}
    assert cov["swaps_pooled"]["minimum"] == 8 and cov["swaps_per_family"]["minimum"] == 10 and cov["doubles"]["minimum"] == 12
    assert cov["all_ok"] is False and "rated" in cov["message"]
    assert dg.gate_coverage({"swap": 9, "double": 14}, 40, 3)["swaps_pooled"]["ok"] is True
    assert dg.gate_coverage({"swap": 9, "double": 14}, 40, 3)["swaps_per_family"] == {"expected": 3.0, "minimum": 10, "ok": False}


# ── D: the anchor baseline ───────────────────────────────────────────────────────────────────────
def test_the_anchor_ledger_gives_the_spend_and_a_takeover_books_an_unsettled_run_once(env, gates):
    corpus(env.world)
    tid = play(env)
    aid = make_set(env, tid)
    a = get_set(env, aid)
    rows = fetch(env.world, lambda db: db.execute(select(EvalAnchorRun).where(EvalAnchorRun.anchor_set_id == aid))).scalars().all()
    assert len(rows) == 18 and all(r.booked and r.verdict for r in rows)
    assert Decimal(a.spent_usd) == sum(Decimal(r.cost_usd) for r in rows) == COST * 18  # derived from the ledger

    # a runner died after writing the trace of a call and before settling it
    async def orphan():
        from tret.db.models import Run

        async with env.world.factory() as db:
            run = Run(project_id=(await db.get(EvalAnchorSet, aid)) and (await db.execute(select(Run.project_id).limit(1))).scalar_one(),
                      harness_id=(await db.execute(select(Run.harness_id).limit(1))).scalar_one(), task_type="direct",
                      task_input={}, document_ids=[], status="failed", cost_usd=Decimal("0.05"))
            db.add(run)
            await db.flush()
            db.add(EvalAnchorRun(run_id=run.id, anchor_set_id=aid, pair_index=0, juror_index=0, family="openai", model="m"))
            await db.commit()
            return run.id

    rid = asyncio.run(orphan())
    b = an._Baseline(aid, "other")
    asyncio.run(b._recover())
    asyncio.run(b._recover())  # a second takeover books nothing more
    spent, votes = asyncio.run(b._ledger())
    assert spent == COST * 18 + Decimal("0.05") and sum(len(v) for v in votes.values()) == 18  # booked once, never a vote
    row = fetch(env.world, lambda db: db.get(EvalAnchorRun, rid))
    assert row.booked and row.verdict is None and Decimal(row.cost_usd) == Decimal("0.05")


def test_an_unreadable_pair_is_left_out_and_the_set_still_becomes_ready(env, gates, monkeypatch):
    corpus(env.world)
    tid = play(env)
    monkeypatch.setattr(get_settings(), "eval_anchor_pairs", 10)

    async def damage_and_run():
        from tret.storage import get_store

        async with env.world.factory() as db:
            a = await an.create_anchor_set(db, workspace_id=env.world.ws.id, tournament_id=tid, budget_usd=100, services=env.svc, spawn=False)
            # a text that only one pair uses, so exactly that pair is lost
            uses = {}
            for p in a.pairs:
                for side in ("a", "b"):
                    uses.setdefault(p[side]["text_key"], set()).add(p["index"])
            key = next(k for k, v in uses.items() if len(v) == 1)
            lost = next(iter(uses[key]))
            aid, owner = a.id, a.lease_owner
        await get_store().put(key, b"someone changed this text")  # its sha no longer matches
        await an.run_baseline(aid, owner)
        return aid, lost

    aid, lost = asyncio.run(damage_and_run())
    a = get_set(env, aid)
    assert a.status == "ready"  # 9 of 10 usable, 8 needed
    bad = a.pairs[lost]
    assert "no longer matches" in bad["unusable"] and bad["baseline"] is None and bad["votes"] == []
    assert all(p["baseline"] for p in a.pairs if p["index"] != lost)
    with client(env) as c:
        out = c.get(f"/api/eval/anchor-sets/{aid}").json()
        assert out["unusable"] == 1 and out["votes"] == out["votes_expected"] == 27


def test_too_few_usable_pairs_fail_the_set_with_a_reason(env, gates, monkeypatch):
    corpus(env.world)
    tid = play(env)

    async def damage_all():
        from tret.storage import get_store

        async with env.world.factory() as db:
            a = await an.create_anchor_set(db, workspace_id=env.world.ws.id, tournament_id=tid, budget_usd=100, services=env.svc, spawn=False)
            keys = {p["a"]["text_key"] for p in a.pairs[:3]}
            aid, owner = a.id, a.lease_owner
        for k in keys:
            await get_store().put(k, b"changed")
        await an.run_baseline(aid, owner)
        return aid

    aid = asyncio.run(damage_all())
    a = get_set(env, aid)
    assert a.status == "failed" and "usable" in a.pause_reason and a.lease_owner is None
    assert an.min_usable(20) == 15 and an.min_usable(6) == 5 and an.min_usable(4) == 4
    with client(env) as c:  # a failed set is not resumable, and no tournament picks it
        assert c.post(f"/api/eval/anchor-sets/{aid}/resume", json={}).status_code == 409


def test_the_baseline_heartbeat_keeps_the_lease_and_stops_when_it_is_lost(env, gates, monkeypatch):
    corpus(env.world)
    tid = play(env)
    monkeypatch.setattr(tn, "HEARTBEAT_SECONDS", 0.05)

    async def go():
        async with env.world.factory() as db:
            a = await an.create_anchor_set(db, workspace_id=env.world.ws.id, tournament_id=tid, budget_usd=100, services=env.svc, spawn=False)
            aid, owner = a.id, a.lease_owner
            await db.execute(EvalAnchorSet.__table__.update().where(EvalAnchorSet.id == aid).values(lease_until=tn._now().replace(year=2001)))
            await db.commit()
        b = an._Baseline(aid, owner)
        beat = asyncio.create_task(b._heartbeat())
        await asyncio.sleep(0.4)
        async with env.world.factory() as db:
            lease = tn._aware((await db.get(EvalAnchorSet, aid)).lease_until)
        assert lease > tn._now() and not b.stopped  # renewed
        async with env.world.factory() as db:
            await db.execute(EvalAnchorSet.__table__.update().where(EvalAnchorSet.id == aid).values(lease_owner="someone else"))
            await db.commit()
        await asyncio.wait_for(beat, 5)
        return b.stopped

    assert asyncio.run(go()) is True


# ── E: the anchor phase on a resume ──────────────────────────────────────────────────────────────
def test_the_anchor_phase_on_a_resume_only_replaces_its_own_gate(env, gates, monkeypatch):
    corpus(env.world)
    first = play(env)
    make_set(env, first)
    monkeypatch.setattr(get_settings(), "eval_gate_anchor_changes_pause", 6)  # the longer-is-better jury moves some anchors: let it
    install(env, longer)
    second = play(env)  # anchors judged and fine, then paused by length_bias
    t = state(env, second)[0]
    assert t.pause_reason == "length_bias" and t.diagnostics["anchors"]["judged"] == 6
    before = gate_map(t)
    assert before["length_bias"]["state"] == "tripped" and before["flip_rate"]["state"] != "not_applicable"

    async def go():
        async with env.world.factory() as db:
            t = await tn.resume_tournament(db, workspace_id=env.world.ws.id, tournament_id=second, acknowledge_gates=["length_bias"],
                                           note="seen", spawn=False)
            owner = t.lease_owner
        r = tn._Runner(second, owner)
        assert await r._claim() and await r._load()
        assert await r._anchor_phase()

    asyncio.run(go())
    after = gate_map(state(env, second)[0])
    assert set(after) == set(before)
    for key in before:
        if key != "anchor_drift":
            assert after[key]["state"] == before[key]["state"] and after[key]["value"] == before[key]["value"], key
    assert after["anchor_drift"]["state"] == "ok"
    assert [g["key"] for g in state(env, second)[0].diagnostics["block"]["gates"]] == list(after)


# ── G: stored diagnostics ────────────────────────────────────────────────────────────────────────
def test_diagnostics_are_served_from_the_stored_block_and_cached_when_completed(env, gates, monkeypatch):
    corpus(env.world)
    install(env, longer)
    tid = play(env)  # paused after a round, with a stored block
    calls = []
    real = dg.compute_from_inputs
    monkeypatch.setattr(dg, "compute_from_inputs", lambda inputs: calls.append(1) or real(inputs))
    with client(env) as c:
        d = c.get(f"/api/eval/tournaments/{tid}/diagnostics").json()
        assert calls == [] and d["status"] == "paused"  # served, not recomputed
        assert {g["key"]: g["state"] for g in d["gates"]}["length_bias"] == "tripped" and d["history"]
        assert set(d) == {"tournament_id", "status", "computed_at", "thresholds", "gates", "length_bias", "flip_rates", "agreement", "anchors", "history", "notes"}
    resume(env, tid, acknowledge_gates=["length_bias"], note="seen", acknowledged_by="boss@example.com")
    assert state(env, tid)[0].status == "completed" and calls  # the run itself computes each round
    calls.clear()
    with client(env) as c:
        first = c.get(f"/api/eval/tournaments/{tid}/diagnostics").json()
        assert len(calls) == 1 and first["gates"][0]["acknowledged_by"]  # completed, not yet cached: computed once
        second = c.get(f"/api/eval/tournaments/{tid}/diagnostics").json()
        assert len(calls) == 1 and second["gates"] == first["gates"] and second["length_bias"] == first["length_bias"]  # cached on the row
    assert fetch(env.world, lambda db: db.get(EvalTournament, tid)).diagnostics["final"] is True
    # a draft is computed live and nothing is written for it

    async def draft():
        async with env.world.factory() as db:
            d = await create(env, db)
            await db.commit()
            return d.id

    did = asyncio.run(draft())
    with client(env) as c:
        assert c.get(f"/api/eval/tournaments/{did}/diagnostics").json()["status"] == "draft"
    assert fetch(env.world, lambda db: db.get(EvalTournament, did)).diagnostics == {}
