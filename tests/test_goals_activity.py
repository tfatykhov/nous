"""F114 goals: what a goal's session is doing now (``nous.goals.activity``), read from rows.

Every row is written directly, each test on an agent of its own. Times are fixed instants around ``T0``: the read
compares stored times only, so nothing here depends on the clock of the machine that runs it.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import update

from nous.config import Settings
from nous.goals import activity
from nous.goals.store import GoalStore
from nous.storage.models import DAGNode, ExecutionDAG, GoalSession, Intention, Subtask

pytestmark = pytest.mark.postgres_only

T0 = datetime(2026, 11, 22, 10, 0, tzinfo=UTC)
# The sample the redaction tests use (tests/test_tool_call_recorder.py).
SECRET = "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUv"
JOB = "job-20261122-102031-ab12cd34"


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


class World:
    """One agent's goal and the rows of its sessions, written as the driver, the continuation and the workers would
    leave them."""

    def __init__(self, db: Any, agent: str, goal_id: uuid.UUID) -> None:
        self.db = db
        self.agent = agent
        self.goal_id = goal_id

    async def _add(self, *rows: Any) -> None:
        async with self.db.session() as s:
            for row in rows:
                s.add(row)
                await s.flush()
            await s.commit()

    def _intention(
        self, root_id: uuid.UUID, kind: str, source_id: uuid.UUID, intent: str, state: str, own_id: uuid.UUID
    ) -> Intention:
        is_root = own_id == root_id
        return Intention(
            id=own_id,
            agent_id=self.agent,
            root_id=root_id,
            parent_id=None if is_root else root_id,
            depth=0 if is_root else 1,
            source_kind=kind,
            source_id=str(source_id),
            intent=intent,
            origin_kind="goal" if is_root else "continuation",
            wake_policy="continue",
            authority="internal_only",
            state=state,
        )

    async def session(
        self,
        *,
        n: int = 1,
        started: datetime = T0,
        ended: datetime | None = None,
        first_status: str = "running",
        root_state: str = "pending",
        with_root: bool = True,
        root_on_row: bool = True,
    ) -> SimpleNamespace:
        """A session, its first subtask and (``with_root``) its root intention; ``root_on_row`` False leaves the
        session's ``root_id`` unwritten, as in the window before the start writes it."""
        first, root = uuid.uuid4(), uuid.uuid4()
        rows: list[Any] = [
            Subtask(
                id=first,
                agent_id=self.agent,
                task="first task",
                status=first_status,
                created_at=started,
                started_at=started if first_status != "pending" else None,
            )
        ]
        if with_root:
            rows.append(
                self._intention(root, "subtask", first, f"Goal G-0001, session {n}: Tidy the notes", root_state, root)
            )
        rows.append(
            GoalSession(
                agent_id=self.agent,
                goal_id=self.goal_id,
                n=n,
                trigger="timer",
                first_subtask_id=first,
                root_id=root if (with_root and root_on_row) else None,
                started_at=started,
                **({"ended_at": ended, "outcome": "no_step"} if ended is not None else {}),
            )
        )
        await self._add(*rows)
        return SimpleNamespace(root_id=root, first_subtask_id=first, n=n)

    async def task(
        self,
        root_id: uuid.UUID,
        *,
        intent: str = "Fix the failing export test",
        status: str = "running",
        started: datetime | None = None,
        created: datetime = T0,
        state: str = "pending",
    ) -> uuid.UUID:
        """A subtask the session's chain spawned, with its intention."""
        sid = uuid.uuid4()
        await self._add(
            Subtask(id=sid, agent_id=self.agent, task="a task", status=status, created_at=created, started_at=started),
            self._intention(root_id, "subtask", sid, intent, state, uuid.uuid4()),
        )
        return sid

    async def workflow(
        self,
        root_id: uuid.UUID,
        *,
        name: str = "Build and check the export",
        status: str = "running",
        started: datetime | None = None,
        created: datetime = T0,
        nodes: tuple[tuple[str, str, str, datetime | None, Any], ...] = (),
        state: str = "pending",
    ) -> uuid.UUID:
        """A workflow the chain spawned, with its intention and ``nodes`` (name, type, status, started, wait_for)."""
        dag = ExecutionDAG(agent_id=self.agent, name=name, status=status, started_at=started, created_at=created)
        await self._add(dag)
        await self._add(
            self._intention(root_id, "dag", dag.id, "Run the workflow", state, uuid.uuid4()),
            *(
                DAGNode(dag_id=dag.id, name=node, node_type=kind, status=st, started_at=when, wait_for=wait)
                for node, kind, st, when, wait in nodes
            ),
        )
        return dag.id

    async def set_root(self, root_id: uuid.UUID, **values: Any) -> None:
        async with self.db.session() as s:
            await s.execute(update(Intention).where(Intention.id == root_id).values(**values))
            await s.commit()

    async def read(self, *, log: bool = True) -> dict[str, Any]:
        async with self.db.session() as s:
            return await activity.read_activity(s, self.agent, self.goal_id, log_enabled=log)


async def world_of(db: Any) -> World:
    agent = f"goals-activity-{uuid.uuid4().hex[:8]}"
    store = GoalStore(db, agent, Settings(_env_file=None, agent_id=agent, telegram_chat_id=""))
    goal = await store.create(
        title="Tidy the notes", objective="Tidy", done_when="Tidy", proposed_by="owner", origin_channel="telegram:1"
    )
    return World(db, agent, goal.id)


# ---- the session, the current work and the phase ------------------------------------------------------------------


async def test_a_goal_with_no_session_reads_none(db):
    w = await world_of(db)
    found = await w.read()
    assert found["session"] is None and found["phase"] == "none" and found["current"] is None
    assert found["running_count"] == 0
    assert (found["last_call_at"], found["calls"], found["jobs"]) == (None, None, None)
    assert (found["recent"], found["work_sessions"]) == ([], [])


async def test_a_running_task_of_the_chain_is_named_by_its_intent_redacted_and_cut(db):
    w = await world_of(db)
    s = await w.session(first_status="completed")
    await w.task(s.root_id, intent=f"Fix the export {SECRET} " + "and check it " * 30, started=at(5))
    found = await w.read()
    assert found["phase"] == "task" and found["running_count"] == 1
    current = found["current"]
    assert (current["kind"], current["status"], current["first_task"]) == ("task", "running", False)
    assert current["started_at"] == at(5).isoformat()
    assert current["name"].startswith("Fix the export ") and SECRET not in current["name"]
    assert "REDACTED" in current["name"]
    assert len(current["name"]) <= activity.NAME_CHARS == 120
    assert (current["node"], current["waiting_for"]) == (None, None)


async def test_the_sessions_own_first_task_has_no_name(db):
    w = await world_of(db)
    await w.session()
    found = await w.read()
    assert found["phase"] == "task"
    assert (found["current"]["name"], found["current"]["first_task"]) == (None, True)


async def test_a_running_workflow_is_named_and_the_later_started_item_is_current(db):
    w = await world_of(db)
    s = await w.session(first_status="completed")
    await w.workflow(s.root_id, name="Build and check the export", started=at(3))
    found = await w.read()
    assert found["phase"] == "workflow" and found["running_count"] == 1
    assert (found["current"]["kind"], found["current"]["name"]) == ("workflow", "Build and check the export")
    await w.task(s.root_id, intent="Sort the photos", started=at(7))
    found = await w.read()
    assert found["running_count"] == 2
    assert (found["current"]["kind"], found["current"]["name"]) == ("task", "Sort the photos")


async def test_a_pending_task_alone_is_current_and_waiting_to_start(db):
    w = await world_of(db)
    s = await w.session(first_status="completed")
    await w.task(s.root_id, status="pending", intent="Sort the photos")
    found = await w.read()
    assert found["phase"] == "task" and found["running_count"] == 0
    assert (found["current"]["status"], found["current"]["started_at"]) == ("pending", None)


WAIT = ("wait_for_job", "wait", "awaiting_check")


@pytest.mark.parametrize(
    ("wait_for", "expected"),
    [
        ({"kind": "job", "job_id": JOB}, {"kind": "job", "job_id": JOB}),
        ({"kind": "job", "job_id": f"wait for {JOB} then email the notes"}, {"kind": "other"}),
        ({"kind": "pr", "number": 951, "repo": "owner/private-repo", "until": "merged"}, {"kind": "pr", "number": 951}),
        ({"kind": "pr", "number": True, "until": "merged"}, {"kind": "other"}),
        ({"kind": "pr", "number": 0, "until": "merged"}, {"kind": "other"}),
        ({"kind": "file", "path": "notes/the-private-plan.md"}, {"kind": "file"}),
        ({"kind": "mail", "from": "someone"}, {"kind": "other"}),
        (["job", JOB], {"kind": "other"}),
    ],
    ids=["job", "job-sentence", "pr", "pr-bool", "pr-zero", "file", "unknown-kind", "not-an-object"],
)
async def test_a_workflow_waiting_on_a_wait_step_says_what_for_in_identifiers_only(db, wait_for, expected):
    w = await world_of(db)
    s = await w.session(first_status="completed")
    await w.workflow(
        s.root_id,
        started=at(1),
        nodes=(("build", "subtask", "completed", at(1), None), (*WAIT, at(20), wait_for)),
    )
    found = await w.read()
    assert found["phase"] == "waiting"
    assert found["current"]["waiting_for"] == expected
    assert found["current"]["node"] == {"name": "wait_for_job", "type": "wait", "status": "awaiting_check"}
    said = json.dumps(found)
    for text in ("then email the notes", "owner/private-repo", "the-private-plan", "someone"):
        assert text not in said


async def test_a_workflow_with_a_running_step_besides_a_wait_is_running(db):
    w = await world_of(db)
    s = await w.session(first_status="completed")
    await w.workflow(
        s.root_id,
        started=at(1),
        nodes=((*WAIT, at(20), {"kind": "job", "job_id": JOB}), ("check", "subtask", "running", at(10), None)),
    )
    found = await w.read()
    assert found["phase"] == "workflow" and found["current"]["waiting_for"] is None
    assert found["current"]["node"] == {"name": "wait_for_job", "type": "wait", "status": "awaiting_check"}


async def test_a_workflow_step_waiting_for_the_owners_approval_reads_approval(db):
    w = await world_of(db)
    s = await w.session(first_status="completed")
    await w.workflow(s.root_id, started=at(1), nodes=(("ship", "approval", "awaiting_input", at(4), None),))
    found = await w.read()
    assert found["phase"] == "approval" and found["current"]["waiting_for"] is None
    assert found["current"]["node"] == {"name": "ship", "type": "approval", "status": "awaiting_input"}


@pytest.mark.parametrize(
    ("root_state", "phase"),
    [("deciding", "deciding"), ("result_ready", "deciding"), ("awaiting_owner", "asking"), ("pending", "between")],
)
async def test_with_nothing_running_the_chains_own_state_gives_the_phase(db, root_state, phase):
    w = await world_of(db)
    await w.session(first_status="completed", root_state=root_state)
    found = await w.read()
    assert (found["phase"], found["current"], found["running_count"]) == (phase, None, 0)


async def test_a_pending_intention_whose_subtask_finished_is_not_running(db):
    w = await world_of(db)
    s = await w.session(first_status="completed")
    await w.task(s.root_id, status="completed", started=at(2), state="pending")
    found = await w.read()
    assert (found["current"], found["running_count"], found["phase"]) == (None, 0, "between")


async def test_the_root_is_found_from_the_first_subtask_before_the_session_row_has_it(db):
    w = await world_of(db)
    await w.session(root_on_row=False)
    found = await w.read()
    assert found["phase"] == "task" and found["current"]["first_task"] is True


async def test_an_open_session_with_no_root_at_all_is_between_steps(db):
    w = await world_of(db)
    await w.session(with_root=False)
    found = await w.read()
    assert (found["phase"], found["current"], found["running_count"]) == ("between", None, 0)


async def test_an_ended_newest_session_reads_none_with_no_current_work(db):
    w = await world_of(db)
    s = await w.session(n=1, first_status="running", ended=at(30))
    await w.task(s.root_id, started=at(5))
    found = await w.read()
    assert found["session"] == {
        "n": 1,
        "open": False,
        "trigger": "timer",
        "started_at": T0.isoformat(),
        "ended_at": at(30).isoformat(),
    }
    assert (found["phase"], found["current"]) == ("none", None)
    assert found["jobs"] is None and found["recent"] == []


async def test_the_open_session_is_read_before_an_ended_one(db):
    w = await world_of(db)
    await w.session(n=1, ended=at(30), first_status="completed")
    await w.session(n=2, started=at(60))
    found = await w.read()
    assert (found["session"]["n"], found["session"]["open"], found["phase"]) == (2, True, "task")


@pytest.mark.parametrize(
    ("nodes", "phase"),
    [
        ([("running", "subtask")], "workflow"),
        ([("awaiting_input", "approval"), ("awaiting_check", "wait")], "approval"),
        ([("awaiting_check", "wait")], "waiting"),
        ([("awaiting_check", "subtask")], "workflow"),
        ([], "workflow"),
    ],
)
def test_phase_of_a_workflow_follows_its_active_steps(nodes, phase):
    assert activity.phase_of(session_open=True, current_kind="workflow", nodes=nodes, states=[]) == phase


def test_phase_of_an_ended_session_is_none_whatever_else_holds():
    assert activity.phase_of(session_open=False, current_kind="task", nodes=[], states=["deciding"]) == "none"
