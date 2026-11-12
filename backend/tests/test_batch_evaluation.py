"""The batch evaluator (`tret/batches/evaluation.py`, `engine/evaluation_tools.py`)
end to end on prompt batches: auto pass, blind evidence pack, tool validation,
budget, infra skips, retries, the batch review, cancel, retroactive passes,
spot checks, records, roll-up and harness seeding.

Built on the world/client fixtures of `test_collections_api.py` and the
`PromptEngine` of `test_batches_prompt.py`; the judge is a fake engine that calls
the real `submit_evaluation` / `submit_batch_review` tools on a `RunContext`.
"""
# ruff: noqa: F811 - the imported fixtures are used by name as test arguments
from __future__ import annotations

import asyncio
import json
import uuid
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from tests.test_batches_prompt import PromptEngine, body
from tests.test_collections_api import client, run_db, settle, world  # noqa: F401 - fixtures
from tret.batches import core, evaluation
from tret.config import get_settings
from tret.db.models import (
    Batch,
    BatchEvaluation,
    Harness,
    Run,
    RunOutcome,
    TargetEvaluation,
)
from tret.engine.evaluation_tools import submit_batch_review, submit_evaluation
from tret.engine.tools import RunContext, ToolError, get_builtin_tools
from tret.services import collections as service
from tret.services.outcomes import build_outcomes
from tret.services.workspace import (
    COLLECTION_HARNESS_TOOLS,
    EVALUATOR_DOCTRINE,
    EVALUATOR_HARNESS_NAME,
    EVALUATOR_HARNESS_TOOLS,
    ensure_evaluator_harness,
)

CRITERIA = ("correctness", "completeness", "instructions", "grounding")
MODEL_ID = "test/secret-model-x9"
EVAL_ON = {"enabled": True}


class EvalEngine(PromptEngine):
    """Target runs behave as `PromptEngine`'s (with a model, a cost and an
    iteration count the evidence pack must not leak); evaluator runs call the
    real evaluation tools."""

    def __init__(self, factory, ws_id):
        super().__init__(factory)
        self.ws_id = ws_id
        self.scores: dict[str, int] = {}  # slug -> score for every criterion (default 4)
        self.packs: dict[str, str] = {}
        self.docs: dict[str, list] = {}
        self.overrides: list[str] = []
        self.judged: list[str] = []
        self.errors: dict[str, str] = {}  # slug -> run error text
        self.flags: dict[str, list[str]] = {}
        self.bad_first: list[dict] = []
        self.tool_errors: list[str] = []
        self.judge_cost = "0.02"
        self.judge_mode = "ok"  # ok | silent | text_first | loop_first | hang | review_fails
        self.cancelled: list[uuid.UUID] = []
        self.review_calls = 0
        self.tool_error_text: dict[str, str] = {}
        self.retry_messages: list[str] = []
        self.iteration_caps: dict[str, list] = {}
        self.hang_review = False

    async def execute(self, run_id):
        async with self.factory() as db:
            run = await db.get(Run, run_id)
            ti = dict(run.task_input or {})
        if "_batch_evaluation_id" in ti:
            return await self._judge(run_id, ti)
        await super().execute(run_id)
        async with self.factory() as db:
            run = await db.get(Run, run_id)
            slug = ti["_label"].split(":", 1)[1]
            run.model_used = MODEL_ID
            run.provider_used = "secretprovider"
            run.iterations = 7
            run.cost_usd = Decimal("0.0137")
            if slug in self.errors:
                run.status, run.error = "failed", self.errors[slug]
            if slug in self.tool_error_text:
                run.messages = [
                    *run.messages,
                    {"role": "assistant", "content": None, "tool_calls": [{"id": "e1", "name": "fetch_url", "arguments": {"url": "https://x.example"}}]},
                    {"role": "tool", "tool_call_id": "e1", "content": self.tool_error_text[slug]},
                ]
            await db.commit()

    async def _judge(self, run_id, ti):
        label = ti["_label"].split(":", 1)[1]
        self.overrides.append(ti.get("_model_override"))
        async with self.factory() as db:
            run = await db.get(Run, run_id)
            run.status, run.model_used = "completed", "judge/model"
            run.cost_usd = Decimal(self.judge_cost)
            await db.commit()
            ctx = RunContext(
                db=db, run_id=run_id, project_id=run.project_id, pack_id=None, doctrine_sha=None,
                model_used="judge/model", document_ids=[], output_schemas={}, workspace_id=self.ws_id,
            )
            if self.judge_mode == "hang":
                while run_id not in self.cancelled:
                    await asyncio.sleep(0.02)
                return
            if ti.get("_batch_review"):
                self.review_calls += 1
                if self.hang_review:
                    while run_id not in self.cancelled:
                        await asyncio.sleep(0.02)
                    return
                self.packs["__review__"] = ti["message"]
                if self.judge_mode == "review_fails":
                    return
                await submit_batch_review(
                    ctx, narrative="Scores are consistent.", consistency_notes=["none"], lessons=["Keep it up"]
                )
                return
            self.judged.append(label)
            self.iteration_caps.setdefault(label, []).append(ti.get("_max_iterations"))
            self.packs[label] = ti["message"]
            self.docs[label] = list(run.document_ids or [])
            if self.judge_mode == "silent" or (self.judge_mode == "text_first" and not ti.get("_evaluator_retry")):
                self.retry_messages.append(ti["message"])
                return
            if self.judge_mode == "loop_first" and not ti.get("_evaluator_retry"):
                run.status, run.error = "failed", "max_iterations (8) reached without completion"
                await db.commit()
                return
            if self.judge_mode == "stuck_first" and not ti.get("_evaluator_retry"):
                run.status, run.error = "failed", "stopped: 3 identical failing calls to read_document"
                await db.commit()
                return
            score = self.scores.get(label, 4)
            good = {
                "scores": {k: {"score": score, "evidence": "see answer"} for k in CRITERIA},
                "difficulty": 3,
                "confidence": "high",
                "rationale": "fine",
                "flags": self.flags.get(label, []),
            }
            bad_calls, self.bad_first = self.bad_first, []  # the first judge takes them all
            for bad in bad_calls:
                try:
                    await submit_evaluation(ctx, **{**good, **bad})
                except ToolError as e:
                    self.tool_errors.append(str(e))
            await submit_evaluation(ctx, **good)

    def cancel(self, run_id):
        self.cancelled.append(run_id)
        return True


@pytest.fixture
def eng(world, monkeypatch, tmp_path):
    from tret.providers.catalog import ProviderRegistry

    monkeypatch.setattr(get_settings(), "storage_dir", str(tmp_path / "storage"))
    e = EvalEngine(world.factory, world.ws.id)
    e.keyed = {"anthropic", "openrouter"}  # providers that "have a key"
    # A stale app bound by an earlier test (an app that shut down and is "draining")
    # would keep passes from launching or completing.
    monkeypatch.setattr(core, "_app", None)
    monkeypatch.setattr(ProviderRegistry, "has_key", lambda self, provider: provider in e.keyed)
    monkeypatch.setattr(service, "get_harness_engine", lambda: e)
    return e


def make(client, world, slug="batch-a", evaluation=EVAL_ON, **over):
    payload = body(world, run_slug=slug, **over)
    if evaluation is not None:
        payload["evaluation"] = evaluation
    r = client.post("/api/batches", json=payload)
    assert r.status_code == 200, r.text
    return r.json()


def run_batch(client, bid):
    assert client.post(f"/api/batches/{bid}/start").status_code == 200
    settle(client)
    return client.get(f"/api/batches/{bid}").json()


def ev_detail(client, bid):
    r = client.get(f"/api/batches/{bid}/evaluation")
    assert r.status_code == 200, r.text
    return r.json()


def rows_of(client, world, bid):
    async def go(db):
        ev = (
            await db.execute(
                select(BatchEvaluation).where(BatchEvaluation.collection_id == uuid.UUID(bid))
                .order_by(BatchEvaluation.created_at.desc())
            )
        ).scalars().first()
        rows = (await db.execute(select(TargetEvaluation).where(TargetEvaluation.evaluation_id == ev.id))).scalars().all()
        return ev, rows

    return run_db(client, world, go)


# ── auto pass ────────────────────────────────────────────────────────────────
def test_create_stores_the_frozen_evaluation_settings(client, world, eng):
    b = make(client, world, evaluation={"enabled": True, "budget_usd": 2.5})
    ev = b["settings"]["evaluation"]
    assert ev["enabled"] and ev["rubric_key"] == "prompt-default-v1" and ev["budget_usd"] == 2.5
    assert ev["judge_model"] is None  # resolved when a pass starts
    assert [c["key"] for c in ev["rubric"]["criteria"]] == list(CRITERIA)
    assert b["family_key"].startswith("prompt:")
    off = make(client, world, slug="batch-off", evaluation=None)
    assert "evaluation" not in off["settings"]
    assert make(client, world, slug="batch-off2", evaluation={"enabled": False})["settings"].get("evaluation") is None


def test_an_evaluated_batch_judges_every_target_as_it_settles(client, world, eng):
    b = make(client, world)
    d = run_batch(client, b["id"])
    assert d["evaluation_status"] == "completed" and d["quality_mean"] == 75.0
    assert d["evaluation_cost_usd"] == pytest.approx(0.08)  # three judges + the batch review
    assert d["cost_usd"] == pytest.approx(0.0411)  # targets only
    assert sorted(eng.judged) == ["row-1", "row-2", "row-3"]
    assert set(eng.overrides) == {"anthropic/claude-opus-5-5"}
    assert all(t["evaluation"]["status"] == "scored" and t["evaluation"]["quality"] == 75.0 for t in d["targets"])
    detail = ev_detail(client, b["id"])
    assert detail["evaluation"]["counts"]["scored"] == 3 and detail["evaluation"]["status"] == "completed"
    assert detail["evaluation"]["summary"]["lessons"] == ["Keep it up"]
    assert detail["rubric"]["key"] == "prompt-default-v1"
    first = detail["targets"][0]
    assert first["relative"]["of"] == 3 and first["relative"]["expected_basis"] == "batch mean, difficulty-adjusted"
    assert first["scores"]["correctness"]["score"] == 4 and not first["stale"]


def test_the_evidence_pack_is_blind(client, world, eng):
    b = make(client, world)
    run_batch(client, b["id"])
    pack = eng.packs["row-1"]
    for leak in (MODEL_ID, "secret-model", "secretprovider", "0.0137", "iteration", "token", "energy", "cost"):
        assert leak not in pack.lower(), leak
    assert "Assess Acme Ltd" in pack and "correctness" in pack and "Answer for row-1" in pack
    assert len(pack) <= 24_000
    # With nothing attached the pack says so, so the judge does not try the document tools.
    assert "No documents are attached to this run" in pack and "call submit_evaluation" in pack


def test_evaluator_runs_use_the_seeded_harness_and_leave_no_outcomes(client, world, eng):
    b = make(client, world)
    run_batch(client, b["id"])

    async def go(db):
        harness = (await db.execute(select(Harness).where(Harness.name == EVALUATOR_HARNESS_NAME))).scalars().all()
        runs = (await db.execute(select(Run).where(Run.harness_id == harness[0].id))).scalars().all()
        outcomes = [await build_outcomes(db, r) for r in runs]
        stored = (await db.execute(select(RunOutcome))).scalars().all()
        return harness, runs, outcomes, stored

    harness, runs, outcomes, stored = run_db(client, world, go)
    assert len(harness) == 1 and len(runs) == 4
    assert all(o == [] for o in outcomes) and stored == []
    assert harness[0].tool_names == list(EVALUATOR_HARNESS_TOOLS)
    assert harness[0].loop_config["max_iterations"] == 8 and harness[0].model_policy["max_cost_tier"] == "premium"


def test_tool_validation_and_refusals(client, world, eng):
    eng.bad_first = [
        {"scores": {"correctness": {"score": 4, "evidence": "x"}}},  # missing criteria
        {"scores": {**{k: {"score": 4, "evidence": "x"} for k in CRITERIA}, "bogus": {"score": 1, "evidence": "x"}}},
        {"scores": {**{k: {"score": 4, "evidence": "x"} for k in CRITERIA}, "grounding": {"score": 6, "evidence": "x"}}},
        {"flags": ["not_a_flag"]},
        {"difficulty": 9},
        {"confidence": "sure"},
        {"scores": {k: {"score": None, "evidence": ""} for k in CRITERIA}},  # nothing applicable scored
    ]
    b = make(client, world)
    run_batch(client, b["id"])
    errors = " | ".join(eng.tool_errors)
    assert len(eng.tool_errors) == 7
    for fragment in ("missing", "Unknown criterion", "1 to 5", "Unknown flag", "difficulty", "confidence", "Score at least one"):
        assert fragment in errors, fragment

    async def outside(db):
        run = Run(
            id=uuid.uuid4(), project_id=world.project.id, harness_id=world.harness.id, task_type="chat",
            task_input={"message": "x"}, document_ids=[], status="running",
        )
        db.add(run)
        await db.commit()
        ctx = RunContext(db=db, run_id=run.id, project_id=world.project.id, pack_id=None, doctrine_sha=None,
                         model_used=None, document_ids=[], output_schemas={}, workspace_id=world.ws.id)
        out = []
        for call in (
            submit_evaluation(ctx, scores={}, difficulty=1, confidence="low", rationale="x"),
            submit_batch_review(ctx, narrative="x"),
        ):
            with pytest.raises(ToolError):
                await call
            out.append(True)
        return out

    assert run_db(client, world, outside) == [True, True]


def test_a_flagged_low_score_ranks_last(client, world, eng):
    eng.scores = {"row-3": 1, "row-1": 5}
    eng.flags = {"row-3": ["wrong_answer"]}
    b = make(client, world)
    d = run_batch(client, b["id"])
    by = {t["slug"]: t["evaluation"] for t in d["targets"]}
    assert by["row-3"]["rank"] == 3 and by["row-3"]["flags"] == ["wrong_answer"]
    assert by["row-1"]["rank"] == 1 and by["row-1"]["percentile"] == 100.0
    assert by["row-3"]["residual"] < 0 < by["row-1"]["residual"]


# ── budget, infra, retry ─────────────────────────────────────────────────────
def test_the_budget_stops_the_pass(client, world, eng, monkeypatch):
    # Two evaluator slots: the second judge must be held back by the budget alone, both
    # while the first runs and in the window after it scored but before its cost is booked.
    monkeypatch.setattr(get_settings(), "batch_evaluator_concurrency", 2)
    per = evaluation.estimate_evaluation_cost("anthropic/claude-opus-5-5", 1)
    eng.judge_cost = str(round(per, 6))
    b = make(client, world, evaluation={"enabled": True, "budget_usd": round(per * 1.5, 6)})
    run_batch(client, b["id"])
    counts = ev_detail(client, b["id"])["evaluation"]["counts"]
    # Running judges count against the budget at their estimate: exactly one fits.
    assert counts["scored"] == 1 and counts["not_evaluated_budget"] == 2, counts


def test_infrastructure_failures_are_skipped_not_judged(client, world, eng):
    eng.errors = {"row-2": "OSError: [Errno 28] No space left on device"}
    b = make(client, world)
    d = run_batch(client, b["id"])
    detail = ev_detail(client, b["id"])
    by = {t["label"]: t for t in detail["targets"]}
    assert by["Globex"]["status"] == "skipped" and "not the run's fault" in by["Globex"]["skip_reason"]
    assert "row-2" not in eng.judged and detail["evaluation"]["counts"]["scored"] == 2
    assert d["evaluation_status"] == "completed"


def test_a_retry_rearms_the_row(client, world, eng):
    eng.errors = {"row-2": "provider exploded"}
    b = make(client, world)
    d = run_batch(client, b["id"])
    globex = next(t for t in d["targets"] if t["slug"] == "row-2")
    assert eng.judged.count("row-2") == 1  # a failed (non-infra) run is judged too
    eng.errors = {}
    assert client.post(f"/api/batches/{b['id']}/targets/{globex['id']}/retry").status_code == 200
    settle(client)
    rows = rows_of(client, world, b["id"])[1]
    row = next(r for r in rows if str(r.target_id) == globex["id"])
    assert row.attempt == 2 and row.status == "scored"
    assert eng.judged.count("row-2") == 2


def test_an_unscored_run_fails_its_row(client, world, eng):
    eng.judge_mode = "silent"
    b = make(client, world)
    run_batch(client, b["id"])
    detail = ev_detail(client, b["id"])
    assert detail["evaluation"]["counts"]["failed"] == 3
    assert len(eng.judged) == 6  # each judge got exactly one more turn, then the row failed
    assert "did not submit" in detail["targets"][0]["error"]
    assert detail["evaluation"]["status"] == "completed" and detail["evaluation"]["summary"] is None


def test_a_failing_review_keeps_the_scores(client, world, eng):
    eng.judge_mode = "review_fails"

    async def noop():
        return None

    b = make(client, world)
    eng.judge_mode = "ok"
    orig = eng._judge

    async def judge(run_id, ti):
        if ti.get("_batch_review"):
            eng.review_calls += 1
            return None
        return await orig(run_id, ti)

    eng._judge = judge
    run_batch(client, b["id"])
    detail = ev_detail(client, b["id"])
    assert detail["evaluation"]["counts"]["scored"] == 3 and detail["evaluation"]["status"] == "completed"
    assert "did not submit a batch review" in detail["evaluation"]["error"]


# ── retroactive, cancel, review ──────────────────────────────────────────────
def test_evaluating_a_finalized_batch_and_only_missing(client, world, eng):
    b = make(client, world, evaluation=None)
    run_batch(client, b["id"])
    assert client.post(f"/api/batches/{b['id']}/finalize").json()["status"] == "finalized"
    assert ev_detail(client, b["id"])["evaluation"] is None
    r = client.post(f"/api/batches/{b['id']}/evaluate", json={})
    assert r.status_code == 200, r.text
    settle(client)
    assert len(eng.judged) == 3
    r = client.post(f"/api/batches/{b['id']}/evaluate", json={"only_missing": True})
    assert r.status_code == 200
    settle(client)
    assert len(eng.judged) == 3  # nothing re-judged
    detail = ev_detail(client, b["id"])
    assert len(detail["passes"]) == 2 and detail["evaluation"]["counts"]["scored"] == 3
    r = client.post(f"/api/batches/{b['id']}/evaluate", json={"only_missing": False, "judge_model": "anthropic/claude-opus-5"})
    assert r.status_code == 200
    settle(client)
    assert len(eng.judged) == 6
    assert client.post(f"/api/batches/{b['id']}/evaluate", json={"judge_model": "nope/nothing"}).status_code == 422


def test_a_running_pass_blocks_a_second_and_can_be_cancelled(client, world, eng):
    eng.judge_mode = "hang"
    b = make(client, world, evaluation=None)
    run_batch(client, b["id"])
    assert client.post(f"/api/batches/{b['id']}/evaluate", json={}).status_code == 200
    assert client.post(f"/api/batches/{b['id']}/evaluate", json={}).status_code == 409
    client.portal.call(asyncio.sleep, 0.5)  # let the first judges start
    r = client.post(f"/api/batches/{b['id']}/evaluation/cancel")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    settle(client)
    counts = ev_detail(client, b["id"])["evaluation"]["counts"]
    assert counts["skipped"] == 3 and counts["scored"] == 0
    assert 1 <= len(eng.cancelled) <= 2
    assert client.post(f"/api/batches/{b['id']}/evaluation/cancel").status_code == 409


def test_spot_checks_drive_agreement(client, world, eng):
    b = make(client, world)
    d = run_batch(client, b["id"])
    t1, t2, t3 = [t["id"] for t in d["targets"]]
    url = lambda t: f"/api/batches/{b['id']}/targets/{t}/evaluation/review"  # noqa: E731
    assert client.post(url(t1), json={"verdict": "agree"}).json()["review"]["verdict"] == "agree"
    assert client.post(url(t2), json={"verdict": "adjust", "adjusted_quality": 70}).status_code == 200  # within 15
    assert client.post(url(t3), json={"verdict": "adjust"}).status_code == 422
    r = client.post(url(t3), json={"verdict": "disagree", "note": "wrong"})
    assert r.json()["review"]["by_name"] == "Rex Reviewer"
    summary = ev_detail(client, b["id"])["evaluation"]
    assert summary["spot_checks"] == 3 and summary["agreement"] == pytest.approx(2 / 3, abs=1e-3)


def test_review_needs_a_scored_row(client, world, eng):
    eng.judge_mode = "silent"
    b = make(client, world)
    d = run_batch(client, b["id"])
    r = client.post(f"/api/batches/{b['id']}/targets/{d['targets'][0]['id']}/evaluation/review", json={"verdict": "agree"})
    assert r.status_code == 409


# ── records, cost roll-up, seeding ───────────────────────────────────────────
def test_records_carry_scores_only_when_there_is_a_pass(client, world, eng):
    plain = make(client, world, slug="plain", evaluation=None)
    run_batch(client, plain["id"])
    records = client.get(f"/api/batches/{plain['id']}/records").json()
    assert all("evaluation" not in r for r in records["results"]) and "evaluation" not in records["manifest"]
    assert "| Score |" not in records["results_summary"]
    scored = make(client, world, slug="scored")
    run_batch(client, scored["id"])
    records = client.get(f"/api/batches/{scored['id']}/records").json()
    assert records["results"][0]["evaluation"]["quality"] == 75.0
    assert records["manifest"]["evaluation"]["scored"] == 3 and records["manifest"]["evaluation"]["rubric_key"] == "prompt-default-v1"
    assert "| Score |" in records["results_summary"]


def test_the_parent_run_is_charged_for_the_evaluation_too(client, world, eng):
    b = make(client, world)
    run_batch(client, b["id"])

    async def go(db):
        parent = Run(id=uuid.uuid4(), project_id=world.project.id, harness_id=world.harness.id,
                     task_type="chat", task_input={"message": "x"}, document_ids=[], status="completed")
        db.add(parent)
        await db.flush()
        batch = await db.get(Batch, uuid.UUID(b["id"]))
        batch.parent_run_id = parent.id
        await db.commit()
        await core.rollup_parent_cost(db, batch)
        await db.refresh(parent)
        return float(parent.delegated_cost_usd)

    assert run_db(client, world, go) == pytest.approx(0.0411 + 0.08)


def test_the_evaluator_harness_and_its_tools(client, world, eng):
    tools = get_builtin_tools()
    assert "submit_evaluation" in tools and "submit_batch_review" in tools
    assert set(EVALUATOR_HARNESS_TOOLS) == {"read_document", "search_documents", "submit_evaluation", "submit_batch_review", "submit_pairwise"}
    assert not {"submit_evaluation", "submit_batch_review"} & set(COLLECTION_HARNESS_TOOLS)
    assert "Finish by calling submit_evaluation exactly once" in EVALUATOR_DOCTRINE

    async def go(db):
        a = await ensure_evaluator_harness(db, world.ws.id)
        b = await ensure_evaluator_harness(db, world.ws.id)
        a.tool_names = ["read_document"]
        c = await ensure_evaluator_harness(db, world.ws.id)
        return a.id == b.id == c.id, c.tool_names, a.task_profile

    same, names, profile = run_db(client, world, go)
    assert same and names == ["read_document", "search_documents", "submit_evaluation", "submit_batch_review", "submit_pairwise"]
    assert profile == "freeform"


def test_custom_rubrics_are_validated(client, world, eng):
    rubric = {
        "criteria": [
            {"key": "tone", "label": "Tone", "weight": 50, "applies": "all", "five": "Warm", "one": "Cold"},
            {"key": "length", "label": "Length", "weight": 50, "applies": "all", "five": "Short", "one": "Long"},
        ],
        "flags": [{"key": "rude", "label": "Rude"}],
    }
    b = make(client, world, slug="custom", evaluation={"enabled": True, "rubric": rubric})
    assert b["settings"]["evaluation"]["rubric_key"].startswith("prompt-custom-")
    r = client.post("/api/batches", json={**body(world, run_slug="bad"), "evaluation": {"enabled": True, "rubric": {"criteria": rubric["criteria"][:1]}}})
    assert r.status_code == 422
    r = client.post("/api/batches", json={**body(world, run_slug="bad2"), "evaluation": {"enabled": True, "judge_model": "x/y"}})
    assert r.status_code == 422 and "unknown judge model" in r.json()["detail"]
    assert json.loads(json.dumps(b["settings"]["evaluation"]["rubric"]))["version"]


# ── review fixes ─────────────────────────────────────────────────────────────
def _db(client, world, fn):
    return run_db(client, world, fn)


def test_a_pass_started_over_unsettled_targets_completes_when_they_settle(client, world, eng):
    eng.behaviour["row-1"] = "finding"
    eng.behaviour["row-2"] = "finding"
    b = make(client, world, evaluation=None, review_schema="connected_write")
    d = run_batch(client, b["id"])
    assert [t["status"] for t in d["targets"]] == ["review", "review", "completed"]
    assert client.post(f"/api/batches/{b['id']}/evaluate", json={}).status_code == 200
    settle(client)
    detail = ev_detail(client, b["id"])
    assert detail["evaluation"]["status"] == "running" and detail["evaluation"]["counts"]["scored"] == 1
    for f in d["pending_reviews"]:
        r = client.post(f"/api/findings/{f['id']}/approval", json={"action": "approve", "note": None})
        assert r.status_code == 200, r.text
    settle(client)
    detail = ev_detail(client, b["id"])
    assert detail["evaluation"]["status"] == "completed" and detail["evaluation"]["counts"]["scored"] == 3
    assert detail["evaluation"]["summary"]["lessons"] == ["Keep it up"]


def test_a_pending_row_on_a_target_that_settled_outside_the_hook_is_rearmed(client, world, eng):
    b = make(client, world)
    run_batch(client, b["id"])
    ev, rows = rows_of(client, world, b["id"])

    async def stale(db):
        e = await db.get(BatchEvaluation, ev.id)
        e.status, e.finished_at = "running", None
        r = await db.get(TargetEvaluation, rows[0].id)
        r.status, r.attempt, r.scores, r.quality = "pending", 99, {}, None
        await db.commit()

    _db(client, world, stale)
    client.portal.call(evaluation._pump, ev.id)
    settle(client)
    ev2, rows2 = rows_of(client, world, b["id"])
    assert ev2.status == "completed" and all(r.status == "scored" for r in rows2)
    assert next(r for r in rows2 if r.id == rows[0].id).attempt == 1


def test_forged_task_input_cannot_write_a_review_or_a_collection_outcome(client, world, eng):
    from tret.engine.collection_tools import report_collection_outcome

    b = make(client, world)
    d = run_batch(client, b["id"])
    ev, _ = rows_of(client, world, b["id"])

    async def forge(db):
        run = Run(
            id=uuid.uuid4(), project_id=world.project.id, harness_id=world.harness.id, task_type="freeform",
            task_input={"message": "x", "_batch_evaluation_id": str(ev.id), "_batch_review": True,
                        "_collection_target_id": d["targets"][0]["id"]},
            document_ids=[], status="running",
        )
        db.add(run)
        await db.commit()
        ctx = RunContext(db=db, run_id=run.id, project_id=world.project.id, pack_id=None, doctrine_sha=None,
                         model_used=None, document_ids=[], output_schemas={}, workspace_id=world.ws.id)
        with pytest.raises(ToolError):
            await submit_batch_review(ctx, narrative="overwritten")
        with pytest.raises(ToolError):
            await report_collection_outcome(ctx, status="gap", summary="forged")
        stored = await db.get(BatchEvaluation, ev.id, populate_existing=True)
        return stored.summary["narrative"]

    assert _db(client, world, forge) == "Scores are consistent."


@pytest.mark.parametrize(
    "error",
    [
        "max_iterations (40) reached without completion",
        "cost_cap_exceeded: run cost $0.43 >= cap $0.50",
        "output_budget_exceeded: 4096 output tokens",
        "stopped: 3 identical failing calls to propose_package_map",
    ],
)
def test_run_limit_errors_do_not_leak_into_the_pack(client, world, eng, error):
    eng.errors = {"row-1": f"[secretprovider] {error}"}
    eng.tool_error_text = {"row-1": f"Tool error: {error} after 12 iterations"}
    b = make(client, world)
    run_batch(client, b["id"])
    pack = eng.packs["row-1"].lower()
    for leak in ("max_iterations", "cost_cap", "output_budget", "$0.43", "$0.50", "4096", "40)", "12 iterations", "secretprovider"):
        assert leak not in pack, leak
    assert "run limit" in pack


def test_cancel_books_the_spend_of_runs_in_flight(client, world, eng):
    eng.judge_mode = "hang"
    b = make(client, world, evaluation=None)
    run_batch(client, b["id"])
    assert client.post(f"/api/batches/{b['id']}/evaluate", json={}).status_code == 200
    client.portal.call(asyncio.sleep, 0.5)
    assert client.post(f"/api/batches/{b['id']}/evaluation/cancel").status_code == 200
    settle(client)
    ev, rows = rows_of(client, world, b["id"])
    assert float(ev.cost_usd) == pytest.approx(0.02 * len(eng.cancelled)) and len(eng.cancelled) >= 1
    assert len(ev.booked_runs) == len(eng.cancelled)
    # a judge arriving late is refused: the row no longer names its run
    late = eng.cancelled[0]

    async def go(db):
        ctx = RunContext(db=db, run_id=late, project_id=world.project.id, pack_id=None, doctrine_sha=None,
                         model_used=None, document_ids=[], output_schemas={}, workspace_id=world.ws.id)
        with pytest.raises(ToolError):
            await submit_evaluation(
                ctx, scores={k: {"score": 5, "evidence": "x"} for k in CRITERIA},
                difficulty=1, confidence="low", rationale="late",
            )
        return all(r.evaluator_run_id is None for r in (await db.execute(select(TargetEvaluation))).scalars())

    assert _db(client, world, go)


def test_the_restart_sweep_books_the_spend_of_interrupted_runs(client, world, eng):
    b = make(client, world)
    run_batch(client, b["id"])
    ev, rows = rows_of(client, world, b["id"])

    async def interrupt(db):
        e = await db.get(BatchEvaluation, ev.id)
        e.status, e.cost_usd, e.booked_runs = "running", Decimal(0), []
        r = await db.get(TargetEvaluation, rows[0].id)
        r.status = "running"
        await db.commit()

    _db(client, world, interrupt)

    async def sweep(db):
        return await evaluation.sweep_orphaned_evaluations(db)

    assert _db(client, world, sweep) == 1
    settle(client)
    ev2, rows2 = rows_of(client, world, b["id"])
    # every judge run of the pass is booked once (a process that died after
    # `submit_evaluation` leaves scored rows unbooked too), plus the review the pump reran
    assert float(ev2.cost_usd) == pytest.approx(0.08)
    row = next(r for r in rows2 if r.id == rows[0].id)
    assert row.status == "failed" and row.error == "interrupted by a restart"
    assert _db(client, world, sweep) == 0


def test_only_one_pass_can_run_at_a_time(client, world, eng):
    from sqlalchemy.exc import IntegrityError

    eng.judge_mode = "hang"
    b = make(client, world, evaluation=None)
    run_batch(client, b["id"])

    async def start(db_factory):
        async with db_factory() as db:
            batch = await db.get(Batch, uuid.UUID(b["id"]))
            try:
                return (await evaluation.start_evaluation(db, batch, user_id=world.user.id)).id
            except Exception as e:  # noqa: BLE001
                return getattr(e, "status_code", repr(e))

    async def both():
        return await asyncio.gather(start(world.factory), start(world.factory))

    results = client.portal.call(both)
    assert sorted(type(r).__name__ for r in results) == ["UUID", "int"] and 409 in results
    eng.judge_mode = "ok"

    async def duplicate(db):
        existing = (await db.execute(select(BatchEvaluation))).scalars().first()
        db.add(BatchEvaluation(
            id=uuid.uuid4(), collection_id=existing.collection_id, workspace_id=existing.workspace_id,
            family_key="f", rubric_key="r", rubric_version="v", rubric={}, judge_model="j", status="running",
        ))
        with pytest.raises(IntegrityError):
            await db.commit()

    _db(client, world, duplicate)
    client.post(f"/api/batches/{b['id']}/evaluation/cancel")
    settle(client)


def test_a_skipped_retry_does_not_reopen_a_finished_pass(client, world, eng):
    eng.errors = {"row-2": "OSError: [Errno 28] No space left on device"}
    b = make(client, world)
    d = run_batch(client, b["id"])
    reviews = eng.review_calls
    target = next(t for t in d["targets"] if t["slug"] == "row-2")
    assert client.post(f"/api/batches/{b['id']}/targets/{target['id']}/retry").status_code == 200
    settle(client)
    detail = ev_detail(client, b["id"])
    assert detail["evaluation"]["status"] == "completed" and eng.review_calls == reviews
    row = next(t for t in detail["targets"] if t["target_id"] == target["id"])
    assert row["status"] == "skipped" and row["attempt"] == 2


def test_a_draining_process_leaves_a_finished_pass_running(client, world, eng, monkeypatch):
    b = make(client, world)
    run_batch(client, b["id"])
    ev, _ = rows_of(client, world, b["id"])

    async def reopen(db):
        e = await db.get(BatchEvaluation, ev.id)
        e.status, e.finished_at = "running", None
        await db.commit()

    _db(client, world, reopen)
    monkeypatch.setattr(core, "draining", lambda: True)
    client.portal.call(evaluation._pump, ev.id)
    assert rows_of(client, world, b["id"])[0].status == "running"
    monkeypatch.setattr(core, "draining", lambda: False)
    client.portal.call(evaluation._pump, ev.id)
    settle(client)
    assert rows_of(client, world, b["id"])[0].status == "completed"


# ── judge availability ───────────────────────────────────────────────────────
OPUS = "anthropic/claude-opus-5-5"
FALLBACK = "openrouter/google/gemini-3.8-flash"


def test_the_judge_falls_back_to_the_next_preference_with_a_key(client, world, eng):
    eng.keyed = {"openrouter"}
    b = make(client, world)
    d = run_batch(client, b["id"])
    assert set(eng.overrides) == {FALLBACK} and d["evaluation_status"] == "completed"
    assert ev_detail(client, b["id"])["evaluation"]["judge_model"] == FALLBACK
    assert d["settings"]["evaluation"]["judge_model"] is None
    eng.keyed = {"anthropic", "openrouter"}  # a later Re-evaluate re-resolves
    assert client.post(f"/api/batches/{b['id']}/evaluate", json={"only_missing": False}).json()["judge_model"] == OPUS
    settle(client)


def test_an_unavailable_explicit_judge_is_a_422(client, world, eng):
    eng.keyed = {"openrouter"}
    r = client.post("/api/batches", json={**body(world, run_slug="batch-x1"), "evaluation": {"enabled": True, "judge_model": OPUS}})
    assert r.status_code == 200  # the creator's choice is checked when the pass starts
    plain = make(client, world, slug="batch-x2", evaluation=None)
    run_batch(client, plain["id"])
    r = client.post(f"/api/batches/{plain['id']}/evaluate", json={"judge_model": OPUS})
    assert r.status_code == 422
    assert r.json()["detail"] == f"Judge model '{OPUS}' is not available here: provider 'anthropic' has no API key."


def test_no_available_judge_is_a_422(client, world, eng):
    eng.keyed = set()
    plain = make(client, world, evaluation=None)
    run_batch(client, plain["id"])
    r = client.post(f"/api/batches/{plain['id']}/evaluate", json={})
    assert r.status_code == 422
    assert r.json()["detail"] == (
        "No judge model is available: set TRET_BATCH_EVALUATOR_JUDGE to a model whose provider has a key."
    )


def test_the_auto_pass_records_why_it_could_not_judge_and_the_batch_still_runs(client, world, eng):
    eng.keyed = set()
    b = make(client, world)
    d = run_batch(client, b["id"])
    assert d["status"] == "completed" and d["evaluation_status"] == "failed" and eng.judged == []
    detail = ev_detail(client, b["id"])
    assert detail["evaluation"]["status"] == "failed" and "No judge model is available" in detail["evaluation"]["error"]
    assert detail["targets"] == []


def test_a_judge_that_answers_in_text_is_relaunched_once_and_the_cost_counts(client, world, eng):
    eng.judge_mode = "text_first"
    b = make(client, world)
    d = run_batch(client, b["id"])
    detail = ev_detail(client, b["id"])
    assert detail["evaluation"]["counts"]["scored"] == 3 and d["quality_mean"] == 75.0
    assert len(eng.judged) == 6 and sorted(set(eng.judged)) == ["row-1", "row-2", "row-3"]
    assert all("Call submit_evaluation now" in m for m in [eng.packs[k] for k in ("row-1", "row-2", "row-3")])
    # six judge runs and the batch review, every one booked
    assert d["evaluation_cost_usd"] == pytest.approx(0.02 * 7)
    first = next(t for t in detail["targets"] if t["label"] == "Acme Ltd")
    assert first["cost_usd"] == pytest.approx(0.04)


def test_a_judge_that_runs_out_of_turns_is_relaunched_once_to_score_from_the_pack(client, world, eng):
    eng.judge_mode = "loop_first"
    b = make(client, world)
    d = run_batch(client, b["id"])
    detail = ev_detail(client, b["id"])
    assert detail["evaluation"]["counts"]["scored"] == 3 and detail["evaluation"]["counts"]["failed"] == 0
    assert len(eng.judged) == 6 and sorted(set(eng.judged)) == ["row-1", "row-2", "row-3"]
    retry = eng.packs["row-1"]
    assert "used all its turns and recorded no score" in retry and "Call submit_evaluation now" not in retry
    assert eng.iteration_caps["row-1"] == [evaluation.EVALUATOR_MAX_ITERATIONS, evaluation.RELAUNCH_MAX_ITERATIONS]
    assert d["evaluation_cost_usd"] == pytest.approx(0.02 * 7)


def test_a_judge_stopped_for_a_stuck_call_is_relaunched_once_to_score_from_the_pack(client, world, eng):
    """The engine now stops a judge that repeats one failing call (it used to run on
    to its turn limit and get the out-of-turns relaunch); it still gets one relaunch."""
    eng.judge_mode = "stuck_first"
    b = make(client, world)
    d = run_batch(client, b["id"])
    detail = ev_detail(client, b["id"])
    assert detail["evaluation"]["counts"]["scored"] == 3 and detail["evaluation"]["counts"]["failed"] == 0
    assert len(eng.judged) == 6
    retry = eng.packs["row-1"]
    assert "kept repeating a tool call that failed the same way" in retry
    assert "used all its turns" not in retry and "Call submit_evaluation now" not in retry
    assert eng.iteration_caps["row-1"] == [evaluation.EVALUATOR_MAX_ITERATIONS, evaluation.RELAUNCH_MAX_ITERATIONS]
    assert d["evaluation_cost_usd"] == pytest.approx(0.02 * 7)


def test_a_judge_that_runs_out_of_turns_twice_fails_the_row(client, world, eng):
    eng.judge_mode = "loop_first"
    real = eng._judge

    async def always_loops(run_id, ti):
        if ti.get("_batch_review"):
            return await real(run_id, ti)
        eng.judged.append(ti["_label"].split(":", 1)[1])
        async with eng.factory() as db:
            run = await db.get(Run, run_id)
            run.status, run.error = "failed", "max_iterations (8) reached without completion"
            run.cost_usd = Decimal(eng.judge_cost)
            await db.commit()

    eng._judge = always_loops
    b = make(client, world)
    run_batch(client, b["id"])
    counts = ev_detail(client, b["id"])["evaluation"]["counts"]
    assert len(eng.judged) == 6 and counts["failed"] == 3 and counts["scored"] == 0, counts


def test_the_pack_caps_document_reads_when_documents_are_attached():
    from tret.batches.evidence import attached_documents

    class Doc:
        id, filename = "d1", "report.pdf"

    text = attached_documents([(Doc(), "captured source")])
    assert text.startswith("- d1: report.pdf (captured source)")
    assert "at most three reads or searches" in text and "call submit_evaluation" in text


# ── second review round ──────────────────────────────────────────────────────
def test_a_manual_pass_on_a_draft_batch_does_not_break_its_start(client, world, eng):
    b = make(client, world)  # evaluation enabled: the start would create the auto pass
    assert client.post(f"/api/batches/{b['id']}/evaluate", json={}).status_code == 200
    r = client.post(f"/api/batches/{b['id']}/start")
    assert r.status_code == 200, r.text
    settle(client)
    d = client.get(f"/api/batches/{b['id']}").json()
    assert d["status"] == "completed" and all(t["status"] == "completed" for t in d["targets"])
    detail = ev_detail(client, b["id"])
    assert len(detail["passes"]) == 1 and detail["evaluation"]["status"] == "completed"
    assert detail["evaluation"]["counts"]["scored"] == 3


def test_a_failing_auto_pass_never_breaks_the_start(client, world, eng, monkeypatch):
    async def boom(*a, **k):
        raise HTTPException(409, "boom")

    monkeypatch.setattr(evaluation, "start_evaluation", boom)
    b = make(client, world)
    d = run_batch(client, b["id"])
    assert d["status"] == "completed" and d["evaluation_status"] is None


def test_the_text_relaunch_respects_the_budget(client, world, eng, monkeypatch):
    monkeypatch.setattr(get_settings(), "batch_evaluator_concurrency", 1)
    eng.judge_mode = "text_first"
    per = evaluation.estimate_evaluation_cost("anthropic/claude-opus-5-5", 1)
    eng.judge_cost = str(round(per, 6))
    b = make(client, world, evaluation={"enabled": True, "budget_usd": round(per * 1.5, 6)})
    run_batch(client, b["id"])
    counts = ev_detail(client, b["id"])["evaluation"]["counts"]
    assert len(eng.judged) == 1 and counts["not_evaluated_budget"] == 3 and counts["failed"] == 0, counts


def test_a_finished_run_does_not_overwrite_a_row_that_moved_on(client, world, eng):
    b = make(client, world)
    run_batch(client, b["id"])
    ev, rows = rows_of(client, world, b["id"])
    row = rows[0]
    run_id = row.evaluator_run_id

    async def rearmed(db):  # a retry re-armed the row while the run was finishing
        r = await db.get(TargetEvaluation, row.id)
        r.status, r.evaluator_run_id = "pending", None
        await db.commit()

    _db(client, world, rearmed)
    before = _db(client, world, lambda db: db.scalar(select(__import__("sqlalchemy").func.count(Run.id))))
    assert client.portal.call(evaluation._after_evaluator_run, ev.id, row.id, run_id) is None
    r = next(x for x in rows_of(client, world, b["id"])[1] if x.id == row.id)
    assert r.status == "pending" and r.evaluator_run_id is None

    async def stale(db):  # the row still names the run, but the target is on a newer attempt
        x = await db.get(TargetEvaluation, row.id)
        x.status, x.evaluator_run_id, x.attempt = "running", run_id, 99
        await db.commit()

    _db(client, world, stale)
    assert client.portal.call(evaluation._after_evaluator_run, ev.id, row.id, run_id) is None
    r = next(x for x in rows_of(client, world, b["id"])[1] if x.id == row.id)
    assert r.status == "pending" and r.evaluator_run_id is None and r.attempt == 1
    after = _db(client, world, lambda db: db.scalar(select(__import__("sqlalchemy").func.count(Run.id))))
    assert after == before  # no relaunch carried the old attempt's evidence


def test_a_finished_pass_drops_its_in_memory_state(client, world, eng):
    b = make(client, world)
    run_batch(client, b["id"])
    ev, _ = rows_of(client, world, b["id"])
    assert ev.id not in evaluation._loop_state["locks"] and ev.id not in evaluation._loop_state["unbooked"]


def test_cancel_also_cancels_a_batch_review_in_flight(client, world, eng):
    eng.hang_review = True
    b = make(client, world)
    assert client.post(f"/api/batches/{b['id']}/start").status_code == 200
    client.portal.call(asyncio.sleep, 1.0)  # judges done, the review is hanging
    ev, _ = rows_of(client, world, b["id"])
    assert ev.status == "running" and ev.summary_run_id is not None
    assert client.post(f"/api/batches/{b['id']}/evaluation/cancel").status_code == 200
    settle(client)
    assert ev.summary_run_id in eng.cancelled
