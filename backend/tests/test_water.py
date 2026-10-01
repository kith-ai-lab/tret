import json
import math

import pytest

from tret.services import water
from tret.services.water import combine_water, compute_water, default_water_factors


def cloud(**kw):
    return default_water_factors("cloud", **kw)


def run(it_wh=10.0, pue=1.18, boundary="node_it", factors=None, **kw):
    return compute_water(it_wh, it_wh * pue, boundary=boundary, pue=pue, factors=factors or cloud(), **kw)


def test_worked_example():
    w = run()
    assert w["water_basis"] == "consumption"
    assert w["onsite_ml"] == pytest.approx(3.75)
    assert w["offsite_ml"] == pytest.approx(56.758)
    assert w["water_ml"] == pytest.approx(60.508)
    assert w["water_ml_low"] == pytest.approx(60.508 / 3, abs=1e-5)
    assert w["water_ml_high"] == pytest.approx(60.508 * 3)
    assert w["embodied_ml"] is None
    assert w["schema_version"] == 1
    assert any("hydro" in c.lower() for c in w["caveats"])


def test_google_anchor():
    w = compute_water(0.24 / 1.09, 0.24, boundary="node_it", pue=1.09, factors=cloud())
    assert w["onsite_ml"] == pytest.approx(0.0826, abs=1e-3)
    assert w["offsite_ml"] == pytest.approx(1.154, abs=1e-3)


def test_local_onsite_zero():
    f = default_water_factors("local")
    w = compute_water(10, 10, boundary="node_it", pue=1.0, factors=f)
    assert w["onsite_ml"] == 0
    assert w["offsite_ml"] == pytest.approx(48.1)


def test_facility_boundary_derives_it():
    w = compute_water(None if False else 99, 11.8, boundary="facility", pue=1.18, factors=cloud())
    assert w["onsite_ml"] == pytest.approx(3.75)
    assert "IT energy derived from configured PUE" in w["caveats"]


def test_partial_boundary_caveat():
    w = run(boundary="gpu")
    assert any("understated" in c for c in w["caveats"])


def test_none_energy():
    assert compute_water(None, None, boundary="node_it", pue=1.1, factors=cloud()) is None


def test_country_dataset():
    f = cloud(country_iso3="usa")
    assert f.grid_water_l_per_kwh == 3.14
    rec = next(r for r in f.records if r["key"] == "grid_water_l_per_kwh")
    assert rec["layer"] == "dataset" and rec["source"] == "dataset:wri2020:USA"


def test_unknown_iso_fallback():
    f = cloud(country_iso3="ZZZ")
    assert f.grid_water_l_per_kwh == 4.81
    assert any("ZZZ" in c for c in f.caveats)
    assert any("ZZZ" in c for c in run(factors=f)["caveats"])


def test_override_wins_and_drops_hydro():
    f = cloud(country_iso3="USA", overrides={"grid_water_l_per_kwh": 2.0, "site_wue_l_per_kwh": 1.0})
    recs = {r["key"]: r for r in f.records}
    assert recs["grid_water_l_per_kwh"]["layer"] == "override"
    assert recs["site_wue_l_per_kwh"]["layer"] == "override"
    assert not any("hydro" in c.lower() for c in run(factors=f)["caveats"])


@pytest.mark.parametrize("bad", [
    {"site_wue_l_per_kwh": -1},
    {"grid_water_l_per_kwh": float("nan")},
    {"grid_water_l_per_kwh": math.inf},
    {"site_wue_l_per_kwh": "1"},
    {"band_low": 0},
    {"band_low": 1.5},
    {"band_high": 0.5},
    {"embodied_water_ml_per_run": -1},
    {"nope": 1},
])
def test_override_validation(bad):
    with pytest.raises(ValueError):
        cloud(overrides=bad)


def test_embodied_counted():
    w = run(factors=cloud(overrides={"embodied_water_ml_per_run": 5}))
    assert w["embodied_ml"] == 5
    assert w["water_ml"] == pytest.approx(65.508)


def test_avoided_signed():
    heavy = run(it_wh=10, baseline_energy_wh=5, baseline_energy_wh_total=5 * 1.18)
    assert heavy["avoided_water_ml"] < 0
    light = run(it_wh=5, baseline_energy_wh=10, baseline_energy_wh_total=11.8)
    assert light["avoided_water_ml"] == pytest.approx(30.254)
    assert light["baseline_water_ml"] == pytest.approx(60.508)
    assert run()["avoided_water_ml"] is None


def test_combine():
    a, b = run(), run(it_wh=5)
    c = combine_water([a, None, b])
    assert c["water_ml"] == pytest.approx(a["water_ml"] + b["water_ml"])
    assert c["runs_without_water"] == 1 and c["runs_counted"] == 2
    assert c["embodied_ml"] is None
    assert combine_water([None]) is None
    assert combine_water([]) is None


def test_combine_basis_mix():
    a = run()
    b = dict(run(), water_basis="withdrawal")
    c = combine_water([a, b])
    assert c["water_ml"] is None and c["water_basis"] is None
    assert any("differ" in x for x in c["caveats"])


def test_data_file_provenance():
    data = json.loads(water._DATA.read_text())
    recs = [data["site_wue"]["cloud"], data["grid_water"], data["band"]]
    for r in recs:
        assert r["confidence"] and r["water_basis"] == "consumption"
    assert data["site_wue"]["cloud"]["url"] and data["grid_water"]["url"]
    assert data["site_wue"]["local"]["value"] == 0
    assert data["band"]["is_confidence_interval"] is False
    for r in run()["factors"]:
        assert r["confidence"] and r["water_basis"] == "consumption" and "url" in r
    for iso, row in data["grid_water"]["countries"].items():
        assert row["l_per_kwh"] == pytest.approx(row["gal_per_kwh"] * 3.785, abs=0.01), iso


def test_facility_metered_run_baseline_is_node_it():
    # A facility meter on the run must not change how the (estimated) baseline is split.
    f = default_water_factors("cloud")
    metered = compute_water(11.8, 11.8, boundary="facility", pue=1.18, factors=f,
                            baseline_energy_wh=10, baseline_energy_wh_total=11.8)
    estimated = compute_water(10, 11.8, boundary="node_it", pue=1.18, factors=f,
                              baseline_energy_wh=10, baseline_energy_wh_total=11.8)
    assert metered["baseline_water_ml"] == estimated["baseline_water_ml"] == pytest.approx(60.508)


def test_combine_partial_baseline_is_none():
    f = default_water_factors("cloud")
    with_b = compute_water(10, 11.8, boundary="node_it", pue=1.18, factors=f,
                           baseline_energy_wh=20, baseline_energy_wh_total=23.6)
    without_b = compute_water(10, 11.8, boundary="node_it", pue=1.18, factors=f)
    out = combine_water([with_b, without_b])
    assert out["water_ml"] == pytest.approx(2 * 60.508)
    assert out["baseline_water_ml"] is None and out["avoided_water_ml"] is None
    assert any("no baseline" in c for c in out["caveats"])
    both = combine_water([with_b, with_b])
    assert both["avoided_water_ml"] == pytest.approx(2 * 60.508)
