"""Tests for 005.4 streaming responses.

Tests cover:
- SSE event parsing (_parse_sse_event pure function)
- StreamEvent dataclass defaults and field setting
- Block accumulator logic (tool input JSON fragment reassembly)
- stream_chat() integration (mocked _call_api_stream + cognitive layer)
- Telegram StreamingMessage progressive editing with debounce
"""

import asyncio
import json
import logging
import time
from typing import Any, AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nous.api.runner import (
    AgentRunner,
    Conversation,
    Message,
    StreamEvent,
    _parse_sse_event,
)
from nous.cognitive.schemas import (
    FrameSelection,
    ToolResult,
    TurnContext,
    TurnResult,
)
from nous.telegram_bot import StreamingMessage


# ---------------------------------------------------------------------------
# TestParseSSEEvent — 10 pure function tests
# ---------------------------------------------------------------------------


class TestParseSSEEvent:
    """Tests for _parse_sse_event() — the pure function."""

    def test_text_delta(self):
        """text_delta parsed with correct text."""
        data = {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "Hello world"},
        }
        event = _parse_sse_event(data)
        assert event is not None
        assert event.type == "text_delta"
        assert event.text == "Hello world"

    def test_tool_use_start(self):
        """content_block_start with tool_use returns tool_start event."""
        data = {
            "type": "content_block_start",
            "index": 1,
            "content_block": {
                "type": "tool_use",
                "id": "toolu_abc123",
                "name": "web_search",
            },
        }
        event = _parse_sse_event(data)
        assert event is not None
        assert event.type == "tool_start"
        assert event.tool_name == "web_search"
        assert event.tool_id == "toolu_abc123"
        assert event.block_index == 1

    def test_text_block_start(self):
        """content_block_start with text returns text_block_start event."""
        data = {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        }
        event = _parse_sse_event(data)
        assert event is not None
        assert event.type == "text_block_start"
        assert event.block_index == 0

    def test_input_json_delta(self):
        """input_json_delta returns tool_input_delta with block_index."""
        data = {
            "type": "content_block_delta",
            "index": 2,
            "delta": {"type": "input_json_delta", "partial_json": '{"query":'},
        }
        event = _parse_sse_event(data)
        assert event is not None
        assert event.type == "tool_input_delta"
        assert event.text == '{"query":'
        assert event.block_index == 2

    def test_message_delta_stop_reason(self):
        """N3: stop_reason from message_delta.delta.stop_reason."""
        data = {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 42},
        }
        event = _parse_sse_event(data)
        assert event is not None
        assert event.type == "done"
        assert event.stop_reason == "end_turn"

    def test_block_stop(self):
        """content_block_stop returns block_stop with index."""
        data = {"type": "content_block_stop", "index": 1}
        event = _parse_sse_event(data)
        assert event is not None
        assert event.type == "block_stop"
        assert event.block_index == 1

    def test_ping_returns_none(self):
        """N4: ping events return None (skip gracefully)."""
        data = {"type": "ping"}
        event = _parse_sse_event(data)
        assert event is None

    def test_in_stream_error(self):
        """N2: error event parsed with type and message."""
        data = {
            "type": "error",
            "error": {
                "type": "overloaded_error",
                "message": "Overloaded",
            },
        }
        event = _parse_sse_event(data)
        assert event is not None
        assert event.type == "error"
        assert "overloaded_error" in event.text
        assert "Overloaded" in event.text

    def test_unknown_event_returns_none(self):
        """Unknown event types return None."""
        data = {"type": "some_future_event", "data": "whatever"}
        event = _parse_sse_event(data)
        assert event is None

    def test_message_stop(self):
        """message_stop event parsed."""
        data = {"type": "message_stop"}
        event = _parse_sse_event(data)
        assert event is not None
        assert event.type == "message_stop"


# ---------------------------------------------------------------------------
# TestBlockAccumulator — 4 integration-level tests
# ---------------------------------------------------------------------------


class TestBlockAccumulator:
    """Tests for per-block-index tool input JSON accumulation (N1).

    These test the accumulation logic as used inside stream_chat:
    block_accumulators dict maps block_index -> {id, name, input_parts}.
    On block_stop, input_parts are joined and JSON-parsed.
    """

    def _accumulate(
        self,
        events: list[StreamEvent],
    ) -> list[dict[str, Any]]:
        """Simulate the block accumulation logic from stream_chat.

        Returns list of completed tool calls with parsed input.
        """
        block_accumulators: dict[int, dict[str, Any]] = {}
        tool_calls: list[dict[str, Any]] = []

        for event in events:
            if event.type == "tool_start":
                block_accumulators[event.block_index] = {
                    "id": event.tool_id,
                    "name": event.tool_name,
                    "input_parts": [],
                }
            elif event.type == "tool_input_delta":
                acc = block_accumulators.get(event.block_index)
                if acc:
                    acc["input_parts"].append(event.text)
            elif event.type == "block_stop":
                acc = block_accumulators.pop(event.block_index, None)
                if acc:
                    input_json = "".join(acc["input_parts"])
                    try:
                        acc["input"] = json.loads(input_json) if input_json else {}
                    except json.JSONDecodeError:
                        acc["input"] = {}
                    tool_calls.append(acc)

        return tool_calls

    def test_single_tool_fragments_reassembled(self):
        """Single tool: JSON fragments joined and parsed."""
        events = [
            StreamEvent(type="tool_start", tool_id="t1", tool_name="web_search", block_index=1),
            StreamEvent(type="tool_input_delta", text='{"qu', block_index=1),
            StreamEvent(type="tool_input_delta", text='ery": ', block_index=1),
            StreamEvent(type="tool_input_delta", text='"test"}', block_index=1),
            StreamEvent(type="block_stop", block_index=1),
        ]
        result = self._accumulate(events)
        assert len(result) == 1
        assert result[0]["name"] == "web_search"
        assert result[0]["input"] == {"query": "test"}

    def test_multiple_tools_separate_accumulators(self):
        """Two parallel tools with different block_index stay separate."""
        events = [
            StreamEvent(type="tool_start", tool_id="t1", tool_name="web_search", block_index=1),
            StreamEvent(type="tool_start", tool_id="t2", tool_name="recall_deep", block_index=2),
            StreamEvent(type="tool_input_delta", text='{"query": "hello"}', block_index=1),
            StreamEvent(type="tool_input_delta", text='{"text": "memory"}', block_index=2),
            StreamEvent(type="block_stop", block_index=1),
            StreamEvent(type="block_stop", block_index=2),
        ]
        result = self._accumulate(events)
        assert len(result) == 2
        search = next(r for r in result if r["name"] == "web_search")
        recall = next(r for r in result if r["name"] == "recall_deep")
        assert search["input"] == {"query": "hello"}
        assert recall["input"] == {"text": "memory"}

    def test_empty_tool_input(self):
        """Empty input_parts -> {}."""
        events = [
            StreamEvent(type="tool_start", tool_id="t1", tool_name="bash", block_index=0),
            StreamEvent(type="block_stop", block_index=0),
        ]
        result = self._accumulate(events)
        assert len(result) == 1
        assert result[0]["input"] == {}

    def test_malformed_json_fallback(self):
        """Malformed JSON -> {} (graceful fallback)."""
        events = [
            StreamEvent(type="tool_start", tool_id="t1", tool_name="bash", block_index=0),
            StreamEvent(type="tool_input_delta", text='{"broken: json', block_index=0),
            StreamEvent(type="block_stop", block_index=0),
        ]
        result = self._accumulate(events)
        assert len(result) == 1
        assert result[0]["input"] == {}


# ---------------------------------------------------------------------------
# TestStreamEvent — 2 dataclass tests
# ---------------------------------------------------------------------------


class TestStreamEvent:
    """Tests for StreamEvent construction."""

    def test_defaults(self):
        """Verify default values."""
        event = StreamEvent(type="text_delta")
        assert event.type == "text_delta"
        assert event.text == ""
        assert event.tool_name == ""
        assert event.tool_id == ""
        assert event.tool_input == {}
        assert event.stop_reason == ""
        assert event.block_index == 0
        assert event.usage is None

    def test_all_fields(self):
        """All fields set correctly."""
        event = StreamEvent(
            type="tool_start",
            text="some text",
            tool_name="web_search",
            tool_id="toolu_123",
            tool_input={"query": "test"},
            stop_reason="end_turn",
            block_index=3,
        )
        assert event.type == "tool_start"
        assert event.text == "some text"
        assert event.tool_name == "web_search"
        assert event.tool_id == "toolu_123"
        assert event.tool_input == {"query": "test"}
        assert event.stop_reason == "end_turn"
        assert event.block_index == 3


# ---------------------------------------------------------------------------
# TestStreamChat — 12 mocked integration tests
# ---------------------------------------------------------------------------


def _make_mock_cognitive():
    """Create a mock cognitive layer with proper pre_turn/post_turn returns."""
    cognitive = AsyncMock()
    frame = FrameSelection(
        frame_id="conversation",
        frame_name="Conversation",
        confidence=0.9,
        match_method="keyword",
    )
    turn_context = MagicMock(spec=TurnContext)
    turn_context.frame = frame
    turn_context.system_prompt = "test system prompt"
    turn_context.decision_id = None
    turn_context.active_censors = []
    turn_context.recalled_decision_ids = []
    turn_context.recalled_fact_ids = []
    turn_context.recalled_episode_ids = []
    turn_context.recalled_procedure_ids = []
    turn_context.censor_blocked = False
    turn_context.censor_block_reason = None
    cognitive.pre_turn.return_value = turn_context
    cognitive.post_turn.return_value = MagicMock()
    return cognitive, turn_context


def _make_mock_settings():
    """Create a MagicMock Settings (NOT real Settings — pydantic validation issues)."""
    settings = MagicMock()
    settings.model = "claude-sonnet-4-5-20250514"
    settings.max_tokens = 4096
    settings.max_turns = 10
    settings.agent_id = "test-agent"
    settings.compaction_enabled = False
    settings.effective_compaction_threshold = 100000
    settings.effective_keep_recent_tokens = 10000
    settings.tool_pruning_enabled = False
    settings.tool_metadata_degrade_after = 8
    settings.tool_hard_clear_after = 12
    settings.keep_last_tool_results = 2
    settings.tool_timeout = 120
    settings.keepalive_interval = 10
    settings.recall_exclude_context_ids = False  # F071: bare MagicMock truthy by default
    settings.date_leg_enabled = False  # F075 L3: bare MagicMock truthy by default
    return settings


def _make_runner(cognitive, settings):
    """Create an AgentRunner with mocked internals."""
    brain = MagicMock()
    heart = MagicMock()
    runner = AgentRunner(cognitive, brain, heart, settings)

    # Mock dispatcher
    dispatcher = MagicMock()
    dispatcher.available_tools.return_value = [
        {"name": "web_search", "description": "Search", "input_schema": {}},
    ]
    dispatcher.dispatch = AsyncMock(return_value=("search result", False))
    runner.set_dispatcher(dispatcher)

    # Mock _build_system_prompt to return a simple string
    runner._build_system_prompt = MagicMock(return_value="test system prompt")

    # Mock _format_messages to return empty messages
    runner._format_messages = MagicMock(return_value=[{"role": "user", "content": "test"}])

    # Mock _check_safety_net (no-op)
    runner._check_safety_net = MagicMock()

    return runner


async def _make_stream(*events: StreamEvent) -> AsyncGenerator[StreamEvent, None]:
    """Create an async generator yielding the given events."""
    for e in events:
        yield e


class TestStreamChat:
    """Tests for AgentRunner.stream_chat() -- mocked API + cognitive layer."""

    @pytest.fixture
    def mock_cognitive(self):
        cognitive, turn_context = _make_mock_cognitive()
        return cognitive, turn_context

    @pytest.fixture
    def mock_settings(self):
        return _make_mock_settings()

    @pytest.fixture
    def runner(self, mock_cognitive, mock_settings):
        cognitive, _ = mock_cognitive
        return _make_runner(cognitive, mock_settings)

    @pytest.mark.asyncio
    async def test_simple_text_streams(self, runner, mock_cognitive):
        """Text-only response yields text_delta events then done."""
        cognitive, _ = mock_cognitive

        async def fake_stream(*args, **kwargs):
            yield StreamEvent(type="text_delta", text="Hello ")
            yield StreamEvent(type="text_delta", text="world")
            yield StreamEvent(type="done", stop_reason="end_turn")

        runner._call_api_stream = MagicMock(side_effect=fake_stream)

        events = [e async for e in runner.stream_chat("s1", "Hi")]
        text_events = [e for e in events if e.type == "text_delta"]
        done_events = [e for e in events if e.type == "done"]

        assert len(text_events) == 2
        assert text_events[0].text == "Hello "
        assert text_events[1].text == "world"
        assert len(done_events) >= 1

    @staticmethod
    async def _drain_via_tasks(agen):
        """Pump an async generator the way rest.py's SSE keepalive race does:
        each __anext__ wrapped in its own asyncio Task, so every resume runs
        in a FRESH copied context. This is what fragments contextvar context
        and exposed the F071 'Token was created in a different Context' crash.
        """
        events = []
        while True:
            try:
                events.append(await asyncio.ensure_future(agen.__anext__()))
            except StopAsyncIteration:
                break
        return events

    @pytest.mark.asyncio
    async def test_streaming_contextvar_survives_cross_context_pump_flag_off(
        self, runner, mock_cognitive
    ):
        """F071 regression (prod default, flag OFF): pumping stream_chat across
        per-chunk contexts must NOT raise the contextvar Token error, and the
        finally's post_turn must still run. Pre-fix, reset(token) raised in the
        finally (token born in a different __anext__ context) and skipped
        post_turn on every Telegram turn."""
        cognitive, _ = mock_cognitive

        async def fake_stream(*args, **kwargs):
            yield StreamEvent(type="text_delta", text="hi")
            yield StreamEvent(type="done", stop_reason="end_turn")

        runner._call_api_stream = MagicMock(side_effect=fake_stream)

        events = await self._drain_via_tasks(runner.stream_chat("s-ctx-off", "Hi"))

        assert any(e.type == "done" for e in events)  # clean completion, finally didn't raise
        cognitive.post_turn.assert_awaited()  # finally ran to completion

    @pytest.mark.asyncio
    async def test_streaming_contextvar_survives_cross_context_pump_flag_on(
        self, mock_cognitive
    ):
        """F071 regression with the feature ON: the contextvar is engaged, but
        value-based restore (set(None), not reset(token)) must still not raise
        when the generator is pumped across contexts.

        Scope: this asserts no crash + post_turn runs. It does NOT assert that
        exclusions are *applied* mid-stream — they aren't on the SSE path (the
        set() is invisible after the first yield; documented limitation in
        stream_chat). This is a crash regression, not a feature-correctness test.
        """
        cognitive, _ = mock_cognitive
        settings = _make_mock_settings()
        settings.recall_exclude_context_ids = True  # F071 ON
        runner = _make_runner(cognitive, settings)

        async def fake_stream(*args, **kwargs):
            yield StreamEvent(type="text_delta", text="hi")
            yield StreamEvent(type="done", stop_reason="end_turn")

        runner._call_api_stream = MagicMock(side_effect=fake_stream)

        events = await self._drain_via_tasks(runner.stream_chat("s-ctx-on", "Hi"))

        assert any(e.type == "done" for e in events)
        cognitive.post_turn.assert_awaited()

    @pytest.mark.asyncio
    async def test_tool_call_mid_stream(self, runner, mock_cognitive):
        """Tool call: accumulate input, execute, resume streaming."""
        cognitive, _ = mock_cognitive
        call_count = 0

        async def fake_stream(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First stream: text + tool call
                yield StreamEvent(type="text_delta", text="Let me search")
                yield StreamEvent(type="tool_start", tool_name="web_search", tool_id="t1", block_index=1)
                yield StreamEvent(type="tool_input_delta", text='{"query": "test"}', block_index=1)
                yield StreamEvent(type="block_stop", block_index=1)
                yield StreamEvent(type="done", stop_reason="tool_use")
            else:
                # Second stream: final text response
                yield StreamEvent(type="text_delta", text="Here are the results")
                yield StreamEvent(type="done", stop_reason="end_turn")

        runner._call_api_stream = MagicMock(side_effect=fake_stream)

        events = [e async for e in runner.stream_chat("s1", "Search for test")]
        text_events = [e for e in events if e.type == "text_delta"]
        tool_starts = [e for e in events if e.type == "tool_start"]

        assert len(text_events) >= 2  # "Let me search" + "Here are the results"
        assert len(tool_starts) >= 1
        assert runner._dispatcher.dispatch.called

    @pytest.mark.asyncio
    async def test_multi_tool_parallel(self, runner, mock_cognitive):
        """Two tool_use blocks in one response dispatched correctly."""
        cognitive, _ = mock_cognitive
        call_count = 0

        async def fake_stream(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield StreamEvent(type="tool_start", tool_name="web_search", tool_id="t1", block_index=1)
                yield StreamEvent(type="tool_input_delta", text='{"query": "a"}', block_index=1)
                yield StreamEvent(type="tool_start", tool_name="recall_deep", tool_id="t2", block_index=2)
                yield StreamEvent(type="tool_input_delta", text='{"text": "b"}', block_index=2)
                yield StreamEvent(type="block_stop", block_index=1)
                yield StreamEvent(type="block_stop", block_index=2)
                yield StreamEvent(type="done", stop_reason="tool_use")
            else:
                yield StreamEvent(type="text_delta", text="Done")
                yield StreamEvent(type="done", stop_reason="end_turn")

        runner._call_api_stream = MagicMock(side_effect=fake_stream)

        events = [e async for e in runner.stream_chat("s1", "Search and recall")]

        # Two tool dispatches should have occurred
        assert runner._dispatcher.dispatch.call_count == 2

    @pytest.mark.asyncio
    async def test_api_error_propagated(self, runner, mock_cognitive):
        """HTTP error yields error event."""
        cognitive, _ = mock_cognitive

        async def fake_stream(*args, **kwargs):
            yield StreamEvent(type="error", text="HTTP 500: Internal Server Error")

        runner._call_api_stream = MagicMock(side_effect=fake_stream)

        events = [e async for e in runner.stream_chat("s1", "Hi")]
        error_events = [e for e in events if e.type == "error"]

        assert len(error_events) >= 1
        assert "500" in error_events[0].text

    @pytest.mark.asyncio
    async def test_in_stream_error_propagated(self, runner, mock_cognitive):
        """N2: In-stream error event propagated and stream stops."""
        cognitive, _ = mock_cognitive

        async def fake_stream(*args, **kwargs):
            yield StreamEvent(type="text_delta", text="Starting...")
            yield StreamEvent(type="error", text="overloaded_error: Overloaded")

        runner._call_api_stream = MagicMock(side_effect=fake_stream)

        events = [e async for e in runner.stream_chat("s1", "Hi")]
        error_events = [e for e in events if e.type == "error"]

        assert len(error_events) >= 1
        assert "overloaded" in error_events[0].text.lower()

    @pytest.mark.asyncio
    async def test_max_turns_safety(self, runner, mock_cognitive, mock_settings):
        """Max turns reached -> final no-tools call."""
        cognitive, _ = mock_cognitive
        mock_settings.max_turns = 1  # Low limit

        # Always return tool_use to force max_turns
        async def fake_stream(*args, **kwargs):
            yield StreamEvent(type="tool_start", tool_name="web_search", tool_id="t1", block_index=1)
            yield StreamEvent(type="tool_input_delta", text='{"query": "x"}', block_index=1)
            yield StreamEvent(type="block_stop", block_index=1)
            yield StreamEvent(type="done", stop_reason="tool_use")

        runner._call_api_stream = MagicMock(side_effect=fake_stream)

        # Mock _call_api for the final no-tools call
        from nous.api.runner import ApiResponse
        runner._call_api = AsyncMock(return_value=ApiResponse(
            content=[{"type": "text", "text": "Max turns reached."}],
            stop_reason="end_turn",
        ))

        events = [e async for e in runner.stream_chat("s1", "Loop me")]
        # Should complete without infinite loop
        assert any(e.type == "done" for e in events)

    @pytest.mark.asyncio
    async def test_post_turn_always_called(self, runner, mock_cognitive):
        """post_turn fires even on error (try/finally)."""
        cognitive, _ = mock_cognitive

        async def fake_stream(*args, **kwargs):
            yield StreamEvent(type="error", text="Something broke")

        runner._call_api_stream = MagicMock(side_effect=fake_stream)

        events = [e async for e in runner.stream_chat("s1", "Hi")]

        # post_turn must have been called
        cognitive.post_turn.assert_called_once()

    @pytest.mark.asyncio
    async def test_tool_results_tracked(self, runner, mock_cognitive):
        """ToolResult objects built with timing for post_turn."""
        cognitive, _ = mock_cognitive
        call_count = 0

        async def fake_stream(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield StreamEvent(type="tool_start", tool_name="web_search", tool_id="t1", block_index=1)
                yield StreamEvent(type="tool_input_delta", text='{"query": "x"}', block_index=1)
                yield StreamEvent(type="block_stop", block_index=1)
                yield StreamEvent(type="done", stop_reason="tool_use")
            else:
                yield StreamEvent(type="text_delta", text="Done")
                yield StreamEvent(type="done", stop_reason="end_turn")

        runner._call_api_stream = MagicMock(side_effect=fake_stream)

        events = [e async for e in runner.stream_chat("s1", "Search")]

        # post_turn should receive TurnResult with tool_results
        cognitive.post_turn.assert_called_once()
        call_args = cognitive.post_turn.call_args
        turn_result = call_args[0][2]  # 3rd positional arg
        assert isinstance(turn_result, TurnResult)
        assert len(turn_result.tool_results) == 1
        assert turn_result.tool_results[0].tool_name == "web_search"
        assert turn_result.tool_results[0].duration_ms is not None

    @pytest.mark.asyncio
    async def test_malformed_tool_json_logs_warning_and_skips_dispatch(
        self, runner, mock_cognitive, caplog
    ):
        """Malformed tool-input JSON is logged (tool name + payload size) and the
        tool is never dispatched -- a call with blanked-out args can only fail
        downstream with a misleading TypeError, so it must not reach the dispatcher.

        The warning must stay structural: tool inputs carry commands, file bodies
        and message text, so echoing the payload would turn every malformed call
        into a disclosure at the prod default log level."""
        cognitive, _ = mock_cognitive
        call_count = 0

        async def fake_stream(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield StreamEvent(
                    type="tool_start", tool_name="record_decision", tool_id="t1", block_index=1
                )
                yield StreamEvent(
                    type="tool_input_delta",
                    text='{"decision": "do the thing", "reasoning": "sk-secret-xyz because...',
                    block_index=1,
                )
                yield StreamEvent(type="block_stop", block_index=1)
                yield StreamEvent(type="done", stop_reason="tool_use")
            else:
                yield StreamEvent(type="text_delta", text="Retrying")
                yield StreamEvent(type="done", stop_reason="end_turn")

        runner._call_api_stream = MagicMock(side_effect=fake_stream)

        with caplog.at_level(logging.WARNING, logger="nous.api.runner"):
            events = [e async for e in runner.stream_chat("s1", "Decide something")]

        assert not runner._dispatcher.dispatch.called

        matching = [r for r in caplog.records if "record_decision" in r.getMessage()]
        assert matching, "expected a warning naming the tool whose input failed to parse"
        message = matching[0].getMessage()
        assert "bytes" in message
        assert "offset" in message
        # Structural diagnostics only -- no payload content, ever.
        assert "sk-secret-xyz" not in message
        assert "do the thing" not in message

    @pytest.mark.asyncio
    async def test_malformed_tool_json_produces_paired_error_result(
        self, runner, mock_cognitive
    ):
        """Malformed tool-input JSON must still produce a tool_result paired to
        its tool_use block (or the next API call 400s), and that result must tell
        the model the call was not executed rather than surfacing a TypeError."""
        cognitive, _ = mock_cognitive
        call_count = 0

        async def fake_stream(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield StreamEvent(
                    type="tool_start", tool_name="record_decision", tool_id="t1", block_index=1
                )
                yield StreamEvent(type="tool_input_delta", text='{"broken', block_index=1)
                yield StreamEvent(type="block_stop", block_index=1)
                yield StreamEvent(type="done", stop_reason="tool_use")
            else:
                yield StreamEvent(type="text_delta", text="Retrying")
                yield StreamEvent(type="done", stop_reason="end_turn")

        runner._call_api_stream = MagicMock(side_effect=fake_stream)

        events = [e async for e in runner.stream_chat("s1", "Decide something")]

        # The second _call_api_stream invocation is the follow-up turn; its
        # `messages` argument must include a tool_result paired to tool_use id "t1".
        assert runner._call_api_stream.call_count == 2
        second_call_messages = runner._call_api_stream.call_args_list[1][0][1]
        tool_result_messages = [
            m for m in second_call_messages
            if m.get("role") == "user" and isinstance(m.get("content"), list)
            and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in m["content"])
        ]
        assert len(tool_result_messages) == 1
        results = tool_result_messages[0]["content"]
        assert len(results) == 1  # exactly one result for the one tool_use block
        assert results[0]["tool_use_id"] == "t1"
        assert results[0]["is_error"] is True
        assert "not executed" in results[0]["content"]
        assert "Re-emit" in results[0]["content"]

        # post_turn's TurnResult reflects the failure, never a dispatch attempt
        cognitive.post_turn.assert_called_once()
        turn_result = cognitive.post_turn.call_args[0][2]
        assert len(turn_result.tool_results) == 1
        assert turn_result.tool_results[0].tool_name == "record_decision"
        assert turn_result.tool_results[0].result is None
        assert turn_result.tool_results[0].error is not None
        assert not runner._dispatcher.dispatch.called

    @pytest.mark.asyncio
    async def test_valid_tool_json_dispatch_unchanged(self, runner, mock_cognitive):
        """Regression guard: well-formed tool input dispatches exactly as before
        the malformed-JSON handling was added (success path byte-for-byte)."""
        cognitive, _ = mock_cognitive
        call_count = 0

        async def fake_stream(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield StreamEvent(type="tool_start", tool_name="web_search", tool_id="t1", block_index=1)
                yield StreamEvent(type="tool_input_delta", text='{"query": "test"}', block_index=1)
                yield StreamEvent(type="block_stop", block_index=1)
                yield StreamEvent(type="done", stop_reason="tool_use")
            else:
                yield StreamEvent(type="text_delta", text="Done")
                yield StreamEvent(type="done", stop_reason="end_turn")

        runner._call_api_stream = MagicMock(side_effect=fake_stream)

        events = [e async for e in runner.stream_chat("s1", "Search for test")]

        runner._dispatcher.dispatch.assert_called_once_with(
            "web_search", {"query": "test"}, session_id="s1", turn_number=1,
        )
        cognitive.post_turn.assert_called_once()
        turn_result = cognitive.post_turn.call_args[0][2]
        assert len(turn_result.tool_results) == 1
        assert turn_result.tool_results[0].tool_name == "web_search"
        assert turn_result.tool_results[0].error is None
        assert turn_result.tool_results[0].result == "search result"

    @pytest.mark.asyncio
    async def test_empty_tool_input_still_dispatches(self, runner, mock_cognitive):
        """Empty input string is the legitimate no-arguments case: it must keep
        parsing to {} and dispatching normally, not be treated as malformed."""
        cognitive, _ = mock_cognitive
        call_count = 0

        async def fake_stream(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield StreamEvent(type="tool_start", tool_name="web_search", tool_id="t1", block_index=1)
                yield StreamEvent(type="block_stop", block_index=1)
                yield StreamEvent(type="done", stop_reason="tool_use")
            else:
                yield StreamEvent(type="text_delta", text="Done")
                yield StreamEvent(type="done", stop_reason="end_turn")

        runner._call_api_stream = MagicMock(side_effect=fake_stream)

        events = [e async for e in runner.stream_chat("s1", "Search")]

        runner._dispatcher.dispatch.assert_called_once_with(
            "web_search", {}, session_id="s1", turn_number=1,
        )
        cognitive.post_turn.assert_called_once()
        turn_result = cognitive.post_turn.call_args[0][2]
        assert turn_result.tool_results[0].error is None

    @pytest.mark.asyncio
    async def test_safety_net_called(self, runner, mock_cognitive):
        """_check_safety_net runs after streaming."""
        cognitive, _ = mock_cognitive

        async def fake_stream(*args, **kwargs):
            yield StreamEvent(type="text_delta", text="Hello")
            yield StreamEvent(type="done", stop_reason="end_turn")

        runner._call_api_stream = MagicMock(side_effect=fake_stream)

        events = [e async for e in runner.stream_chat("s1", "Hi")]

        runner._check_safety_net.assert_called_once()

    @pytest.mark.asyncio
    async def test_conversation_messages_stored(self, runner, mock_cognitive):
        """Message objects (not dicts) stored in conversation."""
        cognitive, _ = mock_cognitive

        async def fake_stream(*args, **kwargs):
            yield StreamEvent(type="text_delta", text="Response")
            yield StreamEvent(type="done", stop_reason="end_turn")

        runner._call_api_stream = MagicMock(side_effect=fake_stream)

        events = [e async for e in runner.stream_chat("s1", "Hello")]

        conv = runner._conversations["s1"]
        assert len(conv.messages) >= 2
        assert isinstance(conv.messages[0], Message)
        assert conv.messages[0].role == "user"
        assert conv.messages[0].content == "Hello"
        assert isinstance(conv.messages[-1], Message)
        assert conv.messages[-1].role == "assistant"

    @pytest.mark.asyncio
    async def test_stream_chat_touches_session_monitor(self, runner, mock_cognitive):
        """stream_chat syncs activity at request start (Codex P1 on PR #424).

        Mirrors test_run_turn_touches_session_monitor — without this call,
        a long streaming turn for a session whose _last_activity was already
        past threshold can be closed mid-stream by a monitor tick.
        """
        from unittest.mock import MagicMock as _MagicMock

        async def fake_stream(*args, **kwargs):
            yield StreamEvent(type="text_delta", text="Hi")
            yield StreamEvent(type="done", stop_reason="end_turn")

        runner._call_api_stream = MagicMock(side_effect=fake_stream)

        monitor = _MagicMock()
        runner._session_monitor = monitor

        _events = [
            e async for e in runner.stream_chat("s-stream", "Hello", agent_id="agent-x")
        ]

        monitor.touch.assert_called_once_with("s-stream", "agent-x")

    @pytest.mark.asyncio
    async def test_agent_id_plumbed(self, runner, mock_cognitive):
        """agent_id passed to pre_turn and post_turn."""
        cognitive, _ = mock_cognitive

        async def fake_stream(*args, **kwargs):
            yield StreamEvent(type="text_delta", text="Hi")
            yield StreamEvent(type="done", stop_reason="end_turn")

        runner._call_api_stream = MagicMock(side_effect=fake_stream)

        events = [e async for e in runner.stream_chat("s1", "Hello", agent_id="custom-agent")]

        # pre_turn should receive the custom agent_id
        pre_call_args = cognitive.pre_turn.call_args
        assert pre_call_args[0][0] == "custom-agent"

        # post_turn should receive the custom agent_id
        post_call_args = cognitive.post_turn.call_args
        assert post_call_args[0][0] == "custom-agent"

    @pytest.mark.asyncio
    async def test_tool_exception_handled(self, runner, mock_cognitive):
        """Tool dispatch exception caught, is_error=True."""
        cognitive, _ = mock_cognitive
        call_count = 0

        async def fake_stream(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield StreamEvent(type="tool_start", tool_name="bash", tool_id="t1", block_index=1)
                yield StreamEvent(type="tool_input_delta", text='{"command": "fail"}', block_index=1)
                yield StreamEvent(type="block_stop", block_index=1)
                yield StreamEvent(type="done", stop_reason="tool_use")
            else:
                yield StreamEvent(type="text_delta", text="Recovered")
                yield StreamEvent(type="done", stop_reason="end_turn")

        runner._call_api_stream = MagicMock(side_effect=fake_stream)
        runner._dispatcher.dispatch = AsyncMock(side_effect=RuntimeError("Command failed"))

        events = [e async for e in runner.stream_chat("s1", "Run command")]

        # post_turn should have ToolResult with is_error=True (error field set)
        cognitive.post_turn.assert_called_once()
        call_args = cognitive.post_turn.call_args
        turn_result = call_args[0][2]
        assert len(turn_result.tool_results) == 1
        assert turn_result.tool_results[0].error is not None
        assert "Command failed" in turn_result.tool_results[0].error


# ---------------------------------------------------------------------------
# TestStreamingMessage — 6 Telegram progressive editing tests
# ---------------------------------------------------------------------------


class TestStreamingMessage:
    """Tests for StreamingMessage progressive editing."""

    def _make_bot(self):
        """Create a mock NousTelegramBot."""
        bot = MagicMock()
        bot._send = AsyncMock(return_value={"message_id": 42})
        bot._tg = AsyncMock(return_value={})
        return bot

    @pytest.mark.asyncio
    async def test_first_update_creates_message(self):
        """First call sends new message via _send."""
        bot = self._make_bot()
        sm = StreamingMessage(bot, chat_id=123)

        await sm.update("Hello")

        bot._send.assert_called_once_with(123, "Hello", parse_mode=None)
        assert sm.message_id == 42

    @pytest.mark.asyncio
    async def test_subsequent_updates_edit(self):
        """Later calls edit existing message via _tg editMessageText."""
        bot = self._make_bot()
        sm = StreamingMessage(bot, chat_id=123)

        # First update: creates message
        with patch("time.time", return_value=1000.0):
            await sm.update("Hello")

        assert sm.message_id == 42

        # Second update: should edit (after debounce interval)
        with patch("time.time", return_value=1002.0):
            await sm.update("Hello world")

        bot._tg.assert_called_with(
            "editMessageText",
            params={
                "chat_id": 123,
                "message_id": 42,
                "text": "Hello world",
            },
        )

    @pytest.mark.asyncio
    async def test_debounce_respects_interval(self):
        """Updates within 1.2s are deferred."""
        bot = self._make_bot()
        sm = StreamingMessage(bot, chat_id=123)

        # First update at t=1000
        with patch("time.time", return_value=1000.0):
            await sm.update("Hello")

        bot._send.assert_called_once()
        initial_send_count = bot._send.call_count
        initial_tg_count = bot._tg.call_count

        # Second update at t=1000.5 (within 1.2s debounce)
        with patch("time.time", return_value=1000.5):
            await sm.update("Hello world")

        # Should NOT have sent or edited — just deferred
        assert bot._send.call_count == initial_send_count
        assert bot._tg.call_count == initial_tg_count
        assert sm._pending is True

    @pytest.mark.asyncio
    async def test_finalize_sends_pending(self):
        """Finalize flushes deferred updates."""
        bot = self._make_bot()
        sm = StreamingMessage(bot, chat_id=123)

        # First update: creates message
        with patch("time.time", return_value=1000.0):
            await sm.update("Hello")

        # Second update: deferred (within debounce)
        with patch("time.time", return_value=1000.5):
            await sm.update("Hello world")

        assert sm._pending is True

        # Finalize should flush the pending update
        with patch("time.time", return_value=1000.6):
            await sm.finalize()

        # Should have edited the message with the latest text
        bot._tg.assert_called_with(
            "editMessageText",
            params={
                "chat_id": 123,
                "message_id": 42,
                "text": "Hello world",
                "parse_mode": "HTML",
            },
        )

    @pytest.mark.asyncio
    async def test_tool_indicator_grouped(self):
        """Tool indicators are grouped with counts instead of appended."""
        bot = self._make_bot()
        sm = StreamingMessage(bot, chat_id=123)

        # First: create a message
        with patch("time.time", return_value=1000.0):
            await sm.update("Thinking")

        # Append same tool indicator multiple times
        with patch("time.time", return_value=1002.0):
            await sm.append_tool_indicator("bash")
        with patch("time.time", return_value=1004.0):
            await sm.append_tool_indicator("bash")
        with patch("time.time", return_value=1006.0):
            await sm.append_tool_indicator("bash")

        assert "Running... (3)" in sm.text
        # Should NOT have 3 separate lines
        assert sm.text.count("Running") == 1

    @pytest.mark.asyncio
    async def test_tool_indicator_mixed_types(self):
        """Different tool types shown separately."""
        bot = self._make_bot()
        sm = StreamingMessage(bot, chat_id=123)

        with patch("time.time", return_value=1000.0):
            await sm.update("Working")

        with patch("time.time", return_value=1002.0):
            await sm.append_tool_indicator("web_search")
        with patch("time.time", return_value=1004.0):
            await sm.append_tool_indicator("bash")
        with patch("time.time", return_value=1006.0):
            await sm.append_tool_indicator("bash")

        assert "Searching..." in sm.text
        assert "Running... (2)" in sm.text

    @pytest.mark.asyncio
    async def test_finalize_clears_indicators_adds_usage(self):
        """Finalize removes tool indicators and adds usage footer."""
        bot = self._make_bot()
        sm = StreamingMessage(bot, chat_id=123)

        with patch("time.time", return_value=1000.0):
            await sm.update("Result")

        with patch("time.time", return_value=1002.0):
            await sm.append_tool_indicator("bash")

        sm.set_usage({"input_tokens": 2847, "output_tokens": 512})

        with patch("time.time", return_value=1004.0):
            await sm.finalize()

        # Tool indicators should be gone
        assert "Running" not in sm.text
        # Usage footer should be present
        assert "2.8K in / 512 out" in sm.text

    @pytest.mark.asyncio
    async def test_overflow_splits_message(self):
        """N7: Messages >4096 chars split into new message."""
        bot = self._make_bot()
        sm = StreamingMessage(bot, chat_id=123)

        # Create initial message
        with patch("time.time", return_value=1000.0):
            await sm.update("Start")
        assert sm.message_id == 42

        # Set up bot._send to return a new message_id for the overflow
        bot._send = AsyncMock(return_value={"message_id": 99})

        # Update with text exceeding 4000 chars
        long_text = "x" * 4100
        with patch("time.time", return_value=1002.0):
            await sm.update(long_text)

        # Should have called editMessageText with truncated text
        edit_calls = [
            c for c in bot._tg.call_args_list
            if c[0][0] == "editMessageText"
        ]
        assert len(edit_calls) >= 1
        # The truncated text should end with "(continued...)"
        edit_text = edit_calls[-1][1]["params"]["text"]
        assert "(continued...)" in edit_text

        # Should have sent overflow as new message
        bot._send.assert_called()
        # New message_id should be stored
        assert sm.message_id == 99
