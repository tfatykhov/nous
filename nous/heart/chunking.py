"""F067: transcript chunking utility.

Pure functions for splitting an episode transcript into searchable chunks.
Deterministic — same input always produces the same chunks. Used by the
EpisodeSummarizer ingest hook when ``settings.episode_chunks_enabled`` is
set.

Design notes:
- Char-based sliding window (not sentence-aware) for deterministic output
  and trivial unit-testing. A future revision could use a smarter splitter
  but the prototype validation used this exact approach.
- Overlap prevents key tokens from being split across chunk boundaries
  (e.g. "M-emrise" if the boundary lands inside the word).
- Texts below ``min_chars`` return an empty list — empty/short episodes
  shouldn't pollute the chunk table.
"""
from __future__ import annotations


def chunk_text(
    text: str,
    *,
    chunk_size: int = 600,
    overlap: int = 80,
    min_chars: int = 50,
) -> list[str]:
    """Split ``text`` into overlapping chunks.

    Returns a list of chunks each at most ``chunk_size`` chars. Adjacent
    chunks share ``overlap`` chars so tokens straddling a boundary appear
    in both neighbors. Texts shorter than ``min_chars`` return ``[]``.

    Args:
        text: The source text (typically an episode transcript).
        chunk_size: Max chars per chunk. Default 600 matches the F067
            prototype validation (gbrain-evals uses 500-700 range).
        overlap: Chars shared between adjacent chunks. Default 80.
        min_chars: Skip texts shorter than this. Default 50.

    Returns:
        Ordered list of chunk strings. Empty for short / empty input.
    """
    if not text or len(text) < min_chars:
        return []
    if chunk_size <= 0:
        raise ValueError(f"chunk_size must be positive, got {chunk_size}")
    if overlap < 0:
        raise ValueError(f"overlap must be non-negative, got {overlap}")
    if overlap >= chunk_size:
        raise ValueError(
            f"overlap ({overlap}) must be < chunk_size ({chunk_size}) — "
            f"otherwise the window does not advance"
        )

    # Short-circuit: text fits in a single chunk.
    if len(text) <= chunk_size:
        return [text]

    chunks: list[str] = []
    start = 0
    step = chunk_size - overlap
    while start < len(text):
        end = min(start + chunk_size, len(text))
        chunks.append(text[start:end])
        if end >= len(text):
            break
        start += step
    return chunks
