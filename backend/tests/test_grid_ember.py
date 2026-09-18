import json
from pathlib import Path

import pytest

from tret.config import GridFactor, Settings
from tret.providers.catalog import ModelCatalog
from tret.services.emission_factors import build_factor_set
from tret.services.emission_settings import shipped_defaults
from tret.services.emissions import energy_accounting
from tret.services.grid_ember import (
    DATA_PATH,
    SOURCE_SHA256,
    EmberDataError,
    ember_attribution,
    entry_for_region,
    import_ember_csv,
    load_ember_data,
)


SOURCE = Path("/tmp/tret-emissions-research/ember-yearly.csv")


def test_bundled_data_has_pinned_world_and_explicit_country_namespace():
    data = load_ember_data()
    assert data["source_sha256"] == SOURCE_SHA256
    assert data["world"]["g_per_kwh"] == 458.49
    assert data["creator"] == "Ember"
    assert data["license_url"] == "https://creativecommons.org/licenses/by/4.0/"
    assert "Ember" in data["attribution"] and "CC BY 4.0" in data["attribution"]
    iso3, entry = entry_for_region("country-USA", data)
    assert iso3 == "USA"
    assert entry["observation_year"] == 2025
    assert entry["factor_boundary"] == "lifecycle_electricity_generation"
    assert entry["gas_coverage"] == "co2e"
    assert entry_for_region("USA", data) is None
    assert entry_for_region("country-ZZZ", data) is None


@pytest.mark.skipif(not SOURCE.exists(), reason="pinned source CSV not available")
def test_importer_reproduces_committed_asset_exactly():
    assert import_ember_csv(SOURCE) == json.loads(DATA_PATH.read_text())


def test_ember_attribution_matches_bundled_asset():
    data = load_ember_data()
    creator, attribution, license_url = ember_attribution()
    assert creator == data["creator"]
    assert attribution == data["attribution"]
    assert license_url == data["license_url"]


def test_default_metadata_is_ember_but_custom_factor_metadata_stays_unknown():
    default = build_factor_set(provider="anthropic", settings=Settings())
    assert float(default.grid.value) == pytest.approx(458.49)
    assert default.grid.gas_coverage == "co2e"
    assert default.grid.observation_year == 2025

    custom = build_factor_set(
        provider="anthropic",
        settings=Settings(grid_factors={"anthropic": GridFactor(g_per_kwh=123)}),
    )
    assert custom.grid.value == 123
    assert custom.grid.factor_boundary == "unknown"
    assert custom.grid.gas_coverage == "unknown"
    assert custom.grid.observation_year is None

    equal_to_default = build_factor_set(
        provider="anthropic",
        settings=Settings(
            grid_factors={"anthropic": GridFactor(g_per_kwh=458.49, label="custom")}
        ),
    )
    assert equal_to_default.grid.layer != "global_default"
    assert equal_to_default.grid.factor_boundary == "unknown"
    assert equal_to_default.grid.dataset_version is None


def test_country_pin_resolves_dataset_and_unknown_pin_falls_back_visibly():
    country = build_factor_set(
        provider="anthropic",
        workspace_settings={"grid": {"regions": {"anthropic": "country-USA"}}},
    )
    assert country.grid.layer == "dataset"
    assert country.grid.source == "dataset:ember:country-USA"
    assert country.grid.region == "country-usa"

    unknown = build_factor_set(
        provider="anthropic",
        workspace_settings={"grid": {"regions": {"anthropic": "country-ZZZ"}}},
    )
    assert unknown.grid.layer == "global_default"
    assert unknown.grid.region is None
    assert unknown.grid.requested_region == "country-zzz"
    assert unknown.grid.region_resolution_status == "fallback_unknown_region"
    assert float(unknown.grid.value) == pytest.approx(458.49)

    report = energy_accounting(
        ModelCatalog().get("anthropic/claude-sonnet-5"), 10, 2, factors=unknown
    )
    assert report["grid_region"] is None
    assert report["grid_requested_region"] == "country-zzz"
    assert report["grid_region_resolution_status"] == "fallback_unknown_region"


def test_shipped_defaults_expose_boundary_and_gas_metadata():
    grid = shipped_defaults()["grid_default"]
    assert grid["value"] == 458.49
    assert grid["factor_boundary"] == "lifecycle_electricity_generation"
    assert grid["gas_coverage"] == "co2e"
    assert grid["includes_td_losses"] is None


def test_importer_rejects_unpinned_content(tmp_path):
    source = tmp_path / "different.csv"
    source.write_text("Area,ISO 3 code\n")
    with pytest.raises(EmberDataError, match="SHA256"):
        import_ember_csv(source)
