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


async def test_density_excludes_supersedes_edges(brain, session):
    """2026-06-13 audit: supersedes edges must NOT inflate the density gate —
    traversal refuses to follow them, so hundreds of backfilled lineage edges
    must not flip auto-mode spreading on."""
    from sqlalchemy import text

    from nous.brain.schemas import RecordInput

    def _input(desc):
        return RecordInput(description=desc, confidence=0.8, category="architecture",
                           stakes="low", reasons=_reasons())

    d1 = await brain.record(_input("Supersedes density A"), session=session)
    d2 = await brain.record(_input("Supersedes density B"), session=session)
    d3 = await brain.record(_input("Supersedes density C"), session=session)
    await brain.link(d1.id, d2.id, "supports", session=session)
    await brain.link(d2.id, d3.id, "related_to", session=session)
    await brain.link(d1.id, d3.id, "caused_by", session=session)

    # A supersedes edge would push edge_count 3 -> 4 if counted. Must be excluded.
    await session.execute(
        text(
            "INSERT INTO brain.graph_edges (source_id,target_id,source_type,target_type,"
            "agent_id,relation,weight,auto_linked,extraction_method) "
            "VALUES (:s,:t,'decision','decision',:a,'supersedes',1.0,true,'deterministic')"
        ),
        {"s": str(d1.id), "t": str(d3.id), "a": brain.agent_id},
    )
    await session.flush()

    density = await compute_graph_density(session, brain.agent_id)
    # supersedes edge excluded => still 3 edges / 3 nodes = 1.0
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


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_spreading_excludes_comention_edges(brain, session):
    """F076 (codex P2-G): spreading activation must NOT traverse co_mention edges.
    They are a Path-A consumer, not a spreading one; otherwise the default-on
    co_mention builder changes decision retrieval the moment spreading is enabled."""
    from sqlalchemy import text

    from nous.brain.schemas import RecordInput

    def _inp(d):
        return RecordInput(description=d, confidence=0.8, category="architecture",
                           stakes="low", reasons=_reasons())

    a = await brain.record(_inp("Spreading seed decision alpha node here"), session=session)
    b = await brain.record(_inp("Decision beta reached by a normal related edge"), session=session)
    c = await brain.record(_inp("Decision gamma reached only by a co_mention edge"), session=session)

    async def _edge(s_id, t_id, method):
        await session.execute(text(
            "INSERT INTO brain.graph_edges (source_id,target_id,source_type,target_type,"
            "agent_id,relation,weight,auto_linked,extraction_method) "
            "VALUES (:s,:t,'decision','decision',:a,'related_to',1.0,true,:m)"),
            {"s": str(s_id), "t": str(t_id), "a": brain.agent_id, "m": method})

    await _edge(a.id, b.id, "deterministic")  # non-co_mention -> traversed
    await _edge(a.id, c.id, "co_mention")     # co_mention -> must be skipped
    await session.flush()

    settings = Settings()  # default spreading_activation_max_depth=2
    activated = await spreading_activation_search(
        session, brain.agent_id, [(a.id, "decision", 1.0)], settings,
    )
    ids = {r[0] for r in activated}
    assert b.id in ids, "a normal edge must be traversed by spreading activation"
    assert c.id not in ids, "a co_mention edge must NOT be traversed by spreading activation"


async def test_spreading_excludes_supersedes_edges(brain, session):
    """2026-06-13 audit: spreading must NOT traverse supersedes edges — they
    bridge an active fact to its superseded (inactive) predecessor, so traversal
    would resurface obsolete facts once the supersedes-edge backfill runs."""
    from sqlalchemy import text

    from nous.brain.schemas import RecordInput

    def _inp(d):
        return RecordInput(description=d, confidence=0.8, category="architecture",
                           stakes="low", reasons=_reasons())

    a = await brain.record(_inp("Spreading seed decision for supersedes test"), session=session)
    b = await brain.record(_inp("Decision reached by a normal related edge here"), session=session)
    c = await brain.record(_inp("Decision reachable only via a supersedes edge"), session=session)

    async def _edge(s_id, t_id, relation):
        await session.execute(text(
            "INSERT INTO brain.graph_edges (source_id,target_id,source_type,target_type,"
            "agent_id,relation,weight,auto_linked,extraction_method) "
            "VALUES (:s,:t,'decision','decision',:a,:r,1.0,true,'deterministic')"),
            {"s": str(s_id), "t": str(t_id), "a": brain.agent_id, "r": relation})

    await _edge(a.id, b.id, "related_to")   # traversed
    await _edge(a.id, c.id, "supersedes")   # must be skipped
    await session.flush()

    settings = Settings()
    activated = await spreading_activation_search(
        session, brain.agent_id, [(a.id, "decision", 1.0)], settings,
    )
    ids = {r[0] for r in activated}
    assert b.id in ids, "a normal edge must be traversed"
    assert c.id not in ids, "a supersedes edge must NOT be traversed by spreading activation"
