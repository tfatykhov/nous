"""F105: shared helpers for the spreading activation v2 tests (Tasks 3-8)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from nous.brain.spreading_activation import spreading_activation_search

# The SQL ``spreading_activation_search`` sends with every F105 flag off; see
# tests/test_spreading_sql_identity.py for how it was made and when it may change.
SNAPSHOT = Path(__file__).parent / "fixtures" / "spreading_sql_default.sql"
# Two seeds of two types and a non-empty exclusion, so every default fragment
# (the VALUES list and the excluded_ids condition) is in the rendered text.
SEEDS: list[tuple[UUID, str, float]] = [
    (UUID("00000000-0000-0000-0000-0000000000a1"), "fact", 0.9),
    (UUID("00000000-0000-0000-0000-0000000000a2"), "decision", 0.6),
]
EXCLUDE: set[UUID] = {SEEDS[0][0]}


async def insert_edge(
    session: AsyncSession,
    agent_id: str,
    src: UUID,
    tgt: UUID,
    *,
    weight: float = 1.0,
    relation: str = "related_to",
    method: str | None = "inferred",
    src_type: str = "decision",
    tgt_type: str = "decision",
) -> None:
    """Insert one ``brain.graph_edges`` row; nodes may be bare UUIDs (no FK).

    ``method=None`` leaves ``extraction_method`` out of the INSERT, so the
    column default applies (the column is NOT NULL). ``flush``, never
    ``commit``: the ``session`` fixture rolls back, and a commit would leak rows
    into whatever database the suite is pointed at.
    """
    cols = "source_id,target_id,source_type,target_type,agent_id,relation,weight,auto_linked"
    vals = ":s,:t,:st,:tt,:a,:r,:w,true"
    params: dict[str, object] = {
        "s": str(src), "t": str(tgt), "st": src_type, "tt": tgt_type,
        "a": agent_id, "r": relation, "w": weight,
    }
    if method is not None:
        cols += ",extraction_method"
        vals += ",:m"
        params["m"] = method
    await session.execute(text(f"INSERT INTO brain.graph_edges ({cols}) VALUES ({vals})"), params)
    await session.flush()


class _EmptyResult:
    def all(self) -> list[Any]:
        return []


class RecordingSession:
    """Fake session: ``execute`` stores the rendered SQL and returns no rows."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    async def execute(self, sql: Any, params: dict[str, Any] | None = None) -> _EmptyResult:
        self.statements.append(str(sql))
        return _EmptyResult()


async def render(settings: Any, **kwargs: Any) -> str:
    """The SQL ``spreading_activation_search`` sends for the fixed call shape."""
    session = RecordingSession()
    await spreading_activation_search(
        session, "agent-x", SEEDS, settings, exclude_ids=EXCLUDE, **kwargs,  # type: ignore[arg-type]
    )
    assert len(session.statements) == 1, session.statements
    return session.statements[0]
