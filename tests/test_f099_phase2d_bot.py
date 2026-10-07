"""F099 Phase 2d-8: the bot's owner actions are parsed in code and reach only the REST routes, never /chat."""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock

import httpx
import pytest

from nous import owner_actions, telegram_bot
from nous.telegram_bot import (
    OWNER_REQUEST_TIMEOUT,
    NousTelegramBot,
    describe_answer,
    describe_decision,
    parse_callback,
    parse_chat_id,
)

HEX = "a" * 32
SHORT = HEX[:8]
DECIDE = f"/intentions/proposals/{HEX}/decide"


class _Response:
    def __init__(self, status: int, body: dict):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


class _FakeHttp:
    """The REST API as the bot sees it: ``routes`` maps a path to ``(status, body)``; any other path is a 404."""

    def __init__(self, routes=None):
        self.routes = routes or {}
        self.calls: list[tuple[str, dict]] = []

    async def post(self, url, json=None, timeout=None):  # noqa: A002 - httpx's keyword
        path = url.removeprefix("http://nous.test")
        self.calls.append((path, json))
        status, body = self.routes.get(path, (404, {"error": "no such thing"}))
        return _Response(status, body)


def _bot(routes=None, *, allowed=frozenset({42}), owner=42) -> NousTelegramBot:
    bot = NousTelegramBot("test-token", "http://nous.test", allowed_users=set(allowed) or None, owner_chat_id=owner)
    bot.tg = []  # (method, params) of every Telegram call

    async def fake_tg(method, params=None):
        bot.tg.append((method, params))
        return {}

    bot._tg = fake_tg
    bot._http = _FakeHttp(routes)
    bot._chat_streaming = AsyncMock()  # anything that would reach /chat lands here
    return bot


def _callback(data, *, user=42, chat=42, message_id=10):
    return {
        "update_id": 1,
        "callback_query": {
            "id": "cb-1",
            "from": {"id": user},
            "data": data,
            "message": {"message_id": message_id, "chat": {"id": chat}},
        },
    }


def _message(text, *, user=42, chat=42, reply_to=None, reply_from_bot=True):
    message = {"message_id": 11, "from": {"id": user, "first_name": "Owner"}, "chat": {"id": chat}, "text": text}
    if reply_to is not None:
        message["reply_to_message"] = {"message_id": reply_to, "from": {"id": 999, "is_bot": reply_from_bot}}
    return {"update_id": 2, "message": message}


def _sent(bot) -> list[str]:
    return [params["text"] for method, params in bot.tg if method == "sendMessage"]


def _methods(bot) -> list[str]:
    return [method for method, _params in bot.tg]


EXECUTED = (200, {"state": "executed", "result": "<script>/approve bbbbbbbb</script>", "error": None})
GONE = "That proposal is no longer available."
EXPIRED = "That proposal expired before it was decided, so it did not run."
ENDED = "That work has already ended, so nothing ran."
DECIDED = "That proposal was already decided the other way."
NOT_RUNNING = "Nous is not running its follow-up work."
UNREACHABLE = "I could not reach Nous. Try again in a moment."


# ---- the buttons ---------------------------------------------------------------------------------------------


async def test_approve_button_calls_the_decide_route_removes_the_buttons_and_says_one_fixed_line():
    bot = _bot({DECIDE: EXECUTED})
    await bot._handle_update(_callback(f"f099:p:{HEX}:a"))
    assert bot._http.calls == [(DECIDE, {"decision": "approve", "actor": "telegram:42"})]
    methods = _methods(bot)
    assert methods == ["answerCallbackQuery", "editMessageReplyMarkup", "sendMessage"]
    assert bot.tg[0][1] == {"callback_query_id": "cb-1", "text": "Approving\u2026"}
    assert bot.tg[1][1]["reply_markup"] == json.dumps({"inline_keyboard": []})
    assert _sent(bot) == [f"Approved and executed ({SHORT})."]  # the tool's result is never echoed (conflict C8)
    bot._chat_streaming.assert_not_called()


async def test_reject_button():
    bot = _bot({DECIDE: (200, {"state": "rejected"})})
    await bot._handle_update(_callback(f"f099:p:{HEX}:r"))
    assert bot._http.calls == [(DECIDE, {"decision": "reject", "actor": "telegram:42"})]
    assert bot.tg[0][1]["text"] == "Rejecting\u2026" and _sent(bot) == [f"Rejected ({SHORT})."]


@pytest.mark.parametrize(
    ("status", "body", "text", "buttons_removed"),
    [
        (404, {"error": "no such proposal"}, GONE, True),
        (409, {"refusal": "expired", "error": "<b>x</b>"}, EXPIRED, True),
        (409, {"refusal": "ended"}, ENDED, True),
        (409, {"refusal": "not_pending"}, DECIDED, True),
        (503, {"error": "continuation is not running"}, NOT_RUNNING, False),
        (400, {"error": "x"}, "I could not read that id.", False),
        (500, {}, UNREACHABLE, False),
    ],
)
async def test_every_answer_of_the_route_has_a_fixed_text_and_a_transient_one_keeps_the_buttons(
    status, body, text, buttons_removed
):
    """Prod's case is the first row: no rows exist, so a stale or forged tap is told it is gone."""
    bot = _bot({DECIDE: (status, body)})
    await bot._handle_update(_callback(f"f099:p:{HEX}:a"))
    assert _sent(bot) == [text] and ("editMessageReplyMarkup" in _methods(bot)) is buttons_removed


async def test_an_unreachable_server_keeps_the_buttons():
    class Down:
        async def post(self, *args, **kwargs):
            raise OSError("connection refused")

    bot = _bot()
    bot._http = Down()
    await bot._handle_update(_callback(f"f099:p:{HEX}:a"))
    assert _sent(bot) == [UNREACHABLE] and "editMessageReplyMarkup" not in _methods(bot)


def _telegram_raises_on(bot, failing: str) -> None:
    """Telegram raises (a transport error, or its HTML 502 page) on ``failing``; every call is still recorded."""

    async def fake_tg(method, params=None):
        bot.tg.append((method, params))
        if method == failing:
            raise httpx.ConnectError("telegram is unreachable")
        return {}

    bot._tg = fake_tg


async def test_a_tap_whose_button_removal_raises_still_tells_the_owner():
    """2d-8 review I1: the route answered 200 (the call ran), so a raised ``editMessageReplyMarkup`` must not skip
    the one line that tells the owner it did."""
    bot = _bot({DECIDE: EXECUTED})
    _telegram_raises_on(bot, "editMessageReplyMarkup")
    await bot._handle_update(_callback(f"f099:p:{HEX}:a"))
    assert bot._http.calls == [(DECIDE, {"decision": "approve", "actor": "telegram:42"})]
    assert _methods(bot) == ["answerCallbackQuery", "editMessageReplyMarkup", "sendMessage"]
    assert _sent(bot) == [f"Approved and executed ({SHORT})."]


async def test_a_tap_whose_follow_up_raises_still_returns_normally():
    """2d-8 review I1: a raised ``sendMessage`` is logged, not raised into the poll loop."""
    bot = _bot({DECIDE: EXECUTED})
    _telegram_raises_on(bot, "sendMessage")
    await bot._handle_update(_callback(f"f099:p:{HEX}:a"))
    assert len(bot._http.calls) == 1
    assert _methods(bot) == ["answerCallbackQuery", "editMessageReplyMarkup", "sendMessage"]


async def test_the_owner_route_bounds_its_connect_like_the_chat_call():
    """2d-8 review m1: only the read may take as long as an approved call; a dropped connection fails in 10 s."""
    seen = []

    class Recording(_FakeHttp):
        async def post(self, url, json=None, timeout=None):  # noqa: A002 - httpx's keyword
            seen.append(timeout)
            return await super().post(url, json=json, timeout=timeout)

    bot = _bot()
    bot._http = Recording({DECIDE: EXECUTED})
    await bot._handle_update(_callback(f"f099:p:{HEX}:a"))
    (timeout,) = seen
    assert isinstance(timeout, httpx.Timeout)
    assert (timeout.connect, timeout.read, timeout.write, timeout.pool) == (10, OWNER_REQUEST_TIMEOUT, 10, 10)


@pytest.mark.parametrize(
    ("kwargs", "owner"),
    [
        ({"user": 7, "chat": 42}, 42),  # not the owner's user
        ({"user": 42, "chat": 7}, 42),  # not the owner's chat
        ({"user": 42, "chat": 42}, None),  # no owner chat configured: owner actions are off
    ],
)
async def test_a_button_from_anyone_but_the_owner_reaches_no_route(kwargs, owner):
    bot = _bot({DECIDE: EXECUTED}, owner=owner)
    await bot._handle_update(_callback(f"f099:p:{HEX}:a", **kwargs))
    assert bot._http.calls == [] and _sent(bot) == []
    assert bot.tg == [("answerCallbackQuery", {"callback_query_id": "cb-1", "text": "Not authorized."})]


async def test_a_group_owner_chat_needs_an_allowlist_and_a_private_one_does_not():
    group = _bot({DECIDE: EXECUTED}, allowed=frozenset(), owner=-1001)
    await group._handle_update(_callback(f"f099:p:{HEX}:a", user=42, chat=-1001))
    assert group._http.calls == []  # anyone in the group could tap: fail closed
    group_ok = _bot({DECIDE: EXECUTED}, allowed=frozenset({42}), owner=-1001)
    await group_ok._handle_update(_callback(f"f099:p:{HEX}:a", user=42, chat=-1001))
    assert len(group_ok._http.calls) == 1
    private = _bot({DECIDE: EXECUTED}, allowed=frozenset(), owner=42)
    await private._handle_update(_callback(f"f099:p:{HEX}:a", user=42, chat=42))
    assert len(private._http.calls) == 1


@pytest.mark.parametrize("data", ["", "f099", f"f099:p:{HEX.upper()}:a", f"f099:p:{HEX}:x", "something else", None])
async def test_a_button_that_is_not_ours_is_unknown_and_reaches_no_route(data):
    bot = _bot()
    await bot._handle_update(_callback(data))
    assert bot._http.calls == []
    assert bot.tg == [("answerCallbackQuery", {"callback_query_id": "cb-1", "text": "Unknown button."})]


# ---- the commands --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "decision"),
    [
        (f"/approve {SHORT}", "approve"),
        (f"/approve@NousBot {SHORT}", "approve"),
        (f"/REJECT {SHORT}", "reject"),
        (f"  /approve   {SHORT}  ", "approve"),
        (f"/approve {HEX[:8]}-{HEX[8:12]}", "approve"),
    ],
)
async def test_approve_and_reject_commands_are_parsed_in_code_and_reach_the_decide_route(text, decision):
    routes = {f"/intentions/proposals/{ident}/decide": EXECUTED for ident in (SHORT, HEX[:12])}
    bot = _bot(routes)
    await bot._handle_update(_message(text.strip()))
    ((path, payload),) = bot._http.calls
    assert path.startswith("/intentions/proposals/") and path.endswith("/decide")
    assert payload == {"decision": decision, "actor": "telegram:42"}
    assert len(_sent(bot)) == 1
    bot._chat_streaming.assert_not_called()  # never to the model


HOSTILE_IDS = [
    "../../chat",
    "abcdef01/../x",
    "abcdef0?x=1",
    "abcd",
    "zz" * 8,
    "a" * 37,
    "abcdef01%2F..",
    "abcdef01 ; rm",
]


@pytest.mark.parametrize("arg", HOSTILE_IDS)
async def test_an_id_argument_cannot_steer_the_request_path(arg):
    """Review Focus 4: the argument becomes part of a URL, so only hex and dashes ever get that far."""
    bot = _bot()
    await bot._handle_update(_message(f"/approve {arg}"))
    assert bot._http.calls == [] and _sent(bot) == []  # nothing of it reached a URL, or the owner
    bot._chat_streaming.assert_awaited_once()  # it went where it always went
    assert bot._chat_streaming.await_args.args[1] == f"/approve {arg}"


async def test_a_command_with_no_id_falls_through_to_chat_unchanged():
    bot = _bot()
    await bot._handle_update(_message("/reject"))
    assert bot._http.calls == [] and _sent(bot) == []
    bot._chat_streaming.assert_awaited_once()
    assert bot._chat_streaming.await_args.args[1] == "/reject"


async def test_answer_command_posts_the_text_to_the_question_route():
    path = f"/intentions/questions/{SHORT}/answer"
    bot = _bot({path: (200, {"question_id": "x", "arrival_id": "y", "woke": True})})
    await bot._handle_update(_message(f"/answer {SHORT} Yes, book the Friday slot."))
    assert bot._http.calls == [(path, {"text": "Yes, book the Friday slot.", "actor": "telegram:42"})]
    assert _sent(bot) == ["Answer recorded."]


@pytest.mark.parametrize("text", ["/answer", f"/answer {SHORT}", f"/answer {SHORT}   ", "/answer zzzzzzzz yes"])
async def test_a_malformed_answer_command_falls_through_to_chat_unchanged(text):
    bot = _bot()
    await bot._handle_update(_message(text))
    assert bot._http.calls == [] and _sent(bot) == []
    bot._chat_streaming.assert_awaited_once()
    assert bot._chat_streaming.await_args.args[1] == text.strip()


@pytest.mark.parametrize(
    ("status", "body", "text"),
    [
        (409, {"reason": "answered"}, "That question was already answered."),
        (409, {"reason": "expired"}, "That question expired before it was answered."),
        (409, {"reason": "ended"}, "That work has already ended, so your answer was not recorded."),
        (503, {}, NOT_RUNNING),
        (500, {}, UNREACHABLE),
    ],
)
async def test_every_answer_of_the_answer_route_has_a_fixed_text(status, body, text):
    bot = _bot({f"/intentions/questions/{SHORT}/answer": (status, body)})
    await bot._handle_update(_message(f"/answer {SHORT} Yes"))
    assert _sent(bot) == [text]


@pytest.mark.parametrize("text", [f"/approve {SHORT}", f"/reject {SHORT}", f"/answer {SHORT} Yes"])
async def test_an_unknown_id_under_prods_empty_tables_falls_through_to_chat_unchanged(text):
    """C18, strict parity: under prod's flags every id is a 404, and the bot behaves as it did before 2d."""
    bot = _bot()  # no route knows anything: every id is a 404, as in prod
    await bot._handle_update(_message(text))
    assert len(bot._http.calls) == 1 and _sent(bot) == []  # asked the route, said nothing
    bot._chat_streaming.assert_awaited_once()
    assert bot._chat_streaming.await_args.args[1] == text


# ---- replies -------------------------------------------------------------------------------------------------


async def test_a_reply_to_a_bot_message_answers_the_question_it_was_sent_as():
    bot = _bot({"/intentions/questions/answer": (200, {"question_id": "x", "arrival_id": "y", "woke": True})})
    await bot._handle_update(_message("Yes, book it.", reply_to=777))
    payload = {"chat_id": 42, "message_id": 777, "text": "Yes, book it.", "actor": "telegram:42"}
    assert bot._http.calls == [("/intentions/questions/answer", payload)]
    assert _sent(bot) == ["Answer recorded."]
    bot._chat_streaming.assert_not_called()


async def test_a_reply_that_is_not_to_a_question_falls_through_to_chat_unchanged():
    bot = _bot()  # the route answers 404: it was a reply to something else
    await bot._handle_update(_message("Thanks!", reply_to=5))
    assert len(bot._http.calls) == 1 and _sent(bot) == []
    bot._chat_streaming.assert_awaited_once()
    assert bot._chat_streaming.await_args.args[1] == "Thanks!"


async def test_a_reply_to_a_message_the_bot_did_not_send_is_not_even_tried():
    bot = _bot()
    await bot._handle_update(_message("hello", reply_to=5, reply_from_bot=False))
    assert bot._http.calls == []
    bot._chat_streaming.assert_awaited_once()


async def test_a_refused_reply_is_told_why_and_not_forwarded():
    bot = _bot({"/intentions/questions/answer": (409, {"reason": "answered"})})
    await bot._handle_update(_message("Yes", reply_to=777))
    assert _sent(bot) == ["That question was already answered."]
    bot._chat_streaming.assert_not_called()


@pytest.mark.parametrize("status", [500, 503, 400])
async def test_a_reply_whose_route_fails_reaches_chat_with_its_original_text(status):
    """Final review I1: only a 200 or a 409 proves the reply was addressed to a question. Any other answer of the
    route (a bug, an outage, no runner) must not swallow an ordinary reply to an earlier bot message."""
    bot = _bot({"/intentions/questions/answer": (status, {"error": "boom"})})
    await bot._handle_update(_message("Thanks, that helps.", reply_to=5))
    assert len(bot._http.calls) == 1 and _sent(bot) == []
    bot._chat_streaming.assert_awaited_once()
    assert bot._chat_streaming.await_args.args[1] == "Thanks, that helps."


@pytest.mark.parametrize("error", [httpx.ConnectError("refused"), httpx.ReadTimeout("slow")])
async def test_a_reply_whose_route_cannot_be_reached_reaches_chat_with_its_original_text(error):
    bot = _bot()
    bot._http.post = AsyncMock(side_effect=error)
    await bot._handle_update(_message("Thanks, that helps.", reply_to=5))
    bot._http.post.assert_awaited_once()
    assert _sent(bot) == []
    bot._chat_streaming.assert_awaited_once()
    assert bot._chat_streaming.await_args.args[1] == "Thanks, that helps."


async def test_a_typed_approve_whose_route_fails_is_still_answered_and_not_forwarded():  # PIN
    """The owner typed an action verb: "could not reach Nous" is the right answer, and the model never sees it."""
    bot = _bot({f"/intentions/proposals/{SHORT}/decide": (500, {})})
    await bot._handle_update(_message(f"/approve {SHORT}"))
    assert len(bot._http.calls) == 1 and _sent(bot) == [UNREACHABLE]
    bot._chat_streaming.assert_not_called()


# ---- everything else is untouched ----------------------------------------------------------------------------


@pytest.mark.parametrize("text", [f"/approve {SHORT}", f"/reject {SHORT}", f"/answer {SHORT} yes"])
async def test_a_command_outside_the_owner_chat_is_not_consumed_and_takes_the_old_path(text):
    """Parity for every other chat: it goes to the agent as it always did. Each command is well formed (2d-8 review
    m2), so the owner-chat gate alone keeps it from the route."""
    bot = _bot(allowed=frozenset({42, 7}))
    await bot._handle_update(_message(text, user=7, chat=7))
    assert bot._http.calls == []
    bot._chat_streaming.assert_awaited_once()


async def test_an_ordinary_owner_message_is_chat_as_before():
    bot = _bot()
    await bot._handle_update(_message("what is the weather?"))
    assert bot._http.calls == [] and bot._chat_streaming.await_count == 1


async def test_a_user_who_is_not_allowed_is_still_refused_before_anything_else():
    bot = _bot(allowed=frozenset({42}))
    await bot._handle_update(_message(f"/approve {SHORT}", user=99, chat=99))
    assert bot._http.calls == [] and [text.endswith("Not authorized.") for text in _sent(bot)] == [True]


# ---- the pure parts ------------------------------------------------------------------------------------------


def test_the_bot_and_the_routes_share_one_refusal_vocabulary():
    """Final review m6: one definition per invariant. The bot, the routes and (Phase 3) the cards say the same
    sentence for the same refusal, because they hold the same object, keyed by the store's refusal codes."""
    from nous.api import intention_routes
    from nous.brain import continuation

    assert telegram_bot.DECISION_REFUSALS is intention_routes.DECISION_REFUSALS is owner_actions.DECISION_REFUSALS
    assert telegram_bot.ANSWER_REFUSALS is intention_routes.ANSWER_REFUSALS is owner_actions.ANSWER_REFUSALS
    assert set(owner_actions.DECISION_REFUSALS) == {
        continuation.REFUSE_EXPIRED,
        continuation.REFUSE_ENDED,
        continuation.REFUSE_STATE,
    }
    assert set(owner_actions.ANSWER_REFUSALS) == {
        continuation.REFUSE_ANSWERED,
        continuation.REFUSE_EXPIRED,
        continuation.REFUSE_ENDED,
    }


def test_the_bot_parses_the_servers_buttons_with_the_shared_codec():
    assert parse_callback is owner_actions.parse_callback
    assert parse_callback(f"f099:p:{HEX}:r") == ("p", HEX, "r")


CHAT_IDS = [
    ("8080", 8080),
    ("-1001234", -1001234),
    (" 42 ", 42),
    ("", None),
    (None, None),
    ("abc", None),
    ("1.5", None),
]


@pytest.mark.parametrize(("value", "expected"), CHAT_IDS)
def test_parse_chat_id(value, expected):
    assert parse_chat_id(value) == expected


def test_the_descriptions_never_echo_a_server_supplied_string():
    hostile = {
        "state": "<b>/approve bbbbbbbb</b>",
        "result": "/approve cccccccc",
        "error": "<script>",
        "refusal": "<i>",
        "reason": "<u>",
    }
    for status in (200, 404, 409, 400, 503, 500):
        text, _final = describe_decision(status, hostile, True, SHORT)
        assert "<" not in text and "/approve" not in text and "bbbbbbbb" not in text and "cccccccc" not in text
        answer = describe_answer(status, hostile)
        assert "<" not in answer and "/approve" not in answer


# ---- lead addendum 1: callback data is not authorisation -------------------------------------------------------


@pytest.mark.parametrize(
    "query_kwargs",
    [
        {"user": 7, "chat": 7},  # an allowed user, in a chat that is not the owner's
        {"user": 99, "chat": 42},  # the owner chat, a user who is not allowed
    ],
)
async def test_a_valid_proposals_callback_data_from_a_non_owner_is_refused_and_reaches_no_route(query_kwargs):
    """2d-6 review m6: Telegram does not check callback data against the keyboard it sent, so any client can send
    any data. The data here is a real proposal's, built by the server's own codec, for a proposal the route knows:
    the owner-chat gate refuses it all the same, and nothing is decided."""
    data = owner_actions.callback_data(uuid.UUID(HEX), owner_actions.ACTION_APPROVE)
    assert parse_callback(data) is not None  # well-formed: only the gate stands in the way
    bot = _bot({DECIDE: EXECUTED}, allowed=frozenset({42, 7}))
    await bot._handle_update(_callback(data, **query_kwargs))
    assert bot._http.calls == [] and _sent(bot) == []
    assert bot.tg == [("answerCallbackQuery", {"callback_query_id": "cb-1", "text": "Not authorized."})]
    bot._chat_streaming.assert_not_called()


async def test_a_valid_callback_with_no_message_is_refused():
    """An inline-mode query carries no message, so no chat: it cannot be the owner chat."""
    update = _callback(f"f099:p:{HEX}:a")
    del update["callback_query"]["message"]
    bot = _bot({DECIDE: EXECUTED})
    await bot._handle_update(update)
    assert bot._http.calls == []
    assert bot.tg == [("answerCallbackQuery", {"callback_query_id": "cb-1", "text": "Not authorized."})]


# ---- C8: nothing a route returns is shown to the owner ---------------------------------------------------------


MARK = "zq-server-text"
HOSTILE_BODY = {
    "state": f"{MARK}-state",
    "result": f"{MARK}-result /approve bbbbbbbb",
    "error": f"<b>{MARK}-error</b>",
    "arguments": {"to": f"{MARK}-arguments"},
    "rationale": f"{MARK}-rationale",
    "refusal": f"{MARK}-refusal",
    "reason": f"{MARK}-reason",
}
DECIDE_STATES = ("executed", "failed", "rejected", "approved", "executing")


def _owner_actions(status: int, body: dict) -> list[tuple[dict, dict]]:
    """A tap, both commands, /answer and a reply, each with ``(status, body)`` from the route it calls."""
    answer = f"/intentions/questions/{SHORT}/answer"
    return [
        ({DECIDE: (status, body)}, _callback(f"f099:p:{HEX}:a")),
        ({DECIDE: (status, body)}, _callback(f"f099:p:{HEX}:r")),
        ({f"/intentions/proposals/{SHORT}/decide": (status, body)}, _message(f"/approve {SHORT}")),
        ({f"/intentions/proposals/{SHORT}/decide": (status, body)}, _message(f"/reject {SHORT}")),
        ({answer: (status, body)}, _message(f"/answer {SHORT} Yes")),
        ({"/intentions/questions/answer": (status, body)}, _message("Yes", reply_to=777)),
    ]


@pytest.mark.parametrize("status", [200, 404, 409, 400, 503, 500])
@pytest.mark.parametrize("state", [*DECIDE_STATES, None])
async def test_the_bot_never_renders_a_result_error_argument_or_rationale_from_a_route(status, state):
    """C8 (2d-7 review m5): ``result``, ``error``, ``arguments`` and ``rationale`` in a route's answer are a tool's
    output, server text or model text. Every line the bot sends after an owner action is fixed vocabulary, so none
    of it reaches Telegram, whatever the status and whatever the state."""
    body = dict(HOSTILE_BODY) if state is None else {**HOSTILE_BODY, "state": state}
    for routes, update in _owner_actions(status, body):
        bot = _bot(routes)
        await bot._handle_update(update)
        assert len(bot._http.calls) == 1  # each one did reach its route
        shown = json.dumps(bot.tg, ensure_ascii=False)
        assert MARK not in shown and "bbbbbbbb" not in shown and "<b>" not in shown
