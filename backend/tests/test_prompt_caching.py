"""Prompt-cache breakpoints and cache-aware cost/token accounting.

No network: the Anthropic and OpenAI-compat request builders are exercised
directly, and the SSE stream is driven through a fake httpx client.
"""
import json
from decimal import Decimal

import pytest

from bench.providers import openai_compat
from bench.providers.anthropic import (
    MAX_CACHE_BREAKPOINTS,
    MESSAGE_CACHE_BREAKPOINTS,
    _apply_conversation_cache,
    _to_anthropic_messages,
)
from bench.providers.base import Msg, ToolCall, Usage
from bench.providers.catalog import (
    CACHE_READ_MULTIPLIER,
    CACHE_WRITE_MULTIPLIER,
    ModelInfo,
)
from bench.providers.openai_compat import (
    KimiProvider,
    OpenRouterProvider,
    _cached_prompt_tokens,
    _to_openai_messages,
    _usage_from_openai,
)


def _model(input_price="3", output_price="15") -> ModelInfo:
    return ModelInfo(
        id="anthropic/test",
        provider="anthropic",
        wire_id="test-1",
        display_name="Test",
        context_window=200_000,
        input_price_per_mtok=Decimal(input_price),
        output_price_per_mtok=Decimal(output_price),
        cost_tier="standard",
    )


def _breakpoints(blocks) -> int:
    """Count cache_control markers across a system/messages payload."""
    total = 0
    for entry in blocks:
        content = entry.get("content", entry) if isinstance(entry, dict) else entry
        if isinstance(content, dict):
            content = [content]
        if not isinstance(content, list):
            continue  # plain string or null body: nothing markable
        total += sum(1 for b in content if isinstance(b, dict) and "cache_control" in b)
    return total


# ── Usage ─────────────────────────────────────────────────────────────────────
def test_usage_defaults_include_cache_buckets():
    usage = Usage()
    assert (usage.cache_read_tokens, usage.cache_write_tokens) == (0, 0)
    usage = Usage(input_tokens=10, output_tokens=2, cache_read_tokens=90, cache_write_tokens=5)
    assert usage.cache_write_tokens == 5


# ── cost math ─────────────────────────────────────────────────────────────────
def test_cost_ignores_cache_when_absent():
    # Back-compat: the two-argument call is unchanged.
    assert _model().cost_usd(1_000_000, 1_000_000) == Decimal(18)


def test_cache_reads_are_a_tenth_of_input_price():
    model = _model()
    assert model.cost_usd(0, 0, cache_read_tokens=1_000_000) == Decimal("0.3")
    assert CACHE_READ_MULTIPLIER == Decimal("0.1")


def test_cache_writes_are_a_premium_over_input_price():
    model = _model()
    assert model.cost_usd(0, 0, cache_write_tokens=1_000_000) == Decimal("3.75")
    assert CACHE_WRITE_MULTIPLIER == Decimal("1.25")


def test_cost_sums_all_four_buckets():
    model = _model()
    cost = model.cost_usd(100_000, 10_000, 800_000, 100_000)
    expected = (
        Decimal(3) * 100_000
        + Decimal(15) * 10_000
        + Decimal(3) * Decimal("0.1") * 800_000
        + Decimal(3) * Decimal("1.25") * 100_000
    ) / Decimal(1_000_000)
    assert cost == expected


def test_cache_multipliers_are_overridable():
    model = _model()
    doubled = model.cost_usd(0, 0, 1_000_000, 0, cache_read_multiplier=Decimal("0.2"))
    assert doubled == Decimal("0.6")


def test_caching_is_cheaper_than_re_sending_the_prefix():
    """A warm cache read must beat paying full input price for the same tokens."""
    model = _model()
    cached = model.cost_usd(0, 0, cache_read_tokens=500_000)
    uncached = model.cost_usd(500_000, 0)
    assert cached < uncached


# ── Anthropic breakpoints ─────────────────────────────────────────────────────
def test_conversation_breakpoint_lands_on_final_content_block():
    messages = _to_anthropic_messages(
        [
            Msg(role="user", content="analyze this"),
            Msg(role="assistant", tool_calls=[ToolCall("t1", "read_document", {"id": "d"})]),
            Msg(role="tool", content="doc text", tool_call_id="t1"),
        ]
    )
    _apply_conversation_cache(messages)
    assert messages[-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}


def test_string_user_content_is_promoted_to_a_markable_block():
    messages = _to_anthropic_messages([Msg(role="user", content="hello")])
    assert isinstance(messages[0]["content"], str)  # unmarked form is a plain string
    _apply_conversation_cache(messages)
    assert messages[0]["content"] == [
        {"type": "text", "text": "hello", "cache_control": {"type": "ephemeral"}}
    ]


def test_conversation_breakpoints_respect_the_request_budget():
    # Ten user turns: only MESSAGE_CACHE_BREAKPOINTS may be marked, leaving room
    # for the system prompt within Anthropic's limit of four.
    convo = []
    for i in range(10):
        convo.append(Msg(role="user", content=f"q{i}"))
        convo.append(Msg(role="assistant", content=f"a{i}"))
    messages = _to_anthropic_messages(convo)
    _apply_conversation_cache(messages)
    marked = _breakpoints(messages)
    assert marked == MESSAGE_CACHE_BREAKPOINTS
    assert marked + 1 <= MAX_CACHE_BREAKPOINTS  # +1 for the system prompt


def test_conversation_breakpoints_anchor_at_user_turns():
    messages = _to_anthropic_messages(
        [
            Msg(role="user", content="first"),
            Msg(role="assistant", content="mid"),
            Msg(role="user", content="second"),
        ]
    )
    _apply_conversation_cache(messages, budget=2)
    assert "cache_control" in messages[2]["content"][-1]
    assert "cache_control" in messages[0]["content"][-1]
    assert _breakpoints([messages[1]]) == 0  # assistant turn is not an anchor


def test_empty_history_is_left_alone():
    messages: list[dict] = []
    _apply_conversation_cache(messages)
    assert messages == []


def test_breakpoints_are_not_duplicated_on_reapplication():
    messages = _to_anthropic_messages([Msg(role="user", content="hello")])
    _apply_conversation_cache(messages, budget=3)
    _apply_conversation_cache(messages, budget=3)
    assert _breakpoints(messages) == 1


# ── OpenAI-compat cached-token parsing ────────────────────────────────────────
@pytest.mark.parametrize(
    "usage,expected",
    [
        ({"prompt_tokens_details": {"cached_tokens": 512}}, 512),
        ({"prompt_tokens_details": {"cached_tokens": 0}}, 0),
        ({}, 0),
        ({"prompt_tokens_details": None}, 0),
        ({"prompt_tokens_details": {}}, 0),
        ({"prompt_tokens_details": {"cached_tokens": None}}, 0),
        ({"prompt_tokens_details": {"cached_tokens": "512"}}, 0),
        ({"prompt_tokens_details": {"cached_tokens": True}}, 0),
        ({"prompt_tokens_details": {"cached_tokens": -5}}, 0),
        ({"prompt_tokens_details": [{"cached_tokens": 5}]}, 0),
    ],
)
def test_cached_prompt_tokens_is_defensive(usage, expected):
    assert _cached_prompt_tokens(usage) == expected


def test_cached_tokens_are_subtracted_from_prompt_total():
    # OpenAI-style prompt_tokens is inclusive of cached tokens; Usage is not.
    usage = _usage_from_openai(
        {
            "prompt_tokens": 1000,
            "completion_tokens": 40,
            "prompt_tokens_details": {"cached_tokens": 900},
        }
    )
    assert (usage.input_tokens, usage.cache_read_tokens, usage.output_tokens) == (100, 900, 40)


def test_usage_without_cache_details_is_all_uncached():
    usage = _usage_from_openai({"prompt_tokens": 1000, "completion_tokens": 40})
    assert (usage.input_tokens, usage.cache_read_tokens) == (1000, 0)


def test_cached_tokens_cannot_exceed_prompt_tokens():
    usage = _usage_from_openai(
        {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 9999}}
    )
    assert usage.input_tokens == 0 and usage.cache_read_tokens == 100


# ── OpenRouter cache_control passthrough ──────────────────────────────────────
def _body(provider, messages):
    body = {"model": "m", "messages": _to_openai_messages("doctrine", messages)}
    provider._apply_cache_control(body)
    return body


CONVO = [
    Msg(role="user", content="analyze this"),
    Msg(role="assistant", tool_calls=[ToolCall("t1", "read_document", {"id": "d"})]),
    Msg(role="tool", content="doc text", tool_call_id="t1"),
]


def test_openrouter_marks_system_and_user_messages():
    body = _body(OpenRouterProvider("k"), CONVO)
    system, user, assistant, tool = body["messages"]
    assert system["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert user["content"][-1]["cache_control"] == {"type": "ephemeral"}
    # Assistant/tool turns stay plain strings — OpenRouter's per-model handling of
    # structured tool content is not uniform.
    assert assistant["content"] is None
    assert tool["content"] == "doc text"


def test_openrouter_respects_the_four_breakpoint_limit():
    convo = []
    for i in range(10):
        convo.append(Msg(role="user", content=f"q{i}"))
        convo.append(Msg(role="assistant", content=f"a{i}"))
    body = _body(OpenRouterProvider("k"), convo)
    assert _breakpoints(body["messages"]) == 4


def test_kimi_body_carries_no_cache_control():
    body = _body(KimiProvider("k"), CONVO)
    assert _breakpoints(body["messages"]) == 0
    assert body["messages"][0]["content"] == "doctrine"  # still a plain string


def test_base_hook_is_a_noop():
    provider = openai_compat.OpenAICompatProvider("k", base_url="https://example.test/v1")
    body = _body(provider, CONVO)
    assert _breakpoints(body["messages"]) == 0


# ── end-to-end stream parsing (fake transport) ────────────────────────────────
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


class _FakeClient:
    """Stands in for httpx.AsyncClient, recording the request body."""

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


async def _run_stream(monkeypatch, provider, lines):
    client = _FakeClient(lines)
    monkeypatch.setattr(openai_compat.httpx, "AsyncClient", client)
    events = []
    async for event in provider.stream(
        model="m",
        system="doctrine",
        messages=[Msg(role="user", content="hi")],
        tools=[],
        max_tokens=64,
        temperature=0.0,
    ):
        events.append(event)
    return events, _FakeClient.captured


async def test_stream_reports_cached_tokens(monkeypatch):
    lines = _sse(
        {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]},
        {
            "choices": [],
            "usage": {
                "prompt_tokens": 2000,
                "completion_tokens": 12,
                "prompt_tokens_details": {"cached_tokens": 1800},
            },
        },
    )
    events, _ = await _run_stream(monkeypatch, KimiProvider("k"), lines)
    usage = events[-1].usage
    assert usage.input_tokens == 200
    assert usage.cache_read_tokens == 1800
    assert usage.output_tokens == 12


async def test_stream_without_cache_details_still_works(monkeypatch):
    lines = _sse(
        {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 2000, "completion_tokens": 12}},
    )
    events, _ = await _run_stream(monkeypatch, KimiProvider("k"), lines)
    usage = events[-1].usage
    assert (usage.input_tokens, usage.cache_read_tokens) == (2000, 0)


async def test_openrouter_stream_sends_cache_control(monkeypatch):
    lines = _sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})
    _, body = await _run_stream(monkeypatch, OpenRouterProvider("k"), lines)
    assert _breakpoints(body["messages"]) == 2  # system + the single user turn


async def test_kimi_stream_sends_no_cache_control(monkeypatch):
    lines = _sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})
    _, body = await _run_stream(monkeypatch, KimiProvider("k"), lines)
    assert _breakpoints(body["messages"]) == 0
