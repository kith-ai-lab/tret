"""Inputs captured for the facility_v3 emissions method (additive evidence only)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tret.engine.harness import ModelSegment
from tret.providers.base import Usage
from tret.providers.catalog import ModelCatalog, ModelInfo, parse_reasoning_default
from tret.services.emissions import overhead_call


def _model() -> ModelInfo:
    return ModelInfo(
        id="openrouter/x/y", provider="openrouter", wire_id="x/y", display_name="Y",
        context_window=1000, input_price_per_mtok=Decimal(1),
        output_price_per_mtok=Decimal(2), cost_tier="standard",
    )


@pytest.mark.parametrize(
    "raw,expected",
    [
        ({"mandatory": False, "default_enabled": True, "default_effort": "high"}, (True, "high")),
        ({"default_enabled": False}, (False, None)),
        ({"default_enabled": "yes", "default_effort": 3}, (None, None)),
        ({}, (None, None)),
        (None, (None, None)),
        ("high", (None, None)),
    ],
)
def test_parse_reasoning_default(raw, expected):
    assert parse_reasoning_default(raw) == expected


def test_modelinfo_defaults_are_none():
    m = _model()
    assert m.reasoning_default_enabled is None
    assert m.reasoning_default_effort is None


def test_curated_sonnet5_declares_reasoning_default():
    m = ModelCatalog().get("anthropic/claude-sonnet-5")
    assert m is not None
    assert m.reasoning_default_enabled is True
    assert m.reasoning_default_effort == "high"


def test_segment_call_record_reasoning_requested():
    seg = ModelSegment(model=_model(), reason="initial")
    seg.add(Usage(input_tokens=1, output_tokens=1), 1, served_by="Anthropic")
    rec = seg.call_records[-1]
    assert rec["reasoning_requested"] is None
    assert rec["reasoning_tokens"] is None  # not exposed
    assert rec["served_by"] == "Anthropic"
    seg.effort = "high"
    seg.add(Usage(input_tokens=1, output_tokens=1, reasoning_tokens=5), 2)
    rec = seg.call_records[-1]
    assert rec["reasoning_requested"] is True
    assert rec["reasoning_tokens"] == 5


def test_overhead_call_served_by_is_optional():
    u = Usage(input_tokens=10, output_tokens=5)
    assert "served_by" not in overhead_call("routing", _model(), u)
    assert overhead_call("routing", _model(), u, served_by="google")["served_by"] == "google"


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, **kwargs):
        return _Resp(self._payload)


async def test_refresh_dynamic_parses_reasoning_metadata(monkeypatch):
    def entry(wire_id, **extra):
        return {
            "id": wire_id, "created": 1735689600, "supported_parameters": ["tools"],
            "pricing": {"prompt": "0.000001", "completion": "0.000002"}, **extra,
        }

    payload = {
        "data": [
            entry("v/thinker", reasoning={
                "mandatory": False, "default_enabled": True, "default_effort": "high"}),
            entry("v/plain"),
        ]
    }
    from tret import config as config_module
    import tret.providers.catalog as catalog_module

    monkeypatch.setattr(config_module, "get_settings", lambda: config_module.Settings(
        openrouter_catalog=True, openrouter_api_key="test-key"))
    monkeypatch.setattr(catalog_module, "get_settings", config_module.get_settings)
    monkeypatch.setattr(catalog_module, "open_client", lambda *a, **k: _FakeClient(payload))
    catalog = ModelCatalog()
    await catalog.refresh_dynamic()
    thinker = catalog._dynamic["openrouter/v/thinker"]
    assert (thinker.reasoning_default_enabled, thinker.reasoning_default_effort) == (True, "high")
    plain = catalog._dynamic["openrouter/v/plain"]
    assert (plain.reasoning_default_enabled, plain.reasoning_default_effort) == (None, None)
