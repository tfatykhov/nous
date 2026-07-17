"""R3 (F085): single canonical key normalizer + query-side entity candidates.

ONE canonicalizer for subject_key, attribute_key, and entity keys, used by
BOTH the write path (enumerative extractor, Heart.learn, backfill) and the
read path (keyed retrieval leg). The R2 conflict lookups compare keys by
exact string equality (facts.py), so every producer and consumer MUST route
through normalize_key.
"""
from __future__ import annotations

import re
import unicodedata

# Strip punctuation except hyphens (handled separately) — \w keeps letters,
# digits, underscore; underscores are converted to spaces beforehand.
_PUNCT = re.compile(r"[^\w\s-]")
_DANGLING_HYPHEN = re.compile(r"(?<!\w)-|-(?!\w)")
_WS = re.compile(r"\s+")
_ARTICLES = ("a ", "an ", "the ")


def _normalize_once(s: str, max_len: int) -> str:
    s = unicodedata.normalize("NFC", s).lower()
    s = s.replace("_", " ")
    s = _PUNCT.sub("", s)
    s = _DANGLING_HYPHEN.sub(" ", s)
    s = _WS.sub(" ", s).strip()
    for art in _ARTICLES:
        if s.startswith(art):
            s = s[len(art):]
            break
    return s[:max_len].strip()


def normalize_key(raw: str | None, *, max_len: int = 200) -> str | None:
    """Canonicalize an entity/attribute key (R3.2).

    lowercase; NFC; underscores -> spaces; strip punctuation except
    intra-word hyphens; collapse whitespace; strip leading articles
    (a/an/the); cap at max_len. Iterated to a fixpoint so
    normalize_key(normalize_key(x)) == normalize_key(x) always holds
    (single-pass article stripping and cap-induced dangling hyphens would
    otherwise break idempotency).
    """
    if not raw:
        return None
    s = raw
    for _ in range(10):  # fixpoint loop; bounded defensively, converges in <=3
        nxt = _normalize_once(s, max_len)
        if nxt == s:
            break
        s = nxt
    return s or None


_QUOTED = re.compile(r"\"([^\"]{2,80})\"|'([^']{2,80})'|[“]([^”]{2,80})[”]")
# Runs of TitleCase words, skipping sentence-initial position (mirrors
# intent.py:148 discipline); allows lowercase connectors inside the run.
_CAP_SPAN = re.compile(
    r"(?<!^)(?<![.!?]\s)"
    r"\b[A-Z][\w''-]*(?:\s+(?:of|the|de|la|van|von|[A-Z][\w''-]*))*"
)
# NOTE (review P2-3): 'and' is deliberately NOT a connector — "The Marriage of
# Figaro and The Barber of Seville" must yield TWO spans, not one merged key.


def extract_entity_candidates(
    text: str,
    *,
    vocab: frozenset[str] | None = None,
    max_candidates: int = 8,
) -> list[str]:
    """NER-lite (R3.3 v1): quoted spans + capitalized spans + known-key
    n-gram matches against the agent's key vocabulary. Returns NORMALIZED,
    deduplicated candidate keys, quoted-first, capped at max_candidates.
    The vocab leg recovers lowercase/sentence-initial entities the
    capitalized-span heuristic misses ("the marriage of figaro").
    """
    seen: set[str] = set()
    out: list[str] = []

    def _add(raw: str) -> None:
        key = normalize_key(raw)
        if key and key not in seen:
            seen.add(key)
            out.append(key)

    for m in _QUOTED.finditer(text):
        _add(next(g for g in m.groups() if g))
    for m in _CAP_SPAN.finditer(text):
        # trim trailing lowercase connectors captured by the run
        span = re.sub(r"\s+(?:of|the|de|la|van|von)$", "", m.group(0))
        _add(span)
    if vocab:
        tokens = (normalize_key(text, max_len=1000) or "").split()
        for n in range(4, 0, -1):  # longest grams first
            for i in range(len(tokens) - n + 1):
                gram = " ".join(tokens[i : i + n])
                if gram in vocab and gram not in seen:
                    seen.add(gram)
                    out.append(gram)
    return out[:max_candidates]
