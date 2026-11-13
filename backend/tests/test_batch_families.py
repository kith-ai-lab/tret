"""Family list, comparable set, per-model aggregates and the leaderboard
(`tret/batches/families.py`, `GET /api/batches/families`, `/api/batches/family`).

Hand-built families in sqlite (the world of `test_batch_pairwise.py`); no engine runs."""
# ruff: noqa: F811 - the imported fixtures are used by name as test arguments
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from tests.test_batch_cross_check import T0, mk_batch, mk_row, mk_target
from tests.test_batch_pairwise import JUDGE, RUBRIC, api, mk_pass, w  # noqa: F401 - fixtures
from tret.batches import families
from tret.db.models import Run

OTHER_JUDGE = "anthropic/claude-sonnet-5-5"


async def model_batch(w, name, slug, per_model, *, judge=JUDGE, rubric=None, minutes=0, fy=2025, evaluate=True):
    """A batch whose targets are `(quality, cost, iterations[, status])` per model."""
    batch = await mk_batch(w, name, slug, fy=fy)
    batch.created_at = T0 + timedelta(minutes=minutes)
    ev = await mk_pass(w, batch, judge=judge, rubric=rubric, minutes=minutes) if evaluate else None
    pos = 0
    for model, items in per_model.items():
        for quality, cost, iterations, *rest in items:
            status = rest[0] if rest else "completed"
            t = await mk_target(w, batch, f"{model.split('/')[-1]}-{pos}", status, position=pos)
            run = await w.db.get(Run, t.run_id)
            run.model_used, run.cost_usd, run.iterations = model, Decimal(str(cost)), iterations
            if status == "failed":
                t.error = "OSError: [Errno 28] No space left on device"
            if ev is not None and quality is not None:
                await mk_row(w, ev, batch, t, quality)
            pos += 1
    await w.db.commit()
    return batch


async def test_per_model_aggregates_on_a_hand_built_family(w):
    await model_batch(
        w, "B1", "b1",
        {
            "m/cheap": [(60, 0.01, 4), (62, 0.01, 5), (64, 0.01, 6), (None, 0.01, 4, "failed")],
            "m/strong": [(90, 0.5, 8), (92, 0.5, 9), (94, 0.5, 10)],
            "m/dominated": [(50, 0.6, 8), (52, 0.6, 9), (54, 0.6, 10)],
            "m/small": [(99, 0.001, 2), (98, 0.001, 3)],
        },
    )
    out = await families.family_models(w.db, w.ws.id, "disclosure:fy2025")
    assert out["comparable"] == {"rubric_key": RUBRIC["key"], "rubric_version": RUBRIC["version"], "judge_model": JUDGE}
    assert out["excluded_batches"] == 0
    models = {m["model"]: m for m in out["models"]}
    assert [m["model"] for m in out["models"]] == ["m/small", "m/strong", "m/cheap", "m/dominated"]  # quality desc
    cheap = models["m/cheap"]
    assert (cheap["batches"], cheap["targets"], cheap["delivered"], cheap["scored"]) == (1, 4, 3, 3)
    assert cheap["infra_failures"] == 1
    # the infra target leaves the yield denominator: 3 acceptable (60, 62, 64 >= 60) of 3
    assert cheap["acceptable"] == 3 and cheap["yield"] == 1.0
    assert cheap["quality_mean"] == 62.0 and cheap["quality_interval90"] is not None
    assert cheap["cost_usd"] == pytest.approx(0.04)
    assert cheap["cost_per_acceptable_usd"] == pytest.approx(0.04 / 3, abs=1e-6)
    assert cheap["mean_iterations"] == 4.8 and cheap["p95_iterations"] == 6 and cheap["p95_cost_usd"] == 0.01
    dominated = models["m/dominated"]
    assert dominated["acceptable"] == 0 and dominated["cost_per_acceptable_usd"] is None
    assert dominated["yield"] == 0.0 and dominated["pareto"] is False
    small = models["m/small"]
    assert small["scored"] == 2 and small["quality_interval90"] is None and small["pareto"] is False
    assert models["m/strong"]["pareto"] is True and cheap["pareto"] is True


async def test_pareto_drops_a_dominated_model(w):
    await model_batch(
        w, "B", "b",
        {
            "m/a": [(70, 0.1, 3), (72, 0.1, 3), (74, 0.1, 3)],
            "m/b": [(70, 0.2, 3), (72, 0.2, 3), (74, 0.2, 3)],  # same quality, dearer: dominated
            "m/c": [(90, 0.4, 3), (92, 0.4, 3), (94, 0.4, 3)],
        },
    )
    models = {m["model"]: m for m in (await families.family_models(w.db, w.ws.id, "disclosure:fy2025"))["models"]}
    assert (models["m/a"]["pareto"], models["m/b"]["pareto"], models["m/c"]["pareto"]) == (True, False, True)


async def test_comparable_set_is_the_most_common_and_others_are_excluded(w):
    await model_batch(w, "B1", "b1", {"m/a": [(70, 0.1, 3)]}, minutes=1)
    await model_batch(w, "B2", "b2", {"m/a": [(80, 0.1, 3)]}, minutes=2)
    await model_batch(w, "B3", "b3", {"m/z": [(10, 0.1, 3)]}, minutes=3, judge=OTHER_JUDGE)
    await model_batch(w, "B4", "b4", {"m/a": [(70, 0.1, 3)]}, minutes=4, evaluate=False)
    out = await families.family_models(w.db, w.ws.id, "disclosure:fy2025")
    assert out["comparable"]["judge_model"] == JUDGE and out["excluded_batches"] == 2
    assert [m["model"] for m in out["models"]] == ["m/a"] and out["models"][0]["batches"] == 2


async def test_a_tie_for_the_comparable_set_goes_to_the_newest(w):
    await model_batch(w, "B1", "b1", {"m/a": [(70, 0.1, 3)]}, minutes=1)
    await model_batch(w, "B2", "b2", {"m/z": [(10, 0.1, 3)]}, minutes=9, judge=OTHER_JUDGE)
    out = await families.family_models(w.db, w.ws.id, "disclosure:fy2025")
    assert out["comparable"]["judge_model"] == OTHER_JUDGE and out["excluded_batches"] == 1


async def test_list_families_is_newest_first_and_counts_evaluated(w):
    await model_batch(w, "Old", "old", {"m/a": [(70, 0.1, 3)]}, minutes=1, fy=2024)
    await model_batch(w, "N1", "n1", {"m/a": [(70, 0.1, 3)]}, minutes=2)
    await model_batch(w, "N2", "n2", {"m/a": [(70, 0.1, 3)]}, minutes=3, evaluate=False)
    listed = await families.list_families(w.db, w.ws.id)
    assert [f["family_key"] for f in listed] == ["disclosure:fy2025", "disclosure:fy2024"]
    assert listed[0] == {
        "family_key": "disclosure:fy2025", "family_label": "Disclosure FY2025", "profile": "disclosure",
        "batches": 2, "evaluated_batches": 1, "last_created_at": listed[0]["last_created_at"],
    }
    assert listed[0]["last_created_at"] > listed[1]["last_created_at"]
    assert await families.list_families(w.db, w.other_ws.id) == []


async def test_the_leaderboard_payload_and_the_api_shapes(w, api):
    b1 = await model_batch(w, "B1", "b1", {"m/a": [(70, 0.1, 3), (80, 0.1, 3), (90, 0.1, 3)]}, minutes=1)
    b2 = await model_batch(w, "B2", "b2", {"m/b": [(60, 0.2, 3), (62, 0.2, 3), (64, 0.2, 3)]}, minutes=2)
    odd = await model_batch(w, "B3", "b3", {"m/c": [(60, 0.2, 3)]}, minutes=3, judge=OTHER_JUDGE)
    board = await families.family_leaderboard(w.db, w.ws.id, "disclosure:fy2025")
    assert [b["batch_id"] for b in board["batches"]] == [str(odd.id), str(b2.id), str(b1.id)]  # newest first
    assert [b["comparable"] for b in board["batches"]] == [False, True, True]
    first = board["batches"][2]
    assert set(first) == {
        "batch_id", "name", "status", "created_at", "started_at", "targets", "classes", "acceptable", "yield",
        "written_share", "quality_mean", "quality_median", "mean_residual", "cost_usd", "cost_per_acceptable_usd",
        "energy_wh", "energy_per_acceptable_wh", "wall_seconds", "models", "judge_model", "rubric_version",
        "comparable", "spot_checks", "agreement",
    }
    assert first["quality_mean"] == 80.0 and first["models"] == [{"model": "m/a", "targets": 3}]
    assert board["row_keys"] == 7 and board["excluded_batches"] == 1

    r = await api.get("/api/batches/families")  # the list is an envelope
    assert r.status_code == 200 and set(r.json()) == {"families"}
    assert r.json()["families"][0]["family_key"] == "disclosure:fy2025"
    r = await api.get("/api/batches/family", params={"key": "disclosure:fy2025"})  # a FLAT object
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"family_key", "family_label", "profile", "comparable", "excluded_batches", "row_keys",
                         "batches", "models", "router_gate"}
    assert set(body["router_gate"]) == {"open", "spot_checks", "agreeing", "agreement", "needed_checks",
                                        "needed_agreement", "mode", "rubric_version", "judge_model"}
    assert isinstance(body["models"], list) and body["excluded_batches"] == 1
    assert body["comparable"]["judge_model"] == JUDGE
    assert set(body["models"][0]) >= {"model", "batches", "targets", "delivered", "scored", "acceptable", "yield",
                                       "quality_mean", "quality_interval90", "mean_residual", "cost_usd",
                                       "cost_per_acceptable_usd", "energy_per_acceptable_wh", "mean_iterations",
                                       "p95_iterations", "p95_cost_usd", "infra_failures", "pareto"}
    assert (await api.get("/api/batches/family", params={"key": "disclosure:fy1999"})).status_code == 404
    await mk_batch(w, "Theirs", "theirs", fy=2031, ws=w.other_ws, project=w.other_project)
    assert (await api.get("/api/batches/family", params={"key": "disclosure:fy2031"})).status_code == 404
    assert (await api.get("/api/collections/families")).status_code == 200  # the deprecated alias


async def test_a_dead_current_pass_is_not_part_of_the_comparable_set(w, api):
    await model_batch(w, "Live", "live", {"m/a": [(70, 0.1, 3)]}, minutes=1)
    dead = await model_batch(w, "Dead", "dead", {"m/z": [(10, 0.1, 3)]}, minutes=2, judge=OTHER_JUDGE)
    from sqlalchemy import update

    from tret.db.models import BatchEvaluation

    await w.db.execute(update(BatchEvaluation).where(BatchEvaluation.collection_id == dead.id).values(status="cancelled"))
    await w.db.commit()
    out = await families.family_models(w.db, w.ws.id, "disclosure:fy2025")
    assert out["comparable"]["judge_model"] == JUDGE and out["excluded_batches"] == 1
    assert [m["model"] for m in out["models"]] == ["m/a"]
    board = await families.family_leaderboard(w.db, w.ws.id, "disclosure:fy2025")
    row = next(b for b in board["batches"] if b["batch_id"] == str(dead.id))
    assert row["comparable"] is False and row["judge_model"] is None
    listed = (await families.list_families(w.db, w.ws.id))[0]
    assert listed["batches"] == 2 and listed["evaluated_batches"] == 1


async def test_latest_batch_is_the_models_most_recent_comparable_batch(w):
    await model_batch(w, "Old", "old", {"m/a": [(80, 0.1, 3), (82, 0.1, 3), (81, 0.1, 3)]}, minutes=0)
    await model_batch(
        w, "New", "new",
        {"m/a": [(90, 0.1, 3), (91, 0.1, 3), (92, 0.1, 3), (90, 0.1, 3)], "m/only": [(70, 0.1, 3)]},
        minutes=60,
    )
    models = {m["model"]: m for m in (await families.family_models(w.db, w.ws.id, "disclosure:fy2025"))["models"]}
    latest = models["m/a"]["latest_batch"]
    assert latest["name"] == "New" and latest["scored"] == 4 and latest["quality_mean"] == 90.8
    assert latest["created_at"] and latest["batch_id"]
    assert models["m/a"]["batches"] == 2 and models["m/a"]["quality_mean"] < latest["quality_mean"]
    only = models["m/only"]
    assert only["latest_batch"]["scored"] == only["scored"] == 1
    assert only["latest_batch"]["quality_mean"] == only["quality_mean"]


async def test_latest_batch_falls_back_to_an_older_scored_batch_when_the_newest_is_unscored(w):
    await model_batch(w, "Old", "old", {"m/a": [(80, 0.1, 3), (82, 0.1, 3)]}, minutes=0)
    await model_batch(w, "New", "new", {"m/a": [(None, 0.1, 3, "failed")]}, minutes=60)
    models = {m["model"]: m for m in (await families.family_models(w.db, w.ws.id, "disclosure:fy2025"))["models"]}
    assert models["m/a"]["latest_batch"]["name"] == "Old"


async def test_latest_batch_is_null_when_no_batch_has_a_scored_row(w):
    await model_batch(w, "Only", "only", {"m/a": [(None, 0.1, 3, "failed")]}, minutes=0)
    models = {m["model"]: m for m in (await families.family_models(w.db, w.ws.id, "disclosure:fy2025"))["models"]}
    assert models["m/a"]["latest_batch"] is None


async def test_latest_batch_prefers_a_batch_with_enough_scored_rows_over_a_newer_tiny_one(w):
    await model_batch(w, "Big", "big", {"m/a": [(80, 0.1, 3)] * 6}, minutes=0)
    await model_batch(w, "Tiny", "tiny", {"m/a": [(95, 0.1, 3)] * 2}, minutes=60)
    models = {m["model"]: m for m in (await families.family_models(w.db, w.ws.id, "disclosure:fy2025"))["models"]}
    latest = models["m/a"]["latest_batch"]
    assert latest["name"] == "Big" and latest["scored"] == 6


async def test_latest_batch_is_the_most_recent_when_every_batch_is_small(w):
    await model_batch(w, "Old", "old", {"m/a": [(80, 0.1, 3)] * 3}, minutes=0)
    await model_batch(w, "New", "new", {"m/a": [(90, 0.1, 3)] * 2}, minutes=60)
    models = {m["model"]: m for m in (await families.family_models(w.db, w.ws.id, "disclosure:fy2025"))["models"]}
    assert models["m/a"]["latest_batch"]["name"] == "New"
