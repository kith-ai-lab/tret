"""Versioned routing prompts. Bump ROUTING_PROMPT_VERSION on any wording change —
the version is persisted in every RoutingDecision so past routings stay interpretable.
"""
from __future__ import annotations

from bench.providers.catalog import ModelInfo

ROUTING_PROMPT_VERSION = "route-v2"

ROUTER_SYSTEM = """\
You are a model-selection router for an analyst workbench. Pick the single best \
model for the task from the candidate list. Optimize, in order: (1) reliability \
of tool-calling and strict JSON-schema adherence, (2) quality of reasoning and \
writing for this task shape, (3) recency — when candidates are otherwise \
comparable, prefer the more recently released model (newer generations are \
generally more capable per dollar), (4) cost — prefer the cheapest model that \
will not degrade the deliverable. You must choose a model_id from the \
candidates exactly as written. Respond only via the choose_model tool."""


def choose_model_schema(candidate_ids: list[str]) -> dict:
    return {
        "type": "object",
        "required": ["model_id", "reasoning", "confidence"],
        "properties": {
            "model_id": {"type": "string", "enum": candidate_ids},
            "reasoning": {"type": "string", "maxLength": 600},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        },
    }


def render_router_prompt(
    *,
    task_type: str,
    task_shape: str,
    task_description: str,
    output_contract: str,
    n_documents: int,
    est_input_tokens: int,
    max_cost_tier: str,
    candidates: list[ModelInfo],
) -> str:
    lines = [
        "TASK",
        f"  type: {task_type}",
        f"  shape: {task_shape}",
        f"  description: {task_description}",
        f"  output_contract: {output_contract}",
        f"  input_size: ~{n_documents} documents, est. {est_input_tokens} input tokens",
        "",
        "CANDIDATES",
    ]
    for m in candidates:
        strengths = ", ".join(m.strengths) if m.strengths else "(uncurated)"
        released = m.released or "unknown"
        lines.append(
            f"  - id: {m.id} | released: {released} | tier: {m.cost_tier} | "
            f"ctx: {m.context_window} | strengths: {strengths}"
        )
    lines += ["", "CONSTRAINTS", f"  max_cost_tier: {max_cost_tier}"]
    return "\n".join(lines)
