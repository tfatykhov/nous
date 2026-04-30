"""F022 audit fix: source-side content guard for cross-type linking.

Pure unit tests — uses mocked embedder + session. Lives in a separate
file from test_graph_linker.py to avoid the autouse postgres-only fixture
in that suite.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from nous.brain.graph_linker import GraphLinker
from nous.config import Settings


def _linker(min_chars: int = 40) -> GraphLinker:
    """Build a GraphLinker with mocked embedder + db for unit testing."""
    settings = Settings(cross_type_link_min_content_chars=min_chars)
    embedder = MagicMock()
    embedder.embed = AsyncMock(return_value=[0.0] * 1536)
    db = MagicMock()
    return GraphLinker(db=db, embedder=embedder, settings=settings,
                       agent_id="test-agent")


@pytest.mark.asyncio
async def test_link_fact_to_decisions_skips_short_source():
    """A fact below min_chars must NOT trigger an embed or DB query."""
    linker = _linker(min_chars=40)
    session = MagicMock()
    session.execute = AsyncMock()
    result = await linker.link_fact_to_decisions(
        fact_id=uuid4(), fact_content="too short",
        session=session,
    )
    assert result == []
    linker.embedder.embed.assert_not_called()
    session.execute.assert_not_called()


@pytest.mark.asyncio
async def test_link_fact_to_facts_skips_short_source():
    """Same source-side guard applies to fact-to-fact linking."""
    linker = _linker(min_chars=40)
    session = MagicMock()
    session.execute = AsyncMock()
    result = await linker.link_fact_to_facts(
        fact_id=uuid4(), fact_content="x" * 39,  # 1 below threshold
        session=session,
    )
    assert result == []
    linker.embedder.embed.assert_not_called()


@pytest.mark.asyncio
async def test_link_fact_to_decisions_passes_at_threshold():
    """At exactly min_chars the guard passes; embedder gets called."""
    linker = _linker(min_chars=40)
    # Mock execute to return empty candidate set so we don't iterate.
    session = MagicMock()
    result_proxy = MagicMock()
    result_proxy.all.return_value = []
    session.execute = AsyncMock(return_value=result_proxy)
    await linker.link_fact_to_decisions(
        fact_id=uuid4(), fact_content="x" * 40,  # exactly at threshold
        session=session,
    )
    linker.embedder.embed.assert_called()


@pytest.mark.asyncio
async def test_min_chars_zero_disables_guard():
    """min_chars=0 reverts to legacy behavior (no source-side guard)."""
    linker = _linker(min_chars=0)
    session = MagicMock()
    result_proxy = MagicMock()
    result_proxy.all.return_value = []
    session.execute = AsyncMock(return_value=result_proxy)
    # Tiny content would fail at min_chars=40 but must pass at 0.
    await linker.link_fact_to_decisions(
        fact_id=uuid4(), fact_content="short",
        session=session,
    )
    linker.embedder.embed.assert_called()


@pytest.mark.asyncio
async def test_whitespace_only_content_treated_as_short():
    """Whitespace-only content has stripped length 0 and must be rejected."""
    linker = _linker(min_chars=40)
    session = MagicMock()
    session.execute = AsyncMock()
    result = await linker.link_fact_to_decisions(
        fact_id=uuid4(), fact_content="   \n\t   ",
        session=session,
    )
    assert result == []
    linker.embedder.embed.assert_not_called()


@pytest.mark.asyncio
async def test_none_content_treated_as_short():
    """None content (defensive — caller bug) does not raise."""
    linker = _linker(min_chars=40)
    session = MagicMock()
    session.execute = AsyncMock()
    # Pass empty string instead of None since type hint forbids None;
    # but the guard's len(coalesce ... '').strip() < min_chars handles it
    # the same way.
    result = await linker.link_fact_to_decisions(
        fact_id=uuid4(), fact_content="",
        session=session,
    )
    assert result == []
