"""A deliverable reports its own compute footprint.

Runs the real happy-path scenario, hangs an approved draft_section on the run it
produced, and assembles the deliverable through the real export path. The point is
the provenance contract: the footprint that reaches a reader is the estimate the
run actually recorded, it is attributed per section, and it is labelled estimated.
"""
from __future__ import annotations

import pytest
from golden_world import GOLDEN_MODEL
from test_golden_runs import run_happy_path

from bench.db.models import Finding
from bench.services.export import _energy_cell, assemble_deliverable

DELIVERABLE = "tcfd-report"
SECTION_BODY = "Governance oversight of climate risk is exercised by the board.\n"


async def _approved_section(world, run, section: str = "governance") -> Finding:
    async with world.session_factory() as db:
        finding = Finding(
            run_id=run.id,
            project_id=world.project_id,
            pack_id=world.pack_id,
            schema_slug="draft_section",
            subject={"deliverable": DELIVERABLE, "section": section},
            payload={"markdown": SECTION_BODY},
            provenance={"model": GOLDEN_MODEL, "doctrine_sha": world.doctrine_sha},
            status="approved",
        )
        db.add(finding)
        await db.commit()
        return finding


@pytest.fixture
async def assembled(world):
    result = await run_happy_path(world)
    await _approved_section(world, result.run)
    async with world.session_factory() as db:
        return result.run, await assemble_deliverable(db, world.project_id, DELIVERABLE)


async def test_section_provenance_carries_the_run_s_estimated_footprint(assembled):
    run, deliverable = assembled
    section = deliverable["sections"][0]

    assert section["run_id"] == str(run.id)
    assert section["energy_wh"] == float(run.energy_wh)
    assert section["co2e_g"] == run.energy_accounting["co2e_g"]
    # ...and it renders as an explicitly estimated figure in the PDF appendix.
    cell = _energy_cell(section)
    assert cell.startswith("~") and "Wh" in cell and "gCO2e" in cell


async def test_the_deliverable_total_matches_the_runs_behind_it(assembled):
    run, deliverable = assembled
    footprint = deliverable["energy"]

    assert footprint["estimated"] is True
    assert footprint["runs"] == 1
    assert footprint["runs_without_estimate"] == 0
    assert footprint["energy_wh"] == pytest.approx(float(run.energy_wh))
    assert footprint["co2e_g"] == pytest.approx(run.energy_accounting["co2e_g"])
    assert footprint["grid_co2e_g_per_kwh"] == run.energy_accounting["grid_co2e_g_per_kwh"]


async def test_the_footprint_reaches_the_reader_labelled_as_an_estimate(assembled):
    _, deliverable = assembled

    assert "Estimated compute footprint" in deliverable["markdown"]
    assert "gCO2e" in deliverable["markdown"]
    assert "not a measurement" in deliverable["markdown"]
    # The body is still the deliverable: the note is an addition, not a rewrite.
    assert SECTION_BODY.strip() in deliverable["markdown"]
    assert "Estimated compute footprint" in deliverable["html"]


async def test_two_sections_from_one_run_are_not_counted_twice(world):
    result = await run_happy_path(world)
    await _approved_section(world, result.run, "governance")
    await _approved_section(world, result.run, "strategy")
    async with world.session_factory() as db:
        deliverable = await assemble_deliverable(db, world.project_id, DELIVERABLE)

    assert len(deliverable["sections"]) == 2
    assert deliverable["energy"]["runs"] == 1
    assert deliverable["energy"]["energy_wh"] == pytest.approx(float(result.run.energy_wh))
