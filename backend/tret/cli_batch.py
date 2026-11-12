"""`tret batch ...`: drive batches on a running server over HTTP.

A batch executes inside the server (its runs, slots, review gate and records
all live there), so this is an httpx client of `/api/batches`, not a database
tool. The same calls the Batches page makes; see docs/batch-runs.md. The one
exception is `search-outages`, which reads the database like
`tret approvals mark-packaging` does.

Connection and auth (options on the `batch` group, accepted before or after the
subcommand):

- `--url` / `TRET_URL` (default http://localhost:8000)
- `--token` / `TRET_TOKEN`: sent as a bearer token (works when the server has
  `TRET_OIDC_API_AUDIENCE` set), or
- `--email` / `TRET_EMAIL` with `--password` / `TRET_PASSWORD`: logs in once at
  `POST /api/auth/login` and keeps the session cookie for the rest of the run.
- `--workspace <id>`: sent as `X-Tret-Workspace`; after a password login the
  session's workspace is also switched with `POST /api/auth/workspace`.
- `--json`: raw JSON instead of the short human summary.

Exit codes: 0 ok, 1 the server refused or was unreachable (its detail is
printed), 2 usage or configuration. Neither the password nor the token is ever
printed. Imports are lazy (httpx only when a command runs) so `tret --help`
stays light (tests/test_sdk_import_hygiene.py).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

DEFAULT_URL = "http://localhost:8000"
TERMINAL_STATUSES = ("completed", "finalized", "failed", "paused")
# `--watch` also stops where only a person can move the batch on.
WAITING_HINTS = {
    "gate": "waiting for a person: check the first targets, then `tret batch release {id}`",
    "review": "waiting for a person: findings need decisions (`tret batch approve {id} --all`)",
}
DECIDE_CHUNK = 200  # POST /api/findings/decide-batch takes at most 200 ids


class CliError(Exception):
    """A failure to report as `error: ...`; `code` is the exit status."""

    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


# ── argparse ─────────────────────────────────────────────────────────────────
def _add_common(p: argparse.ArgumentParser, *, suppress: bool) -> None:
    """The connection options. On a subcommand they use SUPPRESS so a value given
    before the subcommand is not overwritten by the subparser's default."""

    def d(value: Any = None) -> Any:
        return argparse.SUPPRESS if suppress else value

    p.add_argument("--url", default=d(), help=f"Server base URL (env TRET_URL; default {DEFAULT_URL})")
    p.add_argument("--token", default=d(), help="Bearer access token (env TRET_TOKEN)")
    p.add_argument("--email", default=d(), help="Login email (env TRET_EMAIL), with --password")
    p.add_argument("--password", default=d(), help="Login password (env TRET_PASSWORD; prefer the env var)")
    p.add_argument("--workspace", default=d(), help="Workspace id to act in (default: the account's own)")
    p.add_argument("--json", action="store_true", default=d(False), help="Print raw JSON")


def register(sub: argparse._SubParsersAction) -> None:
    """Add the `batch` group to the top-level subparsers."""
    batch = sub.add_parser(
        "batch",
        help="Create, run and review batches on a running server (see docs/batch-runs.md)",
        description="Drive batches on a running Tret server over HTTP. Connection options may "
        "come before or after the subcommand.",
    )
    _add_common(batch, suppress=False)
    bsub = batch.add_subparsers(dest="batch_command", required=True)

    def command(name: str, help_text: str) -> argparse.ArgumentParser:
        p = bsub.add_parser(name, help=help_text, description=help_text)
        _add_common(p, suppress=True)
        return p

    create = command("create", "Create a batch (draft) from a CSV; start it with `batch start`")
    create.add_argument("--profile", choices=("prompt", "disclosure"), default="prompt")
    create.add_argument("--name", required=True)
    create.add_argument("--slug", help="Run slug, 3-61 lowercase letters/digits/hyphens (default: from --name)")
    create.add_argument("--concurrency", type=int, default=3)
    create.add_argument("--gate", type=int, default=0, help="Run this many targets, hold the rest (0 = no gate)")
    create.add_argument("--target", help="SharePoint write target slug")
    create.add_argument("--write-root", help="Folder under the write target")
    create.add_argument("--model", help="Pin a model id for every run")
    create.add_argument("--max-iterations", type=int)
    prompt = create.add_argument_group("prompt profile")
    prompt.add_argument("--harness", help="Harness id that runs each row")
    prompt.add_argument("--template", help="Task text with {column} placeholders, or @file")
    prompt.add_argument("--csv", help="CSV file, header row = columns")
    prompt.add_argument("--label-column")
    prompt.add_argument("--slug-column")
    prompt.add_argument("--review", metavar="SCHEMA", help="Pause at a draft finding: 'any' or a finding type")
    disc = create.add_argument_group("disclosure profile")
    disc.add_argument("--register", help="Register CSV file")
    disc.add_argument("--fiscal-year", type=int, default=2025)
    disc.add_argument("--auto-write", action=argparse.BooleanOptionalAction, default=True)

    command("start", "Start a draft batch").add_argument("batch_id")
    status = command("status", "Show a batch")
    status.add_argument("batch_id")
    status.add_argument("--watch", action="store_true", help="Poll until completed, finalized, failed, paused, or waiting at a gate or review")
    status.add_argument("--targets", action="store_true", help="Also list every target")
    status.add_argument("--interval", type=float, default=10.0, help="Seconds between polls with --watch")
    command("release", "Release the targets held behind the first-batch gate").add_argument("batch_id")
    approve = command("approve", "Approve (or --reject) the findings awaiting review")
    approve.add_argument("batch_id")
    which = approve.add_mutually_exclusive_group()
    which.add_argument("--all", action="store_true", help="Every pending review of the batch")
    which.add_argument("--finding", action="append", metavar="ID", help="One pending finding (repeatable)")
    approve.add_argument("--reject", action="store_true")
    approve.add_argument(
        "--packaging-only", action="store_true",
        help="Approve only so the page maps can be packaged: recorded as a packaging approval, not a quality review",
    )
    approve.add_argument("--note")
    finalize = command("finalize", "Write the batch's records")
    finalize.add_argument("batch_id")
    finalize.add_argument("--force", action="store_true", help="Finalize despite I/O or storage failures")
    command("list", "List the workspace's batches")
    outages = bsub.add_parser(
        "search-outages",
        help="List (and with --mark --apply, mark) targets of earlier runs that look like search-outage victims",
        description="Read the database directly, like `tret approvals mark-packaging`: no server and no "
        "connection options. Lists gap or failed targets that delivered nothing and whose stored "
        "transcript shows at least two web searches, none with results. Writes nothing unless "
        "--mark and --apply are both given, and then marks only the named targets.",
    )
    outages.add_argument("--batch", action="append", required=True, metavar="BATCH_ID", help="Batch to scan (repeatable)")
    outages.add_argument("--mark", action="append", metavar="TARGET_ID", help="Target to mark (repeatable)")
    outages.add_argument("--apply", action="store_true", help="With --mark: write the marks (default: say what would happen)")


# ── HTTP ─────────────────────────────────────────────────────────────────────
def _detail(response: Any) -> str:
    try:
        data = response.json()
    except ValueError:
        return (response.text or "").strip()[:300] or response.reason_phrase
    detail = data.get("detail", data) if isinstance(data, dict) else data
    return detail if isinstance(detail, str) else json.dumps(detail)


class Api:
    def __init__(self, args: argparse.Namespace):
        import httpx

        self._httpx = httpx
        self.url = (args.url or os.environ.get("TRET_URL") or DEFAULT_URL).rstrip("/")
        token = args.token or os.environ.get("TRET_TOKEN")
        email = args.email or os.environ.get("TRET_EMAIL")
        password = args.password or os.environ.get("TRET_PASSWORD")
        workspace = args.workspace
        if not token and not (email and password):
            raise CliError(
                "no credentials: give --token (TRET_TOKEN) or --email and --password "
                "(TRET_EMAIL, TRET_PASSWORD)",
                2,
            )
        if token and (email or password):
            raise CliError("give either a token or an email and password, not both", 2)
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if workspace:
            headers["X-Tret-Workspace"] = workspace
        # An operator CLI talking to its own server, not model-chosen egress: the one
        # client built outside tret/net/ (allowlisted in tests/test_egress_chokepoint.py).
        self.client = httpx.Client(base_url=self.url, headers=headers, timeout=60.0)
        if not token:
            self.request("POST", "/api/auth/login", json={"email": email, "password": password}, what="login")
            if workspace:
                self.request("POST", "/api/auth/workspace", json={"workspace_id": workspace}, what="workspace switch")

    def request(self, method: str, path: str, *, what: str | None = None, **kw: Any) -> Any:
        try:
            r = self.client.request(method, path, **kw)
        except self._httpx.HTTPError as e:
            raise CliError(f"cannot reach {self.url}: {type(e).__name__}") from None
        if r.status_code >= 400:
            prefix = f"{what} failed: " if what else ""
            raise CliError(f"{prefix}{r.status_code} {_detail(r)}")
        return r.json() if r.content else {}

    def close(self) -> None:
        self.client.close()


# ── output ───────────────────────────────────────────────────────────────────
def _counts_line(batch: dict) -> str:
    counts = batch.get("counts") or {}
    by = {k: v for k, v in (counts.get("by_status") or {}).items() if v}
    parts = ", ".join(f"{v} {k}" for k, v in by.items()) or "no targets"
    return f"{counts.get('total', 0)} targets: {parts}"


def _summary(batch: dict) -> str:
    cost = batch.get("cost_usd") or 0
    lines = [
        f"{batch['id']}  {batch['name']}  [{batch.get('profile', 'disclosure')}]  {batch['status']}",
        f"  {_counts_line(batch)}  cost ${cost:.4f}",
    ]
    pending = batch.get("pending_reviews") or []
    if pending:
        lines.append(f"  {len(pending)} finding(s) awaiting review (tret batch approve {batch['id']} --all)")
    if batch.get("last_error"):
        lines.append(f"  note: {batch['last_error']}")
    for w in (batch.get("warnings") or [])[:5]:
        lines.append(f"  warning: {w}")
    fin = batch.get("finalize") or {}
    for f in fin.get("files") or []:
        lines.append(f"  record: {f.get('name')}  {f.get('web_url') or f.get('document_id') or ''}".rstrip())
    return "\n".join(lines)


def _targets_table(batch: dict) -> str:
    rows = [f"  {'#':>3}  {'status':<10} {'tries':>5} {'cost':>9}  label"]
    for t in batch.get("targets") or []:
        err = f"  ({t['error'][:60]})" if t.get("error") else ""
        rows.append(
            f"  {t['position']:>3}  {t['status']:<10} {t.get('attempts', 0):>5} "
            f"{(t.get('cost_usd') or 0):>9.4f}  {t['label']}{err}"
        )
    return "\n".join(rows)


def _emit(args: argparse.Namespace, data: Any, human: str) -> None:
    print(json.dumps(data) if args.json else human)


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:61].strip("-")


def _read(path: str, what: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8-sig")
    except OSError as e:
        raise CliError(f"cannot read {what} {path}: {e.strerror or type(e).__name__}", 2) from None


# ── commands ─────────────────────────────────────────────────────────────────
def _create_body(args: argparse.Namespace) -> dict:
    slug = args.slug or _slug(args.name)
    body: dict[str, Any] = {
        "profile": args.profile,
        "name": args.name,
        "run_slug": slug,
        "concurrency": args.concurrency,
        "first_batch_size": args.gate,
        "model_override": args.model,
        "max_iterations": args.max_iterations,
    }
    if args.profile == "prompt":
        missing = [f for f, v in (("--harness", args.harness), ("--template", args.template), ("--csv", args.csv)) if not v]
        if missing:
            raise CliError(f"a prompt batch needs {', '.join(missing)}", 2)
        template = args.template
        if template.startswith("@"):
            template = _read(template[1:], "template file")
        body.update(
            harness_id=args.harness,
            template=template,
            csv=_read(args.csv, "CSV"),
            label_column=args.label_column,
            slug_column=args.slug_column,
            review_schema=args.review,
            target_slug=args.target,
            write_root=args.write_root,
        )
    else:
        if not args.register or not args.target:
            raise CliError("a disclosure batch needs --register and --target", 2)
        body.update(
            register_csv=_read(args.register, "register"),
            target_slug=args.target,
            fiscal_year=args.fiscal_year,
            auto_write=args.auto_write,
        )
        if args.write_root:
            body["write_root"] = args.write_root
    return body


def _cmd_create(api: Api, args: argparse.Namespace) -> None:
    batch = api.request("POST", "/api/batches", json=_create_body(args))
    _emit(args, batch, _summary(batch) + f"\nstart it with: tret batch start {batch['id']}")


def _cmd_start(api: Api, args: argparse.Namespace) -> None:
    batch = api.request("POST", f"/api/batches/{args.batch_id}/start")
    _emit(args, batch, _summary(batch))


def _cmd_release(api: Api, args: argparse.Namespace) -> None:
    batch = api.request("POST", f"/api/batches/{args.batch_id}/release")
    _emit(args, batch, _summary(batch))


def _cmd_finalize(api: Api, args: argparse.Namespace) -> None:
    batch = api.request("POST", f"/api/batches/{args.batch_id}/finalize", json={"force": bool(args.force)})
    _emit(args, batch, _summary(batch))


def _cmd_status(api: Api, args: argparse.Namespace) -> None:
    while True:
        batch = api.request("GET", f"/api/batches/{args.batch_id}")
        human = _summary(batch) + (("\n" + _targets_table(batch)) if args.targets else "")
        if args.watch and not args.json:
            human = f"{batch['status']}: {_counts_line(batch)}  cost ${(batch.get('cost_usd') or 0):.4f}"
        _emit(args, batch, human)
        sys.stdout.flush()
        if not args.watch or batch["status"] in TERMINAL_STATUSES or batch["status"] in WAITING_HINTS:
            if args.watch and not args.json:
                print(_summary(batch) + (("\n" + _targets_table(batch)) if args.targets else ""))
            if args.watch and batch["status"] in WAITING_HINTS:
                print(WAITING_HINTS[batch["status"]].format(id=batch["id"]), file=sys.stderr)
            if args.watch and batch["status"] == "failed":
                raise CliError(f"batch {batch['id']} failed", 1)
            return
        time.sleep(max(0.0, args.interval))


def _cmd_approve(api: Api, args: argparse.Namespace) -> None:
    if args.packaging_only and args.reject:
        raise CliError("--packaging-only cannot be combined with --reject: a rejection is always a review", 2)
    if not args.all and not args.finding:
        raise CliError("choose --all or --finding ID", 2)
    batch = api.request("GET", f"/api/batches/{args.batch_id}")
    pending = [p["id"] for p in batch.get("pending_reviews") or []]
    if args.all:
        ids = pending
    else:
        ids = list(dict.fromkeys(args.finding))
        unknown = [i for i in ids if i not in pending]
        if unknown:
            raise CliError(f"not awaiting review in this batch: {', '.join(unknown)}")
    if not ids:
        _emit(args, {"results": []}, "nothing is awaiting review")
        return
    action = "reject" if args.reject else "approve"
    results: list[dict] = []
    for i in range(0, len(ids), DECIDE_CHUNK):
        out = api.request(
            "POST",
            "/api/findings/decide-batch",
            json={
                "finding_ids": ids[i : i + DECIDE_CHUNK],
                "action": action,
                "note": args.note,
                **({"purpose": "packaging"} if args.packaging_only else {}),
            },
        )
        results.extend(out.get("results") or [])
    bad = [r for r in results if not r.get("ok")]
    human = f"{action}d {len(results) - len(bad)} of {len(results)} finding(s)"
    _emit(args, {"action": action, "results": results}, human)
    if bad:
        for r in bad:
            print(f"error: {r.get('id')}: {r.get('error')}", file=sys.stderr)
        raise CliError(f"{len(bad)} finding(s) could not be decided")


def _cmd_list(api: Api, args: argparse.Namespace) -> None:
    data = api.request("GET", "/api/batches")
    batches = data.get("batches") or []
    lines = [f"{'id':<36}  {'status':<10} {'profile':<10} {'targets':>7} {'cost':>9}  name"]
    for b in batches:
        lines.append(
            f"{b['id']:<36}  {b['status']:<10} {b.get('profile', ''):<10} "
            f"{(b.get('counts') or {}).get('total', 0):>7} {(b.get('cost_usd') or 0):>9.4f}  {b['name']}"
        )
    _emit(args, data, "\n".join(lines) if batches else "no batches")


_COMMANDS = {
    "create": _cmd_create,
    "start": _cmd_start,
    "status": _cmd_status,
    "release": _cmd_release,
    "approve": _cmd_approve,
    "finalize": _cmd_finalize,
    "list": _cmd_list,
}


def _search_outages(args: argparse.Namespace) -> int:
    """`tret batch search-outages`: candidates, a dry run, or the marks. Exit 1 if
    anything was refused, an id was unknown or unparseable, or a batch was not found."""
    import asyncio
    import uuid

    from tret.batches import search_outage
    from tret.db.engine import get_session_factory
    from tret.db.models import Batch

    def parse(values: list[str] | None, what: str) -> tuple[list[str], list[str]]:
        good: list[str] = []
        bad: list[str] = []
        for v in values or []:
            try:
                u = str(uuid.UUID(str(v)))
            except ValueError:
                bad.append(f"{what} {v}: not a valid id")
                continue
            if u not in good:
                good.append(u)
        return good, bad

    if args.apply and not args.mark:
        print("usage: tret batch search-outages: error: --apply needs at least one --mark", file=sys.stderr)
        return 2
    batches, bad = parse(args.batch, "batch")
    marks, bad_marks = parse(args.mark, "target")
    problems = bad + bad_marks
    for line in problems:
        print(line, file=sys.stderr)

    async def go():
        async with get_session_factory()() as db:
            if not marks:
                for b in batches:
                    if await db.get(Batch, uuid.UUID(b)) is None:
                        print(f"{b}: not found, skipped", file=sys.stderr)
                        problems.append(b)
                return await search_outage.transcript_candidates(db, batches), None
            return None, await search_outage.mark_transcript_outages(db, batches, marks, dry_run=not args.apply)

    cands, res = asyncio.run(go())
    if res is None:
        header = f"  {'batch':<36}  {'target':<36}  {'status':<7} {'srch':>4} {'none':>4} {'err':>3}  label"
        if cands:
            print(header)
        for c in cands:
            tail = f"  | {c['summary']}" if c["summary"] else ""
            print(
                f"  {c['batch_id']:<36}  {c['target_id']:<36}  {c['status']:<7} "
                f"{c['searches']:>4} {c['nothing']:>4} {c['error']:>3}  {c['label']}{tail}"
            )
        print(f"{len(cands)} candidate(s); nothing was written. Mark with --mark <target id> --apply")
        return 1 if problems else 0
    refused_all = list(res["refused"])
    for b in res["batches"]:
        if not b["found"]:
            print(f"{b['batch_id']}: not found, nothing changed")
            continue
        if args.apply:
            tail = f", {b['rechecked']} already marked and re-checked" if b["rechecked"] else ""
            print(f"{b['batch_id']}: {b['marked']} marked, {len(b['refused'])} refused{tail}")
        else:
            print(f"{b['batch_id']}: would mark {b['marked']}, would refuse {len(b['refused'])}")
    for m in res["marked"]:
        verb = "marked" if args.apply else "would mark"
        print(f"  {verb} {m['target_id']}  {m['label']}")
    for m in res["rechecked"]:
        print(f"  already marked, re-checked {m['target_id']}  {m['label']}")
    for tid in res["human_reviewed"]:
        print(
            f"  {tid}: a person's review is kept on the evaluation row, but the row no longer "
            "counts as a spot check"
        )
    seen = set()
    for r in refused_all:
        if r["target_id"] in seen:
            continue
        seen.add(r["target_id"])
        print(f"  refused {r['target_id']}: {r['reason']}", file=sys.stderr)
    if not args.apply:
        print("dry run: nothing written; add --apply to mark")
    elif res["marked"] or res["rechecked"]:
        print(
            "A running server picks up the re-scored record within about 60 seconds "
            "(its priors cache lifetime)."
        )
    unfound = any(not b["found"] for b in res["batches"])
    return 1 if (refused_all or problems or unfound) else 0


def run(args: argparse.Namespace) -> int:
    """Execute the parsed `batch` command; returns the exit status."""
    if args.batch_command == "search-outages":
        return _search_outages(args)
    api = None
    try:
        api = Api(args)
        _COMMANDS[args.batch_command](api, args)
        return 0
    except CliError as e:
        print(f"error: {e}", file=sys.stderr)
        return e.code
    except KeyboardInterrupt:
        return 130
    finally:
        if api is not None:
            api.close()
