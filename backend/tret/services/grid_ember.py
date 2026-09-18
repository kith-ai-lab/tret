"""Pinned Ember yearly electricity intensity data; offline import only."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

SOURCE_URL = "https://files.ember-energy.org/public-downloads/yearly_full_release_long_format.csv"
SOURCE_SHA256 = "259e1095ee8ffeaf0aff37ad557916ae1823a2da13312da50ba4cec6b4574c3b"
METHODOLOGY_URL = "https://files.ember-energy.org/public-downloads/ember_electricity_data_methodology.pdf"
DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "grid_ember_2025.json"
YEAR = 2025

# CC BY 4.0 requires the creator be identified (license §3(a)(1)); these three
# travel with the bundled asset so a consumer can satisfy that on its own,
# the same way `grid_zones.py` carries `attribution`/`license_url` for its
# ODbL table.
CREATOR = "Ember"
ATTRIBUTION = (
    "Ember (2026). Yearly Electricity Data. Licensed under CC BY 4.0. "
    "https://ember-energy.org/data/yearly-electricity-data/"
)
LICENSE_URL = "https://creativecommons.org/licenses/by/4.0/"


class EmberDataError(ValueError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def import_ember_csv(path: Path, *, verify_hash: bool = True) -> dict:
    if verify_hash and _sha256(path) != SOURCE_SHA256:
        raise EmberDataError(f"{path}: source SHA256 does not match pinned release")
    countries: dict[str, dict] = {}
    world = None
    with path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        required = {"Area", "ISO 3 code", "Year", "Area type", "Category", "Subcategory", "Variable", "Unit", "Value"}
        missing = required - set(reader.fieldnames or ())
        if missing:
            raise EmberDataError(f"{path}: missing columns {sorted(missing)}")
        for number, row in enumerate(reader, 2):
            if not (
                row["Year"] == str(YEAR)
                and row["Category"] == "Power sector emissions"
                and row["Subcategory"] == "CO2 intensity"
                and row["Variable"] == "CO2 intensity"
            ):
                continue
            if row["Unit"] != "gCO2/kWh":
                raise EmberDataError(f"{path}: row {number} has unexpected unit {row['Unit']!r}")
            try:
                value = Decimal(row["Value"])
            except InvalidOperation as exc:
                raise EmberDataError(f"{path}: row {number} has invalid value") from exc
            if not value.is_finite() or value <= 0:
                raise EmberDataError(f"{path}: row {number} intensity must be positive and finite")
            record = {"name": row["Area"], "g_per_kwh": float(value), "year": YEAR}
            if row["Area"] == "World":
                world = record
            elif row["Area type"] == "Country or economy":
                iso3 = row["ISO 3 code"].strip().upper()
                if len(iso3) != 3 or not iso3.isalpha():
                    raise EmberDataError(f"{path}: row {number} has invalid ISO3 {iso3!r}")
                countries[iso3] = record
    if world is None or Decimal(str(world["g_per_kwh"])) != Decimal("458.49"):
        raise EmberDataError(f"{path}: pinned World 2025 value 458.49 was not found")
    return {
        "schema_version": 1,
        "dataset": "Ember Yearly Electricity Data 2026 release, 2025 observations",
        "source_url": SOURCE_URL,
        "source_sha256": SOURCE_SHA256,
        "methodology_url": METHODOLOGY_URL,
        "license": "CC BY 4.0",
        "license_url": LICENSE_URL,
        "attribution": ATTRIBUTION,
        "creator": CREATOR,
        "year": YEAR,
        "unit_source": "gCO2/kWh",
        "gas_coverage": "co2e",
        "gwp_horizon_years": 100,
        "gwp_assessment_basis": "unknown",
        "factor_boundary": "lifecycle_electricity_generation",
        "electricity_mix_basis": "production",
        "includes_td_losses": None,
        "world": world,
        "countries": dict(sorted(countries.items())),
    }


def load_ember_data(path: Path | None = None) -> dict:
    with (path or DATA_PATH).open(encoding="utf-8") as source:
        return json.load(source)


def ember_attribution() -> tuple[str, str, str]:
    """The `(creator, attribution, license_url)` triple CC BY 4.0 requires be
    carried alongside a use of the bundled Ember asset — the same three
    fields the committed `grid_ember_2025.json` stores at its top level, so a
    caller can attach them to a factor record without re-deriving them."""
    return CREATOR, ATTRIBUTION, LICENSE_URL


def entry_for_region(region: str, data: dict | None = None) -> tuple[str, dict] | None:
    """Resolve only the explicit ``country-ISO3`` namespace; infer nothing."""
    normalized = (region or "").strip().lower()
    if not normalized.startswith("country-"):
        return None
    iso3 = normalized.removeprefix("country-").upper()
    record = (data or load_ember_data()).get("countries", {}).get(iso3)
    if record is None:
        return None
    return iso3, {
        "g_per_kwh": record["g_per_kwh"],
        "basis": "location_based",
        "label": f"Ember {record['name']} {record['year']} production mix",
        "url": SOURCE_URL,
        "as_of": f"{record['year']}-12-31",
        "factor_boundary": "lifecycle_electricity_generation",
        "gas_coverage": "co2e",
        "gwp_horizon_years": 100,
        "gwp_assessment_basis": "unknown",
        "includes_td_losses": None,
        "electricity_mix_basis": "production",
        "dataset_version": "ember-yearly-2026-release",
        "observation_year": record["year"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tret.services.grid_ember")
    parser.add_argument("source", type=Path)
    parser.add_argument("--out", type=Path, default=DATA_PATH)
    args = parser.parse_args(argv)
    document = import_ember_csv(args.source)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {len(document['countries'])} countries plus World to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
