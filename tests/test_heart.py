"""Integration tests for Heart public API.

All tests use real Postgres via the SAVEPOINT fixture from conftest.py.
Heart methods receive the test session via the session parameter (P1-1).

These tests exercise the Heart class as a whole, testing cross-manager
interactions and the unified recall mechanism.
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from nous.brain.brain import Brain
from nous.brain.schemas import ReasonInput, RecordInput
from nous.heart import (
    CensorInput,
    EpisodeInput,
    FactInput,
    ProcedureInput,
    RecallResult,
    WorkingMemoryItem,
)
from nous.storage.models import Event

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _episode_input(**overrides) -> EpisodeInput:
    defaults = dict(
        title="Integration Test Episode",
        summary="An episode for integration testing",
        trigger="test",
    )
    defaults.update(overrides)
    return EpisodeInput(**defaults)


def _fact_input(**overrides) -> FactInput:
    defaults = dict(
        content="Integration test fact content",
        category="technical",
        subject="testing",
        confidence=0.9,
    )
    defaults.update(overrides)
    return FactInput(**defaults)


def _procedure_input(**overrides) -> ProcedureInput:
    defaults = dict(
        name="Integration test procedure",
        domain="testing",
        core_patterns=["integration testing"],
    )
    defaults.update(overrides)
    return ProcedureInput(**defaults)


def _censor_input(**overrides) -> CensorInput:
    defaults = dict(
        trigger_pattern="integration test censor trigger",
        reason="integration test censor reason",
        action="warn",
    )
    defaults.update(overrides)
    return CensorInput(**defaults)


# ---------------------------------------------------------------------------
# 1. test_full_episode_lifecycle
# ---------------------------------------------------------------------------


async def test_full_episode_lifecycle(heart, db, settings, session):
    """start -> link decision -> link procedure -> end with outcome."""
    # Start episode
    episode = await heart.start_episode(_episode_input(), session=session)
    assert episode.ended_at is None

    # Create and link a decision
    brain = Brain(database=db, settings=settings)
    decision = await brain.record(
        RecordInput(
            description="Lifecycle test decision",
            confidence=0.8,
            category="architecture",
            stakes="low",
            reasons=[ReasonInput(type="analysis", text="Test")],
        ),
        session=session,
    )
    await brain.close()

    await heart.link_decision_to_episode(episode.id, decision.id, session=session)

    # Create and link a procedure
    procedure = await heart.store_procedure(_procedure_input(), session=session)
    await heart.link_procedure_to_episode(episode.id, procedure.id, effectiveness="helped", session=session)

    # End episode
    ended = await heart.end_episode(
        episode.id,
        outcome="success",
        lessons_learned=["Integration tests work"],
        session=session,
    )
    assert ended.ended_at is not None
    assert ended.outcome == "success"
    assert ended.duration_seconds is not None
    assert "Integration tests work" in ended.lessons_learned


# ---------------------------------------------------------------------------
# 2. test_learn_and_recall
# ---------------------------------------------------------------------------


async def test_learn_and_recall(heart, session):
    """Learn 3 facts, recall by query, verify ranking."""
    await heart.learn(
        _fact_input(content="Python is dynamically typed language"),
        session=session,
    )
    await heart.learn(
        _fact_input(content="Rust has a borrow checker for memory safety"),
        session=session,
    )
    await heart.learn(
        _fact_input(content="PostgreSQL supports JSONB data type"),
        session=session,
    )

    # Search for Python fact using identical text
    results = await heart.search_facts("Python is dynamically typed language", session=session)
    assert len(results) >= 1


# ---------------------------------------------------------------------------
# 3. test_fact_deduplication
# ---------------------------------------------------------------------------


async def test_fact_deduplication(heart, session):
    """Learn same fact twice, second call confirms instead of creating new."""
    fact1 = await heart.learn(
        _fact_input(content="Dedup test: water boils at 100 degrees"),
        session=session,
    )

    # Learn identical content — should trigger dedup and confirm
    fact2 = await heart.learn(
        _fact_input(content="Dedup test: water boils at 100 degrees"),
        session=session,
    )

    # Same ID (confirmed, not new)
    assert fact2.id == fact1.id
    assert fact2.confirmation_count == 1


# ---------------------------------------------------------------------------
# 4. test_supersede_fact
# ---------------------------------------------------------------------------


async def test_supersede_fact(heart, session):
    """Supersede old fact, verify chain and active flags."""
    old = await heart.learn(
        _fact_input(content="Supersede test: old version of fact"),
        session=session,
    )
    new = await heart.supersede_fact(
        old.id,
        _fact_input(content="Supersede test: new version of fact"),
        session=session,
    )

    # Old should be inactive with superseded_by
    old_updated = await heart.get_fact(old.id, session=session)
    assert old_updated.active is False
    assert old_updated.superseded_by == new.id

    # New should be active
    assert new.active is True


# ---------------------------------------------------------------------------
# 5. test_contradict_fact
# ---------------------------------------------------------------------------


async def test_contradict_fact(heart, session):
    """Contradict, verify confidence reduction."""
    original = await heart.learn(
        _fact_input(
            content="Contradict integration test: original claim",
            confidence=0.9,
        ),
        session=session,
    )

    contradicting = await heart.contradict_fact(
        original.id,
        _fact_input(content="Contradict integration test: opposing claim"),
        session=session,
    )

    updated_original = await heart.get_fact(original.id, session=session)
    assert updated_original.confidence == pytest.approx(0.7, abs=0.01)
    assert contradicting.contradiction_of == original.id


# ---------------------------------------------------------------------------
# 6. test_procedure_lifecycle
# ---------------------------------------------------------------------------


async def test_procedure_lifecycle(heart, session):
    """store -> activate -> record success -> check effectiveness."""
    proc = await heart.store_procedure(
        _procedure_input(name="Lifecycle test procedure"),
        session=session,
    )
    assert proc.activation_count == 0
    assert proc.effectiveness is None

    # Activate
    activated = await heart.activate_procedure(proc.id, session=session)
    assert activated.activation_count == 1

    # Record success
    result = await heart.record_procedure_outcome(proc.id, "success", session=session)
    assert result.success_count == 1
    # Laplace: (1+1)/(1+0+2) = 2/3 ~ 0.667
    assert result.effectiveness == pytest.approx(2 / 3, abs=0.01)


# ---------------------------------------------------------------------------
# 7. test_censor_lifecycle
# ---------------------------------------------------------------------------


async def test_censor_lifecycle(heart, session):
    """add -> check triggers -> escalate after threshold."""
    censor = await heart.add_censor(
        _censor_input(
            trigger_pattern="lifecycle censor test trigger",
            reason="lifecycle censor test reason",
        ),
        session=session,
    )
    assert censor.action == "warn"

    # Trigger 3 times (threshold) with identical text
    for _ in range(3):
        matches = await heart.check_censors(
            "lifecycle censor test trigger lifecycle censor test reason",
            session=session,
        )

    # After threshold, should have auto-escalated to block
    assert len(matches) >= 1
    assert matches[0].action == "block"


# ---------------------------------------------------------------------------
# 8. test_working_memory_capacity
# ---------------------------------------------------------------------------


async def test_working_memory_capacity(heart, session):
    """Fill to max, verify eviction of lowest relevance."""
    sid = f"test-heart-wm-{uuid.uuid4().hex[:8]}"
    await heart.get_or_create_working_memory(sid, session=session)

    # Fill to capacity (20)
    for i in range(20):
        item = WorkingMemoryItem(
            type="fact",
            ref_id=uuid.uuid4(),
            summary=f"Item {i}",
            relevance=0.5 + (i * 0.01),  # Varying relevance
            loaded_at=datetime.now(UTC),
        )
        await heart.load_to_working_memory(sid, item, session=session)

    # Add one more — should trigger eviction of lowest
    new_item = WorkingMemoryItem(
        type="fact",
        ref_id=uuid.uuid4(),
        summary="New overflow item",
        relevance=0.99,
        loaded_at=datetime.now(UTC),
    )
    state = await heart.load_to_working_memory(sid, new_item, session=session)

    assert state.item_count <= 20
    summaries = [i.summary for i in state.items]
    assert "New overflow item" in summaries


# ---------------------------------------------------------------------------
# 9. test_unified_recall
# ---------------------------------------------------------------------------


async def test_unified_recall(heart, session):
    """Populate all memory types, recall returns mixed results."""
    # Create one of each type with distinct content
    await heart.start_episode(
        _episode_input(
            title="Recall test episode",
            summary="Recall test episode for unified recall",
        ),
        session=session,
    )
    await heart.learn(
        _fact_input(content="Recall test fact for unified recall"),
        session=session,
    )
    await heart.store_procedure(
        _procedure_input(
            name="Recall test procedure for unified recall",
            core_patterns=["recall test"],
        ),
        session=session,
    )
    await heart.add_censor(
        _censor_input(
            trigger_pattern="Recall test censor for unified recall",
            reason="recall test reason",
        ),
        session=session,
    )

    # Recall across all types
    results = await heart.recall("Recall test", limit=20, session=session)

    assert isinstance(results, list)
    for r in results:
        assert isinstance(r, RecallResult)
        assert r.type in ("episode", "fact", "procedure", "censor")
        assert r.score > 0


# ---------------------------------------------------------------------------
# 10. test_unified_recall_type_filter
# ---------------------------------------------------------------------------


async def test_unified_recall_type_filter(heart, session):
    """recall with types=["fact"] returns only facts."""
    await heart.learn(
        _fact_input(content="Type filter recall test fact"),
        session=session,
    )
    await heart.store_procedure(
        _procedure_input(
            name="Type filter recall test procedure",
            core_patterns=["type filter recall"],
        ),
        session=session,
    )

    results = await heart.recall(
        "Type filter recall test fact",
        types=["fact"],
        session=session,
    )
    for r in results:
        assert r.type == "fact"


# ---------------------------------------------------------------------------
# 11. test_events_emitted
# ---------------------------------------------------------------------------


async def test_events_emitted(heart, session):
    """Verify events logged to nous_system.events."""
    # Start an episode — should emit episode_started
    episode = await heart.start_episode(
        _episode_input(
            title="Event emission test",
            summary="Testing event emission",
        ),
        session=session,
    )

    # Check for the event
    result = await session.execute(
        select(Event).where(
            Event.agent_id == heart.agent_id,
            Event.event_type == "episode_started",
        )
    )
    events = result.scalars().all()
    assert len(events) >= 1

    # The event data should contain the episode_id
    found = any(e.data.get("episode_id") == str(episode.id) for e in events)
    assert found, "episode_started event not found with correct episode_id"

    # Learn a fact — should emit fact_learned
    await heart.learn(
        _fact_input(content="Event emission test fact"),
        session=session,
    )

    result2 = await session.execute(
        select(Event).where(
            Event.agent_id == heart.agent_id,
            Event.event_type == "fact_learned",
        )
    )
    fact_events = result2.scalars().all()
    assert len(fact_events) >= 1


# ---------------------------------------------------------------------------
# F038-1.2: Fact minimum content length
# ---------------------------------------------------------------------------


async def test_fact_minimum_content_rejected(heart, session):
    """Content shorter than 30 chars -> FactRejected."""
    from nous.heart.schemas import FactRejected

    result = await heart.learn(
        _fact_input(content="for: CORPGEN"),
        session=session,
    )
    assert isinstance(result, FactRejected)
    assert "too short" in result.explanation.lower()


async def test_fact_minimum_content_boundary(heart, session):
    """Content exactly 30 chars -> accepted (not rejected)."""
    from nous.heart.schemas import FactRejected

    content_30 = "A" * 30  # exactly 30 characters
    result = await heart.learn(
        _fact_input(content=content_30),
        session=session,
    )
    assert not isinstance(result, FactRejected)


async def test_fact_minimum_content_whitespace(heart, session):
    """Content with whitespace padding that strips to < 30 chars -> rejected."""
    from nous.heart.schemas import FactRejected

    result = await heart.learn(
        _fact_input(content="   short   "),
        session=session,
    )
    assert isinstance(result, FactRejected)
    assert "too short" in result.explanation.lower()
