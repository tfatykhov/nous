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
    `compress_string_array` selects by index and does not call this; it
    remains the public error-line check.
    """
    return [ln for ln in lines if _ERROR_PATTERNS.search(ln)]


# --- Task 3: String array compression ---


@dataclass
class CompressResult:
    """Result of compression: the kept lines in original order, plus a marker.

    `kept` (lines without an error word) and `preserved` (error-word lines)
    are disjoint by position. `body` is what is emitted: every kept line in
    its original order, with one gap line in place of each omitted run.
    """
    kept: list[str] = field(default_factory=list)
    preserved: list[str] = field(default_factory=list)
    marker: str = ""
    original_count: int = 0
    body: list[str] = field(default_factory=list)

    def to_text(self) -> str:
        """Assemble the output: the body, then the marker. Never dedups."""
        parts = list(self.body)
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


# Score differences carry float noise: 0.8 - 0.5 == 0.30000000000000004,
# which tripped a 0.3 elbow, gave K=1 and a negative mid count, and dropped
# one arbitrary line. A cliff must clear the threshold by more than noise.
_ELBOW_EPSILON = 1e-9
# Floor for the elbow's K, so head + tail + mid is a real budget.
_MIN_K = 10
# Gap lines cost tokens: when the cut body (gap lines included) still holds
# this fraction of the input's chars, the honest output is the original.
_MAX_BODY_FRACTION = 0.8


def _gap_line(start: int, end: int) -> str:
    """The line standing in for omitted lines [start, end) (0-based), named
    as 1-based line numbers of the result ("result lines", so they are not
    read as file lines after `sed -n 'X,Yp'`). It must never start with
    "[SmartCompressed:": compaction parses that prefix as the summary marker."""
    return f"[\u2026 result lines {start + 1}\u2013{end} omitted ({end - start} lines) \u2026]"


def compress_string_array(
    lines: list[str], max_k: int = 50, elbow_threshold: float = 0.3,
) -> CompressResult:
    """Compress newline-separated output (bash, grep, file listings).

    At most `max_k` lines: returned unchanged, no marker. Otherwise keeps
    every error-word line, then K other lines: the head (30%), the tail
    (15%) and the highest-`_score_line` lines for the rest, K set by the
    score elbow. Kept lines are emitted in their ORIGINAL order with one gap
    line per omitted run, and nothing is deduplicated.
    """
    unchanged = CompressResult(kept=list(lines), body=list(lines), original_count=len(lines))
    if len(lines) <= max_k:
        return unchanged

    # 1. Error-word lines are always kept.
    error_idx = {i for i, ln in enumerate(lines) if _ERROR_PATTERNS.search(ln)}
    others = [i for i in range(len(lines)) if i not in error_idx]  # original order

    # 2. Rank the others by score; ties by position.
    scores = {i: _score_line(lines[i]) for i in others}
    ranked = sorted(others, key=lambda i: (-scores[i], i))

    # 3. Find elbow (score cliff), then floor K.
    k = max_k
    for n in range(1, len(ranked)):
        if scores[ranked[n - 1]] - scores[ranked[n]] > elbow_threshold + _ELBOW_EPSILON:
            k = min(n, max_k)
            break
    k = max(k, min(_MIN_K, max_k))

    # 4. Allocate: 30% from start, 15% from tail, rest = highest scored.
    head_count = max(1, int(k * 0.30))
    tail_count = max(1, int(k * 0.15))
    mid_count = max(0, k - head_count - tail_count)
    keep = set(others[:head_count]) | set(others[-tail_count:])
    keep |= set([i for i in ranked if i not in keep][:mid_count])
    # A gap line costs a line: never use one to hide a single line.
    chosen = keep | error_idx
    keep |= {i for i in others if i not in chosen and i - 1 in chosen and i + 1 in chosen}

    emitted = sorted(keep | error_idx)
    if len(emitted) == len(lines):
        return unchanged

    # 5. Emit in original order, one gap line per omitted run. The first and
    # last lines are always kept (head/tail or error), so gaps are interior.
    body: list[str] = []
    cursor = 0
    for i in emitted:
        if i > cursor:
            body.append(_gap_line(cursor, i))
        body.append(lines[i])
        cursor = i + 1
    if len("\n".join(body)) >= _MAX_BODY_FRACTION * len("\n".join(lines)):
        return unchanged

    preserved = [lines[i] for i in sorted(error_idx)]
    marker = (
        f"[SmartCompressed: {len(lines)}\u2192{len(emitted)} lines, "
        f"{len(preserved)} error/outlier preserved, order kept, gaps marked]"
    )

    return CompressResult(
        kept=[lines[i] for i in sorted(keep)],
        preserved=preserved,
        marker=marker,
        original_count=len(lines),
        body=body,
    )


# --- Task 4: dict_array compression ---


@dataclass
class DictCompressResult:
    """Result of dict_array compression.

    `kept` (items without an error word) and `preserved` (error-word items)
    are disjoint by position. `body` is what is emitted: every selected item
    in its original array position.
    """
    kept: list[dict] = field(default_factory=list)
    preserved: list[dict] = field(default_factory=list)
    marker: str = ""
    original_count: int = 0
    body: list[dict] = field(default_factory=list)

    def to_text(self) -> str:
        """The body as one JSON array, then the marker. Never dedups."""
        result = json.dumps(self.body, default=str, indent=None)
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
    """Compress a JSON array of objects.

    At most `max_k` items: returned unchanged, no marker. Otherwise keeps
    every error-word item, plus the top `max_k` others by the detected score
    field (ties by position), or the first `max_k` others when there is no
    score field. Selected items are emitted in their ORIGINAL array order,
    and nothing is deduplicated.
    """
    if len(items) <= max_k:
        return DictCompressResult(kept=items, original_count=len(items), body=items)

    # Error-word items are always kept.
    error_idx = {i for i, item in enumerate(items) if _is_error_item(item)}
    others = [i for i in range(len(items)) if i not in error_idx]  # original order

    score_field = _detect_score_field([items[i] for i in others])
    if score_field:
        # Stable sort: equal scores keep their original order.
        ranked = sorted(others, key=lambda i: items[i].get(score_field, 0), reverse=True)
        keep = set(ranked[:max_k])
    else:
        keep = set(others[:max_k])

    emitted = sorted(keep | error_idx)
    if len(emitted) == len(items):  # every item kept: nothing cut, no marker
        return DictCompressResult(kept=items, original_count=len(items), body=items)
    score_info = f"K={len(keep)} by {score_field}" if score_field else f"first {len(keep)}"
    marker = (
        f"[SmartCompressed: {len(items)}\u2192{len(emitted)} items, "
        f"{score_info}, order kept, {len(error_idx)} outliers]"
    )

    return DictCompressResult(
        kept=[items[i] for i in sorted(keep)],
        preserved=[items[i] for i in sorted(error_idx)],
        marker=marker,
        original_count=len(items),
        body=[items[i] for i in emitted],
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
    # Not configurable: cache_retrieve exists to return originals, and cutting one again would undo it.
    if tool_name == "cache_retrieve":
        return passthrough
    # Per-tool exemption. Everything below this point ranks by original
    # position and by `_score_line` — neither of which can see a retrieval
    # score — so a tool whose output is ALREADY ranked must not be re-ranked
    # here. `tool_name` was previously used only in a log message.
    if tool_name in getattr(settings, "smart_compress_exempt_tools", ()):
        logger.info(
            "SmartCompress exempt %s: already-ranked output, passing through "
            "(%d chars)", tool_name, len(result_text),
        )
        return passthrough
    if len(result_text) < settings.smart_compress_min_chars:
        return passthrough

    # #179: bash_tool appends an authoritative trailing "Exit code: N"
    # status line to EVERY result. Detach it before crushability checks,
    # classification, and compression — it breaks DICT_ARRAY json parsing,
    # and compression could cut it from the tail — then deterministically
    # re-attach it as the final line, after the marker.
    status_line: str | None = None
    body_text = result_text
    stripped_tail = result_text.rstrip()
    if "\n" in stripped_tail:
        head_part, last_line = stripped_tail.rsplit("\n", 1)
        if last_line.startswith("Exit code: "):
            status_line = last_line
            body_text = head_part

    if not is_crushable(body_text, min_chars=settings.smart_compress_min_chars):
        logger.info("SmartCompress skip %s: not crushable (%d chars)", tool_name, len(result_text))
        return passthrough

    content_type = classify_content(body_text, min_chars=settings.smart_compress_min_chars)

    if content_type == ContentType.SMALL:
        return passthrough

    compressed_text = body_text
    item_count = None

    if content_type == ContentType.STRING_ARRAY:
        lines = body_text.split("\n")
        compressed = compress_string_array(
            lines,
            max_k=settings.smart_compress_max_k,
            elbow_threshold=settings.smart_compress_elbow_threshold,
        )
        if not compressed.marker:  # nothing cut: the original bytes
            return passthrough
        compressed_text = compressed.to_text()
        item_count = compressed.original_count

    elif content_type == ContentType.DICT_ARRAY:
        try:
            items = json.loads(body_text.strip())
            compressed = compress_dict_array(items, max_k=settings.smart_compress_max_k)
            # Nothing cut: return the original bytes. to_text() would
            # re-serialise it, which differs from body_text in whitespace, and
            # count (and, for a non-refetchable tool, cache) as compression.
            if not compressed.marker:
                return passthrough
            compressed_text = compressed.to_text()
            # The string path's rule: a cut that keeps most of the chars is
            # not worth it. Checked here because only here are the original
            # bytes at hand.
            if len(compressed_text) >= _MAX_BODY_FRACTION * len(body_text):
                return passthrough
            item_count = compressed.original_count
        except (json.JSONDecodeError, TypeError):
            return passthrough

    elif content_type in (ContentType.LOG_FORMAT, ContentType.RAW_TEXT):
        lines = body_text.split("\n")
        compressed = compress_string_array(
            lines,
            max_k=settings.smart_compress_max_k,
            elbow_threshold=settings.smart_compress_elbow_threshold,
        )
        if not compressed.marker:  # nothing cut: the original bytes
            return passthrough
        compressed_text = compressed.to_text()
        item_count = compressed.original_count

    if compressed_text == body_text:
        return passthrough

    # codex round 13 (#179): record the original character size in the
    # marker so the compaction pruner's bulk-threshold comparison survives
    # ingestion-time compression (the marker otherwise records only
    # line/item counts). The marker is always to_text()'s final line —
    # checking exactly that line (not a substring window) prevents grafting
    # onto content that merely QUOTES a SmartCompressed marker.
    head, _, marker_line = compressed_text.rpartition("\n")
    if head and marker_line.startswith("[SmartCompressed:") and marker_line.endswith("]"):
        compressed_text = (
            f"{head}\n{marker_line[:-1]}, {len(result_text)} chars original]"
        )

    # Re-attach the authoritative status line as the final line (#179).
    if status_line is not None:
        compressed_text = f"{compressed_text}\n{status_line}"

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


# The final marker as smart_compress() emits it: a whole line that ends with
# the original size. The key goes in front of that size.
_FINAL_MARKER_RE = re.compile(r"^\[SmartCompressed:.*(, \d+ chars original\])$")


def _final_marker(lines: list[str]) -> tuple[int, re.Match[str]] | None:
    """Locate the final SmartCompressed marker: the last line, or the line
    before a trailing "Exit code: " line, if that line is a marker
    smart_compress() emits. Tolerant at the end only: whitespace-only lines
    after the marker or the exit line, and between them, are skipped, and
    the marker is matched after rstrip. No other line is read, so a marker
    quoted inside the body never counts. Returns its index and the match
    (taken on the rstripped line, so its offsets hold in the line), or None."""

    def skip_blank(i: int) -> int:
        while i >= 0 and not lines[i].strip():
            i -= 1
        return i

    i = skip_blank(len(lines) - 1)
    if i >= 1 and lines[i].startswith("Exit code: "):
        i = skip_blank(i - 1)
    if i < 0:
        return None
    m = _FINAL_MARKER_RE.match(lines[i].rstrip())
    return None if m is None else (i, m)


def final_marker_line(text: str) -> str | None:
    """The final SmartCompressed marker line of `text`, or None.

    One rule for every reader: add_cache_key() writes the key into this line
    and compaction reads the original size from it.
    """
    found = _final_marker(text.split("\n"))
    return None if found is None else found[1].group(0)


def add_cache_key(text: str, key: str) -> str:
    """Name the cache_retrieve key in the final SmartCompressed marker.

    The marker is the last line, or the line before a trailing "Exit code: "
    line. ", cache_retrieve key <key>" goes right before ", N chars
    original]". Only that one line is read, so a marker quoted inside the
    body is never touched; with no such marker the text comes back unchanged.
    Every other byte, trailing whitespace and newlines included, is kept.
    The runner calls this only after the original was cached.
    """
    lines = text.split("\n")
    found = _final_marker(lines)
    if found is None:
        return text
    i, m = found
    cut = m.start(1)
    lines[i] = f"{lines[i][:cut]}, cache_retrieve key {key}{lines[i][cut:]}"
    return "\n".join(lines)
