"""Voice POC PR 4: time to first token on model-call rows, and the phases of ``pre_turn`` on turn rows.

No database: the recorder tests catch the row's parameters where the scope hands them to the writer.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from nous.api.anthropic_client import StreamEvent
from nous.api.runner import AgentRunner
from nous.cognitive.critic_schemas import CriticResult, RoutingMode
from nous.cognitive.schemas import TurnContext
from nous.observability import tool_calls
from nous.observability.model_calls import CallContext, ModelCallRecorder
from nous.observability.recording import BoundedWriter
from nous.observability.tool_calls import TurnRecord
from tests.test_layer_critic_skills import _mock_layer, _settings

_PAYLOAD = {"model": "claude-test-model", "max_tokens": 10}


def _recorder(monkeypatch) -> tuple[ModelCallRecorder, list[dict]]:
    recorder = ModelCallRecorder(BoundedWriter("test"), None, "agent", SimpleNamespace())
    rows: list[dict] = []
    monkeypatch.setattr(recorder, "_submit_call", lambda params, *rest: rows.append(params))
    return recorder, rows


def _done() -> StreamEvent:
    return StreamEvent(type="done", stop_reason="end_turn", usage={"output_tokens": 5})


def test_a_streamed_call_records_the_time_of_its_first_token(monkeypatch):
    recorder, rows = _recorder(monkeypatch)
    with recorder.scope(_PAYLOAD, CallContext(purpose="chat"), "stream") as scope:
        scope.observe_stream_event(StreamEvent(type="message_start", usage={"input_tokens": 2}))
        scope._t0 -= 0.2  # the request started 200 ms ago
        scope.mark_first_token()
        first = scope._first_token_t
        scope.mark_first_token()  # a later token never moves it
        assert scope._first_token_t == first
        scope.observe_stream_event(_done())
    assert 200 <= rows[0]["ttft_ms"] <= rows[0]["duration_ms"]


def test_an_aggregated_call_has_no_ttft(monkeypatch):
    recorder, rows = _recorder(monkeypatch)
    with recorder.scope(_PAYLOAD, CallContext(purpose="chat"), "aggregated") as scope:
        scope.mark_first_token()
        scope.observe_stream_event(_done())
    assert rows[0]["status"] == "ok" and rows[0]["ttft_ms"] is None


def test_a_retried_attempt_does_not_keep_the_failed_ones_token(monkeypatch):
    recorder, rows = _recorder(monkeypatch)
    with recorder.scope(_PAYLOAD, CallContext(purpose="chat"), "stream") as scope:
        scope.mark_first_token()
        scope.reset_first_token()  # the client's next attempt
        scope.observe_stream_event(_done())
    assert rows[0]["ttft_ms"] is None


def test_a_non_streamed_call_has_no_ttft(monkeypatch):
    recorder, rows = _recorder(monkeypatch)
    with recorder.scope(_PAYLOAD, CallContext(purpose="chat"), "call") as scope:
        scope.mark_first_token()  # a client that marks by mistake still writes NULL
        scope.finish({"usage": {"output_tokens": 1}, "stop_reason": "end_turn"})
    assert rows[0]["status"] == "ok" and rows[0]["ttft_ms"] is None


def test_a_stream_with_no_token_has_no_ttft(monkeypatch):
    recorder, rows = _recorder(monkeypatch)
    with recorder.scope(_PAYLOAD, CallContext(purpose="chat"), "stream") as scope:
        scope.observe_stream_event(_done())
    assert rows[0]["status"] == "ok" and rows[0]["ttft_ms"] is None


@pytest.mark.parametrize("how", ["error_event", "exception", "cut_off", "cancelled"])
def test_a_failed_stream_writes_no_ttft(monkeypatch, how):
    recorder, rows = _recorder(monkeypatch)
    try:
        with recorder.scope(_PAYLOAD, CallContext(purpose="chat"), "stream") as scope:
            scope.mark_first_token()
            if how == "error_event":
                scope.observe_stream_event(StreamEvent(type="error", text="overloaded"))
            elif how == "exception":
                raise RuntimeError("boom")
            elif how == "cancelled":
                raise asyncio.CancelledError
    except (RuntimeError, asyncio.CancelledError):
        pass
    assert rows[0]["status"] != "ok" and rows[0]["ttft_ms"] is None


def test_mark_first_token_never_raises(monkeypatch):
    recorder, rows = _recorder(monkeypatch)

    def broken() -> float:
        raise RuntimeError("clock")

    with recorder.scope(_PAYLOAD, CallContext(purpose="chat"), "stream") as scope:
        with monkeypatch.context() as patched:
            patched.setattr("nous.observability.model_calls.time.perf_counter", broken)
            scope.mark_first_token()
        scope.observe_stream_event(_done())
    assert rows[0]["status"] == "ok" and rows[0]["ttft_ms"] is None
    assert recorder.stats.dropped_error == 1


# ---- pre_turn phases -------------------------------------------------------------------------------------------


def _quiet_heart(layer) -> None:
    """Stub what pre_turn awaits after the context build, so no phase-less time goes to logged mock failures."""
    layer.warm_active_episode = AsyncMock(return_value=None)
    layer._heart.subtasks.get_undelivered = AsyncMock(return_value=[])
    layer._heart.get_or_create_working_memory = AsyncMock()


def _slow(seconds: float, result):
    async def call(*_args, **_kwargs):
        await asyncio.sleep(seconds)
        return result

    return call


@pytest.mark.asyncio
async def test_pre_turn_reports_its_phases_with_the_critic_apart():
    layer = _mock_layer(_settings(critic_enabled=True, critic_mode="shadow"))
    _quiet_heart(layer)
    layer._critic = MagicMock()
    layer._critic.run_diagnostics = MagicMock(return_value=[])
    verdict = CriticResult(routing=RoutingMode.SINGLE_ADVISED, recommended_frame="task", rationale="t")
    layer._critic.classify = _slow(0.12, verdict)
    layer._frames.select = _slow(0.06, layer._frames.select.return_value)
    built = layer._context.build.return_value
    layer._context.build = _slow(0.18, built)

    ctx = await layer.pre_turn(agent_id="test", session_id="s1", user_input="please summarize this document")

    phases = ctx.pre_turn_phases
    assert set(phases) == {"frame_select_ms", "critic_ms", "context_build_ms", "other_ms", "total_ms"}
    assert ctx.pre_turn_ms == phases["total_ms"]
    assert phases["critic_ms"] >= 110 and phases["frame_select_ms"] >= 55 and phases["context_build_ms"] >= 170
    named = phases["frame_select_ms"] + phases["critic_ms"] + phases["context_build_ms"]
    assert abs(named - phases["total_ms"]) <= 0.05 * phases["total_ms"]  # A11
    assert abs(named + phases["other_ms"] - phases["total_ms"]) <= 2  # rounding only


@pytest.mark.asyncio
async def test_pre_turn_without_a_critic_reports_zero_critic_time():
    layer = _mock_layer(_settings(critic_enabled=False))
    layer.warm_active_episode = AsyncMock(return_value=None)
    ctx = await layer.pre_turn(agent_id="test", session_id="s1", user_input="please summarize this document")
    assert ctx.pre_turn_phases["critic_ms"] == 0


def test_the_runner_hands_the_phases_to_the_turn_record():
    recorded: list[TurnRecord] = []
    fake = SimpleNamespace(_tool_call_recorder=SimpleNamespace(record_turn=recorded.append))
    turn_context = TurnContext(
        system_prompt="p",
        frame=_mock_layer(_settings())._frames.select.return_value,
        pre_turn_ms=42,
        pre_turn_phases={"critic_ms": 10, "total_ms": 42},
    )
    ctx = SimpleNamespace(kind="chat", parent_session_id=None)
    AgentRunner._note_turn(
        fake, ctx, turn_id=uuid4(), session_id="s", turn_number=1, turn_context=turn_context, trigger="hi",
        reply="ok", error=None, claim_check=None, call_indexes=iter([0]), started_at=None,
    )
    assert (recorded[0].pre_turn_ms, recorded[0].pre_turn_phases) == (42, {"critic_ms": 10, "total_ms": 42})


def test_record_turn_serialises_the_phases():
    submitted: list = []
    recorder = tool_calls.ToolCallRecorder(
        SimpleNamespace(submit=lambda fn, stats=None: submitted.append(fn)), None, "agent",
        SimpleNamespace(tool_input_max_chars=1, tool_result_preview_chars=1, tool_reason_max_chars=1),
    )
    recorder._upsert_turn = lambda params: params  # the lambda handed to the writer returns the parameters
    recorder.record_turn(TurnRecord(id=uuid4(), work_kind="chat", pre_turn_ms=5, pre_turn_phases={"total_ms": 5}))
    params = submitted[0]()
    assert params["pre_turn_phases"] == '{"total_ms": 5}' and params["pre_turn_ms"] == 5


def test_the_turn_upsert_writes_the_phase_columns():
    sql = str(tool_calls._UPSERT_TURN)
    assert "pre_turn_ms" in sql and "CAST(:pre_turn_phases AS jsonb)" in sql


def test_the_migration_matches_this_document():
    """Skipped while ``111_ttft.sql`` is absent (a headless job cannot create it): once the owner adds it, it must
    be the DDL block of docs/implementation/voice-poc-ttft-migration.md."""
    root = Path(__file__).resolve().parents[1]
    migration = root / "sql" / "migrations" / "111_ttft.sql"
    if not migration.exists():
        pytest.skip("111_ttft.sql is not added yet")
    doc = (root / "docs" / "implementation" / "voice-poc-ttft-migration.md").read_text()
    block = doc.split("```sql\n", 1)[1].split("```", 1)[0]
    assert migration.read_text().strip() == block.strip()
