"""HarnessEngine: the agent loop.

One entry point, `execute(run_id)`, designed to run as a background task. It
loads the run, assembles context, routes the model, executes the tool loop,
persists the transcript/cost after every iteration, and publishes RunEvents.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select

from bench.db.engine import get_session_factory
from bench.db.models import Document, Harness, Pack, Run
from bench.engine.context import assemble_system_prompt, build_user_message
from bench.engine.events import RunEvent, get_event_bus
from bench.engine.tools import RunContext, execute_tool, get_builtin_tools
from bench.providers.base import (
    Msg,
    ProviderError,
    TextDelta,
    ToolCall,
    ToolCallComplete,
    TurnComplete,
    Usage,
)
from bench.providers.catalog import ModelCatalog, ProviderRegistry, get_catalog
from bench.router_llm.router import ModelRouter, RoutingUnavailable

DEFAULT_MAX_ITERATIONS = 24
DEFAULT_MAX_OUTPUT_TOKENS = 8192
DEFAULT_TEMPERATURE = 0.2
DEFAULT_MAX_COST_USD = Decimal("5.0")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _task_config(pack: Pack | None, task_type: str) -> dict:
    if pack is not None:
        for t in pack.manifest.get("task_types", []):
            if t["slug"] == task_type:
                return t
    return {"slug": task_type, "shape": "freeform", "display_name": task_type}


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

        task = _task_config(pack, run.task_type)
        output_schemas: dict[str, dict] = (pack.manifest.get("schemas", {}) if pack else {})

        loop_cfg = {**(harness.loop_config or {})}
        max_iterations = int(loop_cfg.get("max_iterations", DEFAULT_MAX_ITERATIONS))
        max_output_tokens = int(loop_cfg.get("max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS))
        temperature = float(loop_cfg.get("temperature", DEFAULT_TEMPERATURE))
        max_cost = Decimal(str(loop_cfg.get("max_cost_usd", DEFAULT_MAX_COST_USD)))

        system = assemble_system_prompt(harness, pack, run.task_type, output_schemas)
        user_message = build_user_message(run, pack, documents)

        # ── route ────────────────────────────────────────────────────────────
        est_input_tokens = (len(system) + len(user_message)) // 4
        try:
            decision = await self.router.route(
                model_policy=harness.model_policy or {"mode": "auto"},
                task_type=run.task_type,
                task_shape=task.get("shape", "freeform"),
                task_description=task.get("display_name", run.task_type),
                output_contract=task.get("output_contract", "free text"),
                n_documents=len(documents),
                est_input_tokens=est_input_tokens,
                run_override=run.task_input.get("_model_override"),
            )
        except RoutingUnavailable as e:
            run.status = "failed"
            run.error = str(e)
            run.finished_at = _utcnow()
            await db.commit()
            await self.bus.publish(run.id, RunEvent("error", {"message": run.error}))
            return

        model_info = self.catalog.get(decision.chosen_model)
        run.routing = decision.to_json()
        run.model_used = decision.chosen_model
        run.provider_used = model_info.provider
        run.status = "running"
        run.started_at = _utcnow()
        run.doctrine_sha = pack.doctrine_sha if pack else None
        await db.commit()
        await self.bus.publish(run.id, RunEvent("routing", decision.to_json()))

        provider = self.registry.get(model_info.provider)

        # ── tools ────────────────────────────────────────────────────────────
        builtins = get_builtin_tools()
        enabled_names = list(task.get("tools") or harness.tool_names or [])
        if run.task_type == "freeform" and not enabled_names:
            enabled_names = ["read_document", "search_documents", "lookup_dataset", "list_prior_findings"]
        tool_specs = [builtins[n] for n in enabled_names if n in builtins]

        ctx = RunContext(
            db=db,
            run_id=run.id,
            project_id=run.project_id,
            pack_id=run.pack_id,
            doctrine_sha=run.doctrine_sha,
            model_used=run.model_used,
            document_ids=list(run.document_ids or []),
            output_schemas=output_schemas,
            terminal_tool=task.get("terminal_tool"),
        )

        messages: list[Msg] = [Msg(role="user", content=user_message)]
        total_usage = Usage()
        nudged = False

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
                run.status = "failed"
                run.error = str(e)
                break

            usage = turn.usage if turn else Usage()
            total_usage.input_tokens += usage.input_tokens
            total_usage.output_tokens += usage.output_tokens
            total_usage.cache_read_tokens += usage.cache_read_tokens
            turn_cost = model_info.cost_usd(usage.input_tokens, usage.output_tokens)

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
            run.cost_usd = (run.cost_usd or Decimal(0)) + turn_cost
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
                        "cost_usd": float(run.cost_usd),
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
                run.status = "completed"
                break

            if run.cost_usd >= max_cost:
                run.status = "failed"
                run.error = f"cost_cap_exceeded: run cost ${run.cost_usd} >= cap ${max_cost}"
                break

            # Execute tool calls in parallel.
            await self._publish_tool_calls(run.id, tool_calls)
            results = await asyncio.gather(
                *[
                    execute_tool(ctx, self._spec_for(tool_specs, tc.name), tc.arguments)
                    if self._spec_for(tool_specs, tc.name)
                    else _unknown_tool(tc.name)
                    for tc in tool_calls
                ]
            )
            for tc, (result_text, is_error) in zip(tool_calls, results):
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
            await db.commit()
        else:
            run.status = "failed"
            run.error = f"max_iterations ({max_iterations}) reached without completion"

        # ── finish ───────────────────────────────────────────────────────────
        if run.status == "running":
            run.status = "completed"
        run.messages = [m.to_json() for m in messages]
        run.finished_at = _utcnow()
        await db.commit()
        self._cancelled.discard(run.id)
        if run.status == "completed":
            await self.bus.publish(
                run.id,
                RunEvent(
                    "done",
                    {
                        "status": run.status,
                        "cost_usd": float(run.cost_usd or 0),
                        "iterations": run.iterations,
                    },
                ),
            )
        else:
            await self.bus.publish(
                run.id, RunEvent("error", {"message": run.error or run.status, "status": run.status})
            )

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
