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
    ) -> dict:
        """Non-streaming structured completion via a forced tool call.

        Used by the model router and QA graders. Returns the tool arguments.
        """
        ...
