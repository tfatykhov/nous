"""Tests for F025 P3-B: Chunked summarization."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from nous.handlers.episode_summarizer import EpisodeSummarizer


# ---------------------------------------------------------------------------
# Factory fixture (Task 4): builds uninitialized EpisodeSummarizer instances
# with SimpleNamespace settings containing only the overrides needed per test.
# Uses EpisodeSummarizer.__new__() pattern matching the existing test class
# above (no Heart/Brain/LLM needed for pure-method tests).
# ---------------------------------------------------------------------------

_SETTINGS_DEFAULTS = {
    # Fields consumed by _summary_max_tokens
    "episode_summary_max_tokens": 0,
    "extraction_coverage_broadened": False,
    "episode_open_threads": False,
    # Fields consumed by _select_chunks
    "episode_summary_max_chunks": 4,
    # Fields consumed by _chunk_and_store_transcript
    "episode_chunk_max_per_episode": 100,
    "episode_chunks_enabled": False,
    "episode_chunk_size": 600,
    "episode_chunk_overlap": 80,
    "episode_chunk_min_transcript_chars": 50,
    "agent_id": "test-agent",
}


@pytest.fixture()
def summarizer_factory():
    """Return a factory that builds a settings-patched EpisodeSummarizer.__new__ instance."""
    def _make(settings_overrides: dict | None = None) -> EpisodeSummarizer:
        s = EpisodeSummarizer.__new__(EpisodeSummarizer)
        merged = {**_SETTINGS_DEFAULTS, **(settings_overrides or {})}
        s._settings = SimpleNamespace(**merged)
        return s
    return _make


class TestChunkTranscript:
    def test_short_returns_single_chunk(self):
        summarizer = EpisodeSummarizer.__new__(EpisodeSummarizer)
        transcript = "User: Hello\n\nAssistant: Hi"
        chunks = summarizer._chunk_transcript(transcript, max_chars=16000)
        assert len(chunks) == 1
        assert chunks[0] == transcript

    def test_long_splits_into_multiple_chunks(self):
        summarizer = EpisodeSummarizer.__new__(EpisodeSummarizer)
        turns = [f"User: Question {i}\n\nAssistant: {'x' * 950}" for i in range(20)]
        transcript = "\n\n".join(turns)
        assert len(transcript) > 16000

        chunks = summarizer._chunk_transcript(transcript, max_chars=16000)
        assert len(chunks) >= 2
        for chunk in chunks:
            assert len(chunk) <= 16000

    def test_preserves_all_content(self):
        summarizer = EpisodeSummarizer.__new__(EpisodeSummarizer)
        turns = [f"User: Question {i}\n\nAssistant: Answer {i}" for i in range(50)]
        transcript = "\n\n".join(turns)
        chunks = summarizer._chunk_transcript(transcript, max_chars=500)
        reconstructed = "\n\n".join(chunks)
        assert "Question 0" in reconstructed
        assert "Question 49" in reconstructed

    def test_preserves_turn_integrity(self):
        summarizer = EpisodeSummarizer.__new__(EpisodeSummarizer)
        turns = [f"User: Turn {i} content here" for i in range(100)]
        transcript = "\n\n".join(turns)
        chunks = summarizer._chunk_transcript(transcript, max_chars=500)
        for chunk in chunks:
            for line in chunk.split("\n\n"):
                if line.startswith("User: Turn"):
                    assert "content here" in line


class TestMergeSummaries:
    def test_merge_uses_first_title(self):
        summarizer = EpisodeSummarizer.__new__(EpisodeSummarizer)
        summaries = [
            {"title": "Part 1 Title", "summary": "First part.", "key_points": ["kp1"], "outcome": "ongoing", "topics": ["a"]},
            {"title": "Part 2 Title", "summary": "Second part.", "key_points": ["kp2"], "outcome": "success", "topics": ["b"]},
        ]
        merged = summarizer._merge_summaries(summaries)
        assert merged["title"] == "Part 1 Title"

    def test_merge_uses_last_outcome(self):
        summarizer = EpisodeSummarizer.__new__(EpisodeSummarizer)
        summaries = [
            {"title": "T", "summary": "S1.", "outcome": "ongoing", "outcome_rationale": "still going"},
            {"title": "T", "summary": "S2.", "outcome": "success", "outcome_rationale": "completed"},
        ]
        merged = summarizer._merge_summaries(summaries)
        assert merged["outcome"] == "success"
        assert merged["outcome_rationale"] == "completed"

    def test_merge_concatenates_summaries(self):
        summarizer = EpisodeSummarizer.__new__(EpisodeSummarizer)
        summaries = [
            {"title": "T", "summary": "First."},
            {"title": "T", "summary": "Second."},
        ]
        merged = summarizer._merge_summaries(summaries)
        assert merged["summary"] == "First. Second."

    def test_merge_unions_topics(self):
        summarizer = EpisodeSummarizer.__new__(EpisodeSummarizer)
        summaries = [
            {"title": "T", "summary": "S", "topics": ["python", "testing"]},
            {"title": "T", "summary": "S", "topics": ["python", "deployment"]},
        ]
        merged = summarizer._merge_summaries(summaries)
        assert set(merged["topics"]) == {"python", "testing", "deployment"}

    def test_merge_caps_key_points_at_10(self):
        summarizer = EpisodeSummarizer.__new__(EpisodeSummarizer)
        summaries = [
            {"title": "T", "summary": "S", "key_points": [f"kp{i}" for i in range(8)]},
            {"title": "T", "summary": "S", "key_points": [f"kp{i}" for i in range(8, 16)]},
        ]
        merged = summarizer._merge_summaries(summaries)
        assert len(merged["key_points"]) == 10

    def test_merge_caps_candidate_facts_at_5(self):
        summarizer = EpisodeSummarizer.__new__(EpisodeSummarizer)
        summaries = [
            {"title": "T", "summary": "S", "candidate_facts": [f"f{i}" for i in range(4)]},
            {"title": "T", "summary": "S", "candidate_facts": [f"f{i}" for i in range(4, 8)]},
        ]
        merged = summarizer._merge_summaries(summaries)
        assert len(merged["candidate_facts"]) == 5

    def test_merge_has_all_required_fields(self):
        summarizer = EpisodeSummarizer.__new__(EpisodeSummarizer)
        summaries = [{"title": "T", "summary": "S"}]
        merged = summarizer._merge_summaries(summaries)
        required = {"title", "summary", "key_points", "candidate_facts", "outcome", "outcome_rationale", "topics"}
        assert required.issubset(set(merged.keys()))

    def test_merge_stable_cap_broadened_uses_setting(self):
        """Coverage fix: flag ON raises the stable cap to candidate_facts_stable_limit."""
        from types import SimpleNamespace

        summarizer = EpisodeSummarizer.__new__(EpisodeSummarizer)
        summarizer._settings = SimpleNamespace(
            extraction_coverage_broadened=True,
            candidate_facts_stable_limit=15,
            candidate_facts_event_limit=30,
        )
        facts = [{"subject": f"s{i}", "content": f"c{i}", "category": "concept"} for i in range(20)]
        summaries = [
            {"title": "T", "summary": "S", "candidate_facts": facts[:10]},
            {"title": "T", "summary": "S", "candidate_facts": facts[10:]},
        ]
        merged = summarizer._merge_summaries(summaries)
        assert len(merged["candidate_facts"]) == 15

    def test_merge_stable_cap_legacy_when_flag_off(self):
        """Flag OFF keeps the legacy hardcoded stable cap of 5."""
        from types import SimpleNamespace

        summarizer = EpisodeSummarizer.__new__(EpisodeSummarizer)
        summarizer._settings = SimpleNamespace(
            extraction_coverage_broadened=False,
            candidate_facts_stable_limit=15,
            candidate_facts_event_limit=30,
        )
        facts = [{"subject": f"s{i}", "content": f"c{i}", "category": "concept"} for i in range(20)]
        summaries = [
            {"title": "T", "summary": "S", "candidate_facts": facts[:10]},
            {"title": "T", "summary": "S", "candidate_facts": facts[10:]},
        ]
        merged = summarizer._merge_summaries(summaries)
        assert len(merged["candidate_facts"]) == 5


def test_coverage_expansion_instruction_guards():
    """The coverage-expansion block must keep its target categories + noise guard."""
    from nous.handlers.episode_summarizer import _COVERAGE_EXPANSION_INSTRUCTION as instr

    low = instr.lower()
    assert '"event"' in low and '"status"' in low and '"person"' in low
    assert "queryable" in low
    assert "exclude only pure conversational" in low


def test_cap_candidate_facts_single_source_of_truth():
    """The shared cap helper (summarizer + both FactExtractor storage paths)
    must apply the stable cap consistently: legacy 5 off, stable_limit on,
    None settings → legacy defaults."""
    from types import SimpleNamespace

    from nous.handlers import cap_candidate_facts

    stable = [{"subject": f"s{i}", "content": f"c{i}"} for i in range(20)]
    dated = [{"content": f"d{i}", "event_date": "2024-01-01"} for i in range(40)]
    cands = stable + dated

    off = SimpleNamespace(extraction_coverage_broadened=False,
                          candidate_facts_stable_limit=15, candidate_facts_event_limit=30)
    r = cap_candidate_facts(cands, off)
    assert sum(1 for c in r if not c.get("event_date")) == 5
    assert sum(1 for c in r if c.get("event_date")) == 30

    on = SimpleNamespace(extraction_coverage_broadened=True,
                         candidate_facts_stable_limit=15, candidate_facts_event_limit=30)
    r = cap_candidate_facts(cands, on)
    assert sum(1 for c in r if not c.get("event_date")) == 15

    # None settings (test __new__ paths) → legacy defaults
    r = cap_candidate_facts(cands, None)
    assert sum(1 for c in r if not c.get("event_date")) == 5


# ---------------------------------------------------------------------------
# Task 4: _summary_max_tokens + _select_chunks + F067 chunk-count cap
# ---------------------------------------------------------------------------


class TestSummaryMaxTokens:
    def test_override_respected(self, summarizer_factory):
        s = summarizer_factory(settings_overrides={"episode_summary_max_tokens": 5000})
        assert s._summary_max_tokens() == 5000

    def test_auto_broadened_returns_3000(self, summarizer_factory):
        s = summarizer_factory(settings_overrides={
            "episode_summary_max_tokens": 0,
            "extraction_coverage_broadened": True,
        })
        assert s._summary_max_tokens() == 3000

    def test_auto_open_threads_returns_3000(self, summarizer_factory):
        s = summarizer_factory(settings_overrides={
            "episode_summary_max_tokens": 0,
            "extraction_coverage_broadened": False,
            "episode_open_threads": True,
        })
        assert s._summary_max_tokens() == 3000

    def test_auto_neither_flag_returns_1500(self, summarizer_factory):
        s = summarizer_factory(settings_overrides={
            "episode_summary_max_tokens": 0,
            "extraction_coverage_broadened": False,
            "episode_open_threads": False,
        })
        assert s._summary_max_tokens() == 1500


class TestSelectChunks:
    def test_head_plus_tail_selection(self, summarizer_factory):
        """6 chunks, cap 4 → first cap-1 + final = [0,1,2,5]."""
        s = summarizer_factory(settings_overrides={"episode_summary_max_chunks": 4})
        chunks = [f"chunk-{i}" for i in range(6)]
        assert s._select_chunks(chunks) == ["chunk-0", "chunk-1", "chunk-2", "chunk-5"]

    def test_zero_cap_is_unlimited(self, summarizer_factory):
        s = summarizer_factory(settings_overrides={"episode_summary_max_chunks": 0})
        chunks = [f"chunk-{i}" for i in range(6)]
        assert s._select_chunks(chunks) == chunks

    def test_noop_under_cap(self, summarizer_factory):
        s = summarizer_factory(settings_overrides={"episode_summary_max_chunks": 4})
        chunks = ["a", "b"]
        assert s._select_chunks(chunks) == chunks

    def test_warn_logged_when_truncated(self, summarizer_factory, caplog):
        s = summarizer_factory(settings_overrides={"episode_summary_max_chunks": 3})
        chunks = [f"chunk-{i}" for i in range(5)]
        with caplog.at_level(logging.WARNING, logger="nous.handlers.episode_summarizer"):
            result = s._select_chunks(chunks)
        assert result == ["chunk-0", "chunk-1", "chunk-4"]
        assert any("5" in r.message and "3" in r.message for r in caplog.records)

    def test_cap_one_returns_only_final_chunk(self, summarizer_factory):
        """cap=1 → first 0 + final = [chunks[-1]]; documents the head+tail semantic at its edge."""
        s = summarizer_factory(settings_overrides={"episode_summary_max_chunks": 1})
        chunks = ["chunk-0", "chunk-1", "chunk-2"]
        result = s._select_chunks(chunks)
        assert result == ["chunk-2"]

    def test_cap_equal_to_len_is_noop(self, summarizer_factory):
        """cap == len(chunks) → unchanged (no truncation, no WARN)."""
        s = summarizer_factory(settings_overrides={"episode_summary_max_chunks": 3})
        chunks = ["chunk-0", "chunk-1", "chunk-2"]
        result = s._select_chunks(chunks)
        assert result == chunks


@pytest.mark.asyncio
async def test_f067_chunk_count_cap(summarizer_factory, caplog):
    """Transcript long enough for >3 chunks with chunk_size 50, cap=3 → only 3 embedded."""
    from nous.handlers.episode_summarizer import EpisodeSummarizer as ES

    # Build instance with a real _settings and a wired embedder
    s = summarizer_factory(settings_overrides={
        "episode_chunk_max_per_episode": 3,
        "episode_chunks_enabled": True,
        "episode_chunk_size": 50,
        "episode_chunk_overlap": 5,
        "episode_chunk_min_transcript_chars": 10,
        "agent_id": "test-agent",
    })

    # Fake embedder: records call count + returns fixed vectors
    call_count = 0

    async def fake_embed_batch(texts):
        nonlocal call_count
        call_count += 1
        return [[0.1] * 3 for _ in texts]

    embedder = MagicMock()
    embedder.embed_batch = AsyncMock(side_effect=fake_embed_batch)
    s._embedder = embedder

    # Transcript long enough to produce more than 3 chunks at chunk_size=50
    transcript = ("abcdefghij " * 30).strip()  # ~330 chars → ~6 chunks at size=50

    # Track INSERT calls
    inserted_indices = []

    async def fake_execute(sql_or_text, params=None):
        result = MagicMock()
        result.scalar = MagicMock(return_value=0)
        result.first = MagicMock(return_value=None)
        sql_str = str(sql_or_text)
        if "INSERT INTO heart.episode_chunks" in sql_str and params:
            inserted_indices.append(params.get("idx"))
        return result

    fake_session = AsyncMock()
    fake_session.execute = AsyncMock(side_effect=fake_execute)
    fake_session.commit = AsyncMock()

    fake_db = MagicMock()
    import contextlib

    @contextlib.asynccontextmanager
    async def _session_ctx():
        yield fake_session

    fake_db.session = _session_ctx
    s._heart = MagicMock()
    s._heart.db = fake_db
    s._heart._embeddings = None

    episode_id = uuid4()

    with caplog.at_level(logging.WARNING, logger="nous.handlers.episode_summarizer"):
        await s._chunk_and_store_transcript(
            episode_id=episode_id,
            agent_id="test-agent",
            transcript=transcript,
        )

    # Exactly 3 chunks should have been inserted
    assert len(inserted_indices) == 3, f"Expected 3 inserts, got {inserted_indices}"
    # embed_batch was called with exactly 3 texts
    embed_call_args = embedder.embed_batch.call_args[0][0]
    assert len(embed_call_args) == 3
    # WARN log must mention the episode and the cap
    assert any("F067" in r.message and "3" in r.message for r in caplog.records)
