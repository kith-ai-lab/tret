"""`tret eval ...`: operator commands of the disclosure evaluation engine
(docs/disclosure-evaluation.md).

* `jury-preview` shows who the router would seat on a jury and why. It reads the database
  directly (no server needed), writes nothing and calls no model. Not gated by `TRET_EVAL_JURY`
  (that gates the HTTP routes).
* `tournament create|start|status|cancel|resume|ratings|refit|delete`: the Engine 1 tournament. `create` is a
  free preview (it writes the draft and the peer-pool snapshot, calls no model). `start --budget USD`
  freezes the jury and plays the tournament in this process until it completes, pauses (budget) or
  is interrupted (Ctrl-C pauses it). The tournament needs `TRET_EVAL_JURY=on`; the HTTP routes need
  `TRET_EVAL_TOURNAMENT=on` as well."""
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

    tp = esub.add_parser(
        "tournament",
        help="Engine 1 tournament: pairwise juror matches over peer pools (create is a free preview)",
        description="Create a tournament preview, start it with a budget, watch it, read the ratings.",
    )
    tsub = tp.add_subparsers(dest="tournament_command", required=True)
    c = tsub.add_parser("create", help="Preview: pools, rounds, estimated matches and cost, and the jury. Spends nothing.")
    c.add_argument("--workspace", required=True, metavar="ID", help="Workspace id")
    c.add_argument("--module", required=True, help="Module slug, e.g. physical_climate_risk")
    c.add_argument("--module-set", metavar="SLUG@VERSION", help="Default: the configured module set")
    c.add_argument("--batch", action="append", dest="batches", metavar="ID", help="Source batch id (repeatable); default: every batch")
    c.add_argument("--min-n", type=int, default=15, help="Smallest pool before it goes up the NACE ladder (default 15)")
    c.add_argument("--policy-json", metavar="JSON", help="Jury model policy as a JSON object (default: auto)")
    c.add_argument("--json", action="store_true")
    st = tsub.add_parser("start", help="Freeze the jury and play the tournament here, within a USD budget")
    st.add_argument("tournament_id")
    st.add_argument("--budget", type=float, required=True, metavar="USD", help="The most this tournament may spend")
    rs = tsub.add_parser("resume", help="Continue a paused tournament here (optionally with a larger budget)")
    rs.add_argument("tournament_id")
    rs.add_argument("--budget", type=float, metavar="USD", help="New total budget")
    for name, helptext in (("status", "Status, spend, pools and summary"), ("cancel", "Stop a tournament for good"),
                           ("ratings", "Ratings per pool, best first"),
                           ("refit", "Recompute the ratings of a completed or failed tournament from its stored matches (no spend)"),
                           ("delete", "Delete a draft preview and its snapshot")):
        x = tsub.add_parser(name, help=helptext)
        x.add_argument("tournament_id")
        x.add_argument("--json", action="store_true")


def run(args: argparse.Namespace) -> int:
    if getattr(args, "eval_command", None) == "tournament":
        return _tournament(args)
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


# ── tournament ─────────────────────────────────────────────────────────────────────────────────
def render_preview(t: dict) -> str:
    est = t["estimates"]
    m, cost = est["matches"], est.get("cost_usd")
    lines = [
        f"Tournament {t['id']} (draft, nothing spent): module {t['module']} of {t['module_set']}, "
        f"{est['n']} modules in {len(est['pools'])} pool(s), up to {est['rounds']} round(s).",
        f"Estimated matches: {m['total']} ({m['primary']} primary + {m['swap']} swaps + {m['double']} doubles).",
        "Estimated cost: " + (f"${cost['low']:.2f} to ${cost['high']:.2f} (expected ${cost['expected']:.2f})" if cost else "unknown (a juror is not priced)")
        + f"; {est['method']}.",
        "",
        "Pools:",
    ]
    for p in est["pools"]:
        regimes = ", ".join(f"{k} {v}" for k, v in p["regimes"].items())
        lines.append(f"  {p['key']:<28} n={p['n']:<4} rounds={p['rounds']} matches~{p['primary_matches']}  [{regimes}]" + ("  (small pool)" if p["small"] else ""))
    skipped = est.get("unrated_pools") or []
    if skipped:
        lines.append(f"  not rated (fewer than 2 modules): {', '.join(p['key'] for p in skipped)}")
    snap = t.get("snapshot") or {}
    unclassified = (snap.get("corpus_filter") or {}).get("excluded_unclassified", 0)
    if unclassified:
        lines.append(f"  {unclassified} module(s) left out: no classification")
    lines += ["", render(est["jury"]), "", f"Start it with: tret eval tournament start {t['id']} --budget USD"]
    return "\n".join(lines)


def render_status(t: dict) -> str:
    lines = [
        f"Tournament {t['id']}: {t['status']}" + (f" ({t['pause_reason']})" if t.get("pause_reason") else "")
        + f", round {t['current_round']} of {t['rounds']}, spent ${t['spent_usd']:.4f}"
        + (f" of ${t['budget_usd']:.2f}" if t.get("budget_usd") is not None else " (no budget set)"),
    ]
    s = t.get("summary") or {}
    if s:
        mm = s["matches"]
        lines.append(f"Matches: {mm['primary']} primary, {mm['swap']} swaps, {mm['double']} doubles, {mm['failed']} failed.")
        fr = s["flip_rate"]["overall"]
        lines.append("Flip rate: " + ("n/a" if fr["rate"] is None else f"{fr['rate']:.3f} over {fr['pairs']} pair(s)"))
        ln = s["length_r"]["overall"]
        lines.append(f"Rating vs length: Pearson {ln['pearson']}, Spearman {ln['spearman']} (n={ln['n']})")
        for f in s.get("flags", []):
            lines.append(f"FLAG {f['flag']}: {f['detail']}")
    return "\n".join(lines)


def _tournament(args: argparse.Namespace) -> int:
    import asyncio
    import uuid

    from tret.db.engine import get_session_factory
    from tret.db.models import EvalTournament, PeerPoolSnapshot
    from tret.evaluation import tournament as tn

    cmd = args.tournament_command

    def ident(value: str) -> uuid.UUID | None:
        try:
            return uuid.UUID(value)
        except ValueError:
            print("error: not an id: " + value, file=sys.stderr)
            return None

    async def load(db, tid):
        t = await db.get(EvalTournament, tid)
        if t is None:
            raise tn.TournamentError("Tournament not found", 404)
        return t

    async def out(db, t):
        return tn.tournament_out(t, await db.get(PeerPoolSnapshot, t.snapshot_id))

    async def go() -> tuple[int, str]:
        factory = get_session_factory()
        async with factory() as db:
            if cmd == "create":
                policy = json.loads(args.policy_json) if args.policy_json else None
                t = await tn.create_tournament(
                    db, workspace_id=uuid.UUID(args.workspace), module=args.module, module_set=args.module_set,
                    batch_ids=[uuid.UUID(b) for b in args.batches] if args.batches else None, min_n=args.min_n,
                    model_policy=policy, created_by="cli",
                )
                await db.commit()
                o = await out(db, t)
                return 0, json.dumps(o, indent=2) if args.json else render_preview(o)
            tid = ident(args.tournament_id)
            if tid is None:
                return 2, ""
            t = await load(db, tid)
            ws = t.workspace_id
            if cmd == "status":
                o = await out(db, t)
                return 0, json.dumps(o, indent=2) if args.json else render_status(o)
            if cmd == "ratings":
                r = await tn.list_ratings(db, t)
                if args.json:
                    return 0, json.dumps(r, indent=2)
                lines = [f"Ratings of {t.id} ({t.status}):"]
                for p in r["pools"]:
                    lines.append(f"\n{p['key']}")
                    for x in p["ratings"]:
                        band = f"  {x['band_kind']} {x['band']}" if x["band_kind"] else ""
                        lines.append(f"  {x['rating']:7.1f}  [{x['lo90']:.0f}, {x['hi90']:.0f}]  {x['matches']:>3} matches  {x['company']}{band}")
                return 0, "\n".join(lines)
            if cmd == "cancel":
                t = await tn.cancel_tournament(db, workspace_id=ws, tournament_id=tid)
                return 0, f"Tournament {t.id} cancelled; spent ${float(t.spent_usd or 0):.4f}."
            if cmd == "refit":
                t = await tn.refit_tournament(db, workspace_id=ws, tournament_id=tid)
                return 0, render_status(await out(db, t))
            if cmd == "delete":
                await tn.delete_draft(db, workspace_id=ws, tournament_id=tid)
                return 0, f"Draft {tid} deleted."
            if cmd == "start":
                t = await tn.start_tournament(db, workspace_id=ws, tournament_id=tid, budget_usd=args.budget, started_by="cli", spawn=False)
            else:  # resume
                t = await tn.resume_tournament(db, workspace_id=ws, tournament_id=tid, budget_usd=args.budget, spawn=False)
            owner = t.lease_owner
        try:
            await tn.run_tournament(tid, owner)
        except (KeyboardInterrupt, asyncio.CancelledError):
            # this tournament only, never another one running elsewhere
            await tn.pause_tournament(tid, owner=owner, reason="interrupted")
            return 130, "Interrupted: the tournament is paused. Resume it with `tret eval tournament resume`."
        async with factory() as db:
            return 0, render_status(await out(db, await load(db, tid)))

    try:
        code, text = asyncio.run(go())
    except tn.TournamentError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except (ValueError, json.JSONDecodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if text:
        print(text)
    return code
