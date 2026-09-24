"""Harness Phase 1b: the runner brackets every side-effecting dispatch."""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import MagicMock

import pytest

from nous.api.execution_context import ExecutionContext
from nous.cognitive.ledger_store import LedgerWriteError
from tests.test_runner_authorization import (
    AgentRunner,
    _MockBrain,
    _MockCognitive,
    _MockHeart,
    _one_tool_call_then_done,
    _RecordingDispatcher,
    _run_loop,
    _settings,
)


class _FakeStore:
    def __init__(self, *, fail_open=False, fail_close=False):
        self.events: list[tuple] = []
        self.fail_open = fail_open
        self.fail_close = fail_close
        self.failed_id = uuid.uuid4()

    async def open_entry(self, *, context, tool_name, tool_input, turn):
        self.events.append(("open", tool_name, context.kind))
        if self.fail_open:
            raise LedgerWriteError(self.failed_id, RuntimeError("db down"))
        return f"id-{tool_name}"

    async def record_blocked(self, *, context, tool_name, tool_input, turn, reason):
        self.events.append(("blocked", tool_name, reason))

    async def close_entry(self, entry_id, *, status, result_summary):
        self.events.append(("close", entry_id, status))
        if self.fail_close:
            raise LedgerWriteError(entry_id, RuntimeError("db down"))


def _runner(store, offered=("write_file",), **settings):
    r = AgentRunner(_MockCognitive(), _MockBrain(), _MockHeart(), _settings(**settings))
    d = _RecordingDispatcher(list(offered), store)
    r.set_dispatcher(d)
    r.set_ledger_store(store)
    return r, d


@pytest.mark.asyncio
async def test_row_opens_before_dispatch_and_closes_after():
    store = _FakeStore()
    r, _ = _runner(store)
    r._call_api = _one_tool_call_then_done("write_file")
    await _run_loop(r, is_background=True, context=ExecutionContext(kind="subtask", session_id="s1"))
    assert store.events == [
        ("open", "write_file", "subtask"),
        ("dispatch", "write_file"),
        ("close", "id-write_file", "success"),
    ]


@pytest.mark.asyncio
async def test_tool_error_closes_error():
    store = _FakeStore()
    r, d = _runner(store)

    async def failing(name, inp, **kw):
        store.events.append(("dispatch", name))
        return "boom", True

    d.dispatch = failing
    r._call_api = _one_tool_call_then_done("write_file")
    await _run_loop(r)
    assert store.events[-1] == ("close", "id-write_file", "error")


@pytest.mark.asyncio
async def test_cancellation_mid_call_closes_unknown_and_reraises():
    store = _FakeStore()
    r, d = _runner(store)
    reached = asyncio.Event()

    async def hanging(name, inp, **kw):
        store.events.append(("dispatch", name))
        reached.set()
        await asyncio.sleep(3600)

    d.dispatch = hanging
    r._call_api = _one_tool_call_then_done("write_file")
    task = asyncio.create_task(_run_loop(r, is_background=True))
    await asyncio.wait_for(reached.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.events[-1] == ("close", "id-write_file", "unknown")


@pytest.mark.asyncio
async def test_ledger_outage_fails_open_and_still_closes_the_client_id():
    store = _FakeStore(fail_open=True)
    r, d = _runner(store)
    r._call_api = _one_tool_call_then_done("write_file")
    await _run_loop(r)
    assert [c[0] for c in d.calls] == ["write_file"]
    assert store.events[-1] == ("close", store.failed_id, "success")


@pytest.mark.asyncio
async def test_close_failure_never_breaks_the_turn(caplog):
    store = _FakeStore(fail_close=True)
    r, _ = _runner(store)
    r._call_api = _one_tool_call_then_done("write_file")
    text, *_ = await _run_loop(r)
    assert text == "done" and "execution ledger" in caplog.text


@pytest.mark.asyncio
async def test_enforced_refusal_is_recorded_blocked():
    store = _FakeStore()
    r, d = _runner(store, offered=("recall_deep", "write_file"),
                   tool_offered_set_enforcement_mode="enforce")
    r._call_api = _one_tool_call_then_done("write_file")
    await _run_loop(r, is_background=True, tool_filter=["recall_deep"])
    assert d.calls == []
    (event,) = store.events
    assert event[:2] == ("blocked", "write_file") and event[2].startswith("Tool error:")


@pytest.mark.asyncio
async def test_no_store_means_no_ledger_calls():
    r = AgentRunner(_MockCognitive(), _MockBrain(), _MockHeart(), _settings())
    d = _RecordingDispatcher(["write_file"])
    r.set_dispatcher(d)
    r._call_api = _one_tool_call_then_done("write_file")
    await _run_loop(r)
    assert [c[0] for c in d.calls] == ["write_file"]


@pytest.mark.asyncio
async def test_extra_tools_are_not_persisted():
    store = _FakeStore()
    r, _ = _runner(store, offered=("recall_deep",))
    r._call_api = _one_tool_call_then_done("submit_final_report")

    async def _submit(**_):
        return "ok", False

    schema = {"name": "submit_final_report", "description": "s", "input_schema": {"type": "object"}}
    await _run_loop(r, is_background=True, extra_tools={"submit_final_report": (schema, _submit)})
    assert store.events == []


def test_fork_shares_the_ledger_store():
    store = _FakeStore()
    r, _ = _runner(store)
    assert r.fork(MagicMock())._ledger_store is store


# ---------------------------------------------------------------------------
# Streaming path
# ---------------------------------------------------------------------------


def _stream_runner(store, dispatch):
    from tests.test_streaming import _make_mock_cognitive, _make_mock_settings, _make_runner

    cognitive, _ = _make_mock_cognitive()
    settings = _make_mock_settings()
    settings.tool_offered_set_enforcement_mode = "warn"
    settings.tool_timeout = 0.05
    settings.keepalive_interval = 0.01
    runner = _make_runner(cognitive, settings)
    runner._dispatcher.available_tools.return_value = [
        {"name": "write_file", "description": "l", "input_schema": {}},
    ]
    runner._dispatcher.dispatch = dispatch
    runner.set_ledger_store(store)
    return runner


def _one_streamed_call():
    from nous.api.anthropic_client import StreamEvent

    calls = {"n": 0}

    async def fake_stream(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            yield StreamEvent(type="tool_start", tool_name="write_file", tool_id="t1", block_index=1)
            yield StreamEvent(type="tool_input_delta", text='{"content": "c"}', block_index=1)
            yield StreamEvent(type="block_stop", block_index=1)
            yield StreamEvent(type="done", stop_reason="tool_use")
        else:
            yield StreamEvent(type="text_delta", text="ok")
            yield StreamEvent(type="done", stop_reason="end_turn")

    return MagicMock(side_effect=fake_stream)


@pytest.mark.asyncio
async def test_stream_tool_timeout_closes_unknown():
    store = _FakeStore()

    async def slow(name, inp, **kw):
        await asyncio.sleep(1)
        return "late", False

    runner = _stream_runner(store, slow)
    runner._call_api_stream = _one_streamed_call()
    [e async for e in runner.stream_chat("s1", "go")]
    assert store.events[0][:2] == ("open", "write_file")
    assert store.events[-1] == ("close", "id-write_file", "unknown")


@pytest.mark.asyncio
async def test_stream_closed_mid_call_closes_unknown():
    store = _FakeStore()
    started = asyncio.Event()

    async def hanging(name, inp, **kw):
        started.set()
        await asyncio.sleep(3600)

    runner = _stream_runner(store, hanging)
    runner._settings.tool_timeout = 3600
    runner._call_api_stream = _one_streamed_call()
    gen = runner.stream_chat("s1", "go")

    async def consume():
        async for _ in gen:
            if started.is_set():
                return

    await asyncio.wait_for(consume(), timeout=5)
    await gen.aclose()
    assert store.events[-1] == ("close", "id-write_file", "unknown")
