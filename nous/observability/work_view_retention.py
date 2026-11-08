"""F108: the retention sweep of the work-view records.

``sweep_tool_calls`` deletes this agent's ``nous_system.tool_call_log`` and ``nous_system.agent_turn_log`` rows that
started more than ``NOUS_TOOL_CALL_RETENTION_DAYS`` days ago. main.py runs it once a day while the log is on
(``_tool_call_retention_loop``). It raises on failure; the loop logs it at WARNING.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

# Agent-scoped: the tables are, and the retention setting is per process, so an unscoped DELETE would let one agent
# remove the rows of another that keeps them longer. Both tables index (agent_id, started_at DESC) for this (migration
# 100).
_DELETE_TOOL_CALLS = text(
    "DELETE FROM nous_system.tool_call_log "
    "WHERE agent_id = :agent_id AND started_at < now() - make_interval(days => :d)"
)
_DELETE_TURNS = text(
    "DELETE FROM nous_system.agent_turn_log "
    "WHERE agent_id = :agent_id AND started_at < now() - make_interval(days => :d)"
)


async def sweep_tool_calls(database: Any, agent_id: str, days: int) -> dict[str, int]:
    """Delete ``agent_id``'s tool-call and turn rows that started more than ``days`` days ago, in one transaction.

    Returns the rows deleted per table. Raises ``ValueError`` for ``days`` below 1 (a zero-day interval would delete
    every row), and whatever the database raises (then nothing is deleted).
    """
    if days < 1:
        raise ValueError(f"tool-call retention must be at least 1 day, got {days}")
    params = {"agent_id": agent_id, "d": days}
    async with database.session() as session:
        calls = await session.execute(_DELETE_TOOL_CALLS, params)
        turns = await session.execute(_DELETE_TURNS, params)
        await session.commit()
    return {"tool_call_log": calls.rowcount, "agent_turn_log": turns.rowcount}
