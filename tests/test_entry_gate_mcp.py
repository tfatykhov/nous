"""The inbound MCP server goes through the entry gate, and the lifespan runs the daily notice. No database: the app is
built by main.build_app with its components and the MCP server replaced by stand-ins."""

from __future__ import annotations

import asyncio
import hashlib
import logging

import httpx
import pytest
from starlette.routing import Mount

from nous import main as M
from nous.config import Settings

OWNER_VALUE = "owner-only-test-value"
PROXY_VALUE = "proxy-only-test-value"
MCP_VALUE = "mcp-only-test-value"
WRONG_VALUE = "a-wrong-test-value"
OWNER_DIGEST = hashlib.sha256(OWNER_VALUE.encode()).hexdigest()
PROXY_DIGEST = hashlib.sha256(PROXY_VALUE.encode()).hexdigest()
MCP_DIGEST = hashlib.sha256(MCP_VALUE.encode()).hexdigest()
JSON_TYPE = "application/json"
GATE_LOGGER = "nous.api.entry_gate"

PROOFS: dict[str, dict[str, str]] = {
    "none": {"content-type": JSON_TYPE},
    "owner": {"X-Nous-Owner-Secret": OWNER_VALUE, "content-type": JSON_TYPE},
    "proxy-json": {"X-Nous-Proxy-Secret": PROXY_VALUE, "content-type": JSON_TYPE},
    "proxy-text": {"X-Nous-Proxy-Secret": PROXY_VALUE, "content-type": "text/plain"},
    "empty-header": {"X-Nous-Owner-Secret": "", "content-type": JSON_TYPE},
    "wrong": {"X-Nous-Owner-Secret": WRONG_VALUE, "content-type": JSON_TYPE},
    "mcp-bearer": {"Authorization": f"Bearer {MCP_VALUE}", "content-type": JSON_TYPE},
    "mcp-header": {"X-Nous-Mcp-Secret": MCP_VALUE, "content-type": JSON_TYPE},
}
PROVEN_ON_MCP = {"owner", "mcp-bearer", "mcp-header"}  # the proxy's secret is not a proof here


class _FakeManager:
    def __init__(self) -> None:
        self.requests = 0

    async def handle_request(self, scope, receive, send) -> None:
        from starlette.responses import JSONResponse

        self.requests += 1
        await JSONResponse({"mcp": True})(scope, receive, send)

    async def close(self) -> None:
        pass


@pytest.fixture
def stand_ins(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Components, the MCP server and the Telegram sender replaced; ``created`` counts MCP servers built."""
    state: dict[str, object] = {"created": 0, "manager": _FakeManager(), "sent": []}

    async def fake_create_components(settings):
        return {"runner": object(), "brain": object(), "heart": object()}

    async def fake_shutdown(components):
        return None

    def fake_create_mcp_server(**_kw):
        state["created"] = int(state["created"]) + 1
        return state["manager"]

    def fake_push(settings, **_kw):
        async def push(text: str) -> bool:
            state["sent"].append(text)  # type: ignore[union-attr]
            return True

        return push

    monkeypatch.setattr(M, "create_components", fake_create_components)
    monkeypatch.setattr(M, "shutdown_components", fake_shutdown)
    monkeypatch.setattr(M, "start_event_loop_watchdog", lambda s: None)
    monkeypatch.setattr("nous.api.mcp.create_mcp_server", fake_create_mcp_server)
    monkeypatch.setattr(M, "_telegram_text_push", fake_push)
    return state


def _settings(tmp_path, mode: str = "warn", **kw: object) -> Settings:
    values: dict[str, object] = {
        "workspace_dir": str(tmp_path),
        "owner_entry_mode": mode,
        "owner_route_secret_sha256": OWNER_DIGEST,
        "mcp_client_ui_secret_sha256": PROXY_DIGEST,
        "mcp_server_secret_sha256": MCP_DIGEST,
        "mcp_enabled": True,
        **kw,
    }
    return Settings(_env_file=None, **values)


async def _post_mcp(app, headers: dict[str, str]) -> httpx.Response:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://nous") as client:
        return await client.post(
            "/mcp/", content=b'{"jsonrpc": "2.0", "method": "tools/list", "id": 1}', headers=headers
        )


def _mcp_mount(app) -> Mount:
    mounts = [route for route in app.routes if isinstance(route, Mount) and route.path == "/mcp"]
    assert len(mounts) == 1
    return mounts[0]


def test_the_mcp_mount_is_gated(tmp_path, stand_ins) -> None:
    app = M.build_app(_settings(tmp_path))
    assert getattr(_mcp_mount(app).app, "entry_gated", False) is True


def test_with_mcp_disabled_there_is_no_mount_and_nothing_to_gate(tmp_path, stand_ins) -> None:
    app = M.build_app(_settings(tmp_path, mcp_enabled=False))
    assert not [route for route in app.routes if getattr(route, "path", "").startswith("/mcp")]


@pytest.mark.parametrize("mode", ["off", "warn", "enforce"])
@pytest.mark.parametrize("proof", list(PROOFS))
async def test_the_mcp_server_against_each_mode_and_proof(tmp_path, stand_ins, mode: str, proof: str) -> None:
    app = M.build_app(_settings(tmp_path, mode))
    async with app.router.lifespan_context(app):
        response = await _post_mcp(app, PROOFS[proof])
        counts = app.state.entry_gate.drain_counts()
    manager = stand_ins["manager"]
    if mode != "enforce" or proof in PROVEN_ON_MCP:
        assert (response.status_code, response.json()) == (200, {"mcp": True})
        assert manager.requests == 1
        assert counts == ({"none": 1} if mode == "warn" and proof not in PROVEN_ON_MCP else {})
    else:
        assert (response.status_code, response.json()) == (403, {"error": "forbidden"})
        assert manager.requests == 0
        assert stand_ins["created"] == 0  # refused before the MCP server is built
        assert counts == {}


async def test_enforce_refuses_an_mcp_request_with_no_secret_and_the_manager_is_never_created(
    tmp_path, stand_ins
) -> None:
    app = M.build_app(_settings(tmp_path, "enforce"))
    async with app.router.lifespan_context(app):
        refused = await _post_mcp(app, {"content-type": JSON_TYPE})
        assert refused.status_code == 403
        assert stand_ins["created"] == 0
        allowed = await _post_mcp(app, PROOFS["mcp-bearer"])
        assert allowed.status_code == 200
        assert stand_ins["created"] == 1


async def test_warn_logs_an_mcp_request_without_proof(tmp_path, stand_ins, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    app = M.build_app(_settings(tmp_path, "warn"))
    async with app.router.lifespan_context(app):
        response = await _post_mcp(app, {"content-type": JSON_TYPE, "Authorization": f"Bearer {WRONG_VALUE}"})
    assert response.status_code == 200
    lines = [r.getMessage() for r in caplog.records if r.name == GATE_LOGGER and "carried no proof" in r.getMessage()]
    assert len(lines) == 1 and "POST /mcp" in lines[0] and "session class none" in lines[0]
    for record in caplog.records:
        text = record.getMessage() + repr(record.args)
        for value in (WRONG_VALUE, MCP_VALUE, MCP_DIGEST):
            assert value not in text


async def test_the_daily_notice_task_is_started_and_cancelled_by_the_lifespan(tmp_path, stand_ins) -> None:
    app = M.build_app(_settings(tmp_path, "warn"))
    async with app.router.lifespan_context(app):
        task = app.state.entry_notice_task
        assert isinstance(task, asyncio.Task)
        await asyncio.sleep(0)
        assert not task.done()
    assert task.cancelled()


async def test_the_lifespan_sends_the_pending_count_once_at_a_graceful_shutdown(tmp_path, stand_ins) -> None:
    app = M.build_app(_settings(tmp_path, "warn"))
    async with app.router.lifespan_context(app):
        await _post_mcp(app, {"content-type": JSON_TYPE})
        await _post_mcp(app, {"content-type": JSON_TYPE})
        assert stand_ins["sent"] == []  # the daily line is not due yet
    assert len(stand_ins["sent"]) == 1
    assert "2 calls to chat or the MCP server" in stand_ins["sent"][0]


@pytest.mark.parametrize("mode", ["off", "enforce"])
async def test_no_notice_at_shutdown_outside_warn(tmp_path, stand_ins, mode: str) -> None:
    app = M.build_app(_settings(tmp_path, mode))
    async with app.router.lifespan_context(app):
        await _post_mcp(app, {"content-type": JSON_TYPE})
    assert stand_ins["sent"] == []


@pytest.mark.parametrize("mode", ["off", "enforce"])
async def test_no_daily_notice_task_outside_warn(tmp_path, stand_ins, mode: str) -> None:
    app = M.build_app(_settings(tmp_path, mode))
    async with app.router.lifespan_context(app):
        assert app.state.entry_notice_task is None


async def test_no_daily_notice_task_without_telegram(tmp_path, stand_ins, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(M, "_telegram_text_push", lambda settings, **kw: None)
    app = M.build_app(_settings(tmp_path, "warn"))
    async with app.router.lifespan_context(app):
        assert app.state.entry_notice_task is None


async def test_the_lifespan_sends_the_notice_through_the_telegram_sender(
    tmp_path, stand_ins, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nous.api import entry_gate

    turns: asyncio.Queue[None] = asyncio.Queue()
    slept = asyncio.Event()
    real_loop = entry_gate.EntryGate.daily_notice_loop

    async def released_sleep(_seconds: float) -> None:
        slept.set()
        await turns.get()

    def loop_with_test_sleep(self, send, **kw):
        return real_loop(self, send, sleep=released_sleep)

    monkeypatch.setattr(entry_gate.EntryGate, "daily_notice_loop", loop_with_test_sleep)
    app = M.build_app(_settings(tmp_path, "warn"))
    async with app.router.lifespan_context(app):
        await _post_mcp(app, {"content-type": JSON_TYPE})
        await asyncio.wait_for(slept.wait(), timeout=5)
        slept.clear()
        turns.put_nowait(None)
        await asyncio.wait_for(slept.wait(), timeout=5)
    assert len(stand_ins["sent"]) == 1
    assert "1 call to chat or the MCP server" in stand_ins["sent"][0]


async def test_the_lifespan_cancels_the_daily_task_before_the_notice_at_shutdown(
    tmp_path, stand_ins, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shutdown while the daily line is in flight: the daily task is cancelled first, then the notice at shutdown
    sends the count the cancelled line did not deliver."""
    from nous.api import entry_gate

    go = asyncio.Event()
    in_flight = asyncio.Event()
    calls: list[tuple[str, bool]] = []
    holder: dict[str, object] = {}
    real_loop = entry_gate.EntryGate.daily_notice_loop
    sleeps = 0

    async def sleep_once(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        await (go.wait() if sleeps == 1 else asyncio.Event().wait())

    def fake_push(settings, **_kw):
        async def push(text: str) -> bool:
            task = holder["app"].state.entry_notice_task  # type: ignore[attr-defined]
            calls.append((text, task.done()))
            if len(calls) == 1:
                in_flight.set()
                await asyncio.Event().wait()  # the daily line never goes out
            return True

        return push

    monkeypatch.setattr(M, "_telegram_text_push", fake_push)
    monkeypatch.setattr(
        entry_gate.EntryGate, "daily_notice_loop", lambda self, send, **kw: real_loop(self, send, sleep=sleep_once)
    )
    app = M.build_app(_settings(tmp_path, "warn"))
    holder["app"] = app
    async with app.router.lifespan_context(app):
        await _post_mcp(app, {"content-type": JSON_TYPE})
        go.set()
        await asyncio.wait_for(in_flight.wait(), timeout=5)
    assert [done for _text, done in calls] == [False, True]  # the shutdown line came after the cancel
    assert "1 call to chat or the MCP server" in calls[1][0]


async def test_a_failed_notice_send_is_logged_by_its_class_with_no_traceback(
    tmp_path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The real sender the notices use: a client error whose text holds the request URL is logged by its class."""
    import httpx as real_httpx

    from nous.api.entry_gate import EntryGate
    from nous.api.owner_guard import OwnerGuard

    token = "123456789:" + "A" * 35

    class Down(real_httpx.AsyncClient):
        def __init__(self, **kwargs):
            def refuse(request):
                raise real_httpx.ConnectError(f"cannot reach {request.url}")

            super().__init__(transport=real_httpx.MockTransport(refuse), **kwargs)

    monkeypatch.setattr(M.httpx, "AsyncClient", Down)
    settings = _settings(tmp_path, "warn", telegram_bot_token=token, telegram_chat_id="1")
    gate = EntryGate(settings, OwnerGuard(settings))
    gate._counts["claude"] += 1
    caplog.set_level(logging.DEBUG)
    await gate.notice_at_shutdown(M._telegram_text_push(settings, raise_errors=True))
    lines = [r for r in caplog.records if r.name == GATE_LOGGER]
    assert [r.getMessage() for r in lines] == ["owner entry: the notice at shutdown could not be sent (ConnectError)"]
    for record in caplog.records:
        assert record.exc_info is None, record.name
        text = record.getMessage() + repr(record.args)
        assert token not in text and "A" * 35 not in text, record.name
    assert gate.drain_counts() == {"claude": 1}  # not sent: kept


async def test_the_shared_sender_keeps_its_own_failure_line_for_its_other_callers(
    tmp_path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import httpx as real_httpx

    class Down(real_httpx.AsyncClient):
        def __init__(self, **kwargs):
            def refuse(request):
                raise real_httpx.ConnectError("down")

            super().__init__(transport=real_httpx.MockTransport(refuse), **kwargs)

    monkeypatch.setattr(M.httpx, "AsyncClient", Down)
    settings = _settings(tmp_path, "warn", telegram_bot_token="123456789:" + "B" * 35, telegram_chat_id="1")
    caplog.set_level(logging.DEBUG)
    assert await M._telegram_text_push(settings)("x") is False
    lines = [r for r in caplog.records if r.name == "nous.main"]
    assert len(lines) == 1 and lines[0].exc_info is not None
