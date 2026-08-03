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
from collections.abc import Iterable
from dataclasses import dataclass, field
from statistics import mean, median

from nous_eval.retrieval_runner import QrelResult

# N7: k values reported so a run shows where its
# fixed-k cutoff stops seeing things rather than reporting one depth as if
# it were the whole picture. These are REPORTING points; the depth a run is
# actually scored at is the harness's ``top_k``, not the shallowest entry
# here.
RECALL_CURVE_KS: tuple[int, ...] = (3, 5, 10, 20, 40, 60)


@dataclass(frozen=True)
class MetricsResult:
    """Aggregate metrics for a single (config, source) cell.

    ``n_errored`` is the count of qrels that raised inside the pipeline and
    were excluded from metric aggregates. A non-zero value is a red flag —
    the report surfaces it prominently.

    N7: ``recall_curve`` exists because production does NOT truncate —
    ``recall_deep`` hands the model the whole returned block (median ~77
    rows), so a fixed top-K metric measures a window prod never applies.
    Reading recall across k shows how much of that block a given cutoff was
    blind to. Like every other metric here it is computed over
    ``retrieved_ids`` (the pipeline's output), which keeps it on the same
    basis as ``r_at_10`` / ``ndcg_at_10`` rather than quietly using a
    different denominator.

    A true ``recall@served`` — scored over the rows the formatter actually
    RENDERS — is still not here. Two attempts at deriving it in the harness
    both overstated it, because doing so means re-implementing the
    formatter's section-eligibility rules in a second place. Landing it
    correctly needs the formatter to report the IDs it emitted, which is a
    separate change from this one; see the note on ``leg_visibility``.
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
    # N7: recall across k — shows where a fixed-k cutoff stops seeing things.
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
    for q in valid:
        rrs.append(_reciprocal_rank(q))
        p1s.append(_precision_from_counts(q, 1))
        p5s.append(_precision_from_counts(q, 5))
        p10s.append(_precision_from_counts(q, top_k))
        r1s.append(_recall_from_counts(q, 1))
        r5s.append(_recall_from_counts(q, 5))
        r10s.append(_recall_from_counts(q, top_k))
        ndcgs.append(_ndcg_from_counts(q, top_k))

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
# N7 follow-up: leg visibility
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LegVisibility:
    """Whether the scoring window actually observed one retrieval leg.

    The load-bearing field is ``participation_rate``: of ALL valid qrels in
    the run, the fraction where this leg placed at least one row within
    ``cutoff``. That answers the operator's real question — "how much of the
    experiment could this leg have influenced?" — which neither a pooled
    median nor an emission-conditioned ratio answers.

    Two denominators were tried and rejected. A median over every row lets a
    leg's own long tail hide its head: a leg emitting rank 1 plus a tail at
    20-30 on every qrel scores on every query while its median sits below
    the cutline. Dividing by the qrels where the leg happened to emit is the
    opposite error: a leg reaching the cutoff on 1 qrel of 100 reads 1.00,
    "fully observed", while touching 1% of the run. The denominator is
    ``n_qrels_evaluated``; ``n_qrels_present`` is kept beside it so emission
    coverage and in-window coverage stay separately legible.

    ``visible`` is True when the leg reached the window on at least one
    qrel. Read it WITH ``participation_rate`` — a leg at 0.03 was
    technically observed but a null for it is still uninformative.
    ``median_rank`` / ``best_rank`` are diagnostics only.
    """

    leg: str
    n_rows: int
    n_qrels_evaluated: int
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
    attempted_legs: "Iterable[str] | None" = None,
) -> list[LegVisibility]:
    """Report which legs the scoring window actually observed.

    ``cutoff`` must be the depth the run scored at (``EvalSettings.top_k``);
    the verdict inverts with it, so a caller that lets it default while
    scoring at 30 turns a measured leg into a false "inconclusive".

    ``attempted_legs`` comes from ``PipelineStats.attempted_legs`` — the
    PIPELINE's own report of which legs it entered, unioned across qrels by
    the runner. It is required for correctness, not convenience: a leg that
    emitted zero rows on every qrel appears nowhere in ``retrieved_legs``,
    so without it the most extreme case of an unobserved arm is omitted from
    this report entirely — silently, and precisely when the warning matters
    most. Legs named here but never seen are emitted with ``n_rows=0`` and
    ``participation_rate=0.0`` so absence is stated rather than implied.

    An earlier version derived that set from config flags instead. That
    cannot be correct: the one-hop fallback is skipped when spreading
    activation succeeds, which is decided at runtime, so no combination of
    flags predicts it. Deriving a producer's control flow inside a consumer
    reproduced the pipeline's branching in a second place and was wrong a
    different way each time it was patched.

    Ranks are 1-based positions in the served block. Visibility is computed
    PER QREL — a leg counts as observed on a qrel when any of its rows lands
    at rank <= cutoff — then aggregated. Returns one row per leg, least
    observed first.
    """
    ranks_by_leg: dict[str, list[int]] = {}
    present: dict[str, int] = {}
    within: dict[str, int] = {}
    n_evaluated = 0

    for q in per_qrel:
        if q.error is not None:
            continue
        n_evaluated += 1
        best_in_qrel: dict[str, int] = {}
        for pos, leg in enumerate(q.retrieved_legs, start=1):
            ranks_by_leg.setdefault(leg, []).append(pos)
            if leg not in best_in_qrel or pos < best_in_qrel[leg]:
                best_in_qrel[leg] = pos
        for leg, best in best_in_qrel.items():
            present[leg] = present.get(leg, 0) + 1
            if best <= cutoff:
                within[leg] = within.get(leg, 0) + 1

    # Seed attempted-but-silent legs so zero emission is REPORTED, not omitted.
    for leg in attempted_legs or ():
        ranks_by_leg.setdefault(leg, [])

    out = []
    for leg, ranks in ranks_by_leg.items():
        n_within = within.get(leg, 0)
        out.append(
            LegVisibility(
                leg=leg,
                n_rows=len(ranks),
                n_qrels_evaluated=n_evaluated,
                n_qrels_present=present.get(leg, 0),
                n_qrels_within_cutoff=n_within,
                participation_rate=(n_within / n_evaluated) if n_evaluated else 0.0,
                # A silent leg has no ranks; report sentinels rather than
                # raising on median([]) / min([]).
                median_rank=float(median(ranks)) if ranks else 0.0,
                best_rank=min(ranks) if ranks else 0,
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
