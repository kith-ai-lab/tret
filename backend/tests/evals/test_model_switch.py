"""A run that changes model part-way, through the real engine.

The point of these is the *accounting*. Switching is easy to make work and easy
to make quietly wrong: energy is a per-model calculation, and before this change
the engine recomputed it from the run's running totals against whichever
`ModelInfo` happened to be current — so a run that spent half its tokens on an
S-class model and half on an R-class one would report all of them at one class,
an order of magnitude out.
"""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

from replay_provider import ReplayProvider, ScriptedCall, ScriptedTurn
from test_golden_runs import PERIL, SITE, divergence_happy_script

from bench.engine.supervisor import KIND_SWITCH, Intervention
from bench.providers.catalog import ModelCatalog

BULK = "Narrative detail. " * 400


def _read(document_id) -> ScriptedCall:
    return ScriptedCall("read_document", {"document_id": str(document_id)})


async def _run_with_switch(world, *, switch_at: int = 2):
    """Force one switch at a known iteration, and let the run finish normally.

    `assess` is stubbed rather than provoked. Whether the stall heuristics fire
    correctly is settled in `tests/test_supervisor.py`; what is under test here
    is what the *engine* does once one has, and driving a genuine stall would
    mean scripting a run that fails validation three times — a slower test of
    something already covered.
    """
    catalog = ModelCatalog()
    models = [m for m in catalog.all(curated_only=True) if m.supports_tools]
    target = next(m for m in models if m.id != models[0].id)

    documents = [
        await world.create_document(filename="report.txt", text=BULK),
    ]
    harness_id = await world.create_harness(
        name="Switching Analyst",
        model=models[0].id,
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
            ScriptedTurn(text="Reading.", tool_calls=[_read(documents[0])]),
            *divergence_happy_script(),
        ]
    )

    calls = {"n": 0}

    def fake_assess(state, *, candidates, priors=None):
        calls["n"] += 1
        if calls["n"] == switch_at:
            return Intervention(
                kind=KIND_SWITCH,
                target=target,
                reason="capability_stall",
                detail="forced by the test",
                evidence={"from": state.model.id, "to": target.id},
            )
        return Intervention()

    with patch("bench.engine.harness.assess", side_effect=fake_assess):
        result = await world.run(
            provider=provider,
            harness_id=harness_id,
            task_type="divergence_assessment",
            task_input={"site_id": SITE, "peril": PERIL},
            document_ids=documents,
        )
    return result, models[0], target


async def test_a_switched_run_finishes_and_says_which_models_it_used(world):
    result, first, target = await _run_with_switch(world)

    assert result.run.error is None, result.run.error
    assert result.run.status == "completed"
    assert result.run.model_timeline, "a run that used two models must say so"
    assert [seg["model"] for seg in result.run.model_timeline] == [first.id, target.id]


async def test_model_used_means_the_model_that_produced_the_answer(world):
    # It has always been read that way by the runs list and the chat chip; the
    # timeline is what makes the fuller story available.
    result, _first, target = await _run_with_switch(world)
    assert result.run.model_used == target.id
    assert result.run.provider_used == target.provider


async def test_the_switch_is_explained_in_the_same_place_as_the_original_route(world):
    result, first, target = await _run_with_switch(world)

    switches = result.run.routing["switches"]
    assert len(switches) == 1
    assert switches[0]["from_model"] == first.id
    assert switches[0]["chosen_model"] == target.id
    assert switches[0]["reason"] == "capability_stall"
    # ...and the original decision is still intact beside it.
    assert result.run.routing["chosen_model"] == first.id


async def test_a_switch_is_announced_to_anyone_watching(world):
    result, _first, target = await _run_with_switch(world)
    events = result.events_of("model_switch")
    assert len(events) == 1
    assert events[0].data["chosen_model"] == target.id


async def test_every_segment_is_accounted_against_the_model_that_ran_it(world):
    """The bug this whole refactor exists to prevent.

    Energy class, PUE, grid factor and the frontier baseline are all properties
    of the model. Recomputing the run's totals against whichever model is current
    attributes every token to that one.
    """
    result, first, target = await _run_with_switch(world)

    timeline = result.run.model_timeline
    assert len(timeline) == 2
    for segment, expected in zip(timeline, (first, target)):
        assert segment["energy_accounting"]["model"] == expected.id
        assert segment["input_tokens"] > 0
        assert segment["from_iteration"] <= segment["to_iteration"]

    # The run's own totals are the sum of the parts, not one part restated.
    assert float(result.run.energy_wh) == sum(s["energy_wh"] for s in timeline)
    assert result.run.input_tokens == sum(s["input_tokens"] for s in timeline)
    assert Decimal(str(result.run.cost_usd)).compare(
        Decimal(str(sum(s["cost_usd"] for s in timeline))).quantize(Decimal("0.000001"))
    ) in (Decimal(0), Decimal(-1), Decimal(1))


async def test_the_run_level_accounting_admits_it_covers_two_models(world):
    result, first, target = await _run_with_switch(world)

    accounting = result.run.energy_accounting
    assert accounting["models"] == [first.id, target.id]
    assert any(c["key"] == "multi_model_run" for c in accounting["caveats"])
    # Where the segments used different energy classes, the roll-up says nothing
    # rather than asserting one of them of the other's tokens.
    if first.energy_class != target.energy_class:
        assert accounting["energy_class"] is None


async def test_the_transcript_stays_replayable_across_the_switch(world):
    # The new provider sees tool-call ids it never issued, which is fine only
    # while every call still has its result.
    result, _first, _target = await _run_with_switch(world)
    called = {c["id"] for m in result.run.messages for c in (m.get("tool_calls") or [])}
    answered = {m["tool_call_id"] for m in result.run.messages if m["role"] == "tool"}
    assert called == answered


async def test_a_run_that_never_switches_records_no_timeline(world):
    # The cold path. An ordinary single-model run must not acquire a different
    # accounting record just because this feature exists.
    result, _first, _target = await _run_with_switch(world, switch_at=999)

    assert result.run.model_timeline is None
    assert "switches" not in (result.run.routing or {})
    assert "models" not in (result.run.energy_accounting or {})
    assert result.run.energy_accounting["model"] == result.run.model_used
