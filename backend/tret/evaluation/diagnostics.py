"""Day-1 diagnostics of an Engine 1 tournament (disclosure evaluation engine, PR 7).

Every tournament carries computed gates, each with a threshold that pauses the pass (or, for the
flip rate, drops the offending family), so a bad judge stops before it spends the whole budget:

* `length_bias`: Pearson and Spearman r between a module's word count and its rating, per pool and
  overall, with claim density alongside for contrast. Warn from `eval_gate_length_r_warn`, pause above
  `eval_gate_length_r_pause` (on the overall Spearman r). Needs `eval_gate_min_rated` rated modules.
* `flip_rate`: the swapped re-judgements (same juror, other order) give a flip rate per juror family.
  A family above `eval_gate_flip_drop` (with `eval_gate_min_swaps` swaps) is dropped from the jury for
  the rest of the pass; fewer than two families left pauses (`jury_degraded`).
* `position_bias`: the same flip rate pooled over the swaps of the families still on the jury, so a
  small pass (about 8 swaps at N=48) can still trip. Above `eval_gate_flip_drop` with
  `eval_gate_min_swaps_pooled` swaps pauses.
* `juror_agreement`: Cohen's kappa pooled over all double-judged matches (the per-pair table stays in the
  response). Below `eval_gate_kappa_pause` (with `eval_gate_min_doubles` matches) pauses: the module
  prompt is too vague.
* `anchor_drift`: the anchor set's fixed pairs are re-judged every pass (`evaluation/anchors.py`); more
  changed verdicts than `eval_gate_anchor_changes_pause` pauses.
* `parts_vs_whole`, `score_clumping`: need Engine 2 (PR 8); reported `not_applicable`.

`compute_diagnostics` is the read model of `GET .../diagnostics` and also what the runner evaluates
after each round (`compute_from_inputs`, run in a thread with no session open). Length bias uses
PROVISIONAL ratings: the Bradley-Terry fit over the matches done so far, never written to
`module_ratings`. Anchor matches are excluded from the fit and from every other diagnostic.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from tret.config import get_settings
from tret.db.models import EvalAnchorSet, EvalMatch, EvalTournament
from tret.evaluation import pools as pl
from tret.evaluation.ratings import pearson, spearman

CATEGORIES = ("a", "b", "tie")
GATE_KEYS = ("length_bias", "flip_rate", "position_bias", "juror_agreement", "anchor_drift", "parts_vs_whole", "score_clumping")
# The gates whose trip pauses the pass until someone acknowledges it (flip_rate drops a family
# instead; it needs acknowledging only when that leaves a degraded jury).
PAUSING_GATES = ("length_bias", "position_bias", "juror_agreement", "anchor_drift")
PAUSE_FOR_GATE = {"length_bias": "length_bias", "position_bias": "position_bias", "juror_agreement": "low_agreement",
                  "anchor_drift": "anchor_drift", "flip_rate": "jury_degraded"}
GATE_FOR_PAUSE = {v: k for k, v in PAUSE_FOR_GATE.items()}
MIN_JURY_FAMILIES = 2
ENGINE_2 = "Needs Engine 2 output (not built yet)."


def _now() -> datetime:
    return datetime.now(timezone.utc)


def thresholds() -> dict[str, Any]:
    s = get_settings()
    return {
        "length_r_warn": float(s.eval_gate_length_r_warn), "length_r_pause": float(s.eval_gate_length_r_pause),
        "flip_drop": float(s.eval_gate_flip_drop), "kappa_pause": float(s.eval_gate_kappa_pause),
        "anchor_changes_pause": int(s.eval_gate_anchor_changes_pause), "min_swaps": int(s.eval_gate_min_swaps),
        "min_doubles": int(s.eval_gate_min_doubles), "min_rated": int(s.eval_gate_min_rated),
        "min_swaps_pooled": int(s.eval_gate_min_swaps_pooled),
    }


def gate_coverage(matches: dict[str, int], rated: int, families: int) -> dict[str, Any]:
    """Before any spend: will the gates be able to fire? The expected swaps and doubles of the plan
    against the sample minimums (a gate below its minimum stays `insufficient_data`)."""
    th = thresholds()
    swaps, doubles = int(matches.get("swap", 0)), int(matches.get("double", 0))
    per_family = swaps / max(1, families)
    out = {
        "swaps_pooled": {"expected": swaps, "minimum": th["min_swaps_pooled"], "ok": swaps >= th["min_swaps_pooled"]},
        "swaps_per_family": {"expected": round(per_family, 1), "minimum": th["min_swaps"], "ok": per_family >= th["min_swaps"]},
        "doubles": {"expected": doubles, "minimum": th["min_doubles"], "ok": doubles >= th["min_doubles"]},
        "rated": {"expected": int(rated), "minimum": th["min_rated"], "ok": rated >= th["min_rated"]},
    }
    weak = [k for k, v in out.items() if not v["ok"]]
    out["all_ok"] = not weak
    out["message"] = ("every gate has the samples it needs" if not weak else
                      "too few samples for: " + ", ".join(weak) + " (those gates stay insufficient_data; raise the sample or lower the minimum)")
    return out


# ── pure pieces ─────────────────────────────────────────────────────────────────────────────────
def cohen_kappa(pairs: list[tuple[str, str]]) -> float | None:
    """Cohen's kappa of two raters' verdicts (a | b | tie, both in the canonical a/b frame) on the
    same items: (observed - expected agreement) / (1 - expected), the expected agreement taken from
    each rater's own category shares. None when it is undefined: no pairs, or both raters used one and
    the same single category (expected agreement 1)."""
    n = len(pairs)
    if n == 0:
        return None
    for x, y in pairs:
        if x not in CATEGORIES or y not in CATEGORIES:
            raise ValueError(f"unknown verdict in {(x, y)!r}")
    po = sum(1 for x, y in pairs if x == y) / n
    pe = sum((sum(1 for x, _ in pairs if x == c) / n) * (sum(1 for _, y in pairs if y == c) / n) for c in CATEGORIES)
    if pe >= 1.0 - 1e-12:
        return None
    return (po - pe) / (1.0 - pe)


def majority(verdicts: list[str]) -> str | None:
    """The verdict more than half of the jurors gave; `tie` when there is no such majority; None
    for no votes."""
    if not verdicts:
        return None
    for c in CATEGORIES:
        if verdicts.count(c) * 2 > len(verdicts):
            return c
    return "tie"


def _corr(items: list[tuple[float, float]]) -> tuple[float | None, float | None]:
    xs, ys = [x for x, _ in items], [y for _, y in items]
    return pearson(xs, ys), spearman(xs, ys)


def _length_block(rows: list[dict]) -> dict[str, Any]:
    """Correlation of rating with word count and with claim density, per pool and overall, over the
    members that have played at least one match (a member nobody judged yet sits at the default
    rating and would only dilute the correlation)."""
    played = [r for r in rows if r["matches"] > 0]

    def one(rs: list[dict]) -> dict[str, Any]:
        wc = [(float(r["word_count"]), r["rating"]) for r in rs if r["word_count"] is not None]
        cd = [(float(r["claim_density"]), r["rating"]) for r in rs if r["claim_density"] is not None]
        p, s = _corr(wc)
        pd, sd = _corr(cd)
        return {"n": len(wc), "pearson": p, "spearman": s, "pearson_density": pd, "spearman_density": sd}

    keys = sorted({r["pool_key"] for r in played})
    return {"overall": one(played), "pools": [{"pool_key": k, **one([r for r in played if r["pool_key"] == k])} for k in keys]}


def _flip_block(matches: list[dict], dropped: list[str]) -> list[dict]:
    by = {m["id"]: m for m in matches}
    stats: dict[str, list[int]] = {}
    for m in matches:
        fam = m["family"] or "?"
        stats.setdefault(fam, [0, 0])
        if m["kind"] == "swap" and m["status"] == "done" and m["verdict"]:
            o = by.get(m["swap_of"])
            if o is not None and o["status"] == "done" and o["verdict"]:
                stats[fam][0] += 1
                stats[fam][1] += 1 if o["verdict"] != m["verdict"] else 0
    for f in dropped:
        stats.setdefault(f, [0, 0])
    return [
        {"family": f, "swaps": sw, "flips": fl, "rate": (fl / sw if sw else None), "dropped": f in dropped}
        for f, (sw, fl) in sorted(stats.items())
    ]


def _pooled_kappa(matches: list[dict]) -> dict[str, Any]:
    """Kappa over every double-judged match: the original juror's verdict against the other family's."""
    by = {m["id"]: m for m in matches}
    pairs = []
    for m in matches:
        if m["kind"] != "double" or m["status"] != "done" or not m["verdict"]:
            continue
        o = by.get(m["double_of"])
        if o is not None and o["status"] == "done" and o["verdict"] and (o["family"] or "?") != (m["family"] or "?"):
            pairs.append((o["verdict"], m["verdict"]))
    return {"n": len(pairs), "kappa": cohen_kappa(pairs),
            "raw_agreement": (sum(1 for x, y in pairs if x == y) / len(pairs)) if pairs else None}


def _agreement_block(matches: list[dict]) -> list[dict]:
    by = {m["id"]: m for m in matches}
    groups: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for m in matches:
        if m["kind"] != "double" or m["status"] != "done" or not m["verdict"]:
            continue
        o = by.get(m["double_of"])
        if o is None or o["status"] != "done" or not o["verdict"]:
            continue
        fo, fd = o["family"] or "?", m["family"] or "?"
        if fo == fd:
            continue
        if fo <= fd:
            groups.setdefault((fo, fd), []).append((o["verdict"], m["verdict"]))
        else:
            groups.setdefault((fd, fo), []).append((m["verdict"], o["verdict"]))
    return [
        {"family_a": a, "family_b": b, "n": len(v), "kappa": cohen_kappa(v),
         "raw_agreement": sum(1 for x, y in v if x == y) / len(v)}
        for (a, b), v in sorted(groups.items())
    ]


def anchor_view(set_id: str, pairs: list[dict], matches: list[dict]) -> dict[str, Any]:
    """This pass's re-judgement of an anchor set: the majority verdict of the jurors that judged each
    pair against its baseline. `matches` are the done anchor matches (anchor_index, verdict)."""
    votes: dict[int, list[str]] = {}
    for m in matches:
        if m["status"] == "done" and m["verdict"]:
            votes.setdefault(int(m["anchor_index"]), []).append(m["verdict"])
    items = []
    for p in sorted(pairs, key=lambda p: p["index"]):
        if p.get("baseline") is None:
            continue
        cur = majority(votes.get(int(p["index"]), []))
        items.append({"index": int(p["index"]), "baseline": p["baseline"], "current": cur,
                      "changed": None if cur is None else cur != p["baseline"]})
    judged = [i for i in items if i["current"] is not None]
    return {"anchor_set_id": set_id, "n": len(items), "judged": len(judged),
            "changed": sum(1 for i in judged if i["changed"]), "items": items}


def build_gates(
    length: dict, flips: list[dict], agreement: list[dict], anchors: dict | None, th: dict[str, Any],
    acknowledged: dict[str, dict] | None = None, pooled: dict[str, Any] | None = None,
) -> list[dict]:
    acknowledged = acknowledged or {}
    pooled = pooled or {"n": 0, "kappa": None}
    gates: dict[str, dict] = {}

    ov = length["overall"]
    r = ov["spearman"]
    if ov["n"] < th["min_rated"] or r is None:
        gates["length_bias"] = {"state": "insufficient_data", "value": r, "n": ov["n"], "detail":
                                f"{ov['n']} rated module(s) with a play so far; the correlation needs {th['min_rated']}."}
    else:
        state = "tripped" if abs(r) > th["length_r_pause"] else ("warn" if abs(r) > th["length_r_warn"] else "ok")
        gates["length_bias"] = {"state": state, "value": r, "n": ov["n"], "detail":
                                f"Spearman r between rating and word count {r:+.2f} over {ov['n']} modules (warn above "
                                f"{th['length_r_warn']}, pause above {th['length_r_pause']})."}

    eligible = [f for f in flips if f["swaps"] >= th["min_swaps"] and f["rate"] is not None]
    over = [f for f in eligible if f["rate"] > th["flip_drop"]]
    dropped = [f["family"] for f in flips if f["dropped"]]
    if not eligible and not dropped:
        gates["flip_rate"] = {"state": "insufficient_data", "value": None, "n": sum(f["swaps"] for f in flips), "detail":
                              f"No juror family has {th['min_swaps']} swapped re-judgements yet."}
    else:
        top = max(eligible, key=lambda f: f["rate"]) if eligible else None
        bad = sorted({f["family"] for f in over} | set(dropped))
        gates["flip_rate"] = {
            "state": "tripped" if bad else "ok", "value": top["rate"] if top else None,
            "n": sum(f["swaps"] for f in eligible),
            "detail": (f"Dropped from the jury: {', '.join(bad)} (flip rate above {th['flip_drop']})." if bad else
                       f"Highest flip rate {top['rate']:.2f} (a family above {th['flip_drop']} is dropped)."),
        }

    live = [f for f in flips if not f["dropped"]]
    psw, pfl = sum(f["swaps"] for f in live), sum(f["flips"] for f in live)
    if psw < th["min_swaps_pooled"]:
        gates["position_bias"] = {"state": "insufficient_data", "value": None, "n": psw, "detail":
                                  f"{psw} swapped re-judgement(s) so far; the pooled flip rate needs {th['min_swaps_pooled']}."}
    else:
        rate = pfl / psw
        gates["position_bias"] = {
            "state": "tripped" if rate > th["flip_drop"] else "ok", "value": rate, "n": psw,
            "detail": f"{pfl} of {psw} swapped re-judgements flipped ({rate:.2f}) over the families still on the jury; "
                      f"above {th['flip_drop']} the order of the texts moves the verdict."}

    if pooled["n"] < th["min_doubles"] or pooled["kappa"] is None:
        gates["juror_agreement"] = {"state": "insufficient_data", "value": pooled["kappa"], "n": pooled["n"], "detail":
                                    f"{pooled['n']} double-judged match(es) with a defined kappa so far; the gate needs {th['min_doubles']}."}
    else:
        gates["juror_agreement"] = {
            "state": "tripped" if pooled["kappa"] < th["kappa_pause"] else "ok", "value": pooled["kappa"], "n": pooled["n"],
            "detail": f"Pooled kappa {pooled['kappa']:.2f} over {pooled['n']} double-judged matches; "
                      f"below {th['kappa_pause']} the module prompt is too vague."}

    if anchors is None or not anchors["judged"]:
        gates["anchor_drift"] = {"state": "insufficient_data", "value": None, "n": 0, "detail":
                                 "No anchor set was re-judged in this pass." if anchors is None else
                                 f"Anchor set {anchors['anchor_set_id']} has not been re-judged yet."}
    else:
        ch = anchors["changed"]
        gates["anchor_drift"] = {
            "state": "tripped" if ch > th["anchor_changes_pause"] else "ok", "value": float(ch), "n": anchors["judged"],
            "detail": f"{ch} of {anchors['judged']} anchor verdicts changed (pause above {th['anchor_changes_pause']})."}

    for key in ("parts_vs_whole", "score_clumping"):
        gates[key] = {"state": "not_applicable", "value": None, "n": 0, "detail": ENGINE_2}

    out = []
    for key in GATE_KEYS:
        ack = acknowledged.get(key) or {}
        out.append({"key": key, **gates[key], "acknowledged_by": ack.get("by"), "acknowledged_at": ack.get("at")})
    return out


def finalize_gates(gates: list[dict]) -> list[dict]:
    """A finished tournament: a gate that never got the samples it needs is shown as `warn`, not as
    a quiet `insufficient_data`, so nobody reads silence as a pass."""
    out = []
    for g in gates:
        if g["state"] == "insufficient_data":
            g = {**g, "state": "warn", "detail": f"not evaluated: only {g['n']} samples. {g['detail']}"}
        out.append(g)
    return out


def compute_from_inputs(inputs: dict[str, Any]) -> dict[str, Any]:
    """The whole diagnostics block from loaded inputs. Pure and CPU-bound (it fits the provisional
    ratings): run it in a thread."""
    from tret.evaluation.tournament import _fit_inputs  # lazy: tournament imports this module

    th = inputs.get("thresholds") or thresholds()
    rows, _summary = _fit_inputs(inputs["members"], inputs["matches"], _now().isoformat())
    length = _length_block(rows)
    flips = _flip_block(inputs["matches"], inputs["dropped"])
    agreement = _agreement_block(inputs["matches"])
    anchors = None
    if inputs.get("anchor_pairs") is not None:
        anchors = anchor_view(inputs["anchor_set_id"], inputs["anchor_pairs"], inputs["anchor_matches"])
    pooled = _pooled_kappa(inputs["matches"])
    return {
        "thresholds": th, "length_bias": length, "flip_rates": flips, "agreement": agreement, "anchors": anchors,
        "pooled_kappa": pooled,
        "gates": build_gates(length, flips, agreement, anchors, th, inputs.get("acknowledged"), pooled),
    }


# ── loading ─────────────────────────────────────────────────────────────────────────────────────
async def load_inputs(db: AsyncSession, tid: uuid.UUID) -> dict[str, Any]:
    """Everything the computation reads, as plain data (the caller closes its session before the
    thread runs)."""
    t = await db.get(EvalTournament, tid)
    members = [
        {"id": str(m.id), "pool_key": m.pool_key, "pool_level": m.pool_level, "word_count": m.word_count,
         "claim_density": m.claim_density}
        for m in await pl.members_of(db, t.snapshot_id)
    ]
    matches, anchor_matches = [], []
    for m in (await db.execute(select(EvalMatch).where(EvalMatch.tournament_id == tid))).scalars():
        if m.kind == "anchor":
            anchor_matches.append({"anchor_index": m.anchor_index, "status": m.status, "verdict": m.verdict})
            continue
        matches.append({
            "id": m.id, "pool_key": m.pool_key, "a": str(m.a_member_id), "b": str(m.b_member_id), "kind": m.kind,
            "status": m.status, "verdict": m.verdict, "swap_of": m.swap_of, "double_of": m.double_of, "family": m.juror_family,
        })
    stored = dict(t.diagnostics or {})
    out: dict[str, Any] = {
        "tournament_id": str(t.id), "status": t.status, "members": members, "matches": matches, "anchor_matches": anchor_matches,
        "dropped": list(t.dropped_families or []), "acknowledged": dict(t.acknowledged_gates or {}),
        "history": list(stored.get("history") or []), "thresholds": thresholds(),
        "anchor_set_id": stored.get("anchor_set_id"), "anchor_pairs": None, "notes": list(stored.get("notes") or []),
    }
    if stored.get("anchor_set_id"):
        aset = await db.get(EvalAnchorSet, uuid.UUID(stored["anchor_set_id"]))
        if aset is not None:
            out["anchor_pairs"] = list(aset.pairs or [])
    return out


def history_entry(round_: int, block: dict[str, Any]) -> dict[str, Any]:
    flips = [f["rate"] for f in block["flip_rates"] if f["swaps"] >= block["thresholds"]["min_swaps"] and f["rate"] is not None]
    kappas = [a["kappa"] for a in block["agreement"] if a["n"] >= block["thresholds"]["min_doubles"] and a["kappa"] is not None]
    return {"round": round_, "length_spearman": block["length_bias"]["overall"]["spearman"],
            "max_flip": max(flips) if flips else None, "min_kappa": min(kappas) if kappas else None,
            "rated": block["length_bias"]["overall"]["n"]}


def merge_history(history: list[dict], entry: dict) -> list[dict]:
    """The per-round history with this round's entry (a replayed round replaces its own)."""
    return sorted([h for h in history if h.get("round") != entry["round"]] + [entry], key=lambda h: h["round"])


BLOCK_KEYS = ("thresholds", "gates", "length_bias", "flip_rates", "agreement", "anchors")


def stored_block(block: dict[str, Any], history: list[dict], anchor_set_id: str | None, extra: dict | None = None) -> dict[str, Any]:
    """What the runner keeps in `eval_tournaments.diagnostics` after a round: the gates, the history,
    the anchor set used and the whole computed block (`block`), which `GET .../diagnostics` serves while
    the tournament has not changed. `extra` carries the runner's notes and the jury-restored mark."""
    return {"computed_at": _now().isoformat(), "gates": block["gates"], "history": history,
            "anchor_set_id": anchor_set_id, "anchors": block["anchors"],
            "block": {k: block[k] for k in BLOCK_KEYS}, **(extra or {})}


def _response(t_id: str, status: str, computed_at: str, block: dict, history: list[dict], notes: list[str]) -> dict[str, Any]:
    gates = finalize_gates(block["gates"]) if status == "completed" else block["gates"]
    return {
        "tournament_id": t_id, "status": status, "computed_at": computed_at, "thresholds": block["thresholds"],
        "gates": gates, "length_bias": block["length_bias"], "flip_rates": block["flip_rates"],
        "agreement": block["agreement"], "anchors": block["anchors"], "history": history, "notes": notes,
    }


async def _compute(db: AsyncSession, tournament_id: uuid.UUID) -> tuple[dict, dict]:
    inputs = await load_inputs(db, tournament_id)
    await db.commit()  # no transaction stays open while the fit runs
    return inputs, await asyncio.to_thread(compute_from_inputs, inputs)


async def compute_diagnostics(db: AsyncSession, tournament_id: uuid.UUID) -> dict[str, Any]:
    """The diagnostics of a tournament as of now, computed from the stored matches (any status; a
    draft reports `insufficient_data`). Ratings are provisional fits and are not stored."""
    inputs, block = await _compute(db, tournament_id)
    return _response(inputs["tournament_id"], inputs["status"], _now().isoformat(), block, inputs["history"], inputs["notes"])


def _overlay(block: dict, t: EvalTournament) -> dict:
    """The stored block with what changed since it was computed: acknowledgements and dropped families."""
    ack, dropped = dict(t.acknowledged_gates or {}), set(t.dropped_families or [])
    gates = [{**g, "acknowledged_by": (ack.get(g["key"]) or {}).get("by"), "acknowledged_at": (ack.get(g["key"]) or {}).get("at")}
             for g in block["gates"]]
    return {**block, "gates": gates, "flip_rates": [{**f, "dropped": f["family"] in dropped} for f in block["flip_rates"]]}


async def serve_diagnostics(db: AsyncSession, t: EvalTournament) -> dict[str, Any]:
    """What `GET .../diagnostics` answers. A tournament that is running or paused serves the block its last
    round stored (nothing changes between rounds, so it is not recomputed per request); a draft, or one
    with no block yet (before its first round), is computed live; a completed one is computed once and
    cached on the row (`diagnostics.final`)."""
    stored = dict(t.diagnostics or {})
    block = stored.get("block")
    if block and (t.status in ("running", "paused") or (t.status == "completed" and stored.get("final"))):
        return _response(str(t.id), t.status, stored.get("computed_at") or _now().isoformat(), _overlay(block, t),
                         list(stored.get("history") or []), list(stored.get("notes") or []))
    inputs, fresh = await _compute(db, t.id)
    now = _now().isoformat()
    if t.status == "completed":
        new = {**stored, "final": True, "computed_at": now, "gates": fresh["gates"], "anchors": fresh["anchors"],
               "block": {k: fresh[k] for k in BLOCK_KEYS}}
        await db.execute(update(EvalTournament).where(EvalTournament.id == t.id, EvalTournament.status == "completed").values(diagnostics=new))
        await db.commit()
    return _response(inputs["tournament_id"], inputs["status"], now, fresh, inputs["history"], inputs["notes"])
