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


# ---- a handler that awaits a read before its side effect --------------------------------------------------------

SUBTASK = ExecutionContext(kind="subtask", session_id="subtask-1")


async def test_a_file_whose_read_was_awaited_as_the_halt_landed_is_not_sent(tmp_path, monkeypatch):
    import asyncio

    from nous.api import telegram_tools

    halt = _install()
    path = tmp_path / "report.txt"
    path.write_text("numbers")
    real_to_thread = asyncio.to_thread

    async def read_while_the_halt_lands(fn, *args, **kwargs):
        await halt.halt(actor="t", surface="dashboard")
        return await real_to_thread(fn, *args, **kwargs)

    monkeypatch.setattr(telegram_tools.asyncio, "to_thread", read_while_the_halt_lands)
    http_client = SimpleNamespace(post=AsyncMock())
    settings = SimpleNamespace(telegram_bot_token="token", telegram_chat_id="7")
    dispatcher = ToolDispatcher()
    dispatcher.register(
        "send_file",
        telegram_tools.create_send_file_tool(settings, http_client),
        {"type": "object", "properties": {"file_path": {"type": "string"}}},
    )
    text, is_error = await dispatcher.dispatch("send_file", {"file_path": str(path)}, context=SUBTASK)
    assert is_error and background_halt.HALT_REASON in text and "not sent" in text
    http_client.post.assert_not_awaited()


async def test_a_write_whose_journal_snapshot_was_awaited_as_the_halt_landed_writes_nothing(tmp_path, monkeypatch):
    import functools

    from nous import undo_journal
    from nous.api.builtin_tools import write_file_tool

    halt = _install()

    async def snapshot_while_the_halt_lands(path, workspace_dir):
        await halt.halt(actor="t", surface="dashboard")

    monkeypatch.setattr(undo_journal, "get_journal", lambda: SimpleNamespace(contains=lambda target: False))
    monkeypatch.setattr(undo_journal, "record_file_write", snapshot_while_the_halt_lands)
    dispatcher = ToolDispatcher()
    dispatcher.register(
        "write_file",
        functools.partial(write_file_tool, _workspace_dir=str(tmp_path)),
        {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}},
    )
    text, is_error = await dispatcher.dispatch("write_file", {"path": "a.txt", "content": "x"}, context=SUBTASK)
    assert is_error and background_halt.HALT_REASON in text and "nothing was written" in text
    assert not (tmp_path / "a.txt").exists()
    # A chat turn's write goes on.
    text, is_error = await dispatcher.dispatch(
        "write_file", {"path": "a.txt", "content": "x"}, context=ExecutionContext(kind="interactive", session_id="s1")
    )
    assert not is_error and (tmp_path / "a.txt").read_text() == "x"


async def test_a_surface_whose_compose_was_awaited_as_the_halt_landed_is_not_pushed():
    from test_a2ui_phase2 import _CapturingService

    from nous.a2ui.tools import register_a2ui_tools

    halt = _install()

    async def compose_while_the_halt_lands(intent, **kwargs):
        await halt.halt(actor="t", surface="dashboard")
        return SimpleNamespace(built=object(), fallback=False)

    service = _CapturingService()
    dispatcher = ToolDispatcher()
    register_a2ui_tools(dispatcher, service, composer=SimpleNamespace(compose=compose_while_the_halt_lands))
    text, is_error = await dispatcher.dispatch("compose_surface", {"intent": "snow report"}, context=SUBTASK)
    assert is_error and background_halt.HALT_REASON in text and service.pushed == []


async def test_a_surface_whose_reads_were_awaited_as_the_halt_landed_is_not_pushed():
    from test_a2ui_phase2 import _CapturingService, _FakeDagStore

    from nous.a2ui.tools import register_a2ui_tools

    halt = _install()

    class _HaltOnRead(_FakeDagStore):
        async def get_dag(self, dag_id):
            await halt.halt(actor="t", surface="dashboard")
            return await super().get_dag(dag_id)

    service = _CapturingService()
    dispatcher = ToolDispatcher()
    register_a2ui_tools(dispatcher, service, dag_store=_HaltOnRead())
    dag_id = "0e9d8c7b-6a5f-4e3d-8c2b-1a0f9e8d7c6b"
    text, is_error = await dispatcher.dispatch(
        "push_surface", {"template": "dag_monitor", "params": {"dag_id": dag_id}}, context=SUBTASK
    )
    assert is_error and background_halt.HALT_REASON in text and service.pushed == []
