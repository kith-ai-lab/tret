import random
import time

from tret.evaluation.pairing import choose_extra, rounds_for, swiss_round


def pk(a, b):
    return frozenset((a, b))


def test_rounds_for():
    assert [rounds_for(n) for n in (0, 1)] == [0, 0]
    assert rounds_for(2) == 3
    assert rounds_for(8) == 5
    assert rounds_for(9) == 6
    assert rounds_for(50) == 8
    assert rounds_for(300) == 8


def _check_cover(players, pairs, bye):
    seen = [p for pr in pairs for p in pr] + ([bye] if bye else [])
    assert sorted(seen) == sorted(players)


def test_everyone_once_and_bye_only_for_odd():
    for n in (2, 5, 8, 11):
        players = [f"p{i}" for i in range(n)]
        pairs, bye = swiss_round(players, {}, set(), random.Random(1))
        _check_cover(players, pairs, bye)
        assert (bye is None) == (n % 2 == 0)


def test_no_repeats_when_avoidable():
    players = [f"p{i}" for i in range(8)]
    scores = {p: 0.0 for p in players}
    rng = random.Random(3)
    played = set()
    for _ in range(5):
        pairs, bye = swiss_round(players, scores, played, rng)
        assert not any(pk(a, b) in played for a, b in pairs)
        played |= {pk(a, b) for a, b in pairs}


def test_fewest_repeats_when_unavoidable():
    players = ["a", "b", "c", "d"]
    # Only the matchings a-b/c-d and a-c/b-d are used up; a-d/b-c is still free.
    played = {pk("a", "b"), pk("c", "d"), pk("a", "c"), pk("b", "d")}
    pairs, _ = swiss_round(players, {}, played, random.Random(0))
    assert {pk(*p) for p in pairs} == {pk("a", "d"), pk("b", "c")}
    # Everything played: all pairings repeat, still a full cover.
    allp = {pk(x, y) for x in players for y in players if x != y}
    pairs, bye = swiss_round(players, {}, allp, random.Random(0))
    _check_cover(players, pairs, bye)
    # Three of four matchings blocked in a way that forces exactly one repeat.
    played = {pk("a", "b"), pk("a", "c"), pk("a", "d"), pk("b", "c"), pk("b", "d")}
    pairs, _ = swiss_round(players, {}, played, random.Random(0))
    assert sum(pk(*p) in played for p in pairs) == 1


def test_deterministic_for_seed():
    players = [f"p{i}" for i in range(21)]
    scores = {p: float(i % 3) for i, p in enumerate(players)}
    r1 = swiss_round(players, scores, set(), random.Random(42))
    r2 = swiss_round(players, scores, set(), random.Random(42))
    assert r1 == r2


def test_bye_rotation():
    players = [f"p{i}" for i in range(5)]
    scores = {p: float(i) for i, p in enumerate(players)}
    byes = set()
    for _ in range(5):
        _, bye = swiss_round(players, scores, set(), random.Random(1), byes=byes)
        assert bye not in byes
        byes.add(bye)
    assert byes == set(players)
    # Lowest score gets the first bye.
    _, bye = swiss_round(players, scores, set(), random.Random(1))
    assert bye == "p0"
    # Once everyone has had one, a bye is still assigned.
    _, bye = swiss_round(players, scores, set(), random.Random(1), byes=set(players))
    assert bye == "p0"


def test_n300_fast():
    players = [f"p{i}" for i in range(300)]
    rng = random.Random(7)
    scores = {p: float(rng.randint(0, 4)) for p in players}
    played = set()
    t0 = time.perf_counter()
    for _ in range(3):
        pairs, bye = swiss_round(players, scores, played, rng)
        played |= {pk(a, b) for a, b in pairs}
        _check_cover(players, pairs, bye)
    assert (time.perf_counter() - t0) / 3 < 1.0


def test_n301_fast_with_heavy_history():
    players = [f"p{i}" for i in range(301)]
    rng = random.Random(8)
    played = set()
    for _ in range(7):
        pairs, _ = swiss_round(players, {}, played, rng)
        played |= {pk(a, b) for a, b in pairs}
    t0 = time.perf_counter()
    pairs, bye = swiss_round(players, {}, played, rng)
    assert time.perf_counter() - t0 < 1.0
    _check_cover(players, pairs, bye)


def test_neighbours_similar_scores():
    players = [f"p{i}" for i in range(40)]
    scores = {p: float(i) for i, p in enumerate(players)}
    pairs, _ = swiss_round(players, scores, set(), random.Random(5))
    assert all(abs(scores[a] - scores[b]) == 1.0 for a, b in pairs)


def test_choose_extra():
    ids = [f"m{i}" for i in range(100)]
    got = choose_extra(ids, 0.05, random.Random(1))
    assert len(got) == 5 and len(set(got)) == 5 and set(got) <= set(ids)
    assert got == choose_extra(ids, 0.05, random.Random(1))
    assert len(choose_extra(["x", "y"], 0.01, random.Random(1))) == 1
    assert choose_extra([], 0.5, random.Random(1)) == []
    assert choose_extra(ids, 0, random.Random(1)) == []
    assert len(choose_extra(ids, 2.0, random.Random(1))) == 100
