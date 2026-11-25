"""Anchor sets for the judge-drift gate (disclosure evaluation engine, PR 7).

An anchor set is a fixed list of module pairs (`eval_anchor_sets`) taken from a COMPLETED tournament
and judged once by a frozen jury, the baseline. Every later tournament of the same module re-judges
them first with its own jury (`tournament._Runner._anchor_phase`); more changed verdicts than
`eval_gate_anchor_changes_pause` pauses the pass (`anchor_drift`): the instrument moved.

* Selection is deterministic and stratified across rating-gap quantiles of the source tournament:
  mostly decisive verdicts (clear and lean), a few ties if there are any.
* A pair names its two texts by blob key + sha256 (never by pool member: snapshots differ between
  passes) and copies the masking names, so the text is verified and masked exactly as a match is.
* A pair whose text can never be read as anchored is marked unusable and left out; the set is `ready` when
  at least `min_usable` pairs remain (15 of 20), else `failed` with the reason.
* The baseline judging is run exactly like a match: one `Run` per (pair, juror) on the "Disclosure Jury"
  harness through `engine/direct.execute_direct`, calling the frozen model with no fallback, stamped
  `JUROR_KEY` so it never becomes router evidence. Its cost is booked to the SET's own spend, never to
  a tournament (a run-id ledger, `eval_anchor_runs`, gives both the spend and the votes, so a crash or a
  takeover can neither double- nor under-book). The baseline verdict of a pair is the majority across the jurors (tie when there is
  none). `ready` once every pair has every juror's vote; when the budget or a juror runs out first the
  set stays `baseline_pending` with what was judged and `resume_anchor_set` continues it.
* The jury is the source tournament's freeze when still active, else a new k=3 freeze (`pass_ref`
  `anchors:<set id>`), released when the baseline is done.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import random
import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from tret.batches.rubrics import JUROR_KEY, STRUCTURED_CALL_KEY
from tret.config import get_settings
from tret.db.models import EvalAnchorRun, EvalAnchorSet, EvalTournament, ModuleRating, PeerPoolSnapshot, Project, RoutingFreeze, Run
from tret.engine import direct
from tret.evaluation import diagnostics as dg
from tret.evaluation import jury, pools as pl
from tret.evaluation import tournament as tn
from tret.evaluation.juror import map_verdict, mask_names_with_count
from tret.evaluation.modules import Module
from tret.services.workspace import ensure_jury_harness

log = logging.getLogger("tret.anchors")

TIE_SHARE = 0.15  # about this share of an anchor set is ties (when the source has them)
MIN_PAIRS = 4
BASELINE_LEASE_SECONDS = 900.0  # refreshed before every pair; a pair's calls time out well inside it
EVAL_ANCHOR_KEY = "_eval_anchor_set"
EVAL_ANCHOR_INDEX_KEY = "_eval_anchor_index"


# ── selection (pure) ────────────────────────────────────────────────────────────────────────────
def _spread(items: list, k: int) -> list:
    """k items spread evenly over a sorted list (the middle of each of k equal slices)."""
    if k <= 0 or not items:
        return []
    if k >= len(items):
        return list(items)
    return [items[min(len(items) - 1, int((i + 0.5) * len(items) / k))] for i in range(k)]


def select_pairs(cands: list[dict], n: int) -> list[dict]:
    """`n` candidates (`match_id`, `verdict`, `gap`) spread over the quantiles of the rating gap, with
    about `TIE_SHARE` ties when there are ties to take. Deterministic: ties on the gap break by match id."""
    key = lambda c: (c["gap"], str(c["match_id"]))  # noqa: E731
    decisive = sorted((c for c in cands if c["verdict"] != "tie"), key=key)
    ties = sorted((c for c in cands if c["verdict"] == "tie"), key=key)
    want_ties = min(len(ties), round(n * TIE_SHARE) if ties else 0)
    want_dec = min(len(decisive), n - want_ties)
    want_ties = min(len(ties), n - want_dec)
    return sorted(_spread(decisive, want_dec) + _spread(ties, want_ties), key=key)


# ── create / resume ─────────────────────────────────────────────────────────────────────────────
_tasks: dict[uuid.UUID, tuple[asyncio.Task, str]] = {}


def _spawn(set_id: uuid.UUID, owner: str) -> None:
    live = _tasks.get(set_id)
    if live is not None and not live[0].done():
        if live[1] == owner:
            return
        live[0].cancel()
    task = asyncio.create_task(run_baseline(set_id, owner))
    _tasks[set_id] = (task, owner)
    task.add_done_callback(lambda t_, sid=set_id: _tasks.pop(sid, None) if sid in _tasks and _tasks[sid][0] is t_ else None)


async def drain(timeout: float = 60.0) -> bool:
    """Wait for the running baseline tasks (tests, shutdown)."""
    tasks = [t for t, _o in list(_tasks.values()) if not t.done()]
    if not tasks:
        return True
    _done, pending = await asyncio.wait(tasks, timeout=timeout)
    return not pending


def _pair_side(m: Any) -> dict[str, Any]:
    return {"text_key": m.text_key, "text_sha256": m.text_sha256, "word_count": int(m.word_count or 0),
            "names": list(m.mask_names or []), "domains": list(m.mask_domains or [])}


async def create_anchor_set(
    db: AsyncSession, *, workspace_id: uuid.UUID, tournament_id: uuid.UUID, budget_usd: Any, created_by: str | None = None,
    services: Any = None, spawn: bool = True,
) -> EvalAnchorSet:
    """Pick the pairs of a completed tournament, freeze the baseline jury and start judging. Flushed
    and committed; the baseline runs in the background."""
    from tret.db.models import EvalMatch

    tn._need_jury()
    budget = tn._check_budget(budget_usd)
    t = await tn.get_tournament(db, workspace_id=workspace_id, tournament_id=tournament_id)
    if t.status != "completed":
        raise tn.TournamentError(f"Tournament is {t.status}; an anchor set is taken from a completed one", 409)
    n = int(get_settings().eval_anchor_pairs)
    ratings = {r.member_id: r.rating for r in (await db.execute(select(ModuleRating).where(ModuleRating.tournament_id == t.id))).scalars()}
    members = {m.id: m for m in await pl.members_of(db, t.snapshot_id)}
    cands = []
    for m in (await db.execute(select(EvalMatch).where(
            EvalMatch.tournament_id == t.id, EvalMatch.kind == "primary", EvalMatch.status == "done"))).scalars():
        if m.verdict and m.a_member_id in members and m.b_member_id in members:
            gap = abs(ratings.get(m.a_member_id, 0.0) - ratings.get(m.b_member_id, 0.0))
            cands.append({"match_id": m.id, "verdict": m.verdict, "strength": m.strength, "gap": gap, "a": m.a_member_id, "b": m.b_member_id})
    chosen = select_pairs(cands, n)
    if len(chosen) < MIN_PAIRS:
        raise tn.TournamentError(f"Only {len(chosen)} judged pair(s) to anchor on; an anchor set needs at least {MIN_PAIRS}")
    pairs = [
        {"index": i, "a": _pair_side(members[c["a"]]), "b": _pair_side(members[c["b"]]), "baseline": None,
         "source_match_id": str(c["match_id"]), "source_verdict": c["verdict"], "rating_gap": round(c["gap"], 1), "votes": []}
        for i, c in enumerate(chosen)
    ]
    set_id = uuid.uuid4()
    freeze = await db.get(RoutingFreeze, t.freeze_id) if t.freeze_id else None
    try:
        if freeze is None or freeze.status != "active":
            freeze, _ = await jury.freeze_jury(
                db, workspace_id=workspace_id, pass_ref=f"anchors:{set_id}", purpose="eval_jury", model_policy=t.model_policy,
                k=tn.JURY_K, distinct_authors=True, min_context_window=int((t.estimates or {}).get("min_context_window") or 0),
                created_by=created_by, services=services,
            )
    except jury.JuryError as e:
        raise tn.TournamentError(str(e), e.status) from e
    owner = tn.new_owner()
    aset = EvalAnchorSet(
        id=set_id, workspace_id=workspace_id, module=t.module, module_set=t.module_set, source_tournament_id=t.id,
        module_def=t.module_def, judge_prompt_version=t.judge_prompt_version, freeze_id=freeze.id, pairs=pairs,
        status="baseline_pending", budget_usd=budget, spent_usd=0, lease_owner=owner,
        lease_until=tn._now() + _lease(), created_by=created_by,
    )
    db.add(aset)
    await db.commit()
    await db.refresh(aset)
    if spawn:
        _spawn(aset.id, owner)
    return aset


def _lease() -> timedelta:
    return timedelta(seconds=BASELINE_LEASE_SECONDS)


async def get_anchor_set(db: AsyncSession, *, workspace_id: uuid.UUID, set_id: uuid.UUID) -> EvalAnchorSet:
    a = await db.get(EvalAnchorSet, set_id)
    if a is None or a.workspace_id != workspace_id:
        raise tn.TournamentError("Anchor set not found", 404)
    return a


async def resume_anchor_set(
    db: AsyncSession, *, workspace_id: uuid.UUID, set_id: uuid.UUID, budget_usd: Any = None, spawn: bool = True
) -> EvalAnchorSet:
    """Continue an unfinished baseline (optionally with a larger budget). One conditional update decides
    who runs: a baseline whose runner holds a live lease is a 409."""
    tn._need_jury()
    a = await get_anchor_set(db, workspace_id=workspace_id, set_id=set_id)
    if a.status != "baseline_pending":
        raise tn.TournamentError(f"Anchor set is {a.status}; only a pending baseline can be resumed", 409)
    budget = tn._check_budget(budget_usd) if budget_usd is not None else (Decimal(a.budget_usd) if a.budget_usd is not None else None)
    if budget is None or Decimal(a.spent_usd or 0) >= budget:
        raise tn.TournamentError(f"The budget (${a.budget_usd}) is already spent (${a.spent_usd}): resume with a larger budget_usd", 409)
    owner = tn.new_owner()
    res = await db.execute(
        update(EvalAnchorSet)
        .where(EvalAnchorSet.id == a.id, EvalAnchorSet.status == "baseline_pending",
               or_(EvalAnchorSet.lease_until.is_(None), EvalAnchorSet.lease_until < tn._now()))
        .values(budget_usd=budget, pause_reason=None, lease_owner=owner, lease_until=tn._now() + _lease(), updated_at=tn._now())
        .execution_options(synchronize_session=False)
    )
    if res.rowcount != 1:
        await db.rollback()
        raise tn.TournamentError("The baseline is still running (or was finished by someone else)", 409)
    await db.commit()
    await db.refresh(a)
    if spawn:
        _spawn(a.id, owner)
    return a


# ── the baseline runner ─────────────────────────────────────────────────────────────────────────
def min_usable(n_pairs: int) -> int:
    """Usable pairs a set needs to become ready: 15 of the default 20 (three quarters, at least `MIN_PAIRS`)."""
    return min(15, max(MIN_PAIRS, -(-3 * n_pairs // 4)))


class _Baseline:
    def __init__(self, set_id: uuid.UUID, owner: str) -> None:
        self.sid, self.owner = set_id, owner
        self.factory = tn.get_session_factory()
        self.observed: dict[str, tuple[Decimal, int]] = {}
        self.texts: dict[str, str] = {}
        self.stopped = False

    def _ours(self):
        return and_(EvalAnchorSet.id == self.sid, EvalAnchorSet.lease_owner == self.owner, EvalAnchorSet.status == "baseline_pending")

    async def _save(self, **values: Any) -> bool:
        async with self.factory() as db:
            res = await db.execute(
                update(EvalAnchorSet).where(self._ours()).values(updated_at=tn._now(), **values).execution_options(synchronize_session=False)
            )
            await db.commit()
        return res.rowcount == 1

    async def _heartbeat(self) -> None:
        """Keep the lease while the baseline plays, as the tournament runner does; losing it stops this runner."""
        while True:
            await asyncio.sleep(tn.HEARTBEAT_SECONDS)
            try:
                async with self.factory() as db:
                    res = await db.execute(
                        update(EvalAnchorSet).where(self._ours()).values(lease_until=tn._now() + _lease())
                        .execution_options(synchronize_session=False)
                    )
                    await db.commit()
                if res.rowcount != 1:
                    self.stopped = True
                    return
            except Exception:  # noqa: BLE001 - one missed beat is not a lost lease
                log.warning("anchor set %s heartbeat failed", self.sid, exc_info=True)

    async def _ledger(self) -> tuple[Decimal, dict[int, list[dict]]]:
        """(spend, votes per pair) from the settled ledger rows: the one source of both."""
        async with self.factory() as db:
            rows = list((await db.execute(
                select(EvalAnchorRun).where(EvalAnchorRun.anchor_set_id == self.sid, EvalAnchorRun.booked.is_(True))
            )).scalars())
        votes: dict[int, list[dict]] = {}
        for r in rows:
            if r.verdict:
                votes.setdefault(r.pair_index, []).append({
                    "juror_index": r.juror_index, "family": r.family, "model": r.model, "verdict": r.verdict,
                    "strength": r.strength, "run_id": str(r.run_id), "cost_usd": float(r.cost_usd or 0)})
        for v in votes.values():
            v.sort(key=lambda x: x["juror_index"])
        return sum((Decimal(r.cost_usd or 0) for r in rows), Decimal(0)), votes

    async def _recover(self) -> None:
        """Ledger rows a dead runner left unsettled: the run's own cost is booked (once; the row is
        settled by a conditional update) and the call is not counted as a vote, so it is judged again."""
        async with self.factory() as db:
            rows = list((await db.execute(
                select(EvalAnchorRun).where(EvalAnchorRun.anchor_set_id == self.sid, EvalAnchorRun.booked.is_(False))
            )).scalars())
            for r in rows:
                run = await db.get(Run, r.run_id)
                if run is not None and run.status in ("queued", "running"):
                    created = tn._aware(run.created_at)
                    if created is None or (tn._now() - created).total_seconds() < tn.RUN_STALE_SECONDS:
                        continue  # in flight somewhere: its settlement will come
                cost = Decimal(run.reported_cost_usd if run is not None and run.reported_cost_usd is not None else ((run.cost_usd or 0) if run is not None else 0))
                await db.execute(
                    update(EvalAnchorRun).where(EvalAnchorRun.run_id == r.run_id, EvalAnchorRun.booked.is_(False))
                    .values(booked=True, cost_usd=cost, error="unsettled run recovered by a takeover")
                )
            await db.commit()

    async def _text(self, side: dict) -> str:
        from tret.storage import get_store

        key, sha = side["text_key"], side["text_sha256"]
        if key in self.texts:
            return self.texts[key]
        if not key or not sha:
            raise tn._TextError("the anchor pair has no text hash")
        try:
            data = await get_store().get(key)
        except FileNotFoundError:
            raise tn._TextError(f"text blob {key} is missing") from None
        if hashlib.sha256(data).hexdigest() != sha:
            raise tn._TextError(f"text blob {key} no longer matches the anchor's hash")
        self.texts[key] = data.decode("utf-8", errors="replace")
        return self.texts[key]

    def _estimate(self, pair: dict, model: str) -> Decimal:
        info = self.catalog.get(model)
        est = Decimal(0)
        if info is not None:
            tin, tout = tn.match_tokens(int(pair["a"].get("word_count") or 0), int(pair["b"].get("word_count") or 0))
            est = Decimal(str(info.cost_usd(tin, tout)))
        seen = self.observed.get(model)
        if seen:
            est = max(est, seen[0] / seen[1])
        elif self.observed:
            est = max(est, max(c / n for c, n in self.observed.values()))
        return est

    async def run(self) -> None:
        async with self.factory() as db:
            res = await db.execute(
                update(EvalAnchorSet)
                .where(EvalAnchorSet.id == self.sid, EvalAnchorSet.status == "baseline_pending",
                       or_(EvalAnchorSet.lease_owner == self.owner, EvalAnchorSet.lease_until.is_(None), EvalAnchorSet.lease_until < tn._now()))
                .values(lease_owner=self.owner, lease_until=tn._now() + _lease())
                .execution_options(synchronize_session=False)
            )
            await db.commit()
            if res.rowcount != 1:
                return
            a = await db.get(EvalAnchorSet, self.sid)
            freeze = await db.get(RoutingFreeze, a.freeze_id) if a.freeze_id else None
            if freeze is None:
                await self._save(pause_reason="error: the baseline jury freeze is gone", lease_owner=None, lease_until=None)
                return
            self.sel, self.freeze_id, self.pass_ref, self.policy = jury.selection_of(freeze), freeze.id, freeze.pass_ref, dict(freeze.model_policy or {"mode": "auto"})
            self.module = Module.model_validate(a.module_def)
            self.workspace_id = a.workspace_id
            src = await db.get(EvalTournament, a.source_tournament_id) if a.source_tournament_id else None
            snap = await db.get(PeerPoolSnapshot, src.snapshot_id) if src is not None else None
            self.project_id = (snap.project_id if snap is not None else None) or (await db.execute(
                select(Project.id).where(Project.workspace_id == a.workspace_id).limit(1))).scalar_one()
            self.harness_id = (await ensure_jury_harness(db, a.workspace_id)).id
            self.catalog = (await direct.get_call_services(db, a.workspace_id)).catalog
            pairs = [dict(p) for p in a.pairs]
            budget = Decimal(a.budget_usd) if a.budget_usd is not None else None
            await db.commit()
        beat = asyncio.create_task(self._heartbeat())
        try:
            await self._play(pairs, budget)
        finally:
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beat

    async def _play(self, pairs: list[dict], budget: Decimal | None) -> None:
        await self._recover()
        k = len(self.sel.picks)
        pause: str | None = None
        spent, votes = await self._ledger()
        for p in pairs:
            p["votes"] = votes.get(int(p["index"]), [])
        for p in pairs:
            if self.stopped:
                return
            if p.get("unusable"):
                continue
            have = {v["juror_index"] for v in p["votes"]}
            todo = [i for i in range(k) if i not in have]
            if not todo:
                continue
            try:
                await self._text(p["a"])
                await self._text(p["b"])
            except tn._TextError as e:  # this pair can never be judged as anchored: it is left out, the set goes on
                p["unusable"] = f"{tn.TEXT_ERROR} {e}"[:300]
                if not await self._save(pairs=pairs):
                    return
                continue
            est = sum((self._estimate(p, self.sel.picks[i].model) for i in todo), Decimal(0))
            if budget is not None and spent + est > budget:
                pause = "budget"
                break
            results = await asyncio.gather(*(self._one(p, i) for i in todo))
            spent, votes = await self._ledger()  # every call was booked as it finished
            p["votes"] = votes.get(int(p["index"]), [])
            for r in results:
                if r["verdict"] is not None:
                    c, n = self.observed.get(r["model"], (Decimal(0), 0))
                    self.observed[r["model"]] = (c + r["cost"], n + 1)
                elif r["outage"] or r["error"]:
                    pause = pause or r["outage"] or f"juror_failures: {r['error']}"[:500]
            if not await self._save(pairs=pairs, spent_usd=spent, lease_until=tn._now() + _lease()):
                return  # the lease moved on: not ours to write
            if pause:
                break
        usable = [p for p in pairs if not p.get("unusable")]
        if not all(len(p["votes"]) >= k for p in usable):
            async with self.factory() as db:  # pending: release the lease so a resume can take over
                await db.execute(
                    update(EvalAnchorSet).where(self._ours())
                    .values(pairs=pairs, spent_usd=spent, pause_reason=(pause or "juror_failures")[:500], lease_owner=None,
                            lease_until=None, updated_at=tn._now())
                    .execution_options(synchronize_session=False)
                )
                await db.commit()
            return
        need = min_usable(len(pairs))
        if len(usable) >= need:
            for p in usable:
                p["baseline"] = dg.majority([v["verdict"] for v in p["votes"]])
            fields: dict[str, Any] = {"status": "ready", "pause_reason": None}
        else:
            why = "; ".join(f"pair {p['index']}: {p['unusable']}" for p in pairs if p.get("unusable"))
            fields = {"status": "failed", "pause_reason": f"only {len(usable)} of {len(pairs)} pairs usable (needs {need}): {why}"[:500]}
        if await self._save(pairs=pairs, spent_usd=spent, lease_owner=None, lease_until=None, **fields):
            async with self.factory() as db:
                with contextlib.suppress(jury.JuryError):
                    if self.pass_ref.startswith("anchors:"):  # ours; a source tournament's freeze is its own to release
                        await jury.release_freeze(db, workspace_id=self.workspace_id, freeze_id=self.freeze_id)
                await db.commit()

    async def _one(self, p: dict, idx: int) -> dict[str, Any]:
        """One juror's verdict on one pair: a `Run` like a match's, booked in the ledger the moment it
        finishes (once: the settling update is conditional on the row still being unbooked)."""
        async with tn._semaphore():
            pick = self.sel.picks[idx]
            ta, tb = await self._text(p["a"]), await self._text(p["b"])
            names, domains = sorted(set(p["a"]["names"] + p["b"]["names"])), sorted(set(p["a"]["domains"] + p["b"]["domains"]))
            ta, _ca = mask_names_with_count(ta, names, domains)
            tb, _cb = mask_names_with_count(tb, names, domains)
            ta, tb = tn.defang(ta), tn.defang(tb)
            shown_a_first = random.Random(f"{self.sid}:{p['index']}:{idx}").random() < 0.5
            t1, t2 = (ta, tb) if shown_a_first else (tb, ta)
            run_id = uuid.uuid4()
            out: dict[str, Any] = {"juror_index": idx, "model": pick.model, "verdict": None, "strength": None,
                                   "cost": Decimal(0), "outage": None, "error": None}
            try:
                async with self.factory() as db:
                    db.add(Run(
                        id=run_id, project_id=self.project_id, harness_id=self.harness_id, task_type=direct.TASK_TYPE,
                        task_input={JUROR_KEY: True, STRUCTURED_CALL_KEY: True, EVAL_ANCHOR_KEY: str(self.sid), EVAL_ANCHOR_INDEX_KEY: p["index"]},
                        document_ids=[],
                    ))
                    await db.flush()
                    db.add(EvalAnchorRun(run_id=run_id, anchor_set_id=self.sid, pair_index=int(p["index"]), juror_index=idx,
                                         family=pick.family, model=pick.model))  # the trace is there before the call is
                    await db.commit()
                profile = tn._Juror(
                    module=self.module, text_1=t1, text_2=t2, freeze=self.sel, freeze_id=self.freeze_id, pass_ref=self.pass_ref,
                    juror_index=idx, model=pick.model,
                )
                await direct.execute_direct(run_id, profile, self.factory, model_policy=self.policy)
                async with self.factory() as db:
                    run = (await db.execute(select(Run).where(Run.id == run_id).execution_options(populate_existing=True))).scalar_one()
                    out["cost"] = Decimal(run.reported_cost_usd if run.reported_cost_usd is not None else (run.cost_usd or 0))
                    if run.status != "completed":
                        if run.started_at is None:
                            out["outage"] = "credits"
                        elif (run.error or "").split(":", 1)[0] in tn.OUTAGE_KINDS:
                            out["outage"] = "juror_unavailable"
                        out["error"] = run.error or f"run {run.status}"
                    elif profile.payload is None:
                        out["error"] = "no answer"
                    elif profile.answered_by != pick.model:
                        out["error"] = f"answered by {profile.answered_by}, not the frozen {pick.model}"
                    else:
                        out["verdict"] = map_verdict(shown_a_first, profile.payload)
                        out["strength"] = profile.payload.get("strength")
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - a transient database or provider error, not the jury's verdict
                log.warning("anchor set %s pair %s: %s", self.sid, p["index"], e, exc_info=True)
                out["error"] = f"{type(e).__name__}: {e}"
            async with self.factory() as db:  # settle the ledger row exactly once
                await db.execute(
                    update(EvalAnchorRun).where(EvalAnchorRun.run_id == run_id, EvalAnchorRun.booked.is_(False))
                    .values(booked=True, cost_usd=out["cost"], verdict=out["verdict"], strength=out["strength"],
                            error=(out["error"] or None) and out["error"][:500])
                )
                await db.commit()
            return out


async def run_baseline(set_id: uuid.UUID, owner: str) -> None:
    """The background task of one baseline. Never raises except cancellation."""
    try:
        await _Baseline(set_id, owner).run()
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        log.exception("anchor set %s baseline crashed", set_id)
        with contextlib.suppress(Exception):
            async with tn.get_session_factory()() as db:
                await db.execute(
                    update(EvalAnchorSet).where(EvalAnchorSet.id == set_id, EvalAnchorSet.status == "baseline_pending")
                    .values(pause_reason=f"error: {type(e).__name__}: {e}"[:500], lease_owner=None, lease_until=None)
                )
                await db.commit()


# ── reading ───────────────────────────────────────────────────────────────────────────────────────
async def anchor_set_out(db: AsyncSession, a: EvalAnchorSet, *, detail: bool = False) -> dict[str, Any]:
    freeze = await db.get(RoutingFreeze, a.freeze_id) if a.freeze_id else None
    picks = list(((freeze.selection or {}).get("picks") or [])) if freeze else []
    pairs = list(a.pairs or [])
    lease = tn._aware(a.lease_until)
    out = {
        "id": str(a.id), "module": a.module, "module_set": a.module_set,
        "source_tournament_id": str(a.source_tournament_id) if a.source_tournament_id else None,
        "status": a.status, "pause_reason": a.pause_reason, "freeze_id": str(a.freeze_id) if a.freeze_id else None,
        "jurors": [{"model": p["model"], "family": p["family"]} for p in picks],
        "n_pairs": len(pairs), "unusable": sum(1 for p in pairs if p.get("unusable")), "votes": sum(len(p.get("votes") or []) for p in pairs),
        "votes_expected": sum(1 for p in pairs if not p.get("unusable")) * len(picks),
        "budget_usd": float(a.budget_usd) if a.budget_usd is not None else None, "spent_usd": float(a.spent_usd or 0),
        "stalled": a.status == "baseline_pending" and bool(a.lease_owner) and (lease is None or lease < tn._now()),
        "judge_prompt_version": a.judge_prompt_version, "created_by": a.created_by,
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "updated_at": a.updated_at.isoformat() if a.updated_at else None,
    }
    if detail:
        out["pairs"] = pairs
    return out


async def list_anchor_sets(db: AsyncSession, *, workspace_id: uuid.UUID, limit: int = 50) -> list[dict[str, Any]]:
    rows = (await db.execute(
        select(EvalAnchorSet).where(EvalAnchorSet.workspace_id == workspace_id).order_by(EvalAnchorSet.created_at.desc()).limit(limit)
    )).scalars().all()
    return [await anchor_set_out(db, a) for a in rows]


__all__ = ["create_anchor_set", "resume_anchor_set", "get_anchor_set", "list_anchor_sets", "anchor_set_out", "drain", "select_pairs",
           "run_baseline"]
