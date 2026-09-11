"""Provider-neutral types and the Provider ABC.

The engine speaks only these types; each provider translates to/from its wire
format internally. Adding a provider means implementing *both* abstract methods,
`stream()` and `complete_json()` — the router and the QA graders call
`complete_json()` on whatever provider they are handed, so a provider without it
cannot be routed to at all. `mark_cache_breakpoint` below is shared wire-format
help for the providers whose upstream honours Anthropic-style `cache_control`.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class Msg:
    role: Literal["user", "assistant", "tool"]
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)  # assistant only
    tool_call_id: str | None = None  # tool only
    meta: dict = field(default_factory=dict)  # timing, iteration; never sent to providers

    def to_json(self) -> dict:
        return {
            "role": self.role,
            "content": self.content,
            "tool_calls": [
                {"id": t.id, "name": t.name, "arguments": t.arguments} for t in self.tool_calls
            ],
            "tool_call_id": self.tool_call_id,
            "meta": self.meta,
        }

    @classmethod
    def from_json(cls, d: dict) -> "Msg":
        return cls(
            role=d["role"],
            content=d.get("content"),
            tool_calls=[
                ToolCall(t["id"], t["name"], t["arguments"]) for t in d.get("tool_calls", [])
            ],
            tool_call_id=d.get("tool_call_id"),
            meta=d.get("meta", {}),
        )


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict  # JSON Schema
    handler: Any = None  # async callable(run_ctx, **arguments) -> str; None for spec-only use


@dataclass
class Usage:
    """Token accounting for one turn.

    `input_tokens` is the *uncached* prompt only — cache reads and cache writes
    are reported separately because they are priced differently. Providers whose
    wire format folds cached tokens into the prompt total (OpenAI-compatible
    APIs) subtract them so this invariant holds everywhere.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    # The provider's own reported actual USD cost for the turn. OpenRouter
    # reports this (it bills the upstream's real rate, which can differ from
    # tret's catalog price); every other provider leaves it None rather than 0,
    # because 0 would claim a free turn tret never metered. `cost_usd` elsewhere
    # in the engine — the catalog-priced figure used for routing and cost caps —
    # is unaffected by this: the two numbers answer different questions and are
    # never substituted for one another.
    reported_cost_usd: Decimal | None = None


@dataclass
class JsonCompletion:
    """A structured completion, what it cost, and which model it cost it on.

    `complete_json` used to return a bare dict, so every caller of it — the model
    router on essentially every run, context compaction on long ones, the local
    tool-capability probe on every discovery pass — spent real tokens tret never
    counted. Invisible rather than small: a router call happens before the first
    token of a run and appeared nowhere in that run's cost.

    Returning usage *alongside* the payload rather than as a second value is
    deliberate. A tuple invites `result, _ = await complete_json(...)`, and a
    discarded second element is how this went untracked in the first place. It
    also matches `TurnComplete`, which has always carried usage this way.

    `model` is here rather than re-derived by the caller because these calls do
    **not** run on the run's model. The router runs on `TRET_ROUTER_MODEL`; the
    compaction summarizer resolves its own cheap model within the harness ceiling.
    Their energy class, their provider's grid factor and its GHG Protocol basis
    are all properties of *that* model, and attributing their tokens to the run's
    model would not be an approximation — it would be a different number about a
    different thing.
    """

    payload: dict
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    # The upstream provider name that actually served this completion, when the
    # provider reports one (OpenRouter; see `openai_compat._served_by_from_openai`).
    # None for a provider that doesn't route across upstreams — Kimi, and every
    # other OpenAI-compatible server — and for AnthropicProvider it is the
    # constant "anthropic", so the field reads uniformly across providers.
    served_by: str | None = None


# ── prompt-cache breakpoints ──────────────────────────────────────────────────
def mark_cache_breakpoint(message: dict) -> bool:
    """Attach `cache_control` to a message's final content part, in place.

    Shared by the Anthropic provider and by OpenRouter (which forwards
    Anthropic-style `cache_control` upstream), because it is the same wire
    format in both places — the two providers used to carry byte-identical
    private copies, which is one copy too many for a budget-sensitive
    detail: the request limit of four breakpoints is enforced by counting
    the `True` returns.

    A plain string body is promoted to a single text part first. Returns False
    when there is nothing markable (empty body, or a part that already carries a
    breakpoint), so callers can keep an accurate budget.
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


# ── streaming events ──────────────────────────────────────────────────────────
@dataclass
class TextDelta:
    text: str


@dataclass
class ToolCallComplete:
    tool_call: ToolCall


@dataclass
class TurnComplete:
    usage: Usage
    stop_reason: str  # "end_turn" | "tool_use" | "max_tokens" | provider-specific
    # See `JsonCompletion.served_by` — the same field, carried on the streaming
    # path instead of the structured-completion one.
    served_by: str | None = None


ProviderEvent = TextDelta | ToolCallComplete | TurnComplete


class ProviderError(Exception):
    def __init__(self, provider: str, message: str, status: int | None = None):
        self.provider = provider
        self.status = status
        super().__init__(f"[{provider}] {message}")


class Provider(ABC):
    """One concrete instance per provider, constructed with its API key."""

    name: str

    @abstractmethod
    async def stream(
        self,
        *,
        model: str,  # the provider wire id (already resolved by the catalog)
        system: str,
        messages: list[Msg],
        tools: list[ToolSpec],
        max_tokens: int,
        temperature: float,
        # Reasoning-effort level ("low" | "medium" | "high"), or None to send
        # nothing. The caller (engine/harness.py) has already gated this on
        # the chosen model's `ModelInfo.supports_effort` before calling —
        # every implementation is free to forward whatever it is given as-is.
        effort: str | None = None,
        # Opaque id for provider-side cache affinity; ignored by providers that
        # lack it. engine/harness.py passes the run's own id so every call of
        # one run's tool loop lands on the same upstream (OpenRouter's
        # session-affinity routing) instead of hitting a cold cache each turn.
        session_id: str | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        """Yield TextDelta / ToolCallComplete events, ending with one TurnComplete."""
        ...

    @abstractmethod
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
        """Non-streaming structured completion via a forced tool call.

        Used by the model router, context compaction's summarizer, and the local
        tool-capability probe. Returns the tool arguments *and* the usage the
        call incurred — see `JsonCompletion` for why both.
        """
        ...
