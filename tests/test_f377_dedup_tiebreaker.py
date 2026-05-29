"""Tests for F377 Leg-1 dedup tiebreaker (FactManager.is_distinct_fact).

The tiebreaker resolves the RRF over-dedup of high-lexical-overlap semantic
opposites. It must:
- return None when no LLM client is wired (caller fails open -> dedup),
- map a DISTINCT verdict to True (store, don't dedup),
- map a DUPLICATE verdict to False (dedup),
- return None on malformed output or LLM error (fail open).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from nous.heart.facts import FactManager


def _mock_llm_response(result_dict: dict) -> AsyncMock:
    """Mock LLM client returning a single tool_use block (matches the shape
    call_background_llm_structured extracts: first type=='tool_use' -> input)."""
    response = MagicMock()
    response.content = [
        {"type": "tool_use", "id": "call_1", "name": "classify_dedup", "input": result_dict}
    ]
    client = AsyncMock()
    client.call = AsyncMock(return_value=response)
    return client


def _fm() -> FactManager:
    return FactManager(db=MagicMock(), embeddings=None, agent_id="test")


@pytest.mark.asyncio
async def test_returns_none_without_llm():
    """No LLM wired -> None so the caller fails open to current dedup behavior."""
    fm = _fm()
    assert await fm.is_distinct_fact("MRR is down 5%", "MRR fell by 5 percent") is None


@pytest.mark.asyncio
async def test_distinct_verdict_returns_true():
    fm = _fm()
    fm.set_llm_client(_mock_llm_response({"verdict": "DISTINCT"}))
    # semantic opposite -> store, don't dedup
    assert await fm.is_distinct_fact("MRR is down 5%", "MRR is up 5%") is True


@pytest.mark.asyncio
async def test_duplicate_verdict_returns_false():
    fm = _fm()
    fm.set_llm_client(_mock_llm_response({"verdict": "DUPLICATE"}))
    # paraphrase -> dedup
    assert await fm.is_distinct_fact("MRR is down 5%", "MRR fell by 5 percent") is False


@pytest.mark.asyncio
async def test_malformed_output_returns_none():
    """Missing verdict key -> None (fail open)."""
    fm = _fm()
    fm.set_llm_client(_mock_llm_response({"unexpected": "shape"}))
    assert await fm.is_distinct_fact("a", "b") is None


@pytest.mark.asyncio
async def test_llm_error_returns_none():
    """LLM call raising -> None (fail open, never block the learn path)."""
    fm = _fm()
    client = AsyncMock()
    client.call = AsyncMock(side_effect=Exception("LLM error"))
    fm.set_llm_client(client)
    assert await fm.is_distinct_fact("a", "b") is None
