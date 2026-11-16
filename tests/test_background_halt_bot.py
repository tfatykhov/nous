"""Goals PR 0c: /halt and /resume in the owner's Telegram chat. Always answered, never passed on to chat."""

from __future__ import annotations

import httpx
import pytest
from test_f099_phase2d_bot import _bot, _message, _Response, _sent

from nous.telegram_bot import HALT_LANDED_TEXT, HALT_SENT_TEXT, HALT_UNREACHABLE_TEXT

SECRET = "s" * 40
RESUMED = (
    "Background work is running again. Work that was cancelled was not restarted. "
    "One-time schedules that came due while halted run now."
)


class _Http:
    """The REST API as the bot sees it: ``posts`` and ``gets`` map a path to ``(status, body)``. ``post_error`` and
    ``get_error``: an exception the call raises instead."""

    def __init__(self, posts=None, *, gets=None, post_error=None, get_error=None):
        self.posts, self.gets = posts or {}, gets or {}
        self.post_error, self.get_error = post_error, get_error
        self.calls: list[tuple[str, dict | None, dict | None]] = []

    async def post(self, url, json=None, timeout=None, headers=None):  # noqa: A002 - httpx's keyword
        path = url.removeprefix("http://nous.test")
        self.calls.append((path, json, headers))
        if self.post_error is not None:
            raise self.post_error
        return _Response(*self.posts.get(path, (404, {})))

    async def get(self, url, timeout=None):
        path = url.removeprefix("http://nous.test")
        self.calls.append((path, None, None))
        if self.get_error is not None:
            raise self.get_error
        return _Response(*self.gets.get(path, (404, {})))


def _owner_bot(posts=None, *, secret: str | None = SECRET, **http):
    bot = _bot()
    bot._http = _Http(posts, **http)
    bot.owner_route_secret = secret
    return bot


HALTED_STATE = {"/background/state": (200, {"halted": True, "source": "stored"})}


async def test_halt_posts_with_the_owner_secret_and_the_actor_and_replies_with_the_report():
    report = "Background work is halted. Cancelled: 1 subtask. Nothing new can start."
    bot = _owner_bot({"/background/halt": (200, {"halted": True, "text": report})})
    await bot._handle_update(_message("/halt the bill is too high"))
    ((path, body, headers),) = bot._http.calls
    assert path == "/background/halt" and headers == {"X-Nous-Owner-Secret": SECRET}
    assert body == {"actor": "telegram:42", "surface": "telegram", "reason": "the bill is too high"}
    assert _sent(bot) == [report]
    bot._chat_streaming.assert_not_awaited()


async def test_halt_without_a_secret_still_halts_and_an_unreachable_server_gets_the_host_sentence():
    bot = _owner_bot(
        {"/background/halt": (200, {"text": "Background work is halted. Nothing was running."})}, secret=None
    )
    await bot._handle_update(_message("/halt"))
    assert bot._http.calls[0][2] is None and bot._http.calls[0][1]["reason"] is None
    down = _owner_bot(post_error=httpx.ConnectError("refused"), get_error=httpx.ConnectError("refused"))
    await down._handle_update(_message("/halt"))
    assert _sent(down) == [HALT_UNREACHABLE_TEXT]
    down._chat_streaming.assert_not_awaited()


@pytest.mark.parametrize(
    ("post_error", "gets", "get_error", "expected"),
    [
        (httpx.ReadTimeout("slow"), HALTED_STATE, None, HALT_LANDED_TEXT),  # sent, and the state says it landed
        (httpx.ReadTimeout("slow"), None, httpx.ReadTimeout("slow"), HALT_SENT_TEXT),  # sent, state unreadable
        (httpx.ConnectError("refused"), HALTED_STATE, None, HALT_LANDED_TEXT),  # halted already
        (httpx.ConnectError("refused"), {"/background/state": (200, {"halted": False})}, None, HALT_UNREACHABLE_TEXT),
    ],
)
async def test_a_halt_with_no_answer_checks_the_state_once_before_answering(post_error, gets, get_error, expected):
    bot = _owner_bot(post_error=post_error, gets=gets, get_error=get_error)
    await bot._handle_update(_message("/halt"))
    assert [c[0] for c in bot._http.calls] == ["/background/halt", "/background/state"]
    assert _sent(bot) == [expected]
    bot._chat_streaming.assert_not_awaited()


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (200, {"halted": False, "changed": True}, RESUMED),
        (200, {"halted": False, "changed": False}, "Background work is not halted."),
        (
            403,
            {"error": "forbidden"},
            "Resume refused: this bot has no owner secret, or it is wrong, or the server has no digest set.",
        ),
        (409, {"host_hold": True}, "The host holds the halt (NOUS_BACKGROUND_HALT). Clear it on the host and restart."),
        (409, {"raced": True}, "A halt arrived while resuming, so it stays halted."),
        (503, {"error": "not_saved"}, "Resume was not saved; background work stays halted. Try again."),
        (404, {}, "Resume was not saved; background work stays halted. Try again."),
    ],
)
async def test_resume_answers_every_status_and_never_falls_through_to_chat(status, body, expected):
    bot = _owner_bot({"/background/resume": (status, body)})
    await bot._handle_update(_message("/resume"))
    ((path, sent_body, headers),) = bot._http.calls
    assert path == "/background/resume" and headers == {"X-Nous-Owner-Secret": SECRET}
    assert sent_body == {"actor": "telegram:42", "surface": "telegram"}
    assert _sent(bot) == [expected]
    bot._chat_streaming.assert_not_awaited()


async def test_a_halt_that_is_not_confirmed_is_still_answered():
    bot = _owner_bot({"/background/halt": (503, {"error": "the halt is not available"})})
    await bot._handle_update(_message("/halt"))
    assert _sent(bot) and "not confirmed" in _sent(bot)[0]
    bot._chat_streaming.assert_not_awaited()


async def test_another_chat_is_untouched():
    bot = _owner_bot({"/background/halt": (200, {"text": "halted"})})
    await bot._handle_update(_message("/halt", user=7, chat=7))
    assert bot._http.calls == []
