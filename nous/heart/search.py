"""Shared hybrid search utilities for Heart memory types.

Provides a reusable hybrid vector + keyword search function that each
Heart manager calls with table-specific parameters.  Uses Reciprocal
Rank Fusion (RRF) to combine vector and keyword results (F025).

Also provides MMR diversity re-ranking (F030) for reducing redundancy
in cross-type recall results.
"""

from __future__ import annotations

import json
import math
from functools import lru_cache
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two vectors (pure Python — no numpy dependency).

    Used by MMR re-ranking (F030). Returns 0.0 if either vector is zero.
    """
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


async def set_local_ef_search(session: AsyncSession, value: int) -> None:
    """Prepare a filtered HNSW query — Postgres only, no-op elsewhere.

    Two transaction-scoped GUCs:
    - ``hnsw.ef_search``: widens the candidate horizon (pgvector
      post-applies WHERE filters to the approximate walk, so a tight
      horizon can return fewer rows than the LIMIT).
    - ``hnsw.iterative_scan = strict_order`` (pgvector >= 0.8, codex P1):
      keeps scanning in exact distance order until the LIMIT is satisfied
      AFTER filtering — this removes the missed-match failure mode on
      multi-tenant tables where other agents' nearby vectors could exhaust
      a fixed horizon. Integrity-sensitive callers (fact dedup) rely on
      this where available. Set DIRECTLY under a savepoint (codex round
      7): probing ``current_setting(..., true)`` returns NULL on a fresh
      backend before pgvector's library registers its GUCs, falsely
      reporting "unavailable" right before the first vector query. The
      SET itself either succeeds (registered GUC or accepted placeholder)
      or errors on pgvector < 0.8 (reserved ``hnsw.`` prefix, unknown
      parameter) — the savepoint absorbs that error so the surrounding
      transaction degrades cleanly to the ef_search margin.

    Guarded by dialect so SQLite test harnesses don't choke.
    """
    bind = getattr(session, "bind", None)
    if bind is None or getattr(getattr(bind, "dialect", None), "name", "") != "postgresql":
        return
    await session.execute(text(f"SET LOCAL hnsw.ef_search = {int(value)}"))
    try:
        async with session.begin_nested():
            await session.execute(
                text("SET LOCAL hnsw.iterative_scan = strict_order")
            )
    except Exception:
        pass  # pgvector < 0.8 — GUC absent; ef_search margin still applies


@lru_cache(maxsize=8)
def _cached_settings(_env_fingerprint: tuple) -> "object":
    """Construct Settings at most once per env-fingerprint (audit P3).

    The three resolvers below ran a full pydantic-settings construction
    (~300-field env + .env parse) on EVERY hybrid_search call — 2-3x per
    search, ~10x per recall. The cache key is the tuple of env vars these
    resolvers actually depend on, so tests that monkeypatch NOUS_RRF_K /
    NOUS_VECTOR_WEIGHT / NOUS_HYBRID_SEARCH_KEYWORD_ENABLED still see
    fresh values (changed env → new fingerprint → new Settings).
    RuntimeConfig overrides are NOT cached — they are consulted per call.
    """
    from nous.config import Settings

    return Settings()


def _resolver_settings() -> "object | None":
    import os

    fingerprint = (
        os.environ.get("NOUS_RRF_K"),
        os.environ.get("NOUS_VECTOR_WEIGHT"),
        os.environ.get("NOUS_HYBRID_SEARCH_KEYWORD_ENABLED"),
    )
    try:
        return _cached_settings(fingerprint)
    except Exception:
        return None


def _resolve_vector_weight() -> float:
    """Resolve vector_weight from runtime config > settings > default 0.7."""
    from nous.runtime_config import RuntimeConfig

    settings = _resolver_settings()
    if settings is None:
        return 0.7
    return RuntimeConfig.get().get_vector_weight(settings)


def _resolve_rrf_k() -> int:
    """Resolve rrf_k from runtime config > settings > default 60."""
    from nous.runtime_config import RuntimeConfig

    settings = _resolver_settings()
    if settings is None:
        return 60
    return RuntimeConfig.get().get_rrf_k(settings)


def _resolve_keyword_enabled() -> bool:
    """Resolve hybrid_search_keyword_enabled from settings > default True.

    F054: operator opt-out for vector-dominant corpora. Eval (F051) showed
    vector-only ties default RRF byte-for-byte on personal-Q&A and within
    -0.2% on codebase docs, so the FTS query is dead weight on those shapes.
    No RuntimeConfig override path — this is intentionally a static config
    flip, not a per-request knob.
    """
    settings = _resolver_settings()
    if settings is None:
        return True
    return bool(getattr(settings, "hybrid_search_keyword_enabled", True))


def _rrf_merge(
    vector_ranked: list[tuple[UUID, float]],
    keyword_ranked: list[tuple[UUID, float]],
    k: int,
    vector_weight: float,
    limit: int,
) -> list[tuple[UUID, float]]:
    """Merge two ranked lists using Reciprocal Rank Fusion.

    rrf_score(doc) = vector_weight / (k + vector_rank)
                   + keyword_weight / (k + keyword_rank)

    Docs appearing in only one list get a penalty rank of limit + 1.
    """
    keyword_weight = 1.0 - vector_weight
    penalty_rank = limit + 1

    # Build rank maps (0-indexed)
    vector_ranks: dict[UUID, int] = {doc_id: i for i, (doc_id, _) in enumerate(vector_ranked)}
    keyword_ranks: dict[UUID, int] = {doc_id: i for i, (doc_id, _) in enumerate(keyword_ranked)}

    all_ids = set(vector_ranks) | set(keyword_ranks)
    if not all_ids:
        return []

    scored: list[tuple[UUID, float]] = []
    for doc_id in all_ids:
        v_rank = vector_ranks.get(doc_id, penalty_rank)
        k_rank = keyword_ranks.get(doc_id, penalty_rank)
        score = vector_weight / (k + v_rank) + keyword_weight / (k + k_rank)
        scored.append((doc_id, score))

    scored.sort(key=lambda x: x[1], reverse=True)

    # Normalize to 0-1 range relative to theoretical max
    # Max RRF = vector_weight/(k+0) + keyword_weight/(k+0) = 1/k
    # (since vector_weight + keyword_weight = 1.0 by construction)
    max_score = 1.0 / k
    if max_score > 0 and scored:
        scored = [(doc_id, score / max_score) for doc_id, score in scored]

    return scored[:limit]


async def hybrid_search(
    session: AsyncSession,
    table: str,
    embedding: list[float] | None,
    query_text: str,
    agent_id: str,
    extra_where: str = "",
    extra_params: dict | None = None,
    limit: int = 10,
    vector_weight: float | None = None,
    active_filter: bool = True,
) -> list[tuple[UUID, float]]:
    """Hybrid vector + keyword search over a Heart table using RRF.

    Uses Reciprocal Rank Fusion to combine vector and keyword results:
    1. Vector similarity via cosine distance on embedding column (ranked list)
    2. Keyword relevance via ts_rank_cd on search_tsv column (ranked list)
    3. RRF score = vector_weight / (k + vector_rank) + keyword_weight / (k + keyword_rank)

    This solves the scale mismatch where keyword scores max at ~0.08
    vs vector scores at 0.5-0.9, making weighted-sum keyword-blind.

    Weight resolution order:
    1. Explicit vector_weight param (highest priority)
    2. Runtime override (set via /admin/search-weights API)
    3. NOUS_VECTOR_WEIGHT env var / config default
    4. Fallback: 0.7

    Args:
        session: Active SQLAlchemy async session.
        table: Fully qualified table name (e.g. "heart.episodes").
        embedding: Query embedding vector, or None for keyword-only fallback.
        query_text: Text query for keyword search.
        agent_id: Agent ID filter (always applied).
        extra_where: Additional SQL WHERE clauses (e.g. "AND category = :category").
            Must use :param style placeholders with values in extra_params.
        extra_params: Additional parameters for extra_where bindings.
        limit: Maximum number of results to return.
        vector_weight: Weight for vector score (keyword weight = 1 - vector_weight).
            None = resolve from runtime config / settings / default.
        active_filter: Whether to include ``AND t.active = true`` in the WHERE
            clause.  Set to ``False`` for tables without an ``active`` column
            (e.g. ``brain.decisions``).  Default ``True``.

    Returns:
        List of (id, rrf_score) ordered by score DESC.
    """
    if vector_weight is None:
        vector_weight = _resolve_vector_weight()
    rrf_k = _resolve_rrf_k()

    params: dict = {
        "agent_id": agent_id,
        "query_text": query_text,
        "limit": limit,
        "limit_expanded": limit * 3,
    }
    if extra_params:
        params.update(extra_params)

    active_clause = "AND t.active = true" if active_filter else ""
    filter_clauses = f"AND t.agent_id = :agent_id {active_clause} {extra_where}"

    vector_results: list[tuple[UUID, float]] = []
    keyword_results: list[tuple[UUID, float]] = []

    if embedding is not None:
        # Vector search
        params["query_embedding"] = "[" + ",".join(str(float(v)) for v in embedding) + "]"
        vector_sql = text(f"""
            SELECT t.id, 1 - (t.embedding <=> CAST(:query_embedding AS vector)) AS score
            FROM {table} t
            WHERE t.embedding IS NOT NULL {filter_clauses}
            ORDER BY t.embedding <=> CAST(:query_embedding AS vector)
            LIMIT :limit_expanded
        """)
        result = await session.execute(vector_sql, params)
        vector_results = [(row.id, float(row.score)) for row in result.all()]

    # F054: keyword channel toggle. Skip the FTS query when disabled.
    # _rrf_merge handles an empty keyword list correctly — degenerates to
    # vector-only ranking weighted by vector_weight (penalty rank applied to
    # missing channel cancels out when one channel is empty).
    # Force-on path when embedding is None preserves the keyword-only
    # fallback: callers that intentionally pass embedding=None still get FTS.
    keyword_enabled = _resolve_keyword_enabled() or embedding is None
    if keyword_enabled:
        keyword_sql = text(f"""
            SELECT t.id,
                ts_rank_cd(t.search_tsv, plainto_tsquery('english', :query_text))
                / (1.0 + ts_rank_cd(t.search_tsv, plainto_tsquery('english', :query_text))) AS score
            FROM {table} t
            WHERE t.search_tsv @@ plainto_tsquery('english', :query_text)
                {filter_clauses}
            ORDER BY score DESC
            LIMIT :limit_expanded
        """)
        result = await session.execute(keyword_sql, params)
        keyword_results = [(row.id, float(row.score)) for row in result.all()]

    if embedding is None:
        # Keyword-only fallback — return keyword results directly
        return keyword_results[:limit]

    return _rrf_merge(vector_results, keyword_results, rrf_k, vector_weight, limit)


# ---------------------------------------------------------------------------
# F050: Multi-query expansion — wrapper that fans `hybrid_search` over N
# (text, embedding) pairs and fuses the per-variant ranked lists with
# equal-weight Reciprocal Rank Fusion.
# ---------------------------------------------------------------------------


def _rrf_merge_n(
    ranked_lists: list[list[tuple[UUID, float]]],
    k: int,
    limit: int,
) -> list[tuple[UUID, float]]:
    """Equal-weight Reciprocal Rank Fusion across N ranked lists.

    Score formula (per doc):
        score = sum_{i=1..N} (1/N) / (k + rank_i)
    where ``rank_i`` is the doc's 0-indexed position in list ``i`` (or the
    penalty rank ``limit + 1`` if the doc is missing from list ``i``).

    Normalization is **byte-identical** to ``_rrf_merge`` at N=1: the
    theoretical max of the unnormalized sum is ``N * (1/N) * (1/k) = 1/k``
    regardless of N, and we divide by ``1/k`` so the returned scores live
    in the same ``[0, 1]`` range every downstream consumer (frame boost,
    MMR, CE rerank, F017 relevance floor) already expects.

    Note: the single-query fast-path in ``hybrid_search_multi`` short-circuits
    before reaching this function; it is only invoked when ``len(ranked_lists) >= 2``.
    """
    n = len(ranked_lists)
    if n == 0:
        return []

    penalty_rank = limit + 1
    per_list_weight = 1.0 / n

    rank_maps: list[dict[UUID, int]] = [
        {doc_id: i for i, (doc_id, _) in enumerate(rl)} for rl in ranked_lists
    ]

    all_ids: set[UUID] = set()
    for rm in rank_maps:
        all_ids.update(rm)

    if not all_ids:
        return []

    scored: list[tuple[UUID, float]] = []
    for doc_id in all_ids:
        score = 0.0
        for rm in rank_maps:
            r = rm.get(doc_id, penalty_rank)
            score += per_list_weight / (k + r)
        scored.append((doc_id, score))

    scored.sort(key=lambda x: x[1], reverse=True)

    # Same normalization as _rrf_merge: divide by theoretical max = 1/k.
    # Independent of N because per-list weights sum to 1.0 and best per-list
    # contribution is (1/N) / k, summing to 1/k across N lists.
    max_score = 1.0 / k
    if max_score > 0 and scored:
        scored = [(doc_id, score / max_score) for doc_id, score in scored]

    return scored[:limit]


def _rrf_merge_n_weighted(
    weighted_lists: list[tuple[list[tuple[UUID, float]], float]],
    k: int,
    limit: int,
) -> list[tuple[UUID, float]]:
    """N-leg Reciprocal Rank Fusion with explicit per-leg weights (F082).

    Generalises ``_rrf_merge`` to N legs with arbitrary weights:

        rrf_score(doc) = Σ_legs  w_leg / (k + rank_leg(doc))

    ``weighted_lists`` is a list of ``(ranked_list, weight)`` pairs.
    Docs absent from a leg receive the penalty rank ``limit + 1``.

    Normalisation matches ``_rrf_merge``: divide by theoretical maximum
    (Σ w_leg) / k, so the returned scores live in ``[0, 1]``.

    Back-compat guarantee: when called with exactly two legs whose weights
    sum to 1.0 and the same ``k`` / ``limit`` as ``_rrf_merge``, the output
    is **bit-identical** to ``_rrf_merge``.  See ``test_ppr_recall.py`` for
    the regression test.

    Args:
        weighted_lists: ``[(ranked_list, weight), ...]``.  Empty lists are
            allowed (contribute only penalty ranks for their weight).
        k: RRF smoothing constant.
        limit: Maximum results to return (also sets the penalty rank to
            ``limit + 1`` for docs absent from a leg).

    Returns:
        ``(id, score)`` pairs sorted descending, length ``<= limit``.
    """
    if not weighted_lists:
        return []

    total_weight = sum(w for _, w in weighted_lists)
    if total_weight <= 0.0:
        return []

    penalty_rank = limit + 1

    rank_maps: list[tuple[dict[UUID, int], float]] = [
        ({doc_id: i for i, (doc_id, _) in enumerate(rl)}, w)
        for rl, w in weighted_lists
    ]

    all_ids: set[UUID] = set()
    for rm, _ in rank_maps:
        all_ids.update(rm)

    if not all_ids:
        return []

    scored: list[tuple[UUID, float]] = []
    for doc_id in all_ids:
        score = 0.0
        for rm, w in rank_maps:
            r = rm.get(doc_id, penalty_rank)
            score += w / (k + r)
        scored.append((doc_id, score))

    scored.sort(key=lambda x: x[1], reverse=True)

    # Normalise: max possible score = Σ w_leg / k (each leg ranks the doc #0)
    max_score = total_weight / k
    if max_score > 0.0 and scored:
        scored = [(doc_id, s / max_score) for doc_id, s in scored]

    return scored[:limit]


async def hybrid_search_multi(
    session: AsyncSession,
    table: str,
    queries: list[tuple[str, list[float] | None]],
    agent_id: str,
    extra_where: str = "",
    extra_params: dict | None = None,
    limit: int = 10,
    vector_weight: float | None = None,
    active_filter: bool = True,
) -> list[tuple[UUID, float]]:
    """Multi-query hybrid search over a Heart table (F050).

    Runs ``hybrid_search`` once per (text, embedding) pair in ``queries`` and
    fuses the per-variant ranked lists with equal-weight Reciprocal Rank Fusion
    via ``_rrf_merge_n``. Score scale matches single-query ``hybrid_search``.

    Single-element fast-path: when ``len(queries) == 1`` (or a degenerate empty
    list collapses to one), this delegates directly to ``hybrid_search`` so the
    no-expansion call path is **byte-identical** to today's behavior — no
    over-fetch, no extra Python merge work.

    Each per-variant call internally over-fetches ``limit * 3`` (matching
    ``hybrid_search``) to give the cross-variant RRF something to work with.

    Args:
        session: Active SQLAlchemy async session.
        table: Fully qualified table name (e.g. ``"heart.facts"``).
        queries: List of ``(query_text, embedding)`` pairs. Each embedding may
            be ``None`` to force keyword-only fallback for that variant.
        agent_id: Agent ID filter (always applied).
        extra_where: Additional SQL WHERE clauses; applied to every variant.
        extra_params: Additional parameters for ``extra_where`` bindings.
        limit: Maximum number of results to return.
        vector_weight: Weight for vector score within each per-variant call.
        active_filter: Whether to include ``AND t.active = true``.

    Returns:
        List of ``(id, rrf_score)`` ordered by score DESC, length ``<= limit``.
    """
    if not queries:
        # Loud raise (silent-failure-hunter WARN #12). Empty queries is a
        # caller bug, not a normal degenerate condition. Returning [] would
        # silently swallow the bad call and produce empty recall results
        # downstream — exactly the failure mode F050 is supposed to surface.
        raise ValueError(
            "hybrid_search_multi requires at least one (text, embedding) pair"
        )

    # Single-element fast-path — delegate to hybrid_search so flag-off /
    # single-variant call sites are byte-identical to today's behavior.
    if len(queries) == 1:
        text_only, embedding = queries[0]
        return await hybrid_search(
            session=session,
            table=table,
            embedding=embedding,
            query_text=text_only,
            agent_id=agent_id,
            extra_where=extra_where,
            extra_params=extra_params,
            limit=limit,
            vector_weight=vector_weight,
            active_filter=active_filter,
        )

    rrf_k = _resolve_rrf_k()

    ranked_lists: list[list[tuple[UUID, float]]] = []
    for query_text_i, embedding_i in queries:
        per_variant = await hybrid_search(
            session=session,
            table=table,
            embedding=embedding_i,
            query_text=query_text_i,
            agent_id=agent_id,
            extra_where=extra_where,
            extra_params=extra_params,
            limit=limit,
            vector_weight=vector_weight,
            active_filter=active_filter,
        )
        ranked_lists.append(per_variant)

    return _rrf_merge_n(ranked_lists, rrf_k, limit)


class _ScoredWrapper:
    """Lightweight proxy that overrides .score without mutating the ORM object."""
    __slots__ = ("_item", "_score")

    def __init__(self, item, score: float) -> None:
        object.__setattr__(self, "_item", item)
        object.__setattr__(self, "_score", score)

    def __getattr__(self, name: str):
        if name == "score":
            return object.__getattribute__(self, "_score")
        return getattr(object.__getattribute__(self, "_item"), name)


def _wrap_with_score(item, score: float):
    """Wrap an item with an overridden score."""
    return _ScoredWrapper(item, score)


def apply_frame_boost(
    results: list,
    current_frame: str | None = None,
    current_censors: list[str] | None = None,
) -> list:
    """Re-rank results with frame and censor boost (003.2).

    Same-frame memories get 1.3x boost. Censor overlap adds up to 1.2x.
    NULL encoded_frame/encoded_censors = neutral (no boost/penalty).
    """
    if not current_frame and not current_censors:
        return results

    boosted = []
    for item in results:
        boost = 1.0

        # Frame boost (D3): same frame → 1.3x
        encoded_frame = getattr(item, "encoded_frame", None)
        if encoded_frame and current_frame and encoded_frame == current_frame:
            boost *= 1.3

        # Censor overlap boost (D4): Jaccard * 0.2 + 1.0
        enc_censors = set(getattr(item, "encoded_censors", None) or [])
        cur_censors = set(current_censors or [])
        if enc_censors and cur_censors:
            union = enc_censors | cur_censors
            if union:
                jaccard = len(enc_censors & cur_censors) / len(union)
                boost *= 1.0 + 0.2 * jaccard

        wrapped = _wrap_with_score(item, min((getattr(item, "score", 0) or 0) * boost, 1.0))
        boosted.append((wrapped, boost))

    # 3a (2026-06-13 audit, eval-gated): sort by the BOOSTED SCORE, not the boost
    # multiplier. The old `key=x[1]` produced a frame-tier order — every same-frame
    # item ranked above every non-frame item regardless of relevance, so a 0.3-
    # relevance frame match buried a 0.95-relevance non-match. Frame match is a
    # boost on relevance, not an override of it; sort by the boosted score so a
    # close-relevance frame match still wins but a large relevance gap dominates.
    boosted.sort(key=lambda x: x[0].score, reverse=True)
    return [item for item, _ in boosted]


# --- F030: MMR Diversity Re-Ranking ---

import logging

_mmr_logger = logging.getLogger(__name__)

# Table mapping for batch embedding fetch
_TYPE_TO_TABLE = {
    "fact": "heart.facts",
    "episode": "heart.episodes",
    "procedure": "heart.procedures",
    "censor": "heart.censors",
}


def mmr_rerank(
    candidates: list,
    embeddings: dict,
    query_embedding: list[float],
    lambda_: float = 0.7,
    limit: int = 10,
) -> list:
    """Maximal Marginal Relevance re-ranking for diversity (F030).

    Greedily selects items maximizing:
      MMR(d) = λ · cos_sim(d, query) − (1−λ) · max(cos_sim(d, selected))

    Items without embeddings are appended after MMR-selected items
    in descending score order.

    Args:
        candidates: Pre-scored results (must have .id and .score attrs).
        embeddings: Map of result ID → embedding vector (list[float]).
        query_embedding: The query's embedding vector.
        lambda_: Relevance vs diversity weight (0.0–1.0). Default 0.7.
        limit: Number of results to return.

    Returns:
        Re-ranked list, length ≤ limit.
    """
    if not candidates:
        return []

    # Separate candidates with/without embeddings
    with_emb = [(c, embeddings[c.id]) for c in candidates if c.id in embeddings]
    without_emb = sorted(
        [c for c in candidates if c.id not in embeddings],
        key=lambda c: c.score,
        reverse=True,
    )

    if len(with_emb) <= 1:
        # Not enough candidates for diversity — fall back to score sort
        all_sorted = sorted(candidates, key=lambda c: c.score, reverse=True)
        return all_sorted[:limit]

    # Precompute query similarities
    query_sims: dict = {}
    for c, emb in with_emb:
        query_sims[c.id] = cosine_similarity(query_embedding, emb)

    selected: list = []
    selected_embs: list[list[float]] = []
    remaining = list(with_emb)

    while len(selected) < limit and remaining:
        best_score = -float("inf")
        best_idx = 0

        for i, (c, emb) in enumerate(remaining):
            relevance = query_sims[c.id]

            if selected_embs:
                max_sim = max(
                    cosine_similarity(emb, s_emb) for s_emb in selected_embs
                )
            else:
                max_sim = 0.0

            mmr = lambda_ * relevance - (1 - lambda_) * max_sim

            if mmr > best_score:
                best_score = mmr
                best_idx = i

        winner, winner_emb = remaining.pop(best_idx)
        selected.append(winner)
        selected_embs.append(winner_emb)

    # Append non-embedded items if space remains
    dropped = 0
    for c in without_emb:
        if len(selected) >= limit:
            dropped += 1
            continue
        selected.append(c)
    if dropped:
        _mmr_logger.info("MMR: %d unembedded candidates dropped (limit reached)", dropped)

    return selected


async def batch_fetch_embeddings(
    session: AsyncSession,
    type_ids: dict[str, list[UUID]],
    agent_id: str,
) -> dict[UUID, list[float]]:
    """Batch-fetch embeddings for recall results grouped by memory type (F030).

    Issues one query per memory type (2-4 small index scans on primary key).
    Returns a flat dict mapping result ID → embedding vector.

    Args:
        session: Active SQLAlchemy async session.
        type_ids: Map of memory type → list of IDs to fetch embeddings for.
        agent_id: Agent ID for scoping (defensive filter).

    Returns:
        Dict of {UUID: list[float]} for all IDs that have embeddings.
    """
    embeddings: dict[UUID, list[float]] = {}

    for mem_type, ids in type_ids.items():
        table = _TYPE_TO_TABLE.get(mem_type)
        if not table or not ids:
            continue

        # Build parameterized IN clause
        placeholders = ", ".join(f":id_{i}" for i in range(len(ids)))
        sql = text(f"""
            SELECT id, embedding::text
            FROM {table}
            WHERE id IN ({placeholders})
              AND agent_id = :agent_id
              AND embedding IS NOT NULL
        """)
        params: dict = {f"id_{i}": uid for i, uid in enumerate(ids)}
        params["agent_id"] = agent_id

        result = await session.execute(sql, params)
        for row in result.all():
            emb_str = row.embedding
            if isinstance(emb_str, str):
                emb = json.loads(emb_str)
            elif isinstance(emb_str, list):
                emb = emb_str
            else:
                continue
            embeddings[row.id] = emb

    return embeddings
