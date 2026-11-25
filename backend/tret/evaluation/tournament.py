"""The Engine 1 tournament (disclosure evaluation engine, PR 6).

Pairwise juror matches over the peer pools of a snapshot (`evaluation/pools.py`), played in Swiss
rounds (`evaluation/pairing.py`) and fitted to Bradley-Terry ratings at the end
(`evaluation/ratings.py`).

Show before spend:
* `create_tournament` is a free preview: the pools, N, rounds, the estimated matches, a cost range
  and the jury the router would seat with its reasons. A `draft` row; no model is called.
* `start_tournament` is a separate action with a USD budget. It freezes the jury once
  (`evaluation/jury.py`, purpose `eval_jury`, pass `tournament:<id>`), so the instrument does not
  change under the measurement, and spawns the runner. Spend stops at the budget: the tournament
  pauses (`pause_reason` `budget`) and `resume_tournament` continues, optionally with a raised budget.

History is immutable. A match and a rating point at a pool member, which copies the module's text
location (content-addressed), hash, word count and claim density at snapshot time; a re-extraction
or a deleted report module never changes or removes what a tournament judged. The juror reads the
member's text and verifies its hash; a mismatch or a missing blob fails the match (not counted).

Blind jurors: a juror sees the module definition and the two texts, with the company's names,
tickers and domains masked (`mask_names_with_count`; the counts are kept on the match), in a seeded
random order that is recorded on the match. Never the company, a rating or Engine 2 output. The
text delimiters cannot be spoofed from inside a text (`defang`).

Every match is one `Run` on the system "Disclosure Jury" harness, executed through
`engine/direct.execute_direct` (cost caps, billing gates, energy, the runs UI), calling
`routed_json_call(freeze=..., model=...)`: the frozen model, no fallback to another one. The run is
stamped `JUROR_KEY`, so it never becomes router evidence.

Running safely: state lives in the database. A runner holds a lease (`lease_owner`, `lease_until`,
heartbeat), start and resume are conditional updates, and each match is claimed
(`pending` -> `running`) before it is played, so two processes never play the same match. A
credit or limit refusal, an unreachable frozen model, or too many failed matches PAUSE the
tournament (`credits`, `juror_unavailable`, `juror_failures`, `match_failures`); only an internal
error fails it. Every pause can be resumed. `refit_tournament` recomputes the ratings from the
stored matches with no spend.

Diagnostics (`evaluation/diagnostics.py`, PR 7): when a round finishes the runner fits provisional
ratings and evaluates the gates. A flip-prone juror family is dropped from the rotation for the rest
of the pass (fewer than two left pauses, `jury_degraded`); a length bias or low juror agreement pauses
(`length_bias`, `low_agreement`). If a ready anchor set (`evaluation/anchors.py`) exists for the module,
its pairs are judged first by every frozen juror (matches of kind `anchor`, round 0, never rated or
paired) and too many changed verdicts pause (`anchor_drift`). `resume` takes `acknowledge_gates`: an
acknowledged gate no longer pauses that tournament, and resuming without acknowledging a tripped
pausing gate is a 409.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import random
import re
import platform
import uuid
from collections import OrderedDict, deque
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from sqlalchemy import and_, delete, exists, func, not_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tret.batches.rubrics import JUROR_KEY, STRUCTURED_CALL_KEY
from tret.config import get_settings
from tret.db.engine import get_session_factory
from tret.db.models import (
    EvalAnchorSet,
    EvalMatch,
    EvalMatchRun,
    EvalTournament,
    ModuleRating,
    PeerPoolMember,
    PeerPoolSnapshot,
    Project,
    RoutingFreeze,
    Run,
)
from tret.engine import direct
from tret.engine.routed_call import CallServices, check_policy
from tret.evaluation import diagnostics as dg, jury, pools as pl
from tret.evaluation.juror import (
    JUROR_PROMPT_VERSION,
    JUROR_SCHEMA,
    build_juror_messages,
    map_verdict,
    mask_names_with_count,
    score_for_a,
)
from tret.evaluation.modules import Module, load_module_set
from tret.evaluation.pairing import choose_extra, rounds_for, swiss_round
from tret.evaluation.ratings import bands, fit_bradley_terry, flip_rate, pearson, spearman
from tret.services.workspace import ensure_jury_harness

log = logging.getLogger("tret.tournament")

PASS_PREFIX = "tournament:"
JURY_K = 3
SWAP_RATE = 0.05
DOUBLE_RATE = 0.10
MAX_ROUND_FAILURE_RATE = 0.20
FAIL_WINDOW = 20  # the last matches whose outcome is watched
FAIL_MIN = 10  # fewest matches before the window can pause the tournament
LENGTH_R_FLAG = 0.6
FLIP_RATE_FLAG = 0.15
# Cost estimate: the same chars/4 idea as the extractor's, in words (a word is about 1.5 tokens in
# report prose), plus the juror prompt and a short JSON answer.
TOKENS_PER_WORD = 1.5
PROMPT_OVERHEAD_TOKENS = 700
OUTPUT_TOKENS = 250
MAX_OUTPUT_TOKENS = 600
TEXT_CACHE_SIZE = 64
RUN_STALE_SECONDS = 900.0  # a run still 'running' this long after it started is taken as dead (a call times out at 300 s, twice)
ORPHAN_POLL_SECONDS = 1.0
TEXT_ERROR = "text_error:"  # a match whose snapshot text cannot be used: failed for good, never replayed
LEASE_SECONDS = 120.0
HEARTBEAT_SECONDS = 30.0
EVAL_TOURNAMENT_KEY = "_eval_tournament"
EVAL_MATCH_KEY = "_eval_match"
CANCELLABLE = ("draft", "running", "paused")
REFITTABLE = ("completed", "failed", "paused", "cancelled")  # an unfinished one is fitted as `partial`
# A run that failed with one of these kinds could not reach its frozen model at all.
OUTAGE_KINDS = ("unroutable", "frozen_outside_policy", "not_in_freeze")
PAUSE_REASONS = ("position_bias", "budget", "credits", "juror_unavailable", "juror_failures", "match_failures", "restart", "interrupted",
                 "length_bias", "low_agreement", "anchor_drift", "jury_degraded")


class TournamentError(Exception):
    """A request the tournament cannot serve; `status` is the HTTP status the API answers with."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _lease_end() -> datetime:
    return _now() + timedelta(seconds=LEASE_SECONDS)


def new_owner() -> str:
    return f"{platform.node()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def tournament_enabled() -> bool:
    return (getattr(get_settings(), "eval_tournament", "off") or "off").strip().lower() == "on"


def _need_jury() -> None:
    if (getattr(get_settings(), "eval_jury", "off") or "off").strip().lower() != "on":
        raise TournamentError(
            "The tournament seats its jury through the jury freeze: set TRET_EVAL_JURY=on as well as TRET_EVAL_TOURNAMENT=on.", 409
        )


_DELIMS = re.compile(r"<<<|>>>")


def defang(text: str) -> str:
    """The juror prompt quotes each text between `<<<TEXT n START>>>` / `<<<TEXT n END>>>`. A text
    that contains `<<<` or `>>>` could fake an end marker and speak to the juror, so those
    sequences are replaced by look-alike single characters in both texts, before the prompt is built."""
    return _DELIMS.sub(lambda m: "‹‹‹" if m.group() == "<<<" else "›››", text)


# ── estimates ───────────────────────────────────────────────────────────────────────────────
def match_tokens(words_a: int, words_b: int) -> tuple[int, int]:
    """(input, output) tokens of one juror call over two modules of these word counts."""
    return int((words_a + words_b) * TOKENS_PER_WORD) + PROMPT_OVERHEAD_TOKENS, OUTPUT_TOKENS


def extra_count(n_primaries: int, rate: float) -> int:
    """How many swaps (or doubles) a round with this many primary matches gets: the runner's rule
    (`pairing.choose_extra`: `round(rate * n)`, at least one, at most n)."""
    if n_primaries <= 0 or rate <= 0:
        return 0
    return min(n_primaries, max(1, round(rate * n_primaries)))


def _pool_plan(plist: list[pl.Pool], avg_words: dict[str, float]) -> list[dict]:
    out = []
    for p in plist:
        rounds = rounds_for(p.n)
        out.append({**p.to_json(), "rounds": rounds, "primary_matches": (p.n // 2) * rounds,
                    "avg_words": round(avg_words.get(p.key, 0.0), 1)})
    return out


def estimate_plan(plan: list[dict], jurors: list[Any], swap_rate: float, double_rate: float) -> dict:
    """Matches and a cost range for the plan, priced at the previewed jurors' catalog prices (the
    jury rotates, so the range is the cheapest juror to the dearest and `expected` their mean).
    Swaps and doubles follow the runner's rule: per round, over every pool's primaries of that round."""
    primary = sum(p["primary_matches"] for p in plan)
    swaps = doubles = 0
    for r in range(1, max((p["rounds"] for p in plan), default=0) + 1):
        n_r = sum(p["n"] // 2 for p in plan if p["rounds"] >= r)
        swaps += extra_count(n_r, swap_rate)
        doubles += extra_count(n_r, double_rate) if len(jurors) >= 2 else 0
    total = primary + swaps + doubles
    factor = total / primary if primary else 0.0
    tin = tout = 0.0
    for p in plan:
        if not p["primary_matches"]:
            continue
        i, o = match_tokens(int(p["avg_words"]), int(p["avg_words"]))
        tin += i * p["primary_matches"] * factor
        tout += o * p["primary_matches"] * factor
    per_juror = [(float(m.cost_usd(int(tin), int(tout))) if m is not None else None) for m in jurors]
    priced = [c for c in per_juror if c is not None]
    cost = None
    if priced:
        cost = {"low": min(priced), "high": max(priced), "expected": sum(priced) / len(priced)}
    return {
        "matches": {"primary": primary, "swap": swaps, "double": doubles, "total": total},
        "tokens": {"input": int(tin), "output": int(tout)},
        "cost_usd": cost,
        "method": "words x 1.5 tokens per module pair + 700 prompt tokens + 250 output tokens per match, priced "
        "at each previewed juror's catalog price (the jury rotates: low = cheapest juror for every match, high = dearest)",
    }


# ── create (the free preview) ──────────────────────────────────────────────────────────────
def _module_def(module_set: str, module: str) -> Module:
    ms = load_module_set()
    if ms.ident != module_set:
        raise TournamentError(f"module set {module_set!r} is not the configured one ({ms.ident!r})")
    for m in ms.modules:
        if m.slug == module:
            return m
    raise TournamentError(f"module {module!r} is not in module set {ms.ident}")


async def ready_anchor_set(db: AsyncSession, workspace_id: uuid.UUID, module: str, module_set: str) -> EvalAnchorSet | None:
    """The latest `ready` anchor set of this workspace, module and module set (what a tournament re-judges first)."""
    return (await db.execute(
        select(EvalAnchorSet).where(
            EvalAnchorSet.workspace_id == workspace_id, EvalAnchorSet.module == module, EvalAnchorSet.module_set == module_set,
            EvalAnchorSet.status == "ready",
        ).order_by(EvalAnchorSet.created_at.desc(), EvalAnchorSet.id).limit(1)
    )).scalar_one_or_none()


def add_anchor_estimate(est: dict, aset: EvalAnchorSet | None, models: list[Any]) -> None:
    """The anchor matches (every usable pair x every juror, judged first by each juror) in the plan's
    matches, tokens and cost range, and `anchor_set` (id, pairs, matches; null when there is none)."""
    pairs = [p for p in (aset.pairs if aset else []) if p.get("baseline")]
    if not pairs:
        est["anchor_set"] = None
        est["matches"]["anchor"] = 0
        return
    k = len(models)
    tin = tout = 0
    for p in pairs:
        i, o = match_tokens(int(p["a"].get("word_count") or 0), int(p["b"].get("word_count") or 0))
        tin, tout = tin + i * k, tout + o * k
    n = len(pairs) * k
    est["matches"]["anchor"] = n
    est["matches"]["total"] += n
    est["tokens"] = {"input": est["tokens"]["input"] + tin, "output": est["tokens"]["output"] + tout}
    if est.get("cost_usd"):
        # every juror judges every anchor pair (no rotation): the same extra for the low, expected and high figures
        extra = 0.0
        for m in models:
            if m is None:
                continue
            for p in pairs:
                extra += float(m.cost_usd(*match_tokens(int(p["a"].get("word_count") or 0), int(p["b"].get("word_count") or 0))))
        est["cost_usd"] = {k_: v + extra for k_, v in est["cost_usd"].items()}
    est["anchor_set"] = {"id": str(aset.id), "pairs": len(pairs), "matches": n}


async def create_tournament(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    module: str,
    module_set: str | None = None,
    batch_ids: list[uuid.UUID] | None = None,
    min_n: int = pl.DEFAULT_MIN_N,
    model_policy: dict | None = None,
    created_by: str | None = None,
    seed: int | None = None,
    services: CallServices | None = None,
) -> EvalTournament:
    """A `draft` tournament: snapshot, rounds, estimates and the jury preview. Nothing is spent and
    no model is called. Flushed, not committed."""
    _need_jury()
    ms = load_module_set()
    module_set = module_set or ms.ident
    module_def = _module_def(module_set, module)
    services = services or await jury.get_call_services(db, workspace_id)
    try:
        policy = check_policy(model_policy)
    except Exception as e:  # noqa: BLE001 - RoutedCallError(pinned_policy)
        raise TournamentError(str(e)) from e
    try:
        snap, plist = await pl.build_snapshot(
            db, workspace_id=workspace_id, module_set=module_set, module=module, batch_ids=batch_ids,
            min_n=min_n, created_by=created_by,
        )
    except pl.PoolError as e:
        raise TournamentError(str(e), e.status) from e
    rated = [p for p in plist if p.n >= 2]
    if not rated:
        raise TournamentError(
            f"No pool has two or more classified modules for {module!r} ({snap.corpus_filter.get('excluded_unclassified', 0)} "
            "unclassified): extract the reports and classify the companies first."
        )
    members = await pl.members_of(db, snap.id)
    by_pool: dict[str, list[int]] = {}
    for m in members:
        by_pool.setdefault(m.pool_key, []).append(int(m.word_count or 0))
    avg = {k: sum(v) / len(v) for k, v in by_pool.items()}
    plan = _pool_plan(rated, avg)
    longest = max((max(v) for v in by_pool.values()), default=0)
    min_ctx = int(2 * longest * TOKENS_PER_WORD) + PROMPT_OVERHEAD_TOKENS + MAX_OUTPUT_TOKENS
    try:
        sel = await jury.preview_jury(
            db, workspace_id=workspace_id, model_policy=policy, k=JURY_K, distinct_authors=True,
            min_context_window=min_ctx, purpose="eval_jury", services=services,
        )
    except jury.JuryError as e:
        raise TournamentError(str(e), e.status) from e
    models = [services.catalog.get(p.model) for p in sel.picks]
    est = estimate_plan(plan, models, SWAP_RATE, DOUBLE_RATE)
    add_anchor_estimate(est, await ready_anchor_set(db, workspace_id, module, module_set), models)
    est["gate_coverage"] = dg.gate_coverage(est["matches"], sum(p["n"] for p in plan), len(sel.picks))
    t = EvalTournament(
        workspace_id=workspace_id, module=module, module_set=module_set, snapshot_id=snap.id,
        judge_prompt_version=JUROR_PROMPT_VERSION, module_def=module_def.model_dump(), model_policy=policy,
        rounds=max(p["rounds"] for p in plan), swap_rate=SWAP_RATE, double_rate=DOUBLE_RATE,
        seed=seed if seed is not None else random.SystemRandom().randrange(2**31), status="draft",
        estimates={
            "pools": plan, "n": sum(p["n"] for p in plan), "rounds": max(p["rounds"] for p in plan),
            **est, "jury": sel.to_json(), "min_context_window": min_ctx,
            "unrated_pools": [p.to_json() for p in plist if p.n < 2],
        },
        created_by=created_by,
    )
    db.add(t)
    await db.flush()
    return t


async def get_tournament(db: AsyncSession, *, workspace_id: uuid.UUID, tournament_id: uuid.UUID) -> EvalTournament:
    t = await db.get(EvalTournament, tournament_id)
    if t is None or t.workspace_id != workspace_id:
        raise TournamentError("Tournament not found", 404)
    return t


async def delete_draft(db: AsyncSession, *, workspace_id: uuid.UUID, tournament_id: uuid.UUID) -> None:
    """Delete a draft and its snapshot (a preview nobody started). Anything that ran is history."""
    t = await get_tournament(db, workspace_id=workspace_id, tournament_id=tournament_id)
    if t.status != "draft":
        raise TournamentError(f"Tournament is {t.status}; only a draft can be deleted", 409)
    res = await db.execute(delete(EvalTournament).where(EvalTournament.id == t.id, EvalTournament.status == "draft"))
    if res.rowcount != 1:
        await db.rollback()
        raise TournamentError("Tournament is no longer a draft", 409)
    await db.execute(delete(PeerPoolMember).where(PeerPoolMember.snapshot_id == t.snapshot_id))
    await db.execute(delete(PeerPoolSnapshot).where(PeerPoolSnapshot.id == t.snapshot_id))
    await db.commit()


# ── start / cancel / resume ───────────────────────────────────────────────────────────────
_tasks: dict[uuid.UUID, tuple[asyncio.Task, str | None]] = {}


def _spawn(tid: uuid.UUID, owner: str | None = None) -> None:
    """Run a tournament in this process. A task already running it under a DIFFERENT lease owner is
    a stale runner (the lease moved on): it is cancelled and replaced; the same owner is a no-op."""
    live = _tasks.get(tid)
    if live is not None and not live[0].done():
        if live[1] == owner and owner is not None:
            return
        live[0].cancel()
    task = asyncio.create_task(run_tournament(tid, owner))
    _tasks[tid] = (task, owner)
    task.add_done_callback(lambda t_, tid=tid: _tasks.pop(tid, None) if tid in _tasks and _tasks[tid][0] is t_ else None)


async def drain(timeout: float = 60.0) -> bool:
    """Wait for the running tournament tasks (tests, shutdown)."""
    tasks = [t for t, _o in list(_tasks.values()) if not t.done()]
    if not tasks:
        return True
    _done, pending = await asyncio.wait(tasks, timeout=timeout)
    return not pending


def _check_budget(value: Any) -> Decimal:
    try:
        b = Decimal(str(value))
    except Exception:  # noqa: BLE001
        raise TournamentError("budget_usd must be a number") from None
    if not b.is_finite() or b <= 0:
        raise TournamentError("budget_usd must be greater than 0")
    return b


async def start_tournament(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    tournament_id: uuid.UUID,
    budget_usd: Any,
    started_by: str | None = None,
    services: CallServices | None = None,
    spawn: bool = True,
) -> EvalTournament:
    """Freeze the jury, set `running` (one conditional update: a second start is a 409) and spawn
    the runner, which owns the lease the update took. The budget is the whole spend limit."""
    _need_jury()
    budget = _check_budget(budget_usd)
    t = await get_tournament(db, workspace_id=workspace_id, tournament_id=tournament_id)
    if t.status != "draft":
        raise TournamentError(f"Tournament is {t.status}; only a draft can be started (use resume for a paused one)", 409)
    try:
        row, _created = await jury.freeze_jury(
            db, workspace_id=workspace_id, pass_ref=f"{PASS_PREFIX}{t.id}", purpose="eval_jury",
            model_policy=t.model_policy, k=JURY_K, distinct_authors=True,
            min_context_window=int((t.estimates or {}).get("min_context_window") or 0),
            created_by=started_by, services=services,
        )
    except jury.JuryError as e:
        raise TournamentError(str(e), e.status) from e
    owner = new_owner()
    # The anchor set this pass will re-judge is chosen now and recorded: the one the preview named if it is
    # still ready, else the latest ready one (or none). The runner uses exactly this and never looks again.
    previewed = ((t.estimates or {}).get("anchor_set") or {}).get("id")
    chosen = None
    if previewed:
        prev = await db.get(EvalAnchorSet, uuid.UUID(previewed))
        if prev is not None and prev.status == "ready" and prev.workspace_id == workspace_id:
            chosen = prev
    chosen = chosen or await ready_anchor_set(db, workspace_id, t.module, t.module_set)
    res = await db.execute(
        update(EvalTournament)
        .where(EvalTournament.id == t.id, EvalTournament.status == "draft")
        .values(freeze_id=row.id, budget_usd=budget, status="running", pause_reason=None, started_by=started_by,
                started_at=_now(), lease_owner=owner, lease_until=_lease_end(), updated_at=_now(),
                diagnostics={**(t.diagnostics or {}), "anchor_set_id": str(chosen.id) if chosen else None})
    )
    if res.rowcount != 1:
        await db.rollback()
        raise TournamentError("Tournament was started by someone else", 409)
    await db.commit()
    await db.refresh(t)
    if spawn:
        _spawn(t.id, owner)
    return t


async def cancel_tournament(db: AsyncSession, *, workspace_id: uuid.UUID, tournament_id: uuid.UUID) -> EvalTournament:
    t = await get_tournament(db, workspace_id=workspace_id, tournament_id=tournament_id)
    res = await db.execute(
        update(EvalTournament)
        .where(EvalTournament.id == t.id, EvalTournament.status.in_(CANCELLABLE))
        .values(status="cancelled", pause_reason=None, lease_owner=None, lease_until=None, updated_at=_now())
    )
    if res.rowcount != 1:
        await db.rollback()
        await db.refresh(t)
        raise TournamentError(f"Tournament is {t.status}; nothing to cancel", 409)
    if t.freeze_id:  # the pass is over
        with contextlib.suppress(jury.JuryError):
            await jury.release_freeze(db, workspace_id=workspace_id, freeze_id=t.freeze_id)
    await db.commit()
    await db.refresh(t)
    return t


def _stale_running():
    """A `running` tournament whose runner is gone: its lease has expired (or was never taken)."""
    return and_(EvalTournament.status == "running", or_(EvalTournament.lease_until.is_(None), EvalTournament.lease_until < _now()))


async def resume_tournament(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    tournament_id: uuid.UUID,
    budget_usd: Any = None,
    spawn: bool = True,
    acknowledge_gates: list[str] | None = None,
    note: str | None = None,
    acknowledged_by: str | None = None,
) -> EvalTournament:
    """Continue a paused tournament (any pause reason), or a `running` one whose runner's lease has
    expired. One conditional update decides who runs: a second resume is a 409. `acknowledge_gates`
    records that someone looked at a tripped gate (who, when, `note`): it will not pause this
    tournament again. A tripped pausing gate that is not acknowledged (now or before) is a 409."""
    _need_jury()
    t = await get_tournament(db, workspace_id=workspace_id, tournament_id=tournament_id)
    budget = _check_budget(budget_usd) if budget_usd is not None else (Decimal(t.budget_usd) if t.budget_usd is not None else None)
    if budget is None or Decimal(t.spent_usd or 0) >= budget:
        raise TournamentError(
            f"The budget (${t.budget_usd}) is already spent (${t.spent_usd}): resume with a larger budget_usd", 409
        )
    required = required_gates(t)
    asked = list(dict.fromkeys(acknowledge_gates or []))
    not_required = [g for g in asked if g not in required]
    if not_required:
        raise TournamentError(
            f"Cannot acknowledge {', '.join(not_required)}: only a tripped gate that blocks the resume can be acknowledged"
            f" (here: {', '.join(required) or 'none'})"
        )
    if asked and not (note or "").strip():
        raise TournamentError("A note is required when acknowledging a gate: say why the tripped gate can be accepted")
    acked = dict(t.acknowledged_gates or {})
    new_acks = {g: {"by": acknowledged_by, "at": _now().isoformat(), "note": (note or "")[:500] or None}
                for g in dict.fromkeys(acknowledge_gates or []) if g not in acked}
    acked.update(new_acks)
    open_gates = [g for g in required if g not in acked]
    if open_gates:
        raise TournamentError(
            f"Gate(s) tripped and not acknowledged: {', '.join(open_gates)}. Resume with acknowledge_gates to continue anyway.", 409
        )
    owner = new_owner()
    was_paused_for, accepted_rounds = t.pause_reason, t.current_round  # the update below refreshes `t`
    res = await db.execute(
        update(EvalTournament)
        .where(EvalTournament.id == t.id, or_(EvalTournament.status.in_(("paused", "failed")), _stale_running()))
        .values(status="running", pause_reason=None, budget_usd=budget, lease_owner=owner, lease_until=_lease_end(),
                updated_at=_now(), **({"acknowledged_gates": acked} if new_acks else {}))
        .execution_options(synchronize_session=False)  # the session's copy is refreshed below
    )
    if res.rowcount != 1:
        await db.rollback()
        await db.refresh(t)
        raise TournamentError(f"Tournament is {t.status}; only a paused or failed one (or one whose runner stopped) can be resumed", 409)
    if was_paused_for in ("juror_failures", "match_failures"):
        # the failures that paused it are replayed (rounds already accepted keep theirs); what they
        # spent is already booked
        await db.execute(
            update(EvalMatch)
            .where(EvalMatch.tournament_id == t.id, EvalMatch.status == "failed",
                   or_(EvalMatch.round > accepted_rounds, EvalMatch.kind == "anchor"),
                   not_(EvalMatch.error.like(f"{TEXT_ERROR}%")))
            .values(status="pending", error=None, run_id=None, claimed_by=None, verdict=None, cost_usd=0)
        )
    await db.commit()
    await db.refresh(t)
    if spawn:
        _spawn(t.id, owner)
    return t


def required_gates(t: EvalTournament) -> list[str]:
    """What blocks resuming this tournament: the tripped pausing gates nobody acknowledged yet, and after
    a `jury_degraded` pause either `flip_rate` (one family left: continue on it alone) or `jury_restore`
    (none left: seat the original jurors again). The two are different situations and each needs its own
    acknowledgement; `jury_restore` is used up by the restore, so a later pause with no family left asks
    again. Empty unless the tournament is paused."""
    if t.status != "paused":
        return []
    diag = t.diagnostics or {}
    acked = t.acknowledged_gates or {}
    degraded = t.pause_reason == "jury_degraded"
    if degraded and diag.get("families_left") == 0:
        return [] if "jury_restore" in acked else ["jury_restore"]
    mine = dg.PAUSING_GATES + (("flip_rate",) if degraded else ())
    return [g["key"] for g in (diag.get("gates") or [])
            if g.get("state") == "tripped" and g["key"] in mine and g["key"] not in acked]


async def pause_tournament(
    tournament_id: uuid.UUID, *, owner: str | None = None, reason: str = "interrupted", session_factory: Any = None
) -> bool:
    """Pause exactly this tournament (never another) and release its lease. With `owner`, only if
    that runner still holds it."""
    factory = session_factory or get_session_factory()
    cond = [EvalTournament.id == tournament_id, EvalTournament.status == "running"]
    if owner:
        cond.append(EvalTournament.lease_owner == owner)
    async with factory() as db:
        res = await db.execute(
            update(EvalTournament).where(*cond)
            .values(status="paused", pause_reason=reason, lease_owner=None, lease_until=None, updated_at=_now())
        )
        await db.commit()
        return bool(res.rowcount)


async def recover_running(session_factory: Any = None) -> int:
    """At boot: a tournament left `running` whose lease has expired (its runner is gone) is paused,
    reason `restart`, never resumed on its own. One whose lease is still live belongs to a runner
    that may be alive (another process); it is left alone and becomes resumable when the lease ends."""
    factory = session_factory or get_session_factory()
    async with factory() as db:
        res = await db.execute(
            update(EvalTournament).where(_stale_running())
            .values(status="paused", pause_reason="restart", lease_owner=None, lease_until=None, updated_at=_now())
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        return int(res.rowcount or 0)


# ── pure pieces (tested directly) ──────────────────────────────────────────────────────────
def compute_scores(ids: list[str], rows: list[tuple[str, str, str | None, str, str]]) -> dict[str, float]:
    """Swiss scores of a pool: wins + 0.5 per tie, from the DONE primary and double matches
    (`rows`: a, b, verdict, kind, status). Swaps are a re-judgement of a primary and never count."""
    scores = {i: 0.0 for i in ids}
    for a, b, verdict, kind, status in rows:
        if kind in ("primary", "double") and status == "done" and verdict:
            s = score_for_a(verdict)
            scores[a] += s
            scores[b] += 1.0 - s
    return scores


def _fit_inputs(members: list[dict], matches: list[dict], fitted_at: str) -> tuple[list[dict], dict]:
    """(rating rows, summary). Pure and CPU-bound: run it in a thread."""
    pools: dict[str, list[dict]] = {}
    for m in members:
        pools.setdefault(m["pool_key"], []).append(m)
    pools = {k: sorted(v, key=lambda x: x["id"]) for k, v in pools.items() if len(v) >= 2}
    rows: list[dict] = []
    rated: dict[str, dict] = {}
    pool_rows: list[dict] = []
    for key, mem in sorted(pools.items()):
        ids = [m["id"] for m in mem]
        outcomes = [
            (x["a"], x["b"], score_for_a(x["verdict"]))
            for x in matches
            if x["pool_key"] == key and x["kind"] in ("primary", "double") and x["status"] == "done" and x["verdict"]
        ]
        ratings = fit_bradley_terry(ids, outcomes)
        band = bands(ratings)
        rated[key] = ratings
        by_id = {m["id"]: m for m in mem}
        for i in ids:
            r = ratings[i]
            kind, n = band.get(i, (None, None))
            rows.append({
                "pool_key": key, "member_id": i, "rating": r.rating, "se": r.se, "lo90": r.lo90, "hi90": r.hi90,
                "matches": r.matches, "wins": r.wins, "band_kind": kind, "band": n,
                "word_count": by_id[i]["word_count"], "claim_density": by_id[i]["claim_density"],
            })
        pool_rows.append({
            "key": key, "level": mem[0]["pool_level"], "n": len(ids), "rounds": rounds_for(len(ids)),
            "matches": sum(1 for x in matches if x["pool_key"] == key and x["kind"] in ("primary", "double") and x["status"] == "done"),
            "failed": sum(1 for x in matches if x["pool_key"] == key and x["status"] == "failed"),
        })
    by_match = {x["id"]: x for x in matches}
    flips: dict[str, list[tuple[str, str]]] = {}
    for x in matches:
        if x["kind"] == "swap" and x["status"] == "done" and x["verdict"]:
            o = by_match.get(x["swap_of"])
            if o is not None and o["status"] == "done" and o["verdict"]:
                flips.setdefault(x["family"] or "?", []).append((o["verdict"], x["verdict"]))
    all_pairs = [p for v in flips.values() for p in v]
    flip = {
        "overall": {"rate": flip_rate(all_pairs), "pairs": len(all_pairs)},
        "by_family": {f: {"rate": flip_rate(v), "pairs": len(v)} for f, v in sorted(flips.items())},
    }
    member = {m["id"]: m for m in members}

    def corr(attr: str) -> dict:
        def one(items: list[tuple[float, float]]) -> dict:
            xs, ys = [x for x, _ in items], [y for _, y in items]
            return {"pearson": pearson(xs, ys), "spearman": spearman(xs, ys), "n": len(items)}

        per_pool, everything = {}, []
        for key, ratings in rated.items():
            items = [(float(member[i][attr]), r.rating) for i, r in ratings.items() if member[i][attr] is not None]
            per_pool[key] = one(items)
            everything += items
        return {"overall": one(everything), "by_pool": per_pool}

    length, density = corr("word_count"), corr("claim_density")
    flags = []
    ov = length["overall"]
    if any(v is not None and v > LENGTH_R_FLAG for v in (ov["pearson"], ov["spearman"])):
        flags.append({"flag": "length_correlation_high", "detail": f"rating vs word count r > {LENGTH_R_FLAG}"})
    if flip["overall"]["rate"] is not None and flip["overall"]["rate"] > FLIP_RATE_FLAG:
        flags.append({"flag": "flip_rate_high", "detail": f"order-swap flip rate > {FLIP_RATE_FLAG}"})

    def count(k: str) -> int:
        return sum(1 for x in matches if x["kind"] == k and x["status"] == "done")

    summary = {
        "pools": pool_rows,
        "matches": {k: count(k) for k in ("primary", "swap", "double")} | {"failed": sum(1 for x in matches if x["status"] == "failed")},
        "flip_rate": flip,
        "length_r": length,
        "claim_density_r": density,
        "flags": flags,
        "fitted_at": fitted_at,
    }
    return rows, summary


async def compute_ratings(
    factory: Any, tid: uuid.UUID, *, finish: bool, owner: str | None = None, expect: tuple[str, Any] | None = None
) -> bool:
    """Fit every pool from the stored matches and write ratings and summary (`summary.partial` when the
    tournament is not completed: a refit of a paused, cancelled or failed one). The inputs are loaded
    and the session closed, the fits run in a thread (they are CPU-bound and would block the event
    loop), and the result is written in a fresh short transaction. With `finish` the tournament
    becomes `completed` in the same transaction only if it is still `running` (a cancel that arrived
    meanwhile wins, and so does a new lease holder: nothing is written); the freeze is released afterwards. False = not written."""
    async with factory() as db:
        t = await db.get(EvalTournament, tid)
        freeze = await db.get(RoutingFreeze, t.freeze_id) if t.freeze_id else None
        workspace_id, freeze_id, prompt_version = t.workspace_id, t.freeze_id, t.judge_prompt_version
        partial = not finish and t.status != "completed"  # ratings of an unfinished tournament
        members = [
            {"id": str(m.id), "pool_key": m.pool_key, "pool_level": m.pool_level, "word_count": m.word_count, "claim_density": m.claim_density}
            for m in await pl.members_of(db, t.snapshot_id)
        ]
        matches = [
            {"id": m.id, "pool_key": m.pool_key, "a": str(m.a_member_id), "b": str(m.b_member_id), "kind": m.kind,
             "status": m.status, "verdict": m.verdict, "swap_of": m.swap_of, "family": m.juror_family}
            for m in (await db.execute(select(EvalMatch).where(EvalMatch.tournament_id == tid, EvalMatch.kind != "anchor"))).scalars()
        ]
        picks = list((freeze.selection or {}).get("picks") or []) if freeze else []
    rows, summary = await asyncio.to_thread(_fit_inputs, members, matches, _now().isoformat())
    summary["jury"] = [{"model": p["model"], "family": p["family"], "rank": p["rank"]} for p in picks]
    summary["judge_prompt_version"] = prompt_version
    summary["partial"] = partial
    async with factory() as db:
        if finish:
            res = await db.execute(
                update(EvalTournament)
                .where(EvalTournament.id == tid, EvalTournament.status == "running",
                       *([EvalTournament.lease_owner == owner] if owner else []))
                .values(status="completed", pause_reason=None, lease_owner=None, lease_until=None, summary=summary,
                        updated_at=_now())
            )
            if res.rowcount != 1:
                await db.rollback()
                return False
        else:
            # a refit writes only if the tournament is exactly as it was read (status, updated_at) and no runner holds it
            guard = [EvalTournament.id == tid]
            if expect is not None:
                guard += [EvalTournament.status == expect[0], EvalTournament.updated_at == expect[1], EvalTournament.lease_owner.is_(None)]
            res = await db.execute(update(EvalTournament).where(*guard).values(summary=summary, updated_at=_now())
                                   .execution_options(synchronize_session=False))
            if res.rowcount != 1:
                await db.rollback()
                return False
        await db.execute(delete(ModuleRating).where(ModuleRating.tournament_id == tid))
        db.add_all([ModuleRating(tournament_id=tid, member_id=uuid.UUID(r.pop("member_id")), **r) for r in rows])
        if finish and freeze_id:
            with contextlib.suppress(jury.JuryError):
                await jury.release_freeze(db, workspace_id=workspace_id, freeze_id=freeze_id)
        await db.commit()
    return True


async def refit_tournament(db: AsyncSession, *, workspace_id: uuid.UUID, tournament_id: uuid.UUID) -> EvalTournament:
    """Recompute ratings and summary from the stored matches, for a completed or failed tournament.
    No model call and no spend; the status does not change."""
    t = await get_tournament(db, workspace_id=workspace_id, tournament_id=tournament_id)
    if t.status not in REFITTABLE:
        raise TournamentError(f"Tournament is {t.status}; only a completed, failed, paused or cancelled one can be refitted", 409)
    lease = _aware(t.lease_until)
    if t.lease_owner and lease is not None and lease > _now():
        raise TournamentError("A runner holds this tournament's lease; refit it when it has stopped", 409)
    done = (await db.execute(
        select(func.count()).select_from(EvalMatch).where(
            EvalMatch.tournament_id == t.id, EvalMatch.status == "done", EvalMatch.kind != "anchor")  # anchors are never rated
    )).scalar_one()
    if not done:
        raise TournamentError("There are no completed matches to fit: nothing to fit", 409)
    expect = (t.status, t.updated_at)
    await db.commit()  # no transaction open while the fit runs
    if not await compute_ratings(get_session_factory(), t.id, finish=False, expect=expect):
        await db.rollback()
        raise TournamentError("The tournament changed while it was refitted (it was resumed or finished); nothing was written, try again", 409)
    await db.refresh(t)
    return t


# ── the runner ───────────────────────────────────────────────────────────────────────────────
_sem_state: dict[str, Any] = {"loop": None, "size": 0, "sem": None}


def _semaphore() -> asyncio.Semaphore:
    """One limit for the whole process, shared by every tournament: the machine is small."""
    loop = asyncio.get_running_loop()
    size = max(1, int(get_settings().eval_tournament_concurrency))
    st = _sem_state
    if st["loop"] is not loop or st["size"] != size:
        st.update(loop=loop, size=size, sem=asyncio.Semaphore(size))
    return st["sem"]


def _side(row: EvalMatch, side: str) -> str:
    """The key of a match's text in the runner's member table: the pool member, or for an anchor
    match the anchor pair's side."""
    if row.kind == "anchor":
        return f"anchor:{row.anchor_index}:{side}"
    return str(row.a_member_id if side == "a" else row.b_member_id)


class _TextError(Exception):
    """A member's text cannot be used as snapshotted (missing, changed or unhashed)."""


class _Juror:
    """The `profile` `execute_direct` runs: one frozen-model call over two masked texts."""

    direct = True

    def __init__(self, *, module: Module, text_1: str, text_2: str, freeze: Any, freeze_id: uuid.UUID, pass_ref: str,
                 juror_index: int, model: str) -> None:
        self.module, self.t1, self.t2 = module, text_1, text_2
        self.freeze, self.freeze_id, self.pass_ref = freeze, freeze_id, pass_ref
        self.juror_index, self.model = juror_index, model
        self.payload: dict | None = None
        self.answered_by: str | None = None

    async def run_direct(self, ctx):
        system, user = build_juror_messages(self.module, self.t1, self.t2)
        est = (len(system) + len(user)) // 3
        res = await ctx.call_json(
            purpose="eval_juror", system=system, prompt=user, schema=JUROR_SCHEMA, est_input_tokens=est,
            max_output_tokens=MAX_OUTPUT_TOKENS, freeze=self.freeze, freeze_id=self.freeze_id, pass_ref=self.pass_ref,
            juror_index=self.juror_index, model=self.model,
        )
        self.payload, self.answered_by = res.payload, res.model
        return direct.DirectResult(outcome={"verdict": res.payload.get("verdict")})


class _Runner:
    def __init__(self, tid: uuid.UUID, owner: str | None = None) -> None:
        self.tid = tid
        self.owner = owner or new_owner()
        self.factory = get_session_factory()
        self.spent = Decimal(0)
        self.reserved = Decimal(0)
        self.budget: Decimal | None = None
        self.budget_hit = False
        self.stopped = False  # cancelled, or the lease was lost
        self.pause_reason: str | None = None
        self.observed: dict[str, tuple[Decimal, int]] = {}  # frozen model -> (total cost, matches)
        self.recent: deque[bool] = deque(maxlen=FAIL_WINDOW)  # did the last matches that ran succeed
        self.texts: OrderedDict[str, str] = OrderedDict()

    # -- lease -------------------------------------------------------------------------------
    def _lease_ok(self):
        """True in SQL only while this runner still holds a running tournament: put it in the WHERE of
        every write that must not outlive the lease, so the check and the write are one statement."""
        return exists().where(
            EvalTournament.id == self.tid, EvalTournament.lease_owner == self.owner, EvalTournament.status == "running"
        )

    async def _claim(self) -> bool:
        async with self.factory() as db:
            res = await db.execute(
                update(EvalTournament)
                .where(
                    EvalTournament.id == self.tid, EvalTournament.status == "running",
                    or_(EvalTournament.lease_owner == self.owner, EvalTournament.lease_owner.is_(None),
                        EvalTournament.lease_until.is_(None), EvalTournament.lease_until < _now()),
                )
                .values(lease_owner=self.owner, lease_until=_lease_end())
                .execution_options(synchronize_session=False)
            )
            await db.commit()
            return res.rowcount == 1

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            try:
                async with self.factory() as db:
                    res = await db.execute(
                        update(EvalTournament)
                        .where(EvalTournament.id == self.tid, EvalTournament.lease_owner == self.owner, EvalTournament.status == "running")
                        .values(lease_until=_lease_end())
                    )
                    await db.commit()
                if res.rowcount != 1:
                    self.stopped = True  # someone else owns it now, or it was cancelled
                    return
            except Exception:  # noqa: BLE001 - one missed beat is not a lost lease
                log.warning("tournament %s heartbeat failed", self.tid, exc_info=True)

    async def _release_lease(self) -> None:
        with contextlib.suppress(Exception):
            async with self.factory() as db:
                await db.execute(
                    update(EvalTournament).where(EvalTournament.id == self.tid, EvalTournament.lease_owner == self.owner)
                    .values(lease_owner=None, lease_until=None)
                )
                await db.commit()

    # -- setup -------------------------------------------------------------------------------
    async def _load(self) -> bool:
        async with self.factory() as db:
            t = await db.get(EvalTournament, self.tid)
            if t is None or t.status != "running" or t.freeze_id is None:
                return False
            freeze = await db.get(RoutingFreeze, t.freeze_id)
            self.sel = jury.selection_of(freeze)
            self.freeze_id = freeze.id
            self.pass_ref = freeze.pass_ref
            self.workspace_id = t.workspace_id
            self.seed = t.seed
            self.policy = dict(t.model_policy or {"mode": "auto"})
            self.swap_rate, self.double_rate = t.swap_rate, t.double_rate
            self.dropped: set[str] = set(t.dropped_families or [])
            self.acked: dict[str, dict] = dict(t.acknowledged_gates or {})
            self.accepted = int(t.current_round or 0)  # rounds whose gates were already evaluated
            self.diag: dict[str, Any] = dict(t.diagnostics or {})
            self.history: list[dict] = list(self.diag.get("history") or [])
            self.anchor_set_id: str | None = self.diag.get("anchor_set_id")
            self.notes: list[str] = list(self.diag.get("notes") or [])  # what the runner did about the jury, for the operator
            self.restores: int = int(self.diag.get("restores") or 0)
            self.restore_round: int | None = self.diag.get("restore_round")
            self.families_left: int | None = self.diag.get("families_left")
            self.module_set = t.module_set
            self.spent = Decimal(t.spent_usd or 0)
            self.budget = Decimal(t.budget_usd) if t.budget_usd is not None else None
            self.module = Module.model_validate(t.module_def)  # as it was when the tournament was created
            snap = await db.get(PeerPoolSnapshot, t.snapshot_id)
            self.members = {
                str(m.id): SimpleNamespace(
                    id=str(m.id), pool_key=m.pool_key, text_key=m.text_key, sha=m.text_sha256, words=int(m.word_count or 0),
                    names=list(m.mask_names or []), domains=list(m.mask_domains or []),
                )
                for m in await pl.members_of(db, t.snapshot_id)
            }
            self.pools: dict[str, list[str]] = {}
            for m in self.members.values():
                self.pools.setdefault(m.pool_key, []).append(m.id)
            self.pools = {k: sorted(v) for k, v in self.pools.items() if len(v) >= 2}
            self.project_id = snap.project_id or (await db.execute(
                select(Project.id).where(Project.workspace_id == t.workspace_id).limit(1))).scalar_one()
            for model, total, n in (await db.execute(
                select(EvalMatch.juror_model, func.sum(EvalMatch.cost_usd), func.count()).where(
                    EvalMatch.tournament_id == self.tid, EvalMatch.status == "done").group_by(EvalMatch.juror_model)
            )).all():  # what matches have cost so far (a resume starts from it)
                self.observed[model] = (Decimal(str(total or 0)), int(n))
            self.harness_id = (await ensure_jury_harness(db, t.workspace_id)).id
            self.catalog = (await direct.get_call_services(db, t.workspace_id)).catalog  # the one the calls use
            await db.commit()
        return True

    async def _book(self, db: AsyncSession, run: Run | None, match_id: uuid.UUID | None) -> Decimal:
        """Add a finished run's cost to the tournament's spend, exactly once per run id (the ledger's
        primary key): a second settlement, a recovery or a takeover that sees the same run books nothing.
        Returns what this call booked. Runs in the caller's transaction."""
        if run is None:
            return Decimal(0)
        cost = Decimal(run.reported_cost_usd if run.reported_cost_usd is not None else (run.cost_usd or 0))
        try:
            async with db.begin_nested():
                db.add(EvalMatchRun(run_id=run.id, tournament_id=self.tid, match_id=match_id, cost_usd=cost))
                await db.flush()
        except IntegrityError:
            return Decimal(0)
        if cost:
            await db.execute(update(EvalTournament).where(EvalTournament.id == self.tid).values(spent_usd=EvalTournament.spent_usd + cost))
        return cost

    async def _recover_orphans(self) -> None:
        """Matches a dead (or replaced) runner left claimed or half played: a match whose run is
        still in flight is left alone (never played twice); otherwise its run's spend is booked (once,
        by run id) and the match goes back to pending. A run is taken as dead when it has not finished
        within `RUN_STALE_SECONDS`."""
        async with self.factory() as db:
            rows = list((await db.execute(
                select(EvalMatch).where(
                    EvalMatch.tournament_id == self.tid,
                    or_(EvalMatch.status == "running", and_(EvalMatch.status == "pending", EvalMatch.run_id.is_not(None))),
                )
            )).scalars())
            booked = Decimal(0)
            for m in rows:
                run = await db.get(Run, m.run_id) if m.run_id is not None else None
                if run is not None and run.status in ("queued", "running"):
                    created = _aware(run.created_at)
                    if created is None or (_now() - created).total_seconds() < RUN_STALE_SECONDS:
                        continue  # in flight: wait for it
                booked += await self._book(db, run, m.id)
                m.status, m.run_id, m.claimed_by = "pending", None, None
            await db.commit()
            self.spent += booked

    # -- state ---------------------------------------------------------------------------------
    async def _status(self) -> str | None:
        async with self.factory() as db:
            return (await db.execute(select(EvalTournament.status).where(EvalTournament.id == self.tid))).scalar_one_or_none()

    async def _set(self, **values: Any) -> bool:
        """Write to the tournament only while it is still running and still ours (a cancel or a new
        lease holder stands). False stops this runner."""
        async with self.factory() as db:
            res = await db.execute(
                update(EvalTournament)
                .where(EvalTournament.id == self.tid, EvalTournament.status == "running", EvalTournament.lease_owner == self.owner)
                .values(updated_at=_now(), **values)
            )
            await db.commit()
        if res.rowcount != 1:
            self.stopped = True
        return res.rowcount == 1

    async def _pause(self, reason: str) -> bool:
        return await self._set(status="paused", pause_reason=reason[:500], lease_owner=None, lease_until=None)

    def _add_note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)

    def _extra(self) -> dict[str, Any]:
        return {"notes": self.notes, "restores": self.restores, "restore_round": self.restore_round,
                "families_left": self.families_left}

    async def _restore_jury(self) -> bool:
        """Every family was dropped and the resume acknowledged `jury_restore`: the original frozen jurors
        sit again (the alternative is a pass nobody can finish). The acknowledgement is used up (kept in
        the notes, removed from `acknowledged_gates`), so a later pause with no family left needs a new
        one. Flip drops stay ON: the restored jurors are judged on the swaps they play from now on
        (`restore_round`) and are dropped, and the pass paused, again if they flip. False = stop."""
        families = {p.family for p in self.sel.picks}
        if not families or self.dropped < families or "jury_restore" not in self.acked:
            return True
        ack = self.acked.pop("jury_restore")
        async with self.factory() as db:
            last = (await db.execute(
                select(func.max(EvalMatch.round)).where(EvalMatch.tournament_id == self.tid, EvalMatch.kind != "anchor")
            )).scalar_one()
        self.dropped, self.restores, self.restore_round, self.families_left = set(), self.restores + 1, int(last or 0), len(families)
        self._add_note(f"jury restored (#{self.restores}) after round {self.restore_round}: every family had been dropped, and "
                       f"{ack.get('by') or 'an approver'} acknowledged it ({ack.get('note') or 'no note'})")
        self.diag = {**self.diag, **self._extra()}
        return await self._set(diagnostics=self.diag, dropped_families=[], acknowledged_gates=self.acked)

    def _active(self) -> list[int]:
        """Indexes of the frozen jurors still on the jury (a dropped family is skipped)."""
        live = [i for i, p in enumerate(self.sel.picks) if p.family not in self.dropped]
        return live or list(range(len(self.sel.picks)))

    # -- diagnostics -----------------------------------------------------------------------------
    async def _anchor_phase(self) -> bool:
        """Judge the anchor set first, if a ready one exists for this module, and stop on drift.
        The set is chosen once and recorded (`diagnostics.anchor_set_id`, null = none), so a resume
        never picks another. Idempotent: matches that exist are not made again. False = stop."""
        if self.stopped or await self._status() != "running":
            return False
        if "anchor_set_id" not in self.diag:
            async with self.factory() as db:
                found = (await db.execute(
                    select(EvalAnchorSet.id).where(
                        EvalAnchorSet.workspace_id == self.workspace_id, EvalAnchorSet.module == self.module.slug,
                        EvalAnchorSet.module_set == self.module_set, EvalAnchorSet.status == "ready",
                    ).order_by(EvalAnchorSet.created_at.desc(), EvalAnchorSet.id).limit(1)
                )).scalar_one_or_none()
            self.anchor_set_id = str(found) if found else None
            self.diag = {**self.diag, "anchor_set_id": self.anchor_set_id}
            if not await self._set(diagnostics=self.diag):
                return False
        if self.anchor_set_id is None:
            return True
        async with self.factory() as db:
            aset = await db.get(EvalAnchorSet, uuid.UUID(self.anchor_set_id))
            pairs = [p for p in (aset.pairs or []) if p.get("baseline")] if aset else []
            existing = (await db.execute(
                select(func.count()).select_from(EvalMatch).where(EvalMatch.tournament_id == self.tid, EvalMatch.kind == "anchor")
            )).scalar_one()
            if aset is not None and not existing:
                for p in pairs:
                    for idx, pick in enumerate(self.sel.picks):
                        await self._add(db, EvalMatch(
                            tournament_id=self.tid, pool_key="anchor", round=0, anchor_set_id=aset.id, anchor_index=int(p["index"]),
                            shown_a_first=random.Random(f"{self.seed}:anchor:{p['index']}:{idx}").random() < 0.5,
                            juror_index=idx, juror_family=pick.family, juror_model=pick.model, kind="anchor", status="pending",
                        ))
                await db.commit()
        for p in pairs:
            for side in ("a", "b"):
                x = p[side]
                self.members[f"anchor:{p['index']}:{side}"] = SimpleNamespace(
                    id=f"anchor:{p['index']}:{side}", pool_key="anchor", text_key=x["text_key"], sha=x["text_sha256"],
                    words=int(x.get("word_count") or 0), names=list(x.get("names") or []), domains=list(x.get("domains") or []),
                )
        if not await self._play_pending(0, "anchor"):
            return False
        async with self.factory() as db:
            done = [{"anchor_index": m.anchor_index, "status": m.status, "verdict": m.verdict} for m in (await db.execute(
                select(EvalMatch).where(EvalMatch.tournament_id == self.tid, EvalMatch.kind == "anchor"))).scalars()]
        view = dg.anchor_view(self.anchor_set_id, pairs, done)
        th = dg.thresholds()
        empty = {"overall": {"n": 0, "spearman": None}}
        fresh = {g["key"]: g for g in dg.build_gates(empty, [], [], view, th, self.acked)}
        drift = fresh["anchor_drift"]
        # a resume has other gates from the rounds already played: only the anchor entry is replaced
        kept = {g["key"]: g for g in (self.diag.get("gates") or [])} or fresh
        kept["anchor_drift"] = drift
        gates = [kept[k] for k in dg.GATE_KEYS if k in kept]
        block = self.diag.get("block")
        if block:
            block = {**block, "gates": gates, "anchors": view}
        self.diag = {**self.diag, "anchors": view, "gates": gates, "computed_at": _now().isoformat(), **({"block": block} if block else {})}
        if not await self._set(diagnostics=self.diag):
            return False
        if drift["state"] == "tripped" and "anchor_drift" not in self.acked:
            await self._pause("anchor_drift")
            return False
        return True

    async def _diagnose(self, r: int) -> bool:
        """After round `r`: provisional ratings and the gates. Drops a flip-prone family, stores the
        gates and the round's history entry, and pauses on a tripped gate nobody acknowledged. False = stop."""
        async with self.factory() as db:
            inputs = await dg.load_inputs(db, self.tid)
        inputs.update(dropped=sorted(self.dropped), acknowledged=self.acked)
        block = await asyncio.to_thread(dg.compute_from_inputs, inputs)  # CPU-bound, no session open
        th = block["thresholds"]
        families = {p.family for p in self.sel.picks}
        newly = [
            f["family"] for f in block["flips_gate"]
            if f["family"] in families and f["family"] not in self.dropped and f["swaps"] >= th["min_swaps"]
            and f["rate"] is not None and f["rate"] > th["flip_drop"]
        ]
        if newly:
            self.dropped |= set(newly)
            for f in block["flip_rates"] + block["flips_gate"]:
                f["dropped"] = f["family"] in self.dropped
            block["gates"] = dg.build_gates(block["length_bias"], block["flips_gate"], block["agreement"], block["anchors"], th,
                                            self.acked, block["pooled_kappa"])
        self.history = dg.merge_history(self.history, dg.history_entry(r, block))
        if len(self._active()) < 2 and len(families) >= 2:
            self._add_note("doubles skipped: fewer than two juror families on the jury, so no other family can re-judge a match")
        self.families_left = len(families - self.dropped)
        self.diag = dg.stored_block(block, self.history, self.anchor_set_id, self._extra())
        if not await self._set(diagnostics=self.diag, dropped_families=sorted(self.dropped)):
            return False
        state = {g["key"]: g["state"] for g in block["gates"]}
        for key in ("length_bias", "position_bias", "juror_agreement"):
            if state[key] == "tripped" and key not in self.acked:
                await self._pause(dg.PAUSE_FOR_GATE[key])
                return False
        left = self.families_left
        if self.dropped and (left == 0 or (left < dg.MIN_JURY_FAMILIES and "flip_rate" not in self.acked)):
            await self._pause("jury_degraded")
            return False
        return True

    # -- rounds ----------------------------------------------------------------------------------
    async def go(self) -> None:
        if not await self._claim():
            return  # another runner holds the lease (or it is no longer running)
        beat = asyncio.create_task(self._heartbeat())
        try:
            await self._go()
        except asyncio.CancelledError:
            # interrupted (Ctrl-C, shutdown, a newer runner replacing this task): leave the
            # tournament paused, not "running" with nobody playing it; only if the lease is still ours
            await asyncio.shield(self._pause("interrupted"))
            raise
        finally:
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beat
            await asyncio.shield(self._release_lease())

    async def _go(self) -> None:
        if not await self._load():
            return
        await self._recover_orphans()
        if not await self._restore_jury():
            return
        if not await self._anchor_phase():
            return
        max_rounds = max((rounds_for(len(v)) for v in self.pools.values()), default=0)
        for r in range(1, max_rounds + 1):
            if self.stopped or await self._status() != "running":
                return
            await self._make_primaries(r)
            if not await self._play_pending(r, "primary"):
                return
            await self._make_extras(r)
            if not await self._play_pending(r, "extra"):
                return
            if not await self._round_ok(r):
                return
            if r > self.accepted and not await self._diagnose(r):  # a replayed, already accepted round is not judged twice
                return
            if not await self._set(current_round=r):
                return
        if not self.stopped:
            await compute_ratings(self.factory, self.tid, finish=True, owner=self.owner)

    async def _add(self, db: AsyncSession, row: EvalMatch) -> None:
        """Insert a match unless an identical one exists (a racing runner made it): never an error."""
        try:
            async with db.begin_nested():
                db.add(row)
                await db.flush()
        except IntegrityError:
            pass

    async def _make_primaries(self, r: int) -> None:
        async with self.factory() as db:
            # The juror rotation is a pure function of the plan (rounds before this one, then this
            # round's pools in order), never of what is in the table, so two runners that pair the
            # same round produce identical rows and the unique key drops the second set.
            counter = sum(len(v) // 2 for rr in range(1, r) for v in self.pools.values() if rounds_for(len(v)) >= rr)
            for key in sorted(self.pools):
                ids = self.pools[key]
                if r > rounds_for(len(ids)):
                    continue
                rows = list((await db.execute(
                    select(EvalMatch).where(EvalMatch.tournament_id == self.tid, EvalMatch.pool_key == key,
                                            EvalMatch.kind.in_(("primary", "double")))
                )).scalars())
                if any(m.kind == "primary" and m.round == r for m in rows):
                    counter += len(ids) // 2
                    continue  # this round's pairing already exists (a resume): never re-pair
                scores = compute_scores(ids, [(str(m.a_member_id), str(m.b_member_id), m.verdict, m.kind, m.status) for m in rows])
                # a pair is played unless its match failed for a reason a replay could fix; one whose text
                # can never be read is as played as it will ever be
                played = {
                    frozenset((str(m.a_member_id), str(m.b_member_id))) for m in rows
                    if m.kind == "primary" and (m.status != "failed" or (m.error or "").startswith(TEXT_ERROR))
                }
                byes: set[str] = set()
                for prev in range(1, r):
                    in_prev = {str(x) for m in rows if m.kind == "primary" and m.round == prev for x in (m.a_member_id, m.b_member_id)}
                    if in_prev:
                        byes |= set(ids) - in_prev
                rng = random.Random(f"{self.seed}:{key}:{r}:pairs")
                pairs, _bye = swiss_round(ids, scores, played, rng, byes=byes)
                shown_rng = random.Random(f"{self.seed}:{key}:{r}:shown")
                active = self._active()
                for a, b in pairs:
                    idx = active[counter % len(active)]  # dropped families are skipped; same plan, same rows
                    counter += 1
                    pick = self.sel.picks[idx]
                    await self._add(db, EvalMatch(
                        tournament_id=self.tid, pool_key=key, round=r, a_member_id=uuid.UUID(a), b_member_id=uuid.UUID(b),
                        shown_a_first=shown_rng.random() < 0.5, juror_index=idx, juror_family=pick.family,
                        juror_model=pick.model, kind="primary", status="pending",
                    ))
            await db.commit()

    async def _make_extras(self, r: int) -> None:
        async with self.factory() as db:
            existing = (await db.execute(
                select(func.count()).select_from(EvalMatch).where(
                    EvalMatch.tournament_id == self.tid, EvalMatch.round == r, EvalMatch.kind.in_(("swap", "double")))
            )).scalar_one()
            if existing:
                return
            done = list((await db.execute(
                select(EvalMatch).where(EvalMatch.tournament_id == self.tid, EvalMatch.round == r,
                                        EvalMatch.kind == "primary", EvalMatch.status == "done").order_by(EvalMatch.id)
            )).scalars())
            by_id = {str(m.id): m for m in done}
            ids = sorted(by_id)
            for kind, rate in (("swap", self.swap_rate), ("double", self.double_rate)):
                if kind == "double" and len(self._active()) < 2:
                    continue
                rng = random.Random(f"{self.seed}:{r}:{kind}")
                for mid in sorted(choose_extra(ids, rate, rng)):
                    o = by_id[mid]
                    if kind == "swap":
                        idx, shown = o.juror_index, not o.shown_a_first
                    else:
                        live = self._active()
                        others = [i for i in live if self.sel.picks[i].family != o.juror_family] or [
                            i for i in live if i != o.juror_index
                        ]
                        if not others:
                            continue
                        idx, shown = rng.choice(others), rng.random() < 0.5
                    pick = self.sel.picks[idx]
                    await self._add(db, EvalMatch(
                        tournament_id=self.tid, pool_key=o.pool_key, round=r, a_member_id=o.a_member_id, b_member_id=o.b_member_id,
                        shown_a_first=shown, juror_index=idx, juror_family=pick.family, juror_model=pick.model, kind=kind,
                        swap_of=o.id if kind == "swap" else None, double_of=o.id if kind == "double" else None, status="pending",
                    ))
            await db.commit()

    async def _play_pending(self, r: int, group: str) -> bool:
        """Play this round's pending matches, and wait for any a replaced runner's run still has in
        flight. False = the runner must stop (paused, cancelled)."""
        kinds = {"primary": ("primary",), "anchor": ("anchor",)}.get(group, ("swap", "double"))
        deadline: float | None = None
        loop = asyncio.get_running_loop()
        while True:
            async with self.factory() as db:
                ids = list((await db.execute(
                    select(EvalMatch.id).where(EvalMatch.tournament_id == self.tid, EvalMatch.round == r,
                                               EvalMatch.kind.in_(kinds), EvalMatch.status == "pending").order_by(EvalMatch.created_at, EvalMatch.id)
                )).scalars())
                claimed = (await db.execute(
                    select(func.count()).select_from(EvalMatch).where(
                        EvalMatch.tournament_id == self.tid, EvalMatch.round == r, EvalMatch.kind.in_(kinds), EvalMatch.status == "running")
                )).scalar_one()
            if ids:
                results = await asyncio.gather(*(self._play(i) for i in ids), return_exceptions=True)
                for res in results:
                    if isinstance(res, BaseException) and not isinstance(res, asyncio.CancelledError):
                        raise res
            if self.pause_reason or self.budget_hit or self.stopped or not claimed:
                break
            # matches claimed by someone else whose run is still in flight: wait, then look again
            deadline = deadline or loop.time() + RUN_STALE_SECONDS + 30
            if loop.time() > deadline:
                self.pause_reason = "error: matches claimed by another runner never finished"
                break
            await asyncio.sleep(ORPHAN_POLL_SECONDS)
            await self._recover_orphans()
        reason = self.pause_reason or ("budget" if self.budget_hit else None)
        if reason:
            await self._pause(reason)
            return False
        return not self.stopped and (await self._status()) == "running"

    async def _round_ok(self, r: int) -> bool:
        async with self.factory() as db:
            rows = (await db.execute(
                select(EvalMatch.status, func.count()).where(
                    EvalMatch.tournament_id == self.tid, EvalMatch.round == r,
                    not_(and_(EvalMatch.status == "failed", EvalMatch.error.like(f"{TEXT_ERROR}%"))),  # unusable text is not the jury's fault
                ).group_by(EvalMatch.status)
            )).all()
        counts = {s: n for s, n in rows}
        total = sum(counts.values())
        if total and counts.get("failed", 0) / total > MAX_ROUND_FAILURE_RATE:
            await self._pause("match_failures")
            return False
        return True

    # -- one match -------------------------------------------------------------------------------
    async def _text(self, member_id: str) -> str:
        """The member's snapshotted text, verified against its hash. Bounded LRU. `_TextError` = it can
        never be read as snapshotted; anything else (the blob store, the network) is transient."""
        if member_id in self.texts:
            self.texts.move_to_end(member_id)
            return self.texts[member_id]
        from tret.storage import get_store

        m = self.members[member_id]
        if not m.text_key or not m.sha:
            raise _TextError("the snapshot has no text hash for this module")
        try:
            data = await get_store().get(m.text_key)
        except FileNotFoundError:
            raise _TextError(f"text blob {m.text_key} is missing") from None
        if hashlib.sha256(data).hexdigest() != m.sha:
            raise _TextError(f"text blob {m.text_key} no longer matches the snapshot hash")
        text = data.decode("utf-8", errors="replace")
        self.texts[member_id] = text
        while len(self.texts) > TEXT_CACHE_SIZE:
            self.texts.popitem(last=False)
        return text

    def _estimate(self, m: Any) -> Decimal:
        """What this match is expected to cost, to keep the budget a stop and not a suggestion:
        the catalog price of the frozen model for these two texts, or what matches have actually
        cost so far (this model's average, else the dearest average seen), whichever is higher. Only
        the first matches in flight, before anything has been observed, can overshoot."""
        info = self.catalog.get(m.juror_model)
        est = Decimal(0)
        if info is not None:
            tin, tout = match_tokens(self.members[m.a].words, self.members[m.b].words)
            est = Decimal(str(info.cost_usd(tin, tout)))
        seen = self.observed.get(m.juror_model)
        if seen:
            est = max(est, seen[0] / seen[1])
        elif self.observed:
            est = max(est, max(c / n for c, n in self.observed.values()))
        return est

    def _note(self, ok: bool) -> None:
        """Watch the last matches that ran: too many failures for model reasons pause the tournament."""
        self.recent.append(ok)
        if len(self.recent) >= FAIL_MIN and self.recent.count(False) / len(self.recent) > MAX_ROUND_FAILURE_RATE:
            self.pause_reason = self.pause_reason or "juror_failures"

    async def _play(self, match_id: uuid.UUID) -> None:
        async with _semaphore():
            if self.stopped or self.budget_hit or self.pause_reason:
                return
            if (await self._status()) != "running":
                self.stopped = True
                return
            async with self.factory() as db:
                row = await db.get(EvalMatch, match_id)
                if row is None or row.status != "pending":
                    return
                m = SimpleNamespace(
                    id=row.id, a=_side(row, "a"), b=_side(row, "b"), shown_a_first=row.shown_a_first,
                    juror_index=row.juror_index, juror_model=row.juror_model,
                )
                est = self._estimate(m)
                if self.budget is not None and self.spent + self.reserved + est > self.budget:
                    self.budget_hit = True
                    return
                self.reserved += est  # reserved before any await, so concurrent matches cannot all slip under the budget
            try:
                async with self.factory() as db:
                    # one statement: still pending AND this runner still holds the running tournament
                    claim = await db.execute(
                        update(EvalMatch)
                        .where(EvalMatch.id == match_id, EvalMatch.status == "pending", self._lease_ok())
                        .values(status="running", claimed_by=self.owner)
                    )
                    await db.commit()
                if claim.rowcount != 1:
                    if not await self._holds_lease():
                        self.stopped = True
                    return  # another runner has it, or the lease is gone
                try:
                    await self._play_claimed(m)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001 - a transient database or blob-store error, not the jury
                    log.warning("tournament %s match %s: %s", self.tid, match_id, e, exc_info=True)
                    await self._requeue(match_id, f"{type(e).__name__}: {e}")
            finally:
                self.reserved -= est

    async def _holds_lease(self) -> bool:
        async with self.factory() as db:
            return bool((await db.execute(
                select(func.count()).select_from(EvalTournament).where(
                    EvalTournament.id == self.tid, EvalTournament.lease_owner == self.owner, EvalTournament.status == "running")
            )).scalar_one())

    async def _requeue(self, match_id: uuid.UUID, message: str) -> None:
        """Put a match whose play was cut short by a transient error back to pending (booking what its
        run spent, once) and pause the tournament with the error; resume replays it."""
        try:
            async with self.factory() as db:
                m = await db.get(EvalMatch, match_id)
                booked = Decimal(0)
                if m is not None and m.run_id is not None:
                    booked = await self._book(db, await db.get(Run, m.run_id), m.id)
                await db.execute(
                    update(EvalMatch).where(EvalMatch.id == match_id, EvalMatch.claimed_by == self.owner, EvalMatch.status == "running")
                    .values(status="pending", run_id=None, claimed_by=None)
                )
                await db.commit()
                self.spent += booked
        except Exception:  # noqa: BLE001 - the next runner's orphan recovery will replay it
            log.warning("tournament %s could not requeue match %s", self.tid, match_id, exc_info=True)
        self.pause_reason = self.pause_reason or f"error: {message}"[:500]

    async def _play_claimed(self, m: Any) -> None:
        try:
            ta, tb = await self._text(m.a), await self._text(m.b)
        except _TextError as e:
            await self._settle(m.id, None, None, f"{TEXT_ERROR} {e}")
            return
        a, b = self.members[m.a], self.members[m.b]
        names, domains = sorted(set(a.names + b.names)), sorted(set(a.domains + b.domains))
        ta, ca = mask_names_with_count(ta, names, domains)
        tb, cb = mask_names_with_count(tb, names, domains)
        ta, tb = defang(ta), defang(tb)
        t1, t2 = (ta, tb) if m.shown_a_first else (tb, ta)
        run_id = uuid.uuid4()
        async with self.factory() as db:  # a short transaction: the session is closed before the call
            db.add(Run(
                id=run_id, project_id=self.project_id, harness_id=self.harness_id, task_type=direct.TASK_TYPE,
                task_input={JUROR_KEY: True, STRUCTURED_CALL_KEY: True, EVAL_TOURNAMENT_KEY: str(self.tid), EVAL_MATCH_KEY: str(m.id)},
                document_ids=[],
            ))
            await db.flush()
            res = await db.execute(
                update(EvalMatch)
                .where(EvalMatch.id == m.id, EvalMatch.claimed_by == self.owner, EvalMatch.status == "running", self._lease_ok())
                .values(run_id=run_id, mask_count_a=ca, mask_count_b=cb)
            )
            if res.rowcount != 1:  # replaced meanwhile: spend nothing
                await db.rollback()
                self.stopped = True
                return
            await db.commit()
        profile = _Juror(
            module=self.module, text_1=t1, text_2=t2, freeze=self.sel, freeze_id=self.freeze_id, pass_ref=self.pass_ref,
            juror_index=m.juror_index, model=m.juror_model,
        )
        await direct.execute_direct(run_id, profile, self.factory, model_policy=self.policy)
        await self._settle(m.id, run_id, profile, None)

    async def _settle(self, match_id: uuid.UUID, run_id: uuid.UUID | None, profile: _Juror | None, error: str | None) -> None:
        """Record a match's result in ONE conditional statement (still claimed by this runner, lease
        still ours). The run's cost is booked either way, once, by run id: a runner that lost its lease
        mid-call still pays for the call in the accounts, and stops."""
        outage: str | None = None
        ok = False
        values: dict[str, Any] = {}
        async with self.factory() as db:
            m = await db.get(EvalMatch, match_id)
            run = None
            if run_id is not None:
                run = (await db.execute(select(Run).where(Run.id == run_id).execution_options(populate_existing=True))).scalar_one()
                cost = Decimal(run.reported_cost_usd if run.reported_cost_usd is not None else (run.cost_usd or 0))
                values.update(cost_usd=cost, input_tokens=run.input_tokens or 0, output_tokens=run.output_tokens or 0)
                verdict = strength = rationale = None
                if run.status != "completed":
                    if run.started_at is None:  # the pre-run gate refused: credits or a limit
                        outage = "credits"
                    elif (run.error or "").split(":", 1)[0] in OUTAGE_KINDS:
                        outage = "juror_unavailable"
                if run.status != "completed" or profile is None or profile.payload is None:
                    error = error or run.error or f"run {run.status}"
                elif profile.answered_by != m.juror_model:
                    error = f"answered by {profile.answered_by}, not the frozen {m.juror_model}"
                else:
                    try:
                        verdict = map_verdict(m.shown_a_first, profile.payload)
                        strength = profile.payload.get("strength")
                        rationale = str(profile.payload.get("rationale") or "")[:400]
                    except ValueError as e:
                        error = str(e)
            if outage:
                # Not the match's fault: it goes back to pending and the tournament pauses, so a
                # resume plays it. What the run booked (if anything) counts as spend.
                values = dict(status="pending", run_id=None, claimed_by=None, error=None, verdict=None, cost_usd=Decimal(0))
            elif error:
                values.update(status="failed", error=error[:1000], verdict=None, judged_at=_now())
            else:
                values.update(status="done", verdict=verdict, strength=strength, rationale=rationale, judged_at=_now())
                ok = True
            booked = await self._book(db, run, match_id)
            res = await db.execute(
                update(EvalMatch)
                .where(EvalMatch.id == match_id, EvalMatch.claimed_by == self.owner, EvalMatch.status == "running", self._lease_ok())
                .values(**values)
            )
            await db.commit()
            model = m.juror_model
        self.spent += booked
        if res.rowcount != 1:
            self.stopped = True  # the lease moved on: this result is not ours to record
            return
        if outage:
            self.pause_reason = self.pause_reason or outage
        elif run_id is not None:
            c, n = self.observed.get(model, (Decimal(0), 0))
            self.observed[model] = (c + values.get("cost_usd", Decimal(0)), n + 1)
            self._note(ok)


async def run_tournament(tid: uuid.UUID, owner: str | None = None) -> None:
    """The background task of one tournament. Never raises except cancellation. `owner` is the lease
    holder `start`/`resume` registered; without it the runner takes the lease itself, and returns
    at once if another runner holds it."""
    try:
        await _Runner(tid, owner).go()
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 - an internal error marks the tournament, not the process
        log.exception("tournament %s crashed", tid)
        try:
            async with get_session_factory()() as db:
                res = await db.execute(
                    update(EvalTournament).where(EvalTournament.id == tid, EvalTournament.status == "running")
                    .values(status="failed", pause_reason=f"error: {type(e).__name__}: {e}"[:500], lease_owner=None, lease_until=None)
                )
                t = await db.get(EvalTournament, tid)
                if res.rowcount and t is not None and t.freeze_id:  # the pass is over (resume can still use the selection)
                    with contextlib.suppress(jury.JuryError):
                        await jury.release_freeze(db, workspace_id=t.workspace_id, freeze_id=t.freeze_id)
                await db.commit()
        except Exception:  # noqa: BLE001
            log.warning("could not mark tournament %s failed", tid, exc_info=True)


# ── reading ───────────────────────────────────────────────────────────────────────────────────
def _aware(d: datetime | None) -> datetime | None:
    return d if d is None or d.tzinfo else d.replace(tzinfo=timezone.utc)


def jury_out(freeze: RoutingFreeze | None, dropped: list[str] | None = None) -> list[dict[str, Any]]:
    """The frozen jury as the pass uses it: index, family, model, and whether a gate dropped the family."""
    picks = list(((freeze.selection or {}).get("picks") or [])) if freeze is not None else []
    gone = set(dropped or [])
    return [{"index": i, "family": p.get("family"), "model": p.get("model"), "dropped": p.get("family") in gone}
            for i, p in enumerate(picks)]


def tournament_out(t: EvalTournament, snap: PeerPoolSnapshot | None = None, freeze: RoutingFreeze | None = None) -> dict[str, Any]:
    lease = _aware(t.lease_until)
    stored_gates = (t.diagnostics or {}).get("gates") or []
    gates = dg.finalize_gates(stored_gates) if t.status == "completed" else stored_gates
    return {
        "id": str(t.id),
        "module": t.module,
        "module_set": t.module_set,
        "snapshot_id": str(t.snapshot_id),
        "snapshot": None if snap is None else {
            "taxonomy_version": snap.taxonomy_version, "min_n": snap.min_n, "rule": snap.rule,
            "corpus_filter": snap.corpus_filter, "created_at": snap.created_at.isoformat() if snap.created_at else None,
        },
        "judge_prompt_version": t.judge_prompt_version,
        "freeze_id": str(t.freeze_id) if t.freeze_id else None,
        "model_policy": t.model_policy,
        "status": t.status,
        "pause_reason": t.pause_reason,
        "rounds": t.rounds,
        "current_round": t.current_round,
        "budget_usd": float(t.budget_usd) if t.budget_usd is not None else None,
        "spent_usd": float(t.spent_usd or 0),
        "swap_rate": t.swap_rate,
        "double_rate": t.double_rate,
        "seed": t.seed,
        "estimates": t.estimates,
        "pools": (t.estimates or {}).get("pools", []),
        "summary": t.summary,
        "dropped_families": list(t.dropped_families or []),
        "acknowledged_gates": dict(t.acknowledged_gates or {}),
        # the gates as of the last finished round: key and state (empty until a round finishes);
        # `GET .../diagnostics` has the detail
        "gates": [{"key": g["key"], "state": g["state"]} for g in gates],
        "required_acknowledgements": required_gates(t),
        "jury": jury_out(freeze, t.dropped_families),  # the frozen jury (empty before start); the preview is in `estimates`
        "lease_until": lease.isoformat() if lease else None,
        # running, but nothing is playing it: resume it
        "stalled": t.status == "running" and (lease is None or lease < _now()),
        "created_by": t.created_by,
        "started_by": t.started_by,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "started_at": t.started_at.isoformat() if t.started_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
    }


def match_out(m: EvalMatch, labels: dict[uuid.UUID, str] | None = None) -> dict[str, Any]:
    labels = labels or {}
    return {
        "id": str(m.id), "pool_key": m.pool_key, "round": m.round, "kind": m.kind,
        "a_member_id": str(m.a_member_id) if m.a_member_id else None,
        "b_member_id": str(m.b_member_id) if m.b_member_id else None,
        "anchor_set_id": str(m.anchor_set_id) if m.anchor_set_id else None, "anchor_index": m.anchor_index,
        "a": labels.get(m.a_member_id), "b": labels.get(m.b_member_id),  # staff view only
        "shown_a_first": m.shown_a_first, "juror_index": m.juror_index, "juror_family": m.juror_family,
        "juror_model": m.juror_model, "run_id": str(m.run_id) if m.run_id else None, "verdict": m.verdict,
        "strength": m.strength, "rationale": m.rationale, "swap_of": str(m.swap_of) if m.swap_of else None,
        "double_of": str(m.double_of) if m.double_of else None, "status": m.status, "error": m.error,
        "cost_usd": float(m.cost_usd or 0), "input_tokens": m.input_tokens, "output_tokens": m.output_tokens,
        "mask_count_a": m.mask_count_a, "mask_count_b": m.mask_count_b,
        "created_at": m.created_at.isoformat() if m.created_at else None,
        "judged_at": m.judged_at.isoformat() if m.judged_at else None,
    }


async def list_matches(
    db: AsyncSession, t: EvalTournament, *, limit: int = 50, offset: int = 0, status: str | None = None,
    kind: str | None = None, pool: str | None = None,
) -> dict[str, Any]:
    q = select(EvalMatch).where(EvalMatch.tournament_id == t.id)
    cq = select(func.count()).select_from(EvalMatch).where(EvalMatch.tournament_id == t.id)
    for col, v in ((EvalMatch.status, status), (EvalMatch.kind, kind), (EvalMatch.pool_key, pool)):
        if v:
            q, cq = q.where(col == v), cq.where(col == v)
    total = (await db.execute(cq)).scalar_one()
    rows = list((await db.execute(q.order_by(EvalMatch.round, EvalMatch.created_at, EvalMatch.id).limit(limit).offset(offset))).scalars())
    members = {m.id: m.company for m in await pl.members_of(db, t.snapshot_id)}
    return {"total": total, "limit": limit, "offset": offset, "matches": [match_out(m, members) for m in rows]}


async def list_ratings(db: AsyncSession, t: EvalTournament) -> dict[str, Any]:
    rows = list((await db.execute(
        select(ModuleRating).where(ModuleRating.tournament_id == t.id).order_by(ModuleRating.pool_key, ModuleRating.rating.desc())
    )).scalars())
    members: dict[uuid.UUID, PeerPoolMember] = {m.id: m for m in await pl.members_of(db, t.snapshot_id)}
    pools_out: dict[str, list[dict]] = {}
    for r in rows:
        m = members.get(r.member_id)
        pools_out.setdefault(r.pool_key, []).append({
            "member_id": str(r.member_id), "company": m.company if m else None,
            "report_module_id": str(m.report_module_id) if m and m.report_module_id else None,
            "report_extraction_id": str(m.report_extraction_id) if m and m.report_extraction_id else None,
            "text_sha256": m.text_sha256 if m else None, "rating": r.rating, "se": r.se,
            "lo90": r.lo90, "hi90": r.hi90, "matches": r.matches, "wins": r.wins, "band_kind": r.band_kind,
            "band": r.band, "word_count": r.word_count, "claim_density": r.claim_density,
        })
    level = {m.pool_key: m.pool_level for m in members.values()}
    return {"tournament_id": str(t.id), "status": t.status, "partial": t.status != "completed",
            "snapshot_id": str(t.snapshot_id), "pools": [
        # `key`/`ratings` are the PR 6 names; `pool_key`/`rows` carry the same data for the diagnostics page
        {"key": k, "pool_key": k, "level": level.get(k), "n": len(v), "band_kind": v[0]["band_kind"] if v else None,
         "ratings": v, "rows": v}
        for k, v in pools_out.items()
    ]}
