"""F099 Phase 2c-2: the owner push: due rows go to Telegram once, quiet hours defer only the push."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from f099_support import CHAN, CONT, env_factory, make_root, record, set_intention  # noqa: F401
from sqlalchemy import select, update

from nous.brain import continuation
from nous.handlers.continuation_publisher import OwnerPublisher
from nous.storage.models import ResultInbox

pytestmark = pytest.mark.postgres_only

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


def _http(*, status=200, message_id=777):
    http = MagicMock()
    http.post = AsyncMock(
        return_value=SimpleNamespace(
            status_code=status, json=lambda: {"ok": True, "result": {"message_id": message_id}}
        )
    )
    return http


async def _env(env_factory, **over):  # noqa: F811
    return await env_factory(**CONT, telegram_bot_token="test-token", telegram_chat_id="8080", **over)


async def _row(env, *, kind=continuation.MSG_REPORT, channel=CHAN, push_after=NOW, title="Update: snow", body="Deep."):
    root = await make_root(env)
    async with env.db.session() as s:
        report_id = await continuation.insert_report(
            s,
            env.agent,
            kind=kind,
            title=title,
            body=body,
            channel=channel,
            intention_id=root.id,
            root_id=root.id,
            push_after=push_after,
        )
        await s.commit()
    return report_id


async def _stored(env, report_id) -> ResultInbox:
    async with env.db.session() as s:
        return (await s.execute(select(ResultInbox).where(ResultInbox.source_id == report_id))).scalar_one()


def _publisher(env, http) -> OwnerPublisher:
    return OwnerPublisher(database=env.db, settings=env.settings, http_client=http)


async def test_a_due_row_is_pushed_once_with_its_message_id(env_factory):  # noqa: F811
    env = await _env(env_factory)
    report_id = await _row(env)
    http = _http()
    publisher = _publisher(env, http)
    assert await publisher.push_due(now=NOW) == 1
    ((url,), kwargs) = http.post.call_args
    assert url == "https://api.telegram.org/bottest-token/sendMessage"
    assert kwargs["json"] == {"chat_id": "8080", "text": "Update: snow\n\nDeep."}
    row = await _stored(env, report_id)
    assert row.pushed_at is not None and row.push_message_id == 777
    assert await publisher.push_due(now=NOW) == 0  # idempotent: the stamp keys it
    assert http.post.await_count == 1


async def test_a_question_is_pushed_too(env_factory):  # noqa: F811
    env = await _env(env_factory)
    await _row(env, kind=continuation.MSG_QUESTION, title="Question: trip", body="Shall I book it?")
    http = _http()
    assert await _publisher(env, http).push_due(now=NOW) == 1
    assert "Shall I book it?" in http.post.call_args.kwargs["json"]["text"]


async def test_a_long_body_is_capped_at_the_inbox_body_cap(env_factory):  # noqa: F811
    """Carry-over 7: raw_results_text (gate, fallback and expiry reports) is unbounded; the push is not."""
    env = await _env(env_factory, result_inbox_body_max_chars=200)
    await _row(env, body="x" * 1000)
    http = _http()
    assert await _publisher(env, http).push_due(now=NOW) == 1
    text = http.post.call_args.kwargs["json"]["text"]
    assert text.endswith("[truncated]") and len(text) < 260
    assert "[truncated]" in continuation.clip_body("x" * 9000, env.settings)  # one marker, one source


async def test_a_proposal_is_not_pushed_until_2d(env_factory):  # noqa: F811
    env = await _env(env_factory)
    await _row(env, kind=continuation.MSG_PROPOSAL)
    http = _http()
    assert await _publisher(env, http).push_due(now=NOW) == 0 and http.post.await_count == 0


async def test_a_row_deferred_by_quiet_hours_is_pushed_when_they_end(env_factory):  # noqa: F811
    env = await _env(env_factory)
    morning = datetime(2026, 10, 7, 8, 0, tzinfo=UTC)
    report_id = await _row(env, push_after=morning)  # written at night with the end of the quiet hours
    http = _http()
    publisher = _publisher(env, http)
    assert await publisher.push_due(now=morning - timedelta(hours=1)) == 0  # still quiet
    assert (await _stored(env, report_id)).pushed_at is None  # the row is there, only the push waits
    assert await publisher.push_due(now=morning) == 1


async def test_a_row_with_no_push_time_is_never_pushed(env_factory):  # noqa: F811  # PIN (rows written before 2c)
    env = await _env(env_factory)
    await _row(env, push_after=None)
    http = _http()
    assert await _publisher(env, http).push_due(now=NOW) == 0 and http.post.await_count == 0


async def test_a_row_for_another_channel_is_never_pushed(env_factory):  # noqa: F811
    env = await _env(env_factory)
    await _row(env, channel="rest:abc")
    http = _http()
    assert await _publisher(env, http).push_due(now=NOW) == 0 and http.post.await_count == 0


async def test_the_chat_a_row_is_addressed_to_is_the_one_it_goes_to(env_factory):  # noqa: F811
    env = await _env(env_factory)
    await _row(env, channel="telegram:4242")
    http = _http()
    await _publisher(env, http).push_due(now=NOW)
    assert http.post.call_args.kwargs["json"]["chat_id"] == "4242"


async def test_a_row_older_than_the_inbox_window_is_left_alone(env_factory):  # noqa: F811
    env = await _env(env_factory)
    report_id = await _row(env)
    async with env.db.session() as s:
        await s.execute(
            update(ResultInbox).where(ResultInbox.source_id == report_id).values(created_at=NOW - timedelta(hours=100))
        )
        await s.commit()
    http = _http()
    assert await _publisher(env, http).push_due(now=NOW) == 0


@pytest.mark.parametrize("failure", ["status", "raises"])
async def test_a_failed_send_leaves_the_row_due_and_stops_the_batch(env_factory, failure):  # noqa: F811
    env = await _env(env_factory)
    first, second = await _row(env), await _row(env)
    http = _http(status=500) if failure == "status" else MagicMock(post=AsyncMock(side_effect=ConnectionError("down")))
    publisher = _publisher(env, http)
    assert await publisher.push_due(now=NOW) == 0
    assert http.post.await_count == 1  # the second row was not tried: an outage is not hammered
    assert (await _stored(env, first)).pushed_at is None and (await _stored(env, second)).pushed_at is None
    healthy = _http()
    assert await _publisher(env, healthy).push_due(now=NOW) == 2  # the next sweep sends both


async def test_a_failure_never_logs_the_bot_token(env_factory, caplog):  # noqa: F811
    env = await _env(env_factory)
    await _row(env)
    http = MagicMock(post=AsyncMock(side_effect=ConnectionError("https://api.telegram.org/bottest-token/sendMessage")))
    await _publisher(env, http).push_due(now=NOW)
    assert "test-token" not in caplog.text and "ConnectionError" in caplog.text


async def test_without_a_bot_token_nothing_is_attempted(env_factory):  # noqa: F811
    env = await env_factory(**CONT, telegram_bot_token="", telegram_chat_id="8080")
    await _row(env)
    http = _http()
    assert await _publisher(env, http).push_due(now=NOW) == 0 and http.post.await_count == 0


async def test_a_reported_late_result_reaches_telegram_too(env_factory):  # noqa: F811
    """R2: a result nothing can reopen is a REPORT in chat AND on Telegram. 2b's record_result wrote it with
    no push time, so it never left chat."""
    env = await _env(env_factory)
    root = await make_root(env)
    await set_intention(env, root.id, root_cancelled_at=datetime.now(UTC))  # the root is closed
    recorded = await record(env, root, body="it finished after the cancel")
    assert recorded.reported is True
    (report,) = [r for r in await _owner_rows(env)]
    assert report.push_after is not None  # pinned directly, not only through the publisher
    expected = continuation.push_after_for(env.settings, datetime.now(UTC))
    assert abs((report.push_after - expected).total_seconds()) < 5
    http = _http()
    assert await _publisher(env, http).push_due(now=datetime.now(UTC) + timedelta(days=1)) == 1  # past any quiet hours
    assert "it finished after the cancel" in http.post.call_args.kwargs["json"]["text"]


async def _owner_rows(env):
    async with env.db.session() as s:
        query = select(ResultInbox).where(
            ResultInbox.agent_id == env.agent, ResultInbox.source_kind == continuation.SOURCE_INTENTION_REPORT
        )
        return list((await s.execute(query)).scalars().all())


async def test_with_continuation_off_the_publisher_touches_nothing():  # PIN
    from nous.config import Settings

    class NoDatabase:
        def session(self):
            raise AssertionError("the publisher touched the database with continuation off")

    http = _http()
    settings = Settings(_env_file=None, result_inbox_enabled=True, intentions_enabled=True, telegram_bot_token="t")
    publisher = OwnerPublisher(database=NoDatabase(), settings=settings, http_client=http)
    assert await publisher.push_due() == 0 and http.post.await_count == 0
