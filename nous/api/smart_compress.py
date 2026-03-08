"""SmartCompress — ingestion-time tool output compression.

Operates at L0 (before F016 age-based decay). Classifies content,
checks crushability, and applies content-aware compression that
preserves errors, outliers, and high-signal items.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from enum import Enum

from nous.config import Settings

logger = logging.getLogger(__name__)


class ContentType(Enum):
    SMALL = "small"
    DICT_ARRAY = "dict_array"
    STRING_ARRAY = "string_array"
    LOG_FORMAT = "log_format"
    RAW_TEXT = "raw_text"


# Error keywords — hard-preserved during compression
_ERROR_PATTERNS = re.compile(
    r"\b(error|exception|failed|critical|traceback|fatal|panic)\b", re.IGNORECASE
)

# Log timestamp pattern
_LOG_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}"
)

# Score field detection in JSON
_SCORE_FIELDS = {"score", "relevance", "confidence", "similarity", "rank", "rating"}


def classify_content(text: str, min_chars: int = 500) -> ContentType:
    """Classify tool output for compression strategy selection."""
    if len(text) < min_chars:
        return ContentType.SMALL

    stripped = text.strip()

    # Try JSON array
    if stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, list) and len(parsed) > 0 and isinstance(parsed[0], dict):
                return ContentType.DICT_ARRAY
        except (json.JSONDecodeError, IndexError):
            pass

    # Check for log format (>50% of lines have timestamps)
    lines = stripped.split("\n")
    if len(lines) > 5:
        ts_count = sum(1 for ln in lines[:20] if _LOG_TIMESTAMP.match(ln))
        if ts_count / min(len(lines), 20) > 0.5:
            return ContentType.LOG_FORMAT

    # Multi-line text = string_array if many lines, raw_text otherwise
    if len(lines) > 10:
        return ContentType.STRING_ARRAY

    return ContentType.RAW_TEXT


def is_crushable(text: str, min_chars: int = 500) -> bool:
    """Safety gate: determine if we have enough signal to compress safely.

    Returns False (skip compression) if ALL of:
    - Content is small (< min_chars)
    - Uniqueness ratio > 0.9 (every line distinct)
    - No error lines detected
    - No score field detected
    """
    if len(text) < min_chars:
        return False

    has_errors = bool(_ERROR_PATTERNS.search(text))
    if has_errors:
        return True

    # Check for score fields in JSON
    stripped = text.strip()
    if stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, list) and len(parsed) > 0 and isinstance(parsed[0], dict):
                if _SCORE_FIELDS & set(parsed[0].keys()):
                    return True
        except (json.JSONDecodeError, IndexError):
            pass

    # Check uniqueness ratio
    lines = stripped.split("\n")
    if len(lines) > 1:
        unique_ratio = len(set(lines)) / len(lines)
        if unique_ratio <= 0.9:
            return True  # Enough duplicates to cluster/compress

    return False


# --- Task 2: Error preservation ---


def extract_preserved_lines(lines: list[str]) -> list[str]:
    """Extract lines that must ALWAYS be preserved regardless of compression.

    Hard guarantee: error/exception/traceback lines are never dropped.
    """
    return [ln for ln in lines if _ERROR_PATTERNS.search(ln)]


# --- Task 3: String array compression ---


@dataclass
class CompressResult:
    """Result of compression — kept items + preserved items + marker."""
    kept: list[str] = field(default_factory=list)
    preserved: list[str] = field(default_factory=list)
    marker: str = ""
    original_count: int = 0

    def to_text(self) -> str:
        """Assemble compressed output."""
        parts = list(dict.fromkeys(self.preserved + self.kept))  # Dedup, preserve order
        if self.marker:
            parts.append(self.marker)
        return "\n".join(parts)


def _score_line(line: str) -> float:
    """Score a line by heuristic relevance."""
    score = 0.3  # Base
    if _ERROR_PATTERNS.search(line):
        score = 1.0
    elif re.search(r"https?://", line):
        score = max(score, 0.8)
    elif re.search(r"[/\\][\w.-]+\.[\w]+", line):  # File path
        score = max(score, 0.7)
    elif re.search(r"\d+", line):
        score = max(score, 0.5)
    if not line.strip():
        score = 0.0
    return score


def compress_string_array(
    lines: list[str], max_k: int = 50, elbow_threshold: float = 0.3,
) -> CompressResult:
    """Compress newline-separated output (bash, grep, file listings).

    Preserves errors, keeps top-K by relevance score, includes tail lines.
    """
    if len(lines) <= max_k:
        return CompressResult(
            kept=lines, preserved=[], marker="", original_count=len(lines),
        )

    # 1. Extract hard-preserved lines
    preserved = extract_preserved_lines(lines)
    preserved_set = set(preserved)

    # 2. Score remaining lines
    scored = [
        (i, ln, _score_line(ln))
        for i, ln in enumerate(lines)
        if ln not in preserved_set
    ]
    scored.sort(key=lambda x: x[2], reverse=True)

    # 3. Find elbow (score cliff)
    k = max_k
    for i in range(1, len(scored)):
        if scored[i - 1][2] - scored[i][2] > elbow_threshold:
            k = min(i, max_k)
            break

    # 4. Allocate: 30% from start, 15% from tail, rest = highest scored
    head_count = max(1, int(k * 0.30))
    tail_count = max(1, int(k * 0.15))
    mid_count = k - head_count - tail_count

    # Head lines (by original position)
    non_preserved = [(i, ln) for i, ln, _s in scored]
    by_position = sorted(non_preserved, key=lambda x: x[0])
    head = [ln for _, ln in by_position[:head_count]]

    # Tail lines (last N from original)
    tail = [ln for _, ln in by_position[-tail_count:]]

    # Mid: highest scored not already in head/tail
    head_tail_set = set(head + tail)
    mid = [ln for _, ln, _s in scored if ln not in head_tail_set][:mid_count]

    kept = head + mid + tail

    total_kept = len(kept) + len(preserved)
    marker = (
        f"[SmartCompressed: {len(lines)}\u2192{total_kept} lines, "
        f"{len(preserved)} error/outlier preserved]"
    )

    return CompressResult(
        kept=kept,
        preserved=preserved,
        marker=marker,
        original_count=len(lines),
    )


# --- Task 4: dict_array compression ---


@dataclass
class DictCompressResult:
    """Result of dict_array compression."""
    kept: list[dict] = field(default_factory=list)
    preserved: list[dict] = field(default_factory=list)
    marker: str = ""
    original_count: int = 0

    def to_text(self) -> str:
        all_items = list({json.dumps(d, default=str): d for d in self.preserved + self.kept}.values())
        result = json.dumps(all_items, default=str, indent=None)
        return f"{result}\n{self.marker}"


def _detect_score_field(items: list[dict]) -> str | None:
    """Detect which field is the score/ranking field."""
    if not items:
        return None
    first = items[0]
    for field_name in _SCORE_FIELDS:
        if field_name in first:
            val = first[field_name]
            if isinstance(val, (int, float)):
                return field_name
    return None


def _is_error_item(item: dict) -> bool:
    """Check if a dict item represents an error."""
    text = json.dumps(item, default=str).lower()
    return bool(_ERROR_PATTERNS.search(text))


def compress_dict_array(items: list[dict], max_k: int = 50) -> DictCompressResult:
    """Compress JSON array of objects. Uses score field if available, else keeps all."""
    if len(items) <= max_k:
        return DictCompressResult(kept=items, original_count=len(items))

    # Preserve error items
    preserved = [item for item in items if _is_error_item(item)]
    non_error = [item for item in items if not _is_error_item(item)]

    # Detect and sort by score
    score_field = _detect_score_field(non_error)
    if score_field:
        sorted_items = sorted(non_error, key=lambda x: x.get(score_field, 0), reverse=True)
        kept = sorted_items[:max_k]
    else:
        # No score field — keep first max_k items (original order)
        kept = non_error[:max_k]

    total = len(kept) + len(preserved)
    score_info = f"K={len(kept)} by {score_field}" if score_field else f"first {len(kept)}"
    marker = (
        f"[SmartCompressed: {len(items)}\u2192{total} items, "
        f"{score_info}, {len(preserved)} outliers]"
    )

    return DictCompressResult(
        kept=kept, preserved=preserved, marker=marker, original_count=len(items),
    )


# --- Task 5+9: Main entry point ---


@dataclass
class SmartCompressResult:
    """Result of smart_compress — text + optional cache info."""
    text: str
    was_compressed: bool = False
    original_text: str | None = None  # Only set for non-re-fetchable tools
    item_count: int | None = None


async def smart_compress(
    tool_name: str,
    tool_input: dict,
    result_text: str,
    settings: Settings,
    is_error: bool = False,
) -> SmartCompressResult:
    """Main entry point — compress tool output if appropriate.

    Returns SmartCompressResult with compressed text and cache metadata.
    Called from runner.py _tool_loop after dispatch, before messages[].
    """
    passthrough = SmartCompressResult(text=result_text)

    if not settings.smart_compress_enabled:
        return passthrough
    if is_error:
        return passthrough
    if len(result_text) < settings.smart_compress_min_chars:
        return passthrough
    if not is_crushable(result_text, min_chars=settings.smart_compress_min_chars):
        logger.info("SmartCompress skip %s: not crushable (%d chars)", tool_name, len(result_text))
        return passthrough

    content_type = classify_content(result_text, min_chars=settings.smart_compress_min_chars)

    if content_type == ContentType.SMALL:
        return passthrough

    compressed_text = result_text
    item_count = None

    if content_type == ContentType.STRING_ARRAY:
        lines = result_text.split("\n")
        compressed = compress_string_array(
            lines,
            max_k=settings.smart_compress_max_k,
            elbow_threshold=settings.smart_compress_elbow_threshold,
        )
        compressed_text = compressed.to_text()
        item_count = compressed.original_count

    elif content_type == ContentType.DICT_ARRAY:
        try:
            items = json.loads(result_text.strip())
            compressed = compress_dict_array(items, max_k=settings.smart_compress_max_k)
            compressed_text = compressed.to_text()
            item_count = compressed.original_count
        except (json.JSONDecodeError, TypeError):
            return passthrough

    elif content_type in (ContentType.LOG_FORMAT, ContentType.RAW_TEXT):
        lines = result_text.split("\n")
        compressed = compress_string_array(lines, max_k=settings.smart_compress_max_k)
        compressed_text = compressed.to_text()
        item_count = compressed.original_count

    if compressed_text == result_text:
        return passthrough

    original_len = len(result_text)
    compressed_len = len(compressed_text)
    ratio = (1 - compressed_len / original_len) * 100 if original_len else 0
    logger.info(
        "SmartCompress %s [%s]: %d→%d chars (%.0f%% reduction, %s items)",
        tool_name, content_type.value, original_len, compressed_len, ratio,
        item_count or "n/a",
    )

    from nous.api.tool_cache import NON_REFETCHABLE_TOOLS
    cacheable = tool_name in NON_REFETCHABLE_TOOLS
    if cacheable:
        logger.info("SmartCompress %s: original queued for cache (non-refetchable)", tool_name)
    return SmartCompressResult(
        text=compressed_text,
        was_compressed=True,
        original_text=result_text if cacheable else None,
        item_count=item_count,
    )
