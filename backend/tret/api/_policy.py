"""`model_policy` validation, shared by every endpoint that accepts one.

`api/harnesses.py`'s create/update endpoints validate a policy before it is
saved; `api/routing.py`'s preview endpoint validates an *inline* policy (one
that may never be saved at all — a form previewing edits, or an override on
top of a saved harness) before it is ever handed to the router. Both must
reject the same malformed input the same way: a preview that accepted a
policy a saved harness would 422 on would be a way to route around validation
entirely, not just a way to see a cost estimate.
"""
from __future__ import annotations

from fastapi import HTTPException

from tret.adaptive import validation_error as adaptive_validation_error
from tret.providers.catalog import get_catalog
from tret.router_llm.objectives import DEFAULT_OBJECTIVE, OBJECTIVES
from tret.router_llm.router import TIER_ORDER


def validate_policy(policy: dict) -> None:
    mode = policy.get("mode")
    if mode not in ("auto", "pinned"):
        raise HTTPException(422, "model_policy.mode must be 'auto' or 'pinned'")
    catalog = get_catalog()
    if mode == "pinned":
        model = policy.get("model")
        # Checked before the catalog lookup below: `catalog.get(5)` on a
        # non-string `model` is not a lookup miss, it is a TypeError — a
        # malformed policy must 422, never 500.
        if model is not None and not isinstance(model, str):
            raise HTTPException(422, "model_policy.model must be a string")
        if not model or catalog.get(model) is None:
            raise HTTPException(422, f"model_policy.model '{model}' is not in the catalog")
    allowed = policy.get("allowed")
    if allowed is not None:
        # Same reasoning as `model` above: `for m in allowed` on a non-list
        # (an int, a dict) either raises TypeError or silently iterates
        # something that was never a list of model ids — both wrong, and the
        # first one is a 500 rather than the 422 a malformed policy deserves.
        if not isinstance(allowed, list) or not all(isinstance(m, str) for m in allowed):
            raise HTTPException(422, "model_policy.allowed must be a list of model id strings")
        for m in allowed:
            if catalog.get(m) is None:
                raise HTTPException(422, f"allowed model '{m}' is not in the catalog")
    # `local` is a real ceiling, not a floor: TIER_ORDER ranks it below economy,
    # so capping there leaves local models as the only candidates — the
    # zero-cloud policy (docs/local-models.md), expressed as a cost tier.
    tier = policy.get("max_cost_tier", "premium")
    if tier not in TIER_ORDER:
        raise HTTPException(
            422, f"max_cost_tier must be one of {'|'.join(TIER_ORDER)}"
        )
    # An unrecognized objective must never fall through to the default: silently
    # routing on "balanced" when the operator asked for "eco" is exactly the kind
    # of quiet substitution this platform exists to rule out.
    # `or` (not a get default) so unset/None reads as the default exactly the way
    # router_llm.objectives.objective_of reads it at run time.
    objective = policy.get("objective") or DEFAULT_OBJECTIVE
    if objective not in OBJECTIVES:
        raise HTTPException(422, f"model_policy.objective must be one of {'|'.join(OBJECTIVES)}")
    # The adaptive block, same rule as everything above it: refused at the door,
    # never read as a default. A misspelled key here would silently leave a
    # behavior on that the operator believed they had turned off — and two of
    # them (`escalation`, `compaction`) let a run change what it is doing
    # mid-flight, which is exactly the kind of thing to be sure about.
    problem = adaptive_validation_error(policy.get("adaptive"))
    if problem:
        raise HTTPException(422, problem)
