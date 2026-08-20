"""docs/evals.md describes the golden-run suite in prose; this suite is what
keeps that prose honest.

Two claims in docs/evals.md's `test_golden_runs.py` table were found stale
during a documentation audit and corrected:

1. It said the prompt-contract test (`test_happy_path_context_carries_doctrine_
   schema_and_pack_tools`) locks "both doctrine files" — true back when the
   pack had two, false since a third (`03-reason-codes.md`) was added and
   `divergence_assessment` never opted into task-scoped doctrine, so it loads
   all three, not two.
2. It said "all of these" (the whole `test_golden_runs.py` table) use *Signal
   divergence assessment* for site S-003 x flood — false for
   `test_method_output_is_citable_but_the_same_number_alone_is_not`, which
   runs the `portfolio_divergence_rate` method on a `freeform` harness instead
   (see docs/evals.md's "Known gaps": no shipped verdict task combines
   `run_method` with a `cited_values`-validating terminal tool).

Both are prose describing code, so both can drift again silently — a doc fix
with no test is just a doc fix that will need another audit to catch the next
drift. These tests read the actual pack manifest and the actual eval source,
not docs/evals.md itself, and fail if either underlying fact stops matching
what the doc now claims.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PACK_YAML = REPO_ROOT / "packs" / "climate-risk" / "pack.yaml"
GOLDEN_RUNS = REPO_ROOT / "backend" / "tests" / "evals" / "test_golden_runs.py"
EVALS_DOC = REPO_ROOT / "docs" / "evals.md"


def _pack_manifest() -> dict:
    return yaml.safe_load(PACK_YAML.read_text())


def _divergence_assessment_task(manifest: dict) -> dict:
    return next(t for t in manifest["task_types"] if t["slug"] == "divergence_assessment")


# ── claim 1: divergence_assessment loads every pack doctrine file ─────────────
def test_divergence_assessment_declares_no_doctrine_scope_of_its_own():
    """The doc's "loads every doctrine file, three today" claim rests on this:
    no `doctrine:` override on the task means the loader's documented default
    ("omit doctrine: and the task gets every doctrine file in the pack",
    tret/engine/context.py::task_doctrine_selection) applies in full.
    """
    task = _divergence_assessment_task(_pack_manifest())
    assert "doctrine" not in task, (
        "divergence_assessment now declares its own doctrine scope — "
        "docs/evals.md's claim that it loads every pack doctrine file (because "
        "it declares none) is no longer true and needs updating"
    )


def test_the_pack_has_exactly_three_doctrine_files():
    """docs/evals.md says "three, today" — pinned so a fourth doctrine file
    (or a trim back to two) is a docs update, not a silent mismatch.
    """
    manifest = _pack_manifest()
    assert manifest["doctrine"] == [
        "doctrine/01-assessment-principles.md",
        "doctrine/02-divergence-procedure.md",
        "doctrine/03-reason-codes.md",
    ]


def test_evals_doc_no_longer_claims_exactly_two_doctrine_files():
    """Regression guard for the specific stale phrase, in addition to the
    positive checks above: "both doctrine files" implied a count of two that
    stopped being true.
    """
    text = EVALS_DOC.read_text()
    assert "both doctrine files" not in text


# ── claim 2: the method-citation scenario is the one exception ────────────────
def test_method_citation_scenario_does_not_use_the_flagship_task():
    """The scenario docs/evals.md carves out as an exception must actually be
    one: task_type="freeform", not "divergence_assessment", and no reference to
    the flagship site/peril fixture.
    """
    source = GOLDEN_RUNS.read_text()
    match = re.search(
        r"async def test_method_output_is_citable_but_the_same_number_alone_is_not"
        r"\(world\):(.*?)\n\nasync def ",
        source,
        re.DOTALL,
    )
    assert match, "test_method_output_is_citable_but_the_same_number_alone_is_not not found"
    body = match.group(1)
    assert 'task_type="freeform"' in body, (
        "test_method_output_is_citable_but_the_same_number_alone_is_not no longer "
        "runs on a freeform harness — docs/evals.md's carve-out for it (and the "
        "reason given in Known gaps) needs re-checking"
    )


def test_evals_doc_names_the_method_citation_scenario_as_the_exception():
    text = EVALS_DOC.read_text()
    assert "test_method_output_is_citable_but_the_same_number_alone_is_not" in text.split(
        "All of these use"
    )[1].split("\n\n")[0], (
        "docs/evals.md's exception sentence no longer names the one scenario "
        "that actually doesn't use Signal divergence assessment"
    )
