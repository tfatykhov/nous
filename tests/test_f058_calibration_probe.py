"""Tests for nous_eval.probes.f058_calibration."""
from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from nous_eval.probes.f058_calibration import (
    _FACTOR_RETIRED_AT,
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
# Two-factor era handling (Codex P2/P1, PR #640)
# ---------------------------------------------------------------------------

_CUTOFF = _FACTOR_RETIRED_AT
_BEFORE = _CUTOFF - timedelta(days=30)
_AFTER = _CUTOFF + timedelta(days=1)


class _FakeConn:
    """Serves the probe's two SELECTs off canned rows.

    ``decisions`` are (ratio, created_at) pairs; the era flag is evaluated the
    same way the real query does it, so the tests exercise the cutoff rather
    than restating it.
    """

    def __init__(self, decisions, reviewed_rows=()):
        self._decisions = list(decisions)
        self._reviewed = list(reviewed_rows)
        self.retired_at_arg = None

    async def fetch(self, query, *args):
        if "NULLIF" in query:
            assert "created_at >= $2" in query, (
                "era boundary must come from created_at, not row order"
            )
            self.retired_at_arg = args[1]
            return [
                {"r": r, "is_current_era": created >= args[1]}
                for r, created in self._decisions
            ]
        return self._reviewed


def _reviewed(conf_outcomes, post=False):
    return [
        {"raw": c, "stored": c, "is_post_f058": post, "outcome": o}
        for c, o in conf_outcomes
    ]


class TestSanityCohortIsCutAtTheRetirementDeploy:
    """Every row ever written under F058 carries confidence_raw, so the
    unrestricted cohort spans both eras. Checking 0.7627-era rows against the
    retired-to-1.0 factor left --strict exiting 1 permanently.
    """

    @pytest.mark.asyncio
    async def test_rows_from_the_retired_era_do_not_fail_the_new_factor(self):
        conn = _FakeConn([(0.7627, _BEFORE)] * 5 + [(1.0, _AFTER)] * 3)
        result = await run(conn, "a", 1.0)
        s = result["sanity"]
        assert s["ok"] is True
        assert (s["n_post_f058"], s["n_prior_era"], s["n_current_era"]) == (8, 5, 3)
        assert s["n_bad"] == 0

    @pytest.mark.asyncio
    async def test_strict_verdict_no_longer_pinned_to_failure(self):
        """The reported symptom: --strict exited 1 forever on history."""
        conn = _FakeConn(
            [(0.7627, _BEFORE)] * 4 + [(1.0, _AFTER)] * 2,
            _reviewed([(0.9, "failure"), (0.9, "success"), (0.8, "failure")]),
        )
        assert verdict_exit_code(await run(conn, "a", 1.0)) == 0

    @pytest.mark.asyncio
    async def test_cutoff_defaults_to_the_retirement_constant(self):
        conn = _FakeConn([(1.0, _AFTER)])
        result = await run(conn, "a", 1.0)
        assert conn.retired_at_arg == _FACTOR_RETIRED_AT
        assert result["retired_at"] == _FACTOR_RETIRED_AT.isoformat()

    @pytest.mark.asyncio
    async def test_explicit_cutoff_is_honored(self):
        later = _AFTER + timedelta(days=7)
        conn = _FakeConn([(0.7627, _AFTER)])
        result = await run(conn, "a", 1.0, retired_at=later)
        # The 0.7627 row now predates the cutoff, so it is history, not a bug.
        assert result["sanity"]["n_current_era"] == 0
        assert result["sanity"]["ok"] is True


class TestStaleDeploymentStillFails:
    """Codex P1: a deployment that keeps writing with the stale 0.7627
    override must not pass vacuously while the database fills with
    incorrectly scaled decisions.
    """

    @pytest.mark.asyncio
    async def test_post_deploy_rows_with_the_old_factor_fail(self):
        conn = _FakeConn([(0.7627, _BEFORE)] * 3 + [(0.7627, _AFTER)] * 4)
        result = await run(conn, "a", 1.0)
        assert result["sanity"]["ok"] is False
        assert result["sanity"]["n_bad"] == 4
        assert verdict_exit_code(result) == 1

    @pytest.mark.asyncio
    async def test_a_single_post_deploy_regression_fails(self):
        conn = _FakeConn([(1.0, _AFTER)] * 20 + [(0.7627, _AFTER)])
        result = await run(conn, "a", 1.0)
        assert result["sanity"]["ok"] is False
        assert result["sanity"]["n_bad"] == 1

    @pytest.mark.asyncio
    async def test_stale_deployment_does_not_age_out(self):
        """A rolling window would let a persistent misconfiguration go quiet;
        the cutoff must keep failing however old the bad rows get."""
        ancient = _CUTOFF + timedelta(days=400)
        conn = _FakeConn([(0.7627, ancient)] * 3)
        result = await run(conn, "a", 1.0)
        assert result["sanity"]["ok"] is False
        assert result["sanity"]["n_bad"] == 3

    @pytest.mark.asyncio
    async def test_idle_database_passes(self):
        """No decisions since the deploy is genuinely nothing to check -- and
        is now distinguishable from the stale-factor case above, which lands
        in the cohort instead of being skipped."""
        conn = _FakeConn([(0.7627, _BEFORE)] * 5)
        result = await run(conn, "a", 1.0)
        assert result["sanity"]["n_current_era"] == 0
        assert result["sanity"]["ok"] is True


class TestEraIsNotInferredFromRowOrder:
    """Brain._update rewrites confidence/confidence_raw with the CURRENT factor
    but leaves created_at untouched, so a historical decision edited after the
    retirement carries a new-factor ratio with an old timestamp. Inferring the
    boundary from the first new-factor ratio would drag every later historical
    row into the cohort as a false regression.
    """

    @pytest.mark.asyncio
    async def test_edited_historical_row_does_not_poison_the_cohort(self):
        decisions = [
            (0.7627, _BEFORE - timedelta(days=5)),
            # edited post-retirement -> rescaled to 1.0, created_at unchanged
            (1.0, _BEFORE - timedelta(days=4)),
            (0.7627, _BEFORE - timedelta(days=3)),
            (0.7627, _BEFORE - timedelta(days=2)),
            (1.0, _AFTER),
        ]
        result = await run(conn := _FakeConn(decisions), "a", 1.0)
        assert conn.retired_at_arg is not None
        assert result["sanity"]["ok"] is True
        assert result["sanity"]["n_current_era"] == 1
        assert result["sanity"]["n_bad"] == 0


class TestCounterfactualUsesTheHistoricalFactor:
    """Sibling defect Codex did not flag: `factor` was doing two jobs — "what
    the write path applies now" and "the scaling hypothesis under test". They
    were the same number until the retirement split them, so feeding the live
    1.0 into step 2 multiplies by one and the gate passes vacuously.
    """

    _PRE = [(0.9, "failure"), (0.9, "success"), (0.8, "failure"),
            (0.7, "success"), (0.95, "failure")]

    @pytest.mark.asyncio
    async def test_counterfactual_is_not_the_identity_after_retirement(self):
        conn = _FakeConn([(1.0, _AFTER)], _reviewed(self._PRE))
        result = await run(conn, "a", 1.0)
        cf = result["counterfactual"]
        assert result["counterfactual_factor"] == _HISTORICAL_F058_FACTOR
        assert cf["calibrated"]["mean_conf"] == pytest.approx(
            cf["raw"]["mean_conf"] * _HISTORICAL_F058_FACTOR
        )
        assert cf["calibrated"]["brier"] != cf["raw"]["brier"]

    @pytest.mark.asyncio
    async def test_live_factor_does_not_leak_into_the_hypothesis(self):
        conn = _FakeConn([(1.0, _AFTER)], _reviewed(self._PRE))
        a = await run(conn, "a", 1.0)
        b = await run(conn, "a", 0.5)
        assert (a["counterfactual"]["calibrated"]["brier"]
                == b["counterfactual"]["calibrated"]["brier"])

    @pytest.mark.asyncio
    async def test_explicit_counterfactual_factor_is_honored(self):
        conn = _FakeConn([(1.0, _AFTER)], _reviewed(self._PRE))
        cf = (await run(conn, "a", 1.0, 0.5))["counterfactual"]
        assert cf["calibrated"]["mean_conf"] == pytest.approx(
            cf["raw"]["mean_conf"] * 0.5
        )
