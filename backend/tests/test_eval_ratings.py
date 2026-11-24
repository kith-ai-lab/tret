import math
import random

import pytest

from tret.evaluation.ratings import Rating, bands, fit_bradley_terry, flip_rate, pearson, spearman


def test_strong_player_ranks_top():
    items = ["s", "a", "b", "c"]
    outcomes = [("s", x, 1.0) for x in "abc"] * 3 + [("a", "b", 1.0), ("b", "c", 1.0), ("a", "c", 1.0)]
    r = fit_bradley_terry(items, outcomes)
    assert r["s"].rating > r["a"].rating > r["b"].rating > r["c"].rating
    assert r["s"].wins == 9 and r["s"].matches == 9


def test_symmetric_round_robin_with_ties_is_equal():
    items = ["a", "b", "c", "d"]
    outcomes = [(x, y, 0.5) for i, x in enumerate(items) for y in items[i + 1:]]
    r = fit_bradley_terry(items, outcomes)
    assert all(abs(v.rating - 1500) < 1e-6 for v in r.values())
    assert all(v.wins == 1.5 for v in r.values())


def test_order_independent():
    rng = random.Random(1)
    items = [f"i{k}" for k in range(8)]
    outcomes = []
    for _ in range(60):
        a, b = rng.sample(items, 2)
        outcomes.append((a, b, rng.choice([0.0, 0.5, 1.0])))
    base = fit_bradley_terry(items, outcomes)
    shuffled = outcomes[:]
    rng.shuffle(shuffled)
    other = fit_bradley_terry(list(reversed(items)), shuffled)
    for k in items:
        assert base[k].rating == pytest.approx(other[k].rating, abs=1e-6)
        assert base[k].se == pytest.approx(other[k].se, abs=1e-6)


def test_mean_is_1500_and_interval_contains_rating():
    items = ["a", "b", "c"]
    outcomes = [("a", "b", 1.0), ("b", "c", 1.0), ("a", "c", 0.5), ("c", "a", 0.0), ("b", "a", 1.0)]
    r = fit_bradley_terry(items, outcomes)
    assert sum(v.rating for v in r.values()) / 3 == pytest.approx(1500, abs=1e-6)
    for v in r.values():
        assert v.lo90 < v.rating < v.hi90
        assert v.hi90 - v.rating == pytest.approx(1.6449 * v.se)


def test_se_shrinks_with_more_matches():
    items = ["a", "b"]
    few = [("a", "b", 1.0), ("b", "a", 1.0)] * 2
    many = few * 10
    assert fit_bradley_terry(items, many)["a"].se < fit_bradley_terry(items, few)["a"].se


def test_prior_keeps_undefeated_finite():
    items = ["a", "b"]
    r = fit_bradley_terry(items, [("a", "b", 1.0)] * 20)
    assert math.isfinite(r["a"].rating) and r["a"].rating > r["b"].rating
    assert r["a"].rating - r["b"].rating < 1500
    assert math.isfinite(r["a"].se) and r["a"].se > 0


def test_unplayed_item():
    r = fit_bradley_terry(["a", "b", "z"], [("a", "b", 1.0)], prior_sd_elo=200.0)
    assert r["z"].rating == 1500 and r["z"].se == 200.0 and r["z"].matches == 0
    assert fit_bradley_terry(["a"], [])["a"].rating == 1500


def test_value_errors():
    with pytest.raises(ValueError):
        fit_bradley_terry(["a", "b"], [("a", "x", 1.0)])
    with pytest.raises(ValueError):
        fit_bradley_terry(["a", "b"], [("a", "a", 1.0)])
    with pytest.raises(ValueError):
        fit_bradley_terry(["a", "b"], [("a", "b", 0.7)])


def test_recovery_on_simulated_data():
    rng = random.Random(11)
    items = [f"i{k}" for k in range(30)]
    truth = {i: rng.gauss(0, 1.0) for i in items}
    outcomes = []
    for _ in range(1200):
        a, b = rng.sample(items, 2)
        p = 1 / (1 + math.exp(-(truth[a] - truth[b])))
        outcomes.append((a, b, 1.0 if rng.random() < p else 0.0))
    r = fit_bradley_terry(items, outcomes)
    assert spearman([truth[i] for i in items], [r[i].rating for i in items]) > 0.9


def _ratings(n):
    return {f"i{k}": Rating(f"i{k}", 1000.0 + k, 10.0, 0.0, 0.0, 1, 0.0) for k in range(n)}


def test_bands():
    b49 = bands(_ratings(49))
    assert {v[0] for v in b49.values()} == {"quintile"}
    assert {v[1] for v in b49.values()} == {1, 2, 3, 4, 5}
    assert b49["i48"][1] == 5 and b49["i0"][1] == 1
    b50 = bands(_ratings(50))
    assert {v[0] for v in b50.values()} == {"decile"}
    assert b50["i49"] == ("decile", 10) and b50["i0"] == ("decile", 1)
    assert sum(1 for v in b50.values() if v[1] == 10) == 5
    assert bands({}) == {}


def test_bands_ties_share_band():
    rs = {f"i{k}": Rating(f"i{k}", 1500.0 if k < 5 else 1600.0, 1, 0, 0, 1, 0) for k in range(10)}
    b = bands(rs)
    assert len({b[f"i{k}"] for k in range(5)}) == 1
    assert len({b[f"i{k}"] for k in range(5, 10)}) == 1
    assert b["i0"][1] < b["i9"][1]


def test_pearson_spearman():
    xs, ys = [1, 2, 3, 4, 5], [2, 4, 5, 4, 5]
    # r = 6 / sqrt(10 * 6)
    assert pearson(xs, ys) == pytest.approx(6 / math.sqrt(60))
    assert pearson(xs, [10, 8, 6, 4, 2]) == pytest.approx(-1.0)
    assert spearman([1, 2, 3, 4], [1, 4, 9, 100]) == pytest.approx(1.0)
    # ties: ranks of ys are 1, 2.5, 4, 2.5, 4 -> Pearson of ranks
    rx = [1, 2, 3, 4, 5]
    assert spearman(xs, [1, 2, 5, 2, 5]) == pytest.approx(pearson(rx, [1.0, 2.5, 4.5, 2.5, 4.5]))
    assert pearson([1, 2], [1, 2]) is None
    assert pearson([1, 1, 1], [1, 2, 3]) is None
    assert spearman([1, 2, 3], [5, 5, 5]) is None
    assert spearman([1, 2], [1, 2]) is None


def test_flip_rate():
    assert flip_rate([]) is None
    assert flip_rate([("a", "a"), ("a", "b"), ("tie", "a"), ("b", "b")]) == 0.5
    assert flip_rate([("a", "a")]) == 0.0
