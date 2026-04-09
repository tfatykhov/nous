"""Tests for abandoned decision/episode filtering (issue #45).

Fix 1:  Brain._query() excludes abandoned decisions (outcome='failure', confidence=0.0)
Fix 1b: CalibrationEngine excludes abandoned from Brier score
Fix 2:  Episodes exclude outcome='abandoned' from list/search/format
"""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio

from nous.brain.brain import Brain
from nous.brain.calibration import CalibrationEngine
from nous.brain.schemas import ReasonInput, RecordInput
from nous.cognitive.context import ContextEngine
from nous.heart import EpisodeInput, EpisodeSummary
from nous.storage.models import Decision, Episode


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _record_input(**overrides) -> RecordInput:
    """Build a RecordInput with sensible defaults."""
    defaults = dict(
        description="Test decision for abandoned filtering",
        confidence=0.85,
        category="architecture",
        stakes="medium",
        context="Testing abandoned filtering",
        reasons=[ReasonInput(type="analysis", text="Test reason")],
    )
    defaults.update(overrides)
    return RecordInput(**defaults)


def _episode_input(**overrides) -> EpisodeInput:
    """Build an EpisodeInput with sensible defaults."""
    defaults = dict(
        title="Test Episode",
        summary="A test episode for abandoned filtering",
        trigger="unit_test",
        participants=["agent-1"],
        tags=["test"],
    )
    defaults.update(overrides)
    return EpisodeInput(**defaults)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def brain(db, settings):
    """Brain without embeddings (keyword-only mode)."""
    b = Brain(database=db, settings=settings)
    yield b
    await b.close()


@pytest.fixture
def calibration_engine() -> CalibrationEngine:
    return CalibrationEngine()


# ---------------------------------------------------------------------------
# Fix 1: Brain._query() excludes abandoned decisions
# ---------------------------------------------------------------------------


async def test_query_excludes_abandoned_decisions(brain, session):
    """Abandoned decisions (outcome='failure', confidence=0.0) excluded from default query."""
    # Record a normal decision
    normal = await brain.record(
        _record_input(description="Normal architecture decision about PostgreSQL storage"),
        session=session,
    )

    # Record an abandoned decision — must do it via ORM since record() sets confidence
    abandoned = Decision(
        agent_id=brain.agent_id,
        description="Abandoned architecture decision about PostgreSQL storage",
        confidence=0.0,
        category="architecture",
        stakes="medium",
        outcome="failure",
    )
    session.add(abandoned)
    await session.flush()

    # Query without outcome filter — abandoned should be excluded
    results = await brain.query("PostgreSQL storage", session=session)
    result_ids = {r.id for r in results}

    assert normal.id in result_ids, "Normal decision should appear in query results"
    assert abandoned.id not in result_ids, "Abandoned decision should be excluded from default query"


async def test_query_returns_abandoned_when_outcome_explicitly_requested(brain, session):
    """Abandoned decisions returned when outcome='failure' is explicitly requested."""
    # Insert an abandoned decision via ORM
    abandoned = Decision(
        agent_id=brain.agent_id,
        description="Abandoned decision about PostgreSQL storage query test",
        confidence=0.0,
        category="architecture",
        stakes="medium",
        outcome="failure",
    )
    session.add(abandoned)
    await session.flush()

    # Query with explicit outcome='failure' — abandoned should be included
    results = await brain.query("PostgreSQL storage", outcome="failure", session=session)
    result_ids = {r.id for r in results}

    assert abandoned.id in result_ids, "Abandoned decision should appear when outcome='failure' is explicitly requested"


# ---------------------------------------------------------------------------
# Fix 1b: Calibration excludes abandoned from Brier score
# ---------------------------------------------------------------------------


async def test_calibration_excludes_abandoned_decisions(session, calibration_engine):
    """Abandoned decisions (outcome='failure', confidence=0.0) don't affect Brier score."""
    agent_id = "test-cal-abandoned"

    # Insert a real reviewed decision: conf=0.9, outcome=success -> Brier=(0.9-1.0)^2=0.01
    real = Decision(
        agent_id=agent_id,
        description="Real decision",
        confidence=0.9,
        category="architecture",
        stakes="medium",
        outcome="success",
    )
    session.add(real)
    await session.flush()

    # Compute Brier without abandoned — should be 0.01
    report_without = await calibration_engine.compute(session, agent_id)
    assert report_without.brier_score is not None
    brier_without = report_without.brier_score

    # Now insert an abandoned decision (conf=0.0, outcome='failure')
    abandoned = Decision(
        agent_id=agent_id,
        description="Abandoned decision",
        confidence=0.0,
        category="architecture",
        stakes="medium",
        outcome="failure",
    )
    session.add(abandoned)
    await session.flush()

    # Re-compute — Brier should be the same (abandoned excluded)
    report_with = await calibration_engine.compute(session, agent_id)
    assert report_with.brier_score is not None
    assert abs(report_with.brier_score - brier_without) < 1e-6, (
        f"Brier score changed from {brier_without} to {report_with.brier_score} — abandoned decision leaked in"
    )

    # Also verify the abandoned decision is NOT counted in reviewed_decisions
    assert report_with.reviewed_decisions == report_without.reviewed_decisions


# ---------------------------------------------------------------------------
# Fix 2a: Episodes._list_recent() excludes abandoned
# ---------------------------------------------------------------------------


async def test_list_episodes_excludes_abandoned(heart, session):
    """Abandoned episodes excluded from list_recent() with no outcome filter."""
    # Create a normal episode
    normal = await heart.start_episode(
        _episode_input(title="Normal episode", summary="This episode is normal"),
        session=session,
    )
    await heart.end_episode(normal.id, outcome="success", session=session)

    # Create an abandoned episode
    abandoned = await heart.start_episode(
        _episode_input(title="Abandoned episode", summary="This episode was abandoned"),
        session=session,
    )
    await heart.end_episode(abandoned.id, outcome="abandoned", session=session)

    # List without outcome filter — abandoned should be excluded
    results = await heart.list_episodes(limit=50, session=session)
    result_ids = {r.id for r in results}

    assert normal.id in result_ids, "Normal episode should appear in list"
    assert abandoned.id not in result_ids, "Abandoned episode should be excluded from default list"


async def test_list_episodes_returns_abandoned_when_explicitly_requested(heart, session):
    """Abandoned episodes returned when outcome='abandoned' is explicitly requested."""
    # Create an abandoned episode
    abandoned = await heart.start_episode(
        _episode_input(title="Abandoned episode", summary="This episode was abandoned"),
        session=session,
    )
    await heart.end_episode(abandoned.id, outcome="abandoned", session=session)

    # List with explicit outcome='abandoned' — should be included
    results = await heart.list_episodes(limit=50, outcome="abandoned", session=session)
    result_ids = {r.id for r in results}

    assert abandoned.id in result_ids, "Abandoned episode should appear when outcome='abandoned' is explicitly requested"


# ---------------------------------------------------------------------------
# Fix 2b: Episodes._search() excludes abandoned
# ---------------------------------------------------------------------------


async def test_search_episodes_excludes_abandoned(heart, session):
    """Abandoned episodes excluded from search()."""
    # Create a normal episode with distinctive text for search
    normal = await heart.start_episode(
        _episode_input(
            title="Normal database migration episode",
            summary="Migrated PostgreSQL schema to version 5 successfully",
        ),
        session=session,
    )
    await heart.end_episode(normal.id, outcome="success", session=session)

    # Create an abandoned episode with similar text
    abandoned = await heart.start_episode(
        _episode_input(
            title="Abandoned database migration episode",
            summary="Migrated PostgreSQL schema to version 5 but was abandoned",
        ),
        session=session,
    )
    await heart.end_episode(abandoned.id, outcome="abandoned", session=session)

    # Search — abandoned should be excluded
    results = await heart.search_episodes("PostgreSQL schema migration version 5", session=session)
    result_ids = {r.id for r in results}

    assert abandoned.id not in result_ids, "Abandoned episode should be excluded from search results"
    # Normal episode may or may not appear depending on keyword match quality
    # but the key assertion is that abandoned is excluded


# ---------------------------------------------------------------------------
# Fix 2d: ContextEngine._format_episodes() skips abandoned
# ---------------------------------------------------------------------------


def test_format_episodes_skips_abandoned():
    """_format_episodes() safety-net filter skips abandoned entries."""
    # Use SimpleNamespace to simulate episode objects
    episodes = [
        SimpleNamespace(
            outcome="success",
            summary="Completed task",
            started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ),
        SimpleNamespace(
            outcome="abandoned",
            summary="Should not appear",
            started_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        ),
        SimpleNamespace(
            outcome="ongoing",
            summary="Still in progress",
            started_at=datetime(2026, 1, 3, tzinfo=timezone.utc),
        ),
    ]

    # Create a minimal ContextEngine — _format_episodes is a method that doesn't
    # need any wiring
    engine = ContextEngine.__new__(ContextEngine)
    result = engine._format_episodes(episodes)

    assert "Completed task" in result
    assert "Still in progress" in result
    assert "Should not appear" not in result
    assert "abandoned" not in result.lower()


def test_format_episodes_handles_none_outcome():
    """_format_episodes() treats None outcome as 'ongoing', not abandoned."""
    episodes = [
        SimpleNamespace(
            outcome=None,
            summary="No outcome yet",
            started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ),
    ]

    engine = ContextEngine.__new__(ContextEngine)
    result = engine._format_episodes(episodes)

    assert "No outcome yet" in result
    assert "[ongoing]" in result
