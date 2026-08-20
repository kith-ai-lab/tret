"""Golden runs — "a tret for tret".

Each scenario drives the real engine end to end (real pack, real doctrine, real
tools against seeded sample data, real validation) with a scripted model, and
locks in one of the trust guarantees from the README:

* `test_happy_path_*`        — the AI never invents numbers; the run is auditable.
* `test_hallucination_*`     — a value that was never retrieved is a validation
                               error, not output; and a run that lands no valid
                               verdict says so in its status.
* `test_insufficient_data_*` — honest uncertainty is a first-class outcome.
* `test_method_run_*`        — vetted method output is citable; the same number
                               without a method run behind it is not.

Prompt, doctrine, routing, and provider changes must keep these green. See
docs/evals.md.
"""
from __future__ import annotations

import uuid

import pytest
from golden_world import GOLDEN_MODEL
from replay_provider import ReplayProvider, ScriptedCall, ScriptedTurn, cite, find_row, rows_of

from tret.engine.validation import validate_cited_values, validate_payload

SITE = "S-003"  # Alder Point, River Valley — the demo site
PERIL = "flood"

DIVERGENCE_NOTE = (
    "Alder Point sits in the River Valley, where the forward-looking climate "
    "signal points to a clear increase in extreme-precipitation days by 2050 "
    "under both scenarios we hold, with the higher-emissions case stronger "
    "still. The vendor reference score for this site rates flood exposure as "
    "low, but that score was produced several years ago and before the basin "
    "was re-mapped. Comparing the two on direction and magnitude class, the "
    "forward-looking signal indicates materially more flood risk than the "
    "reference score reflects. The most defensible explanation is the age of "
    "the reference input rather than any site-specific protection. A reader "
    "should treat the low reference rating as stale for this location and seek "
    "a refreshed score before relying on it."
)

INSUFFICIENT_NOTE = (
    "Hawthorn Quay sits in the Coastal Lowland, where the forward-looking "
    "signal points to a moderate increase in extreme-precipitation days by "
    "2050 under both scenarios we hold. There is, however, no vendor reference "
    "score for flood at this site, so there is nothing to compare the signal "
    "against on the honest grain of direction and magnitude class. That is a "
    "coverage gap rather than an agreement or a disagreement, so no divergence "
    "verdict can be defended here. A request for the missing reference score "
    "has been filed. A reader should treat this site as unassessed for flood "
    "until that coverage arrives."
)

PORTFOLIO_NOTE = (
    "This is a portfolio-level view rather than a single site. Every flood "
    "assessment recorded for this portfolio so far has found the forward-"
    "looking signal running ahead of the vendor reference score, and the "
    "aggregate divergence rate reflects that. The dominant explanation across "
    "those assessments is the age of the reference inputs rather than any "
    "site-specific factor. The rate and the dominant explanation here are both "
    "computed by a vetted rollup over the recorded verdicts, not estimated. A "
    "reader should read this as a signal about the vintage of the reference "
    "data set as a whole, and expect the picture to move as scores are "
    "refreshed and more sites are assessed."
)

# A method reference that no method run ever produced.
FABRICATED_METHOD_REF = (
    f"method/portfolio_divergence_rate/{uuid.UUID(int=0)}"
)


# ── shared assertions ─────────────────────────────────────────────────────────
def assert_citations_grounded(finding, schema: dict) -> None:
    """Schema-valid, and every cited value traceable to this run's provenance."""
    assert validate_payload(finding.payload, schema) == []
    retrieved = finding.provenance["retrieved_values"]
    assert validate_cited_values(finding.payload["cited_values"], retrieved) == []
    keys = {(r["dataset"], r["row_ref"], r["value"]) for r in retrieved}
    for cited in finding.payload["cited_values"]:
        assert (cited["dataset"], cited["row_ref"], cited["value"]) in keys


def assert_auditable(result, *, expected_model: str = GOLDEN_MODEL) -> None:
    """Trust rule #3: which model, why, which doctrine, what it cost."""
    run = result.run
    assert run.model_used == expected_model
    assert run.provider_used == "anthropic"
    assert run.routing["chosen_model"] == expected_model
    assert run.routing["override"] == "user_pin"
    assert run.routing["routing_prompt_version"]
    # Which objective the router was working to, even when a pin decided it.
    assert run.routing["objective"] == "balanced"  # golden harnesses set none
    assert run.doctrine_sha and len(run.doctrine_sha) == 64
    assert run.input_tokens > 0 and run.output_tokens > 0
    assert run.cost_usd > 0
    # ...and what it burned: the estimated ecological cost travels with the
    # dollar cost, or the audit trail is only half of one (docs/eco-accounting.md).
    assert run.energy_wh is not None and run.energy_wh > 0
    assert run.energy_accounting["estimated"] is True
    assert run.energy_accounting["model"] == expected_model
    assert run.energy_accounting["co2e_g"] > 0
    assert run.energy_accounting["grid_co2e_g_per_kwh"] > 0
    assert run.started_at is not None and run.finished_at is not None
    assert result.event_types[0] == "routing"
    assert result.event_types[-1] in ("done", "error")
    # The transcript is persisted turn by turn, tool calls and results included.
    assert len(run.messages) > 1
    called_ids = {
        call["id"] for m in run.messages for call in (m.get("tool_calls") or [])
    }
    answered_ids = {m["tool_call_id"] for m in run.messages if m["role"] == "tool"}
    assert called_ids == answered_ids


# ── scenario scripts ──────────────────────────────────────────────────────────
def _lookup(dataset: str, **filters) -> ScriptedCall:
    return ScriptedCall("lookup_dataset", {"dataset": dataset, "filters": filters})


def _happy_verdict(messages) -> dict:
    """Cite only what the tools actually returned, quoted verbatim."""
    signals = rows_of(messages, "lookup_dataset", call_index=1)
    hazard = rows_of(messages, "lookup_dataset", call_index=2)
    mid = find_row(signals, scenario="mid_emissions")
    high = find_row(signals, scenario="high_emissions")
    score = hazard[0]
    return {
        "schema_slug": "divergence_verdict",
        "subject": {"site_id": SITE, "peril": PERIL},
        "payload": {
            "verdict": "diverge_signal_higher",
            "reason_code": "outdated_inputs",
            "confidence": "high",
            "methodology_note": DIVERGENCE_NOTE,
            "cited_values": [
                cite(score, "rating"),
                cite(score, "vintage_year"),
                cite(mid, "median_delta"),
                cite(high, "tail_delta"),
            ],
            "doctrine_citations": [
                "Divergence Assessment Procedure — Step 5",
                "Reason Codes — outdated_inputs",
            ],
        },
    }


def divergence_happy_script() -> list[ScriptedTurn]:
    return [
        ScriptedTurn(
            text="Resolving the site and its region first.",
            tool_calls=[_lookup("sites", site_id=SITE)],
        ),
        ScriptedTurn(
            text="Now the forward-looking regional signal, both scenarios.",
            tool_calls=[_lookup("regional_signals", region_id="R-VALLEY", peril=PERIL)],
        ),
        ScriptedTurn(
            text="And the vendor reference score with its vintage.",
            tool_calls=[_lookup("hazard_scores", site_id=SITE, peril=PERIL)],
        ),
        ScriptedTurn(
            text="The signal is robust and the reference score is stale. Recording.",
            tool_calls=[ScriptedCall("record_verdict", _happy_verdict)],
        ),
        ScriptedTurn(text="Recorded as a draft verdict awaiting review."),
    ]


async def run_happy_path(world) -> object:
    provider = ReplayProvider(divergence_happy_script())
    return await world.run(
        provider=provider,
        task_type="divergence_assessment",
        task_input={"site_id": SITE, "peril": PERIL},
    )


# ── (a) happy path ────────────────────────────────────────────────────────────
async def test_happy_path_records_a_grounded_schema_valid_verdict(world):
    result = await run_happy_path(world)

    assert result.run.status == "completed", result.run.error
    assert result.run.iterations == 5
    assert result.tool_errors == []
    assert result.data_requests == []

    finding = result.finding
    assert finding.schema_slug == "divergence_verdict"
    assert finding.status == "draft"  # trust rule #2: nothing self-approves
    assert finding.subject == {"site_id": SITE, "peril": PERIL}
    assert finding.payload["verdict"] == "diverge_signal_higher"
    assert finding.payload["reason_code"] == "outdated_inputs"
    assert_citations_grounded(finding, world.output_schema("divergence_verdict"))

    # The vendor score really is the stale 2018 'low' rating in the sample data.
    cited = {(c["dataset"], c["column"], c["value"]) for c in finding.payload["cited_values"]}
    assert ("hazard_scores", "vintage_year", "2018") in cited
    assert ("hazard_scores", "rating", "low") in cited
    assert {c["dataset"] for c in finding.payload["cited_values"]} == {
        "hazard_scores",
        "regional_signals",
    }

    assert finding.provenance["model"] == GOLDEN_MODEL
    assert finding.provenance["doctrine_sha"] == world.doctrine_sha
    assert_auditable(result)


async def test_happy_path_run_events_form_an_audit_trail(world):
    result = await run_happy_path(world)

    assert result.event_types.count("tool_call") == 4
    assert result.event_types.count("tool_result") == 4
    assert result.event_types.count("usage") == 5
    assert len(result.events_of("finding_recorded")) == 1
    assert result.events_of("finding_recorded")[0].data["finding_id"] == str(result.finding.id)
    assert [e.data["tool"] for e in result.events_of("tool_call")] == [
        "lookup_dataset",
        "lookup_dataset",
        "lookup_dataset",
        "record_verdict",
    ]
    done = result.events_of("done")[0].data
    assert done["status"] == "completed"
    assert done["iterations"] == 5
    assert done["cost_usd"] > 0
    assert done["energy_wh"] > 0 and done["co2e_g"] > 0  # estimated, and never omitted
    # Energy is reported live per turn, not only at the end.
    assert all(e.data["energy_wh"] > 0 for e in result.events_of("usage"))


async def test_happy_path_context_carries_doctrine_schema_and_pack_tools(world):
    """The context the model actually saw — the prompt contract, locked."""
    result = await run_happy_path(world)
    first = result.provider.calls[0]

    assert "NEVER state or cite a numeric value" in first.system
    assert 'sha256="' in first.system
    for rel in ("doctrine/01-assessment-principles.md", "doctrine/02-divergence-procedure.md"):
        assert f'<doctrine file="{rel}"' in first.system
    assert "# Divergence Assessment Procedure" in first.system
    assert "## Current task: Signal divergence assessment" in first.system
    assert "## Output contract" in first.system
    assert '`"divergence_verdict"`' in first.system

    # Tools offered come from the pack task definition, not from the harness.
    assert first.tool_names == world.task_config("divergence_assessment")["tools"]
    assert first.model == "claude-sonnet-5"  # provider wire id, resolved by the catalog
    assert first.temperature == 0.0 and first.max_tokens == 4096
    assert first.messages[0].role == "user"
    assert f'"site_id": "{SITE}"' in first.messages[0].content

    # Every later turn sees the tool results fed back.
    assert result.provider.calls[1].messages[-1].role == "tool"
    assert "R-VALLEY" in result.provider.calls[1].messages[-1].content


# ── (b) hallucination caught ──────────────────────────────────────────────────
HALLUCINATED_SCORE = "87"


def _hallucinated_verdict(messages) -> dict:
    hazard = rows_of(messages, "lookup_dataset", call_index=0)
    score = hazard[0]
    return {
        "schema_slug": "divergence_verdict",
        "subject": {"site_id": SITE, "peril": PERIL},
        "payload": {
            "verdict": "diverge_signal_higher",
            "reason_code": "outdated_inputs",
            "confidence": "high",
            "methodology_note": DIVERGENCE_NOTE,
            "cited_values": [
                cite(score, "vintage_year"),
                # Never retrieved: a plausible-looking score the model "recalled".
                {
                    "dataset": "hazard_scores",
                    "row_ref": score["_row"],
                    "column": "score",
                    "value": HALLUCINATED_SCORE,
                },
            ],
            "doctrine_citations": ["Divergence Assessment Procedure — Step 5"],
        },
    }


async def test_hallucinated_number_is_a_validation_error_not_a_finding(world):
    provider = ReplayProvider(
        [
            ScriptedTurn(
                text="Reading the vendor score.",
                tool_calls=[_lookup("hazard_scores", site_id=SITE, peril=PERIL)],
            ),
            ScriptedTurn(
                text="Recording the verdict.",
                tool_calls=[ScriptedCall("record_verdict", _hallucinated_verdict)],
            ),
            ScriptedTurn(text="I am unable to record a verdict."),  # gives up
            ScriptedTurn(text="Standing down."),  # answer to the engine's nudge
        ]
    )
    result = await world.run(
        provider=provider,
        task_type="divergence_assessment",
        task_input={"site_id": SITE, "peril": PERIL},
    )

    # Nothing was written: the fabricated number never became a finding.
    assert result.findings == []
    assert await world.findings_in_project() == []

    # The exact failure mode, surfaced to the model as a repairable tool error.
    errors = result.tool_errors
    assert len(errors) == 1
    message = errors[0]["result"]
    assert errors[0]["tool"] == "record_verdict"
    assert "Validation failed (attempt 1/3)" in message
    assert (
        f"cited_values[1]: value '{HALLUCINATED_SCORE}' from dataset 'hazard_scores' "
        "was never retrieved via lookup_dataset in this run" in message
    )

    # It is fed back in-loop so a real model gets the chance to repair.
    third_turn = provider.calls[2].messages[-1]
    assert third_turn.role == "tool" and third_turn.meta["error"] is True
    assert "was never retrieved" in third_turn.content

    # And the engine nudges once for the missing terminal verdict.
    assert "You have not recorded your result" in provider.calls[3].messages[-1].content
    assert provider.calls[3].messages[-1].role == "user"

    # The run's own status admits there is no verdict. This assertion replaces an
    # earlier one that locked in `completed` for this scenario — documented as a
    # known gap in docs/evals.md, not endorsed. A run that produced nothing must
    # not be indistinguishable from one that produced a verdict: `completed` here
    # advertised an output that does not exist, and anything reading run status
    # (the runs list, a delegating chat turn, an operator's filter) inherited that
    # lie. It is not `failed` either — the guardrails did their job and the engine
    # never erred — so the honest third answer is its own terminal status.
    assert result.run.status == "completed_without_output"
    assert result.run.error is None
    # Still a completion, not an error event, and it carries the status verbatim.
    assert result.event_types[-1] == "done"
    assert result.events_of("done")[0].data["status"] == "completed_without_output"


async def test_hallucination_repair_budget_is_finite(world):
    """Three bad attempts exhaust the repair budget and say so."""
    attempt = ScriptedTurn(
        text="Recording the verdict.",
        tool_calls=[ScriptedCall("record_verdict", _hallucinated_verdict)],
    )
    provider = ReplayProvider(
        [
            ScriptedTurn(
                text="Reading the vendor score.",
                tool_calls=[_lookup("hazard_scores", site_id=SITE, peril=PERIL)],
            ),
            attempt,
            attempt,
            attempt,
            ScriptedTurn(text="I cannot ground this verdict; stopping."),
            ScriptedTurn(text="Standing down."),
        ]
    )
    result = await world.run(
        provider=provider,
        task_type="divergence_assessment",
        task_input={"site_id": SITE, "peril": PERIL},
    )

    assert result.findings == []
    messages = [e["result"] for e in result.tool_errors]
    assert len(messages) == 3
    assert "attempt 1/3" in messages[0]
    assert "attempt 2/3" in messages[1]
    assert "repair attempts are exhausted" in messages[2]
    assert result.run.status == "completed_without_output"


def _wrong_column_verdict(messages) -> dict:
    """Two real values from one real row — with one of them on the wrong field."""
    score = rows_of(messages, "lookup_dataset", call_index=0)[0]
    misattributed = cite(score, "vintage_year")
    misattributed["column"] = "rating"  # 2018 presented as the flood rating
    return {
        "schema_slug": "divergence_verdict",
        "subject": {"site_id": SITE, "peril": PERIL},
        "payload": {
            "verdict": "diverge_signal_higher",
            "reason_code": "outdated_inputs",
            "confidence": "high",
            "methodology_note": DIVERGENCE_NOTE,
            "cited_values": [cite(score, "rating"), misattributed],
            "doctrine_citations": ["Divergence Assessment Procedure — Step 5"],
        },
    }


async def test_a_retrieved_value_on_the_wrong_column_is_rejected(world):
    """Grounded in the row is not grounded in the cell.

    The cross-check compared (dataset, row_ref, value) and ignored `column`, so a
    number that really was retrieved could be attributed to another field of the
    same real row and still validate — a vintage year offered as a hazard rating
    reads as a plausible figure, and the citation record existed precisely so no
    reader has to go back to the dataset to catch that.
    """
    provider = ReplayProvider(
        [
            ScriptedTurn(
                text="Reading the vendor score.",
                tool_calls=[_lookup("hazard_scores", site_id=SITE, peril=PERIL)],
            ),
            ScriptedTurn(
                text="Recording the verdict.",
                tool_calls=[ScriptedCall("record_verdict", _wrong_column_verdict)],
            ),
            ScriptedTurn(text="I cannot attribute that value correctly; stopping."),
            ScriptedTurn(text="Standing down."),  # answers the engine's nudge
        ]
    )
    result = await world.run(
        provider=provider,
        task_type="divergence_assessment",
        task_input={"site_id": SITE, "peril": PERIL},
    )

    # Nothing was written, and the run does not claim an output it never landed.
    assert result.findings == []
    assert await world.findings_in_project() == []
    assert result.run.status == "completed_without_output"

    errors = result.tool_errors
    assert len(errors) == 1
    message = errors[0]["result"]
    assert errors[0]["tool"] == "record_verdict"
    assert "cited_values[1]: value '2018' was retrieved from row 'hazard_scores:" in message
    assert "value of column ['vintage_year'], not 'rating'" in message
    # The correctly attributed citation in the same payload is not the complaint.
    assert "cited_values[0]" not in message


async def test_a_run_that_records_its_verdict_is_plainly_completed(world):
    """The other side of the status: `completed` still means "there is output".

    Without this, `completed_without_output` could quietly widen to cover runs
    that did produce a verdict, and the distinction would stop meaning anything.
    """
    assert (await run_happy_path(world)).run.status == "completed"


# ── (c) insufficient_data ─────────────────────────────────────────────────────
GAP_SITE = "S-004"  # Hawthorn Quay: no flood row in hazard_scores


def _insufficient_verdict(messages) -> dict:
    signals = rows_of(messages, "lookup_dataset", call_index=1)
    mid = find_row(signals, scenario="mid_emissions")
    high = find_row(signals, scenario="high_emissions")
    return {
        "schema_slug": "divergence_verdict",
        "subject": {"site_id": GAP_SITE, "peril": PERIL},
        "payload": {
            "verdict": "insufficient_data",
            "confidence": "low",
            "methodology_note": INSUFFICIENT_NOTE,
            "cited_values": [cite(mid, "direction"), cite(high, "magnitude_class")],
            "doctrine_citations": [
                "Divergence Assessment Procedure — Step 4",
                "Reason Codes — coverage_gap",
            ],
        },
    }


async def test_missing_data_yields_insufficient_data_and_a_data_request(world):
    provider = ReplayProvider(
        [
            ScriptedTurn(
                text="Resolving the site.", tool_calls=[_lookup("sites", site_id=GAP_SITE)]
            ),
            ScriptedTurn(
                text="Reading the regional signal.",
                tool_calls=[_lookup("regional_signals", region_id="R-COAST", peril=PERIL)],
            ),
            ScriptedTurn(
                text="Looking for the vendor reference score.",
                tool_calls=[_lookup("hazard_scores", site_id=GAP_SITE, peril=PERIL)],
            ),
            ScriptedTurn(
                text="There is no reference score for this site, so I will file a request.",
                tool_calls=[
                    ScriptedCall(
                        "file_data_request",
                        {
                            "subject": {"site_id": GAP_SITE, "peril": PERIL},
                            "what_is_missing": (
                                f"A vendor flood reference score for site {GAP_SITE} "
                                "(Hawthorn Quay); the hazard score table has no flood row."
                            ),
                            "why_needed": (
                                "Without a reference score there is nothing to compare the "
                                "forward-looking regional signal against, so no divergence "
                                "verdict can be defended."
                            ),
                        },
                    )
                ],
            ),
            ScriptedTurn(
                text="Recording insufficient_data honestly.",
                tool_calls=[ScriptedCall("record_verdict", _insufficient_verdict)],
            ),
            ScriptedTurn(text="Recorded, with the coverage gap stated."),
        ]
    )
    result = await world.run(
        provider=provider,
        task_type="divergence_assessment",
        task_input={"site_id": GAP_SITE, "peril": PERIL},
    )

    assert result.run.status == "completed", result.run.error
    assert result.tool_errors == []  # insufficient_data is not an error path

    # A missing row reads as a legible instruction, not an exception.
    empty = result.tool_results("lookup_dataset")[2]["result"]
    assert "No rows in 'hazard_scores' match" in empty
    assert "file_data_request" in empty

    finding = result.finding
    assert finding.payload["verdict"] == "insufficient_data"
    assert finding.payload["confidence"] == "low"
    assert "reason_code" not in finding.payload  # only divergence verdicts carry one
    assert_citations_grounded(finding, world.output_schema("divergence_verdict"))

    assert len(result.data_requests) == 1
    request = result.data_requests[0]
    assert request.status == "open"
    assert GAP_SITE in request.what_is_missing
    assert request.subject == {"site_id": GAP_SITE, "peril": PERIL}
    assert_auditable(result)


# ── (d) method-run citation ───────────────────────────────────────────────────
ROLLUP_TOOLS = ["lookup_dataset", "run_method", "record_verdict", "file_data_request"]
ROLLUP_RATE = "100.0"  # one recorded flood verdict, diverging
ROLLUP_REASON = "outdated_inputs"


def _rollup_payload(cited: list[dict]) -> dict:
    return {
        "schema_slug": "divergence_verdict",
        "subject": {"scope": "portfolio", "peril": PERIL},
        "payload": {
            "verdict": "diverge_signal_higher",
            "reason_code": "outdated_inputs",
            "confidence": "medium",
            "methodology_note": PORTFOLIO_NOTE,
            "cited_values": cited,
            "doctrine_citations": ["Reason Codes — outdated_inputs"],
        },
    }


def _rollup_without_method(messages) -> dict:
    """The right numbers, with no method run behind them."""
    return _rollup_payload(
        [
            {
                "dataset": FABRICATED_METHOD_REF,
                "row_ref": f"{FABRICATED_METHOD_REF}:0",
                "column": "divergence_rate_pct",
                "value": ROLLUP_RATE,
            },
            {
                "dataset": FABRICATED_METHOD_REF,
                "row_ref": f"{FABRICATED_METHOD_REF}:0",
                "column": "dominant_reason_code",
                "value": ROLLUP_REASON,
            },
        ]
    )


def _rollup_from_method(messages) -> dict:
    """The same numbers, quoted from the method run that produced them."""
    row = find_row(rows_of(messages, "run_method"), peril=PERIL)
    return _rollup_payload(
        [cite(row, "divergence_rate_pct"), cite(row, "dominant_reason_code")]
    )


async def test_method_output_is_citable_but_the_same_number_alone_is_not(world):
    # A recorded verdict exists for the rollup to aggregate.
    await run_happy_path(world)

    provider = ReplayProvider(
        [
            ScriptedTurn(
                text="The portfolio divergence rate is 100% with stale inputs dominating.",
                tool_calls=[ScriptedCall("record_verdict", _rollup_without_method)],
            ),
            ScriptedTurn(
                text="I must compute that with the vetted rollup instead of asserting it.",
                tool_calls=[
                    ScriptedCall(
                        "run_method",
                        {"method": "portfolio_divergence_rate", "params": {"peril": PERIL}},
                    )
                ],
            ),
            ScriptedTurn(
                text="Recording the portfolio verdict from the computed rollup.",
                tool_calls=[ScriptedCall("record_verdict", _rollup_from_method)],
            ),
            ScriptedTurn(text="Recorded as a draft."),
        ]
    )
    harness_id = await world.create_harness(name="Portfolio Analyst", tool_names=ROLLUP_TOOLS)
    result = await world.run(
        provider=provider,
        harness_id=harness_id,
        task_type="freeform",
        task_input={"message": "How divergent is the flood book, and why?"},
    )

    assert result.run.status == "completed", result.run.error

    # Without the method run, the numbers are ungrounded — both rejected.
    errors = result.tool_errors
    assert len(errors) == 1
    message = errors[0]["result"]
    assert f"value '{ROLLUP_RATE}' from dataset '{FABRICATED_METHOD_REF}'" in message
    assert "was never retrieved via lookup_dataset in this run" in message
    assert f"value '{ROLLUP_REASON}'" in message

    # The method run is manifest-pinned: params, code hash, input summary, output hash.
    assert len(result.method_runs) == 1
    method_run = result.method_runs[0]
    assert method_run.method_slug == "portfolio_divergence_rate"
    assert method_run.status == "completed"
    assert method_run.params == {"peril": PERIL}
    assert len(method_run.code_sha) == 64 and len(method_run.output_hash) == 64
    assert method_run.input_summary["findings:divergence_verdict"]["rows"] == 1
    assert method_run.row_count == 1

    # Same numbers, now citable — and they are the deterministic lane's output.
    finding = result.finding
    assert_citations_grounded(finding, world.output_schema("divergence_verdict"))
    method_ref = f"method/portfolio_divergence_rate/{method_run.id}"
    assert {c["dataset"] for c in finding.payload["cited_values"]} == {method_ref}
    assert {c["column"]: c["value"] for c in finding.payload["cited_values"]} == {
        "divergence_rate_pct": ROLLUP_RATE,
        "dominant_reason_code": ROLLUP_REASON,
    }
    assert any(
        r["dataset"] == method_ref and r["value"] == ROLLUP_RATE
        for r in finding.provenance["retrieved_values"]
    )
    assert method_run.output[0]["divergence_rate_pct"] == float(ROLLUP_RATE)


# ── the harness under the harness ─────────────────────────────────────────────
# The engine turns exceptions into run.error, so a broken script could look like
# a passing eval. These two lock the seatbelt.
async def test_script_exhaustion_fails_loudly(world):
    provider = ReplayProvider(
        [ScriptedTurn(text="One turn only.", tool_calls=[_lookup("sites", site_id=SITE)])]
    )
    with pytest.raises(AssertionError, match="script exhausted"):
        await world.run(
            provider=provider,
            task_type="divergence_assessment",
            task_input={"site_id": SITE, "peril": PERIL},
        )


async def test_calling_a_tool_the_pack_does_not_enable_fails_loudly(world):
    """Pack drift (a tool removed from a task) must break the eval, not pass it."""
    provider = ReplayProvider(
        [ScriptedTurn(tool_calls=[ScriptedCall("run_method", {"method": "ghg_inventory"})])]
    )
    with pytest.raises(AssertionError, match="but the engine offered"):
        await world.run(
            provider=provider,
            task_type="divergence_assessment",
            task_input={"site_id": SITE, "peril": PERIL},
        )


# ── determinism ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("attempt", [1, 2])
async def test_golden_run_is_reproducible(world, attempt):
    """Same script, same pack, same recorded verdict — twice."""
    result = await run_happy_path(world)
    assert result.run.status == "completed"
    assert result.finding.payload["verdict"] == "diverge_signal_higher"
    assert [c["value"] for c in result.finding.payload["cited_values"]] == [
        "low",
        "2018",
        "0.18",
        "0.36",
    ]
