"""Estimated energy/carbon accounting: the model, the math, and its exposure.

No network and no DB: the catalog's energy model is exercised directly, catalog
discovery is driven through a fake httpx client (as in test_local_provider.py),
and the runs API serializers are called with detached ORM objects.

The numbers here are heuristics by design (docs/eco-accounting.md); what these
tests lock down is that the heuristic is applied consistently, that discounts
match the cost model, and above all that nothing is ever silently zero.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import httpx
import yaml

from bench.api.runs import _run_summary, get_run
from bench.config import Settings
from bench.db.models import Run
from bench.providers import catalog as catalog_module
from bench.providers.catalog import (
    DEFAULT_ENERGY_CLASS,
    ENERGY_CACHE_READ_MULTIPLIER,
    ENERGY_CACHE_WRITE_MULTIPLIER,
    ENERGY_CLASS_WH_PER_MTOK,
    ENERGY_CLASSES,
    ModelCatalog,
    ModelInfo,
    co2e_grams,
    energy_accounting,
    energy_class_for_tier,
    wh_per_mtok_for_class,
)
from bench.services.export import _deliverable_footprint, _energy_cell

MODELS_YAML = catalog_module._MODELS_YAML


def _model(energy_class: str = "L", **over) -> ModelInfo:
    base = dict(
        id="anthropic/test",
        provider="anthropic",
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


def _settings(**over) -> Settings:
    return Settings(**over)


def _patch_settings(monkeypatch, settings: Settings) -> None:
    monkeypatch.setattr(catalog_module, "get_settings", lambda: settings)


# ── the class table ───────────────────────────────────────────────────────────
def test_energy_classes_are_ordered_powers_of_scale():
    assert ENERGY_CLASSES == ("S", "M", "L", "XL")
    values = [ENERGY_CLASS_WH_PER_MTOK[c] for c in ENERGY_CLASSES]
    assert values == [Decimal("50"), Decimal("300"), Decimal("1200"), Decimal("3000")]
    assert values == sorted(values)
    assert all(v > 0 for v in values)


def test_unknown_class_falls_back_to_the_default_not_to_zero():
    assert wh_per_mtok_for_class("nonsense") == ENERGY_CLASS_WH_PER_MTOK[DEFAULT_ENERGY_CLASS]
    assert _model(energy_class="nonsense").energy_class == DEFAULT_ENERGY_CLASS


def test_tier_defaults_cover_every_tier_and_are_never_zero():
    assert energy_class_for_tier("local") == "S"
    assert energy_class_for_tier("economy") == "M"
    assert energy_class_for_tier("standard") == "L"
    assert energy_class_for_tier("premium") == "XL"
    assert energy_class_for_tier("something-new") == DEFAULT_ENERGY_CLASS
    for tier in ("local", "economy", "standard", "premium", "something-new"):
        assert wh_per_mtok_for_class(energy_class_for_tier(tier)) > 0


def test_class_derives_wh_per_mtok_unless_overridden():
    assert _model(energy_class="S").energy_wh_per_mtok == Decimal("50")
    measured = _model(energy_class="S", energy_wh_per_mtok=Decimal("7.5"))
    assert measured.energy_wh_per_mtok == Decimal("7.5")  # a real measurement wins


# ── per-turn math ─────────────────────────────────────────────────────────────
def test_a_million_tokens_costs_the_class_figure():
    assert _model("L").energy_wh(1_000_000, 0) == Decimal("1200")
    assert _model("L").energy_wh(0, 1_000_000) == Decimal("1200")


def test_cache_reads_are_discounted_like_price_but_not_free():
    model = _model("L")
    assert ENERGY_CACHE_READ_MULTIPLIER == Decimal("0.1")
    assert model.energy_wh(0, 0, cache_read_tokens=1_000_000) == Decimal("120")
    assert model.energy_wh(0, 0, cache_read_tokens=1_000_000) > 0


def test_cache_writes_are_a_full_forward_pass():
    model = _model("L")
    assert ENERGY_CACHE_WRITE_MULTIPLIER == Decimal("1")
    assert model.energy_wh(0, 0, cache_write_tokens=1_000_000) == model.energy_wh(1_000_000, 0)


def test_a_warm_cache_read_beats_re_sending_the_prefix():
    model = _model("L")
    assert model.energy_wh(0, 0, cache_read_tokens=500_000) < model.energy_wh(500_000, 0)


def test_energy_sums_all_four_buckets():
    model = _model("M")
    energy = model.energy_wh(100_000, 10_000, 800_000, 100_000)
    weighted = 100_000 + 10_000 + Decimal("0.1") * 800_000 + 100_000
    assert energy == Decimal("300") * weighted / Decimal(1_000_000)


def test_no_tokens_no_energy():
    assert _model("XL").energy_wh(0, 0, 0, 0) == Decimal(0)


# ── carbon ────────────────────────────────────────────────────────────────────
def test_co2e_uses_the_configured_grid_intensity():
    assert co2e_grams(Decimal("1000"), grid_g_per_kwh=400.0) == Decimal("400")
    assert co2e_grams(Decimal("1000"), grid_g_per_kwh=30.0) == Decimal("30")


def test_grid_intensity_defaults_to_the_world_average_setting(monkeypatch):
    _patch_settings(monkeypatch, _settings())
    assert _settings().grid_co2e_g_per_kwh == 400.0
    assert co2e_grams(Decimal("1000")) == Decimal("400")


def test_grid_setting_is_env_configurable():
    assert _settings(grid_co2e_g_per_kwh=123.5).grid_co2e_g_per_kwh == 123.5


# ── the persisted breakdown ───────────────────────────────────────────────────
def test_accounting_is_auditable_and_json_safe(monkeypatch):
    _patch_settings(monkeypatch, _settings())
    report = energy_accounting(_model("L"), 100_000, 10_000, 800_000, 100_000)

    assert report["estimated"] is True
    assert report["energy_class"] == "L"
    assert report["energy_wh_per_mtok"] == 1200.0
    assert report["weighted_tokens"] == 290_000.0
    assert report["energy_wh"] == 348.0  # compute / IT load, no PUE
    assert report["grid_co2e_g_per_kwh"] == 400.0
    # co2e_g is the run *total*, so it carries the data-centre PUE (1.2 by
    # default): 348 Wh x 1.2 = 417.6 Wh at 400 gCO2e/kWh = 167.04 g. The scope
    # decomposition of that total lives in tests/test_emissions.py.
    assert report["energy_wh_total"] == 417.6
    assert report["co2e_g"] == 167.04
    assert "estimate, not a measurement" in report["basis"]
    # JSON-safe all the way down — the block now nests `scopes` and `baseline`.
    flat = [v for v in report.values() if not isinstance(v, dict)]
    nested = [v for d in report.values() if isinstance(d, dict) for v in d.values()]
    assert all(v is None or isinstance(v, (bool, int, float, str)) for v in flat + nested)


def test_accounting_grid_override_is_recorded_with_the_figure():
    report = energy_accounting(_model("M"), 1_000_000, 0, grid_g_per_kwh=30.0)
    assert report["energy_wh"] == 300.0  # compute / IT load, unaffected by PUE
    assert report["grid_co2e_g_per_kwh"] == 30.0
    # The carbon figure is the run total, so the overridden grid factor is applied
    # to the PUE-inclusive energy: 300 Wh x 1.2 = 360 Wh at 30 gCO2e/kWh = 10.8 g.
    assert report["energy_wh_total"] == 360.0
    assert report["co2e_g"] == 10.8


# ── zero dollars never means zero watts ───────────────────────────────────────
def test_a_free_model_still_reports_energy():
    free = _model("S", input_price_per_mtok=Decimal("0"), output_price_per_mtok=Decimal("0"))
    assert free.cost_usd(1_000_000, 1_000_000) == Decimal(0)
    report = energy_accounting(free, 1_000_000, 1_000_000, grid_g_per_kwh=400.0)
    assert report["energy_wh"] == 100.0
    assert report["co2e_g"] > 0


# ── catalog wiring ────────────────────────────────────────────────────────────
def test_every_curated_model_declares_an_energy_class():
    raw = yaml.safe_load(MODELS_YAML.read_text())
    for entry in raw["models"]:
        assert entry.get("energy_class") in ENERGY_CLASSES, entry["id"]


def test_curated_catalog_energy_is_loaded_and_positive():
    catalog = ModelCatalog()
    for model in catalog.all(curated_only=True):
        assert model.energy_class in ENERGY_CLASSES
        assert model.energy_wh_per_mtok > 0
        assert model.energy_wh_per_mtok == wh_per_mtok_for_class(model.energy_class)
    # Premium flagships must not read as cheaper to run than economy models.
    assert catalog.get("anthropic/claude-fable-5").energy_wh_per_mtok > catalog.get(
        "anthropic/claude-haiku-4-5"
    ).energy_wh_per_mtok


def test_model_json_exposes_energy_for_ui_pickers():
    payload = _model("XL").to_json()
    assert payload["energy_class"] == "XL"
    assert payload["energy_wh_per_mtok"] == 3000.0


# ── fake httpx transport (mirrors test_local_provider.py) ─────────────────────
class _FakeModelsResponse:
    def __init__(self, payload=None, status=200):
        self._payload = payload or {}
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)

    def json(self):
        return self._payload


class _FakeGetClient:
    def __init__(self, payload=None):
        self._payload = payload

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None):
        return _FakeModelsResponse(self._payload)


async def _always_ok(*args, **kwargs) -> dict:
    return {"ok": True}


async def test_discovered_local_models_are_small_but_never_free_in_watts(monkeypatch):
    _patch_settings(monkeypatch, _settings(local_base_url="http://localhost:11434/v1"))
    monkeypatch.setattr(catalog_module.LocalProvider, "complete_json", _always_ok)
    monkeypatch.setattr(
        catalog_module.httpx,
        "AsyncClient",
        _FakeGetClient({"data": [{"id": "qwen2.5:14b-instruct", "context_length": 32768}]}),
    )
    catalog = ModelCatalog()
    await catalog.refresh_local()

    info = catalog.get("local/qwen2.5:14b-instruct")
    assert info.output_price_per_mtok == Decimal("0")  # free in dollars
    assert info.energy_class == "S"
    assert info.energy_wh_per_mtok == Decimal("50")
    assert info.energy_wh(1_000_000, 0) == Decimal("50")  # never free in watts


async def test_dynamic_openrouter_models_get_a_class_from_their_price_tier(monkeypatch):
    _patch_settings(
        monkeypatch, _settings(openrouter_api_key="k", openrouter_catalog=True, local_base_url="")
    )
    monkeypatch.setattr(
        catalog_module.httpx,
        "AsyncClient",
        _FakeGetClient(
            {
                "data": [
                    {
                        "id": "vendor/free-model",
                        "supported_parameters": ["tools"],
                        "pricing": {"prompt": "0", "completion": "0"},
                    },
                    {
                        "id": "vendor/mid-model",
                        "supported_parameters": ["tools"],
                        "pricing": {"prompt": "0.000005", "completion": "0.000015"},
                    },
                    {
                        "id": "vendor/big-model",
                        "supported_parameters": ["tools"],
                        "pricing": {"prompt": "0.00002", "completion": "0.00006"},
                    },
                ]
            }
        ),
    )
    catalog = ModelCatalog()
    await catalog.refresh_dynamic()

    free = catalog.get("openrouter/vendor/free-model")
    mid = catalog.get("openrouter/vendor/mid-model")
    big = catalog.get("openrouter/vendor/big-model")
    assert (free.cost_tier, free.energy_class) == ("economy", "M")
    assert (mid.cost_tier, mid.energy_class) == ("standard", "L")
    assert (big.cost_tier, big.energy_class) == ("premium", "XL")
    # A zero-priced OpenRouter entry is not a zero-energy entry.
    assert free.energy_wh(1_000_000, 0) == Decimal("300")


# ── runs API exposure ─────────────────────────────────────────────────────────
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


class _StubDb:
    """The one method api.runs.get_run uses."""

    def __init__(self, run: Run):
        self._run = run

    async def get(self, _model, _id):
        return self._run


def test_run_summary_carries_energy_beside_dollars():
    accounting = energy_accounting(_model("L"), 100_000, 10_000, grid_g_per_kwh=400.0)
    summary = _run_summary(
        _run(cost_usd=Decimal("0.45"), energy_wh=Decimal("132.0"), energy_accounting=accounting)
    )
    assert summary["cost_usd"] == 0.45
    assert summary["energy_wh"] == 132.0
    assert summary["co2e_g"] == accounting["co2e_g"] > 0


def test_a_run_predating_eco_accounting_reports_none_not_zero():
    summary = _run_summary(_run(cost_usd=Decimal("0.45")))
    assert summary["energy_wh"] is None
    assert summary["co2e_g"] is None


async def test_run_detail_exposes_the_full_derivation():
    accounting = energy_accounting(_model("L"), 100_000, 10_000, grid_g_per_kwh=400.0)
    run = _run(energy_wh=Decimal("132.0"), energy_accounting=accounting)
    detail = await get_run(run.id, user=None, db=_StubDb(run))
    assert detail["energy"] == accounting
    assert detail["energy"]["energy_class"] == "L"
    assert detail["energy_wh"] == 132.0


# ── deliverable provenance ────────────────────────────────────────────────────
def test_footprint_sums_distinct_runs_and_keeps_the_run_time_grid():
    def _accounted(wh: str, co2e: float) -> Run:
        return _run(
            energy_wh=Decimal(wh),
            energy_accounting={"co2e_g": co2e, "grid_co2e_g_per_kwh": 400},
        )

    runs = [_accounted("10.0", 4.0), _accounted("2.5", 1.0)]
    footprint = _deliverable_footprint(runs)
    assert footprint["runs"] == 2
    assert footprint["energy_wh"] == 12.5
    assert footprint["co2e_g"] == 5.0
    assert footprint["grid_co2e_g_per_kwh"] == 400.0
    assert footprint["estimated"] is True


def test_footprint_counts_missing_estimates_instead_of_zeroing_them():
    footprint = _deliverable_footprint([_run(), _run()])
    assert footprint["runs"] == 0
    assert footprint["runs_without_estimate"] == 2
    assert footprint["energy_wh"] is None
    assert footprint["co2e_g"] is None


def test_provenance_cell_is_labelled_estimated_or_an_em_dash():
    assert _energy_cell({"energy_wh": 12.345, "co2e_g": 4.9}) == "~12.3 Wh · 4.9 gCO2e"
    assert _energy_cell({"energy_wh": 12.345, "co2e_g": None}) == "~12.3 Wh"
    assert _energy_cell({}) == "—"
