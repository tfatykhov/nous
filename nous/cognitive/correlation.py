"""F024 Phase 3b — Pure-Python correlation engine.

Computes Pearson and Spearman correlations between rubric dimension
scores and outcome signals. No scipy/numpy dependency.
"""
from __future__ import annotations

import math
from collections import defaultdict
from typing import Any

from nous.cognitive.rubric_schemas import CorrelationResult


def pearson_r(x: list[float], y: list[float]) -> float:
    """Compute Pearson correlation coefficient. Returns 0.0 on degenerate input."""
    n = len(x)
    if n < 2 or len(y) != n:
        return 0.0

    mean_x = sum(x) / n
    mean_y = sum(y) / n

    num = sum((xi - mean_x) * (yi - mean_y) for xi, yi in zip(x, y))
    den_x = math.sqrt(sum((xi - mean_x) ** 2 for xi in x))
    den_y = math.sqrt(sum((yi - mean_y) ** 2 for yi in y))

    if den_x == 0 or den_y == 0:
        return 0.0

    return num / (den_x * den_y)


def _rank(values: list[float]) -> list[float]:
    """Compute fractional ranks for a list of values."""
    n = len(values)
    indexed = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n

    i = 0
    while i < n:
        j = i
        while j < n - 1 and values[indexed[j]] == values[indexed[j + 1]]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[indexed[k]] = avg_rank
        i = j + 1

    return ranks


def spearman_rho(x: list[float], y: list[float]) -> float:
    """Compute Spearman rank correlation. Falls back to Pearson on ranks."""
    if len(x) < 2:
        return 0.0
    return pearson_r(_rank(x), _rank(y))


def correlate_dimensions_with_outcomes(
    episodes: list[dict[str, Any]],
    dimension_names: list[str],
) -> list[CorrelationResult]:
    """Correlate dimension scores with outcome signal presence.

    Each episode dict must have:
    - "scores": dict mapping dimension name to numeric score
    - "signals": list of signal type strings
    """
    all_signal_types = set()
    for ep in episodes:
        for sig in ep.get("signals", []):
            all_signal_types.add(sig)

    results = []
    for dim in dimension_names:
        dim_scores = [ep["scores"].get(dim, 0) for ep in episodes]

        for sig_type in sorted(all_signal_types):
            sig_binary = [
                1.0 if sig_type in ep.get("signals", []) else 0.0
                for ep in episodes
            ]

            r = pearson_r(dim_scores, sig_binary)
            rho = spearman_rho(dim_scores, sig_binary)

            results.append(CorrelationResult(
                dimension=dim,
                signal_type=sig_type,
                pearson_r=round(r, 4),
                spearman_rho=round(rho, 4),
                sample_size=len(episodes),
            ))

    return results


def suggest_weights(
    correlations: list[CorrelationResult],
    current_weights: dict[str, float],
    cap: float = 0.05,
    min_weight: float = 0.10,
    max_weight: float = 0.40,
) -> dict[str, float]:
    """Suggest new weights based on correlation strength.

    Uses average |pearson_r| per dimension as importance signal.
    Shifts weights toward higher-correlation dimensions,
    capped at ±cap per adjustment cycle.
    """
    dim_importance: dict[str, float] = {}
    dim_counts: dict[str, int] = {}
    for c in correlations:
        dim_importance[c.dimension] = dim_importance.get(c.dimension, 0) + abs(c.pearson_r)
        dim_counts[c.dimension] = dim_counts.get(c.dimension, 0) + 1

    for dim in dim_importance:
        if dim_counts[dim] > 0:
            dim_importance[dim] /= dim_counts[dim]

    if not dim_importance:
        return dict(current_weights)

    total_importance = sum(dim_importance.values())
    if total_importance == 0:
        return dict(current_weights)

    target_weights = {
        dim: imp / total_importance
        for dim, imp in dim_importance.items()
    }

    new_weights = {}
    for dim, current in current_weights.items():
        target = target_weights.get(dim, current)
        delta = target - current
        clamped_delta = max(-cap, min(cap, delta))
        new_weight = max(min_weight, min(max_weight, current + clamped_delta))
        new_weights[dim] = round(new_weight, 4)

    total = sum(new_weights.values())
    if total > 0:
        new_weights = {d: round(w / total, 4) for d, w in new_weights.items()}

    return new_weights


def detect_split_candidates(
    correlations: list[CorrelationResult],
    threshold: float = 0.3,
) -> list[str]:
    """Detect dimensions whose sub-signals show divergent correlations."""
    dim_rs: dict[str, list[float]] = defaultdict(list)
    for c in correlations:
        dim_rs[c.dimension].append(abs(c.pearson_r))

    candidates = []
    for dim, rs in dim_rs.items():
        if len(rs) >= 2 and (max(rs) - min(rs)) > threshold:
            candidates.append(dim)

    return candidates


def detect_merge_candidates(
    dim_profiles: dict[str, list[float]],
    threshold: float = 0.85,
) -> list[tuple[str, str]]:
    """Detect dimension pairs whose correlation profiles are highly similar."""
    dims = list(dim_profiles.keys())
    merges = []

    for i in range(len(dims)):
        for j in range(i + 1, len(dims)):
            r = pearson_r(dim_profiles[dims[i]], dim_profiles[dims[j]])
            if r > threshold:
                merges.append((dims[i], dims[j]))

    return merges
