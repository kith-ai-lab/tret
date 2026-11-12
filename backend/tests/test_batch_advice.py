"""Advice for a batch about to be created (`tret/batches/advice.py`,
`POST /api/batches/advice`): the recommendation rules, the iteration and cost-cap
formulas, the concurrency suggestion and the API shape. Hand-built families in sqlite."""
# ruff: noqa: F811 - the imported fixtures are used by name as test arguments
from __future__ import annotations

import uuid

import pytest

from tests.test_batch_cross_check import mk_batch, mk_row, mk_target
from tests.test_batch_families import model_batch
from tests.test_batch_pairwise import RUBRIC, api, mk_pass, w  # noqa: F401 - fixtures
from tret.batches import advice, scoring
from tret.db.models import Harness, Run
from tret.batches.profiles.disclosure import COLLECTION_HARNESS_NAME

FAMILY = {
    "m/best": [(90, 0.5, 8), (91, 0.5, 9), (92, 0.5, 10), (93, 0.5, 8), (94, 0.5, 9)],
    "m/cheap": [(88, 0.04, 4), (89, 0.04, 5), (90, 0.04, 6), (91, 0.04, 8), (92, 0.04, 10)],
    "m/mid": [(86, 0.01, 3), (87, 0.01, 3), (87, 0.01, 3), (88, 0.01, 3), (87, 0.01, 3)],
    "m/weak": [(70, 0.001, 2), (71, 0.001, 2), (72, 0.001, 2), (70, 0.001, 2), (71, 0.001, 2)],
    "m/few": [(99, 0.0001, 1), (99, 0.0001, 1), (99, 0.0001, 1), (99, 0.0001, 1)],  # 4 scored: never
}


async def collector(w, objective):
    h = Harness(
        id=uuid.uuid4(), workspace_id=w.ws.id, name=COLLECTION_HARNESS_NAME, tool_names=[],
        model_policy={"mode": "auto", "objective": objective},
    )
    w.db.add(h)
    await w.db.commit()


def lower(rec_models, name):
    m = next(m for m in rec_models if m["model"] == name)
    return m["quality_interval90"][0]


@pytest.mark.parametrize(
    ("objective", "expected"),
    [("balanced", "m/cheap"), ("eco", "m/mid"), ("quality", "m/best")],
)
async def test_recommendation_per_objective(w, objective, expected):
    await model_batch(w, "B1", "b1", FAMILY)
    await collector(w, objective)
    out = await advice.batch_advice(w.db, w.ws.id, profile="disclosure", fiscal_year=2025)
    models = out["evidence"]["models"]
    best = lower(models, "m/best")
    # the construction: cheap is within 3 points of the best, mid within 5 but not 3
    assert best - lower(models, "m/cheap") <= 3 < best - lower(models, "m/mid") <= 5
    assert out["objective"] == objective and out["family_key"] == "disclosure:fy2025"
    assert out["recommendation"]["model"] == expected
    assert "m/few" not in (out["recommendation"]["model"],)
    assert any("fewer than 5 scored" in n for n in out["notes"])
    assert out["evidence"]["batches"] == 1 and out["evidence"]["comparable"]["rubric_key"] == RUBRIC["key"]


async def test_reason_carries_the_numbers(w):
    await model_batch(w, "B1", "b1", FAMILY)
    out = await advice.batch_advice(w.db, w.ws.id, profile="disclosure", fiscal_year=2025)
    reason = out["recommendation"]["reason"]
    assert reason.startswith("cheapest model whose quality lower bound (") and "within 3 points of the best (" in reason
    assert "$0.04 per acceptable result over 5 scored rows" in reason


async def test_formulas_and_the_cap_at_50(w):
    await model_batch(w, "B1", "b1", FAMILY)
    rec = (await advice.batch_advice(w.db, w.ws.id, profile="disclosure", fiscal_year=2025))["recommendation"]
    # cheap: p95 iterations (5 rows) = 10 -> ceil(12.5) = 13; p95 cost 0.04 -> 0.06
    assert rec["max_iterations"] == 13 and rec["cost_cap_usd"] == 0.06
    assert "95th-percentile of 10 iterations" in rec["max_iterations_basis"]
    assert rec["concurrency"] is None and rec["concurrency_reason"] is None


async def test_iteration_advice_is_capped_at_50(w):
    await model_batch(w, "B1", "b1", {"m/a": [(90, 0.1, 45)] * 5 + [(91, 0.1, 45)]})
    rec = (await advice.batch_advice(w.db, w.ws.id, profile="disclosure", fiscal_year=2025))["recommendation"]
    assert rec["max_iterations"] == 50 and "capped at 50" in rec["max_iterations_basis"]


async def test_no_eligible_model_means_no_recommendation_with_a_reason(w):
    await model_batch(w, "B1", "b1", {"m/few": FAMILY["m/few"]})
    out = await advice.batch_advice(w.db, w.ws.id, profile="disclosure", fiscal_year=2025)
    assert out["recommendation"] is None
    assert any("nothing to recommend" in n for n in out["notes"])
    empty = await advice.batch_advice(w.db, w.ws.id, profile="disclosure", fiscal_year=2031)
    assert empty["recommendation"] is None and empty["evidence"] == {"batches": 0, "comparable": None, "models": []}
    assert empty["notes"] == ["No earlier batch of this kind in this workspace."]


async def test_concurrency_is_suggested_only_above_ten_percent_infra_failures(w):
    ok = [(90, 0.1, 5)] * 8
    bad = await model_batch(w, "Bad", "bad", {"m/a": [*ok, (None, 0.1, 5, "failed"), (None, 0.1, 5, "failed")]}, minutes=5)
    bad.settings = {**bad.settings, "concurrency": 4}
    await w.db.commit()
    rec = (await advice.batch_advice(w.db, w.ws.id, profile="disclosure", fiscal_year=2025))["recommendation"]
    assert rec["concurrency"] == 3 and "2 of 10 targets (20%)" in rec["concurrency_reason"]
    # a newer batch with exactly 10% (1 of 10) is not above the line
    fine = await model_batch(w, "Fine", "fine", {"m/a": [*ok, (90, 0.1, 5), (None, 0.1, 5, "failed")]}, minutes=9)
    fine.settings = {**fine.settings, "concurrency": 4}
    await w.db.commit()
    rec = (await advice.batch_advice(w.db, w.ws.id, profile="disclosure", fiscal_year=2025))["recommendation"]
    assert rec["concurrency"] is None


async def test_another_workspace_is_invisible(w):
    await model_batch(w, "B1", "b1", FAMILY)
    out = await advice.batch_advice(w.db, w.other_ws.id, profile="disclosure", fiscal_year=2025)
    assert out["recommendation"] is None and out["evidence"]["batches"] == 0


async def test_prompt_family_is_the_harness_and_template_hash(w):
    batch = await mk_batch(w, "P", "p", profile="prompt")
    batch.harness_id = w.harness.id
    batch.settings = {"harness_id": str(w.harness.id), "harness_name": "H", "template": "Summarise {x}"}
    await w.db.commit()
    ev = await mk_pass(w, batch)
    for i in range(5):
        t = await mk_target(w, batch, f"row-{i}", "completed", position=i)
        run = await w.db.get(Run, t.run_id)
        run.model_used, run.iterations = "m/p", 3
        await mk_row(w, ev, batch, t, 80 + i)
    await w.db.commit()
    key = scoring.family_key(batch)
    out = await advice.batch_advice(w.db, w.ws.id, profile="prompt", harness_id=w.harness.id, template="Summarise {x}")
    assert out["family_key"] == key and out["family_label"].startswith("Prompt · H · Summarise {x}")
    assert out["recommendation"]["model"] == "m/p"
    other = await advice.batch_advice(w.db, w.ws.id, profile="prompt", harness_id=w.harness.id, template="Other {x}")
    assert other["recommendation"] is None and other["evidence"]["batches"] == 0


async def test_family_key_for_matches_family_key(w):
    d = await mk_batch(w, "D", "d", fy=2027)
    assert scoring.family_key(d) == scoring.family_key_for("disclosure", fiscal_year=2027)


# ── the API ──────────────────────────────────────────────────────────────────
async def test_api_shape_and_validation(w, api):
    await model_batch(w, "B1", "b1", FAMILY)
    r = await api.post("/api/batches/advice", json={"profile": "disclosure", "fiscal_year": 2025})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"family_key", "family_label", "objective", "evidence", "recommendation", "notes"}
    assert set(body["evidence"]) == {"batches", "comparable", "models"}
    assert set(body["evidence"]["comparable"]) == {"rubric_key", "rubric_version", "judge_model"}
    assert set(body["recommendation"]) == {
        "model", "reason", "max_iterations", "max_iterations_basis", "cost_cap_usd", "cost_cap_basis",
        "concurrency", "concurrency_reason",
    }
    assert body["recommendation"]["model"] == "m/cheap" and isinstance(body["notes"], list)
    assert {"model", "scored", "quality_mean", "quality_interval90", "mean_residual", "cost_per_acceptable_usd",
            "energy_per_acceptable_wh", "pareto", "p95_iterations", "p95_cost_usd"} <= set(body["evidence"]["models"][0])
    # no evidence: recommendation is null, the rest is still present
    none = (await api.post("/api/batches/advice", json={"profile": "disclosure", "fiscal_year": 2031})).json()
    assert none["recommendation"] is None and none["evidence"]["models"] == [] and none["evidence"]["comparable"] is None
    # validation
    assert (await api.post("/api/batches/advice", json={"profile": "prompt"})).status_code == 422
    assert (await api.post("/api/batches/advice", json={"profile": "prompt", "template": "x"})).status_code == 422
    assert (await api.post("/api/batches/advice", json={"profile": "nope"})).status_code == 422
    assert (await api.post("/api/batches/advice", json={"profile": "disclosure", "extra": 1})).status_code == 422
    ok = await api.post("/api/batches/advice", json={"profile": "prompt", "harness_id": str(w.harness.id), "template": "x {y}"})
    assert ok.status_code == 200 and ok.json()["recommendation"] is None


async def test_cost_cap_has_a_one_cent_floor(w):
    await model_batch(w, "B1", "b1", {"m/a": [(90, 0.002, 3)] * 5 + [(91, 0.002, 3)]})
    rec = (await advice.batch_advice(w.db, w.ws.id, profile="disclosure", fiscal_year=2025))["recommendation"]
    assert rec["cost_cap_usd"] == 0.01


async def test_concurrency_advice_without_an_eligible_model(w):
    b = await model_batch(w, "Bad", "bad", {"m/a": [(90, 0.1, 5)] * 2 + [(None, 0.1, 5, "failed")] * 2})
    b.settings = {**b.settings, "concurrency": 3}
    await w.db.commit()
    rec = (await advice.batch_advice(w.db, w.ws.id, profile="disclosure", fiscal_year=2025))["recommendation"]
    assert rec["model"] is None and rec["max_iterations"] is None and rec["cost_cap_usd"] is None
    assert rec["concurrency"] == 2 and "scored rows" in rec["reason"]


async def test_prompt_advice_for_an_archived_or_unknown_harness_is_a_422(w, api):
    r = await api.post("/api/batches/advice", json={"profile": "prompt", "harness_id": str(uuid.uuid4()), "template": "x {y}"})
    assert r.status_code == 422 and "no such harness" in r.json()["detail"]
    (await w.db.get(Harness, w.harness.id)).is_archived = True
    await w.db.commit()
    r = await api.post("/api/batches/advice", json={"profile": "prompt", "harness_id": str(w.harness.id), "template": "x {y}"})
    assert r.status_code == 422


async def test_a_free_model_gets_no_cost_cap_but_small_costs_keep_the_floor(w):
    await model_batch(w, "B1", "b1", {"m/a": [(90, 0.0, 3)] * 6})
    rec = (await advice.batch_advice(w.db, w.ws.id, profile="disclosure", fiscal_year=2025))["recommendation"]
    assert rec["cost_cap_usd"] is None and rec["cost_cap_basis"] is None
