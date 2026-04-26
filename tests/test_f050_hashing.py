"""F050 — canonical_input_hash unit tests.

Helper at ``nous.heart.hashing.canonical_input_hash`` is shared by:
- F050 ``heart.query_expansions.input_hash``
- F047 Phase 3 (planned) ``classifier_input_hash``

Canonicalization order (must stay stable across releases):
  1. NFKC Unicode normalization
  2. lowercase
  3. strip leading/trailing whitespace

The NFKC step (added in plan v2 — devil P1) defends against:
- NFC vs NFD cache misses (visually identical chars, different bytes)
- ZWS / NBSP / bidi chars slipping past .strip()
- Compatibility decompositions (½ → 1/2, ﬁ → fi)
"""

from __future__ import annotations

import hashlib
import unicodedata

import pytest

from nous.heart.hashing import canonical_input_hash


def _expected(text: str) -> bytes:
    """Reference implementation — must match the helper exactly."""
    canonical = unicodedata.normalize("NFKC", text).lower().strip()
    return hashlib.sha256(canonical.encode("utf-8")).digest()


class TestCanonicalInputHashBasic:
    def test_canonical_input_hash_basic(self) -> None:
        """SHA-256 of 'hello world' (NFKC + lower + strip) — known digest."""
        result = canonical_input_hash("hello world")
        # Reference: sha256("hello world") since NFKC/lower/strip are no-ops here
        assert result == hashlib.sha256(b"hello world").digest()

    def test_canonical_input_hash_returns_32_bytes(self) -> None:
        """SHA-256 always produces exactly 32 raw bytes."""
        for text in ("", "x", "hello", "a" * 1000, "unicode: ñ é ü ﬁ"):
            assert len(canonical_input_hash(text)) == 32

    def test_canonical_input_hash_deterministic(self) -> None:
        """Same input always produces the same output (no salt, no time)."""
        text = "deterministic query for caching"
        first = canonical_input_hash(text)
        for _ in range(5):
            assert canonical_input_hash(text) == first


class TestCanonicalInputHashLowercase:
    def test_canonical_input_hash_lowercase(self) -> None:
        """'HELLO' and 'hello' must collide — case-insensitive cache."""
        assert canonical_input_hash("HELLO") == canonical_input_hash("hello")

    def test_canonical_input_hash_mixed_case(self) -> None:
        """'TiMeOuT BuG' collides with 'timeout bug'."""
        assert canonical_input_hash("TiMeOuT BuG") == canonical_input_hash("timeout bug")


class TestCanonicalInputHashWhitespace:
    def test_canonical_input_hash_strips_whitespace(self) -> None:
        """Leading/trailing whitespace must be stripped before hashing."""
        assert canonical_input_hash("  q  ") == canonical_input_hash("q")

    def test_canonical_input_hash_strips_tabs_newlines(self) -> None:
        """\\t and \\n at edges are stripped (default Python .strip())."""
        assert canonical_input_hash("\tquery\n") == canonical_input_hash("query")

    def test_canonical_input_hash_preserves_internal_whitespace(self) -> None:
        """'foo bar' and 'foobar' must NOT collide — internal spaces matter."""
        assert canonical_input_hash("foo bar") != canonical_input_hash("foobar")


class TestCanonicalInputHashNFKC:
    """NFKC normalization — plan v2 devil P1."""

    def test_canonical_input_hash_nfkc_normalizes_compat_decomposition(self) -> None:
        """'ﬁ' (U+FB01 LATIN SMALL LIGATURE FI, single char) must collide
        with 'fi' (two chars: f + i) after NFKC compatibility decomposition."""
        ligature = "\ufb01"  # ﬁ
        assert ligature != "fi"
        assert canonical_input_hash(ligature) == canonical_input_hash("fi")

    def test_canonical_input_hash_nfkc_normalizes_combining_chars(self) -> None:
        """'é' precomposed (U+00E9) must collide with 'e' + combining acute
        (U+0065 + U+0301) after NFKC canonical recomposition."""
        precomposed = "\u00e9"  # é (1 char)
        decomposed = "e\u0301"  # e + ́ (2 chars)
        assert precomposed != decomposed
        assert canonical_input_hash(precomposed) == canonical_input_hash(decomposed)

    def test_canonical_input_hash_nfkc_normalizes_fraction(self) -> None:
        """'½' (U+00BD) must collide with '1⁄2' after NFKC compat decomposition."""
        fraction = "\u00bd"
        # NFKC of ½ is "1⁄2" (digit one, fraction slash, digit two)
        normalized = unicodedata.normalize("NFKC", fraction)
        assert canonical_input_hash(fraction) == canonical_input_hash(normalized)

    def test_canonical_input_hash_nfkc_full_width_digit_collides(self) -> None:
        """'１２３' (full-width digits, U+FF11..U+FF13) collides with '123' after NFKC."""
        full_width = "\uff11\uff12\uff13"
        assert full_width != "123"
        assert canonical_input_hash(full_width) == canonical_input_hash("123")

    def test_canonical_input_hash_matches_reference_implementation(self) -> None:
        """Helper must agree with the documented canonicalization order verbatim
        on a tricky mixed input: NFKC + lower + strip."""
        text = "  ﬁND THE BUG  "
        assert canonical_input_hash(text) == _expected(text)


@pytest.mark.parametrize(
    "a,b,should_collide",
    [
        ("hello", "HELLO", True),
        ("  q  ", "q", True),
        ("ﬁsh", "fish", True),
        ("foo bar", "foobar", False),
        ("123", "abc", False),
        ("\u00e9", "e\u0301", True),  # é precomposed vs decomposed
    ],
)
def test_canonical_input_hash_collision_table(
    a: str, b: str, should_collide: bool
) -> None:
    """Parametrized collision/non-collision matrix — single source of truth."""
    if should_collide:
        assert canonical_input_hash(a) == canonical_input_hash(b)
    else:
        assert canonical_input_hash(a) != canonical_input_hash(b)
