"""F065 Phase 2: pre_turn hub-shift autosurface + sleep_handler prune.

Tests the integration of HubSnapshotManager + detect_rank_shifts into
CognitiveLayer.pre_turn, and the prune step in SleepHandler.

The autosurface block injection is the new behavior; everything else
(retrieval, censors, frame selection) must remain byte-identical.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from nous.brain.brain import Brain
from nous.brain.hub_snapshots import HubSnapshotManager
from nous.config import Settings
from nous.storage.models import Decision, GraphEdge, GraphHubSnapshot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_decision(session: AsyncSession, agent_id: str, label: str) -> uuid.UUID:
    d = Decision(
        agent_id=agent_id,
        description=label,
        category="process",
        stakes="low",
        confidence=0.8,
    )
    session.add(d)
    await session.flush()
    return d.id


async def _seed_edge(
    session: AsyncSession,
    *,
    agent_id: str,
    source_id: uuid.UUID,
    target_id: uuid.UUID,
    relation: str = "related_to",
    extraction_method: str = "heuristic",
) -> None:
    session.add(GraphEdge(
        agent_id=agent_id,
        source_id=source_id,
        target_id=target_id,
        source_type="decision",
        target_type="decision",
        relation=relation,
        weight=1.0,
        auto_linked=True,
        extraction_method=extraction_method,
    ))
    await session.flush()


# ---------------------------------------------------------------------------
# _compute_hub_shift_notice (unit-level, no full pre_turn)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def cognitive_layer(db):
    """Minimal CognitiveLayer harness — only what _compute_hub_shift_notice needs."""
    from types import SimpleNamespace
    from nous.cognitive.layer import CognitiveLayer

    # CognitiveLayer's full __init__ requires many dependencies. Construct a
    # bare instance and set only the attributes _compute_hub_shift_notice
    # touches: self._brain, self._settings.
    layer = CognitiveLayer.__new__(CognitiveLayer)
    layer._brain = Brain(database=db, settings=Settings())
    layer._settings = Settings()
    return layer


class TestComputeHubShiftNotice:
    async def test_empty_graph_returns_empty_string(
        self, cognitive_layer, db, session: AsyncSession
    ) -> None:
        agent_id = f"f065-p2-empty-{uuid.uuid4().hex[:6]}"
        # Force the Brain to a unique agent_id without re-init.
        cognitive_layer._brain.agent_id = agent_id

        result = await cognitive_layer._compute_hub_shift_notice(agent_id, session=session)
        assert result == ""

    async def test_first_sight_writes_baseline_no_notice(
        self, cognitive_layer, db, session: AsyncSession
    ) -> None:
        """Brand-new hub with no prior snapshot → silent baseline insert."""
        agent_id = f"f065-p2-first-{uuid.uuid4().hex[:6]}"
        cognitive_layer._brain.agent_id = agent_id

        # Seed a hub: one decision with 3 incident edges.
        hub_id = await _seed_decision(session, agent_id, "the hub")
        for _ in range(3):
            leaf = await _seed_decision(session, agent_id, "leaf")
            await _seed_edge(
                session, agent_id=agent_id, source_id=hub_id, target_id=leaf,
            )
        await session.commit()

        result = await cognitive_layer._compute_hub_shift_notice(agent_id, session=session)
        # First sight → no notice fired.
        assert result == ""

        # Baseline snapshot must have been written.
        mgr = HubSnapshotManager(db, agent_id)
        latest = await mgr.get_latest([hub_id], session=session)
        assert hub_id in latest
        assert latest[hub_id].degree == 3

    async def test_rank_shift_emits_notice(
        self, cognitive_layer, db, session: AsyncSession
    ) -> None:
        """Seed a prior snapshot rank #15, then live state places node at
        rank #1 — that's an entered-top-10 transition; expect a notice."""
        agent_id = f"f065-p2-shift-{uuid.uuid4().hex[:6]}"
        cognitive_layer._brain.agent_id = agent_id

        # Seed the hub.
        hub_id = await _seed_decision(session, agent_id, "shifted hub")
        for _ in range(5):
            leaf = await _seed_decision(session, agent_id, "leaf")
            await _seed_edge(
                session, agent_id=agent_id, source_id=hub_id, target_id=leaf,
            )

        # Prior snapshot: rank below top-10.
        session.add(GraphHubSnapshot(
            agent_id=agent_id,
            node_id=hub_id,
            node_type="decision",
            degree=2,
            rank=15,
            captured_at=datetime.now(UTC) - timedelta(days=1),
        ))
        await session.commit()

        result = await cognitive_layer._compute_hub_shift_notice(agent_id, session=session)
        # Notice fires — node entered the top-10 (rank #1 now).
        assert "entered the top-10" in result
        assert "shifted hub" in result

    async def test_left_top_n_emits_notice(
        self, cognitive_layer, db, session: AsyncSession
    ) -> None:
        """A hub previously in the top-N that no longer appears in live
        hubs should fire a 'left the top-10' notice. Codex P1 (2026-05-22)
        regression — get_latest alone biased prior to the live set, so
        'left' could never fire.
        """
        agent_id = f"f065-p2-left-{uuid.uuid4().hex[:6]}"
        cognitive_layer._brain.agent_id = agent_id

        # Seed one live hub so live_top_hubs is non-empty (else early return).
        live_hub_id = await _seed_decision(session, agent_id, "still hubby")
        for _ in range(3):
            leaf = await _seed_decision(session, agent_id, "leaf")
            await _seed_edge(
                session, agent_id=agent_id, source_id=live_hub_id, target_id=leaf,
            )

        # Baseline snapshot for the live hub (so it doesn't fire 'entered').
        session.add(GraphHubSnapshot(
            agent_id=agent_id,
            node_id=live_hub_id,
            node_type="decision",
            degree=3,
            rank=1,
            captured_at=datetime.now(UTC) - timedelta(hours=1),
        ))

        # Prior top-N snapshot for a node that no longer appears in the
        # live graph at all — should fire 'left'.
        gone_id = uuid.uuid4()
        session.add(GraphHubSnapshot(
            agent_id=agent_id,
            node_id=gone_id,
            node_type="decision",
            degree=5,
            rank=3,
            captured_at=datetime.now(UTC) - timedelta(hours=1),
        ))
        await session.commit()

        result = await cognitive_layer._compute_hub_shift_notice(agent_id, session=session)
        assert "left the top-10" in result

    async def test_left_top_n_persisted_no_repeat_notice(
        self, cognitive_layer, db, session: AsyncSession
    ) -> None:
        """Codex P1 follow-up (2026-05-22): the first pre_turn after a hub
        drops out emits a 'left' notice and persists a rank=None
        snapshot. The second pre_turn must NOT re-emit — otherwise the
        same notice spams the system prompt every turn until retention
        pruning eventually deletes the stale in-top-N row.
        """
        from sqlalchemy import select as _select

        agent_id = f"f065-p2-leftspam-{uuid.uuid4().hex[:6]}"
        cognitive_layer._brain.agent_id = agent_id

        live_hub_id = await _seed_decision(session, agent_id, "still hubby")
        for _ in range(3):
            leaf = await _seed_decision(session, agent_id, "leaf")
            await _seed_edge(
                session, agent_id=agent_id, source_id=live_hub_id, target_id=leaf,
            )
        session.add(GraphHubSnapshot(
            agent_id=agent_id,
            node_id=live_hub_id,
            node_type="decision",
            degree=3,
            rank=1,
            captured_at=datetime.now(UTC) - timedelta(hours=1),
        ))

        gone_id = uuid.uuid4()
        session.add(GraphHubSnapshot(
            agent_id=agent_id,
            node_id=gone_id,
            node_type="decision",
            degree=5,
            rank=3,
            captured_at=datetime.now(UTC) - timedelta(hours=1),
        ))
        await session.commit()

        first = await cognitive_layer._compute_hub_shift_notice(agent_id, session=session)
        assert "left the top-10" in first

        # A rank=None snapshot for gone_id must now exist.
        rows = (await session.execute(
            _select(GraphHubSnapshot).where(
                GraphHubSnapshot.node_id == gone_id,
                GraphHubSnapshot.rank.is_(None),
            )
        )).scalars().all()
        assert len(rows) == 1, (
            "expected one rank=NULL snapshot persisted for the 'left' transition"
        )

        second = await cognitive_layer._compute_hub_shift_notice(agent_id, session=session)
        assert "left the top-10" not in second, (
            "second pre_turn must not re-emit the same 'left' notice — "
            "the rank=None snapshot should suppress it via get_latest_top_n"
        )

    async def test_autosurface_disabled_path_short_circuits(
        self, cognitive_layer, db, session: AsyncSession
    ) -> None:
        """The pre_turn caller short-circuits on the settings flag, but
        even if _compute_hub_shift_notice is called directly with empty
        graph, the empty-string return is the no-op behavior."""
        agent_id = f"f065-p2-disabled-{uuid.uuid4().hex[:6]}"
        cognitive_layer._brain.agent_id = agent_id
        # Even with autosurface flag off, the method itself is callable
        # and returns "" gracefully when graph is empty.
        result = await cognitive_layer._compute_hub_shift_notice(agent_id, session=session)
        assert result == ""


# ---------------------------------------------------------------------------
# Sleep handler prune
# ---------------------------------------------------------------------------


class TestSleepHandlerPrune:
    """The actual prune SQL is covered by
    tests/test_f065_hub_shift.py::test_prune_older_than, and the phase
    invocation is covered by test_event_bus.py +
    test_sleep_handler.py (both updated to include
    `prune_hub_snapshots` in phases_completed). Integration testing
    here would require the phase to share a session with the test
    fixture, which the production code intentionally doesn't do.
    Instead we cover the no-op short-circuit path which doesn't touch
    the DB."""

    async def test_phase_respects_retention_disabled(self, db) -> None:
        """Retention 0 → phase returns True without invoking the manager."""
        from types import SimpleNamespace
        from nous.handlers.sleep_handler import SleepHandler

        handler = SleepHandler.__new__(SleepHandler)
        handler._settings = Settings().model_copy(update={
            "agent_id": "f065-disable-prune",
            "graph_hub_snapshot_retention_days": 0,
        })
        handler._heart = SimpleNamespace(db=db)

        stats: dict = {}
        ok = await handler._phase_prune_hub_snapshots(stats)
        assert ok is True
        assert "hub_snapshots_pruned" not in stats
