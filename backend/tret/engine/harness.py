"""HarnessEngine: the agent loop.

One entry point, `execute(run_id)`, designed to run as a background task. It
loads the run, assembles context, routes the model, executes the tool loop,
persists the transcript/cost after every iteration, and publishes RunEvents.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import select

if TYPE_CHECKING:
    from tret.services.emission_factors import EmissionsOverrides, FactorSet
    from tret.services.energy_meter import EnergyMeter, MeterReading

from tret.db.engine import get_session_factory
from tret.db.models import Document, Harness, Pack, Project, Run
from tret.adaptive import adaptive_of
from tret.engine.compaction import (
    CompactionState,
    apply_plan,
    elided_source_text,
    estimate_message_tokens,
    estimate_wire_tokens,
    over_budget,
    plan_compaction,
    required_context_window,
    summarize,
    trim_history,
    wire_view,
)
from tret.engine.compaction import budget as context_budget
from tret.engine.context import (
    TOKEN_ESTIMATOR,
    assemble_context,
    block_for,
    build_user_message,
    composition_report,
    task_config,
    tool_spec_block,
)
from tret.engine.events import RunEvent, get_event_bus
from tret.engine.extensions import get_extension_registry
from tret.engine.tools import (
    CONNECTOR_TOOL_NAMES,
    COST_CAP_KEY,
    DELEGATION_DEPTH_KEY,
    LESSON_TOOL_NAMES,
    WEB_TOOL_NAMES,
    WRITE_CONNECTOR_TOOL_NAMES,
    RunContext,
    execute_tool,
    get_builtin_tools,
    withheld_connector_tools,
    withheld_web_tools,
)
from tret.providers.base import (
    Msg,
    ProviderError,
    TextDelta,
    ToolCall,
    ToolCallComplete,
    TurnComplete,
    Usage,
)
from tret.providers.catalog import (
    ModelCatalog,
    ModelInfo,
    ProviderRegistry,
    energy_accounting,
    get_catalog,
)
from tret.router_llm.objectives import objective_of
from tret.router_llm.priors import OutcomePriors, PriorsProvider
from tret.router_llm.router import ModelRouter, RoutingUnavailable
from tret.engine.supervisor import (
    KIND_EFFORT,
    Intervention,
    TurnState,
    assess,
    normalize_for_provider,
)
from tret.config import get_settings
from tret.services.emission_settings import factor_set_for, workspace_emissions_layers
from tret.services.emissions import (
    DEPLOYMENT_LOCAL,
    LOCAL_PROVIDER,
    combine_accountings,
    deployment_for,
    overhead_block,
    emission_event_fields,
    energy_wh_field,
)
from tret.services.energy_collector import collected_meter
from tret.services.energy_meter import NvidiaSmiMeter, NvmlEnergyMeter, meter_for_settings
from tret.services.lessons import approved_lessons, lessons_enabled
from tret.services.outcomes import record_outcome
from tret.services.transcript import (
    ENGINE_NUDGE_KEY,
    NUDGE_EMPTY_REPLY,
    NUDGE_GROUNDING,
    NUDGE_OUTPUT_BUDGET,
    NUDGE_TERMINAL,
    REPEATED_CALL_KEY,
)
from tret.engine.grounding import (
    GROUNDING_MAX_REPAIRS,
    evidence_numbers,
    grounding_nudge_message,
    run_has_retrieval_evidence,
    unsupported_numbers,
)

log = logging.getLogger("tret.harness")

DEFAULT_MAX_ITERATIONS = 24
DEFAULT_MAX_OUTPUT_TOKENS = 8192
DEFAULT_TEMPERATURE = 0.2
DEFAULT_MAX_COST_USD = Decimal("5.0")
# A harness config can lower the iteration cap but never raise it past this:
# every iteration re-sends the whole conversation, so runaway loops are the
# most expensive failure mode there is.
MAX_ITERATIONS_CEILING = 50
# Optional per-run output-token budget, set as model_policy["max_run_output_tokens"].
# Soft: crossing it asks the model to finalize now. Hard stop at this multiple of
# it, so a model that ignores the instruction still cannot run away.
OUTPUT_BUDGET_HARD_MULTIPLE = Decimal("1.5")

# A turn's `cache_read_tokens` at or below this counts as a cache miss for the
# ledger below. 0 today — no provider in the catalog has ever been observed to
# report a nonzero-but-noise figure for a genuine miss — named as a constant
# rather than a literal `0` so a provider that does turn up with that kind of
# rounding noise is a one-line fix, not a hunt through `_book_usage`.
CACHE_MISS_FLOOR_TOKENS = 0

# Providers named here never report a cache figure at all, under any route: a
# local deployment's usage is the engine's own per-token estimate with no
# cache concept behind it, and Kimi's *native* API does not return a
# cached-token count in practice. `kimi` here means that native API
# specifically, not the model — a Kimi model reached through OpenRouter is a
# `model_info.provider == "openrouter"` segment, not `"kimi"`, and is not in
# this set.
#
# This set is a cheap shortcut, not the real gate: a provider can also fail to
# report caching while sitting outside this set entirely (an OpenRouter
# upstream that omits `cached_tokens`, e.g. a Kimi model served that way), and
# a prompt below a provider's cacheable minimum reports zero on both sides
# even when the provider caches perfectly well otherwise. `_book_usage`'s own
# `cache_is_live` check is what actually decides whether a turn is classified,
# for every provider including these two — being in this set just means the
# answer is always "no" without having to look.
NO_CACHE_STATS_PROVIDERS = frozenset({LOCAL_PROVIDER, "kimi"})

# ── budget awareness ─────────────────────────────────────────────────────────
# tret enforces iteration, cost, output-token and context-window caps but,
# before this, never told the model where it stood against any of them.
# Google's budget-aware test-time-scaling result (COLM 2026) and Anthropic's
# own injected context-budget tags on Sonnet-class models both found agents
# allocate work better once they can see what is left. Anthropic's tag is
# context-only and Anthropic-only; this is uniform across every provider tret
# talks to — see `_budget_line` and `_append_budget_line` below for what gets
# added and where. `loop_config.budget_line: false` opts a harness out (see
# `docs/architecture.md`'s loop paragraph).
BUDGET_LINE_DEFAULT = True

# The line's own stable opening — no builtin tool ever emits this literal
# string, so it doubles as an anchor a consumer can use to recover a tool
# result's real content when that result happens to be the wire's tail
# message. `tests/evals/replay_provider.py`'s scripted argument builders are
# exactly that consumer: a citation script reading back the last
# `lookup_dataset` result and JSON-parsing it verbatim would otherwise choke
# on this line the same way it already has to split off `engine/tools.py`'s
# `[TRUNCATED: ...]` size-cap marker by hand (see
# `tests/evals/test_token_economy.py`).
BUDGET_LINE_MARKER = "[tret budget: "

# ── exact token counting near the boundary ──────────────────────────────────
# `estimate_wire_tokens` (chars/4) is deliberately dependency-free and
# provider-independent, but Anthropic's newest tokenizer produces roughly 30%
# more tokens than earlier generations for the same text — chars/4 now
# underestimates on the newest models specifically where the margin matters
# most: right at the edge of the window, where a compaction or model-switch
# decision is about to be made on it. Below this fraction of the budget the
# chars/4 estimate has plenty of headroom to be wrong in either direction and
# an exact count would only spend a provider call for no behavior change; at
# or above it, `Provider.count_tokens` is asked for the real number when the
# provider offers one. 0.85 rather than closer to 1.0 so the exact figure is
# in hand with at least one turn's worth of margin before `over_budget` would
# actually fire on the estimate alone.
EXACT_COUNT_THRESHOLD = 0.85


def _near_context_limit(est_tokens: int, context_limit: int) -> bool:
    """Whether `est_tokens` (the chars/4 estimate) is close enough to
    `context_limit` that an exact provider count is worth the call. A `0`
    limit — `budget()`'s own "unknown/unenforceable" signal for a model with
    no reported context window — always answers False: there is no boundary
    to be near, and `over_budget` never fires against it either.
    """
    return bool(context_limit) and est_tokens >= EXACT_COUNT_THRESHOLD * context_limit


def _effective_max_cost(harness_cap: Decimal, task_input: dict) -> Decimal:
    """`harness_cap` narrowed by `_cost_cap_usd` (COST_CAP_KEY) if a
    delegating parent stamped one onto `task_input` (`_prepare_child`,
    engine/tools.py) — via `min`, never assignment, so a caller who sets the
    key by hand through the runs API can only ever lower their own run's cap,
    never raise it. Anything that doesn't parse to a positive Decimal is
    ignored rather than raised: a malformed or stale value should not be able
    to fail the run before it starts.
    """
    raw_cap = task_input.get(COST_CAP_KEY)
    if raw_cap is None:
        return harness_cap
    try:
        stamped_cap = Decimal(str(raw_cap))
    except (ArithmeticError, ValueError, TypeError):
        return harness_cap
    # `Decimal("nan")` constructs without complaint and only raises once it is
    # compared, so finiteness is checked before the sign.
    if not stamped_cap.is_finite() or stamped_cap <= 0:
        return harness_cap
    return min(harness_cap, stamped_cap)


def _spent(run: Run) -> Decimal:
    """What `run`'s cost cap is checked against: its own model spend plus
    whatever it has caused through delegation so far. The two are separate
    columns (`cost_usd` must stay pure per-run spend for `api/analytics.py`'s
    rollup — see `Run.delegated_cost_usd`'s own comment) but the cap doesn't
    care about that split; it cares about total exposure.
    """
    return (run.cost_usd or Decimal(0)) + (run.delegated_cost_usd or Decimal(0))


def _format_token_budget(n: int) -> str:
    """`n` rounded to a short k/M suffix (`118000` -> `"118k"`, `2100` ->
    `"2.1k"`) — `_budget_line`'s own ~160-char ceiling rules out a raw count
    on a six-figure context window.
    """
    for threshold, suffix in ((1_000_000, "M"), (1_000, "k")):
        if n >= threshold:
            value = f"{n / threshold:.1f}".rstrip("0").rstrip(".")
            return f"{value}{suffix}"
    return str(n)


def _budget_line(
    *,
    iteration: int,
    max_iterations: int,
    cost_so_far: Decimal,
    max_cost: Decimal,
    est_tokens: int,
    context_limit: int,
    output_tokens_so_far: int,
    output_budget: int,
) -> str:
    """One line naming this run's position against every cap the engine
    already enforces — never a cap the engine does not have, and this
    function changes none of them; see `_execute_inner`'s loop for where it
    is applied. `context_limit` is `budget()` from `engine/compaction.py`
    (the same figure `over_budget` compares against, not the raw context
    window), and `output_budget` is the optional per-run
    `model_policy["max_run_output_tokens"]` — omitted entirely when a run has
    none, rather than shown against the per-turn `max_output_tokens`, which
    every run has and would make the clause meaningless.
    """
    parts = [
        f"iteration {iteration} of {max_iterations}",
        f"${cost_so_far:.2f} of ${max_cost:.2f} spent",
    ]
    if context_limit:
        parts.append(
            f"~{_format_token_budget(est_tokens)} of {_format_token_budget(context_limit)} "
            "context tokens"
        )
    if output_budget:
        parts.append(
            f"output {_format_token_budget(output_tokens_so_far)} of "
            f"{_format_token_budget(output_budget)}"
        )
    return BUDGET_LINE_MARKER + " · ".join(parts) + "]"


def _append_budget_line(wire: list[Msg], line: str) -> list[Msg]:
    """`wire` with a new trailing `Msg(role="user", content=line, meta={"budget_line":
    True})` — a new list, never `wire` itself, and never touching any message
    already in it.

    A separate message, not text concatenated onto `wire[-1].content` (the prior
    approach): concatenating changed the byte content of whichever message
    happened to be the wire's tail — the folded tool-result turn on most
    iterations — which is exactly the content each provider's tail cache
    breakpoint is written against. The breakpoint written over "tool result +
    this iteration's line" at turn N is not a prefix of "tool result + next
    iteration's line" at turn N+1, so the whole prior turn re-wrote the cache
    (billed at the ~1.25x write price) every single iteration instead of
    reading it. A trailing message is instead something each provider's own
    cache-control pass can recognize by its `meta` and skip, landing the
    breakpoint on the stable content before it — see
    `anthropic._apply_conversation_cache` and `openai_compat._apply_cache_
    control` (a plain trailing `user` message needs no special handling there:
    it is simply never marked). The persisted-transcript guarantee this
    replaced still holds the same way: on the no-compaction path `wire_view`
    returns `messages` itself (see its own docstring), and `messages` is
    `runs.messages`-bound, so this must never mutate `wire` or anything in it
    — only ever return a new list with a new `Msg` appended.
    """
    if not wire:
        return wire
    return [*wire, Msg(role="user", content=line, meta={"budget_line": True})]


def effective_model_policy(harness_policy: dict | None, task_input: dict | None) -> dict:
    """The harness policy with per-run overrides applied, for this run only.

    Chat's composer can ask for a different routing objective than the harness
    default (`_objective`); the harness row is never mutated, and the routing
    decision records which objective actually applied. Validation lives at the
    API boundary, so an unknown value would already have been rejected there;
    `objectives.objective_of` normalizes anything that slips through.
    """
    policy = dict(harness_policy or {"mode": "auto"})
    run_objective = (task_input or {}).get("_objective")
    if run_objective:
        policy["objective"] = run_objective
    return policy

# Terminal run statuses that are not failures. `completed_without_output` is the
# honest name for a run that ran to the end of its own accord but never landed a
# valid terminal result: the task required one (the pack names a `terminal_tool`)
# and none was recorded, usually because every attempt failed validation. It is
# not `failed` — the engine and the guardrails worked exactly as intended — but
# calling it `completed` would advertise a verdict that does not exist.
STATUS_COMPLETED = "completed"
STATUS_COMPLETED_WITHOUT_OUTPUT = "completed_without_output"
SUCCESS_STATUSES = (STATUS_COMPLETED, STATUS_COMPLETED_WITHOUT_OUTPUT)

# The two task types the engine implements itself: a conversational turn and an
# open-ended one. Every other task type must be declared by the run's pack —
# there is no third source of a task's meaning (see `engine/context.task_config`).
GENERIC_TASK_TYPES = ("chat", "freeform")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class ModelSegment:
    """One contiguous stretch of a run spent on one model.

    Exists because energy accounting is a *per-model* calculation — energy class,
    deployment PUE, grid intensity and the frontier baseline all come from the
    model — while `energy_accounting` was being handed the run's running totals
    and a single `ModelInfo`. For a run that never changes model that is correct
    and stays correct. For one that does, it would attribute every token in the
    run to whichever model happened to be current, which is not a rounding error:
    an S-class model and an R-class one differ by more than an order of
    magnitude in Wh per token.
    """

    model: ModelInfo
    reason: str
    from_iteration: int = 0
    to_iteration: int = 0
    usage: Usage = field(default_factory=Usage)
    cost_usd: Decimal = Decimal(0)
    # The layered factor set (tret/services/emission_factors.py) this segment's
    # model was resolved under — the workspace's own override document and any
    # managed layer an extension supplies, snapshotted once when the segment
    # was created. None (today's behaviour) when no layers were configured or
    # loading them failed; `energy_accounting` falls back to `settings` alone
    # in that case, exactly as it always has.
    factors: "FactorSet | None" = None
    # Reasoning-effort level this segment's calls were made at, or None when
    # either nothing was requested (a decision from before effort existed) or
    # this segment's model doesn't accept the control (`ModelInfo.
    # supports_effort` was False when the segment was created — see
    # `HarnessEngine._execute_inner` and `_switch_model`). Recorded on the
    # segment, not just read off the live `RoutingDecision`, because a
    # supervisor switch creates a new segment for a different model whose
    # `supports_effort` may disagree with the one the run started on.
    effort: str | None = None
    # Every reasoning-effort change the quality trigger's Rung 1 made to this
    # segment while it was live — `{at_iteration, from_effort, to_effort,
    # reason}` per raise, oldest first. An effort raise updates `effort` above
    # in place rather than starting a new segment (see `HarnessEngine.
    # _raise_effort`'s own docstring for why: a same-model segment boundary
    # left the segment it split off from looking `handed_off` to
    # `services/outcomes.py`, poisoning the very model the raise was trying to
    # keep), so this is the only place a segment's own effort history survives
    # — `run.routing["effort_changes"]` is the run-wide audit trail, and this
    # is that same fact attached to the segment it happened on.
    effort_history: list[dict] = field(default_factory=list)
    # True once any turn folded into this segment was an ESTIMATE rather than a
    # provider-reported figure — a turn whose stream died mid-way (see
    # `HarnessEngine._book_usage`). The segment's totals stay one running sum
    # either way (an estimate is still real tokens the provider was paid for),
    # but this says so, so analytics can tell a metered receipt from a guessed
    # one instead of reading both as equally certain.
    estimated_usage: bool = False
    # The most recent TurnComplete's `served_by` for this segment — the
    # upstream provider OpenRouter actually routed the call to (Anthropic
    # reports the constant "anthropic"; Kimi and other OpenAI-compatible
    # servers report nothing). Updated on every real TurnComplete, so a
    # session-affinity switch mid-run (rare, but OpenRouter's fallback path can
    # do it) is reflected rather than frozen at the first turn's answer. Never
    # touched by the ProviderError estimate path below — a dying turn has no
    # TurnComplete to read a served_by off, so the segment just keeps whatever
    # its last metered turn reported.
    served_by: str | None = None
    # One record per attempted provider call. Segment totals remain the legacy
    # billing view; this preserves call-specific upstream/geography/reasoning
    # evidence without applying the latest endpoint to earlier calls.
    call_records: list[dict] = field(default_factory=list)
    # The most recent *metered* (non-estimated) turn's cache_read_tokens for
    # this model in this run, or None if this model has not yet completed a
    # metered turn. A mid-stream death's estimate (see the `ProviderError`
    # handler in `_execute_inner`) has no wire-level way to tell how much of
    # its prompt the provider actually served from cache — the wire prefix is
    # unchanged from one turn to the next, so this is the best proxy available,
    # and carrying it forward keeps the estimate from booking a cached prefix
    # as if it were all fresh (see `CACHE_READ_MULTIPLIER` in
    # providers/catalog.py: pricing that 10x too high, straight into
    # `reported_cost_usd`, the billing column).
    last_reported_cache_read_tokens: int | None = None
    # How many of this segment's turns came back with no cache read (or below
    # `CACHE_MISS_FLOOR_TOKENS`) for a reason the engine itself caused —
    # the segment's first turn, the first turn after a compaction pass that
    # changed the wire view, or the first turn after a top-level effort raise
    # on an Anthropic model — versus a miss with no such explanation. Both are
    # real, paid-for cache rebuilds; the distinction is only whether tret can
    # account for why the prefix changed. Classified in `HarnessEngine.
    # _book_usage`, one turn at a time, but only once caching has shown itself
    # to be *live* on this segment — a turn that wrote to the cache, or an
    # earlier turn that read a nonzero figure back (`cache_is_live` in
    # `_book_usage`). Both stay 0 for a segment that never shows that
    # evidence: a provider in `NO_CACHE_STATS_PROVIDERS` (a local deployment,
    # or Kimi's own native API — not an OpenRouter-hosted Kimi endpoint, which
    # is excluded by the same live-activity test as any other OpenRouter
    # upstream that never reports `cached_tokens`, not by name), or simply a
    # segment whose every prompt so far has been below the provider's
    # cacheable minimum (reads 0, writes 0 — indistinguishable from "no cache
    # concept" without the live check).
    cache_rebuilds_expected: int = 0
    cache_misses_unexpected: int = 0
    # Set only for a segment whose model is on a local deployment
    # (`deployment_for`) AND a meter is configured (`TRET_LOCAL_ENERGY_METER`):
    # the running `EnergyMeter` while the segment is live (`HarnessEngine.
    # _start_meter`/`_stop_meter`), and — once the segment ends, a model
    # switch or the run itself finishing — the `MeterReading` `stop()`
    # produced. `meter` and `meter_reading` are never both set: the meter is
    # cleared the moment it is stopped. Neither ever appears on a cloud
    # segment or an unmetered local one; `accounting()` below reads
    # `meter_reading` being present as "this segment was measured", exactly
    # the same way `energy_accounting`'s own `measured_energy_wh` parameter
    # does for a single call.
    meter: "EnergyMeter | None" = field(default=None, repr=False, compare=False)
    meter_reading: "MeterReading | None" = field(default=None, repr=False, compare=False)
    # `meter.describe()`, captured once when the meter is constructed — kept
    # separately from `meter` itself because `meter` is cleared the moment
    # `stop()` returns, but `accounting()` still needs `interval_s` for the
    # `energy_meter` block on every call after that.
    meter_describe: dict | None = field(default=None, repr=False, compare=False)

    @property
    def known_additional_reasoning_tokens(self) -> int:
        """Reasoning tokens known to sit outside provider output totals.

        This deliberately remains available when another call in the segment
        has unknown reasoning metadata.  ``usage.reasoning_tokens`` describes
        whether the aggregate metadata is complete; this value is only the
        confirmed additive portion needed by energy estimation.
        """
        return sum(
            int(record["reasoning_tokens"])
            for record in self.call_records
            if record["reasoning_accounting"] == "additional"
            and record["reasoning_tokens"] is not None
        )

    def add(
        self,
        usage: Usage,
        iteration: int,
        *,
        estimated: bool = False,
        served_by: str | None = None,
        inference_geo: str | None = None,
        usage_status: str = "reported",
        started_at: datetime | None = None,
        ended_at: datetime | None = None,
    ) -> None:
        if not self.from_iteration:
            self.from_iteration = iteration
        self.to_iteration = iteration
        self.usage.input_tokens += usage.input_tokens
        self.usage.output_tokens += usage.output_tokens
        self.usage.cache_read_tokens += usage.cache_read_tokens
        self.usage.cache_write_tokens += usage.cache_write_tokens
        self.cost_usd += self.model.cost_usd(
            usage.input_tokens,
            usage.output_tokens,
            usage.cache_read_tokens,
            usage.cache_write_tokens,
        )
        self.estimated_usage = self.estimated_usage or estimated
        if not estimated:
            self.last_reported_cache_read_tokens = usage.cache_read_tokens
        self.call_records.append(
            {
                "iteration": iteration,
                "started_at": started_at.isoformat() if started_at else None,
                "ended_at": ended_at.isoformat() if ended_at else None,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cache_read_tokens": usage.cache_read_tokens,
                "cache_write_tokens": usage.cache_write_tokens,
                "reasoning_tokens": usage.reasoning_tokens,
                "reasoning_accounting": usage.reasoning_accounting,
                "served_by": served_by,
                "inference_geo": inference_geo,
                "usage_status": usage_status,
            }
        )
        if all(record["reasoning_tokens"] is not None for record in self.call_records):
            self.usage.reasoning_tokens = sum(
                record["reasoning_tokens"] for record in self.call_records
            )
            semantics = {record["reasoning_accounting"] for record in self.call_records}
            self.usage.reasoning_accounting = (
                semantics.pop() if len(semantics) == 1 else "unknown"
            )
        else:
            self.usage.reasoning_tokens = None
            self.usage.reasoning_accounting = None

    def accounting(self, emissions: "_EmissionsContext | None" = None) -> dict:
        """`emissions` — this run's `_EmissionsContext`, when the caller has
        one in scope — lets per-call factor resolution below share its cache
        instead of re-resolving the ladder for every call record (see
        `_EmissionsContext.resolve_call_factors`'s own docstring). `None` (the
        SDK's own accounting path, and any caller with no run context) falls
        back to the plain, uncached `factor_set_for_call` — correct either
        way, just not memoised.
        """
        if (
            self.meter_reading is None
            and self.call_records
            and self.model.provider != "local"
        ):
            from tret.services.emission_calls import account_call_records
            from tret.services.emission_factors import build_factor_set

            per_call = account_call_records(
                self.model, self.call_records, billed_usage=self.usage,
                factors=self.factors or build_factor_set(
                    provider=self.model.provider, model_id=self.model.id
                ),
                resolve_call_factors=(
                    emissions.resolve_call_factors if emissions is not None else None
                ),
            )
            if per_call is not None:
                return per_call
        accounting = energy_accounting(
            self.model,
            self.usage.input_tokens,
            self.usage.output_tokens,
            self.usage.cache_read_tokens,
            self.usage.cache_write_tokens,
            factors=self.factors,
            measured_energy_wh=(
                float(self.meter_reading.wh) if self.meter_reading is not None else None
            ),
            measured_energy_boundary=(
                self.meter_reading.energy_boundary if self.meter_reading is not None else "node_it"
            ),
            energy_output_tokens=(
                self.usage.output_tokens + self.known_additional_reasoning_tokens
            ),
            reasoning_tokens=self.usage.reasoning_tokens,
            reasoning_accounting=self.usage.reasoning_accounting,
        )
        # Additive, on top of everything `energy_accounting` itself already
        # did with `measured_energy_wh` (energy_source: "measured", the
        # `unbatched_local_inference`/`prompt_shape_residual` caveat flips —
        # see its own docstring). This is the metering *provenance* —
        # which meter, how many samples, over how long — that a run-level
        # library call has no way to know about on its own.
        if self.meter_describe is not None:
            accounting["energy_meter"] = dict(self.meter_describe)
        if self.meter_reading is not None:
            reading = self.meter_reading
            accounting["energy_meter"] = {
                **(accounting.get("energy_meter") or {}),
                "kind": reading.kind,
                "energy_boundary": reading.energy_boundary,
                "samples": reading.samples,
                "duration_s": reading.duration_s,
                "interval_s": (self.meter_describe or {}).get("interval_s"),
                "shared_device": reading.shared_device,
                "note": reading.note,
                **reading.describe(),
            }
            if reading.shared_device:
                accounting["caveats"] = [
                    *accounting["caveats"],
                    {
                        "key": "shared_device_measurement",
                        "label": "Measured energy includes shared-device work",
                        "direction": "overstates",
                        "applies": True,
                        "note": (
                            "The meter includes other work sharing the measured devices; "
                            "it does not isolate this run's energy."
                        ),
                    },
                ]
        return accounting

    def to_json(self, emissions: "_EmissionsContext | None" = None) -> dict:
        accounting = self.accounting(emissions)
        return {
            "model": self.model.id,
            "provider": self.model.provider,
            "from_iteration": self.from_iteration,
            "to_iteration": self.to_iteration,
            "reason": self.reason,
            "input_tokens": self.usage.input_tokens,
            "output_tokens": self.usage.output_tokens,
            "cache_read_tokens": self.usage.cache_read_tokens,
            "cache_write_tokens": self.usage.cache_write_tokens,
            "reasoning_tokens": self.usage.reasoning_tokens,
            "reasoning_accounting": self.usage.reasoning_accounting,
            "known_additional_reasoning_tokens": self.known_additional_reasoning_tokens,
            "cost_usd": float(self.cost_usd),
            "effort": self.effort,
            "effort_history": list(self.effort_history),
            "served_by": self.served_by,
            "call_records": list(self.call_records),
            "cache_rebuilds_expected": self.cache_rebuilds_expected,
            "cache_misses_unexpected": self.cache_misses_unexpected,
            "energy_wh": accounting["energy_wh"],
            # The full per-model derivation, kept segment by segment. The
            # run-level roll-up nulls whatever the segments disagreed on, so this
            # is where the un-nulled detail survives.
            "energy_accounting": accounting,
            # See `estimated_usage`'s docstring: True if any turn folded into
            # this segment was priced from an estimate rather than a metered
            # figure.
            "estimated": self.estimated_usage,
        }


def _combine_segments(
    segments: list[ModelSegment], emissions: "_EmissionsContext | None" = None
) -> dict:
    """Roll up segment accounting while retaining meter-attempt quality.

    `emissions`, when given, is threaded into each segment's own
    `accounting()` so per-call factor resolution shares this run's cache
    (see `ModelSegment.accounting` / `_EmissionsContext.resolve_call_factors`)
    instead of re-resolving the ladder from scratch on every call — this
    function is exactly the recompute `_book_usage` runs once per turn.
    """
    blocks = [segment.accounting(emissions) for segment in segments]
    combined = combine_accountings(blocks)
    assert combined is not None
    attempts = [block["energy_meter"] for block in blocks if block.get("energy_meter")]
    if attempts:
        combined["energy_meter_attempts"] = attempts
        if len(attempts) == 1:
            combined["energy_meter"] = {
                **(combined.get("energy_meter") or {}),
                **attempts[0],
            }
        else:
            statuses = {attempt.get("status") for attempt in attempts}
            reasons = [attempt.get("attempt_reason") for attempt in attempts]
            combined["energy_meter"] = {
                **(combined.get("energy_meter") or {}),
                "status": statuses.pop() if len(statuses) == 1 else "mixed",
                "complete": all(attempt.get("complete") is True for attempt in attempts),
                "attempt_reason": next(
                    (reason for reason in reasons if reason is not None), None
                ),
            }
    return combined


@dataclass
class _EmissionsContext:
    """One `execute()` call's workspace/managed emissions-override documents,
    already parsed, and the `FactorSet`s already resolved from them this run.

    Replaces what used to be `HarnessEngine._emissions_workspace_doc` /
    `_emissions_managed_doc` instance attributes. `HarnessEngine` is
    process-wide and `execute()` runs concurrently per run (see its own
    docstring for the pre-existing `self.registry`/`self.router` race this
    does NOT fix); storing per-run documents on `self` meant workspace A's
    overrides and operator labels could land in workspace B's persisted
    accounting under concurrent runs. This object is created fresh in
    `execute()` and threaded explicitly through `_execute_inner` and every
    place that needs a `FactorSet` — `run`/`db` are threaded the same way, for
    the same reason — so no per-run emissions state ever lives on the engine
    instance.

    `workspace_doc`/`managed_doc` are `EmissionsOverrides` instances (or
    `None`), parsed once here rather than once per `_factors_for` call (B1: a
    run with several model segments/switches used to re-validate the same two
    documents — including re-parsing every `grid.tables` CSV entry's shape —
    on every single one). `build_factor_set` accepts either a raw dict or an
    already-validated instance for exactly this reason.

    `broken` is set when either document failed to parse (a downgrade, a
    hand-edited row) — `_factors_for` treats that the same way a raised
    `build_factor_set` call used to: `factors=None` for the whole run, not
    just the layer that broke, matching `build_factor_set`'s own behaviour of
    raising on the first invalid document it reaches rather than skipping it.
    """

    workspace_doc: "EmissionsOverrides | None" = None
    managed_doc: "EmissionsOverrides | None" = None
    broken: bool = False
    # This run's start time, timezone-aware — what an hourly `grid.tables`
    # entry is looked up against (see `emission_factors._apply_grid_table`).
    # Constant for the whole run (every segment/switch/compaction call
    # shares it), so it lives here rather than being recomputed per
    # `_factors_for` call — same reasoning as `workspace_doc`/`managed_doc`.
    at: datetime = field(default_factory=_utcnow)
    # Keyed on `(provider, model_id)`: a `model_overrides` entry is resolved
    # per model id (see `emission_settings.factor_set_for`), so two segments
    # sharing a provider but running different models must never share a
    # cached `FactorSet`. `at` is not part of this key — it never varies
    # within one run, unlike the what-if endpoint's own `fs_cache`, which
    # spans many runs at many different times.
    _factor_sets: dict[tuple[str | None, str | None], "FactorSet | None"] = field(
        default_factory=dict, repr=False
    )
    # The segment whose meter is currently running, if any — set by
    # `_start_meter` and cleared by `_stop_meter` once it has stopped it.
    # `execute()`'s own `finally` reads this to stop whatever segment was
    # live when `_execute_inner` raised or its task was cancelled, without
    # `_execute_inner` needing its own try/finally around the run loop.
    current_segment: "ModelSegment | None" = field(default=None, repr=False)
    # Retained so execute()'s outer finally can persist final meter diagnostics
    # even when an engine exception or cancellation skips the normal finish
    # path inside `_execute_inner`.
    segments: list["ModelSegment"] = field(default_factory=list, repr=False)
    # Per-call `FactorSet` resolutions (`services/emission_calls.
    # account_call_records`'s `resolve_call_factors` hook), keyed by
    # (provider, model_id, served_by, hour bucket of `at`, id of the
    # segment-level factors' own `resolution_context`). `account_call_records`
    # used to call `factor_set_for_call` -> `build_factor_set` once per call
    # record on *every* turn — turn k re-resolving all k calls, ~5,000
    # resolutions on a 100-turn run, each re-parsing the override documents.
    # Scoped to this run (not `HarnessEngine`, which is process-wide and
    # shared across concurrent runs — see this class's own docstring) so nothing
    # here ever crosses from one workspace's run into another's.
    _call_factor_sets: dict[tuple, "FactorSet | None"] = field(
        default_factory=dict, repr=False
    )

    def resolve_call_factors(
        self,
        factors: "FactorSet",
        *,
        provider: str | None,
        model_id: str | None,
        served_by: str | None,
        at: datetime | None = None,
        interval_end: datetime | None = None,
    ) -> "FactorSet | None":
        """Cached, never-raising stand-in for `factor_set_for_call`.

        Same signature (`account_call_records` calls this exactly the way it
        would call `factor_set_for_call` directly), so it can replace it as
        the resolver for every call record in a segment without changing the
        loop that walks them. `interval_end` is passed through to the actual
        resolution but, deliberately, not part of the cache key — two calls a
        few seconds apart within the same hour bucket share a resolution
        rather than each re-parsing the override documents for a difference
        an hourly grid table cannot see anyway.

        A raising `build_factor_set` (a hand-edited override document, same
        failure `_factors_for` above already guards against) is logged once
        and falls back to the segment-level `factors` already resolved for
        this call's segment — the accounting estimate path, never a raise
        into the turn that is trying to book its usage.
        """
        from tret.services.emission_factors import factor_set_for_call

        hour = at.replace(minute=0, second=0, microsecond=0) if at is not None else None
        key = (provider, model_id, served_by, hour, id(factors.resolution_context))
        if key in self._call_factor_sets:
            return self._call_factor_sets[key]
        try:
            resolved = factor_set_for_call(
                factors, provider=provider, model_id=model_id, served_by=served_by,
                at=at, interval_end=interval_end,
            )
        except Exception:
            log.exception(
                "failed to resolve per-call emissions factors for provider %r; "
                "falling back to this segment's own factor set", provider,
            )
            resolved = factors
        self._call_factor_sets[key] = resolved
        return resolved


class HarnessEngine:
    def __init__(
        self,
        registry: ProviderRegistry | None = None,
        catalog: ModelCatalog | None = None,
        priors: PriorsProvider | None = None,
    ):
        self.catalog = catalog or get_catalog()
        self.registry = registry or ProviderRegistry()
        # Held on the engine, not rebuilt per run, because its whole value is the
        # short-lived cache: a burst of runs against the same harness asks the
        # same question, and the aggregate does not move between them.
        self.priors = priors or OutcomePriors()
        self.router = ModelRouter(self.catalog, self.registry, self.priors)
        self.bus = get_event_bus()
        self._cancelled: set[uuid.UUID] = set()
        # In-process delegation lineage: child run id -> parent run id. A child
        # run's `parent_run_id` column (db/models.py) is the persisted record of
        # this, but cancellation needs to walk the chain in-process without a
        # DB round trip per hop, so `run_harness_task` (engine/tools.py)
        # registers a child here before awaiting its `execute()` and
        # unregisters it after (see the module this dict is read from:
        # `_is_cancelled` and `cancel` below). Bounded by construction: a run
        # appears here for exactly the lifetime of the `run_harness_task` call
        # that created it.
        self._parent_of: dict[uuid.UUID, uuid.UUID] = {}

    def register_delegation(self, *, child_id: uuid.UUID, parent_id: uuid.UUID) -> None:
        self._parent_of[child_id] = parent_id

    def unregister_delegation(self, child_id: uuid.UUID) -> None:
        self._parent_of.pop(child_id, None)

    def _factors_for(
        self, provider: str, model_id: str | None, emissions: "_EmissionsContext"
    ) -> "FactorSet | None":
        """The layered factor set a model on `provider` gets under this run's
        workspace/managed documents (`emissions`, built once in `execute()` and
        threaded down rather than read off `self` — see `_EmissionsContext`).

        `model_id` is what a `model_overrides` entry is resolved against — see
        `emission_settings.factor_set_for` — so it is also part of the cache
        key: two segments on the same provider but different models must never
        share a resolved `FactorSet`.

        Never raises: `emissions.workspace_doc`/`managed_doc` are already
        `EmissionsOverrides` instances (or `None`) parsed once at context
        creation, so `factor_set_for` here is pure arithmetic, never
        validation — but a document that failed to parse at context
        creation (a downgrade, a hand-edited row) still must not turn into a
        run that cannot account its own energy at all, so `emissions.broken`
        (set once, at parse time) short-circuits straight to `None` here,
        exactly like a raised `factor_set_for` call used to.
        """
        key = (provider, model_id)
        if key in emissions._factor_sets:
            return emissions._factor_sets[key]
        if emissions.broken:
            factors = None
        else:
            try:
                factors = factor_set_for(
                    provider,
                    workspace_doc=emissions.workspace_doc,
                    managed_doc=emissions.managed_doc,
                    model_id=model_id,
                    at=emissions.at,
                )
            except Exception:
                log.exception(
                    "failed to resolve emissions factors for provider %r; "
                    "accounting for this segment with no configured layers",
                    provider,
                )
                factors = None
        emissions._factor_sets[key] = factors
        return factors

    async def _start_meter(
        self, segment: ModelSegment, settings, emissions: "_EmissionsContext"
    ) -> None:
        """Start measuring `segment`, if — and only if — its model is on a
        local deployment and a meter is configured. A cloud segment never
        even calls `meter_for_settings`: `deployment_for` is checked first,
        so a run with no local model in it costs nothing here, and a spy
        patched onto `meter_for_settings` in a test never sees a call for a
        cloud-only run.

        Never raises and never leaves `segment.meter` set to something that
        failed to start: any exception constructing or starting the meter is
        logged and swallowed, and the segment falls back to the ordinary
        per-token estimate exactly as if metering were off.
        """
        if deployment_for(segment.model.provider) != DEPLOYMENT_LOCAL:
            return
        try:
            meter = meter_for_settings(settings)
        except Exception as exc:
            log.exception(
                "failed to construct an energy meter for a local segment (%s); "
                "this segment will fall back to the per-token estimate",
                segment.model.id,
            )
            segment.meter_describe = {
                **(segment.meter_describe or {}),
                "status": "error",
                "complete": False,
                "attempt_reason": f"{type(exc).__name__}: {exc}",
            }
            return
        if meter is None:
            return
        if isinstance(meter, (NvidiaSmiMeter, NvmlEnergyMeter)):
            gpu_index, interval_s = meter.gpu_index, meter.interval_s

            def _new_nvidia_meter():
                candidate = meter_for_settings(settings)
                if not isinstance(candidate, (NvidiaSmiMeter, NvmlEnergyMeter)):
                    raise RuntimeError("nvidia_smi meter settings changed during collection")
                return candidate

            meter = collected_meter(
                _new_nvidia_meter,
                key=(type(meter).__name__, gpu_index, interval_s),
            )
        segment.meter_describe = meter.describe()
        try:
            interval_s = (segment.meter_describe or {}).get("interval_s") or 1.0
            await asyncio.wait_for(meter.start(), timeout=max(2.0, interval_s * 2))
        except Exception as exc:
            log.exception(
                "energy meter failed to start for a local segment (%s); falling back "
                "to the per-token estimate",
                segment.model.id,
            )
            segment.meter_describe = {
                **(segment.meter_describe or {}),
                "status": "error",
                "complete": False,
                "attempt_reason": f"{type(exc).__name__}: {exc}",
            }
            return
        segment.meter = meter
        # Recorded so `execute()`'s own `finally` can stop this segment on an
        # exception or cancellation out of `_execute_inner`'s run loop, without
        # that loop needing its own try/finally around it.
        emissions.current_segment = segment

    async def _stop_meter(self, segment: ModelSegment, emissions: "_EmissionsContext") -> None:
        """Stop `segment`'s meter, if it has one running, and record what it
        read. A no-op on a segment that was never metered (`segment.meter is
        None`) — cloud segments, and local segments with metering off, both
        take this path. A meter that raises while stopping is logged and
        treated the same as one that returns `None`: this segment keeps its
        per-token estimate rather than lose the run over a metering failure.
        """
        meter, segment.meter = segment.meter, None
        if meter is None:
            return
        # Bounded: a meter must never hold the run up waiting for it. Twice
        # the sampling interval (floor 2s) is generous for what `stop()`
        # actually has to do — cancel a background task and, at most, take
        # one more sample — while still catching a meter that hangs instead
        # of returning.
        interval_s = (segment.meter_describe or {}).get("interval_s") or 1.0
        timeout_s = max(2.0, interval_s * 2)
        try:
            reading = await asyncio.wait_for(meter.stop(), timeout=timeout_s)
        except Exception as exc:
            log.exception(
                "energy meter failed to stop for a local segment (%s); falling back "
                "to the per-token estimate",
                segment.model.id,
            )
            reading = None
            segment.meter_describe = {
                **(segment.meter_describe or {}),
                "status": "error",
                "complete": False,
                "attempt_reason": f"{type(exc).__name__}: {exc}",
            }
        else:
            # Some meters learn coverage/status only while stopping. Refresh
            # the captured description before dropping the meter object.
            try:
                refreshed = meter.describe()
            except Exception:
                refreshed = {}
            segment.meter_describe = {**(segment.meter_describe or {}), **refreshed}
        if reading is not None and reading.complete:
            segment.meter_reading = reading
            segment.meter_describe = {
                **(segment.meter_describe or {}),
                **reading.describe(),
                "attempt_reason": None,
            }
        elif reading is not None:
            segment.meter_describe = {
                **(segment.meter_describe or {}),
                **reading.describe(),
                "attempt_reason": "incomplete_coverage",
            }
        elif (segment.meter_describe or {}).get("status") == "error":
            # Already fully described by the `except` handler above (its own
            # "error" status and exception-derived `attempt_reason`) — leave
            # it exactly as set rather than rewrite it below.
            pass
        elif (segment.meter_describe or {}).get("status") not in (None, ""):
            # Any real collector/meter status — "incomplete",
            # "unallocated_concurrency", "incomplete_claim_interval",
            # "meter_failure", "meter_start_failure", "collector_unavailable",
            # and anything else `CollectedMeter.describe()` (services/
            # energy_collector.py) or a plain meter's own `describe()` emits —
            # is passed through as-is rather than rewritten. Losing the
            # collector's own diagnosis here is exactly what erased the A6
            # detail the accounting record needs when metering degrades.
            segment.meter_describe = {
                **(segment.meter_describe or {}),
                "attempt_reason": "incomplete_coverage",
            }
        else:
            # Genuinely no status to report — the meter never produced one at
            # all (nothing above set `segment.meter_describe["status"]`).
            # This is the only case "unavailable" still applies.
            segment.meter_describe = {
                **(segment.meter_describe or {}),
                "status": "unavailable",
                "complete": False,
                "attempt_reason": "meter_unavailable",
            }
        # This segment is no longer the one `execute()`'s `finally` needs to
        # stop on its way out — guarded by identity so a stale call (this
        # segment was already superseded by a later `_start_meter`) never
        # clobbers the segment that actually is current.
        if emissions.current_segment is segment:
            emissions.current_segment = None

    def _is_cancelled(self, run_id: uuid.UUID) -> bool:
        """True if `run_id`, or any run it was delegated from, is cancelled.

        Checked at the top of every loop iteration instead of a bare membership
        test in `self._cancelled`, so cancelling a parent stops a run several
        delegation hops down even when `cancel()` could not have marked it
        directly yet — e.g. a grandchild registered *after* its ancestor was
        already cancelled, because the ancestor's own loop had not reached its
        `run_harness_task` call at cancel-time. `seen` guards against a cycle in
        `_parent_of` turning a bug elsewhere into an infinite loop here.
        """
        current: uuid.UUID | None = run_id
        seen: set[uuid.UUID] = set()
        while current is not None and current not in seen:
            if current in self._cancelled:
                return True
            seen.add(current)
            current = self._parent_of.get(current)
        return False

    def _descendants_of(self, run_id: uuid.UUID) -> set[uuid.UUID]:
        children = {child for child, parent in self._parent_of.items() if parent == run_id}
        descendants = set(children)
        for child in children:
            descendants |= self._descendants_of(child)
        return descendants

    def cancel(self, run_id: uuid.UUID) -> None:
        """Cancel `run_id` and every run currently delegated from it.

        Delegation runs a child's whole agent loop inline, inside the parent's
        own tool-call step (`run_harness_task`), so a parent a user cancels
        mid-delegation must not leave its child looping to completion unattended
        — that child's cost and side effects belong to the operator who just
        asked for this run to stop, whether or not they know the child's run id.
        Marking every *currently registered* descendant here is the proactive
        half of the contract; `_is_cancelled` above is the half that still
        catches a descendant registered a moment later.
        """
        self._cancelled.add(run_id)
        self._cancelled |= self._descendants_of(run_id)

    def forget_cancelled(self, run_id: uuid.UUID) -> None:
        """Drop `run_id` from the cancelled set. `execute()` does this for every
        run it finishes; this is for a delegated child that was cancelled
        before `execute()` ever ran for it (engine/tools.py::
        `_close_out_abandoned_child`), whose id would otherwise sit here for the
        life of the process."""
        self._cancelled.discard(run_id)

    async def execute(
        self, run_id: uuid.UUID, *, _emissions_test_hook=None
    ) -> None:
        """`_emissions_test_hook`, if given, is awaited once per call, right
        after this run's `_EmissionsContext` is built and before
        `_execute_inner` reads it. Test-only: it exists so a concurrency test
        can deterministically interleave two `execute()` calls sharing one
        engine instance between "documents loaded" and "first segment built",
        the narrowest window where the old `self._emissions_workspace_doc` /
        `_emissions_managed_doc` instance attributes could bleed across runs
        (see `_EmissionsContext`). No production caller passes it.
        """
        async with get_session_factory()() as db:
            run = await db.get(Run, run_id)
            if run is None:
                return
            # Fetched early so DB-key loading below can scope to this run's own
            # workspace, and so the crash handler has it without a second round
            # trip. `_execute_inner` fetches it again — free, same session
            # identity map — because it needs `harness` as a local regardless of
            # how `execute` got here.
            harness = await db.get(Harness, run.harness_id)
            # Captured as a plain value, not read off `harness` again later: the
            # rollback below expires every instance in the session, and a
            # post-rollback attribute access would need to lazy-load it with no
            # greenlet context to do that in.
            workspace_id = harness.workspace_id if harness else None
            # Rebuild registry/router per run so DB-stored keys (settings UI)
            # are honored alongside env keys. Scoped to this run's workspace so
            # one workspace's stored key is never handed to another's run.
            from tret.services.credentials import load_db_keys

            self.registry = ProviderRegistry(await load_db_keys(db, workspace_id))
            self.router = ModelRouter(self.catalog, self.registry, self.priors)
            # The two non-run_override layers of the emissions factor ladder
            # (tret/services/emission_factors.py) — a workspace's own override
            # document and whatever a loaded extension's managed layer
            # contributes — loaded once per run, same as registry/router just
            # above, and turned into a `FactorSet` per provider as each model
            # segment starts (`_factors_for`) rather than once here, since a
            # run may use more than one provider (model_timeline). Loading
            # either document is not this run's business to fail on: a broken
            # workspace row or a raising managed-layer extension must fall
            # back to today's behaviour (`factors=None`) rather than take the
            # run down.
            try:
                emissions_workspace_doc, emissions_managed_doc = (
                    await workspace_emissions_layers(db, workspace_id)
                )
            except Exception:
                log.exception(
                    "failed to load emissions factor layers for workspace %s; "
                    "run will account with no configured layers",
                    workspace_id,
                )
                emissions_workspace_doc, emissions_managed_doc = None, None
            # Parsed once here, not once per `_factors_for` call (B1) — see
            # `_EmissionsContext`'s docstring. A document that fails to
            # validate (a downgrade, a hand-edited row) sets `emissions_broken`
            # rather than raising: `_factors_for` gives the run `factors=None`
            # for exactly that reason, the same fallback a raised
            # `build_factor_set` call used to produce.
            from tret.services.emission_factors import EmissionsOverrides

            emissions_broken = False
            workspace_instance = None
            if emissions_workspace_doc:
                try:
                    workspace_instance = EmissionsOverrides(**emissions_workspace_doc)
                except Exception:
                    log.exception(
                        "workspace %s: stored emissions override document no longer "
                        "validates; run will account with no configured layers",
                        workspace_id,
                    )
                    emissions_broken = True
            managed_instance = None
            if emissions_managed_doc:
                try:
                    managed_instance = EmissionsOverrides(**emissions_managed_doc)
                except Exception:
                    log.exception(
                        "workspace %s: managed emissions layer document does not "
                        "validate; run will account with no configured layers",
                        workspace_id,
                    )
                    emissions_broken = True
            # Built fresh per call and threaded down explicitly (never held on
            # `self`) — see `_EmissionsContext`'s docstring for why: this
            # engine instance is process-wide and `execute()` runs concurrently
            # per run.
            # `run.created_at` is a `TIMESTAMP(timezone=True)` column, so a
            # freshly loaded row already carries a timezone-aware value. A
            # naive one — only ever a hand-built `Run` a test constructs
            # without going through the DB round trip, or a SQLite install,
            # where the same column round-trips naive (see reconcile.py's own
            # `_as_aware_utc`) — is treated as already being UTC, the same as
            # the what-if endpoint's `_aware_utc` treats a naive stored
            # `created_at`; it is never silently substituted with "now",
            # which would price an hourly `grid.tables` lookup (if any)
            # against the wrong hour entirely. Only a genuinely absent
            # `created_at` (never happens outside a hand-built `Run`; the
            # column is NOT NULL) falls back to `_utcnow()`.
            if run.created_at is None:
                run_started_at = _utcnow()
            elif run.created_at.tzinfo is None:
                run_started_at = run.created_at.replace(tzinfo=timezone.utc)
            else:
                run_started_at = run.created_at
            emissions = _EmissionsContext(
                workspace_instance, managed_instance, broken=emissions_broken, at=run_started_at
            )
            if _emissions_test_hook is not None:
                await _emissions_test_hook()
            try:
                await self._execute_inner(db, run, emissions)
            except Exception as e:  # engine bug or provider hard failure
                # Discard whatever the failed iteration left uncommitted before
                # recording the failure: a run marked `failed` must not also
                # persist a finding nobody was ever told about. Everything up to
                # the last end-of-iteration commit survives, so the partial
                # transcript the audit view relies on is untouched. rollback()
                # expires the instance, so re-load it before writing.
                await db.rollback()
                run = await db.get(Run, run_id)
                if run is None:  # pragma: no cover - row deleted mid-run
                    return
                run.status = "failed"
                run.error = f"{type(e).__name__}: {e}"
                run.finished_at = _utcnow()
                await db.commit()
                await self.bus.publish(run_id, RunEvent("error", {"message": run.error}))
                # A crashed run may still have accumulated cost — an extension
                # tracking spend needs to see it even though the run never
                # reached the normal finish path.
                await get_extension_registry().run_post_run_hooks(db, run, workspace_id)
            finally:
                # `_execute_inner`'s run loop no longer wraps itself in its own
                # try/finally for this: `_start_meter` records whichever
                # segment is live on `emissions.current_segment`, so an
                # exception above (already caught by the `except` above it)
                # or a cancellation of this task (`CancelledError`, a
                # `BaseException` the `except Exception` above never sees)
                # both still reach here, and this stops that segment's meter
                # on their way out — otherwise its background sampling task
                # (tret/services/energy_meter.py) would outlive the run,
                # forking a subprocess forever. `_stop_meter` is idempotent
                # (a no-op on a segment already stopped), so this never
                # conflicts with the stop `_execute_inner` already did on its
                # own normal-completion path.
                # Guarded: `_combine_segments` (its own `assert`) and
                # `db.commit()` used to run unguarded here, so a bad segment
                # or a commit error after a failed run skipped straight past
                # `self._cancelled.discard(run_id)` below and leaked the id —
                # every later `cancel()` on a *different* run sharing no
                # state with this one would then see it still "cancelled".
                # This accounting finalize is best-effort, exactly like the
                # `_stop_meter` it wraps: never let it stop the run's id from
                # being released.
                try:
                    try:
                        seg = emissions.current_segment
                        if seg is not None:
                            await self._stop_meter(seg, emissions)
                        if emissions.segments and any(
                            item.meter_describe is not None for item in emissions.segments
                        ):
                            accounting = _combine_segments(emissions.segments, emissions)
                            run.energy_wh = Decimal(str(accounting["energy_wh"]))
                            run.energy_accounting = accounting
                            run.model_timeline = [
                                item.to_json(emissions) for item in emissions.segments
                            ]
                            await db.commit()
                    except Exception:
                        log.exception(
                            "failed to finalize energy accounting while stopping run %s's "
                            "meter; the run's own status/result is unaffected, but this "
                            "run's measured energy may be missing from the persisted record",
                            run_id,
                        )
                        try:
                            await db.rollback()
                        except Exception:
                            log.exception(
                                "rollback after a failed accounting finalize also failed "
                                "for run %s", run_id,
                            )
                finally:
                    # Every path out of this method — the normal finish inside
                    # `_execute_inner`, `_fail_before_start`'s early return from it
                    # (still inside the `try` above, since it never raises), and
                    # the crash handler just above — reaches this exactly once.
                    # Before this `finally` existed, only the normal finish path
                    # discarded `run.id` (see `_execute_inner`'s own comment further
                    # down): a run cancelled while it was, say, failing a pre-flight
                    # check would leave its id sitting in `_cancelled` forever, with
                    # no later code path ever reaching back to clean it up.
                    #
                    # This is a nested `finally`, not the `except Exception`
                    # above it: a `CancelledError` raised while awaiting
                    # inside the guarded block is a `BaseException` that
                    # `except Exception` never sees, and it must still
                    # propagate — but only after releasing this run's id,
                    # never leaking it for a later, unrelated `cancel()` to
                    # find still marked cancelled.
                    self._cancelled.discard(run_id)

    async def _execute_inner(self, db, run: Run, emissions: "_EmissionsContext") -> None:
        harness = await db.get(Harness, run.harness_id)
        pack = await db.get(Pack, run.pack_id) if run.pack_id else None

        # Extension seam: a loaded extension (the proprietary billing package,
        # today) may veto a run before it spends anything — insufficient
        # credits, a suspended workspace. No-op with no extensions loaded (see
        # engine/extensions.py). Checked before any provider work, same as the
        # unknown-task-type and unknown-tool refusals below.
        gate = await get_extension_registry().check_pre_run(db, run, harness.workspace_id)
        if not gate.allowed:
            # No `workspace_id` here, deliberately: the gate itself is what
            # would place a hold, and a run it refuses never got one — there is
            # nothing for a post-run hook to release. See `_fail_before_start`.
            await self._fail_before_start(
                db, run, f"{gate.reason}: {gate.detail}" if gate.detail else gate.reason
            )
            return

        documents = []
        if run.document_ids:
            documents = (
                (await db.execute(select(Document).where(Document.id.in_(run.document_ids))))
                .scalars()
                .all()
            )

        task = task_config(pack, run.task_type)
        if task is None:
            if run.task_type not in GENERIC_TASK_TYPES:
                # A task type nobody declared has no instructions, no output
                # schema and no terminal tool. The engine used to invent a
                # freeform config for it, so a typo'd or uninstalled task type
                # burned a full run and reported `completed` — a status that
                # claimed the requested task had been done. Refuse before the
                # first token instead.
                declared = sorted(
                    t["slug"] for t in (pack.manifest.get("task_types", []) if pack else [])
                )
                await self._fail_before_start(
                    db,
                    run,
                    f"unknown_task_type: '{run.task_type}' is not declared by this run's pack "
                    f"(declared: {declared or 'none'}; the engine's own task types are "
                    f"{list(GENERIC_TASK_TYPES)})",
                    workspace_id=harness.workspace_id,
                )
                return
            task = {}
        output_schemas: dict[str, dict] = (pack.manifest.get("schemas", {}) if pack else {})

        loop_cfg = {**(harness.loop_config or {})}
        requested_iterations = int(loop_cfg.get("max_iterations", DEFAULT_MAX_ITERATIONS))
        max_iterations = max(1, min(requested_iterations, MAX_ITERATIONS_CEILING))
        max_output_tokens = int(loop_cfg.get("max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS))
        temperature = float(loop_cfg.get("temperature", DEFAULT_TEMPERATURE))
        max_cost = _effective_max_cost(
            Decimal(str(loop_cfg.get("max_cost_usd", DEFAULT_MAX_COST_USD))), run.task_input
        )
        model_policy = effective_model_policy(harness.model_policy, run.task_input)
        budget_raw = model_policy.get("max_run_output_tokens")
        output_budget = int(budget_raw) if budget_raw else 0
        # Opt-out only — see `_budget_line`'s own docstring for what this adds
        # and why it defaults on. `docs/architecture.md`'s loop paragraph
        # documents the key.
        budget_line_enabled = bool(loop_cfg.get("budget_line", BUDGET_LINE_DEFAULT))

        # ── tools ────────────────────────────────────────────────────────────
        builtins = get_builtin_tools()
        enabled_names = list(task.get("tools") or harness.tool_names or [])
        if run.task_type == "freeform" and not enabled_names:
            enabled_names = [
                "read_document",
                "search_documents",
                "lookup_dataset",
                "list_prior_findings",
            ]
        # A name with no builtin behind it is refused before the first token,
        # never quietly dropped. `api/harnesses.py` and `packs/loader.py` both
        # reject unknown names on write, so reaching this means a row predating
        # that validation (or a tool removed from the engine since it was
        # written) — and running anyway would execute a harness stripped of a
        # capability its author declared, while reporting `completed`. The same
        # argument, and the same handling, as an unknown task type above.
        unknown_tools = [n for n in enabled_names if n not in builtins]
        if unknown_tools:
            await self._fail_before_start(
                db,
                run,
                f"unknown_tool: {sorted(set(unknown_tools))} — this run's tool list names "
                f"tool(s) the engine has no builtin for (available: {sorted(builtins)}). "
                "Fix the harness's tool_names (or the pack task's `tools`) rather than "
                "running without them.",
                workspace_id=harness.workspace_id,
            )
            return
        # Registered, but withheld. `web_search`/`fetch_url` are always in the
        # registry — "read one file to see everything an agent can do" stays true
        # only if the registry is complete — while whether they are *available*
        # is an operator switch (TRET_EGRESS_RESEARCH). The two are separate
        # questions, so this is a filter here rather than a hole in the registry:
        # removing them from `get_builtin_tools()` would turn every harness that
        # lists one into an `unknown_tool` failure above, which is the wrong
        # answer — the harness is fine, the deployment is offline. Withheld
        # rather than silent, because a run that quietly lost a capability its
        # author declared is the failure mode `unknown_tool` exists to prevent.
        withheld = withheld_web_tools(enabled_names)
        if withheld:
            enabled_names = [n for n in enabled_names if n not in withheld]
            await self.bus.publish(
                run.id,
                RunEvent(
                    "tools_withheld",
                    {
                        "tools": sorted(set(withheld)),
                        "reason": "egress_research_disabled",
                        "detail": (
                            "Web access is off for this deployment "
                            "(TRET_EGRESS_RESEARCH). These tools were not offered to "
                            "the model; the run continues without them."
                        ),
                    },
                ),
            )
        # Connected-source tools (list_connected_sources/search_connected_files/
        # read_connected_file) are withheld the same way, but per-workspace
        # rather than per-deployment: the connection has to exist and still be
        # usable. Resolved from the run's project — a run carries project_id,
        # not workspace_id directly — and reused below for RunContext, so this
        # is the one place that lookup happens. `withheld_connector_tools` does
        # a DB round-trip, so it's only called when there's a connector tool to
        # check in the first place.
        project = await db.get(Project, run.project_id)
        workspace_id = project.workspace_id if project else None
        if set(enabled_names) & (CONNECTOR_TOOL_NAMES | WRITE_CONNECTOR_TOOL_NAMES):
            withheld_connector, connector_reason = await withheld_connector_tools(
                db, workspace_id, enabled_names
            )
            if withheld_connector:
                enabled_names = [n for n in enabled_names if n not in withheld_connector]
                await self.bus.publish(
                    run.id,
                    RunEvent(
                        "tools_withheld",
                        {
                            "tools": sorted(withheld_connector),
                            "reason": "connection_unavailable",
                            "detail": connector_reason
                            or "No usable connected source for this workspace. These tools "
                            "were not offered to the model; the run continues without them.",
                        },
                    ),
                )
        # Pack lessons (services/lessons.py): on for every pack-bound run unless
        # the harness opts out, so the model can read what reviewers approved
        # and propose more. Sorted, and never duplicated if a pack task already
        # names one of the tools: the tools block sits ahead of `system` in the
        # cached prefix, so its order must be stable across processes.
        lessons_on = (
            lessons_enabled(loop_cfg) and workspace_id is not None and pack is not None
        )
        if lessons_on:
            enabled_names += [n for n in sorted(LESSON_TOOL_NAMES) if n not in enabled_names]
        else:
            # Opting out withholds the tools even when a pack task names them,
            # the same way the web and connector filters above subtract theirs.
            enabled_names = [n for n in enabled_names if n not in LESSON_TOOL_NAMES]
        tool_specs = [builtins[n] for n in enabled_names]

        # ── context, accounted ───────────────────────────────────────────────
        # Resolved once and reused below for the RunContext: `objective_of` is
        # pure over `model_policy`, but computing it twice would risk the two
        # call sites drifting if `model_policy` is ever mutated in between.
        objective = objective_of(model_policy)
        assembled = assemble_context(
            harness,
            pack,
            run.task_type,
            output_schemas,
            extra_context=run.task_input.get("_capabilities"),
            web_tools_enabled=any(n in WEB_TOOL_NAMES for n in enabled_names),
            lessons=(
                await approved_lessons(db, workspace_id, pack.slug) if lessons_on else None
            ),
            objective=objective,
        )
        system = assembled.system
        user_message = build_user_message(run, pack, documents)
        history_raw = run.task_input.get("_history") or []
        accounted = [*assembled.blocks, tool_spec_block(tool_specs)]
        if history_raw:
            # Chat turns re-send the thread; that growth belongs in the account.
            accounted.append(
                block_for(
                    "conversation_history",
                    f"{len(history_raw)} prior messages",
                    "".join(str(m.get("content") or "") for m in history_raw),
                )
            )
        accounted.append(block_for("user_message", run.task_type, user_message))
        composition = composition_report(accounted)

        # ── route ────────────────────────────────────────────────────────────
        est_input_tokens = composition["total_est_tokens"]
        # Read here rather than after routing (where the context-budget code
        # below has always read it) because `min_context_window` needs
        # `context_headroom` before a model is chosen, not after —
        # `adaptive_of` is pure over `model_policy` alone, so reading it early
        # changes nothing about what it returns later.
        adaptive = adaptive_of(model_policy)
        # The floor a model must clear is not always `est_input_tokens` itself.
        # `trim_history` (below, in the context-budget section) runs *after* a
        # model is chosen and exists specifically to shrink the
        # `conversation_history` block — so sizing the floor off the untrimmed
        # total makes a long chat's history exclude a model (a 128k-window Kimi,
        # say) that trimming would have made perfectly viable. When adaptive
        # compaction can actually run, the floor is computed off the
        # non-trimmable remainder instead: the total minus that block. With
        # compaction off there is nothing to trim it down later, so the floor
        # stays the untrimmed total, exactly as before. Either way
        # `est_input_tokens` itself — used below for the router's own
        # accounting (priors size-band, the router prompt) — still reflects
        # what was actually composed.
        if adaptive.compaction != "off":
            context_fit_basis = "prompt_without_history"
            floor_input_tokens = est_input_tokens - composition["by_kind"].get(
                "conversation_history", 0
            )
        else:
            context_fit_basis = "full_prompt"
            floor_input_tokens = est_input_tokens
        min_context_window = required_context_window(
            floor_input_tokens, max_output_tokens, adaptive.context_headroom
        )
        try:
            decision = await self.router.route(
                model_policy=model_policy,
                task_type=run.task_type,
                task_shape=task.get("shape", "freeform"),
                task_description=task.get("display_name", run.task_type),
                output_contract=task.get("output_contract", "free text"),
                n_documents=len(documents),
                est_input_tokens=est_input_tokens,
                run_override=run.task_input.get("_model_override"),
                emissions_workspace_doc=emissions.workspace_doc,
                emissions_managed_doc=emissions.managed_doc,
                emissions_at=emissions.at,
                min_context_window=min_context_window,
            )
        except RoutingUnavailable as e:
            await self._fail_before_start(db, run, str(e), workspace_id=harness.workspace_id)
            return
        if decision.context_fit is not None:
            decision.context_fit["basis"] = context_fit_basis

        model_info = self.catalog.get(decision.chosen_model)
        run.routing = decision.to_json()
        run.model_used = decision.chosen_model
        run.provider_used = model_info.provider
        run.status = "running"
        run.started_at = _utcnow()
        run.doctrine_sha = pack.doctrine_sha if pack else None
        run.context_composition = composition
        await db.commit()
        await self.bus.publish(run.id, RunEvent("routing", decision.to_json()))
        await self.bus.publish(run.id, RunEvent("context_composition", composition))

        provider = self.registry.get(model_info.provider)

        ctx = RunContext(
            db=db,
            run_id=run.id,
            project_id=run.project_id,
            pack_id=run.pack_id,
            doctrine_sha=run.doctrine_sha,
            model_used=run.model_used,
            document_ids=list(run.document_ids or []),
            workspace_id=workspace_id,
            conversation_id=run.conversation_id,
            output_schemas=output_schemas,
            pack_manifest=pack.manifest if pack else None,
            pack_dir=pack.source_path if pack else None,
            terminal_tool=task.get("terminal_tool"),
            # Clamped at zero: a negative depth would buy extra hops under
            # `MAX_DELEGATION_DEPTH`. The runs API strips this key from what a
            # caller sends (api/runs.py), but the engine does not rely on that.
            delegation_depth=max(0, int(run.task_input.get(DELEGATION_DEPTH_KEY) or 0)),
            objective=objective,
            max_cost_usd=max_cost,
        )
        # The grounding check (engine/grounding.py) only makes sense where a
        # reply's prose *is* the output: a verdict task's numbers are already
        # held to the cited-values cross-check (engine/validation.py) through
        # its terminal tool's schema, and checking the free text around that
        # too would flag reasoning prose that was never meant to be citable.
        grounding_applies = run.task_type in GENERIC_TASK_TYPES and not ctx.terminal_tool

        # ── context budget ───────────────────────────────────────────────────
        # The chosen model's window is known only now, which is why the history
        # trim below lives here rather than in api/chat.py: that endpoint hands
        # over the last N turns with no idea how large they are or which model
        # will have to hold them. `adaptive` itself was read earlier, above the
        # routing call, so `min_context_window` could be computed before a
        # model was chosen.
        context_limit = context_budget(
            model_info.context_window, max_output_tokens, adaptive.context_headroom
        )
        compaction = CompactionState()
        compaction_records: list[dict] = []
        # Model calls this run made *about itself* — choosing its model, and
        # summarizing what it had to elide. Real money and real electricity,
        # invisible until now. Kept apart from the run's own totals because they
        # ran on different models and possibly different providers; see
        # services/emissions.overhead_call for why folding them in would be
        # wrong rather than just coarse.
        overhead_calls: list[dict] = [decision.spend] if decision.spend else []
        run.overhead = overhead_block(overhead_calls)

        # One segment per model this run uses. Almost always exactly one.
        segments: list[ModelSegment] = [
            ModelSegment(
                model_info,
                reason="initial",
                factors=self._factors_for(model_info.provider, model_info.id, emissions),
                # Recorded regardless of intent — the decision always carries
                # one (see `RoutingDecision.effort`) — but only sent to the
                # provider, via `provider.stream()` below, when this model's
                # own catalog entry says it accepts the control.
                effort=decision.effort if model_info.supports_effort else None,
            )
        ]
        emissions.segments = segments
        segment = segments[0]
        # Read once per run, same as the workspace/managed emissions layers
        # above — a local segment's meter (tret/services/energy_meter.py) is
        # started here, stopped and restarted around every model switch
        # (`_switch_model`, below), and stopped one final time once the loop
        # ends, whichever way it ends (see the `_stop_meter` call right
        # before this run's status is finalized). A cloud segment never
        # starts one at all: `_start_meter` checks `deployment_for` first.
        settings = get_settings()
        await self._start_meter(segment, settings, emissions)
        # `_start_meter` records the live segment on `emissions.current_segment`;
        # `execute()`'s own `finally` around its `_execute_inner` call stops
        # that segment's meter on any exception out of the loop below (a
        # provider/engine bug) or a cancellation of this task (CancelledError,
        # a BaseException that `execute()`'s `except Exception` does not catch)
        # — so this method no longer needs its own try/finally for it.
        # `_stop_meter` is idempotent (a no-op on a segment already stopped),
        # so that stop, the explicit stop-before-switch call inside the loop,
        # and the explicit stop right after the loop below never conflict.
        # Chat turns carry prior conversation turns as history.
        history = [Msg.from_json(m) for m in run.task_input.get("_history", [])]
        if context_limit and adaptive.compaction != "off":
            history, dropped = trim_history(
                history,
                system=system,
                user_message=user_message,
                tools=tool_specs,
                limit=context_limit,
            )
            if dropped:
                compaction_records.append(
                    {
                        "kind": "history_trim",
                        "iteration": 0,
                        "dropped_history_turns": dropped,
                        "context_window": model_info.context_window,
                        "limit_est_tokens": context_limit,
                        "estimator": TOKEN_ESTIMATOR,
                    }
                )
                run.compactions = list(compaction_records)
                await self.bus.publish(
                    run.id, RunEvent("compaction", compaction_records[-1])
                )
        messages: list[Msg] = [*history, Msg(role="user", content=user_message)]
        total_usage = Usage()
        nudged = False
        budget_nudged = False
        # How many times a reply failed the grounding check (engine/
        # grounding.py), on a run where `grounding_applies`. Incremented
        # whenever the check finds unsupported numbers, whether or not a nudge
        # is actually sent for it — the attempt that meets
        # `GROUNDING_MAX_REPAIRS`, or lands on the run's last iteration with
        # no turn left for a rewrite, is kept rather than retried, so this can
        # end at the budget (or below it) with one fewer nudge actually
        # appended to the transcript. `grounding_first_unsupported` is set
        # once, the first time this happens, for
        # `run.grounding["first_unsupported"]`; `grounding_last_unsupported`
        # is overwritten on every failure, so the loop-ended-some-other-way
        # fallback below still has something to report even when the final
        # failing turn never reaches the block that normally sets
        # `run.grounding`.
        grounding_failures = 0
        grounding_first_unsupported: list[str] | None = None
        grounding_last_unsupported: list[str] | None = None
        # False until the loop evaluates its first not-tool-calls turn; see
        # that turn's own comment for what this guards in the fallback below.
        grounding_final_reply_blank = False
        seen_calls: dict[str, int] = {}  # repeated-identical-call breaker
        # Stall signals for the supervisor. Counted here rather than re-derived
        # from the transcript each iteration, because "in a row" is a property of
        # the sequence and the transcript would have to be re-scanned to see it.
        consecutive_terminal_failures = 0
        repeated_call_trips = 0
        # Cumulative, because `ctx.findings_created` is drained at the end of
        # every iteration once its events have been published — reading it in the
        # supervisor would see zero on every turn and report a productive run as
        # a stalled one.
        findings_total = 0
        compaction_exhausted = False
        switches_used = 0
        # Set once the quality trigger's effort rung has fired, so it is never
        # asked twice in one run (see supervisor.assess's Rung 1 gate) — mirrors
        # `switches_used`/`adaptive.max_switches` for the switch path.
        effort_raised = False
        # The iteration the raise happened at, and the two stall counters'
        # values at that moment — None/0/0 until a raise occurs. `_quality_
        # trigger` (engine/supervisor.py) reads these to require fresh evidence
        # after a raise rather than retriggering on the stale count that
        # caused it: without this, the very counters the raise was meant to
        # answer would force a switch on the next assess() before the raised
        # effort ever got to prove itself.
        effort_raised_at: int | None = None
        failures_at_raise = 0
        trips_at_raise = 0
        overridden = bool(decision.override)
        # Set the moment a switch is applied (below), so the very first turn
        # the new model sees is compacted even if that turn is nowhere near
        # its own window — a switch already voids the cache and re-sends the
        # whole transcript at full input price; sending it uncompacted too
        # would pay for both misses on the same turn when one pass could have
        # bundled them. Consumed (and cleared) at the top of the very next
        # iteration, whether or not that iteration turns out to need it.
        compact_before_next_turn = False
        # Set when the quality trigger's effort rung (`KIND_EFFORT`, below)
        # raises effort on an Anthropic segment, so the cache-ledger
        # classification in `_book_usage` reads that segment's next turn as an
        # expected rebuild rather than an unexplained one — a top-level effort
        # change voids Anthropic's prompt cache the same as a real switch,
        # just for the one turn that carries it rather than the rest of the
        # segment. Consumed at the top of the next iteration, same as
        # `compact_before_next_turn`.
        cache_void_pending = False
        # Set once the `provider_ignore` retry below has actually waived this
        # run's routing-decision evidence for a "no eligible provider" error.
        # Without this, the very next iteration recomputes the identical
        # ignore list from `run.routing["provider_ignore"]` (it never changes)
        # and sends it straight back into the same failure — a retry loop
        # rather than a one-time waive. Never cleared once set: the evidence
        # that made OpenRouter reject every endpoint for this model doesn't
        # stop being true partway through a run.
        provider_ignore_waived = False
        # Set the first time this run's provider answers `count_tokens` with
        # `None` (unsupported, or a real call that failed) — see
        # `EXACT_COUNT_THRESHOLD` above. A provider that cannot give an exact
        # count on one turn will not give one on the next either (it is either
        # not implemented or the same call away from an identical timeout), so
        # asking again every iteration for the rest of the run would just pay
        # the same latency for the same `None` each time.
        count_tokens_unavailable = False

        def _wire_for_provider(view: list[Msg]) -> list[Msg]:
            """`view` (a `wire_view(...)` result) with this iteration's budget
            line appended, unless the harness opted out. A closure, not a
            method, because every value it reads — `iteration`, `context_
            limit`, `total_usage`, `run.cost_usd` — is loop-local state that
            changes turn to turn and, for `context_limit`, on a model switch;
            reading them by name here rather than threading eight parameters
            through both call sites keeps those two sites to one line each.
            Called from two places in the loop below: right after every
            `wire_view(...)` call, so the line lands on the wire the same way
            regardless of whether a compaction pass ran first.
            """
            if not budget_line_enabled:
                return view
            return _append_budget_line(
                view,
                _budget_line(
                    iteration=iteration,
                    max_iterations=max_iterations,
                    cost_so_far=_spent(run),
                    max_cost=max_cost,
                    est_tokens=estimate_wire_tokens(system, view, tool_specs),
                    context_limit=context_limit,
                    output_tokens_so_far=total_usage.output_tokens,
                    output_budget=output_budget,
                ),
            )

        # ── loop ─────────────────────────────────────────────────────────────
        for iteration in range(1, max_iterations + 1):
            if self._is_cancelled(run.id):
                run.status = "cancelled"
                break

            assistant_text: list[str] = []
            tool_calls: list[ToolCall] = []
            turn: TurnComplete | None = None
            # This turn's own cache-ledger context, consumed here regardless of
            # how the turn ends up going (compacted, erroring out, or neither)
            # — an effort raise voids the cache for exactly the next turn, not
            # for however many iterations pass before one happens to book.
            voided_by_effort_this_turn = cache_void_pending
            cache_void_pending = False

            # ── stay inside the window ───────────────────────────────────────
            # Checked before every call, not after a failure: a run that exceeds
            # its window gets a provider error with nothing in the transcript
            # explaining it, and by then the turn has already been paid for.
            pre_line_view = wire_view(messages, compaction)
            wire = _wire_for_provider(pre_line_view)
            est_tokens = estimate_wire_tokens(system, wire, tool_specs)
            # `_compact`'s own `after_est_tokens` is estimated on `wire_view(messages,
            # state)` — the pre-append view, with no budget line — so `before_tokens`
            # below is estimated the same way. Using `est_tokens` (which includes this
            # iteration's line) instead would make every before/after pair overstate
            # what compaction actually elided by the line's own handful of tokens, and
            # on a harness with `budget_line` off this is simply `est_tokens` again
            # (`_wire_for_provider` is a no-op in that case, so the two views match).
            before_tokens = (
                est_tokens
                if not budget_line_enabled
                else estimate_wire_tokens(system, pre_line_view, tool_specs)
            )
            # Near the boundary, ask the provider for an exact count instead of
            # trusting chars/4 — see `EXACT_COUNT_THRESHOLD`. `wire` is what is
            # actually about to be sent, so the count is taken on it (one call,
            # not one per estimate below) and both `est_tokens` and
            # `before_tokens` are overwritten from it; `before_tokens` then
            # carries the budget line's own handful of tokens even when the
            # chars/4 path above would have excluded them, which is the same
            # order of error the estimate itself already carries. The budget
            # line already rendered above (inside `_wire_for_provider`) used
            # the chars/4 estimate for its own "context tokens" figure — it is
            # built before this exact count exists, so on a turn that crosses
            # the threshold the number shown to the model can lag by one turn.
            # Not restructured: recomputing it here would mean either a second
            # provider call (against the "at most once per iteration" budget)
            # or reordering the whole budget-line/compaction pipeline to fix a
            # cosmetic mismatch of a few thousand tokens.
            est_tokens_basis = "chars4"
            if not count_tokens_unavailable and _near_context_limit(est_tokens, context_limit):
                exact_tokens = await provider.count_tokens(
                    model=model_info.wire_id, system=system, messages=wire, tools=tool_specs
                )
                if exact_tokens is None:
                    count_tokens_unavailable = True
                else:
                    est_tokens = exact_tokens
                    before_tokens = exact_tokens
                    est_tokens_basis = "provider_count"
            forced_switch_compaction = compact_before_next_turn and adaptive.compaction != "off"
            compact_before_next_turn = False
            compaction_changed_wire = False
            over_window = adaptive.compaction != "off" and over_budget(est_tokens, context_limit)
            if over_window or forced_switch_compaction:
                if over_window:
                    await self.bus.publish(
                        run.id,
                        RunEvent(
                            "context_pressure",
                            {
                                "iteration": iteration,
                                "est_input_tokens": est_tokens,
                                "limit_est_tokens": context_limit,
                                "context_window": model_info.context_window,
                                "estimator": TOKEN_ESTIMATOR,
                                "basis": est_tokens_basis,
                            },
                        ),
                    )
                record = await self._compact(
                    run=run,
                    messages=messages,
                    state=compaction,
                    iteration=iteration,
                    before_tokens=before_tokens,
                    basis=est_tokens_basis,
                    system=system,
                    tool_specs=tool_specs,
                    terminal_tool=ctx.terminal_tool,
                    max_tier=model_policy.get("max_cost_tier") or "premium",
                    overhead=overhead_calls,
                    emissions=emissions,
                    trigger="model_switch" if forced_switch_compaction else "budget",
                )
                if record is not None:
                    compaction_records.append(record)
                    run.compactions = list(compaction_records)
                    run.overhead = overhead_block(overhead_calls)
                    await self.bus.publish(run.id, RunEvent("compaction", record))
                    # Over the budget with only protected material left. The
                    # supervisor's cue that a bigger window is the only remedy —
                    # only meaningful for the ordinary budget-triggered path. A
                    # forced `model_switch` pass being a no-op says nothing
                    # about whether the run is anywhere near its window (it can
                    # be, and usually is, nowhere close — see `_compact`'s own
                    # trigger-aware note), so it must never touch this flag:
                    # leave it exactly as the switch itself already set it.
                    if record["trigger"] == "budget":
                        compaction_exhausted = record["kind"] == "no_op"
                    # Did this pass actually change what the model is about to
                    # see? A no-op (nothing left eligible) leaves the wire
                    # exactly as it was, so a cache miss on this turn is not
                    # this pass's doing.
                    compaction_changed_wire = record["kind"] != "no_op"
                wire = _wire_for_provider(wire_view(messages, compaction))

            # Withheld once `provider_ignore_waived` is set (below): recomputing
            # the same evidence-based list every iteration after it has already
            # been shown to rule out every endpoint would just resend the list
            # that failed and fail again on the very next turn.
            provider_ignore = (
                (run.routing or {}).get("provider_ignore")
                if model_info.id == (run.routing or {}).get("chosen_model")
                and not provider_ignore_waived
                else None
            )
            call_started_at = _utcnow()
            try:
                async for event in provider.stream(
                    model=model_info.wire_id,
                    system=system,
                    messages=wire,
                    tools=tool_specs,
                    max_tokens=max_output_tokens,
                    temperature=temperature,
                    # Already gated by `supports_effort` when the segment was
                    # created (initially above, or in `_switch_model` on a
                    # mid-run switch) — reading it off the live segment here
                    # rather than the run's own `decision.effort` means both
                    # cases go through one gate instead of two.
                    effort=segment.effort,
                    # The run's own id: every call in this run's tool loop
                    # shares one session id, so OpenRouter's sticky routing
                    # keeps them on the same upstream provider instead of a
                    # cold cache on every turn. Providers without session
                    # affinity ignore it.
                    session_id=str(run.id),
                    # This decision's own poor-endpoint evidence
                    # (`RoutingDecision.provider_ignore`), but only while the
                    # run is still on the model that decision actually chose:
                    # after a supervisor switch (`_switch_model`) the list
                    # was computed for a different model's endpoints and is
                    # stale for this one, so it is withheld rather than
                    # forwarded. `run.routing["chosen_model"]` never changes
                    # on a switch (see `_switch_model`'s own docstring), so
                    # this comparison is exactly "has this run switched away
                    # from its original routing decision yet".
                    provider_ignore=provider_ignore,
                ):
                    if isinstance(event, TextDelta):
                        assistant_text.append(event.text)
                        await self.bus.publish(run.id, RunEvent("text_delta", {"text": event.text}))
                    elif isinstance(event, ToolCallComplete):
                        tool_calls.append(event.tool_call)
                    elif isinstance(event, TurnComplete):
                        turn = event
            except ProviderError as e:
                # A stale provider_ignore — this decision's own poor-endpoint
                # evidence (`RoutingDecision.provider_ignore`) — can rule out
                # every endpoint OpenRouter would otherwise route this model to.
                # Live probe against OpenRouter (2026-09-10, every endpoint of a
                # real model excluded via `provider.ignore`): the response is
                # HTTP 404, body `{"error":{"message":"All providers have been
                # ignored. ...","code":404,"metadata":{"failed_routing_step":
                # "Filter by Ignored Providers"}}}` — not the 503 text this
                # handler used to match on alone, which is what OpenRouter's own
                # error docs (https://openrouter.ai/docs/api-reference/errors)
                # describe for the same routing-exhausted case under different
                # wording. Detection is therefore status-based first: 404 or 503
                # on a call that actually carried a `provider_ignore` is already
                # a strong signal by itself, since this is the only call site
                # that ever sets that argument. The lowercase substring match is
                # kept as a fallback for wording (or a status) this probe didn't
                # cover. Retried once with only this decision's own ignore list
                # dropped; an operator's static
                # `TRET_OPENROUTER_PROVIDER_PREFS.ignore` is left untouched
                # (`_provider_body` still merges it in from `_provider_prefs`)
                # by design — that denylist is a deliberate standing choice, not
                # evidence this run collected and might be wrong about. A model
                # genuinely unreachable even without this run's own ignore list
                # still fails, since the retry's own ProviderError falls
                # straight into the ordinary partial-turn handling below. Fires
                # at most once per run (`provider_ignore_waived`, set below) —
                # see the `provider_ignore` computation above the try/except.
                error_text = str(e).lower()
                no_eligible_provider = e.status in (404, 503) or any(
                    phrase in error_text
                    for phrase in (
                        "all providers have been ignored",
                        "filter by ignored providers",
                        "no available model provider",
                        "no endpoints",
                    )
                )
                if provider_ignore and no_eligible_provider:
                    provider_ignore_waived = True
                    run.routing = {
                        **(run.routing or {}),
                        "provider_ignore_waived": {
                            "at_iteration": iteration,
                            "error": str(e),
                        },
                    }
                    await self.bus.publish(
                        run.id,
                        RunEvent(
                            "provider_ignore_waived",
                            {"iteration": iteration, "error": str(e)},
                        ),
                    )
                    assistant_text = []
                    tool_calls = []
                    turn = None
                    try:
                        async for event in provider.stream(
                            model=model_info.wire_id,
                            system=system,
                            messages=wire,
                            tools=tool_specs,
                            max_tokens=max_output_tokens,
                            temperature=temperature,
                            effort=segment.effort,
                            session_id=str(run.id),
                            provider_ignore=None,
                        ):
                            if isinstance(event, TextDelta):
                                assistant_text.append(event.text)
                                await self.bus.publish(
                                    run.id, RunEvent("text_delta", {"text": event.text})
                                )
                            elif isinstance(event, ToolCallComplete):
                                tool_calls.append(event.tool_call)
                            elif isinstance(event, TurnComplete):
                                turn = event
                    except ProviderError as retry_e:
                        e = retry_e
                    else:
                        e = None
                if e is not None:
                    # Keep what the provider did say before it died. The turn's text
                    # was already streamed to the watching client, so dropping it
                    # here left the persisted transcript ending one turn earlier than
                    # what the operator saw — and the reasoning that led into the
                    # failure is exactly what an audit of a failed run needs. Tool
                    # calls that arrived but were never executed are recorded as
                    # metadata rather than as `tool_calls`: an unanswered tool_call id
                    # would make the transcript unreplayable.
                    partial = "".join(assistant_text)
                    if partial or tool_calls:
                        # Providers only yield TurnComplete (the usage carrier) after
                        # a clean stream, so every token already streamed here was
                        # paid to the provider and would otherwise go unmetered —
                        # this run's receipt would understate what it actually cost.
                        # Estimate what was on the wire and what came back (chars/4,
                        # the same dependency-free estimator context/compaction use
                        # for budgeting) and book it through the ordinary catalog
                        # path, flagged `estimated` rather than folded in as a
                        # confident figure. See `_book_usage`.
                        partial_msg = Msg(role="assistant", content=partial or None, tool_calls=tool_calls)
                        est_input_tokens = estimate_wire_tokens(system, wire, tool_specs)
                        # The wire prefix a dying turn sent is the same prefix the
                        # prior turn of this model sent (nothing about the
                        # conversation-so-far changes between consecutive turns
                        # except what got appended at the end) — so the last
                        # *metered* turn's cache_read_tokens is the best available
                        # proxy for how much of this one was served from cache too.
                        # With no prior metered turn (segment.last_reported_cache_
                        # read_tokens is None), there is no proxy and the estimate
                        # stays the plain chars/4 figure it always was.
                        carried_cache_read_tokens = min(
                            segment.last_reported_cache_read_tokens or 0, est_input_tokens
                        )
                        est_usage = Usage(
                            input_tokens=est_input_tokens - carried_cache_read_tokens,
                            output_tokens=estimate_message_tokens(partial_msg),
                            cache_read_tokens=carried_cache_read_tokens,
                        )
                        self._book_usage(
                            run=run,
                            total_usage=total_usage,
                            segment=segment,
                            segments=segments,
                            model_info=model_info,
                            usage=est_usage,
                            iteration=iteration,
                            emissions=emissions,
                            estimated=True,
                            served_by=None,
                            inference_geo=None,
                            usage_status="estimated",
                            started_at=call_started_at,
                            ended_at=_utcnow(),
                        )
                        messages.append(
                            Msg(
                                role="assistant",
                                content=partial or None,
                                meta={
                                    "iteration": iteration,
                                    "partial": True,
                                    "provider_error": str(e),
                                    "unexecuted_tool_calls": [tc.name for tc in tool_calls],
                                    "estimated_usage": {
                                        "input_tokens": est_usage.input_tokens,
                                        "output_tokens": est_usage.output_tokens,
                                        "cache_read_tokens": est_usage.cache_read_tokens,
                                    },
                                },
                            )
                        )
                    else:
                        # The request failed before enough response evidence
                        # existed to estimate usage. Preserve the attempted
                        # call with explicitly unknown routing metadata; never
                        # inherit the preceding successful call's endpoint or
                        # geography.
                        segment.call_records.append(
                            {
                                "iteration": iteration,
                                "input_tokens": 0,
                                "output_tokens": 0,
                                "cache_read_tokens": 0,
                                "cache_write_tokens": 0,
                                "reasoning_tokens": None,
                                "reasoning_accounting": None,
                                "served_by": None,
                                "inference_geo": None,
                                "usage_status": "unavailable",
                                "started_at": call_started_at.isoformat(),
                                "ended_at": _utcnow().isoformat(),
                            }
                        )
                    run.status = "failed"
                    run.error = str(e)
                    break

            usage = turn.usage if turn else Usage()
            if turn is not None and turn.served_by:
                segment.served_by = turn.served_by
            self._book_usage(
                run=run,
                total_usage=total_usage,
                segment=segment,
                segments=segments,
                model_info=model_info,
                usage=usage,
                iteration=iteration,
                emissions=emissions,
                served_by=turn.served_by if turn is not None else None,
                inference_geo=turn.inference_geo if turn is not None else None,
                usage_status="reported" if turn is not None else "missing",
                started_at=call_started_at,
                ended_at=_utcnow(),
                wire_changed_by_compaction=compaction_changed_wire,
                cache_voided_by_effort_raise=voided_by_effort_this_turn,
            )

            messages.append(
                Msg(
                    role="assistant",
                    content="".join(assistant_text) or None,
                    tool_calls=tool_calls,
                    meta={"iteration": iteration},
                )
            )

            run.messages = [m.to_json() for m in messages]
            await db.commit()
            await self.bus.publish(
                run.id,
                RunEvent(
                    "usage",
                    {
                        "iteration": iteration,
                        "input_tokens": total_usage.input_tokens,
                        "output_tokens": total_usage.output_tokens,
                        "cache_read_tokens": total_usage.cache_read_tokens,
                        "cache_write_tokens": total_usage.cache_write_tokens,
                        "cost_usd": float(run.cost_usd),
                        # Estimated, not metered — docs/emissions-methodology.md.
                        # energy_wh is compute only; the carbon fields (co2e_g,
                        # scope2_g, scope3_g, baseline_co2e_g, avoided_co2e_g),
                        # the same-token money figure (avoided_usd) and the
                        # judgment band (co2e_g_low/high) come straight from the
                        # accounting block.
                        "energy_wh": run.energy_accounting["energy_wh"],
                        **emission_event_fields(run.energy_accounting),
                    },
                ),
            )

            if not tool_calls:
                # Model believes it's done. If a terminal verdict is required and
                # missing, nudge once; otherwise finish.
                if ctx.terminal_tool and not ctx.terminal_recorded and not nudged:
                    nudged = True
                    messages.append(
                        Msg(
                            role="user",
                            content=(
                                f"You have not recorded your result. Call `{ctx.terminal_tool}` "
                                "with the required schema now, or file_data_request and record an "
                                "insufficient_data outcome if the assessment cannot be completed."
                            ),
                            # Structural, not prose: outcome scoring counts how
                            # often a model had to be told to finish, and reading
                            # that off the sentence would break the day the
                            # sentence is reworded (services/transcript.py).
                            meta={ENGINE_NUDGE_KEY: NUDGE_TERMINAL},
                        )
                    )
                    continue
                final_text = "".join(assistant_text)
                # Read by the end-of-loop fallback below, after this loop
                # variable has gone out of scope in every sense but Python's
                # own (a `for` body has no block scope) — it is what lets
                # that fallback tell "this run's last turn was a blank reply,
                # deliberately left unchecked" apart from "grounding was
                # mid-repair when something else (cost cap, output budget, a
                # provider error) cut the run off first".
                grounding_final_reply_blank = not final_text.strip()
                if not ctx.terminal_tool and not final_text.strip() and not nudged:
                    # A chat/freeform turn's answer *is* its text, and some
                    # models end a tool exchange with an empty completion. One
                    # structural nudge, same budget as the terminal nudge; a
                    # second empty turn ends `completed_without_output` (see
                    # `_final_status`) rather than presenting silence as success.
                    nudged = True
                    messages.append(
                        Msg(
                            role="user",
                            content=(
                                "You returned no text. Write your reply now; if the "
                                "request cannot be answered, say so and name what is missing."
                            ),
                            meta={ENGINE_NUDGE_KEY: NUDGE_EMPTY_REPLY},
                        )
                    )
                    continue
                # Guarded on `final_text.strip()` too: an empty reply's own
                # guard above owns that path (either nudging once or ending
                # `completed_without_output`), and there is nothing here for
                # the grounding check to run against — an empty string has no
                # numbers, so it would otherwise record a spurious "clean"
                # (or, worse, a stale unsupported list from an EARLIER failed
                # attempt this same run, if one preceded the empty replies)
                # over a turn that never said anything at all.
                if grounding_applies and final_text.strip():
                    if not run_has_retrieval_evidence(messages, ctx.retrieved_values):
                        # No tool ever ran this run and nothing was retrieved
                        # — a pure-knowledge answer has no retrieved data to
                        # contradict, so the check does not run at all rather
                        # than flagging ordinary prose against an empty
                        # evidence set. `checked: False` is what lets an
                        # operator (and the chat UI) tell "not checked" apart
                        # from "checked and found nothing wrong".
                        run.grounding = {
                            "checked": False,
                            "status": "skipped",
                            "attempts": 0,
                            "unsupported": [],
                            "first_unsupported": [],
                        }
                    else:
                        # `messages` already carries this turn's own assistant
                        # message (appended above, before tool_calls was even
                        # known to be empty) as its last entry — `evidence_
                        # numbers` excludes exactly that entry, so the reply
                        # is never checked against itself.
                        unsupported = unsupported_numbers(
                            final_text,
                            evidence_numbers(
                                system=system,
                                messages=messages,
                                task_input=run.task_input,
                                retrieved=ctx.retrieved_values,
                            ),
                        )
                        if unsupported:
                            if grounding_first_unsupported is None:
                                grounding_first_unsupported = unsupported
                            grounding_last_unsupported = unsupported
                            grounding_failures += 1
                            # Never spend the LAST iteration on a nudge: there
                            # is no further turn left for the rewrite it would
                            # ask for, so appending one here would just ship
                            # the reply unresolved anyway, one wasted turn
                            # later, via the max-iterations path instead of
                            # this one — worse, not better, since that path
                            # ends the run `failed` over a repair it was never
                            # given room to make. Finish normally instead.
                            if (
                                grounding_failures < GROUNDING_MAX_REPAIRS
                                and iteration < max_iterations
                            ):
                                messages.append(
                                    Msg(
                                        role="user",
                                        content=grounding_nudge_message(unsupported),
                                        meta={
                                            ENGINE_NUDGE_KEY: NUDGE_GROUNDING,
                                            "unsupported": unsupported,
                                        },
                                    )
                                )
                                continue
                        run.grounding = {
                            "checked": True,
                            "status": (
                                "clean"
                                if grounding_failures == 0
                                else ("unresolved" if unsupported else "repaired")
                            ),
                            "attempts": grounding_failures,
                            "unsupported": unsupported,
                            "first_unsupported": grounding_first_unsupported or [],
                        }
                run.status = self._final_status(ctx, final_text)
                break

            spent = _spent(run)
            if spent >= max_cost:
                run.status = "failed"
                # Byte-identical to the pre-delegation message when nothing has
                # been delegated (delegated_cost_usd == 0) — router_llm/outcomes.py
                # only matches the "cost_cap_exceeded" prefix, but the exact
                # historical tail is kept anyway rather than changed for free.
                if run.delegated_cost_usd:
                    run.error = (
                        f"cost_cap_exceeded: run cost ${run.cost_usd} + delegated "
                        f"${run.delegated_cost_usd} >= cap ${max_cost}"
                    )
                else:
                    run.error = f"cost_cap_exceeded: run cost ${run.cost_usd} >= cap ${max_cost}"
                break

            # Hard stop only once the model has had the finalize-now instruction
            # below and kept going anyway.
            hard_budget = int(output_budget * OUTPUT_BUDGET_HARD_MULTIPLE) if output_budget else 0
            if budget_nudged and total_usage.output_tokens >= hard_budget:
                run.status = "failed"
                run.error = (
                    f"output_budget_exceeded: {total_usage.output_tokens} output tokens vs "
                    f"budget {output_budget} (hard stop at {hard_budget})"
                )
                break

            # Execute the turn's tool calls ONE AT A TIME, committing after each.
            #
            # Sequential is a correctness requirement, not a simplification.
            # Several builtin tools write through the single `RunContext.db`
            # AsyncSession, and SQLAlchemy rejects concurrent flushes on one
            # session. Gathering them raised "Session is already flushing" in the
            # second and later writers *after* `Session.add()` had already run —
            # so the row still landed at the end-of-iteration commit while the
            # model was told its write failed, and every post-write side effect
            # (the `finding_recorded` event, the terminal-tool flag) was skipped.
            # A turn's latency is dominated by the provider call, not by tool
            # execution, so the concurrency bought almost nothing and cost the
            # run's most basic invariant: **a tool never reports failure after
            # its write succeeded, and persisted state never contradicts the
            # run's status or events.** Commit-on-success / rollback-on-error
            # below is the other half of that invariant.
            await self._publish_tool_calls(run.id, tool_calls)
            for tc in tool_calls:
                spec = self._spec_for(tool_specs, tc.name)
                findings_before = len(ctx.findings_created)
                if spec is None:
                    result_text, is_error = await _unknown_tool(tc.name)
                else:
                    result_text, is_error = await execute_tool(ctx, spec, tc.arguments)

                if is_error and ctx.terminal_tool and tc.name == ctx.terminal_tool:
                    consecutive_terminal_failures += 1
                elif not is_error and ctx.terminal_tool and tc.name == ctx.terminal_tool:
                    consecutive_terminal_failures = 0

                if is_error:
                    # Roll the failed tool's partial write out of the session so
                    # the end-of-iteration commit cannot persist something the
                    # model was told did not happen. Earlier calls in this turn
                    # are already committed, so only the failed one is discarded.
                    await db.rollback()
                    del ctx.findings_created[findings_before:]
                    # rollback() expires every instance in the session; reload
                    # the run so later attribute reads don't fault on an async
                    # lazy load.
                    await db.refresh(run)
                else:
                    # Reported success is durable success, before the model is
                    # ever told the call worked.
                    await db.commit()
                    # The terminal flag is the ENGINE's to set, from the task's
                    # declared `terminal_tool` — never a tool's own opinion of
                    # whether it is terminal. A tool and the task config can no
                    # longer disagree (see `_completion_status`).
                    if ctx.terminal_tool and tc.name == ctx.terminal_tool:
                        ctx.terminal_recorded = True

                # Break retrieval loops: an identical call repeated 3+ times gets
                # a pointed reminder appended to its result.
                import json as _json

                call_key = f"{tc.name}:{_json.dumps(tc.arguments, sort_keys=True, default=str)}"
                seen_calls[call_key] = seen_calls.get(call_key, 0) + 1
                repeated = 0
                if seen_calls[call_key] >= 3 and not is_error:
                    repeated = seen_calls[call_key]
                    repeated_call_trips += 1
                    result_text += (
                        "\n\n[NOTE: you have now made this exact call "
                        f"{seen_calls[call_key]} times and the result is unchanged. You have "
                        "the data you need — proceed to your terminal action "
                        f"({ctx.terminal_tool or 'your final answer'}) now.]"
                    )
                # `iteration` is what `plan_compaction`'s `KEEP_RECENT_ITERATIONS`
                # protection reads (`_iteration_of`, engine/compaction.py) — left
                # off here, every tool result read back as iteration 0, so the
                # "recent" cutoff (always > 0 once a run is old enough to compact
                # at all) never matched anything and the protection never once
                # applied to a real run. Concretely: a forced pass after a model
                # switch could elide the very document read from one turn earlier
                # and hand the new model nothing but a marker for it.
                meta = {"error": is_error, "iteration": iteration}
                if repeated:
                    meta[REPEATED_CALL_KEY] = repeated
                messages.append(
                    Msg(role="tool", content=result_text, tool_call_id=tc.id, meta=meta)
                )
                await self.bus.publish(
                    run.id,
                    RunEvent(
                        "tool_result",
                        {"tool": tc.name, "id": tc.id, "error": is_error, "result": result_text[:2000]},
                    ),
                )
            for finding_id in ctx.findings_created:
                await self.bus.publish(
                    run.id, RunEvent("finding_recorded", {"finding_id": str(finding_id)})
                )
            findings_total += len(ctx.findings_created)
            ctx.findings_created.clear()

            # Soft output budget: ask for the terminal action once, then let the
            # hard stop above deal with a model that keeps going anyway.
            if output_budget and total_usage.output_tokens >= output_budget and not budget_nudged:
                budget_nudged = True
                messages.append(
                    Msg(
                        role="user",
                        content=(
                            f"OUTPUT BUDGET REACHED: this run has produced "
                            f"{total_usage.output_tokens} of {output_budget} budgeted output "
                            "tokens. Stop gathering and finalize now: call "
                            f"`{ctx.terminal_tool or 'your final answer'}` with what you already "
                            "retrieved, or file_data_request and record an insufficient_data "
                            "outcome. Do not start new lines of inquiry."
                        ),
                        meta={ENGINE_NUDGE_KEY: NUDGE_OUTPUT_BUDGET},
                    )
                )
                await self.bus.publish(
                    run.id,
                    RunEvent(
                        "budget_warning",
                        {
                            "kind": "output_tokens",
                            "output_tokens": total_usage.output_tokens,
                            "budget": output_budget,
                        },
                    ),
                )

            # ── should this run change model? ────────────────────────────────
            # Between iterations, deterministically, on the state the loop has
            # already gathered. See engine/supervisor.py for why this is not an
            # LLM call and why every intervention is bounded.
            #
            # Short-circuited when switching is off, so a harness that disabled
            # it does not pay for a candidate list and a priors lookup on every
            # iteration to be told the same thing each time. `assess` refuses on
            # the same conditions; this only avoids the work of asking.
            #
            # `max_switches <= 0` alone is NOT one of those short-circuits: it
            # spends no switch, so it must not disable the quality trigger's
            # effort rung (Rung 1) — only once there is nothing left the rung
            # could still do is asking it skipped:
            #   * it already fired this run (`effort_raised`), or
            #   * this harness is not even under `on_quality` (`on_stall` has
            #     no effort rung to reach — see supervisor.assess), or
            #   * the current model would refuse the control anyway
            #     (`not model_info.supports_effort`).
            # Any of those three, together with a 0 switch limit, means every
            # `assess()` call this iteration could produce is one `candidates_
            # for` lookup and a `switch_refused` event for a switch this
            # harness will never be allowed to make.
            if adaptive.escalation == "off" or overridden or (
                adaptive.max_switches <= 0
                and (
                    effort_raised
                    or adaptive.escalation != "on_quality"
                    or not model_info.supports_effort
                    # Already at the top level: the rung can never fire, so
                    # there is nothing left for a zero-switch harness to ask.
                    or segment.effort == "high"
                )
            ):
                await db.commit()
                continue
            candidates, live_priors = await self.router.candidates_for(
                model_policy=model_policy,
                task_shape=task.get("shape", "freeform"),
                est_input_tokens=est_tokens,
            )
            intervention = assess(
                TurnState(
                    iteration=iteration,
                    max_iterations=max_iterations,
                    model=model_info,
                    est_wire_tokens=est_tokens,
                    context_limit=context_limit,
                    compaction_exhausted=compaction_exhausted,
                    consecutive_terminal_failures=consecutive_terminal_failures,
                    grounding_nudges=grounding_failures,
                    repeated_call_trips=repeated_call_trips,
                    terminal_recorded=ctx.terminal_recorded,
                    findings_created=findings_total,
                    cost_so_far=_spent(run),
                    max_cost_usd=max_cost,
                    switches_used=switches_used,
                    max_switches=adaptive.max_switches,
                    escalation=adaptive.escalation,
                    overridden=overridden,
                    effort=segment.effort,
                    supports_effort=model_info.supports_effort,
                    effort_raised=effort_raised,
                    effort_raised_at=effort_raised_at,
                    failures_at_raise=failures_at_raise,
                    trips_at_raise=trips_at_raise,
                ),
                candidates=candidates,
                priors=live_priors,
            )
            if intervention.kind == KIND_EFFORT:
                # Same model, same segment — no meter boundary, no
                # context-limit recompute, no message normalization, and the
                # cache stays alive on every provider but Anthropic (see
                # supervisor.py's note in the intervention's own evidence) —
                # the only thing that changes is which effort level the live
                # segment's own `effort` reads as, off of which `provider.
                # stream()` reads next iteration (see `_raise_effort`'s own
                # docstring for why this is no longer a new segment).
                self._raise_effort(
                    run=run,
                    intervention=intervention,
                    segment=segment,
                    segments=segments,
                    iteration=iteration,
                )
                effort_raised = True
                effort_raised_at = iteration
                failures_at_raise = consecutive_terminal_failures
                trips_at_raise = repeated_call_trips
                # Anthropic is the one provider where this still voids the
                # cache (see the comment above and `KIND_EFFORT`'s own
                # docstring in engine/supervisor.py) — so it is the one
                # provider where the next turn's cache-ledger classification
                # (`_book_usage`) needs to know a miss there is expected, not
                # unexplained.
                if model_info.provider == "anthropic":
                    cache_void_pending = True
                await self.bus.publish(
                    run.id,
                    RunEvent("effort_raised", run.routing["effort_changes"][-1]),
                )
            elif intervention.switching:
                switches_used += 1
                # The old segment's meter (if any) stops the moment its
                # segment stops accumulating turns — the new one starts its
                # own the moment `_switch_model` appends it. Order matters:
                # stop before switching so a metered old segment's final
                # reading is in hand before `_switch_model` builds the new
                # `ModelSegment` that becomes the loop's `segment`.
                await self._stop_meter(segment, emissions)
                model_info, provider, segment = self._switch_model(
                    run=run,
                    intervention=intervention,
                    segments=segments,
                    iteration=iteration,
                    emissions=emissions,
                )
                await self._start_meter(segment, settings, emissions)
                context_limit = context_budget(
                    model_info.context_window, max_output_tokens, adaptive.context_headroom
                )
                ctx.model_used = model_info.id
                messages = normalize_for_provider(messages, model_info.provider)
                # The cache is void from here: a different model has never seen
                # this prefix, so the next turn re-pays full input price. The
                # supervisor priced that in before choosing to switch.
                compaction_exhausted = False
                # ...and since the whole transcript is about to be re-sent
                # uncompacted at that full price anyway, this is the cheapest
                # possible moment to also shrink it: force a compaction pass on
                # the new model's first turn even though that turn is not, on
                # its own, over budget (see the "stay inside the window"
                # section at the top of the loop).
                compact_before_next_turn = True
                consecutive_terminal_failures = 0
                repeated_call_trips = 0
                # `effort_raised` itself is left alone: the rung fires at most
                # once per *run*, not once per model (supervisor.assess's own
                # Rung 1 gate). But `effort_raised_at`/`failures_at_raise`/
                # `trips_at_raise` are a snapshot taken on the model the run
                # is leaving — left un-reset, `_quality_trigger`'s post-raise
                # baseline would compare this new model's fresh counters
                # against a stale snapshot from a model that no longer even
                # applies, desensitising the trigger on the very model the
                # switch was supposed to give a clean shot at. Resetting
                # `effort_raised_at` to None (rather than leaving the old
                # iteration number in place) also moves the new model back
                # onto `_quality_trigger`'s ordinary first-trigger path — no
                # stale grace window to reason about — for exactly the same
                # reason a fresh run's first raise does.
                effort_raised_at = None
                failures_at_raise = 0
                trips_at_raise = 0
                await self.bus.publish(
                    run.id, RunEvent("model_switch", run.routing["switches"][-1])
                )
            elif intervention.refused:
                # A run that was stuck and that tret decided not to rescue is
                # exactly what an operator reading a failed run needs to see, and
                # it is invisible unless it is written down.
                await self.bus.publish(
                    run.id,
                    RunEvent(
                        "switch_refused",
                        {
                            "iteration": iteration,
                            "reason": intervention.reason,
                            "detail": intervention.detail,
                            "refused": intervention.refused,
                        },
                    ),
                )
            await db.commit()
        else:
            # The ceiling stopped the loop. Whether that is a failure depends on
            # whether the run had already delivered: a validated terminal result
            # is recorded, auditable output, and calling the run `failed` threw it
            # away — the runs list, an operator's filter, and `run_harness_task`
            # all report "this produced nothing" while a perfectly good draft sits
            # on disk. The ceiling is still surfaced, as a budget warning and in
            # the run's own `iterations`.
            if ctx.terminal_recorded:
                run.status = self._completion_status(ctx)
                await self.bus.publish(
                    run.id,
                    RunEvent(
                        "budget_warning",
                        {
                            "kind": "iterations",
                            "iterations": max_iterations,
                            "budget": max_iterations,
                        },
                    ),
                )
            else:
                run.status = "failed"
                run.error = f"max_iterations ({max_iterations}) reached without completion"
        # The CURRENT segment — `segment` is reassigned on every model
        # switch inside the loop, so this always stops whichever one was
        # live when the loop exited normally (an exception or cancellation
        # out of the loop above skips straight past this to `execute()`'s
        # own `finally`, which stops it from there instead).
        await self._stop_meter(segment, emissions)
        # Recompute after every attempted meter stops. Successful readings can
        # replace the estimate; failed/incomplete attempts still gained final
        # status and coverage diagnostics during stop and must be persisted.
        if any(seg.meter_describe is not None for seg in segments):
            accounting = _combine_segments(segments, emissions)
            run.energy_wh = Decimal(str(accounting["energy_wh"]))
            run.energy_accounting = accounting
            # Rewritten unconditionally on a recompute — the same way
            # `_switch_model` always rewrites it — rather than only once
            # `len(segments) > 1`: a single-segment run that was measured
            # deserves a `model_timeline` that agrees with `energy_accounting`
            # too, not just the multi-segment case.
            run.model_timeline = [seg.to_json(emissions) for seg in segments]

        # `run.grounding` is normally set inside the loop, on the turn whose
        # reply finally goes unchallenged (engine/grounding.py "the reply
        # being checked itself" block above). If the loop instead ended some
        # other way — the iteration cap, an output-budget or cost-cap stop,
        # a provider error — after at least one grounding failure, that block
        # never ran and the failure would otherwise vanish from the record
        # entirely. Recover it here, minimally, as unresolved.
        #
        # `not grounding_final_reply_blank` excludes exactly the one case
        # that block itself declines to handle on purpose: the run's last
        # turn was an empty reply, whose own guard (`_final_status`) already
        # owns the run's status and leaves `run.grounding` deliberately unset
        # rather than backfilling a verdict over a turn that said nothing.
        if (
            grounding_applies
            and run.grounding is None
            and grounding_failures > 0
            and not grounding_final_reply_blank
        ):
            run.grounding = {
                "checked": True,
                "status": "unresolved",
                "attempts": grounding_failures,
                "unsupported": grounding_last_unsupported or [],
                "first_unsupported": grounding_first_unsupported or [],
            }

        # ── finish ───────────────────────────────────────────────────────────
        if run.status == "running":
            run.status = self._completion_status(ctx)
        # `ctx.document_ids` started as a copy of `run.document_ids` (the
        # run's initial attachments) and grew as tools materialised more of
        # them mid-run — `fetch_url`/`store_snapshot` and `read_connected_
        # file` both append to it (see engine/tools.py), but neither ever
        # wrote back to `run` itself. Without this, GET /api/runs/{id} kept
        # reporting only what the run started with, silently dropping every
        # document a tool pulled in along the way.
        run.document_ids = list(ctx.document_ids)
        run.messages = [m.to_json() for m in messages]
        run.overhead = overhead_block(overhead_calls)
        run.finished_at = _utcnow()
        # A healthy single-segment run never persisted its timeline mid-run
        # (see `_book_usage`), but `served_by` and `effort_history` both live
        # only on the timeline, and `record_outcome` reads `served_by` from
        # there. Refreshed unconditionally here whenever either is present —
        # deliberately NOT gated on `not run.model_timeline`: a run whose
        # effort was raised mid-run already has a truthy timeline from
        # `_raise_effort`'s own write, but that write happened at the raise
        # iteration and is frozen there — nothing refreshes it again for an
        # otherwise-healthy single-segment run (`_book_usage`'s own condition
        # only re-persists on a *later* raise, an estimate, or a second
        # segment), so without dropping the guard here the persisted segment
        # would report only the tokens/cost/energy up to the raise while the
        # run's own totals kept growing underneath it — exactly the frozen
        # timeline api/analytics.py's what-if recompute would otherwise read
        # as the run's whole story. The cache-ledger counters are the same
        # argument a third time: `_book_usage` classifies them mid-run but
        # never persists them (nothing mid-run reads them, so there is no
        # reason to pay that JSONB write every turn — see its own note), so an
        # ordinary single-segment run with a nonzero count would otherwise
        # finish with a timeline nothing ever wrote the ledger onto.
        if any(
            seg.served_by or seg.effort_history or seg.cache_rebuilds_expected
            or seg.cache_misses_unexpected or seg.call_records
            for seg in segments
        ):
            run.model_timeline = [seg.to_json(emissions) for seg in segments]
        # The run-level roll-up of the same counters, written once here rather
        # than recomputed every turn (see `_book_usage`'s own note) — cheap
        # either way, but nothing mid-run reads it, so there is no reason to
        # pay the write on every iteration. Lives on `run.routing` alongside
        # the other per-run routing facts this engine already writes there
        # (`switches`, `effort_changes`) — there is no established top-level
        # "spend summary" column this run assembles that `combine_accountings`
        # would carry a *summed* pair of plain ints through unscathed (its
        # per-segment merge treats an unlisted key as one that must AGREE
        # across segments, which two segments with different rebuild counts
        # almost never do — see its own "handled" set), so it is written here
        # rather than folded into `run.energy_accounting`. Only written once
        # there is something to say, same reasoning as `model_timeline` above.
        if any(seg.cache_rebuilds_expected or seg.cache_misses_unexpected for seg in segments):
            routing = dict(run.routing or {})
            routing["cache_ledger"] = {
                "cache_rebuilds_expected": sum(seg.cache_rebuilds_expected for seg in segments),
                "cache_misses_unexpected": sum(seg.cache_misses_unexpected for seg in segments),
            }
            run.routing = routing
        # Evidence for the next routing decision, folded into the run's own final
        # commit. `record_outcome` never raises and returns None for runs that
        # carry no lesson (cancelled, or never routed) — see services/outcomes.py.
        await record_outcome(db, run)
        await db.commit()
        # Extension seam: run persistence is durable first, so an extension
        # metering this run against a balance sees its final cost. No-op with
        # no extensions loaded.
        await get_extension_registry().run_post_run_hooks(db, run, harness.workspace_id)
        # This run is now evidence, and the cached aggregate predates it. Cheap
        # to drop and the alternative is a bad look: a run finishing badly, and
        # the very next run of the same shape routing as though it had not.
        self.priors.invalidate()
        # `_cancelled` itself is discarded in `execute()`'s `finally`, not here:
        # that one place covers this normal finish *and* `_fail_before_start`'s
        # early return *and* the crash handler, so a terminal run's id is never
        # left behind regardless of which of those three ways it got here.
        if run.status in SUCCESS_STATUSES:
            await self.bus.publish(
                run.id,
                RunEvent(
                    "done",
                    {
                        "status": run.status,
                        "cost_usd": float(run.cost_usd or 0),
                        "energy_wh": energy_wh_field(run.energy_wh),
                        # co2e_g / scope2_g / scope3_g / baseline_co2e_g /
                        # avoided_co2e_g, as recorded. Null when there is no
                        # estimate — never 0.
                        **emission_event_fields(run.energy_accounting),
                        "iterations": run.iterations,
                    },
                ),
            )
        else:
            await self.bus.publish(
                run.id, RunEvent("error", {"message": run.error or run.status, "status": run.status})
            )

    def _book_usage(
        self,
        *,
        run: Run,
        total_usage: Usage,
        segment: ModelSegment,
        segments: list[ModelSegment],
        model_info: ModelInfo,
        usage: Usage,
        iteration: int,
        emissions: "_EmissionsContext | None" = None,
        estimated: bool = False,
        served_by: str | None = None,
        inference_geo: str | None = None,
        usage_status: str = "reported",
        started_at: datetime | None = None,
        ended_at: datetime | None = None,
        wire_changed_by_compaction: bool = False,
        cache_voided_by_effort_raise: bool = False,
    ) -> Decimal:
        """Fold one turn's usage into the run's running totals, cost and energy.

        Shared by the ordinary per-turn accounting above and by the
        `ProviderError` handler's estimated-usage booking for a turn that died
        mid-stream, so the two paths can never compute a run's cost differently.
        `estimated` marks a turn whose usage was guessed (chars/4, from what was
        actually on the wire and what streamed back before the failure) rather
        than reported by the provider — the catalog pricing and energy
        accounting below are identical either way; only the provenance recorded
        on the model segment (`ModelSegment.estimated_usage`) differs. Returns
        the turn's own cost in USD.

        `wire_changed_by_compaction` and `cache_voided_by_effort_raise` are the
        two engine-caused reasons — beyond a segment's own first turn — that a
        cache miss on this turn is an *expected* rebuild rather than an
        unexplained one; see the cache-ledger block below and `ModelSegment.
        cache_rebuilds_expected`'s own docstring. Both default False so the
        `ProviderError` estimate call site (which never classifies — see
        below) need not pass them.
        """
        total_usage.input_tokens += usage.input_tokens
        total_usage.output_tokens += usage.output_tokens
        total_usage.cache_read_tokens += usage.cache_read_tokens
        total_usage.cache_write_tokens += usage.cache_write_tokens
        # ── the cache ledger ─────────────────────────────────────────────────
        # Read `segment.from_iteration` *before* `segment.add()` below moves it
        # off its unset 0 — that field is exactly "has this segment booked a
        # turn yet", which is what "the first turn of the segment" means here.
        # Skipped entirely for an estimated turn: a mid-stream death's usage is
        # a chars/4 guess with a carried-forward cache figure (see the
        # `ProviderError` handler above), never a cache_read_tokens the
        # provider actually reported, so classifying it would misread a guess
        # as measured evidence of a rebuild. Also skipped for a provider with
        # no cache concept at all (`NO_CACHE_STATS_PROVIDERS`): a 0 there means
        # "never wired up", not "this prefix missed".
        if not estimated and model_info.provider not in NO_CACHE_STATS_PROVIDERS:
            # A read/write of 0/0 is ambiguous on its own: it is what a genuine
            # cache miss looks like on a provider that never reports writes
            # either, but it is *also* what an OpenRouter upstream that omits
            # `cached_tokens` reports on every turn (e.g. an OpenRouter-hosted
            # Kimi endpoint — `model_info.provider == "openrouter"`, so
            # `NO_CACHE_STATS_PROVIDERS` alone does not catch it), and what a
            # prompt below the provider's cacheable minimum reports too. All
            # three would otherwise read as an "unexpected miss" that never
            # actually happened. `cache_is_live` is the disambiguator: this
            # turn wrote to the cache (proof caching is being attempted right
            # now), or an earlier turn on this segment read a nonzero figure
            # back (proof it has worked before, so a subsequent 0 is a real
            # rebuild/miss rather than silence). Neither true, and this turn is
            # left unclassified rather than guessed at.
            cache_is_live = usage.cache_write_tokens > 0 or bool(
                segment.last_reported_cache_read_tokens
            )
            if cache_is_live and usage.cache_read_tokens <= CACHE_MISS_FLOOR_TOKENS:
                if (
                    segment.from_iteration == 0
                    or wire_changed_by_compaction
                    or cache_voided_by_effort_raise
                ):
                    segment.cache_rebuilds_expected += 1
                else:
                    segment.cache_misses_unexpected += 1
        # Booked against the model that actually ran the turn. A run may
        # change model part-way (see the supervisor below), and every figure
        # downstream — price, energy class, PUE, grid factor — is a property
        # of *which* model spent the tokens, not of the run as a whole.
        segment.add(
            usage,
            iteration,
            estimated=estimated,
            served_by=served_by,
            inference_geo=inference_geo,
            usage_status=usage_status,
            started_at=started_at,
            ended_at=ended_at,
        )
        all_calls = [record for item in segments for record in item.call_records]
        if all_calls and all(record["reasoning_tokens"] is not None for record in all_calls):
            total_usage.reasoning_tokens = sum(
                record["reasoning_tokens"] for record in all_calls
            )
            semantics = {record["reasoning_accounting"] for record in all_calls}
            total_usage.reasoning_accounting = (
                semantics.pop() if len(semantics) == 1 else "unknown"
            )
        else:
            total_usage.reasoning_tokens = None
            total_usage.reasoning_accounting = None
        turn_cost = model_info.cost_usd(
            usage.input_tokens,
            usage.output_tokens,
            usage.cache_read_tokens,
            usage.cache_write_tokens,
        )
        run.iterations = iteration
        run.input_tokens = total_usage.input_tokens
        run.output_tokens = total_usage.output_tokens
        run.cache_read_tokens = total_usage.cache_read_tokens
        run.cache_write_tokens = total_usage.cache_write_tokens
        run.cost_usd = (run.cost_usd or Decimal(0)) + turn_cost
        # Best-known actual cost, alongside the pure catalog-priced figure
        # above. Per turn: the provider-reported actual when there is one
        # (only OpenRouter reports today, including a genuine 0 for :free
        # models), otherwise that turn's catalog price — an estimated turn has
        # no provider-reported figure either, so it falls into that same
        # "otherwise". This keeps a run that switches providers mid-run (e.g.
        # OpenRouter -> Anthropic) from under-billing on the turns the
        # provider stayed silent on.
        run.reported_cost_usd = (run.reported_cost_usd or Decimal(0)) + (
            usage.reported_cost_usd if usage.reported_cost_usd is not None else turn_cost
        )
        # Estimated energy/carbon, recomputed per segment from that segment's
        # running totals rather than accumulated per turn: the estimate is
        # linear in tokens, so a segment's total cannot drift from the sum of
        # its turns. The run-level block is the roll-up across segments, which
        # for the ordinary single-model run is byte-identical to the single
        # segment's own block (services/emissions.combine_accountings).
        accounting = _combine_segments(segments, emissions)
        run.energy_wh = Decimal(str(accounting["energy_wh"]))
        run.energy_accounting = accounting
        # Normally kept only once a run has used more than one model (see
        # ModelSegment's own docstring) — an estimated turn is one exception:
        # `estimated_usage` lives nowhere else on the run, so the timeline is
        # persisted even for an ordinary single-segment run rather than
        # silently dropping the one signal analytics needs to tell a metered
        # receipt from a guessed one. A segment carrying its own
        # `effort_history` is the same argument again: an effort raise is
        # deliberately *not* a new segment (see `_raise_effort`'s docstring),
        # so without this, a single-segment run that raised effort would
        # never persist a timeline mid-run at all, and analytics reading
        # `model_timeline` to recompute cost (api/analytics.py's what-if path)
        # would see nothing rather than the raise. (A segment's `served_by` is
        # the other signal that lives only on the timeline; that is written
        # once, at the finish path just before `record_outcome`, rather than
        # per turn here — each segment's JSON carries the full energy
        # derivation, so writing it every iteration would double the run
        # row's JSONB churn. The cache-ledger counters are the same argument:
        # nothing mid-run reads them (the classification above only ever
        # *writes* to the live `ModelSegment`, never back off the persisted
        # row), so they are folded into the same once-at-finish write rather
        # than persisted here on every turn — see the finish path, just
        # before `record_outcome`.
        if (
            len(segments) > 1
            or estimated
            or any(seg.effort_history or seg.call_records for seg in segments)
        ):
            run.model_timeline = [seg.to_json(emissions) for seg in segments]
        return turn_cost

    def _raise_effort(
        self,
        *,
        run: Run,
        intervention: Intervention,
        segment: ModelSegment,
        segments: list[ModelSegment],
        iteration: int,
    ) -> None:
        """Move the run onto a higher reasoning-effort level, same model,
        same segment.

        Used to start a new `ModelSegment` the way `_switch_model` does below,
        on the theory that two effort levels on one model are still two
        different call shapes worth accounting separately. In practice that
        boundary was the bug: `services/outcomes.py` builds one `run_outcomes`
        row per timeline segment and scores every segment but the last as a
        handoff (`handoff_score`) — so the model that was *winning* got its own
        prior poisoned with a `handed_off` (quality 0.05) the instant the
        quality trigger gave it more room, for a run where the model never
        actually changed. It also made the run-detail timeline claim "this run
        changed model" for a run that never did (the frontend renders
        `ModelTimeline` only once a run has more than one segment).
        Updating `segment.effort` in place and appending to its own
        `effort_history` (see `ModelSegment`) fixes both: one segment, one
        model, one `run_outcomes` row, effort recorded as a fact about that
        segment rather than a reason to end it.

        Still recorded in `run.routing["effort_changes"]` — `_switch_model`
        reads that list to carry an already-raised effort forward across an
        actual switch — and `run.model_used`/`run.provider_used` were never
        going to change here regardless, same as before.
        """
        new_effort = intervention.target
        from_effort = segment.effort
        segment.effort = new_effort
        segment.effort_history.append(
            {
                "at_iteration": iteration,
                "from_effort": from_effort,
                "to_effort": new_effort,
                "reason": intervention.reason,
            }
        )
        record = {
            "at_iteration": iteration,
            "model": segment.model.id,
            "from_effort": intervention.evidence.get("from_effort"),
            "to_effort": new_effort,
            "reason": intervention.reason,
            "detail": intervention.detail,
            "evidence": intervention.evidence,
            "decided_at": _utcnow().isoformat(),
        }
        routing = dict(run.routing or {})
        routing["effort_changes"] = [*(routing.get("effort_changes") or []), record]
        run.routing = routing
        run.model_timeline = [seg.to_json() for seg in segments]

    def _switch_model(
        self,
        *,
        run: Run,
        intervention: Intervention,
        segments: list[ModelSegment],
        iteration: int,
        emissions: "_EmissionsContext",
    ):
        """Move the run onto a different model, and record that it happened.

        The switch is appended to `run.routing["switches"]` in the shape of a
        routing decision, so the audit trail keeps its existing form: every model
        a run used is explained in the same place, in the same language, as the
        model it started with. `model_used` becomes the new model because it has
        always meant *the model that produced the final answer* — the full
        sequence is in `model_timeline`.
        """
        target = intervention.target
        # Carry the run's current effort intent forward rather than
        # recomputing it: the run's own `["effort_changes"]`, if the quality
        # trigger's Rung 1 already raised it once this run, otherwise
        # `run.routing["effort"]`, the field the run started with (see
        # `RoutingDecision.effort`) — so a switch never invents a different
        # level than the one this run is actually under, and never quietly
        # drops a raise that already happened. Only whether it is actually
        # sent changes here, re-gated by the *new* model's own
        # `supports_effort` (a switch can move onto a model that does, or does
        # not, accept the control, independent of the model it is leaving).
        effort_changes = (run.routing or {}).get("effort_changes") or []
        effort = (
            (effort_changes[-1].get("to_effort") if effort_changes else None)
            or (run.routing or {}).get("effort")
        )
        segments.append(
            ModelSegment(
                target,
                reason=intervention.reason,
                factors=self._factors_for(target.provider, target.id, emissions),
                effort=effort if target.supports_effort else None,
            )
        )
        record = {
            "at_iteration": iteration,
            "from_model": run.model_used,
            "chosen_model": target.id,
            "provider": target.provider,
            "reason": intervention.reason,
            "detail": intervention.detail,
            "evidence": intervention.evidence,
            "decided_at": _utcnow().isoformat(),
        }
        routing = dict(run.routing or {})
        routing["switches"] = [*(routing.get("switches") or []), record]
        run.routing = routing
        run.model_used = target.id
        run.provider_used = target.provider
        run.model_timeline = [seg.to_json(emissions) for seg in segments]
        return target, self.registry.get(target.provider), segments[-1]

    async def _compact(
        self,
        *,
        run: Run,
        messages: list[Msg],
        state: CompactionState,
        iteration: int,
        before_tokens: int,
        system: str,
        tool_specs: list,
        terminal_tool: str | None,
        max_tier: str,
        overhead: list[dict],
        emissions: "_EmissionsContext",
        trigger: str = "budget",
        basis: str = "chars4",
    ) -> dict | None:
        """Shrink what the provider sees, and say exactly what was shrunk.

        `messages` is not modified. Compaction produces a wire view; the
        transcript stays the complete record of what happened, and the dict
        returned here is what states the difference (see engine/compaction.py).

        `trigger` says *why* this pass ran — `"budget"` for the ordinary path
        (the wire view itself was over the window) or `"model_switch"` for the
        pass a supervisor switch forces on the new model's first turn before it
        ever sees the transcript (`compact_before_next_turn`, above). It never
        changes what gets elided — the elision rules are the same either way —
        only what the record says caused this particular pass to run.

        `basis` says whether `before_tokens` (and, for the ordinary budget
        path, the `over_budget` decision that led here) came from the chars/4
        estimate or an exact provider count (`EXACT_COUNT_THRESHOLD`,
        `Provider.count_tokens`) — carried straight onto the returned record
        so an operator reading `runs.compactions` can tell which one triggered
        this pass, without it changing anything about what gets elided.

        Returns None when there was nothing left to elide — which is a real
        outcome, not a failure: a run can be over its window on protected
        material alone (retrieved values and recorded results are never elided),
        and the honest answer is to say so and let the run proceed into whatever
        the provider makes of it rather than to start discarding citations.
        """
        plan = plan_compaction(
            messages,
            state=state,
            current_iteration=iteration,
            terminal_tool=terminal_tool,
        )
        if plan.empty:
            # The two triggers earn different honest sentences. A budget pass
            # that comes back empty means the run really is over its window
            # with nothing left to give — the supervisor's cue that a bigger
            # window is the only remedy (see `compaction_exhausted`, above).
            # A forced `model_switch` pass coming back empty says nothing of
            # the kind: it runs whether or not the run is anywhere near its
            # window (usually it is not), so claiming "over the context
            # budget" for it would be false on the run's own numbers.
            note = (
                "model switch: nothing eligible to elide — every result still on the "
                "transcript is protected, recent, or too short to be worth a marker"
                if trigger == "model_switch"
                else (
                    "over the context budget with nothing elidable left: what remains is "
                    "retrieved values, recorded results and instructions, none of which "
                    "may be dropped"
                )
            )
            return {
                "kind": "no_op",
                "iteration": iteration,
                "trigger": trigger,
                "before_est_tokens": before_tokens,
                "after_est_tokens": before_tokens,
                "note": note,
                "estimator": TOKEN_ESTIMATOR,
                "basis": basis,
            }

        source = elided_source_text(messages, plan)
        apply_plan(state, plan, iteration)

        # The summary is an improvement on top of the elision, never a
        # precondition for it: the space is already freed by the markers, so a
        # summarizer that cannot be reached costs detail, not the run.
        summary_model = None
        if source:
            info = self.router._resolve_router_model(max_tier)
            if info is not None:
                summary, spend = await summarize(
                    self.registry.get(info.provider),
                    info,
                    source,
                    factors=self._factors_for(info.provider, info.id, emissions),
                )
                if spend is not None:
                    overhead.append(spend)
                if summary:
                    state.summary = summary
                    summary_model = info.id

        after_tokens = estimate_wire_tokens(system, wire_view(messages, state), tool_specs)
        return {
            "kind": "elision",
            "iteration": iteration,
            "trigger": trigger,
            "before_est_tokens": before_tokens,
            "after_est_tokens": after_tokens,
            "elided_messages": len(plan.elide),
            "elided_tools": sorted(set(plan.elided_tools)),
            "summarized": bool(summary_model),
            "summarizer_model": summary_model,
            # Metered, not estimated. Accounted against the summarizer's own
            # model in `runs.overhead` rather than folded into this run's totals,
            # because it ran on a different model and possibly a different
            # provider — see services/emissions.overhead_call.
            "estimator": TOKEN_ESTIMATOR,
            "basis": basis,
        }

    async def _fail_before_start(
        self, db, run: Run, message: str, *, workspace_id: uuid.UUID | None = None
    ) -> None:
        """Fail a run that never reached the loop (bad task type, no route, ...).

        Nothing has been spent and nothing partial is pending, so this is a plain
        terminal write plus the error event the client is waiting on.

        `workspace_id`: pass this for every failure reached *after* the pre-run
        gate allowed the run (unknown task type, unknown tool, no route) —
        `check_pre_run` ran, an extension's gate may have placed a hold on this
        workspace, and its post-run hook is the only thing that releases it.
        Leave it `None` for the gate's own refusal: that run never got as far as
        a hold to release, and every other terminal path already fires the hook
        exactly once (the normal finish, and the crash handler in `execute()`),
        so this is the one call site that must not fire it a second time.
        """
        run.status = "failed"
        run.error = message
        run.finished_at = _utcnow()
        await db.commit()
        await self.bus.publish(run.id, RunEvent("error", {"message": message}))
        if workspace_id is not None:
            await get_extension_registry().run_post_run_hooks(db, run, workspace_id)

    @staticmethod
    def _completion_status(ctx: RunContext) -> str:
        """`completed`, or `completed_without_output` if the verdict never landed.

        Covers the declared-`terminal_tool` contract only; the natural end of
        the loop goes through `_final_status`, which also catches the no-text
        case for tasks without one. Callers of a run should treat
        `completed_without_output` as "no result to consume", not as an error.
        """
        if ctx.terminal_tool and not ctx.terminal_recorded:
            return STATUS_COMPLETED_WITHOUT_OUTPUT
        return STATUS_COMPLETED

    @staticmethod
    def _final_status(ctx: RunContext, final_text: str) -> str:
        """Status for the loop's natural end (an assistant turn with no tool calls).

        A chat/freeform turn's answer *is* its text, so a run whose final turn
        carries no text has produced nothing to consume — `completed` here would
        present silence as success, render an empty chat bubble, and score the
        model a clean delivery in the routing track record.
        """
        status = HarnessEngine._completion_status(ctx)
        if status == STATUS_COMPLETED and not ctx.terminal_tool and not final_text.strip():
            return STATUS_COMPLETED_WITHOUT_OUTPUT
        return status

    @staticmethod
    def _spec_for(tool_specs, name):
        return next((t for t in tool_specs if t.name == name), None)

    async def _publish_tool_calls(self, run_id: uuid.UUID, tool_calls: list[ToolCall]) -> None:
        for tc in tool_calls:
            await self.bus.publish(
                run_id,
                RunEvent("tool_call", {"tool": tc.name, "id": tc.id, "arguments": tc.arguments}),
            )


async def _unknown_tool(name: str) -> tuple[str, bool]:
    return f"Tool error: '{name}' is not an enabled tool for this run.", True


_engine: HarnessEngine | None = None


def get_harness_engine() -> HarnessEngine:
    global _engine
    if _engine is None:
        _engine = HarnessEngine()
    return _engine
