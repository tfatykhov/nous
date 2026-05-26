"""F069: structure-aware document chunker.

Unlike F067's :func:`nous.heart.chunking.chunk_text` (fixed 600-char window,
dialogue-shaped), this chunker is intended for full document bodies — arxiv
papers, doc pages, long markdown / text files — that the agent ingests via
the ``ingest_document`` tool after parsing them client-side (e.g. via
``run_python`` with ``pypdf`` / ``python-docx``).

Two differences from the dialogue chunker matter:

1. **Bigger target window.** ~1500 chars (~250 words) preserves paragraph-
   sized units of meaning. Dialogue's 600 chars slices academic prose
   mid-thought; 1500 is the standard size used by langchain / llamaindex
   recursive splitters for paper-shaped content.

2. **Recursive split on natural boundaries.** The algorithm walks a
   delimiter hierarchy (``\\n\\n`` → ``\\n`` → ``. `` → `` ``) and prefers
   to split at the highest-priority boundary that keeps every emitted
   chunk under ``target_size``. This avoids cutting in the middle of a
   sentence when a paragraph break would do.

Deterministic: same input + same parameters → same chunks. The function is
pure — no DB, no embedding, no model calls. Persistence lives in the
``ingest_document`` tool handler, which feeds these chunks into
``heart.episode_chunks`` with ``source_kind='document'``.

The chunker does NOT try to be clever about section headers (``## Methods``)
or citations. That belongs in a v2 / Phase 2 alongside source-format-aware
extraction — out of scope for this ship.
"""
from __future__ import annotations

# Delimiter hierarchy, most-preferred first. Same hierarchy used by
# langchain's RecursiveCharacterTextSplitter; kept short to avoid the
# combinatorial blow-up of trying every separator at every level.
_DEFAULT_DELIMITERS: tuple[str, ...] = ("\n\n", "\n", ". ", " ")


def chunk_document(
    text: str,
    *,
    target_size: int = 1500,
    overlap: int = 200,
    min_chars: int = 100,
    delimiters: tuple[str, ...] = _DEFAULT_DELIMITERS,
) -> list[str]:
    """Recursively split ``text`` into document-shaped chunks.

    Each chunk is at most ``target_size`` chars (modulo the final piece
    of a paragraph that happens to be slightly shorter), and adjacent
    chunks share approximately ``overlap`` trailing chars so a fact that
    straddles a boundary surfaces in both neighbors.

    Args:
        text: The full document body. May contain ``\\n\\n`` paragraph
            breaks, markdown headings, etc. — the chunker does not strip
            or interpret structure beyond the delimiter hierarchy.
        target_size: Soft cap on chunk size in chars. Default 1500
            (~250 English words). Pieces that exceed this even after
            splitting at the finest-grained delimiter (whitespace) are
            emitted as-is — see ``_pack``.
        overlap: Trailing chars of each chunk reproduced at the head of
            the next. Default 200. Set to 0 for no overlap (e.g. tests).
        min_chars: Short-circuit threshold. ``text`` below this length
            returns ``[]`` so we do not pollute the chunk table with
            single-tweet ingests.
        delimiters: Ordered tuple of split markers. The default walks
            paragraph → line → sentence → word. Override for plain text
            without paragraphs.

    Returns:
        Ordered list of chunk strings. Empty for texts shorter than
        ``min_chars`` or empty input.

    Raises:
        ValueError: if ``target_size <= 0``, ``overlap < 0``,
        ``overlap >= target_size``, or ``delimiters`` is empty.
    """
    if not text or len(text) < min_chars:
        return []
    if target_size <= 0:
        raise ValueError(f"target_size must be positive, got {target_size}")
    if overlap < 0:
        raise ValueError(f"overlap must be non-negative, got {overlap}")
    if overlap >= target_size:
        raise ValueError(
            f"overlap ({overlap}) must be < target_size ({target_size}) — "
            f"otherwise chunks would not advance"
        )
    if not delimiters:
        raise ValueError("delimiters must be non-empty")

    # Short-circuit: text fits in a single chunk.
    if len(text) <= target_size:
        return [text]

    # Step 1: split on the highest-priority delimiter that produces at
    # least one piece smaller than target_size. We greedily peel at the
    # outermost level and only recurse into pieces that are still too big.
    pieces = _recursive_split(text, list(delimiters), target_size)

    # Step 2: pack consecutive small pieces into target_size chunks with
    # overlap. Single oversized pieces (longer than target_size even
    # after recursive splitting hit the last delimiter) are emitted as
    # standalone chunks — better to keep them whole than slice arbitrarily.
    return _pack(pieces, target_size=target_size, overlap=overlap)


def _recursive_split(
    text: str, delimiters: list[str], target_size: int,
) -> list[str]:
    """Walk the delimiter hierarchy until every piece <= target_size.

    Returns a flat list of pieces; their concatenation (after re-joining
    with the discarded delimiters' inferred presence) is *not* exactly
    ``text``, because the splitter does not re-insert the separator
    characters. The packer downstream handles that by reading ``text``
    boundaries when overlap is applied.
    """
    if len(text) <= target_size or not delimiters:
        return [text]

    head, *rest = delimiters
    parts = text.split(head)
    out: list[str] = []
    for part in parts:
        if not part:
            continue
        if len(part) <= target_size:
            out.append(part)
        else:
            out.extend(_recursive_split(part, rest, target_size))
    return out


def _pack(pieces: list[str], *, target_size: int, overlap: int) -> list[str]:
    """Pack pieces into chunks of approximately ``target_size`` chars.

    Joins consecutive pieces with ``" "`` (a single space — deliberately
    lossy, since the recursive splitter discarded the original delimiters).
    Pieces that exceed target_size on their own get emitted standalone.
    Overlap is realized by carrying the trailing ``overlap`` chars of the
    last emitted chunk into the head of the next.
    """
    chunks: list[str] = []
    buf = ""
    for piece in pieces:
        # Oversized standalone piece — emit current buffer (if any) then
        # the piece directly. We do NOT seed buf from its overlap tail:
        # an oversized atom is already preserved verbatim, and the next
        # piece (if any) starts fresh. This avoids emitting a leftover
        # overlap-only chunk when an oversized atom is the final piece.
        if len(piece) > target_size:
            if buf:
                chunks.append(buf)
            chunks.append(piece)
            buf = ""
            continue

        candidate = piece if not buf else f"{buf} {piece}"
        if len(candidate) <= target_size:
            buf = candidate
        else:
            # Flush buf; start a new one seeded with the overlap tail of
            # the just-emitted chunk so the next chunk shares vocabulary
            # with this one across the boundary.
            chunks.append(buf)
            tail = _overlap_tail(buf, overlap)
            buf = f"{tail} {piece}" if tail else piece
    if buf:
        chunks.append(buf)
    return chunks


def _overlap_tail(text: str, overlap: int) -> str:
    """Return the trailing ``overlap`` chars of ``text``, snapped to the
    nearest preceding whitespace so we do not start the next chunk
    mid-word. Returns ``""`` for ``overlap == 0`` or empty text."""
    if overlap <= 0 or not text:
        return ""
    tail = text[-overlap:]
    # Snap forward to first whitespace to avoid mid-word starts.
    space_idx = tail.find(" ")
    if space_idx == -1:
        return tail
    return tail[space_idx + 1:]
