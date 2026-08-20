"""Chat turn payload + per-turn routing/model overrides.

No network and no DB: `_assistant_message` is exercised with detached ORM
`Run` objects (as in test_eco_accounting.py), and the request-time helpers
(`_validate_objective`, `_run_task_input`) are pure functions called directly.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from fastapi import HTTPException

from tret.api.chat import SendMessageBody, _assistant_message, _run_task_input, _validate_objective
from tret.db.models import Run
from tret.providers.catalog import ModelInfo, energy_accounting
from tret.router_llm.objectives import OBJECTIVES


def _model(energy_class: str = "L") -> ModelInfo:
    return ModelInfo(
        id="anthropic/test",
        provider="anthropic",
        wire_id="test-1",
        display_name="Test",
        context_window=200_000,
        input_price_per_mtok=Decimal("3"),
        output_price_per_mtok=Decimal("15"),
        cost_tier="standard",
        energy_class=energy_class,
    )


def _run(**over) -> Run:
    base = dict(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        harness_id=uuid.uuid4(),
        task_type="chat",
        task_input={},
        document_ids=[],
        status="completed",
        messages=[{"role": "assistant", "content": "hello", "tool_calls": []}],
        iterations=1,
    )
    base.update(over)
    return Run(**base)


ROUTING = {
    "router_model": "anthropic/router",
    "routing_prompt_version": "v1",
    "candidates": ["anthropic/test"],
    "chosen_model": "anthropic/test",
    "reasoning": "only candidate",
    "confidence": None,
    "objective": "balanced",
    "fallback_used": False,
    "override": None,
    "latency_ms": 12,
    "decided_at": "2026-01-01T00:00:00+00:00",
}


# ── SendMessageBody ──────────────────────────────────────────────────────────


def test_send_message_body_defaults_omit_both_overrides():
    body = SendMessageBody(text="hi")
    assert body.model_override is None
    assert body.objective is None


def test_send_message_body_accepts_both_overrides():
    body = SendMessageBody(text="hi", model_override="anthropic/test", objective="eco")
    assert body.model_override == "anthropic/test"
    assert body.objective == "eco"


# ── objective validation ────────────────────────────────────────────────────


def test_validate_objective_accepts_every_valid_value():
    for objective in OBJECTIVES:
        _validate_objective(objective)  # must not raise


def test_validate_objective_accepts_none():
    _validate_objective(None)  # omitted entirely — must not raise


def test_validate_objective_rejects_garbage_with_the_allowed_values():
    with pytest.raises(HTTPException) as exc:
        _validate_objective("fastest")
    assert exc.value.status_code == 422
    for objective in OBJECTIVES:
        assert objective in exc.value.detail


# ── task_input threading ────────────────────────────────────────────────────


def test_task_input_omitting_both_overrides_is_unchanged():
    task_input = _run_task_input("hi", [], "caps", None, None)
    assert task_input == {"message": "hi", "_history": [], "_capabilities": "caps"}
    assert "_model_override" not in task_input
    assert "_objective" not in task_input


def test_task_input_threads_model_override():
    task_input = _run_task_input("hi", [], "caps", "anthropic/claude-x", None)
    assert task_input["_model_override"] == "anthropic/claude-x"
    assert "_objective" not in task_input


def test_task_input_threads_objective():
    task_input = _run_task_input("hi", [], "caps", None, "eco")
    assert task_input["_objective"] == "eco"
    assert "_model_override" not in task_input


def test_task_input_threads_both():
    task_input = _run_task_input("hi", [], "caps", "anthropic/claude-x", "quality")
    assert task_input["_model_override"] == "anthropic/claude-x"
    assert task_input["_objective"] == "quality"


# ── assistant message payload ───────────────────────────────────────────────


def test_assistant_message_carries_scopes_baseline_and_routing():
    accounting = energy_accounting(_model("L"), 100_000, 10_000, grid_g_per_kwh=400.0)
    run = _run(
        cost_usd=Decimal("0.45"),
        energy_wh=Decimal("132.0"),
        energy_accounting=accounting,
        routing=ROUTING,
        model_used="anthropic/test",
        input_tokens=100_000,
        output_tokens=10_000,
        cache_read_tokens=500,
        cache_write_tokens=50,
    )
    message = _assistant_message(run)
    assert message["role"] == "assistant"
    assert message["content"] == "hello"
    assert message["model_used"] == "anthropic/test"
    assert message["input_tokens"] == 100_000
    assert message["output_tokens"] == 10_000
    assert message["cache_read_tokens"] == 500
    assert message["cache_write_tokens"] == 50
    assert message["cost_usd"] == 0.45
    assert message["energy_wh"] == 132.0
    assert message["co2e_g"] == accounting["co2e_g"] > 0
    assert message["scope2_g"] == accounting["scopes"]["scope2_g"]
    assert message["scope3_g"] == accounting["scopes"]["scope3_g"]
    assert message["avoided_co2e_g"] == accounting["baseline"]["avoided_co2e_g"]
    # Full derivation/decision blocks, verbatim — for the expanded view's
    # EmissionsCalc/RoutingBadge reuse.
    assert message["energy"] == accounting
    assert message["routing"] == ROUTING


def test_assistant_message_preserves_null_not_zero_when_unestimated():
    run = _run(cost_usd=Decimal("0.10"))
    message = _assistant_message(run)
    assert message["energy_wh"] is None
    assert message["co2e_g"] is None
    assert message["scope2_g"] is None
    assert message["scope3_g"] is None
    assert message["avoided_co2e_g"] is None
    assert message["energy"] is None
    assert message["routing"] is None


def test_assistant_message_reports_failure_status_and_error_as_content():
    run = _run(status="failed", error="boom", messages=[])
    message = _assistant_message(run)
    assert message["status"] == "failed"
    assert message["content"] == "(run failed: boom)"


def test_assistant_message_summarizes_delegated_tool_activity():
    run = _run(
        messages=[
            {
                "role": "assistant",
                "content": "done",
                "tool_calls": [
                    {"name": "run_harness_task", "arguments": {"task_type": "evidence_extraction"}}
                ],
            }
        ]
    )
    message = _assistant_message(run)
    assert message["activity"] == [
        {"tool": "run_harness_task", "summary": "delegated evidence_extraction"}
    ]
