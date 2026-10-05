"""F098: terminal-subtask reconciler — sweeps that repair one-shot writes.

The subtask worker writes a finished result to the inbox exactly once, and a
failed write (a transient DB error, a worker cancelled at shutdown) is
swallowed. With ``NOUS_RESULT_INBOX_ENABLED`` the inbox is the ONLY place
``pre_turn`` looks, so such a result would be lost for good. The reconciler
runs on a maintenance loop and re-does that write.

It is built from passes: each pass owns its own query over terminal subtasks
and its own idempotent write, and runs isolated from the others, so a later
pass (the Phase C memory writer) is one more ``register`` call.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol

from sqlalchemy import exists, or_, select, update

from nous.heart.result_inbox import SOURCE_SUBTASK, ResultInboxStore, is_dag_node_subtask, subtask_envelope
from nous.heart.subtasks import INLINE_WORKER_ID
from nous.storage.database import Database
from nous.storage.models import ResultInbox, Subtask

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
                        .where(or_(Subtask.parent_channel.is_not(None), Subtask.parent_session_id.is_not(None)))
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
        for st in candidates:
            env = None if is_dag_node_subtask(st) else subtask_envelope(st, self._settings.result_inbox_body_max_chars)
            if env is None:
                settle.append(st.id)
                continue
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
            )
            if written:
                fixed += 1
                logger.info("F098: reconciler re-inserted the inbox row of subtask %s", st.id.hex[:8])
        if settle:
            async with self._db.session() as session:
                await session.execute(update(Subtask).where(Subtask.id.in_(settle)).values(delivered=True))
                await session.commit()
        return fixed


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


def build_reconciler(database: Database, store: ResultInboxStore, settings: Settings) -> TerminalSubtaskReconciler:
    """The reconciler with every pass its flags enable (Phase A: the inbox pass)."""
    reconciler = TerminalSubtaskReconciler()
    if settings.result_inbox_enabled:
        reconciler.register(InboxSubtaskPass(database, store, settings))
    return reconciler
