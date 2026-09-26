"""Tests for the fault detector heartbeat checks (decision 28f021a0).

Mutation evidence: for each detector, we show what the test asserts, then
demonstrate that reverting the relevant detection logic (commented inline)
would make the test fail.

Coverage:
- ProcessRecorder: start/finish/error/skip, get_recent_runs
- ProcessFaultCheck: missed-run, consecutive-errors, zero-change-collapse,
  ratio-collapse
- RetrievalCanaryCheck: hit, miss, no-op when canary_path empty
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nous.heartbeat.fault_detector import ProcessFaultCheck, RetrievalCanaryCheck
from nous.heartbeat.schemas import CheckResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_settings(**overrides) -> MagicMock:
    s = MagicMock()
    s.agent_id = "test-agent"
    s.fault_detector_check_interval = 3600
    s.fault_detector_sleep_max_gap_hours = 48
    s.fault_detector_consecutive_error_threshold = 3
    s.fault_detector_zero_change_threshold = 5
    s.fault_detector_ratio_collapse_threshold = 0.30
    s.fault_detector_ratio_baseline_window = 20
    s.fault_detector_canary_path = ""
    s.fault_detector_canary_top_k = 10
    s.fault_detector_canary_interval = 3600
    s.stale_scan_age_days = 60
    s.stale_scan_excluded_categories = ["rule"]
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


def _run(now: datetime, offset_hours: float, status: str = "finished", **extra) -> dict:
    ts = now - timedelta(hours=offset_hours)
    return {
        "id": 1,
        "status": status,
        "started_at": ts,
        "finished_at": ts + timedelta(seconds=1) if status != "started" else None,
        "items_examined": extra.get("items_examined"),
        "items_changed": extra.get("items_changed"),
        "error_message": extra.get("error_message"),
    }


# ---------------------------------------------------------------------------
# ProcessRecorder tests
# ---------------------------------------------------------------------------

class TestProcessRecorder:
    """ProcessRecorder writes to and reads from process_run_log."""

    def _make_recorder(self, execute_result=None, rowcount=1):
        from nous.observability.process_recorder import ProcessRecorder

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.commit = AsyncMock()

        if execute_result is not None:
            scalar_mock = MagicMock()
            scalar_mock.scalar_one.return_value = execute_result
            scalar_mock.rowcount = rowcount
            mock_session.execute = AsyncMock(return_value=scalar_mock)
        else:
            result_mock = MagicMock()
            result_mock.scalar_one.return_value = 42  # default row id
            result_mock.rowcount = rowcount
            mock_session.execute = AsyncMock(return_value=result_mock)

        db = MagicMock()
        db.session = MagicMock(return_value=mock_session)

        return ProcessRecorder(db, "test-agent"), mock_session

    @pytest.mark.asyncio
    async def test_start_returns_id(self):
        recorder, mock_session = self._make_recorder(execute_result=42)
        run_id = await recorder.start("sleep/stale_scan")
        assert run_id == 42
        mock_session.execute.assert_called_once()
        call_sql = str(mock_session.execute.call_args[0][0])
        assert "INSERT" in call_sql

    @pytest.mark.asyncio
    async def test_finish_sends_update(self):
        recorder, mock_session = self._make_recorder(execute_result=42)
        await recorder.finish(42, items_examined=100, items_changed=3)
        assert mock_session.execute.call_count == 1
        call_sql = str(mock_session.execute.call_args[0][0])
        assert "UPDATE" in call_sql
        assert "finished" in call_sql

    @pytest.mark.asyncio
    async def test_error_sends_update(self):
        recorder, mock_session = self._make_recorder(execute_result=42)
        await recorder.error(42, "boom")
        assert mock_session.execute.call_count == 1
        call_sql = str(mock_session.execute.call_args[0][0])
        assert "error" in call_sql

    @pytest.mark.asyncio
    async def test_noop_id_skip(self):
        """_NOOP_ID returned by start on DB error; finish/error should be no-ops."""
        from nous.observability.process_recorder import _NOOP_ID
        recorder, mock_session = self._make_recorder()
        # Simulate a DB failure on start
        mock_session.execute.side_effect = Exception("db down")
        run_id = await recorder.start("sleep/stale_scan")
        assert run_id == _NOOP_ID
        # finish/error on _NOOP_ID must not touch the DB at all
        mock_session.execute.reset_mock()
        mock_session.execute.side_effect = None
        await recorder.finish(_NOOP_ID)
        mock_session.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_recent_runs_returns_rows(self):
        from nous.observability.process_recorder import ProcessRecorder

        row = {
            "id": 1, "status": "finished", "started_at": datetime.now(UTC),
            "finished_at": datetime.now(UTC), "items_examined": 5,
            "items_changed": 2, "error_message": None,
        }
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        result_mock = MagicMock()
        result_mock.mappings.return_value.all.return_value = [row]
        mock_session.execute = AsyncMock(return_value=result_mock)

        db = MagicMock()
        db.session = MagicMock(return_value=mock_session)
        recorder = ProcessRecorder(db, "test-agent")

        rows = await recorder.get_recent_runs("sleep/stale_scan", limit=5)
        assert len(rows) == 1
        assert rows[0]["status"] == "finished"


# ---------------------------------------------------------------------------
# ProcessFaultCheck tests
# ---------------------------------------------------------------------------

class TestProcessFaultCheckMissedRun:
    """Detect 'process has not run in N hours'."""

    def _make_check(self, runs_by_process: dict[str, list[dict]]):
        settings = _mock_settings()
        db = MagicMock()
        check = ProcessFaultCheck(db=db, settings=settings, agent_id="test-agent")
        # Patch the recorder's get_recent_runs to return our fake data
        async def _fake_get_recent_runs(process_name, limit=20):
            return runs_by_process.get(process_name, [])

        recorder_mock = AsyncMock()
        recorder_mock.get_recent_runs = _fake_get_recent_runs

        with patch(
            "nous.heartbeat.fault_detector.ProcessRecorder",
            return_value=recorder_mock,
        ):
            return check, recorder_mock

    @pytest.mark.asyncio
    async def test_no_findings_when_phases_ran_recently(self):
        now = datetime.now(UTC)
        runs = {
            f"sleep/{phase}": [_run(now, 1.0)]  # 1 hour ago
            for phase in [
                "review", "prune", "reflect", "resolve_contradictions",
                "stale_scan", "cluster_consolidation", "graph_densification",
                "relink_open_episodes", "prune_dead_edges", "generalize",
            ]
        }
        check, _ = self._make_check(runs)
        settings = _mock_settings()
        db = MagicMock()
        check2 = ProcessFaultCheck(db=db, settings=settings, agent_id="test-agent")

        async def _fake_get(process_name, limit=20):
            return runs.get(process_name, [])

        async def _fake_count(*a, **kw):
            return 0

        recorder_mock = AsyncMock()
        recorder_mock.get_recent_runs = _fake_get
        with patch("nous.heartbeat.fault_detector.ProcessRecorder", return_value=recorder_mock):
            with patch.object(check2, "_count_stale_eligible", _fake_count):
                result = await check2.run()
        assert isinstance(result, CheckResult)
        assert not result.findings

    @pytest.mark.asyncio
    async def test_finding_when_phase_missed_gap(self):
        """Mutation evidence: remove the gap check and this test fails."""
        now = datetime.now(UTC)
        # stale_scan last ran 60h ago (> 48h threshold)
        runs = {
            "sleep/stale_scan": [_run(now, 60.0)],
        }
        settings = _mock_settings(fault_detector_sleep_max_gap_hours=48)
        db = MagicMock()
        check = ProcessFaultCheck(db=db, settings=settings, agent_id="test-agent")

        async def _fake_get(process_name, limit=20):
            return runs.get(process_name, [])

        async def _fake_count(*a, **kw):
            return 0

        recorder_mock = AsyncMock()
        recorder_mock.get_recent_runs = _fake_get
        with patch("nous.heartbeat.fault_detector.ProcessRecorder", return_value=recorder_mock):
            with patch.object(check, "_count_stale_eligible", _fake_count):
                result = await check.run()

        assert any("stale_scan" in f.summary for f in result.findings)
        assert any("not completed" in f.summary for f in result.findings)

    @pytest.mark.asyncio
    async def test_no_finding_when_phase_never_seen(self):
        """A phase with no rows at all is NOT flagged (no baseline yet)."""
        settings = _mock_settings()
        db = MagicMock()
        check = ProcessFaultCheck(db=db, settings=settings, agent_id="test-agent")

        async def _fake_get(process_name, limit=20):
            return []  # never seen

        recorder_mock = AsyncMock()
        recorder_mock.get_recent_runs = _fake_get
        with patch("nous.heartbeat.fault_detector.ProcessRecorder", return_value=recorder_mock):
            result = await check.run()

        # No findings for phases we've never seen (can't detect drift with no data)
        assert not result.findings


class TestProcessFaultCheckConsecutiveErrors:
    """Detect 'N consecutive error rows'."""

    @pytest.mark.asyncio
    async def test_finding_on_n_consecutive_errors(self):
        """Mutation evidence: change threshold to 4 and this test fails."""
        now = datetime.now(UTC)
        # 3 consecutive errors (threshold = 3)
        runs = {
            "sleep/stale_scan": [
                _run(now, 1.0, status="error"),
                _run(now, 2.0, status="error"),
                _run(now, 3.0, status="error"),
                _run(now, 4.0, status="finished"),  # older successful run
            ]
        }
        settings = _mock_settings(fault_detector_consecutive_error_threshold=3)
        db = MagicMock()
        check = ProcessFaultCheck(db=db, settings=settings, agent_id="test-agent")

        async def _fake_get(process_name, limit=20):
            return runs.get(process_name, [])

        async def _fake_count(*a, **kw):
            return 0

        recorder_mock = AsyncMock()
        recorder_mock.get_recent_runs = _fake_get
        with patch("nous.heartbeat.fault_detector.ProcessRecorder", return_value=recorder_mock):
            with patch.object(check, "_count_stale_eligible", _fake_count):
                result = await check.run()

        error_findings = [f for f in result.findings if "failed with errors" in f.summary]
        assert error_findings, "Expected a consecutive-error finding"
        assert error_findings[0].urgency == "high"

    @pytest.mark.asyncio
    async def test_no_finding_on_two_errors_then_success(self):
        """2 errors then a success is NOT flagged (threshold = 3)."""
        now = datetime.now(UTC)
        runs = {
            "sleep/stale_scan": [
                _run(now, 1.0, status="error"),
                _run(now, 2.0, status="error"),
                _run(now, 3.0, status="finished"),
            ]
        }
        settings = _mock_settings(fault_detector_consecutive_error_threshold=3)
        db = MagicMock()
        check = ProcessFaultCheck(db=db, settings=settings, agent_id="test-agent")

        async def _fake_get(process_name, limit=20):
            return runs.get(process_name, [])

        async def _fake_count(*a, **kw):
            return 0

        recorder_mock = AsyncMock()
        recorder_mock.get_recent_runs = _fake_get
        with patch("nous.heartbeat.fault_detector.ProcessRecorder", return_value=recorder_mock):
            with patch.object(check, "_count_stale_eligible", _fake_count):
                result = await check.run()

        error_findings = [f for f in result.findings if "failed with errors" in f.summary]
        assert not error_findings


class TestProcessFaultCheckZeroChangeCollapse:
    """Detect stale_scan running but changing nothing while facts are eligible."""

    @pytest.mark.asyncio
    async def test_finding_when_zero_changed_and_pop_nonzero(self):
        """Mutation evidence: change population check to '> 0' and items threshold
        to 1 — the test still fails if you remove the population guard."""
        now = datetime.now(UTC)
        runs = {
            "sleep/stale_scan": [
                _run(now, h, status="finished", items_examined=50, items_changed=0)
                for h in [1, 2, 3, 4, 5]
            ]
        }
        settings = _mock_settings(fault_detector_zero_change_threshold=5)
        db = MagicMock()
        check = ProcessFaultCheck(db=db, settings=settings, agent_id="test-agent")

        async def _fake_get(process_name, limit=20):
            return runs.get(process_name, [])

        async def _fake_count(*a, **kw):
            return 100  # 100 eligible facts exist

        recorder_mock = AsyncMock()
        recorder_mock.get_recent_runs = _fake_get
        with patch("nous.heartbeat.fault_detector.ProcessRecorder", return_value=recorder_mock):
            with patch.object(check, "_count_stale_eligible", _fake_count):
                result = await check.run()

        zero_findings = [f for f in result.findings if "0 deactivations" in f.summary]
        assert zero_findings, "Expected a zero-change-collapse finding"

    @pytest.mark.asyncio
    async def test_no_finding_when_pop_is_zero(self):
        """No finding when stale_scan finds nothing AND there are no eligible facts."""
        now = datetime.now(UTC)
        runs = {
            "sleep/stale_scan": [
                _run(now, h, status="finished", items_examined=0, items_changed=0)
                for h in [1, 2, 3, 4, 5]
            ]
        }
        settings = _mock_settings(fault_detector_zero_change_threshold=5)
        db = MagicMock()
        check = ProcessFaultCheck(db=db, settings=settings, agent_id="test-agent")

        async def _fake_get(process_name, limit=20):
            return runs.get(process_name, [])

        async def _fake_count(*a, **kw):
            return 0  # clean system

        recorder_mock = AsyncMock()
        recorder_mock.get_recent_runs = _fake_get
        with patch("nous.heartbeat.fault_detector.ProcessRecorder", return_value=recorder_mock):
            with patch.object(check, "_count_stale_eligible", _fake_count):
                result = await check.run()

        zero_findings = [f for f in result.findings if "0 deactivations" in f.summary]
        assert not zero_findings


class TestProcessFaultCheckRatioCollapse:
    """Detect output/input ratio collapsing vs trailing baseline."""

    @pytest.mark.asyncio
    async def test_finding_on_ratio_collapse(self):
        """Mutation evidence: remove the ratio check → no finding."""
        now = datetime.now(UTC)
        # Baseline (older runs): ratio ~0.5
        baseline = [
            _run(now, h, status="finished", items_examined=100, items_changed=50)
            for h in range(6, 26)  # 20 old runs
        ]
        # Recent (last 5 runs): ratio ~0.02 (collapsed)
        recent = [
            _run(now, h, status="finished", items_examined=100, items_changed=2)
            for h in range(1, 6)
        ]
        runs = {"sleep/stale_scan": recent + baseline}
        settings = _mock_settings(
            fault_detector_ratio_collapse_threshold=0.30,
            fault_detector_ratio_baseline_window=20,
        )
        db = MagicMock()
        check = ProcessFaultCheck(db=db, settings=settings, agent_id="test-agent")

        async def _fake_get(process_name, limit=20):
            # Return only the first `limit` rows (newest-first order)
            return runs.get(process_name, [])[:limit]

        async def _fake_count(*a, **kw):
            return 0  # don't trigger the zero-change check

        recorder_mock = AsyncMock()
        recorder_mock.get_recent_runs = _fake_get
        with patch("nous.heartbeat.fault_detector.ProcessRecorder", return_value=recorder_mock):
            with patch.object(check, "_count_stale_eligible", _fake_count):
                result = await check.run()

        ratio_findings = [f for f in result.findings if "ratio" in f.summary]
        assert ratio_findings, "Expected a ratio-collapse finding"

    @pytest.mark.asyncio
    async def test_no_finding_when_ratio_stable(self):
        now = datetime.now(UTC)
        # Consistent ~0.5 ratio across all runs
        runs = {
            "sleep/stale_scan": [
                _run(now, h, status="finished", items_examined=100, items_changed=50)
                for h in range(1, 26)
            ]
        }
        settings = _mock_settings(
            fault_detector_ratio_collapse_threshold=0.30,
            fault_detector_ratio_baseline_window=20,
        )
        db = MagicMock()
        check = ProcessFaultCheck(db=db, settings=settings, agent_id="test-agent")

        async def _fake_get(process_name, limit=20):
            return runs.get(process_name, [])[:limit]

        async def _fake_count(*a, **kw):
            return 0

        recorder_mock = AsyncMock()
        recorder_mock.get_recent_runs = _fake_get
        with patch("nous.heartbeat.fault_detector.ProcessRecorder", return_value=recorder_mock):
            with patch.object(check, "_count_stale_eligible", _fake_count):
                result = await check.run()

        ratio_findings = [f for f in result.findings if "ratio" in f.summary]
        assert not ratio_findings


# ---------------------------------------------------------------------------
# RetrievalCanaryCheck tests
# ---------------------------------------------------------------------------

class TestRetrievalCanaryCheck:
    """Test retrieval canary check."""

    def _make_fact_result(self, uid: str):
        r = MagicMock()
        r.id = uid
        return r

    @pytest.mark.asyncio
    async def test_noop_when_canary_path_empty(self):
        """Canary check is a no-op when path is not configured."""
        settings = _mock_settings(fault_detector_canary_path="")
        heart = AsyncMock()
        check = RetrievalCanaryCheck(heart=heart, settings=settings)
        result = await check.run()
        assert not result.findings
        heart.search_facts.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_finding_on_hit(self, tmp_path):
        """No finding when gold_id IS in top-K results."""
        gold_id = "11111111-1111-1111-1111-111111111111"
        canary = [
            {"query": "what is the model?", "gold_ids": [gold_id], "min_recall_at_k": 0.5}
        ]
        canary_file = tmp_path / "canary.jsonl"
        canary_file.write_text(json.dumps(canary[0]) + "\n")

        settings = _mock_settings(
            fault_detector_canary_path=str(canary_file),
            fault_detector_canary_top_k=10,
        )
        heart = AsyncMock()
        heart.search_facts = AsyncMock(return_value=[self._make_fact_result(gold_id)])

        check = RetrievalCanaryCheck(heart=heart, settings=settings)
        result = await check.run()
        assert not result.findings

    @pytest.mark.asyncio
    async def test_finding_on_miss(self, tmp_path):
        """Mutation evidence: remove the recall < min_recall check → no finding."""
        gold_id = "22222222-2222-2222-2222-222222222222"
        other_id = "33333333-3333-3333-3333-333333333333"
        canary = [
            {"query": "what is the model?", "gold_ids": [gold_id], "min_recall_at_k": 0.5}
        ]
        canary_file = tmp_path / "canary.jsonl"
        canary_file.write_text(json.dumps(canary[0]) + "\n")

        settings = _mock_settings(
            fault_detector_canary_path=str(canary_file),
            fault_detector_canary_top_k=10,
        )
        heart = AsyncMock()
        # Return wrong ID — gold_id is NOT in results
        heart.search_facts = AsyncMock(return_value=[self._make_fact_result(other_id)])

        check = RetrievalCanaryCheck(heart=heart, settings=settings)
        result = await check.run()

        assert result.findings
        assert any("recall" in f.summary for f in result.findings)
        assert result.has_updates

    @pytest.mark.asyncio
    async def test_noop_when_canary_file_missing(self, tmp_path):
        """Missing canary file → no-op, no crash."""
        settings = _mock_settings(
            fault_detector_canary_path=str(tmp_path / "nonexistent.jsonl")
        )
        heart = AsyncMock()
        check = RetrievalCanaryCheck(heart=heart, settings=settings)
        result = await check.run()
        assert not result.findings
        heart.search_facts.assert_not_called()

    @pytest.mark.asyncio
    async def test_multiple_gold_ids_partial_hit(self, tmp_path):
        """Recall = 0.5 (1 of 2 gold IDs found) with min_recall = 0.6 → finding."""
        gold_a = "aaaa0000-0000-0000-0000-000000000000"
        gold_b = "bbbb0000-0000-0000-0000-000000000000"
        canary = [
            {
                "query": "memory architecture",
                "gold_ids": [gold_a, gold_b],
                "min_recall_at_k": 0.6,
            }
        ]
        canary_file = tmp_path / "canary.jsonl"
        canary_file.write_text(json.dumps(canary[0]) + "\n")

        settings = _mock_settings(
            fault_detector_canary_path=str(canary_file),
            fault_detector_canary_top_k=10,
        )
        heart = AsyncMock()
        # Only gold_a is returned (50% recall, below 60% threshold)
        heart.search_facts = AsyncMock(return_value=[self._make_fact_result(gold_a)])

        check = RetrievalCanaryCheck(heart=heart, settings=settings)
        result = await check.run()

        assert result.findings, "Expected finding for partial recall below threshold"

    @pytest.mark.asyncio
    async def test_canary_search_passes_track_access_false(self, tmp_path):
        """Canary search must not update recall state (track_access=False).

        Mutation evidence: remove the track_access=False kwarg and the assertion
        fails because heart.search_facts would be called without it.
        """
        gold_id = "44444444-4444-4444-4444-444444444444"
        canary = [{"query": "deployment model", "gold_ids": [gold_id], "min_recall_at_k": 0.5}]
        canary_file = tmp_path / "canary.jsonl"
        canary_file.write_text(json.dumps(canary[0]) + "\n")

        settings = _mock_settings(
            fault_detector_canary_path=str(canary_file),
            fault_detector_canary_top_k=10,
        )
        heart = AsyncMock()
        heart.search_facts = AsyncMock(return_value=[self._make_fact_result(gold_id)])

        check = RetrievalCanaryCheck(heart=heart, settings=settings)
        await check.run()

        # Verify track_access=False was passed to prevent production side effects
        heart.search_facts.assert_called_once()
        call_kwargs = heart.search_facts.call_args
        assert call_kwargs.kwargs.get("track_access") is False, (
            "Canary search must pass track_access=False to avoid inflating recall counts"
        )

    @pytest.mark.asyncio
    async def test_canary_search_failure_emits_finding(self, tmp_path):
        """A DB exception during canary search must produce a finding (not silence).

        Mutation evidence: if the except branch only logs and continues, no
        finding is emitted and a complete retrieval outage looks healthy.
        """
        gold_id = "55555555-5555-5555-5555-555555555555"
        canary = [{"query": "some query", "gold_ids": [gold_id], "min_recall_at_k": 0.5}]
        canary_file = tmp_path / "canary.jsonl"
        canary_file.write_text(json.dumps(canary[0]) + "\n")

        settings = _mock_settings(
            fault_detector_canary_path=str(canary_file),
            fault_detector_canary_top_k=10,
        )
        heart = AsyncMock()
        heart.search_facts = AsyncMock(side_effect=Exception("DB connection lost"))

        check = RetrievalCanaryCheck(heart=heart, settings=settings)
        result = await check.run()

        assert result.findings, "Search failure must produce a finding"
        assert result.has_updates
        # Should be high urgency since retrieval may be unavailable
        assert any(f.urgency == "high" for f in result.findings)


# ---------------------------------------------------------------------------
# Regression tests for Codex findings
# ---------------------------------------------------------------------------


class TestCountStaleEligibleExcludesCategories:
    """_count_stale_eligible must mirror _phase_stale_scan's category exclusion.

    Mutation evidence: remove the category exclusion from _count_stale_eligible
    and the population count will include excluded-category facts, causing a
    false-positive collapse finding even when the stale_scan correctly skips
    those facts.
    """

    @pytest.mark.asyncio
    async def test_excluded_categories_are_passed_to_query(self):
        """The SQL sent to the DB must contain the exclusion predicate."""
        executed_sqls = []

        mock_result = MagicMock()
        mock_result.scalar_one.return_value = 0

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.execute = AsyncMock(
            side_effect=lambda sql, params: (
                executed_sqls.append((str(sql), params))
                or mock_result
            )
        )

        db = MagicMock()
        db.session = MagicMock(return_value=mock_session)

        settings = _mock_settings(stale_scan_excluded_categories=["rule", "preference"])
        check = ProcessFaultCheck(db=db, settings=settings, agent_id="test-agent")
        await check._count_stale_eligible(60)

        assert executed_sqls, "Expected a DB query to be executed"
        sql_text, params = executed_sqls[0]
        # Exclusion clause must appear in the query
        assert "NOT IN" in sql_text or "category" in sql_text.lower(), (
            "Expected category exclusion predicate in query"
        )
        # Both excluded categories must be in the parameters
        assert "rule" in params.values()
        assert "preference" in params.values()


class TestRatioCollapseBaselineFetch:
    """get_recent_runs must fetch baseline_window + 5 rows.

    Mutation evidence: if limit=max(baseline_window, ...) is used instead of
    max(baseline_window + 5, ...), a minimum baseline_window=5 setting always
    leaves an empty baseline and collapse detection is permanently disabled.
    """

    @pytest.mark.asyncio
    async def test_fetch_limit_includes_recent_window(self):
        """ProcessFaultCheck must fetch at least baseline_window+5 rows."""
        fetch_limits: list[int] = []
        now = datetime.now(UTC)

        # 25 old runs with a high ratio + 5 recent with collapsed ratio
        old_runs = [
            _run(now, h, status="finished", items_examined=100, items_changed=50)
            for h in range(6, 31)
        ]
        recent_runs = [
            _run(now, h, status="finished", items_examined=100, items_changed=2)
            for h in range(1, 6)
        ]
        all_runs = recent_runs + old_runs  # newest first

        async def _fake_get(process_name, limit=20):
            fetch_limits.append(limit)
            return all_runs[:limit]

        settings = _mock_settings(
            fault_detector_ratio_baseline_window=20,
            fault_detector_ratio_collapse_threshold=0.30,
        )
        db = MagicMock()
        check = ProcessFaultCheck(db=db, settings=settings, agent_id="test-agent")

        async def _fake_count(*a, **kw):
            return 0

        recorder_mock = AsyncMock()
        recorder_mock.get_recent_runs = _fake_get
        with patch("nous.heartbeat.fault_detector.ProcessRecorder", return_value=recorder_mock):
            with patch.object(check, "_count_stale_eligible", _fake_count):
                result = await check.run()

        # All phase fetch calls should request at least baseline_window + 5 = 25 rows
        assert all(limit >= 25 for limit in fetch_limits), (
            f"Expected fetch limit >= 25, got: {fetch_limits}"
        )
        # With enough data, ratio collapse should be detected
        ratio_findings = [f for f in result.findings if "ratio" in f.summary]
        assert ratio_findings, "Expected a ratio-collapse finding with sufficient baseline data"

    @pytest.mark.asyncio
    async def test_minimum_baseline_window_still_detects_collapse(self):
        """With baseline_window=5, collapse detection still works after the fix.

        Before the fix, baseline_window=5 fetched only 5 rows, leaving
        ratio_runs[5:] empty and permanently disabling detection.
        """
        now = datetime.now(UTC)
        # 10 old runs with high ratio + 5 recent with collapsed ratio = 15 total
        old_runs = [
            _run(now, h, status="finished", items_examined=100, items_changed=50)
            for h in range(6, 16)  # 10 old runs
        ]
        recent_runs = [
            _run(now, h, status="finished", items_examined=100, items_changed=2)
            for h in range(1, 6)
        ]
        all_runs = recent_runs + old_runs

        async def _fake_get(process_name, limit=20):
            return all_runs[:limit]

        settings = _mock_settings(
            fault_detector_ratio_baseline_window=5,
            fault_detector_ratio_collapse_threshold=0.30,
        )
        db = MagicMock()
        check = ProcessFaultCheck(db=db, settings=settings, agent_id="test-agent")

        async def _fake_count(*a, **kw):
            return 0

        recorder_mock = AsyncMock()
        recorder_mock.get_recent_runs = _fake_get
        with patch("nous.heartbeat.fault_detector.ProcessRecorder", return_value=recorder_mock):
            with patch.object(check, "_count_stale_eligible", _fake_count):
                result = await check.run()

        ratio_findings = [f for f in result.findings if "ratio" in f.summary]
        assert ratio_findings, (
            "Expected ratio-collapse finding with minimum baseline_window=5; "
            "empty baseline means detection is permanently disabled"
        )
