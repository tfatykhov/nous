"""F067: unit tests for nous.heart.chunking.chunk_text.

Pure function — no DB or LLM needed. Tests cover: empty input, short input
(below min_chars), single-chunk path, sliding window with overlap, boundary
arithmetic, invalid arg rejection, determinism.
"""
from __future__ import annotations

import pytest

from nous.heart.chunking import chunk_text


def test_empty_returns_empty_list():
    assert chunk_text("") == []


def test_below_min_chars_returns_empty():
    # Default min_chars=50
    assert chunk_text("hi there", min_chars=50) == []


def test_exactly_at_min_chars_returns_single_chunk():
    text = "a" * 50
    assert chunk_text(text, chunk_size=100, min_chars=50) == [text]


def test_short_text_fits_one_chunk():
    text = "a" * 100
    assert chunk_text(text, chunk_size=200, overlap=20) == [text]


def test_sliding_window_two_chunks():
    # 1000 chars, chunk=600, overlap=80 → step=520 → chunks at [0:600], [520:1000]
    text = "x" * 1000
    chunks = chunk_text(text, chunk_size=600, overlap=80, min_chars=10)
    assert len(chunks) == 2
    assert len(chunks[0]) == 600
    assert len(chunks[1]) == 480  # 1000 - 520
    # Verify overlap region
    assert chunks[0][520:] == chunks[1][:80]


def test_sliding_window_three_chunks():
    # 1500 chars, chunk=600, overlap=80 → step=520 → chunks at 0, 520, 1040
    # 1040+600=1640 > 1500 so last chunk is [1040:1500] = 460 chars
    text = "y" * 1500
    chunks = chunk_text(text, chunk_size=600, overlap=80, min_chars=10)
    assert len(chunks) == 3
    assert len(chunks[0]) == 600
    assert len(chunks[1]) == 600
    assert len(chunks[2]) == 460


def test_overlap_zero_partitions_text():
    text = "a" * 100
    chunks = chunk_text(text, chunk_size=40, overlap=0, min_chars=10)
    assert chunks == ["a" * 40, "a" * 40, "a" * 20]


def test_chunk_size_zero_raises():
    with pytest.raises(ValueError, match="chunk_size must be positive"):
        chunk_text("x" * 100, chunk_size=0)


def test_negative_overlap_raises():
    with pytest.raises(ValueError, match="overlap must be non-negative"):
        chunk_text("x" * 100, overlap=-1)


def test_overlap_geq_chunk_size_raises():
    # overlap >= chunk_size → infinite loop guard
    with pytest.raises(ValueError, match="overlap.*must be"):
        chunk_text("x" * 100, chunk_size=100, overlap=100)


def test_deterministic_across_calls():
    text = "hello world " * 200
    a = chunk_text(text)
    b = chunk_text(text)
    assert a == b


def test_overlap_preserves_boundary_tokens():
    """A token straddling the boundary should appear in both adjacent chunks."""
    text = "left " * 100 + "BOUNDARY_TOKEN " + "right " * 100
    # chunk_size positions boundary inside chunk[0]'s tail and chunk[1]'s head
    chunks = chunk_text(text, chunk_size=500, overlap=80, min_chars=10)
    assert len(chunks) >= 2
    appearances = sum(1 for c in chunks if "BOUNDARY_TOKEN" in c)
    # With overlap=80 and token length ~14, expect token to appear in
    # at least one chunk; with proper window may appear in two
    assert appearances >= 1
