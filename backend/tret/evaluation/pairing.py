"""Swiss pairing for the Engine 1 tournament: pure functions, no DB, no network.

`rounds_for(n)` is the number of rounds for a pool of n modules. `swiss_round` pairs neighbours
by score, avoiding rematches, and gives the odd player out a bye. `choose_extra` samples match
ids for the swapped re-judgements and the double-judged sample. Every function is deterministic
for a given `random.Random` seed.
"""
from __future__ import annotations

import math
import random
from collections.abc import Iterable

MAX_ROUNDS = 8
# Nodes the backtracking search may visit per allowed-repeat level before it gives up on that level.
SEARCH_BUDGET = 20_000


def rounds_for(n: int) -> int:
    """`min(8, ceil(log2(n)) + 2)` rounds for n >= 2, else 0."""
    if n < 2:
        return 0
    return min(MAX_ROUNDS, math.ceil(math.log2(n)) + 2)


def choose_extra(match_ids: list[str], rate: float, rng: random.Random) -> list[str]:
    """A random sample of `round(rate * len)` ids (at least one when rate > 0 and the list is not empty)."""
    if not match_ids or rate <= 0:
        return []
    k = max(1, round(rate * len(match_ids)))
    k = min(k, len(match_ids))
    return rng.sample(list(match_ids), k)


def _pair_up(order: list[str], played: set[frozenset[str]]) -> list[tuple[str, str]]:
    """Pair an even-length ranked list: neighbours first, fewest repeats overall."""
    n = len(order)
    if n == 0:
        return []

    def search(max_repeats: int) -> list[tuple[int, int]] | None:
        used = [False] * n
        pairs: list[tuple[int, int]] = []
        nodes = 0

        def rec(repeats: int) -> bool:
            nonlocal nodes
            i = 0
            while i < n and used[i]:
                i += 1
            if i == n:
                return True
            used[i] = True
            for j in range(i + 1, n):
                if used[j]:
                    continue
                nodes += 1
                if nodes > SEARCH_BUDGET:
                    break
                rep = frozenset((order[i], order[j])) in played
                if repeats + rep > max_repeats:
                    continue
                used[j] = True
                pairs.append((i, j))
                if rec(repeats + rep):
                    return True
                pairs.pop()
                used[j] = False
            used[i] = False
            return False

        return list(pairs) if rec(0) else None

    for allowed in range(0, n // 2 + 1):
        found = search(allowed)
        if found is not None:
            return [(order[i], order[j]) for i, j in found]
    # Search budget exhausted at every level: plain neighbour pairing.
    return [(order[i], order[i + 1]) for i in range(0, n, 2)]


def swiss_round(
    players: list[str],
    scores: dict[str, float],
    played: set[frozenset[str]],
    rng: random.Random,
    *,
    byes: Iterable[str] = frozenset(),
) -> tuple[list[tuple[str, str]], str | None]:
    """One Swiss round: (pairs, bye). Every player appears exactly once.

    Players are ranked by score descending, ties shuffled with `rng`. With an odd count the bye goes
    to the lowest-ranked player not in `byes` (the lowest-ranked player if all have had one). The rest
    are paired as neighbours, with no rematch if one can be avoided and the fewest otherwise.
    """
    shuffled = list(players)
    rng.shuffle(shuffled)
    order = sorted(shuffled, key=lambda p: -scores.get(p, 0.0))  # stable: ties keep the shuffle
    bye: str | None = None
    if len(order) % 2:
        had = set(byes)
        for p in reversed(order):
            if p not in had:
                bye = p
                break
        if bye is None:
            bye = order[-1]
        order.remove(bye)
    return _pair_up(order, played), bye
