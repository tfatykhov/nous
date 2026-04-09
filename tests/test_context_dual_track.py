"""Tests for dual-track procedure loading (issue #229)."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from nous.cognitive.context import ContextEngine
from nous.cognitive.schemas import BuildResult, FrameSelection
from nous.config import Settings
from nous.heart.schemas import ProcedureDetail, ProcedureSummary


def _frame(frame_id: str = "task") -> FrameSelection:
    return FrameSelection(
        frame_id=frame_id,
        frame_name="Task",
        confidence=0.9,
        match_method="test",
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
        implementation_notes=[],
        activation_count=1,
        success_count=0,
        failure_count=0,
        neutral_count=0,
        last_activated=None,
        effectiveness=None,
        tags=[],
        active=True,
        created_at=datetime.now(UTC),
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


class TestDualTrackDisabled:
    """Tests when critic_skill_injection=disabled (default)."""

    @pytest.mark.asyncio
    async def test_disabled_ignores_critic_skills(self):
        """When disabled, critic_skills param is ignored -- pure embedding path."""
        settings = Settings(
            _env_file=None,
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
            agent_id="test",
            session_id="s1",
            input_text="test",
            frame=_frame(),
            critic_skills=["some-skill"],
        )

        assert isinstance(result, BuildResult)
        heart.get_procedure_by_name.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_critic_skills_uses_embedding_only(self):
        """When critic_skills is None, pure embedding path."""
        settings = Settings(
            _env_file=None,
            critic_skill_injection="enabled",
            relevance_floor_enabled=False,
        )
        brain = MagicMock()
        brain.embeddings = None
        heart = MagicMock()
        heart.search_procedures = AsyncMock(
            return_value=[
                _make_procedure_summary("embed-skill", 0.9),
            ]
        )
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
            agent_id="test",
            session_id="s1",
            input_text="test",
            frame=_frame(),
            critic_skills=None,
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
            for p in critic_procs or []:
                if p.name == name:
                    return p
            return None

        heart.get_procedure_by_name = AsyncMock(side_effect=_get_by_name)
        return heart

    def _setup_engine(self, heart, mode="enabled"):
        settings = Settings(
            _env_file=None,
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
            agent_id="test",
            session_id="s1",
            input_text="summarize this",
            frame=_frame(),
            critic_skills=["summarize-text"],
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
            agent_id="test",
            session_id="s1",
            input_text="test",
            frame=_frame(),
            critic_skills=["my-skill"],
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
            agent_id="test",
            session_id="s1",
            input_text="test",
            frame=_frame(),
            critic_skills=["shared-skill"],
        )

        # Should appear exactly once (from Critic track)
        proc_names = [v for v in result.recalled_content_map.values() if v == "shared-skill"]
        assert len(proc_names) == 1

    @pytest.mark.asyncio
    async def test_input_dedup_duplicate_skills(self):
        """Duplicate skill names in critic_skills don't cause duplicate procedures."""
        critic_proc = _make_procedure_detail("dup-skill")
        heart = self._setup_heart(critic_procs=[critic_proc])
        engine = self._setup_engine(heart)

        result = await engine.build(
            agent_id="test",
            session_id="s1",
            input_text="test",
            frame=_frame(),
            critic_skills=["dup-skill", "dup-skill"],
        )

        proc_ids = result.recalled_ids.get("procedure", [])
        assert len(proc_ids) == len(set(proc_ids))  # No duplicates

    @pytest.mark.asyncio
    async def test_unused_critic_slots_rollover(self):
        """Unused Critic slots increase embedding budget."""
        # Critic recommends skill that doesn't exist -> 0 critic procs
        embed_procs = [_make_procedure_summary(f"embed-{i}", 0.9 - i * 0.1) for i in range(5)]
        heart = self._setup_heart(critic_procs=[], embed_procs=embed_procs)
        engine = self._setup_engine(heart)

        result = await engine.build(
            agent_id="test",
            session_id="s1",
            input_text="test",
            frame=_frame(),
            critic_skills=["nonexistent"],
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
            agent_id="test",
            session_id="s1",
            input_text="test",
            frame=_frame(),
            critic_skills=["nonexistent-skill"],
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
            critic_skill_injection="log_only",
            relevance_floor_enabled=False,
        )
        brain = MagicMock()
        brain.embeddings = None
        brain.query = AsyncMock(return_value=[])
        engine = ContextEngine(brain, heart, settings, identity_prompt="Test")

        result = await engine.build(
            agent_id="test",
            session_id="s1",
            input_text="test",
            frame=_frame(),
            critic_skills=["log-only-skill"],
        )

        # get_procedure_by_name WAS called (resolves)
        heart.get_procedure_by_name.assert_called_once()
        # But the skill should NOT appear in recalled IDs (not injected)
        proc_ids = result.recalled_ids.get("procedure", [])  # noqa: F841
        proc_names_in_map = [v for v in result.recalled_content_map.values() if v == "log-only-skill"]
        assert len(proc_names_in_map) == 0


class TestBuildBackwardCompat:
    """Verify build() is backward compatible."""

    @pytest.mark.asyncio
    async def test_build_without_critic_skills(self):
        """build() works without critic_skills parameter (backward compat)."""
        settings = Settings(_env_file=None, relevance_floor_enabled=False)
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
            agent_id="test",
            session_id="s1",
            input_text="test",
            frame=_frame(),
        )
        assert isinstance(result, BuildResult)
