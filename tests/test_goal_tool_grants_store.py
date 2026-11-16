"""Per-goal tool grants, the store: a grant or a revoke is one transaction under the goal row's lock with an owner
``state`` journal row, records the proof the owner's route accepted, never wakes the goal or touches its request, and
is refused for a finished goal, past the cap, or for anyone but the owner. Concurrency runs on separate connections."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select, text

from nous.api.tool_classes import McpIdentity
from nous.config import Settings
from nous.goals.store import GRANTS_MAX, GoalRefused, GoalStore
from nous.storage.models import Goal, GoalJournal, GoalToolGrant

pytestmark = pytest.mark.postgres_only

NOTES = McpIdentity(uuid.uuid4(), "ab" * 32, "search", server_name="notes", server_host="notes.example.com")


def _mcp(n: int) -> McpIdentity:
    return McpIdentity(uuid.uuid4(), "ab" * 32, "t", server_name=f"s{n}", server_host=f"s{n}.example.com")


def _store(db) -> GoalStore:
    agent = f"goal-grants-{uuid.uuid4().hex[:8]}"
    return GoalStore(db, agent, Settings(_env_file=None, agent_id=agent, telegram_chat_id=""))


async def _draft(store: GoalStore) -> Goal:
    return await store.create(
        title="Tidy the notes",
        objective="Merge the duplicate notes",
        done_when="None are left",
        proposed_by="owner",
        origin_channel="telegram:4242",
    )


async def _active(store: GoalStore) -> Goal:
    return await store.approve((await _draft(store)).id, actor="owner")


async def _grants(db, goal: Goal) -> list[GoalToolGrant]:
    async with db.session() as s:
        query = select(GoalToolGrant).where(GoalToolGrant.goal_id == goal.id).order_by(GoalToolGrant.granted_at)
        return list((await s.execute(query)).scalars())


async def _state_rows(db, goal: Goal) -> list[GoalJournal]:
    async with db.session() as s:
        query = select(GoalJournal).where(GoalJournal.goal_id == goal.id, GoalJournal.kind == "state")
        return list((await s.execute(query.order_by(GoalJournal.seq))).scalars())


async def _wait_for_a_waiter_on_goals(db, timeout: float = 10.0) -> None:
    """Until another connection is blocked on a lock in a statement on brain.goals."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    query = text(
        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
        "AND wait_event_type = 'Lock' AND query ILIKE '%brain.goals%'"
    )
    while True:
        async with db.session() as s:
            if (await s.execute(query)).scalar_one():
                return
        assert loop.time() < deadline, "no statement waited on the goal row"
        await asyncio.sleep(0.02)


async def test_a_grant_writes_its_row_and_an_owner_state_row_together(db):
    store = _store(db)
    goal = await _active(store)
    assert await store.grant_tool(goal.id, "send_email", None, actor="owner", proof="proxy") is True
    [row] = await _grants(db, goal)
    assert (row.agent_id, row.tool, row.granted_by, row.revoked_at) == (store.agent_id, "send_email", "proxy", None)
    assert (row.server_name, row.server_id, row.url_sha256, row.remote_tool) == (None, None, None, None)
    journal = (await _state_rows(db, goal))[-1]
    assert (journal.actor, journal.text) == ("owner", "Allowed to use send_email without asking, from now on.")
    assert [grant.tool for grant in await store.tool_grants(goal.id)] == ["send_email"]


async def test_an_mcp_grant_keeps_the_identity_the_owner_saw(db):
    store = _store(db)
    goal = await _active(store)
    assert await store.grant_tool(goal.id, "mcp__notes__search", NOTES, actor="owner", proof="owner")
    [row] = await _grants(db, goal)
    assert (row.server_name, row.server_host, row.server_id, row.url_sha256, row.remote_tool) == (
        "notes",
        "notes.example.com",
        NOTES.server_id,
        NOTES.url_sha256,
        "search",
    )
    journal = (await _state_rows(db, goal))[-1]
    assert journal.text == "Allowed to use mcp__notes__search (server notes) without asking, from now on."


async def test_a_grant_whose_journal_row_fails_leaves_no_grant(db, monkeypatch):
    store = _store(db)
    goal = await _active(store)
    rows_before = len(await _state_rows(db, goal))

    def broken_journal(s, goal_id, kind, body, **_kw):
        s.add(GoalJournal(agent_id=store.agent_id, goal_id=goal_id, kind=kind, actor="owner", text=None, evidence=[]))

    monkeypatch.setattr(store, "add_journal_row", broken_journal)
    with pytest.raises(Exception):  # noqa: B017 -- the database refuses the row; which class is the driver's
        await store.grant_tool(goal.id, "send_email", None, actor="owner", proof="owner")
    assert await _grants(db, goal) == []
    assert len(await _state_rows(db, goal)) == rows_before


async def test_a_second_grant_of_the_same_tool_changes_nothing(db):
    store = _store(db)
    goal = await _active(store)
    await store.grant_tool(goal.id, "send_email", None, actor="owner", proof="owner")
    count = len(await _state_rows(db, goal))
    assert await store.grant_tool(goal.id, "send_email", None, actor="owner", proof="owner") is False
    assert len(await _grants(db, goal)) == 1
    assert len(await _state_rows(db, goal)) == count


async def test_an_mcp_tool_allowed_again_with_another_identity_replaces_its_grant(db):
    store = _store(db)
    goal = await _active(store)
    await store.grant_tool(goal.id, "mcp__notes__search", NOTES, actor="owner", proof="owner")
    moved = McpIdentity(uuid.uuid4(), "cd" * 32, "search", server_name="notes", server_host="notes.example.com")
    assert await store.grant_tool(goal.id, "mcp__notes__search", NOTES, actor="owner", proof="owner") is False
    assert await store.grant_tool(goal.id, "mcp__notes__search", moved, actor="owner", proof="proxy") is True
    old, new = await _grants(db, goal)
    assert (old.server_id, old.revoked_by, old.revoked_at is not None) == (NOTES.server_id, "proxy", True)
    assert (new.server_id, new.revoked_at) == (moved.server_id, None)
    journal = (await _state_rows(db, goal))[-1]
    assert journal.text == (
        "Allowed to use mcp__notes__search (server notes) without asking, from now on, in place of its earlier grant."
    )


async def test_a_revoke_marks_the_row_with_the_proof_and_journals_as_the_owner(db):
    store = _store(db)
    goal = await _active(store)
    await store.grant_tool(goal.id, "send_email", None, actor="owner", proof="owner")
    assert await store.revoke_tool(goal.id, "send_email", actor="owner", proof="proxy") is True
    [row] = await _grants(db, goal)
    assert (row.granted_by, row.revoked_by, row.revoked_at is not None) == ("owner", "proxy", True)
    journal = (await _state_rows(db, goal))[-1]
    assert (journal.actor, journal.text) == ("owner", "No longer allowed to use send_email without asking.")
    assert await store.tool_grants(goal.id) == []


async def test_a_revoke_of_a_tool_not_granted_changes_nothing(db):
    store = _store(db)
    goal = await _active(store)
    count = len(await _state_rows(db, goal))
    assert await store.revoke_tool(goal.id, "send_email", actor="owner", proof="owner") is False
    assert await store.revoke_tool(goal.id, "mcp__gone__thing", actor="owner", proof="owner") is False
    assert len(await _state_rows(db, goal)) == count


async def test_a_tool_can_be_allowed_again_after_a_revoke(db):
    store = _store(db)
    goal = await _active(store)
    await store.grant_tool(goal.id, "send_file", None, actor="owner", proof="owner")
    await store.revoke_tool(goal.id, "send_file", actor="owner", proof="owner")
    assert await store.grant_tool(goal.id, "send_file", None, actor="owner", proof="owner") is True
    first, second = await _grants(db, goal)
    assert first.revoked_at is not None and second.revoked_at is None and first.id != second.id


async def test_a_draft_and_a_paused_goal_accept_grants_and_a_finished_one_refuses(db):
    store = _store(db)
    draft = await _draft(store)
    assert await store.grant_tool(draft.id, "send_email", None, actor="owner", proof="owner")
    paused = await store.pause((await _active(store)).id, actor="owner")
    assert await store.grant_tool(paused.id, "send_email", None, actor="owner", proof="owner")
    cancelled = await store.cancel(draft.id, actor="owner")
    ref = f"G-{cancelled.short_id:04d}"
    message = f"{ref} is finished; its tools can no longer be changed."
    with pytest.raises(GoalRefused) as refused:
        await store.grant_tool(cancelled.id, "send_file", None, actor="owner", proof="owner")
    assert str(refused.value) == message
    with pytest.raises(GoalRefused) as refused:
        await store.revoke_tool(cancelled.id, "send_email", actor="owner", proof="owner")
    assert str(refused.value) == message


async def test_at_most_twenty_tools(db):
    store = _store(db)
    goal = await _active(store)
    for n in range(GRANTS_MAX):
        await store.grant_tool(goal.id, f"mcp__s{n}__t", _mcp(n), actor="owner", proof="owner")
    assert GRANTS_MAX == 20
    with pytest.raises(GoalRefused) as refused:
        await store.grant_tool(goal.id, "send_email", None, actor="owner", proof="owner")
    assert str(refused.value) == "A goal can have at most 20 tools. Remove one first."
    await store.revoke_tool(goal.id, "mcp__s0__t", actor="owner", proof="owner")
    assert await store.grant_tool(goal.id, "send_email", None, actor="owner", proof="owner")


@pytest.mark.parametrize("tool", ["bash", "run_python", "goal_note", "submit_final_report", "web_search", "not_a_tool"])
async def test_a_tool_a_goal_may_never_be_given_is_refused_by_the_store_itself(db, tool):
    """Whoever calls the store (not only the routes): the shell, Python, the harness's own tools, a tool that acts only
    inside Nous and one nobody classified are refused, and nothing is written."""
    store = _store(db)
    goal = await _active(store)
    before = [row.seq for row in await _state_rows(db, goal)]
    with pytest.raises(GoalRefused) as refused:
        await store.grant_tool(goal.id, tool, None, actor="owner", proof="owner")
    assert str(refused.value) == f"{tool} cannot be given to a goal."
    assert await _grants(db, goal) == [] and [row.seq for row in await _state_rows(db, goal)] == before
    with pytest.raises(GoalRefused, match=f"^{tool} cannot be given to a goal.$"):
        await store.create(
            title="Tidy the notes",
            objective="Merge the duplicate notes",
            done_when="None are left",
            proposed_by="owner",
            tools=[("send_email", None), (tool, None)],
            proof="owner",
        )
    async with db.session() as s:
        goals = (await s.execute(select(Goal.id).where(Goal.agent_id == store.agent_id))).scalars().all()
    assert list(goals) == [goal.id]  # nothing created


async def test_only_the_owner_changes_a_goals_tools(db):
    store = _store(db)
    goal = await _active(store)
    for actor in ("chain", "harness"):
        with pytest.raises(GoalRefused, match="only the owner"):
            await store.grant_tool(goal.id, "send_email", None, actor=actor, proof="owner")
        with pytest.raises(GoalRefused, match="only the owner"):
            await store.revoke_tool(goal.id, "send_email", actor=actor, proof="owner")
    assert await _grants(db, goal) == []


async def test_another_agents_goal_is_not_found(db):
    mine, theirs = _store(db), _store(db)
    goal = await _active(theirs)
    with pytest.raises(GoalRefused, match="no goal"):
        await mine.grant_tool(goal.id, "send_email", None, actor="owner", proof="owner")
    with pytest.raises(GoalRefused, match="no goal"):
        await mine.revoke_tool(goal.id, "send_email", actor="owner", proof="owner")
    assert await _grants(db, goal) == []


async def test_a_grant_neither_wakes_the_goal_nor_touches_its_request(db):
    store = _store(db)
    goal = await store.request_run_now((await _active(store)).id, actor="owner")
    before = (goal.state, goal.next_wake_at, goal.pending_request, goal.pending_request_at, goal.updated_at)
    await store.grant_tool(goal.id, "send_email", None, actor="owner", proof="owner")
    await store.revoke_tool(goal.id, "send_email", actor="owner", proof="owner")
    after = await store.get(goal.id)
    assert (after.state, after.next_wake_at, after.pending_request, after.pending_request_at, after.updated_at) == (
        before
    )


async def test_a_grant_waiting_behind_another_on_the_goal_row_adds_no_second_row(db):
    store = _store(db)
    goal = await _active(store)
    async with db.session() as holding:
        await holding.execute(select(Goal).where(Goal.id == goal.id).with_for_update())
        waiting = asyncio.create_task(store.grant_tool(goal.id, "send_email", None, actor="owner", proof="owner"))
        await _wait_for_a_waiter_on_goals(db)
        holding.add(GoalToolGrant(agent_id=store.agent_id, goal_id=goal.id, tool="send_email", granted_by="proxy"))
        await holding.commit()
    assert await waiting is False
    [row] = await _grants(db, goal)
    assert row.granted_by == "proxy"


async def test_the_cap_holds_for_a_grant_waiting_on_the_goal_row(db):
    store = _store(db)
    goal = await _active(store)
    for n in range(GRANTS_MAX - 1):
        await store.grant_tool(goal.id, f"mcp__s{n}__t", _mcp(n), actor="owner", proof="owner")
    async with db.session() as holding:
        await holding.execute(select(Goal).where(Goal.id == goal.id).with_for_update())
        waiting = asyncio.create_task(store.grant_tool(goal.id, "send_email", None, actor="owner", proof="owner"))
        await _wait_for_a_waiter_on_goals(db)
        holding.add(GoalToolGrant(agent_id=store.agent_id, goal_id=goal.id, tool="send_file", granted_by="owner"))
        await holding.commit()
    with pytest.raises(GoalRefused, match="at most 20"):
        await waiting
    assert len(await store.tool_grants(goal.id)) == GRANTS_MAX
