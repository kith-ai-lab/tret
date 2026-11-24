"""The Engine 1 tournament end to end with a fake juror whose verdicts follow a hidden true
strength (no network): preview, freeze, blind prompts, run accounting, budget, cancel, recovery,
ratings, the API and the CLI renderers."""
# ruff: noqa: F811 - imported fixtures are used by name as test arguments
from __future__ import annotations

import asyncio
import re
import uuid
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.eval_corpus import MODULE, seed_corpus, spec
from tests.extract_fakes import http_error
from tests.test_batches_core import world  # noqa: F401 - fixture
from tests.test_route_jury import FIVE, Registry, catalog, no_router_call  # noqa: F401 - autouse fixture
from tret import cli_eval
from tret.api import eval_tournaments as api
from tret.api.auth import current_user
from tret.api.workspace import WorkspaceContext, current_workspace
from tret.batches.rubrics import JUROR_KEY
from tret.config import get_settings
from tret.db.engine import get_db
from tret.db.models import (
    EvalMatch,
    EvalTournament,
    ModuleRating,
    PeerPoolMember,
    RoutingFreeze,
    Run,
    RunOutcome,
    Workspace,
)
from tret.engine import direct
from tret.engine.routed_call import CallServices
from tret.evaluation import jury
from tret.evaluation import tournament as tn
from tret.evaluation.ratings import spearman
from tret.providers.base import JsonCompletion, Usage
from tret.router_llm.router import ModelRouter
from tret.storage import reset_store

COST = Decimal("0.0123")
STRENGTH = re.compile(r"STRENGTH=(\d+)")


class FakeJuror:
    """Answers by the hidden STRENGTH of the two texts in the prompt; records every prompt."""

    def __init__(self):
        self.calls: list[dict] = []
        self.fail: BaseException | None = None
        self.on_call = None

    async def complete_json(self, **kw):
        self.calls.append(kw)
        if self.on_call:
            await self.on_call(len(self.calls))
        if self.fail is not None:
            raise self.fail
        s = [int(x) for x in STRENGTH.findall(kw["prompt"])]
        assert len(s) == 2, kw["prompt"]
        verdict = "tie" if s[0] == s[1] else ("1" if s[0] > s[1] else "2")
        return JsonCompletion(
            payload={"verdict": verdict, "strength": "clear", "rationale": "more specific"},
            usage=Usage(1000, 100, reported_cost_usd=COST), model=kw["model"], served_by="fake",
        )


@pytest.fixture
def env(world, tmp_path, monkeypatch):
    s = get_settings()
    for k, v in (("eval_jury", "on"), ("eval_tournament", "on"), ("eval_tournament_concurrency", 2),
                 ("storage_dir", str(tmp_path / "storage")), ("batches_min_free_mb_per_target", 0)):
        monkeypatch.setattr(s, k, v)
    reset_store()
    juror = FakeJuror()
    reg = Registry(juror)
    cat = catalog(*FIVE)
    svc = CallServices(reg, ModelRouter(cat, reg), cat)

    async def fake_services(db, workspace_id):
        return svc

    monkeypatch.setattr(direct, "get_call_services", fake_services)
    monkeypatch.setattr(jury, "get_call_services", fake_services)
    monkeypatch.setattr(tn, "get_session_factory", lambda: world.factory)
    yield type("Env", (), {"world": world, "juror": juror, "svc": svc, "cat": cat})()
    reset_store()


def corpus(world, n_a=8, n_b=6):
    specs = [spec(i, div="10", cluster="food", strength=i) for i in range(n_a)]
    specs += [spec(100 + i, div="20", cluster="mining", section="B", strength=i * 3 + 1) for i in range(n_b)]
    return seed_corpus(world, specs)


def create(env, db, **kw):
    return tn.create_tournament(
        db, workspace_id=env.world.ws.id, module=MODULE, min_n=4, created_by="a@example.com", seed=7, services=env.svc, **kw
    )


def play(env, budget=100, **kw):
    """Create, start and run to rest; returns (tournament id, state fetched afterwards)."""

    async def go():
        async with env.world.factory() as db:
            t = await create(env, db, **kw)
            await db.commit()
            tid = t.id
            await tn.start_tournament(db, workspace_id=env.world.ws.id, tournament_id=tid, budget_usd=budget,
                                      started_by="a@example.com", services=env.svc)
        assert await tn.drain(120)
        return tid

    return asyncio.run(go())


def fetch(world, fn):
    async def go():
        async with world.factory() as db:
            return await fn(db)

    return asyncio.run(go())


async def member_map(db, tid):
    """report module id -> pool member id of this tournament's snapshot."""
    t = await db.get(EvalTournament, tid)
    ms = (await db.execute(select(PeerPoolMember).where(PeerPoolMember.snapshot_id == t.snapshot_id))).scalars().all()
    return {m.report_module_id: m.id for m in ms}


async def everything(db, tid):
    t = await db.get(EvalTournament, tid)
    matches = list((await db.execute(select(EvalMatch).where(EvalMatch.tournament_id == tid).order_by(EvalMatch.created_at))).scalars())
    ratings = list((await db.execute(select(ModuleRating).where(ModuleRating.tournament_id == tid))).scalars())
    return t, matches, ratings


# ── preview ──────────────────────────────────────────────────────────────────────────────────
def test_create_is_a_free_preview(env):
    corpus(env.world)

    async def go():
        async with env.world.factory() as db:
            t = await create(env, db)
            await db.commit()
            return t.id

    tid = asyncio.run(go())
    t, matches, ratings = fetch(env.world, lambda db: everything(db, tid))
    assert env.juror.calls == [] and matches == [] and ratings == []
    assert t.status == "draft" and t.freeze_id is None and Decimal(t.spent_usd) == 0 and t.budget_usd is None
    est = t.estimates
    assert est["n"] == 14 and est["rounds"] == 5 and {p["key"] for p in est["pools"]} == {"division:10", "division:20"}
    assert est["matches"]["primary"] == 4 * 5 + 3 * 5 and est["matches"]["total"] > est["matches"]["primary"]
    assert 0 < est["cost_usd"]["low"] <= est["cost_usd"]["expected"] <= est["cost_usd"]["high"]
    assert len(est["jury"]["picks"]) == 3 and all(p["why"] for p in est["jury"]["picks"])
    assert fetch(env.world, lambda db: db.execute(select(Run))).scalars().all() == []
    assert fetch(env.world, lambda db: db.execute(select(RoutingFreeze))).scalars().all() == []  # not frozen until start
    text = cli_eval.render_preview(tn.tournament_out(t))
    assert "nothing spent" in text and "division:10" in text and "--budget" in text


def test_tournament_needs_the_jury_setting_and_modules_in_the_set(env, monkeypatch):
    corpus(env.world)
    monkeypatch.setattr(get_settings(), "eval_jury", "off")
    with pytest.raises(tn.TournamentError) as e:
        asyncio.run(_create_once(env))
    assert e.value.status == 409 and "TRET_EVAL_JURY" in str(e.value)
    monkeypatch.setattr(get_settings(), "eval_jury", "on")
    with pytest.raises(tn.TournamentError):
        asyncio.run(_create_once(env, module="nope"))
    with pytest.raises(tn.TournamentError):  # a pin never makes a jury
        asyncio.run(_create_once(env, model_policy={"mode": "pinned", "model": "a/b"}))


async def _create_once(env, **kw):
    async with env.world.factory() as db:
        return await tn.create_tournament(db, workspace_id=env.world.ws.id, module=kw.pop("module", MODULE), min_n=4, services=env.svc, **kw)


# ── a full run ───────────────────────────────────────────────────────────────────────────────
def test_full_tournament(env):
    ids = corpus(env.world)
    tid = play(env)
    t, matches, ratings = fetch(env.world, lambda db: everything(db, tid))
    mem = fetch(env.world, lambda db: member_map(db, tid))
    assert t.status == "completed" and t.current_round == 5 and t.pause_reason is None
    assert t.lease_owner is None and t.lease_until is None
    assert all(m.status == "done" for m in matches)

    # the jury was frozen once, for this pass, and every call used only its models
    freeze = fetch(env.world, lambda db: db.get(RoutingFreeze, t.freeze_id))
    assert (freeze.purpose, freeze.pass_ref, freeze.k) == ("eval_jury", f"tournament:{tid}", 3)
    assert freeze.status == "released"  # the pass is over
    wire = {env.cat.get(p["model"]).wire_id for p in freeze.selection["picks"]}
    assert len(wire) == 3 and {c["model"] for c in env.juror.calls} == wire
    assert len(env.juror.calls) == len(matches)
    assert {m.juror_model for m in matches} == {p["model"] for p in freeze.selection["picks"]}
    counts = {i: sum(1 for m in matches if m.kind == "primary" and m.juror_index == i) for i in range(3)}
    assert max(counts.values()) - min(counts.values()) <= 1  # the families share the load evenly

    # each match is a Run with the juror marker, no router evidence
    runs = fetch(env.world, lambda db: db.execute(select(Run))).scalars().all()
    by_id = {r.id: r for r in runs if (r.task_input or {}).get(JUROR_KEY)}
    assert len(by_id) == len(matches) and {m.run_id for m in matches} == set(by_id)
    for r in by_id.values():
        assert r.task_type == "structured_call" and r.status == "completed" and r.task_input["_eval_tournament"] == str(tid)
        assert r.routing["frozen"]["pass_ref"] == f"tournament:{tid}"
    assert fetch(env.world, lambda db: db.execute(select(RunOutcome))).scalars().all() == []
    assert Decimal(t.spent_usd) == COST * len(matches) == sum(Decimal(m.cost_usd) for m in matches)

    # blind: no company name, ticker or short name in any prompt, and only the module's own text
    for c in env.juror.calls:
        blob = (c["system"] + c["prompt"]).lower()
        assert "zorblax" not in blob and not re.search(r"\bzb\d+\b", blob)
        assert "the company" in blob

    # the shown order varies, is recorded, and matches what the juror saw
    assert {m.shown_a_first for m in matches if m.kind == "primary"} == {True, False}
    a_strength = {mem[mid]: s for mid, s in zip(ids.modules, [*range(8), *[i * 3 + 1 for i in range(6)]])}
    # (the juror answers by the order it was shown; the verdict mapping back to a/b must hold)
    for m in matches:
        sa, sb = a_strength[m.a_member_id], a_strength[m.b_member_id]
        assert m.verdict == ("tie" if sa == sb else "a" if sa > sb else "b")

    # swaps and doubles
    prim = [m for m in matches if m.kind == "primary"]
    swaps, doubles = [m for m in matches if m.kind == "swap"], [m for m in matches if m.kind == "double"]
    for r in range(1, 6):
        n = sum(1 for m in prim if m.round == r)
        assert sum(1 for m in swaps if m.round == r) == max(1, round(0.05 * n))
        assert sum(1 for m in doubles if m.round == r) == max(1, round(0.10 * n))
    byid = {m.id: m for m in matches}
    for s in swaps:
        o = byid[s.swap_of]
        assert (s.a_member_id, s.b_member_id, s.juror_index) == (o.a_member_id, o.b_member_id, o.juror_index)
        assert s.shown_a_first != o.shown_a_first
    for d in doubles:
        o = byid[d.double_of]
        assert (d.a_member_id, d.b_member_id) == (o.a_member_id, o.b_member_id) and d.juror_family != o.juror_family

    # ratings recover the hidden order within each pool; bands, summary, length r
    assert len(ratings) == 14
    for key, rng in (("division:10", range(0, 8)), ("division:20", range(8, 14))):
        rs = [r for r in ratings if r.pool_key == key]
        truth = {mem[ids.modules[i]]: a_strength[mem[ids.modules[i]]] for i in rng}
        assert len(rs) == len(truth)
        assert spearman([r.rating for r in rs], [truth[r.member_id] for r in rs]) > 0.8
    assert all(r.band_kind and r.band and r.word_count and r.hi90 > r.lo90 for r in ratings)
    s = t.summary
    assert s["flip_rate"]["overall"]["pairs"] == len(swaps) and s["flip_rate"]["overall"]["rate"] == 0.0
    assert set(s["flip_rate"]["by_family"]) <= {p["family"] for p in freeze.selection["picks"]}
    assert s["length_r"]["overall"]["n"] == 14 and set(s["length_r"]["by_pool"]) == {"division:10", "division:20"}
    assert s["claim_density_r"]["overall"]["n"] == 14
    assert s["matches"] == {"primary": len(prim), "swap": len(swaps), "double": len(doubles), "failed": 0}
    assert len(s["jury"]) == 3 and s["judge_prompt_version"] == t.judge_prompt_version

    # the preview's swap and double counts follow the runner's per-round rule
    assert t.estimates["matches"]["primary"] == len(prim) and t.estimates["matches"]["swap"] == len(swaps)
    assert t.estimates["matches"]["double"] == len(doubles)
    # the masking is audited per match
    assert all(m.mask_count_a and m.mask_count_b for m in prim)

    # a rerun is a new tournament with a new snapshot and a new freeze; the old ratings stand
    before = sorted((r.member_id, r.rating, r.se) for r in ratings)
    tid2 = play(env)
    t2, m2, r2 = fetch(env.world, lambda db: everything(db, tid2))
    _, _, again = fetch(env.world, lambda db: everything(db, tid))
    assert t2.id != tid and t2.snapshot_id != t.snapshot_id and t2.freeze_id != t.freeze_id and t2.status == "completed"
    assert sorted((r.member_id, r.rating, r.se) for r in again) == before and len(r2) == 14


def test_jurors_never_see_other_modules_or_ratings(env):
    corpus(env.world)
    play(env)
    for c in env.juror.calls:
        assert c["prompt"].count("<<<TEXT") == 4  # exactly two quoted texts
        low = (c["system"] + c["prompt"]).lower()
        assert "engine 2" not in low and not re.search(r"\b(elo|rating|ranking)\b", low)


# ── budget, cancel, recovery, failure ───────────────────────────────────────────────────────
def test_budget_pauses_and_resume_continues(env):
    corpus(env.world)
    tid = play(env, budget=0.1)
    t, matches, ratings = fetch(env.world, lambda db: everything(db, tid))
    assert t.status == "paused" and t.pause_reason == "budget" and ratings == []
    assert Decimal(t.spent_usd) <= Decimal("0.1") and any(m.status == "pending" for m in matches) and t.summary == {}
    spent, calls = Decimal(t.spent_usd), len(env.juror.calls)

    async def go(**kw):
        async with env.world.factory() as db:
            await tn.resume_tournament(db, workspace_id=env.world.ws.id, tournament_id=tid, **kw)
        assert await tn.drain(120)

    asyncio.run(go())  # no more money: it runs nothing and pauses again
    t = fetch(env.world, lambda db: db.get(EvalTournament, tid))
    assert (t.status, t.pause_reason, Decimal(t.spent_usd), len(env.juror.calls)) == ("paused", "budget", spent, calls)
    asyncio.run(go(budget_usd=100))
    t, matches, ratings = fetch(env.world, lambda db: everything(db, tid))
    assert t.status == "completed" and len(ratings) == 14 and all(m.status == "done" for m in matches)
    assert len({(m.round, m.a_member_id, m.b_member_id, m.kind, m.juror_index) for m in matches}) == len(matches)


async def _resume_run(env, tid, **kw):
    async with env.world.factory() as db:
        await tn.resume_tournament(db, workspace_id=env.world.ws.id, tournament_id=tid, **kw)
    assert await tn.drain(120)


def expire_lease(env, tid):
    async def go(db):
        t = await db.get(EvalTournament, tid)
        t.lease_until = tn._now().replace(year=2000)
        await db.commit()

    fetch(env.world, go)


async def _resume(env, tid, **kw):
    async with env.world.factory() as db:
        return await tn.resume_tournament(db, workspace_id=env.world.ws.id, tournament_id=tid, spawn=False, **kw)


def test_cancel_stops_scheduling(env):
    corpus(env.world)

    async def cancel_after_three(n):
        if n == 3:
            async with env.world.factory() as db:
                await tn.cancel_tournament(db, workspace_id=env.world.ws.id, tournament_id=state["tid"])

    state = {}
    env.juror.on_call = cancel_after_three

    async def go():
        async with env.world.factory() as db:
            t = await create(env, db)
            await db.commit()
            state["tid"] = t.id
            await tn.start_tournament(db, workspace_id=env.world.ws.id, tournament_id=t.id, budget_usd=100, services=env.svc)
        assert await tn.drain(120)

    asyncio.run(go())
    t, matches, ratings = fetch(env.world, lambda db: everything(db, state["tid"]))
    assert t.status == "cancelled" and ratings == []
    assert len(env.juror.calls) <= 3 + 2  # the matches already in flight finish; nothing new starts
    assert any(m.status == "pending" for m in matches)
    with pytest.raises(tn.TournamentError):
        asyncio.run(_resume(env, state["tid"]))


def test_restart_recovery_pauses_running_tournaments(env):
    corpus(env.world)

    async def go():
        async with env.world.factory() as db:
            t = await create(env, db)
            await db.commit()
            await tn.start_tournament(db, workspace_id=env.world.ws.id, tournament_id=t.id, budget_usd=100, services=env.svc, spawn=False)
            return t.id

    tid = asyncio.run(go())
    assert fetch(env.world, lambda db: db.get(EvalTournament, tid)).status == "running"
    assert asyncio.run(tn.recover_running(env.world.factory)) == 0  # its runner's lease is live: left alone
    expire_lease(env, tid)
    assert asyncio.run(tn.recover_running(env.world.factory)) == 1
    t = fetch(env.world, lambda db: db.get(EvalTournament, tid))
    assert (t.status, t.pause_reason) == ("paused", "restart") and env.juror.calls == []

    async def resume():
        async with env.world.factory() as db:
            await tn.resume_tournament(db, workspace_id=env.world.ws.id, tournament_id=tid)
        assert await tn.drain(120)

    asyncio.run(resume())
    assert fetch(env.world, lambda db: db.get(EvalTournament, tid)).status == "completed"


def test_a_failing_jury_fails_the_tournament_and_failed_matches_do_not_count(env):
    corpus(env.world, 12, 10)  # a round of 11 matches: the watch over the last 10+ matches trips first
    env.juror.fail = http_error(400, "bad request")
    tid = play(env)
    t, matches, ratings = fetch(env.world, lambda db: everything(db, tid))
    # too many failed matches for model reasons PAUSE the tournament (it is not failed for good)
    assert (t.status, t.pause_reason) == ("paused", "juror_failures") and ratings == [] and t.lease_owner is None
    failed = [m for m in matches if m.status == "failed"]
    assert len(failed) >= tn.FAIL_MIN and all(m.verdict is None and m.error for m in failed)
    env.juror.fail = None  # the provider recovers: resume plays on
    asyncio.run(_resume_run(env, tid))
    t, matches, ratings = fetch(env.world, lambda db: everything(db, tid))
    assert (t.status, t.pause_reason) == ("completed", None) and len(ratings) == 22


def test_a_failing_juror_is_not_replaced_by_another_model(env):
    """A juror that fails does not make the run fall back to another model."""
    corpus(env.world)
    env.juror.fail = http_error(500, "down")
    tid = play(env)
    _, matches, _ = fetch(env.world, lambda db: everything(db, tid))
    wire = {c["model"] for c in env.juror.calls}
    assert wire <= {env.cat.get(m.juror_model).wire_id for m in matches}


# ── API ───────────────────────────────────────────────────────────────────────────────────────
def client(env, role="approver", state=None):
    state = state if state is not None else {"ws": env.world.ws.id}
    app = FastAPI()
    app.include_router(api.router)

    async def _db():
        async with env.world.factory() as s:
            yield s

    async def _user():
        return env.world.user

    async def _ws():
        return WorkspaceContext(Workspace(id=state["ws"], name="W", kind="team"), role)

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[current_user] = _user
    app.dependency_overrides[current_workspace] = _ws
    c = TestClient(app)
    c.state = state
    return c


def test_api_is_a_404_while_off(env, monkeypatch):
    monkeypatch.setattr(get_settings(), "eval_tournament", "off")
    tid = uuid.uuid4()
    with client(env) as c:
        assert c.post("/api/eval/tournaments", json={"module": MODULE}).status_code == 404
        assert c.get("/api/eval/tournaments").status_code == 404
        for path in ("", "/matches", "/ratings"):
            assert c.get(f"/api/eval/tournaments/{tid}{path}").status_code == 404
        for action in ("start", "cancel", "resume"):
            assert c.post(f"/api/eval/tournaments/{tid}/{action}", json={"budget_usd": 1}).status_code == 404


def test_api_requires_the_jury_setting(env, monkeypatch):
    corpus(env.world)
    monkeypatch.setattr(get_settings(), "eval_jury", "off")
    with client(env) as c:
        r = c.post("/api/eval/tournaments", json={"module": MODULE, "min_n": 4})
        assert r.status_code == 409 and "TRET_EVAL_JURY" in r.json()["detail"]


def test_api_lifecycle_roles_and_isolation(env, monkeypatch):
    corpus(env.world)
    spawned = []
    monkeypatch.setattr(tn, "_spawn", lambda tid, owner=None: spawned.append(tid))
    with client(env, role="analyst") as c:
        assert c.post("/api/eval/tournaments", json={"module": MODULE}).status_code == 403
    with client(env) as c:
        assert c.post("/api/eval/tournaments", json={"module": MODULE, "min_n": 4, "model": "a/b"}).status_code == 422
        assert c.post("/api/eval/tournaments", json={"module": MODULE, "min_n": 4, "model_policy": {"mode": "pinned", "model": "a/b"}}).status_code == 422
        r = c.post("/api/eval/tournaments", json={"module": MODULE, "min_n": 4})
        assert r.status_code == 200, r.text
        body = r.json()
        tid = body["id"]
        assert body["status"] == "draft" and body["estimates"]["matches"]["total"] > 0 and len(body["pools"]) == 2
        assert body["snapshot"]["corpus_filter"]["module"] == MODULE and env.juror.calls == [] and spawned == []
        assert c.get(f"/api/eval/tournaments/{tid}").json()["id"] == tid
        assert [t["id"] for t in c.get("/api/eval/tournaments").json()["tournaments"]] == [tid]
        assert c.post(f"/api/eval/tournaments/{tid}/start", json={}).status_code == 422  # a budget is required
        assert c.post(f"/api/eval/tournaments/{tid}/start", json={"budget_usd": 0}).status_code == 422
        assert c.post(f"/api/eval/tournaments/{tid}/resume", json={}).status_code == 409  # not paused
    with client(env, role="analyst") as c:
        assert c.post(f"/api/eval/tournaments/{tid}/start", json={"budget_usd": 5}).status_code == 403
        assert c.post(f"/api/eval/tournaments/{tid}/cancel").status_code == 403
        assert c.get(f"/api/eval/tournaments/{tid}").status_code == 200  # reading is open to members
    with client(env, state={"ws": uuid.uuid4()}) as c:  # another workspace
        assert c.get(f"/api/eval/tournaments/{tid}").status_code == 404
        assert c.get(f"/api/eval/tournaments/{tid}/matches").status_code == 404
        assert c.post(f"/api/eval/tournaments/{tid}/start", json={"budget_usd": 5}).status_code == 404
        assert c.get("/api/eval/tournaments").json()["tournaments"] == []
    with client(env) as c:
        r = c.post(f"/api/eval/tournaments/{tid}/start", json={"budget_usd": 5})
        assert r.status_code == 200 and r.json()["status"] == "running" and r.json()["budget_usd"] == 5.0
        assert r.json()["freeze_id"] and spawned == [uuid.UUID(tid)]
        assert c.post(f"/api/eval/tournaments/{tid}/start", json={"budget_usd": 5}).status_code == 409
        assert c.post(f"/api/eval/tournaments/{tid}/cancel").json()["status"] == "cancelled"
        assert c.delete(f"/api/eval/tournaments/{tid}").status_code == 409  # only a draft can be deleted
        assert c.post(f"/api/eval/tournaments/{tid}/refit").status_code == 409  # nor refitted: nothing was played


def test_api_delete_a_draft_and_its_snapshot_and_refit(env):
    corpus(env.world)
    with client(env) as c:
        tid = c.post("/api/eval/tournaments", json={"module": MODULE, "min_n": 4}).json()["id"]
        sid = c.get(f"/api/eval/tournaments/{tid}").json()["snapshot_id"]
    with client(env, role="analyst") as c:
        assert c.delete(f"/api/eval/tournaments/{tid}").status_code == 403
    with client(env, state={"ws": uuid.uuid4()}) as c:
        assert c.delete(f"/api/eval/tournaments/{tid}").status_code == 404
    with client(env) as c:
        assert c.delete(f"/api/eval/tournaments/{tid}").status_code == 200
        assert c.get(f"/api/eval/tournaments/{tid}").status_code == 404
        assert c.delete(f"/api/eval/tournaments/{tid}").status_code == 404
    from tret.db.models import PeerPoolMember, PeerPoolSnapshot

    assert fetch(env.world, lambda db: db.get(PeerPoolSnapshot, uuid.UUID(sid))) is None
    assert fetch(env.world, lambda db: db.execute(select(PeerPoolMember))).scalars().all() == []
    done = play(env)  # a played tournament is history and is refitted in place
    before = fetch(env.world, lambda db: everything(db, done))[2]
    fetch(env.world, lambda db: db.execute(ModuleRating.__table__.delete().where(ModuleRating.tournament_id == done)))
    with client(env, role="analyst") as c:
        assert c.post(f"/api/eval/tournaments/{done}/refit").status_code == 403
    with client(env) as c:
        assert c.delete(f"/api/eval/tournaments/{done}").status_code == 409
        r = c.post(f"/api/eval/tournaments/{done}/refit")
        assert r.status_code == 200 and r.json()["status"] == "completed" and r.json()["summary"]["fitted_at"]
    after = fetch(env.world, lambda db: everything(db, done))[2]
    assert sorted((r.member_id, round(r.rating, 6)) for r in after) == sorted((r.member_id, round(r.rating, 6)) for r in before)
    assert len(env.juror.calls) == len(fetch(env.world, lambda db: everything(db, done))[1])  # no spend on a refit


def test_api_matches_and_ratings_pages(env):
    ids = corpus(env.world)
    tid = play(env)
    with client(env) as c:
        r = c.get(f"/api/eval/tournaments/{tid}/matches", params={"limit": 10}).json()
        assert r["total"] > 10 and len(r["matches"]) == 10
        m = r["matches"][0]
        assert m["juror_model"] and m["rationale"] and m["a"].startswith("Zorblax") and m["status"] == "done"
        rest = c.get(f"/api/eval/tournaments/{tid}/matches", params={"limit": 500, "offset": 10}).json()
        assert len(rest["matches"]) == r["total"] - 10
        assert all(x["kind"] == "swap" for x in c.get(f"/api/eval/tournaments/{tid}/matches", params={"kind": "swap"}).json()["matches"])
        assert c.get(f"/api/eval/tournaments/{tid}/matches", params={"kind": "x"}).status_code == 422
        rt = c.get(f"/api/eval/tournaments/{tid}/ratings").json()
        assert [p["key"] for p in rt["pools"]] == ["division:10", "division:20"]
        first = rt["pools"][0]["ratings"]
        assert [x["rating"] for x in first] == sorted((x["rating"] for x in first), reverse=True)
        assert first[0]["report_module_id"] == str(ids.modules[7]) and first[0]["member_id"]  # the strongest of pool 1
        s = c.get(f"/api/eval/tournaments/{tid}").json()
        assert s["status"] == "completed" and s["summary"]["flip_rate"]["overall"]["pairs"] > 0
    text = cli_eval.render_status(s)
    assert "completed" in text and "Flip rate" in text


def test_cli_parser_wiring():
    import argparse

    p = argparse.ArgumentParser()
    cli_eval.register(p.add_subparsers(dest="command"))
    a = p.parse_args(["eval", "tournament", "start", str(uuid.uuid4()), "--budget", "5"])
    assert a.tournament_command == "start" and a.budget == 5.0
    with pytest.raises(SystemExit):
        p.parse_args(["eval", "tournament", "start", str(uuid.uuid4())])  # --budget is required
    c = p.parse_args(["eval", "tournament", "create", "--workspace", str(uuid.uuid4()), "--module", "m", "--batch", "x", "--batch", "y"])
    assert c.batches == ["x", "y"] and c.min_n == 15
