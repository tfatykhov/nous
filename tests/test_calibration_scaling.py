"""Unit tests for F058 confidence calibration scaling."""

from __future__ import annotations

import pytest

from nous.brain.calibration_scaling import (
    DEFAULT_CALIBRATION_FACTOR,
    calibrate_confidence,
)


def test_default_factor_corrects_overconfidence() -> None:
    """Default factor should bring 0.834 mean confidence to ~0.636 mean accuracy."""
    # 0.834 was the empirical mean confidence; 0.636 the strict accuracy.
    # Factor should hit the target within rounding.
    result = calibrate_confidence(0.834, DEFAULT_CALIBRATION_FACTOR)
    assert abs(result - 0.636) < 0.005, f"got {result}"


def test_factor_one_is_passthrough() -> None:
    """Factor 1.0 should disable scaling (legacy behavior)."""
    for raw in (0.0, 0.25, 0.5, 0.75, 1.0):
        assert calibrate_confidence(raw, 1.0) == raw


@pytest.mark.parametrize(
    "raw,factor,expected",
    [
        (1.0, 0.7627, 0.7627),
        (0.5, 0.7627, 0.38135),
        (0.0, 0.7627, 0.0),
        (0.5, 0.5, 0.25),
    ],
)
def test_typical_scaling(raw: float, factor: float, expected: float) -> None:
    """Standard cases produce raw * factor."""
    assert abs(calibrate_confidence(raw, factor) - expected) < 1e-9


def test_clipping_at_one() -> None:
    """Factor > 1.0 with high raw value clips to 1.0."""
    # Factor > 1 corrects underconfidence; clip prevents > 1.0 outputs.
    assert calibrate_confidence(0.95, 1.2) == 1.0


def test_clipping_at_zero() -> None:
    """Negative factor clips at 0.0 (defensive only — factors should be > 0)."""
    assert calibrate_confidence(0.5, -0.1) == 0.0


def test_already_calibrated_high_confidence() -> None:
    """A high raw confidence still produces a sensible calibrated value."""
    # Confidence 0.95 raw becomes ~0.725 calibrated — no longer claiming
    # near-certainty when actual prod success rate is around 73% in the
    # [0.9, 1.0) bin.
    result = calibrate_confidence(0.95, DEFAULT_CALIBRATION_FACTOR)
    assert 0.71 < result < 0.74
