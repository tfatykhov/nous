"""Tests for F022 Phase 2: fact-to-decision and fact-to-fact auto-linking."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from nous.brain.graph_linker import GraphLinker
from nous.brain.schemas import GraphEdgeInfo
from nous.config import Settings
from nous.events import Event, EventBus
from nous.heart.schemas import FactDetail, FactInput


def _fake_fact_detail(**overrides):
    """Create a FactDetail with all required fields populated."""
    defaults = dict(
        id=uuid4(),
        agent_id="test-agent",
        content="PostgreSQL uses MVCC",
        category="technical",
        subject="PostgreSQL",
        confidence=0.9,
        source="test",
        source_episode_id=None,
        source_decision_id=None,
        learned_at=datetime.now(UTC),
        last_confirmed=None,
        confirmation_count=0,
        superseded_by=None,
        contradiction_of=None,
        active=True,
        tags=[],
        created_at=datetime.now(UTC),
    )
    defaults.update(overrides)
    return FactDetail(**defaults)


class TestHeartBusEmission:
    """Verify Heart.learn() emits fact_learned on the EventBus."""

    @pytest.mark.asyncio
    async def test_learn_emits_fact_learned_on_bus(self):
        """When Heart._bus is set, learn() should emit fact_learned with content."""
        from nous.heart.heart import Heart

        heart = MagicMock(spec=Heart)
        heart._bus = MagicMock(spec=EventBus)
        heart._bus.emit = AsyncMock()

        fake_detail = _fake_fact_detail(
            content="PostgreSQL uses MVCC",
            subject="PostgreSQL",
        )
        heart.facts = AsyncMock()
        heart.facts.learn = AsyncMock(return_value=fake_detail)
        heart.agent_id = "test-agent"

        # Call the real learn method with mocked internals
        result = await Heart.learn(heart, FactInput(content="PostgreSQL uses MVCC", subject="PostgreSQL"))

        assert result == fake_detail
        heart._bus.emit.assert_called_once()
        emitted = heart._bus.emit.call_args[0][0]
        assert emitted.type == "fact_learned"
        assert emitted.data["fact_id"] == str(fake_detail.id)
        assert emitted.data["content"] == "PostgreSQL uses MVCC"
        assert emitted.data["category"] == "technical"
        assert emitted.data["subject"] == "PostgreSQL"

    @pytest.mark.asyncio
    async def test_learn_works_without_bus(self):
        """When Heart._bus is None, learn() should still work (no emission)."""
        from nous.heart.heart import Heart

        heart = MagicMock(spec=Heart)
        heart._bus = None

        fake_detail = _fake_fact_detail(content="test fact", subject=None)
        heart.facts = AsyncMock()
        heart.facts.learn = AsyncMock(return_value=fake_detail)

        result = await Heart.learn(heart, FactInput(content="test fact"))
        assert result == fake_detail


def _mock_settings(**overrides):
    """Create a mock Settings with cross_type_linking_enabled=True."""
    s = MagicMock(spec=Settings)
    s.cross_type_linking_enabled = overrides.get("cross_type_linking_enabled", True)
    return s


def _make_event(fact_id=None, content="test fact", category="technical", subject="test"):
    """Create a fact_learned Event."""
    return Event(
        type="fact_learned",
        agent_id="test-agent",
        data={
            "fact_id": str(fact_id or uuid4()),
            "content": content,
            "category": category,
            "subject": subject,
        },
    )


class TestFactGraphLinker:
    """Tests for the FactGraphLinker handler."""

    def _make_handler(self, graph_linker=None, settings=None, bus=None):
        from nous.handlers.fact_graph_linker import FactGraphLinker

        if graph_linker is None:
            graph_linker = AsyncMock(spec=GraphLinker)
            # db is an instance attr, not on the class spec — add it manually
            graph_linker.db = MagicMock()
            graph_linker.db.session = MagicMock()
        settings = settings or _mock_settings()
        bus = bus or MagicMock(spec=EventBus)
        bus.on = MagicMock()

        handler = FactGraphLinker(graph_linker, settings, bus)
        return handler, graph_linker, bus

    def test_registers_on_fact_learned(self):
        """Handler should register on the fact_learned event."""
        _, _, bus = self._make_handler()
        bus.on.assert_called_once()
        assert bus.on.call_args[0][0] == "fact_learned"

    @pytest.mark.asyncio
    async def test_skips_when_linking_disabled(self):
        """Should return early when cross_type_linking_enabled=False."""
        settings = _mock_settings(cross_type_linking_enabled=False)
        handler, graph_linker, _ = self._make_handler(settings=settings)

        await handler.handle(_make_event())
        graph_linker.db.session.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_content_missing(self):
        """Should return early when event has no content."""
        handler, graph_linker, _ = self._make_handler()
        event = Event(
            type="fact_learned",
            agent_id="test-agent",
            data={"fact_id": str(uuid4()), "content": ""},
        )

        await handler.handle(event)
        graph_linker.db.session.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_fact_id_missing(self):
        """Should return early when event has no fact_id."""
        handler, graph_linker, _ = self._make_handler()
        event = Event(
            type="fact_learned",
            agent_id="test-agent",
            data={"content": "some fact"},
        )

        await handler.handle(event)
        graph_linker.db.session.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_fact_id_invalid(self):
        """Should return early when fact_id is not a valid UUID."""
        handler, graph_linker, _ = self._make_handler()
        event = Event(
            type="fact_learned",
            agent_id="test-agent",
            data={"fact_id": "not-a-uuid", "content": "some fact"},
        )

        await handler.handle(event)
        graph_linker.db.session.assert_not_called()

    @pytest.mark.asyncio
    async def test_calls_link_fact_to_decisions_and_facts(self):
        """Should call both link_fact_to_decisions and link_fact_to_facts."""
        graph_linker = AsyncMock(spec=GraphLinker)
        mock_session = AsyncMock()
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        graph_linker.db = MagicMock()
        graph_linker.db.session.return_value = mock_cm

        fact_id = uuid4()
        decision_edge = GraphEdgeInfo(
            source_id=fact_id,
            target_id=uuid4(),
            source_type="fact",
            target_type="decision",
            relation="evidence_for",
            weight=0.85,
            auto_linked=True,
        )
        fact_edge = GraphEdgeInfo(
            source_id=fact_id,
            target_id=uuid4(),
            source_type="fact",
            target_type="fact",
            relation="related_to",
            weight=0.92,
            auto_linked=True,
        )
        graph_linker.link_fact_to_decisions = AsyncMock(return_value=[decision_edge])
        graph_linker.link_fact_to_facts = AsyncMock(return_value=[fact_edge])

        handler, _, _ = self._make_handler(graph_linker=graph_linker)
        event = _make_event(fact_id=fact_id, content="PostgreSQL uses MVCC")

        await handler.handle(event)

        graph_linker.link_fact_to_decisions.assert_called_once_with(
            fact_id=fact_id,
            fact_content="PostgreSQL uses MVCC",
            session=mock_session,
        )
        graph_linker.link_fact_to_facts.assert_called_once_with(
            fact_id=fact_id,
            fact_content="PostgreSQL uses MVCC",
            session=mock_session,
        )
        mock_session.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_commits_when_only_fact_edges(self):
        """Should commit when only link_fact_to_facts returns edges."""
        graph_linker = AsyncMock(spec=GraphLinker)
        mock_session = AsyncMock()
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        graph_linker.db = MagicMock()
        graph_linker.db.session.return_value = mock_cm

        fact_edge = GraphEdgeInfo(
            source_id=uuid4(),
            target_id=uuid4(),
            source_type="fact",
            target_type="fact",
            relation="related_to",
            weight=0.93,
            auto_linked=True,
        )
        graph_linker.link_fact_to_decisions = AsyncMock(return_value=[])
        graph_linker.link_fact_to_facts = AsyncMock(return_value=[fact_edge])

        handler, _, _ = self._make_handler(graph_linker=graph_linker)
        await handler.handle(_make_event())

        mock_session.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_commit_when_no_edges(self):
        """Should not commit when both linking methods return empty lists."""
        graph_linker = AsyncMock(spec=GraphLinker)
        mock_session = AsyncMock()
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        graph_linker.db = MagicMock()
        graph_linker.db.session.return_value = mock_cm
        graph_linker.link_fact_to_decisions = AsyncMock(return_value=[])
        graph_linker.link_fact_to_facts = AsyncMock(return_value=[])

        handler, _, _ = self._make_handler(graph_linker=graph_linker)
        await handler.handle(_make_event())

        mock_session.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_error_isolation(self):
        """GraphLinker failures should not propagate."""
        graph_linker = AsyncMock(spec=GraphLinker)
        mock_session = AsyncMock()
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        graph_linker.db = MagicMock()
        graph_linker.db.session.return_value = mock_cm
        graph_linker.link_fact_to_decisions = AsyncMock(
            side_effect=Exception("embedding service down")
        )

        handler, _, _ = self._make_handler(graph_linker=graph_linker)

        # Should NOT raise
        await handler.handle(_make_event())

    @pytest.mark.asyncio
    async def test_error_in_fact_to_facts_isolated(self):
        """link_fact_to_facts failure should not propagate."""
        graph_linker = AsyncMock(spec=GraphLinker)
        mock_session = AsyncMock()
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        graph_linker.db = MagicMock()
        graph_linker.db.session.return_value = mock_cm
        graph_linker.link_fact_to_decisions = AsyncMock(return_value=[])
        graph_linker.link_fact_to_facts = AsyncMock(
            side_effect=Exception("embedding service down")
        )

        handler, _, _ = self._make_handler(graph_linker=graph_linker)

        # Should NOT raise
        await handler.handle(_make_event())
