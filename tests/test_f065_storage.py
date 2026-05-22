"""F065 Commit A: tests for the edge-provenance storage + classify() helper.

Covers:
- classify() exhaustive mapping (every existing relation lands in a known tier)
- classify() source='structural' override yields 'deterministic'
- GraphEdge ORM round-trip with extraction_method
- GraphEdge default = 'heuristic' when not specified
- GraphHubSnapshot ORM round-trip
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.brain.edge_provenance import (
    VALID_METHODS,
    ExtractionMethod,
    classify,
)
from nous.storage.models import GraphEdge, GraphHubSnapshot

# Canonical relations from the GraphEdge ck_edges_relation CHECK constraint
# (sql/init.sql / models.py:241-242). If the constraint is ever extended,
# classify() must handle the new value — this test fails loudly until then.
_GRAPH_EDGE_RELATIONS = [
    "supports",
    "contradicts",
    "supersedes",
    "related_to",
    "caused_by",
    "informed_by",
    "evidence_for",
    "discussed_in",
    "extracted_from",
]


class TestClassify:
    def test_valid_methods_constant_matches_check_constraint(self) -> None:
        assert VALID_METHODS == {"deterministic", "heuristic", "inferred"}

    @pytest.mark.parametrize("relation", _GRAPH_EDGE_RELATIONS)
    def test_every_relation_maps_to_a_valid_tier(self, relation: str) -> None:
        result = classify(relation)
        assert result in VALID_METHODS

    def test_supersedes_is_deterministic(self) -> None:
        assert classify("supersedes") == "deterministic"

    def test_contradicts_is_inferred(self) -> None:
        assert classify("contradicts") == "inferred"

    @pytest.mark.parametrize(
        "relation",
        ["related_to", "extracted_from", "discussed_in", "supports", "caused_by", "informed_by", "evidence_for"],
    )
    def test_remaining_relations_are_heuristic(self, relation: str) -> None:
        assert classify(relation) == "heuristic"

    def test_source_structural_override_promotes_to_deterministic(self) -> None:
        # link_episode_deterministic's discussed_in / extracted_from
        # writes carry structural provenance the relation string alone
        # can't express.
        assert classify("extracted_from", source="structural") == "deterministic"
        assert classify("discussed_in", source="structural") == "deterministic"

    def test_source_structural_overrides_inferred(self) -> None:
        # Edge case: structural source wins even when relation would be
        # 'inferred'. This shouldn't happen in production but documents
        # the precedence rule.
        assert classify("contradicts", source="structural") == "deterministic"

    def test_unknown_source_is_ignored(self) -> None:
        # Defense in depth: an unknown source value must NOT silently
        # change behavior. Only 'structural' is recognized.
        assert classify("related_to", source="experimental") == "heuristic"
        assert classify("supersedes", source="experimental") == "deterministic"

    def test_extraction_method_literal_type(self) -> None:
        # Type-level smoke: classify's return type is the ExtractionMethod
        # Literal. mypy enforces; runtime check is sanity.
        result: ExtractionMethod = classify("supersedes")
        assert result == "deterministic"


class TestGraphEdgeStorage:
    async def test_default_extraction_method_is_heuristic(
        self, session: AsyncSession
    ) -> None:
        """A GraphEdge constructed without extraction_method gets the
        DB default ('heuristic'). This is the safety net — the writer
        plumbing in Commit B sets it explicitly at every write site.
        """
        edge = GraphEdge(
            source_id=uuid.uuid4(),
            target_id=uuid.uuid4(),
            source_type="decision",
            target_type="decision",
            agent_id="test-f065-storage",
            relation="related_to",
            weight=1.0,
            auto_linked=True,
        )
        session.add(edge)
        await session.flush()
        assert edge.extraction_method == "heuristic"

    @pytest.mark.parametrize(
        "method", ["deterministic", "heuristic", "inferred"]
    )
    async def test_extraction_method_round_trip(
        self, session: AsyncSession, method: str
    ) -> None:
        edge = GraphEdge(
            source_id=uuid.uuid4(),
            target_id=uuid.uuid4(),
            source_type="decision",
            target_type="decision",
            agent_id="test-f065-storage",
            relation="related_to",
            weight=1.0,
            auto_linked=True,
            extraction_method=method,
        )
        session.add(edge)
        await session.flush()
        edge_id = edge.id

        loaded = (
            await session.execute(select(GraphEdge).where(GraphEdge.id == edge_id))
        ).scalar_one()
        assert loaded.extraction_method == method


class TestGraphHubSnapshotStorage:
    async def test_minimal_snapshot_round_trip(
        self, session: AsyncSession
    ) -> None:
        snap = GraphHubSnapshot(
            agent_id="test-f065-hubs",
            node_id=uuid.uuid4(),
            node_type="decision",
            degree=31,
            rank=4,
        )
        session.add(snap)
        await session.flush()
        sid = snap.id

        loaded = (
            await session.execute(
                select(GraphHubSnapshot).where(GraphHubSnapshot.id == sid)
            )
        ).scalar_one()
        assert loaded.degree == 31
        assert loaded.rank == 4
        assert loaded.node_type == "decision"
        assert loaded.captured_at is not None

    async def test_rank_nullable_for_below_top_n_nodes(
        self, session: AsyncSession
    ) -> None:
        snap = GraphHubSnapshot(
            agent_id="test-f065-hubs",
            node_id=uuid.uuid4(),
            node_type="fact",
            degree=7,
            rank=None,
        )
        session.add(snap)
        await session.flush()
        assert snap.rank is None
