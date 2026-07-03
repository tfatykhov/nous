"""R2 (2026-07-02 MAB audit): hybrid chunk retrieval — unit tests.

The F067 chunk leg is vector-only (`_search_episode_chunks`): a chunk
literally containing a rare gold token gets no lexical boost, and 4/5 CR
gold chunks ranked 16-50 by cosine (recall limit 10). Migration 050 already
provisioned `search_tsv` + GIN on heart.episode_chunks "for keyword
fallback / hybrid search" — with zero consumers until now.

Flag `NOUS_CHUNK_HYBRID_SEARCH_ENABLED` (default OFF, land-dark): when on,
the chunk leg RRF-fuses the vector and FTS legs via the shared
`heart.search.hybrid_search` helper — which also moves chunk scores from
raw cosine onto the same 1/k-normalized RRF [0,1] scale the coherent heart
legs use (F080: only types sharing the SAME normalizer are comparable).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import contextlib

import pytest

from nous.api.retrieval_pipeline import _search_episode_chunks


def _heart_shim(embedder, rows_by_query=None):
    """Minimal heart stand-in: .db.session() ctx + ._embeddings + .agent_id."""
    session = AsyncMock()
    if rows_by_query is not None:
        async def _execute(sql, params=None):
            result = MagicMock()
            result.all = MagicMock(return_value=rows_by_query(str(sql), params))
            return result
        session.execute = AsyncMock(side_effect=_execute)

    db = MagicMock()

    @contextlib.asynccontextmanager
    async def _session_ctx():
        yield session

    db.session = _session_ctx
    return SimpleNamespace(db=db, _embeddings=embedder, agent_id="test-agent"), session


def _fake_embedder(vec=None):
    e = MagicMock()
    e.embed = AsyncMock(return_value=vec if vec is not None else [0.1] * 4)
    return e


def test_flag_defaults_off():
    from nous.config import Settings

    assert Settings.model_fields["chunk_hybrid_search_enabled"].default is False


class TestFlagOff:
    @pytest.mark.asyncio
    async def test_vector_only_sql_unchanged(self):
        """Flag OFF (or settings omitted) keeps the legacy vector-only query."""
        captured = []

        def rows(sql, params):
            captured.append(sql)
            return []

        heart, _ = _heart_shim(_fake_embedder(), rows)
        out = await _search_episode_chunks(
            heart=heart, query="q", agent_id="test-agent", limit=10,
            settings=SimpleNamespace(chunk_hybrid_search_enabled=False),
        )
        assert out == []
        assert len(captured) == 1
        assert "ORDER BY embedding" in captured[0]
        assert "ts_rank" not in captured[0]

    @pytest.mark.asyncio
    async def test_no_embedder_returns_empty(self):
        heart, _ = _heart_shim(None)
        out = await _search_episode_chunks(
            heart=heart, query="q", agent_id="test-agent", limit=10,
            settings=SimpleNamespace(chunk_hybrid_search_enabled=False),
        )
        assert out == []


class TestFlagOn:
    @pytest.mark.asyncio
    async def test_hybrid_search_called_and_rank_order_preserved(self, monkeypatch):
        id_a, id_b = uuid4(), uuid4()
        ep_a, ep_b = uuid4(), uuid4()
        calls = {}

        async def fake_hybrid(session, table, embedding, query_text, agent_id,
                              limit=10, active_filter=True, **kw):
            calls.update(table=table, embedding=embedding, query_text=query_text,
                         agent_id=agent_id, limit=limit, active_filter=active_filter)
            return [(id_b, 0.9), (id_a, 0.4)]  # b outranks a

        monkeypatch.setattr("nous.heart.search.hybrid_search", fake_hybrid)

        def rows(sql, params):
            # content fetch — return in arbitrary (non-rank) order
            return [(id_a, "content-a", ep_a), (id_b, "content-b", ep_b)]

        heart, _ = _heart_shim(_fake_embedder([0.5] * 4), rows)
        out = await _search_episode_chunks(
            heart=heart, query="rare token query", agent_id="test-agent", limit=7,
            settings=SimpleNamespace(chunk_hybrid_search_enabled=True),
        )
        assert calls["table"] == "heart.episode_chunks"
        assert calls["active_filter"] is False
        assert calls["agent_id"] == "test-agent"
        assert calls["limit"] == 7
        assert calls["embedding"] == [0.5] * 4
        # 4-tuples in hybrid rank order, RRF scores passed through
        assert out == [
            (id_b, "content-b", 0.9, ep_b),
            (id_a, "content-a", 0.4, ep_a),
        ]

    @pytest.mark.asyncio
    async def test_no_embedder_falls_back_to_keyword_only(self, monkeypatch):
        """Flag ON without an embedder passes embedding=None → FTS-only leg
        (vector-only path would return [])."""
        cid, eid = uuid4(), uuid4()
        seen = {}

        async def fake_hybrid(session, table, embedding, query_text, agent_id,
                              limit=10, active_filter=True, **kw):
            seen["embedding"] = embedding
            return [(cid, 0.7)]

        monkeypatch.setattr("nous.heart.search.hybrid_search", fake_hybrid)

        heart, _ = _heart_shim(None, lambda sql, params: [(cid, "kw-content", eid)])
        out = await _search_episode_chunks(
            heart=heart, query="rare gold token", agent_id="test-agent", limit=10,
            settings=SimpleNamespace(chunk_hybrid_search_enabled=True),
        )
        assert seen["embedding"] is None
        assert out == [(cid, "kw-content", 0.7, eid)]

    @pytest.mark.asyncio
    async def test_empty_hybrid_result_returns_empty(self, monkeypatch):
        async def fake_hybrid(*a, **kw):
            return []

        monkeypatch.setattr("nous.heart.search.hybrid_search", fake_hybrid)
        heart, session = _heart_shim(_fake_embedder())
        out = await _search_episode_chunks(
            heart=heart, query="q", agent_id="test-agent", limit=10,
            settings=SimpleNamespace(chunk_hybrid_search_enabled=True),
        )
        assert out == []
