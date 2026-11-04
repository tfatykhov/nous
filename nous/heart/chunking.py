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

H-7 adds ``chunk_transcript``, a turn-aware chunker for the stored
``User: …`` / ``Assistant: …`` transcript, used for NEW episodes when
``settings.episode_chunk_turn_aware`` is on. ``chunk_text`` is unchanged: it
stays the default and the document chunker's helpers do not use either.
"""
from __future__ import annotations

import re
from bisect import bisect_right


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


# A turn starts at the start of the text or after a blank line, with a role label.
# Case-insensitive: prod writes "User:"/"Assistant:", the LongMemEval ingest writes
# lowercase roles. A blank line NOT followed by a label is a paragraph inside a turn.
_TURN_START = re.compile(r"(?:^|(?<=\n\n))(user|assistant): ", re.IGNORECASE)
# Cut points, best first: a turn boundary, a blank line, a sentence end, any
# whitespace. Each match's start is where the chunk ends.
_CUT_LEVELS = (
    re.compile(r"\n\n(?=(?:user|assistant): )", re.IGNORECASE),
    re.compile(r"\n\n"),
    re.compile(r"(?<=[.!?])\s"),
    re.compile(r"\s"),
)
_LONGEST_LABEL = len("assistant: ")
# Below this a window holds too little besides a label for the cut rules to hold
# (the hard-cut fallback can run out of allowed cut points). Settings bounds
# episode_chunk_size at the same value.
MIN_TURN_AWARE_CHUNK_SIZE = 4 * _LONGEST_LABEL


def chunk_transcript(
    text: str,
    *,
    chunk_size: int = 600,
    overlap: int = 80,
    min_chars: int = 50,
) -> list[str]:
    """Split a ``User:``/``Assistant:`` transcript into chunks along turns (H-7).

    Each chunk is at most ``chunk_size`` chars and ends at the best cut point
    in reach: the last turn boundary that fits (so whole turns are packed
    together), else a blank line, a sentence end, or whitespace inside the
    turn. A chunk never ends between a ``User:`` turn and the ``Assistant:``
    turn that answers it, so a question always shares a chunk with the start
    of its answer. A chunk that starts inside a turn carries the turn's role
    label (``Assistant: …``) as written in the transcript, so a fragment of a
    long answer still says whose words it holds.

    A role label is recognised only at the start of the text or after a blank
    line (``\\n\\n``), case-insensitively. So a ``User:`` or ``Assistant:``
    pasted after a blank line inside a message is taken as a turn start.

    A sentence or whitespace cut must leave the chunk at least half full;
    when the rest of the window is one unbroken run (a pasted blob), the chunk
    takes a hard cut at the window's end instead. ``overlap`` applies only to
    a hard cut: the next chunk starts ``overlap`` chars back, as
    ``chunk_text`` does. Cuts at whitespace split no token, so they need none.
    No cut falls inside a role label or right after one, so no chunk ends on a
    bare label.
    Text with no role labels falls back to the same paragraph / sentence /
    whitespace / hard cuts, with no labels added. Short texts follow
    ``chunk_text``'s contract: below ``min_chars`` → ``[]``, within
    ``chunk_size`` → ``[text]``. Deterministic. ``chunk_size`` must be at
    least ``MIN_TURN_AWARE_CHUNK_SIZE`` (4 role labels, 44 chars): below it
    the cut rules cannot all hold, so a smaller size raises ``ValueError``.
    """
    if not text or len(text) < min_chars:
        return []
    if chunk_size < MIN_TURN_AWARE_CHUNK_SIZE:
        raise ValueError(f"chunk_size must be at least {MIN_TURN_AWARE_CHUNK_SIZE}, got {chunk_size}")
    if overlap < 0:
        raise ValueError(f"overlap must be non-negative, got {overlap}")
    if overlap >= chunk_size:
        raise ValueError(f"overlap ({overlap}) must be < chunk_size ({chunk_size})")
    if len(text) <= chunk_size:
        return [text]
    return [label + text[a:b] for a, b, label in _transcript_spans(text, chunk_size, overlap)]


def _transcript_spans(text: str, chunk_size: int, overlap: int) -> list[tuple[int, int, str]]:
    """``(start, end, label)`` per chunk: the chunk is ``label + text[start:end]``.

    ``label`` is empty at a turn start and before the first turn.
    """
    turns = [(m.start(), m.group(1).lower(), m.group(0)) for m in _TURN_START.finditer(text)]
    starts = [t[0] for t in turns]
    # An answer's start: cutting so that the next chunk begins here would leave
    # the question it answers at the end of the previous chunk.
    answer_starts = {
        start for (start, role, _), (_, prev_role, _) in zip(turns[1:], turns)
        if role == "assistant" and prev_role == "user"
    }

    def next_start(i: int) -> int:
        while i < len(text) and text[i].isspace():
            i += 1
        return i

    def allowed(cut: int, pos: int) -> bool:
        # A cut inside a label, or after a label with nothing but whitespace since,
        # would end the chunk on a bare label.
        k = bisect_right(starts, cut) - 1
        bare_label = k >= 0 and starts[k] < cut and next_start(starts[k] + len(turns[k][2])) >= cut
        return cut > pos and not bare_label and next_start(cut) not in answer_starts

    spans: list[tuple[int, int, str]] = []
    pos = 0
    while pos < len(text):
        k = bisect_right(starts, pos) - 1
        label = "" if k < 0 or starts[k] == pos else turns[k][2]
        end = pos + chunk_size - len(label)
        if end >= len(text):
            spans.append((pos, len(text), label))
            break
        cut = None
        for depth, level in enumerate(_CUT_LEVELS):
            # A sentence or word cut that would leave the chunk less than half full
            # (a long unbroken run fills the rest of the window) loses to a hard cut.
            lowest = pos if depth < 2 else pos + (end - pos) // 2
            # Search a little past `end` so a lookahead at a cut point can see the label.
            cuts = [m.start() for m in level.finditer(text, pos, min(len(text), end + _LONGEST_LABEL + 2))]
            cut = next((c for c in reversed(cuts) if lowest <= c <= end and allowed(c, pos)), None)
            if cut is not None:
                break
        if cut is not None:
            spans.append((pos, cut, label))
            pos = next_start(cut)
            continue
        # No whitespace in reach: a hard cut, overlapped like chunk_text's windows.
        cut = end
        while cut > pos + 1 and not allowed(cut, pos):
            cut -= 1
        spans.append((pos, cut, label))
        # The rewind can land on the blank line before a turn or inside its label.
        # Start after the label instead: the loop adds it back as the prefix, so the
        # chunk text equals a start at the label, and the position still advances.
        pos = next_start(max(cut - overlap, pos + 1))
        k = bisect_right(starts, pos) - 1
        if k >= 0 and starts[k] <= pos < starts[k] + len(turns[k][2]):
            pos = starts[k] + len(turns[k][2])
    return spans
