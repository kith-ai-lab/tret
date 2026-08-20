"""Reading signals back out of a persisted run transcript.

`runs.messages` is the audit record, and it is also the only place several
things the engine did are written down: structured-output validation rejected a
payload, a tool errored, the engine had to nudge the model toward its terminal
action, the repeated-call breaker fired. None of those are their own table.

Two callers read them, and they must read them the same way: guardrail analytics
(`api/analytics.py`) reports validation pressure per harness, and outcome
scoring (`router_llm/outcomes.py`) turns the same events into evidence about how
a model performed. Before this module the parser lived inside the analytics API,
which put transcript parsing behind a FastAPI import and would have had the
router importing the API layer to reuse it.

Two kinds of signal live here, and the difference matters:

* **Textual markers** — `VALIDATION_MARKER` / `EXHAUSTED_MARKER` — match strings
  the engine writes into tool *results* (`engine/tools.py::_record`). They are
  matched as text because the payload the model sees is the text.
* **Structural markers** — `meta` keys the engine stamps on the messages it
  appends itself (`ENGINE_NUDGE_KEY`, `REPEATED_CALL_KEY`). These are read as
  data, never matched as prose: a nudge's wording is user-facing copy that will
  be reworded, and a scorer that depended on the wording would silently start
  reading zero the day someone improved the sentence.
"""
from __future__ import annotations

from dataclasses import dataclass

# Written into tool *results* by engine/tools.py::_record when a structured
# payload fails its schema, and when the repair budget is spent.
VALIDATION_MARKER = "Validation failed"
EXHAUSTED_MARKER = "repair attempts are exhausted"

# Stamped by engine/harness.py onto messages the engine itself appends.
ENGINE_NUDGE_KEY = "engine_nudge"  # "terminal_tool" | "output_budget"
REPEATED_CALL_KEY = "repeated_call"  # int: how many times this exact call was made
NUDGE_TERMINAL = "terminal_tool"
NUDGE_OUTPUT_BUDGET = "output_budget"


def validation_errors_in(messages: list) -> tuple[int, int]:
    """(validation errors, of which exhausted repair budget) in one transcript."""
    total = exhausted = 0
    for msg in messages or []:
        if not isinstance(msg, dict) or msg.get("role") != "tool":
            continue
        if not (msg.get("meta") or {}).get("error"):
            continue
        content = msg.get("content") or ""
        if VALIDATION_MARKER in content:
            total += 1
            if EXHAUSTED_MARKER in content:
                exhausted += 1
    return total, exhausted


@dataclass(frozen=True)
class TranscriptSignals:
    """What a transcript says about how the run went, counted once.

    `tool_errors` counts *every* failed tool result; `validation_errors` counts
    the subset that failed schema validation. They overlap on purpose — a caller
    that wants "errors that were not validation failures" subtracts, and the two
    totals stay independently meaningful.
    """

    tool_calls: int = 0
    tool_errors: int = 0
    validation_errors: int = 0
    unrecovered_validation_errors: int = 0
    terminal_nudged: bool = False
    output_budget_nudged: bool = False
    repeated_call_trips: int = 0

    @property
    def non_validation_tool_errors(self) -> int:
        return max(0, self.tool_errors - self.validation_errors)


def read_signals(messages: list) -> TranscriptSignals:
    """Every signal this module knows how to read, in one pass."""
    tool_calls = tool_errors = repeated = 0
    terminal_nudged = budget_nudged = False
    validation, unrecovered = validation_errors_in(messages)

    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        meta = msg.get("meta") or {}
        role = msg.get("role")
        if role == "assistant":
            tool_calls += len(msg.get("tool_calls") or [])
        elif role == "tool":
            if meta.get("error"):
                tool_errors += 1
            # Counted once per message, not once per repetition: the engine
            # stamps the running count, and summing those would score a call
            # made four times as 2+3+4 trips.
            if meta.get(REPEATED_CALL_KEY):
                repeated += 1
        nudge = meta.get(ENGINE_NUDGE_KEY)
        if nudge == NUDGE_TERMINAL:
            terminal_nudged = True
        elif nudge == NUDGE_OUTPUT_BUDGET:
            budget_nudged = True

    return TranscriptSignals(
        tool_calls=tool_calls,
        tool_errors=tool_errors,
        validation_errors=validation,
        unrecovered_validation_errors=unrecovered,
        terminal_nudged=terminal_nudged,
        output_budget_nudged=budget_nudged,
        repeated_call_trips=repeated,
    )
