"""Stage 1 normaliser: one captured report in, one clean Markdown file out.

A `Document` fetched by a disclosure-collection run is turned into a single Markdown
file with real Markdown tables, plus quality metrics and a quarantine status, kept in
the blob store (never in Postgres) and recorded in `disclosure_reports`. Everything
here is behind `TRET_DISCLOSURE_NORMALISER` (off by default); the service never runs
unless an operator starts it (`tret disclosure normalise`).

PDF, two passes:

1. `pdftotext -layout` over EVERY page, page by page, with no character cap (unlike
   `Document.extracted_text`, which stops at 2,000,000 characters, often before the
   sustainability statement). Cheap: about 0.03 s a page.
2. docling (the optional `normalise` extra) only on the pages that look like tables,
   OCR off, 2 threads, in chunks of `TRET_NORMALISER_CHUNK_PAGES` pages. Every chunk
   runs in its own child process (`normalise_worker`) under a wall-clock timeout and an
   RSS guard, because docling peaks at 2.1-2.4 GB per 3-page chunk (3.0-3.3 GB for 5) and
   needs 9-12 GB for a whole document (validation-artifacts/eval-phase0/converters/report.md). A chunk that fails,
   times out or is killed leaves its pages as the pass-1 text and says so in the metrics.

Page markers are `## Page N` with PHYSICAL page numbers (the convention of
`Document.extracted_text`), so page maps built on either line up.

Table-page rule (`table_page_stats` / `is_table_page`), tuned on the 20-PDF benchmark
set: a page is a table page when it holds at least TABLE_MIN_NUMERIC_TOKENS (25)
numeric tokens AND at least TABLE_MIN_ROW_LINES (4) "row lines": lines that, split on
runs of two or more spaces, have at least two purely numeric cells (`7,026   6,815`,
`44%`, `(12.3)`). Pass-1 text keeps column alignment, so this finds a table without
understanding it. At most `TRET_NORMALISER_MAX_TABLE_PAGES` pages go to docling, the
ones naming emissions first, then those with the most row lines; the cut is recorded
(`capped`). A page that names emissions (scope 1/2/3, tCO2e, GHG, ...) needs only
EMISSIONS_MIN_ROW_LINES (2) row lines, because sustainability tables are small and often
share a page with prose (a two-column page of the benchmark set has two).

ESEF filings (provenance `esef_filing`) are read with `esef_filing.extract_filing_text`
in the same kind of child process; consecutive table rows become a Markdown table.
Rendering a paged ESEF filing to PDF is a possible follow-up, not done here.

Import discipline: nothing from the ORM or the server extra at module level, so
`tret disclosure normalise-file` runs on a core install plus the `normalise` extra and
the worker child stays small. Database code imports lazily.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import json
import logging
import os
import re
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.ext.asyncio import AsyncSession

    from tret.db.models import BatchTarget, DisclosureReport, Document

log = logging.getLogger("tret.normalise")

# Bump when output changes in a way a reader of old and new reports should tell apart.
# A report is stored once per (document, version), so a bump re-normalises on demand.
NORMALISER_VERSION = "1"

# source_kind values
KIND_PDF = "pdf"
KIND_ESEF_PAGED = "esef_paged"
KIND_ESEF_REFLOW = "esef_reflow"

# status values
STATUS_NORMALISED = "normalised"
STATUS_QUARANTINED = "quarantined"
STATUS_FAILED = "failed"

# Quarantine rules, checked in this order. Quarantined reports still keep their Markdown.
QUARANTINE_EMPTY = "empty"  # no pages, or no text at all
QUARANTINE_NO_TEXT_LAYER = "no_text_layer"  # scanned: needs OCR (out of scope here)
QUARANTINE_GARBLED = "garbled"  # broken font encodings
MIN_MEDIAN_CHARS_PER_PAGE = 200
MAX_FFFD_RATE = 0.01  # U+FFFD characters / characters

# Table-page heuristic (module docstring).
TABLE_MIN_NUMERIC_TOKENS = 25
TABLE_MIN_ROW_LINES = 4
# A page with fewer row lines than this is prose: alignment whitespace is collapsed.
PROSE_MAX_ROW_LINES = 1

# A docling page is kept only when its cleaned text has at least this many characters and
# at least this share of the page's pdftotext numeric tokens; otherwise the page keeps the
# pdftotext text and is listed in `table_pages_rejected`. Docling drops page furniture and
# some figure text (recall 0.67-0.82 on a few benchmark pages); below 0.6 it has lost data.
MIN_DOCLING_PAGE_CHARS = 20
MIN_DOCLING_RECALL = 0.6

PDFTOTEXT_MIN_TIMEOUT_SECONDS = 120.0
PDFTOTEXT_SECONDS_PER_MIB = 4.0
PDFTOTEXT_MAX_TIMEOUT_SECONDS = 900.0
# Only for the ESEF child: a filing is read in one go (its memory limit is the normaliser's).
ESEF_TIMEOUT_SECONDS = 600

WORKER_MODULE = "tret.services.normalise_worker"
_ENV_PASSTHROUGH = (
    "PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "HF_HOME", "HF_HUB_OFFLINE", "HF_HUB_CACHE",
    "TRANSFORMERS_OFFLINE", "TORCH_HOME", "XDG_CACHE_HOME", "DOCLING_ARTIFACTS_PATH",
)


class NormaliserDisabledError(RuntimeError):
    """`TRET_DISCLOSURE_NORMALISER` is off."""


# ── results ──────────────────────────────────────────────────────────────────
@dataclass
class NormaliseResult:
    source_kind: str
    status: str
    markdown: str
    metrics: dict[str, Any]
    quarantine_reason: str | None = None
    error: str | None = None


@dataclass
class ChunkResult:
    """What one docling child produced: physical page -> Markdown."""

    pages: dict[int, str] = field(default_factory=dict)
    ok: bool = False
    error: str | None = None
    seconds: float = 0.0
    peak_rss_mb: float = 0.0
    docling_version: str | None = None


# ── pass 1: pdftotext ────────────────────────────────────────────────────────
_VERSION_RE = re.compile(r"version\s+(\S+)")


def pdftotext_version() -> str | None:
    exe = shutil.which("pdftotext")
    if exe is None:
        return None
    try:
        run = subprocess.run([exe, "-v"], capture_output=True, timeout=10, env=_poppler_env())
    except (OSError, subprocess.SubprocessError):
        return None
    m = _VERSION_RE.search((run.stderr or run.stdout).decode("utf-8", errors="replace"))
    return m.group(1) if m else None


def _poppler_env() -> dict[str, str]:
    # Poppler needs nothing from the environment but PATH (as services/documents.py).
    return {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")}


def _pdftotext_timeout(size_bytes: int) -> float:
    scaled = size_bytes / 2**20 * PDFTOTEXT_SECONDS_PER_MIB
    return min(PDFTOTEXT_MAX_TIMEOUT_SECONDS, max(PDFTOTEXT_MIN_TIMEOUT_SECONDS, scaled))


def pdftotext_pages(path: Path) -> list[str] | None:
    """Per-page `pdftotext -layout` text of the whole file, or None when Poppler is
    missing or failed (the caller falls back to pypdf). Raises subprocess.TimeoutExpired.
    Poppler ends every page with a form feed, so n segments is n pages."""
    exe = shutil.which("pdftotext")
    if exe is None:
        return None
    timeout = _pdftotext_timeout(path.stat().st_size)
    try:
        run = subprocess.run(
            [exe, "-layout", "-enc", "UTF-8", str(path), "-"],
            capture_output=True, timeout=timeout, env=_poppler_env(),
        )
    except subprocess.TimeoutExpired:
        raise
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("pdftotext could not run: %s", e)
        return None
    if run.returncode != 0:
        log.warning("pdftotext exited %s", run.returncode)
        return None
    pieces = run.stdout.decode("utf-8", errors="replace").split("\f")
    if pieces and not pieces[-1].strip():
        pieces.pop()
    return [p.rstrip() for p in pieces]


PDFINFO_TIMEOUT_SECONDS = 30.0
_PAGES_RE = re.compile(r"^Pages:\s+(\d+)", re.MULTILINE)


def pdfinfo_pages(path: Path) -> int | None:
    """Page count from Poppler's `pdfinfo` under a timeout, or None (not installed, damaged,
    encrypted, timed out). pypdf never opens an untrusted file in this process."""
    exe = shutil.which("pdfinfo")
    if exe is None:
        return None
    try:
        run = subprocess.run(
            [exe, str(path)], capture_output=True, timeout=PDFINFO_TIMEOUT_SECONDS, env=_poppler_env()
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if run.returncode != 0:
        return None
    m = _PAGES_RE.search(run.stdout.decode("utf-8", errors="replace"))
    return int(m.group(1)) if m else None


def pypdf_pages_guarded(path: Path, settings: Any) -> list[str]:
    """The pypdf reader, run in a guarded child (timeout and RSS limit like docling's)."""
    run = run_worker(
        ["pypdf", "--pdf", str(path)],
        timeout_seconds=_pdftotext_timeout(path.stat().st_size),
        max_rss_mb=float(settings.normaliser_max_rss_mb),
    )
    if run.error or run.payload is None:
        raise RuntimeError(f"pypdf could not read the PDF: {run.error or 'no result'}")
    return [str(t).rstrip() for t in run.payload.get("pages") or []]


def read_pages(path: Path, settings: Any = None) -> tuple[list[str], str]:
    """(page texts, extractor name). Poppler when it gives the page tree's count (by
    `pdfinfo`), else pypdf in a guarded child."""
    if settings is None:
        from tret.config import get_settings

        settings = get_settings()
    pages = pdftotext_pages(path)
    if pages is not None:
        count = pdfinfo_pages(path)
        if count is None or count == len(pages):
            return pages, "pdftotext"
        log.warning("pdftotext gave %d page segments for %s pages; using pypdf", len(pages), count)
    return pypdf_pages_guarded(path, settings), "pypdf"


# ── table-page heuristic ─────────────────────────────────────────────────────
_NUMBER = re.compile(r"\d[\d.,]*\d|\d")
_SPLIT = re.compile(r"\s{2,}")
# A cell that is only a number: optional sign/currency/bracket, digits with separators,
# optional % or unit suffix of up to two letters (`%`, `x`, `pp`), optional closing bracket.
_NUMERIC_CELL = re.compile(r"[\(\-−–+€$£]?\s?\d[\d.,'/ ]*[%)]?[A-Za-z]{0,2}\)?")


def table_page_stats(text: str) -> tuple[int, int]:
    """(numeric tokens, row lines) of one page of `pdftotext -layout` text."""
    numeric = len(_NUMBER.findall(text))
    rows = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        cells = _SPLIT.split(line)
        if len(cells) >= 2 and sum(1 for c in cells if _NUMERIC_CELL.fullmatch(c)) >= 2:
            rows += 1
    return numeric, rows


# Sustainability tables are small (a dozen rows) and often sit beside prose. A page that
# names emissions gets the relaxed row-line floor below and goes first when the page cap bites.
_EMISSIONS_WORDS = re.compile(r"scope\s*[123]|tco2|co2\s?e|co\u2082|ghg|greenhouse|emissions", re.IGNORECASE)
EMISSIONS_MIN_ROW_LINES = 2


def is_table_page(text: str) -> bool:
    numeric, rows = table_page_stats(text)
    if numeric < TABLE_MIN_NUMERIC_TOKENS:
        return False
    if rows >= TABLE_MIN_ROW_LINES:
        return True
    return rows >= EMISSIONS_MIN_ROW_LINES and bool(_EMISSIONS_WORDS.search(text))


def select_table_pages(pages: list[str], max_pages: int) -> tuple[list[int], int, bool]:
    """(physical page numbers to convert, ascending; candidates before the cap; capped).
    Over the cap, emissions pages go first, then the pages with the most row lines."""
    scored = []
    for i, text in enumerate(pages, start=1):
        if is_table_page(text):
            numeric, rows = table_page_stats(text)
            scored.append((1 if _EMISSIONS_WORDS.search(text) else 0, rows, numeric, i))
    candidates = len(scored)
    capped = candidates > max_pages
    if capped:
        scored = sorted(scored, key=lambda t: (-t[0], -t[1], -t[2], t[3]))[: max(0, max_pages)]
    return sorted(t[3] for t in scored), candidates, capped


# ── markdown helpers ─────────────────────────────────────────────────────────
_TABLE_SEPARATOR = re.compile(r"^\|(?:\s*:?-{3,}:?\s*\|)+\s*$")
_BLANK_RUNS = re.compile(r"\n{3,}")


def count_markdown_tables(markdown: str) -> int:
    return sum(1 for line in markdown.splitlines() if _TABLE_SEPARATOR.match(line))


def _escape_line(line: str) -> str:
    """A body line that starts with `#` must not be read as a heading or a page marker."""
    return "\\" + line if line.startswith("#") else line


def tidy_prose_page(text: str) -> str:
    """A page with no table-like lines: drop the layout indentation and alignment runs,
    keep line breaks (a line break is the only paragraph signal pdftotext gives)."""
    lines = [re.sub(r"[ \t]{2,}", " ", ln.strip()) for ln in text.splitlines()]
    return _BLANK_RUNS.sub("\n\n", "\n".join(_escape_line(ln) for ln in lines)).strip()


def _plain_page(text: str) -> str:
    lines = [_escape_line(ln.rstrip()) for ln in text.splitlines()]
    return _BLANK_RUNS.sub("\n\n", "\n".join(lines)).strip("\n")


_IMAGE_MARKER = re.compile(r"^\s*<!--\s*image\s*-->\s*$", re.MULTILINE)


_HEADING = re.compile(r"^(#{1,6})(?=\s)", re.MULTILINE)


def _demote_heading(m: re.Match) -> str:
    return "#" * min(6, len(m.group(1)) + 2)


def clean_docling_markdown(markdown: str) -> str:
    """Docling's Markdown for one page: image markers dropped, and its headings pushed
    down two levels so `## Page N` stays the only level-2 heading of the file (retrieval
    reads it as the page locator)."""
    text = _HEADING.sub(_demote_heading, _IMAGE_MARKER.sub("", markdown))
    return _BLANK_RUNS.sub("\n\n", text).strip()


UNSTRUCTURED_NOTE = "<!-- table page: layout text, table structure not recovered -->"


def assemble_pdf_markdown(
    pages: list[str], converted: dict[int, str], table_pages: set[int]
) -> str:
    """`## Page N` blocks in physical order. A converted table page carries docling's
    Markdown; a table page that was not converted keeps its layout text and a note; every
    other page is pass-1 text, with alignment whitespace collapsed only on prose pages."""
    blocks: list[str] = []
    for i, text in enumerate(pages, start=1):
        if i in converted:
            body = converted[i]  # already cleaned by the caller
        elif i in table_pages:
            body = UNSTRUCTURED_NOTE + ("\n" + _plain_page(text) if text.strip() else "")
        elif table_page_stats(text)[1] <= PROSE_MAX_ROW_LINES:
            body = tidy_prose_page(text)
        else:
            body = _plain_page(text)
        blocks.append(f"## Page {i}\n{body}" if body else f"## Page {i}")
    return "\n\n".join(blocks) + "\n"


def _numeric_recall(source: str, converted: str) -> float | None:
    src = Counter(_NUMBER.findall(source))
    if not src:
        return None
    got = Counter(_NUMBER.findall(converted))
    return sum((src & got).values()) / sum(src.values())


# ── quality metrics and quarantine ───────────────────────────────────────────
def text_metrics(page_texts: list[str]) -> dict[str, Any]:
    """The page-level quality numbers shared by every source kind."""
    sizes = [len(t.strip()) for t in page_texts]
    total = sum(len(t) for t in page_texts)
    return {
        "pages": len(page_texts),
        "pages_with_text": sum(1 for n in sizes if n > 0),
        "empty_pages": sum(1 for n in sizes if n == 0),
        "median_chars_per_page": int(statistics.median(sizes)) if sizes else 0,
        "fffd_rate": round(sum(t.count("�") for t in page_texts) / total, 6) if total else 0.0,
    }


def quarantine_reason(metrics: dict[str, Any], source_kind: str) -> str | None:
    """Why this report must not be trusted, or None. The median-characters rule is
    for page-shaped sources: an ESEF filing read as sections has small sections."""
    if not metrics.get("pages") or not metrics.get("pages_with_text"):
        return QUARANTINE_EMPTY
    if source_kind != KIND_ESEF_REFLOW and metrics.get("median_chars_per_page", 0) < MIN_MEDIAN_CHARS_PER_PAGE:
        return QUARANTINE_NO_TEXT_LAYER
    if metrics.get("fffd_rate", 0.0) > MAX_FFFD_RATE:
        return QUARANTINE_GARBLED
    return None


def _finish(source_kind: str, markdown: str, metrics: dict[str, Any]) -> NormaliseResult:
    metrics["tables_found"] = count_markdown_tables(markdown)
    reason = quarantine_reason(metrics, source_kind)
    return NormaliseResult(
        source_kind=source_kind,
        status=STATUS_QUARANTINED if reason else STATUS_NORMALISED,
        quarantine_reason=reason,
        markdown=markdown,
        metrics=metrics,
    )


# ── child processes: the docling chunks and the ESEF reader ──────────────────
def docling_version() -> str | None:
    """The installed docling version without importing it, or None when absent."""
    try:
        return importlib.metadata.version("docling")
    except importlib.metadata.PackageNotFoundError:
        return None


def docling_enabled(settings: Any) -> tuple[bool, str]:
    """(use docling, mode for the metrics): used | off | unavailable."""
    if (getattr(settings, "normaliser_docling", "auto") or "auto").strip().lower() == "off":
        return False, "off"
    if docling_version() is None:
        return False, "unavailable"
    return True, "used"


def _child_env() -> dict[str, str]:
    env = {k: os.environ[k] for k in _ENV_PASSTHROUGH if k in os.environ}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env["OMP_NUM_THREADS"] = "2"
    env["MKL_NUM_THREADS"] = "2"
    return env


def _worker_command(mode_args: list[str], out_path: str) -> list[str]:
    return [sys.executable, "-I", "-m", WORKER_MODULE, *mode_args, "--out", out_path]


def rss_mb(pid: int) -> float:
    """Resident set size of `pid` in MB (0.0 when it is gone or unreadable)."""
    try:
        with open(f"/proc/{pid}/statm") as fh:
            return int(fh.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 2**20
    except (OSError, ValueError, IndexError):
        pass
    try:
        run = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, timeout=5, text=True
        )
        return int(run.stdout.strip() or 0) / 1024
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        proc.kill()
    proc.wait()


@dataclass
class WorkerRun:
    payload: dict[str, Any] | None
    error: str | None
    seconds: float
    peak_rss_mb: float


def run_worker(
    mode_args: list[str], *, timeout_seconds: float, max_rss_mb: float, poll_seconds: float = 0.25
) -> WorkerRun:
    """Run one worker child; kill it past `timeout_seconds` or above `max_rss_mb`. The
    child writes JSON to a temp file (`--out`); its stderr tail explains a failure."""
    with tempfile.TemporaryDirectory(prefix="tret-normalise-") as tmp:
        out = os.path.join(tmp, "out.json")
        err_path = os.path.join(tmp, "stderr.txt")
        started = time.monotonic()
        peak = 0.0
        # The child's TMPDIR is this directory, so whatever it left in a temp dir (docling's
        # chunk PDF) is removed with it, even when the child was killed.
        env = _child_env()
        env["TMPDIR"] = tmp
        with open(err_path, "wb") as err_fh:
            try:
                proc = subprocess.Popen(
                    _worker_command(mode_args, out),
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err_fh,
                    env=env, start_new_session=True,
                )
            except OSError as e:
                return WorkerRun(None, f"could not start the worker: {e}", 0.0, 0.0)
            error: str | None = None
            try:
                while True:
                    try:
                        proc.wait(timeout=poll_seconds)
                        break
                    except subprocess.TimeoutExpired:
                        pass
                    peak = max(peak, rss_mb(proc.pid))
                    if peak > max_rss_mb:
                        _kill_group(proc)
                        error = f"killed: memory {peak:.0f} MB over the {max_rss_mb:.0f} MB limit"
                        break
                    if time.monotonic() - started > timeout_seconds:
                        _kill_group(proc)
                        error = f"killed: timed out after {timeout_seconds:.0f}s"
                        break
            except BaseException:
                # Ctrl-C, SIGTERM-as-exception, anything: the child has its own session and
                # would otherwise keep running (and holding 2-3 GB) after we are gone.
                _kill_group(proc)
                raise
        seconds = time.monotonic() - started
        if error is None and proc.returncode != 0:
            tail = ""
            try:
                tail = Path(err_path).read_text(errors="replace").strip()[-400:]
            except OSError:
                pass
            error = f"worker exited {proc.returncode}" + (f": {tail}" if tail else "")
        payload = None
        if error is None:
            try:
                payload = json.loads(Path(out).read_text())
            except (OSError, ValueError) as e:
                error = f"worker wrote no result: {e}"
        return WorkerRun(payload, error, seconds, peak)


def convert_chunk(pdf_path: Path, pages: list[int], settings: Any) -> ChunkResult:
    """Convert `pages` (physical, ascending) with docling in a guarded child. Tests
    replace this function; it never raises."""
    run = run_worker(
        ["docling", "--pdf", str(pdf_path), "--pages", ",".join(str(p) for p in pages)],
        timeout_seconds=float(settings.normaliser_chunk_timeout_seconds),
        max_rss_mb=float(settings.normaliser_max_rss_mb),
    )
    if run.error or run.payload is None:
        return ChunkResult(error=run.error or "no result", seconds=run.seconds, peak_rss_mb=run.peak_rss_mb)
    got = {int(k): str(v) for k, v in (run.payload.get("pages") or {}).items()}
    return ChunkResult(
        pages=got, ok=True, seconds=run.seconds, peak_rss_mb=run.peak_rss_mb,
        docling_version=run.payload.get("docling_version"),
    )


def chunk_pages(pages: list[int], size: int) -> list[list[int]]:
    size = max(1, int(size))
    return [pages[i : i + size] for i in range(0, len(pages), size)]


# ── PDF ──────────────────────────────────────────────────────────────────────
def normalise_pdf_file(path: Path, settings: Any) -> NormaliseResult:
    """Normalise one PDF on disk. Blocking; needs no database."""
    started = time.monotonic()
    try:
        pages, extractor = read_pages(path, settings)
    except subprocess.TimeoutExpired:
        return _failed(KIND_PDF, "pdftotext timed out")
    except Exception as e:  # damaged or encrypted file
        return _failed(KIND_PDF, f"could not read the PDF: {type(e).__name__}: {e}")

    metrics = text_metrics(pages)
    metrics["text_extractor"] = extractor
    metrics["pdftotext_version"] = pdftotext_version() if extractor == "pdftotext" else None
    # Pass 1 is complete and uncapped: `capped` is about the table pages only.
    table_pages, candidates, capped = select_table_pages(pages, int(settings.normaliser_max_table_pages))
    use_docling, mode = docling_enabled(settings)
    metrics.update(
        table_pages_candidates=candidates,
        table_pages_selected=len(table_pages),
        capped=capped,
        docling=mode if table_pages or not use_docling else "no_table_pages",
        docling_version=None,
        table_pages_converted=0,
        table_pages_fallback=0,
        chunks=[],
        seconds_docling=0.0,
        peak_rss_mb=0.0,
    )

    converted: dict[int, str] = {}
    low_recall: list[int] = []
    if use_docling and table_pages:
        recalls: list[float] = []
        for chunk in chunk_pages(table_pages, int(settings.normaliser_chunk_pages)):
            res = convert_chunk(path, chunk, settings)
            metrics["seconds_docling"] += res.seconds
            metrics["peak_rss_mb"] = max(metrics["peak_rss_mb"], res.peak_rss_mb)
            metrics["docling_version"] = res.docling_version or metrics["docling_version"]
            accepted: list[int] = []
            rejected: list[int] = []
            for p in chunk if res.ok else []:
                # Judge the CLEANED text: a page that was only `<!-- image -->` is empty.
                cleaned = clean_docling_markdown(res.pages.get(p, ""))
                recall = _numeric_recall(pages[p - 1], cleaned)
                if len(cleaned) < MIN_DOCLING_PAGE_CHARS or (recall is not None and recall < MIN_DOCLING_RECALL):
                    rejected.append(p)  # keeps its pdftotext text
                    continue
                converted[p] = cleaned
                accepted.append(p)
                if recall is not None:
                    recalls.append(recall)
            low_recall += rejected
            metrics["chunks"].append(
                {
                    "pages": chunk, "ok": res.ok, "converted": len(accepted), "rejected": rejected,
                    "seconds": round(res.seconds, 1), "peak_rss_mb": round(res.peak_rss_mb),
                    "error": res.error,
                }
            )
        if recalls:
            metrics["table_numeric_recall_mean"] = round(sum(recalls) / len(recalls), 4)
            metrics["table_numeric_recall_min"] = round(min(recalls), 4)
    metrics["table_pages_rejected"] = sorted(low_recall)
    metrics["table_pages_converted"] = len(converted)
    metrics["table_pages_fallback"] = len(table_pages) - len(converted)
    metrics["seconds_docling"] = round(metrics["seconds_docling"], 1)
    metrics["peak_rss_mb"] = round(metrics["peak_rss_mb"])

    markdown = assemble_pdf_markdown(pages, converted, set(table_pages))
    metrics["seconds_total"] = round(time.monotonic() - started, 1)
    return _finish(KIND_PDF, markdown, metrics)


def _failed(source_kind: str, error: str) -> NormaliseResult:
    return NormaliseResult(
        source_kind=source_kind, status=STATUS_FAILED, markdown="", metrics={}, error=error
    )


# ── ESEF ─────────────────────────────────────────────────────────────────────
def _md_cell(cell: Any) -> str:
    return " ".join(str(cell or "").replace("|", "\\|").split())


def markdown_table(rows: list[tuple]) -> list[str]:
    """Rows of cells as a Markdown table; the first row is the header."""
    width = max(len(r) for r in rows)
    out: list[str] = []
    for n, row in enumerate(rows):
        cells = [_md_cell(c) for c in row] + [""] * (width - len(row))
        out.append("| " + " | ".join(cells) + " |")
        if n == 0:
            out.append("|" + "|".join(" --- " for _ in range(width)) + "|")
    return out


def render_filing_markdown(units: list) -> str:
    """`FilingUnit`s as Markdown: the unit's own marker (`## Page N` or `## Section N:
    heading`), text lines as text, and each run of consecutive table rows as one table."""
    blocks: list[str] = []
    for unit in units:
        out: list[str] = [unit.marker]
        rows: list[tuple] = []

        def flush() -> None:
            if rows:
                out.extend(["", *markdown_table(rows), ""])
                rows.clear()

        for ln in unit.lines:
            if ln.kind == "row" and ln.cells:
                if any(str(c or "").strip() for c in ln.cells):
                    rows.append(tuple(ln.cells))
                continue
            flush()
            if ln.kind != "img" and ln.text:
                out.append(_escape_line(ln.text))
        flush()
        blocks.append(_BLANK_RUNS.sub("\n\n", "\n".join(out)).rstrip())
    return "\n\n".join(blocks) + "\n"


def normalise_esef_file(path: Path, settings: Any) -> NormaliseResult:
    """Normalise an ESEF package (.zip/.xbri) on disk, in a guarded child."""
    started = time.monotonic()
    run = run_worker(
        ["esef", "--zip", str(path)], timeout_seconds=ESEF_TIMEOUT_SECONDS,
        max_rss_mb=float(settings.normaliser_max_rss_mb),
    )
    if run.error or run.payload is None:
        return _failed(KIND_ESEF_PAGED, run.error or "no result")
    p = run.payload
    kind = KIND_ESEF_PAGED if p.get("layout") == "paged" else KIND_ESEF_REFLOW
    metrics = text_metrics(list(p.get("unit_texts") or []))
    metrics.update(
        layout=p.get("layout"),
        units_total=p.get("units_total"),
        source_text_capped=bool(p.get("source_text_capped")),
        table_pages_selected=0,
        table_pages_converted=0,
        table_pages_fallback=0,
        capped=bool(p.get("source_text_capped")),
        peak_rss_mb=round(run.peak_rss_mb),
        seconds_docling=0.0,
        seconds_total=round(time.monotonic() - started, 1),
    )
    return _finish(kind, str(p.get("markdown") or ""), metrics)


def normalise_file(path: Path, settings: Any) -> NormaliseResult:
    """Normalise a local file by its kind (.pdf, or an ESEF .zip/.xbri package)."""
    if path.suffix.lower() in (".zip", ".xbri"):
        return normalise_esef_file(path, settings)
    return normalise_pdf_file(path, settings)


# ── database side ────────────────────────────────────────────────────────────
def normaliser_enabled(settings: Any = None) -> bool:
    if settings is None:
        from tret.config import get_settings

        settings = get_settings()
    return (getattr(settings, "disclosure_normaliser", "off") or "off").strip().lower() == "on"


def document_source(document: Any) -> str | None:
    """`pdf`, `esef`, or None for a document the normaliser does not read."""
    meta = document.meta or {}
    if meta.get("provenance") == "esef_filing":
        return "esef"
    name = (document.filename or "").lower()
    if name.endswith(".pdf") or "pdf" in (document.content_type or "").lower():
        return "pdf"
    return None


async def _existing_report(session: AsyncSession, document_id: uuid.UUID) -> DisclosureReport | None:
    from sqlalchemy import select

    from tret.db.models import DisclosureReport

    return (
        await session.execute(
            select(DisclosureReport).where(
                DisclosureReport.document_id == document_id,
                DisclosureReport.normaliser_version == NORMALISER_VERSION,
            )
        )
    ).scalar_one_or_none()


def _link_target(report: DisclosureReport, target: BatchTarget | None, run_id: uuid.UUID | None) -> None:
    """Tie the report to the target that normalised it. A document two targets share (a
    reused ESEF package, a replayed capture) keeps its FIRST target; later ones are listed
    in `metrics.also_linked_targets`, never silently overwriting the link."""
    if target is None:
        report.run_id = report.run_id or run_id
        return
    if report.batch_target_id in (None, target.id):
        report.batch_target_id = target.id
        report.run_id = run_id or target.run_id or report.run_id
        return
    shared = list((report.metrics or {}).get("also_linked_targets") or [])
    if str(target.id) not in shared:
        report.metrics = {**(report.metrics or {}), "also_linked_targets": [*shared, str(target.id)]}


def _apply_result(
    report: DisclosureReport, result: NormaliseResult, key: str | None, digest: str | None
) -> None:
    metrics = dict(result.metrics)
    shared = (report.metrics or {}).get("also_linked_targets")
    if shared:
        metrics["also_linked_targets"] = shared
    report.source_kind = result.source_kind
    report.status = result.status
    report.quarantine_reason = result.quarantine_reason
    report.metrics = metrics
    report.markdown_key = key
    report.markdown_sha256 = digest
    report.markdown_chars = len(result.markdown)
    report.error = result.error


async def normalise_document(
    session: AsyncSession,
    document: Document,
    *,
    target: BatchTarget | None = None,
    run_id: uuid.UUID | None = None,
    force: bool = False,
    settings: Any = None,
) -> DisclosureReport | None:
    """Normalise `document` once per `NORMALISER_VERSION` and return its report row
    (None for a file type the normaliser does not read). A report already
    `normalised` or `quarantined` at this version is returned untouched unless
    `force`; a `failed` or `pending` one is redone. A forced rerun that fails keeps the
    previous good report and Markdown and records `metrics.last_error`. Any error while
    reading or storing the document marks that report `failed` instead of raising, so a
    batch carries on. Two processes racing to create the same row both end on one row
    (the loser updates it). Flushes, never commits."""
    from sqlalchemy.exc import IntegrityError

    from tret.config import get_settings
    from tret.db.models import DisclosureReport
    from tret.storage import document_local_path, get_store, normalised_key

    settings = settings or get_settings()
    if not normaliser_enabled(settings):
        raise NormaliserDisabledError("TRET_DISCLOSURE_NORMALISER is off")
    kind = document_source(document)
    if kind is None:
        return None
    report = await _existing_report(session, document.id)
    if report is not None and not force and report.status in (STATUS_NORMALISED, STATUS_QUARANTINED):
        _link_target(report, target, run_id)
        await session.flush()
        return report

    failed_kind = KIND_PDF if kind == "pdf" else KIND_ESEF_PAGED
    store = get_store(settings)
    try:
        path = await document_local_path(document, settings)
        async with store.pinned(path):
            result = await asyncio.to_thread(
                normalise_esef_file if kind == "esef" else normalise_pdf_file, path, settings
            )
    except FileNotFoundError:
        result = _failed(failed_kind, "the document's bytes are not in storage")
    except Exception as e:
        log.exception("normalising document %s failed", document.id)
        result = _failed(failed_kind, f"{type(e).__name__}: {e}")

    key = digest = None
    if result.markdown:
        data = result.markdown.encode("utf-8")
        key = normalised_key(document.id, NORMALISER_VERSION)
        digest = hashlib.sha256(data).hexdigest()
        try:
            await store.put(key, data, content_type="text/markdown; charset=utf-8")
        except Exception as e:
            log.exception("storing the Markdown of document %s failed", document.id)
            result = _failed(failed_kind, f"could not store the Markdown: {type(e).__name__}: {e}")
            key = digest = None

    if (
        result.status == STATUS_FAILED
        and report is not None
        and report.status in (STATUS_NORMALISED, STATUS_QUARANTINED)
    ):
        report.metrics = {
            **(report.metrics or {}),
            "last_error": result.error,
            "last_error_at": datetime.now(timezone.utc).isoformat(),
        }
        _link_target(report, target, run_id)
        await session.flush()
        return report

    if report is None:
        report = DisclosureReport(
            project_id=document.project_id, document_id=document.id,
            normaliser_version=NORMALISER_VERSION,
        )
        _link_target(report, target, run_id)
        _apply_result(report, result, key, digest)
        try:
            async with session.begin_nested():
                session.add(report)
                await session.flush()
        except IntegrityError:
            # Another process created this (document, version) row first: update that one.
            report = await _existing_report(session, document.id)
            if report is None:
                raise
            _link_target(report, target, run_id)
            _apply_result(report, result, key, digest)
    else:
        _link_target(report, target, run_id)
        _apply_result(report, result, key, digest)
    await session.flush()
    return report


async def target_documents(session: AsyncSession, target: BatchTarget) -> list[Document]:
    """The PDFs and ESEF packages a target's run read: documents it fetched
    (`meta.fetched_by_run`) UNION the ones in the run's `document_ids`. The second set is
    what a reused capture adds (`capture_filing` finding the package already stored, a
    replayed fetch), which `fetched_by_run` alone would miss."""
    from sqlalchemy import or_, select

    from tret.db.models import Batch, Document, Run

    if target.run_id is None:
        return []
    batch = await session.get(Batch, target.collection_id)
    if batch is None:
        return []
    run = await session.get(Run, target.run_id)
    ids: set[uuid.UUID] = set()
    for raw in (run.document_ids if run is not None else None) or []:
        try:
            ids.add(raw if isinstance(raw, uuid.UUID) else uuid.UUID(str(raw)))
        except ValueError:
            continue
    match = [Document.meta["fetched_by_run"].as_string() == str(target.run_id)]
    if ids:
        match.append(Document.id.in_(ids))
    rows = (
        await session.execute(
            select(Document)
            .where(Document.project_id == batch.project_id, Document.source_kind == "web", or_(*match))
            .order_by(Document.created_at, Document.id)
        )
    ).scalars().all()
    return [d for d in rows if document_source(d) is not None]


async def normalise_target(
    session: AsyncSession, target: BatchTarget, *, force: bool = False, settings: Any = None
) -> list[DisclosureReport]:
    """Normalise every PDF/ESEF document the target's run read (`target_documents`), and
    mark the one with the most Markdown as `is_primary` among the reports this target
    OWNS (a document shared with an earlier target stays linked to that one, see
    `_link_target`, and keeps its flag). A quarantined report is primary only when
    nothing else was produced. Flushes, never commits."""
    reports: list[DisclosureReport] = []
    for doc in await target_documents(session, target):
        report = await normalise_document(
            session, doc, target=target, run_id=target.run_id, force=force, settings=settings
        )
        if report is not None:
            reports.append(report)
    owned = [r for r in reports if r.batch_target_id == target.id]
    usable = [r for r in owned if r.status == STATUS_NORMALISED] or [
        r for r in owned if r.status == STATUS_QUARANTINED
    ]
    primary = max(usable, key=lambda r: r.markdown_chars, default=None)
    for r in owned:
        r.is_primary = r is primary
    await session.flush()
    return reports


async def normalise_batch(
    session_factory: Any,
    batch_id: uuid.UUID,
    *,
    limit: int | None = None,
    target_slug: str | None = None,
    only_status: list[str] | None = None,
    force: bool = False,
    settings: Any = None,
    progress: Any = None,
) -> list[dict[str, Any]]:
    """Normalise a batch's targets one at a time, committing after each. Returns one
    summary dict per target: `{slug, label, reports: [{document_id, filename, status,
    quarantine_reason, is_primary, markdown_chars, tables, seconds}]}`."""
    from sqlalchemy import select

    from tret.db.models import BatchTarget

    async with session_factory() as db:
        q = select(BatchTarget.id).where(BatchTarget.collection_id == batch_id).order_by(BatchTarget.position)
        if target_slug:
            q = q.where(BatchTarget.slug == target_slug)
        if only_status:
            q = q.where(BatchTarget.status.in_(only_status))
        target_ids = list((await db.execute(q)).scalars().all())
    if limit is not None:
        target_ids = target_ids[: max(0, limit)]

    summaries: list[dict[str, Any]] = []
    for tid in target_ids:
        async with session_factory() as db:
            target = await db.get(BatchTarget, tid)
            if target is None:
                continue
            reports = await normalise_target(db, target, force=force, settings=settings)
            await db.commit()
            names = {d.id: d.filename for d in await target_documents(db, target)}
            summary = {
                "slug": target.slug,
                "label": target.label or target.company,
                "reports": [
                    {
                        "document_id": str(r.document_id),
                        "filename": names.get(r.document_id),
                        "status": r.status,
                        "quarantine_reason": r.quarantine_reason,
                        "is_primary": r.is_primary,
                        "markdown_chars": r.markdown_chars,
                        "tables": (r.metrics or {}).get("tables_found"),
                        "seconds": (r.metrics or {}).get("seconds_total"),
                        "error": r.error,
                    }
                    for r in reports
                ],
            }
        summaries.append(summary)
        if progress is not None:
            progress(summary)
    return summaries
