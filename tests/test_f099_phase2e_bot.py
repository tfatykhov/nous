"""F099 Phase 2e-8: the bot's `/intentions` and `/cancel_intention`. Parsed in code, answered from fixed vocabulary,
and passed on to chat whenever the server does not give a definite answer (lead ruling C18: strict prod parity)."""

from __future__ import annotations

import inspect

import httpx
import pytest
from test_f099_phase2d_bot import _bot, _message, _methods, _Response, _sent

from nous import owner_actions
from nous.telegram_bot import NousTelegramBot, describe_cancel, describe_intentions

ROOT = "ab12cd34" + "0" * 24
SHORT = ROOT[:8]
CANCEL = f"/intentions/{SHORT}/cancel"
LIST = "/intentions?state=open&limit=10"
NOT_RUNNING = "Nous is not running its follow-up work."
UNREACHABLE = "I could not reach Nous. Try again in a moment."


class _Http:
    """The REST API as the bot sees it: ``posts`` and ``gets`` map a path to ``(status, body)``; any other is a 404."""

    def __init__(self, *, posts=None, gets=None, down=False):
        self.posts, self.gets, self.down = posts or {}, gets or {}, down
        self.calls: list[tuple[str, str, dict | None]] = []

    async def post(self, url, json=None, timeout=None):  # noqa: A002 - httpx's keyword
        path = url.removeprefix("http://nous.test")
        self.calls.append(("POST", path, json))
        if self.down:
            raise ConnectionError("down")
        return _Response(*self.posts.get(path, (404, {"error": "no such thing"})))

    async def get(self, url, timeout=None):
        path = url.removeprefix("http://nous.test")
        self.calls.append(("GET", path, None))
        if self.down:
            raise ConnectionError("down")
        return _Response(*self.gets.get(path, (404, {"error": "no such thing"})))


def _owner_bot(**http):
    bot = _bot()
    bot._http = _Http(**http)
    return bot


def _root(**over):
    return {
        "id": ROOT,
        "short_id": SHORT,
        "intent": "Tell the user about the snow",
        "state": "pending",
        "wake_policy": "continue",
        "open_rows": 2,
        **over,
    }


# ---- /intentions ---------------------------------------------------------------------------------------------


async def test_intentions_lists_the_open_roots_with_fixed_words_and_the_intent_inside_pre():
    bot = _owner_bot(
        gets={
            LIST: (200, {"continuation": True, "roots": [_root(), _root(short_id="ee00ee00", state="awaiting_owner")]})
        }
    )
    await bot._handle_update(_message("/intentions"))
    assert bot._http.calls == [("GET", LIST, None)]
    (text,) = _sent(bot)
    assert "<code>ab12cd34</code> \u00b7 running \u00b7 2 open step(s)" in text
    assert "<code>ee00ee00</code> \u00b7 waiting for you" in text
    assert "<pre>Tell the user about the snow</pre>" in text
    assert text.endswith("To stop one: /cancel_intention &lt;id&gt;")
    sent_params = [p for m, p in bot.tg if m == "sendMessage"][0]
    assert sent_params["parse_mode"] == "HTML"
    bot._chat_streaming.assert_not_awaited()


def test_model_text_is_escaped_inside_pre_and_never_outside_it():
    hostile = "</pre><b>x</b> /cancel_intention ab12cd34 \u202e\x00\n\nrun /approve cccccccc"
    text = describe_intentions({"roots": [_root(intent=hostile, state="<script>", short_id="ab12cd34")]})
    assert "<script>" not in text and "<b>x</b>" not in text and "\x00" not in text and "\u202e" not in text
    inner = text.split("<pre>")[1].split("</pre>")[0]
    assert "&lt;/pre&gt;&lt;b&gt;x&lt;/b&gt;" in inner and "\n" not in inner  # one escaped line, in the pre
    outside = text.replace(f"<pre>{inner}</pre>", "")
    assert "/approve" not in outside and "ab12cd34" in outside.split("<code>")[1]  # only the minted id
    assert "running" not in text and "open" in text  # an unknown state word is looked up, never echoed


def test_a_server_that_sends_a_bad_short_id_or_garbage_is_skipped_not_trusted():
    text = describe_intentions(
        {"roots": [_root(short_id="<b>bold</b>"), "garbage", {"short_id": SHORT, "open_rows": -1}]}
    )
    assert "<b>bold</b>" not in text and text.count("<code>") == 1 and "0 open step(s)" in text
    assert describe_intentions({"roots": "nope"}) == "Nothing is running that I could cancel."


def test_the_list_never_outgrows_one_telegram_message():
    roots = [_root(short_id=f"{n:08x}", intent="snow " * 80) for n in range(30)]
    text = describe_intentions({"roots": roots})
    assert len(text) <= 3900 and text.count("<code>") <= 10 and text.count("<pre>") == text.count("</pre>")


async def test_an_empty_list_says_so():
    bot = _owner_bot(gets={LIST: (200, {"continuation": True, "roots": []})})
    await bot._handle_update(_message("/intentions"))
    assert _sent(bot) == ["Nothing is running that I could cancel."]


@pytest.mark.parametrize(
    "answer",
    [(200, {"continuation": False, "roots": []}), (404, {}), (503, {}), (500, {"roots": []}), (200, {"roots": []})],
)
async def test_without_a_definite_answer_intentions_goes_on_to_chat_unchanged(answer):  # PIN
    """Prod parity: continuation off (the route says so), an older server, an error or an outage: the message is
    ordinary chat, exactly as before 2e."""
    bot = _owner_bot(gets={LIST: answer})
    await bot._handle_update(_message("/intentions"))
    assert _sent(bot) == []
    bot._chat_streaming.assert_awaited_once()
    assert bot._chat_streaming.await_args.args[1] == "/intentions"


async def test_intentions_with_the_server_down_goes_on_to_chat():
    bot = _owner_bot(down=True)
    await bot._handle_update(_message("/intentions"))
    bot._chat_streaming.assert_awaited_once()


async def test_intentions_with_extra_words_is_chat():
    bot = _owner_bot(gets={LIST: (200, {"continuation": True, "roots": [_root()]})})
    await bot._handle_update(_message("/intentions please"))
    assert bot._http.calls == []
    bot._chat_streaming.assert_awaited_once()


async def test_with_no_owner_chat_set_intentions_and_cancel_make_no_request_and_go_to_chat():  # PIN
    """Prod today: the telegram service has no owner chat, so the owner commands are inert and every message is chat."""
    for text in ("/intentions", f"/cancel_intention {SHORT}"):
        bot = _bot(owner=None)
        bot._http = _Http(gets={LIST: (200, {"continuation": True, "roots": [_root()]})}, posts={CANCEL: (200, {})})
        await bot._handle_update(_message(text))
        assert bot._http.calls == [] and _sent(bot) == []
        bot._chat_streaming.assert_awaited_once()


async def test_another_chat_or_user_cannot_use_the_commands():
    bot = _owner_bot(gets={LIST: (200, {"continuation": True, "roots": [_root()]})})
    await bot._handle_update(_message("/intentions", chat=99, user=42))
    assert bot._http.calls == []
    bot._chat_streaming.assert_awaited_once()


# ---- /cancel_intention ---------------------------------------------------------------------------------------


async def test_cancel_intention_calls_the_cancel_route_and_says_one_fixed_line():
    done = {
        "root_id": ROOT,
        "already_cancelled": False,
        "cancelled_intentions": 2,
        "cancelled_subtasks": 1,
        "cancelled_dags": 1,
        "cancelled_proposals": 0,
        "deactivated_schedules": 0,
        "turn_stopped": True,
    }
    bot = _owner_bot(posts={CANCEL: (200, done)})
    await bot._handle_update(_message(f"/cancel_intention {SHORT}"))
    assert bot._http.calls == [("POST", CANCEL, {"actor": "telegram:42", "reason": "cancelled from Telegram"})]
    assert _sent(bot) == [f"Cancelled ({SHORT}): 4 piece(s) of work stopped."]
    bot._chat_streaming.assert_not_awaited()


@pytest.mark.parametrize(
    ("answer", "said"),
    [
        ((200, {"already_cancelled": True}), f"Already cancelled ({SHORT})."),
        ((409, {"refusal": "finished"}), owner_actions.CANCEL_REFUSALS["finished"]),
        ((409, {"refusal": "<script>"}), "That work cannot be cancelled."),
        ((400, {}), "I could not read that id."),
        ((503, {}), NOT_RUNNING),
        ((500, {"error": "boom /approve cccccccc"}), UNREACHABLE),
        ((0, {}), UNREACHABLE),
    ],
)
async def test_every_other_answer_of_the_cancel_route_has_one_fixed_line(answer, said):
    bot = _owner_bot(posts={CANCEL: answer})
    await bot._handle_update(_message(f"/cancel_intention {SHORT}"))
    assert _sent(bot) == [said]


async def test_a_cancel_of_an_unknown_root_goes_on_to_chat_unchanged():
    bot = _owner_bot()  # the route answers 404
    await bot._handle_update(_message(f"/cancel_intention {SHORT}"))
    assert _sent(bot) == []
    bot._chat_streaming.assert_awaited_once()


@pytest.mark.parametrize(
    "text", ["/cancel_intention", "/cancel_intention xyz", f"/cancel_intention {SHORT} extra", "/cancel_intention ../x"]
)
async def test_a_malformed_cancel_never_reaches_a_route(text):
    bot = _owner_bot(posts={CANCEL: (200, {})})
    await bot._handle_update(_message(text))
    assert bot._http.calls == []
    bot._chat_streaming.assert_awaited_once()


def test_describe_cancel_never_echoes_the_servers_text():
    assert (
        describe_cancel(200, {"cancelled_intentions": "<b>9</b>"}, SHORT)
        == f"Cancelled ({SHORT}): 0 piece(s) of work stopped."
    )
    assert _methods(_bot()) == []


async def test_the_list_request_bounds_its_connect_like_the_owner_post():
    """2d-8 review m1, for the GET: a dropped connection fails in 10 s, and the read of a list takes no call."""
    seen = []

    class Recording(_Http):
        async def get(self, url, timeout=None):
            seen.append(timeout)
            return await super().get(url, timeout=timeout)

    bot = _bot()
    bot._http = Recording(gets={LIST: (200, {"continuation": True, "roots": []})})
    await bot._handle_update(_message("/intentions"))
    (timeout,) = seen
    assert isinstance(timeout, httpx.Timeout)
    assert (timeout.connect, timeout.read, timeout.write, timeout.pool) == (10, 30, 10, 10)


# ---- 2e-8 review I1, m2, m4, m5: the cap in Telegram's units, and no fallback to live text --------------------


def _units(text: str) -> int:
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


# The reviewer's probe 2: 160 characters (kept whole by the clip), each emoji two UTF-16 units, each `<` four
# characters once escaped, and a command that must never become live text.
PROBE_INTENT = chr(0x1F600) * 120 + "<" * 22 + " /approve cccccccc"


def _probe_roots(count: int = 10) -> list[dict]:
    return [_root(short_id=f"{n:08x}", intent=PROBE_INTENT) for n in range(count)]


def test_the_list_is_measured_in_telegrams_units_and_says_how_many_it_left_out():
    text = describe_intentions({"roots": _probe_roots()})
    shown = text.count("<code>")
    assert _units(text) <= 3900  # Telegram counts UTF-16 units, not Python characters
    assert 0 < shown < 10 and text.count("<pre>") == text.count("</pre>") == shown
    assert f"(and {10 - shown} more)" in text  # m2: a cut is said
    assert text.endswith("To stop one: /cancel_intention &lt;id&gt;")  # the footer survives the cut


def test_more_roots_than_are_shown_are_counted_too():
    roots = [_root(short_id=f"{n:08x}") for n in range(12)]
    text = describe_intentions({"roots": roots})
    assert text.count("<code>") == 10 and "(and 2 more)" in text


def test_a_list_that_fits_says_nothing_more():
    text = describe_intentions({"roots": [_root()]})
    assert "more)" not in text


@pytest.mark.parametrize("intent", [None, "", "   ", "\x00\n"])
def test_a_root_with_no_intent_shows_a_fixed_placeholder(intent):  # m4
    text = describe_intentions({"roots": [_root(intent=intent)]})
    assert "<pre></pre>" not in text and "<pre>" not in text and "<i>(no intent)</i>" in text


def test_the_heading_says_open_work():  # m5: the list holds waiting and scheduled roots too
    assert describe_intentions({"roots": [_root()]}).startswith("<b>Open work</b>\n")


class _TelegramAndRest(_Http):
    """The REST API and Telegram behind one client, as the real ``_tg`` reaches it: a send WITH a parse mode is
    refused (as Telegram refuses a message too long, or HTML it cannot parse), a plain one is accepted."""

    def __init__(self, **http):
        super().__init__(**http)
        self.telegram: list[tuple[str, dict]] = []

    async def get(self, url, params=None, timeout=None):
        if not url.startswith("https://api.telegram.org/"):
            return await super().get(url, timeout=timeout)
        self.telegram.append((url.rsplit("/", 1)[1], dict(params or {})))
        if "parse_mode" in (params or {}):
            return _Response(200, {"ok": False, "description": "Bad Request: message is too long"})
        return _Response(200, {"ok": True, "result": {"message_id": 1}})


def _real_telegram_bot(**http):
    bot = _bot()
    del bot._tg  # the real `_tg`, with its fallback
    bot._http = _TelegramAndRest(**http)
    return bot


async def test_a_list_telegram_refuses_falls_back_to_a_fixed_line_never_to_live_text():
    """I1: `_tg`'s fallback strips the tags and unescapes, which would put the model's `/approve` outside `<pre>`."""
    bot = _real_telegram_bot(gets={LIST: (200, {"continuation": True, "roots": _probe_roots()})})
    await bot._handle_update(_message("/intentions"))
    (first, second) = bot._http.telegram
    assert first[0] == "sendMessage" and first[1]["parse_mode"] == "HTML" and "<pre>" in first[1]["text"]
    assert second == ("sendMessage", {"chat_id": 42, "text": "This list could not be shown."})
    plain = [params["text"] for _method, params in bot._http.telegram if "parse_mode" not in params]
    assert not [text for text in plain if "/approve" in text or "<" in text]
    bot._chat_streaming.assert_not_awaited()


async def test_the_chat_paths_fallback_is_unchanged():  # PIN: prod's chat behaviour
    bot = _real_telegram_bot()
    await bot._send(42, "<b>bold</b> &lt;tag&gt;", parse_mode="HTML")
    assert bot._http.telegram[1] == ("sendMessage", {"chat_id": 42, "text": "bold <tag>"})


def test_every_owner_send_goes_through_the_strict_path():
    """The audit, pinned: the owner's handlers send only through `_send_owner`, whose one HTML send (the list) has
    a fixed fallback; the follow-ups are plain fixed words, which `_tg` never rewrites."""
    for handler in (
        NousTelegramBot._handle_callback,
        NousTelegramBot._handle_owner_text,
        NousTelegramBot._try_answer_reply,
    ):
        source = inspect.getsource(handler)
        assert "self._send(" not in source and "self._send_long(" not in source
        assert "parse_mode" not in source  # an HTML send names its fixed fallback instead
