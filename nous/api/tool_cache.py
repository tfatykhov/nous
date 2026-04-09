"""ReversibleCache — Postgres-backed cache for compressed tool results.

When SmartCompress compresses a non-re-fetchable tool result (web_search,
web_fetch), the original is stored here. The model can retrieve it via
cache_retrieve tool.
"""

from __future__ import annotations

import hashlib
import json
import logging

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from nous.storage.models import ToolCache

logger = logging.getLogger(__name__)

# Tools whose results cannot be re-fetched (different results each time)
NON_REFETCHABLE_TOOLS = frozenset({"web_search", "web_fetch"})


def compute_hash_key(content: str) -> str:
    """SHA256 of content, truncated to 16 hex chars."""
    return hashlib.sha256(content.encode()).hexdigest()[:16]


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
) -> str | None:
    """Retrieve cached original content by hash_key.

    If query is provided, filter items by simple keyword matching.
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

    content = entry.original_content

    if query:
        content = _keyword_filter(content, query)
        logger.info(
            "Cache hit [%s] %s with filter %r → %d chars",
            hash_key, entry.tool_name, query, len(content),
        )
    else:
        logger.info(
            "Cache hit [%s] %s → %d chars (full content)",
            hash_key, entry.tool_name, len(content),
        )

    return content


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


async def get_cache_hints(session: AsyncSession, session_id: str) -> list[str]:
    """Get human-readable hints about available cached results."""
    stmt = select(
        ToolCache.hash_key,
        ToolCache.tool_name,
        ToolCache.tool_input,
        ToolCache.item_count,
    ).where(ToolCache.session_id == session_id)
    result = await session.execute(stmt)
    rows = result.all()

    hints = []
    for hash_key, tool_name, tool_input, item_count in rows:
        input_summary = ""
        if tool_input:
            # Extract first argument value as summary
            first_val = next(iter(tool_input.values()), "")
            if isinstance(first_val, str):
                input_summary = f'("{first_val[:60]}")'
        count_info = f"{item_count} items" if item_count else "content"
        hints.append(
            f"- [{hash_key}] {tool_name}{input_summary}: {count_info}. "
            f'Use cache_retrieve("{hash_key}") for full results.'
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
