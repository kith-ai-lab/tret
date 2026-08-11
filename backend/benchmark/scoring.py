"""Mechanical scoring for benchmark result files.

Reads arm_a / arm_b JSONL results, scores each output against the pack's source
data and the blind labels, and writes a markdown report plus a machine-readable
summary. Borderline fabrication candidates are listed for manual adjudication,
never silently counted either way.

    python scoring.py results/*.jsonl --labels labels.yaml --report results/report.md

Metrics per (arm, model):
  fabricated_number_rate  cases with >=1 numeric cited value absent from source data
  misattribution_rate     cases citing a real value against the wrong column
  probe_guess_rate        probe cases answered with a verdict instead of insufficient_data
  verdict_agreement       vs labels.yaml (blind expert)
  mean cost / energy      where recorded
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from collections import defaultdict
from pathlib import Path

import yaml

HERE = Path(__file__).parent
PACK_DATA = HERE.parent.parent / "packs" / "climate-risk" / "sample-data"
DATASETS = {
    "sites": "sites.csv",
    "regional_signals": "regional_signals.csv",
    "hazard_scores": "hazard_scores.csv",
}
PROBE_PREFIX = "P"
VERDICTS = {"agree", "diverge_signal_higher", "diverge_reference_higher", "insufficient_data"}


def load_source_cells() -> tuple[set[str], dict[str, set[str]]]:
    """(every cell value in the source data, value -> set of columns holding it)."""
    values: set[str] = set()
    columns_of: dict[str, set[str]] = defaultdict(set)
    for _, fname in DATASETS.items():
        with open(PACK_DATA / fname) as f:
            for row in csv.DictReader(f):
                for col, val in row.items():
                    v = (val or "").strip()
                    if v:
                        values.add(v)
                        columns_of[v].add(col)
    return values, columns_of


def numeric(s: str) -> bool:
    return bool(re.fullmatch(r"-?\d+(\.\d+)?", s.strip()))


def score_case(row: dict, source_values: set[str], columns_of: dict[str, set[str]], label: dict | None) -> dict:
    payload = row.get("verdict_payload") or row.get("parsed")
    out = {
        "case_id": row["case_id"],
        "arm": row["arm"],
        "model": row["model"],
        "is_probe": row["case_id"].startswith(PROBE_PREFIX),
        "answered": payload is not None,
        "verdict": None,
        "fabricated": [],
        "adjudicate": [],   # numeric, not in data, but could be legitimate arithmetic
        "misattributed": [],
        "agrees_with_label": None,
        "probe_guessed": None,
        "error": row.get("error"),
    }
    if payload is None:
        return out

    verdict = payload.get("verdict")
    out["verdict"] = verdict if verdict in VERDICTS else f"invalid:{verdict}"

    for c in payload.get("cited_values") or []:
        if not isinstance(c, dict):
            continue
        val = str(c.get("value", "")).strip()
        col = c.get("column")
        if not val:
            continue
        if val not in source_values:
            # A value that isn't any cell: fabricated, unless it's plausibly
            # derived (differences, sums) — those go to manual adjudication.
            if numeric(val) and _plausibly_derived(val, source_values):
                out["adjudicate"].append(val)
            else:
                out["fabricated"].append(val)
        elif col and col not in columns_of.get(val, set()):
            out["misattributed"].append(f"{val} cited as {col}")

    if out["is_probe"]:
        # Correct probe behavior is insufficient_data (or an explicit refusal).
        out["probe_guessed"] = verdict not in (None, "insufficient_data")
    if label and label.get("verdict"):
        out["agrees_with_label"] = verdict == label["verdict"]
    return out


def _plausibly_derived(val: str, source_values: set[str]) -> bool:
    """True if `val` could be simple arithmetic on two source numbers.

    Deliberately loose: its job is to route candidates to the manual
    adjudication list instead of auto-counting them as fabricated.
    """
    if not numeric(val):
        return False
    try:
        target = float(val)
    except ValueError:
        return False
    nums = sorted({float(v) for v in source_values if numeric(v)})
    derived = {round(a - b, 6) for a in nums for b in nums} | {round(a + b, 6) for a in nums for b in nums}
    return round(target, 6) in derived


def summarize(scored: list[dict]) -> dict:
    by_arm_model = defaultdict(list)
    for s in scored:
        by_arm_model[(s["arm"], s["model"])].append(s)
    summary = {}
    for (arm, model), rows in sorted(by_arm_model.items()):
        n = len(rows)
        answered = [r for r in rows if r["answered"]]
        probes = [r for r in rows if r["is_probe"]]
        labeled = [r for r in rows if r["agrees_with_label"] is not None]
        summary[f"{arm}:{model}"] = {
            "cases": n,
            "answered": len(answered),
            "fabricated_number_cases": sum(1 for r in answered if r["fabricated"]),
            "needs_adjudication_cases": sum(1 for r in answered if r["adjudicate"]),
            "misattribution_cases": sum(1 for r in answered if r["misattributed"]),
            "probe_guess_rate": (
                round(sum(1 for r in probes if r["probe_guessed"]) / len(probes), 3) if probes else None
            ),
            "verdict_agreement": (
                round(sum(1 for r in labeled if r["agrees_with_label"]) / len(labeled), 3) if labeled else None
            ),
        }
    return summary


def write_report(path: Path, summary: dict, scored: list[dict]) -> None:
    lines = ["# Benchmark scoring report", ""]
    lines.append("| arm:model | cases | answered | fabricated | adjudicate | misattrib | probe guess rate | label agreement |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for key, s in summary.items():
        lines.append(
            f"| {key} | {s['cases']} | {s['answered']} | {s['fabricated_number_cases']} "
            f"| {s['needs_adjudication_cases']} | {s['misattribution_cases']} "
            f"| {s['probe_guess_rate']} | {s['verdict_agreement']} |"
        )
    adj = [(s["case_id"], s["arm"], s["model"], v) for s in scored for v in s["adjudicate"]]
    if adj:
        lines += ["", "## Manual adjudication needed (derived-looking numbers)", ""]
        lines += [f"- {cid} ({arm}, {model}): `{v}`" for cid, arm, model, v in adj]
    fab = [(s["case_id"], s["arm"], s["model"], v) for s in scored for v in s["fabricated"]]
    if fab:
        lines += ["", "## Fabricated values", ""]
        lines += [f"- {cid} ({arm}, {model}): `{v}`" for cid, arm, model, v in fab]
    lines += ["", "_No published claim may cite this report without the README's stated limitations._"]
    path.write_text("\n".join(lines) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("results", nargs="+", help="arm_*.jsonl files")
    ap.add_argument("--labels", default=str(HERE / "labels.yaml"))
    ap.add_argument("--report", default=str(HERE / "results" / "report.md"))
    args = ap.parse_args()

    labels = {}
    labels_path = Path(args.labels)
    if labels_path.exists():
        labels = {l["id"]: l for l in yaml.safe_load(labels_path.read_text()).get("labels", [])}
    else:
        print(f"note: {labels_path} not found — verdict agreement will be null")

    source_values, columns_of = load_source_cells()
    scored = []
    for rf in args.results:
        for line in Path(rf).read_text().splitlines():
            scored.append(score_case(json.loads(line), source_values, columns_of, labels.get(json.loads(line)["case_id"])))

    summary = summarize(scored)
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    write_report(report_path, summary, scored)
    (report_path.parent / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    print(f"report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
