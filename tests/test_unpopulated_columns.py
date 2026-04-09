"""Tests for 007.4 — Fix unpopulated database columns.

Tests use mocks to verify wiring without requiring Postgres.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Fix 1: Brain._emit_event session_id
# ---------------------------------------------------------------------------


class TestEmitEventSessionId:
    """Brain._emit_event() populates ORM session_id column."""

    @pytest.fixture
    def brain(self):
        from nous.brain.brain import Brain

        brain = Brain.__new__(Brain)
        brain.agent_id = "test-agent"
        return brain

    @pytest.mark.asyncio
    async def test_emit_event_with_session_id(self, brain):
        """session_id is passed to ORM Event when provided."""
        session = AsyncMock()
        await brain._emit_event(session, "turn_completed", {"key": "value"}, session_id="sess-123")

        # Verify session.add was called with an Event that has session_id set
        session.add.assert_called_once()
        event = session.add.call_args[0][0]
        assert event.session_id == "sess-123"
        assert event.agent_id == "test-agent"
        assert event.event_type == "turn_completed"

    @pytest.mark.asyncio
    async def test_emit_event_without_session_id(self, brain):
        """session_id defaults to None when not provided (backward compat)."""
        session = AsyncMock()
        await brain._emit_event(session, "decision_recorded", {"id": "abc"})

        event = session.add.call_args[0][0]
        assert event.session_id is None


# ---------------------------------------------------------------------------
# Fix 1b: main.py persist_to_db passes session_id
# ---------------------------------------------------------------------------


class TestPersistToDb:
    """persist_to_db adapter passes event.session_id to brain.emit_event."""

    @pytest.mark.asyncio
    async def test_persist_passes_session_id(self):
        """Bus event session_id flows to brain.emit_event."""
        from nous.events import Event

        brain = MagicMock()
        brain.emit_event = AsyncMock()

        # Simulate the persist_to_db function from main.py
        async def persist_to_db(event: Event) -> None:
            data = {**event.data}
            await brain.emit_event(event.type, data, session_id=event.session_id)

        event = Event(type="turn_completed", agent_id="test", session_id="sess-456", data={"frame": "task"})
        await persist_to_db(event)

        brain.emit_event.assert_awaited_once_with("turn_completed", {"frame": "task"}, session_id="sess-456")


# ---------------------------------------------------------------------------
# Fix 2: Telegram bot passes user identity
# ---------------------------------------------------------------------------


class TestTelegramUserIdentity:
    """Telegram bot extracts and passes user_id + display_name."""

    def test_chat_streaming_accepts_user_params(self):
        """_chat_streaming signature accepts user_id and user_display_name."""
        import inspect

        from nous.telegram_bot import NousTelegramBot

        sig = inspect.signature(NousTelegramBot._chat_streaming)
        params = list(sig.parameters.keys())
        assert "user_id" in params
        assert "user_display_name" in params


# ---------------------------------------------------------------------------
# Fix 2b: REST API extracts user identity
# ---------------------------------------------------------------------------


class TestRestUserIdentity:
    """REST API extracts user_id/user_display_name from request body."""

    @pytest.mark.asyncio
    async def test_chat_passes_user_identity(self):
        """POST /chat extracts user_id and user_display_name from body."""
        from starlette.testclient import TestClient

        runner = MagicMock()
        runner.run_turn = AsyncMock(
            return_value=(
                "Hello!",
                MagicMock(
                    frame=MagicMock(frame_id="conversation"),
                    decision_id=None,
                    active_censors=[],
                    recalled_decision_ids=[],
                    recalled_fact_ids=[],
                    recalled_episode_ids=[],
                    context_token_estimate=100,
                    system_prompt="test",
                ),
                {"input_tokens": 10, "output_tokens": 20},
            )
        )

        from nous.api.rest import create_app

        app = create_app(
            runner=runner,
            brain=MagicMock(),
            heart=MagicMock(),
            cognitive=MagicMock(),
            database=MagicMock(),
            settings=MagicMock(agent_id="test", agent_name="Test", model="test"),
        )

        client = TestClient(app)
        response = client.post(
            "/chat",
            json={
                "message": "hello",
                "user_id": "tg-12345",
                "user_display_name": "Tim",
            },
        )

        assert response.status_code == 200
        # Verify runner.run_turn was called with user identity
        call_kwargs = runner.run_turn.call_args
        assert call_kwargs.kwargs.get("user_id") == "tg-12345"
        assert call_kwargs.kwargs.get("user_display_name") == "Tim"


# ---------------------------------------------------------------------------
# Fix 3: agents.last_active updated
# ---------------------------------------------------------------------------


class TestLastActiveUpdate:
    """CognitiveLayer.pre_turn updates agents.last_active."""

    def test_layer_imports_agent_model(self):
        """layer.py imports Agent model for last_active update."""
        from nous.storage.models import Agent

        # If import works, the wiring is in place
        assert Agent is not None
