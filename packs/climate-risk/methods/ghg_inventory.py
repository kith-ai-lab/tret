"""GHG inventory (Scope 1 & 2, location-based) from carbon workbook activity data.

bench method contract: JSON {"params", "inputs"} on stdin -> {"rows": [...]}
on stdout. Pure function, stdlib only.

FACTORS is the pinned demo emission-factor set (factor-set id below). It is
FICTIONAL-but-plausible for demonstration; a production pack would pin a
published factor set (e.g. national grid factors) with its vintage the same way.
"""
import json
import sys

FACTOR_SET = "demo-factors-v1"

# (category, unit) -> tCO2e per unit
FACTORS = {
    ("scope1_stationary", "therms"): 0.00531,
    ("scope1_mobile", "gallons"): 0.01021,
    ("scope1_fugitive", "pounds"): 0.629,  # refrigerant R-448A: GWP 1387 / 2204.6 lb per tonne
    ("scope2_electricity", "kWh"): 0.000385,
}


def main() -> None:
    payload = json.load(sys.stdin)
    params = payload.get("params") or {}
    records = payload.get("inputs", {}).get("carbon_workbook", [])
    if params.get("facility"):
        records = [r for r in records if r.get("facility") == params["facility"]]

    rows = []
    totals = {"scope1": 0.0, "scope2": 0.0}
    skipped = []
    for r in records:
        category = r.get("category", "")
        unit = r.get("unit", "")
        factor = FACTORS.get((category, unit))
        if factor is None:
            skipped.append(r.get("record_id", "?"))
            continue
        try:
            quantity = float(r.get("quantity", 0))
        except (TypeError, ValueError):
            skipped.append(r.get("record_id", "?"))
            continue
        emissions = round(quantity * factor, 2)
        scope = "scope1" if category.startswith("scope1") else "scope2"
        totals[scope] += emissions
        rows.append(
            {
                "record_id": r.get("record_id"),
                "facility": r.get("facility"),
                "category": category,
                "quantity": quantity,
                "unit": unit,
                "factor_tco2e_per_unit": factor,
                "emissions_tco2e": emissions,
                "factor_set": FACTOR_SET,
                "data_source": r.get("data_source"),
            }
        )

    rows.append(
        {
            "record_id": "TOTAL",
            "facility": params.get("facility", "ALL"),
            "category": "scope1_total",
            "emissions_tco2e": round(totals["scope1"], 2),
            "factor_set": FACTOR_SET,
        }
    )
    rows.append(
        {
            "record_id": "TOTAL",
            "facility": params.get("facility", "ALL"),
            "category": "scope2_location_based_total",
            "emissions_tco2e": round(totals["scope2"], 2),
            "factor_set": FACTOR_SET,
        }
    )
    rows.append(
        {
            "record_id": "TOTAL",
            "facility": params.get("facility", "ALL"),
            "category": "scope1_and_2_total",
            "emissions_tco2e": round(totals["scope1"] + totals["scope2"], 2),
            "factor_set": FACTOR_SET,
            "records_skipped_no_factor": ",".join(skipped) if skipped else "",
        }
    )
    json.dump({"rows": rows}, sys.stdout)


if __name__ == "__main__":
    main()
