"""Arm Routing — the router measured against pinned models.

Where Arm A asks "does the harness help", this arm asks a narrower question:
*given* the harness, is the router earning what it costs? For every case it
drives the real engine (through `/api/runs`, exactly like `arm_a.py`) under a
matrix of harness configurations built on top of the same base harness Arm A
uses (`Climate Analyst`):

  - `auto` routing under each objective the router supports (quality,
    balanced, token_conservation, eco)
  - `pinned` routing to each model in `--pins` (default: the catalog's
    cheapest economy-tier model and its most expensive premium-tier model —
    see `default_pins`)

Every configuration is its own temporary harness (same pack, tools, task
profile and loop config as the base harness; only `model_policy` differs),
created or updated through `POST`/`PUT /api/harnesses` so repeated runs reuse
rather than duplicate it. Adaptive behavior on the auto arms matches the
harness default in every field — escalation `on_quality`, compaction `auto`
(see `tret/adaptive.py`'s `DEFAULT_ADAPTIVE`) — except `exploration`, set
explicitly to 0: this benchmark's DB starts thin-to-empty on priors, so every
candidate would read as untried, and exploration's coin flip picking a model
*instead of* the router would spend a real, costed run contaminating the
router-vs-pins comparison this arm exists to make (see `_harness_body`).

`--turns N` (default 1) resends the *same* case as an N-turn conversation:
each turn passes the same `site_id`/`peril` task input again, with the prior
turn's own transcript threaded back in as `_history` — the same mechanism
`tret/api/chat.py` uses for a chat turn's history. Turn 2+ is therefore a
near-identical prompt appended to a growing prefix, which is exactly the
shape that should hit the prompt cache; the point of `--turns` is to make the
cache ledger (`served_by`, `cache_read_tokens`, `cache_rebuilds_expected`)
say something.

Usage (stack must be up: docker compose up):
    TRET_URL=http://localhost:8000 TRET_ADMIN_EMAIL=admin@example.com \
    TRET_ADMIN_PASSWORD=... python arm_routing.py --cases cases.yaml \
        --turns 2 --out results/routing.jsonl

Output: one JSON line per (case, configuration, turn):
    {case_id, config, config_kind, objective, pin, turn, turns_total,
     arm, model, harness_id, run_id, status, chosen_model, effort,
     context_fit_mode, fallback_used, switches_count, switches_reasons,
     effort_changes_count, provider_ignore, served_by, cost_usd,
     reported_cost_usd, router_overhead_usd, input_tokens, output_tokens,
     cache_read_tokens, cache_write_tokens, cache_ledger, compactions_count,
     iterations, verdict_payload, validation_error_count, wall_ms, error}

`config` is `"auto:<objective>"` or `"pinned:<model_id>"`; `arm` and `model`
are set to `"routing"` and `config` respectively so `scoring.score_case` (built
for Arm A/B rows keyed on `arm`/`model`) can be reused unmodified. `served_by`
is a list, one entry per `model_timeline` segment (or a single-element list
from `provider_used` for the common case of a run that never persisted a
timeline — see `tret/engine/harness.py`'s note on when `model_timeline` is
kept). `cache_ledger` is `None` whenever the run carries no `model_timeline`
to sum the per-segment `cache_rebuilds_expected`/`cache_misses_unexpected`
counters from — an older run, or a run that never switched, raised effort, or
recorded an estimate.

Resumable at the (case, config) granularity, like `arm_a.py`: a conversation
whose last turn is already recorded is skipped entirely; an interrupted
conversation (some but not all turns written) is redone from turn 1 rather
than resumed mid-conversation, since resuming would need the exact `_history`
the interrupted run last produced, which is discarded along with the process.

NOTE: costs real money — every (case, configuration, turn) triple is a real
run against a real model.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx
import yaml

HERE = Path(__file__).parent
BASE_HARNESS_NAME = "Climate Analyst"
TASK_TYPE = "divergence_assessment"
HARNESS_PREFIX = "routing-bench"
OBJECTIVES = ("quality", "balanced", "token_conservation", "eco")
POLL_SECONDS = 3
TIMEOUT_SECONDS = 600
TERMINAL = {"completed", "completed_without_output", "failed", "cancelled"}


def login(client: httpx.Client, base: str) -> None:
    email = os.environ.get("TRET_ADMIN_EMAIL", "admin@example.com")
    password = os.environ.get("TRET_ADMIN_PASSWORD", "tret-admin")
    r = client.post(f"{base}/api/auth/login", json={"email": email, "password": password})
    r.raise_for_status()


def find_base_harness(client: httpx.Client, base: str) -> dict:
    """Full detail of the base harness (`Climate Analyst`) — pack links,
    tools, loop config and prompt extra get copied onto every configuration
    harness below; only `model_policy` is replaced per configuration.
    """
    for h in client.get(f"{base}/api/harnesses").json():
        if h["name"] == BASE_HARNESS_NAME:
            return client.get(f"{base}/api/harnesses/{h['id']}").json()
    raise RuntimeError(f"harness {BASE_HARNESS_NAME!r} not found — is the demo seeded?")


def default_pins(client: httpx.Client, base: str) -> list[str]:
    """The catalog's cheapest economy-tier model and its most expensive
    premium-tier model, used when `--pins` is not given.

    The catalog carries no numeric quality score, so "strongest premium"
    stands in for the most expensive model in that tier — a proxy, not a
    benchmark result in itself, hence `--pins` to override it.
    """
    models = client.get(f"{base}/api/models").json()

    def total_price(m: dict) -> float:
        return (m.get("input_price_per_mtok") or 0) + (m.get("output_price_per_mtok") or 0)

    economy = [m for m in models if m.get("cost_tier") == "economy"]
    premium = [m for m in models if m.get("cost_tier") == "premium"]
    pins: list[str] = []
    if economy:
        pins.append(min(economy, key=total_price)["id"])
    else:
        print("warning: no economy-tier model in the catalog for the default pin", file=sys.stderr)
    if premium:
        pins.append(max(premium, key=total_price)["id"])
    else:
        print("warning: no premium-tier model in the catalog for the default pin", file=sys.stderr)
    return pins


def config_label(cfg: dict) -> str:
    if cfg["kind"] == "auto":
        return f"auto:{cfg['objective']}"
    return f"pinned:{cfg['pin']}"


def _harness_body(base_harness: dict, cfg: dict, label: str) -> dict:
    base_policy = base_harness.get("model_policy") or {}
    if cfg["kind"] == "auto":
        model_policy = {"mode": "auto", "objective": cfg["objective"]}
        if base_policy.get("allowed"):
            model_policy["allowed"] = base_policy["allowed"]
        model_policy["max_cost_tier"] = base_policy.get("max_cost_tier", "premium")
        # `adaptive` explicit, not omitted: the harness default (escalation
        # on_quality, compaction auto — tret/adaptive.py DEFAULT_ADAPTIVE)
        # applies here same as before, EXCEPT `exploration`, forced to 0.
        # These auto arms run against a fresh-ish benchmark DB, so priors are
        # thin-to-absent and every candidate would read as untried; leaving
        # exploration on would let a rare coin flip spend a real, costed
        # (case, config) run on a model picked *instead of* the router this
        # arm exists to measure, contaminating the very comparison
        # (router vs. pins) the arm is for.
        model_policy["adaptive"] = {
            "learn_from_outcomes": True,
            "context_headroom": 0.8,
            "compaction": "auto",
            "escalation": "on_quality",
            "max_switches": 1,
            "exploration": 0,
            "exploration_max_cost_tier": "economy",
        }
    else:
        model_policy = {"mode": "pinned", "model": cfg["pin"]}
    return {
        "name": f"{HARNESS_PREFIX} {label}",
        "description": f"Routing benchmark configuration {label!r}. Managed by arm_routing.py.",
        "pack_ids": base_harness.get("pack_ids") or [],
        "task_profile": base_harness.get("task_profile", TASK_TYPE),
        "system_prompt_extra": base_harness.get("system_prompt_extra"),
        "model_policy": model_policy,
        "tool_names": base_harness.get("tool_names") or [],
        "loop_config": base_harness.get("loop_config") or {},
    }


def get_or_create_harness(client: httpx.Client, base: str, base_harness: dict, cfg: dict) -> str:
    """The harness id for one configuration, created on first use and
    updated in place on every later run (so a changed catalog or a
    re-pointed base harness is picked up rather than silently stale).
    """
    label = config_label(cfg)
    body = _harness_body(base_harness, cfg, label)
    existing = {h["name"]: h["id"] for h in client.get(f"{base}/api/harnesses").json()}
    name = body["name"]
    if name in existing:
        r = client.put(f"{base}/api/harnesses/{existing[name]}", json=body)
        r.raise_for_status()
        return existing[name]
    r = client.post(f"{base}/api/harnesses", json=body)
    r.raise_for_status()
    return r.json()["id"]


def _lean_history(messages: list[dict] | None) -> list[dict]:
    """Prior user/assistant turns, tool detail stripped — the same shape
    `tret/api/chat.py` builds for a chat turn's `_history`.
    """
    return [
        {"role": m["role"], "content": m["content"], "tool_calls": [], "tool_call_id": None, "meta": {}}
        for m in (messages or [])
        if m.get("role") in ("user", "assistant") and m.get("content")
    ]


def _count_validation_events(detail: dict) -> int:
    """Same lexical heuristic as `arm_a.py`'s — see its own note on why this
    is a marker-string count, not a structured read.
    """
    blob = json.dumps(detail.get("messages", detail))
    return blob.count("validation error") + blob.count("cited_values[")


def poll_run(client: httpx.Client, base: str, run_id: str) -> tuple[dict | None, int]:
    start = time.time()
    deadline = start + TIMEOUT_SECONDS
    detail = None
    while time.time() < deadline:
        detail = client.get(f"{base}/api/runs/{run_id}").json()
        if detail.get("status") in TERMINAL:
            break
        time.sleep(POLL_SECONDS)
    return detail, int((time.time() - start) * 1000)


def _build_row(case: dict, run_id: str, detail: dict | None, verdict_payload, validation_error_count, wall_ms: int, turn: int) -> dict:
    detail = detail or {}
    routing = detail.get("routing") or {}
    context_fit = routing.get("context_fit") or {}
    switches = routing.get("switches") or []
    effort_changes = routing.get("effort_changes") or []
    segments = detail.get("model_timeline") or []
    if segments:
        served_by = [seg.get("served_by") for seg in segments]
        cache_ledger = {
            "cache_rebuilds_expected": sum(seg.get("cache_rebuilds_expected", 0) or 0 for seg in segments),
            "cache_misses_unexpected": sum(seg.get("cache_misses_unexpected", 0) or 0 for seg in segments),
        }
    else:
        served_by = [detail["provider_used"]] if detail.get("provider_used") else []
        cache_ledger = None
    overhead = detail.get("overhead") or {}
    compactions = detail.get("compactions") or []
    return {
        "case_id": case["id"],
        "turn": turn,
        "run_id": run_id,
        "status": detail.get("status", "timeout"),
        "chosen_model": routing.get("chosen_model"),
        "effort": routing.get("effort"),
        "context_fit_mode": context_fit.get("mode"),
        "fallback_used": routing.get("fallback_used"),
        "switches_count": len(switches),
        "switches_reasons": [s.get("reason") for s in switches],
        "effort_changes_count": len(effort_changes),
        "provider_ignore": routing.get("provider_ignore") or [],
        "served_by": served_by,
        "cost_usd": detail.get("cost_usd"),
        "reported_cost_usd": detail.get("reported_cost_usd"),
        "router_overhead_usd": overhead.get("total_cost_usd") if overhead else None,
        "input_tokens": detail.get("input_tokens"),
        "output_tokens": detail.get("output_tokens"),
        "cache_read_tokens": detail.get("cache_read_tokens"),
        "cache_write_tokens": detail.get("cache_write_tokens"),
        "cache_ledger": cache_ledger,
        "compactions_count": len(compactions),
        "iterations": detail.get("iterations"),
        "verdict_payload": verdict_payload,
        "validation_error_count": validation_error_count,
        "wall_ms": wall_ms,
        "error": detail.get("error"),
    }


def _error_row(case: dict, run_id: str | None, turn: int, err: Exception) -> dict:
    row = _build_row(case, run_id, None, None, None, None, turn)
    row["status"] = "error"
    row["error"] = str(err)
    return row


def run_turn(client: httpx.Client, base: str, harness_id: str, case: dict, history: list[dict], turn: int) -> tuple[dict, list[dict]]:
    task_input = {"site_id": case["site_id"], "peril": case["peril"]}
    if history:
        task_input["_history"] = history
    r = client.post(
        f"{base}/api/runs",
        json={"harness_id": harness_id, "task_type": TASK_TYPE, "task_input": task_input},
    )
    r.raise_for_status()
    run_id = r.json()["run_id"]

    detail, wall_ms = poll_run(client, base, run_id)
    verdict_payload = None
    validation_error_count = None
    if detail is not None:
        findings = client.get(f"{base}/api/findings", params={"run_id": run_id}).json()
        verdicts = [f for f in findings if f.get("schema_slug") == "divergence_verdict"] if isinstance(findings, list) else []
        verdict_payload = verdicts[0].get("payload") if verdicts else None
        validation_error_count = _count_validation_events(detail)

    row = _build_row(case, run_id, detail, verdict_payload, validation_error_count, wall_ms, turn)
    next_history = _lean_history(detail.get("messages")) if detail else history
    return row, next_history


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--cases", default=str(HERE / "cases.yaml"))
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", help="comma-separated case ids (pilot runs)")
    ap.add_argument(
        "--pins",
        help="comma-separated model ids to pin against auto routing; defaults to the "
        "catalog's cheapest economy-tier model and its most expensive premium-tier model",
    )
    ap.add_argument(
        "--objectives",
        default=",".join(OBJECTIVES),
        help=f"comma-separated auto objectives to test (default: all of {', '.join(OBJECTIVES)})",
    )
    ap.add_argument(
        "--turns", type=int, default=1, help="send each case as an N-turn conversation (default 1)"
    )
    args = ap.parse_args()

    if args.turns < 1:
        print("--turns must be >= 1", file=sys.stderr)
        return 2

    objectives = [o.strip() for o in args.objectives.split(",") if o.strip()]
    unknown = [o for o in objectives if o not in OBJECTIVES]
    if unknown:
        print(f"unknown objective(s): {', '.join(unknown)}", file=sys.stderr)
        return 2

    base = os.environ.get("TRET_URL", "http://localhost:8000").rstrip("/")
    cases = yaml.safe_load(Path(args.cases).read_text())["cases"]
    if args.only:
        wanted = set(args.only.split(","))
        cases = [c for c in cases if c["id"] in wanted]

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Resumable at (case, config): a conversation whose final turn is already
    # recorded is skipped whole; see the module docstring for why a partial
    # one is redone rather than resumed.
    done_final_turn: set[tuple[str, str]] = set()
    if out_path.exists():
        for line in out_path.read_text().splitlines():
            r = json.loads(line)
            if r.get("turn") == r.get("turns_total"):
                done_final_turn.add((r["case_id"], r["config"]))

    with httpx.Client(timeout=30) as client, out_path.open("a") as out:
        login(client, base)
        base_harness = find_base_harness(client, base)
        pins = [p.strip() for p in args.pins.split(",") if p.strip()] if args.pins else default_pins(client, base)
        if not pins:
            print("warning: no pins to run — set --pins explicitly", file=sys.stderr)

        configs = [{"kind": "auto", "objective": o, "pin": None} for o in objectives]
        configs += [{"kind": "pinned", "objective": None, "pin": p} for p in pins]
        harness_ids = {config_label(cfg): get_or_create_harness(client, base, base_harness, cfg) for cfg in configs}

        for case in cases:
            for cfg in configs:
                label = config_label(cfg)
                if (case["id"], label) in done_final_turn:
                    continue
                harness_id = harness_ids[label]
                history: list[dict] = []
                for turn in range(1, args.turns + 1):
                    try:
                        row, history = run_turn(client, base, harness_id, case, history, turn)
                    except Exception as e:  # record the failure, stop this conversation
                        row = _error_row(case, None, turn, e)
                        row.update(
                            config=label, config_kind=cfg["kind"], objective=cfg["objective"],
                            pin=cfg["pin"], turns_total=args.turns, arm="routing", model=label,
                        )
                        out.write(json.dumps(row) + "\n")
                        out.flush()
                        print(f"{case['id']} [{label}] turn {turn}: ERROR {e}")
                        break
                    row.update(
                        config=label, config_kind=cfg["kind"], objective=cfg["objective"],
                        pin=cfg["pin"], turns_total=args.turns, arm="routing", model=label,
                    )
                    out.write(json.dumps(row) + "\n")
                    out.flush()
                    print(f"{case['id']} [{label}] turn {turn}: {row['status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
