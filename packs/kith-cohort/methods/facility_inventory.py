"""Facility inventory (Scope 1 + Scope 2 location-based) over the Northwood
carbon template, per facility and per portfolio company.

tret method contract: JSON {"params", "inputs"} on stdin -> {"rows": [...]} on
stdout. Pure function, stdlib only.

NO emission factor is written in this file. Every factor comes from the
`factors` input: a dataset in tret's normalised factor format (columns
category, fuel_or_subregion, unit, co2e_kg_per_unit, gwp_basis, source, edition,
table, retrieved_on, url — see docs/learning-edition.md). A non-empty activity
cell with no matching factor is reported as a `skipped` row with the reason; it
contributes nothing to the totals and is never estimated.

Inputs
  activity  one row per facility (columns of the Northwood template)
  factors   the factor dataset (default name epa_ghg_factors_2025)

Params (all optional)
  subregion_map  {facility_id: eGRID subregion code}. Where a facility has no
                 entry, or the subregion has no factor row, the STATE rate
                 (fuel_or_subregion "state:<ST>") is used and the row's
                 electricity_factor_basis says so.
  reporting_year, portco_slug, facility_id   exact-match filters
"""
import json
import sys

# column -> (scope, factor category, factor fuel/subregion key, factor unit)
ACTIVITY_FIELDS = [
    ("natural_gas_therms", "scope1_stationary", "stationary_combustion", "natural_gas", "therm"),
    ("diesel_gallons", "scope1_stationary", "stationary_combustion", "diesel", "gallon"),
    ("gasoline_gallons", "scope1_stationary", "stationary_combustion", "gasoline", "gallon"),
    ("propane_gallons", "scope1_stationary", "stationary_combustion", "propane", "gallon"),
    ("fleet_diesel_gallons", "scope1_mobile", "mobile_combustion", "diesel", "gallon"),
    ("fleet_gasoline_gallons", "scope1_mobile", "mobile_combustion", "gasoline", "gallon"),
    ("refrigerant_r22_lbs", "scope1_refrigerant", "refrigerant", "R-22", "lb"),
    ("refrigerant_r410a_lbs", "scope1_refrigerant", "refrigerant", "R-410A", "lb"),
    ("electricity_kwh", "scope2", "electricity", None, "kwh"),
]
UNIT_ALIASES = {
    "therm": "therm", "therms": "therm",
    "gallon": "gallon", "gallons": "gallon", "gal": "gallon",
    "lb": "lb", "lbs": "lb", "pound": "lb", "pounds": "lb",
    "kwh": "kwh", "mwh": "mwh",
}
ROUND = 4


def norm(value):
    return "".join(ch for ch in str(value or "").lower() if ch not in " -_")


def unit_norm(value):
    return UNIT_ALIASES.get(str(value or "").strip().lower(), str(value or "").strip().lower())


def number(text):
    """(value, error) for an activity cell; blank is (None, None)."""
    text = str(text if text is not None else "").strip().replace(",", "")
    if text == "":
        return None, None
    try:
        value = float(text)
    except ValueError:
        return None, "not a number: %r" % text
    if value != value or value in (float("inf"), float("-inf")) or value < 0:
        return None, "must be a finite, non-negative number: %r" % text
    return value, None


def build_index(factor_rows):
    """(category, fuel_or_subregion, unit) -> factor row. Conflicting duplicates
    are remembered as None so the lookup can report ambiguity instead of picking."""
    index = {}
    for row in factor_rows:
        key = (norm(row.get("category")), norm(row.get("fuel_or_subregion")), unit_norm(row.get("unit")))
        if key in index:
            old = index[key]
            if old is None or old.get("co2e_kg_per_unit") != row.get("co2e_kg_per_unit"):
                index[key] = None
            continue
        index[key] = row
    return index


def lookup(index, category, fuel, unit):
    """(row, reason): the factor row for this key, or why there is none."""
    key = (norm(category), norm(fuel), unit)
    if key not in index:
        return None, "no factor for %s / %s per %s in the factor dataset" % (category, fuel, unit)
    row = index[key]
    if row is None:
        return None, "the factor dataset has conflicting rows for %s / %s per %s" % (category, fuel, unit)
    try:
        float(row.get("co2e_kg_per_unit"))
    except (TypeError, ValueError):
        return None, "the factor row for %s / %s has no co2e_kg_per_unit" % (category, fuel)
    return row, None


def describe(row):
    return ("%s %s %s" % (row.get("source", ""), row.get("edition", ""), row.get("table", ""))).strip()


def electricity_factor(index, record, subregion_map, qty_unit_pref=("kwh", "mwh")):
    """(row, unit, basis, reason) for a facility's grid factor."""
    facility_id = str(record.get("facility_id", ""))
    state = str(record.get("state", "")).strip().upper()
    subregion = (subregion_map or {}).get(facility_id)
    tried = []
    for fuel, label in ((subregion, "eGRID subregion %s" % subregion), ("state:" + state if state else None, "state rate %s" % state)):
        if not fuel:
            continue
        for unit in qty_unit_pref:
            row, _reason = lookup(index, "electricity", fuel, unit)
            if row is not None:
                basis = label
                if label.startswith("state") and subregion:
                    basis += " (subregion %s has no factor in the dataset)" % subregion
                elif label.startswith("state"):
                    basis += " (no subregion mapped for this facility)"
                return row, unit, basis, None
        tried.append(fuel)
    return None, None, None, "no electricity factor for %s in the factor dataset" % (" or ".join(tried) or "this facility (no state or subregion)")


def main():
    payload = json.load(sys.stdin)
    params = payload.get("params") or {}
    inputs = payload.get("inputs") or {}
    activity = inputs.get("activity") or []
    factors = inputs.get("factors") or []
    if not factors:
        sys.stderr.write("the factor dataset is empty: load it first (docs/learning-edition.md, Loading Ben's EPA spreadsheets)\n")
        sys.exit(2)
    index = build_index(factors)
    subregion_map = params.get("subregion_map") or {}

    for key in ("reporting_year", "portco_slug", "facility_id"):
        if params.get(key) not in (None, ""):
            activity = [r for r in activity if str(r.get(key, "")) == str(params[key])]

    facility_rows, skipped_rows = [], []
    seen = {}
    portcos = {}
    for rec in activity:
        fid = str(rec.get("facility_id", ""))
        year = str(rec.get("reporting_year", ""))
        seen[(fid, year)] = seen.get((fid, year), 0) + 1
        totals = {"scope1_stationary": 0.0, "scope1_mobile": 0.0, "scope1_refrigerant": 0.0, "scope2": 0.0}
        sources, skipped = [], 0
        elec_basis = ""
        for column, bucket, category, fuel, unit in ACTIVITY_FIELDS:
            qty, err = number(rec.get(column))
            if err is None and qty is None:
                continue  # empty cell = no activity
            reason = err
            factor, qty_unit = None, unit
            if reason is None and qty == 0:
                continue
            if reason is None and category == "electricity":
                factor, qty_unit, basis, reason = electricity_factor(index, rec, subregion_map)
                if reason is None:
                    elec_basis = basis
                    if qty_unit == "mwh":
                        qty = qty / 1000.0
            elif reason is None:
                factor, reason = lookup(index, category, fuel, unit)
            if reason is not None:
                skipped += 1
                skipped_rows.append({
                    "row_type": "skipped", "portco_slug": rec.get("portco_slug", ""), "facility_id": fid,
                    "reporting_year": year, "field": column, "activity": rec.get(column, ""), "reason": reason,
                })
                continue
            kg = qty * float(factor["co2e_kg_per_unit"])
            totals[bucket] += kg / 1000.0
            if describe(factor) not in sources:
                sources.append(describe(factor))
        scope1 = totals["scope1_stationary"] + totals["scope1_mobile"] + totals["scope1_refrigerant"]
        row = {
            "row_type": "facility",
            "portco_slug": rec.get("portco_slug", ""), "portco_name": rec.get("portco_name", ""),
            "facility_id": fid, "facility_type": rec.get("facility_type", ""),
            "city": rec.get("city", ""), "state": rec.get("state", ""), "reporting_year": year,
            "scope1_stationary_tco2e": round(totals["scope1_stationary"], ROUND),
            "scope1_mobile_tco2e": round(totals["scope1_mobile"], ROUND),
            "scope1_refrigerant_tco2e": round(totals["scope1_refrigerant"], ROUND),
            "scope1_tco2e": round(scope1, ROUND),
            "scope2_location_tco2e": round(totals["scope2"], ROUND),
            "total_tco2e": round(scope1 + totals["scope2"], ROUND),
            "electricity_factor_basis": elec_basis,
            "factor_sources": "; ".join(sources),
            "fields_skipped": skipped,
            "data_source": rec.get("data_source", ""),
        }
        facility_rows.append(row)
        key = (str(rec.get("portco_slug", "")), year)
        agg = portcos.setdefault(key, {
            "row_type": "portco", "portco_slug": key[0], "portco_name": rec.get("portco_name", ""),
            "reporting_year": year, "facilities": 0, "facilities_incomplete": 0,
            "scope1_tco2e": 0.0, "scope2_location_tco2e": 0.0, "total_tco2e": 0.0,
        })
        agg["facilities"] += 1
        agg["facilities_incomplete"] += 1 if skipped else 0
        agg["scope1_tco2e"] += scope1
        agg["scope2_location_tco2e"] += totals["scope2"]
        agg["total_tco2e"] += scope1 + totals["scope2"]

    duplicates = [
        {"row_type": "skipped", "portco_slug": "", "facility_id": fid, "reporting_year": year,
         "field": "(row)", "activity": "", "reason": "%d rows share this facility_id and reporting_year; all are counted" % n}
        for (fid, year), n in sorted(seen.items()) if n > 1
    ]
    portco_rows = []
    for key in sorted(portcos):
        agg = portcos[key]
        for k in ("scope1_tco2e", "scope2_location_tco2e", "total_tco2e"):
            agg[k] = round(agg[k], ROUND)
        portco_rows.append(agg)
    portfolio = {
        "row_type": "portfolio", "portco_slug": "ALL", "portco_name": "All portfolio companies",
        "reporting_year": str(params.get("reporting_year") or "all"),
        "facilities": sum(p["facilities"] for p in portco_rows),
        "facilities_incomplete": sum(p["facilities_incomplete"] for p in portco_rows),
        "scope1_tco2e": round(sum(p["scope1_tco2e"] for p in portco_rows), ROUND),
        "scope2_location_tco2e": round(sum(p["scope2_location_tco2e"] for p in portco_rows), ROUND),
        "total_tco2e": round(sum(p["total_tco2e"] for p in portco_rows), ROUND),
    }
    json.dump({"rows": [portfolio, *portco_rows, *facility_rows, *skipped_rows, *duplicates]}, sys.stdout)


if __name__ == "__main__":
    main()
