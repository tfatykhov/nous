"""Heartbeat model calls write context_log rows.

The heartbeat runs its checks on a fork of the main AgentRunner. The fork shares the main runner's
ContextLogger, so a dynamic check's turn is logged under its ``dynamic-check-<name>-`` session with the
response's usage, and a failing logger never fails the check.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from nous.api.models import ApiResponse
from nous.api.runner import AgentRunner
from nous.api.tools import ToolDispatcher
from nous.heartbeat.dynamic import DynamicCheck
from nous.observability import context_logger as context_logger_mod
from nous.observability.context_logger import HEARTBEAT_CALL_TYPE, ContextLogger
from tests.test_runner_background import _MockBrain, _MockCognitive, _MockHeart, _settings, _SpyApi

_NO_FINDINGS = '{"has_findings": false, "findings": []}'


def _parent() -> AgentRunner:
    runner = AgentRunner(_MockCognitive(), _MockBrain(), _MockHeart(), _settings())
    runner._api = _SpyApi()
    runner._api_shared = True
    runner.set_dispatcher(ToolDispatcher())  # shared with the fork, as in main.py
    return runner


def _fork(parent: AgentRunner) -> AgentRunner:
    api = _SpyApi(
        default_response=ApiResponse(
            content=[{"type": "text", "text": _NO_FINDINGS}],
            stop_reason="end_turn",
            usage={"input_tokens": 120, "output_tokens": 7, "cache_read_input_tokens": 100},
        )
    )
    return parent.fork(api)


def _check(runner: AgentRunner) -> DynamicCheck:
    return DynamicCheck(
        check_id="check-1", name="ledger_watch", prompt="Anything new?", tools=[], interval=3600, timeout=30,
        urgent=False, runner=runner,
    )


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


def test_a_fork_shares_the_context_logger_set_before_or_after_it():
    parent = _parent()
    early = ContextLogger()
    parent.set_context_logger(early)
    assert parent.fork(_SpyApi())._context_logger is early

    fork = parent.fork(_SpyApi())
    later = ContextLogger()
    parent.set_context_logger(later)
    assert fork._context_logger is later


async def test_a_dynamic_check_run_writes_a_context_log_row_with_its_usage():
    inserted, updated = [], []

    async def writer(entry):
        inserted.append(entry)

    async def updater(entry):
        updated.append((entry.id, entry.input_tokens_actual, entry.output_tokens, entry.cache_read_tokens))

    parent = _parent()
    parent.set_context_logger(ContextLogger(db_writer=writer, db_updater=updater))
    result = await _check(_fork(parent)).run()
    await _settle()

    assert result.has_updates is False
    assert len(inserted) == 1
    assert inserted[0].session_id.startswith("dynamic-check-ledger_watch-")
    assert inserted[0].call_type == HEARTBEAT_CALL_TYPE  # kept out of the chat views of the ring
    assert updated == [(inserted[0].id, 120, 7, 100)]


async def test_a_failing_context_logger_does_not_fail_the_check():
    broken = MagicMock()
    broken.log.side_effect = RuntimeError("log down")
    broken.update_response.side_effect = RuntimeError("update down")
    parent = _parent()
    parent.set_context_logger(broken)
    result = await _check(_fork(parent)).run()
    assert result.has_updates is False
    assert result.tokens_used == 127

    # update_response raising after a successful log() does not fail it either
    broken.log.side_effect = None
    broken.log.return_value = MagicMock(id="entry-1")
    result = await _check(_fork(parent)).run()
    assert result.tokens_used == 127
    broken.update_response.assert_called()


async def test_the_response_update_waits_for_the_row_insert():
    order = []
    insert_gate = asyncio.Event()

    async def slow_writer(entry):
        await insert_gate.wait()
        order.append("insert")

    async def updater(entry):
        order.append("update")

    ctx = ContextLogger(db_writer=slow_writer, db_updater=updater)
    entry = ctx.log(
        session_id="s", turn_number=1, call_type="subtask", model="m", system_prompt="p", messages=[],
        tools=None, frame_id="f", context_window=200000,
    )
    ctx.update_response(entry.id, input_tokens=1, output_tokens=1)
    await _settle()
    assert order == []
    insert_gate.set()
    while ctx._pending_tasks:
        await asyncio.gather(*list(ctx._pending_tasks))
    assert order == ["insert", "update"]


async def test_a_failed_insert_does_not_block_the_update():
    updated = AsyncMock()

    async def failing_writer(entry):
        raise RuntimeError("insert failed")

    ctx = ContextLogger(db_writer=failing_writer, db_updater=updated)
    entry = ctx.log(
        session_id="s", turn_number=1, call_type="subtask", model="m", system_prompt="p", messages=[],
        tools=None, frame_id="f", context_window=200000,
    )
    ctx.update_response(entry.id, input_tokens=1)
    await _settle()
    updated.assert_awaited_once()


def test_forks_log_nothing_when_the_fork_switch_is_off():
    settings = _settings().model_copy(update={"context_log_forks_enabled": False})
    runner = AgentRunner(_MockCognitive(), _MockBrain(), _MockHeart(), settings)
    runner.set_context_logger(ContextLogger())
    fork = runner.fork(_SpyApi())
    assert fork._context_logger is None
    runner.set_context_logger(ContextLogger())
    assert fork._context_logger is None


async def test_a_stuck_db_write_is_cut_off_and_frees_its_slot(monkeypatch):
    monkeypatch.setattr(context_logger_mod, "DB_WRITE_TIMEOUT_SECONDS", 0.1)
    written = []

    async def writer(entry):
        if entry.session_id == "stuck":
            await asyncio.sleep(3600)
        written.append(entry.session_id)

    ctx = ContextLogger(db_writer=writer)
    ctx._db_slots = asyncio.Semaphore(1)
    for sid in ("stuck", "next"):
        ctx.log(
            session_id=sid, turn_number=1, call_type="subtask", model="m", system_prompt="p", messages=[],
            tools=None, frame_id="f", context_window=200000,
        )
    await asyncio.wait_for(asyncio.gather(*list(ctx._pending_tasks)), timeout=5)
    assert written == ["next"]


# ---- heartbeat rows stay out of the chat views of the shared ring ----------------------------------------


def _ring() -> ContextLogger:
    """A ring of a chat call, then more heartbeat calls than the views' limits, newest last."""
    ctx = ContextLogger()
    entries = [("chat-1", "chat")] + [(f"dynamic-check-x-{i}", HEARTBEAT_CALL_TYPE) for i in range(12)]
    for sid, call_type in entries:
        entry = ctx.log(
            session_id=sid, turn_number=1, call_type=call_type, model="m", system_prompt="p", messages=[],
            tools=None, frame_id="f", context_window=200000,
        )
        ctx.update_response(entry.id, input_tokens=10, output_tokens=1, cache_read=5, cache_creation=0)
    return ctx


def _client(ctx: ContextLogger):
    from starlette.testclient import TestClient

    from nous.api.rest import create_app
    from nous.config import Settings

    app = create_app(
        runner=MagicMock(), brain=MagicMock(), heart=MagicMock(), cognitive=MagicMock(), database=MagicMock(),
        settings=Settings(_env_file=None), context_logger=ctx,
    )
    return TestClient(app)


def test_get_recent_drops_the_excluded_call_type_before_its_limit():
    ctx = _ring()
    assert [e.session_id for e in ctx.get_recent(limit=10, exclude_call_type=HEARTBEAT_CALL_TYPE)] == ["chat-1"]
    assert len(ctx.get_recent(limit=10)) == 10


def test_the_dashboards_and_the_context_log_list_leave_heartbeat_calls_out():
    client = _client(_ring())
    observability = client.get("/dashboard/observability").json()["context_log"]
    assert [e["session_id"] for e in observability] == ["chat-1"]
    cache = client.get("/dashboard/cache").json()
    assert cache["summary"]["total_calls"] == 1
    assert [e["session_id"] for e in client.get("/context/log").json()["entries"]] == ["chat-1"]
    # one heartbeat session asked for by id is still there
    one = client.get("/context/log", params={"session_id": "dynamic-check-x-3"}).json()["entries"]
    assert [e["call_type"] for e in one] == [HEARTBEAT_CALL_TYPE]
