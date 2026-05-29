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


def _hit(content: str, score: float, *, id=None, event_date=None, superseded_by=None) -> SimpleNamespace:
    return SimpleNamespace(
        id=id or uuid4(), content=content, score=score,
        event_date=event_date, superseded_by=superseded_by,
    )


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

    canonical, exclude_ids = await ext._resolve_dedup("MRR dropped five percent", None)
    assert canonical == para.id
    # the DISTINCT-judged opposite is recorded for native-dedup exclusion
    assert opp.id in exclude_ids


@pytest.mark.asyncio
async def test_resolve_dedup_stores_when_all_hits_distinct():
    """All above-threshold hits judged DISTINCT -> store as new, and every hit
    id is returned for native-dedup exclusion (codex P2)."""
    a, b = _hit("x up", 0.97), _hit("y down", 0.95)
    heart = MagicMock()
    heart.search_facts = AsyncMock(return_value=[a, b])
    heart.facts.is_distinct_fact = AsyncMock(return_value=True)
    ext = _extractor(heart, tiebreaker=True)
    canonical, exclude_ids = await ext._resolve_dedup("z", None)
    assert canonical is None
    assert set(exclude_ids) == {a.id, b.id}


@pytest.mark.asyncio
async def test_resolve_dedup_flag_off_dedups_top_hit_without_llm():
    """Flag off -> legacy single-top-hit dedup; tiebreaker never called and no
    exclusion ids (native dedup behaves exactly as pre-F377)."""
    top = _hit("anchor", 0.97)
    heart = MagicMock()
    heart.search_facts = AsyncMock(return_value=[top])
    heart.facts.is_distinct_fact = AsyncMock()
    ext = _extractor(heart, tiebreaker=False)

    canonical, exclude_ids = await ext._resolve_dedup("paraphrase", None)
    assert canonical == top.id
    assert exclude_ids == []
    heart.facts.is_distinct_fact.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_dedup_decides_top_hit_at_limit_1_scoring():
    """#469/P2-5: widening the dedup search must not drop the rank-1 hit below
    threshold. A single-channel duplicate scores above threshold at limit=1 but
    below at limit=5 (RRF penalty_rank = limit + 1); phase 1 resolves the top hit
    at limit=1, so it is still deduped instead of wrongly stored."""
    dup_id = uuid4()

    async def fake_search(content, limit):
        # same fact, limit-dependent RRF score (single-channel penalty grows)
        score = 0.94 if limit == 1 else 0.83
        return [_hit("near-dup", score, id=dup_id)]

    heart = MagicMock()
    heart.search_facts = AsyncMock(side_effect=fake_search)
    heart.facts.is_distinct_fact = AsyncMock(return_value=False)  # genuine duplicate
    ext = _extractor(heart, tiebreaker=True)

    canonical, exclude_ids = await ext._resolve_dedup("near duplicate", None)
    assert canonical == dup_id  # deduped via the limit=1 decision, not missed
    assert exclude_ids == []


@pytest.mark.asyncio
async def test_resolve_dedup_finds_superseder_when_top_is_soft_penalized():
    """#470/P2-6: apply_supersession_filter runs after truncation, so at limit=1
    a superseded rank-1 fact is soft-penalized ×0.3 below threshold (superseder
    absent). The widened pass must still run (top.superseded_by is set) and dedup
    against the superseder that surfaces at limit=5."""
    old_id, superseder_id = uuid4(), uuid4()

    async def fake_search(content, limit):
        if limit == 1:
            # superseded old fact, ×0.3 soft-penalized below threshold
            return [_hit("old value", 0.30, id=old_id, superseded_by=superseder_id)]
        # limit=5: old fact dropped by the supersession filter; superseder surfaces
        return [_hit("current value", 0.95, id=superseder_id)]

    heart = MagicMock()
    heart.search_facts = AsyncMock(side_effect=fake_search)
    heart.facts.is_distinct_fact = AsyncMock(return_value=False)  # superseder is a real dup
    ext = _extractor(heart, tiebreaker=True)

    canonical, exclude_ids = await ext._resolve_dedup("the value", None)
    assert canonical == superseder_id


@pytest.mark.asyncio
async def test_resolve_dedup_below_threshold_nonsuperseded_skips_phase2():
    """A genuine non-dup (below-threshold, non-superseded top) stores without the
    extra limit=5 search — no perf regression on the common clean-learn path."""
    heart = MagicMock()
    heart.search_facts = AsyncMock(return_value=[_hit("unrelated", 0.40)])
    heart.facts.is_distinct_fact = AsyncMock()
    ext = _extractor(heart, tiebreaker=True)

    canonical, exclude_ids = await ext._resolve_dedup("brand new fact", None)
    assert canonical is None
    assert exclude_ids == []
    heart.facts.is_distinct_fact.assert_not_called()
    # only the limit=1 phase-1 search ran
    assert heart.search_facts.await_count == 1


@pytest.mark.asyncio
async def test_resolve_dedup_skips_distinct_event_date():
    """F075 bypass: an above-threshold hit with a different event_date is not a
    duplicate, so it is skipped even before the tiebreaker (and not excluded)."""
    from datetime import date
    hit = _hit("same text", 0.97, event_date=date(2024, 1, 1))
    heart = MagicMock()
    heart.search_facts = AsyncMock(return_value=[hit])
    heart.facts.is_distinct_fact = AsyncMock()
    ext = _extractor(heart, tiebreaker=True)

    canonical, exclude_ids = await ext._resolve_dedup("same text", date(2024, 6, 1))
    assert canonical is None
    assert exclude_ids == []
    heart.facts.is_distinct_fact.assert_not_called()
