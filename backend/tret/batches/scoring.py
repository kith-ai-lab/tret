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
- `primaries_elsewhere`: for a disclosure batch's delivered rows, the main documents
  that other batches of the family packaged for the same company and this run did
  not (the mirror image of the gap cross-check; the judge decides).
- `recompute_relative`: rank, percentile and expected-vs-actual (residual) for
  the scored rows of a pass.
"""
from __future__ import annotations

import hashlib
import random
import re
import statistics
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import Select, select
from sqlalchemy.orm import load_only
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
# Package-map roles that are the issuer's main disclosure document (the rest are
# companions: an index, a scorecard, a data appendix, a glossary).
MAIN_DOCUMENT_ROLES = frozenset({"primary_disclosure", "embedded_statement_and_assurance"})
# A main-role source of another run below this share of the largest main-role source
# this run packaged is not a missing main document (an index labelled primary).
MAIN_DOCUMENT_MIN_RATIO = 0.25
# ... and only when it is also this short: an 80-page report is still disputed by a
# 400-page filing's run.
MAIN_DOCUMENT_SMALL_PAGES = 20
BOOTSTRAP_RESAMPLES = 2000
# Statuses of an evaluation pass that do not count as "the current pass".
DEAD_PASS_STATUSES = ("cancelled", "failed")

_DEDUPE_SUFFIX_RE = re.compile(r"\s#\d+$")


# ══════════════════════════════════════════════════════════════════════════
# keys
# ══════════════════════════════════════════════════════════════════════════
def family_key_for(
    profile: str,
    *,
    fiscal_year: Any = None,
    harness_id: Any = None,
    template: str | None = None,
    run_slug: str | None = None,
) -> str:
    """The family key from its parts (the same rule `family_key` applies to a stored
    batch), so a batch that does not exist yet can ask which family it would join."""
    if profile == "disclosure":
        return f"disclosure:fy{fiscal_year}"
    if profile == "prompt":
        digest = hashlib.sha256(str(template or "").encode("utf-8")).hexdigest()[:12]
        return f"prompt:{harness_id}:{digest}"
    return f"{profile}:{run_slug}"


def family_key(batch: Batch) -> str:
    """What makes two batches comparable: `disclosure:fy<year>` for the disclosure
    profile, `prompt:<harness id>:<sha256(template)[:12]>` for a prompt batch;
    `<profile>:<run_slug>` for a profile with no family notion (never comparable
    with another batch)."""
    settings = batch.settings or {}
    profile = batch.profile or "disclosure"
    return family_key_for(
        profile,
        fiscal_year=settings.get("fiscal_year"),
        harness_id=settings.get("harness_id") or (batch.harness_id if profile == "prompt" else None),
        template=str(settings.get("template") or ""),
        run_slug=batch.run_slug,
    )


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


def primary_checked_groups(row: TargetEvaluation | None) -> list[set[str]]:
    """The documents a delivered row's last judging was shown as main documents other
    runs packaged and this run did not: one set of keys (every `sha256:` / `url:` key
    of the document) per shown document. A plain string entry is a one-key document."""
    rel = row.relative if row is not None and isinstance(row.relative, dict) else {}
    check = rel.get("primary_check")
    shown = check.get("checked_against") if isinstance(check, dict) else None
    out: list[set[str]] = []
    for entry in shown if isinstance(shown, list) else []:
        keys = entry if isinstance(entry, list) else [entry]
        out.append({str(k) for k in keys})
    return out


def same_documents(current: list[set[str]], recorded: list[set[str]]) -> bool:
    """True when every current document shares a key with a recorded one and every
    recorded document shares a key with a current one (both empty: True). A document
    that gains a hash or a URL stays the same document; a new or vanished one does not."""
    return all(any(c & r for r in recorded) for c in current) and all(
        any(c & r for c in current) for r in recorded
    )


def primary_rechecked(row: TargetEvaluation | None, evidence: list[dict]) -> bool:
    """True when `evidence` is not empty and the row was judged with exactly those
    documents in view. The identity is the DOCUMENT (sha256 or URL), not the run: another
    run delivering the same document must not re-open a row."""
    return bool(evidence) and same_documents(
        [set(e.get("keys") or [e["key"]]) for e in evidence], primary_checked_groups(row)
    )


def _page_count(value: Any) -> int | None:
    """A page count as an int (a JSON number may arrive as 400, 400.0 or "400.0")."""
    try:
        return int(float(value)) if value not in (None, "") else None
    except (TypeError, ValueError, OverflowError):
        return None


def package_sources(findings: Iterable[Any]) -> list[dict]:
    """The sources of the run's approved, packaged `package_map` findings: role, URL,
    sha256 and page count of each (what `delivered_elsewhere` reads for its URLs)."""
    from tret.batches.profiles.disclosure import _package_of, _relevant_maps

    out: list[dict] = []
    for f in _relevant_maps(findings):
        package = _package_of(f)
        if f.status != "approved" or not package or package.get("status") != "packaged":
            continue
        payload = f.payload or {}
        source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
        map_ = payload.get("map") if isinstance(payload.get("map"), dict) else {}
        out.append(
            {
                "role": map_.get("role"),
                "url": source.get("url"),
                "sha256": source.get("sha256"),
                "page_count": _page_count(source.get("page_count")),
            }
        )
    return out


def _normal_url(url: Any) -> str | None:
    """A URL for comparing documents: `<host><path>[?query]` with the scheme, a leading
    `www.`, a default port (80, 443), the fragment and one trailing slash of the path
    dropped and the host lower-cased; the query is kept."""
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        parts = urlsplit(url.strip())
        host, port = (parts.hostname or "").lower(), parts.port
    except ValueError:
        return None
    if not host:
        return None
    host = host[4:] if host.startswith("www.") else host
    if port not in (None, 80, 443):
        host = f"{host}:{port}"
    path = parts.path[:-1] if parts.path.endswith("/") else parts.path
    return f"{host}{path}" + (f"?{parts.query}" if parts.query else "")


def source_keys(sources: Iterable[dict]) -> set[str]:
    """`sha256:<sha>` for each hash and `url:<normalised url>` for each URL."""
    keys: set[str] = set()
    for src in sources:
        sha = src.get("sha256") if isinstance(src, dict) else None
        if isinstance(sha, str) and sha.strip():
            keys.add(f"sha256:{sha.strip().lower()}")
        url = _normal_url(src.get("url") if isinstance(src, dict) else None)
        if url:
            keys.add(f"url:{url}")
    return keys


def missing_main_documents(own_sources: list[dict], other_sources: list[dict]) -> list[dict]:
    """The main-document sources in `other_sources` that match none of `own_sources`
    (any role) by sha256 or by normalised URL. Not missing: a source whose page count
    is below `MAIN_DOCUMENT_MIN_RATIO` of the largest main-role source of `own_sources`
    AND below `MAIN_DOCUMENT_SMALL_PAGES` (a 100-page report is not disputed by someone's
    11-page index labelled primary; a 400-page filing is still disputed by an 80-page
    report);
    with an unknown page count on either side, or no main-role source of its own, there
    is no such guard."""
    own = source_keys(own_sources)
    own_pages = [
        s["page_count"]
        for s in own_sources
        if s.get("role") in MAIN_DOCUMENT_ROLES and isinstance(s.get("page_count"), (int, float))
    ]
    floor = MAIN_DOCUMENT_MIN_RATIO * max(own_pages) if own_pages else None
    out = []
    for s in other_sources:
        if s.get("role") not in MAIN_DOCUMENT_ROLES:
            continue
        keys = source_keys([s])
        if not keys or not own.isdisjoint(keys):
            continue
        pages = s.get("page_count")
        if (
            floor is not None
            and isinstance(pages, (int, float))
            and pages < floor
            and pages < MAIN_DOCUMENT_SMALL_PAGES
        ):
            continue
        out.append(s)
    return out


async def scorecard_from(db: AsyncSession, data: BatchData, *, router_gate: dict | None = None) -> dict:
    from tret.batches import evaluation as evaluation_service
    from tret.batches.gate import safe_family_gate

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
        qualifying = await _qualifying_elsewhere(
            db, batch, {d.row for d in data.targets if d.cls in ("gap", "success")}
        )
        elsewhere = await delivered_elsewhere(
            db, batch, {d.row for d in gaps}, with_sources=False, qualifying=qualifying
        )
        disputed = [d for d in gaps if elsewhere.get(d.row)]
        quality["gaps_disputed"] = len(disputed)
        quality["gaps_rechecked"] = sum(
            1 for d in disputed if gap_rechecked(d.evaluation, elsewhere[d.row])
        )
        primaries = await primaries_elsewhere(db, batch, [d.target for d in data.targets], qualifying=qualifying)
        disputed_main = [d for d in data.targets if d.cls == "success" and primaries.get(d.row)]
        quality["primaries_disputed"] = len(disputed_main)
        quality["primaries_rechecked"] = sum(
            1 for d in disputed_main if primary_rechecked(d.evaluation, primaries[d.row])
        )
    evaluation_block = (
        await evaluation_service.pass_summary(db, data.current, data.rows) if data.current else None
    )
    if evaluation_block is not None:
        if router_gate is None:
            router_gate = await safe_family_gate(db, f"{family_key(batch)}@{batch.workspace_id}")
        evaluation_block = {**evaluation_block, "router_gate": router_gate}
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
        "evaluation": evaluation_block,
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


def picked_rows(data: BatchData) -> dict[str, TargetData]:
    """One target per row key (`_pick`)."""
    groups: dict[str, list[TargetData]] = {}
    for d in data.targets:
        groups.setdefault(d.row, []).append(d)
    return {key: _pick(members) for key, members in groups.items()}


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


def paired_summary(data_a: BatchData, data_b: BatchData, *, bootstrap: bool = True) -> dict:
    """The paired quality numbers of two already-collected batches (B minus A over the
    rows both delivered or gapped): `{quality_comparable, quality_note, n, wins_a, wins_b,
    ties, mean_quality_delta, interval90}`. The numbers are None when the batches are not
    comparable; `bootstrap=False` leaves the interval None. `compare_batches` uses it."""
    note = _quality_note(data_a, data_b)
    comparable = note is None
    picked_a, picked_b = picked_rows(data_a), picked_rows(data_b)
    shared = sorted((k for k in picked_a if k in picked_b), key=lambda k: picked_a[k].target.position)
    deltas = []
    for key in shared:
        d = _delta(_side(picked_a[key])["quality"], _side(picked_b[key])["quality"], 2)
        if d is not None:
            deltas.append(d)
    out: dict[str, Any] = {
        "quality_comparable": comparable,
        "quality_note": note,
        "n": len(deltas) if comparable else 0,
        "wins_a": None,
        "wins_b": None,
        "ties": None,
        "mean_quality_delta": None,
        "interval90": None,
    }
    if comparable:
        out["wins_a"] = sum(1 for d in deltas if d <= -TIE_POINTS)
        out["wins_b"] = sum(1 for d in deltas if d >= TIE_POINTS)
        out["ties"] = len(deltas) - out["wins_a"] - out["wins_b"]
        out["mean_quality_delta"] = round(statistics.fmean(deltas), 2) if deltas else None
        out["interval90"] = bootstrap_interval(deltas) if bootstrap else None
    return out


async def compare_batches(db: AsyncSession, a: Batch, b: Batch) -> dict:
    data_a, data_b = await collect(db, a), await collect(db, b)
    card_a, card_b = await scorecard_from(db, data_a), await scorecard_from(db, data_b)
    note = _quality_note(data_a, data_b)
    comparable = note is None
    picked_a, picked_b = picked_rows(data_a), picked_rows(data_b)
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
        disputed_primary = None
        if card_a["family_key"] == card_b["family_key"] and da.cls == db_.cls == "success":
            a_missing = _qualifying_delivery(db_) and missing_main_documents(
                package_sources(da.findings), package_sources(db_.findings)
            )
            b_missing = _qualifying_delivery(da) and missing_main_documents(
                package_sources(db_.findings), package_sources(da.findings)
            )
            if a_missing and b_missing:
                disputed_primary = "both"
            elif a_missing:
                disputed_primary = "a"
            elif b_missing:
                disputed_primary = "b"
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
                "disputed_primary": disputed_primary,
                "delta": {
                    "quality": _delta(sa["quality"], sb["quality"], 2),
                    "cost_usd": _delta(sa["cost_usd"], sb["cost_usd"]),
                    "energy_wh": _delta(sa["energy_wh"], sb["energy_wh"]),
                    "retained_pages": _delta(sa["retained_pages"], sb["retained_pages"], 0),
                },
            }
        )
    pair = paired_summary(data_a, data_b)
    wins_a, wins_b, ties = pair["wins_a"], pair["wins_b"], pair["ties"]
    mean_delta, interval = pair["mean_quality_delta"], pair["interval90"]
    if comparable:
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
    from tret.batches import pairwise

    overlay, pairwise_summary = await pairwise.compare_overlay(
        db, a, b, data_a, data_b, picked_a, picked_b, comparable
    )
    for r in rows:
        r["pairwise"] = overlay.get(r["row_key"])
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
            "pairwise": pairwise_summary,
            "disputed_gaps_a": sum(1 for r in rows if r["disputed_gap"] == "a"),
            "disputed_gaps_b": sum(1 for r in rows if r["disputed_gap"] == "b"),
            "disputed_primaries_a": sum(1 for r in rows if r["disputed_primary"] in ("a", "both")),
            "disputed_primaries_b": sum(1 for r in rows if r["disputed_primary"] in ("b", "both")),
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


async def _qualifying_elsewhere(
    db: AsyncSession, batch: Batch, row_keys: set[str]
) -> list[tuple[TargetEvaluation, BatchTarget]]:
    """The scored rows of OTHER batches of this batch's family that count as an
    acceptable delivery of one of `row_keys` (see `delivered_elsewhere`), with their
    targets. Empty for a batch that is not a disclosure batch. Three queries: the
    passes of the other batches, the scored rows of their current passes (only the
    columns read here) and the targets. Callers that need several row-key sets call it
    once and pass the result on (`qualifying=`)."""
    if (batch.profile or "disclosure") != "disclosure" or not row_keys:
        return []
    # The current pass of each batch is picked here by `current_pass`'s rule (latest not
    # cancelled or failed, else latest).
    passes = list(
        (
            await db.execute(
                select(BatchEvaluation)
                .where(
                    BatchEvaluation.workspace_id == batch.workspace_id,
                    BatchEvaluation.family_key == family_key(batch),
                    BatchEvaluation.collection_id != batch.id,
                )
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
    by_pass = {p.id: p for p in current.values()}
    if not by_pass:
        return []
    candidates = [
        r
        for r in (
            await db.execute(
                select(TargetEvaluation)
                .options(
                    load_only(
                        TargetEvaluation.id,
                        TargetEvaluation.evaluation_id,
                        TargetEvaluation.collection_id,
                        TargetEvaluation.target_id,
                        TargetEvaluation.row_key,
                        TargetEvaluation.attempt,
                        TargetEvaluation.evaluated_run_id,
                        TargetEvaluation.status,
                        TargetEvaluation.quality,
                        TargetEvaluation.flags,
                    )
                )
                .where(
                    TargetEvaluation.evaluation_id.in_(sorted(by_pass, key=str)),
                    TargetEvaluation.status == "scored",
                    TargetEvaluation.row_key.in_(sorted(row_keys)),
                )
            )
        ).scalars()
        if r.quality is not None
        and DISQUALIFYING_FLAGS.isdisjoint(r.flags or [])
        and float(r.quality)
        >= float((by_pass[r.evaluation_id].rubric or {}).get("acceptable_quality", DEFAULT_ACCEPTABLE_QUALITY))
    ]
    if not candidates:
        return []
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
    return [
        (r, t)
        for r in candidates
        if (t := targets_by_id.get(r.target_id)) is not None
        and target_class(t) == "success"
        and not is_stale(r, t)
        and r.evaluated_run_id == t.run_id
    ]


async def delivered_elsewhere(
    db: AsyncSession,
    batch: Batch,
    row_keys: set[str],
    *,
    with_sources: bool = True,
    qualifying: list[tuple[TargetEvaluation, BatchTarget]] | None = None,
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
    the batch page polls them; only the judge's evidence pack shows the URLs.

    `qualifying`: a pre-computed `_qualifying_elsewhere` list (for these keys or a
    superset); it is filtered by row key here."""
    if (batch.profile or "disclosure") != "disclosure" or not row_keys:
        return {}
    if qualifying is None:
        qualifying = await _qualifying_elsewhere(db, batch, row_keys)
    qualifying = [(r, t) for r, t in qualifying if r.row_key in row_keys]
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


def _package_sources_statement(run_ids: list[uuid.UUID]) -> Select:
    """The one query behind `_package_sources_by_run`: JSON paths only, no payloads."""
    return (
        select(
            Finding.run_id,
            Finding.payload["map"]["role"].as_string(),
            Finding.payload["source"]["url"].as_string(),
            Finding.payload["source"]["sha256"].as_string(),
            Finding.payload["source"]["page_count"].as_string(),
        )
        .where(
            Finding.run_id.in_(run_ids),
            Finding.schema_slug == "package_map",
            Finding.status == "approved",
            Finding.payload["package"]["status"].as_string() == "packaged",
        )
        .order_by(Finding.created_at, Finding.id)
    )


async def _package_sources_by_run(
    db: AsyncSession, run_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, list[dict]]:
    """`package_sources` for many runs in ONE query that reads only the few JSON
    paths it needs (a package map's payload can be large)."""
    ids = sorted({r for r in run_ids if r is not None}, key=str)
    if not ids:
        return {}
    rows = await db.execute(_package_sources_statement(ids))
    out: dict[uuid.UUID, list[dict]] = {}
    for run_id, role, url, sha, pages in rows:
        # Postgres may render a JSON number as text such as "100.0".
        page_count = _page_count(pages)
        out.setdefault(run_id, []).append(
            {"role": role, "url": url, "sha256": sha, "page_count": page_count}
        )
    return out


async def primaries_elsewhere(
    db: AsyncSession,
    batch: Batch,
    targets: list[BatchTarget],
    *,
    qualifying: list[tuple[TargetEvaluation, BatchTarget]] | None = None,
) -> dict[str, list[dict]]:
    """For each delivered (success-class) target of this batch, by row key: the main
    documents (`MAIN_DOCUMENT_ROLES`) that acceptable deliveries of the same company in
    other batches of the family packaged and this target did not. Grouped by document
    (same sha256 or same normalised URL); each item has `key` (display only), `keys` (every
    `sha256:` / `url:` key of the document: its identity), `url`, `page_count`, `runs`
    (distinct other targets that packaged it), `best_quality` and `target_ids`. Most runs
    first, at most `CROSS_CHECK_MAX`. Row keys with nothing disputed are absent. Never
    carries a batch or model name. A fixed number of queries. `qualifying`: a
    pre-computed `_qualifying_elsewhere` list (filtered by row key here)."""
    if (batch.profile or "disclosure") != "disclosure":
        return {}
    mine = [t for t in targets if target_class(t) == "success"]
    if not mine:
        return {}
    keys = {row_key(batch, t) for t in mine}
    if qualifying is None:
        qualifying = await _qualifying_elsewhere(db, batch, keys)
    qualifying = [(r, t) for r, t in qualifying if r.row_key in keys]
    if not qualifying:
        return {}
    run_ids = {t.run_id for t in mine if t.run_id} | {t.run_id for _, t in qualifying if t.run_id}
    sources = await _package_sources_by_run(db, run_ids)
    own_by_key: dict[str, list[dict]] = {}
    for t in mine:
        own_by_key.setdefault(row_key(batch, t), []).extend(sources.get(t.run_id, []))
    groups_by_key: dict[str, list[dict]] = {}
    for r, t in sorted(qualifying, key=lambda rt: str(rt[1].id)):
        missing = missing_main_documents(own_by_key.get(r.row_key, []), sources.get(t.run_id, []))
        groups = groups_by_key.setdefault(r.row_key, [])
        for src in missing:
            skeys = source_keys([src])
            hit = [g for g in groups if g["keys"] & skeys]
            if hit:
                group = hit[0]
                for other in hit[1:]:  # the source bridges two groups: one document
                    group["keys"] |= other["keys"]
                    group["sources"] += other["sources"]
                    group["targets"] |= other["targets"]
                    group["best"] = max(group["best"], other["best"])
                    groups.remove(other)
            else:
                group = {"keys": set(), "sources": [], "targets": set(), "best": 0.0}
                groups.append(group)
            group["keys"] |= skeys
            group["sources"].append(src)
            group["targets"].add(str(t.id))
            group["best"] = max(group["best"], float(r.quality))
    out: dict[str, list[dict]] = {}
    for rk, groups in groups_by_key.items():
        items = []
        for g in groups:
            ordered = sorted(g["keys"])
            # Order-independent identity: the smallest hash, else the smallest URL.
            key = next((k for k in ordered if k.startswith("sha256:")), ordered[0])
            urls = [s["url"] for s in g["sources"] if isinstance(s.get("url"), str) and s["url"]]
            pages = [s["page_count"] for s in g["sources"] if s.get("page_count") is not None]
            items.append(
                {
                    "key": key,
                    "keys": ordered,
                    # Order-independent: the shortest (then smallest) URL, the largest page count.
                    "url": min(urls, key=lambda u: (len(u), u)) if urls else None,
                    "page_count": max(pages) if pages else None,
                    "runs": len(g["targets"]),
                    "best_quality": g["best"],
                    "target_ids": sorted(g["targets"]),
                }
            )
        # Not by quality: a re-score elsewhere must not rotate which documents are shown.
        items.sort(key=lambda e: (-e["runs"], e["key"]))
        if items:
            out[rk] = items[:CROSS_CHECK_MAX]
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
        primary_check = (r.relative or {}).get("primary_check") if isinstance(r.relative, dict) else None
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
            **({"primary_check": primary_check} if primary_check is not None else {}),
        }
    await db.commit()


def decimal(value: float | None, places: int = 2) -> Decimal | None:
    return None if value is None else Decimal(str(round(value, places)))
