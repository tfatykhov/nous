"""Compensation registry and snapshot store (harness Phase 2.8).

A compensator undoes a successful tool call given its ledger row and a
snapshot of the state before the call. The registry is separate from
``tool_classes.py`` (which is a leaf with no ``nous`` imports) because a
compensator needs the database and domain objects at runtime.

Snapshot lifecycle:
  1. Before dispatch of a compensable tool in a background context,
     ``SnapshotStore.capture`` persists the prior state.
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


async def snapshot_for_write_file(
    path: str,
    workspace_dir: str,
) -> dict[str, Any]:
    """Capture the prior state of a file before write_file overwrites it."""
    import os

    full_path = os.path.join(workspace_dir, path) if not os.path.isabs(path) else path
    existed = os.path.exists(full_path)
    prior_content: str | None = None
    if existed:
        try:
            with open(full_path, encoding="utf-8", errors="replace") as f:
                prior_content = f.read()
        except Exception:
            prior_content = None
    return {
        "path": path,
        "full_path": full_path,
        "existed": existed,
        "prior_content": prior_content,
    }


async def compensate_write_file(
    entry_id: UUID,
    snapshot_data: dict[str, Any],
    deps: Any,
) -> CompensationResult:
    """Restore prior file content or delete if file was new."""
    import os

    full_path = snapshot_data.get("full_path", "")
    existed = snapshot_data.get("existed", False)
    prior_content = snapshot_data.get("prior_content")

    if not full_path:
        return CompensationResult(False, "no path in snapshot")
    if not os.path.exists(full_path):
        # File is absent. If it was new (existed=False), absence IS the reverted state.
        # If it existed before, we must recreate it — otherwise the original content is lost.
        if not existed:
            return CompensationResult(True, "file already absent")
        if prior_content is not None:
            try:
                os.makedirs(os.path.dirname(full_path) or ".", exist_ok=True)
                with open(full_path, "w", encoding="utf-8") as f:
                    f.write(prior_content)
                return CompensationResult(True, f"recreated prior content of {full_path}")
            except Exception as exc:
                return CompensationResult(False, f"revert failed: {exc}")
        return CompensationResult(False, "file existed but prior content not captured; cannot recreate")

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


async def compensate_schedule_task(
    entry_id: UUID,
    snapshot_data: dict[str, Any],
    deps: Any,
) -> CompensationResult:
    """Cancel a schedule that was created."""
    schedule_id = snapshot_data.get("schedule_id")
    if not schedule_id:
        return CompensationResult(False, "no schedule_id in snapshot")
    heart = getattr(deps, "heart", None)
    if heart is None:
        return CompensationResult(False, "heart not available")
    try:
        from nous.heart.schedules import deactivate_schedule

        async with heart._db.session() as s:
            ok = await deactivate_schedule(s, UUID(schedule_id), heart._agent_id)
            await s.commit()
        if ok:
            return CompensationResult(True, f"cancelled schedule {schedule_id}")
        return CompensationResult(True, f"schedule {schedule_id} already inactive")
    except Exception as exc:
        return CompensationResult(False, f"revert failed: {exc}")


async def compensate_heartbeat_check_create(
    entry_id: UUID,
    snapshot_data: dict[str, Any],
    deps: Any,
) -> CompensationResult:
    """Disable a heartbeat check that was created."""
    check_name = snapshot_data.get("check_name")
    if not check_name:
        return CompensationResult(False, "no check_name in snapshot")
    loader = getattr(deps, "heartbeat_loader", None)
    if loader is None:
        return CompensationResult(False, "heartbeat loader not available")
    try:
        await loader.manage_check(check_name, action="disable")
        return CompensationResult(True, f"disabled check {check_name!r}")
    except Exception as exc:
        return CompensationResult(False, f"revert failed: {exc}")


async def compensate_heartbeat_check_manage(
    entry_id: UUID,
    snapshot_data: dict[str, Any],
    deps: Any,
) -> CompensationResult:
    """Reverse an enable/disable action on a heartbeat check."""
    check_name = snapshot_data.get("check_name")
    prior_enabled = snapshot_data.get("prior_enabled")
    if not check_name or prior_enabled is None:
        return CompensationResult(False, "missing check_name or prior_enabled in snapshot")
    loader = getattr(deps, "heartbeat_loader", None)
    if loader is None:
        return CompensationResult(False, "heartbeat loader not available")
    try:
        action = "enable" if prior_enabled else "disable"
        await loader.manage_check(check_name, action=action)
        state = "enabled" if prior_enabled else "disabled"
        return CompensationResult(True, f"restored check {check_name!r} to {state}")
    except Exception as exc:
        return CompensationResult(False, f"revert failed: {exc}")


async def compensate_resolve_decision(
    entry_id: UUID,
    snapshot_data: dict[str, Any],
    deps: Any,
) -> CompensationResult:
    """Restore a decision's prior outcome."""
    decision_id = snapshot_data.get("decision_id")
    prior_outcome = snapshot_data.get("prior_outcome")
    if not decision_id:
        return CompensationResult(False, "no decision_id in snapshot")
    brain = getattr(deps, "brain", None)
    if brain is None:
        return CompensationResult(False, "brain not available")
    try:
        async with brain._db.session() as s:
            from nous.storage.models import Decision

            result = await s.execute(
                update(Decision)
                .where(Decision.id == UUID(decision_id))
                .where(Decision.agent_id == brain._agent_id)
                .values(
                    outcome=prior_outcome,
                    resolution_note=snapshot_data.get("prior_resolution_note"),
                    resolved_at=snapshot_data.get("prior_resolved_at"),
                )
            )
            await s.commit()
        if (result.rowcount or 0) > 0:
            return CompensationResult(True, f"restored decision {decision_id} to outcome={prior_outcome!r}")
        return CompensationResult(False, f"decision {decision_id} not found")
    except Exception as exc:
        return CompensationResult(False, f"revert failed: {exc}")


def register_compensators(registry: CompensationRegistry) -> None:
    """Register all built-in compensators."""
    registry.register("write_file", compensate_write_file)
    registry.register("schedule_task", compensate_schedule_task)
    registry.register("heartbeat_check_create", compensate_heartbeat_check_create)
    registry.register("heartbeat_check_manage", compensate_heartbeat_check_manage)
    registry.register("resolve_decision", compensate_resolve_decision)
