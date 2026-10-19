"""Task scoring v2: the signal gatherer.

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
- how a DAG node's own completion_check ended (``node_check_<state>``) -> the subtask the node records as its
  current attempt; an earlier attempt of that node gets the state of its ``attempt_history`` entry, or
  ``node_check_superseded``
- the owner's audit of a subtask (``nous_system.check_audits``, v5: ``audit_right``, ``audit_wrong``, or
  ``audit_pending`` for a card that is out and unanswered) -> that subtask

A DEPTH-0 row only: the continuation runner stamps the root's ``origin_decision_id`` on every child it spawns
(a continuation turn and an approved proposal's call both pass it on as the spawn's ``decision_id``), so reading the
children would hand one owner grade to every unit of the chain. And a decision that several roots share (one turn
that spawned several roots) goes to the earliest of them by (created_at, id) only; a schedule container, which is
not a scored unit, never takes part.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.audits import AUDIT_VERDICT_ACTION
from nous.cognitive.claim_verifier import UNVERIFIED_MARKER
from nous.dag.approval import as_utc
from nous.observability import task_scoring as ts
from nous.storage.models import (
    A2uiAction,
    CheckAudit,
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
# v5: the agent's audit rows are read in one query, newest sampled first and at most this many. The weekly card
# holds a handful, so this is years of audits: the window's leaves and every kind's latest answers are inside it.
AUDIT_ROWS_CAP = 2000

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


def chunked(values: Sequence[Any], size: int = CHUNK) -> Iterator[Sequence[Any]]:
    """``values`` in consecutive slices of at most ``size`` (the ids of one IN list each)."""
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
        for chunk in chunked(missing):
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
        ``completion_check``): the one outside confirmation, besides the owner's grade, that the scorer accepts for
        "done".
        A check node without a ``completion_check`` completes when the heartbeat's model disables it, which is the
        model's own say-so."""
        missing = [d for d in dict.fromkeys(dag_ids) if d not in self._checked]
        found: set[UUID] = set()
        for chunk in chunked(missing):
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
        for chunk in chunked(values):
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
    for chunk in chunked(list(session_ids)):
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
    for chunk in chunked(list(dict.fromkeys(decision_ids))):
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


class _Audit(NamedTuple):
    id: UUID
    surface_id: str | None
    verdict: str | None


async def _audits(session: AsyncSession, agent_id: str) -> tuple[dict[str, _Audit], set[str]]:
    """v5, one query: subtask id -> its audit row, and the check kinds the owner's answers trust
    (``task_scoring.trusted_kinds``). Only ``right`` and ``wrong`` are verdicts; a row is read as answered only with
    both a verdict and an answer time."""
    result = await session.execute(
        select(
            CheckAudit.id,
            CheckAudit.task_id,
            CheckAudit.check_kind,
            CheckAudit.surface_id,
            CheckAudit.verdict,
            CheckAudit.answered_at,
        )
        .where(CheckAudit.agent_id == agent_id)
        .order_by(CheckAudit.sampled_at.desc(), CheckAudit.id)
        .limit(AUDIT_ROWS_CAP)
    )
    by_task: dict[str, _Audit] = {}
    answered: list[tuple[str, str, datetime, str]] = []
    for r in result:
        # One rule for verification and trust alike: a verdict counts only with its answer time. The handler writes
        # both together, so a verdict without one was written some other way and says nothing.
        verdict = r.verdict if r.verdict in ts.AUDIT_VERDICTS and r.answered_at is not None else None
        by_task[str(r.task_id)] = _Audit(r.id, r.surface_id, verdict)
        if verdict is not None:
            answered.append((r.check_kind, verdict, as_utc(r.answered_at), str(r.id)))
    return by_task, ts.trusted_kinds(answered)


def _uuid_text(value: object) -> str | None:
    """A UUID in one canonical text form, whatever the database spelled it as (None for anything else)."""
    try:
        return str(UUID(str(value)))
    except ValueError:
        return None


async def audits_without_action_row(session: AsyncSession, agent_id: str) -> int:
    """v5 tripwire: the agent's answered audits that have no completed ``audit.verdict`` action recorded for that exact
    audit: same card (``surface_id``), same ``auditId`` in the action's context, same verdict. The action router writes
    that row for every verdict the owner taps, so a verdict without one was written some other way. It cannot tell a
    careful forgery, and it does not change a score: the job logs the count and the scoreboard shows it. Compared in
    Python: the text form of a UUID differs between SQLite and Postgres."""
    answered = (
        await session.execute(
            select(CheckAudit.id, CheckAudit.surface_id, CheckAudit.verdict)
            .where(CheckAudit.agent_id == agent_id, CheckAudit.verdict.is_not(None))
            .limit(AUDIT_ROWS_CAP)
        )
    ).all()
    taps: set[tuple[str, str | None, object]] = set()
    result = await session.execute(  # the agent's audit taps are few: one query, matched in Python below
        select(A2uiAction.surface_id, A2uiAction.context)
        .where(
            A2uiAction.agent_id == agent_id,
            A2uiAction.action_name == AUDIT_VERDICT_ACTION,
            A2uiAction.status == "completed",
        )
        .order_by(A2uiAction.created_at.desc())
        .limit(AUDIT_ROWS_CAP)
    )
    for tap in result:
        context = tap.context if isinstance(tap.context, dict) else {}
        taps.add((tap.surface_id, _uuid_text(context.get("auditId")), context.get("verdict")))
    return sum(1 for r in answered if (r.surface_id, _uuid_text(r.id), r.verdict) not in taps)


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
    for chunk in chunked(list(source_ids)):
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
    for chunk in chunked(list({d for _, d in mine.values()})):
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
    node_check: str | None = None,
    child_ids: Sequence[str] = (),
) -> ts.TaskSignals:
    return ts.TaskSignals(
        task_kind=kind,
        task_id=task_id,
        finished_at=as_utc(finished_at),
        status=status,
        detail=detail,
        dag_checked=dag_checked,
        node_check=node_check,
        owner_grade=grade,
        owner_overrides=sum(
            len(evidence.get(k, [])) for k in ("undone", "proposals_rejected", "failure_graded", "audit_wrong")
        ),
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
    node_check: dict[UUID, str] = {}
    pr_of: dict[UUID, tuple[ts.PrCheck, str, datetime | None, datetime | None]] = {}  # v4: (check, node, DAG, done)
    kind_of: dict[UUID, str] = {}  # v5: the kind of the node's own check, for the node's current attempt
    for chunk in chunked(ids):
        result = await session.execute(
            select(
                Subtask.id,
                DAGNode.dag_id,
                ExecutionDAG.created_at.label("dag_created_at"),
                DAGNode.id.label("node_id"),
                DAGNode.completed_at.label("node_completed_at"),
                DAGNode.subtask_id.label("node_subtask_id"),
                DAGNode.status.label("node_status"),
                DAGNode.error.label("node_error"),
                DAGNode.completion_check,
                DAGNode.check_attempts,
                DAGNode.last_check_at,
                DAGNode.attempt_history,
            )
            .join(DAGNode, Subtask.dag_node_id == DAGNode.id)
            .join(ExecutionDAG, ExecutionDAG.id == DAGNode.dag_id)
            .where(Subtask.agent_id == agent_id, Subtask.id.in_(chunk))
        )
        for r in result:
            dag_of[r.id] = r.dag_id
            # How the node's check reads for this subtask, by the rule shared with the sampler (task_scoring).
            attempt = ts.attempt_check_state(
                r.id,
                node_subtask_id=r.node_subtask_id,
                completion_check=r.completion_check,
                check_attempts=r.check_attempts,
                last_check_at=r.last_check_at,
                node_status=r.node_status,
                node_error=r.node_error,
                attempt_history=r.attempt_history,
            )
            state = attempt.state
            if attempt.current and (r.completion_check or "").strip():
                kind_of[r.id] = ts.check_kind(r.completion_check)
            if state is not None:
                node_check[r.id] = state
                evidence[str(r.id)][f"node_check_{state}"].append(str(r.node_id))
            # v4: a passed PR check of the current attempt goes to github_verify, which decides the credit.
            if state == "passed" and (pr := ts.parse_pr_check(r.completion_check)) is not None:
                pr_of[r.id] = (
                    pr,
                    str(r.node_id),
                    as_utc(r.dag_created_at) if r.dag_created_at else None,
                    as_utc(r.node_completed_at) if r.node_completed_at else None,
                )
    checked = await run.checked(list(set(dag_of.values())))
    origin = await _origin_decisions(session, agent_id, "subtask", [str(i) for i in ids])
    graded = await _owner_grades(session, agent_id, [d for ds in origin.values() for d in ds])
    audit_of, trusted = await _audits(session, agent_id)
    out = []
    for r in rows:
        task = str(r.id)
        grade = _apply_grades(origin.get(task, []), graded, evidence[task])
        dag_checked = dag_of.get(r.id) in checked
        if dag_checked:  # v4: a Nous-written check node, so evidence only
            evidence[task]["dag_check_passed"].append(str(dag_of[r.id]))
        # v5: the owner's verdict on this subtask's audit. "wrong" is counted in owner_overrides through the evidence;
        # a row whose card never went out (surface_id NULL) says nothing.
        audit = audit_of.get(task)
        if audit is not None and audit.verdict is not None:
            evidence[task][f"audit_{audit.verdict}"].append(str(audit.id))
        elif audit is not None and audit.surface_id is not None:
            evidence[task]["audit_pending"].append(str(audit.id))
        pr = pr_of.get(r.id)
        signals = _signals(
            "subtask",
            task,
            r.completed_at,
            r.status,
            r.final_outcome,
            evidence[task],
            tokens=(r.tokens_in or 0) + (r.tokens_out or 0),
            grade=grade,
            dag_checked=dag_checked,
            node_check=node_check.get(r.id),
        )
        if pr is not None:
            signals = replace(signals, pr_check=pr[0], node_id=pr[1], dag_created_at=pr[2], node_completed_at=pr[3])
        kind = kind_of.get(r.id)
        signals = replace(
            signals,
            audit_verdict=audit.verdict if audit is not None else None,
            audit_pending=audit is not None and audit.verdict is None and audit.surface_id is not None,
            check_kind=kind,
            kind_trusted=kind is not None and kind in trusted,
        )
        out.append(signals)
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
        if r.id in checked:  # v4: evidence only, as on its leaves
            evidence[task]["dag_check_passed"].append(task)
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
    for chunk in chunked(root_ids):
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
    for chunk in chunked(root_ids):
        for p in await session.execute(
            select(
                IntentionProposal.id, IntentionProposal.root_id, IntentionProposal.state, IntentionProposal.decided_by
            ).where(IntentionProposal.agent_id == agent_id, IntentionProposal.root_id.in_(chunk))
        ):
            session_root[f"proposal-{p.id}"] = str(p.root_id)
            if p.state == "rejected" and ts.is_owner_decider(p.decided_by):
                evidence[str(p.root_id)]["proposals_rejected"].append(str(p.id))

    # Arrivals: the claim checker marks a flagged note, and each arrival names the decision it recorded.
    for chunk in chunked(root_ids):
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
