"""Goals PR 0c: the stored background halt (``nous_system.background_halt`` and its audit log, migration 114).

Raw SQL: the two tables have no ORM model. Each state change and its audit row are written in one transaction.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text

from nous.background_halt import AuditEntry, HaltRow

_READ = text(
    "SELECT halted, halted_at, halted_by, halted_via, halted_verified, reason, resumed_at, resumed_by "
    "FROM nous_system.background_halt WHERE agent_id = :agent_id"
)
_UPSERT = text(
    "INSERT INTO nous_system.background_halt "
    "(agent_id, halted, halted_at, halted_by, halted_via, halted_verified, reason, resumed_at, resumed_by, updated_at) "
    "VALUES (:agent_id, :halted, :halted_at, :halted_by, :halted_via, :halted_verified, :reason, :resumed_at, "
    ":resumed_by, NOW()) "
    "ON CONFLICT (agent_id) DO UPDATE SET halted = EXCLUDED.halted, halted_at = EXCLUDED.halted_at, "
    "halted_by = EXCLUDED.halted_by, halted_via = EXCLUDED.halted_via, halted_verified = EXCLUDED.halted_verified, "
    "reason = EXCLUDED.reason, resumed_at = EXCLUDED.resumed_at, resumed_by = EXCLUDED.resumed_by, updated_at = NOW()"
)
_AUDIT = text(
    "INSERT INTO nous_system.background_halt_log (agent_id, action, actor, via, verified, reason, detail) "
    "VALUES (:agent_id, :action, :actor, :via, :verified, :reason, CAST(:detail AS JSONB))"
)


class BackgroundHaltStore:
    """The halt state of one agent, over the app's database."""

    def __init__(self, database: Any, agent_id: str) -> None:
        self._db = database
        self._agent_id = agent_id

    async def read(self) -> HaltRow | None:
        async with self._db.session() as session:
            row = (await session.execute(_READ, {"agent_id": self._agent_id})).first()
        if row is None:
            return None
        return HaltRow(
            halted=bool(row.halted),
            halted_at=row.halted_at,
            halted_by=row.halted_by,
            halted_via=row.halted_via,
            halted_verified=bool(row.halted_verified),
            reason=row.reason,
            resumed_at=row.resumed_at,
            resumed_by=row.resumed_by,
        )

    async def write(self, row: HaltRow, audit: AuditEntry) -> None:
        """The state and its audit row, in one transaction: neither lands without the other."""
        async with self._db.session() as session:
            await session.execute(
                _UPSERT,
                {
                    "agent_id": self._agent_id,
                    "halted": row.halted,
                    "halted_at": row.halted_at,
                    "halted_by": row.halted_by,
                    "halted_via": row.halted_via,
                    "halted_verified": row.halted_verified,
                    "reason": row.reason,
                    "resumed_at": row.resumed_at,
                    "resumed_by": row.resumed_by,
                },
            )
            await session.execute(_AUDIT, self._audit_params(audit))
            await session.commit()

    async def audit(self, entry: AuditEntry) -> None:
        async with self._db.session() as session:
            await session.execute(_AUDIT, self._audit_params(entry))
            await session.commit()

    def _audit_params(self, entry: AuditEntry) -> dict[str, Any]:
        return {
            "agent_id": self._agent_id,
            "action": entry.action,
            "actor": entry.actor,
            "via": entry.via,
            "verified": entry.verified,
            "reason": entry.reason,
            "detail": json.dumps(entry.detail) if entry.detail is not None else None,
        }
