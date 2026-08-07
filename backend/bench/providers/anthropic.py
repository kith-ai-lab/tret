"""Anthropic provider using the native SDK.

Two kinds of prompt-cache breakpoint are set:

- the system prompt (the doctrine block is the stable prefix of every run of a
  pack version, so caching it pays for itself);
- the tail of the message history, so each tool-loop iteration reads the whole
  prior conversation from cache instead of re-paying for it.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator

import anthropic

from bench.providers.base import (
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
    mark_cache_breakpoint,
)

# The API rejects requests carrying more than four cache_control blocks. One is
# spent on the system prompt; the rest are available to the message history.
MAX_CACHE_BREAKPOINTS = 4
SYSTEM_CACHE_BREAKPOINTS = 1
MESSAGE_CACHE_BREAKPOINTS = MAX_CACHE_BREAKPOINTS - SYSTEM_CACHE_BREAKPOINTS


def _as_blocks(content) -> list[dict]:
    """A message body as a block list, promoting a plain string."""
    if isinstance(content, list):
        return content
    return [{"type": "text", "text": content or ""}]


def _to_anthropic_messages(messages: list[Msg]) -> list[dict]:
    """Translate canonical Msg list to Anthropic content-block format.

    Consecutive tool-result messages are folded into a single user turn, as the
    API requires tool_result blocks to open the message that follows tool_use.

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
    for m in messages:
        if m.role == "user":
            if out and out[-1]["role"] == "user":
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
    return out


def _apply_conversation_cache(messages: list[dict], budget: int = MESSAGE_CACHE_BREAKPOINTS) -> None:
    """Cache the conversation prefix at turn boundaries, in place.

    The tool loop re-sends the whole history every iteration, so a breakpoint on
    the final content block lets the next iteration read everything before it
    from cache. Earlier user-turn boundaries are marked too, budget permitting,
    so a read anchor survives iterations that append more content blocks than the
    cache lookback window.
    """
    marked = 0
    for i, message in enumerate(reversed(messages)):
        if marked >= budget:
            return
        if i == 0 or message.get("role") == "user":
            if mark_cache_breakpoint(message):
                marked += 1


def _to_anthropic_tools(tools: list[ToolSpec]) -> list[dict]:
    return [
        {"name": t.name, "description": t.description, "input_schema": t.parameters}
        for t in tools
    ]


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(self, api_key: str):
        self._client = anthropic.AsyncAnthropic(api_key=api_key)

    async def stream(
        self,
        *,
        model: str,
        system: str,
        messages: list[Msg],
        tools: list[ToolSpec],
        max_tokens: int,
        temperature: float,
    ) -> AsyncIterator[ProviderEvent]:
        system_blocks = [
            {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
        ]
        anthropic_messages = _to_anthropic_messages(messages)
        _apply_conversation_cache(anthropic_messages)
        kwargs: dict = dict(
            model=model,
            system=system_blocks,
            messages=anthropic_messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        if tools:
            kwargs["tools"] = _to_anthropic_tools(tools)

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
                usage = Usage(
                    input_tokens=final.usage.input_tokens,
                    output_tokens=final.usage.output_tokens,
                    cache_read_tokens=getattr(final.usage, "cache_read_input_tokens", 0) or 0,
                    cache_write_tokens=(
                        getattr(final.usage, "cache_creation_input_tokens", 0) or 0
                    ),
                )
                yield TurnComplete(usage=usage, stop_reason=final.stop_reason or "end_turn")
        except anthropic.APIError as e:
            raise ProviderError("anthropic", str(e), getattr(e, "status_code", None)) from e

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
    ) -> dict:
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
        except anthropic.APIError as e:
            raise ProviderError("anthropic", str(e), getattr(e, "status_code", None)) from e
        for block in msg.content:
            if block.type == "tool_use" and block.name == tool_name:
                return dict(block.input)
        raise ProviderError("anthropic", "No tool_use block in structured completion")
