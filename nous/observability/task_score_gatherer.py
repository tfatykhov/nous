"""Task scoring v1: the signal gatherer (autonomy plan v2, A1).

Read-only. For one agent and a window it collects a ``TaskSignals`` per finished subtask (the scored
leaf), DAG run and intention root (the roll-ups), in a number of queries that depends on the size of
the window in chunks of ``CHUNK`` ids, never on a per-task loop. Typed ORM selects only, so SQLite
(tests) and Postgres (prod) agree. ``task_scoring`` does the arithmetic.

WHERE EACH PIECE OF EVIDENCE GOES. Exactly one unit gets it, the most specific one its evidence names,
so a failing subtask inside a DAG inside a chain is penalised once:

- a ledger row with a ``subtask_id``                       -> that subtask
- a ledger row with a ``dag_id`` and no ``subtask_id``      -> that DAG run (a DAG-level penalty)
- a ledger row with neither, in an ``intent-`` or ``proposal-`` session of a chain -> the chain
- a claim-check event in a ``subtask-`` session             -> that subtask (a continuation-enabled scheduled
  subtask runs in a shared ``schedule-<hex8>-<n>`` session instead, and its claim events are not attributed:
  this undercounts, it never overcounts)
- a claim-check event in an ``intent-`` session, an arrival note carrying the unverified marker,
  a proposal the owner rejected                             -> the chain
- a decision linked to a subtask or DAG by the DEPTH-0 intention row that spawned it -> that unit; a decision
  an arrival recorded, or the one behind a schedule-spawned root -> the chain

A DEPTH-0 row only: the continuation runner stamps the root's ``origin_decision_id`` on every child it spawns
(``continuation_runner.py`` lines 1207, 1549, 1870), so reading the children would hand one owner grade to every
unit of the chain. And a decision that several roots share (one turn that spawned several roots) goes to the
earliest of them by (created_at, id) only; a schedule container, which is not a scored unit, never takes part.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.cognitive.claim_verifier import UNVERIFIED_MARKER
from nous.dag.approval import as_utc
from nous.observability import task_scoring as ts
from nous.storage.models import (
    CompensationSnapshot,
    DAGNode,
    Decision,
    Event,
    ExecutionDAG,
    ExecutionLedgerEntry,
    Intention,
    IntentionArrival,
    IntentionProposal,
    Subtask,
)

CHUNK = 500  # ids per IN list: far under asyncpg's 32767 bind parameters
CLAIMS_EVENT = "f026_claim_verification"  # runner.py writes it per turn; data.verified is a bool
OWNER_GRADE_REVIEWER = "a2ui"  # brain.review(reviewer="a2ui") is the owner's tap in the companion
# The ledger has no index on subtask_id, dag_id or session_id, so every ledger query is bounded by created_at (which
# is indexed, with agent_id). A task's calls were made before it finished: a call older than this window before the
# scoring window is not counted. Ledger retention is 90 days, so the bound is the tighter limit.
LEDGER_LOOKBACK = timedelta(days=30)

Evidence = dict[str, dict[str, list[str]]]  # task_id -> {"ledger_errors": [ledger ids], ...}


@dataclass(frozen=True)
class Gathered:
    """What one pass found: the leaves to score, and the roll-ups that count them."""

    leaves: list[ts.TaskSignals]
    dags: list[ts.TaskSignals]
    chains: list[ts.TaskSignals]


class _LedgerRow(NamedTuple):
    id: UUID
    tool_name: str
    status: str
    result_summary: str | None
    subtask_id: UUID | None
    dag_id: UUID | None
    session_id: str | None
    reverted: bool


def _chunks(values: Sequence[Any], size: int = CHUNK) -> Iterator[Sequence[Any]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _new_evidence() -> Evidence:
    return defaultdict(lambda: defaultdict(list))


def _plain(evidence: dict[str, list[str]] | None) -> dict[str, list[str]]:
    """A plain dict without the empty lists the defaultdict created by being read."""
    return {key: list(ids) for key, ids in (evidence or {}).items() if ids}


class _Pass:
    """The queries several units need, each run once per pass: the subtasks of a DAG, and whether it is checked."""

    def __init__(self, session: AsyncSession, agent_id: str) -> None:
        self._session = session
        self._agent_id = agent_id
        self._subtasks: dict[UUID, list[UUID]] = {}
        self._checked: dict[UUID, bool] = {}

    async def dag_subtasks(self, dag_ids: Sequence[UUID]) -> dict[UUID, list[UUID]]:
        """dag id -> the subtasks that run a node of it."""
        missing = [d for d in dict.fromkeys(dag_ids) if d not in self._subtasks]
        for dag_id in missing:
            self._subtasks[dag_id] = []
        for chunk in _chunks(missing):
            result = await self._session.execute(
                select(Subtask.id, DAGNode.dag_id)
                .join(DAGNode, Subtask.dag_node_id == DAGNode.id)
                .where(Subtask.agent_id == self._agent_id, DAGNode.dag_id.in_(chunk))
            )
            for r in result:
                self._subtasks[r.dag_id].append(r.id)
        return {d: self._subtasks[d] for d in dag_ids}

    async def checked(self, dag_ids: Sequence[UUID]) -> set[UUID]:
        """The DAGs that completed AND have a check node that completed AND carries a check (a non-empty
        ``completion_check``): the one outside confirmation, besides the owner's grade, that v1 accepts for "done".
        A check node without a ``completion_check`` completes when the heartbeat's model disables it, which is the
        model's own say-so."""
        missing = [d for d in dict.fromkeys(dag_ids) if d not in self._checked]
        found: set[UUID] = set()
        for chunk in _chunks(missing):
            result = await self._session.execute(
                select(DAGNode.dag_id)
                .join(ExecutionDAG, ExecutionDAG.id == DAGNode.dag_id)
                .where(
                    DAGNode.dag_id.in_(chunk),
                    DAGNode.node_type == "check",
                    DAGNode.status == "completed",
                    func.length(func.trim(DAGNode.completion_check)) > 0,
                    ExecutionDAG.agent_id == self._agent_id,
                    ExecutionDAG.status == "completed",
                )
            )
            found.update(r.dag_id for r in result)
        for dag_id in missing:
            self._checked[dag_id] = dag_id in found
        return {d for d in dag_ids if self._checked[d]}


async def _ledger_rows(
    session: AsyncSession,
    agent_id: str,
    *,
    since: datetime,
    subtask_ids: Sequence[UUID] = (),
    dag_ids: Sequence[UUID] = (),
    session_ids: Sequence[str] = (),
) -> list[_LedgerRow]:
    """The ledger rows created at or after ``since - LEDGER_LOOKBACK`` that belong to any of the given subtasks,
    DAGs or sessions, each once, with whether an owner undo (``compensation_snapshots.reverted_at``) reached it.
    Read-only calls write no ledger row (a bash call only when the whole command classified as a write)."""
    L, C = ExecutionLedgerEntry, CompensationSnapshot
    found: dict[UUID, _LedgerRow] = {}
    for column, values in (
        (L.subtask_id, list(subtask_ids)),
        (L.dag_id, list(dag_ids)),
        (L.session_id, list(session_ids)),
    ):
        for chunk in _chunks(values):
            result = await session.execute(
                select(
                    L.id, L.tool_name, L.status, L.result_summary, L.subtask_id, L.dag_id, L.session_id, C.reverted_at
                )
                .select_from(L)
                .outerjoin(C, C.ledger_entry_id == L.id)
                .where(L.agent_id == agent_id, L.created_at >= since - LEDGER_LOOKBACK, column.in_(chunk))
            )
            for r in result:
                prior = found.get(r.id)
                reverted = r.reverted_at is not None or (prior is not None and prior.reverted)
                found[r.id] = _LedgerRow(
                    r.id, r.tool_name, r.status, r.result_summary, r.subtask_id, r.dag_id, r.session_id, reverted
                )
    return list(found.values())


def _fold_ledger(rows: list[_LedgerRow], task_of: Callable[[_LedgerRow], str | None], evidence: Evidence) -> None:
    """Add each ledger row's error, bash exit, refusal and undo to the unit ``task_of`` names (None: not any)."""
    for row in rows:
        task = task_of(row)
        if task is None:
            continue
        kind = ts.classify_ledger_row(row.tool_name, row.status, row.result_summary)
        if kind == "ledger_error":
            evidence[task]["ledger_errors"].append(str(row.id))
        elif kind == "bash_exit":
            evidence[task]["bash_exits"].append(str(row.id))
        elif kind == "refusal":
            evidence[task]["refusals"].append(str(row.id))
        if row.reverted:
            evidence[task]["undone"].append(str(row.id))


async def _unverified_claim_events(
    session: AsyncSession, agent_id: str, session_ids: Sequence[str]
) -> list[tuple[str, str]]:
    """(session_id, event id) of every claim-check event in these sessions whose verdict was ``verified: false``."""
    found: list[tuple[str, str]] = []
    for chunk in _chunks(list(session_ids)):
        result = await session.execute(
            select(Event.id, Event.session_id, Event.data).where(
                Event.agent_id == agent_id, Event.event_type == CLAIMS_EVENT, Event.session_id.in_(chunk)
            )
        )
        for ev in result:
            data = ev.data if isinstance(ev.data, dict) else {}
            if data.get("verified") is False:
                found.append((ev.session_id, str(ev.id)))
    return found


async def _owner_grades(session: AsyncSession, agent_id: str, decision_ids: Sequence[UUID]) -> dict[UUID, str]:
    """decision id -> outcome, for the decisions the owner graded (reviewer ``a2ui``: success, partial, failure)."""
    graded: dict[UUID, str] = {}
    for chunk in _chunks(list(dict.fromkeys(decision_ids))):
        result = await session.execute(
            select(Decision.id, Decision.outcome).where(
                Decision.agent_id == agent_id,
                Decision.id.in_(chunk),
                Decision.reviewer == OWNER_GRADE_REVIEWER,
                Decision.outcome.in_(ts.GRADE_ORDER),
            )
        )
        graded.update({r.id: r.outcome for r in result})
    return graded


def _apply_grades(decision_ids: Sequence[UUID], graded: dict[UUID, str], evidence: dict[str, list[str]]) -> str | None:
    """The unit's worst owner grade. Each decision the owner graded failure is an override (it includes a
    course correction); the ids go in the evidence."""
    mine = [d for d in dict.fromkeys(decision_ids) if d in graded]
    if mine:
        evidence["grade_decisions"] = [str(d) for d in mine]
    failures = [str(d) for d in mine if graded[d] == "failure"]
    if failures:
        evidence["failure_graded"] = failures
    return ts.worst_grade(graded[d] for d in mine)


def _age_key(row: Any) -> tuple[datetime, str]:
    created = as_utc(row.created_at) or datetime.max.replace(tzinfo=UTC)
    return created, str(row.id)


async def _origin_decisions(
    session: AsyncSession, agent_id: str, source_kind: str, source_ids: Sequence[str]
) -> dict[str, list[UUID]]:
    """source id -> the decision that spawned it, through the DEPTH-0 intention row written at spawn time.

    Children are not read (the runner copies the root's decision onto each), and a decision shared by several
    depth-0 rows goes to the earliest of them only, so an owner grade lands on one unit. Schedule containers are
    left out of both reads: a container carries its creating turn's decision but is not a scored unit, so if it
    could win the earliest-row rule the grade would land on no unit at all.
    """
    mine: dict[str, tuple[Any, UUID]] = {}
    for chunk in _chunks(list(source_ids)):
        result = await session.execute(
            select(Intention.id, Intention.created_at, Intention.source_id, Intention.origin_decision_id).where(
                Intention.agent_id == agent_id,
                Intention.depth == 0,
                Intention.wake_policy != "container",
                Intention.source_kind == source_kind,
                Intention.source_id.in_(chunk),
                Intention.origin_decision_id.is_not(None),
            )
        )
        for r in result:
            mine[r.source_id] = (r, r.origin_decision_id)
    earliest: dict[UUID, tuple[datetime, str]] = {}
    for chunk in _chunks(list({d for _, d in mine.values()})):
        result = await session.execute(
            select(Intention.id, Intention.created_at, Intention.origin_decision_id).where(
                Intention.agent_id == agent_id,
                Intention.depth == 0,
                Intention.wake_policy != "container",  # a schedule container is not a scored unit: it must not win
                Intention.origin_decision_id.in_(chunk),
            )
        )
        for r in result:
            earliest[r.origin_decision_id] = min(earliest.get(r.origin_decision_id, _age_key(r)), _age_key(r))
    out: dict[str, list[UUID]] = defaultdict(list)
    for source_id, (row, decision_id) in mine.items():
        if _age_key(row) == earliest[decision_id]:
            out[source_id].append(decision_id)
    return out


def _signals(
    kind: str,
    task_id: str,
    finished_at: datetime,
    status: str | None,
    detail: str | None,
    evidence: dict[str, list[str]],
    *,
    tokens: int | None,
    grade: str | None,
    dag_checked: bool = False,
    child_ids: Sequence[str] = (),
) -> ts.TaskSignals:
    return ts.TaskSignals(
        task_kind=kind,
        task_id=task_id,
        finished_at=as_utc(finished_at),
        status=status,
        detail=detail,
        dag_checked=dag_checked,
        owner_grade=grade,
        owner_overrides=sum(len(evidence.get(k, [])) for k in ("undone", "proposals_rejected", "failure_graded")),
        unverified_claims=len(evidence.get("unverified_claims", [])),
        harness_errors=sum(len(evidence.get(k, [])) for k in ("ledger_errors", "bash_exits")),
        refusals=len(evidence.get("refusals", [])),
        tokens=tokens,
        child_ids=tuple(child_ids),
        evidence=_plain(evidence),
    )


async def _leaf_signals(session: AsyncSession, agent_id: str, since: datetime, run: _Pass) -> list[ts.TaskSignals]:
    rows = (
        await session.execute(
            select(
                Subtask.id,
                Subtask.status,
                Subtask.final_outcome,
                Subtask.completed_at,
                Subtask.tokens_in,
                Subtask.tokens_out,
            ).where(
                Subtask.agent_id == agent_id,
                Subtask.status.in_(("completed", "failed", "cancelled")),
                Subtask.completed_at >= since,
            )
        )
    ).all()
    if not rows:
        return []
    ids = [r.id for r in rows]
    evidence = _new_evidence()
    _fold_ledger(
        await _ledger_rows(session, agent_id, since=since, subtask_ids=ids),
        lambda row: str(row.subtask_id) if row.subtask_id else None,
        evidence,
    )
    # The worker's session id (handlers/subtask_worker.py) is "subtask-" plus the first 8 hex digits of the id.
    session_task = {f"subtask-{r.id.hex[:8]}": str(r.id) for r in rows}
    for sess, event_id in await _unverified_claim_events(session, agent_id, list(session_task)):
        evidence[session_task[sess]]["unverified_claims"].append(event_id)
    dag_of: dict[UUID, UUID] = {}
    for chunk in _chunks(ids):
        result = await session.execute(
            select(Subtask.id, DAGNode.dag_id)
            .join(DAGNode, Subtask.dag_node_id == DAGNode.id)
            .where(Subtask.agent_id == agent_id, Subtask.id.in_(chunk))
        )
        dag_of.update({r.id: r.dag_id for r in result})
    checked = await run.checked(list(set(dag_of.values())))
    origin = await _origin_decisions(session, agent_id, "subtask", [str(i) for i in ids])
    graded = await _owner_grades(session, agent_id, [d for ds in origin.values() for d in ds])
    out = []
    for r in rows:
        task = str(r.id)
        grade = _apply_grades(origin.get(task, []), graded, evidence[task])
        out.append(
            _signals(
                "subtask",
                task,
                r.completed_at,
                r.status,
                r.final_outcome,
                evidence[task],
                tokens=(r.tokens_in or 0) + (r.tokens_out or 0),
                grade=grade,
                dag_checked=dag_of.get(r.id) in checked,
            )
        )
    return out


async def _dag_signals(session: AsyncSession, agent_id: str, since: datetime, run: _Pass) -> list[ts.TaskSignals]:
    rows = (
        await session.execute(
            select(ExecutionDAG.id, ExecutionDAG.status, ExecutionDAG.completed_at, ExecutionDAG.tokens_consumed).where(
                ExecutionDAG.agent_id == agent_id,
                ExecutionDAG.status.in_(("completed", "failed", "partial", "cancelled")),
                ExecutionDAG.completed_at >= since,
            )
        )
    ).all()
    if not rows:
        return []
    ids = [r.id for r in rows]
    evidence = _new_evidence()
    # Only the DAG-level calls: a call that names a subtask belongs to the subtask, which is scored on its own.
    _fold_ledger(
        await _ledger_rows(session, agent_id, since=since, dag_ids=ids),
        lambda row: str(row.dag_id) if row.dag_id and not row.subtask_id else None,
        evidence,
    )
    children = {str(d): [str(s) for s in subs] for d, subs in (await run.dag_subtasks(ids)).items()}
    checked = await run.checked(ids)
    origin = await _origin_decisions(session, agent_id, "dag", [str(i) for i in ids])
    graded = await _owner_grades(session, agent_id, [d for ds in origin.values() for d in ds])
    out = []
    for r in rows:
        task = str(r.id)
        grade = _apply_grades(origin.get(task, []), graded, evidence[task])
        out.append(
            _signals(
                "dag",
                task,
                r.completed_at,
                r.status,
                None,
                evidence[task],
                tokens=r.tokens_consumed,
                grade=grade,
                dag_checked=r.id in checked,
                child_ids=children.get(task, ()),
            )
        )
    return out


def _uuid_or_none(value: str) -> UUID | None:
    try:
        return UUID(value)
    except ValueError:
        return None


async def _chain_signals(session: AsyncSession, agent_id: str, since: datetime, run: _Pass) -> list[ts.TaskSignals]:
    roots = (
        await session.execute(
            select(Intention.id, Intention.close_reason, Intention.closed_at).where(
                Intention.agent_id == agent_id,
                Intention.id == Intention.root_id,
                Intention.wake_policy != "container",  # a schedule container is a parent of fires, not a task
                Intention.close_reason.is_not(None),
                Intention.closed_at >= since,
            )
        )
    ).all()
    if not roots:
        return []
    root_ids = [r.id for r in roots]
    evidence = _new_evidence()

    # The lineage: every intention under a root. Its subtasks are the chain's children; the decision behind a
    # subtask or DAG belongs to that unit, so only a schedule-spawned root's decision is the chain's.
    child_subtasks: dict[str, list[str]] = defaultdict(list)
    lineage_dags: dict[UUID, str] = {}
    schedule_roots: dict[str, str] = {}  # source id of a depth-0 schedule intention -> its root
    for chunk in _chunks(root_ids):
        for row in await session.execute(
            select(Intention.root_id, Intention.depth, Intention.source_kind, Intention.source_id).where(
                Intention.agent_id == agent_id, Intention.root_id.in_(chunk)
            )
        ):
            root = str(row.root_id)
            source = _uuid_or_none(row.source_id) if row.source_kind in ("subtask", "dag") else None
            if row.source_kind == "subtask" and source is not None:
                child_subtasks[root].append(str(source))
            elif row.source_kind == "dag" and source is not None:
                lineage_dags[source] = root
            elif row.source_kind == "schedule" and row.depth == 0:
                schedule_roots[row.source_id] = root
    decisions: dict[str, list[UUID]] = defaultdict(list)
    for source_id, found in (await _origin_decisions(session, agent_id, "schedule", list(schedule_roots))).items():
        decisions[schedule_roots[source_id]].extend(found)
    for dag_id, subs in (await run.dag_subtasks(list(lineage_dags))).items():
        child_subtasks[lineage_dags[dag_id]].extend(str(s) for s in subs)

    # Proposals: an owner rejection is an override. "proposal-<id>" is the ledger session of an approved one.
    session_root = {f"intent-{rid}": str(rid) for rid in root_ids}
    for chunk in _chunks(root_ids):
        for p in await session.execute(
            select(
                IntentionProposal.id, IntentionProposal.root_id, IntentionProposal.state, IntentionProposal.decided_by
            ).where(IntentionProposal.agent_id == agent_id, IntentionProposal.root_id.in_(chunk))
        ):
            session_root[f"proposal-{p.id}"] = str(p.root_id)
            if p.state == "rejected" and ts.is_owner_decider(p.decided_by):
                evidence[str(p.root_id)]["proposals_rejected"].append(str(p.id))

    # Arrivals: the claim checker marks a flagged note, and each arrival names the decision it recorded.
    for chunk in _chunks(root_ids):
        for a in await session.execute(
            select(
                IntentionArrival.root_id,
                IntentionArrival.id,
                IntentionArrival.note,
                IntentionArrival.decision_record_id,
            ).where(IntentionArrival.agent_id == agent_id, IntentionArrival.root_id.in_(chunk))
        ):
            if a.note and UNVERIFIED_MARKER in a.note:
                evidence[str(a.root_id)]["unverified_claims"].append(str(a.id))
            if a.decision_record_id is not None:
                decisions[str(a.root_id)].append(a.decision_record_id)

    # Only the chain's own sessions: a call that names a subtask or a DAG belongs to that unit.
    _fold_ledger(
        await _ledger_rows(session, agent_id, since=since, session_ids=list(session_root)),
        lambda row: None if row.subtask_id or row.dag_id else session_root.get(row.session_id or ""),
        evidence,
    )
    # Claim verdicts of the continuation turns, session "intent-<root>".
    for sess, event_id in await _unverified_claim_events(session, agent_id, [f"intent-{rid}" for rid in root_ids]):
        evidence[session_root[sess]]["unverified_claims"].append(event_id)

    graded = await _owner_grades(session, agent_id, [d for ds in decisions.values() for d in ds])

    out = []
    for r in roots:
        root = str(r.id)
        grade = _apply_grades(decisions.get(root, []), graded, evidence[root])
        out.append(
            _signals(
                "intention",
                root,
                r.closed_at,
                r.close_reason,
                None,
                evidence[root],
                tokens=None,  # a chain's spend is its subtasks' and DAGs': not summed here, not in the composite
                grade=grade,
                child_ids=tuple(dict.fromkeys(child_subtasks.get(root, ()))),
            )
        )
    return out


async def gather_signals(session: AsyncSession, agent_id: str, *, since: datetime) -> Gathered:
    """Signals for every subtask, DAG run and intention root of ``agent_id`` that finished at or after ``since``.

    Read-only. Includes tasks the scorer will not score (a cancel, a legacy close): the scorer writes them as
    ``not_scored`` so coverage can tell them from a task it missed.
    """
    run = _Pass(session, agent_id)
    return Gathered(
        leaves=await _leaf_signals(session, agent_id, since, run),
        dags=await _dag_signals(session, agent_id, since, run),
        chains=await _chain_signals(session, agent_id, since, run),
    )
