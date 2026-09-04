"""Hourly grid carbon-intensity tables, parsed from an operator-supplied CSV.

Purpose: an operator can hand tret a table of grid carbon intensity by hour so
a run can look up the factor for its actual start time instead of always
falling back to a single annual average (`grid_co2e_g_per_kwh`,
tret/services/emissions.py). This module is pure and stand-alone — no network
access, ever, and nothing here imports the rest of tret. Nothing imports this
module yet either; wiring a parsed `GridTable` into the emissions lookup path
is later work.

Two CSV shapes are accepted, detected from the header row:

1. Diurnal profile — columns `hour_utc,g_per_kwh`, exactly 24 rows, one per
   hour 0-23. Represents a typical day with no absolute dates; every lookup
   hits (there is always an hour-of-day to match).
2. Dated series — columns `timestamp_utc,g_per_kwh`, ISO-8601 timestamps
   (a trailing `Z` or explicit `+00:00` accepted; a naive timestamp is
   treated as already being UTC), strictly ascending, at least one row.
   A lookup returns the latest row at or before the requested time, but only
   if that row is within `max_gap` of it — otherwise None, so the caller
   falls back to the annual figure rather than silently using a stale reading.

`GridTableError` (a `ValueError`) always names the offending row/column so a
pasted table's operator can find their mistake without re-deriving it.
"""
from __future__ import annotations

import bisect
import csv
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Literal

# One leap year of hours — the cap on how large a pasted table may be.
_MAX_ROWS = 8_784

_VALID_BASES = ("location_based", "market_based", "unspecified")

_DIURNAL_HEADER = ("hour_utc", "g_per_kwh")
_SERIES_HEADER = ("timestamp_utc", "g_per_kwh")


class GridTableError(ValueError):
    """A grid table CSV failed to parse or validate."""


@dataclass(frozen=True)
class GridTable:
    kind: Literal["diurnal", "series"]
    label: str
    basis: str  # location_based | market_based | unspecified
    # Normalized rows: for kind="diurnal", (hour: int, g_per_kwh: Decimal)
    # sorted 0-23; for kind="series", (timestamp: datetime, g_per_kwh: Decimal)
    # ascending, timestamps always UTC-aware.
    rows: tuple

    def lookup(
        self, at: datetime, *, max_gap: timedelta = timedelta(hours=2)
    ) -> Decimal | None:
        """The g/kWh factor for `at`, or None if the caller should fall back.

        `at` must be timezone-aware — a naive datetime is ambiguous about
        which UTC offset it means, and guessing would be a silent local-time
        bug, so it raises instead.
        """
        if at.tzinfo is None:
            raise ValueError("lookup() requires a timezone-aware datetime")
        at_utc = at.astimezone(timezone.utc)
        if self.kind == "diurnal":
            hour = at_utc.hour
            for row_hour, value in self.rows:
                if row_hour == hour:
                    return value
            return None  # unreachable in practice: parsing guarantees all 24 hours

        # `self.rows` is ascending by timestamp (enforced at parse time), so
        # the latest row at-or-before `at_utc` is a binary search rather than
        # a linear scan: `bisect_right` finds the insertion point for
        # `at_utc` among the rows' own timestamps (via `key=`), which is
        # exactly the count of rows with `ts <= at_utc` — the row just before
        # it (if any) is the candidate the old linear scan used to find by
        # walking until it saw a `ts > at_utc`.
        idx = bisect.bisect_right(self.rows, at_utc, key=lambda row: row[0])
        if idx == 0:
            return None
        ts, value = self.rows[idx - 1]
        if at_utc - ts > max_gap:
            return None
        return value

    def summary(self) -> dict:
        """JSON-serializable snapshot: shape, provenance, and value range."""
        values = [value for _, value in self.rows]
        if self.kind == "series":
            first_timestamp = self.rows[0][0].isoformat() if self.rows else None
            last_timestamp = self.rows[-1][0].isoformat() if self.rows else None
        else:
            first_timestamp = None
            last_timestamp = None
        return {
            "kind": self.kind,
            "label": self.label,
            "basis": self.basis,
            "row_count": len(self.rows),
            "first_timestamp": first_timestamp,
            "last_timestamp": last_timestamp,
            "min_g_per_kwh": float(min(values)) if values else None,
            "max_g_per_kwh": float(max(values)) if values else None,
            "mean_g_per_kwh": float(sum(values) / len(values)) if values else None,
        }


def _parse_value(raw: str, row_num: int, *, column: str = "g_per_kwh") -> Decimal:
    try:
        value = Decimal(raw.strip())
    except (InvalidOperation, ValueError):
        raise GridTableError(f"row {row_num}: {column} {raw!r} is not a valid number") from None
    if not value.is_finite() or value <= 0:
        raise GridTableError(
            f"row {row_num}: {column} must be positive and finite, got {raw.strip()!r}"
        )
    return value


def _parse_hour(raw: str, row_num: int) -> int:
    try:
        hour = int(raw.strip())
    except ValueError:
        raise GridTableError(f"row {row_num}: hour_utc {raw!r} is not an integer") from None
    if not 0 <= hour <= 23:
        raise GridTableError(f"row {row_num}: hour_utc {hour} is out of range 0-23")
    return hour


def _parse_timestamp(raw: str, row_num: int) -> datetime:
    text = raw.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise GridTableError(
            f"row {row_num}: timestamp_utc {raw!r} is not a valid ISO-8601 timestamp"
        ) from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt


def _split_rows(csv_text: str) -> list[tuple[int, list[str]]]:
    """Non-blank (row_number, cells) pairs, row_number 1-based from the raw text.

    Row numbers are assigned before blank lines are dropped, so an error
    message's row number always matches the line the operator would count in
    their own file (including any blank lines in the middle).
    """
    text = csv_text.lstrip("﻿")  # a UTF-8 BOM, if the file carried one
    text = text.replace("\r\n", "\n").replace("\r", "\n")  # tolerate CRLF
    lines = text.split("\n")
    parsed = list(csv.reader(lines))
    return [
        (i, row)
        for i, row in enumerate(parsed, start=1)
        if row and any(cell.strip() for cell in row)
    ]


def parse_grid_table(csv_text: str, *, label: str, basis: str) -> GridTable:
    if basis not in _VALID_BASES:
        raise GridTableError(f"basis must be one of {_VALID_BASES}, got {basis!r}")

    rows = _split_rows(csv_text)
    if not rows:
        raise GridTableError("csv is empty: no header row found")

    header_row_num, header = rows[0]
    header_norm = tuple(cell.strip().lower() for cell in header)
    if header_norm == _DIURNAL_HEADER:
        kind: Literal["diurnal", "series"] = "diurnal"
    elif header_norm == _SERIES_HEADER:
        kind = "series"
    else:
        raise GridTableError(
            f"row {header_row_num}: unrecognized header {header!r}; expected "
            f"'hour_utc,g_per_kwh' or 'timestamp_utc,g_per_kwh'"
        )

    data_rows = rows[1:]
    if not data_rows:
        raise GridTableError("csv has a header but no data rows")
    if len(data_rows) > _MAX_ROWS:
        raise GridTableError(
            f"csv has {len(data_rows)} data rows, over the {_MAX_ROWS}-row "
            "(one leap year of hours) limit"
        )

    if kind == "diurnal":
        return _parse_diurnal(data_rows, label=label, basis=basis)
    return _parse_series(data_rows, label=label, basis=basis)


def _parse_diurnal(
    data_rows: list[tuple[int, list[str]]], *, label: str, basis: str
) -> GridTable:
    if len(data_rows) != 24:
        raise GridTableError(
            f"diurnal profile must have exactly 24 rows (one per hour 0-23), got {len(data_rows)}"
        )
    by_hour: dict[int, Decimal] = {}
    for row_num, row in data_rows:
        if len(row) != 2:
            raise GridTableError(f"row {row_num}: expected 2 columns, got {len(row)}")
        hour_raw, value_raw = row
        hour = _parse_hour(hour_raw, row_num)
        if hour in by_hour:
            raise GridTableError(f"row {row_num}: duplicate hour_utc {hour}")
        by_hour[hour] = _parse_value(value_raw, row_num)
    missing = sorted(set(range(24)) - set(by_hour))
    if missing:
        raise GridTableError(f"diurnal profile is missing hour(s): {missing}")
    return GridTable(
        kind="diurnal", label=label, basis=basis, rows=tuple(sorted(by_hour.items()))
    )


def _parse_series(
    data_rows: list[tuple[int, list[str]]], *, label: str, basis: str
) -> GridTable:
    parsed: list[tuple[datetime, Decimal]] = []
    for row_num, row in data_rows:
        if len(row) != 2:
            raise GridTableError(f"row {row_num}: expected 2 columns, got {len(row)}")
        ts_raw, value_raw = row
        ts = _parse_timestamp(ts_raw, row_num)
        value = _parse_value(value_raw, row_num)
        if parsed:
            prev_ts, _ = parsed[-1]
            if ts == prev_ts:
                raise GridTableError(f"row {row_num}: duplicate timestamp {ts.isoformat()}")
            if ts < prev_ts:
                raise GridTableError(
                    f"row {row_num}: timestamps must be ascending; "
                    f"{ts.isoformat()} is before {prev_ts.isoformat()}"
                )
        parsed.append((ts, value))
    return GridTable(kind="series", label=label, basis=basis, rows=tuple(parsed))
