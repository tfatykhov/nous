"""Paired bootstrap, percentiles, ratios and judge agreement. Pure functions, no I/O."""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Sequence
from typing import Any

SEED = 20261105
N_BOOT = 2000


def pct(xs: Sequence[float], p: float) -> float:
    """Nearest-rank percentile (the harnesses' definition); NaN for an empty list."""
    s = sorted(xs)
    return s[min(len(s) - 1, int(p * len(s)))] if s else math.nan


def ratio(pairs: Sequence[tuple[float, float]]) -> float:
    """sum(numerators) / sum(denominators): a micro average. 0.0 when nothing was counted."""
    num, den = sum(a for a, _ in pairs), sum(b for _, b in pairs)
    return num / den if den else 0.0


def mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else math.nan


def paired_bootstrap(
    pairs: Sequence[tuple[Any, Any]],
    stat: Callable[[list[Any]], float],
    *,
    n_boot: int = N_BOOT,
    seed: int = SEED,
) -> tuple[float, float, float]:
    """(point, lo, hi) of ``stat(B) - stat(A)`` over paired units (A, B), with a 95% percentile
    interval from resampling the units with replacement. The pairing is kept: a resample draws
    whole units, so both arms see the same queries."""
    if not pairs:
        raise ValueError("paired_bootstrap needs at least one pair")

    def delta(sample: Sequence[tuple[Any, Any]]) -> float:
        return stat([b for _, b in sample]) - stat([a for a, _ in sample])

    n = len(pairs)
    rng = random.Random(seed)
    ds = sorted(delta([pairs[rng.randrange(n)] for _ in range(n)]) for _ in range(n_boot))
    return delta(pairs), ds[int(0.025 * n_boot)], ds[int(0.975 * n_boot) - 1]


def kappa(primary: dict[Any, bool], alt: dict[Any, bool]) -> dict[str, Any]:
    """Raw agreement and Cohen's kappa over the keys both label sets hold."""
    shared = [k for k in primary if k in alt]
    n = len(shared)
    if not n:
        return {"shared": 0, "agreement": None, "kappa": None, "primary_rel_rate": None, "alt_rel_rate": None}
    tt = sum(1 for k in shared if primary[k] and alt[k])
    tf = sum(1 for k in shared if primary[k] and not alt[k])
    ft = sum(1 for k in shared if not primary[k] and alt[k])
    ff = n - tt - tf - ft
    po = (tt + ff) / n
    pe = ((tt + tf) * (tt + ft) + (ft + ff) * (tf + ff)) / n / n
    return {
        "shared": n,
        "agreement": po,
        "kappa": (po - pe) / (1 - pe) if pe < 1 else None,
        "primary_rel_rate": (tt + tf) / n,
        "alt_rel_rate": (tt + ft) / n,
    }
