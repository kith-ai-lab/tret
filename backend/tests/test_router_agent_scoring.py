"""The per-agent scored ranking (`router_llm.scoring`): pure, hand-built rows."""
from __future__ import annotations

import json
import math
import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from tests.test_router_exploration import _model, _prior
from tret.router_llm import scoring
from tret.router_llm.scoring import AgentRow, acceptable, decide, models_with_record

NOW = datetime(2026, 11, 12, 12, tzinfo=timezone.utc)


def _row(model="test/a", *, ok=True, cost=0.05, age=0.0, objective="balanced", energy=None,
         inp=1000, out=500, **over):
    base = dict(
        model_id=model, objective=objective, observed_at=NOW - timedelta(days=age),
        outcome_class="delivered" if ok else "failed", cost_usd=cost, energy_wh=energy,
        input_tokens=inp, output_tokens=out, findings_rejected=0, is_batch=False, target_class=None,
    )
    base.update(over)
    return AgentRow(**base)


def _runs(model, n, ok_n, **kw):
    return [_row(model, ok=i < ok_n, **kw) for i in range(n)]


def _m(name, price="1", open_weights=None, wh=None):
    m = _model(name, price=price)
    m.open_weights = open_weights
    m.energy_wh_per_mtok = None if wh is None else Decimal(wh)
    return m


def _go(rows, models, pooled=None, **over):
    args = dict(
        rows=rows, pooled=pooled or {}, models=models, objective="balanced", open_weights="any",
        exploration_share=0.1, draw=0.99, live_choice=None, now=NOW,
    )
    args.update(over)
    return decide(**args)


def _by(block):
    return {e["model"]: e for e in block["models"]}


# ── acceptable ───────────────────────────────────────────────────────────────
def test_acceptable_non_batch_needs_delivered_and_nothing_rejected():
    assert acceptable(_row()) is True
    assert acceptable(_row(ok=False)) is False
    assert acceptable(_row(findings_rejected=1)) is False


def test_acceptable_batch_row_without_a_current_target_is_left_out():
    assert acceptable(_row(is_batch=True, target_class=None)) is None


@pytest.mark.parametrize("cls", ["infra", "timeout", "unsettled"])
def test_acceptable_batch_excluded_classes_are_left_out(cls):
    assert acceptable(_row(is_batch=True, target_class=cls)) is None


def test_acceptable_batch_needs_a_success_target_and_a_delivered_run():
    assert acceptable(_row(is_batch=True, target_class="success")) is True
    assert acceptable(_row(is_batch=True, target_class="success", ok=False)) is False
    for cls in ("gap", "rejected", "failed"):
        assert acceptable(_row(is_batch=True, target_class=cls)) is False
    # A batch row's own findings_rejected does not matter: the target decides.
    assert acceptable(_row(is_batch=True, target_class="success", findings_rejected=3)) is True


def test_left_out_rows_are_counted_neither_way():
    rows = _runs("test/a", 6, 6) + [_row("test/a", is_batch=True, target_class=None)] * 5
    b = _go(rows, [_m("a")])
    assert b["agent"]["runs"] == 6 and b["models"][0]["runs"] == 6


# ── weighting ────────────────────────────────────────────────────────────────
def test_time_decay_halves_a_row_every_thirty_days():
    old = [_row("test/a", age=30.0) for _ in range(10)]
    assert models_with_record(old, "balanced", NOW) == {"test/a"}  # 5.0 exactly
    assert models_with_record(old[:9], "balanced", NOW) == set()
    assert models_with_record([_row("test/a", age=60.0)] * 20, "balanced", NOW) == {"test/a"}


def test_other_objective_rows_count_for_half():
    rows = [_row("test/a", objective="eco") for _ in range(9)]
    assert models_with_record(rows, "balanced", NOW) == set()
    assert models_with_record(rows + [_row("test/a", objective="eco")], "balanced", NOW) == {"test/a"}


def test_a_naive_timestamp_is_read_as_utc_and_a_missing_one_as_now():
    rows = [_row("test/a", observed_at=None) for _ in range(3)] + [
        _row("test/a", observed_at=(NOW - timedelta(days=0)).replace(tzinfo=None)) for _ in range(2)
    ]
    assert models_with_record(rows, "balanced", NOW) == {"test/a"}


# ── prior only below the record threshold ────────────────────────────────────
def test_below_min_runs_the_prior_alone_decides():
    a, b = _m("a"), _m("b", price="2")
    base = _runs("test/a", 20, 19)
    pooled = {"test/b": _prior("test/b", 0.9, effective_n=30.0)}
    zero = _by(_go(base, [a, b], pooled))["test/b"]
    four = _by(_go(base + _runs("test/b", 4, 0), [a, b], pooled))["test/b"]
    for key in zero:
        if key in ("runs", "effective_n", "sat_out"):
            continue
        assert zero[key] == four[key], key
    assert zero["basis"] == "pooled" and zero["runs"] == 0 and four["runs"] == 4
    assert four["sat_out"] is True and zero["sat_out"] is False


def test_pooled_prior_counts_for_at_most_three_runs():
    a, b = _m("a"), _m("b")
    base = _runs("test/a", 10, 10)
    big = _by(_go(base, [a, b], {"test/b": _prior("test/b", 0.9, effective_n=500.0)}))["test/b"]
    three = _by(_go(base, [a, b], {"test/b": _prior("test/b", 0.9, effective_n=3.0)}))["test/b"]
    assert big["acceptable_rate"] == three["acceptable_rate"]
    # a=0.5+2.7=3.2, b=0.5+0.3=0.8: mean 0.8
    assert big["acceptable_rate"] == pytest.approx(0.8)
    small = _by(_go(base, [a, b], {"test/b": _prior("test/b", 0.9, effective_n=1.0)}))["test/b"]
    # one run's worth: a=0.5+0.9=1.4, b=0.5+0.1=0.6, mean 0.7
    assert small["acceptable_rate"] == pytest.approx(0.7)


def test_a_model_without_a_pooled_prior_uses_the_agent_average_for_one_run():
    a, b = _m("a"), _m("b")
    block = _go(_runs("test/a", 10, 8), [a, b])
    e = _by(block)["test/b"]
    assert block["agent"]["acceptable_rate"] == pytest.approx(0.8)
    assert e["basis"] == "agent_average"
    # a=0.5+0.8=1.3, b=0.5+0.2=0.7 -> mean 0.65
    assert e["acceptable_rate"] == pytest.approx(0.65)


def test_a_model_with_a_record_adds_its_own_results_to_the_prior():
    e = _by(_go(_runs("test/a", 100, 90), [_m("a")]))["test/a"]
    assert e["basis"] == "agent" and e["acceptable_rate"] == pytest.approx(0.9, abs=0.01)
    assert e["cautious_rate"] < e["acceptable_rate"] < e["optimistic_rate"]


# ── resource ─────────────────────────────────────────────────────────────────
def test_observed_cost_for_a_model_with_a_record_and_list_price_otherwise():
    a, b = _m("a", price="100"), _m("b", price="2")
    # observed equals a's own list estimate (0.2), so the calibration is 1
    rows = _runs("test/a", 10, 10, cost=0.2, inp=1000, out=1000)
    by = _by(_go(rows, [a, b]))
    assert by["test/a"]["resource_basis"] == "observed" and by["test/a"]["resource_per_run"] == 0.2
    # list price: 2 USD/Mtok both ways over typical 1000 + 1000 tokens
    assert by["test/b"]["resource_basis"] == "list_price"
    assert by["test/b"]["resource_per_run"] == pytest.approx(0.004)


def test_another_agents_cost_is_never_used():
    # The module only ever sees this agent's rows: a model with no row here has
    # list price, whatever it costs elsewhere.
    a, b = _m("a", price="5"), _m("b", price="3")
    by = _by(_go(_runs("test/a", 10, 10, cost=0.005, inp=1000, out=0), [a, b]))
    assert by["test/b"]["resource_basis"] == "list_price"
    assert by["test/b"]["resource_per_run"] == pytest.approx(0.003)


def test_token_conservation_is_not_scored():
    # Recorded token counts leave out cache reads, so tokens per result is not
    # comparable across models.
    block = _go(_runs("test/a", 10, 10, inp=800, out=200), [_m("a")], objective="token_conservation")
    assert block["status"] == "objective_not_scored" and "tokens" not in scoring.UNITS.values()


def test_wh_unit_uses_observed_energy_and_falls_back_to_list_when_none_recorded():
    a, b = _m("a", wh="10"), _m("b", wh="10")
    with_energy = _by(_go(_runs("test/a", 10, 10, energy=0.25), [a, b], objective="eco"))
    assert with_energy["test/a"]["resource_per_run"] == 0.25
    assert with_energy["test/a"]["resource_basis"] == "observed"
    without = _by(_go(_runs("test/a", 10, 10, energy=None, inp=1000, out=500), [a, b], objective="eco"))
    assert without["test/a"]["resource_basis"] == "list_price"
    assert without["test/a"]["resource_per_run"] == pytest.approx(float(a.energy_wh(1000, 500)))


def test_a_model_with_no_estimate_is_excluded_and_unscored():
    a, b = _m("a", wh="10"), _m("b")  # b has no energy data, so eco has no list estimate for it
    by = _by(_go(_runs("test/a", 10, 10, energy=0.1), [a, b], objective="eco"))
    assert by["test/b"]["excluded"] == "no_estimate" and by["test/b"]["score"] is None


def test_a_free_tried_model_is_a_valid_zero_score_and_wins():
    block = _go(_runs("test/free", 30, 30, cost=0.0) + _runs("test/paid", 30, 30, cost=0.05),
                [_m("free", price="0"), _m("paid")])
    e = _by(block)["test/free"]
    assert e["excluded"] is None and e["score"] == 0.0 and e["resource_per_run"] == 0.0
    assert block["ranked_pick"] == "test/free"


def test_free_untried_models_are_ordered_for_exploration_by_optimistic_rate():
    tried = _m("tried")
    f1, f2 = _m("f1", price="0"), _m("f2", price="0")
    pooled = {
        "test/f1": _prior("test/f1", 0.6, effective_n=3.0),
        "test/f2": _prior("test/f2", 0.95, effective_n=3.0),
    }
    block = _go(_runs("test/tried", 30, 29), [tried, f1, f2], pooled)
    assert _by(block)["test/f1"]["score"] == 0.0
    assert block["exploration"]["candidate"] == "test/f2"


# ── status ───────────────────────────────────────────────────────────────────
def test_quality_objective_is_not_scored():
    block = _go(_runs("test/a", 10, 10), [_m("a")], objective="quality")
    assert block == {"version": scoring.SCORING_VERSION, "status": "objective_not_scored", "objective": "quality"}


def test_no_record_gives_no_agent_record_and_ranks_nothing():
    block = _go(_runs("test/a", 4, 4), [_m("a"), _m("b")])
    assert block["status"] == "no_agent_record" and block["agent"] == {"runs": 4}
    assert "models" not in block and "pick" not in block


def test_a_record_for_a_model_outside_the_pool_does_not_count_as_a_record():
    block = _go(_runs("test/z", 10, 10), [_m("a")])
    assert block["status"] == "no_agent_record"


# ── reliability floor ────────────────────────────────────────────────────────
def test_the_floor_removes_a_very_cheap_untried_model_but_it_still_explores():
    good, cheap = _m("good", price="10"), _m("cheap", price="0.01")
    rows = _runs("test/good", 60, 58, cost=0.05, inp=1000, out=1000)
    pooled = {"test/cheap": _prior("test/cheap", 0.9, effective_n=3.0)}
    block = _go(rows, [good, cheap], pooled)
    by = _by(block)
    assert by["test/cheap"]["excluded"] == "no_record"
    assert block["ranked_pick"] == "test/good"
    assert block["exploration"]["candidate"] == "test/cheap"
    assert block["reliability_floor"]["threshold"] == pytest.approx(
        0.8 * block["reliability_floor"]["best_tried_cautious_rate"], abs=1e-3
    )


def test_the_floor_removes_no_one_when_every_tried_model_is_poor():
    a, b = _m("a"), _m("b", price="2")
    rows = _runs("test/a", 40, 12, cost=0.05) + _runs("test/b", 40, 12, cost=0.04)
    by = _by(_go(rows, [a, b]))
    assert by["test/a"]["excluded"] is None and by["test/b"]["excluded"] is None


# ── open weights ─────────────────────────────────────────────────────────────
def _ow_rows():
    return _runs("test/closed", 50, 50, cost=0.10) + _runs("test/open", 50, 50, cost=0.11)


def test_open_weights_prefer_takes_an_open_model_within_the_margin():
    models = [_m("closed", open_weights=False), _m("open", open_weights=True)]
    block = _go(_ow_rows(), models, open_weights="prefer")
    assert block["ranked_pick"] == "test/open" and block["open_weights"]["applied"] is True
    assert block["open_weights"]["policy"] == "prefer"


def test_open_weights_prefer_does_not_apply_outside_the_margin():
    models = [_m("closed", open_weights=False), _m("open", open_weights=True)]
    rows = _runs("test/closed", 50, 50, cost=0.10) + _runs("test/open", 50, 50, cost=0.20)
    block = _go(rows, models, open_weights="prefer")
    assert block["ranked_pick"] == "test/closed" and block["open_weights"]["applied"] is False
    assert _go(_ow_rows(), models, open_weights="any")["ranked_pick"] == "test/closed"


# ── exploration ──────────────────────────────────────────────────────────────
def _explore_setup():
    tried, fresh = _m("tried", price="10"), _m("fresh", price="1")
    rows = _runs("test/tried", 30, 29)
    pooled = {"test/fresh": _prior("test/fresh", 0.9, effective_n=3.0)}
    return rows, [tried, fresh], pooled


def test_exploration_fires_only_when_the_draw_is_under_the_share():
    rows, models, pooled = _explore_setup()
    fired = _go(rows, models, pooled, draw=0.05)
    held = _go(rows, models, pooled, draw=0.10)
    assert fired["exploration"]["fired"] is True and fired["pick"] == "test/fresh"
    assert held["exploration"]["fired"] is False and held["pick"] == "test/tried"
    assert held["exploration"]["candidate"] == "test/fresh" and held["exploration"]["draw"] == 0.1


def test_exploration_share_zero_never_fires():
    rows, models, pooled = _explore_setup()
    block = _go(rows, models, pooled, exploration_share=0.0, draw=0.0)
    assert block["exploration"]["fired"] is False and block["pick"] == "test/tried"


def test_a_model_whose_last_two_runs_failed_is_not_an_exploration_candidate():
    rows, models, pooled = _explore_setup()
    rows = rows + _runs("test/fresh", 2, 0)
    block = _go(rows, models, pooled, draw=0.0)
    assert _by(block)["test/fresh"]["sat_out"] is True
    assert block["exploration"]["candidate"] is None and block["exploration"]["fired"] is False


def test_one_failure_among_two_recent_runs_is_not_sitting_out():
    rows, models, pooled = _explore_setup()
    rows = rows + [_row("test/fresh", ok=False, age=1.0), _row("test/fresh", ok=True, age=0.5)]
    assert _by(_go(rows, models, pooled))["test/fresh"]["sat_out"] is False


# ── live choice ──────────────────────────────────────────────────────────────
def test_agrees_with_live_compares_the_pick():
    rows, models, pooled = _explore_setup()
    assert _go(rows, models, pooled, live_choice="test/tried")["agrees_with_live"] is True
    assert _go(rows, models, pooled, live_choice="test/fresh")["agrees_with_live"] is False
    assert _go(rows, models, pooled, live_choice=None)["agrees_with_live"] is False


# ── output hygiene ───────────────────────────────────────────────────────────
def test_the_result_is_deterministic_whatever_the_row_order():
    rows, models, pooled = _explore_setup()
    rows = rows + _runs("test/fresh", 3, 2, age=3.0)
    rng = random.Random(7)
    first = _go(rows, models, pooled)
    for _ in range(5):
        shuffled = rows[:]
        rng.shuffle(shuffled)
        assert _go(shuffled, models, pooled) == first
    assert json.dumps(first, sort_keys=True) == json.dumps(_go(rows, models, pooled), sort_keys=True)


def test_the_result_is_json_with_no_nan_or_infinity():
    rows = _runs("test/a", 10, 0) + _runs("test/b", 10, 10, cost=1e-9) + _runs("test/c", 10, 10, cost=1e12)
    block = _go(rows, [_m("a"), _m("b"), _m("c"), _m("d", price="0")])
    text = json.dumps(block, allow_nan=False)  # raises on NaN or infinity

    def walk(x):
        if isinstance(x, float):
            assert math.isfinite(x)
        elif isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(json.loads(text))
    assert "—" not in block["reason"]


def test_the_models_list_is_capped_and_keeps_the_exploration_candidate_and_live_choice():
    models = [_m(f"m{i:02d}", price=str(1 + i)) for i in range(12)]
    rows = [r for i in range(10) for r in _runs(f"test/m{i:02d}", 10, 10, cost=0.01 * (i + 1))]
    pooled = {"test/m11": _prior("test/m11", 0.9, effective_n=3.0)}
    block = _go(rows, models, pooled, live_choice="test/m09")
    ids = [e["model"] for e in block["models"]]
    assert len(ids) == scoring.SHOWN_MODELS == len(set(ids))
    assert "test/m09" in ids and block["exploration"]["candidate"] in ids
    assert ids[0] == "test/m00"
    ranked_first = block["ranked_pick"]
    assert ranked_first in ids


def test_only_ranked_models_appear_in_order_when_there_is_room():
    rows, models, pooled = _explore_setup()
    block = _go(rows, models, pooled)
    scores = [e["score"] for e in block["models"] if e["excluded"] is None]
    assert scores == sorted(scores)


# ── the real case ────────────────────────────────────────────────────────────
def test_scenario_a_good_cheap_model_wins_and_a_much_cheaper_untried_one_explores():
    a, b, c = _m("a", price="5"), _m("b", price="50"), _m("c", price="8")
    u1, u2 = _m("u1", price="5"), _m("u2", price="0.5")  # u2 is 10x cheaper by list
    rows = (
        _runs("test/a", 150, 139, cost=0.056, inp=20000, out=2000)
        + _runs("test/b", 48, 46, cost=0.90, inp=20000, out=2000)
        + _runs("test/c", 60, 39, cost=0.08, inp=20000, out=2000)
    )
    pooled = {
        "test/u1": _prior("test/u1", 0.85, effective_n=12.0),
        "test/u2": _prior("test/u2", 0.85, effective_n=12.0),
    }
    block = _go(rows, [a, b, c, u1, u2], pooled, live_choice="test/a")
    by = _by(block)
    assert block["ranked_pick"] == "test/a"
    assert block["exploration"]["candidate"] == "test/u2"
    assert block["ranked_pick"] != "test/u2"
    assert by["test/u2"]["excluded"] == "no_record"
    assert by["test/u2"]["resource_basis"] == "list_price" and by["test/a"]["resource_basis"] == "observed"
    assert block["pick"] == "test/a" and block["agrees_with_live"] is True
    # Fired by the draw, the same candidate is the pick.
    assert _go(rows, [a, b, c, u1, u2], pooled, draw=0.01)["pick"] == "test/u2"
    assert "test/a" in block["reason"]


# ── list-price calibration ───────────────────────────────────────────────────
def _cal_block(tried_cost, objective="balanced", price="1", **kw):
    # 1000 in + 1000 out at 1 USD/Mtok lists at 0.002
    tried, fresh = _m("tried", price=price, wh="10"), _m("fresh", price=price, wh="10")
    rows = _runs("test/tried", 10, 10, cost=tried_cost, inp=1000, out=1000, **kw)
    return _go(rows, [tried, fresh], objective=objective), tried, fresh


def test_calibration_is_one_when_observed_equals_the_list_estimate():
    block, _, _ = _cal_block(0.002)
    assert block["agent"]["list_price_calibration"] == 1.0
    assert _by(block)["test/fresh"]["resource_per_run"] == pytest.approx(0.002)


def test_calibration_scales_untried_list_prices_but_not_observed_ones():
    block, _, _ = _cal_block(0.006)
    assert block["agent"]["list_price_calibration"] == 3.0
    by = _by(block)
    assert by["test/fresh"]["resource_per_run"] == pytest.approx(0.006)
    assert by["test/fresh"]["resource_basis"] == "list_price"
    assert by["test/tried"]["resource_per_run"] == 0.006


def test_calibration_is_clamped_at_both_ends():
    assert _cal_block(1000.0)[0]["agent"]["list_price_calibration"] == scoring.CALIBRATION_MAX
    assert _cal_block(1e-7)[0]["agent"]["list_price_calibration"] == scoring.CALIBRATION_MIN


def test_calibration_is_one_without_a_usable_list_estimate():
    free = _m("free", price="0")
    block = _go(_runs("test/free", 10, 10, cost=0.05), [free, _m("b")])
    assert block["agent"]["list_price_calibration"] == 1.0


def test_the_wh_fallback_uses_the_calibration():
    # tried has energy data; other has a record but none, so it falls back to list.
    a, b = _m("a", wh="10"), _m("b", wh="10")
    listed = float(a.energy_wh(1000, 500))
    rows = _runs("test/a", 10, 10, energy=listed * 2, inp=1000, out=500) + _runs(
        "test/b", 10, 10, energy=None, inp=1000, out=500
    )
    block = _go(rows, [a, b], objective="eco")
    assert block["agent"]["list_price_calibration"] == pytest.approx(2.0)
    e = _by(block)["test/b"]
    assert e["resource_basis"] == "list_price" and e["resource_per_run"] == pytest.approx(listed * 2)


# ── the main ranking admits only tried models, bar one exception ─────────────
def test_an_untried_model_with_a_strong_prior_and_a_lower_price_does_not_take_the_main_slot():
    tried, fresh = _m("tried", price="1"), _m("fresh", price="0.5")
    rows = _runs("test/tried", 40, 34, cost=0.002, inp=1000, out=1000)
    pooled = {"test/fresh": _prior("test/fresh", 0.95, effective_n=10.0)}
    block = _go(rows, [tried, fresh], pooled, draw=0.99)
    assert block["ranked_pick"] == "test/tried" and block["pick"] == "test/tried"
    assert _by(block)["test/fresh"]["excluded"] == "no_record"
    assert block["exploration"]["candidate"] == "test/fresh"


def test_when_every_tried_model_is_poor_a_more_reliable_untried_one_joins_the_ranking():
    tried, fresh = _m("tried", price="1"), _m("fresh", price="1")
    rows = _runs("test/tried", 20, 10, cost=0.002, inp=1000, out=1000)
    pooled = {"test/fresh": _prior("test/fresh", 1.0, effective_n=3.0)}
    block = _go(rows, [tried, fresh], pooled)
    by = _by(block)
    assert by["test/fresh"]["excluded"] is None
    # the floor is measured against the best TRIED rate, so the tried model stays
    assert by["test/tried"]["excluded"] is None
    assert block["ranked_pick"] == "test/fresh"


def test_no_scored_tried_model_is_no_agent_record():
    block = _go(_runs("test/a", 10, 10, energy=None, cost=0.0), [_m("a"), _m("b")], objective="eco")
    assert block["status"] == "no_agent_record"


# ── a model's first runs move nothing but its own row ────────────────────────
def test_a_few_runs_of_a_model_without_a_record_change_only_its_own_row():
    a, b = _m("a"), _m("b", price="2")
    base = _runs("test/a", 20, 19, cost=0.05, inp=1000, out=500)
    pooled = {"test/b": _prior("test/b", 0.9, effective_n=30.0)}
    # very different tokens and costs, and the last two runs acceptable
    extra = [_row("test/b", ok=i != 0, cost=9.0, inp=90000, out=40000, age=float(i)) for i in range(4)]
    zero = _go(base, [a, b], pooled)
    four = _go(base + extra, [a, b], pooled)
    for block, runs in ((zero, 0), (four, 4)):
        block["agent"]["runs"] -= runs  # the one agent-level number allowed to move
    assert zero["agent"]["runs"] == four["agent"]["runs"] == 20

    def strip(block):
        block = json.loads(json.dumps(block))
        for e in block["models"]:
            if e["model"] == "test/b":
                for k in ("runs", "effective_n", "sat_out"):
                    e.pop(k)
        return block

    assert strip(zero) == strip(four)


def test_a_draw_of_none_never_fires():
    rows, models, pooled = _explore_setup()
    block = _go(rows, models, pooled, draw=None, exploration_share=0.1)
    assert block["exploration"]["draw"] is None and block["exploration"]["fired"] is False


def test_a_paid_model_recorded_at_zero_cost_is_not_treated_as_free():
    x, y = _m("x", price="5"), _m("y", price="5")
    rows = (
        _runs("test/x", 30, 29, cost=0.0, inp=1000, out=1000)
        + _runs("test/y", 30, 29, cost=0.002, inp=1000, out=1000)
    )
    block = _go(rows, [x, y])
    by = _by(block)
    assert block["ranked_pick"] == "test/y"
    assert by["test/x"]["resource_basis"] == "list_price" and by["test/x"]["resource_per_run"] > 0


def test_an_untried_ranked_pick_is_not_also_the_exploration_candidate():
    tried, f1, f2 = _m("tried"), _m("f1"), _m("f2")
    rows = _runs("test/tried", 20, 10, cost=0.002, inp=1000, out=1000)
    pooled = {
        "test/f1": _prior("test/f1", 1.0, effective_n=3.0),
        "test/f2": _prior("test/f2", 0.95, effective_n=3.0),
    }
    block = _go(rows, [tried, f1, f2], pooled)
    assert block["ranked_pick"] == "test/f1" and block["exploration"]["candidate"] == "test/f2"
    solo = _go(rows, [tried, f1], {"test/f1": pooled["test/f1"]})
    assert solo["ranked_pick"] == "test/f1" and solo["exploration"]["candidate"] is None
    assert "No untried model was eligible" in solo["reason"]


def test_the_floor_comparison_is_strict(monkeypatch):
    monkeypatch.setattr(scoring, "RELIABILITY_FLOOR", 1.0)
    rows = _runs("test/a", 30, 27) + _runs("test/b", 30, 27)
    by = _by(_go(rows, [_m("a"), _m("b")]))
    assert by["test/a"]["excluded"] is None and by["test/b"]["excluded"] is None


# ── exploration_live ─────────────────────────────────────────────────────────
def _fresh_case():
    rows = _runs("test/tried", 10, 10)
    return rows, [_m("tried"), _m("fresh")], {"test/tried": _prior("test/tried", 0.9)}


def test_exploration_not_live_picks_the_ranked_pick_even_when_the_draw_fires():
    rows, models, pooled = _fresh_case()
    block = _go(rows, models, pooled, draw=0.05, exploration_live=False)
    ex = block["exploration"]
    assert ex["fired"] is True and ex["applied"] is False and ex["candidate"] == "test/fresh"
    assert block["pick"] == block["ranked_pick"] == "test/tried"
    assert "would have been tried but the exploration share is not live for this agent yet" in block["reason"]
    assert "test/fresh" in block["reason"]


def test_exploration_not_live_keeps_the_existing_wording_when_the_draw_did_not_fire():
    rows, models, pooled = _fresh_case()
    live = _go(rows, models, pooled, draw=0.99)
    held = _go(rows, models, pooled, draw=0.99, exploration_live=False)
    assert held["reason"] == live["reason"]
    off = _go(rows, models, pooled, exploration_share=0.0, draw=None, exploration_live=False)
    assert "Exploration is off" in off["reason"] and off["exploration"]["applied"] is False


def test_exploration_applied_is_true_in_the_default_mode_when_fired():
    rows, models, pooled = _fresh_case()
    block = _go(rows, models, pooled, draw=0.05)
    assert block["exploration"]["fired"] is True and block["exploration"]["applied"] is True
    assert block["pick"] == "test/fresh"
    assert _go(rows, models, pooled, draw=0.99)["exploration"]["applied"] is False


def test_the_block_lists_the_main_ranking_in_rank_order():
    a, b = _m("a", price="5"), _m("b", price="1")
    block = _go(_runs("test/a", 10, 10, cost=0.05) + _runs("test/b", 10, 10, cost=0.01), [a, b])
    assert block["ranking"] == ["test/b", "test/a"] and block["ranked_pick"] == "test/b"
