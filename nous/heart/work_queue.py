"""F064.6: WorkQueueItemManager — CRUD on nous_system.work_queue_items.

The atomicity model (plan §9.3 + post-review revision):
- `claim_for_dispatch` runs `INSERT … ON CONFLICT DO NOTHING RETURNING …`
  so the bool contract is unambiguous: row returned ⇒ this caller won the
  race and is responsible for dispatching.
- Callers MUST run `dag_create` + `mark_dispatched` in the SAME async
  session as the claim's session, OR run the claim first and the
  create+dispatch in a fresh session that rolls back on failure. The
  reconciler `list_undispatched(source, older_than)` is the safety net.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from nous.storage.database import Database
from nous.storage.models import WorkQueueItem

logger = logging.getLogger(__name__)


class WorkQueueItemManager:
    """CRUD operations for F064.6 work_queue_items rows."""

    def __init__(self, database: Database, agent_id: str) -> None:
        self._db = database
        self._agent_id = agent_id

    async def claim_for_dispatch(
        self,
        source: str,
        external_id: str,
        payload: dict | None,
    ) -> WorkQueueItem | None:
        """Atomic insert: returns the row only when this caller actually
        inserted it (won the race). Returns None on conflict.

        Implementation uses `INSERT … ON CONFLICT (agent_id, source,
        external_id) DO NOTHING RETURNING id, source, external_id`. The
        RETURNING clause yields zero rows on conflict and one row on
        insert — so `result.first()` cleanly distinguishes the two cases
        without a follow-up SELECT.

        @codex P1 on aa3c739 (plan §9.3): the bool ambiguity in the v1
        spec wording was closed by switching to RETURNING semantics.
        """
        try:
            async with self._db.session() as session:
                stmt = (
                    pg_insert(WorkQueueItem)
                    .values(
                        agent_id=self._agent_id,
                        source=source,
                        external_id=external_id,
                        payload=payload,
                        created_at=datetime.now(UTC),
                        # dispatched_at left NULL: caller is now responsible
                        # for the dag_create + mark_dispatched commit.
                    )
                    .on_conflict_do_nothing(
                        index_elements=["agent_id", "source", "external_id"]
                    )
                    .returning(WorkQueueItem.id)
                )
                result = await session.execute(stmt)
                row = result.first()
                if row is None:
                    return None
                await session.commit()
                # Re-fetch the inserted row with all fields populated.
                got = await session.get(WorkQueueItem, row[0])
                return got
        except Exception:
            logger.exception(
                "F064.6: claim_for_dispatch failed for %s/%s",
                source, external_id,
            )
            return None

    async def mark_dispatched(
        self, item_id: UUID, dag_id: UUID
    ) -> None:
        """Link a claimed work item to the DAG that was dispatched for it.

        Plan §9.3: caller MUST run this in the same logical transaction as
        the dag_create. If it raises, the reconciler will eventually catch
        the partial-commit orphan via list_undispatched.
        """
        async with self._db.session() as session:
            await session.execute(
                update(WorkQueueItem)
                .where(WorkQueueItem.id == item_id)
                .where(WorkQueueItem.agent_id == self._agent_id)
                .values(dispatched_at=datetime.now(UTC), dag_id=dag_id)
            )
            await session.commit()

    async def mark_terminal(self, item_id: UUID, state: str) -> None:
        """Record that the source marked this item terminal. Plan §9.3:
        callers do this AFTER cancel_dag returns successfully, so a
        cancel_dag failure leaves terminal_state NULL and the next tick
        retries the cancel."""
        async with self._db.session() as session:
            await session.execute(
                update(WorkQueueItem)
                .where(WorkQueueItem.id == item_id)
                .where(WorkQueueItem.agent_id == self._agent_id)
                .values(terminal_state=state)
            )
            await session.commit()

    async def get_by_external(
        self, source: str, external_id: str
    ) -> WorkQueueItem | None:
        async with self._db.session() as session:
            result = await session.execute(
                select(WorkQueueItem)
                .where(WorkQueueItem.agent_id == self._agent_id)
                .where(WorkQueueItem.source == source)
                .where(WorkQueueItem.external_id == external_id)
            )
            return result.scalar_one_or_none()

    async def list_undispatched(
        self, source: str, older_than: timedelta
    ) -> list[WorkQueueItem]:
        """Reconciler query: rows that have been claimed (row exists) but
        never had mark_dispatched run on them, AND have aged past the
        partial-commit grace window. Plan §9.3."""
        cutoff = datetime.now(UTC) - older_than
        async with self._db.session() as session:
            result = await session.execute(
                select(WorkQueueItem)
                .where(WorkQueueItem.agent_id == self._agent_id)
                .where(WorkQueueItem.source == source)
                .where(WorkQueueItem.dispatched_at.is_(None))
                .where(WorkQueueItem.created_at < cutoff)
            )
            return list(result.scalars().all())
