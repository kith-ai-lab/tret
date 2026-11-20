"""Learning round, run loop (R01, R04, R10, R13 wiring, R18, R25).

Pure pieces (degenerate detector, seed upgrade, partial-results text, unvetted
diff) are unit tests; the loop behaviours drive the real engine through the
golden world with a scripted provider (tests/evals).
"""
from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

EVALS = Path(__file__).parent / "evals"
if str(EVALS) not in sys.path:
    sys.path.insert(0, str(EVALS))

from golden_world import GOLDEN_MODEL, build_world, install_sqlite_type_shims  # noqa: E402
from replay_provider import ReplayProvider, ScriptedCall, ScriptedTurn  # noqa: E402
from sqlalchemy import select  # noqa: E402

import tret.engine.harness as harness_module  # noqa: E402
from tret.db.models import Harness, Workspace  # noqa: E402
from tret.engine.degenerate import (  # noqa: E402
    DegenerateDetector,
    find_degenerate,
    strip_degenerate_tail,
)
from tret.engine.harness import HarnessEngine  # noqa: E402
from tret.engine.tools import _partial_results_text, _summarize_child  # noqa: E402
from tret.providers.base import Msg, ToolCall  # noqa: E402
from tret.providers.catalog import ModelCatalog  # noqa: E402
from tret.services.workspace import (  # noqa: E402
    SUBAGENT_HARNESS_LOOP_UPGRADES,
    seed_subagent_harness,
    upgrade_seeded_loop_config,
)

install_sqlite_type_shims()


@pytest.fixture
async def world(tmp_path):
    w = await build_world(tmp_path / "learning.db")
    try:
        yield w
    finally:
        await w.aclose()


async def _set_guardrails(world, **values) -> None:
    async with world.session_factory() as db:
        ws = await db.get(Workspace, world.workspace_id)
        ws.settings = {**(ws.settings or {}), "guardrails": values}
        await db.commit()


async def _freeform(world, turns, *, tool_names=None, max_iterations=12, loop_config_extra=None):
    provider = ReplayProvider(turns)
    harness_id = await world.create_harness(
        tool_names=tool_names or ["lookup_dataset"],
        max_iterations=max_iterations,
        loop_config_extra=loop_config_extra,
    )
    result = await world.run(
        provider=provider,
        harness_id=harness_id,
        task_type="freeform",
        task_input={"message": "What is the flood hazard score for site X?"},
    )
    return result, provider


FABRICATED = ScriptedTurn(
    text="Looking it up.", tool_calls=[ScriptedCall("lookup_dataset", {"dataset": "hazard_scores", "filters": {}})]
)


# ── R10 detector ──────────────────────────────────────────────────────────────
def test_detector_flags_pad_runs_and_repeated_units():
    assert find_degenerate("Intro. " + "[PAD]" * 60).pattern == "[PAD]"
    assert find_degenerate("so " + "ha ha " * 70) is not None
    assert find_degenerate("x" + "-" * 2100) is not None  # a 2,001+ char rule


def test_detector_leaves_normal_text_and_long_tables_alone():
    table = "| a | b |\n|---|---|\n" + "".join(f"| {i} | row {i} |\n" for i in range(400))
    assert find_degenerate(table) is None
    assert find_degenerate("-" * 100 + "\n" + "word " * 30) is None
    assert find_degenerate("The quick brown fox. " * 300) is None  # long phrase, not <=16 chars repeated 50x... 21 chars
    assert find_degenerate("ab" * 40) is None  # under 50 repeats


def test_detector_trips_while_streaming_and_strips_the_tail():
    det = DegenerateDetector()
    text = "A real sentence. " + "[PAD]" * 200
    hit = None
    for i in range(0, len(text), 7):
        hit = det.feed(text[i : i + 7])
        if hit:
            break
    assert hit is not None
    assert strip_degenerate_tail(text, hit) == "A real sentence."


# ── R01 / R04 seed upgrade ────────────────────────────────────────────────────
def test_seed_upgrade_only_touches_values_still_at_the_old_default():
    old = {"max_iterations": 12, "max_output_tokens": 4096, "temperature": 0.2}
    new = upgrade_seeded_loop_config(old, SUBAGENT_HARNESS_LOOP_UPGRADES)
    assert new == {"max_iterations": 30, "max_output_tokens": 8192, "temperature": 0.2}
    edited = {"max_iterations": 20, "max_output_tokens": 4096}
    assert upgrade_seeded_loop_config(edited, SUBAGENT_HARNESS_LOOP_UPGRADES) == {
        "max_iterations": 20,
        "max_output_tokens": 8192,
    }
    assert upgrade_seeded_loop_config({"max_iterations": 20, "max_output_tokens": 6000}, SUBAGENT_HARNESS_LOOP_UPGRADES) is None


async def test_seed_subagent_harness_upgrades_an_untouched_row_and_leaves_an_edited_one(world):
    async with world.session_factory() as db:
        db.add(
            Harness(
                workspace_id=world.workspace_id,
                name="Subagent",
                task_profile="subagent",
                model_policy={"mode": "auto"},
                loop_config={"max_iterations": 12, "max_output_tokens": 4096, "temperature": 0.2},
                tool_names=[],
            )
        )
        await db.commit()
        await seed_subagent_harness(db, world.workspace_id)
        await db.commit()
        row = (await db.execute(select(Harness).where(Harness.task_profile == "subagent"))).scalars().one()
        assert row.loop_config["max_iterations"] == 30
        assert row.loop_config["max_output_tokens"] == 8192
        row.loop_config = {"max_iterations": 7, "max_output_tokens": 5000}
        await db.commit()
        await seed_subagent_harness(db, world.workspace_id)
        await db.commit()
        await db.refresh(row)
        assert row.loop_config == {"max_iterations": 7, "max_output_tokens": 5000}


# ── R01 truncation ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("stop", ["max_tokens", "length"])
async def test_a_reply_cut_off_at_the_output_limit_is_recorded(world, stop):
    result, provider = await _freeform(
        world, [ScriptedTurn(text="The report begins and then", stop_reason=stop)]
    )
    assert result.run.status == "completed"
    assert result.run.stop_reason == "max_tokens"
    assert result.run.guardrails["truncated"]["max_output_tokens"] == provider.calls[0].max_tokens
    assert result.events_of("reply_truncated")[0].data["max_output_tokens"] == provider.calls[0].max_tokens


async def test_a_normal_reply_has_no_stop_reason_and_records_settings(world):
    result, _ = await _freeform(world, [ScriptedTurn(text="All fine.")])
    assert result.run.stop_reason is None
    assert "truncated" not in result.run.guardrails
    assert result.run.guardrails["settings"]["member"]["number_check"] is True
    assert result.run.guardrails["overhead"]["repair_turns"] == 0


async def test_a_truncated_tool_call_gets_a_tool_error_pointing_at_draft_file(world):
    cut = ScriptedTurn(
        text="",
        tool_calls=[ScriptedCall("lookup_dataset", {"_raw": '{"dataset": "haz'})],
        stop_reason="max_tokens",
    )
    result, _ = await _freeform(world, [cut, ScriptedTurn(text="Retrying in pieces.")])
    errors = result.tool_errors
    assert errors and "cut off at the output limit" in errors[0]["result"]
    assert "draft_file" in errors[0]["result"]
    assert result.run.status == "completed"


def test_output_limit_is_clamped_to_a_known_model_ceiling_only():
    info = SimpleNamespace(max_output_tokens=8000)
    assert harness_module._clamp_output_tokens(16384, info) == 8000
    assert harness_module._clamp_output_tokens(4096, info) == 4096
    assert harness_module._clamp_output_tokens(16384, SimpleNamespace(max_output_tokens=None)) == 16384


# ── R04 out of steps ──────────────────────────────────────────────────────────
async def test_hitting_the_step_cap_is_failed_with_out_of_steps(world):
    result, _ = await _freeform(world, [FABRICATED, FABRICATED], max_iterations=2)
    assert result.run.status == "failed"
    assert result.run.stop_reason == "out_of_steps"
    assert result.run.error == "Ran out of steps (2 of 2). Partial results kept."


def _child(**kw):
    messages = [
        {"role": "user", "content": "brief", "tool_calls": []},
        {
            "role": "assistant",
            "content": "Read the first two files; the third is pending.",
            "tool_calls": [
                {"id": "1", "name": "read_connected_file", "arguments": {"path": "a/b.docx"}},
                {"id": "2", "name": "read_document", "arguments": {"document_id": "d1"}},
            ],
        },
    ]
    base = dict(
        status="failed",
        model_used="m",
        cost_usd=0,
        delegated_cost_usd=0,
        energy_wh=None,
        energy_accounting={},
        error="Ran out of steps (30 of 30). Partial results kept.",
        stop_reason="out_of_steps",
        messages=messages,
        grounding=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_parent_receives_partial_results_for_an_out_of_steps_child():
    text = _partial_results_text(_child())
    assert text.startswith("[ran out of steps after 1 steps — partial results below]")
    assert "Read the first two files" in text
    assert "read_connected_file(path=a/b.docx)" in text
    prepared = SimpleNamespace(child_id="c", kind="subagent")
    result = _summarize_child(prepared, _child(), [])
    assert result["output"].startswith("[ran out of steps")
    assert result["stop_reason"] == "out_of_steps" and result["partial"] is True


# ── R10 degenerate output through the engine ──────────────────────────────────
async def test_a_degenerate_reply_with_nobody_to_retry_on_fails_the_run(world):
    result, _ = await _freeform(world, [ScriptedTurn(text="Start. " + "[PAD]" * 120)])
    assert result.run.status == "failed"
    assert result.run.stop_reason == "degenerate_output"
    assert result.run.guardrails["degenerate"]["pattern"] == "[PAD]"
    assert "[PAD]" not in "".join(m.get("content") or "" for m in result.run.messages[-1:])


async def test_a_degenerate_reply_is_retried_once_on_another_model(world):
    provider = ReplayProvider(
        [ScriptedTurn(text="Start. " + "[PAD]" * 120), ScriptedTurn(text="A clean answer.")]
    )
    harness_id = await world.create_harness(
        tool_names=["lookup_dataset"],
        model_policy={"mode": "auto", "allowed": [GOLDEN_MODEL]},
    )
    alt = dataclasses.replace(ModelCatalog().get(GOLDEN_MODEL), id="golden/alt", wire_id="alt")

    async def _other(self, **kw):
        return alt

    with patch.object(HarnessEngine, "_degenerate_retry_model", _other):
        result = await world.run(
            provider=provider,
            harness_id=harness_id,
            task_type="freeform",
            task_input={"message": "hello"},
        )
    assert result.run.status == "completed", result.run.error
    assert result.run.stop_reason is None
    assert result.run.guardrails["degenerate"]["retried_on"] == "golden/alt"
    assert result.run.model_used == "golden/alt"


# ── R25 guardrail settings honoured ───────────────────────────────────────────
async def _fabricating_run(world, **guard):
    await _set_guardrails(world, **guard)
    turns = [
        FABRICATED,
        ScriptedTurn(text="the retrieved record shows score 58 from vintage 2021"),
        ScriptedTurn(text="I could not find a score."),
    ]
    return await _freeform(world, turns)


async def test_number_check_off_records_status_off_and_never_nudges(world):
    result, provider = await _fabricating_run(world, number_check=False)
    assert result.run.grounding["status"] == "off"
    assert result.run.grounding["checked"] is False
    assert len(provider.calls) == 2


async def test_repair_attempts_zero_means_no_grounding_nudge(world):
    result, provider = await _fabricating_run(world, repair_attempts=0)
    assert len(provider.calls) == 2
    assert result.run.grounding["status"] == "unresolved"
    assert result.run.grounding["unsupported_detail"]  # D's where/line/context


async def test_default_repair_attempts_still_nudges_and_measures_overhead(world):
    result, provider = await _fabricating_run(world)
    assert len(provider.calls) == 3
    assert result.run.grounding["status"] == "repaired"
    overhead = result.run.guardrails["overhead"]
    assert overhead["repair_turns"] == 1
    assert overhead["repair_input_tokens"] > 0 and overhead["repair_output_tokens"] > 0


async def test_max_subagents_zero_withholds_the_delegation_tools(world):
    await _set_guardrails(world, max_subagents=0)
    result, provider = await _freeform(
        world, [ScriptedTurn(text="ok")], tool_names=["lookup_dataset", "spawn_subagent", "delegate_parallel"]
    )
    offered = provider.calls[0].tool_names
    assert "spawn_subagent" not in offered and "delegate_parallel" not in offered
    assert "lookup_dataset" in offered
    withheld = [e for e in result.events_of("tools_withheld") if e.data["reason"] == "guardrail_setting"]
    assert withheld


async def test_spend_cap_narrows_the_harness_cap(world):
    await _set_guardrails(world, spend_cap_usd=0.0000001)
    result, _ = await _freeform(world, [FABRICATED, ScriptedTurn(text="never reached")])
    assert result.run.status == "failed"
    assert result.run.error.startswith("cost_cap_exceeded")


# ── R13 html repair wiring (html_check is D's module; faked here) ─────────────
def _fake_html_module(statuses):
    calls = {"n": 0}

    def check(reply):
        status = statuses[min(calls["n"], len(statuses) - 1)]
        calls["n"] += 1
        issues = [{"block": 1, "kind": "unclosed", "detail": "x", "line": 2}] if status == "issues" else []
        return {"status": status, "blocks": 1, "issues": issues}

    return SimpleNamespace(check_reply_html=check, html_repair_nudge=lambda r: "Reissue the file.")


async def test_a_broken_html_reply_gets_one_repair_nudge_and_is_recorded_repaired(world):
    fake = _fake_html_module(["issues", "ok"])
    with patch.object(harness_module, "_html_check_module", lambda: fake):
        result, provider = await _freeform(
            world, [ScriptedTurn(text="```html\n<table>\n```"), ScriptedTurn(text="```html\n<p>ok</p>\n```")]
        )
    assert len(provider.calls) == 2
    rec = result.run.guardrails["html_check"]
    assert rec["status"] == "ok" and rec["repaired"] is True and rec["first_issues"]
    assert result.run.guardrails["overhead"]["repair_turns"] == 1


async def test_html_check_off_or_zero_repairs_sends_no_nudge(world):
    fake = _fake_html_module(["issues"])
    await _set_guardrails(world, repair_attempts=0)
    with patch.object(harness_module, "_html_check_module", lambda: fake):
        result, provider = await _freeform(world, [ScriptedTurn(text="```html\n<table>\n```")])
    assert len(provider.calls) == 1
    assert result.run.guardrails["html_check"]["status"] == "issues"
    assert result.run.guardrails["html_check"]["repaired"] is False


async def test_a_truncated_reply_is_not_sent_a_repair_nudge(world):
    fake = _fake_html_module(["issues"])
    with patch.object(harness_module, "_html_check_module", lambda: fake):
        result, provider = await _freeform(
            world, [ScriptedTurn(text="```html\n<html><body>", stop_reason="max_tokens")]
        )
    assert len(provider.calls) == 1
    assert result.run.stop_reason == "max_tokens"


# ── unvetted figures ──────────────────────────────────────────────────────────
def test_unvetted_figures_are_those_supported_only_by_run_analysis_results():
    messages = [
        Msg(role="user", content="question"),
        Msg(
            role="assistant",
            tool_calls=[ToolCall("a1", "run_analysis", {"code": "x"}), ToolCall("l1", "lookup_dataset", {})],
        ),
        Msg(role="tool", content=json.dumps({"rows": [{"total": 4321}]}), tool_call_id="a1"),
        Msg(role="tool", content=json.dumps({"rows": [{"score": 58}]}), tool_call_id="l1"),
        Msg(role="assistant", content="Total 4321, score 58, and 9999."),
    ]
    reply = "Total 4321, score 58, and 9999."
    from tret.engine.grounding import evidence_numbers, unsupported_numbers

    kwargs = dict(system="", task_input={}, retrieved=[])
    unsupported_all = unsupported_numbers(reply, evidence_numbers(messages=messages, **kwargs))
    assert "9999" in unsupported_all and "4321" not in unsupported_all
    unvetted = harness_module._unvetted_figures(
        reply, messages=messages, unsupported_all=unsupported_all, **kwargs
    )
    assert unvetted == ["4321"]


def test_no_run_analysis_means_no_unvetted_figures():
    messages = [Msg(role="user", content="q"), Msg(role="assistant", content="1234")]
    assert harness_module._unvetted_figures(
        "1234", system="", messages=messages, task_input={}, retrieved=[], unsupported_all=["1234"]
    ) == []
