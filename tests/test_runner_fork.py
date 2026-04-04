"""Tests for AgentRunner.fork() method."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from nous.api.runner import AgentRunner


def _mock_settings():
    settings = MagicMock()
    settings.tool_pruning_enabled = False
    settings.compaction_enabled = False
    settings.claim_verification_enabled = False
    settings.action_gating_enabled = False
    return settings


class TestAgentRunnerFork:

    def test_fork_creates_new_runner_with_given_client(self):
        """fork() returns a new AgentRunner using the provided API client."""
        cognitive = MagicMock()
        brain = MagicMock()
        heart = MagicMock()
        settings = _mock_settings()

        parent = AgentRunner(cognitive, brain, heart, settings)
        parent._dispatcher = MagicMock(name="dispatcher")

        api_client = AsyncMock(name="dedicated_client")
        child = parent.fork(api_client)

        assert child._api is api_client
        assert child._api_shared is True  # caller owns lifecycle
        assert child._dispatcher is parent._dispatcher
        assert child._cognitive is parent._cognitive
        assert child is not parent

    def test_fork_shares_cognitive_layer(self):
        """Forked runner shares the same cognitive layer instance."""
        cognitive = MagicMock()
        settings = _mock_settings()

        parent = AgentRunner(cognitive, MagicMock(), MagicMock(), settings)
        child = parent.fork(AsyncMock())

        assert child._cognitive is cognitive

    def test_fork_without_dispatcher(self):
        """fork() works even if parent has no dispatcher yet."""
        settings = _mock_settings()
        parent = AgentRunner(MagicMock(), MagicMock(), MagicMock(), settings)
        # dispatcher is None by default
        assert parent._dispatcher is None

        child = parent.fork(AsyncMock())
        assert child._dispatcher is None

    @pytest.mark.asyncio
    async def test_forked_runner_close_does_not_close_client(self):
        """close() on forked runner does not close the API client (api_shared=True)."""
        settings = _mock_settings()
        parent = AgentRunner(MagicMock(), MagicMock(), MagicMock(), settings)

        api_client = AsyncMock()
        child = parent.fork(api_client)

        await child.close()

        # Client should NOT have been closed — caller owns it
        api_client.close.assert_not_called()
        assert child._api is None  # but _api ref is cleared
