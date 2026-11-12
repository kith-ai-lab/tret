"""Batch experiments: a person-started comparison of two or three arms, and a
person-made decision to make the winning arm the harness default.

An arm sets at least one of `model_override`, `objective` and `max_iterations`.
`replicate` runs one batch per arm over the same rows (paired comparison); `split`
runs one batch whose rows alternate between the arms (unpaired comparison). Nothing
here changes a harness by itself: `promote` is called by an admin and records who,
when and what the policy was before (`harness_policy_history`). See docs/batch-runs.md.
"""
from __future__ import annotations

import copy
import hashlib
import json
import random
import statistics
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tret.batches import advice, core, families, scoring
from tret.batches.gate import safe_family_gate
from tret.db.models import Batch, BatchExperiment, Harness, HarnessPolicyHistory, User
from tret.providers.catalog import get_catalog
from tret.router_llm.objectives import OBJECTIVES, objective_of, within_cost_tier
from tret.router_llm.router import _max_cost_tier

ARM_KEYS = ("A", "B", "C")
MODES = ("replicate", "split")
ARM_FIELDS = ("label", "model_override", "objective", "max_iterations")
MIN_SAMPLE = 2
UNPAIRED_NOTE = "different rows"


@dataclass
class ExperimentSpec:
    """What `create_experiment` takes: the experiment's own fields plus the normal
    create body (either profile) every arm batch is a copy of."""

    name: str
    mode: str
    arms: list[dict]
    batch: Any
    sample_n: int | None = None
    extra: dict = field(default_factory=dict)


def _label(arm: dict) -> str:
    parts = []
    if arm.get("model_override"):
        parts.append(arm["model_override"])
    if arm.get("objective"):
        parts.append(f"{arm['objective']} objective")
    if arm.get("max_iterations"):
        parts.append(f"{arm['max_iterations']} iterations")
    return ", ".join(parts)


def clean_arms(raw: Any) -> list[dict]:
    """Validate the arm list: 2-3 arms, each setting at least one known field, all different."""
    if not isinstance(raw, list) or not 2 <= len(raw) <= len(ARM_KEYS):
        raise HTTPException(422, f"An experiment needs 2 or {len(ARM_KEYS)} arms")
    catalog = get_catalog()
    arms: list[dict] = []
    for i, item in enumerate(raw):
        key = ARM_KEYS[i]
        if not isinstance(item, dict):
            raise HTTPException(422, f"Arm {key} must be an object")
        unknown = sorted(set(item) - set(ARM_FIELDS))
        if unknown:
            raise HTTPException(422, f"Arm {key}: unknown field(s) {', '.join(unknown)}")
        model = (item.get("model_override") or "").strip() if isinstance(item.get("model_override"), str) else None
        if item.get("model_override") not in (None, "") and not model:
            raise HTTPException(422, f"Arm {key}: model_override must be a model id")
        objective = item.get("objective") or None
        iterations = item.get("max_iterations")
        if model and catalog.get(model) is None:
            raise HTTPException(422, f"Arm {key}: model '{model}' is not in the catalog")
        if objective is not None and objective not in OBJECTIVES:
            raise HTTPException(422, f"Arm {key}: objective must be one of {', '.join(OBJECTIVES)}")
        if iterations is not None and (
            isinstance(iterations, bool) or not isinstance(iterations, int) or not 1 <= iterations <= 50
        ):
            raise HTTPException(422, f"Arm {key}: max_iterations must be a whole number from 1 to 50")
        if not (model or objective or iterations):
            raise HTTPException(
                422, f"Arm {key} sets nothing: give it a model_override, an objective or max_iterations"
            )
        arm = {
            "key": key,
            "label": (str(item.get("label") or "").strip()[:80]) or "",
            "model_override": model or None,
            "objective": objective,
            "max_iterations": iterations,
        }
        arm["label"] = arm["label"] or _label(arm)
        arms.append(arm)
    seen: dict[tuple, str] = {}
    for arm in arms:
        sig = (arm["model_override"], arm["objective"], arm["max_iterations"])
        if sig in seen:
            raise HTTPException(422, f"Arms {seen[sig]} and {arm['key']} are identical")
        seen[sig] = arm["key"]
    return arms


def _arm_policy_problem(arm: dict, policy: dict | None) -> str | None:
    """Why the arm's model may not run on a harness with this policy, or None. The check
    a run's `_model_override` meets at launch (`allowed` list, `max_cost_tier`)."""
    model = arm.get("model_override")
    if not model:
        return None
    policy = policy or {}
    allowed = policy.get("allowed") or None
    if allowed and model not in allowed:
        return f"model '{model}' is not on the harness's allowed model list"
    tier = _max_cost_tier(policy)
    info = get_catalog().get(model)
    if info is None:
        return f"model '{model}' is not in the catalog"
    if not within_cost_tier(info, tier):
        return f"model '{model}' is above the harness cost ceiling ('{tier}')"
    return None


def _check_arms_against_policy(arms: list[dict], harness: Harness | None) -> None:
    """An arm's model must be one the harness policy lets a run override to, or every
    run of that arm would fail at launch."""
    for arm in arms:
        problem = _arm_policy_problem(arm, harness.model_policy if harness is not None else None)
        if problem:
            raise HTTPException(422, f"Arm {arm['key']}: {problem}")


def _stratum(row: dict, profile: str) -> tuple:
    return (row.get("market"), row.get("category")) if profile == "disclosure" else ()


def sample_rows(rows: list[dict], n: int, profile: str) -> list[dict]:
    """`n` rows chosen evenly across strata (one from each in turn, in register order),
    returned in register order and renumbered from 1."""
    ordered = sorted(rows, key=lambda r: r["position"])
    if n >= len(ordered):
        raise HTTPException(
            422, f"sample_n ({n}) must be smaller than the number of rows ({len(ordered)}); leave it out to use every row"
        )
    strata: dict[tuple, list[dict]] = {}
    for r in ordered:
        strata.setdefault(_stratum(r, profile), []).append(r)
    chosen: list[dict] = []
    depth = 0
    while len(chosen) < n:
        for group in strata.values():
            if depth < len(group) and len(chosen) < n:
                chosen.append(group[depth])
        depth += 1
    chosen.sort(key=lambda r: r["position"])
    return [{**r, "position": i} for i, r in enumerate(chosen, start=1)]


def assign_arms(rows: list[dict], keys: list[str], profile: str) -> list[dict]:
    """Deal the arm keys over the rows in position order, within each stratum in turn
    (disclosure: market and category) and carrying the rotation across strata, so each
    stratum is spread over the arms and the arm counts differ by at most one overall.
    Starts with A; deterministic."""
    if len(rows) < len(keys):
        raise HTTPException(
            422, f"A split experiment needs at least one row per arm: {len(rows)} row(s) for {len(keys)} arms"
        )
    strata: dict[tuple, list[dict]] = {}
    for r in sorted(rows, key=lambda r: r["position"]):
        strata.setdefault(_stratum(r, profile), []).append(r)
    arm_of: dict[int, str] = {}
    offset = 0
    for group in strata.values():
        for r in group:
            arm_of[r["position"]] = keys[offset % len(keys)]
            offset += 1
    return [{**r, "arm": arm_of[r["position"]]} for r in sorted(rows, key=lambda r: r["position"])]


def _family_key(body: Any, profile: str) -> str:
    return scoring.family_key_for(
        profile,
        fiscal_year=getattr(body, "fiscal_year", None),
        harness_id=getattr(body, "harness_id", None),
        template=getattr(body, "template", None) or "",
    )


async def create_experiment(db: AsyncSession, workspace: Any, user: Any, spec: ExperimentSpec) -> BatchExperiment:
    workspace_id = getattr(workspace, "id", workspace)
    user_id = getattr(user, "id", user)
    name = (spec.name or "").strip()
    if not name:
        raise HTTPException(422, "name is required")
    if spec.mode not in MODES:
        raise HTTPException(422, f"mode must be one of {', '.join(MODES)}")
    arms = clean_arms(spec.arms)
    base = spec.batch
    profile = getattr(base, "profile", None) or "disclosure"
    sample_n = spec.sample_n
    if sample_n is not None:
        if spec.mode == "split":
            raise HTTPException(422, "sample_n is only for replicate experiments; a split experiment runs each row once")
        if isinstance(sample_n, bool) or not isinstance(sample_n, int) or sample_n < MIN_SAMPLE:
            raise HTTPException(422, f"sample_n must be a whole number of at least {MIN_SAMPLE}")
    harness = await advice._harness_for(db, workspace_id, profile, getattr(base, "harness_id", None))
    if profile == "prompt" and (harness is None or harness.is_archived):
        raise HTTPException(422, "harness_id: no such harness in this workspace (or it is archived)")
    _check_arms_against_policy(arms, harness)

    experiment_id = uuid.uuid4()
    try:
        if spec.mode == "replicate":
            for arm in arms:
                slug = f"{base.run_slug}-{arm['key'].lower()}"
                if len(slug) > 61:
                    raise HTTPException(422, f"run_slug is too long for an experiment: '{slug}' would exceed 61 characters")
                update = {"name": f"{base.name} · arm {arm['key']}", "run_slug": slug}
                for k in ("model_override", "objective", "max_iterations"):
                    if arm[k] is not None:
                        update[k] = arm[k]
                body = base.model_copy(update=update)
                batch, _ = await core.create_batch(
                    db,
                    workspace_id=workspace_id,
                    user_id=user_id,
                    body=body,
                    profile_slug=profile,
                    settings_extra={"experiment": {"id": str(experiment_id), "arm": arm["key"]}},
                    row_filter=(lambda rows: sample_rows(rows, sample_n, profile)) if sample_n else None,
                    commit=False,
                )
                arm["batch_id"] = str(batch.id)
        else:
            keys = [a["key"] for a in arms]
            batch, _ = await core.create_batch(
                db,
                workspace_id=workspace_id,
                user_id=user_id,
                body=base,
                profile_slug=profile,
                settings_extra={"experiment": {"id": str(experiment_id), "arms": copy.deepcopy(arms)}},
                row_filter=lambda rows: assign_arms(rows, keys, profile),
                commit=False,
            )
            for arm in arms:
                arm["batch_id"] = str(batch.id)
            # the stored arms carry the batch id; the batch's own copy does not need it
        experiment = BatchExperiment(
            id=experiment_id,
            workspace_id=workspace_id,
            name=name,
            mode=spec.mode,
            profile=profile,
            family_key=_family_key(base, profile),
            harness_id=harness.id if harness is not None else None,
            arms=arms,
            sample_n=sample_n,
            created_by=user_id,
        )
        db.add(experiment)
        await db.commit()
    except BaseException:
        await db.rollback()
        raise
    return experiment


async def get_experiment(db: AsyncSession, experiment_id: uuid.UUID, workspace_id: uuid.UUID) -> BatchExperiment:
    e = await db.get(BatchExperiment, experiment_id)
    if e is None or e.workspace_id != workspace_id:
        raise HTTPException(404, "Experiment not found")
    return e


async def _load_batches(db: AsyncSession, ids: list[uuid.UUID]) -> dict[uuid.UUID, Batch]:
    if not ids:
        return {}
    rows = (await db.execute(select(Batch).where(Batch.id.in_(ids)))).scalars().all()
    return {b.id: b for b in rows}


def _arm_batch_map(e: BatchExperiment, loaded: dict[uuid.UUID, Batch]) -> dict[str, Batch | None]:
    out: dict[str, Batch | None] = {}
    for a in e.arms:
        b = loaded.get(uuid.UUID(a["batch_id"])) if a.get("batch_id") else None
        out[a["key"]] = b if b is not None and b.workspace_id == e.workspace_id else None
    return out


async def _arm_batches(db: AsyncSession, e: BatchExperiment) -> dict[str, Batch | None]:
    ids = [uuid.UUID(a["batch_id"]) for a in e.arms if a.get("batch_id")]
    return _arm_batch_map(e, await _load_batches(db, ids))


async def list_experiments(db: AsyncSession, workspace_id: uuid.UUID, limit: int = 100) -> list[dict]:
    rows = (
        await db.execute(
            select(BatchExperiment)
            .where(BatchExperiment.workspace_id == workspace_id)
            .order_by(BatchExperiment.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    loaded = await _load_batches(
        db, [uuid.UUID(a["batch_id"]) for e in rows for a in e.arms if a.get("batch_id")]
    )
    out = []
    for e in rows:
        batches = _arm_batch_map(e, loaded)
        label = next((scoring.family_label(b) for b in batches.values() if b is not None), e.family_key)
        out.append(
            {
                "id": str(e.id),
                "name": e.name,
                "mode": e.mode,
                "family_label": label,
                "arms": [
                    {
                        "key": a["key"],
                        "label": a.get("label"),
                        "batch_id": a.get("batch_id"),
                        "batch_status": batches[a["key"]].status if batches.get(a["key"]) is not None else None,
                    }
                    for a in e.arms
                ],
                "created_at": core._iso(e.created_at),
                "promoted": e.promoted,
            }
        )
    return out


async def start_experiment(db: AsyncSession, experiment: BatchExperiment) -> list[Batch]:
    """`core.start` on every arm batch still in draft; returns the batches started. A
    failure after others started says exactly which arms started and which did not."""
    started: list[Batch] = []
    done_ids: set[str] = set()
    started_arms: list[str] = []
    for arm in experiment.arms:
        bid = arm.get("batch_id")
        if not bid or bid in done_ids:
            continue
        done_ids.add(bid)
        b = await db.get(Batch, uuid.UUID(bid))
        if b is None or b.workspace_id != experiment.workspace_id or b.status != "draft":
            continue
        arms_here = [a["key"] for a in experiment.arms if a.get("batch_id") == bid]
        try:
            await core.start(db, b)
        except HTTPException as exc:
            remaining = [
                a["key"] for a in experiment.arms if a["key"] not in started_arms and a["key"] not in arms_here
            ]
            raise HTTPException(
                exc.status_code,
                f"Arm(s) {', '.join(started_arms) or 'none'} started; arm(s) {', '.join(arms_here)} did not "
                f"({exc.detail}); the rest ({', '.join(remaining) or 'none'}) were not tried",
            ) from exc
        started.append(b)
        started_arms.extend(arms_here)
    if not started:
        raise HTTPException(409, "Every batch of this experiment has already started")
    return started


def _unpaired(qa: list[float], qx: list[float], *, bootstrap: bool = True) -> tuple[float | None, list[float] | None]:
    if not qa or not qx:
        return None, None
    mean = round(statistics.fmean(qx) - statistics.fmean(qa), 2)
    if not bootstrap or len(qa) < 3 or len(qx) < 3:
        return mean, None
    rng = random.Random(0)
    n = scoring.BOOTSTRAP_RESAMPLES
    diffs = []
    for _ in range(n):
        ma = sum(qa[rng.randrange(len(qa))] for _ in qa) / len(qa)
        mx = sum(qx[rng.randrange(len(qx))] for _ in qx) / len(qx)
        diffs.append(mx - ma)
    diffs.sort()
    return mean, [round(diffs[int(0.05 * n)], 2), round(diffs[int(0.95 * n) - 1], 2)]


def _recommend(arm_stats: list[dict], objective: str) -> dict | None:
    pick, reason = advice._recommend_model(
        [{**s, "model": s["key"]} for s in arm_stats], objective
    )
    reason = reason.replace("models", "arms").replace("model", "arm")
    return {"arm": pick["key"], "reason": reason} if pick is not None else {"arm": None, "reason": reason}


MODEL_ARM_NOTE = (
    "This arm's model is not pinned. Model choice stays with the router; this experiment's "
    "evaluated runs are its evidence once the family's spot-check gate is open."
)
MODEL_ONLY_REASON = "A model arm changes no harness setting: its runs are evidence for the router."


def _describe_model(policy: dict) -> str:
    return f"pinned to {policy.get('model')}" if policy.get("mode") == "pinned" else "automatic routing"


def _compact(value: Any) -> str:
    return "unset" if value is None else json.dumps(value, sort_keys=True, separators=(",", ":"))


def _token(*parts: Any) -> str:
    """A short stable hash of the harness's current settings and the plan's new ones: the
    precondition a person's confirmation is tied to."""
    return hashlib.sha256(json.dumps(list(parts), sort_keys=True, default=str).encode()).hexdigest()[:16]


def _diff(cur_policy: dict, new_policy: dict, cur_loop: dict, new_loop: dict) -> list[dict]:
    """Everything that differs between the harness's current settings and the plan's."""
    changes: list[dict] = []
    if _describe_model(cur_policy) != _describe_model(new_policy):
        changes.append({"field": "Model", "from": _describe_model(cur_policy), "to": _describe_model(new_policy)})
    before, after = cur_policy.get("objective") or "balanced", new_policy.get("objective") or "balanced"
    if before != after:
        changes.append({"field": "Objective", "from": before, "to": after})
    if cur_loop.get("max_iterations") != new_loop.get("max_iterations"):
        changes.append(
            {
                "field": "Max iterations",
                "from": str(cur_loop.get("max_iterations") or "harness default"),
                "to": str(new_loop.get("max_iterations") or "harness default"),
            }
        )
    for prefix, cur, new, skip in (
        ("policy", cur_policy, new_policy, {"mode", "model", "objective"}),
        ("loop", cur_loop, new_loop, {"max_iterations"}),
    ):
        for key in sorted((set(cur) | set(new)) - skip):
            if cur.get(key) != new.get(key):
                changes.append({"field": f"{prefix}.{key}", "from": _compact(cur.get(key)), "to": _compact(new.get(key))})
    return changes


def promotion_plan(
    e: BatchExperiment, arm: dict, harness: Harness | None, last_row: Any
) -> dict:
    """What making `arm` the harness default would do, computed from the harness as it is
    now: `{allowed, blocked_reason, changes: [{field, from, to}], notes, new_model_policy,
    new_loop_config}`. `promote` executes exactly this plan.

    BASE: when this experiment's last promotion is still what the harness has, the arm is
    applied on top of the policy from before that promotion (it replaces it); otherwise on
    top of the harness's current policy."""
    if harness is None or harness.is_archived or harness.workspace_id != e.workspace_id:
        return {
            "allowed": False, "blocked_reason": "the experiment's harness no longer exists",
            "changes": [], "notes": [], "new_model_policy": None, "new_loop_config": None, "token": None,
        }
    current_policy = copy.deepcopy(harness.model_policy or {})
    current_loop = copy.deepcopy(harness.loop_config or {})
    base_policy, base_loop = current_policy, current_loop
    notes: list[str] = []
    promoted = e.promoted or {}
    if (
        last_row is not None
        and last_row.new_model_policy == current_policy
        and last_row.new_loop_config == current_loop
    ):
        base_policy = copy.deepcopy(last_row.previous_model_policy)
        base_loop = copy.deepcopy(last_row.previous_loop_config)
        notes.append(f"replaces the earlier promotion of arm {promoted.get('arm')}")
    new_policy, new_loop = copy.deepcopy(base_policy), copy.deepcopy(base_loop)
    # The arm's model is never written to a harness: dynamic routing keeps choosing it.
    if arm.get("model_override"):
        notes.append(MODEL_ARM_NOTE)
    if arm.get("objective"):
        new_policy["objective"] = arm["objective"]
    if arm.get("max_iterations"):
        new_loop["max_iterations"] = arm["max_iterations"]
    changes = _diff(current_policy, new_policy, current_loop, new_loop)
    reason = None
    model_only = bool(arm.get("model_override")) and not arm.get("objective") and not arm.get("max_iterations")
    if model_only:
        reason = MODEL_ONLY_REASON
        changes = []
    elif not changes:
        reason = "the harness already has these settings"
    return {
        "allowed": reason is None,
        "blocked_reason": reason,
        "changes": changes,
        "notes": notes,
        "new_model_policy": new_policy,
        "new_loop_config": new_loop,
        "token": _token(current_policy, current_loop, new_policy, new_loop) if not model_only else None,
    }


async def _last_promotion_row(db: AsyncSession, e: BatchExperiment) -> SimpleNamespace | None:
    """The experiment's last promotion as `new_*` (what that promotion wrote) and `previous_*`
    (the baseline before the experiment's promotions: a chain of replacing promotions is
    followed back to the policy the first of them started from)."""
    hid = (e.promoted or {}).get("history_id")
    if not hid:
        return None
    try:
        last = await db.get(HarnessPolicyHistory, uuid.UUID(str(hid)))
    except ValueError:
        return None
    if (
        last is None
        or last.source != "experiment"
        or last.source_id != e.id
        or last.harness_id != e.harness_id
    ):
        return None
    older = (
        await db.execute(
            select(HarnessPolicyHistory)
            .where(
                HarnessPolicyHistory.harness_id == last.harness_id,
                HarnessPolicyHistory.source == "experiment",
                HarnessPolicyHistory.source_id == e.id,
                HarnessPolicyHistory.changed_at < last.changed_at,
            )
            .order_by(HarnessPolicyHistory.changed_at.desc())
        )
    ).scalars().all()
    previous_policy, previous_loop = last.previous_model_policy, last.previous_loop_config
    for row in older:
        if row.new_model_policy == previous_policy and row.new_loop_config == previous_loop:
            previous_policy, previous_loop = row.previous_model_policy, row.previous_loop_config
        else:
            break
    return SimpleNamespace(
        new_model_policy=last.new_model_policy,
        new_loop_config=last.new_loop_config,
        previous_model_policy=previous_policy,
        previous_loop_config=previous_loop,
    )


def _warnings(e: BatchExperiment, harness: Harness | None, batches: dict[str, Batch | None]) -> list[str]:
    out: list[str] = []
    first = next((b for b in batches.values() if b is not None), None)
    base_model = (first.settings or {}).get("model_override") if first is not None else None
    pinned = (harness.model_policy or {}).get("mode") == "pinned" if harness is not None else False
    for a in e.arms:
        only_objective = a.get("objective") and not a.get("model_override") and not a.get("max_iterations")
        b = batches.get(a["key"])
        fixed = pinned or bool(((b.settings or {}).get("model_override") if b is not None else base_model))
        if only_objective and fixed:
            out.append(
                f"arm {a['key']} changes only the objective; the model is fixed, so only effort differs"
            )
    if e.mode == "replicate" and first is not None and (first.settings or {}).get("auto_write"):
        out.append("each arm writes its own copy of the packages under its own run folder")
    return out


async def experiment_result(db: AsyncSession, e: BatchExperiment) -> dict:
    batches = await _arm_batches(db, e)
    harness = await db.get(Harness, e.harness_id) if e.harness_id else None
    last_row = await _last_promotion_row(db, e)
    arms_out: list[dict] = []
    comparisons: list[dict] = []
    comparable = True
    notes: list[str] = []
    per_arm_data: dict[str, list[scoring.TargetData]] = {}
    datas: dict[str, scoring.BatchData | None] = {}
    split_data = None
    if e.mode == "replicate":
        for k, b in batches.items():
            datas[k] = await scoring.collect(db, b) if b is not None else None
            per_arm_data[k] = datas[k].targets if datas[k] is not None else []
    else:
        first = next((b for b in batches.values() if b is not None), None)
        split_data = await scoring.collect(db, first) if first is not None else None
        for a in e.arms:
            per_arm_data[a["key"]] = [t for t in (split_data.targets if split_data else []) if t.target.arm == a["key"]]
    unsettled = 0
    for a in e.arms:
        ds = per_arm_data.get(a["key"], [])
        done = sum(1 for d in ds if d.target.status in core.TERMINAL_TARGET_STATUSES)
        unsettled += max(0, len(ds) - done) if ds else 1
    bootstrap = unsettled == 0
    evaluating = any(
        d is not None and d.current is not None and d.current.status in ("pending", "running")
        for d in [*datas.values(), split_data]
    )
    for a in e.arms:
        k = a["key"]
        ds = per_arm_data.get(k, [])
        stats = families.group_stats(ds, intervals=bootstrap)
        b = batches.get(k)
        plan = promotion_plan(e, a, harness, last_row)
        arms_out.append(
            {
                "key": k,
                "label": a.get("label"),
                "model_override": a.get("model_override"),
                "objective": a.get("objective"),
                "max_iterations": a.get("max_iterations"),
                "batch_id": a.get("batch_id"),
                "batch_status": b.status if b is not None else None,
                "targets": stats["targets"],
                "settled": sum(1 for d in ds if d.target.status in core.TERMINAL_TARGET_STATUSES),
                **{key: stats[key] for key in (
                    "delivered", "scored", "acceptable", "yield", "quality_mean", "quality_interval90",
                    "mean_residual", "cost_usd", "cost_per_acceptable_usd", "energy_per_acceptable_wh",
                    "mean_iterations", "infra_failures",
                )},
                "promotion": {key: plan[key] for key in ("allowed", "blocked_reason", "changes", "notes", "token")},
                "_current": plan["blocked_reason"] == "the harness already has these settings",
            }
        )
    if e.mode == "replicate":
        base = datas.get("A")
        for a in e.arms[1:]:
            x = datas.get(a["key"])
            if base is None or x is None:
                comparable = False
                comparisons.append({"arm": a["key"], "vs": "A", "paired": True, "n": 0, "n_vs": 0, "wins": None, "losses": None,
                                    "ties": None, "mean_delta": None, "interval90": None, "note": "A batch of this experiment no longer exists."})
                continue
            s = scoring.paired_summary(base, x, bootstrap=bootstrap)
            comparable = comparable and s["quality_comparable"]
            if s["quality_note"]:
                notes.append(s["quality_note"])
            comparisons.append(
                {"arm": a["key"], "vs": "A", "paired": True, "n": s["n"], "n_vs": s["n"], "wins": s["wins_b"],
                 "losses": s["wins_a"], "ties": s["ties"], "mean_delta": s["mean_quality_delta"],
                 "interval90": s["interval90"], "note": s["quality_note"]}
            )
    else:
        if split_data is None or split_data.current is None or split_data.current.status in scoring.DEAD_PASS_STATUSES:
            comparable = False
            notes.append("The batch has no evaluation yet.")
        qa = [d.quality for d in per_arm_data.get("A", []) if d.quality is not None]
        for a in e.arms[1:]:
            qx = [d.quality for d in per_arm_data.get(a["key"], []) if d.quality is not None]
            mean, interval = _unpaired(qa, qx, bootstrap=bootstrap) if comparable else (None, None)
            comparisons.append(
                {"arm": a["key"], "vs": "A", "paired": False, "n": len(qx), "n_vs": len(qa), "wins": None, "losses": None,
                 "ties": None, "mean_delta": mean, "interval90": interval, "note": UNPAIRED_NOTE}
            )
    note = " ".join(dict.fromkeys(notes)) or None
    objective = objective_of(harness.model_policy if harness is not None else None)
    if unsettled:
        recommendation = {"arm": None, "reason": "Some arms still have rows that have not settled."}
    elif not comparable:
        recommendation = {"arm": None, "reason": note or "The arms are not scored comparably, so there is nothing to recommend."}
    else:
        recommendation = _recommend(arms_out, objective)
    # Arms whose plan changes nothing can share a plan (an arm's model is dropped): prefer the
    # one this experiment actually promoted.
    current_keys = [a["key"] for a in arms_out if a["_current"]]
    promoted_arm = (e.promoted or {}).get("arm")
    current_arm = promoted_arm if promoted_arm in current_keys else next(iter(current_keys), None)
    try:
        router_gate = (
            await safe_family_gate(db, f"{e.family_key}@{e.workspace_id}") if e.family_key else None
        )
    except Exception:  # noqa: BLE001 - a display extra, never worth failing the result
        router_gate = None
    for a in arms_out:
        del a["_current"]
    result_batches = [b for b in batches.values() if b is not None]
    return {
        "experiment": {
            "id": str(e.id),
            "name": e.name,
            "mode": e.mode,
            "profile": e.profile,
            "family_key": e.family_key,
            "family_label": scoring.family_label(result_batches[0]) if result_batches else e.family_key,
            "harness_id": str(e.harness_id) if e.harness_id else None,
            "harness_name": harness.name if harness is not None else None,
            "sample_n": e.sample_n,
            "created_at": core._iso(e.created_at),
            "promoted": e.promoted,
            "current_arm": current_arm,
            "evaluating": evaluating,
            "router_gate": router_gate,
            "warnings": _warnings(e, harness, batches),
        },
        "arms": arms_out,
        "comparisons": comparisons,
        "recommendation": recommendation,
        "quality_comparable": comparable,
        "quality_note": note,
    }


async def promote(
    db: AsyncSession, e: BatchExperiment, arm_key: str, user: User, token: str | None = None
) -> dict:
    """Make an arm the harness default (an admin action; the caller checks the role) by
    executing `promotion_plan`. Returns `{harness_id, model_policy, loop_config, history,
    experiment}`; 409 with the plan's reason when it is not allowed."""
    from tret.api._policy import validate_policy
    from tret.services import harness_policy

    arm = next((a for a in e.arms if a["key"] == arm_key), None)
    if arm is None:
        raise HTTPException(422, f"This experiment has no arm '{arm_key}'")
    harness = None
    if e.harness_id:
        # Row lock: two concurrent promotions serialise on the harness.
        harness = (
            await db.execute(select(Harness).where(Harness.id == e.harness_id).with_for_update())
        ).scalar_one_or_none()
        if harness is not None:
            await db.refresh(harness)
    await db.refresh(e)
    plan = promotion_plan(e, arm, harness, await _last_promotion_row(db, e))
    if plan["blocked_reason"] == MODEL_ONLY_REASON:
        raise HTTPException(409, f"Arm {arm_key} cannot be made the default: {plan['blocked_reason']}")
    if token is not None and token != plan["token"]:
        raise HTTPException(
            409,
            "The harness or the experiment changed since this was shown; reload and review the change again.",
        )
    if not plan["allowed"]:
        raise HTTPException(409, f"Arm {arm_key} cannot be made the default: {plan['blocked_reason']}")
    previous_policy = copy.deepcopy(harness.model_policy or {})
    previous_loop = copy.deepcopy(harness.loop_config or {})
    policy, loop = plan["new_model_policy"], plan["new_loop_config"]
    validate_policy(policy)
    harness.model_policy = policy
    harness.loop_config = loop
    row = await harness_policy.record_policy_change(
        db,
        harness,
        previous_model_policy=previous_policy,
        previous_loop_config=previous_loop,
        user_id=user.id,
        source="experiment",
        source_id=e.id,
        note=f"Experiment '{e.name}', arm {arm_key} ({arm.get('label')})",
    )
    e.promoted = {
        "arm": arm_key,
        "by": str(user.id),
        "by_name": user.display_name,
        "at": core._iso(core._now()),
        "history_id": str(row.id),
    }
    await db.commit()
    return {
        "harness_id": str(harness.id),
        "model_policy": harness.model_policy,
        "loop_config": harness.loop_config,
        "history": {
            "id": str(row.id),
            "source": "experiment",
            "source_id": str(e.id),
            "previous_model_policy": previous_policy,
            "previous_loop_config": previous_loop,
            "new_model_policy": policy,
            "new_loop_config": loop,
        },
        "experiment": await experiment_result(db, e),
    }
