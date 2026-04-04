"""Tests for F034.3 Self-Tuning Heartbeat — tuner, parameter bounds, rollback.

20 test cases across 6 test classes:
- TestTuningEngine (4): relax on high negatives, tighten on high positives, no change on balanced, max 1 param per check
- TestParameterBounds (3): cannot exceed min/max, learning rate limits step, pinned params skip
- TestCrossCycleRollback (3): snapshot before adjust, rollback on degradation, no rollback on improvement
- TestTuningReport (3): format includes all adjustments, skipped checks listed, generate_report_text output
- TestMinSamples (2): insufficient data skips, exactly min_samples proceeds
- TestTunerIntegration (3): full cycle with FindingStore, multiple checks, report generation
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from nous.heartbeat.finding_store import FindingStore
from nous.heartbeat.registry import BaseCheck, CheckRegistry
from nous.heartbeat.schemas import (
    CheckResult,
    EscalationConfig,
    Finding,
    FindingState,
    OutcomeSignal,
    TrackedFinding,
    TunableParam,
    TuningReport,
)
from nous.heartbeat.tuner import HeartbeatTuner


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TunableDummyCheck(BaseCheck):
    """Check with tunable parameters for testing the tuner."""

    name = "tunable_dummy"
    interval = 60

    def __init__(self, param_value: float = 5.0, pinned: bool = False) -> None:
        super().__init__()
        self._params = {
            "threshold": TunableParam(
                name="threshold",
                value=param_value,
                min_val=1.0,
                max_val=10.0,
                step=1.0,
                pinned=pinned,
            ),
        }

    async def run(self) -> CheckResult:
        return CheckResult()


class MultiParamCheck(BaseCheck):
    """Check with multiple tunable parameters."""

    name = "multi_param"
    interval = 60

    def __init__(self) -> None:
        super().__init__()
        self._params = {
            "alpha": TunableParam("alpha", 5.0, 1.0, 10.0, 1.0),
            "beta": TunableParam("beta", 0.5, 0.0, 1.0, 0.1),
        }

    async def run(self) -> CheckResult:
        return CheckResult()


def _make_store_with_outcomes(
    check_name: str,
    outcomes: list[OutcomeSignal],
) -> FindingStore:
    """Create a FindingStore pre-loaded with outcomes for a given check."""
    store = FindingStore()
    store._startup_time = datetime.now(UTC) - timedelta(hours=1)

    now = datetime.now(UTC)
    for i, signal in enumerate(outcomes):
        fp = f"fp-{check_name}-{i}"
        finding = Finding(
            source="test",
            summary=f"Finding {i}",
            check_name=check_name,
        )
        tracked = TrackedFinding(
            finding=finding,
            fingerprint=fp,
            state=FindingState.ACKNOWLEDGED,
            first_seen=now - timedelta(hours=i),
            last_seen=now,
            outcome=signal,
            outcome_at=now,
        )
        store._findings[fp] = tracked

    return store


# ===========================================================================
# TestTuningEngine — 4 tests
# ===========================================================================


class TestTuningEngine:
    """Tests for core tuning logic — relax, tighten, no-change."""

    @pytest.mark.asyncio
    async def test_relax_on_high_negatives(self):
        """When >60% outcomes are negative, tuner relaxes parameters."""
        # 7/10 negative = 70% -> should relax
        outcomes = (
            [OutcomeSignal.NEGATIVE] * 7
            + [OutcomeSignal.POSITIVE] * 3
        )
        store = _make_store_with_outcomes("tunable_dummy", outcomes)

        registry = CheckRegistry()
        check = TunableDummyCheck(param_value=5.0)
        registry.register(check)

        tuner = HeartbeatTuner()
        report = await tuner.tune(store, registry)

        assert len(report.adjustments) >= 1
        adj = report.adjustments[0]
        assert adj.direction == "relax"
        assert adj.new_value > adj.old_value  # relax = increase

    @pytest.mark.asyncio
    async def test_tighten_on_high_positives(self):
        """When >80% outcomes are positive, tuner tightens parameters."""
        # 9/10 positive = 90% -> should tighten
        outcomes = (
            [OutcomeSignal.POSITIVE] * 9
            + [OutcomeSignal.NEGATIVE] * 1
        )
        store = _make_store_with_outcomes("tunable_dummy", outcomes)

        registry = CheckRegistry()
        check = TunableDummyCheck(param_value=5.0)
        registry.register(check)

        tuner = HeartbeatTuner()
        report = await tuner.tune(store, registry)

        assert len(report.adjustments) >= 1
        adj = report.adjustments[0]
        assert adj.direction == "tighten"
        assert adj.new_value < adj.old_value  # tighten = decrease

    @pytest.mark.asyncio
    async def test_no_change_on_balanced(self):
        """When outcomes are balanced, tuner makes no changes."""
        # 5/10 positive, 5/10 negative = balanced -> no change
        outcomes = (
            [OutcomeSignal.POSITIVE] * 5
            + [OutcomeSignal.NEGATIVE] * 5
        )
        store = _make_store_with_outcomes("tunable_dummy", outcomes)

        registry = CheckRegistry()
        check = TunableDummyCheck(param_value=5.0)
        registry.register(check)

        tuner = HeartbeatTuner()
        report = await tuner.tune(store, registry)

        # Should have no adjustments (not >60% neg, not >80% pos)
        non_rollback = [a for a in report.adjustments if a.direction != "rollback"]
        assert len(non_rollback) == 0

    @pytest.mark.asyncio
    async def test_max_one_param_per_check(self):
        """Tuner adjusts at most 1 parameter per check per cycle."""
        # Use multi-param check with high negatives -> wants to relax both
        outcomes = [OutcomeSignal.NEGATIVE] * 10
        store = _make_store_with_outcomes("multi_param", outcomes)

        registry = CheckRegistry()
        check = MultiParamCheck()
        registry.register(check)

        tuner = HeartbeatTuner()
        report = await tuner.tune(store, registry)

        # Should adjust at most 1 (not both alpha and beta)
        non_rollback = [a for a in report.adjustments if a.direction != "rollback"]
        assert len(non_rollback) <= 1


# ===========================================================================
# TestParameterBounds — 3 tests
# ===========================================================================


class TestParameterBounds:
    """Tests for parameter bounds enforcement during tuning."""

    @pytest.mark.asyncio
    async def test_cannot_exceed_bounds(self):
        """Tuning cannot push parameter beyond min/max."""
        # Start at max (10.0), try to relax (increase) — should stay at 10.0
        outcomes = [OutcomeSignal.NEGATIVE] * 10
        store = _make_store_with_outcomes("tunable_dummy", outcomes)

        registry = CheckRegistry()
        check = TunableDummyCheck(param_value=10.0)  # already at max
        registry.register(check)

        tuner = HeartbeatTuner()
        report = await tuner.tune(store, registry)

        # The param should still be at max (clamped)
        p = check.get_param("threshold")
        assert p is not None
        assert p.value <= 10.0

    @pytest.mark.asyncio
    async def test_learning_rate_limits_step(self):
        """Step size is limited by learning rate * range."""
        outcomes = [OutcomeSignal.NEGATIVE] * 10
        store = _make_store_with_outcomes("tunable_dummy", outcomes)

        registry = CheckRegistry()
        check = TunableDummyCheck(param_value=5.0)
        registry.register(check)

        old_value = check.get_param_value("threshold")
        tuner = HeartbeatTuner()
        report = await tuner.tune(store, registry)

        if report.adjustments:
            adj = report.adjustments[0]
            delta = abs(adj.new_value - adj.old_value)
            # Max step = range (9) * step (1) * learning_rate (0.1) = 0.9
            # Also capped at 10% of range = 0.9
            assert delta <= 0.9 + 0.01  # small epsilon for float

    @pytest.mark.asyncio
    async def test_pinned_params_skip(self):
        """Pinned parameters are never adjusted."""
        outcomes = [OutcomeSignal.NEGATIVE] * 10
        store = _make_store_with_outcomes("tunable_dummy", outcomes)

        registry = CheckRegistry()
        check = TunableDummyCheck(param_value=5.0, pinned=True)
        registry.register(check)

        tuner = HeartbeatTuner()
        report = await tuner.tune(store, registry)

        # No adjustments since the only param is pinned
        effective_adjs = [
            a for a in report.adjustments
            if a.direction != "rollback" and a.old_value != a.new_value
        ]
        assert len(effective_adjs) == 0
        assert check.get_param_value("threshold") == 5.0  # unchanged


# ===========================================================================
# TestCrossCycleRollback — 3 tests
# ===========================================================================


class TestCrossCycleRollback:
    """Tests for cross-cycle rollback on degradation."""

    @pytest.mark.asyncio
    async def test_snapshot_before_adjust(self):
        """Tuner creates a snapshot of params before adjusting."""
        outcomes = [OutcomeSignal.NEGATIVE] * 10
        store = _make_store_with_outcomes("tunable_dummy", outcomes)

        registry = CheckRegistry()
        check = TunableDummyCheck(param_value=5.0)
        registry.register(check)

        tuner = HeartbeatTuner()
        await tuner.tune(store, registry)

        # Snapshot should exist
        assert "tunable_dummy" in tuner._snapshots
        assert tuner._snapshots["tunable_dummy"]["threshold"] == 5.0

    @pytest.mark.asyncio
    async def test_rollback_on_degradation(self):
        """When outcomes degrade (>80% negative) after adjustment, rollback occurs."""
        # First pass: relax based on moderately negative outcomes
        outcomes_first = [OutcomeSignal.NEGATIVE] * 7 + [OutcomeSignal.POSITIVE] * 3
        store = _make_store_with_outcomes("tunable_dummy", outcomes_first)

        registry = CheckRegistry()
        check = TunableDummyCheck(param_value=5.0)
        registry.register(check)

        tuner = HeartbeatTuner()
        report1 = await tuner.tune(store, registry)
        adjusted_value = check.get_param_value("threshold")
        assert adjusted_value > 5.0  # was relaxed

        # Second pass: outcomes degraded (>80% negative) -> should rollback
        outcomes_second = [OutcomeSignal.NEGATIVE] * 9 + [OutcomeSignal.POSITIVE] * 1
        store2 = _make_store_with_outcomes("tunable_dummy", outcomes_second)

        report2 = await tuner.tune(store2, registry)

        # Check for rollback adjustment in report
        rollback_adjs = [a for a in report2.adjustments if a.direction == "rollback"]
        assert len(rollback_adjs) >= 1

    @pytest.mark.asyncio
    async def test_no_rollback_on_improvement(self):
        """When outcomes improve after adjustment, no rollback occurs."""
        # First pass: relax
        outcomes_first = [OutcomeSignal.NEGATIVE] * 7 + [OutcomeSignal.POSITIVE] * 3
        store = _make_store_with_outcomes("tunable_dummy", outcomes_first)

        registry = CheckRegistry()
        check = TunableDummyCheck(param_value=5.0)
        registry.register(check)

        tuner = HeartbeatTuner()
        await tuner.tune(store, registry)

        # Second pass: outcomes improved (only 30% negative) -> no rollback
        outcomes_second = [OutcomeSignal.NEGATIVE] * 3 + [OutcomeSignal.POSITIVE] * 7
        store2 = _make_store_with_outcomes("tunable_dummy", outcomes_second)

        report2 = await tuner.tune(store2, registry)

        rollback_adjs = [a for a in report2.adjustments if a.direction == "rollback"]
        assert len(rollback_adjs) == 0


# ===========================================================================
# TestTuningReport — 3 tests
# ===========================================================================


class TestTuningReport:
    """Tests for TuningReport formatting and content."""

    def test_empty_report(self):
        """Empty report (no adjustments or skips) generates simple message."""
        tuner = HeartbeatTuner()
        report = TuningReport(timestamp=datetime.now(UTC))
        text = tuner.generate_report_text(report)
        assert "No changes needed" in text

    @pytest.mark.asyncio
    async def test_report_includes_adjustments(self):
        """Report text includes all adjustment details."""
        outcomes = [OutcomeSignal.NEGATIVE] * 10
        store = _make_store_with_outcomes("tunable_dummy", outcomes)

        registry = CheckRegistry()
        check = TunableDummyCheck(param_value=5.0)
        registry.register(check)

        tuner = HeartbeatTuner()
        report = await tuner.tune(store, registry)
        text = tuner.generate_report_text(report)

        assert "tunable_dummy" in text
        assert "threshold" in text
        assert "relax" in text

    @pytest.mark.asyncio
    async def test_report_lists_skipped_checks(self):
        """Report text lists checks skipped due to insufficient data."""
        # Only 3 outcomes, but MIN_SAMPLES=10
        outcomes = [OutcomeSignal.POSITIVE] * 3
        store = _make_store_with_outcomes("tunable_dummy", outcomes)

        registry = CheckRegistry()
        check = TunableDummyCheck(param_value=5.0)
        registry.register(check)

        tuner = HeartbeatTuner()
        report = await tuner.tune(store, registry)
        text = tuner.generate_report_text(report)

        assert "tunable_dummy" in text
        assert "insufficient data" in text.lower()


# ===========================================================================
# TestMinSamples — 2 tests
# ===========================================================================


class TestMinSamples:
    """Tests for minimum sample threshold."""

    @pytest.mark.asyncio
    async def test_insufficient_data_skips(self):
        """Check with fewer than MIN_SAMPLES outcomes is skipped."""
        outcomes = [OutcomeSignal.NEGATIVE] * 5  # below default 10
        store = _make_store_with_outcomes("tunable_dummy", outcomes)

        registry = CheckRegistry()
        check = TunableDummyCheck(param_value=5.0)
        registry.register(check)

        tuner = HeartbeatTuner()
        report = await tuner.tune(store, registry)

        assert "tunable_dummy" in report.skipped_checks
        non_rollback = [a for a in report.adjustments if a.direction != "rollback"]
        assert len(non_rollback) == 0

    @pytest.mark.asyncio
    async def test_exactly_min_samples_proceeds(self):
        """Check with exactly MIN_SAMPLES outcomes is processed."""
        outcomes = [OutcomeSignal.NEGATIVE] * 10  # exactly 10 = MIN_SAMPLES
        store = _make_store_with_outcomes("tunable_dummy", outcomes)

        registry = CheckRegistry()
        check = TunableDummyCheck(param_value=5.0)
        registry.register(check)

        tuner = HeartbeatTuner()
        report = await tuner.tune(store, registry)

        assert "tunable_dummy" not in report.skipped_checks


# ===========================================================================
# TestTunerIntegration — 3 tests
# ===========================================================================


class TestTunerIntegration:
    """Integration tests with FindingStore + Registry + Tuner."""

    @pytest.mark.asyncio
    async def test_full_cycle(self):
        """Full cycle: ingest findings, record outcomes, run tuner."""
        store = FindingStore()
        store._startup_time = datetime.now(UTC) - timedelta(hours=1)

        registry = CheckRegistry()
        check = TunableDummyCheck(param_value=5.0)
        registry.register(check)

        # Ingest findings and record negative outcomes
        # Use alphabetic labels to avoid fingerprint collision (numbers get normalized)
        labels = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot",
                  "golf", "hotel", "india", "juliet", "kilo", "lima"]
        for label in labels:
            f = Finding(source="test", summary=f"Noisy finding {label}", check_name="tunable_dummy")
            store.ingest(f)
            fp = f.fingerprint()
            store.acknowledge(fp)
            store.record_outcome(fp, OutcomeSignal.NEGATIVE)

        tuner = HeartbeatTuner()
        report = await tuner.tune(store, registry)

        assert len(report.adjustments) >= 1
        assert report.timestamp is not None
        assert tuner.last_report is not None

    @pytest.mark.asyncio
    async def test_multiple_checks(self):
        """Tuner processes multiple checks in one pass."""
        store = FindingStore()
        store._startup_time = datetime.now(UTC) - timedelta(hours=1)

        registry = CheckRegistry()
        check1 = TunableDummyCheck(param_value=5.0)
        check1.name = "check_a"
        check2 = TunableDummyCheck(param_value=5.0)
        check2.name = "check_b"
        registry.register(check1)
        registry.register(check2)

        # Pre-load outcomes for both checks
        now = datetime.now(UTC)
        for i in range(10):
            for check_name in ("check_a", "check_b"):
                fp = f"fp-{check_name}-{i}"
                finding = Finding(source="test", summary=f"Finding {i}", check_name=check_name)
                tracked = TrackedFinding(
                    finding=finding,
                    fingerprint=fp,
                    state=FindingState.ACKNOWLEDGED,
                    first_seen=now - timedelta(hours=i),
                    last_seen=now,
                    outcome=OutcomeSignal.NEGATIVE,
                    outcome_at=now,
                )
                store._findings[fp] = tracked

        tuner = HeartbeatTuner()
        report = await tuner.tune(store, registry)

        # Both checks should have been processed (not skipped)
        assert "check_a" not in report.skipped_checks
        assert "check_b" not in report.skipped_checks

    @pytest.mark.asyncio
    async def test_report_generation(self):
        """generate_report_text produces readable output."""
        store = _make_store_with_outcomes(
            "tunable_dummy",
            [OutcomeSignal.NEGATIVE] * 10,
        )

        registry = CheckRegistry()
        check = TunableDummyCheck(param_value=5.0)
        registry.register(check)

        tuner = HeartbeatTuner()
        report = await tuner.tune(store, registry)
        text = tuner.generate_report_text(report)

        assert isinstance(text, str)
        assert len(text) > 0
        assert "Heartbeat Tuning Report" in text
