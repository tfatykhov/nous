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

    def __init__(self, row, sink, boom=False):
        self._row = row
        self._sink = sink
        self._boom = boom

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt, params=None):
        self._sink.append((str(stmt), params))
        if self._boom:
            raise RuntimeError("connection reset")
        result = MagicMock()
        result.fetchone.return_value = self._row
        return result


class _FakeDB:
    def __init__(self, row, sink, boom=False):
        self._row = row
        self._sink = sink
        self._boom = boom

    def session(self):
        return _FakeSession(self._row, self._sink, self._boom)


def _drift_check(db, agent_id="agent-b"):
    from nous.heartbeat.checks import BehaviorDriftCheck

    check = BehaviorDriftCheck.__new__(BehaviorDriftCheck)
    check._db = db
    check._bus_stats = None
    check._last_snapshot = None
    check._last_anomalies = []
    check._last_counts_ok = False  # real _capture_snapshot overwrites this each tick
    check._settings = MagicMock(agent_id=agent_id)
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
        # No clock cutoff may reach the count query -- agent scoping only.
        assert set(params) == {"aid"}
        assert not any(isinstance(v, datetime) for v in params.values())
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


class TestCountsAreAgentScoped:
    """The snapshot these counts feed is written and read back under agent_id
    (_store_snapshot / _load_baseline), so an unscoped count lets another
    agent on a shared database move this agent's deltas -- and, for the
    residualized pair, lets agent A's prune explain away agent B's fact drop.
    """

    _ROW = dict(facts=900, episodes=10, censors=2, procedures=5,
                inactive_facts=100)

    @pytest.mark.asyncio
    async def test_every_count_filters_on_the_current_agent(self):
        sink = []
        check = _drift_check(_FakeDB(_FakeRow(**self._ROW), sink), agent_id="agent-b")
        await check._capture_snapshot()
        sql, params = sink[0]
        assert params == {"aid": "agent-b"}
        # Both halves of the residualized pair must be scoped, or they stop
        # describing the same population.
        assert sql.count("agent_id = :aid") == 5
        for table in ("heart.facts", "heart.episodes", "heart.censors",
                      "heart.procedures"):
            assert table in sql

    @pytest.mark.asyncio
    async def test_no_unscoped_count_remains(self):
        sink = []
        check = _drift_check(_FakeDB(_FakeRow(**self._ROW), sink))
        await check._capture_snapshot()
        sql, _ = sink[0]
        # Each SELECT COUNT(*) subquery must carry the agent predicate.
        assert sql.count("SELECT COUNT(*)") == sql.count("agent_id = :aid")


class TestCountQueryFailureDoesNotFabricateDrift:
    """On a swallowed DB error the counts stayed at their 0 defaults, so every
    delta became -prev.count, the residual added two large negatives instead of
    cancelling, the zeroed snapshot entered the baseline, and recovery produced
    the mirror-image anomaly on the next tick.
    """

    _PREV = dict(fact_count=1000, inactive_fact_count=100, episode_count=50,
                 active_censor_count=6, procedure_count=9)

    def _prev_snapshot(self):
        return BehaviorSnapshot(timestamp=datetime.now(UTC), **self._PREV)

    @pytest.mark.asyncio
    async def test_query_exception_carries_previous_counts_forward(self):
        sink = []
        check = _drift_check(_FakeDB(None, sink, boom=True))
        check._last_snapshot = self._prev_snapshot()
        snap = await check._capture_snapshot()
        assert snap.fact_count == 1000
        assert snap.inactive_fact_count == 100
        assert snap.fact_count_delta == 0
        assert snap.facts_pruned == 0
        assert snap.episode_count_delta == 0
        assert snap.active_censor_delta == 0
        assert snap.procedure_count == 9

    @pytest.mark.asyncio
    async def test_empty_result_row_is_treated_as_failure(self):
        sink = []
        check = _drift_check(_FakeDB(None, sink))
        check._last_snapshot = self._prev_snapshot()
        snap = await check._capture_snapshot()
        assert snap.fact_count_delta == 0
        assert snap.facts_pruned == 0

    @pytest.mark.asyncio
    async def test_failure_on_the_first_tick_aborts_instead_of_zeroing(self):
        """Superseded round-2 behavior: this used to publish an all-zero
        snapshot. See TestStartupCountFailureAbortsTheTick for why that was
        worse than skipping -- the zero became the baseline."""
        sink = []
        check = _drift_check(_FakeDB(None, sink, boom=True))
        assert await check._capture_snapshot() is None

    @pytest.mark.asyncio
    async def test_recovery_after_failure_does_not_mirror_an_anomaly(self):
        """Tick 1 fails, tick 2 recovers with unchanged real counts -> the
        recovery tick must report no movement, not a rebound spike."""
        check = _drift_check(_FakeDB(None, [], boom=True))
        check._last_snapshot = self._prev_snapshot()
        failed = await check._capture_snapshot()
        check._last_snapshot = failed

        check._db = _FakeDB(_FakeRow(facts=1000, episodes=50, censors=6,
                                     procedures=9, inactive_facts=100), [])
        recovered = await check._capture_snapshot()
        assert recovered.fact_count_delta == 0
        assert recovered.facts_pruned == 0


class TestZeroVarianceResidualBaseline:
    """Residualization makes a constant baseline the NORMAL case: when every
    historical change was explained, the residual series is all zeros. Skipping
    on zero variance would then let residualization silence the very metric it
    exists to sharpen.
    """

    def _history(self, pairs):
        return [
            BehaviorSnapshot(timestamp=datetime.now(UTC),
                             fact_count_delta=d, facts_pruned=p)
            for d, p in pairs
        ]

    def test_material_drop_fires_against_a_flat_zero_residual(self):
        # Raw deltas vary, but every one is fully explained -> residual == 0.
        history = self._history([(-10, 10), (-40, 40), (-5, 5)] * 4)
        detector = DriftDetector()
        current = BehaviorSnapshot(timestamp=datetime.now(UTC),
                                   fact_count_delta=-100, facts_pruned=0)
        anomalies = detector.detect(current, history)
        found = [a for a in anomalies if a.metric == "fact_count_delta"]
        assert len(found) == 1
        assert found[0].current == -100
        assert found[0].z_score is None, "undefined sigma must not be faked"
        assert found[0].stddev == 0.0
        assert found[0].direction == "down"
        assert found[0].severity == "alert"
        assert found[0].residualized_by == "facts_pruned"

    def test_nonzero_constant_residual_baseline_also_alerts(self):
        """Codex's example: every historical residual is 5."""
        history = self._history([(-5, 10), (-35, 40), (0, 5)] * 4)
        assert {d + p for d, p in [(-5, 10), (-35, 40), (0, 5)]} == {5}
        current = BehaviorSnapshot(timestamp=datetime.now(UTC),
                                   fact_count_delta=-100, facts_pruned=0)
        found = [a for a in DriftDetector().detect(current, history)
                 if a.metric == "fact_count_delta"]
        assert len(found) == 1
        assert found[0].mean == 5.0

    def test_explained_change_stays_quiet_against_a_flat_baseline(self):
        """The fallback must not turn residualization into a noise machine:
        a fully-explained drop still residualizes to 0 and says nothing."""
        history = self._history([(-10, 10), (-40, 40), (-5, 5)] * 4)
        current = BehaviorSnapshot(timestamp=datetime.now(UTC),
                                   fact_count_delta=-500, facts_pruned=500)
        found = [a for a in DriftDetector().detect(current, history)
                 if a.metric == "fact_count_delta"]
        assert found == []

    def test_immaterial_departure_from_a_flat_baseline_stays_quiet(self):
        """Below the 50-fact materiality floor -> still no alert, because the
        floor is the only scale the fallback has."""
        history = self._history([(-10, 10), (-40, 40), (-5, 5)] * 4)
        current = BehaviorSnapshot(timestamp=datetime.now(UTC),
                                   fact_count_delta=-20, facts_pruned=0)
        found = [a for a in DriftDetector().detect(current, history)
                 if a.metric == "fact_count_delta"]
        assert found == []

    def test_metric_without_a_floor_still_skips_on_zero_variance(self):
        """A metric with no min_abs_deviation gives the fallback no scale to
        judge against, so a constant baseline must stay silent."""
        history = [
            BehaviorSnapshot(timestamp=datetime.now(UTC), episodes_compacted=0)
            for _ in range(12)
        ]
        current = BehaviorSnapshot(timestamp=datetime.now(UTC),
                                   episodes_compacted=99)
        found = [a for a in DriftDetector().detect(current, history)
                 if a.metric == "episodes_compacted"]
        assert found == []


class TestStartupCountFailureAbortsTheTick:
    """Round-2 left a hole: with prev None there was nothing to carry forward,
    so an all-zero snapshot was installed as _last_snapshot and the next
    successful tick reported the whole corpus as a fresh delta.
    """

    @pytest.mark.asyncio
    async def test_capture_returns_none_on_startup_failure(self):
        check = _drift_check(_FakeDB(None, [], boom=True))
        assert await check._capture_snapshot() is None

    @pytest.mark.asyncio
    async def test_run_skips_the_tick_without_storing_a_snapshot(self):
        from nous.heartbeat.checks import BehaviorDriftCheck

        check = BehaviorDriftCheck.__new__(BehaviorDriftCheck)
        check._detector = MagicMock()
        check._last_snapshot = None
        check._last_anomalies = []
        check._capture_snapshot = AsyncMock(return_value=None)
        check._load_baseline = AsyncMock(return_value=[object()] * 20)
        check._store_snapshot = AsyncMock()

        result = await check.run()
        assert result.has_updates is False
        assert result.findings == []
        check._store_snapshot.assert_not_awaited()
        check._detector.detect.assert_not_called()
        assert check._last_snapshot is None, "must not seed a zero baseline"


class TestCountsUnavailableSupprestsStore:
    """P2 regression guard: when the count query fails but a previous snapshot
    exists, run() must carry the previous values forward for in-memory anomaly
    detection but must NOT write the carried-forward snapshot to the DB.

    Persisting zero deltas would corrupt the baseline: every subsequent tick
    sees a mirror-image anomaly (the whole corpus appearing as a fresh delta)
    and variance collapses toward zero, making the z-score detector hypersensitive.
    """

    @pytest.mark.asyncio
    async def test_counts_failure_with_prev_suppresses_store(self):
        """Carried-forward snapshot must NOT be written to the DB."""
        from nous.heartbeat.checks import BehaviorDriftCheck
        from nous.observability.snapshots import BehaviorSnapshot

        check = BehaviorDriftCheck.__new__(BehaviorDriftCheck)
        check._detector = MagicMock()
        check._last_snapshot = None
        check._last_anomalies = []
        # Simulate: _capture_snapshot ran with counts_ok=False (carry-forward case)
        # and set _last_counts_ok=False before returning a non-None snapshot.
        check._last_counts_ok = False
        check._capture_snapshot = AsyncMock(
            return_value=BehaviorSnapshot(timestamp=datetime.now(UTC))
        )
        check._load_baseline = AsyncMock(return_value=[])
        check._store_snapshot = AsyncMock()

        await check.run()

        check._store_snapshot.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_counts_ok_persists_snapshot(self):
        """When counts are available, the snapshot is written to the DB normally."""
        from nous.heartbeat.checks import BehaviorDriftCheck
        from nous.observability.snapshots import BehaviorSnapshot

        check = BehaviorDriftCheck.__new__(BehaviorDriftCheck)
        check._detector = MagicMock()
        check._detector.detect.return_value = []
        check._last_snapshot = None
        check._last_anomalies = []
        check._last_counts_ok = True  # counts were available this tick
        check._capture_snapshot = AsyncMock(
            return_value=BehaviorSnapshot(timestamp=datetime.now(UTC))
        )
        check._load_baseline = AsyncMock(return_value=[])
        check._store_snapshot = AsyncMock()

        await check.run()

        check._store_snapshot.assert_awaited_once()


class TestBaselineExcludesLegacySnapshots:
    """Pre-rollout snapshots have facts_pruned stuck at 0 and globally scoped
    corpus counts, so mixing them into the residual baseline lets stale prune
    gaps and other agents' spikes set today's mean and stddev.
    """

    @pytest.mark.asyncio
    async def test_v1_snapshots_are_dropped_from_the_baseline(self):
        from nous.heartbeat.checks import (
            SNAPSHOT_METRICS_VERSION,
            BehaviorDriftCheck,
        )

        rows = [
            _FakeRow(timestamp=datetime.now(UTC),
                     metrics={"fact_count_delta": -900, "facts_pruned": 0}),
            _FakeRow(timestamp=datetime.now(UTC),
                     metrics={"fact_count_delta": -3, "facts_pruned": 3,
                              "metrics_version": SNAPSHOT_METRICS_VERSION}),
        ]

        class _Sess:
            async def __aenter__(self_inner): return self_inner
            async def __aexit__(self_inner, *a): return False
            async def execute(self_inner, *a, **k):
                r = MagicMock()
                r.fetchall.return_value = rows
                return r

        db = MagicMock()
        db.session = lambda: _Sess()

        check = BehaviorDriftCheck.__new__(BehaviorDriftCheck)
        check._db = db
        check._settings = MagicMock(agent_id="a")

        baseline = await check._load_baseline()
        assert len(baseline) == 1
        assert baseline[0].fact_count_delta == -3
        assert baseline[0].facts_pruned == 3

    @pytest.mark.asyncio
    async def test_stored_metrics_carry_the_version(self):
        from nous.heartbeat.checks import (
            SNAPSHOT_METRICS_VERSION,
            BehaviorDriftCheck,
        )

        captured = {}

        class _Sess:
            async def __aenter__(self_inner): return self_inner
            async def __aexit__(self_inner, *a): return False
            async def execute(self_inner, stmt, params=None):
                captured.update(params or {})
                return MagicMock()
            async def commit(self_inner): return None

        db = MagicMock()
        db.session = lambda: _Sess()

        check = BehaviorDriftCheck.__new__(BehaviorDriftCheck)
        check._db = db
        check._settings = MagicMock(agent_id="a")
        check._last_anomalies = []

        await check._store_snapshot(
            BehaviorSnapshot(timestamp=datetime.now(UTC), fact_count=5)
        )
        import json as _j
        assert _j.loads(captured["metrics"])["metrics_version"] == (
            SNAPSHOT_METRICS_VERSION
        )


class TestMassPruneIsNeverSilentOnBothMetrics:
    """Residualization cancels a mass prune out of fact_count_delta by design,
    which makes facts_pruned the only metric left that can report it. On a
    baseline of quiet zero-prune snapshots that series has no variance, so
    without a materiality floor the zero-variance branch dropped it too and
    the prune vanished from both metrics at once.
    """

    def _quiet_history(self, n=12):
        return [
            BehaviorSnapshot(timestamp=datetime.now(UTC),
                             fact_count_delta=0, facts_pruned=0)
            for _ in range(n)
        ]

    def test_mass_prune_reported_by_facts_pruned(self):
        current = BehaviorSnapshot(timestamp=datetime.now(UTC),
                                   fact_count_delta=-400, facts_pruned=400)
        anomalies = DriftDetector().detect(current, self._quiet_history())
        by_metric = {a.metric: a for a in anomalies}
        # fact_count_delta is correctly explained away...
        assert "fact_count_delta" not in by_metric
        # ...so facts_pruned must carry the signal.
        assert "facts_pruned" in by_metric
        assert by_metric["facts_pruned"].current == 400
        assert by_metric["facts_pruned"].z_score is None

    def test_small_prune_against_a_quiet_baseline_stays_silent(self):
        current = BehaviorSnapshot(timestamp=datetime.now(UTC),
                                   fact_count_delta=-3, facts_pruned=3)
        anomalies = DriftDetector().detect(current, self._quiet_history())
        assert [a for a in anomalies if a.metric == "facts_pruned"] == []


class TestBaselineRejectsIncompatibleVersionsBothWays:
    """A snapshot from a NEWER writer is as incomparable as an older one:
    during a rolling upgrade or rollback this process can share the database
    with a v3 writer.
    """

    async def _baseline_for(self, versions):
        from nous.heartbeat.checks import BehaviorDriftCheck

        rows = []
        for i, v in enumerate(versions):
            m = {"fact_count_delta": -i, "facts_pruned": i}
            if v is not None:
                m["metrics_version"] = v
            rows.append(_FakeRow(timestamp=datetime.now(UTC), metrics=m))

        class _Sess:
            async def __aenter__(self_inner): return self_inner
            async def __aexit__(self_inner, *a): return False
            async def execute(self_inner, *a, **k):
                r = MagicMock()
                r.fetchall.return_value = rows
                return r

        db = MagicMock()
        db.session = lambda: _Sess()
        check = BehaviorDriftCheck.__new__(BehaviorDriftCheck)
        check._db = db
        check._settings = MagicMock(agent_id="a")
        return await check._load_baseline()

    @pytest.mark.asyncio
    async def test_newer_version_snapshots_are_rejected(self):
        from nous.heartbeat.checks import SNAPSHOT_METRICS_VERSION

        baseline = await self._baseline_for(
            [SNAPSHOT_METRICS_VERSION + 1, SNAPSHOT_METRICS_VERSION + 1]
        )
        assert baseline == []

    @pytest.mark.asyncio
    async def test_only_the_matching_version_survives(self):
        from nous.heartbeat.checks import SNAPSHOT_METRICS_VERSION

        baseline = await self._baseline_for([
            None,                            # v1, implicit
            SNAPSHOT_METRICS_VERSION,
            SNAPSHOT_METRICS_VERSION + 1,
        ])
        assert len(baseline) == 1
        assert baseline[0].fact_count_delta == -1


class TestTrendConsumersFilterByVersion:
    """_load_baseline is not the only reader of behavior_snapshots.
    /behavior/trends returns a mean and stddev over the window, and the
    observability dashboard charts fact_count_delta over 7 days — both
    aggregated across rows without checking the version, so for as long as v1
    rows remain in the window they blended global (v1) and agent-scoped (v2)
    fact metrics into the same statistics and the same line.
    """

    def _rest_source(self):
        from pathlib import Path

        return (Path(__file__).resolve().parents[1]
                / "nous/api/rest.py").read_text()

    def test_predicate_matches_the_stamp_written_by_store_snapshot(self):
        from nous.observability.snapshots import (
            CURRENT_METRICS_VERSION_SQL,
            SNAPSHOT_METRICS_VERSION,
        )

        assert CURRENT_METRICS_VERSION_SQL.endswith(
            f"= {SNAPSHOT_METRICS_VERSION}"
        )
        # Unstamped rows must fall back to v1, not to the current version --
        # COALESCE'ing to the current value would defeat the whole guard.
        assert "COALESCE" in CURRENT_METRICS_VERSION_SQL
        assert ", 1)" in CURRENT_METRICS_VERSION_SQL

    def _metric_aggregating_reads(self):
        """Every query that reads `metrics` across MORE THAN ONE row.

        A LIMIT 1 read shows the newest snapshot as written and cannot blend
        definitions, and the anomalies-only reads carry no metric statistics,
        so neither needs the filter. Only cross-row metric aggregation does.
        """
        src = self._rest_source()
        reads = []
        marker = "FROM nous_system.behavior_snapshots "
        i = src.find(marker)
        while i != -1:
            stmt = src[i:i + 420]
            end = stmt.find("), {")
            stmt = stmt[:end] if end != -1 else stmt
            if "metrics" in src[max(0, i - 120):i] and "LIMIT 1" not in stmt:
                reads.append(stmt)
            i = src.find(marker, i + 1)
        return reads

    def test_every_cross_row_metric_reader_applies_the_filter(self):
        """Guard against a new aggregating consumer being added without it."""
        reads = self._metric_aggregating_reads()
        # /behavior/trends and the observability dashboard's drift_trends.
        assert len(reads) == 2, f"unexpected reader set: {reads}"
        for stmt in reads:
            assert "CURRENT_METRICS_VERSION_SQL" in stmt, (
                f"unfiltered cross-row metric read: {stmt}"
            )

    def test_single_row_readers_are_deliberately_unfiltered(self):
        """Pinning the reasoning: a LIMIT 1 read must NOT be filtered, or the
        dashboard would show nothing at all until this build writes its first
        snapshot after a rollback."""
        src = self._rest_source()
        latest = src[src.find("async def behavior_snapshot_latest"):][:700]
        assert "LIMIT 1" in latest
        assert "CURRENT_METRICS_VERSION_SQL" not in latest

    def test_trends_endpoint_reports_the_version(self):
        src = self._rest_source()
        assert '"metrics_version": SNAPSHOT_METRICS_VERSION' in src, (
            "callers need to distinguish a quiet week from a window "
            "truncated by the version change"
        )
