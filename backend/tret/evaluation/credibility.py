"""The Engine 2 pass: the sealed whole-report Credibility call (disclosure evaluation engine, PR 8).

One call per report, no tools, no harness, no lessons, no row memory: a single `routed_json_call`
with a fixed schema reads ONE report (`evaluation/sealed.py`, the only input builder) and returns a
holistic Credibility Score (1-10), the 3 sharpest pushbacks an investor would raise, the places the
story breaks down and a short summary. It never sees a peer, a rating, a module extract or company
metadata. The firewall is enforced in code and tested (tests/test_eval_credibility_firewall.py):

1. no tools or harness: this module never imports the agent engine; the call goes through
   `engine/direct.execute_direct` (a `Run` for cost, billing and the runs UI) into `routed_json_call`;
2. one input builder: `sealed.credibility_payload`; this module is import-linted with a denylist
   (no tournament, ratings, pools, extraction, register, harness or tools code);
3. payload audit: each review stores sha256 of the exact prompt and of the report text, and
   `verify_review` rebuilds both from the report;
4. blind both ways: Engine 1 never imports this module or reads its tables (linted);
5. no learning leak: the run is stamped `SEALED_CALL_KEY`, so it never becomes router evidence or a
   `run_outcomes` row.

A pass has 1-4 weighted ARMS, each `{key, label, weight, model_policy}`; a policy constrains and the
router picks, a model is never named (a pin is refused). At start each arm freezes its own model once
(`evaluation/jury.py`, purpose `sealed_call`, `k=1`, pass reference `credibility:<pass>:<arm>`) with the
router's reason, so the instrument does not change under the measurement; the freezes are released when
the pass completes, is cancelled or fails. Reports are assigned to arms at creation, deterministically
(weighted, seeded by sha256(pass id + report id)).

Running safely mirrors the tournament runner (`evaluation/tournament.py`): state lives in the database;
a runner holds a lease (120 s, 30 s heartbeat); start and resume are conditional updates; each review is
claimed (`pending` -> `running`) before its call so two runners never play the same one; spend stops at
the budget (the pass pauses, reason `budget`); the cost of EVERY call, including a failed or cancelled
one, is booked to the pass exactly once through a run-id ledger (`credibility_review_runs`). Credits,
an unreachable frozen model or a provider outage PAUSE the pass; only an internal error fails it.

Gates (Engine 2's day-1 diagnostics): `score_clumping` (the share of done reviews scoring 6-8) and
`failure_rate` pause the pass; resuming needs each tripped gate acknowledged with a note, as in Engine 1.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import math
import os
import platform
import random
import re
import statistics
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from sqlalchemy import and_, delete, exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tret.batches.rubrics import SEALED_CALL_KEY, STRUCTURED_CALL_KEY
from tret.config import get_settings
from tret.db.engine import get_session_factory
from tret.db.models import (
    Batch,
    BatchTarget,
    CredibilityPass,
    CredibilityReview,
    CredibilityReviewRun,
    DisclosureReport,
    Project,
    RoutingFreeze,
    Run,
)
from tret.engine import direct
from tret.engine.extensions import get_extension_registry
from tret.engine.routed_call import CallServices, RoutedCallError, check_policy
from tret.evaluation import jury, sealed
from tret.services.workspace import ensure_jury_harness

log = logging.getLogger("tret.credibility")

PASS_PREFIX = "credibility:"
EVAL_CREDIBILITY_KEY = "_eval_credibility"
EVAL_CREDIBILITY_REVIEW_KEY = "_eval_credibility_review"
INTERRUPTED_RUN_ERROR = "interrupted: the pass was stopped before this call started"
LEASE_SECONDS = 120.0
HEARTBEAT_SECONDS = 30.0
RUN_STALE_SECONDS = 900.0  # a run still 'running' this long after it started is taken as dead
ORPHAN_POLL_SECONDS = 1.0
MAX_ARMS = 4
MAX_REPORTS = 2000
MAX_ATTEMPTS = 3  # model calls per review before a provider failure fails it for good
PROVIDER_STREAK_PAUSE = 3  # provider failures in a row that pause the pass (an outage, not a bad report)
FAIL_MIN_ATTEMPTS = 10  # reviews that ran to an end before the failure-rate gate counts
CHUNK = 24  # reviews picked up per round of the runner
WINDOW_CAP = 200_000  # the largest context window a pass asks the router for (longer reports are digested)
WINDOW_FLOOR = 32_000  # the smallest window an arm will take
WINDOW_STEPS = (200_000, 128_000, 64_000, 32_000)  # what an arm steps down through when no model has the window it wanted
EXPECTED_OUTPUT_TOKENS = 900  # a typical answer, for the expected cost; the budget check uses the cap
CANCELLABLE = ("draft", "running", "paused")
OUTAGE_KINDS = ("unroutable", "frozen_outside_policy", "not_in_freeze")  # the frozen model cannot be reached
PAUSING_GATES = ("score_clumping", "failure_rate")
PAUSE_REASONS = ("budget", "credits", "model_unavailable", "provider_unavailable", "score_clumping", "failure_rate",
                 "restart", "interrupted")
ARM_KEY = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


class CredibilityError(Exception):
    """A request the pass cannot serve; `status` is the HTTP status the API answers with."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(d: datetime | None) -> datetime | None:
    return d if d is None or d.tzinfo else d.replace(tzinfo=timezone.utc)


def _lease_end() -> datetime:
    return _now() + timedelta(seconds=LEASE_SECONDS)


def new_owner() -> str:
    return f"{platform.node()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def credibility_enabled() -> bool:
    return bool(getattr(get_settings(), "eval_credibility", False))


def _need_jury() -> None:
    if (getattr(get_settings(), "eval_jury", "off") or "off").strip().lower() != "on":
        raise CredibilityError(
            "The pass freezes each arm's model through the jury freeze: set TRET_EVAL_JURY=on as well as TRET_EVAL_CREDIBILITY=true.", 409
        )


def _check_budget(value: Any) -> Decimal:
    try:
        b = Decimal(str(value))
    except Exception:  # noqa: BLE001
        raise CredibilityError("budget_usd must be a number") from None
    if not b.is_finite() or b <= 0:
        raise CredibilityError("budget_usd must be greater than 0")
    return b


# ── arms ────────────────────────────────────────────────────────────────────────────────────
def normalise_arms(arms: list[dict] | None, catalog: Any = None) -> list[dict]:
    """1-4 arms `{key, label, weight, model_policy}`, checked. A policy that pins a model is refused
    (the router's own `check_policy`, plus an `allowed` list of one model, which is a pin in disguise)."""
    if not arms or not 1 <= len(arms) <= MAX_ARMS:
        raise CredibilityError(f"a pass has 1 to {MAX_ARMS} arms")
    out: list[dict] = []
    seen: set[str] = set()
    for a in arms:
        if not isinstance(a, dict):
            raise CredibilityError("each arm is an object {key, label, weight, model_policy}")
        key = str(a.get("key") or "").strip()
        if not ARM_KEY.match(key):
            raise CredibilityError(f"arm key {key!r} must be 1-32 characters of a-z, 0-9, - or _")
        if key in seen:
            raise CredibilityError(f"arm key {key!r} is used twice")
        seen.add(key)
        try:
            weight = float(a.get("weight", 1))
        except (TypeError, ValueError):
            raise CredibilityError(f"arm {key}: weight must be a number") from None
        if not math.isfinite(weight) or weight <= 0:
            raise CredibilityError(f"arm {key}: weight must be greater than 0")
        try:
            policy = check_policy(a.get("model_policy"))
        except RoutedCallError as e:
            raise CredibilityError(f"arm {key}: {e}") from e
        allowed = policy.get("allowed")
        names: set[str] = set()
        if isinstance(allowed, (list, tuple)):
            names = set(map(str, allowed))
            if catalog is not None:
                names = {n for n in names if catalog.get(n) is not None}  # a name the catalog does not know is no choice for the router
        if len(names) == 1:
            raise CredibilityError(f"arm {key}: an allowed list of one model is a pin; the router must have a choice")
        out.append({"key": key, "label": str(a.get("label") or key).strip()[:80] or key, "weight": weight, "model_policy": policy})
    return out


def assign_arm(pass_id: uuid.UUID | str, report_id: uuid.UUID | str, arms: list[dict]) -> str:
    """The arm a report is read by: weighted by `weight`, seeded by sha256(pass id + report id), so the
    same pass and report always land on the same arm."""
    digest = hashlib.sha256(f"{pass_id}{report_id}".encode()).digest()
    u = int.from_bytes(digest[:8], "big") / 2**64
    total = sum(float(a["weight"]) for a in arms)
    acc = 0.0
    for a in arms:
        acc += float(a["weight"]) / total
        if u < acc:
            return a["key"]
    return arms[-1]["key"]


# ── report selection ────────────────────────────────────────────────────────────────────────
def _reason(r: DisclosureReport) -> str | None:
    """Why a report cannot be read by a sealed call, or None."""
    if r.status == "quarantined":
        return f"quarantined: {r.quarantine_reason or 'unknown'}"
    if r.status != "normalised":
        return f"not_normalised ({r.status})"
    if not r.markdown_key:
        return "no_markdown"
    if not r.is_primary:
        return "not_primary"
    return None


async def resolve_selection(
    db: AsyncSession, workspace_id: uuid.UUID, *, report_ids: list[uuid.UUID] | None = None, batch_id: uuid.UUID | None = None,
    limit: int | None = None, sample_seed: int | None = None,
) -> SimpleNamespace:
    """The reports a pass reads: `chosen` (one primary normalised report per target, in the workspace),
    `skipped` (known reports that cannot be read, each with its reason) and `rejected` (explicit ids that
    are not this workspace's). With `limit`, a `sample_seed` draws a seeded random sample, else the first
    `limit` in register order."""
    if bool(report_ids) == bool(batch_id):
        raise CredibilityError("give either disclosure_report_ids or batch_id")
    q = select(DisclosureReport, BatchTarget).outerjoin(BatchTarget, BatchTarget.id == DisclosureReport.batch_target_id)
    rejected: list[str] = []
    explicit = report_ids is not None and len(report_ids) > 0
    if explicit:
        ids = list(dict.fromkeys(report_ids))
        if len(ids) > MAX_REPORTS:
            raise CredibilityError(f"at most {MAX_REPORTS} reports per pass")
        q = q.join(Project, Project.id == DisclosureReport.project_id).where(
            Project.workspace_id == workspace_id, DisclosureReport.id.in_(ids)
        )
    else:
        batch = await db.get(Batch, batch_id)
        if batch is None or batch.workspace_id != workspace_id:
            raise CredibilityError("Batch not found", 404)
        q = q.where(BatchTarget.collection_id == batch.id)
    rows = (await db.execute(q.order_by(BatchTarget.position.is_(None), BatchTarget.position, DisclosureReport.created_at.desc(), DisclosureReport.id))).all()
    if explicit:
        found = {r.id for r, _t in rows}
        rejected = [str(i) for i in ids if i not in found]
    groups: dict[str, list[tuple[DisclosureReport, BatchTarget | None]]] = {}
    for r, t in rows:
        groups.setdefault(str(r.batch_target_id) if r.batch_target_id else f"report:{r.id}", []).append((r, t))
    chosen, skipped = [], []

    def cand(r, t) -> SimpleNamespace:
        return SimpleNamespace(report_id=r.id, project_id=r.project_id, target_id=r.batch_target_id,
                               label=(t.label or t.company) if t is not None else None, chars=int(r.markdown_chars or 0))

    def skip(r, t, reason: str) -> None:
        skipped.append(SimpleNamespace(report_id=r.id, project_id=r.project_id, label=(t.label or t.company) if t is not None else None, reason=reason))

    for items in groups.values():
        ok = [(r, t) for r, t in items if _reason(r) is None]
        if ok:
            r, t = ok[0]  # newest first
            chosen.append(cand(r, t))
            if explicit:
                for r2, t2 in items:
                    if r2.id != r.id:
                        skip(r2, t2, _reason(r2) or "duplicate_target")
        elif explicit:
            for r2, t2 in items:
                skip(r2, t2, _reason(r2) or "unreadable")
        else:
            r2, t2 = items[0]
            skip(r2, t2, _reason(r2) or "unreadable")
    if limit is not None and len(chosen) > limit:
        if sample_seed is not None:
            pick = {c.report_id for c in random.Random(int(sample_seed)).sample(sorted(chosen, key=lambda c: str(c.report_id)), int(limit))}
            chosen = [c for c in chosen if c.report_id in pick]
        else:
            chosen = chosen[: int(limit)]
    if len(chosen) + len(skipped) > MAX_REPORTS:
        raise CredibilityError(f"at most {MAX_REPORTS} reports per pass: narrow the selection with limit")
    return SimpleNamespace(chosen=chosen, skipped=skipped, rejected=rejected)


# ── the plan: what each arm would pick and what it would cost (no spend, no write) ───────────
def _max_output() -> int:
    return int(get_settings().eval_credibility_max_output_tokens)


def _required_window(chosen: list[Any]) -> int:
    longest = max((c.chars for c in chosen), default=0)
    need = (longest + 3) // 4 + sealed.estimate_tokens(sealed.SYSTEM_PROMPT) + sealed.USER_OVERHEAD_TOKENS + _max_output()
    return min(max(need, WINDOW_FLOOR), WINDOW_CAP)


def _price(info: Any) -> dict:
    return {"input_per_mtok": float(info.input_price_per_mtok), "output_per_mtok": float(info.output_price_per_mtok)}


async def _preview_arm(db: AsyncSession, workspace_id: uuid.UUID, arm: dict, window: int, services: CallServices):
    """(selection, window used): the window the longest report wants, else the next step down
    (200k, 128k, 64k, 32k) at which the policy still has a model."""
    steps = [window] + [w for w in WINDOW_STEPS if w < window]
    err: jury.JuryError | None = None
    for w in steps:
        try:
            return await jury.preview_jury(
                db, workspace_id=workspace_id, model_policy=arm["model_policy"], k=1, min_context_window=w,
                purpose="sealed_call", services=services,
            ), w
        except jury.JuryError as e:
            err = e
    raise err  # type: ignore[misc]


def _arm_cost(info: Any, chosen: list[Any], max_input: int) -> dict:
    """Whole/digest counts and tokens of every chosen report through this arm's model."""
    sys_t = sealed.estimate_tokens(sealed.SYSTEM_PROMPT)
    whole = digest = tin = 0
    for c in chosen:
        t = (c.chars + 3) // 4 + sys_t + sealed.USER_OVERHEAD_TOKENS
        if t <= max_input:
            whole += 1
            tin += t
        else:
            digest += 1
            tin += max_input
    n = len(chosen)
    tout = n * min(EXPECTED_OUTPUT_TOKENS, _max_output())
    total = float(info.cost_usd(tin, tout)) if info is not None and n else 0.0
    return {"whole_reports": whole, "digest_reports": digest, "est_input_tokens": tin, "est_output_tokens": tout,
            "cost_usd_all_reports": total, "cost_usd_per_report": (total / n if n else 0.0)}


async def plan_pass(
    db: AsyncSession, *, workspace_id: uuid.UUID, arms: list[dict], selection: SimpleNamespace, budget_usd: Any = None,
    services: CallServices | None = None,
) -> tuple[list[dict], dict]:
    """(arms with their previewed pick, estimates). No write, no model call."""
    services = services or await jury.get_call_services(db, workspace_id)
    chosen = selection.chosen
    window = _required_window(chosen)
    out_arms: list[dict] = []
    views: list[dict] = []
    total_w = sum(float(a["weight"]) for a in arms)
    n = len(chosen)
    for arm in arms:
        view: dict[str, Any] = {"key": arm["key"], "label": arm["label"], "weight": arm["weight"], "model_policy": arm["model_policy"],
                                "expected_reports": round(n * float(arm["weight"]) / total_w, 1)}
        used = window
        try:
            sel, used = await _preview_arm(db, workspace_id, arm, window, services)
        except jury.JuryError as e:
            view.update(pick=None, error=str(e), min_context_window=window)
            views.append(view)
            out_arms.append({**arm, "min_context_window": window})
            continue
        pick = sel.picks[0]
        info = services.catalog.get(pick.model)
        ctx = int(getattr(info, "context_window", 0) or 0) or 100_000
        max_input = sealed.usable_input_tokens(ctx, _max_output())
        view.update(
            pick={"model": pick.model, "family": pick.family, "rank": pick.rank, "why": pick.why, "context_window": ctx,
                  "price": _price(info) if info is not None else None},
            min_context_window=used, max_input_tokens=max_input, selection=sel.to_json(), **_arm_cost(info, chosen, max_input),
        )
        view["expected_cost_usd"] = view["cost_usd_per_report"] * view["expected_reports"]
        views.append(view)
        out_arms.append({**arm, "min_context_window": used})
    priced = [v for v in views if v.get("pick")]
    expected = sum(v["expected_cost_usd"] for v in priced)
    skipped_by: dict[str, int] = {}
    for s in selection.skipped:
        key = s.reason.split(":")[0].split(" (")[0]
        skipped_by[key] = skipped_by.get(key, 0) + 1
    budget = float(_check_budget(budget_usd)) if budget_usd is not None else None
    est = {
        "reports": n, "skipped": len(selection.skipped), "skipped_reasons": skipped_by,
        "skipped_examples": [{"disclosure_report_id": str(s.report_id), "label": s.label, "reason": s.reason} for s in selection.skipped[:20]],
        "rejected_ids": list(selection.rejected),
        "input": {"whole": sum(v.get("whole_reports", 0) for v in priced), "digest": sum(v.get("digest_reports", 0) for v in priced)},
        "arms": views, "prompt_version": sealed.PROMPT_VERSION,
        "cost_usd": ({"low": min(v["cost_usd_per_report"] for v in priced) * n, "high": max(v["cost_usd_per_report"] for v in priced) * n,
                      "expected": expected} if priced else None),
        "budget_usd": budget,
        "within_budget": (None if budget is None or not priced else expected <= budget),
        "unroutable_arms": [v["key"] for v in views if not v.get("pick")],
        "method": "characters / 4 per report plus the prompt, the whole report when it fits the arm's model (less a safety margin), "
        f"else a digest of the report up to that budget; about {EXPECTED_OUTPUT_TOKENS} output tokens a review; priced at each arm's previewed "
        "model. The cost is for every report read by that arm (low/high: the cheapest/dearest arm for all of them; expected: weighted).",
    }
    return out_arms, est


async def preview_pass(
    db: AsyncSession, *, workspace_id: uuid.UUID, arms: list[dict], disclosure_report_ids: list[uuid.UUID] | None = None,
    batch_id: uuid.UUID | None = None, limit: int | None = None, sample_seed: int | None = None, budget_usd: Any = None,
    services: CallServices | None = None,
) -> dict:
    """Show before spend: the reports, what is skipped and why, whole vs digest, the cost per arm and
    what each arm WOULD pick, with the router's reason. Writes nothing, calls no model."""
    _need_jury()
    services = services or await jury.get_call_services(db, workspace_id)
    arms = normalise_arms(arms, services.catalog)
    sel = await resolve_selection(db, workspace_id, report_ids=disclosure_report_ids, batch_id=batch_id, limit=limit, sample_seed=sample_seed)
    _arms, est = await plan_pass(db, workspace_id=workspace_id, arms=arms, selection=sel, budget_usd=budget_usd, services=services)
    return est


# ── create / get / delete ───────────────────────────────────────────────────────────────────
async def create_pass(
    db: AsyncSession, *, workspace_id: uuid.UUID, arms: list[dict], name: str = "", disclosure_report_ids: list[uuid.UUID] | None = None,
    batch_id: uuid.UUID | None = None, limit: int | None = None, sample_seed: int | None = None, budget_usd: Any = None,
    created_by: str | None = None, services: CallServices | None = None,
) -> CredibilityPass:
    """A `draft` pass: the reports, each assigned to an arm, and the preview. No model is called, nothing
    is spent. Flushed, not committed."""
    _need_jury()
    services = services or await jury.get_call_services(db, workspace_id)
    arms = normalise_arms(arms, services.catalog)
    sel = await resolve_selection(db, workspace_id, report_ids=disclosure_report_ids, batch_id=batch_id, limit=limit, sample_seed=sample_seed)
    if not sel.chosen:
        raise CredibilityError("No report can be read: " + (", ".join(sorted({s.reason for s in sel.skipped})) or "none selected"))
    planned, est = await plan_pass(db, workspace_id=workspace_id, arms=arms, selection=sel, budget_usd=budget_usd, services=services)
    pid = uuid.uuid4()
    project_id = sel.chosen[0].project_id
    p = CredibilityPass(
        id=pid, workspace_id=workspace_id, project_id=project_id, name=(name or "").strip()[:120], status="draft", arms=planned,
        prompt_version=sealed.PROMPT_VERSION, estimates=est, created_by=created_by,
        budget_usd=_check_budget(budget_usd) if budget_usd is not None else None,
        selection={"mode": "batch" if batch_id else "reports", "batch_id": str(batch_id) if batch_id else None,
                   "disclosure_report_ids": [str(i) for i in disclosure_report_ids] if disclosure_report_ids else None,
                   "limit": limit, "sample_seed": sample_seed, "selected": len(sel.chosen), "skipped": len(sel.skipped),
                   "rejected_ids": list(sel.rejected)},
    )
    db.add(p)
    await db.flush()
    for c in sel.chosen:
        db.add(CredibilityReview(pass_id=pid, disclosure_report_id=c.report_id, arm_key=assign_arm(pid, c.report_id, planned), status="pending"))
    for s in sel.skipped:
        db.add(CredibilityReview(pass_id=pid, disclosure_report_id=s.report_id, arm_key="", status="skipped", error=s.reason))
    await db.flush()
    return p


async def get_pass(db: AsyncSession, *, workspace_id: uuid.UUID, pass_id: uuid.UUID) -> CredibilityPass:
    p = await db.get(CredibilityPass, pass_id)
    if p is None or p.workspace_id != workspace_id:
        raise CredibilityError("Pass not found", 404)
    return p


async def get_review(db: AsyncSession, *, workspace_id: uuid.UUID, review_id: uuid.UUID) -> tuple[CredibilityReview, CredibilityPass]:
    r = await db.get(CredibilityReview, review_id)
    p = await db.get(CredibilityPass, r.pass_id) if r is not None else None
    if r is None or p is None or p.workspace_id != workspace_id:
        raise CredibilityError("Review not found", 404)
    return r, p


async def delete_pass(db: AsyncSession, *, workspace_id: uuid.UUID, pass_id: uuid.UUID) -> None:
    """Delete a draft or cancelled pass with its reviews. A pass that ran to its end is history."""
    p = await get_pass(db, workspace_id=workspace_id, pass_id=pass_id)
    if p.status not in ("draft", "cancelled"):
        raise CredibilityError(f"Pass is {p.status}; only a draft or cancelled pass can be deleted", 409)
    res = await db.execute(delete(CredibilityPass).where(CredibilityPass.id == p.id, CredibilityPass.status.in_(("draft", "cancelled"))))
    if res.rowcount != 1:
        await db.rollback()
        raise CredibilityError("Pass changed state; nothing deleted", 409)
    await db.commit()


# ── start / cancel / resume ─────────────────────────────────────────────────────────────────
_tasks: dict[uuid.UUID, tuple[asyncio.Task, str | None]] = {}


def _spawn(pid: uuid.UUID, owner: str | None = None) -> None:
    """Run a pass in this process. A task already running it under a DIFFERENT lease owner is a stale
    runner and is cancelled and replaced; the same owner is a no-op."""
    live = _tasks.get(pid)
    if live is not None and not live[0].done():
        if live[1] == owner and owner is not None:
            return
        live[0].cancel()
    task = asyncio.create_task(run_pass(pid, owner))
    _tasks[pid] = (task, owner)
    task.add_done_callback(lambda t_, pid=pid: _tasks.pop(pid, None) if pid in _tasks and _tasks[pid][0] is t_ else None)


async def drain(timeout: float = 60.0) -> bool:
    """Wait for the running pass tasks (tests, shutdown)."""
    tasks = [t for t, _o in list(_tasks.values()) if not t.done()]
    if not tasks:
        return True
    _done, pending = await asyncio.wait(tasks, timeout=timeout)
    return not pending


async def _release_freezes(db: AsyncSession, p: CredibilityPass) -> None:
    for arm in p.arms or []:
        if arm.get("freeze_id"):
            with contextlib.suppress(jury.JuryError):
                await jury.release_freeze(db, workspace_id=p.workspace_id, freeze_id=uuid.UUID(arm["freeze_id"]))


async def start_pass(
    db: AsyncSession, *, workspace_id: uuid.UUID, pass_id: uuid.UUID, budget_usd: Any, started_by: str | None = None,
    services: CallServices | None = None, spawn: bool = True,
) -> CredibilityPass:
    """Freeze each arm's model once (the router picks; the reason is recorded), set `running` (one
    conditional update: a second start is a 409) and spawn the runner, which owns the lease the update took."""
    _need_jury()
    budget = _check_budget(budget_usd)
    p = await get_pass(db, workspace_id=workspace_id, pass_id=pass_id)
    if p.status != "draft":
        raise CredibilityError(f"Pass is {p.status}; only a draft can be started (use resume for a paused one)", 409)
    services = services or await jury.get_call_services(db, workspace_id)
    arms: list[dict] = []
    created: list[uuid.UUID] = []
    try:
        for arm in p.arms:
            row, was_created = await jury.freeze_jury(
                db, workspace_id=workspace_id, pass_ref=f"{PASS_PREFIX}{p.id}:{arm['key']}", purpose="sealed_call",
                model_policy=arm["model_policy"], k=1, min_context_window=int(arm.get("min_context_window") or 0),
                created_by=started_by, services=services,
            )
            if was_created:
                created.append(row.id)
            pick = jury.selection_of(row).picks[0]
            info = services.catalog.get(pick.model)
            ctx = int(getattr(info, "context_window", 0) or 0) or 100_000
            arms.append({**arm, "freeze_id": str(row.id), "model": pick.model, "family": pick.family, "why": pick.why,
                         "context_window": ctx, "max_output_tokens": _max_output(),
                         "max_input_tokens": sealed.usable_input_tokens(ctx, _max_output())})
    except jury.JuryError as e:
        # nothing was committed: rolling back removes the freezes made so far, so a later start can freeze again
        await db.rollback()
        # ... and for a driver whose savepoints commit early (sqlite), delete the freezes THIS call made (never one a concurrent
        # start made: it got those back as `created` False): no call was made with them, and a released freeze under the pass
        # reference would make every later start a 409
        if created:
            await db.execute(delete(RoutingFreeze).where(RoutingFreeze.id.in_(created), RoutingFreeze.status == "active")
                             .execution_options(synchronize_session=False))
            await db.commit()
        raise CredibilityError(f"arm could not be frozen: {e}", e.status) from e
    owner = new_owner()
    res = await db.execute(
        update(CredibilityPass).where(CredibilityPass.id == p.id, CredibilityPass.status == "draft")
        .values(arms=arms, budget_usd=budget, status="running", pause_reason=None, started_by=started_by, started_at=_now(),
                lease_owner=owner, lease_until=_lease_end(), updated_at=_now())
        .execution_options(synchronize_session=False)
    )
    if res.rowcount != 1:
        await db.rollback()
        raise CredibilityError("Pass was started by someone else", 409)
    for arm in arms:
        await db.execute(
            update(CredibilityReview).where(CredibilityReview.pass_id == p.id, CredibilityReview.arm_key == arm["key"])
            .values(freeze_id=uuid.UUID(arm["freeze_id"]), model=arm["model"]).execution_options(synchronize_session=False)
        )
    await db.commit()
    await db.refresh(p)
    if spawn:
        _spawn(p.id, owner)
    return p


async def book_run(db: AsyncSession, pass_id: uuid.UUID, run: Run | None, review_id: uuid.UUID | None, *, force: bool = True) -> Decimal:
    """Add a finished run's cost to the pass's spend, exactly once per run id (the ledger's primary key):
    a second settlement, a recovery, a cancel or a takeover that sees the same run books nothing. Returns
    what this call booked. Runs in the caller's transaction. A run that has not finished is not booked unless
    `force` (its owner is known to be dead): a live runner books it when it settles, and a ledger row made at
    cost 0 now would refuse the real booking later."""
    if run is None or (not force and run.status in ("queued", "running")):
        return Decimal(0)
    cost = Decimal(run.reported_cost_usd if run.reported_cost_usd is not None else (run.cost_usd or 0))
    try:
        async with db.begin_nested():
            db.add(CredibilityReviewRun(run_id=run.id, pass_id=pass_id, review_id=review_id, cost_usd=cost))
            await db.flush()
    except IntegrityError:
        # the ledger already has this run (booked once before): fine. Any other integrity error would drop spend
        # silently, so say so.
        if await db.get(CredibilityReviewRun, run.id) is None:
            log.error("credibility pass %s: run %s could not be booked to the ledger", pass_id, run.id)
            raise
        return Decimal(0)
    if cost:
        await db.execute(
            update(CredibilityPass).where(CredibilityPass.id == pass_id).values(spent_usd=CredibilityPass.spent_usd + cost)
            .execution_options(synchronize_session=False)
        )
    return cost


async def cancel_pass(db: AsyncSession, *, workspace_id: uuid.UUID, pass_id: uuid.UUID) -> CredibilityPass:
    """Stop a pass for good. Reviews that never ran become `skipped`; one whose call has already ended
    with no runner left to settle it has its run's spend booked (once) and is skipped; a call still in
    flight in a live runner is settled by that runner (its spend is booked then). Freezes are released."""
    p = await get_pass(db, workspace_id=workspace_id, pass_id=pass_id)
    lease = _aware(p.lease_until)
    owner = p.lease_owner
    was_running = p.status == "running"  # a paused pass has no runner at all; a running one with a lapsed lease may still have one
    runner_live = p.status == "running" and lease is not None and lease > _now()  # a paused pass or an expired lease has no runner
    res = await db.execute(
        update(CredibilityPass).where(CredibilityPass.id == p.id, CredibilityPass.status.in_(CANCELLABLE))
        .values(status="cancelled", pause_reason=None, lease_owner=None, lease_until=None, finished_at=_now(), updated_at=_now())
        .execution_options(synchronize_session=False)
    )
    if res.rowcount != 1:
        await db.rollback()
        await db.refresh(p)
        raise CredibilityError(f"Pass is {p.status}; nothing to cancel", 409)
    await db.execute(
        update(CredibilityReview).where(CredibilityReview.pass_id == p.id, CredibilityReview.status == "pending", CredibilityReview.run_id.is_(None))
        .values(status="skipped", error="pass cancelled", claimed_by=None).execution_options(synchronize_session=False)
    )
    held = list((await db.execute(
        select(CredibilityReview).where(
            CredibilityReview.pass_id == p.id,
            or_(CredibilityReview.status == "running", and_(CredibilityReview.status == "pending", CredibilityReview.run_id.is_not(None))),
        )
    )).scalars())
    for rv in held:
        if runner_live and rv.claimed_by == owner:
            continue  # the live runner settles it: its paid result is recorded and booked then
        run = await db.get(Run, rv.run_id) if rv.run_id is not None else None
        fresh = False
        if was_running and run is not None and run.status in ("queued", "running"):
            created_at = _aware(run.created_at)
            fresh = created_at is not None and (_now() - created_at).total_seconds() < RUN_STALE_SECONDS
        # a call still in flight (a runner whose lease lapsed but lives on) books itself when it settles; a dead one's run is booked now
        cost = Decimal(0) if fresh else await book_run(db, p.id, run, rv.id, force=True)
        rv.status, rv.error, rv.claimed_by = "skipped", "pass cancelled", None
        rv.cost_usd = cost
    await db.refresh(p)
    await _release_freezes(db, p)
    await db.commit()
    await db.refresh(p)
    return p


def _stale_running():
    """A `running` pass whose runner is gone: its lease has expired (or was never taken)."""
    return and_(CredibilityPass.status == "running", or_(CredibilityPass.lease_until.is_(None), CredibilityPass.lease_until < _now()))


def required_gates(p: CredibilityPass) -> list[str]:
    """What blocks resuming this pass: the pausing gates that are tripped and that nobody acknowledged.
    Empty unless the pass is paused."""
    if p.status != "paused":
        return []
    acked = {a.get("gate") for a in (p.acknowledged_gates or [])}
    return [g["key"] for g in ((p.diagnostics or {}).get("gates") or []) if g.get("state") == "tripped" and g["key"] in PAUSING_GATES and g["key"] not in acked]


async def resume_pass(
    db: AsyncSession, *, workspace_id: uuid.UUID, pass_id: uuid.UUID, budget_usd: Any = None, spawn: bool = True,
    acknowledge_gates: list[str] | None = None, note: str | None = None, acknowledged_by: str | None = None,
) -> CredibilityPass:
    """Continue a paused (or failed) pass, or a `running` one whose runner's lease has expired. One
    conditional update decides who runs: a second resume is a 409. A tripped pausing gate must be
    acknowledged to continue: `acknowledge_gates` has to name exactly the gates in
    `required_acknowledgements` (a gate that is not one of them is a 409, a tripped one left out is a
    409) and needs a non-empty `note` (422)."""
    _need_jury()
    p = await get_pass(db, workspace_id=workspace_id, pass_id=pass_id)
    budget = _check_budget(budget_usd) if budget_usd is not None else (Decimal(p.budget_usd) if p.budget_usd is not None else None)
    if budget is None or Decimal(p.spent_usd or 0) >= budget:
        raise CredibilityError(f"The budget (${p.budget_usd}) is already spent (${p.spent_usd}): resume with a larger budget_usd", 409)
    required = required_gates(p)
    asked = list(dict.fromkeys(acknowledge_gates or []))
    wrong = [g for g in asked if g not in required]
    if wrong:
        raise CredibilityError(
            f"Cannot acknowledge {', '.join(wrong)}: only a tripped gate that blocks the resume can be acknowledged (here: {', '.join(required) or 'none'})", 409
        )
    open_gates = [g for g in required if g not in asked]
    if open_gates:
        raise CredibilityError(f"Gate(s) tripped and not acknowledged: {', '.join(open_gates)}. Resume with acknowledge_gates and a note to continue anyway.", 409)
    if asked and not (note or "").strip():
        raise CredibilityError("A note is required when acknowledging a gate: say why the tripped gate can be accepted")
    acks = list(p.acknowledged_gates or [])
    acks += [{"gate": g, "by": acknowledged_by, "at": _now().isoformat(), "note": (note or "")[:500] or None} for g in asked]
    owner = new_owner()
    res = await db.execute(
        update(CredibilityPass).where(CredibilityPass.id == p.id, or_(CredibilityPass.status.in_(("paused", "failed")), _stale_running()))
        .values(status="running", pause_reason=None, budget_usd=budget, lease_owner=owner, lease_until=_lease_end(), updated_at=_now(),
                finished_at=None, acknowledged_gates=acks)
        .execution_options(synchronize_session=False)
    )
    if res.rowcount != 1:
        await db.rollback()
        await db.refresh(p)
        raise CredibilityError(f"Pass is {p.status}; only a paused or failed one (or one whose runner stopped) can be resumed", 409)
    await db.commit()
    await db.refresh(p)
    if spawn:
        _spawn(p.id, owner)
    return p


async def pause_pass(pid: uuid.UUID, *, owner: str | None = None, reason: str = "interrupted", session_factory: Any = None) -> bool:
    """Pause exactly this pass (never another) and release its lease. With `owner`, only if that runner still holds it."""
    factory = session_factory or get_session_factory()
    cond = [CredibilityPass.id == pid, CredibilityPass.status == "running"]
    if owner:
        cond.append(CredibilityPass.lease_owner == owner)
    async with factory() as db:
        res = await db.execute(
            update(CredibilityPass).where(*cond).values(status="paused", pause_reason=reason, lease_owner=None, lease_until=None, updated_at=_now())
        )
        await db.commit()
        return bool(res.rowcount)


async def recover_running(session_factory: Any = None) -> int:
    """At boot: a pass left `running` whose lease has expired is paused (`restart`), never resumed on its own."""
    factory = session_factory or get_session_factory()
    async with factory() as db:
        res = await db.execute(
            update(CredibilityPass).where(_stale_running())
            .values(status="paused", pause_reason="restart", lease_owner=None, lease_until=None, updated_at=_now())
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        return int(res.rowcount or 0)


# ── gates and arm statistics (pure) ─────────────────────────────────────────────────────────
def thresholds() -> dict[str, Any]:
    s = get_settings()
    return {
        "clumping_pause": float(s.eval_gate_clumping_pause), "clumping_warn": float(s.eval_gate_clumping_warn),
        "clumping_min_n": int(s.eval_gate_clumping_min_n), "failure_rate_pause": float(s.eval_gate_failure_rate_pause),
        "failure_min_attempts": FAIL_MIN_ATTEMPTS,
    }


def compute_gates(rows: list[tuple[str, int | None]], th: dict[str, Any] | None = None, acknowledged: list[dict] | None = None) -> list[dict]:
    """The two gates over the reviews so far (`rows`: status, score). Each is
    `{key, state, value, n, threshold, detail}` with `state` ok | warn | tripped | insufficient_data."""
    th = th or thresholds()
    scores = [sc for st, sc in rows if st == "done" and sc is not None]
    failed = sum(1 for st, _ in rows if st == "failed")
    n = len(scores)
    if n < th["clumping_min_n"]:
        clump = {"state": "insufficient_data", "value": (sum(1 for x in scores if 6 <= x <= 8) / n if n else None), "n": n,
                 "detail": f"{n} done review(s); the share of scores in 6-8 needs {th['clumping_min_n']}."}
    else:
        share = sum(1 for x in scores if 6 <= x <= 8) / n
        state = "tripped" if share > th["clumping_pause"] else ("warn" if share > th["clumping_warn"] else "ok")
        clump = {"state": state, "value": share, "n": n,
                 "detail": f"{share:.0%} of {n} scores fall in 6-8 (warn above {th['clumping_warn']:.0%}, pause above {th['clumping_pause']:.0%}): "
                           "a judge that gives most reports the middle score is not separating them."}
    attempted = n + failed
    if attempted < th["failure_min_attempts"]:
        fail = {"state": "insufficient_data", "value": (failed / attempted if attempted else None), "n": attempted,
                "detail": f"{attempted} review(s) have run to an end; the failure rate needs {th['failure_min_attempts']}."}
    else:
        rate = failed / attempted
        fail = {"state": "tripped" if rate > th["failure_rate_pause"] else "ok", "value": rate, "n": attempted,
                "detail": f"{failed} of {attempted} reviews failed ({rate:.0%}); the pass pauses above {th['failure_rate_pause']:.0%}."}
    acked = {a.get("gate"): a for a in (acknowledged or [])}
    out = []
    for key, g in (("score_clumping", clump), ("failure_rate", fail)):
        thr = ({"warn": th["clumping_warn"], "pause": th["clumping_pause"], "min_n": th["clumping_min_n"]} if key == "score_clumping"
               else {"pause": th["failure_rate_pause"], "min_n": th["failure_min_attempts"]})
        item = {"key": key, **g, "threshold": thr}
        if key in acked:
            item["acknowledged_by"] = acked[key].get("by")
            item["acknowledged_note"] = acked[key].get("note")
        out.append(item)
    return out


def finalize_gates(gates: list[dict]) -> list[dict]:
    """A finished pass: a gate that never got its samples shows as `warn`, not as a quiet pass."""
    return [({**g, "state": "warn", "detail": f"not evaluated: only {g['n']} samples. {g['detail']}"} if g["state"] == "insufficient_data" else g) for g in gates]


def arm_stats(rows: list[tuple], arms: list[dict], costs: dict[str, Any] | None = None) -> list[dict]:
    """Per arm: reviews by status, mean and median score, the histogram of scores 1..10 (a list of 10,
    index 0 = score 1), the share of reviews read as a digest and the cost. `rows`: (arm key, status, score,
    input mode); `costs`: arm key -> spend from the run ledger (so the arms add up to the pass's spend)."""
    costs = costs or {}
    out = []
    for arm in arms:
        mine = [r for r in rows if r[0] == arm["key"]]
        scores = [r[2] for r in mine if r[1] == "done" and r[2] is not None]
        hist = [sum(1 for s in scores if s == v) for v in range(1, 11)]
        read = [r for r in mine if len(r) > 3 and r[3]]
        out.append({
            "key": arm["key"], "label": arm.get("label"), "weight": arm.get("weight"), "model": arm.get("model"),
            "assigned": len(mine), "n_done": len(scores), "n_failed": sum(1 for r in mine if r[1] == "failed"),
            "n_pending": sum(1 for r in mine if r[1] in ("pending", "running")),
            "mean": (sum(scores) / len(scores) if scores else None), "median": (float(statistics.median(scores)) if scores else None),
            "histogram": hist, "n_digest": sum(1 for r in read if r[3] == "digest"),
            "digest_share": (sum(1 for r in read if r[3] == "digest") / len(read) if read else None),
            "cost_usd": float(Decimal(str(costs.get(arm["key"], 0) or 0))),
        })
    return out


async def load_arm_data(db: AsyncSession, pid: uuid.UUID) -> tuple[list[tuple], dict[str, Decimal]]:
    """(review rows for the gates and arm statistics, spend per arm from the run ledger joined to its reviews)."""
    rows = [tuple(r) for r in (await db.execute(
        select(CredibilityReview.arm_key, CredibilityReview.status, CredibilityReview.credibility_score, CredibilityReview.input_mode)
        .where(CredibilityReview.pass_id == pid)
    )).all()]
    costs: dict[str, Decimal] = {}
    for key, total in (await db.execute(
        select(func.coalesce(CredibilityReview.arm_key, ""), func.sum(CredibilityReviewRun.cost_usd))
        .select_from(CredibilityReviewRun).outerjoin(CredibilityReview, CredibilityReview.id == CredibilityReviewRun.review_id)
        .where(CredibilityReviewRun.pass_id == pid).group_by(CredibilityReview.arm_key)
    )).all():
        costs[key] = Decimal(str(total or 0))
    return rows, costs


# ── the runner ──────────────────────────────────────────────────────────────────────────────
_sem_state: dict[str, Any] = {"loop": None, "size": 0, "sem": None}


def _semaphore() -> asyncio.Semaphore:
    """One limit for the whole process, shared by every pass: the machine is small."""
    loop = asyncio.get_running_loop()
    size = max(1, int(get_settings().eval_credibility_concurrency))
    st = _sem_state
    if st["loop"] is not loop or st["size"] != size:
        st.update(loop=loop, size=size, sem=asyncio.Semaphore(size))
    return st["sem"]


class _Sealed:
    """The `profile` `execute_direct` runs: one frozen-model call over one sealed payload."""

    direct = True

    def __init__(self, *, payload: sealed.SealedPayload, arm: Any, max_output: int) -> None:
        self.payload, self.arm, self.max_output = payload, arm, max_output
        self.answer: dict | None = None
        self.answered_by: str | None = None

    async def run_direct(self, ctx):
        res = await ctx.call_json(
            purpose="credibility_review", system=self.payload.system, prompt=self.payload.user, schema=sealed.SCHEMA,
            est_input_tokens=self.payload.est_input_tokens, max_output_tokens=self.max_output, freeze=self.arm.sel,
            freeze_id=self.arm.freeze_id, pass_ref=self.arm.pass_ref, juror_index=0,
        )
        self.answer, self.answered_by = res.payload, res.model
        return direct.DirectResult(outcome={"credibility_score": res.payload.get("credibility_score")})


class _Runner:
    def __init__(self, pid: uuid.UUID, owner: str | None = None) -> None:
        self.pid = pid
        self.owner = owner or new_owner()
        self.factory = get_session_factory()
        self.spent = Decimal(0)
        self.reserved = Decimal(0)
        self.budget: Decimal | None = None
        self.budget_hit = False
        self.stopped = False  # cancelled, or the lease was lost
        self.pause_reason: str | None = None
        self.provider_streak = 0
        self.observed: dict[str, tuple[Decimal, int]] = {}  # arm -> (total cost, reviews)
        self.gate_lock = asyncio.Lock()

    # -- lease -------------------------------------------------------------------------------
    def _lease_ok(self):
        """True in SQL only while this runner still holds a running pass: put it in the WHERE of every
        write that must not outlive the lease, so the check and the write are one statement."""
        return exists().where(CredibilityPass.id == self.pid, CredibilityPass.lease_owner == self.owner, CredibilityPass.status == "running")

    def _may_settle(self):
        """The lease, or a pass that was cancelled while this call was in flight (its result is still recorded)."""
        return or_(self._lease_ok(), exists().where(CredibilityPass.id == self.pid, CredibilityPass.status == "cancelled"))

    async def _claim(self) -> bool:
        async with self.factory() as db:
            res = await db.execute(
                update(CredibilityPass)
                .where(
                    CredibilityPass.id == self.pid, CredibilityPass.status == "running",
                    or_(CredibilityPass.lease_owner == self.owner, CredibilityPass.lease_owner.is_(None),
                        CredibilityPass.lease_until.is_(None), CredibilityPass.lease_until < _now()),
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
                        update(CredibilityPass)
                        .where(CredibilityPass.id == self.pid, CredibilityPass.lease_owner == self.owner, CredibilityPass.status == "running")
                        .values(lease_until=_lease_end())
                    )
                    await db.commit()
                if res.rowcount != 1:
                    self.stopped = True  # someone else owns it now, or it was cancelled
                    return
            except Exception:  # noqa: BLE001 - one missed beat is not a lost lease
                log.warning("credibility pass %s heartbeat failed", self.pid, exc_info=True)

    async def _release_lease(self) -> None:
        with contextlib.suppress(Exception):
            async with self.factory() as db:
                await db.execute(
                    update(CredibilityPass).where(CredibilityPass.id == self.pid, CredibilityPass.lease_owner == self.owner)
                    .values(lease_owner=None, lease_until=None)
                )
                await db.commit()

    # -- setup -------------------------------------------------------------------------------
    async def _load(self) -> bool:
        async with self.factory() as db:
            p = await db.get(CredibilityPass, self.pid)
            if p is None or p.status != "running" or not p.arms or any(not a.get("freeze_id") for a in p.arms):
                return False
            self.workspace_id = p.workspace_id
            self.acked: list[dict] = list(p.acknowledged_gates or [])
            self.spent = Decimal(p.spent_usd or 0)
            self.budget = Decimal(p.budget_usd) if p.budget_usd is not None else None
            self.arms: dict[str, SimpleNamespace] = {}
            for a in p.arms:
                freeze = await db.get(RoutingFreeze, uuid.UUID(a["freeze_id"]))
                self.arms[a["key"]] = SimpleNamespace(
                    key=a["key"], policy=dict(a["model_policy"]), sel=jury.selection_of(freeze), freeze_id=freeze.id,
                    pass_ref=freeze.pass_ref, model=a["model"], max_input_tokens=int(a["max_input_tokens"]),
                    max_output=int(a.get("max_output_tokens") or _max_output()),
                )
            self.arm_list = [dict(a) for a in p.arms]
            self.project_id = p.project_id or (await db.execute(
                select(Project.id).where(Project.workspace_id == p.workspace_id).order_by(Project.created_at).limit(1))).scalar_one()
            for key, total, n in (await db.execute(
                select(CredibilityReview.arm_key, func.sum(CredibilityReview.cost_usd), func.count()).where(
                    CredibilityReview.pass_id == self.pid, CredibilityReview.status == "done").group_by(CredibilityReview.arm_key)
            )).all():  # what reviews have cost so far (a resume starts from it)
                self.observed[key] = (Decimal(str(total or 0)), int(n))
            self.harness_id = (await ensure_jury_harness(db, p.workspace_id)).id
            self.catalog = (await direct.get_call_services(db, p.workspace_id)).catalog  # the one the calls use
            await db.commit()
        return True

    async def _recover_orphans(self) -> None:
        """Reviews a dead (or replaced) runner left claimed or half played: one whose run is still in
        flight is left alone (never played twice); otherwise its run's spend is booked (once, by run id)
        and it goes back to pending."""
        async with self.factory() as db:
            rows = list((await db.execute(
                select(CredibilityReview).where(
                    CredibilityReview.pass_id == self.pid,
                    or_(CredibilityReview.status == "running", and_(CredibilityReview.status == "pending", CredibilityReview.run_id.is_not(None))),
                )
            )).scalars())
            booked = Decimal(0)
            for rv in rows:
                run = await db.get(Run, rv.run_id) if rv.run_id is not None else None
                if run is not None and run.status in ("queued", "running"):
                    created = _aware(run.created_at)
                    if created is None or (_now() - created).total_seconds() < RUN_STALE_SECONDS:
                        continue  # in flight: wait for it
                booked += await book_run(db, self.pid, run, rv.id)
                rv.status, rv.run_id, rv.claimed_by = "pending", None, None
            await db.commit()
            self.spent += booked

    # -- state ---------------------------------------------------------------------------------
    async def _status(self) -> str | None:
        async with self.factory() as db:
            return (await db.execute(select(CredibilityPass.status).where(CredibilityPass.id == self.pid))).scalar_one_or_none()

    async def _set(self, **values: Any) -> bool:
        """Write to the pass only while it is still running and still ours (a cancel or a new lease
        holder stands). False stops this runner."""
        async with self.factory() as db:
            res = await db.execute(
                update(CredibilityPass)
                .where(CredibilityPass.id == self.pid, CredibilityPass.status == "running", CredibilityPass.lease_owner == self.owner)
                .values(updated_at=_now(), **values)
            )
            await db.commit()
        if res.rowcount != 1:
            self.stopped = True
        return res.rowcount == 1

    async def _diagnostics(self, *, final: bool = False) -> dict[str, Any]:
        async with self.factory() as db:
            rows, costs = await load_arm_data(db, self.pid)
        gates = compute_gates([(r[1], r[2]) for r in rows], thresholds(), self.acked)
        return {"gates": finalize_gates(gates) if final else gates, "thresholds": thresholds(),
                "arms": arm_stats(rows, self.arm_list, costs), "computed_at": _now().isoformat()}

    async def _check_gates(self) -> None:
        """After a review ends: store the gates, and pause on a tripped, unacknowledged one."""
        async with self.gate_lock:
            diag = await self._diagnostics()
            await self._set(diagnostics=diag)
            acked = {a.get("gate") for a in self.acked}
            for g in diag["gates"]:
                if g["state"] == "tripped" and g["key"] in PAUSING_GATES and g["key"] not in acked:
                    self.pause_reason = self.pause_reason or g["key"]

    async def _pause(self, reason: str) -> bool:
        diag = await self._diagnostics()
        return await self._set(status="paused", pause_reason=reason[:500], lease_owner=None, lease_until=None, diagnostics=diag)

    async def _finish(self) -> None:
        diag = await self._diagnostics(final=True)
        if await self._set(status="completed", pause_reason=None, lease_owner=None, lease_until=None, finished_at=_now(), diagnostics=diag):
            async with self.factory() as db:
                p = await db.get(CredibilityPass, self.pid)
                await _release_freezes(db, p)  # the pass is over
                await db.commit()

    # -- run -----------------------------------------------------------------------------------
    async def go(self) -> None:
        if not await self._claim():
            return  # another runner holds the lease (or it is no longer running)
        beat = asyncio.create_task(self._heartbeat())
        try:
            await self._go()
        except asyncio.CancelledError:
            # interrupted (Ctrl-C, shutdown, a newer runner replacing this task): leave the pass paused,
            # not "running" with nobody playing it; only if the lease is still ours.
            try:
                await asyncio.shield(self._pause("interrupted"))
            except Exception:  # noqa: BLE001
                log.warning("credibility pass %s could not be paused after its interruption", self.pid, exc_info=True)
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
        deadline: float | None = None
        loop = asyncio.get_running_loop()
        while True:
            if self.stopped or (await self._status()) != "running":
                return
            async with self.factory() as db:
                ids = list((await db.execute(
                    select(CredibilityReview.id).where(CredibilityReview.pass_id == self.pid, CredibilityReview.status == "pending")
                    .order_by(CredibilityReview.created_at, CredibilityReview.id).limit(CHUNK)
                )).scalars())
                claimed = (await db.execute(
                    select(func.count()).select_from(CredibilityReview).where(CredibilityReview.pass_id == self.pid, CredibilityReview.status == "running")
                )).scalar_one()
            if ids:
                results = await asyncio.gather(*(self._play(i) for i in ids), return_exceptions=True)
                for res in results:
                    if isinstance(res, BaseException) and not isinstance(res, asyncio.CancelledError):
                        raise res
            reason = self.pause_reason or ("budget" if self.budget_hit else None)
            if reason:
                await self._pause(reason)
                return
            if self.stopped:
                return
            if ids:
                deadline = None  # progress: the wait for another runner's reviews starts over
                continue
            if not claimed:
                await self._finish()
                return
            # reviews claimed by someone else whose run is still in flight: wait, then look again
            deadline = deadline or loop.time() + RUN_STALE_SECONDS + 30
            if loop.time() > deadline:
                await self._pause("error: reviews claimed by another runner never finished")
                return
            await asyncio.sleep(ORPHAN_POLL_SECONDS)
            await self._recover_orphans()

    def _estimate(self, arm: Any, chars: int) -> Decimal:
        """What this review is expected to cost, to keep the budget a stop and not a suggestion: the
        catalog price of the frozen model for this report (the whole report or the digest budget) and
        the answer cap, or what reviews of this arm have actually cost on average, whichever is higher."""
        info = self.catalog.get(arm.model)
        est = Decimal(0)
        if info is not None:
            tin = min((chars + 3) // 4 + sealed.estimate_tokens(sealed.SYSTEM_PROMPT) + sealed.USER_OVERHEAD_TOKENS, arm.max_input_tokens)
            est = Decimal(str(info.cost_usd(tin, arm.max_output)))
        seen = self.observed.get(arm.key)
        if seen:
            est = max(est, seen[0] / seen[1])
        elif self.observed:
            est = max(est, max(c / n for c, n in self.observed.values()))
        return est

    async def _play(self, review_id: uuid.UUID) -> None:
        async with _semaphore():
            if self.stopped or self.budget_hit or self.pause_reason:
                return
            if (await self._status()) != "running":
                self.stopped = True
                return
            async with self.factory() as db:
                row = await db.get(CredibilityReview, review_id)
                if row is None or row.status != "pending":
                    return
                report = await db.get(DisclosureReport, row.disclosure_report_id)
                rv = SimpleNamespace(id=row.id, report_id=row.disclosure_report_id, arm_key=row.arm_key, input_budget=row.input_budget)
                arm = self.arms.get(row.arm_key)
                est = self._estimate(arm, int(report.markdown_chars or 0)) if arm is not None and report is not None else Decimal(0)
                if self.budget is not None and self.spent + self.reserved + est > self.budget:
                    self.budget_hit = True
                    return
                self.reserved += est  # reserved before any await, so concurrent reviews cannot all slip under the budget
            try:
                async with self.factory() as db:
                    # one statement: still pending AND this runner still holds the running pass
                    claim = await db.execute(
                        update(CredibilityReview)
                        .where(CredibilityReview.id == review_id, CredibilityReview.status == "pending", self._lease_ok())
                        .values(status="running", claimed_by=self.owner, claimed_at=_now())
                    )
                    await db.commit()
                if claim.rowcount != 1:
                    if not await self._holds_lease():
                        self.stopped = True
                    return  # another runner has it, or the lease is gone
                try:
                    await self._play_claimed(rv, arm)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001 - a transient database or blob-store error, not the model
                    log.warning("credibility pass %s review %s: %s", self.pid, review_id, e, exc_info=True)
                    await self._requeue(review_id, f"{type(e).__name__}: {e}")
            finally:
                self.reserved -= est

    async def _holds_lease(self) -> bool:
        async with self.factory() as db:
            return bool((await db.execute(
                select(func.count()).select_from(CredibilityPass).where(
                    CredibilityPass.id == self.pid, CredibilityPass.lease_owner == self.owner, CredibilityPass.status == "running")
            )).scalar_one())

    async def _requeue(self, review_id: uuid.UUID, message: str) -> None:
        """Put a review whose play was cut short by a transient error back to pending (booking what its
        run spent, once) and pause the pass with the error; resume replays it."""
        try:
            async with self.factory() as db:
                rv = await db.get(CredibilityReview, review_id)
                booked = Decimal(0)
                if rv is not None and rv.run_id is not None:
                    booked = await book_run(db, self.pid, await db.get(Run, rv.run_id), rv.id)
                await db.execute(
                    update(CredibilityReview).where(CredibilityReview.id == review_id, CredibilityReview.claimed_by == self.owner, CredibilityReview.status == "running")
                    .values(status="pending", run_id=None, claimed_by=None)
                )
                await db.commit()
                self.spent += booked
        except Exception:  # noqa: BLE001 - the next runner's orphan recovery will replay it
            log.warning("credibility pass %s could not requeue review %s", self.pid, review_id, exc_info=True)
        self.pause_reason = self.pause_reason or f"error: {message}"[:500]

    async def _play_claimed(self, rv: Any, arm: Any) -> None:
        if arm is None:
            await self._settle(rv.id, None, None, None, f"unknown arm {rv.arm_key!r}")
            return
        try:
            async with self.factory() as db:
                payload = await sealed.credibility_payload(db, rv.report_id, max_input_tokens=rv.input_budget or arm.max_input_tokens)
        except sealed.SealedError as e:
            await self._settle(rv.id, None, None, arm, f"payload_error: {e.kind}: {e}")
            return
        run_id = uuid.uuid4()
        try:
            async with self.factory() as db:  # a short transaction: the session is closed before the call
                db.add(Run(
                    id=run_id, project_id=self.project_id, harness_id=self.harness_id, task_type=direct.TASK_TYPE,
                    task_input={SEALED_CALL_KEY: True, STRUCTURED_CALL_KEY: True, EVAL_CREDIBILITY_KEY: str(self.pid),
                                EVAL_CREDIBILITY_REVIEW_KEY: str(rv.id)},
                    document_ids=[],
                ))
                await db.flush()
                res = await db.execute(
                    update(CredibilityReview)
                    .where(CredibilityReview.id == rv.id, CredibilityReview.claimed_by == self.owner, CredibilityReview.status == "running", self._lease_ok())
                    .values(run_id=run_id, input_mode=payload.input_mode, report_sha256=payload.report_sha256,
                            prompt_sha256=payload.prompt_sha256, model=arm.model, freeze_id=arm.freeze_id)
                )
                if res.rowcount != 1:  # replaced meanwhile: spend nothing
                    await db.rollback()
                    self.stopped = True
                    return
                await db.commit()
            profile = _Sealed(payload=payload, arm=arm, max_output=arm.max_output)
            await direct.execute_direct(run_id, profile, self.factory, model_policy=arm.policy)
        except asyncio.CancelledError:
            await asyncio.shield(self._close_unstarted(run_id))
            raise
        await self._settle(rv.id, run_id, profile, arm, None)

    async def _close_unstarted(self, run_id: uuid.UUID) -> None:
        """A run the interruption cut off before `execute_direct` started it (still `queued`; a run it
        started, it closes out itself) is cancelled, with its post-run hooks."""
        try:
            async with self.factory() as db:
                run = await db.get(Run, run_id)
                if run is None or run.status != "queued":
                    return
                run.status, run.error, run.finished_at = "cancelled", INTERRUPTED_RUN_ERROR, _now()
                await db.commit()
                await get_extension_registry().run_post_run_hooks(db, run, self.workspace_id)
        except Exception:  # noqa: BLE001 - the stale window still closes it out
            log.warning("credibility pass %s could not close out run %s after its interruption", self.pid, run_id, exc_info=True)

    async def _settle(self, review_id: uuid.UUID, run_id: uuid.UUID | None, profile: _Sealed | None, arm: Any, error: str | None) -> None:
        """Record a review's result in ONE conditional statement (still claimed by this runner, lease
        still ours or the pass cancelled). The run's cost is booked either way, once, by run id: a runner
        that lost its lease mid-call still pays for the call in the accounts, and stops."""
        outage: str | None = None
        retry = False
        shrink = False  # a context overflow: one more try with a smaller budget
        cancelled = False
        ok = False
        cost = Decimal(0)
        values: dict[str, Any] = {}
        async with self.factory() as db:
            rv = await db.get(CredibilityReview, review_id)
            run = None
            answer: dict | None = None
            if run_id is not None:
                run = (await db.execute(select(Run).where(Run.id == run_id).execution_options(populate_existing=True))).scalar_one()
                cost = Decimal(run.reported_cost_usd if run.reported_cost_usd is not None else (run.cost_usd or 0))
                values.update(cost_usd=cost, input_tokens=run.input_tokens or 0, output_tokens=run.output_tokens or 0)
                kind = (run.error or "").split(":", 1)[0]
                if run.status != "completed":
                    if run.started_at is None:  # the pre-run gate refused: credits or a limit
                        outage = "credits"
                    elif kind in OUTAGE_KINDS:
                        outage = "model_unavailable"
                    elif kind == "provider" and rv.attempts + 1 < MAX_ATTEMPTS:
                        retry = True
                    elif kind == "context_overflow" and rv.input_budget is None and arm is not None:
                        shrink = True
                if run.status != "completed" or profile is None or profile.answer is None:
                    error = error or run.error or f"run {run.status}"
                elif arm is not None and profile.answered_by != arm.model:
                    error = f"answered by {profile.answered_by}, not the frozen {arm.model}"
                else:
                    try:
                        answer = sealed.clean_answer(profile.answer)
                    except ValueError as e:
                        error = f"invalid_answer: {e}"
            pstatus = (await db.execute(select(CredibilityPass.status).where(CredibilityPass.id == self.pid))).scalar_one_or_none()
            if pstatus == "cancelled" and (outage or retry or shrink):
                # the pass was cancelled while this call was in flight: nothing will run it again
                cancelled, outage, retry, shrink = True, None, False, False
                values = dict(status="skipped", error="pass cancelled", claimed_by=None, cost_usd=cost)
            elif shrink:
                # the estimate (characters / 4) fitted the window but the provider counted more tokens: once more, with a digest
                # sized well under the arm's budget and under this report's whole size; the budget is kept on the review so
                # verify rebuilds the same prompt
                report = await db.get(DisclosureReport, rv.disclosure_report_id)
                whole = (int(report.markdown_chars or 0) + 3) // 4 + sealed.estimate_tokens(sealed.SYSTEM_PROMPT) + sealed.USER_OVERHEAD_TOKENS
                budget = max(1000, int(0.6 * min(arm.max_input_tokens, whole)))
                values = dict(status="pending", run_id=None, claimed_by=None, error=None, cost_usd=Decimal(0), input_budget=budget,
                              attempts=CredibilityReview.attempts + 1)
            elif outage or retry:
                # Not the review's fault: it goes back to pending (a provider failure counts an attempt), and the pass pauses
                # on an outage or after repeated provider failures. What the run booked counts as spend.
                values = dict(status="pending", run_id=None, claimed_by=None, error=None, cost_usd=Decimal(0))
                if retry:
                    values["attempts"] = CredibilityReview.attempts + 1
            elif error:
                values.update(status="failed", error=error[:1000], completed_at=_now(), attempts=CredibilityReview.attempts + (1 if run_id else 0))
            else:
                values.update(status="done", completed_at=_now(), error=None, attempts=CredibilityReview.attempts + 1,
                              credibility_score=answer["credibility_score"], pushbacks=answer["pushbacks"],
                              story_breaks=answer["story_breaks"], summary=answer["summary"])
                ok = True
            booked = await book_run(db, self.pid, run, review_id)
            res = await db.execute(
                update(CredibilityReview)
                .where(CredibilityReview.id == review_id, CredibilityReview.claimed_by == self.owner, CredibilityReview.status == "running", self._may_settle())
                .values(**values)
            )
            await db.commit()
        self.spent += booked
        if res.rowcount != 1:
            self.stopped = True  # the lease moved on: this result is not ours to record
            return
        key = arm.key if arm is not None else ""
        if cancelled or shrink:
            return
        if outage:
            self.pause_reason = self.pause_reason or outage
            return
        if retry:
            self.provider_streak += 1
            if self.provider_streak >= PROVIDER_STREAK_PAUSE:
                self.pause_reason = self.pause_reason or "provider_unavailable"
            return
        if run_id is not None:
            c, n = self.observed.get(key, (Decimal(0), 0))
            self.observed[key] = (c + values.get("cost_usd", Decimal(0)), n + 1)
        if ok:
            self.provider_streak = 0
        elif error and error.startswith("provider:"):  # a provider failure that used up the review's attempts
            self.provider_streak += 1
            if self.provider_streak >= PROVIDER_STREAK_PAUSE:
                self.pause_reason = self.pause_reason or "provider_unavailable"
        await self._check_gates()


async def run_pass(pid: uuid.UUID, owner: str | None = None) -> None:
    """The background task of one pass. Never raises except cancellation. `owner` is the lease holder
    `start`/`resume` registered; without it the runner takes the lease itself and returns at once if
    another runner holds it."""
    try:
        await _Runner(pid, owner).go()
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 - an internal error marks the pass, not the process
        log.exception("credibility pass %s crashed", pid)
        try:
            async with get_session_factory()() as db:
                res = await db.execute(
                    update(CredibilityPass).where(CredibilityPass.id == pid, CredibilityPass.status == "running")
                    .values(status="failed", pause_reason=f"error: {type(e).__name__}: {e}"[:500], lease_owner=None, lease_until=None, finished_at=_now())
                )
                p = await db.get(CredibilityPass, pid)
                if res.rowcount and p is not None:  # the pass is over (resume can still use the recorded selections)
                    await _release_freezes(db, p)
                await db.commit()
        except Exception:  # noqa: BLE001
            log.warning("could not mark credibility pass %s failed", pid, exc_info=True)


# ── verify (the payload audit) ──────────────────────────────────────────────────────────────
async def verify_review(db: AsyncSession, *, workspace_id: uuid.UUID, review_id: uuid.UUID) -> dict[str, Any]:
    """Rebuild the review's payload from its report (with the budget its arm froze) and compare the hash of
    the exact prompt and of the report text with what the review stored. `ok` is true only when both
    match (and the input mode agrees); a missing or changed report makes it false, with the reason."""
    rv, p = await get_review(db, workspace_id=workspace_id, review_id=review_id)
    out: dict[str, Any] = {"review_id": str(rv.id), "pass_id": str(p.id), "disclosure_report_id": str(rv.disclosure_report_id),
                           "ok": False, "reason": None, "input_mode": rv.input_mode,
                           "report_sha256": {"stored": rv.report_sha256, "rebuilt": None, "match": False},
                           "prompt_sha256": {"stored": rv.prompt_sha256, "rebuilt": None, "match": False}}
    if not rv.prompt_sha256 or not rv.report_sha256:
        out["reason"] = "this review has no recorded payload (it never reached the model)"
        return out
    arm = next((a for a in (p.arms or []) if a["key"] == rv.arm_key), None)
    if arm is None or not arm.get("max_input_tokens"):
        out["reason"] = "the pass records no input budget for this review's arm"
        return out
    try:
        payload = await sealed.credibility_payload(db, rv.disclosure_report_id, max_input_tokens=int(rv.input_budget or arm["max_input_tokens"]))
    except sealed.SealedError as e:
        out["reason"] = f"the report can no longer be read as it was: {e.kind}: {e}"
        return out
    out["report_sha256"].update(rebuilt=payload.report_sha256, match=payload.report_sha256 == rv.report_sha256)
    out["prompt_sha256"].update(rebuilt=payload.prompt_sha256, match=payload.prompt_sha256 == rv.prompt_sha256)
    out["rebuilt_input_mode"] = payload.input_mode
    out["ok"] = bool(out["report_sha256"]["match"] and out["prompt_sha256"]["match"] and payload.input_mode == rv.input_mode)
    if not out["ok"]:
        out["reason"] = ("the report's text has changed since the review" if not out["report_sha256"]["match"]
                         else "the rebuilt prompt differs from the one that was sent")
    return out


# ── reading ─────────────────────────────────────────────────────────────────────────────────
async def counts_for(db: AsyncSession, pass_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict[str, int]]:
    out: dict[uuid.UUID, dict[str, int]] = {pid: {} for pid in pass_ids}
    if not pass_ids:
        return out
    for pid, status, n in (await db.execute(
        select(CredibilityReview.pass_id, CredibilityReview.status, func.count()).where(CredibilityReview.pass_id.in_(pass_ids))
        .group_by(CredibilityReview.pass_id, CredibilityReview.status)
    )).all():
        out[pid][status] = int(n)
    return out


def _counts_out(c: dict[str, int]) -> dict[str, int]:
    keys = ("pending", "running", "done", "failed", "skipped")
    return {**{k: int(c.get(k, 0)) for k in keys}, "total": sum(int(v) for v in c.values())}


def pass_out(p: CredibilityPass, counts: dict[str, int] | None = None, stats: list[dict] | None = None, *, with_selection_ids: bool = False) -> dict[str, Any]:
    lease = _aware(p.lease_until)
    diag = dict(p.diagnostics or {})
    gates = diag.get("gates") or []
    if p.status == "completed":
        gates = finalize_gates(gates)
    return {
        "id": str(p.id), "name": p.name, "status": p.status, "pause_reason": p.pause_reason, "prompt_version": p.prompt_version,
        "budget_usd": float(p.budget_usd) if p.budget_usd is not None else None, "spent_usd": float(p.spent_usd or 0),
        "arms": [{**a, "frozen": bool(a.get("freeze_id"))} for a in (p.arms or [])],
        "counts": _counts_out(counts or {}),
        "selection": p.selection if with_selection_ids else {k: v for k, v in (p.selection or {}).items() if k != "disclosure_report_ids"},
        "estimates": p.estimates,
        "gates": gates, "thresholds": diag.get("thresholds") or thresholds(),
        "arm_stats": stats if stats is not None else (diag.get("arms") or []),
        "diagnostics_computed_at": diag.get("computed_at"),
        "required_acknowledgements": required_gates(p), "acknowledged_gates": list(p.acknowledged_gates or []),
        "lease_until": lease.isoformat() if lease else None,
        "stalled": p.status == "running" and (lease is None or lease < _now()),  # running, but nothing is playing it: resume it
        "created_by": p.created_by, "started_by": p.started_by,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "started_at": p.started_at.isoformat() if p.started_at else None,
        "finished_at": p.finished_at.isoformat() if p.finished_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


async def pass_detail(db: AsyncSession, p: CredibilityPass) -> dict[str, Any]:
    """The pass with live counts and per-arm statistics computed now (not the runner's last snapshot)."""
    counts = (await counts_for(db, [p.id]))[p.id]
    rows, costs = await load_arm_data(db, p.id)
    out = pass_out(p, counts, arm_stats(rows, p.arms or [], costs), with_selection_ids=True)
    if p.status in ("draft",) or not (p.diagnostics or {}).get("gates"):
        out["gates"] = compute_gates([(r[1], r[2]) for r in rows], thresholds(), p.acknowledged_gates)
    return out


def review_out(r: CredibilityReview, label: str | None = None, *, detail: bool = True) -> dict[str, Any]:
    out = {
        "id": str(r.id), "pass_id": str(r.pass_id), "disclosure_report_id": str(r.disclosure_report_id), "company": label,
        "arm_key": r.arm_key, "status": r.status, "credibility_score": r.credibility_score, "model": r.model,
        "input_mode": r.input_mode, "input_budget": r.input_budget, "input_tokens": r.input_tokens, "output_tokens": r.output_tokens, "cost_usd": float(r.cost_usd or 0),
        "attempts": r.attempts, "error": r.error, "run_id": str(r.run_id) if r.run_id else None,
        "completed_at": r.completed_at.isoformat() if r.completed_at else None,
    }
    if detail:
        out.update(
            pushbacks=r.pushbacks or [], story_breaks=r.story_breaks or [], summary=r.summary,
            freeze_id=str(r.freeze_id) if r.freeze_id else None, report_sha256=r.report_sha256, prompt_sha256=r.prompt_sha256,
            claimed_at=r.claimed_at.isoformat() if r.claimed_at else None,
            created_at=r.created_at.isoformat() if r.created_at else None,
        )
    return out


async def list_reviews(
    db: AsyncSession, p: CredibilityPass, *, limit: int = 50, offset: int = 0, arm: str | None = None, status: str | None = None,
    score: int | None = None, min_score: int | None = None, max_score: int | None = None,
) -> dict[str, Any]:
    conds = [CredibilityReview.pass_id == p.id]
    if arm is not None:
        conds.append(CredibilityReview.arm_key == arm)
    if status:
        conds.append(CredibilityReview.status == status)
    if score is not None:
        conds.append(CredibilityReview.credibility_score == score)
    if min_score is not None:
        conds.append(CredibilityReview.credibility_score >= min_score)
    if max_score is not None:
        conds.append(CredibilityReview.credibility_score <= max_score)
    total = (await db.execute(select(func.count()).select_from(CredibilityReview).where(*conds))).scalar_one()
    rows = (await db.execute(
        select(CredibilityReview, BatchTarget)
        .join(DisclosureReport, DisclosureReport.id == CredibilityReview.disclosure_report_id)
        .outerjoin(BatchTarget, BatchTarget.id == DisclosureReport.batch_target_id)
        .where(*conds).order_by(CredibilityReview.created_at, CredibilityReview.id).limit(limit).offset(offset)
    )).all()
    return {"total": total, "limit": limit, "offset": offset,
            "reviews": [review_out(r, (t.label or t.company) if t is not None else None, detail=True) for r, t in rows]}


async def review_detail(db: AsyncSession, r: CredibilityReview) -> dict[str, Any]:
    row = (await db.execute(
        select(BatchTarget).join(DisclosureReport, DisclosureReport.batch_target_id == BatchTarget.id)
        .where(DisclosureReport.id == r.disclosure_report_id)
    )).scalar_one_or_none()
    return review_out(r, (row.label or row.company) if row is not None else None, detail=True)
