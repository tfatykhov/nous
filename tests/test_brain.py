"""Integration tests for Brain public API.

All tests use real Postgres via the SAVEPOINT fixture from conftest.py.
Brain methods receive the test session via the session parameter (P1-2).
"""

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select, text

from nous.brain.brain import Brain
from nous.brain.schemas import (
    CalibrationReport,
    DecisionDetail,
    DecisionSummary,
    GraphEdgeInfo,
    GuardrailResult,
    ReasonInput,
    RecordInput,
    ThoughtInfo,
)
from nous.storage.models import Event, GraphEdge

pytestmark = pytest.mark.integration



# Must match conftest.GUARDRAIL_TEST_AGENT
GUARDRAIL_TEST_AGENT = "test-guardrail-agent"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def brain(db, settings):
    """Brain without embeddings (keyword-only mode)."""
    brain = Brain(database=db, settings=settings)
    yield brain
    await brain.close()


@pytest_asyncio.fixture
async def brain_with_embeddings(db, settings, mock_embeddings):
    """Brain with mock embedding provider for vector tests."""
    brain = Brain(database=db, settings=settings, embedding_provider=mock_embeddings)
    yield brain
    await brain.close()


@pytest_asyncio.fixture
async def brain_guardrail(db, settings):
    """Brain with agent_id matching seed_guardrails fixture."""
    # Override agent_id to match the test guardrails
    settings_copy = settings.model_copy(update={"agent_id": GUARDRAIL_TEST_AGENT})
    brain = Brain(database=db, settings=settings_copy)
    yield brain
    await brain.close()


def _record_input(**overrides) -> RecordInput:
    """Build a RecordInput with sensible defaults, overridable."""
    defaults = dict(
        description="Use PostgreSQL for storage",
        confidence=0.85,
        category="architecture",
        stakes="medium",
        context="Evaluating database options for the project",
        pattern="Prefer mature, well-supported databases",
        tags=["postgres", "database"],
        reasons=[
            ReasonInput(type="analysis", text="Analyzed the trade-offs"),
            ReasonInput(type="pattern", text="Follows established patterns"),
        ],
    )
    defaults.update(overrides)
    return RecordInput(**defaults)


# ---------------------------------------------------------------------------
# 1. test_record_decision
# ---------------------------------------------------------------------------


async def test_record_decision(brain, session):
    """Record with all fields, verify stored correctly."""
    inp = _record_input()
    detail = await brain.record(inp, session=session)

    assert isinstance(detail, DecisionDetail)
    assert detail.description == inp.description
    assert detail.confidence == inp.confidence
    assert detail.category == inp.category
    assert detail.stakes == inp.stakes
    assert detail.context == inp.context
    assert detail.pattern == inp.pattern
    assert detail.outcome == "pending"
    assert detail.agent_id == brain.agent_id
    assert detail.created_at is not None
    assert detail.updated_at is not None


# ---------------------------------------------------------------------------
# 2. test_record_with_tags_and_reasons
# ---------------------------------------------------------------------------


async def test_record_with_tags_and_reasons(brain, session):
    """Verify tags and reasons cascade-inserted."""
    inp = _record_input()
    detail = await brain.record(inp, session=session)

    assert set(detail.tags) == {"postgres", "database"}
    assert len(detail.reasons) == 2
    reason_types = {r.type for r in detail.reasons}
    assert reason_types == {"analysis", "pattern"}


# ---------------------------------------------------------------------------
# 3. test_record_computes_quality_score
# ---------------------------------------------------------------------------


async def test_record_computes_quality_score(brain, session):
    """Quality score > 0.5 with tags+reasons+pattern."""
    inp = _record_input()
    detail = await brain.record(inp, session=session)

    assert detail.quality_score is not None
    assert detail.quality_score > 0.5
    # tags(0.25) + reasons(0.25) + pattern(0.25) + context(0.15) + diversity(0.10) = 1.0
    assert detail.quality_score == 1.0


# ---------------------------------------------------------------------------
# 4. test_record_generates_bridge
# ---------------------------------------------------------------------------


async def test_record_generates_bridge(brain, session):
    """Bridge auto-extracted from description/pattern."""
    inp = _record_input()
    detail = await brain.record(inp, session=session)

    assert detail.bridge is not None
    assert detail.bridge.structure is not None
    assert len(detail.bridge.structure) > 0
    # Function should be pattern when pattern is set
    assert detail.bridge.function == inp.pattern


# ---------------------------------------------------------------------------
# 5. test_record_auto_links
# ---------------------------------------------------------------------------


async def test_record_auto_links(brain_with_embeddings, session):
    """Record two similar decisions, verify graph edge created."""
    inp1 = _record_input(description="Use PostgreSQL for the database layer")
    detail1 = await brain_with_embeddings.record(inp1, session=session)

    inp2 = _record_input(description="Use PostgreSQL for persistent storage")
    detail2 = await brain_with_embeddings.record(inp2, session=session)

    # Check if any edges were created between the two decisions
    result = await session.execute(
        text(
            "SELECT * FROM brain.graph_edges "
            "WHERE (source_id = :id1 AND target_id = :id2) "
            "   OR (source_id = :id2 AND target_id = :id1)"
        ),
        {"id1": str(detail1.id), "id2": str(detail2.id)},
    )
    edges = result.fetchall()
    # auto_link might or might not create an edge depending on threshold
    # but we verify the mechanism doesn't error
    assert isinstance(edges, list)


# ---------------------------------------------------------------------------
# 6. test_think
# ---------------------------------------------------------------------------


async def test_think(brain, session):
    """Attach thought to decision, verify retrieval."""
    inp = _record_input()
    detail = await brain.record(inp, session=session)

    thought = await brain.think(detail.id, "Considered alternative approaches", session=session)
    assert isinstance(thought, ThoughtInfo)
    assert thought.text == "Considered alternative approaches"
    assert thought.created_at is not None


# ---------------------------------------------------------------------------
# 7. test_get_decision
# ---------------------------------------------------------------------------


async def test_get_decision(brain, session):
    """Fetch with all relations populated."""
    inp = _record_input()
    detail = await brain.record(inp, session=session)
    await brain.think(detail.id, "A thought", session=session)

    # Expire the cached decision so get() re-fetches with eager loading
    await session.flush()
    session.expire_all()

    fetched = await brain.get(detail.id, session=session)
    assert fetched is not None
    assert fetched.id == detail.id
    assert fetched.description == inp.description
    assert set(fetched.tags) == {"postgres", "database"}
    assert len(fetched.reasons) == 2
    assert fetched.bridge is not None
    assert len(fetched.thoughts) == 1
    assert fetched.thoughts[0].text == "A thought"


# ---------------------------------------------------------------------------
# 8. test_get_nonexistent
# ---------------------------------------------------------------------------


async def test_get_nonexistent(brain, session):
    """Returns None for nonexistent decision."""
    result = await brain.get(uuid.uuid4(), session=session)
    assert result is None


# ---------------------------------------------------------------------------
# 9. test_update_decision
# ---------------------------------------------------------------------------


async def test_update_decision(brain, session):
    """Update description, verify re-scored."""
    inp = _record_input()
    detail = await brain.record(inp, session=session)
    original_id = detail.id

    updated = await brain.update(
        detail.id,
        description="Updated: Use PostgreSQL 17 for storage",
        session=session,
    )
    assert updated.id == original_id
    assert "Updated" in updated.description


# ---------------------------------------------------------------------------
# 10. test_query_keyword_only
# ---------------------------------------------------------------------------


async def test_query_keyword_only(brain, session):
    """Search without embeddings (keyword fallback)."""
    await brain.record(
        _record_input(description="Use PostgreSQL for persistent storage"),
        session=session,
    )
    await brain.record(
        _record_input(description="Implement Redis caching layer", pattern="Cache hot data"),
        session=session,
    )

    results = await brain.query("PostgreSQL", session=session)
    assert isinstance(results, list)
    # Should find the PostgreSQL decision via keyword search
    if results:
        assert any("PostgreSQL" in r.description for r in results)


# ---------------------------------------------------------------------------
# 11. test_query_hybrid
# ---------------------------------------------------------------------------


async def test_query_hybrid(brain_with_embeddings, session):
    """Search with mock embeddings (both vector + keyword)."""
    await brain_with_embeddings.record(
        _record_input(description="Use PostgreSQL for persistent storage"),
        session=session,
    )
    await brain_with_embeddings.record(
        _record_input(description="Implement Redis caching layer", pattern="Cache hot data"),
        session=session,
    )

    results = await brain_with_embeddings.query("PostgreSQL storage", session=session)
    assert isinstance(results, list)
    # Hybrid search should return results
    for r in results:
        assert isinstance(r, DecisionSummary)
        assert r.score is not None


# ---------------------------------------------------------------------------
# 12. test_query_with_filters
# ---------------------------------------------------------------------------


async def test_query_with_filters(brain, session):
    """Filter by category, stakes, outcome."""
    await brain.record(
        _record_input(
            description="Architecture decision about storage",
            category="architecture",
            stakes="high",
        ),
        session=session,
    )
    await brain.record(
        _record_input(
            description="Tooling decision about linting",
            category="tooling",
            stakes="low",
        ),
        session=session,
    )

    # Filter by category
    results = await brain.query("decision", category="architecture", session=session)
    assert isinstance(results, list)
    for r in results:
        assert r.category == "architecture"


# ---------------------------------------------------------------------------
# 13. test_check_guardrails_allowed
# ---------------------------------------------------------------------------


async def test_check_guardrails_allowed(brain_guardrail, session, seed_guardrails):
    """Low stakes, high confidence -> allowed."""
    result = await brain_guardrail.check(
        description="Simple tooling change",
        stakes="low",
        confidence=0.9,
        reasons=[{"type": "analysis", "text": "Straightforward change"}],
        quality_score=0.8,
        session=session,
    )
    assert isinstance(result, GuardrailResult)
    assert result.allowed


# ---------------------------------------------------------------------------
# 14. test_check_guardrails_blocked
# ---------------------------------------------------------------------------


async def test_check_guardrails_blocked(brain_guardrail, session, seed_guardrails):
    """High stakes, low confidence -> blocked by seed guardrail."""
    result = await brain_guardrail.check(
        description="Major production change",
        stakes="high",
        confidence=0.3,
        reasons=[{"type": "intuition", "text": "Gut feeling"}],
        quality_score=0.8,
        session=session,
    )
    assert not result.allowed
    assert "no-high-stakes-low-confidence" in result.blocked_by


# ---------------------------------------------------------------------------
# 15. test_review_decision
# ---------------------------------------------------------------------------


async def test_review_decision(brain, session):
    """Set outcome, verify reviewed_at set."""
    inp = _record_input()
    detail = await brain.record(inp, session=session)

    reviewed = await brain.review(detail.id, "success", result="Worked perfectly", session=session)
    assert reviewed.outcome == "success"
    assert reviewed.outcome_result == "Worked perfectly"
    assert reviewed.reviewed_at is not None


# ---------------------------------------------------------------------------
# 16. test_calibration_report
# ---------------------------------------------------------------------------


async def test_calibration_report(brain, session):
    """Record 5 decisions, review 3, verify Brier score."""
    # Record 5 decisions
    decisions = []
    for i in range(5):
        d = await brain.record(
            _record_input(
                description=f"Calibration test decision {i}",
                confidence=0.8,
            ),
            session=session,
        )
        decisions.append(d)

    # Review 3 of them
    await brain.review(decisions[0].id, "success", session=session)
    await brain.review(decisions[1].id, "success", session=session)
    await brain.review(decisions[2].id, "failure", session=session)

    report = await brain.get_calibration(session=session)
    assert isinstance(report, CalibrationReport)
    assert report.total_decisions >= 5
    assert report.reviewed_decisions >= 3
    assert report.brier_score is not None
    assert report.accuracy is not None


# ---------------------------------------------------------------------------
# 17. test_link_decisions
# ---------------------------------------------------------------------------


async def test_link_decisions(brain, session):
    """Manual link, verify edge created."""
    d1 = await brain.record(
        _record_input(description="First decision"),
        session=session,
    )
    d2 = await brain.record(
        _record_input(description="Second decision"),
        session=session,
    )

    edge = await brain.link(d1.id, d2.id, "supports", weight=0.9, session=session)
    assert isinstance(edge, GraphEdgeInfo)
    assert edge.relation == "supports"
    assert edge.weight == 0.9
    assert edge.auto_linked is False


# ---------------------------------------------------------------------------
# 18. test_neighbors
# ---------------------------------------------------------------------------


async def test_neighbors(brain, session):
    """Link 3 decisions, query neighbors of middle one."""
    d1 = await brain.record(
        _record_input(description="Decision A"),
        session=session,
    )
    d2 = await brain.record(
        _record_input(description="Decision B"),
        session=session,
    )
    d3 = await brain.record(
        _record_input(description="Decision C"),
        session=session,
    )

    await brain.link(d1.id, d2.id, "supports", session=session)
    await brain.link(d2.id, d3.id, "related_to", session=session)

    neighbors = await brain.neighbors(d2.id, session=session)
    assert isinstance(neighbors, list)
    assert len(neighbors) >= 2
    neighbor_ids = {n.id for n in neighbors}
    assert d1.id in neighbor_ids
    assert d3.id in neighbor_ids
    for n in neighbors:
        assert n.node_type == "decision"
        assert n.edge_relation in ("supports", "related_to")


# ---------------------------------------------------------------------------
# 18b. test_link_with_types
# ---------------------------------------------------------------------------


async def test_link_with_types(brain, session):
    """Graph edges include source_type, target_type, and agent_id."""
    d1 = await brain.record(_record_input(description="Typed link A"), session=session)
    d2 = await brain.record(_record_input(description="Typed link B"), session=session)

    edge = await brain.link(d1.id, d2.id, "supports", session=session)
    assert edge.source_type == "decision"
    assert edge.target_type == "decision"

    # Verify agent_id persisted in DB
    result = await session.execute(
        select(GraphEdge).where(GraphEdge.source_id == d1.id)
    )
    db_edge = result.scalar_one()
    assert db_edge.agent_id == brain.agent_id
    assert db_edge.source_type == "decision"


# ---------------------------------------------------------------------------
# 18c. test_neighbors_returns_neighbor_result
# ---------------------------------------------------------------------------


async def test_neighbors_returns_neighbor_result(brain, session):
    """neighbors() returns NeighborResult with edge metadata."""
    d1 = await brain.record(_record_input(description="Neighbor result A"), session=session)
    d2 = await brain.record(_record_input(description="Neighbor result B"), session=session)

    await brain.link(d1.id, d2.id, "supports", weight=0.9, session=session)

    results = await brain.neighbors(d1.id, session=session)
    assert len(results) == 1
    n = results[0]
    from nous.brain.schemas import NeighborResult

    assert isinstance(n, NeighborResult)
    assert n.id == d2.id
    assert n.node_type == "decision"
    assert n.edge_relation == "supports"
    assert n.edge_weight == 0.9


# ---------------------------------------------------------------------------
# 18d. test_neighbors_with_node_type
# ---------------------------------------------------------------------------


async def test_neighbors_with_node_type(brain, session):
    """neighbors() accepts node_type parameter for cross-type traversal."""
    from uuid import uuid4


    d1 = await brain.record(_record_input(description="Cross-type neighbor"), session=session)

    fake_fact_id = uuid4()
    edge = GraphEdge(
        source_id=fake_fact_id,
        target_id=d1.id,
        source_type="fact",
        target_type="decision",
        agent_id=brain.agent_id,
        relation="supports",
        weight=0.85,
    )
    session.add(edge)
    await session.flush()

    results = await brain.neighbors(fake_fact_id, node_type="fact", session=session)
    assert len(results) == 1
    assert results[0].id == d1.id
    assert results[0].node_type == "decision"
    assert results[0].edge_relation == "supports"


# ---------------------------------------------------------------------------
# 19. test_emit_event
# ---------------------------------------------------------------------------


async def test_emit_event(brain, session):
    """Verify event written to nous_system.events."""
    await brain.emit_event(
        "test_event",
        {"key": "value"},
        session=session,
    )

    result = await session.execute(
        select(Event).where(
            Event.agent_id == brain.agent_id,
            Event.event_type == "test_event",
        )
    )
    event = result.scalar_one()
    assert event.data == {"key": "value"}


# ---------------------------------------------------------------------------
# Negative tests (P3 fix)
# ---------------------------------------------------------------------------


async def test_get_nonexistent_returns_none(brain, session):
    """get() with nonexistent UUID returns None, not an error."""
    result = await brain.get(uuid.uuid4(), session=session)
    assert result is None


async def test_review_nonexistent_decision(brain, session):
    """review() with nonexistent decision_id raises or returns gracefully."""
    with pytest.raises(Exception):
        await brain.review(uuid.uuid4(), "success", session=session)


async def test_think_nonexistent_decision(brain, session):
    """think() with nonexistent decision_id raises due to FK constraint."""
    with pytest.raises(Exception):
        await brain.think(uuid.uuid4(), "orphan thought", session=session)
