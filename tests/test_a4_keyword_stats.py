"""A4 Step 1: the keyword sub-leg's row counts reach a caller's sink.

Telemetry only. Every ranking assertion compares the SAME call with and without
the sink, because "the sink did not move a ranking" is the one property that must
never go red. The session is a fake returning canned rows per statement, so the
REAL `_rrf_merge` and the real `hybrid_search` body run.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest

import nous.heart.search as search_mod
from nous.heart.search import (
    hybrid_search,
    hybrid_search_multi,
    summarize_keyword_stats,
    with_keyword_stats,
)


def _ids(n: int, base: int = 1) -> list[UUID]:
    return [UUID(int=base + i) for i in range(n)]


class _Sess:
    """Returns canned rows by statement kind and remembers what ran."""

    def __init__(self, vector, keyword):
        self.vector = vector
        self.keyword = keyword
        self.sqls: list[str] = []

    async def execute(self, sql, params=None):
        text = str(sql)
        self.sqls.append(text)
        if "ts_rank_cd" in text:
            rows = self.keyword
        elif "<=>" in text:
            rows = self.vector
        else:
            rows = []
        return MagicMock(all=lambda: [SimpleNamespace(id=i, score=s) for i, s in rows])


def _fixture(n_vector: int = 20, n_keyword: int = 6):
    ids = _ids(n_vector)
    vector = [(i, 0.9 - 0.01 * n) for n, i in enumerate(ids)]
    keyword = [(i, 0.5 - 0.01 * n) for n, i in enumerate(reversed(ids[:n_keyword]))]
    return vector, keyword


async def _run(sink, *, vector, keyword, embedding=(0.1, 0.2), **kw):
    sess = _Sess(vector, keyword)
    emb = list(embedding) if embedding is not None else None
    out = await with_keyword_stats(
        sink, hybrid_search(sess, "heart.facts", emb, "alpha beta", "agent", **kw))
    return out, sess


class TestRecordShape:
    @pytest.mark.asyncio
    async def test_one_record_with_the_all_words_count(self):
        vector, keyword = _fixture(n_keyword=6)
        sink: list = []
        await _run(sink, vector=vector, keyword=keyword, limit=5)
        assert sink == [{"n_keyword": 6, "n_keyword_fallback": None}]

    @pytest.mark.asyncio
    async def test_zero_keyword_rows_is_zero_not_none(self):
        """The whole point: ran-and-found-nothing is not did-not-run."""
        vector, _ = _fixture()
        sink: list = []
        await _run(sink, vector=vector, keyword=[], limit=5)
        assert sink[0]["n_keyword"] == 0

    @pytest.mark.asyncio
    async def test_keyword_leg_disabled_records_none(self):
        vector, keyword = _fixture()
        sink: list = []
        with patch("nous.heart.search._resolve_keyword_enabled", return_value=False):
            _, sess = await _run(sink, vector=vector, keyword=keyword, limit=5)
        assert not any("ts_rank_cd" in s for s in sess.sqls)
        assert sink == [{"n_keyword": None, "n_keyword_fallback": None}]

    @pytest.mark.asyncio
    async def test_keyword_only_exit_still_records(self):
        _, keyword = _fixture()
        sink: list = []
        await _run(sink, vector=[], keyword=keyword, embedding=None, limit=5)
        assert sink[0]["n_keyword"] == 6

    @pytest.mark.asyncio
    async def test_no_scope_means_nothing_is_recorded_and_nothing_breaks(self):
        vector, keyword = _fixture()
        out, _ = await _run(None, vector=vector, keyword=keyword, limit=5)
        assert out


_CASES = [
    {"limit": 5},
    {"limit": 5, "penalty_limit": 20},
    {"limit": 5, "require_keyword_hit": True},
    {"limit": 5, "embedding": None},
    {"limit": 50},
]


class TestRankingUnchanged:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("kw", _CASES, ids=lambda k: ",".join(sorted(k)))
    async def test_identical_with_and_without_the_sink(self, kw):
        vector, keyword = _fixture()
        without, _ = await _run(None, vector=vector, keyword=keyword, **dict(kw))
        with_sink, _ = await _run([], vector=vector, keyword=keyword, **dict(kw))
        assert with_sink == without
        assert with_sink, "fixture must produce rows or the equality is vacuous"

    @pytest.mark.asyncio
    async def test_the_drop_sink_is_unaffected_by_the_stats_sink(self):
        vector, keyword = _fixture()
        d1: list = []
        d2: list = []
        await _run(None, vector=vector, keyword=keyword, limit=5, dropped_out=d1)
        await _run([], vector=vector, keyword=keyword, limit=5, dropped_out=d2)
        assert d1 == d2 and d1


class TestScope:
    @pytest.mark.asyncio
    async def test_the_previous_sink_is_restored_and_scopes_nest(self):
        outer: list = []
        inner: list = []

        async def probe():
            return search_mod._KEYWORD_STATS_SINK.get()

        assert search_mod._KEYWORD_STATS_SINK.get() is None
        assert await with_keyword_stats(outer, probe()) is outer
        assert search_mod._KEYWORD_STATS_SINK.get() is None

        async def nested():
            assert await with_keyword_stats(inner, probe()) is inner
            return await probe()

        assert await with_keyword_stats(outer, nested()) is outer

    @pytest.mark.asyncio
    async def test_an_exception_still_restores_it(self):
        async def boom():
            raise RuntimeError("x")

        with pytest.raises(RuntimeError):
            await with_keyword_stats([], boom())
        assert search_mod._KEYWORD_STATS_SINK.get() is None

    @pytest.mark.asyncio
    async def test_none_installs_nothing(self):
        async def probe():
            return search_mod._KEYWORD_STATS_SINK.get()

        assert await with_keyword_stats(None, probe()) is None


class TestMulti:
    @pytest.mark.asyncio
    async def test_one_record_per_variant_and_same_ranking(self):
        vector, keyword = _fixture()
        queries = [("q one", [0.1, 0.2]), ("q two", [0.2, 0.1]), ("q three", [0.3, 0.3])]

        async def go(sink):
            return await with_keyword_stats(sink, hybrid_search_multi(
                _Sess(vector, keyword), "heart.facts", queries, "agent", limit=5))

        sink: list = []
        assert await go(sink) == await go(None)
        assert len(sink) == 3
        assert all(r["n_keyword"] == 6 for r in sink)

    @pytest.mark.asyncio
    async def test_single_variant_fast_path_records_once(self):
        vector, keyword = _fixture()
        sink: list = []
        await with_keyword_stats(sink, hybrid_search_multi(
            _Sess(vector, keyword), "heart.facts", [("only", [0.1, 0.2])], "agent", limit=5))
        assert len(sink) == 1


class TestSummarize:
    def test_empty_is_none(self):
        assert summarize_keyword_stats([]) is None

    def test_counts_queries_and_zeros(self):
        recs = [{"n_keyword": 0, "n_keyword_fallback": None},
                {"n_keyword": 3, "n_keyword_fallback": None},
                {"n_keyword": 0, "n_keyword_fallback": None}]
        assert summarize_keyword_stats(recs) == {"n_keyword_queries": 3, "n_keyword_zero": 2}

    def test_disabled_keyword_leg_is_not_a_query(self):
        recs = [{"n_keyword": None, "n_keyword_fallback": None}]
        assert summarize_keyword_stats(recs) is None

    def test_fallback_rows_are_summed_separately(self):
        recs = [{"n_keyword": 0, "n_keyword_fallback": 4},
                {"n_keyword": 0, "n_keyword_fallback": 0}]
        assert summarize_keyword_stats(recs) == {
            "n_keyword_queries": 2, "n_keyword_zero": 2, "n_keyword_fallback": 4}
