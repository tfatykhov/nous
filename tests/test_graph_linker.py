"""Tests for F022 Phase 2 — cross-type graph linking."""

from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select, text

from nous.brain.brain import Brain
from nous.brain.graph_linker import GraphLinker, common_template_text
from nous.brain.schemas import RecordInput
from nous.config import Settings
from nous.storage.models import GraphEdge

# ---------------------------------------------------------------------------
# Unit tests (no DB required)
# ---------------------------------------------------------------------------


class TestCommonTemplate:
    def test_decision_template(self):
        assert common_template_text("decision", "Use Redis") == "decision: Use Redis"

    def test_fact_template(self):
        assert common_template_text("fact", "Redis has TTL") == "fact: Redis has TTL"

    def test_episode_template(self):
        assert common_template_text("episode", "Discussed caching") == "episode: Discussed caching"

    def test_procedure_template(self):
        assert common_template_text("procedure", "Deploy Redis") == "procedure: Deploy Redis"


class TestCosine:
    def test_identical_vectors(self):
        v = [1.0, 0.0, 0.0]
        assert GraphLinker._cosine_similarity(v, v) == pytest.approx(1.0)

    def test_orthogonal_vectors(self):
        a = [1.0, 0.0]
        b = [0.0, 1.0]
        assert GraphLinker._cosine_similarity(a, b) == pytest.approx(0.0)

    def test_zero_vector(self):
        assert GraphLinker._cosine_similarity([0, 0], [1, 1]) == 0.0


# ---------------------------------------------------------------------------
# Integration tests (require Postgres via docker-compose)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def brain(db, settings):
    """Brain without embeddings (keyword-only mode)."""
    b = Brain(database=db, settings=settings)
    yield b
    await b.close()


@pytest_asyncio.fixture(autouse=True)
async def _fix_stale_relation_constraint(db):
    """Drop the stale inline relation check if it exists.

    The original init.sql creates an unnamed CHECK on relation that only
    allows the original 5 relation types.  Migration 016 added
    ck_edges_relation with the full set but didn't drop the old inline
    constraint (named graph_edges_relation_check by Postgres).  Drop it
    so cross-type relations (discussed_in, extracted_from, etc.) work.
    """
    async with db.engine.begin() as conn:
        await conn.execute(text("ALTER TABLE brain.graph_edges DROP CONSTRAINT IF EXISTS graph_edges_relation_check"))


@pytest.mark.asyncio
async def test_link_episode_deterministic(brain, session):
    """Deterministic episode linking creates discussed_in and extracted_from edges."""
    settings = Settings()
    linker = GraphLinker(brain.db, None, settings, brain.agent_id)

    d1 = await brain.record(
        RecordInput(
            description="Episode link test decision",
            confidence=0.8,
            category="architecture",
            stakes="low",
        ),
        session=session,
    )

    episode_id = uuid4()
    fact_id = uuid4()

    edges = await linker.link_episode_deterministic(
        episode_id=episode_id,
        decision_ids=[d1.id],
        fact_ids=[fact_id],
        session=session,
    )

    assert len(edges) == 2
    relations = {e.relation for e in edges}
    assert "discussed_in" in relations
    assert "extracted_from" in relations

    # Verify in DB
    result = await session.execute(select(GraphEdge).where(GraphEdge.source_id == episode_id))
    db_edges = result.scalars().all()
    assert len(db_edges) == 1  # episode->decision
    assert db_edges[0].relation == "discussed_in"
