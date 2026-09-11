"""GHG inventory (Scope 1 & 2, location-based) from carbon workbook activity data.

tret method contract: JSON {"params", "inputs"} on stdin -> {"rows": [...]}
on stdout. Pure function, stdlib only.

FACTORS is the pinned demo emission-factor set (factor-set id below) — an
EPA-consistent demonstration set, not a customer's factors; a production
pack would pin a published factor set (e.g. national grid factors) with its
own vintage the same way.
"""
import json
import sys

FACTOR_SET = "demo-factors-v1"

# (category, unit) -> tCO2e per unit, for every category EXCEPT scope1_mobile
# (gallons) — mobile fuel is not one flat number, see MOBILE_FUEL_FACTORS and
# _mobile_fuel_factor below for why.
FACTORS = {
    ("scope1_stationary", "therms"): 0.00531,
    ("scope1_fugitive", "pounds"): 0.629,  # refrigerant R-448A — GWP vintage in FACTOR_SET_META
    ("scope2_electricity", "kWh"): 0.000385,
}

# scope1_mobile (gallons): diesel and gasoline emit at materially different
# rates per gallon — keying the whole category on the diesel number alone
# over-counted a gasoline record by about 16%. Picked per-record by
# _mobile_fuel_factor, from the record's own `activity`/`notes` text; diesel
# is the default when neither fuel word appears (the fleet this sample data
# describes is diesel-heavy, so a silent miss undercounts less often than it
# over-counts) — see `factor_basis` on each output row for whether a record
# was matched or defaulted.
MOBILE_FUEL_FACTORS = {
    "diesel": 0.01021,
    "gasoline": 0.00878,
}
DEFAULT_MOBILE_FUEL = "diesel"

# Provenance for every factor above: which EPA table it came from, and, for
# the one entry whose number depends on an external standard's own vintage,
# which version of that standard. Not read by main() below — this is the
# record of where FACTORS / MOBILE_FUEL_FACTORS came from, for whoever has to
# justify a number in this demonstration pack later.
FACTOR_SET_META = {
    "factor_set": FACTOR_SET,
    "scope1_stationary.therms": {
        "tco2e_per_unit": FACTORS[("scope1_stationary", "therms")],
        "source": "EPA GHG Emission Factors Hub, Stationary Combustion — natural gas, therms basis",
    },
    "scope1_mobile.gallons.diesel": {
        "tco2e_per_unit": MOBILE_FUEL_FACTORS["diesel"],
        "source": "EPA GHG Emission Factors Hub, Mobile Combustion — diesel, gallons basis",
    },
    "scope1_mobile.gallons.gasoline": {
        "tco2e_per_unit": MOBILE_FUEL_FACTORS["gasoline"],
        "source": "EPA GHG Emission Factors Hub, Mobile Combustion — motor gasoline, gallons basis",
    },
    "scope1_fugitive.pounds": {
        "tco2e_per_unit": FACTORS[("scope1_fugitive", "pounds")],
        "source": "EPA GHG Emission Factors Hub, refrigerant GWP tables — R-448A",
        # 0.629 = GWP 1387 / 2204.6 lb per tonne. 1387 is R-448A's 100-year
        # GWP under the IPCC Fifth Assessment Report (AR5) — the vintage
        # EPA's own published factor hub currently follows; AR4 and AR6 give
        # different figures for the same refrigerant, so the assessment
        # report matters as much as the number.
        "gwp": 1387,
        "gwp_assessment_report": "IPCC AR5 (100-year GWP)",
        "refrigerant": "R-448A",
    },
    "scope2_electricity.kWh": {
        "tco2e_per_unit": FACTORS[("scope2_electricity", "kWh")],
        "source": "EPA GHG Emission Factors Hub, national average grid — eGRID-derived",
    },
}


def _mobile_fuel_factor(record: dict) -> tuple[str, float, str]:
    """(fuel, factor, factor_basis) for one scope1_mobile record.

    Matched from `activity`/`notes` text, case-insensitively: "gasoline" or
    "petrol" -> gasoline; "diesel" -> diesel. A record naming both is treated
    as diesel (the more common mobile fuel in this sample fleet, and this
    pack's own factor before per-record fuel matching existed) rather than
    guessing further. Neither word present -> diesel by default, with
    `factor_basis` marked "diesel (default)" so an assumed figure reads
    differently from one the record's own text actually named.
    """
    text = f"{record.get('activity', '')} {record.get('notes', '')}".lower()
    if "gasoline" in text or "petrol" in text:
        fuel, basis = "gasoline", "gasoline"
    elif "diesel" in text:
        fuel, basis = "diesel", "diesel"
    else:
        fuel = DEFAULT_MOBILE_FUEL
        basis = f"{DEFAULT_MOBILE_FUEL} (default)"
    return fuel, MOBILE_FUEL_FACTORS[fuel], basis


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
        fuel = None
        factor_basis = None
        if category == "scope1_mobile" and unit == "gallons":
            fuel, factor, factor_basis = _mobile_fuel_factor(r)
        else:
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
                "fuel": fuel,
                "factor_basis": factor_basis,
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
