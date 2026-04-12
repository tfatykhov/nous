"""Tests for F040 Task 6: ProcedureGraphLinker handler."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from nous.brain.graph_linker import GraphLinker
from nous.brain.schemas import GraphEdgeInfo
from nous.config import Settings
from nous.events import Event, EventBus


def _mock_settings(**overrides):
    """Create a mock Settings with cross_type_linking_enabled=True."""
    s = MagicMock(spec=Settings)
    s.cross_type_linking_enabled = overrides.get("cross_type_linking_enabled", True)
    s.graph_threshold_procedure_any = overrides.get("graph_threshold_procedure_any", 0.70)
    return s


def _make_event(procedure_id=None, description="How to deploy with Docker", domain="devops", tags=None):
    """Create a procedure_stored Event."""
    return Event(
        type="procedure_stored",
        agent_id="test-agent",
        data={
            "procedure_id": str(procedure_id or uuid4()),
            "name": "docker-deploy",
            "domain": domain,
            "description": description,
            "tags": tags or ["docker"],
        },
    )


class TestProcedureGraphLinker:
    """Tests for the ProcedureGraphLinker handler."""

    def _make_handler(self, graph_linker=None, embedder=None, settings=None, bus=None):
        from nous.handlers.procedure_graph_linker import ProcedureGraphLinker

        if graph_linker is None:
            graph_linker = AsyncMock(spec=GraphLinker)
            graph_linker.db = MagicMock()
            graph_linker.db.session = MagicMock()
            graph_linker.agent_id = "test-agent"
        if embedder is None:
            embedder = AsyncMock()
            embedder.embed = AsyncMock(return_value=[0.1] * 1536)
        settings = settings or _mock_settings()
        bus = bus or MagicMock(spec=EventBus)
        bus.on = MagicMock()

        handler = ProcedureGraphLinker(graph_linker, embedder, settings, bus)
        return handler, graph_linker, embedder, bus

    def test_registers_on_procedure_stored(self):
        """Handler should register on the procedure_stored event."""
        _, _, _, bus = self._make_handler()
        bus.on.assert_called_once()
        assert bus.on.call_args[0][0] == "procedure_stored"

    @pytest.mark.asyncio
    async def test_skips_when_linking_disabled(self):
        """Should return early when cross_type_linking_enabled=False."""
        settings = _mock_settings(cross_type_linking_enabled=False)
        handler, graph_linker, _, _ = self._make_handler(settings=settings)

        await handler.handle(_make_event())
        graph_linker.db.session.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_description_missing(self):
        """Should return early when event has no description."""
        handler, graph_linker, _, _ = self._make_handler()
        event = Event(
            type="procedure_stored",
            agent_id="test-agent",
            data={
                "procedure_id": str(uuid4()),
                "name": "test",
                "domain": "",
                "description": "",
                "tags": [],
            },
        )

        await handler.handle(event)
        graph_linker.db.session.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_procedure_id_missing(self):
        """Should return early when event has no procedure_id."""
        handler, graph_linker, _, _ = self._make_handler()
        event = Event(
            type="procedure_stored",
            agent_id="test-agent",
            data={"description": "some procedure"},
        )

        await handler.handle(event)
        graph_linker.db.session.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_no_embedder(self):
        """Should return early when embedder is None."""
        handler, graph_linker, _, _ = self._make_handler(embedder=None)
        # Re-create with explicit None embedder
        from nous.handlers.procedure_graph_linker import ProcedureGraphLinker

        bus = MagicMock(spec=EventBus)
        bus.on = MagicMock()
        gl = AsyncMock(spec=GraphLinker)
        gl.db = MagicMock()
        gl.agent_id = "test-agent"
        settings = _mock_settings()

        handler = ProcedureGraphLinker(gl, None, settings, bus)

        await handler.handle(_make_event())
        gl.db.session.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_procedure_id_invalid(self):
        """Should return early when procedure_id is not a valid UUID."""
        handler, graph_linker, _, _ = self._make_handler()
        event = Event(
            type="procedure_stored",
            agent_id="test-agent",
            data={
                "procedure_id": "not-a-uuid",
                "description": "some procedure",
            },
        )

        await handler.handle(event)
        graph_linker.db.session.assert_not_called()

    @pytest.mark.asyncio
    async def test_creates_edges_for_facts_and_decisions(self):
        """Should call create_edge for matching facts and decisions."""
        graph_linker = AsyncMock(spec=GraphLinker)
        mock_session = AsyncMock()
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        graph_linker.db = MagicMock()
        graph_linker.db.session.return_value = mock_cm
        graph_linker.agent_id = "test-agent"

        proc_id = uuid4()
        fact_id = uuid4()
        decision_id = uuid4()

        # Mock SQL results: fact query returns one row, decision query returns one row
        fact_row = MagicMock()
        fact_row.id = fact_id
        fact_row.similarity = 0.85

        decision_row = MagicMock()
        decision_row.id = decision_id
        decision_row.similarity = 0.80

        # First execute call returns fact results, second returns decision results
        fact_result = MagicMock()
        fact_result.all.return_value = [fact_row]
        decision_result = MagicMock()
        decision_result.all.return_value = [decision_row]
        mock_session.execute = AsyncMock(side_effect=[fact_result, decision_result])

        # create_edge returns an edge info
        graph_linker.create_edge = AsyncMock(return_value=GraphEdgeInfo(
            source_id=proc_id,
            target_id=fact_id,
            source_type="procedure",
            target_type="fact",
            relation="informed_by",
            weight=0.85,
            auto_linked=True,
        ))

        embedder = AsyncMock()
        embedder.embed = AsyncMock(return_value=[0.1] * 1536)

        handler, _, _, _ = self._make_handler(graph_linker=graph_linker, embedder=embedder)
        event = _make_event(procedure_id=proc_id)

        await handler.handle(event)

        # Should have called create_edge twice (once for fact, once for decision)
        assert graph_linker.create_edge.call_count == 2

        # Verify fact edge call
        fact_call = graph_linker.create_edge.call_args_list[0]
        assert fact_call.kwargs["source_type"] == "procedure"
        assert fact_call.kwargs["target_type"] == "fact"
        assert fact_call.kwargs["relation"] == "informed_by"

        # Verify decision edge call
        decision_call = graph_linker.create_edge.call_args_list[1]
        assert decision_call.kwargs["source_type"] == "procedure"
        assert decision_call.kwargs["target_type"] == "decision"
        assert decision_call.kwargs["relation"] == "caused_by"

        mock_session.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_commit_when_no_matches(self):
        """Should not commit when no edges are created."""
        graph_linker = AsyncMock(spec=GraphLinker)
        mock_session = AsyncMock()
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        graph_linker.db = MagicMock()
        graph_linker.db.session.return_value = mock_cm
        graph_linker.agent_id = "test-agent"

        # Both queries return empty results
        empty_result = MagicMock()
        empty_result.all.return_value = []
        mock_session.execute = AsyncMock(return_value=empty_result)

        embedder = AsyncMock()
        embedder.embed = AsyncMock(return_value=[0.1] * 1536)

        handler, _, _, _ = self._make_handler(graph_linker=graph_linker, embedder=embedder)
        await handler.handle(_make_event())

        mock_session.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_error_isolation(self):
        """Database failures should not propagate."""
        graph_linker = AsyncMock(spec=GraphLinker)
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(side_effect=Exception("DB down"))
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        graph_linker.db = MagicMock()
        graph_linker.db.session.return_value = mock_cm
        graph_linker.agent_id = "test-agent"

        embedder = AsyncMock()
        embedder.embed = AsyncMock(return_value=[0.1] * 1536)

        handler, _, _, _ = self._make_handler(graph_linker=graph_linker, embedder=embedder)

        # Should NOT raise
        await handler.handle(_make_event())

    @pytest.mark.asyncio
    async def test_embed_failure_isolated(self):
        """Embedding failures should not propagate."""
        embedder = AsyncMock()
        embedder.embed = AsyncMock(side_effect=Exception("Embedding service down"))

        handler, graph_linker, _, _ = self._make_handler(embedder=embedder)

        # Should NOT raise
        await handler.handle(_make_event())
        graph_linker.db.session.assert_not_called()

    @pytest.mark.asyncio
    async def test_uses_domain_in_search_text(self):
        """When domain is provided, search text should include it."""
        graph_linker = AsyncMock(spec=GraphLinker)
        mock_session = AsyncMock()
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        graph_linker.db = MagicMock()
        graph_linker.db.session.return_value = mock_cm
        graph_linker.agent_id = "test-agent"

        empty_result = MagicMock()
        empty_result.all.return_value = []
        mock_session.execute = AsyncMock(return_value=empty_result)

        embedder = AsyncMock()
        embedder.embed = AsyncMock(return_value=[0.1] * 1536)

        handler, _, _, _ = self._make_handler(graph_linker=graph_linker, embedder=embedder)
        await handler.handle(_make_event(domain="devops", description="Docker deployment"))

        # Verify embed was called with domain-prefixed text
        embed_call = embedder.embed.call_args[0][0]
        assert "devops" in embed_call
        assert "Docker deployment" in embed_call

    @pytest.mark.asyncio
    async def test_skips_below_threshold(self):
        """Should not create edge when similarity is below threshold."""
        graph_linker = AsyncMock(spec=GraphLinker)
        mock_session = AsyncMock()
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        graph_linker.db = MagicMock()
        graph_linker.db.session.return_value = mock_cm
        graph_linker.agent_id = "test-agent"

        # Return a row with similarity below threshold (0.70)
        low_sim_row = MagicMock()
        low_sim_row.id = uuid4()
        low_sim_row.similarity = 0.65  # Below 0.70 threshold

        fact_result = MagicMock()
        fact_result.all.return_value = [low_sim_row]
        empty_result = MagicMock()
        empty_result.all.return_value = []
        mock_session.execute = AsyncMock(side_effect=[fact_result, empty_result])

        embedder = AsyncMock()
        embedder.embed = AsyncMock(return_value=[0.1] * 1536)

        handler, _, _, _ = self._make_handler(graph_linker=graph_linker, embedder=embedder)
        await handler.handle(_make_event())

        graph_linker.create_edge.assert_not_called()
        mock_session.commit.assert_not_called()
