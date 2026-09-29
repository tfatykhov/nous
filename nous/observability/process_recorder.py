"""ProcessRecorder — lightweight per-phase run logger for fault detection.

Writes one row to ``nous_system.process_run_log`` per memory-process execution.
All writes are fire-and-forget and FAIL OPEN: any DB error is logged at WARNING
and the owning process continues unaffected.

Usage (typical — wrap a sleep phase)::

    recorder = ProcessRecorder(db, agent_id)
    run_id = await recorder.start("sleep/stale_scan")
    try:
        # ... do the work ...
        await recorder.finish(run_id, items_examined=100, items_changed=3)
    except Exception as exc:
        await recorder.error(run_id, str(exc))
        raise

Or via the async context manager::

    async with recorder.phase("sleep/stale_scan") as ctx:
        # ... do the work ...
        ctx.items_examined = 100
        ctx.items_changed = 3
    # finish is called automatically; exception → error row.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy import text

from nous.storage.database import Database

logger = logging.getLogger(__name__)

# Sentinel for "recorder is disabled" (fault_detector_enabled=False).
_NOOP_ID = -1


@dataclass
class PhaseContext:
    """Mutable bag the caller fills in while running a phase."""
    items_examined: int | None = None
    items_changed: int | None = None
    metadata: dict | None = None


class ProcessRecorder:
    """Records per-phase run rows to ``nous_system.process_run_log``."""

    def __init__(self, db: Database, agent_id: str) -> None:
        self._db = db
        self._agent_id = agent_id

    # ------------------------------------------------------------------
    # Explicit start / finish / error API
    # ------------------------------------------------------------------

    async def start(self, process_name: str) -> int:
        """Insert a 'started' row; return its id (or _NOOP_ID on error)."""
        try:
            async with self._db.session() as session:
                result = await session.execute(
                    text(
                        "INSERT INTO nous_system.process_run_log "
                        "(agent_id, process_name, status) "
                        "VALUES (:agent_id, :process_name, 'started') "
                        "RETURNING id"
                    ),
                    {"agent_id": self._agent_id, "process_name": process_name},
                )
                row_id: int = result.scalar_one()
                await session.commit()
                return row_id
        except Exception:
            logger.warning("ProcessRecorder.start failed for %s", process_name, exc_info=True)
            return _NOOP_ID

    async def finish(
        self,
        run_id: int,
        items_examined: int | None = None,
        items_changed: int | None = None,
        metadata: dict | None = None,
    ) -> None:
        """Close a run row as 'finished'."""
        if run_id == _NOOP_ID:
            return
        try:
            async with self._db.session() as session:
                await session.execute(
                    text(
                        "UPDATE nous_system.process_run_log "
                        "SET status = 'finished', finished_at = now(), "
                        "    items_examined = :examined, "
                        "    items_changed = :changed, "
                        "    metadata = :meta "
                        "WHERE id = :id"
                    ),
                    {
                        "id": run_id,
                        "examined": items_examined,
                        "changed": items_changed,
                        "meta": metadata,
                    },
                )
                await session.commit()
        except Exception:
            logger.warning("ProcessRecorder.finish failed for id=%s", run_id, exc_info=True)

    async def error(self, run_id: int, error_message: str) -> None:
        """Close a run row as 'error'."""
        if run_id == _NOOP_ID:
            return
        try:
            async with self._db.session() as session:
                await session.execute(
                    text(
                        "UPDATE nous_system.process_run_log "
                        "SET status = 'error', finished_at = now(), "
                        "    error_message = :msg "
                        "WHERE id = :id"
                    ),
                    {"id": run_id, "msg": error_message[:2000]},
                )
                await session.commit()
        except Exception:
            logger.warning("ProcessRecorder.error failed for id=%s", run_id, exc_info=True)

    async def skip(self, run_id: int) -> None:
        """Close a run row as 'skipped' (phase was interrupted before running)."""
        if run_id == _NOOP_ID:
            return
        try:
            async with self._db.session() as session:
                await session.execute(
                    text(
                        "UPDATE nous_system.process_run_log "
                        "SET status = 'skipped', finished_at = now() "
                        "WHERE id = :id"
                    ),
                    {"id": run_id},
                )
                await session.commit()
        except Exception:
            logger.warning("ProcessRecorder.skip failed for id=%s", run_id, exc_info=True)

    # ------------------------------------------------------------------
    # Context-manager API
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def phase(self, process_name: str) -> AsyncIterator[PhaseContext]:
        """Async context manager: records start/finish/error automatically.

        The caller may mutate the yielded ``PhaseContext`` to supply
        ``items_examined`` / ``items_changed`` before the block exits.
        """
        run_id = await self.start(process_name)
        ctx = PhaseContext()
        try:
            yield ctx
            await self.finish(
                run_id,
                items_examined=ctx.items_examined,
                items_changed=ctx.items_changed,
                metadata=ctx.metadata,
            )
        except Exception as exc:
            await self.error(run_id, str(exc))
            raise

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    async def prune_old_rows(self, retention_days: int) -> int:
        """Delete rows older than ``retention_days``. Returns count deleted."""
        if retention_days <= 0:
            return 0
        try:
            async with self._db.session() as session:
                result = await session.execute(
                    text(
                        "DELETE FROM nous_system.process_run_log "
                        "WHERE agent_id = :agent_id "
                        "  AND started_at < now() - make_interval(days => :days)"
                    ),
                    {"agent_id": self._agent_id, "days": retention_days},
                )
                await session.commit()
                return result.rowcount
        except Exception:
            logger.warning("ProcessRecorder.prune_old_rows failed", exc_info=True)
            return 0

    # ------------------------------------------------------------------
    # Query helpers (used by ProcessFaultCheck)
    # ------------------------------------------------------------------

    async def get_recent_runs(
        self,
        process_name: str,
        limit: int = 20,
    ) -> list[dict]:
        """Return last N rows for this process, newest-first.

        Each row: {id, status, started_at, finished_at,
                   items_examined, items_changed, error_message}.
        """
        try:
            async with self._db.session() as session:
                result = await session.execute(
                    text(
                        "SELECT id, status, started_at, finished_at, "
                        "       items_examined, items_changed, error_message "
                        "FROM nous_system.process_run_log "
                        "WHERE agent_id = :agent_id "
                        "  AND process_name = :process_name "
                        "ORDER BY started_at DESC "
                        "LIMIT :limit"
                    ),
                    {
                        "agent_id": self._agent_id,
                        "process_name": process_name,
                        "limit": limit,
                    },
                )
                rows = result.mappings().all()
                return [dict(r) for r in rows]
        except Exception:
            logger.warning(
                "ProcessRecorder.get_recent_runs failed for %s", process_name, exc_info=True
            )
            return []

    async def get_all_process_names(self) -> list[str]:
        """Return distinct process names seen for this agent (for fault checks)."""
        try:
            async with self._db.session() as session:
                result = await session.execute(
                    text(
                        "SELECT DISTINCT process_name "
                        "FROM nous_system.process_run_log "
                        "WHERE agent_id = :agent_id"
                    ),
                    {"agent_id": self._agent_id},
                )
                return [r[0] for r in result.all()]
        except Exception:
            logger.warning("ProcessRecorder.get_all_process_names failed", exc_info=True)
            return []
