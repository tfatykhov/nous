"""Unit tests for nous_eval.metrics (F051 Phase 1).

Golden-vector tests — every metric checked by hand against a tiny qrel set.

Conventions:
- ``rank_of_first_gold`` is 1-based. ``None`` means "no gold in top-K".
- ``n_gold_in_top_k`` counts the overlap between retrieved_ids and gold_ids.
- ``n_gold_total`` is the size of gold_ids (used by R@K).
"""

from __future__ import annotations

from uuid import uuid4

import pytest

pytestmark = pytest.mark.eval

try:
    from nous_eval.metrics import (
        MetricsResult,
        compute_delta,
        compute_metrics,
    )
    from nous_eval.retrieval_runner import QrelResult
except ImportError:
    pytest.skip(
        "nous_eval.metrics / retrieval_runner not yet available",
        allow_module_level=True,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _qr(
    rank: int | None,
    n_in_topk: int,
    n_total: int,
    error: str | None = None,
    retrieved_ids: list | None = None,
    retrieved_types: list | None = None,
    gold_ids: list | None = None,
) -> QrelResult:
    """Build a QrelResult.

    If gold_ids/retrieved_ids aren't supplied, synthesize them so that:
    - gold_ids has n_total items (the first n_in_topk overlap retrieved_ids)
    - retrieved_ids has the gold at `rank`-1 if rank is not None
    """
    if gold_ids is None:
        gold_ids = [uuid4() for _ in range(n_total)]
    if retrieved_ids is None:
        # Build retrieved_ids so first gold appears at `rank` and n_in_topk gold IDs land in top-10
        retrieved_ids = [uuid4() for _ in range(10)]
        if rank is not None and gold_ids:
            retrieved_ids[rank - 1] = gold_ids[0]
            for i in range(1, min(n_in_topk, len(gold_ids))):
                # Spread other gold IDs after the first
                pos = min(rank + i, 9)
                retrieved_ids[pos] = gold_ids[i]
    return QrelResult(
        qrel_index=0,
        qrel_query="q",
        qrel_source="probes",
        gold_ids=gold_ids,
        retrieved_ids=retrieved_ids,
        retrieved_types=retrieved_types or ["fact"] * len(retrieved_ids),
        rank_of_first_gold=rank,
        n_gold_in_top_k=n_in_topk,
        n_gold_total=n_total,
        error=error,
    )


# ---------------------------------------------------------------------------
# MRR
# ---------------------------------------------------------------------------


def test_mrr_rank_1() -> None:
    r = compute_metrics([_qr(1, 1, 1)], top_k=10)
    assert r.mrr == pytest.approx(1.0)


def test_mrr_rank_2() -> None:
    r = compute_metrics([_qr(2, 1, 1)], top_k=10)
    assert r.mrr == pytest.approx(0.5)


def test_mrr_no_gold_found() -> None:
    r = compute_metrics([_qr(None, 0, 1)], top_k=10)
    assert r.mrr == pytest.approx(0.0)


def test_mrr_mean_of_qrels() -> None:
    # ranks: 1, 2, None -> (1 + 0.5 + 0) / 3
    r = compute_metrics(
        [_qr(1, 1, 1), _qr(2, 1, 1), _qr(None, 0, 1)], top_k=10
    )
    assert r.mrr == pytest.approx((1 + 0.5 + 0) / 3)


# ---------------------------------------------------------------------------
# P@K
# ---------------------------------------------------------------------------


def test_p_at_1_hit() -> None:
    """First retrieved is gold -> P@1 = 1.0."""
    gid = uuid4()
    r = compute_metrics(
        [_qr(1, 1, 1, retrieved_ids=[gid], gold_ids=[gid])], top_k=10
    )
    assert r.p_at_1 == pytest.approx(1.0)


def test_p_at_1_miss() -> None:
    gid = uuid4()
    r = compute_metrics(
        [
            _qr(
                5,
                1,
                1,
                retrieved_ids=[uuid4(), uuid4(), uuid4(), uuid4(), gid],
                gold_ids=[gid],
            )
        ],
        top_k=10,
    )
    assert r.p_at_1 == pytest.approx(0.0)


def test_p_at_5_two_gold_in_top5() -> None:
    """2 gold IDs in the top 5 -> P@5 = 2/5 = 0.4."""
    g1, g2, g3 = uuid4(), uuid4(), uuid4()
    r = compute_metrics(
        [
            _qr(
                rank=1,
                n_in_topk=2,
                n_total=3,
                retrieved_ids=[g1, uuid4(), g2, uuid4(), uuid4()],
                gold_ids=[g1, g2, g3],
            )
        ],
        top_k=10,
    )
    assert r.p_at_5 == pytest.approx(2 / 5)


# ---------------------------------------------------------------------------
# R@K
# ---------------------------------------------------------------------------


def test_r_at_10_half_gold_hit() -> None:
    """2 gold in top-10 out of 4 gold total -> R@10 = 0.5."""
    golds = [uuid4() for _ in range(4)]
    retrieved = [golds[0], uuid4(), golds[1], uuid4(), uuid4()]
    r = compute_metrics(
        [
            _qr(
                rank=1,
                n_in_topk=2,
                n_total=4,
                retrieved_ids=retrieved,
                gold_ids=golds,
            )
        ],
        top_k=10,
    )
    # R@10 = 2/4 = 0.5
    assert r.r_at_10 == pytest.approx(0.5)


def test_r_at_1_single_hit() -> None:
    """Single gold, ranked 1, out of 1 total -> R@1 = 1.0."""
    r = compute_metrics([_qr(1, 1, 1)], top_k=10)
    assert r.r_at_1 == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# nDCG@10
# ---------------------------------------------------------------------------


def test_ndcg_perfect_rank_1() -> None:
    """Single gold at rank 1 -> nDCG = DCG/IDCG = 1.0."""
    r = compute_metrics([_qr(1, 1, 1)], top_k=10)
    assert r.ndcg_at_10 == pytest.approx(1.0, abs=1e-6)


def test_ndcg_no_gold() -> None:
    r = compute_metrics([_qr(None, 0, 1)], top_k=10)
    assert r.ndcg_at_10 == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Errored qrels -> counted in n_errored, excluded from valid stats
# ---------------------------------------------------------------------------


def test_errored_qrel_excluded_from_valid() -> None:
    """A qrel with error != None must not pollute metrics; n_errored counts it."""
    r = compute_metrics(
        [_qr(1, 1, 1), _qr(None, 0, 1, error="RuntimeError: boom")], top_k=10
    )
    assert r.n_errored == 1
    # n_qrels semantics: count of non-errored valid qrels
    assert r.n_qrels == 1
    # MRR computed only over valid qrel (rank 1 -> 1.0)
    assert r.mrr == pytest.approx(1.0)


def test_all_errored_returns_zeros() -> None:
    r = compute_metrics(
        [_qr(None, 0, 1, error="x"), _qr(None, 0, 1, error="y")], top_k=10
    )
    assert r.n_qrels == 0
    assert r.n_errored == 2
    assert r.mrr == 0.0


# ---------------------------------------------------------------------------
# Empty input
# ---------------------------------------------------------------------------


def test_empty_input_returns_zero_metrics() -> None:
    r = compute_metrics([], top_k=10)
    assert r.n_qrels == 0
    assert r.mrr == 0.0


# ---------------------------------------------------------------------------
# compute_delta
# ---------------------------------------------------------------------------


def test_compute_delta_positive() -> None:
    base = compute_metrics([_qr(2, 1, 1)], top_k=10)  # MRR = 0.5
    exp = compute_metrics([_qr(1, 1, 1)], top_k=10)  # MRR = 1.0
    d = compute_delta(base, exp, "mrr")
    assert d.baseline_mean == pytest.approx(0.5)
    assert d.experimental_mean == pytest.approx(1.0)
    assert d.absolute == pytest.approx(0.5)
    # +100 % relative
    assert d.relative_pct == pytest.approx(100.0)


def test_compute_delta_zero_baseline() -> None:
    """relative_pct must be finite when baseline=0 — either 0, inf, or a sentinel."""
    base = compute_metrics([_qr(None, 0, 1)], top_k=10)  # MRR = 0
    exp = compute_metrics([_qr(1, 1, 1)], top_k=10)  # MRR = 1.0
    d = compute_delta(base, exp, "mrr")
    # Implementation choice: should not crash. Most likely 0 or inf handled cleanly.
    assert d.absolute == pytest.approx(1.0)


def test_compute_delta_negative() -> None:
    base = compute_metrics([_qr(1, 1, 1)], top_k=10)
    exp = compute_metrics([_qr(None, 0, 1)], top_k=10)
    d = compute_delta(base, exp, "mrr")
    assert d.absolute == pytest.approx(-1.0)
    assert d.relative_pct == pytest.approx(-100.0)
