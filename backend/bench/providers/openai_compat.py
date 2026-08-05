"""Shared OpenAI-compatible chat/completions provider (httpx + SSE).

Kimi (Moonshot) and OpenRouter both subclass this with a base_url and headers.
Prompt caching differs between them: both *report* cached prompt tokens, but only
OpenRouter accepts Anthropic-style `cache_control` breakpoints on message content
parts, so writing them is opt-in per subclass via `_apply_cache_control`.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx

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


def _to_openai_messages(system: str, messages: list[Msg]) -> list[dict]:
    out: list[dict] = [{"role": "system", "content": system}]
    for m in messages:
        if m.role == "user":
            out.append({"role": "user", "content": m.content or ""})
        elif m.role == "assistant":
            entry: dict = {"role": "assistant", "content": m.content or None}
            if m.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
                    }
                    for tc in m.tool_calls
                ]
            out.append(entry)
        elif m.role == "tool":
            out.append(
                {"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content or ""}
            )
    return out


def _cached_prompt_tokens(usage: dict) -> int:
    """Read `usage.prompt_tokens_details.cached_tokens`, defensively.

    OpenRouter and Kimi both report this shape, but only on some models and only
    once a cache is warm — anything unexpected reads as zero rather than raising.
    """
    details = usage.get("prompt_tokens_details")
    if not isinstance(details, dict):
        return 0
    cached = details.get("cached_tokens")
    if isinstance(cached, bool) or not isinstance(cached, (int, float)):
        return 0
    return max(int(cached), 0)


def _usage_from_openai(usage: dict) -> Usage:
    """Translate an OpenAI-style usage object into canonical Usage.

    `prompt_tokens` counts cached tokens too, so they are subtracted to keep
    Usage.input_tokens meaning "uncached input" as it does for Anthropic.
    """
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    cache_read = min(_cached_prompt_tokens(usage), prompt_tokens)
    return Usage(
        input_tokens=prompt_tokens - cache_read,
        output_tokens=int(usage.get("completion_tokens") or 0),
        cache_read_tokens=cache_read,
    )


def _mark_cache_breakpoint(message: dict) -> bool:
    """Attach `cache_control` to a message's final content part, in place.

    A plain string body is promoted to a single text part first. Returns False
    when there is nothing markable, so callers can keep an accurate budget.
    """
    content = message.get("content")
    if isinstance(content, str):
        if not content:
            return False
        content = [{"type": "text", "text": content}]
        message["content"] = content
    if not isinstance(content, list) or not content:
        return False
    part = content[-1]
    if not isinstance(part, dict) or "cache_control" in part:
        return False
    part["cache_control"] = {"type": "ephemeral"}
    return True


def _to_openai_tools(tools: list[ToolSpec]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
        }
        for t in tools
    ]


class OpenAICompatProvider(Provider):
    name = "openai_compat"

    def __init__(
        self,
        api_key: str,
        base_url: str,
        default_headers: dict | None = None,
        extra_body: dict | None = None,
    ):
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            **(default_headers or {}),
        }
        self._extra_body = extra_body or {}

    def _apply_cache_control(self, body: dict) -> None:
        """Hook: add prompt-cache breakpoints to an outgoing request body.

        No-op by default — most OpenAI-compatible APIs cache implicitly and
        reject (or silently mangle) structured content parts. Subclasses whose
        upstream honors Anthropic-style `cache_control` override this.
        """

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
        body: dict = {
            "model": model,
            "messages": _to_openai_messages(system, messages),
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
            **self._extra_body,
        }
        if tools:
            body["tools"] = _to_openai_tools(tools)
        self._apply_cache_control(body)

        # Aggregate tool-call deltas by index.
        pending: dict[int, dict] = {}
        usage = Usage()
        finish_reason = "end_turn"

        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=15.0)) as client:
            try:
                async with client.stream(
                    "POST", f"{self._base_url}/chat/completions", headers=self._headers, json=body
                ) as resp:
                    if resp.status_code >= 400:
                        detail = (await resp.aread()).decode(errors="replace")[:2000]
                        raise ProviderError(self.name, detail, resp.status_code)
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if payload == "[DONE]":
                            break
                        try:
                            chunk = json.loads(payload)
                        except json.JSONDecodeError:
                            continue
                        if chunk.get("usage"):
                            usage = _usage_from_openai(chunk["usage"])
                        for choice in chunk.get("choices", []):
                            if choice.get("finish_reason"):
                                finish_reason = choice["finish_reason"]
                            delta = choice.get("delta") or {}
                            if delta.get("content"):
                                yield TextDelta(delta["content"])
                            for tc in delta.get("tool_calls") or []:
                                idx = tc.get("index", 0)
                                slot = pending.setdefault(
                                    idx, {"id": None, "name": None, "args": ""}
                                )
                                if tc.get("id"):
                                    slot["id"] = tc["id"]
                                fn = tc.get("function") or {}
                                if fn.get("name"):
                                    slot["name"] = fn["name"]
                                if fn.get("arguments"):
                                    slot["args"] += fn["arguments"]
            except httpx.HTTPError as e:
                raise ProviderError(self.name, str(e)) from e

        for idx in sorted(pending):
            slot = pending[idx]
            if not slot["name"]:
                continue
            try:
                args = json.loads(slot["args"]) if slot["args"].strip() else {}
            except json.JSONDecodeError:
                args = {"_raw": slot["args"]}
            yield ToolCallComplete(
                ToolCall(slot["id"] or f"call_{idx}", slot["name"], args)
            )

        stop = "tool_use" if (pending and finish_reason in ("tool_calls", "tool_use")) else finish_reason
        yield TurnComplete(usage=usage, stop_reason=stop)

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
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "description": "Respond with the structured result.",
                        "parameters": schema,
                    },
                }
            ],
            "tool_choice": {"type": "function", "function": {"name": tool_name}},
            "max_tokens": max_tokens,
            **self._extra_body,
        }
        async with httpx.AsyncClient(timeout=timeout) as client:
            try:
                resp = await client.post(
                    f"{self._base_url}/chat/completions", headers=self._headers, json=body
                )
            except httpx.HTTPError as e:
                raise ProviderError(self.name, str(e)) from e
        if resp.status_code >= 400:
            raise ProviderError(self.name, resp.text[:2000], resp.status_code)
        data = resp.json()
        try:
            calls = data["choices"][0]["message"].get("tool_calls") or []
            for call in calls:
                if call["function"]["name"] == tool_name:
                    return json.loads(call["function"]["arguments"])
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            raise ProviderError(self.name, f"Malformed structured completion: {e}") from e
        raise ProviderError(self.name, "No forced tool call in structured completion")


class KimiProvider(OpenAICompatProvider):
    name = "kimi"

    def __init__(self, api_key: str):
        super().__init__(api_key, base_url="https://api.moonshot.ai/v1")


class OpenRouterProvider(OpenAICompatProvider):
    """OpenRouter, which forwards `cache_control` to models that support it.

    OpenRouter strips the field for models that don't, so marking breakpoints is
    safe across its catalog. Only `system` and `user` messages are marked:
    assistant and tool messages are left as plain strings because OpenRouter's
    per-model translation of structured tool content is not uniform.
    """

    name = "openrouter"
    max_cache_breakpoints = 4  # Anthropic's per-request limit, which OpenRouter inherits

    def __init__(self, api_key: str, referer: str = "", title: str = "bench"):
        headers = {}
        if referer:
            headers["HTTP-Referer"] = referer
        if title:
            headers["X-Title"] = title
        super().__init__(api_key, base_url="https://openrouter.ai/api/v1", default_headers=headers)

    def _apply_cache_control(self, body: dict) -> None:
        messages = body.get("messages") or []
        budget = self.max_cache_breakpoints
        if messages and messages[0].get("role") == "system":
            if _mark_cache_breakpoint(messages[0]):
                budget -= 1
            messages = messages[1:]
        for message in reversed(messages):
            if budget <= 0:
                return
            if message.get("role") == "user" and _mark_cache_breakpoint(message):
                budget -= 1
