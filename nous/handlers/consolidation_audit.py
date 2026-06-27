"""F035.6 — Consolidation Audit Diff.

A ``ConsolidationAuditor`` records, once per sleep cycle, a structured changelog
of every memory mutation the cycle made — grouped by phase, with before/after and
a rationale — into ``nous_system.consolidation_cycles`` (one envelope row) and
``nous_system.consolidation_actions`` (one row per mutation).

Design (see docs/features/F035.6-consolidation-audit-diff.md):

- Master-gated by ``settings.consolidation_audit_enabled`` (default OFF). When
  off the sleep handler never constructs an auditor, so sleep behaves
  byte-for-byte as today — there is no hot-path cost and no new writes.
- ``trace_id`` is a 12-char short hash generated in-process *before* any DB write
  (mirrors ``events.py``), so even an envelope-write failure leaves action rows
  causally linked. ``cycle_id`` degrades to ``None`` (orphan rows) on open
  failure rather than rejecting every action.
- Actions buffer per phase and flush as a single batched ``executemany``
  ``INSERT ... ON CONFLICT (action_id) DO NOTHING`` spawned as a tracked task.
  ``close()`` drains all pending batches *before* writing the ``completed``
  envelope, so a reader can never observe ``status=completed`` ahead of its
  action rows (A9).
- ``totals`` comes from the phases' *attempted* mutation counts (sleep_stats),
  so the invariant is ``actions_persisted <= totals`` — equality only on a
  fully-successful cycle. A suppressed batch failure undercounts the action
  table but leaves ``totals`` truthful; the mismatch is logged as an
  audit-integrity WARNING.
- Every DB failure is debug/warning-suppressed: a broken audit can never break a
  sleep cycle.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text as sa_text

logger = logging.getLogger(__name__)

# Cap on the content previews stored in before/after (R5: bound size + PII).
_PREVIEW_CHARS = 200


def make_trace_id() -> str:
    """12-char short hash, matching ``nous/events.py`` ``event_id`` generation."""
    return uuid.uuid4().hex[:12]


def preview(content: Any, limit: int = _PREVIEW_CHARS) -> str:
    """Truncate arbitrary content to a bounded preview string."""
    s = "" if content is None else str(content)
    return s[:limit]


class ConsolidationAuditor:
    """Records a single sleep cycle's mutations. One instance per ``_run_sleep``.

    The caller (sleep handler) constructs this only when the audit flag is on,
    calls :meth:`open` once, threads ``self`` into each mutating phase (which
    call :meth:`record` + ``await`` :meth:`flush` at phase end), then calls
    :meth:`close` with the final ``sleep_stats`` totals.
    """

    def __init__(
        self,
        db: Any,
        agent_id: str,
        *,
        max_inflight: int = 32,
        parent_trace_id: str | None = None,
    ) -> None:
        self._db = db
        self._agent_id = agent_id
        self._max_inflight = max(1, int(max_inflight))
        self.cycle_id: uuid.UUID | None = uuid.uuid4()
        self.trace_id: str = parent_trace_id or make_trace_id()
        self._buffer: list[dict] = []
        self._pending: set[asyncio.Task] = set()
        # Conservation counters (A1/A2).
        self.actions_recorded: int = 0   # record() calls (== actual mutations)
        self.actions_persisted: int = 0  # rows actually inserted

    # -- lifecycle ----------------------------------------------------------

    async def open(self) -> None:
        """Insert the ``running`` envelope row. Degrades to orphan mode on failure."""
        try:
            async with self._db.session() as s:
                await s.execute(
                    sa_text(
                        "INSERT INTO nous_system.consolidation_cycles "
                        "(cycle_id, trace_id, agent_id, started_at, status) "
                        "VALUES (:cid, :tid, :aid, :started, 'running')"
                    ),
                    {
                        "cid": self.cycle_id,
                        "tid": self.trace_id,
                        "aid": self._agent_id,
                        "started": datetime.now(UTC),
                    },
                )
                await s.commit()
        except Exception:
            # Cycle open failed: keep trace_id, drop cycle_id so action rows are
            # written as recoverable orphans (cycle_id=NULL) rather than rejected.
            logger.warning(
                "F035.6: consolidation cycle open failed; degrading to orphan rows "
                "(trace_id=%s)", self.trace_id, exc_info=True,
            )
            self.cycle_id = None

    def record(
        self,
        phase: str,
        op: str,
        *,
        target_ids: list | None = None,
        before: Any = None,
        after: Any = None,
        rationale: str | None = None,
    ) -> None:
        """Buffer one mutation. Cheap + synchronous; flushed per phase."""
        self._buffer.append(
            {
                "action_id": uuid.uuid4(),
                "cycle_id": self.cycle_id,
                "trace_id": self.trace_id,
                "agent_id": self._agent_id,
                "phase": phase,
                "op": op,
                "target_ids": _coerce_uuids(target_ids),
                "before": before,
                "after": after,
                "rationale": rationale,
            }
        )
        self.actions_recorded += 1

    async def flush(self) -> None:
        """Emit the buffered actions as one batched insert.

        Spawned as a tracked task so the sleep loop does not block on DB I/O.
        Backpressure: if too many batches are already in flight, await this one
        inline rather than spawning, so audit rows are never dropped.
        """
        if not self._buffer:
            return
        batch = self._buffer
        self._buffer = []
        if len(self._pending) >= self._max_inflight:
            await self._insert_batch(batch)
            return
        task = asyncio.create_task(self._insert_batch(batch))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def close(self, status: str, phases_run: list[str], totals: dict) -> None:
        """Drain pending batches, then write the terminal envelope row.

        The drain (A9) is what makes ``status=completed`` unobservable before its
        action rows land — awaiting only the close write would not drain the
        fire-and-forget batch tasks.
        """
        await self.flush()
        if self._pending:
            await asyncio.gather(*list(self._pending), return_exceptions=True)

        # Integrity signal: persisted should never exceed attempted (totals).
        attempted = _sum_totals(totals)
        if self.actions_persisted < self.actions_recorded:
            logger.warning(
                "F035.6: audit integrity — persisted %d < recorded %d actions "
                "(cycle=%s); audit table lost rows (memory mutation unaffected)",
                self.actions_persisted, self.actions_recorded, self.cycle_id,
            )

        if self.cycle_id is None:
            logger.warning(
                "F035.6: skipping envelope close — cycle never opened (trace_id=%s)",
                self.trace_id,
            )
            return
        try:
            async with self._db.session() as s:
                await s.execute(
                    sa_text(
                        "UPDATE nous_system.consolidation_cycles "
                        "SET status = :st, finished_at = :fin, phases_run = :phases, "
                        "    totals = CAST(:totals AS jsonb) "
                        "WHERE cycle_id = :cid"
                    ),
                    {
                        "st": status,
                        "fin": datetime.now(UTC),
                        "phases": phases_run,
                        "totals": _json_dumps({**totals, "_attempted": attempted}),
                        "cid": self.cycle_id,
                    },
                )
                await s.commit()
        except Exception:
            logger.warning(
                "F035.6: consolidation cycle close failed (cycle=%s)",
                self.cycle_id, exc_info=True,
            )

    # -- retention ----------------------------------------------------------

    async def prune_old_actions(self, retention_days: int) -> int:
        """Delete action rows older than ``retention_days``. Envelopes are kept.

        Returns the number of deleted rows. Disabled when ``retention_days <= 0``.
        """
        if retention_days <= 0:
            return 0
        try:
            async with self._db.session() as s:
                result = await s.execute(
                    sa_text(
                        "DELETE FROM nous_system.consolidation_actions "
                        "WHERE created_at < now() - make_interval(days => :days)"
                    ),
                    {"days": int(retention_days)},
                )
                await s.commit()
                return int(result.rowcount or 0)
        except Exception:
            logger.warning("F035.6: consolidation action retention sweep failed", exc_info=True)
            return 0

    # -- internals ----------------------------------------------------------

    async def _insert_batch(self, batch: list[dict]) -> None:
        """One batched ``executemany`` insert; debug-suppressed on failure."""
        if not batch:
            return
        params = [
            {
                **row,
                "before": _json_dumps(row["before"]) if row["before"] is not None else None,
                "after": _json_dumps(row["after"]) if row["after"] is not None else None,
            }
            for row in batch
        ]
        try:
            async with self._db.session() as s:
                result = await s.execute(
                    sa_text(
                        "INSERT INTO nous_system.consolidation_actions "
                        "(action_id, cycle_id, trace_id, agent_id, phase, op, "
                        " target_ids, before, after, rationale) VALUES "
                        "(:action_id, :cycle_id, :trace_id, :agent_id, :phase, :op, "
                        " :target_ids, CAST(:before AS jsonb), CAST(:after AS jsonb), :rationale) "
                        "ON CONFLICT (action_id) DO NOTHING"
                    ),
                    params,
                )
                await s.commit()
                # executemany rowcount is driver-dependent; fall back to len(batch).
                inserted = result.rowcount if (result.rowcount or 0) >= 0 else len(batch)
                self.actions_persisted += inserted if inserted else len(batch)
        except Exception:
            logger.warning(
                "F035.6: consolidation action batch insert failed (%d rows, cycle=%s)",
                len(batch), self.cycle_id, exc_info=True,
            )


def _coerce_uuids(ids: list | None) -> list[uuid.UUID] | None:
    """Best-effort coerce a heterogeneous id list to UUIDs; drop non-UUID ids."""
    if not ids:
        return None
    out: list[uuid.UUID] = []
    for i in ids:
        if isinstance(i, uuid.UUID):
            out.append(i)
            continue
        try:
            out.append(uuid.UUID(str(i)))
        except (ValueError, AttributeError, TypeError):
            continue
    return out or None


def _sum_totals(totals: dict) -> int:
    """Sum the integer mutation counters in a totals dict (ignore non-ints)."""
    return sum(v for v in totals.values() if isinstance(v, int) and not isinstance(v, bool))


def _json_dumps(obj: Any) -> str | None:
    import json

    if obj is None:
        return None
    try:
        return json.dumps(obj, default=str)
    except (TypeError, ValueError):
        return json.dumps(str(obj))
