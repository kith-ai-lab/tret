"""ReplayProvider — a deterministic, offline Provider for golden runs.

A golden run exercises the *real* engine: real pack loading, real tools against
real seeded data, real validation. Only the model is scripted. The script is a
list of `ScriptedTurn`s; each `stream()` call plays the next one, so the
provider walks the tool loop exactly as a live model does — emit tool calls,
receive the tool results on the next call, then emit the terminal structured
output.

Scripts stay honest because a turn's arguments may be *derived* from the tool
results the engine fed back (`arguments=` may be a callable, see `rows_of` and
`cite`). A golden run therefore cites the values the tools actually returned,
not values hardcoded in the test — the same discipline the real model is held
to. Hardcode a value only when the point of the scenario is that it was never
retrieved.

No network, no clock, no randomness: same script in, same run out.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

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

# A late-bound argument builder: reads the conversation the engine has built so
# far (including tool results) and returns the tool arguments.
ArgBuilder = Callable[[list[Msg]], dict]


@dataclass
class ScriptedCall:
    name: str
    arguments: dict | ArgBuilder = field(default_factory=dict)


@dataclass
class ScriptedTurn:
    """One assistant turn: some prose, then zero or more tool calls.

    `provider_error` scripts a mid-stream provider failure: the text streams as
    normal and then the provider raises, which is what a dropped connection or a
    500 halfway through a response looks like to the engine.
    """

    text: str = ""
    tool_calls: list[ScriptedCall] = field(default_factory=list)
    input_tokens: int = 1200
    output_tokens: int = 240
    cache_read_tokens: int = 0
    stop_reason: str | None = None  # defaults from whether tools were called
    provider_error: str | None = None


@dataclass
class ProviderCall:
    """What the engine asked the model on one iteration (for assertions)."""

    model: str
    system: str
    messages: list[Msg]
    tool_names: list[str]
    max_tokens: int
    temperature: float

    @property
    def last_message(self) -> Msg | None:
        return self.messages[-1] if self.messages else None


class ReplayProvider(Provider):
    """Plays a fixed script of turns. One instance per scenario run."""

    name = "replay"

    def __init__(
        self,
        turns: list[ScriptedTurn],
        *,
        json_responses: list[dict] | None = None,
        strict_tools: bool = True,
        text_chunk_size: int = 40,
    ):
        self.turns = list(turns)
        self.calls: list[ProviderCall] = []
        self.json_calls: list[dict] = []
        self.violations: list[str] = []
        self._json_responses = list(json_responses or [])
        self._strict_tools = strict_tools
        self._chunk = text_chunk_size

    # ── Provider ABC ─────────────────────────────────────────────────────────
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
        index = len(self.calls)
        offered = [t.name for t in tools]
        self.calls.append(
            ProviderCall(
                model=model,
                system=system,
                messages=list(messages),
                tool_names=offered,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        )
        if index >= len(self.turns):
            raise self._violation(
                f"script exhausted: the engine asked for turn {index + 1} but only "
                f"{len(self.turns)} were scripted. The loop ran longer than the "
                "scenario expects — script the extra turn or fix the expectation."
            )
        turn = self.turns[index]

        for start in range(0, len(turn.text), self._chunk):
            yield TextDelta(turn.text[start : start + self._chunk])

        if turn.provider_error is not None:
            # A deliberate failure, not a script violation: the engine is expected
            # to handle it, so it is not recorded in `violations`.
            raise ProviderError(self.name, turn.provider_error)

        calls: list[ToolCall] = []
        for position, scripted in enumerate(turn.tool_calls, start=1):
            if self._strict_tools and scripted.name not in offered:
                raise self._violation(
                    f"turn {index + 1} calls '{scripted.name}' but the engine offered "
                    f"{offered}. Either the pack no longer enables that tool for this "
                    "task, or the script is wrong."
                )
            arguments = (
                scripted.arguments(list(messages))
                if callable(scripted.arguments)
                else scripted.arguments
            )
            call = ToolCall(
                id=f"call-{index + 1}-{position}", name=scripted.name, arguments=arguments
            )
            calls.append(call)
            yield ToolCallComplete(call)

        yield TurnComplete(
            usage=Usage(
                input_tokens=turn.input_tokens,
                output_tokens=turn.output_tokens,
                cache_read_tokens=turn.cache_read_tokens,
            ),
            stop_reason=turn.stop_reason or ("tool_use" if calls else "end_turn"),
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
    ) -> dict:
        """Scripted structured completion (the router and QA graders use this)."""
        self.json_calls.append({"model": model, "prompt": prompt, "tool_name": tool_name})
        if self._json_responses:
            return self._json_responses.pop(0)
        # Unscripted: answer the router's schema deterministically with the first
        # candidate, so `auto` routing stays offline instead of blowing up.
        choices = ((schema.get("properties") or {}).get("model_id") or {}).get("enum") or []
        if choices:
            return {
                "model_id": choices[0],
                "reasoning": "ReplayProvider: first candidate.",
                "confidence": "high",
            }
        raise self._violation(
            f"complete_json called for tool '{tool_name}' with no scripted response."
        )

    # ── helpers ──────────────────────────────────────────────────────────────
    def _violation(self, message: str) -> AssertionError:
        self.violations.append(message)
        return AssertionError(f"ReplayProvider: {message}")

    @property
    def turns_played(self) -> int:
        return len(self.calls)


# ── script-writing helpers ────────────────────────────────────────────────────
def tool_results(messages: list[Msg], tool_name: str) -> list[str]:
    """Every tool result the engine has fed back for `tool_name`, in order."""
    by_id = {call.id: call.name for m in messages for call in m.tool_calls}
    return [
        m.content or ""
        for m in messages
        if m.role == "tool" and by_id.get(m.tool_call_id) == tool_name
    ]


def rows_of(messages: list[Msg], tool_name: str, call_index: int = -1) -> list[dict]:
    """Parsed rows from a `lookup_dataset` / `run_method` result.

    Both tools return rows carrying the `_row` reference that citations must
    quote, which is what makes derived citations possible.
    """
    results = tool_results(messages, tool_name)
    if not results:
        raise AssertionError(f"no {tool_name} result in the conversation yet")
    parsed = json.loads(results[call_index])
    rows = parsed.get("rows", []) if isinstance(parsed, dict) else parsed
    return list(rows)


def find_row(rows: list[dict], **match) -> dict:
    for row in rows:
        if all(str(row.get(k)) == str(v) for k, v in match.items()):
            return row
    raise AssertionError(f"no row matching {match} in {rows}")


def cite(row: dict, column: str) -> dict:
    """Build one cited_values entry from a retrieved row, verbatim.

    `_row` is `"<dataset>:<index>"` for dataset lookups and
    `"method/<slug>/<method_run_id>:<index>"` for method outputs; the dataset
    part is exactly what the engine recorded in `ctx.retrieved_values`.
    """
    return {
        "dataset": row["_row"].rsplit(":", 1)[0],
        "row_ref": row["_row"],
        "column": column,
        "value": str(row[column]),
    }
