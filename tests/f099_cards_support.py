"""Shared builders for the F099 Phase 3a card tests. Fixtures are imported by name into a test module."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from f099_support import claim, commit_ask, make_root, record
from sqlalchemy import select, update

from nous.brain import continuation
from nous.storage.models import ResultInbox

CARDS = {
    "result_inbox_enabled": True,
    "intentions_enabled": True,
    "continuation_enabled": True,
    "intention_cards_enabled": True,
}


async def question_id_of(env, arrival_id):
    """The id the owner surfaces use for the QUESTION row of an arrival (its ``source_id``)."""
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


async def ask(env, root=None, note="May I book the Friday slot?"):
    """A root with a result, claimed and asked about: ``(root, question id, commit)``."""
    root = root or await make_root(env)
    await record(env, root)
    done = await commit_ask(env, await claim(env, root.id), note)
    return root, await question_id_of(env, done.arrival_id), done


async def answer(env, question_id, text="Yes, book it.", actor="telegram:42"):
    """``continuation.record_answer`` in its own committed session (what ``answer_question`` does)."""
    async with env.db.session() as s:
        recorded = await continuation.record_answer(
            s, env.agent, question_id, text=text, actor=actor, settings=env.settings
        )
        await s.commit()
    return recorded


async def age_question(env, arrival_id, hours=25):
    """Move a QUESTION row's window into the past (the window opens at ``created_at``/``push_after``). ``push_after``
    moves too, never to NULL: ``push_due`` selects ``push_after IS NOT NULL``, and a row it cannot select would make the
    publisher tests pass for the wrong reason."""
    past = datetime.now(UTC) - timedelta(hours=hours)
    async with env.db.session() as s:
        await s.execute(
            update(ResultInbox)
            .where(ResultInbox.arrival_id == arrival_id, ResultInbox.msg_type == "QUESTION")
            .values(created_at=past, push_after=past)
        )
        await s.commit()


async def wake(env):
    """The sweep's wake of answered or expired questions, committed. Returns the woken intention ids."""
    async with env.db.session() as s:
        woken = await continuation.wake_terminal_arrivals(s, env.agent, settings=env.settings)
        await s.commit()
    return woken
