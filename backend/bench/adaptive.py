"""`model_policy.adaptive` — what a harness lets bench do about what it learns.

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

COMPACTION_MODES = ("auto", "off")
ESCALATION_MODES = ("off", "on_stall")

# A run may not spend more than this share of a model's context window before the
# engine intervenes. The remainder has to hold the turn's own output, so the
# headroom is not slack — it is the part of the window that is already spoken for.
DEFAULT_CONTEXT_HEADROOM = 0.8
MIN_CONTEXT_HEADROOM = 0.3
MAX_CONTEXT_HEADROOM = 0.95

# Changing model mid-run voids the prompt cache and re-sends the transcript, so
# it is worth doing once when a run is genuinely stuck and rarely worth doing
# twice.
DEFAULT_MAX_SWITCHES = 1
MAX_MAX_SWITCHES = 3


@dataclass(frozen=True)
class AdaptivePolicy:
    learn_from_outcomes: bool = True
    context_headroom: float = DEFAULT_CONTEXT_HEADROOM
    compaction: str = "auto"
    escalation: str = "on_stall"
    max_switches: int = DEFAULT_MAX_SWITCHES

    def to_json(self) -> dict:
        return asdict(self)


DEFAULT_ADAPTIVE = AdaptivePolicy()
# What the evals and the benchmark arms pin: every adaptive behavior off, so a
# replayed run depends on the replay and nothing else.
STATIC_ADAPTIVE = AdaptivePolicy(
    learn_from_outcomes=False, compaction="off", escalation="off", max_switches=0
)


def adaptive_of(model_policy: dict | None) -> AdaptivePolicy:
    """The adaptive settings a policy asks for, defaulting to all of them on.

    Unknown or out-of-range values fall back to the default *here*, because the
    API validates on write (`api/harnesses.py::_validate_policy`) and a bad value
    should be refused at the door rather than silently steering a run from inside
    the engine. This is the same division `objectives.objective_of` draws.
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
    return AdaptivePolicy(
        learn_from_outcomes=bool(block.get("learn_from_outcomes", True)),
        context_headroom=headroom,
        compaction=compaction if compaction in COMPACTION_MODES else DEFAULT_ADAPTIVE.compaction,
        escalation=escalation if escalation in ESCALATION_MODES else DEFAULT_ADAPTIVE.escalation,
        max_switches=switches,
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
    return None
