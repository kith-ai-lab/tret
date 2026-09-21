"""429 ("rate limited") retry policy: `providers.base.rate_limit_delay` as a
pure function, plus its use in `OpenAICompatProvider` and `AnthropicProvider`'s
`stream()`/`complete_json()` retry loops (see the "rate-limit retry
(2026-09-20)" block in `providers/base.py`).

Reuses `test_openrouter_request_hygiene.py`'s fake-transport pattern for the
existing 5xx retry (its "transient upstream retry" section) rather than
inventing a new one — no network, and `asyncio.sleep` is always stubbed so no
test here sleeps for real. That file's own 5xx tests are left unmodified and
re-run in this task's verify command to confirm the 429 path didn't change
their behaviour.
"""
from __future__ import annotations

from email.utils import format_datetime
from datetime import datetime, timedelta, timezone

import anthropic
import httpx
import pytest

from tret.providers import anthropic as anthropic_provider_module
from tret.providers import openai_compat
from tret.providers.anthropic import AnthropicProvider
from tret.providers.base import (
    RATE_LIMIT_BASE_DELAY_SECONDS,
    RATE_LIMIT_MAX_DELAY_SECONDS,
    RATE_LIMIT_MAX_RETRIES,
    Msg,
    ProviderError,
    TurnComplete,
    rate_limit_delay,
)
from tret.providers.openai_compat import OpenRouterProvider

from tests.test_openrouter_request_hygiene import (
    _FakeAnthropicCreateResult,
    _FakeRawJsonResponse,
    _FakeStreamResponse,
    _RetryingAnthropicCreate,
    _RetryingAnthropicMessages,
    _SequencedJsonClient,
    _SequencedStreamClient,
    _sse,
)


def _capturing_sleep():
    """A stand-in for `asyncio.sleep` that never actually sleeps, but records
    every duration it was asked to wait — so a test can check *what* delay
    the retry policy chose without waiting for it."""
    calls: list[float] = []

    async def _sleep(seconds):
        calls.append(seconds)

    return _sleep, calls


# ── 1. `rate_limit_delay` as a pure function ────────────────────────────────
def test_rate_limit_delay_honours_numeric_retry_after():
    assert rate_limit_delay(0, "5", rng=lambda: 0) == 5.0


def test_rate_limit_delay_honours_http_date_retry_after():
    when = datetime.now(timezone.utc) + timedelta(seconds=10)
    http_date = format_datetime(when, usegmt=True)
    delay = rate_limit_delay(0, http_date, rng=lambda: 0)
    # Computed against `datetime.now()` at call time, so allow the wall-clock
    # slack between building `when` above and the call itself.
    assert delay == pytest.approx(10.0, abs=1.0)


def test_rate_limit_delay_ignores_unparsable_retry_after_and_falls_back_to_exponential():
    assert rate_limit_delay(1, "not-a-date", rng=lambda: 0) == RATE_LIMIT_BASE_DELAY_SECONDS * 2**1
    assert rate_limit_delay(1, None, rng=lambda: 0) == RATE_LIMIT_BASE_DELAY_SECONDS * 2**1


def test_rate_limit_delay_jitter_bounds_with_fixed_rng():
    base = RATE_LIMIT_BASE_DELAY_SECONDS  # attempt 0, no Retry-After
    assert rate_limit_delay(0, None, rng=lambda: 0) == base
    assert rate_limit_delay(0, None, rng=lambda: 1) == pytest.approx(base * 1.25)


def test_rate_limit_delay_clamps_at_max():
    # attempt 10 would exponentiate far past the cap.
    assert rate_limit_delay(10, None, rng=lambda: 0) == RATE_LIMIT_MAX_DELAY_SECONDS
    assert rate_limit_delay(10, None, rng=lambda: 1) == RATE_LIMIT_MAX_DELAY_SECONDS


def test_rate_limit_delay_negative_retry_after_is_zero():
    assert rate_limit_delay(0, "-5", rng=lambda: 0) == 0.0


# ── 2. OpenAICompatProvider (OpenRouter): stream() ──────────────────────────
async def test_stream_429_then_200_succeeds_and_sleeps_the_retry_after_value(monkeypatch):
    responses = [
        _FakeStreamResponse(status_code=429, body=b'{"error":"rate limited"}', headers={"retry-after": "3"}),
        _FakeStreamResponse(
            lines=_sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})
        ),
    ]
    client = _SequencedStreamClient(responses)
    monkeypatch.setattr(openai_compat.httpx, "AsyncClient", client)
    sleep, slept = _capturing_sleep()
    monkeypatch.setattr(openai_compat.asyncio, "sleep", sleep)

    events = []
    async for event in OpenRouterProvider("k").stream(
        model="m", system="doctrine", messages=[Msg(role="user", content="hi")],
        tools=[], max_tokens=64, temperature=0.0,
    ):
        events.append(event)

    assert client.call_count == 2
    assert isinstance(events[-1], TurnComplete)
    assert len(slept) == 1
    # Retry-After of 3s, plus up to +25% jitter.
    assert 3.0 <= slept[0] <= 3.75


async def test_stream_429_exhausts_retries_and_surfaces_the_error(monkeypatch):
    responses = [
        _FakeStreamResponse(status_code=429, body=b'{"error":"rate limited"}')
        for _ in range(RATE_LIMIT_MAX_RETRIES + 1)
    ]
    client = _SequencedStreamClient(responses)
    monkeypatch.setattr(openai_compat.httpx, "AsyncClient", client)
    sleep, slept = _capturing_sleep()
    monkeypatch.setattr(openai_compat.asyncio, "sleep", sleep)

    with pytest.raises(ProviderError) as exc:
        async for _event in OpenRouterProvider("k").stream(
            model="m", system="doctrine", messages=[Msg(role="user", content="hi")],
            tools=[], max_tokens=64, temperature=0.0,
        ):
            pass

    assert exc.value.status == 429
    # Initial attempt + RATE_LIMIT_MAX_RETRIES retries, no more.
    assert client.call_count == RATE_LIMIT_MAX_RETRIES + 1
    assert len(slept) == RATE_LIMIT_MAX_RETRIES


async def test_stream_a_5xx_retry_and_429_retries_are_independent_counters(monkeypatch):
    """A request that already used its one 5xx retry can still get its full
    429 retries afterwards — the two counters don't share a budget."""
    responses = [
        _FakeStreamResponse(status_code=503, body=b'{"error":"overloaded"}'),
        _FakeStreamResponse(status_code=429, body=b'{"error":"rate limited"}'),
        _FakeStreamResponse(
            lines=_sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})
        ),
    ]
    client = _SequencedStreamClient(responses)
    monkeypatch.setattr(openai_compat.httpx, "AsyncClient", client)
    sleep, slept = _capturing_sleep()
    monkeypatch.setattr(openai_compat.asyncio, "sleep", sleep)

    events = []
    async for event in OpenRouterProvider("k").stream(
        model="m", system="doctrine", messages=[Msg(role="user", content="hi")],
        tools=[], max_tokens=64, temperature=0.0,
    ):
        events.append(event)

    assert client.call_count == 3
    assert isinstance(events[-1], TurnComplete)


# ── 3. OpenAICompatProvider (OpenRouter): complete_json() ───────────────────
async def test_complete_json_429_then_200_succeeds_and_sleeps_the_retry_after_value(monkeypatch):
    responses = [
        _FakeRawJsonResponse('{"error":"rate limited"}', status_code=429, headers={"retry-after": "2"}),
        _FakeRawJsonResponse(
            '{"choices": [{"message": {"tool_calls": [{"function": '
            '{"name": "respond", "arguments": "{\\"ok\\": true}"}}]}}], '
            '"usage": {"prompt_tokens": 1, "completion_tokens": 1}}'
        ),
    ]
    client = _SequencedJsonClient(responses)
    monkeypatch.setattr(openai_compat.httpx, "AsyncClient", client)
    sleep, slept = _capturing_sleep()
    monkeypatch.setattr(openai_compat.asyncio, "sleep", sleep)

    completion = await OpenRouterProvider("k").complete_json(
        model="m", system="doctrine", prompt="hi", schema={"type": "object"}
    )

    assert client.call_count == 2
    assert completion.payload == {"ok": True}
    assert len(slept) == 1
    assert 2.0 <= slept[0] <= 2.5


async def test_complete_json_429_exhausts_retries_and_surfaces_the_error(monkeypatch):
    responses = [
        _FakeRawJsonResponse('{"error":"rate limited"}', status_code=429)
        for _ in range(RATE_LIMIT_MAX_RETRIES + 1)
    ]
    client = _SequencedJsonClient(responses)
    monkeypatch.setattr(openai_compat.httpx, "AsyncClient", client)
    sleep, slept = _capturing_sleep()
    monkeypatch.setattr(openai_compat.asyncio, "sleep", sleep)

    with pytest.raises(ProviderError) as exc:
        await OpenRouterProvider("k").complete_json(
            model="m", system="doctrine", prompt="hi", schema={"type": "object"}
        )

    assert exc.value.status == 429
    assert client.call_count == RATE_LIMIT_MAX_RETRIES + 1
    assert len(slept) == RATE_LIMIT_MAX_RETRIES


# ── 4. AnthropicProvider: stream() and complete_json() ──────────────────────
def _anthropic_429_error(retry_after: str | None = None) -> anthropic.APIStatusError:
    headers = {"content-type": "application/json"}
    if retry_after is not None:
        headers["retry-after"] = retry_after
    resp = httpx.Response(
        status_code=429,
        headers=headers,
        content=b'{"error":{"type":"rate_limit_error","message":"Rate limited"}}',
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
    )
    return anthropic.APIStatusError("rate limited", response=resp, body=None)


async def test_anthropic_stream_429_then_200_succeeds_and_sleeps_the_retry_after_value(monkeypatch):
    provider = AnthropicProvider("test-key")
    fake_messages = _RetryingAnthropicMessages([_anthropic_429_error("4")])
    monkeypatch.setattr(provider._client, "messages", fake_messages)
    sleep, slept = _capturing_sleep()
    monkeypatch.setattr(anthropic_provider_module.asyncio, "sleep", sleep)

    events = []
    async for event in provider.stream(
        model="claude-sonnet-5", system="doctrine", messages=[Msg(role="user", content="hi")],
        tools=[], max_tokens=64, temperature=0.0,
    ):
        events.append(event)

    assert fake_messages.call_count == 2
    assert isinstance(events[-1], TurnComplete)
    assert len(slept) == 1
    assert 4.0 <= slept[0] <= 5.0


async def test_anthropic_stream_429_exhausts_retries_and_surfaces_the_error(monkeypatch):
    provider = AnthropicProvider("test-key")
    fake_messages = _RetryingAnthropicMessages(
        [_anthropic_429_error() for _ in range(RATE_LIMIT_MAX_RETRIES + 1)]
    )
    monkeypatch.setattr(provider._client, "messages", fake_messages)
    sleep, slept = _capturing_sleep()
    monkeypatch.setattr(anthropic_provider_module.asyncio, "sleep", sleep)

    with pytest.raises(ProviderError) as exc:
        async for _event in provider.stream(
            model="claude-sonnet-5", system="doctrine", messages=[Msg(role="user", content="hi")],
            tools=[], max_tokens=64, temperature=0.0,
        ):
            pass

    assert exc.value.status == 429
    assert fake_messages.call_count == RATE_LIMIT_MAX_RETRIES + 1
    assert len(slept) == RATE_LIMIT_MAX_RETRIES


async def test_anthropic_complete_json_429_then_200_succeeds(monkeypatch):
    provider = AnthropicProvider("test-key")
    result = _FakeAnthropicCreateResult("respond", {"ok": True})
    fake_messages = _RetryingAnthropicCreate([_anthropic_429_error("1")], result)
    monkeypatch.setattr(provider._client, "messages", fake_messages)
    sleep, slept = _capturing_sleep()
    monkeypatch.setattr(anthropic_provider_module.asyncio, "sleep", sleep)

    completion = await provider.complete_json(
        model="claude-sonnet-5", system="doctrine", prompt="hi", schema={"type": "object"}
    )

    assert fake_messages.call_count == 2
    assert completion.payload == {"ok": True}
    assert len(slept) == 1
    assert 1.0 <= slept[0] <= 1.25


async def test_anthropic_complete_json_429_exhausts_retries(monkeypatch):
    provider = AnthropicProvider("test-key")
    result = _FakeAnthropicCreateResult("respond", {"ok": True})
    fake_messages = _RetryingAnthropicCreate(
        [_anthropic_429_error() for _ in range(RATE_LIMIT_MAX_RETRIES + 1)], result
    )
    monkeypatch.setattr(provider._client, "messages", fake_messages)
    sleep, slept = _capturing_sleep()
    monkeypatch.setattr(anthropic_provider_module.asyncio, "sleep", sleep)

    with pytest.raises(ProviderError) as exc:
        await provider.complete_json(
            model="claude-sonnet-5", system="doctrine", prompt="hi", schema={"type": "object"}
        )

    assert exc.value.status == 429
    assert fake_messages.call_count == RATE_LIMIT_MAX_RETRIES + 1
    assert len(slept) == RATE_LIMIT_MAX_RETRIES
