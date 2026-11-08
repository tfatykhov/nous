"""F042: Cross-encoder reranking module.

Optional reranking stage used by Heart._recall() between RRF merge and MMR
diversity selection. Loads a sentence-transformers CrossEncoder lazily, runs
prediction in a worker thread (to avoid blocking the event loop), applies
sigmoid normalization to the raw logits, and mutates RecallResult.score in
place on the head of the candidate list.

Graceful degradation: if `sentence-transformers` is not installed the module
still imports successfully (CROSS_ENCODER_AVAILABLE=False) and callers should
short-circuit before calling cross_encoder_rerank.

F108: with a recorder set (`set_recorder`), each rerank that runs the model is
counted in its minute's `rerank` bucket: its time, and `error` when the
model failed to load or predict. A rerank has no tokens, so the bucket's
token count stays NULL (the candidate count is not recorded).
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from functools import lru_cache
from typing import TYPE_CHECKING, Callable, TypeVar

if TYPE_CHECKING:
    from nous.observability.model_calls import ModelCallRecorder

logger = logging.getLogger(__name__)

# F108: the recorder each rerank reports to (set_recorder, from main.py); None records nothing.
_recorder: ModelCallRecorder | None = None

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


def set_recorder(recorder: ModelCallRecorder | None) -> None:
    """Count each rerank from now on in ``recorder``'s per-minute ``rerank`` bucket; None stops."""
    global _recorder
    _recorder = recorder


def _record(model_name: str, started: float, status: str) -> None:
    """One rerank into its bucket: the time since ``started``, no tokens. Never raises (``record_bucket`` does
    not)."""
    recorder = _recorder
    if recorder is not None:
        recorder.record_bucket(
            purpose="rerank",
            model=model_name,
            tokens=None,
            duration_ms=(time.perf_counter() - started) * 1000,
            status=status,
        )


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
        Whenever nothing is scored, the same list object comes back, so a
        caller can tell a scored result (a new list) by identity.
      - A lone candidate is scored too (its sigmoid), not passed through.

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
    if not CROSS_ENCODER_AVAILABLE or not query or not candidates:
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
    started = time.perf_counter()
    try:
        model = _load_cross_encoder(model_name)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Cross-encoder load failed for model %s: %s — returning unranked",
            model_name,
            exc,
        )
        _record(model_name, started, "error")
        return candidates

    # Run prediction in a thread to avoid blocking the event loop.
    try:
        scores = await asyncio.to_thread(model.predict, pairs)
    except asyncio.CancelledError:
        _record(model_name, started, "cancelled")
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Cross-encoder predict failed: %s — returning unranked",
            exc,
        )
        _record(model_name, started, "error")
        return candidates
    _record(model_name, started, "ok")

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
