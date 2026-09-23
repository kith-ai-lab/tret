"""Self-healing fallback for models that reject a forced tool_choice.

`AnthropicProvider.complete_json` and `OpenAICompatProvider.complete_json`
both force the response tool on every call (`tool_choice={"type": "tool", ...}`
on Anthropic, `{"type": "function", "function": {...}}` on the OpenAI-compat
wire). Claude Opus 5.5 and Claude Fable 5.1 (and, per OpenRouter, likely
future models) return an HTTP 400 for that — this covers the one-time
downgrade to `tool_choice: auto` (with an appended system instruction) and
the per-instance memoization that skips straight to auto on a model already
known to reject the forced shape.

Reuses the fake-transport helpers from `test_openrouter_request_hygiene.py`
and `test_provider_rate_limit_retry.py` rather than inventing new ones.
"""
from __future__ import annotations

import json

import anthropic
import httpx
import pytest

from tret.providers import anthropic as anthropic_provider_module
from tret.providers import openai_compat
from tret.providers.anthropic import AnthropicProvider
from tret.providers.base import ProviderError
from tret.providers.openai_compat import OpenRouterProvider

from tests.test_openrouter_request_hygiene import (
    _FakeJsonResponse,
    _FakeRawJsonResponse,
    _FakeToolUseBlock,
    _completion_json,
)


# ── Anthropic ────────────────────────────────────────────────────────────────
def _anthropic_400_error(message: str) -> anthropic.APIStatusError:
    resp = httpx.Response(
        status_code=400,
        headers={"content-type": "application/json"},
        content=json.dumps({"error": {"type": "invalid_request_error", "message": message}}).encode(),
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
    )
    return anthropic.APIStatusError(message, response=resp, body=None)


class _FakeAnthropicCreateResult:
    def __init__(self, tool_name: str, payload: dict):
        self.content = [_FakeToolUseBlock(tool_name, payload)]
        self.usage = None


class _RecordingAnthropicCreate:
    """Like `_RetryingAnthropicCreate`, but records every call's kwargs so a
    test can assert what the fallback attempt actually sent."""

    def __init__(self, failures: list[Exception], result):
        self._failures = list(failures)
        self._result = result
        self.call_count = 0
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.call_count += 1
        self.calls.append(kwargs)
        if self._failures:
            raise self._failures.pop(0)
        return self._result


async def test_anthropic_400_tool_choice_retries_with_auto_and_succeeds(monkeypatch):
    provider = AnthropicProvider("test-key")
    result = _FakeAnthropicCreateResult("respond", {"ok": True})
    fake_messages = _RecordingAnthropicCreate(
        [_anthropic_400_error('tool_choice: type "tool" and "any" are not supported for this model.')],
        result,
    )
    monkeypatch.setattr(provider._client, "messages", fake_messages)
    monkeypatch.setattr(anthropic_provider_module.asyncio, "sleep", lambda *_: _noop())

    completion = await provider.complete_json(
        model="claude-opus-5-5", system="doctrine", prompt="hi", schema={"type": "object"}
    )

    assert completion.payload == {"ok": True}
    assert fake_messages.call_count == 2
    first_call, second_call = fake_messages.calls
    assert first_call["tool_choice"] == {"type": "tool", "name": "respond"}
    assert second_call["tool_choice"] == {"type": "auto"}
    assert second_call["tools"] == first_call["tools"]
    assert "Respond only by calling the `respond` tool." in second_call["system"]


async def _noop():
    return None


async def test_anthropic_second_call_on_same_model_skips_the_forced_attempt(monkeypatch):
    provider = AnthropicProvider("test-key")
    # Only ONE response is scripted for the second `complete_json` call: if the
    # provider tried the forced shape again it would raise (no more failures
    # queued) instead of returning this result, so a single successful call
    # here proves the memoized model went straight to auto.
    result = _FakeAnthropicCreateResult("respond", {"ok": True})
    first_messages = _RecordingAnthropicCreate(
        [_anthropic_400_error('tool_choice: type "tool" and "any" are not supported for this model.')],
        result,
    )
    monkeypatch.setattr(provider._client, "messages", first_messages)
    monkeypatch.setattr(anthropic_provider_module.asyncio, "sleep", lambda *_: _noop())
    await provider.complete_json(
        model="claude-opus-5-5", system="doctrine", prompt="hi", schema={"type": "object"}
    )
    assert first_messages.call_count == 2

    second_messages = _RecordingAnthropicCreate([], result)
    monkeypatch.setattr(provider._client, "messages", second_messages)
    completion = await provider.complete_json(
        model="claude-opus-5-5", system="doctrine", prompt="hi again", schema={"type": "object"}
    )

    assert completion.payload == {"ok": True}
    assert second_messages.call_count == 1
    assert second_messages.calls[0]["tool_choice"] == {"type": "auto"}


class _FakeAnthropicNoToolUseResult:
    """A response with no tool_use block at all — the auto-fallback let the
    model answer in plain text instead of calling the tool."""

    def __init__(self):
        self.content = []
        self.usage = None


async def test_anthropic_no_tool_use_block_raises_with_usage_attached(monkeypatch):
    """2026-09-23 fix: the call was billed (it reached the provider and got a
    real response back), so the raised `ProviderError` must carry that spend
    rather than discard it — see `providers/base.py`'s `ProviderError.usage`."""
    provider = AnthropicProvider("test-key")
    fake_messages = _RecordingAnthropicCreate([], _FakeAnthropicNoToolUseResult())
    monkeypatch.setattr(provider._client, "messages", fake_messages)

    with pytest.raises(ProviderError) as exc:
        await provider.complete_json(
            model="claude-opus-5-5", system="doctrine", prompt="hi", schema={"type": "object"}
        )

    assert exc.value.usage is not None
    assert exc.value.model == "claude-opus-5-5"
    assert exc.value.served_by == "anthropic"


async def test_anthropic_non_tool_choice_400_still_raises_without_retry(monkeypatch):
    provider = AnthropicProvider("test-key")
    result = _FakeAnthropicCreateResult("respond", {"ok": True})
    fake_messages = _RecordingAnthropicCreate(
        [_anthropic_400_error("max_tokens is too large for this model")], result
    )
    monkeypatch.setattr(provider._client, "messages", fake_messages)
    monkeypatch.setattr(anthropic_provider_module.asyncio, "sleep", lambda *_: _noop())

    with pytest.raises(ProviderError) as exc:
        await provider.complete_json(
            model="claude-opus-5-5", system="doctrine", prompt="hi", schema={"type": "object"}
        )

    assert exc.value.status == 400
    assert fake_messages.call_count == 1


# ── openai_compat (OpenRouter) ──────────────────────────────────────────────
class _CapturingSequencedJsonClient:
    """Like `_SequencedJsonClient`, but records the JSON body sent on every
    `post()` call so a test can assert what the fallback attempt sent."""

    def __init__(self, responses: list):
        self._responses = list(responses)
        self.call_count = 0
        self.bodies: list[dict] = []

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None):
        self.bodies.append(json or {})
        response = self._responses[min(self.call_count, len(self._responses) - 1)]
        self.call_count += 1
        return response

    async def get(self, url, headers=None):
        return _FakeRawJsonResponse("{}")


def _openrouter_tool_choice_400_body() -> str:
    """Live-observed shape (2026-09-23): OpenRouter's top-level `error.message`
    is just "Provider returned error" — the actual reason is nested in
    `error.metadata.raw`, a JSON string from the upstream provider."""
    return json.dumps(
        {
            "error": {
                "message": "Provider returned error",
                "code": 400,
                "metadata": {
                    "raw": json.dumps(
                        {
                            "type": "error",
                            "error": {
                                "type": "invalid_request_error",
                                "message": (
                                    'tool_choice: type "tool" and "any" are not supported '
                                    "for this model."
                                ),
                            },
                        }
                    )
                },
            }
        }
    )


async def test_openai_compat_400_tool_choice_retries_with_auto_and_succeeds(monkeypatch):
    responses = [
        _FakeRawJsonResponse(_openrouter_tool_choice_400_body(), status_code=400),
        _FakeJsonResponse(_completion_json()),
    ]
    client = _CapturingSequencedJsonClient(responses)
    monkeypatch.setattr(openai_compat.httpx, "AsyncClient", client)
    monkeypatch.setattr(openai_compat.asyncio, "sleep", lambda *_: _noop())

    completion = await OpenRouterProvider("k").complete_json(
        model="anthropic/claude-opus-5.5", system="doctrine", prompt="hi", schema={"type": "object"}
    )

    assert completion.payload == {"ok": True}
    assert client.call_count == 2
    first_body, second_body = client.bodies
    assert first_body["tool_choice"] == {"type": "function", "function": {"name": "respond"}}
    assert second_body["tool_choice"] == "auto"
    assert second_body["tools"] == first_body["tools"]
    assert "Respond only by calling the `respond` tool." in second_body["messages"][0]["content"]


async def test_openai_compat_second_call_on_same_model_skips_the_forced_attempt(monkeypatch):
    first_responses = [
        _FakeRawJsonResponse(_openrouter_tool_choice_400_body(), status_code=400),
        _FakeJsonResponse(_completion_json()),
    ]
    first_client = _CapturingSequencedJsonClient(first_responses)
    monkeypatch.setattr(openai_compat.httpx, "AsyncClient", first_client)
    monkeypatch.setattr(openai_compat.asyncio, "sleep", lambda *_: _noop())

    provider = OpenRouterProvider("k")
    await provider.complete_json(
        model="anthropic/claude-opus-5.5", system="doctrine", prompt="hi", schema={"type": "object"}
    )
    assert first_client.call_count == 2

    # Only ONE response is scripted for the second call: a forced attempt
    # that had to fall back again would need two.
    second_client = _CapturingSequencedJsonClient([_FakeJsonResponse(_completion_json())])
    monkeypatch.setattr(openai_compat.httpx, "AsyncClient", second_client)

    completion = await provider.complete_json(
        model="anthropic/claude-opus-5.5", system="doctrine", prompt="hi again", schema={"type": "object"}
    )

    assert completion.payload == {"ok": True}
    assert second_client.call_count == 1
    assert second_client.bodies[0]["tool_choice"] == "auto"


async def test_openai_compat_non_tool_choice_400_still_raises_without_retry(monkeypatch):
    responses = [
        _FakeRawJsonResponse('{"error":{"message":"invalid schema"}}', status_code=400),
    ]
    client = _CapturingSequencedJsonClient(responses)
    monkeypatch.setattr(openai_compat.httpx, "AsyncClient", client)
    monkeypatch.setattr(openai_compat.asyncio, "sleep", lambda *_: _noop())

    with pytest.raises(ProviderError) as exc:
        await OpenRouterProvider("k").complete_json(
            model="anthropic/claude-opus-5.5", system="doctrine", prompt="hi", schema={"type": "object"}
        )

    assert exc.value.status == 400
    assert client.call_count == 1


def _no_tool_call_json_body() -> dict:
    """A response that got through with `tool_calls` empty — the model
    answered in prose instead, same failure this fix is for on this side."""
    return {
        "choices": [{"message": {"tool_calls": []}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2},
    }


async def test_openai_compat_no_tool_call_raises_with_usage_attached(monkeypatch):
    """2026-09-23 fix: mirrors the Anthropic test above for the OpenAI-compat
    side — a billed call that answered without the forced tool call must not
    make its `ProviderError` throw the usage away."""
    client = _CapturingSequencedJsonClient([_FakeJsonResponse(_no_tool_call_json_body())])
    monkeypatch.setattr(openai_compat.httpx, "AsyncClient", client)
    monkeypatch.setattr(openai_compat.asyncio, "sleep", lambda *_: _noop())

    with pytest.raises(ProviderError) as exc:
        await OpenRouterProvider("k").complete_json(
            model="anthropic/claude-opus-5.5", system="doctrine", prompt="hi", schema={"type": "object"}
        )

    assert exc.value.usage is not None
    assert exc.value.usage.input_tokens == 10
    assert exc.value.usage.output_tokens == 2
    assert exc.value.model == "anthropic/claude-opus-5.5"


# ── allow_auto_fallback=False (the local tool-capability probe) ────────────
async def test_anthropic_probe_with_auto_fallback_disabled_raises_without_retry(monkeypatch):
    """`providers/catalog.py`'s `_probe_supports_tools` exists specifically to
    catch a model that only honors an *auto* tool_choice, not a forced one —
    the 2026-09-23 downgrade must not run for it, or every such model would
    pass the probe it's meant to fail."""
    provider = AnthropicProvider("test-key")
    result = _FakeAnthropicCreateResult("respond", {"ok": True})
    fake_messages = _RecordingAnthropicCreate(
        [_anthropic_400_error('tool_choice: type "tool" and "any" are not supported for this model.')],
        result,
    )
    monkeypatch.setattr(provider._client, "messages", fake_messages)
    monkeypatch.setattr(anthropic_provider_module.asyncio, "sleep", lambda *_: _noop())

    with pytest.raises(ProviderError) as exc:
        await provider.complete_json(
            model="claude-opus-5-5",
            system="doctrine",
            prompt="hi",
            schema={"type": "object"},
            allow_auto_fallback=False,
        )

    assert exc.value.status == 400
    assert fake_messages.call_count == 1  # no auto-fallback retry attempted


async def test_openai_compat_probe_with_auto_fallback_disabled_raises_without_retry(monkeypatch):
    responses = [
        _FakeRawJsonResponse(_openrouter_tool_choice_400_body(), status_code=400),
    ]
    client = _CapturingSequencedJsonClient(responses)
    monkeypatch.setattr(openai_compat.httpx, "AsyncClient", client)
    monkeypatch.setattr(openai_compat.asyncio, "sleep", lambda *_: _noop())

    with pytest.raises(ProviderError) as exc:
        await OpenRouterProvider("k").complete_json(
            model="anthropic/claude-opus-5.5",
            system="doctrine",
            prompt="hi",
            schema={"type": "object"},
            allow_auto_fallback=False,
        )

    assert exc.value.status == 400
    assert client.call_count == 1
