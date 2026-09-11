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


# ── D5: scope1_mobile is diesel or gasoline, not one flat factor ────────────

MOBILE_WORKBOOK = [
    {"record_id": "CW-002", "facility": "Alder Point", "category": "scope1_mobile",
     "activity": "diesel fleet", "quantity": "38200", "unit": "gallons",
     "data_source": "fuel card statements", "notes": "Complete"},
    {"record_id": "CW-011", "facility": "Alder Point", "category": "scope1_mobile",
     "activity": "sales fleet gasoline", "quantity": "1200", "unit": "gallons",
     "data_source": "fuel card statements", "notes": "Complete"},
    {"record_id": "CW-012", "facility": "Willow Bend", "category": "scope1_mobile",
     "activity": "petrol vans", "quantity": "800", "unit": "gallons",
     "data_source": "fuel card statements", "notes": "Complete"},
    {"record_id": "CW-013", "facility": "Cedar Landing", "category": "scope1_mobile",
     "activity": "forklifts", "quantity": "500", "unit": "gallons",
     "data_source": "fuel logs", "notes": "Fuel type not recorded on the invoice"},
]


def test_ghg_inventory_mobile_fuel_is_matched_from_activity_and_notes():
    rows = run_script("ghg_inventory.py", {}, {"carbon_workbook": MOBILE_WORKBOOK})
    by_id = {r["record_id"]: r for r in rows if r["record_id"] != "TOTAL"}

    diesel = by_id["CW-002"]
    assert diesel["fuel"] == "diesel"
    assert diesel["factor_basis"] == "diesel"
    assert diesel["factor_tco2e_per_unit"] == 0.01021
    assert diesel["emissions_tco2e"] == 390.02  # 38200 * 0.01021

    gasoline = by_id["CW-011"]
    assert gasoline["fuel"] == "gasoline"
    assert gasoline["factor_basis"] == "gasoline"
    assert gasoline["factor_tco2e_per_unit"] == 0.00878
    assert gasoline["emissions_tco2e"] == 10.54  # 1200 * 0.00878

    petrol = by_id["CW-012"]  # "petrol" matches gasoline too
    assert petrol["fuel"] == "gasoline"
    assert petrol["factor_tco2e_per_unit"] == 0.00878

    defaulted = by_id["CW-013"]  # neither word present -> diesel, flagged as defaulted
    assert defaulted["fuel"] == "diesel"
    assert defaulted["factor_basis"] == "diesel (default)"
    assert defaulted["factor_tco2e_per_unit"] == 0.01021


def test_ghg_inventory_a_gasoline_record_is_not_over_counted_at_the_diesel_rate():
    """The bug this fix closes: keying scope1_mobile on the diesel factor
    alone over-counted a gasoline record by about 16% (0.01021 vs 0.00878)."""
    rows = run_script("ghg_inventory.py", {}, {"carbon_workbook": MOBILE_WORKBOOK})
    gasoline = next(r for r in rows if r["record_id"] == "CW-011")
    at_the_diesel_rate = round(1200 * 0.01021, 2)
    assert gasoline["emissions_tco2e"] < at_the_diesel_rate
    assert gasoline["emissions_tco2e"] == round(1200 * 0.00878, 2)


def test_ghg_inventory_non_mobile_rows_carry_no_fuel_or_basis():
    """`fuel`/`factor_basis` are new fields on every row (schema
    consistency) but only scope1_mobile ever populates them."""
    rows = run_script("ghg_inventory.py", {}, {"carbon_workbook": WORKBOOK})
    stationary = next(r for r in rows if r["record_id"] == "CW-001")
    assert stationary["fuel"] is None
    assert stationary["factor_basis"] is None


def test_ghg_inventory_totals_are_unchanged_for_the_existing_all_diesel_sample_data():
    """D5's fix must not move the numbers this pack's shipped sample data
    (packs/climate-risk/sample-data/evidence/acme-carbon-workbook.csv) has
    always produced — every one of its scope1_mobile rows is diesel."""
    diesel_only = WORKBOOK + [
        {"record_id": "CW-002", "facility": "Alder Point", "category": "scope1_mobile",
         "activity": "diesel fleet", "quantity": "38200", "unit": "gallons",
         "data_source": "fuel card statements", "notes": "Complete"},
        {"record_id": "CW-005", "facility": "Cedar Landing", "category": "scope1_mobile",
         "activity": "yard tractors diesel", "quantity": "9100", "unit": "gallons",
         "data_source": "fuel card statements", "notes": "Complete"},
    ]
    rows = run_script("ghg_inventory.py", {}, {"carbon_workbook": diesel_only})
    s1 = next(r for r in rows if r["category"] == "scope1_total")
    # 531.0 (stationary, unchanged) + 38200*0.01021 + 9100*0.01021, at the
    # same per-record rounding main() has always used.
    expected = 531.0 + round(38200 * 0.01021, 2) + round(9100 * 0.01021, 2)
    assert s1["emissions_tco2e"] == round(expected, 2)
