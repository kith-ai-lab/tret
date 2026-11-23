"""`tret disclosure ...`: the Stage 1 normaliser (docs/disclosure-collection.md).

* `normalise --batch <id>` reads the database like `tret approvals mark-packaging`
  does (no server needed) and normalises the PDF and ESEF documents each target's run
  fetched. Needs `TRET_DISCLOSURE_NORMALISER=on`.
* `normalise-file <path> --out <dir>` runs the converter on one local file with NO
  database and writes `<stem>.md` and `<stem>.metrics.json`; for manual checks. It does
  not need the setting on.

Exit codes: 0 ok, 1 something failed or was refused, 2 usage or configuration. Imports
are lazy so `tret --help` stays light (tests/test_sdk_import_hygiene.py).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def register(sub: argparse._SubParsersAction) -> None:
    disc = sub.add_parser(
        "disclosure",
        help="Stage 1 normaliser: captured reports to clean Markdown (see docs/disclosure-collection.md)",
        description="Normalise captured disclosure reports into Markdown with real tables.",
    )
    dsub = disc.add_subparsers(dest="disclosure_command", required=True)

    norm = dsub.add_parser(
        "normalise",
        help="Normalise the reports a batch's targets fetched (needs TRET_DISCLOSURE_NORMALISER=on)",
        description="Read the database directly (no server needed) and normalise every PDF and ESEF "
        "document each target's run fetched. A report already normalised at the current version is "
        "skipped unless --force.",
    )
    norm.add_argument("--batch", required=True, metavar="BATCH_ID", help="Batch id")
    norm.add_argument("--limit", type=int, metavar="N", help="Only the first N targets (register order)")
    norm.add_argument("--target", metavar="SLUG", help="Only the target with this slug")
    norm.add_argument(
        "--only-status", action="append", metavar="STATUS",
        help="Only targets in this status, e.g. review (repeatable)",
    )
    norm.add_argument("--force", action="store_true", help="Redo reports already normalised at this version")
    norm.add_argument("--json", action="store_true", help="Print raw JSON")

    one = dsub.add_parser(
        "normalise-file",
        help="Normalise one local PDF (or ESEF .zip/.xbri) with no database; writes <stem>.md and <stem>.metrics.json",
    )
    one.add_argument("path", type=Path)
    one.add_argument("--out", type=Path, required=True, metavar="DIR", help="Output directory (created)")
    one.add_argument("--no-docling", action="store_true", help="pdftotext only; tables stay unstructured")


def run(args: argparse.Namespace) -> int:
    if args.disclosure_command == "normalise-file":
        return _normalise_file(args)
    return _normalise_batch(args)


def _normalise_file(args: argparse.Namespace) -> int:
    from tret.config import get_settings
    from tret.services import normalise

    path: Path = args.path
    if not path.is_file():
        print(f"error: {path} is not a file", file=sys.stderr)
        return 2
    settings = get_settings()
    if args.no_docling:
        settings = settings.model_copy(update={"normaliser_docling": "off"})
    result = normalise.normalise_file(path, settings)
    if result.status == normalise.STATUS_FAILED:
        print(f"error: {path.name}: {result.error}", file=sys.stderr)
        return 1
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / f"{path.stem}.md").write_text(result.markdown, encoding="utf-8")
    report = {
        "file": path.name,
        "source_kind": result.source_kind,
        "status": result.status,
        "quarantine_reason": result.quarantine_reason,
        "normaliser_version": normalise.NORMALISER_VERSION,
        "markdown_chars": len(result.markdown),
        "metrics": result.metrics,
    }
    (args.out / f"{path.stem}.metrics.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    m = result.metrics
    print(
        f"{path.name}: {result.status}"
        + (f" ({result.quarantine_reason})" if result.quarantine_reason else "")
        + f", {m.get('pages')} pages, {m.get('tables_found')} tables, "
        f"{m.get('table_pages_converted')}/{m.get('table_pages_selected')} table pages converted, "
        f"{m.get('seconds_total')}s, peak child RSS {m.get('peak_rss_mb')} MB"
    )
    return 0


def _normalise_batch(args: argparse.Namespace) -> int:
    import asyncio
    import uuid

    from tret.config import get_settings
    from tret.services import normalise

    if not normalise.normaliser_enabled(get_settings()):
        print(
            "error: the normaliser is off: set TRET_DISCLOSURE_NORMALISER=on to run it "
            "(`tret disclosure normalise-file` works without it)",
            file=sys.stderr,
        )
        return 2
    try:
        batch_id = uuid.UUID(args.batch)
    except ValueError:
        print(f"error: {args.batch!r} is not a valid batch id", file=sys.stderr)
        return 2
    if args.limit is not None and args.limit < 1:
        print("error: --limit must be at least 1", file=sys.stderr)
        return 2

    from tret.db.engine import get_session_factory
    from tret.db.models import Batch

    def show(summary: dict) -> None:
        if args.json:
            return
        print(f"{summary['slug']}: {len(summary['reports'])} report(s)")
        for r in summary["reports"]:
            flag = " [primary]" if r["is_primary"] else ""
            why = f" ({r['quarantine_reason']})" if r["quarantine_reason"] else ""
            err = f" error: {r['error']}" if r["error"] else ""
            print(
                f"  {r['status']}{why}{flag} {r['filename']}: {r['markdown_chars']} chars, "
                f"{r['tables']} tables, {r['seconds']}s{err}"
            )

    async def go():
        factory = get_session_factory()
        async with factory() as db:
            if await db.get(Batch, batch_id) is None:
                return None
        return await normalise.normalise_batch(
            factory, batch_id, limit=args.limit, target_slug=args.target,
            only_status=args.only_status, force=args.force, progress=show,
        )

    summaries = asyncio.run(go())
    if summaries is None:
        print(f"error: batch {batch_id} not found", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(summaries, indent=2))
    reports = [r for s in summaries for r in s["reports"]]
    counts = {k: sum(1 for r in reports if r["status"] == k) for k in ("normalised", "quarantined", "failed")}
    print(f"{len(summaries)} target(s), {len(reports)} report(s): " + ", ".join(f"{n} {k}" for k, n in counts.items()))
    return 1 if counts["failed"] else 0
