"""Collection runs: one register of target companies, one engine run per target.

A collection takes a target register (one row per company), launches one run per
target on the workspace's "Disclosure Collector" harness with a templated prompt
(`build_prompt`), and tracks each target from proposal through approval,
packaging and the SharePoint upload of its package files. After the first batch
it can stop at a review gate (`first_batch_size`) until an approver releases the
rest. `finalize` writes the run-level records (manifest, execution, exception
log, register copy, SHA256SUMS, results summary) next to the packages.

Where the state lives:

- `CollectionTarget.status` is re-derived from facts, never advanced by hand:
  the run's status, its `package_map` findings (draft / approved / rejected and
  each one's `payload["package"]`) and the outcome the run reported through
  `report_collection_outcome` (`engine/collection_tools.py`). The rules are the
  pure `derive_target_status`; `_settle_target` applies them and performs the
  upload step they call for.
- `Collection.status` is likewise derived from its targets
  (`derive_collection_status`), except that `draft`, `paused` and `finalized`
  are set by the operator and stay until the operator moves them.

What moves a target: its run finishing (`_run_target`), a decision on one of its
maps (`notify_decided`, from `api/findings.py`), and packaging finishing
(`notify_package_finished`, from the packaging task). Launching is slot-limited:
at most the collection's `concurrency` targets are `running` at once, and a
process-wide semaphore (`TRET_COLLECTIONS_MAX_CONCURRENT`) caps every collection
together. Each finishing target launches the next pending one.
"""
from __future__ import annotations

import asyncio
import contextlib
import csv
import hashlib
import io
import json
import logging
import re
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tret import __version__
from tret.config import get_settings
from tret.db.engine import get_session_factory
from tret.db.models import (
    Approval,
    Collection,
    CollectionTarget,
    Document,
    Finding,
    Harness,
    Project,
    Run,
    User,
)
from tret.engine.collection_tools import COLLECTION_ID_KEY, COLLECTION_TARGET_KEY
from tret.engine.harness import get_harness_engine
from tret.packs.links import packs_for_harness, resolve_pack_for_task
from tret.services import connections as connections_service
from tret.services import lifecycle
from tret.services.packaging import PACKAGING_VERSION, ROLES
from tret.services.workspace import COLLECTION_HARNESS_NAME

log = logging.getLogger("tret.collections")

# ── vocabularies (the API contract's) ────────────────────────────────────────
MARKETS = ("US", "UK", "EU")
CATEGORIES = ("financial", "industrial", "other")
EXPECTED_FORMS = ("standalone", "embedded", "unknown")
COLLECTION_STATUSES = ("draft", "running", "paused", "gate", "review", "completed", "finalized", "failed")
TARGET_STATUSES = (
    "pending",
    "held",
    "running",
    "review",
    "packaging",
    "written",
    "completed",
    "gap",
    "failed",
    "rejected",
    "write_failed",
)
TERMINAL_TARGET_STATUSES = frozenset({"written", "completed", "gap", "failed", "rejected", "write_failed"})
DONE_TARGET_STATUSES = frozenset({"written", "completed"})
# What `derive_target_status` returns when every approved map is packaged, the
# collection writes automatically, and some package file is not uploaded yet.
# Never stored: `_settle_target` turns it into `written` or `write_failed`.
WRITE_PENDING = "write_pending"

RUN_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,60}$")
TARGET_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
# One or two plain folder names: the upload path is write_root/run_slug/
# packages/<target>/<file>, and `safe_upload_path` allows six segments.
WRITE_ROOT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,59}(/[A-Za-z0-9][A-Za-z0-9_-]{0,59})?$")
CSV_COLUMNS = (
    "company",
    "market",
    "domicile_country",
    "sector",
    "category",
    "expected_form",
    "fiscal_year",
    "known_urls",
    "notes",
)
MAX_TARGETS = 500
MAX_KNOWN_URLS = 20
CONCURRENCY_MIN, CONCURRENCY_MAX = 1, 6
# How long a timed-out target run gets to notice the engine's cooperative cancel
# before its task is torn down (the same grace `delegate_parallel` gives).
CANCEL_GRACE_SECONDS = 30.0
COMPLETENESS_DEFINITION = (
    "captured core disclosure within reviewed scope; not independently certified complete"
)
# In upload order: SHA256SUMS.txt last, since it lists the other five.
RECORD_FILES = (
    "manifest.json",
    "execution.json",
    "exception_log.json",
    "target_register.json",
    "results_summary.md",
    "SHA256SUMS.txt",
)
SUCCESS_RUN_STATUSES = ("completed", "completed_without_output")
# Set on a target whose packages were ready only after its collection was
# finalized: nothing is uploaded until an operator asks (retry-write).
FINALIZED_BEFORE_WRITE = "collection finalized before write; re-finalize after retry-write"
# How the prompt describes each packaging role; the list itself always comes
# from `services/packaging.ROLES`, so a role that stops existing drops out.
ROLE_GUIDANCE = {
    "primary_disclosure": "a standalone sustainability report",
    "embedded_statement_and_assurance": "the sustainability section of an annual or management report",
    "assurance": "a separate assurance statement",
    "data_appendix": "a substantive data appendix",
    "incorporated_context": "material explicitly incorporated by reference",
    "methodology": "a methodology or basis-of-preparation document",
    "other": "anything else worth retaining",
}

# Strong references to in-flight target tasks (asyncio keeps only weak ones);
# also what the shutdown drain in `tret/main.py` waits on.
_collection_tasks: set[asyncio.Task] = set()
# (event loop, semaphore / lock), created lazily on the loop that first needs
# them so a restarted loop (tests, reloads) never inherits one bound to a dead loop.
_semaphore_state: tuple[asyncio.AbstractEventLoop, asyncio.Semaphore] | None = None
_lock_state: tuple[asyncio.AbstractEventLoop, asyncio.Lock] | None = None
# One lock per target, so two settlers (a decision, a packaging callback, a
# retry-write) never upload the same target's files at once.
_target_locks: dict[uuid.UUID, tuple[asyncio.AbstractEventLoop, asyncio.Lock]] = {}
# The app whose draining state gates launches (`bind_app`, from main's lifespan).
_app: Any = None


def bind_app(app: Any) -> None:
    """Tell the service which app's draining flag to honour: no new target is
    launched while `lifecycle.is_draining(app)`."""
    global _app
    _app = app


def _target_lock(target_id: uuid.UUID) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    entry = _target_locks.get(target_id)
    if entry is None or entry[0] is not loop:
        entry = (loop, asyncio.Lock())
        _target_locks[target_id] = entry
    return entry[1]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def clamp_concurrency(value: Any) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = 3
    return max(CONCURRENCY_MIN, min(CONCURRENCY_MAX, n))


def _semaphore() -> asyncio.Semaphore:
    global _semaphore_state
    loop = asyncio.get_running_loop()
    if _semaphore_state is None or _semaphore_state[0] is not loop:
        size = clamp_concurrency(get_settings().collections_max_concurrent)
        _semaphore_state = (loop, asyncio.Semaphore(size))
    return _semaphore_state[1]


def _launch_lock() -> asyncio.Lock:
    global _lock_state
    loop = asyncio.get_running_loop()
    if _lock_state is None or _lock_state[0] is not loop:
        _lock_state = (loop, asyncio.Lock())
    return _lock_state[1]


def _session_factory():
    # Resolved per call (not bound at import) so tests can swap
    # `get_session_factory` on this module.
    return get_session_factory()


# ══════════════════════════════════════════════════════════════════════════
# register
# ══════════════════════════════════════════════════════════════════════════
class TargetIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company: str
    market: str
    domicile_country: str | None = None
    sector: str | None = None
    category: str | None = None
    expected_form: str | None = None
    fiscal_year: int | None = None
    known_urls: list[str] | str | None = None
    notes: str | None = None
    slug: str | None = None


class CreateCollectionBody(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str = Field(min_length=1, max_length=200)
    run_slug: str
    # `register` on the wire; renamed here because it shadows a BaseModel attribute.
    register_rows: list[TargetIn] | None = Field(
        default=None, max_length=MAX_TARGETS, alias="register"
    )
    register_csv: str | None = Field(default=None, max_length=2_000_000)
    target_slug: str
    write_root: str = "runs"
    concurrency: int = 3
    first_batch_size: int = Field(default=0, ge=0)
    fiscal_year: int = Field(default=2025, ge=2000, le=2100)
    auto_write: bool = True
    model_override: str | None = Field(default=None, max_length=200)
    max_iterations: int | None = Field(default=None, ge=1, le=50)


_URL_RE = re.compile(r"https?://\S+")
ERROR_SUMMARY_MAX = 300


def summarise_run_error(text: str | None) -> str | None:
    """A run's error made safe for a customer-facing record. A provider error
    often carries the raw upstream JSON body, with an internal key identifier
    and account URL in it. The first `{` to the last `}` is replaced by the
    object's `error.message` (or `message`) when it parses, else dropped; then
    every http(s) URL is removed, whitespace collapsed and the result capped at
    300 characters. A plain error passes through unchanged."""
    if text is None:
        return None
    s = str(text)
    start, end = s.find("{"), s.rfind("}")
    if start != -1 and end > start:
        message = ""
        with contextlib.suppress(ValueError, TypeError):
            obj = json.loads(s[start : end + 1])
            if isinstance(obj, dict):
                err = obj.get("error")
                found = err.get("message") if isinstance(err, dict) else None
                if not isinstance(found, str):
                    found = obj.get("message")
                if isinstance(found, str):
                    message = found
        s = s[:start] + message + s[end + 1 :]
    s = _URL_RE.sub("", s)
    return re.sub(r"\s+", " ", s).strip()[:ERROR_SUMMARY_MAX]


class RegisterError(ValueError):
    """A register row that cannot be accepted; the message names the row."""


_CORPORATE_SUFFIXES = (
    "plc",
    "p.l.c.",
    "se",
    "ag",
    "inc",
    "inc.",
    "incorporated",
    "corporation",
    "corp",
    "corp.",
    "n.v.",
    "nv",
    "s.a.",
    "sa",
    "s.p.a.",
    "spa",
    "ltd",
    "ltd.",
    "limited",
    "co.",
    "llc",
    "asa",
    "ab",
    "oyj",
    "a/s",
    "a.s.",
    "a. s.",
    "gmbh",
    "kgaa",
)


# Letters NFKD does not decompose into an ascii base; without this "Ørsted"
# loses its first letter.
_TRANSLITERATE = str.maketrans(
    {
        "ø": "o", "Ø": "O", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE", "ß": "ss",
        "ð": "d", "Ð": "D", "þ": "th", "Þ": "TH", "ł": "l", "Ł": "L", "đ": "d", "Đ": "D",
    }
)


def _strip_dangling_and(slug: str) -> str:
    # "Chase & Co." loses "Co." as a suffix and leaves the "&" spelled out. A
    # leading "and" stays: it can be the name itself ("AND Digital").
    while slug.endswith("-and"):
        slug = slug[:-4]
    return slug


def slugify(company: str) -> str:
    """Lowercase ascii letters/digits/hyphens, at most 40 characters, with
    trailing corporate suffixes (" plc", " SE", " Inc.", " N.V." ...) removed."""
    name = " ".join((company or "").split())
    changed = True
    while changed:
        changed = False
        stripped = name.rstrip(" ,")
        lowered = stripped.lower()
        for suffix in _CORPORATE_SUFFIXES:
            if lowered.endswith(" " + suffix) and len(stripped) > len(suffix) + 1:
                name = stripped[: -len(suffix) - 1]
                changed = True
                break
    name = name.translate(_TRANSLITERATE)
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    ascii_name = ascii_name.lower().replace("&", " and ")
    slug = _strip_dangling_and(re.sub(r"[^a-z0-9]+", "-", ascii_name).strip("-"))
    slug = _strip_dangling_and(slug[:40].strip("-"))
    return slug or "target"


def _dedupe_slug(base: str, taken: set[str]) -> str:
    if base not in taken:
        return base
    n = 2
    while True:
        suffix = f"-{n}"
        candidate = f"{base[: 40 - len(suffix)].rstrip('-')}{suffix}"
        if candidate not in taken:
            return candidate
        n += 1


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_row(raw: dict, label: str, *, default_fiscal_year: int) -> dict:
    company = _clean(raw.get("company"))
    if not company:
        raise RegisterError(f"{label}: company is required")
    if len(company) > 200:
        raise RegisterError(f"{label} ({company}): company name is longer than 200 characters")
    named = f"{label} ({company})"
    market = (_clean(raw.get("market")) or "").upper()
    if market not in MARKETS:
        raise RegisterError(
            f"{named}: market '{raw.get('market') or ''}' is not one of {', '.join(MARKETS)}"
        )
    category = _clean(raw.get("category"))
    if category is not None:
        category = category.lower()
        if category not in CATEGORIES:
            raise RegisterError(
                f"{named}: category '{category}' is not one of {', '.join(CATEGORIES)}"
            )
    expected_form = (_clean(raw.get("expected_form")) or "unknown").lower()
    if expected_form not in EXPECTED_FORMS:
        raise RegisterError(
            f"{named}: expected_form '{expected_form}' is not one of {', '.join(EXPECTED_FORMS)}"
        )
    fy_raw = raw.get("fiscal_year")
    if fy_raw is None or (isinstance(fy_raw, str) and not fy_raw.strip()):
        fiscal_year = default_fiscal_year
    else:
        try:
            fiscal_year = int(str(fy_raw).strip())
        except ValueError as e:
            raise RegisterError(f"{named}: fiscal_year '{fy_raw}' is not a year") from e
        if not 2000 <= fiscal_year <= 2100:
            raise RegisterError(f"{named}: fiscal_year {fiscal_year} is out of range")
    urls_raw = raw.get("known_urls")
    if urls_raw is None:
        urls: list[str] = []
    elif isinstance(urls_raw, str):
        urls = [u.strip() for u in urls_raw.split("|") if u.strip()]
    else:
        urls = [str(u).strip() for u in urls_raw if str(u).strip()]
    if len(urls) > MAX_KNOWN_URLS:
        raise RegisterError(f"{named}: at most {MAX_KNOWN_URLS} known_urls")
    for url in urls:
        if not url.lower().startswith("https://") or len(url) > 2000 or any(c.isspace() for c in url):
            raise RegisterError(f"{named}: known_urls entry '{url[:120]}' must be an https:// URL")
    notes = _clean(raw.get("notes"))
    if notes is not None and len(notes) > 2000:
        raise RegisterError(f"{named}: notes are longer than 2000 characters")
    slug = _clean(raw.get("slug"))
    if slug is not None and not TARGET_SLUG_RE.match(slug):
        raise RegisterError(
            f"{named}: slug '{slug}' must be lowercase letters, digits and hyphens (at most 40)"
        )
    return {
        "company": company,
        "market": market,
        "domicile_country": _clean(raw.get("domicile_country")),
        "sector": _clean(raw.get("sector")),
        "category": category,
        "expected_form": expected_form,
        "fiscal_year": fiscal_year,
        "known_urls": urls,
        "notes": notes,
        "slug": slug,
    }


def register_warnings(rows: list[dict]) -> list[str]:
    """The briefing's sample-shape checks: at least 3 financial and 5 industrial
    targets per market, at least 6 embedded disclosures in UK and EU, and no
    empty market. Warnings, never errors."""
    warnings: list[str] = []
    for market in MARKETS:
        in_market = [r for r in rows if r["market"] == market]
        if not in_market:
            warnings.append(f"{market}: no targets")
            continue
        financial = sum(1 for r in in_market if r["category"] == "financial")
        industrial = sum(1 for r in in_market if r["category"] == "industrial")
        if financial < 3:
            noun = "institution" if financial == 1 else "institutions"
            warnings.append(f"{market}: {financial} financial {noun} (briefing asks for at least 3)")
        if industrial < 5:
            noun = "company" if industrial == 1 else "companies"
            warnings.append(f"{market}: {industrial} industrial {noun} (briefing asks for at least 5)")
        if market in ("UK", "EU"):
            embedded = sum(1 for r in in_market if r["expected_form"] == "embedded")
            if embedded < 6:
                noun = "disclosure" if embedded == 1 else "disclosures"
                warnings.append(
                    f"{market}: {embedded} embedded {noun} (briefing asks for at least 6)"
                )
    return warnings


def parse_register(
    register: list[dict] | None,
    register_csv: str | None,
    *,
    default_fiscal_year: int,
) -> tuple[list[dict], list[str]]:
    """Validate a register (a JSON list or the contract's CSV) into target dicts
    in row order, with `position` (from 1) and a unique `slug` each, plus the
    register warnings. Raises `RegisterError` naming the first bad row."""
    if (register is None) == (register_csv is None):
        raise RegisterError("Provide exactly one of register or register_csv.")
    raw_rows: list[tuple[str, dict]] = []
    if register_csv is not None:
        reader = csv.DictReader(io.StringIO(register_csv.lstrip("﻿")))
        header = [(h or "").strip() for h in (reader.fieldnames or [])]
        if not header:
            raise RegisterError("The CSV has no header row.")
        reader.fieldnames = header
        unknown = [h for h in header if h not in CSV_COLUMNS and h != "slug"]
        if unknown:
            raise RegisterError(
                f"Unknown CSV column(s): {', '.join(unknown)}. Expected: {','.join(CSV_COLUMNS)}"
            )
        for required in ("company", "market"):
            if required not in header:
                raise RegisterError(f"The CSV header has no '{required}' column.")
        for n, row in enumerate(reader, start=1):
            if None in row:
                raise RegisterError(f"Row {n} (line {n + 1}): more cells than header columns")
            if not any((v or "").strip() for v in row.values()):
                continue  # a blank line
            raw_rows.append((f"Row {n} (line {n + 1})", row))
    else:
        for n, row in enumerate(register or [], start=1):
            if isinstance(row, BaseModel):
                row = row.model_dump()
            if not isinstance(row, dict):
                raise RegisterError(f"Row {n}: must be an object")
            raw_rows.append((f"Row {n}", row))
    if not raw_rows:
        raise RegisterError("The register has no targets.")
    if len(raw_rows) > MAX_TARGETS:
        raise RegisterError(f"The register has {len(raw_rows)} targets; at most {MAX_TARGETS}.")

    parsed = [_parse_row(row, label, default_fiscal_year=default_fiscal_year) for label, row in raw_rows]
    # Explicit slugs are reserved first so a derived slug never takes one.
    taken: set[str] = set()
    for (label, _), row in zip(raw_rows, parsed):
        if row["slug"] is not None:
            if row["slug"] in taken:
                raise RegisterError(f"{label} ({row['company']}): slug '{row['slug']}' is used twice")
            taken.add(row["slug"])
    for position, row in enumerate(parsed, start=1):
        row["position"] = position
        if row["slug"] is None:
            row["slug"] = _dedupe_slug(slugify(row["company"]), taken)
            taken.add(row["slug"])
    return parsed, register_warnings(parsed)


# ══════════════════════════════════════════════════════════════════════════
# prompt
# ══════════════════════════════════════════════════════════════════════════
def build_prompt(collection: Collection, target: CollectionTarget, total: int) -> str:
    fy = target.fiscal_year
    urls = "\n".join(f"  {u}" for u in (target.known_urls or [])) if target.known_urls else "none"
    if target.known_urls:
        urls = "\n" + urls
    slug = target.slug
    company = target.company
    roles = ", ".join(ROLES)
    role_guidance = "; ".join(f"{r} for {ROLE_GUIDANCE[r]}" for r in ROLES if r in ROLE_GUIDANCE)
    return f"""You are collecting one company's sustainability disclosure for the Kith Climate disclosure repository. Collection run "{collection.run_slug}", target {target.position} of {total}.

TARGET
- Company: {company}
- Market: {target.market}; domicile: {target.domicile_country or "not recorded"}; sector: {target.sector or "not recorded"}; category: {target.category or "not recorded"}; expected form: {target.expected_form}
- Fiscal year wanted: FY{fy}
- Known URLs (open these first, exactly as given): {urls}
- Notes: {target.notes or "none"}

DOCTRINE (from the Kith Climate collection briefing; follow it exactly)
1. Collect the FY{fy} sustainability disclosure where it is published. If only an earlier period is available, collect the latest available period; report fy2025_gap=true when the period collected is not FY{fy}, with the period actually collected. Never switch to a later year because a live reporting hub has moved on.
2. Prefer issuer sources, then official regulatory or report repositories (SEC EDGAR, FCA National Storage Mechanism, ESEF portals). A third-party copy is a last resort: report it as a substitution with the reason.
3. Retain the actual disclosure (a standalone sustainability report, or the sustainability section of an annual or management report), the related assurance statement, substantive data appendices, and material explicitly incorporated by reference. General financial statements stay out. A marketing summary is not a disclosure. Distinguish an ESRS sustainability statement from voluntary reporting and record the issuer's stated framework without inferring mandatory status.
4. Page ranges come only from pages you have read in the captured document; never infer them. For an embedded disclosure, map the physical PDF pages of the sustainability section, its assurance and incorporated sections, and exclude the rest.
5. Record dates separately: reporting period start and end, publication date with its basis, assurance date. Unknown stays unknown.

PROCEDURE
a. Open the known URLs, or search with a few precise queries ("{company} sustainability report {fy} pdf", "{company} annual report {fy} sustainability statement"). Do not loop on searches: if the document cannot be reached, report it.
b. capture_source the primary PDF, and separate assurance or appendix PDFs if they exist.
c. read_document to confirm company, period and page layout before mapping. In a long report, locate the sustainability statement, its assurance report and any appendix with search_documents using the section titles from the contents page (each hit names its page and chunk), then read_document with that hit's chunk_ordinal and read forward from there. Never page through a long document from the start and never guess chunk ordinals; the page ranges you map must come from pages you read this way.
d. propose_package_map for each document to retain. Every stem must be unique within this target: use "{slug}" for the primary disclosure, "{slug}-assurance", "{slug}-appendix", and for each further document "{slug}-container-<short-topic>" (for example "{slug}-container-methodology"); never reuse a stem. role is one of {roles}: {role_guidance}. Request ocr="auto" when pages look scanned.
e. Finish by calling report_collection_outcome exactly once with status captured, substituted, gap or blocked and the metadata you established. Call it even when you found nothing; it is how this target is scored.

Do not call propose_connected_write: the collection run writes approved packages itself. There is no user in this run; do not ask questions. Stop after report_collection_outcome."""


# ══════════════════════════════════════════════════════════════════════════
# status derivation (pure)
# ══════════════════════════════════════════════════════════════════════════
def _relevant_maps(findings: Iterable[Any]) -> list[Any]:
    """The run's live `package_map` findings: a superseded (or noted) draft was
    replaced by a newer proposal and decides nothing."""
    return [
        f
        for f in findings
        if getattr(f, "schema_slug", "package_map") == "package_map"
        and f.status in ("draft", "approved", "rejected")
    ]


def _package_of(finding: Any) -> dict | None:
    package = (finding.payload or {}).get("package")
    return package if isinstance(package, dict) else None


def derive_target_status(
    run_status: str | None,
    outcome: dict | None,
    findings: Iterable[Any],
    *,
    run_error: str | None = None,
    auto_write: bool = False,
    written: bool = False,
) -> tuple[str, str | None]:
    """(status, error) for a target whose run is `run_status`.

    1. Run queued/running -> running.
    2. Run ended without success (failed, cancelled, missing) and proposed no
       live map -> failed (the run's error). One that DID propose maps is judged
       by its maps below, with `error` "run ended <status>: <error>" as a warning.
    3. No live package_map finding -> gap (error "run ended without a package
       map" unless the outcome reported gap/blocked).
    4. Any map still draft -> review.
    5. Every map rejected -> rejected.
    6. An approved map with no package record yet, or one still running -> packaging.
    7. An approved map whose packaging failed -> failed (the package's error).
    8. Every approved map packaged: auto_write -> written when every file is
       uploaded (`written`), else WRITE_PENDING; auto_write off -> completed.
    """
    if run_status in ("queued", "running"):
        return "running", None
    maps = _relevant_maps(findings)
    warning = None
    if run_status not in SUCCESS_RUN_STATUSES:
        if not maps:
            return "failed", run_error or f"run {run_status or 'missing'}"
        warning = f"run ended {run_status or 'missing'}: {run_error or 'no error recorded'}"
    if not maps:
        if (outcome or {}).get("status") in ("gap", "blocked"):
            return "gap", None
        return "gap", "run ended without a package map"
    if any(m.status == "draft" for m in maps):
        return "review", warning
    approved = [m for m in maps if m.status == "approved"]
    if not approved:
        return "rejected", warning
    packages = [_package_of(m) for m in approved]
    if any(p is None or p.get("status") == "running" for p in packages):
        return "packaging", warning
    failed = [p for p in packages if p.get("status") != "packaged"]
    if failed:
        return "failed", f"packaging failed: {failed[0].get('error') or 'unknown error'}"
    if not auto_write:
        return "completed", warning
    return ("written", warning) if written else (WRITE_PENDING, warning)


def derive_collection_status(current: str, target_statuses: Iterable[str]) -> str:
    """The collection's status from its targets'. `draft`, `paused`,
    `finalized` and `failed` are the operator's (or reserved) and stay put."""
    if current in ("draft", "paused", "finalized", "failed"):
        return current
    statuses = list(target_statuses)
    if "running" in statuses or "pending" in statuses:
        return "running"
    # Review outranks the gate, so decisions still owed stay visible.
    if any(s in ("review", "packaging") for s in statuses):
        return "review"
    if "held" in statuses:
        return "gate"
    return "completed"


# ══════════════════════════════════════════════════════════════════════════
# derived fields (shared by the API serializers and the records)
# ══════════════════════════════════════════════════════════════════════════
def run_cost(run: Run | None) -> float:
    if run is None:
        return 0.0
    return float(run.cost_usd or 0) + float(run.delegated_cost_usd or 0)


def target_cost(t: CollectionTarget, run: Run | None) -> float:
    """Every attempt's spend: the current run plus the archived attempts."""
    past = sum(float(h.get("cost_usd") or 0) for h in (t.history or []))
    return run_cost(run) + past


def written_files_latest(t: CollectionTarget) -> list[dict]:
    """`written_files` is append-only; a re-upload of the same path replaces the
    file in SharePoint, so the latest record per filename is the live one."""
    latest: dict[str, dict] = {}
    for w in t.written_files or []:
        latest.pop(w.get("filename"), None)
        latest[w.get("filename")] = w
    return list(latest.values())


def package_summaries(findings: Iterable[Finding]) -> list[dict]:
    out = []
    for f in _relevant_maps(findings):
        payload = f.payload or {}
        package = _package_of(f)
        out.append(
            {
                "finding_id": str(f.id),
                "stem": payload.get("stem"),
                "role": (payload.get("map") or {}).get("role"),
                "status": f.status,
                "package_status": package.get("status") if package else None,
                "retained_page_count": package.get("retained_page_count") if package else None,
                "retained_sha256": package.get("retained_sha256") if package else None,
                "documents": [
                    {
                        "role": d.get("role"),
                        "filename": d.get("filename"),
                        "sha256": d.get("sha256"),
                        "byte_size": d.get("byte_size"),
                    }
                    for d in ((package or {}).get("documents") or [])
                ],
                "wall_seconds": package.get("wall_seconds") if package else None,
            }
        )
    return out


def target_out(t: CollectionTarget, run: Run | None, findings: list[Finding]) -> dict:
    return {
        "id": str(t.id),
        "collection_id": str(t.collection_id),
        "position": t.position,
        "slug": t.slug,
        "company": t.company,
        "market": t.market,
        "domicile_country": t.domicile_country,
        "sector": t.sector,
        "category": t.category,
        "expected_form": t.expected_form,
        "fiscal_year": t.fiscal_year,
        "known_urls": list(t.known_urls or []),
        "notes": t.notes,
        "status": t.status,
        "run_id": str(t.run_id) if t.run_id else None,
        "finding_ids": list(t.finding_ids or []),
        "attempts": t.attempts or 0,
        "error": summarise_run_error(t.error),
        "outcome": t.outcome,
        "packages": package_summaries(findings),
        "written_files": [
            {k: w.get(k) for k in ("filename", "web_url", "sha256", "byte_size", "uploaded_at")}
            for w in written_files_latest(t)
        ],
        "history": list(t.history or []),
        "cost_usd": round(target_cost(t, run), 6),
        "model_used": run.model_used if run else None,
        "iterations": run.iterations if run else None,
        "started_at": _iso(t.started_at),
        "finished_at": _iso(t.finished_at),
    }


def collection_counts(targets: list[CollectionTarget]) -> dict:
    by_status = {s: 0 for s in TARGET_STATUSES}
    by_market = {m: {"total": 0, "done": 0, "gap": 0, "failed": 0} for m in MARKETS}
    for t in targets:
        by_status[t.status] = by_status.get(t.status, 0) + 1
        m = by_market.setdefault(t.market, {"total": 0, "done": 0, "gap": 0, "failed": 0})
        m["total"] += 1
        if t.status in DONE_TARGET_STATUSES:
            m["done"] += 1
        elif t.status == "gap":
            m["gap"] += 1
        elif t.status in ("failed", "write_failed"):
            m["failed"] += 1
    return {"total": len(targets), "by_status": by_status, "by_market": by_market}


def collection_out(c: Collection, targets: list[CollectionTarget], runs: dict[uuid.UUID, Run]) -> dict:
    return {
        "id": str(c.id),
        "workspace_id": str(c.workspace_id),
        "name": c.name,
        "run_slug": c.run_slug,
        "status": c.status,
        "settings": {
            k: (c.settings or {}).get(k)
            for k in (
                "target_slug",
                "write_root",
                "concurrency",
                "first_batch_size",
                "fiscal_year",
                "auto_write",
                "model_override",
                "max_iterations",
            )
        },
        "created_by": str(c.created_by) if c.created_by else None,
        "created_at": _iso(c.created_at),
        "started_at": _iso(c.started_at),
        "finalized_at": _iso(c.finalized_at),
        "counts": collection_counts(targets),
        "cost_usd": round(
            sum(target_cost(t, runs.get(t.run_id) if t.run_id else None) for t in targets), 6
        ),
        "warnings": list(c.warnings or []),
        "finalize": c.finalize,
        "last_error": (c.settings or {}).get("last_error"),
    }


async def load_targets(db: AsyncSession, collection_ids: list[uuid.UUID]) -> list[CollectionTarget]:
    if not collection_ids:
        return []
    return list(
        (
            await db.execute(
                select(CollectionTarget)
                .where(CollectionTarget.collection_id.in_(collection_ids))
                .order_by(CollectionTarget.collection_id, CollectionTarget.position)
                .execution_options(populate_existing=True)
            )
        ).scalars()
    )


async def load_runs(db: AsyncSession, targets: list[CollectionTarget]) -> dict[uuid.UUID, Run]:
    run_ids = [t.run_id for t in targets if t.run_id]
    if not run_ids:
        return {}
    rows = (
        await db.execute(
            select(Run).where(Run.id.in_(run_ids)).execution_options(populate_existing=True)
        )
    ).scalars()
    return {r.id: r for r in rows}


async def load_maps(db: AsyncSession, targets: list[CollectionTarget]) -> dict[uuid.UUID, list[Finding]]:
    """`package_map` findings by run id, oldest first."""
    run_ids = [t.run_id for t in targets if t.run_id]
    if not run_ids:
        return {}
    rows = (
        await db.execute(
            select(Finding)
            .where(Finding.run_id.in_(run_ids), Finding.schema_slug == "package_map")
            .order_by(Finding.created_at)
            .execution_options(populate_existing=True)
        )
    ).scalars()
    out: dict[uuid.UUID, list[Finding]] = {}
    for f in rows:
        out.setdefault(f.run_id, []).append(f)
    return out


# ══════════════════════════════════════════════════════════════════════════
# lifecycle
# ══════════════════════════════════════════════════════════════════════════
async def get_collection(db: AsyncSession, collection_id: uuid.UUID, workspace_id: uuid.UUID) -> Collection:
    c = await db.get(Collection, collection_id)
    if c is None or c.workspace_id != workspace_id:
        raise HTTPException(404, "Collection not found")
    return c


async def _collector_harness(db: AsyncSession, workspace_id: uuid.UUID) -> Harness | None:
    return (
        await db.execute(
            select(Harness)
            .where(
                Harness.workspace_id == workspace_id,
                Harness.name == COLLECTION_HARNESS_NAME,
                Harness.is_archived.is_(False),
            )
            .order_by(Harness.created_at)
        )
    ).scalars().first()


async def create_collection(
    db: AsyncSession, *, workspace_id: uuid.UUID, user_id: uuid.UUID | None, body: CreateCollectionBody
) -> tuple[Collection, list[CollectionTarget]]:
    if not RUN_SLUG_RE.match(body.run_slug):
        raise HTTPException(
            422, "run_slug must be 3-61 lowercase letters, digits or hyphens, starting with a letter or digit"
        )
    write_root = body.write_root.strip().strip("/")
    if not WRITE_ROOT_RE.match(write_root):
        raise HTTPException(
            422, "write_root must be one or two folder names (letters, digits, '_' or '-')"
        )
    try:
        rows, warnings = parse_register(
            [t.model_dump() for t in body.register_rows] if body.register_rows is not None else None,
            body.register_csv,
            default_fiscal_year=body.fiscal_year,
        )
    except RegisterError as e:
        raise HTTPException(422, str(e)) from e
    existing = (
        await db.execute(
            select(Collection.id).where(
                Collection.workspace_id == workspace_id, Collection.run_slug == body.run_slug
            )
        )
    ).first()
    if existing is not None:
        raise HTTPException(409, f"A collection with run_slug '{body.run_slug}' already exists")
    try:
        allowed = [t.slug for t in await connections_service.list_write_targets(db, workspace_id)]
    except connections_service.ConnectionUnavailable as e:
        raise HTTPException(422, f"No usable Microsoft 365 connection: {e.reason}") from e
    if body.target_slug not in allowed:
        names = ", ".join(allowed) if allowed else "none are configured"
        raise HTTPException(
            422, f"target_slug '{body.target_slug}' is not a write target; allowed: {names}"
        )
    project = (
        await db.execute(
            select(Project).where(Project.workspace_id == workspace_id).order_by(Project.created_at)
        )
    ).scalars().first()
    if project is None:
        raise HTTPException(500, "No project exists")

    collection = Collection(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        project_id=project.id,
        name=body.name.strip(),
        run_slug=body.run_slug,
        status="draft",
        settings={
            "target_slug": body.target_slug,
            "write_root": write_root,
            "concurrency": clamp_concurrency(body.concurrency),
            "first_batch_size": body.first_batch_size,
            "fiscal_year": body.fiscal_year,
            "auto_write": body.auto_write,
            "model_override": (body.model_override or "").strip() or None,
            "max_iterations": body.max_iterations,
        },
        warnings=warnings,
        created_by=user_id,
        created_at=_now(),
    )
    db.add(collection)
    gate = body.first_batch_size
    targets = []
    for row in rows:
        t = CollectionTarget(
            id=uuid.uuid4(),
            collection_id=collection.id,
            position=row["position"],
            slug=row["slug"],
            company=row["company"],
            market=row["market"],
            domicile_country=row["domicile_country"],
            sector=row["sector"],
            category=row["category"],
            expected_form=row["expected_form"],
            fiscal_year=row["fiscal_year"],
            known_urls=row["known_urls"],
            notes=row["notes"],
            status="held" if gate > 0 and row["position"] > gate else "pending",
            finding_ids=[],
            attempts=0,
            written_files=[],
        )
        targets.append(t)
    await db.flush()  # the collection row first: targets reference it
    for t in targets:
        db.add(t)
    await db.commit()
    return collection, targets


def _require(c: Collection, allowed: tuple[str, ...], verb: str) -> None:
    if c.status not in allowed:
        raise HTTPException(409, f"Collection is '{c.status}'; it cannot be {verb} now")


async def start(db: AsyncSession, c: Collection) -> None:
    _require(c, ("draft",), "started")
    if await _collector_harness(db, c.workspace_id) is None:
        raise HTTPException(
            409,
            f"This workspace has no '{COLLECTION_HARNESS_NAME}' harness; it is seeded at boot "
            "while the research egress class is on.",
        )
    c.status = "running"
    c.started_at = _now()
    await db.commit()
    await _advance(c.id)


async def pause(db: AsyncSession, c: Collection) -> None:
    _require(c, ("running", "gate", "review"), "paused")
    c.status = "paused"
    await db.commit()


async def resume(db: AsyncSession, c: Collection) -> None:
    _require(c, ("paused",), "resumed")
    c.status = "running"
    if (c.settings or {}).get("last_error"):
        c.settings = {k: v for k, v in c.settings.items() if k != "last_error"}
    await db.commit()
    await _advance(c.id)


async def release(db: AsyncSession, c: Collection) -> None:
    _require(c, ("gate", "review", "completed", "paused"), "released")
    held = [t for t in await load_targets(db, [c.id]) if t.status == "held"]
    if not held:
        raise HTTPException(409, "No held targets")
    for t in held:
        t.status = "pending"
    if c.status != "paused":
        c.status = "running"
    await db.commit()
    await _advance(c.id)


def _archive_attempt(t: CollectionTarget, run: Run | None) -> None:
    """Push the attempt that is about to be replaced onto `history`, then clear
    what belongs to it (the outcome it reported, its error, its maps)."""
    if t.run_id is None and t.outcome is None and t.error is None:
        return
    t.history = [
        *(t.history or []),
        {
            "attempt": t.attempts or 0,
            "run_id": str(t.run_id) if t.run_id else None,
            "status": t.status,
            "error": summarise_run_error(t.error),
            "outcome": t.outcome,
            "cost_usd": round(run_cost(run), 6),
            "model_used": run.model_used if run else None,
            "started_at": _iso(t.started_at),
            "finished_at": _iso(t.finished_at),
        },
    ]
    t.run_id = None
    t.outcome = None
    t.error = None
    t.finding_ids = []
    t.started_at = None
    t.finished_at = None


async def retry_target(db: AsyncSession, c: Collection, t: CollectionTarget) -> None:
    if c.status in ("draft", "finalized"):
        raise HTTPException(409, f"Collection is '{c.status}'; its targets cannot be retried")
    if t.status not in ("failed", "gap", "rejected", "write_failed"):
        raise HTTPException(
            409,
            f"Target is '{t.status}'; only failed, gap, rejected or write_failed targets can be retried",
        )
    run = await db.get(Run, t.run_id) if t.run_id else None
    _archive_attempt(t, run)
    t.status = "pending"
    if c.status != "paused":
        c.status = "running"
    await db.commit()
    await _advance(c.id)


async def retry_write(db: AsyncSession, c: Collection, t: CollectionTarget) -> None:
    finalized_hold = c.status == "finalized" and t.error == FINALIZED_BEFORE_WRITE
    if t.status != "write_failed" and not finalized_hold:
        raise HTTPException(409, f"Target is '{t.status}'; only write_failed targets can retry the write")
    await _settle_target(db, c, t, force_write=True)
    await _recompute(db, c)


async def _advance(collection_id: uuid.UUID) -> None:
    """Launch whatever pending targets fit the collection's free slots, then
    re-derive its status (which is how `gate`/`review`/`completed` are reached
    when nothing is left to launch)."""
    await _launch(collection_id)
    async with _session_factory()() as db:
        c = await db.get(Collection, collection_id)
        if c is not None:
            await _recompute(db, c)


async def _recompute(db: AsyncSession, c: Collection) -> str:
    # Re-read under a row lock: an operator's pause (or a finalize) committed
    # by another request since `c` was loaded must win over a derived status.
    await db.refresh(c, with_for_update=True)
    targets = await load_targets(db, [c.id])
    new = derive_collection_status(c.status, [t.status for t in targets])
    if new != c.status:
        c.status = new
    await db.commit()
    return new


async def _launch(collection_id: uuid.UUID) -> int:
    """Create runs for the pending targets that fit the free slots (the
    collection's `concurrency` minus its running targets), in position order,
    and start a task for each. Returns how many were launched; none while the
    process is draining for shutdown."""
    if lifecycle.is_draining(_app):
        return 0
    async with _launch_lock():
        async with _session_factory()() as db:
            c = await db.get(Collection, collection_id)
            if c is None or c.status != "running":
                return 0
            targets = await load_targets(db, [c.id])
            running = sum(1 for t in targets if t.status == "running")
            slots = clamp_concurrency((c.settings or {}).get("concurrency")) - running
            pending = [t for t in targets if t.status == "pending"][: max(0, slots)]
            if not pending:
                return 0
            harness = await _collector_harness(db, c.workspace_id)
            if harness is None:
                c.status = "paused"
                c.settings = {
                    **(c.settings or {}),
                    "last_error": f"No '{COLLECTION_HARNESS_NAME}' harness in this workspace; "
                    "launching stopped.",
                }
                await db.commit()
                return 0
            pack = resolve_pack_for_task(await packs_for_harness(db, harness), "freeform")
            model_override = (c.settings or {}).get("model_override")
            max_iterations = (c.settings or {}).get("max_iterations")
            previous = await load_runs(db, pending)
            launched: list[tuple[uuid.UUID, uuid.UUID]] = []
            for t in pending:
                if t.run_id is not None:  # defensive: retry_target archives first
                    _archive_attempt(t, previous.get(t.run_id))
                task_input = {
                    "message": build_prompt(c, t, len(targets)),
                    COLLECTION_ID_KEY: str(c.id),
                    COLLECTION_TARGET_KEY: str(t.id),
                    "_label": f"collect:{t.slug}",
                }
                if model_override:
                    task_input["_model_override"] = model_override
                if max_iterations:
                    task_input["_max_iterations"] = int(max_iterations)
                run = Run(
                    id=uuid.uuid4(),
                    project_id=c.project_id,
                    harness_id=harness.id,
                    pack_id=pack.id if pack else None,
                    task_type="freeform",
                    task_input=task_input,
                    document_ids=[],
                    created_by=c.created_by,
                )
                db.add(run)
                # Insert the run before the target points at it: with no ORM
                # relationship between the two, the unit of work does not order
                # the UPDATE after the INSERT and Postgres rejects the FK.
                await db.flush()
                t.status = "running"
                t.run_id = run.id
                t.started_at = _now()
                t.finished_at = None
                t.error = None
                t.outcome = None
                t.finding_ids = []
                t.attempts = (t.attempts or 0) + 1
                launched.append((t.id, run.id))
            await db.commit()
        for target_id, run_id in launched:
            _spawn(_run_target(collection_id, target_id, run_id))
        return len(launched)


def _spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _collection_tasks.add(task)
    task.add_done_callback(_collection_tasks.discard)
    return task


async def _close_out_run(run_id: uuid.UUID, error: str) -> None:
    with contextlib.suppress(Exception):
        async with _session_factory()() as db:
            run = await db.get(Run, run_id)
            if run is not None and run.status in ("queued", "running"):
                run.status = "cancelled"
                run.error = error
                run.finished_at = _now()
                await db.commit()


async def _run_target(collection_id: uuid.UUID, target_id: uuid.UUID, run_id: uuid.UUID) -> None:
    """One target's run, start to settle. Never raises (except cancellation)."""
    cancelled = False
    try:
        async with _semaphore():
            async with _session_factory()() as db:
                c = await db.get(Collection, collection_id)
                t = await db.get(CollectionTarget, target_id)
                if c is None or t is None or t.run_id != run_id:
                    return
                if c.status != "running":
                    # Paused while this target waited for a process-wide slot:
                    # give the slot back untouched, to launch on resume.
                    run = await db.get(Run, run_id)
                    if run is not None and run.status == "queued":
                        run.status = "cancelled"
                        run.error = "collection paused before this run started"
                        run.finished_at = _now()
                    t.status = "pending"
                    t.run_id = None
                    t.started_at = None
                    t.attempts = max(0, (t.attempts or 1) - 1)
                    await db.commit()
                    return
            engine = get_harness_engine()
            timeout = max(1, int(get_settings().collections_run_timeout_seconds))
            task = asyncio.create_task(engine.execute(run_id))
            done, _ = await asyncio.wait({task}, timeout=timeout)
            timed_out = not done
            if timed_out:
                engine.cancel(run_id)
                done, _ = await asyncio.wait({task}, timeout=CANCEL_GRACE_SECONDS)
                if not done:
                    task.cancel()
                    # asyncio.wait never raises the task's CancelledError, so a
                    # cancellation of THIS task still propagates.
                    await asyncio.wait({task}, timeout=CANCEL_GRACE_SECONDS)
                await _close_out_run(run_id, "timeout: stopped by the collection run's time limit")
                forget = getattr(engine, "forget_cancelled", None)
                if forget is not None:
                    forget(run_id)
            else:
                task.result()  # an engine crash is a target failure below
        async with _session_factory()() as db:
            c = await db.get(Collection, collection_id)
            t = await db.get(CollectionTarget, target_id)
            if c is None or t is None or t.run_id != run_id:
                return
            status = await _settle_target(db, c, t)
            if timed_out and status == "failed":
                # A run that finished within the cancel grace settled normally
                # above; only one the time limit actually stopped is a timeout.
                t.error = "timeout"
                await db.commit()
    except asyncio.CancelledError:
        cancelled = True
        raise
    except Exception as e:  # noqa: BLE001 - a background task has no caller to raise to
        log.exception("collection target %s crashed", target_id)
        with contextlib.suppress(Exception):
            async with _session_factory()() as db:
                t = await db.get(CollectionTarget, target_id)
                if t is not None and t.run_id == run_id:
                    t.status = "failed"
                    t.error = summarise_run_error(f"{type(e).__name__}: {e}")
                    t.finished_at = _now()
                    await db.commit()
    finally:
        if not cancelled:
            try:
                await _advance(collection_id)
            except Exception:  # noqa: BLE001
                log.exception("collection %s could not advance", collection_id)


async def _settle_target(
    db: AsyncSession, c: Collection, t: CollectionTarget, *, force_write: bool = False
) -> str:
    """Re-derive `t`'s status from its run, its maps and its outcome, and upload
    its packages whenever every approved map is packaged and some file is not
    written yet, whatever status was stored before. Idempotent; serialized per
    target. After the collection is finalized nothing is uploaded unless
    `force_write` (retry-write). Commits."""
    async with _target_lock(t.id):
        # Another settler may have committed (written files, a new run) while
        # this one waited for the lock.
        await db.refresh(t)
        await db.refresh(c)
        run = await db.get(Run, t.run_id, populate_existing=True) if t.run_id else None
        maps = (await load_maps(db, [t])).get(t.run_id, []) if t.run_id else []
        t.finding_ids = [str(f.id) for f in maps]
        auto_write = bool((c.settings or {}).get("auto_write"))
        status, error = derive_target_status(
            run.status if run else None,
            t.outcome,
            maps,
            run_error=summarise_run_error(run.error) if run else "run missing",
            auto_write=auto_write or force_write,
            written=_all_written(t, maps),
        )
        if status == WRITE_PENDING:
            if c.status == "finalized" and not force_write:
                status, error = "completed", FINALIZED_BEFORE_WRITE
            else:
                status, write_error = await _write_packages(db, c, t, maps)
                error = write_error or error
        t.status = status
        t.error = summarise_run_error(error)
        if status in TERMINAL_TARGET_STATUSES:
            t.finished_at = t.finished_at or _now()
        else:
            t.finished_at = None
        await db.commit()
        return status


def _package_documents(maps: list[Finding]) -> list[tuple[Finding, dict]]:
    out = []
    for f in _relevant_maps(maps):
        if f.status != "approved":
            continue
        package = _package_of(f) or {}
        if package.get("status") != "packaged":
            continue
        for d in package.get("documents") or []:
            out.append((f, d))
    return out


def _all_written(t: CollectionTarget, maps: list[Finding]) -> bool:
    written = {w.get("document_id") for w in (t.written_files or [])}
    return all(d.get("document_id") in written for _, d in _package_documents(maps))


async def _write_packages(
    db: AsyncSession, c: Collection, t: CollectionTarget, maps: list[Finding]
) -> tuple[str, str | None]:
    """Upload every approved package file not yet uploaded, replacing whatever
    is at its path (the collection owns `<write_root>/<run>/packages/`).
    ("written", None) when all are; ("write_failed", reason) at the first
    failure, keeping the files already written. Each upload is attributed to the
    map's approver."""
    settings = c.settings or {}
    root = f"{settings.get('write_root') or 'runs'}/{c.run_slug}"
    cap = get_settings().egress_capture_max_bytes
    approvals = await _approvals(db, [f.id for f in maps])
    documents = _package_documents(maps)
    # Uploads use conflict="replace", so two packages with one file name would
    # silently overwrite each other; refuse before uploading anything.
    names: set[str] = set()
    for _f, d in documents:
        name = str(d.get("filename"))
        if name.casefold() in names:  # SharePoint names ignore case
            return (
                "write_failed",
                f"two approved packages for this target share the file name '{name}'; "
                "re-propose one with a distinct stem",
            )
        names.add(name.casefold())
    for f, d in documents:
        written = list(t.written_files or [])
        if any(w.get("document_id") == d.get("document_id") for w in written):
            continue
        relative = f"packages/{t.slug}/{d.get('filename')}"
        approval = approvals.get(f.id)
        try:
            data = await _document_bytes(db, c, d)
            result = await connections_service.upload_connected_file(
                db,
                workspace_id=c.workspace_id,
                target_slug=settings.get("target_slug"),
                filename=f"{root}/{relative}",
                data=data,
                content_type=d.get("content_type") or "application/octet-stream",
                actor_user_id=approval.approver_id if approval is not None else c.created_by,
                actor_run_id=t.run_id,
                conflict="replace",
                max_bytes=cap,
            )
        except (connections_service.ConnectionWriteError, connections_service.ConnectionUnavailable) as e:
            return "write_failed", f"upload failed for {relative}: {e.reason}"
        except (OSError, ValueError) as e:
            return "write_failed", f"upload failed for {relative}: {e}"
        except Exception as e:  # noqa: BLE001 - recorded on the target, retry-write retries
            log.exception("upload failed for %s", relative)
            return "write_failed", f"upload failed for {relative}: {type(e).__name__}: {e}"
        written.append(
            {
                "filename": relative,
                "web_url": result.web_url,
                "sha256": d.get("sha256"),
                "byte_size": d.get("byte_size"),
                "uploaded_at": _now().isoformat(),
                "finding_id": str(f.id),
                "document_id": d.get("document_id"),
            }
        )
        t.written_files = written  # a new list, so SQLAlchemy sees the JSONB change
        await db.commit()
    return "written", None


async def _document_bytes(db: AsyncSession, c: Collection, entry: dict) -> bytes:
    try:
        doc_id = uuid.UUID(str(entry.get("document_id")))
    except ValueError as e:
        raise ValueError("the package names no valid document") from e
    doc = await db.get(Document, doc_id)
    if doc is None or doc.project_id != c.project_id or not doc.storage_path:
        raise ValueError("the package document no longer exists")
    data = await asyncio.to_thread(Path(doc.storage_path).read_bytes)
    expected = entry.get("sha256")
    if expected and hashlib.sha256(data).hexdigest() != expected:
        raise ValueError("the stored package file no longer matches its recorded sha256")
    return data


async def _targets_for_findings(db: AsyncSession, finding_ids: Iterable[uuid.UUID]) -> list[CollectionTarget]:
    run_ids: set[uuid.UUID] = set()
    for fid in finding_ids:
        f = await db.get(Finding, fid)
        if f is not None and f.schema_slug == "package_map" and f.run_id is not None:
            run_ids.add(f.run_id)
    if not run_ids:
        return []
    return list(
        (
            await db.execute(select(CollectionTarget).where(CollectionTarget.run_id.in_(list(run_ids))))
        ).scalars()
    )


async def _resettle_targets(db: AsyncSession, target_ids: Iterable[uuid.UUID]) -> None:
    """Settle each target (always: the derivation decides, whatever is stored)
    and re-derive the collections they belong to."""
    touched: set[uuid.UUID] = set()
    for tid in target_ids:
        t = await db.get(CollectionTarget, tid)
        c = await db.get(Collection, t.collection_id) if t is not None else None
        if t is None or c is None:
            continue
        try:
            await _settle_target(db, c, t)
        except Exception:  # noqa: BLE001 - one target must not stop the rest
            log.exception("could not settle collection target %s", tid)
            with contextlib.suppress(Exception):
                await db.rollback()
        touched.add(c.id)
    for cid in touched:
        c = await db.get(Collection, cid)
        if c is not None:
            await _recompute(db, c)


async def _resettle_in_background(target_ids: list[uuid.UUID], session_factory) -> None:
    try:
        async with session_factory()() as db:
            await _resettle_targets(db, target_ids)
    except Exception:  # noqa: BLE001 - a background task has no caller to raise to
        log.exception("collection re-settle failed for targets %s", target_ids)


async def notify_decided(
    finding_ids: Iterable[uuid.UUID], *, db: AsyncSession, session_factory=None
) -> asyncio.Task | None:
    """Approvals moved: find the collection targets these findings belong to
    (a cheap read on the caller's session) and re-settle them in a background
    task, so a settle that uploads never runs inside the HTTP request. None
    when no finding belongs to a collection."""
    ids = list(finding_ids)
    if not ids:
        return None
    targets = await _targets_for_findings(db, ids)
    if not targets:
        return None
    factory = session_factory if session_factory is not None else _session_factory
    return _spawn(_resettle_in_background([t.id for t in targets], factory))


async def notify_package_finished(finding_id: uuid.UUID, *, session_factory=None) -> None:
    """Packaging finished (or failed) for `finding_id`: re-settle its target,
    which is what moves `packaging` -> `written` / `completed` / `failed`."""
    factory = session_factory() if session_factory is not None else _session_factory()
    async with factory() as db:
        targets = await _targets_for_findings(db, [finding_id])
        if targets:
            await _resettle_targets(db, [t.id for t in targets])


async def drain_collection_tasks(deadline_seconds: float) -> bool:
    """Wait up to `deadline_seconds` for every in-flight target task (including
    the ones finishing tasks launch). True when none are left."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + deadline_seconds
    while True:
        pending = {t for t in _collection_tasks if not t.done()}
        if not pending:
            return True
        remaining = deadline - loop.time()
        if remaining <= 0:
            return False
        await asyncio.wait(pending, timeout=remaining)


async def sweep_orphaned_collections(db: AsyncSession) -> int:
    """Boot sweep, after `sweep_orphaned_runs` and the packaging sweep. Targets
    left `running` whose run is no longer queued/running, and targets in
    `review`/`packaging`, are re-settled from the facts (a run that completed
    is judged by its maps; only one the orphan sweep failed becomes `failed`;
    a restart mid-upload resumes the upload). Collections still `running` with
    pending targets are paused for an operator's resume rather than launched at
    boot. Returns how many targets were re-settled."""
    targets = list(
        (
            await db.execute(
                select(CollectionTarget).where(
                    CollectionTarget.status.in_(["running", "review", "packaging"])
                )
            )
        ).scalars()
    )
    runs = await load_runs(db, targets)
    to_settle = []
    for t in targets:
        run = runs.get(t.run_id) if t.run_id else None
        if t.status == "running" and run is not None and run.status in ("queued", "running"):
            continue  # still live (only possible without the instance lock)
        to_settle.append(t.id)
    if to_settle:
        await _resettle_targets(db, to_settle)
        log.warning("boot sweep: re-settled %d collection target(s)", len(to_settle))
    collections = list(
        (await db.execute(select(Collection).where(Collection.status == "running"))).scalars()
    )
    for c in collections:
        if await _recompute(db, c) == "running":
            c.status = "paused"
            c.settings = {
                **(c.settings or {}),
                "last_error": "The server restarted during this collection; resume to continue.",
            }
            await db.commit()
    return len(to_settle)


# ══════════════════════════════════════════════════════════════════════════
# records
# ══════════════════════════════════════════════════════════════════════════
async def _approvals(db: AsyncSession, finding_ids: list[uuid.UUID]) -> dict[uuid.UUID, Approval]:
    if not finding_ids:
        return {}
    rows = (
        await db.execute(
            select(Approval).where(Approval.finding_id.in_(finding_ids)).order_by(Approval.created_at)
        )
    ).scalars()
    out: dict[uuid.UUID, Approval] = {}
    for a in rows:
        out[a.finding_id] = a  # the latest decision wins
    return out


async def _package_record(db: AsyncSession, package: dict) -> dict | None:
    """The packager's own `.package.json` record for a package, when its
    document is still on disk. Best effort: used for the phase timings only."""
    entry = next((d for d in package.get("documents") or [] if d.get("role") == "package_record"), None)
    if entry is None:
        return None
    try:
        doc = await db.get(Document, uuid.UUID(str(entry.get("document_id"))))
        if doc is None or not doc.storage_path:
            return None
        raw = await asyncio.to_thread(Path(doc.storage_path).read_text, encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


def _seconds_between(a: datetime | None, b: datetime | None) -> float | None:
    a, b = _aware(a), _aware(b)
    if a is None or b is None:
        return None
    return round((b - a).total_seconds(), 3)


async def build_records(
    db: AsyncSession, c: Collection, *, finalized_at: datetime | None = None
) -> dict:
    """Everything `finalize` writes, as data: manifest, execution, exception
    log, register copy, SHA256SUMS text and a markdown results summary."""
    generated = finalized_at or _now()
    targets = await load_targets(db, [c.id])
    runs = await load_runs(db, targets)
    maps_by_run = await load_maps(db, targets)
    all_maps = [f for fs in maps_by_run.values() for f in fs]
    approvals = await _approvals(db, [f.id for f in all_maps])
    approver_ids = {a.approver_id for a in approvals.values()}
    approvers: dict[uuid.UUID, str] = {}
    if approver_ids:
        for u in (await db.execute(select(User).where(User.id.in_(list(approver_ids))))).scalars():
            approvers[u.id] = u.display_name
    settings = c.settings or {}

    companies = []
    retained_docs = retained_pages = 0
    packaging_rows = []
    for t in targets:
        maps = _relevant_maps(maps_by_run.get(t.run_id, []) if t.run_id else [])
        written_by_doc = {w.get("document_id"): w for w in written_files_latest(t)}
        documents = []
        pack_seconds = 0.0
        ocr_pages = None
        for f in maps:
            payload = f.payload or {}
            m = payload.get("map") or {}
            source = payload.get("source") or {}
            package = _package_of(f) or {}
            approval = approvals.get(f.id)
            files = [
                {
                    "filename": d.get("filename"),
                    "role": d.get("role"),
                    "sha256": d.get("sha256"),
                    "byte_size": d.get("byte_size"),
                    "web_url": (written_by_doc.get(d.get("document_id")) or {}).get("web_url"),
                }
                for d in package.get("documents") or []
            ]
            record = await _package_record(db, package) if package.get("status") == "packaged" else None
            timings = (record or {}).get("timings") or (
                {"total": package.get("wall_seconds")} if package.get("wall_seconds") is not None else None
            )
            if package.get("status") == "packaged":
                retained_docs += 1
                retained_pages += int(package.get("retained_page_count") or 0)
                pack_seconds += float(package.get("wall_seconds") or 0)
                ocr = (record or {}).get("ocr")
                if isinstance(ocr, dict) and isinstance(ocr.get("pages"), (int, list)):
                    n = ocr["pages"] if isinstance(ocr["pages"], int) else len(ocr["pages"])
                    ocr_pages = (ocr_pages or 0) + n
            review: dict = {"status": f.status}
            if approval is not None:
                review["approver"] = approvers.get(approval.approver_id, str(approval.approver_id))
                review["note"] = approval.note
                review["decided_at"] = _iso(approval.created_at)
            documents.append(
                {
                    "finding_id": str(f.id),
                    "stem": payload.get("stem"),
                    "role": m.get("role"),
                    "scope": m.get("scope"),
                    "keep_ranges": m.get("keep_ranges"),
                    "source": {
                        "url": source.get("url"),
                        "sha256": source.get("sha256"),
                        "byte_size": source.get("byte_size"),
                        "page_count": source.get("page_count"),
                    },
                    "retained": {
                        "sha256": package.get("retained_sha256"),
                        "page_count": package.get("retained_page_count"),
                        "files": files,
                    },
                    "review": review,
                    "timings": timings,
                    "limitations": list(package.get("limitations") or []),
                }
            )
        outcome = t.outcome or {}
        companies.append(
            {
                "id": t.slug,
                "name": t.company,
                "market": t.market,
                "domicile_country": t.domicile_country,
                "sector": t.sector,
                "category": t.category,
                "expected_form": t.expected_form,
                "status": t.status,
                "outcome_status": outcome.get("status"),
                "reporting_period": {
                    "label": outcome.get("reporting_period_label"),
                    "start": outcome.get("period_start"),
                    "end": outcome.get("period_end"),
                },
                "publication": {
                    "date": outcome.get("publication_date"),
                    "basis": outcome.get("publication_basis"),
                },
                "disclosure_type": outcome.get("disclosure_type"),
                "framework": outcome.get("framework"),
                "language": outcome.get("language"),
                "fy2025_gap": outcome.get("fy2025_gap"),
                "substitution_note": outcome.get("substitution_note"),
                "run_id": str(t.run_id) if t.run_id else None,
                "attempts": t.attempts or 0,
                "history": list(t.history or []),
                "documents": documents,
            }
        )
        packaging_rows.append(
            {"target": t.slug, "wall_seconds": round(pack_seconds, 3), "ocr_pages": ocr_pages}
        )

    counts = collection_counts(targets)["by_status"]
    manifest = {
        "schema_version": "1.0",
        "collection_run": c.run_slug,
        "name": c.name,
        "generated_at_utc": generated.isoformat(),
        "purpose": (
            f"FY{settings.get('fiscal_year')} sustainability disclosure collection for the "
            "Kith Climate disclosure repository"
        ),
        "sample": {
            "groups": list(MARKETS),
            **{m: sum(1 for t in targets if t.market == m) for m in MARKETS},
            "selection_note": "Targets as listed in target_register.json, in register order.",
        },
        "outcome": {
            "targets_total": len(targets),
            "written": counts.get("written", 0),
            "completed": counts.get("completed", 0),
            "gap": counts.get("gap", 0),
            "failed": counts.get("failed", 0) + counts.get("write_failed", 0),
            "rejected": counts.get("rejected", 0),
            "retained_pdf_documents": retained_docs,
            "retained_pdf_pages": retained_pages,
        },
        "completeness_definition": COMPLETENESS_DEFINITION,
        "companies": companies,
        "additional_artifacts": [],
        "execution": "execution.json",
        "exception_log": "exception_log.json",
        "integrity": "SHA256SUMS.txt",
    }

    run_rows = []
    total_cost = total_reported = summed_wall = 0.0
    for t in targets:
        run = runs.get(t.run_id) if t.run_id else None
        wall = _seconds_between(run.started_at, run.finished_at) if run else None
        cost = target_cost(t, run)
        reported = float(run.reported_cost_usd) if run and run.reported_cost_usd is not None else None
        total_cost += cost
        total_reported += reported or 0.0
        summed_wall += wall or 0.0
        run_rows.append(
            {
                "target": t.slug,
                "run_id": str(run.id) if run else None,
                "status": run.status if run else None,
                "model_used": run.model_used if run else None,
                "iterations": run.iterations if run else None,
                "cost_usd": round(cost, 6),
                "current_run_cost_usd": round(run_cost(run), 6),
                "reported_cost_usd": reported,
                "wall_seconds": wall,
                "attempts": t.attempts or 0,
            }
        )
    execution = {
        "collection_run": c.run_slug,
        "started_at_utc": _iso(_aware(c.started_at)),
        "finalized_at_utc": generated.isoformat(),
        "measured_wall_seconds": _seconds_between(c.started_at, generated),
        "targets": len(targets),
        "runs": run_rows,
        "totals": {
            "cost_usd": round(total_cost, 6),
            "reported_cost_usd": round(total_reported, 6),
            "run_wall_seconds_summed": round(summed_wall, 3),
        },
        "packaging": packaging_rows,
        "concurrency": settings.get("concurrency"),
        "model_override": settings.get("model_override"),
        "max_iterations": settings.get("max_iterations"),
        "tret_version": __version__,
        "packaging_version": PACKAGING_VERSION,
        "timing_notes": ["Phases overlap; summed run wall seconds exceed elapsed time."],
        "usage_note": (
            "cost_usd is catalog-priced from token counts; reported_cost_usd is "
            "provider-reported where available"
        ),
    }

    exceptions = []
    for t in targets:
        outcome = t.outcome or {}
        if t.status in DONE_TARGET_STATUSES and outcome.get("status") != "substituted":
            continue
        exceptions.append(
            {
                "slug": t.slug,
                "company": t.company,
                "market": t.market,
                "status": t.status,
                "error": summarise_run_error(t.error),
                "outcome_status": outcome.get("status"),
                "outcome_summary": outcome.get("summary"),
                "blocked_urls": outcome.get("blocked_urls") or [],
                "candidate_urls": outcome.get("candidate_urls") or [],
                "substitution_note": outcome.get("substitution_note"),
                "attempts": t.attempts or 0,
            }
        )
    exception_log = {"collection_run": c.run_slug, "generated_at_utc": generated.isoformat(), "targets": exceptions}

    target_register = {
        "collection_run": c.run_slug,
        "fiscal_year": settings.get("fiscal_year"),
        "first_batch_size": settings.get("first_batch_size"),
        "warnings": list(c.warnings or []),
        "targets": [
            {
                "position": t.position,
                "slug": t.slug,
                "company": t.company,
                "market": t.market,
                "domicile_country": t.domicile_country,
                "sector": t.sector,
                "category": t.category,
                "expected_form": t.expected_form,
                "fiscal_year": t.fiscal_year,
                "known_urls": list(t.known_urls or []),
                "notes": t.notes,
            }
            for t in targets
        ],
    }

    records = {
        "manifest": manifest,
        "execution": execution,
        "exception_log": exception_log,
        "target_register": target_register,
        "results_summary": _results_summary(c, targets, maps_by_run, generated),
    }
    # Pilot style: every written package file plus the other five record files
    # exactly as `record_files` serializes them, so the sums verify the upload.
    lines = [
        (w.get("filename") or "", w.get("sha256") or "")
        for t in targets
        for w in written_files_latest(t)
    ]
    for name, (data, _) in _serialize_records(records).items():
        lines.append((name, hashlib.sha256(data).hexdigest()))
    records["sha256sums"] = "".join(f"{sha}  {name}\n" for name, sha in sorted(lines))
    return records


def _serialize_records(records: dict) -> dict[str, tuple[bytes, str]]:
    """The five record files other than SHA256SUMS.txt, as uploaded."""

    def as_json(value) -> bytes:
        return json.dumps(value, indent=2, ensure_ascii=False).encode("utf-8")

    return {
        "manifest.json": (as_json(records["manifest"]), "application/json"),
        "execution.json": (as_json(records["execution"]), "application/json"),
        "exception_log.json": (as_json(records["exception_log"]), "application/json"),
        "target_register.json": (as_json(records["target_register"]), "application/json"),
        "results_summary.md": (records["results_summary"].encode("utf-8"), "text/markdown; charset=utf-8"),
    }


def record_files(records: dict) -> dict[str, tuple[bytes, str]]:
    """All six record files, in upload order (SHA256SUMS.txt last)."""
    files = _serialize_records(records)
    files["SHA256SUMS.txt"] = (records["sha256sums"].encode("utf-8"), "text/plain; charset=utf-8")
    return {name: files[name] for name in RECORD_FILES}


def _results_summary(
    c: Collection,
    targets: list[CollectionTarget],
    maps_by_run: dict[uuid.UUID, list[Finding]],
    generated: datetime,
) -> str:
    lines = [
        f"# {c.name}",
        "",
        f"Collection run `{c.run_slug}`, generated {generated.strftime('%Y-%m-%d %H:%M UTC')}.",
        "",
        f"Completeness: {COMPLETENESS_DEFINITION}.",
        "",
    ]
    counts = collection_counts(targets)
    for market in MARKETS:
        in_market = [t for t in targets if t.market == market]
        lines += [f"## {market} ({len(in_market)})", ""]
        if not in_market:
            lines += ["No targets.", ""]
            continue
        lines += ["| Company | Status | Retained pages | Gap / note |", "|---|---|---:|---|"]
        for t in in_market:
            maps = _relevant_maps(maps_by_run.get(t.run_id, []) if t.run_id else [])
            pages = sum(
                int((_package_of(f) or {}).get("retained_page_count") or 0)
                for f in maps
                if f.status == "approved" and (_package_of(f) or {}).get("status") == "packaged"
            )
            outcome = t.outcome or {}
            note = str(summarise_run_error(t.error) or outcome.get("substitution_note") or "")[:200]
            if not note and outcome.get("status") in ("gap", "blocked") and outcome.get("summary"):
                # A gap with no error only explains itself through the outcome.
                note = f"{outcome['status']}: {outcome['summary']}"
                blocked = outcome.get("blocked_urls")
                if isinstance(blocked, list) and blocked:
                    note += f" ({len(blocked)} URL(s) blocked)"
                note = note[:200]
            note = note or ("FY gap" if outcome.get("fy2025_gap") else "")
            note = note.replace("|", "/").replace("\n", " ")
            lines.append(f"| {t.company.replace('|', '/')} | {t.status} | {pages or ''} | {note} |")
        lines.append("")
    by_status = counts["by_status"]
    lines += [
        "## Totals",
        "",
        f"- Targets: {counts['total']}",
        *[f"- {s}: {n}" for s, n in by_status.items() if n],
        "",
    ]
    return "\n".join(lines)


async def finalize(db: AsyncSession, c: Collection) -> None:
    """Write the six run-level records to `<write_root>/<run_slug>/`, replacing
    any earlier finalize's files. Allowed once nothing is executing, packaging
    or waiting for a decision; a finalized collection may be finalized again
    (after a retry-write, say)."""
    _require(c, ("gate", "review", "completed", "paused", "finalized"), "finalized")
    targets = await load_targets(db, [c.id])
    busy = [t for t in targets if t.status in ("running", "pending", "packaging")]
    if busy:
        raise HTTPException(
            409,
            f"{len(busy)} target(s) are still running, pending or packaging; finalize when they settle",
        )
    maps = await load_maps(db, targets)
    drafts = sum(1 for fs in maps.values() for f in fs if f.status == "draft")
    if drafts:
        raise HTTPException(
            409, f"{drafts} package maps are still awaiting a decision; approve or reject them first"
        )
    now = _now()
    records = await build_records(db, c, finalized_at=now)
    settings = c.settings or {}
    root = f"{settings.get('write_root') or 'runs'}/{c.run_slug}"
    files: list[dict] = []
    error: str | None = None
    for name, (data, content_type) in record_files(records).items():
        try:
            result = await connections_service.upload_connected_file(
                db,
                workspace_id=c.workspace_id,
                target_slug=settings.get("target_slug"),
                filename=f"{root}/{name}",
                data=data,
                content_type=content_type,
                actor_user_id=c.created_by,
                conflict="replace",
            )
        except (connections_service.ConnectionWriteError, connections_service.ConnectionUnavailable) as e:
            error = f"{name}: {e.reason}"
            break
        except Exception as e:  # noqa: BLE001 - recorded, finalize can be retried
            log.exception("finalize upload failed for %s", name)
            error = f"{name}: {type(e).__name__}: {e}"
            break
        files.append({"name": name, "web_url": result.web_url, "sha256": hashlib.sha256(data).hexdigest()})
    c.finalize = {"written_at": now.isoformat(), "files": files, "error": error}
    if error is None:
        c.status = "finalized"
        c.finalized_at = now
    await db.commit()
