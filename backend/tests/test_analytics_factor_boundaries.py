from tret.api.analytics import (
    NOT_SUMMABLE_NOTE,
    _add_to_bucket,
    _bucket_json,
    _emissions_bucket,
    _recorded_emissions,
)


def test_same_ghg_basis_does_not_allow_mixed_gas_boundaries():
    base = {"energy_wh": 1, "energy_wh_total": 1.2, "co2e_g": .5,
            "grid_co2e_basis": "location_based", "grid_factor_boundary": "generation",
            "grid_gas_coverage": "co2", "grid_gwp_assessment_basis": "unknown"}
    bucket = _emissions_bucket()
    _add_to_bucket(bucket, _recorded_emissions(base))
    _add_to_bucket(bucket, _recorded_emissions(base | {"grid_gas_coverage": "co2e"}))
    result = _bucket_json(bucket)
    assert result["co2e_g"] is None
    assert result["carbon_is_summable"] is False
    assert result["energy_wh"] == 2.4


def test_unknown_custom_factors_require_the_same_recorded_identity():
    base = {
        "energy_wh": 1,
        "energy_wh_total": 1.2,
        "co2e_g": 0.1,
        "grid_co2e_g_per_kwh": 100,
    }
    same = _emissions_bucket()
    _add_to_bucket(same, _recorded_emissions(base))
    _add_to_bucket(same, _recorded_emissions(dict(base)))
    assert _bucket_json(same)["carbon_is_summable"] is True

    different = _emissions_bucket()
    _add_to_bucket(different, _recorded_emissions(base))
    _add_to_bucket(
        different,
        _recorded_emissions(base | {"grid_co2e_g_per_kwh": 200, "co2e_g": 0.2}),
    )
    result = _bucket_json(different)
    assert result["carbon_is_summable"] is False
    assert result["co2e_g"] is None


def test_structured_mixed_run_keeps_energy_but_withholds_carbon():
    accounting = {
        "energy_wh": 10,
        "energy_wh_total": 12,
        "co2e_g": None,
        "carbon_summable": False,
        "by_basis": [
            {"grid_co2e_basis": "location_based", "co2e_g": 1},
            {"grid_co2e_basis": "market_based", "co2e_g": 2},
        ],
    }
    rec = _recorded_emissions(accounting)
    assert rec is not None
    bucket = _emissions_bucket()
    _add_to_bucket(bucket, rec)
    result = _bucket_json(bucket)
    assert result["energy_wh"] == 12
    assert result["carbon_is_summable"] is False
    assert result["co2e_g"] is None
    assert result["runs_without_carbon_total"] == 1


def test_mixed_grid_factor_signature_within_one_basis_gets_a_specific_note():
    """F4: two runs sharing a GHG basis but priced under different grid-factor
    signatures (e.g. the shipped default changing 470 -> 458.49) get a note
    naming the count and values of the differing factors, saying energy and
    dollars still sum, and pointing at the labelled what-if restatement — not
    the generic cross-basis `NOT_SUMMABLE_NOTE`."""
    base = {
        "energy_wh": 1, "energy_wh_total": 1.2, "co2e_g": 0.5,
        "grid_co2e_basis": "location_based", "grid_co2e_g_per_kwh": 470.0,
    }
    bucket = _emissions_bucket()
    _add_to_bucket(bucket, _recorded_emissions(base))
    _add_to_bucket(bucket, _recorded_emissions(base | {"grid_co2e_g_per_kwh": 458.49, "co2e_g": 0.49}))
    result = _bucket_json(bucket)
    assert result["carbon_is_summable"] is False
    assert result["co2e_g"] is None
    assert result["energy_wh"] == 2.4
    note = result["not_summable_note"]
    assert note is not None
    assert note != NOT_SUMMABLE_NOTE
    assert "2 different grid-factor signatures" in note
    assert "470.0" in note and "458.49" in note
    assert "still sum" in note
    assert "/api/analytics/emissions/whatif" in note


def test_a_true_cross_basis_mix_keeps_the_generic_note():
    """A window mixing GHG bases (not just factor signatures within one) is a
    different, wider withholding — it keeps the general note, not F4's."""
    base = {"energy_wh": 1, "energy_wh_total": 1.2, "co2e_g": 0.5,
            "grid_co2e_basis": "location_based", "grid_co2e_g_per_kwh": 470.0}
    bucket = _emissions_bucket()
    _add_to_bucket(bucket, _recorded_emissions(base))
    _add_to_bucket(bucket, _recorded_emissions(base | {"grid_co2e_basis": "market_based"}))
    result = _bucket_json(bucket)
    assert result["carbon_is_summable"] is False
    assert result["not_summable_note"] == NOT_SUMMABLE_NOTE


def test_structured_partial_row_reports_null_carbon_not_a_masked_zero():
    """N1: a structured-partial row's `co2e_g` must be None, never a masked
    Decimal(0) — `carbon_available` already blocks it from any published sum,
    but the zero itself must not exist for a consumer to add in by mistake."""
    accounting = {
        "energy_wh": 5, "energy_wh_total": 6, "co2e_g": None,
        "carbon_summable": False,
        "by_basis": [{"grid_co2e_basis": "location_based", "co2e_g": 1}],
    }
    rec = _recorded_emissions(accounting)
    assert rec["co2e_g"] is None
    assert rec["carbon_available"] is False

    # A structured-partial row still contributes energy to a by_basis bucket
    # (energy is always summable) even though its carbon stays withheld there.
    bucket = _emissions_bucket()
    _add_to_bucket(bucket, rec)
    _add_to_bucket(bucket, _recorded_emissions({
        "energy_wh": 2, "energy_wh_total": 2, "co2e_g": 0.3,
        "grid_co2e_basis": "location_based",
    }))
    result = _bucket_json(bucket)
    assert result["runs"] == 2
    assert result["energy_wh"] == 8.0
    assert result["runs_without_carbon_total"] == 1
    assert result["carbon_is_summable"] is False
    assert result["co2e_g"] is None


def test_combined_cost_totals_take_precedence_over_disagreed_baseline_fields():
    accounting = {
        "energy_wh": 1,
        "co2e_g": 0.1,
        "cost": {"avoided_usd": 0.00186, "baseline_usd": 0.00324},
        "baseline": {"avoided_usd": None, "cost_usd": None},
    }
    rec = _recorded_emissions(accounting)
    assert float(rec["avoided_usd"]) == 0.00186
    assert float(rec["baseline_usd"]) == 0.00324
