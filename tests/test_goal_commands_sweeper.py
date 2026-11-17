"""A goal that may run any command, ended by time and by a server start (``nous.goals.modes.ModeSweeper``), and what
its sessions are told (``charter.commands_line``): an end that has passed, a finished goal and a goal past its draft
with no end return to contained with one harness row each, under the goal row's lock (two sweepers write one row); an
armed draft is left alone; at a start with the flag off every goal returns to contained, and with it on an end past
the host's ceiling moves to it, once; the driver sweeps first in each tick only with the flag on. The session task and
the decision turn carry the line while the mode holds and are as before otherwise."""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest
from f099_support import env_factory  # noqa: F401
from goals_support import reset_goal_report_channel  # noqa: F401
from sqlalchemy import select, text, update

from nous.api.builtin_tools import _MAX_BASH_TIMEOUT
from nous.api.execution_context import BACKGROUND_BASH_TIMEOUT
from nous.config import Settings
from nous.goals import charter
from nous.goals import commands as C
from nous.goals.chain import GoalChain
from nous.goals.driver import GoalDriver
from nous.goals.modes import ModeSweeper
from nous.goals.store import GoalStore
from nous.storage.models import Goal, GoalJournal

pytestmark = pytest.mark.postgres_only

ON = {
    "goals_enabled": True,
    "continuation_enabled": True,
    "intentions_enabled": True,
    "result_inbox_enabled": True,
    "subtask_enabled": True,
    "autonomy_settings_enabled": True,
    "tool_guard_enabled": True,
    "goal_tool_grants_enabled": True,
    "owner_entry_mode": "enforce",
    "owner_route_secret_sha256": "ab" * 32,
    "mcp_client_ui_secret_sha256": "cd" * 32,
    "goal_commands_enabled": True,
}


def _world(db, **over):
    agent = f"goal-modes-{uuid.uuid4().hex[:8]}"
    settings = Settings(_env_file=None, agent_id=agent, telegram_chat_id="", **{**ON, **over})
    store = GoalStore(db, agent, settings)
    return store, ModeSweeper(db, settings, store)


async def _goal(store: GoalStore, *, hours: int | None = 5, approve: bool = True) -> Goal:
    commands = C.CommandsRequest(hours, C.confirm_sentence(hours)) if hours is not None else None
    goal = await store.create(
        title="Ship the fix",
        objective="Push the fix to a branch",
        done_when="The branch is pushed",
        proposed_by="owner",
        origin_channel="telegram:4242",
        overrides={"deadline_days": 5},
        **({"commands": commands, "proof": "owner"} if commands else {}),
    )
    return await store.approve(goal.id, actor="owner") if approve else goal


async def _set(db, goal_id, **values) -> None:
    async with db.session() as s:
        await s.execute(update(Goal).where(Goal.id == goal_id).values(**values))
        await s.commit()


async def _rows(db, goal_id) -> list[GoalJournal]:
    async with db.session() as s:
        query = select(GoalJournal).where(GoalJournal.goal_id == goal_id).order_by(GoalJournal.seq)
        return list((await s.execute(query)).scalars())


async def _get(store: GoalStore, goal_id) -> Goal:
    return await store.get(goal_id)


def _contained(goal: Goal) -> bool:
    return (goal.mode, goal.mode_hours, goal.mode_until) == ("contained", None, None)


# ---- expire -----------------------------------------------------------------------------------------------------


async def test_an_end_that_has_passed_returns_the_goal_to_contained_with_one_row(db):
    store, sweeper = _world(db)
    goal = await _goal(store)
    await _set(db, goal.id, mode_until=goal.approved_at - timedelta(seconds=1))
    assert await sweeper.expire() == 1
    assert _contained(await _get(store, goal.id))
    row = (await _rows(db, goal.id))[-1]
    assert (row.actor, row.text, row.evidence) == ("harness", "Its end time passed.", ["mode:off", "why:expired"])
    count = len(await _rows(db, goal.id))
    assert await sweeper.expire() == 0 and len(await _rows(db, goal.id)) == count


async def test_an_end_still_ahead_and_an_armed_draft_are_left_alone(db):
    store, sweeper = _world(db)
    running = await _goal(store)
    draft = await _goal(store, approve=False)
    assert await sweeper.expire() == 0
    assert (await _get(store, running.id)).mode == "commands" and (await _get(store, draft.id)).mode == "commands"


async def test_a_finished_goal_and_a_goal_with_no_end_past_its_draft_return_to_contained(db):
    store, sweeper = _world(db)
    finished, cleared = await _goal(store), await _goal(store, approve=False)
    await _set(db, finished.id, state="failed", finished_at=finished.approved_at)
    await _set(db, cleared.id, state="cancelled", finished_at=cleared.created_at)
    other = await _goal(store, approve=False)
    async with db.session() as s:  # past its draft with no end: written another way than through the store
        await s.execute(
            text(
                "UPDATE brain.goals SET state = 'active', approved_at = now(), deadline_at = now() + interval '1 day' "
                "WHERE id = :g"
            ),
            {"g": other.id},
        )
        await s.commit()
    assert await sweeper.expire() == 3
    for goal_id, why, words in (
        (finished.id, "finished", "The goal finished."),
        (cleared.id, "finished", "The goal finished."),
        (other.id, "cleared", C.ENDED["cleared"]),
    ):
        assert _contained(await _get(store, goal_id))
        row = (await _rows(db, goal_id))[-1]
        assert (row.evidence, row.text) == (["mode:off", f"why:{why}"], words)


async def test_two_sweepers_at_once_write_one_row(db, monkeypatch):
    store, sweeper = _world(db)
    other = ModeSweeper(db, store._settings, store)
    goal = await _goal(store)
    await _set(db, goal.id, mode_until=goal.approved_at - timedelta(seconds=1))
    real = ModeSweeper._locked
    holding, release = asyncio.Event(), asyncio.Event()

    async def held(self, s, goal_id):
        found = await real(self, s, goal_id)
        if self is sweeper:
            holding.set()
            await release.wait()
        return found

    monkeypatch.setattr(ModeSweeper, "_locked", held)
    first = asyncio.create_task(sweeper.expire())
    await holding.wait()
    second = asyncio.create_task(other.expire())
    await asyncio.sleep(0.2)
    release.set()
    assert sorted([await first, await second]) == [0, 1]
    assert len([row for row in await _rows(db, goal.id) if "why:expired" in row.evidence]) == 1


async def test_another_agents_goal_is_not_swept(db):
    store, _sweeper = _world(db)
    _other_store, other_sweeper = _world(db)
    goal = await _goal(store)
    await _set(db, goal.id, mode_until=goal.approved_at - timedelta(seconds=1))
    assert await other_sweeper.expire() == 0
    assert (await _get(store, goal.id)).mode == "commands"


# ---- at a start -------------------------------------------------------------------------------------------------


async def test_with_the_flag_off_a_start_returns_every_goal_to_contained(db):
    store, _ = _world(db)
    running, draft = await _goal(store), await _goal(store, approve=False)
    object.__setattr__(store._settings, "goal_commands_enabled", False)
    sweeper = ModeSweeper(db, store._settings, store)
    assert await sweeper.at_start() == 2
    for goal_id in (running.id, draft.id):
        assert _contained(await _get(store, goal_id))
        assert (await _rows(db, goal_id))[-1].evidence == ["mode:off", "why:flag_off"]
    assert await sweeper.at_start() == 0


async def test_with_the_flag_on_a_start_moves_an_end_past_the_ceiling_to_it_once(db):
    store, sweeper = _world(db, goal_commands_max_hours=24)
    goal = await _goal(store, hours=24)
    object.__setattr__(store._settings, "goal_commands_max_hours", 3)
    assert await sweeper.at_start() == 1
    moved = await _get(store, goal.id)
    assert moved.mode == "commands" and moved.mode_until <= goal.approved_at + timedelta(hours=3, minutes=1)
    row = (await _rows(db, goal.id))[-1]
    assert row.evidence == ["mode:on", "why:clamped"] and row.text == C.clamped_text(moved.mode_until)
    assert await sweeper.at_start() == 0


async def test_the_driver_sweeps_at_its_start_and_first_in_each_tick_only_with_the_flag_on(db, monkeypatch):
    store, sweeper = _world(db)
    calls: list[str] = []

    async def expire(self):
        calls.append("expire")
        return 0

    async def at_start(self):
        calls.append("at_start")
        return 0

    async def claim_reports(self):
        calls.append("claim")
        return 0

    monkeypatch.setattr(ModeSweeper, "expire", expire)
    monkeypatch.setattr(ModeSweeper, "at_start", at_start)
    monkeypatch.setattr(GoalDriver, "claim_reports", claim_reports)
    for name in ("handle_requests", "close_finished", "catch_stuck", "refresh_sessions", "apply_stop_conditions"):
        monkeypatch.setattr(GoalDriver, name, _nothing)
    monkeypatch.setattr(GoalDriver, "_start_due", _nothing_due)
    monkeypatch.setattr(GoalDriver, "tell_owner_of_pauses", _nothing)
    driver = GoalDriver(db, store._settings, None, store, None, modes=sweeper)
    monkeypatch.setattr(GoalDriver, "_loop", _no_loop)
    await driver.start()
    await driver.tick()
    await driver.stop()
    assert calls == ["at_start", "expire", "claim"]
    calls.clear()
    object.__setattr__(store._settings, "goal_commands_enabled", False)
    off = GoalDriver(db, store._settings, None, store, None, modes=sweeper)
    await off.start()
    await off.tick()
    await off.stop()
    assert calls == ["at_start", "claim"]  # the reset at a start, never a sweep per tick


async def _nothing(self, *args, **kwargs):
    return 0


async def _nothing_due(self):
    return (), None


async def _no_loop(self):
    return None


async def test_a_sweep_that_fails_fails_neither_the_start_nor_the_tick(db, monkeypatch, caplog):
    store, sweeper = _world(db)

    async def broken(self):
        raise ConnectionError("db at 10.0.0.1 is down")

    monkeypatch.setattr(ModeSweeper, "expire", broken)
    monkeypatch.setattr(ModeSweeper, "at_start", broken)
    driver = GoalDriver(db, store._settings, None, store, None, modes=sweeper)
    await driver._sweep_modes(at_start=True)
    await driver._sweep_modes()
    assert "ConnectionError" in caplog.text and "10.0.0.1" not in caplog.text


# ---- what the sessions are told ---------------------------------------------------------------------------------


async def test_the_line_is_given_only_while_the_mode_holds(db):
    store, _ = _world(db)
    goal = await _goal(store)
    now = goal.approved_at
    assert C.charter_until(goal, now=now, settings=store._settings) == goal.mode_until
    assert C.charter_until(goal, now=goal.mode_until, settings=store._settings) is None  # the end has come
    contained = await _goal(store, hours=None)
    assert C.charter_until(contained, now=now, settings=store._settings) is None
    draft = await _goal(store, approve=False)
    assert C.charter_until(draft, now=now, settings=store._settings) is None
    paused = await store.pause(goal.id, actor="owner")
    assert C.charter_until(paused, now=now, settings=store._settings) is None
    object.__setattr__(store._settings, "goal_commands_enabled", False)
    resumed = await store.resume(goal.id, actor="owner")
    assert C.charter_until(resumed, now=now, settings=store._settings) is None


async def test_the_session_task_and_the_decision_turn_carry_the_line_and_are_as_before_without_it(db):
    store, _ = _world(db)
    goal = await _goal(store)
    line = charter.commands_line(goal.mode_until)
    plain = charter.build_session_task(goal, [], [], spent=0, day_spent=0)
    told = charter.build_session_task(goal, [], [], spent=0, day_spent=0, commands_until=goal.mode_until)
    assert line not in plain and line in told
    assert told.replace(f"\n{line}", "", 1) == plain
    block_args = {"plan": [], "journal_rows": [], "spent": 0, "day_spent": 0, "session_n": 1}
    block_args.update(session_ended=False, citeable=[])
    plain_block = charter.render_goal_block(goal, **block_args)
    told_block = charter.render_goal_block(goal, **block_args, commands_until=goal.mode_until)
    assert line in told_block and told_block.replace(f"\n{line}", "", 1) == plain_block


async def test_the_driver_tells_a_new_session_while_the_mode_holds(db, monkeypatch):
    store, sweeper = _world(db)

    async def ungated(_heart, task):
        return None, task

    monkeypatch.setattr("nous.goals.driver.gate_subtask_task", ungated)
    driver = GoalDriver(db, store._settings, None, store, None, modes=sweeper)
    goal = await _goal(store)
    _rejection, task = await driver._gated_task(goal)
    assert charter.commands_line(goal.mode_until) in task
    contained = await _goal(store, hours=None)
    _rejection, task = await driver._gated_task(contained)
    assert "run any command" not in task


async def test_a_decision_turn_of_a_goal_in_the_mode_carries_the_line_until_the_end(env_factory):  # noqa: F811
    from test_goals_driver import _active, _started, _world

    w = await _world(env_factory, **ON)
    commands = C.CommandsRequest(5, C.confirm_sentence(5))
    goal = await _active(w, proposed_by="owner", commands=commands, proof="owner")
    assert goal.mode == C.MODE_COMMANDS
    root, _session = await _started(w, goal)
    chain = GoalChain(w.db, w.env.settings, w.store)
    told = charter.COMMANDS_LINE.split("{until}")[0]  # the line, whatever its end
    assert charter.commands_line(goal.mode_until) in (await chain.turn_context(root.id)).block
    async with w.db.session() as s:  # the end has come by the database's clock, before any sweep
        await s.execute(
            text("UPDATE brain.goals SET mode_until = clock_timestamp() - interval '1 second' WHERE id = :i"),
            {"i": goal.id},
        )
        await s.commit()
    assert told not in (await chain.turn_context(root.id)).block
    other = await _world(env_factory, **ON)  # a world of its own: the limit on open sessions holds a second start
    contained_root, _session = await _started(other, await _active(other))
    assert (
        told not in (await GoalChain(other.db, other.env.settings, other.store).turn_context(contained_root.id)).block
    )


def test_the_line_names_the_bash_tools_own_timeouts():
    assert f"at most {_MAX_BASH_TIMEOUT} seconds ({BACKGROUND_BASH_TIMEOUT} if you give no timeout)" in (
        charter.COMMANDS_LINE
    )


# ---- a restart, and a start after the end -----------------------------------------------------------------------


async def _open_session(db, store: GoalStore, goal: Goal) -> uuid.UUID:
    from nous.storage.models import GoalSession

    root = uuid.uuid4()
    async with db.session() as s:
        s.add(
            GoalSession(
                agent_id=store.agent_id,
                goal_id=goal.id,
                n=1,
                trigger="timer",
                first_subtask_id=uuid.uuid4(),
                root_id=root,
            )
        )
        await s.commit()
    return root


async def test_a_restart_rebuilds_the_view_from_the_rows_and_a_failed_rebuild_lifts_nothing(db):
    from nous.autonomy import tool_guard
    from nous.autonomy.tool_guard import ToolGuard
    from nous.goals.grants import GoalToolGrants

    store, _ = _world(db)
    goal = await _goal(store)
    root = await _open_session(db, store, goal)
    tool_guard.install(ToolGuard())

    class Clock:
        now = 50.0

        def __call__(self):
            return self.now

    restarted = GoalToolGrants(db, store._settings, clock=Clock(), commands=True)

    class Broken:
        def session(self):
            raise ConnectionError("down")

    good, restarted._db = restarted._db, Broken()
    assert await restarted.refresh() is False
    assert restarted._commands == {} and restarted.commands_entry(root) is None
    restarted._db = good
    assert await restarted.refresh() is True
    entry = restarted.commands_entry(root)
    async with db.session() as s:
        left = await s.scalar(
            text("SELECT EXTRACT(EPOCH FROM (mode_until - clock_timestamp())) FROM brain.goals WHERE id = :g"),
            {"g": goal.id},
        )
    assert entry.goal_id == goal.id and abs(entry.expires_at - (50.0 + float(left))) < 5


async def test_a_session_that_opens_after_the_end_gets_nothing_before_the_sweep(db):
    from nous.autonomy import tool_guard
    from nous.autonomy.tool_guard import ToolGuard
    from nous.goals.grants import GoalToolGrants

    store, sweeper = _world(db)
    goal = await _goal(store)
    await _set(db, goal.id, mode_until=goal.approved_at - timedelta(seconds=1))
    tool_guard.install(ToolGuard())
    view = GoalToolGrants(db, store._settings, commands=True)
    assert await view.refresh()
    root = await _open_session(db, store, goal)
    async with db.session() as s:
        async with view.publishing(s, root):
            await s.commit()
    assert view.commands_entry(root) is None
    assert await sweeper.expire() == 1


async def test_the_condition_is_read_again_under_the_lock(db, monkeypatch):
    """A goal that was due when the sweep listed it and is not when its row is locked is left as it is: an armed
    draft, and a goal whose end is still ahead."""
    store, sweeper = _world(db)
    draft, running = await _goal(store, approve=False), await _goal(store)

    async def listed(self, condition):
        return [draft.id, running.id]

    monkeypatch.setattr(ModeSweeper, "_candidates", listed)
    assert await sweeper.expire() == 0
    assert (await _get(store, draft.id)).mode == "commands" and (await _get(store, running.id)).mode == "commands"
