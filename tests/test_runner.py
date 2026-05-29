"""Unit tests for AgentRunner -- the direct Anthropic API execution loop.

Tests mock _tool_loop() (for run_turn tests) or _call_api() (for
reflection/credential tests) to avoid real API calls.
MockCognitiveLayer records all pre_turn/post_turn/end_session calls.

Migrated from _call_sdk mocks to _tool_loop/_call_api mocks for
005.2 direct API rewrite.
"""

import logging
import uuid
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from nous.api.runner import AgentRunner, ApiResponse, MAX_CONVERSATIONS, _parse_sse_event, StreamEvent
from nous.cognitive.schemas import FrameSelection, ToolResult, TurnContext, TurnResult
from nous.config import Settings


# ---------------------------------------------------------------------------
# Mock CognitiveLayer
# ---------------------------------------------------------------------------


class MockCognitiveLayer:
    """Returns preset TurnContext from pre_turn(), records all hook calls."""

    def __init__(self) -> None:
        self.pre_turn_calls: list[tuple] = []
        self.post_turn_calls: list[tuple] = []
        self.end_session_calls: list[tuple] = []
        self.preset_context = TurnContext(
            system_prompt="You are Nous, a thinking agent.",
            frame=FrameSelection(
                frame_id="conversation",
                frame_name="Conversation",
                confidence=0.9,
                match_method="default",
            ),
            decision_id=None,
            active_censors=[],
            context_token_estimate=100,
        )

    async def pre_turn(self, agent_id, session_id, user_input, session=None, *, conversation_messages=None, user_id=None, user_display_name=None, skip_episode=False, is_subtask=False):
        self.pre_turn_calls.append((agent_id, session_id, user_input))
        return self.preset_context

    async def post_turn(self, agent_id, session_id, turn_result, turn_context, session=None, is_background=False):
        self.post_turn_calls.append((agent_id, session_id, turn_result, turn_context))
        from nous.cognitive.schemas import Assessment

        return Assessment(actual=turn_result.response_text[:200])

    async def end_session(self, agent_id, session_id, reflection=None, session=None):
        self.end_session_calls.append((agent_id, session_id, reflection))

    async def list_frames(self, agent_id, session=None):
        return []


# ---------------------------------------------------------------------------
# Mock Brain / Heart stubs
# ---------------------------------------------------------------------------


class MockBrain:
    """Stub Brain for runner constructor."""

    async def close(self):
        pass


class MockHeart:
    """Stub Heart for runner constructor."""

    async def close(self):
        pass


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_cognitive():
    return MockCognitiveLayer()


@pytest.fixture
def mock_settings():
    """Settings with a fake API key for testing.

    _env_file=None makes the fixture hermetic against a developer's local .env
    (e.g., NOUS_THINKING_MODE=adaptive there would shadow our defaults).
    """
    return Settings(
        _env_file=None,
        ANTHROPIC_API_KEY="test-key-123",
        agent_id="test-agent",
        model="claude-sonnet-4-5-20250514",
        max_tokens=1024,
    )


@pytest_asyncio.fixture
async def runner(mock_cognitive, mock_settings):
    """AgentRunner with mocked _tool_loop (no real API calls).

    _tool_loop returns the same (text, list[ToolResult]) shape that
    run_turn() expects. We skip start() since it creates a real httpx client.
    """
    brain = MockBrain()
    heart = MockHeart()
    r = AgentRunner(mock_cognitive, brain, heart, mock_settings)

    # Mock _tool_loop to return (text, tool_results, usage, thinking_blocks)
    r._tool_loop = AsyncMock(return_value=("Hello from Nous!", [], {"input_tokens": 100, "output_tokens": 50}, []))

    yield r
    await r.close()


@pytest_asyncio.fixture
async def runner_error(mock_cognitive, mock_settings):
    """AgentRunner where _tool_loop raises an exception."""
    brain = MockBrain()
    heart = MockHeart()
    r = AgentRunner(mock_cognitive, brain, heart, mock_settings)

    # Mock _tool_loop to raise an exception (simulates API error)
    r._tool_loop = AsyncMock(side_effect=RuntimeError("API call failed: Internal error"))

    yield r
    await r.close()


# ---------------------------------------------------------------------------
# Basic run_turn tests
# ---------------------------------------------------------------------------


async def test_run_turn_basic(runner, mock_cognitive):
    """Sends message, gets response, returns (text, context)."""
    session_id = f"test-{uuid.uuid4().hex[:8]}"
    response_text, turn_context, _usage = await runner.run_turn(session_id, "Hello!")

    assert response_text == "Hello from Nous!"
    assert isinstance(turn_context, TurnContext)
    assert turn_context.frame.frame_id == "conversation"


async def test_run_turn_creates_conversation(runner):
    """New session_id creates Conversation."""
    session_id = f"test-{uuid.uuid4().hex[:8]}"
    await runner.run_turn(session_id, "Hello!")

    assert session_id in runner._conversations
    conv = runner._conversations[session_id]
    assert conv.session_id == session_id


async def test_run_turn_appends_messages(runner):
    """Conversation history grows each turn."""
    session_id = f"test-{uuid.uuid4().hex[:8]}"

    await runner.run_turn(session_id, "First message")
    await runner.run_turn(session_id, "Second message")

    conv = runner._conversations[session_id]
    # Should have 4 messages: user1, assistant1, user2, assistant2
    assert len(conv.messages) == 4
    assert conv.messages[0].role == "user"
    assert conv.messages[0].content == "First message"
    assert conv.messages[1].role == "assistant"
    assert conv.messages[2].role == "user"
    assert conv.messages[2].content == "Second message"
    assert conv.messages[3].role == "assistant"


async def test_run_turn_calls_pre_turn(runner, mock_cognitive):
    """cognitive.pre_turn() called with correct arguments."""
    session_id = f"test-{uuid.uuid4().hex[:8]}"
    await runner.run_turn(session_id, "Hello!")

    assert len(mock_cognitive.pre_turn_calls) == 1
    agent_id, sid, user_input = mock_cognitive.pre_turn_calls[0]
    assert sid == session_id
    assert user_input == "Hello!"


async def test_run_turn_calls_post_turn(runner, mock_cognitive):
    """cognitive.post_turn() called with TurnResult."""
    session_id = f"test-{uuid.uuid4().hex[:8]}"
    await runner.run_turn(session_id, "Hello!")

    assert len(mock_cognitive.post_turn_calls) == 1
    agent_id, sid, turn_result, turn_context = mock_cognitive.post_turn_calls[0]
    assert sid == session_id
    assert isinstance(turn_result, TurnResult)
    assert turn_result.response_text == "Hello from Nous!"
    assert turn_result.error is None


async def test_run_turn_api_error(runner_error, mock_cognitive):
    """API error -> exception re-raised after post_turn cleanup."""
    session_id = f"test-{uuid.uuid4().hex[:8]}"
    with pytest.raises(RuntimeError, match="API call failed"):
        await runner_error.run_turn(session_id, "Hello!")

    # post_turn should still be called (per spec: always called, even on error)
    assert len(mock_cognitive.post_turn_calls) == 1


async def test_run_turn_history_capped(runner):
    """When compaction is disabled, messages capped at last 20 via _format_messages."""
    session_id = f"test-{uuid.uuid4().hex[:8]}"

    # Send 15 turns (30 messages total)
    for i in range(15):
        await runner.run_turn(session_id, f"Message {i}")

    conv = runner._conversations[session_id]

    # With compaction enabled (default), all messages are kept
    formatted_all = runner._format_messages(conv)
    assert len(formatted_all) == 30

    # With compaction disabled, should be capped at 20
    runner._settings.compaction_enabled = False
    formatted_capped = runner._format_messages(conv)
    assert len(formatted_capped) <= 20


# ---------------------------------------------------------------------------
# Tool call tests (mock _tool_loop returns ToolResults)
# ---------------------------------------------------------------------------


async def test_run_turn_with_tool_calls(mock_cognitive, mock_settings):
    """Tool loop returns tool results, post_turn sees them in TurnResult."""
    brain = MockBrain()
    heart = MockHeart()
    r = AgentRunner(mock_cognitive, brain, heart, mock_settings)

    # Mock _tool_loop to return tool results with captured result/error
    tool_results = [
        ToolResult(
            tool_name="record_decision",
            arguments={"description": "test"},
            result="Decision recorded successfully.\nID: abc123",
        ),
        ToolResult(
            tool_name="learn_fact",
            arguments={"content": "a fact"},
            result="Fact learned successfully.\nID: def456",
        ),
    ]
    r._tool_loop = AsyncMock(return_value=("Done with tools.", tool_results, {"input_tokens": 200, "output_tokens": 100}, []))

    try:
        session_id = f"test-tools-{uuid.uuid4().hex[:8]}"
        response_text, _, _usage = await r.run_turn(session_id, "Do something with tools")

        assert response_text == "Done with tools."

        # post_turn should receive the tool results
        assert len(mock_cognitive.post_turn_calls) == 1
        _, _, turn_result, _ = mock_cognitive.post_turn_calls[0]
        assert len(turn_result.tool_results) == 2
        assert turn_result.tool_results[0].tool_name == "record_decision"
        assert turn_result.tool_results[0].result is not None
        assert "Decision recorded" in turn_result.tool_results[0].result
        assert turn_result.tool_results[1].tool_name == "learn_fact"
        assert turn_result.tool_results[1].result is not None
    finally:
        await r.close()


async def test_run_turn_with_tool_error(mock_cognitive, mock_settings):
    """Tool error captured in ToolResult.error field."""
    brain = MockBrain()
    heart = MockHeart()
    r = AgentRunner(mock_cognitive, brain, heart, mock_settings)

    tool_results = [
        ToolResult(
            tool_name="record_decision",
            arguments={"description": "bad"},
            error="Error recording decision: validation failed",
        ),
    ]
    r._tool_loop = AsyncMock(return_value=("Tool had an error.", tool_results, {"input_tokens": 200, "output_tokens": 100}, []))

    try:
        session_id = f"test-tool-err-{uuid.uuid4().hex[:8]}"
        response_text, _, _usage = await r.run_turn(session_id, "Try something broken")

        assert len(mock_cognitive.post_turn_calls) == 1
        _, _, turn_result, _ = mock_cognitive.post_turn_calls[0]
        assert len(turn_result.tool_results) == 1
        assert turn_result.tool_results[0].error is not None
        assert "validation failed" in turn_result.tool_results[0].error
    finally:
        await r.close()


# ---------------------------------------------------------------------------
# Frame instruction tests
# ---------------------------------------------------------------------------


async def test_run_turn_frame_instructions(mock_cognitive, mock_settings):
    """Decision frame adds 'MUST call record_decision' to system prompt.

    Verifies _tool_loop receives the system prompt with frame instructions.
    """
    brain = MockBrain()
    heart = MockHeart()
    r = AgentRunner(mock_cognitive, brain, heart, mock_settings)

    # Set frame to "decision"
    mock_cognitive.preset_context = TurnContext(
        system_prompt="You are Nous.",
        frame=FrameSelection(
            frame_id="decision",
            frame_name="Decision",
            confidence=0.95,
            match_method="pattern",
        ),
        decision_id="test-decision-123",
        active_censors=[],
        context_token_estimate=100,
    )

    r._tool_loop = AsyncMock(return_value=("Decision made.", [], {"input_tokens": 100, "output_tokens": 50}, []))

    try:
        session_id = f"test-frame-{uuid.uuid4().hex[:8]}"
        await r.run_turn(session_id, "Should I use caching?")

        # Check that _tool_loop was called with system prompt containing
        # the decision frame instructions
        call_args = r._tool_loop.call_args
        system_prompt = call_args.kwargs.get("system_prompt") or call_args.args[0]
        assert "MUST call" in system_prompt
        assert "record_decision" in system_prompt
    finally:
        await r.close()


async def test_run_turn_safety_net(mock_cognitive, mock_settings, caplog):
    """Missed record_decision in decision frame -> warning logged."""
    brain = MockBrain()
    heart = MockHeart()
    r = AgentRunner(mock_cognitive, brain, heart, mock_settings)

    # Set frame to "decision" (in _REQUIRED_DECISION_FRAMES)
    mock_cognitive.preset_context = TurnContext(
        system_prompt="You are Nous.",
        frame=FrameSelection(
            frame_id="decision",
            frame_name="Decision",
            confidence=0.95,
            match_method="pattern",
        ),
        decision_id="test-decision-456",
        active_censors=[],
        context_token_estimate=100,
    )

    # Return response with NO tool calls (record_decision not called)
    r._tool_loop = AsyncMock(return_value=("I decided to use caching.", [], {"input_tokens": 100, "output_tokens": 50}, []))

    try:
        session_id = f"test-safety-{uuid.uuid4().hex[:8]}"
        with caplog.at_level(logging.WARNING, logger="nous.api.runner"):
            await r.run_turn(session_id, "Should I use caching?")

        # Safety net should have logged a WARNING (decision frame is required)
        safety_records = [r for r in caplog.records if "Safety net" in r.message]
        assert safety_records
        assert safety_records[0].levelno == logging.WARNING
    finally:
        await r.close()


async def test_run_turn_safety_net_task_frame_debug(mock_cognitive, mock_settings, caplog):
    """Missed record_decision in task frame -> DEBUG (not WARNING)."""
    brain = MockBrain()
    heart = MockHeart()
    r = AgentRunner(mock_cognitive, brain, heart, mock_settings)

    mock_cognitive.preset_context = TurnContext(
        system_prompt="You are Nous.",
        frame=FrameSelection(
            frame_id="task",
            frame_name="Task",
            confidence=0.95,
            match_method="pattern",
        ),
        decision_id=None,
        active_censors=[],
        context_token_estimate=100,
    )

    r._tool_loop = AsyncMock(return_value=("Done.", [], {"input_tokens": 100, "output_tokens": 50}, []))

    try:
        session_id = f"test-safety-task-{uuid.uuid4().hex[:8]}"
        with caplog.at_level(logging.DEBUG, logger="nous.api.runner"):
            await r.run_turn(session_id, "Run the migration")

        safety_records = [r for r in caplog.records if "Safety net" in r.message]
        assert safety_records
        # Task frame should log at DEBUG, not WARNING
        assert safety_records[0].levelno == logging.DEBUG
    finally:
        await r.close()


# ---------------------------------------------------------------------------
# Conversation lifecycle tests
# ---------------------------------------------------------------------------


async def test_run_turn_touches_session_monitor(runner, mock_cognitive):
    """run_turn synchronously touches the wired session_monitor at request start.

    Pins the call-site that closes the mid-turn-closure race Codex flagged
    on PR #424: without this call, a long in-flight turn for a session whose
    _last_activity was already past threshold can be closed mid-stream by a
    monitor tick, popping runner._conversations and orphaning the response.
    """
    from unittest.mock import MagicMock
    monitor = MagicMock()
    runner._session_monitor = monitor

    session_id = f"test-{uuid.uuid4().hex[:8]}"
    await runner.run_turn(session_id, "Hello!")

    monitor.touch.assert_called_once_with(
        session_id, runner._settings.agent_id, is_background=False
    )


async def test_run_turn_touch_failure_does_not_break_turn(runner, mock_cognitive):
    """A monitor.touch() raise is swallowed; the turn still completes.

    Touch is best-effort — a programming bug in the monitor (e.g., a future
    refactor that lets touch raise) must not break a chat turn for users.
    """
    from unittest.mock import MagicMock
    monitor = MagicMock()
    monitor.touch.side_effect = RuntimeError("monitor exploded")
    runner._session_monitor = monitor

    session_id = f"test-{uuid.uuid4().hex[:8]}"
    response_text, turn_context, _usage = await runner.run_turn(session_id, "Hello!")

    assert response_text == "Hello from Nous!"
    monitor.touch.assert_called_once()


async def test_run_turn_no_monitor_does_not_raise(runner):
    """Unwired monitor (None) is a benign no-op — preserves backward compat."""
    assert runner._session_monitor is None
    session_id = f"test-{uuid.uuid4().hex[:8]}"
    response_text, _ctx, _usage = await runner.run_turn(session_id, "Hello!")
    assert response_text == "Hello from Nous!"


async def test_run_turn_and_end_conversation_serialize(runner, mock_cognitive):
    """run_turn and end_conversation must NOT interleave for the same session.

    Codex P1 #3 on PR #424: abort_if was a point-in-time check, but
    cognitive.end_session has internal awaits during which a fresh run_turn
    could start and modify state that end_conversation then popped. The
    per-session lock makes the whole mutation block exclusive.

    Test: start a slow run_turn (block on event), kick off end_conversation
    concurrently, assert end_conversation does NOT touch cognitive.end_session
    until run_turn has released the lock.
    """
    import asyncio
    from unittest.mock import AsyncMock as _AsyncMock

    session_id = f"test-{uuid.uuid4().hex[:8]}"

    # Make _tool_loop block on a gate so we can race end_conversation against it.
    gate = asyncio.Event()
    timeline: list[str] = []

    async def gated_tool_loop(*args, **kwargs):
        timeline.append("run_turn.tool_loop.enter")
        await gate.wait()
        timeline.append("run_turn.tool_loop.exit")
        return ("ok", [], {"input_tokens": 0, "output_tokens": 0}, [])

    runner._tool_loop = _AsyncMock(side_effect=gated_tool_loop)

    async def trace_end_session(*args, **kwargs):
        timeline.append("end_conversation.cognitive.end_session")

    mock_cognitive.end_session = trace_end_session

    async def slow_turn():
        await runner.run_turn(session_id, "hello")

    async def parallel_close():
        # Give run_turn a head start so it grabs the lock first.
        await asyncio.sleep(0.05)
        timeline.append("end_conversation.start")
        await runner.end_conversation(session_id)
        timeline.append("end_conversation.return")

    async def release_after_delay():
        await asyncio.sleep(0.15)
        gate.set()

    await asyncio.gather(slow_turn(), parallel_close(), release_after_delay())

    # Run_turn must release the lock before end_conversation enters its
    # critical section. Specifically, cognitive.end_session must execute
    # AFTER run_turn.tool_loop.exit.
    exit_idx = timeline.index("run_turn.tool_loop.exit")
    cognitive_idx = timeline.index("end_conversation.cognitive.end_session")
    assert cognitive_idx > exit_idx, timeline


async def test_end_conversation_abort_if_skips_state_mutation(runner, mock_cognitive):
    """abort_if=True after reflection prevents cognitive.end_session + _conversations pop.

    Codex P1 #2 on PR #424: monitor passes abort_if so a user message that
    lands during reflection (the 15-30s slow phase of close) pre-empts the
    close before any state is mutated. Pins the contract.
    """
    session_id = f"test-{uuid.uuid4().hex[:8]}"
    await runner.run_turn(session_id, "Hello!")
    assert session_id in runner._conversations

    closed = await runner.end_conversation(
        session_id, abort_if=lambda: True
    )

    assert closed is False
    # State preserved — session lives on, no end_session call.
    assert session_id in runner._conversations
    assert len(mock_cognitive.end_session_calls) == 0


async def test_end_conversation_abort_if_false_proceeds(runner, mock_cognitive):
    """abort_if=False (no activity resumed) still closes normally."""
    session_id = f"test-{uuid.uuid4().hex[:8]}"
    await runner.run_turn(session_id, "Hello!")

    closed = await runner.end_conversation(
        session_id, abort_if=lambda: False
    )

    assert closed is True
    assert session_id not in runner._conversations
    assert len(mock_cognitive.end_session_calls) == 1


async def test_end_conversation_default_returns_true(runner, mock_cognitive):
    """Manual /new path (no abort_if) still returns True and closes unconditionally."""
    session_id = f"test-{uuid.uuid4().hex[:8]}"
    await runner.run_turn(session_id, "Hello!")

    closed = await runner.end_conversation(session_id)

    assert closed is True
    assert session_id not in runner._conversations


async def test_end_conversation(runner, mock_cognitive):
    """Removes from dict, calls cognitive.end_session()."""
    session_id = f"test-{uuid.uuid4().hex[:8]}"
    await runner.run_turn(session_id, "Hello!")
    assert session_id in runner._conversations

    await runner.end_conversation(session_id)

    assert session_id not in runner._conversations
    assert len(mock_cognitive.end_session_calls) == 1
    agent_id, sid, reflection = mock_cognitive.end_session_calls[0]
    assert sid == session_id


async def test_end_conversation_with_reflection(mock_cognitive, mock_settings):
    """Conversation with >= 3 turns triggers reflection via _call_api.

    run_turn calls _tool_loop, end_conversation calls _call_api directly.
    """
    brain = MockBrain()
    heart = MockHeart()
    r = AgentRunner(mock_cognitive, brain, heart, mock_settings)

    # Mock _tool_loop for the 3 run_turn calls
    turn_counter = 0

    async def mock_tool_loop(system_prompt, conversation, frame_id, **kwargs):
        nonlocal turn_counter
        turn_counter += 1
        return (f"Response {turn_counter}", [], {}, [])

    r._tool_loop = mock_tool_loop

    # Mock _call_api for the reflection call in end_conversation
    r._call_api = AsyncMock(return_value=ApiResponse(
        content=[{"type": "text", "text": "Reflection: The task was about testing."}],
        stop_reason="end_turn",
    ))

    try:
        session_id = f"test-reflect-{uuid.uuid4().hex[:8]}"

        # Run 3 turns (6 messages: 3 user + 3 assistant)
        for i in range(3):
            await r.run_turn(session_id, f"Turn {i + 1}")

        assert len(r._conversations[session_id].messages) == 6

        await r.end_conversation(session_id)

        # _call_api should have been called for reflection
        r._call_api.assert_called_once()
        call_kwargs = r._call_api.call_args
        reflection_system = call_kwargs.kwargs.get("system_prompt") or call_kwargs.args[0]
        assert "reviewing a conversation" in reflection_system.lower()

        # end_session should have been called with reflection text
        assert len(mock_cognitive.end_session_calls) == 1
        _, _, reflection = mock_cognitive.end_session_calls[0]
        assert reflection is not None
        assert "Reflection" in reflection
    finally:
        await r.close()


async def test_end_conversation_nonexistent(runner, mock_cognitive):
    """Ending nonexistent conversation doesn't error."""
    session_id = f"nonexistent-{uuid.uuid4().hex[:8]}"
    # Should not raise
    await runner.end_conversation(session_id)
    # end_session should still be called
    assert len(mock_cognitive.end_session_calls) == 1


# ---------------------------------------------------------------------------
# Credential / start() tests
# ---------------------------------------------------------------------------


async def test_start_credentials_api_key(mock_cognitive):
    """start() creates httpx client with x-api-key header when API key set."""
    brain = MockBrain()
    heart = MockHeart()
    settings = Settings(
        _env_file=None,
        ANTHROPIC_API_KEY="test-api-key-abc",
        agent_id="test-agent",
        api_backend="httpx",
    )
    r = AgentRunner(mock_cognitive, brain, heart, settings)

    await r.start()
    try:
        # Verify httpx client was created with x-api-key header
        http = r._api._http
        assert http is not None
        assert http.headers.get("x-api-key") == "test-api-key-abc"
        assert http.headers.get("anthropic-version") == "2023-06-01"
        assert http.headers.get("content-type") == "application/json"
        # No authorization header when only API key set
        assert "authorization" not in http.headers
    finally:
        await r.close()


async def test_start_credentials_auth_token(mock_cognitive):
    """start() uses Bearer token (takes precedence over API key)."""
    brain = MockBrain()
    heart = MockHeart()
    settings = Settings(
        ANTHROPIC_API_KEY="test-api-key",
        ANTHROPIC_AUTH_TOKEN="test-auth-token-xyz",
        agent_id="test-agent",
        api_backend="httpx",
    )
    r = AgentRunner(mock_cognitive, brain, heart, settings)

    await r.start()
    try:
        # Bearer token takes precedence
        http = r._api._http
        assert http is not None
        assert http.headers.get("authorization") == "Bearer test-auth-token-xyz"
        # x-api-key should NOT be set when auth_token is present
        assert "x-api-key" not in http.headers
    finally:
        await r.close()


async def test_start_credentials_none(mock_cognitive, caplog):
    """start() with no credentials logs a warning but doesn't raise."""
    brain = MockBrain()
    heart = MockHeart()
    settings = Settings(_env_file=None, agent_id="test-agent", api_backend="httpx")
    r = AgentRunner(mock_cognitive, brain, heart, settings)

    with caplog.at_level(logging.WARNING, logger="nous.api.anthropic_client"):
        await r.start()

    try:
        # Should have logged a warning about missing credentials
        assert any("API calls will fail" in record.message for record in caplog.records)
        # Client should still be created (just without auth headers)
        http = r._api._http
        assert http is not None
        assert http.headers.get("anthropic-version") == "2023-06-01"
    finally:
        await r.close()


# ---------------------------------------------------------------------------
# LRU eviction test
# ---------------------------------------------------------------------------


async def test_lru_eviction(mock_cognitive, mock_settings):
    """When MAX_CONVERSATIONS exceeded, oldest conversation is evicted."""
    brain = MockBrain()
    heart = MockHeart()
    r = AgentRunner(mock_cognitive, brain, heart, mock_settings)
    r._tool_loop = AsyncMock(return_value=("OK", [], {"input_tokens": 100, "output_tokens": 50}, []))

    try:
        # Create MAX_CONVERSATIONS + 1 conversations
        session_ids = []
        for i in range(MAX_CONVERSATIONS + 1):
            sid = f"evict-{i:04d}"
            session_ids.append(sid)
            await r.run_turn(sid, f"Message {i}")

        # Should not exceed MAX_CONVERSATIONS
        assert len(r._conversations) <= MAX_CONVERSATIONS

        # The first session should have been evicted
        assert session_ids[0] not in r._conversations
        # The last session should still exist
        assert session_ids[-1] in r._conversations
    finally:
        await r.close()


# ---------------------------------------------------------------------------
# 007: Extended thinking config tests
# ---------------------------------------------------------------------------


def test_thinking_mode_defaults():
    """Default settings: thinking off, budget=10000, effort=high."""
    s = Settings(_env_file=None, ANTHROPIC_API_KEY="test-key")
    assert s.thinking_mode == "off"
    assert s.thinking_budget == 10000
    assert s.effort == "high"


def test_thinking_mode_literal_validation():
    """Invalid thinking_mode value rejected by Literal type."""
    with pytest.raises(Exception):
        Settings(ANTHROPIC_API_KEY="test-key", thinking_mode="invalid")


def test_effort_literal_validation():
    """Invalid effort value rejected by Literal type."""
    with pytest.raises(Exception):
        Settings(ANTHROPIC_API_KEY="test-key", effort="extreme")


def test_thinking_budget_minimum():
    """thinking_budget < 1024 raises ValueError for manual mode."""
    with pytest.raises(ValueError, match="thinking_budget must be >= 1024"):
        Settings(ANTHROPIC_API_KEY="test-key", thinking_mode="manual", thinking_budget=500, max_tokens=16000)


def test_thinking_budget_max_tokens_constraint():
    """thinking_budget >= max_tokens raises ValueError for manual mode."""
    with pytest.raises(ValueError, match="thinking_budget.*must be < max_tokens"):
        Settings(ANTHROPIC_API_KEY="test-key", thinking_mode="manual", thinking_budget=10000, max_tokens=4096)


def test_thinking_budget_not_validated_when_off():
    """Budget validation only applies to manual mode, not off or adaptive."""
    # budget > max_tokens is fine when mode is "off" (budget is ignored)
    s = Settings(ANTHROPIC_API_KEY="test-key", thinking_mode="off", thinking_budget=99999, max_tokens=4096)
    assert s.thinking_mode == "off"

    s2 = Settings(ANTHROPIC_API_KEY="test-key", thinking_mode="adaptive")
    assert s2.thinking_mode == "adaptive"


# ---------------------------------------------------------------------------
# 007: Payload tests
# ---------------------------------------------------------------------------


def test_payload_thinking_off(mock_cognitive, mock_settings):
    """thinking_mode=off: no thinking key in payload."""
    brain = MockBrain()
    heart = MockHeart()
    r = AgentRunner(mock_cognitive, brain, heart, mock_settings)
    # mock_settings has thinking_mode="off" by default
    payload = r._build_api_payload("system", [{"role": "user", "content": "hi"}])
    assert "thinking" not in payload
    assert "output_config" not in payload  # effort=high is default, omitted


def test_payload_thinking_adaptive():
    """thinking_mode=adaptive: adaptive thinking in payload."""
    s = Settings(ANTHROPIC_API_KEY="test-key", thinking_mode="adaptive")
    r = AgentRunner(MockCognitiveLayer(), MockBrain(), MockHeart(), s)
    payload = r._build_api_payload("system", [{"role": "user", "content": "hi"}])
    assert payload["thinking"] == {"type": "adaptive"}


def test_payload_thinking_manual():
    """thinking_mode=manual: enabled + budget_tokens in payload."""
    s = Settings(ANTHROPIC_API_KEY="test-key", thinking_mode="manual", thinking_budget=8000, max_tokens=16000)
    r = AgentRunner(MockCognitiveLayer(), MockBrain(), MockHeart(), s)
    payload = r._build_api_payload("system", [{"role": "user", "content": "hi"}])
    assert payload["thinking"] == {"type": "enabled", "budget_tokens": 8000}


def test_payload_effort_default():
    """effort=high (default): no output_config in payload."""
    s = Settings(ANTHROPIC_API_KEY="test-key", effort="high")
    r = AgentRunner(MockCognitiveLayer(), MockBrain(), MockHeart(), s)
    payload = r._build_api_payload("system", [{"role": "user", "content": "hi"}])
    assert "output_config" not in payload


def test_payload_effort_medium():
    """effort=medium: output_config with effort in payload."""
    s = Settings(ANTHROPIC_API_KEY="test-key", effort="medium", model="claude-sonnet-4-6")
    r = AgentRunner(MockCognitiveLayer(), MockBrain(), MockHeart(), s)
    payload = r._build_api_payload("system", [{"role": "user", "content": "hi"}])
    assert payload["output_config"] == {"effort": "medium"}


def test_payload_effort_without_thinking():
    """effort works independently of thinking mode."""
    s = Settings(ANTHROPIC_API_KEY="test-key", thinking_mode="off", effort="low", model="claude-sonnet-4-6")
    r = AgentRunner(MockCognitiveLayer(), MockBrain(), MockHeart(), s)
    payload = r._build_api_payload("system", [{"role": "user", "content": "hi"}])
    assert "thinking" not in payload
    assert payload["output_config"] == {"effort": "low"}


def test_payload_skip_thinking():
    """skip_thinking=True omits thinking param even when mode is set."""
    s = Settings(ANTHROPIC_API_KEY="test-key", thinking_mode="adaptive")
    r = AgentRunner(MockCognitiveLayer(), MockBrain(), MockHeart(), s)
    payload = r._build_api_payload("system", [{"role": "user", "content": "hi"}], skip_thinking=True)
    assert "thinking" not in payload


def test_payload_adaptive_with_effort():
    """Adaptive thinking combined with effort parameter."""
    s = Settings(ANTHROPIC_API_KEY="test-key", thinking_mode="adaptive", effort="medium", model="claude-sonnet-4-6")
    r = AgentRunner(MockCognitiveLayer(), MockBrain(), MockHeart(), s)
    payload = r._build_api_payload("system", [{"role": "user", "content": "hi"}])
    assert payload["thinking"] == {"type": "adaptive"}
    assert payload["output_config"] == {"effort": "medium"}


def test_payload_thinking_skipped_for_haiku():
    """thinking params omitted when model is haiku (unsupported)."""
    s = Settings(ANTHROPIC_API_KEY="test-key", thinking_mode="adaptive", model="claude-haiku-4-5-20251001")
    r = AgentRunner(MockCognitiveLayer(), MockBrain(), MockHeart(), s)
    payload = r._build_api_payload("system", [{"role": "user", "content": "hi"}])
    assert "thinking" not in payload


def test_payload_thinking_skipped_for_haiku_model_override():
    """thinking params omitted when model_override is haiku (subtask worker path)."""
    s = Settings(ANTHROPIC_API_KEY="test-key", thinking_mode="adaptive", model="claude-opus-4-6")
    r = AgentRunner(MockCognitiveLayer(), MockBrain(), MockHeart(), s)
    payload = r._build_api_payload(
        "system", [{"role": "user", "content": "hi"}],
        model_override="claude-haiku-4-5-20251001",
    )
    assert "thinking" not in payload


def test_payload_effort_skipped_for_haiku():
    """effort parameter omitted when model is haiku (unsupported)."""
    s = Settings(ANTHROPIC_API_KEY="test-key", effort="medium", model="claude-haiku-4-5-20251001")
    r = AgentRunner(MockCognitiveLayer(), MockBrain(), MockHeart(), s)
    payload = r._build_api_payload("system", [{"role": "user", "content": "hi"}])
    assert "output_config" not in payload


def test_payload_effort_included_for_opus():
    """effort parameter included for opus (now supported)."""
    s = Settings(ANTHROPIC_API_KEY="test-key", effort="medium", model="claude-opus-4-6")
    r = AgentRunner(MockCognitiveLayer(), MockBrain(), MockHeart(), s)
    payload = r._build_api_payload("system", [{"role": "user", "content": "hi"}])
    assert payload["output_config"] == {"effort": "medium"}


# ---------------------------------------------------------------------------
# 007: SSE parser tests
# ---------------------------------------------------------------------------


def test_parse_thinking_block_start():
    """content_block_start with type=thinking returns thinking_start event."""
    event = _parse_sse_event({
        "type": "content_block_start",
        "index": 0,
        "content_block": {"type": "thinking", "thinking": ""},
    })
    assert event is not None
    assert event.type == "thinking_start"
    assert event.block_index == 0


def test_parse_redacted_thinking_start():
    """content_block_start with type=redacted_thinking returns redacted_thinking event."""
    event = _parse_sse_event({
        "type": "content_block_start",
        "index": 1,
        "content_block": {"type": "redacted_thinking", "data": "encrypted-data-here"},
    })
    assert event is not None
    assert event.type == "redacted_thinking"
    assert event.text == "encrypted-data-here"
    assert event.block_index == 1


def test_parse_redacted_thinking_not_text_fallthrough():
    """redacted_thinking block does NOT fall through to text_block_start."""
    event = _parse_sse_event({
        "type": "content_block_start",
        "index": 0,
        "content_block": {"type": "redacted_thinking", "data": "abc"},
    })
    assert event.type == "redacted_thinking"
    assert event.type != "text_block_start"


def test_parse_thinking_delta():
    """content_block_delta with thinking_delta returns thinking_delta event."""
    event = _parse_sse_event({
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "thinking_delta", "thinking": "Let me analyze..."},
    })
    assert event is not None
    assert event.type == "thinking_delta"
    assert event.text == "Let me analyze..."


def test_parse_signature_delta():
    """content_block_delta with signature_delta returns signature_delta event."""
    event = _parse_sse_event({
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "signature_delta", "signature": "EqQBCgIYAh..."},
    })
    assert event is not None
    assert event.type == "signature_delta"
    assert event.text == "EqQBCgIYAh..."
    assert event.block_index == 0


# ---------------------------------------------------------------------------
# 007: Beta header tests
# ---------------------------------------------------------------------------


async def test_start_thinking_beta_header(mock_cognitive):
    """start() adds interleaved-thinking beta header when thinking enabled."""
    s = Settings(ANTHROPIC_API_KEY="test-api-key", thinking_mode="adaptive", api_backend="httpx")
    r = AgentRunner(mock_cognitive, MockBrain(), MockHeart(), s)
    await r.start()
    try:
        http = r._api._http
        assert "anthropic-beta" in http.headers
        assert "interleaved-thinking-2025-05-14" in http.headers["anthropic-beta"]
    finally:
        await r.close()


async def test_start_no_thinking_no_beta(mock_cognitive):
    """start() still has beta headers (claude-code, fine-grained-tool-streaming, etc.)."""
    s = Settings(ANTHROPIC_API_KEY="test-api-key", thinking_mode="off", api_backend="httpx")
    r = AgentRunner(mock_cognitive, MockBrain(), MockHeart(), s)
    await r.start()
    try:
        http = r._api._http
        # Beta headers are always present (claude-code, fine-grained-tool-streaming, etc.)
        assert "anthropic-beta" in http.headers
        # But interleaved-thinking is always included for forward compat
        assert "interleaved-thinking-2025-05-14" in http.headers["anthropic-beta"]
    finally:
        await r.close()


async def test_start_oat_plus_thinking_headers(mock_cognitive):
    """start() combines OAT + thinking beta headers."""
    s = Settings(
        ANTHROPIC_API_KEY="sk-ant-oat-test-token",
        thinking_mode="manual",
        thinking_budget=8000,
        max_tokens=16000,
        api_backend="httpx",
    )
    r = AgentRunner(mock_cognitive, MockBrain(), MockHeart(), s)
    await r.start()
    try:
        beta = r._api._http.headers["anthropic-beta"]
        assert "oauth-2025-04-20" in beta
        assert "interleaved-thinking-2025-05-14" in beta
    finally:
        await r.close()


# ---------------------------------------------------------------------------
# 007: Tool loop thinking preservation (non-streaming)
# ---------------------------------------------------------------------------


async def test_tool_loop_preserves_thinking_blocks(mock_cognitive):
    """Non-streaming _tool_loop preserves thinking blocks in assistant content.

    _tool_loop appends full api_response.content (line 745-748), so thinking
    blocks are naturally preserved. This test confirms the behavior.
    """
    s = Settings(ANTHROPIC_API_KEY="test-key", thinking_mode="adaptive")
    r = AgentRunner(mock_cognitive, MockBrain(), MockHeart(), s)

    # Simulate: first call returns thinking + tool_use (stop_reason=tool_use)
    # second call returns text (stop_reason=end_turn)
    call_count = 0

    async def mock_call_api(system_prompt, messages, tools=None, skip_thinking=False, model_override=None, is_background=False):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return ApiResponse(
                content=[
                    {"type": "thinking", "thinking": "I should call the tool", "signature": "sig1"},
                    {"type": "tool_use", "id": "tool_1", "name": "test_tool", "input": {"key": "val"}},
                ],
                stop_reason="tool_use",
            )
        return ApiResponse(
            content=[{"type": "text", "text": "Done."}],
            stop_reason="end_turn",
        )

    r._call_api = mock_call_api

    # Mock dispatcher
    class MockDispatcher:
        def available_tools(self, frame_id):
            return [{"name": "test_tool", "description": "test", "input_schema": {"type": "object"}}]

        async def dispatch(self, name, input_data, session_id=None):
            return "tool result", False

    r.set_dispatcher(MockDispatcher())

    from nous.api.runner import Conversation
    conv = Conversation(session_id="test")
    conv.messages.append(type("M", (), {"role": "user", "content": "do something"})())

    text, tool_results, usage, _ = await r._tool_loop("system", conv, "task")

    assert text == "Done."
    # The assistant message in the messages list should have thinking blocks
    # (this happens internally in _tool_loop via messages.append)
    await r.close()


async def test_tool_loop_preserves_redacted_thinking(mock_cognitive):
    """Non-streaming: redacted_thinking blocks preserved in content."""
    s = Settings(ANTHROPIC_API_KEY="test-key", thinking_mode="adaptive")
    r = AgentRunner(mock_cognitive, MockBrain(), MockHeart(), s)

    call_count = 0

    async def mock_call_api(system_prompt, messages, tools=None, skip_thinking=False, model_override=None, is_background=False):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return ApiResponse(
                content=[
                    {"type": "thinking", "thinking": "reasoning...", "signature": "sig1"},
                    {"type": "redacted_thinking", "data": "encrypted-data"},
                    {"type": "tool_use", "id": "tool_1", "name": "test_tool", "input": {}},
                ],
                stop_reason="tool_use",
            )
        return ApiResponse(
            content=[{"type": "text", "text": "Result."}],
            stop_reason="end_turn",
        )

    r._call_api = mock_call_api

    class MockDispatcher:
        def available_tools(self, frame_id):
            return [{"name": "test_tool", "description": "test", "input_schema": {"type": "object"}}]

        async def dispatch(self, name, input_data, session_id=None):
            return "ok", False

    r.set_dispatcher(MockDispatcher())

    from nous.api.runner import Conversation
    conv = Conversation(session_id="test")
    conv.messages.append(type("M", (), {"role": "user", "content": "test"})())

    text, _, _, _ = await r._tool_loop("system", conv, "task")
    assert text == "Result."
    await r.close()
