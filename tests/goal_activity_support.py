"""Row builders of the goal activity tests (``nous.goals.activity``): a goal's sessions, the work of their
chains and the tool-call log, written directly as the driver, the continuation, the workers and the runner's
recorder leave them. Times are fixed instants around ``T0``: the read compares stored times only.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from sqlalchemy import update

from nous.config import Settings
from nous.goals import activity
from nous.goals.store import GoalStore
from nous.storage.models import DAGNode, ExecutionDAG, GoalSession, Intention, Subtask, ToolCallLog

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

    async def call(
        self,
        when: datetime,
        *,
        session_id: str | None = None,
        subtask_id: uuid.UUID | None = None,
        dag_id: uuid.UUID | None = None,
        tool: str = "bash",
        outcome: str = "ran",
        is_error: bool = False,
        work_kind: str = "intention",
        agent: str | None = None,
        **columns: Any,
    ) -> None:
        """One row of the tool-call log, as the runner's recorder writes it when the call ends."""
        await self._add(
            ToolCallLog(
                agent_id=agent or self.agent,
                session_id=session_id,
                subtask_id=subtask_id,
                dag_id=dag_id,
                call_index=0,
                work_kind=work_kind,
                tool_name=tool,
                tool_name_called=tool,
                outcome=outcome,
                is_error=is_error,
                started_at=when,
                **columns,
            )
        )

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


RUNNER = "/workspace/claude-jobs/runner.sh"


def bash_input(command: str) -> str:
    """The tool-call log's text of a bash call: its input as JSON."""
    return json.dumps({"command": command})


def intent_sid(root_id: uuid.UUID) -> str:
    return f"intent-{root_id}"
