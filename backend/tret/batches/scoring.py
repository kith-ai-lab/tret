"""Batch scorecards, comparison and relative scores (no model calls).

E0 of the batch evaluator: everything here is arithmetic over rows the batch
already has (targets, their runs, their review findings) plus, when an
evaluation pass exists (`tret/batches/evaluation.py`), its scores.

- `family_key` / `row_key`: what makes two batches comparable and two targets
  "the same row". Computed, never stored on the batch.
- `target_class`: success / gap / rejected / failed / infra / timeout /
  unsettled. Infrastructure failures and timeouts count against reliability,
  never against the model.
- `batch_scorecard`: yield, cost and energy per acceptable result, wall time,
  reliability, model mix and robust-z outliers (`GET /batches/{id}/scorecard`).
- `compare_batches`: two scorecards plus a row-by-row diff
  (`GET /batches/compare`).
- `delivered_elsewhere`: for a disclosure batch's rows, the acceptable deliveries
  of the same company in other batches of the family (the cross-check that tests a
  gap's reason; the judge decides, nothing is overridden in code).
- `recompute_relative`: rank, percentile and expected-vs-actual (residual) for
  the scored rows of a pass.
"""
from __future__ import annotations

import hashlib
import random
import re
import statistics
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tret.batches import core
from tret.batches.profiles import profile_for
from tret.batches.rubrics import DEFAULT_ACCEPTABLE_QUALITY
from tret.db.models import Batch, BatchEvaluation, BatchTarget, Finding, Harness, Run, TargetEvaluation

CLASSES = ("success", "gap", "rejected", "failed", "infra", "timeout", "unsettled")
UNSETTLED_STATUSES = frozenset({"pending", "held", "running", "review", "packaging"})
EXCLUDED_FROM_YIELD = ("infra", "timeout", "unsettled")
OUTLIER_Z = 3.5
OUTLIER_MIN_N = 5
OUTLIER_LIMIT = 20
OUTLIER_METRICS = ("cost_usd", "iteration_ratio", "retained_ratio", "retained_pages")
TIE_POINTS = 5.0
# Flags that say a delivery is not the document asked for: a row carrying one is not
# evidence that the disclosure could be had.
DISQUALIFYING_FLAGS = frozenset(
    {
        "wrong_year",
        "wrong_entity",
        "wrong_language",
        "summary_not_report",
        "missed_primary",
        "unlogged_substitution",
    }
)
CROSS_CHECK_MAX = 3
BOOTSTRAP_RESAMPLES = 2000
# Statuses of an evaluation pass that do not count as "the current pass".
DEAD_PASS_STATUSES = ("cancelled", "failed")

_DEDUPE_SUFFIX_RE = re.compile(r"\s#\d+$")


# ══════════════════════════════════════════════════════════════════════════
# keys
# ══════════════════════════════════════════════════════════════════════════
def family_key(batch: Batch) -> str:
    """What makes two batches comparable: `disclosure:fy<year>` for the disclosure
    profile, `prompt:<harness id>:<sha256(template)[:12]>` for a prompt batch;
    `<profile>:<run_slug>` for a profile with no family notion (never comparable
    with another batch)."""
    settings = batch.settings or {}
    profile = batch.profile or "disclosure"
    if profile == "disclosure":
        return f"disclosure:fy{settings.get('fiscal_year')}"
    if profile == "prompt":
        template = str(settings.get("template") or "")
        digest = hashlib.sha256(template.encode("utf-8")).hexdigest()[:12]
        return f"prompt:{settings.get('harness_id') or batch.harness_id}:{digest}"
    return f"{profile}:{batch.run_slug}"


def family_label(batch: Batch) -> str:
    settings = batch.settings or {}
    profile = batch.profile or "disclosure"
    if profile == "disclosure":
        return f"Disclosure FY{settings.get('fiscal_year')}"
    if profile == "prompt":
        template = " ".join(str(settings.get("template") or "").split())[:40]
        return f"Prompt · {settings.get('harness_name') or 'harness'} · {template}"
    return f"{profile} · {batch.run_slug}"


def row_key(batch: Batch, target: BatchTarget) -> str:
    """The same company (or the same input row) across batches of one family.
    Disclosure: the target's slug. Prompt: the slug when the batch has a
    `slug_column`, else the label as lowercase ascii letters, digits and hyphens
    with a trailing ` #N` dedupe suffix removed."""
    if (batch.profile or "disclosure") != "prompt" or (batch.settings or {}).get("slug_column"):
        return target.slug
    from tret.batches.profiles.prompt import slugify

    label = _DEDUPE_SUFFIX_RE.sub("", target.label or "")
    return slugify(label) or target.slug


# ══════════════════════════════════════════════════════════════════════════
# classes and per-target metrics
# ══════════════════════════════════════════════════════════════════════════
def target_class(t: BatchTarget) -> str:
    """success | gap | rejected | failed | infra | timeout | unsettled."""
    status = t.status
    if status in UNSETTLED_STATUSES:
        return "unsettled"
    if status in ("failed", "gap", "write_failed"):
        summary = (t.outcome or {}).get("summary") if isinstance(t.outcome, dict) else None
        if core.is_infra_error(t.error) or core.is_infra_error(summary if isinstance(summary, str) else None):
            return "infra"
        error = t.error or ""
        if error == "timeout" or error.startswith("timeout"):
            return "timeout"
    if status in core.DONE_TARGET_STATUSES:
        return "success"
    if status == "gap":
        return "gap"
    if status == "rejected":
        return "rejected"
    return "failed"


def _num(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def target_energy_wh(t: BatchTarget, run: Run | None) -> float | None:
    """The current run's energy plus every archived attempt's that recorded one;
    None when no part of it is known (never 0 for "unknown")."""
    parts = [_num(run.energy_wh)] if run is not None else []
    parts += [_num(h.get("energy_wh")) for h in (t.history or []) if isinstance(h, dict)]
    known = [p for p in parts if p is not None]
    return round(sum(known), 6) if known else None


def _disclosure_pages(findings: list[Finding]) -> tuple[int | None, int | None]:
    """(retained pages, source pages) over the target's packaged, approved maps;
    a source document shared by two maps is counted once."""
    from tret.batches.profiles.disclosure import _package_of, _relevant_maps

    retained = 0
    sources: dict[str, int] = {}
    any_packaged = False
    for f in _relevant_maps(findings):
        package = _package_of(f)
        if f.status != "approved" or not package or package.get("status") != "packaged":
            continue
        any_packaged = True
        retained += int(package.get("retained_page_count") or 0)
        source = (f.payload or {}).get("source") or {}
        key = str(source.get("document_id") or f.id)
        sources[key] = max(sources.get(key, 0), int(source.get("page_count") or 0))
    if not any_packaged:
        return None, None
    return retained, sum(sources.values()) or None


def _seconds(a: Any, b: Any) -> float | None:
    a, b = core._aware(a), core._aware(b)
    if a is None or b is None:
        return None
    return round((b - a).total_seconds(), 3)


def target_metrics(
    batch: Batch,
    target: BatchTarget,
    run: Run | None,
    findings: list[Finding],
    *,
    max_iterations: int | None = None,
) -> dict:
    """The numbers the scorecard, outliers and comparison read for one target."""
    settings = batch.settings or {}
    cap = settings.get("max_iterations") or max_iterations
    retained = source = None
    if (batch.profile or "disclosure") == "disclosure":
        retained, source = _disclosure_pages(findings)
    decided = [f for f in findings if f.status in ("approved", "rejected")]
    return {
        "class": target_class(target),
        "cost_usd": round(core.target_cost(target, run), 6),
        "energy_wh": target_energy_wh(target, run),
        "iterations": run.iterations if run is not None else None,
        "max_iterations": int(cap) if cap else None,
        "duration_seconds": _seconds(target.started_at, target.finished_at),
        "attempts": target.attempts or 0,
        "model_used": run.model_used if run is not None else None,
        "retained_pages": retained,
        "source_pages": source,
        "decided_reviews": len(decided),
        "rejections": sum(1 for f in decided if f.status == "rejected"),
    }


# ══════════════════════════════════════════════════════════════════════════
# data gathering
# ══════════════════════════════════════════════════════════════════════════
async def current_pass(db: AsyncSession, collection_id: uuid.UUID) -> BatchEvaluation | None:
    """The batch's current evaluation pass: its latest one that was not cancelled
    or failed, else its latest of any status."""
    passes = list(
        (
            await db.execute(
                select(BatchEvaluation)
                .where(BatchEvaluation.collection_id == collection_id)
                .order_by(BatchEvaluation.created_at.desc())
                .execution_options(populate_existing=True)
            )
        ).scalars()
    )
    for p in passes:
        if p.status not in DEAD_PASS_STATUSES:
            return p
    return passes[0] if passes else None


async def pass_rows(db: AsyncSession, evaluation_id: uuid.UUID) -> list[TargetEvaluation]:
    return list(
        (
            await db.execute(
                select(TargetEvaluation)
                .where(TargetEvaluation.evaluation_id == evaluation_id)
                .execution_options(populate_existing=True)
            )
        ).scalars()
    )


@dataclass
class TargetData:
    target: BatchTarget
    run: Run | None
    findings: list[Finding]
    metrics: dict
    row: str
    evaluation: TargetEvaluation | None = None
    quality: float | None = None  # a fresh scored evaluation's quality
    acceptable: bool = False

    @property
    def cls(self) -> str:
        return self.metrics["class"]


@dataclass
class BatchData:
    batch: Batch
    targets: list[TargetData]
    current: BatchEvaluation | None
    rows: list[TargetEvaluation] = field(default_factory=list)


def is_stale(row: TargetEvaluation, target: BatchTarget) -> bool:
    return (row.attempt or 0) != (target.attempts or 0)


async def collect(db: AsyncSession, batch: Batch) -> BatchData:
    """Everything the scorecard and the comparison read, loaded once."""
    profile = profile_for(batch)
    targets = await core.load_targets(db, [batch.id])
    runs = await core.load_runs(db, targets)
    findings = await core.load_review_findings(db, profile, targets, batch)
    harness_cap = None
    if batch.harness_id is not None:
        harness = await db.get(Harness, batch.harness_id)
        harness_cap = ((harness.loop_config if harness else None) or {}).get("max_iterations")
    current = await current_pass(db, batch.id)
    rows = await pass_rows(db, current.id) if current is not None else []
    by_target = {r.target_id: r for r in rows}
    threshold = float((current.rubric or {}).get("acceptable_quality", DEFAULT_ACCEPTABLE_QUALITY)) if current else None
    out: list[TargetData] = []
    for t in targets:
        run = runs.get(t.run_id) if t.run_id else None
        fs = findings.get(t.run_id, []) if t.run_id else []
        data = TargetData(
            target=t,
            run=run,
            findings=fs,
            metrics=target_metrics(batch, t, run, fs, max_iterations=harness_cap),
            row=row_key(batch, t),
        )
        ev = by_target.get(t.id)
        data.evaluation = ev
        if ev is not None and ev.status == "scored" and ev.quality is not None and not is_stale(ev, t):
            data.quality = float(ev.quality)
        if data.quality is not None and threshold is not None:
            data.acceptable = data.quality >= threshold
        else:
            data.acceptable = data.cls == "success"
        out.append(data)
    return BatchData(batch=batch, targets=out, current=current, rows=rows)


# ══════════════════════════════════════════════════════════════════════════
# outliers
# ══════════════════════════════════════════════════════════════════════════
def _metric_value(d: TargetData, metric: str) -> float | None:
    m = d.metrics
    if metric == "iteration_ratio":
        if m["iterations"] is None or not m["max_iterations"]:
            return None
        return m["iterations"] / m["max_iterations"]
    if metric == "retained_ratio":
        if m["retained_pages"] is None or not m["source_pages"]:
            return None
        return m["retained_pages"] / m["source_pages"]
    value = m.get(metric)
    return None if value is None else float(value)


def outliers(data: BatchData) -> list[dict]:
    """Robust-z outliers (0.6745 x (x - median) / MAD) over the batch's settled,
    non-infrastructure targets: |z| >= 3.5, n >= 5 and MAD > 0 per metric."""
    pool = [d for d in data.targets if d.cls not in EXCLUDED_FROM_YIELD]
    found: list[dict] = []
    for metric in OUTLIER_METRICS:
        pairs = [(d, v) for d in pool if (v := _metric_value(d, metric)) is not None]
        if len(pairs) < OUTLIER_MIN_N:
            continue
        values = [v for _, v in pairs]
        median = statistics.median(values)
        mad = statistics.median([abs(v - median) for v in values])
        if mad <= 0:
            continue
        for d, v in pairs:
            z = 0.6745 * (v - median) / mad
            if abs(z) >= OUTLIER_Z:
                found.append(
                    {
                        "target_id": str(d.target.id),
                        "label": d.target.label,
                        "metric": metric,
                        "value": round(v, 6),
                        "median": round(median, 6),
                        "z": round(z, 2),
                    }
                )
    found.sort(key=lambda o: abs(o["z"]), reverse=True)
    return found[:OUTLIER_LIMIT]


# ══════════════════════════════════════════════════════════════════════════
# scorecard
# ══════════════════════════════════════════════════════════════════════════
def _ratio(num: float, den: float) -> float | None:
    return round(num / den, 4) if den > 0 else None


def _sum_known(values: list[float | None]) -> float | None:
    known = [v for v in values if v is not None]
    return round(sum(known), 6) if known else None


def quality_summary(data: BatchData) -> dict | None:
    scored = [d for d in data.targets if d.quality is not None]
    if not scored:
        return None
    qualities = [d.quality for d in scored]
    residuals = [
        float(d.evaluation.relative["residual"])
        for d in scored
        if d.evaluation is not None
        and isinstance(d.evaluation.relative, dict)
        and d.evaluation.relative.get("residual") is not None
    ]
    flags: dict[str, int] = {}
    for d in scored:
        for flag in d.evaluation.flags or []:
            flags[flag] = flags.get(flag, 0) + 1
    return {
        "mean": round(statistics.fmean(qualities), 1),
        "median": round(statistics.median(qualities), 1),
        "scored": len(scored),
        "mean_residual": round(statistics.fmean(residuals), 1) if residuals else None,
        "flags": dict(sorted(flags.items())),
    }


def gap_checked_ids(row: TargetEvaluation | None) -> set[str]:
    """The target ids a gap row's last judging was shown as delivered elsewhere."""
    rel = row.relative if row is not None and isinstance(row.relative, dict) else {}
    check = rel.get("gap_check")
    ids = check.get("checked_against") if isinstance(check, dict) else None
    return {str(i) for i in ids} if isinstance(ids, list) else set()


def gap_rechecked(row: TargetEvaluation | None, evidence: list[dict]) -> bool:
    """True when `evidence` is not empty and the row was judged with exactly that
    evidence in view (the keep rule of a pass uses the same equality)."""
    return bool(evidence) and {e["target_id"] for e in evidence} == gap_checked_ids(row)


async def scorecard_from(db: AsyncSession, data: BatchData) -> dict:
    from tret.batches import evaluation as evaluation_service

    batch = data.batch
    classes = {c: 0 for c in CLASSES}
    for d in data.targets:
        classes[d.cls] += 1
    total = len(data.targets)
    denominator = total - sum(classes[c] for c in EXCLUDED_FROM_YIELD)
    acceptable = sum(1 for d in data.targets if d.acceptable and d.cls not in EXCLUDED_FROM_YIELD)
    cost = round(sum(d.metrics["cost_usd"] for d in data.targets), 6)
    energy = _sum_known([d.metrics["energy_wh"] for d in data.targets])
    finished = [core._aware(d.target.finished_at) for d in data.targets if d.target.finished_at]
    started = core._aware(batch.started_at)
    wall = None
    if started is not None and finished:
        wall = round(max(0.0, (max(finished) - started).total_seconds()), 1)
    models: dict[str, int] = {}
    for d in data.targets:
        if d.metrics["model_used"]:
            models[d.metrics["model_used"]] = models.get(d.metrics["model_used"], 0) + 1
    quality = quality_summary(data)
    if quality is not None and (batch.profile or "disclosure") == "disclosure":
        gaps = [d for d in data.targets if d.cls == "gap"]
        elsewhere = await delivered_elsewhere(db, batch, {d.row for d in gaps}, with_sources=False)
        disputed = [d for d in gaps if elsewhere.get(d.row)]
        quality["gaps_disputed"] = len(disputed)
        quality["gaps_rechecked"] = sum(
            1 for d in disputed if gap_rechecked(d.evaluation, elsewhere[d.row])
        )
    return {
        "batch_id": str(batch.id),
        "name": batch.name,
        "profile": batch.profile or "disclosure",
        "status": batch.status,
        "family_key": family_key(batch),
        "family_label": family_label(batch),
        "targets": total,
        "classes": classes,
        "acceptable": acceptable,
        "yield": _ratio(acceptable, denominator),
        "written_share": _ratio(classes["success"], denominator),
        "cost_usd": cost,
        "cost_per_acceptable_usd": round(cost / acceptable, 6) if acceptable else None,
        "energy_wh": energy,
        "energy_per_acceptable_wh": round(energy / acceptable, 4) if acceptable and energy is not None else None,
        "wall_seconds": wall,
        "reliability": {
            "infra_failures": classes["infra"],
            "timeouts": classes["timeout"],
            "retries": sum(max(0, d.metrics["attempts"] - 1) for d in data.targets),
        },
        "human": {
            "decided_reviews": sum(d.metrics["decided_reviews"] for d in data.targets),
            "rejections": sum(d.metrics["rejections"] for d in data.targets),
        },
        "models": [
            {"model": m, "targets": n} for m, n in sorted(models.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
        "outliers": outliers(data),
        "quality": quality,
        "evaluation": await evaluation_service.pass_summary(db, data.current, data.rows) if data.current else None,
    }


async def batch_scorecard(db: AsyncSession, batch: Batch) -> dict:
    return await scorecard_from(db, await collect(db, batch))


# ══════════════════════════════════════════════════════════════════════════
# comparison
# ══════════════════════════════════════════════════════════════════════════
def _pick(candidates: list[TargetData]) -> TargetData:
    """One target per row key: the higher quality, else the success-class one,
    else the lowest position."""
    return min(
        candidates,
        key=lambda d: (
            d.quality is None,
            -(d.quality or 0.0),
            d.cls != "success",
            d.target.position,
        ),
    )


def _side(d: TargetData) -> dict:
    m = d.metrics
    flags = list(d.evaluation.flags or []) if d.quality is not None and d.evaluation is not None else []
    return {
        "target_id": str(d.target.id),
        "status": d.target.status,
        "class": d.cls,
        "quality": d.quality,
        "flags": flags,
        "cost_usd": m["cost_usd"],
        "energy_wh": m["energy_wh"],
        "iterations": m["iterations"],
        "model_used": m["model_used"],
        "retained_pages": m["retained_pages"],
        "source_pages": m["source_pages"],
    }


def _qualifying_delivery(d: TargetData) -> bool:
    """A picked target that delivered an acceptable, scored, undisqualified result."""
    if d.cls != "success" or d.quality is None or not d.acceptable or d.evaluation is None:
        return False
    return DISQUALIFYING_FLAGS.isdisjoint(d.evaluation.flags or [])


def _delta(a: float | None, b: float | None, places: int = 6) -> float | None:
    if a is None or b is None:
        return None
    return round(b - a, places)


def bootstrap_interval(deltas: list[float]) -> list[float] | None:
    """90% percentile interval of the mean of paired deltas: 2000 resamples with
    a fixed seed, so the same data always gives the same interval. None below
    three pairs."""
    n = len(deltas)
    if n < 3:
        return None
    rng = random.Random(0)
    means = sorted(
        sum(deltas[rng.randrange(n)] for _ in range(n)) / n for _ in range(BOOTSTRAP_RESAMPLES)
    )
    lo = means[int(0.05 * BOOTSTRAP_RESAMPLES)]
    hi = means[int(0.95 * BOOTSTRAP_RESAMPLES) - 1]
    return [round(lo, 2), round(hi, 2)]


def _quality_note(a: BatchData, b: BatchData) -> str | None:
    if family_key(a.batch) != family_key(b.batch):
        return "The batches are not the same family (different task or fiscal year)."
    if a.current is None and b.current is None:
        return "Neither batch has an evaluation."
    if a.current is None:
        return "Batch A has no evaluation."
    if b.current is None:
        return "Batch B has no evaluation."
    if (a.current.rubric_key, a.current.rubric_version) != (b.current.rubric_key, b.current.rubric_version):
        return "The batches were scored against different rubrics."
    if a.current.judge_model != b.current.judge_model:
        return "The batches were scored by different judge models."
    return None


async def compare_batches(db: AsyncSession, a: Batch, b: Batch) -> dict:
    data_a, data_b = await collect(db, a), await collect(db, b)
    card_a, card_b = await scorecard_from(db, data_a), await scorecard_from(db, data_b)
    note = _quality_note(data_a, data_b)
    comparable = note is None
    picked_a: dict[str, TargetData] = {}
    picked_b: dict[str, TargetData] = {}
    for data, picked in ((data_a, picked_a), (data_b, picked_b)):
        groups: dict[str, list[TargetData]] = {}
        for d in data.targets:
            groups.setdefault(d.row, []).append(d)
        for key, members in groups.items():
            picked[key] = _pick(members)
    shared = [k for k in picked_a if k in picked_b]
    shared.sort(key=lambda k: picked_a[k].target.position)
    rows = []
    for key in shared:
        da, db_ = picked_a[key], picked_b[key]
        sa, sb = _side(da), _side(db_)
        disputed = None
        if card_a["family_key"] != card_b["family_key"]:
            pass
        elif da.cls == "gap" and _qualifying_delivery(db_):
            disputed = "a"
        elif db_.cls == "gap" and _qualifying_delivery(da):
            disputed = "b"
        rows.append(
            {
                "row_key": key,
                "label": da.target.label,
                "a": sa,
                "b": sb,
                "both_delivered": sa["class"] == sb["class"] == "success"
                and sa["quality"] is not None
                and sb["quality"] is not None,
                "disputed_gap": disputed,
                "delta": {
                    "quality": _delta(sa["quality"], sb["quality"], 2),
                    "cost_usd": _delta(sa["cost_usd"], sb["cost_usd"]),
                    "energy_wh": _delta(sa["energy_wh"], sb["energy_wh"]),
                    "retained_pages": _delta(sa["retained_pages"], sb["retained_pages"], 0),
                },
            }
        )
    deltas = [r["delta"]["quality"] for r in rows if r["delta"]["quality"] is not None]
    wins_a = wins_b = ties = None
    mean_delta = interval = None
    if comparable:
        wins_a = sum(1 for d in deltas if d <= -TIE_POINTS)
        wins_b = sum(1 for d in deltas if d >= TIE_POINTS)
        ties = len(deltas) - wins_a - wins_b
        mean_delta = round(statistics.fmean(deltas), 2) if deltas else None
        interval = bootstrap_interval(deltas)
        rows.sort(key=lambda r: abs(r["delta"]["quality"]) if r["delta"]["quality"] is not None else -1, reverse=True)
    both = [r for r in rows if r["both_delivered"]]
    delivered: dict[str, Any] = {
        "n": len(both),
        "wins_a": None,
        "wins_b": None,
        "ties": None,
        "mean_quality_a": None,
        "mean_quality_b": None,
        "mean_quality_delta": None,
        "interval90": None,
    }
    if comparable:
        d_deltas = [r["delta"]["quality"] for r in both]
        delivered["wins_a"] = sum(1 for d in d_deltas if d <= -TIE_POINTS)
        delivered["wins_b"] = sum(1 for d in d_deltas if d >= TIE_POINTS)
        delivered["ties"] = len(d_deltas) - delivered["wins_a"] - delivered["wins_b"]
        if both:
            delivered["mean_quality_a"] = round(statistics.fmean([r["a"]["quality"] for r in both]), 1)
            delivered["mean_quality_b"] = round(statistics.fmean([r["b"]["quality"] for r in both]), 1)
            delivered["mean_quality_delta"] = round(statistics.fmean(d_deltas), 2)
            delivered["interval90"] = bootstrap_interval(d_deltas)
    return {
        "a": card_a,
        "b": card_b,
        "same_family": card_a["family_key"] == card_b["family_key"],
        "quality_comparable": comparable,
        "quality_note": note,
        "shared": len(shared),
        "only_a": len(picked_a) - len(shared),
        "only_b": len(picked_b) - len(shared),
        "rows": rows,
        "summary": {
            "wins_a": wins_a,
            "wins_b": wins_b,
            "ties": ties,
            "mean_quality_delta": mean_delta,
            "interval90": interval,
            "delivered": delivered,
            "disputed_gaps_a": sum(1 for r in rows if r["disputed_gap"] == "a"),
            "disputed_gaps_b": sum(1 for r in rows if r["disputed_gap"] == "b"),
            "cost_a": round(sum(r["a"]["cost_usd"] for r in rows), 6),
            "cost_b": round(sum(r["b"]["cost_usd"] for r in rows), 6),
            "energy_a": _sum_known([r["a"]["energy_wh"] for r in rows]),
            "energy_b": _sum_known([r["b"]["energy_wh"] for r in rows]),
            "acceptable_a": sum(1 for k in shared if picked_a[k].acceptable and picked_a[k].cls not in EXCLUDED_FROM_YIELD),
            "acceptable_b": sum(1 for k in shared if picked_b[k].acceptable and picked_b[k].cls not in EXCLUDED_FROM_YIELD),
        },
    }


# ══════════════════════════════════════════════════════════════════════════
# relative scores
# ══════════════════════════════════════════════════════════════════════════
async def _history(db: AsyncSession, ev: BatchEvaluation, keys: set[str]) -> dict[str, list[float]]:
    """Scored qualities by row key from OTHER batches' current passes in this
    workspace, for the same rubric key and family."""
    if not keys:
        return {}
    candidates = list(
        (
            await db.execute(
                select(TargetEvaluation).where(
                    TargetEvaluation.workspace_id == ev.workspace_id,
                    TargetEvaluation.rubric_key == ev.rubric_key,
                    TargetEvaluation.family_key == ev.family_key,
                    TargetEvaluation.status == "scored",
                    TargetEvaluation.collection_id != ev.collection_id,
                    TargetEvaluation.row_key.in_(sorted(keys)),
                )
            )
        ).scalars()
    )
    if not candidates:
        return {}
    current_by_batch: dict[uuid.UUID, uuid.UUID | None] = {}
    for cid in {r.collection_id for r in candidates}:
        current = await current_pass(db, cid)
        current_by_batch[cid] = current.id if current is not None else None
    out: dict[str, list[float]] = {}
    for r in candidates:
        if current_by_batch.get(r.collection_id) == r.evaluation_id and r.quality is not None:
            out.setdefault(r.row_key, []).append(float(r.quality))
    return out


async def delivered_elsewhere(
    db: AsyncSession, batch: Batch, row_keys: set[str], *, with_sources: bool = True
) -> dict[str, list[dict]]:
    """Acceptable deliveries of the same companies in OTHER batches of this batch's
    family (same workspace), by row key: scored rows of the other batch's current
    pass whose target succeeded, is not stale, reached that pass's
    `acceptable_quality` and carries no disqualifying flag. At most three per row key,
    best quality first; each names its batch, target, quality, finish date, the run's
    outcome status and reporting period and up to three source URLs of its packaged page maps. Never
    carries a model name. Empty for a batch that is not a disclosure batch.

    `with_sources=False` skips loading the page maps (`source_urls` is then empty):
    the batch detail, scorecard and keep rule need only which targets qualify, and
    the batch page polls them; only the judge's evidence pack shows the URLs."""
    if (batch.profile or "disclosure") != "disclosure" or not row_keys:
        return {}
    candidates = list(
        (
            await db.execute(
                select(TargetEvaluation).where(
                    TargetEvaluation.workspace_id == batch.workspace_id,
                    TargetEvaluation.family_key == family_key(batch),
                    TargetEvaluation.status == "scored",
                    TargetEvaluation.collection_id != batch.id,
                    TargetEvaluation.row_key.in_(sorted(row_keys)),
                )
            )
        ).scalars()
    )
    if not candidates:
        return {}
    cids = sorted({r.collection_id for r in candidates}, key=str)
    # One query for every pass of the candidate batches; the current one is picked
    # here by `current_pass`'s rule (latest not cancelled or failed, else latest).
    passes = list(
        (
            await db.execute(
                select(BatchEvaluation)
                .where(BatchEvaluation.collection_id.in_(cids))
                .order_by(BatchEvaluation.created_at.desc())
                .execution_options(populate_existing=True)
            )
        ).scalars()
    )
    current: dict[uuid.UUID, BatchEvaluation] = {}
    for p in passes:
        if p.status not in DEAD_PASS_STATUSES:
            current.setdefault(p.collection_id, p)
    for p in passes:
        current.setdefault(p.collection_id, p)
    candidates = [
        r
        for r in candidates
        if r.collection_id in current
        and current[r.collection_id].id == r.evaluation_id
        and r.quality is not None
        and DISQUALIFYING_FLAGS.isdisjoint(r.flags or [])
        and float(r.quality)
        >= float((current[r.collection_id].rubric or {}).get("acceptable_quality", DEFAULT_ACCEPTABLE_QUALITY))
    ]
    if not candidates:
        return {}
    targets_by_id = {
        t.id: t
        for t in (
            await db.execute(
                select(BatchTarget)
                .where(BatchTarget.id.in_([r.target_id for r in candidates]))
                .execution_options(populate_existing=True)
            )
        ).scalars()
    }
    qualifying = [
        (r, t)
        for r in candidates
        if (t := targets_by_id.get(r.target_id)) is not None
        and target_class(t) == "success"
        and not is_stale(r, t)
        and r.evaluated_run_id == t.run_id
    ]
    if not qualifying:
        return {}
    batches: dict[uuid.UUID, Batch] = {}
    from tret.batches.profiles.disclosure import _package_of, _relevant_maps

    if with_sources:
        wanted = sorted({r.collection_id for r, _ in qualifying}, key=str)
        batches = {
            b.id: b for b in (await db.execute(select(Batch).where(Batch.id.in_(wanted)))).scalars()
        }
    out: dict[str, list[dict]] = {}
    for cid in sorted({r.collection_id for r, _ in qualifying}, key=str):
        other = batches.get(cid)
        mine = [(r, t) for r, t in qualifying if r.collection_id == cid]
        findings = (
            await core.load_review_findings(db, profile_for(other), [t for _, t in mine], other)
            if with_sources and other is not None
            else {}
        )
        for r, t in mine:
            urls: list[str] = []
            for f in _relevant_maps(findings.get(t.run_id, []) if t.run_id else []):
                package = _package_of(f)
                if f.status != "approved" or not package or package.get("status") != "packaged":
                    continue
                url = ((f.payload or {}).get("source") or {}).get("url")
                if isinstance(url, str) and url and url not in urls:
                    urls.append(url)
            outcome = t.outcome if isinstance(t.outcome, dict) else {}
            status, period = outcome.get("status"), outcome.get("reporting_period_label")
            out.setdefault(r.row_key, []).append(
                {
                    "batch_id": str(cid),
                    "batch_name": other.name if other is not None else None,
                    "target_id": str(t.id),
                    "quality": float(r.quality),
                    "finished_at": core._iso(t.finished_at),
                    "status": status if isinstance(status, str) and status else None,
                    "period": period if isinstance(period, str) and period else None,
                    "source_urls": urls[:CROSS_CHECK_MAX],
                }
            )
    for key, items in out.items():
        items.sort(
            key=lambda e: (-e["quality"], e["finished_at"] is None, e["finished_at"] or "", e["target_id"])
        )
        out[key] = items[:CROSS_CHECK_MAX]
    return out


async def recompute_relative(db: AsyncSession, ev: BatchEvaluation) -> None:
    """Rank, percentile and expected-vs-actual for every scored row of the pass.

    Expected: with k scored rows of the same row key in other batches' current
    passes (mean h) and the pass mean m, `(k x h + 2 x m) / (k + 2)`; with none,
    the batch mean moved by 5 points per difficulty step away from the pass's
    mean difficulty. Commits."""
    batch = await db.get(Batch, ev.collection_id)
    rows = [r for r in await pass_rows(db, ev.id) if r.status == "scored" and r.quality is not None]
    if batch is None or not rows:
        return
    qualities = [float(r.quality) for r in rows]
    n = len(rows)
    mean = statistics.fmean(qualities)
    difficulties = [r.difficulty for r in rows if r.difficulty is not None]
    mean_difficulty = statistics.fmean(difficulties) if difficulties else None
    history = await _history(db, ev, {r.row_key for r in rows})
    data = await collect(db, batch)
    outliers_by_target: dict[str, list[dict]] = {}
    for o in outliers(data):
        outliers_by_target.setdefault(o["target_id"], []).append(o)
    for r in rows:
        q = float(r.quality)
        rank = 1 + sum(1 for other in qualities if other > q)
        past = history.get(r.row_key) or []
        if past:
            k = len(past)
            expected = (k * statistics.fmean(past) + 2 * mean) / (k + 2)
            basis = f"row history (n={k})"
        else:
            offset = 0.0
            if r.difficulty is not None and mean_difficulty is not None:
                offset = 5 * (r.difficulty - mean_difficulty)
            expected = mean - offset
            basis = "batch mean, difficulty-adjusted"
        gap_check = (r.relative or {}).get("gap_check") if isinstance(r.relative, dict) else None
        r.relative = {
            "rank": rank,
            "of": n,
            "percentile": round(100 * (n - rank) / (n - 1), 1) if n > 1 else 100.0,
            "expected": round(expected, 1),
            "expected_basis": basis,
            "residual": round(q - expected, 1),
            "outliers": outliers_by_target.get(str(r.target_id), []),
            # Written by the evidence pack of a gap row, not a score: kept as it was.
            **({"gap_check": gap_check} if gap_check is not None else {}),
        }
    await db.commit()


def decimal(value: float | None, places: int = 2) -> Decimal | None:
    return None if value is None else Decimal(str(round(value, places)))
