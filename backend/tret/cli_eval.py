"""`tret eval ...`: operator commands of the disclosure evaluation engine
(docs/disclosure-evaluation.md). Today: `jury-preview`, which shows who the router would seat on a
jury and why. It reads the database directly (no server needed), writes nothing and calls no
model. Not gated by `TRET_EVAL_JURY` (that gates the HTTP routes)."""
from __future__ import annotations

import argparse
import json
import sys
import textwrap


def register(sub: argparse._SubParsersAction) -> None:
    ev = sub.add_parser(
        "eval",
        help="Disclosure evaluation engine: jury selection (see docs/disclosure-evaluation.md)",
        description="Operator commands of the disclosure evaluation engine.",
    )
    esub = ev.add_subparsers(dest="eval_command", required=True)
    jp = esub.add_parser(
        "jury-preview",
        help="Show the jury the router would pick, and why. No model is called, nothing is stored.",
        description="Ask the router for k jurors from distinct model families under a policy. The "
        "router picks; a model is never named. Prints each pick with its family, rank and reason.",
    )
    jp.add_argument("--k", type=int, default=3, help="Jury size, 1-5 (default 3)")
    jp.add_argument("--policy-json", metavar="JSON", help="Model policy as a JSON object (default: auto)")
    jp.add_argument("--min-context", type=int, default=0, metavar="N", help="Smallest context window a juror needs")
    jp.add_argument("--allow-repeat-families", action="store_true", help="Top k models even from one family")
    jp.add_argument("--purpose", default="eval_jury", choices=("eval_jury", "sealed_call"))
    jp.add_argument("--workspace", metavar="ID", help="Workspace whose provider keys apply (default: server keys)")
    jp.add_argument("--json", action="store_true", help="Print the full selection as JSON")


def run(args: argparse.Namespace) -> int:
    return _jury_preview(args)


def render(sel: dict) -> str:
    lines = [
        f"Jury preview ({sel['purpose']}): k={sel['k']}, objective {sel['objective']}, open weights {sel['open_weights']}, "
        f"{sel['eligible']} eligible model(s) in {sel['families_eligible']} famil(ies). "
        f"Ranking {sel['ranking_version']}; no model was called.",
        "",
    ]
    for i, p in enumerate(sel["picks"], start=1):
        lines.append(f"{i}. {p['model']}  [family {p['family']}, rank {p['rank']}]")
        lines.append(textwrap.fill(p["why"], 100, initial_indent="     ", subsequent_indent="     "))
        if p["alternatives"]:
            lines.append("     next best in the family: " + ", ".join(f"{a['model']} (rank {a['rank']})" for a in p["alternatives"]))
    if sel["skipped_families"]:
        lines += ["", "Families not seated: " + "; ".join(f"{s['family']} ({s['best_model']}, rank {s['best_rank']})" for s in sel["skipped_families"])]
    if sel["excluded_models"]:
        lines.append(f"Excluded: {len(sel['excluded_models'])} model(s), e.g. {sel['excluded_models'][0]['model']}: {sel['excluded_models'][0]['reason']}")
    return "\n".join(lines)


def _jury_preview(args: argparse.Namespace) -> int:
    import asyncio
    import uuid

    policy = None
    if args.policy_json:
        try:
            policy = json.loads(args.policy_json)
            if not isinstance(policy, dict):
                raise ValueError("a policy must be a JSON object")
        except (ValueError, json.JSONDecodeError) as e:
            print(f"error: --policy-json: {e}", file=sys.stderr)
            return 2
    ws = None
    if args.workspace:
        try:
            ws = uuid.UUID(args.workspace)
        except ValueError:
            print("error: --workspace must be a workspace id", file=sys.stderr)
            return 2

    from tret.db.engine import get_session_factory
    from tret.evaluation import jury

    async def go():
        async with get_session_factory()() as db:
            return await jury.preview_jury(
                db, workspace_id=ws, model_policy=policy, k=args.k, distinct_authors=not args.allow_repeat_families,
                min_context_window=args.min_context, purpose=args.purpose,
            )

    try:
        sel = asyncio.run(go())
    except jury.JuryError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(json.dumps(sel.to_json(), indent=2) if args.json else render(sel.to_json()))
    return 0
