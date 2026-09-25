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
    def __init__(self, *, fail_open=False, fail_close=False, duplicate=None,
                 duplicate_after_first=False, fail_claim=False):
        self.events: list[tuple] = []
        self.fail_open = fail_open
        self.fail_close = fail_close
        self.failed_id = uuid.uuid4()
        self.output_of: list[str | None] = []
        # harness Phase 2b
        self.keys: list[str | None] = []
        self.closes: list[tuple[str, str | None]] = []
        self.blocked_keys: list[str | None] = []
        self.duplicate = duplicate
        self.duplicate_after_first = duplicate_after_first
        self.fail_claim = fail_claim
        self.claimed: list = []

    async def open_entry(self, *, context, tool_name, tool_input, turn, idempotency_key=None):
        from nous.cognitive.ledger_store import DuplicateSend

        self.events.append(("open", tool_name, context.kind))
        self.keys.append(idempotency_key)
        if self.fail_open:
            raise LedgerWriteError(self.failed_id, RuntimeError("db down"))
        if self.duplicate is not None and idempotency_key is not None and (
                not self.duplicate_after_first or len(self.keys) > 1):
            raise DuplicateSend(self.duplicate)
        return f"id-{tool_name}"

    async def claim_dispatch(self, entry_id):
        self.claimed.append(entry_id)
        return not self.fail_claim

    async def record_blocked(self, *, context, tool_name, tool_input, turn, refused_by,
                             idempotency_key=None):
        self.events.append(("blocked", tool_name, refused_by))
        self.blocked_keys.append(idempotency_key)

    async def close_entry(self, entry_id, *, status, result_summary, output_of=None,
                          external_ref=None, keyed=False):
        self.events.append(("close", entry_id, status))
        self.output_of.append(output_of)
        self.closes.append((status, external_ref))
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
async def test_tool_output_close_names_the_tool_but_a_cancelled_close_does_not():
    """The store shapes a result by the tool that produced it (bash and
    run_python output is never stored), so the runner must say which text
    is tool output -- and a fixed 'outcome unknown' note is not."""
    store = _FakeStore()
    r, _ = _runner(store)
    r._call_api = _one_tool_call_then_done("write_file")
    await _run_loop(r)
    assert store.output_of == ["write_file"]

    store = _FakeStore()
    r, d = _runner(store)
    reached = asyncio.Event()

    async def hanging(name, inp, **kw):
        reached.set()
        await asyncio.sleep(3600)

    d.dispatch = hanging
    r._call_api = _one_tool_call_then_done("write_file")
    task = asyncio.create_task(_run_loop(r, is_background=True))
    await asyncio.wait_for(reached.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.output_of == [None]


@pytest.mark.asyncio
async def test_stream_tool_output_close_names_the_tool():
    store = _FakeStore()

    async def quick(name, inp, **kw):
        return "done", False

    runner = _stream_runner(store, quick)
    runner._call_api_stream = _one_streamed_call()
    [e async for e in runner.stream_chat("s1", "go")]
    assert store.output_of == ["write_file"]


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
    assert store.events == [("blocked", "write_file", "offered_set")]


@pytest.mark.asyncio
async def test_action_gate_block_records_a_code_never_the_gate_model_prose():
    """codex r4 on #645: the gate prompt carries the call's arguments, so the
    gate model's reason can echo a subject, a body or a bare key."""
    from nous.cognitive.action_gate import GateResult
    from nous.cognitive.execution_ledger import ExecutionLedger

    store = _FakeStore()
    r, d = _runner(store, action_gating_mode="enforce")

    class _Gate:
        async def check(self, *a, **k):
            return GateResult(approved=False, reason="echoes sk-ABCDEFGHIJKLMNOP",
                              suggestion="try sk-ABCDEFGHIJKLMNOP")

    r._action_gate = _Gate()
    r._call_api = _one_tool_call_then_done("write_file")
    await _run_loop(r, ledger=ExecutionLedger(session_id="s1"))
    assert d.calls == []
    assert store.events == [("blocked", "write_file", "action_gate")]


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


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["warn", "enforce"])
async def test_an_unregistered_tool_name_leaves_no_row(mode):
    """A name nothing registered cannot have run, and it is model-chosen text:
    no durable row in either mode (codex r5 on #645)."""
    store = _FakeStore()
    r, d = _runner(store, offered=("write_file",), tool_offered_set_enforcement_mode=mode)
    d.registered = {"write_file"}
    r._call_api = _one_tool_call_then_done("sk_hallucinated_tool")
    await _run_loop(r, is_background=True)
    assert [e for e in store.events if e[0] in ("open", "blocked")] == []


def test_dispatcher_reports_registration():
    from nous.api.tools import ToolDispatcher

    d = ToolDispatcher()

    async def h():
        return {"content": []}

    d.register("real_tool", h, {"name": "real_tool", "input_schema": {"type": "object"}})
    assert d.is_registered("real_tool") and not d.is_registered("sk_hallucinated_tool")


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


# ---------------------------------------------------------------------------
# Harness Phase 2b: keyed sends
# ---------------------------------------------------------------------------


def _dag_ctx():
    return ExecutionContext(kind="dag_node", dag_id=uuid.uuid4(), dag_node_name="send")


def _held(status, subject="s"):
    from datetime import UTC, datetime

    from nous.cognitive.ledger_store import HeldKey, _digest

    return HeldKey(uuid.uuid4(), status, "<m1@x>", datetime.now(UTC), datetime.now(UTC),
                   {"subject_sha256": _digest(subject)[0]})


def _email_call(subject="s"):
    from tests.test_runner_authorization import _one_tool_call_then_done_with

    return _one_tool_call_then_done_with("send_email", {"to": "a@x.io", "subject": subject, "body": "b"})


@pytest.mark.asyncio
async def test_a_dag_node_send_opens_with_its_key_and_claims_dispatch():
    store = _FakeStore()
    r, d = _runner(store, offered=("send_email",))
    r._call_api = _email_call()
    await _run_loop(r, is_background=True, context=_dag_ctx())
    assert store.keys[0].startswith("dag:") and store.claimed == ["id-send_email"]
    assert [c[0] for c in d.calls] == ["send_email"]
    assert store.closes[-1][0] == "success"


@pytest.mark.asyncio
async def test_an_unkeyed_write_is_never_claimed():
    store = _FakeStore()
    r, d = _runner(store, offered=("write_file",))
    r._call_api = _one_tool_call_then_done("write_file")
    await _run_loop(r, is_background=True, context=_dag_ctx())
    assert store.keys == [None] and store.claimed == []


@pytest.mark.asyncio
@pytest.mark.parametrize("subject", ["s", "a reworded subject"])
async def test_a_key_held_from_an_earlier_turn_is_already_sent_whatever_the_wording(subject):
    """A retry rewrites the subject; it must never be told how to send again."""
    from nous.cognitive.execution_ledger import ExecutionLedger

    store = _FakeStore(duplicate=_held("success", subject="s"))
    r, d = _runner(store, offered=("send_email",))
    r._call_api = _email_call(subject)
    ledger = ExecutionLedger(session_id="s1")
    _text, results, _usage, _thinking = await _run_loop(
        r, is_background=True, ledger=ledger, context=_dag_ctx())
    assert d.calls == []
    assert ("blocked", "send_email", "duplicate") in store.events
    assert store.blocked_keys[0] and store.blocked_keys[0].startswith("dag:")
    assert [(a.tool_name, a.status) for a in ledger.actions] == [("send_email", "success")]  # 2c stays grounded
    (res,) = results
    assert res.error is None and "Already sent" in res.result and "<m1@x>" in res.result
    assert "send_label" not in res.result


@pytest.mark.asyncio
async def test_a_second_send_in_the_same_turn_asks_for_a_label():
    from nous.cognitive.execution_ledger import ExecutionLedger
    from tests.test_runner_authorization import _two_tool_calls_then_done_with

    store = _FakeStore(duplicate=_held("success"), duplicate_after_first=True)
    r, d = _runner(store, offered=("send_email",))
    r._call_api = _two_tool_calls_then_done_with("send_email", {"to": "a@x.io", "subject": "s", "body": "b"})
    ledger = ExecutionLedger(session_id="s1")
    _text, results, _usage, _thinking = await _run_loop(
        r, is_background=True, ledger=ledger, context=_dag_ctx())
    assert [c[0] for c in d.calls] == ["send_email"]                 # the first went out
    assert ledger.actions[1].status == "blocked"
    assert "send_label" in results[1].error


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending", "unknown"])
async def test_an_in_flight_or_unknown_holder_refuses_the_send(status):
    store = _FakeStore(duplicate=_held(status))
    r, d = _runner(store, offered=("send_email",))
    r._call_api = _email_call()
    _text, results, _usage, _thinking = await _run_loop(r, is_background=True, context=_dag_ctx())
    assert d.calls == [] and status in results[0].error and "send_label" not in results[0].error


@pytest.mark.asyncio
async def test_a_keyed_send_fails_closed_on_a_ledger_outage():
    store = _FakeStore(fail_open=True)
    r, d = _runner(store, offered=("send_email",))
    r._call_api = _email_call()
    _text, results, _usage, _thinking = await _run_loop(r, is_background=True, context=_dag_ctx())
    assert d.calls == [] and ("close", store.failed_id, "error") in store.events
    assert "could not record this send" in results[0].error


@pytest.mark.asyncio
async def test_a_failed_dispatch_claim_refuses_the_send():
    store = _FakeStore(fail_claim=True)
    r, d = _runner(store, offered=("send_email",))
    r._call_api = _email_call()
    await _run_loop(r, is_background=True, context=_dag_ctx())
    assert d.calls == [] and ("close", "id-send_email", "error") in store.events


@pytest.mark.asyncio
async def test_an_unkeyed_call_still_fails_open():
    store = _FakeStore(fail_open=True)
    r, d = _runner(store, offered=("send_email",))
    r._call_api = _email_call()
    await _run_loop(r)  # interactive: unkeyed
    assert [c[0] for c in d.calls] == ["send_email"] and store.keys == [None]


@pytest.mark.asyncio
async def test_the_provider_id_is_recorded_on_success():
    store = _FakeStore()
    r, d = _runner(store, offered=("send_email",))

    async def sends(name, inp, *, outcome=None, **kw):
        d.calls.append((name, kw.get("context"), False))
        outcome.external_ref = "<m@x>"
        return "Email sent", False

    d.dispatch = sends
    r._call_api = _email_call()
    await _run_loop(r, is_background=True, context=_dag_ctx())
    assert store.closes[-1] == ("success", "<m@x>")


@pytest.mark.asyncio
async def test_an_uncertain_send_closes_unknown_with_its_ref():
    store = _FakeStore()
    r, d = _runner(store, offered=("send_email",))

    async def uncertain(name, inp, *, outcome=None, **kw):
        outcome.external_ref, outcome.uncertain = "<m@x>", True
        return "delivery uncertain", True

    d.dispatch = uncertain
    r._call_api = _email_call()
    await _run_loop(r, is_background=True, context=_dag_ctx())
    assert store.closes[-1] == ("unknown", "<m@x>")


@pytest.mark.asyncio
async def test_a_definite_failure_closes_error_and_frees_the_key():
    store = _FakeStore()
    r, d = _runner(store, offered=("send_email",))

    async def refused(name, inp, *, outcome=None, **kw):
        return "email send failed: SMTPDataError", True

    d.dispatch = refused
    r._call_api = _email_call()
    await _run_loop(r, is_background=True, context=_dag_ctx())
    assert store.closes[-1] == ("error", None)


@pytest.mark.asyncio
async def test_a_cancelled_send_keeps_its_ref():
    store = _FakeStore()
    r, d = _runner(store, offered=("send_email",))
    reached = asyncio.Event()

    async def hang(name, inp, *, outcome=None, **kw):
        outcome.external_ref = "<m@x>"
        reached.set()
        await asyncio.sleep(3600)

    d.dispatch = hang
    r._call_api = _email_call()
    task = asyncio.create_task(_run_loop(r, is_background=True, context=_dag_ctx()))
    await asyncio.wait_for(reached.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.closes[-1] == ("unknown", "<m@x>")


@pytest.mark.asyncio
async def test_stream_chat_survives_a_slow_tool_resumed_across_tasks():
    """rest.py resumes stream_chat with create_task(aiter.__anext__())."""
    store = _FakeStore()

    async def slow(name, inp, *, outcome=None, **kw):
        await asyncio.sleep(0.05)
        outcome.external_ref = "<m@x>"
        return "done", False

    runner = _stream_runner(store, slow)  # keepalive_interval=0.01 < 0.05
    runner._settings.tool_timeout = 5
    runner._call_api_stream = _one_streamed_call()
    agen = runner.stream_chat("s1", "go").__aiter__()
    events = []
    while True:
        try:
            events.append(await asyncio.create_task(agen.__anext__()))
        except StopAsyncIteration:
            break
    assert any(getattr(e, "type", None) == "keepalive" for e in events)
    assert store.events[-1][0] == "close" and store.closes[-1] == ("success", "<m@x>")


# ---------------------------------------------------------------------------
# Harness Phase 2b with the REAL LedgerStore (SQLite test DB)
# ---------------------------------------------------------------------------


def _real_runner(db, agent, offered=("send_email",)):
    from nous.cognitive.ledger_store import LedgerStore

    store = LedgerStore(db, agent)
    r = AgentRunner(_MockCognitive(), _MockBrain(), _MockHeart(), _settings())
    d = _RecordingDispatcher(list(offered))
    r.set_dispatcher(d)
    r.set_ledger_store(store)
    return r, d


async def _rows(db, agent):
    from sqlalchemy import select

    from nous.storage.models import ExecutionLedgerEntry

    async with db.session() as s:
        return (await s.execute(
            select(ExecutionLedgerEntry).where(ExecutionLedgerEntry.agent_id == agent)
            .order_by(ExecutionLedgerEntry.created_at)
        )).scalars().all()


@pytest.mark.asyncio
async def test_a_relaunched_node_does_not_send_twice_with_the_real_store(db):
    agent = f"r2b-{uuid.uuid4().hex[:8]}"
    ctx_kwargs = {"kind": "dag_node", "dag_id": uuid.uuid4(), "dag_node_name": "send"}

    first, d1 = _real_runner(db, agent)
    first._call_api = _email_call("Premarket brief")
    await _run_loop(first, is_background=True, context=ExecutionContext(subtask_id=uuid.uuid4(), **ctx_kwargs))

    relaunch, d2 = _real_runner(db, agent)
    relaunch._call_api = _email_call("Premarket brief (retry)")
    _text, results, _usage, _thinking = await _run_loop(
        relaunch, is_background=True, context=ExecutionContext(subtask_id=uuid.uuid4(), **ctx_kwargs))

    assert [c[0] for c in d1.calls] == ["send_email"] and d2.calls == []
    assert "Already sent" in results[0].result
    rows = await _rows(db, agent)
    assert [(r.status, r.dispatched_at is not None) for r in rows] == [("success", True), ("blocked", False)]
    assert rows[0].idempotency_key == rows[1].idempotency_key


@pytest.mark.asyncio
async def test_a_definite_failure_lets_the_relaunch_send_with_the_real_store(db):
    agent = f"r2b-{uuid.uuid4().hex[:8]}"
    ctx_kwargs = {"kind": "dag_node", "dag_id": uuid.uuid4(), "dag_node_name": "send"}

    first, d1 = _real_runner(db, agent)

    async def refused(name, inp, **kw):
        d1.calls.append((name, kw.get("context"), False))
        return "email send failed: SMTPDataError", True

    d1.dispatch = refused
    first._call_api = _email_call()
    await _run_loop(first, is_background=True, context=ExecutionContext(subtask_id=uuid.uuid4(), **ctx_kwargs))

    relaunch, d2 = _real_runner(db, agent)
    relaunch._call_api = _email_call()
    await _run_loop(relaunch, is_background=True, context=ExecutionContext(subtask_id=uuid.uuid4(), **ctx_kwargs))
    assert [c[0] for c in d2.calls] == ["send_email"]
    assert [r.status for r in await _rows(db, agent)] == ["error", "success"]
