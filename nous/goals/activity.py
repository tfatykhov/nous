"""F114 goals: what a goal's session is doing now, for the Goals page (``GET /goals/{ref}/activity``).

Read from rows, in the caller's one transaction under a statement limit of ``STATEMENT_TIMEOUT``: the goal's open
session (else its newest), the intentions of its chain (its root, ``nous.goals.roots``), the subtasks and workflows
those intentions name with the status of each row, and the active steps of the current workflow. The answer holds
numbers, times, ids and words from closed sets. The only text the agent wrote in it is the name of the current task
(the purpose line it was spawned with) or workflow, and of a workflow step, each redacted and cut
(``ledger_store.redacted_head``).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from nous.api.job_marks import JOB_ID_RE
from nous.brain import intentions
from nous.cognitive.ledger_store import redacted_head
from nous.storage.models import DAGNode, ExecutionDAG, GoalSession, Intention, Subtask

# What a read may take, and how much of each name the page is sent.
STATEMENT_TIMEOUT = "5s"
NAME_CHARS = 120
NODE_NAME_CHARS = 80
# A workflow step in one of these states is under way (a wait step and a completion check wait in awaiting_check, an
# approval step in awaiting_input).
ACTIVE_NODE_STATES: tuple[str, ...] = ("running", "awaiting_check", "awaiting_input")


@dataclass(frozen=True)
class _Item:
    """A subtask or workflow the session's chain spawned, with the status of its own row."""

    kind: str  # "task" or "workflow"
    id: UUID
    status: str
    started_at: datetime | None
    created_at: datetime
    name: str | None  # None for the session's first task, whose intent only repeats the goal's title


def phase_of(
    *,
    session_open: bool,
    current_kind: str | None,
    nodes: Sequence[tuple[str, str]],
    states: Iterable[str],
) -> str:
    """The session's phase, from rows only: ``none`` for an ended session; ``task`` while the current item is a
    task; for a workflow, from its active steps (``nodes``, each ``(status, node_type)``): ``workflow`` while one
    runs, else ``approval`` while one waits for the owner, else ``waiting`` while a wait step waits, else
    ``workflow``; with no current item, from the chain's intention ``states``: ``deciding``, ``asking`` or
    ``between``."""
    if not session_open:
        return "none"
    if current_kind == "task":
        return "task"
    if current_kind == "workflow":
        if any(status == "running" for status, _kind in nodes):
            return "workflow"
        if any(status == "awaiting_input" for status, _kind in nodes):
            return "approval"
        if any(status == "awaiting_check" and kind == "wait" for status, kind in nodes):
            return "waiting"
        return "workflow"
    seen = set(states)
    if seen & {"deciding", "result_ready"}:
        return "deciding"
    if "awaiting_owner" in seen:
        return "asking"
    return "between"


def waiting_for(wait: Any) -> dict[str, Any]:
    """What a wait step waits for, as identifiers only: a coding job's id, a pull request's number, or the kind
    alone. Anything else, or a value of another shape, is ``other``."""
    if isinstance(wait, dict):
        kind = wait.get("kind")
        job_id = wait.get("job_id")
        number = wait.get("number")
        if kind == "job" and isinstance(job_id, str) and JOB_ID_RE.fullmatch(job_id) is not None:
            return {"kind": "job", "job_id": job_id}
        if kind == "pr" and isinstance(number, int) and not isinstance(number, bool) and number >= 1:
            return {"kind": "pr", "number": number}
        if kind == "file":
            return {"kind": "file"}
    return {"kind": "other"}


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _uuid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except ValueError:
        return None


def _started_last(started_at: datetime | None, created_at: datetime, ident: UUID) -> tuple:
    """A sort key whose largest is the item started last, an item never started counted before any started one."""
    return (started_at is not None, started_at or created_at, created_at, str(ident))


async def _goal_session(session: AsyncSession, agent_id: str, goal_id: UUID) -> GoalSession | None:
    """The goal's open session, else its newest by number."""
    mine = (GoalSession.agent_id == agent_id, GoalSession.goal_id == goal_id)
    found = await session.scalar(select(GoalSession).where(*mine, GoalSession.ended_at.is_(None)).limit(1))
    if found is not None:
        return found
    return await session.scalar(select(GoalSession).where(*mine).order_by(GoalSession.n.desc()).limit(1))


async def _root_of(session: AsyncSession, agent_id: str, first_subtask_id: UUID) -> UUID | None:
    """The root of a session whose row has no ``root_id`` yet: the intention of its first subtask."""
    return await session.scalar(
        select(Intention.id).where(
            Intention.agent_id == agent_id,
            Intention.source_kind == intentions.SOURCE_SUBTASK,
            Intention.source_id == str(first_subtask_id),
            Intention.root_id == Intention.id,
        )
    )


async def _items(session: AsyncSession, agent_id: str, root_id: UUID, lineage: Sequence[Any]) -> list[_Item]:
    """The subtasks and workflows the chain's intentions name, each with its own row's status."""
    tasks: dict[UUID, Any] = {}
    dags: dict[UUID, Any] = {}
    for row in lineage:
        ident = _uuid(row.source_id)
        if ident is None:
            continue
        if row.source_kind == intentions.SOURCE_SUBTASK:
            tasks[ident] = row
        elif row.source_kind == intentions.SOURCE_DAG:
            dags[ident] = row
    items: list[_Item] = []
    if tasks:
        found = await session.execute(
            select(Subtask.id, Subtask.status, Subtask.started_at, Subtask.created_at).where(
                Subtask.agent_id == agent_id, Subtask.id.in_(list(tasks))
            )
        )
        for ident, status, started_at, created_at in found.all():
            intention = tasks[ident]
            name = None if intention.id == root_id else intention.intent
            items.append(_Item("task", ident, status, started_at, created_at, name))
    if dags:
        D = ExecutionDAG
        found = await session.execute(
            select(D.id, D.status, D.started_at, D.created_at, D.name).where(
                D.agent_id == agent_id, D.id.in_(list(dags))
            )
        )
        for ident, status, started_at, created_at, name in found.all():
            items.append(_Item("workflow", ident, status, started_at, created_at, name))
    return items


def _current_item(items: Sequence[_Item]) -> _Item | None:
    """The running item started last; with none running, the pending item created first; else None."""
    running = [i for i in items if i.status == "running"]
    if running:
        return max(running, key=lambda i: _started_last(i.started_at, i.created_at, i.id))
    pending = [i for i in items if i.status == "pending"]
    if pending:
        return min(pending, key=lambda i: (i.created_at, str(i.id)))
    return None


async def _active_nodes(session: AsyncSession, dag_id: UUID) -> list[Any]:
    """The workflow's steps under way, the one started last first."""
    found = await session.execute(
        select(DAGNode.name, DAGNode.node_type, DAGNode.status, DAGNode.started_at, DAGNode.wait_for)
        .where(DAGNode.dag_id == dag_id, DAGNode.status.in_(ACTIVE_NODE_STATES))
        .order_by(DAGNode.started_at.desc().nulls_last(), DAGNode.name)
    )
    return list(found.all())


def _node_words(node: Any) -> dict[str, Any]:
    return {"name": redacted_head(node.name, NODE_NAME_CHARS), "type": node.node_type, "status": node.status}


def _current(item: _Item, nodes: Sequence[Any], phase: str) -> dict[str, Any]:
    node = None
    wait = None
    if nodes:
        shown = nodes[0]
        if phase == "waiting":
            shown = next(n for n in nodes if n.node_type == "wait" and n.status == "awaiting_check")
            wait = waiting_for(shown.wait_for)
        node = _node_words(shown)
    return {
        "kind": item.kind,
        "name": redacted_head(item.name, NAME_CHARS) if item.name is not None else None,
        "first_task": item.kind == "task" and item.name is None,
        "status": item.status,
        "started_at": _iso(item.started_at),
        "node": node,
        "waiting_for": wait,
    }


async def read_activity(session: AsyncSession, agent_id: str, goal_id: UUID, *, log_enabled: bool) -> dict[str, Any]:
    """What the goal's session is doing now, for ``GET /goals/{ref}/activity`` (every key always present). Runs
    every statement in ``session``'s one transaction under ``STATEMENT_TIMEOUT``; a statement past it raises."""
    await session.execute(text(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'"))
    row = await _goal_session(session, agent_id, goal_id)
    if row is None:
        return _answer(log_enabled, None, "none", None, 0)
    is_open = row.ended_at is None
    root_id = row.root_id or await _root_of(session, agent_id, row.first_subtask_id)
    lineage: list[Any] = []
    if root_id is not None:
        found = await session.execute(
            select(Intention.id, Intention.source_kind, Intention.source_id, Intention.intent, Intention.state).where(
                Intention.agent_id == agent_id, Intention.root_id == root_id
            )
        )
        lineage = list(found.all())
    current = None
    running = 0
    phase = "none"
    if is_open:
        items = await _items(session, agent_id, root_id, lineage) if root_id is not None else []
        running = sum(1 for i in items if i.status == "running")
        item = _current_item(items)
        nodes: list[Any] = []
        if item is not None and item.kind == "workflow" and item.status == "running":
            nodes = await _active_nodes(session, item.id)
        phase = phase_of(
            session_open=True,
            current_kind=item.kind if item else None,
            nodes=[(n.status, n.node_type) for n in nodes],
            states=[r.state for r in lineage],
        )
        current = _current(item, nodes, phase) if item else None
    return _answer(log_enabled, row, phase, current, running)


def _answer(
    log_enabled: bool, row: GoalSession | None, phase: str, current: dict[str, Any] | None, running: int
) -> dict[str, Any]:
    return {
        "log_enabled": log_enabled,
        "session": None
        if row is None
        else {
            "n": row.n,
            "open": row.ended_at is None,
            "trigger": row.trigger,
            "started_at": _iso(row.started_at),
            "ended_at": _iso(row.ended_at),
        },
        "phase": phase,
        "current": current,
        "running_count": running,
        "last_call_at": None,
        "calls": None,
        "jobs": None,
        "recent": [],
        "work_sessions": [],
    }
