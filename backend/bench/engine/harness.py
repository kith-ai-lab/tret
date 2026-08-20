"""HarnessEngine: the agent loop.

One entry point, `execute(run_id)`, designed to run as a background task. It
loads the run, assembles context, routes the model, executes the tool loop,
persists the transcript/cost after every iteration, and publishes RunEvents.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select

from bench.db.engine import get_session_factory
from bench.db.models import Document, Harness, Pack, Run
from bench.engine.context import (
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
    ProviderRegistry,
    energy_accounting,
    get_catalog,
)
from bench.router_llm.router import ModelRouter, RoutingUnavailable
from bench.services.emissions import emission_event_fields, energy_wh_field

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


class HarnessEngine:
    def __init__(self, registry: ProviderRegistry | None = None, catalog: ModelCatalog | None = None):
        self.catalog = catalog or get_catalog()
        self.registry = registry or ProviderRegistry()
        self.router = ModelRouter(self.catalog, self.registry)
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
            self.router = ModelRouter(self.catalog, self.registry)
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
        if withheld:
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

        # Chat turns carry prior conversation turns as history.
        history = [Msg.from_json(m) for m in run.task_input.get("_history", [])]
        messages: list[Msg] = [*history, Msg(role="user", content=user_message)]
        total_usage = Usage()
        nudged = False
        budget_nudged = False
        seen_calls: dict[str, int] = {}  # repeated-identical-call breaker

        # ── loop ─────────────────────────────────────────────────────────────
        for iteration in range(1, max_iterations + 1):
            if run.id in self._cancelled:
                run.status = "cancelled"
                break

            assistant_text: list[str] = []
            tool_calls: list[ToolCall] = []
            turn: TurnComplete | None = None

            try:
                async for event in provider.stream(
                    model=model_info.wire_id,
                    system=system,
                    messages=messages,
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
            # Estimated energy/carbon, recomputed from the running totals rather
            # than accumulated per turn: the estimate is linear in tokens, so
            # totals cannot drift from the sum of the turns.
            accounting = energy_accounting(
                model_info,
                total_usage.input_tokens,
                total_usage.output_tokens,
                total_usage.cache_read_tokens,
                total_usage.cache_write_tokens,
            )
            run.energy_wh = Decimal(str(accounting["energy_wh"]))
            run.energy_accounting = accounting
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
                if seen_calls[call_key] >= 3 and not is_error:
                    result_text += (
                        "\n\n[NOTE: you have now made this exact call "
                        f"{seen_calls[call_key]} times and the result is unchanged. You have "
                        "the data you need — proceed to your terminal action "
                        f"({ctx.terminal_tool or 'your final answer'}) now.]"
                    )
                messages.append(
                    Msg(role="tool", content=result_text, tool_call_id=tc.id, meta={"error": is_error})
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
        run.finished_at = _utcnow()
        await db.commit()
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
