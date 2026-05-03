"""Tests for nous_eval.probes.sleep_action_audit.

Mock-based tests for the aggregation + verdict logic. The judge call
itself is exercised by the live run; these tests cover the pure
classification math (quality_score formula, correct/wrong/ambiguous
buckets, NaN handling for zero-decisive cases) and the --strict
exit-code rules.
"""
from __future__ import annotations

import math

from nous_eval.probes.sleep_action_audit import (
    JudgedAction,
    aggregate_quality,
    overall_exit_code,
)


def _ja(event_type: str, verdict: str) -> JudgedAction:
    return JudgedAction(
        event_type=event_type, action_summary="x", verdict=verdict, reason=""
    )


# ---------------------------------------------------------------------------
# aggregate_quality
# ---------------------------------------------------------------------------


def test_aggregate_quality_excludes_ambiguous_from_score():
    """quality_score is correct/(correct+wrong) — ambiguous shouldn't
    inflate or dilute. A judge's 'I can't tell' should not count
    against the agent's quality."""
    judged = [
        _ja("f031_contradiction_resolution", "correct"),
        _ja("f031_contradiction_resolution", "correct"),
        _ja("f031_contradiction_resolution", "wrong"),
        _ja("f031_contradiction_resolution", "ambiguous"),  # excluded
    ]
    agg = aggregate_quality(judged)
    s = agg["f031_contradiction_resolution"]
    assert s["n"] == 4
    assert s["correct"] == 2
    assert s["wrong"] == 1
    assert s["ambiguous"] == 1
    # 2 correct / (2 + 1) decisive = 0.667
    assert math.isclose(s["quality_score"], 2 / 3)


def test_aggregate_quality_is_nan_when_all_ambiguous():
    """No decisive judgments → score is NaN (caller can't gate on it).
    This is the "judge is too uncertain to evaluate" signal."""
    judged = [
        _ja("f027_cluster_merge", "ambiguous"),
        _ja("f027_cluster_merge", "ambiguous"),
    ]
    agg = aggregate_quality(judged)
    assert math.isnan(agg["f027_cluster_merge"]["quality_score"])


def test_aggregate_quality_separates_event_types():
    """Per-phase aggregation must not mix F031 stats into F027 score."""
    judged = [
        _ja("f031_contradiction_resolution", "correct"),
        _ja("f031_contradiction_resolution", "wrong"),
        _ja("f027_cluster_merge", "correct"),
        _ja("f027_cluster_merge", "correct"),
    ]
    agg = aggregate_quality(judged)
    assert agg["f031_contradiction_resolution"]["quality_score"] == 0.5
    assert agg["f027_cluster_merge"]["quality_score"] == 1.0


def test_aggregate_quality_handles_empty_input():
    agg = aggregate_quality([])
    assert agg == {}


# ---------------------------------------------------------------------------
# overall_exit_code (--strict gate)
# ---------------------------------------------------------------------------


def test_strict_passes_when_all_phases_above_floor():
    aggregate = {
        "f031_contradiction_resolution": {"quality_score": 0.85},
        "f027_cluster_merge": {"quality_score": 0.90},
    }
    assert overall_exit_code(aggregate, floor=0.70) == 0


def test_strict_fails_when_any_phase_below_floor():
    aggregate = {
        "f031_contradiction_resolution": {"quality_score": 0.85},
        "f027_cluster_merge": {"quality_score": 0.40},
    }
    assert overall_exit_code(aggregate, floor=0.70) == 1


def test_strict_passes_when_score_is_nan():
    """NaN means 'judge couldn't decide on any sample' — don't gate
    CI on that. The probe surfaces it via the report; operator
    investigates manually rather than CI auto-failing."""
    aggregate = {
        "f031_contradiction_resolution": {"quality_score": float("nan")},
    }
    assert overall_exit_code(aggregate, floor=0.70) == 0


def test_strict_passes_on_empty_aggregate():
    """No actions in window → not a regression; just no data."""
    assert overall_exit_code({}, floor=0.70) == 0


def test_strict_passes_at_exact_floor():
    """Score == floor should pass (>= comparison). A 70% floor with
    a 70% score is acceptable; only 69.9% trips."""
    aggregate = {
        "f031_contradiction_resolution": {"quality_score": 0.70},
    }
    assert overall_exit_code(aggregate, floor=0.70) == 0
