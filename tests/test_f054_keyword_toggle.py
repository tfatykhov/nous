"""F054 — Keyword channel toggle in hybrid_search.

The F051 channel-isolation eval (90 nous_prod + 20 longmemeval qrels)
showed `vector_only` ties default RRF byte-for-byte on longmemeval and
within -0.2% on nous_prod, while `keyword_only` collapses (MRR 0.07/0.35).
The keyword channel adds compute (one FTS query per recall) for ~zero
recall benefit on those workloads. F054 adds an opt-out flag.

This module verifies:

  - flag on (default)            -> both vector + keyword SQL execute.
  - flag off                     -> keyword SQL is NOT executed; vector still runs.
  - flag off + embedding=None    -> keyword still runs (keyword-only fallback).
  - flag off + empty keyword     -> RRF degenerates to vector-only correctly.
  - default Settings has flag on -> backward compat preserved.

Mocks SQLAlchemy session to capture which SQL strings are executed; uses
the canonical `text()` SQL statement strings to identify the keyword
query without depending on full SQL equality.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from nous.config import Settings
from nous.heart.search import hybrid_search


def _mock_session(vector_rows=None, keyword_rows=None) -> AsyncMock:
    """Build a session whose execute() returns vector then keyword rows.

    Routes by SQL string content: a query containing `plainto_tsquery`
    is the keyword query; anything else is the vector query.
    """
    session = AsyncMock()

    async def execute(sql, params):
        sql_str = str(sql)
        result = MagicMock()
        if "plainto_tsquery" in sql_str:
            result.all = MagicMock(return_value=keyword_rows or [])
        else:
            result.all = MagicMock(return_value=vector_rows or [])
        return result

    session.execute = AsyncMock(side_effect=execute)
    return session


class TestF054KeywordToggle:
    @pytest.mark.asyncio
    async def test_default_settings_have_keyword_enabled(self):
        s = Settings(_env_file=None)
        assert s.hybrid_search_keyword_enabled is True

    @pytest.mark.asyncio
    async def test_flag_on_runs_both_channels(self):
        """Default behavior: vector SQL + keyword SQL both execute."""
        v_id, k_id = uuid4(), uuid4()
        session = _mock_session(
            vector_rows=[MagicMock(id=v_id, score=0.9)],
            keyword_rows=[MagicMock(id=k_id, score=0.5)],
        )
        with patch(
            "nous.heart.search._resolve_keyword_enabled", return_value=True
        ):
            await hybrid_search(
                session=session,
                table="heart.facts",
                embedding=[0.1, 0.2, 0.3],
                query_text="anything",
                agent_id="test-agent",
            )
        executed_sqls = [str(c.args[0]) for c in session.execute.await_args_list]
        assert any("plainto_tsquery" in s for s in executed_sqls), (
            "keyword SQL should execute when flag on"
        )
        assert any("<=>" in s for s in executed_sqls), (
            "vector SQL should always execute when embedding is provided"
        )

    @pytest.mark.asyncio
    async def test_flag_off_skips_keyword(self):
        """Vector still runs; keyword SQL is NOT executed."""
        v_id = uuid4()
        session = _mock_session(
            vector_rows=[MagicMock(id=v_id, score=0.9)],
            keyword_rows=[],
        )
        with patch(
            "nous.heart.search._resolve_keyword_enabled", return_value=False
        ):
            results = await hybrid_search(
                session=session,
                table="heart.facts",
                embedding=[0.1, 0.2, 0.3],
                query_text="anything",
                agent_id="test-agent",
            )
        executed_sqls = [str(c.args[0]) for c in session.execute.await_args_list]
        assert not any("plainto_tsquery" in s for s in executed_sqls), (
            "keyword SQL should NOT execute when flag off"
        )
        assert any("<=>" in s for s in executed_sqls), (
            "vector SQL should still execute"
        )
        assert len(results) == 1
        assert results[0][0] == v_id

    @pytest.mark.asyncio
    async def test_flag_off_with_no_embedding_still_runs_keyword(self):
        """Keyword-only fallback path: embedding=None must force keyword on."""
        k_id = uuid4()
        session = _mock_session(
            vector_rows=[],
            keyword_rows=[MagicMock(id=k_id, score=0.5)],
        )
        with patch(
            "nous.heart.search._resolve_keyword_enabled", return_value=False
        ):
            results = await hybrid_search(
                session=session,
                table="heart.facts",
                embedding=None,
                query_text="anything",
                agent_id="test-agent",
            )
        executed_sqls = [str(c.args[0]) for c in session.execute.await_args_list]
        assert any("plainto_tsquery" in s for s in executed_sqls), (
            "keyword SQL must run as fallback when embedding is None"
        )
        assert results == [(k_id, 0.5)]

    def test_resolve_keyword_enabled_falls_back_to_true_on_error(self):
        """Settings construction failure must default the flag to True
        (preserve current behavior). P3 review fix.

        `_resolve_keyword_enabled` does `from nous.config import Settings`
        inside the function body, so we patch at the source module.
        """
        from nous.heart.search import _resolve_keyword_enabled
        with patch("nous.config.Settings", side_effect=Exception("boom")):
            assert _resolve_keyword_enabled() is True

    @pytest.mark.asyncio
    async def test_flag_off_returns_vector_results_via_rrf_merge(self):
        """RRF merge with empty keyword list returns vector-only ranking."""
        v_ids = [uuid4(), uuid4(), uuid4()]
        session = _mock_session(
            vector_rows=[
                MagicMock(id=v_ids[0], score=0.9),
                MagicMock(id=v_ids[1], score=0.8),
                MagicMock(id=v_ids[2], score=0.7),
            ],
            keyword_rows=[],
        )
        with patch(
            "nous.heart.search._resolve_keyword_enabled", return_value=False
        ):
            results = await hybrid_search(
                session=session,
                table="heart.facts",
                embedding=[0.1, 0.2, 0.3],
                query_text="anything",
                agent_id="test-agent",
            )
        # All 3 vector ids present in result; order preserved by RRF rank.
        result_ids = [r[0] for r in results]
        for vid in v_ids:
            assert vid in result_ids
        assert result_ids[0] == v_ids[0]  # rank-0 stays rank-0
