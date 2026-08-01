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
    return_limit: int | None = None,
    cap_ranks_at_penalty: bool = False,
) -> list[tuple[UUID, float]]:
    """Merge two ranked lists using Reciprocal Rank Fusion.

    rrf_score(doc) = vector_weight / (k + vector_rank)
                   + keyword_weight / (k + keyword_rank)

    Docs appearing in only one list get a penalty rank of limit + 1.

    ``return_limit`` (default = ``limit``) decouples HOW MANY rows come back
    from the ``limit`` that defines ``penalty_rank``. A caller that needs a
    wider candidate set for post-merge re-ranking (e.g. outcome demotion in
    ``Brain._query``) must use this instead of inflating ``limit`` — a bigger
    ``limit`` changes ``penalty_rank`` and therefore silently changes the
    score of every single-list document (codex #577 r1 / the same trap flagged
    on #574).

    ``cap_ranks_at_penalty`` clamps OBSERVED ranks to ``penalty_rank`` as well.
    Pinning the penalty base alone is not sufficient: the legs over-fetch, so a
    document absent from a list at one row count can APPEAR deep in that list
    at a larger one, and past ``penalty_rank`` an observed rank contributes
    LESS than the missing-leg penalty — presence scores worse than absence
    (crossover exactly at ``rank == penalty_rank``). Capping makes "deep in the
    list" and "absent" score identically, which is what makes the pin actually
    hold as the row count moves. Default ``False`` because unpinned callers
    routinely see ranks past ``limit + 1`` (legs fetch ``limit * 3``), and
    clamping those would change today's behaviour.
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
        if cap_ranks_at_penalty:
            v_rank = min(v_rank, penalty_rank)
            k_rank = min(k_rank, penalty_rank)
        score = vector_weight / (k + v_rank) + keyword_weight / (k + k_rank)
        scored.append((doc_id, score))

    scored.sort(key=lambda x: x[1], reverse=True)

    # Normalize to 0-1 range relative to theoretical max
    # Max RRF = vector_weight/(k+0) + keyword_weight/(k+0) = 1/k
    # (since vector_weight + keyword_weight = 1.0 by construction)
    max_score = 1.0 / k
    if max_score > 0 and scored:
        scored = [(doc_id, score / max_score) for doc_id, score in scored]

    return scored[: (return_limit if return_limit is not None else limit)]


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
    penalty_limit: int | None = None,
    require_keyword_hit: bool = False,
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
        penalty_limit: Decouples the RRF missing-leg penalty from ``limit``.
            ``_rrf_merge`` scores a document absent from one leg at
            ``penalty_rank = limit + 1``, so by default RAISING ``limit`` to
            fetch more rows also DEPRESSES every single-leg document's score —
            the trap ``_rrf_merge``'s own docstring documents. Pass a fixed
            value here to hold scores stable while ``limit`` varies, so a
            row-count knob stops doubling as a scoring knob. It also pins the
            per-leg SQL fetch window (``limit * 3``), because widening that
            window changes which documents are present in each leg and a
            document appearing beyond the penalty rank scores WORSE than one
            absent from the leg entirely. Score invariance therefore holds
            while ``limit <= penalty_limit * 3``. ``None`` (default)
            preserves the coupled behaviour exactly.

    Returns:
        List of (id, rrf_score) ordered by score DESC.
    """
    if vector_weight is None:
        vector_weight = _resolve_vector_weight()
    rrf_k = _resolve_rrf_k()

    # Codex r2: pinning penalty_rank alone does NOT decouple scoring from the
    # allotment, because the SQL legs fetch limit * 3 — so raising `limit`
    # widens the candidate SET too. A document absent from the keyword leg at
    # limit=20 can surface at keyword rank 70 at limit=30, and that is WORSE
    # than being absent: at k=30 with a pinned penalty_rank of 21 the keyword
    # term falls 0.005882 -> 0.003000. (Crossover is exactly at
    # rank == penalty_rank — beyond it, presence scores below absence.)
    #
    # So base the fetch window on the pinned value when one is given. The
    # max() keeps the window from starving the requested row count when
    # `limit` exceeds it; invariance therefore holds while
    # limit <= penalty_limit * 3, which covers the intended use
    # (penalty_limit=20, limit sweeping 10..60).
    fetch_base = penalty_limit if penalty_limit is not None else limit
    params: dict = {
        "agent_id": agent_id,
        "query_text": query_text,
        "limit": limit,
        "limit_expanded": max(fetch_base * 3, limit),
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
    # codex #574 r4: require_keyword_hit is meaningless without the keyword
    # leg — force it on even when the channel toggle is off, else the filter
    # would drop EVERY result in a vector-only deployment.
    keyword_enabled = _resolve_keyword_enabled() or embedding is None or require_keyword_hit
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

    # Codex #574 r5: when filtering to keyword hits, merge WITHOUT truncation —
    # _rrf_merge slices to `limit` before the filter runs, and any fixed
    # expansion can still be consumed entirely by vector-only hits (verified:
    # at limit=1 with 4 decoys the keyword-anchored fact ranked 4th in the
    # merged list and a 3x window still starved it). Candidate count is
    # bounded by the SQL legs' limit_expanded, so this is cheap.
    merge_limit = (
        max(limit, len(vector_results) + len(keyword_results))
        if require_keyword_hit
        else limit
    )
    # penalty_limit (when set) feeds _rrf_merge's ``limit`` — which is ONLY
    # used to derive penalty_rank — while the row count moves to
    # ``return_limit``. Note merge_limit is deliberately inflated above under
    # require_keyword_hit, so this must not simply swap both: the row count
    # keeps its inflated value, only the penalty base is pinned.
    if penalty_limit is not None:
        merged = _rrf_merge(
            vector_results, keyword_results, rrf_k, vector_weight,
            penalty_limit, return_limit=merge_limit,
            cap_ranks_at_penalty=True,
        )
    else:
        merged = _rrf_merge(vector_results, keyword_results, rrf_k, vector_weight, merge_limit)
    if require_keyword_hit:
        # Codex #574 r3: _rrf_merge's missing-leg penalty rank (limit+1) is
        # nearly free at typical k (a vector-only rank-0 hit normalizes to
        # ~0.97), so score floors cannot exclude nearest-neighbor noise.
        # Callers that need a LEXICAL anchor (e.g. the Session Profile leg —
        # domain facts only for domain turns) filter to docs the keyword leg
        # actually matched. No keyword hits => empty result, by design.
        allowed = {doc_id for doc_id, _ in keyword_results}
        merged = [(doc_id, score) for doc_id, score in merged if doc_id in allowed]
        merged = merged[:limit]
    return merged


# ---------------------------------------------------------------------------
# F050: Multi-query expansion — wrapper that fans `hybrid_search` over N
# (text, embedding) pairs and fuses the per-variant ranked lists with
# equal-weight Reciprocal Rank Fusion.
# ---------------------------------------------------------------------------


def _rrf_merge_n(
    ranked_lists: list[list[tuple[UUID, float]]],
    k: int,
    limit: int,
    return_limit: int | None = None,
    cap_ranks_at_penalty: bool = False,
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

    ``return_limit`` (default = ``limit``) decouples HOW MANY rows come back
    from the ``limit`` that defines ``penalty_rank`` — the same escape hatch
    ``_rrf_merge`` carries, and for the same reason: a bigger ``limit`` moves
    ``penalty_rank`` and therefore silently rescores every doc missing from
    any list. Needed here too because ``Heart.recall`` derives its limit as
    ``limit * 2`` from an LLM-controlled parameter.

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
            # See _rrf_merge: past penalty_rank an observed rank contributes
            # LESS than being absent, so a doc surfacing deep in a variant as
            # the row count grows would still be rescored despite the pin.
            if cap_ranks_at_penalty:
                r = min(r, penalty_rank)
            score += per_list_weight / (k + r)
        scored.append((doc_id, score))

    scored.sort(key=lambda x: x[1], reverse=True)

    # Same normalization as _rrf_merge: divide by theoretical max = 1/k.
    # Independent of N because per-list weights sum to 1.0 and best per-list
    # contribution is (1/N) / k, summing to 1/k across N lists.
    max_score = 1.0 / k
    if max_score > 0 and scored:
        scored = [(doc_id, score / max_score) for doc_id, score in scored]

    return scored[: (return_limit if return_limit is not None else limit)]


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
    penalty_limit: int | None = None,
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
        penalty_limit: Pins the RRF missing-leg penalty base, decoupling it
            from ``limit``. Threaded to BOTH layers, because expansion stacks
            two of them: each per-variant ``hybrid_search`` runs its own
            vector/keyword ``_rrf_merge``, and the cross-variant fusion runs
            ``_rrf_merge_n``. Pinning only one would leave the other coupled.
            ``None`` (default) preserves today's behaviour exactly.

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
            penalty_limit=penalty_limit,
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
            penalty_limit=penalty_limit,
        )
        ranked_lists.append(per_variant)

    # Second penalty layer: pin the cross-variant fusion's base too, keeping
    # the row count on return_limit. Pinning only the per-variant merges above
    # would leave this one still tracking `limit`.
    if penalty_limit is not None:
        return _rrf_merge_n(
            ranked_lists, rrf_k, penalty_limit, return_limit=limit,
            cap_ranks_at_penalty=True,
        )
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
