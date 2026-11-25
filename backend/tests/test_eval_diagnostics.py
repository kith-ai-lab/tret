"""Engine 1 diagnostics and pause gates (PR 7): kappa, length bias, flip drop, juror agreement, anchor
sets and judge drift, acknowledgement, and the API / CLI, with fake jurors (no network)."""
# ruff: noqa: F811 - imported fixtures are used by name as test arguments
from __future__ import annotations

import asyncio
import hashlib
import re
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from tests.eval_corpus import MODULE
from tests.test_batches_core import world  # noqa: F401 - fixture
from tests.test_eval_tournament import (  # noqa: F401 - fixtures and helpers
    COST,
    STRENGTH,
    corpus,
    create,
    env,
    everything,
    fetch,
    member_map,
    play,
)
from tests.test_route_jury import FIVE, Registry, catalog, no_router_call  # noqa: F401 - autouse fixture
from tret import cli_eval
from tret.config import get_settings
from tret.db.models import EvalAnchorSet, EvalMatch, EvalTournament, ModuleRating
from tret.evaluation import anchors as an
from tret.evaluation import diagnostics as dg
from tret.evaluation import tournament as tn

TEXTS = re.compile(r"<<<TEXT (\d) START>>>\n(.*?)\n<<<TEXT \1 END>>>", re.S)


# ── fake jurors with a behaviour per model family ─────────────────────────────────────────────────
def _texts(prompt: str) -> list[str]:
    return [t for _n, t in TEXTS.findall(prompt)]


def by_strength(prompt):
    s = [int(x) for x in STRENGTH.findall(prompt)]
    return "tie" if s[0] == s[1] else ("1" if s[0] > s[1] else "2")


def anti_strength(prompt):
    return {"1": "2", "2": "1", "tie": "tie"}[by_strength(prompt)]


def longer(prompt):
    a, b = (len(re.findall("measure", t)) for t in _texts(prompt))
    return "tie" if a == b else ("1" if a > b else "2")


def position(prompt):
    return "1"


def coin(prompt):
    return "1" if hashlib.sha256(prompt.encode()).digest()[0] % 2 else "2"


def install(env, default=by_strength, **families):
    """Make the fake provider answer per family: `gpt` (openai), `gem` (google), `llama` (meta)."""
    table = {"gpt-a": families.get("gpt", default), "gem-a": families.get("gem", default), "llama-a": families.get("llama", default)}

    async def complete_json(**kw):
        from tret.providers.base import JsonCompletion, Usage

        env.juror.calls.append(kw)
        fn = next(f for k, f in table.items() if k in kw["model"])
        return JsonCompletion(
            payload={"verdict": fn(kw["prompt"]), "strength": "clear", "rationale": "because"},
            usage=Usage(1000, 100, reported_cost_usd=COST), model=kw["model"], served_by="fake",
        )

    env.juror.complete_json = complete_json


@pytest.fixture
def no_agreement_gate(monkeypatch):
    """Tests about the flip gates: the agreement gate is out of the way (member ids are random uuids, so the
    orientation of the kappa pairs, and a positional juror's kappa on a handful of doubles, vary run to run)."""
    monkeypatch.setattr(get_settings(), "eval_gate_min_doubles", 1000)
    monkeypatch.setattr(get_settings(), "eval_gate_length_r_pause", 1.0)  # nor can a chance length correlation of 14 modules pause it
    monkeypatch.setattr(get_settings(), "eval_gate_length_r_warn", 1.0)


@pytest.fixture
def gates(monkeypatch):
    """Small samples so the gates can trip on the 14-module test corpus."""
    s = get_settings()
    for k, v in (("eval_gate_min_swaps", 2), ("eval_gate_min_swaps_pooled", 2), ("eval_gate_min_doubles", 2), ("eval_gate_min_rated", 5), ("eval_anchor_pairs", 6),
                 # member ids are random uuids, so on 14 modules a chance length correlation can reach 0.6: only the
                 # tests about length bias (fixture `length_gate`) let that gate fire
                 ("eval_gate_length_r_pause", 1.0), ("eval_gate_length_r_warn", 1.0)):
        monkeypatch.setattr(s, k, v)


@pytest.fixture
def length_gate(gates, monkeypatch):
    """The length-bias thresholds at their defaults, for the tests whose fake juror prefers the longer text."""
    monkeypatch.setattr(get_settings(), "eval_gate_length_r_pause", 0.6)
    monkeypatch.setattr(get_settings(), "eval_gate_length_r_warn", 0.4)


def client(env, role="approver", state=None):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from tret.api import eval_tournaments as api
    from tret.api.auth import current_user
    from tret.api.workspace import WorkspaceContext, current_workspace
    from tret.db.engine import get_db
    from tret.db.models import Workspace

    state = state if state is not None else {"ws": env.world.ws.id}
    app = FastAPI()
    app.include_router(api.router)
    app.include_router(api.anchors_router)

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
    return TestClient(app)


def state(env, tid):
    t, matches, ratings = fetch(env.world, lambda db: everything(db, tid))
    return t, matches, ratings


async def _resume(env, tid, **kw):
    async with env.world.factory() as db:
        t = await tn.resume_tournament(db, workspace_id=env.world.ws.id, tournament_id=tid, **kw)
    assert await tn.drain(120)
    return t


def resume(env, tid, **kw):
    return asyncio.run(_resume(env, tid, **kw))


def gate_map(t):
    return {g["key"]: g for g in (t.diagnostics or {}).get("gates", [])}


# ── pure pieces ──────────────────────────────────────────────────────────────────────────────────
def test_cohen_kappa_hand_computed():
    assert dg.cohen_kappa([("a", "a"), ("a", "a"), ("b", "b"), ("b", "b")]) == pytest.approx(1.0)
    assert dg.cohen_kappa([("a", "b"), ("b", "a"), ("a", "b"), ("b", "a")]) == pytest.approx(-1.0)
    # 50 items: 20 a/a, 5 a/b, 10 b/a, 15 b/b -> po .7, pe .5*.6 + .5*.4 = .5 -> kappa .4
    pairs = [("a", "a")] * 20 + [("a", "b")] * 5 + [("b", "a")] * 10 + [("b", "b")] * 15
    assert dg.cohen_kappa(pairs) == pytest.approx(0.4)
    # ties are a third category: po .75, pe .25 + 0 + .125 -> (0.75 - 0.375) / 0.625
    assert dg.cohen_kappa([("a", "a"), ("tie", "tie"), ("b", "tie"), ("a", "a")]) == pytest.approx(0.6)
    with pytest.raises(ValueError):
        dg.cohen_kappa([("a", "x")])


def test_cohen_kappa_undefined_cases():
    assert dg.cohen_kappa([]) is None
    assert dg.cohen_kappa([("a", "a"), ("a", "a")]) is None  # both raters only ever said a: expected agreement is 1
    assert dg.cohen_kappa([("tie", "tie")]) is None
    assert dg.cohen_kappa([("a", "b"), ("a", "b")]) == pytest.approx(0.0)  # each constant, but different: defined


def test_majority_and_selection():
    assert dg.majority([]) is None
    assert dg.majority(["a", "a", "b"]) == "a" and dg.majority(["a", "b", "tie"]) == "tie" and dg.majority(["a", "b"]) == "tie"
    assert dg.majority(["b"]) == "b"
    cands = [{"match_id": uuid.UUID(int=i), "verdict": "tie" if i % 5 == 0 else "a", "gap": float(i)} for i in range(1, 41)]
    picked = an.select_pairs(cands, 20)
    assert len(picked) == 20 and sum(1 for c in picked if c["verdict"] == "tie") == 3
    gaps = [c["gap"] for c in picked]
    assert gaps == sorted(gaps) and gaps[0] <= 5 and gaps[-1] >= 36  # spread over the whole range, not the middle
    assert an.select_pairs(cands, 20) == picked  # deterministic
    assert len(an.select_pairs(cands[:3], 20)) == 3  # fewer candidates than asked: all of them
    assert all(c["verdict"] != "tie" for c in an.select_pairs([c for c in cands if c["verdict"] != "tie"], 8))


# ── a clean tournament ───────────────────────────────────────────────────────────────────────────
def test_clean_tournament_diagnostics_shape_and_insufficient_data(env):
    corpus(env.world)
    tid = play(env)
    t, matches, ratings = state(env, tid)
    assert t.status == "completed" and t.dropped_families == [] and t.acknowledged_gates == {}

    async def go(db):
        return await dg.compute_diagnostics(db, tid)

    d = fetch(env.world, go)
    assert set(d) == {"tournament_id", "status", "computed_at", "thresholds", "gates", "length_bias", "flip_rates", "agreement", "anchors", "history", "notes"}
    assert set(d["thresholds"]) == {"length_r_warn", "length_r_pause", "flip_drop", "kappa_pause", "anchor_changes_pause", "min_swaps", "min_doubles", "min_rated", "min_swaps_pooled", "agreement_floor"}
    assert d["thresholds"]["length_r_pause"] == 0.6 and d["thresholds"]["min_rated"] == 30 and d["status"] == "completed"
    assert (d["thresholds"]["min_swaps"], d["thresholds"]["min_swaps_pooled"], d["thresholds"]["min_doubles"]) == (10, 8, 12)
    assert [g["key"] for g in d["gates"]] == ["length_bias", "flip_rate", "position_bias", "juror_agreement", "anchor_drift", "parts_vs_whole", "score_clumping"]
    for g in d["gates"]:
        assert set(g) == {"key", "state", "value", "n", "detail", "acknowledged_by", "acknowledged_at"}
    states = {g["key"]: g["state"] for g in d["gates"]}
    # a finished tournament shows a gate that never got its samples as a warning, not as silence
    assert states == {"length_bias": "warn", "flip_rate": "warn", "position_bias": "warn", "juror_agreement": "warn",
                      "anchor_drift": "warn", "parts_vs_whole": "not_applicable", "score_clumping": "not_applicable"}
    assert all(g["detail"].startswith("not evaluated: only ") for g in d["gates"] if g["state"] == "warn")
    assert d["notes"] == []
    ov = d["length_bias"]["overall"]
    assert set(ov) == {"n", "pearson", "spearman", "pearson_density", "spearman_density"} and ov["n"] == 14
    assert [p["pool_key"] for p in d["length_bias"]["pools"]] == ["division:10", "division:20"]
    assert set(d["length_bias"]["pools"][0]) == {"pool_key", "n", "pearson", "spearman", "pearson_density", "spearman_density"}
    assert d["anchors"] is None
    assert sum(f["swaps"] for f in d["flip_rates"]) > 0 and all(set(f) == {"family", "swaps", "flips", "rate", "dropped"} for f in d["flip_rates"])
    assert all(set(a) == {"family_a", "family_b", "n", "kappa", "raw_agreement"} for a in d["agreement"])
    assert [h["round"] for h in d["history"]] == [1, 2, 3, 4, 5]
    assert all(set(h) == {"round", "length_spearman", "max_flip", "min_kappa", "rated"} for h in d["history"])
    assert d["history"][-1]["rated"] == 14
    text = cli_eval.render_diagnostics(d)
    assert "length_bias" in text and "not evaluated" in text and "n/a" in text
    out = tn.tournament_out(t)
    assert out["dropped_families"] == [] and {g["key"] for g in out["gates"]} == set(dg.GATE_KEYS)


def test_a_draft_reports_insufficient_data(env):
    corpus(env.world)

    async def go():
        async with env.world.factory() as db:
            t = await create(env, db)
            await db.commit()
            return await dg.compute_diagnostics(db, t.id)

    d = asyncio.run(go())
    assert d["status"] == "draft" and d["anchors"] is None and d["history"] == []
    assert {g["state"] for g in d["gates"]} == {"insufficient_data", "not_applicable"}
    assert d["length_bias"]["overall"]["n"] == 0


# ── length bias ──────────────────────────────────────────────────────────────────────────────────
def test_length_bias_pauses_and_needs_an_acknowledgement(env, gates, length_gate):
    corpus(env.world)
    install(env, longer)
    tid = play(env)
    t, matches, ratings = state(env, tid)
    assert t.status == "paused" and t.pause_reason == "length_bias" and t.current_round < 5 and ratings == []
    g = gate_map(t)["length_bias"]
    assert g["state"] == "tripped" and g["value"] > 0.6 and g["acknowledged_by"] is None
    spent = Decimal(t.spent_usd)

    with pytest.raises(tn.TournamentError) as e:  # looked at nothing: 409 naming the gate
        resume(env, tid)
    assert e.value.status == 409 and "length_bias" in str(e.value)
    with pytest.raises(tn.TournamentError) as e:  # not a gate that blocks this resume
        resume(env, tid, acknowledge_gates=["nope"], note="x")
    assert e.value.status == 422
    with pytest.raises(tn.TournamentError) as e:
        resume(env, tid, acknowledge_gates=["anchor_drift"], note="x")
    assert e.value.status == 422 and "length_bias" in str(e.value)
    with pytest.raises(tn.TournamentError) as e:  # an acknowledgement needs a reason
        resume(env, tid, acknowledge_gates=["length_bias"], note="  ")
    assert e.value.status == 422 and "note" in str(e.value)
    assert tn.required_gates(fetch(env.world, lambda db: db.get(EvalTournament, tid))) == ["length_bias"]
    assert fetch(env.world, lambda db: db.get(EvalTournament, tid)).status == "paused"
    assert Decimal(fetch(env.world, lambda db: db.get(EvalTournament, tid)).spent_usd) == spent

    resume(env, tid, acknowledge_gates=["length_bias"], note="long modules are better here", acknowledged_by="boss@example.com")
    t, matches, ratings = state(env, tid)
    assert t.status == "completed" and len(ratings) == 14 and t.current_round == 5  # not paused again on that gate
    ack = t.acknowledged_gates["length_bias"]
    assert ack["by"] == "boss@example.com" and ack["note"] == "long modules are better here" and ack["at"]
    g = gate_map(t)["length_bias"]
    assert g["state"] == "tripped" and g["acknowledged_by"] == "boss@example.com" and g["acknowledged_at"]  # still shown tripped
    d = fetch(env.world, lambda db: dg.compute_diagnostics(db, tid))
    assert {x["key"]: x for x in d["gates"]}["length_bias"]["acknowledged_by"] == "boss@example.com"
    assert [h["round"] for h in d["history"]] == [1, 2, 3, 4, 5]
    assert d["length_bias"]["overall"]["spearman"] > 0.6 and d["length_bias"]["overall"]["spearman_density"] is not None


def test_a_mild_length_correlation_only_warns(env, gates, monkeypatch):
    corpus(env.world)
    install(env, longer)
    s = get_settings()
    monkeypatch.setattr(s, "eval_gate_length_r_pause", 1.0)
    monkeypatch.setattr(s, "eval_gate_length_r_warn", 0.2)
    tid = play(env)
    t, _m, ratings = state(env, tid)
    assert t.status == "completed" and len(ratings) == 14
    assert gate_map(t)["length_bias"]["state"] == "warn"


# ── flip rate ────────────────────────────────────────────────────────────────────────────────────
def test_a_position_biased_family_is_dropped_and_never_used_again(env, gates, no_agreement_gate, monkeypatch):
    monkeypatch.setattr(tn, "SWAP_RATE", 1.0)
    corpus(env.world)
    install(env, gpt=position)  # the openai juror always answers "1"
    tid = play(env)
    t, matches, ratings = state(env, tid)
    assert t.status == "completed" and len(ratings) == 14
    assert t.dropped_families == ["openai"]
    rounds = {m.round for m in matches}
    assert rounds == {1, 2, 3, 4, 5}
    assert any(m.juror_family == "openai" for m in matches if m.round == 1)
    assert not [m for m in matches if m.round >= 2 and m.juror_family == "openai"]  # primaries, swaps and doubles
    assert {m.juror_family for m in matches if m.round >= 2} == {"google", "meta"}
    d = fetch(env.world, lambda db: dg.compute_diagnostics(db, tid))
    flips = {f["family"]: f for f in d["flip_rates"]}
    assert flips["openai"]["dropped"] and flips["openai"]["rate"] == 1.0 and not flips["google"]["dropped"] and flips["google"]["rate"] == 0.0
    g = {x["key"]: x for x in d["gates"]}["flip_rate"]
    assert g["state"] == "tripped" and g["value"] == 1.0 and "openai" in g["detail"]
    assert tn.tournament_out(t)["dropped_families"] == ["openai"]


def test_fewer_than_two_families_left_pauses_the_jury_as_degraded(env, gates, no_agreement_gate, monkeypatch):
    monkeypatch.setattr(tn, "SWAP_RATE", 1.0)
    corpus(env.world)
    install(env, gpt=position, gem=position)
    tid = play(env)
    t, matches, ratings = state(env, tid)
    assert t.status == "paused" and t.pause_reason == "jury_degraded" and ratings == [] and set(t.dropped_families) == {"openai", "google"}
    assert {m.round for m in matches} == {1}
    with pytest.raises(tn.TournamentError) as e:
        resume(env, tid)
    assert e.value.status == 409 and "flip_rate" in str(e.value)
    assert tn.required_gates(t) == ["flip_rate"]
    resume(env, tid, acknowledge_gates=["flip_rate"], note="one honest juror is enough here")
    t, matches, ratings = state(env, tid)
    assert t.status == "completed" and len(ratings) == 14
    assert {m.juror_family for m in matches if m.round >= 2} == {"meta"}  # one juror left, and no doubles for want of a second family
    assert not [m for m in matches if m.round >= 2 and m.kind == "double"]
    assert any(n.startswith("doubles skipped") for n in t.diagnostics["notes"])  # recorded, not silent


# ── juror agreement ──────────────────────────────────────────────────────────────────────────────
def test_a_juror_that_answers_against_the_others_pauses_the_pass(env, gates, monkeypatch):
    monkeypatch.setattr(tn, "DOUBLE_RATE", 1.0)
    corpus(env.world)
    install(env, llama=anti_strength)
    tid = play(env)
    t, matches, ratings = state(env, tid)
    assert t.status == "paused" and t.pause_reason == "low_agreement" and ratings == []
    g = gate_map(t)["juror_agreement"]
    assert g["state"] == "tripped" and g["value"] < 0.3 and g["n"] >= 2  # pooled over every double
    d = fetch(env.world, lambda db: dg.compute_diagnostics(db, tid))
    assert any(a["kappa"] is not None and a["kappa"] < 0.3 and "meta" in (a["family_a"], a["family_b"]) for a in d["agreement"])
    with pytest.raises(tn.TournamentError) as e:
        resume(env, tid)
    assert e.value.status == 409 and "juror_agreement" in str(e.value)
    resume(env, tid, acknowledge_gates=["juror_agreement"], note="the module is contested")
    assert state(env, tid)[0].status == "completed"


# ── anchors ──────────────────────────────────────────────────────────────────────────────────────
def make_set(env, tid, budget=100):
    async def go():
        async with env.world.factory() as db:
            a = await an.create_anchor_set(db, workspace_id=env.world.ws.id, tournament_id=tid, budget_usd=budget, created_by="a@example.com", services=env.svc)
            aid = a.id
        assert await an.drain(120)
        return aid

    return asyncio.run(go())


def get_set(env, aid):
    return fetch(env.world, lambda db: db.get(EvalAnchorSet, aid))


def test_anchor_set_creation_baseline_and_resume(env, gates):
    corpus(env.world)
    tid = play(env)
    t0, m0, r0 = state(env, tid)
    calls = len(env.juror.calls)
    aid = make_set(env, tid)
    a = get_set(env, aid)
    assert a.status == "ready" and a.module == MODULE and a.source_tournament_id == tid and len(a.pairs) == 6
    assert len(env.juror.calls) == calls + 18 and Decimal(a.spent_usd) == COST * 18  # 6 pairs x 3 jurors, the set's own spend
    assert Decimal(fetch(env.world, lambda db: db.get(EvalTournament, tid)).spent_usd) == Decimal(t0.spent_usd)  # not the tournament's
    for p in a.pairs:
        assert len(p["votes"]) == 3 and p["baseline"] in ("a", "b", "tie")
        assert p["baseline"] == dg.majority([v["verdict"] for v in p["votes"]]) == p["source_verdict"]  # same jury logic as the matches
        assert set(p["a"]) == {"text_key", "text_sha256", "word_count", "names", "domains"} and p["a"]["text_sha256"]
    assert [p["index"] for p in a.pairs] == list(range(6))
    gaps = [p["rating_gap"] for p in a.pairs]
    allgaps = sorted(abs(x.rating - y.rating) for x, y in _primary_pairs(env, tid))
    assert gaps == sorted(gaps) and gaps[0] <= allgaps[len(allgaps) // 3] and gaps[-1] >= allgaps[2 * len(allgaps) // 3]
    assert len({p["source_match_id"] for p in a.pairs}) == 6
    from tret.db.models import RoutingFreeze

    assert fetch(env.world, lambda db: db.get(RoutingFreeze, a.freeze_id)).status == "released"  # the baseline pass is over

    # a baseline that runs out of money stays pending with what it judged, and resumes
    bid = make_set(env, tid, budget="0.07")
    b = get_set(env, bid)
    assert b.status == "baseline_pending" and b.pause_reason == "budget" and Decimal(b.spent_usd) <= Decimal("0.07")
    done = sum(len(p["votes"]) for p in b.pairs)
    assert 0 < done < 18 and all(p["baseline"] is None for p in b.pairs) and b.lease_owner is None

    async def resume_it(budget):
        async with env.world.factory() as db:
            await an.resume_anchor_set(db, workspace_id=env.world.ws.id, set_id=bid, budget_usd=budget)
        assert await an.drain(120)

    asyncio.run(resume_it(Decimal(b.spent_usd) + Decimal("0.001")))  # not enough for another pair: nothing runs
    assert sum(len(p["votes"]) for p in get_set(env, bid).pairs) == done
    asyncio.run(resume_it(100))
    b = get_set(env, bid)
    assert b.status == "ready" and all(len(p["votes"]) == 3 and p["baseline"] for p in b.pairs) and Decimal(b.spent_usd) == COST * 18
    with pytest.raises(tn.TournamentError) as e:  # a finished set cannot be resumed
        asyncio.run(resume_it(100))
    assert e.value.status == 409


def _primary_pairs(env, tid):
    t, matches, ratings = state(env, tid)
    by = {r.member_id: r for r in ratings}
    return [(by[m.a_member_id], by[m.b_member_id]) for m in matches if m.kind == "primary"]


def test_anchors_are_judged_first_excluded_from_ratings_and_counted_in_spend(env, gates):
    corpus(env.world)
    first = play(env)
    aid = make_set(env, first)
    t1, m1, r1 = state(env, first)
    second = play(env)
    t2, m2, r2 = state(env, second)
    assert t2.status == "completed" and t2.diagnostics["anchor_set_id"] == str(aid)
    anchor = [m for m in m2 if m.kind == "anchor"]
    assert len(anchor) == 18 and {m.round for m in anchor} == {0} and all(m.a_member_id is None and m.anchor_set_id == aid for m in anchor)
    assert {(m.anchor_index, m.juror_index) for m in anchor} == {(i, j) for i in range(6) for j in range(3)}
    first_primary_at = min(m.created_at for m in m2 if m.kind == "primary")
    assert max(m.judged_at for m in anchor) <= max(m.judged_at for m in m2 if m.kind == "primary") and first_primary_at
    # not paired, not rated: the same seed and jury give the same pairing and the same ratings as without anchors
    assert [m.kind for m in m2 if m.kind != "anchor"].count("primary") == [m.kind for m in m1].count("primary")
    for ms, rs in ((m1, r1), (m2, r2)):  # the ratings rest on primaries and doubles only
        assert sum(r.matches for r in rs) == 2 * sum(1 for m in ms if m.kind in ("primary", "double") and m.status == "done")
    # counted in spend: every done match, anchors included
    assert Decimal(t2.spent_usd) == COST * len(m2) and all(m.status == "done" for m in m2)
    assert t2.summary["matches"]["primary"] == t1.summary["matches"]["primary"]  # the summary does not count anchors
    d = fetch(env.world, lambda db: dg.compute_diagnostics(db, second))
    assert d["anchors"]["anchor_set_id"] == str(aid) and d["anchors"]["n"] == 6 and d["anchors"]["judged"] == 6 and d["anchors"]["changed"] == 0
    assert set(d["anchors"]["items"][0]) == {"index", "baseline", "current", "changed"} and d["anchors"]["items"][0]["changed"] is False
    g = {x["key"]: x for x in d["gates"]}["anchor_drift"]
    assert g["state"] == "ok" and g["value"] == 0.0 and g["n"] == 6
    # ratings and matches are still readable
    assert len(fetch(env.world, lambda db: tn.list_matches(db, t2, limit=500, kind="anchor")).get("matches")) == 18


def test_a_drifted_judge_pauses_before_the_first_round(env, gates):
    corpus(env.world)
    first = play(env)
    aid = make_set(env, first)
    install(env, anti_strength)  # the jury now prefers the weaker module: every anchor verdict flips
    second = play(env)
    t, matches, ratings = state(env, second)
    assert t.status == "paused" and t.pause_reason == "anchor_drift" and ratings == [] and t.current_round == 0
    assert {m.kind for m in matches} == {"anchor"} and len(matches) == 18
    g = gate_map(t)["anchor_drift"]
    assert g["state"] == "tripped" and g["value"] == 6.0 and g["n"] == 6
    assert t.diagnostics["anchor_set_id"] == str(aid) and t.diagnostics["anchors"]["changed"] == 6
    d = fetch(env.world, lambda db: dg.compute_diagnostics(db, second))
    assert all(i["changed"] and i["current"] != i["baseline"] for i in d["anchors"]["items"])
    with pytest.raises(tn.TournamentError) as e:
        resume(env, second)
    assert e.value.status == 409 and "anchor_drift" in str(e.value)
    calls = len(env.juror.calls)
    resume(env, second, acknowledge_gates=["anchor_drift"], note="the new jury is the better one")
    t, matches, ratings = state(env, second)
    assert t.status == "completed" and len(ratings) == 14
    assert len([m for m in matches if m.kind == "anchor"]) == 18  # not judged again
    assert len(env.juror.calls) > calls


def test_a_few_changed_anchors_do_not_pause(env, gates, monkeypatch):
    corpus(env.world)
    first = play(env)
    make_set(env, first)
    monkeypatch.setattr(get_settings(), "eval_gate_anchor_changes_pause", 6)  # 6 changes are not "more than 6"
    install(env, anti_strength)
    second = play(env)
    t = state(env, second)[0]
    assert t.status == "completed" and gate_map(t)["anchor_drift"]["state"] == "ok"


def test_a_tournament_without_an_anchor_set_records_none(env, gates):
    corpus(env.world)
    tid = play(env)
    t, matches, _r = state(env, tid)
    assert t.diagnostics["anchor_set_id"] is None and not [m for m in matches if m.kind == "anchor"]
    assert gate_map(t)["anchor_drift"]["state"] == "insufficient_data"


def test_an_anchor_set_needs_a_completed_tournament(env, gates):
    corpus(env.world)

    async def go():
        async with env.world.factory() as db:
            t = await create(env, db)
            await db.commit()
            await an.create_anchor_set(db, workspace_id=env.world.ws.id, tournament_id=t.id, budget_usd=5, services=env.svc)

    with pytest.raises(tn.TournamentError) as e:
        asyncio.run(go())
    assert e.value.status == 409


# ── API ──────────────────────────────────────────────────────────────────────────────────────────
def test_api_is_a_404_while_off(env, monkeypatch):
    monkeypatch.setattr(get_settings(), "eval_tournament", "off")
    tid = uuid.uuid4()
    with client(env) as c:
        assert c.get(f"/api/eval/tournaments/{tid}/diagnostics").status_code == 404
        assert c.post(f"/api/eval/tournaments/{tid}/anchor-set", json={"budget_usd": 1}).status_code == 404
        assert c.get("/api/eval/anchor-sets").status_code == 404
        assert c.get(f"/api/eval/anchor-sets/{tid}").status_code == 404
        assert c.post(f"/api/eval/anchor-sets/{tid}/resume", json={}).status_code == 404


def test_api_diagnostics_anchor_sets_roles_and_isolation(env, gates, length_gate, monkeypatch):
    corpus(env.world)
    install(env, longer)
    tid = str(play(env))
    with client(env, role="analyst") as c:  # members read
        r = c.get(f"/api/eval/tournaments/{tid}/diagnostics")
        assert r.status_code == 200 and {g["key"]: g["state"] for g in r.json()["gates"]}["length_bias"] == "tripped"
        assert c.post(f"/api/eval/tournaments/{tid}/anchor-set", json={"budget_usd": 1}).status_code == 403
        assert c.post(f"/api/eval/tournaments/{tid}/resume", json={"acknowledge_gates": ["length_bias"], "note": "x"}).status_code == 403
        assert c.get("/api/eval/anchor-sets").status_code == 200
    with client(env, state={"ws": uuid.uuid4()}) as c:
        assert c.get(f"/api/eval/tournaments/{tid}/diagnostics").status_code == 404
        assert c.post(f"/api/eval/tournaments/{tid}/anchor-set", json={"budget_usd": 1}).status_code == 404
        assert c.get("/api/eval/anchor-sets").json() == {"anchor_sets": []}
    with client(env) as c:
        body = c.get(f"/api/eval/tournaments/{tid}").json()
        assert body["status"] == "paused" and body["pause_reason"] == "length_bias"
        assert {"key": "length_bias", "state": "tripped"} in body["gates"] and body["dropped_families"] == [] and body["acknowledged_gates"] == {}
        assert body["required_acknowledgements"] == ["length_bias"]
        assert [j["index"] for j in body["jury"]] == [0, 1, 2] and all(j["family"] and j["model"] and j["dropped"] is False for j in body["jury"])
        assert c.get("/api/eval/tournaments").json()["tournaments"][0]["required_acknowledgements"] == ["length_bias"]
        assert len(c.get("/api/eval/tournaments").json()["tournaments"][0]["jury"]) == 3
        assert [g["key"] for g in c.get("/api/eval/tournaments").json()["tournaments"][0]["gates"]][0] == "length_bias"
        r = c.post(f"/api/eval/tournaments/{tid}/resume", json={})
        assert r.status_code == 409 and "length_bias" in r.json()["detail"]
        assert c.post(f"/api/eval/tournaments/{tid}/resume", json={"acknowledge_gates": ["bogus"]}).status_code == 422
        assert c.post(f"/api/eval/tournaments/{tid}/resume", json={"note": "x" * 600}).status_code == 422
        r = c.post(f"/api/eval/tournaments/{tid}/resume", json={"acknowledge_gates": ["length_bias"]})  # no note
        assert r.status_code == 422 and "note" in r.json()["detail"]
        assert c.post(f"/api/eval/tournaments/{tid}/resume", json={"acknowledge_gates": ["anchor_drift"], "note": "x"}).status_code == 422
        assert c.post(f"/api/eval/tournaments/{tid}/anchor-set", json={"budget_usd": 1}).status_code == 409  # not completed yet
        spawned = []
        monkeypatch.setattr(tn, "_spawn", lambda t, owner=None: spawned.append(t))
        r = c.post(f"/api/eval/tournaments/{tid}/resume", json={"acknowledge_gates": ["length_bias"], "note": "ok"})
        assert r.status_code == 200 and r.json()["status"] == "running" and spawned
        assert r.json()["acknowledged_gates"]["length_bias"]["note"] == "ok" and r.json()["acknowledged_gates"]["length_bias"]["by"]
        assert c.get(f"/api/eval/tournaments/{tid}/diagnostics").json()["gates"][0]["acknowledged_by"]


def test_api_anchor_sets_and_ratings_shape(env, gates, monkeypatch):
    corpus(env.world)
    tid = str(play(env))
    spawned = []
    monkeypatch.setattr(an, "_spawn", lambda sid, owner: spawned.append((sid, owner)))
    with client(env) as c:
        assert c.post(f"/api/eval/tournaments/{tid}/anchor-set", json={}).status_code == 422
        r = c.post(f"/api/eval/tournaments/{tid}/anchor-set", json={"budget_usd": 5})
        assert r.status_code == 200, r.text
        s = r.json()
        assert s["status"] == "baseline_pending" and s["n_pairs"] == 6 and len(s["pairs"]) == 6 and spawned and s["votes_expected"] == 18
        assert len(s["jurors"]) == 3 and s["budget_usd"] == 5.0 and s["source_tournament_id"] == tid
        assert c.post(f"/api/eval/anchor-sets/{s['id']}/resume", json={}).status_code == 409  # a runner holds it
        run = asyncio.run
        sid = uuid.UUID(s["id"])
        owner = get_set(env, sid).lease_owner
        run(an.run_baseline(sid, owner))
        got = c.get(f"/api/eval/anchor-sets/{s['id']}").json()
        assert got["status"] == "ready" and got["votes"] == 18 and all(p["baseline"] for p in got["pairs"])
        listed = c.get("/api/eval/anchor-sets").json()["anchor_sets"]
        assert [x["id"] for x in listed] == [s["id"]] and "pairs" not in listed[0]
        assert c.post(f"/api/eval/anchor-sets/{s['id']}/resume", json={}).status_code == 409  # already ready
        assert c.get(f"/api/eval/anchor-sets/{uuid.uuid4()}").status_code == 404
        rt = c.get(f"/api/eval/tournaments/{tid}/ratings").json()
        pool = rt["pools"][0]
        assert {"pool_key", "level", "n", "band_kind", "rows"} <= set(pool) and pool["pool_key"] == "division:10" and pool["level"] == "division"
        assert pool["n"] == 8 and pool["band_kind"] == "quintile" and len(pool["rows"]) == 8
        assert {"member_id", "company", "rating", "se", "lo90", "hi90", "matches", "wins", "band", "word_count", "claim_density"} <= set(pool["rows"][0])
    with client(env, state={"ws": uuid.uuid4()}) as c:
        assert c.get(f"/api/eval/anchor-sets/{s['id']}").status_code == 404
        assert c.post(f"/api/eval/anchor-sets/{s['id']}/resume", json={}).status_code == 404


def test_cli_parser_and_renderers(env, gates, length_gate):
    import argparse

    p = argparse.ArgumentParser()
    cli_eval.register(p.add_subparsers(dest="command"))
    tid = str(uuid.uuid4())
    a = p.parse_args(["eval", "tournament", "resume", tid, "--acknowledge", "length_bias,anchor_drift", "--note", "seen"])
    assert a.acknowledge == "length_bias,anchor_drift" and a.note == "seen"
    assert p.parse_args(["eval", "tournament", "diagnostics", tid]).tournament_command == "diagnostics"
    c = p.parse_args(["eval", "anchors", "create", tid, "--budget", "3"])
    assert c.anchors_command == "create" and c.budget == 3.0
    assert p.parse_args(["eval", "anchors", "list", "--workspace", tid]).anchors_command == "list"
    with pytest.raises(SystemExit):
        p.parse_args(["eval", "anchors", "create", tid])
    corpus(env.world)
    install(env, longer)
    done = play(env)
    d = fetch(env.world, lambda db: dg.compute_diagnostics(db, done))
    text = cli_eval.render_diagnostics(d)
    assert "TRIPPED" in text and "Flip rate by juror family" in text
    assert fetch(env.world, lambda db: db.execute(select(ModuleRating))).scalars().all() == []  # provisional fits are never stored
    assert fetch(env.world, lambda db: db.execute(select(EvalMatch))).scalars().first() is not None
