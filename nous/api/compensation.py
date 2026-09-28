"""Compensation registry and snapshot store (harness Phase 2.8).

A compensator undoes a successful tool call given its ledger row and a
snapshot of the state before the call. The registry is separate from
``tool_classes.py`` (which is a leaf with no ``nous`` imports) because a
compensator needs the database and domain objects at runtime.

Snapshot lifecycle:
  1. Before dispatch of a compensable tool in a background context,
     ``SnapshotStore.capture`` persists the prior state.
     Spawning tools (schedule_task, heartbeat_check_create) are deliberately
     NOT compensable: cancelling after the first fire does not undo the work
     it already started.
  2. On ``review.revert``, the handler reads the snapshot and calls the
     compensator. ``mark_reverted`` is idempotent (double revert = no-op).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, update

from nous.api.tool_classes import TOOL_CLASSES
from nous.storage.models import CompensationSnapshot

logger = logging.getLogger(__name__)

# Cap for file snapshots: matches the read_file tool's 1 MiB limit.
_FILE_SNAPSHOT_MAX_BYTES = 1 * 1024 * 1024


class SnapshotBlocksDispatch(Exception):
    """Raised when a required snapshot cannot be captured, preventing the dispatch.

    Only raised in undoable contexts where a missing snapshot would silently
    allow a non-revertible side effect past the undoable guarantee.
    """


@dataclass(frozen=True)
class CompensationResult:
    """What a compensator did."""

    success: bool
    message: str


Compensator = Callable[[UUID, dict[str, Any], Any], Awaitable[CompensationResult]]


class CompensationRegistry:
    """Runtime registry of compensator functions, keyed by tool name."""

    def __init__(self) -> None:
        self._compensators: dict[str, Compensator] = {}

    def register(self, tool_name: str, fn: Compensator) -> None:
        tc = TOOL_CLASSES.get(tool_name)
        if tc is None or not tc.compensable:
            raise ValueError(
                f"cannot register a compensator for {tool_name!r}: it must be declared compensable in TOOL_CLASSES"
            )
        self._compensators[tool_name] = fn

    def get(self, tool_name: str) -> Compensator | None:
        return self._compensators.get(tool_name)

    def is_registered(self, tool_name: str) -> bool:
        return tool_name in self._compensators


class SnapshotStore:
    """Reads and writes ``nous_system.compensation_snapshots`` (migration 077)."""

    def __init__(self, database: Any, agent_id: str, *, timeout: float = 5.0) -> None:
        self._db = database
        self._agent_id = agent_id
        self._timeout = timeout

    async def capture(
        self,
        *,
        ledger_entry_id: UUID,
        tool_name: str,
        snapshot_data: dict[str, Any],
    ) -> UUID:
        snapshot_id = uuid4()
        row = CompensationSnapshot(
            id=snapshot_id,
            ledger_entry_id=ledger_entry_id,
            agent_id=self._agent_id,
            tool_name=tool_name,
            snapshot_data=snapshot_data,
        )

        async def _write() -> None:
            async with self._db.session() as s:
                s.add(row)
                await s.commit()

        await asyncio.wait_for(_write(), timeout=self._timeout)
        return snapshot_id

    async def get_by_ledger_entry(self, ledger_entry_id: UUID) -> CompensationSnapshot | None:
        async with self._db.session() as s:
            result = await s.execute(
                select(CompensationSnapshot)
                .where(CompensationSnapshot.ledger_entry_id == ledger_entry_id)
                .where(CompensationSnapshot.agent_id == self._agent_id)
                .limit(1)
            )
            return result.scalar_one_or_none()

    async def mark_reverted(
        self,
        snapshot_id: UUID,
        *,
        result_message: str,
    ) -> bool:
        """Mark a snapshot as reverted. Returns False if already reverted (idempotent)."""
        async with self._db.session() as s:
            res = await s.execute(
                update(CompensationSnapshot)
                .where(CompensationSnapshot.id == snapshot_id)
                .where(CompensationSnapshot.agent_id == self._agent_id)
                .where(CompensationSnapshot.reverted_at.is_(None))
                .values(reverted_at=datetime.now(UTC), revert_result=result_message)
            )
            await s.commit()
            return (res.rowcount or 0) == 1

    async def check_enabled(self, name: str) -> bool | None:
        """Whether dynamic check ``name`` is enabled right now, or None when
        no such check exists. Read before a ``heartbeat_check_manage``
        disable so its revert restores the prior state."""
        from nous.storage.models import DynamicCheckModel

        async def _read() -> bool | None:
            async with self._db.session() as s:
                result = await s.execute(
                    select(DynamicCheckModel.enabled)
                    .where(DynamicCheckModel.agent_id == self._agent_id)
                    .where(DynamicCheckModel.name == name)
                )
                return result.scalar_one_or_none()

        return await asyncio.wait_for(_read(), timeout=self._timeout)

    async def decision_state(self, decision_id: str) -> dict[str, Any] | None:
        """The review fields ``Brain.review`` overwrites, as they are now, or
        None when this agent has no such decision. Read before a
        ``resolve_decision`` so its revert restores exactly this state."""
        from nous.storage.models import Decision

        async def _read() -> dict[str, Any] | None:
            async with self._db.session() as s:
                row = (
                    await s.execute(
                        select(
                            Decision.outcome,
                            Decision.outcome_result,
                            Decision.reviewed_at,
                            Decision.reviewer,
                            Decision.superseded_by,
                        )
                        .where(Decision.id == UUID(decision_id))
                        .where(Decision.agent_id == self._agent_id)
                    )
                ).first()
            if row is None:
                return None
            return {
                "outcome": row[0],
                "outcome_result": row[1],
                "reviewed_at": row[2].isoformat() if row[2] is not None else None,
                "reviewer": row[3],
                "superseded_by": str(row[4]) if row[4] is not None else None,
            }

        return await asyncio.wait_for(_read(), timeout=self._timeout)


async def snapshot_for_write_file(
    path: str,
    workspace_dir: str,
) -> dict[str, Any]:
    """Capture the prior state of a file before write_file overwrites it.

    I/O is offloaded to a worker thread so the event loop is never stalled.
    Files larger than ``_FILE_SNAPSHOT_MAX_BYTES`` are flagged ``oversized=True``
    and will not have ``prior_content`` captured; callers in undoable contexts
    should raise ``SnapshotBlocksDispatch`` rather than proceed without a snapshot.
    """
    import os

    full_path = os.path.join(workspace_dir, path) if not os.path.isabs(path) else path
    existed = os.path.exists(full_path)
    prior_content: str | None = None
    oversized = False
    if existed:
        try:
            file_size = os.path.getsize(full_path)
            if file_size > _FILE_SNAPSHOT_MAX_BYTES:
                oversized = True
            else:

                def _read() -> str:
                    with open(full_path, encoding="utf-8", errors="replace") as f:
                        return f.read()

                prior_content = await asyncio.to_thread(_read)
        except Exception:
            prior_content = None
    return {
        "path": path,
        "full_path": full_path,
        "existed": existed,
        "prior_content": prior_content,
        "oversized": oversized,
    }


async def compensate_write_file(
    entry_id: UUID,
    snapshot_data: dict[str, Any],
    deps: Any,
) -> CompensationResult:
    """Restore prior file content or delete if file was new.

    Stale-revert guard: the current file is hashed and compared with the
    ``written_content_hash`` recorded at snapshot time before anything is
    touched. A mismatch -- or a file our write left present that is now gone
    -- means something changed it after our ``write_file`` ran, and the
    revert is refused rather than discard that newer change. A snapshot
    without the hash cannot make that distinction and is refused too.
    """
    import hashlib
    import os

    full_path = snapshot_data.get("full_path", "")
    existed = snapshot_data.get("existed", False)
    prior_content = snapshot_data.get("prior_content")
    written_content_hash: str | None = snapshot_data.get("written_content_hash")

    if not full_path:
        return CompensationResult(False, "no path in snapshot")
    if written_content_hash is None:
        # Without the hash of what the call wrote there is no way to tell our
        # write from a newer one, so the revert could destroy newer content.
        return CompensationResult(False, "revert refused: snapshot does not record what was written")
    if not os.path.exists(full_path):
        if not existed:
            return CompensationResult(True, "file already absent")
        # Our write left the file present: its absence now is a newer change.
        return CompensationResult(False, f"revert refused: {full_path!r} was removed after the original write")

    # Stale-revert guard: refuse if the file was modified after our write.
    try:
        with open(full_path, "rb") as f:
            current_hash = hashlib.sha256(f.read()).hexdigest()
        if current_hash != written_content_hash:
            return CompensationResult(
                False,
                f"revert refused: {full_path!r} was modified after the original write; "
                "revert would overwrite newer content",
            )
    except Exception as exc:
        return CompensationResult(False, f"stale-check read failed: {exc}")

    try:
        if existed and prior_content is not None:
            with open(full_path, "w", encoding="utf-8") as f:
                f.write(prior_content)
            return CompensationResult(True, f"restored prior content of {full_path}")
        elif not existed:
            os.remove(full_path)
            return CompensationResult(True, f"deleted {full_path} (was new)")
        else:
            return CompensationResult(False, "file existed but prior content not captured")
    except Exception as exc:
        return CompensationResult(False, f"revert failed: {exc}")


async def compensate_heartbeat_check_manage(
    entry_id: UUID,
    snapshot_data: dict[str, Any],
    deps: Any,
) -> CompensationResult:
    """Restore a heartbeat check the call disabled to its prior state.

    Only ``action="disable"`` is compensable (``is_compensable_call``), so the
    runner only snapshots that action, recording ``prior_enabled``. A check
    that was already disabled before the call is left disabled (the disable
    was a no-op); a check whose prior state is unknown is NOT enabled, since
    that could start autonomous work that was inactive before the call.
    """
    check_name = snapshot_data.get("check_name")
    if not check_name:
        return CompensationResult(False, "no check_name in snapshot")
    if snapshot_data.get("action") != "disable":
        return CompensationResult(False, f"action {snapshot_data.get('action')!r} is not revertible")
    if "prior_enabled" not in snapshot_data:
        return CompensationResult(
            False, f"prior state of check {check_name!r} was not recorded (snapshot predates capture); not enabling"
        )
    prior_enabled = snapshot_data["prior_enabled"]
    if prior_enabled is False:
        return CompensationResult(True, f"check {check_name!r} was already disabled before the call; left disabled")
    if prior_enabled is not True:
        return CompensationResult(False, f"prior state of check {check_name!r} is unknown; not enabling")
    loader = getattr(deps, "heartbeat_loader", None)
    if loader is None:
        return CompensationResult(False, "heartbeat loader not available")
    try:
        await loader.manage_check("enable", name=check_name)
        return CompensationResult(True, f"re-enabled check {check_name!r}")
    except Exception as exc:
        return CompensationResult(False, f"revert failed: {exc}")


async def compensate_resolve_decision(
    entry_id: UUID,
    snapshot_data: dict[str, Any],
    deps: Any,
) -> CompensationResult:
    """Restore the review fields ``Brain.review`` overwrote.

    Uses the Brain's public ``db`` / ``agent_id``. Stale guard: the update
    applies only while the decision still carries the outcome this call
    wrote, so a later re-resolution is never silently undone.
    """
    from nous.storage.models import Decision

    decision_id = snapshot_data.get("decision_id")
    prior = snapshot_data.get("prior")
    if not decision_id:
        return CompensationResult(False, "no decision_id in snapshot")
    if not isinstance(prior, dict):
        return CompensationResult(False, "prior state of the decision was not recorded; not reverting")
    brain = getattr(deps, "brain", None)
    if brain is None:
        return CompensationResult(False, "brain not available")
    reviewed_at = prior.get("reviewed_at")
    superseded_by = prior.get("superseded_by")
    try:
        stmt = (
            update(Decision)
            .where(Decision.id == UUID(decision_id))
            .where(Decision.agent_id == brain.agent_id)
            .where(Decision.outcome == snapshot_data.get("written_outcome"))
            .values(
                outcome=prior.get("outcome"),
                outcome_result=prior.get("outcome_result"),
                reviewed_at=datetime.fromisoformat(reviewed_at) if reviewed_at else None,
                reviewer=prior.get("reviewer"),
                superseded_by=UUID(superseded_by) if superseded_by else None,
            )
        )
        async with brain.db.session() as s:
            result = await s.execute(stmt)
            await s.commit()
        if (result.rowcount or 0) > 0:
            return CompensationResult(True, f"restored decision {decision_id} to outcome={prior.get('outcome')!r}")
        return CompensationResult(
            False,
            f"revert refused: decision {decision_id} not found or re-resolved since the original call",
        )
    except Exception as exc:
        return CompensationResult(False, f"revert failed: {exc}")


def register_compensators(registry: CompensationRegistry) -> None:
    """Register all built-in compensators."""
    registry.register("write_file", compensate_write_file)
    registry.register("heartbeat_check_manage", compensate_heartbeat_check_manage)
    registry.register("resolve_decision", compensate_resolve_decision)
