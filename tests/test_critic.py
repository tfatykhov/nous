"""Tests for F024 Critic Agent Phase 0."""
import json
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock
from uuid import uuid4

import pytest

from nous.cognitive.critic import CriticAgent
from nous.cognitive.critic_schemas import (
    CriticResult,
    DiagnosticResult,
    RoutingMode,
)
from nous.cognitive.schemas import FrameSelection
from nous.config import Settings
from nous.heart.schemas import ProcedureSummary


def _settings(**overrides):
    return Settings(_env_file=None, **overrides)


def _frame(frame_id="conversation", confidence=0.5, method="default"):
    return FrameSelection(
        frame_id=frame_id,
        frame_name=frame_id.title(),
        confidence=confidence,
        match_method=method,
    )


# ---- Schemas ----


class TestCriticSchemas:
    def test_critic_result_defaults(self):
        result = CriticResult(
            routing=RoutingMode.SINGLE_ADVISED,
            recommended_frame="task",
            rationale="User wants to build something",
        )
        assert result.routing == RoutingMode.SINGLE_ADVISED
        assert result.recommended_frame == "task"
        assert result.skills == []
        assert result.complexity == "moderate"
        assert result.diagnostics == []

    def test_critic_result_passthrough(self):
        result = CriticResult(
            routing=RoutingMode.PASSTHROUGH,
            recommended_frame="conversation",
            rationale="Simple greeting",
            complexity="simple",
        )
        assert result.routing == RoutingMode.PASSTHROUGH

    def test_diagnostic_result(self):
        diag = DiagnosticResult(
            critic_name="repetition",
            intervention="You've searched for similar things multiple times.",
            fired=True,
        )
        assert diag.fired is True
        assert diag.critic_name == "repetition"


# ---- Config ----


class TestCriticConfig:
    def test_critic_defaults(self):
        s = _settings()
        assert s.critic_enabled is True
        assert s.critic_mode == "shadow"
        assert s.critic_model == "claude-sonnet-4-6"
        assert s.critic_max_latency_ms == 5000
        assert s.critic_passthrough_max_words == 5

    def test_critic_disabled(self):
        s = _settings(critic_enabled=False)
        assert s.critic_enabled is False

    def test_critic_mode_values(self):
        for mode in ("shadow", "advised", "parallel"):
            s = _settings(critic_mode=mode)
            assert s.critic_mode == mode


# ---- Complexity Gate ----


class TestComplexityGate:
    def _make(self):
        return CriticAgent(_settings())

    def test_short_greeting_skips_critic(self):
        assert self._make()._needs_critic("hi", []) is False

    def test_short_non_question_skips(self):
        assert self._make()._needs_critic("thanks", []) is False

    def test_empty_message_skips(self):
        assert self._make()._needs_critic("", []) is False

    def test_short_question_invokes(self):
        assert self._make()._needs_critic("what?", []) is True

    def test_multi_sentence_invokes(self):
        msg = "Research how other agents handle memory. Then build a comparison. Also analyze costs."
        assert self._make()._needs_critic(msg, []) is True

    def test_multiple_action_verbs_invokes(self):
        msg = "research the topic and build a summary"
        assert self._make()._needs_critic(msg, []) is True

    def test_repeated_tool_calls_invokes(self):
        history = [
            {"tool": "recall_deep", "args": "memory"},
            {"tool": "recall_deep", "args": "memory search"},
            {"tool": "recall_deep", "args": "memory retrieval"},
        ]
        assert self._make()._needs_critic("find it", history) is True

    def test_default_invokes_critic(self):
        assert self._make()._needs_critic("please explain the full architecture of the system", []) is True

    def test_emoji_only_skips(self):
        assert self._make()._needs_critic("\U0001f44d", []) is False

    def test_single_word_question(self):
        assert self._make()._needs_critic("why?", []) is True


# ---- Classification ----


class TestCriticClassification:
    @pytest.mark.asyncio
    async def test_classify_parses_json_response(self):
        agent = CriticAgent(_settings())
        mock_api = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = [
            {"type": "text", "text": json.dumps({
                "complexity": "moderate", "routing": "single",
                "frames": ["task"], "skills": [],
                "rationale": "User wants to build something",
                "per_frame_instructions": {},
            })}
        ]
        mock_api.call = AsyncMock(return_value=mock_response)
        agent.set_api_client(mock_api)

        result = await agent.classify(
            user_message="Build a REST API for user management",
            heuristic_frame=_frame(),
            available_frames=["task", "conversation", "question"],
        )
        assert result.routing == RoutingMode.SINGLE_ADVISED
        assert result.recommended_frame == "task"
        assert result.heuristic_frame == "conversation"
        assert result.latency_ms >= 0

    @pytest.mark.asyncio
    async def test_classify_handles_malformed_json(self):
        agent = CriticAgent(_settings())
        mock_api = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = [{"type": "text", "text": "not json at all"}]
        mock_api.call = AsyncMock(return_value=mock_response)
        agent.set_api_client(mock_api)

        result = await agent.classify(
            user_message="please help me understand the complex architecture of this system",
            heuristic_frame=_frame("task", 0.8, "pattern"),
            available_frames=["task"],
        )
        assert result.routing == RoutingMode.SINGLE_ADVISED
        assert result.recommended_frame == "task"

    @pytest.mark.asyncio
    async def test_classify_no_api_client_falls_back(self):
        agent = CriticAgent(_settings())
        result = await agent.classify(
            user_message="please help me understand the complex architecture of this system",
            heuristic_frame=_frame("task", 0.8, "pattern"),
            available_frames=["task"],
        )
        assert result.routing == RoutingMode.PASSTHROUGH
        assert result.recommended_frame == "task"

    @pytest.mark.asyncio
    async def test_passthrough_skips_llm_call(self):
        agent = CriticAgent(_settings())
        mock_api = AsyncMock()
        agent.set_api_client(mock_api)

        result = await agent.classify(
            user_message="hi",
            heuristic_frame=_frame(),
            available_frames=["conversation"],
        )
        assert result.routing == RoutingMode.PASSTHROUGH
        mock_api.call.assert_not_called()

    @pytest.mark.asyncio
    async def test_classify_handles_api_exception(self):
        """API errors are caught by call_background_llm (returns None),
        which becomes empty string → parse fallback to heuristic frame."""
        agent = CriticAgent(_settings())
        mock_api = AsyncMock()
        mock_api.call = AsyncMock(side_effect=RuntimeError("rate limited"))
        agent.set_api_client(mock_api)

        result = await agent.classify(
            user_message="please help me understand the complex architecture of this system",
            heuristic_frame=_frame("task", 0.8, "pattern"),
            available_frames=["task"],
        )
        # call_background_llm catches the error and returns None → parse fallback
        assert result.routing == RoutingMode.SINGLE_ADVISED
        assert result.recommended_frame == "task"

    @pytest.mark.asyncio
    async def test_json_wrapped_in_code_fence(self):
        agent = CriticAgent(_settings())
        mock_api = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = [
            {"type": "text", "text": '```json\n{"complexity":"simple","routing":"single","frames":["question"],"skills":[],"rationale":"Simple question","per_frame_instructions":{}}\n```'}
        ]
        mock_api.call = AsyncMock(return_value=mock_response)
        agent.set_api_client(mock_api)

        result = await agent.classify(
            user_message="what is the meaning of life and how does it relate to architecture?",
            heuristic_frame=_frame(),
            available_frames=["conversation", "question"],
        )
        assert result.recommended_frame == "question"

    @pytest.mark.asyncio
    async def test_empty_frames_list_uses_heuristic(self):
        agent = CriticAgent(_settings())
        mock_api = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = [
            {"type": "text", "text": json.dumps({
                "complexity": "simple", "routing": "single",
                "frames": [], "skills": [], "rationale": "",
                "per_frame_instructions": {},
            })}
        ]
        mock_api.call = AsyncMock(return_value=mock_response)
        agent.set_api_client(mock_api)

        result = await agent.classify(
            user_message="do something complex here please",
            heuristic_frame=_frame("task", 0.8, "pattern"),
            available_frames=["task"],
        )
        assert result.recommended_frame == "task"


# ---- Diagnostic Critics ----


class TestDiagnosticCritics:
    def test_repetition_detector(self):
        agent = CriticAgent(_settings())
        tool_history = [
            {"tool": "recall_deep", "query": "how does memory work"},
            {"tool": "recall_deep", "query": "how does memory function"},
            {"tool": "recall_deep", "query": "how does the memory system work"},
        ]
        results = agent.run_diagnostics(tool_history, turn_number=1)
        assert any(d.fired and d.critic_name == "repetition" for d in results)

    def test_stuck_loop_detector(self):
        agent = CriticAgent(_settings())
        tool_history = [
            {"tool": "bash", "args": "ls /tmp"},
            {"tool": "bash", "args": "ls /tmp"},
            {"tool": "bash", "args": "ls /tmp"},
        ]
        results = agent.run_diagnostics(tool_history, turn_number=1)
        assert any(d.fired and d.critic_name == "stuck_loop" for d in results)

    def test_confidence_drift_detector(self):
        agent = CriticAgent(_settings())
        tool_history = [
            {"tool": "record_decision", "confidence": 0.3},
            {"tool": "record_decision", "confidence": 0.25},
            {"tool": "record_decision", "confidence": 0.2},
        ]
        results = agent.run_diagnostics(tool_history, turn_number=1)
        assert any(d.fired and d.critic_name == "confidence_drift" for d in results)

    def test_frame_mismatch_detector(self):
        agent = CriticAgent(_settings())
        tool_history = [
            {"tool": "bash", "args": "npm install"},
            {"tool": "write_file", "args": "main.py"},
            {"tool": "bash", "args": "pytest"},
        ]
        results = agent.run_diagnostics(
            tool_history, turn_number=1, current_frame="conversation",
        )
        assert any(d.fired and d.critic_name == "frame_mismatch" for d in results)

    def test_scope_creep_detector(self):
        agent = CriticAgent(_settings())
        results = agent.run_diagnostics(
            [], turn_number=1, response_lengths=[100, 200, 500, 1200, 2500],
        )
        assert any(d.fired and d.critic_name == "scope_creep" for d in results)

    def test_user_frustration_detector(self):
        agent = CriticAgent(_settings())
        results = agent.run_diagnostics(
            [], turn_number=1,
            recent_user_messages=["no", "I already said that", "no I meant the other one"],
        )
        assert any(d.fired and d.critic_name == "user_frustration" for d in results)

    def test_no_false_positives_on_clean_history(self):
        agent = CriticAgent(_settings())
        tool_history = [
            {"tool": "recall_deep", "query": "python async"},
            {"tool": "bash", "args": "pytest"},
            {"tool": "write_file", "args": "main.py"},
        ]
        results = agent.run_diagnostics(tool_history, turn_number=1)
        assert not any(d.fired for d in results)

    def test_cooldown_prevents_refiring(self):
        agent = CriticAgent(_settings())
        tool_history = [
            {"tool": "recall_deep", "query": "memory"},
            {"tool": "recall_deep", "query": "memory"},
            {"tool": "recall_deep", "query": "memory"},
        ]
        results1 = agent.run_diagnostics(tool_history, turn_number=1)
        assert any(d.fired for d in results1)

        results2 = agent.run_diagnostics(tool_history, turn_number=2)
        assert not any(d.fired for d in results2)

        results3 = agent.run_diagnostics(tool_history, turn_number=5)
        assert any(d.fired for d in results3)

    def test_empty_tool_history(self):
        agent = CriticAgent(_settings())
        results = agent.run_diagnostics([], turn_number=1)
        assert not any(d.fired for d in results)

    def test_format_nudges_empty_when_none_fired(self):
        agent = CriticAgent(_settings())
        results = agent.run_diagnostics([], turn_number=1)
        assert agent.format_nudges(results) == ""

    def test_format_nudges_with_fired(self):
        agent = CriticAgent(_settings())
        tool_history = [
            {"tool": "recall_deep", "query": "memory"},
            {"tool": "recall_deep", "query": "memory"},
            {"tool": "recall_deep", "query": "memory"},
        ]
        results = agent.run_diagnostics(tool_history, turn_number=1)
        nudges = agent.format_nudges(results)
        assert "[Critic/repetition]" in nudges
        assert "[DIAGNOSTIC OBSERVATIONS]" in nudges


# ---- Skill Catalog (Issue #216) ----


class TestSkillCatalog:
    """Tests for issue #216: skill catalog injection."""

    @pytest.mark.asyncio
    async def test_build_catalog_from_procedures(self):
        """Catalog is built from active procedures with correct format."""
        from nous.heart.procedures import ProcedureManager
        agent = CriticAgent(_settings())
        mock_pm = AsyncMock(spec=ProcedureManager)
        mock_pm.list_all = AsyncMock(return_value=([
            ProcedureSummary(id=uuid4(), name="code-review", domain="process",
                           description="Review pull requests", activation_count=5,
                           effectiveness=0.85),
            ProcedureSummary(id=uuid4(), name="debug-strategy", domain="engineering",
                           description="Systematic debugging approach", activation_count=3,
                           effectiveness=None),
        ], 2))
        agent._procedure_manager = mock_pm
        catalog, valid_names = await agent._build_skill_catalog()
        assert "code-review" in catalog
        assert "Review pull requests" in catalog
        assert "(effectiveness: 85%)" in catalog
        assert "debug-strategy" in catalog
        assert valid_names == {"code-review", "debug-strategy"}

    @pytest.mark.asyncio
    async def test_catalog_no_procedure_manager(self):
        """No procedure_manager returns safe default."""
        agent = CriticAgent(_settings())
        catalog, valid_names = await agent._build_skill_catalog()
        assert catalog == "No skills registered."
        assert valid_names == set()

    @pytest.mark.asyncio
    async def test_catalog_empty_procedures(self):
        """Empty procedure list returns safe default."""
        from nous.heart.procedures import ProcedureManager
        agent = CriticAgent(_settings())
        mock_pm = AsyncMock(spec=ProcedureManager)
        mock_pm.list_all = AsyncMock(return_value=([], 0))
        agent._procedure_manager = mock_pm
        catalog, valid_names = await agent._build_skill_catalog()
        assert catalog == "No skills registered."
        assert valid_names == set()

    @pytest.mark.asyncio
    async def test_catalog_list_all_exception_degrades_gracefully(self):
        """list_all failure returns safe default."""
        from nous.heart.procedures import ProcedureManager
        agent = CriticAgent(_settings())
        mock_pm = AsyncMock(spec=ProcedureManager)
        mock_pm.list_all = AsyncMock(side_effect=RuntimeError("DB error"))
        agent._procedure_manager = mock_pm
        catalog, valid_names = await agent._build_skill_catalog()
        assert catalog == "No skills registered."
        assert valid_names == set()

    @pytest.mark.asyncio
    async def test_catalog_escapes_curly_braces(self):
        """Curly braces in descriptions are escaped for .format()."""
        from nous.heart.procedures import ProcedureManager
        agent = CriticAgent(_settings())
        mock_pm = AsyncMock(spec=ProcedureManager)
        mock_pm.list_all = AsyncMock(return_value=([
            ProcedureSummary(id=uuid4(), name="template-skill", domain="general",
                           description="Use {variable} syntax",
                           activation_count=1, effectiveness=None),
        ], 1))
        agent._procedure_manager = mock_pm
        catalog, valid_names = await agent._build_skill_catalog()
        # Should not raise when used in .format()
        test_template = "Skills:\n{skill_catalog}"
        formatted = test_template.format(skill_catalog=catalog)
        assert "{{variable}}" in catalog
        assert "template-skill" in valid_names


class TestSkillFiltering:
    """Tests for hallucinated skill filtering."""

    def test_hallucinated_skills_filtered(self):
        agent = CriticAgent(_settings())
        raw_json = json.dumps({
            "complexity": "moderate", "routing": "single",
            "frames": ["task"],
            "skills": ["code-review", "hallucinated-skill", "debug-strategy"],
            "rationale": "Task with skills",
            "per_frame_instructions": {},
        })
        result = agent._parse_classification(
            raw_json, _frame(), valid_skill_names={"code-review", "debug-strategy"},
        )
        assert result.skills == ["code-review", "debug-strategy"]

    def test_no_valid_names_passes_all(self):
        """When valid_skill_names is None (backward compat), all skills pass through."""
        agent = CriticAgent(_settings())
        raw_json = json.dumps({
            "complexity": "moderate", "routing": "single",
            "frames": ["task"],
            "skills": ["anything", "goes"],
            "rationale": "Test",
            "per_frame_instructions": {},
        })
        result = agent._parse_classification(raw_json, _frame(), valid_skill_names=None)
        assert result.skills == ["anything", "goes"]

    def test_empty_valid_names_filters_everything(self):
        """When catalog is empty (set()), all skills are filtered out."""
        agent = CriticAgent(_settings())
        raw_json = json.dumps({
            "complexity": "moderate", "routing": "single",
            "frames": ["task"],
            "skills": ["hallucinated"],
            "rationale": "Test",
            "per_frame_instructions": {},
        })
        result = agent._parse_classification(raw_json, _frame(), valid_skill_names=set())
        assert result.skills == []

    @pytest.mark.asyncio
    async def test_classify_injects_catalog_into_prompt(self):
        """Full classify call includes skill catalog in prompt."""
        from nous.heart.procedures import ProcedureManager
        agent = CriticAgent(_settings())
        mock_pm = AsyncMock(spec=ProcedureManager)
        mock_pm.list_all = AsyncMock(return_value=([
            ProcedureSummary(id=uuid4(), name="my-skill", domain="engineering",
                           description="A real skill", activation_count=1,
                           effectiveness=0.9),
        ], 1))
        agent._procedure_manager = mock_pm

        mock_api = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = [{"type": "text", "text": json.dumps({
            "complexity": "moderate", "routing": "single",
            "frames": ["task"], "skills": ["my-skill"],
            "rationale": "Needs skill", "per_frame_instructions": {},
        })}]
        mock_api.call = AsyncMock(return_value=mock_response)
        agent.set_api_client(mock_api)

        result = await agent.classify(
            user_message="please help me review this complex code thoroughly",
            heuristic_frame=_frame(),
            available_frames=["task", "conversation"],
        )
        assert result.skills == ["my-skill"]

    @pytest.mark.asyncio
    async def test_passthrough_returns_empty_skills(self):
        """Passthrough skips catalog and returns empty skills."""
        from nous.heart.procedures import ProcedureManager
        agent = CriticAgent(_settings())
        mock_pm = AsyncMock(spec=ProcedureManager)
        agent._procedure_manager = mock_pm

        result = await agent.classify(
            user_message="hi",
            heuristic_frame=_frame(),
            available_frames=["conversation"],
        )
        assert result.skills == []
        # Verify list_all was NOT called (passthrough skips catalog)
        mock_pm.list_all.assert_not_called()
