"""A long run that outgrows its window, through the real engine.

The unit tests in `tests/test_compaction.py` cover the planning rules. This one
covers the thing they cannot: that the engine notices, acts, still finishes, and
— the invariant the whole design rests on — leaves the persisted transcript
completely intact while doing it.

The context budget is patched rather than engineered. Whether the budget is
computed correctly is already settled in the unit tests; what is under test here
is what the engine does once it is over one, and forcing that with a real
million-token document would make this suite slow for no extra coverage.
"""
from __future__ import annotations

from unittest.mock import patch

from replay_provider import ReplayProvider, ScriptedCall, ScriptedTurn
from test_golden_runs import PERIL, SITE, divergence_happy_script

# Big enough to be worth eliding several times over.
BULK = "The site assessment narrative continues. " * 900


def _read(document_id) -> ScriptedCall:
    return ScriptedCall("read_document", {"document_id": str(document_id)})


async def _long_run(world, *, limit: int):
    """Four bulk document reads, then the real divergence script, verbatim.

    The verdict half is `divergence_happy_script()` — the same script the golden
    runs assert on — precisely because it derives its citations from what the
    tools actually returned. That is what makes the citation test below mean
    something: if compaction damaged the retrieved values, this script could not
    produce a valid verdict, and the run would fail on validation rather than on
    an assertion written to expect it.
    """
    documents = [
        await world.create_document(filename=f"report-{i}.txt", text=f"REPORT {i}\n{BULK}")
        for i in range(4)
    ]
    harness_id = await world.create_harness(
        name="Voluminous Analyst",
        tool_names=[
            "read_document",
            "search_documents",
            "lookup_dataset",
            "record_verdict",
            "file_data_request",
        ],
        max_iterations=16,
    )
    provider = ReplayProvider(
        [
            *[
                ScriptedTurn(text=f"Reading report {i}.", tool_calls=[_read(d)])
                for i, d in enumerate(documents)
            ],
            *divergence_happy_script(),
        ]
    )
    with patch("bench.engine.harness.context_budget", return_value=limit):
        result = await world.run(
            provider=provider,
            harness_id=harness_id,
            task_type="divergence_assessment",
            task_input={"site_id": SITE, "peril": PERIL},
            document_ids=documents,
        )
    return result, documents


async def test_a_run_that_outgrows_its_window_compacts_and_still_finishes(world):
    result, _ = await _long_run(world, limit=3_000)

    assert result.event_types.count("context_pressure") >= 1
    assert result.run.error is None, result.run.error
    assert any(e.data.get("kind") == "elision" for e in result.events_of("compaction"))
    assert result.run.status == "completed"
    assert result.findings, "the run still landed its verdict"


async def test_the_persisted_transcript_is_untouched_by_compaction(world):
    """The decision the whole feature rests on.

    An approver reads this transcript to decide whether to trust the finding. If
    compaction edited it, the record would say the model saw a marker where it
    actually saw a document — or worse, the reverse.
    """
    result, _ = await _long_run(world, limit=3_000)

    assert result.run.compactions  # it definitely compacted
    tool_results = [m for m in result.run.messages if m["role"] == "tool"]
    assert any(BULK[:200] in (m.get("content") or "") for m in tool_results)
    assert not any("elided by bench" in (m.get("content") or "") for m in tool_results)


async def test_the_run_records_what_it_stopped_showing_the_model(world):
    result, _ = await _long_run(world, limit=3_000)

    elisions = [c for c in result.run.compactions if c["kind"] == "elision"]
    assert elisions
    first = elisions[0]
    assert first["elided_messages"] >= 1
    assert first["elided_tools"] == ["read_document"]
    assert first["after_est_tokens"] < first["before_est_tokens"]


async def test_retrieved_values_survive_compaction_so_citations_still_validate(world):
    """The reason `lookup_dataset` is protected, demonstrated end to end.

    A citation is validated against what the tool literally returned. Elide that
    result and the model can no longer quote it, so every finding citing it fails
    — compaction would manufacture the failure it was invoked to prevent.
    """
    result, _ = await _long_run(world, limit=3_000)

    assert result.findings
    assert result.findings[0].payload["cited_values"]
    validation_errors = [
        m for m in result.run.messages
        if m["role"] == "tool" and "Validation failed" in (m.get("content") or "")
    ]
    assert validation_errors == []


async def test_a_run_that_fits_its_window_compacts_nothing(world):
    # The cold path: nothing about this feature should touch an ordinary run.
    result, _ = await _long_run(world, limit=10_000_000)

    assert result.run.compactions in (None, [])
    assert "context_pressure" not in result.event_types
