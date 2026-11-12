"""Earlier runs that hit a search outage: candidates from the stored transcript,
marking only what an operator names, the evaluation of a target judged before it
was marked, and `tret batch search-outages`."""
# ruff: noqa: F811 - the imported fixtures are used by name as test arguments
from __future__ import annotations

import sys
import uuid

import pytest
from sqlalchemy import select

from tests.test_batch_evaluation import eng, make, run_batch  # noqa: F401 - fixtures
from tests.test_collections_api import client, run_db, world  # noqa: F401 - fixtures
from tret import cli
from tret.batches import core, scoring, search_outage
from tret.batches.evaluation import INFRA_SKIP
from tret.db import engine as engine_mod
from tret.db.models import Batch, BatchEvaluation, BatchTarget, Run, RunOutcome, TargetEvaluation
from tret.services import outcomes as outcomes_service
from tret.services.outcomes import record_outcome

NOTHING = "No web results for 'q'."
DEGRADED = (
    "No web results for 'q'. 4 of the search backend's engines did not respond (brave: timeout), "
    "so the search backend may be unavailable."
)
FOUND = "Web results for 'q' (via searxng):\n\n1. T\n   https://x.example/\n   s"
LIMIT = (
    "This run has already made 12 web searches, which is the per-run limit "
    "(TRET_EGRESS_RESEARCH_MAX_SEARCHES_PER_RUN=12). Stop searching."
)
FAILED = "SearXNG search failed: ConnectError"
NOT_CONFIGURED = "No web search backend is configured. Set TRET_SEARCH_PROVIDER."


def transcript(*results, other_tool_nothing=0):
    """Assistant/tool message pairs as `Msg.to_json` stores them: a result is
    (content, is_error). A `fetch_url` result that starts like an empty search
    is added `other_tool_nothing` times and must never count."""
    msgs = [{"role": "user", "content": "go", "tool_calls": [], "tool_call_id": None, "meta": {}}]
    for i, (content, is_error) in enumerate(results):
        msgs.append({"role": "assistant", "content": None, "tool_call_id": None, "meta": {},
                     "tool_calls": [{"id": f"s{i}", "name": "web_search", "arguments": {"query": "q"}}]})
        msgs.append({"role": "tool", "content": content, "tool_call_id": f"s{i}", "meta": {"error": is_error}})
    for i in range(other_tool_nothing):
        msgs.append({"role": "assistant", "content": None, "tool_call_id": None, "meta": {},
                     "tool_calls": [{"id": f"f{i}", "name": "fetch_url", "arguments": {}}]})
        msgs.append({"role": "tool", "content": NOTHING, "tool_call_id": f"f{i}", "meta": {"error": False}})
    return msgs


def test_classification():
    c = search_outage.classify_search_result
    assert c(FOUND, False) == "found" and c(NOTHING, False) == "nothing" and c(DEGRADED, False) == "nothing"
    assert c(FAILED, True) == "error"
    assert c("Search backend is unavailable right now", True) == "error"
    assert c(LIMIT, True) is None and c(NOT_CONFIGURED, True) is None
    assert c("Web access is switched off for this deployment", True) is None
    assert c("some other tool error", True) is None


def test_counts_tie_results_to_web_search_by_call_id():
    n = search_outage.transcript_search_counts(
        transcript((NOTHING, False), (FAILED, True), (LIMIT, True), other_tool_nothing=5)
    )
    assert n == {"searches": 2, "found": 0, "nothing": 1, "error": 1}
    assert search_outage.transcript_search_counts(None)["searches"] == 0


# ── a batch with one target per case ─────────────────────────────────────────
CASES = {
    "row-1": transcript(*[(NOTHING, False)] * 12),                       # candidate
    "row-2": transcript(*[(NOTHING, False)] * 11, (FOUND, False)),       # a search found results
    "row-3": transcript((NOTHING, False), *[(LIMIT, True)] * 9),         # one search (limit refusals ignored)
}


def _arm(client, world, messages_by_slug, *, evaluation=True, status="gap"):
    """A finished batch whose targets are turned into gaps with the given transcripts."""
    from tests.test_batch_evaluation import EVAL_ON

    b = make(client, world, evaluation=EVAL_ON if evaluation else None)
    d = run_batch(client, b["id"])
    ids = {t["slug"]: t["id"] for t in d["targets"]}

    async def go(db):
        runs = {}
        for slug, tid in ids.items():
            t = await db.get(BatchTarget, uuid.UUID(tid))
            run = await db.get(Run, t.run_id)
            run.messages = messages_by_slug.get(slug, [])
            t.status = status
            t.outcome = {"status": "blocked", "summary": "search was down " + "x" * 200}
            runs[slug] = str(run.id)
        await db.commit()
        return runs

    return b["id"], ids, run_db(client, world, go)


def _read(client, world, tid):
    async def go(db):
        t = await db.get(BatchTarget, uuid.UUID(tid), populate_existing=True)
        return t.status, t.error

    return run_db(client, world, go)


def _candidates(client, world, bid):
    async def go(db):
        return await search_outage.transcript_candidates(db, [bid, bid])

    return run_db(client, world, go)


def _mark(client, world, bid, tids, dry_run):
    async def go(db):
        return await search_outage.mark_transcript_outages(db, [bid], tids, dry_run=dry_run)

    return run_db(client, world, go)


@pytest.fixture(autouse=True)
def _no_priors(monkeypatch):
    monkeypatch.setattr(outcomes_service, "_invalidate_priors", lambda: None)


def test_candidates(client, world, eng):
    bid, ids, runs = _arm(client, world, CASES)
    got = _candidates(client, world, bid)
    assert [c["target_id"] for c in got] == [ids["row-1"]]
    c = got[0]
    assert (c["batch_id"], c["status"], c["run_id"]) == (bid, "gap", runs["row-1"])
    assert (c["searches"], c["nothing"], c["error"]) == (12, 12, 0)
    assert c["label"] and c["model"] and c["summary"].startswith("search was down") and len(c["summary"]) == 120


def test_mixed_nothing_and_error_counts_and_unfound_batches(client, world, eng):
    cases = {"row-1": transcript((NOTHING, False), (FAILED, True), (DEGRADED, False))}
    bid, ids, _ = _arm(client, world, cases)
    (c,) = _candidates(client, world, bid)
    assert (c["searches"], c["nothing"], c["error"]) == (3, 2, 1)

    async def go(db):
        return await search_outage.transcript_candidates(db, [uuid.uuid4()])

    assert run_db(client, world, go) == []


def test_a_target_that_delivered_or_is_marked_or_not_a_gap_is_not_a_candidate(client, world, eng, monkeypatch):
    every = {s: transcript(*[(NOTHING, False)] * 4) for s in ("row-1", "row-2", "row-3")}
    bid, ids, runs = _arm(client, world, every)

    async def tweak(db):
        t2 = await db.get(BatchTarget, uuid.UUID(ids["row-2"]))
        t2.error = search_outage.outage_error({"calls": 3, "engines": []})
        t3 = await db.get(BatchTarget, uuid.UUID(ids["row-3"]))
        t3.status = "completed"
        await db.commit()

    run_db(client, world, tweak)
    real = core.load_review_findings

    async def delivered(db, profile, targets, batch=None):
        out = await real(db, profile, targets, batch)
        out[uuid.UUID(runs["row-1"])] = [object()]  # a package map
        return out

    monkeypatch.setattr(core, "load_review_findings", delivered)
    assert _candidates(client, world, bid) == []
    res = _mark(client, world, bid, list(ids.values()), True)
    reasons = {r["target_id"]: r["reason"] for r in res["refused"]}
    assert "delivered something" in reasons[ids["row-1"]]
    assert ids["row-2"] not in reasons and [r["target_id"] for r in res["rechecked"]] == [ids["row-2"]]
    assert "not a gap or failed" in reasons[ids["row-3"]]


# ── marking ──────────────────────────────────────────────────────────────────
def test_dry_run_changes_nothing(client, world, eng):
    bid, ids, _ = _arm(client, world, CASES)
    res = _mark(client, world, bid, [ids["row-1"]], True)
    assert res["dry_run"] and [m["target_id"] for m in res["marked"]] == [ids["row-1"]] and not res["refused"]
    assert _read(client, world, ids["row-1"]) == ("gap", None)


def test_apply_marks_only_what_is_named_and_refuses_the_rest(client, world, eng):
    bid, ids, _ = _arm(client, world, {**CASES, "row-3": CASES["row-1"]})
    stranger = str(uuid.uuid4())
    res = _mark(client, world, bid, [ids["row-1"], ids["row-2"], stranger, ids["row-1"]], False)
    assert [m["target_id"] for m in res["marked"]] == [ids["row-1"]]
    reasons = {r["target_id"]: r["reason"] for r in res["refused"]}
    assert reasons == {ids["row-2"]: "a search found results", stranger: "not in these batches"}
    status, error = _read(client, world, ids["row-1"])
    assert status == "gap"
    assert error.startswith(search_outage.SEARCH_OUTAGE_PREFIX) and "12 of 12" in error and "afterwards" in error
    assert _read(client, world, ids["row-2"]) == ("gap", None)
    assert _read(client, world, ids["row-3"]) == ("gap", None)  # a candidate nobody named

    again = _mark(client, world, bid, [ids["row-1"]], False)
    assert not again["marked"] and not again["refused"] and len(again["rechecked"]) == 1
    assert _read(client, world, ids["row-1"]) == ("gap", error)


def test_marking_keeps_the_previous_error_and_the_class_and_counter_follow(client, world, eng):
    bid, ids, _ = _arm(client, world, CASES)

    async def err(db):
        (await db.get(BatchTarget, uuid.UUID(ids["row-1"]))).error = "provider said no"
        await db.commit()

    run_db(client, world, err)
    _mark(client, world, bid, [ids["row-1"]], False)

    async def go(db):
        c = await db.get(Batch, uuid.UUID(bid))
        targets = await core.load_targets(db, [c.id])
        return core.collection_out(c, targets, {})["search_outage_targets"], {t.slug: scoring.target_class(t) for t in targets}, targets[0].error

    n, classes, error = run_db(client, world, go)
    assert n == 1 and classes["row-1"] == "infra" and classes["row-2"] == "gap"
    assert error.endswith("; run error: provider said no")


# ── a target judged before it was marked ─────────────────────────────────────
def test_a_judged_target_stops_counting_once_marked(client, world, eng):
    bid, ids, runs = _arm(client, world, CASES, status="completed")

    async def seed(db):
        for r in runs.values():
            await record_outcome(db, await db.get(Run, uuid.UUID(r)), commit=True)
        t = await db.get(BatchTarget, uuid.UUID(ids["row-1"]))
        t.status = "gap"
        await db.commit()

    run_db(client, world, seed)

    async def snapshot(db):
        outs = {
            s: (await db.get(RunOutcome, (uuid.UUID(r), 0), populate_existing=True)).evaluated_quality_score
            for s, r in runs.items()
        }
        rows = (await db.execute(select(TargetEvaluation).execution_options(populate_existing=True))).scalars().all()
        by = {str(r.target_id): (r.status, r.skip_reason, None if r.quality is None else float(r.quality)) for r in rows}
        return outs, by

    outs_before, rows_before = run_db(client, world, snapshot)
    assert all(v is not None for v in outs_before.values())
    card = client.get(f"/api/batches/{bid}/scorecard").json()
    assert card["quality"]["scored"] == 3

    res = _mark(client, world, bid, [ids["row-1"]], False)
    assert len(res["marked"]) == 1

    outs, rows = run_db(client, world, snapshot)
    assert outs["row-1"] is None
    assert outs["row-2"] == outs_before["row-2"] and outs["row-3"] == outs_before["row-3"]
    assert rows[ids["row-1"]] == ("skipped", INFRA_SKIP, None)  # the state a fresh infra target gets
    assert rows[ids["row-2"]] == rows_before[ids["row-2"]] and rows[ids["row-3"]] == rows_before[ids["row-3"]]
    card = client.get(f"/api/batches/{bid}/scorecard").json()
    assert card["quality"]["scored"] == 2 and card["classes"]["infra"] == 1
    expected = round(sum(v[2] for k, v in rows.items() if v[2] is not None) / 2, 1)
    assert card["quality"]["mean"] == pytest.approx(expected, abs=0.1)

    async def passes(db):
        return (await db.execute(select(BatchEvaluation))).scalars().all()

    assert len(run_db(client, world, passes)) == 1  # the pass is kept, no row deleted
    async def count(db):
        return len((await db.execute(select(TargetEvaluation))).scalars().all())

    assert run_db(client, world, count) == 3


# ── the command ──────────────────────────────────────────────────────────────
def run_cli(monkeypatch, capsys, world, *argv):
    monkeypatch.setattr(engine_mod, "get_session_factory", lambda: world.factory)
    monkeypatch.setattr(sys, "argv", ["tret", "batch", "search-outages", *argv])
    code = 0
    try:
        cli.main()
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
    out = capsys.readouterr()
    return code, out.out, out.err


def test_cli_lists_candidates_and_writes_nothing(client, world, eng, monkeypatch, capsys):
    bid, ids, _ = _arm(client, world, CASES)
    code, out, err = run_cli(monkeypatch, capsys, world, "--batch", bid, "--batch", bid)
    assert code == 0 and ids["row-1"] in out and ids["row-2"] not in out
    assert "1 candidate(s); nothing was written" in out
    assert _read(client, world, ids["row-1"]) == ("gap", None)
    code, out, err = run_cli(monkeypatch, capsys, world, "--batch", str(uuid.uuid4()))
    assert code == 1 and "not found, skipped" in err and "0 candidate(s)" in out


def test_cli_mark_without_apply_is_a_dry_run(client, world, eng, monkeypatch, capsys):
    bid, ids, _ = _arm(client, world, CASES)
    code, out, err = run_cli(monkeypatch, capsys, world, "--batch", bid, "--mark", ids["row-1"])
    assert code == 0 and "would mark 1, would refuse 0" in out and "dry run" in out
    assert _read(client, world, ids["row-1"]) == ("gap", None)
    code, out, err = run_cli(monkeypatch, capsys, world, "--batch", bid, "--mark", ids["row-2"])
    assert code == 1 and "would refuse 1" in out and f"refused {ids['row-2']}: a search found results" in err


def test_cli_apply_marks_and_exits_by_refusals(client, world, eng, monkeypatch, capsys):
    bid, ids, _ = _arm(client, world, CASES)
    code, out, err = run_cli(monkeypatch, capsys, world, "--batch", bid, "--mark", ids["row-1"], "--apply")
    assert code == 0 and f"{bid}: 1 marked, 0 refused" in out and err == ""
    assert _read(client, world, ids["row-1"])[1].startswith(search_outage.SEARCH_OUTAGE_PREFIX)
    code, out, err = run_cli(
        monkeypatch, capsys, world, "--batch", bid, "--mark", ids["row-1"], "--mark", ids["row-2"], "--apply"
    )
    assert code == 1 and "0 marked, 1 refused, 1 already marked and re-checked" in out
    assert "a search found results" in err
    code, out, err = run_cli(monkeypatch, capsys, world, "--batch", bid, "--mark", ids["row-1"], "--apply")
    assert code == 0 and "re-checked" in out and "about 60 seconds" in out
    code, out, err = run_cli(monkeypatch, capsys, world, "--batch", bid, "--mark", "not-a-uuid", "--apply")
    assert code == 1 and "not a valid id" in err


def test_cli_apply_without_mark_is_a_usage_error(client, world, eng, monkeypatch, capsys):
    bid, ids, _ = _arm(client, world, CASES)
    code, out, err = run_cli(monkeypatch, capsys, world, "--batch", bid, "--apply")
    assert code == 2 and "--apply needs at least one --mark" in err and out == ""
    assert _read(client, world, ids["row-1"]) == ("gap", None)


# ── review fixes ─────────────────────────────────────────────────────────────
def test_timed_out_and_infra_failed_targets_are_not_candidates(client, world, eng):
    every = {s: transcript(*[(NOTHING, False)] * 4) for s in ("row-1", "row-2", "row-3")}
    bid, ids, _ = _arm(client, world, every)

    async def tweak(db):
        for slug, err in (("row-1", "timeout"), ("row-2", "timeout after 600s"), ("row-3", "OSError: [Errno 28] No space left")):
            t = await db.get(BatchTarget, uuid.UUID(ids[slug]))
            t.status, t.error = "failed", err
        await db.commit()

    run_db(client, world, tweak)
    assert _candidates(client, world, bid) == []
    res = _mark(client, world, bid, list(ids.values()), False)
    reasons = {r["target_id"]: r["reason"] for r in res["refused"]}
    assert reasons[ids["row-1"]] == "timed out" and reasons[ids["row-2"]] == "timed out"
    assert reasons[ids["row-3"]] == "already an infrastructure failure"
    assert not res["marked"] and _read(client, world, ids["row-1"]) == ("failed", "timeout")


def test_the_no_map_note_is_not_carried_into_the_marker(client, world, eng):
    from tret.batches.profiles.disclosure import NO_MAP_ERROR

    bid, ids, _ = _arm(client, world, CASES)

    async def err(db):
        (await db.get(BatchTarget, uuid.UUID(ids["row-1"]))).error = NO_MAP_ERROR
        await db.commit()

    run_db(client, world, err)
    _mark(client, world, bid, [ids["row-1"]], False)
    error = _read(client, world, ids["row-1"])[1]
    assert error.startswith(search_outage.SEARCH_OUTAGE_PREFIX) and "run error" not in error


def test_a_human_review_is_kept_but_reported(client, world, eng, monkeypatch, capsys):
    bid, ids, runs = _arm(client, world, CASES, status="completed")
    review = {"verdict": "adjust", "adjusted_quality": 40, "note": "checked by hand"}

    async def seed(db):
        t = await db.get(BatchTarget, uuid.UUID(ids["row-1"]))
        t.status = "gap"
        row = (await db.execute(select(TargetEvaluation).where(TargetEvaluation.target_id == t.id))).scalars().one()
        row.review = review
        await db.commit()

    run_db(client, world, seed)
    code, out, err = run_cli(monkeypatch, capsys, world, "--batch", bid, "--mark", ids["row-1"], "--apply")
    assert code == 0 and "no longer counts as a spot check" in out and ids["row-1"] in out

    async def read(db):
        return (
            await db.execute(select(TargetEvaluation).where(TargetEvaluation.target_id == uuid.UUID(ids["row-1"])))
        ).scalars().one()

    row = run_db(client, world, read)
    assert row.status == "skipped" and row.review == review and row.quality is None


def test_a_second_apply_repairs_a_lost_rescore(client, world, eng, monkeypatch, capsys):
    bid, ids, runs = _arm(client, world, CASES, status="completed")

    async def seed(db):
        for r in runs.values():
            await record_outcome(db, await db.get(Run, uuid.UUID(r)), commit=True)
        (await db.get(BatchTarget, uuid.UUID(ids["row-1"]))).status = "gap"
        await db.commit()

    run_db(client, world, seed)
    assert run_cli(monkeypatch, capsys, world, "--batch", bid, "--mark", ids["row-1"], "--apply")[0] == 0

    async def break_it(db):
        row = (await db.execute(select(TargetEvaluation).where(TargetEvaluation.target_id == uuid.UUID(ids["row-1"])))).scalars().one()
        row.status, row.skip_reason, row.quality = "scored", None, 80
        out = await db.get(RunOutcome, (uuid.UUID(runs["row-1"]), 0))
        out.evaluated_quality_score = 0.8
        await db.commit()

    run_db(client, world, break_it)
    code, out, err = run_cli(monkeypatch, capsys, world, "--batch", bid, "--mark", ids["row-1"], "--apply")
    assert code == 0 and "re-checked" in out

    async def read(db):
        row = (await db.execute(select(TargetEvaluation).where(TargetEvaluation.target_id == uuid.UUID(ids["row-1"])).execution_options(populate_existing=True))).scalars().one()
        o = await db.get(RunOutcome, (uuid.UUID(runs["row-1"]), 0), populate_existing=True)
        return row.status, row.skip_reason, o.evaluated_quality_score

    assert run_db(client, world, read) == ("skipped", INFRA_SKIP, None)


def test_compare_leaves_a_marked_target_out(client, world, eng):
    bid, ids, _ = _arm(client, world, CASES, status="completed")
    other = make(client, world, slug="batch-b")
    run_batch(client, other["id"])

    def compared():
        c = client.get(f"/api/batches/compare?a={bid}&b={other['id']}").json()
        return sum(1 for r in c["rows"] if (r.get("delta") or {}).get("quality") is not None)

    assert compared() == 3

    async def gap(db):
        (await db.get(BatchTarget, uuid.UUID(ids["row-1"]))).status = "gap"
        await db.commit()

    run_db(client, world, gap)
    _mark(client, world, bid, [ids["row-1"]], False)
    assert compared() == 2
