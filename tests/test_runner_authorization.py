"""Harness Phase 1a: context threading + offered-set measurement/enforcement."""

from __future__ import annotations

import pytest

from nous.api.execution_context import ExecutionContext
from nous.api.models import ApiResponse
from nous.api.runner import AgentRunner, Conversation, Message
from nous.config import Settings
from tests.test_runner_background import _MockBrain, _MockCognitive, _MockHeart


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, ANTHROPIC_API_KEY="test-key", agent_id="test-agent", **overrides)


class _RecordingDispatcher:
    """Offers ``offered``; records every dispatch (name, context, is_background)."""

    def __init__(self, offered, store=None, registered=None):
        self.offered = list(offered)
        self.store = store
        self.registered = registered  # None = every name is registered
        self.calls: list[tuple[str, ExecutionContext | None, bool]] = []

    def is_registered(self, name):
        return self.registered is None or name in self.registered

    def available_tools(self, frame_id):
        return [{"name": n, "description": n, "input_schema": {"type": "object"}} for n in self.offered]

    async def dispatch(
        self, name, inp, session_id=None, is_background=False, turn_number=None, context=None, outcome=None
    ):
        if self.store is not None:
            self.store.events.append(("dispatch", name))
        self.calls.append((name, context, is_background))
        return f"{name} ran", False


def _one_tool_call_then_done(tool_name: str):
    calls = {"n": 0}

    async def fake_call_api(
        system_prompt, messages, tools=None, skip_thinking=False, model_override=None, is_background=False
    ):
        calls["n"] += 1
        if calls["n"] == 1:
            return ApiResponse(
                content=[{"type": "tool_use", "id": "t1", "name": tool_name, "input": {}}],
                stop_reason="tool_use",
            )
        return ApiResponse(content=[{"type": "text", "text": "done"}], stop_reason="end_turn")

    return fake_call_api


def _one_tool_call_then_done_with(tool_name: str, tool_input: dict):
    return _tool_calls_then_done_with(tool_name, tool_input, times=1)


def _two_tool_calls_then_done_with(tool_name: str, tool_input: dict):
    return _tool_calls_then_done_with(tool_name, tool_input, times=2)


def _tool_calls_then_done_with(tool_name: str, tool_input: dict, *, times: int):
    calls = {"n": 0}

    async def fake_call_api(
        system_prompt, messages, tools=None, skip_thinking=False, model_override=None, is_background=False
    ):
        calls["n"] += 1
        if calls["n"] <= times:
            return ApiResponse(
                content=[{"type": "tool_use", "id": f"t{calls['n']}", "name": tool_name, "input": dict(tool_input)}],
                stop_reason="tool_use",
            )
        return ApiResponse(content=[{"type": "text", "text": "done"}], stop_reason="end_turn")

    return fake_call_api


async def _run_loop(runner: AgentRunner, **kwargs):
    conv = Conversation(session_id="s1")
    conv.messages.append(Message(role="user", content="go"))
    try:
        return await runner._tool_loop(
            system_prompt="sys",
            conversation=conv,
            frame_id="conversation",
            session_id="s1",
            **kwargs,
        )
    finally:
        runner._api_shared = True
        await runner.close()


def _runner(offered, **settings_overrides):
    r = AgentRunner(_MockCognitive(), _MockBrain(), _MockHeart(), _settings(**settings_overrides))
    d = _RecordingDispatcher(offered)
    r.set_dispatcher(d)
    return r, d


# ---------------------------------------------------------------------------
# Task 2: context threading
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_loop_passes_the_explicit_context_to_dispatch():
    r, d = _runner(["recall_deep"])
    r._call_api = _one_tool_call_then_done("recall_deep")
    ctx = ExecutionContext(kind="heartbeat_triage", session_id="s1")
    await _run_loop(r, is_background=True, context=ctx)
    assert d.calls == [("recall_deep", ctx, True)]


@pytest.mark.asyncio
async def test_tool_loop_without_context_resolves_a_generic_one():
    r, d = _runner(["recall_deep"])
    r._call_api = _one_tool_call_then_done("recall_deep")
    await _run_loop(r, is_background=True)
    ((_name, ctx, is_bg),) = d.calls
    assert ctx.kind == "background" and ctx.session_id == "s1" and is_bg is True


@pytest.mark.asyncio
async def test_background_context_makes_the_loop_background():
    r, d = _runner(["recall_deep"])
    r._call_api = _one_tool_call_then_done("recall_deep")
    await _run_loop(r, context=ExecutionContext(kind="dag_summary", session_id="s1"))
    assert d.calls[0][2] is True


@pytest.mark.asyncio
async def test_run_turn_forwards_its_context_to_the_tool_loop():
    """Guards the run_turn -> _tool_loop hop that the loop-level tests skip."""
    r, _ = _runner(["recall_deep"])
    captured = {}

    async def fake_tool_loop(**kwargs):
        captured.update(kwargs)
        return "done", [], {"input_tokens": 0, "output_tokens": 0}, []

    r._tool_loop = fake_tool_loop  # type: ignore[method-assign]
    ctx = ExecutionContext(kind="scheduled", session_id="sched-1")
    try:
        await r.run_turn("sched-1", "go", is_background=True, skip_episode=True, context=ctx)
    finally:
        r._api_shared = True
        await r.close()
    assert captured["context"] is ctx and captured["is_background"] is True


@pytest.mark.asyncio
async def test_run_turn_background_context_makes_the_turn_background():
    """A caller passing a background context without is_background=True still
    runs a background turn (F048 streaming + dispatch agree)."""
    r, _ = _runner(["recall_deep"])
    captured = {}

    async def fake_tool_loop(**kwargs):
        captured.update(kwargs)
        return "done", [], {"input_tokens": 0, "output_tokens": 0}, []

    r._tool_loop = fake_tool_loop  # type: ignore[method-assign]
    try:
        await r.run_turn(
            "h-1",
            "go",
            skip_episode=True,
            context=ExecutionContext(kind="heartbeat_check", session_id="h-1"),
        )
    finally:
        r._api_shared = True
        await r.close()
    assert captured["is_background"] is True


@pytest.fixture
def probe_dispatcher():
    """A real ToolDispatcher with one tool that reads the injected flag."""
    from nous.api.tools import ToolDispatcher

    seen: list[bool] = []
    dispatcher = ToolDispatcher()

    async def probe(_is_background: bool = False):
        seen.append(_is_background)
        return {"content": [{"type": "text", "text": "ok"}]}

    dispatcher.register("probe", probe, {"type": "object", "description": "p"})
    dispatcher._BACKGROUND_AWARE_TOOLS = dispatcher._BACKGROUND_AWARE_TOOLS | {"probe"}
    return dispatcher, seen


@pytest.mark.asyncio
async def test_dispatcher_derives_is_background_from_context(probe_dispatcher):
    dispatcher, seen = probe_dispatcher
    await dispatcher.dispatch("probe", {}, context=ExecutionContext(kind="scheduled", session_id="x"))
    await dispatcher.dispatch("probe", {}, context=ExecutionContext(kind="interactive"))
    await dispatcher.dispatch("probe", {}, context=ExecutionContext(kind="mcp"))
    assert seen == [True, False, False]


@pytest.mark.asyncio
async def test_dispatcher_legacy_flag_still_works(probe_dispatcher):
    dispatcher, seen = probe_dispatcher
    await dispatcher.dispatch("probe", {}, is_background=True)
    await dispatcher.dispatch("probe", {})
    assert seen == [True, False]


# ---------------------------------------------------------------------------
# Task 3: offered-set measurement (warn) / enforcement (enforce)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_warn_mode_runs_the_call_and_records_it():
    """Default mode: nothing changes for the model; the event makes it measurable."""
    from unittest.mock import AsyncMock

    r, d = _runner(["recall_deep", "bash"])
    assert r._settings.tool_offered_set_enforcement_mode == "warn"
    r._brain.emit_event = AsyncMock()
    r._call_api = _one_tool_call_then_done("bash")
    await _run_loop(r, is_background=True, tool_filter=["recall_deep"])
    assert [c[0] for c in d.calls] == ["bash"]
    # _log_f026_decision schedules the write with asyncio.create_task: the
    # coroutine was CALLED (created) but may not have run when the loop returns.
    r._brain.emit_event.assert_called()
    event_type, data = r._brain.emit_event.call_args.args[:2]
    assert event_type == "harness_unoffered_tool_call"
    assert data["tool_name"] == "bash" and data["mode"] == "warn"
    assert data["context_kind"] == "background"


@pytest.mark.asyncio
async def test_enforce_mode_refuses_and_never_dispatches():
    from unittest.mock import AsyncMock

    r, d = _runner(["recall_deep", "bash"], tool_offered_set_enforcement_mode="enforce")
    r._brain.emit_event = AsyncMock()
    r._call_api = _one_tool_call_then_done("bash")
    _text, results, _usage, _thinking = await _run_loop(
        r,
        is_background=True,
        tool_filter=["recall_deep"],
    )
    assert d.calls == []
    (res,) = results
    assert res.tool_name == "bash" and "not available in this turn" in res.error


@pytest.mark.asyncio
async def test_enforce_mode_enforces_subtask_exclusions():
    from unittest.mock import AsyncMock

    r, d = _runner(["spawn_task", "recall_deep"], tool_offered_set_enforcement_mode="enforce")
    r._brain.emit_event = AsyncMock()
    r._call_api = _one_tool_call_then_done("spawn_task")
    await _run_loop(r, is_background=True, is_subtask=True)
    assert d.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["off", "warn", "enforce"])
async def test_offered_tool_runs_in_every_mode(mode):
    from unittest.mock import AsyncMock

    r, d = _runner(["recall_deep"], tool_offered_set_enforcement_mode=mode)
    r._brain.emit_event = AsyncMock()
    r._call_api = _one_tool_call_then_done("recall_deep")
    await _run_loop(r)
    assert [c[0] for c in d.calls] == ["recall_deep"]
    r._brain.emit_event.assert_not_called()


@pytest.mark.asyncio
async def test_extra_tools_count_as_offered():
    r, _d = _runner(["recall_deep"], tool_offered_set_enforcement_mode="enforce")
    r._call_api = _one_tool_call_then_done("submit_final_report")
    ran = []

    async def _submit(**_):
        ran.append(True)
        return "report accepted", False

    schema = {"name": "submit_final_report", "description": "s", "input_schema": {"type": "object"}}
    await _run_loop(r, is_background=True, extra_tools={"submit_final_report": (schema, _submit)})
    assert ran == [True]


@pytest.mark.asyncio
async def test_off_mode_is_silent():
    from unittest.mock import AsyncMock

    r, d = _runner(["recall_deep", "bash"], tool_offered_set_enforcement_mode="off")
    r._brain.emit_event = AsyncMock()
    r._call_api = _one_tool_call_then_done("bash")
    await _run_loop(r, is_background=True, tool_filter=["recall_deep"])
    assert [c[0] for c in d.calls] == ["bash"]
    r._brain.emit_event.assert_not_called()


@pytest.mark.asyncio
async def test_enforced_refusal_is_recorded_blocked_in_the_session_ledger():
    from unittest.mock import AsyncMock

    from nous.cognitive.execution_ledger import ExecutionLedger

    r, _d = _runner(["recall_deep", "bash"], tool_offered_set_enforcement_mode="enforce")
    r._brain.emit_event = AsyncMock()
    r._call_api = _one_tool_call_then_done("bash")
    ledger = ExecutionLedger(session_id="s1")
    await _run_loop(r, is_background=True, tool_filter=["recall_deep"], ledger=ledger)
    assert [(a.tool_name, a.status) for a in ledger.actions] == [("bash", "blocked")]


def test_mode_setting_rejects_unknown_values():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _settings(tool_offered_set_enforcement_mode="block")


@pytest.mark.asyncio
async def test_stream_chat_enforce_refuses_an_unoffered_tool():
    """/chat/stream offers web_search only; the model emits bash."""
    from unittest.mock import MagicMock

    from nous.api.anthropic_client import StreamEvent
    from tests.test_streaming import _make_mock_cognitive, _make_mock_settings, _make_runner

    cognitive, _ = _make_mock_cognitive()
    settings = _make_mock_settings()
    settings.tool_offered_set_enforcement_mode = "enforce"
    runner = _make_runner(cognitive, settings)
    calls = {"n": 0}

    async def fake_stream(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            yield StreamEvent(type="tool_start", tool_name="bash", tool_id="t1", block_index=1)
            yield StreamEvent(type="tool_input_delta", text='{"command": "id"}', block_index=1)
            yield StreamEvent(type="block_stop", block_index=1)
            yield StreamEvent(type="done", stop_reason="tool_use")
        else:
            yield StreamEvent(type="text_delta", text="ok")
            yield StreamEvent(type="done", stop_reason="end_turn")

    runner._call_api_stream = MagicMock(side_effect=fake_stream)
    events = [e async for e in runner.stream_chat("s1", "run it")]

    assert not runner._dispatcher.dispatch.called
    assert any(e.type == "tool_end" and e.tool_name == "bash" for e in events)
    second_call_messages = runner._call_api_stream.call_args_list[1][0][1]
    results = [
        b
        for m in second_call_messages
        if isinstance(m.get("content"), list)
        for b in m["content"]
        if isinstance(b, dict) and b.get("type") == "tool_result"
    ]
    assert len(results) == 1 and results[0]["is_error"] is True
    assert "not available in this turn" in results[0]["content"]


# ---------------------------------------------------------------------------
# Task 4: every caller names its context
# ---------------------------------------------------------------------------


def test_every_production_run_turn_call_passes_a_context():
    """No exemptions: interactive entry points pass an explicit interactive/mcp
    context too, so a background call added to rest.py/mcp.py later cannot
    slip through as the generic kind."""
    import ast
    from pathlib import Path

    offenders = []
    for path in Path("nous").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "run_turn"
                and not any(k.arg == "context" for k in node.keywords)
            ):
                offenders.append(f"{path.as_posix()}:{node.lineno}")
    assert offenders == [], offenders


# ---------------------------------------------------------------------------
# Harness Phase 2a, Task 2: the F078 refuse denylist derives from the table
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refuse_active_strips_previously_unclassified_tools():
    r, _ = _runner(["recall_deep", "dag_create"])
    sent: list[set[str]] = []

    async def capture(
        system_prompt, messages, tools=None, skip_thinking=False, model_override=None, is_background=False
    ):
        sent.append({t["name"] for t in tools or []})
        return ApiResponse(content=[{"type": "text", "text": "done"}], stop_reason="end_turn")

    r._call_api = capture
    await _run_loop(r, refuse_active=True)
    assert sent and "dag_create" not in sent[0] and "recall_deep" in sent[0]


# ---------------------------------------------------------------------------
# Harness Phase 2a, Task 5: the choke point consults the context policy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_policy_warn_runs_an_offered_call_and_records_it():
    r, d = _runner(["send_email"], tool_context_policy_mode="warn")
    events = []
    r._log_f026_decision = lambda kind, data, session_id: events.append((kind, data))
    r._call_api = _one_tool_call_then_done("send_email")
    await _run_loop(r, is_background=True, context=ExecutionContext(kind="heartbeat_triage"))
    assert [c[0] for c in d.calls] == ["send_email"]
    assert (
        "harness_context_policy_violation",
        {"tool_name": "send_email", "context_kind": "heartbeat_triage", "violation": "level:external", "mode": "warn"},
    ) in events


@pytest.mark.asyncio
async def test_policy_enforce_refuses_and_records_a_blocked_row():
    from tests.test_runner_ledger import _FakeStore

    store = _FakeStore()
    r, d = _runner(["spawn_task"], tool_context_policy_mode="enforce")
    r.set_ledger_store(store)
    r._call_api = _one_tool_call_then_done("spawn_task")
    _text, results, _usage, _thinking = await _run_loop(
        r,
        is_background=True,
        context=ExecutionContext(kind="dag_node"),
    )
    assert d.calls == []
    assert store.events == [("blocked", "spawn_task", "context_policy")]
    (res,) = results
    assert res.tool_name == "spawn_task" and "not allowed in a dag_node turn (spawn)" in res.error


@pytest.mark.asyncio
async def test_policy_off_is_silent():
    r, d = _runner(["spawn_task"], tool_context_policy_mode="off")
    events = []
    r._log_f026_decision = lambda kind, data, session_id: events.append(kind)
    r._call_api = _one_tool_call_then_done("spawn_task")
    await _run_loop(r, is_background=True, context=ExecutionContext(kind="dag_node"))
    assert [c[0] for c in d.calls] == ["spawn_task"] and events == []


@pytest.mark.asyncio
async def test_an_unoffered_call_is_checked_by_both_rules():
    r, d = _runner(
        ["recall_deep", "spawn_task"], tool_offered_set_enforcement_mode="warn", tool_context_policy_mode="warn"
    )
    events = []
    r._log_f026_decision = lambda kind, data, session_id: events.append(kind)
    r._call_api = _one_tool_call_then_done("spawn_task")
    await _run_loop(r, is_background=True, tool_filter=["recall_deep"], context=ExecutionContext(kind="dag_node"))
    assert events == ["harness_unoffered_tool_call", "harness_context_policy_violation"]


@pytest.mark.asyncio
async def test_a_foreground_turn_is_never_policed():
    r, d = _runner(["dag_create"], tool_context_policy_mode="enforce")
    events = []
    r._log_f026_decision = lambda kind, data, session_id: events.append(kind)
    r._call_api = _one_tool_call_then_done("dag_create")
    await _run_loop(r, context=ExecutionContext(kind="interactive"))
    assert [c[0] for c in d.calls] == ["dag_create"] and events == []


@pytest.mark.asyncio
async def test_stream_chat_hands_the_call_input_to_the_choke_point():
    """/chat/stream is always interactive (never policed), but it consults the
    same choke point with the call's INPUT, so the policy sees what the
    non-streaming loop sees."""
    from unittest.mock import MagicMock

    from nous.api.anthropic_client import StreamEvent
    from tests.test_streaming import _make_mock_cognitive, _make_mock_settings, _make_runner

    cognitive, _ = _make_mock_cognitive()
    settings = _make_mock_settings()
    settings.tool_offered_set_enforcement_mode = "off"
    settings.tool_context_policy_mode = "enforce"
    runner = _make_runner(cognitive, settings)
    seen = []
    original = runner._authorize_tool_call

    def recording(ctx, tool_name, offered_names, session_id, tool_input):
        seen.append((ctx.kind, tool_name, tool_input))
        return original(ctx, tool_name, offered_names, session_id, tool_input)

    runner._authorize_tool_call = recording
    calls = {"n": 0}

    async def fake_stream(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            yield StreamEvent(type="tool_start", tool_name="web_search", tool_id="t1", block_index=1)
            yield StreamEvent(type="tool_input_delta", text='{"query": "nous"}', block_index=1)
            yield StreamEvent(type="block_stop", block_index=1)
            yield StreamEvent(type="done", stop_reason="tool_use")
        else:
            yield StreamEvent(type="text_delta", text="ok")
            yield StreamEvent(type="done", stop_reason="end_turn")

    runner._call_api_stream = MagicMock(side_effect=fake_stream)
    [e async for e in runner.stream_chat("s1", "search it")]

    assert seen == [("interactive", "web_search", {"query": "nous"})]
    assert runner._dispatcher.dispatch.called  # a foreground turn is never policed


def test_policy_setting_defaults_to_warn():
    assert _settings().tool_context_policy_mode == "warn"


# ---------------------------------------------------------------------------
# Finding #2 regression — not_compensable must block in warn mode
# ---------------------------------------------------------------------------


def test_not_compensable_blocks_in_warn_mode():
    """not_compensable is a safety invariant: blocks even when mode='warn'.

    Before the fix, warn mode let every policy violation through (returning
    None). The fix adds a force_block on 'not_compensable' so an undoable
    dag_node cannot call a non-compensable tool even in the default mode.
    """
    from nous.api.runner import Refusal

    r, _d = _runner(["learn_fact"], tool_context_policy_mode="warn")
    ctx = ExecutionContext(kind="dag_node", undoable=True, session_id="s1")
    refusal = r._authorize_tool_call(ctx, "learn_fact", {"learn_fact"}, "s1", {})
    assert isinstance(refusal, Refusal), "not_compensable must produce a Refusal even in warn mode"
    assert "not_compensable" in refusal.text


def test_regular_policy_violation_still_passes_in_warn_mode():
    """Only not_compensable force-blocks; other violations still warn-through."""
    r, _d = _runner(["send_email"], tool_context_policy_mode="warn")
    ctx = ExecutionContext(kind="heartbeat_triage", session_id="s1")
    # send_email is external → violation "level:external", NOT not_compensable
    refusal = r._authorize_tool_call(ctx, "send_email", {"send_email"}, "s1", {})
    assert refusal is None, "A non-not_compensable violation should not block in warn mode"


@pytest.mark.asyncio
async def test_stream_chat_refuse_strips_the_denylist():
    """The streaming path strips exactly refuse_denylist(); reads survive."""
    from unittest.mock import MagicMock

    from nous.api.anthropic_client import StreamEvent
    from tests.test_streaming import _make_mock_cognitive, _make_mock_settings, _make_runner

    cognitive, turn_context = _make_mock_cognitive()
    turn_context.refuse_active = True
    runner = _make_runner(cognitive, _make_mock_settings())
    runner._dispatcher.available_tools.return_value = [
        {"name": n, "description": n, "input_schema": {"type": "object"}}
        for n in ("recall_deep", "dag_create", "push_surface", "bash", "web_fetch")
    ]
    offered: list[set[str]] = []

    async def fake_stream(*args, **kwargs):
        tools = kwargs.get("tools") or next(
            (a for a in args if isinstance(a, list) and a and isinstance(a[0], dict) and "name" in a[0]), []
        )
        offered.append({t["name"] for t in tools})
        yield StreamEvent(type="text_delta", text="ok")
        yield StreamEvent(type="done", stop_reason="end_turn")

    runner._call_api_stream = MagicMock(side_effect=fake_stream)
    [e async for e in runner.stream_chat("s1", "hi")]
    assert offered == [{"recall_deep", "web_fetch"}]
