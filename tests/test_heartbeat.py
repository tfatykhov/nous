"""Tests for F034 Heartbeat — schemas, registry, runner, checks.

54 test cases across 9 test classes:
- TestFinding (3): construction, defaults, urgency literal
- TestCheckResult (3): defaults, with findings, has_updates flag
- TestHeartbeatResult (2): defaults, with values
- TestBaseCheck (8): is_due never run, is_due recently run, is_due elapsed,
    is_due circuit breaker, is_due inactive, mark_success, mark_failure,
    mark_failure disables, reset_circuit_breaker
- TestCheckRegistry (8): register, unregister, permanent cant unregister,
    unregister nonexistent, get_due_checks timing, get_due_checks circuit breaker,
    get_check, get_status
- TestHeartbeatRunner (16): quiet hours simple range, quiet hours cross midnight,
    quiet hours outside, budget tracking, budget reset, has_budget,
    tick runs due checks, tick urgent only, tick check failure circuit breaker,
    tick timeout circuit breaker, triage high sends telegram, triage normal cognitive,
    triage low logs only, cognitive triage updates budget, start stop lifecycle,
    trigger_tick, trigger_check
- TestHealthCheck (3): finds unreviewed, finds noisy censors, handles missing methods
- TestSelfInitiatedCheck (4): looks_like_pending matches, looks_like_pending no match,
    finds pending facts, finds due schedules
- TestEmailCheck (7): no credentials returns empty, classifies urgency high,
    classifies urgency normal, classifies urgency low, deduplicates seen ids,
    prune seen ids, mock imap fetch
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nous.heartbeat.checks import EmailCheck, HealthCheck, SelfInitiatedCheck
from nous.heartbeat.registry import BaseCheck, CheckRegistry
from nous.heartbeat.schemas import CheckResult, Finding, HeartbeatResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_settings(**overrides) -> MagicMock:
    """MagicMock Settings to avoid pydantic validation."""
    s = MagicMock()
    s.agent_id = "test-agent"
    s.heartbeat_enabled = True
    s.heartbeat_tick_interval = 30
    s.heartbeat_quiet_start = 23
    s.heartbeat_quiet_end = 8
    s.heartbeat_daily_token_budget = 50_000
    s.heartbeat_email_enabled = False
    s.heartbeat_email_interval = 180
    s.heartbeat_email_imap_host = "imap.gmail.com"
    s.email_user = ""
    s.email_password = ""
    s.heartbeat_health_interval = 3600
    s.heartbeat_self_initiated_interval = 1800
    s.telegram_bot_token = "test-token"
    s.telegram_chat_id = "12345"
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


class DummyCheck(BaseCheck):
    """Concrete check for testing BaseCheck ABC."""

    name = "dummy"
    interval = 60

    async def run(self) -> CheckResult:
        return CheckResult(has_updates=True, findings=[
            Finding(source="test", summary="test finding"),
        ])


class FailingCheck(BaseCheck):
    """Check that always raises."""

    name = "failing"
    interval = 60

    async def run(self) -> CheckResult:
        raise RuntimeError("check exploded")


class SlowCheck(BaseCheck):
    """Check that takes too long."""

    name = "slow"
    interval = 60
    timeout = 1  # 1 second timeout

    async def run(self) -> CheckResult:
        await asyncio.sleep(10)
        return CheckResult()


class UrgentDummyCheck(BaseCheck):
    """Check with urgent_override for quiet-hours testing."""

    name = "urgent_dummy"
    interval = 60
    urgent_override = True

    async def run(self) -> CheckResult:
        return CheckResult(has_updates=True, findings=[
            Finding(source="test", summary="urgent finding", urgency="high"),
        ])


# ===========================================================================
# TestFinding — 3 tests
# ===========================================================================


class TestFinding:
    """Finding dataclass construction and defaults."""

    def test_finding_creation(self):
        """1. Finding with all fields."""
        f = Finding(
            source="email",
            summary="New email from Tim",
            urgency="high",
            needs_action=True,
            raw_data={"message_id": "abc123"},
            check_name="email",
        )
        assert f.source == "email"
        assert f.summary == "New email from Tim"
        assert f.urgency == "high"
        assert f.needs_action is True
        assert f.raw_data == {"message_id": "abc123"}
        assert f.check_name == "email"

    def test_finding_defaults(self):
        """2. Finding with minimal args uses defaults."""
        f = Finding(source="test", summary="test")
        assert f.urgency == "normal"
        assert f.needs_action is False
        assert f.raw_data == {}
        assert f.check_name == ""

    def test_finding_urgency_values(self):
        """3. All urgency levels accepted."""
        for level in ("high", "normal", "low"):
            f = Finding(source="test", summary="test", urgency=level)
            assert f.urgency == level


# ===========================================================================
# TestCheckResult — 3 tests
# ===========================================================================


class TestCheckResult:
    """CheckResult dataclass."""

    def test_defaults(self):
        """4. Default CheckResult has no updates."""
        r = CheckResult()
        assert r.has_updates is False
        assert r.findings == []

    def test_with_findings(self):
        """5. CheckResult with findings."""
        findings = [Finding(source="test", summary="found something")]
        r = CheckResult(has_updates=True, findings=findings)
        assert r.has_updates is True
        assert len(r.findings) == 1

    def test_empty_findings_list_independent(self):
        """6. Default factory creates independent lists."""
        r1 = CheckResult()
        r2 = CheckResult()
        r1.findings.append(Finding(source="x", summary="x"))
        assert len(r2.findings) == 0


# ===========================================================================
# TestHeartbeatResult — 2 tests
# ===========================================================================


class TestHeartbeatResult:
    """HeartbeatResult dataclass."""

    def test_defaults(self):
        """7. Default HeartbeatResult."""
        r = HeartbeatResult()
        assert r.response == ""
        assert r.tokens_used == 0

    def test_with_values(self):
        """8. HeartbeatResult with actual values."""
        r = HeartbeatResult(response="Reviewed 3 items.", tokens_used=1500)
        assert r.response == "Reviewed 3 items."
        assert r.tokens_used == 1500


# ===========================================================================
# TestBaseCheck — 8 tests
# ===========================================================================


class TestBaseCheck:
    """BaseCheck ABC behavior — is_due, mark_success, mark_failure, circuit breaker."""

    def test_is_due_never_run(self):
        """9. Never-run check is immediately due."""
        check = DummyCheck()
        assert check.is_due() is True

    def test_is_due_recently_run(self):
        """10. Recently-run check is not due."""
        check = DummyCheck()
        check.last_run = datetime.now(UTC)
        assert check.is_due() is False

    def test_is_due_elapsed(self):
        """11. Check with elapsed interval is due."""
        check = DummyCheck()
        check.last_run = datetime.now(UTC) - timedelta(seconds=120)
        assert check.is_due() is True

    def test_is_due_circuit_breaker(self):
        """12. Circuit breaker prevents check from being due."""
        check = DummyCheck()
        check.consecutive_failures = check.max_failures  # 3
        assert check.is_due() is False

    def test_is_due_inactive(self):
        """13. Inactive check is never due."""
        check = DummyCheck()
        check.active = False
        assert check.is_due() is False

    def test_mark_success(self):
        """14. mark_success resets failures and updates last_run."""
        check = DummyCheck()
        check.consecutive_failures = 2
        check.mark_success()
        assert check.consecutive_failures == 0
        assert check.last_run is not None
        assert (datetime.now(UTC) - check.last_run).total_seconds() < 2

    def test_mark_failure_increments(self):
        """15. mark_failure increments consecutive_failures."""
        check = DummyCheck()
        check.mark_failure()
        assert check.consecutive_failures == 1
        assert check.last_run is not None

    def test_mark_failure_disables(self):
        """16. mark_failure disables after max_failures consecutive failures."""
        check = DummyCheck()
        for _ in range(check.max_failures):
            check.mark_failure()
        assert check.consecutive_failures == check.max_failures
        assert check.is_due() is False

    def test_reset_circuit_breaker(self):
        """17. reset_circuit_breaker clears failure count."""
        check = DummyCheck()
        check.consecutive_failures = check.max_failures
        assert check.is_due() is False
        check.reset_circuit_breaker()
        assert check.consecutive_failures == 0
        # Now it should be due again (last_run was set by mark_failure,
        # but enough time would need to pass for interval — set last_run to None)
        check.last_run = None
        assert check.is_due() is True


# ===========================================================================
# TestCheckRegistry — 8 tests
# ===========================================================================


class TestCheckRegistry:
    """CheckRegistry — register, unregister, permanent, due, status."""

    def test_register(self):
        """18. Register a check."""
        reg = CheckRegistry()
        check = DummyCheck()
        reg.register(check)
        assert reg.get_check("dummy") is check

    def test_unregister(self):
        """19. Unregister removes a check."""
        reg = CheckRegistry()
        reg.register(DummyCheck())
        assert reg.unregister("dummy") is True
        assert reg.get_check("dummy") is None

    def test_permanent_cant_unregister(self):
        """20. Permanent checks cannot be unregistered."""
        reg = CheckRegistry()
        reg.register(DummyCheck(), permanent=True)
        assert reg.unregister("dummy") is False
        assert reg.get_check("dummy") is not None

    def test_unregister_nonexistent(self):
        """21. Unregistering nonexistent check returns False."""
        reg = CheckRegistry()
        assert reg.unregister("ghost") is False

    def test_get_due_checks_respects_interval(self):
        """22. get_due_checks returns only checks that are due."""
        reg = CheckRegistry()
        due_check = DummyCheck()
        due_check.last_run = None  # never run -> due

        not_due_check = DummyCheck()
        not_due_check.name = "not_due"
        not_due_check.last_run = datetime.now(UTC)  # just ran -> not due

        reg.register(due_check)
        reg.register(not_due_check)

        due = reg.get_due_checks()
        assert len(due) == 1
        assert due[0].name == "dummy"

    def test_get_due_checks_respects_circuit_breaker(self):
        """23. Circuit-broken checks excluded from due list."""
        reg = CheckRegistry()
        check = DummyCheck()
        check.consecutive_failures = check.max_failures
        reg.register(check)

        assert len(reg.get_due_checks()) == 0

    def test_get_check(self):
        """24. get_check returns None for unknown name."""
        reg = CheckRegistry()
        assert reg.get_check("unknown") is None

    def test_get_status(self):
        """25. get_status returns dict with correct fields."""
        reg = CheckRegistry()
        check = DummyCheck()
        reg.register(check, permanent=True)

        status = reg.get_status()
        assert "dummy" in status
        entry = status["dummy"]
        assert entry["active"] is True
        assert entry["interval"] == 60
        assert entry["last_run"] is None
        assert entry["consecutive_failures"] == 0
        assert entry["max_failures"] == 3
        assert entry["circuit_breaker_open"] is False
        assert entry["permanent"] is True
        assert entry["urgent_override"] is False


# ===========================================================================
# TestHeartbeatRunner — 16 tests
# ===========================================================================


class TestHeartbeatRunner:
    """HeartbeatRunner — quiet hours, budget, tick, triage, lifecycle."""

    def _make_runner(self, settings=None, registry=None, **kwargs):
        from nous.heartbeat.runner import HeartbeatRunner

        settings = settings or _mock_settings(**kwargs)
        registry = registry or CheckRegistry()
        runner = HeartbeatRunner(
            settings=settings,
            registry=registry,
            runner=AsyncMock(),
            brain=AsyncMock(),
            heart=MagicMock(),
            bus=None,
            http_client=AsyncMock(),
        )
        return runner

    def test_quiet_hours_simple_range(self):
        """26. _in_quiet_hours True during simple range (e.g. 9-17)."""
        runner = self._make_runner(heartbeat_quiet_start=9, heartbeat_quiet_end=17)
        # Mock datetime.now(UTC) to return hour=12
        with patch("nous.heartbeat.runner.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 4, 3, 12, 0, tzinfo=UTC)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            assert runner._in_quiet_hours() is True

    def test_quiet_hours_outside_simple_range(self):
        """27. _in_quiet_hours False outside simple range."""
        runner = self._make_runner(heartbeat_quiet_start=9, heartbeat_quiet_end=17)
        with patch("nous.heartbeat.runner.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 4, 3, 20, 0, tzinfo=UTC)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            assert runner._in_quiet_hours() is False

    def test_quiet_hours_cross_midnight(self):
        """28. _in_quiet_hours cross-midnight case (23-8)."""
        runner = self._make_runner(heartbeat_quiet_start=23, heartbeat_quiet_end=8)
        # Hour 2 is in the quiet window
        with patch("nous.heartbeat.runner.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 4, 3, 2, 0, tzinfo=UTC)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            assert runner._in_quiet_hours() is True

    def test_quiet_hours_cross_midnight_outside(self):
        """29. _in_quiet_hours cross-midnight, outside (hour 15)."""
        runner = self._make_runner(heartbeat_quiet_start=23, heartbeat_quiet_end=8)
        with patch("nous.heartbeat.runner.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 4, 3, 15, 0, tzinfo=UTC)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            assert runner._in_quiet_hours() is False

    def test_has_budget_true(self):
        """30. _has_budget returns True when under budget."""
        runner = self._make_runner(heartbeat_daily_token_budget=50000)
        runner._tokens_used_today = 10000
        assert runner._has_budget() is True

    def test_has_budget_false(self):
        """31. _has_budget returns False when over budget."""
        runner = self._make_runner(heartbeat_daily_token_budget=50000)
        runner._tokens_used_today = 60000
        assert runner._has_budget() is False

    def test_maybe_reset_budget(self):
        """32. _maybe_reset_budget resets on new day."""
        runner = self._make_runner()
        runner._tokens_used_today = 30000
        runner._budget_date = date.today() - timedelta(days=1)
        runner._maybe_reset_budget()
        assert runner._tokens_used_today == 0
        assert runner._budget_date == date.today()

    def test_maybe_reset_budget_same_day(self):
        """33. _maybe_reset_budget does not reset on same day."""
        runner = self._make_runner()
        runner._tokens_used_today = 30000
        runner._budget_date = date.today()
        runner._maybe_reset_budget()
        assert runner._tokens_used_today == 30000

    @pytest.mark.asyncio
    async def test_tick_runs_due_checks(self):
        """34. _tick runs due checks and collects findings."""
        reg = CheckRegistry()
        reg.register(DummyCheck())

        runner = self._make_runner(registry=reg)
        findings = await runner._tick()

        assert len(findings) == 1
        assert findings[0].source == "test"
        assert findings[0].check_name == "dummy"

    @pytest.mark.asyncio
    async def test_tick_urgent_only(self):
        """35. _tick(urgent_only=True) only runs urgent checks."""
        reg = CheckRegistry()
        reg.register(DummyCheck())  # not urgent
        reg.register(UrgentDummyCheck())  # urgent

        runner = self._make_runner(registry=reg)
        findings = await runner._tick(urgent_only=True)

        assert len(findings) == 1
        assert findings[0].source == "test"
        assert findings[0].urgency == "high"

    @pytest.mark.asyncio
    async def test_tick_check_failure_marks_failure(self):
        """36. _tick marks check as failed on exception."""
        reg = CheckRegistry()
        check = FailingCheck()
        reg.register(check)

        runner = self._make_runner(registry=reg)
        findings = await runner._tick()

        assert len(findings) == 0
        assert check.consecutive_failures == 1

    @pytest.mark.asyncio
    async def test_tick_timeout_marks_failure(self):
        """37. _tick marks check as failed on timeout."""
        reg = CheckRegistry()
        check = SlowCheck()
        reg.register(check)

        runner = self._make_runner(registry=reg)
        findings = await runner._tick()

        assert len(findings) == 0
        assert check.consecutive_failures == 1

    @pytest.mark.asyncio
    async def test_triage_high_sends_telegram(self):
        """38. _triage sends Telegram for high-urgency findings."""
        runner = self._make_runner()
        runner._send_telegram = AsyncMock()
        runner._cognitive_triage = AsyncMock(return_value=HeartbeatResult())
        runner._tokens_used_today = 0

        findings = [
            Finding(source="email", summary="Urgent email", urgency="high", needs_action=True),
        ]
        await runner._triage(findings)

        runner._send_telegram.assert_called_once()
        call_text = runner._send_telegram.call_args[0][0]
        assert "Urgent" in call_text or "urgent" in call_text.lower()

    @pytest.mark.asyncio
    async def test_triage_actionable_opens_cognitive_session(self):
        """39. _triage opens cognitive session for actionable findings with budget."""
        runner = self._make_runner(heartbeat_daily_token_budget=50000)
        runner._send_telegram = AsyncMock()
        runner._cognitive_triage = AsyncMock(return_value=HeartbeatResult())
        runner._tokens_used_today = 0

        findings = [
            Finding(source="health", summary="5 decisions pending", urgency="normal", needs_action=True),
        ]
        await runner._triage(findings)

        runner._cognitive_triage.assert_called_once()

    @pytest.mark.asyncio
    async def test_triage_low_no_notification(self):
        """40. _triage does not notify or triage for low-only findings."""
        runner = self._make_runner()
        runner._send_telegram = AsyncMock()
        runner._cognitive_triage = AsyncMock(return_value=HeartbeatResult())

        findings = [
            Finding(source="facts", summary="Stale facts", urgency="low", needs_action=False),
        ]
        await runner._triage(findings)

        runner._send_telegram.assert_not_called()
        runner._cognitive_triage.assert_not_called()

    @pytest.mark.asyncio
    async def test_cognitive_triage_updates_budget(self):
        """41. _cognitive_triage increments tokens_used_today."""
        runner = self._make_runner()
        runner._runner.run_turn = AsyncMock(return_value=(
            "Reviewed items.",
            MagicMock(),
            {"input_tokens": 500, "output_tokens": 300},
        ))
        runner._runner.end_conversation = AsyncMock()

        findings = [
            Finding(source="health", summary="Test", urgency="normal", needs_action=True),
        ]
        result = await runner._cognitive_triage(findings)

        assert result.tokens_used == 800
        assert runner._tokens_used_today == 800

    @pytest.mark.asyncio
    async def test_start_stop_lifecycle(self):
        """42. start/stop create and cancel the background task."""
        runner = self._make_runner()
        # Patch _detect_missed_checks to avoid DB access
        runner._detect_missed_checks = AsyncMock()

        await runner.start()
        assert runner._task is not None
        assert runner._running is True

        await runner.stop()
        assert runner._running is False
        assert runner._task is None

    @pytest.mark.asyncio
    async def test_trigger_tick(self):
        """43. trigger_tick forces an immediate tick."""
        reg = CheckRegistry()
        reg.register(DummyCheck())

        runner = self._make_runner(registry=reg)
        findings = await runner.trigger_tick()

        assert len(findings) == 1

    @pytest.mark.asyncio
    async def test_trigger_check(self):
        """44. trigger_check runs a specific check by name."""
        reg = CheckRegistry()
        reg.register(DummyCheck())

        runner = self._make_runner(registry=reg)
        result = await runner.trigger_check("dummy")

        assert result is not None
        assert result.has_updates is True
        assert len(result.findings) == 1

    @pytest.mark.asyncio
    async def test_trigger_check_unknown(self):
        """45. trigger_check returns None for unknown check."""
        runner = self._make_runner()
        result = await runner.trigger_check("nonexistent")
        assert result is None


# ===========================================================================
# TestHealthCheck — 3 tests
# ===========================================================================


class TestHealthCheck:
    """HealthCheck — system health indicators."""

    def _make_check(self):
        heart = MagicMock()
        brain = AsyncMock()
        settings = _mock_settings()
        check = HealthCheck(heart=heart, brain=brain, settings=settings)
        return check, heart, brain

    @pytest.mark.asyncio
    async def test_finds_unreviewed_decisions(self):
        """46. HealthCheck reports unreviewed decisions."""
        check, heart, brain = self._make_check()
        brain.get_unreviewed = AsyncMock(return_value=[MagicMock(), MagicMock()])
        heart.censors.list_active = AsyncMock(return_value=[])
        heart.facts.count_stale = AsyncMock(return_value=0)
        heart.procedures.get_low_effectiveness = AsyncMock(return_value=[])

        result = await check.run()

        assert result.has_updates is True
        found = [f for f in result.findings if f.source == "brain"]
        assert len(found) == 1
        assert "2 decisions" in found[0].summary

    @pytest.mark.asyncio
    async def test_finds_noisy_censors(self):
        """47. HealthCheck reports high false-positive censors."""
        check, heart, brain = self._make_check()
        brain.get_unreviewed = AsyncMock(return_value=[])

        noisy_censor = MagicMock()
        noisy_censor.false_positive_count = 10
        noisy_censor.id = "censor-1"
        heart.censors.list_active = AsyncMock(return_value=[noisy_censor])
        heart.facts.count_stale = AsyncMock(return_value=0)
        heart.procedures.get_low_effectiveness = AsyncMock(return_value=[])

        result = await check.run()

        assert result.has_updates is True
        found = [f for f in result.findings if f.source == "censors"]
        assert len(found) == 1

    @pytest.mark.asyncio
    async def test_handles_missing_methods(self):
        """48. HealthCheck handles exceptions gracefully (try/except)."""
        check, heart, brain = self._make_check()
        brain.get_unreviewed = AsyncMock(side_effect=AttributeError("no such method"))
        heart.censors.list_active = AsyncMock(side_effect=RuntimeError("boom"))
        heart.facts.count_stale = AsyncMock(side_effect=Exception("fail"))
        heart.procedures.get_low_effectiveness = AsyncMock(side_effect=Exception("fail"))

        result = await check.run()

        # Should not raise, just return empty
        assert result.has_updates is False
        assert result.findings == []


# ===========================================================================
# TestSelfInitiatedCheck — 4 tests
# ===========================================================================


class TestSelfInitiatedCheck:
    """SelfInitiatedCheck — pending actions, due schedules."""

    def _make_check(self):
        heart = MagicMock()
        brain = AsyncMock()
        settings = _mock_settings()
        check = SelfInitiatedCheck(heart=heart, brain=brain, settings=settings)
        return check, heart, brain

    def test_looks_like_pending_matches(self):
        """49. _looks_like_pending detects known markers."""
        assert SelfInitiatedCheck._looks_like_pending("TODO: check the report") is True
        assert SelfInitiatedCheck._looks_like_pending("need to follow-up with Tim") is True
        assert SelfInitiatedCheck._looks_like_pending("This is pending review") is True
        assert SelfInitiatedCheck._looks_like_pending("remind me about the meeting") is True
        assert SelfInitiatedCheck._looks_like_pending("Action needed on PR") is True

    def test_looks_like_pending_no_match(self):
        """50. _looks_like_pending rejects non-matching content."""
        assert SelfInitiatedCheck._looks_like_pending("The weather is nice today") is False
        assert SelfInitiatedCheck._looks_like_pending("Database schema updated") is False

    @pytest.mark.asyncio
    async def test_finds_pending_facts(self):
        """51. SelfInitiatedCheck finds pending follow-up facts."""
        check, heart, _brain = self._make_check()

        fact = MagicMock()
        fact.content = "TODO: follow-up on the deployment issue"
        fact.id = "fact-123"
        heart.facts.search = AsyncMock(return_value=[fact])
        heart.schedules.get_due = AsyncMock(return_value=[])

        result = await check.run()

        assert result.has_updates is True
        found = [f for f in result.findings if f.source == "facts"]
        assert len(found) == 1
        assert "Pending action" in found[0].summary

    @pytest.mark.asyncio
    async def test_finds_due_schedules(self):
        """52. SelfInitiatedCheck finds overdue schedules."""
        check, heart, _brain = self._make_check()

        heart.facts.search = AsyncMock(return_value=[])
        sched = MagicMock()
        sched.id = "sched-1"
        heart.schedules.get_due = AsyncMock(return_value=[sched])

        result = await check.run()

        assert result.has_updates is True
        found = [f for f in result.findings if f.source == "schedules"]
        assert len(found) == 1
        assert "past due" in found[0].summary


# ===========================================================================
# TestEmailCheck — 7 tests
# ===========================================================================


class TestEmailCheck:
    """EmailCheck — IMAP polling, urgency classification, dedup, pruning."""

    def _make_check(self, **overrides):
        settings = _mock_settings(
            email_user="test@example.com",
            email_password="secret",
            heartbeat_email_imap_host="imap.example.com",
            **overrides,
        )
        return EmailCheck(settings=settings)

    @pytest.mark.asyncio
    async def test_no_credentials_returns_empty(self):
        """53. EmailCheck returns empty if no credentials configured."""
        settings = _mock_settings(
            email_user="",
            email_password="",
        )
        check = EmailCheck(settings=settings)
        result = await check.run()
        assert result.has_updates is False

    def test_classify_urgency_high(self):
        """54. Urgent keywords in subject yield high urgency."""
        assert EmailCheck._keyword_classify("URGENT: server down", "alice@co.com") == "high"
        assert EmailCheck._keyword_classify("Critical failure", "bob@co.com") == "high"
        assert EmailCheck._keyword_classify("ASAP fix needed", "eve@co.com") == "high"

    def test_classify_urgency_normal(self):
        """55. Important/action keywords yield normal urgency."""
        assert EmailCheck._keyword_classify("Important update", "alice@co.com") == "normal"
        assert EmailCheck._keyword_classify("Action required: sign form", "bob@co.com") == "normal"
        assert EmailCheck._keyword_classify("Deadline approaching", "eve@co.com") == "normal"

    def test_classify_urgency_normal_default(self):
        """56. Generic subject yields normal urgency (newsletter now low)."""
        assert EmailCheck._keyword_classify("Meeting notes", "team@co.com") == "normal"

    @pytest.mark.asyncio
    async def test_deduplicates_seen_ids(self):
        """57. EmailCheck deduplicates messages by message ID."""
        check = self._make_check()
        messages = [("msg-1", "Hello", "alice@co.com")]

        with patch.object(check, "_fetch_unseen", return_value=messages):
            await asyncio.to_thread(check._fetch_unseen)
            # Simulate first run
            r1 = await check.run()
            assert r1.has_updates is True
            assert len(r1.findings) == 1

            # Second run with same message
            r2 = await check.run()
            assert r2.has_updates is False
            assert len(r2.findings) == 0

    def test_prune_seen_ids(self):
        """58. _prune_seen removes entries older than 24h."""
        check = self._make_check()
        now = datetime.now(UTC)
        check._seen_ids = {
            "old": now - timedelta(hours=25),
            "recent": now - timedelta(hours=1),
        }
        check._prune_seen()
        assert "old" not in check._seen_ids
        assert "recent" in check._seen_ids

    @pytest.mark.asyncio
    async def test_mock_imap_fetch(self):
        """59. EmailCheck processes IMAP results into findings."""
        check = self._make_check()
        fake_messages = [
            ("msg-100", "Urgent: deploy broken", "ops@co.com"),
            ("msg-101", "Weekly sync notes", "team@co.com"),
        ]

        async def _fake_to_thread(fn, *args, **kwargs):
            return fake_messages

        with patch("nous.heartbeat.checks.asyncio.to_thread", side_effect=_fake_to_thread):
            result = await check.run()

        assert result.has_updates is True
        assert len(result.findings) == 2

        # First should be high urgency (contains "urgent")
        urgent_findings = [f for f in result.findings if f.urgency == "high"]
        assert len(urgent_findings) == 1
        assert "deploy broken" in urgent_findings[0].summary

        # Second should be normal urgency (no low tier)
        normal_findings = [f for f in result.findings if f.urgency == "normal"]
        assert len(normal_findings) == 1


# -----------------------------------------------------------------------
# Dashboard-specific tests (event enrichment + public properties)
# -----------------------------------------------------------------------


class _CustomFindingsCheck(BaseCheck):
    """Check that returns custom findings for event enrichment tests."""

    name = "custom"
    interval = 60

    def __init__(self, findings_list):
        super().__init__()
        self._findings = findings_list

    async def run(self) -> CheckResult:
        return CheckResult(has_updates=bool(self._findings), findings=self._findings)


class _EmptyCheck(BaseCheck):
    """Check that returns no findings."""

    name = "empty"
    interval = 60

    async def run(self) -> CheckResult:
        return CheckResult()


class TestHeartbeatEventEnrichment:
    """Tests for enriched heartbeat_tick and heartbeat_triage events."""

    @pytest.mark.asyncio
    async def test_tick_event_includes_findings_array(self):
        """heartbeat_tick event data includes per-finding details."""
        from nous.heartbeat.runner import HeartbeatRunner

        settings = _mock_settings()
        registry = CheckRegistry()
        registry.register(_CustomFindingsCheck([
            Finding(source="brain", summary="3 decisions pending", urgency="normal", needs_action=True),
            Finding(source="facts", summary="10 stale facts", urgency="low", needs_action=False),
        ]))

        bus = AsyncMock()
        bus.emit = AsyncMock()

        runner = HeartbeatRunner(
            settings=settings, registry=registry, runner=AsyncMock(),
            brain=AsyncMock(), heart=AsyncMock(), bus=bus, http_client=AsyncMock(),
        )

        await runner._tick()

        tick_calls = [c for c in bus.emit.call_args_list if c.args[0].type == "heartbeat_tick"]
        assert len(tick_calls) == 1

        event_data = tick_calls[0].args[0].data
        assert "findings" in event_data
        assert len(event_data["findings"]) == 2
        assert event_data["findings"][0]["source"] == "brain"
        assert event_data["findings"][1]["urgency"] == "low"
        assert event_data["by_source"] == {"brain": 1, "facts": 1}
        assert event_data["by_urgency"] == {"normal": 1, "low": 1}

    @pytest.mark.asyncio
    async def test_tick_event_not_emitted_when_no_findings(self):
        """No heartbeat_tick event when checks produce no findings."""
        from nous.heartbeat.runner import HeartbeatRunner

        settings = _mock_settings()
        registry = CheckRegistry()
        registry.register(_EmptyCheck())

        bus = AsyncMock()
        bus.emit = AsyncMock()

        runner = HeartbeatRunner(
            settings=settings, registry=registry, runner=AsyncMock(),
            brain=AsyncMock(), heart=AsyncMock(), bus=bus, http_client=AsyncMock(),
        )

        await runner._tick()
        tick_calls = [c for c in bus.emit.call_args_list if c.args[0].type == "heartbeat_tick"]
        assert len(tick_calls) == 0

    @pytest.mark.asyncio
    async def test_triage_event_emitted_after_cognitive_session(self):
        """heartbeat_triage event emitted with session details."""
        from nous.heartbeat.runner import HeartbeatRunner

        settings = _mock_settings()
        registry = CheckRegistry()

        bus = AsyncMock()
        bus.emit = AsyncMock()

        runner_mock = AsyncMock()
        runner_mock.run_turn = AsyncMock(
            return_value=("Response text", None, {"input_tokens": 100, "output_tokens": 50})
        )
        runner_mock.end_conversation = AsyncMock()

        runner = HeartbeatRunner(
            settings=settings, registry=registry, runner=runner_mock,
            brain=AsyncMock(), heart=AsyncMock(), bus=bus, http_client=AsyncMock(),
        )

        findings = [
            Finding(source="health", summary="test finding", urgency="normal", needs_action=True),
        ]
        await runner._cognitive_triage(findings)

        triage_calls = [c for c in bus.emit.call_args_list if c.args[0].type == "heartbeat_triage"]
        assert len(triage_calls) == 1

        event_data = triage_calls[0].args[0].data
        assert "session_id" in event_data
        assert event_data["session_id"].startswith("heartbeat-")
        assert event_data["findings_count"] == 1
        assert event_data["tokens_used"] == 150
        assert "Response text" in event_data["response_summary"]


class TestHeartbeatPublicProperties:
    """Tests for HeartbeatRunner public properties used by dashboard."""

    def test_is_running_default_false(self):
        from nous.heartbeat.runner import HeartbeatRunner

        settings = _mock_settings()
        runner = HeartbeatRunner(
            settings=settings, registry=CheckRegistry(), runner=AsyncMock(),
            brain=AsyncMock(), heart=AsyncMock(), bus=None, http_client=None,
        )
        assert runner.is_running is False

    @pytest.mark.asyncio
    async def test_is_running_true_after_start(self):
        from nous.heartbeat.runner import HeartbeatRunner

        settings = _mock_settings()
        runner = HeartbeatRunner(
            settings=settings, registry=CheckRegistry(), runner=AsyncMock(),
            brain=AsyncMock(), heart=AsyncMock(), bus=None, http_client=None,
        )
        await runner.start()
        assert runner.is_running is True
        await runner.stop()

    def test_tokens_used_today(self):
        from nous.heartbeat.runner import HeartbeatRunner

        settings = _mock_settings()
        runner = HeartbeatRunner(
            settings=settings, registry=CheckRegistry(), runner=AsyncMock(),
            brain=AsyncMock(), heart=AsyncMock(), bus=None, http_client=None,
        )
        assert runner.tokens_used_today == 0
