"""A4 Step 2: which call sites opt in to the any-word fallback."""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, patch

import pytest

from nous.heart.episodes import EpisodeManager
from nous.heart.facts import FactManager
from nous.heart.procedures import ProcedureManager


def _manager(cls):
    mgr = cls.__new__(cls)
    mgr.embeddings = None
    mgr.agent_id = "nous-test"
    mgr._settings = None
    return mgr


@pytest.mark.asyncio
@pytest.mark.parametrize("variants", [None, [("a", None), ("b", None)]], ids=["single", "multi"])
async def test_the_three_managers_opt_in(variants):
    target = "hybrid_search" if variants is None else "hybrid_search_multi"
    cases = [
        ("episodes", EpisodeManager, lambda m: m._search("q", 5, AsyncMock(), variants, None)),
        ("procedures", ProcedureManager,
         lambda m: m._search("q", 5, None, None, AsyncMock(), variants, None)),
        ("facts", FactManager,
         lambda m: m._search("q", 5, None, True, None, AsyncMock(), variants)),
    ]
    for mod, cls, call in cases:
        with patch(f"nous.heart.{mod}.{target}", new=AsyncMock(return_value=[])) as m:
            await call(_manager(cls))
        assert m.call_args.kwargs["any_word_fallback"] is True, mod


@pytest.mark.asyncio
async def test_the_chunk_leg_opts_in():
    import contextlib
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from nous.api.retrieval_pipeline import _search_episode_chunks

    db = MagicMock()

    @contextlib.asynccontextmanager
    async def _ctx():
        yield AsyncMock()

    db.session = _ctx
    emb = MagicMock()
    emb.embed = AsyncMock(return_value=[0.1] * 4)
    heart = SimpleNamespace(db=db, _embeddings=emb, agent_id="a")
    with patch("nous.heart.search.hybrid_search", new=AsyncMock(return_value=[])) as m:
        await _search_episode_chunks(
            heart=heart, query="q", agent_id="a", limit=5,
            settings=SimpleNamespace(chunk_hybrid_search_enabled=True, chunk_rrf_penalty_limit=None))
    assert m.call_args.kwargs["any_word_fallback"] is True


def test_the_graph_densifier_does_not_opt_in():
    import nous.brain.graph_densifier as d

    assert "any_word_fallback" not in inspect.getsource(d)
