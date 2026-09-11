"""Versioned routing prompts. Bump ROUTING_PROMPT_VERSION on any wording change —
the version is persisted in every RoutingDecision so past routings stay interpretable.
"""
from __future__ import annotations

import hashlib

from tret.providers.catalog import ModelInfo
from tret.router_llm.objectives import DEFAULT_OBJECTIVE, default_effort

# route-v4 added the TRACK RECORD section. With no recorded outcomes the
# section was omitted entirely and the rendered prompt was byte-identical to
# v3's — the version recorded that an evidence-capable renderer produced the
# prompt, which is the fact an auditor reading an old decision needs.
#
# route-v5 adds the EFFORT section below and a required `effort` property on
# `choose_model_schema()`. Unlike the v4 bump, this one is NOT byte-identical
# on a cold start: the EFFORT section is unconditional (every task has an
# objective and a shape, so there is always a default to state), so every
# route-v5 prompt differs from what route-v4 would have rendered for the same
# call, evidence or no evidence.
ROUTING_PROMPT_VERSION = "route-v5"

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


# ── track record ─────────────────────────────────────────────────────────────
# How candidates' recorded outcomes are shown to the router. Three rules travel
# with the numbers, because each corrects a way this evidence is easy to misread,
# and the router is the reader most likely to misread it.
TRACK_RECORD_RULES: tuple[str, ...] = (
    "These are observed outcomes on this task shape, not a controlled "
    "comparison. Harder work is already routed to stronger models, so a strong "
    "model's rate partly reflects the tasks it was given.",
    "A candidate with no record is untried here, not bad. Where recorded "
    "candidates are close, prefer trying an unrecorded one — a record nobody "
    "ever adds to is a record that never improves.",
    "quality measures whether the work was right, never what it cost. The cost "
    "figures are shown separately; weigh them according to the objective, not by "
    "reading them into the quality number.",
)


def track_record_block(priors: dict) -> list[str]:
    """The TRACK RECORD section, or [] when there is nothing to report.

    Omitted wholesale rather than rendered empty, so an install with no history
    produces exactly the prompt tret produced before any of this existed. The
    unrecorded candidates are named only when at least one candidate *is*
    recorded — the contrast is the whole content of that line, and on a cold
    start it would just be a list of every candidate.
    """
    if not priors:
        return []
    lines = ["", "TRACK RECORD (observed outcomes on this task shape)"]
    for prior in sorted(priors.values(), key=lambda p: -p.quality_mean):
        human = (
            f"{prior.approvals} approved / {prior.rejections} rejected by reviewers"
            if (prior.approvals + prior.rejections)
            else "no human review yet"
        )
        failures = (
            " | failures: "
            + ", ".join(f"{kind} x{count}" for kind, count in prior.error_kinds.items())
            if prior.error_kinds
            else ""
        )
        lines.append(
            f"  - {prior.model_id}: {prior.runs} runs (effective {prior.effective_n:.1f}) | "
            f"quality {prior.quality_mean:.2f} (lower bound {prior.quality_ci_low:.2f}) | "
            f"delivered {prior.delivered_rate * 100:.0f}% | {human} | "
            f"avg ${prior.mean_cost_usd:.4f}, {prior.mean_iterations:.1f} iterations{failures}"
        )
    return lines


def unrecorded_block(unrecorded: list[str]) -> list[str]:
    if not unrecorded:
        return []
    return ["  no record yet (untried here, not judged): " + ", ".join(sorted(unrecorded))]


def prompt_sha256(prompt: str) -> str:
    """Fingerprint of a rendered router prompt.

    Covers the *rendered* half only — the part that differs per decision, and the
    part stored beside it, so the hash is verifiable from what is persisted
    rather than from what the reader hopes was in the source at the time. The
    constant half (`ROUTER_SYSTEM`) is pinned by `ROUTING_PROMPT_VERSION`, which
    is what that version string is for.
    """
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def choose_model_schema(candidate_ids: list[str]) -> dict:
    return {
        "type": "object",
        "required": ["model_id", "reasoning", "confidence", "effort"],
        "properties": {
            "model_id": {"type": "string", "enum": candidate_ids},
            "reasoning": {"type": "string", "maxLength": 600},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "effort": {"type": "string", "enum": ["low", "medium", "high"]},
        },
    }


def effort_block(objective: str, task_shape: str, max_cost_tier: str) -> list[str]:
    """The EFFORT section: what the control does, and this task's default.

    Unconditional — every call has an objective and a shape, so there is
    always a default to state, unlike OBJECTIVE (which is silent for
    `balanced`) or TRACK RECORD (which is silent with no history). That is
    what makes the route-v5 bump not byte-identical to v4 even on a cold
    start; see the version comment above `ROUTING_PROMPT_VERSION`.

    `max_cost_tier` stands in for the chosen model's own tier here — no model
    has been picked yet at prompt-render time, and the harness ceiling is the
    closest available proxy for "how much this decision may spend" (see
    `objectives.default_effort`'s `verdict`-shape tier table).
    """
    default = default_effort(objective, task_shape, max_cost_tier)
    return [
        "",
        "EFFORT",
        "  Effort scales how much thinking and output the chosen model spends "
        "on this call — low is fastest and cheapest, high spends the most to "
        "get the best result.",
        f"  Default for this objective and task shape: {default}.",
        "  Choose low unless the task shape needs multi-step judgment; never "
        "exceed the default under token_conservation or eco.",
    ]


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
    priors: dict | None = None,
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
    lines += effort_block(objective, task_shape, max_cost_tier)
    lines += objective_block(objective)

    recorded = {mid: p for mid, p in (priors or {}).items() if mid in {m.id for m in candidates}}
    if recorded:
        lines += track_record_block(recorded)
        lines += unrecorded_block([m.id for m in candidates if m.id not in recorded])
        lines += [f"  - {rule}" for rule in TRACK_RECORD_RULES]
    return "\n".join(lines)
