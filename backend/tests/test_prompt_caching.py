"""Prompt-cache breakpoints and cache-aware cost/token accounting.

No network: the Anthropic and OpenAI-compat request builders are exercised
directly, and the SSE stream is driven through a fake httpx client.
"""
import json
from decimal import Decimal

import pytest

from bench.providers import anthropic as anthropic_module
from bench.providers import base as base_module
from bench.providers import openai_compat
from bench.providers.anthropic import (
    MAX_CACHE_BREAKPOINTS,
    MESSAGE_CACHE_BREAKPOINTS,
    _apply_conversation_cache,
    _to_anthropic_messages,
)
from bench.providers.base import Msg, ToolCall, ToolCallComplete, TextDelta, Usage
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


# ── one cache-breakpoint helper, not one per provider ─────────────────────────
def test_both_providers_use_the_same_breakpoint_helper():
    """It is the same wire format and the same four-per-request budget: two
    byte-identical copies could drift into two different budgets."""
    assert anthropic_module.mark_cache_breakpoint is base_module.mark_cache_breakpoint
    assert openai_compat.mark_cache_breakpoint is base_module.mark_cache_breakpoint
    assert not hasattr(anthropic_module, "_mark_cache_breakpoint")
    assert not hasattr(openai_compat, "_mark_cache_breakpoint")


# ── cache *writes* on an OpenAI-shaped usage object ───────────────────────────
# OpenRouter writes cache breakpoints (above), so it is billed for cache
# creation at a premium over input price. A translation that can only ever
# report reads records those tokens as ordinary input and understates the run.
def test_cache_creation_tokens_are_recorded_as_a_cache_write():
    usage = _usage_from_openai(
        {
            "prompt_tokens": 1000,
            "completion_tokens": 10,
            "cache_creation_input_tokens": 400,
        }
    )
    assert usage.cache_write_tokens == 400
    assert usage.input_tokens == 600  # not counted twice as input
    assert usage.cache_read_tokens == 0


def test_cache_writes_are_read_from_prompt_token_details_too():
    usage = _usage_from_openai(
        {
            "prompt_tokens": 1000,
            "prompt_tokens_details": {"cached_tokens": 300, "cache_creation_tokens": 200},
        }
    )
    assert (usage.cache_read_tokens, usage.cache_write_tokens) == (300, 200)
    assert usage.input_tokens == 500  # the four buckets still sum to prompt_tokens


@pytest.mark.parametrize(
    "usage",
    [
        {"prompt_tokens": 100},
        {"prompt_tokens": 100, "cache_creation_input_tokens": None},
        {"prompt_tokens": 100, "cache_creation_input_tokens": "400"},
        {"prompt_tokens": 100, "cache_creation_input_tokens": True},
        {"prompt_tokens": 100, "cache_creation_input_tokens": -5},
    ],
)
def test_absent_or_junk_cache_writes_read_as_zero(usage):
    translated = _usage_from_openai(usage)
    assert translated.cache_write_tokens == 0
    assert translated.input_tokens == 100


def test_cache_writes_cannot_exceed_what_is_left_of_the_prompt():
    usage = _usage_from_openai(
        {
            "prompt_tokens": 500,
            "prompt_tokens_details": {"cached_tokens": 400},
            "cache_creation_input_tokens": 9999,
        }
    )
    assert (usage.cache_read_tokens, usage.cache_write_tokens) == (400, 100)
    assert usage.input_tokens == 0


async def test_stream_reports_cache_writes(monkeypatch):
    lines = _sse(
        {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]},
        {
            "choices": [],
            "usage": {
                "prompt_tokens": 2000,
                "completion_tokens": 12,
                "prompt_tokens_details": {"cached_tokens": 0},
                "cache_creation_input_tokens": 1500,
            },
        },
    )
    events, _ = await _run_stream(monkeypatch, OpenRouterProvider("k"), lines)
    usage = events[-1].usage
    assert usage.cache_write_tokens == 1500
    assert usage.input_tokens == 500


# ── tool-call aggregation is scoped to one choice ─────────────────────────────
def _tool_delta(choice_index: int, tool_index: int, *, id=None, name=None, args=None) -> dict:
    call: dict = {"index": tool_index, "function": {}}
    if id:
        call["id"] = id
    if name:
        call["function"]["name"] = name
    if args is not None:
        call["function"]["arguments"] = args
    return {"index": choice_index, "delta": {"tool_calls": [call]}}


async def test_tool_calls_from_separate_choices_are_never_merged(monkeypatch):
    """The bug: `index` numbers tool calls *within* a choice, so aggregating on it
    alone concatenated the argument fragments of two unrelated tool calls into one
    unparseable action. A provider that returns a second candidate completion must
    not be able to corrupt the first one's tool call."""
    lines = _sse(
        {
            "choices": [
                _tool_delta(0, 0, id="call_a", name="read_document", args='{"id": "'),
                _tool_delta(1, 0, id="call_b", name="write_finding", args='{"claim": "'),
            ]
        },
        {
            "choices": [
                _tool_delta(0, 0, args='doc-1"}'),
                _tool_delta(1, 0, args='other"}'),
            ]
        },
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    )
    events, _ = await _run_stream(monkeypatch, KimiProvider("k"), lines)

    calls = [e.tool_call for e in events if isinstance(e, ToolCallComplete)]
    assert len(calls) == 1  # only the primary choice is acted on
    assert calls[0].id == "call_a"
    assert calls[0].name == "read_document"
    assert calls[0].arguments == {"id": "doc-1"}  # not a merged, unparseable blob
    assert "_raw" not in calls[0].arguments
    assert events[-1].stop_reason == "tool_use"


async def test_text_from_a_second_choice_is_not_interleaved(monkeypatch):
    lines = _sse(
        {
            "choices": [
                {"index": 0, "delta": {"content": "the answer"}},
                {"index": 1, "delta": {"content": "A DIFFERENT ANSWER"}},
            ]
        },
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    )
    events, _ = await _run_stream(monkeypatch, KimiProvider("k"), lines)
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["the answer"]


async def test_parallel_tool_calls_within_one_choice_still_both_arrive(monkeypatch):
    """The fix must not break the normal case it looks like: two tool calls in the
    same choice, distinguished by their own index."""
    lines = _sse(
        {
            "choices": [
                _tool_delta(0, 0, id="c1", name="read_document", args='{"id":"a"}'),
                _tool_delta(0, 1, id="c2", name="lookup_dataset", args='{"name":"b"}'),
            ]
        },
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    )
    events, _ = await _run_stream(monkeypatch, KimiProvider("k"), lines)
    calls = [e.tool_call for e in events if isinstance(e, ToolCallComplete)]
    assert [(c.id, c.name, c.arguments) for c in calls] == [
        ("c1", "read_document", {"id": "a"}),
        ("c2", "lookup_dataset", {"name": "b"}),
    ]


async def test_a_stream_that_omits_choice_index_still_aggregates(monkeypatch):
    """Plenty of OpenAI-compatible servers omit `index` entirely on a single
    completion; those fragments belong to one tool call, not several."""
    lines = _sse(
        {"choices": [{"delta": {"tool_calls": [{"id": "c1", "function": {"name": "read_document",
                                                                        "arguments": '{"id":'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"function": {"arguments": '"a"}'}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    )
    events, _ = await _run_stream(monkeypatch, KimiProvider("k"), lines)
    calls = [e.tool_call for e in events if isinstance(e, ToolCallComplete)]
    assert [(c.id, c.arguments) for c in calls] == [("c1", {"id": "a"})]


# ── Anthropic: an empty assistant turn ───────────────────────────────────────
def test_an_empty_assistant_turn_is_dropped_not_sent_as_an_empty_text_block():
    """The API rejects an empty text block ("text content blocks must be
    non-empty"), so one empty assistant turn in the history would fail every
    subsequent request of the run that re-sends it."""
    messages = _to_anthropic_messages(
        [
            Msg(role="user", content="analyze this"),
            Msg(role="assistant", content=""),  # model returned nothing
            Msg(role="user", content="still there?"),
        ]
    )
    assert all(
        block.get("text") != ""
        for message in messages
        for block in (message["content"] if isinstance(message["content"], list) else [])
    )
    assert [m["role"] for m in messages] == ["user"]  # the two user turns merged
    assert messages[0]["content"] == [
        {"type": "text", "text": "analyze this"},
        {"type": "text", "text": "still there?"},
    ]


def test_an_assistant_turn_with_only_tool_calls_is_still_sent():
    messages = _to_anthropic_messages(
        [Msg(role="assistant", tool_calls=[ToolCall("t1", "read_document", {"id": "d"})])]
    )
    assert messages == [
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "t1", "name": "read_document",
                         "input": {"id": "d"}}],
        }
    ]


def test_a_trailing_empty_assistant_turn_leaves_a_valid_request():
    messages = _to_anthropic_messages(
        [Msg(role="user", content="hi"), Msg(role="assistant", content=None)]
    )
    assert messages == [{"role": "user", "content": "hi"}]
