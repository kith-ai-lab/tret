"""Deterministic packaging of a human-reviewed page subset of a source PDF.

Kith Climate collects public sustainability disclosures, and the disclosure is
often a few dozen pages inside a 300-page annual report. A reviewer decides which
*physical* pages form the package; this module retains exactly those pages and
records where each one came from. It never infers a range. Ported from Diego's
reference `collect.py` (pilot-2026-10-29), keeping its rules and its wording so a
package produced here is comparable with one produced there.

Design rules that are easy to lose in a refactor:

* **The reviewed map is the only authority.** `validate_map` enforces the shape of
  the review (increasing, disjoint, in bounds; an embedded report may not quietly
  retain every page). Nothing here "improves" a range.
* **Nothing is published until everything verified.** All output is written to a
  staging directory beside `out_dir` and moved in only after the page count, the
  per-page `extract_text()` equality and (for a whole-original package) byte
  equality all hold. A failure leaves nothing behind, and existing files are
  never overwritten — a package is evidence, and evidence that can be replaced
  in place stops being evidence.
* **Encrypted sources get one attempt at the empty password.** Issuers routinely
  ship PDFs with an encryption dictionary that opens with no credential. Anything
  needing a real password is refused; guessing passwords is not something this
  service does.
* **Parser repairs are evidence.** pypdf logs a warning when it repairs a broken
  xref or a wrong pointer. Those are captured with the phase they happened in and
  carried into the stats and the record, because a clean-looking package built
  from a repaired source should say so.
* **Poppler is preferred, not required.** `pdftotext -layout/-raw` gives the best
  page-referenced text. Without it the pypdf fallback still produces a complete
  package, but says so in the markdown header and the record: layout fidelity is
  lower, and the two text files carry the same text.

`package_pdf` is synchronous and CPU-bound; call it from a worker thread.
"""
from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import pypdf
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject, NameObject

log = logging.getLogger("tret.packaging")

PACKAGING_VERSION = "1.0.0"

SCOPES = ("standalone", "embedded")
ROLES = (
    "primary_disclosure",
    "assurance",
    "data_appendix",
    "methodology",
    "embedded_statement_and_assurance",
    "incorporated_context",
    "selected_metric_assertion_and_assurance",
    "other",
)

_STEM_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*")
# The stem becomes seven filenames; 100 keeps `{stem}.extraction_stats.json` far
# from the 255-byte filename limit on every filesystem we ship on.
MAX_STEM_LENGTH = 100
_PASSWORD_POLICY = "empty string only; no password guessing"

# Wall-clock bound on one Poppler run. A pathological page can make pdftotext
# spin; the caller is a worker thread, and a thread that never returns is a
# leaked slot.
_PDFTOTEXT_TIMEOUT_SECONDS = 300
# What of Poppler's stderr is kept in the stats. A hostile or badly broken PDF can
# make it emit a warning per glyph; the stats file is for people to read.
MAX_POPPLER_STDERR_BYTES = 64 * 1024

DEFAULT_DEADLINE_SECONDS = 300
DEFAULT_MAX_RSS_BYTES = 600 * 1024 * 1024

_REPAIR_WARNING_RE = re.compile(
    r"wrong pointing|incorrect|repair|rebuild|reconstruct|xref|not defined|invalid|ignoring", re.I
)

_FILE_SUFFIX = {
    "retained_pdf": ".pdf",
    "layout_text": ".layout.md",
    "raw_text": ".raw.md",
    "page_map": ".page_map.json",
    "links": ".links.json",
    "extraction_stats": ".extraction_stats.json",
    "package_record": ".package.json",
}
_CONTENT_TYPE = {
    "retained_pdf": "application/pdf",
    "layout_text": "text/markdown; charset=utf-8",
    "raw_text": "text/markdown; charset=utf-8",
    "page_map": "application/json",
    "links": "application/json",
    "extraction_stats": "application/json",
    "package_record": "application/json",
}

_VERIFICATION_NOT_ESTABLISHED = (
    "reading order, table accuracy, chart reconstruction, completeness of the reviewed "
    "scope or visual layout fidelity"
)


class PackageMapError(ValueError):
    """The reviewed map is invalid: bad ranges, scope rules, bad ids."""


class PackagingError(RuntimeError):
    """The source could not be packaged: encrypted, unreadable, or a check failed."""


@dataclass(frozen=True)
class PackageMap:
    title: str
    role: str
    scope: str
    # Inclusive 1-based physical page ranges. None only for standalone, meaning the
    # whole original, copied byte for byte.
    keep_ranges: tuple[tuple[int, int], ...] | None
    review_note: str = ""
    # Passthrough records {originating_page, cited_component, resolved_range,
    # status, note}; carried into the record, checked for shape only.
    incorporations: tuple[dict, ...] = ()
    # Passthrough {range, reason}.
    exclusions: tuple[dict, ...] = ()


@dataclass(frozen=True)
class SourceInfo:
    page_count: int
    sha256: str
    pdf_state: dict
    parser_warnings: list[dict]
    page_labels: list[str]


@dataclass(frozen=True)
class PackageFile:
    role: str  # retained_pdf | layout_text | raw_text | page_map | links | extraction_stats | package_record
    path: Path
    filename: str
    content_type: str
    sha256: str
    byte_size: int


@dataclass(frozen=True)
class PackageOutput:
    files: tuple[PackageFile, ...]
    source_sha256: str
    retained_sha256: str
    source_page_count: int
    retained_page_count: int
    full_original_retained: bool
    text_mode: str  # "pdftotext" when Poppler ran, "pypdf" for the fallback
    poppler_version: str | None
    wall_seconds: float
    parser_warning_count: int
    stats: dict
    record: dict


# -- small helpers -----------------------------------------------------------


def _utc() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


@contextlib.contextmanager
def _parser_warning_capture(warnings: list[dict], phase: str) -> Iterator[None]:
    """Keep source parser repairs in the audit trail, labelled with the phase.

    The handler sits on the process-wide `pypdf` logger, so it filters on the
    calling thread: two packages running in two worker threads must not each
    inherit the other's source warnings.
    """
    me = threading.get_ident()

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.thread != me:
                return
            message = record.getMessage()
            category = (
                "source_parser_repair_warning"
                if _REPAIR_WARNING_RE.search(message)
                else "pdf_parser_warning"
            )
            warnings.append(
                {
                    "phase": phase,
                    "level": record.levelname,
                    "logger": record.name,
                    "category": category,
                    "message": message,
                }
            )
            log.warning("[%s; %s] %s", category, phase, message)

    logger = logging.getLogger("pypdf")
    handler = Capture(level=logging.WARNING)
    logger.addHandler(handler)
    try:
        yield
    finally:
        logger.removeHandler(handler)


def _open_pdf(path: Path, warnings: list[dict], phase: str) -> tuple[PdfReader, dict]:
    with _parser_warning_capture(warnings, phase):
        try:
            reader = PdfReader(str(path), strict=False)
            encrypted = bool(reader.is_encrypted)
            opened_with_empty_password = False
            if encrypted:
                # Public issuer PDFs can carry an encryption dictionary yet open with
                # the empty user password. No other value is tried.
                if not reader.decrypt(""):
                    raise PackagingError("PDF requires a non-empty password; cannot open it")
                opened_with_empty_password = True
            # Touching the page tree here surfaces a broken file now, with the
            # phase's warnings still being captured, instead of mid-packaging.
            _ = len(reader.pages)
        except PackagingError:
            raise
        except Exception as exc:  # pypdf raises a wide family for damaged files
            raise PackagingError(f"PDF could not be parsed: {exc}") from exc
    return reader, {
        "is_encrypted": encrypted,
        "opened_with_empty_password": opened_with_empty_password,
        "password_attempt_policy": _PASSWORD_POLICY,
    }


def _page_labels(reader: PdfReader) -> list[str]:
    try:
        return [str(label) for label in reader.page_labels]
    except Exception as exc:
        # Labels are provenance, not content; a malformed label tree falls back to
        # the physical numbers rather than blocking the package.
        log.warning("page labels unreadable, using physical numbers: %s", exc)
        return [str(i) for i in range(1, len(reader.pages) + 1)]


def source_info(path: Path) -> SourceInfo:
    """Page count, hash, encryption state, parser warnings and page labels."""
    path = Path(path)
    if not path.is_file():
        raise PackagingError(f"source file not found: {path.name}")
    warnings: list[dict] = []
    reader, state = _open_pdf(path, warnings, "source_open")
    with _parser_warning_capture(warnings, "source_page_labels"):
        labels = _page_labels(reader)
    return SourceInfo(
        page_count=len(reader.pages),
        sha256=_digest(path),
        pdf_state=state,
        parser_warnings=warnings,
        page_labels=labels,
    )


# -- map validation ----------------------------------------------------------


def _check_stem(stem: Any) -> None:
    if not isinstance(stem, str) or not _STEM_RE.fullmatch(stem):
        raise PackageMapError(f"Unsafe or missing stem: {stem!r}")
    if len(stem) > MAX_STEM_LENGTH:
        raise PackageMapError(f"stem is longer than {MAX_STEM_LENGTH} characters")


def _names_for(stem: str) -> dict[str, str]:
    return {role: stem + suffix for role, suffix in _FILE_SUFFIX.items()}


def _check_pair(pair: Any, page_count: int, what: str) -> tuple[int, int]:
    if not isinstance(pair, (list, tuple)) or len(pair) != 2 or any(type(v) is not int for v in pair):
        raise PackageMapError(f"{what} must be a [first, last] integer pair")
    a, b = pair
    if a < 1 or b < a or b > page_count:
        raise PackageMapError(f"Out-of-bounds range {list(pair)} in {what}, source has {page_count} pages")
    return a, b


def validate_map(mapping: PackageMap, page_count: int) -> list[int]:
    """Return the ordered 1-based physical pages the map selects, or raise PackageMapError."""
    if not isinstance(mapping.title, str) or not mapping.title.strip():
        raise PackageMapError("title is required")
    if mapping.role not in ROLES:
        raise PackageMapError(f"role must be one of {', '.join(ROLES)}; got {mapping.role!r}")
    if mapping.scope not in SCOPES:
        raise PackageMapError("Every document needs scope=standalone or embedded")
    if not isinstance(mapping.review_note, str):
        raise PackageMapError("review_note must be a string")
    if type(page_count) is not int or page_count < 1:
        raise PackageMapError("source has no pages")

    ranges = mapping.keep_ranges
    if ranges is None:
        if mapping.scope == "embedded":
            raise PackageMapError("Embedded annual reports require explicit reviewed keep_ranges")
        pages = list(range(1, page_count + 1))
    else:
        if not isinstance(ranges, (list, tuple)) or not ranges:
            raise PackageMapError("keep_ranges must be a nonempty sequence or None")
        pages = []
        for pair in ranges:
            a, b = _check_pair(pair, page_count, "keep_ranges")
            pages.extend(range(a, b + 1))
        if pages != sorted(set(pages)):
            raise PackageMapError("Ranges must be increasing, disjoint and non-duplicated")
        if mapping.scope == "embedded" and len(pages) == page_count:
            raise PackageMapError("An embedded annual report cannot silently retain every page")

    for rec in mapping.incorporations:
        if not isinstance(rec, dict):
            raise PackageMapError("incorporations entries must be objects")
        if rec.get("resolved_range") is not None:
            _check_pair(rec["resolved_range"], page_count, "incorporations.resolved_range")
    for rec in mapping.exclusions:
        if not isinstance(rec, dict):
            raise PackageMapError("exclusions entries must be objects")
        if rec.get("range") is not None:
            _check_pair(rec["range"], page_count, "exclusions.range")
    try:
        json.dumps([list(mapping.incorporations), list(mapping.exclusions)])
    except (TypeError, ValueError) as exc:
        raise PackageMapError(f"incorporations/exclusions must be JSON-serializable: {exc}") from exc
    return pages


# -- links and subset --------------------------------------------------------


def _resolve_dest(reader: PdfReader, dest: Any, ref_pages: dict, named: Any) -> tuple[int | None, list]:
    """Source page number (1-based) a link destination points at, and its view tail.

    A destination naming a page the source does not have (a bad integer, a stale
    page reference) is unresolved, not a "page not retained": reporting a bogus
    page number as a real, merely-excluded target would send a reviewer looking
    for a page that does not exist.
    """
    page, tail = _resolve_dest_raw(reader, dest, ref_pages, named)
    if page is not None and not (type(page) is int and 1 <= page <= len(ref_pages)):
        page = None
    return page, tail


def _resolve_dest_raw(reader: PdfReader, dest: Any, ref_pages: dict, named: Any) -> tuple[int | None, list]:
    if isinstance(dest, str):
        found = named().get(dest)
        if found is None:
            return None, []
        page = reader.get_destination_page_number(found)
        return (None if page is None else page + 1), list(found.dest_array)[1:]
    if hasattr(dest, "get_object"):
        dest = dest.get_object()
    if isinstance(dest, (list, ArrayObject)) and dest:
        target = dest[0]
        if hasattr(target, "idnum"):
            page = ref_pages.get((target.idnum, target.generation))
        elif isinstance(target, int):
            page = int(target) + 1
        else:
            page = None
        return page, list(dest)[1:]
    return None, []


def _rect(ann: Any) -> list[float] | None:
    """The annotation rectangle, or None when it is missing or not four numbers."""
    try:
        raw = ann.get("/Rect")
        raw = raw.get_object() if hasattr(raw, "get_object") else raw
        return [float(v.get_object() if hasattr(v, "get_object") else v) for v in raw]
    except (TypeError, ValueError, AttributeError):
        return None


def _add_page_without_annotations(writer: PdfWriter, page: Any) -> None:
    """Copy a page into `writer` with no annotations, and without pypdf looking at them.

    `add_page` walks the source page's annotations to queue link fix-ups (which we
    do not use, because we re-add links ourselves, remapped). That walk assumes
    well-formed links and raises on a non-dictionary /A, and it logs a spurious
    "Annotation sizes differ" warning for every page with links. Hiding the
    annotation array from it for the duration of the call avoids both; the
    original is put back immediately.
    """
    saved = page.raw_get("/Annots") if "/Annots" in page else None
    page[NameObject("/Annots")] = ArrayObject()
    try:
        writer.add_page(page, excluded_keys=["/Annots"])
    finally:
        if saved is None:
            del page["/Annots"]
        else:
            page[NameObject("/Annots")] = saved


def _links_and_subset(
    reader: PdfReader, pages: list[int], labels: list[str], dest: Path, full: bool
) -> list[dict]:
    """Audit every link on the selected pages and, unless the whole original is
    kept, write the subset PDF with internal links remapped to retained pages.
    """
    ref_pages = {
        (p.indirect_reference.idnum, p.indirect_reference.generation): i + 1
        for i, p in enumerate(reader.pages)
    }
    mapping = {p: i + 1 for i, p in enumerate(pages)}

    # `named_destinations` re-walks the name tree on every access; resolve it once,
    # and only if a link actually uses a name.
    named_cache: list[dict] = []

    def named() -> dict:
        if not named_cache:
            named_cache.append(reader.named_destinations)
        return named_cache[0]

    writer: PdfWriter | None = None
    if not full:
        writer = PdfWriter()
        for p in pages:
            _add_page_without_annotations(writer, reader.pages[p - 1])
        title = (reader.metadata or {}).get("/Title") or ""
        writer.add_metadata(
            {
                "/Title": str(title),
                "/Subject": "Reviewed page extract; source page identity in accompanying page_map.json",
            }
        )
        for out_no, src_no in enumerate(pages):
            writer.set_page_label(out_no, out_no, prefix=labels[src_no - 1])

    def malformed(src_no: int, index: int | None, detail: str, rect: list[float] | None = None) -> dict:
        return {
            "source_pdf_page": src_no,
            "retained_pdf_page": mapping[src_no],
            "annotation_index": index,
            "rectangle": rect,
            "type": None,
            "status": "malformed_annotation",
            "detail": detail,
        }

    def audit_page(src_no: int) -> None:
        annots = reader.pages[src_no - 1].get("/Annots")
        annots = annots.get_object() if annots is not None else []
        if not isinstance(annots, list):
            audit.append(malformed(src_no, None, "/Annots is not an array"))
            return
        for index, ann_ref in enumerate(annots):
            ann = ann_ref.get_object() if hasattr(ann_ref, "get_object") else ann_ref
            # A null entry or a reference to an object that does not exist: nothing
            # to retain or remap, but the reviewer should know it was there.
            if not isinstance(ann, DictionaryObject):
                audit.append(malformed(src_no, index, "annotation entry is not a dictionary"))
                continue
            if ann.get("/Subtype") != "/Link":
                if writer is not None:
                    # /Parent is ignored with /P: a widget's Parent -> Kids -> P chain
                    # would otherwise drag pages outside the reviewed range into the
                    # subset through the field hierarchy.
                    copied = ann.clone(writer, ignore_fields=["/P", "/Parent"])
                    writer.add_annotation(mapping[src_no] - 1, copied)
                continue
            entry: dict[str, Any] = {
                "source_pdf_page": src_no,
                "retained_pdf_page": mapping[src_no],
                "rectangle": _rect(ann),
                "type": None,
            }
            action = ann.get("/A")
            action = action.get_object() if action else None
            if action is not None and not isinstance(action, DictionaryObject):
                audit.append(malformed(src_no, index, "link /A is not an action dictionary", entry["rectangle"]))
                continue
            dest_value = ann.get("/Dest")
            if action and action.get("/S") == "/GoTo":
                dest_value = action.get("/D")
            copied = (
                ann.clone(writer, ignore_fields=["/P", "/Dest", "/A"]) if writer is not None else None
            )
            if dest_value is not None:
                target, tail = _resolve_dest(reader, dest_value, ref_pages, named)
                entry.update(
                    type="internal",
                    target_source_pdf_page=target,
                    target_retained_pdf_page=mapping.get(target),
                )
                if target in mapping:
                    entry["status"] = "preserved" if full else "remapped"
                    if writer is not None:
                        arr = ArrayObject(
                            [writer.pages[mapping[target] - 1].indirect_reference]
                            + [v.clone(writer) if hasattr(v, "clone") else v for v in tail]
                        )
                        copied[NameObject("/Dest")] = arr
                else:
                    entry["status"] = (
                        "unresolved_source_destination" if target is None else "target_page_not_retained"
                    )
            elif action:
                typ = str(action.get("/S", "unknown"))
                entry.update(
                    type=typ,
                    uri=str(action.get("/URI", "")) or None,
                    remote_file=str(action.get("/F", "")) or None,
                    remote_destination=str(action.get("/D", "")) or None,
                    status=(
                        "external_destination_requires_separate_collection"
                        if typ in ("/URI", "/GoToR", "/Launch")
                        else "action_preserved_unverified"
                    ),
                )
                if writer is not None:
                    copied[NameObject("/A")] = action.clone(writer)
            else:
                entry.update(type="unknown", status="no_resolvable_destination")
            if writer is not None:
                # add_annotation accepts its own link builder format, not an already
                # resolved destination array. Attach first, then set our PDF array.
                resolved = copied.pop(NameObject("/Dest"), None)
                inserted = writer.add_annotation(mapping[src_no] - 1, copied)
                if resolved is not None:
                    inserted[NameObject("/Dest")] = resolved
            audit.append(entry)

    audit: list[dict] = []
    for src_no in pages:
        try:
            audit_page(src_no)
        except PackagingError:
            raise
        except Exception as exc:  # hostile or damaged annotation trees raise anything
            raise PackagingError(
                f"link audit failed on source page {src_no}: {type(exc).__name__}: {exc}"
            ) from exc
    if writer is not None:
        with dest.open("wb") as f:
            writer.write(f)
    return audit


# -- text extraction ---------------------------------------------------------


def _tool_env() -> dict[str, str]:
    # Poppler needs nothing from the caller's environment but PATH; credentials and
    # API keys in the parent's env have no business reaching a PDF parser.
    return {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")}


def _poppler_version(exe: str) -> str:
    try:
        run = subprocess.run([exe, "-v"], capture_output=True, text=True, timeout=30, env=_tool_env())
    except (OSError, subprocess.SubprocessError) as exc:
        raise PackagingError(f"pdftotext could not be run: {exc}") from exc
    # Poppler prints its banner on stderr and exits 0.
    first = ((run.stderr or run.stdout).strip().splitlines() or [""])[0]
    return first.strip()


def _pdftotext_pages(
    exe: str, pdf: Path, mode: str, count: int, timeout: float = _PDFTOTEXT_TIMEOUT_SECONDS
) -> tuple[list[str], str]:
    flag = "-layout" if mode == "layout" else "-raw"
    try:
        run = subprocess.run(
            [exe, flag, "-enc", "UTF-8", str(pdf), "-"],
            capture_output=True,
            timeout=timeout,
            env=_tool_env(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PackagingError(f"pdftotext {flag} failed: {exc}") from exc
    stderr = run.stderr.decode("utf-8", errors="replace")
    if run.returncode != 0:
        raise PackagingError(f"pdftotext {flag} exited {run.returncode}: {stderr.strip()[:300]}")
    pieces = run.stdout.decode("utf-8", errors="replace").split("\f")
    if pieces and not pieces[-1].strip():
        pieces.pop()
    if len(pieces) != count:
        raise PackagingError(f"pdftotext produced {len(pieces)} page segments for {count} pages")
    return pieces, stderr


def _capped_stderr(stderr: str) -> dict[str, Any]:
    raw = stderr.encode("utf-8")
    if len(raw) <= MAX_POPPLER_STDERR_BYTES:
        return {"poppler_stderr": stderr, "stderr_truncated": False}
    cut = raw[:MAX_POPPLER_STDERR_BYTES].decode("utf-8", errors="ignore")
    return {"poppler_stderr": cut, "stderr_truncated": True}


def _pypdf_pages(reader: PdfReader, count: int) -> list[str]:
    try:
        return [reader.pages[i].extract_text() or "" for i in range(count)]
    except Exception as exc:
        raise PackagingError(f"pypdf text extraction failed: {exc}") from exc


def _write_markdown(
    path: Path,
    title: str,
    texts: list[str],
    page_map: list[dict],
    source_sha: str,
    retained_sha: str,
    mode: str,
    text_mode: str,
) -> None:
    # Code blocks preserve spacing and content without assigning invented semantic headings.
    extraction = f"pdftotext -{mode}" if text_mode == "pdftotext" else "pypdf extract_text"
    out = [
        f"# {title}\n",
        f"Extraction mode: {extraction}. Original SHA-256: `{source_sha}`. Retained SHA-256: `{retained_sha}`.\n",
        "Source text is transcribed as extracted, without paraphrase. Page headings are collection "
        "metadata. Code blocks preserve extraction whitespace; table, diagram, hierarchy and "
        "reading-order fidelity are not guaranteed. Consult the PDF for visual evidence.\n",
    ]
    if text_mode == "pypdf":
        out.append(
            "Limitation: Poppler (pdftotext) was unavailable, so this text comes from pypdf "
            "extract_text; layout fidelity is lower, and the layout and raw files carry the same text.\n"
        )
    for txt, pm in zip(texts, page_map, strict=True):
        # A fence longer than any backtick run inside the page keeps the text from
        # closing its own block.
        fence = "`" * max(3, max([len(x) for x in re.findall(r"`+", txt)] + [0]) + 1)
        out.append(
            f"## Original PDF page {pm['source_pdf_page']} "
            f"(source label {json.dumps(pm['source_page_label'], ensure_ascii=False)}); "
            f"retained page {pm['retained_pdf_page']}\n\n{fence}text\n{txt}"
        )
        if not txt.endswith("\n"):
            out.append("\n")
        out.append(f"{fence}\n\n")
    path.write_text("".join(out), encoding="utf-8")


# -- publishing --------------------------------------------------------------


def _create_exclusive(src: Path, target: Path) -> None:
    """Put `src` at `target` without ever replacing something already there.

    `os.replace` would silently overwrite a file that appeared after the
    up-front check. A hard link fails with FileExistsError instead, and the
    staged name is then dropped. Where links are not available (a staging dir on
    another filesystem, a mount that forbids them) fall back to an
    exclusive-create copy, which refuses an existing target just as firmly.
    """
    try:
        os.link(src, target)
    except FileExistsError:
        raise
    except OSError:
        with src.open("rb") as r, target.open("xb") as w:
            shutil.copyfileobj(r, w)
    src.unlink(missing_ok=True)


def _publish(stage: Path, out_dir: Path, names: list[str]) -> None:
    """Move staged files into `out_dir`, refusing to overwrite, undoing on failure.

    Rollback removes only what this call created: a file that already existed is
    the reason we are failing and is never touched.
    """
    made_dir = not out_dir.exists()
    out_dir.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []
    try:
        for name in names:
            target = out_dir / name
            try:
                _create_exclusive(stage / name, target)
            except FileExistsError:
                raise PackagingError(f"refusing to overwrite existing file: {name}") from None
            created.append(target)
    except BaseException:
        for target in created:
            target.unlink(missing_ok=True)
        if made_dir:
            with contextlib.suppress(OSError):
                out_dir.rmdir()
        raise


# -- the service -------------------------------------------------------------


def package_pdf(
    source_path: Path,
    mapping: PackageMap,
    out_dir: Path,
    *,
    stem: str,
    source_meta: dict | None = None,
    deadline_seconds: float | None = None,
    _stage_dir: Path | None = None,
) -> PackageOutput:
    """Package the reviewed pages of `source_path` into `out_dir`, in this process.

    Raises PackageMapError for an invalid map or stem, PackagingError when the
    source cannot be packaged or a verification fails. On any failure `out_dir`
    gains nothing and no staging directory is left behind.

    This runs in the calling thread and cannot be interrupted from outside; a web
    handler should call `run_packaging`, which puts it in a child process with a
    hard deadline and memory ceiling. `deadline_seconds`, when given, only bounds
    each Poppler run to what is left of it (the child's parent enforces the rest).
    `_stage_dir` is for `run_packaging`: a staging directory the parent created, so
    that the parent can remove it if this process is killed.
    """
    started = _utc()
    clock = time.monotonic()
    source_path = Path(source_path)
    out_dir = Path(out_dir)

    _check_stem(stem)
    try:
        json.dumps(source_meta)
    except (TypeError, ValueError) as exc:
        raise PackageMapError(f"source_meta must be JSON-serializable: {exc}") from exc
    if not source_path.is_file():
        raise PackagingError(f"source file not found: {source_path.name}")

    names = _names_for(stem)
    taken = [n for n in names.values() if (out_dir / n).exists()]
    if taken:
        raise PackagingError(f"refusing to overwrite existing package files: {', '.join(sorted(taken))}")

    parser_warnings: list[dict] = []
    reader, source_state = _open_pdf(source_path, parser_warnings, "source_open")
    source_count = len(reader.pages)
    with _parser_warning_capture(parser_warnings, "source_page_selection"):
        pages = validate_map(mapping, source_count)
        labels = _page_labels(reader)
    source_sha = _digest(source_path)
    full = len(pages) == source_count

    out_dir.parent.mkdir(parents=True, exist_ok=True)
    stage = _stage_dir or Path(tempfile.mkdtemp(prefix=".packaging-stage-", dir=out_dir.parent))
    try:
        dst = stage / names["retained_pdf"]
        if full:
            shutil.copyfile(source_path, dst)
        with _parser_warning_capture(parser_warnings, "source_retention"):
            audit = _links_and_subset(reader, pages, labels, dst, full)

        retained, retained_state = _open_pdf(dst, parser_warnings, "retained_pdf_open")
        if len(retained.pages) != len(pages):
            raise PackagingError("output page count mismatch")
        # Meaningful content check independent of the selected range count.
        with _parser_warning_capture(parser_warnings, "retained_content_verification"):
            for idx, p in enumerate(pages):
                try:
                    same = reader.pages[p - 1].extract_text() == retained.pages[idx].extract_text()
                except Exception as exc:
                    raise PackagingError(f"text extraction failed verifying source page {p}: {exc}") from exc
                if not same:
                    raise PackagingError(f"text changed on source page {p}")
        retained_sha = _digest(dst)
        if full and retained_sha != source_sha:
            raise PackagingError("Byte-copy integrity failed")

        page_map = [
            {"retained_pdf_page": i + 1, "source_pdf_page": p, "source_page_label": labels[p - 1]}
            for i, p in enumerate(pages)
        ]
        _write_json(
            stage / names["page_map"],
            {
                "source_filename": source_path.name,
                "source_page_count": source_count,
                "retained_page_count": len(pages),
                "source_sha256": source_sha,
                "retained_sha256": retained_sha,
                "pages": page_map,
            },
        )
        _write_json(stage / names["links"], audit)

        exe = shutil.which("pdftotext")
        text_mode = "pdftotext" if exe else "pypdf"
        poppler_version = _poppler_version(exe) if exe else None
        stats: dict[str, Any] = {
            "source_pdf_state": source_state,
            "retained_pdf_state": retained_state,
            "parser_warnings": parser_warnings,
            "parser_warning_count": len(parser_warnings),
            "text_mode": text_mode,
            "modes": {},
            "internal_links_remapped": sum(a["status"] == "remapped" for a in audit),
            "unretained_internal_targets": sum(a["status"] == "target_page_not_retained" for a in audit),
            "malformed_annotations": sum(a["status"] == "malformed_annotation" for a in audit),
            "unresolved_links": sum(
                a["status"] in ("unresolved_source_destination", "no_resolvable_destination") for a in audit
            ),
            "external_links": sum(a["type"] in ("/URI", "/GoToR", "/Launch") for a in audit),
            "content_verification": (
                "exact pypdf extract_text equality for each selected source page; "
                "byte equality for full originals"
            ),
            "structure_fidelity": "unverified; visual source PDF controls",
        }
        for mode in ("layout", "raw"):
            if exe:
                budget = _PDFTOTEXT_TIMEOUT_SECONDS
                if deadline_seconds is not None:
                    budget = min(budget, deadline_seconds - (time.monotonic() - clock))
                    if budget <= 0:
                        raise PackagingError(f"packaging exceeded {deadline_seconds:g} s")
                texts, tool_stderr = _pdftotext_pages(exe, dst, mode, len(pages), timeout=budget)
            else:
                with _parser_warning_capture(parser_warnings, "retained_text_extraction"):
                    texts, tool_stderr = _pypdf_pages(retained, len(pages)), ""
            _write_markdown(
                stage / names[f"{mode}_text"],
                mapping.title,
                texts,
                page_map,
                source_sha,
                retained_sha,
                mode,
                text_mode,
            )
            stats["modes"][mode] = {
                "characters": sum(len(s) for s in texts),
                "empty_pages": [page_map[i]["source_pdf_page"] for i, s in enumerate(texts) if not s.strip()],
                "replacement_characters": sum(s.count("�") for s in texts),
                **_capped_stderr(tool_stderr),
            }
        # Warnings can arrive during text extraction; refresh the count last.
        stats["parser_warning_count"] = len(parser_warnings)
        _write_json(stage / names["extraction_stats"], stats)

        limitations = [
            "PDF page content is preserved; PDF tagging, bookmarks, embedded attachments, forms, and "
            "signature validity are not certified in page extracts.",
            (
                "Markdown uses verbatim Poppler text in page-referenced code blocks. It is not a "
                "semantic table or chart reconstruction."
                if text_mode == "pdftotext"
                else "Markdown uses verbatim pypdf text in page-referenced code blocks. It is not a "
                "semantic table or chart reconstruction."
            ),
            "External linked documents are audited, not automatically crawled.",
        ]
        if text_mode == "pypdf":
            limitations.append(
                "Poppler (pdftotext) was unavailable; text was extracted with pypdf for both the layout "
                "and raw files, so layout fidelity is lower and the two files carry the same text."
            )
        checked = [
            "output page count equals the number of selected pages",
            "exact pypdf extract_text equality for each selected source page against its retained page",
            "SHA-256 of the source and of the retained PDF",
        ]
        if full:
            checked.append("byte equality between the source and the retained PDF (whole original retained)")
        if text_mode == "pdftotext":
            checked.append("pdftotext produced exactly one text segment per retained page, in both modes")
        verification = {
            "checked": checked,
            "establishes": "content extraction and page retention",
            "does_not_establish": _VERIFICATION_NOT_ESTABLISHED,
        }

        completed = _utc()
        wall = round(time.monotonic() - clock, 3)
        record: dict[str, Any] = {
            "packaging_version": PACKAGING_VERSION,
            "title": mapping.title,
            "role": mapping.role,
            "scope": mapping.scope,
            "keep_ranges": [list(r) for r in mapping.keep_ranges] if mapping.keep_ranges is not None else None,
            "review_note": mapping.review_note,
            "incorporations": [dict(x) for x in mapping.incorporations],
            "exclusions": [dict(x) for x in mapping.exclusions],
            "source_meta": source_meta if source_meta is not None else {},
            "source_sha256": source_sha,
            "retained_sha256": retained_sha,
            "source_page_count": source_count,
            "retained_page_count": len(pages),
            "full_original_retained": full,
            "source_pdf_state": source_state,
            "retained_pdf_state": retained_state,
            "parser_warnings": parser_warnings,
            "text_mode": text_mode,
            "poppler_version": poppler_version,
            "runtime": {"python": sys.version.split()[0], "pypdf": pypdf.__version__},
            "started_utc": started,
            "completed_utc": completed,
            "wall_seconds": wall,
            "artifacts": dict(names),
            "limitations": limitations,
            "verification": verification,
        }
        _write_json(stage / names["package_record"], record)

        _publish(stage, out_dir, list(names.values()))
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    files = tuple(
        PackageFile(
            role=role,
            path=out_dir / name,
            filename=name,
            content_type=_CONTENT_TYPE[role],
            sha256=_digest(out_dir / name),
            byte_size=(out_dir / name).stat().st_size,
        )
        for role, name in names.items()
    )
    return PackageOutput(
        files=files,
        source_sha256=source_sha,
        retained_sha256=retained_sha,
        source_page_count=source_count,
        retained_page_count=len(pages),
        full_original_retained=full,
        text_mode=text_mode,
        poppler_version=poppler_version,
        wall_seconds=wall,
        parser_warning_count=len(parser_warnings),
        stats=stats,
        record=record,
    )


# -- isolated execution --------------------------------------------------------

_WORKER_PATH = Path(__file__).with_name("packaging_worker.py")


def _output_from_dict(data: dict) -> PackageOutput:
    files = tuple(PackageFile(**{**f, "path": Path(f["path"])}) for f in data["files"])
    return PackageOutput(**{**data, "files": files})


def _kill_group(proc: subprocess.Popen) -> None:
    # The child runs in its own session so pdftotext, which it spawns, dies with it.
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError):
        proc.kill()


def run_packaging(
    source_path: Path,
    mapping: PackageMap,
    out_dir: Path,
    *,
    stem: str,
    source_meta: dict | None = None,
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
    max_rss_bytes: int = DEFAULT_MAX_RSS_BYTES,
) -> PackageOutput:
    """`package_pdf` in a child process with a hard deadline and memory ceiling.

    This is what request handlers call. `asyncio.wait_for(to_thread(package_pdf))`
    stops waiting on a timeout but not working: the thread goes on to publish files
    after the caller has cleaned up, and a hostile PDF can exhaust the API
    process's memory. Here the child is killed at `deadline_seconds`; because
    publishing is the last thing the child does, a kill leaves `out_dir` as it was
    (anything half-published is removed here) and the staging directory is removed
    by this process, which created it.

    The child gets RLIMIT_AS = `max_rss_bytes` and RLIMIT_CPU = deadline + 30 s on
    POSIX where the OS allows it (Linux does; macOS refuses RLIMIT_AS, so there only
    the deadline applies). The ceiling is on address space, not resident memory, so
    it is deliberately a little generous for the interpreter itself.

    Raises what `package_pdf` raises, plus PackagingError("packaging exceeded N s")
    at the deadline and PackagingError when the child dies (memory, CPU limit).
    """
    source_path = Path(source_path)
    out_dir = Path(out_dir)
    _check_stem(stem)
    if deadline_seconds <= 0:
        raise PackagingError(f"packaging exceeded {deadline_seconds:g} s")
    try:
        payload = json.dumps(
            {
                "source_path": str(source_path),
                "mapping": dataclasses.asdict(mapping),
                "out_dir": str(out_dir),
                "stem": stem,
                "source_meta": source_meta,
                "deadline_seconds": deadline_seconds,
                "max_rss_bytes": max_rss_bytes,
            }
        )
    except (TypeError, ValueError) as exc:
        raise PackageMapError(f"map and source_meta must be JSON-serializable: {exc}") from exc

    names = list(_names_for(stem).values())
    taken = [n for n in names if (out_dir / n).exists()]
    if taken:
        raise PackagingError(f"refusing to overwrite existing package files: {', '.join(sorted(taken))}")
    out_dir_existed = out_dir.exists()

    out_dir.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".packaging-stage-", dir=out_dir.parent))
    child_lost = False  # killed or died: it may have been mid-publish
    try:
        job = json.loads(payload)
        job["stage_dir"] = str(stage)
        proc = subprocess.Popen(
            [sys.executable, "-I", str(_WORKER_PATH)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_tool_env(),
            start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(json.dumps(job).encode("utf-8"), timeout=deadline_seconds)
        except subprocess.TimeoutExpired:
            child_lost = True
            _kill_group(proc)
            proc.communicate()
            raise PackagingError(f"packaging exceeded {deadline_seconds:g} s") from None
        except BaseException:
            child_lost = True
            _kill_group(proc)
            proc.communicate()
            raise
        tail = stderr.decode("utf-8", errors="replace").strip()[-500:]
        if proc.returncode != 0:
            child_lost = True
            if proc.returncode < 0:
                try:
                    how = f"signal {signal.Signals(-proc.returncode).name}"
                except ValueError:
                    how = f"signal {-proc.returncode}"
            else:
                how = f"exit {proc.returncode}"
            raise PackagingError(f"packaging worker died ({how}): {tail}")
        try:
            result = json.loads(stdout.decode("utf-8"))
        except ValueError:
            raise PackagingError(f"packaging worker returned no result: {tail}") from None
        if not result.get("ok"):
            error = PackageMapError if result.get("kind") == "map" else PackagingError
            raise error(result.get("message", "packaging failed"))
        return _output_from_dict(result["output"])
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        if child_lost:
            # A child that reports its own failure has already rolled back. One that
            # was killed may have died between two publish steps; none of these
            # names existed when we started, so whatever is there now is its debris.
            for name in names:
                (out_dir / name).unlink(missing_ok=True)
            if not out_dir_existed:
                with contextlib.suppress(OSError):
                    out_dir.rmdir()
