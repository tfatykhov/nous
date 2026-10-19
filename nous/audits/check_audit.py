"""The weekly sampler of completion checks the agent wrote for itself (scorer v5, spec section C3).

Once a week (Mondays, from ``AUDIT_HOUR_UTC``) it picks a few subtasks that finished in the last 7 days, whose own DAG
node check passed, and pushes one companion card for the owner to mark Right or Wrong (``a2ui/builders/check_audit.py``,
``audit.verdict`` in ``a2ui/actions.py``). The model has no input into which subtasks are picked, and no tool reaches
this component.

Write order (so a crash never leaves a card the scorer cannot match): the rows go in with ``surface_id`` NULL, then the
card is pushed, then ``surface_id`` is set on the rows. A failed push leaves the rows unpushed; they are re-offered, and
reused, on the next tick of the same week, because "done this week" is a row with a ``surface_id``, read from the DB.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update

from nous.a2ui.builders.check_audit import check_audit
from nous.cancellation import cancel_requested
from nous.observability import task_scoring as ts
from nous.storage.models import CheckAudit, DAGNode, Subtask

logger = logging.getLogger(__name__)

AUDIT_WEEKDAY = 0  # Monday (datetime.weekday())
AUDIT_HOUR_UTC = 9  # the card goes out from this UTC hour on that day
LOOP_INTERVAL_SECONDS = 3600
LOOKBACK = timedelta(days=7)  # a subtask is sampled only if it finished within this long
NOTIFY_TEXT = "Weekly spot check: a few finished tasks to look over"

Clock = Callable[[], datetime]


def week_start(now: datetime) -> datetime:
    """Midnight UTC on the Monday of ``now``'s ISO week."""
    now = now.astimezone(UTC)
    return (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)


def round_robin(by_kind: dict[str, list[Any]], size: int, rng: Any) -> list[Any]:
    """Up to ``size`` items, one kind at a time in turn, in a random order of kinds and within each kind."""
    pools = {kind: list(items) for kind, items in sorted(by_kind.items()) if items}
    for items in pools.values():
        rng.shuffle(items)
    kinds = list(pools)
    rng.shuffle(kinds)
    picked: list[Any] = []
    while len(picked) < size and any(pools.values()):
        for kind in kinds:
            if pools[kind] and len(picked) < size:
                picked.append(pools[kind].pop())
    return picked


class CheckAuditSampler:
    """The weekly spot-check sampler. ``clock`` and ``rng`` are injected so tests are deterministic."""

    def __init__(
        self,
        database: Any,
        agent_id: str,
        surface_service: Any,
        settings: Any,
        *,
        clock: Clock | None = None,
        rng: Any | None = None,
    ) -> None:
        self._db = database
        self._agent_id = agent_id
        self._surfaces = surface_service
        self._settings = settings
        self._clock = clock or (lambda: datetime.now(UTC))
        self._rng = rng or secrets.SystemRandom()

    async def tick(self) -> int:
        """One pass. Returns the number of items pushed, 0 for a no-op pass."""
        if not self._settings.check_audit_enabled:
            return 0
        now = self._clock().astimezone(UTC)
        if now.weekday() != AUDIT_WEEKDAY or now.hour < AUDIT_HOUR_UTC:
            return 0
        if await self._done_this_week(now):
            return 0
        candidates = await self._eligible(now)
        if not candidates:
            return 0
        by_kind: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for c in candidates:
            by_kind[c["kind"]].append(c)
        picked = round_robin(by_kind, self._settings.check_audit_size, self._rng)
        ids = await self._write_rows(picked, now)  # 1. the rows, surface_id NULL
        items = [
            {"audit_id": str(ids[c["task_id"]]), "task": c["task"], "check": c["check"], "result": c["result"]}
            for c in picked
        ]
        surface_id = await self._surfaces.push_built(  # 2. the card; a failure leaves the rows unpushed
            check_audit(items), notify=True, notify_text=NOTIFY_TEXT
        )
        async with self._db.session() as session:  # 3. the rows now name their card
            await session.execute(
                update(CheckAudit)
                .where(CheckAudit.agent_id == self._agent_id, CheckAudit.id.in_(list(ids.values())))
                .values(surface_id=surface_id)
            )
            await session.commit()
        logger.info("Check audits: pushed a card of %d item(s)", len(items))
        return len(items)

    async def _done_this_week(self, now: datetime) -> bool:
        async with self._db.session() as session:
            found = await session.execute(
                select(func.count())
                .select_from(CheckAudit)
                .where(
                    CheckAudit.agent_id == self._agent_id,
                    CheckAudit.surface_id.is_not(None),
                    CheckAudit.sampled_at >= week_start(now),
                )
            )
            return int(found.scalar_one()) > 0

    async def _eligible(self, now: datetime) -> list[dict[str, Any]]:
        """Subtasks finished in the last 7 days that the scorer would read as raw done with a passed own node check
        (the node records them as its current attempt), whose kind is not ``pr``, and that no card has offered."""
        async with self._db.session() as session:
            result = await session.execute(
                select(
                    Subtask.id,
                    Subtask.task,
                    Subtask.result,
                    Subtask.status,
                    Subtask.final_outcome,
                    DAGNode.id.label("node_id"),
                    DAGNode.status.label("node_status"),
                    DAGNode.error.label("node_error"),
                    DAGNode.completion_check,
                    DAGNode.check_attempts,
                    DAGNode.last_check_at,
                )
                .join(DAGNode, Subtask.dag_node_id == DAGNode.id)
                .where(
                    Subtask.agent_id == self._agent_id,
                    DAGNode.subtask_id == Subtask.id,
                    Subtask.completed_at >= now - LOOKBACK,
                )
                .order_by(Subtask.completed_at, Subtask.id)
            )
            rows = list(result)
            taken = await session.execute(
                select(CheckAudit.task_id).where(
                    CheckAudit.agent_id == self._agent_id,
                    (CheckAudit.surface_id.is_not(None)) | (CheckAudit.verdict.is_not(None)),
                )
            )
            offered = {r.task_id for r in taken}
        out = []
        for r in rows:
            if r.id in offered or ts.raw_outcome("subtask", r.status, r.final_outcome) != "done":
                continue
            state = ts.node_check_state(
                r.completion_check, r.check_attempts, r.last_check_at, r.node_status, r.node_error
            )
            kind = ts.check_kind(r.completion_check)
            if state != "passed" or kind == "pr":
                continue
            out.append(
                {
                    "task_id": r.id,
                    "node_id": r.node_id,
                    "kind": kind,
                    "task": r.task,
                    "check": r.completion_check,
                    "result": r.result,
                }
            )
        return out

    async def _write_rows(self, picked: list[dict[str, Any]], now: datetime) -> dict[UUID, UUID]:
        """Insert the picked rows with ``surface_id`` NULL; an unpushed row from an earlier tick is reused
        (re-stamped, so its time is that of the card it goes out on). ``sampled_at`` is the injected clock's."""
        async with self._db.session() as session:
            existing = await session.execute(
                select(CheckAudit).where(
                    CheckAudit.agent_id == self._agent_id, CheckAudit.task_id.in_([c["task_id"] for c in picked])
                )
            )
            rows = {r.task_id: r for r in existing.scalars()}
            for c in picked:
                row = rows.get(c["task_id"])
                if row is None:
                    row = CheckAudit(
                        agent_id=self._agent_id,
                        task_id=c["task_id"],
                        node_id=c["node_id"],
                        check_kind=c["kind"],
                        sampled_at=now,
                    )
                    session.add(row)
                    rows[c["task_id"]] = row
                else:
                    row.node_id, row.check_kind, row.sampled_at = c["node_id"], c["kind"], now
            await session.flush()
            ids = {task_id: row.id for task_id, row in rows.items()}
            await session.commit()
            return ids

    async def run(self) -> None:
        """The maintenance loop: a pass every ``LOOP_INTERVAL_SECONDS``; a failed pass is logged."""
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                if cancel_requested():
                    break
                logger.exception("Check audits: the loop was cancelled from within, it continues")
            except Exception:
                logger.warning("Check audits: a pass failed", exc_info=True)
            try:
                await asyncio.sleep(LOOP_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                if cancel_requested():
                    break
                logger.exception("Check audits: the loop was cancelled from within, it continues")
