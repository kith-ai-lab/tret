"""Water accounting wired into the emissions pipeline.

`tret/services/water.py` is the pure calculator (tests/test_water.py). This file
covers the wiring around it: the factor ladder (`WaterBlock`, `_resolve_water`),
`energy_accounting()["water"]`, the roll-ups (`combine_accountings`,
`overhead_block`, the analytics endpoints), the settings API and the served
methodology page. Method: docs/water-methodology.md.
"""
# ruff: noqa: F401, F811  (fixtures imported from sibling suites)
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
import pytest_asyncio
from pydantic import ValidationError

# Fixtures and helpers reused from the what-if / settings API suites: a real
# sqlite database behind an ASGI client, with a fixed catalog and fixed Settings.
from tests.test_emissions_settings_api import (  # noqa: F401
    login as settings_login,
    make_member as settings_member,
    make_user as settings_user,
    make_workspace as settings_workspace,
)
from tests.test_emissions_whatif import (  # noqa: F401
    FAKE_CATALOG,
    FIXED_SETTINGS,
    MODEL,
    MODEL_ID,
    _account,
    _reset_extension_registry,
    client,
    engine,
    login,
    make_model,
    make_run,
    make_tenant,
    seed,
    session_factory,
)
from tret.api import emissions_settings as emissions_settings_api
from tret.config import Settings
from tret.providers.base import Usage
from tret.services import emissions as emissions_module
from tret.services.emission_factors import EmissionsOverrides, build_factor_set, factor_set_for_model
from tret.services.emissions import (
    combine_accountings,
    emission_event_fields,
    emission_summary_fields,
    energy_accounting,
    overhead_block,
    overhead_call,
)
from tret.services.water import combine_water, compute_water, default_water_factors

BIG = make_model("anthropic/big-test", energy_class="XL")
SMALL = make_model("anthropic/small-test", energy_class="S")
LOCAL = make_model("local/qwen-test", provider="local", energy_class="S")
CATALOG = type(FAKE_CATALOG)({m.id: m for m in (MODEL, BIG, SMALL, LOCAL)})
TOKENS = (1_000_000, 200_000, 0, 0)


def _settings(**over) -> Settings:
    over.setdefault("emissions_baseline_model", BIG.id)
    return Settings(**over)


def _account_with(model, settings=None, **factor_kwargs):
    settings = settings or _settings()
    factors = build_factor_set(
        provider=model.provider, settings=settings, model_id=model.id, **factor_kwargs
    )
    return energy_accounting(model, *TOKENS, settings=settings, catalog=CATALOG, factors=factors)


def _record(block: dict, key: str) -> dict:
    return next(r for r in block["water"]["factors"] if r["key"] == key)


# ── energy_accounting ────────────────────────────────────────────────────────
def test_cloud_run_water_block_matches_compute_water():
    a = _account_with(MODEL)
    expected = compute_water(
        a["energy_wh"], a["energy_wh_total"], boundary=a["energy_boundary"], pue=a["configured_pue"],
        factors=default_water_factors("cloud"),
        baseline_energy_wh=a["baseline"]["energy_wh"],
        baseline_energy_wh_total=a["baseline"]["energy_wh_total"],
        baseline_factors=default_water_factors("cloud"),
    )
    assert a["water"] == expected
    assert a["water"]["onsite_ml"] > 0
    assert a["water"]["water_ml"] == pytest.approx(
        a["water"]["onsite_ml"] + a["water"]["offsite_ml"], abs=1e-5
    )
    assert a["water"]["water_basis"] == "consumption"
    # Plain JSON: floats and strings, no Decimals.
    import json

    json.dumps(a["water"])


def test_local_run_has_zero_onsite_water_but_still_offsite():
    a = _account_with(LOCAL)
    assert a["water"]["onsite_ml"] == 0.0
    assert a["water"]["offsite_ml"] == pytest.approx(a["energy_wh_total"] * 4.81, rel=1e-4)
    assert _record(a, "site_wue_l_per_kwh")["layer"] == "global_default"
    assert _record(a, "site_wue_l_per_kwh")["value"] == 0


def test_measured_facility_boundary_derives_it_energy_from_configured_pue():
    settings = _settings()
    factors = build_factor_set(provider="anthropic", settings=settings, model_id=MODEL.id)
    a = energy_accounting(
        MODEL, *TOKENS, settings=settings, catalog=CATALOG, factors=factors,
        measured_energy_wh=10.0, measured_energy_boundary="facility",
    )
    assert a["pue_applied"] is False and a["energy_wh_total"] == 10.0
    pue = float(factors.pue.value)
    assert a["water"]["onsite_ml"] == pytest.approx(10.0 / pue * 0.375, rel=1e-4)
    assert a["water"]["offsite_ml"] == pytest.approx(10.0 * 4.81, rel=1e-4)
    assert any("derived from configured PUE" in c for c in a["water"]["caveats"])


def test_baseline_water_is_present_and_signed():
    saved = _account_with(MODEL, settings=_settings(emissions_baseline_model=BIG.id))
    assert saved["water"]["baseline_water_ml"] > saved["water"]["water_ml"]
    assert saved["water"]["avoided_water_ml"] == pytest.approx(
        saved["water"]["baseline_water_ml"] - saved["water"]["water_ml"], abs=1e-5
    )
    assert saved["water"]["avoided_water_ml"] > 0

    # A run heavier than its baseline avoided nothing: negative, never clamped.
    heavier = _account_with(BIG, settings=_settings(emissions_baseline_model=SMALL.id))
    assert heavier["water"]["avoided_water_ml"] < 0

    # The run is its own baseline: avoided is 0 by construction, as for carbon.
    same = _account_with(MODEL, settings=_settings(emissions_baseline_model=MODEL.id))
    assert same["water"]["baseline_water_ml"] == same["water"]["water_ml"]
    assert same["water"]["avoided_water_ml"] == 0.0


def test_baseline_resolves_water_through_factor_set_for_model():
    """The counterfactual's water goes through the same ladder, via factor_set_for_model."""
    settings = _settings(emissions_baseline_model=BIG.id)
    factors = build_factor_set(
        provider="anthropic", settings=settings, model_id=MODEL.id,
        workspace_settings={"water": {"grid_water_l_per_kwh": 10.0}},
    )
    cf = factor_set_for_model(factors, provider="anthropic", model_id=BIG.id)
    assert cf.water.grid_water_l_per_kwh == 10.0
    a = energy_accounting(MODEL, *TOKENS, settings=settings, catalog=CATALOG, factors=factors)
    bwh_total = a["baseline"]["energy_wh_total"]
    assert a["water"]["baseline_water_ml"] == pytest.approx(
        a["baseline"]["energy_wh"] * 0.375 + bwh_total * 10.0, rel=1e-4
    )


def test_workspace_override_is_recorded_with_layer_workspace():
    a = _account_with(
        MODEL, workspace_settings={"water": {"site_wue_l_per_kwh": 1.15, "band_low": 0.5, "band_high": 2}}
    )
    wue = _record(a, "site_wue_l_per_kwh")
    assert (wue["layer"], wue["source"], wue["value"]) == ("workspace", "workspace", 1.15)
    assert wue["setting"] == "workspace.emissions.water.site_wue_l_per_kwh"
    band = _record(a, "water_band")
    assert band["layer"] == "workspace" and band["value"] == {"low": 0.5, "high": 2.0}
    assert a["water"]["onsite_ml"] == pytest.approx(a["energy_wh"] * 1.15, rel=1e-4)
    assert a["water"]["water_ml_low"] == pytest.approx(a["water"]["water_ml"] * 0.5, rel=1e-4)
    assert "workspace" in a["factor_layers"]
    # Grid water was not overridden: it still falls through to the shipped default.
    assert _record(a, "grid_water_l_per_kwh")["layer"] == "global_default"


def test_managed_layer_source_carries_its_name():
    a = _account_with(
        MODEL, managed_settings={"source_name": "hosted", "water": {"grid_water_l_per_kwh": 3.0}}
    )
    grid = _record(a, "grid_water_l_per_kwh")
    assert (grid["layer"], grid["source"], grid["value"]) == ("managed", "managed:hosted", 3.0)


def test_env_layer_is_recorded_as_env():
    a = _account_with(MODEL, settings=_settings(water_grid_l_per_kwh=2.5, water_site_wue_l_per_kwh=0.9))
    grid = _record(a, "grid_water_l_per_kwh")
    assert (grid["layer"], grid["source"], grid["value"]) == ("env", "env", 2.5)
    assert grid["setting"] == "TRET_WATER_GRID_L_PER_KWH"
    assert _record(a, "site_wue_l_per_kwh")["layer"] == "env"
    assert a["water"]["offsite_ml"] == pytest.approx(a["energy_wh_total"] * 2.5, rel=1e-4)


def test_ladder_order_workspace_beats_env_and_run_override_beats_workspace():
    settings = _settings(water_grid_l_per_kwh=2.5)
    ws = {"water": {"grid_water_l_per_kwh": 3.0}}
    assert build_factor_set(provider="anthropic", settings=settings, workspace_settings=ws
                            ).water.grid_water_l_per_kwh == 3.0
    ro = build_factor_set(provider="anthropic", settings=settings, workspace_settings=ws,
                          run_overrides={"water_grid_l_per_kwh": 7.0})
    assert ro.water.grid_water_l_per_kwh == 7.0
    rec = next(r for r in ro.water.records if r["key"] == "grid_water_l_per_kwh")
    assert (rec["layer"], rec["source"]) == ("run_override", "run_override")


def test_site_wue_keys_are_per_deployment_in_documents():
    doc = {"water": {"local_site_wue_l_per_kwh": 0.6}}
    local = build_factor_set(provider="local", settings=_settings(), workspace_settings=doc)
    cloud = build_factor_set(provider="anthropic", settings=_settings(), workspace_settings=doc)
    assert local.water.site_wue_l_per_kwh == 0.6
    assert cloud.water.site_wue_l_per_kwh == 0.375
    # The general key is the CLOUD value only; it never leaks onto a local run.
    both = {"water": {"site_wue_l_per_kwh": 0.8}}
    assert build_factor_set(provider="local", settings=_settings(), workspace_settings=both
                            ).water.site_wue_l_per_kwh == 0.0
    assert build_factor_set(provider="anthropic", settings=_settings(), workspace_settings=both
                            ).water.site_wue_l_per_kwh == 0.8


def test_env_site_wue_does_not_leak_into_local_runs():
    env = _settings(water_site_wue_l_per_kwh=0.9)
    assert build_factor_set(provider="local", settings=env).water.site_wue_l_per_kwh == 0.0
    assert build_factor_set(provider="anthropic", settings=env).water.site_wue_l_per_kwh == 0.9
    local_env = _settings(water_local_site_wue_l_per_kwh=0.4)
    local = build_factor_set(provider="local", settings=local_env)
    assert local.water.site_wue_l_per_kwh == 0.4
    rec = next(r for r in local.water.records if r["key"] == "site_wue_l_per_kwh")
    assert (rec["layer"], rec["setting"]) == ("env", "TRET_WATER_LOCAL_SITE_WUE_L_PER_KWH")
    assert build_factor_set(provider="anthropic", settings=local_env).water.site_wue_l_per_kwh == 0.375


def test_one_invalid_water_value_does_not_discard_the_valid_ones(caplog):
    settings = _settings(water_band_low=2.5, water_grid_l_per_kwh=2.0)
    factors = build_factor_set(
        provider="anthropic", settings=settings,
        workspace_settings={"water": {"site_wue_l_per_kwh": 1.15}},
    )
    w = factors.water
    assert (w.site_wue_l_per_kwh, w.grid_water_l_per_kwh) == (1.15, 2.0)
    assert w.band_low == pytest.approx(1 / 3)  # invalid env value skipped -> next rung
    layers = {r["key"]: r["layer"] for r in w.records}
    assert layers["site_wue_l_per_kwh"] == "workspace" and layers["grid_water_l_per_kwh"] == "env"
    assert any("TRET_WATER_BAND_LOW" in c for c in w.caveats)
    # An invalid value at a more specific layer falls to the next valid rung.
    run = build_factor_set(
        provider="anthropic", settings=_settings(water_grid_l_per_kwh=2.0),
        run_overrides={"water_grid_l_per_kwh": -3},
    )
    assert run.water.grid_water_l_per_kwh == 2.0


def test_unexpected_water_failure_never_raises_out_of_build_factor_set(monkeypatch, caplog):
    from tret.services import emission_factors as ef

    def boom(*_a, **_k):
        raise RuntimeError("resolver exploded")

    monkeypatch.setattr(ef, "resolve_water_factors", boom)
    with caplog.at_level("WARNING"):
        factors = build_factor_set(provider="anthropic", settings=_settings())
    assert factors.water.grid_water_l_per_kwh == 4.81
    assert any(r.exc_info for r in caplog.records)
    monkeypatch.setattr(ef, "default_water_factors", boom)
    assert build_factor_set(provider="anthropic", settings=_settings()).water.grid_water_l_per_kwh == 4.81


def test_factor_set_is_hashable():
    assert isinstance(hash(build_factor_set(provider="anthropic", settings=_settings())), int)


def test_country_from_a_grid_country_pin_selects_the_wri_dataset_factor():
    a = _account_with(
        MODEL, workspace_settings={"grid": {"regions": {"anthropic": "country-DEU"}}}
    )
    assert a["grid_co2e_source"] == "dataset:ember:country-DEU"
    grid = _record(a, "grid_water_l_per_kwh")
    assert (grid["layer"], grid["source"], grid["value"]) == ("dataset", "dataset:wri2020:DEU", 1.93)
    assert a["water"]["offsite_ml"] == pytest.approx(a["energy_wh_total"] * 1.93, rel=1e-4)
    # A country absent from tret's WRI table falls back to the world average, with a caveat.
    b = _account_with(MODEL, workspace_settings={"water": {"country": "mlt"}})
    assert _record(b, "grid_water_l_per_kwh")["layer"] == "global_default"
    assert any("MLT" in c for c in b["water"]["caveats"])


def test_explicit_water_country_wins_over_the_grid_pin():
    a = _account_with(
        MODEL,
        workspace_settings={
            "grid": {"regions": {"anthropic": "country-DEU"}},
            "water": {"country": "jpn"},
        },
    )
    grid = _record(a, "grid_water_l_per_kwh")
    assert (grid["source"], grid["value"]) == ("dataset:wri2020:JPN", 2.31)
    # An explicit value beats any country (ladder: workspace > dataset).
    c = _account_with(
        MODEL, workspace_settings={"water": {"country": "jpn", "grid_water_l_per_kwh": 5.0}}
    )
    assert _record(c, "grid_water_l_per_kwh")["layer"] == "workspace"


def test_bad_env_water_setting_falls_back_to_defaults_instead_of_failing():
    factors = build_factor_set(provider="anthropic", settings=_settings(water_band_low=5.0))
    assert factors.water.band_low == pytest.approx(1 / 3)
    a = energy_accounting(MODEL, *TOKENS, settings=_settings(water_band_low=5.0), catalog=CATALOG)
    assert a["water"] is not None


# ── fail-open ────────────────────────────────────────────────────────────────
def test_a_water_failure_yields_none_and_leaves_the_rest_of_the_accounting_intact(monkeypatch):
    good = _account_with(MODEL)

    def boom(*_a, **_k):
        raise RuntimeError("water exploded")

    monkeypatch.setattr(emissions_module, "compute_water", boom)
    bad = _account_with(MODEL)
    assert bad["water"] is None
    assert {k: v for k, v in bad.items() if k != "water"} == {k: v for k, v in good.items() if k != "water"}
    assert emission_summary_fields(bad)["water_ml"] is None


# ── roll-ups ─────────────────────────────────────────────────────────────────
def test_combine_across_segments_sums_water():
    a = _account_with(MODEL)
    b = _account_with(SMALL)
    combined = combine_accountings([a, b])
    water = combined["water"]
    assert water["water_ml"] == pytest.approx(a["water"]["water_ml"] + b["water"]["water_ml"], abs=1e-5)
    assert water["onsite_ml"] == pytest.approx(a["water"]["onsite_ml"] + b["water"]["onsite_ml"], abs=1e-5)
    assert water["runs_without_water"] == 0 and water["runs_counted"] == 2
    assert {f["key"] for f in water["factors"]} == {
        "site_wue_l_per_kwh", "grid_water_l_per_kwh", "water_band",
    }
    # One block is returned unchanged.
    single = combine_accountings([a])["water"]
    assert (single["runs_counted"], single["runs_without_water"]) == (1, 0)
    assert single["water_ml"] == a["water"]["water_ml"]


def test_combine_counts_a_segment_without_water_instead_of_zeroing_it():
    a = _account_with(MODEL)
    legacy = {k: v for k, v in _account_with(SMALL).items() if k != "water"}
    water = combine_accountings([a, legacy])["water"]
    assert water["runs_without_water"] == 1
    assert water["water_ml"] == a["water"]["water_ml"]
    assert combine_accountings([legacy, dict(legacy)])["water"] is None


def test_overhead_calls_roll_water_up_like_carbon():
    calls = [
        overhead_call("routing", SMALL, Usage(input_tokens=2_000, output_tokens=100)),
        overhead_call("compaction_summary", MODEL, Usage(input_tokens=50_000, output_tokens=2_000)),
    ]
    block = overhead_block(calls)
    water = block["accounting"]["water"]
    assert water["water_ml"] == pytest.approx(
        sum(c["energy_accounting"]["water"]["water_ml"] for c in calls), abs=1e-5
    )
    assert block["accounting"]["co2e_g"] == pytest.approx(
        sum(c["energy_accounting"]["co2e_g"] for c in calls), abs=1e-5
    )
    # Run total = segments + overhead, the same way carbon is combined.
    run = _account_with(MODEL)
    total = combine_accountings([run, block["accounting"]])["water"]
    assert total["water_ml"] == pytest.approx(run["water"]["water_ml"] + water["water_ml"], abs=1e-5)


def test_summary_and_event_fields_carry_water_ml():
    a = _account_with(MODEL)
    assert emission_summary_fields(a)["water_ml"] == a["water"]["water_ml"]
    assert emission_event_fields(a)["water_ml"] == a["water"]["water_ml"]
    assert emission_summary_fields({"co2e_g": 1.0})["water_ml"] is None


def test_combine_water_still_refuses_to_sum_across_bases():
    a = _account_with(MODEL)["water"]
    odd = {**a, "water_basis": "withdrawal"}
    assert combine_water([a, odd])["water_ml"] is None


# ── analytics ────────────────────────────────────────────────────────────────
async def _seed_runs(seed, client, email, *, with_water: bool):
    _, user, project, harness = await make_tenant(seed, name=email.split("@")[0], email=email)
    runs = []
    for _ in range(2):
        accounting = _account(MODEL, FIXED_SETTINGS, TOKENS)
        if not with_water:
            accounting.pop("water")
        runs.append(make_run(
            project_id=project.id, harness_id=harness.id, created_by=user.id,
            model_used=MODEL_ID, accounting=accounting,
            input_tokens=TOKENS[0], output_tokens=TOKENS[1],
        ))
    await seed(*runs)
    await login(client, user.email)
    return runs


async def test_analytics_totals_and_breakdowns_include_water(client, seed):
    runs = await _seed_runs(seed, client, "analytics-water@example.com", with_water=True)
    per_run = runs[0].energy_accounting["water"]
    body = (await client.get("/api/analytics/emissions")).json()
    totals = body["totals"]
    assert totals["water_ml"] == pytest.approx(2 * per_run["water_ml"], abs=1e-4)
    assert totals["water_onsite_ml"] == pytest.approx(2 * per_run["onsite_ml"], abs=1e-4)
    assert totals["water_offsite_ml"] == pytest.approx(2 * per_run["offsite_ml"], abs=1e-4)
    assert totals["runs_without_water"] == 0
    for row in (*body["by_model"], *body["by_harness"], *body["by_day"], *body["by_basis"]):
        assert row["water_ml"] == pytest.approx(2 * per_run["water_ml"], abs=1e-4)
        assert row["runs_without_water"] == 0


async def test_old_runs_without_a_water_block_are_counted_not_zeroed(client, seed):
    await _seed_runs(seed, client, "analytics-legacy@example.com", with_water=False)
    body = (await client.get("/api/analytics/emissions")).json()
    assert body["totals"]["runs_without_water"] == 2
    assert body["totals"]["water_ml"] is None
    assert body["totals"]["water_onsite_ml"] is None
    assert body["by_model"][0]["runs_without_water"] == 2
    assert body["by_model"][0]["water_ml"] is None
    # Carbon is unaffected.
    assert body["totals"]["co2e_g"] > 0


async def test_whatif_restates_water_for_pre_water_stored_runs(client, seed):
    await _seed_runs(seed, client, "whatif-water@example.com", with_water=False)
    body = (await client.post(
        "/api/analytics/emissions/whatif",
        json={"factors": {"water": {"grid_water_l_per_kwh": 10.0, "site_wue_l_per_kwh": 1.0}}},
    )).json()
    assert body["recorded"]["totals"]["runs_without_water"] == 2
    assert body["recorded"]["totals"]["water_ml"] is None
    scenario = body["scenario"]["totals"]
    assert scenario["runs_without_water"] == 0
    probe = _account_with(MODEL, settings=FIXED_SETTINGS, workspace_settings={
        "water": {"grid_water_l_per_kwh": 10.0, "site_wue_l_per_kwh": 1.0}})
    assert scenario["water_ml"] == pytest.approx(2 * probe["water"]["water_ml"], abs=1e-4)
    # No delta against a recorded side that never had water.
    assert body["delta"]["water_ml"] is None


async def test_whatif_water_delta_when_both_sides_have_water(client, seed):
    await _seed_runs(seed, client, "whatif-delta@example.com", with_water=True)
    body = (await client.post(
        "/api/analytics/emissions/whatif", json={"factors": {"water": {"grid_water_l_per_kwh": 0.0}}},
    )).json()
    assert body["delta"]["water_ml"] < 0
    assert body["scenario"]["totals"]["water_offsite_ml"] == 0.0


async def test_whatif_rejects_a_bad_water_block(client, seed):
    await _seed_runs(seed, client, "whatif-bad@example.com", with_water=True)
    response = await client.post(
        "/api/analytics/emissions/whatif", json={"factors": {"water": {"band_low": 2}}}
    )
    assert response.status_code == 422
    assert "band_low" in response.json()["detail"]


# ── schema validation ────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "block",
    [
        {"band_low": 2},
        {"band_low": 0},
        {"band_high": 0.5},
        {"site_wue_l_per_kwh": -1},
        {"grid_water_l_per_kwh": float("inf")},
        {"country": "US"},
        {"country": "U5A"},
        {"unknown_key": 1},
    ],
)
def test_water_block_rejects_bad_values(block):
    with pytest.raises(ValidationError):
        EmissionsOverrides(water=block)


def test_water_block_normalizes_country_and_accepts_a_full_document():
    doc = EmissionsOverrides(water={
        "site_wue_l_per_kwh": 0.4, "local_site_wue_l_per_kwh": 0, "grid_water_l_per_kwh": 3.1,
        "country": " usa ", "band_low": 0.5, "band_high": 4,
    })
    assert doc.water.country == "USA"


# ── per-upstream site WUE (water.upstreams) ──────────────────────────────────
GOOGLE_WUE = {
    "site_wue_l_per_kwh": 1.15, "label": "Google 2025 environmental report",
    "url": "https://sustainability.google/reports/", "as_of": "2025-06-30",
    "water_basis": "consumption", "denominator": "it_energy",
}
UP_MANAGED = {"source_name": "kith", "water": {"upstreams": {"google": GOOGLE_WUE}}}


def _site_wue(factors):
    return next(r for r in factors.water.records if r["key"] == "site_wue_l_per_kwh")


@pytest.mark.parametrize(
    "patch",
    [
        {"water_basis": "withdrawal"},
        {"water_basis": None},
        {"site_wue_l_per_kwh": -0.1},
        {"site_wue_l_per_kwh": float("nan")},
        {"site_wue_l_per_kwh": float("inf")},
        {"extra_key": 1},
        {"as_of": "last year"},
        {"label": " "},
        {"denominator": "facility_energy"},
        {"evidence_type": "measured"},
    ],
)
def test_water_disclosure_rejects_bad_values(patch):
    from tret.services.emission_factors import WaterDisclosure

    with pytest.raises(ValidationError):
        WaterDisclosure(**{**GOOGLE_WUE, **patch})


@pytest.mark.parametrize("missing", ["water_basis", "denominator"])
def test_water_disclosure_requires_basis_and_denominator(missing):
    from tret.services.emission_factors import WaterDisclosure

    data = {k: v for k, v in GOOGLE_WUE.items() if k != missing}
    with pytest.raises(ValidationError, match=missing):
        WaterDisclosure(**data)
    with pytest.raises(ValidationError):
        EmissionsOverrides(water={"upstreams": {"google": data}})


@pytest.mark.parametrize("key", ["gcp", "anthropic", "google-vertex", "Google"])
def test_water_upstreams_rejects_unknown_keys(key):
    with pytest.raises(ValidationError, match=r"\['aws', 'google'\]"):
        EmissionsOverrides(water={"upstreams": {key: GOOGLE_WUE}})
    # PUE's own upstream keys are deliberately not validated.
    EmissionsOverrides(pue={"upstreams": {"other": {
        "value": 1.1, "label": "x", "url": "u", "as_of": "2025-01-01"}}})
    assert set(EmissionsOverrides(
        water={"upstreams": {"aws": GOOGLE_WUE, "google": GOOGLE_WUE}}).water.upstreams
    ) == {"aws", "google"}


def test_water_disclosure_withdrawal_message_is_clear():
    from tret.services.emission_factors import WaterDisclosure

    with pytest.raises(ValidationError, match="withdrawal-basis"):
        WaterDisclosure(**{**GOOGLE_WUE, "water_basis": "withdrawal"})
    assert WaterDisclosure(**GOOGLE_WUE).water_basis == "consumption"


@pytest.mark.parametrize("served_by", ["google-vertex/eu", "google-vertex", "google"])
def test_upstream_wue_applies_to_a_google_served_call(served_by):
    f = build_factor_set(provider="openrouter", managed_settings=UP_MANAGED, served_by=served_by)
    assert f.water.site_wue_l_per_kwh == 1.15
    rec = _site_wue(f)
    assert (rec["layer"], rec["source"]) == ("managed", "managed:kith")
    assert rec["setting"] == "managed.emissions.water.upstreams.google"
    assert rec["url"] == GOOGLE_WUE["url"] and rec["date"] == "2025-06-30"
    assert rec["label"] == GOOGLE_WUE["label"] and rec["water_basis"] == "consumption"
    assert rec["disclosure"]["site_wue_l_per_kwh"] == 1.15
    a = energy_accounting(
        MODEL, *TOKENS, settings=_settings(), catalog=CATALOG, factors=f
    )
    assert a["water"]["onsite_ml"] == pytest.approx(a["energy_wh"] * 1.15, rel=1e-4)


@pytest.mark.parametrize("served_by", ["anthropic", None, "amazon-bedrock", "google-vertexx"])
def test_upstream_wue_does_not_apply_to_other_calls(served_by):
    f = build_factor_set(provider="openrouter", managed_settings=UP_MANAGED, served_by=served_by)
    assert f.water.site_wue_l_per_kwh == 0.375
    assert _site_wue(f)["layer"] == "global_default"
    assert "disclosure" not in _site_wue(f)


def test_baseline_factor_set_ignores_upstreams():
    f = build_factor_set(provider="openrouter", managed_settings=UP_MANAGED, served_by="google-vertex")
    base = factor_set_for_model(f, provider="anthropic", model_id=BIG.id)
    assert base.water.site_wue_l_per_kwh == 0.375


def test_local_deployment_ignores_water_upstreams():
    f = build_factor_set(provider="local", managed_settings=UP_MANAGED, served_by="google")
    assert f.water.site_wue_l_per_kwh == 0.0


def test_same_layer_upstream_beats_same_layer_generic():
    doc = {"water": {"site_wue_l_per_kwh": 0.9, "upstreams": {"google": GOOGLE_WUE}}}
    f = build_factor_set(provider="openrouter", workspace_settings=doc, served_by="google-vertex")
    assert f.water.site_wue_l_per_kwh == 1.15
    assert _site_wue(f)["layer"] == "workspace"
    other = build_factor_set(provider="openrouter", workspace_settings=doc, served_by="anthropic")
    assert other.water.site_wue_l_per_kwh == 0.9


def test_more_specific_layer_generic_beats_less_specific_upstream():
    # Same rule as PUE: a workspace generic value beats a managed upstream.
    f = build_factor_set(
        provider="openrouter", managed_settings=UP_MANAGED,
        workspace_settings={"water": {"site_wue_l_per_kwh": 0.9}}, served_by="google-vertex",
    )
    assert f.water.site_wue_l_per_kwh == 0.9
    assert _site_wue(f)["layer"] == "workspace"
    # ...and a workspace upstream beats a managed generic value.
    g = build_factor_set(
        provider="openrouter",
        managed_settings={"water": {"site_wue_l_per_kwh": 0.9}},
        workspace_settings={"water": {"upstreams": {"google": GOOGLE_WUE}}},
        served_by="google-vertex",
    )
    assert g.water.site_wue_l_per_kwh == 1.15 and _site_wue(g)["layer"] == "workspace"


def test_run_override_and_env_precedence_around_upstreams():
    ro = build_factor_set(
        provider="openrouter", managed_settings=UP_MANAGED, served_by="google-vertex",
        run_overrides={"water_site_wue_l_per_kwh": 2.0},
    )
    assert ro.water.site_wue_l_per_kwh == 2.0
    env = build_factor_set(
        provider="openrouter", managed_settings=UP_MANAGED, served_by="google-vertex",
        settings=_settings(water_site_wue_l_per_kwh=0.9),
    )
    assert env.water.site_wue_l_per_kwh == 1.15


def test_factor_set_for_call_replays_water_upstream_per_call():
    from tret.services.emission_factors import factor_set_for_call

    seg = build_factor_set(provider="openrouter", managed_settings=UP_MANAGED)
    google = factor_set_for_call(
        seg, provider="openrouter", model_id=MODEL.id, served_by="google-vertex/eu"
    )
    direct = factor_set_for_call(seg, provider="openrouter", model_id=MODEL.id, served_by="anthropic")
    assert google.water.site_wue_l_per_kwh == 1.15
    assert direct.water.site_wue_l_per_kwh == 0.375
    assert seg.water.site_wue_l_per_kwh == 0.375


def test_per_call_accounting_uses_each_calls_upstream_wue():
    from tret.services.emission_calls import account_call_records

    seg = build_factor_set(provider="openrouter", managed_settings=UP_MANAGED, model_id=MODEL.id)

    def rec(i, served_by):
        return {"iteration": i, "input_tokens": 100, "output_tokens": 10, "cache_read_tokens": 0,
                "cache_write_tokens": 0, "reasoning_tokens": None, "reasoning_accounting": None,
                "served_by": served_by, "usage_status": "reported"}

    result = account_call_records(
        MODEL, [rec(1, "google-vertex/eu"), rec(2, "anthropic")],
        billed_usage={"input_tokens": 200, "output_tokens": 20,
                      "cache_read_tokens": 0, "cache_write_tokens": 0},
        factors=seg, settings=_settings(), catalog=CATALOG,
    )
    calls = result["call_accountings"]
    assert [c["site_wue_l_per_kwh"] for c in calls] == [1.15, 0.375]
    assert calls[0]["water_disclosure"]["url"] == GOOGLE_WUE["url"]
    assert calls[1]["water_disclosure"] is None
    wue = next(r for r in result["water"]["factors"] if r["key"] == "site_wue_l_per_kwh")
    assert wue["source"] == "mixed"
    assert {v["value"] for v in wue["variants"]} == {1.15, 0.375}


# ── what-if keeps recorded upstream water when the scenario names no water key ──
def _recorded_run(served_bys):
    from tret.services.emission_calls import account_call_records

    seg = build_factor_set(provider=MODEL.provider, managed_settings=UP_MANAGED, model_id=MODEL.id)
    records = [
        {"iteration": i, "input_tokens": 500_000, "output_tokens": 100_000, "cache_read_tokens": 0,
         "cache_write_tokens": 0, "reasoning_tokens": None, "reasoning_accounting": None,
         "served_by": sb, "usage_status": "reported"}
        for i, sb in enumerate(served_bys, 1)
    ]
    n = len(records)
    return account_call_records(
        MODEL, records,
        billed_usage={"input_tokens": 500_000 * n, "output_tokens": 100_000 * n,
                      "cache_read_tokens": 0, "cache_write_tokens": 0},
        factors=seg, settings=_settings(), catalog=CATALOG,
    )


def _whatif(recorded, factors_doc):
    from tret.api.analytics import _whatif_accounting

    return _whatif_accounting(
        model_used=MODEL.id, input_tokens=500_000 * len(recorded["call_accountings"]),
        output_tokens=100_000 * len(recorded["call_accountings"]),
        cache_read_tokens=0, cache_write_tokens=0, model_timeline=None, catalog=CATALOG,
        workspace_doc=None, managed_doc=None, factors_doc=factors_doc, fs_cache={},
        recorded=recorded,
    )


@pytest.mark.parametrize("served_bys", [["google-vertex"], ["google-vertex", "anthropic"]])
@pytest.mark.parametrize(
    "doc", [{}, {"grid": {"default": {"g_per_kwh": 200, "basis": "location_based", "label": "x"}}},
            {"pue": {"cloud": 1.5, "label": "x"}}],
)
def test_whatif_without_water_keys_keeps_recorded_onsite_water(served_bys, doc):
    rec = _recorded_run(served_bys)
    assert rec["water"]["onsite_ml"] > 0
    out = _whatif(rec, doc)
    # On-site water is IT energy x WUE; grid and PUE scenarios do not change IT energy.
    # (Two-call runs differ from the one-shot aggregate only by per-call rounding.)
    assert out["energy_wh"] == pytest.approx(rec["energy_wh"], abs=1e-5)
    assert out["water"]["onsite_ml"] == pytest.approx(rec["water"]["onsite_ml"], abs=1e-4)
    # Off-site water follows facility energy (PUE changes it, nothing else here does).
    assert out["water"]["offsite_ml"] == pytest.approx(
        out["energy_wh_total"] * 4.81, rel=1e-4
    )
    if not doc:
        assert out["water"]["water_ml"] == pytest.approx(rec["water"]["water_ml"], abs=1e-4)


def test_whatif_with_water_keys_still_restates_water():
    rec = _recorded_run(["google-vertex"])
    out = _whatif(rec, {"water": {"site_wue_l_per_kwh": 0.5}})
    assert out["water"]["onsite_ml"] == pytest.approx(out["energy_wh"] * 0.5, rel=1e-4)


def test_water_factors_from_recorded_handles_a_mixed_site_wue_record():
    from tret.api.analytics import _water_factors_from_recorded

    rec = _recorded_run(["google-vertex", "anthropic"])
    wue = next(r for r in rec["water"]["factors"] if r["key"] == "site_wue_l_per_kwh")
    assert wue["value"] is None and wue["source"] == "mixed"
    f = _water_factors_from_recorded(rec["water"], it_energy_wh=rec["energy_wh"])
    assert f.site_wue_l_per_kwh == pytest.approx((1.15 + 0.375) / 2, rel=1e-4)
    assert _water_factors_from_recorded(rec["water"]) is None  # no IT energy: cannot form it


# ── settings API ─────────────────────────────────────────────────────────────
@pytest_asyncio.fixture
async def settings_client(session_factory):
    import httpx
    from fastapi import FastAPI

    from tret.api import auth
    from tret.db.engine import get_db

    app = FastAPI()
    app.include_router(auth.router)
    app.include_router(emissions_settings_api.router)

    async def _get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _get_db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_settings_api_rejects_a_bad_water_block_and_accepts_a_good_one(settings_client, seed):
    team = settings_workspace("Water Co")
    owner = settings_user(f"water-{uuid.uuid4().hex[:6]}@example.com")
    await seed(team, owner, settings_member(owner, team, role="owner"))
    await settings_login(settings_client, owner.email)

    bad = await settings_client.put("/api/workspace/settings/emissions", json={"water": {"band_low": 2}})
    assert bad.status_code == 422
    assert "water.band_low" in bad.json()["detail"]

    good = await settings_client.put(
        "/api/workspace/settings/emissions",
        json={"water": {"site_wue_l_per_kwh": 1.15, "country": "irl"}},
    )
    assert good.status_code == 200, good.text
    body = good.json()
    assert body["overrides"]["water"]["country"] == "IRL"
    effective = body["effective"]["anthropic"]["water"]
    assert effective["site_wue_l_per_kwh"] == 1.15
    by_key = {r["key"]: r for r in effective["records"]}
    assert by_key["site_wue_l_per_kwh"]["layer"] == "workspace"
    assert by_key["grid_water_l_per_kwh"]["source"] == "dataset:wri2020:IRL"
    # The general key is the cloud value; local runs keep their own default (0).
    assert body["effective"]["local"]["water"]["site_wue_l_per_kwh"] == 0.0

    got = (await settings_client.get("/api/workspace/settings/emissions")).json()
    assert got["effective"]["kimi"]["water"]["records"]


# ── docs endpoint ────────────────────────────────────────────────────────────
def test_docs_endpoint_serves_the_water_methodology():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from tret.api import docs
    from tret.api.auth import current_user
    from tret.db.engine import get_db

    app = FastAPI()
    app.include_router(docs.router)
    app.dependency_overrides[current_user] = lambda: None
    app.dependency_overrides[get_db] = lambda: None
    response = TestClient(app).get("/api/docs/water-methodology")
    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "Water methodology"
    assert body["repo_path"] == "docs/water-methodology.md"
    assert body["available"] is True
    assert body["markdown"].startswith("# Water methodology")


def test_decimal_import_is_not_needed_for_json_safety():
    # water blocks hold floats only (a Decimal would break the JSONB column).
    a = _account_with(MODEL)

    def walk(value):
        if isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)
        else:
            assert not isinstance(value, Decimal)

    walk(a["water"])


# ── partial roll-ups are not complete water ──────────────────────────────────
def test_combine_water_sums_inner_runs_without_water():
    a = _account_with(MODEL)["water"]
    legacy = {k: v for k, v in _account_with(SMALL).items() if k != "water"}
    partial = combine_accountings([_account_with(MODEL), legacy])["water"]
    assert partial["runs_without_water"] == 1 and partial["runs_counted"] == 1
    again = combine_water([partial, a])
    assert again["runs_without_water"] == 1
    assert again["runs_counted"] == 2


def test_partial_water_counts_as_no_water_in_summaries_and_analytics():
    from tret.api.analytics import _recorded_emissions

    legacy = {k: v for k, v in _account_with(SMALL).items() if k != "water"}
    partial = combine_accountings([_account_with(MODEL), legacy])
    assert partial["water"]["runs_without_water"] == 1
    assert emission_summary_fields(partial)["water_ml"] is None
    assert emission_event_fields(partial)["water_ml"] is None
    rec = _recorded_emissions(partial)
    assert rec["water_ml"] is None and rec["water_onsite_ml"] is None
    complete = combine_accountings([_account_with(MODEL), _account_with(SMALL)])
    assert _recorded_emissions(complete)["water_ml"] is not None


# ── preserve-measured-energy what-if keeps recorded water factors ────────────
def _preserved(recorded, scenario_doc):
    from tret.api.analytics import _preserved_measured_accounting

    return _preserved_measured_accounting(
        recorded=recorded, model=MODEL, tokens=TOKENS, workspace_doc={"water": {"site_wue_l_per_kwh": 2.0}},
        managed_doc=None, factors_doc=scenario_doc, created_at=None, catalog=CATALOG,
    )


def _measured_recorded():
    settings = _settings()
    factors = build_factor_set(
        provider="anthropic", settings=settings, model_id=MODEL.id,
        workspace_settings={"water": {"site_wue_l_per_kwh": 1.0}},
    )
    return energy_accounting(
        MODEL, *TOKENS, settings=settings, catalog=CATALOG, factors=factors,
        measured_energy_wh=5.0, measured_energy_boundary="node_it",
    )


def test_preserve_mode_keeps_recorded_water_factors_and_baseline():
    recorded = _measured_recorded()
    out = _preserved(recorded, {})
    # Today's workspace WUE (2.0) must not leak in: the run was recorded at 1.0.
    assert out["water"]["onsite_ml"] == pytest.approx(recorded["water"]["onsite_ml"], abs=1e-5)
    assert out["water"]["water_ml"] == pytest.approx(recorded["water"]["water_ml"], abs=1e-5)
    # Baseline is the retained recorded one, and avoided is consistent with it.
    assert out["water"]["baseline_water_ml"] == recorded["water"]["baseline_water_ml"]
    assert out["water"]["avoided_water_ml"] == pytest.approx(
        out["water"]["baseline_water_ml"] - out["water"]["water_ml"], abs=1e-5
    )


def test_preserve_mode_without_recorded_water_says_so_and_with_a_scenario_restates():
    recorded = _measured_recorded()
    recorded.pop("water")
    out = _preserved(recorded, {})
    assert out["water"]["onsite_ml"] == pytest.approx(5.0 * 2.0, abs=1e-5)  # today's ladder
    assert any("no recorded water factors" in c for c in out["water"]["caveats"])
    assert out["water"]["baseline_water_ml"] is not None  # recomputed from retained baseline energy

    changed = _preserved(_measured_recorded(), EmissionsOverrides(water={"site_wue_l_per_kwh": 3.0}))
    assert changed["water"]["onsite_ml"] == pytest.approx(5.0 * 3.0, abs=1e-5)
    base = changed["baseline"]
    assert changed["water"]["baseline_water_ml"] == pytest.approx(
        base["energy_wh"] * 3.0 + base["energy_wh_total"] * 4.81, rel=1e-4
    )
