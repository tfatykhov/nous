"""F114 goals (PR 1c): a goal chain's REPORT goes to the goal's channel, which the driver claims into the journal.

The one branch is in ``continuation._insert_report_row``, which every writer of an owner-facing row reaches: with the
goals' lookup bound (``set_goal_report_channel``), a REPORT of a goal session's root is written to ``goal:<id>`` with
no push time; a QUESTION or a PROPOSAL, and any row of another root, is written as before. Unbound, nothing is
looked up.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from f099_support import CHAN, env_factory, make_root, runner_env, set_intention, use  # noqa: F401
from goals_support import (
    active,
    first_result,
    inbox,
    journal,
    reset_goal_report_channel,  # noqa: F401
    started,
    world,
)
from sqlalchemy import event, update

from nous.brain import continuation
from nous.goals.chain import report_channel
from nous.storage.models import GoalSession, ResultInbox

pytestmark = pytest.mark.postgres_only

DONE = "Merged the duplicate notes: 34 notes are left."
REPORT = use("resolve_intention", decision="report", note=DONE, progress=True, confidence=0.8)


async def _world(runner_env, *steps):  # noqa: F811
    """A goal world with only the report hook bound (``main.py`` binds it with the flag on)."""
    w = await world(runner_env, *steps, bind=False)
    continuation.set_goal_report_channel(report_channel)
    return w


async def _insert(w, kind: str, root_id: uuid.UUID, *, push_after: datetime | None = None) -> uuid.UUID:
    async with w.db.session() as s:
        rid = await continuation.insert_report(
            s,
            w.agent,
            kind=kind,
            title="t",
            body="b",
            channel=CHAN,
            intention_id=root_id,
            root_id=root_id,
            push_after=push_after,
        )
        await s.commit()
    return rid


async def test_a_goal_chains_report_is_written_to_the_goal_channel_with_no_push_time(runner_env):  # noqa: F811
    w = await _world(runner_env, [REPORT])
    goal = await active(w)
    root, _session = await started(w, goal)
    await first_result(w, root)
    assert await w.cont.run_arrival(root.id) is not None
    (row,) = await inbox(w, source_kind="intention_report")
    assert (row.channel, row.push_after, row.msg_type, row.body) == (f"goal:{goal.id}", None, "REPORT", DONE)
    assert await inbox(w, channel=CHAN) == []  # the owner's chat gets nothing


async def test_the_driver_claims_that_report_into_the_journal(runner_env):  # noqa: F811
    w = await _world(runner_env, [REPORT])
    goal = await active(w)
    root, session = await started(w, goal)
    await first_result(w, root)
    await w.cont.run_arrival(root.id)
    assert (await w.driver.tick()).claimed == 1
    (row,) = await journal(w, goal, "report")
    assert (row.actor, row.session_n, row.text) == ("chain", session.n, DONE)
    (inbox_row,) = await inbox(w, channel=f"goal:{goal.id}")
    assert inbox_row.delivered_at is not None


@pytest.mark.parametrize("kind", [continuation.MSG_QUESTION, continuation.MSG_PROPOSAL])
async def test_a_goal_chains_question_and_proposal_still_reach_the_owner(runner_env, kind):  # noqa: F811
    w = await _world(runner_env)
    goal = await active(w)
    root, _session = await started(w, goal)
    when = datetime.now(UTC)
    await _insert(w, kind, root.id, push_after=when)
    (row,) = await inbox(w, source_kind="intention_report")
    assert (row.channel, row.msg_type, row.push_after) == (CHAN, kind, when)


async def test_an_ordinary_roots_report_is_unchanged(runner_env):  # noqa: F811
    w = await _world(runner_env)
    await started(w, await active(w))  # a goal with a running session, and this root is not its
    root = await make_root(w.env)
    when = datetime.now(UTC)
    await _insert(w, continuation.MSG_REPORT, root.id, push_after=when)
    (row,) = await inbox(w, source_kind="intention_report")
    assert (row.channel, row.push_after) == (CHAN, when)


async def test_the_lookup_finds_a_goal_root_before_its_root_id_is_written(runner_env):  # noqa: F811
    """The key is the first subtask, written with the session row before the root exists."""
    w = await _world(runner_env)
    goal = await active(w)
    root, session = await started(w, goal)
    async with w.db.session() as s:
        await s.execute(update(GoalSession).where(GoalSession.id == session.id).values(root_id=None))
        await s.commit()
    async with w.db.session() as s:
        assert await report_channel(s, w.agent, root.id) == f"goal:{goal.id}"
        assert await report_channel(s, "another-agent", root.id) is None
        assert await report_channel(s, w.agent, uuid.uuid4()) is None
    ordinary = await make_root(w.env)  # a root of the same agent that no goal session started
    async with w.db.session() as s:
        assert await report_channel(s, w.agent, ordinary.id) is None


async def test_with_the_hook_unbound_no_goal_query_runs(runner_env):  # noqa: F811
    """Unbound (the flag off), a goal root's report goes to the owner as before and no statement reads a goal
    table; bound, the one lookup reads goal_sessions."""
    w = await world(runner_env, bind=False)
    goal = await active(w)
    root, _session = await started(w, goal)
    statements: list[str] = []

    def seen(conn, cursor, statement, *args):
        statements.append(statement)

    engine = w.db.engine.sync_engine
    event.listen(engine, "before_cursor_execute", seen)
    try:
        await _insert(w, continuation.MSG_REPORT, root.id)
        unbound = list(statements)
        continuation.set_goal_report_channel(report_channel)
        statements.clear()
        await _insert(w, continuation.MSG_REPORT, root.id)
        bound = list(statements)
    finally:
        event.remove(engine, "before_cursor_execute", seen)
    assert not [s for s in unbound if "goal" in s]
    assert [s for s in bound if "goal_sessions" in s]
    first, second = await inbox(w, source_kind="intention_report")
    assert (first.channel, second.channel) == (CHAN, f"goal:{goal.id}")


async def test_an_expiry_report_of_a_goal_chain_goes_to_the_journal(runner_env):  # noqa: F811
    w = await _world(runner_env)
    goal = await active(w)
    root, session = await started(w, goal)
    await first_result(w, root)
    await set_intention(w.env, root.id, deadline=datetime.now(UTC) - timedelta(hours=1))
    async with w.db.session() as s:
        expired = await continuation.expire_roots(s, w.agent, ttl_hours=72.0, settings=w.env.settings, limit=10)
        await s.commit()
    assert expired == [root.id]
    (row,) = await inbox(w, source_kind="intention_report")
    assert (row.channel, row.push_after) == (f"goal:{goal.id}", None)
    await w.driver.tick()
    (journalled,) = await journal(w, goal, "report")
    assert journalled.session_n == session.n and journalled.text.startswith("I closed this because")
    assert await inbox(w, channel=CHAN) == []


async def test_a_goal_report_a_cancel_stamped_is_still_journalled(runner_env):  # noqa: F811
    """A report on the goal channel written before a cancel of its root, and stamped by the cancel before the driver
    claimed it, still reaches the goal's journal, once."""
    w = await _world(runner_env, [REPORT])
    goal = await active(w)
    root, session = await started(w, goal)
    await first_result(w, root)
    assert await w.cont.run_arrival(root.id) is not None
    async with w.db.session() as s:  # a cancel's stamp, as continuation.cancel_root writes it
        await s.execute(
            update(ResultInbox)
            .where(ResultInbox.agent_id == w.agent, ResultInbox.channel == f"goal:{goal.id}")
            .values(delivered_at=datetime.now(UTC), delivered_session_id=continuation.SILENT_SESSION_ID)
        )
        await s.commit()
    assert (await w.driver.tick()).claimed == 1
    (row,) = await journal(w, goal, "report")
    assert (row.text, row.session_n) == (DONE, session.n)
    assert (await w.driver.tick()).claimed == 0  # claimed once
