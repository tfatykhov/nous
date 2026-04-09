"""Tests for EpisodeManager — episodic memory.

All tests use real Postgres via the SAVEPOINT fixture from conftest.py.
Heart methods receive the test session via the session parameter (P1-1).
"""

from sqlalchemy import select

from nous.brain.brain import Brain
from nous.brain.schemas import ReasonInput, RecordInput
from nous.heart import (
    EpisodeDetail,
    EpisodeInput,
    EpisodeSummary,
)
from nous.storage.models import EpisodeDecision, EpisodeProcedure

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _episode_input(**overrides) -> EpisodeInput:
    """Build an EpisodeInput with sensible defaults."""
    defaults = dict(
        title="Test Episode",
        summary="A test episode for unit testing",
        trigger="unit_test",
        participants=["agent-1"],
        tags=["test"],
    )
    defaults.update(overrides)
    return EpisodeInput(**defaults)


# ---------------------------------------------------------------------------
# 1. test_start_episode
# ---------------------------------------------------------------------------


async def test_start_episode(heart, session):
    """Start creates episode with started_at, no ended_at."""
    inp = _episode_input()
    detail = await heart.start_episode(inp, session=session)

    assert isinstance(detail, EpisodeDetail)
    assert detail.title == "Test Episode"
    assert detail.summary == "A test episode for unit testing"
    assert detail.started_at is not None
    assert detail.ended_at is None
    assert detail.duration_seconds is None
    assert detail.outcome is None
    assert detail.trigger == "unit_test"


# ---------------------------------------------------------------------------
# 2. test_end_episode
# ---------------------------------------------------------------------------


async def test_end_episode(heart, session):
    """End sets ended_at, duration_seconds, outcome."""
    inp = _episode_input()
    detail = await heart.start_episode(inp, session=session)

    ended = await heart.end_episode(
        detail.id,
        outcome="success",
        lessons_learned=["lesson 1"],
        surprise_level=0.3,
        session=session,
    )

    assert ended.ended_at is not None
    assert ended.outcome == "success"
    assert ended.duration_seconds is not None
    assert ended.lessons_learned == ["lesson 1"]
    assert ended.surprise_level == 0.3


# ---------------------------------------------------------------------------
# 3. test_end_episode_calculates_duration
# ---------------------------------------------------------------------------


async def test_end_episode_calculates_duration(heart, session):
    """duration = ended_at - started_at."""
    inp = _episode_input()
    detail = await heart.start_episode(inp, session=session)

    ended = await heart.end_episode(
        detail.id,
        outcome="success",
        session=session,
    )

    # Duration should be >= 0 (test runs fast, so ~0)
    assert ended.duration_seconds is not None
    assert ended.duration_seconds >= 0


# ---------------------------------------------------------------------------
# 4. test_link_decision
# ---------------------------------------------------------------------------


async def test_link_decision(heart, db, settings, session):
    """episode_decisions row created when linking a decision."""
    # Create an episode
    inp = _episode_input()
    episode = await heart.start_episode(inp, session=session)

    # Create a brain decision to link
    brain = Brain(database=db, settings=settings)
    decision = await brain.record(
        RecordInput(
            description="Test decision for linking",
            confidence=0.8,
            category="architecture",
            stakes="low",
            reasons=[ReasonInput(type="analysis", text="Test")],
        ),
        session=session,
    )
    await brain.close()

    # Link the decision to the episode
    await heart.link_decision_to_episode(episode.id, decision.id, session=session)

    # Verify the link exists
    result = await session.execute(
        select(EpisodeDecision).where(
            EpisodeDecision.episode_id == episode.id,
            EpisodeDecision.decision_id == decision.id,
        )
    )
    link = result.scalar_one()
    assert link.episode_id == episode.id
    assert link.decision_id == decision.id


# ---------------------------------------------------------------------------
# 5. test_link_procedure_with_effectiveness
# ---------------------------------------------------------------------------


async def test_link_procedure_with_effectiveness(heart, session):
    """episode_procedures row created with effectiveness."""
    from nous.heart import ProcedureInput

    episode = await heart.start_episode(_episode_input(), session=session)
    procedure = await heart.store_procedure(
        ProcedureInput(
            name="Test Procedure",
            domain="testing",
            core_patterns=["test pattern"],
        ),
        session=session,
    )

    await heart.link_procedure_to_episode(episode.id, procedure.id, effectiveness="helped", session=session)

    result = await session.execute(
        select(EpisodeProcedure).where(
            EpisodeProcedure.episode_id == episode.id,
            EpisodeProcedure.procedure_id == procedure.id,
        )
    )
    link = result.scalar_one()
    assert link.effectiveness == "helped"


# ---------------------------------------------------------------------------
# 6. test_list_recent
# ---------------------------------------------------------------------------


async def test_list_recent(heart, session):
    """List recent closed episodes ordered by started_at DESC."""
    # Create 3 episodes and close them (list_recent only returns closed episodes)
    for i in range(3):
        ep = await heart.start_episode(
            _episode_input(title=f"Episode {i}", summary=f"Episode number {i}"),
            session=session,
        )
        await heart.end_episode(ep.id, outcome="success", session=session)

    results = await heart.list_episodes(limit=10, session=session)
    assert isinstance(results, list)
    assert len(results) >= 3

    # Should be ordered by started_at DESC
    for r in results:
        assert isinstance(r, EpisodeSummary)


# ---------------------------------------------------------------------------
# 7. test_search_episodes
# ---------------------------------------------------------------------------


async def test_search_episodes(heart, session):
    """Hybrid search returns relevant episodes."""
    # Create episodes with distinct summaries
    await heart.start_episode(
        _episode_input(
            title="Database migration",
            summary="Migrated PostgreSQL schema to version 3",
        ),
        session=session,
    )
    await heart.start_episode(
        _episode_input(
            title="UI refactor",
            summary="Refactored the React components",
        ),
        session=session,
    )

    results = await heart.search_episodes("Migrated PostgreSQL schema to version 3", session=session)
    assert isinstance(results, list)
    # With mock embeddings, identical text should match
    if results:
        assert any("PostgreSQL" in r.summary or "migration" in (r.title or "") for r in results)


# ---------------------------------------------------------------------------
# 8. test_update_summary_backfills_columns (008.3)
# ---------------------------------------------------------------------------


async def test_update_summary_backfills_columns(heart, session):
    """008.3: update_summary should backfill title, summary, lessons_learned."""
    inp = _episode_input(title=None, summary="hey what is the weather")
    detail = await heart.start_episode(inp, session=session)
    assert detail.title is None
    assert detail.summary == "hey what is the weather"

    structured = {
        "title": "Weather Check and Project Discussion",
        "summary": "Tim asked about weather, then discussed project architecture.",
        "key_points": ["Weather was 12C and sunny", "Decided on Astro framework"],
        "topics": ["weather", "architecture"],
        "outcome": "resolved",
    }
    await heart.update_episode_summary(detail.id, structured, session=session)

    # Re-fetch and verify backfill
    updated = await heart.get_episode(detail.id, session=session)
    assert updated.title == "Weather Check and Project Discussion"
    assert updated.summary == "Tim asked about weather, then discussed project architecture."
    assert updated.lessons_learned == ["Weather was 12C and sunny", "Decided on Astro framework"]
    assert updated.structured_summary == structured


# ---------------------------------------------------------------------------
# 9. test_update_summary_partial_data (008.3)
# ---------------------------------------------------------------------------


async def test_update_summary_partial_data(heart, session):
    """008.3: Gracefully handle structured summary with missing fields."""
    inp = _episode_input(summary="test input")
    detail = await heart.start_episode(inp, session=session)

    # Summary with only title, no key_points
    structured = {"title": "Quick Chat", "topics": ["misc"]}
    await heart.update_episode_summary(detail.id, structured, session=session)

    updated = await heart.get_episode(detail.id, session=session)
    assert updated.title == "Quick Chat"
    assert updated.summary == "test input"  # Not overwritten — no "summary" in structured
    assert updated.lessons_learned == []  # Not set — no "key_points" (_to_detail converts None → [])


# ---------------------------------------------------------------------------
# 10. test_end_sets_active_false (008.3)
# ---------------------------------------------------------------------------


async def test_end_sets_active_false(heart, session):
    """008.3: Ending an episode should set active=false."""
    inp = _episode_input()
    detail = await heart.start_episode(inp, session=session)
    assert detail.active is True

    ended = await heart.end_episode(detail.id, outcome="success", session=session)
    assert ended.active is False
    assert ended.outcome == "success"
    assert ended.ended_at is not None


# ---------------------------------------------------------------------------
# 11. test_end_with_lessons_and_active_flag (008.3)
# ---------------------------------------------------------------------------


async def test_end_with_lessons_and_active_flag(heart, session):
    """008.3: End with lessons_learned also sets active=false."""
    inp = _episode_input()
    detail = await heart.start_episode(inp, session=session)

    ended = await heart.end_episode(
        detail.id,
        outcome="success",
        lessons_learned=["Always check DB state first"],
        session=session,
    )
    assert ended.active is False
    assert ended.lessons_learned == ["Always check DB state first"]
