"""R3 (F085): canonical key normalization + entity-candidate extraction."""
import unicodedata

from nous.heart.keys import extract_entity_candidates, normalize_key


class TestNormalizeKeyV2:
    def test_underscores_become_spaces(self):
        assert normalize_key("thomas_kyd") == "thomas kyd"

    def test_leading_article_stripped(self):
        assert normalize_key("The Marriage of Figaro") == "marriage of figaro"
        assert normalize_key("a red car") == "red car"
        assert normalize_key("An Apple") == "apple"

    def test_article_strip_iterates_to_fixpoint(self):
        # single-pass stripping would return "a red car" here
        assert normalize_key("the a red car") == "red car"

    def test_bare_article_is_preserved(self):
        # the whole key IS the article -> keep it rather than return None
        assert normalize_key("the") == "the"

    def test_intra_word_hyphen_preserved(self):
        assert normalize_key("cross-encoder") == "cross-encoder"

    def test_dangling_hyphen_removed(self):
        assert normalize_key("cross -encoder") == "cross encoder"
        assert normalize_key("- leading") == "leading"

    def test_nfc_unicode(self):
        composed = "café"                                   # U+00E9
        decomposed = unicodedata.normalize("NFD", "café")   # e + U+0301
        assert normalize_key(composed) == normalize_key(decomposed) == "café"

    def test_possessive_and_punctuation(self):
        assert normalize_key("Tim's Laptop") == "tims laptop"
        assert normalize_key("  RED   Car!! ") == "red car"

    def test_empty_none(self):
        assert normalize_key(None) is None
        assert normalize_key("") is None
        assert normalize_key("   ") is None
        assert normalize_key("!!!") is None

    def test_max_len_cap(self):
        assert len(normalize_key("x" * 300)) == 200
        assert len(normalize_key("x" * 300, max_len=100)) == 100

    def test_idempotent_property(self):
        cases = [
            "The Marriage of Figaro", "thomas_kyd", "the a red car",
            "cross-encoder", "Tim's Laptop", "café",
            unicodedata.normalize("NFD", "café"),
            "the " + "ab-" * 80,   # truncation lands mid-token -> dangling hyphen
            "A  b__c--d", "-", "the the the", "an an apple",
        ]
        for raw in cases:
            once = normalize_key(raw)
            assert normalize_key(once) == once, raw
