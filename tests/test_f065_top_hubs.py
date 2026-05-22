"""F065 Commit D: tests for Brain.top_hubs and the recall_hubs tool.

These tests use the standard test fixtures (db + session). They run
on the project test backend (SQLite for unit tests, Postgres when
NOUS_TEST_DB=postgres).
"""

from __future__ import annotations

import uuid

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from nous.brain.brain import Brain
from nous.config import Settings
from nous.storage.models import Decision, Fact, GraphEdge


@pytest_asyncio.fixture
async def brain_factory(db):
    """Build a Brain instance bound to a unique test agent_id."""
    def _make(agent_id: str) -> Brain:
        s = Settings().model_copy(update={"agent_id": agent_id})
        return Brain(database=db, settings=s)
    return _make


def _make_decision_id() -> uuid.UUID:
    return uuid.uuid4()


async def _seed_decision(
    session: AsyncSession,
    *,
    agent_id: str,
    description: str = "x",
) -> uuid.UUID:
    d = Decision(
        agent_id=agent_id,
        description=description,
        category="process",  # valid per ck_decisions_category
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
    source_type: str = "decision",
    target_type: str = "decision",
) -> None:
    edge = GraphEdge(
        agent_id=agent_id,
        source_id=source_id,
        target_id=target_id,
        source_type=source_type,
        target_type=target_type,
        relation=relation,
        weight=1.0,
        auto_linked=True,
        extraction_method=extraction_method,
    )
    session.add(edge)
    await session.flush()


class TestTopHubsOrdering:
    async def test_returns_top_n_by_degree(
        self, session: AsyncSession, brain_factory
    ) -> None:
        agent_id = f"f065-tophubs-{uuid.uuid4().hex[:6]}"

        # Build a star: hub_a has 5 outbound edges, hub_b has 3, hub_c has 1.
        hub_a = await _seed_decision(session, agent_id=agent_id, description="hub A")
        hub_b = await _seed_decision(session, agent_id=agent_id, description="hub B")
        hub_c = await _seed_decision(session, agent_id=agent_id, description="hub C")
        for _ in range(5):
            leaf = await _seed_decision(session, agent_id=agent_id)
            await _seed_edge(session, agent_id=agent_id, source_id=hub_a, target_id=leaf)
        for _ in range(3):
            leaf = await _seed_decision(session, agent_id=agent_id)
            await _seed_edge(session, agent_id=agent_id, source_id=hub_b, target_id=leaf)
        leaf = await _seed_decision(session, agent_id=agent_id)
        await _seed_edge(session, agent_id=agent_id, source_id=hub_c, target_id=leaf)

        # Construct a Brain instance — its db.session() will use the same DB.
        brain = brain_factory(agent_id)
        # Pass our existing session so we don't open a second connection.
        hubs = await brain.top_hubs(limit=3, session=session)

        # The three hub decisions should dominate by degree. (Leaves
        # have degree=1 each but there are 9 of them; they share that
        # degree so the top 3 are deterministically the hubs.)
        labels = {h["label"] for h in hubs}
        # hub_a (degree 5) and hub_b (degree 3) MUST be in top 3.
        assert "hub A" in labels
        assert "hub B" in labels
        # The third slot is hub_c OR a leaf — both have degree 1. Both
        # acceptable; we just confirm ordering by degree.
        assert hubs[0]["label"] == "hub A"
        assert hubs[1]["label"] == "hub B"
        assert hubs[0]["degree"] >= hubs[1]["degree"] >= hubs[2]["degree"]

    async def test_empty_graph_returns_empty_list(
        self, session: AsyncSession, brain_factory
    ) -> None:
        agent_id = f"f065-empty-{uuid.uuid4().hex[:6]}"
        brain = brain_factory(agent_id)
        hubs = await brain.top_hubs(limit=10, session=session)
        assert hubs == []


class TestTopHubsNodeTypeFilter:
    async def test_filter_decision_only(
        self, session: AsyncSession, brain_factory
    ) -> None:
        agent_id = f"f065-typefilter-{uuid.uuid4().hex[:6]}"

        # One decision hub with degree 3, one fact-only hub with degree 4.
        dec_hub = await _seed_decision(session, agent_id=agent_id, description="decision hub")
        for _ in range(3):
            leaf = await _seed_decision(session, agent_id=agent_id)
            await _seed_edge(session, agent_id=agent_id, source_id=dec_hub, target_id=leaf)

        fact_hub_id = uuid.uuid4()
        f = Fact(
            id=fact_hub_id, agent_id=agent_id, subject="fact hub", content="fact body content"
        )
        session.add(f)
        await session.flush()
        for _ in range(4):
            f_id = uuid.uuid4()
            session.add(Fact(id=f_id, agent_id=agent_id, subject="leaf", content="leaf content"))
            await session.flush()
            await _seed_edge(
                session,
                agent_id=agent_id,
                source_id=fact_hub_id,
                target_id=f_id,
                source_type="fact",
                target_type="fact",
            )

        brain = brain_factory(agent_id)

        # Filtered to decisions only — the fact hub (higher degree) must not appear.
        hubs = await brain.top_hubs(limit=5, node_type="decision", session=session)
        labels = [h["label"] for h in hubs]
        assert "decision hub" in labels
        assert "fact hub" not in labels
        for h in hubs:
            assert h["node_type"] == "decision"


class TestExtractionMethodBreakdown:
    async def test_breakdown_counts_by_tier(
        self, session: AsyncSession, brain_factory
    ) -> None:
        agent_id = f"f065-breakdown-{uuid.uuid4().hex[:6]}"

        hub = await _seed_decision(session, agent_id=agent_id, description="breakdown hub")
        # 2 deterministic, 1 inferred, 3 heuristic edges incident to hub.
        for method, n in [("deterministic", 2), ("inferred", 1), ("heuristic", 3)]:
            for _ in range(n):
                leaf = await _seed_decision(session, agent_id=agent_id)
                await _seed_edge(
                    session,
                    agent_id=agent_id,
                    source_id=hub,
                    target_id=leaf,
                    extraction_method=method,
                )

        brain = brain_factory(agent_id)
        hubs = await brain.top_hubs(limit=1, session=session)
        assert len(hubs) == 1
        bk = hubs[0]["extraction_method_breakdown"]
        assert bk == {"deterministic": 2, "heuristic": 3, "inferred": 1}


class TestLabelFallback:
    async def test_orphan_node_falls_back_to_typed_uuid_label(
        self, session: AsyncSession, brain_factory
    ) -> None:
        """If a hub's node_id has no corresponding row in the Decision/
        Fact/Episode/Procedure table (e.g. soft-deleted), the label
        falls back to '[<type>] <uuid>' (matches _neighbors's pattern)."""
        agent_id = f"f065-orphan-{uuid.uuid4().hex[:6]}"

        # Create edges to a non-existent decision UUID.
        orphan_id = uuid.uuid4()
        for _ in range(2):
            leaf = await _seed_decision(session, agent_id=agent_id)
            await _seed_edge(session, agent_id=agent_id, source_id=orphan_id, target_id=leaf)

        brain = brain_factory(agent_id)
        hubs = await brain.top_hubs(limit=5, session=session)
        orphan_row = next((h for h in hubs if h["node_id"] == str(orphan_id)), None)
        assert orphan_row is not None
        # Fallback format
        assert orphan_row["label"].startswith("[decision] ")
