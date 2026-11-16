"""Per-goal tool grants, the refresh points: the driver reads the view at its start, at the end of every tick (after
the stop conditions), in a session's publish and after it, after one closes, and right after each pause it makes;
a goal session's decision turn reads its root again, and its done claim that pauses the goal reads the goal's
grants at once. A refresh that fails fails neither the tick, the turn, the pause nor the claim."""

from __future__ import annotations

import contextlib
import uuid
from datetime import timedelta

import pytest
from f099_support import env_factory  # noqa: F401
from test_goals_driver import _active, _finish, _started, _world

from nous.api.execution_context import ExecutionContext
from nous.autonomy import tool_guard
from nous.autonomy.tool_guard import ToolGuard
from nous.goals.chain import GoalChain
from nous.goals.grants import GoalToolGrants
from nous.goals.tools import GoalSessionRef, make_goal_tools

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


# ---- a pause the routes do not make ----------------------------------------------------------------------------


def _note_tool(w, goal, session, view):
    ref = GoalSessionRef(
        goal_id=goal.id,
        session_id=session.id,
        session_n=session.n,
        root_id=session.root_id,
        started_at=session.started_at,
    )
    return make_goal_tools(w.db, w.env.settings, w.store, ref, tool_grants=view)["goal_note"][1]


async def test_a_done_claim_that_pauses_the_goal_reads_its_grants_at_once(env_factory):  # noqa: F811
    w = await _world(env_factory)
    goal = await _active(w)
    _root, session = await _started(w, goal)
    recorder = Recorder()
    note = _note_tool(w, goal, session, recorder)
    answer, is_error = await note(text="Half of the notes are merged.", kind="progress")
    assert is_error is False and recorder.calls == []  # not a pause: nothing to read
    answer, is_error = await note(text="Every duplicate is merged.", kind="done_claim")
    assert is_error is False and "The goal now waits for the owner" in answer
    assert recorder.calls == [("goal", goal.id)]
    recorder.calls.clear()
    answer, is_error = await note(text="Still done.", kind="done_claim")  # the goal waits already: journalled only
    assert is_error is False and recorder.calls == []


def _turn(root_id):
    return ExecutionContext(kind="subtask", authority="internal_only", intention_id=root_id, root_intention_id=root_id)


async def _granted_view(w, goal):
    await w.store.grant_tool(goal.id, "send_email", None, actor="owner", proof="owner")
    tool_guard.install(ToolGuard())
    view = GoalToolGrants(w.db, w.env.settings)
    assert await view.refresh()
    return view


async def test_after_a_done_claim_the_goals_tool_is_lifted_no_more_in_that_turn(env_factory):  # noqa: F811
    w = await _world(env_factory)
    goal = await _active(w)
    root, session = await _started(w, goal)
    view = await _granted_view(w, goal)
    assert view.lifts(_turn(root.id), "send_email") is True
    answer, is_error = await _note_tool(w, goal, session, view)(text="Every duplicate is merged.", kind="done_claim")
    assert is_error is False
    assert view.grant(root.id, "send_email") is None and view.lifts(_turn(root.id), "send_email") is False
    assert view.fresh()  # one goal read: the rest of the view stands


async def test_a_done_claim_whose_read_fails_clears_the_view_and_is_still_noted(env_factory, caplog):  # noqa: F811
    w = await _world(env_factory)
    goal = await _active(w)
    root, session = await _started(w, goal)
    view = await _granted_view(w, goal)

    async def raising(**_kw):
        raise RuntimeError("the read blew up")

    view._read = raising
    answer, is_error = await _note_tool(w, goal, session, view)(text="Every duplicate is merged.", kind="done_claim")
    assert is_error is False and "The goal now waits for the owner" in answer
    assert not view.fresh() and view.lifts(_turn(root.id), "send_email") is False
    assert "RuntimeError" in caplog.text and "blew up" not in caplog.text


async def test_a_stop_condition_pause_reads_the_goals_grants_at_once(env_factory):  # noqa: F811
    w = await _world(env_factory)
    goal = await _active(w)
    root, _session = await _started(w, goal)
    view = await _granted_view(w, goal)
    w.driver.tool_grants = view
    deadline = (await w.store.get(goal.id)).deadline_at
    assert await w.driver.apply_stop_conditions(now=deadline) == 1  # not a tick: no full read after it
    assert (await w.store.get(goal.id)).state == "paused"
    assert view.grant(root.id, "send_email") is None and view.lifts(_turn(root.id), "send_email") is False


async def test_a_stop_condition_pause_asks_for_the_goal_only_once_it_is_written(env_factory):  # noqa: F811
    w = await _world(env_factory)
    goal = await _active(w)
    await _started(w, goal)
    w.driver.tool_grants = recorder = Recorder()
    deadline = (await w.store.get(goal.id)).deadline_at
    assert await w.driver.apply_stop_conditions(now=deadline - timedelta(microseconds=1)) == 0
    assert recorder.calls == []  # nothing paused, nothing read
    assert await w.driver.apply_stop_conditions(now=deadline) == 1
    assert recorder.calls == [("goal", goal.id)]


async def test_the_pause_after_failed_starts_reads_the_goals_grants_at_once(env_factory):  # noqa: F811
    w = await _world(env_factory)
    goal = await _active(w)
    root, _session = await _started(w, goal)
    view = await _granted_view(w, goal)
    w.driver.tool_grants = view
    note = "Paused: three starts in a row failed."
    assert await w.driver._pause_after_failed_starts(goal.id, note, since=7) is False
    assert view.grant(root.id, "send_email") is not None  # nothing written, nothing read
    assert await w.driver._pause_after_failed_starts(goal.id, note, since=0) is True
    assert (await w.store.get(goal.id)).state == "paused"
    assert view.grant(root.id, "send_email") is None and view.lifts(_turn(root.id), "send_email") is False


async def test_a_pause_whose_read_fails_is_still_made(env_factory, caplog):  # noqa: F811
    w = await _world(env_factory)
    goal = await _active(w)
    await _started(w, goal)
    w.driver.tool_grants = recorder = Recorder()

    async def raising(goal_id):
        raise RuntimeError("the read blew up")

    recorder.refresh_goal = raising
    note = "Paused: three starts in a row failed."
    assert await w.driver._pause_after_failed_starts(goal.id, note, since=0) is True
    assert (await w.store.get(goal.id)).state == "paused"
    assert "RuntimeError" in caplog.text and "blew up" not in caplog.text


async def test_a_decision_turns_done_claim_reads_the_goals_grants_through_the_chain(env_factory):  # noqa: F811
    w = await _world(env_factory)
    goal = await _active(w)
    root, _session = await _started(w, goal)
    recorder = Recorder()
    turn = await GoalChain(w.db, w.env.settings, w.store, tool_grants=recorder).turn_context(root.id)
    recorder.calls.clear()  # the turn's own read of its root
    answer, is_error = await turn.tools["goal_note"][1](text="Every duplicate is merged.", kind="done_claim")
    assert is_error is False and recorder.calls == [("goal", goal.id)]
