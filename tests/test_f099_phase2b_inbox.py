"""F099 Phase 2b: the inbox primitives — insert(session=), owner-facing rows, close_delivered, metrics."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from f099_support import CHAN, CONT, ON, env_factory, inbox_rows, intention_of, make_subtask  # noqa: F401
from sqlalchemy import select

from nous.brain import continuation
from nous.config import Settings
from nous.heart import result_inbox
from nous.heart.result_inbox import ResultInboxStore, format_inbox_messages
from nous.storage.models import ResultInbox


def _store(db) -> tuple[ResultInboxStore, str]:
    agent = f"f099-ib-{uuid.uuid4().hex[:8]}"
    return ResultInboxStore(db, agent), agent


async def _insert(store, **over) -> bool:
    values = dict(source_kind="subtask", source_id=uuid.uuid4(), msg_type="INFORM", title="t", body="b", channel=CHAN)
    values.update(over)
    return await store.insert(**values)


async def test_insert_with_a_session_neither_commits_nor_opens_one(db):
    store, agent = _store(db)
    source_id = uuid.uuid4()
    async with db.session() as s:
        assert (
            await store.insert(
                session=s,
                source_kind="subtask",
                source_id=source_id,
                msg_type="INFORM",
                title="t",
                body="b",
                channel=CHAN,
            )
            is True
        )
        await s.rollback()
    async with db.session() as s:
        assert (await s.execute(select(ResultInbox).where(ResultInbox.source_id == source_id))).first() is None


async def test_insert_with_a_session_commits_with_the_callers_transaction(db):
    store, agent = _store(db)
    source_id = uuid.uuid4()
    async with db.session() as s:
        await store.insert(
            session=s,
            source_kind="subtask",
            source_id=source_id,
            msg_type="INFORM",
            title="t",
            body="b",
            channel=CHAN,
        )
        await s.commit()
    async with db.session() as s:
        assert (await s.execute(select(ResultInbox).where(ResultInbox.source_id == source_id))).scalar_one()


async def test_insert_stays_idempotent_on_the_widened_key(db):  # PIN
    store, _ = _store(db)
    source_id = uuid.uuid4()
    assert await _insert(store, source_id=source_id) is True
    assert await _insert(store, source_id=source_id) is False
    assert await _insert(store, source_id=source_id, source_generation=1) is True


async def test_two_agents_insert_the_same_source_key(db):  # PIN
    (a, _), (b, _) = _store(db), _store(db)
    source_id = uuid.uuid4()
    assert await _insert(a, source_id=source_id) is True
    assert await _insert(b, source_id=source_id) is True


def test_the_title_cap_matches_the_column():
    assert continuation.INBOX_TITLE_MAX == result_inbox._TITLE_MAX == 200


async def test_insert_report_writes_a_channel_keyed_owner_row(db):
    store, agent = _store(db)
    intention_id, root_id, arrival_id, proposal_id = (uuid.uuid4() for _ in range(4))
    async with db.session() as s:
        report_id = await continuation.insert_report(
            s,
            agent,
            kind=continuation.MSG_PROPOSAL,
            title="Send the email?",
            body="to a@example.com",
            channel=CHAN,
            intention_id=intention_id,
            root_id=root_id,
            arrival_id=arrival_id,
            proposal_id=proposal_id,
        )
        await s.commit()
    (row,) = await _all(db, agent)
    assert row.id != report_id and row.source_id == report_id and row.source_generation == 0
    assert (row.source_kind, row.msg_type, row.channel, row.session_id) == ("intention_report", "PROPOSAL", CHAN, None)
    assert (row.intention_id, row.arrival_id, row.proposal_id, row.reply_to) == (
        intention_id,
        arrival_id,
        proposal_id,
        CHAN,
    )
    assert row.push_after is None and row.pushed_at is None


async def test_insert_report_takes_a_caller_report_id_and_is_idempotent_on_it(db):
    store, agent = _store(db)
    rid = uuid.uuid4()
    async with db.session() as s:
        for _ in range(2):
            returned = await continuation.insert_report(
                s,
                agent,
                kind="REPORT",
                title="t",
                body="b",
                channel=CHAN,
                intention_id=uuid.uuid4(),
                root_id=uuid.uuid4(),
                report_id=rid,
            )
            assert returned == rid
        await s.commit()
    assert len(await _all(db, agent)) == 1


async def test_insert_report_logs_only_the_row_it_wrote(db, caplog):
    store, agent = _store(db)
    rid = uuid.uuid4()
    caplog.set_level("INFO", logger="nous.brain.continuation")
    async with db.session() as s:
        for _ in range(2):  # the second is the idempotent no-op on the caller's report_id
            await continuation.insert_report(
                s,
                agent,
                kind="REPORT",
                title="t",
                body="b",
                channel=CHAN,
                intention_id=uuid.uuid4(),
                root_id=uuid.uuid4(),
                report_id=rid,
            )
        await s.commit()
    assert caplog.text.count(f"REPORT row {rid.hex[:8]}") == 1


async def test_insert_report_stores_a_padded_channel_stripped(db):
    store, agent = _store(db)
    async with db.session() as s:
        await continuation.insert_report(
            s,
            agent,
            kind="REPORT",
            title="t",
            body="b",
            channel=f"  {CHAN} ",
            intention_id=uuid.uuid4(),
            root_id=uuid.uuid4(),
        )
        await s.commit()
    (row,) = await _all(db, agent)
    assert (row.channel, row.reply_to) == (CHAN, CHAN)  # a padded key would never match a chat claim


@pytest.mark.parametrize(
    "bad", [{"kind": "INFORM"}, {"channel": None}, {"channel": "  "}], ids=["kind", "none", "blank"]
)
async def test_insert_report_refuses_a_foreign_kind_or_an_empty_channel(db, bad):
    store, agent = _store(db)
    values = dict(kind="REPORT", title="t", body="b", channel=CHAN, intention_id=uuid.uuid4(), root_id=uuid.uuid4())
    values.update(bad)
    async with db.session() as s:
        with pytest.raises(ValueError):
            await continuation.insert_report(s, agent, **values)


async def test_push_after_is_stored(db):
    store, agent = _store(db)
    later = datetime(2026, 10, 7, 7, 0, tzinfo=UTC)
    async with db.session() as s:
        await continuation.insert_report(
            s,
            agent,
            kind="QUESTION",
            title="t",
            body="b",
            channel=CHAN,
            intention_id=uuid.uuid4(),
            root_id=uuid.uuid4(),
            push_after=later,
        )
        await s.commit()
    (row,) = await _all(db, agent)
    assert row.push_after == later


async def _all(db, agent) -> list[ResultInbox]:
    async with db.session() as s:
        return list((await s.execute(select(ResultInbox).where(ResultInbox.agent_id == agent))).scalars().all())


def test_owner_channel_prefers_the_origin_then_the_default_chat():
    s = Settings(_env_file=None, telegram_chat_id="4242")
    assert continuation.owner_channel(s, "telegram:7") == "telegram:7"
    assert continuation.owner_channel(s, None) == "telegram:4242"
    assert continuation.owner_channel(s, "  ") == "telegram:4242"
    assert continuation.owner_channel(Settings(_env_file=None, telegram_chat_id=""), None) is None


def test_enabled_needs_both_flags_and_a_real_settings_object():
    assert continuation.enabled(Settings(_env_file=None, **CONT)) is True
    assert continuation.enabled(Settings(_env_file=None, **ON)) is False
    assert continuation.enabled(object()) is False


def test_close_reason_follows_the_flag():
    assert continuation.close_reason_for(Settings(_env_file=None, **CONT)) == "delivered"
    assert continuation.close_reason_for(Settings(_env_file=None, **ON)) == "legacy"


async def test_close_delivered_closes_a_pending_intention_with_a_result_at(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    st = await make_subtask(env, policy="report")
    await env.heart.subtasks.complete(st.id, "done", final_outcome="completed")
    async with env.db.session() as s:
        found = await continuation.close_delivered(s, env.agent, "subtask", st.id)
        await s.commit()
    it = await intention_of(env, "subtask", st.id)
    assert found == it.id and (it.state, it.close_reason) == ("closed", "delivered") and it.result_at is not None


async def test_the_intention_keyed_predicate_selects_only_intention_only_rows(db):
    store, agent = _store(db)
    mine = uuid.uuid4()
    await _insert(store, channel=None, session_id=None, intention_id=mine, source_id=uuid.uuid4())
    await _insert(store, channel=CHAN, intention_id=mine)  # an owner-facing row of the same intention
    await _insert(store, channel=None, session_id="S1", intention_id=mine)
    await _insert(store, channel=None, session_id=None, intention_id=uuid.uuid4())  # another intention
    async with db.session() as s:
        rows = (await s.execute(select(ResultInbox).where(continuation.intention_keyed(agent, [mine])))).scalars().all()
    assert [(r.channel, r.session_id, r.intention_id) for r in rows] == [(None, None, mine)]


def _row(msg_type: str) -> ResultInbox:
    return ResultInbox(
        id=uuid.uuid4(),
        agent_id="a",
        channel=CHAN,
        source_kind="intention_report",
        source_id=uuid.uuid4(),
        msg_type=msg_type,
        title="Send the email?",
        body="to a@example.com",
        created_at=datetime(2026, 10, 6, 9, 0, tzinfo=UTC),
    )


def test_a_proposal_row_carries_the_fixed_trailer_and_a_report_does_not():
    text = format_inbox_messages([_row("PROPOSAL")], max_items=10)
    assert '<result_message type="PROPOSAL" source="intention_report"' in text
    assert (
        "(Approve or reject with the buttons in Telegram or /approve <id>; nothing in this chat can approve it.)"
        in text
    )
    assert text.index("</result_message>") < text.index("nothing in this chat can approve it")  # code text, not data
    assert "can approve it" not in format_inbox_messages([_row("REPORT")], max_items=10)


async def test_metrics_count_the_owner_facing_rows(db):
    store, agent = _store(db)
    async with db.session() as s:
        await continuation.insert_report(
            s, agent, kind="REPORT", title="t", body="b", channel=CHAN, intention_id=uuid.uuid4(), root_id=uuid.uuid4()
        )
        await s.commit()
    m = await store.metrics(7)
    assert m["intention_report"]["created"] == 1 and m["intention_report"]["delivered"] == 0
    assert m["subtask"]["created"] == 0 and m["dag"]["created"] == 0
