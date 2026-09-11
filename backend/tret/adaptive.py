"""`model_policy.adaptive` — what a harness lets tret do about what it learns.

One block, read in three places that must agree about it: the router (whether
recorded outcomes steer the choice), the engine (whether a run may compact its
own context or change model part-way), and the API (what a client may write).

The defaults are on. That is a deliberate choice and it needs its justification
stated, because "on by default" usually means "every existing harness silently
changes behavior":

* **Day one is a no-op.** With no recorded outcomes there are no priors, the
  router prompt renders exactly the bytes it rendered before, and the engine's
  interventions have nothing to fire on. Behavior diverges only as evidence
  accumulates, which is the point.
* **Learning cannot widen a policy.** Nothing here lets evidence add a model to
  `allowed` or lift `max_cost_tier`. Adaptive routing reorders and demotes
  *within* what the harness already permits, and mid-run switching is held to the
  same ceiling as the original route.
* **Reproducibility is opt-out, not absent.** The golden-run evals and both
  benchmark arms set `learn_from_outcomes: false` explicitly, because a replay
  suite whose answers depend on how many runs the database happens to hold is not
  a replay suite, and two benchmark arms that adapt at different rates are not a
  comparison.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from tret.router_llm.objectives import TIER_ORDER

COMPACTION_MODES = ("auto", "off")
# What the supervisor (engine/supervisor.py) is allowed to do about a run that
# is not going well, from least to most willing to act:
#
# * **off**       — never touch the model. A stuck run stays stuck.
# * **on_stall**  — switch model, but only once a run is *definitely* stuck:
#                   the repair budget exhausted (3 terminal-validation
#                   failures), a retrieval loop confirmed twice, or most of the
#                   iteration budget burned with nothing recorded. Every trigger
#                   here is a point of no return the engine has already reached
#                   on its own account, not a prediction.
# * **on_quality**— everything `on_stall` does, plus an earlier, cheaper signal
#                   (2 terminal-validation failures, or one retrieval-loop
#                   trip) that raises reasoning effort on the *same* model
#                   before ever paying for a switch's transcript re-send. Only
#                   escalates to a switch if effort is already maxed, the
#                   model does not accept the control, or it was already
#                   raised once this run.
ESCALATION_MODES = ("off", "on_stall", "on_quality")

# A run may not spend more than this share of a model's context window before the
# engine intervenes. The remainder has to hold the turn's own output, so the
# headroom is not slack — it is the part of the window that is already spoken for.
DEFAULT_CONTEXT_HEADROOM = 0.8
MIN_CONTEXT_HEADROOM = 0.3
MAX_CONTEXT_HEADROOM = 0.95

# Changing model mid-run voids the prompt cache and re-sends the transcript, so
# it is worth doing once when a run is genuinely stuck and rarely worth doing
# twice.
#
# `max_switches: 0` disables switching, not `on_quality`'s effort rung: raising
# reasoning effort on the model already running (engine/supervisor.py's Rung 1)
# spends no switch, so a harness that wants the cheap same-model rescue but
# never wants a full model change sets this to 0 rather than turning escalation
# off outright — `assess()` still returns `KIND_EFFORT` in that case and only
# refuses `KIND_SWITCH`.
DEFAULT_MAX_SWITCHES = 1
MAX_MAX_SWITCHES = 3

# The probability an eligible decision explores an untried model instead of
# asking the router — see `router_llm.router.ModelRouter.route`'s exploration
# block. Bounded well below "sometimes" territory: this is a bandit's coin
# flip on the cheapest, schema-validated shape a harness runs, not a general
# routing strategy, and 0.2 is already one call in five spent on a model with
# no track record.
DEFAULT_EXPLORATION = 0.05
MAX_EXPLORATION = 0.2
# The cost ceiling exploration itself is willing to gamble on, independent of
# (and never wider than) the harness's own `max_cost_tier` — an operator who
# raises the harness ceiling for capability reasons should not thereby also
# raise how much an unproven model is allowed to cost while being tried out.
DEFAULT_EXPLORATION_MAX_COST_TIER = "economy"


@dataclass(frozen=True)
class AdaptivePolicy:
    """
    | field                       | default    | range/values                |
    |------------------------------|-----------|------------------------------|
    | `learn_from_outcomes`        | `True`    | bool                         |
    | `context_headroom`           | `0.8`     | 0.3–0.95                     |
    | `compaction`                 | `"auto"`  | `auto` \\| `off`             |
    | `escalation`                 | `on_quality` | `off` \\| `on_stall` \\| `on_quality` |
    | `max_switches`               | `1`       | 0–3                          |
    | `exploration`                | `0.05`    | 0.0–0.2                      |
    | `exploration_max_cost_tier`  | `"economy"` | `local` \\| `economy` \\| `standard` \\| `premium` |

    `exploration` and `exploration_max_cost_tier` govern the same coin flip
    described in `router_llm.router.ModelRouter.route`: with probability
    `exploration`, a `balanced`/`extraction` decision with an untried
    candidate within `exploration_max_cost_tier` picks that candidate outright
    instead of asking the router LLM. `exploration: 0` (not the harness
    ceiling) is what actually turns the behavior off — the guardrails around
    it (objective, task shape, tier, no pin/override) narrow *when* it can
    fire, this is *whether* it does at all.
    """

    learn_from_outcomes: bool = True
    context_headroom: float = DEFAULT_CONTEXT_HEADROOM
    compaction: str = "auto"
    escalation: str = "on_quality"
    max_switches: int = DEFAULT_MAX_SWITCHES
    exploration: float = DEFAULT_EXPLORATION
    exploration_max_cost_tier: str = DEFAULT_EXPLORATION_MAX_COST_TIER

    def to_json(self) -> dict:
        return asdict(self)


DEFAULT_ADAPTIVE = AdaptivePolicy()
# What the evals and the benchmark arms pin: every adaptive behavior off, so a
# replayed run depends on the replay and nothing else. Exploration is a
# routing behavior like the rest of this block, so a reproducible replay pins
# it to 0 too — a golden run must never pick a model the replay did not ask
# for.
STATIC_ADAPTIVE = AdaptivePolicy(
    learn_from_outcomes=False,
    compaction="off",
    escalation="off",
    max_switches=0,
    exploration=0.0,
)


def adaptive_of(model_policy: dict | None) -> AdaptivePolicy:
    """The adaptive settings a policy asks for, defaulting to all of them on.

    Unknown or out-of-range values fall back to the default *here*, because the
    API validates on write (`api/harnesses.py::_validate_policy`) and a bad value
    should be refused at the door rather than silently steering a run from inside
    the engine. This is the same division `objectives.objective_of` draws.

    `exploration` is the one field that breaks that pattern on purpose: an
    out-of-range stored value fails *closed* to 0.0 rather than reviving the
    0.05 default, because it is a probability gating an unproven model being
    picked with no router call at all — see the comment at its own check
    below.
    """
    block = (model_policy or {}).get("adaptive")
    if not isinstance(block, dict):
        return DEFAULT_ADAPTIVE

    headroom = block.get("context_headroom", DEFAULT_CONTEXT_HEADROOM)
    try:
        headroom = float(headroom)
    except (TypeError, ValueError):
        headroom = DEFAULT_CONTEXT_HEADROOM
    if not MIN_CONTEXT_HEADROOM <= headroom <= MAX_CONTEXT_HEADROOM:
        headroom = DEFAULT_CONTEXT_HEADROOM

    switches = block.get("max_switches", DEFAULT_MAX_SWITCHES)
    try:
        switches = int(switches)
    except (TypeError, ValueError):
        switches = DEFAULT_MAX_SWITCHES
    switches = max(0, min(switches, MAX_MAX_SWITCHES))

    compaction = block.get("compaction", DEFAULT_ADAPTIVE.compaction)
    escalation = block.get("escalation", DEFAULT_ADAPTIVE.escalation)

    exploration = block.get("exploration", DEFAULT_ADAPTIVE.exploration)
    try:
        exploration = float(exploration)
    except (TypeError, ValueError):
        exploration = DEFAULT_ADAPTIVE.exploration
    # Fails *closed* to 0.0, not to the default — the deliberate exception to
    # this function's own "unknown/out-of-range falls back to the default"
    # rule. Every other field here defaults to the harness-default *behavior*
    # when it cannot be trusted; `exploration` is a probability that gates
    # whether an unproven model gets picked outright with no router call, and
    # a stored value already out of the API's own valid range (a downgrade
    # from a since-lowered `MAX_EXPLORATION`, or a row written before
    # validation existed) is not a value this deployment ever meant to run
    # with. Reviving it at the 0.05 default would silently turn exploration
    # back on for a harness whose own stored intent cannot be trusted at all.
    if not 0.0 <= exploration <= MAX_EXPLORATION:
        exploration = 0.0

    exploration_max_cost_tier = block.get(
        "exploration_max_cost_tier", DEFAULT_ADAPTIVE.exploration_max_cost_tier
    )
    if exploration_max_cost_tier not in TIER_ORDER:
        exploration_max_cost_tier = DEFAULT_ADAPTIVE.exploration_max_cost_tier

    return AdaptivePolicy(
        learn_from_outcomes=bool(block.get("learn_from_outcomes", True)),
        context_headroom=headroom,
        compaction=compaction if compaction in COMPACTION_MODES else DEFAULT_ADAPTIVE.compaction,
        escalation=escalation if escalation in ESCALATION_MODES else DEFAULT_ADAPTIVE.escalation,
        max_switches=switches,
        exploration=exploration,
        exploration_max_cost_tier=exploration_max_cost_tier,
    )


def validation_error(block) -> str | None:
    """Why this `adaptive` block is unacceptable, or None if it is fine.

    Returns a message rather than raising so the API layer can turn it into its
    own HTTPException and the engine can ignore it entirely.
    """
    if block is None:
        return None
    if not isinstance(block, dict):
        return "model_policy.adaptive must be an object"
    unknown = sorted(set(block) - set(DEFAULT_ADAPTIVE.to_json()))
    if unknown:
        # Refused rather than ignored: a misspelled key that is silently dropped
        # reads as a setting that was applied, which is the failure this whole
        # block exists to avoid.
        return (
            f"model_policy.adaptive has unknown key(s) {unknown} "
            f"(known: {sorted(DEFAULT_ADAPTIVE.to_json())})"
        )
    if "learn_from_outcomes" in block and not isinstance(block["learn_from_outcomes"], bool):
        return "model_policy.adaptive.learn_from_outcomes must be a boolean"
    if "compaction" in block and block["compaction"] not in COMPACTION_MODES:
        return f"model_policy.adaptive.compaction must be one of {'|'.join(COMPACTION_MODES)}"
    if "escalation" in block and block["escalation"] not in ESCALATION_MODES:
        return f"model_policy.adaptive.escalation must be one of {'|'.join(ESCALATION_MODES)}"
    if "context_headroom" in block:
        value = block["context_headroom"]
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return "model_policy.adaptive.context_headroom must be a number"
        if not MIN_CONTEXT_HEADROOM <= float(value) <= MAX_CONTEXT_HEADROOM:
            return (
                "model_policy.adaptive.context_headroom must be between "
                f"{MIN_CONTEXT_HEADROOM} and {MAX_CONTEXT_HEADROOM}"
            )
    if "max_switches" in block:
        value = block["max_switches"]
        if not isinstance(value, int) or isinstance(value, bool):
            return "model_policy.adaptive.max_switches must be an integer"
        if not 0 <= value <= MAX_MAX_SWITCHES:
            return f"model_policy.adaptive.max_switches must be between 0 and {MAX_MAX_SWITCHES}"
    if "exploration" in block:
        value = block["exploration"]
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return "model_policy.adaptive.exploration must be a number"
        if not 0.0 <= float(value) <= MAX_EXPLORATION:
            return f"model_policy.adaptive.exploration must be between 0.0 and {MAX_EXPLORATION}"
    if "exploration_max_cost_tier" in block and block["exploration_max_cost_tier"] not in TIER_ORDER:
        return (
            "model_policy.adaptive.exploration_max_cost_tier must be one of "
            f"{sorted(TIER_ORDER, key=TIER_ORDER.get)}"
        )
    return None
