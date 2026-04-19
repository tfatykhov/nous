"""F048: Tests for call_streaming_aggregated on both Anthropic client backends.

Pure-unit tests: no real API is contacted. The httpx aggregator is fed a
canned StreamEvent iterator; the SDK aggregator is fed a mocked messages.stream()
async context manager.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from nous.api.anthropic_client import (
    HttpxAnthropicClient,
    SdkAnthropicClient,
    StreamEvent,
)
from nous.config import Settings


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _settings() -> Settings:
    return Settings(
        ANTHROPIC_API_KEY="test-key",
        api_background_timeout_read=600,
    )


def _canned_stream(events: list[StreamEvent]):
    """Return an async generator that yields the supplied StreamEvents in order."""

    async def _gen(payload=None, **_kwargs):
        for ev in events:
            yield ev

    return _gen


# ---------------------------------------------------------------------------
# HttpxAnthropicClient.call_streaming_aggregated
# ---------------------------------------------------------------------------


async def test_httpx_aggregator_reconstructs_text_blocks(monkeypatch):
    """F048: text_block_start → text_delta × 2 → block_stop → done ⇒ single
    text block with concatenated content, stop_reason + usage propagated."""
    client = HttpxAnthropicClient(_settings())
    client._http = MagicMock()  # bypass start() guard

    events = [
        StreamEvent(
            type="message_start",
            usage={"input_tokens": 25, "cache_read_input_tokens": 0},
        ),
        StreamEvent(type="text_block_start", block_index=0),
        StreamEvent(type="text_delta", text="Hello, "),
        StreamEvent(type="text_delta", text="world!"),
        StreamEvent(type="block_stop", block_index=0),
        StreamEvent(type="done", stop_reason="end_turn", usage={"output_tokens": 12}),
    ]
    monkeypatch.setattr(client, "stream", _canned_stream(events))

    resp = await client.call_streaming_aggregated({"model": "x", "messages": []})

    assert resp.content == [{"type": "text", "text": "Hello, world!"}]
    assert resp.stop_reason == "end_turn"
    assert resp.usage is not None
    assert resp.usage["input_tokens"] == 25
    assert resp.usage["output_tokens"] == 12


async def test_httpx_aggregator_reconstructs_tool_use_blocks(monkeypatch):
    """F048: tool_start + fragmented JSON input_deltas are re-joined and
    parsed into a single tool_use block."""
    client = HttpxAnthropicClient(_settings())
    client._http = MagicMock()

    events = [
        StreamEvent(type="message_start", usage={"input_tokens": 50}),
        StreamEvent(
            type="tool_start",
            tool_name="learn_fact",
            tool_id="toolu_abc",
            block_index=0,
        ),
        StreamEvent(type="tool_input_delta", text='{"arg": ', block_index=0),
        StreamEvent(type="tool_input_delta", text="42}", block_index=0),
        StreamEvent(type="block_stop", block_index=0),
        StreamEvent(type="done", stop_reason="tool_use", usage={"output_tokens": 8}),
    ]
    monkeypatch.setattr(client, "stream", _canned_stream(events))

    resp = await client.call_streaming_aggregated({"model": "x", "messages": []})

    assert len(resp.content) == 1
    block = resp.content[0]
    assert block["type"] == "tool_use"
    assert block["id"] == "toolu_abc"
    assert block["name"] == "learn_fact"
    assert block["input"] == {"arg": 42}
    assert resp.stop_reason == "tool_use"


async def test_httpx_aggregator_mixed_text_and_tool_blocks(monkeypatch):
    """F048: a response with both a text block and a tool_use block returns
    both in content, ordered by block_index."""
    client = HttpxAnthropicClient(_settings())
    client._http = MagicMock()

    events = [
        StreamEvent(type="message_start", usage={"input_tokens": 30}),
        # Text block at index 0
        StreamEvent(type="text_block_start", block_index=0),
        StreamEvent(type="text_delta", text="Thinking aloud..."),
        StreamEvent(type="block_stop", block_index=0),
        # Tool_use block at index 1
        StreamEvent(
            type="tool_start",
            tool_name="recall_deep",
            tool_id="toolu_xyz",
            block_index=1,
        ),
        StreamEvent(type="tool_input_delta", text='{"query": "q"}', block_index=1),
        StreamEvent(type="block_stop", block_index=1),
        StreamEvent(type="done", stop_reason="tool_use", usage={"output_tokens": 15}),
    ]
    monkeypatch.setattr(client, "stream", _canned_stream(events))

    resp = await client.call_streaming_aggregated({"model": "x", "messages": []})

    assert len(resp.content) == 2
    assert resp.content[0]["type"] == "text"
    assert resp.content[0]["text"] == "Thinking aloud..."
    assert resp.content[1]["type"] == "tool_use"
    assert resp.content[1]["name"] == "recall_deep"
    assert resp.content[1]["input"] == {"query": "q"}


async def test_httpx_aggregator_preserves_cache_tokens_from_message_start(monkeypatch):
    """F048 P1-3: message_start carries cache_*_input_tokens; message_delta
    carries only output_tokens. Aggregated usage must keep the cache fields
    intact rather than clobbering them to 0."""
    client = HttpxAnthropicClient(_settings())
    client._http = MagicMock()

    events = [
        StreamEvent(
            type="message_start",
            usage={
                "input_tokens": 100,
                "cache_read_input_tokens": 200,
                "cache_creation_input_tokens": 50,
            },
        ),
        StreamEvent(type="text_block_start", block_index=0),
        StreamEvent(type="text_delta", text="ok"),
        StreamEvent(type="block_stop", block_index=0),
        # Delta event reports ONLY output_tokens (most Anthropic responses do this).
        StreamEvent(type="done", stop_reason="end_turn", usage={"output_tokens": 30}),
    ]
    monkeypatch.setattr(client, "stream", _canned_stream(events))

    resp = await client.call_streaming_aggregated({"model": "x", "messages": []})

    assert resp.usage is not None
    assert resp.usage["input_tokens"] == 100
    assert resp.usage["output_tokens"] == 30
    # Critical: cache fields must survive the done-event merge.
    assert resp.usage["cache_read_input_tokens"] == 200
    assert resp.usage["cache_creation_input_tokens"] == 50


async def test_httpx_aggregator_raises_on_error_event(monkeypatch):
    """F048: an error StreamEvent mid-stream raises RuntimeError with the text."""
    client = HttpxAnthropicClient(_settings())
    client._http = MagicMock()

    events = [
        StreamEvent(type="message_start", usage={"input_tokens": 10}),
        StreamEvent(type="error", text="rate_limit: slow down"),
    ]
    monkeypatch.setattr(client, "stream", _canned_stream(events))

    with pytest.raises(RuntimeError, match="rate_limit"):
        await client.call_streaming_aggregated({"model": "x", "messages": []})


async def test_httpx_aggregator_raises_on_truncated_stream_without_terminal_event(monkeypatch):
    """F048 silent-failure fix: a stream that ends cleanly but never emits
    `done` or `message_stop` (e.g. proxy truncation on HTTP 200) must raise,
    not silently return an empty ApiResponse."""
    client = HttpxAnthropicClient(_settings())
    client._http = MagicMock()

    events = [
        StreamEvent(type="message_start", usage={"input_tokens": 40}),
        StreamEvent(type="text_block_start", block_index=0),
        StreamEvent(type="text_delta", text="partial"),
        # NO block_stop, NO done, NO message_stop — stream cut off.
    ]
    monkeypatch.setattr(client, "stream", _canned_stream(events))

    with pytest.raises(RuntimeError, match="truncated|terminal event"):
        await client.call_streaming_aggregated({"model": "x", "messages": []})


async def test_httpx_aggregator_accepts_message_stop_as_terminal(monkeypatch):
    """F048 silent-failure fix: message_stop alone is a valid terminal event."""
    client = HttpxAnthropicClient(_settings())
    client._http = MagicMock()

    events = [
        StreamEvent(type="message_start", usage={"input_tokens": 10}),
        StreamEvent(type="text_block_start", block_index=0),
        StreamEvent(type="text_delta", text="done"),
        StreamEvent(type="block_stop", block_index=0),
        # No "done" event — only message_stop.
        StreamEvent(type="message_stop"),
    ]
    monkeypatch.setattr(client, "stream", _canned_stream(events))

    resp = await client.call_streaming_aggregated({"model": "x", "messages": []})
    assert resp.content == [{"type": "text", "text": "done"}]
    # stop_reason defaults to end_turn when only message_stop (no done) seen.
    assert resp.stop_reason == "end_turn"


async def test_httpx_aggregator_logs_warning_on_malformed_tool_input_json(monkeypatch, caplog):
    """F048 P1: when joined tool_input fragments fail json.loads, aggregator
    must log a WARNING (not swallow silently) and fall back to input={}."""
    import logging

    client = HttpxAnthropicClient(_settings())
    client._http = MagicMock()

    events = [
        StreamEvent(type="message_start", usage={"input_tokens": 10}),
        StreamEvent(
            type="tool_start",
            tool_name="bash",
            tool_id="toolu_bad",
            block_index=0,
        ),
        StreamEvent(type="tool_input_delta", text='{"cmd": "ls"', block_index=0),
        # Malformed — missing closing brace
        StreamEvent(type="block_stop", block_index=0),
        StreamEvent(type="done", stop_reason="tool_use", usage={"output_tokens": 5}),
    ]
    monkeypatch.setattr(client, "stream", _canned_stream(events))

    with caplog.at_level(logging.WARNING, logger="nous.api.anthropic_client"):
        resp = await client.call_streaming_aggregated({"model": "x", "messages": []})

    block = resp.content[0]
    assert block["type"] == "tool_use"
    assert block["input"] == {}  # Fallback
    # Assert a warning was emitted mentioning the block / tool.
    warned = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("malformed tool_input" in r.getMessage() for r in warned), (
        f"expected malformed-tool-input warning; got: {[r.getMessage() for r in warned]}"
    )


# ---------------------------------------------------------------------------
# SdkAnthropicClient.call_streaming_aggregated
# ---------------------------------------------------------------------------


def _fabricated_sdk_message(
    *,
    content: list[dict],
    stop_reason: str = "end_turn",
    input_tokens: int = 100,
    output_tokens: int = 20,
    cache_creation: int = 0,
    cache_read: int = 0,
):
    """Return a namespace that quacks like an SDK Message for the aggregator."""
    blocks = []
    for block in content:
        # The aggregator calls block.model_dump() or block.dict() on each block;
        # provide both via a MagicMock so either path returns the dict.
        mock_block = MagicMock()
        mock_block.model_dump.return_value = block
        blocks.append(mock_block)

    usage = SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=cache_creation,
        cache_read_input_tokens=cache_read,
    )
    return SimpleNamespace(content=blocks, stop_reason=stop_reason, usage=usage)


class _AsyncContextManagerStream:
    """Stand-in for the `async with messages.stream(**kwargs) as s` return."""

    def __init__(self, final_message):
        self._final = final_message

    async def __aenter__(self):
        inner = MagicMock()
        inner.get_final_message = AsyncMock(return_value=self._final)
        return inner

    async def __aexit__(self, exc_type, exc, tb):
        return False


async def test_sdk_aggregator_pops_stream_kwarg_before_calling_stream():
    """F048 P0-2: _payload_to_kwargs sets stream=True, but messages.stream()
    does NOT accept it. The aggregator must pop it before calling."""
    client = SdkAnthropicClient.__new__(SdkAnthropicClient)
    client._settings = _settings()

    captured_kwargs: dict = {}

    def stream_spy(**kwargs):
        # Record kwargs at call time, before __aenter__.
        captured_kwargs.update(kwargs)
        final = _fabricated_sdk_message(
            content=[{"type": "text", "text": "ok"}],
        )
        return _AsyncContextManagerStream(final)

    mock_messages = MagicMock()
    mock_messages.stream = stream_spy
    client._client = SimpleNamespace(messages=mock_messages)

    payload = {
        "model": "claude-sonnet-4-5-20250514",
        "max_tokens": 1000,
        "system": "s",
        "messages": [{"role": "user", "content": "hi"}],
        # The presence of stream=True here simulates _build_api_payload output.
        "stream": True,
    }
    await client.call_streaming_aggregated(payload)

    # stream= must NOT have been forwarded to messages.stream().
    assert "stream" not in captured_kwargs


async def test_sdk_aggregator_injects_background_timeout():
    """F048 P1-1 / P2: aggregator sets timeout=float(api_background_timeout_read)
    before calling messages.stream()."""
    client = SdkAnthropicClient.__new__(SdkAnthropicClient)
    client._settings = _settings()  # api_background_timeout_read=600

    captured_kwargs: dict = {}

    def stream_spy(**kwargs):
        captured_kwargs.update(kwargs)
        final = _fabricated_sdk_message(
            content=[{"type": "text", "text": "ok"}],
        )
        return _AsyncContextManagerStream(final)

    mock_messages = MagicMock()
    mock_messages.stream = stream_spy
    client._client = SimpleNamespace(messages=mock_messages)

    await client.call_streaming_aggregated({
        "model": "x",
        "max_tokens": 100,
        "system": "s",
        "messages": [],
    })

    assert captured_kwargs.get("timeout") == 600.0


async def test_sdk_aggregator_matches_call_for_same_message():
    """F048: for an identical mocked SDK Message, call() and
    call_streaming_aggregated() produce identical ApiResponse."""
    content_blocks = [
        {"type": "text", "text": "final reply"},
    ]

    # --- call() path ---
    client_a = SdkAnthropicClient.__new__(SdkAnthropicClient)
    client_a._settings = _settings()
    message = _fabricated_sdk_message(
        content=content_blocks,
        stop_reason="end_turn",
        input_tokens=42,
        output_tokens=17,
        cache_creation=3,
        cache_read=9,
    )
    mock_msgs_a = MagicMock()
    mock_msgs_a.create = AsyncMock(return_value=message)
    client_a._client = SimpleNamespace(messages=mock_msgs_a)

    resp_call = await client_a.call({
        "model": "x",
        "max_tokens": 100,
        "system": "s",
        "messages": [],
    })

    # --- call_streaming_aggregated() path ---
    client_b = SdkAnthropicClient.__new__(SdkAnthropicClient)
    client_b._settings = _settings()
    mock_msgs_b = MagicMock()
    mock_msgs_b.stream = lambda **_kwargs: _AsyncContextManagerStream(message)
    client_b._client = SimpleNamespace(messages=mock_msgs_b)

    resp_stream = await client_b.call_streaming_aggregated({
        "model": "x",
        "max_tokens": 100,
        "system": "s",
        "messages": [],
    })

    assert resp_call.content == resp_stream.content
    assert resp_call.stop_reason == resp_stream.stop_reason
    assert resp_call.usage == resp_stream.usage
