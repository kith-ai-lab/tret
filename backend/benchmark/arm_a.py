"""Arm A — the harnessed runs, through tret's real API.

Drives the running tret stack exactly as the UI does: create a
divergence_assessment run on the Climate Analyst harness, wait for it to
finish, then collect the run record (verdict finding, validation events, cost,
energy). Nothing is mocked; this is the production path.

Usage (stack must be up: docker compose up):
    TRET_URL=http://localhost:8000 TRET_ADMIN_EMAIL=admin@example.com \
    TRET_ADMIN_PASSWORD=... python arm_a.py --model openrouter/openai/gpt-5.6-luna \
        --cases cases.yaml --out results/arm_a.<model>.jsonl

Output: one JSON line per case:
    {case_id, site_id, peril, model, run_id, status, verdict_payload,
     validation_error_count, data_requests, cost_usd, energy_wh, error}

NOTE (pilot checklist): the findings/data-request read paths below are written
against the API as of Aug 2026 — verify both against the live stack during the
week-2 pilot before trusting a full run.
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
HARNESS_NAME = "Climate Analyst"
TASK_TYPE = "divergence_assessment"
POLL_SECONDS = 3
TIMEOUT_SECONDS = 600
TERMINAL = {"completed", "completed_without_output", "failed", "cancelled"}


def login(client: httpx.Client, base: str) -> None:
    email = os.environ.get("TRET_ADMIN_EMAIL", "admin@example.com")
    password = os.environ.get("TRET_ADMIN_PASSWORD", "tret-admin")
    r = client.post(f"{base}/api/auth/login", json={"email": email, "password": password})
    r.raise_for_status()


def find_harness(client: httpx.Client, base: str) -> str:
    for h in client.get(f"{base}/api/harnesses").json():
        if h["name"] == HARNESS_NAME:
            return h["id"]
    raise RuntimeError(f"harness {HARNESS_NAME!r} not found — is the demo seeded?")


def run_case(client: httpx.Client, base: str, harness_id: str, model: str, case: dict) -> dict:
    r = client.post(
        f"{base}/api/runs",
        json={
            "harness_id": harness_id,
            "task_type": TASK_TYPE,
            "task_input": {"site_id": case["site_id"], "peril": case["peril"]},
            "model_override": model,
        },
    )
    r.raise_for_status()
    run_id = r.json()["run_id"]

    deadline = time.time() + TIMEOUT_SECONDS
    detail = None
    while time.time() < deadline:
        detail = client.get(f"{base}/api/runs/{run_id}").json()
        if detail.get("status") in TERMINAL:
            break
        time.sleep(POLL_SECONDS)

    # The run detail carries the transcript; findings hold the recorded verdict.
    findings = client.get(f"{base}/api/findings", params={"run_id": run_id}).json()
    verdicts = [f for f in findings if f.get("schema_slug") == "divergence_verdict"] if isinstance(findings, list) else []

    return {
        "case_id": case["id"],
        "site_id": case["site_id"],
        "peril": case["peril"],
        "arm": "A",
        "model": model,
        "run_id": run_id,
        "status": detail.get("status") if detail else "timeout",
        "verdict_payload": verdicts[0].get("payload") if verdicts else None,
        # How many grounding violations the harness caught and bounced back to
        # the model — the honest Arm-A statistic (see README limitation #3).
        "validation_error_count": _count_validation_events(client, base, run_id),
        "data_requests": _data_requests(detail),
        "cost_usd": detail.get("cost_usd") if detail else None,
        "energy_wh": detail.get("energy_wh") if detail else None,
        "input_tokens": detail.get("input_tokens") if detail else None,
        "output_tokens": detail.get("output_tokens") if detail else None,
        "error": detail.get("error") if detail else "poll timeout",
    }


def _count_validation_events(client: httpx.Client, base: str, run_id: str) -> int | None:
    """Validation rejections recorded on the run transcript.

    Read from the run detail's message log: retriable validation failures are
    surfaced to the model as tool results. Counted lexically here; the pilot
    should confirm the marker string against a live transcript.
    """
    try:
        detail = client.get(f"{base}/api/runs/{run_id}").json()
        blob = json.dumps(detail.get("messages", detail))
        return blob.count("validation error") + blob.count("cited_values[")
    except Exception:
        return None


def _data_requests(detail: dict | None) -> int | None:
    if not detail:
        return None
    blob = json.dumps(detail)
    return blob.count("file_data_request") or 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", required=True)
    ap.add_argument("--cases", default=str(HERE / "cases.yaml"))
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", help="comma-separated case ids (pilot runs)")
    args = ap.parse_args()

    base = os.environ.get("TRET_URL", "http://localhost:8000").rstrip("/")
    cases = yaml.safe_load(Path(args.cases).read_text())["cases"]
    if args.only:
        wanted = set(args.only.split(","))
        cases = [c for c in cases if c["id"] in wanted]

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    done = set()
    if out_path.exists():
        for line in out_path.read_text().splitlines():
            done.add(json.loads(line)["case_id"])

    with httpx.Client(timeout=30) as client, out_path.open("a") as out:
        login(client, base)
        harness_id = find_harness(client, base)
        for case in cases:
            if case["id"] in done:
                continue
            try:
                row = run_case(client, base, harness_id, args.model, case)
            except Exception as e:
                row = {"case_id": case["id"], "site_id": case["site_id"], "peril": case["peril"],
                       "arm": "A", "model": args.model, "run_id": None, "status": "error",
                       "verdict_payload": None, "validation_error_count": None,
                       "data_requests": None, "cost_usd": None, "energy_wh": None,
                       "input_tokens": None, "output_tokens": None, "error": str(e)}
            out.write(json.dumps(row) + "\n")
            out.flush()
            print(f"{case['id']}: {row['status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
