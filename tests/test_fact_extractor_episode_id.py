"""Regression test for F022 orphan-rate audit (2026-04-30):
fact_extractor must populate FactInput.source_episode_id so
link_episode_deterministic can create extracted_from edges.

Pre-fix: every extracted fact landed with source_episode_id=NULL,
breaking the link_episode_deterministic SQL query and contributing
to the 60.4% episode orphan rate measured in production.
"""
from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from nous.handlers.fact_extractor import _parse_episode_uuid


class TestParseEpisodeUuid:
    def test_valid_uuid_returns_uuid(self):
        u = uuid4()
        assert _parse_episode_uuid(str(u)) == u

    def test_missing_returns_none(self):
        assert _parse_episode_uuid(None) is None
        assert _parse_episode_uuid("") is None

    def test_question_mark_sentinel_returns_none(self):
        """The handle() path falls back to '?' when event data is missing
        an episode_id. Must convert to None, not raise."""
        assert _parse_episode_uuid("?") is None

    def test_garbage_returns_none(self):
        """Defensive — never raise on malformed input."""
        assert _parse_episode_uuid("not-a-uuid") is None
        assert _parse_episode_uuid("12345") is None


class TestFactExtractorPopulatesSourceEpisodeId:
    """Verify both fact-creation paths in fact_extractor pass episode_id
    through to FactInput.source_episode_id.

    Uses light monkey-patching to capture FactInput calls without
    requiring a real DB or LLM.
    """

    @pytest.mark.asyncio
    async def test_candidate_facts_path_sets_episode_id(self, monkeypatch):
        from nous.handlers import fact_extractor as fe_mod
        from nous.heart.schemas import FactInput

        captured: list[FactInput] = []

        class _StubResult:
            id = uuid4()

        class _StubHeart:
            async def search_facts(self, *a, **kw):
                return []

            async def learn(self, fact_input: FactInput):
                captured.append(fact_input)
                return _StubResult()

        class _StubSettings:
            fact_dedup_threshold = 0.92

        ep_id = str(uuid4())
        ext = fe_mod.FactExtractor.__new__(fe_mod.FactExtractor)
        ext._heart = _StubHeart()
        ext._settings = _StubSettings()
        ext._dedup_via_search = False
        ext._llm = None

        await ext._store_candidate_facts(
            candidates=[{"content": "Tim likes coffee", "subject": "Tim",
                         "category": "preference"}],
            episode_id=ep_id,
            transcript=None,
        )

        assert len(captured) == 1
        assert captured[0].source_episode_id == UUID(ep_id), (
            "candidate-facts path must populate source_episode_id"
        )

    @pytest.mark.asyncio
    async def test_extracted_facts_path_sets_episode_id(self, monkeypatch):
        from nous.handlers import fact_extractor as fe_mod
        from nous.heart.schemas import FactInput

        captured: list[FactInput] = []

        class _StubResult:
            id = uuid4()

        class _StubHeart:
            async def search_facts(self, *a, **kw):
                return []

            async def learn(self, fact_input: FactInput):
                captured.append(fact_input)
                return _StubResult()

        class _StubSettings:
            fact_dedup_threshold = 0.92

        ep_id = str(uuid4())
        ext = fe_mod.FactExtractor.__new__(fe_mod.FactExtractor)
        ext._heart = _StubHeart()
        ext._settings = _StubSettings()
        ext._dedup_via_search = False
        ext._llm = None

        await ext._store_extracted_facts(
            candidates=[{"content": "Use Postgres", "subject": "stack",
                         "category": "tool", "confidence": 0.9}],
            episode_id=ep_id,
            transcript=None,
        )

        assert len(captured) == 1
        assert captured[0].source_episode_id == UUID(ep_id), (
            "LLM-extracted path must populate source_episode_id"
        )

    @pytest.mark.asyncio
    async def test_question_mark_episode_id_yields_none(self, monkeypatch):
        """Defensive: when handle() can't find episode_id and falls back to
        '?', FactInput.source_episode_id is None (not a parse error)."""
        from nous.handlers import fact_extractor as fe_mod
        from nous.heart.schemas import FactInput

        captured: list[FactInput] = []

        class _StubResult:
            id = uuid4()

        class _StubHeart:
            async def search_facts(self, *a, **kw):
                return []

            async def learn(self, fact_input: FactInput):
                captured.append(fact_input)
                return _StubResult()

        class _StubSettings:
            fact_dedup_threshold = 0.92

        ext = fe_mod.FactExtractor.__new__(fe_mod.FactExtractor)
        ext._heart = _StubHeart()
        ext._settings = _StubSettings()
        ext._dedup_via_search = False
        ext._llm = None

        await ext._store_candidate_facts(
            candidates=[{"content": "x" * 30, "subject": "test",
                         "category": "concept"}],
            episode_id="?",  # sentinel from handle() fallback
            transcript=None,
        )
        assert captured[0].source_episode_id is None
