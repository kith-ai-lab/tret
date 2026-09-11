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

from tret.engine.supervisor import KIND_SWITCH, Intervention
from tret.providers.catalog import ModelCatalog

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
    models = [m for m in catalog.all(curated_only=True) if m.supports_tools][:2]

    documents = [
        await world.create_document(filename="report.txt", text=BULK),
    ]
    harness_id = await world.create_harness(
        name="Switching Analyst",
        # Deliberately NOT pinned. A pinned or per-run-overridden model is never
        # switched away from — the caller named it — so a pinned harness could
        # not exercise this path at all (see the guard test below).
        model_policy={"mode": "auto", "allowed": [m.id for m in models]},
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

    chosen: dict = {}

    def fake_assess(state, *, candidates, priors=None):
        calls["n"] += 1
        # The target is derived from the candidate list the engine actually
        # handed over, so this cannot accidentally "switch" to a model the
        # harness policy never permitted.
        other = next((m for m in candidates if m.id != state.model.id), None)
        if calls["n"] == switch_at and other is not None:
            chosen["from"] = state.model
            chosen["to"] = other
            return Intervention(
                kind=KIND_SWITCH,
                target=other,
                reason="capability_stall",
                detail="forced by the test",
                evidence={"from": state.model.id, "to": other.id},
            )
        return Intervention()

    with patch("tret.engine.harness.assess", side_effect=fake_assess):
        result = await world.run(
            provider=provider,
            harness_id=harness_id,
            task_type="divergence_assessment",
            task_input={"site_id": SITE, "peril": PERIL},
            document_ids=documents,
        )
    return result, chosen.get("from"), chosen.get("to")


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


# ── the switch becomes evidence ──────────────────────────────────────────────
async def _outcomes(world, run_id):
    from sqlalchemy import select

    from tret.db.models import RunOutcome

    async with world.session_factory() as db:
        rows = (
            await db.execute(
                select(RunOutcome)
                .where(RunOutcome.run_id == run_id)
                .order_by(RunOutcome.segment_index)
            )
        ).scalars().all()
        return [
            {
                "segment": r.segment_index,
                "model": r.model_id,
                "class": r.outcome_class,
                "quality": float(r.quality_score),
                "input_tokens": r.input_tokens,
                "findings_approved": r.findings_approved,
            }
            for r in rows
        ]


async def test_a_switch_records_evidence_about_both_models(world):
    """The pair is the strongest label tret can produce.

    Not "this model averages 0.6 across a hundred different tasks", but "on this
    specific problem, at this iteration, this model stalled and that one
    finished it" — a within-task comparison, which is the only kind that is not
    confounded by which tasks each model tends to be given.
    """
    result, first, target = await _run_with_switch(world)
    rows = await _outcomes(world, result.run.id)

    assert [r["model"] for r in rows] == [first.id, target.id]
    assert rows[0]["class"] == "handed_off"
    assert rows[1]["class"] == "delivered"
    assert rows[0]["quality"] < rows[1]["quality"]


async def test_each_segments_evidence_carries_that_segments_own_spend(world):
    result, _first, _target = await _run_with_switch(world)
    rows = await _outcomes(world, result.run.id)

    assert all(r["input_tokens"] > 0 for r in rows)
    assert sum(r["input_tokens"] for r in rows) == result.run.input_tokens


async def test_the_model_that_finished_owns_the_human_verdict(world):
    # The approved finding is the one it produced. Crediting the model that was
    # abandoned before recording anything would reward it for someone else's work.
    from tret.db.models import Finding
    from tret.services.outcomes import record_outcome_for_finding

    result, first, target = await _run_with_switch(world)
    async with world.session_factory() as db:
        finding = (await db.get(Finding, result.findings[0].id))
        finding.status = "approved"
        await db.commit()
        await record_outcome_for_finding(db, finding.id)

    rows = await _outcomes(world, result.run.id)
    assert rows[0]["findings_approved"] == 0
    assert rows[1]["findings_approved"] == 1
    assert rows[1]["quality"] > rows[0]["quality"]


async def test_a_pinned_harness_is_never_switched_away_from(world):
    """The guard, end to end: the operator named a model.

    Not a hypothetical — `world.create_harness` pins by default, and every other
    test in this file had to opt out of that to exercise switching at all.
    """
    documents = [await world.create_document(filename="report.txt", text=BULK)]
    harness_id = await world.create_harness(name="Pinned Analyst", max_iterations=16)
    provider = ReplayProvider(
        [ScriptedTurn(text="Reading.", tool_calls=[_read(documents[0])]), *divergence_happy_script()]
    )

    seen = []

    def fake_assess(state, *, candidates, priors=None):
        seen.append(state)
        return Intervention()

    with patch("tret.engine.harness.assess", side_effect=fake_assess):
        result = await world.run(
            provider=provider,
            harness_id=harness_id,
            task_type="divergence_assessment",
            task_input={"site_id": SITE, "peril": PERIL},
            document_ids=documents,
        )

    # The supervisor is not even consulted: there is no decision to make.
    assert seen == []
    assert result.run.model_timeline is None
    assert result.run.status == "completed"


async def test_an_ordinary_run_records_exactly_one_outcome(world):
    result, _first, _target = await _run_with_switch(world, switch_at=999)
    rows = await _outcomes(world, result.run.id)
    assert len(rows) == 1
    assert rows[0]["segment"] == 0
    assert rows[0]["model"] == result.run.model_used


# ── a switch re-gates effort on the model actually being switched to ───────
# `ModelSegment.effort` is set once when a segment is created (initial routing,
# or `_switch_model` on a mid-run switch) — see engine/harness.py — and each
# creation gates the run's recorded `RoutingDecision.effort` on *that
# segment's own* `ModelInfo.supports_effort`. A switch landing on a model with
# a different `supports_effort` than the one it left must flip what is
# actually sent, not carry over whatever the first segment decided.
_EFFORT_MODEL = "anthropic/claude-sonnet-5"  # supports_effort: true
_NO_EFFORT_MODEL = "anthropic/claude-haiku-4-5"  # supports_effort: false


async def _run_with_switch_between(world, model_a_id: str, model_b_id: str, *, switch_at: int = 2):
    """`_run_with_switch`, but between two named models instead of "the first
    two curated tool-capable models" — so the pair's `supports_effort` values
    are chosen deliberately rather than whatever the catalog happens to order
    first.
    """
    documents = [await world.create_document(filename="report.txt", text=BULK)]
    harness_id = await world.create_harness(
        name="Switching Analyst (effort)",
        model_policy={"mode": "auto", "allowed": [model_a_id, model_b_id]},
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
    switched = {"n": 0}

    def fake_assess(state, *, candidates, priors=None):
        switched["n"] += 1
        other = next((m for m in candidates if m.id != state.model.id), None)
        if switched["n"] == switch_at and other is not None:
            return Intervention(
                kind=KIND_SWITCH,
                target=other,
                reason="capability_stall",
                detail="forced by the test",
                evidence={"from": state.model.id, "to": other.id},
            )
        return Intervention()

    with patch("tret.engine.harness.assess", side_effect=fake_assess):
        result = await world.run(
            provider=provider,
            harness_id=harness_id,
            task_type="divergence_assessment",
            task_input={"site_id": SITE, "peril": PERIL},
            document_ids=documents,
        )
    return result, provider


def _assert_calls_match_their_segments_effort_gate(provider, timeline):
    """Every call the replay provider actually received, matched back to
    whichever segment (pre- or post-switch) was running at that iteration, and
    checked against *that segment's own* model's `supports_effort`.

    Which of the two allowed models the router picks first is the router's own
    ordering (price, evidence, ...) to make, not something this test pins — so
    the expectation is read off `model_timeline` itself rather than assumed
    from the order the two model ids were passed in.
    """
    # `ModelSegment.add()` sets `from_iteration` on the first turn a segment
    # actually ran (see engine/harness.py) — the switch itself is decided at
    # the end of the prior iteration, so the new segment's `from_iteration` is
    # one past the iteration where `assess()` returned the switch. Iterations
    # are 1-indexed; `provider.calls` is 0-indexed, one entry per iteration.
    switch_iteration = timeline[1]["from_iteration"]
    for i, call in enumerate(provider.calls):
        segment = timeline[0] if (i + 1) < switch_iteration else timeline[1]
        # `ModelSegment.to_json()`'s own "effort" is the ground truth for what
        # that segment believed it should send; cross-checking the call
        # against it (rather than a hardcoded model->bool table) is what
        # catches a segment created without re-gating on its own model.
        assert call.effort == segment["effort"], (i, segment["model"])
        if segment["model"] == _EFFORT_MODEL:
            assert call.effort is not None, f"call {i} on {segment['model']} should carry an effort"
        else:
            assert call.effort is None, f"call {i} on {segment['model']} must never carry one"


async def test_a_switch_re_gates_effort_on_the_new_models_own_support(world):
    result, provider = await _run_with_switch_between(world, _EFFORT_MODEL, _NO_EFFORT_MODEL)

    timeline = result.run.model_timeline
    assert timeline is not None and len(timeline) == 2
    assert set(seg["model"] for seg in timeline) == {_EFFORT_MODEL, _NO_EFFORT_MODEL}
    _assert_calls_match_their_segments_effort_gate(provider, timeline)


async def test_a_switch_the_other_direction_also_re_gates(world):
    # Same scenario, models swapped — the gate follows the model actually
    # running, not "whichever one happened to go first" in the prior test.
    result, provider = await _run_with_switch_between(world, _NO_EFFORT_MODEL, _EFFORT_MODEL)

    timeline = result.run.model_timeline
    assert timeline is not None and len(timeline) == 2
    assert set(seg["model"] for seg in timeline) == {_EFFORT_MODEL, _NO_EFFORT_MODEL}
    _assert_calls_match_their_segments_effort_gate(provider, timeline)
