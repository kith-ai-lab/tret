"""Anthropic provider using the native SDK.

Sets a prompt-cache breakpoint on the system prompt (the doctrine block is the
stable prefix of every run of a pack version, so caching it pays for itself).
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
)


def _to_anthropic_messages(messages: list[Msg]) -> list[dict]:
    """Translate canonical Msg list to Anthropic content-block format.

    Consecutive tool-result messages are folded into a single user turn, as the
    API requires tool_result blocks to open the message that follows tool_use.
    """
    out: list[dict] = []
    for m in messages:
        if m.role == "user":
            out.append({"role": "user", "content": m.content or ""})
        elif m.role == "assistant":
            blocks: list[dict] = []
            if m.content:
                blocks.append({"type": "text", "text": m.content})
            for tc in m.tool_calls:
                blocks.append(
                    {"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.arguments}
                )
            out.append({"role": "assistant", "content": blocks or [{"type": "text", "text": ""}]})
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
        kwargs: dict = dict(
            model=model,
            system=system_blocks,
            messages=_to_anthropic_messages(messages),
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
