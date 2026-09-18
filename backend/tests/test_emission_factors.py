"""The layered factor set: `run_override > harness > workspace > managed > env >
global_default`, resolved per factor rather than per document.

`tret/services/emissions.py` already knows how to pick one grid factor out of
several sources and record which rule won; this module (`emission_factors.py`)
generalises that to every accounting constant and adds two rungs above the
process environment. What this suite defends: the ladder's precedence holds for
every factor, an empty layer is indistinguishable from an absent one, every
provenance record still carries a real `layer`, the scope invariant survives
layering, and `energy_accounting(factors=None)` is exactly what it was before
`FactorSet` existed.
"""
from __future__ import annotations

from decimal import Decimal
from dataclasses import asdict

import pytest
from pydantic import ValidationError

from tret.config import GridFactor, Settings
from tret.providers.catalog import ModelCatalog, ModelInfo
from tret.services.emission_factors import (
    EmissionsOverrides,
    Resolved,
    build_factor_set,
    factor_set_for_model,
)
from tret.services.emissions import combine_accountings, energy_accounting

CATALOG = ModelCatalog()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("factor_boundary", "banana"),
        ("gas_coverage", "gasnope"),
        ("gwp_assessment_basis", "ar99"),
        ("electricity_mix_basis", "regional"),
        ("gwp_horizon_years", -1),
    ],
)
def test_grid_factor_rejects_unknown_method_metadata(field, value):
    with pytest.raises(ValidationError):
        GridFactor(g_per_kwh=100, **{field: value})


def test_cloud_explicit_embodied_override_is_counted_but_default_remains_unknown_zero():
    default = build_factor_set(provider="anthropic")
    assert default.embodied_g.value == 0
    supplied = build_factor_set(
        provider="anthropic",
        workspace_settings={"embodied": {"g_per_run": 2.5, "label": "supplier allocation"}},
    )
    assert supplied.embodied_g.value == Decimal("2.5")
    report = energy_accounting(_model(), 10, 10, factors=supplied)
    assert report["embodied_g"] == 2.5
    assert "embodied_hardware" in report["included_components"]
    assert "embodied_hardware" not in report["excluded_components"]
    assert report["coverage"]["functional_unit"] == "one_run"
    assert report["coverage"]["complete_total"] is None


def test_pue_upstream_requires_exact_served_by_and_baseline_context_does_not_reuse_it():
    managed = {
        "pue": {"upstreams": {"aws": {
            "value": 1.14, "label": "AWS fleet average",
            "url": "https://aws.amazon.com/sustainability/", "as_of": "2025-12-31",
        }}}
    }
    fallback = build_factor_set(provider="anthropic", managed_settings=managed)
    selected = build_factor_set(provider="anthropic", managed_settings=managed, served_by="aws")
    assert selected.pue.value == Decimal("1.14")
    assert selected.pue.disclosure["statistic"] == "operating_fleet_average"
    assert fallback.pue.value != selected.pue.value
    baseline = factor_set_for_model(selected, provider="anthropic", model_id="anthropic/test")
    assert baseline is not None
    assert baseline.pue.disclosure is None


def test_amazon_bedrock_served_by_selects_the_aws_disclosure():
    """F11: OpenRouter's Bedrock slug must reach the AWS upstream PUE — the
    only mapping that previously existed was `google-vertex` -> `google`."""
    managed = {
        "pue": {"upstreams": {"aws": {
            "value": 1.14, "label": "AWS fleet average",
            "url": "https://aws.amazon.com/sustainability/", "as_of": "2025-12-31",
        }}}
    }
    selected = build_factor_set(
        provider="openrouter", managed_settings=managed, served_by="amazon-bedrock"
    )
    assert selected.pue.value == Decimal("1.14")
    assert selected.pue.disclosure["statistic"] == "operating_fleet_average"
    # The direct (non-OpenRouter) Anthropic provider must never be aliased.
    unmapped = build_factor_set(
        provider="anthropic", managed_settings=managed, served_by="anthropic"
    )
    assert unmapped.pue.value != Decimal("1.14")


def test_time_allocation_is_added_once_and_supplier_double_count_is_rejected():
    allocation = {
        "method_id": "time_resource_share_v1", "complete_total_g": 1.25,
        "covered_subtotal_g": 1.25, "components": [],
    }
    report = energy_accounting(_model(), 10, 10, embodied_allocation=allocation)
    assert report["embodied_g"] == 1.25
    assert report["embodied_allocation"] == allocation
    # Accounting must never fail a run (see the project contract): a supplier
    # figure that would double-count embodied hardware is dropped rather than
    # raised, with a caveat recording that it happened.
    guarded = energy_accounting(
        _model(), 10, 10, embodied_allocation=allocation,
        supplier_includes_inference_hardware=True,
    )
    assert guarded["embodied_g"] == 0.0
    assert guarded.get("embodied_allocation") is None
    caveat = next(
        c for c in guarded["caveats"] if c["key"] == "embodied_double_add_prevented"
    )
    assert caveat["applies"] is True


def _settings(**over) -> Settings:
    return Settings(**over)


def _model(energy_class: str = "L", provider: str = "anthropic", **over) -> ModelInfo:
    base = dict(
        id=f"{provider}/test",
        provider=provider,
        wire_id="test-1",
        display_name="Test",
        context_window=200_000,
        input_price_per_mtok=Decimal("3"),
        output_price_per_mtok=Decimal("15"),
        cost_tier="standard",
        energy_class=energy_class,
    )
    base.update(over)
    return ModelInfo(**base)


def _account(model: ModelInfo, settings: Settings | None = None, **kw) -> dict:
    tokens = kw.pop("tokens", (1_000_000, 0, 0, 0))
    return energy_accounting(model, *tokens, settings=settings or _settings(), catalog=CATALOG, **kw)


def _scope_sum(report: dict) -> Decimal:
    s = report["scopes"]
    return Decimal(str(s["scope1_g"])) + Decimal(str(s["scope2_g"])) + Decimal(str(s["scope3_g"]))


# ── the ladder, per factor ────────────────────────────────────────────────────
def _grid_doc(value, label):
    return {
        "grid": {"providers": {"anthropic": {"g_per_kwh": value, "basis": "location_based", "label": label}}}
    }


def _pue_doc(value, label):
    return {"pue": {"cloud": value, "label": label}}


def _embodied_doc(value, label):
    return {"embodied": {"g_per_run": value, "label": label}}


def _band_low_doc(value, label):
    return {"band": {"low": value, "label": label}}


def _baseline_doc(value, _label=None):
    return {"baseline_model": value}


@pytest.mark.parametrize(
    "doc_factory, run_override_key, resolved_attr, doc_value, override_value, provider",
    [
        (_grid_doc, "grid_g_per_kwh", "grid", 999, 111, "anthropic"),
        (_pue_doc, "pue", "pue", 1.9, 1.4, "anthropic"),
        # Embodied hardware is only ever amortized for self-hosted inference
        # (see `_resolve_embodied`), so this factor needs a local provider —
        # every other layer here is deployment-agnostic.
        (_embodied_doc, "embodied_g", "embodied_g", 8.0, 3.0, "local"),
        (_band_low_doc, "band_low", "band_low", 4.0, 2.0, "anthropic"),
        (_baseline_doc, "baseline_model", "baseline_model", "anthropic/x", "anthropic/other", "anthropic"),
    ],
)
def test_run_override_beats_every_configured_layer(
    doc_factory, run_override_key, resolved_attr, doc_value, override_value, provider
):
    doc = doc_factory(doc_value, "layer doc")
    fs = build_factor_set(
        provider=provider,
        run_overrides={run_override_key: override_value},
        harness_settings=doc,
        workspace_settings=doc,
        managed_settings=doc,
    )
    resolved: Resolved = getattr(fs, resolved_attr)
    assert resolved.layer == "run_override"
    assert resolved.source == "run_override"
    assert resolved.setting is None
    expected = override_value if resolved_attr == "baseline_model" else Decimal(str(override_value))
    assert resolved.value == expected


@pytest.mark.parametrize(
    "doc_factory, resolved_attr, provider",
    [
        (_grid_doc, "grid", "anthropic"),
        (_pue_doc, "pue", "anthropic"),
        (_embodied_doc, "embodied_g", "local"),
        (_band_low_doc, "band_low", "anthropic"),
    ],
)
def test_harness_beats_workspace_beats_managed(doc_factory, resolved_attr, provider):
    harness = doc_factory(101, "harness label")
    workspace = doc_factory(102, "workspace label")
    managed = doc_factory(103, "managed label")

    only_managed = build_factor_set(provider=provider, managed_settings=managed)
    assert getattr(only_managed, resolved_attr).layer == "managed"
    assert getattr(only_managed, resolved_attr).value == Decimal("103")

    ws_over_managed = build_factor_set(
        provider=provider, workspace_settings=workspace, managed_settings=managed
    )
    assert getattr(ws_over_managed, resolved_attr).layer == "workspace"
    assert getattr(ws_over_managed, resolved_attr).value == Decimal("102")

    harness_over_all = build_factor_set(
        provider=provider,
        harness_settings=harness,
        workspace_settings=workspace,
        managed_settings=managed,
    )
    assert getattr(harness_over_all, resolved_attr).layer == "harness"
    assert getattr(harness_over_all, resolved_attr).value == Decimal("101")


def test_env_beats_global_default_only_when_actually_set():
    # Nothing configured anywhere: the shipped constant, global_default.
    default_fs = build_factor_set(provider="anthropic", settings=_settings())
    assert default_fs.grid.layer == "global_default"
    assert default_fs.pue.layer == "global_default"
    assert default_fs.embodied_g.layer == "global_default"  # cloud: always global_default
    assert default_fs.band_low.layer == "global_default"
    assert default_fs.baseline_model.layer == "global_default"

    # The operator actually set these TRET_* values (an explicit constructor
    # kwarg is indistinguishable from an env var for `model_fields_set`).
    env_settings = _settings(
        grid_co2e_g_per_kwh=400.0,
        datacenter_pue=1.3,
        uncertainty_band_low=2.0,
        uncertainty_band_high=2.0,
        emissions_baseline_model="anthropic/test",
    )
    env_fs = build_factor_set(provider="anthropic", settings=env_settings)
    assert env_fs.grid.layer == "env"
    assert env_fs.grid.setting == "TRET_GRID_CO2E_G_PER_KWH"
    assert env_fs.pue.layer == "env"
    assert env_fs.band_low.layer == "env"
    assert env_fs.baseline_model.layer == "env"


def test_a_per_provider_grid_factor_is_always_env_never_global_default():
    # There is no shipped default for a per-provider entry or the legacy local
    # setting — either one existing at all means an operator configured it.
    settings = _settings(grid_factors={"anthropic": {"g_per_kwh": 120}})
    fs = build_factor_set(provider="anthropic", settings=settings)
    assert fs.grid.layer == "env"
    assert fs.grid.source == "provider:anthropic"

    local_settings = _settings(local_grid_co2e_g_per_kwh=30.0)
    local_fs = build_factor_set(provider="local", settings=local_settings)
    assert local_fs.grid.layer == "env"
    assert local_fs.grid.source == "local_setting"


# ── absent key falls through ──────────────────────────────────────────────────
def test_a_workspace_document_that_only_sets_grid_leaves_everything_else_to_fall_through():
    fs = build_factor_set(
        provider="anthropic",
        workspace_settings={
            "grid": {"default": {"g_per_kwh": 55, "basis": "location_based", "label": "L"}}
        },
    )
    assert fs.grid.layer == "workspace"
    assert fs.pue.layer == "global_default"
    assert fs.embodied_g.layer == "global_default"
    assert fs.band_low.layer == "global_default"
    assert fs.band_high.layer == "global_default"
    assert fs.baseline_model.layer == "global_default"
    assert fs.layers_present == ("workspace", "global_default")


# ── per-provider beats default within one layer ───────────────────────────────
def test_a_provider_entry_beats_the_default_in_the_same_layer():
    doc = {
        "grid": {
            "default": {"g_per_kwh": 10, "basis": "location_based", "label": "default"},
            "providers": {
                "anthropic": {"g_per_kwh": 20, "basis": "market_based", "label": "anthropic"}
            },
        }
    }
    fs = build_factor_set(provider="anthropic", workspace_settings=doc)
    assert fs.grid.value == Decimal("20")
    assert fs.grid.source == "workspace:provider:anthropic"
    assert fs.grid.setting == "workspace.emissions.grid.providers.anthropic"

    other_provider_fs = build_factor_set(provider="kimi", workspace_settings=doc)
    assert other_provider_fs.grid.value == Decimal("10")
    assert other_provider_fs.grid.source == "workspace"
    assert other_provider_fs.grid.setting == "workspace.emissions.grid.default"


# ── a more specific layer's default beats a less specific layer's provider ───
def test_a_more_specific_layers_default_beats_a_less_specific_layers_provider_entry():
    workspace = {
        "grid": {"default": {"g_per_kwh": 77, "basis": "location_based", "label": "ws default"}}
    }
    managed = {
        "grid": {
            "providers": {
                "anthropic": {"g_per_kwh": 88, "basis": "market_based", "label": "managed provider"}
            }
        }
    }
    fs = build_factor_set(
        provider="anthropic", workspace_settings=workspace, managed_settings=managed
    )
    assert fs.grid.value == Decimal("77")
    assert fs.grid.layer == "workspace"
    assert fs.grid.source == "workspace"


# ── empty managed rung ────────────────────────────────────────────────────────
@pytest.mark.parametrize("empty", [None, {}])
def test_an_empty_managed_layer_resolves_exactly_as_without_one(empty):
    settings = _settings(grid_co2e_g_per_kwh=333.0)
    with_none = build_factor_set(provider="anthropic", settings=settings, managed_settings=empty)
    without = build_factor_set(provider="anthropic", settings=settings)
    assert with_none == without


# ── provenance: every record carries a layer; setting names the winning path ─
def test_every_record_in_factors_carries_a_layer():
    report = _account(_model("L"))
    for f in report["factors"]:
        assert "layer" in f, f["key"]
        assert f["layer"] in (
            "run_override", "harness", "workspace", "managed", "env", "global_default",
        )


def test_a_workspace_win_names_its_own_setting_path_and_is_visible_at_the_top_level():
    settings = _settings()
    workspace_settings = {
        "grid": {
            "providers": {
                "anthropic": {"g_per_kwh": 133, "basis": "market_based", "label": "ontario PPA"}
            }
        }
    }
    fs = build_factor_set(
        provider="anthropic", settings=settings, workspace_settings=workspace_settings
    )
    report = _account(_model("L"), settings, factors=fs)

    assert report["grid_co2e_g_per_kwh"] == 133.0
    assert report["grid_co2e_source"] == "workspace:provider:anthropic"
    assert report["grid_co2e_layer"] == "workspace"
    assert report["factor_layers"] == ["workspace", "global_default"]

    grid_factor = next(f for f in report["factors"] if f["key"] == "grid_intensity")
    assert grid_factor["layer"] == "workspace"
    assert grid_factor["setting"] == "workspace.emissions.grid.providers.anthropic"
    assert grid_factor["source_label"] == "ontario PPA"


# ── the scope invariant survives layering ─────────────────────────────────────
@pytest.mark.parametrize(
    "workspace_settings",
    [
        None,
        {"grid": {"default": {"g_per_kwh": 250, "basis": "location_based", "label": "L"}}},
        {"pue": {"cloud": 1.5, "label": "L"}},
        {"embodied": {"g_per_run": 5.0, "label": "L"}},
        {"band": {"low": 3.0, "high": 3.0, "label": "L"}},
    ],
)
def test_co2e_equals_the_sum_of_scopes_under_every_layering(workspace_settings):
    settings = _settings()
    fs = build_factor_set(
        provider="anthropic", settings=settings, workspace_settings=workspace_settings
    )
    report = _account(_model("L"), settings, factors=fs)
    assert Decimal(str(report["co2e_g"])) == pytest.approx(_scope_sum(report))


def test_co2e_equals_the_sum_of_scopes_for_a_local_workspace_embodied_override():
    settings = _settings()
    fs = build_factor_set(
        provider="local",
        settings=settings,
        workspace_settings={"embodied": {"g_per_run": 12.0, "label": "site metered"}},
    )
    report = _account(_model("S", provider="local", id="local/x", cost_tier="local",
                             input_price_per_mtok=Decimal("0"), output_price_per_mtok=Decimal("0")),
                       settings, factors=fs)
    assert report["embodied_g"] == pytest.approx(12.0)
    assert Decimal(str(report["co2e_g"])) == pytest.approx(_scope_sum(report))


# ── schema validation ─────────────────────────────────────────────────────────
def test_a_numeric_override_without_a_label_is_rejected_and_names_the_field():
    with pytest.raises(ValidationError) as exc:
        EmissionsOverrides(pue={"cloud": 1.3})
    assert "pue.label is required when pue.cloud or pue.local is set" in str(exc.value)

    with pytest.raises(ValidationError) as exc:
        EmissionsOverrides(grid={"default": {"g_per_kwh": 42}})
    assert "grid.default.label is required" in str(exc.value)

    with pytest.raises(ValidationError) as exc:
        EmissionsOverrides(embodied={"g_per_run": 1.0})
    assert "embodied.label is required" in str(exc.value)

    with pytest.raises(ValidationError) as exc:
        EmissionsOverrides(band={"low": 2.0})
    assert "band.label is required" in str(exc.value)


def test_pue_below_one_is_rejected():
    with pytest.raises(ValidationError) as exc:
        EmissionsOverrides(pue={"cloud": 0.9, "label": "x"})
    assert "pue.cloud must be >= 1.0" in str(exc.value)


def test_band_below_one_is_rejected():
    with pytest.raises(ValidationError) as exc:
        EmissionsOverrides(band={"low": 0.5, "high": 2.0, "label": "x"})
    assert "band.low must be >= 1.0" in str(exc.value)


def test_an_unknown_key_anywhere_is_rejected():
    with pytest.raises(ValidationError):
        EmissionsOverrides(bogus_top_level=1)
    with pytest.raises(ValidationError):
        EmissionsOverrides(grid={"bogus": 1})
    with pytest.raises(ValidationError):
        EmissionsOverrides(grid={"default": {"g_per_kwh": 1, "label": "x", "bogus": 2}})
    with pytest.raises(ValidationError):
        EmissionsOverrides(pue={"cloud": 1.2, "label": "x", "bogus": 2})


def test_a_valid_full_document_round_trips():
    raw = {
        "version": 1,
        "grid": {
            "default": {
                "g_per_kwh": 42,
                "basis": "location_based",
                "label": "Ontario grid, IESO 2024",
                "url": "https://example.org/ieso",
                "as_of": "2024-12-31",
            },
            "providers": {
                "anthropic": {
                    "g_per_kwh": 120,
                    "basis": "market_based",
                    "label": "provider PPA disclosure",
                    "as_of": "2025-06-01",
                }
            },
        },
        "pue": {"cloud": 1.2, "local_profile": "onprem_datacenter", "local": 1.38, "label": "Site metered, Q2 2025"},
        "embodied": {"g_per_run": 0.31, "label": "vendor disclosure"},
        "band": {"low": 2.0, "high": 2.0, "label": "narrowed after metering"},
        "baseline_model": "anthropic/claude-opus-5",
        "source_name": "acme-admin",
        "updated_by": "ops@example.com",
        "updated_at": "2025-06-01T00:00:00Z",
    }
    doc = EmissionsOverrides(**raw)
    assert doc.grid.default.g_per_kwh == 42.0
    assert doc.grid.providers["anthropic"].label == "provider PPA disclosure"
    assert doc.pue.local_profile == "onprem_datacenter"
    assert doc.embodied.g_per_run == 0.31
    assert doc.band.low == 2.0
    assert doc.baseline_model == "anthropic/claude-opus-5"
    assert doc.source_name == "acme-admin"

    # And it resolves through the ladder without error.
    fs = build_factor_set(provider="anthropic", workspace_settings=raw)
    assert fs.grid.value == Decimal("120.0")
    assert fs.pue.value == Decimal("1.2")


# ── energy_accounting(factors=None) is exactly what it was before ────────────
def test_energy_accounting_with_no_factors_matches_an_explicit_default_factor_set():
    settings = _settings(grid_co2e_g_per_kwh=444.0, datacenter_pue=1.25)
    model = _model("L")
    implicit = energy_accounting(model, 100_000, 20_000, settings=settings, catalog=CATALOG)
    explicit_fs = build_factor_set(provider=model.provider, settings=settings)
    explicit = energy_accounting(
        model, 100_000, 20_000, settings=settings, catalog=CATALOG, factors=explicit_fs
    )
    assert implicit == explicit


def test_a_run_override_grid_still_wins_over_a_pre_built_factor_set():
    settings = _settings()
    model = _model("L")
    fs = build_factor_set(
        provider=model.provider,
        settings=settings,
        workspace_settings={
            "grid": {"default": {"g_per_kwh": 55, "basis": "location_based", "label": "L"}}
        },
    )
    report = energy_accounting(
        model, 100_000, 20_000, settings=settings, catalog=CATALOG, factors=fs, grid_g_per_kwh=999.0
    )
    assert report["grid_co2e_g_per_kwh"] == 999.0
    assert report["grid_co2e_source"] == "run_override"
    assert report["grid_co2e_layer"] == "run_override"
    assert "run_override" in report["factor_layers"]


# ── combine_accountings unions factor_layers ──────────────────────────────────
def test_combine_accountings_unions_factor_layers_across_segments():
    settings = _settings()
    a = _model("L", provider="anthropic")
    b = [m for m in CATALOG.all(curated_only=True) if m.provider != "anthropic"][0]

    fs_workspace = build_factor_set(
        provider="anthropic",
        settings=settings,
        workspace_settings={
            "grid": {"default": {"g_per_kwh": 60, "basis": "location_based", "label": "L"}}
        },
    )
    block_a = energy_accounting(a, 1000, 500, settings=settings, catalog=CATALOG, factors=fs_workspace)
    block_b = energy_accounting(b, 1000, 500, settings=settings, catalog=CATALOG)

    combined = combine_accountings([block_a, block_b])
    # Precedence order (most specific first), matching a single segment's own
    # `factor_layers` (`FactorSet.layers_present`) — not alphabetical, which
    # would put "global_default" ahead of "workspace".
    from tret.services.emission_factors import LAYER_PRECEDENCE

    present = set(block_a["factor_layers"]) | set(block_b["factor_layers"])
    assert combined["factor_layers"] == [layer for layer in LAYER_PRECEDENCE if layer in present]
    assert "workspace" in combined["factor_layers"]
    assert combined["factor_layers"].index("workspace") < combined["factor_layers"].index(
        "global_default"
    )


def test_combine_accountings_withholds_carbon_for_incompatible_factor_metadata():
    first = energy_accounting(_model(), 100, 20)
    second = dict(energy_accounting(_model(), 100, 20))
    second["grid_gas_coverage"] = "co2"
    combined = combine_accountings([first, second])
    assert combined["carbon_summable"] is False
    assert combined["co2e_g"] is None
    assert combined["coverage"]["complete_total"] is None


def test_unknown_custom_grid_factors_require_identical_factor_identity():
    first = energy_accounting(_model(), 100, 20, grid_g_per_kwh=100)
    same = energy_accounting(_model(), 100, 20, grid_g_per_kwh=100)
    different = energy_accounting(_model(), 100, 20, grid_g_per_kwh=200)
    assert combine_accountings([first, same])["carbon_summable"] is True
    mixed = combine_accountings([first, different])
    assert mixed["carbon_summable"] is False
    assert mixed["co2e_g"] is None
    assert len(mixed["by_basis"]) == 2
    assert sorted(row["co2e_g"] for row in mixed["by_basis"]) == sorted(
        [first["co2e_g"], different["co2e_g"]]
    )
    assert all(
        row["co2e_g"] != pytest.approx(first["co2e_g"] + different["co2e_g"])
        for row in mixed["by_basis"]
    )
    caveat = next(c for c in mixed["caveats"] if c["key"] == "carbon_crosses_grid_basis")
    assert "incompatible grid factors" in caveat["label"]


# ── a layered baseline_model actually changes the counterfactual model ───────
def test_a_layered_baseline_model_changes_which_model_the_run_is_compared_against():
    settings = _settings()
    candidates = [m for m in CATALOG.all(curated_only=True) if m.provider != "local"]
    candidates.sort(key=lambda m: (-(m.energy_wh_per_mtok or Decimal(0)), m.id))
    auto_pick, alternative = candidates[0], candidates[1]
    assert auto_pick.id != alternative.id

    model = _model("L")
    default_report = _account(model, settings)
    assert default_report["baseline"]["model"] == auto_pick.id

    fs = build_factor_set(
        provider="anthropic", settings=settings, workspace_settings={"baseline_model": alternative.id}
    )
    overridden_report = _account(model, settings, factors=fs)
    assert overridden_report["baseline"]["model"] == alternative.id


def test_resolution_context_is_immutable_minimal_and_contains_no_settings_secrets():
    sentinel = "secret-sentinel-must-not-be-retained"
    raw = {
        "grid": {"default": {
            "g_per_kwh": 77, "basis": "location_based", "label": "original",
        }}
    }
    factors = build_factor_set(
        provider="anthropic", settings=Settings(anthropic_api_key=sentinel),
        workspace_settings=raw, model_id="anthropic/test",
    )
    raw["grid"]["default"]["g_per_kwh"] = 999
    context_dump = repr(asdict(factors.resolution_context))
    assert sentinel not in context_dump

    baseline = factor_set_for_model(
        factors, provider="anthropic", model_id="anthropic/other"
    )
    assert baseline.grid.value == Decimal("77")
    # The public context consists only of tuples/scalars: callers cannot mutate
    # nested documents and change a later counterfactual resolution.
    assert isinstance(factors.resolution_context.workspace_settings, tuple)
