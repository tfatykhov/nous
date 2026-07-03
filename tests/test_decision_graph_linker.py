"""Tests for F040 Phase 5: DecisionGraphLinker reverse-linking handler."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from nous.brain.brain import Brain
from nous.brain.embeddings import EmbeddingProvider
from nous.brain.graph_linker import GraphLinker
from nous.brain.schemas import DecisionDetail, GraphEdgeInfo
from nous.config import Settings
from nous.events import Event, EventBus
from nous.handlers.decision_graph_linker import DecisionGraphLinker


def _mock_settings(**overrides):
    """Create a mock Settings with cross-type linking enabled."""
    s = MagicMock(spec=Settings)
    s.cross_type_linking_enabled = overrides.get("cross_type_linking_enabled", True)
    s.graph_threshold_fact_decision = overrides.get("graph_threshold_fact_decision", 0.72)
    s.graph_threshold_fact_episode = overrides.get("graph_threshold_fact_episode", 0.70)
    s.graph_link_candidate_window_days = overrides.get("graph_link_candidate_window_days", 60)
    s.tinyhippo_lite_enabled = overrides.get("tinyhippo_lite_enabled", False)
    return s


def _make_event(decision_id=None, category="architecture"):
    """Create a decision_recorded Event."""
    return Event(
        type="decision_recorded",
        agent_id="test-agent",
        data={
            "decision_id": str(decision_id or uuid4()),
            "category": category,
        },
    )


def _fake_decision_detail(**overrides):
    """Create a DecisionDetail with required fields."""
    defaults = dict(
        id=uuid4(),
        agent_id="test-agent",
        description="Use PostgreSQL for storage",
        context="Evaluating databases",
        pattern=None,
        confidence=0.85,
        category="architecture",
        stakes="medium",
        quality_score=0.7,
        outcome="pending",
        outcome_result=None,
        reviewed_at=None,
        reviewer=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        tags=[],
        reasons=[],
        bridge=None,
    )
    defaults.update(overrides)
    return DecisionDetail(**defaults)


class TestDecisionGraphLinker:
    """Tests for the DecisionGraphLinker handler."""

    def _make_handler(self, brain=None, graph_linker=None, embedder=None, settings=None, bus=None):
        if brain is None:
            brain = AsyncMock(spec=Brain)
        if graph_linker is None:
            graph_linker = AsyncMock(spec=GraphLinker)
            graph_linker.db = MagicMock()
            graph_linker.db.session = MagicMock()
            graph_linker.agent_id = "test-agent"
        if embedder is None:
            embedder = AsyncMock(spec=EmbeddingProvider)
            embedder.embed = AsyncMock(return_value=[0.1] * 1536)
        settings = settings or _mock_settings()
        bus = bus or MagicMock(spec=EventBus)
        bus.on = MagicMock()

        handler = DecisionGraphLinker(brain, graph_linker, embedder, settings, bus)
        return handler, brain, graph_linker, embedder, bus

    def test_registers_on_decision_recorded(self):
        """Handler should register on the decision_recorded event."""
        _, _, _, _, bus = self._make_handler()
        bus.on.assert_called_once()
        assert bus.on.call_args[0][0] == "decision_recorded"

    @pytest.mark.asyncio
    async def test_skips_when_linking_disabled(self):
        """Should return early when cross_type_linking_enabled=False."""
        settings = _mock_settings(cross_type_linking_enabled=False)
        handler, brain, _, _, _ = self._make_handler(settings=settings)

        await handler.handle(_make_event())
        brain.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_decision_id_missing(self):
        """Should return early when event has no decision_id."""
        handler, brain, _, _, _ = self._make_handler()
        event = Event(
            type="decision_recorded",
            agent_id="test-agent",
            data={"category": "architecture"},
        )

        await handler.handle(event)
        brain.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_decision_id_invalid(self):
        """Should return early when decision_id is not a valid UUID."""
        handler, brain, _, _, _ = self._make_handler()
        event = Event(
            type="decision_recorded",
            agent_id="test-agent",
            data={"decision_id": "not-a-uuid", "category": "architecture"},
        )

        await handler.handle(event)
        brain.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_no_embedder(self):
        """Should return early when embedder is None."""
        handler, brain, _, _, _ = self._make_handler(embedder=None)
        # Re-create with None embedder
        brain = AsyncMock(spec=Brain)
        graph_linker = AsyncMock(spec=GraphLinker)
        graph_linker.db = MagicMock()
        graph_linker.agent_id = "test-agent"
        settings = _mock_settings()
        bus = MagicMock(spec=EventBus)
        bus.on = MagicMock()

        handler = DecisionGraphLinker(brain, graph_linker, None, settings, bus)
        await handler.handle(_make_event())
        brain.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_decision_not_found(self):
        """Should return early when brain.get returns None."""
        handler, brain, graph_linker, _, _ = self._make_handler()
        brain.get = AsyncMock(return_value=None)

        await handler.handle(_make_event())
        graph_linker.db.session.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_description_empty(self):
        """Should return early when decision has empty description."""
        handler, brain, graph_linker, _, _ = self._make_handler()
        brain.get = AsyncMock(return_value=_fake_decision_detail(description=""))

        await handler.handle(_make_event())
        graph_linker.db.session.assert_not_called()

    @pytest.mark.asyncio
    async def test_fetches_decision_and_attempts_linking(self):
        """Should fetch decision, embed it, and query for facts/episodes."""
        decision_id = uuid4()
        decision = _fake_decision_detail(id=decision_id, description="Use PostgreSQL")

        brain = AsyncMock(spec=Brain)
        brain.get = AsyncMock(return_value=decision)

        embedder = AsyncMock(spec=EmbeddingProvider)
        embedder.embed = AsyncMock(return_value=[0.1] * 1536)

        graph_linker = AsyncMock(spec=GraphLinker)
        graph_linker.agent_id = "test-agent"
        mock_session = AsyncMock()
        # Return empty results for both SQL queries
        mock_result = MagicMock()
        mock_result.all.return_value = []
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        graph_linker.db = MagicMock()
        graph_linker.db.session.return_value = mock_cm

        settings = _mock_settings()
        bus = MagicMock(spec=EventBus)
        bus.on = MagicMock()

        handler = DecisionGraphLinker(brain, graph_linker, embedder, settings, bus)
        event = _make_event(decision_id=decision_id)

        await handler.handle(event)

        brain.get.assert_called_once_with(decision_id)
        embedder.embed.assert_called_once()
        # Two SQL queries: facts + episodes
        assert mock_session.execute.call_count == 2
        # No edges found, so no commit
        mock_session.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_creates_edges_and_commits(self):
        """Should create edges and commit when candidates are found."""
        decision_id = uuid4()
        fact_id = uuid4()
        episode_id = uuid4()
        decision = _fake_decision_detail(id=decision_id, description="Use PostgreSQL")

        brain = AsyncMock(spec=Brain)
        brain.get = AsyncMock(return_value=decision)

        embedder = AsyncMock(spec=EmbeddingProvider)
        embedder.embed = AsyncMock(return_value=[0.1] * 1536)

        graph_linker = AsyncMock(spec=GraphLinker)
        graph_linker.agent_id = "test-agent"

        # Mock create_edge to return an edge (indicating success)
        fact_edge = GraphEdgeInfo(
            source_id=fact_id,
            target_id=decision_id,
            source_type="fact",
            target_type="decision",
            relation="evidence_for",
            weight=0.85,
            auto_linked=True,
        )
        ep_edge = GraphEdgeInfo(
            source_id=episode_id,
            target_id=decision_id,
            source_type="episode",
            target_type="decision",
            relation="discussed_in",
            weight=0.80,
            auto_linked=True,
        )
        graph_linker.create_edge = AsyncMock(side_effect=[fact_edge, ep_edge])

        mock_session = AsyncMock()
        # First call returns fact candidates, second returns episode candidates
        fact_row = MagicMock()
        fact_row.id = fact_id
        fact_row.content = "PostgreSQL uses MVCC for concurrency"
        fact_row.similarity = 0.85

        ep_row = MagicMock()
        ep_row.id = episode_id
        ep_row.summary = "Discussed database options for the project"
        ep_row.similarity = 0.80

        fact_result = MagicMock()
        fact_result.all.return_value = [fact_row]
        ep_result = MagicMock()
        ep_result.all.return_value = [ep_row]
        mock_session.execute = AsyncMock(side_effect=[fact_result, ep_result])

        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        graph_linker.db = MagicMock()
        graph_linker.db.session.return_value = mock_cm

        settings = _mock_settings()
        bus = MagicMock(spec=EventBus)
        bus.on = MagicMock()

        handler = DecisionGraphLinker(brain, graph_linker, embedder, settings, bus)

        # Patch cosine similarity to return above thresholds
        with patch.object(GraphLinker, "_cosine_similarity", return_value=0.85):
            await handler.handle(_make_event(decision_id=decision_id))

        assert graph_linker.create_edge.call_count == 2
        mock_session.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_commit_when_no_edges(self):
        """Should not commit when no candidates pass threshold."""
        decision_id = uuid4()
        decision = _fake_decision_detail(id=decision_id, description="Use PostgreSQL")

        brain = AsyncMock(spec=Brain)
        brain.get = AsyncMock(return_value=decision)

        embedder = AsyncMock(spec=EmbeddingProvider)
        embedder.embed = AsyncMock(return_value=[0.1] * 1536)

        graph_linker = AsyncMock(spec=GraphLinker)
        graph_linker.agent_id = "test-agent"

        mock_session = AsyncMock()
        empty_result = MagicMock()
        empty_result.all.return_value = []
        mock_session.execute = AsyncMock(return_value=empty_result)

        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        graph_linker.db = MagicMock()
        graph_linker.db.session.return_value = mock_cm

        settings = _mock_settings()
        bus = MagicMock(spec=EventBus)
        bus.on = MagicMock()

        handler = DecisionGraphLinker(brain, graph_linker, embedder, settings, bus)
        await handler.handle(_make_event(decision_id=decision_id))

        mock_session.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_error_isolation(self):
        """Errors should not propagate — handler catches all exceptions."""
        handler, brain, _, _, _ = self._make_handler()
        brain.get = AsyncMock(side_effect=Exception("DB connection lost"))

        # Should NOT raise
        await handler.handle(_make_event())

    @pytest.mark.asyncio
    async def test_cancelled_error_reraises(self):
        """asyncio.CancelledError should be re-raised for clean shutdown."""
        handler, brain, _, _, _ = self._make_handler()
        brain.get = AsyncMock(side_effect=asyncio.CancelledError())

        with pytest.raises(asyncio.CancelledError):
            await handler.handle(_make_event())

    @pytest.mark.asyncio
    async def test_skips_fact_candidate_on_embed_failure(self):
        """If re-embedding a fact candidate fails, skip it and continue."""
        decision_id = uuid4()
        fact_id = uuid4()
        decision = _fake_decision_detail(id=decision_id, description="Use PostgreSQL")

        brain = AsyncMock(spec=Brain)
        brain.get = AsyncMock(return_value=decision)

        embedder = AsyncMock(spec=EmbeddingProvider)
        # First call succeeds (decision embed), second fails (fact re-embed)
        embedder.embed = AsyncMock(side_effect=[
            [0.1] * 1536,  # decision embedding
            Exception("embed failed"),  # fact re-embed
        ])

        graph_linker = AsyncMock(spec=GraphLinker)
        graph_linker.agent_id = "test-agent"

        mock_session = AsyncMock()
        fact_row = MagicMock()
        fact_row.id = fact_id
        fact_row.content = "Some fact"
        fact_row.similarity = 0.85

        fact_result = MagicMock()
        fact_result.all.return_value = [fact_row]
        ep_result = MagicMock()
        ep_result.all.return_value = []
        mock_session.execute = AsyncMock(side_effect=[fact_result, ep_result])

        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        graph_linker.db = MagicMock()
        graph_linker.db.session.return_value = mock_cm

        settings = _mock_settings()
        bus = MagicMock(spec=EventBus)
        bus.on = MagicMock()

        handler = DecisionGraphLinker(brain, graph_linker, embedder, settings, bus)

        # Should NOT raise — embed failure for candidate is skipped
        await handler.handle(_make_event(decision_id=decision_id))
        graph_linker.create_edge.assert_not_called()
        mock_session.commit.assert_not_called()


class TestDecisionGraphLinkerCandidateWindow:
    """graph_link_candidate_window_days wiring in DecisionGraphLinker.handle."""

    def _make_handler_with_session(self, **setting_overrides):
        decision_id = uuid4()
        decision = _fake_decision_detail(id=decision_id, description="Use PostgreSQL")

        brain = AsyncMock(spec=Brain)
        brain.get = AsyncMock(return_value=decision)

        embedder = AsyncMock(spec=EmbeddingProvider)
        embedder.embed = AsyncMock(return_value=[0.1] * 1536)

        graph_linker = AsyncMock(spec=GraphLinker)
        graph_linker.agent_id = "test-agent"

        mock_session = AsyncMock()
        empty_result = MagicMock()
        empty_result.all.return_value = []
        mock_session.execute = AsyncMock(return_value=empty_result)

        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        graph_linker.db = MagicMock()
        graph_linker.db.session.return_value = mock_cm

        settings = _mock_settings(**setting_overrides)
        bus = MagicMock(spec=EventBus)
        bus.on = MagicMock()

        handler = DecisionGraphLinker(brain, graph_linker, embedder, settings, bus)
        return handler, decision_id, mock_session

    @pytest.mark.asyncio
    async def test_window_days_positive_bounds_cutoff(self):
        """With window_days=7, cutoff passed to SQL is approximately 7 days ago."""
        handler, decision_id, mock_session = self._make_handler_with_session(
            graph_link_candidate_window_days=7
        )

        before = datetime.now(UTC) - timedelta(days=7, seconds=2)
        await handler.handle(_make_event(decision_id=decision_id))
        after = datetime.now(UTC) - timedelta(days=7)

        # Both fact SQL and episode SQL share the same cutoff param
        first_call_kwargs = mock_session.execute.call_args_list[0][0][1]
        cutoff = first_call_kwargs["cutoff"]
        assert before <= cutoff <= after, f"Expected cutoff ~7 days ago, got {cutoff}"

    @pytest.mark.asyncio
    async def test_window_days_zero_uses_far_past(self):
        """With window_days=0, cutoff is far-past (no effective time filter)."""
        handler, decision_id, mock_session = self._make_handler_with_session(
            graph_link_candidate_window_days=0
        )

        await handler.handle(_make_event(decision_id=decision_id))

        first_call_kwargs = mock_session.execute.call_args_list[0][0][1]
        cutoff = first_call_kwargs["cutoff"]
        assert cutoff <= datetime(2000, 1, 1, tzinfo=UTC), f"Expected far-past cutoff, got {cutoff}"

    @pytest.mark.asyncio
    async def test_default_window_is_sixty_days(self):
        """Default window_days=60 produces cutoff ~60 days ago."""
        handler, decision_id, mock_session = self._make_handler_with_session(
            graph_link_candidate_window_days=60
        )

        before = datetime.now(UTC) - timedelta(days=60, seconds=2)
        await handler.handle(_make_event(decision_id=decision_id))
        after = datetime.now(UTC) - timedelta(days=60)

        first_call_kwargs = mock_session.execute.call_args_list[0][0][1]
        cutoff = first_call_kwargs["cutoff"]
        assert before <= cutoff <= after, f"Expected cutoff ~60 days ago, got {cutoff}"
