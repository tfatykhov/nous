"""GET /dashboard/loop-numbers data: tokens per finished unit and the share of finished units nobody outside checked.

Seeded rows, one agent per test. Real Postgres: a workflow step's subtask row points at a DAG node, and a goal session
at a goal.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio

from nous.api import loop_numbers_dashboard as ln
from nous.observability.task_scoring import SCORER_VERSION
from nous.storage.models import DAGNode, ExecutionDAG, Goal, GoalSession, ScorerHeartbeat, Subtask, TaskScore

pytestmark = pytest.mark.scoring_writer

NOW = datetime.now(UTC)


@pytest_asyncio.fixture
async def agent() -> str:
    return f"test-loop-numbers-{uuid.uuid4().hex[:8]}"


async def add(db, *rows) -> None:
    async with db.session() as s:
        s.add_all(rows)
        await s.commit()


async def numbers(db, agent, *, days: int = 7, claims_recorded: bool = True) -> dict[str, Any]:
    async with db.session() as s:
        return await ln.get_loop_numbers(s, agent, days=days, claims_recorded=claims_recorded, now=NOW)


def task(
    agent: str,
    *,
    status: str = "completed",
    final: str | None = "completed",
    tokens: tuple[int, int] = (100, 10),
    age_days: float = 1,
) -> Subtask:
    return Subtask(
        id=uuid.uuid4(),
        agent_id=agent,
        task="a task",
        status=status,
        final_outcome=final,
        tokens_in=tokens[0],
        tokens_out=tokens[1],
        completed_at=NOW - timedelta(days=age_days),
    )


async def step(db, agent: str, **kw: Any) -> Subtask:
    """A workflow step's subtask row: its DAG node exists (the foreign key)."""
    dag = ExecutionDAG(id=uuid.uuid4(), agent_id=agent, name="d", status="completed")
    node = DAGNode(id=uuid.uuid4(), dag_id=dag.id, name=f"n-{uuid.uuid4().hex[:6]}", node_type="subtask")
    sub = task(agent, **kw)
    sub.dag_node_id = node.id
    await add(db, dag)
    await add(db, node, sub)
    return sub


def score(
    agent: str,
    sub: Subtask,
    outcome: str,
    *,
    settled: bool = False,
    claims: int = 0,
    version: int = SCORER_VERSION,
    kind: str = "subtask",
) -> TaskScore:
    return TaskScore(
        agent_id=agent,
        task_kind=kind,
        task_id=str(sub.id),
        finished_at=sub.completed_at,
        scorer_version=version,
        outcome=outcome,
        score=None if outcome == "not_scored" or kind != "subtask" else 0.0,
        settled=settled,
        unverified_claims=claims,
    )


def beat(agent: str) -> ScorerHeartbeat:
    return ScorerHeartbeat(agent_id=agent, scorer_version=SCORER_VERSION, last_run_at=NOW, last_status="ok")


async def goal(db, agent: str) -> Goal:
    row = Goal(
        id=uuid.uuid4(),
        agent_id=agent,
        title="Tidy the notes",
        objective="Merge duplicate notes",
        done_when="No duplicates",
        preset="small",
        total_budget=100_000,
        day_budget=10_000,
        deadline_days=7,
        sessions_per_day=4,
        min_interval_seconds=600,
        jobs_per_day=2,
        proposed_by="owner",
    )
    await add(db, row)
    return row


def goal_session(
    agent: str, g: Goal, n: int, *, spent: int, steps: int, age_days: float = 1, open_: bool = False
) -> GoalSession:
    started = NOW - timedelta(days=age_days)
    outcome = "step_closed" if steps else "no_step"
    ended = {} if open_ else {"ended_at": started + timedelta(minutes=5), "outcome": outcome}
    return GoalSession(
        agent_id=agent,
        goal_id=g.id,
        n=n,
        trigger="timer",
        first_subtask_id=uuid.uuid4(),
        started_at=started,
        spent=spent,
        steps_closed=steps,
        **ended,
    )


def checks(unit: dict[str, Any]) -> dict[str, Any]:
    return unit["outside_check"]


def conserved(unit: dict[str, Any]) -> bool:
    c = checks(unit)
    return unit["done"] == c["confirmed"] + c["unconfirmed"] + c["taken_back"] + c["unscored"]


# ---------------------------------------------------------------------------------------------------------------------
# Units and number 1: tokens per finished unit
# ---------------------------------------------------------------------------------------------------------------------


async def test_tasks_and_workflow_steps_are_split_on_the_dag_node(db, agent):
    await add(db, task(agent), task(agent))
    await step(db, agent)
    cur = (await numbers(db, agent))["current"]
    assert (cur["tasks"]["done"], cur["workflow_steps"]["done"]) == (2, 1)


async def test_a_cancelled_task_is_left_out_and_a_timed_out_or_blocked_one_did_not_finish(db, agent):
    await add(
        db,
        task(agent),
        task(agent, status="failed", final="cancelled", tokens=(999, 999)),  # the scorer leaves it out
        task(agent, status="cancelled", final=None, tokens=(999, 999)),
        task(agent, status="failed", final="timed_out", tokens=(50, 5)),
        task(agent, status="completed", final="incomplete_blocked", tokens=(30, 3)),
        task(agent, status="running", final=None, tokens=(999, 999)),  # not ended
    )
    t = (await numbers(db, agent))["current"]["tasks"]
    assert (t["done"], t["not_done"]) == (1, 2)
    assert t["tokens_in"] == {"done": 100, "not_done": 80}
    assert t["tokens_out"] == {"done": 10, "not_done": 8}
    assert t["by_final_outcome"] == {"completed": 1, "timed_out": 1, "incomplete_blocked": 1}


async def test_a_row_from_before_the_hardened_executor_reads_by_its_status(db, agent):
    await add(db, task(agent, final=None), task(agent, status="failed", final=None))
    t = (await numbers(db, agent))["current"]["tasks"]
    assert (t["done"], t["not_done"]) == (1, 1)
    assert t["by_final_outcome"] == {"none": 2}


async def test_tokens_per_finished_carries_the_work_that_did_not_finish(db, agent):
    await add(
        db,
        task(agent, tokens=(1000, 100)),
        task(agent, tokens=(3000, 300)),
        task(agent, status="failed", final="timed_out", tokens=(500, 50)),
    )
    t = (await numbers(db, agent))["current"]["tasks"]
    assert t["tokens_per_finished"] == (1100 + 3300 + 550) / 2
    assert t["output_per_finished"] == (100 + 300 + 50) / 2
    assert t["done_tokens"] == {"mean": 2200.0, "p50": 2200.0}


async def test_with_nothing_finished_the_cost_is_null_never_zero(db, agent):
    await add(db, task(agent, status="failed", final="errored", tokens=(500, 50)))
    t = (await numbers(db, agent))["current"]["tasks"]
    assert (t["done"], t["not_done"]) == (0, 1)
    assert t["tokens_per_finished"] is None and t["output_per_finished"] is None
    assert t["done_tokens"] == {"mean": None, "p50": None}
    assert checks(t)["unconfirmed_share"] is None


# ---------------------------------------------------------------------------------------------------------------------
# Number 2: finished with no outside check
# ---------------------------------------------------------------------------------------------------------------------


async def test_the_four_buckets_and_the_share(db, agent):
    subs = [task(agent) for _ in range(7)]
    await add(db, beat(agent), *subs)
    await add(
        db,
        score(agent, subs[0], "done_verified", settled=True),
        score(agent, subs[1], "done_checked", settled=True),
        score(agent, subs[2], "done_self"),
        score(agent, subs[3], "done_self", settled=True),
        score(agent, subs[4], "failed"),  # a check said no
        score(agent, subs[5], "partial"),
        # subs[6] has no score row yet
    )
    data = await numbers(db, agent)
    t = data["current"]["tasks"]
    c = checks(t)
    assert data["scorer_ran"] is True
    assert (c["confirmed"], c["unconfirmed"], c["taken_back"], c["unscored"]) == (2, 2, 2, 1)
    assert c["unconfirmed_share"] == 0.5  # taken back and unscored are not in it
    assert c["provisional"] == 1  # of the four counted, one can still move
    assert conserved(t)


async def test_the_share_is_an_exact_fraction(db, agent):
    subs = [task(agent) for _ in range(3)]
    await add(db, beat(agent), *subs)
    await add(db, *(score(agent, s, o) for s, o in zip(subs, ["done_self", "done_self", "done_verified"])))
    assert checks((await numbers(db, agent))["current"]["tasks"])["unconfirmed_share"] == round(2 / 3, 4)


async def test_with_no_score_rows_the_share_is_null_and_a_missing_heartbeat_says_the_scorer_never_ran(db, agent):
    await add(db, task(agent), task(agent))
    data = await numbers(db, agent)
    c = checks(data["current"]["tasks"])
    assert data["scorer_ran"] is False
    assert (c["unscored"], c["unconfirmed_share"]) == (2, None)


async def test_only_this_scorer_versions_leaf_rows_count(db, agent):
    subs = [task(agent) for _ in range(3)]
    await add(db, beat(agent), *subs)
    await add(
        db,
        score(agent, subs[0], "done_verified", version=SCORER_VERSION - 1),
        score(agent, subs[1], "done_self", kind="dag"),
        score(agent, subs[2], "done_self"),
    )
    c = checks((await numbers(db, agent))["current"]["tasks"])
    assert (c["confirmed"], c["unconfirmed"], c["unscored"]) == (0, 1, 2)


async def test_a_not_scored_row_on_a_finished_unit_counts_as_unscored(db, agent):
    sub = task(agent)
    await add(db, beat(agent), sub)
    await add(db, score(agent, sub, "not_scored"))
    t = (await numbers(db, agent))["current"]["tasks"]
    assert checks(t)["unscored"] == 1 and conserved(t)


async def test_flagged_counts_units_with_an_unverified_claim_and_is_null_when_claims_are_not_recorded(db, agent):
    subs = [task(agent) for _ in range(3)]
    await add(db, beat(agent), *subs)
    await add(
        db,
        score(agent, subs[0], "done_self", claims=2),
        score(agent, subs[1], "failed", claims=1),
        score(agent, subs[2], "done_verified"),
    )
    assert checks((await numbers(db, agent))["current"]["tasks"])["flagged"] == 2
    off = await numbers(db, agent, claims_recorded=False)
    assert checks(off["current"]["tasks"])["flagged"] is None


async def test_workflow_steps_have_their_own_buckets(db, agent):
    a = await step(db, agent)
    b = await step(db, agent)
    await add(db, beat(agent), task(agent))
    await add(db, score(agent, a, "done_checked"), score(agent, b, "done_self"))
    data = (await numbers(db, agent))["current"]
    w = checks(data["workflow_steps"])
    assert (w["confirmed"], w["unconfirmed"], w["unconfirmed_share"]) == (1, 1, 0.5)
    assert checks(data["tasks"])["unscored"] == 1


# ---------------------------------------------------------------------------------------------------------------------
# Goal sessions
# ---------------------------------------------------------------------------------------------------------------------


async def test_goal_sessions_cost_per_closed_step_and_have_no_outside_check(db, agent):
    g = await goal(db, agent)
    other = await goal(db, agent)
    await add(
        db,
        goal_session(agent, g, 1, spent=9000, steps=2),
        goal_session(agent, g, 2, spent=3000, steps=0),
        goal_session(agent, g, 3, spent=7000, steps=1, open_=True),  # still running: not ended
        goal_session(agent, other, 1, spent=6000, steps=1),
        goal_session(agent, other, 2, spent=5000, steps=1, age_days=30),  # outside the window
    )
    s = (await numbers(db, agent))["current"]["goal_sessions"]
    assert s["sessions"] == 3 and s["sessions_without_step"] == 1
    assert (s["spent"], s["steps_closed"], s["spent_without_step"]) == (18000, 3, 3000)
    assert s["tokens_per_finished"] == 6000.0
    assert s["outside_check"] is None


async def test_goal_sessions_with_no_closed_step_cost_null(db, agent):
    g = await goal(db, agent)
    await add(db, goal_session(agent, g, 1, spent=4000, steps=0))
    s = (await numbers(db, agent))["current"]["goal_sessions"]
    assert (s["sessions"], s["tokens_per_finished"]) == (1, None)


# ---------------------------------------------------------------------------------------------------------------------
# Scope, windows and shape
# ---------------------------------------------------------------------------------------------------------------------


async def test_another_agents_rows_are_ignored(db, agent):
    other = f"{agent}-other"
    g = await goal(db, other)
    s = task(other)
    await add(db, s, beat(other), goal_session(other, g, 1, spent=100, steps=1))
    await add(db, score(other, s, "done_verified"))
    data = await numbers(db, agent)
    cur = data["current"]
    assert data["scorer_ran"] is False
    assert (cur["tasks"]["done"], cur["workflow_steps"]["done"], cur["goal_sessions"]["sessions"]) == (0, 0, 0)


async def test_the_previous_window_is_the_same_length_just_before(db, agent):
    g = await goal(db, agent)
    await add(
        db,
        task(agent, age_days=1, tokens=(10, 1)),
        task(agent, age_days=8, tokens=(20, 2)),
        task(agent, age_days=9, tokens=(40, 4)),
        task(agent, age_days=15, tokens=(80, 8)),  # in neither
        goal_session(agent, g, 1, spent=500, steps=1, age_days=10),
    )
    data = await numbers(db, agent)
    assert data["window_days"] == 7
    assert data["current"]["tasks"]["done"] == 1 and data["current"]["tasks"]["tokens_per_finished"] == 11
    prev = data["previous"]
    assert prev["tasks"]["done"] == 2 and prev["tasks"]["tokens_per_finished"] == (22 + 44) / 2
    assert prev["goal_sessions"]["sessions"] == 1
    assert prev["to"] == data["current"]["from"] == (NOW - timedelta(days=7)).isoformat()
    assert prev["from"] == (NOW - timedelta(days=14)).isoformat()


async def test_a_capped_read_says_it_was_truncated(db, agent, monkeypatch):
    await add(db, *(task(agent) for _ in range(3)))
    assert (await numbers(db, agent))["current"]["truncated"] is False
    monkeypatch.setattr(ln, "MAX_ROWS", 2)
    cur = (await numbers(db, agent))["current"]
    assert cur["truncated"] is True and cur["tasks"]["done"] == 2


async def test_the_response_holds_numbers_and_labels_only(db, agent):
    g = await goal(db, agent)
    sub = task(agent)
    await add(db, beat(agent), sub, goal_session(agent, g, 1, spent=100, steps=1))
    await add(db, score(agent, sub, "done_self", claims=1))
    data = await numbers(db, agent)
    assert set(data) == {
        "enabled",
        "window_days",
        "counts_cached_input",
        "scorer_ran",
        "claims_recorded",
        "notes",
        "current",
        "previous",
    }
    assert data["enabled"] is True and data["counts_cached_input"] is False
    assert set(data["current"]) == {"from", "to", "truncated", "tasks", "workflow_steps", "goal_sessions"}
    iso = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+\+00:00$")
    label = re.compile(r"^[a-z][a-z0-9_]*$")

    def strings(node: Any) -> list[str]:
        if isinstance(node, dict):
            return [*node, *(s for v in node.values() for s in strings(v))]
        if isinstance(node, list):
            return [s for v in node for s in strings(v)]
        return [node] if isinstance(node, str) else []

    found = strings(json.loads(json.dumps(data)))
    assert found and all(iso.match(s) or label.match(s) for s in found), [s for s in found if not label.match(s)]
    assert "a task" not in json.dumps(data)
