"""The Telegram bot sends the owner's secret when it chats, and answers a refused chat in fixed words. No database: the
bot's HTTP client is a real httpx client over a mock transport, so the calls are made as in production."""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from nous.telegram_bot import CHAT_REFUSED, NousTelegramBot

SECRET = "s" * 64
SECRET_HEADER = "x-nous-owner-secret"


class _Api:
    """The REST API as the bot reaches it: ``answers`` maps (method, path) to (status, body); each request's method,
    path and owner-secret header are recorded."""

    def __init__(self, answers: dict[tuple[str, str], tuple[int, object]] | None = None) -> None:
        self.answers = answers or {}
        self.requests: list[tuple[str, str, str | None]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.path, request.headers.get(SECRET_HEADER)))
        status, body = self.answers.get((request.method, request.url.path), (404, {"error": "no such thing"}))
        if isinstance(body, str):
            return httpx.Response(status, text=body, headers={"content-type": "text/event-stream"})
        return httpx.Response(status, json=body)


STREAM_OK = 'data: {"type": "text_delta", "text": "hello"}\n\ndata: {"type": "done"}\n\n'
CHAT_OK = {"response": "hello", "session_id": "s-1", "frame": "conversation", "decision_id": None}
OK_ANSWERS = {
    ("POST", "/chat"): (200, CHAT_OK),
    ("POST", "/chat/stream"): (200, STREAM_OK),
    ("DELETE", "/chat/old-session"): (200, {"status": "ended", "session_id": "old-session"}),
    ("GET", "/identity"): (200, {"agent_id": "nous", "is_initiated": True, "sections": {"character": "kind"}}),
}
REFUSED_BODY = {"error": "forbidden", "detail": "<b>server text</b>"}


def _bot(api: _Api, *, secret: str | None = SECRET) -> NousTelegramBot:
    bot = NousTelegramBot(
        "test-token", "http://nous.test", allowed_users={42}, owner_chat_id=42, owner_route_secret=secret
    )
    bot.tg = []  # (method, params) of every Telegram call

    async def fake_tg(method, params=None, **_fallback):
        bot.tg.append((method, params))
        return {"message_id": 1}

    bot._tg = fake_tg
    bot._http = httpx.AsyncClient(transport=httpx.MockTransport(api))
    return bot


def _message(text: str) -> dict:
    return {
        "update_id": 2,
        "message": {"message_id": 11, "from": {"id": 42, "first_name": "Owner"}, "chat": {"id": 42}, "text": text},
    }


def _sent(bot: NousTelegramBot) -> list[str]:
    return [params["text"] for method, params in bot.tg if method in ("sendMessage", "editMessageText")]


async def _chat_three_ways(bot: NousTelegramBot) -> None:
    """An ordinary message (the stream), /debug (/chat) and /new (DELETE /chat/{session})."""
    await bot._handle_update(_message("hello there"))
    await bot._handle_update(_message("/debug"))
    bot._sessions[42] = "old-session"
    await bot._handle_update(_message("/new"))


async def test_chat_stream_chat_and_new_carry_the_owner_secret() -> None:
    api = _Api(OK_ANSWERS)
    bot = _bot(api)
    await _chat_three_ways(bot)
    chat_calls = [(method, path, header) for method, path, header in api.requests if path.startswith("/chat")]
    assert chat_calls == [
        ("POST", "/chat/stream", SECRET),
        ("POST", "/chat", SECRET),
        ("DELETE", "/chat/old-session", SECRET),
    ]


async def test_a_bot_with_no_secret_sends_no_header() -> None:
    api = _Api(OK_ANSWERS)
    bot = _bot(api, secret=None)
    await _chat_three_ways(bot)
    assert [path for _method, path, _header in api.requests] == ["/chat/stream", "/chat", "/chat/old-session"]
    assert all(header is None for _method, _path, header in api.requests)


@pytest.mark.parametrize("path", ["/chat/stream", "/chat"])
async def test_a_403_is_answered_in_fixed_words(path: str) -> None:
    api = _Api({("POST", "/chat/stream"): (403, REFUSED_BODY), ("POST", "/chat"): (403, REFUSED_BODY)})
    bot = _bot(api)
    await bot._handle_update(_message("hello there" if path == "/chat/stream" else "/debug"))
    assert [p for _m, p, _h in api.requests] == [path]
    assert _sent(bot) == [CHAT_REFUSED]
    assert "server text" not in CHAT_REFUSED and "forbidden" not in CHAT_REFUSED


async def test_a_403_on_new_is_answered_in_the_same_fixed_words() -> None:
    api = _Api({("DELETE", "/chat/old-session"): (403, REFUSED_BODY)})
    bot = _bot(api)
    bot._sessions[42] = "old-session"
    await bot._handle_update(_message("/new"))
    assert api.requests == [("DELETE", "/chat/old-session", SECRET)]
    assert _sent(bot) == [CHAT_REFUSED]


async def test_new_still_starts_a_session_when_the_end_is_accepted() -> None:
    api = _Api(OK_ANSWERS)
    bot = _bot(api)
    bot._sessions[42] = "old-session"
    await bot._handle_update(_message("/new"))
    assert len(_sent(bot)) == 1 and "New session started" in _sent(bot)[0]
    assert 42 not in bot._sessions


async def test_a_refused_chat_is_logged_once_per_process_with_no_secret(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    api = _Api({("POST", "/chat/stream"): (403, REFUSED_BODY), ("POST", "/chat"): (403, REFUSED_BODY)})
    bot = _bot(api)
    for text in ("one", "two", "/debug"):
        await bot._handle_update(_message(text))
    assert _sent(bot) == [CHAT_REFUSED] * 3
    refusals = [r for r in caplog.records if r.name == "nous.telegram_bot" and "403" in r.getMessage()]
    assert len(refusals) == 1
    for record in caplog.records:
        assert SECRET not in record.getMessage() + repr(record.args)


async def test_another_error_is_still_reported_as_before() -> None:
    api = _Api({("POST", "/chat"): (500, {"error": "Chat error"})})
    bot = _bot(api)
    await bot._handle_update(_message("/debug"))
    assert len(_sent(bot)) == 1 and "Chat error" in _sent(bot)[0]


async def test_identity_still_works_with_no_secret() -> None:
    api = _Api(OK_ANSWERS)
    bot = _bot(api, secret=None)
    await bot._handle_update(_message("/identity"))
    assert api.requests == [("GET", "/identity", None)]
    assert any("character" in text.lower() for text in _sent(bot))


def test_the_refusal_names_the_setting_and_echoes_nothing() -> None:
    assert "NOUS_OWNER_ROUTE_SECRET" in CHAT_REFUSED
    assert json.dumps(REFUSED_BODY) not in CHAT_REFUSED
