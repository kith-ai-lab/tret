"""R07/R08: dataset import (CSV + XLSX), the normalised factor format, the
facility_inventory method, import_table and run_method's `inputs` mapping.

All factor numbers below are invented test values, never real factors."""
from __future__ import annotations

import io
import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import ARRAY, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sqlalchemy.ext.compiler import compiles

from tret.db.models import Base, Dataset, DatasetRow, Document, MethodRun, Project, Workspace
from tret.engine.tools import RunContext, ToolError, import_table, run_method
from tret.packs.loader import PackEditionSkipped, install_pack
from tret.services import dataset_import
from tret.services.dataset_import import DatasetExists, DatasetImportError


@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(type_, compiler, **kw):  # pragma: no cover - DDL only
    return "JSON"


@compiles(ARRAY, "sqlite")
def _array_on_sqlite(type_, compiler, **kw):  # pragma: no cover - DDL only
    return "JSON"


PACK_DIR = Path(__file__).resolve().parents[2] / "packs" / "kith-cohort"

FACTOR_CSV = (
    "category,fuel_or_subregion,unit,co2e_kg_per_unit,gwp_basis\n"
    "stationary_combustion,natural_gas,therm,5.0,TEST-GWP\n"
    "stationary_combustion,diesel,gallon,10.0,TEST-GWP\n"
    "mobile_combustion,diesel,gallon,11.0,TEST-GWP\n"
    "refrigerant,R-410A,lb,1000.0,TEST-GWP\n"
    "electricity,state:TN,kWh,0.5,TEST-GWP\n"
    "electricity,SRTV,kWh,0.4,TEST-GWP\n"
)
ACTIVITY_CSV = (
    "facility_id,portco_slug,portco_name,facility_type,city,state,reporting_year,electricity_kwh,"
    "natural_gas_therms,diesel_gallons,gasoline_gallons,propane_gallons,refrigerant_r22_lbs,"
    "refrigerant_r410a_lbs,fleet_diesel_gallons,fleet_gasoline_gallons,data_source,notes\n"
    "F1,acme,Acme,plant,Nashville,TN,2024,1000,100,,,,,2,10,,bill,\n"
    "F2,acme,Acme,office,Memphis,TN,2024,2000,,,,,5,,,,bill,\n"
    "F3,beta,Beta,shop,Austin,TX,2024,100,,,,,,,,,bill,\n"
)
PROV = dict(source="Test Source", edition="2025", retrieved_on="2026-11-20", url="http://x.test", table="T1")


@pytest.fixture()
async def db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'di.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        ws = Workspace(name="W", settings={"edition": "cohort"})
        session.add(ws)
        await session.flush()
        session.add(Project(workspace_id=ws.id, name="P"))
        await session.commit()
        yield session
    await engine.dispose()


@pytest.fixture()
async def project(db):
    return (await db.execute(select(Project))).scalars().one()


def _xlsx(rows: list[list]) -> bytes:
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ── parsing / provenance ──────────────────────────────────────────────────────
def test_parse_csv_and_xlsx_agree():
    cols, rows = dataset_import.parse_table("a.csv", ACTIVITY_CSV.encode())
    assert cols[0] == "facility_id" and len(rows) == 3 and rows[0]["electricity_kwh"] == "1000"
    x = _xlsx([cols] + [[r[c] for c in cols] for r in rows])
    cols2, rows2 = dataset_import.parse_table("a.xlsx", x)
    assert (cols2, rows2) == (cols, rows)


def test_parse_rejects_bad_files():
    with pytest.raises(DatasetImportError, match="only .csv"):
        dataset_import.parse_table("a.pdf", b"x")
    with pytest.raises(DatasetImportError, match="duplicate"):
        dataset_import.parse_table("a.csv", b"a,a\n1,2\n")
    with pytest.raises(DatasetImportError, match="no rows"):
        dataset_import.parse_table("a.csv", b"\n\n")


async def test_factor_import_puts_provenance_on_every_row(db, project):
    ds, created = await dataset_import.import_factor_table(
        db, project_id=project.id, name="epa_ghg_factors_2025", filename="f.csv",
        data=FACTOR_CSV.encode(), **PROV,
    )
    assert created and ds.row_count == 6
    rows = (await db.execute(select(DatasetRow).where(DatasetRow.dataset_id == ds.id))).scalars().all()
    assert len(rows) == 6
    for r in rows:
        for col in dataset_import.FACTOR_COLUMNS:
            assert col in r.data
        for col in ("source", "edition", "retrieved_on", "url", "table"):
            assert r.data[col] == PROV[col]
    assert ds.schema_json["source"]["sha256"] == dataset_import.sha256_hex(FACTOR_CSV.encode())


async def test_factor_import_xlsx_keeps_per_row_provenance(db, project):
    header = dataset_import.FACTOR_COLUMNS
    rows = [
        ["electricity", "SRTV", "MWh", None, None, None, 400.5, "TEST", "eGRID", "2023", "SRL", "2026-01-01", "u"],
    ]
    ds, _ = await dataset_import.import_factor_table(
        db, project_id=project.id, name="f", filename="f.xlsx", data=_xlsx([header, *rows]),
        source="ignored default", edition="ignored", retrieved_on="2000-01-01",
    )
    row = (await db.execute(select(DatasetRow).where(DatasetRow.dataset_id == ds.id))).scalars().one()
    assert row.data["source"] == "eGRID" and row.data["edition"] == "2023"  # file wins over defaults
    assert row.data["co2e_kg_per_unit"] == "400.5"


@pytest.mark.parametrize(
    "csv_text,kwargs,match",
    [
        ("category,unit,co2e_kg_per_unit\nx,y,1\n", PROV, "missing required columns"),
        ("category,fuel_or_subregion,unit,co2e_kg_per_unit,gwp_basis\nx,y,z,abc,B\n", PROV, "not a number"),
        ("category,fuel_or_subregion,unit,co2e_kg_per_unit,gwp_basis\nx,y,z,-1,B\n", PROV, "non-negative"),
        ("category,fuel_or_subregion,unit,co2e_kg_per_unit,gwp_basis\nx,,z,1,B\n", PROV, "fuel_or_subregion is empty"),
        ("category,fuel_or_subregion,unit,co2e_kg_per_unit,gwp_basis\nx,y,z,1,B\n", {}, "provenance column 'source'"),
        ("category,fuel_or_subregion,unit,co2_kg_per_unit,gwp_basis\nx,y,z,1,B\n", PROV, "cannot be derived"),
        ("category,fuel_or_subregion,unit,co2e_kg_per_unit\nx,y,z,1\n", PROV, "gwp_basis is empty"),
    ],
)
def test_factor_validation_rejects(csv_text, kwargs, match):
    cols, rows = dataset_import.parse_table("f.csv", csv_text.encode())
    with pytest.raises(DatasetImportError, match=match):
        dataset_import.normalise_factor_rows(cols, rows, **kwargs)


def test_co2e_is_derived_only_from_supplied_gwps():
    cols, rows = dataset_import.parse_table(
        "f.csv",
        b"category,fuel_or_subregion,unit,co2_kg_per_unit,ch4_g_per_unit,n2o_g_per_unit\nx,y,z,10,2000,1000\n",
    )
    out = dataset_import.normalise_factor_rows(cols, rows, gwp={"ch4": 3.0, "n2o": 5.0}, gwp_basis="TEST", **PROV)
    assert float(out[0]["co2e_kg_per_unit"]) == pytest.approx(10 + 2 * 3.0 + 1 * 5.0)
    assert out[0]["gwp_basis"] == "TEST"


async def test_reimport_policy(db, project):
    kw = dict(project_id=project.id, name="t", filename="a.csv")
    ds, created = await dataset_import.import_table(db, data=b"a\n1\n", **kw)
    again, created2 = await dataset_import.import_table(db, data=b"a\n1\n", **kw)
    assert created and not created2 and again.id == ds.id  # same bytes: no-op
    with pytest.raises(DatasetExists, match="already exists"):
        await dataset_import.import_table(db, data=b"a\n2\n", **kw)
    replaced, created3 = await dataset_import.import_table(db, data=b"a\n2\n", replace=True, **kw)
    assert created3 and replaced.row_count == 1
    assert len((await db.execute(select(Dataset).where(Dataset.name == "t"))).scalars().all()) == 1


# ── pack install gating ───────────────────────────────────────────────────────
async def test_pack_installs_in_cohort_only(db, project, monkeypatch):
    monkeypatch.setenv("TRET_EDITION", "standard")
    from tret.config import get_settings

    get_settings.cache_clear()
    pack = await install_pack(db, PACK_DIR, project.workspace_id, project.id)
    assert pack.slug == "kith-cohort"  # the workspace's own override is cohort
    other = Workspace(name="Std", settings={})
    db.add(other)
    await db.flush()
    p2 = Project(workspace_id=other.id, name="P2")
    db.add(p2)
    await db.flush()
    with pytest.raises(PackEditionSkipped):
        await install_pack(db, PACK_DIR, other.id, p2.id)
    get_settings.cache_clear()


# ── facility_inventory via the real runner ────────────────────────────────────
async def _install_and_load(db, project, factors: str | None = FACTOR_CSV):
    pack = await install_pack(db, PACK_DIR, project.workspace_id, project.id)
    if factors is not None:
        await dataset_import.import_factor_table(
            db, project_id=project.id, name="epa_ghg_factors_2025", filename="f.csv",
            data=factors.encode(), **PROV,
        )
    await dataset_import.import_table(
        db, project_id=project.id, name="northwood_2024", filename="n.csv", data=ACTIVITY_CSV.encode()
    )
    await db.commit()
    return pack


def _ctx(db, project, pack):
    return RunContext(
        db=db, run_id=uuid.uuid4(), project_id=project.id, pack_id=pack.id, doctrine_sha=None,
        model_used=None, document_ids=[], output_schemas={}, workspace_id=project.workspace_id,
        pack_manifest=pack.manifest, pack_dir=pack.source_path,
    )


def _rows(out: str) -> list[dict]:
    return json.loads(out)["rows"]


async def test_facility_inventory_per_facility_and_portco(db, project):
    pack = await _install_and_load(db, project)
    ctx = _ctx(db, project, pack)
    out = await run_method(ctx, "facility_inventory", {"subregion_map": {"F1": "SRTV"}}, {"activity": "northwood_2024"})
    rows = _rows(out)
    fac = {r["facility_id"]: r for r in rows if r["row_type"] == "facility"}
    # F1: gas 100*5 + diesel(fleet) 10*11 + R-410A 2*1000 = 2610 kg ; elec SRTV 1000*0.4 = 400 kg
    assert fac["F1"]["scope1_tco2e"] == pytest.approx(2.610)
    assert fac["F1"]["scope2_location_tco2e"] == pytest.approx(0.400)
    assert "eGRID subregion SRTV" in fac["F1"]["electricity_factor_basis"]
    # F2: state rate 2000*0.5 = 1000 kg; R-22 has no factor -> skipped, not guessed
    assert fac["F2"]["scope2_location_tco2e"] == pytest.approx(1.0)
    assert "state rate TN" in fac["F2"]["electricity_factor_basis"]
    assert fac["F2"]["scope1_tco2e"] == 0 and fac["F2"]["fields_skipped"] == 1
    # F3: Texas has no state factor in this dataset
    assert fac["F3"]["scope2_location_tco2e"] == 0 and fac["F3"]["fields_skipped"] == 1
    skipped = {(r["facility_id"], r["field"]): r["reason"] for r in rows if r["row_type"] == "skipped"}
    assert "no factor" in skipped[("F2", "refrigerant_r22_lbs")]
    assert "state:TX" in skipped[("F3", "electricity_kwh")]
    portco = {r["portco_slug"]: r for r in rows if r["row_type"] == "portco"}
    assert portco["acme"]["total_tco2e"] == pytest.approx(2.610 + 0.4 + 1.0)
    assert portco["acme"]["facilities"] == 2 and portco["acme"]["facilities_incomplete"] == 1
    assert portco["beta"]["total_tco2e"] == 0
    total = next(r for r in rows if r["row_type"] == "portfolio")
    assert total["total_tco2e"] == pytest.approx(4.01)
    # Provenance travels with each facility row, and the run records dataset names + hashes.
    assert fac["F1"]["factor_sources"].startswith("Test Source 2025")
    rec = (await db.execute(select(MethodRun))).scalars().one()
    assert rec.input_summary["activity"]["dataset"] == "northwood_2024"
    assert rec.input_summary["activity"]["source_sha256"] == dataset_import.sha256_hex(ACTIVITY_CSV.encode())
    assert rec.input_summary["factors"]["dataset"] == "epa_ghg_factors_2025"
    assert rec.code_sha and rec.output_hash


async def test_method_nothing_guessed_without_factor_and_filters(db, project):
    pack = await _install_and_load(db, project)
    ctx = _ctx(db, project, pack)
    rows = _rows(await run_method(ctx, "facility_inventory", {"portco_slug": "beta"}, {"activity": "northwood_2024"}))
    assert [r["facility_id"] for r in rows if r["row_type"] == "facility"] == ["F3"]


async def test_missing_factor_dataset_points_at_the_docs(db, project):
    pack = await _install_and_load(db, project, factors=None)
    ctx = _ctx(db, project, pack)
    with pytest.raises(ToolError) as e:
        await run_method(ctx, "facility_inventory", {}, {"activity": "northwood_2024"})
    assert "docs/learning-edition.md" in str(e.value) and "epa_ghg_factors_2025" in str(e.value)


async def test_missing_activity_input_says_how(db, project):
    pack = await _install_and_load(db, project)
    ctx = _ctx(db, project, pack)
    with pytest.raises(ToolError, match="import_table"):
        await run_method(ctx, "facility_inventory", {})


async def test_inputs_mapping_validation(db, project):
    pack = await _install_and_load(db, project)
    ctx = _ctx(db, project, pack)
    with pytest.raises(ToolError, match="does not declare"):
        await run_method(ctx, "facility_inventory", {}, {"nope": "northwood_2024"})
    with pytest.raises(ToolError, match="mapping"):
        await run_method(ctx, "facility_inventory", {}, {"activity": 3})  # type: ignore[arg-type]


async def test_empty_factor_table_is_refused_at_import(db, project):
    with pytest.raises(DatasetImportError, match="no data rows"):
        await dataset_import.import_factor_table(
            db, project_id=project.id, name="f", filename="f.csv",
            data=b"category,fuel_or_subregion,unit,co2e_kg_per_unit,gwp_basis\n", **PROV,
        )


# ── import_table tool ─────────────────────────────────────────────────────────
async def _document(db, project, tmp_path, name: str, data: bytes) -> Document:
    path = tmp_path / name
    path.write_bytes(data)
    doc = Document(
        project_id=project.id, filename=name, content_type="text/csv", byte_size=len(data),
        storage_path=str(path), sha256=dataset_import.sha256_hex(data), extraction_status="done",
    )
    db.add(doc)
    await db.flush()
    return doc


async def test_import_table_tool_records_document_and_sha(db, project, tmp_path):
    doc = await _document(db, project, tmp_path, "n.csv", ACTIVITY_CSV.encode())
    ctx = RunContext(db=db, run_id=uuid.uuid4(), project_id=project.id, pack_id=None, doctrine_sha=None,
                     model_used=None, document_ids=[doc.id], output_schemas={}, workspace_id=project.workspace_id)
    out = json.loads(await import_table(ctx, name="member_file", document_id=str(doc.id)))
    assert out["rows"] == 3 and out["created"] and out["source_document_id"] == str(doc.id)
    ds = (await db.execute(select(Dataset).where(Dataset.name == "member_file"))).scalars().one()
    assert ds.document_id == doc.id
    assert ds.schema_json["source"]["sha256"] == dataset_import.sha256_hex(ACTIVITY_CSV.encode())
    # different contents under the same name are refused
    doc2 = await _document(db, project, tmp_path, "m.csv", b"a\n1\n")
    ctx.document_ids.append(doc2.id)
    with pytest.raises(ToolError, match="already exists"):
        await import_table(ctx, name="member_file", document_id=str(doc2.id))
    with pytest.raises(ToolError, match="exactly one"):
        await import_table(ctx, name="x")
    with pytest.raises(ToolError, match="not found"):
        await import_table(ctx, name="x", document_id=str(uuid.uuid4()))


async def test_import_table_is_cohort_only(db, project, tmp_path):
    ws = await db.get(Workspace, project.workspace_id)
    ws.settings = {"edition": "standard"}
    await db.flush()
    doc = await _document(db, project, tmp_path, "n.csv", b"a\n1\n")
    ctx = RunContext(db=db, run_id=uuid.uuid4(), project_id=project.id, pack_id=None, doctrine_sha=None,
                     model_used=None, document_ids=[doc.id], output_schemas={}, workspace_id=project.workspace_id)
    with pytest.raises(ToolError, match="cohort"):
        await import_table(ctx, name="x", document_id=str(doc.id))


def test_pack_has_no_factor_numbers_shipped():
    assert not list(PACK_DIR.glob("**/*.csv"))
    assert not list(PACK_DIR.glob("**/*.xlsx"))


def test_an_xlsx_member_that_inflates_past_its_declared_size_is_refused():
    """openpyxl reads the workbook parts with an unbounded `ZipFile.read`, so
    a member that under-declares its size must be refused before it opens."""
    import struct
    import zipfile

    from tret.services.dataset_import import DatasetImportError, parse_table

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("xl/sharedStrings.xml", b"\x00" * (8 * 1024 * 1024))
    raw = bytearray(buf.getvalue())
    struct.pack_into("<L", raw, 22, 4096)
    struct.pack_into("<L", raw, raw.find(b"PK\x01\x02") + 24, 4096)
    with pytest.raises(DatasetImportError, match="expands past its declared"):
        parse_table("bomb.xlsx", bytes(raw))


def test_an_xlsx_over_the_row_limit_is_refused_while_reading(monkeypatch):
    import openpyxl

    from tret.services import dataset_import
    from tret.services.dataset_import import DatasetImportError, parse_table

    monkeypatch.setattr(dataset_import, "MAX_ROWS", 3)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["name", "value"])
    for i in range(10):
        ws.append([f"row-{i}", i])
        ws.append([None, None])  # blank rows never count toward the limit
    out = io.BytesIO()
    wb.save(out)
    with pytest.raises(DatasetImportError, match="more than 3 rows"):
        parse_table("big.xlsx", out.getvalue())

    monkeypatch.setattr(dataset_import, "MAX_ROWS", 10)
    columns, rows = parse_table("big.xlsx", out.getvalue())
    assert columns == ["name", "value"] and len(rows) == 10


def _xlsx_bytes(rows: list[list]) -> bytes:
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def test_a_cell_in_the_last_column_does_not_pad_every_row():
    """openpyxl pads a row to the sheet's width: one value in column XFD per
    row used to make each row 16,384 slots (a 16 KB file, gigabytes held).
    Rows are now read at most MAX_COLUMNS + 1 wide; data past the header
    width was always dropped, so the result is unchanged."""
    import openpyxl

    from tret.services.dataset_import import parse_table

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["name", "value"])
    ws.append(["a", 1])
    for i in range(3, 3003):
        ws.cell(row=i, column=16384, value="x")
    out = io.BytesIO()
    wb.save(out)
    columns, rows = parse_table("wide.xlsx", out.getvalue())
    assert columns == ["name", "value"]
    assert rows == [{"name": "a", "value": "1"}]


def test_a_header_wider_than_the_column_limit_is_refused(monkeypatch):
    from tret.services import dataset_import
    from tret.services.dataset_import import DatasetImportError, parse_table

    monkeypatch.setattr(dataset_import, "MAX_COLUMNS", 3)
    data = _xlsx_bytes([["a", "b", "c", "d"], [1, 2, 3, 4]])
    with pytest.raises(DatasetImportError, match="past column 3"):
        parse_table("wide.xlsx", data)
    monkeypatch.setattr(dataset_import, "MAX_COLUMNS", 4)
    columns, rows = parse_table("wide.xlsx", data)
    assert columns == ["a", "b", "c", "d"] and len(rows) == 1


def test_an_xlsx_over_the_cell_limit_is_refused(monkeypatch):
    from tret.services import dataset_import
    from tret.services.dataset_import import DatasetImportError, parse_table

    monkeypatch.setattr(dataset_import, "MAX_CELLS", 10)
    data = _xlsx_bytes([["a", "b", "c"]] + [[i, i, i] for i in range(5)])
    with pytest.raises(DatasetImportError, match="more than 10 cells"):
        parse_table("cells.xlsx", data)


def test_a_non_zip_xlsx_is_a_clean_import_error():
    from tret.services.dataset_import import DatasetImportError, parse_table

    with pytest.raises(DatasetImportError, match="Could not open"):
        parse_table("broken.xlsx", b"not a zip at all")
