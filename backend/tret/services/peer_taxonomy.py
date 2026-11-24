"""Peer taxonomy service: company classifications on the NACE Rev. 2 ladder
(`tret/taxonomy`, docs/disclosure-collection.md "Peer taxonomy").

* `classify_sec_batch` reads each batch target's SEC SIC code (the `submissions` JSON of
  its CIK), maps it through the curated SIC to NACE concordance and upserts a `sec_sic`
  row. The fetcher is injected: the operator CLI builds a rate-limited `SecFetcher`;
  tests pass a fake.
* `effective_for_targets` returns each target's effective classification (manual >
  sec_sic > extractor), including targets that have none.
* `set_manual` upserts a workspace admin's override.

Classifications are workspace-scoped like the register.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Protocol

from sqlalchemy import select, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tret import taxonomy
from tret.config import Settings
from tret.db.models import Batch, BatchTarget, CompanyClassification
from tret.net.client import build_client
from tret.net.policy import CLASS_RESEARCH, EgressDenied, sec_host_permitted, sec_policy

log = logging.getLogger(__name__)

SEC_BASE_URL = "https://data.sec.gov"
SEC_MIN_INTERVAL = 0.125  # 8 requests/s; the SEC's published limit is 10/s
SEC_MAX_BYTES = 8 * 1024 * 1024
KEY_TYPES = ("lei", "cik", "ticker", "target")
SOURCES = ("sec_sic", "extractor", "manual")


class SecFetchError(Exception):
    """A submissions request that did not yield JSON. `status` is the HTTP status when
    there was one; `fatal` means every further request would fail the same way (the SEC
    refuses this client), so the run should stop."""

    def __init__(self, message: str, *, status: int | None = None, fatal: bool = False):
        super().__init__(message)
        self.status = status
        self.fatal = fatal


class SubmissionsFetcher(Protocol):
    async def fetch_submissions(self, cik10: str) -> dict: ...


def sec_user_agent(settings: Settings) -> str | None:
    """The configured SEC User-Agent, or None when it is empty."""
    ua = (settings.sec_user_agent or "").strip()
    return ua or None


def peer_taxonomy_enabled(settings: Settings) -> bool:
    return settings.peer_taxonomy == "on"


def submissions_url(cik10: str) -> str:
    return f"{SEC_BASE_URL}/submissions/CIK{cik10}.json"


# ── keys ─────────────────────────────────────────────────────────────────────
def normalise_cik(value: Any) -> str | None:
    """A CIK as the SEC's 10-digit zero-padded string, or None when it is not a CIK."""
    if value is None or isinstance(value, bool):
        return None
    s = str(value).strip()
    if s.upper().startswith("CIK"):
        s = s[3:].strip()
    if not s.isascii() or not s.isdigit() or len(s) > 10 or int(s) == 0:
        return None
    return s.zfill(10)


def normalise_key(key_type: str, key: Any) -> str | None:
    """The stored form of a company key, or None when it is empty or malformed."""
    if key is None:
        return None
    s = str(key).strip()
    if not s:
        return None
    if key_type == "cik":
        return normalise_cik(s)
    if key_type in ("lei", "ticker"):
        return s.upper()
    if key_type == "target":
        return s
    return None


# ── the SEC fetcher ──────────────────────────────────────────────────────────
class SecFetcher:
    """Sequential, rate-limited GETs of `data.sec.gov/submissions/CIK##########.json`.

    The client comes from `open_sec_client` (tret/net: egress switch, proxy, URL guard and
    audit log all apply). A connection that cannot be made is fatal at once: every further
    CIK would fail the same way, and retrying each would cost a minute apiece.
    """

    def __init__(
        self,
        client: Any,
        *,
        min_interval: float = SEC_MIN_INTERVAL,
        max_retries: int = 5,
        max_backoff: float = 120.0,
        max_bytes: int = SEC_MAX_BYTES,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        import httpx

        self._httpx = httpx
        self._client = client
        self.min_interval = max(0.0, float(min_interval))
        self.max_retries = max_retries
        self.max_backoff = max_backoff
        self.max_bytes = max_bytes
        self._sleep = sleep
        self._clock = clock
        self._last: float | None = None
        self.requests = 0
        self.rate_limited = 0

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _wait_turn(self) -> None:
        if self._last is not None:
            gap = self.min_interval - (self._clock() - self._last)
            if gap > 0:
                await self._sleep(gap)
        self._last = self._clock()

    @staticmethod
    def _retry_after(value: str | None) -> float | None:
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            pass
        try:
            return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
        except (TypeError, ValueError):
            return None

    async def fetch_submissions(self, cik10: str) -> dict:
        url = submissions_url(cik10)
        httpx = self._httpx
        for attempt in range(self.max_retries + 1):
            await self._wait_turn()
            self.requests += 1
            wait: float | None = None
            try:
                async with self._client.stream("GET", url) as resp:
                    status = resp.status_code
                    if status == 200:
                        declared = resp.headers.get("content-length")
                        if declared and declared.isdigit() and int(declared) > self.max_bytes:
                            raise SecFetchError("response too large", status=status)
                        buf = bytearray()
                        async for chunk in resp.aiter_bytes():
                            buf += chunk
                            if len(buf) > self.max_bytes:
                                raise SecFetchError("response too large", status=status)
                        try:
                            doc = json.loads(buf)
                        except (ValueError, RecursionError) as e:
                            raise SecFetchError(f"not valid JSON: {e}", status=status) from e
                        if not isinstance(doc, dict):
                            raise SecFetchError("unexpected JSON shape", status=status)
                        return doc
                    if status == 429 or status >= 500:
                        if status == 429:
                            self.rate_limited += 1
                        wait = self._retry_after(resp.headers.get("retry-after"))
                        problem = f"HTTP {status}"
                    elif status == 403:
                        raise SecFetchError(
                            "HTTP 403: the SEC refused the request (check TRET_SEC_USER_AGENT "
                            "and the request rate)", status=403, fatal=True,
                        )
                    else:
                        raise SecFetchError(f"HTTP {status}", status=status)
            except EgressDenied as e:
                raise SecFetchError(f"egress refused: {e}", fatal=True) from e
            except httpx.ConnectError as e:
                raise SecFetchError(f"cannot connect to data.sec.gov ({e})", fatal=True) from e
            except httpx.HTTPError as e:
                problem = type(e).__name__
            if attempt >= self.max_retries:
                raise SecFetchError(f"{problem} after {attempt + 1} attempts")
            backoff = min(self.max_backoff, 2.0 ** (attempt + 1))
            delay = min(self.max_backoff, wait) if wait is not None else backoff
            log.warning("%s for %s; retrying in %.1fs", problem, url, delay)
            await self._sleep(delay)
        raise SecFetchError("unreachable")  # pragma: no cover


def open_sec_client(settings: Settings, user_agent: str) -> Any:
    """An httpx.AsyncClient for data.sec.gov built through tret/net (the egress switch,
    TRET_EGRESS_PROXY, the URL guard and the audit log apply). The caller closes it.
    Raises `EgressDenied` when egress is off, or when the operator's research allowlist
    excludes data.sec.gov."""
    policy = sec_policy(settings)
    if not sec_host_permitted(settings):
        raise EgressDenied(
            "host_not_allowed", SEC_BASE_URL, CLASS_RESEARCH,
            "TRET_EGRESS_RESEARCH_ALLOW_HOSTS does not include data.sec.gov",
        )
    return build_client(
        CLASS_RESEARCH,
        timeout=policy.timeout_seconds,
        headers={"User-Agent": user_agent, "Accept": "application/json"},
        follow_redirects=False,
        policy=policy,
    )


# ── building rows ────────────────────────────────────────────────────────────
def classification_fields(code: str, *, gics_sector: str | None = None) -> dict[str, Any]:
    """The ladder columns for a NACE code (raises ValueError for an unknown code), plus the
    GICS cross-check."""
    lad = taxonomy.ladder(code)
    return {
        "nace_group": lad["group"],
        "nace_division": lad["division"],
        "cluster": lad["cluster"],
        "nace_section": lad["section"],
        "taxonomy_version": taxonomy.TAXONOMY_VERSION,
        "gics_sector": (gics_sector or "").strip() or None,
        "gics_disagrees": taxonomy.gics_disagrees(gics_sector, lad["cluster"]),
    }


async def _get_row(
    db: AsyncSession, workspace_id: uuid.UUID, key_type: str, key: str, source: str
) -> CompanyClassification | None:
    return (
        await db.execute(
            select(CompanyClassification).where(
                CompanyClassification.workspace_id == workspace_id,
                CompanyClassification.company_key_type == key_type,
                CompanyClassification.company_key == key,
                CompanyClassification.source == source,
            )
        )
    ).scalar_one_or_none()


# ── SEC SIC import ───────────────────────────────────────────────────────────
COMMIT_EVERY = 25  # CIKs per write transaction


@dataclass
class ClassifyResult:
    classified: int = 0
    skipped_existing: int = 0
    skipped_no_cik: int = 0
    skipped_duplicate: int = 0
    unmapped_sic: int = 0
    unclassifiable_sic: int = 0
    no_sic: int = 0
    errors: int = 0
    dry_run: bool = False
    aborted: str | None = None
    items: list[dict[str, Any]] = field(default_factory=list)  # one per processed target

    def counts(self) -> dict[str, int]:
        return {
            "classified": self.classified, "unmapped_sic": self.unmapped_sic,
            "unclassifiable_sic": self.unclassifiable_sic, "no_sic": self.no_sic,
            "errors": self.errors, "skipped_no_cik": self.skipped_no_cik,
            "skipped_existing": self.skipped_existing, "skipped_duplicate": self.skipped_duplicate,
        }


async def classify_sec_batch(
    factory: async_sessionmaker[AsyncSession],
    batch_id: uuid.UUID,
    fetcher: SubmissionsFetcher,
    *,
    limit: int | None = None,
    force: bool = False,
    dry_run: bool = False,
    progress: Callable[[dict[str, Any]], None] | None = None,
    commit_every: int = COMMIT_EVERY,
) -> ClassifyResult | None:
    """Classify a batch's targets from the SEC. Returns None when the batch does not exist.

    One request per distinct CIK. A target without a (valid) CIK is skipped; one whose
    `sec_sic` row exists is skipped unless `force`. A SIC code with no industry (blank
    checks, ...) is counted as `unclassifiable_sic` and writes nothing. Nothing is written
    on `dry_run`. No database transaction is open while the SEC is being asked (or while a
    retry sleeps): rows are written in short transactions every `commit_every` CIKs and at
    the end, including when the run is interrupted, so finished rows survive.
    """
    result = ClassifyResult(dry_run=dry_run)
    async with factory() as db:
        batch = await db.get(Batch, batch_id)
        if batch is None:
            return None
        q = select(BatchTarget).where(BatchTarget.collection_id == batch.id).order_by(BatchTarget.position)
        if limit is not None:
            q = q.limit(limit)
        targets = list((await db.execute(q)).scalars())
        workspace_id = batch.workspace_id
        ciks = sorted({c for c in (normalise_cik((t.identifiers or {}).get("cik")) for t in targets) if c})
        existing: set[str] = set()
        for i in range(0, len(ciks), 400):
            existing |= set((await db.execute(
                select(CompanyClassification.company_key).where(
                    CompanyClassification.workspace_id == workspace_id,
                    CompanyClassification.company_key_type == "cik",
                    CompanyClassification.source == "sec_sic",
                    CompanyClassification.company_key.in_(ciks[i:i + 400]),
                )
            )).scalars())
    # the session is closed: nothing below holds a transaction while it waits on the SEC

    pending: list[tuple[str, dict[str, Any]]] = []

    async def flush() -> None:
        if not pending or dry_run:
            pending.clear()
            return
        batch_rows = list(pending)
        pending.clear()
        async with factory() as db:
            for cik, values in batch_rows:
                row = await _get_row(db, workspace_id, "cik", cik, "sec_sic")
                if row is None:
                    db.add(CompanyClassification(
                        workspace_id=workspace_id, company_key_type="cik", company_key=cik, **values
                    ))
                else:
                    for k, v in values.items():
                        setattr(row, k, v)
            await db.commit()

    seen: set[str] = set()
    try:
        for t in targets:
            cik = normalise_cik((t.identifiers or {}).get("cik"))
            item: dict[str, Any] = {"slug": t.slug, "company": t.company, "cik": cik}
            stop = False
            if cik is None:
                result.skipped_no_cik += 1
                item["outcome"] = "skipped_no_cik"
            elif cik in seen:
                result.skipped_duplicate += 1
                item["outcome"] = "skipped_duplicate"
            else:
                seen.add(cik)
                if cik in existing and not force:
                    result.skipped_existing += 1
                    item["outcome"] = "skipped_existing"
                else:
                    stop = await _classify_one(t, cik, fetcher, result, item, pending)
                    if len(pending) >= commit_every:
                        await flush()
            result.items.append(item)
            if progress:
                progress(item)
            if stop:
                result.aborted = item.get("error")
                break
    finally:
        await flush()
    return result


async def _classify_one(
    target: BatchTarget, cik: str, fetcher: SubmissionsFetcher, result: ClassifyResult,
    item: dict[str, Any], pending: list[tuple[str, dict[str, Any]]],
) -> bool:
    """Fetch and map one CIK, queueing its row. Returns True when the whole run should stop."""
    try:
        doc = await fetcher.fetch_submissions(cik)
    except SecFetchError as e:
        result.errors += 1
        item.update(outcome="error", error=f"CIK {cik}: {e}")
        return e.fatal
    sic_raw = doc.get("sic")
    sic = str(sic_raw).strip() if sic_raw is not None else ""
    if not sic or sic == "0":
        result.no_sic += 1
        item["outcome"] = "no_sic"
        return False
    mapping = taxonomy.classify_sic(sic)
    item.update(sic=sic, sic_description=doc.get("sicDescription"))
    if mapping is None:
        result.unmapped_sic += 1
        item["outcome"] = "unmapped_sic"
        return False
    if not mapping.classifiable:
        result.unclassifiable_sic += 1
        item["outcome"] = "unclassifiable_sic"
        return False
    fields = classification_fields(mapping.nace_group or mapping.nace_division, gics_sector=target.sector)
    name = (doc.get("name") or "").strip() or target.company
    pending.append((cik, {
        **fields,
        "company_name": name,
        "source": "sec_sic",
        "source_code": mapping.sic,
        "source_label": (doc.get("sicDescription") or mapping.sic_title or None),
        "match": mapping.match,
        "evidence": {"url": submissions_url(cik)},
    }))
    item.update(
        outcome="classified", nace_division=fields["nace_division"], nace_group=fields["nace_group"],
        cluster=fields["cluster"], match=mapping.match, gics_disagrees=fields["gics_disagrees"],
    )
    result.classified += 1
    return False


# ── reading ──────────────────────────────────────────────────────────────────
def target_keys(target: BatchTarget) -> list[tuple[str, str]]:
    """Every (key_type, key) a target can be classified under, most specific first."""
    ids = target.identifiers or {}
    keys: list[tuple[str, str]] = []
    for kt in ("cik", "lei", "ticker"):
        k = normalise_key(kt, ids.get(kt))
        if k:
            keys.append((kt, k))
    keys.append(("target", target.slug))
    return keys


def row_out(r: CompanyClassification) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "company_key_type": r.company_key_type,
        "company_key": r.company_key,
        "company_name": r.company_name,
        "nace_group": r.nace_group,
        "nace_division": r.nace_division,
        "cluster": r.cluster,
        "nace_section": r.nace_section,
        "taxonomy_version": r.taxonomy_version,
        "source": r.source,
        "source_code": r.source_code,
        "source_label": r.source_label,
        "confidence": r.confidence,
        "match": r.match,
        "evidence": r.evidence,
        "gics_sector": r.gics_sector,
        "gics_disagrees": r.gics_disagrees,
        "reviewed_by": r.reviewed_by,
        "reviewed_at": r.reviewed_at.isoformat() if r.reviewed_at else None,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }


async def rows_for_keys(
    db: AsyncSession, workspace_id: uuid.UUID, pairs: Iterable[tuple[str, str]]
) -> dict[tuple[str, str], list[CompanyClassification]]:
    """Every classification row of the workspace under any of the (key_type, key) pairs,
    grouped by pair."""
    pair_list = sorted(set(pairs))
    by_key: dict[tuple[str, str], list[CompanyClassification]] = {}
    for i in range(0, len(pair_list), 400):  # stay under SQLite's bound-parameter limit
        chunk = pair_list[i:i + 400]
        for r in (await db.execute(
            select(CompanyClassification).where(
                CompanyClassification.workspace_id == workspace_id,
                tuple_(CompanyClassification.company_key_type, CompanyClassification.company_key).in_(chunk),
            )
        )).scalars():
            by_key.setdefault((r.company_key_type, r.company_key), []).append(r)
    return by_key


def resolve_for_keys(
    by_key: dict[tuple[str, str], list[CompanyClassification]], keys: Iterable[tuple[str, str]]
) -> tuple[CompanyClassification | None, list[CompanyClassification]]:
    """The one resolver the CLI and the API share: across the rows of every key a company is
    known by, the effective one (manual > sec_sic > extractor) and all rows, most trusted
    first. Within one source the first key listed wins."""
    rows = [r for k in keys for r in by_key.get(k, [])]
    rank = {s: i for i, s in enumerate(taxonomy.SOURCE_PRECEDENCE)}
    ordered = sorted(rows, key=lambda r: rank.get(r.source, len(rank)))  # stable: key order kept
    return taxonomy.resolve(ordered), ordered


async def effective_for_keys(
    db: AsyncSession, workspace_id: uuid.UUID, keys: list[tuple[str, str]]
) -> tuple[CompanyClassification | None, list[CompanyClassification]]:
    return resolve_for_keys(await rows_for_keys(db, workspace_id, keys), keys)


async def effective_for_targets(
    db: AsyncSession, batch: Batch
) -> list[tuple[BatchTarget, CompanyClassification | None]]:
    """Each of the batch's targets with its effective classification (None when it has
    none), in register order."""
    targets = list(
        (await db.execute(
            select(BatchTarget).where(BatchTarget.collection_id == batch.id).order_by(BatchTarget.position)
        )).scalars()
    )
    by_key = await rows_for_keys(db, batch.workspace_id, (p for t in targets for p in target_keys(t)))
    return [(t, resolve_for_keys(by_key, target_keys(t))[0]) for t in targets]


# ── manual override ──────────────────────────────────────────────────────────
async def _known_gics_sector(
    db: AsyncSession, workspace_id: uuid.UUID, key_type: str, key: str
) -> str | None:
    """A GICS sector name already on record for this company: from another classification
    row under the same key, else from a register target of the workspace that carries it."""
    cc = CompanyClassification
    hit = (await db.execute(
        select(cc.gics_sector).where(
            cc.workspace_id == workspace_id, cc.company_key_type == key_type, cc.company_key == key,
            cc.gics_sector.isnot(None),
        ).limit(1)
    )).scalar_one_or_none()
    if hit:
        return hit
    q = (
        select(BatchTarget.slug, BatchTarget.identifiers, BatchTarget.sector)
        .join(Batch, Batch.id == BatchTarget.collection_id)
        .where(Batch.workspace_id == workspace_id, BatchTarget.sector.isnot(None))
    )
    for slug, ids, sector in (await db.execute(q)).all():
        if not (sector or "").strip():
            continue
        mine = slug if key_type == "target" else normalise_key(key_type, (ids or {}).get(key_type))
        if mine == key:
            return sector.strip()
    return None


async def set_manual(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    key_type: str,
    key: str,
    company_name: str | None,
    nace_code: str,
    note: str | None,
    reviewed_by: str,
    gics_sector: str | None = None,
) -> CompanyClassification:
    """Upsert the `manual` row of a company. Raises ValueError for a bad key or NACE code.

    The GICS sector is carried over from what is already on record for the company (another
    row under the same key, or a register target with it) when not given, so the cross-check
    survives an override. A concurrent insert of the same row is retried as an update.
    """
    if key_type not in KEY_TYPES:
        raise ValueError(f"company_key_type must be one of {', '.join(KEY_TYPES)}")
    norm = normalise_key(key_type, key)
    if norm is None:
        raise ValueError(f"not a valid {key_type} key: {key!r}")
    taxonomy.ladder(nace_code)  # validate before touching the database
    sector = (gics_sector or "").strip() or await _known_gics_sector(db, workspace_id, key_type, norm)
    fields = classification_fields(nace_code, gics_sector=sector)
    values = {
        **fields,
        "company_name": (company_name or "").strip() or None,
        "source_code": str(nace_code).strip(),
        "source_label": (note or "").strip() or None,
        "reviewed_by": reviewed_by,
        "reviewed_at": datetime.now(timezone.utc),
    }
    row = await _get_row(db, workspace_id, key_type, norm, "manual")
    if row is None:
        try:
            async with db.begin_nested():
                row = CompanyClassification(
                    workspace_id=workspace_id, company_key_type=key_type, company_key=norm,
                    source="manual", match=None, **values,
                )
                db.add(row)
                await db.flush()
            return row
        except IntegrityError:
            # another request inserted the same (company, manual) row first: update theirs
            row = await _get_row(db, workspace_id, key_type, norm, "manual")
            if row is None:
                raise
    for k, v in values.items():
        setattr(row, k, v)
    await db.flush()
    return row
