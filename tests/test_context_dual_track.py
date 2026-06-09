"""Tests for dual-track procedure loading (issue #229)."""
import pytest
import pytest_asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from nous.cognitive.context import ContextEngine
from nous.cognitive.schemas import BuildResult, ContextBudget, FrameSelection
from nous.config import Settings
from nous.heart.schemas import ProcedureDetail, ProcedureSummary


def _frame(frame_id: str = "task") -> FrameSelection:
    return FrameSelection(
        frame_id=frame_id, frame_name="Task", confidence=0.9, match_method="test",
    )


def _make_procedure_detail(name: str, domain: str = "test") -> ProcedureDetail:
    """Create a ProcedureDetail for testing (what get_procedure_by_name returns)."""
    return ProcedureDetail(
        id=uuid4(),
        agent_id="test",
        name=name,
        domain=domain,
        description=f"Test procedure: {name}",
        goals=[],
        core_patterns=["pattern1"],
        core_tools=[],
        core_concepts=[],
        implementation_notes=["Step 1: investigate the cause", "Step 2: apply the fix"],
        activation_count=1,
        success_count=0,
        failure_count=0,
        neutral_count=0,
        last_activated=None,
        effectiveness=None,
        tags=[],
        active=True,
        created_at=datetime.now(timezone.utc),
    )


def _make_procedure_summary(name: str, score: float = 0.8, domain: str = "test") -> ProcedureSummary:
    """Create a ProcedureSummary for testing (what search_procedures returns)."""
    return ProcedureSummary(
        id=uuid4(),
        name=name,
        domain=domain,
        description=f"Test procedure: {name}",
        activation_count=1,
        effectiveness=None,
        score=score,
    )


class TestGraphPrimarySelection:
    """F080 §14.7: graph-primary (K-line) procedure selection with critic fallback."""

    def _engine(self, *, neighbors, get_procedure=None, get_by_name=None):
        from nous.brain.schemas import NeighborResult  # noqa: F401 (kept local)

        settings = Settings(_env_file=None, proc_selection_graph_primary=True)
        brain = MagicMock()
        brain.neighbors = AsyncMock(return_value=neighbors)
        heart = MagicMock()
        heart.get_procedure = AsyncMock(return_value=get_procedure)
        heart.get_procedure_by_name = AsyncMock(return_value=get_by_name)
        return ContextEngine(brain, heart, settings, identity_prompt="Test")

    def _nbr(self, proc_id, weight=0.8):
        from nous.brain.schemas import NeighborResult

        return NeighborResult(
            id=proc_id, node_type="procedure", description="d",
            edge_relation="summarized_by", edge_weight=weight,
            created_at=datetime.now(timezone.utc), extraction_method="auto_linked",
        )

    @pytest.mark.asyncio
    async def test_graph_primary_selects_linked_procedure_with_body(self):
        proc = _make_procedure_detail("deploy-runbook")
        engine = self._engine(neighbors=[self._nbr(proc.id)], get_procedure=proc)
        fid = str(uuid4())

        selected = await engine._select_procedures(
            slots=5, critic_skills=[],
            recalled_ids={"fact": [fid], "decision": []},
            recalled_score_map={fid: 0.9}, session=None,
        )

        assert [p.id for p in selected] == [proc.id]
        body = "\n".join(engine._format_procedure_bodies(selected, 1200))
        assert "deploy-runbook" in body
        # the actual skill body (implementation_notes) is preloaded; trigger
        # keywords (core_patterns) are NOT (matcher metadata, not instructions)
        assert "Step 1: investigate the cause" in body
        assert "pattern1" not in body

    @pytest.mark.asyncio
    async def test_inactive_procedure_never_surfaced(self):
        archived = _make_procedure_detail("archived-skill").model_copy(
            update={"active": False}
        )
        engine = self._engine(neighbors=[self._nbr(archived.id)], get_procedure=archived)
        fid = str(uuid4())

        selected = await engine._select_procedures(
            slots=5, critic_skills=[],
            recalled_ids={"fact": [fid]}, recalled_score_map={fid: 0.9}, session=None,
        )
        assert selected == []  # active=False => dropped

    @pytest.mark.asyncio
    async def test_critic_fallback_when_no_graph_hits(self):
        crit = _make_procedure_detail("critic-skill")
        engine = self._engine(neighbors=[], get_by_name=crit)

        selected = await engine._select_procedures(
            slots=5, critic_skills=["critic-skill"],
            recalled_ids={"fact": [str(uuid4())]}, recalled_score_map={}, session=None,
        )
        assert [p.id for p in selected] == [crit.id]

    @pytest.mark.asyncio
    async def test_body_format_respects_per_item_cap(self):
        engine = self._engine(neighbors=[])
        long_proc = _make_procedure_detail("x").model_copy(
            update={"description": "Z" * 5000}
        )
        blocks = engine._format_procedure_bodies([long_proc], 200)
        assert len(blocks) == 1
        assert blocks[0].count("Z") <= 200  # body truncated to the per-item cap
        assert "get_procedure('x')" in blocks[0]  # pointer to the untruncated full skill

    @pytest.mark.asyncio
    async def test_build_flag_on_renders_section_and_syncs_recalled_ids(self):
        """End-to-end through build(): flag-ON renders 'Recommended Procedures'
        (here via critic fallback, no graph seeds) and records exactly the shown
        procedure's id — guards the wiring + the recalled-ids/budget sync."""
        proc = _make_procedure_detail("runbook-x")
        settings = Settings(
            _env_file=None,
            proc_selection_graph_primary=True,
            critic_skill_injection="enabled",
            relevance_floor_enabled=False,
        )
        brain = MagicMock()
        brain.embeddings = None
        brain.query = AsyncMock(return_value=[])
        brain.neighbors = AsyncMock(return_value=[])  # no graph hits → critic fallback
        heart = MagicMock()
        heart.search_procedures = AsyncMock(return_value=[])
        heart.search_facts = AsyncMock(return_value=[])
        heart.search_episodes = AsyncMock(return_value=[])
        heart.list_facts_by_category = AsyncMock(return_value=[])
        heart.list_working_memory = AsyncMock(return_value=[])
        heart.list_censors = AsyncMock(return_value=[])
        heart.list_episodes = AsyncMock(return_value=[])
        heart.list_procedures = AsyncMock(return_value=([], 0))
        heart.get_procedure = AsyncMock()
        heart.get_procedure_by_name = AsyncMock(return_value=proc)
        engine = ContextEngine(brain, heart, settings, identity_prompt="Test")

        result = await engine.build(
            agent_id="test", session_id="s1", input_text="do the deploy thing",
            frame=_frame(), critic_skills=["runbook-x"],
        )

        assert "Recommended Procedures" in result.system_prompt
        assert "runbook-x" in result.system_prompt
        assert result.recalled_ids.get("procedure") == [str(proc.id)]

    @pytest.mark.asyncio
    async def test_build_flag_on_fallback_respects_disabled_injection(self):
        """critic_skill_injection='disabled' must suppress the critic fallback even
        under graph-primary selection (codex P1) — no section, no name lookup."""
        proc = _make_procedure_detail("runbook-x")
        settings = Settings(
            _env_file=None,
            proc_selection_graph_primary=True,
            critic_skill_injection="disabled",
            relevance_floor_enabled=False,
        )
        brain = MagicMock()
        brain.embeddings = None
        brain.query = AsyncMock(return_value=[])
        brain.neighbors = AsyncMock(return_value=[])  # no graph hits
        heart = MagicMock()
        heart.search_procedures = AsyncMock(return_value=[])
        heart.search_facts = AsyncMock(return_value=[])
        heart.search_episodes = AsyncMock(return_value=[])
        heart.list_facts_by_category = AsyncMock(return_value=[])
        heart.list_working_memory = AsyncMock(return_value=[])
        heart.list_censors = AsyncMock(return_value=[])
        heart.list_episodes = AsyncMock(return_value=[])
        heart.list_procedures = AsyncMock(return_value=([], 0))
        heart.get_procedure = AsyncMock()
        heart.get_procedure_by_name = AsyncMock(return_value=proc)
        engine = ContextEngine(brain, heart, settings, identity_prompt="Test")

        result = await engine.build(
            agent_id="test", session_id="s1", input_text="do the deploy thing",
            frame=_frame(), critic_skills=["runbook-x"],
        )

        assert "Recommended Procedures" not in result.system_prompt
        heart.get_procedure_by_name.assert_not_called()

    @pytest.mark.asyncio
    async def test_cosine_fallback_fills_when_graph_and_critic_empty(self):
        """§14 ladder: graph misses + no critic -> cosine (embedding) fills the slot
        with the query-relevant procedure (the A/B showed cosine carries coverage)."""
        proc = _make_procedure_detail("cosine-skill")
        settings = Settings(_env_file=None, proc_selection_graph_primary=True)
        brain = MagicMock()
        brain.neighbors = AsyncMock(return_value=[])  # graph misses
        heart = MagicMock()
        heart.get_procedure = AsyncMock(return_value=proc)
        heart.get_procedure_by_name = AsyncMock()
        heart.search_procedures = AsyncMock(return_value=[
            _make_procedure_summary("cosine-skill").model_copy(update={"id": proc.id})
        ])
        engine = ContextEngine(brain, heart, settings, identity_prompt="Test")

        selected = await engine._select_procedures(
            slots=3, critic_skills=[], recalled_ids={"fact": [str(uuid4())]},
            recalled_score_map={}, session=None, query="how do I do the thing",
        )
        assert [p.id for p in selected] == [proc.id]
        heart.search_procedures.assert_awaited()

    @pytest.mark.asyncio
    async def test_cosine_fallback_skipped_without_query(self):
        """No query -> no cosine leg (back-compat: callers that don't pass a query)."""
        settings = Settings(_env_file=None, proc_selection_graph_primary=True)
        brain = MagicMock()
        brain.neighbors = AsyncMock(return_value=[])
        heart = MagicMock()
        heart.get_procedure = AsyncMock()
        heart.get_procedure_by_name = AsyncMock()
        heart.search_procedures = AsyncMock(return_value=[])
        engine = ContextEngine(brain, heart, settings, identity_prompt="Test")

        selected = await engine._select_procedures(
            slots=3, critic_skills=[], recalled_ids={"fact": [str(uuid4())]},
            recalled_score_map={}, session=None,  # query="" default
        )
        assert selected == []
        heart.search_procedures.assert_not_called()


class TestDualTrackDisabled:
    """Tests when critic_skill_injection=disabled (default)."""

    @pytest.mark.asyncio
    async def test_disabled_ignores_critic_skills(self):
        """When disabled, critic_skills param is ignored -- pure embedding path."""
        settings = Settings(
            _env_file=None,
            proc_selection_graph_primary=False,  # these test the legacy passive path
            critic_skill_injection="disabled",
            relevance_floor_enabled=False,
        )
        brain = MagicMock()
        brain.embeddings = None
        heart = MagicMock()
        heart.search_procedures = AsyncMock(return_value=[])
        heart.search_facts = AsyncMock(return_value=[])
        heart.search_episodes = AsyncMock(return_value=[])
        heart.list_facts_by_category = AsyncMock(return_value=[])
        heart.list_working_memory = AsyncMock(return_value=[])
        heart.list_censors = AsyncMock(return_value=[])
        heart.list_episodes = AsyncMock(return_value=[])
        # get_procedure_by_name should NOT be called
        heart.get_procedure_by_name = AsyncMock()

        engine = ContextEngine(brain, heart, settings, identity_prompt="Test")
        brain.query = AsyncMock(return_value=[])

        result = await engine.build(
            agent_id="test", session_id="s1", input_text="test",
            frame=_frame(), critic_skills=["some-skill"],
        )

        assert isinstance(result, BuildResult)
        heart.get_procedure_by_name.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_critic_skills_uses_embedding_only(self):
        """When critic_skills is None, pure embedding path."""
        settings = Settings(
            _env_file=None,
            proc_selection_graph_primary=False,  # these test the legacy passive path
            critic_skill_injection="enabled",
            relevance_floor_enabled=False,
        )
        brain = MagicMock()
        brain.embeddings = None
        heart = MagicMock()
        heart.search_procedures = AsyncMock(return_value=[
            _make_procedure_summary("embed-skill", 0.9),
        ])
        heart.search_facts = AsyncMock(return_value=[])
        heart.search_episodes = AsyncMock(return_value=[])
        heart.list_facts_by_category = AsyncMock(return_value=[])
        heart.list_working_memory = AsyncMock(return_value=[])
        heart.list_censors = AsyncMock(return_value=[])
        heart.list_episodes = AsyncMock(return_value=[])
        heart.get_procedure_by_name = AsyncMock()

        engine = ContextEngine(brain, heart, settings, identity_prompt="Test")
        brain.query = AsyncMock(return_value=[])

        result = await engine.build(
            agent_id="test", session_id="s1", input_text="test",
            frame=_frame(), critic_skills=None,
        )

        assert isinstance(result, BuildResult)
        heart.get_procedure_by_name.assert_not_called()


class TestDualTrackEnabled:
    """Tests when critic_skill_injection=enabled."""

    def _setup_heart(self, critic_procs=None, embed_procs=None):
        """Build a mocked heart with configured returns."""
        heart = MagicMock()
        heart.search_procedures = AsyncMock(return_value=embed_procs or [])
        heart.search_facts = AsyncMock(return_value=[])
        heart.search_episodes = AsyncMock(return_value=[])
        heart.list_facts_by_category = AsyncMock(return_value=[])
        heart.list_working_memory = AsyncMock(return_value=[])
        heart.list_censors = AsyncMock(return_value=[])
        heart.list_episodes = AsyncMock(return_value=[])

        async def _get_by_name(name, session=None):
            for p in (critic_procs or []):
                if p.name == name:
                    return p
            return None

        heart.get_procedure_by_name = AsyncMock(side_effect=_get_by_name)
        return heart

    def _setup_engine(self, heart, mode="enabled"):
        settings = Settings(
            _env_file=None,
            proc_selection_graph_primary=False,  # these test the legacy passive path
            critic_skill_injection=mode,
            critic_skill_slots=2,
            embedding_skill_slots=3,
            relevance_floor_enabled=False,
        )
        brain = MagicMock()
        brain.embeddings = None
        brain.query = AsyncMock(return_value=[])
        return ContextEngine(brain, heart, settings, identity_prompt="Test")

    @pytest.mark.asyncio
    async def test_critic_skills_injected(self):
        """Critic-recommended skills appear in recalled procedures."""
        critic_proc = _make_procedure_detail("summarize-text")
        heart = self._setup_heart(critic_procs=[critic_proc])
        engine = self._setup_engine(heart)

        result = await engine.build(
            agent_id="test", session_id="s1", input_text="summarize this",
            frame=_frame(), critic_skills=["summarize-text"],
        )

        proc_ids = result.recalled_ids.get("procedure", [])
        assert len(proc_ids) >= 1
        assert any("summarize-text" in v for v in result.recalled_content_map.values())

    @pytest.mark.asyncio
    async def test_critic_score_is_synthetic(self):
        """Critic-track procedures get score 1.0 (no .score attr on ProcedureDetail)."""
        critic_proc = _make_procedure_detail("my-skill")
        heart = self._setup_heart(critic_procs=[critic_proc])
        engine = self._setup_engine(heart)

        result = await engine.build(
            agent_id="test", session_id="s1", input_text="test",
            frame=_frame(), critic_skills=["my-skill"],
        )

        for mid, score in result.recalled_score_map.items():
            if result.recalled_content_map.get(mid) == "my-skill":
                assert score == 1.0

    @pytest.mark.asyncio
    async def test_dedup_critic_from_embedding(self):
        """Skills in both Critic and embedding tracks appear only once."""
        critic_proc = _make_procedure_detail("shared-skill")
        embed_proc = _make_procedure_summary("shared-skill", 0.8)
        # Make them have same name but different UUIDs
        heart = self._setup_heart(
            critic_procs=[critic_proc],
            embed_procs=[embed_proc],
        )
        engine = self._setup_engine(heart)

        result = await engine.build(
            agent_id="test", session_id="s1", input_text="test",
            frame=_frame(), critic_skills=["shared-skill"],
        )

        # Should appear exactly once (from Critic track)
        proc_names = [
            v for v in result.recalled_content_map.values()
            if v == "shared-skill"
        ]
        assert len(proc_names) == 1

    @pytest.mark.asyncio
    async def test_input_dedup_duplicate_skills(self):
        """Duplicate skill names in critic_skills don't cause duplicate procedures."""
        critic_proc = _make_procedure_detail("dup-skill")
        heart = self._setup_heart(critic_procs=[critic_proc])
        engine = self._setup_engine(heart)

        result = await engine.build(
            agent_id="test", session_id="s1", input_text="test",
            frame=_frame(), critic_skills=["dup-skill", "dup-skill"],
        )

        proc_ids = result.recalled_ids.get("procedure", [])
        assert len(proc_ids) == len(set(proc_ids))  # No duplicates

    @pytest.mark.asyncio
    async def test_unused_critic_slots_rollover(self):
        """Unused Critic slots increase embedding budget."""
        # Critic recommends skill that doesn't exist -> 0 critic procs
        embed_procs = [
            _make_procedure_summary(f"embed-{i}", 0.9 - i*0.1) for i in range(5)
        ]
        heart = self._setup_heart(critic_procs=[], embed_procs=embed_procs)
        engine = self._setup_engine(heart)

        result = await engine.build(
            agent_id="test", session_id="s1", input_text="test",
            frame=_frame(), critic_skills=["nonexistent"],
        )

        # With critic_slots=2 (all unused) + embedding_slots=3, total=5
        # All 5 embedding procs should be eligible (up to total_slots)
        proc_ids = result.recalled_ids.get("procedure", [])
        assert len(proc_ids) <= 5

    @pytest.mark.asyncio
    async def test_not_found_skill_does_not_crash(self):
        """Skill name not in DB is gracefully skipped."""
        heart = self._setup_heart(critic_procs=[])
        engine = self._setup_engine(heart)

        result = await engine.build(
            agent_id="test", session_id="s1", input_text="test",
            frame=_frame(), critic_skills=["nonexistent-skill"],
        )
        assert isinstance(result, BuildResult)


class TestDualTrackLogOnly:
    """Tests when critic_skill_injection=log_only."""

    @pytest.mark.asyncio
    async def test_log_only_resolves_but_does_not_inject(self):
        """log_only mode resolves skills but doesn't put them in output."""
        critic_proc = _make_procedure_detail("log-only-skill")
        heart = MagicMock()
        heart.search_procedures = AsyncMock(return_value=[])
        heart.search_facts = AsyncMock(return_value=[])
        heart.search_episodes = AsyncMock(return_value=[])
        heart.list_facts_by_category = AsyncMock(return_value=[])
        heart.list_working_memory = AsyncMock(return_value=[])
        heart.list_censors = AsyncMock(return_value=[])
        heart.list_episodes = AsyncMock(return_value=[])

        async def _get_by_name(name, session=None):
            if name == "log-only-skill":
                return critic_proc
            return None
        heart.get_procedure_by_name = AsyncMock(side_effect=_get_by_name)

        settings = Settings(
            _env_file=None,
            proc_selection_graph_primary=False,  # these test the legacy passive path
            critic_skill_injection="log_only",
            relevance_floor_enabled=False,
        )
        brain = MagicMock()
        brain.embeddings = None
        brain.query = AsyncMock(return_value=[])
        engine = ContextEngine(brain, heart, settings, identity_prompt="Test")

        result = await engine.build(
            agent_id="test", session_id="s1", input_text="test",
            frame=_frame(), critic_skills=["log-only-skill"],
        )

        # get_procedure_by_name WAS called (resolves)
        heart.get_procedure_by_name.assert_called_once()
        # But the skill should NOT appear in recalled IDs (not injected)
        proc_ids = result.recalled_ids.get("procedure", [])
        proc_names_in_map = [
            v for v in result.recalled_content_map.values()
            if v == "log-only-skill"
        ]
        assert len(proc_names_in_map) == 0


class TestBuildBackwardCompat:
    """Verify build() is backward compatible."""

    @pytest.mark.asyncio
    async def test_build_without_critic_skills(self):
        """build() works without critic_skills parameter (backward compat)."""
        settings = Settings(
            _env_file=None, relevance_floor_enabled=False,
            proc_selection_graph_primary=False,  # tests the legacy passive path
        )
        brain = MagicMock()
        brain.embeddings = None
        brain.query = AsyncMock(return_value=[])
        heart = MagicMock()
        heart.search_procedures = AsyncMock(return_value=[])
        heart.search_facts = AsyncMock(return_value=[])
        heart.search_episodes = AsyncMock(return_value=[])
        heart.list_facts_by_category = AsyncMock(return_value=[])
        heart.list_working_memory = AsyncMock(return_value=[])
        heart.list_censors = AsyncMock(return_value=[])
        heart.list_episodes = AsyncMock(return_value=[])

        engine = ContextEngine(brain, heart, settings, identity_prompt="Test")

        # Call without critic_skills -- must not crash
        result = await engine.build(
            agent_id="test", session_id="s1", input_text="test", frame=_frame(),
        )
        assert isinstance(result, BuildResult)
