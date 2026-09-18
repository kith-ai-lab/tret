"""A bundled table of published annual grid carbon intensities, keyed by
Electricity Maps zone id, plus a best-effort map from cloud provider region
names to those zone ids.

## Why this exists

`tret.services.grid_regions` lets an operator pin a provider to a region
(`grid.regions: {provider: region}`) so a `provider@region` grid entry can be
looked up ahead of the bare `provider` one. Today that pin is useless unless
the operator *also* typed a `provider@region` entry into `TRET_GRID_FACTORS`
themselves — tret ships no data connecting a region name to an actual grid
figure. This module is that data: a bundled table an operator's pinned region
can resolve against, without tret ever inferring, geolocating, or defaulting
a region on anyone's behalf (see `docs/emissions-methodology.md`, "Why this
is configuration and not geolocation" — the stance is identical here: a zone
figure is only ever *reached*, never *guessed*).

## Data source and licensing

The bundled `../data/grid_zones.json` is built from Electricity Maps' free
downloadable datasets (hourly/daily/monthly/yearly, 2021-2025, 160+ zones),
available at https://www.electricitymaps.com/data-portal (an account is
required to download; the files themselves are free). Those datasets are
published under the Open Database License (ODbL) v1.0
(https://opendatacommons.org/licenses/odbl/1-0/), which permits commercial
use with attribution and requires that a derived database — this JSON file
is one — stay under ODbL too. `grid_zones.json` carries that attribution
inline (`attribution`, `license`, `license_url`, `download_url` keys). The
ODbL-covered data file is read only through this module (Apache-2.0, like the
rest of tret), and the two licenses coexist because the license attaches to
the *data file*, not the code that reads it.

The live Electricity Maps API, and any of tret's own internal endpoints, are
NOT a source for this table and must never be fetched here or anywhere else
in this module — this table is refreshed offline by a maintainer running the
importer below against downloaded CSVs, on whatever cadence they choose, and
the result is committed like any other bundled asset. Nothing in this module
makes a network call or imports the rest of tret. `_resolve_grid`
(`tret.services.emission_factors`) consumes this data by calling
`grid_entry_for_region`, which supplies the `dataset` rung of the factor
ladder — between `env` and `global_default` (it only ever beats the shipped
global default; anything an operator actually set, including
`TRET_GRID_FACTORS` and the legacy local setting, still wins), and only for a
provider the workspace has actually pinned to a region — see
`docs/grid-zones.md`.
"""
from __future__ import annotations

import argparse
import csv
import functools
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable, Mapping

# Electricity Maps zone ids: two-letter country code, optionally followed by
# one or more `-SUBREGION` segments (`DE`, `US-MIDA-PJM`, `SE-SE3`, `IT-NO`).
ZONE_ID_RE = re.compile(r"^[A-Z]{2}(-[A-Z0-9]+)*$")

ZONE_TABLE_PATH = Path(__file__).resolve().parent.parent / "data" / "grid_zones.json"


class ZoneTableError(ValueError):
    """The bundled zone table, or a CSV being imported into it, failed
    validation. Always names the offending file/row/zone/field."""


@dataclass(frozen=True)
class ZoneFactor:
    """One zone's most recent published annual average, as bundled.

    `g_per_kwh` is the LIFECYCLE (well-to-wheel) figure — the number tret
    reports, matching the lifecycle basis its other bundled factors use.
    `direct_g_per_kwh` (combustion-only) is kept alongside for reference but
    is never what `grid_entry_for_region` hands back.
    """

    zone: str
    name: str
    year: int
    g_per_kwh: Decimal
    direct_g_per_kwh: Decimal | None
    cfe_pct: Decimal | None
    re_pct: Decimal | None
    estimated: bool


@dataclass(frozen=True)
class ZoneTable:
    """A loaded `grid_zones.json`: zone data plus the licensing metadata that
    must travel with it wherever the data is used or redistributed."""

    zones: Mapping[str, ZoneFactor]
    attribution: str
    license: str
    download_url: str
    generated_at: str | None

    def get(self, zone_id: str) -> ZoneFactor | None:
        """Case-insensitive: `"de"`, `"De"`, `"DE"` all reach the same entry."""
        return self.zones.get(zone_id.strip().upper())


def _read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _parse_zone_table(doc: dict, *, source: Path) -> ZoneTable:
    zones: dict[str, ZoneFactor] = {}
    for zone_id, entry in (doc.get("zones") or {}).items():
        if not ZONE_ID_RE.match(zone_id):
            raise ZoneTableError(f"{source}: invalid zone id {zone_id!r}")
        try:
            g_per_kwh = Decimal(str(entry["g_per_kwh"]))
        except (KeyError, InvalidOperation, TypeError) as exc:
            raise ZoneTableError(
                f"{source}: zone {zone_id!r} has an invalid g_per_kwh"
            ) from exc
        if not g_per_kwh.is_finite():
            raise ZoneTableError(
                f"{source}: zone {zone_id!r} has a non-finite g_per_kwh "
                f"{entry['g_per_kwh']!r}"
            )
        if g_per_kwh <= 0:
            raise ZoneTableError(
                f"{source}: zone {zone_id!r} g_per_kwh must be > 0, got {g_per_kwh}"
            )
        year = entry.get("year")
        if not isinstance(year, int) or isinstance(year, bool):
            raise ZoneTableError(f"{source}: zone {zone_id!r} year must be an int, got {year!r}")
        direct = entry.get("direct_g_per_kwh")
        cfe = entry.get("cfe_pct")
        re_pct = entry.get("re_pct")
        zones[zone_id] = ZoneFactor(
            zone=zone_id,
            name=str(entry.get("name") or zone_id),
            year=year,
            g_per_kwh=g_per_kwh,
            direct_g_per_kwh=Decimal(str(direct)) if direct is not None else None,
            cfe_pct=Decimal(str(cfe)) if cfe is not None else None,
            re_pct=Decimal(str(re_pct)) if re_pct is not None else None,
            estimated=bool(entry.get("estimated", False)),
        )
    return ZoneTable(
        zones=zones,
        attribution=doc.get("attribution", ""),
        license=doc.get("license", ""),
        download_url=doc.get("download_url", ""),
        generated_at=doc.get("generated_at"),
    )


@functools.lru_cache(maxsize=1)
def _load_default() -> ZoneTable:
    return _parse_zone_table(_read_json(ZONE_TABLE_PATH), source=ZONE_TABLE_PATH)


def load_zone_table(path: Path | None = None) -> ZoneTable:
    """Load and validate a zone table. `path=None` (the default) loads the
    bundled `grid_zones.json`, cached after the first call — it never
    changes at runtime, so re-parsing it on every lookup would be wasted
    work."""
    if path is None:
        return _load_default()
    return _parse_zone_table(_read_json(path), source=path)


# ── cloud provider region -> Electricity Maps zone ─────────────────────────
#
# These map each cloud provider's region *name* to the Electricity Maps zone
# whose grid physically serves that region's data centres. It is a
# maintained best-effort table, not a provider disclosure — providers do not
# publish which zone backs a region, so entries here come from public
# information about where each region's data centres are (cloud regions
# rarely move once launched). This table alone resolves nothing: a zone
# figure only ever applies once an operator has explicitly pinned the
# provider to the region (see this module's docstring, and
# `tret.services.grid_regions`).
#
# Keys are lowercase and match `grid_regions._REGION_RE`; values are
# Electricity Maps zone ids.
REGION_ALIASES: dict[str, str] = {
    # --- AWS ---
    "us-east-1": "US-MIDA-PJM",
    "us-east-2": "US-MIDA-PJM",
    "us-west-1": "US-CAL-CISO",
    "us-west-2": "US-NW-PACW",
    "ca-central-1": "CA-QC",
    "ca-west-1": "CA-AB",
    "eu-west-1": "IE",
    "eu-west-2": "GB",
    "eu-west-3": "FR",
    "eu-central-1": "DE",
    "eu-central-2": "CH",
    "eu-north-1": "SE-SE3",
    "eu-south-1": "IT-NO",
    "eu-south-2": "ES",
    "ap-northeast-1": "JP-TK",
    "ap-northeast-2": "KR",
    "ap-northeast-3": "JP-KN",
    "ap-southeast-1": "SG",
    "ap-southeast-2": "AU-NSW",
    "ap-southeast-3": "ID",
    "ap-southeast-4": "AU-VIC",
    "ap-south-1": "IN-WE",
    "ap-south-2": "IN-SO",
    "ap-east-1": "HK",
    "sa-east-1": "BR-CS",
    "me-south-1": "BH",
    "me-central-1": "AE",
    "il-central-1": "IL",
    "af-south-1": "ZA",
    # --- GCP ---
    "us-central1": "US-MIDW-MISO",
    "us-east1": "US-CAR-SC",
    "us-east4": "US-MIDA-PJM",
    "us-east5": "US-MIDA-PJM",
    "us-west1": "US-NW-PACW",
    "us-west2": "US-CAL-LDWP",
    "us-west3": "US-NW-PACE",
    "us-west4": "US-NW-NEVP",
    "us-south1": "US-TEX-ERCO",
    "northamerica-northeast1": "CA-QC",
    "northamerica-northeast2": "CA-ON",
    "europe-west1": "BE",
    "europe-west2": "GB",
    "europe-west3": "DE",
    "europe-west4": "NL",
    "europe-west6": "CH",
    "europe-west8": "IT-NO",
    "europe-west9": "FR",
    "europe-west10": "DE",
    "europe-west12": "IT-NO",
    "europe-north1": "FI",
    "europe-southwest1": "ES",
    "europe-central2": "PL",
    "asia-east1": "TW",
    "asia-east2": "HK",
    "asia-northeast1": "JP-TK",
    "asia-northeast2": "JP-KN",
    "asia-northeast3": "KR",
    "asia-south1": "IN-WE",
    "asia-south2": "IN-NO",
    "asia-southeast1": "SG",
    "asia-southeast2": "ID",
    "australia-southeast1": "AU-NSW",
    "australia-southeast2": "AU-VIC",
    "southamerica-east1": "BR-CS",
    "southamerica-west1": "CL-SEN",
    "me-west1": "IL",
    "me-central1": "QA",
    "africa-south1": "ZA",
    # --- Azure ---
    "eastus": "US-MIDA-PJM",
    "eastus2": "US-MIDA-PJM",
    "centralus": "US-MIDW-MISO",
    "northcentralus": "US-MIDA-PJM",
    "southcentralus": "US-TEX-ERCO",
    "westus": "US-CAL-CISO",
    "westus2": "US-NW-GCPD",
    "westus3": "US-SW-AZPS",
    "canadacentral": "CA-ON",
    "canadaeast": "CA-QC",
    "northeurope": "IE",
    "westeurope": "NL",
    "uksouth": "GB",
    "ukwest": "GB",
    "francecentral": "FR",
    "germanywestcentral": "DE",
    "switzerlandnorth": "CH",
    "norwayeast": "NO-NO1",
    "swedencentral": "SE-SE2",
    "polandcentral": "PL",
    "italynorth": "IT-NO",
    "spaincentral": "ES",
    "japaneast": "JP-TK",
    "japanwest": "JP-KN",
    "koreacentral": "KR",
    "eastasia": "HK",
    "southeastasia": "SG",
    "australiaeast": "AU-NSW",
    "australiasoutheast": "AU-VIC",
    "centralindia": "IN-WE",
    "southindia": "IN-SO",
    "brazilsouth": "BR-CS",
    "southafricanorth": "ZA",
    "uaenorth": "AE",
    "qatarcentral": "QA",
    "israelcentral": "IL",
}


def zone_for_region(region: str, table: ZoneTable | None = None) -> str | None:
    """The Electricity Maps zone id for a cloud region name, or `None` when
    nothing matches.

    `REGION_ALIASES` is consulted first: it is curated and unambiguous,
    whereas `ZONE_ID_RE` is a permissive *shape* that many cloud region names
    also fit (`eu-central-1` uppercases to `EU-CENTRAL-1`, which looks like a
    zone id but is not one). A region that matches no alias and is a valid
    zone id (an operator can pin straight to `"DE"` or `"US-MIDA-PJM"`) is
    returned as-is — checked against `table` for existence only when a table
    is given, so `zone_for_region("DE")` with no table still says "DE".
    """
    key = (region or "").strip().lower()
    if not key:
        return None
    alias = REGION_ALIASES.get(key)
    if alias is not None:
        return alias
    candidate = key.upper()
    if ZONE_ID_RE.match(candidate) and (table is None or table.get(candidate) is not None):
        return candidate
    return None


def grid_entry_for_region(region: str, table: ZoneTable | None = None) -> tuple[str, dict] | None:
    """`(zone_id, entry)` for a region, `entry` shaped exactly like
    `tret.services.emission_factors.GridEntry` — ready to hand to whatever
    eventually wires this into the factor ladder. `None` when the region
    resolves to no zone, or resolves to a zone the table doesn't have data
    for. Never raises for an unknown region: an operator can pin any region
    string, and a miss here is a "no data" outcome, not an error.

    `table` defaults to the bundled table (loaded once, then cached).
    """
    resolved_table = table if table is not None else load_zone_table()
    zone_id = zone_for_region(region, resolved_table)
    if zone_id is None:
        return None
    factor = resolved_table.get(zone_id)
    if factor is None:
        return None
    label = f"Electricity Maps {factor.zone} {factor.year} yearly avg, lifecycle"
    if factor.estimated:
        label += " (estimated)"
    entry = {
        "g_per_kwh": float(factor.g_per_kwh),
        "basis": "location_based",
        # Defensive, not load-bearing: the longest zone id bundled is 12
        # chars, so this label never actually reaches 80 chars and the slice
        # never truncates.
        "label": label[:80],
        "url": f"https://app.electricitymaps.com/zone/{factor.zone}/all/yearly",
        "as_of": f"{factor.year}-12-31",
        # Electricity Maps labels the exported value as lifecycle gCO2eq.
        # Its GWP horizon, assessment basis, transmission losses, and mix basis
        # are not asserted by the bundled table, so they remain unknown.
        "factor_boundary": "lifecycle_electricity_generation",
        "gas_coverage": "co2e",
        "gwp_horizon_years": None,
        "gwp_assessment_basis": "unknown",
        "includes_td_losses": None,
        "electricity_mix_basis": "unknown",
        "dataset_version": "electricity-maps-bundled-yearly",
        "observation_year": factor.year,
    }
    return factor.zone, entry


# ── importer: Electricity Maps yearly CSV downloads -> grid_zones.json ────
#
# Column headers have varied wording across Electricity Maps' releases (e.g.
# "(Life cycle)" vs "(LCA)"), so columns are matched by regex rather than by
# exact name. Files are read as utf-8-sig because the header carries `₂`
# (gCO₂eq) which some exports BOM-prefix.
_COL_DATETIME = re.compile(r"^datetime", re.IGNORECASE)
_COL_ZONE_ID = re.compile(r"^zone id$", re.IGNORECASE)
_COL_ZONE_NAME = re.compile(r"^zone name$", re.IGNORECASE)
_COL_DIRECT = re.compile(r"carbon intensity.*\(direct\)", re.IGNORECASE)
_COL_LIFECYCLE = re.compile(r"carbon intensity.*\((lca|life ?cycle)\)", re.IGNORECASE)
_COL_CFE = re.compile(r"(carbon.free|low carbon).*(%|percentage)", re.IGNORECASE)
_COL_RENEWABLE = re.compile(r"renewable.*(%|percentage)", re.IGNORECASE)
_COL_ESTIMATED = re.compile(r"^data estimated$", re.IGNORECASE)

_TRUE_STRINGS = {"true", "1", "yes", "y"}

# The importer only accepts *yearly* exports: every row's datetime must be
# the first instant of its year. Separator between date and time is lenient
# (space or "T", matching Electricity Maps' own variation across exports),
# and a trailing "Z" or "+00:00" offset is tolerated, but the calendar
# fields themselves are strict — month, day, hour and minute must all read
# as the very start of the year.
_DATETIME_RE = re.compile(
    r"^(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})"
    r"(?:[ T](?P<hour>\d{2}):(?P<minute>\d{2})(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?)?$"
)


def _match_columns(header: list[str], path: Path) -> dict[str, int | None]:
    col: dict[str, int | None] = {
        "datetime": None,
        "zone_id": None,
        "zone_name": None,
        "direct": None,
        "lifecycle": None,
        "cfe": None,
        "renewable": None,
        "estimated": None,
    }
    for idx, raw in enumerate(header):
        name = raw.strip()
        if col["datetime"] is None and _COL_DATETIME.search(name):
            col["datetime"] = idx
        elif col["zone_id"] is None and _COL_ZONE_ID.match(name):
            col["zone_id"] = idx
        elif col["zone_name"] is None and _COL_ZONE_NAME.match(name):
            col["zone_name"] = idx
        elif col["lifecycle"] is None and _COL_LIFECYCLE.search(name):
            col["lifecycle"] = idx
        elif col["direct"] is None and _COL_DIRECT.search(name):
            col["direct"] = idx
        elif col["cfe"] is None and _COL_CFE.search(name):
            col["cfe"] = idx
        elif col["renewable"] is None and _COL_RENEWABLE.search(name):
            col["renewable"] = idx
        elif col["estimated"] is None and _COL_ESTIMATED.match(name):
            col["estimated"] = idx
    if col["zone_id"] is None or col["lifecycle"] is None:
        raise ZoneTableError(
            f"{path}: no recognisable zone id / lifecycle carbon intensity column"
        )
    if col["datetime"] is None:
        raise ZoneTableError(f"{path}: no recognisable datetime column")
    return col


def _parse_year(row: list[str], col: dict[str, int | None], path: Path, row_num: int) -> int:
    """Parse the row's datetime cell and return its year — but only when
    that datetime is the first instant of the year. A yearly Electricity
    Maps export has exactly one row per zone per year, dated `YYYY-01-01`
    (optionally with a `00:00:00` time and a `Z`/`+00:00` offset); an
    hourly, daily, or monthly export has rows that are not, which is
    exactly what this rejects — averaging in one arbitrary non-yearly row
    as if it were the year's figure would be silently wrong, not
    approximately right.
    """
    raw = row[col["datetime"]].strip()
    match = _DATETIME_RE.match(raw)
    is_year_start = bool(match)
    if is_year_start:
        fields = match.groupdict()
        is_year_start = fields["month"] == "01" and fields["day"] == "01"
        if is_year_start and fields["hour"] is not None:
            is_year_start = fields["hour"] == "00" and fields["minute"] == "00"
    if not is_year_start:
        raise ZoneTableError(
            f"{path}: row {row_num} has datetime {raw!r}, which is not the first "
            "instant of a year — this file does not look like a yearly export"
        )
    return int(match.group("year"))


def _parse_decimal(raw: str, path: Path, row_num: int, field: str) -> Decimal:
    text = raw.strip()
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise ZoneTableError(f"{path}: row {row_num} has a non-numeric {field} {raw!r}") from exc
    if not value.is_finite():
        raise ZoneTableError(f"{path}: row {row_num} has a non-finite {field} {raw!r}")
    return value


def _parse_optional_decimal(
    row: list[str], idx: int | None, path: Path, row_num: int, field: str
) -> Decimal | None:
    if idx is None:
        return None
    raw = row[idx].strip()
    if not raw:
        return None
    return _parse_decimal(raw, path, row_num, field)


def _parse_bool(raw: str) -> bool:
    return raw.strip().lower() in _TRUE_STRINGS


def _ingest_csv(
    path: Path, zones: dict[str, dict], seen: dict[tuple[str, int], tuple[Path, int]]
) -> None:
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh)
        try:
            header = next(reader)
        except StopIteration as exc:
            raise ZoneTableError(f"{path}: empty file") from exc
        col = _match_columns(header, path)
        for row_num, row in enumerate(reader, start=2):
            if not row or all(not cell.strip() for cell in row):
                continue
            if len(row) < len(header):
                raise ZoneTableError(
                    f"{path}: row {row_num} has fewer cells than the header "
                    f"({len(row)} vs {len(header)})"
                )
            zone_id = row[col["zone_id"]].strip()
            if not zone_id:
                continue
            if not ZONE_ID_RE.match(zone_id):
                raise ZoneTableError(f"{path}: row {row_num} has an invalid zone id {zone_id!r}")
            year = _parse_year(row, col, path, row_num)
            key = (zone_id, year)
            duplicate_of = seen.get(key)
            if duplicate_of is not None:
                prev_path, prev_row = duplicate_of
                raise ZoneTableError(
                    f"{path}: row {row_num} duplicates zone {zone_id!r} year {year}, "
                    f"already read from {prev_path} row {prev_row}"
                )
            seen[key] = (path, row_num)
            existing = zones.get(zone_id)
            if existing is not None and existing["year"] > year:
                continue
            lifecycle = _parse_decimal(
                row[col["lifecycle"]], path, row_num, "lifecycle carbon intensity"
            )
            zone_name_idx = col["zone_name"]
            name = row[zone_name_idx].strip() if zone_name_idx is not None else ""
            direct = _parse_optional_decimal(
                row, col["direct"], path, row_num, "direct carbon intensity"
            )
            cfe = _parse_optional_decimal(row, col["cfe"], path, row_num, "CFE percentage")
            re_pct = _parse_optional_decimal(
                row, col["renewable"], path, row_num, "renewable percentage"
            )
            estimated_idx = col["estimated"]
            estimated = _parse_bool(row[estimated_idx]) if estimated_idx is not None else False
            zones[zone_id] = {
                "name": name or zone_id,
                "year": year,
                "g_per_kwh": float(round(lifecycle, 1)),
                "direct_g_per_kwh": float(round(direct, 1)) if direct is not None else None,
                "cfe_pct": float(round(cfe, 1)) if cfe is not None else None,
                "re_pct": float(round(re_pct, 1)) if re_pct is not None else None,
                "estimated": estimated,
            }


def build_zone_table(csv_paths: Iterable[Path]) -> dict:
    """Build a `grid_zones.json` document from one or more Electricity Maps
    yearly CSV downloads. Each file may hold a single zone across several
    yearly rows, or many zones; for each zone, across all files given, only
    the row with the latest year is kept. Raises `ZoneTableError` (naming
    the file, and the row where relevant) for a file missing its zone-id or
    lifecycle-intensity columns, a non-numeric or non-finite intensity
    value, a ragged row, a row whose datetime is not the first instant of a
    year (i.e. not actually a yearly export), or a second row for the same
    (zone, year) within or across the given files."""
    zones: dict[str, dict] = {}
    seen: dict[tuple[str, int], tuple[Path, int]] = {}
    for path in csv_paths:
        _ingest_csv(Path(path), zones, seen)
    return {
        "source": "Electricity Maps",
        "dataset": "yearly carbon intensity, flow-traced, per zone",
        "license": "ODbL-1.0",
        "license_url": "https://opendatacommons.org/licenses/odbl/1-0/",
        "attribution": (
            "Contains data from Electricity Maps (https://www.electricitymaps.com), "
            "licensed under the Open Database License (ODbL) v1.0."
        ),
        "download_url": "https://www.electricitymaps.com/data-portal",
        "generated_by": "python -m tret.services.grid_zones import <yearly csv files...>",
        "generated_at": datetime.now(timezone.utc).date().isoformat(),
        "zones": zones,
    }


def write_zone_table(doc: dict, path: Path) -> None:
    """Pretty-print `doc` (sorted keys, trailing newline) to `path`,
    creating parent directories as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # allow_nan=False: NaN/Infinity are rejected at parse time (_parse_decimal)
    # already, but refusing to *write* them too means a non-finite value can
    # never reach disk as an invalid bare `NaN`/`Infinity` JSON token.
    text = json.dumps(doc, indent=2, sort_keys=True, allow_nan=False)
    path.write_text(text + "\n", encoding="utf-8")


# ── CLI ──────────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tret.services.grid_zones")
    sub = parser.add_subparsers(dest="command", required=True)

    import_p = sub.add_parser(
        "import", help="Build grid_zones.json from Electricity Maps yearly CSV downloads"
    )
    import_p.add_argument("csv_files", nargs="+", type=Path)
    import_p.add_argument("--out", type=Path, default=ZONE_TABLE_PATH)

    show_p = sub.add_parser("show", help="Print zone data for one or more regions")
    show_p.add_argument("regions", nargs="*")
    show_p.add_argument(
        "--table", type=Path, default=None, help="Zone table to read (default: bundled table)"
    )

    args = parser.parse_args(argv)

    if args.command == "import":
        doc = build_zone_table(args.csv_files)
        write_zone_table(doc, args.out)
        print(f"wrote {len(doc['zones'])} zones to {args.out}")
        return 0

    table = load_zone_table(args.table)
    if not args.regions:
        print(f"{len(table.zones)} zones. {table.attribution}")
        return 0
    for region in args.regions:
        resolved = grid_entry_for_region(region, table)
        if resolved is None:
            print(f"{region}: no zone data")
            continue
        zone_id, _entry = resolved
        factor = table.get(zone_id)
        print(f"{region}: {factor.zone} {factor.year} {factor.g_per_kwh} gCO2e/kWh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
