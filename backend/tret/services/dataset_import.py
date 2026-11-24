"""Import CSV/XLSX tables into project Datasets (R07, R08).

Two entry points:

* `import_table` — any member-supplied table (an activity workbook, say) into a
  Dataset, recording the source document id and the file's sha256 in
  `schema_json["source"]`. Used by the cohort `import_table` tool.
* `import_factor_table` — an emission-factor spreadsheet in tret's normalised
  long format (FACTOR_COLUMNS), every row carrying provenance. No factor value
  is typed anywhere in tret: the numbers come from the file you supply.

Re-import policy: refuse. A Dataset name is its identity in `lookup_dataset`,
`run_method` and every audit record; silently swapping the rows under a name
would change what old method runs cite. Importing the same bytes again (same
sha256) is a no-op that returns the existing dataset; different bytes under an
existing name raise `DatasetExists`, and the caller picks a new name (e.g. a
`_v2` suffix) or passes `replace=True` (CLI: `--replace`) knowingly.

CLI:
  python -m tret.services.dataset_import --project <uuid> --name epa_ghg_factors_2025 \\
      --file hub.xlsx --source "EPA GHG Emission Factors Hub" --edition 2025 \\
      --retrieved-on 2026-11-20 [--table "Table 1"] [--url ...] [--sheet NAME] \\
      [--gwp ch4=28 --gwp n2o=265 --gwp-basis "IPCC AR5"] [--replace]
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import io
import json
import math
import uuid
from datetime import date

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from tret.db.models import Dataset, DatasetRow

FACTOR_COLUMNS = [
    "category", "fuel_or_subregion", "unit",
    "co2_kg_per_unit", "ch4_g_per_unit", "n2o_g_per_unit", "co2e_kg_per_unit", "gwp_basis",
    "source", "edition", "table", "retrieved_on", "url",
]
PROVENANCE_COLUMNS = ["source", "edition", "table", "retrieved_on", "url"]
REQUIRED_PER_ROW = ["category", "fuel_or_subregion", "unit"]
NUMERIC_COLUMNS = ["co2_kg_per_unit", "ch4_g_per_unit", "n2o_g_per_unit", "co2e_kg_per_unit"]
MAX_ROWS = 50_000
# An .xlsx row is read at most this wide. openpyxl pads each row to its last
# cell's column (or the sheet's declared width), so one cell in column XFD
# would otherwise make every row 16,384 slots: a 16 KB upload holding gigabytes.
# A header wider than this is refused rather than cut.
MAX_COLUMNS = 500
# Non-empty cells kept across a whole sheet (50,000 rows of 40 columns).
MAX_CELLS = 2_000_000
MAX_BYTES = 50 * 1024 * 1024


class DatasetImportError(Exception):
    pass


class DatasetExists(DatasetImportError):
    def __init__(self, name: str):
        super().__init__(
            f"A dataset named '{name}' already exists with different contents. Choose another "
            f"name (for example '{name}_v2') — existing method runs cite this one by name."
        )
        self.name = name


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ── parsing ───────────────────────────────────────────────────────────────────
def _clean_header(h: object) -> str:
    return str(h if h is not None else "").strip().replace("﻿", "")


def _cell(v: object) -> str:
    """Every cell becomes a trimmed string, like pack-seeded CSV datasets."""
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer() and abs(v) < 1e15:
        return str(int(v))
    if isinstance(v, (date,)):
        return v.isoformat()
    return str(v).strip()


def parse_table(filename: str, data: bytes, *, sheet: str | None = None) -> tuple[list[str], list[dict]]:
    """(columns, rows) from a .csv/.tsv/.xlsx. Raises DatasetImportError."""
    if len(data) > MAX_BYTES:
        raise DatasetImportError(f"{filename} is {len(data)} bytes; the import limit is {MAX_BYTES}.")
    lower = filename.lower()
    if lower.endswith(".xlsx"):
        raw = _xlsx_rows(filename, data, sheet)
    elif lower.endswith((".csv", ".tsv", ".txt")):
        text = _decode(data)
        delimiter = "\t" if lower.endswith(".tsv") else ","
        raw = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    else:
        raise DatasetImportError(f"Cannot import {filename}: only .csv, .tsv and .xlsx tables are supported.")
    raw = [r for r in raw if any(_cell(c) for c in r)]
    if not raw:
        raise DatasetImportError(f"{filename} has no rows.")
    columns = [_clean_header(h) for h in raw[0]]
    if not all(columns):
        raise DatasetImportError(f"{filename}: the header row has an empty column name.")
    if len(set(columns)) != len(columns):
        dupes = sorted({c for c in columns if columns.count(c) > 1})
        raise DatasetImportError(f"{filename}: duplicate column names {dupes}.")
    rows = []
    for r in raw[1:]:
        cells = [_cell(c) for c in r][: len(columns)]
        cells += [""] * (len(columns) - len(cells))
        rows.append(dict(zip(columns, cells)))
    if len(rows) > MAX_ROWS:
        raise DatasetImportError(f"{filename} has {len(rows)} rows; the limit is {MAX_ROWS}.")
    return columns, rows


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252")


def _xlsx_rows(filename: str, data: bytes, sheet: str | None) -> list[list]:
    import zipfile

    import openpyxl

    from tret.services.documents import MAX_ZIP_UNCOMPRESSED_BYTES, _refuse_zip_bombs

    # Bounded before openpyxl opens it: the declared total, and every member
    # inflated in bounded chunks against its own declared size (openpyxl reads
    # the workbook and shared strings with an unbounded `ZipFile.read`). The
    # sheet itself streams (`read_only`), so the xlsx extractor's ceiling
    # applies; the rows kept are bounded below.
    try:
        _refuse_zip_bombs(filename, data, MAX_ZIP_UNCOMPRESSED_BYTES)
    except ValueError as e:
        raise DatasetImportError(str(e)) from e
    except zipfile.BadZipFile as e:
        raise DatasetImportError(f"Could not open {filename} as a workbook: {e}") from e
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as e:  # noqa: BLE001 - openpyxl raises many types for a bad file
        raise DatasetImportError(f"Could not open {filename} as a workbook: {e}") from e
    try:
        if sheet is not None:
            if sheet not in wb.sheetnames:
                raise DatasetImportError(f"{filename} has no sheet '{sheet}'. Sheets: {wb.sheetnames}")
            ws = wb[sheet]
        else:
            ws = wb[wb.sheetnames[0]]
        # The sheet's declared `<dimension>` is ignored (it pads every row,
        # gap rows included, to its width) and rows are read at most
        # MAX_COLUMNS + 1 wide: a value in that extra column means the sheet
        # is wider than the limit. Empty rows are dropped here rather than
        # after the fact (parse_table drops them anyway), trailing empty cells
        # are trimmed, and the import is refused as soon as it passes the row
        # or cell limit, so nothing near those sizes is ever held whole.
        ws.reset_dimensions()
        rows: list[list] = []
        cells = 0
        for r in ws.iter_rows(values_only=True, max_col=MAX_COLUMNS + 1):
            if r.count(None) == len(r) or not any(_cell(c) for c in r):
                continue
            if len(r) > MAX_COLUMNS and _cell(r[MAX_COLUMNS]):
                raise DatasetImportError(
                    f"{filename} has data past column {MAX_COLUMNS}; the limit is {MAX_COLUMNS} columns."
                )
            row = list(r[:MAX_COLUMNS])
            while row and row[-1] is None:
                row.pop()
            rows.append(row)
            cells += len(row) - row.count(None)
            if len(rows) > MAX_ROWS + 1:  # + the header row
                raise DatasetImportError(f"{filename} has more than {MAX_ROWS} rows; the limit is {MAX_ROWS}.")
            if cells > MAX_CELLS:
                raise DatasetImportError(f"{filename} has more than {MAX_CELLS} cells; the limit is {MAX_CELLS}.")
        return rows
    finally:
        wb.close()


# ── persistence ───────────────────────────────────────────────────────────────
async def _existing(db: AsyncSession, project_id: uuid.UUID, name: str) -> Dataset | None:
    return (
        await db.execute(select(Dataset).where(Dataset.project_id == project_id, Dataset.name == name))
    ).scalars().first()


async def store_dataset(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    name: str,
    columns: list[str],
    rows: list[dict],
    sha256: str,
    source_filename: str,
    document_id: uuid.UUID | None = None,
    extra_schema: dict | None = None,
    replace: bool = False,
) -> tuple[Dataset, bool]:
    """Create the Dataset. Returns (dataset, created); created is False when the
    same bytes were already imported under this name."""
    name = (name or "").strip()
    if not name or len(name) > 120:
        raise DatasetImportError("Dataset name must be 1-120 characters.")
    old = await _existing(db, project_id, name)
    if old is not None:
        if (old.schema_json or {}).get("source", {}).get("sha256") == sha256 and not replace:
            return old, False
        if not replace:
            raise DatasetExists(name)
        await db.execute(delete(DatasetRow).where(DatasetRow.dataset_id == old.id))
        await db.delete(old)
        await db.flush()
    schema = {
        "columns": columns,
        "source": {"sha256": sha256, "filename": source_filename,
                   "document_id": str(document_id) if document_id else None},
        **(extra_schema or {}),
    }
    ds = Dataset(project_id=project_id, name=name, document_id=document_id, schema_json=schema,
                 row_count=len(rows))
    db.add(ds)
    await db.flush()
    for i, row in enumerate(rows):
        db.add(DatasetRow(dataset_id=ds.id, row_index=i, data=row))
    await db.flush()
    return ds, True


async def import_table(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    name: str,
    filename: str,
    data: bytes,
    document_id: uuid.UUID | None = None,
    sheet: str | None = None,
    replace: bool = False,
) -> tuple[Dataset, bool]:
    # Off the event loop: the zip guard and the sheet read are CPU-bound.
    columns, rows = await asyncio.to_thread(parse_table, filename, data, sheet=sheet)
    return await store_dataset(
        db, project_id=project_id, name=name, columns=columns, rows=rows, sha256=sha256_hex(data),
        source_filename=filename, document_id=document_id, replace=replace,
    )


def _num(value: str, column: str, line: int) -> float | None:
    if value == "":
        return None
    try:
        f = float(value.replace(",", ""))
    except ValueError:
        raise DatasetImportError(f"row {line}: {column} '{value}' is not a number") from None
    if not math.isfinite(f) or f < 0:
        raise DatasetImportError(f"row {line}: {column} '{value}' must be a finite, non-negative number")
    return f


def normalise_factor_rows(
    columns: list[str],
    rows: list[dict],
    *,
    source: str | None = None,
    edition: str | None = None,
    table: str | None = None,
    retrieved_on: str | None = None,
    url: str | None = None,
    gwp: dict[str, float] | None = None,
    gwp_basis: str | None = None,
) -> list[dict]:
    """Validate a factor table against the normalised format and fill provenance.

    Per-row provenance in the file wins; the keyword arguments fill blanks. Every
    output row has all of FACTOR_COLUMNS, and non-empty source, edition and
    retrieved_on. `co2e_kg_per_unit` is taken from the file; when it is blank it is
    computed ONLY if the caller supplied GWPs (`gwp={"ch4": .., "n2o": ..}`, a
    value they read from the source's own GWP table, recorded in gwp_basis).
    Otherwise the row is rejected: nothing here guesses a conversion.
    """
    missing = [c for c in REQUIRED_PER_ROW if c not in columns]
    if missing:
        raise DatasetImportError(
            f"Factor table is missing required columns {missing}. Expected columns: {FACTOR_COLUMNS}"
        )
    if not any(c in columns for c in NUMERIC_COLUMNS):
        raise DatasetImportError(f"Factor table has none of the numeric columns {NUMERIC_COLUMNS}.")
    fill = {"source": source, "edition": edition, "table": table, "retrieved_on": retrieved_on, "url": url}
    out: list[dict] = []
    for i, row in enumerate(rows, start=2):  # line 1 is the header
        for c in REQUIRED_PER_ROW:
            if not row.get(c, "").strip():
                raise DatasetImportError(f"row {i}: {c} is empty")
        new = {c: row.get(c, "") for c in FACTOR_COLUMNS}
        for c, default in fill.items():
            if not new[c] and default:
                new[c] = str(default)
        for c in ("source", "edition", "retrieved_on"):
            if not new[c]:
                raise DatasetImportError(
                    f"row {i}: provenance column '{c}' is empty and no value was supplied for it"
                )
        co2 = _num(new["co2_kg_per_unit"], "co2_kg_per_unit", i)
        ch4 = _num(new["ch4_g_per_unit"], "ch4_g_per_unit", i)
        n2o = _num(new["n2o_g_per_unit"], "n2o_g_per_unit", i)
        co2e = _num(new["co2e_kg_per_unit"], "co2e_kg_per_unit", i)
        if co2e is None:
            if co2 is not None and gwp and "ch4" in gwp and "n2o" in gwp:
                co2e = co2 + (ch4 or 0.0) / 1000 * gwp["ch4"] + (n2o or 0.0) / 1000 * gwp["n2o"]
                new["co2e_kg_per_unit"] = repr(co2e)
                if not new["gwp_basis"]:
                    new["gwp_basis"] = gwp_basis or f"CH4={gwp['ch4']}, N2O={gwp['n2o']}"
            else:
                raise DatasetImportError(
                    f"row {i}: co2e_kg_per_unit is empty and cannot be derived. Supply the "
                    "source's CO2e figure in the file, or pass the GWPs it uses (--gwp ch4=.. "
                    "--gwp n2o=..) so tret can combine the gases; tret never assumes a GWP."
                )
        if not new["gwp_basis"]:
            raise DatasetImportError(f"row {i}: gwp_basis is empty (say which GWP vintage the CO2e uses)")
        out.append(new)
    if not out:
        raise DatasetImportError("Factor table has no data rows.")
    return out


async def import_factor_table(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    name: str,
    filename: str,
    data: bytes,
    source: str | None = None,
    edition: str | None = None,
    table: str | None = None,
    retrieved_on: str | None = None,
    url: str | None = None,
    sheet: str | None = None,
    gwp: dict[str, float] | None = None,
    gwp_basis: str | None = None,
    document_id: uuid.UUID | None = None,
    replace: bool = False,
) -> tuple[Dataset, bool]:
    # Off the event loop: the zip guard and the sheet read are CPU-bound.
    columns, rows = await asyncio.to_thread(parse_table, filename, data, sheet=sheet)
    factors = normalise_factor_rows(
        columns, rows, source=source, edition=edition, table=table, retrieved_on=retrieved_on,
        url=url, gwp=gwp, gwp_basis=gwp_basis,
    )
    return await store_dataset(
        db, project_id=project_id, name=name, columns=FACTOR_COLUMNS, rows=factors,
        sha256=sha256_hex(data), source_filename=filename, document_id=document_id,
        extra_schema={"format": "tret.factor_table/v1"}, replace=replace,
    )


# ── CLI ───────────────────────────────────────────────────────────────────────
def _parse_gwp(items: list[str] | None) -> dict[str, float] | None:
    if not items:
        return None
    out: dict[str, float] = {}
    for item in items:
        k, _, v = item.partition("=")
        if not k or not v:
            raise SystemExit(f"--gwp expects gas=value (got {item!r})")
        out[k.strip().lower()] = float(v)
    return out


async def _run_cli(args: argparse.Namespace) -> None:
    from tret.db.engine import get_session_factory  # deferred: needs the server extra

    data = open(args.file, "rb").read()
    async with get_session_factory()() as db:
        ds, created = await import_factor_table(
            db, project_id=uuid.UUID(args.project), name=args.name,
            filename=args.file.rsplit("/", 1)[-1], data=data, source=args.source,
            edition=args.edition, table=args.table, retrieved_on=args.retrieved_on, url=args.url,
            sheet=args.sheet, gwp=_parse_gwp(args.gwp), gwp_basis=args.gwp_basis, replace=args.replace,
        )
        await db.commit()
    print(json.dumps({"dataset": ds.name, "rows": ds.row_count, "created": created}))


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Import a normalised emission-factor table as a Dataset.")
    p.add_argument("--project", required=True, help="project UUID the dataset belongs to")
    p.add_argument("--name", required=True)
    p.add_argument("--file", required=True, help=".csv, .tsv or .xlsx in the normalised format")
    p.add_argument("--source", help="fills blank per-row `source`")
    p.add_argument("--edition", help="fills blank per-row `edition`, e.g. 2025")
    p.add_argument("--table", help="fills blank per-row `table`")
    p.add_argument("--retrieved-on", help="ISO date, fills blank per-row `retrieved_on`")
    p.add_argument("--url", help="fills blank per-row `url`")
    p.add_argument("--sheet", help="workbook sheet (default: the first)")
    p.add_argument("--gwp", action="append", help="gas=value, e.g. ch4=28 (only to derive blank CO2e)")
    p.add_argument("--gwp-basis", help="text recorded in gwp_basis for derived rows")
    p.add_argument("--replace", action="store_true", help="replace an existing dataset of this name")
    args = p.parse_args(argv)
    asyncio.run(_run_cli(args))


if __name__ == "__main__":
    main()
