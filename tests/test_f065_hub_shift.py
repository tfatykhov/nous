"""F065 Commit E: tests for HubSnapshotManager + rank-shift detection.

Covers:
- detect_rank_shifts() pure logic (no DB):
  - node entered top-N (notice emitted)
  - node left top-N (notice emitted)
  - new node (no prior snapshot) → no notice, baseline insert pending
  - rank shift WITHIN top-N (no boundary cross) → no notice
- HubSnapshotManager round-trip via real DB:
  - record + get_latest
  - prune_older_than
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from nous.brain.hub_snapshots import (
    HubSnapshotManager,
    detect_rank_shifts,
    format_hub_shift_block,
)
from nous.storage.models import GraphHubSnapshot


def _live_hub(node_id: uuid.UUID, label: str, degree: int) -> dict:
    """Build a live top_hubs() return shape (without rank — added by detect)."""
    return {
        "node_id": str(node_id),
        "node_type": "decision",
        "label": label,
        "degree": degree,
        "extraction_method_breakdown": {"deterministic": 0, "heuristic": degree, "inferred": 0},
    }


def _snapshot(node_id: uuid.UUID, rank: int | None, degree: int) -> GraphHubSnapshot:
    return GraphHubSnapshot(
        agent_id="t",
        node_id=node_id,
        node_type="decision",
        degree=degree,
        rank=rank,
        captured_at=datetime.now(UTC),
    )


class TestDetectRankShifts:
    def test_node_entered_top_n_emits_notice(self) -> None:
        uid = uuid.uuid4()
        live = [_live_hub(uid, "hub A", degree=50)]
        prior = {uid: _snapshot(uid, rank=15, degree=20)}  # was below top-10
        notices, new_nodes = detect_rank_shifts(live, prior, top_n=10)
        assert any(n["kind"] == "entered" and n["label"] == "hub A" for n in notices)
        assert new_nodes == []

    def test_node_left_top_n_emits_notice(self) -> None:
        # Live top-10 is just one other node; the prior hub-1 is no longer there.
        other = uuid.uuid4()
        gone = uuid.uuid4()
        live = [_live_hub(other, "still here", degree=40)]
        prior = {
            other: _snapshot(other, rank=2, degree=35),
            gone: _snapshot(gone, rank=1, degree=30),
        }
        notices, _ = detect_rank_shifts(live, prior, top_n=10)
        assert any(n["kind"] == "left" for n in notices)

    def test_new_node_no_notice_baseline_pending(self) -> None:
        """First-sight nodes: silent baseline insert; no notice fired."""
        uid = uuid.uuid4()
        live = [_live_hub(uid, "brand new hub", degree=99)]
        prior = {}  # No prior snapshot for this node
        notices, new_nodes = detect_rank_shifts(live, prior, top_n=10)
        assert notices == []
        assert new_nodes == [uid]

    def test_rank_shift_within_top_n_no_notice(self) -> None:
        """Rank change that doesn't cross the top-N boundary is silent."""
        uid = uuid.uuid4()
        live = [_live_hub(uid, "moved within top-10", degree=42)]
        prior = {uid: _snapshot(uid, rank=3, degree=35)}  # was rank 3, now rank 1
        notices, new_nodes = detect_rank_shifts(live, prior, top_n=10)
        assert notices == []
        assert new_nodes == []

    def test_format_block_renders_notices(self) -> None:
        notices = [
            {"kind": "entered", "label": "X", "rank": 4, "degree": 38},
            {"kind": "left", "label": "[decision] some-uuid", "rank": None, "degree": 20},
        ]
        block = format_hub_shift_block(notices)
        assert "entered the top-10" in block
        assert "left the top-10" in block

    def test_format_block_empty_when_no_notices(self) -> None:
        assert format_hub_shift_block([]) == ""


# ---------------------------------------------------------------------------
# HubSnapshotManager integration tests
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def snapshot_mgr(db):
    return HubSnapshotManager(database=db, agent_id=f"t-hub-{uuid.uuid4().hex[:6]}")


class TestHubSnapshotManager:
    async def test_record_and_get_latest(
        self, session: AsyncSession, db
    ) -> None:
        agent_id = f"t-hub-{uuid.uuid4().hex[:6]}"
        mgr = HubSnapshotManager(database=db, agent_id=agent_id)
        uid = uuid.uuid4()
        await mgr.record_snapshot(uid, "decision", degree=31, rank=4, session=session)
        await session.flush()
        latest = await mgr.get_latest([uid], session=session)
        assert uid in latest
        assert latest[uid].degree == 31
        assert latest[uid].rank == 4

    async def test_get_latest_empty_returns_empty(
        self, session: AsyncSession, db
    ) -> None:
        agent_id = f"t-hub-{uuid.uuid4().hex[:6]}"
        mgr = HubSnapshotManager(database=db, agent_id=agent_id)
        assert await mgr.get_latest([], session=session) == {}

    async def test_get_latest_picks_most_recent_per_node(
        self, session: AsyncSession, db
    ) -> None:
        """Two snapshots for the same node — get_latest returns the later one."""
        agent_id = f"t-hub-{uuid.uuid4().hex[:6]}"
        mgr = HubSnapshotManager(database=db, agent_id=agent_id)
        uid = uuid.uuid4()

        # Insert an older one with explicit captured_at.
        older = GraphHubSnapshot(
            agent_id=agent_id,
            node_id=uid,
            node_type="decision",
            degree=10,
            rank=8,
            captured_at=datetime.now(UTC) - timedelta(days=2),
        )
        session.add(older)
        await session.flush()

        # And a newer one.
        await mgr.record_snapshot(uid, "decision", degree=31, rank=4, session=session)
        await session.flush()

        latest = await mgr.get_latest([uid], session=session)
        # The most recent row wins.
        assert latest[uid].degree == 31

    async def test_prune_older_than(
        self, session: AsyncSession, db
    ) -> None:
        agent_id = f"t-hub-{uuid.uuid4().hex[:6]}"
        mgr = HubSnapshotManager(database=db, agent_id=agent_id)
        old_uid = uuid.uuid4()
        new_uid = uuid.uuid4()

        # An old row (200 days back) and a new one.
        session.add(GraphHubSnapshot(
            agent_id=agent_id,
            node_id=old_uid,
            node_type="decision",
            degree=5,
            rank=10,
            captured_at=datetime.now(UTC) - timedelta(days=200),
        ))
        await mgr.record_snapshot(new_uid, "decision", degree=20, rank=2, session=session)
        await session.flush()

        # Prune older than 90 days.
        deleted = await mgr.prune_older_than(days=90, session=session)
        assert deleted == 1

        # The new row survived.
        latest_new = await mgr.get_latest([new_uid], session=session)
        assert new_uid in latest_new

        # The old row is gone.
        latest_old = await mgr.get_latest([old_uid], session=session)
        assert old_uid not in latest_old

    async def test_record_failure_does_not_propagate(
        self, monkeypatch, db
    ) -> None:
        """record_snapshot swallows DB errors and WARN-logs.
        The pre_turn fire-and-forget caller depends on this contract."""

        class BrokenDb:
            def session(self):
                raise RuntimeError("simulated DB outage")

        mgr = HubSnapshotManager(database=BrokenDb(), agent_id="t-broken")
        # Must NOT raise — diagnostic, not correctness.
        await mgr.record_snapshot(uuid.uuid4(), "decision", degree=5, rank=1)
