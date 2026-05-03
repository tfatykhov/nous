"""Tests for nous_eval.probes.sleep_cycle_health."""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from nous_eval.probes.sleep_cycle_health import (
    _DEFAULT_BOUNDS,
    PHASES,
    CycleStat,
    PhaseBounds,
    PhaseSpec,
    analyze_phase,
    overall_exit_code,
)


def _cycle(**fields) -> CycleStat:
    return CycleStat(cycle_at=datetime(2026, 5, 1), data=dict(fields))


def test_phases_well_formed():
    """Every phase must have a non-empty name + activity_field +
    description AND a corresponding entry in _DEFAULT_BOUNDS."""
    assert len(PHASES) >= 6
    for phase in PHASES:
        assert phase.name and phase.activity_field and phase.description
        assert phase.name in _DEFAULT_BOUNDS, (
            f"Phase {phase.name!r} has no entry in _DEFAULT_BOUNDS — "
            f"the analyzer will KeyError on it"
        )


def test_analyze_phase_green_when_recent_activity():
    """A phase that produced non-zero output recently is healthy
    regardless of any historical zeros."""
    phase = PhaseSpec("test", "facts_created", "test")
    bounds = PhaseBounds(zero_warn_after=5)
    cycles = [
        _cycle(facts_created=3),  # most recent
        _cycle(facts_created=0),
        _cycle(facts_created=0),
        _cycle(facts_created=0),
        _cycle(facts_created=4),
    ]
    result = analyze_phase(cycles, phase, bounds)
    assert result["verdict"] == "GREEN"
    assert result["zero_streak"] == 0


def test_analyze_phase_red_when_zero_streak_exceeds_threshold():
    """The signal we care about: phase silently doing nothing across
    many consecutive recent cycles. This is exactly the pattern we
    found on procedures_created (15 zero cycles)."""
    phase = PhaseSpec("test", "procedures_created", "test")
    bounds = PhaseBounds(zero_warn_after=10)
    cycles = [_cycle(procedures_created=0) for _ in range(15)]
    result = analyze_phase(cycles, phase, bounds)
    assert result["verdict"] == "RED"
    assert result["zero_streak"] == 15
    assert "silently broken" in result["verdict_reason"].lower()


def test_analyze_phase_yellow_in_warning_band():
    """Between half-threshold and full threshold = YELLOW (early warning,
    not yet a confirmed silent failure)."""
    phase = PhaseSpec("test", "x", "test")
    bounds = PhaseBounds(zero_warn_after=10)
    # Streak of 6 = above half (5), below full (10) → YELLOW
    cycles = [_cycle(x=0)] * 6 + [_cycle(x=3)]
    result = analyze_phase(cycles, phase, bounds)
    assert result["verdict"] == "YELLOW"


def test_analyze_phase_no_data_when_field_absent():
    """If no cycle in the window contains the activity field at all,
    we report NO_DATA — the operator may have a stale schema or the
    phase isn't yet emitting."""
    phase = PhaseSpec("test", "missing_field", "test")
    bounds = PhaseBounds(zero_warn_after=5)
    cycles = [_cycle(other_field=1) for _ in range(5)]
    result = analyze_phase(cycles, phase, bounds)
    assert result["verdict"] == "NO_DATA"
    assert result["n_cycles"] == 0


def test_analyze_phase_handles_non_numeric_gracefully():
    """A non-numeric value (e.g., bool sneaking through) must not crash;
    skip it and analyze the rest."""
    phase = PhaseSpec("test", "x", "test")
    bounds = PhaseBounds(zero_warn_after=5)
    cycles = [
        _cycle(x="not a number"),
        _cycle(x=2),
        _cycle(x=4),
    ]
    result = analyze_phase(cycles, phase, bounds)
    assert result["n_cycles"] == 2
    assert result["min"] == 2
    assert result["max"] == 4


def test_overall_exit_code_red_blocks_strict_mode():
    """--strict CI gate must surface ANY red phase."""
    phases = [
        {"verdict": "GREEN"},
        {"verdict": "GREEN"},
        {"verdict": "RED"},
    ]
    assert overall_exit_code(phases) == 1


def test_overall_exit_code_yellow_does_not_block():
    """YELLOW is early-warning only; --strict only fires on confirmed
    silent failures (RED) so CI doesn't flake on natural noise."""
    phases = [
        {"verdict": "GREEN"},
        {"verdict": "YELLOW"},
        {"verdict": "GREEN"},
    ]
    assert overall_exit_code(phases) == 0


def test_overall_exit_code_passes_when_all_green():
    phases = [{"verdict": "GREEN"}] * 5
    assert overall_exit_code(phases) == 0


def test_default_bounds_are_calibrated_for_observed_phases():
    """Each PHASES entry needs a bound — and the bound must allow at
    least 3 cycles of zeros before warning (otherwise CI flakes on
    legitimately-rare events like cluster_consolidation)."""
    for phase in PHASES:
        bounds = _DEFAULT_BOUNDS[phase.name]
        assert bounds.zero_warn_after >= 3, (
            f"{phase.name} threshold {bounds.zero_warn_after} too tight — "
            f"will fire on natural variance"
        )
