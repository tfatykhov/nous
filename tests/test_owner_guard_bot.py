"""The Telegram bot sends the owner's secret on every owner action, and answers a 403 in fixed words. No database."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import pytest

from nous.telegram_bot import NousTelegramBot

SECRET = "s" * 64
HEADERS = {"X-Nous-Owner-Secret": SECRET}
HEX = "a" * 32
SHORT = HEX[:8]
NOT_SET_UP = "Owner actions are not set up on this server."
REFUSED = (403, {"error": "forbidden"})


class _Response:
    def __init__(self, status: int, body: dict):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


class _FakeHttp:
    """The REST API as the bot sees it: ``routes`` maps a path to ``(status, body)``; any other path is a 404. Every
    call is recorded as ``(path, json, headers)``."""

    def __init__(self, routes=None):
        self.routes = routes or {}
        self.calls: list[tuple[str, dict | None, dict | None]] = []

    def _answer(self, url):
        path = url.removeprefix("http://nous.test")
        status, body = self.routes.get(path.split("?", 1)[0], (404, {"error": "no such thing"}))
        return path, _Response(status, body)

    async def post(self, url, json=None, timeout=None, headers=None):  # noqa: A002 - httpx's keyword
        path, response = self._answer(url)
        self.calls.append((path, json, headers))
        return response

    async def get(self, url, timeout=None, headers=None):
        path, response = self._answer(url)
        self.calls.append((path, None, headers))
        return response


def _bot(routes=None, *, secret: str | None = SECRET) -> NousTelegramBot:
    bot = NousTelegramBot(
        "test-token", "http://nous.test", allowed_users={42}, owner_chat_id=42, owner_route_secret=secret
    )
    bot.tg = []  # (method, params) of every Telegram call

    async def fake_tg(method, params=None, **_fallback):
        bot.tg.append((method, params))
        return {}

    bot._tg = fake_tg
    bot._http = _FakeHttp(routes)
    bot._chat_streaming = AsyncMock()  # anything that would reach /chat lands here
    return bot


def _tap(action: str = "a") -> dict:
    button = {
        "id": "c",
        "from": {"id": 42, "first_name": "Owner"},
        "data": f"f099:p:{HEX}:{action}",
        "message": {"message_id": 1, "chat": {"id": 42}},
    }
    return {"callback_query": button}


def _message(text: str, *, reply_to: int | None = None) -> dict:
    message = {"message_id": 11, "from": {"id": 42, "first_name": "Owner"}, "chat": {"id": 42}, "text": text}
    if reply_to is not None:
        message["reply_to_message"] = {"message_id": reply_to, "from": {"id": 999, "is_bot": True}}
    return {"update_id": 2, "message": message}


def _sent(bot) -> list[str]:
    return [params["text"] for method, params in bot.tg if method == "sendMessage"]


OWNER_ACTIONS = {
    "tap-approve": (_tap(), f"/intentions/proposals/{HEX}/decide"),
    "reject": (_message(f"/reject {SHORT}"), f"/intentions/proposals/{SHORT}/decide"),
    "answer": (_message(f"/answer {SHORT} yes"), f"/intentions/questions/{SHORT}/answer"),
    "reply": (_message("Yes, go ahead.", reply_to=9), "/intentions/questions/answer"),
    "cancel": (_message(f"/cancel_intention {SHORT}"), f"/intentions/{SHORT}/cancel"),
    "drop": (_message("/drop C-7"), "/commitments/7/drop"),
}


@pytest.mark.parametrize("case", list(OWNER_ACTIONS))
async def test_every_owner_action_carries_the_secret(case: str) -> None:
    update, path = OWNER_ACTIONS[case]
    bot = _bot()
    await bot._handle_update(update)
    assert [(called, headers) for called, _json, headers in bot._http.calls] == [(path, HEADERS)]


async def test_the_list_carries_no_secret() -> None:
    bot = _bot()
    await bot._handle_update(_message("/intentions"))
    assert len(bot._http.calls) == 1
    path, _json, headers = bot._http.calls[0]
    assert path.startswith("/intentions?")
    assert headers is None


async def test_chat_carries_no_secret() -> None:
    bot = _bot()
    await bot._handle_update(_message("Hello there"))
    assert bot._http.calls == []  # an ordinary message reaches no owner route
    assert bot._chat_streaming.await_count == 1


async def test_a_bot_with_no_secret_sends_no_header() -> None:
    bot = _bot(secret=None)
    await bot._handle_update(_message(f"/reject {SHORT}"))
    assert [headers for _path, _json, headers in bot._http.calls] == [None]


@pytest.mark.parametrize(
    ("text", "path"),
    [
        (f"/approve {SHORT}", f"/intentions/proposals/{SHORT}/decide"),
        (f"/reject {SHORT}", f"/intentions/proposals/{SHORT}/decide"),
        (f"/answer {SHORT} yes", f"/intentions/questions/{SHORT}/answer"),
        (f"/cancel_intention {SHORT}", f"/intentions/{SHORT}/cancel"),
    ],
    ids=["approve", "reject", "answer", "cancel"],
)
async def test_a_403_on_a_command_is_answered_in_fixed_words(text: str, path: str) -> None:
    bot = _bot({path: REFUSED})
    await bot._handle_update(_message(text))
    assert _sent(bot) == [NOT_SET_UP]
    assert bot._chat_streaming.await_count == 0


async def test_a_403_on_a_tap_keeps_the_buttons() -> None:
    bot = _bot({f"/intentions/proposals/{HEX}/decide": REFUSED})
    await bot._handle_update(_tap())
    assert _sent(bot) == [NOT_SET_UP]
    assert "editMessageReplyMarkup" not in [method for method, _params in bot.tg]


async def test_a_reply_that_gets_a_403_still_goes_to_chat(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="nous.telegram_bot")
    bot = _bot({"/intentions/questions/answer": REFUSED})
    await bot._handle_update(_message("Yes, go ahead.", reply_to=9))
    await bot._handle_update(_message("And one more thing.", reply_to=9))
    assert _sent(bot) == []
    assert bot._chat_streaming.await_count == 2
    refusals = [r for r in caplog.records if "refused the bot's owner secret" in r.getMessage()]
    assert len(refusals) == 1  # once per process, not once per reply
    assert all(SECRET not in r.getMessage() for r in caplog.records)
