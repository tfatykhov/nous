"""Shared hybrid search utilities for Heart memory types.

Provides a reusable hybrid vector + keyword search function that each
Heart manager calls with table-specific parameters. Follows the same
CTE pattern as Brain.query() (brain.py:418-456).
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def hybrid_search(
    session: AsyncSession,
    table: str,
    embedding: list[float] | None,
    query_text: str,
    agent_id: str,
    extra_where: str = "",
    extra_params: dict | None = None,
    limit: int = 10,
    vector_weight: float = 0.7,
) -> list[tuple[UUID, float]]:
    """Hybrid vector + keyword search over a Heart table.

    Uses same CTE pattern as Brain.query():
    1. Vector similarity via cosine distance on embedding column
    2. Keyword relevance via ts_rank_cd on search_tsv column
    3. Combined score = vector_weight * vector_score + (1 - vector_weight) * keyword_score

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

    Returns:
        List of (id, combined_score) ordered by score DESC.
    """
    params: dict = {
        "agent_id": agent_id,
        "query_text": query_text,
        "limit": limit,
        "limit_expanded": limit * 3,
    }
    if extra_params:
        params.update(extra_params)

    filter_clauses = f"AND t.agent_id = :agent_id AND t.active = true {extra_where}"

    if embedding is not None:
        # Full hybrid search: vector + keyword CTEs with FULL OUTER JOIN
        # Format embedding as pgvector string without spaces
        params["query_embedding"] = "[" + ",".join(str(float(v)) for v in embedding) + "]"
        keyword_weight = 1.0 - vector_weight
        sql = text(f"""
            WITH semantic AS (
                SELECT t.id, 1 - (t.embedding <=> CAST(:query_embedding AS vector)) AS score
                FROM {table} t
                WHERE t.embedding IS NOT NULL {filter_clauses}
                ORDER BY t.embedding <=> CAST(:query_embedding AS vector)
                LIMIT :limit_expanded
            ),
            keyword AS (
                SELECT t.id,
                    ts_rank_cd(t.search_tsv, plainto_tsquery('english', :query_text))
                    / (1.0 + ts_rank_cd(t.search_tsv, plainto_tsquery('english', :query_text))) AS score
                FROM {table} t
                WHERE t.search_tsv @@ plainto_tsquery('english', :query_text)
                    {filter_clauses}
                LIMIT :limit_expanded
            )
            SELECT COALESCE(s.id, k.id) AS id,
                (COALESCE(s.score, 0) * {vector_weight} + COALESCE(k.score, 0) * {keyword_weight}) AS combined_score
            FROM semantic s
            FULL OUTER JOIN keyword k ON s.id = k.id
            ORDER BY combined_score DESC
            LIMIT :limit
        """)
    else:
        # Keyword-only fallback (no embedding provider)
        sql = text(f"""
            SELECT t.id,
                ts_rank_cd(t.search_tsv, plainto_tsquery('english', :query_text))
                / (1.0 + ts_rank_cd(t.search_tsv, plainto_tsquery('english', :query_text))) AS score
            FROM {table} t
            WHERE t.search_tsv @@ plainto_tsquery('english', :query_text)
                {filter_clauses}
            ORDER BY score DESC
            LIMIT :limit
        """)

    result = await session.execute(sql, params)
    rows = result.all()

    return [(row.id, float(row.combined_score if hasattr(row, "combined_score") else row.score)) for row in rows]


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
