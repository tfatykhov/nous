"""Paired-comparison statistics for scripts/diag/arm_separation.py.

Pure functions, no I/O except `main`, which pools the per-query dumps several
`arm_separation.py --out-json` runs wrote:

    python scripts/diag/ab_stats.py --arm or_fallback a.json b.json c.json

Why not a "noise floor": a floor measured once on one qrel set (+-0.005 at n=57)
does not transfer to another set or size. A paired bootstrap CI over the actual
per-query reciprocal-rank differences is computed from the run itself.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import sys
from pathlib import Path


def bootstrap_ci(deltas: list[float], n_boot: int = 10_000, seed: int = 20261016,
                 alpha: float = 0.05) -> tuple[float, float, float]:
    """(mean, lo, hi): percentile bootstrap CI of the mean paired difference."""
    if not deltas:
        return (0.0, 0.0, 0.0)
    rng = random.Random(seed)
    n = len(deltas)
    means = sorted(sum(deltas[rng.randrange(n)] for _ in range(n)) / n for _ in range(n_boot))
    lo = means[int((alpha / 2) * n_boot)]
    hi = means[min(n_boot - 1, int((1 - alpha / 2) * n_boot))]
    return (sum(deltas) / n, lo, hi)


def sign_test_p(better: int, worse: int) -> float:
    """Two-sided exact binomial sign test on the non-tied queries."""
    n = better + worse
    if n == 0:
        return 1.0
    k = min(better, worse)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def percentile(xs: list[float], q: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    return s[min(len(s) - 1, int(q * len(s)))]


def pooled_deltas(dumps: list[dict], arm: str, against: str = "baseline") -> list[float]:
    """Per-query rr(arm) - rr(against) over each dump's shared population."""
    out: list[float] = []
    for d in dumps:
        a, b = d["arms"][arm]["rr"], d["arms"][against]["rr"]
        out.extend(a[i] - b[i] for i in d["shared"])
    return out


def fired_summary(baseline_kw, arm_kw, shared) -> tuple[int, int]:
    """(eligible, fired): of the queries whose BASELINE all-words keyword sub-query
    found nothing (the only ones a fallback can act on), how many the arm added
    rows to."""
    eligible = [i for i in shared if baseline_kw[i]["zero"] > 0]
    return len(eligible), sum(1 for i in eligible if arm_kw[i]["fallback"] > 0)


def pooled_fired(dumps: list[dict], arm: str, against: str = "baseline") -> tuple[int, int]:
    """`fired_summary` summed over each dump's shared population."""
    eligible = fired = 0
    for d in dumps:
        e, f = fired_summary(d["arms"][against]["kw"], d["arms"][arm]["kw"], d["shared"])
        eligible, fired = eligible + e, fired + f
    return eligible, fired


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--arm", required=True)
    p.add_argument("--against", default="baseline")
    p.add_argument("dumps", nargs="+")
    a = p.parse_args()
    dumps = [json.loads(Path(f).read_text(encoding="utf-8")) for f in a.dumps]
    d = pooled_deltas(dumps, a.arm, a.against)
    if not d:
        # every query dropped (error / expansion drift) in every dump
        print("no shared queries in any dump: nothing to pool", file=sys.stderr)
        return 2
    mean, lo, hi = bootstrap_ci(d)
    better = sum(1 for x in d if x > 1e-9)
    worse = sum(1 for x in d if x < -1e-9)
    print(f"pooled n={len(d)} dMRR={mean:+.4f} 95% CI [{lo:+.4f}, {hi:+.4f}] "
          f"better={better} worse={worse} ties={len(d) - better - worse} "
          f"sign-test p={sign_test_p(better, worse):.4f}")
    added = []
    for dd in dumps:
        x, y = dd["arms"][a.arm]["ms"], dd["arms"][a.against]["ms"]
        added.extend(x[i] - y[i] for i in dd["shared"])
    print(f"added latency per recall: mean {statistics.mean(added):.1f} ms, "
          f"p95 {percentile(added, 0.95):.1f} ms, max {max(added):+.1f} ms (n={len(added)})")
    elig, fired = pooled_fired(dumps, a.arm, a.against)
    print(f"keyword fallback fired on {fired}/{elig} pooled queries with a zero-hit "
          f"{a.against} sub-query" + (f" = {fired / elig:.0%}" if elig else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
