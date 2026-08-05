"""The LLM model router. Every decision — including user pins and overrides —
is persisted as a RoutingDecision on the run, so routing is always auditable.

Two invariants hold across every path through `route()`:

* The harness cost ceiling (`model_policy["max_cost_tier"]`) binds the automatic
  paths — the candidate list, the deterministic fallback, and the choice of
  router model itself. Only an explicit pin or per-run override may exceed it,
  and those are recorded as overrides. A capped harness with nothing to run
  raises `RoutingUnavailable` rather than escalating.
* The persisted decision describes what actually happened: `chosen_model` is
  always one of `candidates` on the automatic paths, and `router_model` is null
  whenever no router was consulted.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from bench.config import get_settings
from bench.providers.base import ProviderError
from bench.providers.catalog import ModelCatalog, ModelInfo, ProviderRegistry
from bench.router_llm.fallback import fallback_model
from bench.router_llm.objectives import (
    DEFAULT_MAX_COST_TIER,
    DEFAULT_OBJECTIVE,
    TIER_ORDER,
    candidate_sort_key,
    objective_of,
    within_cost_tier,
)
from bench.router_llm.prompts import (
    ROUTER_SYSTEM,
    ROUTING_PROMPT_VERSION,
    choose_model_schema,
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
    def __init__(self, catalog: ModelCatalog, registry: ProviderRegistry):
        self._catalog = catalog
        self._registry = registry

    def _candidates(self, model_policy: dict) -> list[ModelInfo]:
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
        # Ordered by the harness objective; cap the list the router sees.
        out.sort(key=candidate_sort_key(objective))
        return out[:CANDIDATE_LIMIT]

    def _resolve_router_model(self, max_tier: str) -> ModelInfo | None:
        """Which model performs the routing decision, or None to skip the LLM step.

        `BENCH_ROUTER_MODEL` names a specific model, and its provider may well not
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
        return min(options, key=lambda m: (m.output_price_per_mtok, m.id))

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
    ) -> RoutingDecision:
        objective = objective_of(model_policy)
        max_tier = _max_cost_tier(model_policy)
        # 1. Overrides short-circuit — but are still logged as decisions.
        if run_override:
            return self._validated_override(run_override, "run_override", objective)
        if model_policy.get("mode") == "pinned":
            return self._validated_override(model_policy.get("model", ""), "user_pin", objective)

        candidates = self._candidates(model_policy)
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
                fallback_used=False,
            )

        settings = get_settings()
        router_info = self._resolve_router_model(max_tier)
        router_model_id = router_info.id if router_info is not None else settings.router_model
        candidate_ids = [m.id for m in candidates]

        router_usable = router_info is not None
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
            )
            start = time.monotonic()
            for _attempt in range(2):  # one retry
                try:
                    provider = self._registry.get(router_info.provider)
                    result = await provider.complete_json(
                        model=router_info.wire_id,
                        system=ROUTER_SYSTEM,
                        prompt=prompt,
                        schema=choose_model_schema(candidate_ids),
                        tool_name="choose_model",
                        max_tokens=512,
                        timeout=settings.router_timeout_seconds,
                    )
                    chosen = result.get("model_id")
                    if chosen in candidate_ids:
                        return RoutingDecision(
                            router_model=router_model_id,
                            routing_prompt_version=ROUTING_PROMPT_VERSION,
                            candidates=candidate_ids,
                            chosen_model=chosen,
                            reasoning=str(result.get("reasoning", ""))[:600],
                            confidence=result.get("confidence"),
                            objective=objective,
                            latency_ms=int((time.monotonic() - start) * 1000),
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
            fallback_used=True,
        )

    def _assert_within_ceiling(self, model_id: str, max_tier: str) -> None:
        """Belt and braces: no decision leaves this router above the ceiling.

        Pins and per-run overrides are deliberately exempt — they are explicit
        operator choices, and they are recorded as overrides in the audit trail.
        This guards the *automatic* paths, where a future selection rule could
        otherwise reintroduce silent escalation.
        """
        info = self._catalog.get(model_id)
        if info is not None and not within_cost_tier(info, max_tier):
            raise RoutingUnavailable(
                f"Refusing to route to '{model_id}' (tier '{info.cost_tier}'): the harness "
                f"cost ceiling is '{max_tier}'."
            )

    def _validated_override(
        self, model_id: str, kind: str, objective: str = DEFAULT_OBJECTIVE
    ) -> RoutingDecision:
        info = self._catalog.get(model_id)
        if info is None:
            raise RoutingUnavailable(f"Model '{model_id}' is not in the catalog.")
        if not self._registry.has_key(info.provider):
            raise RoutingUnavailable(
                f"Model '{model_id}' requires provider '{info.provider}', which has no API key."
            )
        return RoutingDecision(
            router_model=None,
            routing_prompt_version=ROUTING_PROMPT_VERSION,
            candidates=[model_id],
            chosen_model=model_id,
            reasoning="user pin" if kind == "user_pin" else "per-run override",
            objective=objective,
            override=kind,
        )
