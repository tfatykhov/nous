"""F116 PR 2: the runner carries the pending filters' reports into the tick event, its stats and /heartbeat/status."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from nous.config import Settings
from nous.heartbeat.finding_store import FindingStore
from nous.heartbeat.registry import BaseCheck, CheckRegistry
from nous.heartbeat.runner import HeartbeatRunner
from nous.heartbeat.schemas import (
    CheckResult,
    Finding,
    FindingAction,
    FindingState,
    SuppressionReport,
    TrackedFinding,
)

TODAYS_KEYS = ["findings_count", "checks_run", "tokens_used_today", "findings", "by_source", "by_urgency"]
REPORT_KEYS = ["suppressed", "suppressed_items", "shadow_suppressed", "shadow_suppressed_items", "suppression_errors"]


def _report(hidden: int = 0, shadow: int = 0, errors: int = 0, reason: str = "age_snapshot") -> SuppressionReport:
    report = SuppressionReport()
    for i in range(hidden):
        report.add(reason, shadow=False, fingerprint=f"{i:016x}", kind="fact", item_id=f"hidden-{i}")
    for i in range(shadow):
        report.add("kind_rule", shadow=True, fingerprint=f"{i:016x}", kind="fact", item_id=f"shadow-{i}")
    report.errors = errors
    return report


class _ReportingCheck(BaseCheck):
    name = "self_initiated"
    interval = 60

    def __init__(self, findings: list[Finding], report: SuppressionReport | None) -> None:
        super().__init__()
        self._findings = findings
        self._report = report

    async def run(self) -> CheckResult:
        return CheckResult(has_updates=bool(self._findings), findings=list(self._findings), suppression=self._report)


def _settings() -> MagicMock:
    s = MagicMock()
    s.agent_id = "test-agent"
    s.heartbeat_daily_token_budget = 50_000
    s.heartbeat_tuning_min_samples = 10
    return s


def _runner(*checks: BaseCheck, bus: AsyncMock | None = None) -> HeartbeatRunner:
    registry = CheckRegistry()
    for check in checks:
        registry.register(check)
    runner = HeartbeatRunner(
        settings=_settings(),
        registry=registry,
        runner=AsyncMock(),
        brain=AsyncMock(),
        heart=AsyncMock(),
        bus=bus,
        http_client=AsyncMock(),
    )
    runner._triage = AsyncMock()
    return runner


def _bus() -> AsyncMock:
    bus = AsyncMock()
    bus.emit = AsyncMock()
    return bus


def _tick_events(bus: AsyncMock) -> list:
    return [c.args[0] for c in bus.emit.call_args_list if c.args[0].type == "heartbeat_tick"]


def _finding() -> Finding:
    return Finding(source="facts", summary="Pending action: synthetic: send the invoice", needs_action=True)


class TestTickEvent:
    async def test_without_a_report_the_event_has_todays_keys_in_todays_order(self):
        bus = _bus()
        runner = _runner(_ReportingCheck([_finding()], None), bus=bus)

        await runner._tick()

        (event,) = _tick_events(bus)
        assert list(event.data) == TODAYS_KEYS
        assert runner._current_tick_event is event

    async def test_an_empty_report_adds_nothing(self):
        bus = _bus()
        runner = _runner(_ReportingCheck([_finding()], SuppressionReport()), bus=bus)

        await runner._tick()

        (event,) = _tick_events(bus)
        assert list(event.data) == TODAYS_KEYS
        assert "pending_filter" not in runner.get_stats()

    async def test_no_event_for_a_tick_without_findings_or_report(self):
        bus = _bus()
        runner = _runner(_ReportingCheck([], None), bus=bus)
        await runner._tick()
        assert _tick_events(bus) == []

    async def test_a_report_adds_the_five_keys(self):
        bus = _bus()
        runner = _runner(_ReportingCheck([_finding()], _report(hidden=2, shadow=1, errors=1)), bus=bus)

        await runner._tick()

        (event,) = _tick_events(bus)
        assert list(event.data) == TODAYS_KEYS + REPORT_KEYS
        assert event.data["findings_count"] == 1
        assert event.data["suppressed"] == {"age_snapshot": 2}
        assert event.data["shadow_suppressed"] == {"kind_rule": 1}
        assert event.data["suppression_errors"] == 1
        assert event.data["suppressed_items"][0] == {
            "fingerprint": f"{0:016x}",
            "kind": "fact",
            "id": "hidden-0",
            "reason": "age_snapshot",
        }
        runner._triage.assert_awaited_once()

    async def test_a_tick_whose_findings_were_all_hidden_still_writes_its_event(self):
        bus = _bus()
        runner = _runner(_ReportingCheck([], _report(hidden=3)), bus=bus)

        findings = await runner._tick()

        assert findings == []
        (event,) = _tick_events(bus)
        assert event.data["findings_count"] == 0
        assert event.data["findings"] == []
        assert event.data["suppressed"] == {"age_snapshot": 3}
        runner._triage.assert_not_awaited()
        assert getattr(runner, "_current_tick_event", None) is None

    async def test_errors_alone_write_the_event(self):
        bus = _bus()
        runner = _runner(_ReportingCheck([], _report(errors=1)), bus=bus)
        await runner._tick()
        (event,) = _tick_events(bus)
        assert event.data["suppression_errors"] == 1 and event.data["suppressed"] == {}

    async def test_thirty_items_leave_twenty_in_the_event_and_the_counts_complete(self):
        bus = _bus()
        runner = _runner(_ReportingCheck([], _report(hidden=30)), bus=bus)

        await runner._tick()

        (event,) = _tick_events(bus)
        assert event.data["suppressed"] == {"age_snapshot": 30}
        assert len(event.data["suppressed_items"]) == 20

    async def test_no_bus_no_event_and_no_error(self):
        runner = _runner(_ReportingCheck([], _report(hidden=1)), bus=None)
        assert await runner._tick() == []
        assert runner.get_stats()["pending_filter"]["suppressed"] == {"age_snapshot": 1}

    async def test_a_result_without_a_report_type_is_ignored(self):
        class _OddCheck(_ReportingCheck):
            async def run(self) -> CheckResult:
                result = await super().run()
                result.suppression = MagicMock()
                return result

        bus = _bus()
        runner = _runner(_OddCheck([_finding()], None), bus=bus)

        await runner._tick()

        (event,) = _tick_events(bus)
        assert list(event.data) == TODAYS_KEYS
        assert "pending_filter" not in runner.get_stats()

    async def test_a_report_that_cannot_be_merged_does_not_fail_the_run(self, monkeypatch):
        def broken(self, other):
            raise RuntimeError("merge failed")

        monkeypatch.setattr(SuppressionReport, "merge", broken)
        check = _ReportingCheck([_finding()], _report(hidden=1))
        bus = _bus()
        runner = _runner(check, bus=bus)

        findings = await runner._tick()

        assert [f.summary for f in findings] == [_finding().summary]
        assert check.consecutive_failures == 0
        (event,) = _tick_events(bus)
        assert list(event.data) == TODAYS_KEYS


class TestStats:
    async def test_no_key_before_a_report_and_cumulative_counts_after_two_ticks(self):
        check = _ReportingCheck([], None)
        runner = _runner(check)

        await runner._tick()
        assert "pending_filter" not in runner.get_stats()
        assert runner.pending_filter_stats is None

        check.last_run = None  # due again
        check._report = _report(hidden=2, shadow=1, errors=1)
        await runner._tick()
        check.last_run = None
        check._report = _report(hidden=1, shadow=3, reason="age_fact")
        await runner._tick()

        assert runner.get_stats()["pending_filter"] == {
            "suppressed": {"age_snapshot": 2, "age_fact": 1},
            "shadow_suppressed": {"kind_rule": 4},
            "errors": 1,
        }

    async def test_a_report_from_a_triggered_check_is_returned_not_counted(self):
        report = _report(hidden=1)
        runner = _runner(_ReportingCheck([], report))

        result = await runner.trigger_check("self_initiated")

        assert result.suppression is report
        assert "pending_filter" not in runner.get_stats()


async def _status(runner: HeartbeatRunner) -> dict:
    from nous.api.rest import create_app

    settings = Settings.model_construct(heartbeat_daily_token_budget=10_000)
    app = create_app(
        runner=MagicMock(),
        brain=MagicMock(),
        heart=MagicMock(),
        cognitive=MagicMock(),
        database=MagicMock(),
        settings=settings,
        heartbeat_runner=runner,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/heartbeat/status")
    assert resp.status_code == 200, resp.text
    return resp.json()


class TestStatusRoute:
    async def test_no_key_before_a_report(self):
        assert "pending_filter" not in await _status(_runner(_ReportingCheck([], None)))

    async def test_the_counts_after_a_report(self):
        runner = _runner(_ReportingCheck([], _report(hidden=2, shadow=1)))
        await runner._tick()

        body = await _status(runner)

        assert body["pending_filter"] == {
            "suppressed": {"age_snapshot": 2},
            "shadow_suppressed": {"kind_rule": 1},
            "errors": 0,
        }

    @pytest.mark.parametrize("value", [MagicMock(), None, {}, "x"])
    async def test_a_value_that_is_not_a_dict_adds_no_key(self, value, monkeypatch):
        runner = _runner(_ReportingCheck([], None))
        monkeypatch.setattr(HeartbeatRunner, "pending_filter_stats", property(lambda self: value))
        assert "pending_filter" not in await _status(runner)


# ---------------------------------------------------------------------------
# FindingStore: a finding that asks for its stored payload to be replaced
# ---------------------------------------------------------------------------


def _store() -> FindingStore:
    store = FindingStore()
    store._startup_time = datetime.now(UTC) - timedelta(hours=1)  # past the start-up quiet window
    return store


def _line(count: int, *, replaces: bool) -> Finding:
    return Finding(
        source="facts",
        summary=f"Pending items hidden by age cap: {count}",
        urgency="low",
        needs_action=False,
        raw_data={"detection": "age_digest", "count": count},
        check_name="self_initiated",
        replaces_stored=replaces,
    )


def _without_payload(tracked: TrackedFinding) -> dict:
    """Every tracked field but the payload and the two clock readings."""
    return {
        f.name: getattr(tracked, f.name)
        for f in dataclasses.fields(tracked)
        if f.name not in {"finding", "first_seen", "last_seen"}
    }


class TestFindingStoreReplacesStored:
    def test_the_flag_is_off_by_default_and_not_part_of_the_fingerprint(self):
        assert Finding(source="s", summary="x").replaces_stored is False
        assert _line(3, replaces=True).fingerprint() == _line(3, replaces=False).fingerprint()
        assert _line(3, replaces=True).fingerprint() == _line(4, replaces=True).fingerprint()

    def test_without_the_flag_the_first_payload_stays(self):
        store = _store()
        first, second = _line(3, replaces=False), _line(4, replaces=False)

        actions = [store.ingest(first), store.ingest(second)]

        tracked = store.get_tracked(first.fingerprint())
        assert actions == [FindingAction.TRIAGE, FindingAction.SUPPRESS]
        assert tracked.finding is first
        assert tracked.seen_count == 2

    @pytest.mark.parametrize("state", [None, FindingState.ACKNOWLEDGED, FindingState.RESOLVED])
    def test_with_the_flag_only_the_payload_changes(self, state):
        plain, flagged = _store(), _store()
        for store, replaces in ((plain, False), (flagged, True)):
            store.ingest(_line(3, replaces=replaces))
            fp = _line(3, replaces=replaces).fingerprint()
            if state == FindingState.ACKNOWLEDGED:
                store.acknowledge(fp)
            elif state == FindingState.RESOLVED:
                store.resolve(fp)

        newer_plain, newer_flagged = _line(4, replaces=False), _line(4, replaces=True)
        assert plain.ingest(newer_plain) == flagged.ingest(newer_flagged)

        fp = newer_plain.fingerprint()
        assert plain.get_tracked(fp).finding.summary == "Pending items hidden by age cap: 3"
        assert flagged.get_tracked(fp).finding is newer_flagged
        assert _without_payload(flagged.get_tracked(fp)) == _without_payload(plain.get_tracked(fp))

    def test_a_flagged_finding_escalates_once_like_any_other(self):
        store = _store()
        store.ingest(_line(3, replaces=True))
        fp = _line(3, replaces=True).fingerprint()
        store.acknowledge(fp)
        store.get_tracked(fp).first_seen = datetime.now(UTC) - timedelta(days=5)

        escalating = _line(3, replaces=True)
        assert store.ingest(escalating) == FindingAction.ESCALATE
        escalating.urgency = "normal"  # what the runner does to an escalated finding
        later = [store.ingest(_line(n, replaces=True)) for n in (4, 5, 6)]

        assert later == [FindingAction.SUPPRESS] * 3
        assert store.get_tracked(fp).finding.summary == "Pending items hidden by age cap: 6"
