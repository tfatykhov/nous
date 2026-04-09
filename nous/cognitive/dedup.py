"""Conversation deduplication for context assembly.

Checks retrieved memories against recent conversation to avoid
injecting redundant information the user already said.

F2: Uses embed_batch() for all embeddings in one API call.
F12: try/except around embedding path, falls back to keyword overlap.
F22: Keyword overlap inlined (no lazy import from usage_tracker).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from nous.brain.embeddings import EmbeddingProvider

logger = logging.getLogger(__name__)


@dataclass
class DeduplicationResult:
    """Result of dedup check for a single memory item."""

    memory_id: str
    content: str
    max_similarity: float  # Highest similarity against conversation messages
    is_redundant: bool  # True if similarity > threshold


class ConversationDeduplicator:
    """Check retrieved memories against recent conversation to avoid redundancy.

    If a retrieved memory is >85% similar (embedding) or >50% similar
    (keyword fallback) to something already in the conversation window,
    skip it -- the information is already present.
    """

    DEFAULT_EMBEDDING_THRESHOLD = 0.85
    DEFAULT_KEYWORD_THRESHOLD = 0.5

    def __init__(
        self,
        embedding_provider: EmbeddingProvider | None = None,
        threshold: float = DEFAULT_EMBEDDING_THRESHOLD,
    ) -> None:
        self._embeddings = embedding_provider
        self._threshold = threshold

    async def check(
        self,
        memories: list[tuple[str, str]],  # (memory_id, content)
        conversation_messages: list[str],
    ) -> list[DeduplicationResult]:
        """Check each memory against conversation messages.

        F2: Uses embed_batch() for all embeddings in one API call.
        F12: Falls back to keyword overlap on embedding failure.
        """
        if not memories or not conversation_messages:
            return [DeduplicationResult(mid, content, 0.0, False) for mid, content in memories]

        # Try embedding-based dedup first
        if self._embeddings is not None:
            try:
                return await self._check_with_embeddings(memories, conversation_messages)
            except Exception:
                logger.warning("Embedding dedup failed, falling back to keyword overlap")

        # Fallback: keyword overlap (F12, F22)
        return self._check_with_keywords(memories, conversation_messages)

    async def _check_with_embeddings(
        self,
        memories: list[tuple[str, str]],
        conversation_messages: list[str],
    ) -> list[DeduplicationResult]:
        """Embedding-based dedup using embed_batch() (F2)."""
        assert self._embeddings is not None

        # F2: Batch all texts into single embed_batch() call
        all_texts = list(conversation_messages) + [content for _, content in memories]
        all_embeddings = await self._embeddings.embed_batch(all_texts)

        # Split results
        n_conv = len(conversation_messages)
        conv_embeddings = all_embeddings[:n_conv]
        mem_embeddings = all_embeddings[n_conv:]

        results = []
        for i, (mid, content) in enumerate(memories):
            mem_emb = mem_embeddings[i]
            max_sim = max(self._cosine_similarity(mem_emb, ce) for ce in conv_embeddings)
            results.append(DeduplicationResult(mid, content, max_sim, max_sim > self._threshold))
        return results

    def _check_with_keywords(
        self,
        memories: list[tuple[str, str]],
        conversation_messages: list[str],
    ) -> list[DeduplicationResult]:
        """Keyword-based dedup fallback (F22: inlined, no lazy import)."""
        results = []
        for mid, content in memories:
            max_overlap = max(self._keyword_overlap(content, msg) for msg in conversation_messages)
            results.append(
                DeduplicationResult(
                    mid,
                    content,
                    max_overlap,
                    max_overlap > self.DEFAULT_KEYWORD_THRESHOLD,
                )
            )
        return results

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        """Cosine similarity between two vectors."""
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = sum(x * x for x in a) ** 0.5
        norm_b = sum(x * x for x in b) ** 0.5
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    @staticmethod
    def _keyword_overlap(text_a: str, text_b: str) -> float:
        """Keyword overlap using containment coefficient.

        F22: Inlined to avoid circular import with usage_tracker.
        Uses same containment metric as UsageTracker.compute_overlap (F9).
        """
        words_a = set(re.findall(r"\b\w{3,}\b", text_a.lower()))
        words_b = set(re.findall(r"\b\w{3,}\b", text_b.lower()))
        if not words_a or not words_b:
            return 0.0
        intersection = words_a & words_b
        return len(intersection) / min(len(words_a), len(words_b))
