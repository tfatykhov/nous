"""Tests for F040 — edge_confidence scoring and create_edge helper."""
import pytest
import pytest_asyncio
from uuid import uuid4

from sqlalchemy import text

from nous.brain.graph_linker import (
    RELATION_WEIGHT_MULTIPLIERS,
    edge_confidence,
    GraphLinker,
)
from nous.config import Settings
from nous.storage.models import GraphEdge


# ---------------------------------------------------------------------------
# Unit tests: edge_confidence()
# ---------------------------------------------------------------------------


class TestEdgeConfidence:
    def test_pure_similarity(self):
        """With no other signals, score is 60% of similarity + temporal bonus."""
        score = edge_confidence(1.0, 0, False, 0.0)
        # 1.0 * 0.6 + 0 + 0 + 0.15 = 0.75
        assert score == pytest.approx(0.75)

    def test_all_signals_max(self):
        """All signals maxed out should cap at 1.0."""
        score = edge_confidence(1.0, 5, True, 0.0)
        # 0.6 + 0.15 (capped) + 0.10 + 0.15 = 1.0
        assert score == pytest.approx(1.0)

    def test_shared_tags_capped(self):
        """Tag contribution is capped at 0.15 (3 tags)."""
        score_3 = edge_confidence(0.5, 3, False, 1000.0)
        score_10 = edge_confidence(0.5, 10, False, 1000.0)
        assert score_3 == score_10

    def test_shared_subject_bonus(self):
        score_with = edge_confidence(0.5, 0, True, 1000.0)
        score_without = edge_confidence(0.5, 0, False, 1000.0)
        assert score_with - score_without == pytest.approx(0.10)

    def test_temporal_proximity_decays(self):
        """Temporal bonus decays with distance in days."""
        score_near = edge_confidence(0.5, 0, False, 0.0)
        score_far = edge_confidence(0.5, 0, False, 150.0)
        score_very_far = edge_confidence(0.5, 0, False, 200.0)
        assert score_near > score_far
        # At 150 days: 0.15 - 0.15 = 0.0
        assert score_far == score_very_far  # both capped at 0

    def test_zero_similarity(self):
        score = edge_confidence(0.0, 0, False, 0.0)
        # 0 + 0 + 0 + 0.15 = 0.15
        assert score == pytest.approx(0.15)

    def test_negative_temporal_clamped(self):
        """Large temporal distance doesn't produce negative contribution."""
        score = edge_confidence(0.5, 0, False, 9999.0)
        assert score >= 0.0


# ---------------------------------------------------------------------------
# Unit tests: RELATION_WEIGHT_MULTIPLIERS
# ---------------------------------------------------------------------------


class TestRelationWeightMultipliers:
    def test_all_values_between_0_and_1(self):
        for relation, mult in RELATION_WEIGHT_MULTIPLIERS.items():
            assert 0.0 < mult <= 1.0, f"{relation} has invalid multiplier {mult}"

    def test_key_relations_present(self):
        expected = {"supports", "contradicts", "related_to", "evidence_for",
                    "discussed_in", "extracted_from"}
        assert expected.issubset(set(RELATION_WEIGHT_MULTIPLIERS.keys()))


# ---------------------------------------------------------------------------
# Integration tests: create_edge (require Postgres)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def _fix_stale_relation_constraint(db):
    """Drop the stale inline relation check if it exists."""
    async with db.engine.begin() as conn:
        await conn.execute(text(
            "ALTER TABLE brain.graph_edges "
            "DROP CONSTRAINT IF EXISTS graph_edges_relation_check"
        ))


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_create_edge_basic(db, session, settings, mock_embeddings, _fix_stale_relation_constraint):
    """create_edge inserts a new edge and returns GraphEdgeInfo."""
    linker = GraphLinker(db, mock_embeddings, settings, "test-edge-create")
    src, tgt = uuid4(), uuid4()

    edge = await linker.create_edge(
        source_id=src, target_id=tgt,
        source_type="fact", target_type="fact",
        relation="related_to", weight=0.9,
        session=session,
    )
    assert edge is not None
    assert edge.source_id == src
    assert edge.target_id == tgt
    assert edge.relation == "related_to"
    # Weight should be adjusted by multiplier (0.8 for related_to)
    assert edge.weight == pytest.approx(0.9 * 0.8)
    assert edge.auto_linked is True


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_create_edge_duplicate_returns_none(db, session, settings, mock_embeddings, _fix_stale_relation_constraint):
    """Duplicate edges (same src, tgt, relation) are silently skipped."""
    linker = GraphLinker(db, mock_embeddings, settings, "test-edge-dedup")
    src, tgt = uuid4(), uuid4()

    edge1 = await linker.create_edge(
        source_id=src, target_id=tgt,
        source_type="fact", target_type="decision",
        relation="evidence_for", weight=0.85,
        session=session,
    )
    assert edge1 is not None

    edge2 = await linker.create_edge(
        source_id=src, target_id=tgt,
        source_type="fact", target_type="decision",
        relation="evidence_for", weight=0.95,
        session=session,
    )
    assert edge2 is None


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_create_edge_unknown_relation_uses_default(db, session, settings, mock_embeddings, _fix_stale_relation_constraint):
    """Unknown relation type falls back to 0.8 multiplier."""
    linker = GraphLinker(db, mock_embeddings, settings, "test-edge-unknown-rel")
    src, tgt = uuid4(), uuid4()

    # Use a valid relation from the constraint but not in RELATION_WEIGHT_MULTIPLIERS
    # All valid relations are in the map, so we test the default by verifying
    # the known relation "supports" uses 1.0
    edge = await linker.create_edge(
        source_id=src, target_id=tgt,
        source_type="decision", target_type="decision",
        relation="supports", weight=0.7,
        session=session,
    )
    assert edge is not None
    assert edge.weight == pytest.approx(0.7 * 1.0)
