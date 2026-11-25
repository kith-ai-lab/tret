"""PR 9: divergence (Engine 1 parts vs Engine 2 whole) and the human spot checks. Fixtures only: no model, no network."""
# ruff: noqa: F811
from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from tests.test_batches_core import world  # noqa: F401 - fixture
from tret.api import eval_divergence as api
from tret.api.auth import current_user
from tret.api.workspace import WorkspaceContext, current_workspace
from tret.config import get_settings
from tret.db.engine import get_db
from tret.db.models import (
    CredibilityPass,
    CredibilityReview,
    DisclosureReport,
    DivergenceSet,
    EvalTournament,
    ModuleRating,
    PeerPoolMember,
    PeerPoolSnapshot,
    Project,
    SpotCheck,
    Workspace,
)
from tret.evaluation import divergence as dv
from tret.evaluation.ratings import pearson, spearman
from tret.storage import get_store, reset_store

ARMS = [{"key": "a", "label": "Arm A"}, {"key": "b", "label": "Arm B"}]
_A = ["Zor", "Quil", "Bren", "Tavo", "Mun", "Xel", "Par", "Kest"]
_B = ["vex", "lon", "nic", "rin", "dal", "ith", "dov", "rian", "vane"]
NAMES = [_A[i % 8] + _B[i // 8] for i in range(60)]
assert len(set(NAMES)) == 60


def run(coro):
    return asyncio.run(coro)


def fetch(world, fn):
    async def go():
        async with world.factory() as db:
            return await fn(db)

    return asyncio.run(go())


@pytest.fixture
def env(world, tmp_path, monkeypatch):
    s = get_settings()
    for k, v in (("storage_dir", str(tmp_path / "storage")), ("eval_divergence", True), ("eval_divergence_min_n", 3),
                 ("eval_divergence_reading_list", 3), ("eval_spot_check_triads", 4), ("eval_spot_check_critiques", 4)):
        monkeypatch.setattr(s, k, v)
    reset_store()
    yield SimpleNamespace(world=world)
    reset_store()


def spec(co, score, mods, arm="a", **kw):
    """One report: its Credibility Score (None: never scored) and its module ratings {module: (band, kind, pool, rating)}."""
    return {"co": co, "score": score, "mods": mods, "arm": arm, **kw}


def seed(env, specs, modules=("m1", "m2"), *, pass_status="completed", t_status="completed", arms=ARMS, project=None, other_ws=False):
    """A completed pass, a completed tournament per module and one report per spec. Returns ids."""
    world = env.world

    async def go():
        async with world.factory() as db:
            proj = project or (await db.execute(select(Project).where(Project.workspace_id == world.ws.id))).scalars().first()
            ws_id = world.ws.id
            if other_ws:
                ws_id = uuid.uuid4()
                db.add(Workspace(id=ws_id, name="Other", kind="team"))
                await db.flush()
            p = CredibilityPass(id=uuid.uuid4(), workspace_id=ws_id, project_id=proj.id, name="Pass 1", status=pass_status, arms=arms,
                                prompt_version="v1")
            db.add(p)
            tids, snaps = {}, {}
            for m in modules:
                sn = PeerPoolSnapshot(id=uuid.uuid4(), workspace_id=ws_id, project_id=proj.id, taxonomy_version="t", rule="r")
                db.add(sn)
                await db.flush()
                t = EvalTournament(id=uuid.uuid4(), workspace_id=ws_id, module=m, module_set="s", snapshot_id=sn.id, judge_prompt_version="j",
                                   status=t_status)
                db.add(t)
                tids[m], snaps[m] = t.id, sn.id
            await db.flush()
            rids, members = {}, {}
            for sp in specs:
                rep = DisclosureReport(id=uuid.uuid4(), project_id=proj.id, document_id=uuid.uuid4(), status="normalised", markdown_key="k",
                                       markdown_sha256="x", markdown_chars=1, normaliser_version="1")
                db.add(rep)
                await db.flush()
                rids[sp["co"]] = rep.id
                if sp["score"] is not None:
                    db.add(CredibilityReview(pass_id=p.id, disclosure_report_id=rep.id, arm_key=sp["arm"], status="done", credibility_score=sp["score"],
                                             pushbacks=[{"pushback": f"p{i}", "evidence": "e"} for i in range(3)], story_breaks=["b"],
                                             summary=f"summary of {sp['co']}", model="fake/model", input_mode="whole"))
                for m, (band, kind, pool, rating) in sp["mods"].items():
                    body = (sp.get("text") or f"# {m}\n{sp['co']} Holdings reports on {m}. The company {sp['co']} states targets. Emissions fell at {sp['co']}.\n").encode()
                    key = f"divtest/{uuid.uuid4().hex}.md"
                    await get_store().put(key, body)
                    mem = PeerPoolMember(id=uuid.uuid4(), snapshot_id=snaps[m], disclosure_report_id=rep.id, text_key=key,
                                         text_sha256=hashlib.sha256(body).hexdigest() if not sp.get("bad_hash") else "0" * 64, word_count=20,
                                         company=sp.get("company") or f"{sp['co']} Holdings", pool_key=pool, pool_level="division", regime="other",
                                         mask_names=sp.get("mask_names") or [], mask_domains=sp.get("mask_domains") or [])
                    db.add(mem)
                    await db.flush()
                    db.add(ModuleRating(tournament_id=tids[m], pool_key=pool, member_id=mem.id, rating=rating, se=10.0, lo90=rating - 16,
                                        hi90=rating + 16, band_kind=kind, band=band))
                    members[(sp["co"], m)] = mem.id
            await db.commit()
            return SimpleNamespace(pass_id=p.id, tid=tids, rid=rids, member=members, ws_id=ws_id, project_id=proj.id)

    return run(go())


# The single-module fixture worked through by hand in the tests: (parts, whole) for six reports.
SIX = [("Alpha", 9, 2), ("Bravo", 9, 8), ("Cobalt", 5, 5), ("Delta", 5, 5), ("Ember", 1, 1), ("Fjord", 1, 9)]


def six(env, **kw):
    specs = [spec(co, whole, {"m1": (band, "decile", "p1", 1000 + band * 10 + i)}) for i, (co, band, whole) in enumerate(SIX)]
    return seed(env, specs, modules=("m1",), **kw)


def join(env, ids, modules=("m1",), arm_key=None, ws=True):
    async def go():
        async with env.world.factory() as db:
            return await dv.build_join(db, pass_id=ids.pass_id, tournament_ids=[ids.tid[m] for m in modules], arm_key=arm_key,
                                       workspace_id=ids.ws_id if ws else None)

    return run(go())


def by_co(c, ids):
    inv = {str(v): k for k, v in ids.rid.items()}
    return {inv[r["disclosure_report_id"]]: r for r in c["rows"]}


def make_set(env, ids, modules=("m1",), arm_key=None, user=None):
    async def go():
        async with env.world.factory() as db:
            s = await dv.create_set(db, workspace=env.world.ws, user=user or env.world.user, name="S", pass_id=ids.pass_id,
                                    tournament_ids=[ids.tid[m] for m in modules], arm_key=arm_key)
            return s.id

    return run(go())


def pool_world(env, n=9, modules=("m1",), pools=("p1",)):
    """`n` fully scored reports per pool and module; ratings rise with the index."""
    specs = []
    for pi, pool in enumerate(pools):
        for i in range(n):
            co = NAMES[pi * n + i]
            specs.append(spec(co, 1 + (i * 7 + pi) % 10, {m: (1 + i % 10, "decile", pool, 1000 + 40 * i) for m in modules}))
    return seed(env, specs, modules=modules)


# ── the join ─────────────────────────────────────────────────────────────────────────────────────
def test_join_percentiles_ties_quadrants_deltas_and_reading_list(env):
    ids = six(env)
    c = join(env, ids)
    rows = by_co(c, ids)
    twelfth = lambda n: n / 12  # noqa: E731
    assert rows["Alpha"]["parts_pct"] == pytest.approx(twelfth(10)) and rows["Cobalt"]["parts_pct"] == pytest.approx(twelfth(6))
    assert rows["Ember"]["parts_pct"] == pytest.approx(twelfth(2))  # ties share the average rank
    assert rows["Alpha"]["whole_pct"] == pytest.approx(twelfth(3)) and rows["Cobalt"]["whole_pct"] == rows["Delta"]["whole_pct"] == pytest.approx(twelfth(6))
    assert rows["Fjord"]["delta"] == pytest.approx(twelfth(9)) and rows["Alpha"]["delta"] == pytest.approx(-twelfth(7))
    assert rows["Alpha"]["raw_delta"] == 2 - 9 and rows["Fjord"]["raw_delta"] == 9 - 1
    assert {k: v["quadrant"] for k, v in rows.items()} == {
        "Alpha": "parts_over_whole", "Bravo": "both_strong", "Cobalt": "both_strong", "Delta": "both_strong", "Ember": "both_weak",
        "Fjord": "whole_over_parts"}
    assert c["stats"]["quadrants"] == {"both_strong": 3, "parts_over_whole": 1, "whole_over_parts": 1, "both_weak": 1}
    xs, ys = [float(b) for _, b, _ in SIX], [float(w) for _, _, w in SIX]
    assert c["stats"]["pearson"] == pytest.approx(pearson(xs, ys)) and c["stats"]["spearman"] == pytest.approx(spearman(xs, ys))
    assert c["stats"]["n_joined"] == c["stats"]["n_complete"] == 6 and c["stats"]["n_partial"] == 0
    inv = {str(v): k for k, v in ids.rid.items()}
    assert [inv[i] for i in c["reading_list"]] == ["Fjord", "Alpha", "Bravo"]  # |delta| desc, then |raw delta|, then id
    assert c["rows"][0]["disclosure_report_id"] == c["reading_list"][0]
    assert rows["Alpha"]["company"] == "Alpha Holdings" and rows["Alpha"]["modules"]["m1"]["pool_key"] == "p1"
    json.dumps(c)  # frozen as JSON
    assert c["inputs"]["pass_name"] == "Pass 1" and [t["module"] for t in c["inputs"]["tournaments"]] == ["m1"]


def test_quintiles_partial_rows_and_the_side_counts(env):
    specs = [
        spec("Alpha", 9, {"m1": (9, "decile", "p1", 1500), "m2": (5, "quintile", "q1", 1500)}),   # parts (9 + 9.5) / 2
        spec("Bravo", 2, {"m1": (2, "decile", "p1", 1000), "m2": (1, "quintile", "q1", 1000)}),   # (2 + 1.5) / 2
        spec("Cobalt", 5, {"m1": (5, "decile", "p1", 1200), "m2": (3, "quintile", "q1", 1200)}),  # (5 + 5.5) / 2
        spec("Delta", 6, {"m1": (6, "decile", "p1", 1300)}),                                       # partial
        spec("Ember", 7, {}),                                                                      # whole only
        spec("Fjord", None, {"m1": (4, "decile", "p1", 1100), "m2": (2, "quintile", "q1", 1100)}),  # parts only
        spec("Gamma", 3, {"m1": (3, "decile", "p1", 1050), "m2": (2, "quintile", "q1", 1050)}, arm="b"),
    ]
    ids = seed(env, specs)
    c = join(env, ids, ("m1", "m2"))
    rows = by_co(c, ids)
    assert rows["Alpha"]["modules"]["m2"]["decile_eq"] == 9.5 and rows["Alpha"]["decile_eq"] == pytest.approx(9.25) == rows["Alpha"]["parts"]
    assert rows["Cobalt"]["parts"] == pytest.approx(5.25) and rows["Bravo"]["parts"] == pytest.approx(1.75)
    assert rows["Alpha"]["pool_keys"] == {"m1": "p1", "m2": "q1"} and rows["Alpha"]["modules"]["m2"]["band_kind"] == "quintile"
    st = c["stats"]
    assert (st["n_joined"], st["n_complete"], st["n_partial"], st["n_whole_only"], st["n_parts_only"]) == (5, 4, 1, 1, 1)
    d = rows["Delta"]
    assert not d["complete"] and d["delta"] is None and d["quadrant"] is None and d["parts_pct"] is None and d["raw_delta"] == 6 - 6
    assert d["disclosure_report_id"] not in c["reading_list"] and sum(st["quadrants"].values()) == 4
    complete = [r for r in c["rows"] if r["complete"]]
    assert st["pearson"] == pytest.approx(pearson([r["parts"] for r in complete], [r["whole"] for r in complete]))  # partial rows are out of r
    assert c["rows"][-1]["complete"] is False  # partial rows sort last
    assert st["mixed_arms"] is True and {a["arm_key"]: a["n"] for a in st["by_arm"]} == {"a": 3, "b": 1}
    assert st["by_arm"][1]["pearson"] is None  # one report is no correlation


def test_arm_filter(env):
    specs = [spec(co, whole, {"m1": (band, "decile", "p1", 1000 + band * 10 + i)}, arm="a" if i % 2 == 0 else "b") for i, (co, band, whole) in enumerate(SIX)]
    ids = seed(env, specs, modules=("m1",))
    c = join(env, ids, arm_key="a")
    assert {r["arm_key"] for r in c["rows"]} == {"a"} and c["stats"]["n_complete"] == 3
    assert c["stats"]["by_arm"] == [] and c["stats"]["mixed_arms"] is False
    assert c["stats"]["n_other_arm"] == 3 and c["stats"]["n_parts_only"] == 0
    assert join(env, ids)["stats"]["n_complete"] == 6


def test_validation(env):
    ids = six(env)

    def bad(status, **kw):
        args = {"pass_id": ids.pass_id, "tournament_ids": [ids.tid["m1"]], "arm_key": None, **kw}

        async def go():
            async with env.world.factory() as db:
                await dv.build_join(db, **args)

        with pytest.raises(dv.DivergenceError) as e:
            run(go())
        assert e.value.status == status, str(e.value)

    bad(422, arm_key="nope")
    bad(422, tournament_ids=[])
    bad(404, pass_id=uuid.uuid4())
    bad(404, tournament_ids=[uuid.uuid4()])
    bad(422, tournament_ids=[ids.tid["m1"], ids.tid["m1"]])
    # not completed
    d = seed(env, [spec("Alpha", 5, {"m1": (5, "decile", "p1", 1000)})], modules=("m1",), t_status="running")
    bad(409, pass_id=d.pass_id, tournament_ids=[d.tid["m1"]])
    d = seed(env, [spec("Alpha", 5, {"m1": (5, "decile", "p1", 1000)})], modules=("m1",), pass_status="running")
    bad(409, pass_id=d.pass_id, tournament_ids=[d.tid["m1"]])
    # another workspace's pass, and a tournament from another project
    o = seed(env, [spec("Alpha", 5, {"m1": (5, "decile", "p1", 1000)})], modules=("m1",), other_ws=True)
    with pytest.raises(dv.DivergenceError) as e:
        run(_with_ws(env, o))
    assert e.value.status == 404

    async def other_project():
        async with env.world.factory() as db:
            pr = Project(id=uuid.uuid4(), workspace_id=env.world.ws.id, name="P2")
            db.add(pr)
            await db.commit()
            return pr

    pr = run(other_project())
    p2 = seed(env, [spec("Beta", 5, {"m1": (5, "decile", "p1", 1000)})], modules=("m1",), project=pr)
    bad(404, tournament_ids=[p2.tid["m1"]])  # the pass is in project P, the tournament in P2
    # two tournaments for the same module
    two = seed(env, [spec("Alpha", 5, {"m1": (5, "decile", "p1", 1000)})], modules=("m1",))
    other = seed(env, [spec("Beta", 5, {"m1": (5, "decile", "p1", 1000)})], modules=("m1",))
    bad(422, pass_id=two.pass_id, tournament_ids=[two.tid["m1"], other.tid["m1"]])


async def _with_ws(env, o):
    async with env.world.factory() as db:
        await dv.build_join(db, pass_id=o.pass_id, tournament_ids=[o.tid["m1"]], arm_key=None, workspace_id=env.world.ws.id)


# ── the parts-vs-whole gate ──────────────────────────────────────────────────────────────────────
GATE_SETTINGS = SimpleNamespace(eval_divergence_min_n=20, eval_divergence_r_low=0.2, eval_divergence_r_high=0.8)


@pytest.mark.parametrize("n,r,state", [(19, 0.5, "insufficient_data"), (20, None, "insufficient_data"), (20, 0.19, "tripped"), (20, -0.4, "tripped"),
                                        (20, 0.81, "tripped"), (20, 0.3, "warn"), (20, 0.7, "warn"), (20, 0.4, "ok"), (20, 0.6, "ok"), (20, 0.2, "warn")])
def test_parts_vs_whole_gate_states(n, r, state):
    g = dv.parts_vs_whole_gate({"stats": {"n_complete": n, "pearson": r}}, GATE_SETTINGS)
    assert g["state"] == state and g["key"] == "parts_vs_whole" and g["n"] == n and g["value"] == r and g["detail"]
    if state == "tripped":
        assert "read the extremes before going on" in g["detail"] and "0.4" in g["detail"]


# ── sampling ─────────────────────────────────────────────────────────────────────────────────────
def checks_of(env, set_id):
    return fetch(env.world, lambda db: dv.load_checks(db, set_id))


def test_triads_take_one_member_from_each_third_and_are_deterministic(env):
    ids = pool_world(env, n=9)
    sid = make_set(env, ids)
    triads = [c for c in checks_of(env, sid) if c.kind == "triad"]
    assert len(triads) == 4
    ratings = [1000 + 40 * i for i in range(9)]
    for c in triads:
        it = c.item
        assert it["module"] == "m1" and it["pool_key"] == "p1" and sorted(m["letter"] for m in it["members"]) == ["A", "B", "C"]
        got = sorted(m["rating"] for m in it["members"])
        assert got[0] in ratings[:3] and got[1] in ratings[3:6] and got[2] in ratings[6:]
        best_first = sorted(it["members"], key=lambda m: -m["rating"])
        assert it["expected_order"] == [m["letter"] for m in best_first]
        assert it["members"][0]["disclosure_report_id"]
    assert len({frozenset(m["member_id"] for m in c.item["members"]) for c in triads}) == 4  # no triad twice
    assert all(c.purpose == "spot_check" and c.status == "pending" for c in checks_of(env, sid))
    # same set id, same sample
    s = fetch(env.world, lambda db: db.get(DivergenceSet, sid))

    async def again():
        async with env.world.factory() as db:
            return await dv.sample_spot_checks(db, s, json.loads(json.dumps(s.computed)), get_settings())

    twice = run(again())
    assert [c.item for c in twice] == [c.item for c in checks_of(env, sid)]


def test_modules_are_covered_round_robin_and_a_small_pool_is_a_shortfall(env, monkeypatch):
    monkeypatch.setattr(get_settings(), "eval_spot_check_triads", 6)
    ids = pool_world(env, n=6, modules=("m1", "m2"), pools=("p1", "p2"))
    sid = make_set(env, ids, ("m1", "m2"))
    triads = [c for c in checks_of(env, sid) if c.kind == "triad"]
    assert [c.item["module"] for c in triads] == ["m1", "m2"] * 3
    assert len({(c.item["module"], c.item["pool_key"]) for c in triads[:4]}) == 4  # pools not yet used come first
    # one pool of exactly 3 members can give one triad only: the rest is a recorded shortfall
    ids = pool_world(env, n=3)
    sid = make_set(env, ids)
    triads = [c for c in checks_of(env, sid) if c.kind == "triad"]
    s = fetch(env.world, lambda db: db.get(DivergenceSet, sid))
    assert len(triads) == 1 and s.computed["stats"]["spot_check_shortfall"]["triads"] == 5
    # no pool of 3 at all
    ids = seed(env, [spec(f"{NAMES[i]}", 3 + i, {"m1": (3 + i, "decile", f"p{i}", 1000 + i)}) for i in range(4)], modules=("m1",))
    sid = make_set(env, ids)
    s = fetch(env.world, lambda db: db.get(DivergenceSet, sid))
    assert [c for c in checks_of(env, sid) if c.kind == "triad"] == [] and s.computed["stats"]["spot_check_shortfall"]["triads"] == 6


def test_half_the_critiques_come_from_the_reading_list(env, monkeypatch):
    monkeypatch.setattr(get_settings(), "eval_divergence_reading_list", 5)
    monkeypatch.setattr(get_settings(), "eval_spot_check_critiques", 5)
    ids = pool_world(env, n=12)
    sid = make_set(env, ids)
    s = fetch(env.world, lambda db: db.get(DivergenceSet, sid))
    crit = [c for c in checks_of(env, sid) if c.kind == "critique"]
    reading = set(s.computed["reading_list"])
    assert len(crit) == 5 and len(reading) == 5
    assert sum(c.item["disclosure_report_id"] in reading for c in crit) == 3  # half, rounded up
    assert sum(c.item["from_reading_list"] for c in crit) == 3
    assert len({c.item["disclosure_report_id"] for c in crit}) == 5 and sorted(c.position for c in crit) == list(range(5))
    assert {"review_id", "company", "whole", "parts", "quadrant"} <= set(crit[0].item)


def test_too_few_complete_rows_fall_back_to_any_joined_row(env, monkeypatch):
    monkeypatch.setattr(get_settings(), "eval_spot_check_critiques", 4)
    specs = [spec("Alpha", 9, {"m1": (9, "decile", "p1", 1500), "m2": (9, "decile", "q1", 1500)}),
             spec("Bravo", 2, {"m1": (2, "decile", "p1", 1000), "m2": (2, "decile", "q1", 1000)}),
             spec("Cobalt", 5, {"m1": (5, "decile", "p1", 1200), "m2": (5, "decile", "q1", 1200)}),
             spec("Delta", 6, {"m1": (6, "decile", "p1", 1300)}), spec("Ember", 4, {"m1": (4, "decile", "p1", 1100)})]
    ids = seed(env, specs)
    sid = make_set(env, ids, ("m1", "m2"))
    crit = [c for c in checks_of(env, sid) if c.kind == "critique"]
    assert len(crit) == 4 and any(c.item["quadrant"] is None for c in crit)


# ── blindness ────────────────────────────────────────────────────────────────────────────────────
FORBIDDEN = ("rating", "band", "band_kind", "expected_order", "company", "disclosure_report_id", "member_id", "pool_key", "credibility", "whole", "parts")


def keys(x):
    if isinstance(x, dict):
        for k, v in x.items():
            yield k
            yield from keys(v)
    elif isinstance(x, list):
        for v in x:
            yield from keys(v)


def outs(env, sid):
    async def go():
        async with env.world.factory() as db:
            return await dv.spot_checks_out(db, await db.get(DivergenceSet, sid))

    return run(go())


def test_a_pending_triad_is_blind_and_masked_and_the_reveal_waits_for_the_answer(env):
    ids = pool_world(env, n=9)
    sid = make_set(env, ids)
    triad = next(o for o in outs(env, sid) if o["kind"] == "triad")
    blob = json.dumps(triad)
    assert set(keys(triad)) <= {"id", "kind", "position", "status", "module", "texts", "letter", "text", "answer", "agreement", "note", "decided_by",
                                "decided_at", "revealed"}
    assert not (set(keys(triad)) & set(FORBIDDEN)) and triad["revealed"] is None and triad["status"] == "pending"
    assert all(n not in blob for n in NAMES), "a company name reached a pending triad"
    assert [t["letter"] for t in triad["texts"]] == ["A", "B", "C"] and all("reports on m1" in t["text"] for t in triad["texts"])
    assert "the company" in triad["texts"][0]["text"]
    chk = next(c for c in checks_of(env, sid) if str(c.id) == triad["id"])
    done = answer(env, chk.id, {"order": chk.item["expected_order"]})
    # answered, but the set still has pending checks: nothing is revealed, not even the agreement
    assert done["status"] == "done" and done["revealed"] is None and done["agreement"] is None and done["stored_agreement"] == 1.0
    for c in checks_of(env, sid):
        if c.status == "pending":
            answer(env, c.id, {"order": c.item["expected_order"]} if c.kind == "triad" else {"verdict": "agree"})
    rev = next(o for o in outs(env, sid) if o["id"] == triad["id"])["revealed"]
    assert next(o for o in outs(env, sid) if o["id"] == triad["id"])["agreement"] == 1.0
    assert rev["expected_order"] == chk.item["expected_order"] and rev["agreement"] == 1.0
    assert {m["letter"] for m in rev["members"]} == {"A", "B", "C"} and all(m["company"].endswith("Holdings") and m["rating"] for m in rev["members"])


def test_a_triad_whose_text_is_missing_or_changed_says_so(env):
    specs = [spec(f"{NAMES[i]}", 3 + i, {"m1": (3 + i, "decile", "p1", 1000 + 50 * i)}, bad_hash=(i == 1)) for i in range(3)]
    ids = seed(env, specs, modules=("m1",))
    sid = make_set(env, ids)
    triad = next(o for o in outs(env, sid) if o["kind"] == "triad")
    bad = [t for t in triad["texts"] if t["text"] is None]
    assert len(bad) == 1 and bad[0]["error"] == "text unavailable"


def test_a_pending_critique_has_no_module_bands_until_it_is_answered(env):
    ids = pool_world(env, n=9)
    sid = make_set(env, ids)
    crit = next(o for o in outs(env, sid) if o["kind"] == "critique")
    assert crit["revealed"] is None and not (set(keys(crit)) & {"band", "band_kind", "rating", "parts", "quadrant", "modules", "pool_key"})
    assert crit["review"]["credibility_score"] and len(crit["review"]["pushbacks"]) == 3 and crit["review"]["model"] == "fake/model"
    assert crit["company"] and crit["document_id"] and crit["disclosure_report_id"] and "from_reading_list" not in crit
    done = answer(env, uuid.UUID(crit["id"]), {"verdict": "partly", "pushbacks_sharp": [True, False, True]})
    assert done["revealed"] is None and "from_reading_list" not in done and done["agreement"] is None
    for c in checks_of(env, sid):
        if c.status == "pending":
            answer(env, c.id, {"order": c.item["expected_order"]} if c.kind == "triad" else {"verdict": "agree"})
    full = next(o for o in outs(env, sid) if o["id"] == crit["id"])
    assert full["revealed"]["quadrant"] and full["revealed"]["modules"]["m1"]["band"] and full["revealed"]["parts"] and full["agreement"] == 0.5
    assert isinstance(full["revealed"]["from_reading_list"], bool) and "from_reading_list" not in full


# ── answers ──────────────────────────────────────────────────────────────────────────────────────
def answer(env, check_id, ans, note=None, user=None):
    async def go():
        async with env.world.factory() as db:
            c = await db.get(SpotCheck, check_id)
            c = await dv.answer_spot_check(db, c, user=user or env.world.user, answer=ans, note=note)
            return {**await dv.spot_check_out(db, c), "stored_agreement": c.agreement}

    return run(go())


def answer_fails(env, check_id, ans, status):
    with pytest.raises(dv.DivergenceError) as e:
        answer(env, check_id, ans)
    assert e.value.status == status, str(e.value)


def test_triad_answers_are_validated_and_scored_by_pairs(env):
    ids = pool_world(env, n=9)
    sid = make_set(env, ids)
    t = next(c for c in checks_of(env, sid) if c.kind == "triad")
    best, mid, worst = t.item["expected_order"]
    for bad in ({"order": ["A", "B"]}, {"order": ["A", "A", "B"]}, {"order": ["A", "B", "D"]}, {"order": "ABC"}, {"order": [1, 2, 3]}, {}, {"order": ["A", "B", "C"], "x": 1},
                {"verdict": "agree"}, "ABC"):
        answer_fails(env, t.id, bad, 422)
    assert answer(env, t.id, {"order": [best, mid, worst]})["stored_agreement"] == 1.0
    assert answer(env, t.id, {"order": [mid, best, worst]})["stored_agreement"] == pytest.approx(2 / 3)
    assert answer(env, t.id, {"order": [best, worst, mid]})["stored_agreement"] == pytest.approx(2 / 3)
    assert answer(env, t.id, {"order": [worst, best, mid]})["stored_agreement"] == pytest.approx(1 / 3)
    assert answer(env, t.id, {"order": [mid, worst, best]})["stored_agreement"] == pytest.approx(1 / 3)
    assert answer(env, t.id, {"order": [worst, mid, best]})["stored_agreement"] == 0.0


def test_critique_answers_skip_restamp_and_the_session_user(env):
    ids = pool_world(env, n=9)
    sid = make_set(env, ids)
    c = next(c for c in checks_of(env, sid) if c.kind == "critique")
    for bad in ({"verdict": "meh"}, {}, {"verdict": "agree", "pushbacks_sharp": [True]}, {"verdict": "agree", "pushbacks_sharp": [1, 0, 1]}, {"verdict": "agree", "x": 1},
                {"skip": False}, {"skip": True, "verdict": "agree"}):
        answer_fails(env, c.id, bad, 422)
    assert answer(env, c.id, {"verdict": "agree"})["stored_agreement"] == 1.0
    assert answer(env, c.id, {"verdict": "disagree"}, note="  nope ")["stored_agreement"] == 0.0
    row = fetch(env.world, lambda db: db.get(SpotCheck, c.id))
    assert row.answer == {"verdict": "disagree"} and row.note == "nope" and row.status == "done"
    assert row.decided_by == env.world.user.id and row.decided_at
    # decided_by is the session's user, whoever the answer text names
    other = SimpleNamespace(id=uuid.uuid4(), email="o@example.com")
    out = answer(env, c.id, {"verdict": "partly"}, user=other)
    assert out["decided_by"] == str(other.id) and out["stored_agreement"] == 0.5
    skipped = answer(env, c.id, {"skip": True})
    assert skipped["status"] == "skipped" and skipped["stored_agreement"] is None and skipped["answer"] == {"skip": True} and skipped["revealed"] is None


def test_a_reviewed_set_takes_no_more_answers(env):
    ids = pool_world(env, n=9)
    sid = make_set(env, ids)
    fetch(env.world, lambda db: _set_status(db, sid, "reviewed"))
    c = checks_of(env, sid)[0]
    answer_fails(env, c.id, {"skip": True}, 409)


async def _set_status(db, sid, status):
    s = await db.get(DivergenceSet, sid)
    s.status = status
    await db.commit()


# ── the agreement gate ───────────────────────────────────────────────────────────────────────────
def fake(kind, status="done", agreement=None):
    return SimpleNamespace(kind=kind, status=status, agreement=agreement)


SPOT = SimpleNamespace(eval_spot_check_agreement_min=0.70)


def test_spot_check_gate_states():
    g = dv.spot_check_gate([fake("triad", "pending"), fake("triad", "done", 1.0), fake("critique", "done", 1.0)], SPOT)
    assert g["state"] == "insufficient_data" and g["detail"] == "2 of 3 answered." and g["key"] == "spot_check_agreement"
    triads = [fake("triad", "done", a) for a in (1.0, 1.0, 2 / 3, 1.0)]
    crit = [fake("critique", "done", a) for a in (1.0, 0.5)]
    ok = dv.spot_check_gate(triads + crit, SPOT)
    assert ok["state"] == "ok" and ok["triad_agreement"] == pytest.approx(11 / 12) and ok["critique_agreement"] == 0.75
    assert ok["value"] == 0.75 and ok["n_triads"] == 4 and ok["n_critiques"] == 2 and ok["n_skipped"] == 0 and ok["n"] == 6
    low = dv.spot_check_gate([fake("triad", "done", 1 / 3)] * 3 + [fake("critique", "done", 1.0)], SPOT)
    assert low["state"] == "tripped" and "Agreement under 70%: no client use" in low["detail"] and low["value"] == pytest.approx(1 / 3)
    low = dv.spot_check_gate([fake("triad", "done", 1.0)] * 3 + [fake("critique", "done", 0.5), fake("critique", "done", 0.5)], SPOT)
    assert low["state"] == "tripped"
    edge = dv.spot_check_gate([fake("triad", "done", 0.7)] * 2 + [fake("critique", "done", 1.0)], SPOT)
    assert edge["state"] == "ok"
    heavy = dv.spot_check_gate([fake("triad", "skipped")] * 3 + [fake("triad", "done", 1.0)] * 4 + [fake("critique", "done", 1.0)], SPOT)
    assert heavy["state"] == "warn" and "skipped" in heavy["detail"] and heavy["n_skipped"] == 3
    closed = dv.spot_check_gate([fake("triad", "skipped"), fake("critique", "skipped")], SPOT)
    assert closed["state"] == "tripped" and "No triad was answered; agreement can't be shown" in closed["detail"] and "No critique was answered" in closed["detail"]
    one = dv.spot_check_gate([fake("triad", "skipped")] * 2 + [fake("critique", "done", 1.0)], SPOT)
    assert one["state"] == "tripped" and "No triad was answered" in one["detail"] and "critique" not in one["detail"]
    assert dv.spot_check_gate([], SPOT)["state"] == "insufficient_data"


# ── lifecycle ────────────────────────────────────────────────────────────────────────────────────
def answer_all(env, sid, agree=True):
    for c in checks_of(env, sid):
        ans = ({"order": c.item["expected_order"] if agree else list(reversed(c.item["expected_order"]))} if c.kind == "triad"
               else {"verdict": "agree" if agree else "disagree"})
        answer(env, c.id, ans)


def lifecycle(env, fn):
    async def go():
        async with env.world.factory() as db:
            return await fn(db)

    return run(go())


def review(env, sid, note=None):
    async def fn(db):
        return await dv.mark_reviewed(db, await db.get(DivergenceSet, sid), env.world.user, note)

    return lifecycle(env, fn)


def expect(status, fn):
    with pytest.raises(dv.DivergenceError) as e:
        fn()
    assert e.value.status == status, str(e.value)
    return str(e.value)


def acknowledge(env, sid, gate, note="seen it"):
    async def fn(db):
        return await dv.acknowledge_gate(db, await db.get(DivergenceSet, sid), env.world.user, gate, note)

    return lifecycle(env, fn)


def test_review_needs_every_check_answered_and_every_tripped_gate_acknowledged(env, monkeypatch):
    monkeypatch.setattr(get_settings(), "eval_divergence_min_n", 3)
    ids = pool_world(env, n=9)
    sid = make_set(env, ids)
    assert "pending" in expect(409, lambda: review(env, sid))
    answer_all(env, sid, agree=False)  # Engine 1's order reversed: agreement 0, so the gate trips
    s = fetch(env.world, lambda db: db.get(DivergenceSet, sid))
    gates = {g["key"]: g for g in dv.gates_for(s, checks_of(env, sid))}
    assert gates["spot_check_agreement"]["state"] == "tripped"
    assert "spot_check_agreement" in expect(409, lambda: review(env, sid))
    # the note is required, the gate has to be tripped
    expect(422, lambda: acknowledge(env, sid, "spot_check_agreement", note="  "))
    expect(422, lambda: acknowledge(env, sid, "nonsense"))
    if gates["parts_vs_whole"]["state"] != "tripped":
        expect(409, lambda: acknowledge(env, sid, "parts_vs_whole"))
    s = acknowledge(env, sid, "spot_check_agreement", note="we know")
    assert s.acknowledged_gates[0]["gate"] == "spot_check_agreement" and s.acknowledged_gates[0]["by"] == env.world.user.email
    left = [k for k, g in gates.items() if g["state"] == "tripped" and k != "spot_check_agreement"]
    for k in left:
        assert k in expect(409, lambda: review(env, sid))
        acknowledge(env, sid, k)
    done = review(env, sid, note="read the extremes")
    assert done.status == "reviewed" and done.reviewed_by == env.world.user.id and done.reviewed_at
    expect(409, lambda: review(env, sid))
    expect(409, lambda: acknowledge(env, sid, "spot_check_agreement"))
    g = {x["key"]: x for x in dv.gates_for(done, checks_of(env, sid))}["spot_check_agreement"]
    assert g["state"] == "tripped" and g["acknowledged_by"] == env.world.user.email  # recorded, "no client use" stays


def test_a_set_with_good_agreement_reviews_straight_away_and_only_an_open_set_deletes(env, monkeypatch):
    monkeypatch.setattr(get_settings(), "eval_divergence_min_n", 1000)  # r is never judged here
    ids = pool_world(env, n=9)
    sid = make_set(env, ids)
    answer_all(env, sid)
    assert review(env, sid).status == "reviewed"

    async def delete(sid_):
        async with env.world.factory() as db:
            await dv.delete_set(db, await db.get(DivergenceSet, sid_))

    expect(409, lambda: run(delete(sid)))
    open_id = make_set(env, ids)
    run(delete(open_id))
    assert fetch(env.world, lambda db: db.get(DivergenceSet, open_id)) is None and checks_of(env, open_id) == []


def test_create_refuses_an_empty_join(env):
    ids = seed(env, [spec("Alpha", 5, {}), spec("Bravo", None, {"m1": (4, "decile", "p1", 1000)})], modules=("m1",))
    expect(422, lambda: make_set(env, ids))


def test_deleting_the_pass_cascades_to_the_sets_and_their_checks(env):
    ids = pool_world(env, n=9)
    sid = make_set(env, ids)

    async def go():
        async with env.world.factory() as db:
            await db.execute(text("PRAGMA foreign_keys=ON"))  # sqlite enforces the ON DELETE CASCADE only when asked
            await db.execute(CredibilityPass.__table__.delete().where(CredibilityPass.id == ids.pass_id))
            await db.commit()

    run(go())
    assert fetch(env.world, lambda db: db.get(DivergenceSet, sid)) is None and checks_of(env, sid) == []


# ── the API ──────────────────────────────────────────────────────────────────────────────────────
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


def body(ids, modules=("m1",), **kw):
    return {"pass_id": str(ids.pass_id), "tournament_ids": [str(ids.tid[m]) for m in modules], **kw}


def test_api_is_a_404_while_off(env, monkeypatch):
    monkeypatch.setattr(get_settings(), "eval_divergence", False)
    c = client(env)
    assert c.get("/api/eval/divergence").status_code == 404
    assert c.get("/api/eval/divergence/inputs").status_code == 404
    assert c.post("/api/eval/divergence/preview", json={}).status_code == 404
    assert c.get(f"/api/eval/divergence/{uuid.uuid4()}/rows").status_code == 404


def test_api_lifecycle_shapes_roles_and_workspace_scoping(env):
    ids = pool_world(env, n=9)
    c = client(env)
    inp = c.get("/api/eval/divergence/inputs").json()
    assert [p["id"] for p in inp["passes"]] == [str(ids.pass_id)] and inp["passes"][0]["n_done"] == 9 and inp["passes"][0]["arms"][0] == {"key": "a", "label": "Arm A"}
    assert inp["tournaments"][0]["id"] == str(ids.tid["m1"]) and inp["tournaments"][0]["n_rated"] == 9 and inp["tournaments"][0]["module"] == "m1"
    # preview writes nothing
    before = len(fetch(env.world, lambda db: db.execute(select(DivergenceSet))).scalars().all())
    pv = c.post("/api/eval/divergence/preview", json=body(ids))
    assert pv.status_code == 200, pv.text
    assert set(pv.json()) == {"stats", "gate", "n_reading_list", "warnings"} and pv.json()["n_reading_list"] == 3 and pv.json()["gate"]["key"] == "parts_vs_whole"
    assert len(fetch(env.world, lambda db: db.execute(select(DivergenceSet))).scalars().all()) == before
    assert fetch(env.world, lambda db: db.execute(select(SpotCheck))).scalars().all() == []
    # create
    r = c.post("/api/eval/divergence", json=body(ids, name="First"))
    assert r.status_code == 200, r.text
    s = r.json()
    for k in ("id", "name", "status", "project_id", "credibility_pass_id", "pass_name", "modules", "arm_key", "created_at", "reviewed_at", "n_complete", "pearson",
              "quadrant_counts", "gates", "spot_checks", "stats", "inputs", "acknowledged_gates", "reviewed_by", "scatter"):
        assert k in s, k
    assert s["name"] == "First" and s["status"] == "open" and s["pass_name"] == "Pass 1" and s["modules"] == ["m1"] and s["n_complete"] == 9
    assert [g["key"] for g in s["gates"]] == ["parts_vs_whole", "spot_check_agreement"] and s["spot_checks"] == {"total": 8, "answered": 0, "locked": False}
    assert s["scatter"] == [] and s["stale"] is False
    sid = s["id"]
    assert [x["id"] for x in c.get("/api/eval/divergence").json()] == [sid] and "scatter" not in c.get("/api/eval/divergence").json()[0]
    assert c.get(f"/api/eval/divergence/{sid}").json()["id"] == sid
    assert c.get(f"/api/eval/divergence/{sid}/rows?limit=4&offset=2").status_code == 409
    assert c.get(f"/api/eval/divergence/{sid}/reading-list").status_code == 409
    sc = c.get(f"/api/eval/divergence/{sid}/spot-checks").json()
    assert len(sc) == 8 and [x["kind"] for x in sc] == ["triad"] * 4 + ["critique"] * 4
    # answer through the API
    triad = next(x for x in sc if x["kind"] == "triad")
    a = c.post(f"/api/eval/divergence/spot-checks/{triad['id']}/answer", json={"answer": {"order": ["A", "B", "C"]}, "note": "ok"})
    assert a.status_code == 200 and a.json()["status"] == "done" and a.json()["revealed"] is None and a.json()["note"] == "ok"
    assert c.post(f"/api/eval/divergence/spot-checks/{triad['id']}/answer", json={"answer": {"order": ["A", "A", "C"]}}).status_code == 422
    assert c.post(f"/api/eval/divergence/{sid}/review", json={}).status_code == 409
    assert c.post(f"/api/eval/divergence/{sid}/acknowledge", json={"gate": "spot_check_agreement", "note": "x"}).status_code == 409
    # roles
    v = client(env, role="analyst")
    assert v.get("/api/eval/divergence").status_code == 200 and v.get(f"/api/eval/divergence/{sid}/spot-checks").status_code == 200
    assert v.post("/api/eval/divergence", json=body(ids)).status_code == 403
    assert v.post("/api/eval/divergence/preview", json=body(ids)).status_code == 403
    assert v.post(f"/api/eval/divergence/spot-checks/{triad['id']}/answer", json={"answer": {"skip": True}}).status_code == 403
    assert v.post(f"/api/eval/divergence/{sid}/review", json={}).status_code == 403
    assert v.post(f"/api/eval/divergence/{sid}/acknowledge", json={"gate": "x", "note": "x"}).status_code == 403
    assert v.delete(f"/api/eval/divergence/{sid}").status_code == 403
    # another workspace sees nothing
    o = client(env, state={"ws": uuid.uuid4()})
    assert o.get("/api/eval/divergence").json() == [] and o.get("/api/eval/divergence/inputs").json() == {"passes": [], "tournaments": []}
    for path in (f"{sid}", f"{sid}/rows", f"{sid}/reading-list", f"{sid}/spot-checks"):
        assert o.get(f"/api/eval/divergence/{path}").status_code == 404, path
    assert o.post(f"/api/eval/divergence/spot-checks/{triad['id']}/answer", json={"answer": {"skip": True}}).status_code == 404
    assert o.delete(f"/api/eval/divergence/{sid}").status_code == 404
    assert o.post("/api/eval/divergence", json=body(ids)).status_code == 404  # the pass is not theirs
    assert c.post("/api/eval/divergence", json=body(ids, arm_key="zzz")).status_code == 422
    assert c.delete(f"/api/eval/divergence/{sid}").status_code == 409  # an answer was given: the set is the record
    again = c.post("/api/eval/divergence", json=body(ids)).json()  # same pass: no new spot checks, nothing answered, so it can go
    assert again["spot_checks"] == {"total": 0, "answered": 0, "locked": True}
    assert c.delete(f"/api/eval/divergence/{again['id']}").status_code == 204
    assert c.get(f"/api/eval/divergence/{again['id']}").status_code == 404


def test_api_review_and_gates_end_to_end(env, monkeypatch):
    monkeypatch.setattr(get_settings(), "eval_divergence_min_n", 1000)
    ids = pool_world(env, n=9)
    c = client(env)
    sid = c.post("/api/eval/divergence", json=body(ids)).json()["id"]
    expected = {str(k.id): k.item.get("expected_order") for k in checks_of(env, uuid.UUID(sid))}
    for x in c.get(f"/api/eval/divergence/{sid}/spot-checks").json():
        ans = {"order": expected[x["id"]]} if x["kind"] == "triad" else {"verdict": "agree"}
        assert c.post(f"/api/eval/divergence/spot-checks/{x['id']}/answer", json={"answer": ans}).status_code == 200
    done = c.post(f"/api/eval/divergence/{sid}/review", json={"note": "fine"})
    assert done.status_code == 200 and done.json()["status"] == "reviewed" and done.json()["reviewed_by"] == str(env.world.user.id)
    assert c.delete(f"/api/eval/divergence/{sid}").status_code == 409


def test_cli_parser_wiring_and_a_read_only_command(env, monkeypatch, capsys):
    import argparse

    from tret import cli_eval

    p = argparse.ArgumentParser()
    cli_eval.register(p.add_subparsers(dest="command"))
    a = p.parse_args(["eval", "divergence", "create", "--workspace", "w", "--pass", "p", "--tournament", "t1", "--tournament", "t2", "--arm", "a", "--name", "N"])
    assert a.divergence_command == "create" and a.tournaments == ["t1", "t2"] and a.arm == "a" and a.name == "N"
    with pytest.raises(SystemExit):
        p.parse_args(["eval", "divergence", "preview", "--workspace", "w"])  # --pass and --tournament are required
    ids = pool_world(env, n=9)
    sid = make_set(env, ids)
    monkeypatch.setattr("tret.db.engine.get_session_factory", lambda: env.world.factory)
    assert cli_eval.run(p.parse_args(["eval", "divergence", "show", str(sid)])) == 0
    out = capsys.readouterr().out
    assert "Divergence set" in out and "parts_vs_whole" in out and "0 of 8 answered" in out
    assert cli_eval.run(p.parse_args(["eval", "divergence", "reading-list", str(sid)])) == 0
    assert cli_eval.run(p.parse_args(["eval", "divergence", "spot-checks", str(sid), "--json"])) == 0
    capsys.readouterr()
    assert cli_eval.run(p.parse_args(["eval", "divergence", "spot-checks", str(sid), "--json"])) == 0
    assert len(json.loads(capsys.readouterr().out)) == 8


# ── review fixes: reveal only when everything is answered, the lock on rows, the gate, races, staleness ──
def test_nothing_is_revealed_until_every_check_is_answered_then_answers_lock(env):
    ids = pool_world(env, n=9)
    sid = make_set(env, ids)
    cs = checks_of(env, sid)
    first, rest = cs[0], cs[1:]
    answer(env, first.id, {"skip": True})
    for c in rest[:-1]:
        answer(env, c.id, {"order": c.item["expected_order"]} if c.kind == "triad" else {"verdict": "agree"})
    pending = [o for o in outs(env, sid) if o["status"] == "pending"]
    assert len(pending) == 1 and all(o["revealed"] is None and o["agreement"] is None for o in outs(env, sid))  # skipped ones too
    answer(env, first.id, {"order": first.item["expected_order"]})  # a skipped check can still be answered ...
    answer(env, rest[0].id, {"skip": True})  # ... and any answer changed, while something is pending
    last = rest[-1]
    answer(env, last.id, {"order": last.item["expected_order"]} if last.kind == "triad" else {"verdict": "agree"})
    out = outs(env, sid)
    assert all(o["revealed"] is not None for o in out)
    assert next(o for o in out if o["id"] == str(rest[0].id))["status"] == "skipped"
    with pytest.raises(dv.DivergenceError) as e:
        answer(env, first.id, {"skip": True})
    assert e.value.status == 409 and "locked once revealed" in str(e.value)


def test_rows_reading_list_and_scatter_wait_for_the_spot_checks(env):
    ids = pool_world(env, n=9)
    c = client(env)
    s = c.post("/api/eval/divergence", json=body(ids)).json()
    sid = s["id"]
    assert s["scatter"] == [] and s["spot_checks"]["locked"] is False and s["stats"]["n_complete"] == 9 and s["pearson"] is not None
    for path in ("rows", "reading-list"):
        r = c.get(f"/api/eval/divergence/{sid}/{path}")
        assert r.status_code == 409 and r.json()["detail"] == "Finish the spot checks first: the reading list and rows show module bands"
    gate = next(g for g in s["gates"] if g["key"] == "spot_check_agreement")
    assert gate["value"] is None and gate["triad_agreement"] is None  # no running agreement while pending
    for k in checks_of(env, uuid.UUID(sid)):
        assert c.post(f"/api/eval/divergence/spot-checks/{k.id}/answer", json={"answer": {"skip": True}}).status_code == 200
    s = c.get(f"/api/eval/divergence/{sid}").json()
    assert s["spot_checks"]["locked"] is True and len(s["scatter"]) == 9
    page = c.get(f"/api/eval/divergence/{sid}/rows?limit=4&offset=2").json()
    assert page["total"] == 9 and len(page["rows"]) == 4 and page["rows"][0]["document_id"] and page["rows"][0]["company"]
    assert c.get(f"/api/eval/divergence/{sid}/rows?complete=false").json()["total"] == 0
    assert c.get(f"/api/eval/divergence/{sid}/rows?quadrant=both_weak").json()["total"] == sum(1 for x in s["scatter"] if x["quadrant"] == "both_weak")
    rl = c.get(f"/api/eval/divergence/{sid}/reading-list").json()
    assert len(rl) == 3 and rl[0]["review"]["summary"].startswith("summary of") and set(rl[0]["review"]) == {"summary", "pushbacks", "story_breaks", "credibility_score", "model"}
    assert set(s["scatter"][0]) == {"id", "parts", "whole", "quadrant", "delta"}
    assert c.get("/api/eval/divergence").json()[0]["spot_checks"]["locked"] is True


def test_cli_reading_list_warns_while_checks_are_pending(env, monkeypatch, capsys):
    import argparse

    from tret import cli_eval

    p = argparse.ArgumentParser()
    cli_eval.register(p.add_subparsers(dest="command"))
    sid = make_set(env, pool_world(env, n=9))
    monkeypatch.setattr("tret.db.engine.get_session_factory", lambda: env.world.factory)
    assert cli_eval.run(p.parse_args(["eval", "divergence", "reading-list", str(sid)])) == 0
    cap = capsys.readouterr()
    assert "still pending" in cap.err and "divergence" in cap.out.lower() or "biggest" in cap.out


def test_agreement_gate_warns_on_a_short_sample_and_tripped_still_wins():
    ok = [fake("triad", "done", 1.0)] * 15 + [fake("critique", "done", 1.0)] * 10
    assert dv.spot_check_gate(ok, SPOT, {"triads": 0, "critiques": 0})["state"] == "ok"
    g = dv.spot_check_gate(ok[:12] + ok[15:], SPOT, {"triads": 3, "critiques": 0})
    assert g["state"] == "warn" and "only 12 of 15 triads could be drawn" in g["detail"]
    g = dv.spot_check_gate(ok[:15], SPOT, {"triads": 0, "critiques": 10})
    assert g["state"] == "warn" and "no critiques were drawn" in g["detail"]
    low = [fake("triad", "done", 0.0)] * 12 + ok[15:]
    assert dv.spot_check_gate(low, SPOT, {"triads": 3})["state"] == "tripped"


def test_a_short_sample_shows_as_a_warn_on_the_set(env, monkeypatch):
    ids = pool_world(env, n=3)  # one triad can be drawn of four
    sid = make_set(env, ids)
    answer_all(env, sid)
    s = fetch(env.world, lambda db: db.get(DivergenceSet, sid))
    g = {x["key"]: x for x in dv.gates_for(s, checks_of(env, sid))}["spot_check_agreement"]
    assert g["state"] == "warn" and "only 1 of 4 triads could be drawn" in g["detail"]


def test_a_race_between_answer_and_review_is_closed_by_the_conditional_row_touch(env):
    ids = pool_world(env, n=9)
    sid = make_set(env, ids)
    cs = checks_of(env, sid)
    # a second session reviews the set after this one loaded the check: the answer is refused, nothing is written
    async def go():
        async with env.world.factory() as db:
            c = await db.get(SpotCheck, cs[0].id)
            async with env.world.factory() as other:
                await _set_status(other, sid, "reviewed")
            with pytest.raises(dv.DivergenceError) as e:
                await dv.answer_spot_check(db, c, user=env.world.user, answer={"skip": True})
            assert e.value.status == 409
            with pytest.raises(dv.DivergenceError) as e:
                await dv.mark_reviewed(db, await db.get(DivergenceSet, sid), env.world.user)
            assert e.value.status == 409

    run(go())
    assert checks_of(env, sid)[0].status == "pending"


def test_a_refit_or_a_changed_pass_marks_the_set_stale(env):
    ids = pool_world(env, n=9)
    c = client(env)
    sid = c.post("/api/eval/divergence", json=body(ids)).json()["id"]
    s = c.get(f"/api/eval/divergence/{sid}").json()
    assert s["stale"] is False and s["stale_tournaments"] == [] and all(t["fingerprint"] for t in s["inputs"]["tournaments"]) and s["inputs"]["pass_fingerprint"]

    async def refit():
        async with env.world.factory() as db:
            r = (await db.execute(select(ModuleRating).where(ModuleRating.tournament_id == ids.tid["m1"]))).scalars().first()
            r.rating += 5
            await db.commit()

    run(refit())
    s = c.get(f"/api/eval/divergence/{sid}").json()
    assert s["stale"] is True and s["stale_tournaments"] == [str(ids.tid["m1"])] and s["stale_pass"] is False

    async def rescore():
        async with env.world.factory() as db:
            r = (await db.execute(select(CredibilityReview).where(CredibilityReview.pass_id == ids.pass_id))).scalars().first()
            r.credibility_score = 10 if r.credibility_score != 10 else 9
            await db.commit()

    fresh = client(env).post("/api/eval/divergence", json=body(ids)).json()  # a new set after the refit is fresh again
    assert fresh["stale"] is False
    run(rescore())
    s2 = c.get(f"/api/eval/divergence/{fresh['id']}").json()
    assert s2["stale"] is True and s2["stale_pass"] is True and s2["stale_tournaments"] == []


def test_triads_skip_unbanded_ratings_and_need_three_distinct_ratings(env):
    # nine members, but only two distinct rating values in the middle and top: no triad of distinct ratings exists
    specs = [spec(NAMES[i], 5, {"m1": (1 + i, "decile", "p1", 1000 if i < 3 else 1100)}) for i in range(9)]
    ids = seed(env, specs, modules=("m1",))
    sid = make_set(env, ids)
    s = fetch(env.world, lambda db: db.get(DivergenceSet, sid))
    assert [c for c in checks_of(env, sid) if c.kind == "triad"] == [] and s.computed["stats"]["spot_check_shortfall"]["triads"] == 4
    # members without a band are not in the pool at all
    specs = [spec(NAMES[i], 5, {"m1": (None if i >= 3 else 1 + i, "decile", "p1", 1000 + 50 * i)}) for i in range(9)]
    ids = seed(env, specs, modules=("m1",))

    async def pools():
        async with env.world.factory() as db:
            return await dv._pools(db, [str(ids.tid["m1"])])

    assert len(run(pools())[str(ids.tid["m1"])]["p1"]) == 3
    # and a pool with ties still yields only distinct-rating triads when it can
    specs = [spec(NAMES[i], 5, {"m1": (1 + i, "decile", "p1", 1000 + 50 * (i // 2))}) for i in range(12)]
    ids = seed(env, specs, modules=("m1",))
    sid = make_set(env, ids)
    for c in checks_of(env, sid):
        if c.kind == "triad":
            assert len({m["rating"] for m in c.item["members"]}) == 3


def test_masking_catches_another_members_company_and_its_variants(env):
    specs = [
        spec("Acme Chemicals", 4, {"m1": (2, "decile", "p1", 1000)}, company="Acme Chemicals",
             text="# m1\nThe company has targets.\n", mask_names=["ACM Group"], mask_domains=["acme-chem.com"]),
        spec("Borel Dynamics", 6, {"m1": (5, "decile", "p1", 1100)}, company="Borel Dynamics",
             text="# m1\nUnlike Acme Chemicals, ACME CHEMICALS and Acme Chemicals plc we cut Scope 1. Borel Dynamics leads. "
                  "CMC Resources and https://www.cirrus-mining.com/esg are not us.\n"),
        spec("Cirrus Mining", 8, {"m1": (9, "decile", "p1", 1200)}, company="Cirrus Mining", mask_names=["CMC Resources"],
             mask_domains=["cirrus-mining.com"],
             text="# m1\nCirrus Mining reports; peers such as Borel Dynamics and Acme Chemicals Ltd differ. See ACM Group and acme-chem.com.\n"),
    ]
    ids = seed(env, specs, modules=("m1",))
    sid = make_set(env, ids)
    triad = next(o for o in outs(env, sid) if o["kind"] == "triad")
    blob = json.dumps(triad).lower()
    for leak in ("acme", "borel", "cirrus", "acm group", "cmc resources", "acme-chem.com", "cirrus-mining.com"):
        assert leak not in blob, (leak, blob)


def test_a_second_set_on_a_spent_pass_gets_no_spot_checks_and_the_first_stays(env, monkeypatch):
    monkeypatch.setattr(get_settings(), "eval_divergence_min_n", 1000)
    ids = pool_world(env, n=9)
    first = make_set(env, ids)
    # all of the first set's checks are still pending: a second set gets its own sample
    second = make_set(env, ids)
    assert len(checks_of(env, second)) == 8
    answer(env, checks_of(env, first)[0].id, {"skip": True})  # now the first set's blinding is spent
    third = make_set(env, ids)
    assert checks_of(env, third) == []
    s = fetch(env.world, lambda db: db.get(DivergenceSet, third))
    assert s.computed["stats"]["spot_checks_from_set"] == str(first)
    c = client(env)
    out = c.get(f"/api/eval/divergence/{third}").json()
    g = {x["key"]: x for x in out["gates"]}["spot_check_agreement"]
    assert g["state"] == "not_applicable" and g["detail"] == "Spot checks for this pass were already answered in set S; their result stands there."
    assert out["spot_checks"] == {"total": 0, "answered": 0, "locked": True} and len(out["scatter"]) == 9
    assert c.get(f"/api/eval/divergence/{third}/rows").status_code == 200 and c.get(f"/api/eval/divergence/{third}/reading-list").status_code == 200
    assert c.get(f"/api/eval/divergence/{third}/spot-checks").json() == []
    done = c.post(f"/api/eval/divergence/{third}/review", json={})
    assert done.status_code == 200 and done.json()["status"] == "reviewed"
    # the first set cannot be deleted any more, the untouched second one can
    expect(409, lambda: run(_delete(env, first)))
    run(_delete(env, second))


async def _delete(env, sid):
    async with env.world.factory() as db:
        await dv.delete_set(db, await db.get(DivergenceSet, sid))


def test_delete_needs_every_check_still_pending(env):
    sid = make_set(env, pool_world(env, n=9))
    answer(env, checks_of(env, sid)[0].id, {"skip": True})
    assert "kept as the record" in expect(409, lambda: run(_delete(env, sid)))
    assert fetch(env.world, lambda db: db.get(DivergenceSet, sid)) is not None and len(checks_of(env, sid)) == 8


def test_acknowledge_and_delete_are_refused_on_a_reviewed_set_and_two_acks_both_land(env):
    ids = pool_world(env, n=9)
    sid = make_set(env, ids)
    answer_all(env, sid, agree=False)  # spot gate trips
    acknowledge(env, sid, "spot_check_agreement", note="first")
    # a stale copy of the set from before the acknowledgement must not overwrite it
    async def stale_ack():
        async with env.world.factory() as db:
            old = await db.get(DivergenceSet, sid)
            async with env.world.factory() as other:
                await dv.acknowledge_gate(other, await other.get(DivergenceSet, sid), env.world.user, "spot_check_agreement", "second")
            return await dv.acknowledge_gate(db, old, env.world.user, "spot_check_agreement", "third")

    out = run(stale_ack())
    assert [a["note"] for a in out.acknowledged_gates if a["gate"] == "spot_check_agreement"] == ["third"]
    s = fetch(env.world, lambda db: db.get(DivergenceSet, sid))
    done = review(env, sid) if all(g["state"] != "tripped" or g["key"] in {a["gate"] for a in s.acknowledged_gates} for g in dv.gates_for(s, checks_of(env, sid))) else None
    if done is None:
        for g in dv.gates_for(s, checks_of(env, sid)):
            if g["state"] == "tripped" and g["key"] not in {a["gate"] for a in s.acknowledged_gates}:
                acknowledge(env, sid, g["key"])
        review(env, sid)
    expect(409, lambda: acknowledge(env, sid, "spot_check_agreement"))
    expect(409, lambda: run(_delete(env, sid)))
    # a copy loaded before the review cannot ack or delete either
    async def late():
        async with env.world.factory() as db:
            old = await db.get(DivergenceSet, sid)
        async with env.world.factory() as db2:
            with pytest.raises(dv.DivergenceError) as e:
                await dv.acknowledge_gate(db2, old, env.world.user, "spot_check_agreement", "late")
            assert e.value.status == 409

    run(late())


def test_the_pass_fingerprint_covers_only_the_selected_arm(env):
    specs = [spec(co, whole, {"m1": (band, "decile", "p1", 1000 + band * 10 + i)}, arm="a" if i % 2 == 0 else "b") for i, (co, band, whole) in enumerate(SIX)]
    ids = seed(env, specs, modules=("m1",))
    c = client(env)
    sid = c.post("/api/eval/divergence", json=body(ids, arm_key="a")).json()["id"]

    async def rescore(arm):
        async with env.world.factory() as db:
            r = (await db.execute(select(CredibilityReview).where(CredibilityReview.pass_id == ids.pass_id, CredibilityReview.arm_key == arm))).scalars().first()
            r.credibility_score = 10 if r.credibility_score != 10 else 9
            await db.commit()

    run(rescore("b"))
    assert c.get(f"/api/eval/divergence/{sid}").json()["stale"] is False  # another arm's score is not this set's input
    run(rescore("a"))
    assert c.get(f"/api/eval/divergence/{sid}").json()["stale_pass"] is True


def test_spot_check_texts_are_read_concurrently(env, monkeypatch):
    sid = make_set(env, pool_world(env, n=9))
    live = {"now": 0, "max": 0}
    real = dv._member_text

    async def slow(m):
        live["now"] += 1
        live["max"] = max(live["max"], live["now"])
        await asyncio.sleep(0.01)
        try:
            return await real(m)
        finally:
            live["now"] -= 1

    monkeypatch.setattr(dv, "_member_text", slow)
    out = outs(env, sid)
    assert live["max"] > 3 and sum(1 for o in out if o["kind"] == "triad") == 4 and all(t["text"] for o in out if o["kind"] == "triad" for t in o["texts"])
