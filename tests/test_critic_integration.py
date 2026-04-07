"""Integration tests for F024 Critic Agent wiring."""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from nous.cognitive.critic import CriticAgent
from nous.cognitive.critic_schemas import CriticResult, RoutingMode
from nous.cognitive.schemas import FrameSelection, TurnContext
from nous.config import Settings


def _settings(**overrides):
    return Settings(**overrides)


def _frame(frame_id="conversation", confidence=0.5, method="default"):
    return FrameSelection(
        frame_id=frame_id,
        frame_name=frame_id.title(),
        confidence=confidence,
        match_method=method,
    )


class TestCriticLayerIntegration:
    @pytest.mark.asyncio
    async def test_shadow_mode_logs_but_keeps_heuristic(self):
        settings = _settings(critic_enabled=True, critic_mode="shadow")
        critic = CriticAgent(settings)
        mock_api = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = [
            {"type": "text", "text": json.dumps({
                "complexity": "moderate", "routing": "single",
                "frames": ["decision"], "skills": [],
                "rationale": "Decision task", "per_frame_instructions": {},
            })}
        ]
        mock_api.call = AsyncMock(return_value=mock_response)
        critic.set_api_client(mock_api)

        result = await critic.classify(
            user_message="Should we use Redis or Memcached for our cache layer?",
            heuristic_frame=_frame("task", 0.7, "pattern"),
            available_frames=["task", "decision", "conversation"],
        )
        assert result.recommended_frame == "decision"
        assert result.heuristic_frame == "task"

    @pytest.mark.asyncio
    async def test_advised_mode_overrides_frame(self):
        settings = _settings(critic_enabled=True, critic_mode="advised")
        critic = CriticAgent(settings)
        mock_api = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = [
            {"type": "text", "text": json.dumps({
                "complexity": "moderate", "routing": "single",
                "frames": ["debug"], "skills": [],
                "rationale": "Troubleshooting", "per_frame_instructions": {},
            })}
        ]
        mock_api.call = AsyncMock(return_value=mock_response)
        critic.set_api_client(mock_api)

        result = await critic.classify(
            user_message="The web search keeps timing out and I can't figure out why",
            heuristic_frame=_frame("conversation", 0.3),
            available_frames=["conversation", "debug", "task"],
        )
        assert result.recommended_frame == "debug"

    def test_diagnostic_nudges_in_turn_context(self):
        ctx = TurnContext(
            system_prompt="test",
            frame=_frame("task"),
            diagnostic_nudges="[DIAGNOSTIC OBSERVATIONS]\n[Critic/repetition]: Stop repeating.",
        )
        assert "[DIAGNOSTIC OBSERVATIONS]" in ctx.diagnostic_nudges


class TestRunnerCriticWiring:
    def test_diagnostic_nudges_injected_in_system_prompt(self):
        from nous.api.runner import AgentRunner

        settings = _settings(critic_enabled=True)
        runner = AgentRunner(
            cognitive=MagicMock(),
            brain=MagicMock(),
            heart=MagicMock(),
            settings=settings,
        )

        turn_context = MagicMock()
        turn_context.system_prompt = "Base system prompt"
        turn_context.frame = MagicMock()
        turn_context.frame.frame_id = "task"
        turn_context.diagnostic_nudges = (
            "\n\n[DIAGNOSTIC OBSERVATIONS]\n"
            "[Critic/repetition]: You've searched for similar things."
        )

        prompt = runner._build_system_prompt(turn_context)
        # _build_system_prompt may return str or dict[str, str]
        prompt_text = prompt if isinstance(prompt, str) else " ".join(prompt.values())
        assert "[DIAGNOSTIC OBSERVATIONS]" in prompt_text
        assert "[Critic/repetition]" in prompt_text

    def test_no_nudges_when_empty(self):
        from nous.api.runner import AgentRunner

        settings = _settings(critic_enabled=True)
        runner = AgentRunner(
            cognitive=MagicMock(),
            brain=MagicMock(),
            heart=MagicMock(),
            settings=settings,
        )

        turn_context = MagicMock()
        turn_context.system_prompt = "Base system prompt"
        turn_context.frame = MagicMock()
        turn_context.frame.frame_id = "task"
        turn_context.diagnostic_nudges = ""

        prompt = runner._build_system_prompt(turn_context)
        prompt_text = prompt if isinstance(prompt, str) else " ".join(prompt.values())
        assert "[DIAGNOSTIC OBSERVATIONS]" not in prompt_text


class TestShadowModeEvent:
    @pytest.mark.asyncio
    async def test_shadow_mode_event_data_structure(self):
        """Verify the event data structure for critic_classified events."""
        from nous.events import Event

        result = CriticResult(
            routing=RoutingMode.SINGLE_ADVISED,
            recommended_frame="decision",
            rationale="Decision task",
            heuristic_frame="task",
            latency_ms=150,
        )
        event = Event(
            type="critic_classified",
            agent_id="test-agent",
            session_id="test-session",
            data={
                "heuristic_frame": result.heuristic_frame,
                "critic_frame": result.recommended_frame,
                "routing": result.routing.value,
                "rationale": result.rationale,
                "latency_ms": result.latency_ms,
                "mode": "shadow",
                "agreed": result.heuristic_frame == result.recommended_frame,
            },
        )

        assert event.type == "critic_classified"
        assert event.data["agreed"] is False
        assert event.data["critic_frame"] == "decision"
        assert event.data["heuristic_frame"] == "task"
        assert event.data["mode"] == "shadow"
        assert event.data["latency_ms"] == 150
