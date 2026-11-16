"""A goal that may run any command, in the view and the rule (``nous.goals.grants``, ``nous.goals.commands``): the view
holds the open sessions' roots of active goals in the mode whose end is after the database's clock, each refresh
replaces what it read, a failure clears both maps, the end is kept on the view's monotonic clock, and
``decide_commands`` lets exactly a task or a workflow step of such a root run any command."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text, update

from nous.api import tool_policy
from nous.api.execution_context import CONTEXT_KINDS, ExecutionContext
from nous.autonomy import tool_guard
from nous.autonomy.tool_guard import ToolGuard
from nous.config import Settings
from nous.goals import grants as G
from nous.goals.commands import CommandsEntry, decide_commands
from nous.goals.grants import GoalToolGrants
from nous.goals.store import GoalStore
from nous.storage.models import Goal, GoalSession

pytestmark = pytest.mark.postgres_only


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _settings(agent: str) -> Settings:
    return Settings(_env_file=None, agent_id=agent, telegram_chat_id="", goals_tick_seconds=60)


async def _world(db, *, commands: bool = True):
    agent = f"commands-view-{uuid.uuid4().hex[:8]}"
    settings = _settings(agent)
    store = GoalStore(db, agent, settings)
    clock = Clock()
    view = GoalToolGrants(db, settings, clock=clock, commands=commands)
    tool_guard.install(ToolGuard())
    return store, view, clock


async def _goal(store: GoalStore, *, state: str = "active") -> Goal:
    goal = await store.create(
        title="Tidy the notes",
        objective="Merge the duplicate notes",
        done_when="None are left",
        proposed_by="owner",
        origin_channel="telegram:4242",
    )
    if state == "draft":
        return goal
    goal = await store.approve(goal.id, actor="owner")
    if state == "paused":
        goal = await store.pause(goal.id, actor="owner")
    if state == "cancelled":
        goal = await store.cancel(goal.id, actor="owner")
    return goal


async def _mode(db, goal_id, *, hours: int = 24, left: timedelta | None = timedelta(hours=2)) -> None:
    """The goal's row in the mode, written as the store writes it; ``left`` None: a draft's choice (no end yet)."""
    async with db.session() as s:
        until = None if left is None else (await s.execute(text("SELECT clock_timestamp()"))).scalar_one() + left
        await s.execute(
            update(Goal).where(Goal.id == goal_id).values(mode="commands", mode_hours=hours, mode_until=until)
        )
        await s.commit()


async def _contained(db, goal_id) -> None:
    async with db.session() as s:
        await s.execute(
            update(Goal).where(Goal.id == goal_id).values(mode="contained", mode_hours=None, mode_until=None)
        )
        await s.commit()


async def _session(db, store: GoalStore, goal: Goal, n: int = 1, *, ended: bool = False) -> uuid.UUID:
    root = uuid.uuid4()
    async with db.session() as s:
        s.add(
            GoalSession(
                agent_id=store.agent_id,
                goal_id=goal.id,
                n=n,
                trigger="timer",
                first_subtask_id=uuid.uuid4(),
                root_id=root,
                ended_at=datetime.now(UTC) if ended else None,
                outcome="no_step" if ended else None,
            )
        )
        await s.commit()
    return root


def _task(root: uuid.UUID | None, kind: str = "subtask", authority: str = "internal_only", **extra) -> ExecutionContext:
    if kind == "approved_action":
        extra = {"proposal_id": uuid.uuid4(), "declared_tools": ("bash",), **extra}
    return ExecutionContext(
        kind=kind,
        authority=authority,
        intention_id=extra.pop("intention_id", uuid.uuid4()),
        root_intention_id=root,
        **extra,
    )


async def _in_mode(db, **over):
    store, view, clock = await _world(db, **over)
    goal = await _goal(store)
    root = await _session(db, store, goal)
    await _mode(db, goal.id)
    assert await view.refresh()
    return store, view, clock, goal, root


# ---- what the view holds ----------------------------------------------------------------------------------------


async def test_the_view_holds_the_open_session_of_an_active_goal_in_the_mode(db):
    _store, view, clock, goal, root = await _in_mode(db)
    entry = view.commands_entry(root)
    assert (entry.goal_id, entry.session_n) == (goal.id, 1)
    assert clock.now + 2 * 3600 - 60 < entry.expires_at <= clock.now + 2 * 3600  # the time left, on the view's clock
    assert view.commands_for(_task(root)) == entry
    assert view._by_root == {}  # a goal in the mode needs no grant


@pytest.mark.parametrize("state", ["draft", "paused", "cancelled"])
async def test_a_goal_that_is_not_active_has_no_entry(db, state):
    store, view, _clock = await _world(db)
    goal = await _goal(store, state="active" if state != "draft" else "draft")
    root = await _session(db, store, goal)
    await _mode(db, goal.id, left=None if state == "draft" else timedelta(hours=2))
    if state == "paused":
        await store.pause(goal.id, actor="owner")
    if state == "cancelled":
        await store.cancel(goal.id, actor="owner")
    await view.refresh()
    assert view.commands_entry(root) is None and view._commands == {}


async def test_an_ended_session_and_an_end_already_passed_have_no_entry(db):
    store, view, _clock, goal, root = await _in_mode(db)
    assert view.commands_entry(root) is not None
    async with db.session() as s:
        await s.execute(
            text("UPDATE brain.goal_sessions SET ended_at = now(), outcome = 'no_step' WHERE root_id = :r"), {"r": root}
        )
        await s.commit()
    await view.refresh()
    assert view.commands_entry(root) is None
    ended = await _session(db, store, goal, 2, ended=True)
    await view.refresh_root(ended)
    assert view.commands_entry(ended) is None
    later = await _session(db, store, goal, 3)
    await _mode(db, goal.id, left=timedelta(seconds=-1))
    await view.refresh()
    assert view.commands_entry(later) is None


async def test_two_goals_each_have_their_own_entry_and_the_view_is_scoped_to_its_agent(db):
    store, view, _clock = await _world(db)
    other_store, _other_view, _ = await _world(db)
    one, two, foreign = await _goal(store), await _goal(store), await _goal(other_store)
    root_one, root_two = await _session(db, store, one), await _session(db, store, two)
    root_foreign = await _session(db, other_store, foreign)
    for goal in (one, two, foreign):
        await _mode(db, goal.id)
    await view.refresh()
    assert view.commands_entry(root_one).goal_id == one.id and view.commands_entry(root_two).goal_id == two.id
    assert view.commands_entry(root_foreign) is None


async def test_with_commands_off_the_view_reads_no_entry(db):
    store, view, _clock = await _world(db, commands=False)
    goal = await _goal(store)
    root = await _session(db, store, goal)
    await _mode(db, goal.id)
    assert await view.refresh()
    assert view.commands_entry(root) is None and view._commands == {}


# ---- each refresh replaces what it read ------------------------------------------------------------------------


async def test_a_refresh_replaces_the_entry_with_the_new_end(db):
    _store, view, clock, goal, root = await _in_mode(db)
    first = view.commands_entry(root).expires_at
    await _mode(db, goal.id, left=timedelta(minutes=10))
    await view.refresh()
    assert view.commands_entry(root).expires_at < first  # an earlier end replaces the later one
    await _mode(db, goal.id, left=timedelta(minutes=5))
    await view.refresh_goal(goal.id)
    assert view.commands_entry(root).expires_at <= clock.now + 300
    await _mode(db, goal.id, left=timedelta(minutes=3))
    await view.refresh_root(root)
    assert view.commands_entry(root).expires_at <= clock.now + 180


@pytest.mark.parametrize("how", ["full", "goal", "root"])
async def test_a_goal_with_no_grant_returned_to_contained_loses_its_entry(db, how):
    store, view, _clock, goal, root = await _in_mode(db)
    other = await _goal(store)
    other_root = await _session(db, store, other)
    await _mode(db, other.id)
    await view.refresh()
    assert view._by_root == {} and view.commands_entry(root) is not None
    await _contained(db, goal.id)
    refreshed = {
        "full": view.refresh,
        "goal": lambda: view.refresh_goal(goal.id),
        "root": lambda: view.refresh_root(root),
    }
    assert await refreshed[how]() is True
    assert view.commands_entry(root) is None
    assert view.commands_entry(other_root) is not None  # another goal's entry is kept


async def test_a_partial_refresh_keeps_the_entries_of_other_goals_and_roots(db):
    store, view, _clock = await _world(db)
    one, two = await _goal(store), await _goal(store)
    root_one, root_two = await _session(db, store, one), await _session(db, store, two)
    await _mode(db, one.id)
    await _mode(db, two.id)
    await view.refresh()
    await _contained(db, one.id)
    await _contained(db, two.id)
    await view.refresh_goal(one.id)
    assert view.commands_entry(root_one) is None and view.commands_entry(root_two) is not None
    await view.refresh_root(root_two)
    assert view.commands_entry(root_two) is None


# ---- the publish ------------------------------------------------------------------------------------------------


async def _published_root(db, store, goal, view, *, fail_commit: bool = False):
    """Open a session row with no root, then give it its root inside ``view.publishing``."""
    session_id = uuid.uuid4()
    async with db.session() as s:
        s.add(
            GoalSession(
                id=session_id,
                agent_id=store.agent_id,
                goal_id=goal.id,
                n=1,
                trigger="timer",
                first_subtask_id=uuid.uuid4(),
            )
        )
        await s.commit()
    root = uuid.uuid4()
    seen: dict = {}
    async with db.session() as s:
        await s.execute(update(GoalSession).where(GoalSession.id == session_id).values(root_id=root))
        with contextlib.suppress(ConnectionError):
            async with view.publishing(s, root):
                seen["before_commit"] = view.commands_entry(root)
                if fail_commit:
                    raise ConnectionError("the commit was lost")
                await s.commit()
    return root, seen


async def test_the_publish_puts_the_roots_entry_in_before_its_commit(db):
    store, view, _clock = await _world(db)
    goal = await _goal(store)
    await _mode(db, goal.id)
    await view.refresh()
    root, seen = await _published_root(db, store, goal, view)
    assert seen["before_commit"] is not None and seen["before_commit"].goal_id == goal.id
    assert view.commands_entry(root) == seen["before_commit"]


async def test_a_publish_whose_commit_fails_takes_the_roots_entry_out_of_both_maps(db):
    store, view, _clock = await _world(db)
    goal = await _goal(store)
    await _mode(db, goal.id)
    await view.refresh()
    root, seen = await _published_root(db, store, goal, view, fail_commit=True)
    assert seen["before_commit"] is not None
    assert view.commands_entry(root) is None and root not in view._by_root


async def test_a_failed_publish_read_clears_both_maps(db, caplog):
    store, view, _clock, goal, root = await _in_mode(db)
    await store.grant_tool(goal.id, "send_email", None, actor="owner", proof="owner")
    await view.refresh()
    assert view.commands_entry(root) is not None and view.grant(root, "send_email") is not None

    async def failing(*, root_id, goal_id, session=None):
        await session.execute(text("SELECT 1 / 0"))

    view._read = failing
    async with db.session() as s:
        async with view.publishing(s, uuid.uuid4()):
            pass
    assert view._commands == {} and view._by_root == {} and not view.fresh()


async def test_a_refresh_that_fails_clears_both_maps(db, caplog):
    store, view, _clock, goal, root = await _in_mode(db)
    await store.grant_tool(goal.id, "send_email", None, actor="owner", proof="owner")

    class Broken:
        def session(self):
            raise ConnectionError("db at 10.0.0.1 is down")

    good = view._db
    for refresh in (view.refresh, lambda: view.refresh_root(root), lambda: view.refresh_goal(goal.id)):
        view._db = good
        assert await view.refresh() and view._commands and view._by_root
        view._db = Broken()
        assert await refresh() is False
        assert view._commands == {} and view._by_root == {} and not view.fresh()
        assert decide_commands(view, _task(root)) is None
    assert "10.0.0.1" not in caplog.text
    view._db = good


# ---- the clock --------------------------------------------------------------------------------------------------


async def test_the_end_is_kept_on_the_injected_monotonic_clock(db, monkeypatch):
    _store, view, clock, goal, root = await _in_mode(db)
    await _mode(db, goal.id, left=timedelta(seconds=100))
    await view.refresh()
    entry = view.commands_entry(root)
    clock.now = entry.expires_at - 0.5
    assert decide_commands(view, _task(root)) == entry
    monkeypatch.setattr(time, "time", lambda: 10**12)  # the wall clock jumps: nothing changes
    assert decide_commands(view, _task(root)) == entry
    clock.now = entry.expires_at
    assert decide_commands(view, _task(root)) is None


async def test_a_stale_view_lifts_nothing_and_only_a_full_refresh_renews_it(db):
    _store, view, clock, goal, root = await _in_mode(db)
    entry = view.commands_entry(root)
    clock.now += max(3 * 60, G.MIN_FRESH_SECONDS) + 1
    await _mode(db, goal.id, left=timedelta(hours=10))
    await view.refresh_goal(goal.id)
    assert not view.fresh() and decide_commands(view, _task(root)) is None
    await view.refresh()
    assert view.fresh() and decide_commands(view, _task(root)) is not None
    assert entry is not None


async def test_the_views_own_loop_keeps_it_fresh(db):
    _store, view, clock, _goal_row, root = await _in_mode(db)
    clock.now += max(3 * 60, G.MIN_FRESH_SECONDS) + 1
    assert not view.fresh()
    view._tick_seconds = 0.01
    view.start_loop()
    try:
        for _ in range(200):
            if view.fresh():
                break
            await asyncio.sleep(0.01)
        assert view.fresh() and decide_commands(view, _task(root)) is not None
    finally:
        await view.stop_loop()
    assert view._loop_task is None


async def test_contained_during_a_suspended_refresh_ends_with_no_lift(db):
    """A refresh reads the entry, then is held between its read and its apply; the goal is returned to contained and
    the goal is read again. That read waits for the held one, so the entry read before the change is never the last
    word."""
    _store, view, _clock, goal, root = await _in_mode(db)
    read_done, release = asyncio.Event(), asyncio.Event()
    real = view._db
    held = {"first": True}

    class Holding:
        @contextlib.asynccontextmanager
        async def session(self):
            async with real.session() as s:
                yield s
            if held["first"]:
                held["first"] = False
                read_done.set()
                await release.wait()

    view._db = Holding()
    suspended = asyncio.create_task(view.refresh())
    await read_done.wait()
    await _contained(db, goal.id)
    after = asyncio.create_task(view.refresh_goal(goal.id))
    await asyncio.sleep(0.05)
    release.set()
    assert await suspended is True and await after is True
    assert view.commands_entry(root) is None and decide_commands(view, _task(root)) is None


# ---- decide_commands: every step ---------------------------------------------------------------------------------


async def test_exactly_a_task_or_a_workflow_step_of_the_root_runs_any_command(db):
    _store, view, _clock, _goal_row, root = await _in_mode(db)
    for kind in CONTEXT_KINDS:
        for authority in ("owner", "internal_only"):
            if kind == "continuation" and authority != "internal_only":
                continue
            answer = decide_commands(view, _task(root, kind, authority))
            lifted = kind in tool_policy.COMMANDS_KINDS and authority == "internal_only"
            assert (answer is not None) == lifted, (kind, authority)


async def test_each_step_that_fails_lifts_nothing(db):
    _store, view, clock, _goal_row, root = await _in_mode(db)
    assert decide_commands(view, _task(root)) is not None
    tool_guard.install(None)
    assert decide_commands(view, _task(root)) is None
    tool_guard.install(ToolGuard())
    assert decide_commands(view, _task(root, authority="owner")) is None
    assert decide_commands(view, _task(root, "continuation")) is None
    assert decide_commands(view, _task(None)) is None
    assert decide_commands(view, _task(uuid.uuid4())) is None  # another root
    clock.now = view.commands_entry(root).expires_at + 1
    assert decide_commands(view, _task(root)) is None


async def test_the_rule_keys_on_the_root_never_the_turns_own_intention(db):
    _store, view, _clock, _goal_row, root = await _in_mode(db)
    assert decide_commands(view, _task(uuid.uuid4(), intention_id=root)) is None
    assert decide_commands(view, _task(root, intention_id=uuid.uuid4())) is not None


async def test_a_decision_that_raises_lifts_nothing(db, caplog):
    _store, view, _clock, _goal_row, root = await _in_mode(db)

    def broken(_root):
        raise RuntimeError("view at 10.0.0.1 broke")

    view.commands_entry = broken
    with caplog.at_level(logging.WARNING):
        assert view.commands_for(_task(root)) is None
    assert "RuntimeError" in caplog.text and "10.0.0.1" not in caplog.text


def test_an_entry_is_frozen():
    entry = CommandsEntry(uuid.uuid4(), 1, 5.0)
    with pytest.raises(AttributeError):
        entry.expires_at = 10.0  # type: ignore[misc]
