"""Golden runs for the token economy — tret's climate mission, mechanized.

Every token tret sends is energy spent, so the engine treats context size as a
property to be measured and bounded, not a side effect:

* `test_run_records_*`      — what the prompt was made of is recorded per
                              component, so spend is attributable.
* `test_truncated_lookup_*` — an oversized tool result is capped *and says so*,
                              and the values it did not show cannot be cited.
* `test_iteration_*`        — the loop cannot run forever, whatever a harness
                              config asks for.
* `test_output_budget_*`    — an over-budget run is told to finalize, then
                              stopped if it ignores the instruction.
"""
from __future__ import annotations

import csv
import json

from golden_world import CLIMATE_PACK
from replay_provider import ReplayProvider, ScriptedCall, ScriptedTurn, cite, tool_results
from test_golden_runs import DIVERGENCE_NOTE, PERIL, SITE, run_happy_path

from tret.api.runs import get_run
from tret.api.workspace import WorkspaceContext
from tret.db.models import Workspace
from tret.engine import harness as harness_module
from tret.engine import tools as tools_module
from tret.engine.validation import validate_cited_values

HAZARD_ROWS = list(csv.DictReader((CLIMATE_PACK / "sample-data/hazard_scores.csv").open()))
TOTAL_HAZARD_ROWS = len(HAZARD_ROWS)
ROW_CAP = 3  # patched cap for the truncation scenarios
# S-003's stale 'low' flood rating: row index 4, i.e. past a 3-row cap.
STALE_ROW_INDEX = next(
    i for i, r in enumerate(HAZARD_ROWS) if r["site_id"] == SITE and r["peril"] == PERIL
)


def _lookup_all_hazard_scores() -> ScriptedCall:
    return ScriptedCall("lookup_dataset", {"dataset": "hazard_scores"})


def _rows_shown(messages) -> list[dict]:
    """Parse a possibly-truncated lookup result: JSON array, then the marker."""
    raw = tool_results(messages, "lookup_dataset")[-1]
    return json.loads(raw.split("\n\n[TRUNCATED", 1)[0])


# ── composition accounting ────────────────────────────────────────────────────
async def test_run_records_what_its_context_was_made_of(world):
    result = await run_happy_path(world)
    composition = result.run.context_composition

    assert composition["estimator"] == "chars/4"
    assert composition["total_est_tokens"] == sum(
        b["est_tokens"] for b in composition["blocks"]
    )
    by_kind = composition["by_kind"]
    for kind in (
        "platform_preamble",
        "doctrine",
        "task_instructions",
        "output_schema",
        "tool_specs",
        "user_message",
    ):
        assert by_kind[kind] > 0, kind
    # Doctrine dominates, and each file is attributed individually with the hash
    # of exactly the text this run loaded.
    doctrine = [b for b in composition["blocks"] if b["kind"] == "doctrine"]
    assert [b["label"] for b in doctrine] == world.pack_manifest["doctrine"]
    assert all(len(b["sha256"]) == 64 for b in doctrine)
    tools_block = next(b for b in composition["blocks"] if b["kind"] == "tool_specs")
    assert set(tools_block["parts"]) == set(world.task_config("divergence_assessment")["tools"])

    # Published live too, so a watching analyst sees the cost before it is spent.
    event = result.events_of("context_composition")[0].data
    assert event["total_est_tokens"] == composition["total_est_tokens"]

    # And it describes the prompt the provider actually received.
    system = result.provider.calls[0].system
    assert abs(composition["by_kind"]["platform_preamble"] - len(system) // 4) > 0
    assert composition["total_chars"] > len(system)  # + tool specs + user message


async def test_runs_api_exposes_the_composition(world):
    """Legibility is the point: the breakdown reaches whoever reads the run."""
    result = await run_happy_path(world)
    ctx = WorkspaceContext(Workspace(id=world.workspace_id, name="W", kind="team"), "owner")
    async with world.session_factory() as db:
        payload = await get_run(result.run.id, user=None, ctx=ctx, db=db)
    assert payload["context_composition"] == result.run.context_composition
    assert payload["context_composition"]["by_kind"]["doctrine"] > 0


# ── tool-result caps ──────────────────────────────────────────────────────────
def _verdict_citing_a_truncated_row(messages) -> dict:
    """The right answer for S-003 — from a row the capped result never showed."""
    shown = _rows_shown(messages)
    return {
        "schema_slug": "divergence_verdict",
        "subject": {"site_id": SITE, "peril": PERIL},
        "payload": {
            "verdict": "diverge_signal_higher",
            "reason_code": "outdated_inputs",
            "confidence": "high",
            "methodology_note": DIVERGENCE_NOTE,
            "cited_values": [
                cite(shown[0], "rating"),
                {
                    "dataset": "hazard_scores",
                    "row_ref": f"hazard_scores:{STALE_ROW_INDEX}",
                    "column": "rating",
                    "value": "low",
                },
            ],
            "doctrine_citations": ["Divergence Assessment Procedure — Step 5"],
        },
    }


def _verdict_citing_shown_rows(messages) -> dict:
    shown = _rows_shown(messages)
    return {
        "schema_slug": "divergence_verdict",
        "subject": {"site_id": shown[0]["site_id"], "peril": shown[0]["peril"]},
        "payload": {
            "verdict": "agree",
            "confidence": "medium",
            "methodology_note": DIVERGENCE_NOTE,
            "cited_values": [cite(shown[0], "rating"), cite(shown[0], "vintage_year")],
            "doctrine_citations": ["Assessment Principles — Compare at the honest grain"],
        },
    }


async def test_truncated_lookup_announces_itself_and_shrinks_the_citable_set(world, monkeypatch):
    monkeypatch.setattr(tools_module, "MAX_RESULT_ROWS", ROW_CAP)
    provider = ReplayProvider(
        [
            ScriptedTurn(
                text="Reading the vendor scores.", tool_calls=[_lookup_all_hazard_scores()]
            ),
            ScriptedTurn(
                text="Recording the S-003 verdict from what I know of that site.",
                tool_calls=[ScriptedCall("record_verdict", _verdict_citing_a_truncated_row)],
            ),
            ScriptedTurn(
                text="I may only use the rows I was actually shown.",
                tool_calls=[ScriptedCall("record_verdict", _verdict_citing_shown_rows)],
            ),
            ScriptedTurn(text="Recorded as a draft."),
        ]
    )
    result = await world.run(
        provider=provider,
        task_type="divergence_assessment",
        task_input={"site_id": SITE, "peril": PERIL},
    )

    # The cap is announced in the result text, with a way out of it.
    shown = result.tool_results("lookup_dataset")[0]["result"]
    assert f"[TRUNCATED: showing {ROW_CAP} of {TOTAL_HAZARD_ROWS} matching rows" in shown
    assert "may not cite or reason over them" in shown
    assert "Narrow the query" in shown and "run_method" in shown

    # Truncation shrinks the citable set: the stale 'low' rating is real data in
    # the dataset, but this run never retrieved it, so it cannot be cited.
    errors = result.tool_errors
    assert len(errors) == 1
    assert "value 'low' from dataset 'hazard_scores'" in errors[0]["result"]
    assert "was never retrieved via lookup_dataset in this run" in errors[0]["result"]

    # The rows that were shown remain fully citable.
    finding = result.finding
    retrieved = finding.provenance["retrieved_values"]
    assert validate_cited_values(finding.payload["cited_values"], retrieved) == []
    assert {r["row_ref"] for r in retrieved} == {f"hazard_scores:{i}" for i in range(ROW_CAP)}
    assert result.run.status == "completed", result.run.error


async def test_an_untruncated_result_is_byte_for_byte_what_it_always_was(world):
    """The caps are a ceiling, not a reformat: normal results are unchanged."""
    result = await run_happy_path(world)
    for shown in result.tool_results("lookup_dataset"):
        assert "[TRUNCATED" not in shown["result"]
        assert isinstance(json.loads(shown["result"]), list)


# ── iteration and budget guardrails ───────────────────────────────────────────
def _endless_lookups(n: int) -> list[ScriptedTurn]:
    return [
        ScriptedTurn(text="Still looking.", tool_calls=[_lookup_all_hazard_scores()])
        for _ in range(n)
    ]


async def test_iteration_cap_ends_a_loop_that_never_finishes(world):
    provider = ReplayProvider(_endless_lookups(2))
    harness_id = await world.create_harness(max_iterations=2)
    result = await world.run(
        provider=provider,
        harness_id=harness_id,
        task_type="divergence_assessment",
        task_input={"site_id": SITE, "peril": PERIL},
    )

    assert result.run.status == "failed"
    assert result.run.error == "max_iterations (2) reached without completion"
    assert result.run.iterations == 2
    assert provider.turns_played == 2
    assert result.findings == []


async def test_the_engine_ceiling_overrides_a_harness_asking_for_more(world, monkeypatch):
    monkeypatch.setattr(harness_module, "MAX_ITERATIONS_CEILING", 2)
    provider = ReplayProvider(_endless_lookups(2))
    harness_id = await world.create_harness(max_iterations=999)
    result = await world.run(
        provider=provider,
        harness_id=harness_id,
        task_type="divergence_assessment",
        task_input={"site_id": SITE, "peril": PERIL},
    )

    assert result.run.status == "failed"
    assert result.run.error == "max_iterations (2) reached without completion"


async def test_output_budget_asks_for_a_finish_then_stops_the_run(world):
    # Scripted turns report 240 output tokens each: crossed at iteration 2,
    # hard-stopped (1.5x) at iteration 3.
    provider = ReplayProvider(_endless_lookups(3))
    harness_id = await world.create_harness(max_run_output_tokens=300)
    result = await world.run(
        provider=provider,
        harness_id=harness_id,
        task_type="divergence_assessment",
        task_input={"site_id": SITE, "peril": PERIL},
    )

    warning = result.events_of("budget_warning")
    assert len(warning) == 1
    assert warning[0].data == {"kind": "output_tokens", "output_tokens": 480, "budget": 300}

    # The model is told to finalize, in-loop, before anything is cut off.
    nudge = provider.calls[2].messages[-1]
    assert nudge.role == "user"
    assert "OUTPUT BUDGET REACHED" in nudge.content
    assert "record_verdict" in nudge.content
    assert "file_data_request" in nudge.content

    # It kept going, so the run stops rather than spending without limit.
    assert result.run.status == "failed"
    assert "output_budget_exceeded" in result.run.error
    assert "720 output tokens vs budget 300" in result.run.error


async def test_no_budget_declared_means_no_budget_behaviour(world):
    result = await run_happy_path(world)
    assert result.events_of("budget_warning") == []
    assert result.run.status == "completed", result.run.error
