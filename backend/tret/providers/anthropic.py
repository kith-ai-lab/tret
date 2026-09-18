"""Anthropic provider using the native SDK.

Two kinds of prompt-cache breakpoint are set:

- the system prompt (the doctrine block is the stable prefix of every run of a
  pack version, so caching it pays for itself);
- the tail of the message history, so each tool-loop iteration reads the whole
  prior conversation from cache instead of re-paying for it.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator

import anthropic

from tret.net import CLASS_PROVIDER, build_client

from tret.providers.base import (
    RETRY_DELAY_SECONDS,
    JsonCompletion,
    Msg,
    Provider,
    ProviderError,
    ProviderEvent,
    TextDelta,
    ToolCall,
    ToolCallComplete,
    ToolSpec,
    TurnComplete,
    Usage,
    is_retryable_status,
    looks_like_html,
    mark_cache_breakpoint,
    normalize_call_slug,
    summarize_html_error,
)

# The API rejects requests carrying more than four cache_control blocks. One is
# spent on the system prompt; the rest are available to the message history.
MAX_CACHE_BREAKPOINTS = 4
SYSTEM_CACHE_BREAKPOINTS = 1
MESSAGE_CACHE_BREAKPOINTS = MAX_CACHE_BREAKPOINTS - SYSTEM_CACHE_BREAKPOINTS

log = logging.getLogger("tret.providers.anthropic")

# `count_tokens` sits on the hot per-iteration path (engine/harness.py calls it
# only once budget pressure is already showing, but that can be every turn from
# then on), so it gets a much shorter timeout than the 600s the streaming client
# is built with — a slow or hung count is worth abandoning quickly in favour of
# the chars/4 estimate the caller already has, not worth stalling a turn over.
COUNT_TOKENS_TIMEOUT = 5.0

# There is only ever one upstream behind this provider — unlike OpenRouter,
# which can route the same request to any of several — so `served_by` is a
# constant rather than something read off the response. Recorded anyway so the
# field is uniform across providers instead of "populated for OpenRouter,
# absent everywhere else".
SERVED_BY = normalize_call_slug("anthropic")


class _StreamFailure(Exception):
    """One attempt's failure, carrying what the retry loop needs to decide
    whether a second attempt is worth making — see `AnthropicProvider.stream`
    and the shared retry policy in `providers.base`. Internal to this module;
    always converted to a `ProviderError` before it can escape.
    """

    def __init__(self, message: str, status: int | None, *, retryable: bool):
        self.message = message
        self.status = status
        self.retryable = retryable
        super().__init__(message)


def _failure_from_api_error(e: anthropic.APIError) -> _StreamFailure:
    """One attempt's `_StreamFailure`, from whatever the SDK raised.

    `APIStatusError` (a real HTTP response came back, just an error one) is
    the only case with a status and a body to summarize; `retryable` is
    decided the same way as every other provider (`providers.base.
    is_retryable_status`) — 529 is Anthropic's own "overloaded" code, so this
    is the provider most likely to actually hit that branch. Anything else
    (`APIConnectionError` and friends) has no status and is never retried —
    same scope as `openai_compat.py`'s handling of a bare `httpx.HTTPError`.
    """
    if isinstance(e, anthropic.APIStatusError):
        response = e.response
        try:
            raw = response.text
        except Exception:  # noqa: BLE001 - a body that cannot be read is not an HTML one
            raw = ""
        html = looks_like_html(raw, response.headers.get("content-type"))
        message = summarize_html_error(e.status_code, raw) if html else str(e)
        return _StreamFailure(
            message, e.status_code, retryable=is_retryable_status(e.status_code, html_body=html)
        )
    return _StreamFailure(str(e), getattr(e, "status_code", None), retryable=False)


def _as_blocks(content) -> list[dict]:
    """A message body as a block list, promoting a plain string."""
    if isinstance(content, list):
        return content
    return [{"type": "text", "text": content or ""}]


def _to_anthropic_messages(messages: list[Msg]) -> tuple[list[dict], dict | None]:
    """Translate canonical Msg list to Anthropic content-block format.

    Returns `(messages, budget_line_block)`: `budget_line_block` is the exact
    content-block dict the wire-only budget line (`harness._append_budget_line`)
    ended up as, when `messages`' tail carries one (`meta={"budget_line": True}`,
    see `harness.py`), else None. It is a reference into `messages` itself, not
    a copy — `_apply_conversation_cache` uses `is` on it to recognize which
    block a cache breakpoint must never land on, since that block is re-sent
    with different content (this iteration's numbers) every turn and would
    defeat the very breakpoint it carried. A marker string would work too, but
    the meta already says exactly which Msg this is; no reason to duplicate
    that as a magic prefix another consumer could collide with.

    Consecutive tool-result messages are folded into a single user turn, as the
    API requires tool_result blocks to open the message that follows tool_use.
    The budget line, when present, is always this kind of trailing message —
    a lone `role="user"` `Msg` appended after everything else — so it folds
    into whatever turn already ends the conversation the same way a real
    trailing user message would.

    An assistant turn with neither text nor tool calls is *dropped* rather than
    sent as an empty text block: the API rejects `{"type": "text", "text": ""}`
    with a 400 ("text content blocks must be non-empty"), so a model that
    returned nothing — a max_tokens stop before any output, a persisted chat turn
    with an empty body — used to poison every subsequent request that re-sent the
    history. Dropping one can leave two adjacent user turns, so user turns are
    merged when that happens; nothing references an empty assistant turn (it has
    no tool_use ids), so no id can dangle.
    """
    out: list[dict] = []
    budget_line_block: dict | None = None
    for m in messages:
        if m.role == "user":
            if m.meta.get("budget_line"):
                # Always built as an explicit block (never left as a bare
                # string, unlike the plain-message branch below) so there is a
                # single object identity `_apply_conversation_cache` can test
                # for, whether this ends up merged onto an existing turn or
                # opening a new one.
                block = {"type": "text", "text": m.content or ""}
                if out and out[-1]["role"] == "user":
                    out[-1]["content"] = _as_blocks(out[-1]["content"]) + [block]
                else:
                    out.append({"role": "user", "content": [block]})
                budget_line_block = block
            elif out and out[-1]["role"] == "user":
                out[-1]["content"] = _as_blocks(out[-1]["content"]) + _as_blocks(m.content)
            else:
                out.append({"role": "user", "content": m.content or ""})
        elif m.role == "assistant":
            blocks: list[dict] = []
            if m.content:
                blocks.append({"type": "text", "text": m.content})
            for tc in m.tool_calls:
                blocks.append(
                    {"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.arguments}
                )
            if not blocks:
                continue
            out.append({"role": "assistant", "content": blocks})
        elif m.role == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": m.tool_call_id,
                "content": m.content or "",
            }
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
    return out, budget_line_block


def _mark_last_stable_block(message: dict, budget_line_block: dict | None) -> bool:
    """`mark_cache_breakpoint(message)`, except when `message`'s own last block
    is `budget_line_block` — in which case the breakpoint is set one block
    earlier instead, and never on the line itself.

    The budget line changes every iteration (this turn's numbers), so a
    breakpoint written on it — or, worse, a breakpoint whose presence shifts
    depending on where the line happened to land — is never a prefix match at
    the next iteration. The rest of that same turn (the tool_result blocks it
    was merged after, or a real trailing user message) is exactly as stable
    turn to turn as it always was; this only ever changes which block within
    the message gets marked, never which message.
    """
    content = message.get("content")
    if (
        budget_line_block is not None
        and isinstance(content, list)
        and content
        and content[-1] is budget_line_block
    ):
        if len(content) < 2:
            # This turn *is* the budget line and nothing else — there is no
            # earlier block in it to mark instead.
            return False
        # `content[:-1]` is a new list, but its elements are the same dict
        # objects as `content`'s — marking through this view mutates the real
        # block in place, exactly as `mark_cache_breakpoint(message)` would.
        return mark_cache_breakpoint({"content": content[:-1]})
    return mark_cache_breakpoint(message)


def _apply_conversation_cache(
    messages: list[dict],
    budget: int = MESSAGE_CACHE_BREAKPOINTS,
    *,
    budget_line_block: dict | None = None,
) -> None:
    """Cache the conversation prefix at turn boundaries, in place.

    The tool loop re-sends the whole history every iteration, so a breakpoint on
    the final content block lets the next iteration read everything before it
    from cache. Earlier user-turn boundaries are marked too, budget permitting,
    so a read anchor survives iterations that append more content blocks than the
    cache lookback window.

    `budget_line_block` (see `_to_anthropic_messages`) is the wire-only budget
    line's own content block, when this call's tail carries one — every
    breakpoint here is placed by `_mark_last_stable_block`, which skips exactly
    that block rather than the message it lives in.
    """
    marked = 0
    for i, message in enumerate(reversed(messages)):
        if marked >= budget:
            return
        if i == 0 or message.get("role") == "user":
            if _mark_last_stable_block(message, budget_line_block):
                marked += 1


def _to_anthropic_tools(tools: list[ToolSpec]) -> list[dict]:
    return [
        {"name": t.name, "description": t.description, "input_schema": t.parameters}
        for t in tools
    ]


def _usage_of(raw) -> Usage:
    """Anthropic's usage object as canonical `Usage`.

    Factored out because `stream()` and `complete_json()` both need it and a
    second hand-rolled copy is how the cache buckets drift apart. Tolerates a
    missing usage block (returns zeros) rather than raising: a completion whose
    token counts did not arrive is still a valid completion, and refusing it
    would fail a run over bookkeeping.
    """
    if raw is None:
        return Usage()
    details = getattr(raw, "output_tokens_details", None)
    if isinstance(details, dict):
        thinking_raw = details.get("thinking_tokens")
    else:
        thinking_raw = getattr(details, "thinking_tokens", None)
    thinking_tokens = (
        thinking_raw
        if isinstance(thinking_raw, int)
        and not isinstance(thinking_raw, bool)
        and thinking_raw >= 0
        else None
    )
    return Usage(
        input_tokens=getattr(raw, "input_tokens", 0) or 0,
        output_tokens=getattr(raw, "output_tokens", 0) or 0,
        cache_read_tokens=getattr(raw, "cache_read_input_tokens", 0) or 0,
        cache_write_tokens=getattr(raw, "cache_creation_input_tokens", 0) or 0,
        reasoning_tokens=thinking_tokens,
        reasoning_accounting=("counted_in_output" if thinking_tokens is not None else None),
    )


def _inference_geo_of(raw) -> str | None:
    """Provider-reported execution geography, `usage` first.

    The plan cited `usage.inference_geo`; only a top-level attribute was ever
    exercised in tests, so a live response nesting it under `usage` (as the
    Anthropic API does for other per-call fields) would silently read as
    `None`. Checked here first, with the message/body top level kept as a
    fallback for a shape that puts it there instead.
    """
    usage = getattr(raw, "usage", None)
    value = getattr(usage, "inference_geo", None) if usage is not None else None
    if not (isinstance(value, str) and value.strip()):
        value = getattr(raw, "inference_geo", None)
    return normalize_call_slug(value) if isinstance(value, str) else None


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(self, api_key: str):
        # The SDK brings its own httpx client; it is handed tret's instead so
        # that these calls pass the same policy check as every other outbound
        # request (tret/net/client.py). The SDK owns and closes what it is
        # given, which is why this is `build_client` and not the context manager.
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key, http_client=build_client(CLASS_PROVIDER, timeout=600.0)
        )

    async def stream(
        self,
        *,
        model: str,
        system: str,
        messages: list[Msg],
        tools: list[ToolSpec],
        max_tokens: int,
        temperature: float,
        effort: str | None = None,
        session_id: str | None = None,  # no equivalent on the Anthropic API; ignored
        provider_ignore: list[str] | None = None,  # no equivalent either; ignored
    ) -> AsyncIterator[ProviderEvent]:
        system_blocks = [
            {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
        ]
        anthropic_messages, budget_line_block = _to_anthropic_messages(messages)
        _apply_conversation_cache(anthropic_messages, budget_line_block=budget_line_block)
        kwargs: dict = dict(
            model=model,
            system=system_blocks,
            messages=anthropic_messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        if tools:
            kwargs["tools"] = _to_anthropic_tools(tools)
        if effort:
            # `output_config` is a first-class keyword on the installed SDK's
            # `messages.stream()` (anthropic>=0.117, verified against this
            # repo's pinned version) — not an unsupported field that has to
            # go through `extra_body`. The harness has already decided this
            # model accepts effort (`ModelInfo.supports_effort`) before
            # calling; this provider only forwards what it is given.
            kwargs["output_config"] = {"effort": effort}

        # Retry a transient upstream failure once (2026-09-11 — see the
        # shared retry policy in `providers.base`). 529 ("overloaded") is
        # Anthropic's own status for exactly this. Never retried past the
        # first token: `yielded_any` tracks whether anything has already
        # reached the caller, and a partial stream is not something a second
        # attempt can safely replace.
        for attempt in range(2):
            yielded_any = False
            try:
                async for event in self._stream_attempt(kwargs):
                    yielded_any = True
                    yield event
                return
            except _StreamFailure as exc:
                if exc.retryable and not yielded_any and attempt == 0:
                    await asyncio.sleep(RETRY_DELAY_SECONDS)
                    continue
                raise ProviderError("anthropic", exc.message, exc.status) from exc

    async def _stream_attempt(self, kwargs: dict) -> AsyncIterator[ProviderEvent]:
        """One attempt at the streamed request — the whole of what `stream()`
        used to be, before it needed a retry loop around it. Raises
        `_StreamFailure` (never `ProviderError` directly) so `stream()` can
        decide whether a second attempt is worth making.
        """
        try:
            async with self._client.messages.stream(**kwargs) as stream:
                # Track in-flight tool_use blocks by index to assemble arguments.
                open_tools: dict[int, dict] = {}
                async for event in stream:
                    et = event.type
                    if et == "content_block_delta":
                        delta = event.delta
                        if delta.type == "text_delta":
                            yield TextDelta(delta.text)
                        elif delta.type == "input_json_delta":
                            slot = open_tools.get(event.index)
                            if slot is not None:
                                slot["json"] += delta.partial_json
                    elif et == "content_block_start":
                        block = event.content_block
                        if block.type == "tool_use":
                            open_tools[event.index] = {
                                "id": block.id,
                                "name": block.name,
                                "json": "",
                            }
                    elif et == "content_block_stop":
                        slot = open_tools.pop(event.index, None)
                        if slot is not None:
                            try:
                                args = json.loads(slot["json"]) if slot["json"].strip() else {}
                            except json.JSONDecodeError:
                                args = {"_raw": slot["json"]}
                            yield ToolCallComplete(ToolCall(slot["id"], slot["name"], args))
                final = await stream.get_final_message()
                yield TurnComplete(
                    usage=_usage_of(final.usage),
                    stop_reason=final.stop_reason or "end_turn",
                    served_by=SERVED_BY,
                    inference_geo=_inference_geo_of(final),
                )
        except anthropic.APIError as e:
            raise _failure_from_api_error(e) from e

    async def complete_json(
        self,
        *,
        model: str,
        system: str,
        prompt: str,
        schema: dict,
        tool_name: str = "respond",
        max_tokens: int = 1024,
        timeout: float = 30.0,
    ) -> JsonCompletion:
        # Same retry-once policy as `stream()` — see `providers.base`'s retry
        # constants and this class's `_failure_from_api_error`. A
        # non-streaming call has no partial output to protect, so the only
        # question is whether the failure itself is worth a second try.
        for attempt in range(2):
            try:
                msg = await self._client.messages.create(
                    model=model,
                    system=system,
                    messages=[{"role": "user", "content": prompt}],
                    tools=[{"name": tool_name, "description": "Respond with the structured result.",
                            "input_schema": schema}],
                    tool_choice={"type": "tool", "name": tool_name},
                    max_tokens=max_tokens,
                    timeout=timeout,
                )
                break
            except anthropic.APIError as e:
                failure = _failure_from_api_error(e)
                if failure.retryable and attempt == 0:
                    await asyncio.sleep(RETRY_DELAY_SECONDS)
                    continue
                raise ProviderError("anthropic", failure.message, failure.status) from e
        for block in msg.content:
            if block.type == "tool_use" and block.name == tool_name:
                return JsonCompletion(
                    payload=dict(block.input),
                    usage=_usage_of(getattr(msg, "usage", None)),
                    model=model,
                    served_by=SERVED_BY,
                    inference_geo=_inference_geo_of(msg),
                )
        raise ProviderError("anthropic", "No tool_use block in structured completion")

    async def count_tokens(
        self,
        *,
        model: str,
        system: str,
        messages: list[Msg],
        tools: list[ToolSpec],
    ) -> int | None:
        """The SDK's `messages.count_tokens`, given the same converted shapes
        `stream()` sends — including the trailing budget-line message, when
        `messages`' tail carries one, and any `cache_control` blocks that
        would ride along on the real call. The endpoint tolerates
        `cache_control`; stripping it before counting would mean a second,
        divergent conversion path to keep in sync with `_to_anthropic_messages`
        for no accuracy gain, since it changes nothing the tokenizer counts.

        Never raises: an exact count is a refinement on top of the chars/4
        estimate the caller already has, not something a run may fail over.
        Any exception — a timeout (`COUNT_TOKENS_TIMEOUT`, short because this
        sits on the per-iteration hot path once a run is near its window), a
        rate limit (the endpoint is metered separately from completions), a
        malformed conversion — is logged and answered with `None`, which
        `engine/harness.py` reads as "fall back to chars/4, and stop asking
        this provider for the rest of the run".
        """
        anthropic_messages, _ = _to_anthropic_messages(messages)
        kwargs: dict = dict(model=model, system=system, messages=anthropic_messages)
        if tools:
            kwargs["tools"] = _to_anthropic_tools(tools)
        try:
            result = await self._client.messages.count_tokens(
                timeout=COUNT_TOKENS_TIMEOUT, **kwargs
            )
        except Exception:  # noqa: BLE001 - an exact count is optional, never fatal
            log.warning("anthropic count_tokens failed; falling back to chars/4", exc_info=True)
            return None
        return result.input_tokens
