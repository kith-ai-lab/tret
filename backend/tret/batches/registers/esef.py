"""Build a disclosure register from the ESEF filings index (filings.xbrl.org).

The index is one JSON:API document per filing year (`data` = filings, `included` =
entities). This module cleans it to one filing per LEI, optionally ranks the
companies by size (revenue from each filing's xBRL-JSON, fetched politely and
cached), and writes a register CSV plus a ranking sidecar.

An ESEF report is inline XBRL, not a PDF, so the register supplies the target list
and the filing of record only; the Collector still has to find the disclosure PDF.

Network use is confined to https://filings.xbrl.org: sequential requests with a
minimum interval, back-off on 429 and 5xx (honouring Retry-After), a streamed
per-response size cap, and a disk cache of the extracted metric only. Everything
downloaded is parsed as data and never executed."""
from __future__ import annotations

import csv
import json
import logging
import math
import re
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

from tret.batches.registers.base import RegisterRow, write_register_csv

log = logging.getLogger("tret.registers.esef")

BASE_URL = "https://filings.xbrl.org"
USER_AGENT = "tret-register-importer"
LEI_RE = re.compile(r"^[A-Z0-9]{20}$")
SKIP_COUNTRIES = frozenset({"UA"})
MAX_INDEX_BYTES = 400 * 1024 * 1024
DEFAULT_MIN_INTERVAL = 1.5
DEFAULT_MAX_BYTES = 80 * 1024 * 1024
# This many candidates in a row ending in a transient failure stop the fetching for the
# run (a CDN answering with 403/429 is rate limiting us; hammering on makes it worse).
MAX_CONSECUTIVE_TRANSIENT = 5
OVER_SIZE_REASON = "over size cap"
MIN_ANNUAL_DAYS = 300
REVENUE_CONCEPT = "ifrs-full:Revenue"
# Tried in order: many issuers tag the income-statement top line with a more
# specific concept and have no plain Revenue fact.
REVENUE_CONCEPTS = (
    REVENUE_CONCEPT,
    "ifrs-full:RevenueFromContractsWithCustomers",
    "ifrs-full:InsuranceRevenue",
)
EXTRA_RAW_CONCEPTS = (
    "ifrs-full:RevenueFromInterest",
    "ifrs-full:InterestRevenueCalculatedUsingEffectiveInterestMethod",
    "ifrs-full:Equity",
    "ifrs-full:ProfitLoss",
)
ASSETS_CONCEPT = "ifrs-full:Assets"
# Concepts measured over a period (a full year is wanted); the others are instants.
DURATION_CONCEPTS = frozenset({*REVENUE_CONCEPTS, "ifrs-full:ProfitLoss"})
ASSETS_PROXY_FACTOR = 0.05
# Dimension keys a "plain" (undimensioned) fact may carry.
PLAIN_DIMENSIONS = frozenset({"concept", "entity", "period", "unit", "language"})

# APPROXIMATE EUR value of one unit of each currency. For ranking companies by
# size only; never use these figures as data. Update by hand when they drift.
EUR_PER_UNIT = {
    "EUR": 1.0,
    "GBP": 1.17,
    "SEK": 0.088,
    "DKK": 0.134,
    "NOK": 0.086,
    "PLN": 0.235,
    "CHF": 1.07,
    "USD": 0.92,
    "CZK": 0.040,
    "HUF": 0.0025,
    "RON": 0.20,
    "ISK": 0.0066,
}


class FetchError(Exception):
    """A request that did not yield a body. `permanent` errors are worth caching
    (the same URL will fail the same way); the rest are retried on a later run."""

    def __init__(self, message: str, *, permanent: bool = False, status: int | None = None):
        super().__init__(message)
        self.permanent = permanent
        self.status = status


class Fetcher(Protocol):
    def get_bytes(self, url: str, *, max_bytes: int) -> bytes | bytearray: ...


# ── fetching ─────────────────────────────────────────────────────────────────
def _bad_url_char(c: str) -> bool:
    """Whitespace, control, format (zero-width, bidi) and line/paragraph separator characters."""
    return c.isspace() or unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp")


def absolute_url(path: Any) -> str | None:
    """A filings.xbrl.org URL from an index path (relative, or already absolute on
    that host); None for anything else, so a hostile index cannot point us elsewhere."""
    if not isinstance(path, str):
        return None
    path = path.strip()
    if not path or len(path) > 2000 or any(_bad_url_char(c) for c in path):
        return None
    if path.startswith(BASE_URL + "/"):
        url = path
    elif path.startswith("/") and not path.startswith("//"):
        url = BASE_URL + path
    else:
        return None
    if ".." in url.split("?")[0].split("/"):
        return None
    return url


class HttpFetcher:
    """Sequential, rate-limited GETs against filings.xbrl.org."""

    def __init__(
        self,
        *,
        min_interval: float = DEFAULT_MIN_INTERVAL,
        timeout: float = 60.0,
        max_retries: int = 5,
        max_backoff: float = 120.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        import httpx

        self._httpx = httpx
        self.min_interval = max(0.0, float(min_interval))
        self.max_retries = max_retries
        self.max_backoff = max_backoff
        self._sleep = sleep
        self._clock = clock
        self._last = None  # type: float | None
        self.requests = 0
        self.rate_limited = 0  # 429 responses seen
        self._client = httpx.Client(
            timeout=timeout,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            follow_redirects=False,
        )

    def close(self) -> None:
        self._client.close()

    def _wait_turn(self) -> None:
        if self._last is not None:
            gap = self.min_interval - (self._clock() - self._last)
            if gap > 0:
                self._sleep(gap)
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
            when = parsedate_to_datetime(value)
            return max(0.0, when.timestamp() - time.time())
        except (TypeError, ValueError):
            return None

    def get_bytes(self, url: str, *, max_bytes: int = DEFAULT_MAX_BYTES) -> bytes | bytearray:
        if not url.startswith(BASE_URL + "/"):
            raise FetchError(f"refusing to fetch outside {BASE_URL}", permanent=True)
        httpx = self._httpx
        for attempt in range(self.max_retries + 1):
            self._wait_turn()
            self.requests += 1
            wait: float | None = None
            try:
                with self._client.stream("GET", url) as resp:
                    status = resp.status_code
                    if status == 200:
                        declared = resp.headers.get("content-length")
                        if declared and declared.isdigit() and int(declared) > max_bytes:
                            raise FetchError(OVER_SIZE_REASON, permanent=True)
                        buf = bytearray()
                        for chunk in resp.iter_bytes():
                            buf += chunk
                            if len(buf) > max_bytes:
                                raise FetchError(OVER_SIZE_REASON, permanent=True)
                        return buf
                    if status == 429 or status == 403 or status >= 500:
                        if status == 429:
                            self.rate_limited += 1
                        wait = self._retry_after(resp.headers.get("retry-after"))
                        problem = f"HTTP {status}"
                    else:
                        # 403 is not cached as permanent: a CDN may answer rate limiting with it.
                        raise FetchError(f"HTTP {status}", permanent=status in (404, 410, 400), status=status)
            except httpx.HTTPError as e:  # transport, timeout, decoding (corrupt gzip), stream errors
                problem = f"{type(e).__name__}"
            if attempt >= self.max_retries:
                raise FetchError(f"{problem} after {attempt + 1} attempts")
            backoff = min(self.max_backoff, 2.0 ** (attempt + 1))
            delay = min(self.max_backoff, wait) if wait is not None else backoff
            log.warning("%s for %s; retrying in %.1fs", problem, url, delay)
            self._sleep(delay)
        raise FetchError("unreachable")  # pragma: no cover


# ── the index ────────────────────────────────────────────────────────────────
def index_url(year: int) -> str:
    return f"{BASE_URL}/indexes/filings-{int(year)}.json"


def load_index(year: int, *, index_file: str | Path | None = None, fetcher: Fetcher | None = None) -> dict:
    """The yearly index document: from `index_file` when given, else downloaded."""
    if index_file is not None:
        raw = Path(index_file).read_bytes()
    else:
        fetcher = fetcher or HttpFetcher()
        raw = fetcher.get_bytes(index_url(year), max_bytes=MAX_INDEX_BYTES)
    try:
        doc = json.loads(raw)
    except (ValueError, RecursionError) as e:
        raise ValueError(f"the filings index is not valid JSON: {e}") from e
    if not isinstance(doc, dict) or not isinstance(doc.get("data"), list):
        raise ValueError("the filings index has no 'data' list")
    return doc


@dataclass
class Candidate:
    lei: str
    name: str
    country: str
    period_end: date
    error_count: int
    processed: str
    fxo_id: str
    sha256: str
    report_url: str | None
    package_url: str | None
    json_url: str | None
    errors_known: bool = True  # False when the index gave no usable error_count


@dataclass
class CleanResult:
    candidates: list[Candidate] = field(default_factory=list)
    dropped: Counter = field(default_factory=Counter)
    filings: int = 0


def clean_name(value: Any) -> str:
    """A CSV-safe company name: control characters removed, whitespace collapsed."""
    text = "".join(
        " " if unicodedata.category(c) in ("Zl", "Zp") else c
        for c in str(value or "")
        if unicodedata.category(c) not in ("Cc", "Cf", "Cs", "Co", "Cn") or c in "\t\n\r"
    )
    return " ".join(text.split())[:200]


def _error_count(value: Any) -> int | None:
    """The filing's error count, or None when missing or not an integer."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value) if isinstance(value, (int, str)) else None
    except ValueError:
        return None


def _parse_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def clean_index(
    doc: dict,
    year: int,
    *,
    countries: Iterable[str] | None = None,
    exclude_countries: Iterable[str] = (),
) -> CleanResult:
    """One candidate per LEI: the filing with the fewest errors, then the latest
    `processed`. Drops are counted by reason."""
    only = {c.strip().upper() for c in countries} if countries else None
    excluded = {c.strip().upper() for c in exclude_countries} | SKIP_COUNTRIES
    entities: dict[tuple[str, str], dict] = {}
    for ent in doc.get("included") or []:
        if isinstance(ent, dict) and ent.get("id") is not None:
            entities[(str(ent.get("type")), str(ent["id"]))] = ent.get("attributes") or {}
    result = CleanResult()
    best: dict[str, Candidate] = {}
    for filing in doc.get("data") or []:
        result.filings += 1
        if not isinstance(filing, dict):
            result.dropped["malformed filing"] += 1
            continue
        attrs = filing.get("attributes") or {}
        period_end = _parse_date(attrs.get("period_end"))
        if period_end is None or period_end.year != year:
            result.dropped["period_end outside the year"] += 1
            continue
        country = str(attrs.get("country") or "").strip().upper()
        if not re.fullmatch(r"[A-Z]{2}", country):
            result.dropped["no country code"] += 1
            continue
        if country in excluded:
            result.dropped["country excluded"] += 1
            continue
        if only is not None and country not in only:
            result.dropped["country not selected"] += 1
            continue
        ref = ((filing.get("relationships") or {}).get("entity") or {}).get("data") or {}
        ent = entities.get((str(ref.get("type")), str(ref.get("id")))) if ref else None
        if ent is None:
            result.dropped["no entity"] += 1
            continue
        lei = str(ent.get("identifier") or "").strip().upper()
        if not LEI_RE.match(lei):
            result.dropped["identifier is not a LEI"] += 1
            continue
        name = clean_name(ent.get("name"))
        if not name:
            result.dropped["no company name"] += 1
            continue
        errors = _error_count(attrs.get("error_count"))
        cand = Candidate(
            lei=lei,
            name=name,
            country=country,
            period_end=period_end,
            error_count=errors if errors is not None else 0,
            processed=str(attrs.get("processed") or ""),
            fxo_id=str(attrs.get("fxo_id") or filing.get("id") or ""),
            sha256=str(attrs.get("sha256") or ""),
            report_url=absolute_url(attrs.get("report_url")),
            package_url=absolute_url(attrs.get("package_url")),
            json_url=absolute_url(attrs.get("json_url")),
            errors_known=errors is not None,
        )
        prev = best.get(lei)
        if prev is None:
            best[lei] = cand
            continue
        result.dropped["older or worse filing of the same LEI"] += 1
        if _rank_key(cand) < _rank_key(prev):
            best[lei] = cand
    result.candidates = sorted(best.values(), key=lambda c: c.lei)
    return result


def _rank_key(c: Candidate) -> tuple:
    """Lower is better: known error counts first, fewest errors, a `processed` stamp
    present, then the latest one."""
    return (not c.errors_known, c.error_count, not c.processed, _neg(c.processed))


def _neg(text: str) -> tuple[int, ...]:
    """Sort key that makes a later timestamp string sort first."""
    return tuple(-ord(c) for c in text)


# ── size from xBRL-JSON ──────────────────────────────────────────────────────
def to_eur(value: float, currency: str) -> float | None:
    rate = EUR_PER_UNIT.get((currency or "").upper())
    return None if rate is None else value * rate


def _period_end(period: Any) -> str:
    """The end of an xBRL-JSON period ("start/end" or an instant) as YYYY-MM-DD."""
    text = str(period or "")
    return text.split("/")[-1][:10]


def _duration_days(period: Any) -> int:
    """Days in an xBRL-JSON "start/end" period; 0 for an instant or an unreadable one."""
    text = str(period or "")
    if "/" not in text:
        return 0
    start, _, end = text.partition("/")
    a, b = _parse_date(start), _parse_date(end)
    return max(0, (b - a).days) if a and b else 0


def _plain_fact_value(facts: dict, concept: str) -> tuple[float, str, str] | None:
    """(value, currency, period end) of the undimensioned `concept` fact with the
    latest period end (the longest duration among equal ends), or None. For a
    duration concept, facts shorter than MIN_ANNUAL_DAYS (a quarter) are ignored
    when a full-year one exists."""
    found: list[tuple[str, int, float, str]] = []  # (end, days, value, currency)
    for fact in facts.values():
        if not isinstance(fact, dict):
            continue
        dims = fact.get("dimensions")
        if not isinstance(dims, dict) or dims.get("concept") != concept:
            continue
        if set(dims) - PLAIN_DIMENSIONS:
            continue
        unit = str(dims.get("unit") or "")
        if not unit.startswith("iso4217:"):
            continue
        try:
            value = float(fact.get("value"))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value):
            continue
        period = dims.get("period")
        found.append((_period_end(period), _duration_days(period), value, unit.split(":", 1)[1].upper()))
    if concept in DURATION_CONCEPTS and any(f[1] >= MIN_ANNUAL_DAYS for f in found):
        found = [f for f in found if f[1] >= MIN_ANNUAL_DAYS]
    if not found:
        return None
    end, _days, value, currency = max(found, key=lambda f: (f[0], f[1]))
    return value, currency, end


def extract_metric(doc: Any) -> dict:
    """The size metric of one xBRL-JSON report: {metric, value, currency, period}.
    `metric` is "revenue", "assets_proxy" (assets x 0.05) or None."""
    facts = doc.get("facts") if isinstance(doc, dict) else None
    if not isinstance(facts, dict):
        return {"metric": None}
    # Raw values of every sizing concept are kept in the cache entry, so the
    # ranking rule can change later without fetching the reports again.
    raw: dict[str, list] = {}
    for concept in (*REVENUE_CONCEPTS, ASSETS_CONCEPT, *EXTRA_RAW_CONCEPTS):
        got = _plain_fact_value(facts, concept)
        if got is not None:
            raw[concept] = [got[0], got[1], got[2]]
    for concept in REVENUE_CONCEPTS:
        if concept in raw:
            value, currency, period = raw[concept]
            return {"metric": "revenue", "value": value, "currency": currency, "period": period, "raw": raw}
    if ASSETS_CONCEPT in raw:
        value, currency, period = raw[ASSETS_CONCEPT]
        return {
            "metric": "assets_proxy",
            "value": value * ASSETS_PROXY_FACTOR,
            "currency": currency,
            "period": period,
            "raw": raw,
        }
    return {"metric": None, "raw": raw} if raw else {"metric": None}


def _cache_key(c: Candidate) -> str | None:
    raw = c.sha256 or c.fxo_id
    key = re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("._")[:120]
    return key or None


def _read_cache(cache_dir: Path | None, c: Candidate) -> dict | None:
    key = _cache_key(c)
    if cache_dir is None or key is None:
        return None
    try:
        data = json.loads((cache_dir / f"{key}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and "metric" in data else None


def _write_cache(cache_dir: Path | None, c: Candidate, metric: dict) -> None:
    key = _cache_key(c)
    if cache_dir is None or key is None:
        return
    cache_dir.mkdir(parents=True, exist_ok=True)
    tmp = cache_dir / f"{key}.json.tmp"
    tmp.write_text(json.dumps(metric), encoding="utf-8")
    tmp.replace(cache_dir / f"{key}.json")


@dataclass
class Ranked:
    candidate: Candidate
    size_eur: float | None = None
    metric: str | None = None
    currency: str | None = None
    reason: str = ""  # why no size, when known (over size cap, not valid JSON, ...)


@dataclass
class RankStats:
    fetched: int = 0
    cached: int = 0
    unreadable: int = 0  # no json_url, unreadable or no usable fact
    not_fetched: int = 0  # left over by --limit-fetch or a transient error
    aborted: bool = False  # fetching stopped early after repeated transient failures


def rank_candidates(
    candidates: list[Candidate],
    *,
    fetcher: Fetcher | None,
    cache_dir: str | Path | None = None,
    limit_fetch: int | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    progress: Callable[[str], None] | None = None,
) -> tuple[list[Ranked], RankStats]:
    """Size every candidate (cache first, then at most `limit_fetch` fetches) and
    return them largest first, unknown sizes last."""
    cache = Path(cache_dir) if cache_dir else None
    stats = RankStats()
    ranked: list[Ranked] = []
    transient_streak = 0
    for cand in candidates:
        metric = _read_cache(cache, cand)
        if metric is not None:
            stats.cached += 1
        elif not cand.json_url:
            metric = {"metric": None}
            stats.unreadable += 1
        elif fetcher is None or stats.aborted or (limit_fetch is not None and stats.fetched >= limit_fetch):
            stats.not_fetched += 1
        else:
            stats.fetched += 1
            try:
                body = fetcher.get_bytes(cand.json_url, max_bytes=max_bytes)
                try:
                    metric = extract_metric(json.loads(body))
                finally:
                    del body
                transient_streak = 0
            except FetchError as e:
                if e.permanent:
                    transient_streak = 0
                    metric = {"metric": None, "reason": str(e)[:200]}
                    stats.unreadable += 1
                else:
                    stats.not_fetched += 1
                    log.warning("%s: %s", cand.fxo_id, e)
                    transient_streak += 1
                    if transient_streak >= MAX_CONSECUTIVE_TRANSIENT:
                        stats.aborted = True
                        log.warning(
                            "stopping fetches: %d candidates in a row failed transiently (last: %s); "
                            "the rest are left unsized and uncached, rerun later to resume",
                            transient_streak, e,
                        )
            except (ValueError, RecursionError):  # not JSON, or nested too deeply to parse
                transient_streak = 0
                metric = {"metric": None, "reason": "not valid JSON"}
                stats.unreadable += 1
            if metric is not None:
                if not metric.get("metric") and "reason" not in metric:
                    stats.unreadable += 1  # parsed, but no Revenue or Assets fact
                _write_cache(cache, cand, metric)
            if progress and stats.fetched % 25 == 0:
                progress(f"fetched {stats.fetched}")
        item = Ranked(cand)
        if metric and metric.get("reason"):
            item.reason = str(metric["reason"])[:200]
        if metric and metric.get("metric"):
            item.metric = metric["metric"]
            item.currency = metric.get("currency")
            item.size_eur = to_eur(float(metric["value"]), metric.get("currency") or "")
            if item.size_eur is None:
                item.metric = None
        ranked.append(item)
    ranked.sort(key=lambda r: (r.size_eur is None, -(r.size_eur or 0.0), r.candidate.name.lower(), r.candidate.lei))
    return ranked, stats


# ── output ───────────────────────────────────────────────────────────────────
def market_for(country: str) -> str:
    return "UK" if country == "GB" else "EU"


def to_register_row(c: Candidate, year: int) -> RegisterRow:
    return RegisterRow(
        company=c.name,
        market=market_for(c.country),
        domicile_country=c.country,
        sector="",
        category="other",
        expected_form="embedded",
        fiscal_year=c.period_end.year,
        known_urls="",
        notes=f"ESEF filing jurisdiction {c.country}; LEI {c.lei}",
        lei=c.lei,
        source_filing_url=c.report_url or c.package_url or "",
    )


SIDECAR_COLUMNS = (
    "lei", "company", "country", "period_end", "size_eur", "size_metric", "currency", "error_count", "fxo_id", "reason",
)


def sidecar_path(out: str | Path) -> Path:
    out = Path(out)
    return out.with_name(out.name + ".ranking.csv")


def write_ranking_csv(ranked: Iterable[Ranked], path: str | Path) -> int:
    n = 0
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(SIDECAR_COLUMNS)
        for r in ranked:
            c = r.candidate
            w.writerow([
                c.lei, c.name, c.country, c.period_end.isoformat(),
                "" if r.size_eur is None else f"{r.size_eur:.0f}",
                r.metric or "", r.currency or "", c.error_count if c.errors_known else "", c.fxo_id,
                r.reason,
            ])
            n += 1
    return n


@dataclass
class Summary:
    year: int
    filings: int
    candidates: int
    dropped: Counter
    stats: RankStats | None
    written: int
    register_path: Path
    sidecar: Path


def build_register(
    year: int,
    out: str | Path,
    *,
    countries: Iterable[str] | None = None,
    exclude_countries: Iterable[str] = (),
    top: int | None = None,
    rank_by: str = "none",
    index_file: str | Path | None = None,
    cache_dir: str | Path | None = None,
    fetcher: Fetcher | None = None,
    limit_fetch: int | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    progress: Callable[[str], None] | None = None,
) -> Summary:
    """Index -> clean -> (rank) -> top N -> register CSV + `<out>.ranking.csv`."""
    if rank_by not in ("revenue", "none"):
        raise ValueError("rank_by must be 'revenue' or 'none'")
    doc = load_index(year, index_file=index_file, fetcher=fetcher)
    cleaned = clean_index(doc, year, countries=countries, exclude_countries=exclude_countries)
    stats: RankStats | None = None
    if rank_by == "revenue":
        ranked, stats = rank_candidates(
            cleaned.candidates, fetcher=fetcher, cache_dir=cache_dir,
            limit_fetch=limit_fetch, max_bytes=max_bytes, progress=progress,
        )
    else:
        ranked = sorted(
            (Ranked(c) for c in cleaned.candidates), key=lambda r: (r.candidate.name.lower(), r.candidate.lei)
        )
    chosen = ranked if top is None else ranked[: max(0, top)]
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    written = write_register_csv((to_register_row(r.candidate, year) for r in chosen), out)
    side = sidecar_path(out)
    write_ranking_csv(ranked, side)
    return Summary(year, cleaned.filings, len(cleaned.candidates), cleaned.dropped, stats, written, out, side)
