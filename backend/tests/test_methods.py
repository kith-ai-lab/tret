"""Deterministic method scripts — run them exactly as the runner does
(subprocess, stdin/stdout contract) but without a DB."""
import json
import subprocess
import sys
from pathlib import Path

METHODS_DIR = Path(__file__).parent.parent.parent / "packs/climate-risk/methods"


def run_script(name: str, params: dict, inputs: dict) -> list[dict]:
    proc = subprocess.run(
        [sys.executable, "-I", str(METHODS_DIR / name)],
        input=json.dumps({"params": params, "inputs": inputs}).encode(),
        capture_output=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr.decode()
    return json.loads(proc.stdout.decode())["rows"]


VERDICTS = [
    {"peril": "flood", "verdict": "diverge_signal_higher", "reason_code": "outdated_inputs", "status": "approved"},
    {"peril": "flood", "verdict": "agree", "status": "draft"},
    {"peril": "flood", "verdict": "diverge_signal_higher", "reason_code": "outdated_inputs", "status": "draft"},
    {"peril": "wind", "verdict": "insufficient_data", "status": "draft"},
]


def test_divergence_rate_aggregates():
    rows = run_script(
        "portfolio_divergence_rate.py", {}, {"findings:divergence_verdict": VERDICTS}
    )
    flood = next(r for r in rows if r["peril"] == "flood")
    assert flood["verdicts_total"] == 3
    assert flood["diverge_signal_higher"] == 2
    assert flood["divergence_rate_pct"] == 66.7
    assert flood["dominant_reason_code"] == "outdated_inputs"
    wind = next(r for r in rows if r["peril"] == "wind")
    assert wind["insufficient_data"] == 1
    assert wind["divergence_rate_pct"] == 0.0  # nothing assessable


def test_divergence_rate_status_filter():
    rows = run_script(
        "portfolio_divergence_rate.py",
        {"status": "approved"},
        {"findings:divergence_verdict": VERDICTS},
    )
    flood = next(r for r in rows if r["peril"] == "flood")
    assert flood["verdicts_total"] == 1


WORKBOOK = [
    {"record_id": "CW-001", "facility": "Alder Point", "category": "scope1_stationary",
     "quantity": "100000", "unit": "therms", "data_source": "utility invoices"},
    {"record_id": "CW-006", "facility": "Alder Point", "category": "scope2_electricity",
     "quantity": "1000000", "unit": "kWh", "data_source": "utility invoices"},
    {"record_id": "CW-099", "facility": "Alder Point", "category": "scope1_unknown",
     "quantity": "5", "unit": "widgets", "data_source": "guess"},
]


def test_ghg_inventory_totals_and_skips():
    rows = run_script("ghg_inventory.py", {}, {"carbon_workbook": WORKBOOK})
    s1 = next(r for r in rows if r["category"] == "scope1_total")
    s2 = next(r for r in rows if r["category"] == "scope2_location_based_total")
    total = next(r for r in rows if r["category"] == "scope1_and_2_total")
    assert s1["emissions_tco2e"] == 531.0  # 100000 therms * 0.00531
    assert s2["emissions_tco2e"] == 385.0  # 1M kWh * 0.000385
    assert total["emissions_tco2e"] == 916.0
    assert "CW-099" in total["records_skipped_no_factor"]  # unknown factor declared, not guessed


def test_ghg_inventory_is_deterministic():
    a = run_script("ghg_inventory.py", {}, {"carbon_workbook": WORKBOOK})
    b = run_script("ghg_inventory.py", {}, {"carbon_workbook": WORKBOOK})
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
