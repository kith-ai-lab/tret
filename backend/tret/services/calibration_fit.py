"""Deterministic Jegham v1 boundary normalization and fit diagnostics."""
from __future__ import annotations

import itertools
import math


def _least_squares(columns: list[list[float]], y: list[float]) -> list[float] | None:
    n = len(columns)
    matrix = [
        [sum(a * b for a, b in zip(columns[i], columns[j])) for j in range(n)]
        + [sum(a * b for a, b in zip(columns[i], y))]
        for i in range(n)
    ]
    for i in range(n):
        pivot = max(range(i, n), key=lambda row: abs(matrix[row][i]))
        matrix[i], matrix[pivot] = matrix[pivot], matrix[i]
        if abs(matrix[i][i]) < 1e-20:
            return None
        divisor = matrix[i][i]
        matrix[i] = [value / divisor for value in matrix[i]]
        for row in range(n):
            if row == i:
                continue
            multiplier = matrix[row][i]
            matrix[row] = [
                value - multiplier * basis
                for value, basis in zip(matrix[row], matrix[i])
            ]
    return [matrix[i][-1] for i in range(n)]


def _nnls(columns: list[list[float]], y: list[float]) -> tuple[list[float], float]:
    best: tuple[list[float], float] | None = None
    for count in range(1, len(columns) + 1):
        for active in itertools.combinations(range(len(columns)), count):
            solution = _least_squares([columns[index] for index in active], y)
            if solution is None or any(value < 0 for value in solution):
                continue
            full = [0.0] * len(columns)
            for index, value in zip(active, solution):
                full[index] = value
            residual = sum(
                (sum(coef * column[row] for coef, column in zip(full, columns)) - y[row]) ** 2
                for row in range(len(y))
            )
            if best is None or residual < best[1]:
                best = full, residual
    if best is None:  # pragma: no cover - every shipped dataset has a feasible zero solution
        return [0.0] * len(columns), sum(value * value for value in y)
    return best


def fit_observation(shapes, facility_means, pue: float, input_weight: float = 0.05) -> dict:
    node = [float(value) / pue for value in facility_means]
    weighted = [(output + input_weight * input_) / 1_000_000 for input_, output in shapes]
    coefficient = max(
        0.0,
        sum(x * y for x, y in zip(weighted, node)) / sum(x * x for x in weighted),
    )
    fixed_residuals = [coefficient * x - y for x, y in zip(weighted, node)]
    input_col = [shape[0] / 1_000_000 for shape in shapes]
    output_col = [shape[1] / 1_000_000 for shape in shapes]
    free, free_rss = _nnls([input_col, output_col], node)
    intercept, intercept_rss = _nnls([input_col, output_col, [1.0] * len(node)], node)
    return {
        "node_it_wh_mean": node,
        "fixed_weight": {
            "input_weight": input_weight,
            "wh_per_mtok": coefficient,
            "residual_wh": fixed_residuals,
            "rmse_wh": math.sqrt(sum(value * value for value in fixed_residuals) / len(node)),
        },
        "nnls_free_input_output": {
            "input_wh_per_mtok": free[0], "output_wh_per_mtok": free[1],
            "rmse_wh": math.sqrt(free_rss / len(node)),
        },
        "nnls_with_intercept": {
            "input_wh_per_mtok": intercept[0], "output_wh_per_mtok": intercept[1],
            "intercept_wh": intercept[2], "rmse_wh": math.sqrt(intercept_rss / len(node)),
            "deployment_status": "diagnostic_only_three_parameters_three_observations",
        },
    }


def fit_manifest(source: dict) -> dict:
    diagnostics = {}
    for model, row in source["observations"].items():
        provider = source["providers"][row["provider"]]
        diagnostics[model] = fit_observation(
            source["shapes"], row["facility_wh_mean"], provider["pue"]
        )
    return diagnostics
