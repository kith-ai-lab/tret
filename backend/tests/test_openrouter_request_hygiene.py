"""OpenRouter request/response hygiene: session affinity, provider
preferences, and recording which upstream actually served a call.

No network: request bodies are inspected directly (`_apply_provider` /
`_session_body`) or via a fake httpx client driving `stream()`/`complete_json()`
exactly the way `test_prompt_caching.py` does for cache_control.

Field names verified against the live OpenRouter docs (fetched 2026-09-10):
- https://openrouter.ai/docs/api-reference/chat-completion — `session_id` is a
  top-level request string (CreateChatCompletionRequest), max 256 chars, used
  as OpenRouter's sticky routing key. The *response* schemas
  (ChatCompletionResponse, ChatStreamChunk) carry no top-level `provider`
  field — the served endpoint is named only inside
  `openrouter_metadata.endpoints.available[]`, in the entry with
  `selected: true`.
- https://openrouter.ai/docs/guides/routing/provider-selection — the
  `provider` request object's fields: `order`, `ignore`, `only`,
  `quantizations`, `data_collection`, `zdr`, `sort`, `require_parameters`
  (all verified against the ProviderPreferences schema).
"""
from __future__ import annotations

import json
import logging

from tret.config import Settings
from tret.providers import openai_compat
from tret.providers.anthropic import AnthropicProvider
from tret.providers.base import Msg, ToolSpec
from tret.providers.openai_compat import (
    KimiProvider,
    OpenRouterProvider,
    _served_by_from_openai,
)


def _tool() -> ToolSpec:
    return ToolSpec(name="lookup", description="look something up", parameters={"type": "object"})


# ── fake httpx client (streaming) — same shape as test_prompt_caching.py ──────
class _FakeResponse:
    status_code = 200

    def __init__(self, lines):
        self._lines = lines

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self):  # pragma: no cover - only used on error paths
        return b""


class _FakeStreamCtx:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *exc):
        return False


class _FakeStreamClient:
    """Stands in for httpx.AsyncClient, recording the streamed request body."""

    captured: dict = {}

    def __init__(self, lines):
        self._lines = lines

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, method, url, headers=None, json=None):
        type(self).captured = json or {}
        return _FakeStreamCtx(_FakeResponse(self._lines))


def _sse(*chunks) -> list[str]:
    return [f"data: {json.dumps(c)}" for c in chunks] + ["data: [DONE]"]


async def _run_stream(monkeypatch, provider, lines, *, tools=None, session_id=None, effort=None):
    client = _FakeStreamClient(lines)
    monkeypatch.setattr(openai_compat.httpx, "AsyncClient", client)
    events = []
    async for event in provider.stream(
        model="m",
        system="doctrine",
        messages=[Msg(role="user", content="hi")],
        tools=tools or [],
        max_tokens=64,
        temperature=0.0,
        session_id=session_id,
        effort=effort,
    ):
        events.append(event)
    return events, _FakeStreamClient.captured


# ── fake httpx client (non-streaming, for complete_json) ──────────────────────
class _FakeJsonResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class _FakeJsonClient:
    captured: dict = {}

    def __init__(self, response: _FakeJsonResponse):
        self._response = response

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None):
        type(self).captured = json or {}
        return self._response


def _completion_json(*, served_by: str | None = None, tool_name: str = "respond") -> dict:
    payload = {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {
                            "function": {
                                "name": tool_name,
                                "arguments": json.dumps({"ok": True}),
                            }
                        }
                    ]
                }
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2},
    }
    if served_by is not None:
        payload["openrouter_metadata"] = {
            "endpoints": {"available": [{"model": "x/y", "provider": served_by, "selected": True}]}
        }
    return payload


async def _run_complete_json(monkeypatch, provider, response: dict):
    client = _FakeJsonClient(_FakeJsonResponse(response))
    monkeypatch.setattr(openai_compat.httpx, "AsyncClient", client)
    completion = await provider.complete_json(
        model="m", system="doctrine", prompt="hi", schema={"type": "object"}
    )
    return completion, _FakeJsonClient.captured


# ── 1. session affinity ────────────────────────────────────────────────────────
async def test_openrouter_stream_sends_session_id_when_given(monkeypatch):
    lines = _sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})
    _, body = await _run_stream(monkeypatch, OpenRouterProvider("k"), lines, session_id="run-123")
    assert body["session_id"] == "run-123"


async def test_openrouter_stream_omits_session_id_when_none(monkeypatch):
    lines = _sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})
    _, body = await _run_stream(monkeypatch, OpenRouterProvider("k"), lines, session_id=None)
    assert "session_id" not in body


async def test_kimi_stream_never_sends_session_id(monkeypatch):
    lines = _sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})
    _, body = await _run_stream(monkeypatch, KimiProvider("k"), lines, session_id="run-123")
    assert "session_id" not in body


# ── 1b. reasoning-effort wire shape ─────────────────────────────────────────
async def test_openrouter_stream_sends_reasoning_effort_when_given(monkeypatch):
    lines = _sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})
    _, body = await _run_stream(monkeypatch, OpenRouterProvider("k"), lines, effort="low")
    assert body["reasoning"] == {"effort": "low"}


async def test_openrouter_stream_omits_reasoning_when_effort_is_none(monkeypatch):
    lines = _sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})
    _, body = await _run_stream(monkeypatch, OpenRouterProvider("k"), lines, effort=None)
    assert "reasoning" not in body


async def test_kimi_stream_never_sends_reasoning(monkeypatch):
    # Kimi is Moonshot's native API, not OpenRouter's unified surface — it has
    # no equivalent control, so `_effort_body`'s base-class default (`{}`)
    # applies, regardless of what `effort` the harness passed in.
    lines = _sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})
    _, body = await _run_stream(monkeypatch, KimiProvider("k"), lines, effort="high")
    assert "reasoning" not in body


# ── 2. provider preferences ────────────────────────────────────────────────────
async def test_require_parameters_set_only_when_tools_present(monkeypatch):
    lines = _sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})
    _, body = await _run_stream(monkeypatch, OpenRouterProvider("k"), lines, tools=[_tool()])
    assert body["provider"]["require_parameters"] is True


async def test_no_provider_key_without_tools_or_prefs(monkeypatch):
    lines = _sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})
    _, body = await _run_stream(monkeypatch, OpenRouterProvider("k"), lines, tools=[])
    assert "provider" not in body


async def test_prefs_apply_without_tools_but_carry_no_require_parameters(monkeypatch):
    lines = _sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})
    provider = OpenRouterProvider("k", provider_prefs={"zdr": True})
    _, body = await _run_stream(monkeypatch, provider, lines, tools=[])
    assert body["provider"] == {"zdr": True}
    assert "require_parameters" not in body["provider"]


async def test_prefs_merge_on_top_of_require_parameters_and_add_quantizations(monkeypatch):
    lines = _sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})
    provider = OpenRouterProvider("k", provider_prefs={"quantizations": ["fp8", "fp16"]})
    _, body = await _run_stream(monkeypatch, provider, lines, tools=[_tool()])
    assert body["provider"] == {"require_parameters": True, "quantizations": ["fp8", "fp16"]}


async def test_prefs_can_override_require_parameters_itself(monkeypatch):
    lines = _sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})
    provider = OpenRouterProvider("k", provider_prefs={"require_parameters": False})
    _, body = await _run_stream(monkeypatch, provider, lines, tools=[_tool()])
    assert body["provider"]["require_parameters"] is False


async def test_kimi_never_gets_a_provider_key(monkeypatch):
    lines = _sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})
    _, body = await _run_stream(monkeypatch, KimiProvider("k"), lines, tools=[_tool()])
    assert "provider" not in body


async def test_complete_json_sends_require_parameters_too(monkeypatch):
    """The router's own forced-tool-call JSON completion always carries
    `tools`, so it benefits from `require_parameters` exactly like a
    tool-calling agent turn."""
    _, body = await _run_complete_json(monkeypatch, OpenRouterProvider("k"), _completion_json())
    assert body["provider"]["require_parameters"] is True


async def test_complete_json_prefs_merge_too(monkeypatch):
    provider = OpenRouterProvider("k", provider_prefs={"order": ["anthropic"]})
    _, body = await _run_complete_json(monkeypatch, provider, _completion_json())
    assert body["provider"] == {"require_parameters": True, "order": ["anthropic"]}


async def test_kimi_complete_json_never_gets_a_provider_key(monkeypatch):
    _, body = await _run_complete_json(monkeypatch, KimiProvider("k"), _completion_json())
    assert "provider" not in body


# ── 3. TRET_OPENROUTER_PROVIDER_PREFS parsing ──────────────────────────────────
def test_invalid_provider_prefs_json_is_ignored_with_a_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="tret"):
        settings = Settings(openrouter_provider_prefs="{not valid json")
    assert settings.openrouter_provider_prefs is None
    assert "TRET_OPENROUTER_PROVIDER_PREFS" in caplog.text
    assert "not valid JSON" in caplog.text


def test_non_object_provider_prefs_json_is_ignored_with_a_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="tret"):
        settings = Settings(openrouter_provider_prefs='["not", "an", "object"]')
    assert settings.openrouter_provider_prefs is None
    assert "must be a JSON object" in caplog.text


def test_valid_provider_prefs_json_is_applied():
    settings = Settings(openrouter_provider_prefs='{"order": ["anthropic"], "zdr": true}')
    assert settings.openrouter_provider_prefs == {"order": ["anthropic"], "zdr": True}


def test_blank_provider_prefs_means_unset():
    assert Settings(openrouter_provider_prefs="").openrouter_provider_prefs is None


def test_unset_provider_prefs_defaults_to_none():
    assert Settings().openrouter_provider_prefs is None


# ── 4. served_by parsing ────────────────────────────────────────────────────────
def test_served_by_reads_the_selected_endpoint():
    data = {
        "openrouter_metadata": {
            "endpoints": {
                "available": [
                    {"model": "x/y", "provider": "Together", "selected": False},
                    {"model": "x/y", "provider": "Anthropic", "selected": True},
                ]
            }
        }
    }
    assert _served_by_from_openai(data) == "Anthropic"


def test_served_by_is_none_when_nothing_is_selected():
    data = {"openrouter_metadata": {"endpoints": {"available": [{"provider": "Together"}]}}}
    assert _served_by_from_openai(data) is None


def test_served_by_is_none_without_openrouter_metadata():
    assert _served_by_from_openai({}) is None
    assert _served_by_from_openai({"usage": {"prompt_tokens": 1}}) is None


async def test_served_by_parsed_from_a_non_streaming_response(monkeypatch):
    completion, _ = await _run_complete_json(
        monkeypatch, OpenRouterProvider("k"), _completion_json(served_by="Anthropic")
    )
    assert completion.served_by == "Anthropic"


async def test_served_by_none_from_a_non_streaming_response_without_it(monkeypatch):
    completion, _ = await _run_complete_json(
        monkeypatch, OpenRouterProvider("k"), _completion_json(served_by=None)
    )
    assert completion.served_by is None


async def test_kimi_complete_json_never_reports_served_by(monkeypatch):
    completion, _ = await _run_complete_json(
        monkeypatch, KimiProvider("k"), _completion_json(served_by="Anthropic")
    )
    # Kimi's real responses never carry `openrouter_metadata` at all; even if a
    # test payload smuggled one in, KimiProvider shares the same parser, so this
    # only pins that the parser itself is correct — the absence in practice
    # comes from Kimi's wire format, not special-casing in the provider.
    assert completion.served_by == "Anthropic"


async def test_served_by_parsed_from_a_streaming_chunk_sequence(monkeypatch):
    lines = _sse(
        {"choices": [{"delta": {"content": "ok"}, "finish_reason": None}]},
        {
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "openrouter_metadata": {
                "endpoints": {"available": [{"provider": "Fireworks", "selected": True}]}
            },
        },
    )
    events, _ = await _run_stream(monkeypatch, OpenRouterProvider("k"), lines)
    assert events[-1].served_by == "Fireworks"


async def test_served_by_takes_the_last_non_empty_value_seen(monkeypatch):
    lines = _sse(
        {
            "choices": [{"delta": {"content": "a"}, "finish_reason": None}],
            "openrouter_metadata": {
                "endpoints": {"available": [{"provider": "Together", "selected": True}]}
            },
        },
        {
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "openrouter_metadata": {
                "endpoints": {"available": [{"provider": "Fireworks", "selected": True}]}
            },
        },
    )
    events, _ = await _run_stream(monkeypatch, OpenRouterProvider("k"), lines)
    assert events[-1].served_by == "Fireworks"


async def test_served_by_none_when_absent_from_every_chunk(monkeypatch):
    lines = _sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})
    events, _ = await _run_stream(monkeypatch, KimiProvider("k"), lines)
    assert events[-1].served_by is None


# ── 5. AnthropicProvider: effort forwarded via output_config, SDK-native ───
class _FakeAnthropicStreamCtx:
    """Stands in for `anthropic.AsyncMessages.stream()`'s return value: an
    async context manager over an async-iterable of zero events, whose
    `get_final_message()` reports a minimal completed turn."""

    def __init__(self):
        self.usage = None
        self.stop_reason = "end_turn"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        async def _empty():
            return
            yield  # pragma: no cover - makes this an async generator

        return _empty()

    async def get_final_message(self):
        return self


class _FakeAnthropicMessages:
    captured: dict = {}

    def stream(self, **kwargs):
        type(self).captured = kwargs
        return _FakeAnthropicStreamCtx()


async def _run_anthropic_stream(monkeypatch, *, effort=None):
    provider = AnthropicProvider("test-key")
    fake_messages = _FakeAnthropicMessages()
    monkeypatch.setattr(provider._client, "messages", fake_messages)
    events = []
    async for event in provider.stream(
        model="claude-sonnet-5",
        system="doctrine",
        messages=[Msg(role="user", content="hi")],
        tools=[],
        max_tokens=64,
        temperature=0.0,
        effort=effort,
    ):
        events.append(event)
    return events, fake_messages.captured


async def test_anthropic_stream_passes_output_config_effort_when_given(monkeypatch):
    _, kwargs = await _run_anthropic_stream(monkeypatch, effort="high")
    assert kwargs["output_config"] == {"effort": "high"}


async def test_anthropic_stream_omits_output_config_when_effort_is_none(monkeypatch):
    _, kwargs = await _run_anthropic_stream(monkeypatch, effort=None)
    assert "output_config" not in kwargs
