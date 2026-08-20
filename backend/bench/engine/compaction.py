"""Making a long run fit its model's context window, without lying about it.

Nothing in bench compared the growing transcript against
`ModelInfo.context_window` — the field was rendered into the router prompt and
then never used again — so a long run met its limit as an opaque `ProviderError`
part-way through, and a long chat thread on a small-window model simply could not
finish. This module is the fix.

**The transcript is not what gets compacted.** `runs.messages` stays the complete
record of what actually happened; compaction produces a separate *wire view* that
is sent to the provider. This is the load-bearing decision of the whole feature.
The transcript is the audit artifact — it is what an approver reads when deciding
whether to trust a finding, and what an operator reads when a run went wrong —
and a feature that quietly edited it would cost more than the tokens it saved.
Every compaction is itself recorded (`runs.compactions`), so the gap between what
happened and what the model could still see is stated rather than hidden.

**Structure is never altered, only content.** No message is ever dropped: an
elided message keeps its role, its `tool_call_id` and its `tool_calls`, and only
its text is replaced by a marker naming what was there. Dropping messages would
break the pairing between a tool call and its result, which makes a transcript
unreplayable — and the failure would show up as a provider error at some later
iteration, nowhere near the code that caused it.

**Some results are not elidable at any price.** A model may only cite numbers it
retrieved in this run, and `validate_cited_values` checks each citation against
what the tools actually returned. Eliding a `lookup_dataset` result would leave
the model unable to quote it verbatim, so every finding that cited it would fail
validation — compaction would manufacture exactly the failure it was invoked to
prevent. `PROTECTED_TOOLS` is that list, and `test_compaction.py` asserts every
builtin tool is deliberately classified, so adding a tool forces the decision
rather than defaulting it.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from bench.engine.context import estimate_tokens
from bench.providers.base import Msg, ToolSpec
from bench.services.emissions import overhead_call

# ── what may be elided ───────────────────────────────────────────────────────
# Retrieval that returns bulk text. This is where nearly all the weight of a long
# run sits, and losing the exact wording of a document the model has already read
# and reasoned about costs little.
ELIDABLE_TOOLS = frozenset(
    {
        "read_document",
        "search_documents",
        "list_prior_findings",
        "run_harness_task",
        # The web tools are elidable by construction rather than by judgment.
        # `web_search` returns titles, URLs and vendor snippets that its own
        # description forbids quoting or citing — navigation, not evidence.
        # `fetch_url` stores the page as a Document and returns a short preview
        # plus that document's id, so the page itself lives in Postgres and not
        # in the context window: a model whose old fetch result was elided can
        # still re-read the page in full via `read_document`.
        "web_search",
        "fetch_url",
    }
)
# Never elided, for two distinct reasons:
#
#   * `lookup_dataset` and `run_method` are the deterministic lane. They are the
#     only sanctioned source of a number, and citations are validated against
#     their literal output — elide one and every finding citing it fails.
#   * the recording tools are the run's output. A run that can no longer see what
#     it already recorded will record it again, or contradict it.
PROTECTED_TOOLS = frozenset(
    {
        "lookup_dataset",
        "run_method",
        "record_verdict",
        "record_finding",
        "draft_section",
        "file_data_request",
    }
)

# Iterations kept verbatim no matter what. The recent turns are where the model's
# current line of reasoning lives; compacting into them is how a run loses its
# thread and starts over.
KEEP_RECENT_ITERATIONS = 3

# A message whose content is already this short frees nothing worth the marker.
MIN_ELIDABLE_CHARS = 400

ELISION_TEMPLATE = "[elided by bench at iteration {at}: {tool} result, {chars} characters]"

SUMMARY_PREFACE = (
    "CONTEXT NOTE (written by bench, not by you): this conversation exceeded the "
    "model's context window, so earlier tool results were elided. What they "
    "contained is summarized below. Retrieved values, recorded results and your "
    "own instructions were NOT elided and appear above in full. If you need "
    "detail that is only in this summary, retrieve it again rather than relying "
    "on the summary for anything you must cite.\n\n"
)

SUMMARIZER_SYSTEM = (
    "You compress tool output for an analyst's working context. Summarize what "
    "the material established, in plain prose, preserving specifics that later "
    "reasoning would need: entities, dates, document names, findings and their "
    "qualifications. Do not restate numeric values as though they were retrieved "
    "— say where a number came from instead. Be dense; omit nothing load-bearing."
)


def summary_schema() -> dict:
    return {
        "type": "object",
        "required": ["summary"],
        "properties": {"summary": {"type": "string", "maxLength": 4000}},
    }


# ── measuring ────────────────────────────────────────────────────────────────
def estimate_message_tokens(msg: Msg) -> int:
    """One message's share of the wire, including its tool-call payload.

    Tool call arguments are part of what is sent and can be substantial (a long
    `search_documents` query, a whole drafted section), so counting only
    `content` understates a tool-heavy turn — which is exactly the kind of turn
    that fills a window.
    """
    total = estimate_tokens(msg.content or "")
    for call in msg.tool_calls or []:
        total += estimate_tokens(call.name) + estimate_tokens(str(call.arguments))
    return total


def estimate_wire_tokens(system: str, messages: list[Msg], tools: list[ToolSpec]) -> int:
    """What one request to the provider is about to cost in input tokens.

    The same chars/4 estimator `engine/context.py` uses for composition
    accounting — deliberately dependency-free and provider-independent. It is an
    estimate and is treated as one: the headroom it is compared against exists
    partly to absorb its error.
    """
    total = estimate_tokens(system)
    total += sum(estimate_message_tokens(m) for m in messages)
    for spec in tools:
        total += estimate_tokens(spec.name + spec.description + str(spec.parameters))
    return total


def budget(context_window: int, max_output_tokens: int, headroom: float) -> int:
    """The most input this run may send before the engine has to intervene.

    The output reservation is subtracted rather than folded into the headroom
    because it is not slack — it is the part of the window already spoken for by
    the answer the model has yet to write.
    """
    if not context_window:
        # A discovered local model may report no window at all. Unknown is not a
        # licence to assume infinity, but it is also not something to guess at:
        # returning 0 makes `over_budget` never fire, which leaves such a model
        # exactly where it was before this module existed.
        return 0
    return max(0, int(context_window * headroom) - max_output_tokens)


def over_budget(est_tokens: int, limit: int) -> bool:
    return bool(limit) and est_tokens > limit


# ── planning ─────────────────────────────────────────────────────────────────
@dataclass
class CompactionState:
    """What has been elided so far, and the audit record of how it got that way.

    Held by the engine for the life of a run and applied to the transcript on
    every turn, so a message elided at iteration 9 stays elided at iteration 20
    without being reconsidered.
    """

    # index -> the iteration at which it was elided, so each marker can say when
    # it happened rather than all of them quoting the latest compaction.
    elided: dict[int, int] = field(default_factory=dict)
    summary: str | None = None
    summary_at: int | None = None  # transcript index the note is inserted before
    records: list[dict] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return bool(self.elided or self.summary)


@dataclass
class CompactionPlan:
    """Which messages to elide, and what it buys."""

    elide: list[int]
    freed_tokens: int
    boundary: int | None  # transcript index the summary note goes before
    elided_tools: list[str]

    @property
    def empty(self) -> bool:
        return not self.elide


def _tool_names_by_call_id(messages: list[Msg]) -> dict[str, str]:
    return {
        call.id: call.name
        for msg in messages
        if msg.role == "assistant"
        for call in msg.tool_calls or []
    }


def _iteration_of(msg: Msg) -> int:
    return int((msg.meta or {}).get("iteration") or 0)


def plan_compaction(
    messages: list[Msg],
    *,
    state: CompactionState,
    current_iteration: int,
    terminal_tool: str | None = None,
    keep_recent: int = KEEP_RECENT_ITERATIONS,
) -> CompactionPlan:
    """Choose what to elide next. Pure — no I/O, no provider, no clock.

    Walks oldest-first, because the oldest bulk retrieval is both the least
    likely to be load-bearing now and the most likely to be large.
    """
    names = _tool_names_by_call_id(messages)
    cutoff = current_iteration - keep_recent
    elide: list[int] = []
    tools: list[str] = []
    freed = 0
    boundary: int | None = None

    for index, msg in enumerate(messages):
        if _iteration_of(msg) > cutoff:
            # First message of the retained region: where the note belongs, so it
            # lands on an iteration boundary and never between a tool call and
            # the result answering it.
            if boundary is None and _iteration_of(msg):
                boundary = index
            continue
        if index in state.elided or msg.role != "tool":
            continue
        tool = names.get(msg.tool_call_id or "")
        if tool is None or tool == terminal_tool or tool in PROTECTED_TOOLS:
            continue
        if tool not in ELIDABLE_TOOLS:
            # Unclassified: left alone. A tool nobody decided about is treated as
            # protected, because the failure from wrongly eliding is silent and
            # the failure from wrongly keeping is only a larger prompt.
            continue
        content = msg.content or ""
        if len(content) < MIN_ELIDABLE_CHARS:
            continue
        elide.append(index)
        tools.append(tool)
        freed += estimate_tokens(content) - estimate_tokens(
            ELISION_TEMPLATE.format(at=current_iteration, tool=tool, chars=len(content))
        )

    return CompactionPlan(elide=elide, freed_tokens=max(0, freed), boundary=boundary,
                          elided_tools=tools)


def apply_plan(state: CompactionState, plan: CompactionPlan, iteration: int) -> None:
    state.elided.update({index: iteration for index in plan.elide})
    # `None` means nothing was retained — every message is older than the cutoff.
    # The note then belongs at the end, which `wire_view` handles.
    state.summary_at = plan.boundary


def trim_history(
    history: list[Msg],
    *,
    system: str,
    user_message: str,
    tools: list[ToolSpec],
    limit: int,
) -> tuple[list[Msg], int]:
    """Drop the oldest prior turns until this turn can start inside its window.

    A separate mechanism from eliding tool results, for a different problem.
    `api/chat.py` hands the engine the last `MAX_HISTORY_TURNS` turns of a
    conversation with no notion of how large they are or which model will run
    them — so a long thread routed to a small-window model failed on its first
    call, as an opaque provider error with nothing in the transcript explaining
    why. The trim has to happen here, after routing, because here is the first
    point at which the model's actual context window is known.

    Whole turns are dropped, oldest first: these are plain user/assistant text
    with no tool structure to break, and half a turn is worse than none. This run's
    own task input is never at risk — only conversation that preceded it, and the
    caller records how much went.
    """
    kept = list(history)
    dropped = 0
    while kept and over_budget(
        estimate_wire_tokens(system, [*kept, Msg(role="user", content=user_message)], tools),
        limit,
    ):
        kept.pop(0)
        dropped += 1
    return kept, dropped


# ── the wire view ────────────────────────────────────────────────────────────
def wire_view(messages: list[Msg], state: CompactionState) -> list[Msg]:
    """What the provider sees. `messages` itself is never modified.

    Returns the transcript unchanged when nothing has been compacted, so a run
    that never came near its window sends exactly the bytes it always did.
    """
    if not state.active:
        return messages

    names = _tool_names_by_call_id(messages)
    out: list[Msg] = []
    for index, msg in enumerate(messages):
        if state.summary and state.summary_at == index:
            out.append(Msg(role="user", content=SUMMARY_PREFACE + state.summary))
        if index in state.elided:
            tool = names.get(msg.tool_call_id or "") or "tool"
            out.append(
                Msg(
                    role=msg.role,
                    content=ELISION_TEMPLATE.format(
                        at=state.elided[index],
                        tool=tool,
                        chars=len(msg.content or ""),
                    ),
                    # Preserved exactly: the pairing between a call and its
                    # result is what keeps the conversation replayable.
                    tool_calls=msg.tool_calls,
                    tool_call_id=msg.tool_call_id,
                )
            )
        else:
            out.append(msg)
    if state.summary and (state.summary_at is None or state.summary_at >= len(messages)):
        # Nothing was retained to sit after, so the note goes last. Safe in this
        # position for the same reason the engine's own nudges are: every tool
        # call in the transcript has already been answered by the time a turn
        # ends, so a trailing user message never orphans one.
        out.append(Msg(role="user", content=SUMMARY_PREFACE + state.summary))
    return out


def elided_source_text(messages: list[Msg], plan: CompactionPlan, limit: int = 60_000) -> str:
    """The material a summarizer is asked to compress, bounded."""
    parts = []
    used = 0
    for index in plan.elide:
        content = messages[index].content or ""
        if used + len(content) > limit:
            content = content[: max(0, limit - used)]
        parts.append(content)
        used += len(content)
        if used >= limit:
            break
    return "\n\n---\n\n".join(p for p in parts if p)


async def summarize(
    provider, model, text: str, *, timeout: float = 60.0
) -> tuple[str | None, dict | None]:
    """(summary, what it cost) — either may be None, independently.

    Failure is not an error. The deterministic elision has already happened and
    already freed the space; a summary is an improvement on top of it, and a run
    that loses its summarizer is better off continuing with markers than failing.

    The spend is returned even when the summary is unusable, because the tokens
    were spent either way — a summarizer that answers with an empty string still
    cost money, and that is precisely the case where silently dropping the cost
    would flatter the numbers.
    """
    if not text.strip():
        return None, None
    try:
        completion = await provider.complete_json(
            model=model.wire_id,
            system=SUMMARIZER_SYSTEM,
            prompt=text,
            schema=summary_schema(),
            tool_name="summarize",
            max_tokens=1500,
            timeout=timeout,
        )
    except Exception:  # noqa: BLE001 - a lost summarizer must not fail the run
        return None, None
    spend = overhead_call("compaction_summary", model, completion.usage)
    summary = (completion.payload or {}).get("summary")
    usable = summary.strip() if isinstance(summary, str) and summary.strip() else None
    return usable, spend
