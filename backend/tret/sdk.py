"""tret's public SDK surface.

    from tret import Router
    result = Router().run(task)
    result.receipt  # $ and gCO2e, per call

`Router` routes a plain-language task to the optimal model via the existing
`ModelRouter`, executes exactly one model call — no tools, no multi-turn
engine loop — and returns the model's text plus a `Receipt`: what that one
call cost, in dollars and estimated carbon, and how that compares to the
frontier-baseline counterfactual (`tret.services.emissions`).

This module is on the pip-installable core path (`pip install tret`, no
`[server]` extra): it must never import anything that pulls in a server
dependency (SQLAlchemy, FastAPI, ...). `tests/test_sdk_import_hygiene.py`
enforces that in a subprocess, with a fresh interpreter.
"""
from __future__ import annotations

import asyncio
import copy
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, TypeVar
from uuid import uuid4

from tret.adaptive import adaptive_of
from tret.engine.compaction import required_context_window
from tret.providers.base import Msg, Provider, TextDelta, ToolCallComplete, TurnComplete, Usage
from tret.providers.catalog import ModelCatalog, ModelInfo, ProviderRegistry, get_catalog
from tret.router_llm.objectives import DEFAULT_MAX_COST_TIER, DEFAULT_OBJECTIVE, OBJECTIVES
from tret.router_llm.priors_base import NoPriors
from tret.router_llm.router import (
    TIER_ORDER,
    ModelRouter,
    RoutingDecision,
    RoutingUnavailable,
)
from tret.services.emissions import energy_accounting

if TYPE_CHECKING:
    # Type-only: `tret.services.emission_factors` is itself core-safe (no
    # SQLAlchemy/FastAPI import), but there is no runtime need to import it
    # here — a caller builds the `FactorSet` it hands to `arun`/`run` however
    # it likes (a server-side caller would go through
    # `tret.services.emission_settings.factor_set_for`, which is not
    # core-safe and stays off this module's import path).
    from tret.services.emission_factors import FactorSet

__all__ = ["Receipt", "Router", "RunResult"]

_T = TypeVar("_T")


def _validate_measurement(wh: float | None, boundary: str) -> None:
    if boundary not in {"gpu", "node_it", "facility", "partial", "unknown"}:
        raise ValueError(f"unsupported measured_energy_boundary: {boundary!r}")
    if wh is not None and (isinstance(wh, bool) or not math.isfinite(wh) or wh < 0):
        raise ValueError("measured_energy_wh must be finite and >= 0")

# A minimal freeform preamble, in the spirit of `engine/context.py`'s
# FREEFORM_PREAMBLE — reimplemented here, rather than imported, because
# engine/context.py is not on the core (server-free) import path and this
# module has to stay off it too.
_DEFAULT_SYSTEM = (
    "You are a careful, direct assistant. Read the request and respond to it "
    "precisely and concisely, in plain text."
)

# How much of the task the router's own prompt is shown. The router only needs
# enough of the task to classify it, not the whole thing — and the router's
# prompt is itself a cost (see RoutingDecision.spend).
_TASK_DESCRIPTION_CHARS = 200


def _run_sync(coro_fn: Callable[[], Awaitable[_T]]) -> _T:
    """Run `coro_fn()` to completion, refusing to nest inside a live loop.

    `coro_fn` is a thunk rather than an already-built coroutine so that, on the
    rejection path, no coroutine object is ever created — an un-awaited one
    would otherwise leak a "coroutine was never awaited" warning on exactly
    the call that was refused.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError(
            "Router.run() cannot be called from a running event loop; "
            "use `await Router().arun(...)`"
        )
    return asyncio.run(coro_fn())


@dataclass(frozen=True)
class Receipt:
    """What one model call cost, and what it would have cost the frontier.

    `None` always means "estimate unavailable" — never 0. A receipt whose
    carbon fields are all `None` is not a receipt for a free run; it is one
    where no baseline or grid factor could be resolved (see
    `tret.services.emissions.energy_accounting`) — or, for `usd`/`co2e_g`/
    `energy_wh` specifically, one where the provider never reported usage at
    all (no `TurnComplete`, or an all-zero `Usage`): some local OpenAI-compat
    servers ignore `stream_options.include_usage` entirely, and a confident
    "$0.0000 · 0.00 gCO₂e" for that call would be a fabricated number, not an
    estimate.
    """

    model: str  # tret model id, e.g. "anthropic/claude-haiku-4-5"
    usd: float | None
    co2e_g: float | None
    energy_wh: float | None
    baseline_model: str | None  # the frontier counterfactual, when available
    avoided_usd: float | None  # signed; negative = dearer than the baseline
    avoided_usd_pct: float | None
    avoided_co2e_g: float | None
    avoided_co2e_pct: float | None
    usage: dict
    routing: dict  # {chosen_model, reasoning, candidates, fallback_used, objective}
    # The router's own spend, kept separate deliberately — see RoutingDecision.spend
    # / tret.services.emissions.overhead_call. None when no router call was made (a
    # pinned model, or only one candidate to begin with). Keys, when present:
    # {kind, model, provider, input_tokens, output_tokens, cache_read_tokens,
    #  cache_write_tokens, cost_usd, energy_wh, energy_accounting}.
    overhead: dict | None
    raw: dict  # the full energy_accounting() dict, untouched
    # True when `usage` above was not reported by the provider but guessed —
    # from what was actually sent and what streamed back before a mid-turn
    # `ProviderError` (see `local_run._run_agentic_loop`). Priced the same way
    # a metered turn is (same `raw`/`usd` derivation), so a run that failed
    # mid-stream still gets a receipt for what it burned rather than a silent
    # $0.0000 — this is the flag that keeps that receipt from being mistaken
    # for a confidently metered one. Defaults False so every existing caller
    # (`Router.arun`, which has no partial-turn case to estimate) is unaffected.
    estimated: bool = False
    call_records: list[dict] = field(default_factory=list)

    def __str__(self) -> str:
        short_model = self.model.rsplit("/", 1)[-1]  # display form, not the full tret id
        if self.usd is None:
            # No reliable usage to price at all — see the class docstring.
            return f"receipt · estimate unavailable · {short_model}"
        usd_segment = f"${self.usd:.4f}" + (" (estimated)" if self.estimated else "")
        if self.overhead is not None and self.overhead.get("cost_usd") is not None:
            usd_segment += f" (+${self.overhead['cost_usd']:.4f} routing)"
        segments = ["receipt", usd_segment]
        if self.co2e_g is not None:
            segments.append(f"{self.co2e_g:.2f} gCO₂e")
        segments.append(short_model)
        return " · ".join(segments)


@dataclass(frozen=True)
class RunResult:
    text: str
    model: str
    receipt: Receipt
    stop_reason: str


class Router:
    """Routes a task to a model and (optionally) runs it, in one call.

    Instance-cached, not thread-safe: the catalog, provider registry and
    underlying `ModelRouter` are built lazily on first use and then reused —
    fine for one router driving one event loop, not promised safe to share
    across threads.
    """

    def __init__(
        self,
        *,
        objective: str = DEFAULT_OBJECTIVE,
        max_cost_tier: str = DEFAULT_MAX_COST_TIER,
        allowed: list[str] | None = None,
        model: str | None = None,
        temperature: float = 0.2,
    ) -> None:
        # A confidentiality boundary, not just a spending one: max_cost_tier
        # "local" is documented (docs/local-models.md) as "no cloud provider may
        # be chosen". `_max_cost_tier` in router_llm/router.py normalizes any
        # unrecognized value to the *top* tier rather than rejecting it — right
        # for a policy dict that has already been validated at the API boundary,
        # wrong here, where a typo'd tier is the caller's only chance to catch a
        # cap that silently failed to apply. So: fail loudly, before it can ever
        # reach that permissive normalization.
        if max_cost_tier not in TIER_ORDER:
            valid = ", ".join(sorted(TIER_ORDER, key=TIER_ORDER.__getitem__))
            raise ValueError(
                f"Router(max_cost_tier={max_cost_tier!r}) is not valid; "
                f"choose one of: {valid}"
            )
        if objective not in OBJECTIVES:
            valid = ", ".join(OBJECTIVES)
            raise ValueError(
                f"Router(objective={objective!r}) is not valid; choose one of: {valid}"
            )
        self._objective = objective
        self._max_cost_tier = max_cost_tier
        self._allowed = allowed
        self._model = model
        self._temperature = temperature
        self._catalog: ModelCatalog | None = None
        self._registry: ProviderRegistry | None = None
        self._model_router: ModelRouter | None = None

    def _model_policy(self) -> dict:
        return {
            "mode": "pinned" if self._model else "auto",
            "model": self._model,
            "objective": self._objective,
            "allowed": self._allowed,
            "max_cost_tier": self._max_cost_tier,
        }

    def _ensure_wired(self) -> ModelRouter:
        if self._model_router is None:
            self._catalog = get_catalog()
            self._registry = ProviderRegistry()
            # No evidence: an SDK caller has no run history to learn from, and
            # every routing decision must be reproducible from the policy alone.
            self._model_router = ModelRouter(self._catalog, self._registry, NoPriors())
        return self._model_router

    async def aroute(self, task: str) -> RoutingDecision:
        """Routing only — decides the model, executes nothing."""
        router = self._ensure_wired()
        return await router.route(
            model_policy=self._model_policy(),
            task_type="freeform",
            task_shape="freeform",
            task_description=task[:_TASK_DESCRIPTION_CHARS],
            output_contract="free text",
            n_documents=0,
            est_input_tokens=max(1, len(task) // 4),
            # No `max_output_tokens` at this call site — routing-only, nothing
            # is ever executed here — so there is nothing to size a window
            # against; the context-fit filter is skipped (`min_context_window`
            # left None), same as every caller before this parameter existed.
        )

    async def arun(
        self,
        task: str,
        *,
        system: str | None = None,
        max_tokens: int = 4096,
        factors: "FactorSet | None" = None,
        measured_energy_wh: float | None = None,
        measured_energy_boundary: str = "node_it",
    ) -> RunResult:
        """Route `task` to a model, run it once, and return its `Receipt`.

        `factors` — a `tret.services.emission_factors.FactorSet` — is passed
        straight through to `energy_accounting()`, the same layered factor
        set (grid intensity, PUE, embodied hardware, the uncertainty band,
        the baseline model) a server-side run snapshots at its own start. Left
        `None` (the default, and every caller before this parameter existed),
        `energy_accounting` resolves its own from `Settings` alone, exactly as
        it always has.

        `measured_energy_wh` — a caller's own metered IT-load figure for this
        one call, in Wh — is passed straight through to `energy_accounting`'s
        own `measured_energy_wh` parameter: it replaces the per-token
        estimate for `energy_wh`, keeps the estimate as `energy_wh_estimated`,
        and flips `energy_source` to `"measured"` on the returned `Receipt`.
        See `energy_accounting`'s own docstring and
        docs/emissions-methodology.md's "Measured energy" section for exactly
        what changes and what does not (PUE, grid intensity and embodied
        hardware depend on the declared boundary). `measured_energy_boundary`
        defaults to `node_it`; use `facility` for facility-inclusive energy,
        which receives no additional PUE. GPU-only readings use `gpu` and
        retain missing-node coverage. Left `None` (the default),
        this call prices from tokens exactly as it always has. Must be `>=
        0`; a negative value raises `ValueError`, same as `energy_accounting`
        itself.
        """
        _validate_measurement(measured_energy_wh, measured_energy_boundary)
        router = self._ensure_wired()
        assert self._catalog is not None and self._registry is not None  # set by _ensure_wired
        # Best-effort dynamic + local discovery, so a freshly booted process can
        # still route to a model that only the catalog's discovery pass knows
        # about. `route()` also calls this, but doing it here too costs nothing
        # (it is a once-per-process no-op after the first call) and means
        # `aroute()`-then-`arun()` and a bare `arun()` see the same catalog.
        await self._catalog.warm_once()

        est_input_tokens = max(1, len(task) // 4)
        decision = await router.route(
            model_policy=self._model_policy(),
            task_type="freeform",
            task_shape="freeform",
            task_description=task[:_TASK_DESCRIPTION_CHARS],
            output_contract="free text",
            n_documents=0,
            est_input_tokens=est_input_tokens,
            # `max_tokens` (this call's own output reservation) and the
            # policy's headroom are both known here — unlike `aroute()`,
            # which never executes anything — so the router can be told the
            # smallest window this call needs, same as harness.py.
            min_context_window=required_context_window(
                est_input_tokens, max_tokens, adaptive_of(self._model_policy()).context_headroom
            ),
        )

        model = self._catalog.get(decision.chosen_model)
        if model is None:
            raise RoutingUnavailable(
                f"Routed to '{decision.chosen_model}', which is no longer in the catalog."
            )
        provider: Provider = self._registry.get(model.provider)

        text_parts: list[str] = []
        usage = Usage()
        stop_reason = ""
        turn_complete_seen = False
        served_by: str | None = None
        inference_geo: str | None = None
        call_started_at = datetime.now(timezone.utc)
        async for event in provider.stream(
            model=model.wire_id,
            system=system if system is not None else _DEFAULT_SYSTEM,
            messages=[Msg(role="user", content=task)],
            tools=[],  # exactly one call, no tool loop
            max_tokens=max_tokens,
            temperature=self._temperature,
            # Recorded on every decision (`RoutingDecision.effort`) but only
            # sent when this model's own catalog entry says it accepts the
            # control — same gate `engine/harness.py` applies before its own
            # `provider.stream()` call.
            effort=decision.effort if model.supports_effort else None,
            # One id per call: this path has no run/ledger id to reuse (it is
            # a single untracked call, unlike `local_run.arun`'s ledger entry
            # or a server run's `run.id`), so a fresh one stands in — enough
            # for a provider with sticky routing (OpenRouter) to key on.
            session_id=str(uuid4()),
        ):
            if isinstance(event, TextDelta):
                text_parts.append(event.text)
            elif isinstance(event, TurnComplete):
                usage = event.usage
                stop_reason = event.stop_reason
                turn_complete_seen = True
                served_by = event.served_by
                inference_geo = event.inference_geo
            elif isinstance(event, ToolCallComplete):
                # No tools were offered; a call that arrives anyway is ignored
                # rather than acted on.
                continue

        # Some OpenAI-compat servers ignore stream_options.include_usage and
        # never report real numbers; when that happens `usage` is either never
        # set (no TurnComplete at all) or arrives all-zero. Either way there is
        # nothing honest to price or weigh — see the Receipt docstring.
        usage_reported = turn_complete_seen and not _usage_is_empty(usage)
        receipt = _build_receipt(
            model,
            usage,
            decision,
            self._catalog,
            usage_reported,
            factors=factors,
            measured_energy_wh=measured_energy_wh,
            measured_energy_boundary=measured_energy_boundary,
            call_records=[
                {
                    "iteration": 1,
                    "started_at": call_started_at.isoformat(),
                    "ended_at": datetime.now(timezone.utc).isoformat(),
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                    "cache_read_tokens": usage.cache_read_tokens,
                    "cache_write_tokens": usage.cache_write_tokens,
                    "reasoning_tokens": usage.reasoning_tokens,
                    "reasoning_accounting": usage.reasoning_accounting,
                    "served_by": served_by,
                    "inference_geo": inference_geo,
                    "usage_status": "reported" if usage_reported else "missing",
                }
            ],
        )
        return RunResult(
            text="".join(text_parts),
            model=model.id,
            receipt=receipt,
            stop_reason=stop_reason,
        )

    def route(self, task: str) -> RoutingDecision:
        return _run_sync(lambda: self.aroute(task))

    def run(
        self,
        task: str,
        *,
        system: str | None = None,
        max_tokens: int = 4096,
        factors: "FactorSet | None" = None,
        measured_energy_wh: float | None = None,
        measured_energy_boundary: str = "node_it",
    ) -> RunResult:
        """Synchronous `arun()` — see its docstring for `factors` and
        `measured_energy_wh`."""
        return _run_sync(
            lambda: self.arun(
                task,
                system=system,
                max_tokens=max_tokens,
                factors=factors,
                measured_energy_wh=measured_energy_wh,
                measured_energy_boundary=measured_energy_boundary,
            )
        )


def _usage_is_empty(usage: Usage) -> bool:
    """True when a `Usage` carries no tokens in any bucket at all."""
    return not (
        usage.input_tokens
        or usage.output_tokens
        or usage.cache_read_tokens
        or usage.cache_write_tokens
    )


def _build_receipt(
    model: ModelInfo,
    usage: Usage,
    decision: RoutingDecision,
    catalog: ModelCatalog,
    usage_reported: bool,
    *,
    estimated: bool = False,
    factors: "FactorSet | None" = None,
    measured_energy_wh: float | None = None,
    measured_energy_boundary: str = "node_it",
    call_records: list[dict] | None = None,
) -> Receipt:
    # `raw` is computed either way — even on a zero/unreported usage it is a
    # faithful account of exactly the tokens the provider gave us, and stays
    # available for inspection. Only the Receipt's own headline fields, which
    # a caller would otherwise read as a confident measurement, are withheld.
    needs_per_call = bool(call_records) and (
        len(call_records or ()) > 1
        or any(record.get("served_by") for record in call_records or ())
        or any(record.get("reasoning_accounting") == "additional" for record in call_records or ())
        or any(record.get("started_at") for record in call_records or ())
    )
    if needs_per_call and measured_energy_wh is None:
        from tret.services.emission_calls import account_call_records
        from tret.services.emission_factors import build_factor_set

        call_factors = factors or build_factor_set(provider=model.provider, model_id=model.id)
        accounting = account_call_records(
            model, call_records, billed_usage=usage, factors=call_factors, catalog=catalog,
        )
    else:
        accounting = None
    if accounting is None:
        known_additional = sum(
            record["reasoning_tokens"] for record in call_records or []
            if record.get("reasoning_accounting") == "additional"
            and type(record.get("reasoning_tokens")) is int
            and record["reasoning_tokens"] >= 0
        )
        accounting = energy_accounting(
            model,
            usage.input_tokens,
            usage.output_tokens,
            usage.cache_read_tokens,
            usage.cache_write_tokens,
            catalog=catalog,
            factors=factors,
            measured_energy_wh=measured_energy_wh,
            measured_energy_boundary=measured_energy_boundary,
            energy_output_tokens=max(usage.energy_output_tokens, usage.output_tokens + known_additional),
            reasoning_tokens=usage.reasoning_tokens,
            reasoning_accounting=usage.reasoning_accounting,
        )
    if usage_reported:
        # Computed directly from the model's own price table — the exact figure
        # a caller comparing against `ModelInfo.cost_usd(...)` themselves would
        # get — rather than read back off `accounting["cost"]["usd"]`, which is
        # rounded for persistence.
        usd = float(
            model.cost_usd(
                usage.input_tokens,
                usage.output_tokens,
                usage.cache_read_tokens,
                usage.cache_write_tokens,
            )
        )
        cost_block = accounting["cost"]
        baseline = accounting["baseline"]
        co2e_g = accounting["co2e_g"]
        energy_wh = accounting["energy_wh"]
        baseline_model = baseline.get("model")
        avoided_usd = cost_block["avoided_usd"]
        avoided_usd_pct = cost_block["avoided_pct"]
        avoided_co2e_g = baseline.get("avoided_co2e_g")
        avoided_co2e_pct = baseline.get("avoided_pct")
    else:
        usd = None
        co2e_g = accounting["co2e_g"] if measured_energy_wh is not None else None
        energy_wh = accounting["energy_wh"] if measured_energy_wh is not None else None
        baseline_model = None
        avoided_usd = avoided_usd_pct = avoided_co2e_g = avoided_co2e_pct = None
        accounting["usage_coverage"] = "unavailable_or_incomplete"
        accounting["caveats"].append({
            "key": "usage_coverage_missing", "label": "Provider token usage is incomplete",
            "direction": "unknown", "applies": True,
            "note": "Supplied energy remains recorded; billing and same-token comparisons are unavailable.",
        })
        # Missing usage does not support a zero-token counterfactual, even if
        # independently measured energy is available.
        for key in ("cost_usd", "energy_wh", "energy_wh_total", "co2e_g", "avoided_usd",
                    "avoided_usd_pct", "avoided_co2e_g", "avoided_pct"):
            accounting["baseline"][key] = None
        for key in ("usd", "baseline_usd", "avoided_usd", "avoided_pct"):
            accounting["cost"][key] = None
    return Receipt(
        model=model.id,
        usd=usd,
        co2e_g=co2e_g,
        energy_wh=energy_wh,
        baseline_model=baseline_model,
        avoided_usd=avoided_usd,
        avoided_usd_pct=avoided_usd_pct,
        avoided_co2e_g=avoided_co2e_g,
        avoided_co2e_pct=avoided_co2e_pct,
        usage={
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_read_tokens": usage.cache_read_tokens,
            "cache_write_tokens": usage.cache_write_tokens,
            **(
                {
                    "reasoning_tokens": usage.reasoning_tokens,
                    "reasoning_accounting": usage.reasoning_accounting,
                }
                if usage.reasoning_tokens is not None
                or usage.reasoning_accounting is not None
                else {}
            ),
        },
        routing={
            "chosen_model": decision.chosen_model,
            "reasoning": decision.reasoning,
            "candidates": decision.candidates,
            "fallback_used": decision.fallback_used,
            "objective": decision.objective,
        },
        # Deep-copied: `decision.spend` is the RoutingDecision's own audit dict
        # and `accounting` is otherwise handed out by reference — either one
        # mutated through a supposedly-frozen Receipt would corrupt state the
        # caller does not own.
        overhead=copy.deepcopy(decision.spend),
        raw=copy.deepcopy(accounting),
        estimated=estimated,
        call_records=copy.deepcopy(call_records or []),
    )
