"""`tret eval ...`: operator commands of the disclosure evaluation engine
(docs/disclosure-evaluation.md).

* `jury-preview` shows who the router would seat on a jury and why. It reads the database
  directly (no server needed), writes nothing and calls no model. Not gated by `TRET_EVAL_JURY`
  (that gates the HTTP routes).
* `tournament diagnostics <id>` prints the gates (length bias, flip rate, juror agreement, anchor drift);
  `tournament resume <id> --acknowledge length_bias,... --note "..."` continues past a tripped gate.
  `anchors create <tournament-id> --budget N | list | resume <id>`: the anchor sets of the judge-drift gate.
* `tournament create|start|status|cancel|resume|ratings|refit|delete`: the Engine 1 tournament. `create` is a
  free preview (it writes the draft and the peer-pool snapshot, calls no model). `start --budget USD`
  freezes the jury and plays the tournament in this process until it completes, pauses (budget) or
  is interrupted (Ctrl-C pauses it). The tournament needs `TRET_EVAL_JURY=on`; the HTTP routes need
  `TRET_EVAL_TOURNAMENT=on` as well.
* `credibility preview|create|start|status|reviews|show|verify|cancel|resume`: Engine 2, the sealed whole-report
  Credibility call. `preview` is free (it writes nothing); `create` writes a draft; `start --budget USD` freezes
  each arm's model and plays the pass in this process until it completes, pauses or is interrupted. Needs
  `TRET_EVAL_JURY=on`; the HTTP routes need `TRET_EVAL_CREDIBILITY=true` as well.
* `divergence inputs|preview|create|show|reading-list|spot-checks`: Engine 1's parts against Engine 2's whole (PR 9).
  Plain code over finished results: no model call, so no `TRET_EVAL_JURY` needed. Answering spot checks is done in
  the web UI only; the HTTP routes need `TRET_EVAL_DIVERGENCE=true`."""
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
    rs.add_argument("--acknowledge", metavar="GATE,GATE", help="Tripped gates you have looked at (they will not pause this tournament again)")
    rs.add_argument("--note", help="Why (kept with the acknowledgement)")
    for name, helptext in (("status", "Status, spend, pools and summary"), ("cancel", "Stop a tournament for good"),
                           ("ratings", "Ratings per pool, best first"),
                           ("diagnostics", "The gates: length bias, flip rate, juror agreement, anchor drift"),
                           ("refit", "Recompute the ratings of a completed or failed tournament from its stored matches (no spend)"),
                           ("delete", "Delete a draft preview and its snapshot")):
        x = tsub.add_parser(name, help=helptext)
        x.add_argument("tournament_id")
        x.add_argument("--json", action="store_true")

    ap = esub.add_parser(
        "anchors",
        help="Anchor sets of the judge-drift gate: fixed pairs re-judged every pass",
        description="Take an anchor set from a completed tournament (its baseline is judged once by a frozen jury), list them.",
    )
    asub = ap.add_subparsers(dest="anchors_command", required=True)
    ac = asub.add_parser("create", help="Pick the pairs of a completed tournament and judge the baseline here, within a USD budget")
    ac.add_argument("tournament_id")
    ac.add_argument("--budget", type=float, required=True, metavar="USD", help="The most the baseline may spend")
    al = asub.add_parser("list", help="The workspace's anchor sets")
    al.add_argument("--workspace", required=True, metavar="ID")
    al.add_argument("--json", action="store_true")
    ar = asub.add_parser("resume", help="Continue an unfinished baseline here (optionally with a larger budget)")
    ar.add_argument("anchor_set_id")
    ar.add_argument("--budget", type=float, metavar="USD")


    cp = esub.add_parser(
        "credibility",
        help="Engine 2: the sealed whole-report Credibility call (preview is free)",
        description="One report in, a 1-10 Credibility Score and the 3 sharpest pushbacks out. The model never sees peers, ratings or metadata.",
    )
    csub = cp.add_subparsers(dest="credibility_command", required=True)
    for name, helptext in (("preview", "Free: reports, skipped, whole vs digest, cost per arm and what each arm would pick. Writes nothing."),
                           ("create", "Write a draft pass (no model call, nothing spent)")):
        x = csub.add_parser(name, help=helptext)
        x.add_argument("--workspace", required=True, metavar="ID", help="Workspace id")
        x.add_argument("--batch", metavar="ID", help="Read the primary report of every target of this batch")
        x.add_argument("--report", action="append", dest="reports", metavar="ID", help="A disclosure report id (repeatable)")
        x.add_argument("--limit", type=int, metavar="N", help="Read at most N reports")
        x.add_argument("--sample-seed", type=int, metavar="N", help="With --limit: a seeded random sample, not the first N")
        x.add_argument("--arms-json", metavar="JSON", help='Arms as a JSON list of {"key","label","weight","model_policy"} (default: --experiment)')
        x.add_argument("--experiment", action="store_true", help="Two equal arms: open weights required vs any model (the default)")
        x.add_argument("--budget", type=float, metavar="USD", help="Compare the estimate with this budget")
        x.add_argument("--json", action="store_true")
        if name == "create":
            x.add_argument("--name", default="", help="A name for the pass")
    cs = csub.add_parser("start", help="Freeze each arm's model and play the pass here, within a USD budget")
    cs.add_argument("pass_id")
    cs.add_argument("--budget", type=float, required=True, metavar="USD", help="The most this pass may spend")
    cr_ = csub.add_parser("resume", help="Continue a paused pass here (optionally with a larger budget)")
    cr_.add_argument("pass_id")
    cr_.add_argument("--budget", type=float, metavar="USD", help="New total budget")
    cr_.add_argument("--acknowledge", metavar="GATE,GATE", help="Tripped gates you have looked at (they will not pause this pass again)")
    cr_.add_argument("--note", help="Why (kept with the acknowledgement)")
    for name, helptext in (("status", "Status, spend, gates and per-arm statistics"), ("cancel", "Stop a pass for good")):
        x = csub.add_parser(name, help=helptext)
        x.add_argument("pass_id")
        x.add_argument("--json", action="store_true")
    cl = csub.add_parser("reviews", help="The pass's reviews (company, arm, score, status)")
    cl.add_argument("pass_id")
    cl.add_argument("--arm")
    cl.add_argument("--status", choices=("pending", "running", "done", "failed", "skipped"))
    cl.add_argument("--score", type=int)
    cl.add_argument("--limit", type=int, default=50)
    cl.add_argument("--offset", type=int, default=0)
    cl.add_argument("--json", action="store_true")
    for name, helptext in (("show", "One review: score, pushbacks, story breaks, summary and its audit hashes"),
                           ("verify", "Payload audit: rebuild the prompt from the report and compare the stored hashes")):
        x = csub.add_parser(name, help=helptext)
        x.add_argument("review_id")
        x.add_argument("--json", action="store_true")

    dp = esub.add_parser(
        "divergence",
        help="PR 9: parts (Engine 1) against whole (Engine 2): quadrants, reading list, spot checks. No model is called.",
        description="Join a completed credibility pass with completed tournaments, per report. Answering spot checks is UI-only.",
    )
    dsub = dp.add_subparsers(dest="divergence_command", required=True)
    di = dsub.add_parser("inputs", help="The completed passes and tournaments a set can be built from")
    di.add_argument("--workspace", required=True, metavar="ID", help="Workspace id")
    di.add_argument("--json", action="store_true")
    for name, helptext in (("preview", "Free: stats, the parts-vs-whole gate and warnings. Writes nothing."),
                           ("create", "Freeze the join and sample the spot checks")):
        x = dsub.add_parser(name, help=helptext)
        x.add_argument("--workspace", required=True, metavar="ID", help="Workspace id")
        x.add_argument("--pass", dest="pass_id", required=True, metavar="ID", help="A completed credibility pass")
        x.add_argument("--tournament", action="append", dest="tournaments", required=True, metavar="ID", help="A completed tournament (repeatable, one per module, up to 4)")
        x.add_argument("--arm", metavar="KEY", help="Only this arm's scores (default: all arms)")
        x.add_argument("--json", action="store_true")
        if name == "create":
            x.add_argument("--name", default="", help="A name for the set")
    for name, helptext in (("show", "A set: stats, gates and spot-check progress"),
                           ("reading-list", "The biggest divergences, with each review's summary and pushbacks"),
                           ("spot-checks", "The set's spot checks as the UI shows them (blind until answered)")):
        x = dsub.add_parser(name, help=helptext)
        x.add_argument("set_id")
        x.add_argument("--json", action="store_true")


def run(args: argparse.Namespace) -> int:
    if getattr(args, "eval_command", None) == "divergence":
        return _divergence(args)
    if getattr(args, "eval_command", None) == "credibility":
        return _credibility(args)
    if getattr(args, "eval_command", None) == "tournament":
        return _tournament(args)
    if getattr(args, "eval_command", None) == "anchors":
        return _anchors(args)
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
        f"Estimated matches: {m['total']} ({m['primary']} primary + {m['swap']} swaps + {m['double']} doubles"
        + (f" + {m['anchor']} anchors of set {est['anchor_set']['id']}" if est.get("anchor_set") else "") + ").",
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
        if s.get("partial"):
            lines.append("Ratings are PARTIAL: the tournament did not finish.")
    if t.get("required_acknowledgements"):
        lines.append("Blocked on: " + ", ".join(t["required_acknowledgements"])
                     + f". Resume with: tret eval tournament resume {t['id']} --acknowledge {','.join(t['required_acknowledgements'])} --note \"why\"")
    return "\n".join(lines)


_STATE_MARK = {"ok": "ok", "warn": "WARN", "tripped": "TRIPPED", "insufficient_data": "not enough data", "not_applicable": "n/a"}


def render_diagnostics(d: dict) -> str:
    """The gates as a table, then what each rests on."""
    lines = [f"Diagnostics of {d['tournament_id']} ({d['status']}), computed {d['computed_at']}", "",
             f"{'gate':<17}{'state':<17}{'value':>8}{'n':>6}  detail"]
    for g in d["gates"]:
        value = "" if g["value"] is None else f"{g['value']:.2f}"
        ack = f" [acknowledged by {g['acknowledged_by']}]" if g.get("acknowledged_by") else ""
        lines.append(f"{g['key']:<17}{_STATE_MARK.get(g['state'], g['state']):<17}{value:>8}{g['n']:>6}  {g['detail']}{ack}")
    ov = d["length_bias"]["overall"]
    fmt = lambda v: "n/a" if v is None else f"{v:+.2f}"  # noqa: E731
    lines += ["", f"Rating vs word count (n={ov['n']}): Pearson {fmt(ov['pearson'])}, Spearman {fmt(ov['spearman'])}; "
              f"vs claim density: Pearson {fmt(ov['pearson_density'])}, Spearman {fmt(ov['spearman_density'])}"]
    for p in d["length_bias"]["pools"]:
        lines.append(f"  {p['pool_key']:<28} n={p['n']:<4} Spearman {fmt(p['spearman'])} (density {fmt(p['spearman_density'])})")
    if d["flip_rates"]:
        lines += ["", "Flip rate by juror family:"]
        for f in d["flip_rates"]:
            rate = "n/a" if f["rate"] is None else f"{f['rate']:.2f}"
            lines.append(f"  {f['family']:<14} {f['flips']}/{f['swaps']} = {rate}" + ("  DROPPED from the jury" if f["dropped"] else ""))
    if d["agreement"]:
        lines += ["", "Agreement between families (double-judged):"]
        for a in d["agreement"]:
            k = "n/a" if a["kappa"] is None else f"{a['kappa']:.2f}"
            lines.append(f"  {a['family_a']} vs {a['family_b']}: kappa {k}, raw {a['raw_agreement']:.2f}, n={a['n']}")
    for n in d.get("notes") or []:
        lines += ["", f"Note: {n}"] if n == d["notes"][0] else [f"Note: {n}"]
    if d["anchors"]:
        a = d["anchors"]
        lines += ["", f"Anchors {a['anchor_set_id']}: {a['changed']} changed of {a['judged']} judged ({a['n']} in the set)"]
    return "\n".join(lines)


def _anchors(args: argparse.Namespace) -> int:
    import asyncio
    import uuid

    from tret.db.engine import get_session_factory
    from tret.db.models import EvalTournament
    from tret.evaluation import anchors as an
    from tret.evaluation import tournament as tn

    cmd = args.anchors_command

    async def go() -> tuple[int, str]:
        factory = get_session_factory()
        async with factory() as db:
            if cmd == "list":
                rows = await an.list_anchor_sets(db, workspace_id=uuid.UUID(args.workspace))
                if args.json:
                    return 0, json.dumps(rows, indent=2)
                if not rows:
                    return 0, "No anchor sets."
                return 0, "\n".join(
                    f"{r['id']}  {r['status']:<16} {r['module']} ({r['module_set']})  {r['votes']}/{r['votes_expected']} votes  ${r['spent_usd']:.4f}"
                    + (f"  paused: {r['pause_reason']}" if r["pause_reason"] else "")
                    for r in rows
                )
            if cmd == "create":
                t = await db.get(EvalTournament, uuid.UUID(args.tournament_id))
                if t is None:
                    raise tn.TournamentError("Tournament not found", 404)
                a = await an.create_anchor_set(
                    db, workspace_id=t.workspace_id, tournament_id=t.id, budget_usd=args.budget, created_by="cli", spawn=False
                )
            else:  # resume
                a0 = await db.get(an.EvalAnchorSet, uuid.UUID(args.anchor_set_id))
                if a0 is None:
                    raise tn.TournamentError("Anchor set not found", 404)
                a = await an.resume_anchor_set(db, workspace_id=a0.workspace_id, set_id=a0.id, budget_usd=args.budget, spawn=False)
            sid, owner = a.id, a.lease_owner
        try:
            await an.run_baseline(sid, owner)
        except (KeyboardInterrupt, asyncio.CancelledError):
            return 130, "Interrupted: the baseline is unfinished. Resume it with `tret eval anchors resume`."
        async with factory() as db:
            out = await an.anchor_set_out(db, await db.get(an.EvalAnchorSet, sid))
        return 0, (f"Anchor set {out['id']}: {out['status']}" + (f" ({out['pause_reason']})" if out["pause_reason"] else "")
                   + f", {out['votes']}/{out['votes_expected']} votes, spent ${out['spent_usd']:.4f}")

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
            if cmd == "diagnostics":
                from tret.evaluation import diagnostics as dg

                d = await dg.compute_diagnostics(db, tid)
                return 0, json.dumps(d, indent=2) if args.json else render_diagnostics(d)
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
                acks = [g.strip() for g in (getattr(args, "acknowledge", None) or "").split(",") if g.strip()]
                t = await tn.resume_tournament(
                    db, workspace_id=ws, tournament_id=tid, budget_usd=args.budget, spawn=False,
                    acknowledge_gates=acks or None, note=getattr(args, "note", None), acknowledged_by="cli",
                )
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


# ── credibility (Engine 2) ──────────────────────────────────────────────────────────────────────
EXPERIMENT_ARMS = [
    {"key": "open", "label": "Open weights required", "weight": 1, "model_policy": {"mode": "auto", "open_weights": "require"}},
    {"key": "any", "label": "Any model", "weight": 1, "model_policy": {"mode": "auto", "open_weights": "any"}},
]


def render_credibility_preview(est: dict, pass_id: str | None = None) -> str:
    cost = est.get("cost_usd")
    head = f"Credibility pass {pass_id} (draft, nothing spent): " if pass_id else "Credibility preview (nothing written, no model called): "
    lines = [
        head + f"{est['reports']} report(s) to read, {est['skipped']} skipped"
        + (f" ({', '.join(f'{k} {v}' for k, v in est['skipped_reasons'].items())})" if est["skipped_reasons"] else "") + ".",
        f"Whole vs digest (summed over arms, each report counted per arm): {est['input']['whole']} whole, {est['input']['digest']} digest.",
        "Estimated cost: " + (f"${cost['low']:.2f} to ${cost['high']:.2f} (expected ${cost['expected']:.2f})" if cost else "unknown (no arm can be priced)")
        + (f"; budget ${est['budget_usd']:.2f}: " + ("within" if est["within_budget"] else "OVER") if est.get("budget_usd") is not None and cost else "") + ".",
        "",
        "Arms:",
    ]
    for a in est["arms"]:
        if not a.get("pick"):
            lines.append(f"  {a['key']:<10} {a['label']}: NO MODEL: {a.get('error')}")
            continue
        p = a["pick"]
        lines.append(f"  {a['key']:<10} {a['label']} (weight {a['weight']}, ~{a['expected_reports']} report(s)): {p['model']} [family {p['family']}, window {p['context_window']:,}]")
        lines.append(textwrap.fill(p["why"], 100, initial_indent="     ", subsequent_indent="     "))
        lines.append(f"     {a['whole_reports']} whole + {a['digest_reports']} digest, ~${a['cost_usd_per_report']:.4f} per report")
    if est.get("rejected_ids"):
        lines.append(f"  {len(est['rejected_ids'])} id(s) are not this workspace's reports and were left out")
    return "\n".join(lines)


def render_credibility_status(p: dict) -> str:
    c = p["counts"]
    lines = [
        f"Credibility pass {p['id']}: {p['status']}" + (f" ({p['pause_reason']})" if p.get("pause_reason") else "")
        + f", {c['done']} done / {c['failed']} failed / {c['pending'] + c['running']} left / {c['skipped']} skipped of {c['total']}, spent ${p['spent_usd']:.4f}"
        + (f" of ${p['budget_usd']:.2f}" if p.get("budget_usd") is not None else " (no budget set)"),
    ]
    for g in p.get("gates") or []:
        v = "" if g["value"] is None else f" {g['value']:.2f}"
        lines.append(f"  gate {g['key']:<15} {_STATE_MARK.get(g['state'], g['state'])}{v} (n={g['n']}): {g['detail']}")
    for a in p.get("arm_stats") or []:
        mean = "n/a" if a["mean"] is None else f"{a['mean']:.2f}"
        med = "n/a" if a["median"] is None else f"{a['median']:.1f}"
        lines.append(f"  arm {a['key']:<10} {a.get('model') or '(not frozen)'}: {a['n_done']} done, mean {mean}, median {med}, ${a['cost_usd']:.4f}")
        if a["n_done"]:
            lines.append("     scores 1-10: " + " ".join(f"{n:>3}" for n in a["histogram"]))
    if p.get("required_acknowledgements"):
        lines.append("Blocked on: " + ", ".join(p["required_acknowledgements"])
                     + f". Resume with: tret eval credibility resume {p['id']} --acknowledge {','.join(p['required_acknowledgements'])} --note \"why\"")
    return "\n".join(lines)


def render_credibility_review(r: dict) -> str:
    lines = [f"Review {r['id']}: {r.get('company') or r['disclosure_report_id']} ({r['status']}, arm {r['arm_key'] or '-'}, {r.get('model') or 'no model'}, {r.get('input_mode') or 'no input'})"]
    if r["credibility_score"] is not None:
        lines.append(f"Credibility Score: {r['credibility_score']}/10")
        lines.append(textwrap.fill(r.get("summary") or "", 100))
        for i, p in enumerate(r["pushbacks"], start=1):
            lines += [f"Pushback {i}: " + p["pushback"], textwrap.fill("evidence: " + p["evidence"], 100, initial_indent="    ", subsequent_indent="    ")]
        for b in r["story_breaks"]:
            lines.append("Story break: " + b)
    if r.get("error"):
        lines.append("Error: " + r["error"])
    lines.append(f"report sha256 {r.get('report_sha256')}  prompt sha256 {r.get('prompt_sha256')}  cost ${r['cost_usd']:.4f}")
    return "\n".join(lines)


def _credibility(args: argparse.Namespace) -> int:
    import asyncio
    import uuid

    from tret.db.engine import get_session_factory
    from tret.db.models import CredibilityPass
    from tret.evaluation import credibility as cr

    cmd = args.credibility_command

    def selection() -> dict:
        reports = [uuid.UUID(i) for i in args.reports] if args.reports else None
        return {"disclosure_report_ids": reports, "batch_id": uuid.UUID(args.batch) if args.batch else None,
                "limit": args.limit, "sample_seed": args.sample_seed}

    def arms() -> list[dict]:
        if args.arms_json:
            return json.loads(args.arms_json)
        return EXPERIMENT_ARMS

    async def load(db, pid: str):
        p = await db.get(CredibilityPass, uuid.UUID(pid))
        if p is None:
            raise cr.CredibilityError("Pass not found", 404)
        return p

    async def go() -> tuple[int, str]:
        factory = get_session_factory()
        async with factory() as db:
            if cmd == "preview":
                est = await cr.preview_pass(db, workspace_id=uuid.UUID(args.workspace), arms=arms(), budget_usd=args.budget, **selection())
                return 0, json.dumps(est, indent=2) if args.json else render_credibility_preview(est)
            if cmd == "create":
                p = await cr.create_pass(db, workspace_id=uuid.UUID(args.workspace), arms=arms(), name=args.name, budget_usd=args.budget,
                                         created_by="cli", **selection())
                await db.commit()
                if args.json:
                    return 0, json.dumps(await cr.pass_detail(db, p), indent=2)
                return 0, render_credibility_preview(p.estimates, str(p.id)) + f"\n\nStart it with: tret eval credibility start {p.id} --budget USD"
            if cmd in ("show", "verify"):
                rv = await db.get(cr.CredibilityReview, uuid.UUID(args.review_id))
                if rv is None:
                    raise cr.CredibilityError("Review not found", 404)
                p = await db.get(CredibilityPass, rv.pass_id)
                if cmd == "verify":
                    v = await cr.verify_review(db, workspace_id=p.workspace_id, review_id=rv.id)
                    return (0 if v["ok"] else 1), (json.dumps(v, indent=2) if args.json else
                                                   ("OK: " if v["ok"] else "MISMATCH: ") + (v["reason"] or "the rebuilt prompt and report hashes match the stored ones"))
                r = await cr.review_detail(db, rv)
                return 0, json.dumps(r, indent=2) if args.json else render_credibility_review(r)
            p = await load(db, args.pass_id)
            ws = p.workspace_id
            if cmd == "status":
                d = await cr.pass_detail(db, p)
                return 0, json.dumps(d, indent=2) if args.json else render_credibility_status(d)
            if cmd == "reviews":
                out = await cr.list_reviews(db, p, limit=args.limit, offset=args.offset, arm=args.arm, status=args.status, score=args.score)
                if args.json:
                    return 0, json.dumps(out, indent=2)
                lines = [f"{out['total']} review(s) (showing {len(out['reviews'])}):"]
                for r in out["reviews"]:
                    score = "-" if r["credibility_score"] is None else str(r["credibility_score"])
                    lines.append(f"  {r['id']}  {r['status']:<8} arm {r['arm_key'] or '-':<8} score {score:>2}  {r.get('company') or r['disclosure_report_id']}"
                                 + (f"  [{r['error'][:60]}]" if r.get("error") else ""))
                return 0, "\n".join(lines)
            if cmd == "cancel":
                p = await cr.cancel_pass(db, workspace_id=ws, pass_id=p.id)
                return 0, f"Pass {p.id} cancelled; spent ${float(p.spent_usd or 0):.4f}."
            pid = p.id
            if cmd == "start":
                p = await cr.start_pass(db, workspace_id=ws, pass_id=pid, budget_usd=args.budget, started_by="cli", spawn=False)
            else:  # resume
                acks = [g.strip() for g in (args.acknowledge or "").split(",") if g.strip()]
                p = await cr.resume_pass(db, workspace_id=ws, pass_id=pid, budget_usd=args.budget, spawn=False,
                                         acknowledge_gates=acks or None, note=args.note, acknowledged_by="cli")
            owner = p.lease_owner
        try:
            await cr.run_pass(pid, owner)
        except (KeyboardInterrupt, asyncio.CancelledError):
            # this pass only, never another one running elsewhere
            await cr.pause_pass(pid, owner=owner, reason="interrupted")
            return 130, "Interrupted: the pass is paused. Resume it with `tret eval credibility resume`."
        async with factory() as db:
            return 0, render_credibility_status(await cr.pass_detail(db, await load(db, str(pid))))

    try:
        code, text = asyncio.run(go())
    except KeyboardInterrupt:  # a second Ctrl-C (or one outside the runner): asyncio.run re-raises it
        print("Interrupted. A pass that was running is paused or its lease will lapse; resume it with `tret eval credibility resume`.", file=sys.stderr)
        return 130
    except cr.CredibilityError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except (ValueError, json.JSONDecodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if text:
        print(text)
    return code


# ── divergence (PR 9) ───────────────────────────────────────────────────────────────────────────
def render_divergence_set(d: dict) -> str:
    c = d.get("stats") or {}
    q = d["quadrant_counts"]
    lines = [
        f"Divergence set {d['id']} ({d['name']}): {d['status']}, pass {d['pass_name'] or d['credibility_pass_id']}, modules {', '.join(d['modules'])}"
        + (f", arm {d['arm_key']}" if d["arm_key"] else ", all arms"),
        f"{c.get('n_complete', d['n_complete'])} complete report(s), r = " + ("n/a" if d["pearson"] is None else f"{d['pearson']:.2f}")
        + (f", spearman {c['spearman']:.2f}" if c.get("spearman") is not None else "")
        + f"; both strong {q.get('both_strong', 0)}, parts over whole {q.get('parts_over_whole', 0)}, "
        f"whole over parts {q.get('whole_over_parts', 0)}, both weak {q.get('both_weak', 0)}",
    ]
    for g in d["gates"]:
        v = "" if g["value"] is None else f" {g['value']:.2f}"
        lines.append(f"  gate {g['key']:<22} {_STATE_MARK.get(g['state'], g['state'])}{v} (n={g['n']}): {g['detail']}")
    sc = d["spot_checks"]
    lines.append(f"Spot checks: {sc['answered']} of {sc['total']} answered (in the web UI).")
    return "\n".join(lines)


def _divergence(args: argparse.Namespace) -> int:
    import asyncio
    import uuid
    from types import SimpleNamespace

    from tret.db.engine import get_session_factory
    from tret.db.models import DivergenceSet
    from tret.evaluation import divergence as dv

    cmd = args.divergence_command

    def dump(x) -> str:
        return json.dumps(x, indent=2, default=str)

    async def go() -> tuple[int, str]:
        async with get_session_factory()() as db:
            if cmd == "inputs":
                out = await dv.inputs_out(db, workspace_id=uuid.UUID(args.workspace))
                if args.json:
                    return 0, dump(out)
                lines = [f"{len(out['passes'])} completed pass(es):"]
                lines += [f"  {p['id']}  {p['name'] or '-'}  arms {','.join(a['key'] for a in p['arms'])}  {p['n_done']} done review(s)" for p in out["passes"]]
                lines.append(f"{len(out['tournaments'])} completed tournament(s):")
                lines += [f"  {t['id']}  {t['module']}  {t['n_rated']} rated" for t in out["tournaments"]]
                return 0, "\n".join(lines)
            if cmd in ("preview", "create"):
                sel = {"pass_id": uuid.UUID(args.pass_id), "tournament_ids": [uuid.UUID(t) for t in args.tournaments], "arm_key": args.arm}
                ws = uuid.UUID(args.workspace)
                if cmd == "preview":
                    out = await dv.preview_set(db, workspace_id=ws, **sel)
                    if args.json:
                        return 0, dump(out)
                    st, g = out["stats"], out["gate"]
                    lines = [f"Divergence preview (nothing written): {st['n_complete']} complete of {st['n_joined']} joined report(s), r = "
                             + ("n/a" if st["pearson"] is None else f"{st['pearson']:.2f}") + f", reading list {out['n_reading_list']}.",
                             f"  gate {g['key']}: {_STATE_MARK.get(g['state'], g['state'])}: {g['detail']}"]
                    return 0, "\n".join(lines + [f"  warning: {w}" for w in out["warnings"]])
                s = await dv.create_set(db, workspace=ws, user=SimpleNamespace(id=None, email="cli"), name=args.name, **sel)
                d = await dv.set_detail(db, s)
                return 0, dump(d) if args.json else render_divergence_set(d) + f"\n\nAnswer the spot checks in the web UI (Divergence page, set {s.id})."
            s = await db.get(DivergenceSet, uuid.UUID(args.set_id))
            if s is None:
                raise dv.DivergenceError("Divergence set not found", 404)
            if cmd == "show":
                d = await dv.set_detail(db, s)
                return 0, dump(d) if args.json else render_divergence_set(d)
            if cmd == "reading-list":
                rows = await dv.reading_list_out(db, s, enforce_lock=False)
                if not dv._locked(await dv.load_checks(db, s.id)):
                    print("warning: spot checks are still pending; this list shows module bands the human checks are meant to be blind to.", file=sys.stderr)
                if args.json:
                    return 0, dump(rows)
                lines = [f"{len(rows)} biggest divergence(s), read them:"]
                for r in rows:
                    rv = r.get("review") or {}
                    lines.append(f"  {r['company'] or r['disclosure_report_id']}: whole {r['whole']}, parts {r['parts']:.1f}, {r['quadrant']}, delta {r['delta']:+.2f}")
                    if rv.get("summary"):
                        lines.append(textwrap.fill(rv["summary"], 100, initial_indent="     ", subsequent_indent="     "))
                return 0, "\n".join(lines)
            out = await dv.spot_checks_out(db, s)
            if args.json:
                return 0, dump(out)
            return 0, "\n".join(f"  {c['kind']:<8} #{c['position']:<3} {c['status']}" + (f" agreement {c['agreement']:.2f}" if c["agreement"] is not None else "") for c in out)

    try:
        code, text = asyncio.run(go())
    except dv.DivergenceError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except (ValueError, json.JSONDecodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if text:
        print(text)
    return code
