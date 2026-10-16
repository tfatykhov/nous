"""A4 Step 1: the keyword counts reach the retrieval_log legs on every path.

The sink is a ContextVar installed around `heart.recall`, the chunk helper and
the context engine's fact / episode searches, so no manager signature carries it.
Nothing here may change a served byte.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

import nous.heart.search as search_mod
from nous.api.retrieval_pipeline import _search_episode_chunks, run_recall_pipeline
from nous.cognitive.context import ContextEngine
from nous.cognitive.schemas import FrameSelection
from nous.config import Settings
from nous.heart.search import with_keyword_stats
from nous.observability.retrieval_logger import RetrievalLogger
from nous.observability.retrieval_trace import NULL_TRACE, RetrievalTrace

ZERO = {"n_keyword": 0, "n_keyword_fallback": None}


def _settings(**over):
    base = dict(
        graph_recall_enabled=False, cross_type_linking_enabled=False,
        spreading_activation_enabled="false", contradiction_detection=False,
        graph_recall_decay=0.7, graph_recall_max_expand=5,
        graph_recall_max_neighbors=3, heart_graph_all_types_enabled=False,
        heart_graph_neighbors_per_seed=3, episode_chunks_enabled=False,
        episode_chunk_recall_limit=5, coherent_ranking_enabled=False,
        chunk_hybrid_search_enabled=True, chunk_rrf_penalty_limit=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _brain():
    brain = MagicMock()
    brain.neighbors = AsyncMock(return_value=[])
    brain.query = AsyncMock(return_value=[])
    brain._query = AsyncMock(return_value=[])
    brain.get_contradictions = AsyncMock(return_value=[])
    return brain


def _leg(trace, name):
    return next(lg for lg in trace.to_dict()["legs"] if lg["name"] == name)


class TestHeartPrimary:
    @pytest.mark.asyncio
    async def test_leg_carries_the_counts(self):
        async def recall(*a, **kw):
            sink = search_mod._KEYWORD_STATS_SINK.get()
            sink.extend([dict(ZERO), dict(ZERO), {"n_keyword": 4, "n_keyword_fallback": None}])
            return []

        heart = MagicMock()
        heart.recall = recall
        heart.agent_id = "a"
        tr = RetrievalTrace(capture_candidates=False)
        await run_recall_pipeline(query="q", heart=heart, brain=_brain(),
                                  settings=_settings(), limit=5,
                                  memory_types=["all"], trace=tr)
        leg = _leg(tr, "heart_primary")
        assert (leg["n_keyword_queries"], leg["n_keyword_zero"]) == (3, 2)
        assert leg["attempted"] is True

    @pytest.mark.asyncio
    async def test_telemetry_off_installs_no_sink(self):
        seen = {}

        async def recall(*a, **kw):
            seen["sink"] = search_mod._KEYWORD_STATS_SINK.get()
            return []

        heart = MagicMock()
        heart.recall = recall
        heart.agent_id = "a"
        await run_recall_pipeline(query="q", heart=heart, brain=_brain(),
                                  settings=_settings(), limit=5,
                                  memory_types=["all"], trace=NULL_TRACE)
        assert seen["sink"] is None

    @pytest.mark.asyncio
    async def test_a_recall_that_ran_no_keyword_query_adds_no_keys(self):
        heart = MagicMock()
        heart.recall = AsyncMock(return_value=[])
        heart.agent_id = "a"
        tr = RetrievalTrace(capture_candidates=False)
        await run_recall_pipeline(query="q", heart=heart, brain=_brain(),
                                  settings=_settings(), limit=5,
                                  memory_types=["all"], trace=tr)
        assert "n_keyword_queries" not in _leg(tr, "heart_primary")

    @pytest.mark.asyncio
    async def test_served_results_are_identical_with_and_without_a_trace(self):
        from nous.heart.heart import RecallResult

        results = [RecallResult(type="fact", id=UUID(int=i + 1), summary=f"s{i}",
                                score=0.9 - 0.1 * i, metadata={}) for i in range(3)]

        async def recall(*a, **kw):
            sink = search_mod._KEYWORD_STATS_SINK.get()
            if sink is not None:
                sink.append(dict(ZERO))
            return list(results)

        async def go(trace):
            heart = MagicMock()
            heart.recall = recall
            heart.agent_id = "a"
            out, _ = await run_recall_pipeline(
                query="q", heart=heart, brain=_brain(), settings=_settings(),
                limit=5, memory_types=["all"], trace=trace)
            return [(r.id, r.score) for r in out]

        assert await go(RetrievalTrace(capture_candidates=False)) == await go(NULL_TRACE)


class _SplitSess:
    def __init__(self, vector, keyword):
        self.vector, self.keyword = vector, keyword

    async def execute(self, sql, params=None):
        text = str(sql)
        if "ts_rank_cd" in text:
            rows = [SimpleNamespace(id=i, score=s) for i, s in self.keyword]
        elif "embedding <=>" in text and "SELECT id, content" not in text:
            rows = [SimpleNamespace(id=i, score=s) for i, s in self.vector]
        else:
            ids = params["ids"] if params else []
            rows = [(cid, f"body-{cid.int}", UUID(int=999)) for cid in ids]
        return MagicMock(all=lambda: rows)


def _chunk_heart(sess):
    db = MagicMock()

    @contextlib.asynccontextmanager
    async def _ctx():
        yield sess

    db.session = _ctx
    emb = MagicMock()
    emb.embed = AsyncMock(return_value=[0.1] * 4)
    return SimpleNamespace(db=db, _embeddings=emb, agent_id="a")


class TestChunkLeg:
    @pytest.mark.asyncio
    async def test_the_helper_records_and_serves_the_same_rows(self):
        ids = [UUID(int=i + 1) for i in range(20)]
        vector = [(i, 0.9 - 0.01 * n) for n, i in enumerate(ids)]
        keyword = [(i, 0.5) for i in ids[:4]]

        async def go(sink):
            return await with_keyword_stats(sink, _search_episode_chunks(
                heart=_chunk_heart(_SplitSess(vector, keyword)), query="q",
                agent_id="a", limit=5, settings=_settings()))

        sink: list = []
        assert await go(sink) == await go(None)
        assert sink == [{"n_keyword": 4, "n_keyword_fallback": None}]

    @pytest.mark.asyncio
    async def test_vector_only_branch_appends_nothing(self):
        ids = [UUID(int=i + 1) for i in range(5)]
        sess = MagicMock()
        sess.execute = AsyncMock(return_value=MagicMock(
            all=lambda: [(i, "c", 0.9, UUID(int=9)) for i in ids]))
        sink: list = []
        await with_keyword_stats(sink, _search_episode_chunks(
            heart=_chunk_heart(sess), query="q", agent_id="a", limit=5,
            settings=_settings(chunk_hybrid_search_enabled=False)))
        assert sink == []

    @pytest.mark.asyncio
    async def test_pipeline_puts_the_counts_on_the_chunk_leg(self, monkeypatch):
        async def fake(**kw):
            search_mod._KEYWORD_STATS_SINK.get().append(dict(ZERO))
            return []

        monkeypatch.setattr("nous.api.retrieval_pipeline._search_episode_chunks", fake)
        heart = MagicMock()
        heart.recall = AsyncMock(return_value=[])
        heart.agent_id = "a"
        tr = RetrievalTrace(capture_candidates=False)
        await run_recall_pipeline(
            query="q", heart=heart, brain=_brain(),
            settings=_settings(episode_chunks_enabled=True), limit=5,
            memory_types=["all"], trace=tr)
        leg = _leg(tr, "chunk")
        assert (leg["n_keyword_queries"], leg["n_keyword_zero"]) == (1, 1)

    @pytest.mark.asyncio
    async def test_a_failed_chunk_fetch_reports_no_counts(self, monkeypatch):
        """Same rule as chunk_dropped: counts from a retrieval that then died
        must not be reported as a healthy zero-hit leg."""
        async def fake(**kw):
            search_mod._KEYWORD_STATS_SINK.get().append(dict(ZERO))
            raise RuntimeError("content fetch exploded")

        monkeypatch.setattr("nous.api.retrieval_pipeline._search_episode_chunks", fake)
        heart = MagicMock()
        heart.recall = AsyncMock(return_value=[])
        heart.agent_id = "a"
        tr = RetrievalTrace(capture_candidates=False)
        await run_recall_pipeline(
            query="q", heart=heart, brain=_brain(),
            settings=_settings(episode_chunks_enabled=True), limit=5,
            memory_types=["all"], trace=tr)
        legs = [lg for lg in tr.to_dict()["legs"] if lg["name"] == "chunk"]
        assert all("n_keyword_queries" not in lg for lg in legs)


class TestContextPath:
    """The per-turn context build searches facts and episodes with
    '{current_topic}: {input}' -- the long-query population."""

    @staticmethod
    def _engine(heart):
        return ContextEngine(brain=AsyncMock(), heart=heart, settings=Settings(_env_file=None))

    @staticmethod
    def _heart(facts_records, episode_records):
        heart = AsyncMock()
        heart.get_working_memory = AsyncMock(return_value=None)

        async def search_facts(*a, **kw):
            search_mod._KEYWORD_STATS_SINK.get().extend(facts_records)
            return []

        async def search_episodes(*a, **kw):
            search_mod._KEYWORD_STATS_SINK.get().extend(episode_records)
            return []

        heart.search_facts = search_facts
        heart.search_episodes = search_episodes
        return heart

    @staticmethod
    async def _build(engine, logger):
        frame = FrameSelection(frame_id="task", frame_name="Task", confidence=0.9,
                               match_method="test")
        with patch("nous.cognitive.context.get_active_retrieval_logger", return_value=logger):
            return await engine.build(agent_id="a", session_id="s1",
                                      input_text="do a task now", frame=frame,
                                      session=None)

    @pytest.mark.asyncio
    async def test_context_facts_and_episodes_legs_carry_the_counts(self):
        heart = self._heart([dict(ZERO), dict(ZERO)], [{"n_keyword": 2, "n_keyword_fallback": None}])
        result = await self._build(self._engine(heart), RetrievalLogger(candidate_sample_rate=1.0))
        facts = _leg(result.retrieval_trace, "context_facts")
        eps = _leg(result.retrieval_trace, "context_episodes")
        assert (facts["n_keyword_queries"], facts["n_keyword_zero"]) == (2, 2)
        assert (eps["n_keyword_queries"], eps["n_keyword_zero"]) == (1, 0)

    @pytest.mark.asyncio
    async def test_without_telemetry_no_sink_is_installed(self):
        seen = []
        heart = self._heart([], [])

        async def search_facts(*a, **kw):
            seen.append(search_mod._KEYWORD_STATS_SINK.get())
            return []

        heart.search_facts = search_facts
        await self._build(self._engine(heart), None)
        assert seen == [None]
