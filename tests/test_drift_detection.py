"""Tests for F035.3: Behavioral drift detection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from nous.observability.drift import DriftDetector
from nous.observability.snapshots import BehaviorSnapshot

# ------------------------------------------------------------------
# BehaviorSnapshot tests
# ------------------------------------------------------------------


class TestBehaviorSnapshot:
    def test_creation_defaults(self):
        now = datetime.now(UTC)
        snap = BehaviorSnapshot(timestamp=now)
        assert snap.timestamp == now
        assert snap.fact_count == 0
        assert snap.handler_error_rate == 0.0
        assert snap.interval_changes == []

    def test_to_metrics_dict(self):
        now = datetime.now(UTC)
        snap = BehaviorSnapshot(
            timestamp=now,
            fact_count=10,
            fact_count_delta=3,
            handler_error_rate=0.05,
            sleep_ran=True,
        )
        d = snap.to_metrics_dict()
        assert d["fact_count"] == 10
        assert d["fact_count_delta"] == 3
        assert d["handler_error_rate"] == 0.05
        assert d["sleep_ran"] == 1  # bool -> int
        # Ensure all expected keys are present
        assert "episode_count" in d
        assert "events_processed" in d
        assert "tool_calls" in d

    def test_to_metrics_dict_completeness(self):
        """All numeric fields should appear in the metrics dict."""
        now = datetime.now(UTC)
        snap = BehaviorSnapshot(timestamp=now)
        d = snap.to_metrics_dict()
        # Should have all numeric fields (excluding timestamp and interval_changes)
        expected_keys = {
            "fact_count",
            "fact_count_delta",
            "episode_count",
            "episode_count_delta",
            "active_censor_count",
            "active_censor_delta",
            "procedure_count",
            "decision_count",
            "facts_admitted",
            "facts_rejected_dedup",
            "facts_rejected_admission",
            "admission_rate",
            "checks_run",
            "findings_created",
            "findings_resolved",
            "triage_sessions_opened",
            "sleep_ran",
            "episodes_compacted",
            "facts_pruned",
            "contradictions_resolved",
            "events_processed",
            "events_dropped",
            "handler_error_count",
            "handler_error_rate",
            "turns_processed",
            "avg_turn_latency_ms",
            "tool_calls",
        }
        assert set(d.keys()) == expected_keys


# ------------------------------------------------------------------
# DriftDetector tests
# ------------------------------------------------------------------


def _make_snapshot(delta: int = 0, **kwargs) -> BehaviorSnapshot:
    """Helper to create snapshots with offsets from 'now'."""
    ts = datetime.now(UTC) - timedelta(hours=delta)
    return BehaviorSnapshot(timestamp=ts, **kwargs)


class TestDriftDetector:
    def test_no_anomaly_within_threshold(self):
        """Values within k stddevs should produce no anomalies."""
        detector = DriftDetector()
        # 15 baseline snapshots with fact_count_delta around 5 +/- 1
        history = [_make_snapshot(delta=i, fact_count_delta=5 + (i % 3 - 1)) for i in range(15)]
        # Current value within normal range
        current = _make_snapshot(fact_count_delta=6)
        anomalies = detector.detect(current, history)
        # fact_count_delta=6 is within 2 stddev of mean ~5
        fact_anomalies = [a for a in anomalies if a.metric == "fact_count_delta"]
        assert len(fact_anomalies) == 0

    def test_anomaly_detected_above_threshold(self):
        """Value far above mean should be flagged."""
        detector = DriftDetector()
        # 15 baseline snapshots with handler_error_count around 2
        history = [_make_snapshot(delta=i, handler_error_count=2) for i in range(15)]
        # Introduce slight variance so stddev > 0
        history[0] = _make_snapshot(delta=0, handler_error_count=3)
        history[1] = _make_snapshot(delta=1, handler_error_count=1)
        # Current value way above normal
        current = _make_snapshot(handler_error_count=50)
        anomalies = detector.detect(current, history)
        error_anomalies = [a for a in anomalies if a.metric == "handler_error_count"]
        assert len(error_anomalies) == 1
        assert error_anomalies[0].direction == "up"
        assert error_anomalies[0].severity in ("warning", "alert")

    def test_min_samples_guard(self):
        """Should not detect anomalies with insufficient history."""
        detector = DriftDetector()
        # Only 3 samples, below min_samples for all metrics
        history = [_make_snapshot(delta=i, handler_error_rate=0.01) for i in range(3)]
        current = _make_snapshot(handler_error_rate=0.99)
        anomalies = detector.detect(current, history)
        assert len(anomalies) == 0

    def test_zero_stddev_skipped(self):
        """Identical values (stddev=0) should not cause division by zero."""
        detector = DriftDetector()
        # All values identical
        history = [_make_snapshot(delta=i, handler_error_count=5) for i in range(15)]
        current = _make_snapshot(handler_error_count=100)
        anomalies = detector.detect(current, history)
        error_anomalies = [a for a in anomalies if a.metric == "handler_error_count"]
        # stddev is 0, so this metric should be skipped
        assert len(error_anomalies) == 0

    def test_downward_anomaly(self):
        """Value far below mean should be flagged as 'down'."""
        detector = DriftDetector()
        # Baseline with handler_error_count around 50
        history = [_make_snapshot(delta=i, handler_error_count=50 + (i % 5)) for i in range(15)]
        current = _make_snapshot(handler_error_count=0)
        anomalies = detector.detect(current, history)
        error_anomalies = [a for a in anomalies if a.metric == "handler_error_count"]
        assert len(error_anomalies) == 1
        assert error_anomalies[0].direction == "down"

    def test_alert_severity_at_high_z(self):
        """Z-score >= 3.0 should yield 'alert' severity."""
        detector = DriftDetector()
        # handler_error_rate: k=1.5, so even moderate z should trigger
        # Use values with known stddev
        history = [_make_snapshot(delta=i, handler_error_rate=0.1) for i in range(10)]
        history[0] = _make_snapshot(delta=0, handler_error_rate=0.11)
        history[1] = _make_snapshot(delta=1, handler_error_rate=0.09)
        # Current value extremely high
        current = _make_snapshot(handler_error_rate=0.9)
        anomalies = detector.detect(current, history)
        rate_anomalies = [a for a in anomalies if a.metric == "handler_error_rate"]
        assert len(rate_anomalies) == 1
        assert rate_anomalies[0].severity == "alert"


# ------------------------------------------------------------------
# BehaviorDriftCheck tests
# ------------------------------------------------------------------


class TestBehaviorDriftCheck:
    def test_initialization_and_name(self):
        """Check can be instantiated with minimal args."""
        from unittest.mock import MagicMock

        from nous.heartbeat.checks import BehaviorDriftCheck

        mock_heart = MagicMock()
        mock_brain = MagicMock()
        mock_settings = MagicMock()
        mock_settings.drift_detection_interval = 7200

        check = BehaviorDriftCheck(
            heart=mock_heart,
            brain=mock_brain,
            settings=mock_settings,
        )
        assert check.name == "behavior_drift"
        assert check.interval == 7200
        assert check.timeout == 30

    def test_default_interval(self):
        """Falls back to 3600 if setting is missing."""
        from unittest.mock import MagicMock

        from nous.heartbeat.checks import BehaviorDriftCheck

        mock_settings = MagicMock(spec=[])  # No attributes
        check = BehaviorDriftCheck(
            heart=MagicMock(),
            brain=MagicMock(),
            settings=mock_settings,
        )
        assert check.interval == 3600
