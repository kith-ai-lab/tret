"""`bench` CLI — pack authoring utilities, and outcome bookkeeping."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _validate(path: Path) -> None:
    from bench.packs.loader import validate_pack

    manifest, schemas, errors = validate_pack(path)
    if errors:
        print(f"INVALID: {path}")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)
    print(f"OK: {manifest.pack}@{manifest.version}")
    print(f"  task types: {', '.join(t.slug for t in manifest.task_types)}")
    print(f"  schemas:    {', '.join(sorted(schemas))}")
    print(f"  doctrine:   {len(manifest.doctrine)} files")
    print(f"  methods:    {', '.join(m.slug for m in manifest.methods) or '(none)'} "
          "— static safety scan passed (a deterrent, not a sandbox)")


def _hash(path: Path) -> None:
    from bench.packs.integrity import iter_pack_entries, pack_content_hash

    if not path.is_dir():
        print(f"not a directory: {path}")
        sys.exit(1)
    entries = iter_pack_entries(path)
    links = sum(1 for e in entries if e.is_symlink)
    print(pack_content_hash(path))
    # Counted from the same walk the hash uses, symlinks included, so the number
    # printed here is always what was actually pinned.
    covered = f"{len(entries)} entries under {path}"
    print(f"  {covered}{f' ({links} symlink(s), pinned by target)' if links else ''}",
          file=sys.stderr)
    print(
        "  This is the hash an install pins on the pack row. Reinstall the pack "
        "(restart bench, or POST /api/packs/install) to re-pin after an intentional edit.",
        file=sys.stderr,
    )


def _backfill_outcomes(limit: int | None) -> None:
    """Rebuild `run_outcomes` from the runs already in the database.

    Two jobs, same command. On upgrade it seeds the table so routing has history
    from day one instead of starting blind. Afterwards it is the repair path: the
    table is derived, so re-running this after the scoring weights change
    re-judges every past run under the new version rather than leaving a database
    of scores that mean two different things.
    """
    import asyncio

    from bench.db.engine import get_session_factory
    from bench.router_llm.outcomes import OUTCOME_SCORE_VERSION
    from bench.services.outcomes import backfill

    async def run() -> dict:
        async with get_session_factory()() as db:
            return await backfill(db, limit=limit)

    stats = asyncio.run(run())
    # Rows, not runs: a run that changed model part-way produced evidence about
    # every model it used.
    print(f"scored {stats['written']} outcome row(s) at {OUTCOME_SCORE_VERSION}")
    print(f"  scanned: {stats['scanned']}")
    # Not an error: runs that failed before they were routed have no model to
    # attribute anything to, and cancelled runs are deliberately not evidence.
    print(f"  skipped: {stats['skipped']} (never routed, or carrying no lesson)")


def main() -> None:
    parser = argparse.ArgumentParser(prog="bench")
    sub = parser.add_subparsers(dest="command", required=True)

    packs = sub.add_parser("packs", help="Domain pack utilities")
    packs_sub = packs.add_subparsers(dest="packs_command", required=True)
    validate = packs_sub.add_parser("validate", help="Validate a pack directory")
    validate.add_argument("path", type=Path)
    hash_cmd = packs_sub.add_parser(
        "hash", help="Print the pack content hash used for integrity pinning"
    )
    hash_cmd.add_argument("path", type=Path)

    outcomes = sub.add_parser("outcomes", help="Routing outcome evidence")
    outcomes_sub = outcomes.add_subparsers(dest="outcomes_command", required=True)
    backfill_cmd = outcomes_sub.add_parser(
        "backfill", help="Rebuild run_outcomes from existing runs"
    )
    backfill_cmd.add_argument(
        "--limit", type=int, default=None, help="Only score the N most recent runs"
    )

    args = parser.parse_args()
    if args.command == "outcomes":
        if args.outcomes_command == "backfill":
            _backfill_outcomes(args.limit)
        return
    if args.command == "packs":
        if args.packs_command == "validate":
            _validate(args.path)
        elif args.packs_command == "hash":
            _hash(args.path)


if __name__ == "__main__":
    main()
