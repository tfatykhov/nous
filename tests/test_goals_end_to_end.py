"""F114 goals (PR 1c), end to end: a goal the owner approves runs in background sessions and makes checkable progress.

The goal is created and approved through the store; the driver's ticks start and close its sessions; the
continuation runner works each session's chain against a scripted model with the chain's hooks bound as main.py binds
them. Session 1 plans, notes, closes a step with real evidence and reports; it closes as ``step_closed``, its report
is in the journal and the owner's chat got nothing. After the interval session 2 starts, sees the plan and the
journal, and claims the goal is done: the goal waits for the owner.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from f099_support import CHAN, env_factory, runner_env, use  # noqa: F401
from goals_support import (
    active,
    first_result,
    goal_row,
    inbox,
    journal,
    reset_goal_report_channel,  # noqa: F401
    session_row,
    set_goal,
    world,
)
from sqlalchemy import func, select

from nous.goals import spend
from nous.storage.models import GoalSession, Subtask

pytestmark = pytest.mark.postgres_only

FOUND = "Listed 40 notes in the notes folder; 6 of them say the same thing as another."
REPORTED = "Listed the notes and found 6 duplicates; merging them is next."
CLAIM = "The duplicates are merged: no two notes say the same thing."


async def test_a_goal_runs_in_sessions_closes_a_step_with_evidence_and_waits_for_the_owner(runner_env):  # noqa: F811
    first_subtask: dict = {}

    async def close_with_evidence(_call):
        return [
            use(
                "goal_plan",
                steps=[{"id": "s1", "status": "closed", "evidence": [f"subtask:{first_subtask['id']}"]}],
            )
        ]

    w = await world(
        runner_env,
        # Session 1's decision turn: plan, note, close a step with its evidence, report.
        [
            use("goal_plan", steps=[{"text": "List the notes"}, {"text": "Merge the duplicates"}]),
            use("goal_note", kind="progress", text=FOUND),
        ],
        close_with_evidence,
        [use("resolve_intention", decision="report", note=REPORTED, progress=True, confidence=0.8)],
        # Session 2's decision turn: claim the goal done, report.
        [use("goal_note", kind="done_claim", text=CLAIM)],
        [use("resolve_intention", decision="report", note="Merged the duplicates.", progress=True, confidence=0.9)],
    )
    goal = await active(w)

    # ---- session 1 ----
    tick = await w.driver.tick()
    (root_1,) = tick.started
    async with w.db.session() as s:
        session_1 = (await s.execute(select(GoalSession).where(GoalSession.root_id == root_1))).scalar_one()
    root_intention = await w.env.heart.intentions.get_for_source("subtask", session_1.first_subtask_id)
    first_subtask["id"] = await first_result(w, root_intention, body=FOUND)
    assert await w.cont.run_arrival(root_1) is not None
    plan_call = w.model.calls[0]
    assert {"goal_note", "goal_plan"} <= {tool["name"] for tool in plan_call["tools"]}

    tick = await w.driver.tick()
    assert (tick.claimed, tick.closed, tick.started) == (1, 1, ())
    closed = await session_row(w, session_1.id)
    assert (closed.outcome, closed.steps_closed) == ("step_closed", 1)
    assert closed.spent > 0  # the decision turn's model calls
    rows = await journal(w, goal)
    kinds = [(r.kind, r.actor) for r in rows if r.session_n == 1]
    assert kinds == [
        ("plan", "chain"),
        ("progress", "chain"),
        ("plan", "chain"),
        ("report", "chain"),
        ("state", "harness"),
    ]
    assert rows[-1].text.startswith("Session 1 ended: step_closed")
    assert [r.text for r in rows if r.kind == "report"] == [REPORTED]
    s1, s2 = (await goal_row(w, goal)).plan
    assert (s1["status"], s1["evidence"], s1["closed_session"]) == ("closed", [f"subtask:{first_subtask['id']}"], 1)
    assert s2["status"] == "open"
    assert await inbox(w, channel=CHAN) == []  # the owner's chat got nothing
    after_1 = await goal_row(w, goal)
    assert after_1.state == "active"
    assert after_1.next_wake_at == closed.ended_at + timedelta(seconds=goal.min_interval_seconds)
    async with w.db.session() as s:
        total, _today = await spend.goal_spent(s, w.agent, goal.id)
    assert total == closed.spent

    # ---- the interval passes; session 2 ----
    assert (await w.driver.tick()).started == ()  # not due yet
    await set_goal(w, goal, next_wake_at=func.clock_timestamp() - timedelta(seconds=1))
    tick = await w.driver.tick()
    (root_2,) = tick.started
    async with w.db.session() as s:
        session_2 = (await s.execute(select(GoalSession).where(GoalSession.root_id == root_2))).scalar_one()
        task = (await s.execute(select(Subtask.task).where(Subtask.id == session_2.first_subtask_id))).scalar_one()
    assert session_2.n == 2
    assert '<plan_step id="s1" status="closed">' in task and FOUND in task and REPORTED in task
    root_intention_2 = await w.env.heart.intentions.get_for_source("subtask", session_2.first_subtask_id)
    await first_result(w, root_intention_2, body="Merged the six duplicates.")
    assert await w.cont.run_arrival(root_2) is not None

    waiting = await goal_row(w, goal)
    assert (waiting.state, waiting.pause_reason, waiting.next_wake_at) == ("paused", "waiting_for_owner", None)
    (message,) = await inbox(w, channel=CHAN)  # the one owner message
    assert message.msg_type == "REPORT" and "confirm it done" in message.body
    tick = await w.driver.tick()
    assert (tick.closed, tick.started) == (1, ())
    closed_2 = await session_row(w, session_2.id)
    assert closed_2.outcome == "no_step" and closed_2.spent > 0
    assert [r.text for r in await journal(w, goal, "done_claim")] == [CLAIM]
    assert (await goal_row(w, goal)).state == "paused"  # only the owner finishes it
    done = await w.store.confirm_done(goal.id, actor="owner")
    assert done.state == "done"
