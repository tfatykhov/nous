"""R1 (064): enumerative fact extraction from raw transcript chunks.

The episode summarizer lossy-compresses enumerable content (lists, tables,
statement-per-line documents) into a ~150-word summary before the fact
extractor sees it — the measured cause of 1% fact coverage on dense factual
corpora. This module classifies transcripts with a cheap heuristic and, for
enumerable ones, extracts atomic facts from the RAW text, chunked in-memory
with the same helper/params as F067 (no dependency on heart.episode_chunks).
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

_PUNCT = re.compile(r"[^\w\s]")
_WS = re.compile(r"\s+")
_LIST_MARKER = re.compile(r"^\s*(?:[-*•]|\d{1,5}[.):])\s+")
# A short, self-contained declarative line: 10-180 chars ending in period/semicolon (not question/exclamation).
_STATEMENT_LINE = re.compile(r"^.{10,180}[.;]\s*$")


def normalize_key(raw: str | None) -> str | None:
    """Canonicalize an entity/attribute identifier: lowercase, strip
    punctuation (possessives collapse: "Tim's" -> "tims"), collapse
    whitespace, cap at 200 chars. None/empty -> None."""
    if not raw:
        return None
    s = _PUNCT.sub("", raw.lower())
    s = _WS.sub(" ", s).strip()
    return s[:200] or None


def density_score(text: str) -> float:
    """Fraction of non-empty lines that look like standalone declarative
    statements or list items. Pure heuristic — no LLM (R1.1)."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) < 5:
        return 0.0
    hits = sum(
        1 for ln in lines if _LIST_MARKER.match(ln) or _STATEMENT_LINE.match(ln)
    )
    return hits / len(lines)


def is_enumerable(text: str, threshold: float) -> bool:
    return density_score(text) >= threshold
