"""The extraction review list: what a human should look at after a Stage 2 extraction run.

The items are COMPUTED from the stored extractions every time (`review_items`), never stored, so
a rule change or a new threshold applies at once. What is stored is the reviewer's decision on an
item (`ExtractionReview`: accepted | dismissed | fixed), keyed by a stable `item_key`, so a
decided item does not come back.

Item kinds, with the severity order they are listed in (failed, partial, NACE, then module issues):

* `extraction_failed`, `extraction_partial` (keys `status:failed`, `status:partial`): the
  extraction's status. A failed extraction lists only this item (it has no modules to judge).
* The NACE items are about the COMPANY, not one report, so each is listed once per company, on
  the company's most recent non-failed extraction (a re-extraction moves them there):
  `nace_low_confidence` (the effective classification is the extractor's own and its confidence is
  below the floor; a `sec_sic` or `manual` row outranks it, so there is nothing to look at; fix it
  with `PUT /api/company-classifications/manual`), `nace_missing` (no effective classification at
  all) and `gics_disagrees` (the effective classification is not manual and the register's GICS
  sector does not fit its cluster).
* `module_low_confidence`, `module_present_no_text`, `module_truncated`: present modules.
* `module_low_density`: a present module of at least `MIN_DENSITY_WORDS` words with fewer claims
  per 100 words than the floor (`tret/evaluation/density.py`): likely boilerplate.
* `span_issues`: one item per (issue kind, module) of `invalid_span`, `reversed_span`,
  `present_without_valid_span`, `invalid_block`.

Thresholds: `disclosure_review_min_confidence` (0.6), `disclosure_review_min_density` (0.5).
A re-extraction that replaces an extraction's contents (`extract.store_result`) also deletes that
extraction's decisions, so the new result gets a fresh review.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Iterable

from sqlalchemy import or_, select

from tret.config import get_settings

DEFAULT_MIN_CONFIDENCE = 0.6
DEFAULT_MIN_DENSITY = 0.5
MIN_DENSITY_WORDS = 100

DECISIONS = ("accepted", "dismissed", "fixed")
SPAN_ISSUE_KINDS = ("invalid_span", "reversed_span", "present_without_valid_span", "invalid_block")

K_FAILED = "extraction_failed"
K_PARTIAL = "extraction_partial"
K_NACE_LOW = "nace_low_confidence"
K_NACE_MISSING = "nace_missing"
K_GICS = "gics_disagrees"
K_MOD_LOW = "module_low_confidence"
K_MOD_NO_TEXT = "module_present_no_text"
K_MOD_TRUNC = "module_truncated"
K_MOD_DENSITY = "module_low_density"
K_SPAN = "span_issues"

KINDS = (K_FAILED, K_PARTIAL, K_NACE_LOW, K_NACE_MISSING, K_GICS, K_MOD_LOW, K_MOD_NO_TEXT, K_MOD_TRUNC, K_MOD_DENSITY, K_SPAN)
CHUNK = 400  # ids per IN (...) list, under SQLite's bound-parameter limit
DEFAULT_LIMIT = 500
MAX_LIMIT = 5000
_SEVERITY = {K_FAILED: 0, K_PARTIAL: 1, K_NACE_LOW: 2, K_NACE_MISSING: 2, K_GICS: 2}  # others: 3

NACE_FIX = "fix it with PUT /api/company-classifications/manual"


class ReviewNotFound(LookupError):
    """The extraction (in this workspace) or the item key does not exist."""


@dataclass
class ReviewItem:
    extraction_id: uuid.UUID
    disclosure_report_id: uuid.UUID
    batch_target_id: uuid.UUID | None
    company: str | None
    kind: str
    item_key: str
    detail: str
    module: str | None = None
    value: float | None = None
    decision: dict[str, Any] | None = field(default=None)

    @property
    def severity(self) -> int:
        return _SEVERITY.get(self.kind, 3)

    def as_dict(self) -> dict[str, Any]:
        return {
            "extraction_id": str(self.extraction_id),
            "disclosure_report_id": str(self.disclosure_report_id),
            "batch_target_id": str(self.batch_target_id) if self.batch_target_id else None,
            "company": self.company,
            "kind": self.kind,
            "item_key": self.item_key,
            "module": self.module,
            "detail": self.detail,
            "value": self.value,
            "severity": self.severity,
            "decision": self.decision,
        }


def thresholds(settings: Any = None) -> tuple[float, float]:
    """(min confidence, min density) from the settings."""
    s = settings or get_settings()
    return (
        float(getattr(s, "disclosure_review_min_confidence", DEFAULT_MIN_CONFIDENCE)),
        float(getattr(s, "disclosure_review_min_density", DEFAULT_MIN_DENSITY)),
    )


# ── selecting extractions ───────────────────────────────────────────────────────────────────
async def extraction_ids_for_batch(db, workspace_id: uuid.UUID, batch_id: uuid.UUID) -> list[uuid.UUID]:
    """The workspace's extractions made from `batch_id`: either the source collection batch the
    reports came from (`tret disclosure extract --from-batch`) or the extraction batch itself."""
    from tret.db.models import BatchTarget, DisclosureReport, ReportExtraction

    targets = select(BatchTarget.id).where(BatchTarget.collection_id == batch_id)
    reports = select(DisclosureReport.id).where(DisclosureReport.batch_target_id.in_(targets))
    stmt = select(ReportExtraction.id).where(
        ReportExtraction.workspace_id == workspace_id,
        or_(ReportExtraction.batch_target_id.in_(targets), ReportExtraction.disclosure_report_id.in_(reports)),
    )
    return list((await db.execute(stmt)).scalars().all())


def _item(e, target, kind: str, key: str, detail: str, *, module: str | None = None, value: float | None = None) -> ReviewItem:
    return ReviewItem(
        extraction_id=e.id, disclosure_report_id=e.disclosure_report_id, batch_target_id=e.batch_target_id,
        company=(getattr(target, "company", None) or getattr(target, "label", None)) if target is not None else None,
        kind=kind, item_key=key, detail=detail, module=module, value=value,
    )


def _items_for(e, modules, target, min_conf: float, min_dens: float) -> list[ReviewItem]:
    """Every extraction-level item of one extraction (no decisions applied, no NACE items)."""
    items: list[ReviewItem] = []
    if e.status == "failed":
        errors = [i for i in (e.issues or []) if i.get("kind") in ("window_failed", "cost_cap", "no_blocks")]
        why = (errors[-1].get("detail") if errors else None) or "no window could be extracted"
        items.append(_item(e, target, K_FAILED, "status:failed", f"extraction failed: {str(why)[:200]}"))
        return items
    if e.status == "partial":
        failed = sum(1 for i in (e.issues or []) if i.get("kind") in ("window_failed", "cost_cap"))
        items.append(_item(e, target, K_PARTIAL, "status:partial", f"partial extraction: {failed} window(s) failed or were cut by the cost cap"))

    for m in sorted(modules, key=lambda x: x.module):
        if not m.present:
            continue
        if m.confidence is not None and m.confidence < min_conf:
            items.append(_item(e, target, K_MOD_LOW, f"module:{m.module}:low_confidence",
                               f"{m.module}: confidence {m.confidence:.2f} is below {min_conf:g}", module=m.module, value=m.confidence))
        if not m.text_key or not m.word_count:
            items.append(_item(e, target, K_MOD_NO_TEXT, f"module:{m.module}:no_text",
                               f"{m.module}: marked present but has no text", module=m.module))
        if m.truncated:
            items.append(_item(e, target, K_MOD_TRUNC, f"module:{m.module}:truncated",
                               f"{m.module}: cut to the module's word limit; check the kept spans", module=m.module,
                               value=float(m.word_count)))
        if m.claim_density is not None and m.word_count >= MIN_DENSITY_WORDS and m.claim_density < min_dens:
            items.append(_item(e, target, K_MOD_DENSITY, f"module:{m.module}:low_density",
                               f"{m.module}: {m.claim_density:.2f} claims per 100 words over {m.word_count} words "
                               f"(below {min_dens:g}); likely boilerplate", module=m.module, value=m.claim_density))

    groups: dict[tuple[str, str | None], int] = {}
    for i in e.issues or []:
        if i.get("kind") in SPAN_ISSUE_KINDS:
            k = (i["kind"], i.get("module") or i.get("field"))
            groups[k] = groups.get(k, 0) + 1
    for (kind, where), n in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1] or "")):
        items.append(_item(e, target, K_SPAN, f"issue:{kind}:{where or '-'}",
                           f"{kind} on {where or 'the report'} ({n}x): the model's span was rejected",
                           module=where if where and any(m.module == where for m in modules) else None, value=float(n)))

    return items


def _nace_items(e, target, effective, min_conf: float) -> list[ReviewItem]:
    """The company-level NACE items; `e` is the company's most recent non-failed extraction."""
    items: list[ReviewItem] = []
    if effective is None:
        items.append(_item(e, target, K_NACE_MISSING, "nace:missing",
                           f"no NACE classification for the company; {NACE_FIX}"))
        return items
    conf = effective.confidence
    if effective.source == "extractor" and isinstance(conf, (int, float)) and conf < min_conf:
        items.append(_item(e, target, K_NACE_LOW, "nace:low_confidence",
                           f"NACE {effective.nace_group or effective.nace_division} classified by the extractor at "
                           f"confidence {conf:.2f} (below {min_conf:g}) and nothing outranks it; {NACE_FIX}", value=float(conf)))
    if effective.source != "manual" and effective.gics_disagrees:
        items.append(_item(e, target, K_GICS, "nace:gics_disagrees",
                           f"the register's GICS sector ({effective.gics_sector or 'unknown'}) does not fit the "
                           f"{effective.source} classification {effective.source_code or '(no code)'}; {NACE_FIX}"))
    return items


def _chunks(seq: list, n: int = CHUNK):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


async def _targets_for(db, rows: list[tuple[uuid.UUID, uuid.UUID | None, uuid.UUID | None]]) -> dict[uuid.UUID, Any]:
    """Extraction id -> its company's BatchTarget (the extraction's own batch target, else the
    source target of its report). `rows` are (extraction id, batch_target_id, disclosure_report_id)."""
    from tret.db.models import BatchTarget, DisclosureReport

    need = list({r for _e, _t, r in rows})
    report_target: dict[uuid.UUID, uuid.UUID | None] = {}
    for part in _chunks(need):
        for rid, tid in (await db.execute(
            select(DisclosureReport.id, DisclosureReport.batch_target_id).where(DisclosureReport.id.in_(part))
        )).all():
            report_target[rid] = tid
    target_ids = list({t or report_target.get(r) for _e, t, r in rows} - {None})
    targets: dict[uuid.UUID, Any] = {}
    for part in _chunks(target_ids):
        for t in (await db.execute(select(BatchTarget).where(BatchTarget.id.in_(part)))).scalars():
            targets[t.id] = t
    return {eid: targets.get(t or report_target.get(r)) for eid, t, r in rows}


async def _latest_by_company(db, workspace_id: uuid.UUID) -> dict[tuple[str, str], uuid.UUID]:
    """Company (its most specific key) -> its most recent non-failed extraction in the workspace;
    the one extraction that carries the company's NACE items. Loads light columns only."""
    from tret.db.models import ReportExtraction

    rows = (await db.execute(
        select(ReportExtraction.id, ReportExtraction.batch_target_id, ReportExtraction.disclosure_report_id,
               ReportExtraction.created_at, ReportExtraction.status)
        .where(ReportExtraction.workspace_id == workspace_id, ReportExtraction.status != "failed")
    )).all()
    from tret.services import peer_taxonomy as pt

    tgt = await _targets_for(db, [(r[0], r[1], r[2]) for r in rows])
    best: dict[tuple[str, str], tuple[Any, str, uuid.UUID]] = {}
    for eid, _bt, _rid, created, _st in rows:
        t = tgt.get(eid)
        if t is None:
            continue
        ck = pt.target_keys(t)[0]
        rank = (created, str(eid))
        if ck not in best or rank > best[ck][:2]:
            best[ck] = (created, str(eid), eid)
    return {ck: v[2] for ck, v in best.items()}


async def _load(db, workspace_id: uuid.UUID, extraction_ids: list[uuid.UUID] | None):
    """(extractions, modules by extraction, target by extraction, effective classification by
    extraction, companies whose NACE items this extraction carries: extraction id -> bool)."""
    from tret.db.models import ReportExtraction, ReportModule
    from tret.services import peer_taxonomy as pt

    if extraction_ids is not None and not extraction_ids:
        return [], {}, {}, {}, {}
    base = select(ReportExtraction).where(ReportExtraction.workspace_id == workspace_id)
    if extraction_ids is None:
        exts = list((await db.execute(base.order_by(ReportExtraction.created_at))).scalars().all())
    else:
        exts = []
        for part in _chunks(list(extraction_ids)):
            exts.extend((await db.execute(base.where(ReportExtraction.id.in_(part)))).scalars().all())
        exts.sort(key=lambda e: (e.created_at, str(e.id)))
    if not exts:
        return [], {}, {}, {}, {}
    mods: dict[uuid.UUID, list] = {}
    for part in _chunks([e.id for e in exts]):
        for m in (await db.execute(select(ReportModule).where(ReportModule.extraction_id.in_(part)))).scalars():
            mods.setdefault(m.extraction_id, []).append(m)
    tgt = await _targets_for(db, [(e.id, e.batch_target_id, e.disclosure_report_id) for e in exts])
    by_key = await pt.rows_for_keys(db, workspace_id, (k for t in tgt.values() if t is not None for k in pt.target_keys(t)))
    eff = {eid: (pt.resolve_for_keys(by_key, pt.target_keys(t))[0] if t is not None else None) for eid, t in tgt.items()}
    latest = await _latest_by_company(db, workspace_id)
    carries = {
        e.id: tgt.get(e.id) is not None and latest.get(pt.target_keys(tgt[e.id])[0]) == e.id for e in exts
    }
    return exts, mods, tgt, eff, carries


def _all_items(e, mods, tgt, eff, carries, min_conf, min_dens) -> list[ReviewItem]:
    its = _items_for(e, mods.get(e.id, []), tgt.get(e.id), min_conf, min_dens)
    if carries.get(e.id):
        its += _nace_items(e, tgt.get(e.id), eff.get(e.id), min_conf)
    return its


async def _decisions(db, extraction_ids: Iterable[uuid.UUID]) -> dict[tuple[uuid.UUID, str], Any]:
    from tret.db.models import ExtractionReview

    out: dict[tuple[uuid.UUID, str], Any] = {}
    for part in _chunks(list(extraction_ids)):
        for r in (await db.execute(select(ExtractionReview).where(ExtractionReview.extraction_id.in_(part)))).scalars():
            out[(r.extraction_id, r.item_key)] = r
    return out


def _decision_out(r) -> dict[str, Any]:
    return {
        "decision": r.decision, "note": r.note, "reviewed_by": r.reviewed_by,
        "reviewed_at": r.reviewed_at.isoformat() if r.reviewed_at else None,
    }


async def review_page(
    db, *, workspace_id: uuid.UUID, batch_id: uuid.UUID | None = None, include_resolved: bool = False,
    kinds: Iterable[str] | None = None, extraction_id: uuid.UUID | None = None,
    limit: int | None = None, offset: int = 0,
) -> tuple[list[ReviewItem], int]:
    """`(items, total)`: the review list of the workspace's extractions (of one batch with
    `batch_id`), most severe first, cut to `limit` items from `offset`; `total` counts the whole
    list. An item with a stored decision is left out unless `include_resolved`, and then carries
    its decision. `kinds` limits the list to those item kinds (unknown ones raise `ValueError`)."""
    wanted = set(kinds or [])
    if wanted - set(KINDS):
        raise ValueError(f"unknown kind(s): {', '.join(sorted(wanted - set(KINDS)))}")
    ids: list[uuid.UUID] | None = None
    if batch_id is not None:
        ids = await extraction_ids_for_batch(db, workspace_id, batch_id)
    if extraction_id is not None:
        ids = [extraction_id] if ids is None or extraction_id in ids else []
    exts, mods, tgt, eff, carries = await _load(db, workspace_id, ids)
    min_conf, min_dens = thresholds()
    decided = await _decisions(db, [e.id for e in exts])
    items: list[ReviewItem] = []
    for e in exts:
        for it in _all_items(e, mods, tgt, eff, carries, min_conf, min_dens):
            if wanted and it.kind not in wanted:
                continue
            d = decided.get((e.id, it.item_key))
            if d is not None:
                if not include_resolved:
                    continue
                it.decision = _decision_out(d)
            items.append(it)
    items.sort(key=lambda it: (it.severity, (it.company or "").lower(), it.item_key, str(it.extraction_id)))
    total = len(items)
    start = max(0, offset)
    return (items[start:] if limit is None else items[start:start + max(0, limit)]), total


async def review_items(db, *, limit: int | None = None, offset: int = 0, **kw) -> list[ReviewItem]:
    """`review_page` without the total (same arguments)."""
    return (await review_page(db, limit=limit, offset=offset, **kw))[0]


async def set_decision(
    db, *, workspace_id: uuid.UUID, extraction_id: uuid.UUID, item_key: str, decision: str,
    note: str | None, reviewed_by: str | None,
) -> dict[str, Any]:
    """Record (or replace) the decision on one item of one extraction. The item key must be one
    the extraction currently produces (a decided item that later stops being computed keeps its
    row, but cannot be newly decided). Raises `ValueError` for a bad decision and
    `ReviewNotFound` for an unknown extraction (or one in another workspace) or item. An upsert
    (INSERT ... ON CONFLICT DO UPDATE), so two first decisions racing on one item both succeed.
    Flushes, never commits."""
    from sqlalchemy.dialects import postgresql, sqlite

    from tret.db.models import ExtractionReview, utcnow

    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {', '.join(DECISIONS)}")
    exts, mods, tgt, eff, carries = await _load(db, workspace_id, [extraction_id])
    if not exts:
        raise ReviewNotFound("Extraction not found")
    e = exts[0]
    min_conf, min_dens = thresholds()
    if item_key not in {i.item_key for i in _all_items(e, mods, tgt, eff, carries, min_conf, min_dens)}:
        raise ReviewNotFound(f"No review item {item_key!r} on this extraction")
    now = utcnow()
    values = {
        "id": uuid.uuid4(), "workspace_id": workspace_id, "extraction_id": e.id, "item_key": item_key,
        "decision": decision, "note": (note or "").strip() or None, "reviewed_by": reviewed_by,
        "reviewed_at": now, "created_at": now,
    }
    dialect = db.get_bind().dialect.name
    insert = (postgresql if dialect == "postgresql" else sqlite).insert
    stmt = insert(ExtractionReview).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["extraction_id", "item_key"],
        set_={k: values[k] for k in ("decision", "note", "reviewed_by", "reviewed_at")},
    )
    await db.execute(stmt)
    await db.flush()
    return _decision_out(SimpleNamespace(**values))
