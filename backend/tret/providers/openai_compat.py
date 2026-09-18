"""Shared OpenAI-compatible chat/completions provider (httpx + SSE).

Kimi (Moonshot) and OpenRouter both subclass this with a base_url and headers.
Prompt caching differs between them: both *report* cached prompt tokens, but only
OpenRouter accepts Anthropic-style `cache_control` breakpoints on message content
parts, so writing them is opt-in per subclass via `_apply_cache_control`.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from decimal import Decimal, InvalidOperation
from typing import Literal

import httpx

from tret.net import CLASS_PROVIDER, EgressDenied, open_client

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


class _StreamFailure(Exception):
    """One attempt's failure, carrying what the retry loop needs to decide
    whether a second attempt is worth making — see `OpenAICompatProvider.stream`.

    Internal to this module: never escapes `stream()`, which always converts
    it to a `ProviderError` (retried once first, when `retryable` says so and
    nothing has been yielded to the caller yet).
    """

    def __init__(self, message: str, status: int | None, *, retryable: bool):
        self.message = message
        self.status = status
        self.retryable = retryable
        super().__init__(message)


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


def _reported_cost_usd(usage: dict) -> Decimal | None:
    """OpenRouter's own billed USD cost for the turn, if the response carries one.

    `cost_details.upstream_inference_cost` is preferred over the top-level
    `cost` when both are present: the top-level figure is OpenRouter's own
    charge (which can include its markup or be absent for BYOK requests),
    while `upstream_inference_cost` is what the upstream provider actually
    billed. Other OpenAI-compatible servers send neither, so this returns None
    rather than 0 — 0 would claim a free turn nobody metered.
    """
    details = usage.get("cost_details")
    if isinstance(details, dict) and details.get("upstream_inference_cost") is not None:
        value = details["upstream_inference_cost"]
    elif usage.get("cost") is not None:
        value = usage["cost"]
    else:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _served_by_from_openai(data: dict) -> str | None:
    """The upstream provider that actually served this response, if named.

    OpenRouter does not put this at the top level of the response — there is
    no top-level `provider` string on either the non-streaming completion or a
    streaming chunk (verified against
    https://openrouter.ai/docs/api-reference/chat-completion: the
    ChatCompletionResponse/ChatStreamChunk schemas list only choices, created,
    id, model, object, openrouter_metadata, service_tier, system_fingerprint
    and usage). The served endpoint is named inside
    `openrouter_metadata.endpoints.available[]`, in the entry with
    `selected: true` — the schema's own example is
    `{"endpoints": {"available": [{"model": "openai/gpt-4o", "provider":
    "OpenAI", "selected": true}]}}`. Kimi and other OpenAI-compatible servers
    send neither key, so this reads as None for them.

    That `provider` field is a **display name** ("DeepInfra", "Google"), not
    the slug OpenRouter's own `provider.ignore` matches against ("deepinfra",
    "google-vertex") — the metadata schema carries no slug/tag field at all.
    This function only extracts the raw display name; `OpenRouterProvider.
    _resolve_served_by` (below) is what turns it into a slug before it is
    ever stored on a `TurnComplete`/`JsonCompletion`.
    """
    metadata = data.get("openrouter_metadata")
    if not isinstance(metadata, dict):
        return None
    endpoints = metadata.get("endpoints")
    if not isinstance(endpoints, dict):
        return None
    for entry in endpoints.get("available") or []:
        if isinstance(entry, dict) and entry.get("selected") and entry.get("provider"):
            return str(entry["provider"])
    return None


def _usage_from_openai(
    usage: dict,
    *,
    reasoning_accounting: Literal["counted_in_output", "additional", "unknown"] = "unknown",
) -> Usage:
    """Translate an OpenAI-style usage object into canonical Usage.

    `prompt_tokens` counts the cache buckets too, so both of them are subtracted
    to keep Usage.input_tokens meaning "uncached input" as it does for Anthropic,
    and to keep the four buckets summing to what the provider billed rather than
    double-counting a cached token as input as well.
    """
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    cache_read = min(_cached_prompt_tokens(usage), prompt_tokens)
    cache_write = min(_cache_write_tokens(usage), prompt_tokens - cache_read)
    reasoning_tokens: int | None = None
    details = usage.get("completion_tokens_details")
    if isinstance(details, dict):
        raw_reasoning = details.get("reasoning_tokens")
        if (
            isinstance(raw_reasoning, int)
            and not isinstance(raw_reasoning, bool)
            and raw_reasoning >= 0
        ):
            reasoning_tokens = raw_reasoning
    return Usage(
        input_tokens=prompt_tokens - cache_read - cache_write,
        output_tokens=int(usage.get("completion_tokens") or 0),
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        reasoning_tokens=reasoning_tokens,
        reasoning_accounting=(reasoning_accounting if reasoning_tokens is not None else None),
        reported_cost_usd=_reported_cost_usd(usage),
    )


def _inference_geo_from_openai(data: dict) -> str | None:
    """Return explicit response geography; never infer it from configuration.

    Checked under `usage` first (`usage.get("inference_geo")`, the plan's own
    cited shape) before the body's top level, so a live response that nests
    it under `usage` is not silently read as absent.
    """
    usage = data.get("usage")
    value = usage.get("inference_geo") if isinstance(usage, dict) else None
    if not (isinstance(value, str) and value.strip()):
        value = data.get("inference_geo")
    return normalize_call_slug(value) if isinstance(value, str) else None


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
    # deployment keeps (tret/net/policy.py).
    egress_class = CLASS_PROVIDER
    # Compatibility alone does not prove whether a vendor includes hidden
    # reasoning in completion_tokens. A provider with a pinned contract may
    # narrow this (OpenRouter below); otherwise explicit values remain unknown.
    reasoning_accounting: Literal["counted_in_output", "additional", "unknown"] = "unknown"

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

    def _apply_cache_control(self, body: dict, messages: list[Msg]) -> None:
        """Hook: add prompt-cache breakpoints to an outgoing request body.

        No-op by default — most OpenAI-compatible APIs cache implicitly and
        reject (or silently mangle) structured content parts. Subclasses whose
        upstream honors Anthropic-style `cache_control` override this.

        `messages` — the canonical `Msg` list `body["messages"]` was built
        from (`_to_openai_messages`, which is 1:1 with it after the leading
        system entry) — is here so an override can recognize the wire-only
        budget line (`harness._append_budget_line`, `meta={"budget_line":
        True}`) by identity rather than by re-parsing `body`'s already-rendered
        strings.
        """

    def _effort_body(self, effort: str | None) -> dict:
        """Hook: the request-body fragment for a reasoning-effort level.

        `{}` by default — most OpenAI-compatible servers (Kimi included) have
        no such control, so a harness that passes `effort` to one gets a
        request unchanged from before this parameter existed. OpenRouter
        overrides this with its unified `reasoning.effort`.
        """
        return {}

    def _session_body(self, session_id: str | None) -> dict:
        """Hook: the request-body fragment for provider-side cache affinity.

        `{}` by default — most OpenAI-compatible servers (Kimi included) have
        no such control. OpenRouter overrides this with its top-level
        `session_id`, which the router uses as a sticky routing key so a
        run's whole tool loop lands on the same upstream provider.
        """
        return {}

    async def _resolve_served_by(self, model: str, served_by: str | None) -> str | None:
        """Hook: normalize a raw `served_by` reading into the namespace the
        rest of tret expects it in (see `Provider.stream`'s `provider_ignore`
        docstring in `providers/base.py`: provider *slugs*, not display names).

        Identity by default — Kimi and every other single-upstream
        OpenAI-compatible server never populates `served_by` at all, so
        there is nothing to normalize. OpenRouter overrides this to map the
        display name `_served_by_from_openai` reads off the wire (e.g.
        "DeepInfra") to the slug (e.g. "deepinfra") its own `provider.ignore`
        actually matches against.
        """
        return served_by

    def _provider_body(
        self, tools_present: bool, provider_ignore: list[str] | None = None
    ) -> dict:
        """Hook: the `provider` object (OpenRouter's provider-selection block).

        `{}` by default — most OpenAI-compatible servers (Kimi included) have
        no such field, so `provider_ignore` is accepted here only to keep the
        signature uniform and is otherwise unused. OpenRouter overrides this
        to send whatever an operator configured via
        `TRET_OPENROUTER_PROVIDER_PREFS` (including an opt-in
        `require_parameters`), plus `provider_ignore` merged into `ignore`. An empty return means "omit the `provider` key
        entirely" — sending `{}` is not the same as sending nothing on some
        upstreams.
        """
        return {}

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
        session_id: str | None = None,
        provider_ignore: list[str] | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        body: dict = {
            "model": model,
            "messages": _to_openai_messages(system, messages),
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
            **self._extra_body,
            **self._effort_body(effort),
            **self._session_body(session_id),
        }
        if tools:
            body["tools"] = _to_openai_tools(tools)
        provider_body = self._provider_body(bool(tools), provider_ignore)
        if provider_body:
            body["provider"] = provider_body
        self._apply_cache_control(body, messages)

        # Retry a transient upstream failure once (2026-09-11 — see
        # `providers.base`'s retry-constants docstring for the incident this
        # is for): three Google AI Studio 503s relayed through OpenRouter and
        # one OpenRouter HTML error page each failed a run outright, when a
        # single retry would very likely have recovered them. Never retried
        # past the first token: `yielded_any` tracks whether anything has
        # already reached the caller, and a partial stream is not something a
        # second attempt can safely replace — the caller may already have
        # acted on what it received.
        for attempt in range(2):
            yielded_any = False
            try:
                async for event in self._stream_attempt(body, model=model):
                    yielded_any = True
                    yield event
                return
            except _StreamFailure as exc:
                if exc.retryable and not yielded_any and attempt == 0:
                    await asyncio.sleep(RETRY_DELAY_SECONDS)
                    continue
                raise ProviderError(self.name, exc.message, exc.status) from exc

    async def _stream_attempt(self, body: dict, *, model: str) -> AsyncIterator[ProviderEvent]:
        """One attempt at the streamed request — the whole of what `stream()`
        used to be, before it needed a retry loop around it. Raises
        `_StreamFailure` (never `ProviderError` directly) so `stream()` can
        decide whether a second attempt is worth making.
        """
        # Aggregate tool-call deltas per (choice, tool index). The tool index is
        # only unique *within* a choice, so keying on it alone concatenated the
        # argument fragments of unrelated tool calls whenever a provider returned
        # more than one choice — a silently corrupted (or unparseable) action in
        # the agent loop. tret always asks for a single completion, so extra
        # choices are dropped rather than merged or executed — `primary_choice`
        # below is the first choice index the stream mentions.
        pending: dict[tuple[int, int], dict] = {}
        primary_choice: int | None = None
        usage = Usage()
        finish_reason = "end_turn"
        # The last non-empty value seen across chunks: OpenRouter's routing
        # decision (and so which endpoint is `selected`) is settled once the
        # upstream is picked, but nothing guarantees which chunk first carries
        # `openrouter_metadata` — taking the last keeps this correct even if
        # an earlier chunk arrives before the decision is final.
        served_by: str | None = None
        inference_geo: str | None = None

        async with open_client(
            self.egress_class, timeout=httpx.Timeout(300.0, connect=15.0)
        ) as client:
            try:
                async with client.stream(
                    "POST", f"{self._base_url}/chat/completions", headers=self._headers, json=body
                ) as resp:
                    if resp.status_code >= 400:
                        raw = (await resp.aread()).decode(errors="replace")
                        html = looks_like_html(raw, resp.headers.get("content-type"))
                        message = summarize_html_error(resp.status_code, raw) if html else raw[:2000]
                        raise _StreamFailure(
                            message,
                            resp.status_code,
                            retryable=is_retryable_status(resp.status_code, html_body=html),
                        )
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
                            # Streaming usage objects are cumulative snapshots;
                            # replace with the newest one rather than summing.
                            usage = _usage_from_openai(
                                chunk["usage"],
                                reasoning_accounting=self.reasoning_accounting,
                            )
                        chunk_geo = _inference_geo_from_openai(chunk)
                        if chunk_geo:
                            inference_geo = chunk_geo
                        chunk_served_by = _served_by_from_openai(chunk)
                        if chunk_served_by:
                            served_by = chunk_served_by
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
                # No status code and no body to summarize — a connection-level
                # failure, never retried (see `providers.base.is_retryable_status`,
                # which this deliberately does not call).
                raise _StreamFailure(str(e), None, retryable=False) from e

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
        # Resolved after the stream is fully read, not per-chunk: the display
        # name can change chunk to chunk until routing settles (see the
        # `served_by` local above), and resolving each intermediate value
        # would spend the endpoints-lookup cache churn on names never kept.
        served_by = normalize_call_slug(await self._resolve_served_by(model, served_by))
        yield TurnComplete(
            usage=usage,
            stop_reason=stop,
            served_by=served_by,
            inference_geo=inference_geo,
        )

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
        # A forced tool call always carries `tools`, so this is unconditionally
        # the "tools present" case; the same operator prefs apply as on an
        # agent turn.
        provider_body = self._provider_body(True)
        if provider_body:
            body["provider"] = provider_body
        # Same retry-once policy as `stream()` (see `providers.base`'s
        # retry-constants docstring): a non-streaming call has no partial
        # output to protect, so the only question is whether the failure
        # itself is worth a second try.
        for attempt in range(2):
            async with open_client(self.egress_class, timeout=timeout) as client:
                try:
                    resp = await client.post(
                        f"{self._base_url}/chat/completions", headers=self._headers, json=body
                    )
                except httpx.HTTPError as e:
                    raise ProviderError(self.name, str(e)) from e
            if resp.status_code >= 400:
                raw = resp.text
                html = looks_like_html(raw, resp.headers.get("content-type"))
                message = summarize_html_error(resp.status_code, raw) if html else raw[:2000]
                if is_retryable_status(resp.status_code, html_body=html) and attempt == 0:
                    await asyncio.sleep(RETRY_DELAY_SECONDS)
                    continue
                raise ProviderError(self.name, message, resp.status_code)
            break
        data = resp.json()
        usage = _usage_from_openai(
            data.get("usage") or {}, reasoning_accounting=self.reasoning_accounting
        )
        served_by = normalize_call_slug(await self._resolve_served_by(model, _served_by_from_openai(data)))
        inference_geo = _inference_geo_from_openai(data)
        try:
            calls = data["choices"][0]["message"].get("tool_calls") or []
            for call in calls:
                if call["function"]["name"] == tool_name:
                    return JsonCompletion(
                        payload=json.loads(call["function"]["arguments"]),
                        usage=usage,
                        model=model,
                        served_by=served_by,
                        inference_geo=inference_geo,
                    )
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            raise ProviderError(self.name, f"Malformed structured completion: {e}") from e
        raise ProviderError(self.name, "No forced tool call in structured completion")


class KimiProvider(OpenAICompatProvider):
    name = "kimi"

    def __init__(self, api_key: str):
        super().__init__(api_key, base_url="https://api.moonshot.ai/v1")


def _choose_base_slug(tags: list[str]) -> str:
    """Deterministically resolve one display name's endpoint tag(s) to the
    single slug `OpenRouterProvider._provider_body` puts in `provider.ignore`.

    A display name can name more than one tag — OpenRouter's live
    `/models/{model}/endpoints` returns both "google-vertex" and
    "google-vertex/us-central1" under the display name "Google" — and the
    old last-write-wins dict comprehension picked whichever happened to come
    last in the response, silently changing which one `ignore` matched
    depending on wire order.

    A tag with no "/" (a *base* provider slug) is preferred over a
    variant-scoped one when both are present: OpenRouter matches `ignore`
    against a tag literally, so a variant tag like "deepinfra/turbo" excludes
    only that variant while the base slug "deepinfra" excludes every
    region/variant endpoint the provider runs (see the `provider_ignore`
    docstring in providers/base.py). `openrouter_metadata` — the only signal
    `_served_by_from_openai` ever reads — reports the display name and
    nothing else, never which variant actually served a call, so there is no
    way to pick a variant tag correctly here; the base slug is the only
    choice this data supports, and it is also the intended one (excluding a
    poor-quality provider account-wide, not one region of it). Falls back to
    the shortest tag, ties broken alphabetically for a stable result across
    runs, only when every candidate is variant-scoped.
    """
    bases = sorted(tag for tag in tags if "/" not in tag)
    if bases:
        return bases[0]
    return sorted(tags, key=lambda tag: (len(tag), tag))[0]


class OpenRouterProvider(OpenAICompatProvider):
    """OpenRouter, which forwards `cache_control` to models that support it.

    OpenRouter strips the field for models that don't, so marking breakpoints is
    safe across its catalog. Only `system` and `user` messages are marked:
    assistant and tool messages are left as plain strings because OpenRouter's
    per-model translation of structured tool content is not uniform.
    """

    name = "openrouter"
    reasoning_accounting = "counted_in_output"
    max_cache_breakpoints = 4  # Anthropic's per-request limit, which OpenRouter inherits

    def __init__(
        self,
        api_key: str,
        referer: str = "",
        title: str = "tret",
        provider_prefs: dict | None = None,
    ):
        headers = {}
        if referer:
            headers["HTTP-Referer"] = referer
        if title:
            headers["X-Title"] = title
        # Opt-in to `openrouter_metadata` on the response — verified against
        # https://openrouter.ai/docs/api-reference/chat-completion: "Opt-in to
        # surface routing metadata on the response under `openrouter_metadata`.
        # Defaults to disabled." Without this header every response's
        # `openrouter_metadata` is absent, `_served_by_from_openai` always
        # reads None, and the whole served_by/priors/provider_ignore chain
        # this module builds never sees a value to act on.
        headers["X-OpenRouter-Metadata"] = "enabled"
        super().__init__(api_key, base_url="https://openrouter.ai/api/v1", default_headers=headers)
        # Operator settings for the `provider` request object — order, ignore,
        # quantizations, data_collection, zdr, sort, require_parameters — sent
        # as given by `_provider_body`, which adds nothing of its own except
        # the evidence-driven `ignore` union. See TRET_OPENROUTER_PROVIDER_PREFS
        # (config.py) for where this is parsed.
        self._provider_prefs = provider_prefs or {}
        # Per-model cache for `_resolve_served_by`: wire model id -> (expiry
        # monotonic timestamp, {display name lowercased: provider slug}).
        # Keyed by model because two models can share an upstream provider
        # under different endpoint tags (e.g. quantized variants), and the
        # `/models/{model}/endpoints` lookup is itself per-model.
        self._endpoint_slug_cache: dict[str, tuple[float, dict[str, str]]] = {}

    def _effort_body(self, effort: str | None) -> dict:
        """OpenRouter's unified `reasoning.effort`, forwarded to whichever
        upstream the request lands on. See
        https://openrouter.ai/docs/guides/best-practices/reasoning-tokens.
        """
        return {"reasoning": {"effort": effort}} if effort else {}

    def _session_body(self, session_id: str | None) -> dict:
        """OpenRouter's top-level `session_id`: a sticky routing key so every
        request in the session (here, one run's whole tool loop) lands on the
        same upstream provider, maximizing prompt-cache hits. Verified against
        https://openrouter.ai/docs/api-reference/chat-completion — the
        CreateChatCompletionRequest schema documents `session_id` as a
        top-level string field (max 256 characters), not something nested
        under `provider` or `metadata`.
        """
        return {"session_id": session_id} if session_id else {}

    # 24h: long enough that a busy deployment resolves each model's endpoint
    # slugs once a day at most, short enough that OpenRouter adding or
    # renaming an endpoint (new region, new quantization) is picked up the
    # same day rather than needing a restart.
    _ENDPOINT_SLUG_CACHE_TTL_S = 24 * 60 * 60
    # 10 minutes: how long a *failed* (or empty) lookup is remembered before
    # the next served_by resolution tries it again. Short relative to the
    # success TTL above — an outage or a bad model id should not need a
    # restart to recover from once OpenRouter is reachable again — but long
    # enough that a persistently failing lookup isn't retried on every single
    # turn of every run using this model, each attempt costing up to the
    # lookup's own ~15s timeout.
    _ENDPOINT_SLUG_CACHE_FAILURE_TTL_S = 10 * 60

    async def _resolve_served_by(self, model: str, served_by: str | None) -> str | None:
        """Map `_served_by_from_openai`'s display name to the provider slug
        `provider.ignore` matches against (see that function's docstring and
        `_provider_body`'s below) — `openrouter_metadata` never carries the
        slug itself, only `provider: "DeepInfra"`.

        `served_by` is not read anywhere in production yet (this is the first
        pass wiring it up), so there is no stored display-name value to
        migrate — everything recorded from here on is a slug, full stop.

        Best-effort and never raises: a lookup failure, an unrecognized
        model, or a display name with no matching endpoint (OpenRouter
        renamed or retired one between the call and this lookup) all resolve
        to None rather than guessing — a dropped `served_by` costs this run
        one row of routing evidence; a wrong one would poison priors for
        every run after it.
        """
        if not served_by:
            return None
        slug_map = await self._endpoint_slug_map(model)
        return slug_map.get(served_by.lower())

    async def _endpoint_slug_map(self, model: str) -> dict[str, str]:
        """`{display name lowercased: provider slug}` for `model`'s current
        endpoints, from `GET /models/{model}/endpoints` — verified live: each
        entry carries `provider_name` ("OpenAI", "Azure") and `tag` ("openai",
        "azure"), which is exactly the display-name-to-slug mapping
        `openrouter_metadata` itself doesn't provide. Cached per model for
        `_ENDPOINT_SLUG_CACHE_TTL_S` on a real result, or the shorter
        `_ENDPOINT_SLUG_CACHE_FAILURE_TTL_S` when the lookup failed or came
        back with no endpoints — negative-cached the same as a positive one,
        so a lookup that keeps failing is retried on a timer rather than on
        every turn of every run.

        Failures fall back to whatever is cached (possibly nothing) rather
        than raising: this runs after a turn has already completed, so an
        outage here must not turn a successful turn into a failed one, and a
        stale mapping is still more useful than none.
        """
        now = time.monotonic()
        cached = self._endpoint_slug_cache.get(model)
        if cached is not None and cached[0] > now:
            return cached[1]
        try:
            async with open_client(
                self.egress_class, timeout=httpx.Timeout(10.0, connect=5.0)
            ) as client:
                resp = await client.get(
                    f"{self._base_url}/models/{model}/endpoints", headers=self._headers
                )
                resp.raise_for_status()
                data = resp.json()
            # Group every tag seen under each display name first — a display
            # name can legitimately name more than one endpoint tag (e.g.
            # "Google" -> "google-vertex" and "google-vertex/us-central1") —
            # then `_choose_base_slug` resolves that to one slug deterministically.
            by_name: dict[str, list[str]] = {}
            for ep in (data.get("data") or {}).get("endpoints") or []:
                if (
                    isinstance(ep, dict)
                    and isinstance(ep.get("provider_name"), str)
                    and isinstance(ep.get("tag"), str)
                ):
                    by_name.setdefault(ep["provider_name"].lower(), []).append(ep["tag"])
            mapping = {name: _choose_base_slug(tags) for name, tags in by_name.items()}
        except (httpx.HTTPError, EgressDenied, ValueError, TypeError, KeyError, AttributeError):
            ttl = self._ENDPOINT_SLUG_CACHE_FAILURE_TTL_S
            fallback = cached[1] if cached is not None else {}
            self._endpoint_slug_cache[model] = (now + ttl, fallback)
            return fallback
        ttl = self._ENDPOINT_SLUG_CACHE_TTL_S if mapping else self._ENDPOINT_SLUG_CACHE_FAILURE_TTL_S
        self._endpoint_slug_cache[model] = (now + ttl, mapping)
        return mapping

    def _provider_body(
        self, tools_present: bool, provider_ignore: list[str] | None = None
    ) -> dict:
        """The `provider` object: https://openrouter.ai/docs/guides/routing/provider-selection.

        Starts empty: `provider_prefs` (TRET_OPENROUTER_PROVIDER_PREFS) is
        shallow-merged in, so an operator's `order`/`ignore`/`quantizations`/
        `data_collection`/`zdr`/`sort`/`require_parameters` are sent verbatim.

        `require_parameters` is opt-in, never a default. Sending it whenever
        tools were present (the behaviour until 2026-09-11) made OpenRouter
        drop every endpoint that does not advertise the full parameter set,
        and for the OpenAI models that was every endpoint: each tool-calling
        run on gpt-5.6-luna, gpt-5.6-terra and gpt-6-astra died at iteration
        0 with "No endpoints found that can handle the requested parameters"
        while the router's own tool-less call to the same model succeeded.
        The engine already validates tool calls and JSON output in-loop, so a
        provider that silently drops tools is caught there; an operator who
        wants OpenRouter to pre-filter anyway sets
        `{"require_parameters": true}` in the prefs.

        `provider_ignore` — the calling `RoutingDecision`'s own poor-endpoint
        evidence (`router_llm.router.RoutingDecision.provider_ignore`) — is
        then unioned into `ignore` on top of that merge, deduplicated and
        sorted for a stable wire body, so an operator's static denylist and
        this call's evidence-driven one both apply rather than one silently
        replacing the other. The `ignore` key is omitted entirely when the
        union is empty, matching every other key here: sending `[]` is not
        the same as sending nothing on some upstreams. Note this never
        widens `allow_fallbacks` (OpenRouter's own default is already true),
        so a chosen model whose every endpoint ends up ignored degrades to
        OpenRouter's "no eligible provider" error rather than resurrecting an
        endpoint this call meant to avoid — the existing provider-error path
        in engine/harness.py handles that the same as any other upstream
        failure.
        """
        body: dict = dict(self._provider_prefs)
        ignore = sorted(set(self._provider_prefs.get("ignore") or []) | set(provider_ignore or []))
        if ignore:
            body["ignore"] = ignore
        else:
            body.pop("ignore", None)
        return body

    def _apply_cache_control(self, body: dict, messages: list[Msg]) -> None:
        """Mark `system` and `user` messages, skipping the wire-only budget
        line (see the base hook's docstring) rather than spending a breakpoint
        slot on a message that is never sent the same way twice: on the
        OpenAI-compatible wire shape the line is always its own trailing
        `user` message (`_to_openai_messages` never merges consecutive `Msg`s
        the way the Anthropic translator does), so skipping it is just "don't
        mark this one message" — no block-level surgery needed the way
        `anthropic._mark_last_stable_block` requires.
        """
        body_messages = body.get("messages") or []
        budget = self.max_cache_breakpoints
        if body_messages and body_messages[0].get("role") == "system":
            if mark_cache_breakpoint(body_messages[0]):
                budget -= 1
            body_messages = body_messages[1:]
        skip_tail = bool(messages and messages[-1].meta.get("budget_line"))
        for i, message in enumerate(reversed(body_messages)):
            if skip_tail and i == 0:
                continue
            if budget <= 0:
                return
            if message.get("role") == "user" and mark_cache_breakpoint(message):
                budget -= 1
