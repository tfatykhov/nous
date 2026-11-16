"""The chat routes go through the entry gate: which routes, what each mode does with each proof, and what is logged.
No database: the app is built with stand-ins, as tests/test_owner_guard_routes.py does."""

from __future__ import annotations

import hashlib
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from starlette.applications import Starlette

OWNER_VALUE = "owner-only-test-value"
PROXY_VALUE = "proxy-only-test-value"
MCP_VALUE = "mcp-only-test-value"
WRONG_VALUE = "a-wrong-test-value"
OWNER_DIGEST = hashlib.sha256(OWNER_VALUE.encode()).hexdigest()
PROXY_DIGEST = hashlib.sha256(PROXY_VALUE.encode()).hexdigest()
MCP_DIGEST = hashlib.sha256(MCP_VALUE.encode()).hexdigest()
FORBIDDEN = {"error": "forbidden"}
NOT_JSON = {"error": "Content-Type must be application/json"}
JSON_TYPE = "application/json"
MESSAGE = "a private message text"
GATE_LOGGER = "nous.api.entry_gate"

# Each proof a caller can present, as headers (a content type is added to each call by the matrix).
PROOFS: dict[str, dict[str, str]] = {
    "none": {"content-type": JSON_TYPE},
    "owner": {"X-Nous-Owner-Secret": OWNER_VALUE, "content-type": JSON_TYPE},
    "proxy-json": {"X-Nous-Proxy-Secret": PROXY_VALUE, "content-type": JSON_TYPE},
    "proxy-text": {"X-Nous-Proxy-Secret": PROXY_VALUE, "content-type": "text/plain"},
    "empty-header": {"X-Nous-Owner-Secret": "", "content-type": JSON_TYPE},
    "wrong": {"X-Nous-Owner-Secret": WRONG_VALUE, "content-type": JSON_TYPE},
}
PROVEN = {"owner", "proxy-json"}
MODES = ("off", "warn", "enforce")


class _Runner:
    """The runner as the chat routes call it: every call is recorded."""

    def __init__(self) -> None:
        turn_context = SimpleNamespace(frame=SimpleNamespace(frame_id="conversation"), decision_id=None)
        self.run_turn = AsyncMock(return_value=("hello", turn_context, {}))
        self.end_conversation = AsyncMock(return_value=None)
        self.streams: list[str] = []

    def stream_chat(self, session_id: str, message: str, **_kw: object):
        self.streams.append(session_id)

        async def events():
            yield SimpleNamespace(type="text_delta", text="hello", tool_name=None, stop_reason=None, usage=None)

        return events()


def _settings(tmp_path, mode: str | None = "warn", **kw: object):
    from nous.config import Settings

    values: dict[str, object] = {
        "workspace_dir": str(tmp_path),
        "owner_route_secret_sha256": OWNER_DIGEST,
        "mcp_client_ui_secret_sha256": PROXY_DIGEST,
        "mcp_server_secret_sha256": MCP_DIGEST,
        **kw,
    }
    if mode is not None:
        values["owner_entry_mode"] = mode
    return Settings(_env_file=None, **values)


def _rest_app(tmp_path, mode: str | None = "warn", *, runner: _Runner | None = None, heart: object = None, **kw):
    from nous.api.rest import create_app

    return create_app(
        runner or _Runner(),
        SimpleNamespace(),
        heart or SimpleNamespace(schedules=None),
        SimpleNamespace(),
        SimpleNamespace(),
        _settings(tmp_path, mode, **kw),
        heartbeat_runner=SimpleNamespace(finding_store=None),
    )


async def _send(app: Starlette, method: str, path: str, **kw: object) -> httpx.Response:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://nous") as client:
        return await client.request(method, path, **kw)


def _unproven_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == GATE_LOGGER and "carried no proof" in r.getMessage()]


# Each chat entry: (method, path, body) and how to tell whether the runner was reached.
CHAT_ENTRIES = {
    "chat": ("POST", "/chat", {"message": MESSAGE, "session_id": "pr-review-1"}),
    "chat-stream": ("POST", "/chat/stream", {"message": MESSAGE, "session_id": "pr-review-1"}),
    "end-chat": ("DELETE", "/chat/pr-review-1", None),
}


def _reached(runner: _Runner, entry: str) -> int:
    if entry == "chat":
        return runner.run_turn.await_count
    if entry == "chat-stream":
        return len(runner.streams)
    return runner.end_conversation.await_count


def test_every_chat_route_is_gated(tmp_path) -> None:
    app = _rest_app(tmp_path)
    chat_routes = [route for route in app.routes if getattr(route, "path", "").startswith("/chat")]
    assert {route.path for route in chat_routes} == {"/chat", "/chat/stream", "/chat/{session_id}"}
    for route in chat_routes:
        assert getattr(route.endpoint, "entry_gated", False) is True, route.path
        assert not getattr(route.endpoint, "owner_guarded", False), route.path  # the switch, not the bare guard


@pytest.mark.parametrize("entry", list(CHAT_ENTRIES))
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("proof", list(PROOFS))
async def test_each_chat_entry_against_each_mode_and_proof(tmp_path, entry: str, mode: str, proof: str) -> None:
    method, path, body = CHAT_ENTRIES[entry]
    runner = _Runner()
    app = _rest_app(tmp_path, mode, runner=runner)
    kw = {"content": json.dumps(body).encode()} if body is not None else {}
    response = await _send(app, method, path, headers=PROOFS[proof], **kw)
    gate = app.state.entry_gate
    if mode != "enforce" or proof in PROVEN:
        assert response.status_code == 200, response.text
        assert _reached(runner, entry) == 1
        counted = mode == "warn" and proof not in PROVEN
        assert gate.drain_counts() == ({"pr-review": 1} if counted else {})
        if entry == "chat-stream":
            assert response.headers["content-type"].startswith("text/event-stream")
            assert "data: " in response.text
    else:
        expected = (415, NOT_JSON) if proof == "proxy-text" else (403, FORBIDDEN)
        assert (response.status_code, response.json()) == expected
        assert response.headers["content-type"] == "application/json"  # a plain HTTP error, never a stream
        assert _reached(runner, entry) == 0
        assert gate.drain_counts() == {}


@pytest.mark.parametrize("entry", list(CHAT_ENTRIES))
async def test_enforce_refuses_chat_without_a_secret_before_the_body_is_read(tmp_path, entry: str) -> None:
    method, path, _body = CHAT_ENTRIES[entry]
    runner = _Runner()
    app = _rest_app(tmp_path, "enforce", runner=runner)
    response = await _send(app, method, path, content=b"\xff not json")
    assert (response.status_code, response.json()) == (403, FORBIDDEN)
    assert _reached(runner, entry) == 0


@pytest.mark.parametrize(
    ("session_id", "expected"),
    [("pr-review-897", "pr-review"), ("claude-job-1", "claude"), ("subtask-9", "background"), (None, "none")],
)
@pytest.mark.parametrize("path", ["/chat", "/chat/stream"])
async def test_warn_logs_the_session_class_of_a_chat_call_without_proof(
    tmp_path, caplog: pytest.LogCaptureFixture, path: str, session_id: str | None, expected: str
) -> None:
    caplog.set_level(logging.DEBUG)
    runner = _Runner()
    app = _rest_app(tmp_path, "warn", runner=runner)
    body = {"message": MESSAGE, **({"session_id": session_id} if session_id else {})}
    response = await _send(app, "POST", path, json=body)
    assert response.status_code == 200
    assert app.state.entry_gate.drain_counts() == {expected: 1}
    lines = _unproven_lines(caplog)
    assert len(lines) == 1
    assert f"POST {path} carried no proof" in lines[0]
    assert f"session class {expected}" in lines[0]
    for record in caplog.records:
        text = record.getMessage() + repr(record.args)
        assert MESSAGE not in text
        if session_id:
            assert session_id not in text


async def test_warn_logs_the_class_of_the_session_an_end_of_chat_names(
    tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    app = _rest_app(tmp_path, "warn")
    response = await _send(app, "DELETE", "/chat/claude-job-7")
    assert response.status_code == 200
    assert app.state.entry_gate.drain_counts() == {"claude": 1}
    lines = _unproven_lines(caplog)
    assert len(lines) == 1 and "DELETE /chat/{session_id}" in lines[0]
    # The server's records (the test client logs its own request line, path included).
    server = [r for r in caplog.records if r.name != "httpx"]
    assert not any("claude-job-7" in r.getMessage() + repr(r.args) for r in server)


async def test_warn_counts_a_chat_call_whose_body_is_not_json(tmp_path) -> None:
    runner = _Runner()
    app = _rest_app(tmp_path, "warn", runner=runner)
    response = await _send(app, "POST", "/chat", content=b"\xff")
    assert (response.status_code, response.json()) == (400, {"error": "Invalid JSON body"})
    assert app.state.entry_gate.drain_counts() == {"none": 1}


def test_the_default_mode_is_warn(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOUS_OWNER_ENTRY_MODE", raising=False)
    assert _rest_app(tmp_path, None).state.entry_gate.mode == "warn"


async def test_a_device_token_does_not_open_chat(tmp_path) -> None:
    runner = _Runner()
    app = _rest_app(tmp_path, "enforce", runner=runner)
    headers = {"X-Nous-Device-Token": "t" * 40, "content-type": JSON_TYPE}
    response = await _send(app, "POST", "/chat", json={"message": "hi"}, headers=headers)
    assert response.status_code == 403
    assert runner.run_turn.await_count == 0


@pytest.mark.parametrize(
    "headers", [{"X-Nous-Mcp-Secret": MCP_VALUE}, {"Authorization": f"Bearer {MCP_VALUE}"}], ids=["x-header", "bearer"]
)
async def test_the_mcp_key_does_not_open_chat(tmp_path, headers: dict[str, str]) -> None:
    runner = _Runner()
    app = _rest_app(tmp_path, "enforce", runner=runner)
    response = await _send(app, "POST", "/chat", json={"message": "hi"}, headers=headers)
    assert response.status_code == 403
    assert runner.run_turn.await_count == 0


async def test_the_mode_is_said_once_at_start(tmp_path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    _rest_app(tmp_path, "warn")
    lines = [r.getMessage() for r in caplog.records if r.name == GATE_LOGGER]
    assert len(lines) == 1 and "NOUS_OWNER_ENTRY_MODE=warn" in lines[0]


async def test_no_log_record_carries_a_secret_on_a_chat_route(tmp_path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    for mode in MODES:
        app = _rest_app(tmp_path, mode)
        for headers in PROOFS.values():
            await _send(
                app, "POST", "/chat", json={"message": MESSAGE, "session_id": "claude-secret-id"}, headers=headers
            )
            await _send(app, "DELETE", "/chat/claude-secret-id", headers=headers)
    server = [r for r in caplog.records if r.name != "httpx"]  # the test client logs its own request line
    assert server
    for record in server:
        text = record.getMessage() + repr(record.args)
        for value in (OWNER_VALUE, PROXY_VALUE, WRONG_VALUE, OWNER_DIGEST, PROXY_DIGEST, MESSAGE, "claude-secret-id"):
            assert value not in text, (record.name, value)
