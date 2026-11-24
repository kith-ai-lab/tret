"""Bradley-Terry ratings on the Elo scale, bands and the length-bias statistics. Pure Python.

`fit_bradley_terry` is a MAP fit with an independent Gaussian prior on the log-strengths; the
standard errors come from the inverse negative Hessian at the optimum. `bands` turns ratings into
deciles (pool of 50 or more) or quintiles. `pearson` / `spearman` back the word-count-against-rating
check and `flip_rate` the swapped re-judgement check.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

ELO_PER_NAT = 400.0 / math.log(10)
Z90 = 1.6449
BASE = 1500.0
DECILE_MIN_POOL = 50


@dataclass(frozen=True)
class Rating:
    item: str
    rating: float
    se: float
    lo90: float
    hi90: float
    matches: int
    wins: float


def _cholesky(a: list[list[float]]) -> list[list[float]]:
    n = len(a)
    low: list[list[float]] = [[0.0] * n for _ in range(n)]
    for i in range(n):
        li = low[i]
        for j in range(i + 1):
            lj = low[j]
            s = sum(x * y for x, y in zip(li[:j], lj[:j]))
            if i == j:
                d = a[i][i] - s
                if d <= 0:
                    raise ValueError("Hessian is not positive definite")
                li[j] = math.sqrt(d)
            else:
                li[j] = (a[i][j] - s) / lj[j]
    return low


def _solve(low: list[list[float]], b: list[float]) -> list[float]:
    n = len(low)
    y = [0.0] * n
    for i in range(n):
        y[i] = (b[i] - sum(x * z for x, z in zip(low[i][:i], y[:i]))) / low[i][i]
    x = [0.0] * n
    for i in range(n - 1, -1, -1):
        s = sum(low[k][i] * x[k] for k in range(i + 1, n))
        x[i] = (y[i] - s) / low[i][i]
    return x


def _inverse_diagonal(low: list[list[float]]) -> list[float]:
    """diag(A^-1) for A = L L^T: the column sums of squares of L^-1."""
    n = len(low)
    diag = [0.0] * n
    for c in range(n):
        col = [0.0] * n  # column c of L^-1
        col[c] = 1.0 / low[c][c]
        for i in range(c + 1, n):
            col[i] = -sum(x * z for x, z in zip(low[i][c:i], col[c:i])) / low[i][i]
        for i in range(c, n):
            diag[c] += col[i] * col[i]
    return diag


def fit_bradley_terry(
    items: list[str],
    outcomes: list[tuple[str, str, float]],
    *,
    prior_sd_elo: float = 200.0,
    max_iter: int = 200,
    tol: float = 1e-8,
) -> dict[str, Rating]:
    """MAP Bradley-Terry fit. Outcomes are (a, b, score_a) with score_a in {0, 0.5, 1}."""
    known = set(items)
    for a, b, s in outcomes:
        if a not in known or b not in known:
            raise ValueError(f"outcome names an unknown item: {a!r} vs {b!r}")
        if a == b:
            raise ValueError(f"self-match: {a!r}")
        if s not in (0.0, 0.5, 1.0):
            raise ValueError(f"score must be 0, 0.5 or 1, got {s!r}")

    matches = {i: 0 for i in items}
    wins = {i: 0.0 for i in items}
    for a, b, s in outcomes:
        matches[a] += 1
        matches[b] += 1
        wins[a] += s
        wins[b] += 1.0 - s

    active = sorted(i for i in known if matches[i] > 0)  # sorted: order independent
    idx = {it: k for k, it in enumerate(active)}
    n = len(active)
    prior_nat = prior_sd_elo / ELO_PER_NAT
    prec = 1.0 / (prior_nat * prior_nat)
    pairs = [(idx[a], idx[b], s) for a, b, s in outcomes]

    theta = [0.0] * n
    hess: list[list[float]] = []
    for _ in range(max_iter):
        grad = [-prec * t for t in theta]
        hess = [[0.0] * n for _ in range(n)]
        for k in range(n):
            hess[k][k] = prec
        for i, j, s in pairs:
            d = theta[i] - theta[j]
            p = 1.0 / (1.0 + math.exp(-d)) if d >= 0 else math.exp(d) / (1.0 + math.exp(d))
            grad[i] += s - p
            grad[j] -= s - p
            w = p * (1.0 - p)
            hess[i][i] += w
            hess[j][j] += w
            hess[i][j] -= w
            hess[j][i] -= w
        if n == 0:
            break
        step = _solve(_cholesky(hess), grad)
        theta = [t + st for t, st in zip(theta, step)]
        if max(abs(st) for st in step) < tol:
            break

    ses: list[float] = []
    if n:
        # Hessian at the final theta (the loop's last matrix is at the previous iterate; rebuild).
        hess = [[0.0] * n for _ in range(n)]
        for k in range(n):
            hess[k][k] = prec
        for i, j, _s in pairs:
            d = theta[i] - theta[j]
            p = 1.0 / (1.0 + math.exp(-d)) if d >= 0 else math.exp(d) / (1.0 + math.exp(d))
            w = p * (1.0 - p)
            hess[i][i] += w
            hess[j][j] += w
            hess[i][j] -= w
            hess[j][i] -= w
        ses = [math.sqrt(v) * ELO_PER_NAT for v in _inverse_diagonal(_cholesky(hess))]
    shift = sum(theta) / n if n else 0.0

    out: dict[str, Rating] = {}
    for it in items:
        if it in idx:
            k = idx[it]
            r = BASE + (theta[k] - shift) * ELO_PER_NAT
            se = ses[k]
        else:
            r, se = BASE, prior_sd_elo
        out[it] = Rating(it, r, se, r - Z90 * se, r + Z90 * se, matches[it], wins[it])
    return out


def bands(ratings: dict[str, Rating]) -> dict[str, tuple[str, int]]:
    """("decile", 1..10) for a pool of 50 or more, else ("quintile", 1..5); the highest band is best.

    band = 1 + floor(k * rank / n), where rank is the 0-based rank from the bottom and tied ratings
    share the average rank of their group, so equal ratings always get the same band.
    """
    n = len(ratings)
    if n == 0:
        return {}
    kind, k = ("decile", 10) if n >= DECILE_MIN_POOL else ("quintile", 5)
    ordered = sorted(ratings, key=lambda i: ratings[i].rating)
    out: dict[str, tuple[str, int]] = {}
    pos = 0
    while pos < n:
        end = pos
        while end + 1 < n and ratings[ordered[end + 1]].rating == ratings[ordered[pos]].rating:
            end += 1
        avg = (pos + end) / 2.0
        band = min(k, 1 + int(k * avg / n))
        for m in range(pos, end + 1):
            out[ordered[m]] = (kind, band)
        pos = end + 1
    return out


def pearson(xs: list[float], ys: list[float]) -> float | None:
    """Pearson correlation; None when n < 3 or either side has no variance."""
    if len(xs) != len(ys):
        raise ValueError("xs and ys differ in length")
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return sxy / math.sqrt(sxx * syy)


def _average_ranks(vals: list[float]) -> list[float]:
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    ranks = [0.0] * len(vals)
    pos = 0
    while pos < len(order):
        end = pos
        while end + 1 < len(order) and vals[order[end + 1]] == vals[order[pos]]:
            end += 1
        for m in range(pos, end + 1):
            ranks[order[m]] = (pos + end) / 2.0 + 1.0
        pos = end + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float | None:
    """Spearman rank correlation (average ranks for ties); None when n < 3 or no variance."""
    if len(xs) != len(ys):
        raise ValueError("xs and ys differ in length")
    if len(xs) < 3:
        return None
    return pearson(_average_ranks(list(xs)), _average_ranks(list(ys)))


def flip_rate(pairs: list[tuple[str, str]]) -> float | None:
    """Share of (original, swapped re-judgement) verdict pairs that disagree; None when empty."""
    if not pairs:
        return None
    return sum(1 for a, b in pairs if a != b) / len(pairs)
