"""F042: Cross-encoder reranking module.

Optional reranking stage used by Heart._recall() between RRF merge and MMR
diversity selection. Loads a sentence-transformers CrossEncoder lazily, runs
prediction in a worker thread (to avoid blocking the event loop), applies
sigmoid normalization to the raw logits, and mutates RecallResult.score in
place on the head of the candidate list.

Graceful degradation: if `sentence-transformers` is not installed the module
still imports successfully (CROSS_ENCODER_AVAILABLE=False) and callers should
short-circuit before calling cross_encoder_rerank.
"""

from __future__ import annotations

import asyncio
import logging
import math
from functools import lru_cache
from typing import Callable, TypeVar

logger = logging.getLogger(__name__)

try:
    from sentence_transformers import CrossEncoder  # noqa: F401

    CROSS_ENCODER_AVAILABLE = True
except ImportError:
    CROSS_ENCODER_AVAILABLE = False
    logger.warning(
        "sentence-transformers not installed — F042 cross-encoder reranking disabled. "
        "Install via `pip install 'nous[rerank]'` to enable."
    )

T = TypeVar("T")


@lru_cache(maxsize=1)
def _load_cross_encoder(model_name: str):
    """Lazily load and cache a CrossEncoder model.

    Imported inside the function (not at module top) so the one-time import
    cost is paid only on first use. Re-import is effectively free once the
    module is in sys.modules.

    Raises ImportError if sentence-transformers is not installed; callers
    MUST short-circuit on CROSS_ENCODER_AVAILABLE=False before calling this.
    """
    from sentence_transformers import CrossEncoder  # lazy

    logger.info("Loading cross-encoder model: %s", model_name)
    model = CrossEncoder(model_name)
    logger.info("Cross-encoder model loaded: %s", model_name)
    return model


def _sigmoid(x: float) -> float:
    """Numerically safe sigmoid with overflow guards."""
    if x > 50.0:
        x = 50.0
    elif x < -50.0:
        x = -50.0
    return 1.0 / (1.0 + math.exp(-x))


async def cross_encoder_rerank(
    query: str,
    candidates: list,
    text_fn: Callable[[object], str],
    *,
    model_name: str,
    max_candidates: int,
    text_limit: int,
) -> list:
    """Rerank the head of `candidates` with a cross-encoder model.

    Semantics:
      - Mutates `.score` in place on the first `max_candidates` items (head).
      - Returns `head + tail` where the head is sigmoid-sorted DESC and the
        tail is the untouched suffix `candidates[max_candidates:]`.
      - Candidates whose `text_fn(c)` returns an empty string receive
        `.score = float("-inf")` so they sink to the end of the head.
      - On ANY exception (missing dep, load failure, predict failure) the
        original `candidates` list is returned unchanged — never raises.

    Args:
        query: The retrieval query string.
        candidates: List of objects with a mutable `.score` attribute.
        text_fn: Callable that extracts the text-to-rerank from one candidate.
        model_name: sentence-transformers CrossEncoder model id.
        max_candidates: Head length — only the first N are reranked.
        text_limit: Max characters per candidate text.

    Returns:
        The reranked list (same list objects, possibly reordered).
    """
    if not CROSS_ENCODER_AVAILABLE or not query or len(candidates) <= 1:
        return candidates

    # P3: cap query length too (defensive against pathologically long queries).
    capped_query = query[:text_limit]

    head = candidates[:max_candidates]
    tail = candidates[max_candidates:]

    # Build pairs only for candidates with non-empty text.
    pairs: list[tuple[str, str]] = []
    pair_indices: list[int] = []  # index within `head`
    empty_indices: list[int] = []

    for i, c in enumerate(head):
        text = (text_fn(c) or "")[:text_limit]
        if text:
            pairs.append((capped_query, text))
            pair_indices.append(i)
        else:
            empty_indices.append(i)

    if not pairs:
        # Nothing rerankable; return candidates unchanged.
        return candidates

    # Load model (lazy, cached).
    try:
        model = _load_cross_encoder(model_name)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Cross-encoder load failed for model %s: %s — returning unranked",
            model_name,
            exc,
        )
        return candidates

    # Run prediction in a thread to avoid blocking the event loop.
    try:
        scores = await asyncio.to_thread(model.predict, pairs)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Cross-encoder predict failed: %s — returning unranked",
            exc,
        )
        return candidates

    # Mutate head scores in place.
    for pair_idx, raw_score in zip(pair_indices, scores):
        try:
            raw_f = float(raw_score)
            sig = _sigmoid(raw_f)
        except (TypeError, ValueError):
            raw_f = 0.0
            sig = 0.0
        head[pair_idx].score = sig
        logger.debug(
            "CE rerank idx=%d raw=%.4f sigmoid=%.4f",
            pair_idx,
            raw_f,
            sig,
        )

    for empty_idx in empty_indices:
        head[empty_idx].score = float("-inf")

    # Stable sort DESC — Python's sort is stable so tied scores preserve
    # original (RRF) input order.
    head.sort(key=lambda c: c.score, reverse=True)

    return head + tail
