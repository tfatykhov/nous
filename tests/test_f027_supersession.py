"""Tests for F027 Supersession Detection.

Covers:
- Step 1: Access tracking (track_access, _fire_track_access)
- Step 2: LLM write-time classifier (_classify_fact_pair, _find_contradiction routes,
          _supersede_by_subject routes)
- Step 3: Retrieval soft suppression (apply_supersession_filter)
- Step 4: Sleep-time stale scan (_phase_stale_scan) and cluster consolidation
          (_phase_cluster_consolidation)
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from nous.heart.schemas import FactSummary

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fact_summary(
    *,
    id: uuid.UUID | None = None,
    content: str = "Test fact content here",
    category: str | None = "technical",
    subject: str | None = "test",
    confidence: float = 1.0,
    active: bool = True,
    score: float = 0.8,
    superseded_by: uuid.UUID | None = None,
) -> FactSummary:
    return FactSummary(
        id=id or uuid.uuid4(),
        content=content,
        category=category,
        subject=subject,
        confidence=confidence,
        active=active,
        score=score,
        superseded_by=superseded_by,
    )


def _mock_llm_structured(tool_input: dict, tool_name: str = "classify_fact_relationship") -> AsyncMock:
    """Create a mock LLM client returning a structured tool_use response."""
    client = AsyncMock()
    response = MagicMock()
    response.content = [
        {"type": "tool_use", "id": "toolu_mock", "name": tool_name, "input": tool_input}
    ]
    client.call = AsyncMock(return_value=response)
    return client


def _mock_llm_no_tool_use() -> AsyncMock:
    """Mock LLM client that returns no tool_use block (simulates failure)."""
    client = AsyncMock()
    response = MagicMock()
    response.content = [{"type": "text", "text": "I don't know"}]
    client.call = AsyncMock(return_value=response)
    return client


def _make_sleep_handler(llm_client=None, heart=None, settings=None, bus=None, brain=None):
    from nous.events import EventBus
    from nous.handlers.sleep_handler import SleepHandler

    brain = brain or AsyncMock()
    heart = heart or AsyncMock()
    settings = settings or _mock_settings()
    bus = bus or MagicMock(spec=EventBus)
    bus.on = MagicMock()
    bus.emit = AsyncMock()
    handler = SleepHandler(brain, heart, settings, bus, llm_client)
    return handler, brain, heart, bus, llm_client


def _mock_settings(**overrides) -> MagicMock:
    s = MagicMock()
    s.background_model = "claude-haiku-4-5-20251001"
    s.agent_id = "test-agent"
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


# ===========================================================================
# Step 3: apply_supersession_filter (pure function, no DB needed)
# ===========================================================================


class TestApplySupersessionFilter:
    """apply_supersession_filter penalises superseded and low-confidence facts."""

    def _filter(self, results):
        from nous.heart.facts import FactManager
        return FactManager.apply_supersession_filter(results)

    def test_no_superseded_facts_unchanged(self):
        facts = [
            _make_fact_summary(score=0.9),
            _make_fact_summary(score=0.7),
        ]
        filtered = self._filter(facts)
        assert len(filtered) == 2
        assert filtered[0].score == pytest.approx(0.9)

    def test_superseded_fact_dropped_when_superseder_present(self):
        """If superseder is in results, drop the old fact entirely."""
        new_id = uuid.uuid4()
        old = _make_fact_summary(score=0.85, superseded_by=new_id)
        new = _make_fact_summary(id=new_id, score=0.75)
        filtered = self._filter([old, new])
        ids = {r.id for r in filtered}
        assert old.id not in ids
        assert new.id in ids

    def test_superseded_fact_soft_penalised_when_superseder_absent(self):
        """If superseder is not in results, apply 0.3x penalty."""
        superseder_id = uuid.uuid4()
        old = _make_fact_summary(score=0.8, superseded_by=superseder_id)
        unrelated = _make_fact_summary(score=0.6)
        filtered = self._filter([old, unrelated])
        old_filtered = next(r for r in filtered if r.id == old.id)
        assert old_filtered.score == pytest.approx(0.8 * 0.3)

    def test_low_confidence_fact_penalised(self):
        """Active fact with confidence < 0.5 gets score *= confidence."""
        low_conf = _make_fact_summary(score=0.8, confidence=0.3)
        normal = _make_fact_summary(score=0.7, confidence=1.0)
        filtered = self._filter([low_conf, normal])
        low_filtered = next(r for r in filtered if r.id == low_conf.id)
        assert low_filtered.score == pytest.approx(0.8 * 0.3)

    def test_confidence_at_threshold_not_penalised(self):
        """Fact with confidence == 0.5 is not penalised (< 0.5 threshold)."""
        at_threshold = _make_fact_summary(score=0.8, confidence=0.5)
        filtered = self._filter([at_threshold])
        assert filtered[0].score == pytest.approx(0.8)

    def test_results_sorted_by_adjusted_score(self):
        """After penalties, results are re-sorted by adjusted score."""
        new_id = uuid.uuid4()
        # old has score 0.9 but gets penalised to 0.9*0.3=0.27
        old = _make_fact_summary(score=0.9, superseded_by=new_id)
        new = _make_fact_summary(id=new_id, score=0.5)
        other = _make_fact_summary(score=0.6)
        filtered = self._filter([old, new, other])
        scores = [r.score for r in filtered]
        assert scores == sorted(scores, reverse=True)

    def test_empty_input(self):
        assert self._filter([]) == []

    def test_none_score_handled(self):
        """Facts with score=None are not penalised (guard against multiply with None)."""
        f = _make_fact_summary(score=None, confidence=0.3)
        filtered = self._filter([f])
        assert filtered[0].score is None


# ===========================================================================
# Step 1: Access tracking (unit)
# ===========================================================================


class TestTrackAccess:
    """track_access updates recall_count and last_recalled_at (mocked DB)."""

    @pytest.mark.asyncio
    async def test_track_access_no_ids_is_noop(self):
        """Empty list does nothing."""
        from nous.heart.facts import FactManager

        mock_db = MagicMock()
        fm = FactManager(mock_db, None, "test-agent")
        # Should not raise or call db
        await fm.track_access([])
        mock_db.session.assert_not_called()

    @pytest.mark.asyncio
    async def test_track_access_executes_update(self):
        """track_access calls db.session and issues an UPDATE statement."""
        from nous.heart.facts import FactManager

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_db = MagicMock()
        mock_db.session = MagicMock(return_value=mock_session)

        fm = FactManager(mock_db, None, "test-agent")
        fact_ids = [uuid.uuid4(), uuid.uuid4()]
        await fm.track_access(fact_ids)

        mock_session.execute.assert_called_once()
        mock_session.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_track_access_swallows_db_exception(self):
        """DB error in track_access is logged but not re-raised."""
        from nous.heart.facts import FactManager

        mock_db = MagicMock()
        mock_db.session = MagicMock(side_effect=RuntimeError("db gone"))

        fm = FactManager(mock_db, None, "test-agent")
        # Should not raise
        await fm.track_access([uuid.uuid4()])


# ===========================================================================
# Step 2: LLM write-time classifier (unit — _classify_fact_pair)
# ===========================================================================


class TestClassifyFactPair:
    """_classify_fact_pair returns structured dict from LLM or None."""

    @pytest.mark.asyncio
    async def test_returns_none_without_llm(self):
        from nous.heart.facts import FactManager

        fm = FactManager(MagicMock(), None, "test-agent")
        # _llm is None by default
        result = await fm._classify_fact_pair("old fact content here", "new fact content here")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_classification_dict(self):
        from nous.heart.facts import FactManager

        mock_llm = _mock_llm_structured(
            {"relation": "UPDATE", "current_fact": "new", "confidence": 0.92}
        )
        fm = FactManager(MagicMock(), None, "test-agent")
        fm._llm = mock_llm

        result = await fm._classify_fact_pair(
            "Tim lives in Washington DC",
            "Tim moved to Silver Spring, MD",
        )
        assert result is not None
        assert result["relation"] == "UPDATE"
        assert result["current_fact"] == "new"
        assert result["confidence"] == pytest.approx(0.92)

    @pytest.mark.asyncio
    async def test_returns_none_on_llm_failure(self):
        from nous.heart.facts import FactManager

        mock_llm = _mock_llm_no_tool_use()
        fm = FactManager(MagicMock(), None, "test-agent")
        fm._llm = mock_llm

        result = await fm._classify_fact_pair("fact a", "fact b")
        assert result is None


# ===========================================================================
# Step 2: _find_contradiction routing with LLM
# ===========================================================================


class TestFindContradictionLLMRouting:
    """_find_contradiction uses LLM to route UPDATE/CONTRADICTION/REFINEMENT/UNRELATED."""

    def _make_fact_manager(self, llm_input: dict | None = None):
        """Return a FactManager with mocked DB and optional LLM client."""
        from nous.heart.facts import FactManager

        fm = FactManager(MagicMock(), None, "test-agent")
        if llm_input is not None:
            fm._llm = _mock_llm_structured(llm_input)
        return fm

    @pytest.mark.asyncio
    async def test_no_candidate_returns_none(self):
        """No matching fact in DB → None (no LLM call needed)."""
        from nous.heart.facts import FactManager

        fm = FactManager(MagicMock(), None, "test-agent")
        mock_session = AsyncMock()
        # Simulate empty result from DB query
        mock_result = MagicMock()
        mock_result.first = MagicMock(return_value=None)
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await fm._find_contradiction(
            [0.1] * 1536, "new content here for testing", [], mock_session
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_unrelated_returns_none(self):
        """LLM classifies UNRELATED → no warning returned."""
        from nous.heart.facts import FactManager

        fm = FactManager(MagicMock(), None, "test-agent")
        fm._llm = _mock_llm_structured({"relation": "UNRELATED", "current_fact": "new", "confidence": 0.9})

        old_id = uuid.uuid4()
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_row = MagicMock()
        mock_row.id = old_id
        mock_row.content = "Some unrelated fact"
        mock_row.similarity = 0.88
        mock_result.first = MagicMock(return_value=mock_row)
        mock_session.execute = AsyncMock(return_value=mock_result)

        new_id = uuid.uuid4()
        result = await fm._find_contradiction(
            [0.1] * 1536, "different topic content here", [], mock_session, new_fact_id=new_id
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_refinement_creates_edge_returns_none(self):
        """LLM classifies REFINEMENT → refines edge created, no warning."""
        from nous.heart.facts import FactManager

        fm = FactManager(MagicMock(), None, "test-agent")
        fm._llm = _mock_llm_structured({"relation": "REFINEMENT", "current_fact": "new", "confidence": 0.85})

        old_id = uuid.uuid4()
        new_id = uuid.uuid4()
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_row = MagicMock()
        mock_row.id = old_id
        mock_row.content = "Base fact with some info"
        mock_row.similarity = 0.88
        mock_result.first = MagicMock(return_value=mock_row)
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.begin_nested = MagicMock()
        mock_nested = AsyncMock()
        mock_nested.__aenter__ = AsyncMock(return_value=mock_nested)
        mock_nested.__aexit__ = AsyncMock(return_value=False)
        mock_session.begin_nested = MagicMock(return_value=mock_nested)

        result = await fm._find_contradiction(
            [0.1] * 1536, "refined fact with more detail here", [], mock_session, new_fact_id=new_id
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_contradiction_returns_warning(self):
        """LLM classifies CONTRADICTION → ContradictionWarning returned."""
        from nous.heart.facts import FactManager
        from nous.heart.schemas import ContradictionWarning

        fm = FactManager(MagicMock(), None, "test-agent")
        fm._llm = _mock_llm_structured({"relation": "CONTRADICTION", "current_fact": "new", "confidence": 0.9})

        old_id = uuid.uuid4()
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_row = MagicMock()
        mock_row.id = old_id
        mock_row.content = "The opposite is true"
        mock_row.similarity = 0.88
        mock_result.first = MagicMock(return_value=mock_row)
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await fm._find_contradiction(
            [0.1] * 1536, "contradicting fact content", [], mock_session, new_fact_id=uuid.uuid4()
        )
        assert isinstance(result, ContradictionWarning)
        assert result.existing_fact_id == old_id

    @pytest.mark.asyncio
    async def test_low_confidence_update_defers_to_f031(self):
        """UPDATE with confidence < 0.8 → ContradictionWarning (deferred to F031)."""
        from nous.heart.facts import FactManager
        from nous.heart.schemas import ContradictionWarning

        fm = FactManager(MagicMock(), None, "test-agent")
        fm._llm = _mock_llm_structured({"relation": "UPDATE", "current_fact": "new", "confidence": 0.65})

        old_id = uuid.uuid4()
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_row = MagicMock()
        mock_row.id = old_id
        mock_row.content = "Possibly outdated info"
        mock_row.similarity = 0.90
        mock_result.first = MagicMock(return_value=mock_row)
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await fm._find_contradiction(
            [0.1] * 1536, "possibly newer info here", [], mock_session, new_fact_id=uuid.uuid4()
        )
        assert isinstance(result, ContradictionWarning)

    @pytest.mark.asyncio
    async def test_no_llm_returns_contradiction_warning(self):
        """Without LLM, existing behavior: returns ContradictionWarning."""
        from nous.heart.facts import FactManager
        from nous.heart.schemas import ContradictionWarning

        fm = FactManager(MagicMock(), None, "test-agent")
        # No _llm set

        old_id = uuid.uuid4()
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_row = MagicMock()
        mock_row.id = old_id
        mock_row.content = "Old fact content here"
        mock_row.similarity = 0.88
        mock_result.first = MagicMock(return_value=mock_row)
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await fm._find_contradiction(
            [0.1] * 1536, "somewhat different content here", [], mock_session
        )
        assert isinstance(result, ContradictionWarning)


# ===========================================================================
# Step 4: _phase_stale_scan (unit — mocked DB via heart)
# ===========================================================================


class TestPhaseStaleScam:
    """_phase_stale_scan deactivates superseded+low-conf+unrecalled facts."""

    @pytest.mark.asyncio
    async def test_returns_true_no_stale_facts(self):
        """No stale facts → True, stats set to 0."""
        from nous.events import EventBus
        from nous.handlers.sleep_handler import SleepHandler

        # Mock heart with db that returns no stale facts
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_result = MagicMock()
        mock_result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        mock_session.execute = AsyncMock(return_value=mock_result)

        mock_db = MagicMock()
        mock_db.session = MagicMock(return_value=mock_session)

        mock_heart = MagicMock()
        mock_heart.db = mock_db
        mock_heart.agent_id = "test-agent"

        bus = MagicMock(spec=EventBus)
        bus.on = MagicMock()
        bus.emit = AsyncMock()

        handler = SleepHandler(MagicMock(), mock_heart, _mock_settings(), bus, None)

        sleep_stats: dict = {}
        result = await handler._phase_stale_scan(sleep_stats)
        assert result is True
        assert sleep_stats["stale_deactivated"] == 0

    @pytest.mark.asyncio
    async def test_deactivates_stale_facts(self):
        """Stale facts get active=False and stat is incremented."""
        from nous.events import EventBus
        from nous.handlers.sleep_handler import SleepHandler
        from nous.storage.models import Fact

        # Build a mock fact that is active
        mock_fact = MagicMock(spec=Fact)
        mock_fact.active = True

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_result = MagicMock()
        mock_result.scalars = MagicMock(
            return_value=MagicMock(all=MagicMock(return_value=[mock_fact]))
        )
        mock_session.execute = AsyncMock(return_value=mock_result)

        mock_db = MagicMock()
        mock_db.session = MagicMock(return_value=mock_session)

        mock_heart = MagicMock()
        mock_heart.db = mock_db
        mock_heart.agent_id = "test-agent"

        bus = MagicMock(spec=EventBus)
        bus.on = MagicMock()
        bus.emit = AsyncMock()

        handler = SleepHandler(MagicMock(), mock_heart, _mock_settings(), bus, None)

        sleep_stats: dict = {}
        result = await handler._phase_stale_scan(sleep_stats)
        assert result is True
        assert sleep_stats["stale_deactivated"] == 1
        assert mock_fact.active is False

    @pytest.mark.asyncio
    async def test_returns_false_on_exception(self):
        """DB error → returns False."""
        from nous.events import EventBus
        from nous.handlers.sleep_handler import SleepHandler

        mock_db = MagicMock()
        mock_db.session = MagicMock(side_effect=RuntimeError("db error"))

        mock_heart = MagicMock()
        mock_heart.db = mock_db
        mock_heart.agent_id = "test-agent"

        bus = MagicMock(spec=EventBus)
        bus.on = MagicMock()
        bus.emit = AsyncMock()

        handler = SleepHandler(MagicMock(), mock_heart, _mock_settings(), bus, None)

        sleep_stats: dict = {}
        result = await handler._phase_stale_scan(sleep_stats)
        assert result is False


# ===========================================================================
# Step 4: _phase_cluster_consolidation (unit)
# ===========================================================================


class TestPhaseClusterConsolidation:
    """_phase_cluster_consolidation merges 3+ facts with same subject."""

    @pytest.mark.asyncio
    async def test_returns_true_no_llm(self):
        """No LLM client → no-op returns True."""
        handler, *_ = _make_sleep_handler(llm_client=None)
        handler._llm = None
        sleep_stats: dict = {}
        result = await handler._phase_cluster_consolidation(sleep_stats)
        assert result is True

    @pytest.mark.asyncio
    async def test_returns_true_no_clusters(self):
        """No clusters found → True, clusters_merged=0."""
        mock_llm = _mock_llm_structured(
            {"merged_content": "Merged", "confidence": 0.8}, "merge_facts"
        )

        # Mock heart.db to return empty clusters
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_result = MagicMock()
        mock_result.all = MagicMock(return_value=[])
        mock_session.execute = AsyncMock(return_value=mock_result)

        mock_db = MagicMock()
        mock_db.session = MagicMock(return_value=mock_session)

        mock_heart = MagicMock()
        mock_heart.db = mock_db
        mock_heart.agent_id = "test-agent"

        handler, _, _, bus, _ = _make_sleep_handler(llm_client=mock_llm, heart=mock_heart)

        sleep_stats: dict = {}
        result = await handler._phase_cluster_consolidation(sleep_stats)
        assert result is True
        assert sleep_stats["clusters_merged"] == 0

    @pytest.mark.asyncio
    async def test_returns_false_on_exception(self):
        """DB error → returns False."""
        from nous.events import EventBus

        mock_llm = _mock_llm_structured({"merged_content": "Merged", "confidence": 0.8}, "merge_facts")

        mock_db = MagicMock()
        mock_db.session = MagicMock(side_effect=RuntimeError("db error"))

        mock_heart = MagicMock()
        mock_heart.db = mock_db
        mock_heart.agent_id = "test-agent"

        bus = MagicMock(spec=EventBus)
        bus.on = MagicMock()
        bus.emit = AsyncMock()

        from nous.handlers.sleep_handler import SleepHandler
        handler = SleepHandler(MagicMock(), mock_heart, _mock_settings(), bus, mock_llm)

        sleep_stats: dict = {}
        result = await handler._phase_cluster_consolidation(sleep_stats)
        assert result is False

    @pytest.mark.asyncio
    async def test_merges_cluster_and_deactivates_originals(self):
        """Happy path: 3 facts merged, originals deactivated."""
        from nous.events import EventBus
        from nous.handlers.sleep_handler import SleepHandler
        from nous.storage.models import Fact

        merged_content = "Comprehensive merged fact about the subject"
        mock_llm = _mock_llm_structured(
            {"merged_content": merged_content, "confidence": 0.85},
            "merge_facts",
        )

        # Three mock facts with the same subject
        facts = []
        for i in range(3):
            f = MagicMock(spec=Fact)
            f.id = uuid.uuid4()
            f.content = f"Fact {i} about test subject with some content"
            f.category = "technical"
            f.active = True
            f.created_at = datetime.now(UTC)
            facts.append(f)

        subject = "test subject"

        # First DB call: find clusters (returns one row)
        cluster_row = MagicMock()
        cluster_row.subject = subject
        cluster_row.cnt = 3

        cluster_result = MagicMock()
        cluster_result.all = MagicMock(return_value=[cluster_row])

        # Second DB call: fetch facts for that subject
        facts_result = MagicMock()
        facts_result.scalars = MagicMock(
            return_value=MagicMock(all=MagicMock(return_value=facts))
        )

        # Third DB call: deactivate originals (each session.get returns the fact)
        deactivation_session = AsyncMock()
        deactivation_session.__aenter__ = AsyncMock(return_value=deactivation_session)
        deactivation_session.__aexit__ = AsyncMock(return_value=False)
        deactivation_session.get = AsyncMock(side_effect=lambda model, id: next(
            (f for f in facts if f.id == id), None
        ))

        call_count = 0

        def make_session():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Cluster query session
                s = AsyncMock()
                s.__aenter__ = AsyncMock(return_value=s)
                s.__aexit__ = AsyncMock(return_value=False)
                s.execute = AsyncMock(return_value=cluster_result)
                return s
            elif call_count == 2:
                # Facts for subject session
                s = AsyncMock()
                s.__aenter__ = AsyncMock(return_value=s)
                s.__aexit__ = AsyncMock(return_value=False)
                s.execute = AsyncMock(return_value=facts_result)
                return s
            else:
                return deactivation_session

        mock_db = MagicMock()
        mock_db.session = MagicMock(side_effect=make_session)

        merged_fact = MagicMock()
        merged_fact.id = uuid.uuid4()

        mock_heart = MagicMock()
        mock_heart.db = mock_db
        mock_heart.agent_id = "test-agent"
        mock_heart.learn = AsyncMock(return_value=merged_fact)

        bus = MagicMock(spec=EventBus)
        bus.on = MagicMock()
        bus.emit = AsyncMock()

        handler = SleepHandler(MagicMock(), mock_heart, _mock_settings(), bus, mock_llm)

        sleep_stats: dict = {}
        result = await handler._phase_cluster_consolidation(sleep_stats)
        assert result is True
        assert sleep_stats["clusters_merged"] == 1
        assert sleep_stats.get("facts_created", 0) >= 1

        # Verify heart.learn was called with the merged content
        mock_heart.learn.assert_called_once()
        call_args = mock_heart.learn.call_args[0][0]
        assert call_args.content == merged_content
        assert call_args.subject == subject
        assert call_args.source == "cluster_consolidation"


# ===========================================================================
# Step 3: superseded_by included in FactSummary (unit)
# ===========================================================================


class TestFactSummarySupersededBy:
    """FactSummary includes the superseded_by field."""

    def test_fact_summary_has_superseded_by_field(self):
        sid = uuid.uuid4()
        fs = FactSummary(
            id=uuid.uuid4(),
            content="A fact about something important",
            category="technical",
            subject="test",
            confidence=1.0,
            active=True,
            score=0.8,
            superseded_by=sid,
        )
        assert fs.superseded_by == sid

    def test_fact_summary_superseded_by_defaults_none(self):
        fs = FactSummary(
            id=uuid.uuid4(),
            content="A fact about something important",
            category="technical",
            subject="test",
            confidence=1.0,
            active=True,
            score=0.8,
        )
        assert fs.superseded_by is None


# ===========================================================================
# Step 4: Phase registration in _run_sleep
# ===========================================================================


class TestSleepPhaseRegistration:
    """New phases are included in phases_completed output."""

    @pytest.mark.asyncio
    async def test_stale_scan_phase_in_completed(self):
        """_phase_stale_scan is called and its name appears in phases_completed."""
        from nous.events import Event, EventBus
        from nous.handlers.sleep_handler import SleepHandler

        bus = MagicMock(spec=EventBus)
        bus.on = MagicMock()
        emitted_events: list = []

        async def capture_emit(event):
            emitted_events.append(event)

        bus.emit = capture_emit

        mock_heart = AsyncMock()
        mock_heart.agent_id = "test-agent"
        mock_heart.db = MagicMock()

        # Make _phase_stale_scan work by having the DB return empty results
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_result = MagicMock()
        mock_result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_heart.db.session = MagicMock(return_value=mock_session)

        mock_brain = AsyncMock()
        mock_brain.list_decisions = AsyncMock(return_value=([], 0))

        settings = _mock_settings()

        handler = SleepHandler(mock_brain, mock_heart, settings, bus, None)

        # Patch the other LLM phases to return True quickly
        handler._phase_compress = AsyncMock(return_value=True)
        handler._phase_reflect = AsyncMock(return_value=True)
        handler._phase_resolve_contradictions = AsyncMock(return_value=True)
        handler._phase_cluster_consolidation = AsyncMock(return_value=True)
        handler._phase_generalize = AsyncMock(return_value=True)
        handler._phase_evolve_rubric = AsyncMock(return_value=True)

        event = Event(type="sleep_started", agent_id="test-agent", data={})
        await handler._run_sleep(event)

        phases = emitted_events[0].data["phases_completed"]
        assert "stale_scan" in phases

    @pytest.mark.asyncio
    async def test_cluster_consolidation_phase_in_completed(self):
        """_phase_cluster_consolidation is called and its name appears in phases_completed."""
        from nous.events import Event, EventBus
        from nous.handlers.sleep_handler import SleepHandler

        bus = MagicMock(spec=EventBus)
        bus.on = MagicMock()
        emitted_events: list = []

        async def capture_emit(event):
            emitted_events.append(event)

        bus.emit = capture_emit

        mock_heart = AsyncMock()
        mock_heart.agent_id = "test-agent"
        mock_heart.db = MagicMock()

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_result = MagicMock()
        mock_result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_heart.db.session = MagicMock(return_value=mock_session)

        mock_brain = AsyncMock()
        mock_brain.list_decisions = AsyncMock(return_value=([], 0))

        settings = _mock_settings()

        handler = SleepHandler(mock_brain, mock_heart, settings, bus, None)

        # Patch phases, but let cluster_consolidation run (no LLM → instant True)
        handler._phase_compress = AsyncMock(return_value=True)
        handler._phase_reflect = AsyncMock(return_value=True)
        handler._phase_resolve_contradictions = AsyncMock(return_value=True)
        handler._phase_stale_scan = AsyncMock(return_value=True)
        handler._phase_generalize = AsyncMock(return_value=True)
        handler._phase_evolve_rubric = AsyncMock(return_value=True)

        event = Event(type="sleep_started", agent_id="test-agent", data={})
        await handler._run_sleep(event)

        phases = emitted_events[0].data["phases_completed"]
        assert "cluster_consolidation" in phases
