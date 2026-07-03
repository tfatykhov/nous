"""Tests for S2 extraction-input hardening (2026-07-02 MAB CR root-cause).

The episode summarizer and knowledge extractor inject conversation text into
instruction-dense prompts with no data/instruction boundary. Instruction-like
input ("Please remember the following... (part 1/9)") makes the LLM misread
the transcript as instructions and echo its own prompt back as facts — the
nous_mab eval stored 11/11 verbatim prompt-echo facts and 0 content facts.

Hardening (flag ``extraction_input_hardening_enabled``, default ON):
1. Transcript/conversation wrapped in explicit delimiters + a
   DATA/INSTRUCTION BOUNDARY guard block appended to the prompt.
2. Deterministic echo backstop: candidate facts sharing a 6-word verbatim
   run with the generating prompt template are dropped post-parse.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from nous.handlers.episode_summarizer import (
    _SUMMARY_PROMPT,
    _F075_TEMPORAL_INSTRUCTION,
    EpisodeSummarizer,
)
from nous.handlers.knowledge_extractor import _EXTRACT_PROMPT, KnowledgeExtractor


# Verbatim prompt-echo facts observed in the nous_mab eval DB
# (agent mab-eval-prod_memory-8f18622a, 2026-07-02).
_ECHO_FAITHFULNESS = (
    "Summaries must only include claims directly supported by the transcript; "
    "do not invent user motivation, prior session context, or success criteria."
)
_ECHO_F075_EVENT = (
    "When a transcript describes an event on a specific date, capture it as a "
    "separate candidate_fact with category event and the date attached."
)
_ECHO_SHORT = "Return ONLY valid JSON"
_GENUINE_FACT = (
    "Romano Prodi is a citizen of Italy and served as Prime Minister of Italy "
    "and President of the European Commission."
)


def _summarizer(**overrides) -> EpisodeSummarizer:
    s = EpisodeSummarizer.__new__(EpisodeSummarizer)
    defaults = {
        "background_model": "test-model",
        "episode_summary_max_tokens": 0,
        "extraction_coverage_broadened": False,
        "episode_open_threads": False,
        "temporal_extraction_enabled": False,
        "extraction_input_hardening_enabled": True,
    }
    s._settings = SimpleNamespace(**{**defaults, **overrides})
    s._llm = None  # call_background_llm is monkeypatched in tests that reach it
    return s


# ---------------------------------------------------------------------------
# Echo guard helper
# ---------------------------------------------------------------------------


class TestDropPromptEchoFacts:
    def _guard(self, candidates):
        from nous.handlers import drop_prompt_echo_facts

        return drop_prompt_echo_facts(
            candidates,
            (_SUMMARY_PROMPT, _F075_TEMPORAL_INSTRUCTION),
            source="test",
        )

    def test_drops_verbatim_echo(self):
        kept = self._guard([{"subject": "s", "content": _ECHO_FAITHFULNESS}])
        assert kept == []

    def test_drops_f075_echo(self):
        kept = self._guard([{"subject": "s", "content": _ECHO_F075_EVENT}])
        assert kept == []

    def test_drops_short_echo_via_substring(self):
        kept = self._guard([{"subject": "s", "content": _ECHO_SHORT}])
        assert kept == []

    def test_keeps_genuine_content_fact(self):
        cand = {"subject": "Romano Prodi", "content": _GENUINE_FACT}
        assert self._guard([cand]) == [cand]

    def test_keeps_short_genuine_fact(self):
        cand = {"subject": "user", "content": "Tim prefers uv over pip"}
        assert self._guard([cand]) == [cand]

    def test_mixed_list_filters_only_echoes(self):
        genuine = {"subject": "Romano Prodi", "content": _GENUINE_FACT}
        kept = self._guard([
            {"subject": "s", "content": _ECHO_FAITHFULNESS},
            genuine,
            {"subject": "s", "content": _ECHO_F075_EVENT},
        ])
        assert kept == [genuine]

    def test_non_dict_and_empty_content_pass_through(self):
        # Malformed entries are downstream's problem — the guard must not crash
        # or swallow them.
        items = ["bare-string", {"subject": "s"}, {"content": ""}]
        assert self._guard(items) == items

    def test_logs_warning_on_drop(self, caplog):
        import logging

        with caplog.at_level(logging.WARNING, logger="nous.handlers"):
            self._guard([{"subject": "s", "content": _ECHO_FAITHFULNESS}])
        assert any("echo" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# Summarizer prompt hardening
# ---------------------------------------------------------------------------


class TestBuildSummaryPromptHardening:
    def test_flag_on_wraps_transcript_and_appends_guard(self):
        s = _summarizer()
        prompt = s._build_summary_prompt("User: hello\n\nAssistant: hi", "", None)
        assert "<transcript>\nUser: hello\n\nAssistant: hi\n</transcript>" in prompt
        assert "DATA/INSTRUCTION BOUNDARY" in prompt

    def test_flag_off_is_byte_identical_to_legacy(self):
        s = _summarizer(extraction_input_hardening_enabled=False)
        prompt = s._build_summary_prompt("User: hello", "ctx", None)
        assert prompt == _SUMMARY_PROMPT.format(
            transcript="User: hello", decision_context="ctx"
        )

    def test_guard_appended_after_flag_addenda(self):
        # The boundary guard must stay the LAST block so flag addenda
        # (F075/coverage/open-threads) can't displace it.
        s = _summarizer(temporal_extraction_enabled=True)
        prompt = s._build_summary_prompt("t", "", None)
        assert prompt.index("DATA/INSTRUCTION BOUNDARY") > prompt.index(
            "DATE-ANCHORED EVENTS (F075)"
        )


class TestSummarizeSingleEchoGuard:
    @pytest.mark.asyncio
    async def test_flag_on_drops_echoes_keeps_content(self, monkeypatch):
        genuine = {"subject": "Romano Prodi", "content": _GENUINE_FACT}
        s = _summarizer()

        async def fake_llm(*args, **kwargs):
            return json.dumps({
                "title": "T", "summary": "S",
                "candidate_facts": [
                    {"subject": "s", "content": _ECHO_FAITHFULNESS},
                    genuine,
                ],
            })

        monkeypatch.setattr(
            "nous.handlers.episode_summarizer.call_background_llm", fake_llm
        )
        result = await s._summarize_single("User: hello", "")
        assert result["candidate_facts"] == [genuine]

    @pytest.mark.asyncio
    async def test_flag_off_leaves_candidates_untouched(self, monkeypatch):
        echo = {"subject": "s", "content": _ECHO_FAITHFULNESS}
        s = _summarizer(extraction_input_hardening_enabled=False)

        async def fake_llm(*args, **kwargs):
            return json.dumps({"title": "T", "summary": "S",
                               "candidate_facts": [echo]})

        monkeypatch.setattr(
            "nous.handlers.episode_summarizer.call_background_llm", fake_llm
        )
        result = await s._summarize_single("User: hello", "")
        assert result["candidate_facts"] == [echo]


class TestSummarizeSingleArraySalvage:
    """A bulk data-dump chunk makes the LLM emit a bare JSON fact-array instead
    of the summary object. PR #525 made that a clean skip — which silently
    discards every fact in the chunk (observed: all 31,925-char chunks of the
    MAB CR transcript lost this way). Fact-shaped arrays must be salvaged."""

    @pytest.mark.asyncio
    async def test_fact_shaped_array_salvaged_as_candidate_facts(self, monkeypatch):
        facts = [
            {"subject": "Blue Origin", "content": "Blue Origin was founded by Jeff Bezos.", "category": "concept"},
            {"subject": "Facebook", "content": "Facebook was created by Mark Zuckerberg.", "category": "concept"},
        ]
        s = _summarizer()

        async def fake_llm(*args, **kwargs):
            return json.dumps(facts)

        monkeypatch.setattr(
            "nous.handlers.episode_summarizer.call_background_llm", fake_llm
        )
        result = await s._summarize_single("User: data dump", "")
        assert result is not None
        assert result["candidate_facts"] == facts

    @pytest.mark.asyncio
    async def test_salvaged_array_still_passes_echo_guard(self, monkeypatch):
        genuine = {"subject": "Blue Origin", "content": "Blue Origin was founded by Jeff Bezos.", "category": "concept"}
        s = _summarizer()

        async def fake_llm(*args, **kwargs):
            return json.dumps([
                {"subject": "s", "content": _ECHO_FAITHFULNESS, "category": "rule"},
                genuine,
            ])

        monkeypatch.setattr(
            "nous.handlers.episode_summarizer.call_background_llm", fake_llm
        )
        result = await s._summarize_single("User: data dump", "")
        assert result["candidate_facts"] == [genuine]

    @pytest.mark.asyncio
    async def test_flag_off_keeps_legacy_skip(self, monkeypatch):
        """Kill-switch semantics: flag OFF restores the PR #525 clean skip."""
        s = _summarizer(extraction_input_hardening_enabled=False)

        async def fake_llm(*args, **kwargs):
            return json.dumps([{"subject": "s", "content": "A fact.", "category": "concept"}])

        monkeypatch.setattr(
            "nous.handlers.episode_summarizer.call_background_llm", fake_llm
        )
        assert await s._summarize_single("User: hello", "") is None

    @pytest.mark.asyncio
    async def test_non_fact_array_still_skipped(self, monkeypatch):
        s = _summarizer()

        async def fake_llm(*args, **kwargs):
            return json.dumps(["just", "strings"])

        monkeypatch.setattr(
            "nous.handlers.episode_summarizer.call_background_llm", fake_llm
        )
        assert await s._summarize_single("User: hello", "") is None

    @pytest.mark.asyncio
    async def test_empty_array_still_skipped(self, monkeypatch):
        s = _summarizer()

        async def fake_llm(*args, **kwargs):
            return "[]"

        monkeypatch.setattr(
            "nous.handlers.episode_summarizer.call_background_llm", fake_llm
        )
        assert await s._summarize_single("User: hello", "") is None


# ---------------------------------------------------------------------------
# Knowledge extractor hardening
# ---------------------------------------------------------------------------


def _knowledge_extractor(**overrides) -> KnowledgeExtractor:
    ke = KnowledgeExtractor.__new__(KnowledgeExtractor)
    defaults = {
        "background_model": "test-model",
        "knowledge_extractor_max_chars": 24000,
        "extraction_input_hardening_enabled": True,
    }
    ke._settings = SimpleNamespace(**{**defaults, **overrides})
    ke._llm = object()  # non-None so _extract_facts proceeds
    return ke


class TestKnowledgeExtractorHardening:
    @pytest.mark.asyncio
    async def test_flag_on_wraps_conversation_and_appends_guard(self, monkeypatch):
        ke = _knowledge_extractor()
        captured = {}

        async def fake_llm(client, model, system_prompt, user_message, max_tokens=800):
            captured["prompt"] = user_message
            return "[]"

        monkeypatch.setattr(
            "nous.handlers.knowledge_extractor.call_background_llm", fake_llm
        )
        await ke._extract_facts("User: hello there friend")
        assert "<conversation>\nUser: hello there friend\n</conversation>" in captured["prompt"]
        assert "DATA/INSTRUCTION BOUNDARY" in captured["prompt"]

    @pytest.mark.asyncio
    async def test_flag_off_is_byte_identical_to_legacy(self, monkeypatch):
        ke = _knowledge_extractor(extraction_input_hardening_enabled=False)
        captured = {}

        async def fake_llm(client, model, system_prompt, user_message, max_tokens=800):
            captured["prompt"] = user_message
            return "[]"

        monkeypatch.setattr(
            "nous.handlers.knowledge_extractor.call_background_llm", fake_llm
        )
        await ke._extract_facts("User: hello")
        assert captured["prompt"] == _EXTRACT_PROMPT.format(
            conversation_text="User: hello"
        )

    @pytest.mark.asyncio
    async def test_flag_on_drops_prompt_echo_candidates(self, monkeypatch):
        ke = _knowledge_extractor()
        echo = {
            "subject": "rules",
            "content": ("Only explicit directives from the user should be "
                        "stored with category rule."),
            "category": "rule",
            "confidence": 0.9,
        }
        genuine = {
            "subject": "Tim",
            "content": "Tim deploys nous on a home server at 192.168.1.141.",
            "category": "technical",
            "confidence": 0.9,
        }

        async def fake_llm(*args, **kwargs):
            return json.dumps([echo, genuine])

        monkeypatch.setattr(
            "nous.handlers.knowledge_extractor.call_background_llm", fake_llm
        )
        result = await ke._extract_facts("User: hello")
        assert result == [genuine]

    @pytest.mark.asyncio
    async def test_flag_off_returns_parse_result_untouched(self, monkeypatch):
        ke = _knowledge_extractor(extraction_input_hardening_enabled=False)
        echo = {"subject": "s",
                "content": "Only explicit directives from the user",
                "category": "rule", "confidence": 0.9}

        async def fake_llm(*args, **kwargs):
            return json.dumps([echo])

        monkeypatch.setattr(
            "nous.handlers.knowledge_extractor.call_background_llm", fake_llm
        )
        result = await ke._extract_facts("User: hello")
        assert result == [echo]


# ---------------------------------------------------------------------------
# Settings default
# ---------------------------------------------------------------------------


def test_hardening_flag_defaults_on():
    from nous.config import Settings

    assert Settings.model_fields["extraction_input_hardening_enabled"].default is True
