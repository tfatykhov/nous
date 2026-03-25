"""Shared hybrid search utilities for Heart memory types.

Provides a reusable hybrid vector + keyword search function that each
Heart manager calls with table-specific parameters.  Uses Reciprocal
Rank Fusion (RRF) to combine vector and keyword results (F025).
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def _resolve_vector_weight() -> float:
    """Resolve vector_weight from runtime config > settings > default 0.7."""
    from nous.config import Settings
    from nous.runtime_config import RuntimeConfig

    try:
        settings = Settings()
    except Exception:
        return 0.7
    return RuntimeConfig.get().get_vector_weight(settings)


def _resolve_rrf_k() -> int:
    """Resolve rrf_k from runtime config > settings > default 60."""
    from nous.config import Settings
    from nous.runtime_config import RuntimeConfig

    try:
        settings = Settings()
    except Exception:
        return 60
    return RuntimeConfig.get().get_rrf_k(settings)


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

    filter_clauses = f"AND t.agent_id = :agent_id AND t.active = true {extra_where}"

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

    # Keyword search
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

        wrapped = _wrap_with_score(item, (getattr(item, "score", 0) or 0) * boost)
        boosted.append((wrapped, boost))

    # Sort by boost descending (stable sort preserves relevance order within same boost)
    boosted.sort(key=lambda x: x[1], reverse=True)
    return [item for item, _ in boosted]
