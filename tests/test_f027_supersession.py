"""Tests for F027 Supersession Detection.

Covers:
- FactSummary.superseded_by field
- apply_supersession_filter (soft suppression)
- track_access / _fire_track_access
- _classify_fact_pair (LLM classifier)
- _find_contradiction LLM routing
- Sleep handler phases (stale_scan, cluster_consolidation)
- Sleep phase registration
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from nous.heart.facts import FactManager
from nous.heart.schemas import FactSummary


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_summary(
    *,
    id: UUID | None = None,
    score: float | None = 1.0,
    superseded_by: UUID | None = None,
    confidence: float = 0.9,
    active: bool = True,
) -> FactSummary:
    return FactSummary(
        id=id or uuid4(),
        content="test fact",
        category="technical",
        subject="test",
        confidence=confidence,
        active=active,
        score=score,
        superseded_by=superseded_by,
    )


def _mock_llm_response(result_dict: dict) -> MagicMock:
    """Create a mock LLM client that returns a tool_use block."""
    response = MagicMock()
    response.content = [
        {"type": "tool_use", "id": "call_1", "name": "classify_facts", "input": result_dict}
    ]
    client = AsyncMock()
    client.call = AsyncMock(return_value=response)
    return client


# ===========================================================================
# TestFactSummarySupersededBy
# ===========================================================================

class TestFactSummarySupersededBy:
    """Test that superseded_by field exists on FactSummary."""

    def test_field_present(self):
        uid = uuid4()
        s = _make_summary(superseded_by=uid)
        assert s.superseded_by == uid

    def test_defaults_none(self):
        s = FactSummary(
            id=uuid4(),
            content="x",
            category="technical",
            subject="x",
            confidence=1.0,
            active=True,
        )
        assert s.superseded_by is None


# ===========================================================================
# TestApplySupersessionFilter
# ===========================================================================

class TestApplySupersessionFilter:
    """Test soft suppression filter on search results."""

    def test_no_superseded_unchanged(self):
        results = [_make_summary(score=0.9), _make_summary(score=0.8)]
        filtered = FactManager.apply_supersession_filter(results)
        assert len(filtered) == 2
        assert filtered[0].score == 0.9

    def test_drop_when_superseder_present(self):
        new_id = uuid4()
        old_id = uuid4()
        old = _make_summary(id=old_id, score=0.8, superseded_by=new_id)
        new = _make_summary(id=new_id, score=0.9)
        filtered = FactManager.apply_supersession_filter([old, new])
        assert len(filtered) == 1
        assert filtered[0].id == new_id

    def test_soft_penalty_when_superseder_absent(self):
        absent_id = uuid4()
        s = _make_summary(score=1.0, superseded_by=absent_id)
        filtered = FactManager.apply_supersession_filter([s])
        assert len(filtered) == 1
        assert filtered[0].score == pytest.approx(0.3)

    def test_low_confidence_penalty(self):
        s = _make_summary(score=1.0, confidence=0.3)
        filtered = FactManager.apply_supersession_filter([s])
        assert len(filtered) == 1
        assert filtered[0].score == pytest.approx(0.3)

    def test_threshold_boundary_no_penalty(self):
        """Confidence exactly 0.5 should NOT trigger penalty."""
        s = _make_summary(score=1.0, confidence=0.5)
        filtered = FactManager.apply_supersession_filter([s])
        assert filtered[0].score == pytest.approx(1.0)

    def test_resort_by_adjusted_score(self):
        absent_id = uuid4()
        s1 = _make_summary(score=0.5, superseded_by=absent_id)  # -> 0.15
        s2 = _make_summary(score=0.4)  # -> 0.4
        filtered = FactManager.apply_supersession_filter([s1, s2])
        assert filtered[0].score == pytest.approx(0.4)
        assert filtered[1].score == pytest.approx(0.15)

    def test_empty_input(self):
        assert FactManager.apply_supersession_filter([]) == []

    def test_none_score_treated_as_zero(self):
        absent_id = uuid4()
        s = _make_summary(score=None, superseded_by=absent_id)
        filtered = FactManager.apply_supersession_filter([s])
        assert filtered[0].score == pytest.approx(0.0)


# ===========================================================================
# TestTrackAccess
# ===========================================================================

class TestTrackAccess:
    """Test access tracking."""

    @pytest.mark.asyncio
    async def test_empty_list_noop(self):
        db = MagicMock()
        fm = FactManager(db=db, embeddings=None, agent_id="test")
        await fm.track_access([])
        db.session.assert_not_called()

    @pytest.mark.asyncio
    async def test_executes_update(self):
        session = AsyncMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        db = MagicMock()
        db.session = MagicMock(return_value=session)

        fm = FactManager(db=db, embeddings=None, agent_id="test")
        await fm.track_access([uuid4()])
        session.execute.assert_called_once()
        session.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_swallows_exceptions(self):
        db = MagicMock()
        db.session = MagicMock(side_effect=Exception("db error"))
        fm = FactManager(db=db, embeddings=None, agent_id="test")
        # Should not raise
        await fm.track_access([uuid4()])


# ===========================================================================
# TestClassifyFactPair
# ===========================================================================

class TestClassifyFactPair:
    """Test LLM fact pair classifier."""

    @pytest.mark.asyncio
    async def test_returns_none_without_llm(self):
        fm = FactManager(db=MagicMock(), embeddings=None, agent_id="test")
        result = await fm._classify_fact_pair("old", "new")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_classification(self):
        expected = {"relation": "UPDATE", "current_fact": "new", "confidence": 0.9}
        client = _mock_llm_response(expected)
        fm = FactManager(db=MagicMock(), embeddings=None, agent_id="test")
        fm.set_llm_client(client)
        result = await fm._classify_fact_pair("old content", "new content")
        assert result == expected

    @pytest.mark.asyncio
    async def test_returns_none_on_failure(self):
        client = AsyncMock()
        client.call = AsyncMock(side_effect=Exception("LLM error"))
        fm = FactManager(db=MagicMock(), embeddings=None, agent_id="test")
        fm.set_llm_client(client)
        result = await fm._classify_fact_pair("old", "new")
        assert result is None


# ===========================================================================
# TestFindContradictionLLMRouting
# ===========================================================================

class TestFindContradictionLLMRouting:
    """Test _find_contradiction with LLM routing."""

    def _make_fm(self, llm_result: dict | None = None) -> FactManager:
        fm = FactManager(db=MagicMock(), embeddings=None, agent_id="test")
        if llm_result is not None:
            client = _mock_llm_response(llm_result)
            fm.set_llm_client(client)
        return fm

    @pytest.mark.asyncio
    async def test_no_candidate_returns_none(self):
        """When SQL returns no row, result is None."""
        fm = self._make_fm()
        session = AsyncMock()
        result_mock = MagicMock()
        result_mock.first.return_value = None
        session.execute = AsyncMock(return_value=result_mock)

        result = await fm._find_contradiction(
            embedding=[0.1] * 1536,
            new_content="test",
            exclude_ids=[],
            session=session,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_unrelated_returns_none(self):
        """UNRELATED classification -> return None (no contradiction)."""
        fm = self._make_fm({"relation": "UNRELATED", "current_fact": "new", "confidence": 0.9})
        session = AsyncMock()
        row = SimpleNamespace(id=uuid4(), content="existing", similarity=0.88)
        result_mock = MagicMock()
        result_mock.first.return_value = row
        session.execute = AsyncMock(return_value=result_mock)

        fm._create_graph_edge = AsyncMock()
        fm._get_fact_orm = AsyncMock(return_value=None)

        result = await fm._find_contradiction(
            embedding=[0.1] * 1536,
            new_content="test",
            exclude_ids=[],
            session=session,
            new_fact_id=uuid4(),
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_refinement_creates_edge_returns_none(self):
        """REFINEMENT -> create refines edge, return None."""
        fm = self._make_fm({"relation": "REFINEMENT", "current_fact": "new", "confidence": 0.9})
        session = AsyncMock()
        existing_id = uuid4()
        row = SimpleNamespace(id=existing_id, content="existing", similarity=0.88)
        result_mock = MagicMock()
        result_mock.first.return_value = row
        session.execute = AsyncMock(return_value=result_mock)

        fm._create_graph_edge = AsyncMock()
        new_fact_id = uuid4()

        result = await fm._find_contradiction(
            embedding=[0.1] * 1536,
            new_content="test",
            exclude_ids=[],
            session=session,
            new_fact_id=new_fact_id,
        )
        assert result is None
        fm._create_graph_edge.assert_called_once_with(
            new_fact_id, existing_id, "fact", "fact", "refines", 0.8, session,
        )

    @pytest.mark.asyncio
    async def test_contradiction_returns_warning_with_edge(self):
        """CONTRADICTION -> create edge, reduce confidence, return warning."""
        fm = self._make_fm({"relation": "CONTRADICTION", "current_fact": "new", "confidence": 0.9})
        session = AsyncMock()
        existing_id = uuid4()
        row = SimpleNamespace(id=existing_id, content="existing fact", similarity=0.88)
        result_mock = MagicMock()
        result_mock.first.return_value = row
        session.execute = AsyncMock(return_value=result_mock)

        new_fact_id = uuid4()
        new_fact_orm = MagicMock()
        new_fact_orm.contradiction_of = None
        old_fact_orm = MagicMock()
        old_fact_orm.confidence = 0.8

        async def get_fact_orm(fid, _session):
            if fid == new_fact_id:
                return new_fact_orm
            return old_fact_orm

        fm._get_fact_orm = AsyncMock(side_effect=get_fact_orm)
        fm._create_graph_edge = AsyncMock()

        result = await fm._find_contradiction(
            embedding=[0.1] * 1536,
            new_content="contradicting",
            exclude_ids=[],
            session=session,
            new_fact_id=new_fact_id,
        )
        assert result is not None
        assert result.existing_fact_id == existing_id
        # Should have set contradiction_of on new fact
        assert new_fact_orm.contradiction_of == existing_id
        # Should have reduced old fact confidence
        assert old_fact_orm.confidence == pytest.approx(0.6)
        fm._create_graph_edge.assert_called_once()

    @pytest.mark.asyncio
    async def test_low_conf_update_returns_warning(self):
        """UPDATE with low confidence -> fall through to ContradictionWarning."""
        fm = self._make_fm({"relation": "UPDATE", "current_fact": "new", "confidence": 0.5})
        session = AsyncMock()
        existing_id = uuid4()
        row = SimpleNamespace(id=existing_id, content="existing", similarity=0.88)
        result_mock = MagicMock()
        result_mock.first.return_value = row
        session.execute = AsyncMock(return_value=result_mock)

        fm._create_graph_edge = AsyncMock()
        fm._get_fact_orm = AsyncMock(return_value=None)

        result = await fm._find_contradiction(
            embedding=[0.1] * 1536,
            new_content="test",
            exclude_ids=[],
            session=session,
            new_fact_id=uuid4(),
        )
        assert result is not None
        assert result.existing_fact_id == existing_id

    @pytest.mark.asyncio
    async def test_no_llm_returns_warning(self):
        """Without LLM, always return ContradictionWarning when row found."""
        fm = self._make_fm()  # No LLM
        session = AsyncMock()
        existing_id = uuid4()
        row = SimpleNamespace(id=existing_id, content="existing", similarity=0.88)
        result_mock = MagicMock()
        result_mock.first.return_value = row
        session.execute = AsyncMock(return_value=result_mock)

        result = await fm._find_contradiction(
            embedding=[0.1] * 1536,
            new_content="test",
            exclude_ids=[],
            session=session,
        )
        assert result is not None
        assert result.existing_fact_id == existing_id


# ===========================================================================
# TestPhaseStaleScam
# ===========================================================================

class TestPhaseStaleScam:
    """Test _phase_stale_scan in SleepHandler."""

    def _make_handler(self):
        from nous.handlers.sleep_handler import SleepHandler
        brain = MagicMock()
        heart = MagicMock()
        settings = MagicMock()
        settings.background_model = "test-model"
        bus = MagicMock()
        bus.on = MagicMock()
        handler = SleepHandler(brain, heart, settings, bus, llm_client=None)
        # _phase_stale_scan reads settings off heart (not the handler's
        # injected settings) — supply numeric/list values explicitly
        # because MagicMock attribute access returns a MagicMock, which
        # breaks `timedelta(days=...)` and `notin_(...)`.
        heart.settings.stale_scan_age_days = 60
        heart.settings.stale_scan_excluded_categories = ["rule"]
        heart.settings.cluster_consolidation_min_facts = 3
        heart.settings.cluster_consolidation_max_facts = 10
        return handler, heart

    @pytest.mark.asyncio
    async def test_no_stale_returns_true(self):
        handler, heart = self._make_handler()

        session = AsyncMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        scalars_mock = MagicMock()
        scalars_mock.all.return_value = []
        result_mock = MagicMock()
        result_mock.scalars.return_value = scalars_mock
        session.execute = AsyncMock(return_value=result_mock)

        heart.db.session = MagicMock(return_value=session)
        heart.agent_id = "test"

        stats = {}
        result = await handler._phase_stale_scan(stats)
        assert result is True
        assert stats["stale_deactivated"] == 0

    @pytest.mark.asyncio
    async def test_deactivates_stale(self):
        handler, heart = self._make_handler()

        fake_fact = MagicMock()
        fake_fact.active = True

        session = AsyncMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        scalars_mock = MagicMock()
        scalars_mock.all.return_value = [fake_fact]
        result_mock = MagicMock()
        result_mock.scalars.return_value = scalars_mock
        session.execute = AsyncMock(return_value=result_mock)

        heart.db.session = MagicMock(return_value=session)
        heart.agent_id = "test"

        stats = {}
        result = await handler._phase_stale_scan(stats)
        assert result is True
        assert stats["stale_deactivated"] == 1
        assert fake_fact.active is False

    @pytest.mark.asyncio
    async def test_exception_returns_false(self):
        handler, heart = self._make_handler()
        heart.db.session = MagicMock(side_effect=Exception("db error"))
        heart.agent_id = "test"

        stats = {}
        result = await handler._phase_stale_scan(stats)
        assert result is False


# ===========================================================================
# TestPhaseClusterConsolidation
# ===========================================================================

class TestPhaseClusterConsolidation:
    """Test _phase_cluster_consolidation in SleepHandler."""

    def _make_handler(self, llm_client=None):
        from nous.handlers.sleep_handler import SleepHandler
        brain = MagicMock()
        heart = MagicMock()
        settings = MagicMock()
        settings.background_model = "test-model"
        bus = MagicMock()
        bus.on = MagicMock()
        handler = SleepHandler(brain, heart, settings, bus, llm_client=llm_client)
        return handler, heart

    @pytest.mark.asyncio
    async def test_no_llm_returns_true(self):
        handler, _ = self._make_handler(llm_client=None)
        stats = {}
        result = await handler._phase_cluster_consolidation(stats)
        assert result is True

    @pytest.mark.asyncio
    async def test_no_clusters_returns_true(self):
        client = _mock_llm_response({})
        handler, heart = self._make_handler(llm_client=client)

        session = AsyncMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        result_mock = MagicMock()
        result_mock.all.return_value = []
        session.execute = AsyncMock(return_value=result_mock)

        heart.db.session = MagicMock(return_value=session)
        heart.agent_id = "test"

        stats = {}
        result = await handler._phase_cluster_consolidation(stats)
        assert result is True
        assert stats["clusters_merged"] == 0

    @pytest.mark.asyncio
    async def test_exception_returns_false(self):
        client = _mock_llm_response({})
        handler, heart = self._make_handler(llm_client=client)
        heart.db.session = MagicMock(side_effect=Exception("db error"))
        heart.agent_id = "test"

        stats = {}
        result = await handler._phase_cluster_consolidation(stats)
        assert result is False

    @pytest.mark.asyncio
    async def test_happy_path_merge(self):
        merge_result = {"merged_content": "consolidated fact", "confidence": 0.85}
        client = _mock_llm_response(merge_result)
        handler, heart = self._make_handler(llm_client=client)
        handler._interrupted = False

        # First session: cluster query
        cluster_session = AsyncMock()
        cluster_session.__aenter__ = AsyncMock(return_value=cluster_session)
        cluster_session.__aexit__ = AsyncMock(return_value=False)
        cluster_result = MagicMock()
        cluster_result.all.return_value = [("test_subject", 3)]
        cluster_session.execute = AsyncMock(return_value=cluster_result)

        # Second session: fact fetch
        fact1 = MagicMock()
        fact1.id = uuid4()
        fact1.category = "technical"
        fact1.content = "fact 1"
        fact1.created_at = datetime.now(UTC)
        fact2 = MagicMock()
        fact2.id = uuid4()
        fact2.category = "technical"
        fact2.content = "fact 2"
        fact2.created_at = datetime.now(UTC)
        fact3 = MagicMock()
        fact3.id = uuid4()
        fact3.category = "technical"
        fact3.content = "fact 3"
        fact3.created_at = datetime.now(UTC)

        fact_session = AsyncMock()
        fact_session.__aenter__ = AsyncMock(return_value=fact_session)
        fact_session.__aexit__ = AsyncMock(return_value=False)
        fact_scalars = MagicMock()
        fact_scalars.all.return_value = [fact1, fact2, fact3]
        fact_result = MagicMock()
        fact_result.scalars.return_value = fact_scalars
        fact_session.execute = AsyncMock(return_value=fact_result)

        # Third session: deactivate originals
        deactivate_session = AsyncMock()
        deactivate_session.__aenter__ = AsyncMock(return_value=deactivate_session)
        deactivate_session.__aexit__ = AsyncMock(return_value=False)
        deactivate_session.get = AsyncMock(return_value=MagicMock())

        # Mock learn to return a FactDetail-like object
        merged_detail = MagicMock()
        merged_detail.id = uuid4()
        heart.learn = AsyncMock(return_value=merged_detail)

        # db.session returns different sessions on each call
        call_count = 0

        def session_factory():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return cluster_session
            elif call_count == 2:
                return fact_session
            else:
                return deactivate_session

        heart.db.session = session_factory
        heart.agent_id = "test"

        stats = {}
        result = await handler._phase_cluster_consolidation(stats)
        assert result is True
        assert stats["clusters_merged"] == 1
        heart.learn.assert_called_once()


# ===========================================================================
# TestSleepPhaseRegistration
# ===========================================================================

class TestSleepPhaseRegistration:
    """Test that F027 phases are registered in _run_sleep."""

    def _make_handler(self):
        from nous.handlers.sleep_handler import SleepHandler
        brain = MagicMock()
        heart = MagicMock()
        settings = MagicMock()
        settings.background_model = "test-model"
        bus = MagicMock()
        bus.on = MagicMock()
        bus.emit = AsyncMock()
        handler = SleepHandler(brain, heart, settings, bus, llm_client=None)
        return handler

    @pytest.mark.asyncio
    async def test_stale_scan_in_phases(self):
        handler = self._make_handler()
        # Stub all phases to succeed
        handler._phase_review_decisions = AsyncMock(return_value=True)
        handler._phase_prune = AsyncMock(return_value=True)
        handler._phase_compress = AsyncMock(return_value=True)
        handler._phase_reflect = AsyncMock(return_value=True)
        handler._phase_resolve_contradictions = AsyncMock(return_value=True)
        handler._phase_stale_scan = AsyncMock(return_value=True)
        handler._phase_cluster_consolidation = AsyncMock(return_value=True)
        handler._phase_generalize = AsyncMock(return_value=True)
        handler._phase_evolve_rubric = AsyncMock(return_value=True)

        event = MagicMock()
        event.agent_id = "test"
        event.trace_id = None
        event.event_id = uuid4()

        await handler._run_sleep(event)
        handler._phase_stale_scan.assert_called_once()

    @pytest.mark.asyncio
    async def test_cluster_consolidation_in_phases(self):
        handler = self._make_handler()
        handler._phase_review_decisions = AsyncMock(return_value=True)
        handler._phase_prune = AsyncMock(return_value=True)
        handler._phase_compress = AsyncMock(return_value=True)
        handler._phase_reflect = AsyncMock(return_value=True)
        handler._phase_resolve_contradictions = AsyncMock(return_value=True)
        handler._phase_stale_scan = AsyncMock(return_value=True)
        handler._phase_cluster_consolidation = AsyncMock(return_value=True)
        handler._phase_generalize = AsyncMock(return_value=True)
        handler._phase_evolve_rubric = AsyncMock(return_value=True)

        event = MagicMock()
        event.agent_id = "test"
        event.trace_id = None
        event.event_id = uuid4()

        await handler._run_sleep(event)
        handler._phase_cluster_consolidation.assert_called_once()
