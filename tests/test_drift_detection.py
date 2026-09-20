"""Tests for F035.3: Behavioral drift detection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from unittest.mock import AsyncMock, MagicMock

from nous.observability.snapshots import BehaviorSnapshot
from nous.observability.drift import Anomaly, DriftDetector


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
            "fact_count", "fact_count_delta", "episode_count", "episode_count_delta",
            "active_censor_count", "active_censor_delta", "procedure_count", "decision_count",
            "facts_admitted", "facts_rejected_dedup", "facts_rejected_admission", "admission_rate",
            "checks_run", "findings_created", "findings_resolved", "triage_sessions_opened",
            "sleep_ran", "episodes_compacted", "facts_pruned", "contradictions_resolved",
            "events_processed", "events_dropped", "handler_error_count", "handler_error_rate",
            "turns_processed", "avg_turn_latency_ms", "tool_calls",
            "inactive_fact_count",
        }
        assert set(d.keys()) == expected_keys


# ------------------------------------------------------------------
# DriftDetector tests
# ------------------------------------------------------------------


def _make_snapshot(delta: int = 0, **kwargs) -> BehaviorSnapshot:
    """Helper to create snapshots with offsets from 'now'."""
    ts = datetime.now(UTC) - timedelta(hours=delta)
    return BehaviorSnapshot(timestamp=ts, **kwargs)


class TestBehaviorDriftCheckHasUpdates:
    """Audit HB-1: BehaviorDriftCheck.run() must set has_updates=True when it
    emits findings — the heartbeat runner gates on result.has_updates, so a
    CheckResult(findings=...) without the flag silently drops every drift alert.
    """

    def _build_check(self):
        from nous.heartbeat.checks import BehaviorDriftCheck

        check = BehaviorDriftCheck.__new__(BehaviorDriftCheck)
        # Bypass __init__ (which needs Heart/Brain); wire only what run() touches.
        from nous.heartbeat.schemas import CheckResult  # noqa: F401
        check._detector = MagicMock()
        check._last_snapshot = None
        check._last_anomalies = []
        check._capture_snapshot = AsyncMock(
            return_value=BehaviorSnapshot(timestamp=datetime.now(UTC))
        )
        check._load_baseline = AsyncMock(return_value=[object()])  # truthy baseline
        check._store_snapshot = AsyncMock()
        return check

    @pytest.mark.asyncio
    async def test_has_updates_true_when_anomaly_alert(self):
        check = self._build_check()
        check._detector.detect.return_value = [
            Anomaly(
                metric="fact_count_delta", current=99, mean=5.0, stddev=1.0,
                z_score=94.0, direction="above", severity="alert",
            )
        ]
        result = await check.run()
        assert result.findings, "expected a drift finding"
        assert result.has_updates is True

    @pytest.mark.asyncio
    async def test_has_updates_false_when_no_anomaly(self):
        check = self._build_check()
        check._detector.detect.return_value = []
        result = await check.run()
        assert result.findings == []
        assert result.has_updates is False


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


# ------------------------------------------------------------------
# Residualization + materiality floor (fact_count_delta false positive)
# ------------------------------------------------------------------


class TestResidualization:
    """A fact drop that facts_pruned explains must not alert; an unexplained
    drop of the same magnitude must still alert at full strength.

    Regression for the `fact_count_delta` false positive that was triaged by
    hand 7+ times: the explanation was already in the same snapshot, unused.
    """

    @staticmethod
    def _history():
        # Quiet baseline: small churn, nothing deactivated.
        return [_make_snapshot(fact_count_delta=d, facts_pruned=0) for d in
                (2, -1, 3, 0, 1, -2, 4, 1, 0, 2)]

    def _delta_anomalies(self, current):
        return [a for a in DriftDetector().detect(current, self._history())
                if a.metric == "fact_count_delta"]

    def test_explained_drop_is_silent(self):
        # 661 facts vanish, 669 deactivations recorded -> fully accounted for.
        current = _make_snapshot(fact_count_delta=-661, facts_pruned=669)
        assert self._delta_anomalies(current) == []

    def test_unexplained_drop_still_fires(self):
        # Same magnitude, but nothing was deactivated -> genuinely anomalous.
        current = _make_snapshot(fact_count_delta=-661, facts_pruned=0)
        anomalies = self._delta_anomalies(current)
        assert len(anomalies) == 1
        assert anomalies[0].direction == "down"
        assert anomalies[0].severity == "alert"

    def test_partially_explained_drop_fires_on_the_remainder(self):
        # Real case, 2026-09-16: -366 delta but only 11 deactivations.
        current = _make_snapshot(fact_count_delta=-366, facts_pruned=11)
        anomalies = self._delta_anomalies(current)
        assert len(anomalies) == 1
        # Reported in residual space, and labelled as such so the finding text
        # cannot pass the residual off as the raw metric.
        assert anomalies[0].residualized_by == "facts_pruned"
        assert anomalies[0].raw_current == -366
        assert anomalies[0].current == -355

    def test_small_deviation_below_floor_is_silent(self):
        # Many sigma against a near-constant series, but only ~20 facts --
        # statistically real, operationally meaningless.
        current = _make_snapshot(fact_count_delta=-20, facts_pruned=0)
        assert self._delta_anomalies(current) == []

    def test_deviation_just_above_floor_fires(self):
        # Guards the boundary so the floor cannot be raised silently.
        current = _make_snapshot(fact_count_delta=-60, facts_pruned=0)
        assert len(self._delta_anomalies(current)) == 1

    def test_floor_does_not_apply_to_rate_metrics(self):
        """admission_rate lives in [0, 1]; a 50.0 floor would mute it forever."""
        assert DriftDetector.THRESHOLDS["admission_rate"].get("min_abs_deviation", 0.0) == 0.0

    def test_non_residualized_metrics_report_raw(self):
        history = [_make_snapshot(events_dropped=n) for n in
                   (0, 1, 0, 0, 1, 0, 2, 0, 1, 0)]
        current = _make_snapshot(events_dropped=50)
        anomalies = [a for a in DriftDetector().detect(current, history)
                     if a.metric == "events_dropped"]
        assert len(anomalies) == 1
        assert anomalies[0].residualized_by is None
        assert anomalies[0].raw_current is None
        assert anomalies[0].current == 50


# ------------------------------------------------------------------
# Prune accounting + residual metadata propagation (Codex P2s, PR #641)
# ------------------------------------------------------------------


class _FakeRow:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _FakeSession:
    """Records every statement executed and replays a canned row."""

    def __init__(self, row, sink):
        self._row = row
        self._sink = sink

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt, params=None):
        self._sink.append((str(stmt), params))
        result = MagicMock()
        result.fetchone.return_value = self._row
        return result


class _FakeDB:
    def __init__(self, row, sink):
        self._row = row
        self._sink = sink

    def session(self):
        return _FakeSession(self._row, self._sink)


def _drift_check(db):
    from nous.heartbeat.checks import BehaviorDriftCheck

    check = BehaviorDriftCheck.__new__(BehaviorDriftCheck)
    check._db = db
    check._bus_stats = None
    check._last_snapshot = None
    check._last_anomalies = []
    return check


class TestPruneAccounting:
    """facts_pruned must be derived from the SAME snapshot as fact_count, by
    differencing the inactive count -- not from an ``updated_at`` wall-clock
    window.

    A window cannot be made consistent with the counts: ``heart.facts.updated_at``
    is stamped by a BEFORE UPDATE trigger with ``clock_timestamp()`` (write
    time, not commit time), so a batch prune that writes before the snapshot
    and commits after it is invisible to the counts while already sitting below
    any cutoff -- permanently lost from the next window.
    """

    _ROW = dict(facts=900, episodes=10, censors=2, procedures=5)

    @pytest.mark.asyncio
    async def test_pruned_is_inactive_count_delta(self):
        sink = []
        check = _drift_check(_FakeDB(_FakeRow(inactive_facts=140, **self._ROW), sink))
        check._last_snapshot = BehaviorSnapshot(
            timestamp=datetime.now(UTC), fact_count=1000, inactive_fact_count=100,
        )
        snap = await check._capture_snapshot()
        # 100 facts left the active set; 40 of them show up as newly inactive.
        assert snap.fact_count_delta == -100
        assert snap.facts_pruned == 40
        assert snap.inactive_fact_count == 140

    @pytest.mark.asyncio
    async def test_no_wall_clock_window_in_the_query(self):
        """The bug this replaces: a cutoff computed off the application clock
        before the query, so a deactivation landing while we waited on a pooled
        connection was dropped from the active count but excluded by the window.
        """
        sink = []
        check = _drift_check(_FakeDB(_FakeRow(inactive_facts=1, **self._ROW), sink))
        await check._capture_snapshot()
        sql, params = sink[0]
        assert "updated_at" not in sql, "prune window must not be time-based"
        assert not params, "no clock cutoff may be passed into the count query"
        # ...and both fact counts come from one statement => one MVCC snapshot.
        assert sql.count("heart.facts") == 2
        assert len(sink) == 1

    @pytest.mark.asyncio
    async def test_first_tick_after_restart_reports_zero_prunes(self):
        """prev is None, so fact_count_delta is 0; facts_pruned must be 0 too,
        or the residual would subtract an explanation for a change of nothing.
        """
        sink = []
        check = _drift_check(_FakeDB(_FakeRow(inactive_facts=140, **self._ROW), sink))
        snap = await check._capture_snapshot()
        assert snap.fact_count_delta == 0
        assert snap.facts_pruned == 0
        assert snap.inactive_fact_count == 140

    @pytest.mark.asyncio
    async def test_reactivation_nets_out(self):
        """A fact moving inactive -> active is +1 delta and -1 prune, so the
        residual is 0 rather than a phantom unexplained gain.
        """
        sink = []
        check = _drift_check(_FakeDB(_FakeRow(inactive_facts=99, **self._ROW), sink))
        check._last_snapshot = BehaviorSnapshot(
            timestamp=datetime.now(UTC), fact_count=899, inactive_fact_count=100,
        )
        snap = await check._capture_snapshot()
        assert snap.fact_count_delta == 1
        assert snap.facts_pruned == -1
        metrics = snap.to_metrics_dict()
        assert metrics["fact_count_delta"] + metrics["facts_pruned"] == 0


class TestAnomalyPersistenceCarriesResidualMetadata:
    """_last_anomalies is what gets written to nous_system.behavior_snapshots
    and replayed by /behavior/anomalies and /behavior/drift-report. If it drops
    residualized_by/raw_current, those endpoints present the residual (-355) as
    though it were the raw fact_count_delta (-366).
    """

    def _check(self, anomaly):
        from nous.heartbeat.checks import BehaviorDriftCheck

        check = BehaviorDriftCheck.__new__(BehaviorDriftCheck)
        check._detector = MagicMock()
        check._detector.detect.return_value = [anomaly]
        check._last_snapshot = None
        check._last_anomalies = []
        check._capture_snapshot = AsyncMock(
            return_value=BehaviorSnapshot(timestamp=datetime.now(UTC))
        )
        check._load_baseline = AsyncMock(return_value=[object()])
        check._store_snapshot = AsyncMock()
        return check

    @pytest.mark.asyncio
    async def test_residual_fields_survive_serialization(self):
        check = self._check(Anomaly(
            metric="fact_count_delta", current=-355, mean=1.0, stddev=2.0,
            z_score=-178.0, direction="down", severity="alert",
            residualized_by="facts_pruned", raw_current=-366,
        ))
        await check.run()
        assert len(check._last_anomalies) == 1
        stored = check._last_anomalies[0]
        assert stored["residualized_by"] == "facts_pruned"
        assert stored["raw_current"] == -366
        assert stored["current"] == -355

    @pytest.mark.asyncio
    async def test_plain_anomaly_stores_explicit_nulls(self):
        check = self._check(Anomaly(
            metric="events_dropped", current=50, mean=0.5, stddev=0.7,
            z_score=70.0, direction="up", severity="alert",
        ))
        await check.run()
        stored = check._last_anomalies[0]
        assert stored["residualized_by"] is None
        assert stored["raw_current"] is None
