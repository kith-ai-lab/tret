"""Per-agent scored ranking: the shadow decision's arithmetic.

An "agent" is a harness. Its record is its own `run_outcomes` rows, each marked
acceptable or not. The score of a model is what one *acceptable* result is
expected to cost it on this agent (USD or Wh, by objective), using a
cautious acceptable rate so a thin record cannot win on luck. The pooled priors
(`priors_base`) only seed models the agent has not tried enough.

Pure by design: no database, no clock, no settings. Callers pass rows, the pooled
priors, the model pool, the exploration draw and `now`, so every rule is testable
with hand-built rows and the same inputs always give the same output.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from tret.router_llm.priors_base import Z_CONSERVATIVE, _decay

if TYPE_CHECKING:
    from tret.providers.catalog import ModelInfo
    from tret.router_llm.priors_base import ModelPrior

# Stored beside the decision so a later change to the rules is recognisable.
SCORING_VERSION = "agent-score-v1"
# Decayed weight a model needs on an agent before its own results count.
MIN_AGENT_RUNS = 5.0
# A run under a different objective counts for this much of a run.
CROSS_OBJECTIVE_WEIGHT = 0.5
# The pooled prior never counts for more than this many runs.
POOLED_PRIOR_MAX_RUNS = 3.0
# Runs the agent-wide average counts for, for a model with no pooled prior.
AGENT_AVERAGE_PRIOR_RUNS = 1.0
# Half a run each way: keeps the estimate proper at 0% and 100%.
BASE_PSEUDO = 0.5
# Share of decisions that would try a model the agent has no record of.
EXPLORATION_SHARE = 0.10
# Under "prefer", an open-weight model may win within this fraction of the best score.
OPEN_WEIGHTS_MARGIN = 0.15
# A model whose cautious rate is under this share of the best one leaves the main ranking.
RELIABILITY_FLOOR = 0.8
# Lowest acceptable rate used as a divisor, so a score is always finite.
RATE_FLOOR = 0.01
# Most models listed in the stored block.
SHOWN_MODELS = 8
# Clamp on the list-price calibration: how much this agent's tried models actually
# cost against their own list-price estimate, mostly cache reads and writes that
# the token counts leave out.
CALIBRATION_MIN = 0.1
CALIBRATION_MAX = 20.0
# Objective to the unit its resource is measured in. "quality" is deliberately
# absent: it is not scored. So is "token_conservation": recorded token counts leave
# out cache reads, so tokens per acceptable result is not comparable across models.
UNITS = {"balanced": "usd", "eco": "wh"}

_EXCLUDED_CLASSES = ("infra", "timeout", "unsettled")


@dataclass(frozen=True)
class AgentRow:
    """One `run_outcomes` row of an agent, with its batch target's class if any."""

    model_id: str
    objective: str
    observed_at: datetime | None
    outcome_class: str
    cost_usd: float
    energy_wh: float | None
    input_tokens: int
    output_tokens: int
    findings_rejected: int
    is_batch: bool
    target_class: str | None


def acceptable(row: AgentRow, excluded_classes: tuple[str, ...] = _EXCLUDED_CLASSES) -> bool | None:
    """True or False for a row that counts, None for one that is left out.

    A batch row is judged by its target's class (the batch scorecard's rule), so a
    target no current attempt points at, or one still unsettled or lost to
    infrastructure, says nothing about the model. Any other row is acceptable when
    it delivered and no finding was rejected."""
    if row.is_batch:
        if row.target_class is None or row.target_class in excluded_classes:
            return None
        return row.target_class == "success" and row.outcome_class == "delivered"
    return row.outcome_class == "delivered" and row.findings_rejected == 0


def _aware(ts: datetime | None) -> datetime | None:
    if ts is not None and ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts


def _weight(row: AgentRow, objective: str, now: datetime) -> float:
    observed = _aware(row.observed_at)
    age_days = ((now - observed).total_seconds() / 86400.0) if observed else 0.0
    w = _decay(age_days)
    if row.objective != objective:
        w *= CROSS_OBJECTIVE_WEIGHT
    return w


def _row_key(row: AgentRow, now: datetime):
    """A total order over rows, so sums never depend on the order callers pass them in."""
    return (
        row.model_id,
        _aware(row.observed_at) or now,
        row.objective,
        row.outcome_class,
        row.cost_usd,
        -1.0 if row.energy_wh is None else row.energy_wh,
        row.input_tokens,
        row.output_tokens,
        row.findings_rejected,
        row.is_batch,
        row.target_class or "",
    )


class _Acc:
    __slots__ = ("runs", "weight", "ok", "cost", "energy", "energy_w", "inp", "out", "recent")

    def __init__(self) -> None:
        self.runs = 0
        self.weight = 0.0
        self.ok = 0.0
        self.cost = 0.0
        self.energy = 0.0
        self.energy_w = 0.0
        self.inp = 0.0
        self.out = 0.0
        self.recent: list[tuple[datetime, bool]] = []


def _accumulate(rows: list[AgentRow], objective: str, now: datetime) -> tuple[dict[str, _Acc], int]:
    accs: dict[str, _Acc] = {}
    counted = 0
    for row in sorted(rows, key=lambda r: _row_key(r, now)):
        ok = acceptable(row)
        if ok is None:
            continue
        counted += 1
        w = _weight(row, objective, now)
        a = accs.setdefault(row.model_id, _Acc())
        a.runs += 1
        a.weight += w
        a.ok += w if ok else 0.0
        a.cost += w * float(row.cost_usd or 0.0)
        if row.energy_wh is not None:
            a.energy += w * float(row.energy_wh)
            a.energy_w += w
        a.inp += w * float(row.input_tokens or 0)
        a.out += w * float(row.output_tokens or 0)
        a.recent.append((_aware(row.observed_at) or now, ok))
    return accs, counted


def models_with_record(rows: list[AgentRow], objective: str, now: datetime) -> set[str]:
    """Models whose decayed weight on the agent reaches `MIN_AGENT_RUNS`."""
    accs, _ = _accumulate(rows, objective, now)
    return {m for m, a in accs.items() if a.weight >= MIN_AGENT_RUNS}


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _sig(x: float | None) -> float | None:
    if x is None or not math.isfinite(x):
        return None
    return float(f"{x:.6g}")


def _rate(x: float) -> float:
    return round(x, 4) if math.isfinite(x) else 0.0


def _estimate(m: ModelInfo, objective: str, in_tok: int, out_tok: int) -> float | None:
    try:
        unit = UNITS[objective]
        if unit == "usd":
            v = float(m.cost_usd(in_tok, out_tok))
        else:
            v = float(m.energy_wh(in_tok, out_tok))
    except Exception:  # noqa: BLE001 - a model with no estimate is excluded, not fatal
        return None
    return v if math.isfinite(v) else None


def _listed(m, objective: str, in_tok: int, out_tok: int, calibration: float) -> float | None:
    est = _estimate(m, objective, in_tok, out_tok)
    return None if est is None else calibration * est


def _calibration(accs: dict[str, _Acc], with_record, models, objective: str) -> float:
    """Observed cost over list-price estimate, for the tried models, each at its own
    mean tokens, so a list-price estimate is in the same terms as an observed cost."""
    unit = UNITS[objective]
    num = den = 0.0
    for m in models:
        a = accs.get(m.id)
        if a is None or m.id not in with_record:
            continue
        if unit == "usd":
            obs = a.cost / a.weight
        elif a.energy_w > 0:
            obs = a.energy / a.energy_w
        else:
            continue
        listed = _estimate(m, objective, round(a.inp / a.weight), round(a.out / a.weight))
        if listed is None or not (obs > 0 and listed > 0 and math.isfinite(obs)):
            continue
        num += a.weight * obs
        den += a.weight * listed
    if den <= 0:
        return 1.0
    return _clamp(num / den, CALIBRATION_MIN, CALIBRATION_MAX)


def _prefer_open(order: list[dict], key: str, policy: str) -> tuple[dict | None, bool]:
    """The head of `order` (sorted by `key`), or under "prefer" the best-placed
    open-weight model within the margin of it. Returns (entry, applied)."""
    if not order:
        return None, False
    first = order[0]
    if policy != "prefer" or first["_open"] is True:
        return first, False
    for e in order:
        if e["_open"] is True:
            if e[key] <= first[key] * (1.0 + OPEN_WEIGHTS_MARGIN):
                return e, True
            break
    return first, False


def decide(
    *,
    rows: list[AgentRow],
    pooled: dict[str, ModelPrior],
    models: list[ModelInfo],
    objective: str,
    open_weights: str,
    exploration_share: float,
    draw: float | None,
    live_choice: str | None,
    now: datetime,
    exploration_live: bool = True,
) -> dict:
    """What the scored ranking would pick for this agent, with every number."""
    if objective not in UNITS:
        return {"version": SCORING_VERSION, "status": "objective_not_scored", "objective": objective}
    unit = UNITS[objective]
    accs, counted = _accumulate(rows, objective, now)
    with_record = {m for m, a in accs.items() if a.weight >= MIN_AGENT_RUNS}
    if not any(m.id in with_record for m in models):
        return {
            "version": SCORING_VERSION,
            "status": "no_agent_record",
            "objective": objective,
            "agent": {"runs": counted},
        }

    # Typical tokens come from models with a record only, so a model's first few
    # runs change nothing but its own row.
    tot_w = sum(accs[m].weight for m in with_record)
    typical_in = int(round(sum(accs[m].inp for m in with_record) / tot_w)) if tot_w > 0 else 0
    typical_out = int(round(sum(accs[m].out for m in with_record) / tot_w)) if tot_w > 0 else 0
    rec_w = tot_w
    agent_rate = sum(accs[m].ok for m in with_record) / rec_w if rec_w > 0 else 0.0

    calibration = _calibration(accs, with_record, models, objective)

    entries: list[dict] = []
    for m in models:
        a = accs.get(m.id)
        has = m.id in with_record
        pr = pooled.get(m.id)
        if pr is not None:
            p0, w0 = pr.delivered_rate, min(POOLED_PRIOR_MAX_RUNS, pr.effective_n)
            basis = "pooled"
        else:
            p0, w0 = agent_rate, AGENT_AVERAGE_PRIOR_RUNS
            basis = "agent_average"
        alpha = BASE_PSEUDO + p0 * w0
        beta = BASE_PSEUDO + (1.0 - p0) * w0
        if has and a is not None:
            alpha += a.ok
            beta += a.weight - a.ok
            basis = "agent"
        total = alpha + beta
        mean = alpha / total
        sd = math.sqrt(max(0.0, alpha * beta / (total * total * (total + 1.0))))
        cautious = _clamp(mean - Z_CONSERVATIVE * sd, RATE_FLOOR, 1.0)
        optimistic = _clamp(mean + Z_CONSERVATIVE * sd, RATE_FLOOR, 1.0)

        resource: float | None
        resource_basis = "list_price"
        if has and a is not None:
            observed: float | None = None
            if unit == "usd":
                observed = a.cost / a.weight
            elif unit == "wh" and a.energy_w > 0:
                observed = a.energy / a.energy_w
            if observed is not None and (
                observed > 0
                # An observed zero is real only for a model that lists at zero too;
                # for a paid model it is a missing cost, not a free run.
                or _estimate(m, objective, round(a.inp / a.weight), round(a.out / a.weight)) == 0
            ):
                resource, resource_basis = observed, "observed"
            else:
                resource = _listed(m, objective, typical_in, typical_out, calibration)
        else:
            resource = _listed(m, objective, typical_in, typical_out, calibration)
        sat_out = False
        if a is not None and len(a.recent) >= 2:
            last_two = sorted(a.recent, key=lambda t: t[0], reverse=True)[:2]
            sat_out = not any(ok for _, ok in last_two)

        entry = {
            "model": m.id,
            "basis": basis,
            "runs": a.runs if a else 0,
            "effective_n": round(a.weight, 4) if a else 0.0,
            "acceptable_rate": _rate(mean),
            "cautious_rate": _rate(cautious),
            "optimistic_rate": _rate(optimistic),
            "resource_per_run": None,
            "resource_basis": resource_basis,
            "score": None,
            "optimistic_score": None,
            "open_weights": m.open_weights,
            "excluded": None,
            "sat_out": sat_out,
            "_open": m.open_weights,
            "_has": has,
            "_cautious": cautious,
            "_optimistic": optimistic,
            "_score": None,
            "_opt_score": None,
        }
        # Zero is a valid resource (a free or local model); only a missing,
        # non-finite or negative estimate is no estimate.
        if resource is None or not math.isfinite(resource) or resource < 0:
            entry["excluded"] = "no_estimate"
        else:
            entry["resource_per_run"] = _sig(resource)
            entry["_score"] = resource / cautious
            entry["_opt_score"] = resource / optimistic
            entry["score"] = _sig(entry["_score"])
            entry["optimistic_score"] = _sig(entry["_opt_score"])
        entries.append(entry)

    scored = [e for e in entries if e["_score"] is not None]
    tried = [e for e in scored if e["_has"]]
    if not tried:
        return {
            "version": SCORING_VERSION,
            "status": "no_agent_record",
            "objective": objective,
            "agent": {"runs": counted},
        }
    best_tried = max(e["_cautious"] for e in tried)
    threshold = RELIABILITY_FLOOR * best_tried
    for e in scored:
        if e["_has"]:
            if e["_cautious"] < threshold:
                e["excluded"] = "below_reliability_floor"
        elif e["_cautious"] < best_tried:
            # No record on this agent: it takes the main slot only when even its
            # cautious rate matches the best tried model; otherwise it is reached
            # through the exploration share alone.
            e["excluded"] = "no_record"

    ranking = sorted(
        (e for e in scored if e["excluded"] is None),
        # On a tie an observed cost beats a calibrated list price.
        key=lambda e: (e["_score"], -e["_cautious"], e["resource_basis"] != "observed", e["model"]),
    )
    main, main_applied = _prefer_open(ranking, "_score", open_weights)
    ranked_pick = main["model"] if main else None

    eligible = sorted(
        # The ranked pick is never also the exploration candidate.
        (e for e in scored if not e["_has"] and not e["sat_out"] and e["model"] != ranked_pick),
        key=lambda e: (e["_opt_score"], -e["_optimistic"], e["model"]),
    )
    cand, cand_applied = _prefer_open(eligible, "_opt_score", open_weights)
    candidate = cand["model"] if cand else None
    fired = bool(
        exploration_share > 0 and candidate is not None and draw is not None and draw < exploration_share
    )
    # With exploration not live the draw is still recorded, but the pick is the ranking's.
    pick = candidate if fired and exploration_live else ranked_pick

    # Listing: the ranking in order, with room kept for the models the reader
    # needs to see whatever their rank.
    required: list[str] = []
    for mid in (ranked_pick, candidate, live_choice):
        if mid is not None and mid not in required and any(e["model"] == mid for e in entries):
            required.append(mid)
    ranking_ids = [e["model"] for e in ranking]
    extras: list[str] = []
    while True:
        prefix = ranking_ids[: max(0, SHOWN_MODELS - len(extras))]
        missing = [mid for mid in required if mid not in prefix]
        if len(missing) == len(extras):
            break
        extras = missing
    by_id = {e["model"]: e for e in entries}
    shown_ids = [*ranking_ids[: max(0, SHOWN_MODELS - len(extras))], *extras]
    # Spare room goes to the models the ranking left out, so the reader can see why.
    shown_ids += [e["model"] for e in entries if e["model"] not in shown_ids][
        : max(0, SHOWN_MODELS - len(shown_ids))
    ]
    shown = [{k: v for k, v in by_id[mid].items() if not k.startswith("_")} for mid in shown_ids]

    return {
        "version": SCORING_VERSION,
        "status": "scored",
        "objective": objective,
        "unit": unit,
        "pick": pick,
        "ranked_pick": ranked_pick,
        "live_choice": live_choice,
        "agrees_with_live": pick == live_choice,
        "exploration": {
            "share": exploration_share,
            "draw": None if draw is None else round(draw, 6),
            "fired": fired,
            "applied": fired and exploration_live,
            "candidate": candidate,
            "open_weights_applied": cand_applied,
        },
        "open_weights": {
            "policy": open_weights,
            "margin": OPEN_WEIGHTS_MARGIN,
            "applied": main_applied,
        },
        "reliability_floor": {
            "ratio": RELIABILITY_FLOOR,
            "best_tried_cautious_rate": _rate(best_tried),
            "threshold": _rate(threshold),
        },
        "agent": {
            # Every counted row for the agent, across all models, including models
            # below the record threshold or outside the pool.
            "runs": counted,
            "typical_input_tokens": typical_in,
            "typical_output_tokens": typical_out,
            "acceptable_rate": _rate(agent_rate),
            "list_price_calibration": round(calibration, 4),
        },
        "ranking": ranking_ids,
        "models": shown,
        "reason": _reason(
            main, ranking, main_applied, candidate, fired, exploration_share, draw, unit,
            exploration_live,
        ),
    }


def _reason(
    main, ranking, main_applied, candidate, fired, share, draw, unit, exploration_live=True
) -> str:
    if main is None:
        head = "No model had a usable estimate, so nothing was ranked."
    else:
        runner = next((e for e in ranking if e is not main), None)
        head = (
            f"Ranked pick {main['model']}: score {main['score']} {unit} per acceptable result "
            f"at a cautious rate of {main['cautious_rate']} over {main['runs']} runs"
        )
        if main_applied:
            head += " (an open-weight model within the margin of the best score)"
        if runner is not None:
            head += f"; runner-up {runner['model']} at {runner['score']}"
        head += "."
    if candidate is None:
        tail = "No untried model was eligible for exploration."
    elif fired and not exploration_live:
        tail = (
            f"The exploration draw {round(draw, 4)} was under {share}, so {candidate} would "
            "have been tried but the exploration share is not live for this agent yet."
        )
    elif fired:
        tail = f"The exploration draw {round(draw, 4)} was under {share}, so {candidate} was picked to try."
    elif share > 0 and draw is not None:
        tail = f"The exploration draw {round(draw, 4)} was not under {share}, so {candidate} was not tried."
    else:
        tail = f"Exploration is off, so {candidate} was not tried."
    return f"{head} {tail}"
