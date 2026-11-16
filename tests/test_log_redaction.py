"""Credentials never reach a log line.

Two libraries write them to their own loggers. The Bot API carries the bot
token in the URL path, and the HTTP client logs every request URL at INFO. The
HTTP/2 header encoder logs every header it encodes at DEBUG, the model API's
key among them.

The first tests send a real client request (over a mock transport, no network)
and log real client errors through a root handler that has the redaction
installed. The last ones run the two real entry points, each in a fresh
interpreter. Every token and key here is made up.
"""

from __future__ import annotations

import contextlib
import io
import logging
import os
import re
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from nous.log_redaction import _RedactingFormatter, install_log_redaction, redact

REPO = Path(__file__).resolve().parents[1]
# Made up, and in two parts: no line of this file has the shape of a real token.
BOT_ID = "1234567890"
SECRET = "AAFakeFakeFakeFakeFakeFakeFakeFake_-x"
TOKEN = f"{BOT_ID}:{SECRET}"
API_KEY = "made-up-api-key-0123456789"
URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
REDACTED_URL = "https://api.telegram.org/bot<redacted>/sendMessage"
CLIENT_ERROR = "Client error '401 Unauthorized' for url '{}'"


@contextlib.contextmanager
def _on_the_root_logger(*handlers: logging.Handler) -> Iterator[None]:
    """Add ``handlers`` to the root logger for the length of the block.

    pytest keeps handlers of its own on the root logger for the whole session,
    and installing the redaction wraps those too. Their formatters are put back
    afterwards, so no later test reads a redacted ``caplog.text``.
    """
    root = logging.getLogger()
    before = [(handler, handler.formatter) for handler in root.handlers]
    for handler in handlers:
        root.addHandler(handler)
    try:
        yield
    finally:
        for handler in handlers:
            root.removeHandler(handler)
        for handler, formatter in before:
            handler.setFormatter(formatter)


@pytest.fixture
def emitted(caplog):
    """What a root handler formatted like the entry points' writes, once the redaction is installed."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    with _on_the_root_logger(handler), caplog.at_level(logging.INFO):
        install_log_redaction()
        yield stream


async def _post(status: int = 200) -> httpx.Response:
    transport = httpx.MockTransport(lambda request: httpx.Response(status, json={"ok": status < 400}))
    async with httpx.AsyncClient(transport=transport) as client:
        return await client.post(URL, json={"chat_id": 1, "text": "x"}, timeout=10)


async def test_the_http_clients_request_line_is_written_without_the_token(emitted):
    await _post()

    out = emitted.getvalue()
    assert 'HTTP Request: POST https://api.telegram.org/bot<redacted>/sendMessage "HTTP/1.1 200 OK"' in out
    assert SECRET not in out


async def test_a_logged_client_error_and_its_traceback_are_written_without_the_token(emitted):
    response = await _post(401)
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        logging.getLogger("nous.telegram_bot").error("Polling error: %s", exc)
        logging.getLogger("nous.telegram_bot").exception("photo download failed")

    out = emitted.getvalue()
    assert out.count("bot<redacted>/sendMessage") >= 3  # the request line, the message, the traceback
    assert "Traceback (most recent call last)" in out
    assert SECRET not in out


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (URL, REDACTED_URL),
        (
            f"https://api.telegram.org/file/bot{TOKEN}/photos/file_1.jpg",
            "https://api.telegram.org/file/bot<redacted>/photos/file_1.jpg",
        ),
        (
            CLIENT_ERROR.format(f"https://api.telegram.org/bot{TOKEN}/getUpdates?offset=5&timeout=30"),
            CLIENT_ERROR.format("https://api.telegram.org/bot<redacted>/getUpdates?offset=5&timeout=30"),
        ),
        (
            f"http://bot-api.internal:8081/bot{TOKEN}/sendMessage",
            "http://bot-api.internal:8081/bot<redacted>/sendMessage",
        ),
        (f"{URL} then {URL}", f"{REDACTED_URL} then {REDACTED_URL}"),
        (f"https://api.telegram.org/bot{BOT_ID}:{SECRET[:12]}", "https://api.telegram.org/bot<redacted>"),
    ],
    ids=["send", "file-download", "client-error-text", "another-host", "twice-in-one-line", "cut-short"],
)
def test_redact_removes_a_token_shaped_path_segment(text, expected):
    assert redact(text) == expected


@pytest.mark.parametrize("bot_id", ["12345678", "123456789", "1234567890"])
def test_a_bot_id_of_any_length_in_use_is_redacted(bot_id):
    assert (
        redact(f"https://api.telegram.org/bot{bot_id}:{SECRET}/getMe") == "https://api.telegram.org/bot<redacted>/getMe"
    )


@pytest.mark.parametrize(
    "text",
    [
        "Starting Nous agent: nous (nous-default)",
        'HTTP Request: POST https://api.anthropic.com/v1/messages "HTTP/1.1 200 OK"',
        "GET /dashboard/v2/bots/12 200",
        "check 'dag-1a2b3c4d-wait' disabled at 12:30:05",
        "/bot without a token after it",
        # Look-alikes: a tag, a port and a time after "/bot<digits>:", and the shape without its slash.
        "docker pull registry.local/team/bot3:latest",
        "docker pull registry.local/team/bot42:release-candidate-2",
        "GET http://proxy.internal/bot12345:8080/health",
        "C:/bot12:30:05/file",
        "pulled registry.local/team-chatbot20260101:release-candidate-2",
    ],
)
def test_a_line_without_a_token_is_written_unchanged(text):
    assert redact(text) == text


async def test_every_other_part_of_the_line_is_kept(emitted):
    logging.getLogger("nous.main").info("Model: %s", "claude-test")
    await _post()

    # Without the timestamp, which is two words.
    lines = [line.split(" ", 2)[-1] for line in emitted.getvalue().splitlines()]
    assert "nous.main INFO Model: claude-test" in lines
    assert f'httpx INFO HTTP Request: POST {REDACTED_URL} "HTTP/1.1 200 OK"' in lines


def test_installing_twice_wraps_a_handler_once(emitted):
    handler = next(h for h in logging.getLogger().handlers if getattr(h, "stream", None) is emitted)
    wrapped = handler.formatter

    install_log_redaction()

    assert handler.formatter is wrapped
    logging.getLogger("nous.main").info("to %s", URL)
    assert emitted.getvalue().count("bot<redacted>") == 1


def test_every_root_handler_is_wrapped_and_one_without_a_formatter_still_writes(caplog):
    first, second = io.StringIO(), io.StringIO()
    formatted = logging.StreamHandler(first)
    formatted.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    bare = logging.StreamHandler(second)  # no formatter of its own: logging's default applies

    with _on_the_root_logger(formatted, bare), caplog.at_level(logging.INFO):
        install_log_redaction()
        logging.getLogger("nous.main").info("to %s", URL)

    assert first.getvalue() == f"INFO to {REDACTED_URL}\n"
    assert second.getvalue() == f"to {REDACTED_URL}\n"


def test_no_root_handler_is_left_wrapped_after_a_test():
    root = logging.getLogger()
    assert root.handlers, "pytest has no handler on the root logger: nothing to check"

    with _on_the_root_logger(logging.StreamHandler(io.StringIO())):
        install_log_redaction()
        assert all(isinstance(handler.formatter, _RedactingFormatter) for handler in root.handlers)

    # Whatever ran before this test: a wrapper left behind by an earlier one shows here too.
    assert not any(isinstance(handler.formatter, _RedactingFormatter) for handler in root.handlers)


def test_the_root_logger_is_configured_in_one_place():
    configure = "logging.basicConfig("
    configured = sorted(
        path.relative_to(REPO).as_posix()
        for path in (REPO / "nous").rglob("*.py")
        if configure in path.read_text(encoding="utf-8")
    )

    assert configured == ["nous/log_redaction.py"], "call nous.log_redaction.configure_logging instead"


def _run_entry_point(script: str, cwd: Path, **env: str) -> subprocess.CompletedProcess[str]:
    """Run ``script`` in a fresh interpreter and return what it wrote.

    ``logging.basicConfig`` only acts on a root logger that has no handler, and
    pytest's has some, so an entry point's logging setup cannot run in this
    process. The working directory is empty: no ``.env`` is read.
    """
    return subprocess.run(
        [sys.executable, "-X", "utf8", "-c", textwrap.dedent(script)],
        cwd=cwd,
        env={**os.environ, "PYTHONPATH": str(REPO), **env},
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


BOT_ENTRY_POINT = """
    import asyncio
    import logging

    import httpx

    import nous.telegram_bot as bot


    class Stop(BaseException):
        "Ends the polling loop, which carries on after an Exception."


    polls = []


    def answer(request):
        if request.url.path.endswith("/getMe"):
            return httpx.Response(200, json={"ok": True, "result": {"id": 1, "username": "made_up_bot"}})
        polls.append(request)
        logging.getLogger("nous.made_up").debug("a debug line")
        if len(polls) > 2:
            raise Stop
        return httpx.Response(200, json={"ok": True, "result": []})


    class Client(httpx.AsyncClient):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(answer), **kwargs)


    httpx.AsyncClient = Client
    try:
        asyncio.run(bot.main())
    except Stop:
        pass

    from nous.log_redaction import HTTP_CLIENT_LOGGERS, RedactingFilter

    print("FILTERED", all(
        any(isinstance(f, RedactingFilter) for f in logging.getLogger(name).filters) for name in HTTP_CLIENT_LOGGERS
    ))
"""


def test_the_bot_entry_point_writes_no_token(tmp_path):
    proc = _run_entry_point(BOT_ENTRY_POINT, tmp_path, TELEGRAM_BOT_TOKEN=TOKEN)

    assert proc.returncode == 0, proc.stderr
    requests = [line for line in proc.stderr.splitlines() if " httpx INFO HTTP Request: " in line]
    assert len(requests) == 3  # getMe and two polls
    assert all("GET https://api.telegram.org/bot<redacted>/get" in line for line in requests)
    assert "FILTERED True" in proc.stdout
    assert " DEBUG " not in proc.stderr  # the bot logs at INFO
    assert SECRET not in proc.stderr + proc.stdout


SERVER_ENTRY_POINT = """
    import logging
    import os

    import hpack
    import httpx
    import uvicorn

    import nous.main as server


    async def app(scope, receive, send):
        "Never served."


    def run(app, host, port, log_level):
        # What uvicorn.run does first: its Config sets up uvicorn's own logging.
        uvicorn.Config(app, host=host, port=port, log_level=log_level)
        # What a running server then does: a Telegram call, and a model call's headers over HTTP/2.
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))
        with httpx.Client(transport=transport) as client:
            client.post("https://api.telegram.org/bot" + os.environ["MADE_UP_BOT_TOKEN"] + "/sendMessage")
        encoder = hpack.Encoder()
        encoder.encode([(b"x-api-key", os.environ["MADE_UP_API_KEY"].encode())])
        encoder.header_table_size = 1  # evicts the header: the encoder's second logger writes it too
        logging.getLogger("nous.made_up").debug("a debug line")
        from nous.log_redaction import HTTP_CLIENT_LOGGERS, RedactingFilter

        print("FILTERED", all(
            any(isinstance(f, RedactingFilter) for f in logging.getLogger(name).filters) for name in HTTP_CLIENT_LOGGERS
        ))


    server.build_app = lambda settings: app
    uvicorn.run = run
    server.main()
"""


@pytest.mark.parametrize("level", ["info", "debug"])
def test_the_server_entry_point_writes_no_token_and_no_api_key(level, tmp_path):
    proc = _run_entry_point(
        SERVER_ENTRY_POINT, tmp_path, NOUS_LOG_LEVEL=level, MADE_UP_BOT_TOKEN=TOKEN, MADE_UP_API_KEY=API_KEY
    )

    assert proc.returncode == 0, proc.stderr
    assert " nous.main INFO Starting Nous agent: " in proc.stderr  # logging is set up before the first line
    requests = [line for line in proc.stderr.splitlines() if " httpx INFO HTTP Request: " in line]
    assert len(requests) == 1
    assert f'POST {REDACTED_URL} "HTTP/1.1 200 OK"' in requests[0]
    assert "FILTERED True" in proc.stdout
    assert (" nous.made_up DEBUG a debug line" in proc.stderr) == (level == "debug")
    written = proc.stderr + proc.stdout
    assert SECRET not in written
    assert API_KEY not in written


SCORER_ENTRY_POINT = """
    import logging
    import sys

    import nous.observability.task_score_job as job
    from nous.log_redaction import HTTP_CLIENT_LOGGERS, RedactingFilter


    async def run(agent_id, window_days):
        "Stands in for the scoring pass, which runs once the logging is set up."
        print("FILTERED", all(
            any(isinstance(f, RedactingFilter) for f in logging.getLogger(name).filters) for name in HTTP_CLIENT_LOGGERS
        ))
        return 0


    job._run = run
    sys.exit(job.main([]))
"""


def test_the_scorer_entry_point_installs_the_filter(tmp_path):
    proc = _run_entry_point(SCORER_ENTRY_POINT, tmp_path)

    assert proc.returncode == 0, proc.stderr
    assert "FILTERED True" in proc.stdout


@pytest.fixture
def configure(monkeypatch):
    """``configure_logging`` in this process, with the root handlers and pytest's own left alone; the HTTP client's
    loggers get their filters back as they were afterwards."""
    import nous.log_redaction as log_redaction

    names = (*log_redaction.HTTP_CLIENT_LOGGERS, "hpack")
    saved = {name: (logging.getLogger(name).level, list(logging.getLogger(name).filters)) for name in names}
    monkeypatch.setattr(log_redaction.logging, "basicConfig", lambda **kwargs: None)
    monkeypatch.setattr(log_redaction, "install_log_redaction", lambda: None)
    try:
        yield log_redaction.configure_logging
    finally:
        for name, (level, filters) in saved.items():
            client_logger = logging.getLogger(name)
            client_logger.setLevel(level)
            client_logger.filters[:] = filters


def _records(caplog, name: str = "httpx") -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == name]


async def test_a_request_line_holds_no_token_in_the_record_once_configured(configure, caplog):
    caplog.set_level(logging.DEBUG)
    await _post()
    # Without configure_logging, the request line's record holds the token.
    assert [SECRET in record.getMessage() for record in _records(caplog)] == [True]
    caplog.clear()

    configure(logging.INFO)
    await _post()

    [record] = _records(caplog)
    assert record.getMessage() == f'HTTP Request: POST {REDACTED_URL} "HTTP/1.1 200 OK"'
    assert SECRET not in str(record.msg) and SECRET not in repr(record.args)  # the record itself, not a handler's text


async def test_the_notices_sender_writes_its_request_line_with_the_token_redacted_in_the_record(
    configure, monkeypatch, caplog
):
    from types import SimpleNamespace

    import nous.main as server

    class Client(httpx.AsyncClient):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(lambda request: httpx.Response(200)), **kwargs)

    monkeypatch.setattr(server.httpx, "AsyncClient", Client)
    configure(logging.INFO)
    caplog.set_level(logging.DEBUG)
    push = server._telegram_text_push(
        SimpleNamespace(telegram_bot_token=TOKEN, telegram_chat_id="1"), raise_errors=True
    )

    assert await push("one line") is True

    [record] = _records(caplog)
    assert REDACTED_URL in record.getMessage()
    for each in caplog.records:
        assert SECRET not in str(each.msg) + repr(each.args), each.name


async def test_a_model_api_request_line_is_unchanged(configure, caplog):
    configure(logging.INFO)
    caplog.set_level(logging.DEBUG)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    async with httpx.AsyncClient(transport=transport) as client:
        await client.post("https://api.anthropic.com/v1/messages?beta=true", json={})

    [record] = _records(caplog)
    assert record.getMessage() == 'HTTP Request: POST https://api.anthropic.com/v1/messages?beta=true "HTTP/1.1 200 OK"'
    assert record.args  # left as the client wrote it


def test_every_http_client_logger_gets_the_filter_once(configure):
    from nous.log_redaction import HTTP_CLIENT_LOGGERS, RedactingFilter

    configure(logging.INFO)
    configure(logging.INFO)
    for name in HTTP_CLIENT_LOGGERS:
        assert sum(isinstance(f, RedactingFilter) for f in logging.getLogger(name).filters) == 1, name
    assert "httpx" in HTTP_CLIENT_LOGGERS and "httpcore" in HTTP_CLIENT_LOGGERS


def test_the_list_names_every_logger_the_installed_transport_creates():
    import httpcore

    from nous.log_redaction import HTTP_CLIENT_LOGGERS

    source = Path(httpcore.__file__).parent
    created = {
        match
        for path in source.rglob("*.py")
        for match in re.findall(r'getLogger\("([^"]+)"\)', path.read_text(encoding="utf-8"))
    }
    assert created  # the scan found the transport's loggers
    assert created <= set(HTTP_CLIENT_LOGGERS)


@pytest.mark.parametrize("name", ["httpcore", "httpcore.connection", "httpcore.http11", "httpcore.http2"])
def test_a_transport_debug_line_that_holds_a_token_is_redacted_in_the_record(configure, caplog, name):
    configure(logging.DEBUG)
    caplog.set_level(logging.DEBUG)
    logging.getLogger(name).debug("send_request_headers.started target=%s", URL)

    [record] = _records(caplog, name)
    assert record.getMessage() == f"send_request_headers.started target={REDACTED_URL}"
    assert SECRET not in str(record.msg) + repr(record.args)
