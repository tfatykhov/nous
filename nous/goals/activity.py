"""F114 goals: what a goal's session is doing now, for the Goals page (``GET /goals/{ref}/activity``).

Read from rows, in the caller's one transaction under a statement limit of ``STATEMENT_TIMEOUT``: the goal's open
session (else its newest), the intentions of its chain (its root, ``nous.goals.roots``), the subtasks and workflows
those intentions name with the status of each row, the kind and state of the current workflow's active steps (never
their names) and, with the tool-call log on, the chain's calls since the session began: a call of the root's
decision turns (their session id), of a subtask of the chain or of a workflow of the chain, as
``spend.jobs_launched_today`` selects them. A call row is written when the call ends, so an ended session's calls are
read up to ``ENDED_GRACE`` after its end, and a call's time in the answer is when it ended (its start plus its
duration).

The answer holds numbers, times, ids and words from closed sets. The only text the agent wrote in it is the name of
the current task (the purpose line it was spawned with) or workflow, redacted and cut (``ledger_store.redacted_head``).
A tool's name is sent only in the shape of a tool name (``TOOL_NAME_RE``; the log keeps what the model called a tool it
made up). A shell call's input is read to count its job launches (``spend.job_launches``) and is never put in the
answer, nor is any call's result, reason or error text.

What a read costs: the calls are found through the log's ``(agent_id, started_at)`` index over the agent's calls
since the session began (there is no index on a call's subtask or workflow), at most ``CALLS_CAP`` rows of narrow
columns, and at most ``LAUNCH_ROWS_CAP`` shell calls with their input (each at most ``NOUS_TOOL_INPUT_MAX_CHARS``).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from nous.api.job_marks import JOB_ID_RE
from nous.brain import continuation, intentions
from nous.cognitive.ledger_store import redacted_head
from nous.goals.spend import RAN_OUTCOMES, job_launches
from nous.storage.models import DAGNode, ExecutionDAG, GoalSession, Intention, Subtask, ToolCallLog

# What a read may take, and how much of each name the page is sent.
STATEMENT_TIMEOUT = "5s"
NAME_CHARS = 120
# A tool's name as the answer may carry it; any other stored name is sent as ``other``.
TOOL_NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
# The most call rows read (the count then says "more than"), shell calls read for launches (the count then says "at
# least"), recent calls and Work screen sessions sent.
CALLS_CAP = 2000
LAUNCH_ROWS_CAP = 200
RECENT_SHOWN = 8
WORK_SESSIONS_SHOWN = 5
ENDED_GRACE = timedelta(minutes=5)
# A workflow step in one of these states is under way (a wait step and a completion check wait in awaiting_check, an
# approval step in awaiting_input).
ACTIVE_NODE_STATES: tuple[str, ...] = ("running", "awaiting_check", "awaiting_input")
# The tool-call log's outcomes (migration 100); any other stored value is sent as ``other``.
OUTCOMES = frozenset({"ran", "error", "refused", "gated", "suppressed", "retired", "bad_input"})
# The kind of work a call was made in, in the page's words; any other kind is ``other``.
WORK_KIND_WORDS: dict[str, str] = {"intention": "decisions", "subtask": "task", "workflow_step": "workflow"}


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
    """The kind, state and wait of the workflow's steps under way (never their names), the one started last first."""
    found = await session.execute(
        select(DAGNode.node_type, DAGNode.status, DAGNode.wait_for)
        .where(DAGNode.dag_id == dag_id, DAGNode.status.in_(ACTIVE_NODE_STATES))
        .order_by(DAGNode.started_at.desc().nulls_last(), DAGNode.id)
    )
    return list(found.all())


def _current(item: _Item, nodes: Sequence[Any], phase: str) -> dict[str, Any]:
    wait = None
    if phase == "waiting":
        step = next(n for n in nodes if n.node_type == "wait" and n.status == "awaiting_check")
        wait = waiting_for(step.wait_for)
    return {
        "kind": item.kind,
        "name": redacted_head(item.name, NAME_CHARS) if item.name is not None else None,
        "first_task": item.kind == "task" and item.name is None,
        "status": item.status,
        "started_at": _iso(item.started_at),
        "waiting_for": wait,
    }


def _chain_calls(root_id: UUID | None, lineage: Sequence[Any]) -> list[ColumnElement[bool]]:
    """The calls of the chain: of the root's decision turns, of its subtasks, of its workflows (a branch whose list is
    empty is left out)."""
    found: list[ColumnElement[bool]] = []
    if root_id is not None:
        found.append(ToolCallLog.session_id == f"{continuation.INTENT_SESSION_PREFIX}{root_id}")
    branches = ((intentions.SOURCE_SUBTASK, ToolCallLog.subtask_id), (intentions.SOURCE_DAG, ToolCallLog.dag_id))
    for kind, column in branches:
        ids = [ident for r in lineage if r.source_kind == kind and (ident := _uuid(r.source_id)) is not None]
        if ids:
            found.append(column.in_(ids))
    return found


def _window(agent_id: str, row: GoalSession, chain: Sequence[ColumnElement[bool]]) -> list[ColumnElement[bool]]:
    where = [ToolCallLog.agent_id == agent_id, ToolCallLog.started_at >= row.started_at, or_(*chain)]
    if row.ended_at is not None:
        where.append(ToolCallLog.started_at <= row.ended_at + ENDED_GRACE)
    return where


def _ended(call: Any) -> datetime:
    """When the call ended: its start plus its duration (its start when no duration was logged)."""
    return call.started_at + timedelta(milliseconds=max(call.duration_ms or 0, 0))


def _call_words(call: Any) -> dict[str, Any]:
    return {
        "tool": call.tool_name if TOOL_NAME_RE.fullmatch(call.tool_name) else "other",
        "at": _iso(_ended(call)),
        "outcome": call.outcome if call.outcome in OUTCOMES else "other",
        "in": WORK_KIND_WORDS.get(call.work_kind, "other"),
    }


def _work_sessions(calls: Sequence[Any]) -> list[dict[str, str]]:
    """The distinct session ids of ``calls`` (newest first), each with the kind of its newest call."""
    found: dict[str, str] = {}
    for call in calls:
        if call.session_id and call.session_id not in found:
            found[call.session_id] = WORK_KIND_WORDS.get(call.work_kind, "other")
            if len(found) == WORK_SESSIONS_SHOWN:
                break
    return [{"id": sid, "kind": kind} for sid, kind in found.items()]


async def _launches(
    session: AsyncSession, agent_id: str, row: GoalSession, chain: Sequence[ColumnElement[bool]]
) -> dict[str, Any]:
    """Job launches of the chain's shell calls that ran (``spend.job_launches``, which reads every shell call as
    ``spend.jobs_launched_today`` does), when the newest one was, and whether more shell calls ran than were read."""
    cap = LAUNCH_ROWS_CAP
    launched = 0
    last: datetime | None = None
    rows: list[Any] = []
    if chain:
        found = await session.execute(
            select(ToolCallLog.input_text, ToolCallLog.started_at)
            .where(
                *_window(agent_id, row, chain), ToolCallLog.tool_name == "bash", ToolCallLog.outcome.in_(RAN_OUTCOMES)
            )
            .order_by(ToolCallLog.started_at.desc(), ToolCallLog.id.desc())
            .limit(cap + 1)
        )
        rows = list(found.all())
    for input_text, started_at in rows[:cap]:
        count = job_launches(input_text)
        launched += count
        if count and last is None:
            last = started_at
    return {"launched": launched, "last_launch_at": _iso(last), "capped": len(rows) > cap}


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
    answer = _answer(log_enabled, row, phase, current, running)
    if not log_enabled:
        return answer
    cap = CALLS_CAP
    chain = _chain_calls(root_id, lineage)
    calls: list[Any] = []
    if chain:
        C = ToolCallLog
        found = await session.execute(
            select(C.tool_name, C.outcome, C.is_error, C.duration_ms, C.started_at, C.session_id, C.work_kind)
            .where(*_window(agent_id, row, chain))
            .order_by(C.started_at.desc(), C.id.desc())
            .limit(cap + 1)
        )
        calls = list(found.all())
    kept = calls[:cap]
    # Newest first by when each call ended: a long command that ended last is the last call.
    by_end = sorted(kept, key=_ended, reverse=True)
    answer["last_call_at"] = _iso(_ended(by_end[0])) if by_end else None
    answer["calls"] = {"total": len(kept), "errors": sum(1 for c in kept if c.is_error), "capped": len(calls) > cap}
    if is_open:
        answer["jobs"] = await _launches(session, agent_id, row, chain)
        answer["recent"] = [_call_words(c) for c in by_end[:RECENT_SHOWN]]
    answer["work_sessions"] = _work_sessions(by_end)
    return answer


def _answer(
    log_enabled: bool, row: GoalSession | None, phase: str, current: dict[str, Any] | None, running: int
) -> dict[str, Any]:
    """The answer with the call parts as they are with the log off (the read fills them in with it on)."""
    return {
        "log_enabled": log_enabled,
        "session": None
        if row is None
        else {
            "n": row.n,
            "open": row.ended_at is None,
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
