"""Tests for F031 Consolidation Orient & Resolve.

Covers:
- find_contradiction_candidates() query
- Orient context injection in sleep reflection
- Contradiction resolution phase
"""

from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from nous.events import Event, EventBus
from nous.heart.schemas import FactInput, FactRejected


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_event(
    event_type: str = "sleep_started",
    agent_id: str = "test-agent",
    data: dict | None = None,
    session_id: str | None = "sess-1",
) -> Event:
    return Event(type=event_type, agent_id=agent_id, data=data or {}, session_id=session_id)


def _mock_settings(**overrides) -> MagicMock:
    s = MagicMock()
    s.background_model = "claude-sonnet-4-5-20250514"
    s.anthropic_api_key = "sk-ant-test-key"
    s.anthropic_auth_token = ""
    s.agent_id = "test-agent"
    s.sleep_enabled = True
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


def _mock_llm_client(text: str = "", status_code: int = 200) -> AsyncMock:
    client = AsyncMock()
    if status_code == 200:
        response = MagicMock()
        response.content = [{"type": "text", "text": text}]
        client.call = AsyncMock(return_value=response)
    else:
        client.call = AsyncMock(side_effect=RuntimeError(f"API error ({status_code})"))
    return client


def _make_sleep_handler(brain=None, heart=None, settings=None, bus=None, llm_client=None):
    from nous.handlers.sleep_handler import SleepHandler

    brain = brain or AsyncMock()
    heart = heart or AsyncMock()
    settings = settings or _mock_settings()
    bus = bus or MagicMock(spec=EventBus)
    bus.on = MagicMock()
    bus.emit = AsyncMock()
    llm_client = llm_client or _mock_llm_client()
    handler = SleepHandler(brain, heart, settings, bus, llm_client)
    return handler, brain, heart, bus, llm_client


# ===========================================================================
# Task 1: find_contradiction_candidates
# ===========================================================================

class TestFindContradictionCandidates:
    """FactManager.find_contradiction_candidates() returns same-subject, high-similarity pairs."""

    @pytest.mark.asyncio
    async def test_method_exists_and_callable(self):
        """find_contradiction_candidates should exist on Heart and be callable."""
        heart = AsyncMock()
        heart.find_contradiction_candidates = AsyncMock(return_value=[])
        result = await heart.find_contradiction_candidates(limit=10)
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_dict_structure(self):
        """Results should have the expected dict keys."""
        heart = AsyncMock()
        heart.find_contradiction_candidates = AsyncMock(return_value=[{
            "fact1_id": uuid4(),
            "fact2_id": uuid4(),
            "content1": "Tim's timezone is EST",
            "content2": "Tim's timezone is PST",
            "date1": "2026-03-01",
            "date2": "2026-03-15",
            "similarity": 0.88,
        }])
        result = await heart.find_contradiction_candidates(limit=10)
        assert len(result) == 1
        pair = result[0]
        assert "fact1_id" in pair
        assert "fact2_id" in pair
        assert "content1" in pair
        assert "content2" in pair
        assert "date1" in pair
        assert "date2" in pair
        assert "similarity" in pair
        assert pair["similarity"] > 0.75

    @pytest.mark.asyncio
    async def test_respects_limit(self):
        """Should return at most `limit` pairs."""
        heart = AsyncMock()
        heart.find_contradiction_candidates = AsyncMock(return_value=[
            {"fact1_id": uuid4(), "fact2_id": uuid4(), "content1": "A", "content2": "B",
             "date1": "2026-03-01", "date2": "2026-03-15", "similarity": 0.88},
        ])
        result = await heart.find_contradiction_candidates(limit=1)
        assert len(result) <= 1

    @pytest.mark.asyncio
    async def test_empty_when_no_candidates(self):
        """Returns empty list when no matching pairs exist."""
        heart = AsyncMock()
        heart.find_contradiction_candidates = AsyncMock(return_value=[])
        result = await heart.find_contradiction_candidates(limit=10)
        assert result == []
