"""Engine 2 pass: arms, freezes, runner, gates, resume contract, cancel, claims, API, migration."""
# ruff: noqa: F811
from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from tests.eval_credibility_support import (  # noqa: F401
    COST, EXPERIMENT, body, client, create, env, fetch, get, make_env, model, reviews, seed_reports, start, world,
)
from tests.extract_fakes import http_error
from tests.test_route_jury import FIVE, no_router_call  # noqa: F401
from tret.config import get_settings
from tret.db.models import CredibilityReviewRun, RoutingFreeze, Run
from tret.evaluation import credibility as cr


def reports(env, n=6, score=None):
    return seed_reports(env.world, [body(i, score=(score if score is not None else 3 + i % 5)) for i in range(n)])


def resume(env, pid, **kw):
    async def go():
        async with env.world.factory() as db:
            await cr.resume_pass(db, workspace_id=env.world.ws.id, pass_id=pid, **kw)
        assert await cr.drain(120)

    asyncio.run(go())


def raises(status, fn):
    with pytest.raises(cr.CredibilityError) as e:
        asyncio.run(fn())
    assert e.value.status == status
    return e.value


# ── arms ─────────────────────────────────────────────────────────────────────────────────────
def test_arm_assignment_is_deterministic_and_weighted():
    arms = [{"key": "a", "weight": 3}, {"key": "b", "weight": 1}]
    pid = uuid.UUID(int=7)
    ids = [uuid.UUID(int=i) for i in range(2000)]
    first = [cr.assign_arm(pid, i, arms) for i in ids]
    assert first == [cr.assign_arm(pid, i, arms) for i in ids]
    share = first.count("a") / len(first)
    assert 0.70 < share < 0.80
    assert [cr.assign_arm(uuid.UUID(int=8), i, arms) for i in ids[:50]] != first[:50]


def test_pins_are_rejected():
    for pol in ({"mode": "pinned", "model": "x/y"}, {"model": "x/y"}, {"mode": "auto", "allowed": ["x/y"]}):
        with pytest.raises(cr.CredibilityError):
            cr.normalise_arms([{"key": "a", "model_policy": pol}])
    with pytest.raises(cr.CredibilityError):
        cr.normalise_arms([{"key": "a"}, {"key": "a"}])
    assert len(cr.normalise_arms(EXPERIMENT)) == 2


# ── happy path, freezes ──────────────────────────────────────────────────────────────────────
def test_pass_runs_with_one_frozen_model_per_arm_and_releases(env):
    batch, ids = reports(env)
    pid = create(env, batch_id=batch)
    p = get(env, pid)
    assert p.status == "draft" and not env.judge.calls and {r.arm_key for r in reviews(env, pid)} <= {"open", "any"}
    assert fetch(env.world, lambda db: db.execute(select(RoutingFreeze))).scalars().all() == []
    start(env, pid)
    p = get(env, pid)
    assert p.status == "completed" and p.lease_owner is None and p.finished_at
    rs = reviews(env, pid)
    assert all(r.status == "done" and 1 <= r.credibility_score <= 10 and len(r.pushbacks) == 3 and r.input_mode == "whole" for r in rs)
    models = {a["key"]: a["model"] for a in p.arms}
    assert models["open"] != models["any"] and models["open"].endswith("llama-a")
    assert all(r.model == models[r.arm_key] for r in rs)
    freezes = fetch(env.world, lambda db: db.execute(select(RoutingFreeze))).scalars().all()
    assert len(freezes) == 2 and all(f.purpose == "sealed_call" and f.k == 1 and f.status == "released" for f in freezes)
    assert {f.pass_ref for f in freezes} == {f"credibility:{pid}:open", f"credibility:{pid}:any"}
    assert Decimal(p.spent_usd) == COST * len(rs) == sum(Decimal(r.cost_usd) for r in rs)
    ledger = fetch(env.world, lambda db: db.execute(select(CredibilityReviewRun))).scalars().all()
    assert len(ledger) == len(rs)
    wire = {env.cat.get(m).wire_id for m in models.values()}
    assert {c["model"] for c in env.judge.calls} <= wire


def test_freeze_is_idempotent_per_arm(env):
    from tret.evaluation import jury
    async def go():
        async with env.world.factory() as db:
            pol = {"mode": "auto"}
            a, c1 = await jury.freeze_jury(db, workspace_id=env.world.ws.id, pass_ref="credibility:x:a", purpose="sealed_call", model_policy=pol, k=1, services=env.svc)
            b, c2 = await jury.freeze_jury(db, workspace_id=env.world.ws.id, pass_ref="credibility:x:a", purpose="sealed_call", model_policy=pol, k=1, services=env.svc)
            return a.id == b.id, c1, c2
    assert asyncio.run(go()) == (True, True, False)


# ── budget, outage ───────────────────────────────────────────────────────────────────────────
def test_budget_pauses_instead_of_overspending(env):
    batch, _ = reports(env, 8)
    pid = create(env, batch_id=batch)
    start(env, pid, budget=Decimal("0.05"))
    p = get(env, pid)
    assert p.status == "paused" and p.pause_reason == "budget" and Decimal(p.spent_usd) <= Decimal("0.05") + 2 * COST  # only the calls in flight before any cost was observed can overshoot
    assert len(env.judge.calls) < 8
    assert any(r.status == "pending" for r in reviews(env, pid))
    resume(env, pid, budget_usd=100)
    assert get(env, pid).status == "completed" and all(r.status == "done" for r in reviews(env, pid))


def test_provider_outage_pauses_not_fails(env):
    batch, _ = reports(env, 6)
    pid = create(env, batch_id=batch)
    env.judge.fail = http_error(503, "down")
    start(env, pid)
    p = get(env, pid)
    assert p.status == "paused" and p.pause_reason == "provider_unavailable"
    assert not any(r.status == "failed" for r in reviews(env, pid))
    env.judge.fail = None
    resume(env, pid)
    assert get(env, pid).status == "completed" and all(r.status == "done" for r in reviews(env, pid))
    assert len(fetch(env.world, lambda db: db.execute(select(CredibilityReviewRun))).scalars().all()) == len(fetch(env.world, lambda db: db.execute(select(Run))).scalars().all())  # one ledger row per run, however many calls it made


def test_unroutable_frozen_model_pauses(env):
    batch, _ = reports(env, 3)
    pid = create(env, batch_id=batch)
    owner = start(env, pid, spawn=False)
    env.cat._static.clear()  # the frozen models vanish from the catalog

    async def go():
        await cr.run_pass(pid, owner)
    asyncio.run(go())
    assert get(env, pid).status == "paused"


# ── gates and the resume contract ────────────────────────────────────────────────────────────
def test_gate_math():
    th = {"clumping_pause": 0.7, "clumping_warn": 0.6, "clumping_min_n": 30, "failure_rate_pause": 0.25, "failure_min_attempts": 10}
    g = {x["key"]: x for x in cr.compute_gates([("done", 7)] * 29, th)}
    assert g["score_clumping"]["state"] == "insufficient_data"
    g = {x["key"]: x for x in cr.compute_gates([("done", 7)] * 25 + [("done", 2)] * 5, th)}
    assert g["score_clumping"]["state"] == "tripped" and g["failure_rate"]["state"] == "ok"
    g = {x["key"]: x for x in cr.compute_gates([("done", 7)] * 19 + [("done", 2)] * 11, th)}
    assert g["score_clumping"]["state"] == "warn"
    g = {x["key"]: x for x in cr.compute_gates([("done", 7)] * 6 + [("failed", None)] * 4, th)}
    assert g["failure_rate"]["state"] == "tripped"


def test_clumping_gate_pauses_and_resume_needs_acknowledgement(env, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "eval_gate_clumping_min_n", 4)
    monkeypatch.setattr(s, "eval_credibility_concurrency", 1)
    batch, _ = reports(env, 8, score=7)
    pid = create(env, batch_id=batch)
    start(env, pid)
    p = get(env, pid)
    assert p.status == "paused" and p.pause_reason == "score_clumping"
    assert cr.required_gates(p) == ["score_clumping"] and any(r.status == "pending" for r in reviews(env, pid))
    raises(409, lambda: _resume(env, pid))  # not acknowledged
    raises(409, lambda: _resume(env, pid, ack=["failure_rate"], note="x"))  # wrong set
    raises(422, lambda: _resume(env, pid, ack=["score_clumping"], note=" "))  # no note
    resume(env, pid, acknowledge_gates=["score_clumping"], note="judged by hand: reports really are alike")
    p = get(env, pid)
    assert p.status == "completed" and p.acknowledged_gates[0]["gate"] == "score_clumping" and p.acknowledged_gates[0]["note"]


async def _resume(env, pid, ack=None, note=None):
    async with env.world.factory() as db:
        await cr.resume_pass(db, workspace_id=env.world.ws.id, pass_id=pid, acknowledge_gates=ack, note=note, spawn=False)


def test_failure_rate_gate_pauses(env, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "eval_gate_failure_rate_pause", 0.25)
    monkeypatch.setattr(s, "eval_credibility_concurrency", 1)
    batch, _ = reports(env, 14)
    pid = create(env, batch_id=batch)
    env.judge.payload = {"credibility_score": 99}  # never valid: fails after the repair retry
    start(env, pid)
    p = get(env, pid)
    assert p.status == "paused" and p.pause_reason == "failure_rate"
    failed = [r for r in reviews(env, pid) if r.status == "failed"]
    assert len(failed) >= 10 and all("invalid_json" in r.error for r in failed)
    assert Decimal(p.spent_usd) == COST * len(env.judge.calls)  # failed calls are booked too


# ── cancel, claims ───────────────────────────────────────────────────────────────────────────
def test_cancel_books_in_flight_spend_once(env):
    batch, _ = reports(env, 4)
    pid = create(env, batch_id=batch)
    gate = {}

    async def go():
        started = asyncio.Event()
        release = asyncio.Event()

        async def block(n):
            started.set()
            await release.wait()

        env.judge.on_call = block
        async with env.world.factory() as db:
            await cr.start_pass(db, workspace_id=env.world.ws.id, pass_id=pid, budget_usd=100, services=env.svc)
        await asyncio.wait_for(started.wait(), 20)
        async with env.world.factory() as db:
            p = await cr.cancel_pass(db, workspace_id=env.world.ws.id, pass_id=pid)
            gate["status"] = p.status
        release.set()
        env.judge.on_call = None
        assert await cr.drain(60)

    asyncio.run(go())
    p = get(env, pid)
    assert gate["status"] == "cancelled" and p.status == "cancelled"
    ledger = fetch(env.world, lambda db: db.execute(select(CredibilityReviewRun))).scalars().all()
    assert len({r.run_id for r in ledger}) == len(ledger) == len(env.judge.calls)
    assert Decimal(p.spent_usd) == COST * len(ledger)
    assert not any(r.status in ("pending", "running") for r in reviews(env, pid))
    freezes = fetch(env.world, lambda db: db.execute(select(RoutingFreeze))).scalars().all()
    assert all(f.status == "released" for f in freezes)
    raises(409, lambda: _cancel(env, pid))


async def _cancel(env, pid):
    async with env.world.factory() as db:
        await cr.cancel_pass(db, workspace_id=env.world.ws.id, pass_id=pid)


def test_cancel_books_a_dead_runners_run_exactly_once(env):
    batch, _ = reports(env, 2)
    pid = create(env, batch_id=batch)
    start(env, pid, spawn=False)

    async def go():
        async with env.world.factory() as db:
            rv = (await db.execute(select(cr.CredibilityReview).where(cr.CredibilityReview.pass_id == pid))).scalars().first()
            run = Run(id=uuid.uuid4(), project_id=(await db.get(cr.CredibilityPass, pid)).project_id, harness_id=None, task_type="structured_call",
                      task_input={}, document_ids=[], status="cancelled", reported_cost_usd=Decimal("0.5"), cost_usd=Decimal("0.5"))
            harness = (await db.execute(select(__import__("tret.db.models", fromlist=["Harness"]).Harness))).scalars().first()
            run.harness_id = harness.id
            db.add(run)
            await db.flush()
            rv.status, rv.run_id, rv.claimed_by = "running", run.id, "dead"
            await db.commit()
        await _cancel(env, pid)

    asyncio.run(go())
    p = get(env, pid)
    assert Decimal(p.spent_usd) == Decimal("0.5") and p.status == "cancelled"
    assert len(fetch(env.world, lambda db: db.execute(select(CredibilityReviewRun))).scalars().all()) == 1


def test_two_runners_never_double_claim(env):
    batch, _ = reports(env, 6)
    pid = create(env, batch_id=batch)
    owner = start(env, pid, spawn=False)

    async def go():
        # the per-review claim: the same review offered twice under one lease owner is played once
        r = cr._Runner(pid, owner)
        assert await r._claim() and await r._load()
        async with env.world.factory() as db:
            rid = (await db.execute(select(cr.CredibilityReview.id).where(cr.CredibilityReview.pass_id == pid))).scalars().first()
        await asyncio.gather(r._play(rid), r._play(rid), r._play(rid))
        assert len(env.judge.calls) == 1
        # and two whole runners: the lease keeps the second out
        await asyncio.gather(cr.run_pass(pid, owner), cr.run_pass(pid, "someone-else"))

    asyncio.run(go())
    assert get(env, pid).status == "completed"
    assert len(env.judge.calls) == 6
    assert len(fetch(env.world, lambda db: db.execute(select(CredibilityReviewRun))).scalars().all()) == 6


def test_book_run_is_once_per_run(env):
    batch, _ = reports(env, 1)
    pid = create(env, batch_id=batch)
    start(env, pid)

    async def go():
        async with env.world.factory() as db:
            run = (await db.execute(select(Run))).scalars().first()
            again = await cr.book_run(db, pid, run, None)
            await db.commit()
            return again

    assert asyncio.run(go()) == 0
    assert Decimal(get(env, pid).spent_usd) == COST


def test_failed_start_leaves_no_freeze_behind(env):
    batch, _ = reports(env, 3)
    arms = [EXPERIMENT[0], {"key": "none", "weight": 1, "model_policy": {"mode": "auto", "allowed": ["openrouter/nope/a", "openrouter/nope/b"]}}]
    pid = create(env, batch_id=batch, arms=arms)
    for _ in range(2):  # the second try fails the same way (it used to 409 on a released freeze)
        async def go():
            async with env.world.factory() as db:
                await cr.start_pass(db, workspace_id=env.world.ws.id, pass_id=pid, budget_usd=5, services=env.svc, spawn=False)
        with pytest.raises(cr.CredibilityError) as e:
            asyncio.run(go())
        assert e.value.status == 422
    assert fetch(env.world, lambda db: db.execute(select(RoutingFreeze))).scalars().all() == []
    assert get(env, pid).status == "draft"


def test_cancel_of_a_paused_pass_books_its_orphaned_run(env):
    batch, _ = reports(env, 2)
    pid = create(env, batch_id=batch)
    start(env, pid, spawn=False)

    async def go():
        async with env.world.factory() as db:
            from tret.db.models import Harness
            rv = (await db.execute(select(cr.CredibilityReview).where(cr.CredibilityReview.pass_id == pid))).scalars().first()
            h = (await db.execute(select(Harness))).scalars().first()
            run = Run(id=uuid.uuid4(), project_id=(await db.get(cr.CredibilityPass, pid)).project_id, harness_id=h.id, task_type="structured_call",
                      task_input={}, document_ids=[], status="running", reported_cost_usd=Decimal("0.7"), cost_usd=Decimal("0.7"))
            db.add(run)
            await db.flush()
            rv.status, rv.run_id, rv.claimed_by = "running", run.id, "dead"
            await db.commit()
        await cr.pause_pass(pid, reason="interrupted", session_factory=env.world.factory)
        await _cancel(env, pid)

    asyncio.run(go())
    p = get(env, pid)
    assert p.status == "cancelled" and Decimal(p.spent_usd) == Decimal("0.7")
    assert not any(r.status in ("running", "pending") for r in reviews(env, pid))


def test_window_steps_down_and_arm_stats_report_digest_share_and_ledger_cost(world, tmp_path, monkeypatch):
    models = (model("openai/gpt-a", window=100_000), model("meta-llama/llama-a", price="4", window=64_000, open_weights=True))
    env = make_env(world, tmp_path, monkeypatch, models=models)
    long = "# Report\n\n## Principal risks\n" + " ".join(f"risk{i}" for i in range(60_000))  # ~75k tokens
    batch, _ = seed_reports(world, [long, body(1), body(2), body(3)])
    pid = create(env, batch_id=batch, arms=[EXPERIMENT[0]])
    arm = get(env, pid).arms[0]
    assert arm["min_context_window"] == 64_000  # not the 32k floor, and not the 77k the report wanted
    est = get(env, pid).estimates["arms"][0]
    assert est["digest_reports"] == 1 and est["whole_reports"] == 3
    start(env, pid)
    p = get(env, pid)
    assert p.status == "completed" and p.arms[0]["max_output_tokens"] == 2000
    out = asyncio.run(_detail(env, pid))
    st = out["arm_stats"][0]
    assert st["n_digest"] == 1 and st["digest_share"] == 0.25
    assert sum(a["cost_usd"] for a in out["arm_stats"]) == pytest.approx(float(p.spent_usd))
    assert {r.input_mode for r in reviews(env, pid)} == {"whole", "digest"}


async def _detail(env, pid):
    async with env.world.factory() as db:
        return await cr.pass_detail(db, await db.get(cr.CredibilityPass, pid))


def test_arm_costs_add_up_to_spend(env):
    batch, _ = reports(env, 6)
    pid = create(env, batch_id=batch)
    start(env, pid)
    out = asyncio.run(_detail(env, pid))
    assert len(out["arm_stats"]) == 2 and sum(a["cost_usd"] for a in out["arm_stats"]) == pytest.approx(out["spent_usd"])


def test_the_arms_output_cap_is_the_one_used(env, monkeypatch):
    batch, _ = reports(env, 2)
    pid = create(env, batch_id=batch, arms=[EXPERIMENT[1]])
    start(env, pid, spawn=False)
    monkeypatch.setattr(get_settings(), "eval_credibility_max_output_tokens", 700)  # changed after the pass started
    owner = get(env, pid).lease_owner
    asyncio.run(cr.run_pass(pid, owner))
    assert {c["max_tokens"] for c in env.judge.calls} == {2000}


def test_skipped_reports_are_recorded(env):
    b1, ok = reports(env, 2)
    b2, bad = seed_reports(env.world, ["x"], status="quarantined")

    async def go():
        async with env.world.factory() as db:
            p = await cr.create_pass(db, workspace_id=env.world.ws.id, arms=EXPERIMENT, disclosure_report_ids=ok + bad + [uuid.uuid4()], services=env.svc)
            await db.commit()
            return p.id

    pid = asyncio.run(go())
    rs = reviews(env, pid)
    sk = [r for r in rs if r.status == "skipped"]
    assert len(rs) == 3 and len(sk) == 1 and "quarantined" in sk[0].error
    assert get(env, pid).selection["rejected_ids"]


# ── API ──────────────────────────────────────────────────────────────────────────────────────
def test_api_404_when_off(env, monkeypatch):
    monkeypatch.setattr(get_settings(), "eval_credibility", False)
    with client(env) as c:
        assert c.get("/api/eval/credibility").status_code == 404
        assert c.post("/api/eval/credibility/preview", json={"arms": EXPERIMENT, "batch_id": str(uuid.uuid4())}).status_code == 404


def test_preview_writes_nothing_and_calls_no_model(env):
    batch, _ = reports(env, 4)
    with client(env) as c:
        r = c.post("/api/eval/credibility/preview", json={"arms": EXPERIMENT, "batch_id": str(batch), "budget_usd": 5})
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["reports"] == 4 and j["input"]["whole"] == 8 and {a["key"] for a in j["arms"]} == {"open", "any"}
        assert all(a["pick"]["why"] for a in j["arms"]) and j["cost_usd"]["expected"] > 0 and j["within_budget"] is True
        bad = c.post("/api/eval/credibility/preview", json={"arms": [{"key": "a", "model_policy": {"model": "x/y"}}], "batch_id": str(batch)})
        assert bad.status_code == 422
    assert not env.judge.calls
    assert fetch(env.world, lambda db: db.execute(select(cr.CredibilityPass))).scalars().all() == []
    assert fetch(env.world, lambda db: db.execute(select(RoutingFreeze))).scalars().all() == []


def test_api_flow_reviews_verify_resume_and_delete(env):
    batch, _ = reports(env, 6)
    with client(env) as c:
        j = c.post("/api/eval/credibility", json={"arms": EXPERIMENT, "batch_id": str(batch), "limit": 4, "sample_seed": 3, "name": "t"}).json()
        pid = j["id"]
        assert j["status"] == "draft" and j["counts"]["pending"] == 4 and j["selection"]["sample_seed"] == 3
        assert c.delete(f"/api/eval/credibility/{pid}").status_code == 200
        pid = c.post("/api/eval/credibility", json={"arms": EXPERIMENT, "batch_id": str(batch)}).json()["id"]
        assert c.post(f"/api/eval/credibility/{pid}/start", json={"budget_usd": 50}).status_code == 200
        import time
        for _ in range(200):  # the runner lives on the client's event loop: poll
            if c.get(f"/api/eval/credibility/{pid}").json()["status"] != "running":
                break
            time.sleep(0.1)
        j = c.get(f"/api/eval/credibility/{pid}").json()
        assert j["status"] == "completed" and j["counts"]["done"] == 6 and all(a["model"] for a in j["arms"])
        assert sum(sum(a["histogram"]) for a in j["arm_stats"]) == 6 and {g["key"] for g in j["gates"]} == {"score_clumping", "failure_rate"}
        assert c.delete(f"/api/eval/credibility/{pid}").status_code == 409
        lst = c.get(f"/api/eval/credibility/{pid}/reviews", params={"arm": "open"}).json()
        assert lst["total"] == len([r for r in reviews(env, uuid.UUID(pid)) if r.arm_key == "open"])
        rid = c.get(f"/api/eval/credibility/{pid}/reviews").json()["reviews"][0]["id"]
        d = c.get(f"/api/eval/credibility/reviews/{rid}").json()
        assert d["company"].startswith("Company") and len(d["pushbacks"]) == 3
        v = c.post(f"/api/eval/credibility/reviews/{rid}/verify").json()
        assert v["ok"] and v["prompt_sha256"]["match"]
        assert c.get(f"/api/eval/credibility/{uuid.uuid4()}").status_code == 404
        assert c.post(f"/api/eval/credibility/{pid}/resume", json={}).status_code == 409


# ── migration ────────────────────────────────────────────────────────────────────────────────
def test_migration_up_and_down_on_sqlite(tmp_path):
    import importlib.util
    from pathlib import Path

    import sqlalchemy as sa
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    from tests.evals.golden_world import install_sqlite_type_shims
    from tret.db.models import Base

    install_sqlite_type_shims()
    mine = ("credibility_review_runs", "credibility_reviews", "credibility_passes")
    spec = importlib.util.spec_from_file_location("mig", next(Path(cr.__file__).parents[2].glob("alembic/versions/d7b3f1a9c542_*.py")))
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    mig._now = lambda: sa.text("(CURRENT_TIMESTAMP)")
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    with engine.begin() as conn:
        Base.metadata.create_all(conn, tables=[t for n, t in Base.metadata.tables.items() if n not in mine])
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            mig.upgrade()
            assert set(mine) <= set(sa.inspect(conn).get_table_names())
            mig.downgrade()
            assert not set(mine) & set(sa.inspect(conn).get_table_names())
            mig.upgrade()
    assert mig.down_revision == "c5d1a8f3e726"



# ── second review round ──────────────────────────────────────────────────────────────────────
def test_context_overflow_retries_once_with_a_smaller_budget_and_verifies(env):
    long = "# R\n\n## Principal risks\n" + " ".join(f"risk{i}" for i in range(9000))  # ~55k chars
    batch, _ = seed_reports(env.world, [long])
    pid = create(env, batch_id=batch, arms=[EXPERIMENT[1]])
    real = env.judge.complete_json

    async def tight(**kw):  # the provider counts more tokens than the estimate: refuses the whole report
        if len(kw["prompt"]) > 60_000:
            raise http_error(400, "prompt is too long: context length exceeded")
        return await real(**kw)

    env.judge.complete_json = tight
    start(env, pid)
    (rv,) = reviews(env, pid)
    assert rv.status == "done", (rv.error, rv.input_budget, rv.attempts, rv.input_mode)
    assert rv.input_mode == "digest" and rv.input_budget and rv.attempts == 2
    assert get(env, pid).status == "completed"

    async def verify():
        async with env.world.factory() as db:
            return await cr.verify_review(db, workspace_id=env.world.ws.id, review_id=rv.id)

    v = asyncio.run(verify())
    assert v["ok"], v
    # a second overflow is final
    batch2, _ = seed_reports(env.world, [long])
    pid2 = create(env, batch_id=batch2, arms=[EXPERIMENT[1]])

    async def always(**kw):
        raise http_error(400, "prompt is too long: context length exceeded")

    env.judge.complete_json = always
    start(env, pid2)
    (rv2,) = reviews(env, pid2)
    assert rv2.status == "failed" and "context_overflow" in rv2.error


def test_failed_start_keeps_a_concurrent_starts_freezes(env):
    batch, _ = reports(env, 3)
    arms = [EXPERIMENT[1], {"key": "none", "weight": 1, "model_policy": {"mode": "auto", "allowed": ["openrouter/nope/a", "openrouter/nope/b"]}}]
    pid = create(env, batch_id=batch, arms=arms)

    async def go():
        from tret.evaluation import jury
        async with env.world.factory() as db:  # "the other start" froze the first arm already
            await jury.freeze_jury(db, workspace_id=env.world.ws.id, pass_ref=f"credibility:{pid}:any", purpose="sealed_call",
                                   model_policy=arms[0]["model_policy"], k=1, min_context_window=32_000, services=env.svc)
            await db.commit()
        async with env.world.factory() as db:
            await cr.start_pass(db, workspace_id=env.world.ws.id, pass_id=pid, budget_usd=5, services=env.svc, spawn=False)

    with pytest.raises(cr.CredibilityError):
        asyncio.run(go())
    fr = fetch(env.world, lambda db: db.execute(select(RoutingFreeze))).scalars().all()
    assert [f.status for f in fr] == ["active"]


def test_a_one_model_allowed_list_is_a_pin_once_unknown_names_are_dropped(env):
    real = "openrouter/openai/gpt-a"
    with pytest.raises(cr.CredibilityError):
        cr.normalise_arms([{"key": "a", "model_policy": {"mode": "auto", "allowed": [real, "no-such-model"]}}], env.cat)
    assert cr.normalise_arms([{"key": "a", "model_policy": {"mode": "auto", "allowed": [real, "openrouter/openai/gpt-b"]}}], env.cat)


def _block_then(env, pid, between, after=None):
    """Start the pass with every call parked, run `between` while they are in flight, then let them finish."""
    async def go():
        started = asyncio.Event()
        release = asyncio.Event()

        async def block(n):
            started.set()
            await release.wait()

        env.judge.on_call = block
        async with env.world.factory() as db:
            await cr.start_pass(db, workspace_id=env.world.ws.id, pass_id=pid, budget_usd=100, services=env.svc)
        await asyncio.wait_for(started.wait(), 20)
        await between()
        if after:
            after()
        release.set()
        assert await cr.drain(60)

    asyncio.run(go())


def test_cancel_with_a_live_runner_keeps_the_in_flight_results(env):
    batch, _ = reports(env, 4)
    pid = create(env, batch_id=batch)
    _block_then(env, pid, lambda: _cancel(env, pid))
    rs = reviews(env, pid)
    assert sum(1 for r in rs if r.status == "done") == len(env.judge.calls) >= 1  # paid results are recorded, not discarded
    assert all(r.status in ("done", "skipped") for r in rs)
    assert Decimal(get(env, pid).spent_usd) == COST * len(env.judge.calls)


def test_a_retry_settled_after_cancel_is_skipped_not_pending(env):
    batch, _ = reports(env, 4)
    pid = create(env, batch_id=batch)
    _block_then(env, pid, lambda: _cancel(env, pid), after=lambda: setattr(env.judge, "fail", http_error(503, "down")))
    rs = reviews(env, pid)
    assert not any(r.status in ("pending", "running") for r in rs)
    assert get(env, pid).status == "cancelled"


def test_cancel_after_a_lease_lapse_does_not_book_the_live_call_at_zero(env):
    batch, _ = reports(env, 2)
    pid = create(env, batch_id=batch)

    async def lapse_then_cancel():
        async with env.world.factory() as db:
            p = await db.get(cr.CredibilityPass, pid)
            p.lease_until = cr._now().replace(year=2000)  # the heartbeat missed: the runner still lives
            await db.commit()
        await _cancel(env, pid)

    _block_then(env, pid, lapse_then_cancel)
    ledger = fetch(env.world, lambda db: db.execute(select(CredibilityReviewRun))).scalars().all()
    assert len(ledger) == len({r.run_id for r in ledger}) >= 1
    assert all(Decimal(r.cost_usd) == COST for r in ledger)  # the real cost, never a placeholder 0
    assert Decimal(get(env, pid).spent_usd) == COST * len(ledger)
