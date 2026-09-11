"""The LLM model router. Every decision — including user pins and overrides —
is persisted as a RoutingDecision on the run, so routing is always auditable.

Three invariants hold across every path through `route()`:

* The harness cost ceiling (`model_policy["max_cost_tier"]`) binds the automatic
  paths — the candidate list, the deterministic fallback, and the choice of
  router model itself. A capped harness with nothing to run raises
  `RoutingUnavailable` rather than escalating.
* The two kinds of override are not equally powerful, because they do not come
  from the same place. A harness pin (`model_policy["mode"] == "pinned"`) is
  written on the harness and may exceed that harness's own ceiling. A per-request
  override (`_model_override` on a run's task_input, settable by any signed-in
  caller of POST /api/runs or the chat composer) may choose *within* the harness
  policy — its `allowed` list and its `max_cost_tier` — and nothing more. See
  `_assert_override_within_policy`.
* The persisted decision describes what actually happened: `chosen_model` is
  always one of `candidates` on the automatic paths, and `router_model` is null
  whenever no router was consulted.
* Recorded evidence may reorder and demote, never widen. Priors from past runs
  (`router_llm/priors.py`) steer which candidates are seen first and are shown to
  the router model, but nothing derived from them can add a model to `allowed`,
  lift `max_cost_tier`, or reach a model whose provider has no key. Whatever was
  read is snapshotted onto the decision as `evidence`, because the priors move
  and a decision has to stay explicable after they have.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from tret.config import get_settings
from tret.providers.base import ProviderError
from tret.providers.catalog import ModelCatalog, ModelInfo, ProviderRegistry
from tret.router_llm.fallback import fallback_model
from tret.adaptive import adaptive_of
from tret.router_llm.objectives import (
    DEFAULT_MAX_COST_TIER,
    DEFAULT_OBJECTIVE,
    EFFORT_LEVELS,
    THRIFT_OBJECTIVES,
    TIER_ORDER,
    TIER_POOR,
    TIER_PROVEN,
    candidate_sort_key,
    default_effort,
    evidence_tier,
    objective_of,
    within_cost_tier,
)
from tret.router_llm.outcomes import size_band
from tret.router_llm.priors_base import (
    PRIORS_VERSION,
    ModelPrior,
    NoPriors,
    PriorsProvider,
    poor_endpoints,
)
from tret.services.emissions import overhead_call
from tret.router_llm.prompts import (
    ROUTER_SYSTEM,
    ROUTING_PROMPT_VERSION,
    choose_model_schema,
    prompt_sha256,
    render_router_prompt,
)

# TIER_ORDER is defined in router_llm.objectives (and re-exported here for
# callers that have always imported it from this module) because the
# deterministic fallback has to apply the identical ceiling.
__all__ = ["TIER_ORDER", "ModelRouter", "RoutingDecision", "RoutingUnavailable"]

# How many candidates the router model is shown. The list is a prompt cost too.
CANDIDATE_LIMIT = 20


def _max_cost_tier(model_policy: dict) -> str:
    """The ceiling a policy asks for, defaulting to no ceiling.

    An unrecognized value would silently become "premium" inside TIER_ORDER's
    `.get(..., 2)` default, so it is normalized here instead: the API validates
    tiers on write (api/harnesses.py), and a policy that somehow carries a bad
    one must not be read as *unrestricted*.
    """
    tier = (model_policy or {}).get("max_cost_tier") or DEFAULT_MAX_COST_TIER
    return tier if tier in TIER_ORDER else DEFAULT_MAX_COST_TIER


def _apply_context_fit(
    candidates: list[ModelInfo],
    min_context_window: int | None,
    priors: dict[str, ModelPrior] | None = None,
) -> tuple[list[ModelInfo], dict]:
    """Narrow an already-ordered candidate list to those whose window can hold
    the composed prompt, or fall back to best effort when none can.

    `candidates` is assumed already filtered by `allowed`/provider-key/cost-tier
    and sorted by the harness objective (or is a single override/pin model) —
    this only ever narrows or reorders what the policy already permitted, the
    same guarantee the evidence-based reordering above it keeps. A model with
    an unreported window (`context_window` falsy — a freshly discovered local
    model) is never excluded by this filter: there is nothing to compare
    against, and `engine/compaction.budget` already treats an unknown window as
    "no limit enforced" rather than guessing at one.

    A local model (`cost_tier == "local"`) is exempt from this filter outright,
    window known or not: it is very often chosen for confidentiality
    (docs/local-models.md), not for capability, and a harness that picked it on
    purpose should not have the router quietly hand the task to a cloud model
    because the local one's window looked small. It keeps its ordering position
    — the same treatment an unknown window already gets — and is reported under
    `context_fit["exempt"]` rather than `excluded`, since it was never at risk
    of exclusion. Compaction/trimming (`engine/compaction.py`) is what actually
    absorbs the overflow for it, same as for any model that fails to fit.

    `priors` steers the best-effort re-sort exactly the way it steers the
    primary ordering (`candidate_sort_key`): a model with a poor track record
    must not lead just because it advertises the biggest window.

    Returns the list the caller should actually use, and the `context_fit`
    record to persist on the `RoutingDecision` — see that dataclass's
    docstring for the three `mode` values.
    """
    if min_context_window is None:
        return candidates, {"required": 0, "mode": "unchecked", "excluded": [], "exempt": []}
    exempt = [m.id for m in candidates if m.cost_tier == "local"]
    fits = [
        m
        for m in candidates
        if m.cost_tier == "local"
        or not m.context_window
        or m.context_window >= min_context_window
    ]
    if fits:
        fit_ids = {m.id for m in fits}
        excluded = [m.id for m in candidates if m.id not in fit_ids]
        return fits, {
            "required": min_context_window,
            "mode": "fit",
            "excluded": excluded,
            "exempt": exempt,
        }
    # Nothing fits within what the policy already allowed (local models aside —
    # `fits` above would already be non-empty if any survived). The cost
    # ceiling still binds — this never reaches outside `candidates` for a
    # bigger model, it only reorders what survived the ceiling, evidence tier
    # first and window size second, and says on the decision that nothing
    # actually fit.
    best_effort = sorted(
        candidates,
        key=lambda m: (
            evidence_tier(priors.get(m.id) if priors else None),
            -m.context_window,
        ),
    )
    return best_effort, {
        "required": min_context_window,
        "mode": "best_effort",
        "excluded": [m.id for m in candidates if m.id not in exempt],
        "exempt": exempt,
    }


def _evidence_snapshot(
    priors: dict[str, ModelPrior], candidate_ids: list[str], est_input_tokens: int
) -> dict | None:
    """What this decision knew, frozen onto it. None when it knew nothing.

    Only the candidates actually considered are included: a decision's evidence
    should describe the choice that was made, and priors for models the policy
    excluded are not part of it. `demoted` and `unrecorded` are called out by
    name because they are the two things an operator asks about first — why a
    model was passed over, and which models had no say.
    """
    shown = {mid: p for mid, p in priors.items() if mid in candidate_ids}
    if not shown:
        return None
    return {
        "version": PRIORS_VERSION,
        "size_band": size_band(est_input_tokens),
        "priors": {mid: p.to_json() for mid, p in shown.items()},
        "demoted": sorted(
            mid for mid, p in shown.items() if evidence_tier(p) == TIER_POOR
        ),
        "proven": sorted(
            mid for mid, p in shown.items() if evidence_tier(p) == TIER_PROVEN
        ),
        "unrecorded": sorted(mid for mid in candidate_ids if mid not in shown),
    }


def _provider_ignore_for(priors: dict[str, ModelPrior], chosen_model: str) -> list[str]:
    """Endpoints this decision's own evidence says to steer `chosen_model`
    away from (`priors_base.poor_endpoints`). `[]` whenever there is nothing
    to act on: no prior for the chosen model (learning off, cold start, or an
    override/pin path that never reads `priors` at all), a prior with fewer
    than two endpoints on record, or one where nothing stands out as poor.
    Naturally empty for anything but an `openrouter/*` model too —
    `ModelPrior.endpoints` is only ever populated from `RunOutcome.served_by`,
    itself only set on OpenRouter runs — so no separate provider check is
    needed here.

    `ModelPrior.endpoints`' keys, and so the entries this returns, are
    provider *slugs* ("deepinfra") from here on — `OpenRouterProvider.
    _resolve_served_by` (providers/openai_compat.py) resolves the display
    name OpenRouter's metadata carries into a slug before `served_by` is ever
    recorded, which is also what this list is fed back to OpenRouter as
    (`provider.ignore` matches on slugs, not display names; see `Provider.
    stream`'s `provider_ignore` docstring in providers/base.py). `served_by`
    was not read anywhere before this, so there is no prior display-name
    data on record to migrate.
    """
    prior = priors.get(chosen_model)
    if prior is None:
        return []
    return poor_endpoints(prior)


@dataclass
class RoutingDecision:
    router_model: str | None
    routing_prompt_version: str
    candidates: list[str]
    chosen_model: str
    reasoning: str
    confidence: str | None = None
    # What the harness asked the router to optimize for. Persisted because the
    # same candidates and the same prompt version can yield different picks
    # under different objectives — the audit trail has to say which was in force.
    objective: str = DEFAULT_OBJECTIVE
    # The other two inputs that change the answer without appearing anywhere in
    # it. `task_shape` selects the deterministic fallback's preference list and
    # is the key outcome evidence is grouped by; `max_cost_tier` is the ceiling
    # that shaped the candidate list. Both were previously reconstructible only
    # by re-reading the run's pack — which stops working the moment the pack is
    # upgraded, so a past decision became uninterpretable exactly when it
    # mattered. Defaulted so a decision built without them still validates.
    task_shape: str = "freeform"
    max_cost_tier: str = DEFAULT_MAX_COST_TIER
    # Reasoning-effort level for the chosen model, one of EFFORT_LEVELS.
    # Recorded on every path, including the override/pin/fallback paths that
    # never ask a router anything (see `default_effort`) — it documents
    # *intent* even for a chosen model whose `ModelInfo.supports_effort` is
    # False; the harness/provider layer is what actually decides whether to
    # send it (`engine/harness.py` gates on `supports_effort` before passing
    # it to `provider.stream()`). `None` only for a decision built before this
    # field existed.
    effort: str | None = None
    # The upstream provider that actually served the router's own LLM call
    # (`JsonCompletion.served_by`; see providers/base.py) — set only on the LLM
    # path below, where a router model was actually contacted. Null on every
    # other path (single candidate, deterministic fallback, override/pin) for
    # the same reason `router_prompt` is null there: no router call happened to
    # have a serving provider to report.
    router_served_by: str | None = None
    # The track record this decision was made against, snapshotted. Null when no
    # evidence was read — a harness with learning off, an install with no
    # history, or an override, which skips the automatic paths entirely. Stored
    # rather than referenced because priors are a moving aggregate: re-deriving
    # them next month answers a different question than the one this decision
    # was answering.
    evidence: dict | None = None
    # Endpoints to exclude from OpenRouter's provider selection for this
    # decision's chosen model, from its own poor-endpoint record (see
    # `_provider_ignore_for` above and `priors_base.poor_endpoints`). Computed
    # from the same `priors` snapshot as `evidence`, so it is `[]` under
    # everything that leaves `evidence` null too — learning off, a cold
    # start, or the override/pin paths, which never read `priors` at all —
    # plus any chosen model with fewer than two endpoints on record or none
    # that stands out as poor. `engine/harness.py` reads this off
    # `run.routing["provider_ignore"]` and forwards it to `Provider.stream()`
    # as `provider_ignore`, but only while the run is still on this
    # decision's own chosen model: a supervisor switch makes the list stale
    # for whatever model the run moves onto.
    provider_ignore: list[str] = field(default_factory=list)
    # Whether the chosen model can actually hold this call, set on every path —
    # including the override/pin paths, which are never blocked by it (see
    # `_validated_override`). Shape: `{"required": int, "mode": "fit" |
    # "best_effort" | "unchecked", "excluded": [model_id, ...], "exempt":
    # [model_id, ...], "basis": "prompt_without_history" | "full_prompt"}`.
    #   "fit"         the context-window floor was applied and at least one
    #                 candidate (or the override/pin) met it.
    #   "best_effort" nothing within the harness policy met the floor, so the
    #                 candidate with the largest window was preferred instead —
    #                 the cost ceiling still bound the search; `excluded` names
    #                 everything that was dropped before that fallback (on the
    #                 automatic paths, that is every candidate the policy
    #                 allowed, minus `exempt`; on the override/pin path, just
    #                 that one model).
    #   "unchecked"   the caller passed no `min_context_window` (it does not yet
    #                 know the prompt size, or is resolving a router model
    #                 rather than sizing a run) — no filtering happened at all.
    # `exempt` names local models (`cost_tier == "local"`): the filter never
    # excludes one, window known or not (see `_apply_context_fit`), so they
    # appear here rather than in `excluded` even when their window is too
    # small — compaction/trimming is what absorbs that overflow instead.
    # `basis` says which prompt size the floor itself (`required`) was computed
    # against — set by `engine/harness.py`, which knows about compaction; every
    # other caller (sdk.py, local_run.py) leaves it unset, since neither trims
    # history before sending it. `"prompt_without_history"` means adaptive
    # compaction is on and the floor already excludes the conversation-history
    # block that `trim_history` will shrink after routing; `"full_prompt"`
    # means compaction is off (or the caller has no history to trim), so the
    # floor is the whole composed prompt.
    context_fit: dict | None = None
    # What the router was actually asked, and its fingerprint. Null on every path
    # where no router was consulted — an override, a single candidate, or no
    # usable router model — which is the same thing `router_model` being null
    # means, said about the prompt.
    #
    # NOTE the deliberate deviation from `engine/context.ContextBlock`, which
    # hashes doctrine and pointedly does *not* persist its text. That works there
    # because the text is recoverable: doctrine lives in the pack, pinned by
    # `content_hash`. A rendered routing prompt is recoverable from nowhere — its
    # candidate list, its track record and its objective rules existed only at
    # that instant — so a hash with nothing to compare against would be a
    # fingerprint of a document nobody kept. Bounded by CANDIDATE_LIMIT to a few
    # KB, against run rows that already carry whole transcripts.
    router_prompt: str | None = None
    router_prompt_sha256: str | None = None
    # What choosing cost. A routing call runs on `TRET_ROUTER_MODEL`, not on the
    # model it selects, so this is accounted against that model with its own
    # energy class and its provider's own grid factor — see
    # services/emissions.overhead_call. Null when no router was contacted.
    spend: dict | None = None
    fallback_used: bool = False
    override: str | None = None  # "user_pin" | "run_override" | None
    latency_ms: int = 0
    decided_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_json(self) -> dict:
        return asdict(self)


class RoutingUnavailable(Exception):
    """No model can be selected within the harness policy.

    Raised for a deployment with no provider keys, and also — deliberately —
    when a harness cost ceiling excludes everything that is available. A capped
    harness that cannot run fails loudly rather than escalating past its cap.
    """


class ModelRouter:
    def __init__(
        self,
        catalog: ModelCatalog,
        registry: ProviderRegistry,
        priors: PriorsProvider | None = None,
    ):
        self._catalog = catalog
        self._registry = registry
        # Defaults to no evidence, so a caller that never wires up priors — and
        # every existing caller — routes exactly as it did before.
        self._priors = priors or NoPriors()

    def _candidates(
        self,
        model_policy: dict,
        priors: dict[str, ModelPrior] | None = None,
        min_context_window: int | None = None,
    ) -> list[ModelInfo]:
        return self._candidates_with_fit(model_policy, priors, min_context_window)[0]

    def _candidates_with_fit(
        self,
        model_policy: dict,
        priors: dict[str, ModelPrior] | None = None,
        min_context_window: int | None = None,
    ) -> tuple[list[ModelInfo], dict]:
        """`_candidates()` plus the `context_fit` record the caller needs to
        persist on the `RoutingDecision`. Split out because `_candidates()` is
        also called from `candidates_for()` (the supervisor's mid-run candidate
        list), which has no `min_context_window` of its own and no decision to
        record it on.
        """
        allowed = model_policy.get("allowed") or None
        max_tier = _max_cost_tier(model_policy)
        objective = objective_of(model_policy)
        out = []
        for m in self._catalog.all():
            if not m.supports_tools:
                continue
            if not self._registry.has_key(m.provider):
                continue
            if allowed and m.id not in allowed:
                continue
            if not within_cost_tier(m, max_tier):
                continue
            out.append(m)
        # Ordered by the harness objective, with any recorded evidence leading;
        # cap the list the router sees. Note the order of operations: the
        # `allowed` list, the provider-key check and the cost ceiling have all
        # already been applied above, so evidence only ever reorders models the
        # policy had already permitted.
        out.sort(key=candidate_sort_key(objective, priors))
        # The context-window floor is applied last, and — like the cost ceiling
        # before it — only ever narrows or reorders what survived the earlier
        # filters. See `_apply_context_fit`.
        out, context_fit = _apply_context_fit(out, min_context_window, priors)
        return out[:CANDIDATE_LIMIT], context_fit

    async def candidates_for(
        self,
        *,
        model_policy: dict,
        task_shape: str,
        est_input_tokens: int,
    ) -> tuple[list[ModelInfo], dict[str, ModelPrior]]:
        """The models this policy permits, in order, plus their track records.

        Public because the mid-run supervisor (`engine/supervisor.py`) needs
        exactly the list `route()` chose from — the same `allowed` filter, the
        same provider-key check, the same cost ceiling. Re-deriving that list in
        the engine is how the two would drift, and a drift here means a switch
        landing outside the harness policy.
        """
        await self._catalog.warm_once()
        objective = objective_of(model_policy)
        priors: dict[str, ModelPrior] = {}
        if adaptive_of(model_policy).learn_from_outcomes:
            priors = await self._priors.for_key(
                task_shape=task_shape,
                objective=objective,
                size_band=size_band(est_input_tokens),
            )
        return self._candidates(model_policy, priors), priors

    def _resolve_router_model(self, max_tier: str) -> ModelInfo | None:
        """Which model performs the routing decision, or None to skip the LLM step.

        `TRET_ROUTER_MODEL` names a specific model, and its provider may well not
        be the provider the operator configured — the shipped default is an
        Anthropic model, while the documented quickstart is "OpenRouter alone
        works". Requiring an exact match meant the LLM router silently never ran
        on the recommended configuration, and every decision came from the
        deterministic fallback while the audit trail still named a router model
        that was never called.

        So: honour the configured model when it is usable, and otherwise pick the
        cheapest small model that *is* usable. Two constraints on the substitute:

        * It must be within the harness cost ceiling. Choosing a model is itself a
          model call, so a harness capped at `local` must not hand its task
          description to a cloud router (see docs/local-models.md). At that cap
          the substitute is a discovered local model or nothing.
        * It stays in the `economy` tier or below. The router reads a short prompt
          and answers with one identifier; spending premium tokens to save
          premium tokens is not a trade worth making silently.

        Whatever is chosen is recorded as `router_model` on the persisted
        RoutingDecision, so the audit trail always names the model that actually
        decided — never one that was merely configured.
        """
        configured = self._catalog.get(get_settings().router_model)
        if (
            configured is not None
            and self._registry.has_key(configured.provider)
            and within_cost_tier(configured, max_tier)
        ):
            return configured

        if max_tier == "local":
            # Local-only harness: a probe-verified local model or no LLM router.
            options = [
                m
                for m in self._catalog.all()
                if m.provider == "local" and m.supports_tools and self._registry.has_key("local")
            ]
        else:
            options = [
                m
                for m in self._catalog.all(curated_only=True)
                if m.provider != "local"
                and m.supports_tools
                and self._registry.has_key(m.provider)
                and TIER_ORDER.get(m.cost_tier, 2) <= TIER_ORDER["economy"]
                and within_cost_tier(m, max_tier)
            ]
        if not options:
            return None
        return min(options, key=lambda m: (m.prices_at()[1], m.id))

    async def route(
        self,
        *,
        model_policy: dict,
        task_type: str,
        task_shape: str,
        task_description: str,
        output_contract: str,
        n_documents: int,
        est_input_tokens: int,
        run_override: str | None = None,
        # The run's workspace/managed emissions-override documents
        # (tret/services/emission_settings.py), passed through so the
        # routing call's own `overhead_call` (below) records its energy under
        # the same configured layers as the run it serves, rather than
        # resolving its own from `settings` alone. Both `None` (every caller
        # before this parameter existed) is exactly that fallback.
        emissions_workspace_doc: dict | None = None,
        emissions_managed_doc: dict | None = None,
        # The run's start time, timezone-aware — threaded through to
        # `factor_set_for` below exactly like the two documents above, so a
        # routing call's own accounting sees the same hourly grid-table
        # value (if any) the run it serves does, rather than resolving its
        # own from "now". `None` (every caller before this parameter
        # existed) means "annual" — same as `factor_set_for`/
        # `build_factor_set` themselves.
        emissions_at: datetime | None = None,
        # The smallest window that can hold this call's composed prompt plus
        # its output reservation — `engine.compaction.required_context_window`,
        # computed by the caller because only it knows the prompt size and the
        # harness's output/headroom settings. `None` (every caller before this
        # parameter existed, and the router-model resolution path, which has no
        # run to size) means the context filter is skipped entirely and the
        # persisted decision says so (`context_fit.mode == "unchecked"`) rather
        # than silently claiming a check that never happened.
        min_context_window: int | None = None,
    ) -> RoutingDecision:
        objective = objective_of(model_policy)
        max_tier = _max_cost_tier(model_policy)
        learning = adaptive_of(model_policy).learn_from_outcomes
        # Local and dynamic models reach the catalog only through a discovery
        # pass. On a fresh process this is the first thing that needs them, so
        # make sure one has been attempted before deciding there are no
        # candidates. Idempotent, and a no-op once main.py's startup warm-up has
        # run or when neither source is configured.
        await self._catalog.warm_once()
        # 1. Overrides short-circuit — but are still logged as decisions, and a
        # per-request override is confined to the harness policy.
        if run_override:
            return self._validated_override(
                run_override,
                "run_override",
                objective,
                model_policy=model_policy,
                task_shape=task_shape,
                max_cost_tier=max_tier,
                min_context_window=min_context_window,
            )
        if model_policy.get("mode") == "pinned":
            return self._validated_override(
                model_policy.get("model", ""),
                "user_pin",
                objective,
                task_shape=task_shape,
                # A harness pin may exceed the harness's own ceiling (see the
                # module docstring), so the tier recorded here is what the policy
                # asked for, not a claim that the pinned model sits inside it.
                max_cost_tier=max_tier,
                min_context_window=min_context_window,
            )

        # Read once and reused for ordering, for the prompt, and for the
        # fallback, so all three are reasoning about the same snapshot even if
        # the aggregate moves mid-decision.
        priors: dict[str, ModelPrior] = {}
        if learning:
            priors = await self._priors.for_key(
                task_shape=task_shape,
                objective=objective,
                size_band=size_band(est_input_tokens),
            )

        candidates, context_fit = self._candidates_with_fit(
            model_policy, priors, min_context_window
        )
        if not candidates:
            raise RoutingUnavailable(
                "No candidate models: check provider API keys and the harness model policy."
            )
        if len(candidates) == 1:
            return RoutingDecision(
                router_model=None,
                routing_prompt_version=ROUTING_PROMPT_VERSION,
                candidates=[candidates[0].id],
                chosen_model=candidates[0].id,
                reasoning="Only one candidate model available.",
                objective=objective,
                task_shape=task_shape,
                max_cost_tier=max_tier,
                evidence=_evidence_snapshot(priors, [candidates[0].id], est_input_tokens),
                provider_ignore=_provider_ignore_for(priors, candidates[0].id),
                context_fit=context_fit,
                fallback_used=False,
                effort=default_effort(objective, task_shape),
            )

        settings = get_settings()
        router_info = self._resolve_router_model(max_tier)
        router_model_id = router_info.id if router_info is not None else settings.router_model
        candidate_ids = [m.id for m in candidates]

        router_usable = router_info is not None
        prompt = None
        prompt_fingerprint = None
        if router_usable:
            prompt = render_router_prompt(
                task_type=task_type,
                task_shape=task_shape,
                task_description=task_description,
                output_contract=output_contract,
                n_documents=n_documents,
                est_input_tokens=est_input_tokens,
                max_cost_tier=max_tier,
                candidates=candidates,
                objective=objective,
                priors=priors,
            )
            prompt_fingerprint = prompt_sha256(prompt)
            start = time.monotonic()
            for _attempt in range(2):  # one retry
                try:
                    provider = self._registry.get(router_info.provider)
                    completion = await provider.complete_json(
                        model=router_info.wire_id,
                        system=ROUTER_SYSTEM,
                        prompt=prompt,
                        schema=choose_model_schema(candidate_ids),
                        tool_name="choose_model",
                        max_tokens=512,
                        timeout=settings.router_timeout_seconds,
                    )
                    result = completion.payload
                    # Recorded even when the answer is rejected below: the tokens
                    # were spent either way, and a router that keeps returning
                    # invalid choices is exactly the case where the unbilled cost
                    # would otherwise be highest.
                    from tret.services.emission_settings import factor_set_for

                    try:
                        routing_factors = factor_set_for(
                            router_info.provider,
                            workspace_doc=emissions_workspace_doc,
                            managed_doc=emissions_managed_doc,
                            model_id=router_info.id,
                            at=emissions_at,
                        )
                    except Exception:
                        # Same fallback the engine gives a broken workspace
                        # document for the run's own model (harness.py's
                        # `_factors_for`): a routing call must still be
                        # accounted, just without the configured layers.
                        routing_factors = None
                    spend = overhead_call(
                        "routing", router_info, completion.usage, factors=routing_factors
                    )
                    chosen = result.get("model_id")
                    if chosen in candidate_ids:
                        # The router's own answer, honored unless it is
                        # unusable or it oversteps the objective's ceiling.
                        # Missing/invalid falls back to the same default the
                        # prompt itself showed the router (`effort_block`) —
                        # a router that skipped the field gets what it was
                        # told to assume by default, not an unrelated guess.
                        # Under the thrift objectives the default is also a
                        # hard ceiling: `default_effort` already returns
                        # "low" for both, so a router that named anything
                        # else there ignored the EFFORT section's explicit
                        # "never exceed the default" rule, and this is the
                        # enforcement of that rule rather than a suggestion.
                        default = default_effort(objective, task_shape)
                        chosen_effort = result.get("effort")
                        if chosen_effort not in EFFORT_LEVELS:
                            chosen_effort = default
                        elif (
                            objective in THRIFT_OBJECTIVES
                            and EFFORT_LEVELS.index(chosen_effort)
                            > EFFORT_LEVELS.index(default)
                        ):
                            chosen_effort = default
                        return RoutingDecision(
                            router_model=router_model_id,
                            routing_prompt_version=ROUTING_PROMPT_VERSION,
                            candidates=candidate_ids,
                            chosen_model=chosen,
                            reasoning=str(result.get("reasoning", ""))[:600],
                            confidence=result.get("confidence"),
                            objective=objective,
                            task_shape=task_shape,
                            max_cost_tier=max_tier,
                            evidence=_evidence_snapshot(
                                priors, candidate_ids, est_input_tokens
                            ),
                            provider_ignore=_provider_ignore_for(priors, chosen),
                            context_fit=context_fit,
                            router_prompt=prompt,
                            router_prompt_sha256=prompt_fingerprint,
                            spend=spend,
                            latency_ms=int((time.monotonic() - start) * 1000),
                            effort=chosen_effort,
                            router_served_by=completion.served_by,
                        )
                except ProviderError:
                    continue

        # 2. Deterministic fallback. The cost ceiling travels with it: this path
        # runs on every run of a deployment that has no key for the configured
        # router model, so a ceiling applied only to `_candidates` above would be
        # no ceiling at all.
        chosen = fallback_model(
            task_shape,
            self._catalog,
            self._registry,
            model_policy.get("allowed"),
            objective=objective,
            max_cost_tier=max_tier,
            priors=priors,
            min_context_window=min_context_window,
        )
        if chosen is None:
            raise RoutingUnavailable(
                "Router failed and no fallback model is available within the harness "
                f"cost ceiling '{max_tier}'."
            )
        self._assert_within_ceiling(chosen, max_tier)
        if not router_usable:
            why = (
                f"no router model available within the cost ceiling '{max_tier}' "
                f"(configured: '{settings.router_model}')"
            )
        else:
            why = "LLM router failed or returned an invalid choice"
        return RoutingDecision(
            # None when the router model was never contacted, so the audit record
            # cannot suggest a routing conversation that did not happen.
            router_model=router_model_id if router_usable else None,
            routing_prompt_version=ROUTING_PROMPT_VERSION,
            candidates=candidate_ids,
            chosen_model=chosen,
            reasoning=(
                f"{why}; deterministic fallback for shape '{task_shape}' under "
                f"objective '{objective}' with cost ceiling '{max_tier}'."
            ),
            objective=objective,
            task_shape=task_shape,
            max_cost_tier=max_tier,
            evidence=_evidence_snapshot(priors, candidate_ids, est_input_tokens),
            provider_ignore=_provider_ignore_for(priors, chosen),
            context_fit=context_fit,
            # Kept on the fallback path too, and this is where it earns its
            # place: the router was asked something and either failed or
            # answered with a model that was not on its own list. "What did we
            # ask it?" is the first question, and without this the answer was
            # unavailable exactly when it mattered.
            router_prompt=prompt,
            router_prompt_sha256=prompt_fingerprint,
            fallback_used=True,
            effort=default_effort(objective, task_shape),
        )

    def _assert_within_ceiling(self, model_id: str, max_tier: str) -> None:
        """Belt and braces: no decision leaves this router above the ceiling.

        A harness pin is deliberately exempt — it is the operator's own statement,
        made on the harness itself, and it is recorded as an override in the audit
        trail. Per-request overrides are *not* exempt (see
        `_assert_override_within_policy`). This guards the *automatic* paths, where
        a future selection rule could otherwise reintroduce silent escalation.
        """
        info = self._catalog.get(model_id)
        if info is not None and not within_cost_tier(info, max_tier):
            raise RoutingUnavailable(
                f"Refusing to route to '{model_id}' (tier '{info.cost_tier}'): the harness "
                f"cost ceiling is '{max_tier}'."
            )

    def _assert_override_within_policy(self, info: ModelInfo, model_policy: dict) -> None:
        """A per-request override picks a model; it may not widen the policy.

        Who can set one is the whole argument. `_model_override` arrives on a
        run's `task_input` from POST /api/runs and from the chat composer
        (tret/api/runs.py, tret/api/chat.py): any signed-in caller, no admin
        check, no harness edit, no persistence beyond that run. A harness's
        `mode: pinned` model and its `max_cost_tier` are by contrast written on
        the harness row by whoever may edit harnesses. Treating both as "an
        explicit operator choice" gave a request body the authority of a harness
        setting, which made the documented policy guarantees — only models on the
        `allowed` list, never above `max_cost_tier` — conditional on nobody
        passing one extra JSON field. In particular `max_cost_tier: local` is a
        confidentiality control (docs/local-models.md: no cloud provider may see
        the task), and a request must not be able to lift it.

        So a per-request override is confined to what the harness already allows,
        and refusal is loud (`RoutingUnavailable`, surfacing as a failed run with
        this message) rather than a silent downgrade to automatic routing: the
        caller named a model, and quietly running a different one would be the
        worse answer. Permitted overrides are unchanged, and still recorded in the
        audit trail as `override: "run_override"`.
        """
        allowed = model_policy.get("allowed") or None
        if allowed and info.id not in allowed:
            raise RoutingUnavailable(
                f"Model '{info.id}' is not on this harness's allowed model list. A per-run "
                "override may choose among the models the harness policy permits; widening "
                "that list is a harness setting."
            )
        max_tier = _max_cost_tier(model_policy)
        if not within_cost_tier(info, max_tier):
            raise RoutingUnavailable(
                f"Refusing the per-run override to '{info.id}' (tier '{info.cost_tier}'): the "
                f"harness cost ceiling is '{max_tier}'. Raise the ceiling on the harness, or "
                "pin the model there, if that is the intent."
            )

    def _validated_override(
        self,
        model_id: str,
        kind: str,
        objective: str = DEFAULT_OBJECTIVE,
        model_policy: dict | None = None,
        task_shape: str = "freeform",
        max_cost_tier: str = DEFAULT_MAX_COST_TIER,
        min_context_window: int | None = None,
    ) -> RoutingDecision:
        info = self._catalog.get(model_id)
        if info is None:
            raise RoutingUnavailable(f"Model '{model_id}' is not in the catalog.")
        if not self._registry.has_key(info.provider):
            raise RoutingUnavailable(
                f"Model '{model_id}' requires provider '{info.provider}', which has no API key."
            )
        if kind == "run_override":
            self._assert_override_within_policy(info, model_policy or {})
        # An override or a pin is never blocked on context fit — it is the
        # operator's or the caller's own explicit choice — but a pin that
        # cannot hold the prompt is still worth recording as such: `mode`
        # reads `best_effort` with the pinned model as its own `excluded`
        # entry, exactly like `_apply_context_fit` reports a policy where
        # nothing fit.
        _, context_fit = _apply_context_fit([info], min_context_window)
        return RoutingDecision(
            router_model=None,
            routing_prompt_version=ROUTING_PROMPT_VERSION,
            candidates=[model_id],
            chosen_model=model_id,
            reasoning="user pin" if kind == "user_pin" else "per-run override",
            objective=objective,
            task_shape=task_shape,
            max_cost_tier=max_cost_tier,
            context_fit=context_fit,
            override=kind,
            effort=default_effort(objective, task_shape),
        )
