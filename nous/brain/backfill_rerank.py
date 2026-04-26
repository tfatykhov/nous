"""F043 — Cross-encoder rerank adapter for F040 sleep-cycle graph backfill.

This module is a thin adapter that bridges the shape mismatch between
``hybrid_search()``'s ``list[tuple[UUID, float]]`` return type and the
F042 cross-encoder reranker's mutable-``.score`` contract. It wraps each
``(UUID, rrf_score)`` row in a small dataclass, fetches the reranking
text for the candidate set in one ``IN (...)`` query, then delegates to
``nous.heart.reranker.cross_encoder_rerank`` for the actual scoring.

Design notes:

* **Settings sharing with F042.** ``cross_encoder_model`` and
  ``cross_encoder_text_limit`` are intentionally shared with F042; the
  MVP runs one reranker model across both recall and sleep backfill.
  Only the enable flag, top-K, and min-score floor are F043-specific.
* **``len(candidates) <= 1`` short-circuit in F042.** The F042 reranker
  returns the candidate list unchanged when there is 0 or 1 element.
  That path bypasses the ``ce_backfill_min_score`` floor for a lone
  survivor — the downstream cosine gate in
  ``graph_densifier._backfill_same_type`` / ``_backfill_cross_type``
  remains the correctness floor for that case. Documented behavior.
* **No ``sentence_transformers`` import here.** We only depend on
  ``CROSS_ENCODER_AVAILABLE`` and ``cross_encoder_rerank`` from
  ``nous.heart.reranker``, which already holds the ImportError guard.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Sequence
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from nous.brain._entity_config import _ENTITY_CONFIG
from nous.heart.reranker import CROSS_ENCODER_AVAILABLE, cross_encoder_rerank

logger = logging.getLogger(__name__)


@dataclass
class RerankCandidate:
    """Mutable wrapper to satisfy the F042 reranker's ``.score`` mutation contract."""

    id: UUID
    content: str
    score: float


async def fetch_candidate_content(
    session: AsyncSession,
    agent_id: str,
    entity_type: str,
    candidate_ids: Sequence[UUID],
    *,
    settings: object | None = None,
) -> dict[UUID, str]:
    """Batch-fetch reranking text for F040 backfill candidates.

    Uses ``_ENTITY_CONFIG[entity_type]`` to look up the table and content
    column. Adds ``AND t.agent_id = :agent_id`` for defense-in-depth even
    though upstream ``hybrid_search`` already scopes IDs by agent. Rows
    whose content is ``None`` / empty / whitespace-only are omitted.

    F054: when ``entity_type == "decision"`` and ``settings`` is provided,
    decision rows whose ``context.strip()`` is shorter than
    ``settings.ce_backfill_min_decision_chars`` (default 40) are also dropped.
    Mirrors the F045 fact-content guard (`ce_backfill_min_content_chars=80`)
    but applies at fetch time so the threshold is type-aware. Pass
    ``settings=None`` (or omit) to skip the guard — kept backward-compatible
    so existing callers don't break.
    """
    if not candidate_ids:
        return {}
    config = _ENTITY_CONFIG[entity_type]
    # _ENTITY_CONFIG tuple shape: (table, type_name, content_col, extra_where)
    table = config[0]
    content_col = config[2]
    placeholders = ", ".join(f":id_{i}" for i in range(len(candidate_ids)))
    params: dict[str, object] = {
        f"id_{i}": cid for i, cid in enumerate(candidate_ids)
    }
    params["agent_id"] = agent_id
    sql = text(
        f"SELECT t.id, {content_col} AS content "
        f"FROM {table} t "
        f"WHERE t.id IN ({placeholders}) AND t.agent_id = :agent_id"
    )
    rows = await session.execute(sql, params)

    # F054: type-aware min-content guard. Default 0 means "no guard"
    # (preserves behavior for callers that don't pass settings).
    min_chars = 0
    if settings is not None and entity_type == "decision":
        min_chars = int(getattr(settings, "ce_backfill_min_decision_chars", 0))

    result: dict[UUID, str] = {}
    for row in rows:
        content = row.content
        if not content:
            continue
        stripped = str(content).strip()
        if not stripped:
            continue
        if min_chars > 0 and len(stripped) < min_chars:
            continue  # F054: drop short-content decisions
        result[row.id] = str(content)
    return result


async def ce_rerank_backfill_candidates(
    query_text: str,
    candidate_rows: Sequence[tuple[UUID, float]],
    content_map: dict[UUID, str],
    *,
    settings,
    log_context: str = "",
) -> list[tuple[UUID, float]]:
    """Rerank F040 backfill candidates via the F042 cross-encoder.

    Returns survivors in CE-ranked order with sigmoid-normalized scores
    replacing the input RRF scores. Short-circuits (returning
    ``list(candidate_rows)`` unchanged) when:

    * ``settings.ce_backfill_enabled`` is False,
    * ``CROSS_ENCODER_AVAILABLE`` is False (``sentence-transformers``
      not installed),
    * ``candidate_rows`` is empty,
    * ``query_text`` is empty.

    Candidates whose content is missing from ``content_map`` (for
    cross-type the map is already filtered; for same-type any row whose
    content was NULL/whitespace in ``fetch_candidate_content`` is gone)
    are dropped up front. Remaining wrapped candidates are fed to the
    F042 reranker with ``max_candidates = ce_backfill_top_k`` (no
    doubling — F042 plan P2-1 fix: ``hybrid_search`` already caps at 10
    and the head is sliced to top-K anyway).

    Survivors are walked in sigmoid-DESC order and kept until the first
    ``score < ce_backfill_min_score`` — the stable sort inside F042
    guarantees that break is correct.

    NOTE: The F042 reranker short-circuits on ``len(candidates) <= 1``,
    so a lone survivor bypasses ``min_score``. The downstream cosine
    gate in ``graph_densifier._backfill_same_type`` /
    ``_backfill_cross_type`` is the correctness floor for that case.
    """
    if (
        not settings.ce_backfill_enabled
        or not CROSS_ENCODER_AVAILABLE
        or not candidate_rows
        or not query_text
    ):
        return list(candidate_rows)

    # F045: content-length guard. Facts that are literally just a URL or
    # short boilerplate string (<min_chars after strip) are dropped BEFORE
    # CE inference. Empirically, URL-only facts co-score highly on shared
    # token shape with no semantic signal — the 2026-04-14 A/B experiment
    # found this to be the dominant failure mode for the CE pipeline.
    min_chars = int(getattr(settings, "ce_backfill_min_content_chars", 80))

    # Wrap only rows with non-empty content. Rows missing from content_map
    # (e.g. NULL/whitespace-only content filtered by fetch_candidate_content)
    # are dropped entirely. This is a no-op for cross-type callers whose
    # content_map is already filtered — documented so it isn't removed as
    # dead code later.
    wrapped: list[RerankCandidate] = []
    for cand_id, rrf in candidate_rows:
        content = content_map.get(cand_id, "")
        if not content:
            continue
        if len(content.strip()) < min_chars:
            continue  # F045: drop URL-only / boilerplate facts
        wrapped.append(
            RerankCandidate(id=cand_id, content=content, score=float(rrf))
        )

    if not wrapped:
        return []

    top_k = max(int(getattr(settings, "ce_backfill_top_k", 10)), 1)
    min_score = float(getattr(settings, "ce_backfill_min_score", 0.30))

    reranked = await cross_encoder_rerank(
        query=query_text,
        candidates=wrapped,
        text_fn=lambda c: c.content,
        model_name=settings.cross_encoder_model,
        max_candidates=top_k,  # NO doubling — see plan P2-1 fix
        text_limit=settings.cross_encoder_text_limit,
    )

    kept: list[tuple[UUID, float]] = []
    for c in reranked[:top_k]:
        # Head is sigmoid-DESC, so break on first below-floor entry.
        if c.score < min_score:
            break
        kept.append((c.id, c.score))

    pruned = len(wrapped) - len(kept)
    if pruned > 0:
        logger.debug(
            "CE backfill rerank: kept=%d pruned=%d ctx=%s",
            len(kept),
            pruned,
            log_context,
        )
    return kept
