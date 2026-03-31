"""Tests for critic skill wiring in CognitiveLayer (issue #216 + #229).

Tests exercise the activation logic from layer.py lines 377-396 (#216),
and the critic_skills -> context.build() wiring (#229).
"""
import pytest
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from nous.cognitive.critic_schemas import CriticResult, RoutingMode
from nous.cognitive.schemas import BuildResult, FrameSelection


async def _run_activation_logic(
    critic_mode: str,
    critic_result: CriticResult,
    mock_heart: AsyncMock,
    session: object = None,
) -> list[str]:
    """Replicate layer.py lines 377-396 activation logic exactly."""
    activated_skill_ids: list[str] = []
    if critic_mode == "advised" and critic_result.skills:
        for skill_name in critic_result.skills:
            try:
                proc = await mock_heart.get_procedure_by_name(
                    skill_name, session=session,
                )
                if proc:
                    await mock_heart.activate_procedure(
                        proc.id, session=session,
                    )
                    activated_skill_ids.append(str(proc.id))
            except Exception:
                pass  # matches layer.py exception handling
    return activated_skill_ids


class TestCriticSkillActivation:
    """Tests for CriticResult.skills wiring in layer.py pre_turn."""

    @pytest.mark.asyncio
    async def test_advised_mode_activates_skills(self):
        """In advised mode, Critic skills trigger procedure activation via Heart facade."""
        proc_id = uuid4()
        proc_detail = MagicMock()
        proc_detail.id = proc_id

        mock_heart = AsyncMock()
        mock_heart.get_procedure_by_name = AsyncMock(return_value=proc_detail)
        mock_heart.activate_procedure = AsyncMock(return_value=proc_detail)

        critic_result = CriticResult(
            routing=RoutingMode.SINGLE_ADVISED,
            recommended_frame="task",
            rationale="Task with skills",
            skills=["code-review", "debug-strategy"],
        )

        activated = await _run_activation_logic("advised", critic_result, mock_heart)

        assert mock_heart.get_procedure_by_name.call_count == 2
        mock_heart.get_procedure_by_name.assert_any_call("code-review", session=None)
        mock_heart.get_procedure_by_name.assert_any_call("debug-strategy", session=None)
        assert mock_heart.activate_procedure.call_count == 2
        assert len(activated) == 2
        assert all(aid == str(proc_id) for aid in activated)

    @pytest.mark.asyncio
    async def test_shadow_mode_does_not_activate(self):
        """In shadow mode, skills are NOT activated — Heart facade is never called."""
        mock_heart = AsyncMock()

        critic_result = CriticResult(
            routing=RoutingMode.SINGLE_ADVISED,
            recommended_frame="task",
            rationale="Task",
            skills=["some-skill"],
        )

        activated = await _run_activation_logic("shadow", critic_result, mock_heart)

        mock_heart.get_procedure_by_name.assert_not_called()
        mock_heart.activate_procedure.assert_not_called()
        assert activated == []

    @pytest.mark.asyncio
    async def test_unknown_skill_safely_skipped(self):
        """If get_procedure_by_name returns None, activate is NOT called for that skill."""
        known_id = uuid4()
        known_proc = MagicMock()
        known_proc.id = known_id

        mock_heart = AsyncMock()
        mock_heart.get_procedure_by_name = AsyncMock(
            side_effect=lambda name, session=None: known_proc if name == "known" else None,
        )
        mock_heart.activate_procedure = AsyncMock(return_value=known_proc)

        critic_result = CriticResult(
            routing=RoutingMode.SINGLE_ADVISED,
            recommended_frame="task",
            rationale="Task",
            skills=["known", "nonexistent"],
        )

        activated = await _run_activation_logic("advised", critic_result, mock_heart)

        assert mock_heart.get_procedure_by_name.call_count == 2
        assert mock_heart.activate_procedure.call_count == 1  # only for "known"
        assert activated == [str(known_id)]

    @pytest.mark.asyncio
    async def test_activation_exception_does_not_block_others(self):
        """Exception on one skill does not prevent activating the next."""
        good_id = uuid4()
        good_proc = MagicMock()
        good_proc.id = good_id

        mock_heart = AsyncMock()

        async def side_effect(name, session=None):
            if name == "failing-skill":
                raise RuntimeError("DB error")
            return good_proc

        mock_heart.get_procedure_by_name = AsyncMock(side_effect=side_effect)
        mock_heart.activate_procedure = AsyncMock(return_value=good_proc)

        critic_result = CriticResult(
            routing=RoutingMode.SINGLE_ADVISED,
            recommended_frame="task",
            rationale="Task",
            skills=["failing-skill", "good-skill"],
        )

        activated = await _run_activation_logic("advised", critic_result, mock_heart)

        assert activated == [str(good_id)]
        mock_heart.activate_procedure.assert_called_once_with(good_id, session=None)

    @pytest.mark.asyncio
    async def test_empty_skills_does_not_call_heart(self):
        """Empty skills list means no Heart calls, even in advised mode."""
        mock_heart = AsyncMock()

        critic_result = CriticResult(
            routing=RoutingMode.SINGLE_ADVISED,
            recommended_frame="task",
            rationale="Task",
            skills=[],
        )

        activated = await _run_activation_logic("advised", critic_result, mock_heart)

        mock_heart.get_procedure_by_name.assert_not_called()
        assert activated == []

    @pytest.mark.asyncio
    async def test_activated_skill_ids_always_defined(self):
        """activated_skill_ids is always a list, never raises NameError."""
        mock_heart = AsyncMock()

        # Even with no skills and non-advised mode, should return empty list
        for mode in ("shadow", "advised", "parallel"):
            critic_result = CriticResult(
                routing=RoutingMode.PASSTHROUGH,
                recommended_frame="conversation",
                rationale="Simple",
                skills=[],
            )
            activated = await _run_activation_logic(mode, critic_result, mock_heart)
            assert activated == [], f"Failed for mode={mode}"


# --- Issue #229: critic_skills -> context.build() wiring tests ---


def _frame(frame_id="conversation", confidence=0.5, method="default"):
    return FrameSelection(
        frame_id=frame_id,
        frame_name=frame_id.title(),
        confidence=confidence,
        match_method=method,
    )


def _settings(**overrides):
    from nous.config import Settings
    return Settings(_env_file=None, **overrides)


def _mock_layer(settings):
    """Build a CognitiveLayer with fully mocked dependencies."""
    from nous.cognitive.layer import CognitiveLayer

    brain = MagicMock()
    brain.db = MagicMock()
    brain.embeddings = MagicMock()
    heart = MagicMock()
    heart.list_censors = AsyncMock(return_value=[])
    heart.list_working_memory = AsyncMock(return_value=[])
    heart.get_procedure_by_name = AsyncMock(return_value=None)
    heart.activate_procedure = AsyncMock()
    heart.active_episode = AsyncMock(return_value=None)
    heart.start_episode = AsyncMock(return_value=MagicMock(id="ep1"))
    heart.focus = AsyncMock()

    layer = CognitiveLayer(
        brain=brain, heart=heart, settings=settings,
    )

    # Mock context build
    mock_build_result = BuildResult(system_prompt="test", sections=[], recalled_ids={})
    layer._context = MagicMock()
    layer._context.build = AsyncMock(return_value=mock_build_result)
    layer._context._identity_prompt = "Test"

    # Mock frames
    mock_frame = _frame("task", 0.9, "pattern")
    layer._frames = MagicMock()
    layer._frames.select = AsyncMock(return_value=mock_frame)
    layer._frames.list_frames = AsyncMock(return_value=[mock_frame])
    layer._frames.get = AsyncMock(return_value=mock_frame)

    # No event bus
    layer._bus = None

    return layer


class TestCriticSkillsContextWiring:
    """Issue #229: Verify critic_skills are wired to context.build()."""

    @pytest.mark.asyncio
    async def test_critic_skills_passed_in_advised_mode(self):
        """In advised mode, critic_result.skills are passed to context.build()."""
        settings = _settings(
            critic_enabled=True,
            critic_mode="advised",
            critic_skill_injection="enabled",
        )
        layer = _mock_layer(settings)

        mock_result = CriticResult(
            routing=RoutingMode.SINGLE_ADVISED,
            recommended_frame="task",
            rationale="test",
            skills=["skill-a", "skill-b"],
        )
        layer._critic = MagicMock()
        layer._critic.classify = AsyncMock(return_value=mock_result)
        layer._critic.run_diagnostics = MagicMock(return_value=[])

        await layer.pre_turn(
            agent_id="test",
            session_id="s1",
            user_input="please summarize this document for me now",
            session=None,
        )

        layer._context.build.assert_called_once()
        call_kwargs = layer._context.build.call_args.kwargs
        assert "critic_skills" in call_kwargs
        assert call_kwargs["critic_skills"] == ["skill-a", "skill-b"]

    @pytest.mark.asyncio
    async def test_critic_skills_empty_in_shadow_mode(self):
        """In shadow mode, critic_skills should be empty (P1-1 gate)."""
        settings = _settings(
            critic_enabled=True,
            critic_mode="shadow",
            critic_skill_injection="enabled",
        )
        layer = _mock_layer(settings)

        mock_result = CriticResult(
            routing=RoutingMode.SINGLE_ADVISED,
            recommended_frame="task",
            rationale="test",
            skills=["skill-a"],
        )
        layer._critic = MagicMock()
        layer._critic.classify = AsyncMock(return_value=mock_result)
        layer._critic.run_diagnostics = MagicMock(return_value=[])

        await layer.pre_turn(
            agent_id="test",
            session_id="s1",
            user_input="please summarize this document for me now",
            session=None,
        )

        layer._context.build.assert_called_once()
        call_kwargs = layer._context.build.call_args.kwargs
        assert call_kwargs.get("critic_skills") == []

    @pytest.mark.asyncio
    async def test_critic_skills_empty_when_critic_disabled(self):
        """When critic is disabled, critic_skills defaults to empty list."""
        settings = _settings(critic_enabled=False)
        layer = _mock_layer(settings)

        await layer.pre_turn(
            agent_id="test",
            session_id="s1",
            user_input="hi there how are you",
            session=None,
        )

        layer._context.build.assert_called_once()
        call_kwargs = layer._context.build.call_args.kwargs
        assert call_kwargs.get("critic_skills") == []

    @pytest.mark.asyncio
    async def test_critic_skills_empty_when_no_skills_returned(self):
        """In advised mode with empty skills from critic, list stays empty."""
        settings = _settings(
            critic_enabled=True,
            critic_mode="advised",
            critic_skill_injection="enabled",
        )
        layer = _mock_layer(settings)

        mock_result = CriticResult(
            routing=RoutingMode.SINGLE_ADVISED,
            recommended_frame="task",
            rationale="test",
            skills=[],
        )
        layer._critic = MagicMock()
        layer._critic.classify = AsyncMock(return_value=mock_result)
        layer._critic.run_diagnostics = MagicMock(return_value=[])

        await layer.pre_turn(
            agent_id="test",
            session_id="s1",
            user_input="help me debug this issue",
            session=None,
        )

        layer._context.build.assert_called_once()
        call_kwargs = layer._context.build.call_args.kwargs
        assert call_kwargs.get("critic_skills") == []
