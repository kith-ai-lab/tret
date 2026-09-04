"""Bundled Electricity Maps zone table, cloud-region aliases, and the CSV
importer: pure, offline, no network. Mirrors test_grid_regions.py's style."""
from __future__ import annotations

import json

import pytest

from tret.services.grid_regions import _REGION_RE
from tret.services.grid_zones import (
    ZONE_ID_RE,
    ZONE_TABLE_PATH,
    REGION_ALIASES,
    ZoneTableError,
    build_zone_table,
    grid_entry_for_region,
    load_zone_table,
    main,
    write_zone_table,
    zone_for_region,
)

CSV_HEADER = (
    "Datetime (UTC),Country,Zone Name,Zone Id,"
    "Carbon Intensity gCO₂eq/kWh (direct),"
    "Carbon Intensity gCO₂eq/kWh (Life cycle),"
    "Carbon-Free Energy Percentage (CFE%),Renewable Energy Percentage (RE%),"
    "Data Source,Data Estimated,Data Estimation Method"
)


def _row(
    dt: str,
    country: str,
    zone_name: str,
    zone_id: str,
    direct: str,
    lifecycle: str,
    cfe: str,
    re_pct: str,
    estimated: str = "false",
) -> str:
    return (
        f"{dt},{country},{zone_name},{zone_id},{direct},{lifecycle},"
        f"{cfe},{re_pct},Some Source,{estimated},"
    )


def _de_fr_csv() -> str:
    lines = [
        CSV_HEADER,
        _row("2024-01-01 00:00:00", "DE", "Germany", "DE", "400.123", "410.456", "38.2", "20.1"),
        _row(
            "2025-01-01 00:00:00",
            "DE",
            "Germany",
            "DE",
            "350.789",
            "360.049",
            "41.34",
            "22.55",
            "true",
        ),
        _row("2024-01-01 00:00:00", "FR", "France", "FR", "50.1", "60.2", "90.5", "12.3"),
    ]
    return "\n".join(lines) + "\n"


# ── REGION_ALIASES shape ────────────────────────────────────────────────────
def test_region_aliases_keys_are_valid_region_tokens():
    for key in REGION_ALIASES:
        assert _REGION_RE.match(key), f"bad alias key: {key!r}"


def test_region_aliases_values_are_valid_zone_ids():
    for key, value in REGION_ALIASES.items():
        assert ZONE_ID_RE.match(value), f"bad zone id for {key!r}: {value!r}"


def test_region_aliases_nonempty():
    assert len(REGION_ALIASES) > 50


# ── zone_for_region ──────────────────────────────────────────────────────────
def test_zone_for_region_exact_zone_id():
    assert zone_for_region("DE") == "DE"


def test_zone_for_region_zone_id_case_insensitive():
    assert zone_for_region("de") == "DE"
    assert zone_for_region("De") == "DE"


def test_zone_for_region_alias():
    assert zone_for_region("canadacentral") == "CA-ON"


def test_zone_for_region_alias_uppercase_input():
    assert zone_for_region("CANADACENTRAL") == "CA-ON"


def test_zone_for_region_alias_beats_the_zone_id_shape():
    # "eu-central-1" uppercases to "EU-CENTRAL-1", which fits ZONE_ID_RE's
    # permissive shape even though it is not an Electricity Maps zone. The
    # curated alias wins, with or without a table to check against.
    assert zone_for_region("eu-central-1") == "DE"
    assert zone_for_region("EU-CENTRAL-1") == "DE"


def test_zone_for_region_unaliased_zone_shape_without_table_passes_through():
    # No alias, valid shape, no table to say otherwise: returned as a zone id
    # for a caller that only wants the key. With a table it must exist.
    assert zone_for_region("xx-nowhere-9") == "XX-NOWHERE-9"


def test_zone_for_region_unknown():
    assert zone_for_region("not-a-region") is None


def test_zone_for_region_whitespace():
    assert zone_for_region("  canadacentral  ") == "CA-ON"


def test_zone_for_region_empty():
    assert zone_for_region("") is None


def test_zone_for_region_direct_zone_id_missing_from_table_falls_through(tmp_path):
    doc = build_zone_table([])
    path = tmp_path / "zones.json"
    write_zone_table(doc, path)
    table = load_zone_table(path)
    # "de" formatted as a zone id, but the table has no DE entry
    assert zone_for_region("de", table) is None


# ── grid_entry_for_region ────────────────────────────────────────────────────
@pytest.fixture
def de_table(tmp_path):
    csv_path = tmp_path / "de_fr.csv"
    csv_path.write_text(_de_fr_csv(), encoding="utf-8")
    doc = build_zone_table([csv_path])
    table_path = tmp_path / "grid_zones.json"
    write_zone_table(doc, table_path)
    return load_zone_table(table_path)


def test_grid_entry_for_region_key_set(de_table):
    resolved = grid_entry_for_region("eu-central-1", de_table)
    assert resolved is not None
    zone_id, entry = resolved
    assert zone_id == "DE"
    assert set(entry.keys()) == {"g_per_kwh", "basis", "label", "url", "as_of"}


def test_grid_entry_for_region_values(de_table):
    zone_id, entry = grid_entry_for_region("eu-central-1", de_table)
    assert entry["basis"] == "location_based"
    assert entry["g_per_kwh"] == pytest.approx(360.0)
    assert entry["as_of"] == "2025-12-31"
    assert len(entry["label"]) <= 80
    assert "DE" in entry["label"]
    assert entry["url"] == "https://app.electricitymaps.com/zone/DE/all/yearly"


def test_grid_entry_for_region_unknown_region(de_table):
    assert grid_entry_for_region("nowhere", de_table) is None


def test_grid_entry_for_region_zone_missing_from_table(de_table):
    # AU-NSW is a valid zone id, resolvable via alias ap-southeast-2, but
    # this synthetic table only has DE and FR.
    assert grid_entry_for_region("ap-southeast-2", de_table) is None


def test_grid_entry_for_region_never_raises_on_garbage(de_table):
    assert grid_entry_for_region("!!not valid!!", de_table) is None


def test_grid_entry_for_region_estimated_surfaces_in_label(de_table):
    # de_table's DE 2025 row was built with estimated="true" (see _de_fr_csv).
    _zone_id, entry = grid_entry_for_region("eu-central-1", de_table)
    assert entry["label"].endswith(" (estimated)")
    assert len(entry["label"]) <= 80


def test_grid_entry_for_region_not_estimated_omits_label_suffix(de_table):
    # FR's only row in _de_fr_csv leaves estimated at its "false" default.
    _zone_id, entry = grid_entry_for_region("eu-west-3", de_table)
    assert "estimated" not in entry["label"]


# ── bundled table ─────────────────────────────────────────────────────────
def test_bundled_table_loads():
    table = load_zone_table()
    assert table.zones == {}


def test_bundled_table_carries_odbl_attribution():
    table = load_zone_table()
    assert "Open Database License" in table.attribution or "ODbL" in table.attribution
    assert "electricitymaps.com" in table.attribution


def test_bundled_json_on_disk_has_license_fields():
    doc = json.loads(ZONE_TABLE_PATH.read_text(encoding="utf-8"))
    assert doc["license"] == "ODbL-1.0"
    assert doc["license_url"] == "https://opendatacommons.org/licenses/odbl/1-0/"
    assert "electricitymaps.com" in doc["attribution"]


def test_bundled_table_alias_targets_present_when_nonempty():
    table = load_zone_table()
    if not table.zones:
        pytest.skip("bundled zone table has no zones yet")
    for zone_id in REGION_ALIASES.values():
        assert table.get(zone_id) is not None, f"alias target missing from bundled table: {zone_id}"


# ── importer ──────────────────────────────────────────────────────────────
def test_build_zone_table_latest_year_wins(tmp_path):
    csv_path = tmp_path / "de_fr.csv"
    csv_path.write_text(_de_fr_csv(), encoding="utf-8")
    doc = build_zone_table([csv_path])
    assert doc["zones"]["DE"]["year"] == 2025
    assert doc["zones"]["DE"]["g_per_kwh"] == 360.0
    assert doc["zones"]["DE"]["direct_g_per_kwh"] == 350.8
    assert doc["zones"]["DE"]["estimated"] is True
    assert doc["zones"]["FR"]["year"] == 2024


def test_build_zone_table_rounding(tmp_path):
    csv_path = tmp_path / "de_fr.csv"
    csv_path.write_text(_de_fr_csv(), encoding="utf-8")
    doc = build_zone_table([csv_path])
    assert doc["zones"]["DE"]["cfe_pct"] == 41.3
    assert doc["zones"]["DE"]["re_pct"] == 22.6


def test_build_zone_table_alternate_header_lca(tmp_path):
    header = CSV_HEADER.replace("(Life cycle)", "(LCA)")
    body = "\n".join(
        [
            header,
            _row("2025-01-01 00:00:00", "DE", "Germany", "DE", "350.0", "360.0", "41.3", "22.5"),
        ]
    ) + "\n"
    csv_path = tmp_path / "de.csv"
    csv_path.write_text(body, encoding="utf-8")
    doc = build_zone_table([csv_path])
    assert doc["zones"]["DE"]["g_per_kwh"] == 360.0


def test_build_zone_table_missing_lifecycle_column_raises(tmp_path):
    header = "Datetime (UTC),Zone Name,Zone Id,Carbon Intensity gCO₂eq/kWh (direct)"
    body = header + "\n2025-01-01 00:00:00,Germany,DE,350.0\n"
    csv_path = tmp_path / "bad.csv"
    csv_path.write_text(body, encoding="utf-8")
    with pytest.raises(ZoneTableError) as exc_info:
        build_zone_table([csv_path])
    assert "bad.csv" in str(exc_info.value)


def test_build_zone_table_non_numeric_value_raises(tmp_path):
    body = "\n".join(
        [
            CSV_HEADER,
            _row("2025-01-01 00:00:00", "DE", "Germany", "DE", "n/a", "not-a-number", "41.3", "22.5"),
        ]
    ) + "\n"
    csv_path = tmp_path / "bad.csv"
    csv_path.write_text(body, encoding="utf-8")
    with pytest.raises(ZoneTableError) as exc_info:
        build_zone_table([csv_path])
    message = str(exc_info.value)
    assert "bad.csv" in message
    assert "row 2" in message


def test_build_zone_table_utf8_sig(tmp_path):
    csv_path = tmp_path / "bom.csv"
    csv_path.write_bytes(b"\xef\xbb\xbf" + _de_fr_csv().encode("utf-8"))
    doc = build_zone_table([csv_path])
    assert "DE" in doc["zones"]


def test_build_zone_table_hourly_shaped_row_rejected(tmp_path):
    body = "\n".join(
        [
            CSV_HEADER,
            _row("2025-01-01 05:00:00", "DE", "Germany", "DE", "350.0", "360.0", "41.3", "22.5"),
        ]
    ) + "\n"
    csv_path = tmp_path / "hourly.csv"
    csv_path.write_text(body, encoding="utf-8")
    with pytest.raises(ZoneTableError) as exc_info:
        build_zone_table([csv_path])
    message = str(exc_info.value)
    assert "hourly.csv" in message
    assert "row 2" in message


def test_build_zone_table_monthly_shaped_row_rejected(tmp_path):
    body = "\n".join(
        [
            CSV_HEADER,
            _row("2025-02-01", "DE", "Germany", "DE", "350.0", "360.0", "41.3", "22.5"),
        ]
    ) + "\n"
    csv_path = tmp_path / "monthly.csv"
    csv_path.write_text(body, encoding="utf-8")
    with pytest.raises(ZoneTableError) as exc_info:
        build_zone_table([csv_path])
    message = str(exc_info.value)
    assert "monthly.csv" in message
    assert "row 2" in message


def test_build_zone_table_accepts_lenient_yearly_datetime_shapes(tmp_path):
    for dt in ("2025-01-01", "2025-01-01 00:00:00", "2025-01-01T00:00:00Z", "2025-01-01T00:00:00+00:00"):
        body = "\n".join(
            [CSV_HEADER, _row(dt, "DE", "Germany", "DE", "350.0", "360.0", "41.3", "22.5")]
        ) + "\n"
        csv_path = tmp_path / f"de-{dt.replace(':', '_')}.csv"
        csv_path.write_text(body, encoding="utf-8")
        doc = build_zone_table([csv_path])
        assert doc["zones"]["DE"]["year"] == 2025


def test_build_zone_table_duplicate_zone_year_across_files_rejected(tmp_path):
    body = "\n".join(
        [CSV_HEADER, _row("2025-01-01 00:00:00", "DE", "Germany", "DE", "350.0", "360.0", "41.3", "22.5")]
    ) + "\n"
    csv_path_1 = tmp_path / "de_a.csv"
    csv_path_2 = tmp_path / "de_b.csv"
    csv_path_1.write_text(body, encoding="utf-8")
    csv_path_2.write_text(body, encoding="utf-8")
    with pytest.raises(ZoneTableError) as exc_info:
        build_zone_table([csv_path_1, csv_path_2])
    message = str(exc_info.value)
    assert "de_b.csv" in message
    assert "DE" in message
    assert "2025" in message


def test_build_zone_table_nan_lifecycle_rejected(tmp_path):
    body = "\n".join(
        [
            CSV_HEADER,
            _row("2025-01-01 00:00:00", "DE", "Germany", "DE", "350.0", "NaN", "41.3", "22.5"),
        ]
    ) + "\n"
    csv_path = tmp_path / "nan.csv"
    csv_path.write_text(body, encoding="utf-8")
    with pytest.raises(ZoneTableError) as exc_info:
        build_zone_table([csv_path])
    message = str(exc_info.value)
    assert "nan.csv" in message
    assert "row 2" in message
    assert "lifecycle carbon intensity" in message


def test_build_zone_table_infinity_lifecycle_rejected(tmp_path):
    body = "\n".join(
        [
            CSV_HEADER,
            _row("2025-01-01 00:00:00", "DE", "Germany", "DE", "350.0", "Infinity", "41.3", "22.5"),
        ]
    ) + "\n"
    csv_path = tmp_path / "inf.csv"
    csv_path.write_text(body, encoding="utf-8")
    with pytest.raises(ZoneTableError) as exc_info:
        build_zone_table([csv_path])
    message = str(exc_info.value)
    assert "inf.csv" in message
    assert "lifecycle carbon intensity" in message


def test_build_zone_table_ragged_row_rejected(tmp_path):
    # Drop the trailing three fields (Data Source, Data Estimated, Data
    # Estimation Method) so the row is narrower than the 11-column header.
    body = CSV_HEADER + "\n" + "2025-01-01 00:00:00,DE,Germany,DE,350.0,360.0,41.3,22.5\n"
    csv_path = tmp_path / "ragged.csv"
    csv_path.write_text(body, encoding="utf-8")
    with pytest.raises(ZoneTableError) as exc_info:
        build_zone_table([csv_path])
    message = str(exc_info.value)
    assert "ragged.csv" in message
    assert "row 2" in message


def test_load_zone_table_nan_g_per_kwh_rejected(tmp_path):
    doc = build_zone_table([])
    doc["zones"]["DE"] = {
        "name": "Germany",
        "year": 2025,
        "g_per_kwh": "NaN",
        "direct_g_per_kwh": None,
        "cfe_pct": None,
        "re_pct": None,
        "estimated": False,
    }
    path = tmp_path / "zones.json"
    write_zone_table(doc, path)
    with pytest.raises(ZoneTableError) as exc_info:
        load_zone_table(path)
    assert "DE" in str(exc_info.value)


def test_load_zone_table_non_numeric_string_g_per_kwh_rejected(tmp_path):
    doc = build_zone_table([])
    doc["zones"]["DE"] = {
        "name": "Germany",
        "year": 2025,
        "g_per_kwh": "not-a-number",
        "direct_g_per_kwh": None,
        "cfe_pct": None,
        "re_pct": None,
        "estimated": False,
    }
    path = tmp_path / "zones.json"
    write_zone_table(doc, path)
    with pytest.raises(ZoneTableError) as exc_info:
        load_zone_table(path)
    assert "DE" in str(exc_info.value)


def test_write_zone_table_round_trips_with_no_nan_token(tmp_path):
    csv_path = tmp_path / "de_fr.csv"
    csv_path.write_text(_de_fr_csv(), encoding="utf-8")
    doc = build_zone_table([csv_path])
    path = tmp_path / "zones.json"
    write_zone_table(doc, path)
    raw = path.read_text(encoding="utf-8")
    assert "NaN" not in raw
    assert "Infinity" not in raw
    # Round-trips through the JSON parser cleanly (a bare NaN token, which
    # write_zone_table now refuses to emit, would fail json.loads).
    assert json.loads(raw) == doc


def test_write_and_load_round_trip(tmp_path):
    csv_path = tmp_path / "de_fr.csv"
    csv_path.write_text(_de_fr_csv(), encoding="utf-8")
    doc = build_zone_table([csv_path])
    table_path = tmp_path / "grid_zones.json"
    write_zone_table(doc, table_path)
    table = load_zone_table(table_path)
    factor = table.get("DE")
    assert factor is not None
    assert factor.year == 2025
    assert float(factor.g_per_kwh) == 360.0

    zone_id, entry = grid_entry_for_region("eu-central-1", table)
    assert zone_id == "DE"
    assert entry["g_per_kwh"] == pytest.approx(360.0)


# ── CLI ───────────────────────────────────────────────────────────────────
def test_main_show_region(tmp_path, capsys):
    csv_path = tmp_path / "de_fr.csv"
    csv_path.write_text(_de_fr_csv(), encoding="utf-8")
    doc = build_zone_table([csv_path])
    table_path = tmp_path / "grid_zones.json"
    write_zone_table(doc, table_path)

    rc = main(["show", "eu-central-1", "--table", str(table_path)])
    assert rc == 0
    captured = capsys.readouterr()
    assert "DE" in captured.out
    assert "360.0" in captured.out


def test_main_show_no_args_bundled_table(capsys):
    rc = main(["show"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "0 zones" in captured.out
    assert "electricitymaps.com" in captured.out


def test_main_import_writes_file(tmp_path):
    csv_path = tmp_path / "de_fr.csv"
    csv_path.write_text(_de_fr_csv(), encoding="utf-8")
    out_path = tmp_path / "out.json"
    rc = main(["import", str(csv_path), "--out", str(out_path)])
    assert rc == 0
    assert out_path.exists()
    doc = json.loads(out_path.read_text(encoding="utf-8"))
    assert doc["zones"]["DE"]["year"] == 2025
