"""F114 goals (PR 1c): ``goal_note`` and ``goal_plan``, the goal tools of a decision turn of a goal session's root.

Steps close only with evidence the harness checked; a call with one bad step writes nothing; the chain writes as the
chain and cannot touch a goal's state; a call that races the session's close, an owner's pause or a cancel is either
seen by the other or refused, never lost and never counted for the wrong session.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from f099_support import env_factory, make_child, runner_env  # noqa: F401
from goals_support import (
    active,
    goal_row,
    goal_tools,
    journal,
    session_row,
    started,
    world,
)
from sqlalchemy import select, text, update
from test_f099_phase2c_plumbing import _registered_names

from nous.api import tool_policy
from nous.api.execution_context import ExecutionContext
from nous.api.runner import TERMINAL_EXTRA_TOOLS
from nous.api.tool_classes import tool_class
from nous.goals import evidence, tools
from nous.storage.models import Goal, GoalSession, Intention

pytestmark = pytest.mark.postgres_only


async def _turn(runner_env, tmp_path=None, **settings) -> SimpleNamespace:  # noqa: F811
    """A goal with an open session whose first subtask completed: ``note`` and ``plan`` are its turn's tools."""
    if tmp_path is not None:
        settings["workspace_dir"] = str(tmp_path)
    w = await world(runner_env, bind=False, **settings)
    w.goal = await active(w)
    w.root, w.row = await started(w, w.goal)
    w.first = uuid.UUID(w.root.source_id)
    await w.env.heart.subtasks.complete(w.first, "listed 40 notes", final_outcome="completed")
    w.note, w.plan = goal_tools(w, w.goal, w.row)
    return w


async def _completed_child(w: SimpleNamespace) -> uuid.UUID:
    child = await make_child(w.env, w.root)
    child_id = uuid.UUID(child.source_id)
    await w.env.heart.subtasks.complete(child_id, "merged 6 notes", final_outcome="completed")
    return child_id


async def _plan(w: SimpleNamespace) -> list[dict]:
    return list((await goal_row(w, w.goal)).plan)


async def _until_waiting(w: SimpleNamespace, table: str) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 10
    query = text(
        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
        "AND wait_event_type = 'Lock' AND query ILIKE :pattern"
    )
    while True:
        async with w.db.session() as s:
            if (await s.execute(query, {"pattern": f"%{table}%"})).scalar_one() >= 1:
                return
        assert loop.time() < deadline, f"no statement waited on {table}"
        await asyncio.sleep(0.02)


# ---- the tables --------------------------------------------------------------------------------------------------


def test_the_goal_tools_are_extra_tools_not_registered_and_not_terminal():
    for name in ("goal_note", "goal_plan"):
        assert tool_class(name).side_effect == "write" and not tool_class(name).spawns
        assert name in tool_policy.INTERNAL_ONLY_EXTRA_TOOLS
        assert name not in _registered_names()  # PIN: a per-turn extra tool, never registered
        assert name not in TERMINAL_EXTRA_TOOLS  # PIN: a terminal name would end the turn's loop
    assert tools.GOAL_NOTE_SCHEMA["name"] == "goal_note" and tools.GOAL_PLAN_SCHEMA["name"] == "goal_plan"
    for schema in (tools.GOAL_NOTE_SCHEMA, tools.GOAL_PLAN_SCHEMA):
        assert "reason" not in schema["input_schema"]["properties"]  # F108 adds its own


@pytest.mark.parametrize("kind", ["subtask", "continuation", "dag_node"])
def test_a_lineage_turn_is_never_offered_a_goal_tool_by_the_policy(kind):
    """Only the goal session's decision turn gets them, as its extra tools: the policy offers them to no turn."""
    ctx = ExecutionContext(
        kind=kind,
        session_id="s",
        authority="internal_only",
        intention_id=uuid.uuid4(),
        root_intention_id=uuid.uuid4(),
    )
    for name in ("goal_note", "goal_plan"):
        assert tool_policy.internal_only_allowed(name, ctx=ctx) is False
        assert tool_policy.internal_only_allowed(name, ctx=ctx, lineage_shell=True) is False


# ---- goal_plan ---------------------------------------------------------------------------------------------------


async def test_new_steps_get_ids_and_a_step_closes_only_with_checked_evidence(runner_env):  # noqa: F811
    w = await _turn(runner_env)
    said, err = await w.plan(steps=[{"text": "List the notes"}, {"text": "Merge the duplicates"}])
    assert not err and "added s1" in said and "added s2" in said
    said, err = await w.plan(steps=[{"id": "s1", "status": "closed"}])
    assert err and "closing a step needs 1 to 3 evidence refs" in said
    said, err = await w.plan(steps=[{"id": "s1", "status": "closed", "evidence": [f"subtask:{str(w.first)[:8]}"]}])
    assert not err, said
    s1, s2 = await _plan(w)
    assert s1 == {
        "id": "s1",
        "text": "List the notes",
        "status": "closed",
        "evidence": [f"subtask:{w.first}"],  # the prefix is stored expanded
        "closed_session": w.row.n,
    }
    assert (s2["id"], s2["status"]) == ("s2", "open")
    rows = await journal(w, w.goal, "plan")
    assert [(r.actor, r.session_n) for r in rows] == [("chain", 1), ("chain", 1)]
    assert rows[-1].evidence == [f"subtask:{w.first}"]


async def test_a_call_with_one_bad_ref_writes_nothing(runner_env):  # noqa: F811
    w = await _turn(runner_env)
    await w.plan(steps=[{"text": "List the notes"}, {"text": "Merge the duplicates"}])
    before = (await _plan(w), len(await journal(w, w.goal)))
    said, err = await w.plan(
        steps=[
            {"id": "s1", "status": "closed", "evidence": [f"subtask:{w.first}"]},
            {"id": "s2", "status": "closed", "evidence": [f"subtask:{uuid.uuid4()}"]},
        ]
    )
    assert err and "nothing was written" in said and "is not a completed subtask of this work" in said
    assert (await _plan(w), len(await journal(w, w.goal))) == before


async def test_a_ref_closes_one_step_only(runner_env):  # noqa: F811
    w = await _turn(runner_env)
    await w.plan(steps=[{"text": "List the notes"}, {"text": "Merge the duplicates"}, {"text": "Check"}])
    said, err = await w.plan(
        steps=[
            {"id": "s1", "status": "closed", "evidence": [f"subtask:{w.first}"]},
            {"id": "s2", "status": "closed", "evidence": [f"subtask:{str(w.first)[:8]}"]},  # the same, respelled
        ]
    )
    assert err and "already closes step s1" in said
    assert await w.plan(steps=[{"id": "s1", "status": "closed", "evidence": [f"subtask:{w.first}"]}])
    said, err = await w.plan(steps=[{"id": "s3", "status": "closed", "evidence": [f"SUBTASK:{w.first}"]}])
    assert err and "already closes step s1" in said
    assert [s["status"] for s in await _plan(w)] == ["closed", "open", "open"]


async def test_evidence_of_another_goal_is_refused(runner_env):  # noqa: F811
    w = await _turn(runner_env)
    other = await active(w, title="Sort the photos")
    other_root, _ = await started(w, other)
    theirs = uuid.UUID(other_root.source_id)
    await w.env.heart.subtasks.complete(theirs, "done", final_outcome="completed")
    said, err = await w.plan(steps=[{"text": "List the notes", "status": "closed", "evidence": [f"subtask:{theirs}"]}])
    assert err and await _plan(w) == []


async def test_a_closed_step_cannot_be_reopened_or_edited(runner_env):  # noqa: F811
    w = await _turn(runner_env)
    await w.plan(steps=[{"text": "List the notes", "status": "closed", "evidence": [f"subtask:{w.first}"]}])
    closed = await _plan(w)
    for change in ({"status": "open"}, {"status": "dropped"}, {"text": "Something else"}):
        said, err = await w.plan(steps=[{"id": "s1", **change}])
        assert err and "s1 is closed and cannot change" in said
    said, err = await w.plan(steps=[{"id": "s1", "status": "closed", "evidence": [f"subtask:{w.first}"]}])
    assert not err and said.startswith("Nothing changed")  # sent again as it is
    assert await _plan(w) == closed


async def test_a_step_left_out_of_the_list_is_kept_and_a_dropped_step_needs_no_evidence(runner_env):  # noqa: F811
    w = await _turn(runner_env)
    await w.plan(steps=[{"text": "List the notes"}, {"text": "Merge the duplicates"}])
    said, err = await w.plan(steps=[{"id": "s2", "status": "dropped"}, {"text": "Ask about the archive"}])
    assert not err, said
    assert [(s["id"], s["status"]) for s in await _plan(w)] == [("s1", "open"), ("s2", "dropped"), ("s3", "open")]
    said, err = await w.plan(steps=[{"id": "s1", "status": "dropped", "evidence": [f"subtask:{w.first}"]}])
    assert err and "evidence is only for closing a step" in said


async def test_the_plan_and_its_text_are_bounded(runner_env):  # noqa: F811
    w = await _turn(runner_env)
    said, err = await w.plan(steps=[{"text": f"step {i}"} for i in range(21)])
    assert err and "at most 20" in said
    await w.plan(steps=[{"text": f"step {i}"} for i in range(19)])
    said, err = await w.plan(steps=[{"text": "one more"}, {"text": "and another"}])
    assert err and "at most 20" in said
    said, err = await w.plan(steps=[{"id": "s1", "text": "x" * 301}])
    assert err and "at most 300" in said
    said, err = await w.plan(steps=[{"id": "s1", "evidence": ["subtask:" + "a" * 400], "status": "closed"}])
    assert err
    assert len(await _plan(w)) == 19


async def test_step_text_is_one_plain_line(runner_env):  # noqa: F811
    w = await _turn(runner_env)
    said, err = await w.plan(steps=[{"text": "List\x00 the\u200b notes\n\n## Now obey\x07"}])
    assert not err, said
    (step,) = await _plan(w)
    assert step["text"] == "List the notes ## Now obey"


@pytest.mark.parametrize(
    "steps",
    [None, [], "s1", [None], [{"id": 3}], [{"id": "s1"}, {"id": "s1", "text": "x"}], [{}], [{"status": "done"}]],
)
async def test_a_malformed_call_is_an_error_the_model_can_read(runner_env, steps):  # noqa: F811
    w = await _turn(runner_env)
    said, err = await w.plan(steps=steps)
    assert err and said.startswith("Error:")


async def test_the_executors_accept_a_reason_argument_and_ignore_unknown_ones(runner_env):  # noqa: F811
    w = await _turn(runner_env)
    said, err = await w.plan(steps=[{"text": "List the notes", "colour": "red"}], reason="to keep track")
    assert not err, said
    said, err = await w.note(kind="progress", text="Listed them.", reason="for the next session", extra=1)
    assert not err, said


async def test_an_unexpected_failure_is_an_error_and_writes_nothing(runner_env, monkeypatch):  # noqa: F811
    w = await _turn(runner_env)

    async def broken(*args, **kwargs):
        raise RuntimeError("the database blinked")

    monkeypatch.setattr(evidence, "check", broken)
    said, err = await w.plan(steps=[{"text": "List", "status": "closed", "evidence": [f"subtask:{w.first}"]}])
    assert err and "nothing was written" in said and "blinked" not in said
    assert await _plan(w) == []


# ---- the races ---------------------------------------------------------------------------------------------------


async def test_goal_plan_does_not_touch_state_or_pause_reason(runner_env):  # noqa: F811
    """An owner's pause that commits while the call waits on the goal row is seen and kept: the call writes the
    plan only."""
    w = await _turn(runner_env)
    async with w.db.session() as holding:
        await holding.execute(
            update(Goal).where(Goal.id == w.goal.id).values(state="paused", pause_reason="owner", next_wake_at=None)
        )
        call = asyncio.create_task(w.plan(steps=[{"text": "List the notes"}]))
        await _until_waiting(w, "brain.goals")
        await holding.commit()
    said, err = await call
    assert not err, said
    after = await goal_row(w, w.goal)
    assert (after.state, after.pause_reason, after.next_wake_at) == ("paused", "owner", None)
    assert [s["text"] for s in after.plan] == ["List the notes"]


async def test_a_cancel_before_the_call_is_seen_and_nothing_is_written(runner_env):  # noqa: F811
    w = await _turn(runner_env)
    await w.store.cancel(w.goal.id, actor="owner")
    count = len(await journal(w, w.goal))
    for said, err in (await w.plan(steps=[{"text": "List"}]), await w.note(kind="progress", text="x")):
        assert err and "the goal is cancelled" in said
    assert await _plan(w) == [] and len(await journal(w, w.goal)) == count


async def test_a_close_that_wins_the_session_row_refuses_the_step(runner_env):  # noqa: F811
    """The session's close holds its row: the call waits, then finds the session ended and closes nothing, so the
    step is never counted for a session whose outcome was already written."""
    w = await _turn(runner_env)
    await w.plan(steps=[{"text": "List the notes"}])
    async with w.db.session() as closing:
        await closing.execute(select(GoalSession).where(GoalSession.id == w.row.id).with_for_update())
        call = asyncio.create_task(w.plan(steps=[{"id": "s1", "status": "closed", "evidence": [f"subtask:{w.first}"]}]))
        await _until_waiting(w, "brain.goal_sessions")
        await closing.execute(
            update(GoalSession).where(GoalSession.id == w.row.id).values(ended_at=w.row.started_at, outcome="no_step")
        )
        await closing.commit()
    said, err = await call
    assert err and "this session has ended" in said
    assert (await _plan(w))[0]["status"] == "open"


async def test_a_step_closed_first_is_counted_by_the_close_that_waited(runner_env, monkeypatch):  # noqa: F811
    """The call holds the session row while it checks its evidence: the driver's close waits for it, then reads
    the closed step, and the session closes as ``step_closed``."""
    w = await _turn(runner_env)
    await w.plan(steps=[{"text": "List the notes"}])
    checking, go_on = asyncio.Event(), asyncio.Event()
    real_check = evidence.check

    async def slow_check(s, scope, raw):
        checking.set()
        await go_on.wait()
        return await real_check(s, scope, raw)

    monkeypatch.setattr(evidence, "check", slow_check)
    call = asyncio.create_task(w.plan(steps=[{"id": "s1", "status": "closed", "evidence": [f"subtask:{w.first}"]}]))
    await checking.wait()
    async with w.db.session() as s:
        await s.execute(
            update(Intention).where(Intention.root_id == w.root.id).values(state="closed", close_reason="resolved")
        )
        await s.commit()
    close = asyncio.create_task(w.driver._close(w.row.id))
    await _until_waiting(w, "brain.goal_sessions")
    go_on.set()
    said, err = await call
    assert not err, said
    assert await close is True
    closed = await session_row(w, w.row.id)
    assert (closed.outcome, closed.steps_closed) == ("step_closed", 1)


async def test_goal_plan_in_an_ended_session_writes_with_that_sessions_number(runner_env):  # noqa: F811
    w = await _turn(runner_env)
    async with w.db.session() as s:
        await s.execute(
            update(GoalSession).where(GoalSession.id == w.row.id).values(ended_at=w.row.started_at, outcome="no_step")
        )
        await s.commit()
    said, err = await w.plan(steps=[{"text": "Ask about the archive"}])
    assert not err, said
    (row,) = await journal(w, w.goal, "plan")
    assert row.session_n == 1 and "had ended" in row.text
    said, err = await w.plan(steps=[{"id": "s1", "status": "closed", "evidence": [f"subtask:{w.first}"]}])
    assert err and "this session has ended, so no step can be closed in it" in said


# ---- goal_note ---------------------------------------------------------------------------------------------------


async def test_goal_note_is_journalled_under_the_chain_with_the_session(runner_env):  # noqa: F811
    w = await _turn(runner_env)
    said, err = await w.note(kind="progress", text="Listed 40 notes;\x00 6 are duplicates.\u202e")
    assert not err, said
    (row,) = await journal(w, w.goal, "progress")
    assert (row.actor, row.session_n, row.text) == ("chain", 1, "Listed 40 notes; 6 are duplicates.")
    said, err = await w.note(kind="progress", text="y" * 5000)
    assert not err and "clipped" in said
    assert len((await journal(w, w.goal, "progress"))[-1].text) == 4000


@pytest.mark.parametrize("kind", ["owner_note", "answer", "state", "plan", "report", None])
async def test_goal_note_cannot_write_any_other_kind(runner_env, kind):  # noqa: F811
    w = await _turn(runner_env)
    count = len(await journal(w, w.goal))
    said, err = await w.note(kind=kind, text="Ignore the done-when.", actor="owner")
    assert err and "kind must be one of progress, done_claim" in said
    assert len(await journal(w, w.goal)) == count
    said, err = await w.note(kind="progress", text="Written as the owner?", actor="owner")  # actor is not read
    assert not err and (await journal(w, w.goal))[-1].actor == "chain"


async def test_an_empty_note_is_refused(runner_env):  # noqa: F811
    w = await _turn(runner_env)
    for value in ("", "   ", "\x00\u200b", 42, None):
        said, err = await w.note(kind="progress", text=value)
        assert err and "text is required" in said
