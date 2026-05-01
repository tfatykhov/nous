"""F058 follow-up: structured-output compaction tests.

Validates `ConversationCompactor._summarize_structured` and the
`_format_structured_checkpoint` renderer that backs it.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from nous.api.compaction import (
    _CHECKPOINT_TOOL_SCHEMA,
    _format_structured_checkpoint,
    ConversationCompactor,
)
from nous.config import Settings


def _settings(structured: bool = True) -> Settings:
    return Settings(
        compaction_structured_facts_enabled=structured,
        background_model="claude-sonnet-4-6",
    )


# ---------------------------------------------------------------------------
# _format_structured_checkpoint — renderer tests
# ---------------------------------------------------------------------------


def test_renderer_produces_required_sections():
    """All three sections required by _validate_summary appear."""
    rendered = _format_structured_checkpoint({
        "goal": "Test goal",
        "constraints": [],
        "progress_done": ["finished thing"],
        "progress_in_progress": [],
        "key_decisions": [],
        "conversation_dynamics": [],
        "next_steps": [],
        "critical_context": [{"topic": "port", "value": "8080"}],
    })
    assert "## Goal" in rendered
    assert "## Progress" in rendered
    assert "## Critical Context" in rendered


def test_renderer_preserves_verbatim_values():
    """Specific values in critical_context appear verbatim."""
    rendered = _format_structured_checkpoint({
        "goal": "G",
        "constraints": [],
        "progress_done": [],
        "progress_in_progress": [],
        "key_decisions": [],
        "conversation_dynamics": [],
        "next_steps": [],
        "critical_context": [
            {"topic": "Redis port (staging)", "value": "6380"},
            {"topic": "API account", "value": "123456789012"},
            {"topic": "Primary contact", "value": "marcus.webb@acme.com"},
        ],
    })
    assert "6380" in rendered
    assert "123456789012" in rendered
    assert "marcus.webb@acme.com" in rendered


def test_renderer_handles_empty_fact_ledger():
    """When critical_context is empty, the section header still appears
    (validator requires it)."""
    rendered = _format_structured_checkpoint({
        "goal": "G", "constraints": [], "progress_done": [],
        "progress_in_progress": [], "key_decisions": [],
        "conversation_dynamics": [], "next_steps": [], "critical_context": [],
    })
    assert "## Critical Context" in rendered
    assert "(no specific values recorded)" in rendered


def test_renderer_handles_decision_objects():
    rendered = _format_structured_checkpoint({
        "goal": "G", "constraints": [], "progress_done": [],
        "progress_in_progress": [],
        "key_decisions": [
            {"decision": "Use Postgres", "rationale": "Has JSONB"}
        ],
        "conversation_dynamics": [], "next_steps": [], "critical_context": [],
    })
    assert "**Use Postgres**" in rendered
    assert "Has JSONB" in rendered


# ---------------------------------------------------------------------------
# _summarize_structured — tool-call extraction tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summarize_structured_returns_rendered_on_success():
    compactor = ConversationCompactor(_settings(True))

    tool_payload = {
        "goal": "Set up CI",
        "constraints": ["Must use GitHub Actions"],
        "progress_done": ["Wrote workflow file"],
        "progress_in_progress": [],
        "key_decisions": [],
        "conversation_dynamics": [],
        "next_steps": ["Test the deploy"],
        "critical_context": [
            {"topic": "AWS account", "value": "123456789012"},
            {"topic": "Slack secret", "value": "ci-slack-webhook"},
        ],
    }
    mock_response = MagicMock()
    mock_response.content = [
        {"type": "tool_use", "name": "checkpoint_summary", "input": tool_payload}
    ]
    call_api = AsyncMock(return_value=mock_response)

    result = await compactor._summarize_structured(
        user_content="prior conversation",
        system="system prompt",
        call_api=call_api,
    )
    assert result is not None
    assert "123456789012" in result
    assert "ci-slack-webhook" in result
    assert "## Critical Context" in result


@pytest.mark.asyncio
async def test_summarize_structured_returns_none_on_no_tool_use():
    """If the model returns plain text instead of a tool_use block, return None."""
    compactor = ConversationCompactor(_settings(True))
    mock_response = MagicMock()
    mock_response.content = [{"type": "text", "text": "Some prose"}]
    call_api = AsyncMock(return_value=mock_response)

    result = await compactor._summarize_structured(
        user_content="x", system="y", call_api=call_api,
    )
    assert result is None


@pytest.mark.asyncio
async def test_summarize_structured_swallows_exceptions():
    """An API error from the call doesn't propagate; returns None."""
    compactor = ConversationCompactor(_settings(True))
    call_api = AsyncMock(side_effect=RuntimeError("API down"))

    result = await compactor._summarize_structured(
        user_content="x", system="y", call_api=call_api,
    )
    assert result is None


@pytest.mark.asyncio
async def test_summarize_structured_below_validator_minimum_returns_none():
    """Codex P1 follow-up to #395: structured output rendering to <200
    chars must return None so the legacy fallback runs. Previously the
    threshold was 100, mismatched with the validator's 200, causing a
    wasted API call."""
    compactor = ConversationCompactor(_settings(True))

    # Minimal payload renders to ~120 chars — below the validator's 200.
    tool_payload = {
        "goal": "G",
        "constraints": [],
        "progress_done": [],
        "progress_in_progress": [],
        "key_decisions": [],
        "conversation_dynamics": [],
        "next_steps": [],
        "critical_context": [],
    }
    mock_response = MagicMock()
    mock_response.content = [
        {"type": "tool_use", "name": "checkpoint_summary", "input": tool_payload}
    ]
    call_api = AsyncMock(return_value=mock_response)

    result = await compactor._summarize_structured(
        user_content="x", system="y", call_api=call_api,
    )
    assert result is None


@pytest.mark.asyncio
async def test_summarize_falls_back_to_freeform_when_structured_fails():
    """When the structured path returns None, _summarize tries the legacy path."""
    compactor = ConversationCompactor(_settings(True))

    # First call returns no tool_use, second call returns the legacy text.
    structured_response = MagicMock()
    structured_response.content = [{"type": "text", "text": "no tool"}]

    legacy_response = MagicMock()
    legacy_response.content = [
        {"type": "text", "text": (
            "## Goal\nLegacy summary.\n## Progress\n### Done\n- thing\n"
            "## Critical Context\n- X: Y\n"
        )}
    ]

    call_api = AsyncMock(side_effect=[structured_response, legacy_response])
    result = await compactor._summarize(
        old_messages=[{"role": "user", "content": "prior chat"}],
        existing_summary=None,
        call_api=call_api,
    )
    # Two calls made: one structured (failed), one legacy (succeeded).
    assert call_api.await_count == 2
    assert "Legacy summary" in result


@pytest.mark.asyncio
async def test_summarize_skips_structured_when_disabled():
    """compaction_structured_facts_enabled=False reverts to legacy path only."""
    compactor = ConversationCompactor(_settings(structured=False))

    legacy_response = MagicMock()
    legacy_response.content = [
        {"type": "text", "text": "## Goal\nLegacy.\n## Progress\n### Done\n- t\n## Critical Context\n- A: B"}
    ]
    call_api = AsyncMock(return_value=legacy_response)

    result = await compactor._summarize(
        old_messages=[{"role": "user", "content": "x"}],
        existing_summary=None,
        call_api=call_api,
    )
    # Only one call (legacy), no structured attempt.
    assert call_api.await_count == 1
    assert "Legacy" in result


def test_schema_required_fields():
    """Ensure the tool schema has the load-bearing fields in `required`."""
    required = set(_CHECKPOINT_TOOL_SCHEMA["required"])
    assert "critical_context" in required, (
        "critical_context MUST be required — it's the fact ledger"
    )
    assert "goal" in required
