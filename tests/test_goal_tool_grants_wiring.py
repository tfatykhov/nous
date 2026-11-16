"""Per-goal tool grants, the refresh points: the driver reads the view at its start, at the end of every tick (after
the stop conditions), in a session's publish and after it, and after one closes; a goal session's decision turn reads
its root again. A refresh that fails fails neither the tick nor the turn."""

from __future__ import annotations

import contextlib
import uuid

import pytest
from f099_support import env_factory  # noqa: F401
from test_goals_driver import _active, _finish, _started, _world

from nous.goals.chain import GoalChain
from nous.goals.grants import GoalToolGrants

pytestmark = pytest.mark.postgres_only


class Recorder:
    """A grants view that records which reads were asked for; with ``fail`` each one reports a failure."""

    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple] = []
        self.fail = fail

    async def refresh(self) -> bool:
        self.calls.append(("all",))
        return not self.fail

    async def refresh_root(self, root_id) -> bool:
        self.calls.append(("root", root_id))
        return not self.fail

    async def refresh_goal(self, goal_id) -> bool:
        self.calls.append(("goal", goal_id))
        return not self.fail

    @contextlib.asynccontextmanager
    async def publishing(self, session, root_id):
        self.calls.append(("publish", root_id))
        yield


class Broken:
    def session(self):
        raise ConnectionError("down")


async def test_the_driver_reads_the_view_at_its_start(env_factory):  # noqa: F811
    w = await _world(env_factory)
    w.driver.tool_grants = recorder = Recorder()
    await w.driver.start()
    await w.driver.stop()
    assert recorder.calls[0] == ("all",)


async def test_a_tick_reads_the_view_last_after_the_stop_conditions(env_factory, monkeypatch):  # noqa: F811
    w = await _world(env_factory)
    w.driver.tool_grants = recorder = Recorder()
    order: list[str] = []
    real = w.driver.apply_stop_conditions

    async def stop_conditions(**kw):
        order.append("stop_conditions")
        return await real(**kw)

    monkeypatch.setattr(w.driver, "apply_stop_conditions", stop_conditions)
    real_refresh = recorder.refresh

    async def refresh():
        order.append("refresh")
        return await real_refresh()

    recorder.refresh = refresh
    await w.driver.tick()
    assert order == ["stop_conditions", "refresh"]


async def test_a_started_session_and_a_closed_one_have_their_root_read(env_factory):  # noqa: F811
    w = await _world(env_factory)
    goal = await _active(w)
    w.driver.tool_grants = recorder = Recorder()
    root, _session = await _started(w, goal)
    assert recorder.calls == [("publish", root.id), ("root", root.id)]
    recorder.calls.clear()
    await _finish(w, root)
    report = await w.driver.tick()
    assert report.closed == 1
    assert ("root", root.id) in recorder.calls and recorder.calls[-1] == ("all",)
    assert recorder.calls.index(("root", root.id)) < len(recorder.calls) - 1


async def test_a_failing_view_fails_no_tick_and_no_start(env_factory, caplog):  # noqa: F811
    w = await _world(env_factory)
    goal = await _active(w)
    view = GoalToolGrants(Broken(), w.env.settings)
    w.driver.tool_grants = view
    root, _session = await _started(w, goal)
    await _finish(w, root)
    report = await w.driver.tick()
    assert report.closed == 1
    assert "ConnectionError" in caplog.text and not view.fresh()


async def test_a_goal_sessions_decision_turn_reads_its_root_again(env_factory):  # noqa: F811
    w = await _world(env_factory)
    goal = await _active(w)
    root, _session = await _started(w, goal)
    recorder = Recorder()
    chain = GoalChain(w.db, w.env.settings, w.store, tool_grants=recorder)
    assert await chain.turn_context(uuid.uuid4()) is None
    assert recorder.calls == []  # not a goal's root: nothing to read
    turn = await chain.turn_context(root.id)
    assert turn is not None and recorder.calls == [("root", root.id)]
    failing = GoalChain(w.db, w.env.settings, w.store, tool_grants=GoalToolGrants(Broken(), w.env.settings))
    assert await failing.turn_context(root.id) is not None
