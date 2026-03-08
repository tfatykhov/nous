"""Tests for FactManager — semantic memory (what we know).

All tests use real Postgres via the SAVEPOINT fixture from conftest.py.
Heart methods receive the test session via the session parameter (P1-1).
"""

import pytest

from nous.heart import (
    EpisodeInput,
    FactDetail,
    FactInput,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fact_input(**overrides) -> FactInput:
    """Build a FactInput with sensible defaults."""
    defaults = dict(
        content="Python uses indentation for block scoping",
        category="technical",
        subject="python",
        confidence=0.9,
        source="documentation",
        tags=["python", "syntax"],
    )
    defaults.update(overrides)
    return FactInput(**defaults)


# ---------------------------------------------------------------------------
# 1. test_learn_fact
# ---------------------------------------------------------------------------


async def test_learn_fact(heart, session):
    """Basic creation with all fields."""
    inp = _fact_input()
    detail = await heart.learn(inp, session=session)

    assert isinstance(detail, FactDetail)
    assert detail.content == inp.content
    assert detail.category == "technical"
    assert detail.subject == "python"
    assert detail.confidence == 0.9
    assert detail.source == "documentation"
    assert detail.active is True
    assert detail.confirmation_count == 0


# ---------------------------------------------------------------------------
# 2. test_learn_with_provenance
# ---------------------------------------------------------------------------


async def test_learn_with_provenance(heart, session):
    """source_episode_id and source_decision_id set."""
    # Create a real episode to use as provenance
    episode = await heart.start_episode(
        EpisodeInput(summary="Learning session"),
        session=session,
    )

    inp = _fact_input(
        content="Unique fact with provenance for testing",
        source_episode_id=episode.id,
    )
    detail = await heart.learn(inp, session=session)

    assert detail.source_episode_id == episode.id


# ---------------------------------------------------------------------------
# 3. test_confirm_fact
# ---------------------------------------------------------------------------


async def test_confirm_fact(heart, session):
    """confirmation_count increments, last_confirmed updates."""
    inp = _fact_input(content="Fact to confirm for testing")
    detail = await heart.learn(inp, session=session)

    assert detail.confirmation_count == 0
    assert detail.last_confirmed is None

    confirmed = await heart.confirm_fact(detail.id, session=session)
    assert confirmed.confirmation_count == 1
    assert confirmed.last_confirmed is not None

    # Confirm again
    confirmed2 = await heart.confirm_fact(detail.id, session=session)
    assert confirmed2.confirmation_count == 2


# ---------------------------------------------------------------------------
# 4. test_supersede_chain
# ---------------------------------------------------------------------------


async def test_supersede_chain(heart, session):
    """A superseded by B, B by C. A and B inactive, C active."""
    fact_a = await heart.learn(_fact_input(content="Fact A original version"), session=session)
    fact_b = await heart.supersede_fact(
        fact_a.id,
        _fact_input(content="Fact B replaces A"),
        session=session,
    )
    fact_c = await heart.supersede_fact(
        fact_b.id,
        _fact_input(content="Fact C replaces B"),
        session=session,
    )

    # Verify chain: A and B inactive, C active
    a = await heart.get_fact(fact_a.id, session=session)
    assert a.active is False
    assert a.superseded_by == fact_b.id

    b = await heart.get_fact(fact_b.id, session=session)
    assert b.active is False
    assert b.superseded_by == fact_c.id

    c = await heart.get_fact(fact_c.id, session=session)
    assert c.active is True
    assert c.superseded_by is None

    # get_current from A should return C
    current = await heart.get_current_fact(fact_a.id, session=session)
    assert current.id == fact_c.id


# ---------------------------------------------------------------------------
# 5. test_contradict_reduces_confidence
# ---------------------------------------------------------------------------


async def test_contradict_reduces_confidence(heart, session):
    """Original confidence drops by 0.2."""
    original = await heart.learn(
        _fact_input(content="Earth is flat", confidence=0.8),
        session=session,
    )

    contradicting = await heart.contradict_fact(
        original.id,
        _fact_input(content="Earth is round, contradicting flat earth claim"),
        session=session,
    )

    # Re-read the original
    updated_original = await heart.get_fact(original.id, session=session)
    assert updated_original.confidence == pytest.approx(0.6, abs=0.01)

    # New fact should have contradiction_of set
    assert contradicting.contradiction_of == original.id


# ---------------------------------------------------------------------------
# 6. test_contradict_floor_zero
# ---------------------------------------------------------------------------


async def test_contradict_floor_zero(heart, session):
    """Confidence can't go below 0.0."""
    original = await heart.learn(
        _fact_input(content="Very uncertain claim", confidence=0.1),
        session=session,
    )

    await heart.contradict_fact(
        original.id,
        _fact_input(content="Contradicting the uncertain claim"),
        session=session,
    )

    updated = await heart.get_fact(original.id, session=session)
    assert updated.confidence >= 0.0


# ---------------------------------------------------------------------------
# 7. test_search_active_only
# ---------------------------------------------------------------------------


async def test_search_active_only(heart, session):
    """Superseded facts excluded by default."""
    fact_a = await heart.learn(
        _fact_input(content="Active search test original fact"),
        session=session,
    )
    await heart.supersede_fact(
        fact_a.id,
        _fact_input(content="Active search test replacement fact"),
        session=session,
    )

    results = await heart.search_facts("Active search test original fact", session=session)
    # The superseded fact should NOT appear in active-only search
    ids = [r.id for r in results]
    assert fact_a.id not in ids


# ---------------------------------------------------------------------------
# 8. test_search_with_category
# ---------------------------------------------------------------------------


async def test_search_with_category(heart, session):
    """Filter by category."""
    await heart.learn(
        _fact_input(
            content="Category filter test technical fact",
            category="technical",
        ),
        session=session,
    )
    await heart.learn(
        _fact_input(
            content="Category filter test preference fact",
            category="preference",
        ),
        session=session,
    )

    results = await heart.search_facts(
        "Category filter test",
        category="technical",
        session=session,
    )
    for r in results:
        assert r.category == "technical"


# ---------------------------------------------------------------------------
# 9. test_deactivate
# ---------------------------------------------------------------------------


async def test_deactivate(heart, session):
    """Soft delete, search excludes it."""
    fact = await heart.learn(
        _fact_input(content="Deactivation test fact to remove"),
        session=session,
    )

    await heart.deactivate_fact(fact.id, session=session)

    # Should not appear in active search
    results = await heart.search_facts("Deactivation test fact to remove", session=session)
    ids = [r.id for r in results]
    assert fact.id not in ids


# ---------------------------------------------------------------------------
# 10. test_dedup_exclude_ids (P1-2)
# ---------------------------------------------------------------------------


async def test_dedup_exclude_ids(heart, session):
    """Verify exclude_ids prevents supersede/contradict dedup collision."""
    # Learn a fact
    original = await heart.learn(
        _fact_input(content="Exclude IDs dedup test fact"),
        session=session,
    )

    # Supersede with IDENTICAL content — without exclude_ids this would
    # confirm the original instead of creating a new fact
    new_fact = await heart.supersede_fact(
        original.id,
        _fact_input(content="Exclude IDs dedup test fact"),
        session=session,
    )

    # The new fact should be a DIFFERENT fact, not the original confirmed
    assert new_fact.id != original.id

    # Original should be inactive
    updated_original = await heart.get_fact(original.id, session=session)
    assert updated_original.active is False
    assert updated_original.superseded_by == new_fact.id


# ---------------------------------------------------------------------------
# F022: Graph edge bridge tests
# ---------------------------------------------------------------------------


async def test_contradict_creates_graph_edge(heart, session):
    """facts.contradict() also creates a 'contradicts' graph edge."""
    from nous.storage.models import GraphEdge
    from sqlalchemy import select

    f1 = await heart.learn(
        _fact_input(content="Tim prefers Celsius", subject="Tim"),
        session=session,
    )
    f2 = await heart.contradict_fact(
        f1.id,
        _fact_input(content="Tim uses Fahrenheit", subject="Tim"),
        session=session,
    )

    result = await session.execute(
        select(GraphEdge).where(
            GraphEdge.source_id == f2.id,
            GraphEdge.target_id == f1.id,
            GraphEdge.relation == "contradicts",
        )
    )
    edge = result.scalar_one_or_none()
    assert edge is not None
    assert edge.source_type == "fact"
    assert edge.target_type == "fact"


async def test_supersede_creates_graph_edge(heart, session):
    """facts.supersede() also creates a 'supersedes' graph edge."""
    from nous.storage.models import GraphEdge
    from sqlalchemy import select

    f1 = await heart.learn(
        _fact_input(content="Python 3.11 is latest", subject="Python"),
        session=session,
    )
    f2 = await heart.supersede_fact(
        f1.id,
        _fact_input(content="Python 3.12 is latest", subject="Python"),
        session=session,
    )

    result = await session.execute(
        select(GraphEdge).where(
            GraphEdge.source_id == f2.id,
            GraphEdge.target_id == f1.id,
            GraphEdge.relation == "supersedes",
        )
    )
    edge = result.scalar_one_or_none()
    assert edge is not None
    assert edge.source_type == "fact"
    assert edge.target_type == "fact"
