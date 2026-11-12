"""Batch experiments (`tret/batches/experiments.py`, `/api/batches/experiments`): arm
validation, replicate and split creation, per-run overrides at launch, results, the
admin-only promotion and its history row. Built on the prompt-batch fixtures."""
# ruff: noqa: F811 - the imported fixtures are used by name as test arguments
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from tests.test_batches_prompt import body as prompt_body, engine, settle  # noqa: F401 - fixtures
from tests.test_collections_api import client, run_db, world  # noqa: F401 - fixtures
from tret.batches import experiments
from tret.db.models import Batch, BatchExperiment, BatchTarget, Harness, HarnessPolicyHistory, Run

CHEAP = "anthropic/claude-sonnet-5"
OTHER = "anthropic/claude-opus-5"
ROWS = [{"company": f"Co{i}", "country": "DE"} for i in range(1, 7)]


def make(client, world, arms, mode="replicate", **over):
    payload = {
        "name": "Model test", "mode": mode, "arms": arms,
        "batch": prompt_body(world, csv=None, rows=ROWS, run_slug="exp", name="Screen"),
        **over,
    }
    return client.post("/api/batches/experiments", json=payload)


TWO = [{"model_override": CHEAP}, {"model_override": OTHER}]


def test_replicate_creates_one_batch_per_arm_with_overrides(client, world, engine):
    r = make(client, world, [{"model_override": CHEAP}, {"objective": "eco", "max_iterations": 7}])
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["experiment"]["mode"] == "replicate" and out["experiment"]["harness_name"] == world.harness.name
    assert [a["key"] for a in out["arms"]] == ["A", "B"] and out["arms"][0]["label"] == CHEAP
    assert out["recommendation"]["arm"] is None and out["comparisons"][0]["paired"] is True
    listed = client.get("/api/batches/experiments").json()["experiments"]
    assert [e["id"] for e in listed] == [out["experiment"]["id"]]
    ids = [a["batch_id"] for a in out["arms"]]
    details = [client.get(f"/api/batches/{i}").json() for i in ids]
    assert [d["run_slug"] for d in details] == ["exp-a", "exp-b"]
    assert details[0]["experiment"] == {"id": out["experiment"]["id"], "arm": "A"}
    assert details[1]["settings"]["objective"] == "eco" and details[1]["settings"]["max_iterations"] == 7
    for i in ids:
        assert client.post(f"/api/batches/{i}/start").status_code == 200
    settle(client)

    async def runs(db):
        rows = (await db.execute(select(Run.task_input))).scalars().all()
        return [t for t in rows if "_label" in t and "_max_iterations" in t or t.get("_model_override")]

    inputs = run_db(client, world, runs)
    assert sum(1 for t in inputs if t.get("_model_override") == CHEAP) == 6
    assert sum(1 for t in inputs if t.get("_objective") == "eco" and t.get("_max_iterations") == 7) == 6
    # started twice -> 409
    assert client.post(f"/api/batches/experiments/{out['experiment']['id']}/start").status_code == 409


def test_replicate_sample_keeps_the_same_rows(client, world, engine):
    r = make(client, world, TWO, sample_n=3)
    assert r.status_code == 200, r.text
    labels = []
    for a in r.json()["arms"]:
        d = client.get(f"/api/batches/{a['batch_id']}").json()
        labels.append([t["label"] for t in d["targets"]])
        assert [t["position"] for t in d["targets"]] == [1, 2, 3]
    assert labels[0] == labels[1] and len(labels[0]) == 3


def test_split_round_robins_and_stamps_arms_at_launch(client, world, engine):
    r = make(client, world, [{"model_override": CHEAP}, {"model_override": OTHER}, {"max_iterations": 9}], mode="split")
    assert r.status_code == 200, r.text
    out = r.json()
    assert len({a["batch_id"] for a in out["arms"]}) == 1
    d = client.get(f"/api/batches/{out['arms'][0]['batch_id']}").json()
    assert [t["arm"] for t in d["targets"]] == ["A", "B", "C", "A", "B", "C"]
    assert d["experiment"]["id"] == out["experiment"]["id"] and len(d["experiment"]["arms"]) == 3
    assert client.post(f"/api/batches/experiments/{out['experiment']['id']}/start").status_code == 200
    settle(client)
    d = client.get(f"/api/batches/{d['id']}").json()
    assert all(t["status"] in ("completed", "written") for t in d["targets"])

    async def runs(db):
        return (await db.execute(select(Run.task_input))).scalars().all()

    inputs = [t for t in run_db(client, world, runs) if "_label" in t]
    assert sum(1 for t in inputs if t.get("_model_override") == CHEAP) == 2
    assert sum(1 for t in inputs if t.get("_model_override") == OTHER) == 2
    assert sum(1 for t in inputs if t.get("_max_iterations") == 9 and "_model_override" not in t) == 2
    res = client.get(f"/api/batches/experiments/{out['experiment']['id']}").json()
    assert all(c["paired"] is False and c["note"] == "different rows" and c["wins"] is None for c in res["comparisons"])
    assert [a["targets"] for a in res["arms"]] == [2, 2, 2]
    assert res["recommendation"]["arm"] is None


@pytest.mark.parametrize(
    ("arms", "extra", "fragment"),
    [
        ([{"model_override": CHEAP}], {}, "arms"),
        ([{"model_override": CHEAP}, {}], {}, "sets nothing"),
        ([{"model_override": CHEAP}, {"model_override": CHEAP}], {}, "identical"),
        ([{"model_override": CHEAP}, {"model_override": "nope/none"}], {}, "not in the catalog"),
        ([{"model_override": CHEAP}, {"objective": "cheapest"}], {}, "objective"),
        ([{"model_override": CHEAP}, {"max_iterations": 51}], {}, "max_iterations"),
        ([{"model_override": CHEAP}, {"max_iterations": 0}], {}, "max_iterations"),
        (TWO, {"sample_n": 1}, "sample_n"),
        (TWO, {"mode": "split", "sample_n": 3}, "sample_n"),
    ],
)
def test_arm_validation_is_a_422(client, world, engine, arms, extra, fragment):
    r = make(client, world, arms, **extra)
    assert r.status_code == 422 and fragment in r.text
    assert client.get("/api/batches/experiments").json() == {"experiments": []}


def test_a_model_outside_the_harness_policy_is_a_422_naming_the_arm(client, world, engine):
    async def narrow(db):
        h = await db.get(Harness, world.harness.id)
        h.model_policy = {"mode": "auto", "allowed": [CHEAP]}
        await db.commit()

    run_db(client, world, narrow)
    r = make(client, world, TWO)
    assert r.status_code == 422 and "Arm B" in r.text and "allowed" in r.text


def test_creation_is_all_or_nothing_on_a_slug_collision(client, world, engine):
    assert client.post("/api/batches", json=prompt_body(world, run_slug="exp-b")).status_code == 200
    r = make(client, world, TWO)
    assert r.status_code == 409

    async def count(db):
        slugs = (await db.execute(select(Batch.run_slug))).scalars().all()
        return slugs, (await db.execute(select(BatchExperiment.id))).scalars().all()

    slugs, exps = run_db(client, world, count)
    assert sorted(slugs) == ["exp-b"] and exps == []


def test_strata_and_unpaired_helpers_are_deterministic():
    rows = [{"position": i, "market": m, "category": "c"} for i, m in enumerate("xxxyyz", start=1)]
    picked = experiments.sample_rows(rows, 3, "disclosure")
    assert [r["market"] for r in picked] == ["x", "y", "z"] and [r["position"] for r in picked] == [1, 2, 3]
    arms = experiments.assign_arms(rows, ["A", "B"], "disclosure")
    assert [r["arm"] for r in arms] == ["A", "B", "A", "B", "A", "B"]
    qa, qx = [80.0, 82.0, 84.0, 86.0], [88.0, 90.0, 92.0, 94.0]
    assert experiments._unpaired(qa, qx) == experiments._unpaired(qa, qx)
    mean, interval = experiments._unpaired(qa, qx)
    assert mean == 8.0 and interval[0] < 8.0 < interval[1]
    assert experiments._unpaired(qa, [90.0]) == (7.0, None)


@pytest.mark.parametrize(
    ("objective", "expected"), [("eco", "B"), ("balanced", "B"), ("quality", "A")]
)
def test_recommendation_uses_the_advice_rule(objective, expected):
    def arm(key, lo, hi, cost):
        return {"key": key, "scored": 6, "quality_interval90": [lo, hi], "cost_per_acceptable_usd": cost}

    out = experiments._recommend([arm("A", 90, 95, 0.5), arm("B", 88, 94, 0.05)], objective)
    assert out["arm"] == expected and "arm" in out["reason"]
    assert experiments._recommend([{**arm("A", 90, 95, 0.5), "scored": 4}], "balanced")["arm"] is None


def promote(client, arm="B"):
    exp = client.get("/api/batches/experiments").json()["experiments"][0]["id"]
    return exp, client.post(f"/api/batches/experiments/{exp}/promote", json={"arm": arm})


def test_promote_is_admin_only_and_records_history(client, world, engine):
    assert make(client, world, [{"model_override": CHEAP}, {"model_override": OTHER, "max_iterations": 12}]).status_code == 200
    world.role = "approver"
    assert promote(client)[1].status_code == 403
    world.role = "admin"
    exp, r = promote(client)
    assert r.status_code == 200, r.text
    assert r.json()["model_policy"].get("mode") != "pinned" and "model" not in r.json()["model_policy"]
    assert r.json()["loop_config"]["max_iterations"] == 12

    async def state(db):
        h = await db.get(Harness, world.harness.id)
        rows = (await db.execute(select(HarnessPolicyHistory))).scalars().all()
        e = await db.get(BatchExperiment, uuid.UUID(exp))
        return h.model_policy, h.loop_config, rows, e.promoted

    policy, loop, rows, promoted = run_db(client, world, state)
    assert "model" not in policy and loop["max_iterations"] == 12
    assert len(rows) == 1 and rows[0].source == "experiment" and str(rows[0].source_id) == exp
    assert rows[0].changed_by == world.user.id and rows[0].previous_loop_config != rows[0].new_loop_config
    assert promoted["arm"] == "B" and promoted["by_name"] == "Rex Reviewer" and promoted["history_id"] == str(rows[0].id)
    assert promote(client)[1].status_code == 409
    got = client.get(f"/api/batches/experiments/{exp}").json()["experiment"]["promoted"]
    assert got["arm"] == "B"


def test_cross_workspace_experiments_are_a_404(client, world, engine):
    assert make(client, world, TWO).status_code == 200
    exp = client.get("/api/batches/experiments").json()["experiments"][0]["id"]

    async def move(db):
        e = await db.get(BatchExperiment, uuid.UUID(exp))
        e.workspace_id = world.other_ws.id
        await db.commit()

    run_db(client, world, move)
    world.role = "admin"
    assert client.get(f"/api/batches/experiments/{exp}").status_code == 404
    assert client.post(f"/api/batches/experiments/{exp}/start").status_code == 404
    assert client.post(f"/api/batches/experiments/{exp}/promote", json={"arm": "A"}).status_code == 404
    assert client.get("/api/batches/experiments").json() == {"experiments": []}


def test_experiment_batches_keep_their_target_arm_column_null_when_replicated(client, world, engine):
    out = make(client, world, TWO).json()

    async def arms(db):
        return (await db.execute(select(BatchTarget.arm))).scalars().all()

    assert set(run_db(client, world, arms)) == {None} and out["arms"][0]["batch_id"]


# ── results on hand-built evaluation data ─────────────────────────────────────
from decimal import Decimal  # noqa: E402

from tests.test_batch_cross_check import mk_batch, mk_row, mk_target  # noqa: E402
from tests.test_batch_pairwise import JUDGE, mk_pass, w  # noqa: E402, F401 - fixtures
from tret.batches import scoring  # noqa: E402

OTHER_JUDGE = "anthropic/claude-sonnet-5-5"


async def _arm_batch(w, slug, rows, *, judge=JUDGE, settings=None, arms=None):
    """A completed batch with a scored pass; rows are (slug, quality, cost[, arm])."""
    batch = await mk_batch(w, f"Batch {slug}", slug)
    batch.settings = {"fiscal_year": 2025, **(settings or {})}
    ev = await mk_pass(w, batch, judge=judge)
    for i, (tslug, quality, cost, *rest) in enumerate(rows):
        t = await mk_target(w, batch, tslug, position=i)
        if rest:
            t.arm = rest[0]
        run = await w.db.get(Run, t.run_id)
        run.cost_usd, run.iterations = Decimal(str(cost)), 4
        await mk_row(w, ev, batch, t, quality)
    await w.db.commit()
    return batch


async def _experiment(w, mode, batches, arms, *, policy=None):
    if policy is not None:
        (await w.db.get(Harness, w.harness.id)).model_policy = policy
    for i, (a, b) in enumerate(zip(arms, batches)):
        a["key"] = "ABC"[i]
        a["batch_id"] = str(b.id)
    e = BatchExperiment(
        id=uuid.uuid4(), workspace_id=w.ws.id, name="E", mode=mode, profile="disclosure",
        family_key=scoring.family_key(batches[0]), harness_id=w.harness.id, arms=arms,
    )
    w.db.add(e)
    await w.db.commit()
    return e


def _rows(qualities, cost, prefix="r"):
    return [(f"{prefix}{i}", q, cost) for i, q in enumerate(qualities, start=1)]


QA = [80, 82, 84, 86, 88, 90]
QB = [92, 94, 84, 86, 78, 100]  # vs A: +12, +12, 0, 0, -10, +10


async def test_replicate_result_numbers_and_comparison_orientation(w):
    a = await _arm_batch(w, "ea-a", _rows(QA, 0.10))
    b = await _arm_batch(w, "ea-b", _rows(QB, 0.02))
    e = await _experiment(w, "replicate", [a, b], [{"label": "slow"}, {"label": "fast"}])
    out = await experiments.experiment_result(w.db, e)
    arm_a, arm_b = out["arms"]
    assert (arm_a["targets"], arm_a["settled"], arm_a["delivered"], arm_a["scored"], arm_a["acceptable"]) == (6, 6, 6, 6, 6)
    assert arm_a["yield"] == 1.0 and arm_a["quality_mean"] == 85.0
    assert arm_a["cost_usd"] == pytest.approx(0.6) and arm_a["cost_per_acceptable_usd"] == pytest.approx(0.1)
    assert arm_b["quality_mean"] == 89.0 and arm_b["cost_per_acceptable_usd"] == pytest.approx(0.02)
    assert arm_b["batch_status"] == "completed" and len(arm_b["quality_interval90"]) == 2
    cmp = scoring.compare_batches
    direct = (await cmp(w.db, a, b))["summary"]
    (c,) = out["comparisons"]
    assert c["paired"] is True and c["vs"] == "A" and c["arm"] == "B" and c["n"] == 6
    assert (c["wins"], c["losses"], c["ties"]) == (3, 1, 2) == (direct["wins_b"], direct["wins_a"], direct["ties"])
    assert c["mean_delta"] == direct["mean_quality_delta"] == 4.0 and c["mean_delta"] > 0
    assert c["interval90"] == direct["interval90"] and out["quality_comparable"] is True


@pytest.mark.parametrize(
    ("gap_quality", "expected"),
    [(88, {"eco": "B", "balanced": "B", "quality": "A"}), (86, {"eco": "B", "balanced": "A", "quality": "A"})],
)
@pytest.mark.parametrize("objective", ["eco", "balanced", "quality"])
async def test_replicate_recommendation_per_objective(w, objective, gap_quality, expected):
    a = await _arm_batch(w, "ra-a", _rows([90] * 6, 0.10))
    b = await _arm_batch(w, "ra-b", _rows([gap_quality] * 6, 0.02))
    e = await _experiment(
        w, "replicate", [a, b], [{}, {}], policy={"mode": "auto", "objective": objective}
    )
    rec = (await experiments.experiment_result(w.db, e))["recommendation"]
    assert rec["arm"] == expected[objective] and rec["reason"]


async def test_fewer_than_five_scored_rows_means_no_recommendation(w):
    a = await _arm_batch(w, "fa-a", _rows([90] * 4, 0.10))
    b = await _arm_batch(w, "fa-b", _rows([88] * 4, 0.02))
    e = await _experiment(w, "replicate", [a, b], [{}, {}])
    rec = (await experiments.experiment_result(w.db, e))["recommendation"]
    assert rec["arm"] is None and "nothing to recommend" in rec["reason"]


async def test_arms_scored_by_different_judges_are_not_comparable(w):
    a = await _arm_batch(w, "ja-a", _rows(QA, 0.10))
    b = await _arm_batch(w, "ja-b", _rows(QB, 0.02), judge=OTHER_JUDGE)
    e = await _experiment(w, "replicate", [a, b], [{}, {}])
    out = await experiments.experiment_result(w.db, e)
    assert out["quality_comparable"] is False and "judge" in out["quality_note"]
    assert out["recommendation"]["arm"] is None
    assert out["comparisons"][0]["mean_delta"] is None and out["comparisons"][0]["n"] == 0


@pytest.mark.parametrize(("qa", "qb", "sign"), [([80, 82, 84, 86], [90, 92, 94, 96], 1), ([90, 92, 94, 96], [80, 82, 84, 86], -1)])
async def test_split_result_is_unpaired_with_a_deterministic_interval(w, qa, qb, sign):
    rows = []
    for i in range(4):
        rows += [(f"s{2 * i}", qa[i], 0.05, "A"), (f"s{2 * i + 1}", qb[i], 0.01, "B")]
    arms = [{"label": "a"}, {"label": "b"}]
    batch = await _arm_batch(w, "sp", rows)
    batch.settings = {"fiscal_year": 2025, "experiment": {"id": "x", "arms": arms}}
    e = await _experiment(w, "split", [batch, batch], arms)
    out = await experiments.experiment_result(w.db, e)
    arm_a, arm_b = out["arms"]
    assert (arm_a["targets"], arm_a["scored"], arm_a["settled"]) == (4, 4, 4) == (arm_b["targets"], arm_b["scored"], arm_b["settled"])
    assert arm_a["quality_mean"] == statistics_mean(qa) and arm_b["quality_mean"] == statistics_mean(qb)
    assert arm_a["cost_per_acceptable_usd"] == pytest.approx(0.05) and arm_b["cost_per_acceptable_usd"] == pytest.approx(0.01)
    (c,) = out["comparisons"]
    assert c["paired"] is False and c["note"] == "different rows" and c["n"] == 4
    assert (c["wins"], c["losses"], c["ties"]) == (None, None, None)
    assert c["mean_delta"] == 10.0 * sign
    lo, hi = c["interval90"]
    assert (lo > 0) if sign > 0 else (hi < 0)
    again = await experiments.experiment_result(w.db, e)
    assert again["comparisons"][0]["interval90"] == c["interval90"]


def statistics_mean(values):
    return round(sum(values) / len(values), 1)


# ── promote, per arm field ────────────────────────────────────────────────────
def _history(client, world):
    async def go(db):
        rows = (await db.execute(select(HarnessPolicyHistory).order_by(HarnessPolicyHistory.changed_at))).scalars().all()
        return [
            (r.source, str(r.source_id), r.previous_model_policy, r.new_model_policy, r.previous_loop_config, r.new_loop_config, str(r.id))
            for r in rows
        ]

    return run_db(client, world, go)


def _set_policy(client, world, policy, loop):
    async def go(db):
        h = await db.get(Harness, world.harness.id)
        h.model_policy, h.loop_config = policy, loop
        await db.commit()

    run_db(client, world, go)


def test_promote_each_arm_field_and_a_second_arm_replaces_the_first(client, world, engine):
    base = {"mode": "auto", "objective": "balanced", "max_cost_tier": "premium"}
    loop = {"max_iterations": 20, "max_cost_usd": 5}
    _set_policy(client, world, base, loop)
    arms = [{"model_override": CHEAP}, {"objective": "eco"}, {"max_iterations": 12}]
    res = make(client, world, arms).json()
    plan_a = res["arms"][0]["promotion"]
    # a model-only arm changes no harness setting
    assert plan_a["allowed"] is False and plan_a["changes"] == [] and plan_a["token"] is None
    assert plan_a["blocked_reason"] == "A model arm changes no harness setting: its runs are evidence for the router."
    assert any("not pinned" in n and "router" in n for n in plan_a["notes"])
    assert res["arms"][1]["promotion"]["changes"] == [{"field": "Objective", "from": "balanced", "to": "eco"}]
    assert res["arms"][2]["promotion"]["changes"] == [{"field": "Max iterations", "from": "20", "to": "12"}]
    assert res["experiment"]["current_arm"] is None
    world.role = "admin"
    exp, r = promote(client, "A")
    assert r.status_code == 409 and "evidence for the router" in r.text
    assert _history(client, world) == []
    r = promote(client, "B")[1]
    assert r.status_code == 200, r.text
    eco = {**base, "objective": "eco"}
    assert r.json()["model_policy"] == eco and r.json()["loop_config"] == loop
    assert r.json()["experiment"]["experiment"]["current_arm"] == "B"
    again = promote(client, "B")[1]
    assert again.status_code == 409 and "already has these settings" in again.text
    # C replaces B on the pre-promotion baseline
    shown = client.get(f"/api/batches/experiments/{exp}").json()["arms"][2]["promotion"]
    assert "replaces the earlier promotion of arm B" in shown["notes"]
    r = promote(client, "C")[1]
    assert r.json()["loop_config"] == {"max_iterations": 12, "max_cost_usd": 5} and r.json()["model_policy"] == base
    rows = _history(client, world)
    assert [x[0] for x in rows] == ["experiment"] * 2 and {x[1] for x in rows} == {exp}
    assert rows[0][2] == base and rows[0][3] == eco and rows[0][4] == rows[0][5] == loop
    assert rows[1][2] == eco and rows[1][3] == base and rows[1][5] == {"max_iterations": 12, "max_cost_usd": 5}
    got = client.get(f"/api/batches/experiments/{exp}").json()["experiment"]["promoted"]
    assert got["arm"] == "C" and got["by_name"] == "Rex Reviewer" and got["history_id"] == rows[1][6]
    world.role = "approver"
    assert promote(client, "B")[1].status_code == 403


def test_the_same_arm_can_be_promoted_again_after_a_revert(client, world, engine):
    _set_policy(client, world, {"mode": "auto", "objective": "balanced"}, {"max_iterations": 20})
    assert make(client, world, [{"objective": "eco"}, {"max_iterations": 9}]).status_code == 200
    world.role = "admin"
    exp, r = promote(client, "A")
    assert r.status_code == 200
    history_id = r.json()["history"]["id"]
    async def revert(db):
        from tret.services import harness_policy

        h = await db.get(Harness, world.harness.id)
        await harness_policy.revert_policy_change(db, h, uuid.UUID(history_id), world.user)
        await db.commit()

    run_db(client, world, revert)
    assert promote(client, "A")[1].status_code == 200
    assert [x[0] for x in _history(client, world)] == ["experiment", "revert", "experiment"]


def test_a_policy_tightened_after_creation_does_not_matter_to_promotion(client, world, engine):
    _set_policy(client, world, {"mode": "auto"}, {})
    res = make(client, world, [{"model_override": OTHER, "objective": "eco"}, {"model_override": CHEAP}]).json()
    assert res["arms"][0]["promotion"]["allowed"] is True
    _set_policy(client, world, {"mode": "auto", "allowed": [CHEAP]}, {})
    shown = client.get(f"/api/batches/experiments/{res['experiment']['id']}").json()["arms"][0]["promotion"]
    assert shown["allowed"] is True and "allowed model list" not in str(shown)
    world.role = "admin"
    r = promote(client, "A")[1]
    assert r.status_code == 200
    assert r.json()["model_policy"] == {"mode": "auto", "allowed": [CHEAP], "objective": "eco"}


def test_sample_n_at_or_above_the_row_count_and_too_few_split_rows_are_422(client, world, engine):
    assert make(client, world, TWO, sample_n=6).status_code == 422
    assert make(client, world, TWO, sample_n=9).status_code == 422
    batch = prompt_body(world, csv=None, rows=ROWS[:2], run_slug="tiny", name="Tiny")
    r = client.post(
        "/api/batches/experiments",
        json={"name": "x", "mode": "split", "arms": [{"model_override": CHEAP}, {"model_override": OTHER}, {"max_iterations": 3}], "batch": batch},
    )
    assert r.status_code == 422 and "at least one row per arm" in r.text


def test_creation_warnings(client, world, engine):
    _set_policy(client, world, {"mode": "pinned", "model": CHEAP}, {})
    res = make(client, world, [{"objective": "eco"}, {"objective": "quality"}]).json()
    assert any("arm A changes only the objective" in w_ for w_ in res["experiment"]["warnings"])


def test_an_arm_that_sets_two_fields_promotes_only_the_non_model_ones(client, world, engine):
    base = {"mode": "auto", "objective": "balanced"}
    _set_policy(client, world, base, {"max_iterations": 20})
    assert make(client, world, [{"objective": "quality"}, {"model_override": OTHER, "max_iterations": 7}]).status_code == 200
    world.role = "admin"
    r = promote(client, "B")[1]
    assert r.json()["model_policy"] == base  # the model is never written
    assert r.json()["loop_config"] == {"max_iterations": 7}
    (row,) = _history(client, world)
    assert row[2] == row[3] == base and row[5] == {"max_iterations": 7}
    assert _arm_plan(client, r.json()["experiment"]["experiment"]["id"], "B")["blocked_reason"] == "the harness already has these settings"


def test_a_boot_upgrade_does_not_overwrite_a_promoted_policy(client, world, engine):
    from tret.services import workspace as ws_service

    old = dict(ws_service._PREVIOUS_COLLECTION_MODEL_POLICIES[0])
    _set_policy(client, world, old, {"max_iterations": 20})

    async def boot(db):
        await ws_service.ensure_collection_harness(db, world.ws.id)
        await db.commit()
        h = await db.get(Harness, world.harness.id)
        return dict(h.model_policy)

    assert run_db(client, world, boot) != old  # the premise: an unedited old seed is upgraded
    assert make(client, world, [{"max_iterations": 9}, {"objective": "quality"}]).status_code == 200
    world.role = "admin"
    promoted = promote(client, "B")[1].json()["model_policy"]
    assert run_db(client, world, boot) == promoted and promoted["objective"] == "quality"


# ── a normal batch is unchanged; the public body cannot carry experiment settings ──
def test_a_normal_batch_stores_no_experiment_and_stamps_no_objective(client, world, engine):
    from tret.engine.collection_tools import COLLECTION_ID_KEY, COLLECTION_TARGET_KEY

    d = client.post("/api/batches", json=prompt_body(world)).json()
    assert d["settings"]["objective"] is None and "experiment" not in d["settings"] and d["experiment"] is None
    assert all(t["arm"] is None for t in d["targets"])
    assert client.post(f"/api/batches/{d['id']}/start").status_code == 200
    settle(client)

    async def inputs(db):
        return [r for r in (await db.execute(select(Run.task_input))).scalars().all()]

    keys = [set(t) for t in run_db(client, world, inputs)]
    assert len(keys) == 3
    assert all(k == {"message", "_label", COLLECTION_ID_KEY, COLLECTION_TARGET_KEY} for k in keys)


@pytest.mark.parametrize("extra", ["experiment", "settings_extra"])
def test_the_public_create_body_forbids_experiment_settings(client, world, engine, extra):
    from tests.test_collections_api import REGISTER

    smuggled = {extra: {"id": str(uuid.uuid4()), "arm": "A"}}
    r = client.post("/api/batches", json={**prompt_body(world), **smuggled})
    assert r.status_code == 422
    r = client.post(
        "/api/batches",
        json={"name": "D", "run_slug": "d-smuggle", "register_csv": REGISTER, "target_slug": "test-output", **smuggled},
    )
    assert r.status_code == 422
    assert client.get("/api/batches").json()["batches"] == []


def _counts(arms):
    return {k: sum(1 for a in arms if a["arm"] == k) for k in sorted({a["arm"] for a in arms})}


def _strata_rows(sizes):
    rows = []
    for si, n in enumerate(sizes):
        for _ in range(n):
            rows.append({"position": len(rows) + 1, "market": f"m{si}", "category": "c"})
    return rows


def test_assign_arms_balances_overall_and_spreads_within_strata():
    assert _counts(experiments.assign_arms(_strata_rows([1] * 5), ["A", "B"], "disclosure")) == {"A": 3, "B": 2}
    assert _counts(experiments.assign_arms(_strata_rows([3] * 6), ["A", "B"], "disclosure")) == {"A": 9, "B": 9}
    three = experiments.assign_arms(_strata_rows([4, 3, 3]), ["A", "B", "C"], "disclosure")
    assert _counts(three) == {"A": 4, "B": 3, "C": 3}
    assert experiments.assign_arms(_strata_rows([4, 3, 3]), ["A", "B", "C"], "disclosure") == three  # deterministic
    big = experiments.assign_arms(_strata_rows([6]), ["A", "B"], "disclosure")
    assert [r["arm"] for r in big] == list("ABABAB")
    with pytest.raises(Exception, match="at least one row per arm"):
        experiments.assign_arms(_strata_rows([1, 1]), ["A", "B", "C"], "disclosure")


async def test_intervals_are_null_while_unsettled_and_collect_runs_once_per_arm(w, monkeypatch):
    a = await _arm_batch(w, "qa-a", _rows(QA, 0.10))
    b = await _arm_batch(w, "qa-b", _rows(QB, 0.02))
    e = await _experiment(w, "replicate", [a, b], [{}, {}])
    calls = []
    real = scoring.collect

    async def counting(db, batch):
        calls.append(batch.id)
        return await real(db, batch)

    monkeypatch.setattr(scoring, "collect", counting)
    t = (await w.db.execute(select(BatchTarget).where(BatchTarget.collection_id == b.id))).scalars().first()
    t.status = "running"
    await w.db.commit()
    out = await experiments.experiment_result(w.db, e)
    assert sorted(calls) == sorted([a.id, b.id])
    assert out["comparisons"][0]["interval90"] is None and out["arms"][0]["quality_interval90"] is None
    assert out["recommendation"]["arm"] is None and out["experiment"]["evaluating"] is False
    t.status = "completed"
    await w.db.commit()
    out = await experiments.experiment_result(w.db, e)
    assert out["comparisons"][0]["interval90"] is not None and out["arms"][0]["quality_interval90"] is not None


async def test_row_memory_ignores_the_experiments_own_arms(w):
    from tret.batches import memory

    exp_id = str(uuid.uuid4())
    mine = await _arm_batch(w, "mm-a", [], settings={"experiment": {"id": exp_id, "arm": "A"}})
    sibling = await _arm_batch(w, "mm-b", [], settings={"experiment": {"id": exp_id, "arm": "B"}})
    outside = await _arm_batch(w, "mm-c", [])
    pending = await mk_target(w, mine, "acme", "pending", position=0)
    for batch in (sibling, outside):
        t = await mk_target(w, batch, "acme", "failed", position=0)
        t.error = "boom"
    await w.db.commit()
    got = await memory.earlier_attempts(w.db, mine, [pending])
    assert pending.id in got
    assert got[pending.id].count("\n") < 6  # one block; the sibling's failed attempt is not in it
    only_sibling = await memory.earlier_attempts(w.db, sibling, [await mk_target(w, sibling, "zed", "pending", position=1)])
    assert only_sibling == {}


# ── promotion plan: full diff, token, foreign history rows ────────────────────
def _arm_plan(client, exp, key):
    arms = client.get(f"/api/batches/experiments/{exp}").json()["arms"]
    return next(a for a in arms if a["key"] == key)["promotion"]


def _fields(plan):
    return {c["field"]: (c["from"], c["to"]) for c in plan["changes"]}


def test_changes_list_everything_a_replacing_promotion_changes(client, world, engine):
    _set_policy(client, world, {"mode": "auto", "objective": "balanced"}, {"max_iterations": 24})
    res = make(client, world, [{"model_override": CHEAP, "max_iterations": 10}, {"objective": "eco"}]).json()
    exp = res["experiment"]["id"]
    # not replacing: only what really differs (never a model)
    assert _fields(res["arms"][0]["promotion"]) == {"Max iterations": ("24", "10")}
    assert _fields(res["arms"][1]["promotion"]) == {"Objective": ("balanced", "eco")}
    world.role = "admin"
    assert promote(client, "A")[1].status_code == 200
    plan = _arm_plan(client, exp, "B")
    assert "replaces the earlier promotion of arm A" in plan["notes"]
    assert _fields(plan) == {"Max iterations": ("10", "24"), "Objective": ("balanced", "eco")}
    assert _arm_plan(client, exp, "A")["blocked_reason"] == "the harness already has these settings"


def test_diff_lists_other_keys_compactly():
    cur, new = {"mode": "auto", "max_cost_tier": "premium"}, {"mode": "auto"}
    got = experiments._diff(cur, new, {"max_cost_usd": 5}, {"max_cost_usd": 5, "timeout": 3})
    assert got == [
        {"field": "policy.max_cost_tier", "from": '"premium"', "to": "unset"},
        {"field": "loop.timeout", "from": "unset", "to": "3"},
    ]


def test_a_stale_token_is_a_409_and_writes_nothing(client, world, engine):
    _set_policy(client, world, {"mode": "auto", "objective": "balanced"}, {"max_iterations": 24})
    res = make(client, world, [{"objective": "eco"}, {"max_iterations": 9}]).json()
    exp = res["experiment"]["id"]
    token_a, token_b = (a["promotion"]["token"] for a in res["arms"])
    assert len(token_a) == 16 and token_a != token_b
    world.role = "admin"
    post = lambda arm, tok: client.post(  # noqa: E731
        f"/api/batches/experiments/{exp}/promote", json={"arm": arm, "token": tok}
    )
    ok = post("A", token_a)
    assert ok.status_code == 200, ok.text
    stale = post("B", token_b)  # shown before A was promoted
    assert stale.status_code == 409 and "changed since this was shown" in stale.text
    assert len(_history(client, world)) == 1
    fresh = _arm_plan(client, exp, "B")["token"]
    assert fresh != token_b
    _set_policy(client, world, {"mode": "auto", "objective": "balanced"}, {"max_iterations": 30})  # an unrelated PUT
    assert post("B", fresh).status_code == 409 and len(_history(client, world)) == 1
    assert post("B", _arm_plan(client, exp, "B")["token"]).status_code == 200
    assert promote(client, "A")[1].status_code == 200  # no token still works
    assert len(_history(client, world)) == 3


def test_a_history_id_pointing_at_a_foreign_row_is_ignored(client, world, engine):
    base = {"mode": "auto", "objective": "balanced"}
    _set_policy(client, world, base, {"max_iterations": 24})
    res = make(client, world, [{"objective": "eco"}, {"max_iterations": 9}]).json()
    exp = res["experiment"]["id"]
    hid = uuid.uuid4()

    async def plant(db):
        db.add(
            HarnessPolicyHistory(
                id=hid, workspace_id=world.ws.id, harness_id=world.harness.id, changed_by=world.user.id,
                source="edit", source_id=None, previous_model_policy={"mode": "pinned", "model": OTHER},
                previous_loop_config={}, new_model_policy=base, new_loop_config={"max_iterations": 24},
            )
        )
        e = await db.get(BatchExperiment, uuid.UUID(exp))
        e.promoted = {"arm": "A", "by": "x", "by_name": "x", "at": "2026-11-12", "history_id": str(hid)}
        await db.commit()

    run_db(client, world, plant)
    plan = _arm_plan(client, exp, "B")
    assert plan["notes"] == [] and _fields(plan) == {"Max iterations": ("24", "9")}


async def test_paired_summary_orders_shared_rows_by_picked_position(w):
    settings = {"harness_id": str(w.harness.id), "template": "t", "label_column": "company"}

    async def prompt_batch(slug, rows):
        batch = await mk_batch(w, slug, slug, profile="prompt")
        batch.settings = settings
        ev = await mk_pass(w, batch)
        for i, (label, quality) in enumerate(rows):
            t = await mk_target(w, batch, f"{slug}-{i}", position=i)
            t.label = label
            await mk_row(w, ev, batch, t, quality)
        await w.db.commit()
        return batch

    # in A the key "acme" first appears at position 0 but `_pick` chooses the later "Acme #2"
    a = await prompt_batch("pa", [("Acme", 60), ("Beta", 80), ("Gamma", 82), ("Delta", 70), ("Epsilon", 75), ("Zeta", 90), ("Acme #2", 90)])
    b = await prompt_batch("pb", [("Acme", 85), ("Beta", 70), ("Gamma", 95), ("Delta", 72), ("Epsilon", 60), ("Zeta", 99)])
    da, db_ = await scoring.collect(w.db, a), await scoring.collect(w.db, b)
    pa = scoring.picked_rows(da)
    assert pa["acme"].target.position == 6
    in_position_order = [70 - 80, 95 - 82, 72 - 70, 60 - 75, 99 - 90, 85 - 90]  # acme is last
    in_dict_order = [85 - 90, *in_position_order[:-1]]
    expected = scoring.bootstrap_interval([float(x) for x in in_position_order])
    assert expected != scoring.bootstrap_interval([float(x) for x in in_dict_order])
    got = scoring.paired_summary(da, db_)
    assert got["interval90"] == expected and got["n"] == 6


# ── no pinning: models are evidence for the router ────────────────────────────
ARM_TABLE = [
    {"model_override": CHEAP},
    {"model_override": OTHER, "objective": "eco"},
    {"model_override": CHEAP, "max_iterations": 5},
    {"model_override": OTHER, "objective": "quality", "max_iterations": 9},
]


@pytest.mark.parametrize("arm", ARM_TABLE + [{"objective": "eco"}, {"max_iterations": 3}])
@pytest.mark.parametrize("policy", [{"mode": "auto", "objective": "balanced"}, {"mode": "pinned", "model": CHEAP}])
@pytest.mark.parametrize("replaces", [None, "auto", "pinned"])
def test_no_plan_ever_pins_a_model(arm, policy, replaces):
    ws = uuid.uuid4()
    loop = {"max_iterations": 20}
    last_row = None
    expect = policy  # the base the plan starts from
    if replaces:
        # an earlier promotion by this experiment wrote `policy`; its baseline was `prev`
        prev = {"mode": "auto", "objective": "quality"} if replaces == "auto" else {"mode": "pinned", "model": OTHER}
        last_row = SimpleNamespace(
            new_model_policy=policy, new_loop_config=loop, previous_model_policy=prev, previous_loop_config=loop
        )
        expect = prev
    e = SimpleNamespace(workspace_id=ws, promoted={"arm": "Z"} if replaces else None)
    harness = SimpleNamespace(is_archived=False, workspace_id=ws, model_policy=policy, loop_config=loop)
    full = {"key": "A", "model_override": None, "objective": None, "max_iterations": None, **arm}
    plan = experiments.promotion_plan(e, full, harness, last_row)
    new = plan["new_model_policy"]
    assert new.get("mode") == expect.get("mode") and new.get("model") == expect.get("model")
    if expect.get("mode") != "pinned":
        assert new.get("mode") != "pinned" and "model" not in new
    if arm.get("model_override") and expect.get("model") != arm["model_override"]:
        assert new.get("model") != arm["model_override"]
    if replaces:
        assert "replaces the earlier promotion" in plan["notes"][0]
    assert (plan["token"] is None) == (plan["blocked_reason"] == experiments.MODEL_ONLY_REASON)


def test_current_arm_prefers_the_promoted_arm_when_plans_coincide(client, world, engine):
    _set_policy(client, world, {"mode": "auto", "objective": "balanced"}, {"max_iterations": 20})
    res = make(client, world, [{"model_override": CHEAP, "objective": "eco"}, {"objective": "eco"}]).json()
    assert res["experiment"]["current_arm"] is None
    world.role = "admin"
    r = promote(client, "B")[1]
    assert r.status_code == 200, r.text
    assert r.json()["experiment"]["experiment"]["current_arm"] == "B"
    exp = res["experiment"]["id"]
    assert client.get(f"/api/batches/experiments/{exp}").json()["experiment"]["current_arm"] == "B"


def test_model_only_promote_with_a_token_gets_the_model_only_reason(client, world, engine):
    base = {"mode": "auto", "objective": "balanced"}
    _set_policy(client, world, base, {"max_iterations": 20})
    res = make(client, world, [{"model_override": CHEAP}, {"objective": "eco"}]).json()
    exp = res["experiment"]["id"]
    world.role = "admin"
    r = client.post(f"/api/batches/experiments/{exp}/promote", json={"arm": "A", "token": "deadbeefdeadbeef"})
    assert r.status_code == 409 and experiments.MODEL_ONLY_REASON in r.json()["detail"]
    assert "changed since" not in r.text
    assert _history(client, world) == []

    async def state(db):
        h = await db.get(Harness, world.harness.id)
        return h.model_policy, h.loop_config

    assert run_db(client, world, state) == (base, {"max_iterations": 20})


def test_model_only_arm_cannot_be_promoted_and_changes_nothing(client, world, engine):
    base = {"mode": "auto", "objective": "balanced"}
    _set_policy(client, world, base, {"max_iterations": 20})
    res = make(client, world, [{"model_override": CHEAP}, {"model_override": OTHER}]).json()
    assert all(a["promotion"]["allowed"] is False and a["promotion"]["token"] is None for a in res["arms"])
    world.role = "admin"
    r = promote(client, "A")[1]
    assert r.status_code == 409 and "A model arm changes no harness setting" in r.text
    assert _history(client, world) == []
    assert res["experiment"]["current_arm"] is None

    async def state(db):
        h = await db.get(Harness, world.harness.id)
        return h.model_policy, h.loop_config

    assert run_db(client, world, state) == (base, {"max_iterations": 20})


def test_a_model_plus_objective_arm_promotes_only_the_objective(client, world, engine):
    base = {"mode": "auto", "objective": "balanced", "allowed": [CHEAP, OTHER]}
    _set_policy(client, world, base, {"max_iterations": 20})
    res = make(client, world, [{"model_override": OTHER, "objective": "eco"}, {"model_override": CHEAP, "max_iterations": 8}]).json()
    plan = res["arms"][0]["promotion"]
    assert plan["allowed"] and _fields(plan) == {"Objective": ("balanced", "eco")}
    assert any("not pinned" in n for n in plan["notes"])
    world.role = "admin"
    r = promote(client, "A")[1]
    assert r.status_code == 200
    assert r.json()["model_policy"] == {**base, "objective": "eco"}  # mode/model/allowed untouched
    (row,) = _history(client, world)
    assert row[2] == base and row[3] == {**base, "objective": "eco"} and row[4] == row[5]
    # the max-iterations arm: replaces A on the baseline, changes only the loop
    plan_b = _arm_plan(client, res["experiment"]["id"], "B")
    assert _fields(plan_b) == {"Objective": ("eco", "balanced"), "Max iterations": ("20", "8")}
    r = promote(client, "B")[1]
    assert r.json()["model_policy"] == base and r.json()["loop_config"] == {"max_iterations": 8}


def test_replacing_an_old_pinning_promotion_shows_the_unpin(client, world, engine):
    base = {"mode": "auto", "objective": "balanced"}
    _set_policy(client, world, base, {"max_iterations": 20})
    res = make(client, world, [{"objective": "eco"}, {"max_iterations": 9}]).json()
    exp = res["experiment"]["id"]
    pinned = {**base, "mode": "pinned", "model": CHEAP}

    async def plant(db):
        hid = uuid.uuid4()
        db.add(
            HarnessPolicyHistory(
                id=hid, workspace_id=world.ws.id, harness_id=world.harness.id, changed_by=world.user.id,
                source="experiment", source_id=uuid.UUID(exp), previous_model_policy=base,
                previous_loop_config={"max_iterations": 20}, new_model_policy=pinned,
                new_loop_config={"max_iterations": 20},
            )
        )
        h = await db.get(Harness, world.harness.id)
        h.model_policy = pinned
        e = await db.get(BatchExperiment, uuid.UUID(exp))
        e.promoted = {"arm": "A", "by": "x", "by_name": "x", "at": "2026-11-12", "history_id": str(hid)}
        await db.commit()

    run_db(client, world, plant)
    plan = _arm_plan(client, exp, "B")
    assert _fields(plan) == {"Model": (f"pinned to {CHEAP}", "automatic routing"), "Max iterations": ("20", "9")}
    world.role = "admin"
    assert promote(client, "B")[1].json()["model_policy"] == base


def test_router_gate_is_in_the_result(client, world, engine):
    res = make(client, world, TWO).json()
    gate = res["experiment"]["router_gate"]
    assert set(gate) >= {"open", "spot_checks", "agreeing", "needed_checks"} and gate["open"] is False
    exp = res["experiment"]["id"]
    assert client.get(f"/api/batches/experiments/{exp}").json()["experiment"]["router_gate"] == gate


def test_router_gate_failure_is_null(client, world, engine, monkeypatch):
    async def boom(db, family):
        raise RuntimeError("down")

    monkeypatch.setattr(experiments, "safe_family_gate", boom)
    res = make(client, world, TWO).json()
    assert res["experiment"]["router_gate"] is None
