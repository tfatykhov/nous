"""F098: terminal-subtask reconciler — sweeps that repair one-shot writes.

The subtask worker writes a finished result to the inbox exactly once, and a
failed write (a transient DB error, a worker cancelled at shutdown) is
swallowed. With ``NOUS_RESULT_INBOX_ENABLED`` the inbox is the ONLY place
``pre_turn`` looks, so such a result would be lost for good. The reconciler
runs on a maintenance loop and re-does that write. A finished DAG's write,
made by the F087 delivery path, is repaired the same way.

It is built from passes: each pass owns its own query over terminal rows
and its own idempotent write, and runs isolated from the others. Phase C
registers one more, :class:`~nous.heart.result_memory.ResultMemoryPass`,
which writes finished results to memory.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol

from sqlalchemy import exists, or_, select, update
from sqlalchemy.orm import selectinload

from nous.brain import continuation, intentions
from nous.heart.result_inbox import (
    SOURCE_DAG,
    SOURCE_SUBTASK,
    ResultInboxStore,
    close_intention_quietly,
    is_dag_node_subtask,
    record_dag_result,
    route_result,
    subtask_envelope,
)
from nous.heart.result_memory import ResultMemoryPass, ResultMemoryWriter
from nous.heart.subtasks import INLINE_WORKER_ID
from nous.storage.database import Database
from nous.storage.models import ExecutionDAG, ResultInbox, Subtask

if TYPE_CHECKING:  # pragma: no cover - typing only
    from nous.config import Settings

logger = logging.getLogger(__name__)

# Rows one pass may handle per tick.
RECONCILE_BATCH_SIZE = 50
# Seconds between two reconciler ticks.
RECONCILE_INTERVAL_SECONDS = 60
# Seconds one pass may run before it is abandoned for this tick.
_PASS_TIMEOUT_SECONDS = 30


class ReconcilerPass(Protocol):
    """One repair over terminal subtasks. ``run`` returns the rows it fixed."""

    name: str

    async def run(self, *, limit: int) -> int: ...


class InboxSubtaskPass:
    """Re-insert terminal subtasks whose inbox write never landed.

    Candidates mirror the worker hook's writer: completed/failed, not inline,
    not a DAG node, with a routing key, not yet delivered, finished inside
    the inbox age bound, and with no inbox row. Two more bounds:

    * Only subtasks that FINISHED at or after the inbox was first switched
      on (``heart.result_inbox_state``), so turning the flag on never
      backfills the historical backlog (F098 §4.6), while every result
      finished since is repaired — the agent's first write included, and a
      task that was already running when the flag came on.
    * A subtask with nothing to say gets no row, so it is marked delivered
      instead; otherwise it would come back every tick and, past
      ``RECONCILE_BATCH_SIZE`` of them, starve the rows that need repair.

    A repaired row is stamped ``created_at = completed_at``, so the age bound
    and the latency metric see when the result really finished.
    """

    name = "inbox"

    def __init__(self, database: Database, store: ResultInboxStore, settings: Settings) -> None:
        self._db = database
        self._store = store
        self._settings = settings

    async def run(self, *, limit: int) -> int:
        agent_id = self._settings.agent_id
        since = datetime.now(UTC) - timedelta(hours=self._settings.result_inbox_max_age_hours)
        # Recorded at startup; recorded here instead if that write failed.
        enabled_at = await self._store.ensure_enabled_at()
        async with self._db.session() as session:
            has_row = exists().where(ResultInbox.source_kind == SOURCE_SUBTASK, ResultInbox.source_id == Subtask.id)
            routable = or_(Subtask.parent_channel.is_not(None), Subtask.parent_session_id.is_not(None))
            if continuation.enabled(self._settings):
                routable = or_(routable, continuation.has_continue_intention(agent_id, SOURCE_SUBTASK, Subtask.id))
            candidates = (
                (
                    await session.execute(
                        select(Subtask)
                        .where(Subtask.agent_id == agent_id)
                        .where(Subtask.status.in_(("completed", "failed")))
                        .where(Subtask.completed_at.is_not(None), Subtask.completed_at > since)
                        .where(Subtask.completed_at >= enabled_at)
                        .where(or_(Subtask.worker_id.is_(None), Subtask.worker_id != INLINE_WORKER_ID))
                        .where(Subtask.dag_node_id.is_(None))
                        .where(Subtask.delivered.is_(False))
                        .where(routable)
                        .where(~has_row)
                        .order_by(Subtask.completed_at)
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )

        fixed = 0
        settle = []
        continuation_on = continuation.enabled(self._settings)
        for st in candidates:
            env = None if is_dag_node_subtask(st) else subtask_envelope(st, self._settings.result_inbox_body_max_chars)
            if continuation_on and not is_dag_node_subtask(st):
                # F099 Phase 2: the same routing as the worker hook. A continue result is written
                # by its intention (an empty one too); a non-continue one with nothing to say settles.
                written = await route_result(
                    self._store,
                    self._settings,
                    source_kind=SOURCE_SUBTASK,
                    source_id=st.id,
                    generation=0,
                    env=env,
                    channel=st.parent_channel,
                    session_id=st.parent_session_id,
                    correlation_id=str(st.id),
                    created_at=st.completed_at,
                    empty_title=st.task or "subtask",
                )
                if written:
                    fixed += 1
                    logger.info("F098: reconciler re-inserted the inbox row of subtask %s", st.id.hex[:8])
                elif env is None:
                    settle.append(st.id)
                continue
            if env is None:
                settle.append(st.id)
                continue
            intention_id = await close_intention_quietly(self._store, self._settings, SOURCE_SUBTASK, st.id)
            written = await self._store.insert(
                source_kind=SOURCE_SUBTASK,
                source_id=st.id,
                msg_type=env.msg_type,
                title=env.title,
                body=env.body,
                channel=st.parent_channel,
                session_id=st.parent_session_id,
                correlation_id=str(st.id),
                created_at=st.completed_at,
                intention_id=intention_id,
            )
            if written:
                fixed += 1
                logger.info("F098: reconciler re-inserted the inbox row of subtask %s", st.id.hex[:8])
        if settle:
            async with self._db.session() as session:
                await session.execute(update(Subtask).where(Subtask.id.in_(settle)).values(delivered=True))
                await session.commit()
        return fixed


class InboxDagPass:
    """Re-insert terminal DAGs whose inbox write never landed.

    F087's delivery writes the DAG's row on every attempt, but that write is
    best-effort: when it fails on the attempt whose Telegram push succeeds,
    the DAG is marked delivered with no row. Making the row a required leg
    would not help, because a retry re-runs every leg and re-sends the push.

    Candidates: terminal and already delivered by F087 (until then its own
    retries write the row, with the summary they announce), finished at or
    after the inbox was first switched on and inside the age bound, routable
    (an origin, or scheduled routing on), and with no row for their current
    ``delivery_generation``. The body is the summary F087 cached, else its
    template; the row is stamped ``created_at = completed_at``.
    """

    name = "dag"

    def __init__(self, database: Database, store: ResultInboxStore, settings: Settings) -> None:
        self._db = database
        self._store = store
        self._settings = settings

    async def run(self, *, limit: int) -> int:
        # Late imports: nous.dag.delivery imports nous.heart.result_inbox.
        from nous.dag.delivery import DAGResultDelivery
        from nous.dag.store import TERMINAL_DAG_STATUSES

        settings = self._settings
        since = datetime.now(UTC) - timedelta(hours=settings.result_inbox_max_age_hours)
        enabled_at = await self._store.ensure_enabled_at()
        has_row = exists().where(
            ResultInbox.source_kind == SOURCE_DAG,
            ResultInbox.source_id == ExecutionDAG.id,
            ResultInbox.source_generation == ExecutionDAG.delivery_generation,
        )
        query = (
            select(ExecutionDAG)
            .where(ExecutionDAG.agent_id == settings.agent_id)
            .where(ExecutionDAG.status.in_(sorted(TERMINAL_DAG_STATUSES)))
            .where(ExecutionDAG.delivered_at.is_not(None))
            .where(ExecutionDAG.completed_at.is_not(None), ExecutionDAG.completed_at > since)
            .where(ExecutionDAG.completed_at >= enabled_at)
            .where(~has_row)
            .options(selectinload(ExecutionDAG.nodes))
            .order_by(ExecutionDAG.completed_at)
            .limit(limit)
        )
        if not (settings.result_inbox_dag_scheduled and settings.telegram_chat_id):
            routable = or_(ExecutionDAG.origin_channel.is_not(None), ExecutionDAG.origin_session_id.is_not(None))
            if continuation.enabled(settings):
                # F099 Phase 2: a DAG a continuation spawned has no origin, and its row is keyed by its
                # intention alone. Without this its lost row would never be repaired. A closed
                # intention counts: a retried DAG (new generation) reopens it.
                routable = or_(
                    routable,
                    continuation.has_continue_intention(
                        settings.agent_id, SOURCE_DAG, ExecutionDAG.id, include_closed=True
                    ),
                )
            query = query.where(routable)
        async with self._db.session() as session:
            candidates = (await session.execute(query)).scalars().all()

        template = DAGResultDelivery(settings, agent_id=settings.agent_id)
        fixed = 0
        for dag in candidates:
            written = await record_dag_result(
                self._store,
                settings,
                dag_id=dag.id,
                name=dag.name,
                status=dag.status,
                summary=dag.delivery_summary or template.build_template(dag),
                blocked=DAGResultDelivery._is_blocked(dag),
                origin_channel=dag.origin_channel,
                origin_session_id=dag.origin_session_id,
                generation=dag.delivery_generation,
                created_at=dag.completed_at,
            )
            if written:
                fixed += 1
                logger.info("F098: reconciler re-inserted the inbox row of DAG %s", dag.id.hex[:8])
        return fixed


class IntentionClosePass:
    """F099 Phase 1: close intentions whose work is over, whatever their routing key.

    Subtasks and DAGs: the PRIMARY closer of cancelled subtasks (the writers
    skip 'cancelled', and a subtask cancelled while pending never reaches a
    writer), and otherwise the backstop. The inbox writers close an intention
    before their routing-key check, but each runs once, and a worker
    cancelled at shutdown, a failed hook, or a second cancel during an inline
    close leaves it pending.

    Containers: the repair for ScheduleManager._close_container, which runs
    after the deactivation commits and may fail or never run. A container
    whose schedule is inactive or gone is closed here; containers have no
    TTL, so nothing else would.

    Each kind at most ``limit`` per tick, oldest first. With the continuation
    flag on it leaves ``continue`` and ``report`` intentions to their writers
    (F099 Phase 2).
    """

    name = "intentions"

    def __init__(self, database: Database, settings: Settings) -> None:
        self._db = database
        self._settings = settings

    async def run(self, *, limit: int) -> int:
        agent_id = self._settings.agent_id
        on = continuation.enabled(self._settings)
        async with self._db.session() as session:
            closed = await intentions.close_finished_sources(
                session,
                agent_id,
                limit=limit,
                reason=continuation.close_reason_for(self._settings),
                # Phase 2: a continue result is the writer's (the row and the move to result_ready are one
                # transaction) and a report closes with its insert; closing either here would strand a
                # result with no row. A lost row is the inbox passes' to repair (they select the work of a
                # continue intention); what they cannot select (a cancelled subtask) is PR-2c's
                # repair_missing_results.
                exclude_policies=(intentions.WAKE_CONTINUE, intentions.WAKE_REPORT) if on else (),
            )
            containers = await intentions.close_finished_containers(session, agent_id, limit=limit)
            await session.commit()
        if closed or containers:
            logger.info(
                "F099: reconciler closed %d intention(s) of finished work and %d container(s) of stopped schedules",
                len(closed),
                len(containers),
            )
        return len(closed) + len(containers)


class TerminalSubtaskReconciler:
    """Runs the registered passes, each isolated and bounded."""

    def __init__(self, passes: list[ReconcilerPass] | None = None, batch_size: int = RECONCILE_BATCH_SIZE) -> None:
        self._passes: list[ReconcilerPass] = list(passes or [])
        self._batch_size = batch_size

    def register(self, p: ReconcilerPass) -> None:
        self._passes.append(p)

    async def run_once(self) -> dict[str, int]:
        """One tick: every pass, in order. A pass that fails or times out is
        logged and skipped; the others still run."""
        results: dict[str, int] = {}
        for p in self._passes:
            try:
                results[p.name] = await asyncio.wait_for(p.run(limit=self._batch_size), _PASS_TIMEOUT_SECONDS)
            except Exception:
                logger.warning("F098: reconciler pass %s failed", p.name, exc_info=True)
        return results


def build_reconciler(
    database: Database,
    store: ResultInboxStore,
    settings: Settings,
    memory: ResultMemoryWriter | None = None,
) -> TerminalSubtaskReconciler:
    """The reconciler with every pass its flags enable (F098 Phase A: the
    inbox passes; F098 Phase C: the memory pass; F099: the intentions pass)."""
    reconciler = TerminalSubtaskReconciler()
    if settings.result_inbox_enabled:
        reconciler.register(InboxSubtaskPass(database, store, settings))
        reconciler.register(InboxDagPass(database, store, settings))
        if intentions.enabled(settings):
            reconciler.register(IntentionClosePass(database, settings))
    if settings.result_memory_enabled and memory is not None:
        reconciler.register(ResultMemoryPass(memory, settings))
    return reconciler
