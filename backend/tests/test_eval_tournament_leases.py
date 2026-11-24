"""Tournament runner safety, second round: interruption, transient errors, an atomic lease, run-cost
ledger, replaced runners, unusable text."""
# ruff: noqa: F811 - imported fixtures are used by name as test arguments
from __future__ import annotations

import asyncio
import contextlib
import uuid
from decimal import Decimal

from sqlalchemy import select, update

from tests.test_batches_core import world  # noqa: F401 - fixture
from tests.test_eval_tournament import (  # noqa: F401 - fixtures and helpers
    COST,
    _resume_run,
    client,
    corpus,
    create,
    env,
    everything,
    fetch,
)
from tests.test_eval_tournament_hardening import start_only
from tests.test_route_jury import no_router_call  # noqa: F401 - autouse fixture
from tret.db.models import EvalMatch, EvalMatchRun, EvalTournament, RoutingFreeze, Run
from tret.evaluation import tournament as tn


def ledger(env, tid):
    return fetch(env.world, lambda db: db.execute(select(EvalMatchRun).where(EvalMatchRun.tournament_id == tid))).scalars().all()


# ── F2: a cancelled runner leaves the tournament paused ──────────────────────────────────────
def test_cancelling_a_running_runner_task_pauses_the_tournament_and_resume_finishes_it(env):
    corpus(env.world)
    tid, owner = start_only(env)

    async def go():
        started = asyncio.Event()

        async def block(n):
            started.set()
            await asyncio.sleep(30)

        env.juror.on_call = block
        task = asyncio.create_task(tn.run_tournament(tid, owner))
        await asyncio.wait_for(started.wait(), 20)
        task.cancel()  # Ctrl-C, shutdown
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.5)  # the cut-short run closes itself in a shielded task

    asyncio.run(go())
    t, matches, _ = fetch(env.world, lambda db: everything(db, tid))
    assert (t.status, t.pause_reason) == ("paused", "interrupted") and t.lease_owner is None and t.lease_until is None
    env.juror.on_call = None
    asyncio.run(_resume_run(env, tid))  # the call that was cut short is replayed, its cost booked once
    t, matches, ratings = fetch(env.world, lambda db: everything(db, tid))
    assert t.status == "completed" and all(m.status == "done" for m in matches) and len(ratings) == 14
    assert len(ledger(env, tid)) == len(set(r.run_id for r in ledger(env, tid)))


# ── F3: transient errors pause, never fail ───────────────────────────────────────────────────
def test_a_transient_blob_store_error_pauses_with_the_message_and_resume_replays(env, monkeypatch):
    corpus(env.world)
    import tret.storage as storage

    real = storage.get_store
    down = {"on": True}

    class Flaky:
        def __init__(self, inner):
            self.inner = inner

        async def get(self, key):
            if down["on"]:
                raise OSError("blob store down")
            return await self.inner.get(key)

    monkeypatch.setattr(storage, "get_store", lambda *a, **k: Flaky(real(*a, **k)))
    tid, owner = start_only(env)
    asyncio.run(tn.run_tournament(tid, owner))
    t, matches, _ = fetch(env.world, lambda db: everything(db, tid))
    assert t.status == "paused" and t.pause_reason.startswith("error: ") and "blob store down" in t.pause_reason
    assert not any(m.status in ("failed", "running") for m in matches) and env.juror.calls == []
    down["on"] = False
    asyncio.run(_resume_run(env, tid))
    t, matches, ratings = fetch(env.world, lambda db: everything(db, tid))
    assert t.status == "completed" and len(ratings) == 14 and all(m.status == "done" for m in matches)


def test_an_error_after_the_call_books_the_runs_cost_once_and_the_match_is_replayed(env, monkeypatch):
    corpus(env.world)
    real = tn._Runner._settle
    boom = {"n": 0}

    async def flaky(self, *a, **k):
        boom["n"] += 1
        if boom["n"] == 1:
            raise ConnectionError("database connection dropped")
        return await real(self, *a, **k)

    monkeypatch.setattr(tn._Runner, "_settle", flaky)
    tid, owner = start_only(env)
    asyncio.run(tn.run_tournament(tid, owner))
    t = fetch(env.world, lambda db: db.get(EvalTournament, tid))
    assert t.status == "paused" and "database connection dropped" in t.pause_reason
    assert Decimal(t.spent_usd) >= COST  # the paid call is on the books
    asyncio.run(_resume_run(env, tid))
    t, matches, _ = fetch(env.world, lambda db: everything(db, tid))
    assert t.status == "completed"
    assert Decimal(t.spent_usd) == COST * len(env.juror.calls) and len(ledger(env, tid)) == len(env.juror.calls)  # no double booking


def test_a_failed_tournament_can_be_resumed_and_its_freeze_is_released(env, monkeypatch):
    corpus(env.world)
    real = tn._Runner._make_primaries

    async def boom(self, r):
        raise RuntimeError("bug")

    monkeypatch.setattr(tn._Runner, "_make_primaries", boom)
    tid, owner = start_only(env)
    asyncio.run(tn.run_tournament(tid, owner))
    t = fetch(env.world, lambda db: db.get(EvalTournament, tid))
    assert t.status == "failed" and fetch(env.world, lambda db: db.get(RoutingFreeze, t.freeze_id)).status == "released"
    monkeypatch.setattr(tn._Runner, "_make_primaries", real)  # the bug is fixed
    asyncio.run(_resume_run(env, tid))
    t, matches, ratings = fetch(env.world, lambda db: everything(db, tid))
    assert t.status == "completed" and len(ratings) == 14 and t.pause_reason is None


def test_the_module_definition_is_fixed_at_create_time(env, monkeypatch):
    corpus(env.world)
    tid, owner = start_only(env)
    t = fetch(env.world, lambda db: db.get(EvalTournament, tid))
    assert t.module_def["slug"] == "physical_climate_risk" and t.module_def["definition"]

    def changed():
        raise AssertionError("the configured module set must not be read after create")

    monkeypatch.setattr(tn, "load_module_set", changed)
    asyncio.run(tn.run_tournament(tid, owner))
    assert fetch(env.world, lambda db: db.get(EvalTournament, tid)).status == "completed"
    assert env.juror.calls and t.module_def["definition"] in env.juror.calls[0]["system"]


# ── F5: the lease is enforced in the statements ──────────────────────────────────────────────
def test_a_stale_runner_after_a_takeover_cannot_claim_settle_or_finalize_and_nothing_is_booked_twice(env):
    corpus(env.world)
    tid, owner = start_only(env)
    state = {}

    async def takeover_mid_call(n):
        if n == 1 and "done" not in state:
            state["done"] = True
            async with env.world.factory() as db:  # a resume gave the lease to someone else during the call
                await db.execute(update(EvalTournament).where(EvalTournament.id == tid)
                                 .values(lease_owner="new-owner", lease_until=tn._lease_end()))
                await db.commit()

    env.juror.on_call = takeover_mid_call

    async def go():
        a = tn._Runner(tid, owner)
        assert await a._load()
        await a._make_primaries(1)
        async with env.world.factory() as db:
            ids = list((await db.execute(select(EvalMatch.id).where(EvalMatch.tournament_id == tid).order_by(EvalMatch.id))).scalars())
        await a._play(ids[0])  # the call happens, then the lease is gone at settle time
        assert a.stopped
        await a._play(ids[1])  # and it can no longer claim anything
        assert len(env.juror.calls) == 1
        assert await a._set(current_round=1) is False
        assert await tn.compute_ratings(env.world.factory, tid, finish=True, owner=owner) is False
        return ids

    ids = asyncio.run(go())
    t, matches, ratings = fetch(env.world, lambda db: everything(db, tid))
    first = next(m for m in matches if m.id == ids[0])
    assert first.status == "running" and first.claimed_by == owner  # its result was not recorded
    assert Decimal(t.spent_usd) == COST and len(ledger(env, tid)) == 1  # but the call is paid for, once
    assert t.status == "running" and t.current_round == 0 and ratings == []
    # the new holder recovers it: the finished run is already on the books, so nothing is added
    async def recover():
        b = tn._Runner(tid, "new-owner")
        assert await b._load()
        await b._recover_orphans()
        await b._recover_orphans()

    asyncio.run(recover())
    t, matches, _ = fetch(env.world, lambda db: everything(db, tid))
    assert Decimal(t.spent_usd) == COST and len(ledger(env, tid)) == 1
    assert next(m for m in matches if m.id == ids[0]).status == "pending"


def test_a_run_still_in_flight_is_not_replayed_and_books_once_when_it_ends(env):
    corpus(env.world)
    tid, owner = start_only(env)

    async def go():
        r = tn._Runner(tid, "new-owner")
        async with env.world.factory() as db:
            await db.execute(update(EvalTournament).where(EvalTournament.id == tid).values(lease_owner="new-owner", lease_until=tn._lease_end()))
            await db.commit()
        assert await r._load()
        await r._make_primaries(1)
        async with env.world.factory() as db:
            m = (await db.execute(select(EvalMatch).where(EvalMatch.tournament_id == tid).order_by(EvalMatch.id))).scalars().first()
            run = Run(id=uuid.uuid4(), project_id=r.project_id, harness_id=r.harness_id, task_type="structured_call",
                      task_input={}, document_ids=[], status="running")
            db.add(run)
            await db.flush()
            m.status, m.run_id, m.claimed_by = "running", run.id, "old-owner"
            await db.commit()
            mid, rid = m.id, run.id

        async def state():
            async with env.world.factory() as db:
                m = await db.get(EvalMatch, mid)
                t = await db.get(EvalTournament, tid)
                return m.status, Decimal(t.spent_usd)

        await r._recover_orphans()
        assert await state() == ("running", Decimal(0))  # in flight: left alone
        async with env.world.factory() as db:
            run = await db.get(Run, rid)
            run.status, run.reported_cost_usd = "completed", Decimal("0.2")
            await db.commit()
        await r._recover_orphans()
        assert await state() == ("pending", Decimal("0.2"))
        await r._recover_orphans()
        assert await state() == ("pending", Decimal("0.2"))  # booked once

    asyncio.run(go())
    assert len(ledger(env, tid)) == 1 and env.juror.calls == []


def test_a_run_that_never_finishes_is_taken_as_dead_after_the_stale_window(env, monkeypatch):
    monkeypatch.setattr(tn, "RUN_STALE_SECONDS", 0.0)
    corpus(env.world)
    tid, owner = start_only(env)

    async def go():
        r = tn._Runner(tid, owner)
        assert await r._load()
        await r._make_primaries(1)
        async with env.world.factory() as db:
            m = (await db.execute(select(EvalMatch).where(EvalMatch.tournament_id == tid))).scalars().first()
            run = Run(id=uuid.uuid4(), project_id=r.project_id, harness_id=r.harness_id, task_type="structured_call",
                      task_input={}, document_ids=[], status="running")
            db.add(run)
            await db.flush()
            m.status, m.run_id = "running", run.id
            await db.commit()
            mid = m.id
        await r._recover_orphans()
        async with env.world.factory() as db:
            return (await db.get(EvalMatch, mid)).status

    assert asyncio.run(go()) == "pending"


# ── L2: a stale in-process task is replaced ──────────────────────────────────────────────────
def test_resuming_a_stalled_tournament_in_the_same_process_replaces_the_stale_task(env):
    corpus(env.world)

    async def go():
        async with env.world.factory() as db:
            t = await create(env, db)
            await db.commit()
            await tn.start_tournament(db, workspace_id=env.world.ws.id, tournament_id=t.id, budget_usd=100, services=env.svc, spawn=False)
        stale = asyncio.create_task(asyncio.sleep(3600))  # a runner that stopped but is still registered
        tn._tasks[t.id] = (stale, "old-owner")
        async with env.world.factory() as db:
            await db.execute(update(EvalTournament).where(EvalTournament.id == t.id).values(lease_until=tn._now().replace(year=2000)))
            await db.commit()
        async with env.world.factory() as db:
            await tn.resume_tournament(db, workspace_id=env.world.ws.id, tournament_id=t.id)  # spawns
        assert await tn.drain(120)
        return t.id, stale

    tid, stale = asyncio.run(go())
    assert stale.cancelled()
    assert fetch(env.world, lambda db: db.get(EvalTournament, tid)).status == "completed"


# ── L3: unusable text is permanent ───────────────────────────────────────────────────────────
def test_text_errors_are_not_replayed_on_resume_and_the_pair_counts_as_played(env):
    corpus(env.world)
    tid, owner = start_only(env)

    async def go():
        r = tn._Runner(tid, owner)
        assert await r._load()
        await r._make_primaries(1)
        async with env.world.factory() as db:
            ms = list((await db.execute(select(EvalMatch).where(EvalMatch.tournament_id == tid, EvalMatch.pool_key == "division:10")
                                        .order_by(EvalMatch.id))).scalars())
            ms[0].status, ms[0].error = "failed", "text_error: text blob x is missing"
            ms[1].status, ms[1].error = "failed", "provider: eval_juror: down"
            for m in ms[2:]:
                m.status, m.verdict = "done", "a"
            t = await db.get(EvalTournament, tid)
            t.status, t.pause_reason, t.lease_owner = "paused", "match_failures", None
            await db.commit()
            return ms[0].id, ms[1].id, {frozenset((str(ms[0].a_member_id), str(ms[0].b_member_id)))}
        
    textual, replayable, bad_pair = asyncio.run(go())
    asyncio.run(_resume_run(env, tid, spawn=False))
    st = fetch(env.world, lambda db: db.execute(select(EvalMatch.id, EvalMatch.status))).all()
    st = dict(st)
    assert st[textual] == "failed" and st[replayable] == "pending"

    async def next_round():
        async with env.world.factory() as db:
            lease_owner = (await db.get(EvalTournament, tid)).lease_owner
        r = tn._Runner(tid, lease_owner)
        assert await r._load()
        async with env.world.factory() as db:  # round 1 is accepted for this check
            for m in (await db.execute(select(EvalMatch).where(EvalMatch.tournament_id == tid))).scalars():
                if m.status == "pending":
                    m.status, m.verdict = "done", "a"
            await db.commit()
        await r._make_primaries(2)
        async with env.world.factory() as db:
            return [frozenset((str(m.a_member_id), str(m.b_member_id))) for m in (await db.execute(
                select(EvalMatch).where(EvalMatch.tournament_id == tid, EvalMatch.round == 2, EvalMatch.pool_key == "division:10"))).scalars()]

    pairs = asyncio.run(next_round())
    assert pairs and not (set(pairs) & bad_pair)


# ── L5 ────────────────────────────────────────────────────────────────────────────────────────
def test_the_matches_status_filter_accepts_running(env):
    corpus(env.world)
    tid, owner = start_only(env)

    async def mark():
        r = tn._Runner(tid, owner)
        assert await r._load()
        await r._make_primaries(1)
        async with env.world.factory() as db:
            m = (await db.execute(select(EvalMatch).where(EvalMatch.tournament_id == tid))).scalars().first()
            m.status = "running"
            await db.commit()

    asyncio.run(mark())
    with client(env) as c:
        r = c.get(f"/api/eval/tournaments/{tid}/matches", params={"status": "running"})
        assert r.status_code == 200 and r.json()["total"] == 1 and r.json()["matches"][0]["status"] == "running"
        assert c.get(f"/api/eval/tournaments/{tid}/matches", params={"status": "bogus"}).status_code == 422
