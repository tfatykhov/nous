"""F105: shared helpers for the spreading activation v2 tests (Tasks 3-8)."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


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
