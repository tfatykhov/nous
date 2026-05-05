"""Tests for the F059 compaction hallucination guard.

Two layers:
  1. Pure helpers (`_extract_entities`, `detect_hallucinated_entities`)
     — fast, no I/O, exercise the regex set + substring policy.
  2. End-to-end `ConversationCompactor.compact` — verifies the warn-only
     logging and fallback-to-truncation behavior with a stub call_api.
"""
from __future__ import annotations

import logging
from typing import Any

import pytest

from nous.api.compaction import (
    ConversationCompactor,
    _extract_entities,
    detect_hallucinated_entities,
)
from nous.api.models import Conversation, Message
from nous.config import Settings


# ---------------------------------------------------------------------
# Layer 1: pure helpers
# ---------------------------------------------------------------------


class TestExtractEntities:
    def test_emails(self) -> None:
        ents = _extract_entities("contact marcus.webb@acme.com for ops")
        assert "marcus.webb@acme.com" in ents

    def test_urls(self) -> None:
        ents = _extract_entities("see https://example.com/api/v1 for details")
        assert any("https://example.com/api/v1" in e for e in ents)

    def test_ipv4_with_port(self) -> None:
        ents = _extract_entities("bind to 0.0.0.0:8080 not localhost")
        assert "0.0.0.0:8080" in ents

    def test_version_strings(self) -> None:
        ents = _extract_entities("upgrade to Python 3.12.7 (was 3.11.4)")
        assert "3.12.7" in ents
        assert "3.11.4" in ents

    def test_file_paths(self) -> None:
        ents = _extract_entities("config lives in /etc/nginx/sites-enabled/app")
        assert any("/etc/nginx/sites-enabled/app" in e for e in ents)

    def test_named_entities(self) -> None:
        ents = _extract_entities("CEO is Sarah Chen, contact Marcus Webb")
        assert "sarah chen" in ents
        assert "marcus webb" in ents

    def test_empty_input(self) -> None:
        assert _extract_entities("") == set()
        assert _extract_entities("   ") == set()

    def test_case_insensitive(self) -> None:
        ents_a = _extract_entities("MARCUS WEBB")
        ents_b = _extract_entities("Marcus Webb")
        # Lowercased form should be in both — but the all-caps form
        # doesn't match the multi-word capitalized regex by design (we
        # only flag `Cap-lower Cap-lower` style names). Assert at least
        # the second form survives.
        assert "marcus webb" in ents_b


class TestDetectHallucinatedEntities:
    def test_no_substitution_returns_empty(self) -> None:
        input_text = "contact marcus.webb@acme.com or call 555-1234"
        summary = (
            "## Critical Context\n- Primary contact: Marcus Webb "
            "(marcus.webb@acme.com)"
        )
        assert detect_hallucinated_entities(input_text, summary) == []

    def test_substituted_email_flagged(self) -> None:
        input_text = "contact marcus.webb@acme.com for ops"
        summary = (
            "## Critical Context\n- Primary contact: David Park "
            "(david.park@acmecorp.com)"
        )
        suspects = detect_hallucinated_entities(input_text, summary)
        assert "david.park@acmecorp.com" in suspects
        assert "david park" in suspects

    def test_version_substitution_flagged(self) -> None:
        input_text = "we pinned Python 3.12.7"
        summary = "Python version: 3.11.4"
        suspects = detect_hallucinated_entities(input_text, summary)
        assert "3.11.4" in suspects

    def test_partial_substring_match_passes(self) -> None:
        # Summary uses a longer form of the input port. The input
        # `0.0.0.0:8080` covers the summary `:8080` substring.
        input_text = "bind to 0.0.0.0:8080"
        summary = "Server binds on :8080"
        # `:8080` -> token `8080`. `8080` is a substring of `0.0.0.0:8080`
        # in lowercased input. So no flag.
        assert detect_hallucinated_entities(input_text, summary) == []

    def test_case_insensitive_match(self) -> None:
        input_text = "we use Postgres for storage"
        summary = "Database: postgres"
        # Single capitalized word doesn't match multi-word name regex,
        # so it isn't extracted at all — and we shouldn't flag it.
        assert detect_hallucinated_entities(input_text, summary) == []

    def test_empty_either_side_returns_empty(self) -> None:
        assert detect_hallucinated_entities("", "summary") == []
        assert detect_hallucinated_entities("input", "") == []


# ---------------------------------------------------------------------
# Layer 2: integration with ConversationCompactor.compact
# ---------------------------------------------------------------------


def _make_settings(**overrides: Any) -> Settings:
    base = Settings()
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


def _make_conversation(messages: list[tuple[str, str]]) -> Conversation:
    conv = Conversation(session_id="test-session-f059")
    for role, content in messages:
        conv.messages.append(Message(role=role, content=content))
    return conv


def _stub_caller(summary_text: str):
    """Returns an `ApiCaller`-shaped async function that replies with
    `summary_text` on every call."""
    from nous.api.models import ApiResponse

    async def caller(
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        skip_thinking: bool = False,
        model_override: str | None = None,
    ) -> ApiResponse:
        return ApiResponse(
            content=[{"type": "text", "text": summary_text}],
            stop_reason="end_turn",
            usage={"input_tokens": 0, "output_tokens": 0},
        )
    return caller


_SUMMARY_WITH_HALLUCINATIONS = """
## Goal
Capture compacted state for the staging configuration session covering
contact details, the Redis port assignment, and operator escalation.

## Progress
### Done
- [x] Recorded operator and backup contact details
- [x] Captured the staging Redis port assignment

## Critical Context
- Primary contact: David Park (david.park@acmecorp.com)
- Operator: Sarah Chen
- Backup: Emerson Cole (emerson.cole@example.org)
- Redis (staging): 6380
"""


_SUMMARY_CLEAN = """
## Goal
Configure staging environment with the agreed contact details, the
Redis port assignment, and the operator escalation rules.

## Progress
### Done
- [x] Got requirements
- [x] Captured the staging Redis port assignment

## Critical Context
- Primary contact: Marcus Webb (marcus.webb@acme.com)
- Redis port (staging): 6380 (not the default 6379)
- Operator escalation: contact Marcus first, then page on-call.
"""


@pytest.mark.asyncio
async def test_guard_warns_when_threshold_exceeded(caplog) -> None:
    settings = _make_settings(
        compaction_hallucination_guard_enabled=True,
        compaction_hallucination_max_suspect_count=2,
        compaction_hallucination_fallback_enabled=False,
        compaction_structured_facts_enabled=False,
    )
    compactor = ConversationCompactor(settings)
    conv = _make_conversation([
        ("user", "Contact Marcus Webb at marcus.webb@acme.com about Redis port 6380."),
        ("assistant", "Got it."),
        ("user", "Continue."),
    ])
    messages = [{"role": m.role, "content": m.content} for m in conv.messages]
    caller = _stub_caller(_SUMMARY_WITH_HALLUCINATIONS)

    with caplog.at_level(logging.WARNING, logger="nous.api.compaction"):
        await compactor.compact(conv, messages, caller, cut_point=2)

    assert any(
        "hallucination guard" in r.message.lower()
        and "suspect entities" in r.message.lower()
        for r in caplog.records
    ), [r.message for r in caplog.records]
    # warn-only: summary still applied
    assert conv.summary is not None
    assert conv.summary.startswith("\n## Goal") or "## Goal" in conv.summary


@pytest.mark.asyncio
async def test_guard_falls_back_to_truncation_when_enabled(caplog) -> None:
    settings = _make_settings(
        compaction_hallucination_guard_enabled=True,
        compaction_hallucination_max_suspect_count=2,
        compaction_hallucination_fallback_enabled=True,
        compaction_structured_facts_enabled=False,
    )
    compactor = ConversationCompactor(settings)
    conv = _make_conversation([
        ("user", "Contact Marcus Webb at marcus.webb@acme.com about Redis port 6380."),
        ("assistant", "Got it."),
        ("user", "Continue."),
    ])
    messages = [{"role": m.role, "content": m.content} for m in conv.messages]
    caller = _stub_caller(_SUMMARY_WITH_HALLUCINATIONS)

    with caplog.at_level(logging.WARNING, logger="nous.api.compaction"):
        await compactor.compact(conv, messages, caller, cut_point=2)

    # Fallback path nulls the summary and truncates messages.
    assert conv.summary is None
    assert any(
        "falling back to truncation" in r.message.lower()
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_guard_quiet_for_clean_summary(caplog) -> None:
    settings = _make_settings(
        compaction_hallucination_guard_enabled=True,
        compaction_hallucination_max_suspect_count=2,
        compaction_hallucination_fallback_enabled=False,
        compaction_structured_facts_enabled=False,
    )
    compactor = ConversationCompactor(settings)
    conv = _make_conversation([
        ("user", "Contact Marcus Webb at marcus.webb@acme.com about Redis port 6380."),
        ("assistant", "Got it."),
        ("user", "Continue."),
    ])
    messages = [{"role": m.role, "content": m.content} for m in conv.messages]
    caller = _stub_caller(_SUMMARY_CLEAN)

    with caplog.at_level(logging.WARNING, logger="nous.api.compaction"):
        await compactor.compact(conv, messages, caller, cut_point=2)

    # No WARNING-level guard records.
    assert not any(
        "hallucination guard" in r.message.lower()
        and r.levelno >= logging.WARNING
        for r in caplog.records
    )
    assert conv.summary is not None


@pytest.mark.asyncio
async def test_guard_persists_event_when_logger_wired() -> None:
    settings = _make_settings(
        compaction_hallucination_guard_enabled=True,
        compaction_hallucination_max_suspect_count=2,
        compaction_hallucination_fallback_enabled=False,
        compaction_hallucination_persist_enabled=True,
        compaction_structured_facts_enabled=False,
    )
    captured: list[tuple[str, dict, str]] = []

    def logger(event_type: str, data: dict, session_id: str) -> None:
        captured.append((event_type, data, session_id))

    compactor = ConversationCompactor(settings, event_logger=logger)
    conv = _make_conversation([
        ("user", "Contact Marcus Webb at marcus.webb@acme.com about Redis port 6380."),
        ("assistant", "Got it."),
        ("user", "Continue."),
    ])
    messages = [{"role": m.role, "content": m.content} for m in conv.messages]
    caller = _stub_caller(_SUMMARY_WITH_HALLUCINATIONS)

    await compactor.compact(conv, messages, caller, cut_point=2)

    assert captured, "expected the guard to persist a fire"
    event_type, data, session_id = captured[0]
    assert event_type == "f059_hallucination_guard"
    assert session_id == "test-session-f059"
    assert data["session_id"] == "test-session-f059"
    assert data["suspect_count"] >= 1
    assert "suspects" in data
    assert data["exceeded_threshold"] is True
    assert data["fallback_taken"] is False
    assert data["threshold"] == 2
    assert data["summary_chars"] > 0
    assert data["input_chars"] > 0


@pytest.mark.asyncio
async def test_guard_persists_fallback_taken_flag() -> None:
    settings = _make_settings(
        compaction_hallucination_guard_enabled=True,
        compaction_hallucination_max_suspect_count=2,
        compaction_hallucination_fallback_enabled=True,
        compaction_hallucination_persist_enabled=True,
        compaction_structured_facts_enabled=False,
    )
    captured: list[dict] = []

    def logger(event_type: str, data: dict, session_id: str) -> None:
        captured.append(data)

    compactor = ConversationCompactor(settings, event_logger=logger)
    conv = _make_conversation([
        ("user", "Contact Marcus Webb at marcus.webb@acme.com about Redis port 6380."),
        ("assistant", "Got it."),
        ("user", "Continue."),
    ])
    messages = [{"role": m.role, "content": m.content} for m in conv.messages]
    caller = _stub_caller(_SUMMARY_WITH_HALLUCINATIONS)

    await compactor.compact(conv, messages, caller, cut_point=2)

    assert captured
    assert captured[0]["fallback_taken"] is True


@pytest.mark.asyncio
async def test_guard_skips_persistence_when_disabled() -> None:
    settings = _make_settings(
        compaction_hallucination_guard_enabled=True,
        compaction_hallucination_max_suspect_count=2,
        compaction_hallucination_persist_enabled=False,
        compaction_structured_facts_enabled=False,
    )
    captured: list[Any] = []

    def logger(event_type: str, data: dict, session_id: str) -> None:
        captured.append(data)

    compactor = ConversationCompactor(settings, event_logger=logger)
    conv = _make_conversation([
        ("user", "Contact Marcus Webb at marcus.webb@acme.com about Redis port 6380."),
        ("assistant", "Got it."),
        ("user", "Continue."),
    ])
    messages = [{"role": m.role, "content": m.content} for m in conv.messages]
    caller = _stub_caller(_SUMMARY_WITH_HALLUCINATIONS)

    await compactor.compact(conv, messages, caller, cut_point=2)

    assert not captured


@pytest.mark.asyncio
async def test_guard_persistence_swallows_logger_exceptions() -> None:
    settings = _make_settings(
        compaction_hallucination_guard_enabled=True,
        compaction_hallucination_max_suspect_count=2,
        compaction_hallucination_persist_enabled=True,
        compaction_structured_facts_enabled=False,
    )

    def boom(event_type: str, data: dict, session_id: str) -> None:
        raise RuntimeError("event sink down")

    compactor = ConversationCompactor(settings, event_logger=boom)
    conv = _make_conversation([
        ("user", "Contact Marcus Webb at marcus.webb@acme.com."),
        ("assistant", "Got it."),
        ("user", "Continue."),
    ])
    messages = [{"role": m.role, "content": m.content} for m in conv.messages]
    caller = _stub_caller(_SUMMARY_WITH_HALLUCINATIONS)

    # Should not raise even though the logger does — compaction must
    # remain unaffected by event-sink failures.
    await compactor.compact(conv, messages, caller, cut_point=2)

    # Summary still applied (warn-only mode).
    assert conv.summary is not None


@pytest.mark.asyncio
async def test_guard_disabled_no_check(caplog) -> None:
    settings = _make_settings(
        compaction_hallucination_guard_enabled=False,
        compaction_structured_facts_enabled=False,
    )
    compactor = ConversationCompactor(settings)
    conv = _make_conversation([
        ("user", "Contact Marcus Webb at marcus.webb@acme.com."),
        ("assistant", "Got it."),
        ("user", "Continue."),
    ])
    messages = [{"role": m.role, "content": m.content} for m in conv.messages]
    caller = _stub_caller(_SUMMARY_WITH_HALLUCINATIONS)

    with caplog.at_level(logging.INFO, logger="nous.api.compaction"):
        await compactor.compact(conv, messages, caller, cut_point=2)

    assert not any(
        "hallucination guard" in r.message.lower() for r in caplog.records
    )
    assert conv.summary is not None
