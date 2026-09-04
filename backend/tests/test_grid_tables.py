"""Grid intensity table parsing and lookup: pure, offline, no imports from the
rest of tret. Every rejection case must name the offending row/column so an
operator pasting their own table can find the mistake."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from tret.services.grid_tables import GridTableError, parse_grid_table

DIURNAL_CSV = "hour_utc,g_per_kwh\n" + "\n".join(
    f"{h},{100 + h}" for h in range(24)
)

SERIES_CSV = """timestamp_utc,g_per_kwh
2026-01-01T00:00:00Z,200
2026-01-01T06:00:00Z,150
2026-01-01T12:00:00+00:00,400
2026-01-01T18:00:00,300
"""


# ── both shapes parse ─────────────────────────────────────────────────────────
def test_diurnal_profile_parses():
    table = parse_grid_table(DIURNAL_CSV, label="Typical day", basis="location_based")
    assert table.kind == "diurnal"
    assert table.label == "Typical day"
    assert table.basis == "location_based"
    assert len(table.rows) == 24
    assert table.rows[0] == (0, Decimal("100"))
    assert table.rows[23] == (23, Decimal("123"))


def test_dated_series_parses():
    table = parse_grid_table(SERIES_CSV, label="Jan 1 actuals", basis="market_based")
    assert table.kind == "series"
    assert len(table.rows) == 4
    # naive timestamp (no Z/offset) is treated as UTC
    assert table.rows[3][0] == datetime(2026, 1, 1, 18, tzinfo=timezone.utc)
    assert table.rows[3][1] == Decimal("300")


# ── diurnal lookup by hour ─────────────────────────────────────────────────────
def test_diurnal_lookup_returns_the_matching_hour_regardless_of_date():
    table = parse_grid_table(DIURNAL_CSV, label="d", basis="unspecified")
    at = datetime(2026, 6, 15, 5, 30, tzinfo=timezone.utc)
    assert table.lookup(at) == Decimal("105")


def test_diurnal_lookup_converts_a_non_utc_timezone_to_utc_first():
    table = parse_grid_table(DIURNAL_CSV, label="d", basis="unspecified")
    tz = timezone(timedelta(hours=-5))
    # 19:00 -05:00 == 00:00 UTC the next day
    at = datetime(2026, 6, 15, 19, 0, tzinfo=tz)
    assert table.lookup(at) == Decimal("100")


# ── series lookup: at, between, and beyond rows ───────────────────────────────
def test_series_lookup_at_an_exact_row():
    table = parse_grid_table(SERIES_CSV, label="s", basis="market_based")
    at = datetime(2026, 1, 1, 6, 0, tzinfo=timezone.utc)
    assert table.lookup(at) == Decimal("150")


def test_series_lookup_between_rows_uses_the_latest_row_at_or_before():
    table = parse_grid_table(SERIES_CSV, label="s", basis="market_based")
    at = datetime(2026, 1, 1, 7, 30, tzinfo=timezone.utc)
    assert table.lookup(at) == Decimal("150")


def test_series_lookup_before_the_first_row_is_none():
    table = parse_grid_table(SERIES_CSV, label="s", basis="market_based")
    at = datetime(2025, 12, 31, 23, 0, tzinfo=timezone.utc)
    assert table.lookup(at) is None


def test_series_lookup_within_max_gap_of_the_last_row():
    table = parse_grid_table(SERIES_CSV, label="s", basis="market_based")
    at = datetime(2026, 1, 1, 19, 30, tzinfo=timezone.utc)  # 1.5h after 18:00 row
    assert table.lookup(at) == Decimal("300")


def test_series_lookup_beyond_max_gap_falls_back_to_none():
    table = parse_grid_table(SERIES_CSV, label="s", basis="market_based")
    at = datetime(2026, 1, 1, 21, 0, tzinfo=timezone.utc)  # 3h after 18:00 row
    assert table.lookup(at) is None


def test_series_lookup_custom_max_gap_is_honored():
    table = parse_grid_table(SERIES_CSV, label="s", basis="market_based")
    at = datetime(2026, 1, 1, 19, 30, tzinfo=timezone.utc)  # 1.5h after 18:00 row
    assert table.lookup(at, max_gap=timedelta(hours=1)) is None
    assert table.lookup(at, max_gap=timedelta(hours=2)) == Decimal("300")


# ── naive datetime rejected ────────────────────────────────────────────────────
def test_lookup_rejects_a_naive_datetime():
    table = parse_grid_table(DIURNAL_CSV, label="d", basis="unspecified")
    with pytest.raises(ValueError, match="timezone-aware"):
        table.lookup(datetime(2026, 6, 15, 5, 30))


# ── summary shape ──────────────────────────────────────────────────────────────
def test_diurnal_summary_shape():
    table = parse_grid_table(DIURNAL_CSV, label="Typical day", basis="location_based")
    summary = table.summary()
    assert summary == {
        "kind": "diurnal",
        "label": "Typical day",
        "basis": "location_based",
        "row_count": 24,
        "first_timestamp": None,
        "last_timestamp": None,
        "min_g_per_kwh": 100.0,
        "max_g_per_kwh": 123.0,
        "mean_g_per_kwh": pytest.approx(111.5),
    }


def test_series_summary_shape():
    table = parse_grid_table(SERIES_CSV, label="Jan 1 actuals", basis="market_based")
    summary = table.summary()
    assert summary["kind"] == "series"
    assert summary["row_count"] == 4
    assert summary["first_timestamp"] == "2026-01-01T00:00:00+00:00"
    assert summary["last_timestamp"] == "2026-01-01T18:00:00+00:00"
    assert summary["min_g_per_kwh"] == 150.0
    assert summary["max_g_per_kwh"] == 400.0


# ── rejection cases ────────────────────────────────────────────────────────────
def test_rejects_an_invalid_basis():
    with pytest.raises(GridTableError, match="basis"):
        parse_grid_table(DIURNAL_CSV, label="d", basis="renewable_based")


def test_rejects_empty_csv():
    with pytest.raises(GridTableError, match="empty"):
        parse_grid_table("", label="d", basis="unspecified")


def test_rejects_an_unrecognized_header():
    with pytest.raises(GridTableError, match="row 1"):
        parse_grid_table("foo,bar\n1,2\n", label="d", basis="unspecified")


def test_rejects_a_header_with_no_data_rows():
    with pytest.raises(GridTableError, match="no data rows"):
        parse_grid_table("hour_utc,g_per_kwh\n", label="d", basis="unspecified")


def test_rejects_more_than_one_leap_year_of_hours():
    # Build exactly 8785 strictly-ascending one-second-apart rows.
    lines = ["timestamp_utc,g_per_kwh"]
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i in range(8785):
        ts = base + timedelta(seconds=i)
        lines.append(f"{ts.isoformat().replace('+00:00', 'Z')},100")
    csv_text = "\n".join(lines) + "\n"
    with pytest.raises(GridTableError, match="8784"):
        parse_grid_table(csv_text, label="d", basis="unspecified")


def test_rejects_wrong_row_count_for_diurnal():
    short = "hour_utc,g_per_kwh\n" + "\n".join(f"{h},100" for h in range(23))
    with pytest.raises(GridTableError, match="24 rows"):
        parse_grid_table(short, label="d", basis="unspecified")


def test_rejects_duplicate_hours():
    csv_text = "hour_utc,g_per_kwh\n" + "\n".join(
        f"{h if h != 5 else 4},100" for h in range(24)
    )
    with pytest.raises(GridTableError, match="duplicate hour_utc"):
        parse_grid_table(csv_text, label="d", basis="unspecified")


def test_rejects_an_out_of_range_hour():
    csv_text = "hour_utc,g_per_kwh\n" + "\n".join(
        f"{(24 if h == 0 else h)},100" for h in range(24)
    )
    with pytest.raises(GridTableError, match="out of range"):
        parse_grid_table(csv_text, label="d", basis="unspecified")


def test_rejects_duplicate_timestamps():
    csv_text = (
        "timestamp_utc,g_per_kwh\n"
        "2026-01-01T00:00:00Z,100\n"
        "2026-01-01T00:00:00Z,200\n"
    )
    with pytest.raises(GridTableError, match="duplicate timestamp"):
        parse_grid_table(csv_text, label="d", basis="unspecified")


def test_rejects_non_ascending_series():
    csv_text = (
        "timestamp_utc,g_per_kwh\n"
        "2026-01-01T06:00:00Z,100\n"
        "2026-01-01T00:00:00Z,200\n"
    )
    with pytest.raises(GridTableError, match="ascending"):
        parse_grid_table(csv_text, label="d", basis="unspecified")


def test_rejects_a_missing_column():
    with pytest.raises(GridTableError, match="2 columns"):
        parse_grid_table("hour_utc,g_per_kwh\n1,2,3\n" + "\n".join(
            f"{h},100" for h in range(24) if h != 1
        ), label="d", basis="unspecified")


def test_rejects_a_non_numeric_value():
    csv_text = "hour_utc,g_per_kwh\n" + "\n".join(
        f"{h},{'oops' if h == 3 else 100}" for h in range(24)
    )
    with pytest.raises(GridTableError, match="not a valid number"):
        parse_grid_table(csv_text, label="d", basis="unspecified")


def test_rejects_a_zero_or_negative_value():
    csv_text = "hour_utc,g_per_kwh\n" + "\n".join(
        f"{h},{-5 if h == 3 else 100}" for h in range(24)
    )
    with pytest.raises(GridTableError, match="positive and finite"):
        parse_grid_table(csv_text, label="d", basis="unspecified")


def test_rejects_a_non_finite_value():
    csv_text = "hour_utc,g_per_kwh\n" + "\n".join(
        f"{h},{'NaN' if h == 3 else 100}" for h in range(24)
    )
    with pytest.raises(GridTableError, match="positive and finite"):
        parse_grid_table(csv_text, label="d", basis="unspecified")


def test_rejects_an_invalid_timestamp():
    csv_text = (
        "timestamp_utc,g_per_kwh\n"
        "not-a-timestamp,100\n"
    )
    with pytest.raises(GridTableError, match="not a valid ISO-8601 timestamp"):
        parse_grid_table(csv_text, label="d", basis="unspecified")


# ── BOM and CRLF tolerance ─────────────────────────────────────────────────────
def test_strips_a_utf8_bom():
    csv_text = "﻿" + DIURNAL_CSV
    table = parse_grid_table(csv_text, label="d", basis="unspecified")
    assert len(table.rows) == 24


def test_tolerates_crlf_line_endings():
    csv_text = DIURNAL_CSV.replace("\n", "\r\n")
    table = parse_grid_table(csv_text, label="d", basis="unspecified")
    assert len(table.rows) == 24
