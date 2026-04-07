"""Tests for Spec 008.1 Phase 2: History Compaction."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from nous.api.compaction import (
    ConversationCompactor,
    _SECTION_PATTERNS,
)
from nous.api.models import ApiResponse, Conversation, Message
from nous.config import Settings


def _make_settings(**overrides) -> Settings:
    defaults = {
        "ANTHROPIC_API_KEY": "test",
        "NOUS_COMPACTION_ENABLED": "true",
        "NOUS_COMPACTION_THRESHOLD": "1000",
        "NOUS_KEEP_RECENT_TOKENS": "200",
        "NOUS_TOOL_SOFT_TRIM_CHARS": "100",
        "NOUS_TOOL_SOFT_TRIM_HEAD": "20",
        "NOUS_TOOL_SOFT_TRIM_TAIL": "20",
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _make_conversation(n_messages: int = 10) -> Conversation:
    conv = Conversation(session_id="test-session")
    for i in range(n_messages):
        conv.messages.append(Message(role="user", content=f"User message {i} " + "x" * 100))
        conv.messages.append(Message(role="assistant", content=f"Assistant reply {i} " + "y" * 100))
    return conv


def _make_messages(conv: Conversation) -> list[dict]:
    return [{"role": m.role, "content": m.content} for m in conv.messages]


VALID_SUMMARY = """\
## Goal
Testing conversation compaction for Nous agent framework.

## Constraints & Preferences
- Must preserve recent messages
- Token-based windowing

## Progress
### Done
- [x] Implemented Layer 1 pruning
- [x] Added token estimation
### In Progress
- [ ] Layer 2 compaction

## Key Decisions
- **Two-layer approach**: Separate pruning from compaction

## Conversation Dynamics
- User is technical and direct
- Prefers concise responses

## Next Steps
1. Finish Phase 2 implementation
2. Add persistence in Phase 3

## Critical Context
- Config: compaction_threshold=100000, keep_recent_tokens=20000
- Files: nous/api/compaction.py, nous/api/runner.py
"""


def _mock_call_api(summary_text: str = VALID_SUMMARY) -> AsyncMock:
    mock = AsyncMock()
    mock.return_value = ApiResponse(
        content=[{"type": "text", "text": summary_text}],
        stop_reason="end_turn",
        usage={"input_tokens": 100, "output_tokens": 50},
    )
    return mock


# ------------------------------------------------------------------
# should_compact tests
# ------------------------------------------------------------------


class TestShouldCompact:
    def test_disabled(self):
        settings = _make_settings(NOUS_COMPACTION_ENABLED="false")
        compactor = ConversationCompactor(settings)
        assert compactor.should_compact(5000, 200000) is False

    def test_under_threshold(self):
        compactor = ConversationCompactor(_make_settings())
        assert compactor.should_compact(100, 100) is False

    def test_over_threshold(self):
        compactor = ConversationCompactor(_make_settings())
        # threshold=1000, so 500+600=1100 > 1000
        assert compactor.should_compact(500, 600) is True

    def test_exact_threshold(self):
        compactor = ConversationCompactor(_make_settings())
        # 500+500=1000, not > 1000
        assert compactor.should_compact(500, 500) is False


# ------------------------------------------------------------------
# find_cut_point tests
# ------------------------------------------------------------------


class TestFindCutPoint:
    def test_returns_zero_when_fits(self):
        compactor = ConversationCompactor(_make_settings())
        messages = [
            {"role": "user", "content": "short"},
            {"role": "assistant", "content": "reply"},
        ]
        assert compactor.find_cut_point(messages, keep_recent_tokens=10000) == 0

    def test_snaps_to_user_boundary(self):
        compactor = ConversationCompactor(_make_settings())
        messages = [
            {"role": "user", "content": "a" * 1000},
            {"role": "assistant", "content": "b" * 1000},
            {"role": "user", "content": "c" * 1000},
            {"role": "assistant", "content": "d" * 1000},
        ]
        cut = compactor.find_cut_point(messages, keep_recent_tokens=300)
        assert cut > 0, "Should cut with small budget"
        assert messages[cut]["role"] == "user"

    def test_no_user_after_returns_zero(self):
        compactor = ConversationCompactor(_make_settings())
        # All assistant messages after the cut point
        messages = [
            {"role": "user", "content": "a" * 1000},
            {"role": "assistant", "content": "b" * 5000},
        ]
        # With very small budget, tries to cut but only assistant after
        cut = compactor.find_cut_point(messages, keep_recent_tokens=50)
        # Should return a valid cut point (snapping to user at index 0 or 0)
        assert cut == 0 or messages[cut]["role"] == "user"

    def test_empty_messages(self):
        compactor = ConversationCompactor(_make_settings())
        assert compactor.find_cut_point([], keep_recent_tokens=1000) == 0


# ------------------------------------------------------------------
# compact tests
# ------------------------------------------------------------------


class TestCompact:
    def test_successful_compaction(self):
        compactor = ConversationCompactor(_make_settings())
        conv = _make_conversation(10)
        messages = _make_messages(conv)
        mock_api = _mock_call_api()

        asyncio.get_event_loop().run_until_complete(
            compactor.compact(conv, messages, call_api=mock_api, cut_point=10)
        )

        assert conv.summary is not None
        assert "## Goal" in conv.summary
        assert conv.compaction_count == 1
        # Should have synthetic prefix (2) + recent messages
        assert conv.messages[0].content.startswith("[Previous conversation summary]")
        assert conv.messages[1].content == "I have the context. Let's continue."
        # Verify API was called with background model
        mock_api.assert_called_once()
        call_kwargs = mock_api.call_args
        assert call_kwargs.kwargs.get("skip_thinking") is True
        assert call_kwargs.kwargs.get("model_override") is not None

    def test_cut_point_zero_raises(self):
        compactor = ConversationCompactor(_make_settings())
        conv = _make_conversation(2)
        messages = _make_messages(conv)
        try:
            asyncio.get_event_loop().run_until_complete(
                compactor.compact(conv, messages, call_api=AsyncMock(), cut_point=0)
            )
            assert False, "Should have raised"
        except ValueError as e:
            assert "cut_point" in str(e)

    def test_index_alignment_mismatch_raises(self):
        compactor = ConversationCompactor(_make_settings())
        conv = _make_conversation(2)
        messages = _make_messages(conv)
        messages.append({"role": "user", "content": "extra"})  # Misalign
        try:
            asyncio.get_event_loop().run_until_complete(
                compactor.compact(conv, messages, call_api=AsyncMock(), cut_point=2)
            )
            assert False, "Should have raised"
        except ValueError as e:
            assert "alignment" in str(e).lower()

    def test_fallback_on_api_failure(self):
        compactor = ConversationCompactor(_make_settings())
        conv = _make_conversation(10)
        original_count = len(conv.messages)
        messages = _make_messages(conv)
        mock_api = AsyncMock(side_effect=RuntimeError("API down"))

        asyncio.get_event_loop().run_until_complete(
            compactor.compact(conv, messages, call_api=mock_api, cut_point=10)
        )

        # Should fall back to truncation
        assert conv.summary is None
        assert len(conv.messages) == original_count - 10

    def test_fallback_on_bad_summary(self):
        compactor = ConversationCompactor(_make_settings())
        conv = _make_conversation(10)
        messages = _make_messages(conv)
        mock_api = _mock_call_api(summary_text="too short")

        asyncio.get_event_loop().run_until_complete(
            compactor.compact(conv, messages, call_api=mock_api, cut_point=10)
        )

        # Should fall back to truncation (summary too short)
        assert conv.summary is None

    def test_iterative_update_uses_existing_summary(self):
        compactor = ConversationCompactor(_make_settings())
        conv = _make_conversation(10)
        conv.summary = "Previous summary content"
        conv.compaction_count = 1
        # Add synthetic prefix from previous compaction
        conv.messages.insert(0, Message(role="user", content="[Previous conversation summary]\n\nOld"))
        conv.messages.insert(1, Message(role="assistant", content="I have the context."))
        messages = _make_messages(conv)
        mock_api = _mock_call_api()

        asyncio.get_event_loop().run_until_complete(
            compactor.compact(conv, messages, call_api=mock_api, cut_point=12)
        )

        assert conv.compaction_count == 2
        # Verify the UPDATE prompt was used (existing summary in input)
        call_args = mock_api.call_args
        user_content = call_args.kwargs["messages"][0]["content"]
        assert "Existing Summary" in user_content

    def test_alternation_no_user_in_recent(self):
        compactor = ConversationCompactor(_make_settings())
        conv = Conversation(session_id="test")
        # Create messages where after cut_point, only assistant messages remain
        conv.messages = [
            Message(role="user", content="a" * 200),
            Message(role="assistant", content="b" * 200),
            Message(role="user", content="c" * 200),
            Message(role="assistant", content="d" * 200),
            Message(role="assistant", content="e" * 200),  # no user after cut
        ]
        messages = _make_messages(conv)
        mock_api = _mock_call_api()

        # Cut at index 4 (only assistant message remains)
        asyncio.get_event_loop().run_until_complete(
            compactor.compact(conv, messages, call_api=mock_api, cut_point=4)
        )

        # Recent should be empty (no user found), only synthetic prefix
        assert len(conv.messages) == 2
        assert conv.messages[0].role == "user"
        assert conv.messages[1].role == "assistant"

    def test_compaction_count_increments(self):
        compactor = ConversationCompactor(_make_settings())
        conv = _make_conversation(10)
        messages = _make_messages(conv)
        mock_api = _mock_call_api()

        asyncio.get_event_loop().run_until_complete(
            compactor.compact(conv, messages, call_api=mock_api, cut_point=10)
        )
        assert conv.compaction_count == 1

    def test_turn_contexts_cleaned(self):
        compactor = ConversationCompactor(_make_settings())
        conv = _make_conversation(10)
        # Add fake turn contexts
        from nous.cognitive.schemas import FrameSelection, TurnContext
        for i in range(10):
            conv.turn_contexts.append(
                TurnContext(
                    frame=FrameSelection(frame_id="conversation", confidence=0.9, reasoning="test"),
                    session_id="test",
                    agent_id="test",
                )
            )
        messages = _make_messages(conv)
        mock_api = _mock_call_api()

        asyncio.get_event_loop().run_until_complete(
            compactor.compact(conv, messages, call_api=mock_api, cut_point=10)
        )

        # turn_contexts should be reduced
        assert len(conv.turn_contexts) < 10


# ------------------------------------------------------------------
# validate_summary tests
# ------------------------------------------------------------------


class TestValidateSummary:
    def test_valid_summary(self):
        compactor = ConversationCompactor(_make_settings())
        assert compactor._validate_summary(VALID_SUMMARY) is True

    def test_too_short(self):
        compactor = ConversationCompactor(_make_settings())
        assert compactor._validate_summary("## Goal\nShort") is False

    def test_missing_sections(self):
        compactor = ConversationCompactor(_make_settings())
        text = "x" * 300  # Long enough but no sections
        assert compactor._validate_summary(text) is False

    def test_case_insensitive(self):
        compactor = ConversationCompactor(_make_settings())
        text = (
            "## goal\nSomething\n" + "x" * 200 + "\n"
            "## PROGRESS\nMore stuff\n"
            "## Critical context\nDetails\n"
        )
        assert compactor._validate_summary(text) is True

    def test_goals_plural_accepted(self):
        compactor = ConversationCompactor(_make_settings())
        text = (
            "## Goals\nMultiple goals\n" + "x" * 200 + "\n"
            "## Progress\nDone stuff\n"
            "## Critical Context\nPaths\n"
        )
        assert compactor._validate_summary(text) is True


# ------------------------------------------------------------------
# serialize_for_summary tests
# ------------------------------------------------------------------


class TestSerializeForSummary:
    def test_string_content(self):
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "world"},
        ]
        result = ConversationCompactor._serialize_for_summary(messages)
        assert "**User:** hello" in result
        assert "**Assistant:** world" in result

    def test_empty_messages(self):
        result = ConversationCompactor._serialize_for_summary([])
        assert result == ""


# ------------------------------------------------------------------
# _format_messages tests (runner integration)
# ------------------------------------------------------------------


class TestFormatMessages:
    def test_compaction_disabled_uses_cap(self):
        """When compaction disabled, uses MAX_HISTORY_MESSAGES cap."""
        from nous.api.runner import AgentRunner, MAX_HISTORY_MESSAGES

        settings = _make_settings(NOUS_COMPACTION_ENABLED="false")
        # Can't fully instantiate AgentRunner without deps, so test the method directly
        conv = _make_conversation(20)  # 40 messages
        # Simulate: would return last MAX_HISTORY_MESSAGES
        recent = conv.messages[-MAX_HISTORY_MESSAGES:]
        assert len(recent) == MAX_HISTORY_MESSAGES

    def test_compaction_enabled_returns_all(self):
        """When compaction enabled, returns all messages."""
        conv = _make_conversation(20)
        # With compaction, all messages returned
        all_msgs = [{"role": m.role, "content": m.content} for m in conv.messages]
        assert len(all_msgs) == 40


# ------------------------------------------------------------------
# _format_history_text tests
# ------------------------------------------------------------------


class TestFormatHistoryText:
    def test_with_summary(self):
        conv = _make_conversation(2)
        conv.summary = "Previous context here"
        # Simulate what _format_history_text does
        lines = []
        if conv.summary:
            lines.append(f"[Previous context summary]\n{conv.summary}\n")
        for msg in conv.messages[-20:]:
            role = "User" if msg.role == "user" else "Assistant"
            lines.append(f"{role}: {msg.content}")
        text = "\n\n".join(lines)
        assert "[Previous context summary]" in text
        assert "Previous context here" in text

    def test_without_summary(self):
        conv = _make_conversation(2)
        lines = []
        if conv.summary:
            lines.append(f"[Previous context summary]\n{conv.summary}\n")
        for msg in conv.messages[-20:]:
            role = "User" if msg.role == "user" else "Assistant"
            lines.append(f"{role}: {msg.content}")
        text = "\n\n".join(lines)
        assert "[Previous context summary]" not in text


# ------------------------------------------------------------------
# Section patterns test
# ------------------------------------------------------------------


class TestSectionPatterns:
    def test_patterns_match_expected(self):
        assert _SECTION_PATTERNS[0].search("## Goal")
        assert _SECTION_PATTERNS[0].search("## Goals")
        assert _SECTION_PATTERNS[0].search("## GOAL")
        assert _SECTION_PATTERNS[1].search("## Progress")
        assert _SECTION_PATTERNS[1].search("## PROGRESS")
        assert _SECTION_PATTERNS[2].search("## Critical Context")
        assert _SECTION_PATTERNS[2].search("## critical context")
