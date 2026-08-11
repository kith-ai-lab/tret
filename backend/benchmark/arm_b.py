"""Arm B — the unharnessed baseline.

Same model, same data, no harness: the pack's datasets and doctrine are pasted
into the prompt in full, the model is asked for the same JSON shape the harness
enforces, and nothing checks the answer. Deliberately a *strong* baseline —
full data, full doctrine, explicit permission to answer `insufficient_data`.

Usage:
    BENCH_OPENROUTER_API_KEY=... python arm_b.py --model openrouter/openai/gpt-5.6-luna \
        --cases cases.yaml --out results/arm_b.<model>.jsonl

Output: one JSON line per case:
    {case_id, site_id, peril, model, raw_text, parsed (json or null),
     input_tokens, output_tokens, error}
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
PACK = HERE.parent.parent / "packs" / "climate-risk"

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

PROMPT_TEMPLATE = """\
You are a climate risk analyst. Assess whether the forward-looking regional
climate signal and the vendor reference hazard score agree for the site and
peril below, following the assessment procedure provided.

SITE: {site_id}
PERIL: {peril}

You have the complete datasets below. Follow the procedure exactly. If a
required input is missing from the data, the honest answer is the verdict
"insufficient_data" with a description of what is missing — that is a fully
acceptable answer.

=== PROCEDURE AND RULES (follow and cite by heading) ===
{doctrine}

=== DATASET: sites ===
{sites}

=== DATASET: regional_signals ===
{signals}

=== DATASET: hazard_scores ===
{scores}

=== OUTPUT FORMAT ===
Respond with a single JSON object, no other text:
{{
  "verdict": "agree" | "diverge_signal_higher" | "diverge_reference_higher" | "insufficient_data",
  "reason_code": "site_specific_factor" | "outdated_inputs" | "methodology_choice" | "scale_mismatch" | "coverage_gap",  // only for divergence verdicts
  "confidence": "high" | "medium" | "low",
  "methodology_note": "3-6 sentences for a credit officer, plain language",
  "cited_values": [ {{ "dataset": "...", "column": "...", "value": "verbatim value from the data" }} ],
  "doctrine_citations": [ "heading you relied on" ]
}}
"""


def build_prompt(site_id: str, peril: str) -> str:
    doctrine = "\n\n".join(
        (PACK / "doctrine" / f).read_text()
        for f in ("01-assessment-principles.md", "02-divergence-procedure.md", "03-reason-codes.md")
    )
    return PROMPT_TEMPLATE.format(
        site_id=site_id,
        peril=peril,
        doctrine=doctrine,
        sites=(PACK / "sample-data" / "sites.csv").read_text(),
        signals=(PACK / "sample-data" / "regional_signals.csv").read_text(),
        scores=(PACK / "sample-data" / "hazard_scores.csv").read_text(),
    )


def parse_json_block(text: str):
    """The model was asked for bare JSON; tolerate a ```json fence around it."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        t = t.rsplit("```", 1)[0]
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        return None


def run_case(client: httpx.Client, model: str, case: dict, api_key: str) -> dict:
    # OpenRouter model ids don't carry bench's "openrouter/" prefix.
    or_model = model.removeprefix("openrouter/")
    resp = client.post(
        OPENROUTER_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": or_model,
            "temperature": 0,
            "messages": [
                {"role": "user", "content": build_prompt(case["site_id"], case["peril"])}
            ],
        },
        timeout=180,
    )
    resp.raise_for_status()
    data = resp.json()
    text = data["choices"][0]["message"]["content"]
    usage = data.get("usage", {})
    return {
        "case_id": case["id"],
        "site_id": case["site_id"],
        "peril": case["peril"],
        "arm": "B",
        "model": model,
        "raw_text": text,
        "parsed": parse_json_block(text),
        "input_tokens": usage.get("prompt_tokens"),
        "output_tokens": usage.get("completion_tokens"),
        "error": None,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", required=True, help="e.g. openrouter/openai/gpt-5.6-luna")
    ap.add_argument("--cases", default=str(HERE / "cases.yaml"))
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", help="comma-separated case ids (pilot runs)")
    args = ap.parse_args()

    api_key = os.environ.get("BENCH_OPENROUTER_API_KEY") or os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        print("Set BENCH_OPENROUTER_API_KEY", file=sys.stderr)
        return 2

    cases = yaml.safe_load(Path(args.cases).read_text())["cases"]
    if args.only:
        wanted = set(args.only.split(","))
        cases = [c for c in cases if c["id"] in wanted]

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    done = set()
    if out_path.exists():  # resumable: skip cases already recorded
        for line in out_path.read_text().splitlines():
            done.add(json.loads(line)["case_id"])

    with httpx.Client() as client, out_path.open("a") as out:
        for case in cases:
            if case["id"] in done:
                continue
            try:
                row = run_case(client, args.model, case, api_key)
            except Exception as e:  # record the failure, keep going
                row = {"case_id": case["id"], "site_id": case["site_id"], "peril": case["peril"],
                       "arm": "B", "model": args.model, "raw_text": None, "parsed": None,
                       "input_tokens": None, "output_tokens": None, "error": str(e)}
            out.write(json.dumps(row) + "\n")
            out.flush()
            print(f"{case['id']}: {'ok' if row['error'] is None else 'ERROR ' + row['error']}")
            time.sleep(1)  # be polite to the API
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
