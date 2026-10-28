"""ReversibleCache — Postgres-backed cache for compressed tool results.

When SmartCompress compresses a non-re-fetchable tool result (bash,
web_search, web_fetch, dag_manage), the original is stored here. The model
can retrieve it via cache_retrieve tool.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from nous.storage.models import ToolCache

logger = logging.getLogger(__name__)

# Tools where calling again does NOT recover the original, pre-compression
# content — not "results differ each time" (dag_manage's node_result is
# deterministic for unchanged DAG state, yet belongs here too). web_search
# and web_fetch qualify because the upstream result can genuinely change on
# a re-fetch. dag_manage qualifies for a different reason (codex P2 round
# 6, FINDING 10): SmartCompress re-runs on every dispatch and re-compresses
# the same input identically, so a second call reproduces the SAME
# compressed output, not the original. Same criterion, different cause —
# the next tool added here should satisfy "calling it again gets you back
# to the original", not "the tool is nondeterministic". bash qualifies too:
# re-running a command is not guaranteed to reproduce its output (files,
# processes and remote state change), and it may be slow or have side
# effects, so the omitted lines of a cut bash result are only recoverable
# from here.
NON_REFETCHABLE_TOOLS = frozenset({"web_search", "web_fetch", "dag_manage", "bash"})


def compute_hash_key(content: str) -> str:
    """SHA256 of content, truncated to 32 hex chars (128-bit).

    CR-7: widened from 16 (64-bit) to make same-session collisions — which
    would return the wrong cached content via cache_retrieve — negligible.
    """
    return hashlib.sha256(content.encode()).hexdigest()[:32]


async def cache_compressed_result(
    session: AsyncSession,
    agent_id: str,
    session_id: str,
    tool_name: str,
    tool_input: dict,
    original_content: str,
    item_count: int | None = None,
) -> str:
    """Store original content before compression. Returns hash_key."""
    hash_key = compute_hash_key(original_content)

    # Upsert — same session+hash means same content, skip
    stmt = insert(ToolCache).values(
        agent_id=agent_id,
        session_id=session_id,
        hash_key=hash_key,
        tool_name=tool_name,
        tool_input=tool_input,
        original_content=original_content,
        item_count=item_count,
    ).on_conflict_do_nothing(index_elements=["session_id", "hash_key"])
    await session.execute(stmt)
    await session.commit()

    logger.info(
        "Cached %s result [%s] for session %s (%d chars, %s items)",
        tool_name, hash_key, session_id[:8], len(original_content),
        item_count or "n/a",
    )
    return hash_key


async def retrieve_cached_result(
    session: AsyncSession,
    session_id: str,
    hash_key: str,
    query: str | None = None,
    lines: str | None = None,
) -> str | None:
    """Retrieve cached original content by hash_key.

    `lines` ("A-B", "A–B" or "A") returns only those 1-based lines, numbered
    as the gap markers number them. If query is provided, filter items by
    simple keyword matching; with both, the range is taken first.
    """
    stmt = select(ToolCache).where(
        ToolCache.session_id == session_id,
        ToolCache.hash_key == hash_key,
    )
    result = await session.execute(stmt)
    entry = result.scalar_one_or_none()

    if not entry:
        logger.info("Cache miss [%s] for session %s", hash_key, session_id[:8])
        return None

    content = _render_cached(entry.original_content, query=query, lines=lines)
    logger.info(
        "Cache hit [%s] %s (lines=%r, filter=%r) → %d chars",
        hash_key, entry.tool_name, lines, query, len(content),
    )
    return content


# A line range as cache_retrieve takes it: "A-B", "A–B" (the en dash the gap
# markers print) or "A".
_LINES_RE = re.compile(r"^\s*(\d+)\s*(?:[-–]\s*(\d+)\s*)?$")


def _parse_lines(spec: str, total: int) -> tuple[int, int] | str:
    """(A, B), 1-based and inclusive with B clipped to `total`, or a plain
    message saying why `spec` names no lines of a `total`-line result."""
    m = _LINES_RE.match(str(spec))
    if m is None:
        return (
            f'lines="{spec}" is not a line range: use "A-B" or "A" (1-based, inclusive). '
            f"This result has {total} lines."
        )
    a = int(m.group(1))
    b = int(m.group(2)) if m.group(2) is not None else a
    if a < 1:
        return f'lines="{spec}": lines start at 1. This result has {total} lines.'
    if a > b:
        return f'lines="{spec}": the first line is after the last. This result has {total} lines.'
    if a > total:
        return f'lines="{spec}": this result has only {total} lines.'
    return a, min(b, total)


def _render_cached(content: str, query: str | None = None, lines: str | None = None) -> str:
    """What cache_retrieve returns for a cached original.

    `lines` numbers lines over `content.split("\\n")`, the numbering of the
    gap markers, and the output starts with "[result lines A–B of N]".
    `query` filters the whole content, or the range when both are given.
    Every answer is held to _MAX_RETRIEVE_CHARS.
    """
    all_lines = content.split("\n")
    if lines:
        parsed = _parse_lines(lines, len(all_lines))
        if isinstance(parsed, str):
            return parsed
        a, b = parsed
        header = f"[result lines {a}–{b} of {len(all_lines)}]"
        if query:
            selected = "\n".join(all_lines[a - 1 : b])
            return _cap_plain(f"{header}\n{_keyword_filter(selected, query)}")
        return _cap_lines(all_lines, a, b, header)
    if query:
        return _cap_plain(_keyword_filter(content, query))
    return _cap_lines(all_lines, 1, len(all_lines), "")


# One retrieval returns at most this many chars (the footer aside): a whole
# large original would refill the context the cut was made to save.
_MAX_RETRIEVE_CHARS = 30_000


def _cap_lines(all_lines: list[str], a: int, b: int, header: str) -> str:
    """`header` (if any), then lines a..b (1-based, inclusive), cut after the
    last whole line that fits in _MAX_RETRIEVE_CHARS. A cut ends with a footer
    naming the lines not shown and the `lines=` value that fetches them. When
    not even line `a` fits, it is cut at the cap and the footer says so."""
    shown = [header] if header else []
    used = len(header)
    n = a  # the next line to show
    while n <= b:
        cost = len(all_lines[n - 1]) + (1 if shown else 0)  # the line and its newline
        if used + cost > _MAX_RETRIEVE_CHARS:
            break
        shown.append(all_lines[n - 1])
        used += cost
        n += 1
    if n > b:
        return "\n".join(shown)
    if n > a:
        return "\n".join([*shown, f"[… {_not_shown(n, b)} …]"])

    # Not even line a fits: show its start, cut at the cap.
    line = all_lines[a - 1]
    room = _MAX_RETRIEVE_CHARS - used - (1 if shown else 0)
    notes = [f"result line {a} cut at {room} of {len(line)} chars; cache_retrieve cannot page within a line"]
    if a < b:
        notes.append(_not_shown(a + 1, b))
    return "\n".join([*shown, line[:room], f"[… {'; '.join(notes)} …]"])


def _not_shown(k: int, b: int) -> str:
    """The footer text for lines k..b left out by the cap."""
    return f'result lines {k}–{b} not shown ({b - k + 1} lines); call cache_retrieve with lines="{k}-{b}"'


def _cap_plain(text: str) -> str:
    """A query answer held to _MAX_RETRIEVE_CHARS: cut at the cap (a filtered
    JSON array is one line), with a plain footer."""
    if len(text) <= _MAX_RETRIEVE_CHARS:
        return text
    footer = f"[… query result cut at {_MAX_RETRIEVE_CHARS} of {len(text)} chars; narrow the query …]"
    return f"{text[:_MAX_RETRIEVE_CHARS]}\n{footer}"


def _keyword_filter(content: str, query: str) -> str:
    """Simple keyword-based filtering within cached content.

    For JSON arrays: filter items containing query terms.
    For text: filter lines containing query terms.
    """
    query_terms = query.lower().split()

    # Try JSON array
    stripped = content.strip()
    if stripped.startswith("["):
        try:
            items = json.loads(stripped)
            if isinstance(items, list) and items and isinstance(items[0], dict):
                matched = [
                    item for item in items
                    if any(
                        term in json.dumps(item, default=str).lower()
                        for term in query_terms
                    )
                ]
                if matched:
                    return json.dumps(matched[:20], default=str, indent=None)
                return f"No items matching '{query}' in cached {len(items)} results."
        except (json.JSONDecodeError, TypeError):
            pass

    # Text: filter lines
    lines = content.split("\n")
    matched = [
        ln for ln in lines
        if any(term in ln.lower() for term in query_terms)
    ]
    if matched:
        return "\n".join(matched[:50])
    return f"No lines matching '{query}' in cached {len(lines)} lines."


async def has_cache_entries(session: AsyncSession, session_id: str) -> bool:
    """Check if any cache entries exist for this session."""
    stmt = select(ToolCache.id).where(
        ToolCache.session_id == session_id,
    ).limit(1)
    result = await session.execute(stmt)
    return result.scalar_one_or_none() is not None


# The Cached Results context section lists only the newest few: with bash
# cached, a long session holds many entries, and the newest marker already
# names its own key.
_MAX_CACHE_HINTS = 5


async def get_cache_hints(session: AsyncSession, session_id: str) -> list[str]:
    """One line per cached result, the _MAX_CACHE_HINTS newest first."""
    stmt = (
        select(
            ToolCache.hash_key,
            ToolCache.tool_name,
            ToolCache.tool_input,
            ToolCache.item_count,
        )
        .where(ToolCache.session_id == session_id)
        .order_by(ToolCache.compressed_at.desc())
        .limit(_MAX_CACHE_HINTS)
    )
    result = await session.execute(stmt)
    rows = result.all()

    hints = []
    for hash_key, tool_name, tool_input, item_count in rows:
        input_summary = ""
        if tool_input:
            # Extract first argument value as summary, on one line (a bash
            # command can span several).
            first_val = next(iter(tool_input.values()), "")
            if isinstance(first_val, str):
                one_line = " ".join(first_val.split())
                input_summary = f'("{one_line[:60]}")'
        count_info = f"{item_count} items" if item_count else "content"
        hints.append(
            f"- {tool_name}{input_summary}: {count_info}, cache_retrieve key {hash_key}; "
            'lines="A-B" fetches a range.'
        )
    return hints


async def cleanup_session_cache(session: AsyncSession, session_id: str) -> int:
    """Delete all cache entries for a session. Returns count deleted."""
    stmt = delete(ToolCache).where(ToolCache.session_id == session_id)
    result = await session.execute(stmt)
    await session.commit()
    count = result.rowcount or 0
    if count:
        logger.info("Cleaned up %d cache entries for session %s", count, session_id[:8])
    return count
