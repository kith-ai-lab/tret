"""Emissions accounting: PUE, GHG Protocol scopes, the baseline counterfactual.

No network and no DB. The accounting functions take an explicit `settings` (and
optional `catalog`), so every case here is driven by construction rather than by
patching globals; the endpoint tests use the fake-session pattern from
test_analytics.py.

What these tests defend is honesty, not the specific heuristics
(docs/emissions-methodology.md owns those): the scope total always equals the sum
of its parts, cloud and self-hosted land in different scopes, PUE inflates the
total and never the compute figure, the counterfactual is allowed to be negative,
missing estimates stay null instead of becoming zero, and window rollups are
sums of stored figures rather than recomputations at today's settings.
"""
from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bench.api import analytics
from bench.api.analytics import _recorded_emissions, emissions
from bench.api.runs import _run_summary
from bench.config import Settings
from bench.db.models import Run, utcnow
from bench.providers.catalog import ModelCatalog, ModelInfo
from bench.services.emissions import (
    DEPLOYMENT_CLOUD,
    DEPLOYMENT_LOCAL,
    co2e_grams,
    deployment_for,
    embodied_g_for,
    emission_event_fields,
    emission_summary_fields,
    energy_accounting,
    grid_factor_for,
    pue_for,
    resolve_baseline_model,
    scope_split,
)

CATALOG = ModelCatalog()


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


def _local_model(energy_class: str = "S") -> ModelInfo:
    return _model(
        energy_class,
        provider="local",
        id="local/qwen2.5:14b",
        cost_tier="local",
        input_price_per_mtok=Decimal("0"),
        output_price_per_mtok=Decimal("0"),
    )


def _account(model: ModelInfo, settings: Settings | None = None, **kw) -> dict:
    """One million input tokens through `model`, unless overridden."""
    tokens = kw.pop("tokens", (1_000_000, 0, 0, 0))
    return energy_accounting(
        model, *tokens, settings=settings or _settings(), catalog=CATALOG, **kw
    )


def _scope_sum(report: dict) -> Decimal:
    s = report["scopes"]
    return (
        Decimal(str(s["scope1_g"])) + Decimal(str(s["scope2_g"])) + Decimal(str(s["scope3_g"]))
    )


# ── the invariant: the total is the sum of its scopes ─────────────────────────
@pytest.mark.parametrize(
    "model, settings",
    [
        (_model("XL"), _settings()),
        (_model("S"), _settings(grid_co2e_g_per_kwh=723.4, datacenter_pue=1.37)),
        (_local_model(), _settings()),
        (_local_model(), _settings(embodied_g_per_run=12.5, local_grid_co2e_g_per_kwh=41.0)),
        (_local_model(), _settings(embodied_g_per_run=0.000003)),
    ],
)
def test_run_total_always_equals_scope1_plus_scope2_plus_scope3(model, settings):
    report = _account(model, settings, tokens=(123_457, 9_871, 55_555, 3_333))
    assert Decimal(str(report["co2e_g"])) == _scope_sum(report)
    assert report["co2e_g"] == pytest.approx(
        report["scopes"]["scope1_g"]
        + report["scopes"]["scope2_g"]
        + report["scopes"]["scope3_g"]
    )


def test_zero_tokens_is_zero_carbon_in_every_scope():
    report = _account(_model("XL"), tokens=(0, 0, 0, 0))
    assert report["co2e_g"] == 0.0
    assert _scope_sum(report) == 0


# ── scope 1 ──────────────────────────────────────────────────────────────────
def test_scope1_is_always_an_explicit_zero_with_an_explanation():
    for model in (_model("L"), _local_model()):
        report = _account(model)
        assert report["scopes"]["scope1_g"] == 0.0  # present, not omitted
        assert "Scope 1 = 0" in report["scopes"]["basis"]
        assert "on-site" in report["scopes"]["basis"]


# ── cloud vs self-hosted ─────────────────────────────────────────────────────
def test_deployment_split_is_only_about_who_buys_the_power():
    assert deployment_for("local") == DEPLOYMENT_LOCAL
    for provider in ("anthropic", "kimi", "openrouter", "something-new"):
        assert deployment_for(provider) == DEPLOYMENT_CLOUD


def test_cloud_inference_is_entirely_scope_3():
    report = _account(_model("L"))
    assert report["deployment"] == "cloud"
    assert report["scopes"]["scope2_g"] == 0.0
    assert report["scopes"]["scope3_g"] == report["co2e_g"] > 0
    assert "purchased goods and services" in report["scopes"]["basis"]


def test_self_hosted_electricity_is_scope_2():
    settings = _settings()
    report = _account(_local_model(), settings)
    assert report["deployment"] == "local"
    # 1 Mtok at S (50 Wh) x local PUE 1.05 = 52.5 Wh, at 400 g/kWh = 21 g.
    assert report["energy_wh"] == 50.0
    assert report["energy_wh_total"] == 52.5
    assert report["scopes"]["scope2_g"] == pytest.approx(21.0)
    assert report["scopes"]["scope3_g"] == 0.0  # nothing embodied by default
    assert report["co2e_g"] == report["scopes"]["scope2_g"]


def test_a_local_operator_may_set_their_own_grid_factor():
    assert grid_factor_for(DEPLOYMENT_LOCAL, _settings(grid_co2e_g_per_kwh=400.0)) == 400.0
    settings = _settings(grid_co2e_g_per_kwh=400.0, local_grid_co2e_g_per_kwh=30.0)
    assert grid_factor_for(DEPLOYMENT_LOCAL, settings) == 30.0
    # Cloud keeps the general factor: bench does not know which region served it.
    assert grid_factor_for(DEPLOYMENT_CLOUD, settings) == 400.0

    report = _account(_local_model(), settings)
    assert report["grid_co2e_g_per_kwh"] == 30.0
    assert report["scopes"]["scope2_g"] == pytest.approx(1.575)  # 52.5 Wh at 30 g/kWh


def test_an_explicit_grid_override_still_wins_over_both_settings():
    settings = _settings(grid_co2e_g_per_kwh=400.0, local_grid_co2e_g_per_kwh=30.0)
    report = _account(_local_model(), settings, grid_g_per_kwh=700.0)
    assert report["grid_co2e_g_per_kwh"] == 700.0


# ── PUE ──────────────────────────────────────────────────────────────────────
def test_pue_inflates_the_total_and_never_the_compute_figure():
    settings = _settings(datacenter_pue=1.5)
    report = _account(_model("M"), settings)
    # energy_wh keeps its original meaning: compute / IT load only.
    assert report["energy_wh"] == 300.0
    assert report["pue"] == 1.5
    assert report["energy_wh_total"] == 450.0
    # Carbon comes off the *total*, not the compute figure.
    assert report["co2e_g"] == pytest.approx(float(co2e_grams(Decimal("450"), 400.0)))
    assert report["co2e_g"] != pytest.approx(float(co2e_grams(Decimal("300"), 400.0)))


def test_defaults_are_the_documented_heuristics():
    settings = _settings()
    assert settings.datacenter_pue == 1.2  # inside the hyperscaler-reported 1.1–1.2 band
    assert settings.local_pue == 1.05  # a desktop has almost no facility overhead
    assert settings.embodied_g_per_run == 0.0
    assert settings.local_grid_co2e_g_per_kwh is None
    assert settings.emissions_baseline_model == ""
    assert pue_for(DEPLOYMENT_CLOUD, settings) == Decimal("1.2")
    assert pue_for(DEPLOYMENT_LOCAL, settings) == Decimal("1.05")


def test_a_pue_below_one_is_refused_rather_than_shrinking_the_number():
    # Using less than the IT load is not physically possible; a typo must not
    # produce a flattering figure.
    assert pue_for(DEPLOYMENT_CLOUD, _settings(datacenter_pue=0.5)) == Decimal(1)
    assert pue_for(DEPLOYMENT_LOCAL, _settings(local_pue=0.0)) == Decimal(1)


# ── embodied hardware ────────────────────────────────────────────────────────
def test_embodied_carbon_defaults_to_zero_and_only_applies_to_local():
    assert embodied_g_for(DEPLOYMENT_LOCAL, _settings()) == Decimal(0)
    settings = _settings(embodied_g_per_run=8.0)
    assert embodied_g_for(DEPLOYMENT_LOCAL, settings) == Decimal("8.0")
    # Cloud hardware is inside the purchased service, not the operator's Cat. 2.
    assert embodied_g_for(DEPLOYMENT_CLOUD, settings) == Decimal(0)
    assert _account(_model("L"), settings)["embodied_g"] == 0.0


def test_configured_embodied_carbon_lands_in_scope_3_for_local_runs():
    report = _account(_local_model(), _settings(embodied_g_per_run=8.0))
    assert report["embodied_g"] == 8.0
    assert report["scopes"]["scope2_g"] == pytest.approx(21.0)  # electricity
    assert report["scopes"]["scope3_g"] == 8.0  # capital goods
    assert Decimal(str(report["co2e_g"])) == _scope_sum(report)
    assert "capital goods" in report["scopes"]["basis"]


def test_negative_embodied_setting_cannot_subtract_carbon():
    assert embodied_g_for(DEPLOYMENT_LOCAL, _settings(embodied_g_per_run=-100.0)) == Decimal(0)


# ── the baseline counterfactual ──────────────────────────────────────────────
def test_baseline_auto_selects_the_heaviest_curated_cloud_model_deterministically():
    settings = _settings()
    chosen = resolve_baseline_model(settings, CATALOG)
    heaviest = max(
        m.energy_wh_per_mtok for m in CATALOG.all(curated_only=True) if m.provider != "local"
    )
    assert chosen.energy_wh_per_mtok == heaviest
    assert chosen.provider != "local"
    # Deterministic across calls: ties break on model id.
    assert [resolve_baseline_model(settings, CATALOG).id for _ in range(3)] == [chosen.id] * 3
    tied = sorted(
        m.id for m in CATALOG.all(curated_only=True) if m.energy_wh_per_mtok == heaviest
    )
    assert chosen.id == tied[0]


def test_a_configured_baseline_is_used_verbatim():
    settings = _settings(emissions_baseline_model="anthropic/claude-haiku-4-5")
    assert resolve_baseline_model(settings, CATALOG).id == "anthropic/claude-haiku-4-5"
    report = _account(_model("XL"), settings)
    assert report["baseline"]["model"] == "anthropic/claude-haiku-4-5"


def test_an_unknown_baseline_reports_nothing_rather_than_substituting_one():
    report = _account(_model("L"), _settings(emissions_baseline_model="nope/not-a-model"))
    baseline = report["baseline"]
    assert baseline["model"] is None
    assert baseline["co2e_g"] is None
    assert baseline["avoided_co2e_g"] is None  # null, not 0
    assert "not a comparison that came out even" in baseline["basis"]


def test_baseline_is_a_same_token_counterfactual_and_says_so():
    report = _account(_model("M"), _settings(emissions_baseline_model="anthropic/claude-fable-5"))
    baseline = report["baseline"]
    # Same tokens, XL class: 1 Mtok = 3000 Wh compute, x1.2 PUE, at 400 g/kWh.
    assert baseline["energy_class"] == "XL"
    assert baseline["energy_wh"] == 3000.0
    assert baseline["co2e_g"] == pytest.approx(1440.0)
    assert baseline["avoided_co2e_g"] == pytest.approx(1440.0 - report["co2e_g"])
    assert baseline["avoided_pct"] == pytest.approx(90.0)
    for phrase in ("not an offset", "efficiency indicator", "not usable for statutory"):
        assert phrase in baseline["basis"]


def test_a_heavier_than_baseline_run_reports_negative_avoided_carbon():
    # Cheap baseline, expensive run: honest arithmetic gives a negative figure,
    # and clamping it to zero would be a greenwash.
    settings = _settings(emissions_baseline_model="anthropic/claude-haiku-4-5")
    report = _account(_model("XL"), settings)
    baseline = report["baseline"]
    assert baseline["co2e_g"] < report["co2e_g"]
    assert baseline["avoided_co2e_g"] < 0
    assert baseline["avoided_pct"] < 0
    assert baseline["avoided_co2e_g"] == pytest.approx(baseline["co2e_g"] - report["co2e_g"])


def test_a_run_on_the_baseline_model_avoided_nothing():
    settings = _settings(emissions_baseline_model="anthropic/claude-haiku-4-5")
    model = CATALOG.get("anthropic/claude-haiku-4-5")
    report = _account(model, settings)
    baseline = report["baseline"]
    assert baseline["model"] == model.id
    assert baseline["avoided_co2e_g"] == 0.0
    assert baseline["avoided_pct"] == 0.0
    assert baseline["co2e_g"] == report["co2e_g"]
    assert "baseline model itself" in baseline["basis"]


def test_a_local_run_is_compared_against_a_cloud_baseline_including_embodied():
    settings = _settings(
        emissions_baseline_model="anthropic/claude-fable-5", embodied_g_per_run=5.0
    )
    report = _account(_local_model(), settings)
    baseline = report["baseline"]
    # The run's own total carries the embodied grams; the cloud baseline does not.
    assert report["embodied_g"] == 5.0
    assert baseline["co2e_g"] == pytest.approx(1440.0)
    assert baseline["avoided_co2e_g"] == pytest.approx(1440.0 - report["co2e_g"])


# ── the existing contract ────────────────────────────────────────────────────
def test_every_original_key_survives_with_its_original_meaning():
    report = _account(_model("L"), tokens=(100_000, 10_000, 800_000, 100_000))
    assert report["estimated"] is True
    assert report["model"] == "anthropic/test"
    assert report["energy_class"] == "L"
    assert report["energy_wh_per_mtok"] == 1200.0
    assert report["weighted_tokens"] == 290_000.0
    assert report["cache_read_weight"] == 0.1
    assert report["cache_write_weight"] == 1.0
    assert report["energy_wh"] == 348.0  # compute only, exactly as before
    assert report["grid_co2e_g_per_kwh"] == 400.0
    assert "estimate, not a measurement" in report["basis"]
    # co2e_g is still the run total — now PUE-inclusive and scope-decomposed.
    assert report["co2e_g"] == pytest.approx(139.2 * 1.2)


def test_the_block_is_json_safe_all_the_way_down():
    report = _account(_local_model(), _settings(embodied_g_per_run=1.0))

    def _check(value):
        if isinstance(value, dict):
            for k, v in value.items():
                assert isinstance(k, str)
                _check(v)
        else:
            assert value is None or isinstance(value, (bool, int, float, str)), value

    _check(report)


def test_scope_split_is_callable_on_its_own():
    cloud = scope_split(DEPLOYMENT_CLOUD, Decimal("10"), Decimal("0"))
    assert (cloud["scope1_g"], cloud["scope2_g"], cloud["scope3_g"]) == (0.0, 0.0, 10.0)
    local = scope_split(DEPLOYMENT_LOCAL, Decimal("10"), Decimal("2"))
    assert (local["scope1_g"], local["scope2_g"], local["scope3_g"]) == (0.0, 10.0, 2.0)


# ── reading a stored block back: nulls stay null ─────────────────────────────
def test_summary_and_event_fields_are_null_when_there_is_no_estimate():
    for accounting in (None, {}, {"estimated": True}):
        summary = emission_summary_fields(accounting)
        assert summary == {
            "co2e_g": None,
            "scope2_g": None,
            "scope3_g": None,
            "avoided_co2e_g": None,
        }
        event = emission_event_fields(accounting)
        assert set(event) == {
            "co2e_g",
            "scope2_g",
            "scope3_g",
            "baseline_co2e_g",
            "avoided_co2e_g",
        }
        assert all(v is None for v in event.values())


def test_summary_and_event_fields_read_the_stored_values():
    report = _account(_model("L"))
    assert emission_summary_fields(report) == {
        "co2e_g": report["co2e_g"],
        "scope2_g": report["scopes"]["scope2_g"],
        "scope3_g": report["scopes"]["scope3_g"],
        "avoided_co2e_g": report["baseline"]["avoided_co2e_g"],
    }
    assert emission_event_fields(report)["baseline_co2e_g"] == report["baseline"]["co2e_g"]


def _run(**over) -> Run:
    base = dict(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        harness_id=uuid.uuid4(),
        task_type="divergence_assessment",
        task_input={},
        document_ids=[],
        status="completed",
        messages=[],
        iterations=3,
    )
    base.update(over)
    return Run(**base)


def test_run_summary_carries_the_scope_split_and_the_counterfactual():
    report = _account(_model("L"))
    summary = _run_summary(
        _run(cost_usd=Decimal("0.4"), energy_wh=Decimal("1200"), energy_accounting=report)
    )
    assert summary["co2e_g"] == report["co2e_g"]
    assert summary["scope2_g"] == 0.0  # a cloud run really did buy no power
    assert summary["scope3_g"] == report["co2e_g"]
    assert summary["avoided_co2e_g"] == report["baseline"]["avoided_co2e_g"]


def test_run_summary_keeps_missing_estimates_null():
    summary = _run_summary(_run(cost_usd=Decimal("0.4")))
    for key in ("energy_wh", "co2e_g", "scope2_g", "scope3_g", "avoided_co2e_g"):
        assert summary[key] is None, key


def test_a_run_recorded_before_scopes_existed_reports_null_not_zero():
    legacy = {"estimated": True, "energy_wh": 10.0, "co2e_g": 4.0, "grid_co2e_g_per_kwh": 400}
    summary = _run_summary(_run(energy_wh=Decimal("10"), energy_accounting=legacy))
    assert summary["co2e_g"] == 4.0
    assert summary["scope2_g"] is None
    assert summary["scope3_g"] is None
    assert summary["avoided_co2e_g"] is None


# ── the /emissions rollup ────────────────────────────────────────────────────
class _EmissionRows:
    """Returns run rows for the first query, harness names for the rest."""

    def __init__(self, rows):
        self.rows = rows
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _Result(self.rows if len(self.statements) == 1 else [])


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows

    def scalars(self):
        return self

    def unique(self):
        return self


def _row(accounting, harness_id=None, model_used="anthropic/test", at=None):
    return (harness_id or uuid.uuid4(), model_used, accounting, at or utcnow())


async def test_rollup_sums_stored_values_and_is_not_recomputed(monkeypatch):
    """The flaw this endpoint exists to avoid: totals at *today's* factors.

    Two runs are recorded at 400 gCO2e/kWh. The operator then corrects the
    setting to 30. The window's totals must not move — they are history.
    """
    recorded = _settings(grid_co2e_g_per_kwh=400.0)
    reports = [
        _account(_model("L"), recorded),
        _account(_model("M"), recorded),
    ]
    expected_co2e = sum(r["co2e_g"] for r in reports)

    monkeypatch.setattr(analytics, "get_settings", lambda: _settings(grid_co2e_g_per_kwh=30.0))
    db = _EmissionRows([_row(reports[0]), _row(reports[1])])
    out = await emissions(project_id=None, days=30, user=None, db=db)

    assert out["totals"]["co2e_g"] == pytest.approx(expected_co2e)
    assert out["totals"]["runs_with_estimate"] == 2
    # Same grid factor on both runs, so the window has one honest basis...
    assert out["factors"]["mixed_factors"] is False
    # ...even though the *current* setting no longer matches what was recorded.
    assert out["factors"]["grid_co2e_g_per_kwh"] == 30.0
    assert out["factors"]["recorded"] == [
        {"deployment": "cloud", "grid_co2e_g_per_kwh": 400.0, "pue": 1.2, "runs": 2}
    ]
    assert "for reference only" in out["factors"]["note"]


async def test_rollup_flags_a_window_recorded_under_differing_factors():
    cloud = _account(_model("L"), _settings(grid_co2e_g_per_kwh=400.0))
    greener = _account(_model("L"), _settings(grid_co2e_g_per_kwh=30.0))
    out = await emissions(
        project_id=None, days=30, user=None, db=_EmissionRows([_row(cloud), _row(greener)])
    )
    assert out["factors"]["mixed_factors"] is True
    assert "MIXES RECORDING BASES" in out["disclaimer"]
    assert len(out["factors"]["recorded"]) == 2


async def test_a_window_mixing_cloud_and_self_hosted_is_also_flagged_and_split():
    settings = _settings()
    cloud = _account(_model("L"), settings)
    local = _account(_local_model(), settings)
    out = await emissions(
        project_id=None, days=30, user=None, db=_EmissionRows([_row(cloud), _row(local)])
    )
    totals = out["totals"]
    assert totals["scope1_g"] == 0.0
    assert totals["scope2_g"] == pytest.approx(local["scopes"]["scope2_g"])
    assert totals["scope3_g"] == pytest.approx(cloud["scopes"]["scope3_g"])
    assert totals["co2e_g"] == pytest.approx(
        totals["scope1_g"] + totals["scope2_g"] + totals["scope3_g"]
    )
    # Cloud and local use different factors by design, so the window has no
    # single factor behind it and says so.
    assert out["factors"]["mixed_factors"] is True


async def test_runs_without_an_estimate_are_excluded_and_counted():
    report = _account(_model("L"))
    db = _EmissionRows([_row(report), _row(None), _row({"estimated": True}), _row("junk")])
    out = await emissions(project_id=None, days=30, user=None, db=db)
    totals = out["totals"]
    assert totals["runs"] == 4
    assert totals["runs_with_estimate"] == 1
    assert totals["runs_without_estimate"] == 3
    assert totals["co2e_g"] == pytest.approx(report["co2e_g"])
    assert totals["energy_wh"] == pytest.approx(report["energy_wh_total"])
    assert totals["energy_wh_compute"] == pytest.approx(report["energy_wh"])


async def test_legacy_runs_keep_their_carbon_but_not_a_fabricated_scope_split():
    legacy = {"estimated": True, "energy_wh": 10.0, "co2e_g": 4.0, "grid_co2e_g_per_kwh": 400}
    out = await emissions(project_id=None, days=30, user=None, db=_EmissionRows([_row(legacy)]))
    totals = out["totals"]
    assert totals["co2e_g"] == 4.0
    assert totals["runs_without_scope_split"] == 1
    assert totals["runs_without_baseline"] == 1
    assert (totals["scope1_g"], totals["scope2_g"], totals["scope3_g"]) == (0.0, 0.0, 0.0)
    assert totals["baseline_co2e_g"] == 0.0
    assert "predate it" in out["disclaimer"]
    # A row with no PUE recorded contributes its compute figure, not nothing.
    assert totals["energy_wh"] == 10.0


async def test_rollup_groups_by_model_harness_and_day():
    settings = _settings()
    heavy = _account(_model("XL", id="anthropic/heavy"), settings)
    light = _account(_model("M", id="anthropic/light"), settings)
    harness = uuid.uuid4()
    yesterday = utcnow() - timedelta(days=1)
    db = _EmissionRows(
        [
            _row(heavy, harness_id=harness, at=yesterday),
            _row(light, harness_id=harness, at=utcnow()),
        ]
    )
    out = await emissions(project_id=None, days=30, user=None, db=db)

    assert [m["model"] for m in out["by_model"]] == ["anthropic/heavy", "anthropic/light"]
    assert out["by_model"][0]["energy_class"] == "XL"
    assert out["by_model"][0]["co2e_g"] == pytest.approx(heavy["co2e_g"])
    assert out["by_model"][0]["avoided_co2e_g"] == pytest.approx(
        heavy["baseline"]["avoided_co2e_g"]
    )
    assert len(out["by_harness"]) == 1
    assert out["by_harness"][0]["runs"] == 2
    assert out["by_harness"][0]["harness_name"] == "(deleted harness)"
    assert [d["date"] for d in out["by_day"]] == sorted(d["date"] for d in out["by_day"])
    assert len(out["by_day"]) == 2


async def test_avoided_pct_is_signed_and_safe_when_there_is_no_baseline():
    empty = await emissions(project_id=None, days=30, user=None, db=_EmissionRows([]))
    assert empty["totals"] == {
        "runs": 0,
        "runs_with_estimate": 0,
        "runs_without_estimate": 0,
        "runs_without_scope_split": 0,
        "runs_without_baseline": 0,
        "energy_wh": 0.0,
        "energy_wh_compute": 0.0,
        "co2e_g": 0.0,
        "scope1_g": 0.0,
        "scope2_g": 0.0,
        "scope3_g": 0.0,
        "baseline_co2e_g": 0.0,
        "avoided_co2e_g": 0.0,
        "avoided_pct": 0.0,
    }
    heavier = _account(_model("XL"), _settings(emissions_baseline_model="anthropic/claude-haiku-4-5"))
    out = await emissions(project_id=None, days=30, user=None, db=_EmissionRows([_row(heavier)]))
    assert out["totals"]["avoided_co2e_g"] < 0
    assert out["totals"]["avoided_pct"] < 0


async def test_rollup_declares_its_scan_bound_and_never_claims_measurement():
    out = await emissions(project_id=None, days=7, user=None, db=_EmissionRows([]))
    assert out["window_days"] == 7
    assert out["estimated"] is True
    assert out["scan"] == {
        "limit": analytics.EMISSIONS_RUN_SCAN_LIMIT,
        "rows_scanned": 0,
        "truncated": False,
    }
    for phrase in (
        "ESTIMATES, NOT MEASUREMENTS",
        "NOT recomputed at current settings",
        "not an offset",
        "not usable for statutory",
    ):
        assert phrase in out["disclaimer"]


async def test_rollup_query_is_bounded_and_filtered_without_json_predicates():
    from sqlalchemy.dialects import postgresql

    db = _EmissionRows([])
    await emissions(project_id=uuid.uuid4(), days=30, user=None, db=db)
    compiled = str(db.statements[0].compile(dialect=postgresql.dialect()))
    assert "LIMIT" in compiled
    assert "runs.created_at >=" in compiled
    assert "runs.project_id =" in compiled
    assert "->" not in compiled  # no dialect-specific JSON access


def test_recorded_reader_ignores_anything_that_is_not_a_block():
    for junk in (None, "nope", 42, [], {}, {"co2e_g": None}):
        assert _recorded_emissions(junk) is None


# ── auth ─────────────────────────────────────────────────────────────────────
def test_emissions_requires_authentication():
    app = FastAPI()
    app.include_router(analytics.router)
    client = TestClient(app)
    assert client.get("/api/analytics/emissions").status_code == 401


def test_emissions_is_readable_by_any_authenticated_user():
    from bench.api.auth import current_user
    from bench.db.engine import get_db

    app = FastAPI()
    app.include_router(analytics.router)
    app.dependency_overrides[current_user] = lambda: None
    app.dependency_overrides[get_db] = lambda: _EmissionRows([])
    client = TestClient(app)
    body = client.get("/api/analytics/emissions?days=7").json()
    assert body["window_days"] == 7
    assert body["totals"]["runs"] == 0
