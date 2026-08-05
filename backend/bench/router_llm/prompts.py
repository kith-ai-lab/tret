"""Versioned routing prompts. Bump ROUTING_PROMPT_VERSION on any wording change —
the version is persisted in every RoutingDecision so past routings stay interpretable.
"""
from __future__ import annotations

from bench.providers.catalog import ModelInfo
from bench.router_llm.objectives import DEFAULT_OBJECTIVE

ROUTING_PROMPT_VERSION = "route-v3"

ROUTER_SYSTEM = """\
You are a model-selection router for an analyst workbench. Pick the single best \
model for the task from the candidate list. Optimize, in order: (1) reliability \
of tool-calling and strict JSON-schema adherence, (2) quality of reasoning and \
writing for this task shape, (3) recency — when candidates are otherwise \
comparable, prefer the more recently released model (newer generations are \
generally more capable per dollar), (4) cost — prefer the cheapest model that \
will not degrade the deliverable. You must choose a model_id from the \
candidates exactly as written. Respond only via the choose_model tool."""

# Per-objective preference rules, appended as an OBJECTIVE section. `balanced` is
# deliberately absent: its rules *are* the ordering in ROUTER_SYSTEM above, so a
# balanced harness renders the same prompt bytes it always has.
OBJECTIVE_RULES: dict[str, tuple[str, ...]] = {
    "quality": (
        "Optimize for the best deliverable, not for thrift. Within the allowed "
        "cost tier, prefer the most capable candidate for this task shape.",
        "Token economy is a secondary consideration: break ties toward the "
        "cheaper model only when two candidates are genuinely comparable in "
        "capability for this shape.",
        "Do not pick a premium model when its strengths are irrelevant to the "
        "task — capability means capability *for this task*, not price.",
    ),
    "token_conservation": (
        "Optimize for token thrift. Prefer the smallest model that will still "
        "produce a correct, schema-valid result for this task shape.",
        "Weight output discipline heavily: prefer candidates known for terse, "
        "on-contract responses over candidates that pad, restate the prompt, or "
        "narrate their reasoning at length.",
        "Penalize premium models. Choose one only when the task shape clearly "
        "demands it (hard multi-step judgment, long-context synthesis, or a "
        "contract that smaller candidates reliably fail).",
    ),
    "eco": (
        "Optimize for the least estimated energy per unit of work. Each "
        "candidate carries an estimated energy class (S < M < L < XL) and an "
        "estimated Wh per million tokens.",
        "Prefer the local tier and the lowest energy class that can do the job: "
        "a local or S-class model that produces a correct result is the right "
        "answer even when a larger model would be marginally better.",
        "Treat a jump to a higher energy class as a cost that must be justified "
        "by the task shape — pick premium/XL only when the task clearly cannot "
        "be done at a lower class.",
        "Verbosity is energy: prefer candidates with disciplined output.",
    ),
}

# Objectives whose rules reason about energy, so the candidate list carries it.
_ENERGY_IN_CANDIDATES = ("eco", "token_conservation")


def objective_block(objective: str) -> list[str]:
    """The OBJECTIVE section lines, or [] for the default objective."""
    rules = OBJECTIVE_RULES.get(objective)
    if not rules:
        return []
    return ["", "OBJECTIVE", f"  objective: {objective}", *(f"  - {rule}" for rule in rules)]


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
    objective: str = DEFAULT_OBJECTIVE,
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
    show_energy = objective in _ENERGY_IN_CANDIDATES
    for m in candidates:
        strengths = ", ".join(m.strengths) if m.strengths else "(uncurated)"
        released = m.released or "unknown"
        # Energy is shown only where the objective reasons about it, so the
        # default objective's prompt is unchanged from earlier versions.
        energy = (
            f" | energy: {m.energy_class} (~{m.energy_wh_per_mtok} Wh/Mtok, est.)"
            if show_energy
            else ""
        )
        lines.append(
            f"  - id: {m.id} | released: {released} | tier: {m.cost_tier} | "
            f"ctx: {m.context_window} | strengths: {strengths}{energy}"
        )
    lines += ["", "CONSTRAINTS", f"  max_cost_tier: {max_cost_tier}"]
    lines += objective_block(objective)
    return "\n".join(lines)
