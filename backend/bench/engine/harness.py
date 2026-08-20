"""HarnessEngine: the agent loop.

One entry point, `execute(run_id)`, designed to run as a background task. It
loads the run, assembles context, routes the model, executes the tool loop,
persists the transcript/cost after every iteration, and publishes RunEvents.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select

from bench.db.engine import get_session_factory
from bench.db.models import Document, Harness, Pack, Run
from bench.adaptive import adaptive_of
from bench.engine.compaction import (
    CompactionState,
    apply_plan,
    elided_source_text,
    estimate_wire_tokens,
    over_budget,
    plan_compaction,
    summarize,
    trim_history,
    wire_view,
)
from bench.engine.compaction import budget as context_budget
from bench.engine.context import (
    TOKEN_ESTIMATOR,
    assemble_context,
    block_for,
    build_user_message,
    composition_report,
    task_config,
    tool_spec_block,
)
from bench.engine.events import RunEvent, get_event_bus
from bench.engine.tools import (
    DELEGATION_DEPTH_KEY,
    WEB_TOOL_NAMES,
    RunContext,
    execute_tool,
    get_builtin_tools,
    withheld_web_tools,
)
from bench.providers.base import (
    Msg,
    ProviderError,
    TextDelta,
    ToolCall,
    ToolCallComplete,
    TurnComplete,
    Usage,
)
from bench.providers.catalog import (
    ModelCatalog,
    ModelInfo,
    ProviderRegistry,
    energy_accounting,
    get_catalog,
)
from bench.router_llm.priors import OutcomePriors, PriorsProvider
from bench.router_llm.router import ModelRouter, RoutingUnavailable
from bench.engine.supervisor import (
    Intervention,
    TurnState,
    assess,
    normalize_for_provider,
)
from bench.services.emissions import (
    combine_accountings,
    overhead_block,
    emission_event_fields,
    energy_wh_field,
)
from bench.services.outcomes import record_outcome
from bench.services.transcript import (
    ENGINE_NUDGE_KEY,
    NUDGE_OUTPUT_BUDGET,
    NUDGE_TERMINAL,
    REPEATED_CALL_KEY,
)

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

    def add(self, usage: Usage, iteration: int) -> None:
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

    def accounting(self) -> dict:
        return energy_accounting(
            self.model,
            self.usage.input_tokens,
            self.usage.output_tokens,
            self.usage.cache_read_tokens,
            self.usage.cache_write_tokens,
        )

    def to_json(self) -> dict:
        accounting = self.accounting()
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
            "cost_usd": float(self.cost_usd),
            "energy_wh": accounting["energy_wh"],
            # The full per-model derivation, kept segment by segment. The
            # run-level roll-up nulls whatever the segments disagreed on, so this
            # is where the un-nulled detail survives.
            "energy_accounting": accounting,
        }


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

    def cancel(self, run_id: uuid.UUID) -> None:
        self._cancelled.add(run_id)

    async def execute(self, run_id: uuid.UUID) -> None:
        async with get_session_factory()() as db:
            run = await db.get(Run, run_id)
            if run is None:
                return
            # Rebuild registry/router per run so DB-stored keys (settings UI)
            # are honored alongside env keys.
            from bench.services.credentials import load_db_keys

            self.registry = ProviderRegistry(await load_db_keys(db))
            self.router = ModelRouter(self.catalog, self.registry, self.priors)
            try:
                await self._execute_inner(db, run)
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

    async def _execute_inner(self, db, run: Run) -> None:
        harness = await db.get(Harness, run.harness_id)
        pack = await db.get(Pack, run.pack_id) if run.pack_id else None
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
                )
                return
            task = {}
        output_schemas: dict[str, dict] = (pack.manifest.get("schemas", {}) if pack else {})

        loop_cfg = {**(harness.loop_config or {})}
        requested_iterations = int(loop_cfg.get("max_iterations", DEFAULT_MAX_ITERATIONS))
        max_iterations = max(1, min(requested_iterations, MAX_ITERATIONS_CEILING))
        max_output_tokens = int(loop_cfg.get("max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS))
        temperature = float(loop_cfg.get("temperature", DEFAULT_TEMPERATURE))
        max_cost = Decimal(str(loop_cfg.get("max_cost_usd", DEFAULT_MAX_COST_USD)))
        model_policy = effective_model_policy(harness.model_policy, run.task_input)
        budget_raw = model_policy.get("max_run_output_tokens")
        output_budget = int(budget_raw) if budget_raw else 0

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
            )
            return
        # Registered, but withheld. `web_search`/`fetch_url` are always in the
        # registry — "read one file to see everything an agent can do" stays true
        # only if the registry is complete — while whether they are *available*
        # is an operator switch (BENCH_EGRESS_RESEARCH). The two are separate
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
                            "(BENCH_EGRESS_RESEARCH). These tools were not offered to "
                            "the model; the run continues without them."
                        ),
                    },
                ),
            )
        tool_specs = [builtins[n] for n in enabled_names]

        # ── context, accounted ───────────────────────────────────────────────
        assembled = assemble_context(
            harness,
            pack,
            run.task_type,
            output_schemas,
            extra_context=run.task_input.get("_capabilities"),
            web_tools_enabled=any(n in WEB_TOOL_NAMES for n in enabled_names),
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
            )
        except RoutingUnavailable as e:
            await self._fail_before_start(db, run, str(e))
            return

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
            output_schemas=output_schemas,
            pack_manifest=pack.manifest if pack else None,
            pack_dir=pack.source_path if pack else None,
            terminal_tool=task.get("terminal_tool"),
            delegation_depth=int(run.task_input.get(DELEGATION_DEPTH_KEY) or 0),
        )

        # ── context budget ───────────────────────────────────────────────────
        # The chosen model's window is known only now, which is why the history
        # trim below lives here rather than in api/chat.py: that endpoint hands
        # over the last N turns with no idea how large they are or which model
        # will have to hold them.
        adaptive = adaptive_of(model_policy)
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
        segments: list[ModelSegment] = [ModelSegment(model_info, reason="initial")]
        segment = segments[0]

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
        overridden = bool(decision.override)

        # ── loop ─────────────────────────────────────────────────────────────
        for iteration in range(1, max_iterations + 1):
            if run.id in self._cancelled:
                run.status = "cancelled"
                break

            assistant_text: list[str] = []
            tool_calls: list[ToolCall] = []
            turn: TurnComplete | None = None

            # ── stay inside the window ───────────────────────────────────────
            # Checked before every call, not after a failure: a run that exceeds
            # its window gets a provider error with nothing in the transcript
            # explaining it, and by then the turn has already been paid for.
            wire = wire_view(messages, compaction)
            est_tokens = estimate_wire_tokens(system, wire, tool_specs)
            if adaptive.compaction != "off" and over_budget(est_tokens, context_limit):
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
                        },
                    ),
                )
                record = await self._compact(
                    run=run,
                    messages=messages,
                    state=compaction,
                    iteration=iteration,
                    before_tokens=est_tokens,
                    system=system,
                    tool_specs=tool_specs,
                    terminal_tool=ctx.terminal_tool,
                    max_tier=model_policy.get("max_cost_tier") or "premium",
                    overhead=overhead_calls,
                )
                if record is not None:
                    compaction_records.append(record)
                    run.compactions = list(compaction_records)
                    run.overhead = overhead_block(overhead_calls)
                    await self.bus.publish(run.id, RunEvent("compaction", record))
                    # Over the budget with only protected material left. The
                    # supervisor's cue that a bigger window is the only remedy.
                    compaction_exhausted = record["kind"] == "no_op"
                wire = wire_view(messages, compaction)

            try:
                async for event in provider.stream(
                    model=model_info.wire_id,
                    system=system,
                    messages=wire,
                    tools=tool_specs,
                    max_tokens=max_output_tokens,
                    temperature=temperature,
                ):
                    if isinstance(event, TextDelta):
                        assistant_text.append(event.text)
                        await self.bus.publish(run.id, RunEvent("text_delta", {"text": event.text}))
                    elif isinstance(event, ToolCallComplete):
                        tool_calls.append(event.tool_call)
                    elif isinstance(event, TurnComplete):
                        turn = event
            except ProviderError as e:
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
                    messages.append(
                        Msg(
                            role="assistant",
                            content=partial or None,
                            meta={
                                "iteration": iteration,
                                "partial": True,
                                "provider_error": str(e),
                                "unexecuted_tool_calls": [tc.name for tc in tool_calls],
                            },
                        )
                    )
                run.status = "failed"
                run.error = str(e)
                break

            usage = turn.usage if turn else Usage()
            total_usage.input_tokens += usage.input_tokens
            total_usage.output_tokens += usage.output_tokens
            total_usage.cache_read_tokens += usage.cache_read_tokens
            total_usage.cache_write_tokens += usage.cache_write_tokens
            # Booked against the model that actually ran the turn. A run may
            # change model part-way (see the supervisor below), and every figure
            # downstream — price, energy class, PUE, grid factor — is a property
            # of *which* model spent the tokens, not of the run as a whole.
            segment.add(usage, iteration)
            turn_cost = model_info.cost_usd(
                usage.input_tokens,
                usage.output_tokens,
                usage.cache_read_tokens,
                usage.cache_write_tokens,
            )

            messages.append(
                Msg(
                    role="assistant",
                    content="".join(assistant_text) or None,
                    tool_calls=tool_calls,
                    meta={"iteration": iteration},
                )
            )

            run.iterations = iteration
            run.input_tokens = total_usage.input_tokens
            run.output_tokens = total_usage.output_tokens
            run.cache_read_tokens = total_usage.cache_read_tokens
            run.cache_write_tokens = total_usage.cache_write_tokens
            run.cost_usd = (run.cost_usd or Decimal(0)) + turn_cost
            # Estimated energy/carbon, recomputed per segment from that segment's
            # running totals rather than accumulated per turn: the estimate is
            # linear in tokens, so a segment's total cannot drift from the sum of
            # its turns. The run-level block is the roll-up across segments,
            # which for the ordinary single-model run is byte-identical to the
            # single segment's own block (services/emissions.combine_accountings).
            accounting = combine_accountings([seg.accounting() for seg in segments])
            run.energy_wh = Decimal(str(accounting["energy_wh"]))
            run.energy_accounting = accounting
            if len(segments) > 1:
                run.model_timeline = [seg.to_json() for seg in segments]
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
                        "energy_wh": accounting["energy_wh"],
                        **emission_event_fields(accounting),
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
                run.status = self._completion_status(ctx)
                break

            if run.cost_usd >= max_cost:
                run.status = "failed"
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
                meta = {"error": is_error}
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
            if adaptive.escalation == "off" or adaptive.max_switches <= 0 or overridden:
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
                    repeated_call_trips=repeated_call_trips,
                    terminal_recorded=ctx.terminal_recorded,
                    findings_created=findings_total,
                    cost_so_far=run.cost_usd or Decimal(0),
                    max_cost_usd=max_cost,
                    switches_used=switches_used,
                    max_switches=adaptive.max_switches,
                    escalation=adaptive.escalation,
                    overridden=overridden,
                ),
                candidates=candidates,
                priors=live_priors,
            )
            if intervention.switching:
                switches_used += 1
                model_info, provider, segment = self._switch_model(
                    run=run,
                    intervention=intervention,
                    segments=segments,
                    iteration=iteration,
                )
                context_limit = context_budget(
                    model_info.context_window, max_output_tokens, adaptive.context_headroom
                )
                ctx.model_used = model_info.id
                messages = normalize_for_provider(messages, model_info.provider)
                # The cache is void from here: a different model has never seen
                # this prefix, so the next turn re-pays full input price. The
                # supervisor priced that in before choosing to switch.
                compaction_exhausted = False
                consecutive_terminal_failures = 0
                repeated_call_trips = 0
                await self.bus.publish(
                    run.id, RunEvent("model_switch", run.routing["switches"][-1])
                )
            elif intervention.refused:
                # A run that was stuck and that bench decided not to rescue is
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

        # ── finish ───────────────────────────────────────────────────────────
        if run.status == "running":
            run.status = self._completion_status(ctx)
        run.messages = [m.to_json() for m in messages]
        run.overhead = overhead_block(overhead_calls)
        run.finished_at = _utcnow()
        # Evidence for the next routing decision, folded into the run's own final
        # commit. `record_outcome` never raises and returns None for runs that
        # carry no lesson (cancelled, or never routed) — see services/outcomes.py.
        await record_outcome(db, run)
        await db.commit()
        # This run is now evidence, and the cached aggregate predates it. Cheap
        # to drop and the alternative is a bad look: a run finishing badly, and
        # the very next run of the same shape routing as though it had not.
        self.priors.invalidate()
        self._cancelled.discard(run.id)
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

    def _switch_model(
        self,
        *,
        run: Run,
        intervention: Intervention,
        segments: list[ModelSegment],
        iteration: int,
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
        segments.append(ModelSegment(target, reason=intervention.reason))
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
        run.model_timeline = [seg.to_json() for seg in segments]
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
    ) -> dict | None:
        """Shrink what the provider sees, and say exactly what was shrunk.

        `messages` is not modified. Compaction produces a wire view; the
        transcript stays the complete record of what happened, and the dict
        returned here is what states the difference (see engine/compaction.py).

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
            return {
                "kind": "no_op",
                "iteration": iteration,
                "before_est_tokens": before_tokens,
                "after_est_tokens": before_tokens,
                "note": (
                    "over the context budget with nothing elidable left: what remains is "
                    "retrieved values, recorded results and instructions, none of which "
                    "may be dropped"
                ),
                "estimator": TOKEN_ESTIMATOR,
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
                    self.registry.get(info.provider), info, source
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
        }

    async def _fail_before_start(self, db, run: Run, message: str) -> None:
        """Fail a run that never reached the loop (bad task type, no route).

        Nothing has been spent and nothing partial is pending, so this is a plain
        terminal write plus the error event the client is waiting on.
        """
        run.status = "failed"
        run.error = message
        run.finished_at = _utcnow()
        await db.commit()
        await self.bus.publish(run.id, RunEvent("error", {"message": message}))

    @staticmethod
    def _completion_status(ctx: RunContext) -> str:
        """`completed`, or `completed_without_output` if the verdict never landed.

        Only tasks that declare a `terminal_tool` can end without output: a
        freeform or chat turn's answer *is* its text, so there is nothing to
        detect. Callers of a run should treat this as "no result to consume",
        not as an error.
        """
        if ctx.terminal_tool and not ctx.terminal_recorded:
            return STATUS_COMPLETED_WITHOUT_OUTPUT
        return STATUS_COMPLETED

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
