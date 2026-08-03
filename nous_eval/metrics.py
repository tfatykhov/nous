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
from dataclasses import dataclass, field
from statistics import mean, median

from nous_eval.retrieval_runner import QrelResult

# N7: k values reported alongside recall@served, so a run shows where its
# fixed-k cutoff stops seeing things rather than reporting one depth as if
# it were the whole picture. These are REPORTING points; the depth a run is
# actually scored at (and therefore the cutline ``leg_visibility`` judges
# against) is the harness's ``top_k``, not the shallowest entry here.
RECALL_CURVE_KS: tuple[int, ...] = (3, 5, 10, 20, 40, 60)


@dataclass(frozen=True)
class MetricsResult:
    """Aggregate metrics for a single (config, source) cell.

    ``n_errored`` is the count of qrels that raised inside the pipeline and
    were excluded from metric aggregates. A non-zero value is a red flag —
    the report surfaces it prominently.

    N7: ``r_at_served`` and ``recall_curve`` exist because production does
    NOT truncate — ``recall_deep`` hands the model the whole served block
    (median ~77 rows), so a fixed top-K metric measures a window prod never
    applies. Legs that band below the cutline are invisible to it, and a
    null from such a leg is uninformative rather than negative.
    ``mean_served`` records how many rows that block actually held.
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
    # N7: recall over the full served block, and the curve that shows where
    # the fixed-k cutoff stops seeing things.
    r_at_served: float = 0.0
    mean_served: float = 0.0
    recall_curve: dict[int, float] = field(default_factory=dict)


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


def _hits_at_k(q: QrelResult, k: int) -> int:
    """Exact count of gold IDs in the first K retrieved IDs.

    Computed from ``retrieved_ids[:k] ∩ gold_ids`` — authoritative,
    no over-counting. Falls back to ``rank_of_first_gold``-based bound
    only when ``gold_ids`` is empty (test-constructed QrelResult without
    gold plumbing).
    """
    if k <= 0:
        return 0
    if q.gold_ids:
        gold_set = set(q.gold_ids)
        return sum(1 for rid in q.retrieved_ids[:k] if rid in gold_set)
    # Legacy fallback — only rank-of-first-gold known. At most 1 hit at or
    # above rank k, and that hit must be exactly at rank_of_first_gold.
    rank = q.rank_of_first_gold
    if rank is None or rank > k:
        return 0
    return 1


def _precision_from_counts(q: QrelResult, k: int) -> float:
    """P@K = |top-K ∩ gold| / K. Uses exact set intersection at rank K."""
    if k <= 0:
        return 0.0
    return _hits_at_k(q, k) / float(k)


def _recall_from_counts(q: QrelResult, k: int) -> float:
    """R@K = |top-K ∩ gold| / |gold|. Uses exact set intersection at rank K."""
    if q.n_gold_total <= 0:
        return 0.0
    return _hits_at_k(q, k) / float(q.n_gold_total)


def _ndcg_from_counts(q: QrelResult, k: int = 10) -> float:
    """nDCG@K with binary gains.

    Preferred path: compute DCG from exact (position, gold?) pairs via
    ``retrieved_ids[:k] ∩ gold_ids``. Fallback path (no gold_ids on the
    QrelResult): place the first gold at ``rank_of_first_gold`` and
    ``(n_gold_in_top_k - 1)`` subsequent golds contiguously after it — a
    worst-case placement that under-states DCG but never over-states it.
    """
    if q.n_gold_total <= 0:
        return 0.0

    if q.gold_ids:
        gold_set = set(q.gold_ids)
        dcg = 0.0
        for i, rid in enumerate(q.retrieved_ids[:k], start=1):
            if rid in gold_set:
                dcg += 1.0 / math.log2(i + 1)
        ideal_hits = min(q.n_gold_total, k)
        idcg = sum(1.0 / math.log2(i + 1) for i in range(1, ideal_hits + 1))
        return 0.0 if idcg == 0.0 else dcg / idcg

    # Legacy fallback for test-constructed QrelResults without gold_ids.
    if q.n_gold_in_top_k <= 0:
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
    # N7: recall over the full served block. ``retrieved_ids`` is already the
    # untruncated served list, so this needs no re-run — it is the same data
    # scored without the artificial window.
    served_recalls: list[float] = []
    served_lengths: list[float] = []

    for q in valid:
        rrs.append(_reciprocal_rank(q))
        p1s.append(_precision_from_counts(q, 1))
        p5s.append(_precision_from_counts(q, 5))
        p10s.append(_precision_from_counts(q, top_k))
        r1s.append(_recall_from_counts(q, 1))
        r5s.append(_recall_from_counts(q, 5))
        r10s.append(_recall_from_counts(q, top_k))
        ndcgs.append(_ndcg_from_counts(q, top_k))
        served = len(q.retrieved_ids)
        served_lengths.append(float(served))
        served_recalls.append(_recall_from_counts(q, served))

    curve = {
        k: mean([_recall_from_counts(q, k) for q in valid])
        for k in RECALL_CURVE_KS
    }

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
        r_at_served=mean(served_recalls),
        mean_served=mean(served_lengths),
        recall_curve=curve,
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
# N7: leg visibility
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LegVisibility:
    """Whether the scoring window actually observed one retrieval leg.

    The load-bearing field is ``participation_rate``: the fraction of qrels
    where this leg placed AT LEAST ONE row within ``cutoff``. That is the
    question an operator needs answered — "could this leg have influenced
    the top-k metric?" — and it is not what a pooled median measures.

    A leg emitting ranks 1 and 20-30 on every qrel has a median far below
    the cutline yet participates in every single top-10 score; legs with
    large allotments (chunks, exemplars) are especially prone to this. The
    earlier median-of-all-rows test would have branded such a leg invisible
    and told operators to discount a perfectly valid null.

    ``visible`` is therefore True when the leg reached the window on at
    least one qrel. Read it together with ``participation_rate`` — a leg at
    0.03 was technically observed but a null for it is still largely
    uninformative. ``median_rank`` / ``best_rank`` are retained as
    diagnostics only.
    """

    leg: str
    n_rows: int
    n_qrels_present: int
    n_qrels_within_cutoff: int
    participation_rate: float
    median_rank: float
    best_rank: int
    cutoff: int
    visible: bool


def leg_visibility(
    per_qrel: list[QrelResult],
    cutoff: int = 10,
) -> list[LegVisibility]:
    """Report each leg's rank distribution against the scoring cutline.

    ``cutoff`` defaults to 10 — ``run_matrix``'s default ``top_k``, i.e. the
    window this harness actually scores at. Callers running a different
    ``top_k`` should pass it, since that is the depth their nulls are
    conditioned on. (The shallower points in ``RECALL_CURVE_KS`` are
    reporting detail; scoring at k=3 would mark nearly every leg invisible
    and the check would stop discriminating.)

    Ranks are 1-based positions in the served block. Legs are labelled by
    ``QrelResult.retrieved_legs`` (populated by the runner from the
    pipeline's own provenance markers). Returns one row per observed leg,
    lowest participation first, so the least-observed legs read at the top.

    Visibility is computed PER QREL — a leg counts as observed on a qrel
    when any of its rows lands at rank <= cutoff — and then aggregated into
    ``participation_rate``. Pooling every row and taking one median would
    let a leg's own tail hide its head (see ``LegVisibility``).

    Callers that treat this as a gate should fail when a run reports a null
    for a leg whose ``participation_rate`` is 0 (or near it) — that
    combination is the specific error N7 exists to prevent.
    """
    ranks_by_leg: dict[str, list[int]] = {}
    present: dict[str, int] = {}
    within: dict[str, int] = {}

    for q in per_qrel:
        if q.error is not None:
            continue
        best_in_qrel: dict[str, int] = {}
        for pos, leg in enumerate(q.retrieved_legs, start=1):
            ranks_by_leg.setdefault(leg, []).append(pos)
            if leg not in best_in_qrel or pos < best_in_qrel[leg]:
                best_in_qrel[leg] = pos
        for leg, best in best_in_qrel.items():
            present[leg] = present.get(leg, 0) + 1
            if best <= cutoff:
                within[leg] = within.get(leg, 0) + 1

    out = []
    for leg, ranks in ranks_by_leg.items():
        n_present = present.get(leg, 0)
        n_within = within.get(leg, 0)
        rate = (n_within / n_present) if n_present else 0.0
        out.append(
            LegVisibility(
                leg=leg,
                n_rows=len(ranks),
                n_qrels_present=n_present,
                n_qrels_within_cutoff=n_within,
                participation_rate=rate,
                median_rank=float(median(ranks)),
                best_rank=min(ranks),
                cutoff=cutoff,
                visible=n_within > 0,
            )
        )
    # Least-observed first; median as a stable tiebreak (deepest first).
    out.sort(key=lambda v: (v.participation_rate, -v.median_rank))
    return out


# ---------------------------------------------------------------------------
# Filtering helpers — used by report.py's gate-decision logic
# ---------------------------------------------------------------------------


def filter_by_sources(
    per_qrel: list[QrelResult],
    source_names: set[str],
) -> list[QrelResult]:
    """Return only qrels whose ``qrel_source`` is in ``source_names``."""
    return [q for q in per_qrel if q.qrel_source in source_names]
