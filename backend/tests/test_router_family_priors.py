"""Family evidence: the pure re-ranking, the provider's truthful status and the router.

The properties that matter most: with no family, a closed gate or the setting off,
nothing about routing changes; with an open gate the family only RE-RANKS models it
can compare, by a bounded shift of their unchanged base priors."""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from tests.test_router_exploration import (
    DOES_NOT_FIRE_AT_DEFAULT_SEED,
    ROUTE_ARGS,
    _AnsweringRegistry,
    _catalog,
    _model,
    _policy,
    _prior,
)
from tret.config import get_settings
from tret.db.models import RunOutcome
from tret.router_llm.priors import OutcomePriors, invalidate_all_priors, reset_priors_registry
from tret.router_llm.priors_base import (
    FAMILY_MAX_SHIFT,
    FAMILY_MIN_ROWS,
    FAMILY_SHRINKAGE_STRENGTH,
    NoPriors,
    family_adjusted,
)
from tret.router_llm.router import ModelRouter

NOW = datetime(2026, 11, 12, tzinfo=timezone.utc)
FAMILY = "disclosure:fy2025@00000000-0000-0000-0000-000000000001"


@pytest.fixture(autouse=True)
def _fresh_registry():
    reset_priors_registry()
    yield
    reset_priors_registry()


def _row(model_id, quality, *, evaluated=None, age_days=0.0, family=FAMILY, judge="j", rubric="v1"):
    return RunOutcome(
        task_shape="freeform", objective="balanced", max_cost_tier="premium", task_type="freeform",
        size_band="m", model_id=model_id, provider="openrouter", outcome_class="delivered",
        quality_score=Decimal(str(quality)),
        evaluated_quality_score=None if evaluated is None else Decimal(str(evaluated)),
        score_version="outcome-v1",
        components={} if evaluated is None else {"evaluator": {"rubric_version": rubric, "judge_model": judge}},
        iterations=3, cost_usd=Decimal("0.01"), input_tokens=100, output_tokens=50, duration_ms=1,
        task_family=family, observed_at=NOW - timedelta(days=age_days),
    )


def _many(model_id, evaluated, n, **kw):
    return [_row(model_id, 0.5, evaluated=evaluated, **kw) for _ in range(n)]


# ── family_adjusted ──────────────────────────────────────────────────────────
def test_one_family_row_changes_nothing_for_a_well_established_model():
    base = {"m/a": _prior("m/a", 0.80, floor=0.72, effective_n=50.0)}
    out, info = family_adjusted(base, _many("m/a", 0.85, 1), now=NOW)
    assert out == base and out["m/a"] is base["m/a"] and info["adjusted"] == {}


def test_a_single_eligible_model_says_nothing_about_relative_quality():
    base = {"m/a": _prior("m/a", 0.7), "m/b": _prior("m/b", 0.7)}
    out, _ = family_adjusted(base, _many("m/a", 0.99, 40) + _many("m/b", 0.1, 2), now=NOW)
    assert all(out[k] is base[k] for k in base)


def test_two_eligible_models_shift_by_the_formula_and_others_keep_their_object():
    base = {"m/a": _prior("m/a", 0.70, floor=0.60), "m/b": _prior("m/b", 0.70, floor=0.60), "m/c": _prior("m/c", 0.5)}
    out, info = family_adjusted(base, _many("m/a", 0.90, 30) + _many("m/b", 0.60, 30), now=NOW)
    e_bar = 0.75
    shift = 30 / (30 + FAMILY_SHRINKAGE_STRENGTH)
    assert out["m/a"].quality_mean == pytest.approx(0.70 + shift * 0.15, abs=1e-6)
    assert out["m/a"].quality_ci_low == pytest.approx(0.60 + shift * 0.15, abs=1e-6)
    assert out["m/b"].quality_mean == pytest.approx(0.70 - shift * 0.15, abs=1e-6)
    assert out["m/a"].effective_n == base["m/a"].effective_n and out["m/a"].runs == base["m/a"].runs
    assert out["m/c"] is base["m/c"]
    assert info["pooled_mean"] == pytest.approx(e_bar) and set(info["adjusted"]) == {"m/a", "m/b"}


def test_a_harsh_judge_with_equal_models_demotes_nobody():
    base = {"m/a": _prior("m/a", 0.75, floor=0.7), "m/b": _prior("m/b", 0.75, floor=0.7), "m/u": _prior("m/u", 0.75, floor=0.7)}
    out, info = family_adjusted(base, _many("m/a", 0.55, 20) + _many("m/b", 0.55, 20), now=NOW)
    assert info["adjusted"] == {"m/a": 0.0, "m/b": 0.0}
    assert out["m/a"].quality_mean == pytest.approx(0.75) and out["m/u"] is base["m/u"]


def test_the_shift_is_clamped():
    base = {"m/a": _prior("m/a", 0.5), "m/b": _prior("m/b", 0.5)}
    out, info = family_adjusted(base, _many("m/a", 1.0, 400) + _many("m/b", 0.0, 400), now=NOW)
    assert info["adjusted"]["m/a"] == pytest.approx(FAMILY_MAX_SHIFT, abs=1e-3)
    assert out["m/a"].quality_mean == pytest.approx(0.5 + FAMILY_MAX_SHIFT, abs=1e-3)
    big = {"m/a": _prior("m/a", 0.95), "m/b": _prior("m/b", 0.05)}
    top, _ = family_adjusted(big, _many("m/a", 1.0, 400) + _many("m/b", 0.0, 400), now=NOW)
    assert 0.0 <= top["m/b"].quality_mean and top["m/a"].quality_mean <= 1.0


def test_five_rows_shift_less_than_thirty():
    base = {"m/a": _prior("m/a", 0.7), "m/b": _prior("m/b", 0.7)}

    def delta(n):
        _, info = family_adjusted(base, _many("m/a", 0.9, n) + _many("m/b", 0.6, n), now=NOW)
        return info["adjusted"]["m/a"]

    assert 0 < delta(int(FAMILY_MIN_ROWS)) < delta(30)
    assert family_adjusted(base, _many("m/a", 0.9, 4) + _many("m/b", 0.6, 4), now=NOW)[1]["adjusted"] == {}


def test_decay_applies_and_rows_without_an_evaluated_score_are_ignored():
    base = {"m/a": _prior("m/a", 0.7), "m/b": _prior("m/b", 0.7)}
    _, info = family_adjusted(base, _many("m/a", 0.9, 6, age_days=300) + _many("m/b", 0.6, 30), now=NOW)
    assert abs(info["adjusted"]["m/a"]) < 0.01  # eligible by count, but decay leaves it ~no weight
    plain = [_row("m/a", 0.9) for _ in range(30)] + [_row("m/b", 0.2) for _ in range(30)]
    assert family_adjusted(base, plain, now=NOW)[0] == base


# ── the provider: for_key keeps its signature; for_key_family reports truthfully ─────────
class _P(OutcomePriors):
    def __init__(self, *, gate, base_rows, family_rows, gate_error=False, rows_error=False):
        super().__init__(session_factory=object(), now=lambda: NOW)
        self.gate, self.base_rows, self.family_rows = gate, base_rows, family_rows
        self.gate_error, self.rows_error = gate_error, rows_error
        self.family_calls = 0

    async def _rows(self, task_shape, objective):
        return self.base_rows

    async def _family_rows(self, task_family, objective):
        self.family_calls += 1
        if self.rows_error:
            raise RuntimeError("rows down")
        return self.family_rows

    async def _gate(self, task_family):
        if self.gate_error:
            raise RuntimeError("gate down")
        return self.gate


KEY = dict(task_shape="freeform", objective="balanced", size_band="m")
OPEN = {"open": True, "spot_checks": 15, "agreement": 0.9, "rubric_version": "v1", "judge_model": "j"}
CLOSED = {"open": False, "spot_checks": 2, "agreement": 0.5, "rubric_version": "v1", "judge_model": "j"}


def _base_rows():
    return ([_row("m/a", 0.7, family=None) for _ in range(8)] + [_row("m/b", 0.7, family=None) for _ in range(8)])


def _fam_rows():
    return _many("m/a", 0.9, 10) + _many("m/b", 0.5, 10) + _many("m/a", 0.1, 10, judge="other")


async def test_for_key_takes_no_family_and_is_untouched():
    with pytest.raises(TypeError):
        await _P(gate=OPEN, base_rows=_base_rows(), family_rows=[]).for_key(**KEY, task_family=FAMILY)


async def test_closed_gate_returns_base_and_reports_unused():
    p = _P(gate=CLOSED, base_rows=_base_rows(), family_rows=_fam_rows())
    base = await p.for_key(**KEY)
    got, status = await p.for_key_family(**KEY, task_family=FAMILY)
    assert got == base and status["used"] is False and p.family_calls == 0


async def test_open_gate_adjusts_only_with_the_gates_judge_and_rubric():
    p = _P(gate=OPEN, base_rows=_base_rows(), family_rows=_fam_rows())
    base = await p.for_key(**KEY)
    got, status = await p.for_key_family(**KEY, task_family=FAMILY)
    assert status["used"] is True and set(status["adjusted"]) == {"m/a", "m/b"}
    assert got["m/a"].quality_mean > base["m/a"].quality_mean > got["m/b"].quality_mean
    # the other judge's rows (0.1 for m/a) were ignored: m/a is still above the mean
    again = await p.for_key_family(**KEY, task_family=FAMILY)
    assert again == (got, status) and p.family_calls == 1  # cached
    # a run-end invalidate() keeps the gate cache; the hooks' invalidate_all_priors() clears it
    p._gates[FAMILY] = (1e18, OPEN)
    p.invalidate()
    assert FAMILY in p._gates
    invalidate_all_priors()
    assert p._gates == {}


async def test_open_gate_without_two_comparable_models_is_not_reported_as_used():
    p = _P(gate=OPEN, base_rows=_base_rows(), family_rows=_many("m/a", 0.9, 10))
    got, status = await p.for_key_family(**KEY, task_family=FAMILY)
    assert got == await p.for_key(**KEY) and status["used"] is False


async def test_off_returns_no_status_and_failures_fall_back(monkeypatch):
    p = _P(gate=OPEN, base_rows=_base_rows(), family_rows=_fam_rows())
    base = await p.for_key(**KEY)
    monkeypatch.setattr(get_settings(), "router_family_priors", "off")
    assert await p.for_key_family(**KEY, task_family=FAMILY) == (base, None)
    monkeypatch.setattr(get_settings(), "router_family_priors", "gated")
    for kw in ({"gate_error": True}, {"rows_error": True}):
        broken = _P(gate=OPEN, base_rows=_base_rows(), family_rows=_fam_rows(), **kw)
        got, status = await broken.for_key_family(**KEY, task_family=FAMILY)
        assert got == base and status["used"] is False and status["error"] is True


async def test_failing_adjustment_falls_back(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr("tret.router_llm.priors.family_adjusted", boom)
    p = _P(gate=OPEN, base_rows=_base_rows(), family_rows=_fam_rows())
    got, status = await p.for_key_family(**KEY, task_family=FAMILY)
    assert got == await p.for_key(**KEY) and status["error"] is True


async def test_no_priors_has_the_trivial_family_method():
    assert await NoPriors().for_key_family(task_shape="x", objective="y", task_family=FAMILY) == ({}, None)


# ── the router ───────────────────────────────────────────────────────────────
class _Stub:
    def __init__(self, base, family, status):
        self.base, self.family, self.status = base, family, status
        self.base_calls = self.family_calls = 0

    async def for_key(self, *, task_shape, objective, size_band=None):
        self.base_calls += 1
        return self.base

    async def for_key_family(self, *, task_shape, objective, size_band=None, task_family):
        self.family_calls += 1
        return self.family, self.status

    def invalidate(self):
        pass


def _comparable(d):
    j = d.to_json()
    j.pop("decided_at", None)
    j.pop("latency_ms", None)
    return j


async def _decide(priors, **over):
    a, b = _model("alpha"), _model("beta", price="2")
    # The exploration roll (5% by default on this shape: it picks an untried model with
    # no router call) is the only nondeterminism in route(). Pin it the way
    # test_router_exploration does, or two calls are not comparable (the full-suite
    # flake of the identical-decision test was exactly this).
    router = ModelRouter(
        _catalog(a, b), _AnsweringRegistry({"openrouter"}, "test/alpha"), priors,
        rng=random.Random(DOES_NOT_FIRE_AT_DEFAULT_SEED),
    )
    args = dict(model_policy=_policy(), **ROUTE_ARGS)
    args.update(over)
    return await router.route(**args)


@pytest.mark.parametrize("family_first", [True, False])
async def test_unused_family_evidence_leaves_the_decision_identical(family_first):
    a = "test/alpha"
    base = {a: _prior(a, 0.9, floor=0.8)}
    for status in (None, {"used": False, "gate": {"open": False}, "adjusted": {}},
                   {"used": False, "error": True, "gate": {}, "adjusted": {}}):
        for _ in range(10):  # the old flake was 5% per call
            stub = _Stub(base, base, status)  # unused means the base priors came back
            calls = [lambda: _decide(stub, task_family=FAMILY), lambda: _decide(stub)]
            if not family_first:
                calls.reverse()
            first, second = await calls[0](), await calls[1]()
            assert _comparable(first) == _comparable(second)
            assert "task_family" not in first.to_json() and "family_priors" not in first.to_json()


async def test_a_used_adjustment_is_recorded_and_changes_the_order():
    a, b = "test/alpha", "test/beta"
    base = {a: _prior(a, 0.9, floor=0.8)}
    family = {a: _prior(a, 0.6, floor=0.5), b: _prior(b, 0.95, floor=0.9)}
    status = {"used": True, "gate": {"open": True, "spot_checks": 15, "agreement": 0.9}, "adjusted": {a: -0.3}}
    decision = await _decide(_Stub(base, family, status), task_family=FAMILY)
    plain = await _decide(_Stub(base, family, status))
    assert plain.candidates[0] == a and decision.candidates[0] == b
    assert decision.task_family == FAMILY and decision.family_priors["used"] is True
    assert decision.family_priors["adjusted"] == {a: -0.3}


async def test_override_and_pinned_paths_never_read_family_priors():
    stub = _Stub({}, {}, {"used": True, "gate": {}, "adjusted": {}})
    over = await _decide(stub, task_family=FAMILY, run_override="test/alpha")
    pinned = await _decide(stub, task_family=FAMILY, model_policy=_policy(mode="pinned", model="test/beta"))
    assert stub.family_calls == 0 and stub.base_calls == 0
    assert over.family_priors is None and pinned.family_priors is None


def test_a_model_without_a_base_prior_is_unrankable_and_ignored():
    base = {"m/a": _prior("m/a", 0.7)}
    out, info = family_adjusted(base, _many("m/d", 0.9, 10) + _many("m/a", 0.6, 10), now=NOW)
    assert out["m/a"] is base["m/a"] and info["adjusted"] == {}  # only one rankable model
    base3 = {"m/a": _prior("m/a", 0.7), "m/b": _prior("m/b", 0.7)}
    out, info = family_adjusted(
        base3, _many("m/a", 0.9, 10) + _many("m/b", 0.6, 10) + _many("m/d", 0.1, 50), now=NOW)
    assert info["pooled_mean"] == pytest.approx(0.75) and "m/d" not in out


def test_eligibility_is_by_row_count_not_decayed_weight():
    base = {"m/a": _prior("m/a", 0.7), "m/b": _prior("m/b", 0.7)}
    five = _many("m/a", 0.9, 5, age_days=1) + _many("m/b", 0.6, 5, age_days=1)
    four = _many("m/a", 0.9, 4, age_days=1) + _many("m/b", 0.6, 5, age_days=1)
    assert set(family_adjusted(base, five, now=NOW)[1]["adjusted"]) == {"m/a", "m/b"}
    assert family_adjusted(base, four, now=NOW)[1]["adjusted"] == {}


async def test_a_provider_without_for_key_family_means_no_adjustment():
    class OnlyForKey:
        async def for_key(self, *, task_shape, objective, size_band=None):
            return {"test/alpha": _prior("test/alpha", 0.9, floor=0.8)}

        def invalidate(self):
            pass

    with_family = await _decide(OnlyForKey(), task_family=FAMILY)
    without = await _decide(OnlyForKey())
    assert _comparable(with_family) == _comparable(without)
