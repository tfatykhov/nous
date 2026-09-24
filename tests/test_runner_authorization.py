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

    def __init__(self, offered, store=None):
        self.offered = list(offered)
        self.store = store
        self.calls: list[tuple[str, ExecutionContext | None, bool]] = []

    def available_tools(self, frame_id):
        return [{"name": n, "description": n, "input_schema": {"type": "object"}} for n in self.offered]

    async def dispatch(self, name, inp, session_id=None, is_background=False,
                       turn_number=None, context=None):
        if self.store is not None:
            self.store.events.append(("dispatch", name))
        self.calls.append((name, context, is_background))
        return f"{name} ran", False


def _one_tool_call_then_done(tool_name: str):
    calls = {"n": 0}

    async def fake_call_api(system_prompt, messages, tools=None, skip_thinking=False,
                            model_override=None, is_background=False):
        calls["n"] += 1
        if calls["n"] == 1:
            return ApiResponse(
                content=[{"type": "tool_use", "id": "t1", "name": tool_name, "input": {}}],
                stop_reason="tool_use",
            )
        return ApiResponse(content=[{"type": "text", "text": "done"}], stop_reason="end_turn")

    return fake_call_api


async def _run_loop(runner: AgentRunner, **kwargs):
    conv = Conversation(session_id="s1")
    conv.messages.append(Message(role="user", content="go"))
    try:
        return await runner._tool_loop(
            system_prompt="sys", conversation=conv, frame_id="conversation",
            session_id="s1", **kwargs,
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
            "h-1", "go", skip_episode=True,
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
