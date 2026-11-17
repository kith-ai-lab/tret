"""The row type and CSV writer every register source shares.

`write_register_csv` emits exactly the columns `parse_register` accepts
(`tret.batches.profiles.disclosure.CSV_COLUMNS`), so a source only has to produce
`RegisterRow`s."""
from __future__ import annotations

import csv
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Iterable

from tret.batches.profiles.disclosure import CSV_COLUMNS


@dataclass
class RegisterRow:
    company: str
    market: str
    domicile_country: str = ""
    sector: str = ""
    category: str = ""
    expected_form: str = ""
    fiscal_year: int | None = None
    known_urls: str = ""  # several urls separated by "|"
    notes: str = ""
    lei: str = ""
    cik: str = ""
    ticker: str = ""
    source_filing_url: str = ""


# The dataclass must stay in step with the parser's columns.
assert tuple(f.name for f in fields(RegisterRow)) == CSV_COLUMNS


def _cell(value: object) -> str:
    return "" if value is None else str(value)


def write_register_csv(rows: Iterable[RegisterRow], path: str | Path) -> int:
    """Write `rows` as a register CSV (UTF-8, header first); returns the row count."""
    n = 0
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(CSV_COLUMNS)
        for row in rows:
            writer.writerow([_cell(getattr(row, c)) for c in CSV_COLUMNS])
            n += 1
    return n
