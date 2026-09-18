"""Phase 2 of tret's emissions work: pluggable per-token energy strategies, and
measured energy on self-hosted runs.

Three ways a run's per-token energy constant can be reached, in precedence
order: an operator's `model_overrides` entry for this exact model id, the
model's own explicit `energy_wh_per_mtok` (today's behavior, unchanged), the
EcoLogits active-parameter formula (opt-in, only when a `model_overrides`/
explicit constant did not already win and the model carries `active_params_b`),
and the class ladder as the final fallback. Separately, `energy_accounting`
can take an operator-supplied `measured_energy_wh` for any deployment, which
replaces the *estimate* with a real IT-load figure while leaving PUE, grid
intensity and embodied hardware to still apply on top.

What this suite defends: the precedence order and what each rung records in
`factors`/`caveats`; the active-parameter formula's shape and its documented
floor; that a measured run's numbers compose exactly (buckets, scopes, co2e);
and that `combine_accountings` unions `energy_source` honestly.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from tret.config import Settings
from tret.providers.catalog import ModelCatalog, ModelInfo
from tret.services.emission_factors import EmissionsOverrides, build_factor_set
from tret.services.emissions import (
    ENERGY_CLASS_WH_PER_MTOK,
    combine_accountings,
    energy_accounting,
    energy_constant_for_model,
    wh_per_mtok_for_model,
)

CATALOG = ModelCatalog()


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


def _local_model(**over) -> ModelInfo:
    return _model(
        "S",
        provider="local",
        id="local/test",
        cost_tier="local",
        input_price_per_mtok=Decimal("0"),
        output_price_per_mtok=Decimal("0"),
        **over,
    )


def _account(model: ModelInfo, settings: Settings | None = None, **kw) -> dict:
    tokens = kw.pop("tokens", (1_000_000, 200_000, 0, 0))
    return energy_accounting(
        model, *tokens, settings=settings or Settings(), catalog=CATALOG, **kw
    )


# ── strategy precedence ───────────────────────────────────────────────────────
def test_model_override_beats_the_explicit_catalog_constant():
    model = _model(energy_wh_per_mtok=Decimal("999"), active_params_b=70.0)
    factors = build_factor_set(
        provider=model.provider,
        model_id=model.id,
        workspace_settings={
            "model_overrides": {
                model.id: {
                    "energy_wh_per_mtok": 42.0,
                    "label": "metered on our own box",
                    "confidence": "measured",
                }
            }
        },
    )
    constant = energy_constant_for_model(model, factors)
    assert constant.wh_per_mtok == Decimal("42.0")
    assert constant.strategy == "measured"
    assert constant.confidence == "measured"
    assert constant.source == "metered on our own box"

    report = _account(model, factors=factors)
    record = next(f for f in report["factors"] if f["key"] == "energy_class")
    assert record["value"] == 42.0
    assert record["strategy"] == "measured"
    assert record["confidence"] == "measured"
    assert record["layer"] == "workspace"


def test_explicit_catalog_constant_beats_active_params_even_when_configured():
    # A catalog entry with BOTH an explicit energy_wh_per_mtok AND
    # active_params_b keeps today's behavior: the explicit field wins, exactly
    # as `wh_per_mtok_for_model` always has, regardless of what the run's
    # energy_strategy asks for.
    model = _model(energy_wh_per_mtok=Decimal("777"), active_params_b=70.0)
    factors = build_factor_set(
        provider=model.provider, run_overrides={"energy_strategy": "active_params"}
    )
    constant = energy_constant_for_model(model, factors)
    assert constant.wh_per_mtok == Decimal("777")
    assert constant.strategy == "catalog_override"

    report = _account(model, factors=factors)
    record = next(f for f in report["factors"] if f["key"] == "energy_class")
    assert record["value"] == 777.0
    assert record["strategy"] == "catalog_override"
    assert record["source"] == "Explicit model catalog energy constant"
    assert record["layer"] == "global_default"


def test_active_params_wins_over_the_ladder_when_strategy_asks_and_field_present():
    model = _model(active_params_b=70.0)
    factors = build_factor_set(
        provider=model.provider, run_overrides={"energy_strategy": "active_params"}
    )
    constant = energy_constant_for_model(model, factors)
    assert constant.strategy == "active_params"
    # "low", not "calibrated": EcoLogits' own f_E is per-GPU and GPU-energy
    # only — tret has neither a GPU count nor a server-energy term, so this
    # figure systematically understates the real thing. See
    # `_active_params_energy_constant` and the B1 tests below.
    assert constant.confidence == "low"
    assert constant.anchor == "EcoLogits"


def test_ladder_is_the_final_fallback_when_nothing_else_applies():
    model = _model()
    factors = build_factor_set(provider=model.provider)  # default strategy: class_ladder
    constant = energy_constant_for_model(model, factors)
    assert constant.strategy == "class_ladder_v2"
    assert constant.wh_per_mtok == wh_per_mtok_for_model(model)


# ── B2: the active_params branch, reached through a real catalog load ────────
# `ModelInfo.__post_init__` used to bake the class-ladder constant into
# `energy_wh_per_mtok` for EVERY model, so `_energy_constant_and_flags`'s
# "explicit constant wins" rung tested `energy_wh_per_mtok is not None` — true
# either way — and the active_params rung below it could never fire for a
# catalog-loaded model. Fixed with `ModelInfo.energy_wh_per_mtok_explicit`,
# set in `__post_init__` before the bake. These tests go through
# `ModelCatalog._load_static()` (a real yaml load), not a directly-constructed
# `ModelInfo`, so they defend the actual bug: a `models.yaml` entry, not a
# unit fed straight to the dataclass.
_YAML_TEMPLATE = """
models:
  - id: test/active-params-model
    provider: anthropic
    wire_id: test-active-params
    display_name: Test Active Params Model
    context_window: 200000
    input_price_per_mtok: 3.0
    output_price_per_mtok: 15.0
    cost_tier: standard
    energy_class: L
    active_params_b: 70
  - id: test/explicit-constant-model
    provider: anthropic
    wire_id: test-explicit-constant
    display_name: Test Explicit Constant Model
    context_window: 200000
    input_price_per_mtok: 3.0
    output_price_per_mtok: 15.0
    cost_tier: standard
    energy_class: L
    energy_wh_per_mtok: 777
    active_params_b: 70
  - id: test/no-active-params-model
    provider: anthropic
    wire_id: test-no-active-params
    display_name: Test No Active Params Model
    context_window: 200000
    input_price_per_mtok: 3.0
    output_price_per_mtok: 15.0
    cost_tier: standard
    energy_class: L
"""


def _catalog_from_yaml(tmp_path, monkeypatch) -> ModelCatalog:
    import tret.providers.catalog as catalog_module

    yaml_path = tmp_path / "models.yaml"
    yaml_path.write_text(_YAML_TEMPLATE)
    monkeypatch.setattr(catalog_module, "_MODELS_YAML", yaml_path)
    return ModelCatalog()


def test_catalog_loaded_model_with_active_params_b_and_no_explicit_constant_uses_the_formula(
    tmp_path, monkeypatch
):
    catalog = _catalog_from_yaml(tmp_path, monkeypatch)
    model = catalog.get("test/active-params-model")
    assert model.energy_wh_per_mtok_explicit is False
    assert model.active_params_b == 70.0

    factors = build_factor_set(
        provider=model.provider, run_overrides={"energy_strategy": "active_params"}
    )
    constant = energy_constant_for_model(model, factors)
    assert constant.strategy == "active_params"


def test_catalog_loaded_model_with_an_explicit_constant_wins_regardless_of_strategy(
    tmp_path, monkeypatch
):
    catalog = _catalog_from_yaml(tmp_path, monkeypatch)
    model = catalog.get("test/explicit-constant-model")
    assert model.energy_wh_per_mtok_explicit is True
    assert model.energy_wh_per_mtok == Decimal("777")

    factors = build_factor_set(
        provider=model.provider, run_overrides={"energy_strategy": "active_params"}
    )
    constant = energy_constant_for_model(model, factors)
    assert constant.strategy == "catalog_override"
    assert constant.wh_per_mtok == Decimal("777")


def test_catalog_loaded_model_with_no_active_params_b_fires_the_unknown_caveat(
    tmp_path, monkeypatch
):
    catalog = _catalog_from_yaml(tmp_path, monkeypatch)
    model = catalog.get("test/no-active-params-model")
    assert model.energy_wh_per_mtok_explicit is False
    assert model.active_params_b is None

    factors = build_factor_set(
        provider=model.provider, run_overrides={"energy_strategy": "active_params"}
    )
    report = _account(model, factors=factors)
    caveat = next(c for c in report["caveats"] if c["key"] == "active_params_unknown")
    assert caveat["applies"] is True


# ── the active-parameter formula (B1: EcoLogits' real published GPU model) ────
def _active_params_wh(active_params_b: float) -> Decimal:
    model = _model(active_params_b=active_params_b)
    factors = build_factor_set(
        provider=model.provider, run_overrides={"energy_strategy": "active_params"}
    )
    return energy_constant_for_model(model, factors).wh_per_mtok


def test_real_model_sizes_are_positive_and_monotone_in_active_params_b():
    # 7B/70B/175B/405B — real model-size ballpark figures, not the old
    # formula's degenerate floor-everything behavior.
    seven, seventy, one_seventy_five, four_oh_five = (
        _active_params_wh(7.0),
        _active_params_wh(70.0),
        _active_params_wh(175.0),
        _active_params_wh(405.0),
    )
    assert seven > 0
    assert seven < seventy < one_seventy_five < four_oh_five


def test_the_405b_figure_matches_the_published_constants_within_one_percent():
    # alpha=1.17e-6, beta=-1.12e-2, gamma=4.05e-5, B=64:
    #   alpha * exp(beta*B) * 405 + gamma, x1e6 Wh/Mtok ~= 271.9
    figure = float(_active_params_wh(405.0))
    assert figure == pytest.approx(271.9, rel=0.01)


def test_no_floor_exists_the_raw_formula_is_reported_even_when_small():
    # The misapplied form this replaced floored almost every real model at the
    # S-class constant (250 Wh/Mtok); the corrected form has no floor at all —
    # a small active-parameter count is reported as the (small, honestly low)
    # figure the formula actually yields, not clamped up to a class constant.
    figure = _active_params_wh(7.0)
    assert figure < ENERGY_CLASS_WH_PER_MTOK["S"]
    assert float(figure) == pytest.approx(44.5, rel=0.02)


def test_active_params_strategy_records_low_confidence_and_the_gpu_only_caveat():
    model = _model(active_params_b=70.0)
    factors = build_factor_set(
        provider=model.provider, run_overrides={"energy_strategy": "active_params"}
    )
    report = _account(model, factors=factors)
    record = next(f for f in report["factors"] if f["key"] == "energy_class")
    assert record["strategy"] == "active_params"
    assert record["confidence"] == "low"

    caveat = next(c for c in report["caveats"] if c["key"] == "active_params_gpu_only")
    assert caveat["applies"] is True
    assert caveat["direction"] == "understates"
    assert "GPU" in caveat["note"]

    # The missing-field caveat must NOT fire when the field was present — a
    # non-applying caveat is dropped from the list entirely (see
    # `caveat_records`'s return statement), not kept with `applies: False`.
    assert all(c["key"] != "active_params_unknown" for c in report["caveats"])


def test_missing_active_params_b_falls_back_to_the_ladder_with_a_caveat():
    model = _model()  # no active_params_b
    factors = build_factor_set(
        provider=model.provider, run_overrides={"energy_strategy": "active_params"}
    )
    constant = energy_constant_for_model(model, factors)
    assert constant.strategy == "class_ladder_v2"

    report = _account(model, factors=factors)
    caveat = next(c for c in report["caveats"] if c["key"] == "active_params_unknown")
    assert caveat["applies"] is True
    assert "active_params_b" in caveat["note"]

    # The strategy fell back to the ladder, so the GPU-only caveat (which
    # names a bias specific to the active-parameter formula actually pricing
    # the run) must not apply either — dropped from the list, not kept
    # `applies: False`.
    assert all(c["key"] != "active_params_gpu_only" for c in report["caveats"])


# ── measured energy ────────────────────────────────────────────────────────────
def test_measured_energy_replaces_the_estimate_and_composes_exactly():
    model = _local_model()
    settings = Settings(embodied_g_per_run=5.0)
    estimate = _account(model, settings)
    report = _account(model, settings, measured_energy_wh=1000.0)

    assert report["energy_wh"] == 1000.0
    assert report["energy_source"] == "measured"
    assert report["energy_wh_estimated"] == estimate["energy_wh"]
    bucket_sum = sum(report["energy_wh_by_bucket"].values())
    assert bucket_sum == pytest.approx(1000.0, abs=1e-3)

    # pue=1.05 (workstation default), grid=458.49 (global default), embodied=5.0
    expected_co2e = 1000.0 * 1.05 * 458.49 / 1000.0 + 5.0
    assert report["co2e_g"] == pytest.approx(expected_co2e, abs=1e-3)
    scopes = report["scopes"]
    assert (
        scopes["scope1_g"] + scopes["scope2_g"] + scopes["scope3_g"]
        == pytest.approx(report["co2e_g"], abs=1e-6)
    )


def test_unvalidated_measurement_does_not_claim_instrument_accuracy():
    model = _local_model()
    report = _account(model, measured_energy_wh=500.0)

    caveats = {c["key"]: c for c in report["caveats"]}
    assert caveats["unbatched_local_inference"]["applies"] is False
    assert "measured" in caveats["unbatched_local_inference"]["note"].lower()
    assert caveats["prompt_shape_residual"]["applies"] is False
    assert "measured" in caveats["prompt_shape_residual"]["note"].lower()

    contributions = {c["key"]: c for c in report["uncertainty"]["contributions"]}
    assert contributions["energy_class"]["low_multiplier"] == 0.33
    assert contributions["energy_class"]["high_multiplier"] == 3.0
    assert contributions["batching"]["low_multiplier"] == 0.55
    assert contributions["batching"]["high_multiplier"] == 1.45

    record = next(f for f in report["factors"] if f["key"] == "energy_class")
    assert record["confidence"] == "measured"
    assert record["source"] == "Measured energy (node_it boundary Wh)"
    assert report["energy_boundary"] == "node_it"
    assert record["measured"] is True


def test_additional_reasoning_changes_energy_without_repricing_billed_output():
    model = _model()
    plain = energy_accounting(model, 100, 200)
    extra = energy_accounting(model, 100, 200, energy_output_tokens=300,
                              reasoning_tokens=100, reasoning_accounting="additional")
    included = energy_accounting(model, 100, 200, energy_output_tokens=200,
                                 reasoning_tokens=100, reasoning_accounting="counted_in_output")
    assert extra["energy_wh"] > plain["energy_wh"]
    assert included["energy_wh"] == plain["energy_wh"]
    assert extra["cost"] == plain["cost"]
    assert extra["tokens"] == plain["tokens"]
    assert extra["energy_output_tokens"] == 300
    assert any(c["key"] == "reasoning_counted_in_output" for c in included["caveats"])
    assert not any(c["key"] == "reasoning_hidden" for c in included["caveats"])


def test_a_cloud_run_keeps_its_own_caveats_when_measured():
    # unbatched_local_inference never applied to a cloud run in the first
    # place, so measuring it must not fabricate a caveat that was never there.
    model = _model()
    report = _account(model, measured_energy_wh=10.0)
    assert all(c["key"] != "unbatched_local_inference" for c in report["caveats"])
    caveats = {c["key"]: c for c in report["caveats"]}
    assert caveats["prompt_shape_residual"]["applies"] is False


def test_negative_measured_energy_raises():
    model = _model()
    with pytest.raises(ValueError):
        _account(model, measured_energy_wh=-1.0)


def test_measured_energy_with_zero_estimated_tokens_does_not_divide_by_zero():
    model = _model()
    report = energy_accounting(
        model, 0, 0, settings=Settings(), catalog=CATALOG, measured_energy_wh=10.0
    )
    assert report["energy_wh"] == 10.0
    assert sum(report["energy_wh_by_bucket"].values()) == pytest.approx(10.0)


# ── schema ─────────────────────────────────────────────────────────────────────
def test_model_override_without_a_label_is_rejected():
    with pytest.raises(ValidationError) as exc:
        EmissionsOverrides(
            model_overrides={"anthropic/test": {"energy_wh_per_mtok": 100.0}}
        )
    locs = {".".join(str(p) for p in e["loc"]) for e in exc.value.errors()}
    assert any("model_overrides" in loc and "label" in loc for loc in locs)


def test_an_unknown_energy_strategy_is_rejected():
    with pytest.raises(ValidationError):
        EmissionsOverrides(energy_strategy="not_a_real_strategy")


def test_a_workspace_model_override_wins_over_the_explicit_catalog_constant():
    model = _model(energy_wh_per_mtok=Decimal("999"))
    factors = build_factor_set(
        provider=model.provider,
        model_id=model.id,
        workspace_settings={
            "model_overrides": {
                model.id: {"energy_wh_per_mtok": 55.0, "label": "workspace metering"}
            }
        },
    )
    report = _account(model, factors=factors)
    record = next(f for f in report["factors"] if f["key"] == "energy_class")
    assert record["value"] == 55.0
    assert record["layer"] == "workspace"
    assert record["strategy"] == "measured"


# ── combine_accountings ────────────────────────────────────────────────────────
def test_combine_accountings_reports_mixed_energy_source():
    a = _model(provider="anthropic")
    b = _model(provider="kimi")
    measured_block = _account(a, measured_energy_wh=100.0)
    estimated_block = _account(b)

    combined = combine_accountings([measured_block, estimated_block])
    assert combined["energy_source"] == "mixed"

    both_measured = combine_accountings(
        [_account(a, measured_energy_wh=100.0), _account(b, measured_energy_wh=50.0)]
    )
    assert both_measured["energy_source"] == "measured"

    both_estimated = combine_accountings([_account(a), _account(b)])
    assert both_estimated["energy_source"] == "estimated"
