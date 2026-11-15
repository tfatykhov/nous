"""Goals PR 0c: the halt in both model loops, the dispatcher, the hardened executor, the chat line and the prompt."""

from __future__ import annotations

import asyncio
import json
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from f099_support import env_factory, runner_env, say, use  # noqa: F401
from sqlalchemy import select
from test_f061_subtask_executor import _make_heart_mock, _make_settings, _make_subtask, _scripted_runner
from test_runner_authorization import _run_loop, _runner

from nous import background_halt
from nous.api.anthropic_client import StreamEvent
from nous.api.execution_context import ExecutionContext
from nous.api.models import ApiResponse
from nous.api.tools import ToolDispatcher
from nous.background_halt import BackgroundHalt
from nous.cognitive.ledger_store import REFUSAL_CODES
from nous.handlers.subtask_executor import HardenedRunState, execute_hardened
from nous.storage.models import Subtask


def _install() -> BackgroundHalt:
    halt = BackgroundHalt(host_hold=False)
    background_halt.install(halt)
    return halt


def _two_calls_then(text: str = "done"):
    """The first response asks for two tool calls (A then B); every later one would answer with text."""
    calls = {"n": 0}

    async def fake_call_api(system_prompt, messages, tools=None, **_):
        calls["n"] += 1
        if calls["n"] == 1:
            return ApiResponse(
                content=[
                    {"type": "tool_use", "id": "a", "name": "recall_deep", "input": {"q": "a"}},
                    {"type": "tool_use", "id": "b", "name": "recall_deep", "input": {"q": "b"}},
                ],
                stop_reason="tool_use",
            )
        return ApiResponse(content=[{"type": "text", "text": text}], stop_reason="end_turn")

    return fake_call_api, calls


class _Blocked:
    """A ledger store that keeps each refusal code, and rejects a code the ledger does not know."""

    def __init__(self) -> None:
        self.codes: list[str] = []

    def __getattr__(self, name):  # every other ledger write: a no-op
        return AsyncMock(return_value=None)

    async def record_blocked(self, **kwargs):
        if kwargs["refused_by"] not in REFUSAL_CODES:
            raise ValueError(kwargs["refused_by"])
        self.codes.append(kwargs["refused_by"])


# ---- the real _tool_loop -------------------------------------------------------------------------------------


async def test_halt_between_two_tool_calls_refuses_the_second_and_makes_no_more_model_calls():
    halt = _install()
    r, d = _runner(["recall_deep"])
    blocked = _Blocked()
    r.set_ledger_store(blocked)
    model, calls = _two_calls_then()
    r._call_api = model
    original = d.dispatch

    async def dispatch_then_halt(name, inp, **kwargs):
        out = await original(name, inp, **kwargs)
        await halt.halt(actor="t", surface="dashboard")  # lands while the turn is between its two calls
        return out

    d.dispatch = dispatch_then_halt
    ctx = ExecutionContext(kind="subtask", session_id="s1")
    text, results, _usage, _thinking = await _run_loop(r, is_background=True, context=ctx)
    assert [c[0] for c in d.calls] == ["recall_deep"]  # B never reached the dispatcher
    assert calls["n"] == 1  # no model call after the halt
    assert text == background_halt.HALT_TURN_TEXT
    assert results[1].error == background_halt.HALTED_TOOL_TEXT
    assert blocked.codes == ["background_halt"]


async def test_a_turn_that_started_before_a_halt_is_refused_after_an_immediate_resume():
    halt = _install()
    r, d = _runner(["recall_deep"])
    model, calls = _two_calls_then()
    r._call_api = model
    original = d.dispatch

    async def dispatch_then_halt_and_resume(name, inp, **kwargs):
        out = await original(name, inp, **kwargs)
        await halt.halt(actor="t", surface="dashboard")
        assert (await halt.resume(actor="t", via="dashboard")).changed
        return out

    d.dispatch = dispatch_then_halt_and_resume
    ctx = ExecutionContext(kind="dag_node", session_id="s1")
    text, results, _usage, _thinking = await _run_loop(r, is_background=True, context=ctx)
    assert not halt.halted
    assert [c[0] for c in d.calls] == ["recall_deep"] and calls["n"] == 1
    assert results[1].error == background_halt.HALTED_TOOL_TEXT and text == background_halt.HALT_TURN_TEXT

    fresh, fresh_d = _runner(["recall_deep"])  # a turn that starts after the resume runs
    fresh._call_api, fresh_calls = _two_calls_then("all done")
    text, _results, _usage, _thinking = await _run_loop(fresh, is_background=True, context=ctx)
    assert [c[0] for c in fresh_d.calls] == ["recall_deep", "recall_deep"] and text == "all done"


async def test_the_worker_epoch_outlives_a_halt_and_resume_before_the_turn_started():
    """The epoch captured at the claim (here set by hand, as the worker does at dequeue) wins over the one the loop
    would capture itself: a turn claimed before a halt and a resume does not run."""
    halt = _install()
    epoch = halt.epoch
    await halt.halt(actor="t", surface="dashboard")
    await halt.resume(actor="t", via="dashboard")
    r, d = _runner(["recall_deep"])
    r._call_api, calls = _two_calls_then()
    with background_halt.epoch_scope(epoch):
        text, *_ = await _run_loop(r, is_background=True, context=ExecutionContext(kind="subtask", session_id="s1"))
    assert d.calls == [] and calls["n"] == 0 and text == background_halt.HALT_TURN_TEXT


async def test_with_no_halt_the_loop_runs_exactly_as_before():
    r, d = _runner(["recall_deep"])
    r._call_api, calls = _two_calls_then("finished")
    text, results, *_ = await _run_loop(r, is_background=True, context=ExecutionContext(kind="subtask"))
    assert text == "finished" and calls["n"] == 2 and len(d.calls) == 2
    assert all(res.error is None for res in results)


async def test_the_tool_limit_call_and_the_max_turns_call_are_not_made_while_halted():
    halt = _install()
    r, d = _runner(["recall_deep"])
    model, calls = _two_calls_then()
    r._call_api = model
    original = d.dispatch

    async def dispatch_then_halt(name, inp, **kwargs):
        out = await original(name, inp, **kwargs)
        if not halt.halted:
            await halt.halt(actor="t", surface="dashboard")
        return out

    d.dispatch = dispatch_then_halt
    text, *_ = await _run_loop(
        r, is_background=True, max_tool_calls=1, context=ExecutionContext(kind="subtask", session_id="s1")
    )
    assert calls["n"] == 1 and text == background_halt.HALT_TURN_TEXT


# ---- the authorize choke point ---------------------------------------------------------------------------------


async def test_every_background_kind_is_refused_and_chat_is_not():
    halt = _install()
    await halt.halt(actor="t", surface="dashboard")
    r, _d = _runner(["web_search"])

    def code(ctx, session):
        refusal = r._authorize_tool_call(ctx, "web_search", frozenset({"web_search"}), session, {})
        return None if refusal is None else refusal.code

    assert code(ExecutionContext(kind="interactive", session_id="s1"), "s1") is None
    assert code(ExecutionContext(kind="interactive", session_id="claude-job"), "claude-job") == "background_halt"
    assert code(ExecutionContext(kind="mcp", session_id="m1"), "m1") is None
    approved = ExecutionContext(
        kind="approved_action", session_id="proposal-x", proposal_id=uuid.uuid4(), declared_tools=("web_search",)
    )
    assert code(approved, approved.session_id) is None  # the single-call path's arguments
    for kind in ("subtask", "dag_node", "scheduled", "agent_action", "heartbeat_triage", "heartbeat_check"):
        assert code(ExecutionContext(kind=kind, session_id="s1"), "s1") == "background_halt", kind
    for kind in ("heartbeat_callback", "dag_summary", "background"):
        assert code(ExecutionContext(kind=kind, session_id="s1"), "s1") == "background_halt", kind


def test_the_refusal_code_is_a_ledger_code():
    assert "background_halt" in REFUSAL_CODES and background_halt.REFUSAL_CODE == "background_halt"


# ---- stream_chat, pumped as rest.py pumps it --------------------------------------------------------------------


async def _pump(gen) -> list:
    """One task per ``__anext__``, as rest.py's keepalive race does: each resume runs in a fresh context."""
    events = []
    while True:
        try:
            events.append(await asyncio.ensure_future(gen.__anext__()))
        except StopAsyncIteration:
            return events


def _stream_runner():
    from test_streaming import _make_mock_cognitive, _make_mock_settings, _make_runner

    cognitive, turn_context = _make_mock_cognitive()
    turn_context.refuse_active = False
    settings = _make_mock_settings()
    settings.tool_offered_set_enforcement_mode = "off"
    settings.tool_context_policy_mode = "off"
    settings.f099_lineage_shell = False
    return _make_runner(cognitive, settings)


def _two_streamed_calls_then_text(calls: dict):
    async def fake_stream(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            for index, query in ((1, "a"), (2, "b")):
                yield StreamEvent(type="tool_start", tool_name="web_search", tool_id=f"t{index}", block_index=index)
                yield StreamEvent(type="tool_input_delta", text=json.dumps({"query": query}), block_index=index)
                yield StreamEvent(type="block_stop", block_index=index)
            yield StreamEvent(type="done", stop_reason="tool_use")
        else:
            yield StreamEvent(type="text_delta", text="more")
            yield StreamEvent(type="done", stop_reason="end_turn")

    return fake_stream


async def test_halt_during_a_real_stream_chat_loop():
    halt = _install()
    runner = _stream_runner()
    blocked = _Blocked()
    runner.set_ledger_store(blocked)
    runner._dispatcher.is_registered = MagicMock(return_value=True)

    async def dispatch_then_halt(*args, **kwargs):
        await halt.halt(actor="t", surface="dashboard")
        return "40 cm", False

    runner._dispatcher.dispatch = AsyncMock(side_effect=dispatch_then_halt)
    calls = {"n": 0}
    runner._call_api_stream = MagicMock(side_effect=_two_streamed_calls_then_text(calls))
    runner._call_api = AsyncMock(side_effect=AssertionError("no model call after the halt"))
    events = await _pump(runner.stream_chat("claude-job-1", "the job finished"))
    assert calls["n"] == 1 and runner._dispatcher.dispatch.await_count == 1
    texts = [e.text for e in events if e.type == "text_delta"]
    assert background_halt.HALT_TURN_TEXT in texts
    assert background_halt.HALT_LINE not in "".join(texts)  # a job's session gets no chat line
    assert blocked.codes == ["background_halt"]
    assert events[-1].type == "done"


async def test_interactive_stream_while_halted_carries_the_line_and_it_is_not_stored():
    halt = _install()
    await halt.halt(actor="t", surface="dashboard")
    runner = _stream_runner()

    async def text_only(*args, **kwargs):
        yield StreamEvent(type="text_delta", text="Here is the answer.")
        yield StreamEvent(type="done", stop_reason="end_turn")

    runner._call_api_stream = MagicMock(side_effect=text_only)
    events = await _pump(runner.stream_chat("s1", "hi"))
    texts = "".join(e.text for e in events if e.type == "text_delta")
    assert texts.startswith("Here is the answer.") and texts.endswith(background_halt.HALT_LINE)
    stored = runner._conversations["s1"].messages[-1].content
    assert stored == "Here is the answer."


async def test_interactive_reply_carries_the_halt_line_in_run_turn_and_it_is_not_stored(runner_env):  # noqa: F811
    env = await runner_env([say("Snow tomorrow.")], [say("Still snowing.")])
    plain, _ctx, _usage = await env.runner.run_turn("s1", "weather?")
    assert plain == "Snow tomorrow."  # not halted: unchanged
    halt = _install()
    await halt.halt(actor="t", surface="dashboard")
    shown, _ctx, _usage = await env.runner.run_turn("s1", "and then?")
    assert shown == "Still snowing.\n\n" + background_halt.HALT_LINE
    stored = [m.content for m in env.runner._conversations["s1"].messages if m.role == "assistant"]
    assert stored == ["Snow tomorrow.", "Still snowing."]
    assert background_halt.PROMPT_NOTE in str(env.model.calls[-1]["system_prompt"])
    assert background_halt.PROMPT_NOTE not in str(env.model.calls[0]["system_prompt"])


# ---- the system prompt note ------------------------------------------------------------------------------------


def _turn_context(**over):
    tc = MagicMock()
    tc.system_prompt = "base"
    tc.diagnostic_nudges = ""
    tc.sections_by_tier = over.get("sections_by_tier")
    tc.frame.frame_id = "conversation"
    return tc


async def test_system_prompt_note_only_while_halted():
    flat, _ = _runner([])
    tiered, _ = _runner([], cache_split_system_prompt=True)
    tiers = {"stable": "s", "dynamic": "d"}
    assert background_halt.PROMPT_NOTE not in flat._build_system_prompt(_turn_context())
    before = tiered._build_system_prompt(_turn_context(sections_by_tier=tiers))
    assert background_halt.PROMPT_NOTE not in before["dynamic"]
    halt = _install()
    await halt.halt(actor="t", surface="dashboard")
    assert flat._build_system_prompt(_turn_context()).endswith(background_halt.PROMPT_NOTE)
    after = tiered._build_system_prompt(_turn_context(sections_by_tier=tiers))
    assert after["dynamic"].endswith(background_halt.PROMPT_NOTE) and after["stable"] == "s"
    for r in (flat, tiered):
        r._api_shared = True
        await r.close()


# ---- the dispatcher: nothing new starts from chat ----------------------------------------------------------------


async def test_chat_spawn_tools_and_retry_node_are_refused_with_the_fixed_text_and_schedule_task_is_allowed():
    dispatcher = ToolDispatcher()
    ran: list[str] = []

    def handler(name):
        async def run(**kwargs):
            ran.append(name)
            return {"content": [{"type": "text", "text": f"{name} ok"}]}

        return run

    schema = {"type": "object", "properties": {"action": {"type": "string"}}}
    for name in ("spawn_task", "spawn_sync", "dag_create", "dag_manage", "schedule_task"):
        dispatcher.register(name, handler(name), schema)
    chat = ExecutionContext(kind="interactive", session_id="s1")
    assert (await dispatcher.dispatch("spawn_task", {}, context=chat))[1] is False  # not halted: runs
    ran.clear()
    halt = _install()
    await halt.halt(actor="t", surface="dashboard")
    for name, args in (
        ("spawn_task", {}),
        ("spawn_sync", {}),
        ("dag_create", {}),
        ("dag_manage", {"action": "retry_node"}),
    ):
        assert await dispatcher.dispatch(name, args, context=chat) == (background_halt.HALTED_SPAWN_TEXT, True)
    assert ran == []
    assert (await dispatcher.dispatch("schedule_task", {}, context=chat))[1] is False
    assert (await dispatcher.dispatch("dag_manage", {"action": "list"}, context=chat))[1] is False
    assert ran == ["schedule_task", "dag_manage"]


# ---- the hardened executor ------------------------------------------------------------------------------------


GOOD_REPORT = {
    "summary": "Found 3 candidates matching the search criteria. Each was reviewed for fitness.",
    "confidence": 0.9,
    "findings": ["a", "b", "c"],
}


async def test_the_hardened_executor_does_not_retry_a_halted_run():
    halt = _install()
    runner = _scripted_runner(scripted_payloads=[None, None])
    inner = runner.run_turn.side_effect

    async def run_then_halt(**kwargs):
        out = await inner(**kwargs)
        await halt.halt(actor="t", surface="dashboard")
        return out

    runner.run_turn.side_effect = run_then_halt
    heart = _make_heart_mock()
    heart.subtasks.cancel = AsyncMock(return_value=True)
    emitted, notified = AsyncMock(), AsyncMock()
    state = HardenedRunState()
    _text, result = await execute_hardened(
        _make_subtask(),
        "subtask-1",
        runner=runner,
        heart=heart,
        settings=_make_settings(max_attempts=2),
        emit_event=emitted,
        notify_telegram=notified,
        state=state,
    )
    assert runner.run_turn.await_count == 1
    assert result.outcome == "incomplete_blocked" and result.reason == background_halt.HALT_REASON
    assert state.halted
    heart.subtasks.cancel.assert_awaited_once()
    heart.subtasks.complete.assert_not_awaited()
    heart.subtasks.fail.assert_not_awaited()
    emitted.assert_not_awaited()
    notified.assert_not_awaited()


async def test_a_report_that_returns_after_the_halt_is_not_accepted():
    """The model call returns a valid report between the flag and the sweep's UPDATE: no completed row."""
    halt = _install()
    runner = _scripted_runner(scripted_payloads=[GOOD_REPORT])
    inner = runner.run_turn.side_effect

    async def halt_then_return(**kwargs):
        await halt.halt(actor="t", surface="dashboard")
        return await inner(**kwargs)

    runner.run_turn.side_effect = halt_then_return
    heart = _make_heart_mock()
    heart.subtasks.cancel = AsyncMock(return_value=True)
    _text, result = await execute_hardened(
        _make_subtask(), "subtask-1", runner=runner, heart=heart, settings=_make_settings()
    )
    assert not result.ok and result.outcome == "incomplete_blocked"
    heart.subtasks.complete.assert_not_awaited()
    heart.subtasks.cancel.assert_awaited_once()


async def test_the_hardened_executor_is_unchanged_with_no_halt():
    runner = _scripted_runner(scripted_payloads=[GOOD_REPORT])
    heart = _make_heart_mock()
    heart.subtasks.cancel = AsyncMock()
    state = HardenedRunState()
    _text, result = await execute_hardened(
        _make_subtask(), "subtask-1", runner=runner, heart=heart, settings=_make_settings(), state=state
    )
    assert result.ok and not state.halted
    heart.subtasks.complete.assert_awaited_once()
    heart.subtasks.cancel.assert_not_awaited()


# ---- the epoch is taken at the turn's entry ---------------------------------------------------------------------


async def test_a_halt_and_a_resume_inside_the_pre_turn_work_leave_a_run_turn_refused(runner_env):  # noqa: F811
    env = await runner_env()  # no model step: a model call would fail the test
    halt = _install()
    original = env.cognitive.pre_turn

    async def pre_turn_spanning_a_halt(*args, **kwargs):
        await halt.halt(actor="t", surface="dashboard")
        assert (await halt.resume(actor="t", via="dashboard")).changed
        return await original(*args, **kwargs)

    env.cognitive.pre_turn = pre_turn_spanning_a_halt
    ctx = ExecutionContext(kind="heartbeat_triage", session_id="heartbeat-1")
    text, _ctx, _usage = await env.runner.run_turn("heartbeat-1", "look", is_background=True, context=ctx)
    assert not halt.halted and text == background_halt.HALT_TURN_TEXT and env.model.calls == []


async def test_a_halt_and_a_resume_inside_the_pre_turn_work_leave_a_background_session_stream_refused():
    halt = _install()
    runner = _stream_runner()
    turn_context = runner._cognitive.pre_turn.return_value

    async def pre_turn_spanning_a_halt(*args, **kwargs):
        await halt.halt(actor="t", surface="dashboard")
        assert (await halt.resume(actor="t", via="dashboard")).changed
        return turn_context

    runner._cognitive.pre_turn = AsyncMock(side_effect=pre_turn_spanning_a_halt)
    runner._call_api_stream = MagicMock(side_effect=AssertionError("no model call"))
    events = await _pump(runner.stream_chat("claude-job-2", "the job finished"))
    texts = [e.text for e in events if e.type == "text_delta"]
    assert texts == [background_halt.HALT_TURN_TEXT] and not runner._call_api_stream.called


async def test_a_stream_turn_spanning_a_halt_and_a_resume_refuses_its_next_call():
    halt = _install()
    runner = _stream_runner()
    blocked = _Blocked()
    runner.set_ledger_store(blocked)
    runner._dispatcher.is_registered = MagicMock(return_value=True)

    async def dispatch_then_halt_and_resume(*args, **kwargs):
        await halt.halt(actor="t", surface="dashboard")
        assert (await halt.resume(actor="t", via="dashboard")).changed
        return "40 cm", False

    runner._dispatcher.dispatch = AsyncMock(side_effect=dispatch_then_halt_and_resume)
    calls = {"n": 0}
    runner._call_api_stream = MagicMock(side_effect=_two_streamed_calls_then_text(calls))
    events = await _pump(runner.stream_chat("claude-job-3", "the job finished"))
    assert not halt.halted and calls["n"] == 1 and runner._dispatcher.dispatch.await_count == 1
    assert blocked.codes == ["background_halt"]  # refused by the epoch alone: the flag is off again
    assert background_halt.HALT_TURN_TEXT in [e.text for e in events if e.type == "text_delta"]


# ---- a chat turn hands no epoch to the work it starts --------------------------------------------------------------


async def test_a_chat_turn_spanning_a_halt_and_a_resume_can_still_run_spawn_sync(runner_env):  # noqa: F811
    halt = _install()

    async def the_chat_turn_spans_a_halt(kwargs):
        await halt.halt(actor="t", surface="dashboard")
        assert (await halt.resume(actor="t", via="dashboard")).changed
        return [use("spawn_sync", task="Check the snow report", intent="Tell the user about the snow")]

    env = await runner_env(
        the_chat_turn_spans_a_halt,
        [use("submit_final_report", **GOOD_REPORT)],  # the inline run's own turn
        [say("It snowed 40 cm.")],
        subtask_hardening_enabled=True,
        subtask_payload_schema_enabled=True,  # spawn_sync is registered with both
    )
    text, _ctx, _usage = await env.runner.run_turn("s1", "how much snow?")
    assert text == "It snowed 40 cm." and not halt.halted
    async with env.db.session() as s:
        (row,) = (await s.execute(select(Subtask).where(Subtask.agent_id == env.agent))).scalars().all()
    assert row.status == "completed" and row.final_outcome == "completed"


async def test_a_background_turns_inline_run_keeps_its_epoch_after_a_halt_and_a_resume(runner_env):  # noqa: F811
    halt = _install()
    env = await runner_env(subtask_hardening_enabled=False)  # no model step: the inline run makes no model call
    epoch = halt.epoch
    await halt.halt(actor="t", surface="dashboard")
    assert (await halt.resume(actor="t", via="dashboard")).changed
    background = ExecutionContext(kind="background", session_id="heartbeat-1")
    with background_halt.epoch_scope(epoch):  # the background turn that spawns started before the halt
        text, is_error = await env.dispatcher.dispatch(
            "spawn_task",
            {"task": "Check the snow report", "await_result": True, "intent": "Tell the user about the snow"},
            session_id="heartbeat-1",
            context=background,
        )
    assert is_error and background_halt.HALT_REASON in text and env.model.calls == []


# ---- the halt is checked again at the dispatch, after every await of the pre-dispatch work -------------------------


AWAIT_POINTS = ["action gate", "ledger row", "compensation snapshot"]


def _halt_lands_at(runner, halt, point: str, entry_id) -> AsyncMock:
    """Make the halt land while the runner awaits ``point``; returns the stand-in of ``_ledger_close``."""
    from types import SimpleNamespace

    async def gate_check(*args, **kwargs):
        await halt.halt(actor="t", surface="dashboard")
        return SimpleNamespace(approved=True, reason="fine", suggestion=None)

    async def open_row(*args, **kwargs):
        if point == "ledger row":
            await halt.halt(actor="t", surface="dashboard")
        return entry_id, None, None

    async def snapshot(*args, **kwargs):
        if point == "compensation snapshot":
            await halt.halt(actor="t", surface="dashboard")
        return False

    if point == "action gate":
        runner._action_gate = SimpleNamespace(check=gate_check)
    runner._open_for_call = open_row
    runner._capture_compensation_snapshot = snapshot
    closed = AsyncMock()
    runner._ledger_close = closed
    return closed


@pytest.mark.parametrize("point", AWAIT_POINTS)
async def test_a_halt_landing_during_the_pre_dispatch_work_refuses_the_call_in_the_tool_loop(point):
    from nous.cognitive.execution_ledger import ExecutionLedger

    halt = _install()
    r, d = _runner(["send_email"])
    entry_id = uuid.uuid4()
    closed = _halt_lands_at(r, halt, point, entry_id)

    async def one_send_then_text(system_prompt, messages, tools=None, **_):
        if len(messages) == 1:
            return ApiResponse(
                content=[{"type": "tool_use", "id": "s", "name": "send_email", "input": {"to": "a@b.c"}}],
                stop_reason="tool_use",
            )
        return ApiResponse(content=[{"type": "text", "text": "late"}], stop_reason="end_turn")

    r._call_api = one_send_then_text
    ledger = ExecutionLedger(session_id="subtask-1")
    ctx = ExecutionContext(kind="subtask", session_id="subtask-1")
    text, results, *_ = await _run_loop(r, is_background=True, context=ctx, ledger=ledger)
    assert d.calls == []  # the handler never ran
    assert results[0].error == background_halt.HALTED_TOOL_TEXT and text == background_halt.HALT_TURN_TEXT
    closed.assert_awaited_once_with(entry_id, "blocked", "refused by background_halt", keyed=False)
    assert "blocked" in [action.status for action in ledger.actions]  # as a refused snapshot records it


@pytest.mark.parametrize("point", AWAIT_POINTS)
async def test_a_halt_landing_during_the_pre_dispatch_work_refuses_the_call_in_the_stream(point):
    halt = _install()
    runner = _stream_runner()
    runner._dispatcher.available_tools.return_value = [
        {"name": "send_email", "description": "Send", "input_schema": {}}
    ]
    entry_id = uuid.uuid4()
    closed = _halt_lands_at(runner, halt, point, entry_id)
    calls = {"n": 0}

    async def one_send_then_text(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            yield StreamEvent(type="tool_start", tool_name="send_email", tool_id="s1", block_index=1)
            yield StreamEvent(type="tool_input_delta", text=json.dumps({"to": "a@b.c"}), block_index=1)
            yield StreamEvent(type="block_stop", block_index=1)
            yield StreamEvent(type="done", stop_reason="tool_use")
        else:
            yield StreamEvent(type="text_delta", text="late")
            yield StreamEvent(type="done", stop_reason="end_turn")

    runner._call_api_stream = MagicMock(side_effect=one_send_then_text)
    await _pump(runner.stream_chat("claude-job-4", "send it"))
    runner._dispatcher.dispatch.assert_not_called()
    closed.assert_awaited_once_with(entry_id, "blocked", "refused by background_halt", keyed=False)
    assert calls["n"] == 1  # and the loop made no further model call


@pytest.mark.parametrize("point", AWAIT_POINTS)
async def test_a_chat_call_is_not_refused_at_its_dispatch(point):
    from nous.cognitive.execution_ledger import ExecutionLedger

    halt = _install()
    r, d = _runner(["recall_deep"])
    _halt_lands_at(r, halt, point, uuid.uuid4())
    r._call_api, _calls = _two_calls_then("ok")
    chat = ExecutionContext(kind="interactive", session_id="s1")
    await _run_loop(r, is_background=False, context=chat, ledger=ExecutionLedger(session_id="s1"))
    assert halt.halted and [c[0] for c in d.calls] == ["recall_deep", "recall_deep"]  # chat runs while halted
