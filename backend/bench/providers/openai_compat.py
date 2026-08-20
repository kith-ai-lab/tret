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

from bench.net import CLASS_PROVIDER, open_client

from bench.providers.base import (
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
    mark_cache_breakpoint,
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


def _token_count(value) -> int:
    """A non-negative int from a wire field, or 0 for anything unexpected."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(int(value), 0)


def _cached_prompt_tokens(usage: dict) -> int:
    """Read `usage.prompt_tokens_details.cached_tokens`, defensively.

    OpenRouter and Kimi both report this shape, but only on some models and only
    once a cache is warm — anything unexpected reads as zero rather than raising.
    """
    details = usage.get("prompt_tokens_details")
    if not isinstance(details, dict):
        return 0
    return _token_count(details.get("cached_tokens"))


# Field names a cache *write* (cache creation) can arrive under on an
# OpenAI-shaped usage object. There is no standard one: OpenRouter forwards the
# upstream Anthropic name at the top level, and some gateways nest it in
# prompt_tokens_details next to cached_tokens. All of them are read, because
# writing breakpoints (see OpenRouterProvider._apply_cache_control) and then
# never reading the write count back means paying the 1.25x cache-write premium
# and recording it as ordinary input — the one accounting error a cost-and-carbon
# harness must not make.
_CACHE_WRITE_KEYS = ("cache_creation_input_tokens", "cache_write_tokens", "cache_creation_tokens")


def _cache_write_tokens(usage: dict) -> int:
    """Cache-creation tokens reported anywhere in an OpenAI-style usage object."""
    details = usage.get("prompt_tokens_details")
    sources = [usage, details if isinstance(details, dict) else {}]
    for source in sources:
        for key in _CACHE_WRITE_KEYS:
            if key in source:
                count = _token_count(source[key])
                if count:
                    return count
    return 0


def _usage_from_openai(usage: dict) -> Usage:
    """Translate an OpenAI-style usage object into canonical Usage.

    `prompt_tokens` counts the cache buckets too, so both of them are subtracted
    to keep Usage.input_tokens meaning "uncached input" as it does for Anthropic,
    and to keep the four buckets summing to what the provider billed rather than
    double-counting a cached token as input as well.
    """
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    cache_read = min(_cached_prompt_tokens(usage), prompt_tokens)
    cache_write = min(_cache_write_tokens(usage), prompt_tokens - cache_read)
    return Usage(
        input_tokens=prompt_tokens - cache_read - cache_write,
        output_tokens=int(usage.get("completion_tokens") or 0),
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
    )


def _index_of(entry: dict) -> int:
    """The `index` field of a choice or tool-call delta, defaulting to 0.

    Providers that stream a single completion sometimes omit it; a non-integer
    (or a bool, which `isinstance(..., int)` would otherwise accept) reads as 0
    rather than becoming a dict key that no later fragment can match.
    """
    idx = entry.get("index", 0)
    if isinstance(idx, bool) or not isinstance(idx, int):
        return 0
    return idx


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
    # Which egress class these calls belong to. Cloud upstreams are `provider`;
    # LocalProvider overrides this to `local`, which is the class an air-gapped
    # deployment keeps (bench/net/policy.py).
    egress_class = CLASS_PROVIDER

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

        # Aggregate tool-call deltas per (choice, tool index). The tool index is
        # only unique *within* a choice, so keying on it alone concatenated the
        # argument fragments of unrelated tool calls whenever a provider returned
        # more than one choice — a silently corrupted (or unparseable) action in
        # the agent loop. bench always asks for a single completion, so extra
        # choices are dropped rather than merged or executed — `primary_choice`
        # below is the first choice index the stream mentions.
        pending: dict[tuple[int, int], dict] = {}
        primary_choice: int | None = None
        usage = Usage()
        finish_reason = "end_turn"

        async with open_client(
            self.egress_class, timeout=httpx.Timeout(300.0, connect=15.0)
        ) as client:
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
                            choice_idx = _index_of(choice)
                            if primary_choice is None:
                                primary_choice = choice_idx
                            if choice_idx != primary_choice:
                                # A second candidate completion for the same
                                # request: its text would interleave with the
                                # answer and its tool calls would be executed as
                                # extra actions. Ignore it entirely.
                                continue
                            if choice.get("finish_reason"):
                                finish_reason = choice["finish_reason"]
                            delta = choice.get("delta") or {}
                            if delta.get("content"):
                                yield TextDelta(delta["content"])
                            for tc in delta.get("tool_calls") or []:
                                idx = _index_of(tc)
                                slot = pending.setdefault(
                                    (choice_idx, idx), {"id": None, "name": None, "args": ""}
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

        for key in sorted(pending):
            _choice_idx, idx = key
            slot = pending[key]
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
    ) -> JsonCompletion:
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
        async with open_client(self.egress_class, timeout=timeout) as client:
            try:
                resp = await client.post(
                    f"{self._base_url}/chat/completions", headers=self._headers, json=body
                )
            except httpx.HTTPError as e:
                raise ProviderError(self.name, str(e)) from e
        if resp.status_code >= 400:
            raise ProviderError(self.name, resp.text[:2000], resp.status_code)
        data = resp.json()
        usage = _usage_from_openai(data.get("usage") or {})
        try:
            calls = data["choices"][0]["message"].get("tool_calls") or []
            for call in calls:
                if call["function"]["name"] == tool_name:
                    return JsonCompletion(
                        payload=json.loads(call["function"]["arguments"]),
                        usage=usage,
                        model=model,
                    )
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
            if mark_cache_breakpoint(messages[0]):
                budget -= 1
            messages = messages[1:]
        for message in reversed(messages):
            if budget <= 0:
                return
            if message.get("role") == "user" and mark_cache_breakpoint(message):
                budget -= 1
