"""Golden runs for the write-integrity invariant.

One rule, stated three ways:

> **A tool never reports failure after its write succeeded, and the run's
> persisted state never contradicts its status or its events.**

`test_golden_runs.py` covers the flagship verdict task. Every scenario there
drives `divergence_assessment` → `record_verdict`, one write per turn, so two
whole classes of mainline behaviour went unexercised and two trust-breaking bugs
shipped underneath them:

* **Multi-write turns.** The loop used to `asyncio.gather` a turn's tool calls
  over the single `RunContext.db` AsyncSession. SQLAlchemy rejects concurrent
  flushes, so the second and later writers raised *after* `Session.add()` — the
  row was committed at the end of the iteration while the model was told the
  write failed, no `finding_recorded` event was published, and the terminal flag
  was never set. The shipped pack asks for "one record_finding call per finding",
  so a multi-write turn is the *normal* shape for extraction.
* **The terminal flag.** It was set by each tool about itself. `record_finding`
  never set it, yet the pack declares it the `terminal_tool` for both
  `evidence_extraction` and `qa_review` — so half the shipped task types were
  nudged after succeeding and could only ever end `completed_without_output`.
  `draft_section` had the opposite bug: it set the flag unconditionally, so it
  could mark a run complete on a task whose real terminal tool validates a
  schema.

These scenarios fail loudly on either regression. See docs/evals.md.
"""
from __future__ import annotations

import json

from golden_world import CLIMATE_PACK, GOLDEN_MODEL, build_world
from replay_provider import (
    ReplayProvider,
    ScriptedCall,
    ScriptedTurn,
    cite,
    find_row,
    rows_of,
    tool_results,
)
from test_golden_runs import (
    GAP_SITE,
    INSUFFICIENT_NOTE,
    PERIL,
    assert_auditable,
    assert_citations_grounded,
    run_happy_path,
)

EVIDENCE_DOC = CLIMATE_PACK / "sample-data" / "evidence" / "acme-esg-questionnaire.md"
EVIDENCE_FILENAME = "acme-esg-questionnaire.md"


def _lookup(dataset: str, **filters) -> ScriptedCall:
    return ScriptedCall("lookup_dataset", {"dataset": dataset, "filters": filters})


def _normalized(text: str) -> str:
    return " ".join(text.split())


# ── deriving evidence from what the tool actually returned ────────────────────
def _document_body(messages) -> str:
    """The document text as `read_document` returned it, header stripped."""
    results = tool_results(messages, "read_document")
    if not results:
        raise AssertionError("no read_document result in the conversation yet")
    return results[-1].split("\n\n", 1)[1]


def _quote(messages, needle: str) -> str:
    """A verbatim sentence from the retrieved document, trimmed, not paraphrased.

    Like `cite()` for datasets: the quote is lifted out of the text the tool
    handed back, so a scenario cannot smuggle in evidence the run never read.
    """
    body = _document_body(messages)
    start = body.find(needle)
    if start == -1:
        raise AssertionError(f"'{needle}' is not in the document read_document returned")
    end = body.find(".", start)
    if end == -1:
        raise AssertionError(f"no sentence end after '{needle}'")
    return _normalized(body[start : end + 1])


def _evidence(messages, *, needle: str, claim: str, confidence: str, note: str) -> dict:
    return {
        "schema_slug": "evidence_finding",
        "subject": {"topic": needle[:60]},
        "payload": {
            "claim": claim,
            "evidence_quote": _quote(messages, needle),
            "source_document": EVIDENCE_FILENAME,
            "confidence": confidence,
            "note": note,
        },
    }


def _read_the_questionnaire(messages) -> dict:
    """Read the one document the run was given, by the id it was given."""
    return {"document_id": _document_id_from_manifest(messages)}


def _document_id_from_manifest(messages) -> str:
    """The document id out of the run's own user message manifest."""
    first = messages[0].content or ""
    for line in first.splitlines():
        if line.startswith("- ") and EVIDENCE_FILENAME in line:
            return line[2:].split(" — ", 1)[0].strip()
    raise AssertionError(f"no {EVIDENCE_FILENAME} in the document manifest:\n{first}")


GOVERNANCE = dict(
    needle="The Audit & Risk Committee charter was amended",
    claim="The board has formalised climate oversight through its Audit & Risk Committee charter.",
    confidence="medium",
    note="Client-asserted in the questionnaire; the amended charter itself was not provided.",
)
REMUNERATION = dict(
    needle="No executive or management incentives",
    claim="No executive remuneration is linked to climate or emissions performance.",
    confidence="high",
    note="A direct negative answer from the client, which needs no corroborating document.",
)
TARGET = dict(
    needle="The company has publicly stated an ambition",
    claim="The emissions reduction ambition has no documented baseline, boundary or methodology.",
    confidence="low",
    note="Asserted on the website only: no baseline year, boundary, methodology, or board approval.",
)


async def _extraction_run(world, turns: list[ScriptedTurn]):
    document_id = await world.create_document(
        filename=EVIDENCE_FILENAME, source=EVIDENCE_DOC
    )
    provider = ReplayProvider(turns)
    return await world.run(
        provider=provider,
        task_type="evidence_extraction",
        task_input={"focus": "governance evidence"},
        document_ids=[document_id],
    )


def assert_no_false_nudge(result) -> None:
    """The engine must never claim a result is missing when it is on disk."""
    nudges = [
        m.content
        for call in result.provider.calls
        for m in call.messages
        if m.role == "user" and "You have not recorded your result" in (m.content or "")
    ]
    assert nudges == [], f"engine nudged a run that had already recorded its result: {nudges}"


def assert_status_agrees_with_findings(result) -> None:
    """Status, events, and rows are three views of one fact — or the run lied."""
    recorded = [str(f.id) for f in result.findings]
    announced = [e.data["finding_id"] for e in result.events_of("finding_recorded")]
    assert announced == recorded, (
        "persisted findings and finding_recorded events disagree: "
        f"rows={recorded} events={announced}"
    )
    if recorded:
        assert result.run.status != "completed_without_output", (
            f"run persisted {len(recorded)} finding(s) but its status says it produced nothing"
        )
    # No write may be reported as a failure — that is the whole invariant.
    assert [e["tool"] for e in result.tool_errors] == []


# ── (a) evidence_extraction reaches `completed` via record_finding ────────────
async def test_evidence_extraction_completes_on_its_declared_terminal_tool(world):
    """`record_finding` IS the terminal tool for this task; recording one ends it.

    Before the engine owned the terminal flag, `record_finding` never set it, so
    this task type — half the shipped pack, counting `qa_review` — was nudged
    after succeeding, burned an extra iteration inviting a duplicate, and ended
    `completed_without_output` with a perfectly good finding on disk.
    """
    result = await _extraction_run(
        world,
        [
            ScriptedTurn(
                text="Reading the questionnaire response.",
                tool_calls=[ScriptedCall("read_document", _read_the_questionnaire)],
            ),
            ScriptedTurn(
                text="Recording the governance evidence.",
                tool_calls=[
                    ScriptedCall("record_finding", lambda m: _evidence(m, **GOVERNANCE))
                ],
            ),
            ScriptedTurn(text="I recorded one governance finding, as a draft."),
        ],
    )

    assert result.run.status == "completed", result.run.error
    # No fourth turn was asked for: the run ended on its own, un-nudged.
    assert result.provider.turns_played == 3
    assert result.run.iterations == 3
    assert_no_false_nudge(result)
    assert_status_agrees_with_findings(result)

    finding = result.finding
    assert finding.schema_slug == "evidence_finding"
    assert finding.status == "draft"  # trust rule #2: nothing self-approves
    assert finding.payload["confidence"] == "medium"
    assert finding.payload["source_document"] == EVIDENCE_FILENAME
    # The quote is verbatim from the document the pack ships, not from the test.
    assert finding.payload["evidence_quote"] in _normalized(EVIDENCE_DOC.read_text())
    assert (
        validate_evidence(world, finding) == []
    ), "the recorded extraction is not schema-valid"

    assert finding.provenance["model"] == GOLDEN_MODEL
    assert finding.provenance["document_ids"] == [str(result.run.document_ids[0])]
    assert result.events_of("done")[0].data["status"] == "completed"
    assert_auditable(result)


def validate_evidence(world, finding) -> list[str]:
    from bench.engine.validation import validate_payload

    return validate_payload(finding.payload, world.output_schema("evidence_finding"))


# ── (b) several writes in ONE turn: all persist, all report success ───────────
async def test_every_write_in_a_multi_write_turn_persists_and_reports_success(world):
    """Three `record_finding` calls in one turn — the pack's own prescribed shape.

    The bug this locks out: 3 rows written, 2 reported to the model as
    "Tool error: unexpected failure", 1 `finding_recorded` event. A model told
    its write failed re-records, so the approvals queue filled with duplicates
    of findings that were already there.
    """
    result = await _extraction_run(
        world,
        [
            ScriptedTurn(
                text="Reading the questionnaire response.",
                tool_calls=[ScriptedCall("read_document", _read_the_questionnaire)],
            ),
            ScriptedTurn(
                text="Three discrete findings, one call each.",
                tool_calls=[
                    ScriptedCall("record_finding", lambda m: _evidence(m, **GOVERNANCE)),
                    ScriptedCall("record_finding", lambda m: _evidence(m, **REMUNERATION)),
                    ScriptedCall("record_finding", lambda m: _evidence(m, **TARGET)),
                ],
            ),
            ScriptedTurn(text="I recorded three governance findings, all drafts."),
        ],
    )

    assert result.run.status == "completed", result.run.error
    assert result.provider.turns_played == 3
    assert_no_false_nudge(result)
    assert_status_agrees_with_findings(result)

    # Every write landed exactly once — no partial turn, and no duplicates.
    assert len(result.findings) == 3
    assert len(await world.findings_in_project()) == 3
    assert [f.schema_slug for f in result.findings] == ["evidence_finding"] * 3
    assert [f.payload["confidence"] for f in result.findings] == ["medium", "high", "low"]
    for finding in result.findings:
        assert validate_evidence(world, finding) == []
        assert finding.status == "draft"
        assert finding.payload["evidence_quote"] in _normalized(EVIDENCE_DOC.read_text())

    # Every write was reported to the model as the success it was...
    writes = result.tool_results("record_finding")
    assert len(writes) == 3
    assert [w["error"] for w in writes] == [False, False, False]
    for finding, reported in zip(result.findings, writes):
        assert str(finding.id) in reported["result"]

    # ...and the audit trail carries one event per finding, in order.
    events = result.events_of("finding_recorded")
    assert [e.data["finding_id"] for e in events] == [str(f.id) for f in result.findings]
    assert result.event_types.count("tool_call") == 4
    assert result.event_types.count("tool_result") == 4
    assert_auditable(result)


# ── (c) a data request and the terminal verdict in the same turn ──────────────
def _insufficient_verdict_from_signals(messages) -> dict:
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


async def test_a_data_request_and_the_verdict_in_one_turn_agree_with_the_run_status(world):
    """Two writers in one turn, the second of them the terminal tool.

    This is the exact turn the audit reproduced: `file_data_request` flushed
    first, `record_verdict` then failed on the shared session — and the verdict
    was persisted as a draft anyway. The tool said it failed, the terminal flag
    stayed down, the run ended `completed_without_output`, and `run_harness_task`
    told the delegating chat agent "there is no finding to report" while handing
    it the finding. Three views of the run, three different answers.
    """
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
                text="Filing the gap and recording insufficient_data in the same breath.",
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
                    ),
                    ScriptedCall("record_verdict", _insufficient_verdict_from_signals),
                ],
            ),
            ScriptedTurn(text="Recorded, with the coverage gap stated."),
        ]
    )
    result = await world.run(
        provider=provider,
        task_type="divergence_assessment",
        task_input={"site_id": GAP_SITE, "peril": PERIL},
    )

    # The three views agree: status, events, rows.
    assert result.run.status == "completed", result.run.error
    assert result.provider.turns_played == 5
    assert_no_false_nudge(result)
    assert_status_agrees_with_findings(result)
    assert result.events_of("done")[0].data["status"] == "completed"

    # Both writers in the shared turn reported success...
    turn_results = [
        r for r in result.tool_results() if r["tool"] in ("file_data_request", "record_verdict")
    ]
    assert [(r["tool"], r["error"]) for r in turn_results] == [
        ("file_data_request", False),
        ("record_verdict", False),
    ]

    # ...and both writes are on disk, exactly once each.
    assert len(result.data_requests) == 1
    assert result.data_requests[0].status == "open"
    assert str(result.data_requests[0].id) in turn_results[0]["result"]

    finding = result.finding
    assert finding.payload["verdict"] == "insufficient_data"
    assert finding.status == "draft"
    assert str(finding.id) in turn_results[1]["result"]
    assert_citations_grounded(finding, world.output_schema("divergence_verdict"))
    assert_auditable(result)


# ── (d) qa_review reaches `completed` ─────────────────────────────────────────
def _qa_assessment(messages) -> dict:
    reviewed = json.loads(tool_results(messages, "list_prior_findings")[0])
    verdict = next(f for f in reviewed if f["schema"] == "divergence_verdict")
    hazard = rows_of(messages, "lookup_dataset")[0]
    return {
        "schema_slug": "qa_assessment",
        "subject": {"finding_id": verdict["id"]},
        "payload": {
            "target_finding_id": verdict["id"],
            "criteria": [
                {
                    "criterion": "Verdict is consistent with the values cited",
                    "doctrine_heading": "Divergence Assessment Procedure — Step 5",
                    "met": True,
                    "note": (
                        "The vendor rating cited is "
                        f"{hazard['rating']} of vintage {hazard['vintage_year']}, which I "
                        "re-retrieved and which supports the recorded direction."
                    ),
                },
                {
                    "criterion": "Robustness across scenarios was handled",
                    "doctrine_heading": "Divergence Assessment Procedure — Step 5",
                    "met": True,
                    "note": "Both held scenarios were read and the tail spread was noted.",
                },
                {
                    "criterion": "The reason code's evidence test is satisfied",
                    "doctrine_heading": "Reason Codes — outdated_inputs",
                    "met": True,
                    "note": "The reference vintage year is cited, which is the required test.",
                },
                {
                    "criterion": "The methodology note is plain language",
                    "doctrine_heading": "Assessment Principles",
                    "met": True,
                    "note": "No tool names or internal ids appear in the note.",
                },
            ],
            "overall": "pass",
            "recommendation": (
                "Approve this verdict as recorded. The reference score's age is cited "
                "explicitly, so an approver can see the basis for the reason code without "
                "re-reading the transcript."
            ),
        },
    }


async def test_qa_review_completes_on_its_declared_terminal_tool(world):
    """The pack's other `record_finding` task type, end to end.

    `qa_review` and `evidence_extraction` are half the shipped pack's task types.
    Both declare `record_finding` as their `terminal_tool`, so before the engine
    owned the flag neither could ever report success, no matter what the model did.
    """
    reviewed = await run_happy_path(world)
    target_id = str(reviewed.finding.id)

    provider = ReplayProvider(
        [
            ScriptedTurn(
                text="Locating the finding under review.",
                tool_calls=[
                    ScriptedCall(
                        "list_prior_findings",
                        {"schema_slug": "divergence_verdict", "limit": 5},
                    )
                ],
            ),
            ScriptedTurn(
                text="Re-retrieving the values it cited.",
                tool_calls=[_lookup("hazard_scores", site_id="S-003", peril=PERIL)],
            ),
            ScriptedTurn(
                text="Recording the QA assessment.",
                tool_calls=[ScriptedCall("record_finding", _qa_assessment)],
            ),
            ScriptedTurn(text="Graded and recorded as a draft; I did not overwrite anything."),
        ]
    )
    result = await world.run(
        provider=provider,
        task_type="qa_review",
        task_input={"finding_id": target_id},
    )

    assert result.run.status == "completed", result.run.error
    assert result.provider.turns_played == 4
    assert result.run.iterations == 4
    assert_no_false_nudge(result)
    assert_status_agrees_with_findings(result)

    finding = result.finding
    assert finding.schema_slug == "qa_assessment"
    assert finding.status == "draft"
    assert finding.payload["target_finding_id"] == target_id
    assert finding.payload["overall"] == "pass"
    assert len(finding.payload["criteria"]) == 4

    from bench.engine.validation import validate_payload

    assert validate_payload(finding.payload, world.output_schema("qa_assessment")) == []
    # Reviewing is not re-deciding: the finding under review is untouched.
    assert [f.id for f in await world.findings_in_project()] == [reviewed.finding.id, finding.id]
    assert result.events_of("done")[0].data["status"] == "completed"
    assert_auditable(result)


# ── the other half of the flag: an unvalidated write is not a terminal one ────
GUARD_SCHEMA = json.dumps(
    {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Guard verdict",
        "type": "object",
        "required": ["answer"],
        "additionalProperties": False,
        "properties": {"answer": {"enum": ["yes", "no"]}},
    }
)

GUARD_PACK_YAML = """\
pack: guard-test
version: 0.1.0
display_name: Guard Test
description: A one-task pack that enables draft_section alongside a validated terminal tool.
doctrine:
  - doctrine/01-rules.md
task_types:
  - slug: guarded_verdict
    display_name: Guarded verdict
    shape: verdict
    input_schema:
      subject_id: { type: string, description: "Anything" }
    output_schema: schemas/guard_verdict.schema.json
    terminal_tool: record_verdict
    tools: [draft_section, record_verdict]
    output_contract: A schema-validated yes/no answer via record_verdict.
    instructions: Answer yes or no and record it with record_verdict.
"""

DRAFT_MARKDOWN = (
    "## Working notes\n\nThis is a drafted section of prose. It is long enough to satisfy the "
    "tool's minimum length, and it is deliberately not a validated verdict: no schema was "
    "checked, no citation was cross-referenced, nothing here was graded."
)


async def _guard_world(tmp_path):
    """A world on a minimal pack whose terminal tool is validated, plus draft_section."""
    pack_dir = tmp_path / "guard-pack"
    (pack_dir / "doctrine").mkdir(parents=True)
    (pack_dir / "schemas").mkdir()
    (pack_dir / "pack.yaml").write_text(GUARD_PACK_YAML)
    (pack_dir / "doctrine" / "01-rules.md").write_text("# Rules\n\nAnswer yes or no.\n")
    (pack_dir / "schemas" / "guard_verdict.schema.json").write_text(GUARD_SCHEMA)
    return await build_world(tmp_path / "guard.db", pack_dir=pack_dir)


async def test_draft_section_does_not_satisfy_a_validated_terminal_tool(tmp_path):
    """`draft_section` used to set the terminal flag unconditionally.

    On a task whose declared `terminal_tool` validates a schema, that let a slab
    of unvalidated markdown mark the run `completed` — advertising a graded
    result that was never graded. The flag now comes from the task's declaration,
    so only the declared tool can satisfy it.
    """
    world = await _guard_world(tmp_path)
    try:
        drafted = await world.run(
            provider=ReplayProvider(
                [
                    ScriptedTurn(
                        text="Writing this up as prose instead.",
                        tool_calls=[
                            ScriptedCall(
                                "draft_section",
                                {
                                    "deliverable_slug": "notes",
                                    "section_slug": "working",
                                    "markdown": DRAFT_MARKDOWN,
                                },
                            )
                        ],
                    ),
                    ScriptedTurn(text="That is my answer."),  # tries to stop early
                    ScriptedTurn(text="Standing down."),  # answers the nudge
                ]
            ),
            task_type="guarded_verdict",
            task_input={"subject_id": "anything"},
        )

        # The draft itself succeeded and is on disk — it just is not a verdict.
        assert drafted.tool_errors == []
        assert [f.schema_slug for f in drafted.findings] == ["draft_section"]
        assert drafted.run.status == "completed_without_output"
        assert drafted.run.error is None
        # And the engine correctly asked for the tool that WAS declared terminal.
        nudges = [
            m.content
            for call in drafted.provider.calls
            for m in call.messages
            if m.role == "user" and "You have not recorded your result" in (m.content or "")
        ]
        assert len(nudges) == 1 and "`record_verdict`" in nudges[0]

        # The pair: the declared terminal tool does satisfy it.
        recorded = await world.run(
            provider=ReplayProvider(
                [
                    ScriptedTurn(
                        text="Recording the answer properly.",
                        tool_calls=[
                            ScriptedCall(
                                "record_verdict",
                                {
                                    "schema_slug": "guard_verdict",
                                    "subject": {"subject_id": "anything"},
                                    "payload": {"answer": "yes"},
                                },
                            )
                        ],
                    ),
                    ScriptedTurn(text="Recorded as a draft."),
                ]
            ),
            task_type="guarded_verdict",
            task_input={"subject_id": "anything"},
        )
        assert recorded.run.status == "completed", recorded.run.error
        assert recorded.provider.turns_played == 2
        assert_no_false_nudge(recorded)
        assert_status_agrees_with_findings(recorded)
        assert recorded.finding.schema_slug == "guard_verdict"
    finally:
        await world.aclose()
