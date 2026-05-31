"""Integration tests for the AgentRunner._tool_loop() method.

Tests mock _call_api() to return controlled ApiResponse objects and
use a real ToolDispatcher with mock tool handlers. This verifies the
tool loop's message accumulation, dispatch, termination conditions,
and parallel tool handling.
"""

import uuid
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from nous.api.runner import AgentRunner, ApiResponse, Conversation, Message
from nous.api.tools import ToolDispatcher
from nous.cognitive.schemas import FrameSelection, TurnContext
from nous.config import Settings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_api_response(
    text: str = "",
    stop_reason: str = "end_turn",
    tool_uses: list[dict] | None = None,
) -> ApiResponse:
    """Build an ApiResponse with text and/or tool_use blocks."""
    content = []
    if text:
        content.append({"type": "text", "text": text})
    if tool_uses:
        for tu in tool_uses:
            content.append({
                "type": "tool_use",
                "id": tu.get("id", f"toolu_{uuid.uuid4().hex[:12]}"),
                "name": tu["name"],
                "input": tu.get("input", {}),
            })
    return ApiResponse(content=content, stop_reason=stop_reason)


def _make_conversation(messages: list[tuple[str, str]] | None = None) -> Conversation:
    """Build a Conversation with optional (role, content) message pairs."""
    conv = Conversation(session_id=f"test-{uuid.uuid4().hex[:8]}")
    if messages:
        for role, content in messages:
            conv.messages.append(Message(role=role, content=content))
    return conv


class MockCognitiveLayer:
    """Minimal mock for runner constructor."""

    async def pre_turn(self, *a, **kw):
        return TurnContext(
            system_prompt="test",
            frame=FrameSelection(
                frame_id="task", frame_name="Task",
                confidence=0.9, match_method="default",
            ),
        )

    async def post_turn(self, *a, **kw):
        pass

    async def end_session(self, *a, **kw):
        pass


class MockBrain:
    async def close(self):
        pass


class MockHeart:
    async def close(self):
        pass


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tool_settings():
    """Settings with low max_turns for testing loop termination."""
    return Settings(
        ANTHROPIC_API_KEY="test-key",
        agent_id="test-agent",
        max_turns=3,
        max_tokens=1024,
    )


@pytest.fixture
def echo_dispatcher():
    """ToolDispatcher with a simple echo tool registered."""
    dispatcher = ToolDispatcher()

    async def echo_tool(message: str = "default") -> dict:
        return {"content": [{"type": "text", "text": f"Echo: {message}"}]}

    dispatcher.register("echo", echo_tool, {
        "type": "object",
        "description": "Echo tool",
        "properties": {"message": {"type": "string"}},
        "required": ["message"],
    })

    # Register a second tool for parallel dispatch tests
    async def add_tool(a: float = 0, b: float = 0) -> dict:
        return {"content": [{"type": "text", "text": str(a + b)}]}

    dispatcher.register("add", add_tool, {
        "type": "object",
        "description": "Add tool",
        "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
        "required": ["a", "b"],
    })

    return dispatcher


@pytest_asyncio.fixture
async def loop_runner(tool_settings, echo_dispatcher):
    """AgentRunner with mocked _call_api and real ToolDispatcher."""
    cognitive = MockCognitiveLayer()
    brain = MockBrain()
    heart = MockHeart()
    r = AgentRunner(cognitive, brain, heart, tool_settings)
    r.set_dispatcher(echo_dispatcher)
    # _call_api will be mocked per-test
    yield r
    await r.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestToolLoop:
    """Tests for the _tool_loop method of AgentRunner."""

    @pytest.mark.asyncio
    async def test_tool_loop_end_turn(self, loop_runner):
        """API returns end_turn on first call -> single API call, text extracted."""
        loop_runner._call_api = AsyncMock(return_value=_make_api_response(
            text="Simple response",
            stop_reason="end_turn",
        ))

        conv = _make_conversation([("user", "Hello")])
        response_text, tool_results, _usage, _ = await loop_runner._tool_loop(
            system_prompt="Test prompt",
            conversation=conv,
            frame_id="task",
        )

        assert response_text == "Simple response"
        assert tool_results == []
        loop_runner._call_api.assert_called_once()

    @pytest.mark.asyncio
    async def test_tool_loop_with_tool_use(self, loop_runner):
        """API returns tool_use then end_turn -> dispatch + 2 API calls."""
        # First call: tool_use
        tool_response = _make_api_response(
            stop_reason="tool_use",
            tool_uses=[{"name": "echo", "input": {"message": "test"}, "id": "toolu_001"}],
        )
        # Second call: end_turn with final text
        final_response = _make_api_response(
            text="Done after tool call",
            stop_reason="end_turn",
        )
        loop_runner._call_api = AsyncMock(side_effect=[tool_response, final_response])

        conv = _make_conversation([("user", "Use a tool")])
        response_text, tool_results, _usage, _ = await loop_runner._tool_loop(
            system_prompt="Test prompt",
            conversation=conv,
            frame_id="task",
        )

        assert response_text == "Done after tool call"
        assert len(tool_results) == 1
        assert tool_results[0].tool_name == "echo"
        assert tool_results[0].result == "Echo: test"
        assert tool_results[0].error is None
        assert tool_results[0].duration_ms is not None
        assert loop_runner._call_api.call_count == 2

    @pytest.mark.asyncio
    async def test_tool_use_dispatched_despite_end_turn_stop_reason(self, loop_runner):
        """opus-4.8 with adaptive/interleaved thinking returns tool_use blocks while
        reporting stop_reason='end_turn' (the message ends with a thinking block AFTER the
        tool_use). The loop MUST dispatch those tools, not terminate on the stop_reason —
        gating on stop_reason=='tool_use' silently dropped them, producing empty answers.
        Regression for the opus-4.8 non-streaming dropped-tool bug."""
        tool_response = _make_api_response(
            stop_reason="end_turn",   # the bug trigger: end_turn WITH a tool_use block
            tool_uses=[{"name": "echo", "input": {"message": "bridge"}, "id": "toolu_et"}],
        )
        final_response = _make_api_response(text="Answered after the tool", stop_reason="end_turn")
        loop_runner._call_api = AsyncMock(side_effect=[tool_response, final_response])

        conv = _make_conversation([("user", "needs a tool")])
        response_text, tool_results, _usage, _ = await loop_runner._tool_loop(
            system_prompt="Test prompt", conversation=conv, frame_id="task",
        )
        assert len(tool_results) == 1, "tool_use must dispatch even when stop_reason='end_turn'"
        assert tool_results[0].tool_name == "echo"
        assert response_text == "Answered after the tool"
        assert loop_runner._call_api.call_count == 2

    @pytest.mark.asyncio
    async def test_tool_loop_max_turns(self, loop_runner):
        """API always returns tool_use -> stops at max_turns (3) and makes final call."""
        # Always return tool_use (will hit max_turns=3)
        tool_response = _make_api_response(
            stop_reason="tool_use",
            tool_uses=[{"name": "echo", "input": {"message": "loop"}, "id": "toolu_loop"}],
        )
        # Final call (no tools) returns text
        final_text_response = _make_api_response(
            text="Reached max turns",
            stop_reason="end_turn",
        )
        # 3 tool_use responses + 1 final call = 4 total
        loop_runner._call_api = AsyncMock(
            side_effect=[tool_response, tool_response, tool_response, final_text_response]
        )

        conv = _make_conversation([("user", "Keep using tools")])
        response_text, tool_results, _usage, _ = await loop_runner._tool_loop(
            system_prompt="Test prompt",
            conversation=conv,
            frame_id="task",
        )

        # Should have dispatched echo 3 times
        assert len(tool_results) == 3
        # Final text comes from the last _call_api (no tools)
        assert response_text == "Reached max turns"
        # 3 loop iterations + 1 final call = 4 total API calls
        assert loop_runner._call_api.call_count == 4

    @pytest.mark.asyncio
    async def test_tool_loop_max_tokens(self, loop_runner):
        """API returns max_tokens stop_reason -> loop breaks, text extracted."""
        loop_runner._call_api = AsyncMock(return_value=_make_api_response(
            text="Partial response due to token limit",
            stop_reason="max_tokens",
        ))

        conv = _make_conversation([("user", "Long request")])
        response_text, tool_results, _usage, _ = await loop_runner._tool_loop(
            system_prompt="Test prompt",
            conversation=conv,
            frame_id="task",
        )

        assert response_text == "Partial response due to token limit"
        assert tool_results == []
        loop_runner._call_api.assert_called_once()

    @pytest.mark.asyncio
    async def test_tool_loop_parallel_tools(self, loop_runner):
        """API returns 2 tool_use blocks -> both dispatched, single user message with 2 tool_results."""
        # Response with two tool_use blocks (parallel tool calls)
        parallel_response = _make_api_response(
            stop_reason="tool_use",
            tool_uses=[
                {"name": "echo", "input": {"message": "first"}, "id": "toolu_par1"},
                {"name": "add", "input": {"a": 2, "b": 3}, "id": "toolu_par2"},
            ],
        )
        # Final response after tool results
        final_response = _make_api_response(
            text="Both tools completed",
            stop_reason="end_turn",
        )
        loop_runner._call_api = AsyncMock(side_effect=[parallel_response, final_response])

        conv = _make_conversation([("user", "Use both tools")])
        response_text, tool_results, _usage, _ = await loop_runner._tool_loop(
            system_prompt="Test prompt",
            conversation=conv,
            frame_id="task",
        )

        assert response_text == "Both tools completed"
        assert len(tool_results) == 2
        assert tool_results[0].tool_name == "echo"
        assert tool_results[0].result == "Echo: first"
        assert tool_results[1].tool_name == "add"
        assert tool_results[1].result == "5"  # 2 + 3
        assert loop_runner._call_api.call_count == 2

    @pytest.mark.asyncio
    async def test_tool_loop_preserves_messages(self, loop_runner):
        """Verify assistant response + tool_results are appended to messages array.

        After a tool_use round, messages should contain:
        1. Original user message (from conversation)
        2. Assistant response (full content blocks including tool_use)
        3. User message with tool_result blocks
        """
        tool_response = _make_api_response(
            stop_reason="tool_use",
            tool_uses=[{"name": "echo", "input": {"message": "msg"}, "id": "toolu_msg1"}],
        )
        final_response = _make_api_response(
            text="Final answer",
            stop_reason="end_turn",
        )
        loop_runner._call_api = AsyncMock(side_effect=[tool_response, final_response])

        conv = _make_conversation([("user", "Test message preservation")])
        await loop_runner._tool_loop(
            system_prompt="Test prompt",
            conversation=conv,
            frame_id="task",
        )

        # Inspect what _call_api received on its second call
        # The messages arg should contain the original user msg + assistant + tool_results
        second_call_args = loop_runner._call_api.call_args_list[1]
        messages = second_call_args.kwargs.get("messages") or second_call_args.args[1]

        # Should have: user message, assistant (tool_use), user (tool_results)
        assert len(messages) >= 3
        # Last message before the second API call should be a user message with tool_results
        tool_result_msg = messages[-1]
        assert tool_result_msg["role"] == "user"
        assert isinstance(tool_result_msg["content"], list)
        assert tool_result_msg["content"][0]["type"] == "tool_result"
        assert tool_result_msg["content"][0]["tool_use_id"] == "toolu_msg1"

        # Second-to-last should be assistant with tool_use content
        assistant_msg = messages[-2]
        assert assistant_msg["role"] == "assistant"

    @pytest.mark.asyncio
    async def test_tool_loop_no_dispatcher_raises(self, tool_settings):
        """_tool_loop without dispatcher set -> RuntimeError."""
        cognitive = MockCognitiveLayer()
        r = AgentRunner(cognitive, MockBrain(), MockHeart(), tool_settings)
        # Don't call set_dispatcher

        conv = _make_conversation([("user", "Hello")])
        with pytest.raises(RuntimeError, match="No tool dispatcher"):
            await r._tool_loop(
                system_prompt="Test",
                conversation=conv,
                frame_id="task",
            )
        await r.close()

    @pytest.mark.asyncio
    async def test_tool_loop_tool_dispatch_error(self, loop_runner):
        """Tool dispatch error -> tracked as error in tool_results, loop continues."""
        # Return a tool_use for an unknown tool, then end_turn
        unknown_tool_response = _make_api_response(
            stop_reason="tool_use",
            tool_uses=[{"name": "nonexistent_tool", "input": {}, "id": "toolu_unk"}],
        )
        final_response = _make_api_response(
            text="Handled error",
            stop_reason="end_turn",
        )
        loop_runner._call_api = AsyncMock(side_effect=[unknown_tool_response, final_response])

        conv = _make_conversation([("user", "Call unknown tool")])
        response_text, tool_results, _usage, _ = await loop_runner._tool_loop(
            system_prompt="Test prompt",
            conversation=conv,
            frame_id="task",
        )

        assert response_text == "Handled error"
        assert len(tool_results) == 1
        assert tool_results[0].tool_name == "nonexistent_tool"
        assert tool_results[0].error is not None
        assert "Unknown tool" in tool_results[0].error
