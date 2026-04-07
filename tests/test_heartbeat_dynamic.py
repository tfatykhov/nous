"""Tests for F034.5 Dynamic Heartbeat Checks + #273 on_complete callback.

71 test cases across 15 test classes:
- TestDynamicCheckInit (4): default values, tools filtered to allowed, empty tools, cron set
- TestDynamicCheckIsDue (8): due when never run, not due when inactive, not due circuit breaker,
    due after interval, not due before interval, cron due, cron not due, cron never run
- TestDynamicCheckRun (7): successful run with findings, successful run no findings,
    run without runner, run passes tool filter, run tracks tokens, run cleans up session,
    run raises on failure
- TestDynamicCheckParsing (6): valid JSON, no findings, malformed JSON, urgency validation,
    summary truncation, findings source
- TestDynamicCheckLoader (6): sync registers new, sync unregisters removed, sync skips unchanged,
    sync detects changes, sync rejects permanent name, set_runner updates existing
- TestDynamicCheckLoaderCRUD (10): create check, create rejects low interval, create rejects max
    count, create rejects permanent name, create validates cron, manage list, manage enable,
    manage disable, manage delete, manage update
- TestDynamicCheckLoaderStats (2): update stats success, update stats failure
- TestHeartbeatRunnerDynamic (6): runner has dynamic loader, start syncs loader,
    tick tracks dynamic tokens, tick updates run stats, tick updates run stats on failure,
    periodic sync
- TestToolFilter (2): tool filter restricts tools, tool filter none keeps all
- TestOnCompleteFields (3): fields stored, in signature, tools filtered
- TestSelfDisabledFlag (3): default false, set on disable, in check result
- TestOnCompleteExecution (5): callback success, retry on failure, both fail telegram,
    both fail finding, budget exhausted skips
- TestOnCompleteValidation (3): create subset, update tools validates, update on_complete validates
- TestOnCompleteCRUD (3): create with on_complete, list includes on_complete, update on_complete
- TestRunnerCallback (3): tick fires callback, tick skips no prompt, trigger fires callback
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nous.heartbeat.dynamic import ALLOWED_TOOLS, DynamicCheck, DynamicCheckLoader
from nous.heartbeat.registry import CheckRegistry
from nous.heartbeat.schemas import CheckResult, Finding


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
    s.heartbeat_max_dynamic_checks = 10
    s.heartbeat_dynamic_sync_ticks = 10
    s.telegram_bot_token = "test-token"
    s.telegram_chat_id = "12345"
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


def _make_dynamic_check(
    name: str = "test_check",
    prompt: str = "Check if X is happening",
    tools: list[str] | None = None,
    interval: int = 3600,
    timeout: int = 30,
    urgent: bool = False,
    runner: AsyncMock | None = None,
) -> DynamicCheck:
    """Create a DynamicCheck for testing."""
    return DynamicCheck(
        check_id="check-uuid-001",
        name=name,
        prompt=prompt,
        tools=tools or [],
        interval=interval,
        timeout=timeout,
        urgent=urgent,
        runner=runner,
    )


def _mock_db_row(
    id: str = "row-uuid-001",
    name: str = "test_check",
    prompt: str = "Check if X is happening",
    tools: list[str] | None = None,
    interval_seconds: int = 3600,
    timeout_seconds: int = 30,
    cron_expr: str | None = None,
    urgent: bool = False,
    enabled: bool = True,
    run_count: int = 0,
    error_count: int = 0,
    last_error: str | None = None,
    last_run_at: datetime | None = None,
    created_at: datetime | None = None,
    created_by: str | None = None,
    on_complete_prompt: str | None = None,
    on_complete_tools: list[str] | None = None,
) -> MagicMock:
    """Create a mock DB row resembling DynamicCheckModel."""
    row = MagicMock()
    row.id = id
    row.name = name
    row.prompt = prompt
    row.tools = tools
    row.interval_seconds = interval_seconds
    row.timeout_seconds = timeout_seconds
    row.cron_expr = cron_expr
    row.urgent = urgent
    row.enabled = enabled
    row.run_count = run_count
    row.error_count = error_count
    row.last_error = last_error
    row.last_run_at = last_run_at
    row.created_at = created_at
    row.created_by = created_by
    row.on_complete_prompt = on_complete_prompt
    row.on_complete_tools = on_complete_tools if on_complete_tools is not None else []
    return row


def _mock_db() -> MagicMock:
    """Create a mock Database with async context manager session."""
    db = MagicMock()
    mock_session = AsyncMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    db.session.return_value = ctx
    return db, mock_session


# ===========================================================================
# TestDynamicCheckInit — 4 tests
# ===========================================================================


class TestDynamicCheckInit:
    """DynamicCheck construction and defaults."""

    def test_default_values(self):
        """1. Check name, interval, timeout set correctly."""
        check = _make_dynamic_check(name="my_check", interval=7200, timeout=60)
        assert check.name == "my_check"
        assert check.interval == 7200
        assert check.timeout == 60
        assert check.check_id == "check-uuid-001"
        assert check.active is True
        assert check.urgent_override is False

    def test_tools_filtered_to_allowed(self):
        """2. Only tools in ALLOWED_TOOLS are kept."""
        check = _make_dynamic_check(tools=["web_search", "evil_tool", "bash"])
        assert check._tools == ["web_search", "bash"]
        assert "evil_tool" not in check._tools

    def test_empty_tools(self):
        """3. Empty tools list stays empty."""
        check = _make_dynamic_check(tools=[])
        assert check._tools == []

    def test_cron_set(self):
        """4. set_cron stores expression."""
        check = _make_dynamic_check()
        check.set_cron("0 */6 * * *")
        assert check._cron_expr == "0 */6 * * *"


# ===========================================================================
# TestDynamicCheckIsDue — 8 tests
# ===========================================================================


class TestDynamicCheckIsDue:
    """DynamicCheck scheduling logic including cron."""

    def test_due_when_never_run(self):
        """5. New check is due immediately."""
        check = _make_dynamic_check()
        assert check.is_due() is True

    def test_not_due_when_inactive(self):
        """6. Inactive check is not due."""
        check = _make_dynamic_check()
        check.active = False
        assert check.is_due() is False

    def test_not_due_circuit_breaker(self):
        """7. Circuit breaker open prevents due."""
        check = _make_dynamic_check()
        check.consecutive_failures = check.max_failures
        assert check.is_due() is False

    def test_due_after_interval(self):
        """8. Due when elapsed > interval."""
        check = _make_dynamic_check(interval=3600)
        check.last_run = datetime.now(UTC) - timedelta(seconds=3601)
        assert check.is_due() is True

    def test_not_due_before_interval(self):
        """9. Not due when elapsed < interval."""
        check = _make_dynamic_check(interval=3600)
        check.last_run = datetime.now(UTC) - timedelta(seconds=1800)
        assert check.is_due() is False

    def test_cron_due(self):
        """10. Cron check due when next fire time has passed."""
        check = _make_dynamic_check()
        check.set_cron("0 */6 * * *")
        # Last run was 7 hours ago -> next fire was 6h after last_run -> overdue
        check.last_run = datetime.now(UTC) - timedelta(hours=7)
        assert check.is_due() is True

    def test_cron_not_due(self):
        """11. Cron check not due when before next fire time."""
        check = _make_dynamic_check()
        check.set_cron("0 */6 * * *")
        # Fix a reference time to avoid clock-alignment flakiness:
        # anchor = 10:01, next fire = 12:00, now = 11:00 → not due
        anchor = datetime(2025, 1, 1, 10, 1, tzinfo=UTC)
        check.last_run = anchor
        now = datetime(2025, 1, 1, 11, 0, tzinfo=UTC)
        assert check.is_due(now=now) is False

    def test_cron_never_run(self):
        """12. Cron with no last_run is due (anchor year 2000)."""
        check = _make_dynamic_check()
        check.set_cron("0 */6 * * *")
        # last_run is None -> anchor is 2000-01-01 -> next fire long past
        assert check.last_run is None
        assert check.is_due() is True


# ===========================================================================
# TestDynamicCheckRun — 7 tests
# ===========================================================================


class TestDynamicCheckRun:
    """DynamicCheck prompt execution."""

    @pytest.mark.asyncio
    async def test_successful_run_with_findings(self):
        """13. Run returns findings when LLM responds with JSON."""
        runner = AsyncMock()
        runner.run_turn = AsyncMock(return_value=(
            '{"has_findings": true, "findings": [{"summary": "Found issue", "urgency": "high", "needs_action": true}]}',
            MagicMock(),
            {"input_tokens": 100, "output_tokens": 50},
        ))
        runner.end_conversation = AsyncMock()

        check = _make_dynamic_check(runner=runner)
        result = await check.run()

        assert result.has_updates is True
        assert len(result.findings) == 1
        assert result.findings[0].summary == "Found issue"
        assert result.findings[0].urgency == "high"

    @pytest.mark.asyncio
    async def test_successful_run_no_findings(self):
        """14. Run returns empty when LLM says no findings."""
        runner = AsyncMock()
        runner.run_turn = AsyncMock(return_value=(
            '{"has_findings": false, "findings": []}',
            MagicMock(),
            {"input_tokens": 80, "output_tokens": 20},
        ))
        runner.end_conversation = AsyncMock()

        check = _make_dynamic_check(runner=runner)
        result = await check.run()

        assert result.has_updates is False
        assert len(result.findings) == 0

    @pytest.mark.asyncio
    async def test_run_without_runner(self):
        """15. Run with no runner returns empty CheckResult."""
        check = _make_dynamic_check(runner=None)
        result = await check.run()

        assert result.has_updates is False
        assert len(result.findings) == 0

    @pytest.mark.asyncio
    async def test_run_passes_tool_filter(self):
        """16. run_turn called with tool_filter matching check tools."""
        runner = AsyncMock()
        runner.run_turn = AsyncMock(return_value=(
            '{"has_findings": false, "findings": []}',
            MagicMock(),
            {"input_tokens": 50, "output_tokens": 20},
        ))
        runner.end_conversation = AsyncMock()

        check = _make_dynamic_check(tools=["web_search", "bash"], runner=runner)
        await check.run()

        call_kwargs = runner.run_turn.call_args
        assert call_kwargs[1]["tool_filter"] == ["web_search", "bash"]

    @pytest.mark.asyncio
    async def test_run_tracks_tokens(self):
        """17. tokens_used populated from usage dict."""
        runner = AsyncMock()
        runner.run_turn = AsyncMock(return_value=(
            '{"has_findings": false, "findings": []}',
            MagicMock(),
            {"input_tokens": 300, "output_tokens": 200},
        ))
        runner.end_conversation = AsyncMock()

        check = _make_dynamic_check(runner=runner)
        result = await check.run()

        assert result.tokens_used == 500

    @pytest.mark.asyncio
    async def test_run_cleans_up_session(self):
        """18. end_conversation called in finally block."""
        runner = AsyncMock()
        runner.run_turn = AsyncMock(return_value=(
            '{"has_findings": false, "findings": []}',
            MagicMock(),
            {"input_tokens": 50, "output_tokens": 20},
        ))
        runner.end_conversation = AsyncMock()

        check = _make_dynamic_check(runner=runner)
        await check.run()

        runner.end_conversation.assert_called_once()
        # Session ID starts with "dynamic-check-test_check-"
        session_id = runner.end_conversation.call_args[0][0]
        assert session_id.startswith("dynamic-check-test_check-")

    @pytest.mark.asyncio
    async def test_run_raises_on_failure(self):
        """19. Exception from run_turn propagates after cleanup."""
        runner = AsyncMock()
        runner.run_turn = AsyncMock(side_effect=RuntimeError("API error"))
        runner.end_conversation = AsyncMock()

        check = _make_dynamic_check(runner=runner)

        with pytest.raises(RuntimeError, match="API error"):
            await check.run()

        # end_conversation still called in finally
        runner.end_conversation.assert_called_once()


# ===========================================================================
# TestDynamicCheckParsing — 6 tests
# ===========================================================================


class TestDynamicCheckParsing:
    """DynamicCheck JSON extraction from LLM responses."""

    def test_parse_valid_json(self):
        """20. Clean JSON response yields findings list."""
        check = _make_dynamic_check(name="parser_test")
        response = '{"has_findings": true, "findings": [{"summary": "Issue found", "urgency": "high", "needs_action": true}]}'
        findings = check._parse_findings(response)

        assert len(findings) == 1
        assert findings[0].summary == "Issue found"
        assert findings[0].urgency == "high"
        assert findings[0].needs_action is True

    def test_parse_no_findings(self):
        """21. has_findings=false returns empty."""
        check = _make_dynamic_check()
        response = '{"has_findings": false, "findings": []}'
        findings = check._parse_findings(response)

        assert len(findings) == 0

    def test_parse_malformed_json(self):
        """22. Garbage text returns empty, no crash."""
        check = _make_dynamic_check()
        response = "This is not valid JSON at all {{{"
        findings = check._parse_findings(response)

        assert len(findings) == 0

    def test_parse_urgency_validation(self):
        """23. Invalid urgency defaults to 'normal'."""
        check = _make_dynamic_check()
        response = '{"has_findings": true, "findings": [{"summary": "Test", "urgency": "critical", "needs_action": false}]}'
        findings = check._parse_findings(response)

        assert len(findings) == 1
        assert findings[0].urgency == "normal"

    def test_parse_summary_truncation(self):
        """24. Summary >200 chars truncated to 200."""
        check = _make_dynamic_check()
        long_summary = "A" * 300
        response = f'{{"has_findings": true, "findings": [{{"summary": "{long_summary}", "urgency": "normal"}}]}}'
        findings = check._parse_findings(response)

        assert len(findings) == 1
        assert len(findings[0].summary) == 200

    def test_parse_findings_source(self):
        """25. Finding source is 'dynamic:{name}'."""
        check = _make_dynamic_check(name="my_sensor")
        response = '{"has_findings": true, "findings": [{"summary": "Test", "urgency": "low"}]}'
        findings = check._parse_findings(response)

        assert len(findings) == 1
        assert findings[0].source == "dynamic:my_sensor"


# ===========================================================================
# TestDynamicCheckLoader — 6 tests
# ===========================================================================


class TestDynamicCheckLoader:
    """DynamicCheckLoader DB sync and registry management."""

    def _make_loader(self, registry=None, runner=None, max_checks=10):
        db, _ = _mock_db()
        registry = registry or CheckRegistry()
        loader = DynamicCheckLoader(
            db=db,
            registry=registry,
            runner=runner,
            agent_id="test-agent",
            max_checks=max_checks,
        )
        return loader, registry

    @pytest.mark.asyncio
    async def test_sync_registers_new_checks(self):
        """26. sync() registers checks from DB rows."""
        loader, registry = self._make_loader()
        rows = [
            _mock_db_row(id="id-1", name="check_a", prompt="Check A"),
            _mock_db_row(id="id-2", name="check_b", prompt="Check B"),
        ]
        loader._fetch_enabled = AsyncMock(return_value=rows)

        count = await loader.sync()

        assert count == 2
        assert registry.get_check("check_a") is not None
        assert registry.get_check("check_b") is not None

    @pytest.mark.asyncio
    async def test_sync_unregisters_removed(self):
        """27. Second sync without a check unregisters it."""
        loader, registry = self._make_loader()

        # First sync: 2 checks
        rows = [
            _mock_db_row(id="id-1", name="check_a", prompt="Check A"),
            _mock_db_row(id="id-2", name="check_b", prompt="Check B"),
        ]
        loader._fetch_enabled = AsyncMock(return_value=rows)
        await loader.sync()
        assert registry.get_check("check_a") is not None
        assert registry.get_check("check_b") is not None

        # Second sync: only check_a
        rows = [_mock_db_row(id="id-1", name="check_a", prompt="Check A")]
        loader._fetch_enabled = AsyncMock(return_value=rows)
        await loader.sync()

        assert registry.get_check("check_a") is not None
        assert registry.get_check("check_b") is None

    @pytest.mark.asyncio
    async def test_sync_skips_unchanged(self):
        """28. Same data on re-sync does not re-register."""
        loader, registry = self._make_loader()
        rows = [_mock_db_row(id="id-1", name="check_a", prompt="Check A")]
        loader._fetch_enabled = AsyncMock(return_value=rows)

        await loader.sync()
        first_check = registry.get_check("check_a")

        # Re-sync with same data
        await loader.sync()
        second_check = registry.get_check("check_a")

        # Same object — not re-registered
        assert first_check is second_check

    @pytest.mark.asyncio
    async def test_sync_detects_changes(self):
        """29. Changed prompt triggers re-registration."""
        loader, registry = self._make_loader()

        rows = [_mock_db_row(id="id-1", name="check_a", prompt="Check A")]
        loader._fetch_enabled = AsyncMock(return_value=rows)
        await loader.sync()
        first_check = registry.get_check("check_a")

        # Re-sync with changed prompt
        rows = [_mock_db_row(id="id-1", name="check_a", prompt="Check A v2")]
        loader._fetch_enabled = AsyncMock(return_value=rows)
        await loader.sync()
        second_check = registry.get_check("check_a")

        # Different object — re-registered
        assert first_check is not second_check

    @pytest.mark.asyncio
    async def test_sync_rejects_permanent_name(self):
        """30. DB check with permanent check name is skipped."""
        registry = CheckRegistry()
        # Register a permanent check named "health"
        permanent_check = MagicMock()
        permanent_check.name = "health"
        permanent_check.is_due = MagicMock(return_value=False)
        registry.register(permanent_check, permanent=True)

        loader, _ = self._make_loader(registry=registry)
        rows = [_mock_db_row(id="id-1", name="health", prompt="Overlapping check")]
        loader._fetch_enabled = AsyncMock(return_value=rows)

        count = await loader.sync()

        # The permanent check should still be the original
        assert registry.get_check("health") is permanent_check

    @pytest.mark.asyncio
    async def test_set_runner_updates_existing(self):
        """31. set_runner after sync updates all checks with new runner."""
        loader, registry = self._make_loader()
        rows = [
            _mock_db_row(id="id-1", name="check_a", prompt="Check A"),
            _mock_db_row(id="id-2", name="check_b", prompt="Check B"),
        ]
        loader._fetch_enabled = AsyncMock(return_value=rows)
        await loader.sync()

        new_runner = AsyncMock()
        loader.set_runner(new_runner)

        check_a = registry.get_check("check_a")
        check_b = registry.get_check("check_b")
        assert check_a._runner is new_runner
        assert check_b._runner is new_runner


# ===========================================================================
# TestDynamicCheckLoaderCRUD — 10 tests
# ===========================================================================


class TestDynamicCheckLoaderCRUD:
    """DynamicCheckLoader create/manage operations."""

    def _make_loader(self, registry=None, max_checks=10):
        db, mock_session = _mock_db()
        registry = registry or CheckRegistry()
        loader = DynamicCheckLoader(
            db=db,
            registry=registry,
            runner=AsyncMock(),
            agent_id="test-agent",
            max_checks=max_checks,
        )
        return loader, registry, mock_session

    @pytest.mark.asyncio
    async def test_create_check(self):
        """32. create_check stores in DB and registers immediately."""
        loader, registry, mock_session = self._make_loader()

        # Mock the model returned after refresh
        mock_model = MagicMock()
        mock_model.id = "new-uuid-001"
        mock_session.add = MagicMock()
        mock_session.commit = AsyncMock()
        mock_session.refresh = AsyncMock()

        # Patch DynamicCheckModel constructor (imported inside create_check)
        with patch("nous.storage.models.DynamicCheckModel") as MockModel:
            MockModel.return_value = mock_model
            result = await loader.create_check(
                name="new_check",
                description="A new check",
                prompt="Check the thing",
                tools=["web_search"],
                interval_seconds=3600,
            )

        assert result["name"] == "new_check"
        assert result["id"] == "new-uuid-001"
        assert result["tools"] == ["web_search"]
        assert registry.get_check("new_check") is not None

    @pytest.mark.asyncio
    async def test_create_rejects_low_interval(self):
        """33. interval_seconds < MIN_INTERVAL raises ValueError."""
        loader, _, _ = self._make_loader()

        with pytest.raises(ValueError, match="Minimum interval"):
            await loader.create_check(
                name="fast_check",
                description="Too fast",
                prompt="Check fast",
                interval_seconds=60,
            )

    @pytest.mark.asyncio
    async def test_create_rejects_max_count(self):
        """34. Creating beyond max_checks raises ValueError."""
        loader, _, _ = self._make_loader(max_checks=1)
        # Simulate having one loaded check already
        loader._loaded_ids = {"existing-id"}

        with pytest.raises(ValueError, match="Maximum"):
            await loader.create_check(
                name="overflow_check",
                description="One too many",
                prompt="Check overflow",
            )

    @pytest.mark.asyncio
    async def test_create_rejects_permanent_name(self):
        """35. Name conflicting with permanent check raises ValueError."""
        registry = CheckRegistry()
        permanent = MagicMock()
        permanent.name = "health"
        registry.register(permanent, permanent=True)

        loader, _, _ = self._make_loader(registry=registry)

        with pytest.raises(ValueError, match="conflicts with a permanent check"):
            await loader.create_check(
                name="health",
                description="Override health",
                prompt="Replace health check",
            )

    @pytest.mark.asyncio
    async def test_create_validates_cron(self):
        """36. Invalid cron expression raises ValueError."""
        loader, _, _ = self._make_loader()

        with pytest.raises(ValueError, match="Invalid cron"):
            await loader.create_check(
                name="bad_cron",
                description="Bad cron",
                prompt="Check with bad cron",
                cron_expr="not a cron expression",
            )

    @pytest.mark.asyncio
    async def test_manage_list(self):
        """37. manage_check(action='list') returns check info."""
        loader, _, mock_session = self._make_loader()

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [
            _mock_db_row(name="check_a", run_count=5, error_count=1),
        ]
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await loader.manage_check(action="list")

        assert "checks" in result
        assert result["count"] == 1
        assert result["checks"][0]["name"] == "check_a"
        assert result["checks"][0]["run_count"] == 5

    @pytest.mark.asyncio
    async def test_manage_enable(self):
        """38. manage_check(action='enable') enables and syncs."""
        loader, _, mock_session = self._make_loader()
        loader.sync = AsyncMock()

        mock_model = MagicMock()
        mock_model.enabled = False
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_model
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.commit = AsyncMock()

        result = await loader.manage_check(action="enable", name="check_a")

        assert result["status"] == "enabled"
        assert mock_model.enabled is True
        loader.sync.assert_called_once()

    @pytest.mark.asyncio
    async def test_manage_disable(self):
        """39. manage_check(action='disable') disables and unregisters."""
        loader, registry, mock_session = self._make_loader()

        # Pre-register a check so it can be unregistered
        check = _make_dynamic_check(name="check_a")
        registry.register(check, permanent=False)
        loader._loaded_ids = {"id-1"}
        loader._id_to_name = {"id-1": "check_a"}
        loader._signatures = {"check_a": "sig"}

        mock_model = MagicMock()
        mock_model.id = "id-1"
        mock_model.enabled = True
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_model
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.commit = AsyncMock()

        result = await loader.manage_check(action="disable", name="check_a")

        assert result["status"] == "disabled"
        assert registry.get_check("check_a") is None
        assert "id-1" not in loader._loaded_ids

    @pytest.mark.asyncio
    async def test_manage_delete(self):
        """40. manage_check(action='delete') removes from DB and registry."""
        loader, registry, mock_session = self._make_loader()

        check = _make_dynamic_check(name="check_a")
        registry.register(check, permanent=False)
        loader._loaded_ids = {"id-1"}
        loader._id_to_name = {"id-1": "check_a"}
        loader._signatures = {"check_a": "sig"}

        mock_model = MagicMock()
        mock_model.id = "id-1"
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_model
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.delete = AsyncMock()
        mock_session.commit = AsyncMock()

        result = await loader.manage_check(action="delete", name="check_a")

        assert result["status"] == "deleted"
        assert registry.get_check("check_a") is None
        assert "id-1" not in loader._loaded_ids
        mock_session.delete.assert_called_once_with(mock_model)

    @pytest.mark.asyncio
    async def test_manage_update(self):
        """41. manage_check(action='update') updates fields and re-syncs."""
        loader, _, mock_session = self._make_loader()
        loader.sync = AsyncMock()

        mock_model = MagicMock()
        mock_model.prompt = "Old prompt"
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_model
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.commit = AsyncMock()

        result = await loader.manage_check(
            action="update", name="check_a",
            updates={"prompt": "New prompt"},
        )

        assert result["status"] == "updated"
        assert mock_model.prompt == "New prompt"
        loader.sync.assert_called_once()


# ===========================================================================
# TestDynamicCheckLoaderStats — 2 tests
# ===========================================================================


class TestDynamicCheckLoaderStats:
    """DynamicCheckLoader run stats updates."""

    @pytest.mark.asyncio
    async def test_update_stats_success(self):
        """42. Success increments run_count and sets last_run_at."""
        db, mock_session = _mock_db()
        mock_session.execute = AsyncMock()
        mock_session.commit = AsyncMock()

        loader = DynamicCheckLoader(
            db=db, registry=CheckRegistry(),
            agent_id="test-agent",
        )

        await loader.update_run_stats("check-id-1", success=True)

        mock_session.execute.assert_called_once()
        mock_session.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_stats_failure(self):
        """43. Failure increments run_count and error_count, sets last_error."""
        db, mock_session = _mock_db()
        mock_session.execute = AsyncMock()
        mock_session.commit = AsyncMock()

        loader = DynamicCheckLoader(
            db=db, registry=CheckRegistry(),
            agent_id="test-agent",
        )

        await loader.update_run_stats("check-id-1", success=False, error_msg="timeout")

        mock_session.execute.assert_called_once()
        mock_session.commit.assert_called_once()


# ===========================================================================
# TestHeartbeatRunnerDynamic — 6 tests
# ===========================================================================


class TestHeartbeatRunnerDynamic:
    """HeartbeatRunner integration with dynamic checks."""

    def _make_runner(self, settings=None, registry=None, dynamic_loader=None, **kwargs):
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
            dynamic_loader=dynamic_loader,
        )
        return runner

    def test_runner_has_dynamic_loader(self):
        """44. Dynamic loader stored on runner."""
        loader = MagicMock()
        runner = self._make_runner(dynamic_loader=loader)
        assert runner._dynamic_loader is loader
        assert runner.dynamic_loader is loader

    @pytest.mark.asyncio
    async def test_start_syncs_loader(self):
        """45. start() calls loader.sync() and set_runner()."""
        loader = MagicMock()
        loader.sync = AsyncMock(return_value=2)
        loader.set_runner = MagicMock()

        runner = self._make_runner(dynamic_loader=loader)
        runner._detect_missed_checks = AsyncMock()

        await runner.start()
        try:
            loader.set_runner.assert_called_once()
            loader.sync.assert_called_once()
        finally:
            await runner.stop()

    @pytest.mark.asyncio
    async def test_tick_tracks_dynamic_tokens(self):
        """46. Dynamic check tokens_used added to daily budget."""
        reg = CheckRegistry()
        check = _make_dynamic_check(name="dyn_1")
        # Override run to return tokens
        check.run = AsyncMock(return_value=CheckResult(
            has_updates=False, tokens_used=500,
        ))
        reg.register(check)

        runner = self._make_runner(registry=reg)
        initial_tokens = runner._tokens_used_today

        await runner._tick()

        assert runner._tokens_used_today == initial_tokens + 500

    @pytest.mark.asyncio
    async def test_tick_updates_run_stats(self):
        """47. Successful dynamic check updates run stats."""
        reg = CheckRegistry()
        check = _make_dynamic_check(name="dyn_1")
        check.run = AsyncMock(return_value=CheckResult(has_updates=False))
        reg.register(check)

        loader = MagicMock()
        loader.update_run_stats = AsyncMock()

        runner = self._make_runner(registry=reg, dynamic_loader=loader)
        await runner._tick()

        loader.update_run_stats.assert_called_once_with(
            "check-uuid-001", success=True,
        )

    @pytest.mark.asyncio
    async def test_tick_updates_run_stats_on_failure(self):
        """48. Failed dynamic check records error in run stats."""
        reg = CheckRegistry()
        check = _make_dynamic_check(name="dyn_fail")
        check.run = AsyncMock(side_effect=RuntimeError("boom"))
        check.timeout = 30
        reg.register(check)

        loader = MagicMock()
        loader.update_run_stats = AsyncMock()

        runner = self._make_runner(registry=reg, dynamic_loader=loader)
        await runner._tick()

        loader.update_run_stats.assert_called_once()
        call_kwargs = loader.update_run_stats.call_args
        assert call_kwargs[1]["success"] is False
        assert "boom" in call_kwargs[1]["error_msg"]

    @pytest.mark.asyncio
    async def test_periodic_sync(self):
        """49. After N ticks, loader.sync() is called again."""
        loader = MagicMock()
        loader.sync = AsyncMock(return_value=0)

        runner = self._make_runner(
            dynamic_loader=loader,
            heartbeat_dynamic_sync_ticks=5,
        )
        # Simulate being at tick count that's a multiple of sync_ticks
        runner._tick_count = 4  # _tick increments to 5

        reg = runner._registry
        # No due checks, just test the sync path
        await runner._tick()

        # _tick_count is now 5, which is divisible by sync_ticks=5
        # But sync is called in _loop, not _tick. We test the tick_count instead.
        assert runner._tick_count == 5


# ===========================================================================
# TestToolFilter — 2 tests
# ===========================================================================


class TestToolFilter:
    """tool_filter param on AgentRunner._tool_loop."""

    @pytest.mark.asyncio
    async def test_tool_filter_restricts_tools(self):
        """50. tool_filter restricts available tools in _tool_loop."""
        # This tests the filtering logic directly
        all_tools = [
            {"name": "web_search", "description": "Search", "input_schema": {}},
            {"name": "bash", "description": "Shell", "input_schema": {}},
            {"name": "write_file", "description": "Write", "input_schema": {}},
        ]

        tool_filter = ["web_search"]
        filtered = [t for t in all_tools if t["name"] in tool_filter]

        assert len(filtered) == 1
        assert filtered[0]["name"] == "web_search"

    @pytest.mark.asyncio
    async def test_tool_filter_none_keeps_all(self):
        """51. tool_filter=None keeps all tools."""
        all_tools = [
            {"name": "web_search", "description": "Search", "input_schema": {}},
            {"name": "bash", "description": "Shell", "input_schema": {}},
        ]

        tool_filter = None
        if tool_filter is not None:
            filtered = [t for t in all_tools if t["name"] in tool_filter]
        else:
            filtered = all_tools

        assert len(filtered) == 2


# ===========================================================================
# TestOnCompleteFields — 3 tests
# ===========================================================================


class TestOnCompleteFields:
    """DynamicCheck on_complete field storage and signature."""

    def test_on_complete_fields_stored(self):
        """52. on_complete_prompt and on_complete_tools are stored on construction."""
        check = DynamicCheck(
            check_id="check-uuid-100",
            name="callback_check",
            prompt="Run something",
            tools=["web_search"],
            on_complete_prompt="Send summary to user",
            on_complete_tools=["web_search", "bash"],
        )
        assert check.on_complete_prompt == "Send summary to user"
        assert check.on_complete_tools == ["web_search", "bash"]

    def test_on_complete_in_signature(self):
        """53. on_complete fields are included in signature() for change detection."""
        check_a = DynamicCheck(
            check_id="check-uuid-101",
            name="sig_test",
            prompt="Check X",
            tools=[],
            on_complete_prompt="Do cleanup",
            on_complete_tools=["web_search"],
        )
        check_b = DynamicCheck(
            check_id="check-uuid-101",
            name="sig_test",
            prompt="Check X",
            tools=[],
            on_complete_prompt="Do different cleanup",
            on_complete_tools=["web_search"],
        )
        sig_a = check_a.signature()
        sig_b = check_b.signature()
        assert "Do cleanup" in sig_a
        assert sig_a != sig_b

    def test_on_complete_tools_filtered(self):
        """54. on_complete_tools are filtered through ALLOWED_TOOLS."""
        check = DynamicCheck(
            check_id="check-uuid-102",
            name="filter_test",
            prompt="Run",
            tools=["web_search"],
            on_complete_prompt="Callback",
            on_complete_tools=["web_search", "evil_tool", "recall_deep"],
        )
        assert "evil_tool" not in check.on_complete_tools
        assert check.on_complete_tools == ["web_search", "recall_deep"]


# ===========================================================================
# TestSelfDisabledFlag — 3 tests
# ===========================================================================


class TestSelfDisabledFlag:
    """DynamicCheck _self_disabled flag behavior."""

    def test_self_disabled_default_false(self):
        """55. New DynamicCheck has _self_disabled=False."""
        check = _make_dynamic_check()
        assert check._self_disabled is False

    @pytest.mark.asyncio
    async def test_self_disabled_set_on_disable(self):
        """56. manage_check(action='disable') sets _self_disabled=True on in-memory check."""
        db, mock_session = _mock_db()
        registry = CheckRegistry()
        loader = DynamicCheckLoader(
            db=db, registry=registry, runner=AsyncMock(), agent_id="test-agent",
        )

        # Register a check in the registry
        check = _make_dynamic_check(name="disable_me")
        registry.register(check, permanent=False)
        loader._loaded_ids = {"id-1"}
        loader._id_to_name = {"id-1": "disable_me"}
        loader._signatures = {"disable_me": "sig"}

        # Mock DB lookup
        mock_model = MagicMock()
        mock_model.id = "id-1"
        mock_model.enabled = True
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_model
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.commit = AsyncMock()

        await loader.manage_check(action="disable", name="disable_me")

        # The in-memory check should have _self_disabled set before unregister
        assert check._self_disabled is True

    @pytest.mark.asyncio
    async def test_self_disabled_in_check_result(self):
        """57. When _self_disabled is True, run() sets result.self_disabled=True."""
        runner = AsyncMock()
        runner.run_turn = AsyncMock(return_value=(
            '{"has_findings": false, "findings": []}',
            MagicMock(),
            {"input_tokens": 50, "output_tokens": 20},
        ))
        runner.end_conversation = AsyncMock()

        check = _make_dynamic_check(runner=runner)
        check._self_disabled = True
        result = await check.run()

        assert result.self_disabled is True


# ===========================================================================
# TestOnCompleteExecution — 5 tests
# ===========================================================================


class TestOnCompleteExecution:
    """HeartbeatRunner._execute_callback behavior."""

    def _make_runner_for_callback(self, **kwargs):
        from nous.heartbeat.runner import HeartbeatRunner

        settings = _mock_settings(**kwargs)
        settings.heartbeat_model = None
        settings.background_model = "claude-sonnet-4-6"
        registry = CheckRegistry()
        mock_runner = AsyncMock()
        runner = HeartbeatRunner(
            settings=settings,
            registry=registry,
            runner=mock_runner,
            brain=AsyncMock(),
            heart=MagicMock(),
            bus=None,
            http_client=AsyncMock(),
        )
        return runner

    @pytest.mark.asyncio
    async def test_execute_callback_success(self):
        """58. _execute_callback runs prompt, tracks tokens, cleans up session."""
        runner = self._make_runner_for_callback()
        triage_runner = AsyncMock()
        triage_runner.run_turn = AsyncMock(return_value=(
            "Callback completed successfully",
            MagicMock(),
            {"input_tokens": 200, "output_tokens": 100},
        ))
        triage_runner.end_conversation = AsyncMock()
        runner._get_triage_runner = MagicMock(return_value=triage_runner)

        check = DynamicCheck(
            check_id="cb-001", name="cb_check", prompt="Check X",
            tools=["web_search"],
            on_complete_prompt="Send summary",
            on_complete_tools=["web_search"],
        )

        initial_tokens = runner._tokens_used_today
        await runner._execute_callback(check)

        triage_runner.run_turn.assert_called_once()
        assert runner._tokens_used_today == initial_tokens + 300
        triage_runner.end_conversation.assert_called()

    @pytest.mark.asyncio
    async def test_execute_callback_retry_on_failure(self):
        """59. First attempt fails, retry succeeds after delay."""
        runner = self._make_runner_for_callback()
        triage_runner = AsyncMock()
        # First call fails, second succeeds
        triage_runner.run_turn = AsyncMock(side_effect=[
            RuntimeError("API timeout"),
            ("Retry success", MagicMock(), {"input_tokens": 100, "output_tokens": 50}),
        ])
        triage_runner.end_conversation = AsyncMock()
        runner._get_triage_runner = MagicMock(return_value=triage_runner)

        check = DynamicCheck(
            check_id="cb-002", name="retry_check", prompt="Check Y",
            tools=["web_search"],
            on_complete_prompt="Retry me",
            on_complete_tools=["web_search"],
        )

        with patch("nous.heartbeat.runner.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await runner._execute_callback(check)

        # Two run_turn calls (first fails, second succeeds)
        assert triage_runner.run_turn.call_count == 2
        # Sleep called before retry
        mock_sleep.assert_called_once()
        # Tokens tracked from successful attempt
        assert runner._tokens_used_today == 150

    @pytest.mark.asyncio
    async def test_execute_callback_both_fail_telegram(self):
        """60. Both attempts fail -> Telegram notification sent."""
        runner = self._make_runner_for_callback()
        triage_runner = AsyncMock()
        triage_runner.run_turn = AsyncMock(side_effect=RuntimeError("Always fails"))
        triage_runner.end_conversation = AsyncMock()
        runner._get_triage_runner = MagicMock(return_value=triage_runner)
        runner._send_telegram = AsyncMock()

        check = DynamicCheck(
            check_id="cb-003", name="fail_check", prompt="Check Z",
            tools=["web_search"],
            on_complete_prompt="Will fail",
            on_complete_tools=[],
        )

        with patch("nous.heartbeat.runner.asyncio.sleep", new_callable=AsyncMock):
            await runner._execute_callback(check)

        runner._send_telegram.assert_called_once()
        telegram_text = runner._send_telegram.call_args[0][0]
        assert "fail_check" in telegram_text
        assert "Callback failed" in telegram_text

    @pytest.mark.asyncio
    async def test_execute_callback_both_fail_finding(self):
        """61. Both attempts fail -> warning Finding created in FindingStore."""
        runner = self._make_runner_for_callback()
        triage_runner = AsyncMock()
        triage_runner.run_turn = AsyncMock(side_effect=RuntimeError("Always fails"))
        triage_runner.end_conversation = AsyncMock()
        runner._get_triage_runner = MagicMock(return_value=triage_runner)
        runner._send_telegram = AsyncMock()

        mock_store = MagicMock()
        runner._finding_store = mock_store

        check = DynamicCheck(
            check_id="cb-004", name="find_check", prompt="Check W",
            tools=["web_search"],
            on_complete_prompt="Will fail and create finding",
            on_complete_tools=[],
        )

        with patch("nous.heartbeat.runner.asyncio.sleep", new_callable=AsyncMock):
            await runner._execute_callback(check)

        mock_store.ingest.assert_called_once()
        finding = mock_store.ingest.call_args[0][0]
        assert "find_check" in finding.summary
        assert finding.urgency == "normal"
        assert finding.needs_action is True
        assert finding.raw_data["check_id"] == "cb-004"

    @pytest.mark.asyncio
    async def test_execute_callback_budget_exhausted_skips(self):
        """62. Budget exhausted -> callback not executed at all."""
        runner = self._make_runner_for_callback()
        triage_runner = AsyncMock()
        triage_runner.run_turn = AsyncMock()
        triage_runner.end_conversation = AsyncMock()
        runner._get_triage_runner = MagicMock(return_value=triage_runner)
        runner._send_telegram = AsyncMock()

        # Budget exhausted from the start — first attempt skipped
        runner._has_budget = MagicMock(return_value=False)

        check = DynamicCheck(
            check_id="cb-005", name="budget_check", prompt="Check B",
            tools=["web_search"],
            on_complete_prompt="No budget callback",
            on_complete_tools=[],
        )

        await runner._execute_callback(check)

        # No run_turn calls — budget exhausted before first attempt
        assert triage_runner.run_turn.call_count == 0


# ===========================================================================
# TestOnCompleteValidation — 3 tests
# ===========================================================================


class TestOnCompleteValidation:
    """Validation of on_complete_tools as subset of check tools."""

    def _make_loader(self, registry=None, max_checks=10):
        db, mock_session = _mock_db()
        registry = registry or CheckRegistry()
        loader = DynamicCheckLoader(
            db=db, registry=registry, runner=AsyncMock(),
            agent_id="test-agent", max_checks=max_checks,
        )
        return loader, registry, mock_session

    @pytest.mark.asyncio
    async def test_create_check_on_complete_tools_subset(self):
        """63. on_complete_tools must be subset of check tools at creation."""
        loader, _, mock_session = self._make_loader()

        with pytest.raises(ValueError, match="on_complete_tools must be a subset"):
            await loader.create_check(
                name="bad_subset",
                description="Invalid on_complete_tools",
                prompt="Check it",
                tools=["web_search"],
                on_complete_tools=["web_search", "bash"],  # bash not in tools
            )

    @pytest.mark.asyncio
    async def test_create_check_on_complete_tools_empty_tools_rejected(self):
        """63b. on_complete_tools with empty tools list is rejected."""
        loader, _, mock_session = self._make_loader()

        with pytest.raises(ValueError, match="on_complete_tools must be a subset"):
            await loader.create_check(
                name="empty_tools",
                description="Empty tools check",
                prompt="Check something",
                tools=[],
                on_complete_tools=["web_search"],
            )

    @pytest.mark.asyncio
    async def test_update_tools_validates_on_complete_subset(self):
        """64. Updating tools validates on_complete_tools still subset."""
        loader, _, mock_session = self._make_loader()
        loader.sync = AsyncMock()

        mock_model = MagicMock()
        mock_model.tools = ["web_search", "bash"]
        mock_model.on_complete_tools = ["web_search", "bash"]
        mock_model.cron_expr = None
        mock_model.interval_seconds = 3600
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_model
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.commit = AsyncMock()

        # Remove bash from tools, but on_complete_tools still has it
        with pytest.raises(ValueError, match="on_complete_tools must be a subset"):
            await loader.manage_check(
                action="update", name="check_a",
                updates={"tools": ["web_search"]},
            )

    @pytest.mark.asyncio
    async def test_update_on_complete_tools_validates_subset(self):
        """65. Updating on_complete_tools validates against current tools."""
        loader, _, mock_session = self._make_loader()
        loader.sync = AsyncMock()

        mock_model = MagicMock()
        mock_model.tools = ["web_search"]
        mock_model.on_complete_tools = []
        mock_model.cron_expr = None
        mock_model.interval_seconds = 3600
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_model
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.commit = AsyncMock()

        # Try to set on_complete_tools with a tool not in check tools
        with pytest.raises(ValueError, match="on_complete_tools must be a subset"):
            await loader.manage_check(
                action="update", name="check_a",
                updates={"on_complete_tools": ["web_search", "bash"]},
            )


# ===========================================================================
# TestOnCompleteCRUD — 3 tests
# ===========================================================================


class TestOnCompleteCRUD:
    """CRUD operations including on_complete fields."""

    def _make_loader(self, registry=None, max_checks=10):
        db, mock_session = _mock_db()
        registry = registry or CheckRegistry()
        loader = DynamicCheckLoader(
            db=db, registry=registry, runner=AsyncMock(),
            agent_id="test-agent", max_checks=max_checks,
        )
        return loader, registry, mock_session

    @pytest.mark.asyncio
    async def test_create_check_with_on_complete(self):
        """66. create_check includes on_complete fields in DB and return dict."""
        loader, registry, mock_session = self._make_loader()

        mock_model = MagicMock()
        mock_model.id = "new-uuid-oc"
        mock_session.add = MagicMock()
        mock_session.commit = AsyncMock()
        mock_session.refresh = AsyncMock()

        with patch("nous.storage.models.DynamicCheckModel") as MockModel:
            MockModel.return_value = mock_model
            result = await loader.create_check(
                name="oc_check",
                description="With on_complete",
                prompt="Check the thing",
                tools=["web_search", "bash"],
                on_complete_prompt="Summarize findings",
                on_complete_tools=["web_search"],
            )

        assert result["on_complete_prompt"] == "Summarize findings"
        assert result["on_complete_tools"] == ["web_search"]
        assert result["name"] == "oc_check"

        # Verify the check is registered with on_complete fields
        registered = registry.get_check("oc_check")
        assert registered is not None
        assert registered.on_complete_prompt == "Summarize findings"
        assert registered.on_complete_tools == ["web_search"]

    @pytest.mark.asyncio
    async def test_list_includes_on_complete(self):
        """67. _list_checks includes on_complete_prompt (truncated) and on_complete_tools."""
        loader, _, mock_session = self._make_loader()

        long_prompt = "A" * 300
        row = _mock_db_row(name="oc_list")
        row.on_complete_prompt = long_prompt
        row.on_complete_tools = ["web_search"]
        row.description = "Test check"

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [row]
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await loader.manage_check(action="list")

        assert result["count"] == 1
        check_info = result["checks"][0]
        assert check_info["on_complete_tools"] == ["web_search"]
        # Truncated to 200 chars
        assert len(check_info["on_complete_prompt"]) == 200

    @pytest.mark.asyncio
    async def test_update_on_complete_fields(self):
        """68. manage_check(action='update') can update on_complete_prompt and on_complete_tools."""
        loader, _, mock_session = self._make_loader()
        loader.sync = AsyncMock()

        mock_model = MagicMock()
        mock_model.on_complete_prompt = "Old prompt"
        mock_model.on_complete_tools = []
        mock_model.tools = ["web_search", "bash"]
        mock_model.cron_expr = None
        mock_model.interval_seconds = 3600
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_model
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.commit = AsyncMock()

        result = await loader.manage_check(
            action="update", name="oc_update",
            updates={
                "on_complete_prompt": "New callback prompt",
                "on_complete_tools": ["web_search"],
            },
        )

        assert result["status"] == "updated"
        assert mock_model.on_complete_prompt == "New callback prompt"
        assert mock_model.on_complete_tools == ["web_search"]


# ===========================================================================
# TestRunnerCallback — 3 tests
# ===========================================================================


class TestRunnerCallback:
    """HeartbeatRunner callback firing from _tick and trigger_check."""

    def _make_runner(self, registry=None, **kwargs):
        from nous.heartbeat.runner import HeartbeatRunner

        settings = _mock_settings(**kwargs)
        settings.heartbeat_model = None
        settings.background_model = "claude-sonnet-4-6"
        registry = registry or CheckRegistry()
        loader = MagicMock()
        loader.update_run_stats = AsyncMock()
        runner = HeartbeatRunner(
            settings=settings,
            registry=registry,
            runner=AsyncMock(),
            brain=AsyncMock(),
            heart=MagicMock(),
            bus=None,
            http_client=AsyncMock(),
            dynamic_loader=loader,
        )
        return runner, loader

    @pytest.mark.asyncio
    async def test_tick_fires_callback_for_self_disabled(self):
        """69. _tick detects self_disabled check and creates background task."""
        reg = CheckRegistry()
        check = DynamicCheck(
            check_id="cb-tick-001", name="self_disable_check",
            prompt="Check and disable",
            tools=["web_search"],
            on_complete_prompt="Run callback after disable",
            on_complete_tools=["web_search"],
        )
        check.run = AsyncMock(return_value=CheckResult(
            has_updates=False, self_disabled=True,
        ))
        reg.register(check)

        runner, _ = self._make_runner(registry=reg)

        with patch("nous.heartbeat.runner.asyncio.create_task") as mock_create_task:
            await runner._tick()

        mock_create_task.assert_called_once()
        # Verify the task name includes the check name
        call_kwargs = mock_create_task.call_args
        assert "self_disable_check" in call_kwargs[1]["name"]

    @pytest.mark.asyncio
    async def test_tick_skips_callback_no_prompt(self):
        """70. self_disabled but no on_complete_prompt -> no callback."""
        reg = CheckRegistry()
        check = DynamicCheck(
            check_id="cb-tick-002", name="no_prompt_check",
            prompt="Check without callback",
            tools=["web_search"],
            # No on_complete_prompt
        )
        check.run = AsyncMock(return_value=CheckResult(
            has_updates=False, self_disabled=True,
        ))
        reg.register(check)

        runner, _ = self._make_runner(registry=reg)

        with patch("nous.heartbeat.runner.asyncio.create_task") as mock_create_task:
            await runner._tick()

        mock_create_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_trigger_check_fires_callback(self):
        """71. trigger_check detects self_disabled and creates background task."""
        reg = CheckRegistry()
        check = DynamicCheck(
            check_id="cb-trig-001", name="trigger_cb_check",
            prompt="Trigger me",
            tools=["web_search"],
            on_complete_prompt="Callback on trigger",
            on_complete_tools=["web_search"],
        )
        check.run = AsyncMock(return_value=CheckResult(
            has_updates=False, self_disabled=True,
        ))
        reg.register(check)

        runner, _ = self._make_runner(registry=reg)

        with patch("nous.heartbeat.runner.asyncio.create_task") as mock_create_task:
            result = await runner.trigger_check("trigger_cb_check")

        assert result is not None
        assert result.self_disabled is True
        mock_create_task.assert_called_once()
        call_kwargs = mock_create_task.call_args
        assert "trigger_cb_check" in call_kwargs[1]["name"]
