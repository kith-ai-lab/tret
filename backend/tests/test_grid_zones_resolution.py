"""The `dataset` rung of the factor ladder: a workspace's pinned region
resolving to a bundled Electricity Maps yearly zone average when no override
document above priced that `provider@region` itself.

The rung is only ever *reached*, never *guessed*: an unpinned provider skips
it, a pinned region the table has no zone for falls through to env/global,
and anything a harness/workspace/managed document wrote down still wins.
The bundled table is swapped for a small in-memory one here so the tests do
not depend on whether a maintainer has run the importer yet."""
from __future__ import annotations

from decimal import Decimal

import pytest

from tret.config import Settings
from tret.services import grid_zones
from tret.services.emission_factors import (
    LAYER_DATASET,
    LAYER_ENV,
    LAYER_GLOBAL_DEFAULT,
    LAYER_MANAGED,
    LAYER_PRECEDENCE,
    build_factor_set,
)
from tret.services.grid_zones import ZoneFactor, ZoneTable


def _table(**zones: float) -> ZoneTable:
    return ZoneTable(
        zones={
            zone: ZoneFactor(
                zone=zone, name=zone, year=2025, g_per_kwh=Decimal(str(value)),
                direct_g_per_kwh=None, cfe_pct=None, re_pct=None, estimated=False,
            )
            for zone, value in zones.items()
        },
        attribution="test attribution",
        license="ODbL-1.0",
        download_url="https://example.invalid/",
        generated_at=None,
    )


@pytest.fixture
def de_table(monkeypatch):
    table = _table(DE=339.0)
    monkeypatch.setattr(grid_zones, "load_zone_table", lambda path=None: table)
    return table


PIN_DE = {"grid": {"regions": {"anthropic": "eu-central-1"}}}


def test_dataset_sits_between_env_and_global_default():
    order = list(LAYER_PRECEDENCE)
    assert order.index(LAYER_ENV) < order.index(LAYER_DATASET) < order.index(LAYER_GLOBAL_DEFAULT)


def test_a_pinned_region_resolves_to_the_zone_average(de_table):
    fs = build_factor_set(provider="anthropic", workspace_settings=PIN_DE)
    assert fs.grid.value == Decimal("339.0")
    assert fs.grid.layer == LAYER_DATASET
    assert fs.grid.source == "dataset:zone:DE"
    assert fs.grid.setting == "dataset.grid_zones.DE"
    assert fs.grid.region == "eu-central-1"
    assert fs.grid.temporal == "annual_average"
    assert fs.grid_basis == "location_based"
    assert "DE" in (fs.grid.label or "")
    assert fs.grid.url == "https://app.electricitymaps.com/zone/DE/all/yearly"
    assert fs.grid.as_of == "2025-12-31"


def test_a_pin_straight_to_a_zone_id_works_too(de_table):
    fs = build_factor_set(
        provider="anthropic", workspace_settings={"grid": {"regions": {"anthropic": "de"}}}
    )
    assert fs.grid.layer == LAYER_DATASET
    assert fs.grid.source == "dataset:zone:DE"


def test_an_unpinned_provider_never_touches_the_dataset(de_table):
    fs = build_factor_set(provider="anthropic")
    assert fs.grid.layer != LAYER_DATASET
    assert not fs.grid.source.startswith("dataset:")


def test_a_pinned_region_the_table_lacks_falls_through(de_table):
    fs = build_factor_set(
        provider="anthropic",
        workspace_settings={"grid": {"regions": {"anthropic": "ap-south-1"}}},
    )
    assert fs.grid.layer != LAYER_DATASET
    assert not fs.grid.source.startswith("dataset:")


def test_a_workspace_regional_entry_beats_the_dataset(de_table):
    doc = {
        "grid": {
            "regions": {"anthropic": "eu-central-1"},
            "providers": {
                "anthropic@eu-central-1": {
                    "g_per_kwh": 90, "basis": "location_based", "label": "my own DE figure",
                }
            },
        }
    }
    fs = build_factor_set(provider="anthropic", workspace_settings=doc)
    assert fs.grid.value == Decimal("90")
    assert fs.grid.layer == "workspace"


def test_a_managed_bare_provider_entry_beats_the_dataset(de_table):
    managed = {
        "grid": {
            "providers": {
                "anthropic": {"g_per_kwh": 400, "basis": "location_based", "label": "managed"}
            }
        }
    }
    fs = build_factor_set(
        provider="anthropic", workspace_settings=PIN_DE, managed_settings=managed
    )
    assert fs.grid.value == Decimal("400")
    assert fs.grid.layer == LAYER_MANAGED


def test_a_run_override_beats_the_dataset(de_table):
    fs = build_factor_set(
        provider="anthropic", workspace_settings=PIN_DE, run_overrides={"grid_g_per_kwh": 12}
    )
    assert fs.grid.value == Decimal("12")
    assert fs.grid.layer == "run_override"


def test_an_env_per_provider_factor_beats_the_dataset_and_keeps_its_basis(de_table):
    # Pinning a region says where the load ran, not that the operator's own
    # figure should go — and a market-based figure must never silently
    # become a location-based one.
    settings = Settings(
        grid_factors={
            "anthropic": {"g_per_kwh": 123, "basis": "market_based", "label": "our PPA"}
        }
    )
    fs = build_factor_set(provider="anthropic", settings=settings, workspace_settings=PIN_DE)
    assert fs.grid.value == Decimal("123")
    assert fs.grid.layer == LAYER_ENV
    assert fs.grid_basis == "market_based"


def test_the_legacy_local_setting_beats_the_dataset(de_table):
    settings = Settings(local_grid_co2e_g_per_kwh=55, local_grid_co2e_basis="market_based")
    fs = build_factor_set(
        provider="local", settings=settings,
        workspace_settings={"grid": {"regions": {"local": "eu-central-1"}}},
    )
    assert fs.grid.value == Decimal("55")
    assert fs.grid.layer == LAYER_ENV
    assert fs.grid_basis == "market_based"


def test_an_explicitly_set_global_factor_beats_the_dataset(de_table):
    settings = Settings(grid_co2e_g_per_kwh=470)  # the shipped value, but *set*
    fs = build_factor_set(provider="anthropic", settings=settings, workspace_settings=PIN_DE)
    assert fs.grid.layer == LAYER_ENV
    assert fs.grid.value == Decimal("470")


def test_the_dataset_beats_only_the_shipped_default(de_table):
    fs = build_factor_set(provider="anthropic", settings=Settings(), workspace_settings=PIN_DE)
    assert fs.grid.layer == LAYER_DATASET


def test_an_unreadable_table_falls_through_to_the_default(monkeypatch, caplog):
    def _boom(path=None):
        raise FileNotFoundError("grid_zones.json")

    monkeypatch.setattr(grid_zones, "load_zone_table", _boom)
    fs = build_factor_set(provider="anthropic", settings=Settings(), workspace_settings=PIN_DE)
    assert fs.grid.layer == LAYER_GLOBAL_DEFAULT
    assert "grid zone table unavailable" in caplog.text


def test_the_shipped_table_is_inert_until_imported():
    # The bundled grid_zones.json ships with zero zones until a maintainer
    # runs the importer; a pinned region must then resolve exactly as before.
    if grid_zones.load_zone_table().zones:
        pytest.skip("bundled zone table has been populated")
    fs = build_factor_set(provider="anthropic", workspace_settings=PIN_DE)
    assert fs.grid.layer != LAYER_DATASET


def test_the_run_record_cites_the_dataset_not_the_operator(de_table):
    from tret.providers.catalog import ModelCatalog, ModelInfo
    from tret.services.emissions import energy_accounting

    model = ModelInfo(
        id="anthropic/test", provider="anthropic", wire_id="test-1", display_name="Test",
        context_window=200_000, input_price_per_mtok=Decimal("3"),
        output_price_per_mtok=Decimal("15"), cost_tier="standard", energy_class="L",
    )
    fs = build_factor_set(provider="anthropic", workspace_settings=PIN_DE)
    report = energy_accounting(model, 1_000_000, 0, 0, 0, catalog=ModelCatalog(), factors=fs)
    assert report["grid_region"] == "eu-central-1"
    record = next(f for f in report["factors"] if f["key"] == "grid_intensity")
    assert record["source"].startswith("published — Electricity Maps DE 2025")
    assert record["url"] == "https://app.electricitymaps.com/zone/DE/all/yearly"
    assert record["date"] == "2025-12-31"
    assert record["layer"] == LAYER_DATASET
    assert "pinned the provider to region eu-central-1" in record["note"]
