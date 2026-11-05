"""A check with ``chat_delivery = False`` never reaches Telegram, at any urgency (owner, P4: dashboard only).

The memory write-health check is driven through the real runner on every route by which a finding can reach chat:
born high at ingest, a normal finding the time ladder escalates to high, the 09:00 digest and the accumulation
alert. Each route has a control: the same finding from a chat-enabled check is sent, so the pin is not vacuous.
The finding store's five-minute startup suppression is moved out of the way, or every ingest would be SUPPRESS.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from nous.heartbeat.finding_store import FindingStore
from nous.heartbeat.memory_write_health_check import MemoryWriteHealthCheck
from nous.heartbeat.registry import BaseCheck, CheckRegistry
from nous.heartbeat.runner import HeartbeatRunner
from nous.heartbeat.schemas import CheckResult, Finding, FindingState
from nous.observability import memory_write_health as mwh

H1_SUMMARY = "memory rows vanished since the baseline: 1 fact ids, 0 episode ids"


def _store() -> FindingStore:
    store = FindingStore()
    store._startup_time -= timedelta(seconds=600)
    return store


def _runner(registry: CheckRegistry, store: FindingStore) -> HeartbeatRunner:
    settings = MagicMock()
    settings.heartbeat_daily_token_budget = 50_000
    runner = HeartbeatRunner(
        settings=settings,
        registry=registry,
        runner=AsyncMock(),
        brain=AsyncMock(),
        heart=MagicMock(),
        bus=None,
        http_client=AsyncMock(),
        finding_store=store,
    )
    runner._send_telegram = AsyncMock()
    runner._cognitive_triage = AsyncMock()
    runner._tokens_used_today = 0
    return runner


def _mwh_check(monkeypatch: pytest.MonkeyPatch, rule: str = "H1", summary: str = H1_SUMMARY) -> MemoryWriteHealthCheck:
    async def fake_run_once(database, agent_id, *, now=None):
        return [mwh.RuleState(rule, "tripped", value=1, threshold=1, summary=summary)]

    monkeypatch.setattr(mwh, "run_once", fake_run_once)
    return MemoryWriteHealthCheck(database=object(), agent_id="test-mwh-chat")


class _ChatCheck(BaseCheck):
    """The control: same finding, an ordinary (chat-enabled) check."""

    name = "chat_control"

    def __init__(self, urgency: str = "high", summary: str = H1_SUMMARY) -> None:
        super().__init__()
        self._urgency = urgency
        self._summary = summary

    async def run(self) -> CheckResult:
        return CheckResult(
            has_updates=True,
            findings=[Finding(source="memory_write_health", summary=self._summary, urgency=self._urgency)],  # type: ignore[arg-type]
        )


def _sent_text(runner: HeartbeatRunner) -> str:
    return "\n".join(str(c.args[0]) for c in runner._send_telegram.await_args_list)


async def test_a_high_write_health_finding_is_not_sent_to_telegram(monkeypatch):
    registry = CheckRegistry()
    registry.register(_mwh_check(monkeypatch))
    store = _store()
    runner = _runner(registry, store)

    findings = await runner._tick()

    assert [f.urgency for f in findings] == ["high"]
    assert findings[0].chat_delivery is False and findings[0].needs_action is False
    runner._send_telegram.assert_not_awaited()
    runner._cognitive_triage.assert_not_awaited()
    # It is still tracked for the dashboard (GET /heartbeat/findings).
    tracked = store.get_tracked(findings[0].fingerprint())
    assert tracked is not None and tracked.state == FindingState.ACKNOWLEDGED


async def test_the_control_high_finding_is_sent():
    registry = CheckRegistry()
    registry.register(_ChatCheck())
    runner = _runner(registry, _store())

    await runner._tick()

    assert "Urgent findings" in _sent_text(runner)


async def test_quiet_hours_tick_runs_the_check_and_still_sends_nothing(monkeypatch):
    registry = CheckRegistry()
    check = _mwh_check(monkeypatch)
    registry.register(check)
    runner = _runner(registry, _store())

    assert check.urgent_override is True
    findings = await runner._tick(urgent_only=True)

    assert len(findings) == 1
    runner._send_telegram.assert_not_awaited()


async def test_a_normal_finding_escalated_to_high_is_not_sent(monkeypatch):
    summary = "profile learning (person, preference) dropped: 2 facts in the last 14 days against a mean of 20 before"
    registry = CheckRegistry()
    registry.register(_mwh_check(monkeypatch, rule="P1", summary=summary))
    store = _store()
    runner = _runner(registry, store)
    (first,) = await runner._tick()
    assert first.urgency == "normal"
    store.get_tracked(first.fingerprint()).first_seen = datetime.now(UTC) - timedelta(hours=25)
    registry.get_check("memory_write_health").last_run = None  # due again

    (again,) = await runner._tick()

    assert again.urgency == "high"  # the ladder bumped it
    runner._send_telegram.assert_not_awaited()


async def test_the_control_escalated_finding_is_sent():
    registry = CheckRegistry()
    registry.register(_ChatCheck(urgency="normal", summary="profile learning dropped"))
    store = _store()
    runner = _runner(registry, store)
    (first,) = await runner._tick()
    store.get_tracked(first.fingerprint()).first_seen = datetime.now(UTC) - timedelta(hours=25)
    registry.get_check("chat_control").last_run = None

    await runner._tick()

    assert "Urgent findings" in _sent_text(runner)


async def test_the_daily_digest_leaves_write_health_findings_out(monkeypatch):
    registry = CheckRegistry()
    registry.register(_mwh_check(monkeypatch))
    registry.register(_ChatCheck(urgency="low", summary="disk almost full"))
    store = _store()
    runner = _runner(registry, store)
    await runner._tick()
    runner._send_telegram.reset_mock()

    await runner._daily_digest()

    sent = _sent_text(runner)
    assert "disk almost full" in sent  # the control is in the digest
    assert "vanished" not in sent and "memory_write_health" not in sent


async def test_a_digest_of_only_write_health_findings_sends_nothing(monkeypatch):
    registry = CheckRegistry()
    registry.register(_mwh_check(monkeypatch))
    runner = _runner(registry, _store())
    await runner._tick()

    await runner._daily_digest()

    runner._send_telegram.assert_not_awaited()


async def test_five_acknowledged_write_health_findings_raise_no_accumulation_alert():
    store = _store()
    runner = _runner(CheckRegistry(), store)
    findings = [
        Finding(
            source=mwh.SOURCE,
            summary=f"fact flood from source {label}",
            urgency="low",
            check_name="memory_write_health",
            chat_delivery=False,
        )
        for label in ("user_direct", "reflection", "sleep_reflection", "other", "episode_summarizer")
    ]
    for f in findings:
        store.ingest(f)
        store.acknowledge(f.fingerprint())
    assert store.check_accumulation_escalation("memory_write_health") is True  # the alert would fire
    store._accumulation_escalated.clear()

    await runner._triage([findings[0]])

    runner._send_telegram.assert_not_awaited()


async def test_the_control_accumulation_alert_is_sent():
    store = _store()
    runner = _runner(CheckRegistry(), store)
    findings = [
        Finding(source="disk", summary=f"disk {name} almost full", urgency="low", check_name="disk_check")
        for name in ("alpha", "beta", "gamma", "delta", "epsilon")
    ]
    for f in findings:
        store.ingest(f)
        store.acknowledge(f.fingerprint())

    await runner._triage([findings[0]])

    assert "Accumulation alert" in _sent_text(runner)
