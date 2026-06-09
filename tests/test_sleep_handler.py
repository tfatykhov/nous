"""Tests for sleep handler fixes (#173) and manual trigger endpoint.

Covers:
- Phase bool returns (success, failure, no-op)
- phases_completed accuracy (failed phase excluded)
- sleep_completed event includes facts_created, procedures_created, censors_retired
- is_sleeping property
- exc_info=True on warning logs
- POST /sleep/trigger endpoint (200, 409, event emission)
"""

from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from nous.events import Event, EventBus
from nous.heart.schemas import FactInput


# ---------------------------------------------------------------------------
# Helpers (same patterns as test_event_bus.py)
# ---------------------------------------------------------------------------


def _make_event(
    event_type: str = "test_event",
    agent_id: str = "test-agent",
    data: dict | None = None,
    session_id: str | None = "sess-1",
) -> Event:
    return Event(
        type=event_type,
        agent_id=agent_id,
        data=data or {},
        session_id=session_id,
    )


def _mock_settings(**overrides) -> MagicMock:
    s = MagicMock()
    s.background_model = "claude-sonnet-4-5-20250514"
    s.anthropic_api_key = "sk-ant-test-key"
    s.anthropic_auth_token = ""
    s.agent_id = "test-agent"
    s.sleep_enabled = True
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


def _mock_llm_client(text: str = "", status_code: int = 200) -> AsyncMock:
    """Create a mock LLM client that returns text response (legacy format)."""
    client = AsyncMock()
    if status_code == 200:
        response = MagicMock()
        response.content = [{"type": "text", "text": text}]
        client.call = AsyncMock(return_value=response)
    else:
        client.call = AsyncMock(side_effect=RuntimeError(f"API error ({status_code})"))
    return client


def _mock_llm_client_structured(tool_input: dict, tool_name: str = "store_reflection") -> AsyncMock:
    """Create a mock LLM client that returns tool_use structured response."""
    client = AsyncMock()
    response = MagicMock()
    response.content = [
        {"type": "tool_use", "id": "toolu_mock", "name": tool_name, "input": tool_input}
    ]
    client.call = AsyncMock(return_value=response)
    return client


def _make_sleep_handler(brain=None, heart=None, settings=None, bus=None, llm_client=None):
    from nous.handlers.sleep_handler import SleepHandler

    brain = brain or AsyncMock()
    heart = heart or AsyncMock()
    settings = settings or _mock_settings()
    bus = bus or MagicMock(spec=EventBus)
    bus.on = MagicMock()
    bus.emit = AsyncMock()
    llm_client = llm_client or _mock_llm_client()
    handler = SleepHandler(brain, heart, settings, bus, llm_client)
    return handler, brain, heart, bus, llm_client


# ===========================================================================
# TestPhaseBoolReturns
# ===========================================================================


class TestPhaseBoolReturns:
    """Phase methods return True on success/no-op, False on exception."""

    @pytest.mark.asyncio
    async def test_review_returns_true_on_success(self):
        handler, brain, heart, bus, _ = _make_sleep_handler()
        brain.list_decisions = AsyncMock(return_value=([], 0))
        result = await handler._phase_review_decisions()
        assert result is True

    @pytest.mark.asyncio
    async def test_review_returns_false_on_exception(self):
        handler, brain, heart, bus, _ = _make_sleep_handler()
        brain.list_decisions = AsyncMock(side_effect=RuntimeError("db error"))
        result = await handler._phase_review_decisions()
        assert result is False

    @pytest.mark.asyncio
    async def test_prune_returns_true(self):
        handler, *_ = _make_sleep_handler()
        result = await handler._phase_prune()
        assert result is True

    @pytest.mark.asyncio
    async def test_compress_returns_true_no_llm(self):
        """No-op (no LLM client) returns True, not False."""
        handler, *_ = _make_sleep_handler(llm_client=None)
        # Remove LLM client
        handler._llm = None
        result = await handler._phase_compress()
        assert result is True

    @pytest.mark.asyncio
    async def test_compress_returns_true_with_llm(self):
        handler, *_ = _make_sleep_handler()
        result = await handler._phase_compress()
        assert result is True

    @pytest.mark.asyncio
    async def test_reflect_returns_true_no_llm(self):
        """No-op (no LLM client) returns True."""
        handler, *_ = _make_sleep_handler()
        handler._llm = None
        sleep_stats = {"facts_created": 0, "procedures_created": 0, "censors_retired": 0}
        result = await handler._phase_reflect(sleep_stats)
        assert result is True

    @pytest.mark.asyncio
    async def test_reflect_returns_true_few_episodes(self):
        """No-op (not enough episodes) returns True."""
        handler, brain, heart, bus, _ = _make_sleep_handler()
        ep1 = MagicMock()
        ep1.summary = "Single episode"
        heart.list_episodes = AsyncMock(return_value=[ep1])
        sleep_stats = {"facts_created": 0, "procedures_created": 0, "censors_retired": 0}
        result = await handler._phase_reflect(sleep_stats)
        assert result is True

    @pytest.mark.asyncio
    async def test_reflect_returns_false_on_exception(self):
        handler, brain, heart, bus, llm_client = _make_sleep_handler()
        # Make list_episodes raise to trigger the except block
        heart.list_episodes = AsyncMock(side_effect=RuntimeError("db error"))
        sleep_stats = {"facts_created": 0, "procedures_created": 0, "censors_retired": 0}
        result = await handler._phase_reflect(sleep_stats)
        assert result is False

    @pytest.mark.asyncio
    async def test_generalize_returns_true_no_learner(self):
        """No procedure learner = no-op = True."""
        handler, *_ = _make_sleep_handler()
        handler._procedure_learner = None
        sleep_stats = {"facts_created": 0, "procedures_created": 0, "censors_retired": 0}
        result = await handler._phase_generalize(sleep_stats)
        assert result is True

    @pytest.mark.asyncio
    async def test_generalize_returns_true_on_success(self):
        handler, *_ = _make_sleep_handler()
        handler._procedure_learner = AsyncMock()
        handler._procedure_learner.run_sleep_learning = AsyncMock(
            return_value={"decisions_learned": 2, "episodes_learned": 1, "weak_reviewed": 0}
        )
        sleep_stats = {"facts_created": 0, "procedures_created": 0, "censors_retired": 0}
        result = await handler._phase_generalize(sleep_stats)
        assert result is True

    @pytest.mark.asyncio
    async def test_generalize_returns_false_on_exception(self):
        handler, *_ = _make_sleep_handler()
        handler._procedure_learner = AsyncMock()
        handler._procedure_learner.run_sleep_learning = AsyncMock(
            side_effect=RuntimeError("learner crash")
        )
        sleep_stats = {"facts_created": 0, "procedures_created": 0, "censors_retired": 0}
        result = await handler._phase_generalize(sleep_stats)
        assert result is False


# ===========================================================================
# TestPhasesCompleted
# ===========================================================================


class TestPhasesCompleted:
    """phases_completed only includes phases that returned True."""

    @pytest.mark.asyncio
    async def test_failed_phase_excluded_from_phases_completed(self):
        handler, brain, heart, bus, _ = _make_sleep_handler()
        # review succeeds, prune fails, rest succeed
        handler._phase_review_decisions = AsyncMock(return_value=True)
        handler._phase_prune = AsyncMock(return_value=False)
        handler._phase_compress = AsyncMock(return_value=True)
        handler._phase_reflect = AsyncMock(return_value=True)
        handler._phase_resolve_contradictions = AsyncMock(return_value=True)
        handler._phase_graph_densification = AsyncMock(return_value=True)
        handler._phase_recover_abandoned_episodes = AsyncMock(return_value=True)
        handler._phase_generalize = AsyncMock(return_value=True)
        handler._phase_evolve_rubric = AsyncMock(return_value=True)

        await handler._run_sleep(_make_event("sleep_started"))

        emitted = bus.emit.call_args[0][0]
        assert "review" in emitted.data["phases_completed"]
        assert "prune" not in emitted.data["phases_completed"]
        assert "compress" in emitted.data["phases_completed"]
        assert "reflect" in emitted.data["phases_completed"]
        assert "resolve_contradictions" in emitted.data["phases_completed"]
        assert "graph_densification" in emitted.data["phases_completed"]
        assert "recover_abandoned_episodes" in emitted.data["phases_completed"]
        assert "generalize" in emitted.data["phases_completed"]
        # F065 Phase 2: prune_hub_snapshots runs after prune_dead_edges
        # and is included when graph_hub_snapshot_retention_days > 0 (default).
        assert "prune_hub_snapshots" in emitted.data["phases_completed"]
        assert len(emitted.data["phases_completed"]) == 9

    @pytest.mark.asyncio
    async def test_all_phases_succeed_all_in_phases_completed(self):
        handler, brain, heart, bus, _ = _make_sleep_handler()
        handler._phase_review_decisions = AsyncMock(return_value=True)
        handler._phase_prune = AsyncMock(return_value=True)
        handler._phase_compress = AsyncMock(return_value=True)
        handler._phase_reflect = AsyncMock(return_value=True)
        handler._phase_resolve_contradictions = AsyncMock(return_value=True)
        handler._phase_graph_densification = AsyncMock(return_value=True)
        handler._phase_recover_abandoned_episodes = AsyncMock(return_value=True)
        handler._phase_generalize = AsyncMock(return_value=True)
        handler._phase_evolve_rubric = AsyncMock(return_value=True)
        handler._phase_prune_hub_snapshots = AsyncMock(return_value=True)  # F065 Phase 2

        await handler._run_sleep(_make_event("sleep_started"))

        emitted = bus.emit.call_args[0][0]
        # F065 Phase 2 adds prune_hub_snapshots → 10 phases.
        assert len(emitted.data["phases_completed"]) == 10


# ===========================================================================
# TestSleepStats
# ===========================================================================


class TestSleepStats:
    """sleep_completed event includes facts_created, procedures_created, censors_retired."""

    @pytest.mark.asyncio
    async def test_sleep_completed_includes_stats_keys(self):
        handler, brain, heart, bus, _ = _make_sleep_handler()
        handler._phase_review_decisions = AsyncMock(return_value=True)
        handler._phase_prune = AsyncMock(return_value=True)
        handler._phase_compress = AsyncMock(return_value=True)
        handler._phase_reflect = AsyncMock(return_value=True)
        handler._phase_generalize = AsyncMock(return_value=True)

        await handler._run_sleep(_make_event("sleep_started"))

        emitted = bus.emit.call_args[0][0]
        assert "facts_created" in emitted.data
        assert "procedures_created" in emitted.data
        assert "censors_retired" in emitted.data
        assert emitted.data["facts_created"] == 0
        assert emitted.data["procedures_created"] == 0
        assert emitted.data["censors_retired"] == 0

    @pytest.mark.asyncio
    async def test_reflect_increments_facts_created(self):
        handler, brain, heart, bus, llm_client = _make_sleep_handler()

        ep1 = MagicMock()
        ep1.summary = "Episode about Python testing"
        ep2 = MagicMock()
        ep2.summary = "Episode about async patterns"
        heart.list_episodes = AsyncMock(return_value=[ep1, ep2])
        # learn returns a mock (not FactRejected) — counts as stored
        heart.learn = AsyncMock(return_value=MagicMock())
        heart.search_facts = AsyncMock(return_value=[])

        reflection_json = {
            "patterns": [], "lessons": [], "connections": [], "gaps": [],
            "summary": "Testing patterns",
            "facts": [
                {"subject": "testing", "content": "Use pytest", "category": "tool"},
                {"subject": "async", "content": "Use asyncio", "category": "technical"},
            ],
        }
        llm_client.call = AsyncMock(return_value=MagicMock(
            content=[{"type": "tool_use", "id": "toolu_1", "name": "store_reflection", "input": reflection_json}]
        ))

        sleep_stats = {"facts_created": 0, "procedures_created": 0, "censors_retired": 0}
        await handler._phase_reflect(sleep_stats)

        assert sleep_stats["facts_created"] == 2

    @pytest.mark.asyncio
    async def test_generalize_increments_procedures_created(self):
        handler, *_ = _make_sleep_handler()
        handler._procedure_learner = AsyncMock()
        handler._procedure_learner.run_sleep_learning = AsyncMock(
            return_value={"decisions_learned": 3, "episodes_learned": 1, "weak_reviewed": 0}
        )

        sleep_stats = {"facts_created": 0, "procedures_created": 0, "censors_retired": 0}
        await handler._phase_generalize(sleep_stats)

        # After #188 fix: counts decisions_learned (3) + episodes_learned (1) = 4
        assert sleep_stats["procedures_created"] == 4

    @pytest.mark.asyncio
    async def test_full_sleep_propagates_stats_to_event(self):
        """End-to-end: reflect creates facts, generalize creates procedures, event has both."""
        handler, brain, heart, bus, llm_client = _make_sleep_handler()

        handler._phase_review_decisions = AsyncMock(return_value=True)
        handler._phase_prune = AsyncMock(return_value=True)
        handler._phase_compress = AsyncMock(return_value=True)

        # Reflect: will store 1 fact
        ep1 = MagicMock()
        ep1.summary = "Episode 1"
        ep2 = MagicMock()
        ep2.summary = "Episode 2"
        heart.list_episodes = AsyncMock(return_value=[ep1, ep2])
        heart.learn = AsyncMock(return_value=MagicMock())
        heart.search_facts = AsyncMock(return_value=[])
        reflection_json = {
            "patterns": [], "lessons": [], "connections": [], "gaps": [],
            "summary": "Reflection",
            "facts": [{"subject": "x", "content": "y", "category": "concept"}],
        }
        llm_client.call = AsyncMock(return_value=MagicMock(
            content=[{"type": "tool_use", "id": "toolu_1", "name": "store_reflection", "input": reflection_json}]
        ))

        # Generalize: will create 2 procedures
        handler._procedure_learner = AsyncMock()
        handler._procedure_learner.run_sleep_learning = AsyncMock(
            return_value={"decisions_learned": 2, "episodes_learned": 0, "weak_reviewed": 0}
        )

        await handler._run_sleep(_make_event("sleep_started"))

        emitted = bus.emit.call_args[0][0]
        assert emitted.data["facts_created"] == 1
        assert emitted.data["procedures_created"] == 2
        assert emitted.data["censors_retired"] == 0


# ===========================================================================
# TestIsSleeping
# ===========================================================================


class TestIsSleeping:
    """is_sleeping property."""

    def test_is_sleeping_false_initially(self):
        handler, *_ = _make_sleep_handler()
        assert handler.is_sleeping is False

    @pytest.mark.asyncio
    async def test_is_sleeping_true_during_sleep(self):
        handler, brain, heart, bus, _ = _make_sleep_handler()
        handler._phase_review_decisions = AsyncMock(return_value=True)
        handler._phase_prune = AsyncMock(return_value=True)
        handler._phase_compress = AsyncMock(return_value=True)
        handler._phase_reflect = AsyncMock(return_value=True)
        handler._phase_generalize = AsyncMock(return_value=True)

        # Manually set sleeping state
        handler._sleeping = True
        assert handler.is_sleeping is True

    @pytest.mark.asyncio
    async def test_is_sleeping_false_after_sleep(self):
        handler, brain, heart, bus, _ = _make_sleep_handler()
        handler._phase_review_decisions = AsyncMock(return_value=True)
        handler._phase_prune = AsyncMock(return_value=True)
        handler._phase_compress = AsyncMock(return_value=True)
        handler._phase_reflect = AsyncMock(return_value=True)
        handler._phase_generalize = AsyncMock(return_value=True)

        await handler._run_sleep(_make_event("sleep_started"))
        assert handler.is_sleeping is False


# ===========================================================================
# TestExcInfo
# ===========================================================================


class TestExcInfo:
    """Exception handlers use exc_info=True for tracebacks."""

    @pytest.mark.asyncio
    async def test_review_logs_with_exc_info(self, caplog):
        handler, brain, *_ = _make_sleep_handler()
        brain.list_decisions = AsyncMock(side_effect=RuntimeError("db error"))

        with caplog.at_level(logging.WARNING, logger="nous.handlers.sleep_handler"):
            await handler._phase_review_decisions()

        assert "Decision review phase failed" in caplog.text
        # Verify exc_info was used — traceback should be in the log
        assert "RuntimeError: db error" in caplog.text

    @pytest.mark.asyncio
    async def test_prune_logs_with_exc_info(self, caplog):
        """Prune is a stub so it's hard to make it fail — but we test the structure."""
        # The prune phase is a stub that just logs debug, so it won't fail normally.
        # We verify the code path exists by checking the handler has the right pattern.
        from nous.handlers.sleep_handler import SleepHandler
        import inspect
        source = inspect.getsource(SleepHandler._phase_prune)
        assert "exc_info=True" in source

    @pytest.mark.asyncio
    async def test_compress_logs_with_exc_info(self):
        from nous.handlers.sleep_handler import SleepHandler
        import inspect
        source = inspect.getsource(SleepHandler._phase_compress)
        assert "exc_info=True" in source

    @pytest.mark.asyncio
    async def test_reflect_logs_with_exc_info(self, caplog):
        handler, brain, heart, bus, llm_client = _make_sleep_handler()
        # Make list_episodes raise to trigger the except block directly
        heart.list_episodes = AsyncMock(side_effect=RuntimeError("db error"))

        sleep_stats = {"facts_created": 0, "procedures_created": 0, "censors_retired": 0}
        with caplog.at_level(logging.WARNING, logger="nous.handlers.sleep_handler"):
            await handler._phase_reflect(sleep_stats)

        assert "Reflection phase failed" in caplog.text
        assert "RuntimeError: db error" in caplog.text

    @pytest.mark.asyncio
    async def test_generalize_logs_with_exc_info(self, caplog):
        handler, *_ = _make_sleep_handler()
        handler._procedure_learner = AsyncMock()
        handler._procedure_learner.run_sleep_learning = AsyncMock(
            side_effect=RuntimeError("learner error")
        )

        sleep_stats = {"facts_created": 0, "procedures_created": 0, "censors_retired": 0}
        with caplog.at_level(logging.WARNING, logger="nous.handlers.sleep_handler"):
            await handler._phase_generalize(sleep_stats)

        assert "Generalize phase" in caplog.text
        assert "RuntimeError: learner error" in caplog.text

    @pytest.mark.asyncio
    async def test_reflect_exception_simplified(self):
        """Verify the except clause is 'except Exception:' not 'except (json.JSONDecodeError, Exception):'."""
        from nous.handlers.sleep_handler import SleepHandler
        import inspect
        source = inspect.getsource(SleepHandler._phase_reflect)
        assert "json.JSONDecodeError" not in source


# ===========================================================================
# TestSleepTriggerEndpoint
# ===========================================================================


class TestSleepTriggerEndpoint:
    """POST /sleep/trigger endpoint tests."""

    def _make_app(self, sleep_handler=None, bus=None):
        from starlette.testclient import TestClient

        from nous.api.rest import create_app

        runner = MagicMock()
        runner._conversations = {}
        brain = AsyncMock()
        heart = AsyncMock()
        cognitive = AsyncMock()
        database = AsyncMock()
        settings = _mock_settings()

        bus = bus or MagicMock(spec=EventBus)
        bus.emit = AsyncMock()

        app = create_app(
            runner=runner,
            brain=brain,
            heart=heart,
            cognitive=cognitive,
            database=database,
            settings=settings,
            bus=bus,
            sleep_handler=sleep_handler,
        )
        return TestClient(app), bus, settings

    def test_trigger_returns_200(self):
        sleep_handler = MagicMock()
        sleep_handler.is_sleeping = False
        client, bus, settings = self._make_app(sleep_handler=sleep_handler)

        response = client.post("/sleep/trigger")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "started"
        assert data["message"] == "Sleep cycle triggered"

    def test_trigger_returns_409_if_already_sleeping(self):
        sleep_handler = MagicMock()
        sleep_handler.is_sleeping = True
        client, bus, settings = self._make_app(sleep_handler=sleep_handler)

        response = client.post("/sleep/trigger")
        assert response.status_code == 409
        assert "already in progress" in response.json()["error"]

    def test_trigger_emits_sleep_started_with_manual_flag(self):
        sleep_handler = MagicMock()
        sleep_handler.is_sleeping = False
        bus = MagicMock(spec=EventBus)
        bus.emit = AsyncMock()
        client, bus, settings = self._make_app(sleep_handler=sleep_handler, bus=bus)

        client.post("/sleep/trigger")

        bus.emit.assert_called_once()
        emitted = bus.emit.call_args[0][0]
        assert emitted.type == "sleep_started"
        assert emitted.data["manual"] is True
        assert emitted.agent_id == settings.agent_id

    def test_trigger_returns_503_if_no_sleep_handler(self):
        client, _, _ = self._make_app(sleep_handler=None)

        response = client.post("/sleep/trigger")
        assert response.status_code == 503


# ===========================================================================
# Regression: Issue #188
# ===========================================================================


class TestProcedureStatsCountBothPathways:
    """Regression #188 Bug 2: procedures_created must count both pathways."""

    @pytest.mark.asyncio
    async def test_procedures_created_includes_episodes(self):
        """procedures_created must sum decisions_learned + episodes_learned."""
        handler, brain, heart, bus, _ = _make_sleep_handler()
        handler._phase_review_decisions = AsyncMock(return_value=True)
        handler._phase_prune = AsyncMock(return_value=True)
        handler._phase_compress = AsyncMock(return_value=True)
        handler._phase_reflect = AsyncMock(return_value=True)

        handler._procedure_learner = AsyncMock()
        handler._procedure_learner.run_sleep_learning = AsyncMock(
            return_value={"decisions_learned": 2, "episodes_learned": 3, "weak_reviewed": 1}
        )

        await handler._run_sleep(_make_event("sleep_started"))

        emitted = bus.emit.call_args[0][0]
        # Must be 2 + 3 = 5, not just 2
        assert emitted.data["procedures_created"] == 5


# ===========================================================================
# TestStructuredReflection (Issue #233)
# ===========================================================================


class TestStructuredReflection:
    """Issue #233: _phase_reflect uses tool_use trick for structured output."""

    @pytest.mark.asyncio
    async def test_reflect_calls_structured_llm(self):
        """_phase_reflect should use call_background_llm_structured, not call_background_llm."""
        handler, brain, heart, bus, llm_client = _make_sleep_handler()

        ep1 = MagicMock()
        ep1.summary = "Episode about Python testing"
        ep2 = MagicMock()
        ep2.summary = "Episode about async patterns"
        heart.list_episodes = AsyncMock(return_value=[ep1, ep2])
        heart.learn = AsyncMock(return_value=MagicMock())
        heart.search_facts = AsyncMock(return_value=[])

        # Mock the structured response (tool_use block)
        reflection = {
            "patterns": ["Pattern 1"], "lessons": ["Lesson 1"],
            "connections": [], "gaps": [],
            "summary": "Test reflection",
            "facts": [{"subject": "test", "content": "Test fact", "category": "technical"}],
        }
        response = MagicMock()
        response.content = [
            {"type": "tool_use", "id": "toolu_1", "name": "store_reflection", "input": reflection}
        ]
        llm_client.call = AsyncMock(return_value=response)

        sleep_stats = {"facts_created": 0, "procedures_created": 0, "censors_retired": 0}
        result = await handler._phase_reflect(sleep_stats)

        assert result is True
        assert sleep_stats["facts_created"] == 1
        # Verify tool_choice was in the payload
        payload = llm_client.call.call_args[0][0]
        assert "tool_choice" in payload
        assert payload["tool_choice"]["name"] == "store_reflection"

    @pytest.mark.asyncio
    async def test_reflect_stores_patterns_and_lessons(self):
        """Patterns and lessons from structured output are logged correctly."""
        handler, brain, heart, bus, llm_client = _make_sleep_handler()

        ep1 = MagicMock()
        ep1.summary = "Episode 1 about testing"
        ep2 = MagicMock()
        ep2.summary = "Episode 2 about debugging"
        heart.list_episodes = AsyncMock(return_value=[ep1, ep2])
        heart.learn = AsyncMock(return_value=MagicMock())
        heart.search_facts = AsyncMock(return_value=[])

        reflection = {
            "patterns": ["Users ask about testing frequently", "Debugging sessions are long"],
            "lessons": ["Pytest fixtures save time", "Async debugging is harder"],
            "connections": ["Testing and debugging are related"],
            "gaps": ["Need better error messages"],
            "summary": "Productive day of testing and debugging",
            "facts": [],
        }
        response = MagicMock()
        response.content = [
            {"type": "tool_use", "id": "toolu_1", "name": "store_reflection", "input": reflection}
        ]
        llm_client.call = AsyncMock(return_value=response)

        sleep_stats = {"facts_created": 0, "procedures_created": 0, "censors_retired": 0}
        result = await handler._phase_reflect(sleep_stats)

        assert result is True

    @pytest.mark.asyncio
    async def test_reflect_handles_structured_call_failure(self):
        """If structured LLM call returns None, reflect returns True (no-op)."""
        handler, brain, heart, bus, llm_client = _make_sleep_handler()

        ep1 = MagicMock()
        ep1.summary = "Episode 1"
        ep2 = MagicMock()
        ep2.summary = "Episode 2"
        heart.list_episodes = AsyncMock(return_value=[ep1, ep2])
        heart.search_facts = AsyncMock(return_value=[])

        # API error -> None from structured call
        llm_client.call = AsyncMock(side_effect=RuntimeError("API error"))

        sleep_stats = {"facts_created": 0, "procedures_created": 0, "censors_retired": 0}
        result = await handler._phase_reflect(sleep_stats)

        assert result is True
        assert sleep_stats["facts_created"] == 0

    @pytest.mark.asyncio
    async def test_reflect_fallback_stores_summary_when_no_facts(self):
        """When structured output has no facts, summary and lessons are stored as facts."""
        handler, brain, heart, bus, llm_client = _make_sleep_handler()

        ep1 = MagicMock()
        ep1.summary = "Episode about X"
        ep2 = MagicMock()
        ep2.summary = "Episode about Y"
        heart.list_episodes = AsyncMock(return_value=[ep1, ep2])
        heart.learn = AsyncMock(return_value=MagicMock())
        heart.search_facts = AsyncMock(return_value=[])

        reflection = {
            "patterns": ["P1"], "lessons": ["L1", "L2"],
            "connections": [], "gaps": [],
            "summary": "Day summary here",
            "facts": [],
        }
        response = MagicMock()
        response.content = [
            {"type": "tool_use", "id": "toolu_1", "name": "store_reflection", "input": reflection}
        ]
        llm_client.call = AsyncMock(return_value=response)

        sleep_stats = {"facts_created": 0, "procedures_created": 0, "censors_retired": 0}
        await handler._phase_reflect(sleep_stats)

        # summary (1) + 2 lessons = 3 facts stored
        assert sleep_stats["facts_created"] == 3

    @pytest.mark.asyncio
    async def test_reflect_updates_prefix_still_works(self):
        """UPDATES: prefix in structured output still triggers supersession."""
        handler, brain, heart, bus, llm_client = _make_sleep_handler()

        ep1 = MagicMock()
        ep1.summary = "Episode 1"
        ep2 = MagicMock()
        ep2.summary = "Episode 2"
        heart.list_episodes = AsyncMock(return_value=[ep1, ep2])
        heart.learn = AsyncMock(return_value=MagicMock())

        existing_fact = MagicMock()
        existing_fact.id = "fact-123"
        existing_fact.subject = "old subject"
        existing_fact.category = "technical"
        existing_fact.score = 0.95
        heart.search_facts = AsyncMock(return_value=[existing_fact])
        # HD-2: UPDATES supersession now probes find_similar_facts (raw cosine).
        heart.find_similar_facts = AsyncMock(return_value=[existing_fact])
        heart.supersede_fact = AsyncMock()

        reflection = {
            "patterns": [], "lessons": [], "connections": [], "gaps": [],
            "summary": "Updated knowledge",
            "facts": [
                {"subject": "UPDATES: old fact content", "content": "New updated content", "category": "technical"}
            ],
        }
        response = MagicMock()
        response.content = [
            {"type": "tool_use", "id": "toolu_1", "name": "store_reflection", "input": reflection}
        ]
        llm_client.call = AsyncMock(return_value=response)

        sleep_stats = {"facts_created": 0, "procedures_created": 0, "censors_retired": 0}
        await handler._phase_reflect(sleep_stats)

        heart.supersede_fact.assert_called_once()

    @pytest.mark.asyncio
    async def test_reflect_zero_results_logs_warning(self, caplog):
        """0 stored facts from a non-empty reflection should log WARNING, not INFO."""
        handler, brain, heart, bus, llm_client = _make_sleep_handler()

        ep1 = MagicMock()
        ep1.summary = "Episode 1"
        ep2 = MagicMock()
        ep2.summary = "Episode 2"
        heart.list_episodes = AsyncMock(return_value=[ep1, ep2])
        heart.search_facts = AsyncMock(return_value=[])

        # All facts rejected by admission control
        from nous.heart.schemas import FactRejected
        heart.learn = AsyncMock(return_value=FactRejected(
            content="y", composite_score=0.1, threshold=0.5,
            scores={"novelty": 0.1}, explanation="below threshold",
        ))

        reflection = {
            "patterns": ["P1", "P2"], "lessons": ["L1"],
            "connections": [], "gaps": [],
            "summary": "Good day",
            "facts": [{"subject": "x", "content": "y", "category": "concept"}],
        }
        response = MagicMock()
        response.content = [
            {"type": "tool_use", "id": "toolu_1", "name": "store_reflection", "input": reflection}
        ]
        llm_client.call = AsyncMock(return_value=response)

        sleep_stats = {"facts_created": 0, "procedures_created": 0, "censors_retired": 0}
        with caplog.at_level(logging.WARNING, logger="nous.handlers.sleep_handler"):
            await handler._phase_reflect(sleep_stats)

        assert any("0 facts stored" in r.message and r.levelno == logging.WARNING for r in caplog.records)


# ===========================================================================
# TestStructuredContradictionResolution (Issue #233)
# ===========================================================================


class TestStructuredContradictionResolution:
    """Issue #233: _phase_resolve_contradictions uses tool_use structured output."""

    @pytest.mark.asyncio
    async def test_contradiction_uses_structured_call(self):
        """Contradiction resolution should use call_background_llm_structured."""
        handler, brain, heart, bus, llm_client = _make_sleep_handler()

        heart.find_contradiction_candidates = AsyncMock(return_value=[
            {
                "fact1_id": "f1", "fact2_id": "f2",
                "content1": "Python is slow", "content2": "Python is fast",
                "date1": "2026-01-01", "date2": "2026-02-01",
                "subject": "python", "category": "technical",
            }
        ])
        heart.deactivate_fact = AsyncMock()

        resolution = {"action": "SUPERSEDE_A", "confidence": 0.9, "reason": "Newer info"}
        response = MagicMock()
        response.content = [
            {"type": "tool_use", "id": "toolu_1", "name": "resolve_contradiction", "input": resolution}
        ]
        llm_client.call = AsyncMock(return_value=response)

        sleep_stats = {"facts_created": 0, "procedures_created": 0, "censors_retired": 0}
        result = await handler._phase_resolve_contradictions(sleep_stats)

        assert result is True
        assert sleep_stats["contradictions_resolved"] == 1
        heart.deactivate_fact.assert_called_once_with("f1")

        # Verify tool_choice in payload
        payload = llm_client.call.call_args[0][0]
        assert "tool_choice" in payload

    @pytest.mark.asyncio
    async def test_contradiction_structured_failure_skips_pair(self):
        """If structured call returns None for a pair, skip it gracefully."""
        handler, brain, heart, bus, llm_client = _make_sleep_handler()

        heart.find_contradiction_candidates = AsyncMock(return_value=[
            {
                "fact1_id": "f1", "fact2_id": "f2",
                "content1": "A", "content2": "B",
                "date1": "2026-01-01", "date2": "2026-02-01",
            }
        ])

        llm_client.call = AsyncMock(side_effect=RuntimeError("API error"))

        sleep_stats = {"facts_created": 0, "procedures_created": 0, "censors_retired": 0}
        result = await handler._phase_resolve_contradictions(sleep_stats)

        assert result is True
        assert sleep_stats.get("contradictions_resolved", 0) == 0

    @pytest.mark.asyncio
    async def test_low_conf_merge_without_content_sets_both_flags(self):
        """Codex P2 follow-up to #396: a verdict that hits BOTH the
        confidence floor (<0.7) AND missing merged_content must set
        BOTH flags. The pre-fix logic mutated `action` to KEEP_BOTH on
        the floor check first, then the missing-content check failed
        because action was no longer MERGE.
        """
        handler, brain, heart, bus, llm_client = _make_sleep_handler()

        captured_events: list[tuple[str, dict]] = []

        async def capture_emit(event_type, data, **kwargs):
            captured_events.append((event_type, data))

        brain.emit_event = capture_emit

        heart.find_contradiction_candidates = AsyncMock(return_value=[
            {
                "fact1_id": "f1", "fact2_id": "f2",
                "content1": "X", "content2": "Y",
                "date1": "2026-01-01", "date2": "2026-02-01",
                "subject": "s", "category": "c",
            }
        ])
        heart.deactivate_fact = AsyncMock()
        heart.learn = AsyncMock()

        # Both: low confidence (0.5 < 0.7) AND missing merged_content.
        resolution = {
            "action": "MERGE", "confidence": 0.5,
            "reason": "Both apply but unsure",
            # merged_content omitted
        }
        response = MagicMock()
        response.content = [
            {"type": "tool_use", "id": "x", "name": "resolve_contradiction",
             "input": resolution}
        ]
        llm_client.call = AsyncMock(return_value=response)

        sleep_stats = {"facts_created": 0, "procedures_created": 0,
                       "censors_retired": 0}
        await handler._phase_resolve_contradictions(sleep_stats)
        import asyncio as _aio
        await _aio.sleep(0)

        assert captured_events
        _, data = captured_events[0]
        assert data["downgraded_by_floor"] is True, (
            "0.5 < 0.7 floor — must be flagged"
        )
        assert data["downgraded_due_to_missing_content"] is True, (
            "MERGE without merged_content — must be flagged regardless of "
            "the floor downgrade order"
        )
        assert data["raw_action"] == "MERGE"
        assert data["applied_action"] == "KEEP_BOTH"

    @pytest.mark.asyncio
    async def test_persistence_flags_distinguish_downgrade_reasons(self):
        """Codex P2 follow-up to #393: `downgraded_by_floor` must reflect
        confidence-floor downgrades only; missing-merged_content gets its
        own `downgraded_due_to_missing_content` flag in the persistence
        event so eval queries can disambiguate the two."""
        handler, brain, heart, bus, llm_client = _make_sleep_handler()

        captured_events: list[tuple[str, dict]] = []

        async def capture_emit(event_type, data, **kwargs):
            captured_events.append((event_type, data))

        brain.emit_event = capture_emit

        heart.find_contradiction_candidates = AsyncMock(return_value=[
            {
                "fact1_id": "fA", "fact2_id": "fB",
                "content1": "Pi=3.14", "content2": "Pi=4",
                "date1": "2026-01-01", "date2": "2026-02-01",
                "subject": "math", "category": "concept",
            }
        ])
        heart.deactivate_fact = AsyncMock()
        heart.learn = AsyncMock()

        # MERGE without merged_content; confidence is HIGH (above 0.7) so
        # the floor doesn't fire. Only the missing-content flag should set.
        resolution = {
            "action": "MERGE", "confidence": 0.92,
            "reason": "Both partial truths",
            # merged_content omitted
        }
        response = MagicMock()
        response.content = [
            {"type": "tool_use", "id": "x", "name": "resolve_contradiction",
             "input": resolution}
        ]
        llm_client.call = AsyncMock(return_value=response)

        sleep_stats = {"facts_created": 0, "procedures_created": 0,
                       "censors_retired": 0}
        await handler._phase_resolve_contradictions(sleep_stats)
        # Drain pending fire-and-forget tasks
        import asyncio as _aio
        await _aio.sleep(0)

        assert captured_events, "no F031 event emitted"
        _, data = captured_events[0]
        assert data["downgraded_by_floor"] is False, (
            "confidence was 0.92 — floor didn't fire"
        )
        assert data["downgraded_due_to_missing_content"] is True, (
            "MERGE without merged_content should set the missing-content flag"
        )
        assert data["applied_action"] == "KEEP_BOTH"
        assert data["raw_action"] == "MERGE"

    def test_f031_merge_preserves_supersede_chain(self):
        """Codex finding on PR #411: F031 MERGE used to discard the
        heart.learn() result and call deactivate_fact() on both
        originals — leaving them active=False with NO superseded_by
        link. That orphaned every successful MERGE's source facts
        (chain broken, recoverable only via manual SQL).

        The fix mirrors F027's pattern: capture merged_detail.id and
        set superseded_by AND active=False atomically in a single
        session. Pin that contract via source inspection so a future
        PR can't accidentally re-introduce the bare deactivate_fact
        pattern.
        """
        import inspect

        from nous.handlers.sleep_handler import SleepHandler

        src = inspect.getsource(SleepHandler._phase_resolve_contradictions)

        # Find the MERGE branch (between `elif action == "MERGE":` and
        # the next elif).
        merge_start = src.index('elif action == "MERGE":')
        merge_end = src.index('elif action == "REMOVE_A":', merge_start)
        merge_branch = src[merge_start:merge_end]

        # Must capture the learn() result.
        assert "merged_detail" in merge_branch, (
            "F031 MERGE must capture heart.learn() return value to use "
            "its ID for the supersede link"
        )
        # Must set superseded_by on the originals (not just deactivate).
        assert "superseded_by" in merge_branch, (
            "F031 MERGE must set superseded_by on the originals to "
            "preserve the supersede chain — Codex P1 on PR #411"
        )
        # Must NOT use the bare deactivate_fact pattern that orphans.
        assert "deactivate_fact(fact1_id)" not in merge_branch, (
            "F031 MERGE must not bare-deactivate originals — that "
            "drops the superseded_by link and breaks recovery"
        )

    @pytest.mark.asyncio
    async def test_merge_without_content_downgrades_to_keep_both(self):
        """2026-05-01 audit: prod sleep returned MERGE without merged_content
        and silently no-op'd. New behavior: log warning and treat as
        KEEP_BOTH so the resolution is observable and counted accurately."""
        handler, brain, heart, bus, llm_client = _make_sleep_handler()

        heart.find_contradiction_candidates = AsyncMock(return_value=[
            {
                "fact1_id": "f1", "fact2_id": "f2",
                "content1": "Python is fast", "content2": "Python is slow",
                "date1": "2026-01-01", "date2": "2026-02-01",
                "subject": "python", "category": "technical",
            }
        ])
        heart.deactivate_fact = AsyncMock()
        heart.learn = AsyncMock()

        # Simulate the prod failure mode: action=MERGE, no merged_content
        resolution = {
            "action": "MERGE", "confidence": 0.85, "reason": "Both true at different times",
            # merged_content omitted — this is the bug we're guarding against
        }
        response = MagicMock()
        response.content = [
            {"type": "tool_use", "id": "toolu_x", "name": "resolve_contradiction",
             "input": resolution}
        ]
        llm_client.call = AsyncMock(return_value=response)

        sleep_stats = {"facts_created": 0, "procedures_created": 0, "censors_retired": 0}
        result = await handler._phase_resolve_contradictions(sleep_stats)

        assert result is True
        # MERGE→KEEP_BOTH downgrade means no facts deactivated, no merged fact created
        heart.deactivate_fact.assert_not_called()
        heart.learn.assert_not_called()
        # Counter does NOT increment for KEEP_BOTH (correct prod behavior)
        assert sleep_stats.get("contradictions_resolved", 0) == 0
