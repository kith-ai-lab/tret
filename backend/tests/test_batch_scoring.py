"""Scorecards, classes, outliers, keys, quality arithmetic and comparison
(`tret/batches/scoring.py`, `tret/batches/rubrics.py`). Pure functions are tested
directly; the scorecard and the comparison run through the API on prompt batches
built from the evaluator tests' fixtures."""
# ruff: noqa: F811 - the imported fixtures are used by name as test arguments
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from tests.test_batch_evaluation import EVAL_ON, eng, make, run_batch  # noqa: F401 - fixtures
from tests.test_collections_api import client, world  # noqa: F401 - fixtures
from tret.batches import scoring
from tret.batches.rubrics import (
    RubricError,
    compute_quality,
    default_rubric,
    rubric_version,
    validate_custom_rubric,
)


def T(status, error=None, outcome=None, slug="a", label="A"):
    return SimpleNamespace(status=status, error=error, outcome=outcome, slug=slug, label=label)


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        (T("written"), "success"),
        (T("completed"), "success"),
        (T("gap", outcome={"summary": "nothing"}), "gap"),
        (T("gap", outcome={"summary": "write failed: insufficient storage space"}), "infra"),
        (T("failed", error="OSError: [Errno 28] No space left on device"), "infra"),
        (T("write_failed", error="read-only file system"), "infra"),
        (T("failed", error="timeout"), "timeout"),
        (T("failed", error="timeout: stopped by the limit"), "timeout"),
        (T("failed", error="provider exploded"), "failed"),
        (T("rejected"), "rejected"),
        (T("write_failed", error="upload failed"), "failed"),
        (T("running"), "unsettled"),
        (T("held"), "unsettled"),
        (T("review"), "unsettled"),
    ],
)
def test_target_classes(target, expected):
    assert scoring.target_class(target) == expected


def test_family_and_row_keys():
    d = SimpleNamespace(profile="disclosure", settings={"fiscal_year": 2025}, run_slug="r")
    assert scoring.family_key(d) == "disclosure:fy2025" and scoring.family_label(d) == "Disclosure FY2025"
    p = SimpleNamespace(profile="prompt", settings={"template": "Do {x}", "harness_id": "h1", "harness_name": "H"},
                        harness_id=None, run_slug="r")
    q = SimpleNamespace(**{**p.__dict__, "settings": {**p.settings, "template": "Do {y}"}})
    assert scoring.family_key(p).startswith("prompt:h1:") and scoring.family_key(p) != scoring.family_key(q)
    assert scoring.family_label(p).startswith("Prompt · H · Do {x}")
    t = SimpleNamespace(slug="acme-2", label="Acme Ltd #2")
    assert scoring.row_key(d, t) == "acme-2"
    assert scoring.row_key(p, t) == "acme-ltd"
    ps = SimpleNamespace(**{**p.__dict__, "settings": {**p.settings, "slug_column": "id"}})
    assert scoring.row_key(ps, t) == "acme-2"


def _data(values, metric_key="cost_usd"):
    targets = []
    for i, v in enumerate(values):
        m = {"class": "success", "cost_usd": v, "iterations": None, "max_iterations": None,
             "retained_pages": None, "source_pages": None}
        targets.append(scoring.TargetData(
            target=SimpleNamespace(id=uuid.uuid4(), label=f"T{i}"), run=None, findings=[], metrics=m, row=f"t{i}"))
    return SimpleNamespace(targets=targets)


def test_outliers_need_five_a_spread_and_a_large_z():
    assert scoring.outliers(_data([1, 1, 1, 50])) == []  # n < 5
    assert scoring.outliers(_data([2, 2, 2, 2, 2, 2])) == []  # MAD 0
    found = scoring.outliers(_data([1.0, 1.1, 0.9, 1.05, 0.95, 40.0]))
    assert [o["label"] for o in found] == ["T5"] and found[0]["metric"] == "cost_usd" and found[0]["z"] > 3.5
    infra = _data([1.0, 1.1, 0.9, 1.05, 0.95, 40.0])
    infra.targets[5].metrics["class"] = "infra"
    assert scoring.outliers(infra) == []


def test_bootstrap_is_deterministic_and_needs_three_pairs():
    assert scoring.bootstrap_interval([1.0, 2.0]) is None
    a = scoring.bootstrap_interval([5.0, -2.0, 8.0, 3.0, 1.0])
    assert a == scoring.bootstrap_interval([5.0, -2.0, 8.0, 3.0, 1.0]) and a[0] <= 3.0 <= a[1]


def test_quality_renormalises_over_applicable_scored_criteria():
    rubric = default_rubric("disclosure")
    full = {k: {"score": 5, "evidence": "x"} for k in ("right_document", "coverage", "page_map_precision", "provenance", "gap_honesty")}
    assert compute_quality(rubric, full, "success") == 100.0
    # gap_honesty does not apply to a written result; provenance null drops out too.
    mixed = {**full, "right_document": {"score": 1, "evidence": "x"}, "provenance": {"score": None, "evidence": ""}}
    # weights 30 (0) + 25 (100) + 20 (100) over 75
    assert compute_quality(rubric, mixed, "success") == pytest.approx(60.0)
    # a gap is judged on provenance and gap honesty only: (15*100 + 10*50) / 25
    gap = {**full, "gap_honesty": {"score": 3, "evidence": "x"}}
    assert compute_quality(rubric, gap, "gap") == pytest.approx(80.0)
    assert compute_quality(rubric, {k: {"score": None, "evidence": ""} for k in full}, "success") is None


def test_rubric_validation_and_versions():
    ok = {"criteria": [
        {"key": "a_one", "label": "A", "weight": 10, "applies": "all", "five": "good", "one": "bad"},
        {"key": "b_two", "label": "B", "weight": 20, "applies": "gap", "five": "good", "one": "bad"},
    ]}
    r = validate_custom_rubric(ok)
    assert r["key"] == f"prompt-custom-{r['version']}" and r["acceptable_quality"] == 60
    assert rubric_version(default_rubric("disclosure")) == default_rubric("disclosure")["version"]
    bad = [
        {"criteria": ok["criteria"][:1]},
        {"criteria": [ok["criteria"][0], {**ok["criteria"][1], "key": "a_one"}]},
        {"criteria": [ok["criteria"][0], {**ok["criteria"][1], "weight": 0}]},
        {"criteria": [ok["criteria"][0], {**ok["criteria"][1], "key": "Bad Key"}]},
        {"criteria": [ok["criteria"][0], {**ok["criteria"][1], "applies": "sometimes"}]},
        {"criteria": [ok["criteria"][0], {**ok["criteria"][1], "five": "x" * 301}]},
        {**ok, "acceptable_quality": 101},
        {**ok, "flags": [{"key": f"f_{i}x"} for i in range(16)]},
    ]
    for raw in bad:
        with pytest.raises(RubricError):
            validate_custom_rubric(raw)


# ── through the API ──────────────────────────────────────────────────────────
def test_scorecard_of_a_fixture_batch(client, world, eng):
    eng.errors = {"row-3": "provider exploded"}
    b = make(client, world, evaluation=None)
    run_batch(client, b["id"])
    card = client.get(f"/api/batches/{b['id']}/scorecard").json()
    assert card["targets"] == 3 and card["classes"]["success"] == 2 and card["classes"]["failed"] == 1
    assert card["acceptable"] == 2 and card["yield"] == pytest.approx(2 / 3, abs=1e-3)
    assert card["written_share"] == card["yield"]
    assert card["cost_usd"] == pytest.approx(0.0411) and card["cost_per_acceptable_usd"] == pytest.approx(0.02055)
    assert card["energy_wh"] is None and card["energy_per_acceptable_wh"] is None
    assert card["reliability"] == {"infra_failures": 0, "timeouts": 0, "retries": 0}
    assert card["models"] == [{"model": "test/secret-model-x9", "targets": 3}]
    assert card["quality"] is None and card["evaluation"] is None
    assert card["family_key"].startswith("prompt:") and card["profile"] == "prompt"


def test_yield_with_an_evaluation_counts_quality_not_status(client, world, eng):
    eng.scores = {"row-1": 1, "row-2": 5, "row-3": 5}
    b = make(client, world)
    run_batch(client, b["id"])
    card = client.get(f"/api/batches/{b['id']}/scorecard").json()
    assert card["classes"]["success"] == 3 and card["acceptable"] == 2  # row-1 scores 0 < 60
    assert card["quality"]["scored"] == 3 and card["quality"]["mean"] == pytest.approx(66.7, abs=0.1)
    assert card["evaluation"]["status"] == "completed"


def test_infra_and_timeouts_leave_the_yield_denominator(client, world, eng):
    eng.errors = {"row-2": "OSError: [Errno 28] No space left on device", "row-3": "x"}
    b = make(client, world, evaluation=None)
    run_batch(client, b["id"])
    card = client.get(f"/api/batches/{b['id']}/scorecard").json()
    assert card["classes"]["infra"] == 1 and card["yield"] == 0.5  # 1 acceptable of (3 - 1)
    assert card["reliability"]["infra_failures"] == 1


def _pair(client, world, eng, *, a_eval=True, b_eval=True, scores_b=None):
    a = make(client, world, slug="batch-a", evaluation=EVAL_ON if a_eval else None)
    run_batch(client, a["id"])
    eng.scores = scores_b or {}
    b = make(client, world, slug="batch-b", evaluation=EVAL_ON if b_eval else None)
    run_batch(client, b["id"])
    return a, b


def test_compare_matches_rows_and_computes_deltas(client, world, eng):
    a, b = _pair(client, world, eng, scores_b={"row-1": 2})
    r = client.get(f"/api/batches/compare?a={a['id']}&b={b['id']}")
    assert r.status_code == 200, r.text
    c = r.json()
    assert c["same_family"] and c["quality_comparable"] and c["quality_note"] is None
    assert (c["shared"], c["only_a"], c["only_b"]) == (3, 0, 0)
    first = c["rows"][0]  # largest |delta| first
    assert first["row_key"] == "acme-ltd" and first["delta"]["quality"] == -50.0
    assert first["a"]["quality"] == 75.0 and first["b"]["quality"] == 25.0
    s = c["summary"]
    assert (s["wins_a"], s["wins_b"], s["ties"]) == (1, 0, 2) and s["mean_quality_delta"] == pytest.approx(-16.67)
    assert s["interval90"] is not None and s["acceptable_a"] == 3 and s["acceptable_b"] == 2
    assert c["a"]["batch_id"] == a["id"] and c["b"]["batch_id"] == b["id"]
    again = client.get(f"/api/batches/compare?a={a['id']}&b={b['id']}").json()
    assert again["summary"]["interval90"] == s["interval90"]


def test_quality_is_not_comparable_without_matching_evaluations(client, world, eng):
    a, b = _pair(client, world, eng, b_eval=False)
    c = client.get(f"/api/batches/compare?a={a['id']}&b={b['id']}").json()
    assert not c["quality_comparable"] and c["quality_note"] == "Batch B has no evaluation."
    assert c["summary"]["wins_a"] is None and c["summary"]["interval90"] is None
    assert c["rows"][0]["delta"]["quality"] is None and c["summary"]["cost_a"] > 0
    # a different judge makes the two passes incomparable
    assert client.post(f"/api/batches/{b['id']}/evaluate", json={"judge_model": "anthropic/claude-opus-5"}).status_code == 200
    from tests.test_collections_api import settle

    settle(client)
    c = client.get(f"/api/batches/compare?a={a['id']}&b={b['id']}").json()
    assert not c["quality_comparable"] and "different judge" in c["quality_note"]
