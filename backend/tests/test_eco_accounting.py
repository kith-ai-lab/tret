"""Estimated energy/carbon accounting: the model, the math, and its exposure.

No network and no DB: the catalog's energy model is exercised directly, catalog
discovery is driven through a fake httpx client (as in test_local_provider.py),
and the runs API serializers are called with detached ORM objects.

The class constants are now calibrated rather than hand-picked (least-squares
fits against Jegham et al., arXiv:2505.09598 — see
docs/emissions-methodology.md), but they are still estimates. What these tests
lock down is the arithmetic on top of them: that input and output tokens are
weighted apart by the fitted ~20x, that the reasoning tier exists and sits far
above the ladder, that discounts match the cost model, and above all that
nothing is ever silently zero.

Every expected number below is spelled out from the constants rather than copied
from a previous run, so a future recalibration has to be argued for in the test
rather than absorbed by a loosened assertion.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import httpx
import pytest
import yaml

from tret.providers.base import JsonCompletion
from tret.api.runs import _run_summary, get_run
from tret.config import Settings
from tret.db.models import Run
from tret.providers import catalog as catalog_module
from tret.providers.catalog import (
    DEFAULT_ENERGY_CLASS,
    ENERGY_CACHE_READ_MULTIPLIER,
    ENERGY_CACHE_WRITE_MULTIPLIER,
    ENERGY_CLASS_WH_PER_MTOK,
    ENERGY_CLASSES,
    ENERGY_TOKEN_WEIGHT_INPUT,
    ENERGY_TOKEN_WEIGHT_OUTPUT,
    REASONING_ENERGY_CLASS,
    ModelCatalog,
    ModelInfo,
    co2e_grams,
    energy_accounting,
    energy_class_for_tier,
    wh_per_mtok_for_class,
)
from tret.services.export import _deliverable_footprint, _energy_cell

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


def _assert_json_safe(value, path: str = "report") -> None:
    """The block must survive a JSONB round-trip: dicts, lists, scalars only."""
    if isinstance(value, dict):
        for k, v in value.items():
            assert isinstance(k, str), path
            _assert_json_safe(v, f"{path}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _assert_json_safe(v, f"{path}[{i}]")
    else:
        assert value is None or isinstance(value, (bool, int, float, str)), (path, value)


# ── the class table ───────────────────────────────────────────────────────────
def test_energy_classes_are_ordered_powers_of_scale():
    assert ENERGY_CLASSES == ("S", "M", "L", "XL", "R")
    values = [ENERGY_CLASS_WH_PER_MTOK[c] for c in ENERGY_CLASSES]
    # Wh per million OUTPUT-EQUIVALENT tokens, each anchored on a least-squares
    # fit of Wh = a*input + b*output over Jegham et al. (arXiv:2505.09598):
    # S from GPT-4.1 nano (b=271.9), M from GPT-4o (b=1233.1), L from Claude 3.7
    # Sonnet (b=2634.7), R from o3 (b=20850.4). XL is the only interpolation.
    assert values == [
        Decimal("250"),
        Decimal("1200"),
        Decimal("2600"),
        Decimal("6000"),
        Decimal("21000"),
    ]
    assert values == sorted(values)
    assert all(v > 0 for v in values)


def test_the_reasoning_tier_is_its_own_class_far_above_the_ladder():
    reasoning = ENERGY_CLASS_WH_PER_MTOK[REASONING_ENERGY_CLASS]
    # o3 implies ~10,700 Wh/Mtok on a flat per-token basis at the medium prompt
    # shape — already 3.5x the old XL ceiling of 3,000, and 20,850 once input and
    # output are weighted apart. The tier exists because that gap is real.
    assert reasoning == Decimal("21000")
    assert reasoning > ENERGY_CLASS_WH_PER_MTOK["XL"] * 3
    assert _model(energy_class="R").energy_wh_per_mtok == Decimal("21000")
    assert _model(energy_class="R").is_reasoning_tier is True
    for other in ("S", "M", "L", "XL"):
        assert _model(energy_class=other).is_reasoning_tier is False


def test_no_cost_tier_ever_infers_the_reasoning_tier():
    # DeepSeek-R1 is among the two heaviest models measured and among the
    # cheapest sold, so price is not evidence either way: R must be declared.
    for tier in ("local", "economy", "standard", "premium", "something-new"):
        assert energy_class_for_tier(tier) != REASONING_ENERGY_CLASS


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
    assert _model(energy_class="S").energy_wh_per_mtok == Decimal("250")
    measured = _model(energy_class="S", energy_wh_per_mtok=Decimal("7.5"))
    assert measured.energy_wh_per_mtok == Decimal("7.5")  # a real measurement wins


# ── per-turn math ─────────────────────────────────────────────────────────────
def test_the_class_figure_is_a_million_OUTPUT_tokens():
    # The unit is an output-equivalent token, so a million *generated* tokens is
    # the class figure exactly...
    assert _model("L").energy_wh(0, 1_000_000) == Decimal("2600")
    # ...and a million *read* tokens is a twentieth of it. This is the whole
    # change: prefill is parallel, generation is sequential.
    assert _model("L").energy_wh(1_000_000, 0) == Decimal("130")
    assert _model("L").energy_wh(0, 1_000_000) == 20 * _model("L").energy_wh(1_000_000, 0)


def test_input_and_output_are_no_longer_interchangeable():
    model = _model("M")
    assert ENERGY_TOKEN_WEIGHT_INPUT == Decimal("0.05")  # 1/20, the fitted b/a
    assert ENERGY_TOKEN_WEIGHT_OUTPUT == Decimal("1")
    # 1,000 in / 1,000 out is dominated by the output half.
    buckets = model.energy_wh_by_bucket(1_000, 1_000)
    assert buckets["output"] == 20 * buckets["input"]
    assert sum(buckets.values()) == model.energy_wh(1_000, 1_000)
    # Swapping the shape (10k in / 100 out vs 100 in / 10k out) moves the figure
    # by ~20x, which a 1:1 weighting could not express at all.
    assert model.energy_wh(100, 10_000) > 15 * model.energy_wh(10_000, 100)


def test_bucket_energy_always_sums_to_the_run_figure():
    for cls in ("S", "M", "L", "XL", "R"):
        model = _model(cls)
        tokens = (123_457, 9_871, 55_555, 3_333)
        assert sum(model.energy_wh_by_bucket(*tokens).values()) == model.energy_wh(*tokens)


def test_cache_reads_are_discounted_like_price_but_not_free():
    model = _model("L")
    # 0.005 output-equivalents = 0.1x an input token, the same discount price
    # gets: 2600 x 0.005 = 13 Wh per million tokens read.
    assert ENERGY_CACHE_READ_MULTIPLIER == Decimal("0.005")
    assert ENERGY_CACHE_READ_MULTIPLIER == Decimal("0.1") * ENERGY_TOKEN_WEIGHT_INPUT
    assert model.energy_wh(0, 0, cache_read_tokens=1_000_000) == Decimal("13")
    assert model.energy_wh(0, 0, cache_read_tokens=1_000_000) > 0


def test_cache_writes_are_a_full_forward_pass():
    model = _model("L")
    # A write is a full prefill pass, so it weighs exactly what input does —
    # which is now 0.05 output-equivalents rather than 1.0.
    assert ENERGY_CACHE_WRITE_MULTIPLIER == ENERGY_TOKEN_WEIGHT_INPUT == Decimal("0.05")
    assert model.energy_wh(0, 0, cache_write_tokens=1_000_000) == model.energy_wh(1_000_000, 0)
    assert model.energy_wh(0, 0, cache_write_tokens=1_000_000) == Decimal("130")


def test_a_warm_cache_read_beats_re_sending_the_prefix():
    model = _model("L")
    assert model.energy_wh(0, 0, cache_read_tokens=500_000) < model.energy_wh(500_000, 0)


def test_energy_sums_all_four_buckets():
    model = _model("M")
    energy = model.energy_wh(100_000, 10_000, 800_000, 100_000)
    # 0.05x100k + 1x10k + 0.005x800k + 0.05x100k = 5,000 + 10,000 + 4,000 + 5,000
    weighted = Decimal("24000")
    assert energy == Decimal("1200") * weighted / Decimal(1_000_000) == Decimal("28.8")


def test_no_tokens_no_energy():
    assert _model("XL").energy_wh(0, 0, 0, 0) == Decimal(0)


# ── carbon ────────────────────────────────────────────────────────────────────
def test_co2e_uses_the_configured_grid_intensity():
    assert co2e_grams(Decimal("1000"), grid_g_per_kwh=400.0) == Decimal("400")
    assert co2e_grams(Decimal("1000"), grid_g_per_kwh=30.0) == Decimal("30")


def test_grid_intensity_defaults_to_the_cited_iea_global_average(monkeypatch):
    _patch_settings(monkeypatch, _settings())
    # IEA Electricity 2025, 2024 global power-sector average (~460-480; 470 is
    # the midpoint). The old 400 was stale-low and uncited.
    assert _settings().grid_co2e_g_per_kwh == 470.0
    assert co2e_grams(Decimal("1000")) == Decimal("470")


def test_grid_setting_is_env_configurable():
    assert _settings(grid_co2e_g_per_kwh=123.5).grid_co2e_g_per_kwh == 123.5


# ── the persisted breakdown ───────────────────────────────────────────────────
def test_accounting_is_auditable_and_json_safe(monkeypatch):
    _patch_settings(monkeypatch, _settings())
    report = energy_accounting(_model("L"), 100_000, 10_000, 800_000, 100_000)

    assert report["estimated"] is True
    assert report["energy_class"] == "L"
    assert report["energy_wh_per_mtok"] == 2600.0
    # 0.05x100k + 1x10k + 0.005x800k + 0.05x100k = 24,000 output-equivalents.
    assert report["weighted_tokens"] == 24_000.0
    assert report["energy_wh"] == 62.4  # 2600 x 24,000 / 1e6, compute only
    assert report["grid_co2e_g_per_kwh"] == 470.0
    # co2e_g is the run *total*, so it carries the data-centre PUE (1.2 by
    # default): 62.4 Wh x 1.2 = 74.88 Wh at 470 gCO2e/kWh = 35.1936 g. The scope
    # decomposition of that total lives in tests/test_emissions.py.
    assert report["energy_wh_total"] == 74.88
    assert report["co2e_g"] == 35.1936
    assert "estimate, not a measurement" in report["basis"]
    # Per-bucket energy sums to the compute figure, so the split is inspectable.
    assert sum(report["energy_wh_by_bucket"].values()) == pytest.approx(62.4)
    assert report["energy_wh_by_bucket"]["output"] == 26.0  # 2600 x 10k / 1e6
    _assert_json_safe(report)


def test_accounting_grid_override_is_recorded_with_the_figure():
    report = energy_accounting(_model("M"), 1_000_000, 0, grid_g_per_kwh=30.0)
    # 1 Mtok of *input* at M is 1200 x 0.05 = 60 Wh, not 1200.
    assert report["energy_wh"] == 60.0  # compute / IT load, unaffected by PUE
    assert report["grid_co2e_g_per_kwh"] == 30.0
    # The carbon figure is the run total, so the overridden grid factor is applied
    # to the PUE-inclusive energy: 60 Wh x 1.2 = 72 Wh at 30 gCO2e/kWh = 2.16 g.
    assert report["energy_wh_total"] == 72.0
    assert report["co2e_g"] == 2.16
    # tret was handed a number, not a provenance, so it refuses to label it.
    assert report["grid_co2e_basis"] == "unspecified"


# ── zero dollars never means zero watts ───────────────────────────────────────
def test_a_free_model_still_reports_energy():
    free = _model("S", input_price_per_mtok=Decimal("0"), output_price_per_mtok=Decimal("0"))
    assert free.cost_usd(1_000_000, 1_000_000) == Decimal(0)
    report = energy_accounting(free, 1_000_000, 1_000_000, grid_g_per_kwh=400.0)
    # 250 x (0.05x1M + 1M) / 1e6 = 262.5 Wh
    assert report["energy_wh"] == 262.5
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
    assert payload["energy_wh_per_mtok"] == 6000.0
    # Added so a picker can show that reading is cheap and writing is not.
    assert payload["energy_wh_per_mtok_output"] == 6000.0
    assert payload["energy_wh_per_mtok_input"] == 300.0
    assert payload["reasoning_tier"] is False
    assert _model("R").to_json()["reasoning_tier"] is True


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


async def _always_ok(*args, **kwargs) -> JsonCompletion:
    return JsonCompletion(payload={"ok": True})


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
    assert info.energy_wh_per_mtok == Decimal("250")
    assert info.energy_wh(0, 1_000_000) == Decimal("250")  # never free in watts
    assert info.energy_wh(1_000_000, 0) == Decimal("12.5")


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
    assert free.energy_wh(1_000_000, 0) == Decimal("60")
    assert free.energy_wh(0, 1_000_000) == Decimal("1200")
    # Price tiers never infer the reasoning class, however expensive.
    assert big.energy_class != "R"


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
