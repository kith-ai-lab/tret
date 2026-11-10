"""The disclosure profile: the Kith Climate disclosure collection.

One register row per target company; each target's run proposes `package_map`
findings (what to keep of which source document), an approver decides them,
packaging produces the retained files, and approved packages are uploaded to
the SharePoint write target. This module holds everything specific to that:
the register vocabulary and parser, the collector prompt, the target status
rules (`derive_target_status`), the package upload, and the run-level records
`finalize` writes. Moved verbatim from `services/collections.py` (2026-11-05);
the orchestration is `tret/batches/core.py`.

Test patch point: `_approvals` is resolved through `tret.services.collections`
at call time (`_compat()`), because the existing tests patch it there.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import re
import unicodedata
import uuid
from datetime import datetime
from typing import Any, Iterable, Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tret import __version__
from tret.batches.core import (
    DONE_TARGET_STATUSES,
    MAX_TARGETS,
    SUCCESS_RUN_STATUSES,
    TARGET_SLUG_RE,
    WRITE_PENDING,
    _aware,
    _compat,
    _iso,
    _now,
    clamp_concurrency,
    collection_counts,
    load_review_findings,
    load_runs,
    load_targets,
    run_cost,
    summarise_run_error,
    target_cost,
    written_files_latest,
)
from tret.batches.profiles import RegisterError, register_profile
from tret.batches.rubrics import EvaluationIn
from tret.config import get_settings
from tret.db.models import (
    Approval,
    Batch,
    BatchTarget,
    Document,
    Finding,
    Harness,
    Run,
    User,
)
from tret.services import connections as connections_service
from tret.services.packaging import PACKAGING_VERSION, ROLES
from tret.storage import document_bytes
from tret.services.workspace import COLLECTION_HARNESS_NAME

log = logging.getLogger("tret.collections")

# ── vocabularies (the API contract's) ────────────────────────────────────────
MARKETS = ("US", "UK", "EU")
CATEGORIES = ("financial", "industrial", "other")
EXPECTED_FORMS = ("standalone", "embedded", "unknown")
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
MAX_KNOWN_URLS = 20
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
    profile: Literal["disclosure"] = "disclosure"
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
    # Optional batch evaluation (tret/batches/evaluation.py); absent = off.
    evaluation: EvaluationIn | None = None



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
def doctrine_text(fy: Any) -> str:
    """The five numbered doctrine points of the collection prompt (also what the
    batch evaluator's evidence pack tells the judge the run was held to)."""
    return f"""1. Collect the FY{fy} sustainability disclosure where it is published. If only an earlier period is available, collect the latest available period; report fy2025_gap=true when the period collected is not FY{fy}, with the period actually collected. Never switch to a later year because a live reporting hub has moved on.
2. Prefer issuer sources, then official regulatory or report repositories (SEC EDGAR, FCA National Storage Mechanism, ESEF portals). A third-party copy is a last resort: report it as a substitution with the reason.
3. Retain the actual disclosure (a standalone sustainability report, or the sustainability section of an annual or management report), the related assurance statement, substantive data appendices, and material explicitly incorporated by reference. General financial statements stay out. A marketing summary is not a disclosure. Distinguish an ESRS sustainability statement from voluntary reporting and record the issuer's stated framework without inferring mandatory status.
4. Page ranges come only from pages you have read in the captured document; never infer them. For an embedded disclosure, map the physical PDF pages of the sustainability section, its assurance and incorporated sections, and exclude the rest.
5. Record dates separately: reporting period start and end, publication date with its basis, assurance date. Unknown stays unknown."""


def build_prompt(collection: Batch, target: BatchTarget, total: int) -> str:
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
{doctrine_text(fy)}

PROCEDURE
a. Open the known URLs, or search with a few precise queries ("{company} sustainability report {fy} pdf", "{company} annual report {fy} sustainability statement"). Do not loop on searches: if the document cannot be reached, report it.
b. capture_source the primary PDF, and separate assurance or appendix PDFs if they exist.
c. read_document to confirm company, period and page layout before mapping. In a long report, locate the sustainability statement, its assurance report and any appendix with search_documents using the section titles from the contents page (each hit names its page and chunk), then read_document with that hit's chunk_ordinal and read forward from there. Never page through a long document from the start and never guess chunk ordinals; the page ranges you map must come from pages you read this way.
d. propose_package_map for each document to retain. Every stem must be unique within this target: use "{slug}" for the primary disclosure, "{slug}-assurance", "{slug}-appendix", and for each further document "{slug}-container-<short-topic>" (for example "{slug}-container-methodology"); never reuse a stem. role is one of {roles}: {role_guidance}. Request ocr="auto" when pages look scanned.
e. Finish by calling report_collection_outcome exactly once with status captured, substituted, gap or blocked and the metadata you established. Call it even when you found nothing; it is how this target is scored.

Do not call propose_connected_write: the collection run writes approved packages itself. There is no user in this run; do not ask questions. Stop after report_collection_outcome."""


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


# ══════════════════════════════════════════════════════════════════════════
# serializer pieces
# ══════════════════════════════════════════════════════════════════════════
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


# ══════════════════════════════════════════════════════════════════════════
# write step
# ══════════════════════════════════════════════════════════════════════════
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


def _all_written(t: BatchTarget, maps: list[Finding]) -> bool:
    written = {w.get("document_id") for w in (t.written_files or [])}
    return all(d.get("document_id") in written for _, d in _package_documents(maps))


async def _write_packages(
    db: AsyncSession, c: Batch, t: BatchTarget, maps: list[Finding]
) -> tuple[str, str | None]:
    """Upload every approved package file not yet uploaded, replacing whatever
    is at its path (the collection owns `<write_root>/<run>/packages/`).
    ("written", None) when all are; ("write_failed", reason) at the first
    failure, keeping the files already written. Each upload is attributed to the
    map's approver."""
    settings = c.settings or {}
    root = f"{settings.get('write_root') or 'runs'}/{c.run_slug}"
    cap = get_settings().egress_capture_max_bytes
    approvals = await _compat()._approvals(db, [f.id for f in maps])
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


async def _document_bytes(db: AsyncSession, c: Batch, entry: dict) -> bytes:
    try:
        doc_id = uuid.UUID(str(entry.get("document_id")))
    except ValueError as e:
        raise ValueError("the package names no valid document") from e
    doc = await db.get(Document, doc_id)
    if doc is None or doc.project_id != c.project_id or not doc.storage_path:
        raise ValueError("the package document no longer exists")
    data = await document_bytes(doc)
    expected = entry.get("sha256")
    if expected and hashlib.sha256(data).hexdigest() != expected:
        raise ValueError("the stored package file no longer matches its recorded sha256")
    return data


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
        raw = (await document_bytes(doc)).decode("utf-8")
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
    db: AsyncSession, c: Batch, *, finalized_at: datetime | None = None
) -> dict:
    """Everything `finalize` writes, as data: manifest, execution, exception
    log, register copy, SHA256SUMS text and a markdown results summary."""
    generated = finalized_at or _now()
    targets = await load_targets(db, [c.id])
    runs = await load_runs(db, targets)
    maps_by_run = await load_review_findings(db, PROFILE, targets)
    all_maps = [f for fs in maps_by_run.values() for f in fs]
    approvals = await _compat()._approvals(db, [f.id for f in all_maps])
    approver_ids = {a.approver_id for a in approvals.values()}
    approvers: dict[uuid.UUID, str] = {}
    if approver_ids:
        for u in (await db.execute(select(User).where(User.id.in_(list(approver_ids))))).scalars():
            approvers[u.id] = u.display_name
    settings = c.settings or {}
    from tret.batches import evaluation as evaluation_service

    scored = await evaluation_service.records_context(db, c)  # None without a scored pass

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
                **({"evaluation": scored.by_target.get(t.id)} if scored is not None else {}),
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
        **({"evaluation": scored.manifest} if scored is not None else {}),
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
        "results_summary": _results_summary(c, targets, maps_by_run, generated, scored),
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
    c: Batch,
    targets: list[BatchTarget],
    maps_by_run: dict[uuid.UUID, list[Finding]],
    generated: datetime,
    scored: Any = None,
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
        if scored is None:
            lines += ["| Company | Status | Retained pages | Gap / note |", "|---|---|---:|---|"]
        else:
            lines += ["| Company | Status | Retained pages | Score | Gap / note |", "|---|---|---:|---:|---|"]
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
            if scored is None:
                lines.append(f"| {t.company.replace('|', '/')} | {t.status} | {pages or ''} | {note} |")
            else:
                quality = (scored.by_target.get(t.id) or {}).get("quality")
                score = "" if quality is None else f"{quality:.0f}"
                lines.append(f"| {t.company.replace('|', '/')} | {t.status} | {pages or ''} | {score} | {note} |")
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


# ══════════════════════════════════════════════════════════════════════════
# evaluation evidence
# ══════════════════════════════════════════════════════════════════════════
EXCERPT_HEAD_CHARS = 1200
EXCERPT_BOUNDARY_CHARS = 400
TEXT_ROLES = ("layout_text", "raw_text", "ocr_text")
CROSS_CHECK_TEXT = (
    "Other runs of the same target list delivered this company's FY{fy} disclosure, judged "
    "acceptable (listed below). Use this to test the gap's reason, not to assume it. You cannot "
    "open those runs' files: judge from each source URL (the issuer's own site, a third-party "
    "copy or an archive) and the status and period shown. If the document is on the issuer's own "
    "site and this run's stated reason does not explain why it could not get it, the reason is "
    "wrong: score gap_honesty at most 2 and flag gap_reason_wrong. If the other runs only had a "
    "third-party or archive copy, a specific, true reason with the routes tried listed can still "
    "score well; judge whether the doctrine required this run to try that route."
)


async def _document_text(db: AsyncSession, document_id: Any) -> tuple[Document | None, str | None]:
    try:
        doc = await db.get(Document, uuid.UUID(str(document_id)))
    except (ValueError, TypeError):
        return None, None
    if doc is None:
        return None, None
    if doc.extracted_text is not None:
        return doc, doc.extracted_text
    if (doc.content_type or "").startswith("text/") or (doc.filename or "").endswith((".md", ".txt")):
        try:
            return doc, (await document_bytes(doc)).decode("utf-8", errors="replace")
        except (OSError, FileNotFoundError):
            return doc, None
    return doc, None


def _page_excerpt(text: str, page: Any, chars: int) -> str | None:
    """The first `chars` characters after a `## Page N` marker, or None."""
    try:
        n = int(page)
    except (TypeError, ValueError):
        return None
    match = re.search(rf"^## Page {n}\b[^\n]*\n", text, flags=re.MULTILINE)
    if match is None:
        return None
    return " ".join(text[match.end() : match.end() + chars].split())


async def build_evidence(
    db: AsyncSession,
    batch: Batch,
    target: BatchTarget,
    run: Run | None,
    findings: list[Finding],
    rubric: dict | None = None,
    *,
    cross_check: list[dict] | None = None,
) -> tuple[str, list[uuid.UUID]]:
    """The blind evidence pack for one disclosure target (see
    `tret/batches/evidence.py`) and the documents to attach to the evaluator run:
    each map's source document and its retained-text package document. For a gap
    target, `cross_check` (`scoring.delivered_elsewhere`) adds what other runs of the
    same target list delivered, so the judge can test the gap's reason."""
    from tret.batches import evidence, scoring
    from tret.batches.rubrics import default_rubric, rubric_text

    rubric = rubric or default_rubric("disclosure")
    row_class = scoring.target_class(target)
    fy = target.fiscal_year
    urls = "; ".join(target.known_urls or []) or "none"
    task = "\n".join(
        [
            f"- Company: {target.company}",
            f"- Market: {target.market}; domicile: {target.domicile_country or 'not recorded'}; "
            f"sector: {target.sector or 'not recorded'}; category: {target.category or 'not recorded'}",
            f"- Expected form: {target.expected_form}",
            f"- Fiscal year wanted: FY{fy}",
            f"- Known URLs: {urls}",
            f"- Notes: {target.notes or 'none'}",
            "",
            f"The run was held to this doctrine:\n{doctrine_text(fy)}",
        ]
    )
    error = evidence.clean_error(summarise_run_error(target.error), run) if target.error else "none"
    result = "\n".join(
        [
            f"Target status: {target.status} (class: {row_class})",
            f"Error: {error}",
            "Outcome reported by the run:",
            evidence.render_outcome(target.outcome),
        ]
    )

    attached: list[tuple[Document, str]] = []
    seen_docs: set[uuid.UUID] = set()
    excerpts: list[str] = []
    map_blocks: list[str] = []
    for f in _relevant_maps(findings):
        payload = f.payload or {}
        m = payload.get("map") or {}
        source = payload.get("source") or {}
        package = _package_of(f) or {}
        source_doc, source_text = await _document_text(db, source.get("document_id"))
        meta = (source_doc.meta or {}) if source_doc is not None else {}
        lines = [
            f"### Map {payload.get('stem')}",
            f"- role: {m.get('role')}; scope: {m.get('scope')}; decision: {f.status}",
            f"- source file: {source.get('filename') or (source_doc.filename if source_doc else None)}; "
            f"url: {meta.get('url') or source.get('url')}; "
            f"sha256: {(source_doc.sha256 if source_doc else None) or source.get('sha256')}; "
            f"source pages: {source.get('page_count')}",
            f"- keep ranges (page numbers): {json.dumps(m.get('keep_ranges'))}",
            f"- packaging: {package.get('status') or 'not packaged'}; "
            f"retained pages: {package.get('retained_page_count')}",
        ]
        if m.get("review_note"):
            lines.append(f"- proposer's note: {evidence.clip(m['review_note'], 400)}")
        if package.get("limitations"):
            lines.append(f"- limitations: {evidence.clip('; '.join(map(str, package['limitations'])), 500)}")
        record = await _package_record(db, package) if package.get("status") == "packaged" else None
        verification = (record or {}).get("verification") or {}
        if verification.get("checked"):
            lines.append(f"- verification: {evidence.clip('; '.join(map(str, verification['checked'])), 500)}")
        map_blocks.append("\n".join(lines))

        if source_doc is not None and source_doc.id not in seen_docs:
            seen_docs.add(source_doc.id)
            attached.append((source_doc, f"source of {payload.get('stem')}"))
        retained_doc = retained_text = None
        for role in TEXT_ROLES:
            entry = next((d for d in package.get("documents") or [] if d.get("role") == role), None)
            if entry is not None:
                retained_doc, retained_text = await _document_text(db, entry.get("document_id"))
                if retained_doc is not None:
                    break
        if retained_doc is not None and retained_doc.id not in seen_docs:
            seen_docs.add(retained_doc.id)
            attached.append((retained_doc, f"retained text of {payload.get('stem')}"))
        if retained_text:
            excerpts.append(
                f"### Retained text of {payload.get('stem')} (first {EXCERPT_HEAD_CHARS} characters)\n"
                + " ".join(retained_text[: EXCERPT_HEAD_CHARS * 2].split())[:EXCERPT_HEAD_CHARS]
            )
        if source_text:
            for start_page, end_page in [r for r in (m.get("keep_ranges") or []) if isinstance(r, (list, tuple)) and len(r) == 2]:
                for label, page in (("first retained page", start_page), ("last retained page", end_page)):
                    snippet = _page_excerpt(source_text, page, EXCERPT_BOUNDARY_CHARS)
                    if snippet:
                        excerpts.append(f"### {payload.get('stem')}: {label} (page {page})\n{snippet}")
    attached = attached[: evidence.MAX_ATTACHED_DOCUMENTS]

    cross_section: list[tuple[str, str]] = []
    if row_class == "gap" and cross_check:
        lines = [CROSS_CHECK_TEXT.format(fy=fy)]
        for item in cross_check:
            when = (item.get("finished_at") or "")[:10] or "date unknown"
            status = item.get("status") or "not recorded"
            period = evidence.clip(evidence.sanitize(item.get("period") or ""), 80) or "not recorded"
            urls = "; ".join(evidence.clip(evidence.scrub(u, run), 200) for u in item.get("source_urls") or [])
            lines.append(
                f"- delivered {when}, quality {float(item['quality']):.0f} of 100; "
                f"status: {status}; period: {period}; source: {urls or 'not recorded'}"
            )
        cross_section = [("Cross-check: other runs of this target list", "\n".join(lines))]

    sections = [
        ("Rubric", rubric_text(rubric, row_class)),
        ("Task", task),
        ("Result", result),
        *cross_section,
        ("Page maps", "\n\n".join(map_blocks) or "(the run proposed no page map)"),
        ("Tool-call trail", evidence.run_digest(run) if run is not None else "(no run)"),
        ("Attached documents", evidence.attached_documents(attached)),
        ("Excerpts", "\n\n".join(excerpts) or "(none)"),
    ]
    return evidence.assemble(sections), [d.id for d, _ in attached]


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


class DisclosureProfile:
    slug = "disclosure"
    review_schema = "package_map"
    no_harness_error = (
        f"This workspace has no '{COLLECTION_HARNESS_NAME}' harness; it is seeded at boot "
        "while the research egress class is on."
    )
    missing_harness_message = (
        f"No '{COLLECTION_HARNESS_NAME}' harness in this workspace; launching stopped."
    )

    def review_schema_for(self, batch: Any) -> str | None:
        return self.review_schema

    async def settle_outcome(
        self, db: AsyncSession, batch: Batch, target: BatchTarget, run: Run, findings: list[Finding]
    ) -> dict | None:
        """The outcome comes from the run's `report_collection_outcome` call."""
        return None

    async def parse_targets(self, body: CreateCollectionBody) -> tuple[list[dict], list[str]]:
        write_root = body.write_root.strip().strip("/")
        if not WRITE_ROOT_RE.match(write_root):
            raise RegisterError(
                "write_root must be one or two folder names (letters, digits, '_' or '-')"
            )
        rows, warnings = parse_register(
            [t.model_dump() for t in body.register_rows] if body.register_rows is not None else None,
            body.register_csv,
            default_fiscal_year=body.fiscal_year,
        )
        for row in rows:
            row["label"] = row["company"]
            row["task_input"] = {}  # the typed columns hold the register row
        return rows, warnings

    async def validate_settings(
        self, db: AsyncSession, workspace_id: uuid.UUID, body: CreateCollectionBody
    ) -> dict:
        try:
            allowed = [t.slug for t in await connections_service.list_write_targets(db, workspace_id)]
        except connections_service.ConnectionUnavailable as e:
            raise HTTPException(422, f"No usable Microsoft 365 connection: {e.reason}") from e
        if body.target_slug not in allowed:
            names = ", ".join(allowed) if allowed else "none are configured"
            raise HTTPException(
                422, f"target_slug '{body.target_slug}' is not a write target; allowed: {names}"
            )
        return {
            "target_slug": body.target_slug,
            "write_root": body.write_root.strip().strip("/"),
            "concurrency": clamp_concurrency(body.concurrency),
            "first_batch_size": body.first_batch_size,
            "fiscal_year": body.fiscal_year,
            "auto_write": body.auto_write,
            "model_override": (body.model_override or "").strip() or None,
            "max_iterations": body.max_iterations,
        }

    async def harness_for(self, db: AsyncSession, batch: Batch) -> Harness | None:
        if batch.harness_id is not None:
            pinned = await db.get(Harness, batch.harness_id)
            if (
                pinned is not None
                and not pinned.is_archived
                and pinned.workspace_id == batch.workspace_id
            ):
                return pinned
        return await _collector_harness(db, batch.workspace_id)

    def task_input(self, batch: Batch, target: BatchTarget, total: int) -> dict:
        return {
            "message": build_prompt(batch, target, total),
            "_label": f"collect:{target.slug}",
        }

    def derive_status(self, *args, **kwargs) -> tuple[str, str | None]:
        return derive_target_status(*args, **kwargs)

    async def write_outputs(
        self, db: AsyncSession, batch: Batch, target: BatchTarget, findings: list[Finding]
    ) -> tuple[str, str | None]:
        return await _write_packages(db, batch, target, findings)

    def all_written(self, target: BatchTarget, findings: list[Finding]) -> bool:
        return _all_written(target, findings)

    async def after_approve(self, db: AsyncSession, finding: Finding) -> None:
        """Nothing to do: packaging an approved map is finding-level behaviour
        (`api/findings.py`), whether or not the finding belongs to a batch."""

    def target_extra(self, target: BatchTarget, run: Run | None, findings: list[Finding]) -> dict:
        return {
            "company": target.company,
            "market": target.market,
            "domicile_country": target.domicile_country,
            "sector": target.sector,
            "category": target.category,
            "expected_form": target.expected_form,
            "fiscal_year": target.fiscal_year,
            "known_urls": list(target.known_urls or []),
            "notes": target.notes,
            "packages": package_summaries(findings),
        }

    def counts_extra(self, targets: list[BatchTarget]) -> dict:
        by_market = {m: {"total": 0, "done": 0, "gap": 0, "failed": 0} for m in MARKETS}
        for t in targets:
            m = by_market.setdefault(t.market, {"total": 0, "done": 0, "gap": 0, "failed": 0})
            m["total"] += 1
            if t.status in DONE_TARGET_STATUSES:
                m["done"] += 1
            elif t.status == "gap":
                m["gap"] += 1
            elif t.status in ("failed", "write_failed"):
                m["failed"] += 1
        return {"by_market": by_market}

    async def evaluation_evidence(
        self,
        db: AsyncSession,
        batch: Batch,
        target: BatchTarget,
        run: Run | None,
        findings: list[Finding],
        *,
        rubric: dict | None = None,
        cross_check: list[dict] | None = None,
    ) -> tuple[str, list[uuid.UUID]]:
        return await build_evidence(db, batch, target, run, findings, rubric, cross_check=cross_check)

    def settings_keys(self) -> tuple[str, ...]:
        return (
            "target_slug",
            "write_root",
            "concurrency",
            "first_batch_size",
            "fiscal_year",
            "auto_write",
            "model_override",
            "max_iterations",
            "evaluation",
        )

    async def records(self, db: AsyncSession, batch: Batch, *, finalized_at: datetime | None = None) -> dict:
        return await build_records(db, batch, finalized_at=finalized_at)

    def record_files(self, records: dict) -> dict[str, tuple[bytes, str]]:
        return record_files(records)

    def finalize_blockers(self, targets: list[BatchTarget], findings_by_run: dict) -> list[str]:
        drafts = sum(1 for fs in findings_by_run.values() for f in fs if f.status == "draft")
        if drafts:
            return [f"{drafts} package maps are still awaiting a decision; approve or reject them first"]
        return []


PROFILE = DisclosureProfile()
register_profile(PROFILE)
