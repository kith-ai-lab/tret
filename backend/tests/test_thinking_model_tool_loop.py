"""Open-weight thinking models in the tool loop (Kimi K3, DeepSeek V4 ...).

1. Reasoning is captured from each assistant turn and replayed unmodified on
   later requests of the same run (OpenRouter `reasoning_details` / `reasoning`,
   Kimi native `reasoning_content`); never to generic/local servers, never to a
   different model than produced it.
2. `execute_tool` argument errors name the accepted parameters.
3. The harness reminds the model when the same call keeps failing identically.

OpenRouter doc relied on:
https://openrouter.ai/docs/use-cases/reasoning-tokens (chat completions:
pass `reasoning_details`, or the `reasoning` string, back on the assistant
message unmodified and in order).
"""
from __future__ import annotations

import json

import pytest

from tret.engine import compaction
from tret.engine.harness import (
    IDENTICAL_FAILURE_STOP_DEFAULT,
    _FailureStreak,
    _identical_failure_limit,
    _note_repeated_failure,
)
from tret.engine.tools import execute_tool
from tret.providers import openai_compat
from tret.providers.base import Msg, ToolCall, ToolSpec, TurnComplete
from tret.providers.local import LocalProvider
from tret.providers.openai_compat import KimiProvider, OpenRouterProvider, _to_openai_messages


# ── fake transport that records the request body ────────────────────────────
class _Resp:
    headers: dict = {}

    def __init__(self, chunks, status_code=200, error_body=""):
        self.status_code = status_code
        self._error_body = error_body
        self._lines = [f"data: {json.dumps(c)}" for c in chunks] + ["data: [DONE]"]

    async def aread(self):
        return self._error_body.encode()

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _Ctx:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *exc):
        return False


class _Client:
    def __init__(self, chunks, failures=()):
        self._chunks = chunks
        self._failures = list(failures)  # [(status, body)] consumed one per request
        self.bodies: list[dict] = []

    def __call__(self, *a, **k):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, method, url, headers=None, json=None):
        import copy

        self.bodies.append(copy.deepcopy(json))
        if self._failures:
            status, text = self._failures.pop(0)
            return _Ctx(_Resp([], status, text))
        return _Ctx(_Resp(self._chunks))

    async def get(self, url, headers=None):
        raise AssertionError("unexpected GET")


async def _turn(
    monkeypatch, provider, chunks, messages=None, model="moonshotai/kimi-k3", failures=()
):
    client = _Client(chunks, failures)
    monkeypatch.setattr(openai_compat.httpx, "AsyncClient", client)
    events = []
    async for ev in provider.stream(
        model=model,
        system="s",
        messages=messages or [Msg(role="user", content="hi")],
        tools=[],
        max_tokens=64,
        temperature=0.0,
    ):
        events.append(ev)
    return events, client


DETAILS = [
    {"type": "reasoning.text", "text": "think ", "index": 0, "format": "unknown"},
    {"type": "reasoning.text", "text": "more", "index": 0, "signature": "sig"},
    {"type": "reasoning.encrypted", "data": "abc==", "index": 1},
]


async def test_openrouter_captures_reasoning_details_merged_in_order(monkeypatch):
    chunks = [
        {"choices": [{"delta": {"reasoning_details": [DETAILS[0]]}}]},
        {"choices": [{"delta": {"reasoning_details": [DETAILS[1], DETAILS[2]]}}]},
        {"choices": [{"delta": {"content": "x"}, "finish_reason": "stop"}]},
    ]
    events, _ = await _turn(monkeypatch, OpenRouterProvider("k"), chunks)
    done = events[-1]
    assert isinstance(done, TurnComplete)
    assert done.reasoning["model"] == "moonshotai/kimi-k3"
    details = done.reasoning["reasoning_details"]
    assert details[0] == {
        "type": "reasoning.text", "text": "think more", "index": 0,
        "format": "unknown", "signature": "sig",
    }
    assert details[1] == DETAILS[2]


async def test_openrouter_falls_back_to_reasoning_string(monkeypatch):
    chunks = [
        {"choices": [{"delta": {"reasoning": "a"}}]},
        {"choices": [{"delta": {"reasoning_content": "b"}, "finish_reason": "stop"}]},
    ]
    events, _ = await _turn(monkeypatch, OpenRouterProvider("k"), chunks)
    assert events[-1].reasoning == {"model": "moonshotai/kimi-k3", "reasoning": "ab"}


async def test_kimi_native_uses_reasoning_content(monkeypatch):
    chunks = [{"choices": [{"delta": {"reasoning_content": "hmm"}, "finish_reason": "stop"}]}]
    events, _ = await _turn(monkeypatch, KimiProvider("k"), chunks, model="kimi-k3")
    assert events[-1].reasoning == {"model": "kimi-k3", "reasoning_content": "hmm"}


async def test_no_reasoning_means_none_and_local_never_captures(monkeypatch):
    chunks = [{"choices": [{"delta": {"content": "x", "reasoning": "r"}, "finish_reason": "stop"}]}]
    events, _ = await _turn(monkeypatch, LocalProvider("http://x"), chunks)
    assert events[-1].reasoning is None
    events, _ = await _turn(
        monkeypatch, OpenRouterProvider("k"),
        [{"choices": [{"delta": {"content": "x"}, "finish_reason": "stop"}]}],
    )
    assert events[-1].reasoning is None


def _history(reasoning):
    return [
        Msg(role="user", content="go"),
        Msg(
            role="assistant",
            tool_calls=[ToolCall("c1", "t", {"a": 1})],
            reasoning=reasoning,
        ),
        Msg(role="tool", content="ok", tool_call_id="c1"),
    ]


async def test_reasoning_replayed_unmodified_on_next_request(monkeypatch):
    rec = {"model": "moonshotai/kimi-k3", "reasoning_details": DETAILS}
    chunks = [{"choices": [{"delta": {"content": "x"}, "finish_reason": "stop"}]}]
    _, client = await _turn(monkeypatch, OpenRouterProvider("k"), chunks, _history(rec))
    assistant = client.bodies[0]["messages"][2]
    assert assistant["reasoning_details"] == DETAILS
    assert "model" not in assistant and assistant["tool_calls"][0]["id"] == "c1"

    rec = {"model": "kimi-k3", "reasoning_content": "hmm"}
    _, client = await _turn(
        monkeypatch, KimiProvider("k"), chunks, _history(rec), model="kimi-k3"
    )
    assert client.bodies[0]["messages"][2]["reasoning_content"] == "hmm"


def test_reasoning_not_replayed_to_other_model_or_non_supporting_provider():
    rec = {"model": "a/b", "reasoning": "r"}
    msgs = _history(rec)
    assert "reasoning" not in _to_openai_messages("s", msgs, replay_reasoning=True, model="c/d")[2]
    assert "reasoning" not in _to_openai_messages("s", msgs)[2]
    assert _to_openai_messages("s", msgs, replay_reasoning=True, model="a/b")[2]["reasoning"] == "r"


def test_msg_roundtrip_and_old_snapshots():
    rec = {"model": "m", "reasoning": "r"}
    m = Msg(role="assistant", content="x", reasoning=rec)
    assert Msg.from_json(m.to_json()).reasoning == rec
    plain = Msg(role="assistant", content="x").to_json()
    assert "reasoning" not in plain and Msg.from_json(plain).reasoning is None


# ── compaction ───────────────────────────────────────────────────────────────
def test_compaction_keeps_reasoning_on_kept_turns_and_counts_it():
    rec = {"model": "m", "reasoning": "r" * 4000}
    msgs = _history(rec)
    assert compaction.estimate_message_tokens(msgs[1]) > 900
    state = compaction.CompactionState()
    assert compaction.wire_view(msgs, state)[1].reasoning == rec
    state.elided[1] = 3  # even if a turn is rewritten, its reasoning stays paired
    assert compaction.wire_view(msgs, state)[1].reasoning == rec


def test_trim_history_drops_whole_turns_with_their_reasoning():
    big = Msg(role="assistant", content="x" * 4000, reasoning={"model": "m", "reasoning": "r" * 4000})
    kept, dropped = compaction.trim_history(
        [big, Msg(role="user", content="u")], system="", user_message="q", tools=[], limit=100
    )
    assert dropped >= 1 and all(m.reasoning is None for m in kept)


# ── execute_tool argument errors ─────────────────────────────────────────────
async def _handler(ctx, source_document_id, title, review_note=None, stem=None):
    return "ok"


_SPEC = ToolSpec(
    name="propose_package_map",
    description="d",
    parameters={"type": "object", "required": ["source_document_id", "title"], "properties": {}},
    handler=_handler,
)


async def test_unknown_argument_suggests_closest():
    text, err = await execute_tool(
        None, _SPEC, {"source_document_id": "d", "title": "t", "review_note_text": "x"}
    )
    assert err and text.startswith("Tool error: ")
    assert (
        "propose_package_map: unknown argument 'review_note_text' (did you mean 'review_note'?). "
        "Accepted: source_document_id*, title*, review_note, stem (* required). "
        "Call it again with only these names." in text
    )


async def test_missing_required_argument():
    text, err = await execute_tool(None, _SPEC, {"title": "t"})
    assert err and "missing required argument(s) 'source_document_id'" in text
    assert "source_document_id*, title*" in text


async def test_unparseable_json_raw():
    text, err = await execute_tool(None, _SPEC, {"_raw": '{"title": '})
    assert err and "not valid JSON" in text and "source_document_id*" in text
    assert "unknown argument" not in text


async def test_valid_call_and_kwargs_handlers_untouched():
    assert await execute_tool(None, _SPEC, {"source_document_id": "d", "title": "t"}) == ("ok", False)

    async def loose(ctx, **kw):
        return "loose"

    spec = ToolSpec("x", "d", {}, handler=loose)
    assert await execute_tool(None, spec, {"anything": 1}) == ("loose", False)


# ── repeated identical failure reminder ──────────────────────────────────────
def test_repeated_identical_failure_gets_reminder_on_third():
    failed: dict[str, int] = {}
    out = [_note_repeated_failure(failed, "t:{}", "Tool error: bad") for _ in range(3)]
    assert out[0] == ("Tool error: bad", 0) and out[1] == ("Tool error: bad", 0)
    text, n = out[2]
    assert n == 3 and "This exact call has failed 3 times with the same error" in text
    assert text.startswith("Tool error: bad")


def test_different_error_or_call_resets_count():
    failed: dict[str, int] = {}
    for _ in range(2):
        _note_repeated_failure(failed, "t:{}", "Tool error: a")
    assert _note_repeated_failure(failed, "t:{}", "Tool error: b")[1] == 0
    assert _note_repeated_failure(failed, "t:{'x':1}", "Tool error: a")[1] == 0


# ── stuck-call hard stop ────────────────────────────────────────────────────
def test_the_streak_trips_on_the_third_identical_failure_in_a_row():
    streak = _FailureStreak(3)
    streak.record("t", "t:{}", "Tool error: bad", True)
    assert not streak.one_from_stop and not streak.tripped
    streak.record("t", "t:{}", "Tool error: bad", True)
    assert streak.one_from_stop and not streak.tripped
    assert "failed 2 times in a row" in streak.warning("record_verdict")
    assert "(record_verdict)" in streak.warning("record_verdict")
    streak.record("t", "t:{}", "Tool error: bad", True)
    assert streak.tripped and streak.tool == "t" and streak.count == 3


def test_any_other_call_or_error_or_a_success_restarts_the_streak():
    streak = _FailureStreak(3)
    for _ in range(2):
        streak.record("t", "t:{}", "Tool error: bad", True)
    streak.record("t", "t:{}", "Tool error: different", True)  # same call, new error
    assert streak.count == 1
    streak.record("u", "u:{}", "Tool error: different", True)  # new call, same error
    assert streak.count == 1 and streak.tool == "u"
    streak.record("u", "u:{}", "Tool error: different", True)
    streak.record("u", "u:{}", "ok", False)  # the same call succeeding
    assert streak.count == 0 and not streak.tripped
    streak.record("u", "u:{}", "Tool error: different", True)
    streak.reset()  # a model switch
    assert streak.count == 0 and streak.tool == ""


def test_a_limit_of_zero_never_trips_or_warns():
    streak = _FailureStreak(0)
    for _ in range(10):
        streak.record("t", "t:{}", "Tool error: bad", True)
        assert not streak.one_from_stop
    assert not streak.tripped


def test_the_warning_reads_once_for_a_limit_of_two():
    streak = _FailureStreak(2)
    streak.record("t", "t:{}", "Tool error: bad", True)
    assert streak.one_from_stop and "has now failed once" in streak.warning("x")


@pytest.mark.parametrize(
    ("loop_config", "expected"),
    [
        ({}, IDENTICAL_FAILURE_STOP_DEFAULT),
        ({"max_identical_failures": 5}, 5),
        ({"max_identical_failures": "4"}, 4),
        ({"max_identical_failures": 1}, 2),
        ({"max_identical_failures": 0}, 0),
        ({"max_identical_failures": -3}, 0),
        ({"max_identical_failures": False}, 0),
        ({"max_identical_failures": None}, IDENTICAL_FAILURE_STOP_DEFAULT),
        ({"max_identical_failures": "many"}, IDENTICAL_FAILURE_STOP_DEFAULT),
    ],
)
def test_the_limit_comes_from_loop_config(loop_config, expected):
    assert IDENTICAL_FAILURE_STOP_DEFAULT == 3
    assert _identical_failure_limit(loop_config) == expected


# ── review fixes ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", [None, 5, True, [1, 2], "str"])
async def test_non_object_arguments_are_a_tool_error_not_a_crash(bad):
    text, err = await execute_tool(None, _SPEC, bad)
    assert err and text.startswith("Tool error: ")
    assert "arguments must be a JSON object" in text
    assert "source_document_id*" in text

    async def loose(ctx, **kw):
        return "loose"

    text, err = await execute_tool(None, ToolSpec("x", "d", {}, handler=loose), bad)
    assert err and "arguments must be a JSON object" in text


def test_merge_does_not_splice_distinct_ids_and_drops_redacted():
    from tret.providers.openai_compat import _merge_reasoning_details

    out = _merge_reasoning_details([
        {"type": "reasoning.text", "text": "a", "index": 0, "id": "x"},
        {"type": "reasoning.text", "text": "b", "index": 0, "id": "y"},
        {"type": "reasoning.text", "text": "c", "index": 0},  # no id: joins previous
        {"type": "reasoning.encrypted", "data": "[REDACTED]", "index": 1},
        {"type": "reasoning.encrypted", "data": "real==", "index": 2},
    ])
    assert [b["text"] for b in out[:2]] == ["a", "bc"]
    assert len(out) == 3 and out[2]["data"] == "real=="


async def test_only_redacted_blocks_store_no_reasoning(monkeypatch):
    chunks = [
        {"choices": [{"delta": {"reasoning_details": [
            {"type": "reasoning.encrypted", "data": "[REDACTED]", "index": 0}
        ]}}]},
        {"choices": [{"delta": {"content": "x"}, "finish_reason": "stop"}]},
    ]
    events, _ = await _turn(monkeypatch, OpenRouterProvider("k"), chunks)
    assert events[-1].reasoning is None


async def test_400_mentioning_reasoning_retries_once_without_replay(monkeypatch):
    rec = {"model": "moonshotai/kimi-k3", "reasoning_details": DETAILS}
    chunks = [{"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}]
    events, client = await _turn(
        monkeypatch, OpenRouterProvider("k"), chunks, _history(rec),
        failures=[(400, '{"error":"Invalid encrypted reasoning signature"}')],
    )
    assert len(client.bodies) == 2
    assert client.bodies[0]["messages"][2]["reasoning_details"] == DETAILS
    assert "reasoning_details" not in client.bodies[1]["messages"][2]
    assert client.bodies[1]["messages"][2]["tool_calls"][0]["id"] == "c1"
    assert isinstance(events[-1], TurnComplete)


async def test_other_400_is_not_retried(monkeypatch):
    from tret.providers.base import ProviderError

    rec = {"model": "moonshotai/kimi-k3", "reasoning_details": DETAILS}
    with pytest.raises(ProviderError):
        await _turn(
            monkeypatch, OpenRouterProvider("k"), [], _history(rec),
            failures=[(400, '{"error":"max_tokens too large"}')],
        )


async def test_reasoning_400_without_replayed_reasoning_is_not_retried(monkeypatch):
    from tret.providers.base import ProviderError

    with pytest.raises(ProviderError):
        await _turn(
            monkeypatch, OpenRouterProvider("k"), [],
            failures=[(400, "unsupported reasoning effort")],
        )
