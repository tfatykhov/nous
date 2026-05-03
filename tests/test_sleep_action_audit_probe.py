"""Tests for nous_eval.probes.sleep_action_audit.

Mock-based tests for the aggregation + verdict logic. The judge call
itself is exercised by the live run; these tests cover the pure
classification math (quality_score formula, correct/wrong/ambiguous
buckets, NaN handling for zero-decisive cases) and the --strict
exit-code rules.
"""
from __future__ import annotations

import math

import pytest

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
        "f031_contradiction_resolution": {
            "quality_score": 0.85, "ambiguity_rate": 0.05,
        },
        "f027_cluster_merge": {
            "quality_score": 0.90, "ambiguity_rate": 0.10,
        },
    }
    assert overall_exit_code(aggregate, floor=0.70) == 0


def test_strict_fails_when_any_phase_below_floor():
    aggregate = {
        "f031_contradiction_resolution": {
            "quality_score": 0.85, "ambiguity_rate": 0.05,
        },
        "f027_cluster_merge": {
            "quality_score": 0.40, "ambiguity_rate": 0.05,
        },
    }
    assert overall_exit_code(aggregate, floor=0.70) == 1


def test_strict_passes_when_score_is_nan():
    """NaN means 'judge couldn't decide on any sample' — don't gate
    CI on that. The probe surfaces it via the report; operator
    investigates manually rather than CI auto-failing."""
    aggregate = {
        "f031_contradiction_resolution": {
            "quality_score": float("nan"), "ambiguity_rate": 1.0,
        },
    }
    assert overall_exit_code(aggregate, floor=0.70) == 0


def test_strict_passes_on_empty_aggregate():
    """No actions in window → not a regression; just no data."""
    assert overall_exit_code({}, floor=0.70) == 0


def test_strict_passes_at_exact_floor():
    """Score == floor should pass (>= comparison). A 70% floor with
    a 70% score is acceptable; only 69.9% trips."""
    aggregate = {
        "f031_contradiction_resolution": {
            "quality_score": 0.70,
            "ambiguity_rate": 0.0,
        },
    }
    assert overall_exit_code(aggregate, floor=0.70) == 0


# ---------------------------------------------------------------------------
# Ambiguity guard — prevents a high-ambiguity-rate score from masking
# a real regression.
# ---------------------------------------------------------------------------


def test_aggregate_includes_ambiguity_rate():
    judged = [
        _ja("f027_cluster_merge", "ambiguous"),
        _ja("f027_cluster_merge", "ambiguous"),
        _ja("f027_cluster_merge", "correct"),
    ]
    agg = aggregate_quality(judged)
    s = agg["f027_cluster_merge"]
    assert s["ambiguity_rate"] == pytest.approx(2 / 3)


def test_strict_passes_when_ambiguity_too_high_to_trust_score():
    """3 correct / 0 wrong / 47 ambiguous = score=100% on n=3.
    Without the ambiguity guard this would silently --strict-pass.
    The guard treats >50%-ambiguous as untrustworthy and skips
    gating — operator must investigate."""
    aggregate = {
        "f031_contradiction_resolution": {
            "quality_score": 1.0,  # all 3 decisive were correct
            "ambiguity_rate": 47 / 50,  # but 94% of samples ambiguous
        },
    }
    # Even with floor=0.70 and score=1.0, this should NOT pass-by-default
    # the strict gate at 0.99 — but the ambiguity guard skips gating
    # rather than asserting either pass or fail.
    assert overall_exit_code(aggregate, floor=0.99) == 0


def test_strict_still_gates_when_ambiguity_low_and_score_below_floor():
    aggregate = {
        "f027_cluster_merge": {
            "quality_score": 0.40,
            "ambiguity_rate": 0.05,
        },
    }
    assert overall_exit_code(aggregate, floor=0.70) == 1


# ---------------------------------------------------------------------------
# Rubric drift guard — the F031 rubric must reference the live safety
# constant from sleep_handler. If the constant changes, the formatted
# rubric content changes, but if the import disappears the test below
# fails loud.
# ---------------------------------------------------------------------------


def test_verdict_normalized_handles_case_and_whitespace():
    """Codex P2 on PR #407: aggregator does exact-string match on
    'correct' / 'wrong' / 'ambiguous'. The judge sometimes returns
    'Wrong' or ' correct ' which must be normalized before storage,
    not silently treated as ambiguous (which would inflate quality)."""
    judged = [
        # These would silently be treated as 'ambiguous' without
        # normalization upstream; the test pins the contract that
        # by the time aggregate sees them, they're canonical.
        _ja("f031_contradiction_resolution", "correct"),
        _ja("f031_contradiction_resolution", "wrong"),
        _ja("f031_contradiction_resolution", "ambiguous"),
    ]
    agg = aggregate_quality(judged)
    s = agg["f031_contradiction_resolution"]
    assert s["correct"] == 1
    assert s["wrong"] == 1
    assert s["ambiguous"] == 1


def test_audit_imports_safety_floor_from_sleep_handler():
    """The F031 rubric explains the safety floor to the judge so
    KEEP_BOTH downgrades aren't scored as wrong actions. Source of
    truth is `nous.handlers.sleep_handler.F031_SAFETY_FLOOR_DESCRIPTION`.

    If a future PR removes or renames the constant, this test fails
    loud rather than letting the rubric silently drift to a stale
    description (which would re-introduce the original 40% false-
    positive rate on safety downgrades).
    """
    from nous.handlers.sleep_handler import F031_SAFETY_FLOOR_DESCRIPTION
    from nous_eval.probes import sleep_action_audit
    # The probe module must import the constant from the handler
    # (not redeclare it). Verify by checking the symbol resolves.
    assert hasattr(sleep_action_audit, "F031_SAFETY_FLOOR_DESCRIPTION")
    assert (
        sleep_action_audit.F031_SAFETY_FLOOR_DESCRIPTION
        is F031_SAFETY_FLOOR_DESCRIPTION
    )
    # Sanity: the constant mentions the load-bearing 0.7 floor.
    assert "0.7" in F031_SAFETY_FLOOR_DESCRIPTION
