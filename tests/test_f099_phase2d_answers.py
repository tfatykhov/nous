"""F099 Phase 2d-3: the owner's answer to a question, in the store (spec 4.4 Questions, ruling R8)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from f099_support import (
    CONT,
    ask_with_proposals,
    claim,
    claimed,
    commit_ask,
    env_factory,  # noqa: F401
    inbox_rows,
    intention_of,
    make_child,
    make_root,
    record,
    set_intention,
    until_a_backend_waits_on_a_lock,
)
from sqlalchemy import select, update

from nous.brain import continuation, intentions
from nous.brain.continuation import Resolution
from nous.storage.models import Intention, ResultInbox

pytestmark = pytest.mark.postgres_only


def _high_then_low_ids(monkeypatch):
    """The next root sorts AFTER its child: ``prepare_intention`` draws a high id, then a low one."""
    high, low = uuid.uuid4().hex, uuid.uuid4().hex
    ids = iter([uuid.UUID("ff" + high[2:]), uuid.UUID("00" + low[2:])])
    monkeypatch.setattr(intentions, "uuid", SimpleNamespace(uuid4=lambda: next(ids)))


async def _question_id(env, arrival_id):
    async with env.db.session() as s:
        return (
            await s.execute(
                select(ResultInbox.source_id).where(
                    ResultInbox.agent_id == env.agent,
                    ResultInbox.arrival_id == arrival_id,
                    ResultInbox.msg_type == "QUESTION",
                )
            )
        ).scalar_one()


async def _ask(env, root=None, note="May I book the Friday slot?"):
    """A root with a result, claimed and asked about: ``(root, question id, commit)``."""
    root = root or await make_root(env)
    await record(env, root)
    got = await claim(env, root.id)
    done = await commit_ask(env, got, note)
    return root, await _question_id(env, done.arrival_id), done


async def _answer(env, question_id, text="Yes, book it.", **kwargs):
    async with env.db.session() as s:
        recorded = await continuation.record_answer(
            s,
            env.agent,
            question_id,
            text=text,
            actor=kwargs.pop("actor", "telegram:42"),
            settings=env.settings,
            **kwargs,
        )
        await s.commit()
    return recorded


async def _owner_rows(env):
    return [row for row in await inbox_rows(env) if row.source_kind == "intention_report" and row.channel]


async def _informs(env, arrival_id):
    return [r for r in await inbox_rows(env) if r.msg_type == "INFORM" and r.arrival_id == arrival_id]


async def _age_question(env, arrival_id, hours=25):
    async with env.db.session() as s:
        await s.execute(
            update(ResultInbox)
            .where(ResultInbox.arrival_id == arrival_id, ResultInbox.msg_type == "QUESTION")
            .values(created_at=datetime.now(UTC) - timedelta(hours=hours))
        )
        await s.commit()


async def test_an_answer_becomes_the_next_result_and_wakes_the_arrival(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root, qid, done = await _ask(env)
    recorded = await _answer(env, qid, "Yes, book it.", actor="telegram:42")
    assert (recorded.question_id, recorded.arrival_id, recorded.intention_ids, recorded.woke_arrival) == (
        qid,
        done.arrival_id,
        (root.id,),
        True,
    )
    assert (await intention_of(env, "subtask", root.source_id)).state == "result_ready"
    (row,) = await _informs(env, done.arrival_id)
    assert (row.title, row.body, row.channel, row.intention_id) == ("Owner's answer", "Yes, book it.", None, root.id)
    assert row.correlation_id == "owner-answer:telegram:42" and row.delivered_at is None
    got = await claim(env, root.id)
    assert [r.body for r in got.inbox_rows if r.msg_type == "INFORM"] == ["Yes, book it."]


async def test_a_second_answer_is_refused_and_writes_nothing(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    _root, qid, done = await _ask(env)
    await _answer(env, qid)
    with pytest.raises(continuation.AnswerRefused) as refused:
        await _answer(env, qid, "No, wait.")
    assert refused.value.reason == "answered" and len(await _informs(env, done.arrival_id)) == 1


async def test_an_answer_to_work_that_ended_is_refused_and_never_becomes_a_raw_report(env_factory):  # noqa: F811
    """R8: record_result's closed-root branch would turn the answer into a REPORT to the owner."""
    env = await env_factory(**CONT)
    root, qid, _done = await _ask(env)
    await set_intention(env, root.id, deadline=datetime.now(UTC) - timedelta(hours=1))
    async with env.db.session() as s:
        assert await continuation.expire_roots(s, env.agent, ttl_hours=72.0, settings=env.settings) == [root.id]
        await s.commit()
    before = len(await inbox_rows(env))
    with pytest.raises(continuation.AnswerRefused) as refused:
        await _answer(env, qid)
    assert refused.value.reason == "ended" and len(await inbox_rows(env)) == before


async def test_an_answer_to_a_question_past_its_deadline_is_refused(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    _root, qid, done = await _ask(env)
    await _age_question(env, done.arrival_id)
    before = len(await inbox_rows(env))
    with pytest.raises(continuation.AnswerRefused) as refused:
        await _answer(env, qid)
    assert refused.value.reason == "expired" and len(await inbox_rows(env)) == before


async def test_an_answer_after_the_sweep_woke_an_expired_question_is_refused(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root, qid, done = await _ask(env)
    await _age_question(env, done.arrival_id)
    async with env.db.session() as s:
        assert await continuation.wake_terminal_arrivals(s, env.agent, settings=env.settings) == [root.id]
        await s.commit()
    with pytest.raises(continuation.AnswerRefused) as refused:
        await _answer(env, qid)
    assert refused.value.reason == "expired"


async def test_an_unknown_id_and_a_proposal_id_are_not_questions(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    with pytest.raises(continuation.QuestionNotFound):
        await _answer(env, uuid.uuid4())
    asked = await ask_with_proposals(env)
    with pytest.raises(continuation.QuestionNotFound):
        await _answer(env, asked.ids[0])  # a PROPOSAL row is not a question, though it shares the inbox


async def test_an_answer_reaches_every_intention_of_the_asking_arrival(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await make_root(env)
    child = await make_child(env, root)
    await record(env, child)
    _r, qid, done = await _ask(env, root)  # claims both
    recorded = await _answer(env, qid)
    assert set(recorded.intention_ids) == {root.id, child.id} and recorded.woke_arrival
    assert len(await _informs(env, done.arrival_id)) == 2
    for intention in (root, child):
        assert (await intention_of(env, "subtask", intention.source_id)).state == "result_ready"


async def test_questions_are_found_by_a_unique_prefix_and_by_the_telegram_message_they_were_pushed_as(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    _root, qid, _done = await _ask(env)
    async with env.db.session() as s:
        assert await continuation.find_question_id(s, env.agent, qid.hex[:8]) == qid
        assert await continuation.find_question_id(s, env.agent, "ffffffff") is None
        assert await continuation.find_question_id_by_message(s, env.agent, chat_id=8080, message_id=777) is None
        await s.execute(
            update(ResultInbox)
            .where(ResultInbox.source_id == qid)
            .values(push_message_id=777, pushed_at=datetime.now(UTC))
        )
        await s.commit()
    async with env.db.session() as s:
        assert await continuation.find_question_id_by_message(s, env.agent, chat_id=8080, message_id=777) == qid
        assert await continuation.find_question_id_by_message(s, env.agent, chat_id=9999, message_id=777) is None
        assert await continuation.find_question_id_by_message(s, env.agent, chat_id=8080, message_id=778) is None


# ---- the races -----------------------------------------------------------------------------------------------


async def test_an_answer_racing_the_commit_that_publishes_its_question_sees_nothing_until_it_commits(env_factory):  # noqa: F811
    """The question and the state it waits in are one commit: before it, there is no question to answer (not found);
    after it, the answer finds the intention awaiting and the arrival woken."""
    env = await env_factory(**CONT)
    root, got = await claimed(env)
    async with env.db.session() as committing:
        await continuation.commit_arrival(
            committing,
            env.agent,
            got,
            resolution=Resolution("ask", "May I?", True, 0.8),
            outcome="resolved",
            settings=env.settings,
        )
        qid = (
            await committing.execute(
                select(ResultInbox.source_id).where(
                    ResultInbox.agent_id == env.agent, ResultInbox.msg_type == "QUESTION"
                )
            )
        ).scalar_one()  # visible to the transaction that wrote it, and to nobody else
        with pytest.raises(continuation.QuestionNotFound):
            await _answer(env, qid)
        await committing.commit()
    recorded = await _answer(env, qid)
    assert (
        recorded.woke_arrival is True and (await intention_of(env, "subtask", root.source_id)).state == "result_ready"
    )


async def _hold_root(session, root_id):
    await session.execute(select(Intention.id).where(Intention.id == root_id).with_for_update(key_share=True))


async def test_an_answer_and_an_expiry_that_meet_on_a_low_id_child_do_not_deadlock(env_factory, monkeypatch):  # noqa: F811
    """The lock order (root first). The arrival lists the low-id child before the high-id root: a record_answer that
    locked the awaiting intentions in that order before the root would hold the child while the expiry holds the
    root and wants it. Both finish, whichever is granted the root first."""
    _high_then_low_ids(monkeypatch)
    env = await env_factory(**CONT)
    root = await make_root(env)  # the high id
    child = await make_child(env, root)  # the low id
    assert root.id > child.id
    await record(env, root)
    await record(env, child)
    got = await claim(env, root.id)
    done = await commit_ask(env, got, "Shall I?")
    qid = await _question_id(env, done.arrival_id)
    await set_intention(env, root.id, deadline=datetime.now(UTC) - timedelta(hours=1))

    async def expire():
        async with env.db.session() as s:
            moved = await continuation.expire_roots(s, env.agent, ttl_hours=72.0, settings=env.settings)
            await s.commit()
        return moved

    async def answer():
        try:
            return await _answer(env, qid)
        except continuation.AnswerRefused as refused:
            return refused

    async with env.db.session() as holder:
        await _hold_root(holder, root.id)  # the root is busy: whoever wants it first waits
        sweep = asyncio.create_task(expire())
        await asyncio.wait_for(until_a_backend_waits_on_a_lock(env), timeout=10)
        late = asyncio.create_task(answer())
        try:
            await asyncio.wait_for(until_a_backend_waits_on_a_lock(env, at_least=2), timeout=10)
        finally:
            await holder.commit()
    expired = await asyncio.wait_for(sweep, timeout=30)  # a deadlock error on either side raises here
    result = await asyncio.wait_for(late, timeout=30)
    # The order the two waiters are granted the root in is Postgres's: the expiry first (the answer is refused as
    # ended), or the answer first (recorded, then the expiry closes the lineage). Never a deadlock, and never a
    # raw report made of the answer.
    assert (expired, isinstance(result, continuation.AnswerRefused)) in (([root.id], True), ([root.id], False))
    assert not [r for r in await _owner_rows(env) if r.msg_type == "REPORT" and "Owner's answer" in r.title]


async def test_an_answer_and_a_commit_on_the_same_root_do_not_deadlock(env_factory, monkeypatch):  # noqa: F811
    """record_answer against commit_arrival, the other root-first path. The commit holds the claimed low-id child
    and wants nothing else; the answer holds the root and wants the awaiting high-id root intention."""
    _high_then_low_ids(monkeypatch)
    env = await env_factory(**CONT)
    root = await make_root(env)  # the high id
    child = await make_child(env, root)  # the low id
    await record(env, root)
    asked = await claim(env, root.id)
    done = await commit_ask(env, asked, "Shall I?")
    qid = await _question_id(env, done.arrival_id)
    await record(env, child)
    second = await claim(env, root.id)
    assert {i.id for i in second.intentions} == {child.id}

    async def commit():
        async with env.db.session() as s:
            out = await continuation.commit_arrival(
                s,
                env.agent,
                second,
                resolution=Resolution("drop", "Done.", False, 0.9),
                outcome="resolved",
                settings=env.settings,
            )
            await s.commit()
        return out

    async with env.db.session() as holder:
        await _hold_root(holder, root.id)
        committed = asyncio.create_task(commit())
        await asyncio.wait_for(until_a_backend_waits_on_a_lock(env), timeout=10)
        answered = asyncio.create_task(_answer(env, qid))
        try:
            await asyncio.wait_for(until_a_backend_waits_on_a_lock(env, at_least=2), timeout=10)
        finally:
            await holder.commit()
    assert await asyncio.wait_for(committed, timeout=30) is not None
    assert (await asyncio.wait_for(answered, timeout=30)).woke_arrival is True
