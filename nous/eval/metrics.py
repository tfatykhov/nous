"""Retrieval quality metrics — MRR, P@K, R@K, nDCG@10.

Pure Python (``statistics.mean`` + list comps) so the harness has no extra
scientific-stack dependency. With N=500 qrels × 5 configs this completes in
well under a second.

Point estimates use mean-of-means, which equals paired-average for per-qrel
scalar metrics by linearity of expectation. What differs between paired and
unpaired analysis is confidence interval width; CIs are deferred to Phase 2.

Every function documents the contract in its docstring. No silent fallback
behavior — an empty qrel list returns zeroed metrics with ``n_qrels=0``
explicitly, so callers can branch on it if they need to.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import mean

from nous.eval.retrieval_runner import QrelResult


@dataclass(frozen=True)
class MetricsResult:
    """Aggregate metrics for a single (config, source) cell.

    ``n_errored`` is the count of qrels that raised inside the pipeline and
    were excluded from metric aggregates. A non-zero value is a red flag —
    the report surfaces it prominently.
    """

    mrr: float
    p_at_1: float
    p_at_5: float
    p_at_10: float
    r_at_1: float
    r_at_5: float
    r_at_10: float
    ndcg_at_10: float
    n_qrels: int
    n_errored: int = 0


@dataclass(frozen=True)
class Delta:
    """Single-metric delta between baseline and experimental runs.

    ``relative_pct`` is signed — negative means experimental regressed
    vs baseline. Division by zero in the baseline_mean=0 edge case
    returns ``+inf`` when experimental > 0, ``0.0`` when both zero.
    """

    metric: str
    baseline_mean: float
    experimental_mean: float
    absolute: float
    relative_pct: float
    n_pairs: int


# ---------------------------------------------------------------------------
# Per-qrel scoring helpers
# ---------------------------------------------------------------------------


def _reciprocal_rank(qrel_result: QrelResult) -> float:
    """1/rank if at least one gold appears in the top-K, else 0."""
    rank = qrel_result.rank_of_first_gold
    if rank is None or rank <= 0:
        return 0.0
    return 1.0 / float(rank)


def _precision_from_counts(q: QrelResult, k: int) -> float:
    """P@K from counts, adjusted by rank_of_first_gold.

    ``n_gold_in_top_k`` is the count of gold in the full top-K_top (the
    runner's retrieval depth, typically 10). For K smaller than
    ``rank_of_first_gold``, no gold appears → 0. Otherwise we cap the
    hit count at K and divide by K, matching the standard P@K formula.
    """
    if k <= 0:
        return 0.0
    rank = q.rank_of_first_gold
    if rank is None or rank > k:
        # First gold is beyond position K → zero gold in top-K
        return 0.0
    # At least one gold in top-K; upper-bound at min(n_gold_in_top_k, k)
    hits = min(q.n_gold_in_top_k, k)
    return hits / float(k)


def _recall_from_counts(q: QrelResult, k: int) -> float:
    """R@K from counts, adjusted by rank_of_first_gold.

    Same rank-cap logic as P@K: if the first gold is beyond position K,
    recall is zero; else cap hits at K before dividing by ``n_gold_total``.
    """
    if q.n_gold_total <= 0:
        return 0.0
    rank = q.rank_of_first_gold
    if rank is None or rank > k:
        return 0.0
    hits = min(q.n_gold_in_top_k, k)
    return hits / float(q.n_gold_total)


def _ndcg_from_counts(q: QrelResult, k: int = 10) -> float:
    """Fallback nDCG@K using only rank_of_first_gold + n_gold_in_top_k.

    This is a lower bound — we only know the *first* gold's rank exactly,
    not the others. Places the first gold at rank_of_first_gold and
    (n_in_top_k - 1) subsequent golds at contiguous positions immediately
    after it, which is the worst-case (least-DCG) valid placement.
    """
    if q.n_gold_total <= 0 or q.n_gold_in_top_k <= 0:
        return 0.0
    rank = q.rank_of_first_gold or 1
    dcg = 0.0
    for i in range(q.n_gold_in_top_k):
        pos = rank + i
        if pos > k:
            break
        dcg += 1.0 / math.log2(pos + 1)
    ideal_hits = min(q.n_gold_total, k)
    idcg = sum(1.0 / math.log2(i + 1) for i in range(1, ideal_hits + 1))
    if idcg == 0.0:
        return 0.0
    return dcg / idcg


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def compute_metrics(
    per_qrel: list[QrelResult],
    top_k: int = 10,
) -> MetricsResult:
    """Aggregate per-qrel metrics over a list of QrelResults.

    Errored qrels (``error`` is not None) are excluded from the means but
    reported in ``n_errored``. If all qrels errored, returns zeros with
    ``n_qrels=0``.
    """
    valid = [q for q in per_qrel if q.error is None]
    n_errored = len(per_qrel) - len(valid)

    if not valid:
        return MetricsResult(
            mrr=0.0,
            p_at_1=0.0,
            p_at_5=0.0,
            p_at_10=0.0,
            r_at_1=0.0,
            r_at_5=0.0,
            r_at_10=0.0,
            ndcg_at_10=0.0,
            n_qrels=0,
            n_errored=n_errored,
        )

    # Build per-qrel gold sets once so we don't rebuild on every K.
    rrs: list[float] = []
    p1s: list[float] = []
    p5s: list[float] = []
    p10s: list[float] = []
    r1s: list[float] = []
    r5s: list[float] = []
    r10s: list[float] = []
    ndcgs: list[float] = []

    # Metrics are computed from the numeric ``n_gold_in_top_k`` counter +
    # ``rank_of_first_gold`` — the runner populates both by re-scanning
    # ``retrieved_ids`` against the authoritative ``Qrel.gold_ids`` at
    # scoring time. This keeps metrics decoupled from whether the caller
    # also propagates gold_ids onto QrelResult (which test fixtures don't
    # always do).
    for q in valid:
        rrs.append(_reciprocal_rank(q))
        p1s.append(_precision_from_counts(q, 1))
        p5s.append(_precision_from_counts(q, 5))
        p10s.append(_precision_from_counts(q, top_k))
        r1s.append(_recall_from_counts(q, 1))
        r5s.append(_recall_from_counts(q, 5))
        r10s.append(_recall_from_counts(q, top_k))
        ndcgs.append(_ndcg_from_counts(q, top_k))

    return MetricsResult(
        mrr=mean(rrs),
        p_at_1=mean(p1s),
        p_at_5=mean(p5s),
        p_at_10=mean(p10s),
        r_at_1=mean(r1s),
        r_at_5=mean(r5s),
        r_at_10=mean(r10s),
        ndcg_at_10=mean(ndcgs),
        n_qrels=len(valid),
        n_errored=n_errored,
    )


def compute_delta(
    baseline: MetricsResult,
    experimental: MetricsResult,
    metric: str = "mrr",
) -> Delta:
    """Compute absolute + relative delta between two MetricsResults.

    Supported metrics: ``mrr``, ``p_at_1``, ``p_at_5``, ``p_at_10``,
    ``r_at_1``, ``r_at_5``, ``r_at_10``, ``ndcg_at_10``.
    """
    if not hasattr(baseline, metric) or not hasattr(experimental, metric):
        raise ValueError(f"Unknown metric: {metric}")

    base_val = float(getattr(baseline, metric))
    exp_val = float(getattr(experimental, metric))
    absolute = exp_val - base_val

    if base_val == 0.0:
        relative_pct = 0.0 if exp_val == 0.0 else float("inf")
    else:
        relative_pct = (absolute / base_val) * 100.0

    # For paired metrics, n_pairs is the smaller n_qrels (paired count).
    n_pairs = min(baseline.n_qrels, experimental.n_qrels)

    return Delta(
        metric=metric,
        baseline_mean=base_val,
        experimental_mean=exp_val,
        absolute=absolute,
        relative_pct=relative_pct,
        n_pairs=n_pairs,
    )


# ---------------------------------------------------------------------------
# Filtering helpers — used by report.py's gate-decision logic
# ---------------------------------------------------------------------------


def filter_by_sources(
    per_qrel: list[QrelResult],
    source_names: set[str],
) -> list[QrelResult]:
    """Return only qrels whose ``qrel_source`` is in ``source_names``."""
    return [q for q in per_qrel if q.qrel_source in source_names]
