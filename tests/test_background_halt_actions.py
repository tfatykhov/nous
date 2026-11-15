"""Goals PR 0c: a tool handler that awaits before its own action checks the halt right before that action."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from test_mcp_client_manager import _alpha, _tool

from nous import background_halt
from nous.api.execution_context import ExecutionContext
from nous.api.tools import ToolDispatcher
from nous.background_halt import BackgroundHalt


def _install() -> BackgroundHalt:
    halt = BackgroundHalt(host_hold=False)
    background_halt.install(halt)
    return halt


async def test_the_dispatcher_hands_its_handler_the_calls_scope():
    halt = _install()
    seen: list[bool] = []

    async def handler(**kwargs):
        await halt.halt(actor="t", surface="dashboard")  # lands while the handler awaits
        seen.append(background_halt.call_refused())
        return {"content": [{"type": "text", "text": "ok"}]}

    dispatcher = ToolDispatcher()
    dispatcher.register("probe", handler, {"type": "object", "properties": {}})
    await dispatcher.dispatch("probe", {}, context=ExecutionContext(kind="subtask", session_id="subtask-1"))
    assert (await halt.resume(actor="t", via="dashboard")).changed
    await dispatcher.dispatch("probe", {}, context=ExecutionContext(kind="interactive", session_id="s1"))
    assert seen == [True, False]  # a background call is refused, a chat call is not
    assert background_halt.CALL_SCOPE.get() is None and not background_halt.call_refused()  # outside a call


async def test_an_mcp_call_whose_connection_was_awaited_as_the_halt_landed_is_not_sent():
    from contextlib import asynccontextmanager

    from test_mcp_client_manager import FakeRegistry, FakeServer, FakeSession, _pins, _settings

    from nous.mcp_client import McpClientManager

    halt = _install()

    class _HaltOnConnect(FakeServer):
        @asynccontextmanager
        async def connect(self, url, allow, *, auth=None):
            self.opened += 1
            await halt.halt(actor="t", surface="dashboard")  # lands while the connection is awaited
            yield FakeSession(self)

    rows = [_alpha(_tool("alpha", "get.thing"))]
    fake = _HaltOnConnect()
    dispatcher = ToolDispatcher()
    manager = McpClientManager(
        dispatcher, FakeRegistry(rows), _settings(mcp_client_pinned_servers=_pins(rows)), None, connect=fake.connect
    )
    await manager.start()
    text, is_error = await dispatcher.dispatch(
        "mcp__alpha__get_thing", {"q": "hi"}, context=ExecutionContext(kind="subtask", session_id="subtask-1")
    )
    assert is_error and "halted by the owner" in text and fake.calls == []
    text, is_error = await dispatcher.dispatch(
        "mcp__alpha__get_thing", {"q": "chat"}, context=ExecutionContext(kind="interactive", session_id="s1")
    )
    assert not is_error and fake.calls == [("get.thing", {"q": "chat"})]  # chat still reaches the server
    await manager.close()


async def test_a_browser_task_stops_at_its_next_step_once_the_halt_refuses_its_call():
    from nous.browser.loop import BrowserTaskResult, _drive

    halt = _install()
    executor = SimpleNamespace(execute=AsyncMock(return_value="done"))
    model_calls = 0

    async def call_api(payload):
        nonlocal model_calls
        model_calls += 1
        await halt.halt(actor="t", surface="dashboard")  # lands while the inner model call is out
        return {
            "stop_reason": "tool_use",
            "content": [
                {"type": "tool_use", "id": "a1", "name": "click", "input": {"ref": "e1"}, "toolset_name": "browser"}
            ],
        }

    state = BrowserTaskResult(status="running", summary="", run_id="r1")
    token = background_halt.CALL_SCOPE.set(("subtask", "subtask-1", None))  # as the dispatcher sets it
    try:
        await _drive("look", SimpleNamespace(hosts={"example.com"}), None, 5, "m", call_api, executor, state)
    finally:
        background_halt.CALL_SCOPE.reset(token)
    executor.execute.assert_not_awaited()  # the action the model asked for after the halt never ran
    assert model_calls == 1 and state.status == "error" and "halted" in state.summary
