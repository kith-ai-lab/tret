"""Rolling several models' emissions into one run-level figure.

A run can span more than one model — because it switched mid-flight, or because
its routing and compaction calls ran elsewhere — and each model brings its own
energy class, its own provider and that provider's own grid factor. The rule
this suite defends is the one `api/analytics.py` already applies at window
scale, stated in EMISSIONS_DISCLAIMER: **carbon may only be summed within one
GHG Protocol basis.** Energy, tokens and money are summable across bases; carbon
is not, because location-based and market-based figures answer different
questions.
"""
from __future__ import annotations

import pytest

from tret.providers.catalog import ModelCatalog
from tret.services.emissions import combine_accountings, energy_accounting


def _blocks():
    models = [m for m in ModelCatalog().all(curated_only=True) if m.supports_tools]
    return models[0], models[3]


def _block(model, *, basis: str | None = None, grid: float | None = None):
    block = energy_accounting(model, 1000, 500, grid_g_per_kwh=grid)
    if basis is not None:
        block["grid_co2e_basis"] = basis
    return block


def _mixed():
    a, b = _blocks()
    return combine_accountings(
        [
            _block(a, basis="market_based", grid=400.0),
            _block(b, basis="location_based", grid=500.0),
        ]
    )


# ── the degenerate case must stay degenerate ─────────────────────────────────
def test_a_single_block_comes_back_untouched():
    # The overwhelming majority of runs use one model. None of them should
    # acquire a different accounting record because this function exists.
    a, _ = _blocks()
    block = _block(a)
    assert combine_accountings([block]) is block


def test_no_blocks_is_none_not_an_empty_shell():
    assert combine_accountings([]) is None
    assert combine_accountings([None]) is None


# ── within one basis, everything sums ────────────────────────────────────────
def test_carbon_sums_when_every_segment_shares_a_basis():
    a, b = _blocks()
    one, two = _block(a), _block(b)
    combined = combine_accountings([one, two])
    assert combined["co2e_g"] == pytest.approx(one["co2e_g"] + two["co2e_g"])
    assert combined["carbon_summable"] is True
    assert "by_basis" not in combined


def test_energy_and_tokens_always_sum():
    a, b = _blocks()
    one, two = _block(a), _block(b)
    combined = combine_accountings([one, two])
    assert combined["energy_wh"] == pytest.approx(one["energy_wh"] + two["energy_wh"])
    assert combined["tokens"]["input"] == 2000
    assert isinstance(combined["tokens"]["input"], int)  # not 2000.0


# ── across bases, carbon is withheld ─────────────────────────────────────────
def test_carbon_is_null_when_the_segments_span_two_bases():
    # The bug this suite exists for. Nulling the *basis* while keeping the sum
    # was worse than doing nothing: the number survived and the one field that
    # would have exposed it was removed.
    combined = _mixed()
    assert combined["co2e_g"] is None
    assert combined["carbon_summable"] is False


def test_every_carbon_field_follows_the_rule_not_just_the_headline():
    combined = _mixed()
    assert combined["scopes"]["scope1_g"] is None
    assert combined["scopes"]["scope2_g"] is None
    assert combined["scopes"]["scope3_g"] is None
    assert combined["baseline"]["co2e_g"] is None
    assert combined["baseline"]["avoided_co2e_g"] is None


def test_energy_money_and_tokens_survive_a_basis_split():
    # These are the figures that remain legitimate, and withholding them would
    # be its own kind of dishonesty.
    combined = _mixed()
    assert combined["energy_wh"] > 0
    assert combined["energy_wh_total"] > 0
    assert combined["cost"]["usd"] > 0
    assert combined["tokens"]["input"] == 2000


def test_baseline_energy_still_sums_because_the_baseline_model_is_one_model():
    # `resolve_baseline_model` picks a single global baseline, so baseline energy
    # is a linear function of tokens against one model. Baseline *carbon* is
    # carbon and is withheld above.
    combined = _mixed()
    assert combined["baseline"]["energy_wh"] > 0


def test_embodied_carbon_is_not_electricity_so_no_basis_applies():
    combined = _mixed()
    assert combined["embodied_g"] is not None


def test_the_carbon_is_still_available_per_basis():
    # Withheld as one number, not hidden. Same shape as `by_basis` in the
    # analytics rollup.
    combined = _mixed()
    subtotals = {row["grid_co2e_basis"]: row["co2e_g"] for row in combined["by_basis"]}
    assert set(subtotals) == {"market_based", "location_based"}
    assert all(value > 0 for value in subtotals.values())
    assert combined["grid_bases"] == ["location_based", "market_based"]


def test_a_reader_is_told_why_the_carbon_is_missing():
    # A null with no explanation reads as "not recorded", which is a different
    # and much less alarming claim than "this addition is not legitimate".
    combined = _mixed()
    keys = {c["key"] for c in combined["caveats"]}
    assert "carbon_crosses_grid_basis" in keys
    assert "multi_model_run" in keys
    assert "by_basis" in combined["basis"]


def test_cost_discloses_that_it_is_list_price_only():
    # A total mixing a cloud call with a local one understates real cost:
    # electricity and hardware amortization are not in the provider's bill.
    combined = _mixed()
    assert "cost_is_list_price_only" in {c["key"] for c in combined["caveats"]}


# ── per-model factors ────────────────────────────────────────────────────────
def test_a_factor_the_segments_disagree_on_is_null_never_averaged():
    a, b = _blocks()
    combined = combine_accountings([_block(a), _block(b)])
    if a.energy_class != b.energy_class:
        assert combined["energy_class"] is None
    assert combined["models"] == [a.id, b.id]


def test_a_factor_every_segment_agreed_on_is_kept():
    a, b = _blocks()
    combined = combine_accountings([_block(a), _block(b)])
    assert combined["pue"] is not None  # both cloud


def test_combined_uncertainty_band_sums_across_segments():
    """A run that switched models carries the band of the *whole* run, not the
    first segment's — every per-model block contributes its own low/high."""
    from tret.services.emissions import combine_accountings

    def block(co2e, low, high, ewh, basis="location_based"):
        return {
            "co2e_g": co2e,
            "energy_wh": ewh,
            "energy_wh_total": ewh * 1.2,
            "grid_co2e_basis": basis,
            "scopes": {"scope1_g": 0.0, "scope2_g": 0.0, "scope3_g": co2e},
            "uncertainty": {
                "kind": "judgment_band",
                "is_confidence_interval": False,
                "band_factor_low": 2.5,
                "band_factor_high": 2.5,
                "co2e_g_low": low,
                "co2e_g_high": high,
                "energy_wh_low": ewh / 2.5,
                "energy_wh_high": ewh * 2.5,
                "energy_wh_total_low": ewh * 1.2 / 2.5,
                "energy_wh_total_high": ewh * 1.2 * 2.5,
            },
        }

    a = block(10.0, 4.0, 25.0, 1.0)
    b = block(30.0, 12.0, 75.0, 3.0)
    combined = combine_accountings([a, b])
    band = combined["uncertainty"]
    assert band["co2e_g_low"] == 16.0
    assert band["co2e_g_high"] == 100.0
    assert band["energy_wh_low"] == 4.0 / 2.5
    assert band["energy_wh_high"] == 4.0 * 2.5
    assert band["band_factor_low"] == 2.5
    assert band["is_confidence_interval"] is False

    # Mixed bases: the carbon edges are nulled like every other carbon figure,
    # the energy edges still add.
    mixed = combine_accountings([a, block(30.0, 12.0, 75.0, 3.0, basis="market_based")])
    assert mixed["uncertainty"]["co2e_g_low"] is None
    assert mixed["uncertainty"]["co2e_g_high"] is None
    assert mixed["uncertainty"]["energy_wh_low"] == 4.0 / 2.5

    # Segments that disagree on the band factor report no single factor.
    c = block(30.0, 12.0, 75.0, 3.0)
    c["uncertainty"]["band_factor_low"] = 2.0
    assert combine_accountings([a, c])["uncertainty"]["band_factor_low"] is None
