"""F086: pure parsing/detection for ICL exemplar streams (`utterance\\nlabel: N`).

Zero-LLM by design. The R1 enumerative heuristic (`is_enumerable`) does NOT
fire on label-streams (label lines fail both its regexes), so exemplar
detection is a distinct predicate, checked BEFORE R1 in the extractor seam.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# A label line: `label: <value>` — value may be numeric or symbolic (trec: "LOC").
_LABEL_LINE = re.compile(r"^\s*label\s*:\s*(\S.*?)\s*$", re.IGNORECASE)
# Transcript speaker prefixes (layer.py capture format).
_USER_PREFIX = re.compile(r"^User:\s?", re.IGNORECASE)
_ASSISTANT_PREFIX = re.compile(r"^Assistant:", re.IGNORECASE)

_MIN_PAIRS = 3  # below this, never classify as an exemplar stream


@dataclass(frozen=True)
class ExemplarPair:
    text: str
    label: str
    ordinal: int


def _content_lines(text: str) -> list[str]:
    """Non-empty lines with Assistant lines removed and User: prefixes stripped."""
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or _ASSISTANT_PREFIX.match(line):
            continue
        out.append(_USER_PREFIX.sub("", line))
    return out


def exemplar_density(text: str) -> float:
    """Fraction-of-pair-structure score in [0, 1].

    A pure alternating stream has 50% label lines -> density 1.0 (the 2x
    factor). Assistant lines are excluded from the denominator so ack turns
    do not dilute a genuine stream.
    """
    lines = _content_lines(text)
    if len(lines) < 2 * _MIN_PAIRS:
        return 0.0
    n_labels = sum(1 for line in lines if _LABEL_LINE.match(line))
    if n_labels < _MIN_PAIRS:
        return 0.0
    return min(1.0, 2.0 * n_labels / len(lines))


def is_exemplar_stream(text: str, threshold: float) -> bool:
    # Codex r6: density is a label-FREQUENCY score — "3 utterances then 3
    # labels" scores 1.0 yet parse_exemplars yields ONE malformed pair (the
    # three utterances collapse into the first label; the trailing labels have
    # no preceding utterance and drop). Routing modal on that skips legacy
    # extraction and loses the real facts. The predicate is authoritative for
    # BOTH the extractor seam and backfill qualification, so it must also
    # require the actual alternation STRUCTURE: at least _MIN_PAIRS parsed
    # (utterance, label) pairs. parse_exemplars is a cheap regex walk.
    if exemplar_density(text) < threshold:
        return False
    return len(parse_exemplars(text)) >= _MIN_PAIRS


def parse_exemplars(text: str) -> list[ExemplarPair]:
    """Walk lines, accumulating utterance lines until each label line."""
    pairs: list[ExemplarPair] = []
    acc: list[str] = []
    for line in _content_lines(text):
        m = _LABEL_LINE.match(line)
        if m:
            utterance = "\n".join(acc).strip()
            if utterance:
                pairs.append(ExemplarPair(text=utterance, label=m.group(1), ordinal=len(pairs)))
            acc = []
        else:
            acc.append(line)
    return pairs


def parse_label(content: str) -> str | None:
    """Extract the label from a stored exemplar fact's content (last label line)."""
    for line in reversed(content.splitlines()):
        m = _LABEL_LINE.match(line)
        if m:
            return m.group(1)
    return None
