"""`benchmark/scoring_routing.py`'s summariser, on hand-built rows.

Offline and hand-built on purpose: `summarize_by_config`/`router_vs_pinned`
are pure functions over already-scored rows, so every aggregate here is
checkable without the engine, a live stack, or a network call — the same
reasoning `test_routing_priors.py` gives for testing `summarize` directly.
`score_case` itself (which reads the pack's sample CSVs) is exercised by
`arm_routing.py`/`scoring.py` at runtime, not here.

`rows`/`scored` are parallel lists — `scored[i]` stands in for
`score_case(rows[i], ...)` — matching the convention `scoring.summarize`
already uses for its own `scored` list.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "benchmark"))

from scoring_routing import router_vs_pinned, summarize_by_config  # noqa: E402


def _row(config: str, **overrides) -> dict:
    row = {
        "case_id": "C01",
        "config": config,
        "status": "completed",
        "cost_usd": 0.01,
        "reported_cost_usd": 0.012,
        "router_overhead_usd": 0.001,
        "input_tokens": 1000,
        "output_tokens": 200,
        "cache_read_tokens": 0,
        "iterations": 3,
        "switches_count": 0,
        "effort": "medium",
    }
    row.update(overrides)
    return row


def _scored(**overrides) -> dict:
    scored = {"answered": True, "agrees_with_label": None}
    scored.update(overrides)
    return scored


# ---------------------------------------------------------------------------
# summarize_by_config: per-configuration aggregates
# ---------------------------------------------------------------------------


def test_per_config_aggregates_basic():
    rows = [
        _row("auto:quality", case_id="C01", cost_usd=0.02, iterations=4, effort="high"),
        _row("auto:quality", case_id="C02", cost_usd=0.04, iterations=2, effort="high", status="failed"),
        _row("pinned:economy/model", case_id="C01", cost_usd=0.01, iterations=1, effort=None),
    ]
    scored = [
        _scored(agrees_with_label=True),
        _scored(answered=False, agrees_with_label=False),
        _scored(agrees_with_label=True),
    ]

    summary = summarize_by_config(rows, scored)

    assert set(summary) == {"auto:quality", "pinned:economy/model"}

    quality = summary["auto:quality"]
    assert quality["cases"] == 2
    # C02 answered False -> not delivered even though status alone isn't enough
    assert quality["delivered_rate"] == 0.5
    assert quality["verdict_agreement"] == 0.5  # 1 of 2 labeled rows agreed
    assert quality["mean_cost_usd"] == 0.03
    assert quality["mean_iterations"] == 3
    assert quality["effort_distribution"] == {"high": 1.0}

    pinned = summary["pinned:economy/model"]
    assert pinned["cases"] == 1
    assert pinned["delivered_rate"] == 1.0
    assert pinned["verdict_agreement"] == 1.0
    # effort None is excluded from the distribution entirely
    assert pinned["effort_distribution"] == {}


def test_switch_rate_and_effort_distribution_mix():
    rows = [
        _row("auto:balanced", switches_count=1, effort="low"),
        _row("auto:balanced", switches_count=0, effort="low"),
        _row("auto:balanced", switches_count=2, effort="high"),
        _row("auto:balanced", switches_count=0, effort=None),
    ]
    scored = [_scored() for _ in rows]

    summary = summarize_by_config(rows, scored)["auto:balanced"]

    assert summary["switch_rate"] == 0.5  # 2 of 4 rows switched at least once
    # distribution denominator is all 4 rows, not just the 3 with a recorded effort
    assert summary["effort_distribution"] == {"low": 0.5, "high": 0.25}


def test_verdict_agreement_is_none_with_no_labeled_rows():
    rows = [_row("auto:eco")]
    scored = [_scored(agrees_with_label=None)]

    summary = summarize_by_config(rows, scored)["auto:eco"]

    assert summary["verdict_agreement"] is None


# ---------------------------------------------------------------------------
# cache hit ratio: cache_read / (cache_read + input), including zero denominators
# ---------------------------------------------------------------------------


def test_cache_hit_ratio_normal_case():
    rows = [
        _row("auto:token_conservation", cache_read_tokens=300, input_tokens=100),
        _row("auto:token_conservation", cache_read_tokens=100, input_tokens=500),
    ]
    scored = [_scored(), _scored()]

    summary = summarize_by_config(rows, scored)["auto:token_conservation"]

    # (300 + 100) / (300 + 100 + 100 + 500) = 400 / 1000
    assert summary["cache_hit_ratio"] == 0.4


def test_cache_hit_ratio_zero_denominator_is_none_not_a_crash():
    rows = [_row("pinned:local/model", cache_read_tokens=0, input_tokens=0)]
    scored = [_scored()]

    summary = summarize_by_config(rows, scored)["pinned:local/model"]

    assert summary["cache_hit_ratio"] is None


def test_cache_hit_ratio_missing_keys_entirely_defaults_like_zero():
    # An older result row that predates the cache ledger: no cache_read_tokens
    # or input_tokens key at all, not just a null value.
    row = _row("pinned:local/model")
    del row["cache_read_tokens"]
    del row["input_tokens"]

    summary = summarize_by_config([row], [_scored()])["pinned:local/model"]

    assert summary["cache_hit_ratio"] is None


# ---------------------------------------------------------------------------
# router_vs_pinned: deltas against the best-agreement pin and the cheapest pin
# ---------------------------------------------------------------------------


def test_router_vs_pinned_picks_best_agreement_and_cheapest():
    config_summary = {
        "auto:quality": {
            "mean_cost_usd": 0.05,
            "mean_reported_cost_usd": 0.06,
            "verdict_agreement": 0.9,
        },
        "pinned:cheap-economy": {
            "mean_cost_usd": 0.01,
            "mean_reported_cost_usd": 0.01,
            "verdict_agreement": 0.7,
        },
        "pinned:strong-premium": {
            "mean_cost_usd": 0.20,
            "mean_reported_cost_usd": 0.22,
            "verdict_agreement": 0.95,
        },
    }
    rows = [
        {"config": "auto:quality", "router_overhead_usd": 0.002},
        {"config": "auto:quality", "router_overhead_usd": 0.004},
    ]

    table = router_vs_pinned(config_summary, rows)

    assert set(table) == {"auto:quality"}
    entry = table["auto:quality"]
    assert entry["router_overhead_usd"] == 0.003
    # 0.06 reported cost + 0.003 mean overhead
    assert entry["router_cost_with_overhead_usd"] == 0.063

    best = entry["vs_best_agreement_pin"]
    assert best["pin"] == "pinned:strong-premium"
    assert best["cost_delta_usd"] == round(0.063 - 0.22, 6)
    assert best["agreement_delta"] == round(0.9 - 0.95, 4)

    cheap = entry["vs_cheapest_pin"]
    assert cheap["pin"] == "pinned:cheap-economy"
    assert cheap["cost_delta_usd"] == round(0.063 - 0.01, 6)
    assert cheap["agreement_delta"] == round(0.9 - 0.7, 4)


def test_router_vs_pinned_empty_without_any_pinned_config():
    config_summary = {
        "auto:quality": {"mean_cost_usd": 0.05, "mean_reported_cost_usd": None, "verdict_agreement": 0.9},
    }
    assert router_vs_pinned(config_summary, []) == {}


def test_router_vs_pinned_handles_missing_agreement_and_cost_without_crashing():
    # A pin with no labeled cases (agreement None) and no cost at all — the
    # kind of row a very early, mostly-failed run would produce.
    config_summary = {
        "auto:balanced": {"mean_cost_usd": None, "mean_reported_cost_usd": None, "verdict_agreement": None},
        "pinned:only-pin": {"mean_cost_usd": None, "mean_reported_cost_usd": None, "verdict_agreement": None},
    }

    table = router_vs_pinned(config_summary, [])

    entry = table["auto:balanced"]
    assert entry["router_overhead_usd"] is None
    assert entry["router_cost_with_overhead_usd"] is None
    best = entry["vs_best_agreement_pin"]
    assert best["pin"] == "pinned:only-pin"
    assert best["cost_delta_usd"] is None
    assert best["agreement_delta"] is None
    # no priced pin at all -> no cheapest-pin comparison offered
    assert "vs_cheapest_pin" not in entry


def test_router_vs_pinned_missing_router_overhead_rows_does_not_crash():
    config_summary = {
        "auto:quality": {"mean_cost_usd": 0.05, "mean_reported_cost_usd": 0.05, "verdict_agreement": 0.8},
        "pinned:p": {"mean_cost_usd": 0.02, "mean_reported_cost_usd": 0.02, "verdict_agreement": 0.6},
    }
    # rows carry no "router_overhead_usd" key at all (an older result file)
    rows = [{"config": "auto:quality"}]

    table = router_vs_pinned(config_summary, rows)

    assert table["auto:quality"]["router_overhead_usd"] is None
    assert table["auto:quality"]["router_cost_with_overhead_usd"] == 0.05
