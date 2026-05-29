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


# ---------------------------------------------------------------------------
# FactExtractor._resolve_dedup — multi-hit checking (codex P2-1)
# ---------------------------------------------------------------------------

from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402

from nous.handlers.fact_extractor import FactExtractor  # noqa: E402


def _hit(content: str, score: float, *, id=None, event_date=None) -> SimpleNamespace:
    return SimpleNamespace(id=id or uuid4(), content=content, score=score, event_date=event_date)


def _extractor(heart, *, tiebreaker: bool) -> FactExtractor:
    settings = SimpleNamespace(
        fact_dedup_tiebreaker_enabled=tiebreaker,
        fact_dedup_threshold=0.92,
    )
    return FactExtractor(
        heart=heart, settings=settings, bus=None, llm_client=None, dedup_via_search=True
    )


@pytest.mark.asyncio
async def test_resolve_dedup_checks_lower_hits_when_top_is_distinct():
    """P2-1: a high-overlap opposite ranked #1 must not hide a true duplicate
    ranked #2. With the tiebreaker on, dedup against the lower paraphrase."""
    opp, para = _hit("MRR is up 5%", 0.97), _hit("MRR fell by 5 percent", 0.95)
    heart = MagicMock()
    heart.search_facts = AsyncMock(return_value=[opp, para])
    # top opposite -> DISTINCT (skip), lower paraphrase -> DUPLICATE (dedup)
    heart.facts.is_distinct_fact = AsyncMock(side_effect=[True, False])
    ext = _extractor(heart, tiebreaker=True)

    result = await ext._resolve_dedup("MRR dropped five percent", None)
    assert result == para.id


@pytest.mark.asyncio
async def test_resolve_dedup_stores_when_all_hits_distinct():
    """All above-threshold hits judged DISTINCT -> None (store as new)."""
    heart = MagicMock()
    heart.search_facts = AsyncMock(return_value=[_hit("x up", 0.97), _hit("y down", 0.95)])
    heart.facts.is_distinct_fact = AsyncMock(return_value=True)
    ext = _extractor(heart, tiebreaker=True)
    assert await ext._resolve_dedup("z", None) is None


@pytest.mark.asyncio
async def test_resolve_dedup_flag_off_dedups_top_hit_without_llm():
    """Flag off -> legacy single-top-hit dedup; the tiebreaker is never called."""
    top = _hit("anchor", 0.97)
    heart = MagicMock()
    heart.search_facts = AsyncMock(return_value=[top])
    heart.facts.is_distinct_fact = AsyncMock()
    ext = _extractor(heart, tiebreaker=False)

    assert await ext._resolve_dedup("paraphrase", None) == top.id
    heart.facts.is_distinct_fact.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_dedup_skips_distinct_event_date():
    """F075 bypass: an above-threshold hit with a different event_date is not a
    duplicate, so it is skipped even before the tiebreaker."""
    from datetime import date
    hit = _hit("same text", 0.97, event_date=date(2024, 1, 1))
    heart = MagicMock()
    heart.search_facts = AsyncMock(return_value=[hit])
    heart.facts.is_distinct_fact = AsyncMock()
    ext = _extractor(heart, tiebreaker=True)

    assert await ext._resolve_dedup("same text", date(2024, 6, 1)) is None
    heart.facts.is_distinct_fact.assert_not_called()
