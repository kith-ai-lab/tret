"""Divergence and human spot checks (disclosure evaluation engine, PR 9): Engine 1's parts against Engine 2's whole.

Engine 1 rates report MODULES against peers; Engine 2 scores each WHOLE report. The two are blind to each other
by design (tests/test_eval_credibility_firewall.py lints it). This module is the one place that reads both
result tables, and it is plain code over finished results plus human answers: it makes no model call (no
provider, router or engine import; linted) and everything it does is deterministic, seeded from the set id.

A `DivergenceSet` joins ONE completed credibility pass with 1-4 completed tournaments (one per module), per
report: the mean module decile (the parts), the Credibility Score (the whole), both as mid-rank percentiles
over the reports rated in every selected module, and the delta between them. The join is frozen in
`computed` (both inputs are completed, so they never change, and the spot-check sample has to stay stable).

Two gates come out of it. `parts_vs_whole`: the Pearson r of parts against whole; outside the configured
range (0.2-0.8) the extremes are to be read before going on (the briefing's target is 0.4-0.6).
`spot_check_agreement`: a human answers blind triads (rank three module extracts; Engine 1's order is the
truth) and critiques (judge a whole-report review); agreement under 70% means no client use.

Spot checks are rows of their own table, not `approvals`: an approval needs a finding and a run, and an
Engine 2 review has neither. They keep the approval's shape (decision, `decided_by` stamped from the
session, note, purpose `spot_check`).

Blindness (enforced in the output builders, tested): while a triad is pending nothing but the letters and
the masked module extracts leaves this module; the critique shows the review on its own, without any module
band, until it is answered.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from tret.config import get_settings
from tret.db.models import (
    CredibilityPass,
    CredibilityReview,
    DisclosureReport,
    DivergenceSet,
    EvalTournament,
    ModuleRating,
    PeerPoolMember,
    PeerPoolSnapshot,
    SpotCheck,
)
from tret.evaluation.juror import mask_names
from tret.evaluation.ratings import _average_ranks, pearson, spearman
from tret.storage import get_store

log = logging.getLogger("tret.divergence")

GATE_PARTS = "parts_vs_whole"
GATE_SPOT = "spot_check_agreement"
GATES = (GATE_PARTS, GATE_SPOT)
QUADRANTS = ("both_strong", "parts_over_whole", "whole_over_parts", "both_weak")
MAX_TOURNAMENTS = 4
LETTERS = ("A", "B", "C")
VERDICT_AGREEMENT = {"agree": 1.0, "partly": 0.5, "disagree": 0.0}
TARGET_LOW, TARGET_HIGH = 0.4, 0.6  # the briefing's target band for r
TEXT_UNAVAILABLE = "text unavailable"


class DivergenceError(Exception):
    """A request the divergence step cannot serve; `status` is the HTTP status the API answers with."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(d: datetime | None) -> str | None:
    return d.isoformat() if d else None


def divergence_enabled() -> bool:
    return bool(getattr(get_settings(), "eval_divergence", False))


def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


# ── the join (pure after the reads) ─────────────────────────────────────────────────────────────────
def decile_eq(band: int, band_kind: str | None) -> float:
    """A band on the 1-10 scale: deciles as they are, quintiles at the middle of the two deciles they cover."""
    return 2 * band - 0.5 if band_kind == "quintile" else float(band)


def _percentiles(vals: list[float]) -> list[float]:
    """Mid-rank percentiles `(average rank - 0.5) / n`; ties share their average rank."""
    n = len(vals)
    return [(r - 0.5) / n for r in _average_ranks(list(vals))]


def _quadrant(parts_pct: float, whole_pct: float) -> str:
    high_parts, high_whole = parts_pct >= 0.5, whole_pct >= 0.5
    if high_parts and high_whole:
        return "both_strong"
    if high_parts:
        return "parts_over_whole"  # the briefing's "8/10 modules but hides a major exposure" case
    return "whole_over_parts" if high_whole else "both_weak"


def _corr(rows: list[dict]) -> dict[str, Any]:
    xs, ys = [r["parts"] for r in rows], [r["whole"] for r in rows]
    return {"n": len(rows), "pearson": pearson(xs, ys), "spearman": spearman(xs, ys)}


def _reading_key(r: dict) -> tuple:
    return (-abs(r["delta"]), -abs(r["raw_delta"]), r["disclosure_report_id"])


async def _load_inputs(db: AsyncSession, *, pass_id: uuid.UUID, tournament_ids: list, arm_key: str | None,
                       workspace_id: uuid.UUID | None) -> tuple[CredibilityPass, list[EvalTournament], list[PeerPoolSnapshot]]:
    p = await db.get(CredibilityPass, pass_id)
    if p is None or (workspace_id is not None and p.workspace_id != workspace_id):
        raise DivergenceError("Credibility pass not found", 404)
    try:
        tids = [t if isinstance(t, uuid.UUID) else uuid.UUID(str(t)) for t in tournament_ids]
    except ValueError:
        raise DivergenceError("tournament_ids must be tournament ids") from None
    tournaments, snaps = [], []
    for tid in tids:
        t = await db.get(EvalTournament, tid)
        snap = await db.get(PeerPoolSnapshot, t.snapshot_id) if t is not None else None
        if t is None or t.workspace_id != p.workspace_id or snap is None or p.project_id is None or snap.project_id != p.project_id:
            raise DivergenceError("Tournament not found in the pass's workspace and project", 404)
        tournaments.append(t)
        snaps.append(snap)
    if not 1 <= len(tids) <= MAX_TOURNAMENTS:
        raise DivergenceError(f"Select 1 to {MAX_TOURNAMENTS} tournaments, one per module")
    modules = [t.module for t in tournaments]
    if len(set(modules)) != len(modules):
        raise DivergenceError("At most one tournament per module")
    if arm_key is not None and arm_key not in {a.get("key") for a in (p.arms or [])}:
        raise DivergenceError(f"Unknown arm {arm_key!r}: this pass has {', '.join(a.get('key') for a in (p.arms or []))}")
    if p.status != "completed":
        raise DivergenceError(f"The credibility pass is {p.status}; only a completed one can be joined", 409)
    for t in tournaments:
        if t.status != "completed":
            raise DivergenceError(f"Tournament {t.id} ({t.module}) is {t.status}; only a completed one can be joined", 409)
    return p, tournaments, snaps


def _fingerprint(items: list) -> str:
    return hashlib.sha256(json.dumps(sorted(items), separators=(",", ":")).encode()).hexdigest()


async def tournament_fingerprint(db: AsyncSession, tournament_id: uuid.UUID | str) -> str:
    """sha256 over the tournament's ratings, (member id, rating to 6 dp, band, band kind) sorted: a refit that
    rewrites them changes it."""
    found = (await db.execute(select(ModuleRating.member_id, ModuleRating.rating, ModuleRating.band, ModuleRating.band_kind)
                              .where(ModuleRating.tournament_id == uuid.UUID(str(tournament_id))))).all()
    return _fingerprint([[str(m), round(float(r), 6), b, k] for m, r, b, k in found])


async def pass_fingerprint(db: AsyncSession, pass_id: uuid.UUID | str, arm_key: str | None = None) -> str:
    """sha256 over the pass's done reviews, (review id, credibility score) sorted; only the selected arm's when `arm_key` is set."""
    q = select(CredibilityReview.id, CredibilityReview.credibility_score).where(
        CredibilityReview.pass_id == uuid.UUID(str(pass_id)), CredibilityReview.status == "done", CredibilityReview.credibility_score.is_not(None))
    if arm_key is not None:
        q = q.where(CredibilityReview.arm_key == arm_key)
    found = (await db.execute(q)).all()
    return _fingerprint([[str(i), sc] for i, sc in found])


async def stale_info(db: AsyncSession, s: DivergenceSet) -> dict[str, Any]:
    """Whether the inputs the set froze have changed since (a refit of a tournament, a pass whose reviews or
    scores differ). The frozen rows stay as they were; the set just says so."""
    inp = (s.computed or {}).get("inputs") or {}
    bad = []
    for t in inp.get("tournaments") or []:
        fp = t.get("fingerprint")
        if fp and await tournament_fingerprint(db, t["id"]) != fp:
            bad.append(t["id"])
    stale_pass = bool(inp.get("pass_fingerprint")) and await pass_fingerprint(db, s.credibility_pass_id, s.arm_key) != inp["pass_fingerprint"]
    return {"stale": bool(bad) or stale_pass, "stale_tournaments": bad, "stale_pass": stale_pass}


async def build_join(db: AsyncSession, *, pass_id: uuid.UUID, tournament_ids: list, arm_key: str | None = None,
                     workspace_id: uuid.UUID | None = None) -> dict[str, Any]:
    """The frozen result of joining the pass's whole scores with the tournaments' module ratings, per report
    (`rows`), the statistics over the complete rows (`stats`), the reading list of the biggest divergences
    (`reading_list`, report ids) and what it was built from (`inputs`). Reads only; deterministic. With
    `workspace_id` the pass must belong to it (404 otherwise)."""
    s = get_settings()
    p, tournaments, snaps = await _load_inputs(db, pass_id=pass_id, tournament_ids=tournament_ids, arm_key=arm_key, workspace_id=workspace_id)
    modules = [t.module for t in tournaments]
    all_scored = set((await db.execute(
        select(CredibilityReview.disclosure_report_id).where(
            CredibilityReview.pass_id == p.id, CredibilityReview.status == "done", CredibilityReview.credibility_score.is_not(None)))).scalars())
    q = select(CredibilityReview).where(
        CredibilityReview.pass_id == p.id, CredibilityReview.status == "done", CredibilityReview.credibility_score.is_not(None))
    if arm_key is not None:
        q = q.where(CredibilityReview.arm_key == arm_key)
    wholes = {r.disclosure_report_id: r for r in (await db.execute(q.order_by(CredibilityReview.disclosure_report_id))).scalars()}

    parts: dict[uuid.UUID, dict[str, dict]] = {}
    company: dict[uuid.UUID, str] = {}
    for t in tournaments:
        found = (await db.execute(
            select(ModuleRating, PeerPoolMember).join(PeerPoolMember, PeerPoolMember.id == ModuleRating.member_id)
            .where(ModuleRating.tournament_id == t.id).order_by(ModuleRating.member_id))).all()
        for rating, member in found:
            rid = member.disclosure_report_id
            if rid is None or rating.band is None or t.module in parts.get(rid, {}):
                continue
            parts.setdefault(rid, {})[t.module] = {
                "band": rating.band, "band_kind": rating.band_kind, "decile_eq": decile_eq(rating.band, rating.band_kind),
                "rating": rating.rating, "lo90": rating.lo90, "hi90": rating.hi90, "pool_key": rating.pool_key, "member_id": str(member.id)}
            company.setdefault(rid, member.company or "")

    rows: list[dict] = []
    for rid in sorted(set(wholes) & set(parts), key=str):
        r, mods = wholes[rid], parts[rid]
        mean_parts = _mean([m["decile_eq"] for m in mods.values()])
        rows.append({
            "disclosure_report_id": str(rid), "company": company.get(rid, ""),
            "pool_keys": {m: v["pool_key"] for m, v in mods.items()}, "modules": mods,
            "decile_eq": mean_parts, "parts": mean_parts, "whole": r.credibility_score, "arm_key": r.arm_key, "review_id": str(r.id),
            "complete": all(m in mods for m in modules), "parts_pct": None, "whole_pct": None, "delta": None,
            "raw_delta": r.credibility_score - mean_parts, "quadrant": None})
    complete = [r for r in rows if r["complete"]]
    if complete:
        for r, pp, wp in zip(complete, _percentiles([r["parts"] for r in complete]), _percentiles([r["whole"] for r in complete])):
            r.update(parts_pct=pp, whole_pct=wp, delta=wp - pp, quadrant=_quadrant(pp, wp))
    complete.sort(key=_reading_key)
    partial = sorted((r for r in rows if not r["complete"]), key=lambda r: r["disclosure_report_id"])
    rows = complete + partial
    reading_list = [r["disclosure_report_id"] for r in complete[: int(s.eval_divergence_reading_list)]]

    arms = [{"key": a.get("key"), "label": a.get("label") or a.get("key")} for a in (p.arms or [])]
    in_rows = {r["arm_key"] for r in complete}
    stats: dict[str, Any] = {
        "n_joined": len(rows), "n_complete": len(complete), "n_partial": len(partial),
        "n_whole_only": len(set(wholes) - set(parts)), "n_parts_only": len(set(parts) - all_scored),
        "n_other_arm": len((set(parts) & all_scored) - set(wholes)),
        **{k: v for k, v in _corr(complete).items() if k != "n"},
        "quadrants": {q_: sum(1 for r in complete if r["quadrant"] == q_) for q_ in QUADRANTS},
        "mixed_arms": arm_key is None and len(in_rows) > 1,
        "by_arm": ([{"arm_key": a["key"], "label": a["label"], **_corr([r for r in complete if r["arm_key"] == a["key"]])} for a in arms]
                   if arm_key is None and len(arms) > 1 else []),
    }
    inputs = {
        "pass_id": str(p.id), "pass_name": p.name, "arms": arms, "arm_key": arm_key,
        "tournaments": [{"id": str(t.id), "module": t.module, "snapshot_id": str(t.snapshot_id),
                         "completed_at": (t.summary or {}).get("completed_at") or _iso(t.updated_at),
                         "fingerprint": await tournament_fingerprint(db, t.id)} for t in tournaments],
        "pass_fingerprint": await pass_fingerprint(db, p.id, arm_key)}
    return {"rows": rows, "stats": stats, "reading_list": reading_list, "inputs": inputs}


def parts_vs_whole_gate(computed: dict, settings: Any = None) -> dict[str, Any]:
    """The parts-vs-whole gate: r between the mean module decile and the Credibility Score over the complete
    reports. `insufficient_data` below `eval_divergence_min_n`; `tripped` outside [r_low, r_high] (read the
    extremes before going on); `warn` inside it but outside the briefing's 0.4-0.6 target; else `ok`."""
    st = settings or get_settings()
    stats = computed.get("stats") or {}
    n, r = int(stats.get("n_complete") or 0), stats.get("pearson")
    lo, hi, min_n = float(st.eval_divergence_r_low), float(st.eval_divergence_r_high), int(st.eval_divergence_min_n)
    out = {"key": GATE_PARTS, "value": r, "n": n, "threshold": {"low": lo, "high": hi, "target_low": TARGET_LOW, "target_high": TARGET_HIGH, "min_n": min_n}}
    if n < min_n or r is None:
        why = f"{n} complete report(s); r needs {min_n}" if n < min_n else "r is undefined (a side has no variance)"
        return {**out, "state": "insufficient_data", "detail": f"{why}."}
    if r < lo or r > hi:
        return {**out, "state": "tripped", "detail": f"r = {r:.2f} over {n} reports is outside {lo:.1f}-{hi:.1f}: read the extremes before going on (briefing target {TARGET_LOW}–{TARGET_HIGH})."}
    if r < TARGET_LOW or r > TARGET_HIGH:
        return {**out, "state": "warn", "detail": f"r = {r:.2f} over {n} reports is inside {lo:.1f}-{hi:.1f} but outside the briefing target {TARGET_LOW}–{TARGET_HIGH}."}
    return {**out, "state": "ok", "detail": f"r = {r:.2f} over {n} reports is inside the briefing target {TARGET_LOW}–{TARGET_HIGH}."}


# ── spot-check sampling ─────────────────────────────────────────────────────────────────────────────
async def _pools(db: AsyncSession, tournament_ids: list[str]) -> dict[str, list[tuple[Any, Any]]]:
    """tournament id -> {pool_key: [(rating, member)]} for every rated member."""
    out: dict[str, dict[str, list]] = {}
    for tid in tournament_ids:
        found = (await db.execute(
            select(ModuleRating, PeerPoolMember).join(PeerPoolMember, PeerPoolMember.id == ModuleRating.member_id)
            .where(ModuleRating.tournament_id == uuid.UUID(str(tid)), ModuleRating.band.is_not(None)).order_by(ModuleRating.member_id))).all()
        pools: dict[str, list] = {}
        for rating, member in found:
            pools.setdefault(rating.pool_key, []).append((rating, member))
        out[str(tid)] = pools
    return out


def _draw_triad(rng: random.Random, members: list[tuple[Any, Any]], joined: set[str]) -> list[tuple[Any, Any]] | None:
    """One member from the bottom, middle and top third of the pool's ratings (members whose report is in the
    join preferred), with three strictly distinct ratings; None when this draw cannot get them (ties)."""
    ordered = sorted(members, key=lambda x: (x[0].rating, str(x[1].id)))
    k = len(ordered) // 3
    bottom, middle, top = ordered[:k], ordered[k: len(ordered) - k], ordered[len(ordered) - k:]

    def pick(third: list, below: float | None) -> tuple[Any, Any] | None:
        ok = [x for x in third if below is None or x[0].rating < below]
        preferred = [x for x in ok if x[1].disclosure_report_id is not None and str(x[1].disclosure_report_id) in joined]
        return rng.choice(preferred or ok) if ok else None

    hi = pick(top, None)
    mid = pick(middle, hi[0].rating) if hi else None
    lo = pick(bottom, mid[0].rating) if mid else None
    return [lo, mid, hi] if lo and mid and hi else None


def _triad_item(rng: random.Random, module: str, tournament_id: str, pool_key: str, picks: list[tuple[Any, Any]]) -> dict:
    picks = list(picks)
    rng.shuffle(picks)
    members = [{"letter": LETTERS[i], "member_id": str(m.id), "disclosure_report_id": str(m.disclosure_report_id) if m.disclosure_report_id else None,
                "rating": r.rating, "band": r.band, "band_kind": r.band_kind} for i, (r, m) in enumerate(picks)]
    expected = sorted(members, key=lambda x: (-x["rating"], x["member_id"]))
    return {"module": module, "tournament_id": tournament_id, "pool_key": pool_key, "members": members,
            "expected_order": [x["letter"] for x in expected]}


async def sample_spot_checks(db: AsyncSession, dset: DivergenceSet, computed: dict, settings: Any = None) -> list[SpotCheck]:
    """The set's spot checks, drawn once at creation (seeded from the set id; same set, same sample).

    Triads: `eval_spot_check_triads`, spread over the selected modules round-robin; a pool with 3+ rated
    members (weighted by size, pools not yet used first); one member from each third of its ratings. Fewer
    when no pool has 3 members left (recorded in `computed["stats"]["spot_check_shortfall"]`). Critiques:
    `eval_spot_check_critiques`, half (rounded up) from the top of the reading list, the rest at random
    from the other complete rows (any joined rows when there are too few complete ones)."""
    st = settings or get_settings()
    rng = random.Random(int(dset.id))
    rows = computed.get("rows") or []
    joined = {r["disclosure_report_id"] for r in rows}
    modules = list(dset.modules or [])
    tids = [str(t) for t in (dset.tournament_ids or [])]
    pools = await _pools(db, tids)
    elig = {m: sorted(k for k, v in pools[t].items() if len(v) >= 3) for m, t in zip(modules, tids)}
    tid_of = dict(zip(modules, tids))

    checks: list[SpotCheck] = []
    used_pools: set[tuple[str, str]] = set()
    exhausted: set[tuple[str, str]] = set()
    seen: set[frozenset] = set()
    want_triads = int(st.eval_spot_check_triads)
    for i in range(want_triads):
        if not modules:
            break
        placed = False
        for off in range(len(modules)):
            m = modules[(i + off) % len(modules)]
            while not placed:
                cand = [k for k in elig[m] if (m, k) not in exhausted]
                if not cand:
                    break
                pick_from = [k for k in cand if (m, k) not in used_pools] or cand
                key = rng.choices(pick_from, weights=[len(pools[tid_of[m]][k]) for k in pick_from])[0]
                for _ in range(12):
                    picks = _draw_triad(rng, pools[tid_of[m]][key], joined)
                    if picks is None:
                        continue
                    ident = frozenset(x[1].id for x in picks)
                    if ident not in seen:
                        seen.add(ident)
                        used_pools.add((m, key))
                        checks.append(SpotCheck(id=uuid.uuid4(), set_id=dset.id, kind="triad", position=len(checks), purpose="spot_check",
                                                item=_triad_item(rng, m, tid_of[m], key, picks), status="pending"))
                        placed = True
                        break
                else:
                    exhausted.add((m, key))
            if placed:
                break
    n_triads = len(checks)

    complete = [r for r in rows if r.get("complete")]
    want_crit = int(st.eval_spot_check_critiques)
    candidates = complete if len(complete) >= want_crit else complete + [r for r in rows if not r.get("complete")]
    n = min(want_crit, len(candidates))
    reading = list(computed.get("reading_list") or [])
    by_id = {r["disclosure_report_id"]: r for r in candidates}
    top = [by_id[i] for i in reading if i in by_id][: (n + 1) // 2]
    chosen = {r["disclosure_report_id"] for r in top}
    others = sorted((r for r in candidates if r["disclosure_report_id"] not in chosen and r["disclosure_report_id"] not in reading),
                    key=lambda r: r["disclosure_report_id"])
    rest = rng.sample(others, min(len(others), n - len(top)))
    chosen |= {r["disclosure_report_id"] for r in rest}
    if len(top) + len(rest) < n:  # too few outside the reading list: top up from the rest of it
        left = sorted((r for r in candidates if r["disclosure_report_id"] not in chosen), key=lambda r: r["disclosure_report_id"])
        rest += rng.sample(left, min(len(left), n - len(top) - len(rest)))
    picked = top + rest
    rng.shuffle(picked)
    reading_set = set(reading)
    for pos, r in enumerate(picked):
        checks.append(SpotCheck(
            id=uuid.uuid4(), set_id=dset.id, kind="critique", position=pos, purpose="spot_check", status="pending",
            item={"review_id": r["review_id"], "disclosure_report_id": r["disclosure_report_id"], "company": r["company"],
                  "from_reading_list": r["disclosure_report_id"] in reading_set, "whole": r["whole"], "parts": r["parts"], "quadrant": r["quadrant"]}))
    stats = computed.setdefault("stats", {})
    stats["spot_check_shortfall"] = {"triads": want_triads - n_triads, "critiques": want_crit - len(picked)}
    return checks


# ── answers and the agreement gate ──────────────────────────────────────────────────────────────────
def triad_agreement(expected_order: list[str], order: list[str]) -> float:
    """The share of the 3 pairs the human ordered the same way as Engine 1."""
    ex = {x: i for i, x in enumerate(expected_order)}
    hu = {x: i for i, x in enumerate(order)}
    pairs = [("A", "B"), ("A", "C"), ("B", "C")]
    return sum(1 for a, b in pairs if (ex[a] < ex[b]) == (hu[a] < hu[b])) / len(pairs)


def _clean_answer(check: SpotCheck, answer: Any) -> tuple[dict, float | None, str]:
    """(stored answer, agreement, status); a 422 for anything malformed."""
    if not isinstance(answer, dict):
        raise DivergenceError("answer must be an object")
    if "skip" in answer:
        if answer["skip"] is not True or len(answer) != 1:
            raise DivergenceError('To skip, answer {"skip": true}')
        return {"skip": True}, None, "skipped"
    if check.kind == "triad":
        if set(answer) != {"order"}:
            raise DivergenceError('A triad answer is {"order": ["B", "A", "C"]}, best to worst')
        order = answer["order"]
        if not isinstance(order, list) or sorted(map(str, order)) != list(LETTERS) or not all(isinstance(x, str) for x in order):
            raise DivergenceError("order must use each of A, B and C once, best to worst")
        return {"order": list(order)}, triad_agreement(check.item["expected_order"], order), "done"
    if not set(answer) <= {"verdict", "pushbacks_sharp"} or answer.get("verdict") not in VERDICT_AGREEMENT:
        raise DivergenceError('A critique answer is {"verdict": "agree" | "partly" | "disagree", "pushbacks_sharp": [bool, bool, bool]}')
    out: dict[str, Any] = {"verdict": answer["verdict"]}
    sharp = answer.get("pushbacks_sharp")
    if sharp is not None:
        if not isinstance(sharp, list) or len(sharp) != 3 or not all(isinstance(x, bool) for x in sharp):
            raise DivergenceError("pushbacks_sharp must be three true/false values, one per pushback")
        out["pushbacks_sharp"] = list(sharp)
    return out, VERDICT_AGREEMENT[answer["verdict"]], "done"


async def answer_spot_check(db: AsyncSession, check: SpotCheck, *, user: Any, answer: Any, note: str | None = None) -> SpotCheck:
    """Record a human answer (or a skip). `decided_by` is the session's user, never the request's. A done
    check can be answered again while the set is open (the answer is overwritten and re-stamped); a reviewed
    set is a 409."""
    s = await db.get(DivergenceSet, check.set_id)
    if s is None:
        raise DivergenceError("Set not found", 404)
    try:
        await _lock_open(db, s)  # from here to the commit no one can review the set under us
        await db.refresh(check)
        if not any(c.status == "pending" for c in await load_checks(db, s.id)):
            raise DivergenceError("answers are locked once revealed", 409)
        stored, agreement, status = _clean_answer(check, answer)
    except DivergenceError:
        await db.rollback()
        raise
    check.answer, check.agreement, check.status = stored, agreement, status
    check.note = (note or "").strip()[:2000] or None
    check.decided_by, check.decided_at = user.id, _now()
    await db.commit()
    await db.refresh(check)
    return check


async def _lock_open(db: AsyncSession, s: DivergenceSet) -> None:
    """Take the set's row lock, but only while it is open (409 otherwise): a no-op UPDATE ... WHERE
    status = 'open' inside the writer's transaction. On Postgres it blocks a concurrent writer until this
    transaction ends and re-checks the status when it wakes, so an answer and a review never interleave."""
    res = await db.execute(update(DivergenceSet).where(DivergenceSet.id == s.id, DivergenceSet.status == "open")
                           .values(status="open").execution_options(synchronize_session=False))
    if res.rowcount != 1:
        await db.rollback()
        raise DivergenceError("This set is reviewed; its spot checks can no longer change", 409)
    await db.refresh(s)


def spot_check_gate(checks: list[SpotCheck], settings: Any = None, shortfall: dict | None = None) -> dict[str, Any]:
    """Agreement of the human with the machines: the mean over done triads and over done critiques, the
    gate's value the lower of the two. `insufficient_data` while any check is pending (and then no agreement
    is shown at all: a running figure would reveal Engine 1's order); `tripped` when either mean is under
    `eval_spot_check_agreement_min` (no client use); otherwise `warn` when over a third of a kind was skipped,
    when a kind has no checks, or when fewer checks were drawn than wanted (`shortfall`); else `ok`."""
    st = settings or get_settings()
    floor = float(st.eval_spot_check_agreement_min)
    shortfall = shortfall or {}
    by_kind: dict[str, list[SpotCheck]] = {"triad": [], "critique": []}
    for c in checks:
        by_kind.setdefault(c.kind, []).append(c)
    means = {k: _mean([c.agreement for c in v if c.status == "done" and c.agreement is not None]) for k, v in by_kind.items()}
    skipped = {k: sum(1 for c in v if c.status == "skipped") for k, v in by_kind.items()}
    total = len(checks)
    answered = sum(1 for c in checks if c.status != "pending")
    known = [m for m in means.values() if m is not None]
    out = {"key": GATE_SPOT, "value": min(known) if known else None, "n": answered, "threshold": {"min": floor},
           "triad_agreement": means["triad"], "critique_agreement": means["critique"],
           "n_triads": len(by_kind["triad"]), "n_critiques": len(by_kind["critique"]), "n_skipped": sum(skipped.values())}
    if answered < total or not total:
        hidden = {**out, "value": None, "triad_agreement": None, "critique_agreement": None}
        return {**hidden, "state": "insufficient_data", "detail": f"{answered} of {total} answered." if total else "No spot checks were sampled."}
    if any(m is not None and round(m, 9) < floor for m in means.values()):
        return {**out, "state": "tripped", "detail": f"Agreement under {floor:.0%}: no client use "
                f"(triads {_pct(means['triad'])}, critiques {_pct(means['critique'])})."}
    unanswered = [k for k in ("triad", "critique") if by_kind[k] and means[k] is None]
    if unanswered:  # fail closed: checks with no human answer at all are not agreement
        return {**out, "state": "tripped", "detail": "; ".join(f"No {k} was answered; agreement can't be shown" for k in unanswered) + "."}
    why = []
    for kind, label, wanted_key in (("triad", "triads", "triads"), ("critique", "critiques", "critiques")):
        n, short = len(by_kind[kind]), int(shortfall.get(wanted_key) or 0)
        if not n:
            why.append(f"no {label} were drawn")
        elif short > 0:
            why.append(f"only {n} of {n + short} {label} could be drawn")
        if n and skipped[kind] * 3 > n:
            why.append(f"more than a third of the {label} were skipped")
    if why:
        return {**out, "state": "warn", "detail": "The agreement rests on few answers: " + "; ".join(why) + "."}
    return {**out, "state": "ok", "detail": f"Agreement {floor:.0%} or better (triads {_pct(means['triad'])}, critiques {_pct(means['critique'])})."}


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.0%}"


# ── lifecycle ───────────────────────────────────────────────────────────────────────────────────────
async def create_set(db: AsyncSession, *, workspace: Any, project_id: uuid.UUID | None = None, user: Any, name: str = "",
                     pass_id: uuid.UUID, tournament_ids: list, arm_key: str | None = None) -> DivergenceSet:
    """Join, freeze the result in `computed`, sample the spot checks and commit. `workspace` is the workspace
    (or its id); the pass must belong to it. `project_id` defaults to the pass's project (a different one is a 404)."""
    ws_id = getattr(workspace, "id", workspace)
    computed = await build_join(db, pass_id=pass_id, tournament_ids=tournament_ids, arm_key=arm_key, workspace_id=ws_id)
    p = await db.get(CredibilityPass, pass_id)
    if project_id is not None and project_id != p.project_id:
        raise DivergenceError("Credibility pass not found", 404)
    if not computed["rows"]:
        raise DivergenceError("No report has both a Credibility Score and a module rating in these inputs")
    modules = [t["module"] for t in computed["inputs"]["tournaments"]]
    s = DivergenceSet(
        id=uuid.uuid4(), workspace_id=ws_id, project_id=p.project_id, name=(name or "").strip()[:120] or f"{p.name or 'Pass'}: {', '.join(modules)}",
        credibility_pass_id=p.id, tournament_ids=[t["id"] for t in computed["inputs"]["tournaments"]], modules=modules, arm_key=arm_key,
        status="open", computed=computed, acknowledged_gates=[], created_by=user.id)
    db.add(s)
    await db.flush()
    spent = await _spent_set(db, p.id)
    if spent is not None:
        # spot checks are per pass: the first set's answers are the record, and a new set must not get a fresh look at the same reports
        computed["stats"]["spot_checks_from_set"] = str(spent.id)
        computed["stats"]["spot_checks_from_set_name"] = spent.name
    else:
        db.add_all(await sample_spot_checks(db, s, computed, get_settings()))
    flag_modified(s, "computed")  # the sampler recorded the shortfall inside the dict
    await db.commit()
    await db.refresh(s)
    return s


async def _spent_set(db: AsyncSession, pass_id: uuid.UUID) -> DivergenceSet | None:
    """The oldest set on this pass whose blinding is spent (any spot check answered or skipped), if any."""
    for other in (await db.execute(select(DivergenceSet).where(DivergenceSet.credibility_pass_id == pass_id)
                                   .order_by(DivergenceSet.created_at, DivergenceSet.id))).scalars():
        if any(c.status != "pending" for c in await load_checks(db, other.id)):
            return other
    return None


def _warnings(stats: dict, st: Any) -> list[str]:
    out = []
    if stats["mixed_arms"]:
        out.append("The scores come from more than one arm, so from different models: pick one arm, or read the per-arm correlations rather than the pooled one.")
    if stats["n_partial"]:
        out.append(f"{stats['n_partial']} report(s) are rated in only some of the selected modules; they are left out of r, the quadrants and the reading list.")
    if stats["n_whole_only"]:
        out.append(f"{stats['n_whole_only']} scored report(s) have no module rating in the selected tournaments.")
    if stats["n_parts_only"]:
        out.append(f"{stats['n_parts_only']} rated report(s) have no Credibility Score in this pass.")
    if stats["n_other_arm"]:
        out.append(f"{stats['n_other_arm']} rated report(s) were scored by another arm than the one selected.")
    if stats["n_complete"] < int(st.eval_divergence_min_n):
        out.append(f"Only {stats['n_complete']} complete report(s): the parts-vs-whole r needs {int(st.eval_divergence_min_n)} before it is judged.")
    return out


async def preview_set(db: AsyncSession, *, workspace_id: uuid.UUID, pass_id: uuid.UUID, tournament_ids: list, arm_key: str | None = None) -> dict[str, Any]:
    """The join with nothing written: stats, the parts-vs-whole gate, the reading-list size and warnings."""
    st = get_settings()
    c = await build_join(db, pass_id=pass_id, tournament_ids=tournament_ids, arm_key=arm_key, workspace_id=workspace_id)
    return {"stats": c["stats"], "gate": parts_vs_whole_gate(c, st), "n_reading_list": len(c["reading_list"]), "warnings": _warnings(c["stats"], st)}


async def load_checks(db: AsyncSession, set_id: uuid.UUID) -> list[SpotCheck]:
    rows = (await db.execute(select(SpotCheck).where(SpotCheck.set_id == set_id))).scalars().all()
    return sorted(rows, key=lambda c: (0 if c.kind == "triad" else 1, c.position))


def gates_for(s: DivergenceSet, checks: list[SpotCheck], settings: Any = None) -> list[dict]:
    """The two gates of a set, each with who acknowledged it (and why) when somebody did."""
    acked = {a.get("gate"): a for a in (s.acknowledged_gates or [])}
    out = []
    stats = (s.computed or {}).get("stats") or {}
    short = stats.get("spot_check_shortfall")
    if stats.get("spot_checks_from_set"):
        spot = {"key": GATE_SPOT, "state": "not_applicable", "value": None, "n": 0, "threshold": {}, "triad_agreement": None, "critique_agreement": None,
                "n_triads": 0, "n_critiques": 0, "n_skipped": 0, "from_set": stats["spot_checks_from_set"],
                "detail": f"Spot checks for this pass were already answered in set {stats.get('spot_checks_from_set_name') or stats['spot_checks_from_set']}; their result stands there."}
    else:
        spot = spot_check_gate(checks, settings, short)
    for g in (parts_vs_whole_gate(s.computed or {}, settings), spot):
        if g["key"] in acked:
            g = {**g, "acknowledged_by": acked[g["key"]].get("by"), "acknowledged_note": acked[g["key"]].get("note")}
        out.append(g)
    return out


async def acknowledge_gate(db: AsyncSession, s: DivergenceSet, user: Any, gate: str, note: str | None) -> DivergenceSet:
    """Say a tripped gate was looked at. A note is required (422); the gate has to be tripped right now (409).
    Takes the set's row lock first and reads the state after it, so two acknowledgements both land and none lands after a review."""
    if not (note or "").strip():
        raise DivergenceError("A note is required when acknowledging a gate: say why the tripped gate can be accepted")
    if gate not in GATES:
        raise DivergenceError(f"Unknown gate {gate!r}: {', '.join(GATES)}")
    await _lock_open(db, s)
    try:
        g = next(x for x in gates_for(s, await load_checks(db, s.id)) if x["key"] == gate)
        if g["state"] != "tripped":
            raise DivergenceError(f"Gate {gate} is {g['state']}, not tripped: there is nothing to acknowledge", 409)
    except DivergenceError:
        await db.rollback()
        raise
    acks = [a for a in (s.acknowledged_gates or []) if a.get("gate") != gate]
    acks.append({"gate": gate, "by": getattr(user, "email", None), "at": _now().isoformat(), "note": note.strip()[:500]})
    s.acknowledged_gates = acks
    await db.commit()
    await db.refresh(s)
    return s


async def mark_reviewed(db: AsyncSession, s: DivergenceSet, user: Any, note: str | None = None) -> DivergenceSet:
    """Close the set. Every spot check has to be done or skipped (409) and every tripped gate acknowledged
    (409 naming them). Low agreement does not block this: it is recorded, and the set reads "no client use"."""
    if s.status != "open":
        raise DivergenceError("This set is already reviewed", 409)
    await _lock_open(db, s)
    try:
        checks = await load_checks(db, s.id)
        pending = sum(1 for c in checks if c.status == "pending")
        if pending:
            raise DivergenceError(f"{pending} spot check(s) are still pending: answer or skip them first", 409)
        acked = {a.get("gate") for a in (s.acknowledged_gates or [])}
        open_gates = [g["key"] for g in gates_for(s, checks) if g["state"] == "tripped" and g["key"] not in acked]
        if open_gates:
            raise DivergenceError(f"Gate(s) tripped and not acknowledged: {', '.join(open_gates)}. Acknowledge each with a note first.", 409)
    except DivergenceError:
        await db.rollback()
        raise
    if (note or "").strip():
        s.acknowledged_gates = [*(s.acknowledged_gates or []), {"gate": "review", "by": getattr(user, "email", None), "at": _now().isoformat(), "note": note.strip()[:500]}]
    s.status, s.reviewed_by, s.reviewed_at = "reviewed", user.id, _now()
    await db.commit()
    await db.refresh(s)
    return s


async def delete_set(db: AsyncSession, s: DivergenceSet) -> None:
    await _lock_open(db, s)
    if any(c.status != "pending" for c in await load_checks(db, s.id)):
        await db.rollback()
        raise DivergenceError("a set with spot-check answers is kept as the record; it can't be deleted", 409)
    await db.execute(delete(SpotCheck).where(SpotCheck.set_id == s.id))
    await db.delete(s)
    await db.commit()


async def get_set(db: AsyncSession, *, workspace_id: uuid.UUID, set_id: uuid.UUID) -> DivergenceSet:
    s = await db.get(DivergenceSet, set_id)
    if s is None or s.workspace_id != workspace_id:
        raise DivergenceError("Divergence set not found", 404)
    return s


async def get_check(db: AsyncSession, *, workspace_id: uuid.UUID, check_id: uuid.UUID) -> SpotCheck:
    c = await db.get(SpotCheck, check_id)
    s = await db.get(DivergenceSet, c.set_id) if c is not None else None
    if c is None or s is None or s.workspace_id != workspace_id:
        raise DivergenceError("Spot check not found", 404)
    return c


# ── output ──────────────────────────────────────────────────────────────────────────────────────────
def _locked(checks: list[SpotCheck]) -> bool:
    """True once no spot check is pending: only then does anything Engine 1 knew about them leave the server."""
    return not any(c.status == "pending" for c in checks)


LOCKED_MESSAGE = "Finish the spot checks first: the reading list and rows show module bands"


def set_list_out(s: DivergenceSet, pass_name: str | None, checks: list[SpotCheck], settings: Any = None) -> dict[str, Any]:
    stats = (s.computed or {}).get("stats") or {}
    return {
        "id": str(s.id), "name": s.name, "status": s.status, "project_id": str(s.project_id), "credibility_pass_id": str(s.credibility_pass_id),
        "pass_name": pass_name, "modules": list(s.modules or []), "arm_key": s.arm_key, "created_at": _iso(s.created_at),
        "reviewed_at": _iso(s.reviewed_at), "n_complete": stats.get("n_complete"), "pearson": stats.get("pearson"),
        "quadrant_counts": stats.get("quadrants") or {}, "gates": gates_for(s, checks, settings),
        "spot_checks": {"total": len(checks), "answered": sum(1 for c in checks if c.status != "pending"), "locked": _locked(checks)}}


def set_out(s: DivergenceSet, pass_name: str | None, checks: list[SpotCheck], settings: Any = None, stale: dict | None = None) -> dict[str, Any]:
    c = s.computed or {}
    return {
        **set_list_out(s, pass_name, checks, settings), **(stale or {"stale": False, "stale_tournaments": [], "stale_pass": False}), "tournament_ids": list(s.tournament_ids or []), "stats": c.get("stats") or {},
        "inputs": c.get("inputs") or {}, "acknowledged_gates": list(s.acknowledged_gates or []),
        "created_by": str(s.created_by) if s.created_by else None, "reviewed_by": str(s.reviewed_by) if s.reviewed_by else None,
        "scatter": [{"id": r["disclosure_report_id"], "parts": r["parts"], "whole": r["whole"], "quadrant": r["quadrant"], "delta": r["delta"]}
                    for r in c.get("rows") or [] if r.get("complete")] if _locked(checks) else []}


async def pass_name_of(db: AsyncSession, s: DivergenceSet) -> str | None:
    p = await db.get(CredibilityPass, s.credibility_pass_id)
    return p.name if p else None


async def set_detail(db: AsyncSession, s: DivergenceSet) -> dict[str, Any]:
    return set_out(s, await pass_name_of(db, s), await load_checks(db, s.id), stale=await stale_info(db, s))


async def list_sets(db: AsyncSession, *, workspace_id: uuid.UUID, limit: int = 100) -> list[dict[str, Any]]:
    sets = (await db.execute(select(DivergenceSet).where(DivergenceSet.workspace_id == workspace_id)
                             .order_by(DivergenceSet.created_at.desc()).limit(limit))).scalars().all()
    out = []
    for s in sets:
        out.append(set_list_out(s, await pass_name_of(db, s), await load_checks(db, s.id)))
    return out


async def _document_ids(db: AsyncSession, report_ids: list[str]) -> dict[str, str]:
    if not report_ids:
        return {}
    found = (await db.execute(select(DisclosureReport.id, DisclosureReport.document_id).where(
        DisclosureReport.id.in_([uuid.UUID(i) for i in report_ids])))).all()
    return {str(i): str(d) for i, d in found}


async def rows_out(db: AsyncSession, rows: list[dict]) -> list[dict]:
    """Rows with `document_id` (the source document of the report) added."""
    docs = await _document_ids(db, [r["disclosure_report_id"] for r in rows])
    return [{**r, "document_id": docs.get(r["disclosure_report_id"])} for r in rows]


async def _require_locked(db: AsyncSession, s: DivergenceSet) -> None:
    if not _locked(await load_checks(db, s.id)):
        raise DivergenceError(LOCKED_MESSAGE, 409)


async def rows_page(db: AsyncSession, s: DivergenceSet, *, quadrant: str | None = None, complete: bool | None = None,
                    limit: int = 500, offset: int = 0) -> dict[str, Any]:
    await _require_locked(db, s)
    rows = [r for r in (s.computed or {}).get("rows") or []
            if (quadrant is None or r.get("quadrant") == quadrant) and (complete is None or bool(r.get("complete")) == complete)]
    return {"total": len(rows), "rows": await rows_out(db, rows[offset: offset + limit])}


async def reading_list_out(db: AsyncSession, s: DivergenceSet, *, enforce_lock: bool = True) -> list[dict]:
    if enforce_lock:
        await _require_locked(db, s)
    c = s.computed or {}
    by_id = {r["disclosure_report_id"]: r for r in c.get("rows") or []}
    rows = [by_id[i] for i in c.get("reading_list") or [] if i in by_id]
    reviews = {str(r.id): r for r in (await db.execute(select(CredibilityReview).where(
        CredibilityReview.id.in_([uuid.UUID(r["review_id"]) for r in rows])))).scalars()} if rows else {}
    out = []
    for r in await rows_out(db, rows):
        rv = reviews.get(r["review_id"])
        out.append({**r, "review": None if rv is None else {
            "summary": rv.summary, "pushbacks": rv.pushbacks, "story_breaks": rv.story_breaks, "credibility_score": rv.credibility_score, "model": rv.model}})
    return out


async def _member_text(m: PeerPoolMember) -> tuple[str | None, str | None]:
    """The member's snapshotted module text, hash-checked as the tournament runner reads it: (text, error)."""
    if not m.text_key or not m.text_sha256:
        return None, TEXT_UNAVAILABLE
    try:
        data = await get_store().get(m.text_key)
    except FileNotFoundError:
        return None, TEXT_UNAVAILABLE
    except Exception:  # the blob store is transient trouble, not a reason to break the page
        log.warning("spot check text unreadable: %s", m.text_key, exc_info=True)
        return None, TEXT_UNAVAILABLE
    if hashlib.sha256(data).hexdigest() != m.text_sha256:
        return None, TEXT_UNAVAILABLE
    return data.decode("utf-8", errors="replace"), None


def _common(c: SpotCheck, locked: bool) -> dict[str, Any]:
    # the agreement is computed against Engine 1's truth, so it is shown only once everything is revealed
    return {"id": str(c.id), "kind": c.kind, "position": c.position, "status": c.status, "answer": c.answer, "agreement": c.agreement if locked else None,
            "note": c.note, "decided_by": str(c.decided_by) if c.decided_by else None, "decided_at": _iso(c.decided_at)}


async def _triad_members(db: AsyncSession, c: SpotCheck) -> dict[uuid.UUID, PeerPoolMember]:
    ids = [uuid.UUID(m["member_id"]) for m in c.item["members"]]
    return {m.id: m for m in (await db.execute(select(PeerPoolMember).where(PeerPoolMember.id.in_(ids)))).scalars()}


async def triad_out(db: AsyncSession, c: SpotCheck, locked: bool = False, members: dict | None = None,
                    texts_by_member: dict | None = None) -> dict[str, Any]:
    """A triad. Until every check of the set is answered or skipped (`locked`): only the letters and the masked extracts (no rating, band, company, id, pool or
    expected order anywhere). Done or skipped: `revealed` adds Engine 1's order and each member's rating."""
    item = c.item or {}
    ids = [uuid.UUID(m["member_id"]) for m in item["members"]]
    members = members if members is not None else await _triad_members(db, c)
    names: list[str] = []
    domains: list[str] = []
    for i in ids:
        m = members.get(i)
        if m is not None:
            names += [x for x in [m.company, *(m.mask_names or [])] if x]
            domains += list(m.mask_domains or [])
    texts = []
    for tm in item["members"]:
        m = members.get(uuid.UUID(tm["member_id"]))
        if texts_by_member is not None and m is not None:
            text, err = texts_by_member[m.id]
        else:
            text, err = await _member_text(m) if m is not None else (None, TEXT_UNAVAILABLE)
        entry: dict[str, Any] = {"letter": tm["letter"], "text": mask_names(text, names, domains) if text is not None else None}
        if err:
            entry["error"] = err
        texts.append(entry)
    out = {**_common(c, locked), "module": item["module"], "texts": texts, "revealed": None}
    if locked and c.status != "pending":
        out["revealed"] = {
            "expected_order": list(item["expected_order"]), "agreement": c.agreement,
            "members": [{"letter": tm["letter"], "company": members[uuid.UUID(tm["member_id"])].company if uuid.UUID(tm["member_id"]) in members else None,
                         "band": tm["band"], "band_kind": tm["band_kind"], "rating": tm["rating"]} for tm in item["members"]]}
    return out


async def critique_out(db: AsyncSession, c: SpotCheck, s: DivergenceSet, locked: bool = False) -> dict[str, Any]:
    """A critique: the whole-report review on its own (no module band while pending); `revealed` adds the parts."""
    item = c.item or {}
    rv = await db.get(CredibilityReview, uuid.UUID(item["review_id"]))
    docs = await _document_ids(db, [item["disclosure_report_id"]])
    out = {**_common(c, locked), "company": item["company"], "disclosure_report_id": item["disclosure_report_id"],
           "document_id": docs.get(item["disclosure_report_id"]),
           "review": None if rv is None else {
               "credibility_score": rv.credibility_score, "summary": rv.summary, "pushbacks": rv.pushbacks, "story_breaks": rv.story_breaks,
               "model": rv.model, "input_mode": rv.input_mode},
           "revealed": None}
    if locked and c.status != "pending":
        row = next((r for r in (s.computed or {}).get("rows") or [] if r["disclosure_report_id"] == item["disclosure_report_id"]), {})
        out["revealed"] = {"from_reading_list": item["from_reading_list"], "parts": row.get("parts", item.get("parts")), "quadrant": row.get("quadrant", item.get("quadrant")), "modules": row.get("modules") or {}}
    return out


async def spot_check_out(db: AsyncSession, c: SpotCheck, s: DivergenceSet | None = None, locked: bool | None = None) -> dict[str, Any]:
    if locked is None:
        locked = _locked(await load_checks(db, c.set_id))
    if c.kind == "triad":
        return await triad_out(db, c, locked)
    return await critique_out(db, c, s or await db.get(DivergenceSet, c.set_id), locked)


async def spot_checks_out(db: AsyncSession, s: DivergenceSet) -> list[dict[str, Any]]:
    checks = await load_checks(db, s.id)
    locked = _locked(checks)
    members: dict[uuid.UUID, dict] = {}
    for c in checks:  # the DB reads stay sequential on the session; only the blob reads fan out below
        if c.kind == "triad":
            members[c.id] = await _triad_members(db, c)
    unique = {m.id: m for ms in members.values() for m in ms.values()}
    texts = dict(zip(unique, await asyncio.gather(*(_member_text(m) for m in unique.values()))))
    return [await triad_out(db, c, locked, members[c.id], texts) if c.kind == "triad" else await spot_check_out(db, c, s, locked) for c in checks]


# ── the create form ─────────────────────────────────────────────────────────────────────────────────
async def inputs_out(db: AsyncSession, *, workspace_id: uuid.UUID) -> dict[str, Any]:
    """What a set can be built from: the workspace's completed passes and completed tournaments."""
    passes = (await db.execute(select(CredibilityPass).where(CredibilityPass.workspace_id == workspace_id, CredibilityPass.status == "completed")
                               .order_by(CredibilityPass.created_at.desc()))).scalars().all()
    done = dict((await db.execute(select(CredibilityReview.pass_id, func.count()).where(
        CredibilityReview.pass_id.in_([p.id for p in passes]), CredibilityReview.status == "done").group_by(CredibilityReview.pass_id))).all()) if passes else {}
    tours = (await db.execute(select(EvalTournament, PeerPoolSnapshot.project_id).join(PeerPoolSnapshot, PeerPoolSnapshot.id == EvalTournament.snapshot_id)
                              .where(EvalTournament.workspace_id == workspace_id, EvalTournament.status == "completed")
                              .order_by(EvalTournament.created_at.desc()))).all()
    rated = dict((await db.execute(select(ModuleRating.tournament_id, func.count()).where(
        ModuleRating.tournament_id.in_([t.id for t, _ in tours])).group_by(ModuleRating.tournament_id))).all()) if tours else {}
    return {
        "passes": [{"id": str(p.id), "name": p.name, "status": p.status, "project_id": str(p.project_id) if p.project_id else None,
                    "arms": [{"key": a.get("key"), "label": a.get("label") or a.get("key")} for a in (p.arms or [])], "n_done": int(done.get(p.id, 0))}
                   for p in passes],
        "tournaments": [{"id": str(t.id), "name": f"{t.module} ({t.created_at.date().isoformat() if t.created_at else ''})", "module": t.module,
                         "status": t.status, "project_id": str(pr) if pr else None,
                         "completed_at": (t.summary or {}).get("completed_at") or _iso(t.updated_at), "n_rated": int(rated.get(t.id, 0))}
                        for t, pr in tours]}
