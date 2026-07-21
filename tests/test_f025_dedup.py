"""Tests for F025 P2-D: Fact extractor dedup threshold."""

from __future__ import annotations

import pytest
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

from nous.config import Settings
from nous.events import Event


@dataclass
class MockSearchResult:
    score: float | None = None
    content: str = "existing fact"


class TestDedupConfig:
    def test_default_threshold_092(self):
        settings = Settings(_env_file=None)
        assert settings.fact_dedup_threshold == 0.92

    def test_configurable(self):
        settings = Settings(_env_file=None, fact_dedup_threshold=0.88)
        assert settings.fact_dedup_threshold == 0.88


class TestFactDedupBehavior:
    @pytest.mark.asyncio
    async def test_fact_below_threshold_passes(self):
        """A fact with 0.88 similarity should pass when threshold is 0.92."""
        from nous.handlers.fact_extractor import FactExtractor

        heart = MagicMock()
        heart.find_similar_facts = AsyncMock(return_value=[MockSearchResult(score=0.88)])
        heart.learn = AsyncMock(return_value=MagicMock(spec=["id", "content"]))

        settings = Settings(_env_file=None, fact_dedup_threshold=0.92)
        bus = MagicMock()
        bus.on = MagicMock()

        extractor = FactExtractor(heart, settings, bus)

        event = Event(
            type="episode_summarized",
            agent_id="test-agent",
            data={
                "summary": {"summary": "test summary"},
                "episode_id": "test-ep",
                "candidate_facts": [{"content": "New version is 2.0", "subject": "version", "category": "technical"}],
            },
        )

        await extractor.handle(event)
        heart.learn.assert_called_once()

    @pytest.mark.asyncio
    async def test_fact_above_threshold_blocked(self):
        """A fact with 0.95 similarity should be blocked when threshold is 0.92."""
        from nous.handlers.fact_extractor import FactExtractor

        heart = MagicMock()
        heart.find_similar_facts = AsyncMock(return_value=[MockSearchResult(score=0.95)])
        heart.learn = AsyncMock()

        settings = Settings(_env_file=None, fact_dedup_threshold=0.92)
        bus = MagicMock()
        bus.on = MagicMock()

        extractor = FactExtractor(heart, settings, bus)

        event = Event(
            type="episode_summarized",
            agent_id="test-agent",
            data={
                "summary": {"summary": "test summary"},
                "episode_id": "test-ep",
                "candidate_facts": [{"content": "Same old fact", "subject": "test", "category": "technical"}],
            },
        )

        await extractor.handle(event)
        heart.learn.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_existing_facts_passes(self):
        """When no existing facts found, the fact should pass through."""
        from nous.handlers.fact_extractor import FactExtractor

        heart = MagicMock()
        heart.find_similar_facts = AsyncMock(return_value=[])
        heart.learn = AsyncMock(return_value=MagicMock(spec=["id", "content"]))

        settings = Settings(_env_file=None, fact_dedup_threshold=0.92)
        bus = MagicMock()
        bus.on = MagicMock()

        extractor = FactExtractor(heart, settings, bus)

        event = Event(
            type="episode_summarized",
            agent_id="test-agent",
            data={
                "summary": {"summary": "test summary"},
                "episode_id": "test-ep",
                "candidate_facts": [{"content": "Brand new fact", "subject": "new", "category": "technical"}],
            },
        )

        await extractor.handle(event)
        heart.learn.assert_called_once()

    @pytest.mark.asyncio
    async def test_llm_extraction_path_uses_config_threshold(self):
        """The LLM extraction path should also use the config threshold, not hardcoded 0.85."""
        import inspect
        from nous.handlers.fact_extractor import FactExtractor

        source = inspect.getsource(FactExtractor.handle)
        # Check that no code line (excluding comments) uses hardcoded 0.85
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue  # skip comment-only lines
            assert "> 0.85" not in stripped, (
                f"Found hardcoded '> 0.85' in code line: {stripped!r} — should use self._settings.fact_dedup_threshold"
            )

    @pytest.mark.asyncio
    async def test_exemplar_row_does_not_block_conversational_fact(self):
        """Codex r11 (F086): the Leg-1 dedup probe must exclude exemplar rows, so a
        conversational fact near an ``utterance\\nlabel`` row is NOT confirm-dropped.
        The mock honors the SQL contract (exemplar returned UNLESS excluded), so this
        is RED on pre-r11 code (no exclude_sources passed -> exemplar returned ->
        candidate dropped -> learn not called)."""
        from nous.handlers.fact_extractor import FactExtractor

        async def _fake_find_similar(content, limit=5, *, exclude_sources=None):
            # A high-cosine exemplar row is the only near hit; it is returned
            # UNLESS the caller excludes exemplar sources (the r11 fix).
            if exclude_sources and "exemplar_extractor" in exclude_sources:
                return []
            return [MockSearchResult(score=0.97)]

        heart = MagicMock()
        heart.find_similar_facts = AsyncMock(side_effect=_fake_find_similar)
        heart.learn = AsyncMock(return_value=MagicMock(spec=["id", "content"]))

        settings = Settings(_env_file=None, fact_dedup_threshold=0.92)
        bus = MagicMock()
        bus.on = MagicMock()

        extractor = FactExtractor(heart, settings, bus)
        event = Event(
            type="episode_summarized",
            agent_id="test-agent",
            data={
                "summary": {"summary": "test summary"},
                "episode_id": "test-ep",
                "candidate_facts": [
                    {"content": "The user asked how to reset their card pin", "subject": "pin", "category": "technical"}
                ],
            },
        )

        await extractor.handle(event)
        heart.learn.assert_called_once()  # stored, not dropped against the exemplar row
        _, kwargs = heart.find_similar_facts.call_args
        assert kwargs.get("exclude_sources") == ("exemplar_extractor",)  # probe wired to exclude
