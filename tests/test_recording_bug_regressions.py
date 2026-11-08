"""F108 Phase 1, PR 4: one regression test per recording bug of the design ("Recording bugs found", 2026-11-07).

Each test drives the real path, never a unit helper: a real ``AgentRunner`` (or the real background helper or critic)
over a real Anthropic client whose HTTP goes to a stubbed Messages API (``httpx.MockTransport``), recording through
a real ``ModelCallRecorder`` and the shared ``BoundedWriter`` into real Postgres, and read back from the table and,
where the bug was about what the dashboard showed, through the ``TableContextView`` and the REST endpoints. The harness
is ``tests/test_context_view.py``'s. Every wait is bounded; each test writes under its own agent id and deletes its
rows.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any
from uuid import uuid4

import pytest
from test_context_view import (
    MODEL,
    TICK,
    WAIT,
    StubApi,
    attempt_rows,
    delete_rows,
    error_response,
    is_gate,
    message,
    model_rows,
    new_agent,
    preset,
    real_client,
    recorder_for,
    rest,
    settle,
    turn,
)
from test_runner_authorization import _RecordingDispatcher, _run_loop
from test_runner_call_context import _drive, _runner

import nous.handlers
from nous.api.execution_context import ExecutionContext
from nous.api.runner import Conversation, Message
from nous.brain.intentions import AUTHORITY_INTERNAL
from nous.cognitive.critic import CriticAgent
from nous.cognitive.execution_ledger import ExecutionLedger
from nous.cognitive.schemas import FrameSelection
from nous.config import Settings
from nous.handlers import call_with_tool_choice
from nous.observability.context_logger import ContextLogger
from nous.observability.context_view import TableContextView

pytestmark = pytest.mark.postgres_only

TRACE = "0123456789ab"
GATE_USAGE = {"input_tokens": 41, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "output_tokens": 9}
RESPONSE_COLUMNS = ("input_uncached", "cache_read", "cache_write", "output_tokens", "stop_reason", "duration_ms")


@contextlib.asynccontextmanager
async def recording(db: Any, monkeypatch: Any, api: StubApi | None = None, *, text_on: bool = False, **kw: Any):
    """The recording stack for one test: ``(agent, writer, client, store)``; the rows are deleted afterwards."""
    agent = new_agent()
    writer, recorder, store = recorder_for(db, agent, text_on=text_on)
    client = await real_client(api or StubApi(), monkeypatch, recorder, **kw)
    try:
        yield agent, writer, client, store
    finally:
        await client.close()
        await delete_rows(db, agent)


async def _entries(view: TableContextView, **params: str) -> list[dict[str, Any]]:
    async with rest(view=view) as http:
        return (await http.get("/context/log", params=params)).json()["entries"]


# ---------------------------------------------------------------------------------------------------------------------
# Prod data (bugs 1-4)
# ---------------------------------------------------------------------------------------------------------------------


async def test_bug1_duration_is_set(db, monkeypatch):
    """Bug 1: duration_ms was NULL on every context_log row. A chat call that takes 30 ms has it, in the row and on
    the endpoint the dashboard reads."""
    async with recording(db, monkeypatch, StubApi(delay=0.03)) as (agent, writer, client, _):
        await turn(_runner(client))
        await settle(writer)
        [row] = await model_rows(db, agent)
        assert row["duration_ms"] >= 30
        [entry] = await _entries(TableContextView(db, agent))
        assert entry["duration_ms"] == row["duration_ms"]


async def test_bug2_no_null_response_columns_on_ok_rows(db, monkeypatch):
    """Bug 2: response columns were NULL on 14% of subtask rows and 2.5% of chat rows. Every ok row of a chat call,
    a streamed chat and a background subtask (aggregated stream) has every response column."""
    async with recording(db, monkeypatch) as (agent, writer, client, _):
        await turn(_runner(client), "chat-1")
        assert await _drive(_runner(client), "stream_chat", "stream-1") == "done"
        sub = ExecutionContext(kind="subtask", session_id="sub-1", parent_session_id="chat-1", subtask_id=uuid4())
        await asyncio.wait_for(
            _runner(client).run_turn("sub-1", "go", is_subtask=True, is_background=True, context=sub), WAIT
        )
        await settle(writer)
        rows = await model_rows(db, agent)
        assert sorted((r["purpose"], r["call_kind"], r["status"]) for r in rows) == [
            ("chat", "call", "ok"),
            ("chat", "stream", "ok"),
            ("subtask", "aggregated", "ok"),
        ]
        for row in rows:
            assert all(row[c] is not None for c in RESPONSE_COLUMNS), {c: row[c] for c in RESPONSE_COLUMNS}


async def test_bug3_input_total_is_uncached_plus_cache_read_plus_cache_write(db, monkeypatch):
    """Bug 3: input_tokens_actual averaged 2 because the real input is in the cache columns. The row's input_total
    is the sum; the endpoints add it as input_total_tokens / input_total and keep input_tokens_actual uncached."""
    async with recording(db, monkeypatch) as (agent, writer, client, _):
        await turn(_runner(client))
        await settle(writer)
        [row] = await model_rows(db, agent)
        assert (row["input_uncached"], row["cache_read"], row["cache_write"], row["input_total"]) == (3, 2000, 50, 2053)
        view = TableContextView(db, agent)
        async with rest(view=view) as http:
            [entry] = (await http.get("/context/log")).json()["entries"]
            summary = (await http.get("/dashboard/cache")).json()["summary"]
        assert (entry["input_tokens_actual"], entry["input_total_tokens"]) == (3, 2053)
        assert (entry["cache_read"], entry["cache_creation"]) == (2000, 50)
        assert summary["input_total"] == summary["total_input_tokens"] == 2053


async def test_bug4_dynamic_check_not_labelled_heartbeat(db, monkeypatch):
    """Bug 4: background dynamic checks were labelled heartbeat. A check run on the heartbeat's fork is a
    background_check row; the triage is heartbeat."""
    async with recording(db, monkeypatch) as (agent, writer, client, _):
        for kind, sid in (("heartbeat_check", "dynamic-check-disk-1"), ("heartbeat_triage", "heartbeat-1")):
            fork = _runner(client).fork(client)
            ctx = ExecutionContext(kind=kind, session_id=sid)
            await asyncio.wait_for(fork.run_turn(sid, "check", is_background=True, context=ctx), WAIT)
        await settle(writer)
        assert {r["session_id"]: r["purpose"] for r in await model_rows(db, agent)} == {
            "dynamic-check-disk-1": "background_check",
            "heartbeat-1": "heartbeat",
        }
        [entry] = await _entries(TableContextView(db, agent), session_id="dynamic-check-disk-1")
        assert entry["call_type"] == "background_check"


# ---------------------------------------------------------------------------------------------------------------------
# Code audit (bugs 5-15)
# ---------------------------------------------------------------------------------------------------------------------


async def test_bug5_critic_call_is_recorded(db, monkeypatch):
    """Bug 5: background calls (the critic first) went straight to the client and were recorded nowhere."""
    verdict = json.dumps(
        {
            "complexity": "moderate",
            "routing": "single",
            "frames": ["task"],
            "skills": [],
            "rationale": "a question",
            "per_frame_instructions": {},
        }
    )
    async with recording(db, monkeypatch, StubApi(lambda body: message(verdict))) as (agent, writer, client, _):
        critic = CriticAgent(Settings(_env_file=None, critic_model=MODEL))
        critic.set_api_client(client)
        heuristic = FrameSelection(
            frame_id="conversation", frame_name="Conversation", confidence=0.5, match_method="default"
        )
        result = await asyncio.wait_for(
            critic.classify("Should we ship the release tonight?", heuristic, ["conversation", "task"]), WAIT
        )
        assert result.recommended_frame == "task"
        await settle(writer)
        [row] = await model_rows(db, agent)
        assert (row["purpose"], row["label_source"], row["status"], row["model"]) == ("critic", "explicit", "ok", MODEL)
        assert (row["input_uncached"], row["output_tokens"]) == (3, 7)


async def test_bug6_concurrent_sessions(db, monkeypatch):
    """Bug 6: chat and the subtask worker share the runner, so one session's response landed on another's row. Two
    sessions interleaved on one runner each get their own rows, with their own usage."""
    both = asyncio.Event()
    arrived: list[str] = []
    seen: dict[str, int] = {}

    async def reply(body: dict[str, Any]) -> dict[str, Any]:
        who = "A" if "go A" in json.dumps(body["messages"]) else "B"
        seen[who] = seen.get(who, 0) + 1
        usage = {
            "input_tokens": 111 if who == "A" else 222,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "output_tokens": 7,
        }
        if seen[who] == 1:
            arrived.append(who)
            if len(arrived) == 2:
                both.set()
            await asyncio.wait_for(both.wait(), WAIT)  # both first calls are in flight together
            return message(tool="lookup", usage=usage)
        return message(usage=usage)

    async with recording(db, monkeypatch, StubApi(reply)) as (agent, writer, client, _):
        r = _runner(client)
        await asyncio.wait_for(asyncio.gather(r.run_turn("sA", "go A"), r.run_turn("sB", "go B")), WAIT)
        await settle(writer)
        rows = await model_rows(db, agent)
        assert len(rows) == 4 and all(r_["status"] == "ok" for r_ in rows)
        assert {s: {r_["input_uncached"] for r_ in rows if r_["session_id"] == s} for s in ("sA", "sB")} == {
            "sA": {111},
            "sB": {222},
        }
        assert len({r_["turn_id"] for r_ in rows if r_["session_id"] == "sA"}) == 1
        assert {r_["turn_id"] for r_ in rows if r_["session_id"] == "sA"}.isdisjoint(
            {r_["turn_id"] for r_ in rows if r_["session_id"] == "sB"}
        )


async def test_bug7_error_rows_exist_and_count_in_cache_totals(db, monkeypatch):
    """Bug 7: errors were never recorded and /dashboard/cache dropped rows without input tokens, so failures
    vanished. A refused call is an error row, listed, and counted in the cache totals."""
    replies = iter([message(), error_response(400, "invalid_request_error", "max_tokens: too large")])
    async with recording(db, monkeypatch, StubApi(lambda body: next(replies))) as (agent, writer, client, _):
        await turn(_runner(client), "ok-1")
        with contextlib.suppress(Exception):  # the turn's own handling of the failure is not this bug
            await turn(_runner(client), "err-1")
        await settle(writer)
        failed = [r for r in await model_rows(db, agent) if r["session_id"] == "err-1"]
        assert [(r["status"], r["http_status"]) for r in failed] == [("error", 400)]
        assert "max_tokens: too large" in failed[0]["error_text"]
        view = TableContextView(db, agent)
        async with rest(view=view) as http:
            summary = (await http.get("/dashboard/cache")).json()["summary"]
            listed = (await http.get("/context/log", params={"session_id": "err-1"})).json()["entries"]
        assert (summary["total_calls"], summary["error_calls"]) == (2, 1)
        assert [(e["status"], e["input_tokens_actual"]) for e in listed] == [("error", None)]


async def test_bug8_gate_and_reflection_complete(db, monkeypatch):
    """Bug 8: the action gate's and the end-of-conversation reflection's rows were never completed, and the gate
    inherited the chat label. Both are complete rows under their own purpose, shown on the endpoints."""
    replies = iter([message(tool="send_email"), message(), message("Lesson: keep it short.")])

    def reply(body: dict[str, Any]) -> dict[str, Any]:
        if is_gate(body):
            return message('{"approved": true, "reason": "fine"}', usage=GATE_USAGE)
        return next(replies)

    # each call takes longer than a clock tick, so the rows' start times give their order
    async with recording(db, monkeypatch, StubApi(reply, delay=TICK)) as (agent, writer, client, _):
        r = _runner(client, offered=("send_email",), action_gating_enabled=True)
        turn_id = uuid4()
        ledger = ExecutionLedger(session_id="s1")
        await asyncio.wait_for(_run_loop(r, ledger=ledger, turn_number=4, turn_id=turn_id), WAIT)
        r = _runner(client)
        r._conversations["s1"] = Conversation(
            session_id="s1",
            messages=[Message(role=role, content=f"m{i}") for i, role in enumerate(["user", "assistant"] * 3)],
        )
        assert await asyncio.wait_for(r.end_conversation("s1"), WAIT) is True
        await settle(writer)
        rows = await model_rows(db, agent)
        assert [r_["purpose"] for r_ in rows] == ["chat", "action_gate", "chat", "reflection"]
        gate, reflection = rows[1], rows[3]
        assert (gate["input_uncached"], gate["output_tokens"], gate["turn_number"]) == (41, 9, 4)
        assert gate["turn_id"] == rows[0]["turn_id"] == rows[2]["turn_id"] == turn_id
        for row in (gate, reflection):
            assert row["status"] == "ok" and all(row[c] is not None for c in RESPONSE_COLUMNS)
        listed = await _entries(TableContextView(db, agent), session_id="s1")
        assert sorted(e["call_type"] for e in listed) == ["action_gate", "chat", "chat", "reflection"]


async def test_bug9_trace_id_set(db, monkeypatch):
    """Bug 9: trace_id was always NULL, so the F035 correlation join was dead. A turn's calls carry its trace id."""
    async with recording(db, monkeypatch) as (agent, writer, client, _):
        await turn(_runner(client, preset=preset(trace_id=TRACE)))
        await settle(writer)
        [row] = await model_rows(db, agent)
        assert row["trace_id"] == TRACE
        [entry] = await _entries(TableContextView(db, agent))
        assert entry["trace_id"] == TRACE


async def test_bug10_unknown_heading_reported_by_name(db, monkeypatch):
    """Bug 10: an unknown ``## X`` heading was folded into the section before it, and the preamble block was left
    out. Both are sections of their own, in the row and in the section text the endpoint serves."""
    prompt = "## Current Date/Time\nnow\n\n## Brand New Rules\nbe brief"
    async with recording(db, monkeypatch, text_on=True) as (agent, writer, client, store):
        await turn(_runner(client, preset=preset(system_prompt=prompt)))
        await settle(writer)
        [row] = await model_rows(db, agent)
        assert {"preamble", "datetime", "brand_new_rules"} <= set(row["sections_present"])
        assert row["section_tokens_est"]["brand_new_rules"] > 0 and row["section_tokens_est"]["preamble"] > 0
        async with rest(view=TableContextView(db, agent, store)) as http:
            sections = (await http.get(f"/context/log/{row['id']}/sections")).json()
        assert "be brief" in sections["sections_text"]["brand_new_rules"]
        assert "be brief" not in sections["sections_text"]["datetime"]


async def test_bug11_views_read_the_table(db, monkeypatch):
    """Bug 11: every dashboard view read the in-memory ring, so a restart emptied them. With the flag on the ring
    writes nothing to the database and the views read the table: after a restart (a new view, no ring) every call
    is still listed, and the cache numbers are the table's."""
    async with recording(db, monkeypatch) as (agent, writer, client, _):
        ring = ContextLogger()  # main.py's ring with the flag on: no db_writer
        r = _runner(client)
        r.set_context_logger(ring)
        await turn(r, "s1")
        await turn(r, "s2")
        await settle(writer, ring)
        ids = [e.id for e in ring.get_recent()]
        restarted = TableContextView(db, agent)
        async with rest(view=restarted) as http:
            assert [e["id"] for e in (await http.get("/context/log")).json()["entries"]] == ids
            assert [e["id"] for e in (await http.get("/dashboard/observability")).json()["context_log"]] == ids
            assert (await http.get("/dashboard/cache")).json()["summary"]["total_calls"] == 2
            assert (await http.get(f"/context/log/{ids[0]}")).json()["session_id"] == "s2"
            diff = (await http.get("/context/diff", params={"a": ids[1], "b": ids[0]})).json()
            assert diff["a"] == ids[1] and diff["messages_delta"] == 0


class _BigResults(_RecordingDispatcher):
    """Each tool returns a long result, as a file read or a search does."""

    async def dispatch(self, name, inp, **kwargs):
        await super().dispatch(name, inp, **kwargs)
        return "\n".join(f"line {i}: {uuid4().hex} {uuid4().hex}" for i in range(60)), False


def _tool_result_chars(body: dict[str, Any]) -> int:
    """The characters of tool-result content a request carries."""
    total = 0
    for msg in body["messages"]:
        for block in msg["content"] if isinstance(msg["content"], list) else []:
            if block.get("type") == "tool_result":
                content = block.get("content")
                if isinstance(content, str):
                    total += len(content)
                else:
                    total += sum(len(b.get("text", "")) for b in content or [])
    return total


async def test_bug12_tool_results_in_estimate(db, monkeypatch):
    """Bug 12: the message estimate ignored tool-result content, the largest part of a long tool loop. The call
    after a tool counts its result."""
    replies = iter([message(tool="lookup"), message()])
    api = StubApi(lambda body: next(replies))
    async with recording(db, monkeypatch, api) as (agent, writer, client, _):
        r = _runner(client)
        r.set_dispatcher(_BigResults(["lookup"]))
        await turn(r)
        await settle(writer)
        first, second = await model_rows(db, agent)
        result_chars = _tool_result_chars(api.bodies[1])
        assert result_chars > 2000
        grown = second["section_tokens_est"]["messages"] - first["section_tokens_est"]["messages"]
        assert grown >= result_chars // 4


async def test_bug13_write_failure_is_warning_and_counted(db, monkeypatch, caplog):
    """Bug 13: write failures logged at DEBUG. A row the database refuses is a WARNING and counted, and the turn
    goes on."""
    async with recording(db, monkeypatch) as (agent, writer, client, _):
        client._recorder._agent_id = None  # agent_id is NOT NULL: the database refuses the row
        with caplog.at_level(logging.WARNING):
            assert await turn(_runner(client)) == "done"
            await settle(writer)
        assert await model_rows(db, agent) == []
        stats = client._recorder.stats
        assert (stats.dropped_error, stats.written) == (1, 0)
        assert [r for r in caplog.records if r.levelno == logging.WARNING and "write failed" in r.getMessage()]


async def test_bug14_call_types_distinct(db, monkeypatch):
    """Bug 14: continuations, DAG summaries and MCP turns were all "chat". Each has its own call type."""
    async with recording(db, monkeypatch) as (agent, writer, client, _):
        await turn(_runner(client), "s1")
        contexts = [
            ExecutionContext(
                kind="continuation",
                session_id="intent-1",
                authority=AUTHORITY_INTERNAL,
                intention_id=uuid4(),
                root_intention_id=uuid4(),
            ),
            ExecutionContext(kind="dag_summary", session_id="dag-summary-1", dag_id=uuid4()),
            ExecutionContext(kind="mcp", session_id="mcp-1"),
        ]
        for ctx in contexts:
            await asyncio.wait_for(_run_loop(_runner(client), context=ctx, is_background=ctx.is_background), WAIT)
        await settle(writer)
        listed = await _entries(TableContextView(db, agent))
        assert sorted(e["call_type"] for e in listed) == ["chat", "continuation", "dag_summary", "mcp"]


@pytest.mark.parametrize("backend", ["httpx", "sdk"])
async def test_bug15_attempts_and_tool_choice_fallback_visible(backend, db, monkeypatch):
    """Bug 15: retries and the forced tool_choice re-send left no trace: two API calls, zero rows. A retried call is
    one row with its attempts; the re-send is a second row of the same group, marked fallback."""
    monkeypatch.setattr(nous.handlers, "_REJECTS_FORCED_TOOL_CHOICE", set())
    rejection = 'tool_choice: type "tool" and "any" are not supported for this model.'
    answer = {"type": "tool_use", "id": "t1", "name": "answer_tool", "input": {"answer": "yes"}}
    replies = iter(
        [
            error_response(529, "overloaded_error", "Overloaded", retry_soon=True),
            message(),
            error_response(400, "invalid_request_error", rejection),
            {**message(), "content": [answer], "stop_reason": "tool_use"},
        ]
    )
    async with recording(db, monkeypatch, StubApi(lambda body: next(replies)), backend=backend) as (
        agent,
        writer,
        client,
        _,
    ):
        await turn(_runner(client))
        forced = {
            "model": MODEL,
            "max_tokens": 50,
            "system": [{"type": "text", "text": "sys"}],
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
            "tools": [{"name": "answer_tool", "description": "d", "input_schema": {"type": "object"}}],
            "tool_choice": {"type": "tool", "name": "answer_tool"},
        }
        assert await call_with_tool_choice(client, forced, purpose="sleep_reflection") == {"answer": "yes"}
        await settle(writer)
        rows = await model_rows(db, agent)
        [chat] = [r for r in rows if r["purpose"] == "chat"]
        assert (chat["status"], chat["attempts"]) == ("ok", 2)
        assert [a["http_status"] for a in await attempt_rows(db, chat["id"])] == [529, 200]
        # the two requests may share a clock tick: tell them apart by what they are, not by their order
        sent = sorted((r for r in rows if r["purpose"] == "sleep_reflection"), key=lambda r: r["is_fallback"])
        assert [(r["status"], r["http_status"], r["is_fallback"]) for r in sent] == [
            ("error", 400, False),
            ("ok", 200, True),
        ]
        assert sent[0]["group_id"] is not None and sent[0]["group_id"] == sent[1]["group_id"]
