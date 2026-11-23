"""The default goal prompt (``nous.goals.default_prompt``): the harness rules each of its sentences states.

Each test names the sentence it backs. When one fails, the rule changed and the default text must be reworded before
the change merges; the test is not bent to the text.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from f099_support import (  # noqa: F401
    CONT,
    age,
    claim,
    env_factory,
    intention_of,
    make_child,
    make_dag,
    make_root,
    record,
    runner_env,
)
from goals_support import active, goal_tools, started, world
from sqlalchemy import update

from nous.api import runner, tool_policy
from nous.brain.continuation import RootLimits
from nous.config import Settings
from nous.goals import charter, evidence
from nous.goals.default_prompt import DEFAULT_GOAL_PROMPT
from nous.handlers.continuation_runner import build_arrival_prompt
from nous.storage.models import Subtask

pytestmark = pytest.mark.postgres_only


async def _turn(runner_env) -> SimpleNamespace:  # noqa: F811
    """A goal with an open session whose first task completed: ``plan`` is its follow-up turn's goal_plan."""
    w = await world(runner_env, bind=False)
    w.goal = await active(w)
    w.root, w.row = await started(w, w.goal)
    w.first = uuid.UUID(w.root.source_id)
    await w.env.heart.subtasks.complete(w.first, "listed 40 notes", final_outcome="completed")
    _note, w.plan = goal_tools(w, w.goal, w.row)
    return w


async def _completed_child(w: SimpleNamespace) -> uuid.UUID:
    """A task the follow-up turn started (spawn_task writes a child of the root), completed."""
    child = await make_child(w.env, w.root)
    child_id = uuid.UUID(child.source_id)
    await w.env.heart.subtasks.complete(child_id, "merged 6 notes", final_outcome="completed")
    return child_id


# ---- "A task cannot start other tasks or workflows. Only a follow-up turn can: spawn_task ..., dag_create ..." ----


def test_only_a_follow_up_turn_may_start_tasks_and_workflows():
    assert tool_policy.CONTEXT_POLICY["subtask"].spawn is False
    assert tool_policy.CONTEXT_POLICY["dag_node"].spawn is False
    assert tool_policy.CONTEXT_POLICY["continuation"].spawn == frozenset({"spawn_task", "dag_create"})
    assert "spawn_task" in runner._SUBTASK_EXCLUDED_TOOLS
    assert "spawn_task" in DEFAULT_GOAL_PROMPT and "dag_create" in DEFAULT_GOAL_PROMPT


def test_goal_plan_and_goal_note_are_tools_of_the_follow_up_turn():
    assert {"goal_plan", "goal_note"} <= tool_policy.INTERNAL_ONLY_EXTRA_TOOLS
    assert "- goal_plan:" in charter.RECORDING and "- goal_note:" in charter.RECORDING


# ---- "a plan step is closed by citing a completed task or workflow of this session, and each citation closes one step
# ---- only"


async def test_each_citation_closes_one_step_only_and_a_workflow_is_one_citation(runner_env):  # noqa: F811
    w = await _turn(runner_env)
    await w.plan(steps=[{"text": "List the notes"}, {"text": "Merge the duplicates"}, {"text": "Check"}])
    dag, _store = await make_dag(w.env, parent=w.root)
    said, err = await w.plan(steps=[{"id": "s1", "status": "closed", "evidence": [f"dag:{dag.id}"]}])
    assert not err, said
    said, err = await w.plan(steps=[{"id": "s2", "status": "closed", "evidence": [f"dag:{dag.id}"]}])
    assert err and "already closes step s1" in said
    assert "each ref closing one step only" in charter.RECORDING


async def test_a_task_of_a_workflow_is_not_a_citation_of_its_own(runner_env):  # noqa: F811
    """A workflow's task is created as the orchestrator creates it: the workflow's lineage stamp in its metadata, its
    node's id, and no intention row of its own. Only the workflow is citeable, so it closes one step."""
    w = await _turn(runner_env)
    await w.plan(steps=[{"text": "List the notes"}, {"text": "Merge the duplicates"}])
    dag, store = await make_dag(w.env, parent=w.root)
    lineage = await store.intention_lineage(dag.id)
    assert lineage is not None
    node = dag.nodes[0]
    task = await w.env.heart.subtasks.create(
        task="merge one folder",
        metadata={"dag_id": str(dag.id), "node_name": node.name, "intention": lineage},
        dag_node_id=node.id,
    )
    await w.env.heart.subtasks.complete(task.id, "merged", final_outcome="completed")
    scope = evidence.Scope(
        agent_id=w.agent,
        root_id=w.root.id,
        started_at=w.row.started_at,
        workspace_dir=str(w.env.settings.workspace_dir),
    )
    async with w.db.session() as s:
        refs = [c.ref for c in await evidence.citeable(s, scope, limit=charter.REFS_SHOWN)]
    assert f"dag:{dag.id}" in refs and f"subtask:{task.id}" not in refs
    said, err = await w.plan(steps=[{"id": "s1", "status": "closed", "evidence": [f"subtask:{task.id}"]}])
    assert err and "is not a completed subtask of this work" in said


# ---- "When several open steps do not depend on each other, start one task for each (spawn_task) in this same turn" -


async def test_tasks_started_in_one_turn_each_close_their_own_step(runner_env):  # noqa: F811
    w = await _turn(runner_env)
    await w.plan(steps=[{"text": "Merge folder A"}, {"text": "Merge folder B"}, {"text": "Check"}])
    first, second = await _completed_child(w), await _completed_child(w)
    said, err = await w.plan(
        steps=[
            {"id": "s1", "status": "closed", "evidence": [f"subtask:{first}"]},
            {"id": "s2", "status": "closed", "evidence": [f"subtask:{second}"]},
        ]
    )
    assert not err, said
    said, err = await w.plan(steps=[{"id": "s3", "status": "closed", "evidence": [f"subtask:{first}"]}])
    assert err and "already closes step s1" in said


# ---- "They run side by side as far as the task workers allow and the rest wait in the queue" ----------------------


async def test_waiting_tasks_are_handed_out_oldest_first(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    tasks = [await env.heart.subtasks.create(task=f"merge folder {n}") for n in range(3)]
    now = datetime.now(UTC)
    ages = {tasks[0].id: 1, tasks[1].id: 3, tasks[2].id: 2}  # minutes old: the second task is the oldest
    async with env.db.session() as s:
        for task_id, minutes in ages.items():
            await s.execute(
                update(Subtask).where(Subtask.id == task_id).values(created_at=now - timedelta(minutes=minutes))
            )
        await s.commit()
    taken = await env.heart.subtasks.dequeue("worker-1")
    assert taken is not None and taken.id == tasks[1].id
    rest = [await env.heart.subtasks.get(t.id) for t in (tasks[0], tasks[2])]
    assert [t.status for t in rest] == ["pending", "pending"]
    assert (await env.heart.subtasks.dequeue("worker-2")).id == tasks[2].id


def test_the_number_of_task_workers_is_a_setting():
    """The text names no number: the pool's size and its ceiling are settings."""
    fields = Settings.model_fields
    assert "subtask_workers" in fields and "subtask_max_concurrent" in fields
    assert not any(ch.isdigit() for ch in DEFAULT_GOAL_PROMPT)


# ---- "their results may be read in one follow-up turn or in several" ----------------------------------------------
# The claim is continuation.claim_root (f099_support.claim) with the settings' debounce and longest wait: it takes
# every ready result of the root once the newest is past the debounce or the oldest past the longest wait, and none
# while a turn of the root is deciding.


def _waits(env) -> dict[str, float]:
    return {
        "debounce": env.settings.continuation_debounce_seconds,
        "max_wait": env.settings.continuation_max_wait_seconds,
    }


async def test_results_that_finish_close_together_are_read_in_one_follow_up_turn(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    waits = _waits(env)
    between = (waits["debounce"] + waits["max_wait"]) / 2  # past the debounce, before the longest wait
    root = await make_root(env)
    a, b = await make_child(env, root), await make_child(env, root)
    await record(env, a)
    await record(env, b)
    assert await claim(env, root.id, **waits) is None  # the newest result is fresh: the turn waits for it
    await age(env, a.id, seconds=between)
    assert await claim(env, root.id, **waits) is None  # the wait runs from the newest result
    await age(env, a.id, b.id, seconds=between)
    got = await claim(env, root.id, **waits)
    assert got is not None and {i.id for i in got.intentions} == {a.id, b.id}


async def test_results_that_keep_coming_are_read_together_once_the_oldest_waited_the_longest(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    waits = _waits(env)
    root = await make_root(env)
    a, b = await make_child(env, root), await make_child(env, root)
    await record(env, a)
    await record(env, b)
    await age(env, a.id, seconds=waits["max_wait"] + 1)  # b is fresh
    got = await claim(env, root.id, **waits)
    assert got is not None and {i.id for i in got.intentions} == {a.id, b.id}


async def test_a_result_that_finishes_later_is_read_in_a_later_follow_up_turn(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    waits = _waits(env)
    between = (waits["debounce"] + waits["max_wait"]) / 2
    root = await make_root(env)
    a, b = await make_child(env, root), await make_child(env, root)
    await record(env, a)
    await age(env, a.id, seconds=between)  # b is still running: no result yet
    got = await claim(env, root.id, **waits)
    assert got is not None and [i.id for i in got.intentions] == [a.id]
    assert (await intention_of(env, "subtask", b.source_id)).state == b.state  # left for a later turn
    await record(env, b)
    await age(env, b.id, seconds=between)
    assert await claim(env, root.id, **waits) is None  # not while the turn that reads a is deciding


# ---- "The Limits of a follow-up turn show what is left of spawned work, follow-up turns and tokens, and whether more
# ---- work can be started"


def _limits_prompt(*, blocked: bool) -> str:
    intention = SimpleNamespace(
        id=uuid.uuid4(),
        intent="Goal G-0001, session 1: Tidy the notes",
        depth=0,
        origin_kind="goal",
        origin_decision_id=None,
        state="deciding",
        close_reason=None,
        source_kind="subtask",
        source_id=str(uuid.uuid4()),
    )
    found = SimpleNamespace(root_id=intention.id, intentions=(intention,), deepest=intention, inbox_rows=())
    limits = RootLimits(depth=1, spawns=2, turns=1, tokens=500, stalls=0, spawn_blocked=blocked, escalate=None)
    settings = Settings(_env_file=None, result_inbox_enabled=True, intentions_enabled=True, continuation_enabled=True)
    return build_arrival_prompt(found, [], [], limits, settings)


def test_the_limits_of_a_follow_up_turn_show_spawned_work_turns_tokens_and_whether_more_can_start():
    for blocked, line in ((False, "You may spawn more work"), (True, "You cannot spawn more work")):
        text = _limits_prompt(blocked=blocked)
        limits = text[text.index("## Limits") :]
        assert "spawned 2 of" in limits and "follow-up turns used 1 of" in limits and "tokens 500 of" in limits
        assert line in limits
    assert "Limits" in DEFAULT_GOAL_PROMPT and "follow-up turn" in DEFAULT_GOAL_PROMPT


# ---- "Aim to close at least one plan step each session" -------------------------------------------------------------


def test_a_session_counts_as_progress_only_when_it_closes_a_plan_step():
    assert "A session counts as progress only when it closes a plan step." in charter.RECORDING
