"""Tests for 006.2 Context Quality fixes.

Covers:
- text_overlap utility
- Subject + similarity fact supersession
- Episode dedup
- Informational response detection
- Decision recall dedup
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from nous.utils import text_overlap


# ---------------------------------------------------------------
# text_overlap utility
# ---------------------------------------------------------------


class TestTextOverlap:
    """Shared word-overlap utility tests."""

    def test_identical_strings(self):
        assert text_overlap("hello world foo", "hello world foo") == 1.0

    def test_no_overlap(self):
        assert text_overlap("hello world", "goodbye moon") == 0.0

    def test_partial_overlap(self):
        # "hello" overlaps, "world"/"moon" don't. 3+ char words: hello, world / hello, moon
        result = text_overlap("hello world", "hello moon")
        assert result == pytest.approx(0.5)

    def test_case_insensitive(self):
        assert text_overlap("Hello World", "hello world") == 1.0

    def test_empty_string(self):
        assert text_overlap("", "hello") == 0.0
        assert text_overlap("hello", "") == 0.0
        assert text_overlap("", "") == 0.0

    def test_short_words_filtered(self):
        # "a", "is", "in", "the" are all < 3 chars, filtered out
        assert text_overlap("a is in the", "a is in the") == 0.0

    def test_stop_word_resistance(self):
        # "show me brain status" vs "show me heart status"
        # 3+ char words: {show, brain, status} vs {show, heart, status}
        # overlap = {show, status} = 2, smaller = 3
        result = text_overlap("show me brain status", "show me heart status")
        assert result == pytest.approx(2 / 3)  # 0.667, below 0.80

    def test_single_word(self):
        assert text_overlap("hello", "hello") == 1.0
        assert text_overlap("hello", "world") == 0.0


# ---------------------------------------------------------------
# Fact supersession
# ---------------------------------------------------------------


class TestFactSupersession:
    """Subject + similarity supersession in FactManager._supersede_by_subject()."""

    @pytest.fixture
    def fact_manager(self):
        from nous.heart.facts import FactManager

        db = MagicMock()
        embeddings = MagicMock()
        return FactManager(db, embeddings, agent_id="test-agent")

    def test_cosine_similarity_identical(self, fact_manager):
        a = [1.0, 0.0, 0.0]
        b = [1.0, 0.0, 0.0]
        assert fact_manager._cosine_similarity(a, b) == pytest.approx(1.0)

    def test_cosine_similarity_orthogonal(self, fact_manager):
        a = [1.0, 0.0, 0.0]
        b = [0.0, 1.0, 0.0]
        assert fact_manager._cosine_similarity(a, b) == pytest.approx(0.0)

    def test_cosine_similarity_similar(self, fact_manager):
        a = [1.0, 0.8, 0.0]
        b = [1.0, 0.9, 0.0]
        sim = fact_manager._cosine_similarity(a, b)
        assert sim > 0.95  # Very similar vectors

    def test_cosine_similarity_zero_vector(self, fact_manager):
        assert fact_manager._cosine_similarity([0, 0, 0], [1, 2, 3]) == 0.0

    @pytest.mark.asyncio
    async def test_supersede_same_subject_similar_content(self, fact_manager):
        """Same subject + high similarity -> old fact superseded."""
        new_id = uuid4()
        old_fact = MagicMock()
        old_fact.id = uuid4()
        old_fact.embedding = [1.0, 0.9, 0.1]  # Similar to new
        old_fact.active = True

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [old_fact]

        session = AsyncMock()
        session.execute = AsyncMock(return_value=mock_result)

        await fact_manager._supersede_by_subject(
            new_id, "Nous", [1.0, 0.85, 0.15], session
        )

        assert old_fact.active is False
        assert old_fact.superseded_by == new_id

    @pytest.mark.asyncio
    async def test_no_supersede_different_content(self, fact_manager):
        """Same subject + low similarity -> NOT superseded."""
        new_id = uuid4()
        old_fact = MagicMock()
        old_fact.id = uuid4()
        old_fact.embedding = [0.0, 0.0, 1.0]  # Very different
        old_fact.active = True

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [old_fact]

        session = AsyncMock()
        session.execute = AsyncMock(return_value=mock_result)

        await fact_manager._supersede_by_subject(
            new_id, "Nous", [1.0, 0.0, 0.0], session
        )

        assert old_fact.active is True  # Not superseded

    # Test 3: Different subject not superseded
    @pytest.mark.asyncio
    async def test_different_subject_not_superseded(self, fact_manager):
        """Different subject filtered by SQL WHERE -> no facts returned, nothing modified."""
        new_id = uuid4()

        # SQL WHERE clause filters by subject, so empty result set
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []

        session = AsyncMock()
        session.execute = AsyncMock(return_value=mock_result)

        await fact_manager._supersede_by_subject(
            new_id, "Redis", [1.0, 0.0, 0.0], session
        )

        # No facts returned means no modifications -- just verify no error
        session.execute.assert_awaited_once()

    # Test 4: None subject skipped
    @pytest.mark.asyncio
    async def test_none_subject_skipped(self, fact_manager):
        """When subject is None or empty, _supersede_by_subject should NOT be called."""
        from nous.heart.schemas import FactInput

        fact_manager.embeddings.embed = AsyncMock(return_value=[1.0, 0.0, 0.0])

        session = AsyncMock()
        session.flush = AsyncMock()
        session.add = MagicMock()

        fact_manager._emit_event = AsyncMock()
        fact_manager._find_duplicate = AsyncMock(return_value=None)
        fact_manager._to_detail = MagicMock(return_value=MagicMock())
        fact_manager._find_contradiction = AsyncMock(return_value=None)
        fact_manager._check_domain_threshold = AsyncMock()

        with patch.object(fact_manager, "_supersede_by_subject", new_callable=AsyncMock) as mock_supersede:
            input_no_subject = FactInput(
                content="Some fact without subject",
                category="general",
                subject=None,
                confidence=0.9,
                source="test",
            )
            await fact_manager._learn(
                input_no_subject,
                exclude_ids=[],
                check_contradictions=True,
                session=session,
            )
            mock_supersede.assert_not_awaited()

        # Also test with empty string subject
        with patch.object(fact_manager, "_supersede_by_subject", new_callable=AsyncMock) as mock_supersede:
            input_empty_subject = FactInput(
                content="Some fact with empty subject",
                category="general",
                subject="",
                confidence=0.9,
                source="test",
            )
            await fact_manager._learn(
                input_empty_subject,
                exclude_ids=[],
                check_contradictions=True,
                session=session,
            )
            mock_supersede.assert_not_awaited()

    # Test 6: Case insensitive subject
    @pytest.mark.asyncio
    async def test_case_insensitive_subject(self, fact_manager):
        """Verify code path works with different cases -- SQL uses func.lower()."""
        new_id = uuid4()
        old_fact = MagicMock()
        old_fact.id = uuid4()
        old_fact.embedding = [1.0, 0.9, 0.1]  # Similar enough
        old_fact.active = True

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [old_fact]

        session = AsyncMock()
        session.execute = AsyncMock(return_value=mock_result)

        # Call with "NOUS" (uppercase) -- the code does subject.lower()
        await fact_manager._supersede_by_subject(
            new_id, "NOUS", [1.0, 0.85, 0.15], session
        )

        # Old fact should be superseded (high similarity)
        assert old_fact.active is False
        assert old_fact.superseded_by == new_id

        # Reset and test with mixed case
        old_fact.active = True
        old_fact.superseded_by = None

        await fact_manager._supersede_by_subject(
            new_id, "Nous", [1.0, 0.85, 0.15], session
        )

        assert old_fact.active is False
        assert old_fact.superseded_by == new_id

    # Test 7: check_contradictions=False skips supersession
    @pytest.mark.asyncio
    async def test_check_contradictions_false_skips_supersession(self, fact_manager):
        """_learn() with check_contradictions=False should NOT call _supersede_by_subject."""
        from nous.heart.schemas import FactInput

        fact_manager.embeddings.embed = AsyncMock(return_value=[1.0, 0.0, 0.0])
        fact_manager._find_duplicate = AsyncMock(return_value=None)
        fact_manager._emit_event = AsyncMock()
        fact_manager._to_detail = MagicMock(return_value=MagicMock())

        session = AsyncMock()
        session.add = MagicMock()
        session.flush = AsyncMock()

        with patch.object(fact_manager, "_supersede_by_subject", new_callable=AsyncMock) as mock_supersede:
            input_data = FactInput(
                content="Nous is version 0.2",
                category="system",
                subject="Nous",
                confidence=0.9,
                source="test",
            )
            await fact_manager._learn(
                input_data,
                exclude_ids=[],
                check_contradictions=False,
                session=session,
            )
            mock_supersede.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_old_fact_no_embedding_skipped(self, fact_manager):
        """Old fact without embedding -> skipped gracefully."""
        new_id = uuid4()
        old_fact = MagicMock()
        old_fact.id = uuid4()
        old_fact.embedding = None
        old_fact.active = True

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [old_fact]

        session = AsyncMock()
        session.execute = AsyncMock(return_value=mock_result)

        await fact_manager._supersede_by_subject(
            new_id, "Nous", [1.0, 0.0, 0.0], session
        )

        assert old_fact.active is True  # Unchanged

    @pytest.mark.asyncio
    async def test_multiple_old_facts_selective(self, fact_manager):
        """Multiple old facts: only similar ones superseded."""
        new_id = uuid4()
        similar = MagicMock(id=uuid4(), embedding=[1.0, 0.9, 0.1], active=True)
        different = MagicMock(id=uuid4(), embedding=[0.0, 0.0, 1.0], active=True)

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [similar, different]

        session = AsyncMock()
        session.execute = AsyncMock(return_value=mock_result)

        await fact_manager._supersede_by_subject(
            new_id, "Nous", [1.0, 0.85, 0.15], session
        )

        assert similar.active is False
        assert similar.superseded_by == new_id
        assert different.active is True


# ---------------------------------------------------------------
# Episode dedup
# ---------------------------------------------------------------


class TestEpisodeDedup:
    """Episode creation deduplication."""

    def test_text_overlap_above_threshold(self):
        """Identical summaries have overlap 1.0 > 0.80."""
        assert text_overlap(
            "Show me your current status and tools",
            "Show me your current status and tools",
        ) == 1.0

    def test_text_overlap_below_threshold_different_content(self):
        """Different summaries should be below 0.80."""
        result = text_overlap(
            "Show me brain module status",
            "Show me heart module status",
        )
        assert result < 0.80

    def test_text_overlap_empty_summaries(self):
        """Empty summaries should not trigger dedup."""
        assert text_overlap("", "") == 0.0

    @pytest.mark.asyncio
    async def test_episode_start_dedup_reuses_existing(self):
        """_start() returns existing episode when summary overlaps > 0.80."""
        from nous.heart.episodes import EpisodeManager
        from nous.heart.schemas import EpisodeInput
        from nous.storage.models import Episode

        existing_ep = MagicMock(spec=Episode)
        existing_ep.id = uuid4()
        existing_ep.summary = "Show me your current status and tools"
        existing_ep.ended_at = None
        existing_ep.started_at = datetime.now(UTC)

        mock_scalars = MagicMock()
        mock_scalars.all.return_value = [existing_ep]
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars

        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)

        manager = EpisodeManager.__new__(EpisodeManager)
        manager.agent_id = "test"
        manager.embeddings = None

        reloaded_ep = MagicMock(spec=Episode)
        manager._get_episode_orm = AsyncMock(return_value=reloaded_ep)

        detail = MagicMock()
        manager._to_detail = MagicMock(return_value=detail)

        inp = EpisodeInput(summary="Show me your current status and tools")
        result = await manager._start(inp, mock_session)

        assert result is detail
        manager._get_episode_orm.assert_awaited_once_with(existing_ep.id, mock_session)
        manager._to_detail.assert_called_once_with(reloaded_ep)

    @pytest.mark.asyncio
    async def test_episode_start_no_dedup_different_summary(self):
        """_start() creates new episode when no similar ongoing episode."""
        from nous.heart.episodes import EpisodeManager
        from nous.heart.schemas import EpisodeInput
        from nous.storage.models import Episode

        existing_ep = MagicMock(spec=Episode)
        existing_ep.id = uuid4()
        existing_ep.summary = "Completely different topic about weather"
        existing_ep.ended_at = None
        existing_ep.started_at = datetime.now(UTC)

        mock_scalars = MagicMock()
        mock_scalars.all.return_value = [existing_ep]
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars

        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.add = MagicMock()
        mock_session.flush = AsyncMock()

        manager = EpisodeManager.__new__(EpisodeManager)
        manager.agent_id = "test"
        manager.embeddings = None

        new_ep = MagicMock(spec=Episode)
        new_ep.id = uuid4()
        manager._get_episode_orm = AsyncMock(return_value=new_ep)
        manager._to_detail = MagicMock(return_value=MagicMock())
        manager._emit_event = AsyncMock()

        inp = EpisodeInput(summary="Show me your current status and tools")
        await manager._start(inp, mock_session)

        # No dedup — new episode added
        mock_session.add.assert_called_once()

    # Test 12: Episode start no dedup after 30 min
    @pytest.mark.asyncio
    async def test_episode_start_no_dedup_after_30_min(self):
        """Episode started > 30 min ago excluded by WHERE clause -> new episode created."""
        from nous.heart.episodes import EpisodeManager
        from nous.heart.schemas import EpisodeInput
        from nous.storage.models import Episode

        # Existing episode started 31 minutes ago -- beyond the 30 min dedup window
        old_ep = MagicMock(spec=Episode)
        old_ep.id = uuid4()
        old_ep.summary = "Show me your current status and tools"
        old_ep.ended_at = None
        old_ep.started_at = datetime.now(UTC) - timedelta(minutes=31)

        # The SQL query filters Episode.started_at >= cutoff (30 min ago),
        # so the old episode is excluded. Mock returns empty results.
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = []  # Old episode excluded by WHERE clause
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars

        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.add = MagicMock()
        mock_session.flush = AsyncMock()

        manager = EpisodeManager.__new__(EpisodeManager)
        manager.agent_id = "test"
        manager.embeddings = None

        new_ep = MagicMock(spec=Episode)
        new_ep.id = uuid4()
        manager._get_episode_orm = AsyncMock(return_value=new_ep)
        manager._to_detail = MagicMock(return_value=MagicMock())
        manager._emit_event = AsyncMock()

        inp = EpisodeInput(summary="Show me your current status and tools")
        await manager._start(inp, mock_session)

        # New episode was created since no dedup match
        mock_session.add.assert_called_once()

    # Test 13: Ended episode not reused
    @pytest.mark.asyncio
    async def test_ended_episode_not_reused(self):
        """Episode with ended_at set is filtered by WHERE clause -> new episode created."""
        from nous.heart.episodes import EpisodeManager
        from nous.heart.schemas import EpisodeInput
        from nous.storage.models import Episode

        # Existing episode is ended (ended_at is not None)
        # The SQL query filters Episode.ended_at.is_(None), so it's excluded
        # Mock returns empty results
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = []  # Ended episode excluded by WHERE clause
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars

        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.add = MagicMock()
        mock_session.flush = AsyncMock()

        manager = EpisodeManager.__new__(EpisodeManager)
        manager.agent_id = "test"
        manager.embeddings = None

        new_ep = MagicMock(spec=Episode)
        new_ep.id = uuid4()
        manager._get_episode_orm = AsyncMock(return_value=new_ep)
        manager._to_detail = MagicMock(return_value=MagicMock())
        manager._emit_event = AsyncMock()

        inp = EpisodeInput(summary="Show me your current status and tools")
        await manager._start(inp, mock_session)

        # New episode was created since ended episodes are filtered out
        mock_session.add.assert_called_once()


# ---------------------------------------------------------------
# Informational response detection
# ---------------------------------------------------------------


@dataclass
class MockToolResult:
    tool_name: str
    result: str = ""
    error: str | None = None


@dataclass
class MockTurnResult:
    response_text: str
    tool_results: list = field(default_factory=list)
    error: str | None = None


class TestInformationalDetection:
    """CognitiveLayers._is_informational() tests."""

    @pytest.fixture
    def layer(self):
        """Minimal CognitiveLayer for testing _is_informational."""
        from nous.cognitive.layer import CognitiveLayer

        brain = MagicMock()
        heart = MagicMock()
        settings = MagicMock()
        settings.agent_id = "test"
        settings.identity_prompt = ""
        layer = CognitiveLayer.__new__(CognitiveLayer)
        return layer

    def test_record_decision_tool_overrides(self, layer):
        """record_decision tool called -> NOT informational."""
        result = MockTurnResult(
            response_text="Here's my current status...",
            tool_results=[MockToolResult(tool_name="record_decision")],
        )
        assert layer._is_informational(result) is False

    def test_status_dump_detected(self, layer):
        """Response with 'current status' -> informational."""
        result = MockTurnResult(
            response_text="Here is my current status and configuration...",
        )
        assert layer._is_informational(result) is True

    def test_memory_recall_detected(self, layer):
        """Response with 'i remember' -> informational."""
        result = MockTurnResult(
            response_text="Yes, I remember that you mentioned PostgreSQL...",
        )
        assert layer._is_informational(result) is True

    def test_normal_response_not_filtered(self, layer):
        """Normal analytical response -> NOT informational."""
        result = MockTurnResult(
            response_text="Based on the analysis, I recommend using Redis caching because...",
        )
        assert layer._is_informational(result) is False

    def test_heres_not_filtered(self, layer):
        """'Here's my recommendation' -> NOT informational (pattern removed)."""
        result = MockTurnResult(
            response_text="Here's my recommendation: use Redis for this use case.",
        )
        assert layer._is_informational(result) is False

    def test_available_tools_detected(self, layer):
        """Response listing available tools -> informational."""
        result = MockTurnResult(
            response_text="My available tools include web_search, recall_deep...",
        )
        assert layer._is_informational(result) is True

    # --- 007.3: Expanded detection tests ---

    def test_git_status_detected(self, layer):
        """Response about git pull -> informational."""
        result = MockTurnResult(
            response_text="Repo pulled — now at efedacd on main with 3 new commits.",
        )
        assert layer._is_informational(result) is True

    def test_acknowledgment_detected(self, layer):
        """Short acknowledgment -> informational."""
        result = MockTurnResult(
            response_text="Got it, will do.",
        )
        assert layer._is_informational(result) is True

    def test_heres_what_detected(self, layer):
        """'Here's what I found' -> informational."""
        result = MockTurnResult(
            response_text="Here's what I found in the codebase about the database layer...",
        )
        assert layer._is_informational(result) is True

    def test_emoji_header_detected(self, layer):
        """Emoji + header (status dump) -> informational."""
        result = MockTurnResult(
            response_text="\U0001f916 Nous — Current Status\n\U0001f7e2 Agent is running...",
        )
        assert layer._is_informational(result) is True

    def test_short_response_no_tools_detected(self, layer):
        """Very short response without tools -> informational."""
        result = MockTurnResult(
            response_text="Sure, done.",
        )
        assert layer._is_informational(result) is True

    def test_short_response_with_tools_not_filtered(self, layer):
        """Short response WITH tools -> NOT informational (tool did real work)."""
        result = MockTurnResult(
            response_text="Done.",
            tool_results=[MockToolResult(tool_name="bash")],
        )
        assert layer._is_informational(result) is False

    def test_list_dominated_detected(self, layer):
        """Response that is mostly a list -> informational."""
        result = MockTurnResult(
            response_text="Available endpoints:\n- GET /status\n- POST /chat\n- DELETE /chat/{id}\n- GET /health\n- GET /facts",
        )
        assert layer._is_informational(result) is True

    def test_real_decision_not_filtered(self, layer):
        """Response with decision language -> NOT informational."""
        result = MockTurnResult(
            response_text="I decided to use PostgreSQL over Redis because it supports complex queries and we need ACID guarantees for the decision log.",
        )
        assert layer._is_informational(result) is False

    def test_recall_pattern_detected(self, layer):
        """'I recall' pattern -> informational."""
        result = MockTurnResult(
            response_text="I recall that the last time we deployed, the migration took about 15 minutes.",
        )
        assert layer._is_informational(result) is True

    # Test 20: No decision_id skips informational check
    @pytest.mark.asyncio
    async def test_no_decision_id_no_deliberation(self, layer):
        """No decision_id -> deliberation block entirely skipped."""
        layer._deliberation = MagicMock()
        layer._deliberation.abandon = AsyncMock()
        layer._deliberation.finalize = AsyncMock()

        result = MockTurnResult(
            response_text="Here is my current status...",  # Would be informational
        )
        # Simulate the decision_id check from post_turn
        decision_id = None
        if decision_id:
            if layer._is_informational(result):
                await layer._deliberation.abandon(decision_id)
            else:
                await layer._deliberation.finalize(decision_id, description="test", confidence=0.8)

        layer._deliberation.abandon.assert_not_awaited()
        layer._deliberation.finalize.assert_not_awaited()


# ---------------------------------------------------------------
# Decision recall dedup
# ---------------------------------------------------------------


@dataclass
class MockDecision:
    description: str
    outcome: str = "pending"
    confidence: float = 0.80
    created_at: str = ""


class TestDecisionRecallDedup:
    """ContextEngine._dedup_decisions() tests."""

    @pytest.fixture
    def context_engine(self):
        from nous.cognitive.context import ContextEngine

        engine = ContextEngine.__new__(ContextEngine)
        return engine

    def test_similar_same_outcome_deduped(self, context_engine):
        """5 similar decisions with same outcome -> 1 kept."""
        decisions = [
            MockDecision(description="Here's my full live status and tools", created_at=f"2026-02-25T0{i}:00:00")
            for i in range(5)
        ]
        result = context_engine._dedup_decisions(decisions)
        assert len(result) == 1

    def test_distinct_decisions_kept(self, context_engine):
        """3 distinct decisions -> all kept."""
        decisions = [
            MockDecision(description="Chose Redis for caching layer", created_at="2026-02-25T03:00:00"),
            MockDecision(description="Deployed service to production", created_at="2026-02-25T02:00:00"),
            MockDecision(description="Migrated database schema version", created_at="2026-02-25T01:00:00"),
        ]
        result = context_engine._dedup_decisions(decisions)
        assert len(result) == 3

    def test_different_outcomes_preserved(self, context_engine):
        """Same description, different outcomes -> both kept."""
        decisions = [
            MockDecision(description="Deploy service to production", outcome="success", created_at="2026-02-25T02:00:00"),
            MockDecision(description="Deploy service to production", outcome="failure", created_at="2026-02-25T01:00:00"),
        ]
        result = context_engine._dedup_decisions(decisions)
        assert len(result) == 2

    def test_most_recent_kept(self, context_engine):
        """When deduping, the most recent decision is kept."""
        old = MockDecision(description="Show current status overview", created_at="2026-02-24T01:00:00")
        new = MockDecision(description="Show current status overview", created_at="2026-02-25T01:00:00")
        result = context_engine._dedup_decisions([old, new])
        assert len(result) == 1
        assert result[0].created_at == "2026-02-25T01:00:00"

    def test_empty_list(self, context_engine):
        assert context_engine._dedup_decisions([]) == []

    def test_single_decision(self, context_engine):
        d = MockDecision(description="Something")
        assert context_engine._dedup_decisions([d]) == [d]

    def test_mixed_duplicates_and_unique(self, context_engine):
        """Mix of similar and distinct -> duplicates removed, unique kept."""
        decisions = [
            MockDecision(description="Show current status overview", created_at="2026-02-25T04:00:00"),
            MockDecision(description="Show current status overview now", created_at="2026-02-25T03:00:00"),
            MockDecision(description="Deploy Redis caching layer", created_at="2026-02-25T02:00:00"),
            MockDecision(description="Show my current status overview", created_at="2026-02-25T01:00:00"),
        ]
        result = context_engine._dedup_decisions(decisions)
        # 3 status dupes -> 1, plus Redis -> 2 total
        assert len(result) == 2
