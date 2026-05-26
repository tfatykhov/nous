"""Shared CJK (Chinese / Japanese / Korean) detection + word counting.

Ported from gbrain's ``src/core/cjk.ts`` (github.com/garrytan/gbrain) so
downstream callers (F050 query expansion gate, future chunker support)
share one source of truth instead of duplicating regex literals.

Scope: BMP-only Unicode ranges that cover ~99% of real CJK content:
  - Han (CJK Unified Ideographs): U+4E00-U+9FFF
  - Hiragana: U+3040-U+309F
  - Katakana: U+30A0-U+30FF
  - Hangul Syllables: U+AC00-U+D7AF

Out of scope: Han extensions A/B/C, halfwidth katakana, compatibility
ideographs / Jamo, iteration marks. Mirrors gbrain v0.32.7's scope decision.
"""

from __future__ import annotations

import re

# Character class covering Han + Hiragana + Katakana + Hangul Syllables.
# Used in two compiled regexes below.
_CJK_CHARS = (
    "一-鿿"   # Han
    "぀-ゟ"   # Hiragana
    "゠-ヿ"   # Katakana
    "가-힯"   # Hangul Syllables
)

_CJK_PROBE = re.compile(f"[{_CJK_CHARS}]")
_CJK_GLOBAL = re.compile(f"[{_CJK_CHARS}]")
_WHITESPACE_TOKEN = re.compile(r"\S+")

# Density threshold for switching word-count strategy. Below this CJK char
# density, a doc is treated as Latin-mostly and stays whitespace-tokenized.
# At or above, it's CJK-mostly and we count non-whitespace characters.
# Mirrors gbrain's 0.30 (codex outside-voice C13 in their repo).
CJK_DENSITY_THRESHOLD = 0.30


def has_cjk(s: str) -> bool:
    """True if the string contains at least one CJK character (in scope)."""
    return bool(_CJK_PROBE.search(s))


def count_cjk_aware_words(s: str) -> int:
    """CJK-aware "word" count.

    CJK languages aren't whitespace-tokenized, so a paragraph of Chinese
    collapses to 1 word under whitespace splitting and downstream gates
    (F050 ``query_expansion_min_words``, future chunker word caps)
    misclassify it as below threshold.

    Heuristic: switch on CJK character density, not mere presence.
      - Density >= ``CJK_DENSITY_THRESHOLD`` (0.30): CJK-dominant; count each
        non-whitespace character as a "word".
      - Below threshold: Latin-dominant; whitespace tokens are the right unit.

    Examples:
      - ``"hello world"`` -> 2 (Latin)
      - ``"今天北京天气怎么样"`` -> 9 (CJK-dominant, char count)
      - ``"What about 北京 weather?"`` -> 4 (Latin-dominant, whitespace tokens)
      - ``""`` -> 0
    """
    if not s:
        return 0
    cjk_count = len(_CJK_GLOBAL.findall(s))
    non_whitespace = len(re.sub(r"\s", "", s))
    if non_whitespace == 0:
        return 0
    density = cjk_count / non_whitespace
    if density >= CJK_DENSITY_THRESHOLD:
        return non_whitespace
    return len(_WHITESPACE_TOKEN.findall(s))
