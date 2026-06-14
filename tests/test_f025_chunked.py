"""Tests for F025 P3-B: Chunked summarization."""

from __future__ import annotations

import pytest
from nous.handlers.episode_summarizer import EpisodeSummarizer


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
