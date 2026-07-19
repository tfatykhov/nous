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
# codex P2 round 12: possessive suffixes must collapse to the base entity —
# "Tim's" and "tim" must normalize identically, or a query mentioning "Tim's
# trip" can never exact-match a stored key "tim". Two patterns since 's and
# s' are structurally different: a singular/trailing 's is dropped entirely
# ("tim's" -> "tim"), while a plural possessive only loses its apostrophe,
# keeping the s that's already the plural marker ("students'" -> "students").
_POSSESSIVE_SINGULAR = re.compile(r"(['’])s\b")
_POSSESSIVE_PLURAL = re.compile(r"s(['’])(?=\s|$)")


def _normalize_once(s: str, max_len: int) -> str:
    s = unicodedata.normalize("NFC", s).lower()
    # Must run BEFORE the punctuation strip below — apostrophes are still
    # present here. Order matters: singular 's is checked first so a name
    # like "boss's" (ends in "s's") collapses via the singular rule, not the
    # plural one.
    s = _POSSESSIVE_SINGULAR.sub("", s)
    s = _POSSESSIVE_PLURAL.sub("s", s)
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

    lowercase; NFC; strip possessive suffixes ('s / s'); underscores ->
    spaces; strip punctuation except intra-word hyphens; collapse
    whitespace; strip leading articles (a/an/the); cap at max_len. Iterated
    to a fixpoint so normalize_key(normalize_key(x)) == normalize_key(x)
    always holds (single-pass article stripping and cap-induced dangling
    hyphens would otherwise break idempotency).
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


_QUOTED = re.compile(
    r"\"([^\"]{2,80})\"|(?<!\w)'([^']{2,80})'(?!\w)|[“]([^”]{2,80})[”]"
)
# codex P2 round 15: the single-quote alternative used to match straight
# apostrophes bare -- a contraction ("what's") followed later by a
# possessive ("Tim's") gave it two apostrophes to pair up, capturing the
# junk span between them ("s Tim") as if it were a quoted mention. Guarding
# it with non-word-context lookaround/lookahead means an apostrophe
# directly attached to a word (contraction/possessive) can never serve as
# an opening OR closing delimiter, while a genuine quoted span like
# 'Belgium' (bounded by spaces/punctuation on both sides) still matches.
# Runs of TitleCase words, skipping sentence-initial position (mirrors
# intent.py:148 discipline); allows lowercase connectors inside the run.
_CAP_SPAN = re.compile(
    r"(?<!^)(?<![.!?]\s)"
    r"\b[A-Z][\w'’-]*(?:\s+(?:of|the|de|la|van|von|[A-Z][\w'’-]*))*"
)
# NOTE (review P2-3): 'and' is deliberately NOT a connector — "The Marriage of
# Figaro and The Barber of Seville" must yield TWO spans, not one merged key.


def extract_entity_candidates(
    text: str,
    *,
    vocab: frozenset[str] | None = None,
    max_candidates: int = 8,
    vocab_only: bool = False,
) -> list[str]:
    """NER-lite (R3.3 v1): quoted spans + capitalized spans + known-key
    n-gram matches against the agent's key vocabulary. Returns NORMALIZED,
    deduplicated candidate keys, quoted-first, capped at max_candidates.
    The vocab leg recovers lowercase/sentence-initial entities the
    capitalized-span heuristic misses ("the marriage of figaro").

    R3v2 codex round 1: ``vocab_only=True`` skips the quoted-span and
    capitalized-span legs entirely and runs ONLY the vocab n-gram leg. The
    round-2 content scan needs exactly "content mentions matched against the
    agent's key vocabulary" (the spec's own definition of that step) — the
    heuristic span legs exist for QUERY-side NER, where a non-indexed span
    is still a useful candidate to try. On a FACT-CONTENT scan they are pure
    cap pollution: the return value is truncated to ``max_candidates`` at
    the very end, quoted/cap-span-first, so >= ``max_candidates`` junk spans
    in a fact's content exhaust the cap before the vocab leg's real match is
    ever reached — even though that match IS collected, it just lands past
    the final slice. Default ``False`` keeps the query-side (round-1
    candidate extraction) path byte-unchanged.
    """
    seen: set[str] = set()
    out: list[str] = []

    def _add(raw: str) -> None:
        key = normalize_key(raw)
        if key and key not in seen:
            seen.add(key)
            out.append(key)

    if not vocab_only:
        for m in _QUOTED.finditer(text):
            _add(next(g for g in m.groups() if g))
        for m in _CAP_SPAN.finditer(text):
            # trim trailing lowercase connectors captured by the run
            span = re.sub(r"\s+(?:of|the|de|la|van|von)$", "", m.group(0))
            _add(span)
    if vocab:
        tokens = (normalize_key(text, max_len=1000) or "").split()
        # codex P2 round 2: the window used to be a fixed 4 tokens, but keys
        # can run much longer (normalize_key allows up to 200 chars — e.g.
        # "national museum of african american history" is 6 tokens) and
        # this vocab leg is the ONLY extractor that can recover a lowercase
        # mention (the capitalized-span regex above requires TitleCase).
        # Derive the window from the vocab's own longest key instead of a
        # fixed guess, capped at 8 to bound the O(tokens * max_n) scan.
        max_n = min(8, max((len(k.split()) for k in vocab), default=1))
        for n in range(max_n, 0, -1):  # longest grams first
            for i in range(len(tokens) - n + 1):
                gram = " ".join(tokens[i : i + n])
                if gram in vocab and gram not in seen:
                    seen.add(gram)
                    out.append(gram)
    return out[:max_candidates]


_NUMERIC = re.compile(r"[\d\s.,:/-]+")
# Code-side safety net; the extraction prompt is the primary proper-noun
# filter (R3.1 stop-policy). Deliberately small.
_SCALAR_STOP = frozenset({
    "red", "green", "blue", "black", "white", "yellow", "orange", "purple",
    "true", "false", "yes", "no", "none", "null", "unknown",
    # article-strip + question-word collisions (review devil-P3-5): keys like
    # "The Who"->"who" would otherwise create query-token junk buckets
    "the", "who", "what", "when", "where", "why", "how", "this", "that",
})


def is_keyable_entity(key: str, *, min_chars: int) -> bool:
    """R3.1 stop-policy: index proper-noun/entity values, never scalars.
    key must already be normalized."""
    if not key or len(key) < min_chars:
        return False
    if _NUMERIC.fullmatch(key):
        return False
    if key in _SCALAR_STOP:
        return False
    return True
