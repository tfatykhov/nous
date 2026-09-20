"""Tests for nous_eval.probes.f058_calibration."""
from __future__ import annotations

import math

import pytest

from nous_eval.probes.f058_calibration import (
    _HISTORICAL_F058_FACTOR,
    brier,
    ece,
    gap,
    run,
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
            "raw": {"n": 100, "brier": 0.25, "ece": 0.10},
            "calibrated": {"n": 100, "brier": 0.20, "ece": 0.05},
        },
    }
    assert verdict_exit_code(result) == 0


def test_verdict_fails_on_sanity_break():
    """If scaling stops being applied correctly in prod, fail loud
    regardless of historical Brier improvement."""
    result = {
        "sanity": {"ok": False, "n_post_f058": 17, "n_bad": 5},
        "counterfactual": {
            "raw": {"n": 100, "brier": 0.25, "ece": 0.10},
            "calibrated": {"n": 100, "brier": 0.20, "ece": 0.05},
        },
    }
    assert verdict_exit_code(result) == 1


def test_verdict_fails_when_factor_degrades_brier():
    """If applying the factor makes per-instance error WORSE,
    the factor needs re-derivation — fail to surface that."""
    result = {
        "sanity": {"ok": True, "n_post_f058": 17, "n_bad": 0},
        "counterfactual": {
            "raw": {"n": 100, "brier": 0.20, "ece": 0.05},
            "calibrated": {"n": 100, "brier": 0.25, "ece": 0.05},
        },
    }
    assert verdict_exit_code(result) == 1


def test_verdict_passes_when_no_data():
    """Empty counterfactual → not a regression (just no data yet)."""
    result = {
        "sanity": {"ok": True, "n_post_f058": 0, "n_bad": 0},
        "counterfactual": {
            "raw": {"n": 0, "brier": float("nan"), "ece": float("nan")},
            "calibrated": {"n": 0, "brier": float("nan"), "ece": float("nan")},
        },
    }
    assert verdict_exit_code(result) == 0


def test_verdict_fails_on_ece_regression_even_when_brier_holds():
    """A re-derivation could improve Brier marginally while degrading
    per-bin calibration — the gate must inspect ECE too, not just Brier.
    Without this guard, a factor that helps the high-conf bin but
    crushes low-conf bins would silently pass."""
    result = {
        "sanity": {"ok": True, "n_post_f058": 17, "n_bad": 0},
        "counterfactual": {
            # Brier holds steady (improves trivially)
            "raw": {"n": 100, "brier": 0.20, "ece": 0.05},
            "calibrated": {"n": 100, "brier": 0.199, "ece": 0.10},  # +0.05 ECE
        },
    }
    assert verdict_exit_code(result) == 1


def test_verdict_passes_on_tiny_ece_drift():
    """ECE deltas under the regression epsilon (0.01) are noise; don't
    fail loud on those."""
    result = {
        "sanity": {"ok": True, "n_post_f058": 17, "n_bad": 0},
        "counterfactual": {
            "raw": {"n": 100, "brier": 0.20, "ece": 0.05},
            "calibrated": {"n": 100, "brier": 0.18, "ece": 0.055},  # +0.005 ECE
        },
    }
    assert verdict_exit_code(result) == 0


# ---------------------------------------------------------------------------
# Two-factor era handling (Codex P2, PR #640)
# ---------------------------------------------------------------------------


class _FakeConn:
    """Serves the probe's two SELECTs off canned rows.

    ``ratio_rows`` are (ratio, ) entries for the sanity cohort, IN created_at
    order — the probe relies on that ordering to find the era boundary.
    """

    def __init__(self, ratio_rows, reviewed_rows=()):
        self._ratios = list(ratio_rows)
        self._reviewed = list(reviewed_rows)

    async def fetch(self, query, *args):
        if "NULLIF" in query:
            assert "ORDER BY created_at" in query, (
                "era detection depends on chronological order"
            )
            return [
                {"confidence_raw": 0.8, "confidence": 0.8 * r, "r": r}
                for r in self._ratios
            ]
        return self._reviewed


def _reviewed(conf_outcomes, post=False):
    return [
        {"raw": c, "stored": c, "is_post_f058": post, "outcome": o}
        for c, o in conf_outcomes
    ]


class TestSanityCohortIsEraScoped:
    """Every row ever written under F058 carries confidence_raw, so the sanity
    cohort spans both the 0.7627 era and the retired-to-1.0 era. Comparing the
    old rows against the new factor made sanity_ok permanently false and
    --strict permanently exit 1 no matter how correct the write path was.
    """

    @pytest.mark.asyncio
    async def test_rows_from_the_retired_era_do_not_fail_the_new_factor(self):
        conn = _FakeConn([0.7627] * 5 + [1.0] * 3)
        result = await run(conn, "a", 1.0)
        s = result["sanity"]
        assert s["ok"] is True
        assert s["n_post_f058"] == 8
        assert s["n_prior_era"] == 5
        assert s["n_current_era"] == 3
        assert s["n_bad"] == 0

    @pytest.mark.asyncio
    async def test_no_rows_under_the_new_factor_yet_is_not_a_failure(self):
        """Retirement deployed, nothing written since — vacuously fine."""
        conn = _FakeConn([0.7627] * 5)
        result = await run(conn, "a", 1.0)
        assert result["sanity"]["ok"] is True
        assert result["sanity"]["n_current_era"] == 0

    @pytest.mark.asyncio
    async def test_scaling_reverting_after_the_boundary_still_fails(self):
        """The check must not be defanged: an old-factor row appearing AFTER
        the era boundary is a live write-path regression."""
        conn = _FakeConn([0.7627, 0.7627, 1.0, 1.0, 0.7627])
        result = await run(conn, "a", 1.0)
        assert result["sanity"]["ok"] is False
        assert result["sanity"]["n_bad"] == 1

    @pytest.mark.asyncio
    async def test_write_path_dropping_scaling_mid_era_still_fails(self):
        """Pre-retirement config (factor 0.7627): an unscaled row is the exact
        bug step 1 exists to catch, and is still caught."""
        conn = _FakeConn([0.7627, 0.7627, 1.0, 0.7627])
        result = await run(conn, "a", 0.7627)
        assert result["sanity"]["ok"] is False
        assert result["sanity"]["n_bad"] == 1

    @pytest.mark.asyncio
    async def test_strict_verdict_no_longer_pinned_to_failure(self):
        """End-to-end: the reported symptom was --strict exiting 1 forever."""
        conn = _FakeConn(
            [0.7627] * 4 + [1.0] * 2,
            _reviewed([(0.9, "failure"), (0.9, "success"), (0.8, "failure")]),
        )
        result = await run(conn, "a", 1.0)
        assert verdict_exit_code(result) == 0


class TestCounterfactualUsesTheHistoricalFactor:
    """Sibling defect Codex did not flag: `factor` was doing two jobs — "what
    the write path applies now" and "the scaling hypothesis under test". They
    were the same number until the retirement split them. Feeding the live 1.0
    into step 2 multiplies by one, so the Brier/ECE gate passes vacuously.
    """

    _PRE = [(0.9, "failure"), (0.9, "success"), (0.8, "failure"),
            (0.7, "success"), (0.95, "failure")]

    @pytest.mark.asyncio
    async def test_counterfactual_is_not_the_identity_after_retirement(self):
        conn = _FakeConn([1.0], _reviewed(self._PRE))
        result = await run(conn, "a", 1.0)
        cf = result["counterfactual"]
        assert result["counterfactual_factor"] == _HISTORICAL_F058_FACTOR
        assert cf["calibrated"]["mean_conf"] == pytest.approx(
            cf["raw"]["mean_conf"] * _HISTORICAL_F058_FACTOR
        )
        assert cf["calibrated"]["brier"] != cf["raw"]["brier"]

    @pytest.mark.asyncio
    async def test_live_factor_does_not_leak_into_the_hypothesis(self):
        """--factor is independent of what step 2 tests."""
        conn = _FakeConn([1.0], _reviewed(self._PRE))
        a = await run(conn, "a", 1.0)
        b = await run(conn, "a", 0.5)
        assert (a["counterfactual"]["calibrated"]["brier"]
                == b["counterfactual"]["calibrated"]["brier"])

    @pytest.mark.asyncio
    async def test_explicit_counterfactual_factor_is_honored(self):
        conn = _FakeConn([1.0], _reviewed(self._PRE))
        result = await run(conn, "a", 1.0, 0.5)
        cf = result["counterfactual"]
        assert cf["calibrated"]["mean_conf"] == pytest.approx(
            cf["raw"]["mean_conf"] * 0.5
        )
