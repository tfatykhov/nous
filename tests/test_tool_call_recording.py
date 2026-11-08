"""F108: the runner records every tool call the model asks for, refused, gated and malformed ones included.

Each place a loop appends a ``ToolResult`` is an exit with one ``_note_tool_call``. The reason
(NOUS_TOOL_REASON_ENABLED) is split out where the ``tool_use`` block is read, so the ledger, the F026 gate, the
idempotency key, the handler and an F061 extra tool never see it. These tests drive the real loops (``run_turn``,
``stream_chat`` and ``_tool_loop``) over a real ``ToolDispatcher`` with an in-memory spy in place of the recorder.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from unittest.mock import MagicMock

import pytest
from test_runner_authorization import _MockBrain, _MockCognitive, _MockHeart, _run_loop, _settings
from test_runner_ledger import _FakeStore
from test_tool_aliases import HUBS_MESSAGE, RECENT_NOTE

from nous.api.anthropic_client import StreamEvent
from nous.api.execution_context import ExecutionContext
from nous.api.models import ApiResponse
from nous.api.runner import AgentRunner
from nous.api.tools import ToolDispatcher
from nous.cognitive.action_gate import GateResult
from nous.cognitive.execution_ledger import ExecutionLedger
from nous.observability.tool_calls import ToolCallRecord
from nous.tool_names import CHANGED_TOOLS

MALFORMED = "Tool input JSON was malformed and could not be parsed. The tool was not executed."
SUBMIT = {"name": "submit_final_report", "description": "d", "input_schema": {"type": "object"}}
OWN_SUBMIT = {
    "name": "submit_final_report",
    "description": "d",
    "input_schema": {
        "type": "object",
        "properties": {"reason": {"type": "string", "description": "Why the run ended"}},
        "required": ["reason"],
    },
}


class _Spy:
    """Stands in for ``ToolCallRecorder``: keeps every record, or raises on each one."""

    def __init__(self, *, fail: bool = False) -> None:
        self.records: list[ToolCallRecord] = []
        self.fail = fail

    def record(self, rec: ToolCallRecord) -> None:
        if self.fail:
            raise RuntimeError("recorder down")
        self.records.append(rec)

    def record_turn(self, rec) -> None:
        """Turn records are tests/test_turn_record.py's to check; here they are dropped."""


class _Store(_FakeStore):
    """A durable ledger whose rows have UUIDs; keeps the input each row was opened with."""

    def __init__(self) -> None:
        super().__init__()
        self.inputs: list[tuple[str, dict]] = []
        self.ids: list[uuid.UUID] = []

    async def open_entry(self, *, context, tool_name, tool_input, turn, idempotency_key=None):
        await super().open_entry(
            context=context, tool_name=tool_name, tool_input=tool_input, turn=turn, idempotency_key=idempotency_key
        )
        self.inputs.append((tool_name, dict(tool_input)))
        self.ids.append(uuid.uuid4())
        return self.ids[-1]


class _Ledger(ExecutionLedger):
    """The in-memory F026 ledger (what the gate's repeat check reads), keeping the input of each record."""

    def __init__(self, session_id: str) -> None:
        super().__init__(session_id=session_id)
        self.inputs: list[tuple[str, dict]] = []

    def record(self, tool_name, tool_input, result, status):
        self.inputs.append((tool_name, dict(tool_input)))
        return super().record(tool_name, tool_input, result, status)


class _Gate:
    """An F026 gate that keeps what it was asked and blocks only the names in ``block``."""

    def __init__(self, block: frozenset[str] = frozenset()) -> None:
        self.block = block
        self.inputs: list[tuple[str, dict]] = []

    async def check(self, tool_name, tool_input, ledger, user_message=""):
        self.inputs.append((tool_name, dict(tool_input)))
        if tool_name in self.block:
            return GateResult(approved=False, reason="looks like a repeat")
        return GateResult(approved=True, reason="fine")


def _dispatcher(calls: list, *, recall_deep_own_reason: bool = False) -> ToolDispatcher:
    """lookup (runs), boom (raises), recall_deep with the recall_recent alias, and the retired recall_hubs."""

    def handler(name: str, *, fail: bool = False):
        async def run(**kwargs):
            calls.append((name, {k: v for k, v in kwargs.items() if not k.startswith("_")}))
            if fail:
                raise RuntimeError("disk full")
            return {"content": [{"type": "text", "text": f"{name} ran"}]}

        return run

    d = ToolDispatcher()
    d.register("lookup", handler("lookup"), {"type": "object", "properties": {"q": {"type": "string"}}})
    d.register("boom", handler("boom", fail=True), {"type": "object", "properties": {"q": {"type": "string"}}})
    recall_props: dict = {"query": {"type": "string"}, "hours": {"type": "integer"}}
    if recall_deep_own_reason:
        recall_props["reason"] = {"type": "string", "description": "What the recall is for"}
    d.register("recall_deep", handler("recall_deep"), {"type": "object", "properties": recall_props})
    recent, hubs = CHANGED_TOOLS["recall_recent"], CHANGED_TOOLS["recall_hubs"]
    d.register_alias("recall_recent", recent.target, recent.translate, recent.note)
    d.retire("recall_hubs", hubs.note)
    return d


def _runner(*, recorder: _Spy | None = None, store: _Store | None = None, dispatcher_kw=None, **settings):
    """A runner over the real dispatcher, reason flag ON unless overridden, F026 gate off unless a test sets one."""
    overrides = {
        "tool_reason_enabled": True,
        "tool_call_log_enabled": True,
        "tool_offered_set_enforcement_mode": "enforce",
        "action_gating_enabled": False,
        **settings,
    }
    r = AgentRunner(_MockCognitive(), _MockBrain(), _MockHeart(), _settings(**overrides))
    calls: list = []
    r.set_dispatcher(_dispatcher(calls, **(dispatcher_kw or {})))
    if store is not None:
        r.set_ledger_store(store)
    if recorder is not None:
        r.set_tool_call_recorder(recorder)
    r._log_f026_decision = lambda *a, **k: None
    return r, calls


def _block(call_id: str, name: str, tool_input: dict | None) -> dict:
    return {"type": "tool_use", "id": call_id, "name": name, "input": {} if tool_input is None else tool_input}


def _scripted_api(rounds: list[list[tuple[str, str, dict | None]]]):
    """A fake ``_call_api``: one response per round (a None input is malformed JSON), then the text "done"."""
    sent: list[list] = []

    async def fake_call_api(system_prompt, messages, tools=None, **_):
        sent.append(list(messages))
        if len(sent) <= len(rounds):
            calls = rounds[len(sent) - 1]
            errors = {cid: MALFORMED for cid, _n, inp in calls if inp is None}
            return ApiResponse(
                content=[_block(cid, name, inp) for cid, name, inp in calls],
                stop_reason="tool_use",
                input_errors=errors or None,
            )
        return ApiResponse(content=[{"type": "text", "text": "done"}], stop_reason="end_turn")

    return fake_call_api, sent


def _scripted_stream(rounds: list[list[tuple[str, str, dict | None]]]):
    """The same script as a fake ``_call_api_stream``; a None input streams as JSON that does not parse."""
    n = {"calls": 0}

    async def fake_stream(*args, **kwargs):
        n["calls"] += 1
        if n["calls"] <= len(rounds):
            for i, (cid, name, inp) in enumerate(rounds[n["calls"] - 1]):
                yield StreamEvent(type="tool_start", tool_name=name, tool_id=cid, block_index=i)
                yield StreamEvent(
                    type="tool_input_delta", text='{"q": ' if inp is None else json.dumps(inp), block_index=i
                )
                yield StreamEvent(type="block_stop", block_index=i)
            yield StreamEvent(type="done", stop_reason="tool_use")
        else:
            yield StreamEvent(type="text_delta", text="done")
            yield StreamEvent(type="done", stop_reason="end_turn")

    return MagicMock(side_effect=fake_stream)


async def _drive(r: AgentRunner, path: str, rounds, session_id: str = "s1") -> str:
    """One whole turn through ``run_turn`` or ``stream_chat``; returns the reply."""
    try:
        if path == "run_turn":
            r._call_api, _sent = _scripted_api(rounds)
            text, _ctx, _usage = await r.run_turn(session_id, "go")
            return text
        r._call_api_stream = _scripted_stream(rounds)
        events = [e async for e in r.stream_chat(session_id, "go")]
        return "".join(e.text or "" for e in events if e.type == "text_delta")
    finally:
        r._api_shared = True
        await r.close()


PATHS = ["run_turn", "stream_chat"]

# The scenario both paths run: two rounds of calls in one turn.
SCENARIO = [
    [
        ("t1", "lookup", {"q": "deploy date", "reason": "Find the deploy date."}),
        ("t2", "not_offered", {"q": "x", "reason": "Try it"}),
        ("t3", "boom", {"q": "y", "reason": "Write it"}),
    ],
    [
        ("t4", "recall_recent", {"hours": 6, "reason": "Recent work"}),
        ("t5", "recall_hubs", {"reason": "Hubs"}),
        ("t6", "lookup", None),
    ],
]


# ---- one record per exit -------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ran_call_is_recorded_with_reason_and_ledger_link():
    spy, store = _Spy(), _Store()
    r, calls = _runner(recorder=spy, store=store)
    r._call_api, _sent = _scripted_api([[("t1", "lookup", {"q": "deploy date", "reason": "Find the deploy date."})]])
    ctx = ExecutionContext(
        kind="subtask", session_id="s1", parent_session_id="p1", subtask_id=uuid.uuid4(), dag_id=uuid.uuid4()
    )
    await _run_loop(r, is_background=True, context=ctx, turn_number=3)

    assert calls == [("lookup", {"q": "deploy date"})]
    (rec,) = spy.records
    assert rec.outcome == "ran" and rec.is_error is False and rec.error_text is None and rec.refused_by is None
    assert (rec.tool_name, rec.tool_name_called) == ("lookup", "lookup")
    assert rec.reason == "Find the deploy date."
    assert json.loads(rec.input_text) == {"q": "deploy date"}
    assert rec.result_preview == "lookup ran"
    assert rec.ledger_entry_id == store.ids[0]
    assert rec.call_index == 0 and rec.turn_number == 3 and isinstance(rec.turn_id, uuid.UUID)
    assert (rec.work_kind, rec.context_kind) == ("subtask", "subtask")
    assert (rec.session_id, rec.parent_session_id) == ("s1", "p1")
    assert (rec.subtask_id, rec.dag_id, rec.dag_node_id) == (ctx.subtask_id, ctx.dag_id, None)
    assert isinstance(rec.started_at, datetime) and rec.started_at.tzinfo is not None
    assert isinstance(rec.duration_ms, int) and rec.duration_ms >= 0


@pytest.mark.asyncio
async def test_error_call_records_error_text_and_flag():
    spy = _Spy()
    r, _calls = _runner(recorder=spy, store=_Store())
    r._call_api, _sent = _scripted_api([[("t1", "boom", {"q": "y"})]])
    await _run_loop(r)
    (rec,) = spy.records
    assert rec.outcome == "error" and rec.is_error is True
    assert rec.error_text == "Tool error: disk full"
    assert rec.result_preview == ""
    assert rec.ledger_entry_id is not None and rec.refused_by is None


@pytest.mark.asyncio
async def test_refused_call_records_refused_by():
    spy, store = _Spy(), _Store()
    r, calls = _runner(recorder=spy, store=store)
    r._call_api, _sent = _scripted_api([[("t1", "boom", {"q": "y", "reason": "Try it"})]])
    await _run_loop(r, tool_filter=["lookup"])
    assert calls == []
    (rec,) = spy.records
    assert rec.outcome == "refused" and rec.refused_by == "offered_set" and rec.is_error is True
    assert "'boom'" in rec.error_text
    assert rec.reason == "Try it" and json.loads(rec.input_text) == {"q": "y"}
    assert rec.ledger_entry_id is None and rec.duration_ms == 0
    assert store.events == [("blocked", "boom", "offered_set")]


@pytest.mark.asyncio
async def test_gated_call_is_recorded():
    spy = _Spy()
    r, calls = _runner(recorder=spy, store=_Store(), action_gating_mode="enforce")
    r._action_gate = _Gate(block=frozenset({"lookup"}))
    r._call_api, _sent = _scripted_api(
        [[("t1", "boom", {"q": "first"}), ("t2", "lookup", {"q": "x", "reason": "Again"})]]
    )
    await _run_loop(r, ledger=ExecutionLedger(session_id="s1"))
    assert [c[0] for c in calls] == ["boom"]
    first, gated = spy.records
    assert first.ledger_entry_id is not None
    assert gated.outcome == "gated" and gated.refused_by == "action_gate" and gated.is_error is True
    assert gated.error_text.startswith("[BLOCKED by ActionGate]")
    assert gated.reason == "Again"
    # A gated call opened no ledger row: it never inherits the previous call's.
    assert gated.ledger_entry_id is None and gated.duration_ms == 0


@pytest.mark.asyncio
async def test_alias_call_records_both_names():
    spy = _Spy()
    r, calls = _runner(recorder=spy)
    r._call_api, _sent = _scripted_api([[("t1", "recall_recent", {"hours": 6, "reason": "Recent work"})]])
    await _run_loop(r)
    assert calls == [("recall_deep", {"hours": 6})]
    (rec,) = spy.records
    assert (rec.tool_name, rec.tool_name_called) == ("recall_deep", "recall_recent")
    assert rec.outcome == "ran" and rec.reason == "Recent work"
    assert json.loads(rec.input_text) == {"hours": 6}
    assert rec.result_preview.startswith(RECENT_NOTE)


@pytest.mark.asyncio
async def test_alias_of_a_tool_with_its_own_reason_keeps_it():
    """The reason is judged under the alias TARGET: recall_deep declares its own, so it reaches the handler."""
    spy = _Spy()
    r, calls = _runner(recorder=spy, dispatcher_kw={"recall_deep_own_reason": True})
    r._call_api, _sent = _scripted_api([[("t1", "recall_recent", {"hours": 6, "reason": "For the report"})]])
    await _run_loop(r)
    assert calls == [("recall_deep", {"hours": 6, "reason": "For the report"})]
    (rec,) = spy.records
    assert rec.reason == "For the report"


@pytest.mark.asyncio
async def test_retired_name_recorded():
    spy, store = _Spy(), _Store()
    r, calls = _runner(recorder=spy, store=store)
    r._call_api, _sent = _scripted_api([[("t1", "recall_hubs", {"limit": 5, "reason": "Hubs"})]])
    await _run_loop(r)
    assert calls == [] and store.events == []
    (rec,) = spy.records
    assert (rec.tool_name, rec.tool_name_called) == ("recall_hubs", "recall_hubs")
    assert rec.outcome == "retired" and rec.is_error is True and rec.error_text == HUBS_MESSAGE
    assert rec.reason == "Hubs" and rec.ledger_entry_id is None


@pytest.mark.asyncio
async def test_bad_input_recorded():
    spy = _Spy()
    r, calls = _runner(recorder=spy)
    r._call_api, _sent = _scripted_api([[("t1", "lookup", None)]])
    await _run_loop(r)
    assert calls == []
    (rec,) = spy.records
    assert rec.outcome == "bad_input" and rec.is_error is True and rec.error_text == MALFORMED
    assert rec.tool_name == "lookup" and rec.reason is None and rec.duration_ms == 0


# ---- the reason never reaches what runs the call -------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
async def test_reason_is_not_in_ledger_input_or_handler_args(path):
    spy, store, gate = _Spy(), _Store(), _Gate()
    r, calls = _runner(recorder=spy, store=store, action_gating_enabled=True)
    r._action_gate = gate
    r._ledgers["s1"] = ledger = _Ledger("s1")
    emitted = {"q": "deploy date", "reason": "Find the deploy date."}
    rounds = [[("t1", "lookup", dict(emitted))]]
    if path == "run_turn":
        r._call_api, sent = _scripted_api(rounds)
        try:
            text, _ctx, _usage = await r.run_turn("s1", "go")
        finally:
            r._api_shared = True
            await r.close()
        second_call = sent[1]
    else:
        r._call_api_stream = _scripted_stream(rounds)
        events = [e async for e in r.stream_chat("s1", "go")]
        text = "".join(e.text or "" for e in events if e.type == "text_delta")
        second_call = r._call_api_stream.call_args_list[1][0][1]
    assert text == "done"
    assert calls == [("lookup", {"q": "deploy date"})]  # the handler
    assert store.inputs == [("lookup", {"q": "deploy date"})]  # the durable ledger row
    assert gate.inputs == [("lookup", {"q": "deploy date"})]  # the F026 gate
    assert ledger.inputs == [("lookup", {"q": "deploy date"})]  # the in-memory ledger the repeat check reads
    (rec,) = spy.records
    assert rec.reason == "Find the deploy date."
    # The assistant message keeps what the model emitted.
    (use,) = [b for m in second_call if m["role"] == "assistant" for b in m["content"] if b.get("type") == "tool_use"]
    assert use["input"] == emitted


@pytest.mark.asyncio
@pytest.mark.parametrize(("schema", "kept"), [(SUBMIT, False), (OWN_SUBMIT, True)], ids=["added", "own"])
async def test_extra_tool_never_sees_reason(schema, kept):
    spy = _Spy()
    r, _calls = _runner(recorder=spy)
    seen: list[dict] = []

    async def collector(**kwargs):
        seen.append(kwargs)
        return "report accepted", False

    r._call_api, _sent = _scripted_api(
        [[("t1", "submit_final_report", {"summary": "all green", "reason": "Checks passed."})]]
    )
    await _run_loop(r, extra_tools={"submit_final_report": (schema, collector)})
    want = {"summary": "all green", "reason": "Checks passed."} if kept else {"summary": "all green"}
    assert seen == [want]
    (rec,) = spy.records
    assert rec.outcome == "ran" and rec.reason == "Checks passed." and rec.ledger_entry_id is None
    assert rec.result_preview == "report accepted"


# ---- call order within a turn --------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_index_orders_calls_in_turn_including_refused():
    spy = _Spy()
    r, _calls = _runner(recorder=spy)
    r._call_api, _sent = _scripted_api(
        [[("t1", "lookup", {"q": "a"}), ("t2", "boom", {"q": "b"}), ("t3", "recall_hubs", {}), ("t4", "lookup", None)]]
    )
    await _run_loop(r, tool_filter=["lookup"])
    assert [(rec.call_index, rec.outcome) for rec in spy.records] == [
        (0, "ran"),
        (1, "refused"),
        (2, "retired"),
        (3, "bad_input"),
    ]
    assert len({rec.turn_id for rec in spy.records}) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
async def test_call_index_does_not_reset_across_stream_segments(path):
    spy = _Spy()
    r, _calls = _runner(recorder=spy, store=_Store())
    assert await _drive(r, path, SCENARIO) == "done"
    assert [rec.call_index for rec in spy.records] == [0, 1, 2, 3, 4, 5]
    assert len({rec.turn_id for rec in spy.records}) == 1


def _shape(rec: ToolCallRecord) -> tuple:
    """A record minus what differs run to run (ids, times) and the stream's own wording of a malformed input."""
    return (
        rec.call_index,
        rec.work_kind,
        rec.context_kind,
        rec.session_id,
        rec.turn_number,
        rec.tool_name,
        rec.tool_name_called,
        rec.outcome,
        rec.reason,
        rec.input_text,
        rec.result_preview,
        rec.is_error,
        None if rec.outcome == "bad_input" else rec.error_text,
        rec.refused_by,
        rec.ledger_entry_id is not None,
    )


@pytest.mark.asyncio
async def test_stream_and_run_turn_paths_both_record():
    shapes = {}
    for path in PATHS:
        spy = _Spy()
        r, _calls = _runner(recorder=spy, store=_Store())
        assert await _drive(r, path, SCENARIO) == "done"
        shapes[path] = [_shape(rec) for rec in spy.records]
    assert shapes["run_turn"] == shapes["stream_chat"]
    by_index = {s[0]: s for s in shapes["run_turn"]}
    assert [s[7] for s in shapes["run_turn"]] == ["ran", "refused", "error", "ran", "retired", "bad_input"]
    assert by_index[0][8] == "Find the deploy date." and by_index[0][14] is True
    assert by_index[1][13] == "offered_set" and by_index[1][14] is False
    assert (by_index[3][5], by_index[3][6]) == ("recall_deep", "recall_recent")
    assert all(s[2] == "interactive" and s[1] == "conversation" and s[4] == 1 for s in shapes["run_turn"])


@pytest.mark.asyncio
async def test_two_turns_with_same_turn_number_get_distinct_turn_ids():
    spy = _Spy()
    r, _calls = _runner(recorder=spy)
    r._call_api, _sent = _scripted_api([[("t1", "lookup", {"q": "a"})]])
    await r.run_turn("s1", "go")
    r._conversations["s1"].messages.clear()  # compaction: the next turn's number repeats
    r._call_api, _sent = _scripted_api([[("t1", "lookup", {"q": "b"})]])
    await r.run_turn("s1", "go")
    first, second = spy.records
    assert first.turn_number == second.turn_number == 1
    assert first.turn_id != second.turn_id
    assert first.call_index == second.call_index == 0


# ---- the approved-action path ---------------------------------------------------------------------------------


def _approved(tool: str) -> ExecutionContext:
    proposal_id = uuid.uuid4()
    return ExecutionContext(
        kind="approved_action",
        session_id=f"proposal-{proposal_id}",
        proposal_id=proposal_id,
        declared_tools=(tool,),
        root_intention_id=uuid.uuid4(),
        intention_id=uuid.uuid4(),
    )


@pytest.mark.asyncio
async def test_execute_single_call_records():
    spy, store = _Spy(), _Store()
    r, calls = _runner(recorder=spy, store=store)
    ctx = _approved("lookup")
    out = await r.execute_single_call(ctx, "lookup", {"q": "x"})
    assert (out.text, out.is_error) == ("lookup ran", False)
    alias = await r.execute_single_call(_approved("recall_recent"), "recall_recent", {"hours": 2})
    assert alias.is_error is False
    refused = await r.execute_single_call(_approved("lookup"), "boom", {"q": "y"})
    assert refused.is_error is True
    retired = await r.execute_single_call(_approved("recall_hubs"), "recall_hubs", {})
    assert (retired.text, retired.is_error) == (HUBS_MESSAGE, True)
    assert [c[0] for c in calls] == ["lookup", "recall_deep"]

    ran, aliased, refused_rec, retired_rec = spy.records
    assert ran.outcome == "ran" and ran.result_preview == "lookup ran" and ran.ledger_entry_id == store.ids[0]
    assert (ran.context_kind, ran.work_kind, ran.session_id) == ("approved_action", "intention", ctx.session_id)
    assert ran.turn_number is None and ran.call_index == 0 and ran.reason is None
    assert (aliased.tool_name, aliased.tool_name_called, aliased.outcome) == ("recall_deep", "recall_recent", "ran")
    assert refused_rec.outcome == "refused" and refused_rec.refused_by and refused_rec.tool_name == "boom"
    assert retired_rec.outcome == "retired" and retired_rec.error_text == HUBS_MESSAGE


@pytest.mark.asyncio
async def test_execute_single_call_gets_fresh_turn_id():
    spy = _Spy()
    r, _calls = _runner(recorder=spy)
    await r.execute_single_call(_approved("lookup"), "lookup", {"q": "x"})
    await r.execute_single_call(_approved("lookup"), "lookup", {"q": "x"})
    first, second = spy.records
    assert isinstance(first.turn_id, uuid.UUID) and first.turn_id != second.turn_id
    assert first.turn_number is None and second.turn_number is None


# ---- recording never changes a turn ---------------------------------------------------------------------------


def _summary(results) -> list[tuple]:
    return [(t.tool_name, t.arguments, t.result, t.error) for t in results]


@pytest.mark.asyncio
async def test_recorder_failure_does_not_change_the_turn_result(caplog):
    rounds = [[("t1", "lookup", {"q": "a", "reason": "Why"}), ("t2", "boom", {"q": "b"}), ("t3", "recall_hubs", {})]]
    outcomes = []
    for recorder in (None, _Spy(fail=True)):
        r, calls = _runner(recorder=recorder)
        r._call_api, _sent = _scripted_api(rounds)
        with caplog.at_level(logging.WARNING, logger="nous.api.runner"):
            text, results, _usage, _thinking = await _run_loop(r, tool_filter=["lookup", "boom"])
        outcomes.append((text, _summary(results), calls))
    assert outcomes[0] == outcomes[1]
    assert outcomes[1][0] == "done"
    assert sum("F108" in rec.getMessage() for rec in caplog.records if rec.levelno == logging.WARNING) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
async def test_flag_off_records_nothing_and_changes_nothing(path):
    store = _Store()
    r, calls = _runner(store=store, tool_reason_enabled=False, tool_call_log_enabled=False)

    def must_not_run(*a, **k):
        raise AssertionError("_note_tool_call ran with no recorder")

    r._note_tool_call = must_not_run
    emitted = {"q": "deploy date", "reason": "Find the deploy date."}
    assert await _drive(r, path, [[("t1", "lookup", dict(emitted))]]) == "done"
    # No split: the durable row keeps what the model sent; the dispatcher's own repair still drops the stray reason.
    assert store.inputs == [("lookup", emitted)]
    assert calls == [("lookup", {"q": "deploy date"})]


@pytest.mark.asyncio
async def test_reason_flag_off_with_a_recorder_records_no_reason():
    spy = _Spy()
    r, _calls = _runner(recorder=spy, tool_reason_enabled=False)
    r._call_api, _sent = _scripted_api([[("t1", "lookup", {"q": "x", "reason": "Why"})]])
    _text, results, _u, _t = await _run_loop(r)
    (rec,) = spy.records
    assert rec.reason is None
    assert json.loads(rec.input_text) == {"q": "x", "reason": "Why"}
    assert results[0].arguments == {"q": "x", "reason": "Why"}


@pytest.mark.asyncio
async def test_forks_get_the_recorder():
    spy = _Spy()
    r, _calls = _runner()
    early = r.fork(MagicMock())
    r.set_tool_call_recorder(spy)
    late = r.fork(MagicMock())
    assert early._tool_call_recorder is spy and late._tool_call_recorder is spy
    r.set_tool_call_recorder(None)
    assert early._tool_call_recorder is None and late._tool_call_recorder is None
