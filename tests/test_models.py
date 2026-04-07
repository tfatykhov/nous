"""Tests for ORM models: CRUD, relationships, constraints, cross-schema linking."""

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from nous.storage.models import (
    Decision,
    DecisionReason,
    DecisionTag,
    Episode,
    EpisodeDecision,
    Fact,
    Guardrail,
)


async def test_create_decision(session):
    """Insert a decision via ORM, read it back, verify all fields."""
    decision = Decision(
        agent_id="test-agent",
        description="Use PostgreSQL for storage",
        context="Evaluating database options",
        pattern="Prefer mature, well-supported databases",
        confidence=0.85,
        category="architecture",
        stakes="medium",
    )
    session.add(decision)
    await session.commit()

    result = await session.execute(select(Decision).where(Decision.id == decision.id))
    loaded = result.scalar_one()
    assert loaded.agent_id == "test-agent"
    assert loaded.description == "Use PostgreSQL for storage"
    assert loaded.context == "Evaluating database options"
    assert loaded.pattern == "Prefer mature, well-supported databases"
    assert loaded.confidence == 0.85
    assert loaded.category == "architecture"
    assert loaded.stakes == "medium"
    assert loaded.outcome == "pending"
    assert loaded.created_at is not None
    assert loaded.updated_at is not None


async def test_create_episode(session):
    """Insert an episode with tags array, verify tags stored correctly."""
    episode = Episode(
        agent_id="test-agent",
        summary="Implemented database schema for Nous project",
        tags=["database", "postgres", "schema"],
    )
    session.add(episode)
    await session.commit()

    result = await session.execute(select(Episode).where(Episode.id == episode.id))
    loaded = result.scalar_one()
    assert loaded.agent_id == "test-agent"
    assert loaded.summary == "Implemented database schema for Nous project"
    assert loaded.tags == ["database", "postgres", "schema"]
    assert loaded.active is True


async def test_create_fact(session):
    """Insert a fact with embedding vector, verify storage."""
    embedding = [0.1] * 1536
    fact = Fact(
        agent_id="test-agent",
        content="PostgreSQL supports vector similarity search via pgvector",
        category="technical",
        subject="pgvector",
        embedding=embedding,
    )
    session.add(fact)
    await session.commit()

    result = await session.execute(select(Fact).where(Fact.id == fact.id))
    loaded = result.scalar_one()
    assert loaded.content == "PostgreSQL supports vector similarity search via pgvector"
    assert loaded.category == "technical"
    assert loaded.embedding is not None
    assert len(loaded.embedding) == 1536


async def test_decision_with_tags(session):
    """Insert decision with tags via relationship, query by tag."""
    decision = Decision(
        agent_id="test-agent",
        description="Tag test decision",
        confidence=0.9,
        category="tooling",
        stakes="low",
        tags=[
            DecisionTag(tag="python"),
            DecisionTag(tag="sqlalchemy"),
        ],
    )
    session.add(decision)
    await session.commit()

    # Query by tag
    result = await session.execute(select(DecisionTag).where(DecisionTag.tag == "python"))
    tag = result.scalar_one()
    assert tag.decision_id == decision.id

    # Verify tags accessible from decision
    result = await session.execute(select(Decision).where(Decision.id == decision.id))
    loaded = result.scalar_one()
    await session.refresh(loaded, ["tags"])
    tag_values = {t.tag for t in loaded.tags}
    assert tag_values == {"python", "sqlalchemy"}


async def test_decision_with_reasons(session):
    """Insert decision with typed reasons, verify cascade delete."""
    decision = Decision(
        agent_id="test-agent",
        description="Reason cascade test",
        confidence=0.8,
        category="architecture",
        stakes="medium",
        reasons=[
            DecisionReason(type="analysis", text="Analyzed the trade-offs"),
            DecisionReason(type="pattern", text="Follows established patterns"),
        ],
    )
    session.add(decision)
    await session.commit()

    # Verify reasons exist
    reason_ids = [r.id for r in decision.reasons]
    assert len(reason_ids) == 2

    # Delete the decision
    await session.delete(decision)
    await session.commit()

    # Verify reasons were cascade deleted
    for rid in reason_ids:
        result = await session.execute(select(DecisionReason).where(DecisionReason.id == rid))
        assert result.scalar_one_or_none() is None


async def test_guardrail_jsonb(session):
    """Read a seed guardrail, parse JSONB condition."""
    result = await session.execute(
        select(Guardrail).where(
            Guardrail.agent_id == "nous-default",
            Guardrail.name == "no-high-stakes-low-confidence",
        )
    )
    guardrail = result.scalar_one()
    assert isinstance(guardrail.condition, dict)
    assert "cel" in guardrail.condition
    assert "stakes" in guardrail.condition["cel"]
    assert guardrail.severity == "block"


async def test_cross_schema_relationship(session):
    """EpisodeDecision links heart.episode + brain.decision across schemas."""
    decision = Decision(
        agent_id="test-agent",
        description="Cross-schema test decision",
        confidence=0.7,
        category="process",
        stakes="low",
    )
    episode = Episode(
        agent_id="test-agent",
        summary="Cross-schema test episode",
    )
    session.add_all([decision, episode])
    await session.commit()

    link = EpisodeDecision(
        episode_id=episode.id,
        decision_id=decision.id,
    )
    session.add(link)
    await session.commit()

    # Query the link back
    result = await session.execute(
        select(EpisodeDecision).where(
            EpisodeDecision.episode_id == episode.id,
            EpisodeDecision.decision_id == decision.id,
        )
    )
    loaded = result.scalar_one()
    assert loaded.episode_id == episode.id
    assert loaded.decision_id == decision.id


async def test_check_constraints(session):
    """Invalid enum values are rejected by CHECK constraints."""
    decision = Decision(
        agent_id="test-agent",
        description="Invalid stakes test",
        confidence=0.5,
        category="architecture",
        stakes="invalid_stakes",  # Not in CHECK constraint
    )
    session.add(decision)
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_null_embedding(session):
    """NULL embedding is accepted without error."""
    decision = Decision(
        agent_id="test-agent",
        description="Null embedding test",
        confidence=0.6,
        category="tooling",
        stakes="low",
        embedding=None,
    )
    session.add(decision)
    await session.commit()

    result = await session.execute(select(Decision).where(Decision.id == decision.id))
    loaded = result.scalar_one()
    assert loaded.embedding is None


async def test_unique_guardrail_per_agent(session):
    """Same guardrail name OK for different agents, duplicate fails for same agent."""
    # First guardrail for agent-a
    g1 = Guardrail(
        agent_id="agent-a",
        name="test-rule",
        condition={"test": True},
        severity="warn",
    )
    session.add(g1)
    await session.flush()

    # Same name for agent-b — should succeed
    g2 = Guardrail(
        agent_id="agent-b",
        name="test-rule",
        condition={"test": True},
        severity="warn",
    )
    session.add(g2)
    await session.flush()

    # Duplicate name for agent-a — should fail
    g3 = Guardrail(
        agent_id="agent-a",
        name="test-rule",
        condition={"test": True},
        severity="block",
    )
    session.add(g3)
    with pytest.raises(IntegrityError):
        await session.flush()
