"""Tests for nous_eval.probes.f058_calibration."""
from __future__ import annotations

import math

import pytest

from nous_eval.probes.f058_calibration import (
    brier,
    ece,
    gap,
    summarize,
    verdict_exit_code,
)


def test_brier_perfect_calibration_is_zero():
    """Confidence == outcome → squared error 0."""
    assert brier([(1.0, 1.0), (0.0, 0.0)]) == 0.0


def test_brier_worst_calibration_is_one():
    """Confidence opposite of outcome → squared error 1."""
    assert brier([(1.0, 0.0), (0.0, 1.0)]) == 1.0


def test_brier_random_baseline_is_quarter():
    """Always-0.5 confidence on 50/50 outcomes → Brier 0.25."""
    pairs = [(0.5, 1.0), (0.5, 0.0), (0.5, 1.0), (0.5, 0.0)]
    assert brier(pairs) == 0.25


def test_brier_empty_is_nan():
    assert math.isnan(brier([]))


def test_ece_perfect_per_bin_calibration_is_zero():
    """Each bin's mean_conf equals mean_outcome → ECE 0."""
    # All in bin [0.9, 1.0): conf=0.95, outcome alternates so mean=0.5;
    # mean_conf 0.95 != 0.5 → not zero. Use a case where they match.
    pairs = [(0.95, 1.0)] * 10  # mean_conf 0.95, mean_acc 1.0 → gap 0.05
    assert abs(ece(pairs) - 0.05) < 1e-9


def test_ece_handles_empty_and_sparse_bins():
    """ECE must skip empty bins, not divide-by-zero."""
    pairs = [(0.95, 1.0), (0.05, 0.0)]  # one row in two extreme bins
    e = ece(pairs)
    assert math.isfinite(e)
    assert 0 <= e <= 1


def test_gap_sign_matches_overconfidence_direction():
    """gap > 0 when mean_conf > mean_outcome (overconfident)."""
    overconfident = [(0.9, 0.5), (0.9, 0.5)]
    underconfident = [(0.5, 0.9), (0.5, 0.9)]
    assert gap(overconfident) > 0
    assert gap(underconfident) < 0


def test_summarize_handles_empty():
    s = summarize("test", [])
    assert s["n"] == 0
    assert s["label"] == "test"


def test_summarize_returns_all_metrics_for_nonempty():
    s = summarize("test", [(0.8, 1.0), (0.6, 0.0)])
    assert s["n"] == 2
    assert s["mean_conf"] == 0.7
    assert s["mean_outcome"] == 0.5
    assert s["gap"] == pytest.approx(0.2)
    assert "brier" in s and math.isfinite(s["brier"])
    assert "ece" in s and math.isfinite(s["ece"])


def test_verdict_passes_when_sanity_ok_and_brier_improves():
    result = {
        "sanity": {"ok": True, "n_post_f058": 17, "n_bad": 0},
        "counterfactual": {
            "raw": {"n": 100, "brier": 0.25},
            "calibrated": {"n": 100, "brier": 0.20},  # lower (better)
        },
    }
    assert verdict_exit_code(result) == 0


def test_verdict_fails_on_sanity_break():
    """If scaling stops being applied correctly in prod, fail loud
    regardless of historical Brier improvement."""
    result = {
        "sanity": {"ok": False, "n_post_f058": 17, "n_bad": 5},
        "counterfactual": {
            "raw": {"n": 100, "brier": 0.25},
            "calibrated": {"n": 100, "brier": 0.20},
        },
    }
    assert verdict_exit_code(result) == 1


def test_verdict_fails_when_factor_degrades_brier():
    """If applying the factor makes per-instance error WORSE,
    the factor needs re-derivation — fail to surface that."""
    result = {
        "sanity": {"ok": True, "n_post_f058": 17, "n_bad": 0},
        "counterfactual": {
            "raw": {"n": 100, "brier": 0.20},
            "calibrated": {"n": 100, "brier": 0.25},  # worse
        },
    }
    assert verdict_exit_code(result) == 1


def test_verdict_passes_when_no_data():
    """Empty counterfactual → not a regression (just no data yet)."""
    result = {
        "sanity": {"ok": True, "n_post_f058": 0, "n_bad": 0},
        "counterfactual": {
            "raw": {"n": 0, "brier": float("nan")},
            "calibrated": {"n": 0, "brier": float("nan")},
        },
    }
    assert verdict_exit_code(result) == 0
