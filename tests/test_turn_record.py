"""F108: one ``agent_turn_log`` row per turn, handed to the recorder where the turn ends.

The row's id is the turn's ``turn_id``, the one its tool-call rows carry. The runner gives the recorder raw text; the
recorder redacts and clips it (trigger 500, reply 1000) and keeps at most 50 recalled ids per kind. The runner tests
drive the real ``run_turn`` and ``stream_chat`` with an in-memory spy in place of the recorder. The row tests write
``nous_system.agent_turn_log`` in real Postgres under their own agent id and delete their rows afterwards.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import text
from test_layer_critic_skills import _mock_layer
from test_layer_critic_skills import _settings as _layer_settings
from test_tool_call_recording import PATHS, SCENARIO, _block, _drive, _runner, _Spy

from nous.api.anthropic_client import StreamEvent
from nous.api.models import ApiResponse
from nous.cognitive.critic_schemas import CriticResult, RoutingMode
from nous.cognitive.ledger_store import detail_text
from nous.cognitive.schemas import FrameSelection, TurnContext
from nous.observability.recording import BoundedWriter
from nous.observability.tool_calls import ToolCallRecorder, TurnRecord

_SETTINGS = SimpleNamespace(tool_input_max_chars=2000, tool_result_preview_chars=500, tool_reason_max_chars=500)
_CUT_MARKER = "\n…[middle cut]…\n"
# A token the ledger's redaction masks (an sk- key needs 20+ characters after the prefix).
_SECRET = "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUv"
VERDICT = {
    "heuristic_frame": "conversation",
    "critic_frame": "task",
    "routing": "single_advised",
    "rationale": "A build request.",
    "latency_ms": 120,
    "mode": "shadow",
    "agreed": False,
    "skills": [],
    "activated_skills": [],
}


class _TurnSpy(_Spy):
    """The tool-call spy, keeping the turn records too."""

    def __init__(self) -> None:
        super().__init__()
        self.turns: list[TurnRecord] = []

    def record_turn(self, rec: TurnRecord) -> None:
        self.turns.append(rec)


def _context(**overrides) -> TurnContext:
    frame = FrameSelection(frame_id="conversation", frame_name="Conversation", confidence=0.9, match_method="default")
    return TurnContext(system_prompt="You are Nous.", frame=frame, **overrides)


def _turn_runner(spy: _TurnSpy, **settings):
    r, calls = _runner(recorder=spy, **settings)
    r._cognitive.preset = _context(
        recalled_decision_ids=["d1"],
        recalled_fact_ids=["f1", "f2"],
        recalled_episode_ids=["e1"],
        critic_verdict=VERDICT,
    )
    return r, calls


# ---- what the row carries -------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
async def test_turn_row_has_trigger_memory_critic_reply_and_claim_check(path):
    spy = _TurnSpy()
    r, _calls = _turn_runner(spy)
    before = datetime.now(UTC)
    assert await _drive(r, path, [[("t1", "lookup", {"q": "deploy date", "reason": "Find it."})]]) == "done"
    (turn,) = spy.turns
    assert isinstance(turn.id, uuid.UUID)
    assert (turn.trigger_text, turn.reply_text, turn.error) == ("go", "done", None)
    assert turn.memory_context == {"decision": ["d1"], "fact": ["f1", "f2"], "procedure": [], "episode": ["e1"]}
    assert turn.critic == VERDICT
    assert turn.claim_check["verified"] is True
    assert turn.claim_check["tool_names_this_turn"] == ["lookup"]
    assert (turn.work_kind, turn.context_kind, turn.frame_id) == ("conversation", "interactive", "conversation")
    assert (turn.session_id, turn.parent_session_id, turn.turn_number) == ("s1", None, 1)
    assert turn.tool_call_count == 1
    assert turn.started_at.tzinfo is not None and turn.ended_at.tzinfo is not None
    assert before <= turn.started_at <= turn.ended_at <= datetime.now(UTC)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
async def test_turn_without_critic_has_null_critic(path):
    spy = _TurnSpy()
    r, _calls = _runner(recorder=spy)  # the default preset: the critic did not run
    assert await _drive(r, path, []) == "done"
    (turn,) = spy.turns
    assert turn.critic is None


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
async def test_turn_without_claim_check_has_null_claim_check(path):
    spy = _TurnSpy()
    r, _calls = _turn_runner(spy, claim_verification_enabled=False)
    assert await _drive(r, path, []) == "done"
    (turn,) = spy.turns
    assert turn.claim_check is None


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
async def test_turn_id_matches_tool_call_rows(path):
    spy = _TurnSpy()
    r, _calls = _turn_runner(spy)
    assert await _drive(r, path, SCENARIO) == "done"
    (turn,) = spy.turns
    assert len(spy.records) == 6
    assert {rec.turn_id for rec in spy.records} == {turn.id}


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
async def test_tool_call_count_equals_rows(path):
    spy = _TurnSpy()
    r, _calls = _turn_runner(spy)
    assert await _drive(r, path, SCENARIO) == "done"
    (turn,) = spy.turns
    # Refused, retired and malformed calls each drew an index, so each has a row and counts.
    assert turn.tool_call_count == len(spy.records) == 6

    quiet = _TurnSpy()
    r2, _calls2 = _turn_runner(quiet)
    assert await _drive(r2, path, []) == "done"
    assert quiet.turns[0].tool_call_count == 0 and quiet.records == []


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
async def test_no_recorder_writes_no_turn_row(path):
    r, _calls = _runner(tool_call_log_enabled=False)

    def must_not_run(*a, **k):
        raise AssertionError("_note_turn ran with no recorder")

    r._note_turn = must_not_run
    assert await _drive(r, path, [[("t1", "lookup", {"q": "x"})]]) == "done"


# ---- error and cancel still end in a row ----------------------------------------------------------------------


def _api_then(exc: BaseException):
    """A fake ``_call_api``: one lookup call, then ``exc`` on the next model call."""
    n = {"calls": 0}

    async def fake_call_api(system_prompt, messages, tools=None, **_):
        n["calls"] += 1
        if n["calls"] == 1:
            return ApiResponse(content=[_block("t1", "lookup", {"q": "x"})], stop_reason="tool_use")
        raise exc

    return fake_call_api


def _stream_then(exc: BaseException | None, *, error_event: str | None = None):
    """A fake ``_call_api_stream``: one lookup call, then a partial reply ended by ``exc`` or an error event."""
    n = {"calls": 0}

    async def fake_stream(*args, **kwargs):
        n["calls"] += 1
        if n["calls"] == 1:
            yield StreamEvent(type="tool_start", tool_name="lookup", tool_id="t1", block_index=0)
            yield StreamEvent(type="tool_input_delta", text='{"q": "x"}', block_index=0)
            yield StreamEvent(type="block_stop", block_index=0)
            yield StreamEvent(type="done", stop_reason="tool_use")
            return
        yield StreamEvent(type="text_delta", text="partial")
        if error_event is not None:
            yield StreamEvent(type="error", text=error_event)
            return
        raise exc

    return MagicMock(side_effect=fake_stream)


async def _drive_failing(r, path: str, exc: BaseException | None, *, error_event: str | None = None) -> None:
    try:
        if path == "run_turn":
            r._call_api = _api_then(exc)
            await r.run_turn("s1", "go")
        else:
            r._call_api_stream = _stream_then(exc, error_event=error_event)
            _events = [e async for e in r.stream_chat("s1", "go")]
    finally:
        r._api_shared = True
        await r.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "exc", "raised", "error"),
    [
        ("run_turn", RuntimeError("model down"), RuntimeError, "model down"),
        ("run_turn", asyncio.CancelledError(), asyncio.CancelledError, "cancelled"),
        ("stream_chat", RuntimeError("model down"), RuntimeError, "model down"),
        # A client disconnect: stream_chat treats it as a clean exit and does not re-raise.
        ("stream_chat", asyncio.CancelledError(), None, "cancelled"),
    ],
    ids=["run_turn-error", "run_turn-cancel", "stream-error", "stream-cancel"],
)
async def test_turn_row_written_on_error_and_cancel(path, exc, raised, error):
    spy = _TurnSpy()
    r, _calls = _turn_runner(spy)
    if raised is None:
        await _drive_failing(r, path, exc)
    else:
        with pytest.raises(raised):
            await _drive_failing(r, path, exc)
    (turn,) = spy.turns
    assert turn.error == error
    assert turn.tool_call_count == 1 == len(spy.records)
    assert spy.records[0].turn_id == turn.id
    assert turn.claim_check is None  # the claim check runs on a turn that ended well only
    assert turn.critic == VERDICT and turn.trigger_text == "go"
    assert turn.started_at.tzinfo is not None and turn.ended_at.tzinfo is not None


@pytest.mark.asyncio
async def test_stream_error_event_ends_in_a_row():
    spy = _TurnSpy()
    r, _calls = _turn_runner(spy)
    await _drive_failing(r, "stream_chat", None, error_event="overloaded")
    (turn,) = spy.turns
    assert turn.error == "overloaded"
    assert turn.tool_call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
async def test_recorder_failure_does_not_change_the_turn(path):
    class _Broken(_TurnSpy):
        def record_turn(self, rec):
            raise RuntimeError("recorder down")

    r, _calls = _turn_runner(_Broken())
    assert await _drive(r, path, [[("t1", "lookup", {"q": "x"})]]) == "done"


# ---- the critic's verdict on the TurnContext ------------------------------------------------------------------


class _Bus:
    def __init__(self) -> None:
        self.events: list = []

    async def emit(self, event) -> None:
        self.events.append(event)


@pytest.mark.asyncio
async def test_critic_verdict_is_the_emitted_event_data():
    layer = _mock_layer(_layer_settings(critic_enabled=True, critic_mode="shadow"))
    layer._bus = _Bus()
    result = CriticResult(
        routing=RoutingMode.SINGLE_ADVISED, recommended_frame="decision", rationale="Weighs options", latency_ms=90
    )
    layer._critic = MagicMock()
    layer._critic.classify = AsyncMock(return_value=result)
    layer._critic.run_diagnostics = MagicMock(return_value=[])
    ctx = await layer.pre_turn(
        agent_id="test", session_id="s1", user_input="should we pick redis or kafka", session=None
    )
    (event,) = [e for e in layer._bus.events if e.type == "critic_classified"]
    assert ctx.critic_verdict == event.data
    assert ctx.critic_verdict["critic_frame"] == "decision" and ctx.critic_verdict["rationale"] == "Weighs options"


@pytest.mark.asyncio
async def test_critic_verdict_is_none_when_the_critic_is_off():
    layer = _mock_layer(_layer_settings(critic_enabled=False))
    ctx = await layer.pre_turn(agent_id="test", session_id="s1", user_input="hi there how are you", session=None)
    assert ctx.critic_verdict is None


# ---- rows in Postgres -----------------------------------------------------------------------------------------


def _agent() -> str:
    return f"test-awv-turn-{uuid.uuid4().hex[:12]}"


async def _rows(db, agent: str) -> list[dict]:
    async with db.engine.connect() as conn:
        result = await conn.execute(
            text("SELECT * FROM nous_system.agent_turn_log WHERE agent_id = :a ORDER BY started_at"), {"a": agent}
        )
        return [dict(r) for r in result.mappings().all()]


async def _delete(db, agent: str) -> None:
    async with db.engine.begin() as conn:
        await conn.execute(text("DELETE FROM nous_system.agent_turn_log WHERE agent_id = :a"), {"a": agent})


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


def _turn(**overrides) -> TurnRecord:
    started = datetime.now(UTC)
    values = {
        "id": uuid.uuid4(),
        "work_kind": "conversation",
        "session_id": "s-1",
        "turn_number": 2,
        "context_kind": "interactive",
        "frame_id": "task",
        "trigger_text": "go",
        "reply_text": "done",
        "tool_call_count": 3,
        "started_at": started,
        "ended_at": started + timedelta(seconds=4),
    }
    values.update(overrides)
    return TurnRecord(**values)


@pytest.mark.postgres_only
async def test_trigger_and_reply_are_clipped_and_redacted(db):
    agent = _agent()
    writer = BoundedWriter("test")
    recorder = ToolCallRecorder(writer, db, agent, _SETTINGS)
    trigger = f"deploy with {_SECRET} " + "t" * 900 + " TRIGGER-END"
    reply = f"used {_SECRET} " + "r" * 1500 + " REPLY-END"
    error = f"failed with {_SECRET} " + "e" * 3000
    critic = {**VERDICT, "rationale": f"the user pasted {_SECRET}"}
    claim = {
        "verified": False,
        "claim_count": 1,
        "claims": [{"kind": "action", "evidence": "none", "text": f"I pushed with {_SECRET}"}],
        "violations": [{"claimed_text": f"pushed {_SECRET}", "expected_tool": "bash"}],
    }
    try:
        recorder.record_turn(
            _turn(trigger_text=trigger, reply_text=reply, error=error, critic=critic, claim_check=claim)
        )
        await writer.drain(5.0)
        (row,) = await _rows(db, agent)
        assert row["trigger_text"] == detail_text(trigger, 500)
        assert row["reply_text"] == detail_text(reply, 1000)
        assert row["error"] == detail_text(error, 1000)
        assert row["trigger_text"].endswith("TRIGGER-END") and _CUT_MARKER in row["trigger_text"]
        assert row["reply_text"].endswith("REPLY-END") and _CUT_MARKER in row["reply_text"]
        assert len(row["trigger_text"]) <= 500 + len(_CUT_MARKER)
        assert len(row["reply_text"]) <= 1000 + len(_CUT_MARKER)
        stored_critic, stored_claim = _json(row["critic"]), _json(row["claim_check"])
        for value in (
            row["trigger_text"],
            row["reply_text"],
            row["error"],
            json.dumps(stored_critic),
            json.dumps(stored_claim),
        ):
            assert _SECRET not in value
        assert stored_critic == {**VERDICT, "rationale": detail_text(critic["rationale"], 1000)}
        assert stored_claim["verified"] is False and stored_claim["claim_count"] == 1
        assert stored_claim["claims"][0]["kind"] == "action"
        assert stored_claim["claims"][0]["text"] == detail_text(f"I pushed with {_SECRET}", 1000)
        assert recorder.stats.written == 1
    finally:
        await _delete(db, agent)


@pytest.mark.postgres_only
async def test_memory_context_ids_capped_at_50_each(db):
    agent = _agent()
    writer = BoundedWriter("test")
    recorder = ToolCallRecorder(writer, db, agent, _SETTINGS)
    facts = [f"f{i}" for i in range(120)]
    memory = {"decision": ["d1", "d2", "d3"], "fact": facts, "procedure": [], "episode": [f"e{i}" for i in range(50)]}
    try:
        recorder.record_turn(_turn(memory_context=memory))
        await writer.drain(5.0)
        (row,) = await _rows(db, agent)
        stored = _json(row["memory_context"])
        assert stored == {
            "decision": ["d1", "d2", "d3"],
            "fact": facts[:50],
            "procedure": [],
            "episode": memory["episode"],
        }
    finally:
        await _delete(db, agent)


@pytest.mark.postgres_only
async def test_record_turn_upserts_on_id(db):
    agent = _agent()
    writer = BoundedWriter("test")
    recorder = ToolCallRecorder(writer, db, agent, _SETTINGS)
    first = _turn(critic=VERDICT, memory_context={"fact": ["f1"]})
    try:
        recorder.record_turn(first)
        await writer.drain(5.0)
        recorder.record_turn(
            _turn(
                id=first.id,
                started_at=first.started_at,
                reply_text="second reply",
                error="cancelled",
                tool_call_count=5,
            )
        )
        await writer.drain(5.0)
        (row,) = await _rows(db, agent)
        assert row["id"] == first.id
        assert (row["reply_text"], row["error"], row["tool_call_count"]) == ("second reply", "cancelled", 5)
        assert row["critic"] is None and row["memory_context"] is None and row["claim_check"] is None
        assert (row["work_kind"], row["context_kind"], row["frame_id"]) == ("conversation", "interactive", "task")
        assert (row["session_id"], row["turn_number"]) == ("s-1", 2)
        assert row["started_at"] == first.started_at and row["ended_at"] is not None
        assert recorder.stats.written == 2
    finally:
        await _delete(db, agent)


async def test_record_turn_never_raises_when_database_fails(caplog):
    class _DownDB:
        def session(self):
            raise RuntimeError("database down")

    writer = BoundedWriter("test")
    recorder = ToolCallRecorder(writer, _DownDB(), "test-agent", _SETTINGS)
    recorder.record_turn(_turn())
    await writer.drain(5.0)
    assert recorder.stats.dropped_error == 1 and recorder.stats.written == 0
