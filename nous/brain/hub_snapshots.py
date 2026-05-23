"""F065: HubSnapshotManager — session-start hub-shift detection.

Reads/writes brain.graph_hub_snapshots to track which decisions/facts/
episodes/procedures are currently in the top-10 hub list and emit a
working-memory notice when a node enters or leaves the top-10
(rank-based shift, not raw degree-percent — see F065 spec Open
Question 4 resolution).

Fail-mode: every method catches its own exceptions and logs at WARN;
hub-shift detection is a diagnostic, not a correctness path. Callers
(pre_turn hook) wrap the whole computation in asyncio.create_task so
session start never blocks on it.

Retention: prune_older_than() is called from the sleep handler with
the configured retention days. The (agent_id, captured_at) index
supports the prune query.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from nous.storage.database import Database
from nous.storage.models import GraphHubSnapshot

logger = logging.getLogger(__name__)


class HubSnapshotManager:
    """Read/write hub-rank snapshots for session-start shift detection."""

    def __init__(self, database: Database, agent_id: str) -> None:
        self._db = database
        self._agent_id = agent_id

    async def get_latest(
        self,
        node_ids: list[UUID],
        session: AsyncSession | None = None,
    ) -> dict[UUID, GraphHubSnapshot]:
        """Return the most-recent snapshot per (agent_id, node_id).

        Uses DISTINCT ON (Postgres) when available; falls back to a
        portable subquery for the SQLite test backend.
        """
        if not node_ids:
            return {}
        if session is None:
            async with self._db.session() as s:
                return await self._get_latest_inner(node_ids, s)
        return await self._get_latest_inner(node_ids, session)

    async def get_latest_top_n(
        self,
        top_n: int,
        session: AsyncSession | None = None,
    ) -> dict[UUID, GraphHubSnapshot]:
        """F065: return the most-recent snapshot for each node that was
        in the top-N at last capture (rank IS NOT NULL AND rank <= top_n).

        Used by pre_turn alongside ``get_latest`` for the live hubs —
        the combined set lets ``detect_rank_shifts`` fire 'left top-N'
        notices for nodes that dropped out (Codex review P1, 2026-05-22).
        Without this, the lookup is biased toward current live hubs and
        we never emit the 'left' branch of the shift signal.
        """
        from sqlalchemy import desc, func, select as _select

        if session is None:
            async with self._db.session() as s:
                return await self._get_latest_top_n_inner(top_n, s)
        return await self._get_latest_top_n_inner(top_n, session)

    async def _get_latest_top_n_inner(
        self,
        top_n: int,
        session: AsyncSession,
    ) -> dict[UUID, GraphHubSnapshot]:
        from sqlalchemy import desc, func

        # Codex P1 (2026-05-22, follow-up): take the ABSOLUTE most recent
        # snapshot per node (no rank filter in the subquery), then filter
        # the outer join by rank in [1, top_n]. This way, when a "left"
        # transition is persisted as a rank=NULL row, that newer row
        # supersedes the earlier in-top-N row and the node is correctly
        # excluded. Without this, a node that dropped out kept surfacing
        # its stale in-top-N snapshot turn after turn, re-emitting "left"
        # notices forever (prompt spam) until retention pruning kicked in.
        subq = (
            select(
                GraphHubSnapshot.node_id,
                func.max(GraphHubSnapshot.captured_at).label("max_at"),
            )
            .where(GraphHubSnapshot.agent_id == self._agent_id)
            .group_by(GraphHubSnapshot.node_id)
            .subquery()
        )
        q = (
            select(GraphHubSnapshot)
            .join(
                subq,
                (GraphHubSnapshot.node_id == subq.c.node_id)
                & (GraphHubSnapshot.captured_at == subq.c.max_at),
            )
            .where(GraphHubSnapshot.agent_id == self._agent_id)
            .where(GraphHubSnapshot.rank.is_not(None))
            .where(GraphHubSnapshot.rank <= top_n)
            .order_by(desc(GraphHubSnapshot.captured_at))
        )
        result = await session.execute(q)
        latest: dict[UUID, GraphHubSnapshot] = {}
        for snap in result.scalars().all():
            # Duplicates with identical captured_at (microsecond race) —
            # keep the first seen.
            latest.setdefault(snap.node_id, snap)
        return latest

    async def _get_latest_inner(
        self,
        node_ids: list[UUID],
        session: AsyncSession,
    ) -> dict[UUID, GraphHubSnapshot]:
        # Portable variant: for each node_id, pick the row with max
        # captured_at. SQLAlchemy ORM handles dialect specifics.
        from sqlalchemy import desc, func

        # Build a "max captured_at per node" subquery.
        subq = (
            select(
                GraphHubSnapshot.node_id,
                func.max(GraphHubSnapshot.captured_at).label("max_at"),
            )
            .where(GraphHubSnapshot.agent_id == self._agent_id)
            .where(GraphHubSnapshot.node_id.in_(node_ids))
            .group_by(GraphHubSnapshot.node_id)
            .subquery()
        )
        q = (
            select(GraphHubSnapshot)
            .join(
                subq,
                (GraphHubSnapshot.node_id == subq.c.node_id)
                & (GraphHubSnapshot.captured_at == subq.c.max_at),
            )
            .where(GraphHubSnapshot.agent_id == self._agent_id)
            .order_by(desc(GraphHubSnapshot.captured_at))
        )
        result = await session.execute(q)
        latest: dict[UUID, GraphHubSnapshot] = {}
        for snap in result.scalars().all():
            # If duplicates with identical captured_at (microsecond race),
            # keep the first one seen.
            latest.setdefault(snap.node_id, snap)
        return latest

    async def record_snapshot(
        self,
        node_id: UUID,
        node_type: str,
        degree: int,
        rank: int | None,
        session: AsyncSession | None = None,
    ) -> None:
        """Insert a snapshot row. Errors are logged and swallowed —
        the caller (pre_turn fire-and-forget task) doesn't propagate
        snapshot failures.
        """
        try:
            if session is None:
                async with self._db.session() as s:
                    s.add(GraphHubSnapshot(
                        agent_id=self._agent_id,
                        node_id=node_id,
                        node_type=node_type,
                        degree=degree,
                        rank=rank,
                    ))
                    await s.commit()
            else:
                session.add(GraphHubSnapshot(
                    agent_id=self._agent_id,
                    node_id=node_id,
                    node_type=node_type,
                    degree=degree,
                    rank=rank,
                ))
                await session.flush()
        except Exception:
            logger.warning(
                "F065: failed to record hub snapshot for %s (agent=%s)",
                node_id, self._agent_id,
            )

    async def prune_older_than(
        self,
        days: int,
        session: AsyncSession | None = None,
    ) -> int:
        """DELETE rows older than `days`. Returns deleted-row count.

        Called from the sleep handler with NOUS_GRAPH_HUB_SNAPSHOT_RETENTION_DAYS.
        """
        if days <= 0:
            return 0
        cutoff = datetime.now(UTC) - timedelta(days=days)
        try:
            if session is None:
                async with self._db.session() as s:
                    result = await s.execute(
                        text(
                            "DELETE FROM brain.graph_hub_snapshots "
                            "WHERE agent_id = :agent_id "
                            "AND captured_at < :cutoff"
                        ),
                        {"agent_id": self._agent_id, "cutoff": cutoff},
                    )
                    await s.commit()
                    return int(result.rowcount or 0)
            else:
                result = await session.execute(
                    text(
                        "DELETE FROM brain.graph_hub_snapshots "
                        "WHERE agent_id = :agent_id "
                        "AND captured_at < :cutoff"
                    ),
                    {"agent_id": self._agent_id, "cutoff": cutoff},
                )
                return int(result.rowcount or 0)
        except Exception:
            logger.warning(
                "F065: failed to prune hub snapshots (agent=%s, days=%d)",
                self._agent_id, days,
            )
            return 0


# ---------------------------------------------------------------------------
# Rank-shift detection (pure logic, no DB)
# ---------------------------------------------------------------------------


def detect_rank_shifts(
    live_top_hubs: list[dict],
    prior_snapshots: dict[UUID, GraphHubSnapshot],
    top_n: int,
) -> tuple[list[dict], list[UUID]]:
    """Identify nodes that crossed the top-N boundary since the last snapshot.

    Args:
        live_top_hubs: result of Brain.top_hubs(limit=top_n). Each dict has
                      node_id (str), node_type, label, degree, breakdown.
        prior_snapshots: most-recent snapshot per node_id (UUID-keyed).
        top_n: the threshold (typically 10).

    Returns:
        - notices: a list of dicts for the working-memory hub-shift block
          (each carries 'kind': 'entered'|'left', 'label', 'rank', 'degree').
        - new_nodes: UUIDs of hubs with no prior snapshot at all — caller
          MUST insert baselines for these and NOT emit notices.
    """
    notices: list[dict] = []
    new_nodes: list[UUID] = []

    live_by_uuid: dict[UUID, dict] = {}
    for i, h in enumerate(live_top_hubs):
        uid = UUID(h["node_id"])
        live_by_uuid[uid] = {**h, "rank": i + 1}

    # ENTERED: in live top-N but not in prior top-N (or no prior at all).
    for uid, h in live_by_uuid.items():
        if uid not in prior_snapshots:
            new_nodes.append(uid)
            # Silent baseline — no notice on first sight.
            continue
        prior = prior_snapshots[uid]
        prior_rank = prior.rank
        if prior_rank is None or prior_rank > top_n:
            # Wasn't in the top-N before, is now.
            notices.append({
                "kind": "entered",
                "node_id": uid,
                "label": h["label"],
                "rank": h["rank"],
                "degree": h["degree"],
            })

    # LEFT: in prior top-N but not in live top-N.
    for uid, prior in prior_snapshots.items():
        if prior.rank is None or prior.rank > top_n:
            continue
        if uid not in live_by_uuid:
            # Was a hub, no longer in the top-N.
            notices.append({
                "kind": "left",
                "node_id": uid,
                "label": f"[{prior.node_type}] {uid}",
                "rank": None,
                "degree": prior.degree,
            })

    return notices, new_nodes


def format_hub_shift_block(notices: list[dict]) -> str:
    """Render hub-shift notices for injection into the system prompt.

    Returns an empty string when no notices fired (caller can skip the
    section header entirely).
    """
    if not notices:
        return ""
    lines = []
    for n in notices:
        if n["kind"] == "entered":
            lines.append(
                f"[graph] Hub shift: \"{n['label'][:80]}\" entered the "
                f"top-10 (rank #{n['rank']}, degree {n['degree']})."
            )
        elif n["kind"] == "left":
            lines.append(
                f"[graph] Hub shift: {n['label'][:80]} left the top-10 "
                f"(was degree {n['degree']})."
            )
    return "\n".join(lines)
