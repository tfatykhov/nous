"""F114 goals: a goal's chain never takes the continuation slots kept for the owner's own chains (PR 1b-ii).

The continuation runner asks the bound goal driver which due roots are goal sessions' (``goal_roots``) and keeps
those out of the last ``goals_reserved_slots`` free slots. With no driver bound (goals off) the sweep is the one it
was. Modelled on ``tests/test_f099_phase2c_loop.py``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from f099_support import (
    age,
    env_factory,  # noqa: F401
    make_root,
    record,
    runner_env,  # noqa: F401
    use,
)
from test_goals_driver import CHAN

from nous.brain import intentions
from nous.goals.driver import GoalDriver
from nous.goals.store import GoalStore
from nous.handlers.continuation_runner import ContinuationRunner
from nous.storage.models import Intention

pytestmark = pytest.mark.postgres_only

GOALS = {"subtask_enabled": True, "goals_enabled": True, "heartbeat_quiet_start": 0, "heartbeat_quiet_end": 0}


def resolve():
    return use("resolve_intention", decision="drop", note="Nothing more to do.", progress=False, confidence=0.7)


async def _world(runner_env, *steps, slots: int, bind: bool = True) -> SimpleNamespace:  # noqa: F811
    env = await runner_env(*steps, continuation_max_concurrent=slots, **GOALS)
    cont = ContinuationRunner(
        database=env.db, settings=env.settings, runner=env.runner, heart=env.heart, brain=env.brain, bus=env.bus
    )
    store = GoalStore(env.db, env.agent, env.settings)
    driver = GoalDriver(env.db, env.settings, env.heart, store, cont)
    if bind:
        cont.set_goals(driver)
    return SimpleNamespace(env=env, cont=cont, store=store, driver=driver)


async def _owner_root(w: SimpleNamespace, *, older: float = 0):
    root = await make_root(w.env)
    await record(w.env, root)
    if older:
        await age(w.env, root.id, seconds=older)
    return root


async def _goal_root(w: SimpleNamespace, *, older: float = 0, title: str = "Tidy the notes"):
    goal = await w.store.create(
        title=title,
        objective="Merge the duplicate notes",
        done_when="None are left",
        proposed_by="host",
        origin_channel=CHAN,
    )
    await w.store.approve(goal.id, actor="host")
    root_id = await w.driver.start_session(goal.id, trigger="timer")
    assert root_id is not None
    async with w.env.db.session() as s:
        root = await s.get(Intention, root_id)
    await record(w.env, root)  # its first task's result: the root is due
    if older:
        await age(w.env, root.id, seconds=older)
    return root


def _blocking():
    started, go = asyncio.Event(), asyncio.Event()

    async def step(kwargs):
        started.set()
        await go.wait()
        return [resolve()]

    return started, go, step


async def _settle(cont: ContinuationRunner) -> None:
    tasks = list(cont._running.values())
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=30)


async def test_a_goal_root_does_not_take_the_last_free_slot(runner_env):  # noqa: F811
    """Two slots, one owner chain running: a due goal root waits, and a due owner root after it in the list goes
    (``continue``, not ``break``)."""
    started, go, blocked = _blocking()
    w = await _world(runner_env, blocked, [resolve()], slots=2)
    owner = await _owner_root(w, older=300)
    assert (await w.cont.run_once()).launched == (owner.id,)
    await asyncio.wait_for(started.wait(), timeout=10)
    goal_root = await _goal_root(w, older=200)  # due before the second owner root
    second = await _owner_root(w, older=100)
    assert (await w.cont.run_once()).launched == (second.id,)
    assert goal_root.id not in w.cont.running_roots
    go.set()
    await _settle(w.cont)


async def test_a_goal_root_takes_a_slot_when_two_are_free(runner_env):  # noqa: F811
    w = await _world(runner_env, [resolve()], slots=2)
    goal_root = await _goal_root(w)
    assert (await w.cont.run_once()).launched == (goal_root.id,)
    await _settle(w.cont)


async def test_with_one_slot_a_goal_root_still_runs(runner_env):  # noqa: F811
    w = await _world(runner_env, [resolve()], slots=1)
    goal_root = await _goal_root(w)
    assert (await w.cont.run_once()).launched == (goal_root.id,)
    await _settle(w.cont)


async def test_a_held_goal_root_launches_when_the_owners_chain_ends(runner_env):  # noqa: F811
    started, go, blocked = _blocking()
    w = await _world(runner_env, blocked, [resolve()], slots=2)
    owner = await _owner_root(w, older=300)
    assert (await w.cont.run_once()).launched == (owner.id,)
    await asyncio.wait_for(started.wait(), timeout=10)
    goal_root = await _goal_root(w)
    assert (await w.cont.run_once()).launched == ()
    w.cont._wake.clear()
    go.set()
    await _settle(w.cont)
    await asyncio.sleep(0)  # the done callback runs after the task
    assert w.cont._wake.is_set()  # the arrival's end wakes the loop, whose next sweep launches the goal root
    assert (await w.cont.run_once()).launched == (goal_root.id,)
    await _settle(w.cont)


async def test_a_failed_goal_lookup_never_holds_the_owners_chains(runner_env, monkeypatch):  # noqa: F811
    started, go, blocked = _blocking()
    w = await _world(runner_env, blocked, [resolve()], slots=2)
    owner = await _owner_root(w, older=300)
    assert (await w.cont.run_once()).launched == (owner.id,)
    await asyncio.wait_for(started.wait(), timeout=10)
    second = await _owner_root(w)

    async def broken(root_ids):
        raise RuntimeError("the database went away")

    monkeypatch.setattr(w.driver, "goal_roots", broken)
    assert (await w.cont.run_once()).launched == (second.id,)
    go.set()
    await _settle(w.cont)


async def test_flag_off_launch_is_unchanged(runner_env):  # noqa: F811
    """No driver bound (goals off): the lookup is never made, and the sweep launches in due order as before, so the
    goal root, due first, takes the last slot and the owner root after it waits."""
    started, go, blocked = _blocking()
    w = await _world(runner_env, blocked, [resolve()], slots=2, bind=False)
    lookup = MagicMock(side_effect=AssertionError("goal_roots was called with goals off"))
    w.driver.goal_roots = lookup  # bound to nothing: the runner holds no driver
    assert w.cont._goals is None
    owner = await _owner_root(w, older=300)
    assert (await w.cont.run_once()).launched == (owner.id,)
    await asyncio.wait_for(started.wait(), timeout=10)
    goal_root = await _goal_root(w, older=200)
    second = await _owner_root(w, older=100)
    assert (await w.cont.run_once()).launched == (goal_root.id,)
    assert second.id not in w.cont.running_roots
    lookup.assert_not_called()
    go.set()
    await _settle(w.cont)


async def test_unbinding_the_driver_restores_the_sweep(runner_env):  # noqa: F811
    started, go, blocked = _blocking()
    w = await _world(runner_env, blocked, [resolve()], slots=2)
    w.cont.set_goals(None)
    owner = await _owner_root(w, older=300)
    assert (await w.cont.run_once()).launched == (owner.id,)
    await asyncio.wait_for(started.wait(), timeout=10)
    goal_root = await _goal_root(w)
    assert (await w.cont.run_once()).launched == (goal_root.id,)
    go.set()
    await _settle(w.cont)


async def test_goal_roots_names_only_roots_of_goal_sessions(runner_env):  # noqa: F811
    w = await _world(runner_env, slots=2)
    owner = await _owner_root(w)
    goal_root = await _goal_root(w)
    assert goal_root.origin_kind == intentions.ORIGIN_GOAL
    assert await w.driver.goal_roots([owner.id, goal_root.id]) == frozenset({goal_root.id})
    assert await w.driver.goal_roots([]) == frozenset()
