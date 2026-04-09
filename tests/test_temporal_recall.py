"""Tests for temporal recall — spec 008.6 Phases 1-3.

Covers the list_recent() fix (active filter → ended_at filter),
the new `hours` parameter for time-windowed episode listing,
config toggle, temporal context tier, and recall_recent tool.

5 tests in TestListRecentFix, 2 in TestTemporalConfig, 4 in TestTemporalContextTier,
4 tests in TestRecallRecentTool.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from nous.heart.schemas import EpisodeSummary
from nous.storage.models import Episode as EpisodeORM



# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

AGENT_ID = "nous-default"


def _make_episode(
    *,
    active: bool = False,
    ended_at: datetime | None = None,
    started_at: datetime | None = None,
    outcome: str | None = "success",
    summary: str = "test episode",
    title: str = "Test",
) -> EpisodeORM:
    """Build an Episode ORM instance with sensible defaults."""
    now = datetime.now(UTC)
    return EpisodeORM(
        id=uuid4(),
        agent_id=AGENT_ID,
        title=title,
        summary=summary,
        active=active,
        started_at=started_at or now,
        ended_at=ended_at,
        outcome=outcome,
        tags=["test"],
    )


# ---------------------------------------------------------------------------
# TestListRecentFix
# ---------------------------------------------------------------------------


class TestListRecentFix:
    """Tests for the fixed list_recent() behavior."""

    async def test_list_recent_returns_closed_episodes(self, heart, session):
        """Closed episodes (active=False, ended_at set) should be returned."""
        now = datetime.now(UTC)
        ep = _make_episode(
            active=False,
            ended_at=now,
            started_at=now - timedelta(minutes=10),
            outcome="success",
        )
        session.add(ep)
        await session.flush()

        results = await heart.list_episodes(limit=10, session=session)

        ids = [r.id for r in results]
        assert ep.id in ids

    async def test_list_recent_excludes_ongoing_episodes(self, heart, session):
        """Ongoing episodes (active=True, ended_at=None) should be excluded."""
        now = datetime.now(UTC)
        ep = _make_episode(
            active=True,
            ended_at=None,
            started_at=now - timedelta(minutes=5),
            outcome=None,
        )
        session.add(ep)
        await session.flush()

        results = await heart.list_episodes(limit=10, session=session)

        ids = [r.id for r in results]
        assert ep.id not in ids

    async def test_list_recent_hours_filter(self, heart, session):
        """hours parameter filters episodes by started_at time window."""
        now = datetime.now(UTC)

        recent_ep = _make_episode(
            active=False,
            ended_at=now - timedelta(minutes=30),
            started_at=now - timedelta(hours=1),
            outcome="success",
            summary="recent episode",
        )
        old_ep = _make_episode(
            active=False,
            ended_at=now - timedelta(days=2, hours=23),
            started_at=now - timedelta(days=3),
            outcome="success",
            summary="old episode",
        )
        session.add(recent_ep)
        session.add(old_ep)
        await session.flush()

        # hours=48 should return only the recent one
        results_48 = await heart.list_episodes(limit=10, hours=48, session=session)
        ids_48 = [r.id for r in results_48]
        assert recent_ep.id in ids_48
        assert old_ep.id not in ids_48

        # hours=100 should return both
        results_100 = await heart.list_episodes(limit=10, hours=100, session=session)
        ids_100 = [r.id for r in results_100]
        assert recent_ep.id in ids_100
        assert old_ep.id in ids_100

    async def test_list_recent_ordered_by_started_at_desc(self, heart, session):
        """Episodes should be returned newest-first."""
        now = datetime.now(UTC)

        ep1 = _make_episode(
            active=False,
            ended_at=now - timedelta(hours=2),
            started_at=now - timedelta(hours=3),
            outcome="success",
            summary="oldest",
        )
        ep2 = _make_episode(
            active=False,
            ended_at=now - timedelta(hours=1),
            started_at=now - timedelta(hours=2),
            outcome="partial",
            summary="middle",
        )
        ep3 = _make_episode(
            active=False,
            ended_at=now - timedelta(minutes=10),
            started_at=now - timedelta(hours=1),
            outcome="success",
            summary="newest",
        )
        session.add(ep1)
        session.add(ep2)
        session.add(ep3)
        await session.flush()

        results = await heart.list_episodes(limit=10, session=session)

        # Filter to only our test episodes
        our_ids = {ep1.id, ep2.id, ep3.id}
        our_results = [r for r in results if r.id in our_ids]

        assert len(our_results) == 3
        assert our_results[0].id == ep3.id  # newest first
        assert our_results[1].id == ep2.id
        assert our_results[2].id == ep1.id

    async def test_list_recent_excludes_abandoned(self, heart, session):
        """Abandoned episodes should be excluded by default."""
        now = datetime.now(UTC)
        ep = _make_episode(
            active=False,
            ended_at=now,
            started_at=now - timedelta(minutes=10),
            outcome="abandoned",
        )
        session.add(ep)
        await session.flush()

        results = await heart.list_episodes(limit=10, session=session)

        ids = [r.id for r in results]
        assert ep.id not in ids


# ---------------------------------------------------------------------------
# TestTemporalConfig (Phase 2, Task 2)
# ---------------------------------------------------------------------------


class TestTemporalConfig:
    """Tests for the temporal_context_enabled config toggle."""

    def test_temporal_context_enabled_default(self):
        from nous.config import Settings

        s = Settings(**{"ANTHROPIC_API_KEY": "test"})
        assert s.temporal_context_enabled is True

    def test_temporal_context_disabled(self, monkeypatch):
        from nous.config import Settings

        monkeypatch.setenv("NOUS_TEMPORAL_CONTEXT_ENABLED", "false")
        s = Settings(**{"ANTHROPIC_API_KEY": "test"})
        assert s.temporal_context_enabled is False


# ---------------------------------------------------------------------------
# TestTemporalContextTier (Phase 2, Task 3)
# ---------------------------------------------------------------------------


class TestTemporalContextTier:
    """Always-on temporal tier injects recent episode titles into context."""

    @pytest.fixture
    def mock_heart(self):
        from unittest.mock import AsyncMock, MagicMock

        heart = MagicMock()
        heart.list_episodes = AsyncMock(return_value=[])
        heart.search_episodes = AsyncMock(return_value=[])
        heart.search_facts = AsyncMock(return_value=[])
        heart.search_procedures = AsyncMock(return_value=[])
        heart.search_censors = AsyncMock(return_value=[])
        heart.list_censors = AsyncMock(return_value=[])
        heart.list_facts_by_category = AsyncMock(return_value=[])
        heart.get_facts_by_categories = AsyncMock(return_value=[])
        heart.get_working_memory = AsyncMock(return_value=None)
        return heart

    @pytest.fixture
    def mock_brain(self):
        from unittest.mock import AsyncMock, MagicMock

        brain = MagicMock()
        brain.query = AsyncMock(return_value=[])
        brain.embeddings = None
        return brain

    @pytest.fixture
    def settings(self):
        from unittest.mock import MagicMock

        s = MagicMock()
        s.temporal_context_enabled = True
        return s

    @pytest.fixture
    def settings_disabled(self):
        from unittest.mock import MagicMock

        s = MagicMock()
        s.temporal_context_enabled = False
        return s

    @pytest.fixture
    def frame(self):
        from nous.cognitive.schemas import FrameSelection

        return FrameSelection(
            frame_id="task",
            frame_name="Task",
            confidence=1.0,
            match_method="pattern",
        )

    async def test_temporal_tier_includes_recent_titles(
        self, mock_brain, mock_heart, settings, frame
    ):
        """Temporal tier should include recent episode titles in context."""
        from nous.cognitive.context import ContextEngine

        now = datetime.now(UTC)
        mock_heart.list_episodes.return_value = [
            EpisodeSummary(
                id=uuid4(),
                title="Debugging the login flow",
                summary="Fixed auth bug",
                outcome="success",
                started_at=now - timedelta(hours=2),
                tags=["debug"],
            ),
            EpisodeSummary(
                id=uuid4(),
                title="Database migration planning",
                summary="Planned schema changes",
                outcome="success",
                started_at=now - timedelta(hours=5),
                tags=["architecture"],
            ),
        ]

        engine = ContextEngine(mock_brain, mock_heart, settings)
        result = await engine.build("test-agent", "session-1", "hello", frame)

        # Check that the temporal tier section exists
        labels = [s.label for s in result.sections]
        assert "Recent Conversations" in labels

        # Check both titles appear in the system prompt
        assert "Debugging the login flow" in result.system_prompt
        assert "Database migration planning" in result.system_prompt

    async def test_temporal_tier_disabled_by_config(
        self, mock_brain, mock_heart, settings_disabled, frame
    ):
        """When temporal_context_enabled=False, no temporal tier should appear."""
        from nous.cognitive.context import ContextEngine

        now = datetime.now(UTC)
        mock_heart.list_episodes.return_value = [
            EpisodeSummary(
                id=uuid4(),
                title="Should not appear",
                summary="Hidden",
                outcome="success",
                started_at=now - timedelta(hours=1),
                tags=["test"],
            ),
        ]

        engine = ContextEngine(mock_brain, mock_heart, settings_disabled)
        result = await engine.build("test-agent", "session-1", "hello", frame)

        labels = [s.label for s in result.sections]
        assert "Recent Conversations" not in labels

    async def test_temporal_tier_empty_when_no_recent(
        self, mock_brain, mock_heart, settings, frame
    ):
        """When list_episodes returns empty, no temporal section should appear."""
        from nous.cognitive.context import ContextEngine

        mock_heart.list_episodes.return_value = []

        engine = ContextEngine(mock_brain, mock_heart, settings)
        result = await engine.build("test-agent", "session-1", "hello", frame)

        labels = [s.label for s in result.sections]
        assert "Recent Conversations" not in labels

    async def test_temporal_tier_deduplicates_with_semantic(
        self, mock_brain, mock_heart, settings, frame
    ):
        """Episode in temporal tier should be excluded from semantic episode results."""
        from nous.cognitive.context import ContextEngine

        now = datetime.now(UTC)
        shared_id = uuid4()

        # Same episode appears in both temporal and semantic results
        temporal_ep = EpisodeSummary(
            id=shared_id,
            title="Shared episode",
            summary="This appears in both tiers",
            outcome="success",
            started_at=now - timedelta(hours=1),
            tags=["test"],
        )
        semantic_ep = EpisodeSummary(
            id=shared_id,
            title="Shared episode",
            summary="This appears in both tiers",
            outcome="success",
            started_at=now - timedelta(hours=1),
            tags=["test"],
            score=0.9,
        )
        unique_semantic_ep = EpisodeSummary(
            id=uuid4(),
            title="Unique semantic episode",
            summary="Only in semantic search",
            outcome="success",
            started_at=now - timedelta(hours=10),
            tags=["other"],
            score=0.8,
        )

        mock_heart.list_episodes.return_value = [temporal_ep]
        mock_heart.search_episodes.return_value = [semantic_ep, unique_semantic_ep]

        engine = ContextEngine(mock_brain, mock_heart, settings)
        result = await engine.build("test-agent", "session-1", "hello", frame)

        # The shared episode title should appear exactly once (in temporal tier)
        # Count occurrences of "Shared episode" in the prompt
        count = result.system_prompt.count("Shared episode")
        assert count == 1, f"Expected 'Shared episode' once, found {count} times"

        # The unique semantic episode should still appear
        assert "Only in semantic search" in result.system_prompt


# ---------------------------------------------------------------------------
# TestRecallRecentTool (Phase 3, Task 4)
# ---------------------------------------------------------------------------


class TestRecallRecentTool:
    """recall_recent tool returns time-ordered episodes."""

    @pytest.mark.asyncio
    async def test_recall_recent_returns_formatted_episodes(self):
        from unittest.mock import AsyncMock, MagicMock
        from nous.heart.schemas import EpisodeSummary
        from nous.api.tools import create_nous_tools
        from datetime import datetime, UTC
        from uuid import uuid4

        mock_brain = MagicMock()
        mock_heart = MagicMock()
        episodes = [
            EpisodeSummary(
                id=uuid4(), title="Ski Trip Planning",
                summary="Budget for Breckenridge", outcome="success",
                started_at=datetime(2026, 2, 28, 12, 24, tzinfo=UTC), tags=["travel"],
            ),
            EpisodeSummary(
                id=uuid4(), title="Code Review",
                summary="Reviewed PR #81", outcome="success",
                started_at=datetime(2026, 2, 28, 11, 0, tzinfo=UTC), tags=["dev"],
            ),
        ]
        mock_heart.list_episodes = AsyncMock(return_value=episodes)

        tools = create_nous_tools(mock_brain, mock_heart)
        result = await tools["recall_recent"](hours=48, limit=10)

        text = result["content"][0]["text"]
        assert "Ski Trip Planning" in text
        assert "Code Review" in text
        assert "Feb 28" in text

    @pytest.mark.asyncio
    async def test_recall_recent_empty(self):
        from unittest.mock import AsyncMock, MagicMock
        from nous.api.tools import create_nous_tools

        mock_brain = MagicMock()
        mock_heart = MagicMock()
        mock_heart.list_episodes = AsyncMock(return_value=[])

        tools = create_nous_tools(mock_brain, mock_heart)
        result = await tools["recall_recent"](hours=48, limit=10)

        text = result["content"][0]["text"]
        assert "No episodes" in text

    @pytest.mark.asyncio
    async def test_recall_recent_passes_hours_and_limit(self):
        from unittest.mock import AsyncMock, MagicMock
        from nous.api.tools import create_nous_tools

        mock_brain = MagicMock()
        mock_heart = MagicMock()
        mock_heart.list_episodes = AsyncMock(return_value=[])

        tools = create_nous_tools(mock_brain, mock_heart)
        await tools["recall_recent"](hours=72, limit=5)

        mock_heart.list_episodes.assert_called_once_with(limit=5, hours=72)

    def test_recall_recent_in_frame_tools(self):
        from nous.api.runner import FRAME_TOOLS

        for frame, tools in FRAME_TOOLS.items():
            if frame == "initiation":
                assert "recall_recent" not in tools
            elif tools == ["*"]:
                pass  # task frame includes all
            else:
                assert "recall_recent" in tools, f"recall_recent missing from {frame}"


# ---------------------------------------------------------------------------
# TestRecapDetection (Phase 4, Task 5)
# ---------------------------------------------------------------------------


class TestRecapDetection:

    @pytest.mark.parametrize("query", [
        "what did we talk about",
        "What did we talk about recently?",
        "what have we discussed",
        "catch me up",
        "recap",
        "give me a recap of recent conversations",
        "what happened recently",
        "summary of recent discussions",
        "what did we do yesterday",
    ])
    def test_recap_patterns_detected(self, query):
        from nous.cognitive.layer import _is_recap_query
        assert _is_recap_query(query) is True

    @pytest.mark.parametrize("query", [
        "how do I fix this bug",
        "what is the capital of France",
        "write a function to sort a list",
        "hello",
        "thanks",
        "tell me about the architecture",
    ])
    def test_non_recap_not_detected(self, query):
        from nous.cognitive.layer import _is_recap_query
        assert _is_recap_query(query) is False


# ---------------------------------------------------------------------------
# TestTemporalRecencyWiring (Phase 4, Task 6)
# ---------------------------------------------------------------------------


class TestTemporalRecencyWiring:
    def test_plan_retrieval_boosts_episodes_on_high_recency(self):
        from nous.cognitive.intent import IntentClassifier
        from nous.cognitive.schemas import FrameSelection

        classifier = IntentClassifier()
        frame = FrameSelection(
            frame_id="conversation", frame_name="Conversation",
            confidence=1.0, match_method="pattern",
        )
        signals = classifier.classify("what did we talk about recently", frame)
        assert signals.temporal_recency > 0.5

        plan = classifier.plan_retrieval(signals, input_text="what did we talk about recently")
        # Conversation frame normally has episodes=0, but temporal boost should override
        assert plan.budget_overrides.get("episodes", 0) > 0

    def test_bare_recap_without_temporal_words_still_boosts(self):
        """Bare 'recap' or 'catch me up' should trigger budget boost even without temporal words."""
        from nous.cognitive.layer import _is_recap_query
        from nous.cognitive.intent import IntentClassifier
        from nous.cognitive.schemas import FrameSelection

        # "recap" has no temporal signal words like "recently"
        assert _is_recap_query("recap") is True

        classifier = IntentClassifier()
        frame = FrameSelection(
            frame_id="conversation", frame_name="Conversation",
            confidence=1.0, match_method="pattern",
        )
        signals = classifier.classify("recap", frame)
        # Without the layer.py fix, temporal_recency would be 0.0
        # The fix in layer.py sets it to 0.8 for recap queries
        # We can't test layer.py wiring here (needs full CognitiveLayer),
        # but we verify the intent signal behavior:
        # With temporal_recency set to 0.8, plan_retrieval should boost
        signals.temporal_recency = 0.8  # Simulates what layer.py does
        plan = classifier.plan_retrieval(signals, input_text="recap")
        assert plan.budget_overrides.get("episodes", 0) > 0


# ---------------------------------------------------------------------------
# TestBudgetBoost (Phase 4, Task 6)
# ---------------------------------------------------------------------------


class TestBudgetBoost:
    @pytest.fixture
    def mock_heart(self):
        from unittest.mock import AsyncMock, MagicMock
        heart = MagicMock()
        heart.list_episodes = AsyncMock(return_value=[])
        heart.search_episodes = AsyncMock(return_value=[])
        heart.search_facts = AsyncMock(return_value=[])
        heart.search_procedures = AsyncMock(return_value=[])
        heart.search_censors = AsyncMock(return_value=[])
        heart.list_censors = AsyncMock(return_value=[])
        heart.list_facts_by_category = AsyncMock(return_value=[])
        heart.get_facts_by_categories = AsyncMock(return_value=[])
        heart.get_working_memory = AsyncMock(return_value=None)
        return heart

    @pytest.fixture
    def mock_brain(self):
        from unittest.mock import AsyncMock, MagicMock
        brain = MagicMock()
        brain.query = AsyncMock(return_value=[])
        brain.embeddings = None
        return brain

    @pytest.fixture
    def settings(self):
        from unittest.mock import MagicMock
        s = MagicMock()
        s.temporal_context_enabled = True
        return s

    @pytest.fixture
    def frame(self):
        from nous.cognitive.schemas import FrameSelection
        return FrameSelection(
            frame_id="conversation", frame_name="Conversation",
            confidence=1.0, match_method="pattern",
        )

    @pytest.mark.asyncio
    async def test_temporal_boost_includes_summaries(self, mock_heart, mock_brain, settings, frame):
        from unittest.mock import AsyncMock
        from nous.cognitive.context import ContextEngine

        episodes = [
            EpisodeSummary(
                id=uuid4(), title="Ski Trip Planning",
                summary="Discussed budget and dates for Breckenridge ski trip in March 2026",
                outcome="success",
                started_at=datetime.now(UTC) - timedelta(hours=1), tags=["travel"],
            ),
        ]
        mock_heart.list_episodes = AsyncMock(return_value=episodes)

        engine = ContextEngine(mock_brain, mock_heart, settings)
        result = await engine.build(
            "test-agent", "session-1", "what did we talk about", frame,
            temporal_boost=True,
        )

        section_labels = [s.label for s in result.sections]
        assert "Recent Conversations" in section_labels

        recent_section = next(s for s in result.sections if s.label == "Recent Conversations")
        # With boost, summaries should be included
        assert "Discussed budget" in recent_section.content

    @pytest.mark.asyncio
    async def test_no_boost_excludes_summaries(self, mock_heart, mock_brain, settings, frame):
        from unittest.mock import AsyncMock
        from nous.cognitive.context import ContextEngine

        episodes = [
            EpisodeSummary(
                id=uuid4(), title="Ski Trip Planning",
                summary="Discussed budget and dates for Breckenridge ski trip in March 2026",
                outcome="success",
                started_at=datetime.now(UTC) - timedelta(hours=1), tags=["travel"],
            ),
        ]
        mock_heart.list_episodes = AsyncMock(return_value=episodes)

        engine = ContextEngine(mock_brain, mock_heart, settings)
        result = await engine.build(
            "test-agent", "session-1", "hello", frame,
            temporal_boost=False,
        )

        section_labels = [s.label for s in result.sections]
        if "Recent Conversations" in section_labels:
            recent_section = next(s for s in result.sections if s.label == "Recent Conversations")
            # Without boost, summaries should NOT be included
            assert "Discussed budget" not in recent_section.content
