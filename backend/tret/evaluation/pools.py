"""Peer pool snapshots for the Engine 1 tournament (disclosure evaluation engine, PR 6).

A module is only ever judged against peers: report modules of the same kind from companies in the
same industry. `build_snapshot` takes the eligible report modules of one module, classifies each
company on the NACE ladder (`services/peer_taxonomy.py`, manual > sec_sic > extractor) and cuts
pools:

* start at division; a division with fewer than `min_n` members goes up to its cluster; a cluster
  still under `min_n` (counting only the members that did not already get a division pool) goes up
  to its section. A section under `min_n` stays as it is, flagged `small`, and is still rated if it
  has at least two members.
* a pool is split by disclosure regime (`csrd`, `uk`, `us`, `other`) only when every part still has
  `min_n` members.
* members with no classification are left out and counted in the snapshot's `corpus_filter`.
* one member per company key and fiscal year (the best, newest extraction) and none with identical text;
  the drops are recorded in `corpus_filter["dropped"]`.
* each member copies the module's text key, hash, word count, claim density and the masking inputs, so the
  snapshot does not depend on the report rows staying as they are.

The cut is a pure function of the members (`cut_pools`), so the same input gives the same snapshot.
The snapshot is written once and never changed: a tournament's ratings belong to it.
"""
from __future__ import annotations

import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tret.db.models import (
    Batch,
    BatchTarget,
    PeerPoolMember,
    PeerPoolSnapshot,
    ReportExtraction,
    ReportModule,
)
from tret.services import peer_taxonomy as pt
from tret.taxonomy import TAXONOMY_VERSION

DEFAULT_MIN_N = 15
RULE = "division>cluster>section; regime split if every part >= min_n"
LEVELS = ("division", "cluster", "section")
USABLE = ("extracted", "partial")
_RANK_STATUS = {"extracted": 2, "partial": 1}
CSRD_FRAMEWORKS = frozenset({"CSRD_ESRS", "ESRS", "CSRD"})
UK = frozenset({"uk", "gb", "gbr", "united kingdom", "great britain", "england", "scotland", "wales"})
US = frozenset({"us", "usa", "united states", "united states of america", "u.s.", "u.s.a."})


class PoolError(Exception):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


@dataclass
class Candidate:
    """One eligible report module before it is placed in a pool."""

    module_id: uuid.UUID
    extraction_id: uuid.UUID
    report_id: uuid.UUID
    company: str
    division: str | None
    cluster: str | None
    section: str | None
    regime: str = "other"
    text_key: str | None = None
    text_sha256: str | None = None
    word_count: int = 0
    claim_density: float | None = None
    names: list[str] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)
    project_id: uuid.UUID | None = None


@dataclass
class Pool:
    key: str
    level: str
    n: int
    regimes: dict[str, int] = field(default_factory=dict)
    small: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "key": self.key, "level": self.level, "n": self.n, "regimes": dict(sorted(self.regimes.items())),
            "small": self.small, "rated": self.n >= 2,
        }


def regime_of(frameworks: list[str] | None, country: str | None, market: str | None = None) -> str:
    if any(str(f).strip().upper() in CSRD_FRAMEWORKS for f in (frameworks or [])):
        return "csrd"
    for c in (country, market):
        v = (c or "").strip().lower()
        if v in UK:
            return "uk"
        if v in US:
            return "us"
    return "other"


def cut_pools(cands: list[Candidate], min_n: int = DEFAULT_MIN_N) -> dict[uuid.UUID, tuple[str, str]]:
    """module_id -> (pool_key, level) for classified candidates. Deterministic."""
    placed: dict[uuid.UUID, tuple[str, str]] = {}
    left = [c for c in cands if c.division and c.cluster and c.section]
    for level, attr in (("division", "division"), ("cluster", "cluster")):
        groups: dict[str, list[Candidate]] = defaultdict(list)
        for c in left:
            groups[getattr(c, attr)].append(c)
        rest: list[Candidate] = []
        for name in sorted(groups):
            members = groups[name]
            if len(members) >= min_n:
                for c in members:
                    placed[c.module_id] = (f"{level}:{name}", level)
            else:
                rest.extend(members)
        left = rest
    for c in left:
        placed[c.module_id] = (f"section:{c.section}", "section")
    # regime split: only when every part has min_n
    by_pool: dict[str, list[Candidate]] = defaultdict(list)
    for c in cands:
        if c.module_id in placed:
            by_pool[placed[c.module_id][0]].append(c)
    for key, members in by_pool.items():
        parts = Counter(c.regime for c in members)
        if len(parts) > 1 and all(n >= min_n for n in parts.values()):
            level = placed[members[0].module_id][1]
            for c in members:
                placed[c.module_id] = (f"{key}|{c.regime}", level)
    return placed


def summarise(members: list[PeerPoolMember], min_n: int) -> list[Pool]:
    pools: dict[str, Pool] = {}
    for m in members:
        p = pools.setdefault(m.pool_key, Pool(m.pool_key, m.pool_level, 0))
        p.n += 1
        p.regimes[m.regime] = p.regimes.get(m.regime, 0) + 1
    for p in pools.values():
        p.small = p.n < min_n
    return sorted(pools.values(), key=lambda p: p.key)


def _hosts(urls: Any) -> list[str]:
    out: set[str] = set()
    for u in urls or []:
        raw = u.get("url") if isinstance(u, dict) else u
        host = (urlsplit(str(raw or "").strip()).hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        if host:
            out.add(host)
    return sorted(out)


def identity_names(t: BatchTarget | None, classification_name: str | None = None) -> list[str]:
    """Strings that identify the company in its own text: name, label, ticker, the register's aliases
    or short name if it has them, and the classification's company name. Never the slug: it is a
    lowercased, suffix-stripped handle ("target" for Target Corporation) that would mask ordinary words."""
    if t is None:
        return [classification_name] if classification_name else []
    ids = t.identifiers or {}
    raw: list[Any] = [t.company, t.label, ids.get("ticker"), ids.get("short_name"), classification_name]
    aliases = ids.get("aliases")
    raw += [aliases] if isinstance(aliases, str) else list(aliases or [])
    return sorted({" ".join(str(n).split()) for n in raw if n and str(n).strip()})


def _company_key(t: BatchTarget | None, report_id: uuid.UUID) -> str:
    if t is None:
        return f"report:{report_id}"
    keys = pt.target_keys(t)
    return f"{keys[0][0]}:{keys[0][1]}" if keys else f"target:{t.id}"


def _dedupe(chosen: list, cap: int = 200) -> tuple[list, dict]:
    """One module per company key and fiscal year (the best, newest extraction), then none with
    identical text. Returns the survivors and what was dropped, with the reason."""
    ordered = sorted(
        chosen,
        key=lambda x: (-_RANK_STATUS.get(x[1].status, 0), -(x[1].created_at.timestamp() if x[1].created_at else 0.0), str(x[0].id)),
    )
    seen: dict[tuple[str, Any], uuid.UUID] = {}
    kept, dropped_cy, dropped_text = [], [], []
    for rm, ex, t in ordered:
        key = (_company_key(t, ex.disclosure_report_id), (ex.metadata_json or {}).get("report_year"))
        if key in seen:
            dropped_cy.append({"module_id": str(rm.id), "kept": str(seen[key]), "company": key[0], "year": key[1]})
            continue
        seen[key] = rm.id
        kept.append((rm, ex, t))
    by_sha: dict[str, uuid.UUID] = {}
    final = []
    for rm, ex, t in kept:
        sha = rm.text_sha256
        if sha and sha in by_sha:
            dropped_text.append({"module_id": str(rm.id), "kept": str(by_sha[sha]), "sha256": sha})
            continue
        if sha:
            by_sha[sha] = rm.id
        final.append((rm, ex, t))
    info = {
        "duplicate_company_year": len(dropped_cy), "identical_text": len(dropped_text),
        "duplicate_company_year_modules": dropped_cy[:cap], "identical_text_modules": dropped_text[:cap],
    }
    return final, info


async def _candidates(
    db: AsyncSession, *, workspace_id: uuid.UUID, module_set: str, module: str, batch_ids: list[uuid.UUID] | None
) -> tuple[list[Candidate], int, dict]:
    """(candidates, count with no classification, dropped duplicates). Latest usable extraction
    per report of the module set (a newer failed one never displaces an older good one), then one
    per company and fiscal year."""
    rows = (
        await db.execute(
            select(ReportModule, ReportExtraction)
            .join(ReportExtraction, ReportModule.extraction_id == ReportExtraction.id)
            .where(
                ReportExtraction.workspace_id == workspace_id,
                ReportExtraction.module_set == module_set,
                ReportExtraction.status.in_(USABLE),
                ReportModule.module == module,
            )
            .order_by(ReportExtraction.created_at, ReportExtraction.id)
        )
    ).all()
    latest: dict[uuid.UUID, tuple[ReportModule, ReportExtraction]] = {}
    for rm, ex in rows:
        latest[ex.disclosure_report_id] = (rm, ex)  # ascending: the newest usable one wins
    target_ids = {ex.batch_target_id for _, ex in latest.values() if ex.batch_target_id}
    targets: dict[uuid.UUID, BatchTarget] = {}
    if target_ids:
        ids = sorted(target_ids)
        for i in range(0, len(ids), 400):
            for t in (
                await db.execute(
                    select(BatchTarget)
                    .join(Batch, Batch.id == BatchTarget.collection_id)
                    .where(BatchTarget.id.in_(ids[i:i + 400]), Batch.workspace_id == workspace_id)
                )
            ).scalars():
                targets[t.id] = t
    allowed = set(batch_ids) if batch_ids else None
    chosen = []
    for rm, ex in latest.values():
        if not (rm.present and rm.text_key):
            continue
        t = targets.get(ex.batch_target_id) if ex.batch_target_id else None
        if allowed is not None and (t is None or t.collection_id not in allowed):
            continue
        chosen.append((rm, ex, t))
    chosen, dropped = _dedupe(chosen)
    by_key = await pt.rows_for_keys(db, workspace_id, (p for _, _, t in chosen if t for p in pt.target_keys(t)))
    cands: list[Candidate] = []
    unclassified = 0
    for rm, ex, t in sorted(chosen, key=lambda x: str(x[0].id)):
        cls = pt.resolve_for_keys(by_key, pt.target_keys(t))[0] if t else None
        if cls is None:
            unclassified += 1
            continue
        fw = (ex.metadata_json or {}).get("frameworks_cited") or []
        cands.append(
            Candidate(
                module_id=rm.id, extraction_id=ex.id, report_id=ex.disclosure_report_id,
                company=(t.company if t else "") or "", division=cls.nace_division, cluster=cls.cluster,
                section=cls.nace_section, regime=regime_of(fw, t.domicile_country if t else None, t.market if t else None),
                text_key=rm.text_key, text_sha256=rm.text_sha256, word_count=int(rm.word_count or 0),
                claim_density=rm.claim_density, names=identity_names(t, cls.company_name),
                domains=_hosts(t.known_urls if t else None), project_id=ex.project_id,
            )
        )
    return cands, unclassified, dropped


async def build_snapshot(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    module_set: str,
    module: str,
    batch_ids: list[uuid.UUID] | None = None,
    min_n: int = DEFAULT_MIN_N,
    created_by: str | None = None,
) -> tuple[PeerPoolSnapshot, list[Pool]]:
    """Write a snapshot (flushed, not committed) and return it with its pools."""
    if min_n < 2:
        raise PoolError("min_n must be at least 2")
    if batch_ids:
        found = set(
            (await db.execute(select(Batch.id).where(Batch.workspace_id == workspace_id, Batch.id.in_(list(batch_ids))))).scalars()
        )
        missing = [str(b) for b in batch_ids if b not in found]
        if missing:
            raise PoolError(f"batch not found: {', '.join(missing)}", 404)
    cands, unclassified, dropped = await _candidates(
        db, workspace_id=workspace_id, module_set=module_set, module=module, batch_ids=batch_ids
    )
    placed = cut_pools(cands, min_n)
    snap = PeerPoolSnapshot(
        workspace_id=workspace_id, taxonomy_version=TAXONOMY_VERSION, min_n=min_n, rule=RULE,
        corpus_filter={
            "batch_ids": sorted(str(b) for b in (batch_ids or [])), "module_set": module_set, "module": module,
            "eligible": len(cands) + unclassified, "excluded_unclassified": unclassified, "dropped": dropped,
        },
        project_id=next((c.project_id for c in cands if c.project_id), None),
        created_by=created_by,
    )
    db.add(snap)
    await db.flush()
    members = []
    for c in sorted(cands, key=lambda c: (placed[c.module_id][0], str(c.module_id))):
        key, level = placed[c.module_id]
        m = PeerPoolMember(
            snapshot_id=snap.id, report_extraction_id=c.extraction_id, report_module_id=c.module_id,
            disclosure_report_id=c.report_id, company=c.company, pool_key=key, pool_level=level,
            regime=c.regime, nace_division=c.division, text_key=c.text_key, text_sha256=c.text_sha256,
            word_count=c.word_count, claim_density=c.claim_density, mask_names=c.names, mask_domains=c.domains,
        )
        members.append(m)
    db.add_all(members)
    await db.flush()
    return snap, summarise(members, min_n)


async def members_of(db: AsyncSession, snapshot_id: uuid.UUID) -> list[PeerPoolMember]:
    return list(
        (
            await db.execute(
                select(PeerPoolMember)
                .where(PeerPoolMember.snapshot_id == snapshot_id)
                .order_by(PeerPoolMember.pool_key, PeerPoolMember.report_module_id)
            )
        ).scalars()
    )
