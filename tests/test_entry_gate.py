"""The entry gate: chat and the inbound MCP server require the owner's or the proxy's secret (or, on the MCP server,
the MCP key), under NOUS_OWNER_ENTRY_MODE (off, warn, enforce). No database: a tiny Starlette app and stand-in
settings."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse

from nous.api.entry_gate import SESSION_CLASSES, EntryGate, session_class
from nous.api.owner_guard import OwnerGuard

OWNER_VALUE = "owner-only-test-value"
PROXY_VALUE = "proxy-only-test-value"
MCP_VALUE = "mcp-only-test-value"
WRONG_VALUE = "a-wrong-test-value"
OWNER_DIGEST = hashlib.sha256(OWNER_VALUE.encode()).hexdigest()
PROXY_DIGEST = hashlib.sha256(PROXY_VALUE.encode()).hexdigest()
MCP_DIGEST = hashlib.sha256(MCP_VALUE.encode()).hexdigest()
OWNER = {"X-Nous-Owner-Secret": OWNER_VALUE}
PROXY = {"X-Nous-Proxy-Secret": PROXY_VALUE}
JSON_TYPE = {"content-type": "application/json"}
FORBIDDEN = {"error": "forbidden"}
NOT_JSON = {"error": "Content-Type must be application/json"}
SESSION_ID = "claude-abc-private-session"
MESSAGE = "a private message text"
GATE_LOGGER = "nous.api.entry_gate"


def _settings(mode: object = "warn", *, owner: bool = True, proxy: bool = True, mcp: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        owner_entry_mode=mode,
        owner_route_secret_sha256=OWNER_DIGEST if owner else "",
        mcp_client_ui_secret_sha256=PROXY_DIGEST if proxy else "",
        mcp_server_secret_sha256=MCP_DIGEST if mcp else "",
    )


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _gate(mode: object = "warn", *, devices: object | None = None, clock: _Clock | None = None, **kw: bool):
    settings = _settings(mode, **kw)
    return EntryGate(settings, OwnerGuard(settings, devices=devices), clock=clock or _Clock())


def _app(gate: EntryGate, calls: list[str]) -> Starlette:
    """One gated route. The handler notes the session id of a JSON body, as the chat handlers do."""

    async def handler(request: Request) -> JSONResponse:
        calls.append(request.method)
        if request.method in ("GET", "DELETE"):
            return JSONResponse({"ok": True})
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        gate.note_session(request, body.get("session_id") if isinstance(body, dict) else None)
        return JSONResponse({"ok": True})

    return Starlette(routes=[gate.route("/chat", handler, methods=["GET", "POST", "DELETE"])])


async def _send(app, method: str, path: str = "/chat", *, client=("127.0.0.1", 5000), **kw) -> httpx.Response:
    transport = httpx.ASGITransport(app=app, client=client)
    async with httpx.AsyncClient(transport=transport, base_url="http://nous") as http:
        return await http.request(method, path, **kw)


def _gate_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == GATE_LOGGER]


def _unproven_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in _gate_records(caplog) if "carried no proof" in r.getMessage()]


# ---- enforce ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-Nous-Owner-Secret": WRONG_VALUE},
        {"X-Nous-Owner-Secret": PROXY_VALUE},
        {"X-Nous-Owner-Secret": ""},
        {"X-Nous-Proxy-Secret": OWNER_VALUE},
        {"X-Nous-Mcp-Secret": MCP_VALUE},
        {"Authorization": f"Bearer {MCP_VALUE}"},
    ],
    ids=["none", "wrong", "proxy-value-in-owner-header", "empty", "owner-value-in-proxy-header", "mcp-key", "bearer"],
)
async def test_enforce_refuses_a_call_with_no_secret_before_the_handler(headers: dict[str, str]) -> None:
    calls: list[str] = []
    response = await _send(_app(_gate("enforce"), calls), "POST", content=b"\xff", headers=headers)
    assert (response.status_code, response.json()) == (403, FORBIDDEN)
    assert calls == []


@pytest.mark.parametrize(
    ("method", "headers", "expected"),
    [
        ("POST", OWNER, 200),
        ("POST", {**OWNER, "content-type": "text/plain"}, 200),
        ("POST", {**PROXY, **JSON_TYPE}, 200),
        ("POST", {**PROXY, "content-type": "application/json; charset=utf-8"}, 200),
        ("POST", {**PROXY, "content-type": "text/plain"}, 415),
        ("POST", {**PROXY, "content-type": "text/plain; application/json"}, 415),
        ("DELETE", PROXY, 415),
        ("DELETE", {**PROXY, **JSON_TYPE}, 200),
        ("GET", PROXY, 200),
    ],
    ids=[
        "owner",
        "owner-text",
        "proxy-json",
        "proxy-json-charset",
        "proxy-text",
        "proxy-smuggled-json",
        "proxy-delete-untyped",
        "proxy-delete-json",
        "proxy-read",
    ],
)
async def test_enforce_lets_the_owner_and_the_proxy_through(method: str, headers: dict, expected: int) -> None:
    calls: list[str] = []
    kw = {"content": b'{"session_id": "x"}'} if method == "POST" else {}
    response = await _send(_app(_gate("enforce"), calls), method, headers=headers, **kw)
    assert response.status_code == expected
    if expected == 415:
        assert response.json() == NOT_JSON
        assert calls == []
    else:
        assert calls == [method]


async def test_enforce_with_no_digest_refuses_every_call_and_says_why_at_start(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    gate = _gate("enforce", owner=False, proxy=False, mcp=False)
    gate.startup_report()
    errors = [r.getMessage() for r in _gate_records(caplog) if r.levelno == logging.ERROR]
    assert len(errors) == 1 and "NOUS_OWNER_ENTRY_MODE=enforce" in errors[0] and "chat" in errors[0]
    # With the MCP server on, it says so for the MCP server too.
    caplog.clear()
    settings = SimpleNamespace(**vars(_settings("enforce", owner=False, proxy=False, mcp=False)), mcp_enabled=True)
    EntryGate(settings, OwnerGuard(settings)).startup_report()
    errors = [r.getMessage() for r in _gate_records(caplog) if r.levelno == logging.ERROR]
    assert len(errors) == 2 and "MCP" in errors[1]
    # The proxy digest alone keeps chat open to the proxy, but not the MCP server.
    caplog.clear()
    settings = SimpleNamespace(**vars(_settings("enforce", owner=False, mcp=False)), mcp_enabled=True)
    EntryGate(settings, OwnerGuard(settings)).startup_report()
    errors = [r.getMessage() for r in _gate_records(caplog) if r.levelno == logging.ERROR]
    assert len(errors) == 1 and "MCP" in errors[0]
    calls: list[str] = []
    for headers in ({}, OWNER, {**PROXY, **JSON_TYPE}):
        response = await _send(_app(gate, calls), "POST", json={}, headers=headers)
        assert response.status_code == 403
    assert calls == []


@pytest.mark.parametrize(
    ("mode", "level"), [("off", logging.WARNING), ("warn", logging.WARNING), ("enforce", logging.INFO)]
)
def test_the_start_up_line_names_the_mode(caplog: pytest.LogCaptureFixture, mode: str, level: int) -> None:
    caplog.set_level(logging.DEBUG)
    _gate(mode).startup_report()
    records = _gate_records(caplog)
    assert [(r.levelno, f"NOUS_OWNER_ENTRY_MODE={mode}" in r.getMessage()) for r in records] == [(level, True)]


async def test_a_device_token_is_not_a_proof_here() -> None:
    class Devices:
        def __init__(self) -> None:
            self.asked = 0

        async def verify(self, token: str) -> bool:
            self.asked += 1
            return True

    devices = Devices()
    calls: list[str] = []
    for mode in ("enforce", "warn"):
        gate = _gate(mode, devices=devices)
        response = await _send(
            _app(gate, calls), "POST", json={}, headers={"X-Nous-Device-Token": "t" * 40, **JSON_TYPE}
        )
        assert response.status_code == (403 if mode == "enforce" else 200)
        if mode == "warn":
            assert sum(gate.drain_counts().values()) == 1  # counted as a call without proof
    assert devices.asked == 0


async def test_the_mcp_secret_is_not_a_proof_on_the_chat_routes() -> None:
    calls: list[str] = []
    gate = _gate("enforce")
    for headers in ({"X-Nous-Mcp-Secret": MCP_VALUE}, {"Authorization": f"Bearer {MCP_VALUE}"}):
        response = await _send(_app(gate, calls), "POST", json={}, headers=headers)
        assert response.status_code == 403
    assert calls == []


# ---- warn and off ------------------------------------------------------------------------------------------------


async def test_warn_logs_and_counts_a_call_without_proof_by_its_class(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    calls: list[str] = []
    gate = _gate("warn")
    response = await _send(
        _app(gate, calls),
        "POST",
        json={"session_id": SESSION_ID, "message": MESSAGE},
        headers={"X-Nous-Owner-Secret": WRONG_VALUE},
    )
    assert (response.status_code, response.json(), calls) == (200, {"ok": True}, ["POST"])
    lines = _unproven_lines(caplog)
    assert len(lines) == 1
    assert lines == ["owner entry: POST /chat carried no proof (a wrong secret; session class claude)"]
    counts = gate.drain_counts()
    assert counts == {"claude": 1}
    for record in caplog.records:
        text = record.getMessage() + repr(record.args)
        for value in (SESSION_ID, "abc-private", MESSAGE, WRONG_VALUE):
            assert value not in text
    assert SESSION_ID not in repr(counts)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"session_id": "pr-review-897"}, "pr-review"),
        ({"session_id": "claude-job-1"}, "claude"),
        ({"session_id": "subtask-1"}, "background"),
        ({"session_id": "my-chat"}, "other"),
        ({}, "none"),
    ],
)
async def test_warn_counts_each_call_under_its_session_class(
    caplog: pytest.LogCaptureFixture, body: dict, expected: str
) -> None:
    caplog.set_level(logging.DEBUG)
    gate = _gate("warn")
    response = await _send(_app(gate, []), "POST", json=body)
    assert response.status_code == 200
    assert gate.drain_counts() == {expected: 1}
    assert [f"session class {expected}" in line for line in _unproven_lines(caplog)] == [True]


async def test_warn_counts_a_call_whose_body_is_not_json_as_class_none(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    calls: list[str] = []
    gate = _gate("warn")
    response = await _send(_app(gate, calls), "POST", content=b"\xff")
    assert response.status_code == 400  # the handler's own answer
    assert calls == ["POST"]
    assert gate.drain_counts() == {"none": 1}
    assert len(_unproven_lines(caplog)) == 1


async def test_warn_records_exactly_one_line_per_call_even_when_the_class_is_noted_twice(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    gate = _gate("warn")

    async def handler(request: Request) -> JSONResponse:
        gate.note_session(request, "subtask-1")
        gate.note_session(request, "claude-1")
        return JSONResponse({"ok": True})

    app = Starlette(routes=[gate.route("/chat", handler, methods=["POST"])])
    await _send(app, "POST", json={})
    assert gate.drain_counts() == {"claude": 1}
    assert len(_unproven_lines(caplog)) == 1


async def test_warn_records_a_call_whose_handler_raises() -> None:
    gate = _gate("warn")

    async def handler(request: Request) -> JSONResponse:
        gate.note_session(request, "pr-review-1")
        raise RuntimeError("boom")

    app = Starlette(routes=[gate.route("/chat", handler, methods=["POST"])])
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://nous") as http:
        response = await http.post("/chat", json={})
    assert response.status_code == 500
    assert gate.drain_counts() == {"pr-review": 1}


@pytest.mark.parametrize("headers", [OWNER, {**PROXY, **JSON_TYPE}], ids=["owner", "proxy-json"])
async def test_a_call_with_a_valid_proof_is_not_counted(caplog: pytest.LogCaptureFixture, headers: dict) -> None:
    caplog.set_level(logging.DEBUG)
    gate = _gate("warn")
    response = await _send(_app(gate, []), "POST", json={"session_id": "claude-1"}, headers=headers)
    assert response.status_code == 200
    assert gate.drain_counts() == {}
    assert _unproven_lines(caplog) == []


async def test_warn_counts_a_proxy_call_that_is_not_json(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    calls: list[str] = []
    gate = _gate("warn")
    response = await _send(
        _app(gate, calls), "POST", content=b'{"session_id": "x"}', headers={**PROXY, "content-type": "text/plain"}
    )
    assert response.status_code == 200
    assert calls == ["POST"]
    assert gate.drain_counts() == {"other": 1}
    assert ["not JSON" in line for line in _unproven_lines(caplog)] == [True]


async def test_off_checks_nothing_and_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    calls: list[str] = []
    gate = _gate("off")
    for headers in ({}, {"X-Nous-Owner-Secret": WRONG_VALUE}, {**PROXY, "content-type": "text/plain"}):
        response = await _send(_app(gate, calls), "POST", content=b'{"session_id": "claude-1"}', headers=headers)
        assert response.status_code == 200
    assert calls == ["POST"] * 3
    assert gate.drain_counts() == {}
    assert _gate_records(caplog) == []


async def test_note_session_outside_a_pending_call_does_nothing() -> None:
    gate = _gate("warn")
    calls: list[str] = []
    await _send(_app(gate, calls), "POST", json={"session_id": "claude-1"}, headers=OWNER)
    request = Request({"type": "http", "method": "POST", "path": "/x", "headers": []})
    gate.note_session(request, "claude-2")  # no gate ran on this request
    assert gate.drain_counts() == {}


# ---- the mode setting --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["enfroce", "", 5, None, "WARN"])
async def test_a_mode_outside_the_three_is_enforce_on_a_namespace(mode: object) -> None:
    gate = _gate(mode)
    assert gate.mode == "enforce"
    calls: list[str] = []
    response = await _send(_app(gate, calls), "POST", json={})
    assert response.status_code == 403
    assert calls == []


def test_a_mode_missing_from_the_settings_is_warn() -> None:
    settings = SimpleNamespace(owner_route_secret_sha256=OWNER_DIGEST)
    assert EntryGate(settings, OwnerGuard(settings)).mode == "warn"


def test_a_typo_in_the_mode_fails_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    from nous.config import Settings

    with pytest.raises(ValidationError):
        Settings(_env_file=None, owner_entry_mode="enfroce")
    monkeypatch.setenv("NOUS_OWNER_ENTRY_MODE", "enfroce")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
    monkeypatch.setenv("NOUS_OWNER_ENTRY_MODE", "enforce")
    assert Settings(_env_file=None).owner_entry_mode == "enforce"


def test_the_code_default_is_warn(monkeypatch: pytest.MonkeyPatch) -> None:
    from nous.config import Settings

    monkeypatch.delenv("NOUS_OWNER_ENTRY_MODE", raising=False)
    assert Settings(_env_file=None).owner_entry_mode == "warn"
    assert Settings(_env_file=None).mcp_server_secret_sha256 == ""


# ---- session class -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("session_id", "expected"),
    [
        (None, "none"),
        ("", "none"),
        ("claude-x", "claude"),
        ("pr-review-897", "pr-review"),
        ("subtask-1", "background"),
        ("dag-summary-2", "background"),
        ("heartbeat-3", "background"),
        ("abc", "other"),
        # Hostile or look-alike ids
        ("claude", "other"),
        ("Claude-x", "other"),
        (" claude-x", "other"),
        ("\x00claude-x", "other"),
        ("pr-review", "other"),
        ("pr_review-1", "other"),
        ("pr-review-claude-1", "pr-review"),
        ("claude-pr-review-1", "claude"),
        ("claude-\n\r\x1b[31mred", "claude"),
        ("x" * 1_000_000, "other"),
        ("claude-" + "x" * 1_000_000, "claude"),
        (12, "other"),
        (0, "other"),
        (False, "other"),
        (["claude-x"], "other"),
        ({"id": "claude-x"}, "other"),
    ],
    ids=lambda value: repr(value)[:30],
)
def test_session_class(session_id: object, expected: str) -> None:
    assert session_class(session_id) == expected
    assert expected in SESSION_CLASSES


async def test_a_hostile_session_id_never_reaches_the_log(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    hostile = "claude-\nINJECTED line\x1b[31m" + "y" * 5000
    gate = _gate("warn")
    await _send(_app(gate, []), "POST", json={"session_id": hostile})
    assert gate.drain_counts() == {"claude": 1}
    for record in caplog.records:
        text = record.getMessage() + repr(record.args)
        assert "INJECTED" not in text
        assert "yyyy" not in text


# ---- the log throttle --------------------------------------------------------------------------------------------


async def test_the_log_is_throttled_per_route_presentation_and_class(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    clock = _Clock()
    gate = _gate("warn", clock=clock)
    app = _app(gate, [])
    for _ in range(3):
        await _send(app, "POST", json={"session_id": "claude-1"})
    assert len(_unproven_lines(caplog)) == 1  # one key: one line per ten minutes
    await _send(app, "POST", json={"session_id": "claude-1"}, client=("10.1.2.3", 7000))  # another address: same key
    assert len(_unproven_lines(caplog)) == 1
    await _send(app, "POST", json={"session_id": "pr-review-1"})  # a new class
    await _send(app, "POST", json={"session_id": "claude-1"}, headers={"X-Nous-Owner-Secret": WRONG_VALUE})
    assert len(_unproven_lines(caplog)) == 3
    clock.now += 601
    await _send(app, "POST", json={"session_id": "claude-1"})
    assert len(_unproven_lines(caplog)) == 4
    assert gate.drain_counts() == {"claude": 6, "pr-review": 1}  # every call is counted, logged or not


ZONE_AND_TEXT = "fe80::1%eth0 INJECTED free text"
LONG = "f" * 20_000


@pytest.mark.parametrize(
    ("client", "headers"),
    [
        (("127.0.0.1", 5000), {"X-Forwarded-For": ZONE_AND_TEXT}),
        (("127.0.0.1", 5000), {"X-Forwarded-For": LONG, "X-Real-IP": ZONE_AND_TEXT}),
        ((ZONE_AND_TEXT, 1), {}),
        ((LONG, 1), {"Forwarded": f"for={LONG}"}),
        (("10.9.8.7", 1), {}),
    ],
    ids=["forwarded-zone-and-text", "forwarded-long", "client-zone-and-text", "client-long", "another-ip"],
)
async def test_the_client_address_never_reaches_the_log(
    caplog: pytest.LogCaptureFixture, client: tuple[str, int], headers: dict[str, str]
) -> None:
    caplog.set_level(logging.DEBUG)
    plain = _gate("warn")
    await _send(_app(plain, []), "POST", json={"session_id": "claude-1"})
    expected = _unproven_lines(caplog)
    caplog.clear()
    gate = _gate("warn")
    await _send(_app(gate, []), "POST", json={"session_id": "claude-1"}, client=client, headers=headers)
    assert (
        _unproven_lines(caplog)
        == expected
        == ["owner entry: POST /chat carried no proof (no secret header; session class claude)"]
    )
    for record in caplog.records:
        text = record.getMessage() + repr(record.args)
        for value in ("fe80", "INJECTED", "free text", "ffffffff", "10.9.8.7", "127.0.0.1"):
            assert value not in text, (record.name, value)


def _method_names(count: int) -> list[str]:
    """``count`` distinct method names, none of them a common HTTP method."""
    names = []
    for i in range(count):
        word, n = "", i
        for _ in range(3):
            word, n = chr(ord("A") + n % 26) + word, n // 26
        names.append("X" + word)
    return names


async def test_a_method_outside_the_common_seven_is_logged_and_keyed_as_a_question_mark(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    gate = _gate("warn", clock=_Clock())
    app = Starlette()
    app.mount("/mcp", gate.asgi(_Inner(), name="/mcp"))
    names = [*_method_names(48), "INJECTEDLINE", "P" * 200]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://nous") as http:
        for method in names:
            await http.request(method, "/mcp/")
    assert len(names) == 50
    assert _unproven_lines(caplog) == ["owner entry: ? /mcp carried no proof (no secret header; session class none)"]
    assert list(gate._last_logged) == [("?", "/mcp", "no secret header", "none")]
    assert gate.drain_counts() == {"none": 50}  # every call is still counted
    for record in (r for r in caplog.records if r.name != "httpx"):  # the test client logs its own request line
        text = record.getMessage() + repr(record.args)
        assert "XAA" not in text and "INJECTED" not in text and "PPPP" not in text


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
async def test_each_common_method_is_logged_as_it_is(caplog: pytest.LogCaptureFixture, method: str) -> None:
    caplog.set_level(logging.DEBUG)
    gate = _gate("warn")
    app = Starlette()
    app.mount("/mcp", gate.asgi(_Inner(), name="/mcp"))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://nous") as http:
        await http.request(method, "/mcp/")
    assert _unproven_lines(caplog) == [
        f"owner entry: {method} /mcp carried no proof (no secret header; session class none)"
    ]


# ---- the MCP wrapper ---------------------------------------------------------------------------------------------


class _Inner:
    """An ASGI app that records each scope type it was given and answers 200."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    async def __call__(self, scope, receive, send) -> None:
        self.seen.append(scope["type"])
        if scope["type"] == "http":
            await JSONResponse({"inner": True})(scope, receive, send)


async def _mcp(gate: EntryGate, inner: _Inner, headers: dict[str, str], **kw) -> httpx.Response:
    app = Starlette()
    app.mount("/mcp", gate.asgi(inner, name="/mcp"))
    return await _send(app, "POST", "/mcp/", json={"jsonrpc": "2.0"}, headers=headers, **kw)


@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": f"Bearer {MCP_VALUE}"},
        {"Authorization": f"bearer {MCP_VALUE}"},
        {"X-Nous-Mcp-Secret": MCP_VALUE},
        OWNER,
    ],
    ids=["bearer", "bearer-lowercase-scheme", "x-header", "owner"],
)
async def test_the_asgi_wrapper_accepts_the_mcp_key_and_the_owner_secret(headers: dict[str, str]) -> None:
    inner = _Inner()
    response = await _mcp(_gate("enforce"), inner, headers)
    assert (response.status_code, response.json(), inner.seen) == (200, {"inner": True}, ["http"])


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": f"Bearer {WRONG_VALUE}"},
        {"Authorization": f"Bearer {OWNER_VALUE}"},
        {"Authorization": f"Basic {MCP_VALUE}"},
        {"Authorization": MCP_VALUE},
        {"Authorization": "Bearer "},
        {"X-Nous-Mcp-Secret": ""},
        {"X-Nous-Mcp-Secret": OWNER_VALUE},
        {**PROXY, **JSON_TYPE},
        {**PROXY, "content-type": "text/plain"},
        {"X-Nous-Owner-Secret": MCP_VALUE},
    ],
    ids=[
        "none",
        "bearer-wrong",
        "bearer-owner-value",
        "basic",
        "no-scheme",
        "bearer-empty",
        "x-header-empty",
        "x-header-owner-value",
        "proxy-json",
        "proxy-text",
        "mcp-value-in-owner-header",
    ],
)
async def test_the_asgi_wrapper_refuses_the_rest_before_the_inner_app(headers: dict[str, str]) -> None:
    inner = _Inner()
    response = await _mcp(_gate("enforce"), inner, headers)
    assert (response.status_code, response.json(), inner.seen) == (403, FORBIDDEN, [])


async def test_the_asgi_wrapper_in_warn_counts_and_in_off_logs_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    inner = _Inner()
    warn = _gate("warn")
    assert (await _mcp(warn, inner, {})).status_code == 200
    assert (await _mcp(warn, inner, {"Authorization": f"Bearer {MCP_VALUE}"})).status_code == 200
    assert warn.drain_counts() == {"none": 1}
    lines = _unproven_lines(caplog)
    assert len(lines) == 1 and "POST /mcp" in lines[0]
    caplog.clear()
    off = _gate("off")
    assert (await _mcp(off, inner, {})).status_code == 200
    assert off.drain_counts() == {}
    assert _gate_records(caplog) == []
    assert inner.seen == ["http"] * 3


async def test_with_no_mcp_digest_the_mcp_key_is_refused_and_the_owner_still_passes() -> None:
    inner = _Inner()
    gate = _gate("enforce", mcp=False)
    assert (await _mcp(gate, inner, {"Authorization": f"Bearer {MCP_VALUE}"})).status_code == 403
    assert (await _mcp(gate, inner, OWNER)).status_code == 200


@pytest.mark.parametrize(
    "headers",
    [
        {},
        OWNER,
        {"Authorization": f"Bearer {MCP_VALUE}"},
        {"X-Nous-Mcp-Secret": MCP_VALUE},
        {**PROXY, **JSON_TYPE},
    ],
    ids=["none", "owner", "bearer", "x-header", "proxy-json"],
)
async def test_with_neither_the_mcp_nor_the_owner_digest_the_mcp_server_refuses_every_call(
    headers: dict[str, str],
) -> None:
    inner = _Inner()
    response = await _mcp(_gate("enforce", owner=False, mcp=False), inner, headers)
    assert (response.status_code, inner.seen) == (403, [])


async def test_the_asgi_wrapper_passes_lifespan_through_and_gates_a_websocket() -> None:
    inner = _Inner()
    gated = _gate("enforce").asgi(inner, name="/mcp")
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "lifespan.startup"}

    async def send(message: dict) -> None:
        sent.append(message)

    await gated({"type": "lifespan", "asgi": {"version": "3.0"}}, receive, send)
    assert inner.seen == ["lifespan"]
    websocket = {"type": "websocket", "path": "/mcp/", "headers": [], "client": ("127.0.0.1", 1)}
    await gated(websocket, receive, send)
    assert inner.seen == ["lifespan"]  # not passed through without a proof
    assert sent[-1]["type"] == "websocket.close"
    with_key = {**websocket, "headers": [(b"x-nous-mcp-secret", MCP_VALUE.encode())]}
    await gated(with_key, receive, send)
    assert inner.seen == ["lifespan", "websocket"]


# ---- the daily notice --------------------------------------------------------------------------------------------


class _Turns:
    """A stand-in for asyncio.sleep: each loop turn waits until the test releases it."""

    def __init__(self) -> None:
        self.sleeps = 0
        self.seconds: list[float] = []
        self._release: asyncio.Queue[None] = asyncio.Queue()
        self._slept = asyncio.Event()

    async def __call__(self, seconds: float) -> None:
        self.sleeps += 1
        self.seconds.append(seconds)
        self._slept.set()
        await self._release.get()

    async def _sleeps_past(self, count: int) -> None:
        while self.sleeps <= count:
            self._slept.clear()
            await asyncio.wait_for(self._slept.wait(), timeout=5)

    async def turn(self) -> None:
        """Let the loop run one turn, and return once it is asleep again."""
        await self._sleeps_past(0)
        before = self.sleeps
        self._release.put_nowait(None)
        await self._sleeps_past(before)


async def test_the_daily_notice_is_sent_once_with_the_count_only_in_warn_and_only_above_zero() -> None:
    sent: list[str] = []
    fail_next = False

    async def send(text: str) -> bool:
        if fail_next:
            raise RuntimeError("telegram is down")
        sent.append(text)
        return True

    gate = _gate("warn")
    app = _app(gate, [])
    turns = _Turns()
    task = asyncio.create_task(gate.daily_notice_loop(send, sleep=turns))
    try:
        for session in ("claude-1", "claude-2", "my-chat"):
            await _send(app, "POST", json={"session_id": session})
        await turns.turn()
        assert len(sent) == 1
        assert "3 calls" in sent[0]
        assert "claude: 2" in sent[0] and "other: 1" in sent[0]
        assert "NOUS_OWNER_ENTRY_MODE=enforce" in sent[0]
        assert "claude-1" not in sent[0] and "my-chat" not in sent[0]
        await turns.turn()  # nothing new: nothing sent
        assert len(sent) == 1
        fail_next = True
        await _send(app, "POST", json={})
        await turns.turn()  # the send fails; the loop goes on and keeps the count
        fail_next = False
        await _send(app, "POST", json={})
        await turns.turn()
        assert len(sent) == 2 and "2 calls" in sent[1]  # the call of the failed turn and this one
        assert not task.done()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.parametrize("mode", ["enforce", "off"])
async def test_the_daily_notice_loop_does_nothing_outside_warn(mode: str) -> None:
    async def send(text: str) -> bool:
        raise AssertionError("sent outside warn")

    async def sleep(_seconds: float) -> None:
        raise AssertionError("slept outside warn")

    await asyncio.wait_for(_gate(mode).daily_notice_loop(send, sleep=sleep), timeout=1)


async def test_a_failed_send_is_logged_by_class_only(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    answers: list[object] = [RuntimeError(f"failed with {OWNER_VALUE}"), False]

    async def send(text: str) -> bool:
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    gate = _gate("warn")
    app = _app(gate, [])
    turns = _Turns()
    task = asyncio.create_task(gate.daily_notice_loop(send, sleep=turns))
    try:
        await _send(app, "POST", json={})
        await turns.turn()
        await _send(app, "POST", json={})
        await turns.turn()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert answers == []
    assert gate.drain_counts() == {"none": 2}  # neither line went out: both calls are kept for the next one
    notices = [r.getMessage() for r in _gate_records(caplog) if "notice" in r.getMessage()]
    assert len(notices) == 2
    assert "RuntimeError" in notices[0]
    assert not any(OWNER_VALUE in m for m in notices)
    assert turns.seconds[0] == 86400


async def test_the_pending_count_is_sent_once_at_a_graceful_shutdown() -> None:
    sent: list[str] = []

    async def send(text: str) -> bool:
        sent.append(text)
        return True

    gate = _gate("warn")
    app = _app(gate, [])
    await gate.notice_at_shutdown(send)
    assert sent == []  # nothing pending: nothing sent
    for session in ("claude-1", "my-chat"):
        await _send(app, "POST", json={"session_id": session})
    await gate.notice_at_shutdown(send)
    assert len(sent) == 1
    assert "2 calls" in sent[0] and "claude: 1" in sent[0] and "other: 1" in sent[0]
    assert "the record" in sent[0]
    await gate.notice_at_shutdown(send)
    assert len(sent) == 1  # sent once


async def test_a_daily_line_cancelled_in_flight_leaves_its_count_for_the_notice_at_shutdown() -> None:
    """A shutdown while the daily line is being sent: the task is cancelled mid-send, then the shutdown notice runs."""
    in_flight = asyncio.Event()
    never = asyncio.Event()
    sent: list[str] = []

    async def daily_send(text: str) -> bool:
        in_flight.set()
        await never.wait()
        return True

    async def shutdown_send(text: str) -> bool:
        sent.append(text)
        return True

    gate = _gate("warn")
    app = _app(gate, [])
    for session in ("claude-1", "claude-2", "my-chat"):
        await _send(app, "POST", json={"session_id": session})

    async def no_wait(_seconds: float) -> None:
        return None

    task = asyncio.create_task(gate.daily_notice_loop(daily_send, sleep=no_wait))
    await asyncio.wait_for(in_flight.wait(), timeout=5)
    await _send(app, "POST", json={"session_id": "pr-review-1"})  # a call while the line is in flight
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await gate.notice_at_shutdown(shutdown_send)
    assert len(sent) == 1
    assert "4 calls" in sent[0] and "claude: 2" in sent[0] and "pr-review: 1" in sent[0] and "other: 1" in sent[0]


async def test_a_call_counted_while_a_line_is_in_flight_stays_for_the_next_line() -> None:
    release = asyncio.Event()
    sent: list[str] = []
    gate = _gate("warn")
    app = _app(gate, [])

    async def send(text: str) -> bool:
        sent.append(text)
        if len(sent) == 1:
            await release.wait()
        return True

    await _send(app, "POST", json={"session_id": "claude-1"})
    first = asyncio.create_task(gate.notice_at_shutdown(send))
    await asyncio.sleep(0)
    await _send(app, "POST", json={"session_id": "my-chat"})
    release.set()
    await first
    assert "1 call " in sent[0] and "claude: 1" in sent[0]
    assert gate.drain_counts() == {"other": 1}


@pytest.mark.parametrize("mode", ["off", "enforce"])
async def test_no_notice_at_shutdown_outside_warn(mode: str) -> None:
    async def send(text: str) -> bool:
        raise AssertionError("sent outside warn")

    gate = _gate(mode)
    gate._counts["claude"] += 3  # whatever is pending
    await gate.notice_at_shutdown(send)


async def test_a_notice_at_shutdown_that_hangs_or_fails_is_bounded_and_logged_by_class_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    never = asyncio.Event()

    async def hangs(text: str) -> bool:
        await never.wait()
        return True

    async def fails(text: str) -> bool:
        raise RuntimeError(f"failed with {OWNER_VALUE}")

    gate = _gate("warn")
    app = _app(gate, [])
    await _send(app, "POST", json={})
    started = asyncio.get_running_loop().time()
    # Bounded here too, so a notice that waits for its send fails this test instead of hanging the run.
    await asyncio.wait_for(gate.notice_at_shutdown(hangs, timeout=0.05), timeout=5)
    assert asyncio.get_running_loop().time() - started < 2
    await _send(app, "POST", json={})
    await gate.notice_at_shutdown(fails)
    lines = [r.getMessage() for r in _gate_records(caplog) if "shutdown" in r.getMessage()]
    assert len(lines) == 2
    assert "TimeoutError" in lines[0] and "RuntimeError" in lines[1]
    assert not any(OWNER_VALUE in line for line in lines)


def test_the_shutdown_notice_waits_about_five_seconds_by_default() -> None:
    import inspect

    from nous.api import entry_gate

    assert entry_gate.NOTICE_AT_SHUTDOWN_SECONDS == 5.0
    default = inspect.signature(EntryGate.notice_at_shutdown).parameters["timeout"].default
    assert default == entry_gate.NOTICE_AT_SHUTDOWN_SECONDS


# ---- what is logged, and the markers ----------------------------------------------------------------------------


async def test_nothing_in_the_log_holds_a_secret_at_debug(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    wrong = {
        "X-Nous-Owner-Secret": WRONG_VALUE,
        "X-Nous-Proxy-Secret": WRONG_VALUE + "-p",
        "X-Nous-Mcp-Secret": WRONG_VALUE + "-m",
        "Authorization": f"Bearer {WRONG_VALUE}-b",
        "User-Agent": "agent-string-test-value",
    }
    body = {"session_id": SESSION_ID, "message": MESSAGE}
    for mode in ("warn", "enforce"):
        gate = _gate(mode)
        gate.startup_report()
        app = _app(gate, [])
        for headers in (wrong, OWNER, {**PROXY, **JSON_TYPE}, {**PROXY, "content-type": "text/plain"}):
            await _send(app, "POST", json=body, headers=headers)
        inner = _Inner()
        for headers in (wrong, {"Authorization": f"Bearer {MCP_VALUE}"}, {"X-Nous-Mcp-Secret": MCP_VALUE}):
            await _mcp(gate, inner, headers)
    assert _gate_records(caplog)  # something was logged
    secrets = (OWNER_VALUE, PROXY_VALUE, MCP_VALUE, OWNER_DIGEST, PROXY_DIGEST, MCP_DIGEST, WRONG_VALUE)
    for record in caplog.records:
        text = record.getMessage() + repr(record.args)
        for value in (*secrets, SESSION_ID, MESSAGE, "agent-string-test-value"):
            assert value not in text, (record.name, value)


def test_the_wrappers_are_marked() -> None:
    gate = _gate("warn")

    async def handler(request: Request) -> JSONResponse:
        return JSONResponse({})

    assert gate.protect(handler, "/chat").entry_gated is True
    assert gate.route("/chat", handler, methods=["POST"]).endpoint.entry_gated is True
    assert gate.asgi(_Inner(), name="/mcp").entry_gated is True
