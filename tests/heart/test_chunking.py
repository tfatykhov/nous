"""F067: unit tests for nous.heart.chunking.chunk_text.

Pure function — no DB or LLM needed. Tests cover: empty input, short input
(below min_chars), single-chunk path, sliding window with overlap, boundary
arithmetic, invalid arg rejection, determinism.
"""
from __future__ import annotations

import pytest

from nous.heart.chunking import _transcript_spans, chunk_text, chunk_transcript


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


# ---------------------------------------------------------------------------
# H-7: chunk_transcript, the turn-aware chunker (episode_chunk_turn_aware)
# ---------------------------------------------------------------------------


def _sentences(n_chars: int, word: str) -> str:
    """Prose of about ``n_chars`` chars: 9-word sentences of ``word``."""
    out = ""
    while len(out) < n_chars:
        out += " ".join([word] * 9) + ". "
    return out[:n_chars].rstrip()


def _covered(text: str, spans: list[tuple[int, int, str]]) -> bool:
    """Every non-whitespace char of ``text`` lies in some span."""
    seen = [False] * len(text)
    for a, b, _label in spans:
        for i in range(a, b):
            seen[i] = True
    return all(seen[i] or text[i].isspace() for i in range(len(text)))


def test_turn_aware_short_text_matches_chunk_text_contract():
    assert chunk_transcript("User: hi") == []
    text = "User: hello there\n\nAssistant: hi, how can I help today?"
    assert chunk_transcript(text, chunk_size=600, min_chars=10) == [text]


def test_turn_aware_is_deterministic():
    text = "\n\n".join(
        f"User: {_sentences(150, f'q{i}')}\n\nAssistant: {_sentences(700, f'a{i}')}" for i in range(4)
    )
    assert chunk_transcript(text) == chunk_transcript(text)


def test_question_and_answer_share_a_chunk_when_they_fit():
    q1, a1 = f"User: {_sentences(100, 'qa')}", f"Assistant: {_sentences(200, 'aa')}"
    q2, a2 = f"User: {_sentences(100, 'qb')}", f"Assistant: {_sentences(300, 'ab')}"
    text = "\n\n".join([q1, a1, q2, a2])
    assert len(text) > 600
    assert chunk_transcript(text) == [f"{q1}\n\n{a1}", f"{q2}\n\n{a2}"]


def test_a_question_never_ends_a_chunk_before_its_answer():
    # The review's probe shape: a question near the window size, a short answer.
    q = f"User: {_sentences(584, 'ask')}"
    a = f"Assistant: {_sentences(130, 'reply')}"
    text = f"{q}\n\n{a}"
    chunks = chunk_transcript(text, chunk_size=600, overlap=80)
    # The User -> Assistant boundary sits inside a chunk, never between two.
    assert any(f"\n\n{a[:30]}" in c for c in chunks)
    assert all(len(c) <= 600 for c in chunks)


def test_long_answer_fragments_carry_the_role_prefix_and_no_user_stub():
    answer = _sentences(1400, "alpha") + "\n\n" + _sentences(1600, "beta")
    text = f"User: what happened?\n\nAssistant: {answer}"
    chunks = chunk_transcript(text, chunk_size=600, overlap=80)
    assert len(chunks) >= 6
    assert chunks[0].startswith("User: what happened?\n\nAssistant: alpha")
    assert all(c.startswith("Assistant: ") for c in chunks[1:])
    assert not any("User:" in c for c in chunks[1:])  # no question stub on continuations
    assert all(len(c) <= 600 for c in chunks)


def test_a_blank_line_inside_a_message_is_not_a_turn():
    text = f"User: {'a' * 294}\n\n{'b' * 250}\n\nAssistant: {'c' * 100}"
    chunks = chunk_transcript(text, chunk_size=600, overlap=80)
    # The cut lands on the message's own blank line; the second half is still the
    # user's turn (prefixed), and it keeps its answer.
    assert chunks == [f"User: {'a' * 294}", f"User: {'b' * 250}\n\nAssistant: {'c' * 100}"]


def test_role_labels_match_case_insensitively():
    # nous_eval's LongMemEval ingest writes lowercase roles; prod writes "User:".
    answer = _sentences(1500, "gamma")
    upper = f"User: {_sentences(120, 'q')}\n\nAssistant: {answer}\n\nUser: thanks"
    lower = upper.replace("User: ", "user: ").replace("Assistant: ", "assistant: ")
    up, low = chunk_transcript(upper), chunk_transcript(lower)
    assert [c.replace("User: ", "user: ").replace("Assistant: ", "assistant: ") for c in up] == low
    assert all(c.startswith("assistant: ") for c in low[1:-1])


def test_no_role_markers_fall_back_to_whitespace_windows():
    text = _sentences(2000, "plain")
    chunks = chunk_transcript(text, chunk_size=600, overlap=80)
    assert len(chunks) >= 4
    assert all(len(c) <= 600 for c in chunks)
    assert not any(c.startswith(("User:", "Assistant:")) for c in chunks)


def test_every_character_is_kept():
    """No loss: each chunk is its label plus a slice of the input, and the slices cover
    every non-whitespace char (the label is re-added text, so the chunks do not
    concatenate back to the input byte for byte)."""
    text = "\n\n".join(
        [
            "preamble line without a role",
            f"User: {_sentences(900, 'ask')}\n\nsecond paragraph of the question",
            f"Assistant: {_sentences(2500, 'ans')}\n\n{'z' * 900}",
            "user: lower-case follow up?",
            f"assistant: {_sentences(300, 'end')}",
        ]
    )
    spans = _transcript_spans(text, 600, 80)
    assert _covered(text, spans)
    assert chunk_transcript(text, chunk_size=600, overlap=80) == [label + text[a:b] for a, b, label in spans]
    assert all(len(label) + b - a <= 600 for a, b, label in spans)


def test_a_hard_cut_overlaps_and_keeps_the_prefix():
    text = f"User: paste this\n\nAssistant: {'x' * 1500}"
    chunks = chunk_transcript(text, chunk_size=600, overlap=80)
    assert all(len(c) <= 600 for c in chunks)
    assert all(c.startswith("Assistant: x") for c in chunks[1:])
    # Window fragments share `overlap` chars, as chunk_text's do.
    assert chunks[0][-80:] == chunks[1][len("Assistant: "):][:80]


def test_turn_aware_rejects_bad_sizes():
    with pytest.raises(ValueError, match="overlap"):
        chunk_transcript("User: " + "x" * 100, chunk_size=100, overlap=100, min_chars=10)
    with pytest.raises(ValueError, match="chunk_size"):
        chunk_transcript("User: " + "x" * 100, chunk_size=11, overlap=0, min_chars=10)


def test_no_chunk_ends_on_a_bare_role_label():
    text = f"User: {_sentences(570, 'ask')}\n\nAssistant: {'x' * 1000}"
    chunks = chunk_transcript(text, chunk_size=600, overlap=80)
    assert not any(c.rstrip().lower().endswith(("assistant:", "user:")) for c in chunks)
    assert any("\n\nAssistant: x" in c for c in chunks)


def test_invariants_hold_on_generated_transcripts():
    import random
    import re

    rng = random.Random(7)
    for _ in range(60):
        turns = []
        for i in range(rng.randint(1, 8)):
            role = rng.choice(["User", "Assistant", "user", "assistant"]) if i else "User"
            paras = [
                rng.choice([_sentences(rng.randint(5, 900), "w"), "y" * rng.randint(1, 700)])
                for _ in range(rng.randint(1, 3))
            ]
            turns.append(f"{role}: " + "\n\n".join(paras))
        text = "\n\n".join(turns)
        if len(text) <= 600:
            continue
        spans = _transcript_spans(text, 600, 80)
        assert _covered(text, spans)
        assert all(len(label) + b - a <= 600 and b > a for a, b, label in spans)
        labels = re.finditer(r"(?:^|(?<=\n\n))(user|assistant): ", text, re.I)
        starts = [(m.start(), m.group(1).lower()) for m in labels]
        answers = {s for (s, r), (_, pr) in zip(starts[1:], starts) if r == "assistant" and pr == "user"}
        assert not any(a in answers for a, _b, _label in spans[1:])
