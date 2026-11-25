"""Out-of-credit handling and in-stream provider failures (audit M8, M9, M10).

M8: a 402 / out-of-credit for one workspace is a billing failure, not a quality
or availability failure — it must not cool the model for everyone or dent the
pooled priors. M9: the batch credit pause recognises Anthropic's and OpenAI's
wording as well as OpenRouter's, and OpenAI's `insufficient_quota` 429 is not
retried as a rate limit. M10: an error chunk or a truncated SSE stream fails the
run instead of completing it with zero usage.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from tret.db.models import RunOutcome
from tret.providers import openai_compat
from tret.providers.base import (
    Msg,
    ProviderError,
    TextDelta,
    TurnComplete,
    provider_out_of_credit,
)
from tret.providers.openai_compat import OpenRouterProvider
from tret.router_llm.outcomes import BILLING_FAILED, NON_QUALITY_CLASSES, error_kind, score
from tret.router_llm.priors import OutcomePriors, summarize

from tests.test_openrouter_request_hygiene import (
    _FakeStreamResponse,
    _SequencedStreamClient,
    _sse,
)

NOW = datetime(2026, 8, 20, tzinfo=timezone.utc)

ANTHROPIC_LOW_BALANCE = (
    "Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', "
    "'message': 'Your credit balance is too low to access the Anthropic API. Please go to "
    "Plans & Billing to upgrade or purchase credits.'}}"
)
OPENAI_QUOTA = json.dumps(
    {
        "error": {
            "message": "You exceeded your current quota, please check your plan and billing details.",
            "type": "insufficient_quota",
            "param": None,
            "code": "insufficient_quota",
        }
    }
)
OPENROUTER_402 = "[openrouter] Insufficient credits. Add more using https://openrouter.ai/settings/credits"


# ── M8: billing failures are not quality / availability evidence ─────────────
def _scored_row(model_id: str, error: str, *, minutes_ago: float) -> RunOutcome:
    """The `RunOutcome` a failed-at-iteration-0 run would be recorded as, built
    from the real scorer so the test follows whatever class it assigns."""
    result = score(
        status="failed", error=error, messages=[], iterations=0, max_iterations=24
    )
    return RunOutcome(
        task_shape="verdict",
        objective="balanced",
        max_cost_tier="premium",
        task_type="assess",
        size_band="m",
        model_id=model_id,
        provider="openrouter",
        outcome_class=result.outcome_class,
        quality_score=Decimal(str(result.quality_score)),
        score_version=result.score_version,
        error_kind=result.error_kind,
        components=result.components,
        iterations=0,
        cost_usd=Decimal("0"),
        input_tokens=0,
        output_tokens=0,
        energy_wh=None,
        duration_ms=0,
        findings_created=0,
        findings_approved=0,
        findings_rejected=0,
        observed_at=NOW - timedelta(minutes=minutes_ago),
    )


class _ScriptedCooldownPriors(OutcomePriors):
    def __init__(self, rows):
        super().__init__(session_factory=object(), now=lambda: NOW, cooldown_minutes=30)
        self._rows = rows

    async def _cooldown_rows(self, candidate_ids, since):
        return [r for r in self._rows if r.model_id in candidate_ids]


@pytest.mark.parametrize(
    "error",
    [OPENROUTER_402, f"[anthropic] {ANTHROPIC_LOW_BALANCE}", f"[openai_compat] {OPENAI_QUOTA}"],
)
def test_an_out_of_credit_failure_is_a_billing_class_not_a_quality_failure(error):
    result = score(status="failed", error=error, messages=[], iterations=0, max_iterations=24)
    assert result.outcome_class == BILLING_FAILED
    assert result.outcome_class in NON_QUALITY_CLASSES
    assert result.error_kind == "billing_error"
    assert error_kind("failed", error) == "billing_error"


def test_an_ordinary_provider_failure_is_still_a_quality_failure():
    result = score(
        status="failed", error="[openrouter] Provider returned error", messages=[],
        iterations=0, max_iterations=24,
    )
    assert result.outcome_class == "failed"
    assert result.error_kind == "provider_error"


async def test_a_402_in_workspace_a_does_not_cool_the_model_for_workspace_b():
    # Two workspaces' runs of the same model both hit "Insufficient credits"
    # (RunOutcome is pooled across workspaces — the breaker is global).
    rows = [
        _scored_row("m/x", OPENROUTER_402, minutes_ago=1),  # workspace A
        _scored_row("m/x", OPENROUTER_402, minutes_ago=2),  # workspace A again
    ]
    assert await _ScriptedCooldownPriors(rows).cooldown_for(["m/x"]) == []


async def test_a_402_between_two_real_provider_failures_does_not_hide_them():
    outage = "[openrouter] Provider returned error"
    rows = [
        _scored_row("m/x", outage, minutes_ago=1),
        _scored_row("m/x", OPENROUTER_402, minutes_ago=2),
        _scored_row("m/x", outage, minutes_ago=3),
    ]
    cooldown = await _ScriptedCooldownPriors(rows).cooldown_for(["m/x"])
    assert [c["model"] for c in cooldown] == ["m/x"]


def test_a_billing_failure_is_not_recorded_as_quality_evidence():
    rows = [_scored_row("m/x", OPENROUTER_402, minutes_ago=1) for _ in range(40)]
    # Even if one reaches `summarize` by another route, it counts for nothing.
    assert summarize(rows, now=NOW) == {}


# ── M9: the credit classifier recognises every provider's wording ────────────
def test_anthropic_low_balance_is_out_of_credit():
    assert provider_out_of_credit(f"[anthropic] {ANTHROPIC_LOW_BALANCE}") == "anthropic"
    assert provider_out_of_credit(ProviderError("anthropic", ANTHROPIC_LOW_BALANCE, 400)) == "anthropic"


def test_openai_insufficient_quota_is_out_of_credit():
    err = ProviderError("openai_compat", OPENAI_QUOTA, 429)
    assert provider_out_of_credit(err) == "openai_compat"
    # The code alone (no friendly message) is enough, in either quoting style.
    assert provider_out_of_credit('[kimi] {"error": {"code": "insufficient_quota"}}') == "kimi"
    assert provider_out_of_credit("[kimi] {'error': {'code': 'insufficient_quota'}}") == "kimi"


def test_ordinary_rate_limits_and_400s_are_still_not_credit_problems():
    for err in (
        ProviderError("openai_compat", '{"error":{"code":"rate_limit_exceeded","message":"Rate limit reached"}}', 429),
        ProviderError("anthropic", "Error code: 400 - {'type': 'invalid_request_error', 'message': 'max_tokens too large'}", 400),
    ):
        assert provider_out_of_credit(err) is None


def _stream_call(provider):
    return provider.stream(
        model="m", system="doctrine", messages=[Msg(role="user", content="hi")],
        tools=[], max_tokens=64, temperature=0.0,
    )


async def test_insufficient_quota_429_is_not_retried_as_a_rate_limit(monkeypatch):
    client = _SequencedStreamClient(
        [_FakeStreamResponse(status_code=429, body=OPENAI_QUOTA.encode())]
    )
    monkeypatch.setattr(openai_compat.httpx, "AsyncClient", client)

    async def _boom(_s):  # a rate-limit retry would sleep
        raise AssertionError("insufficient_quota must not be retried")

    monkeypatch.setattr(openai_compat.asyncio, "sleep", _boom)

    with pytest.raises(ProviderError) as exc:
        async for _ in _stream_call(OpenRouterProvider("k")):
            pass
    assert client.call_count == 1
    assert exc.value.status == 429
    assert provider_out_of_credit(exc.value) == "openrouter"


# ── M10: in-stream errors and truncated streams fail the run ─────────────────
async def _no_sleep(_s):
    return None


async def _drive(monkeypatch, lines, *, attempts=1):
    client = _SequencedStreamClient([_FakeStreamResponse(lines=lines)] * attempts)
    monkeypatch.setattr(openai_compat.httpx, "AsyncClient", client)
    monkeypatch.setattr(openai_compat.asyncio, "sleep", _no_sleep)
    events = []
    async for event in _stream_call(OpenRouterProvider("k")):
        events.append(event)
    return events


async def test_an_error_chunk_fails_the_stream_with_the_providers_message(monkeypatch):
    lines = _sse({"error": {"message": "Upstream connection dropped", "code": 400}})
    with pytest.raises(ProviderError) as exc:
        await _drive(monkeypatch, lines)
    assert "Upstream connection dropped" in str(exc.value)
    assert exc.value.status == 400


async def test_an_error_chunk_after_partial_output_surfaces_after_the_text(monkeypatch):
    # The engine meters this partial turn through its estimated-usage path (the
    # `ProviderError` handler in engine/harness.py) — it only can if the
    # text was delivered first and the failure then raised.
    lines = _sse(
        {"choices": [{"delta": {"content": "partial answer"}}]},
        {"error": {"message": "Provider disconnected", "code": 502}},
    )
    seen: list = []
    client = _SequencedStreamClient([_FakeStreamResponse(lines=lines)])
    monkeypatch.setattr(openai_compat.httpx, "AsyncClient", client)
    with pytest.raises(ProviderError, match="Provider disconnected"):
        async for event in _stream_call(OpenRouterProvider("k")):
            seen.append(event)
    assert [e.text for e in seen if isinstance(e, TextDelta)] == ["partial answer"]
    assert not any(isinstance(e, TurnComplete) for e in seen)
    assert client.call_count == 1  # output already reached the caller: no retry


async def test_an_in_stream_insufficient_quota_error_reaches_the_credit_pause(monkeypatch):
    lines = _sse({"error": {"message": "You exceeded your current quota", "code": "insufficient_quota"}})
    with pytest.raises(ProviderError) as exc:
        await _drive(monkeypatch, lines)
    assert provider_out_of_credit(exc.value) == "openrouter"


async def test_a_truncated_stream_without_finish_reason_fails(monkeypatch):
    # Cut mid-answer: no finish_reason, no [DONE], no usage frame.
    lines = [f"data: {json.dumps({'choices': [{'delta': {'content': 'half an ans'}}]})}"]
    with pytest.raises(ProviderError, match="stream ended"):
        await _drive(monkeypatch, lines, attempts=2)


async def test_a_stream_without_done_but_with_finish_reason_still_completes(monkeypatch):
    # Local runtimes and some gateways omit `[DONE]`; they still say how the
    # turn ended and report usage.
    lines = [
        f"data: {json.dumps(c)}"
        for c in (
            {"choices": [{"delta": {"content": "ok"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 3}},
        )
    ]
    events = await _drive(monkeypatch, lines)
    done = events[-1]
    assert isinstance(done, TurnComplete)
    assert done.stop_reason == "stop"
    assert (done.usage.input_tokens, done.usage.output_tokens) == (7, 3)


def test_geminis_per_minute_quota_429_is_not_out_of_credit():
    """Gemini words its ordinary RESOURCE_EXHAUSTED rate limit as "You exceeded
    your current quota"; only OpenAI's `insufficient_quota` code means billing."""
    from tret.providers.base import is_out_of_credit_text

    gemini = json.dumps(
        {
            "error": {
                "code": 429,
                "message": "You exceeded your current quota, please check your plan and billing details.",
                "status": "RESOURCE_EXHAUSTED",
            }
        }
    )
    assert not is_out_of_credit_text(gemini)
    assert is_out_of_credit_text(OPENAI_QUOTA)
