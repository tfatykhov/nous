"""F108 Phase 1, PR 4: the context endpoints read the model-call table, and the table is the one recorder.

With ``NOUS_MODEL_CALL_LOG_ENABLED`` on, ``/context/log*``, ``/context/diff``, ``/dashboard/cache`` and the
``context_log`` list of ``/dashboard/observability`` read ``nous_system.model_call_log`` through a
``TableContextView``, and the F035.4 ring writes no ``context_log`` row. Off, they read the ring through a
``RingContextView`` exactly as before.

The harness here drives the real path: a real ``AgentRunner`` over a real Anthropic client (``create_client``, the
httpx backend) whose HTTP goes to a stubbed Messages API (``httpx.MockTransport``), recording through a real
``ModelCallRecorder`` into real Postgres under a per-test agent id, read back through the views and the REST app
(``create_app``, served in this event loop by ``httpx.ASGITransport``). Nothing reaches the network. Every wait is
bounded. ``tests/test_recording_bug_regressions.py`` uses the same harness.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from sqlalchemy import text
from test_runner_call_context import _runner

from nous import main
from nous.api import anthropic_client
from nous.api.execution_context import ExecutionContext
from nous.api.rest import create_app
from nous.cognitive.schemas import FrameSelection, TurnContext
from nous.config import Settings
from nous.handlers import call_background_llm
from nous.observability.context_logger import ContextLogger
from nous.observability.context_view import RingContextView, TableContextView
from nous.observability.model_call_text import TextStore
from nous.observability.model_calls import ModelCallRecorder
from nous.observability.recording import BoundedWriter

MODEL = "claude-sonnet-5-5"
USAGE = {"input_tokens": 3, "cache_read_input_tokens": 2000, "cache_creation_input_tokens": 50, "output_tokens": 7}
WAIT = 10.0
# Longer than one tick of the wall clock a row's started_at is read from (about 16 ms on Windows before Python 3.13).
TICK = 0.02

# The keys each endpoint returns today (base b82e940e), read off rest.py and ContextLogEntry.to_dict(). The ring path
# keeps exactly these; the table path adds exactly the TABLE_* keys.
ENTRY_KEYS = frozenset(
    {
        "id",
        "session_id",
        "turn_number",
        "timestamp",
        "call_type",
        "model",
        "frame_id",
        "trace_id",
        "token_breakdown",
        "total_tokens_est",
        "context_window_size",
        "utilization_pct",
        "sections_present",
        "tools_count",
        "tool_names",
        "messages_count",
        "message_roles",
        "loaded_facts",
        "loaded_decisions",
        "loaded_procedures",
        "loaded_episodes",
        "recent_conversations",
        "input_tokens_actual",
        "output_tokens",
        "cache_creation",
        "cache_read",
        "duration_ms",
        "stop_reason",
    }
)
TABLE_ENTRY_KEYS = frozenset({"input_total_tokens", "status", "purpose", "attempts", "turn_id"})
SECTIONS_KEYS = frozenset({"sections", "sections_text", "total_tokens_est", "sections_present"})
DIFF_KEYS = frozenset(
    {"a", "b", "token_delta", "sections_added", "sections_removed", "tools_added", "tools_removed", "messages_delta"}
)
CACHE_KEYS = frozenset({"summary", "break_components", "sessions", "timeline"})
CACHE_SUMMARY_KEYS = frozenset(
    {
        "total_calls",
        "total_input_tokens",
        "total_cache_read",
        "total_cache_created",
        "overall_hit_rate",
        "total_breaks",
        "break_rate",
        "tokens_lost_to_breaks",
    }
)
TABLE_CACHE_SUMMARY_KEYS = frozenset({"error_calls", "input_total"})
CACHE_SESSION_KEYS = frozenset(
    {"session_id", "calls", "input_tokens", "cache_read", "cache_created", "hit_rate", "breaks"}
)
CACHE_TIMELINE_KEYS = frozenset(
    {
        "timestamp",
        "session_id",
        "turn",
        "model",
        "input_tokens",
        "cache_read",
        "cache_created",
        "hit_rate",
        "cache_break",
        "break_components",
    }
)


# ---------------------------------------------------------------------------------------------------------------------
# The stub Messages API
# ---------------------------------------------------------------------------------------------------------------------


def message(
    text_: str = "done", usage: dict[str, int] | None = None, *, tool: str | None = None, stop: str | None = None
) -> dict[str, Any]:
    """One Messages API response body: a text reply, or a call of ``tool``."""
    content = (
        [{"type": "tool_use", "id": f"t-{uuid.uuid4().hex[:6]}", "name": tool, "input": {}}]
        if tool
        else [{"type": "text", "text": text_}]
    )
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": MODEL,
        "content": content,
        "stop_reason": stop or ("tool_use" if tool else "end_turn"),
        "stop_sequence": None,
        "usage": dict(usage or USAGE),
    }


def error_response(status: int, kind: str, text_: str, *, retry_soon: bool = False) -> httpx.Response:
    """An API error; ``retry_soon`` makes a client's retry wait a millisecond (both backends honour it)."""
    headers = {"retry-after-ms": "1"} if retry_soon else None
    return httpx.Response(status, headers=headers, json={"type": "error", "error": {"type": kind, "message": text_}})


def _sse(body: dict[str, Any]) -> httpx.Response:
    """``body`` (a whole message) as the event stream the API sends for it."""
    usage = body["usage"]
    events: list[dict[str, Any]] = [
        {
            "type": "message_start",
            "message": {**body, "content": [], "stop_reason": None, "usage": {**usage, "output_tokens": 1}},
        }
    ]
    for i, block in enumerate(body["content"]):
        if block["type"] == "tool_use":
            start = {**block, "input": {}}
            delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
        else:
            start, delta = {"type": "text", "text": ""}, {"type": "text_delta", "text": block["text"]}
        events += [
            {"type": "content_block_start", "index": i, "content_block": start},
            {"type": "content_block_delta", "index": i, "delta": delta},
            {"type": "content_block_stop", "index": i},
        ]
    events += [
        {
            "type": "message_delta",
            "delta": {"stop_reason": body["stop_reason"], "stop_sequence": None},
            "usage": {"output_tokens": usage["output_tokens"]},
        },
        {"type": "message_stop"},
    ]
    payload = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=payload.encode())


Reply = Callable[[dict[str, Any]], "dict[str, Any] | httpx.Response | Awaitable[dict[str, Any] | httpx.Response]"]


class StubApi:
    """The Messages API, as an ``httpx.MockTransport`` handler: ``reply(body)`` gives a message body (sent as JSON,
    or as an event stream when the request asks for one) or a whole ``httpx.Response``, or a coroutine giving
    either. ``delay`` holds each reply that long (a blocking sleep: on Windows a loop timer can fire early)."""

    def __init__(self, reply: Reply | None = None, delay: float = 0.0) -> None:
        self._reply = reply or (lambda body: message())
        self.delay = delay
        self.bodies: list[dict[str, Any]] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.bodies.append(body)
        if self.delay:
            time.sleep(self.delay)
        reply = self._reply(body)
        if asyncio.iscoroutine(reply):
            reply = await reply
        if isinstance(reply, httpx.Response):
            return reply
        return _sse(reply) if body.get("stream") else httpx.Response(200, json=reply)


def is_gate(body: dict[str, Any]) -> bool:
    """Whether ``body`` is the F026 action gate's request."""
    return "safety gate" in json.dumps(body.get("system"))


# ---------------------------------------------------------------------------------------------------------------------
# The recording stack
# ---------------------------------------------------------------------------------------------------------------------


def new_agent() -> str:
    return f"test-awv-cv-{uuid.uuid4().hex[:12]}"


def recorder_for(db: Any, agent: str, *, text_on: bool = False) -> tuple[BoundedWriter, ModelCallRecorder, Any]:
    """The shared writer, a recorder writing ``agent``'s rows, and its text store (None unless ``text_on``)."""
    writer = BoundedWriter("test")
    store = None
    if text_on:
        settings = SimpleNamespace(
            model_call_text_enabled=True, model_call_log_enabled=True, model_call_text_block_max_chars=200_000
        )
        store = TextStore(writer, db, agent, settings)
    return writer, ModelCallRecorder(writer, db, agent, SimpleNamespace(), text_store=store), store


async def real_client(api: StubApi, monkeypatch: Any, recorder: ModelCallRecorder | None, backend: str = "httpx"):
    """A started Anthropic client of ``backend`` whose HTTP goes to ``api``, recording through ``recorder``."""
    monkeypatch.setattr(
        anthropic_client, "_build_transport_with_env_proxies", lambda **_: (httpx.MockTransport(api), {})
    )
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(_env_file=None, api_backend=backend, ANTHROPIC_API_KEY="test-key")
    client = anthropic_client.create_client(settings, recorder=recorder)
    await client.start()
    return client


async def model_rows(db: Any, agent: str) -> list[dict[str, Any]]:
    async with db.engine.connect() as conn:
        result = await conn.execute(
            text("SELECT * FROM nous_system.model_call_log WHERE agent_id = :a ORDER BY started_at, id"), {"a": agent}
        )
        return [dict(r) for r in result.mappings().all()]


async def attempt_rows(db: Any, call_id: uuid.UUID) -> list[dict[str, Any]]:
    async with db.engine.connect() as conn:
        result = await conn.execute(
            text("SELECT * FROM nous_system.model_call_attempt WHERE call_id = :c ORDER BY attempt_no"), {"c": call_id}
        )
        return [dict(r) for r in result.mappings().all()]


async def context_log_rows(db: Any, agent: str) -> list[dict[str, Any]]:
    async with db.engine.connect() as conn:
        result = await conn.execute(text("SELECT * FROM nous_system.context_log WHERE agent_id = :a"), {"a": agent})
        return [dict(r) for r in result.mappings().all()]


async def delete_rows(db: Any, agent: str) -> None:
    """This test's rows: the model calls (their attempts and text refs go with them), its text blocks and its
    context_log rows."""
    async with db.engine.begin() as conn:
        for table in ("model_call_log", "model_text_block", "context_log"):
            await conn.execute(text(f"DELETE FROM nous_system.{table} WHERE agent_id = :a"), {"a": agent})


async def settle(writer: BoundedWriter, ring: ContextLogger | None = None) -> None:
    """Every write handed over so far has landed: the shared writer's, and the ring's own fire-and-forget ones."""
    await writer.drain(WAIT)
    if ring is not None:

        async def ring_done() -> None:
            while ring._pending_tasks:
                await asyncio.wait(set(ring._pending_tasks))

        await asyncio.wait_for(ring_done(), WAIT)


class _NoDatabase:
    """The app's own database is down: the observability sections that query it take their fallback. The views
    are handed the real database themselves."""

    @contextlib.asynccontextmanager
    async def session(self):
        raise ConnectionError("no database in this test")
        yield


@contextlib.asynccontextmanager
async def rest(view: Any = None, context_logger: Any = None):
    """A client of the REST app over ``view`` (or the ring of ``context_logger``), served in this event loop."""
    app = create_app(
        runner=MagicMock(),
        brain=MagicMock(),
        heart=MagicMock(),
        cognitive=MagicMock(),
        database=_NoDatabase(),
        settings=Settings(_env_file=None),
        context_logger=context_logger,
        context_view=view,
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://nous") as client:
        yield client


def preset(**overrides: Any) -> TurnContext:
    """The turn context the mock cognitive layer hands every turn: a conversation frame, and ``overrides``."""
    frame = FrameSelection(frame_id="conversation", frame_name="Conversation", confidence=0.9, match_method="default")
    return TurnContext(**{"system_prompt": "You are Nous.", "frame": frame, **overrides})


async def turn(r: Any, session_id: str = "s1", message_: str = "go") -> str:
    reply, _ctx, _usage = await asyncio.wait_for(r.run_turn(session_id, message_), WAIT)
    return reply


# ---------------------------------------------------------------------------------------------------------------------
# One recorder: the cutover
# ---------------------------------------------------------------------------------------------------------------------


@pytest.mark.postgres_only
@pytest.mark.parametrize("cutover", [True, False], ids=["flag on", "flag off"])
async def test_cutover_stops_context_log_writes(cutover, db, monkeypatch):
    """main.py's ContextLogger with the model-call log on writes no context_log row (the table is the one recorder);
    off, it writes the row and its response update exactly as before, and nothing writes the table."""
    agent = new_agent()
    settings = Settings(_env_file=None, agent_id=agent, model_call_log_enabled=cutover)
    writer, recorder, _ = recorder_for(db, agent)
    client = await real_client(StubApi(), monkeypatch, recorder if cutover else None)
    ring = main._build_context_logger(settings, db)
    try:
        r = _runner(client)
        r.set_context_logger(ring)
        assert await turn(r) == "done"
        await settle(writer, ring)
        rows, logged = await model_rows(db, agent), await context_log_rows(db, agent)
        [entry] = ring.get_recent()
        if cutover:
            assert len(rows) == 1 and logged == []
            assert entry.id == str(rows[0]["id"])  # the ring entry is the row's: its payload and text are found by it
        else:
            assert rows == []
            [row] = logged
            assert (row["id"], row["session_id"], row["call_type"]) == (entry.id, "s1", "chat")
            assert (row["input_tokens_actual"], row["output_tokens"], row["cache_read"]) == (3, 7, 2000)
            assert len(entry.id) == 16  # today's ring id
    finally:
        await client.close()
        await delete_rows(db, agent)


def test_main_builds_the_ring_view_with_the_flag_off_and_the_table_view_with_it_on():
    """The wiring: the table view only with the model-call log on, so nothing reads the new tables while it is off."""
    import ast
    import inspect

    source = inspect.getsource(main.create_components)
    tree = ast.parse(source)
    built: dict[str, list[str]] = {}

    def visit(node: ast.AST, switches: list[str]) -> None:
        if isinstance(node, ast.If):
            for child in node.body:
                visit(child, [*switches, ast.unparse(node.test)])
            for child in node.orelse:
                visit(child, [*switches, f"not ({ast.unparse(node.test)})"])
            return
        if isinstance(node, ast.Call) and ast.unparse(node.func) in ("TableContextView", "RingContextView"):
            built[ast.unparse(node.func)] = switches
        for child in ast.iter_child_nodes(node):
            visit(child, switches)

    visit(tree, [])
    assert built == {
        "TableContextView": ["settings.model_call_log_enabled"],
        "RingContextView": ["not (settings.model_call_log_enabled)", "context_logger is not None"],
    }


# ---------------------------------------------------------------------------------------------------------------------
# The views
# ---------------------------------------------------------------------------------------------------------------------


async def _both_ways(db: Any, monkeypatch: Any, agent: str, **runner_settings: Any):
    """Turns recorded both ways at once: a ring (no database writes) and a recording client, so each call has a
    ring entry and a row with the same id. Two turns in s1 (the second calls a tool first), one in s2."""
    writer, recorder, _ = recorder_for(db, agent)
    replies = iter([message(), message(tool="lookup"), message("again"), message()])
    # Each call takes longer than a clock tick (about 16 ms on Windows' Python 3.12), so no two share a start time
    # and the table's newest-first order is the ring's.
    client = await real_client(StubApi(lambda body: next(replies), delay=TICK), monkeypatch, recorder)
    ring = ContextLogger(full_payload_enabled=True)
    r = _runner(client, **runner_settings)
    r.set_context_logger(ring)
    await turn(r, "s1")
    await turn(r, "s1", "again")
    await turn(r, "s2")
    await settle(writer, ring)
    return client, ring


def _keys(value: Any) -> frozenset[str]:
    return frozenset(value)


@pytest.mark.postgres_only
async def test_table_view_response_shapes_match_ring_view(db, monkeypatch):
    agent = new_agent()
    client, ring = await _both_ways(db, monkeypatch, agent)
    try:
        table = TableContextView(db, agent, None, ring)
        ids = [e.id for e in ring.get_recent()]
        assert len(ids) == 4
        a, b = ids[-1], ids[0]
        responses: dict[str, dict[str, Any]] = {}
        for name, app in (("ring", rest(context_logger=ring)), ("table", rest(view=table))):
            async with app as http:
                got = {
                    "list": (await http.get("/context/log")).json(),
                    "detail": (await http.get(f"/context/log/{a}")).json(),
                    "sections": (await http.get(f"/context/log/{a}/sections")).json(),
                    "diff": (await http.get("/context/diff", params={"a": a, "b": b})).json(),
                    "observability": (await http.get("/dashboard/observability")).json(),
                    "cache": (await http.get("/dashboard/cache")).json(),
                }
            responses[name] = got
        ring_r, table_r = responses["ring"], responses["table"]

        # The ring path: today's keys exactly.
        assert {_keys(e) for e in ring_r["list"]["entries"]} == {ENTRY_KEYS}
        assert _keys(ring_r["detail"]) == ENTRY_KEYS
        assert _keys(ring_r["sections"]) == SECTIONS_KEYS
        assert _keys(ring_r["diff"]) == DIFF_KEYS
        assert {_keys(e) for e in ring_r["observability"]["context_log"]} == {ENTRY_KEYS}
        assert _keys(ring_r["cache"]) == CACHE_KEYS and _keys(ring_r["cache"]["summary"]) == CACHE_SUMMARY_KEYS
        # The table path: the same keys, plus exactly the listed ones.
        assert {_keys(e) for e in table_r["list"]["entries"]} == {ENTRY_KEYS | TABLE_ENTRY_KEYS}
        assert _keys(table_r["detail"]) == ENTRY_KEYS | TABLE_ENTRY_KEYS
        assert _keys(table_r["sections"]) == SECTIONS_KEYS
        assert _keys(table_r["diff"]) == DIFF_KEYS
        assert {_keys(e) for e in table_r["observability"]["context_log"]} == {ENTRY_KEYS | TABLE_ENTRY_KEYS}
        assert _keys(table_r["cache"]) == CACHE_KEYS
        assert _keys(table_r["cache"]["summary"]) == CACHE_SUMMARY_KEYS | TABLE_CACHE_SUMMARY_KEYS
        for r_ in (ring_r, table_r):
            assert {_keys(s) for s in r_["cache"]["sessions"]} == {CACHE_SESSION_KEYS}
            assert {_keys(t) for t in r_["cache"]["timeline"]} == {CACHE_TIMELINE_KEYS}

        # Same calls, same values, newest first, wherever both record the same thing.
        same = ENTRY_KEYS - {"timestamp", "duration_ms"}  # the ring never had a duration (bug 1)
        assert [e["id"] for e in table_r["list"]["entries"]] == [e["id"] for e in ring_r["list"]["entries"]] == ids
        for ring_e, table_e in zip(ring_r["list"]["entries"], table_r["list"]["entries"], strict=True):
            assert {k: ring_e[k] for k in same} == {k: table_e[k] for k in same}
            assert table_e["duration_ms"] is not None
            assert (table_e["purpose"], table_e["status"], table_e["attempts"]) == ("chat", "ok", 1)
            assert table_e["input_total_tokens"] == 2053 and table_e["turn_id"]
        assert ring_r["sections"]["sections"] == table_r["sections"]["sections"]
        assert ring_r["diff"] == table_r["diff"]
        assert ring_r["cache"]["summary"] == {
            k: v for k, v in table_r["cache"]["summary"].items() if k not in TABLE_CACHE_SUMMARY_KEYS
        }
        assert (table_r["cache"]["summary"]["error_calls"], table_r["cache"]["summary"]["input_total"]) == (0, 4 * 2053)
        assert ring_r["cache"]["sessions"] == table_r["cache"]["sessions"]
    finally:
        await client.close()
        await delete_rows(db, agent)


@pytest.mark.postgres_only
async def test_views_survive_restart(db, monkeypatch):
    """Bug 11: a view built after a restart (a new process: no ring, a new view) sees the rows written before it."""
    agent = new_agent()
    client, ring = await _both_ways(db, monkeypatch, agent)
    ids = [e.id for e in ring.get_recent()]
    await client.close()
    del ring
    try:
        restarted = TableContextView(db, agent, None, None)
        assert [e.id for e in await restarted.recent(None, 20, frozenset())] == ids
        assert [e.id for e in await restarted.recent("s2", 20, frozenset())] == ids[:1]
        assert [e.id for e in await restarted.recent(None, 2, frozenset())] == ids[:2]
        entry = await restarted.get(ids[1])
        assert entry is not None and (entry.session_id, entry.call_type, entry.input_tokens_actual) == ("s1", "chat", 3)
        async with rest(view=restarted) as http:
            assert [e["id"] for e in (await http.get("/context/log")).json()["entries"]] == ids
            assert (await http.get(f"/context/log/{ids[1]}")).json()["id"] == ids[1]
            assert [e["id"] for e in (await http.get("/dashboard/observability")).json()["context_log"]] == ids
            assert (await http.get("/dashboard/cache")).json()["summary"]["total_calls"] == 4
            # no ring and no text: the payload was never captured, as today
            assert (await http.get(f"/context/log/{ids[1]}/payload")).status_code == 404
            assert (await http.get(f"/context/log/{ids[1]}/sections")).json()["sections_text"] == {}
            for bad in ("not-a-uuid", str(uuid.uuid4())):
                assert (await http.get(f"/context/log/{bad}")).status_code == 404
    finally:
        await delete_rows(db, agent)


@pytest.mark.postgres_only
async def test_quiet_purposes_stay_out_of_chat_lists(db, monkeypatch):
    """Heartbeat and background-check calls stay out of the lists asked for without a session id, as the ring's
    heartbeat calls did; asked for by id they are there."""
    agent = new_agent()
    writer, recorder, _ = recorder_for(db, agent)
    client = await real_client(StubApi(), monkeypatch, recorder)
    try:
        r = _runner(client)
        await turn(r, "chat-1")
        check = ExecutionContext(kind="heartbeat_check", session_id="dynamic-check-1")
        triage = ExecutionContext(kind="heartbeat_triage", session_id="heartbeat-1")
        for ctx in (check, triage):
            fork = _runner(client).fork(client)
            await asyncio.wait_for(fork.run_turn(ctx.session_id, "check", is_background=True, context=ctx), WAIT)
        await settle(writer)
        assert sorted(row["purpose"] for row in await model_rows(db, agent)) == [
            "background_check",
            "chat",
            "heartbeat",
        ]
        view = TableContextView(db, agent, None, None)
        async with rest(view=view) as http:
            listed = (await http.get("/context/log")).json()["entries"]
            assert [e["session_id"] for e in listed] == ["chat-1"]
            assert [e["session_id"] for e in (await http.get("/dashboard/observability")).json()["context_log"]] == [
                "chat-1"
            ]
            assert (await http.get("/dashboard/cache")).json()["summary"]["total_calls"] == 1
            for ctx, purpose in ((check, "background_check"), (triage, "heartbeat")):
                one = (await http.get("/context/log", params={"session_id": ctx.session_id})).json()["entries"]
                assert [(e["call_type"], e["purpose"]) for e in one] == [(purpose, purpose)]
    finally:
        await client.close()
        await delete_rows(db, agent)


@pytest.mark.postgres_only
async def test_background_helper_calls_never_appear_in_context_log_endpoint(db, monkeypatch):
    """The model-call table holds every call; the context endpoints show only the runner's own (MAIN_LOOP_PURPOSES):
    not in the list, not by session, not by id, and not their stored text."""
    agent = new_agent()
    writer, recorder, store = recorder_for(db, agent, text_on=True)
    client = await real_client(StubApi(), monkeypatch, recorder)
    try:
        await turn(_runner(client), "s1")
        assert await call_background_llm(client, MODEL, "sys", "q", purpose="critic") == "done"
        summary = await call_background_llm(client, MODEL, "sys", "q", purpose="episode_summarizer", session_id="s1")
        assert summary == "done"
        await settle(writer)
        rows = await model_rows(db, agent)
        assert sorted(row["purpose"] for row in rows) == ["chat", "critic", "episode_summarizer"]
        hidden = [str(row["id"]) for row in rows if row["purpose"] != "chat"]
        for call_id in hidden:
            assert await store.load(uuid.UUID(call_id))  # their text is stored: the endpoints must not serve it
        async with rest(view=TableContextView(db, agent, store, None)) as http:
            for params in ({}, {"session_id": "s1"}):
                listed = (await http.get("/context/log", params=params)).json()["entries"]
                assert [e["purpose"] for e in listed] == ["chat"]
            assert [e["purpose"] for e in (await http.get("/dashboard/observability")).json()["context_log"]] == [
                "chat"
            ]
            assert (await http.get("/dashboard/cache")).json()["summary"]["total_calls"] == 1
            for call_id in hidden:
                assert (await http.get(f"/context/log/{call_id}")).status_code == 404
                assert (await http.get(f"/context/log/{call_id}/payload")).status_code == 404
                assert (await http.get(f"/context/log/{call_id}/sections")).status_code == 404
    finally:
        await client.close()
        await delete_rows(db, agent)


@pytest.mark.postgres_only
async def test_payload_falls_back_to_ring_when_text_off(db, monkeypatch):
    """Text off: the payload and the section text are the ring's (what NOUS_CONTEXT_LOG_FULL_PAYLOAD serves today),
    found by the row's id; without them, 404 "Not captured" as today."""
    agent = new_agent()
    client, ring = await _both_ways(db, monkeypatch, agent, context_log_full_payload=True)
    try:
        call_id = ring.get_recent()[0].id
        async with rest(view=TableContextView(db, agent, None, ring)) as http:
            got = await http.get(f"/context/log/{call_id}/payload")
            assert got.status_code == 200 and got.json() == ring.get_payload(call_id)
            assert got.json()["messages"]
            sections = (await http.get(f"/context/log/{call_id}/sections")).json()
            assert sections["sections_text"] == ring.get_entry(call_id).sections_text != {}
        async with rest(view=TableContextView(db, agent, None, ContextLogger())) as http:
            missing = await http.get(f"/context/log/{call_id}/payload")
            assert (missing.status_code, missing.json()) == (404, {"error": "Not captured"})
    finally:
        await client.close()
        await delete_rows(db, agent)


@pytest.mark.postgres_only
async def test_payload_and_section_text_come_from_the_text_store_when_text_on(db, monkeypatch):
    """Text on: the stored (redacted) text is served, rebuilt as a payload, and the section text is parsed from its
    system blocks as the runner parsed the prompt; no ring is needed."""
    agent = new_agent()
    writer, recorder, store = recorder_for(db, agent, text_on=True)
    secret = "sk-ant-api03-" + "A" * 40
    prompt = f"## Current Date/Time\nnow\n\n## Working Memory\nthe key is {secret}"
    client = await real_client(StubApi(), monkeypatch, recorder)
    try:
        r = _runner(client, preset=preset(system_prompt=prompt))
        assert await turn(r, "s1", "hello there") == "done"
        await settle(writer)
        [row] = await model_rows(db, agent)
        call_id = str(row["id"])
        async with rest(view=TableContextView(db, agent, store, None)) as http:
            payload = (await http.get(f"/context/log/{call_id}/payload")).json()
            assert payload["model"] == row["model"]
            assert [block["type"] for block in payload["system"]] == ["text", "text"]
            assert payload["messages"][-1]["role"] == "user" and "hello there" in json.dumps(payload["messages"])
            assert payload["response"] == [{"type": "text", "text": "done"}]
            assert secret not in json.dumps(payload)
            sections = (await http.get(f"/context/log/{call_id}/sections")).json()
            assert {"preamble", "datetime", "working_memory"} <= set(sections["sections_text"])
            assert list(sections["sections_text"]) == sections["sections_present"] == row["sections_present"]
            assert secret not in sections["sections_text"]["working_memory"]
    finally:
        await client.close()
        await delete_rows(db, agent)


async def test_flag_off_endpoints_serve_the_base_bytes():
    """With the flag off the context endpoints answer byte for byte what the base code (b82e940e) answered: each
    expected body below is that code's expression over the same ring, rendered by the same JSONResponse."""
    from starlette.responses import JSONResponse

    ring = ContextLogger(full_payload_enabled=True)
    for i, (sid, call_type) in enumerate([("c", "chat"), ("h", "heartbeat"), ("t", "subtask"), ("c", "chat")]):
        entry = ring.log(
            session_id=sid,
            turn_number=i,
            call_type=call_type,
            model="m",
            system_prompt=f"## Current Date/Time\nnow\n\n## Relevant Facts\n- fact {i}",
            messages=[{"role": "user", "content": "x" * (10 * i)}],
            tools=[{"name": f"tool{i}"}],
            frame_id="f",
            context_window=1000,
            payload={"sid": sid, "i": i},
        )
        ring.update_response(entry.id, input_tokens=i, output_tokens=2, cache_read=100, stop_reason="end_turn")
    newest, oldest = ring.get_recent()[0], ring.get_recent()[-1]

    def base_diff(a: Any, b: Any) -> dict[str, Any]:
        token_delta = {}
        for s in set(a.token_breakdown) | set(b.token_breakdown):
            d = b.token_breakdown.get(s, 0) - a.token_breakdown.get(s, 0)
            if d != 0:
                token_delta[s] = d
        return {
            "a": a.id,
            "b": b.id,
            "token_delta": {"total": b.total_tokens_est - a.total_tokens_est, "by_section": token_delta},
            "sections_added": [s for s in b.sections_present if s not in a.sections_present],
            "sections_removed": [s for s in a.sections_present if s not in b.sections_present],
            "tools_added": [t for t in b.tool_names if t not in a.tool_names],
            "tools_removed": [t for t in a.tool_names if t not in b.tool_names],
            "messages_delta": b.messages_count - a.messages_count,
        }

    expected = {
        "/context/log": {
            "entries": [e.to_dict() for e in ring.get_recent(session_id=None, limit=20, exclude_call_type="heartbeat")]
        },
        "/context/log?session_id=h&limit=5": {
            "entries": [e.to_dict() for e in ring.get_recent(session_id="h", limit=5, exclude_call_type=None)]
        },
        "/context/log?limit=1": {
            "entries": [e.to_dict() for e in ring.get_recent(session_id=None, limit=1, exclude_call_type="heartbeat")]
        },
        f"/context/log/{oldest.id}": oldest.to_dict(),
        f"/context/log/{oldest.id}/payload": ring.get_payload(oldest.id),
        f"/context/log/{oldest.id}/sections": {
            "sections": oldest.token_breakdown,
            "sections_text": oldest.sections_text,
            "total_tokens_est": oldest.total_tokens_est,
            "sections_present": oldest.sections_present,
        },
        f"/context/diff?a={oldest.id}&b={newest.id}": base_diff(oldest, newest),
    }
    async with rest(context_logger=ring) as http:
        for path, body in expected.items():
            got = await http.get(path)
            assert (got.status_code, got.content) == (200, JSONResponse(body).body), path
        observability = (await http.get("/dashboard/observability")).json()["context_log"]
        base = [e.to_dict() for e in ring.get_recent(limit=10, exclude_call_type="heartbeat")]
        assert json.dumps(observability) == json.dumps(base)
        for path, status, body in [
            ("/context/log/nope", 404, {"error": "Not found"}),
            ("/context/log/nope/payload", 404, {"error": "Not captured"}),
            ("/context/log/nope/sections", 404, {"error": "Not found"}),
            ("/context/diff?a=x", 400, {"error": "Missing parameters"}),
            (f"/context/diff?a={oldest.id}&b=nope", 404, {"error": "Not found"}),
        ]:
            got = await http.get(path)
            assert (got.status_code, got.content) == (status, JSONResponse(body).body), path
    async with rest() as http:  # no context log at all
        assert (await http.get("/context/log")).json() == {"entries": []}
        assert (await http.get("/dashboard/cache")).status_code == 503
        assert (await http.get("/context/log/x")).json() == {"error": "Not enabled"}
        assert (await http.get("/dashboard/observability")).json()["context_log"] == []


def test_ring_view_is_the_ring_unchanged():
    """The ring path returns the ring's own entries and payloads: what every endpoint served before PR 4."""
    ring = ContextLogger(full_payload_enabled=True)
    for sid, call_type in [("c", "chat"), ("h", "heartbeat"), ("t", "subtask")]:
        entry = ring.log(
            session_id=sid,
            turn_number=1,
            call_type=call_type,
            model="m",
            system_prompt="## Identity\nme",
            messages=[],
            tools=None,
            frame_id="f",
            context_window=1000,
            payload={"sid": sid},
        )
        ring.update_response(entry.id, input_tokens=1)
    view = RingContextView(ring)

    async def check() -> None:
        quiet = frozenset({"heartbeat", "background_check"})
        assert [e.session_id for e in await view.recent(None, 10, quiet)] == ["t", "c"]
        assert [e.session_id for e in await view.recent(None, 1, quiet)] == ["t"]
        assert [e.session_id for e in await view.recent("h", 10, frozenset())] == ["h"]
        first = ring.get_recent()[-1]
        assert await view.get(first.id) is first
        assert await view.payload(first.id) == {"sid": "c"}
        assert await view.sections_text(first.id) is first.sections_text
        assert await view.get("nope") is None and await view.sections_text("nope") == {}

    asyncio.run(check())
