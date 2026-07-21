"""F086 ICL exemplar mode tests."""

from nous.heart.exemplars import (
    ExemplarPair,  # noqa: F401 -- imported to assert the exported type exists
    exemplar_density,
    is_exemplar_stream,
    parse_exemplars,
    parse_label,
)

PURE_STREAM = "how do I reset my card pin\nlabel: 21\nmy card is lost\nlabel: 41\nwhat's the exchange rate\nlabel: 32\n"
TRANSCRIPT_STREAM = (
    "User: how do I reset my card pin\nlabel: 21\n"
    "Assistant: Noted.\n"
    "User: my card is lost\nlabel: 41\n"
    "Assistant: Stored.\n"
    "User: what's the exchange rate\nlabel: 32\n"
)


class TestExemplarParser:
    def test_density_pure_stream_is_high(self):
        assert exemplar_density(PURE_STREAM) >= 0.9

    def test_density_prose_is_zero(self):
        prose = "\n".join(f"This is ordinary sentence number {i}." for i in range(10))
        assert exemplar_density(prose) == 0.0

    def test_density_short_input_is_zero(self):
        assert exemplar_density("hello\nlabel: 1\n") == 0.0  # < 3 pairs

    def test_is_exemplar_stream_threshold(self):
        assert is_exemplar_stream(PURE_STREAM, threshold=0.8)
        assert not is_exemplar_stream("just chatting about the weather today", threshold=0.8)

    def test_parse_pure_stream(self):
        pairs = parse_exemplars(PURE_STREAM)
        assert [p.label for p in pairs] == ["21", "41", "32"]
        assert pairs[0].text == "how do I reset my card pin"
        assert [p.ordinal for p in pairs] == [0, 1, 2]

    def test_parse_transcript_skips_assistant_and_strips_user_prefix(self):
        pairs = parse_exemplars(TRANSCRIPT_STREAM)
        assert [p.label for p in pairs] == ["21", "41", "32"]
        assert pairs[1].text == "my card is lost"  # no "User: " prefix

    def test_parse_multiline_utterance(self):
        s = "line one\nline two of same utterance\nlabel: 7\nnext utt\nlabel: 8\n"
        pairs = parse_exemplars(s)
        assert pairs[0].text == "line one\nline two of same utterance"
        assert pairs[0].label == "7"

    def test_parse_skips_empty_utterance(self):
        s = "label: 5\nreal utterance\nlabel: 6\n"
        pairs = parse_exemplars(s)
        assert len(pairs) == 1 and pairs[0].label == "6"

    def test_parse_label_from_content(self):
        assert parse_label("some utterance\nlabel: 42") == "42"
        assert parse_label("no label here") is None
        assert parse_label("text\nlabel: atm_support") == "atm_support"
