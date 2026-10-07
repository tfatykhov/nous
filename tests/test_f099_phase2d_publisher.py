"""F099 Phase 2d-6: the owner push of proposals and questions. Model-authored text is escaped inside <pre>."""

from __future__ import annotations

import html
import re
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from f099_support import (
    CONT,
    SEND_EMAIL_ARGS,
    ask_with_proposals,
    claimed,
    commit_ask,
    env_factory,  # noqa: F401
    inbox_rows,
    proposal_row,
    stage,
)
from sqlalchemy import select, update

from nous.brain import continuation
from nous.handlers.continuation_publisher import (
    PUSHED_KINDS,
    QUESTION_MARKUP,
    OwnerPublisher,
    proposal_keyboard,
    render_proposal_html,
)
from nous.owner_actions import ACTION_APPROVE, ACTION_REJECT, callback_data, parse_callback
from nous.storage.models import ResultInbox

LATER = 10  # hours: past any quiet-hours end, inside the 72 h claim window


def _later() -> datetime:
    return datetime.now(UTC) + timedelta(hours=LATER)


def _http(*, status=200, message_id=555):
    http = MagicMock()
    http.post = AsyncMock(
        return_value=SimpleNamespace(
            status_code=status, json=lambda: {"ok": True, "result": {"message_id": message_id}}
        )
    )
    return http


async def _env(env_factory, **over):  # noqa: F811
    return await env_factory(**CONT, telegram_bot_token="test-token", telegram_chat_id="8080", **over)


def _publisher(env, http) -> OwnerPublisher:
    return OwnerPublisher(database=env.db, settings=env.settings, http_client=http)


async def _row(env, msg_type):
    return next(r for r in await inbox_rows(env) if r.msg_type == msg_type)


async def _stored(env, row_id) -> ResultInbox:
    async with env.db.session() as s:
        return (await s.execute(select(ResultInbox).where(ResultInbox.id == row_id))).scalar_one()


def _payload(http) -> dict:
    return http.post.call_args.kwargs["json"]


def _outside_pre(text: str) -> str:
    return re.sub(r"<pre>.*?</pre>", "", text, flags=re.S)


def _plain_length(text: str) -> int:
    """What Telegram counts: the text after its entities are parsed."""
    return len(html.unescape(re.sub(r"<[^>]+>", "", text)))


# ---- the codec -----------------------------------------------------------------------------------------------


def test_callback_data_round_trips_and_fits_a_telegram_button():
    pid = uuid.uuid4()
    approve, reject = callback_data(pid, ACTION_APPROVE), callback_data(pid, ACTION_REJECT)
    assert approve == f"f099:p:{pid.hex}:a" and reject == f"f099:p:{pid.hex}:r"
    assert max(len(approve.encode()), len(reject.encode())) <= 64
    assert parse_callback(approve) == ("p", pid.hex, "a") and parse_callback(reject) == ("p", pid.hex, "r")
    with pytest.raises(ValueError):
        callback_data(pid, "x")


_HEX = "a" * 32
JUNK = [
    None,
    5,
    "",
    "f099",
    "f099:p:abc:a",
    f"f099:p:{_HEX.upper()}:a",  # lower-case hex only
    f"f099:p:{_HEX}:x",
    f"f099:p:{_HEX}:a:more",
    f"f099:q:{_HEX}:a",
    f"x f099:p:{_HEX}:a",
    f"f099:p:{_HEX}:a\n",
]


@pytest.mark.parametrize("junk", JUNK)
def test_parse_callback_rejects_anything_that_is_not_exactly_a_proposal_button(junk):
    assert parse_callback(junk) is None


# ---- a proposal ----------------------------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_a_proposal_is_pushed_with_its_buttons_once(env_factory):  # noqa: F811
    env = await _env(env_factory)
    asked = await ask_with_proposals(env)
    (pid,) = asked.ids
    http = _http()
    publisher = _publisher(env, http)
    assert PUSHED_KINDS == (continuation.MSG_REPORT, continuation.MSG_QUESTION, continuation.MSG_PROPOSAL)
    assert await publisher.push_due(now=_later()) == 1
    body = _payload(http)
    assert body["chat_id"] == "8080" and body["parse_mode"] == "HTML"
    assert body["reply_markup"] == {
        "inline_keyboard": [
            [
                {"text": "Approve", "callback_data": f"f099:p:{pid.hex}:a"},
                {"text": "Reject", "callback_data": f"f099:p:{pid.hex}:r"},
            ]
        ]
    }
    assert body["reply_markup"] == proposal_keyboard(pid)
    assert pid.hex[:8] in body["text"] and "send_email" in body["text"] and "May I email this?" in body["text"]
    row = await _stored(env, (await _row(env, "PROPOSAL")).id)
    assert row.pushed_at is not None and row.push_message_id == 555
    assert await publisher.push_due(now=_later()) == 0 and http.post.await_count == 1  # idempotent


@pytest.mark.postgres_only
async def test_model_text_is_escaped_inside_pre_and_never_outside_it(env_factory):  # noqa: F811
    """Review Focus 4: a rationale, argument or note shaped by an injected result must not become markup, a link or
    a tappable /command. Telegram parses no entity inside <pre>."""
    env = await _env(env_factory)
    _root, got = await claimed(env)
    hostile_args = {
        **SEND_EMAIL_ARGS,
        "body": '<a href="https://evil.example">click</a> /approve deadbeef <b>bold</b> & more </pre><pre>',
    }
    proposal_id = await stage(
        env,
        got,
        arguments=hostile_args,
        rationale="Tap /approve ab12cd34 to confirm <script>alert(1)</script> & </pre>",
    )
    await commit_ask(env, got, note="/reject 12345678 <i>now</i> https://evil.example/pay")
    http = _http()
    assert await _publisher(env, http).push_due(now=_later()) == 1
    text = _payload(http)["text"]
    outside = _outside_pre(text)
    for needle in ("evil", "/approve", "/reject", "<script", "click", "alert", "&amp;", "<a ", "<i>"):
        assert needle not in outside, (needle, outside)
    assert set(re.findall(r"</?(\w+)", outside)) <= {"b", "code"}  # only the publisher's own tags
    blocks = re.findall(r"<pre>(.*?)</pre>", text, flags=re.S)
    assert len(blocks) == 3  # why, the call, the note
    assert all("<" not in block and ">" not in block for block in blocks)
    stored = (await proposal_row(env, proposal_id)).arguments  # jsonb keeps its own key order: show what is stored
    assert html.escape(continuation.render_arguments(stored), quote=False) in text  # verbatim, escaped
    assert "&lt;a href=" in text and "&amp; more" in text and "&lt;/pre&gt;&lt;pre&gt;" in text


@pytest.mark.postgres_only
async def test_a_proposal_message_is_never_truncated(env_factory):  # noqa: F811
    """The longest call, rationale and note the stage-time caps allow fit one message whole: the owner reads all
    of what they approve."""
    env = await _env(env_factory)
    _root, got = await claimed(env)
    args = {"to": "a@example.com", "subject": "s", "body": "z" * 1800}
    assert len(continuation.render_arguments(args)) <= continuation.PROPOSAL_ARGS_MAX_CHARS
    rationale = "r" * continuation.PROPOSAL_RATIONALE_MAX_CHARS
    proposal_id = await stage(env, got, arguments=args, rationale=rationale)
    rendered = continuation.render_arguments((await proposal_row(env, proposal_id)).arguments)  # the stored order
    await commit_ask(env, got, note="n" * 4000)
    http = _http()
    assert await _publisher(env, http).push_due(now=_later()) == 1
    text = _payload(http)["text"]
    assert html.escape(rendered, quote=False) in text and html.escape(rationale, quote=False) in text
    assert _plain_length(text) < 4096  # Telegram's limit, counted after parsing
    assert text.count("<pre>") == text.count("</pre>") == 3  # no tag was cut off


@pytest.mark.postgres_only
async def test_bidi_text_in_a_call_reaches_the_owner_as_an_escape(env_factory):  # noqa: F811
    env = await _env(env_factory)
    _root, got = await claimed(env)
    await stage(env, got, arguments={**SEND_EMAIL_ARGS, "body": "pay \u202eevil"})
    await commit_ask(env, got)
    http = _http()
    await _publisher(env, http).push_due(now=_later())
    text = _payload(http)["text"]
    assert "\u202e" not in text and "\\u202e" in text


@pytest.mark.postgres_only
async def test_a_proposal_that_is_no_longer_pending_is_not_sent_and_does_not_hold_the_queue(env_factory):  # noqa: F811
    env = await _env(env_factory)
    asked = await ask_with_proposals(env)
    (pid,) = asked.ids
    async with env.db.session() as s:
        await continuation.decide_proposal(s, env.agent, pid, approve=False, actor="t", settings=env.settings)
        await s.commit()
    http = _http()
    assert await _publisher(env, http).push_due(now=_later()) == 0 and http.post.await_count == 0
    row = await _stored(env, (await _row(env, "PROPOSAL")).id)
    assert row.pushed_at is not None and row.push_message_id is None  # stamped: nothing is waiting behind it
    assert (await proposal_row(env, pid)).state == "rejected"


@pytest.mark.postgres_only
async def test_a_proposal_deferred_by_quiet_hours_is_pushed_when_they_end(env_factory):  # noqa: F811
    env = await _env(env_factory)
    await ask_with_proposals(env)
    morning = datetime.now(UTC) + timedelta(hours=2)
    async with env.db.session() as s:
        await s.execute(update(ResultInbox).where(ResultInbox.msg_type == "PROPOSAL").values(push_after=morning))
        await s.commit()
    http = _http()
    publisher = _publisher(env, http)
    assert await publisher.push_due(now=morning - timedelta(hours=1)) == 0 and http.post.await_count == 0
    assert await publisher.push_due(now=morning) == 1


@pytest.mark.postgres_only
@pytest.mark.parametrize(("status", "stamped"), [(503, False), (429, False), (400, True), (403, True)])
async def test_a_proposal_push_keeps_the_sent_transient_refused_outcomes(env_factory, status, stamped):  # noqa: F811
    env = await _env(env_factory)
    await ask_with_proposals(env)
    assert await _publisher(env, _http(status=status)).push_due(now=_later()) == 0
    row = await _stored(env, (await _row(env, "PROPOSAL")).id)
    assert (row.pushed_at is not None) is stamped  # an outage stays due; a refusal never blocks the rows behind it


@pytest.mark.postgres_only
async def test_a_staged_proposal_has_no_row_and_nothing_is_pushed(env_factory):  # noqa: F811
    env = await _env(env_factory)
    _root, got = await claimed(env)
    await stage(env, got)  # staged only: the commit that publishes it never happened
    http = _http()
    assert await _publisher(env, http).push_due(now=_later()) == 0 and http.post.await_count == 0


# ---- a question ----------------------------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_a_question_asks_for_a_reply_and_escapes_the_models_text(env_factory):  # noqa: F811
    env = await _env(env_factory)
    _root, got = await claimed(env)
    await commit_ask(env, got, note="Shall I book it? /approve deadbeef <b>now</b>")
    http = _http()
    assert await _publisher(env, http).push_due(now=_later()) == 1
    body = _payload(http)
    assert (
        body["reply_markup"]
        == QUESTION_MARKUP
        == {
            "force_reply": True,
            "input_field_placeholder": "Your answer",
            "selective": False,
        }
    )
    assert body["parse_mode"] == "HTML"
    outside = _outside_pre(body["text"])
    assert "/approve" not in outside and "<b>now" not in body["text"] and "&lt;b&gt;now" in body["text"]
    assert "Shall I book it?" in body["text"]
    row = await _stored(env, (await _row(env, "QUESTION")).id)
    assert row.push_message_id == 555  # a reply to this message is the answer (2d-7 looks it up)


def test_the_renderer_puts_every_model_authored_field_in_a_pre_block():
    proposal = SimpleNamespace(
        id=uuid.uuid4(),
        tool="send_email",
        rationale="<x>why</x>",
        arguments={"k": "<y>"},
        deadline=datetime(2026, 10, 8, 9, 30, tzinfo=UTC),
    )
    text = render_proposal_html(proposal, "<z>note</z>")
    assert text.count("<pre>") == 3 and "Expires 2026-10-08 09:30 UTC" in text
    assert not {"<x>", "<y>", "<z>"} & set(re.findall(r"<[a-z]>", text))


# ---- lead addendum 1 (2d-1 and 2d-2 reviews): the rationale and the note, escaped and measured in UTF-16 -------


def _utf16_length(text: str) -> int:
    """Telegram's own count: UTF-16 units of the text after its entities are parsed (an emoji is two)."""
    return len(html.unescape(re.sub(r"<[^>]+>", "", text)).encode("utf-16-le")) // 2


BIDI = "\u202e"
EMOJI = "\U0001f600"


@pytest.mark.postgres_only
async def test_a_bidi_rationale_and_note_reach_the_owner_as_escapes(env_factory):  # noqa: F811
    """A right-to-left override in the rationale or the note would reorder what the owner reads: both are shown
    with the escape render_arguments uses, on Telegram and in the row a chat turn reads."""
    env = await _env(env_factory)
    _root, got = await claimed(env)
    await stage(env, got, rationale=f"It is safe {BIDI}gnihton syap")
    await commit_ask(env, got, note=f"Nous {BIDI}seyas")
    http = _http()
    assert await _publisher(env, http).push_due(now=_later()) == 1
    text = _payload(http)["text"]
    assert BIDI not in text and text.count("\\u202e") == 2
    body = (await _row(env, "PROPOSAL")).body
    assert BIDI not in body and body.count("\\u202e") == 2


@pytest.mark.postgres_only
async def test_a_rationale_is_capped_as_the_owner_is_shown_it(env_factory):  # noqa: F811
    """Escaping lengthens a rationale (one override is six characters shown), and the push never truncates: the
    stage-time cap measures what the owner will read, as it does for the call."""
    env = await _env(env_factory)
    _root, got = await claimed(env)
    with pytest.raises(continuation.ProposalRefused, match="rationale is too long"):
        await stage(env, got, rationale="Why " + BIDI * 200)  # 204 units raw, 1204 shown


@pytest.mark.postgres_only
async def test_an_emoji_heavy_proposal_fits_one_telegram_message(env_factory):  # noqa: F811
    """Telegram counts UTF-16 units, and an emoji is two: the call, the rationale and the note at their caps, all
    astral, still make one message of at most 4096 units, with nothing cut but the note."""
    env = await _env(env_factory)
    _root, got = await claimed(env)
    args = {"to": "a@example.com", "subject": "s", "body": EMOJI * 960}
    assert continuation.utf16_units(continuation.render_arguments(args)) <= continuation.PROPOSAL_ARGS_MAX_CHARS
    rationale = EMOJI * (continuation.PROPOSAL_RATIONALE_MAX_CHARS // 2)
    proposal_id = await stage(env, got, arguments=args, rationale=rationale)
    rendered = continuation.render_arguments((await proposal_row(env, proposal_id)).arguments)
    await commit_ask(env, got, note=EMOJI * 4000)
    http = _http()
    assert await _publisher(env, http).push_due(now=_later()) == 1
    text = _payload(http)["text"]
    assert _utf16_length(text) <= 4096
    assert html.escape(rendered, quote=False) in text and rationale in text  # the call and the why, whole
    note_block = re.findall(r"<pre>(.*?)</pre>", text, flags=re.S)[2]
    assert note_block == EMOJI * (continuation.PROPOSAL_NOTE_MAX_CHARS // 2)  # clipped in units, not characters
    body = (await _row(env, "PROPOSAL")).body  # the chat row clips the note the same way
    assert body.endswith("Nous says: " + EMOJI * (continuation.PROPOSAL_NOTE_MAX_CHARS // 2))


def test_the_note_clip_never_cuts_inside_an_escape():
    shown = continuation.proposal_note("a" * 597 + BIDI)  # the escape would end past the cap
    assert shown == "a" * 597
    assert continuation.clip_shown("abc" + BIDI, 8, marker="!") == "abc!"
    assert continuation.clip_shown("ab" + BIDI, 8) == "ab\\u202e"  # fits exactly: not cut


@pytest.mark.postgres_only
async def test_a_question_is_escaped_and_fits_in_utf16_units(env_factory):  # noqa: F811
    env = await _env(env_factory)
    _root, got = await claimed(env)
    await commit_ask(env, got, note=f"Book {BIDI}it? " + EMOJI * 3000)
    http = _http()
    assert await _publisher(env, http).push_due(now=_later()) == 1
    text = _payload(http)["text"]
    assert BIDI not in text and "\\u202e" in text
    assert _utf16_length(text) <= 4096 and "[truncated]" in text
    assert text.count("<pre>") == text.count("</pre>") == 1
