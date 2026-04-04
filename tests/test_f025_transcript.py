"""Tests for F025 P2-C: Configurable transcript truncation."""

from __future__ import annotations

import pytest
from nous.config import Settings


class TestTranscriptConfig:
    """P2-C config tests."""

    def test_default_max_chars_is_16000(self):
        settings = Settings()
        assert settings.transcript_max_chars == 16000

    def test_configurable_via_constructor(self):
        settings = Settings(transcript_max_chars=24000)
        assert settings.transcript_max_chars == 24000


class TestTruncateTranscript:
    """P2-C transcript truncation tests."""

    def test_short_transcript_unchanged(self):
        from nous.handlers.episode_summarizer import EpisodeSummarizer
        summarizer = EpisodeSummarizer.__new__(EpisodeSummarizer)
        transcript = "User: Hello\n\nAssistant: Hi there"
        result = summarizer._truncate_transcript(transcript, max_chars=16000)
        assert result == transcript

    def test_long_transcript_truncated_near_limit(self):
        from nous.handlers.episode_summarizer import EpisodeSummarizer
        summarizer = EpisodeSummarizer.__new__(EpisodeSummarizer)
        turns = [f"User: Question {i} {'detail ' * 20}\n\nAssistant: Answer {i} {'explanation ' * 20}" for i in range(200)]
        transcript = "\n\n".join(turns)
        assert len(transcript) > 16000
        result = summarizer._truncate_transcript(transcript, max_chars=16000)
        # Method keeps first+last turns and fills middle by score; result is
        # approximately bounded (first/last turn overhead + separators may
        # push slightly over the char budget).
        assert len(result) < len(transcript)
        assert len(result) < 18000  # well under original transcript length

    def test_old_limit_still_works(self):
        from nous.handlers.episode_summarizer import EpisodeSummarizer
        summarizer = EpisodeSummarizer.__new__(EpisodeSummarizer)
        turns = [f"User: Q{i}\n\nAssistant: A{i} {'x' * 100}" for i in range(200)]
        transcript = "\n\n".join(turns)
        result = summarizer._truncate_transcript(transcript, max_chars=8000)
        # Budget is approximate (first/last turns + separators may exceed)
        assert len(result) < len(transcript)
        assert len(result) < 10000

    def test_default_param_is_16000(self):
        """Method default should be 16000, not 8000."""
        from nous.handlers.episode_summarizer import EpisodeSummarizer
        import inspect
        sig = inspect.signature(EpisodeSummarizer._truncate_transcript)
        assert sig.parameters["max_chars"].default == 16000
