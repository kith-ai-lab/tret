"""HarnessEngine: the agent loop.

One entry point, `execute(run_id)`, designed to run as a background task. It
loads the run, assembles context, routes the model, executes the tool loop,
persists the transcript/cost after every iteration, and publishes RunEvents.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import select

if TYPE_CHECKING:
    from tret.services.emission_factors import EmissionsOverrides, FactorSet

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
    DELEGATION_DEPTH_KEY,
    WEB_TOOL_NAMES,
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
from tret.router_llm.priors import OutcomePriors, PriorsProvider
from tret.router_llm.router import ModelRouter, RoutingUnavailable
from tret.engine.supervisor import (
    Intervention,
    TurnState,
    assess,
    normalize_for_provider,
)
from tret.services.emission_settings import factor_set_for, workspace_emissions_layers
from tret.services.emissions import (
    combine_accountings,
    overhead_block,
    emission_event_fields,
    energy_wh_field,
)
from tret.services.outcomes import record_outcome
from tret.services.transcript import (
    ENGINE_NUDGE_KEY,
    NUDGE_EMPTY_REPLY,
    NUDGE_OUTPUT_BUDGET,
    NUDGE_TERMINAL,
    REPEATED_CALL_KEY,
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
    # True once any turn folded into this segment was an ESTIMATE rather than a
    # provider-reported figure — a turn whose stream died mid-way (see
    # `HarnessEngine._book_usage`). The segment's totals stay one running sum
    # either way (an estimate is still real tokens the provider was paid for),
    # but this says so, so analytics can tell a metered receipt from a guessed
    # one instead of reading both as equally certain.
    estimated_usage: bool = False
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

    def add(self, usage: Usage, iteration: int, *, estimated: bool = False) -> None:
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

    def accounting(self) -> dict:
        return energy_accounting(
            self.model,
            self.usage.input_tokens,
            self.usage.output_tokens,
            self.usage.cache_read_tokens,
            self.usage.cache_write_tokens,
            factors=self.factors,
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
            # See `estimated_usage`'s docstring: True if any turn folded into
            # this segment was priced from an estimate rather than a metered
            # figure.
            "estimated": self.estimated_usage,
        }


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
        # In-process delegation lineage: child run id -> parent run id.
        # `run_harness_task` (engine/tools.py) registers a child here before
        # awaiting its `execute()` and unregisters it after, so a run created by
        # delegation is never a mystery to the engine that has to cancel it —
        # even though a child has no `parent_run_id` column (see the module this
        # dict is read from: `_is_cancelled` and `cancel` below). Bounded by
        # construction: a run appears here for exactly the lifetime of the
        # `run_harness_task` call that created it.
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
                # Every path out of this method — the normal finish inside
                # `_execute_inner`, `_fail_before_start`'s early return from it
                # (still inside the `try` above, since it never raises), and
                # the crash handler just above — reaches this exactly once.
                # Before this `finally` existed, only the normal finish path
                # discarded `run.id` (see `_execute_inner`'s own comment further
                # down): a run cancelled while it was, say, failing a pre-flight
                # check would leave its id sitting in `_cancelled` forever, with
                # no later code path ever reaching back to clean it up.
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
        if set(enabled_names) & CONNECTOR_TOOL_NAMES:
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
                emissions_workspace_doc=emissions.workspace_doc,
                emissions_managed_doc=emissions.managed_doc,
                emissions_at=emissions.at,
            )
        except RoutingUnavailable as e:
            await self._fail_before_start(db, run, str(e), workspace_id=harness.workspace_id)
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
            workspace_id=workspace_id,
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
        segments: list[ModelSegment] = [
            ModelSegment(
                model_info,
                reason="initial",
                factors=self._factors_for(model_info.provider, model_info.id, emissions),
            )
        ]
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
            if self._is_cancelled(run.id):
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
                    emissions=emissions,
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
                        estimated=True,
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
                run.status = "failed"
                run.error = str(e)
                break

            usage = turn.usage if turn else Usage()
            self._book_usage(
                run=run,
                total_usage=total_usage,
                segment=segment,
                segments=segments,
                model_info=model_info,
                usage=usage,
                iteration=iteration,
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
                run.status = self._final_status(ctx, final_text)
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
                    emissions=emissions,
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
        estimated: bool = False,
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
        """
        total_usage.input_tokens += usage.input_tokens
        total_usage.output_tokens += usage.output_tokens
        total_usage.cache_read_tokens += usage.cache_read_tokens
        total_usage.cache_write_tokens += usage.cache_write_tokens
        # Booked against the model that actually ran the turn. A run may
        # change model part-way (see the supervisor below), and every figure
        # downstream — price, energy class, PUE, grid factor — is a property
        # of *which* model spent the tokens, not of the run as a whole.
        segment.add(usage, iteration, estimated=estimated)
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
        accounting = combine_accountings([seg.accounting() for seg in segments])
        run.energy_wh = Decimal(str(accounting["energy_wh"]))
        run.energy_accounting = accounting
        # Normally kept only once a run has used more than one model (see
        # ModelSegment's own docstring) — an estimated turn is the exception:
        # `estimated_usage` lives nowhere else on the run, so the timeline is
        # persisted even for an ordinary single-segment run rather than
        # silently dropping the one signal analytics needs to tell a metered
        # receipt from a guessed one.
        if len(segments) > 1 or estimated:
            run.model_timeline = [seg.to_json() for seg in segments]
        return turn_cost

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
        segments.append(
            ModelSegment(
                target,
                reason=intervention.reason,
                factors=self._factors_for(target.provider, target.id, emissions),
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
        emissions: "_EmissionsContext",
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
