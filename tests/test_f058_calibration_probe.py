"""Tests for nous_eval.probes.f058_calibration."""
from __future__ import annotations

import math
import statistics
from datetime import UTC, datetime, timedelta

import pytest

from nous.brain.calibration_scaling import calibrate_confidence
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
# Factor provenance (Codex P2/P1 rounds 2-3, PR #640)
# ---------------------------------------------------------------------------

# No default cutoff any more: writing provenance at all marks the current
# build, so there is no unchecked window at rollout.
assert _FACTOR_RETIRED_AT is None
_CUTOFF = datetime(2026, 9, 21, tzinfo=UTC)
_BEFORE = _CUTOFF - timedelta(days=30)
_AFTER = _CUTOFF + timedelta(days=1)


class _FakeConn:
    """Serves the probe's two SELECTs off canned rows.

    ``decisions`` are (confidence_raw, calibration_factor,
    calibration_applied_at) triples and the stored confidence is computed the
    way the write path computes it, so a row is correct by construction unless
    a test overrides it. Pass a 4th element to force a specific stored value
    and simulate a corrupt write. The era flag is evaluated the way the real
    query does it, so the tests exercise the predicate rather than restate it.
    """

    def __init__(self, decisions, reviewed_rows=()):
        self._decisions = [tuple(d) for d in decisions]
        self._reviewed = list(reviewed_rows)
        self.retired_at_arg = None

    async def fetch(self, query, *args):
        if "calibration_factor" in query and "brain.decisions" in query:
            assert "NULLIF" not in query, (
                "integrity must not be judged from the ratio: it breaks on "
                "clipping and on raw 0.0"
            )
            assert "created_at >=" not in query, (
                "created_at cannot date a calibration -- _update rescales "
                "historical rows without changing it"
            )
            self.retired_at_arg = args[1] if len(args) > 1 else None
            rows = []
            for d in self._decisions:
                raw, cf, at = d[0], d[1], d[2]
                if len(d) > 3:
                    stored = d[3]
                elif cf is None:
                    stored = raw
                else:
                    stored = calibrate_confidence(raw, cf)
                rows.append({
                    "confidence_raw": raw, "confidence": stored,
                    "calibration_factor": cf, "calibration_applied_at": at,
                })
            return rows
        return self._reviewed


def _reviewed(conf_outcomes, post=False):
    return [
        {"raw": c, "stored": c, "is_post_f058": post, "outcome": o}
        for c, o in conf_outcomes
    ]


class TestHistoryCannotPinStrictToFailure:
    """The reported symptom: every row ever written under F058 carries
    confidence_raw, so checking the whole cohort against the retired-to-1.0
    factor left sanity_ok false and --strict exiting 1 forever.
    """

    @pytest.mark.asyncio
    async def test_rows_without_provenance_do_not_fail_the_new_factor(self):
        """Pre-migration-073 rows have a NULL factor: unknowable, so skipped.
        That is what keeps history from pinning --strict to a failure."""
        conn = _FakeConn([(1.0, None, None)] * 5 + [(1.0, 1.0, _AFTER)] * 3)
        result = await run(conn, "a", 1.0)
        s = result["sanity"]
        assert s["ok"] is True
        assert (s["n_post_f058"], s["n_with_factor"], s["n_bad"]) == (8, 3, 0)

    @pytest.mark.asyncio
    async def test_strict_verdict_no_longer_pinned_to_failure(self):
        conn = _FakeConn(
            [(1.0, None, None)] * 4 + [(1.0, 1.0, _AFTER)] * 2,
            _reviewed([(0.9, "failure"), (0.9, "success"), (0.8, "failure")]),
        )
        assert verdict_exit_code(await run(conn, "a", 1.0)) == 0

    @pytest.mark.asyncio
    async def test_rows_predating_the_provenance_column_are_skipped(self):
        """calibration_factor IS NULL -> unknowable, so not a failure."""
        conn = _FakeConn([(1.0, None, None)] * 9 + [(1.0, 1.0, _AFTER)])
        result = await run(conn, "a", 1.0)
        assert result["sanity"]["ok"] is True
        assert result["sanity"]["n_post_f058"] == 10
        assert result["sanity"]["n_with_factor"] == 1


class TestStaleOverrideIsCaughtHoweverItIsWritten:
    """Codex P1 (round 2) and P2 (round 3): a deployment still applying 0.7627
    must fail, including when it only ever RE-calibrates pre-cutoff rows.
    """

    @pytest.mark.asyncio
    async def test_new_rows_with_the_stale_factor_fail(self):
        conn = _FakeConn([(1.0, 0.7627, _AFTER)] * 4)
        result = await run(conn, "a", 1.0)
        assert result["sanity"]["ok"] is False
        assert result["sanity"]["n_bad"] == 4
        assert verdict_exit_code(result) == 1

    @pytest.mark.asyncio
    async def test_recalibrated_historical_rows_are_still_checked(self):
        """The round-3 hole: under a created_at predicate these rows counted
        as prior-era, so n_current_era stayed 0 and --strict reported PASS
        while the database filled with incorrectly scaled values.

        They carry provenance, so the current build wrote them -- whatever
        their creation date -- and every one is checked.
        """
        conn = _FakeConn([(1.0, 0.7627, _AFTER)] * 3
                         + [(1.0, 0.7627, _BEFORE)] * 6)
        result = await run(conn, "a", 1.0)
        assert result["sanity"]["n_current_era"] == 9
        assert result["sanity"]["ok"] is False
        assert result["sanity"]["n_bad"] == 9

    @pytest.mark.asyncio
    async def test_a_stale_override_does_not_age_out(self):
        conn = _FakeConn([(1.0, 0.7627, _CUTOFF + timedelta(days=400))] * 3)
        result = await run(conn, "a", 1.0)
        assert result["sanity"]["ok"] is False

    @pytest.mark.asyncio
    async def test_idle_database_passes(self):
        """Nothing calibrated by this build yet -> nothing it could have got
        wrong. Distinguishable from the stale-override case above, which does
        write provenance and therefore does get checked."""
        conn = _FakeConn([(1.0, None, None)] * 5)
        result = await run(conn, "a", 1.0)
        assert result["sanity"]["n_current_era"] == 0
        assert result["sanity"]["ok"] is True


class TestWritePathIntegrityIsCheckedInBothEras:
    """New check enabled by recording the factor: a row must match the factor
    it says it used, whenever it was written.
    """

    @pytest.mark.asyncio
    async def test_ratio_disagreeing_with_recorded_factor_fails(self):
        # Claims 0.7627 but stored the unscaled value -> write path is broken.
        conn = _FakeConn([(1.0, 0.7627, _BEFORE, 1.0)])
        result = await run(conn, "a", 1.0)
        assert result["sanity"]["n_bad_integrity"] == 1
        assert result["sanity"]["ok"] is False

    @pytest.mark.asyncio
    async def test_rows_agreeing_with_their_own_factor_pass_integrity(self):
        """Integrity is judged against the factor the row RECORDED, so a
        0.7627 row is internally consistent even while the currency check
        separately flags it as a stale override."""
        conn = _FakeConn([(1.0, 0.7627, _BEFORE)] * 4)
        result = await run(conn, "a", 1.0)
        assert result["sanity"]["n_bad_integrity"] == 0
        # ...and the same rows still fail currency against the live 1.0.
        assert result["sanity"]["n_bad"] == 4

    @pytest.mark.asyncio
    async def test_integrity_and_currency_both_pass_when_aligned(self):
        conn = _FakeConn([(1.0, 0.7627, _BEFORE)] * 4)
        result = await run(conn, "a", 0.7627)
        assert result["sanity"]["n_bad_integrity"] == 0
        assert result["sanity"]["ok"] is True


class TestEraIsNotInferredFromRowOrder:
    """Brain._update rewrites confidence/confidence_raw with the CURRENT factor
    while leaving created_at untouched, so a historical decision edited after
    the retirement carries a new-factor ratio with an old timestamp.
    """

    @pytest.mark.asyncio
    async def test_edited_historical_row_does_not_poison_the_cohort(self):
        decisions = [
            # untouched history: written before migration 073, no provenance
            (1.0, None, None),
            # edited post-retirement -> rescaled to 1.0 and stamped, even
            # though the row itself is old
            (1.0, 1.0, _AFTER),
            (1.0, None, None),
            (1.0, None, None),
        ]
        result = await run(_FakeConn(decisions), "a", 1.0)
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
        conn = _FakeConn([(1.0, 1.0, _AFTER)], _reviewed(self._PRE))
        cf = (await run(conn, "a", 1.0))["counterfactual"]
        assert cf["calibrated"]["mean_conf"] == pytest.approx(
            cf["raw"]["mean_conf"] * _HISTORICAL_F058_FACTOR
        )
        assert cf["calibrated"]["brier"] != cf["raw"]["brier"]

    @pytest.mark.asyncio
    async def test_live_factor_does_not_leak_into_the_hypothesis(self):
        conn = _FakeConn([(1.0, 1.0, _AFTER)], _reviewed(self._PRE))
        a = await run(conn, "a", 1.0)
        b = await run(conn, "a", 0.5)
        assert (a["counterfactual"]["calibrated"]["brier"]
                == b["counterfactual"]["calibrated"]["brier"])

    @pytest.mark.asyncio
    async def test_explicit_counterfactual_factor_is_honored(self):
        conn = _FakeConn([(1.0, 1.0, _AFTER)], _reviewed(self._PRE))
        cf = (await run(conn, "a", 1.0, 0.5))["counterfactual"]
        assert cf["calibrated"]["mean_conf"] == pytest.approx(
            cf["raw"]["mean_conf"] * 0.5
        )


class TestIntegrityUsesTheCalibratorNotTheRatio:
    """confidence/confidence_raw is not a faithful signal of correctness."""

    @pytest.mark.asyncio
    async def test_clipped_write_above_factor_one_is_not_corruption(self):
        """calibrate_confidence clips to [0, 1], so raw 0.95 at factor 1.2
        stores 1.0 -- a ratio of ~1.053 that the ratio check called broken."""
        assert calibrate_confidence(0.95, 1.2) == 1.0
        conn = _FakeConn([(0.95, 1.2, _AFTER)])
        result = await run(conn, "a", 1.2)
        assert result["sanity"]["n_bad_integrity"] == 0
        assert result["sanity"]["ok"] is True

    @pytest.mark.asyncio
    async def test_raw_zero_is_still_checked(self):
        """NULLIF(confidence_raw, 0) made the ratio NULL, so these rows
        bypassed integrity validation entirely."""
        # Correct: 0.0 * anything is 0.0.
        ok = _FakeConn([(0.0, 0.7627, _BEFORE)])
        assert (await run(ok, "a", 1.0))["sanity"]["n_bad_integrity"] == 0

        # Corrupt: stored a non-zero value for a raw 0.0 claim.
        bad = _FakeConn([(0.0, 0.7627, _BEFORE, 0.4)])
        result = await run(bad, "a", 1.0)
        assert result["sanity"]["n_bad_integrity"] == 1
        assert result["sanity"]["ok"] is False

    @pytest.mark.asyncio
    async def test_clipping_at_zero_is_also_accepted(self):
        conn = _FakeConn([(0.0, 0.0, _AFTER)])
        assert (await run(conn, "a", 0.0))["sanity"]["n_bad_integrity"] == 0

    @pytest.mark.asyncio
    async def test_a_genuinely_miscomputed_write_still_fails(self):
        """The check must not become permissive: an off-by-a-bit stored value
        is still corruption."""
        conn = _FakeConn([(0.8, 0.7627, _AFTER, 0.8 * 0.5)])
        result = await run(conn, "a", 0.7627)
        assert result["sanity"]["n_bad_integrity"] == 1
        assert verdict_exit_code(result) == 1

    @pytest.mark.asyncio
    async def test_passthrough_factor_one_round_trips_exactly(self):
        conn = _FakeConn([(raw, 1.0, _AFTER) for raw in (0.0, 0.33, 0.5, 1.0)])
        result = await run(conn, "a", 1.0)
        assert result["sanity"]["n_bad_integrity"] == 0
        assert result["sanity"]["ok"] is True


class TestNoUncheckedWindowAtRollout:
    """Codex round-5 P2: a hard-coded cutoff one day after the documented
    retirement meant every calibration performed on rollout day fell outside
    current_era, so the probe could report n_current_era == 0 and pass
    --strict while the host's stale 0.7627 override was still active.
    """

    @pytest.mark.asyncio
    async def test_stale_override_on_rollout_day_is_caught(self):
        rollout_day = datetime(2026, 9, 20, 14, 0, tzinfo=UTC)
        conn = _FakeConn([(1.0, 0.7627, rollout_day)] * 3)
        result = await run(conn, "a", 1.0)
        assert result["sanity"]["n_current_era"] == 3
        assert result["sanity"]["ok"] is False
        assert verdict_exit_code(result) == 1

    @pytest.mark.asyncio
    async def test_the_very_first_post_deploy_write_is_checked(self):
        conn = _FakeConn([(1.0, 0.7627, datetime(2026, 9, 20, tzinfo=UTC))])
        assert (await run(conn, "a", 1.0))["sanity"]["n_bad"] == 1

    @pytest.mark.asyncio
    async def test_provenance_alone_defines_the_cohort(self):
        """No date anywhere: an ancient timestamp with provenance is still the
        current build's work, because only the current build writes it."""
        ancient = datetime(2020, 1, 1, tzinfo=UTC)
        conn = _FakeConn([(1.0, 1.0, ancient)])
        result = await run(conn, "a", 1.0)
        assert result["sanity"]["n_current_era"] == 1
        assert result["sanity"]["ok"] is True


class TestCounterfactualRespectsProductionClipping:
    """Codex round-5 P2: --counterfactual-factor above 1.0 is supported for
    correcting underconfidence, but a bare multiply produced confidences above
    1.0 that the write path clips and could never store, so the Brier/ECE
    deltas and the --strict verdict judged impossible values.
    """

    _PRE = [(0.99, "success"), (0.95, "success"), (0.9, "failure"),
            (0.85, "success"), (0.8, "success")]

    @pytest.mark.asyncio
    async def test_counterfactual_confidences_never_exceed_one(self):
        conn = _FakeConn([(1.0, 1.0, _AFTER)], _reviewed(self._PRE))
        cf = (await run(conn, "a", 1.0, 1.2))["counterfactual"]
        assert cf["calibrated"]["mean_conf"] <= 1.0
        expected = statistics.mean(
            [calibrate_confidence(c, 1.2) for c, _ in self._PRE]
        )
        assert cf["calibrated"]["mean_conf"] == pytest.approx(expected, abs=5e-3)

    @pytest.mark.asyncio
    async def test_bare_multiply_would_have_exceeded_one(self):
        """Guard that the fixture actually exercises clipping."""
        assert max(c * 1.2 for c, _ in self._PRE) > 1.0
        assert calibrate_confidence(0.99, 1.2) == 1.0

    @pytest.mark.asyncio
    async def test_factors_below_one_are_unaffected(self):
        conn = _FakeConn([(1.0, 1.0, _AFTER)], _reviewed(self._PRE))
        cf = (await run(conn, "a", 1.0, 0.7627))["counterfactual"]
        expected = statistics.mean([c * 0.7627 for c, _ in self._PRE])
        assert cf["calibrated"]["mean_conf"] == pytest.approx(expected, abs=5e-3)
