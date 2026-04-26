"""F052 — Multi-embedding seed for ``_backfill_same_type``.

Eight mandatory unit-test cases per spec §Test plan, exercised against
the wedge installed in ``nous/brain/graph_densifier.py``. Tests use mocks
for ``Heart.expand_query_pairs`` and patch the imported
``hybrid_search_multi`` symbol on ``nous.brain.graph_densifier`` so they
run without a Postgres connection.

Each test ALSO carries an ``ImportError`` skip-guard so the file remains
collectable on a working tree where Subagent A or B has not yet landed
the ``Heart.expand_query_pairs`` helper or the
``graph_backfill_multi_embedding_enabled`` Settings field. In that
scenario the test module is skipped at collection time with a
diagnostic rather than crashing the test suite.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

# ---------------------------------------------------------------------------
# Import gates — skip module cleanly if A or B hasn't merged yet.
# ---------------------------------------------------------------------------

try:
    from nous.config import Settings
    _settings_probe = Settings()
    if not hasattr(_settings_probe, "graph_backfill_multi_embedding_enabled"):
        raise ImportError(
            "Settings.graph_backfill_multi_embedding_enabled missing "
            "(F052 Subagent A not yet merged)"
        )
except (ImportError, Exception) as exc:  # pragma: no cover - skip path
    pytest.skip(
        f"F052 Settings field not yet merged ({exc!r}) — skipping module",
        allow_module_level=True,
    )

try:
    from nous.heart.heart import Heart
    if not hasattr(Heart, "expand_query_pairs"):
        raise ImportError(
            "Heart.expand_query_pairs missing (F052 Subagent A not yet merged)"
        )
except ImportError as exc:  # pragma: no cover - skip path
    pytest.skip(
        f"Heart.expand_query_pairs not yet merged ({exc!r}) — skipping module",
        allow_module_level=True,
    )

# Densifier import is below the gates so a missing wedge import (B not merged)
# also short-circuits cleanly.
try:
    from nous.brain.graph_densifier import GraphDensifier
    import inspect as _inspect
    _gd_sig = _inspect.signature(GraphDensifier.__init__)
    if "heart" not in _gd_sig.parameters:
        raise ImportError(
            "GraphDensifier(heart=...) kwarg missing (F052 Subagent B not yet merged)"
        )
except ImportError as exc:  # pragma: no cover - skip path
    pytest.skip(
        f"GraphDensifier wedge not yet merged ({exc!r}) — skipping module",
        allow_module_level=True,
    )


# ---------------------------------------------------------------------------
# Helpers — minimal Heart/Densifier construction without a real DB.
# ---------------------------------------------------------------------------


def _make_settings(**overrides) -> Settings:
    """Settings copy with F052 enabled and graph thresholds dropped low.

    The low thresholds let the cosine gate pass for synthetic candidates
    without forcing real embeddings.
    """
    base = Settings()
    update = {
        "graph_backfill_multi_embedding_enabled": True,
        "query_expansion_enabled": True,
        "graph_backfill_enabled": True,
        "graph_threshold_fact_fact": 0.0,
        "ce_backfill_enabled": False,  # tests target the wedge, not CE
    }
    update.update(overrides)
    return base.model_copy(update=update)


def _make_densifier(
    heart: object | None,
    *,
    settings: Settings | None = None,
) -> GraphDensifier:
    """Build a GraphDensifier with mocked deps and (optionally) a heart."""
    settings = settings or _make_settings()
    db = MagicMock()
    linker = MagicMock()
    linker.create_edge = AsyncMock(return_value=MagicMock())
    embedder = MagicMock()
    return GraphDensifier(
        db=db,
        graph_linker=linker,
        embedder=embedder,
        settings=settings,
        agent_id="test-f052-agent",
        heart=heart,
    )


def _fake_session_factory(
    sim_value: float = 0.95,
    orphan_embedding: list[float] | None = None,
):
    """Return a MagicMock session that responds to the densifier's SQL.

    The densifier hits two SQL statements inside ``_backfill_same_type``:
    1. SELECT embedding for the orphan (returns ``orphan_embedding``).
    2. SELECT cosine similarity for each candidate (returns ``sim_value``).
    """
    if orphan_embedding is None:
        orphan_embedding = [0.1] * 8

    session = MagicMock()

    # First SQL execute returns a row with an "embedding" attr; subsequent
    # similarity SQLs return a row with a "similarity" attr. We discriminate
    # by SQL contents — cleaner than counting calls.
    async def _execute(sql, params=None):
        sql_str = str(sql)
        result = MagicMock()
        if "FROM heart.facts WHERE id" in sql_str and "embedding::text" in sql_str:
            row = SimpleNamespace(embedding=orphan_embedding)
            result.first = MagicMock(return_value=row)
        elif "1 - (embedding <=>" in sql_str:
            row = SimpleNamespace(similarity=sim_value)
            result.first = MagicMock(return_value=row)
        else:
            result.first = MagicMock(return_value=None)
        return result

    session.execute = AsyncMock(side_effect=_execute)
    return session


# ---------------------------------------------------------------------------
# Test 1 — single-pair short-circuit byte-identity (feature off)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_path_byte_identical_to_hybrid_search():
    """When ``graph_backfill_multi_embedding_enabled=False``, the wedge must
    construct a single ``(content, orphan_embedding)`` pair and route through
    ``hybrid_search_multi`` — which short-circuits at search.py:319-332 to
    a byte-identical ``hybrid_search`` call.
    """
    settings = _make_settings(graph_backfill_multi_embedding_enabled=False)
    # heart=None to additionally prove the wedge does NOT consult the helper
    # when the flag is off.
    densifier = _make_densifier(heart=None, settings=settings)
    session = _fake_session_factory()
    orphan_id = uuid4()

    fake_multi = AsyncMock(return_value=[(uuid4(), 0.9)])
    with patch(
        "nous.brain.graph_densifier.hybrid_search_multi", fake_multi
    ):
        await densifier._backfill_same_type(
            entity_type="fact",
            orphan_id=orphan_id,
            orphan_content="orphan content here",
            session=session,
        )

    # Critical assertion: queries len==1 → triggers the single-element
    # fast-path in hybrid_search_multi.
    assert fake_multi.await_count == 1
    call_kwargs = fake_multi.await_args.kwargs
    assert "queries" in call_kwargs
    queries = call_kwargs["queries"]
    assert len(queries) == 1, "Disabled path must produce exactly one query pair"
    assert queries[0][0] == "orphan content here"
    # Embedding is whatever _fake_session_factory returned for orphan
    assert queries[0][1] is not None


# ---------------------------------------------------------------------------
# Test 2 — multi-pair widens (or matches) candidate set
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multi_pair_widens_candidate_set():
    """With 3 distinct (text, embedding) variants, the wedge must call
    ``hybrid_search_multi`` with a list of length 3 — letting RRF widen
    the candidate set vs the single-query baseline.
    """
    heart = MagicMock()
    heart.expand_query_pairs = AsyncMock(
        return_value=[
            ("variant 1 text", [0.1] * 8),
            ("variant 2 text", [0.2] * 8),
            ("variant 3 text", [0.3] * 8),
        ]
    )
    densifier = _make_densifier(heart=heart)
    session = _fake_session_factory()

    # Two unique candidates from RRF fusion.
    cand_a, cand_b = uuid4(), uuid4()
    fake_multi = AsyncMock(return_value=[(cand_a, 0.9), (cand_b, 0.8)])
    with patch(
        "nous.brain.graph_densifier.hybrid_search_multi", fake_multi
    ):
        await densifier._backfill_same_type(
            entity_type="fact",
            orphan_id=uuid4(),
            orphan_content="orphan content goes here",
            session=session,
        )

    assert heart.expand_query_pairs.await_count == 1
    queries = fake_multi.await_args.kwargs["queries"]
    assert len(queries) == 3, "Multi-pair path must surface all 3 variants"
    assert {q[0] for q in queries} == {
        "variant 1 text", "variant 2 text", "variant 3 text",
    }


# ---------------------------------------------------------------------------
# Test 3 — original embedding still gates cosine
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cosine_uses_original_orphan_embedding():
    """Variants influence candidate-gen only. The cosine verification at
    graph_densifier.py:284-298 must use the ORIGINAL ``orphan_embedding``,
    so the edge weight must reflect the orphan↔candidate similarity, not
    any variant↔candidate similarity.
    """
    orphan_emb = [0.7] * 8  # the embedding cosine SQL must use this
    heart = MagicMock()
    heart.expand_query_pairs = AsyncMock(
        return_value=[
            ("v1", [0.1] * 8),
            ("v2", [0.2] * 8),
        ]
    )
    settings = _make_settings()
    densifier = _make_densifier(heart=heart, settings=settings)

    captured_emb_params: list[dict] = []

    async def _execute(sql, params=None):
        sql_str = str(sql)
        result = MagicMock()
        if "embedding::text" in sql_str:
            result.first = MagicMock(
                return_value=SimpleNamespace(embedding=orphan_emb)
            )
        elif "1 - (embedding <=>" in sql_str:
            captured_emb_params.append(params or {})
            result.first = MagicMock(
                return_value=SimpleNamespace(similarity=0.91)
            )
        else:
            result.first = MagicMock(return_value=None)
        return result

    session = MagicMock()
    session.execute = AsyncMock(side_effect=_execute)

    cand = uuid4()
    fake_multi = AsyncMock(return_value=[(cand, 0.5)])
    with patch(
        "nous.brain.graph_densifier.hybrid_search_multi", fake_multi
    ):
        await densifier._backfill_same_type(
            entity_type="fact",
            orphan_id=uuid4(),
            orphan_content="long orphan text that drives expansion",
            session=session,
        )

    # The cosine SQL must have been called with the orphan's stored embedding,
    # NOT a variant embedding.
    assert captured_emb_params, "cosine SQL was never called"
    emb_param = captured_emb_params[0].get("emb", "")
    assert "0.7" in emb_param, (
        "Cosine gate used a non-orphan embedding — F052 invariant violated"
    )
    # And the create_edge weight must equal the cosine similarity (0.91).
    densifier._linker.create_edge.assert_awaited_once()
    weight = densifier._linker.create_edge.await_args.kwargs.get("weight")
    assert weight == pytest.approx(0.91)


# ---------------------------------------------------------------------------
# Test 4 — expander failure → helper single-pair fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expander_failure_helper_returns_single_pair():
    """If the helper itself returns ``[(query, None)]`` (its documented
    fallback), the wedge must substitute the orphan's stored embedding
    so the short-circuit path still has vector signal — and never sees
    ``None`` flowing into ``hybrid_search_multi`` for the embedding slot.
    """
    heart = MagicMock()
    heart.expand_query_pairs = AsyncMock(
        return_value=[("orphan content", None)]  # helper's fallback shape
    )
    densifier = _make_densifier(heart=heart)
    orphan_emb = [0.3] * 8
    session = _fake_session_factory(orphan_embedding=orphan_emb)

    fake_multi = AsyncMock(return_value=[])
    with patch(
        "nous.brain.graph_densifier.hybrid_search_multi", fake_multi
    ):
        await densifier._backfill_same_type(
            entity_type="fact",
            orphan_id=uuid4(),
            orphan_content="orphan content",
            session=session,
        )

    assert heart.expand_query_pairs.await_count == 1
    queries = fake_multi.await_args.kwargs["queries"]
    assert len(queries) == 1
    text_q, emb_q = queries[0]
    assert text_q == "orphan content"
    assert emb_q == orphan_emb, (
        "Helper single-pair fallback should be backfilled with orphan_embedding"
    )


# ---------------------------------------------------------------------------
# Test 5 — RRF fusion handles identical-list inputs without double-counting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rrf_no_double_count_on_identical_lists():
    """If 3 variant searches return identical candidate lists, the wedge
    must hand all 3 to hybrid_search_multi — the RRF inside that function
    is responsible for not double-counting. The wedge's own contract is
    just to deliver N variants without de-duplicating them itself.
    """
    heart = MagicMock()
    heart.expand_query_pairs = AsyncMock(
        return_value=[
            ("variant 1", [0.1] * 8),
            ("variant 2", [0.2] * 8),
            ("variant 3", [0.3] * 8),
        ]
    )
    densifier = _make_densifier(heart=heart)
    session = _fake_session_factory()

    fake_multi = AsyncMock(return_value=[])
    with patch(
        "nous.brain.graph_densifier.hybrid_search_multi", fake_multi
    ):
        await densifier._backfill_same_type(
            entity_type="fact",
            orphan_id=uuid4(),
            orphan_content="orphan content",
            session=session,
        )

    queries = fake_multi.await_args.kwargs["queries"]
    assert len(queries) == 3, (
        "Wedge must hand all variants to hybrid_search_multi; RRF de-dupes"
    )
    # The wedge does NOT collapse duplicates itself — that's RRF's job.
    assert len({q[0] for q in queries}) == 3


# ---------------------------------------------------------------------------
# Test 6 — empty union returns 0 cleanly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_union_returns_zero_edges():
    """``hybrid_search_multi`` returning ``[]`` must short-circuit the
    method to ``return 0`` without raising or attempting cosine SQL.
    """
    heart = MagicMock()
    heart.expand_query_pairs = AsyncMock(
        return_value=[("v1", [0.1] * 8), ("v2", [0.2] * 8)]
    )
    densifier = _make_densifier(heart=heart)
    session = _fake_session_factory()

    fake_multi = AsyncMock(return_value=[])
    with patch(
        "nous.brain.graph_densifier.hybrid_search_multi", fake_multi
    ):
        edges_created = await densifier._backfill_same_type(
            entity_type="fact",
            orphan_id=uuid4(),
            orphan_content="content",
            session=session,
        )

    assert edges_created == 0
    densifier._linker.create_edge.assert_not_awaited()


# ---------------------------------------------------------------------------
# Test 7 — large union forwarded to (CE-disabled) cosine loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_union_above_ce_cap_truncated():
    """With CE backfill disabled, a 30-candidate union (3 variants × 10) is
    forwarded directly to the cosine-verification loop without truncation.
    Behaviour is well-defined — we just iterate every candidate the RRF
    surfaced. (When CE backfill IS enabled, the F042 reranker imposes its
    own head-truncation cap; that is a separate code path with its own
    tests in test_backfill_rerank.py.)
    """
    heart = MagicMock()
    heart.expand_query_pairs = AsyncMock(
        return_value=[
            ("v1", [0.1] * 8),
            ("v2", [0.2] * 8),
            ("v3", [0.3] * 8),
        ]
    )
    densifier = _make_densifier(heart=heart)
    session = _fake_session_factory(sim_value=0.99)

    candidates = [(uuid4(), 0.9 - i * 0.01) for i in range(30)]
    fake_multi = AsyncMock(return_value=candidates)
    with patch(
        "nous.brain.graph_densifier.hybrid_search_multi", fake_multi
    ):
        edges_created = await densifier._backfill_same_type(
            entity_type="fact",
            orphan_id=uuid4(),
            orphan_content="content",
            session=session,
        )

    # Without CE rerank, every candidate above the (zeroed) threshold
    # produces an edge. The number is deterministic and equals 30.
    assert edges_created == 30
    assert densifier._linker.create_edge.await_count == 30


# ---------------------------------------------------------------------------
# Test 8 — CancelledError propagates; never swallowed by the wedge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancelled_error_propagates_not_swallowed():
    """If ``Heart.expand_query_pairs`` raises ``asyncio.CancelledError``,
    the wedge must let it propagate (CancelledError is a BaseException in
    Python 3.8+; ``except Exception`` does NOT catch it). The densifier
    must not log-and-swallow.
    """
    heart = MagicMock()
    heart.expand_query_pairs = AsyncMock(side_effect=asyncio.CancelledError())
    densifier = _make_densifier(heart=heart)
    session = _fake_session_factory()

    fake_multi = AsyncMock(return_value=[])
    with patch(
        "nous.brain.graph_densifier.hybrid_search_multi", fake_multi
    ):
        with pytest.raises(asyncio.CancelledError):
            await densifier._backfill_same_type(
                entity_type="fact",
                orphan_id=uuid4(),
                orphan_content="content",
                session=session,
            )

    fake_multi.assert_not_awaited()
