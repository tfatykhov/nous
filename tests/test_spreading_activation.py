"""Tests for F022 Phase 4 — spreading activation."""
import pytest
import pytest_asyncio

from nous.brain.brain import Brain
from nous.brain.schemas import ReasonInput
from nous.brain.spreading_activation import (
    compute_graph_density,
    should_use_spreading_activation,
    spreading_activation_search,
)
from nous.config import Settings


def _reasons():
    """Provide default reasons to pass noise decision filter."""
    return [ReasonInput(type="analysis", text="Test decision for graph density")]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def brain(db, settings):
    """Brain without embeddings (keyword-only mode)."""
    b = Brain(database=db, settings=settings)
    yield b
    await b.close()


# ---------------------------------------------------------------------------
# Density gate (pure logic, no DB)
# ---------------------------------------------------------------------------


class TestDensityGate:
    def test_force_on(self):
        s = Settings(spreading_activation_enabled="true")
        assert should_use_spreading_activation(s, 0.0) is True

    def test_force_off(self):
        s = Settings(spreading_activation_enabled="false")
        assert should_use_spreading_activation(s, 100.0) is False

    def test_auto_below_threshold(self):
        s = Settings(spreading_activation_enabled="auto", spreading_activation_density_threshold=3.0)
        assert should_use_spreading_activation(s, 2.5) is False

    def test_auto_above_threshold(self):
        s = Settings(spreading_activation_enabled="auto", spreading_activation_density_threshold=3.0)
        assert should_use_spreading_activation(s, 3.5) is True

    def test_auto_at_exact_threshold(self):
        s = Settings(spreading_activation_enabled="auto", spreading_activation_density_threshold=3.0)
        assert should_use_spreading_activation(s, 3.0) is True


# ---------------------------------------------------------------------------
# DB-backed tests
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_density_zero_when_empty(session):
    """Empty graph has density 0."""
    density = await compute_graph_density(session, "nonexistent-agent")
    assert density == 0.0


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_density_with_edges(brain, session):
    """Density = edges / unique_nodes."""
    from nous.brain.schemas import RecordInput

    def _input(desc):
        return RecordInput(description=desc, confidence=0.8, category="architecture", stakes="low", reasons=_reasons())

    d1 = await brain.record(_input("Density A"), session=session)
    d2 = await brain.record(_input("Density B"), session=session)
    d3 = await brain.record(_input("Density C"), session=session)

    await brain.link(d1.id, d2.id, "supports", session=session)
    await brain.link(d2.id, d3.id, "related_to", session=session)
    await brain.link(d1.id, d3.id, "caused_by", session=session)

    density = await compute_graph_density(session, brain.agent_id)
    # 3 edges, 3 unique nodes => density = 1.0
    assert density == pytest.approx(1.0, abs=0.1)


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_density_excludes_comention_edges(brain, session):
    """F076 (codex P2): co_mention edges must NOT inflate the spreading-activation
    density gate — the default-on builder must not flip auto-mode retrieval on."""
    from sqlalchemy import text

    from nous.brain.schemas import RecordInput

    def _input(desc):
        return RecordInput(description=desc, confidence=0.8, category="architecture", stakes="low", reasons=_reasons())

    d1 = await brain.record(_input("Comention density A"), session=session)
    d2 = await brain.record(_input("Comention density B"), session=session)
    d3 = await brain.record(_input("Comention density C"), session=session)
    await brain.link(d1.id, d2.id, "supports", session=session)
    await brain.link(d2.id, d3.id, "related_to", session=session)
    await brain.link(d1.id, d3.id, "caused_by", session=session)

    # A co_mention edge between existing nodes would push edge_count 3 -> 4
    # (density 1.0 -> 1.33) if it were counted. It must be excluded.
    await session.execute(
        text(
            "INSERT INTO brain.graph_edges (source_id,target_id,source_type,target_type,"
            "agent_id,relation,weight,auto_linked,extraction_method) "
            "VALUES (:s,:t,'decision','decision',:a,'related_to',0.80,true,'co_mention')"
        ),
        {"s": str(d1.id), "t": str(d3.id), "a": brain.agent_id},
    )
    await session.flush()

    density = await compute_graph_density(session, brain.agent_id)
    # co_mention edge excluded => still 3 edges / 3 nodes = 1.0
    assert density == pytest.approx(1.0, abs=0.1)


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_spreading_activation_empty_seeds(session):
    """Empty seed list returns empty results."""
    settings = Settings()
    results = await spreading_activation_search(session, "test-agent", [], settings)
    assert results == []


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_spreading_activation_returns_seeds(brain, session):
    """Spreading activation returns at least the seed nodes."""
    from nous.brain.schemas import RecordInput

    d1 = await brain.record(
        RecordInput(
            description="Spreading activation test decision for graph traversal",
            confidence=0.8, category="architecture", stakes="low",
            reasons=_reasons(),
        ),
        session=session,
    )

    settings = Settings()
    results = await spreading_activation_search(
        session,
        brain.agent_id,
        [(d1.id, "decision", 0.9)],
        settings,
    )
    assert len(results) >= 1
    ids = [r[0] for r in results]
    assert d1.id in ids
