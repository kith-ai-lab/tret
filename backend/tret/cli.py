"""`tret` CLI — pack authoring utilities, outcome bookkeeping, and `run`."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Duplicated from tret.router_llm.objectives / tret.local_run rather than
# imported: every import `run` needs must be lazy, inside its handler (see
# `_run_command`), so `tret run --help` and every other subcommand never pay
# for the router/provider/pypdf/docx import chain. These three values are the
# ones argparse needs *before* the handler runs, just to print `--help`.
_RUN_DEFAULT_OBJECTIVE = "balanced"  # tret.router_llm.objectives.DEFAULT_OBJECTIVE
_RUN_DEFAULT_MAX_COST_TIER = "premium"  # tret.router_llm.objectives.DEFAULT_MAX_COST_TIER
_RUN_DEFAULT_MAX_ITERATIONS = 24  # tret.local_run.DEFAULT_MAX_ITERATIONS


def _at_least_one(value: str) -> int:
    try:
        n = int(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"{value!r} is not an integer") from e
    if n < 1:
        # 0 or a negative cap would "run" without a single provider call and
        # then report on a run that never happened.
        raise argparse.ArgumentTypeError("must be at least 1")
    return n


def _validate(path: Path) -> None:
    from tret.packs.loader import validate_pack

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
    from tret.packs.integrity import iter_pack_entries, pack_content_hash

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
        "(restart tret, or POST /api/packs/install) to re-pin after an intentional edit.",
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

    from tret.db.engine import get_session_factory
    from tret.router_llm.outcomes import OUTCOME_SCORE_VERSION
    from tret.services.outcomes import backfill

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


def _run_command(args: argparse.Namespace) -> None:
    """`tret run TASK [--path DIR] [--out FILE] ...` — see backend/tret/local_run.py.

    Every import this needs is lazy, here in the handler: `tret run --help`
    (and every other subcommand) must stay off the router/provider/pypdf/docx
    import chain, and `tests/test_sdk_import_hygiene.py` checks exactly that
    for `--help`.
    """
    import asyncio
    import json as json_module
    from dataclasses import asdict

    from tret import local_run
    from tret.providers.base import ProviderError
    from tret.router_llm.router import RoutingUnavailable

    quiet: bool = args.quiet

    def emit(line: str) -> None:
        if not quiet:
            print(line, file=sys.stderr)

    def on_route(decision) -> None:
        short_model = decision.chosen_model.rsplit("/", 1)[-1]
        reasoning = (decision.reasoning or "").strip().splitlines()
        first_line = reasoning[0] if reasoning else "(no reasoning given)"
        emit(f"→ routed to {short_model} · {first_line}")

    def on_tool_call(tc) -> None:
        arg_summary = ", ".join(f"{k}={v!r}" for k, v in tc.arguments.items())
        emit(f"→ {tc.name} {arg_summary}")

    try:
        result = asyncio.run(
            local_run.arun(
                args.task,
                path=args.path,
                out=args.out,
                objective=args.objective,
                max_cost_tier=args.max_cost_tier,
                model=args.model,
                max_iterations=args.max_iterations,
                on_route=on_route,
                on_tool_call=on_tool_call,
            )
        )
    except (ValueError, RoutingUnavailable, ProviderError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)

    # A run that did not complete, or completed with nothing to say, must not
    # touch --out: overwriting yesterday's good report with an empty file and
    # printing a checkmark would be silent data loss behind a green exit code.
    failed = result.status != "completed" or not result.text
    if result.status != "completed":
        detail = f": {result.error}" if result.error else ""
        print(f"error: run did not complete (status {result.status}){detail}", file=sys.stderr)

    out_failed = False
    if args.out and not failed:
        out_path = Path(args.out)
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(result.text, encoding="utf-8")
            emit(f"✓ written to {args.out}")
        except OSError as e:
            # The answer was paid for; losing it to a bad --out path would be
            # worse than the wrong destination. Fall back to stdout.
            print(f"error: could not write {args.out}: {e}", file=sys.stderr)
            out_failed = True

    if args.json:
        payload = {
            "text": result.text,
            "receipt": asdict(result.receipt),
            "ledger_id": result.ledger_id,
            "status": result.status,
            "iterations": result.iterations,
            "error": result.error,
        }
        print(json_module.dumps(payload))
    else:
        if result.text and (not args.out or out_failed or failed):
            print(result.text)
        # Everything but the payload text is metadata about the run, so it goes
        # to stderr alongside the progress lines above — stdout stays just the
        # answer (or, with --out, is left free for shell piping of something
        # else).
        receipt_line = f"{result.receipt} · ledger #{result.ledger_id[:4]}"
        if result.status != "completed":
            receipt_line += f" · status {result.status}"
        print(receipt_line, file=sys.stderr)

    if failed or out_failed:
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(prog="tret")
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

    run_cmd = sub.add_parser(
        "run", help="Route a task, run it (optionally over local files), print a receipt"
    )
    run_cmd.add_argument("task", help="The task to perform, in plain language")
    run_cmd.add_argument(
        "--path", default=None, help="Root directory of local files the model may read"
    )
    run_cmd.add_argument(
        "--out", default=None, help="Write the final text here instead of stdout"
    )
    run_cmd.add_argument(
        "--model", default=None, help="Pin a specific model id, bypassing the router"
    )
    run_cmd.add_argument(
        "--objective", default=_RUN_DEFAULT_OBJECTIVE, help="Routing objective"
    )
    run_cmd.add_argument(
        "--max-cost-tier",
        dest="max_cost_tier",
        default=_RUN_DEFAULT_MAX_COST_TIER,
        help="Cost ceiling tier (local | economy | standard | premium)",
    )
    run_cmd.add_argument(
        "--max-iterations",
        dest="max_iterations",
        type=_at_least_one,
        default=_RUN_DEFAULT_MAX_ITERATIONS,
        help="Cap on agent-loop iterations (at least 1)",
    )
    run_cmd.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON to stdout"
    )
    run_cmd.add_argument(
        "--quiet", action="store_true", help="Suppress progress lines on stderr"
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
        return
    if args.command == "run":
        _run_command(args)


if __name__ == "__main__":
    main()
