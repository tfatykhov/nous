"""F069: tests for the document chunker.

Focus on (a) correctness invariants the dialogue chunker shares with us
(empty in -> empty out, deterministic, no chunk exceeds target_size unless
it had to), and (b) the document-specific behavior — paragraph-boundary
preference, overlap snapping to whitespace, oversized-piece passthrough.
"""
from __future__ import annotations

import pytest

from nous.heart.document_chunker import chunk_document


class TestEmptyAndShort:
    def test_empty_returns_empty(self):
        assert chunk_document("") == []

    def test_none_safe(self):
        # mirror chunk_text — None is treated as falsy/empty
        assert chunk_document(None) == []  # type: ignore[arg-type]

    def test_below_min_returns_empty(self):
        assert chunk_document("short", min_chars=100) == []

    def test_fits_in_single_chunk(self):
        text = "This is a short paragraph well under 1500 chars."
        result = chunk_document(text, target_size=1500, min_chars=10)
        assert result == [text]


class TestParameterValidation:
    def test_zero_target_size_raises(self):
        with pytest.raises(ValueError, match="target_size must be positive"):
            chunk_document("x" * 200, target_size=0)

    def test_negative_overlap_raises(self):
        with pytest.raises(ValueError, match="overlap must be non-negative"):
            chunk_document("x" * 200, target_size=100, overlap=-1)

    def test_overlap_ge_target_raises(self):
        with pytest.raises(ValueError, match="overlap.*must be <.*target_size"):
            chunk_document("x" * 200, target_size=100, overlap=100)

    def test_empty_delimiters_raises(self):
        with pytest.raises(ValueError, match="delimiters must be non-empty"):
            chunk_document("x" * 5000, delimiters=())


class TestRecursiveSplit:
    def test_paragraph_boundary_preferred(self):
        # Two clean paragraphs, both well under target.
        para_a = "A" * 400
        para_b = "B" * 400
        text = f"{para_a}\n\n{para_b}"
        # target_size=500 forces a split; the splitter should prefer the
        # \n\n boundary over slicing inside either paragraph.
        chunks = chunk_document(text, target_size=500, overlap=50)
        assert len(chunks) >= 2
        # Each chunk should contain content from only one paragraph
        # (modulo overlap tails).
        assert any("A" in c and "B" not in c for c in chunks[:1])
        assert any("B" in c for c in chunks)

    def test_long_paragraph_falls_through_to_sentence(self):
        # One long paragraph that must be split on sentences.
        sentences = ". ".join("Sentence " + ("x" * 50) for _ in range(20))
        chunks = chunk_document(sentences, target_size=500, overlap=50, min_chars=10)
        # No chunk should exceed target_size by more than a sentence.
        for c in chunks:
            assert len(c) <= 500 + 80, f"chunk too large: {len(c)}"

    def test_oversized_atomic_piece_emitted_standalone(self):
        # A single atomic "word" larger than target_size cannot be split
        # by any delimiter — should be emitted as its own chunk.
        oversized = "X" * 3000
        chunks = chunk_document(oversized, target_size=1500, overlap=100)
        assert len(chunks) == 1
        assert chunks[0] == oversized

    def test_deterministic(self):
        text = "Paragraph one. " * 200 + "\n\n" + "Paragraph two. " * 200
        a = chunk_document(text, target_size=1500, overlap=200)
        b = chunk_document(text, target_size=1500, overlap=200)
        assert a == b


class TestChunkSizeContract:
    """Codex P2 regression: every chunk under target_size, including
    cases where overlap-tail seeding would otherwise push the new buffer
    above the limit. The size contract is hard; overlap is best-effort."""

    def test_overlap_tail_does_not_violate_size_contract(self):
        # Construct pieces so the second piece + overlap tail of the first
        # chunk would exceed target_size if not re-checked.
        # 92-char piece + 30-char tail + 1 space would be 123 > 100 target.
        target_size = 100
        overlap = 30
        # First long piece flushes via paragraph break; second is ~90 chars.
        first = ("aaa " * 25).strip()   # 99 chars
        second = ("bbb " * 25).strip()  # 99 chars (similar)
        text = f"{first}\n\n{second}"

        chunks = chunk_document(
            text,
            target_size=target_size,
            overlap=overlap,
            min_chars=10,
        )

        for c in chunks:
            assert len(c) <= target_size, (
                f"chunk size {len(c)} > target_size {target_size}: {c[:60]!r}"
            )


class TestOverlap:
    def test_overlap_zero_no_shared_content(self):
        text = (
            "Alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha. "
            "Beta beta beta beta beta beta beta beta beta beta beta beta. "
            "Gamma gamma gamma gamma gamma gamma gamma gamma gamma gamma. "
            "Delta delta delta delta delta delta delta delta delta delta. "
        ) * 4
        chunks = chunk_document(text, target_size=150, overlap=0, min_chars=10)
        # With zero overlap and big chunks, consecutive chunks should
        # not share content prefixes.
        if len(chunks) >= 2:
            assert not chunks[1].startswith(chunks[0][-20:])

    def test_overlap_snaps_to_word_boundary(self):
        # Build a chunk whose tail ends mid-word. The overlap tail should
        # snap to the next whitespace so the next chunk starts on a word.
        text = ("word " * 500).strip()  # 500 words = 2495 chars
        chunks = chunk_document(text, target_size=600, overlap=80, min_chars=10)
        assert len(chunks) >= 2
        # No chunk should start with a partial token (i.e. with non-space
        # char immediately after the join "tail space piece").
        for c in chunks[1:]:
            # Acceptable starts: a complete "word" or whitespace.
            assert c.startswith("word") or c.startswith(" "), (
                f"chunk starts mid-token: {c[:30]!r}"
            )


class TestRealisticDocument:
    def test_arxiv_paper_shape(self):
        # Synthetic arxiv-shaped doc: abstract, intro, methods, results.
        sections = []
        for section in ("Abstract", "Introduction", "Methods", "Results"):
            body = (f"{section} body. " * 80).strip()  # ~960 chars / section
            sections.append(f"## {section}\n\n{body}")
        text = "\n\n".join(sections)

        chunks = chunk_document(text, target_size=1500, overlap=200, min_chars=100)

        # Should produce several chunks but not absurdly many for ~4K chars.
        assert 2 <= len(chunks) <= 6, f"unexpected chunk count: {len(chunks)}"

        # Total content should be approximately preserved (overlap means
        # some duplication; allow up to 50% inflation).
        joined = " ".join(chunks)
        assert len(joined) >= len(text), "lost content"
        assert len(joined) <= int(len(text) * 1.5), f"too much overlap inflation: {len(joined)} vs {len(text)}"
