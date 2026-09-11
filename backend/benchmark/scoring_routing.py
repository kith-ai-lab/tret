"""Scoring for `arm_routing.py`'s output: per-configuration aggregates, plus
the router-versus-pinned comparison the whole arm exists to produce.

Correctness reuses `scoring.score_case` unmodified — `arm_routing.py` rows
carry `arm`/`model` (set to `"routing"`/the config label) precisely so a
routing row scores exactly like an Arm A/B row.

    python scoring_routing.py results/routing.*.jsonl --labels labels.yaml \
        --summary results/routing_summary.json

Per-configuration metrics (one row per `config`, e.g. `auto:quality` or
`pinned:<model_id>`):
  cases              rows recorded for this configuration
  delivered_rate     completed AND produced a verdict payload
  verdict_agreement  vs labels.yaml (blind expert), same test `scoring.py` uses
  mean_cost_usd / mean_reported_cost_usd
  cache_hit_ratio    cache_read_tokens / (cache_read_tokens + input_tokens)
  mean_iterations
  switch_rate        share of rows with at least one mid-run model switch
  effort_distribution  share of rows at each recorded reasoning-effort level

Router-vs-pinned: for each `auto:<objective>` configuration, its cost
(reported cost, catalog cost as a fallback, plus the router's own mean
overhead) and verdict agreement relative to two of the run's own pinned
configurations — whichever pin scored the best agreement, and whichever pin
was cheapest. Present only when at least one pinned configuration exists in
the input.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

import yaml

from scoring import load_source_cells, score_case

HERE = Path(__file__).parent


def _mean(values: list) -> float | None:
    present = [v for v in values if v is not None]
    return round(sum(present) / len(present), 6) if present else None


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def score_rows(rows: list[dict], labels: dict) -> list[dict]:
    source_values, columns_of = load_source_cells()
    return [score_case(row, source_values, columns_of, labels.get(row["case_id"])) for row in rows]


def summarize_by_config(rows: list[dict], scored: list[dict]) -> dict:
    """Per-`config` aggregate. `rows` and `scored` must be the same length and
    in the same order — `scored[i]` is `score_case(rows[i], ...)` — same
    convention `scoring.summarize` uses for its own `scored` list.

    Every field read off `rows` is read with `.get`, defaulting missing keys
    the same way a missing/`None` value is treated: an older result file
    (recorded before the cache ledger, switches, or effort existed) summarizes
    rather than raising.
    """
    by_config: dict[str, list[tuple[dict, dict]]] = defaultdict(list)
    for row, s in zip(rows, scored):
        by_config[row.get("config")].append((row, s))

    summary = {}
    for config, pairs in sorted(by_config.items(), key=lambda kv: (kv[0] is None, kv[0])):
        n = len(pairs)
        delivered = sum(1 for row, s in pairs if row.get("status") == "completed" and s.get("answered"))
        labeled = [s for _, s in pairs if s.get("agrees_with_label") is not None]
        cache_read_total = sum((row.get("cache_read_tokens") or 0) for row, _ in pairs)
        input_total = sum((row.get("input_tokens") or 0) for row, _ in pairs)
        cache_denominator = cache_read_total + input_total
        switched = sum(1 for row, _ in pairs if (row.get("switches_count") or 0) > 0)
        efforts = Counter(row.get("effort") for row, _ in pairs if row.get("effort") is not None)

        summary[config] = {
            "cases": n,
            "delivered_rate": _rate(delivered, n),
            "verdict_agreement": (
                _rate(sum(1 for s in labeled if s["agrees_with_label"]), len(labeled))
                if labeled else None
            ),
            "mean_cost_usd": _mean([row.get("cost_usd") for row, _ in pairs]),
            "mean_reported_cost_usd": _mean([row.get("reported_cost_usd") for row, _ in pairs]),
            "cache_hit_ratio": round(cache_read_total / cache_denominator, 4) if cache_denominator else None,
            "mean_iterations": _mean([row.get("iterations") for row, _ in pairs]),
            "switch_rate": _rate(switched, n),
            "effort_distribution": {k: round(v / n, 4) for k, v in efforts.items()} if n else {},
        }
    return summary


def _router_cost(s: dict) -> float | None:
    """A configuration's own cost basis: the provider-reported figure when
    one was recorded, the catalog-priced figure otherwise — same fallback
    `tret/api/runs.py`'s `reported_cost_usd` field documents.
    """
    return s["mean_reported_cost_usd"] if s["mean_reported_cost_usd"] is not None else s["mean_cost_usd"]


def router_vs_pinned(config_summary: dict, rows: list[dict]) -> dict:
    """For each `auto:*` configuration in `config_summary`: cost (with the
    router's own mean overhead folded in) and verdict agreement, relative to
    whichever pinned configuration scored the best agreement and whichever
    was cheapest. `{}` when `config_summary` carries no pinned configuration
    to compare against.
    """
    pins = {c: s for c, s in config_summary.items() if isinstance(c, str) and c.startswith("pinned:")}
    autos = {c: s for c, s in config_summary.items() if isinstance(c, str) and c.startswith("auto:")}
    if not pins:
        return {}

    best_agreement_pin = max(pins, key=lambda c: pins[c]["verdict_agreement"] if pins[c]["verdict_agreement"] is not None else -1)
    priced_pins = [c for c in pins if _router_cost(pins[c]) is not None]
    cheapest_pin = min(priced_pins, key=lambda c: _router_cost(pins[c])) if priced_pins else None

    overhead_by_config: dict[str, list] = defaultdict(list)
    for row in rows:
        overhead_by_config[row.get("config")].append(row.get("router_overhead_usd"))

    def _delta(entry: dict, other_config: str) -> dict:
        other = pins[other_config]
        own_cost = entry["router_cost_with_overhead_usd"]
        other_cost = _router_cost(other)
        own_agreement = entry["verdict_agreement"]
        other_agreement = other["verdict_agreement"]
        return {
            "pin": other_config,
            "cost_delta_usd": (
                round(own_cost - other_cost, 6) if own_cost is not None and other_cost is not None else None
            ),
            "agreement_delta": (
                round(own_agreement - other_agreement, 4)
                if own_agreement is not None and other_agreement is not None
                else None
            ),
        }

    table = {}
    for config, s in autos.items():
        router_cost = _router_cost(s)
        router_overhead = _mean(overhead_by_config.get(config, []))
        router_cost_with_overhead = (
            round(router_cost + (router_overhead or 0), 6) if router_cost is not None else None
        )
        entry = {
            "router_overhead_usd": router_overhead,
            "router_cost_with_overhead_usd": router_cost_with_overhead,
            "verdict_agreement": s["verdict_agreement"],
        }
        entry["vs_best_agreement_pin"] = _delta(entry, best_agreement_pin)
        if cheapest_pin is not None:
            entry["vs_cheapest_pin"] = _delta(entry, cheapest_pin)
        table[config] = entry
    return table


def render_markdown(config_summary: dict, comparison: dict) -> str:
    lines = ["# Routing benchmark summary", ""]
    lines.append(
        "| config | cases | delivered | agreement | mean cost | mean reported cost "
        "| cache hit ratio | mean iters | switch rate | effort mix |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for config, s in config_summary.items():
        effort_mix = ", ".join(f"{k}:{v}" for k, v in s["effort_distribution"].items()) or "-"
        lines.append(
            f"| {config} | {s['cases']} | {s['delivered_rate']} | {s['verdict_agreement']} "
            f"| {s['mean_cost_usd']} | {s['mean_reported_cost_usd']} | {s['cache_hit_ratio']} "
            f"| {s['mean_iterations']} | {s['switch_rate']} | {effort_mix} |"
        )
    if comparison:
        lines += [
            "",
            "## Router vs pinned",
            "",
            "| objective | agreement | overhead | vs best-agreement pin (cost Δ / agreement Δ) "
            "| vs cheapest pin (cost Δ / agreement Δ) |",
            "|---|---|---|---|---|",
        ]
        for config, entry in comparison.items():
            best = entry["vs_best_agreement_pin"]
            cheap = entry.get("vs_cheapest_pin")
            cheap_cell = f"{cheap['pin']}: {cheap['cost_delta_usd']} / {cheap['agreement_delta']}" if cheap else "-"
            lines.append(
                f"| {config} | {entry['verdict_agreement']} | {entry['router_overhead_usd']} "
                f"| {best['pin']}: {best['cost_delta_usd']} / {best['agreement_delta']} "
                f"| {cheap_cell} |"
            )
    lines += ["", "_No published claim may cite this report without labels.yaml and the caveat", "that every run here cost real money._"]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("results", nargs="+", help="arm_routing.py JSONL result files")
    ap.add_argument("--labels", default=str(HERE / "labels.yaml"))
    ap.add_argument("--summary", default=str(HERE / "results" / "routing_summary.json"))
    args = ap.parse_args()

    labels = {}
    labels_path = Path(args.labels)
    if labels_path.exists():
        labels = {row["id"]: row for row in yaml.safe_load(labels_path.read_text()).get("labels", [])}
    else:
        print(f"note: {labels_path} not found — verdict agreement will be null")

    rows = [json.loads(line) for rf in args.results for line in Path(rf).read_text().splitlines()]
    scored = score_rows(rows, labels)

    config_summary = summarize_by_config(rows, scored)
    comparison = router_vs_pinned(config_summary, rows)

    summary_path = Path(args.summary)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary = {"by_config": config_summary, "router_vs_pinned": comparison}
    summary_path.write_text(json.dumps(summary, indent=2) + "\n")

    print(render_markdown(config_summary, comparison))
    print(f"summary: {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
