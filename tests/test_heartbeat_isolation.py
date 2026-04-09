"""Tests for heartbeat client isolation — verifying triage routing and cleanup."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from nous.heartbeat.registry import CheckRegistry
from nous.heartbeat.runner import HeartbeatRunner
from nous.heartbeat.schemas import Finding


def _mock_settings(**overrides):
    """Create mock settings for isolation tests."""
    defaults = {
        "agent_id": "test-agent",
        "heartbeat_enabled": True,
        "heartbeat_tick_interval": 30,
        "heartbeat_quiet_start": 23,
        "heartbeat_quiet_end": 8,
        "heartbeat_daily_token_budget": 50000,
        "heartbeat_digest_hour_utc": 9,
        "heartbeat_suppression_ttl_hours": 24,
        "heartbeat_tuning_enabled": False,
        "heartbeat_tuning_interval_hours": 168,
    }
    defaults.update(overrides)
    settings = MagicMock()
    for k, v in defaults.items():
        setattr(settings, k, v)
    return settings


class TestTriageRouting:
    """Verify _cognitive_triage routes to the correct runner."""

    @pytest.mark.asyncio
    async def test_triage_uses_dedicated_runner_when_available(self):
        """When api_client provided and start() called, triage uses dedicated runner."""
        settings = _mock_settings()

        shared_runner = AsyncMock()
        shared_runner.run_turn = AsyncMock(side_effect=AssertionError(
            "Shared runner should NOT be called when dedicated client exists"
        ))

        dedicated_runner = AsyncMock()
        dedicated_runner.run_turn = AsyncMock(return_value=(
            "OK", None, {"input_tokens": 50, "output_tokens": 50},
        ))
        dedicated_runner.end_conversation = AsyncMock()

        hb = HeartbeatRunner(
            settings=settings, registry=CheckRegistry(), runner=shared_runner,
            brain=AsyncMock(), heart=AsyncMock(), bus=None, http_client=None,
            api_client=AsyncMock(),
        )
        # Simulate what start() does
        hb._dedicated_runner = dedicated_runner

        findings = [
            Finding(source="health", summary="test", urgency="normal", needs_action=True),
        ]
        result = await hb._cognitive_triage(findings)

        assert result.tokens_used == 100
        dedicated_runner.run_turn.assert_called_once()
        shared_runner.run_turn.assert_not_called()

    @pytest.mark.asyncio
    async def test_triage_falls_back_to_shared_runner(self):
        """Without api_client, triage uses the shared runner (backward compat)."""
        settings = _mock_settings()

        shared_runner = AsyncMock()
        shared_runner.run_turn = AsyncMock(return_value=(
            "OK", None, {"input_tokens": 50, "output_tokens": 50},
        ))
        shared_runner.end_conversation = AsyncMock()

        hb = HeartbeatRunner(
            settings=settings, registry=CheckRegistry(), runner=shared_runner,
            brain=AsyncMock(), heart=AsyncMock(), bus=None, http_client=None,
        )

        findings = [
            Finding(source="health", summary="test", urgency="normal", needs_action=True),
        ]
        result = await hb._cognitive_triage(findings)

        assert result.tokens_used == 100
        shared_runner.run_turn.assert_called_once()

    def test_get_triage_runner_warns_when_dedicated_missing(self, caplog):
        """_get_triage_runner logs warning when api_client set but no dedicated runner."""
        settings = _mock_settings()
        hb = HeartbeatRunner(
            settings=settings, registry=CheckRegistry(), runner=AsyncMock(),
            brain=AsyncMock(), heart=AsyncMock(), bus=None, http_client=None,
            api_client=AsyncMock(),
        )
        # Don't call start() — _dedicated_runner stays None

        import logging
        with caplog.at_level(logging.WARNING, logger="nous.heartbeat.runner"):
            result = hb._get_triage_runner()

        assert result is hb._runner
        assert "dedicated runner not initialized" in caplog.text


class TestDedicatedRunnerCleanup:
    """Verify stop() properly cleans up dedicated runner and API client."""

    @pytest.mark.asyncio
    async def test_stop_closes_dedicated_runner(self):
        """stop() calls close() on dedicated runner."""
        settings = _mock_settings()

        dedicated_runner = AsyncMock()
        dedicated_runner.close = AsyncMock()

        api_client = AsyncMock()
        api_client.close = AsyncMock()

        hb = HeartbeatRunner(
            settings=settings, registry=CheckRegistry(), runner=AsyncMock(),
            brain=AsyncMock(), heart=AsyncMock(), bus=None, http_client=None,
            api_client=api_client,
        )
        hb._dedicated_runner = dedicated_runner
        hb._task = None  # No loop running

        await hb.stop()

        dedicated_runner.close.assert_called_once()
        assert hb._dedicated_runner is None

    @pytest.mark.asyncio
    async def test_stop_closes_api_client(self):
        """stop() explicitly closes the api_client (not left to runner)."""
        settings = _mock_settings()

        api_client = AsyncMock()
        api_client.close = AsyncMock()

        hb = HeartbeatRunner(
            settings=settings, registry=CheckRegistry(), runner=AsyncMock(),
            brain=AsyncMock(), heart=AsyncMock(), bus=None, http_client=None,
            api_client=api_client,
        )
        hb._dedicated_runner = AsyncMock()
        hb._task = None

        await hb.stop()

        api_client.close.assert_called_once()
        assert hb._api_client is None

    @pytest.mark.asyncio
    async def test_stop_without_api_client_is_unchanged(self):
        """stop() without api_client works like before (no dedicated cleanup)."""
        settings = _mock_settings()

        hb = HeartbeatRunner(
            settings=settings, registry=CheckRegistry(), runner=AsyncMock(),
            brain=AsyncMock(), heart=AsyncMock(), bus=None, http_client=None,
        )
        hb._task = None

        await hb.stop()  # Should not raise

        assert hb._dedicated_runner is None
        assert hb._api_client is None

    @pytest.mark.asyncio
    async def test_stop_closes_api_client_even_if_runner_close_fails(self):
        """api_client is still closed even if dedicated_runner.close() raises."""
        settings = _mock_settings()

        dedicated_runner = AsyncMock()
        dedicated_runner.close = AsyncMock(side_effect=RuntimeError("boom"))

        api_client = AsyncMock()
        api_client.close = AsyncMock()

        hb = HeartbeatRunner(
            settings=settings, registry=CheckRegistry(), runner=AsyncMock(),
            brain=AsyncMock(), heart=AsyncMock(), bus=None, http_client=None,
            api_client=api_client,
        )
        hb._dedicated_runner = dedicated_runner
        hb._task = None

        await hb.stop()  # Should not raise

        api_client.close.assert_called_once()
        assert hb._api_client is None
        assert hb._dedicated_runner is None


class TestStartIntegration:
    """Verify start() correctly wires the dedicated runner via fork()."""

    @pytest.mark.asyncio
    async def test_start_calls_fork_when_api_client_provided(self):
        """start() calls runner.fork(api_client) to create dedicated runner."""
        settings = _mock_settings()
        api_client = AsyncMock(name="heartbeat_client")

        shared_runner = MagicMock()
        forked_runner = MagicMock(name="forked_runner")
        shared_runner.fork = MagicMock(return_value=forked_runner)

        hb = HeartbeatRunner(
            settings=settings, registry=CheckRegistry(), runner=shared_runner,
            brain=AsyncMock(), heart=AsyncMock(), bus=None, http_client=None,
            api_client=api_client,
        )

        # Prevent the actual loop from running
        hb._detect_missed_checks = AsyncMock()

        await hb.start()

        # Verify fork was called with the api_client
        shared_runner.fork.assert_called_once_with(api_client)
        assert hb._dedicated_runner is forked_runner

        # Clean up
        hb._running = False
        if hb._task:
            hb._task.cancel()
            try:
                await hb._task
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_start_skips_fork_when_no_api_client(self):
        """start() does not call fork when no api_client provided."""
        settings = _mock_settings()

        shared_runner = MagicMock()
        shared_runner.fork = MagicMock()

        hb = HeartbeatRunner(
            settings=settings, registry=CheckRegistry(), runner=shared_runner,
            brain=AsyncMock(), heart=AsyncMock(), bus=None, http_client=None,
        )

        hb._detect_missed_checks = AsyncMock()

        await hb.start()

        shared_runner.fork.assert_not_called()
        assert hb._dedicated_runner is None

        hb._running = False
        if hb._task:
            hb._task.cancel()
            try:
                await hb._task
            except asyncio.CancelledError:
                pass


class TestBackwardCompatibility:
    """Verify existing behavior is preserved when no api_client is passed."""

    def test_api_client_defaults_to_none(self):
        """HeartbeatRunner api_client defaults to None."""
        settings = _mock_settings()
        hb = HeartbeatRunner(
            settings=settings, registry=CheckRegistry(), runner=AsyncMock(),
            brain=AsyncMock(), heart=AsyncMock(), bus=None, http_client=None,
        )
        assert hb._api_client is None
        assert hb._dedicated_runner is None
