"""Tests for F058 calibration probe."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest

from nous_eval.probes.f058_calibration import (
    _DEFAULT_FACTOR,
    _ERA_CUTOFF_DATE,
    _FACTOR_TOLERANCE,
    _HISTORICAL_F058_FACTOR,
    run,
)


@pytest.fixture
def mock_conn():
    """Mock asyncpg connection."""
    conn = AsyncMock()
    return conn


def _make_decision(
    confidence: float,
    confidence_raw: float | None,
    calibration_factor: float | None,
    outcome: str,
    created_at: datetime,
    is_post_f058: bool = True,
    calibration_applied_at: datetime | None = None,
) -> dict:
    """Helper to create a decision row."""
    return {
        "raw": confidence_raw if confidence_raw is not None else confidence,
        "stored": confidence,
        "is_post_f058": is_post_f058,
        "applied_factor": calibration_factor,
        "outcome": outcome,
        "created_at": created_at,
        "confidence": confidence,
        "confidence_raw": confidence_raw,
        "calibration_factor": calibration_factor,
        "calibration_applied_at": calibration_applied_at,
    }


@pytest.mark.asyncio
async def test_era_split_factor_based_classification(mock_conn):
    """P1-1: Era split should use calibration_factor, not created_at.

    When an old decision's confidence is edited after retirement,
    Brain._update recalibrates it with the current factor (1.0) and
    stamps calibration_factor while preserving its original created_at.
    A date-based cutoff would misclassify these decisions.
    """
    old_date = _ERA_CUTOFF_DATE - timedelta(days=30)
    new_date = _ERA_CUTOFF_DATE + timedelta(days=30)

    rows = [
        # Old decision with old factor — should be in pre-era
        _make_decision(0.76, 1.0, _HISTORICAL_F058_FACTOR, "success",
                      old_date, calibration_applied_at=old_date),
        # Old decision EDITED with new factor — should be in post-era
        # (This is the P1-1 case: old created_at, new factor)
        _make_decision(0.95, 0.95, _DEFAULT_FACTOR, "success",
                      old_date, calibration_applied_at=new_date),
        # New decision with new factor — should be in post-era
        _make_decision(0.80, 0.80, _DEFAULT_FACTOR, "failure",
                      new_date, calibration_applied_at=new_date),
    ]

    mock_conn.fetch.side_effect = [rows, rows]

    result = await run(mock_conn, "test-agent", _DEFAULT_FACTOR)

    es = result["era_split"]
    assert es["classification"] == "factor-based"

    # Pre-era should have only the 0.7627-factor decision
    assert es["pre_era"]["n"] == 1
    assert abs(es["pre_era"]["factor"] - _HISTORICAL_F058_FACTOR) < 1e-6

    # Post-era should have the two 1.0-factor decisions,
    # INCLUDING the one with old created_at
    assert es["post_era"]["n"] == 2
    assert abs(es["post_era"]["factor"] - _DEFAULT_FACTOR) < 1e-6

    # Overall should include all 3 (all are post-F058)
    assert es["overall"]["n"] == 3


@pytest.mark.asyncio
async def test_era_split_excludes_pre_f058_decisions(mock_conn):
    """P1-2: Pre-F058 decisions (is_post_f058=False) should be excluded.

    These rows have confidence_raw=NULL (migration 039 left these) and
    predate the calibration system. Including them in the 0.7627 era
    corrupts that era's metrics with pass-through historical values.
    """
    date = _ERA_CUTOFF_DATE - timedelta(days=10)

    rows = [
        # Pre-F058 decision: confidence_raw=NULL, is_post_f058=False
        _make_decision(0.85, None, None, "success", date, is_post_f058=False),
        # Post-F058 decision with 0.7627 factor
        _make_decision(0.76, 1.0, _HISTORICAL_F058_FACTOR, "failure",
                      date, calibration_applied_at=date),
        # Post-F058 decision with 1.0 factor
        _make_decision(0.90, 0.90, _DEFAULT_FACTOR, "success",
                      date, calibration_applied_at=date),
    ]

    mock_conn.fetch.side_effect = [rows, rows]

    result = await run(mock_conn, "test-agent", _DEFAULT_FACTOR)

    es = result["era_split"]

    # Pre-era should have only the 0.7627-factor post-F058 decision
    assert es["pre_era"]["n"] == 1

    # Post-era should have only the 1.0-factor post-F058 decision
    assert es["post_era"]["n"] == 1

    # Overall should have only the 2 post-F058 decisions
    # (the pre-F058 one is excluded)
    assert es["overall"]["n"] == 2


@pytest.mark.asyncio
async def test_era_split_null_factor_treated_as_legacy(mock_conn):
    """NULL calibration_factor should be treated as legacy 0.7627 era.

    Rows predating migration 073 have NULL calibration_factor but were
    written under F058, so they belong to the historical factor's cohort.
    """
    date = _ERA_CUTOFF_DATE - timedelta(days=5)

    rows = [
        # Post-F058 decision with NULL factor (pre-migration-073)
        _make_decision(0.76, 1.0, None, "success", date, is_post_f058=True),
        # Post-F058 decision with explicit 1.0 factor
        _make_decision(0.85, 0.85, _DEFAULT_FACTOR, "failure",
                      date, calibration_applied_at=date),
    ]

    mock_conn.fetch.side_effect = [rows, rows]

    result = await run(mock_conn, "test-agent", _DEFAULT_FACTOR)

    es = result["era_split"]

    # Pre-era should include the NULL-factor decision
    assert es["pre_era"]["n"] == 1

    # Post-era should have only the 1.0-factor decision
    assert es["post_era"]["n"] == 1


@pytest.mark.asyncio
async def test_era_split_unexpected_factor_excluded(mock_conn, capsys):
    """Decisions with unexpected calibration_factor should be excluded.

    If a decision has a factor that doesn't match either era's expected
    value (within tolerance), it should be excluded and logged.
    """
    date = _ERA_CUTOFF_DATE + timedelta(days=1)
    unexpected_factor = 0.85  # Not 0.7627 or 1.0

    rows = [
        # Decision with unexpected factor
        _make_decision(0.85, 1.0, unexpected_factor, "success",
                      date, is_post_f058=True, calibration_applied_at=date),
        # Normal decision with 1.0 factor
        _make_decision(0.90, 0.90, _DEFAULT_FACTOR, "failure",
                      date, calibration_applied_at=date),
    ]

    mock_conn.fetch.side_effect = [rows, rows]

    result = await run(mock_conn, "test-agent", _DEFAULT_FACTOR)

    es = result["era_split"]

    # The unexpected-factor decision should be excluded
    assert es["n_excluded"] == 1

    # Only the normal decision should be in post-era
    assert es["pre_era"]["n"] == 0
    assert es["post_era"]["n"] == 1
    assert es["overall"]["n"] == 1

    # Should have warned about the exclusion
    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    assert "unexpected calibration_factor" in captured.err


@pytest.mark.asyncio
async def test_era_split_tolerance_matching(mock_conn):
    """Factor matching should use tolerance, not exact equality.

    Floating-point comparisons need tolerance. A factor within
    _FACTOR_TOLERANCE of the expected value should match.
    """
    date = _ERA_CUTOFF_DATE + timedelta(days=1)

    # Factors within tolerance
    almost_one = _DEFAULT_FACTOR - (_FACTOR_TOLERANCE / 2)
    almost_legacy = _HISTORICAL_F058_FACTOR + (_FACTOR_TOLERANCE / 2)

    rows = [
        _make_decision(0.76, 1.0, almost_legacy, "success",
                      date, calibration_applied_at=date),
        _make_decision(0.90, 0.90, almost_one, "failure",
                      date, calibration_applied_at=date),
    ]

    mock_conn.fetch.side_effect = [rows, rows]

    result = await run(mock_conn, "test-agent", _DEFAULT_FACTOR)

    es = result["era_split"]

    # Both should be classified despite not being exact
    assert es["pre_era"]["n"] == 1
    assert es["post_era"]["n"] == 1
    assert es["n_excluded"] == 0


@pytest.mark.asyncio
async def test_era_split_all_pre_f058_excluded(mock_conn):
    """When all decisions are pre-F058, era counts should be zero."""
    date = _ERA_CUTOFF_DATE - timedelta(days=30)

    rows = [
        _make_decision(0.80, None, None, "success", date, is_post_f058=False),
        _make_decision(0.75, None, None, "failure", date, is_post_f058=False),
    ]

    mock_conn.fetch.side_effect = [rows, rows]

    result = await run(mock_conn, "test-agent", _DEFAULT_FACTOR)

    es = result["era_split"]

    # All decisions should be excluded from era analysis
    assert es["pre_era"]["n"] == 0
    assert es["post_era"]["n"] == 0
    assert es["overall"]["n"] == 0


@pytest.mark.asyncio
async def test_counterfactual_uses_only_pre_f058(mock_conn):
    """Step 2 counterfactual should only use pre-F058 decisions.

    This is not a new P1 fix, but ensures the counterfactual logic
    stays correct alongside the era-split fix.
    """
    old_date = _ERA_CUTOFF_DATE - timedelta(days=30)
    new_date = _ERA_CUTOFF_DATE + timedelta(days=30)

    rows = [
        # Pre-F058: used in counterfactual
        _make_decision(0.85, None, None, "success", old_date, is_post_f058=False),
        # Post-F058: NOT used in counterfactual
        _make_decision(0.76, 1.0, _HISTORICAL_F058_FACTOR, "failure",
                      new_date, calibration_applied_at=new_date),
    ]

    mock_conn.fetch.side_effect = [rows, rows]

    result = await run(mock_conn, "test-agent", _DEFAULT_FACTOR)

    # Counterfactual should have exactly 1 decision (the pre-F058 one)
    assert result["counterfactual"]["raw"]["n"] == 1
    assert result["counterfactual"]["calibrated"]["n"] == 1


@pytest.mark.asyncio
async def test_sanity_check_integrity_all_factors(mock_conn):
    """Integrity check (1a) should validate all provenanced rows.

    Every row with calibration_factor should satisfy:
    confidence == calibrate_confidence(confidence_raw, calibration_factor)
    regardless of which era it's from.
    """
    from nous.brain.calibration_scaling import calibrate_confidence

    date = _ERA_CUTOFF_DATE + timedelta(days=1)

    raw = 0.95
    # Correctly calibrated rows
    correct_legacy = calibrate_confidence(raw, _HISTORICAL_F058_FACTOR)
    correct_current = calibrate_confidence(raw, _DEFAULT_FACTOR)
    # Incorrectly calibrated row
    wrong = raw * 0.5  # Wrong multiplier

    rows_reviewed = [
        _make_decision(correct_legacy, raw, _HISTORICAL_F058_FACTOR, "success",
                      date, calibration_applied_at=date),
        _make_decision(correct_current, raw, _DEFAULT_FACTOR, "failure",
                      date, calibration_applied_at=date),
    ]

    rows_all = rows_reviewed + [
        # This row has wrong confidence for its factor
        _make_decision(wrong, raw, _DEFAULT_FACTOR, "success",
                      date, calibration_applied_at=date),
    ]

    mock_conn.fetch.side_effect = [rows_reviewed, rows_all]

    result = await run(mock_conn, "test-agent", _DEFAULT_FACTOR)

    # Integrity check should fail
    assert not result["sanity"]["ok"]
    assert result["sanity"]["n_bad_integrity"] == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
