"""The LLM model router. Every decision — including user pins and overrides —
is persisted as a RoutingDecision on the run, so routing is always auditable.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from bench.config import get_settings
from bench.providers.base import ProviderError
from bench.providers.catalog import ModelCatalog, ModelInfo, ProviderRegistry
from bench.router_llm.fallback import fallback_model
from bench.router_llm.prompts import (
    ROUTER_SYSTEM,
    ROUTING_PROMPT_VERSION,
    choose_model_schema,
    render_router_prompt,
)

TIER_ORDER = {"economy": 0, "standard": 1, "premium": 2}


@dataclass
class RoutingDecision:
    router_model: str | None
    routing_prompt_version: str
    candidates: list[str]
    chosen_model: str
    reasoning: str
    confidence: str | None = None
    fallback_used: bool = False
    override: str | None = None  # "user_pin" | "run_override" | None
    latency_ms: int = 0
    decided_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_json(self) -> dict:
        return asdict(self)


class RoutingUnavailable(Exception):
    """No model can be selected at all (no provider keys)."""


class ModelRouter:
    def __init__(self, catalog: ModelCatalog, registry: ProviderRegistry):
        self._catalog = catalog
        self._registry = registry

    def _candidates(self, model_policy: dict) -> list[ModelInfo]:
        allowed = model_policy.get("allowed") or None
        max_tier = model_policy.get("max_cost_tier", "premium")
        out = []
        for m in self._catalog.all():
            if not m.supports_tools:
                continue
            if not self._registry.has_key(m.provider):
                continue
            if allowed and m.id not in allowed:
                continue
            if TIER_ORDER.get(m.cost_tier, 2) > TIER_ORDER.get(max_tier, 2):
                continue
            out.append(m)
        # Curated first, then cheap-to-expensive; cap the list the router sees.
        out.sort(key=lambda m: (not m.curated, m.output_price_per_mtok))
        return out[:20]

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
        # 1. Overrides short-circuit — but are still logged as decisions.
        if run_override:
            return self._validated_override(run_override, "run_override")
        if model_policy.get("mode") == "pinned":
            return self._validated_override(model_policy.get("model", ""), "user_pin")

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
                fallback_used=False,
            )

        settings = get_settings()
        router_model_id = settings.router_model
        router_info = self._catalog.get(router_model_id)
        candidate_ids = [m.id for m in candidates]

        if router_info is not None and self._registry.has_key(router_info.provider):
            prompt = render_router_prompt(
                task_type=task_type,
                task_shape=task_shape,
                task_description=task_description,
                output_contract=output_contract,
                n_documents=n_documents,
                est_input_tokens=est_input_tokens,
                max_cost_tier=model_policy.get("max_cost_tier", "premium"),
                candidates=candidates,
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
                            latency_ms=int((time.monotonic() - start) * 1000),
                        )
                except ProviderError:
                    continue

        # 2. Deterministic fallback.
        chosen = fallback_model(
            task_shape, self._catalog, self._registry, model_policy.get("allowed")
        )
        if chosen is None:
            raise RoutingUnavailable("Router failed and no fallback model is available.")
        return RoutingDecision(
            router_model=router_model_id,
            routing_prompt_version=ROUTING_PROMPT_VERSION,
            candidates=candidate_ids,
            chosen_model=chosen,
            reasoning=f"LLM router unavailable or invalid; deterministic fallback for shape '{task_shape}'.",
            fallback_used=True,
        )

    def _validated_override(self, model_id: str, kind: str) -> RoutingDecision:
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
            override=kind,
        )
