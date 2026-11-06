"""The scorecard, comparison and evaluation routes: shapes, roles, workspace
isolation, route order and the deprecated `/collections` aliases."""
# ruff: noqa: F811 - the imported fixtures are used by name as test arguments
from __future__ import annotations

import uuid

from tests.test_batch_evaluation import eng, make, run_batch  # noqa: F401 - fixtures
from tests.test_collections_api import client, run_db, settle, world  # noqa: F401 - fixtures
from tret.db.models import Batch


def test_default_rubrics_are_served_before_the_id_routes(client, world):
    d = client.get("/api/batches/rubrics/default?profile=disclosure")
    assert d.status_code == 200
    body = d.json()
    assert body["key"] == "disclosure-v1" and body["acceptable_quality"] == 60
    assert [c["key"] for c in body["criteria"]][:2] == ["right_document", "coverage"]
    assert {"key", "version", "title", "criteria", "flags"} <= set(body)
    p = client.get("/api/batches/rubrics/default").json()
    assert p["key"] == "prompt-default-v1" and len(p["flags"]) == 5
    assert client.get("/api/collections/rubrics/default?profile=prompt").status_code == 200
    assert client.get("/api/batches/rubrics/default?profile=other").status_code == 422


def test_compare_validates_its_arguments(client, world, eng):
    a = make(client, world, evaluation=None)
    assert client.get(f"/api/batches/compare?a={a['id']}&b={a['id']}").status_code == 422
    assert client.get(f"/api/batches/compare?a={a['id']}&b={uuid.uuid4()}").status_code == 404
    assert client.get("/api/batches/compare?a=nope&b=nope").status_code == 422


def test_compare_hides_other_workspaces(client, world, eng):
    a = make(client, world, evaluation=None)
    other = uuid.uuid4()

    async def add(db):
        db.add(Batch(id=other, workspace_id=world.other_ws.id, project_id=world.project.id,
                     name="Theirs", run_slug="theirs-1", status="draft", settings={}))
        await db.commit()

    run_db(client, world, add)
    assert client.get(f"/api/batches/compare?a={a['id']}&b={other}").status_code == 404
    for path in ("scorecard", "evaluation"):
        assert client.get(f"/api/batches/{other}/{path}").status_code == 404
    assert client.post(f"/api/batches/{other}/evaluate", json={}).status_code == 404


def test_reads_need_membership_and_writes_need_the_approver_role(client, world, eng):
    b = make(client, world, evaluation=None)
    run_batch(client, b["id"])
    world.role = "analyst"
    assert client.get(f"/api/batches/{b['id']}/scorecard").status_code == 200
    assert client.get(f"/api/batches/{b['id']}/evaluation").status_code == 200
    assert client.post(f"/api/batches/{b['id']}/evaluate", json={}).status_code == 403
    assert client.post(f"/api/batches/{b['id']}/evaluation/cancel").status_code == 403
    target = client.get(f"/api/batches/{b['id']}").json()["targets"][0]["id"]
    assert client.post(f"/api/batches/{b['id']}/targets/{target}/evaluation/review", json={"verdict": "agree"}).status_code == 403


def test_the_evaluation_resource_before_and_after_a_pass(client, world, eng):
    b = make(client, world, evaluation=None)
    run_batch(client, b["id"])
    empty = client.get(f"/api/batches/{b['id']}/evaluation").json()
    assert empty == {"evaluation": None, "rubric": None, "passes": [], "targets": []}
    r = client.post(f"/api/batches/{b['id']}/evaluate", json={"budget_usd": 3})
    assert r.status_code == 200
    started = r.json()
    assert started["budget_usd"] == 3.0 and started["judge_model"] == "anthropic/claude-opus-5-5"
    assert set(started) >= {"id", "status", "rubric_key", "rubric_version", "cost_usd", "estimated_cost_usd",
                            "counts", "spot_checks", "agreement", "summary", "error", "created_at", "finished_at"}
    settle(client)
    d = client.get(f"/api/batches/{b['id']}/evaluation").json()
    assert d["evaluation"]["status"] == "completed" and len(d["passes"]) == 1 and len(d["targets"]) == 3
    t = d["targets"][0]
    assert set(t) >= {"id", "target_id", "position", "label", "row_key", "attempt", "stale", "status", "skip_reason",
                      "error", "scores", "quality", "difficulty", "flags", "confidence", "rationale", "relative",
                      "review", "cost_usd", "evaluator_run_id", "finished_at"}
    assert t["position"] == 1 and t["label"] == "Acme Ltd"
    listed = client.get("/api/batches").json()["batches"][0]
    assert listed["quality_mean"] == 75.0 and listed["evaluation_status"] == "completed"
    assert listed["evaluation_cost_usd"] > 0 and listed["family_key"].startswith("prompt:")
    assert client.get(f"/api/collections/{b['id']}/scorecard").json()["evaluation"]["status"] == "completed"


def test_start_batch_evaluate_and_batch_status_scorecard(client, world, eng):
    import json

    from tests.test_batch_tools import call
    from tret.engine.batch_tools import batch_status, start_batch

    out, _ = call(client, world, start_batch, name="Eval me", template="Assess {company}.",
                  rows=[{"company": "A"}, {"company": "B"}], evaluate=True)
    bid = json.loads(out)["batch_id"]
    settle(client)
    st, _ = call(client, world, batch_status, batch_id=bid)
    card = json.loads(st)["scorecard"]
    assert card["evaluation_status"] == "completed" and card["quality_mean"] == 75.0 and card["acceptable"] == 2
    assert client.get(f"/api/batches/{bid}").json()["settings"]["evaluation"]["enabled"] is True
