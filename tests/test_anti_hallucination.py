"""Tests for anti-hallucination prompt injection (F016 Phase 0)."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from nous.cognitive.context import ContextEngine
from nous.cognitive.schemas import FrameSelection
from nous.config import Settings


def _make_engine(settings: Settings) -> ContextEngine:
    brain = AsyncMock()
    heart = AsyncMock()
    heart.search_facts = AsyncMock(return_value=[])
    heart.list_facts_by_category = AsyncMock(return_value=[])
    brain.query = AsyncMock(return_value=[])
    heart.search_procedures = AsyncMock(return_value=[])
    heart.search_episodes = AsyncMock(return_value=[])
    heart.list_censors = AsyncMock(return_value=[])
    heart.get_working_memory = AsyncMock(return_value=None)
    heart.list_episodes = AsyncMock(return_value=[])
    return ContextEngine(brain, heart, settings)


def _make_frame() -> FrameSelection:
    return FrameSelection(
        frame_id="task",
        frame_name="Task",
        confidence=1.0,
        match_method="default",
        description=None,
    )


class TestAntiHallucinationPrompt:
    @pytest.mark.asyncio
    async def test_prompt_included_when_enabled(self):
        s = Settings(anti_hallucination_prompt=True)
        engine = _make_engine(s)

        result = await engine.build(
            agent_id="test",
            session_id="test-session",
            input_text="test",
            frame=_make_frame(),
        )
        assert "re-read that file" in result.system_prompt or "re-fetch" in result.system_prompt

    @pytest.mark.asyncio
    async def test_prompt_excluded_when_disabled(self):
        s = Settings(anti_hallucination_prompt=False)
        engine = _make_engine(s)

        result = await engine.build(
            agent_id="test",
            session_id="test-session",
            input_text="test",
            frame=_make_frame(),
        )
        assert "re-fetch" not in result.system_prompt
        assert "re-read that file" not in result.system_prompt

    @pytest.mark.asyncio
    async def test_prompt_section_has_correct_label(self):
        s = Settings(anti_hallucination_prompt=True)
        engine = _make_engine(s)

        result = await engine.build(
            agent_id="test",
            session_id="test-session",
            input_text="test",
            frame=_make_frame(),
        )
        labels = [sec.label for sec in result.sections]
        assert "Context Safety" in labels

    @pytest.mark.asyncio
    async def test_prompt_forbids_fabricated_identifiers(self):
        """Extended anti-hallucination clause: never fabricate IDs/UUIDs/paths.

        Prevents the failure mode where the LLM invents a plausible-looking UUID
        for get_procedure / get_fact / etc. when the ID was not present in context.
        """
        s = Settings(anti_hallucination_prompt=True)
        engine = _make_engine(s)

        result = await engine.build(
            agent_id="test",
            session_id="test-session",
            input_text="test",
            frame=_make_frame(),
        )
        prompt = result.system_prompt
        assert "fabricate" in prompt.lower()
        assert "identifiers" in prompt.lower() or "UUIDs" in prompt
        # Both original and extended clauses must coexist
        assert "re-read that file" in prompt or "re-fetch" in prompt
