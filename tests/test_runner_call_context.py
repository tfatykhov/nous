"""F108 Phase 1, PR 2: the runner passes every model call an explicit ``CallContext``.

The context is built from the turn's own locals (``turn_id``, session, turn number, frame, trace id), never from the
runner's instance fields, which a concurrent session overwrites (bug 6). Its purpose comes from the kind of turn
(``purpose_for_context``); the F026 gate is ``action_gate`` and the end-of-conversation lessons are ``reflection``.
``_build_api_payload`` attaches each call's context estimate (``ContextStats``, with the F036 cache break) to it.

The tests drive the real loops (``run_turn``, ``stream_chat``, ``_tool_loop``, ``end_conversation``) over a client
double that takes ``call_context`` as both real clients do and records each call through a real
``ModelCallRecorder`` whose rows are captured instead of written: the scope, ``finish`` and ``observe_stream_event``
are the ones the clients use. Every wait is bounded, so a broken scope fails a test instead of hanging it.
"""

from __future__ import annotations

import ast
import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from test_layer_critic_skills import _settings as _layer_settings
from test_runner_authorization import _RecordingDispatcher, _run_loop, _settings
from test_runner_background import _MockBrain, _MockCognitive, _MockHeart
from test_turn_record import _Bus, _layer

from nous.api import runner as runner_module
from nous.api.anthropic_client import StreamEvent
from nous.api.execution_context import CONTEXT_KINDS, ExecutionContext
from nous.api.models import ApiResponse
from nous.api.runner import AgentRunner, Conversation, Message
from nous.brain.intentions import AUTHORITY_INTERNAL
from nous.cognitive.execution_ledger import ExecutionLedger
from nous.cognitive.schemas import FrameSelection, TurnContext, TurnResult
from nous.observability.model_calls import PURPOSES, CallContext, ModelCallRecorder

_USAGE = {"input_tokens": 3, "cache_read_input_tokens": 2000, "cache_creation_input_tokens": 50, "output_tokens": 7}
_GATE_USAGE = {"input_tokens": 41, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "output_tokens": 9}
_TRACE = "0123456789ab"
PATHS = ["run_turn", "stream_chat"]


# ---------------------------------------------------------------------------------------------------------------------
# The client double and its rows
# ---------------------------------------------------------------------------------------------------------------------


def _recorder() -> tuple[ModelCallRecorder, list[dict[str, Any]]]:
    """A real recorder whose rows are kept in a list instead of written."""
    rows: list[dict[str, Any]] = []
    rec = ModelCallRecorder(SimpleNamespace(), None, "test-agent", SimpleNamespace())
    rec._submit_call = lambda params, attempts: rows.append(params)
    return rec, rows


class _SpyClient:
    """A model client that takes ``call_context`` (``supports_call_context``) and records each call in a real scope.

    ``reply(payload)`` gives a call's response (an ``ApiResponse``, an exception to raise, or a coroutine giving
    one); ``segments`` are the event lists of successive streams. ``closed_streams`` counts the streams whose body
    has exited, so a test can see when a stream was closed."""

    supports_call_context = True

    def __init__(
        self,
        reply: Callable[[dict[str, Any]], Any] | None = None,
        segments: list[list[StreamEvent]] | None = None,
    ) -> None:
        self.recorder, self.rows = _recorder()
        self._reply = reply or (lambda payload: _text())
        self._segments = list(segments or [])
        self.contexts: list[CallContext | None] = []
        self.closed_streams = 0

    async def _record(self, payload: dict[str, Any], call_context: CallContext | None, kind: str) -> ApiResponse:
        self.contexts.append(call_context)
        with self.recorder.scope(payload, call_context, kind) as scope:
            reply = self._reply(payload)
            if asyncio.iscoroutine(reply):
                reply = await reply
            if isinstance(reply, BaseException):
                raise reply
            scope.finish(reply)
            return reply

    async def call(self, payload: dict[str, Any], *, call_context: CallContext | None = None) -> ApiResponse:
        return await self._record(payload, call_context, "call")

    async def call_streaming_aggregated(
        self, payload: dict[str, Any], *, call_context: CallContext | None = None, **_: Any
    ) -> ApiResponse:
        return await self._record(payload, call_context, "aggregated")

    async def stream(self, payload: dict[str, Any], *, call_context: CallContext | None = None):
        self.contexts.append(call_context)
        with self.recorder.scope(payload, call_context, "stream") as scope:
            try:
                for event in self._segments.pop(0):
                    scope.observe_stream_event(event)
                    yield event
            finally:
                self.closed_streams += 1

    async def start(self) -> None:
        pass

    async def close(self) -> None:
        pass


def _text(text: str = "done", usage: dict[str, int] | None = _USAGE) -> ApiResponse:
    return ApiResponse(
        content=[{"type": "text", "text": text}], stop_reason="end_turn", usage=None if usage is None else dict(usage)
    )


def _tool_use(name: str = "lookup", usage: dict[str, int] = _USAGE, call_id: str = "t1") -> ApiResponse:
    return ApiResponse(
        content=[{"type": "tool_use", "id": call_id, "name": name, "input": {}}],
        stop_reason="tool_use",
        usage=dict(usage),
    )


def _text_segment(text: str = "done") -> list[StreamEvent]:
    return [
        StreamEvent(type="message_start", usage={k: v for k, v in _USAGE.items() if k != "output_tokens"}),
        StreamEvent(type="text_block_start", block_index=0),
        StreamEvent(type="text_delta", text=text, block_index=0),
        StreamEvent(type="block_stop", block_index=0),
        StreamEvent(type="done", stop_reason="end_turn", usage={"output_tokens": 7}),
    ]


def _tool_segment(name: str = "lookup") -> list[StreamEvent]:
    return [
        StreamEvent(type="message_start", usage={k: v for k, v in _USAGE.items() if k != "output_tokens"}),
        StreamEvent(type="tool_start", tool_name=name, tool_id="t1", block_index=0),
        StreamEvent(type="tool_input_delta", text="{}", block_index=0),
        StreamEvent(type="block_stop", block_index=0),
        StreamEvent(type="done", stop_reason="tool_use", usage={"output_tokens": 7}),
    ]


def _runner(client: _SpyClient, offered: tuple[str, ...] = ("lookup",), preset: TurnContext | None = None, **settings):
    """A runner over a recording dispatcher, the F026 gate off unless a test turns it on."""
    r = AgentRunner(
        _MockCognitive(preset), _MockBrain(), _MockHeart(), _settings(**{"action_gating_enabled": False, **settings})
    )
    r.set_dispatcher(_RecordingDispatcher(list(offered)))
    r.set_api_client(client)  # shared, so close() leaves it alone
    r._log_f026_decision = lambda *a, **k: None
    return r


def _preset(**overrides: Any) -> TurnContext:
    frame = FrameSelection(frame_id="conversation", frame_name="Conversation", confidence=0.9, match_method="default")
    return TurnContext(system_prompt="You are Nous.", frame=frame, **overrides)


async def _pump(gen: Any) -> list[StreamEvent]:
    """Drive a stream as rest.py does: each step in a task of its own, so each resume runs in a fresh copy of the
    caller's context (a ContextVar set inside ``stream_chat`` is gone after its first yield)."""
    events: list[StreamEvent] = []
    it = gen.__aiter__()
    while True:
        try:
            events.append(await asyncio.wait_for(asyncio.ensure_future(it.__anext__()), 10))
        except StopAsyncIteration:
            return events


async def _until_rows(client: _SpyClient, n: int) -> list[dict[str, Any]]:
    """The client's rows once there are ``n`` (a stream's row is written when its scope exits)."""

    async def wait() -> None:
        while len(client.rows) < n:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(wait(), 10)
    return client.rows


async def _drive(r: AgentRunner, path: str, session_id: str = "s1", message: str = "go") -> str:
    if path == "run_turn":
        text, _ctx, _usage = await asyncio.wait_for(r.run_turn(session_id, message), 10)
        return text
    events = await _pump(r.stream_chat(session_id, message))
    return "".join(e.text or "" for e in events if e.type == "text_delta")


def _who(payload: dict[str, Any]) -> str:
    """Which session's message a payload carries ("A" or "B"), read from the messages, not the context."""
    sent = json.dumps(payload["messages"])
    return "A" if "go A" in sent else "B"


def _interleaved(usage_by_who: dict[str, int]):
    """A reply function: each session's first call waits until both sessions' first calls are in flight, then both
    get a tool call; each session's second call gets the reply. Usage is per session (input_tokens)."""
    both = asyncio.Event()
    arrived: list[str] = []
    seen: dict[str, int] = {}

    async def reply(payload: dict[str, Any]) -> ApiResponse:
        who = _who(payload)
        seen[who] = seen.get(who, 0) + 1
        usage = {**_USAGE, "input_tokens": usage_by_who[who]}
        if seen[who] == 1:
            arrived.append(who)
            if len(arrived) == 2:
                both.set()
            await asyncio.wait_for(both.wait(), 10)
            return _tool_use(usage=usage)
        return _text(usage=usage)

    return reply


# ---------------------------------------------------------------------------------------------------------------------
# Bug 6: concurrent sessions
# ---------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_sessions_keep_their_own_rows():
    client = _SpyClient(_interleaved({"A": 111, "B": 222}))
    r = _runner(client)
    # B already has a turn, so its numbers differ from A's.
    r._conversations["sB"] = Conversation(
        session_id="sB", messages=[Message(role="user", content="earlier"), Message(role="assistant", content="ok")]
    )
    await asyncio.wait_for(asyncio.gather(r.run_turn("sA", "go A"), r.run_turn("sB", "go B")), 10)

    rows = client.rows
    assert len(rows) == 4
    for row in rows:
        for column in ("session_id", "turn_id", "turn_number", "frame_id", "input_uncached", "output_tokens"):
            assert row[column] is not None, column
        assert (row["purpose"], row["label_source"], row["status"]) == ("chat", "explicit", "ok")
    by_session = {s: [row for row in rows if row["session_id"] == s] for s in ("sA", "sB")}
    assert [len(v) for v in by_session.values()] == [2, 2]
    assert {row["input_uncached"] for row in by_session["sA"]} == {111}
    assert {row["input_uncached"] for row in by_session["sB"]} == {222}
    assert {row["turn_number"] for row in by_session["sA"]} == {1}
    assert {row["turn_number"] for row in by_session["sB"]} == {2}
    turn_a = {row["turn_id"] for row in by_session["sA"]}
    turn_b = {row["turn_id"] for row in by_session["sB"]}
    assert len(turn_a) == len(turn_b) == 1 and turn_a != turn_b


@pytest.mark.asyncio
async def test_subtask_does_not_relabel_a_chat_call():
    client = _SpyClient(_interleaved({"A": 111, "B": 222}))
    r = _runner(client)
    sub = ExecutionContext(kind="subtask", session_id="subtask-1", parent_session_id="chat-1", subtask_id=uuid4())
    await asyncio.wait_for(
        asyncio.gather(
            r.run_turn("chat-1", "go A"),
            r.run_turn("subtask-1", "go B", is_subtask=True, is_background=True, context=sub),
        ),
        10,
    )
    chat = [row for row in client.rows if row["session_id"] == "chat-1"]
    subtask = [row for row in client.rows if row["session_id"] == "subtask-1"]
    assert len(chat) == len(subtask) == 2
    assert {(row["purpose"], row["call_kind"], row["subtask_id"]) for row in chat} == {("chat", "call", None)}
    assert {(row["purpose"], row["call_kind"]) for row in subtask} == {("subtask", "aggregated")}
    assert {(row["subtask_id"], row["parent_session_id"]) for row in subtask} == {(sub.subtask_id, "chat-1")}


# ---------------------------------------------------------------------------------------------------------------------
# Purposes
# ---------------------------------------------------------------------------------------------------------------------


_BRIEF_MAPPING = {
    "interactive": "chat",
    "agent_action": "chat",
    "mcp": "mcp",
    "subtask": "subtask",
    "dag_node": "subtask",
    "scheduled": "subtask",
    "heartbeat_triage": "heartbeat",
    "heartbeat_check": "background_check",
    "heartbeat_callback": "background_check",
    "background": "background_check",
    "dag_summary": "dag_summary",
    "continuation": "continuation",
}


@pytest.mark.parametrize("kind", CONTEXT_KINDS)
def test_every_execution_context_kind_has_a_purpose(kind):
    """A kind added to ExecutionContext later fails here until it is mapped (or is knowingly ``unlabelled``)."""
    for forked in (False, True):
        assert runner_module.purpose_for_context(kind, forked) in PURPOSES
    assert runner_module.purpose_for_context(kind, False) == _BRIEF_MAPPING.get(kind, "unlabelled")


def test_an_unknown_kind_is_unlabelled_not_chat():
    assert runner_module.purpose_for_context("some_new_kind", False) == "unlabelled"
    assert runner_module.purpose_for_context("some_new_kind", True) == "unlabelled"


def test_a_fork_never_labels_a_call_chat():
    """A fork serves the heartbeat: nobody is in the loop, so a chat kind there is mislabelled, and is shown so."""
    for kind, purpose in _BRIEF_MAPPING.items():
        expected = "unlabelled" if purpose == "chat" else purpose
        assert runner_module.purpose_for_context(kind, True) == expected, kind


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ctx",
    [
        ExecutionContext(
            kind="continuation",
            session_id="intent-1",
            authority=AUTHORITY_INTERNAL,
            intention_id=uuid4(),
            root_intention_id=uuid4(),
        ),
        ExecutionContext(kind="dag_summary", session_id="dag-summary-1", dag_id=uuid4()),
        ExecutionContext(kind="mcp", session_id="mcp-1"),
    ],
    ids=lambda c: c.kind,
)
async def test_continuation_dag_summary_and_mcp_have_their_own_purpose(ctx):
    client = _SpyClient()
    r = _runner(client)
    await asyncio.wait_for(_run_loop(r, context=ctx, is_background=ctx.is_background, turn_number=2), 10)
    (row,) = client.rows
    assert (row["purpose"], row["label_source"]) == (ctx.kind, "explicit")
    assert (row["session_id"], row["turn_number"], row["dag_id"]) == ("s1", 2, ctx.dag_id)


@pytest.mark.asyncio
async def test_dynamic_check_is_background_check_not_heartbeat():
    client = _SpyClient()
    fork = _runner(client).fork(client)  # the heartbeat runs its checks, triage and callbacks on a fork
    check = ExecutionContext(kind="heartbeat_check", session_id="dynamic-check-1")
    triage = ExecutionContext(kind="heartbeat_triage", session_id="heartbeat-1")
    await asyncio.wait_for(_run_loop(fork, context=check, is_background=True), 10)
    fork = _runner(client).fork(client)
    await asyncio.wait_for(_run_loop(fork, context=triage, is_background=True), 10)
    assert [row["purpose"] for row in client.rows] == ["background_check", "heartbeat"]


@pytest.mark.asyncio
async def test_a_chat_kind_on_a_fork_is_recorded_unlabelled():
    """A fork has nobody in the loop: a turn there that names a chat kind is shown as ``unlabelled``, not passed
    off as chat, and not as the old ring's blanket ``heartbeat``."""
    client = _SpyClient()
    fork = _runner(client).fork(client)
    await asyncio.wait_for(_run_loop(fork, context=ExecutionContext(kind="interactive", session_id="s1")), 10)
    (row,) = client.rows
    assert (row["purpose"], row["label_source"], row["session_id"]) == ("unlabelled", "explicit", "s1")


# ---------------------------------------------------------------------------------------------------------------------
# Bug 8: the gate and the reflection
# ---------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_row_is_action_gate_with_tokens():
    replies = [_tool_use("send_email"), _text()]

    def reply(payload: dict[str, Any]) -> ApiResponse:
        if "safety gate" in json.dumps(payload["system"]):
            return ApiResponse(
                content=[{"type": "text", "text": '{"approved": true, "reason": "fine"}'}],
                stop_reason="end_turn",
                usage=dict(_GATE_USAGE),
            )
        return replies.pop(0)

    client = _SpyClient(reply)
    r = _runner(client, offered=("send_email",), action_gating_enabled=True)
    turn_id = uuid4()
    await asyncio.wait_for(
        _run_loop(r, ledger=ExecutionLedger(session_id="s1"), turn_number=4, turn_id=turn_id, trace_id=_TRACE), 10
    )
    assert [row["purpose"] for row in client.rows] == ["chat", "action_gate", "chat"]
    gate = client.rows[1]
    assert gate["label_source"] == "explicit" and gate["status"] == "ok"
    assert gate["model"] == r._settings.action_gating_model
    assert (gate["input_uncached"], gate["output_tokens"]) == (41, 9)
    # The gate checks this turn's tool call: it carries the turn, not the chat or subtask label.
    assert (gate["session_id"], gate["turn_id"], gate["turn_number"], gate["trace_id"]) == ("s1", turn_id, 4, _TRACE)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
async def test_gate_row_carries_the_turn_on_both_paths(path):
    """Both gate sites: run_turn's (in _tool_loop) and stream_chat's, whose turn context must reach the gate's call
    although stream_chat is a generator resumed in a fresh context after each yield."""
    replies = [_tool_use("send_email"), _text()]

    def reply(payload: dict[str, Any]) -> ApiResponse:
        if "safety gate" in json.dumps(payload["system"]):
            return ApiResponse(
                content=[{"type": "text", "text": '{"approved": true, "reason": "fine"}'}],
                stop_reason="end_turn",
                usage=dict(_GATE_USAGE),
            )
        return replies.pop(0)

    client = _SpyClient(reply, segments=[_tool_segment("send_email"), _text_segment()])
    r = _runner(client, offered=("send_email",), preset=_preset(trace_id=_TRACE), action_gating_enabled=True)
    assert await _drive(r, path) == "done"
    chat_1, gate, chat_2 = await _until_rows(client, 3)
    assert [row["purpose"] for row in (chat_1, gate, chat_2)] == ["chat", "action_gate", "chat"]
    assert (gate["label_source"], gate["call_kind"], gate["input_uncached"], gate["output_tokens"]) == (
        "explicit",
        "call",
        41,
        9,
    )
    assert gate["turn_id"] is not None and gate["turn_id"] == chat_1["turn_id"] == chat_2["turn_id"]
    assert (gate["session_id"], gate["turn_number"], gate["frame_id"], gate["trace_id"]) == (
        "s1",
        1,
        "conversation",
        _TRACE,
    )


@pytest.mark.asyncio
async def test_reflection_row_has_tokens():
    client = _SpyClient(lambda payload: _text("Lesson: keep it short."))
    r = _runner(client)
    r._conversations["s1"] = Conversation(
        session_id="s1",
        messages=[Message(role=role, content=f"m{i}") for i, role in enumerate(["user", "assistant"] * 3)],
    )
    assert await asyncio.wait_for(r.end_conversation("s1"), 10) is True
    (row,) = client.rows
    assert (row["purpose"], row["label_source"], row["status"]) == ("reflection", "explicit", "ok")
    assert row["session_id"] == "s1"
    assert (row["input_uncached"], row["cache_read"], row["output_tokens"]) == (3, 2000, 7)


# ---------------------------------------------------------------------------------------------------------------------
# Bug 7: errors and calls without usage
# ---------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_error_event_leaves_an_error_row():
    error = [
        StreamEvent(type="message_start", usage={"input_tokens": 3}),
        StreamEvent(type="error", text="Overloaded"),
    ]
    client = _SpyClient(segments=[error])
    r = _runner(client)
    events = await _pump(r.stream_chat("s1", "go"))
    assert [e.type for e in events if e.type != "keepalive"] == ["error"]
    (row,) = await _until_rows(client, 1)
    assert (row["status"], row["purpose"], row["session_id"]) == ("error", "chat", "s1")
    assert "Overloaded" in row["error_text"] and row["input_uncached"] == 3
    assert row["turn_id"] is not None and row["turn_number"] == 1


@pytest.mark.asyncio
async def test_non_stream_call_without_usage_still_records():
    client = _SpyClient(lambda payload: _text(usage=None))
    r = _runner(client)
    assert await _drive(r, "run_turn") == "done"
    (row,) = client.rows
    assert (row["status"], row["purpose"], row["session_id"], row["turn_number"]) == ("ok", "chat", "s1", 1)
    assert (row["input_uncached"], row["cache_read"], row["output_tokens"]) == (None, None, None)


@pytest.mark.asyncio
async def test_failed_call_records_an_error_row_with_its_turn():
    client = _SpyClient(lambda payload: RuntimeError("API error 500: overloaded"))
    r = _runner(client)
    with pytest.raises(RuntimeError):
        await _drive(r, "run_turn")
    (row,) = client.rows
    assert (row["status"], row["purpose"], row["session_id"], row["turn_number"]) == ("error", "chat", "s1", 1)
    assert "overloaded" in row["error_text"] and row["turn_id"] is not None


# ---------------------------------------------------------------------------------------------------------------------
# Bug 9: trace_id
# ---------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
async def test_trace_id_is_recorded_when_the_turn_has_one(path):
    client = _SpyClient(segments=[_text_segment()])
    r = _runner(client, preset=_preset(trace_id=_TRACE))
    assert await _drive(r, path) == "done"
    (row,) = await _until_rows(client, 1)
    assert row["trace_id"] == _TRACE

    client = _SpyClient(segments=[_text_segment()])
    r = _runner(client)  # a turn context without one (a call before pre_turn ran, or a test double)
    assert await _drive(r, path) == "done"
    (row,) = await _until_rows(client, 1)
    assert row["trace_id"] is None and row["session_id"] == "s1"


@pytest.mark.asyncio
async def test_pre_turn_mints_a_trace_id_that_turn_completed_reuses():
    layer = _layer(_layer_settings(critic_enabled=False))
    layer._bus = _Bus()
    ctx = await layer.pre_turn(agent_id="test", session_id="s1", user_input="hi there how are you", session=None)
    assert isinstance(ctx.trace_id, str) and len(ctx.trace_id) == 12
    int(ctx.trace_id, 16)  # the bus's own id: 12 hex characters
    other = await layer.pre_turn(agent_id="test", session_id="s1", user_input="and again", session=None)
    assert other.trace_id != ctx.trace_id

    await layer.post_turn("test", "s1", TurnResult(response_text="hello"), ctx)
    (event,) = [e for e in layer._bus.events if e.type == "turn_completed"]
    assert event.event_id == event.trace_id == ctx.trace_id


# ---------------------------------------------------------------------------------------------------------------------
# The context estimate travels with the call
# ---------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stats_travel_with_the_call():
    """Sections, counts and the F036 cache break land on the call's own row. The second turn's static identity
    changed, so its call breaks the cache."""
    client = _SpyClient()
    r = _runner(client)
    system = {
        "static": "## Identity\nI am Nous.",
        "semi_stable": "## Relevant Facts\n- [a] one\n- [b] two",
        "dynamic": "## Current Date/Time\nnow",
    }
    changed = {**system, "static": "## Identity\n" + "I am Nous, changed. " * 20}
    for prompt in (system, changed):
        conv = Conversation(session_id="s1", messages=[Message(role="user", content="go")])
        await asyncio.wait_for(
            r._tool_loop(system_prompt=prompt, conversation=conv, frame_id="conversation", session_id="s1"), 10
        )
    first, second = client.rows
    assert first["sections_present"] == ["identity", "relevant_facts", "datetime"]
    sections = json.loads(first["section_tokens_est"])
    assert {"identity", "relevant_facts", "datetime", "tools_definition", "messages"} <= set(sections)
    assert first["total_tokens_est"] == sum(sections.values())
    assert (first["tools_count"], first["tool_names"], first["messages_count"]) == (1, ["lookup"], 1)
    assert first["loaded_facts"] == 2 and first["context_window_size"] == r._get_context_window_size()
    assert first["cache_break"] is False and first["cache_break_components"] == []
    assert second["cache_break"] is True and second["cache_break_components"] == ["static_identity"]
    assert second["cache_break_tokens_lost"] == len(changed["static"]) // 4
    assert json.loads(second["section_tokens_est"])["identity"] > sections["identity"]


# ---------------------------------------------------------------------------------------------------------------------
# The streaming path: explicit after the first yield; the keepalive wrapper closes the client stream
# ---------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_chat_passes_context_explicitly_after_first_yield():
    """runner.py's F091 note: a ContextVar set inside stream_chat is gone after its first yield. The row of the
    segment after a tool round-trip still has the turn, because the context is passed, not read from a variable."""
    client = _SpyClient(segments=[_tool_segment(), _text_segment()])
    r = _runner(client, preset=_preset(trace_id=_TRACE))
    events = await _pump(r.stream_chat("s1", "go"))
    assert "".join(e.text for e in events if e.type == "text_delta") == "done"
    first, second = await _until_rows(client, 2)
    for row in (first, second):
        assert (row["purpose"], row["label_source"], row["status"], row["call_kind"]) == (
            "chat",
            "explicit",
            "ok",
            "stream",
        )
        assert (row["session_id"], row["turn_number"], row["frame_id"], row["trace_id"]) == (
            "s1",
            1,
            "conversation",
            _TRACE,
        )
        assert (row["input_uncached"], row["cache_read"], row["output_tokens"]) == (3, 2000, 7)
        assert row["tools_count"] == 1
    assert first["turn_id"] is not None and first["turn_id"] == second["turn_id"]
    assert second["messages_count"] == 3  # the later segment carries the tool round-trip


@pytest.mark.asyncio
async def test_keepalive_wrapper_closes_client_stream():
    """The runner abandons a segment after an ``error`` event (stream_chat returns). Closing the keepalive wrapper
    closes the client's stream inside that close, so its scope exits and its row is written now, not at a later
    garbage-collection hop."""
    error = [StreamEvent(type="error", text="Overloaded"), StreamEvent(type="text_delta", text="never sent")]
    client = _SpyClient(segments=[error])
    r = _runner(client)
    wrapper = r._stream_with_keepalive("sys", [{"role": "user", "content": "go"}])
    first = await asyncio.wait_for(wrapper.__anext__(), 10)
    assert first.type == "error"
    await asyncio.wait_for(wrapper.aclose(), 10)
    assert client.closed_streams == 1
    (row,) = client.rows
    assert row["status"] == "error" and "Overloaded" in row["error_text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["hangs", "raises"])
async def test_keepalive_close_is_bounded_and_never_raises(fault, monkeypatch):
    """A client stream whose close hangs or raises cannot hold up or break the wrapper's close."""
    monkeypatch.setattr(runner_module, "_STREAM_CLOSE_TIMEOUT_SECONDS", 0.05)

    async def bad_stream(*args: Any, **kwargs: Any):
        try:
            yield StreamEvent(type="error", text="Overloaded")
        finally:
            if fault == "hangs":
                await asyncio.sleep(30)
            else:
                raise RuntimeError("close failed")

    r = _runner(_SpyClient())
    r._call_api_stream = bad_stream
    wrapper = r._stream_with_keepalive("sys", [{"role": "user", "content": "go"}])
    assert (await asyncio.wait_for(wrapper.__anext__(), 10)).type == "error"
    await asyncio.wait_for(wrapper.aclose(), 5)  # neither waits 30 s nor raises


# ---------------------------------------------------------------------------------------------------------------------
# Every call site passes a context
# ---------------------------------------------------------------------------------------------------------------------

_RUNNER_HELPERS = {"_call_api", "_call_api_stream", "_stream_with_keepalive"}
_CLIENT_METHODS = {"call", "stream", "call_streaming_aggregated"}


def _passes_context_kwargs(call: ast.Call) -> bool:
    """``**context_kwargs(...)`` in the call's keywords."""
    return any(
        k.arg is None
        and isinstance(k.value, ast.Call)
        and isinstance(k.value.func, ast.Name)
        and k.value.func.id == "context_kwargs"
        for k in call.keywords
    )


def test_every_runner_call_api_site_passes_call_context():
    """The Task 11 source guard cannot see runner-internal calls: a ``_call_api``, ``_call_api_stream`` or
    ``_stream_with_keepalive`` call must pass ``call_context=`` (not a literal None), and a ``self._api`` model call
    must pass ``**context_kwargs(...)``."""
    tree = ast.parse(Path(runner_module.__file__).read_text(encoding="utf-8"))
    missing: list[tuple[str, int]] = []
    helper_sites = client_sites = 0
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        name = node.func.attr
        if name in _RUNNER_HELPERS:
            helper_sites += 1
            given = {k.arg: k.value for k in node.keywords}
            value = given.get("call_context")
            if value is None or (isinstance(value, ast.Constant) and value.value is None):
                missing.append((name, node.lineno))
        elif name in _CLIENT_METHODS and ast.unparse(node.func.value) == "self._api":
            client_sites += 1
            if not _passes_context_kwargs(node):
                missing.append((f"self._api.{name}", node.lineno))
    assert missing == []
    # the gate, the reflection, both loops' calls and finals, the stream helper and the keepalive wrapper
    assert helper_sites >= 8 and client_sites == 3
