"""`tret disclosure ...`: the Stage 1 normaliser (docs/disclosure-collection.md).

* `normalise --batch <id>` reads the database like `tret approvals mark-packaging`
  does (no server needed) and normalises the PDF and ESEF documents each target's run
  fetched. Needs `TRET_DISCLOSURE_NORMALISER=on`.
* `classify-sec --batch <id>` fills the peer taxonomy from the SEC's SIC codes (needs
  `TRET_SEC_USER_AGENT`); `classifications --batch <id>` lists each target's effective
  classification. Both work whether or not `TRET_PEER_TAXONOMY` is on.
* `extract`, `claim-density --from-batch <id>` and `review-list --from-batch <id>` are the
  Stage 2 commands (docs/disclosure-evaluation.md): create an extraction batch, fill the stored
  claim density, print what a human should check.
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

    cls = dsub.add_parser(
        "classify-sec",
        help="Classify a batch's targets by their SEC SIC code (needs TRET_SEC_USER_AGENT)",
        description="For each target with a CIK, read its SIC code from the SEC submissions API, "
        "map it to NACE Rev. 2 and store a sec_sic classification. Reads and writes the database "
        "directly (no server needed). At most 8 requests per second.",
    )
    cls.add_argument("--batch", required=True, metavar="BATCH_ID", help="Batch id")
    cls.add_argument("--limit", type=int, metavar="N", help="Only the first N targets (register order)")
    cls.add_argument("--force", action="store_true", help="Redo targets that already have a sec_sic row")
    cls.add_argument("--dry-run", action="store_true", help="Fetch and map but write nothing")
    cls.add_argument("--json", action="store_true", help="Print raw JSON")

    lst = dsub.add_parser(
        "classifications",
        help="List each target's effective peer classification (manual > sec_sic > extractor)",
    )
    lst.add_argument("--batch", required=True, metavar="BATCH_ID", help="Batch id")
    lst.add_argument("--csv", type=Path, metavar="FILE", help="Also write the table to this CSV file")
    lst.add_argument("--json", action="store_true", help="Print raw JSON")

    ext = dsub.add_parser(
        "extract",
        help="Create a module-extraction batch from a collection batch's normalised reports "
        "(needs TRET_DISCLOSURE_EXTRACT=on)",
        description="Stage 2: create a DRAFT `disclosure_extract` batch with one target per primary "
        "normalised report of the source batch, or with --dry-run only print how many reports, how "
        "many input tokens and what cost range (no model call). Start the batch from the batches API "
        "or UI (POST /api/collections/<id>/start) so the running server executes it. The model is "
        "chosen by the router under --policy; a pinned model is refused.",
    )
    ext.add_argument("--from-batch", required=True, metavar="BATCH_ID", help="The collection batch to read")
    ext.add_argument("--limit", type=int, metavar="N", help="Only the first N reports")
    ext.add_argument("--only-missing", action="store_true", help="Skip reports already extracted at this version")
    ext.add_argument(
        "--policy", metavar="POLICY",
        help='Model policy: a JSON object or key=value pairs, e.g. "objective=eco,open_weights=prefer"',
    )
    ext.add_argument("--module-set", type=Path, metavar="FILE", help="Module set YAML (default: TRET_DISCLOSURE_MODULE_SET or the built-in)")
    ext.add_argument("--name", metavar="NAME", help="Batch name")
    ext.add_argument("--run-slug", metavar="SLUG", help="Batch run slug (default: extract-<date>-<id>)")
    ext.add_argument("--max-cost", type=float, metavar="USD", help="Cost cap per report")
    ext.add_argument("--dry-run", action="store_true", help="Count reports, estimate tokens and cost; create nothing")
    ext.add_argument("--json", action="store_true", help="Print raw JSON")

    dens = dsub.add_parser(
        "claim-density",
        help="Compute (or recompute) the claim density of a batch's extracted modules",
        description="Read the module texts of the extractions made from a collection batch (or an "
        "extraction batch) from the blob store and store their claim density (claims per 100 words, "
        "with the breakdown by category). Modules already at the current density version are "
        "skipped unless --force; a module whose text is missing from storage is skipped and counted. "
        "Works whether or not TRET_DISCLOSURE_EXTRACT is on.",
    )
    dens.add_argument("--from-batch", required=True, metavar="BATCH_ID", help="The collection batch (or extraction batch)")
    dens.add_argument("--force", action="store_true", help="Recompute modules already at the current density version")
    dens.add_argument("--json", action="store_true", help="Print raw JSON")

    rvw = dsub.add_parser(
        "review-list",
        help="List what a human should check in a batch's extractions",
        description="The computed extraction review list (failed or partial extractions, low-confidence "
        "or truncated modules, boilerplate-looking modules, rejected spans, doubtful NACE "
        "classifications), most severe first. Decisions are recorded with "
        "PUT /api/report-extractions/<id>/review.",
    )
    rvw.add_argument("--from-batch", required=True, metavar="BATCH_ID", help="The collection batch (or extraction batch)")
    rvw.add_argument("--kind", action="append", metavar="KIND", help="Only this item kind (repeatable)")
    rvw.add_argument("--all", action="store_true", help="Include items that already have a decision")
    rvw.add_argument("--json", action="store_true", help="Print raw JSON")

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
    if args.disclosure_command == "extract":
        return _extract(args)
    if args.disclosure_command == "claim-density":
        return _claim_density(args)
    if args.disclosure_command == "review-list":
        return _review_list(args)
    if args.disclosure_command == "classify-sec":
        return _classify_sec(args)
    if args.disclosure_command == "classifications":
        return _classifications(args)
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


def _batch_uuid(raw: str):
    import uuid

    try:
        return uuid.UUID(raw)
    except ValueError:
        print(f"error: {raw!r} is not a valid batch id", file=sys.stderr)
        return None


def _classify_sec(args: argparse.Namespace) -> int:
    """`tret disclosure classify-sec`: SEC SIC code -> NACE ladder. No server needed."""
    import asyncio

    from tret.config import get_settings
    from tret.services import peer_taxonomy as pt

    user_agent = pt.sec_user_agent(get_settings())
    if user_agent is None:
        print(
            "error: TRET_SEC_USER_AGENT is empty. The SEC requires a descriptive User-Agent that "
            "names who is calling and how to reach them (organisation "
            "name plus a contact address or URL). Set it and run again.",
            file=sys.stderr,
        )
        return 2
    batch_id = _batch_uuid(args.batch)
    if batch_id is None:
        return 2
    if args.limit is not None and args.limit < 1:
        print("error: --limit must be at least 1", file=sys.stderr)
        return 2

    from tret.db.engine import get_session_factory
    from tret.net.policy import EgressDenied

    settings = get_settings()
    fetcher_box: dict = {}

    def show(item: dict) -> None:
        if args.json:
            return
        extra = ""
        if item["outcome"] == "classified":
            extra = (
                f" SIC {item['sic']} -> {item['nace_group'] or item['nace_division']} "
                f"({item['cluster']}, {item['match']})"
                + (" [GICS disagrees]" if item.get("gics_disagrees") else "")
            )
        elif item["outcome"] in ("unmapped_sic", "unclassifiable_sic", "no_sic"):
            extra = f" SIC {item.get('sic') or 'none'} {item.get('sic_description') or ''}".rstrip()
        elif item["outcome"] == "error":
            extra = f" {item['error']}"
        print(f"{item['slug']}: {item['outcome']}{extra}")

    async def go():
        # Built through tret/net: TRET_EGRESS, the research switch, TRET_EGRESS_PROXY, the URL
        # guard and the audit log all apply. An EgressDenied here means "refused", exit 2.
        client = pt.open_sec_client(settings, user_agent)
        fetcher = pt.SecFetcher(client)
        fetcher_box["f"] = fetcher
        try:
            return await pt.classify_sec_batch(
                get_session_factory(), batch_id, fetcher, limit=args.limit, force=args.force,
                dry_run=args.dry_run, progress=show,
            )
        finally:
            await fetcher.aclose()

    try:
        result = asyncio.run(go())
    except EgressDenied as e:
        print(f"error: egress refused: {e}", file=sys.stderr)
        return 2
    fetcher = fetcher_box["f"]
    if result is None:
        print(f"error: batch {batch_id} not found", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({**result.counts(), "dry_run": result.dry_run, "aborted": result.aborted,
                          "items": result.items}, indent=2))
    c = result.counts()
    verb = "would classify" if result.dry_run else "classified"
    print(
        f"{verb} {c['classified']}; unmapped SIC {c['unmapped_sic']}; unclassifiable SIC "
        f"{c['unclassifiable_sic']}; no SIC on record {c['no_sic']}; "
        f"errors {c['errors']}; skipped: {c['skipped_no_cik']} without a CIK, "
        f"{c['skipped_existing']} already classified, {c['skipped_duplicate']} duplicate CIK; "
        f"{fetcher.requests} request(s), {fetcher.rate_limited} HTTP 429"
    )
    if result.aborted:
        print(f"stopped early: {result.aborted}", file=sys.stderr)
    return 1 if (c["errors"] or result.aborted) else 0


CLASSIFICATION_COLUMNS = (
    "slug", "company", "nace_group", "nace_division", "cluster", "nace_section", "source",
    "source_code", "match", "gics_sector", "gics_disagrees", "reviewed_by",
)


def _classifications(args: argparse.Namespace) -> int:
    """`tret disclosure classifications`: each target's effective classification."""
    import asyncio
    import csv

    batch_id = _batch_uuid(args.batch)
    if batch_id is None:
        return 2

    from tret.db.engine import get_session_factory
    from tret.db.models import Batch
    from tret.services import peer_taxonomy as pt

    async def go():
        async with get_session_factory()() as db:
            batch = await db.get(Batch, batch_id)
            if batch is None:
                return None
            return await pt.effective_for_targets(db, batch)

    pairs = asyncio.run(go())
    if pairs is None:
        print(f"error: batch {batch_id} not found", file=sys.stderr)
        return 1
    rows = []
    for target, row in pairs:
        rows.append({
            "slug": target.slug, "company": target.company,
            "nace_group": row.nace_group if row else "", "nace_division": row.nace_division if row else "",
            "cluster": row.cluster if row else "", "nace_section": row.nace_section if row else "",
            "source": row.source if row else "", "source_code": (row.source_code if row else "") or "",
            "match": (row.match if row else "") or "",
            "gics_sector": (row.gics_sector if row else target.sector) or "",
            "gics_disagrees": "" if row is None or row.gics_disagrees is None else str(row.gics_disagrees).lower(),
            "reviewed_by": (row.reviewed_by if row else "") or "",
        })
    if args.csv:
        with args.csv.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CLASSIFICATION_COLUMNS, lineterminator="\n")
            w.writeheader()
            w.writerows(rows)
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        for r in rows:
            where = r["nace_group"] or r["nace_division"]
            print(
                f"{r['slug']}: "
                + (f"{where} {r['cluster']} ({r['source']} {r['source_code']})" if r["source"] else "unclassified")
                + (" [GICS disagrees]" if r["gics_disagrees"] == "true" else "")
            )
    done = sum(1 for r in rows if r["source"])
    print(f"{len(rows)} target(s), {done} classified, {len(rows) - done} without a classification")
    return 0


def _claim_density(args: argparse.Namespace) -> int:
    """`tret disclosure claim-density`: fill or refresh the stored claim density."""
    import asyncio

    batch_id = _batch_uuid(args.from_batch)
    if batch_id is None:
        return 2

    from tret.db.engine import get_session_factory
    from tret.db.models import Batch
    from tret.evaluation import density

    async def go():
        async with get_session_factory()() as db:
            batch = await db.get(Batch, batch_id)
            if batch is None:
                return None
            stats = await density.recompute_claim_density(
                db, workspace_id=batch.workspace_id, batch_id=batch_id, force=args.force
            )
            await db.commit()
            return stats

    stats = asyncio.run(go())
    if stats is None:
        print(f"error: batch {batch_id} not found", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({**stats, "density_version": density.DENSITY_VERSION}, indent=2))
    else:
        print(
            f"density {density.DENSITY_VERSION}: {stats['considered']} module(s), {stats['updated']} updated, "
            f"{stats['skipped_current']} already current, {stats['missing_text']} skipped (text missing from storage)"
        )
    return 0


def _review_list(args: argparse.Namespace) -> int:
    """`tret disclosure review-list`: the extraction review items of a batch."""
    import asyncio

    batch_id = _batch_uuid(args.from_batch)
    if batch_id is None:
        return 2

    from tret.db.engine import get_session_factory
    from tret.db.models import Batch
    from tret.evaluation import review

    async def go():
        async with get_session_factory()() as db:
            batch = await db.get(Batch, batch_id)
            if batch is None:
                return None
            return await review.review_items(
                db, workspace_id=batch.workspace_id, batch_id=batch_id, include_resolved=args.all, kinds=args.kind
            )

    try:
        items = asyncio.run(go())
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if items is None:
        print(f"error: batch {batch_id} not found", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps([i.as_dict() for i in items], indent=2))
        return 0
    rows = [
        ((i.company or "?")[:28], i.kind, i.module or "", (i.decision or {}).get("decision", ""), i.detail)
        for i in items
    ]
    head = ("COMPANY", "KIND", "MODULE", "DECISION", "DETAIL")
    widths = [max(len(head[c]), *(len(r[c]) for r in rows)) if rows else len(head[c]) for c in range(4)]
    print("  ".join(h.ljust(w) for h, w in zip(head, widths)) + "  " + head[4])
    for r in rows:
        print("  ".join(v.ljust(w) for v, w in zip(r, widths)) + "  " + r[4])
    print(f"{len(items)} item(s) to review" + ("" if args.all else " (decided items hidden; --all shows them)"))
    return 0


def parse_policy(raw: str | None) -> dict | None:
    """`--policy`: a JSON object, or comma-separated key=value pairs (`allowed` takes `|`)."""
    if raw is None or not raw.strip():
        return None
    text = raw.strip()
    if text.startswith("{"):
        value = json.loads(text)
        if not isinstance(value, dict):
            raise ValueError("a policy must be a JSON object")
        return value
    out: dict = {}
    for part in text.split(","):
        key, sep, val = part.partition("=")
        if not sep or not key.strip():
            raise ValueError(f"bad policy item {part!r}; use key=value")
        val = val.strip()
        out[key.strip()] = val.split("|") if key.strip() == "allowed" else val
    return out


def _extract(args: argparse.Namespace) -> int:
    """`tret disclosure extract`: create (or, with --dry-run, estimate) an extraction batch."""
    import asyncio
    import datetime as dt

    from fastapi import HTTPException

    batch_id = _batch_uuid(args.from_batch)
    if batch_id is None:
        return 2
    if args.limit is not None and args.limit < 1:
        print("error: --limit must be at least 1", file=sys.stderr)
        return 2
    try:
        policy = parse_policy(args.policy)
    except (ValueError, json.JSONDecodeError) as e:
        print(f"error: --policy: {e}", file=sys.stderr)
        return 2

    from tret.batches.profiles import disclosure_extract as de
    from tret.config import get_settings
    from tret.db.engine import get_session_factory
    from tret.db.models import Batch
    from tret.evaluation import estimate
    from tret.evaluation.modules import ModuleSetError, load_module_set

    if not de.extract_enabled(get_settings()):
        print(f"error: {de.OFF_MESSAGE}", file=sys.stderr)
        return 2
    try:
        module_set = load_module_set(args.module_set)
    except ModuleSetError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    async def go():
        async with get_session_factory()() as db:
            src = await db.get(Batch, batch_id)
            if src is None:
                return None
            if args.dry_run:
                register, pairs = await de.build_register(
                    db, workspace_id=src.workspace_id, source_batch_id=batch_id, limit=args.limit,
                    only_missing=args.only_missing, module_set=module_set,
                )
                return await estimate.estimate_reports(
                    [r for r, _ in pairs], module_set, get_settings().extract_window_tokens, policy
                )
            slug = args.run_slug or f"extract-{dt.date.today():%Y%m%d}-{str(batch_id)[:6]}"
            batch, targets = await de.create_extract_batch(
                db, workspace_id=src.workspace_id, user_id=None, source_batch_id=batch_id,
                name=args.name or f"Module extraction of {src.name}", run_slug=slug, limit=args.limit,
                only_missing=args.only_missing, model_policy=policy,
                module_set_path=str(args.module_set) if args.module_set else None,
                max_cost_usd=args.max_cost,
            )
            return {"batch_id": str(batch.id), "run_slug": batch.run_slug, "targets": len(targets), "status": batch.status}

    try:
        result = asyncio.run(go())
    except HTTPException as e:
        print(f"error: {e.detail}", file=sys.stderr)
        return 1
    if result is None:
        print(f"error: batch {batch_id} not found", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, indent=2))
    elif args.dry_run:
        cost = result["cost_usd"]
        print(
            f"{result['reports']} report(s), {result['windows']} window(s), about "
            f"{result['input_tokens']:,} input tokens; estimated cost "
            + (f"${cost['low']:.2f} to ${cost['high']:.2f} (cheapest to dearest catalog model)" if cost else "unknown (no catalog model)")
        )
    else:
        print(
            f"created draft batch {result['batch_id']} ({result['run_slug']}) with {result['targets']} target(s); "
            f"start it with POST /api/collections/{result['batch_id']}/start"
        )
    return 0
