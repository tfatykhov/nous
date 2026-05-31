"""F076: deterministic proper-noun / named-entity extraction for co-mention linking.

Used by the sleep densifier to link facts that NAME the same entity, independent of
embedding distance (the associative edge cosine-only linking misses). Pure, no deps,
no model calls — a token scanner (not one big regex) so it is Unicode-aware, respects
sentence boundaries, and has no catastrophic-backtracking risk.

Heuristic, deliberately conservative (favor precision over recall — these edges ship
default-on and feed retrieval). A proper NER/LLM upgrade is a noted follow-on (v2), not
v1: keep the surface deterministic and cheap enough to run over the corpus in a sleep cycle.
"""
from __future__ import annotations

import re

# Lowercase connectors that may sit BETWEEN capitalized tokens of a single name
# ("Marie de Medici", "Duke of Orleans", "Ludwig van Beethoven"). They never start
# or end an entity and don't count toward the >=2 capitalized-token requirement.
_CONNECTORS: frozenset[str] = frozenset(
    {"de", "of", "the", "von", "van", "der", "den", "del", "di", "da", "la", "le", "du", "el"}
)

# Capitalized words that are almost always sentence-initial function words, not names.
# Dropping them as a LEADING token avoids "The Beatles" → "the beatles" style noise and
# "But Steve Hillage" merges.
_STOP_INITIAL: frozenset[str] = frozenset({
    "The", "A", "An", "In", "On", "For", "He", "She", "It", "They", "This", "That",
    "But", "And", "As", "At", "By", "To", "Of", "From", "With", "When", "While", "If",
    "We", "I", "You", "His", "Her", "Their", "Its", "Our", "My", "Was", "Is", "Are",
    "Then", "There", "Here", "So", "Or", "Not", "No", "Also", "After", "Before",
    # Sentence-initial discourse/temporal adverbs — leaking these as a leading
    # token splinters an entity ("Later Steve Hillage" != "Steve Hillage") and
    # silently costs recall on the co-mention match.
    "Later", "However", "Now", "Today", "Yesterday", "Tomorrow", "Meanwhile",
    "Eventually", "Finally", "Subsequently", "Thus", "Therefore", "Although",
    "Though", "Since", "Because", "During", "Despite", "Once", "Soon", "Recently",
    "Currently", "Previously", "Originally", "Initially", "Additionally",
    "Furthermore", "Moreover", "Nevertheless", "Instead", "Perhaps", "Indeed",
    "Overall", "Together", "Both", "Each", "Every", "Some", "Many", "Most",
    "Several", "Other", "Another", "These", "Those", "What", "Who", "Where",
    "Which", "Why", "How",
})

# Split into sentence-ish segments so an entity never spans '.', '!', '?', ';', ':',
# newline, or a comma+space — prevents "...visited Paris. Later Steve..." → "paris later".
_SEGMENT_RE = re.compile(r"[.!?;:\n]+|,\s")

# Strip surrounding punctuation/quotes from a token (keep internal . & - ' for "U.S.", "AT&T").
_EDGE_PUNCT = "\"'’“”()[]{}.,;:!?…«»"


def _strip_possessive(tok: str) -> str:
    low = tok.lower()
    if low.endswith("'s") or low.endswith("’s"):
        tok = tok[:-2]
    elif low.endswith("'") or low.endswith("’"):
        tok = tok[:-1]
    return tok


def _norm(tok: str) -> str:
    """Normalize a single token: trim edge punctuation + possessive. May return ''."""
    tok = tok.strip(_EDGE_PUNCT)
    tok = _strip_possessive(tok)
    return tok.strip(_EDGE_PUNCT)


def _is_capitalized(tok: str) -> bool:
    """First alphabetic char is uppercase (Unicode-aware: É, Ł, Ø ...)."""
    for ch in tok:
        if ch.isalpha():
            return ch.isupper()
    return False


def extract_entities(text: str, *, min_chars: int = 6) -> set[str]:
    """Extract normalized multi-token proper-noun phrases mentioned in ``text``.

    A phrase is a run of capitalized tokens (with optional lowercase connectors in
    between), with >=2 capitalized tokens, lowercased, length >= ``min_chars``.
    Single surnames are intentionally NOT emitted (precision over recall for a
    default-on edge builder). Sentence boundaries are not crossed.
    """
    out: set[str] = set()
    if not text:
        return out

    for segment in _SEGMENT_RE.split(text):
        run: list[str] = []          # normalized tokens of the current run
        n_caps = 0                   # capitalized (non-connector) tokens in the run

        def flush() -> None:
            nonlocal run, n_caps
            # Trim trailing connectors ("Steve Hillage of" -> "Steve Hillage").
            while run and run[-1].lower() in _CONNECTORS:
                run.pop()
            if n_caps >= 2:
                phrase = " ".join(run).lower()
                if len(phrase) >= min_chars:
                    out.add(phrase)
            run = []
            n_caps = 0

        for i, raw in enumerate(segment.split()):
            norm = _norm(raw)
            if not norm:
                flush()
                continue
            low = norm.lower()
            if _is_capitalized(norm):
                # Drop a sentence-initial stopword only when it would START the run.
                if not run and raw.strip(_EDGE_PUNCT) in _STOP_INITIAL:
                    continue
                run.append(norm)
                n_caps += 1
            elif low in _CONNECTORS and run:
                # Connector continues a run only if it's mid-name (a cap precedes it);
                # a trailing connector is trimmed in flush().
                run.append(low)
            else:
                flush()
        flush()
    return out
