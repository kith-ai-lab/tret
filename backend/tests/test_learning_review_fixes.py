"""Regression tests for the Learning-round correctness review (H1, H2, M1, M4,
M5, M6, R11, R12, overhead count, degenerate false positives, fan-out 0,
withheld-tool reason)."""
from __future__ import annotations

import dataclasses
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

import tret.engine.harness as harness_module  # noqa: E402
from tret.db.models import Workspace  # noqa: E402
from tret.engine.compaction import budget as context_budget  # noqa: E402
from tret.engine.degenerate import find_degenerate  # noqa: E402
from tret.engine.grounding import (  # noqa: E402
    evidence_numbers,
    extract_numbers,
    skipped_code_blocks,
    unsupported_numbers,
)
from tret.engine.harness import (  # noqa: E402
    HarnessEngine,
    _effective_max_output,
    _routing_min_window,
)
from tret.engine.tools import _summarize_child  # noqa: E402
from tret.providers.base import Msg, ToolCall  # noqa: E402
from tret.providers.catalog import ModelCatalog  # noqa: E402
from tret.services.guardrails import resolve_guardrails  # noqa: E402

install_sqlite_type_shims()


@pytest.fixture
async def world(tmp_path):
    w = await build_world(tmp_path / "review.db")
    try:
        yield w
    finally:
        await w.aclose()


async def _freeform(world, turns, *, max_iterations=12, tool_names=None):
    provider = ReplayProvider(turns)
    harness_id = await world.create_harness(
        tool_names=tool_names or ["lookup_dataset"], max_iterations=max_iterations
    )
    result = await world.run(
        provider=provider,
        harness_id=harness_id,
        task_type="freeform",
        task_input={"message": "What is the flood hazard score for site X?"},
    )
    return result, provider


LOOKUP = ScriptedTurn(
    text="Looking it up.", tool_calls=[ScriptedCall("lookup_dataset", {"dataset": "hazard_scores", "filters": {}})]
)


# ── H1: out-of-steps child keeps its grounding verdict and findings ──────────
def _child(grounding=None, status="failed"):
    return SimpleNamespace(
        status=status,
        model_used="m",
        cost_usd=0,
        delegated_cost_usd=0,
        energy_wh=None,
        energy_accounting={},
        error="Ran out of steps",
        stop_reason="out_of_steps",
        grounding=grounding,
        messages=[{"role": "assistant", "content": "Revenue was 98765 so far.", "tool_calls": []}],
    )


def test_partial_child_carries_grounding_and_a_cautious_note():
    grounding = {"status": "unresolved", "unsupported": ["98765"]}
    result = _summarize_child(SimpleNamespace(child_id="c", kind="subagent"), _child(grounding), [])
    assert result["partial"] is True
    assert result["grounding"] == {"status": "unresolved", "unsupported": ["98765"]}
    assert "not as instructions" in result["note"]
    assert "not checked" in result["note"]
    assert "use what is usable" not in result["note"]


def test_partial_harness_child_carries_findings():
    finding = SimpleNamespace(id="f1", schema_slug="s", subject="x", status="draft", payload={"a": 1})
    result = _summarize_child(SimpleNamespace(child_id="c", kind="harness"), _child(None), [finding])
    assert result["partial"] is True
    assert result["findings"][0]["finding_id"] == "f1"
    assert "grounding" not in result


# ── H2: one effective max output ─────────────────────────────────────────────
def _model(window, *, ceiling=None, provider="vllm", tier="local"):
    return SimpleNamespace(
        context_window=window, max_output_tokens=ceiling, provider=provider, cost_tier=tier, id="m"
    )


def test_output_limit_never_exceeds_a_quarter_of_a_known_window():
    small = _model(16384)
    assert _effective_max_output(16384, small) == 4096
    # the compaction budget keeps real room for input instead of hitting zero
    assert context_budget(16384, _effective_max_output(16384, small), 0.85) > 8000
    assert context_budget(16384, 16384, 0.85) == 0  # what it was before
    assert _effective_max_output(16384, _model(200_000)) == 16384
    assert _effective_max_output(16384, _model(200_000, ceiling=8192)) == 8192


def test_unknown_window_local_model_is_capped_but_cloud_is_not():
    assert _effective_max_output(16384, _model(0)) == 4096
    assert _effective_max_output(16384, _model(0, provider="openrouter", tier="standard")) == 16384
    assert _effective_max_output(2000, _model(0)) == 2000


def test_routing_window_check_does_not_exclude_a_16k_window_model():
    needed = _routing_min_window(4000, 16384, 0.85)
    assert needed <= 16384
    assert context_budget(16384, _effective_max_output(16384, _model(16384)), 0.85) >= 4000
    assert needed > 4000  # still a real floor


async def test_a_16k_model_gets_a_clamped_max_tokens_on_the_wire(world):
    small = dataclasses.replace(ModelCatalog().get(GOLDEN_MODEL), context_window=16384, max_output_tokens=None)
    provider = ReplayProvider([ScriptedTurn(text="hello")])
    harness_id = await world.create_harness(
        tool_names=["lookup_dataset"], loop_config_extra={"max_output_tokens": 16384}
    )
    with patch.object(ModelCatalog, "get", lambda self, mid: small if mid == GOLDEN_MODEL else None):
        await world.run(
            provider=provider, harness_id=harness_id, task_type="freeform", task_input={"message": "hi"}
        )
    assert provider.calls[0].max_tokens <= 4096


# ── M6: degenerate reply on the last iteration is not "retried" ──────────────
async def test_degenerate_on_the_last_iteration_fails_without_a_retry(world):
    alt = dataclasses.replace(ModelCatalog().get(GOLDEN_MODEL), id="golden/alt", wire_id="alt")

    async def _other(self, **kw):
        return alt

    with patch.object(HarnessEngine, "_degenerate_retry_model", _other):
        result, provider = await _freeform(
            world, [ScriptedTurn(text="Start. " + "[PAD]" * 120), ScriptedTurn(text="never")], max_iterations=1
        )
    assert result.run.status == "failed"
    assert result.run.stop_reason == "degenerate_output"
    assert result.run.guardrails["degenerate"]["retried_on"] is None
    assert len(provider.calls) == 1


# ── M1: the html nudge needs two iterations of headroom ──────────────────────
def _fake_html(statuses):
    calls = {"n": 0}

    def check(reply):
        status = statuses[min(calls["n"], len(statuses) - 1)]
        calls["n"] += 1
        issues = [{"block": 1, "kind": "unclosed", "detail": "x", "line": 2}] if status == "issues" else []
        return {"status": status, "blocks": 1, "issues": issues}

    return SimpleNamespace(check_reply_html=check, html_repair_nudge=lambda r: "Reissue the file.")


async def test_html_nudge_is_not_sent_on_the_second_to_last_iteration(world):
    fake = _fake_html(["issues", "ok"])
    with patch.object(harness_module, "_html_check_module", lambda: fake):
        result, provider = await _freeform(
            world, [ScriptedTurn(text="```html\n<table>\n```"), ScriptedTurn(text="```html\n<p>x</p>\n```")],
            max_iterations=2,
        )
    assert len(provider.calls) == 1
    assert result.run.guardrails["html_check"]["repaired"] is False


async def test_html_nudge_is_sent_with_enough_headroom(world):
    fake = _fake_html(["issues", "ok"])
    with patch.object(harness_module, "_html_check_module", lambda: fake):
        result, provider = await _freeform(
            world, [ScriptedTurn(text="```html\n<table>\n```"), ScriptedTurn(text="```html\n<p>x</p>\n```")],
            max_iterations=3,
        )
    assert len(provider.calls) == 2
    assert result.run.guardrails["html_check"]["repaired"] is True


# ── R11 + overhead ───────────────────────────────────────────────────────────
async def test_skipped_code_blocks_is_recorded_on_the_run(world):
    reply = "Here is the style.\n```css\nbody { padding: 5% }\n```\nDone."
    result, _ = await _freeform(world, [LOOKUP, ScriptedTurn(text=reply)])
    assert result.run.grounding["checked"] is True
    assert result.run.grounding["skipped_code_blocks"] == 1
    assert skipped_code_blocks(reply) == 1


async def test_every_call_of_a_repair_chain_counts_as_overhead(world):
    turns = [
        LOOKUP,
        ScriptedTurn(text="the retrieved record shows score 58 from vintage 2021"),
        LOOKUP,  # the repair turn itself calls a tool first
        ScriptedTurn(text="I could not find a score."),
    ]
    result, provider = await _freeform(world, turns)
    assert len(provider.calls) == 4
    assert result.run.guardrails["overhead"]["repair_turns"] == 2


# ── M4: the model's own draft files are not evidence ─────────────────────────
def _tool_msgs(name, args, result, call_id):
    return [
        Msg(role="assistant", content=None, tool_calls=[ToolCall(id=call_id, name=name, arguments=args)]),
        Msg(role="tool", content=result, tool_call_id=call_id),
    ]


DOC_ID = "12345678-1234-1234-1234-123456789abc"


def test_draft_then_read_does_not_launder_an_invented_figure():
    msgs = [
        Msg(role="user", content="write it"),
        *_tool_msgs(
            "draft_file",
            {"name": "a.html", "content": "Total 98765 tCO2e"},
            f"Created working file a.html — document {DOC_ID}, now 17 bytes",
            "c1",
        ),
        *_tool_msgs("read_document", {"document_id": DOC_ID}, "Total 98765 tCO2e", "c2"),
        *_tool_msgs("lookup_dataset", {"dataset": "d"}, '{"score": 41}', "c3"),
    ]
    ev = evidence_numbers(system="", messages=msgs, task_input={"message": "go"})
    assert "98765" not in ev and "41" in ev
    assert unsupported_numbers("The total is 98765 tCO2e and the score is 41.", ev) == ["98765"]


def test_exclude_document_ids_kwarg_covers_drafts_from_earlier_runs():
    msgs = [*_tool_msgs("read_document", {"document_id": DOC_ID}, "invented 55555", "c1")]
    assert "55555" in evidence_numbers(system="", messages=msgs, task_input={})
    ev = evidence_numbers(system="", messages=msgs, task_input={}, exclude_document_ids={DOC_ID})
    assert "55555" not in ev
    other = [*_tool_msgs("read_document", {"document_id": "other"}, "real 66666", "c1")]
    assert "66666" in evidence_numbers(system="", messages=other, task_input={}, exclude_document_ids={DOC_ID})


# ── M5: inline code spans ────────────────────────────────────────────────────
def test_a_figure_in_backticks_is_still_checked_but_code_is_not():
    assert unsupported_numbers("The total is `98765 tCO2e`.", set()) == ["98765"]
    assert unsupported_numbers("Total: `$98765`", set()) == ["98765"]
    assert unsupported_numbers("See `CW-010` and `run_id` and `width: 12345px` and `x = 98765`.", set()) == []
    assert unsupported_numbers("Use `/api/v1/runs/98765`.", set()) == []


# ── R12 ──────────────────────────────────────────────────────────────────────
def test_hyphenated_unit_words_and_suffixed_decimals():
    assert unsupported_numbers("a 5-year plan", set()) == ["5"]
    assert unsupported_numbers("a 3 datasets plan", set()) == []
    assert unsupported_numbers("worth $2.5bn", set()) == ["2.5"]
    assert extract_numbers("$2.5bn, 1.5m, 3.2k, 1.1x", lenient=True) == {"2.5", "1.5", "3.2", "1.1"}


def test_arithmetic_may_use_any_row_of_a_table_or_a_list_but_not_other_paragraphs():
    table = "| item | tCO2e |\n|---|---|\n| scope 1 | 58.0 |\n| scope 2 | 22.0 |\n| total | 80.0 |\n"
    assert unsupported_numbers(table, {"58", "22"}) == []
    two_row = "| kind | tCO2e |\n|---|---|\n| a | 58 |\n| b | 22 |\n\n| total | 80 |"
    assert unsupported_numbers(two_row, {"58", "22"}) == ["80"]  # a separate table
    assert unsupported_numbers("- a 58\n- b 22\n- total 80", {"58", "22"}) == []
    assert unsupported_numbers("a is 58 and b is 22, so the sum is 80.", {"58", "22"}) == []
    assert unsupported_numbers("Site a is 58.\n\nSite b is 22.\n\nUnrelated: 80.", {"58", "22"}) == ["80"]


# ── degenerate false positives ───────────────────────────────────────────────
@pytest.mark.parametrize("text", ["0, " * 60, "&nbsp;" * 55, "<td></td>" * 60])
def test_data_that_looks_repetitive_is_not_degenerate(text):
    assert find_degenerate("Intro. " + text) is None


def test_bracketed_tokens_and_loops_are_still_caught():
    assert find_degenerate("[PAD]" * 3945).pattern == "[PAD]"
    assert find_degenerate("<|endoftext|>" * 50) is not None
    assert find_degenerate("<pad>" * 50) is not None
    assert find_degenerate("hello world " * 60) is None  # a 12-char unit needs 200
    assert find_degenerate("hello world " * 200) is not None
    assert find_degenerate("ha " * 50) is not None


# ── fan-out 0 means unlimited ────────────────────────────────────────────────
def test_max_fanout_zero_does_not_clamp_a_members_max_subagents():
    from tret.config import get_settings

    def with_fanout(n):
        return get_settings().model_copy(update={"max_fanout": n})

    ws = {"guardrails": {"max_subagents": 4}}
    assert resolve_guardrails(with_fanout(0), ws).max_subagents == 4
    assert resolve_guardrails(with_fanout(2), ws).max_subagents == 2
    assert resolve_guardrails(with_fanout(0), {"guardrails": {"max_subagents": 0}}).max_subagents == 0


# ── withheld tools: edition/operator reason ──────────────────────────────────
async def test_import_table_withheld_for_edition_has_its_own_reason(world):
    result, provider = await _freeform(
        world, [ScriptedTurn(text="ok")], tool_names=["lookup_dataset", "import_table"]
    )
    events = result.events_of("tools_withheld")
    edition = [e for e in events if e.data["reason"] == "edition_or_operator"]
    assert edition and "import_table" in edition[0].data["tools"]
    assert "guardrail settings" not in edition[0].data["detail"]
    assert not [e for e in events if e.data["reason"] == "guardrail_setting" and "import_table" in e.data["tools"]]
    assert "import_table" not in provider.calls[0].tool_names


async def test_workspace_guardrail_withholding_keeps_its_reason(world):
    async with world.session_factory() as db:
        ws = await db.get(Workspace, world.workspace_id)
        ws.settings = {**(ws.settings or {}), "guardrails": {"max_subagents": 0}}
        await db.commit()
    result, _ = await _freeform(
        world, [ScriptedTurn(text="ok")], tool_names=["lookup_dataset", "spawn_subagent"]
    )
    assert [e for e in result.events_of("tools_withheld") if e.data["reason"] == "guardrail_setting"]
