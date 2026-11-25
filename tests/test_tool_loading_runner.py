"""F120 tool loading, watch only, in the runner: no request changes, and the per-call record.

Both tool loops are driven for real (``run_turn`` with ``_tool_loop`` over ``call`` and over the aggregated stream of a
background turn, and ``stream_chat``) over the client double of ``test_runner_call_context``, which records each call
through a real ``ModelCallRecorder``. Each payload is serialized when the client receives it. With the setting off,
on with the mode Off, and on with Watch only for every kind, the payloads are byte for byte the same; the record is on
a call's row only in Watch only and only for a switched-on kind, never for a turn of internal authority, a turn with
its own tool list or a call outside the loop. A fault anywhere in making the record leaves the call as it is. A change
made through the route is seen by the next call with no restart. One test writes the record to real Postgres under its
own agent id and deletes its rows.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from starlette.applications import Starlette
from test_runner_authorization import _RecordingDispatcher
from test_runner_background import _MockBrain, _MockCognitive, _MockHeart
from test_runner_call_context import (
    _drive,
    _preset,
    _SpyClient,
    _text,
    _text_segment,
    _tool_segment,
    _tool_use,
)

from nous.api import runner as runner_module
from nous.api import tool_loading
from nous.api.execution_context import ExecutionContext
from nous.api.owner_guard import OwnerGuard
from nous.api.runner import AgentRunner
from nous.api.tool_loading import KINDS, ToolLoadingConfig
from nous.api.tool_loading_routes import build_tool_loading_routes
from nous.brain.intentions import AUTHORITY_INTERNAL
from nous.config import Settings
from nous.observability.model_calls import ModelCallRecorder
from nous.observability.recording import BoundedWriter
from nous.runtime_config import RuntimeConfig

OFFERED = ("bash", "recall_deep", "db_query", "send_email", "dag_create")
AGENT = "test-agent"
WATCH_ALL = ToolLoadingConfig(mode="watch", kinds=frozenset(KINDS))
PATHS = ["run_turn", "stream_chat", "subtask"]
OWNER_VALUE = "owner-only-test-value"
OWNER = {"X-Nous-Owner-Secret": OWNER_VALUE}


@pytest.fixture(autouse=True)
def _fresh_runtime_config(monkeypatch):
    RuntimeConfig.reset()
    monkeypatch.setattr(runner_module, "_tool_loading_fault_logged", False)
    yield
    RuntimeConfig.reset()


def _settings(agent: str = AGENT, **over: Any) -> Settings:
    return Settings(_env_file=None, ANTHROPIC_API_KEY="test-key", agent_id=agent, action_gating_enabled=False, **over)


def _client(stream: bool, sent: list[str]) -> _SpyClient:
    """The double, serializing each payload as it arrives (the runner appends to the messages afterwards)."""
    replies = iter([_tool_use("bash"), _text()])

    def reply(payload: dict[str, Any]) -> Any:
        sent.append(json.dumps(payload))
        return next(replies)

    client = _SpyClient(reply, segments=[_tool_segment("bash"), _text_segment()] if stream else None)
    if stream:
        streamed = client.stream

        async def stream_and_keep(payload: dict[str, Any], *, call_context: Any = None):
            sent.append(json.dumps(payload))
            async for event in streamed(payload, call_context=call_context):
                yield event

        client.stream = stream_and_keep
    return client


def _runner(client: _SpyClient, settings: Settings) -> AgentRunner:
    r = AgentRunner(_MockCognitive(_preset()), _MockBrain(), _MockHeart(), settings)
    r.set_dispatcher(_RecordingDispatcher(list(OFFERED)))
    r.set_api_client(client)
    r._log_f026_decision = lambda *a, **k: None
    return r


async def _turn(
    path: str, settings: Settings, *, context: ExecutionContext | None = None, tool_filter: list[str] | None = None
) -> tuple[list[str], list[dict[str, Any]]]:
    """One turn on ``path`` (a tool call, then the answer): the payloads as sent, and the rows."""
    sent: list[str] = []
    client = _client(path == "stream_chat", sent)
    r = _runner(client, settings)
    if path == "subtask" or context is not None or tool_filter is not None:
        ctx = context or ExecutionContext(kind="subtask", session_id="subtask-1", subtask_id=uuid.uuid4())
        await asyncio.wait_for(
            r.run_turn(
                ctx.session_id or "s",
                "go",
                is_subtask=ctx.kind == "subtask",
                is_background=ctx.is_background,
                context=ctx,
                tool_filter=tool_filter,
            ),
            10,
        )
    else:
        await _drive(r, path)
    assert len(sent) == 2, sent
    return sent, client.rows


def _records(rows: list[dict[str, Any]]) -> list[dict[str, Any] | None]:
    return [None if row["tool_loading"] is None else json.loads(row["tool_loading"]) for row in rows]


# ---- no request changes ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", PATHS)
async def test_the_payload_is_byte_for_byte_the_same_off_and_in_watch_only(path):
    """Both loops: the setting off, on with the mode Off, and on with Watch only for every kind send the same bytes."""
    off, off_rows = await _turn(path, _settings())
    RuntimeConfig.get().set_tool_loading(AGENT, ToolLoadingConfig(mode="off", kinds=frozenset(KINDS)))
    mode_off, mode_off_rows = await _turn(path, _settings(tool_loading_enabled=True))
    RuntimeConfig.get().set_tool_loading(AGENT, WATCH_ALL)
    watch, watch_rows = await _turn(path, _settings(tool_loading_enabled=True))
    assert off == mode_off == watch
    assert '"tools": [{"name": "bash"' in off[0]  # the tools were sent, in full
    assert _records(off_rows) == _records(mode_off_rows) == [None, None]
    assert all(record is not None for record in _records(watch_rows))


@pytest.mark.parametrize("path", PATHS)
async def test_in_watch_only_each_loop_call_records_what_it_would_leave_out(path):
    RuntimeConfig.get().set_tool_loading(AGENT, WATCH_ALL)
    sent, rows = await _turn(path, _settings(tool_loading_enabled=True))
    records = _records(rows)
    kind = "subtask" if path == "subtask" else "chat"
    for payload, record in zip(sent, records, strict=True):
        tools = json.loads(payload)["tools"]
        assert record == {**tool_loading.call_record(WATCH_ALL, kind, tools), "cache": "read"}
        assert record["sent"] == len(tools) and record["deferred"] == 2  # send_email and dag_create
        assert record["kept"] == ["bash", "db_query", "recall_deep"]


async def test_the_record_is_only_for_a_switched_on_kind():
    RuntimeConfig.get().set_tool_loading(AGENT, ToolLoadingConfig(mode="watch", kinds=frozenset({"subtask"})))
    _sent, chat_rows = await _turn("run_turn", _settings(tool_loading_enabled=True))
    _sent, stream_rows = await _turn("stream_chat", _settings(tool_loading_enabled=True))
    _sent, subtask_rows = await _turn("subtask", _settings(tool_loading_enabled=True))
    assert _records(chat_rows) == _records(stream_rows) == [None, None]
    assert [record["kind"] for record in _records(subtask_rows)] == ["subtask", "subtask"]


async def test_with_the_setting_off_a_stored_watch_is_not_read():
    RuntimeConfig.get().set_tool_loading(AGENT, WATCH_ALL)
    _sent, rows = await _turn("run_turn", _settings(tool_loading_enabled=False))
    assert _records(rows) == [None, None]


async def test_a_turn_of_internal_authority_is_untouched():
    RuntimeConfig.get().set_tool_loading(AGENT, WATCH_ALL)
    internal = ExecutionContext(
        kind="subtask", session_id="subtask-2", subtask_id=uuid.uuid4(), authority=AUTHORITY_INTERNAL
    )
    off, _rows = await _turn("subtask", _settings(), context=internal)
    watch, rows = await _turn("subtask", _settings(tool_loading_enabled=True), context=internal)
    assert off == watch
    assert _records(rows) == [None, None]


async def test_a_turn_with_its_own_tool_list_is_untouched():
    RuntimeConfig.get().set_tool_loading(AGENT, WATCH_ALL)
    check = ExecutionContext(kind="heartbeat_check", session_id="dynamic-check-1")
    off, _rows = await _turn("subtask", _settings(), context=check, tool_filter=["bash", "send_email"])
    watch, rows = await _turn(
        "subtask", _settings(tool_loading_enabled=True), context=check, tool_filter=["bash", "send_email"]
    )
    assert off == watch
    assert _records(rows) == [None, None]
    # the same kind without a list of its own is watched
    _sent, plain = await _turn("subtask", _settings(tool_loading_enabled=True), context=check)
    assert [record["kind"] for record in _records(plain)] == ["heartbeat", "heartbeat"]


@pytest.mark.parametrize("path", PATHS)
async def test_a_fault_in_the_list_builder_leaves_todays_payload(path, monkeypatch, caplog):
    off, _rows = await _turn(path, _settings())

    def explode(_deferred):
        raise RuntimeError("index builder broke")

    monkeypatch.setattr(tool_loading, "index_tokens", explode)
    RuntimeConfig.get().set_tool_loading(AGENT, WATCH_ALL)
    with caplog.at_level(logging.WARNING, logger="nous.api.runner"):
        watch, rows = await _turn(path, _settings(tool_loading_enabled=True))
    assert watch == off
    assert _records(rows) == [None, None]
    faults = [r.getMessage() for r in caplog.records if "F120" in r.getMessage()]
    assert faults == ["F120: tool-loading record not made (RuntimeError); calls go on as before"]  # once, two calls


async def test_a_fault_reading_the_setting_leaves_todays_payload(monkeypatch):
    off, _rows = await _turn("run_turn", _settings())

    def explode(self, agent_id):
        raise RuntimeError("cannot read")

    monkeypatch.setattr(RuntimeConfig, "get_tool_loading", explode)
    watch, rows = await _turn("run_turn", _settings(tool_loading_enabled=True))
    assert watch == off and _records(rows) == [None, None]


async def test_the_calls_outside_the_loop_carry_no_kind():
    """The record rides only on the context the loop passes its own calls: the turn's base context, which compaction,
    the gate and the closing calls copy, carries none."""
    RuntimeConfig.get().set_tool_loading(AGENT, WATCH_ALL)
    client = _client(False, [])
    r = _runner(client, _settings(tool_loading_enabled=True))
    await asyncio.wait_for(r.run_turn("s1", "go"), 10)
    assert [getattr(c, "tool_loading_kind", None) for c in client.contexts] == ["chat", "chat"]
    assert (
        r._call_context(
            ExecutionContext(kind="interactive"),
            purpose="chat",
            session_id="s",
            turn_id=None,
            turn_number=None,
            frame_id=None,
        ).tool_loading_kind
        is None
    )


# ---- a change is seen by the next call ------------------------------------------------------------------------------


class _Session:
    """A stand-in database session for the route's store: records the upsert."""

    def __init__(self, store: list) -> None:
        self.store = store

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, statement, params=None):
        self.store.append(params)

    async def commit(self):
        pass


async def test_a_change_through_the_route_is_seen_by_the_next_call_without_a_restart():
    settings = _settings(
        tool_loading_enabled=True, owner_route_secret_sha256=hashlib.sha256(OWNER_VALUE.encode()).hexdigest()
    )
    stored: list = []
    database = SimpleNamespace(session=lambda: _Session(stored))
    dispatcher = _RecordingDispatcher(list(OFFERED))
    app = Starlette(
        routes=build_tool_loading_routes(
            database=database, settings=settings, dispatcher=dispatcher, guard=OwnerGuard(settings)
        )
    )
    _sent, before = await _turn("run_turn", settings)
    assert _records(before) == [None, None]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://nous") as http:
        answer = await http.put(
            "/dashboard/tool-loading", headers=OWNER, json={"mode": "watch", "kinds": {"chat": True}}
        )
    assert answer.status_code == 200, answer.text
    assert stored and stored[0]["key"] == f"tool_loading:{AGENT}"
    _sent, after = await _turn("run_turn", settings)  # the same process, no restart
    assert [record["kind"] for record in _records(after)] == ["chat", "chat"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://nous") as http:
        await http.put("/dashboard/tool-loading", headers=OWNER, json={"mode": "off"})
    _sent, again = await _turn("run_turn", settings)
    assert _records(again) == [None, None]


# ---- the record in Postgres ---------------------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_the_record_is_written_to_the_calls_row(db):
    agent = f"test-tool-loading-{uuid.uuid4().hex[:12]}"
    writer = BoundedWriter("test")
    sent: list[str] = []
    client = _client(False, sent)
    client.recorder = ModelCallRecorder(writer, db, agent, SimpleNamespace())
    RuntimeConfig.get().set_tool_loading(agent, WATCH_ALL)
    r = _runner(client, _settings(agent, tool_loading_enabled=True))
    try:
        await asyncio.wait_for(r.run_turn("s1", "go"), 10)
        await writer.drain(5.0)
        async with db.engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT tool_loading, tool_names, cache_read FROM nous_system.model_call_log "
                        "WHERE agent_id = :a ORDER BY started_at"
                    ),
                    {"a": agent},
                )
            ).all()
        assert len(rows) == 2
        for row in rows:
            record = row[0] if isinstance(row[0], dict) else json.loads(row[0])
            assert (record["kind"], record["deferred"], record["cache"]) == ("chat", 2, "read")
            assert set(row[1]) - set(record["kept"]) == {"send_email", "dag_create"}
    finally:
        async with db.engine.begin() as conn:
            await conn.execute(text("DELETE FROM nous_system.model_call_log WHERE agent_id = :a"), {"a": agent})
