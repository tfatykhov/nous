"""Seed nous_dedup_test (5433) with the real cluster data from _proc_bodies.json.

Inserts the 20 cluster procedures + their affinity rows + incident graph edges so the
consolidation tool can be validated end-to-end against prod-shaped data on a throwaway DB.
Array columns are passed as their PostgreSQL array-literal strings (the canonical output
format is also valid input) and cast in SQL. Connects via DB_* env vars.
"""
from __future__ import annotations

import asyncio
import json
import os

from sqlalchemy import text

from nous.config import Settings
from nous.storage.database import Database

AGENT = os.environ.get("PROC_CONSOLIDATE_AGENT", "nous-default")


def arr(v):
    """PG array-literal string or None -> param value (None stays None)."""
    if v in (None, "", "{}"):
        return None
    return v


async def main() -> int:
    with open(os.path.abspath("scripts/diag/_proc_bodies.json"), encoding="utf-8") as f:
        data = json.load(f)

    db = Database(Settings())
    await db.connect()
    async with db.engine.connect() as conn:
        trans = await conn.begin()
        # idempotent: clear any prior seed for this agent's cluster ids
        ids = [p["id"] for p in data["procedures"]]
        await conn.execute(
            text("DELETE FROM heart.procedure_task_affinity WHERE procedure_id = ANY(CAST(:ids AS uuid[]))"),
            {"ids": ids},
        )
        await conn.execute(
            text("DELETE FROM brain.graph_edges WHERE (source_id = ANY(CAST(:ids AS uuid[])) "
                 "OR target_id = ANY(CAST(:ids AS uuid[])))"),
            {"ids": ids},
        )
        await conn.execute(
            text("DELETE FROM heart.procedures WHERE id = ANY(CAST(:ids AS uuid[]))"),
            {"ids": ids},
        )

        for p in data["procedures"]:
            await conn.execute(
                text(
                    """
                    INSERT INTO heart.procedures
                        (id, agent_id, name, domain, description, tags, goals, core_patterns,
                         core_tools, core_concepts, implementation_notes, censor_ids,
                         related_procedures, activation_count, success_count, failure_count,
                         active, created_at)
                    VALUES
                        (CAST(:id AS uuid), :agent, :name, :domain, :description,
                         CAST(CAST(:tags AS text) AS text[]), CAST(CAST(:goals AS text) AS text[]),
                         CAST(CAST(:core_patterns AS text) AS text[]), CAST(CAST(:core_tools AS text) AS text[]),
                         CAST(CAST(:core_concepts AS text) AS text[]), CAST(CAST(:implementation_notes AS text) AS text[]),
                         CAST(CAST(:censor_ids AS text) AS uuid[]),
                         CAST(CAST(:related_procedures AS text) AS uuid[]), :ac, :sc, :fc, :active,
                         CAST(CAST(:created_at AS text) AS timestamptz))
                    """
                ),
                {
                    "id": p["id"], "agent": AGENT, "name": p["name"], "domain": p["domain"],
                    "description": p["description"], "tags": arr(p["tags"]), "goals": arr(p["goals"]),
                    "core_patterns": arr(p["core_patterns"]), "core_tools": arr(p["core_tools"]),
                    "core_concepts": arr(p["core_concepts"]),
                    "implementation_notes": arr(p["implementation_notes"]),
                    "censor_ids": arr(p["censor_ids"]), "related_procedures": arr(p["related_procedures"]),
                    "ac": p["activation_count"], "sc": p["success_count"], "fc": p["failure_count"],
                    "active": p["active"], "created_at": p["created_at"],
                },
            )

        for a in data["affinity"]:
            await conn.execute(
                text(
                    """
                    INSERT INTO heart.procedure_task_affinity
                        (procedure_id, frame_type, activation_count, success_count,
                         failure_count, active, agent_id)
                    VALUES (CAST(:pid AS uuid), :ft, :ac, :sc, :fc, :active, :agent)
                    """
                ),
                {"pid": a["procedure_id"], "ft": a["frame_type"], "ac": a["activation_count"],
                 "sc": a["success_count"], "fc": a["failure_count"], "active": a["active"], "agent": AGENT},
            )

        for e in data["edges"]:
            await conn.execute(
                text(
                    """
                    INSERT INTO brain.graph_edges
                        (id, source_id, source_type, target_id, target_type, relation, agent_id)
                    VALUES (CAST(:id AS uuid), CAST(:sid AS uuid), :st, CAST(:tid AS uuid), :tt, :rel, :agent)
                    ON CONFLICT (id) DO NOTHING
                    """
                ),
                {"id": e["id"], "sid": e["source_id"], "st": e["source_type"],
                 "tid": e["target_id"], "tt": e["target_type"], "rel": e["relation"], "agent": AGENT},
            )

        await trans.commit()
    await db.disconnect()
    print(f"seeded {len(data['procedures'])} procedures, {len(data['affinity'])} affinity, {len(data['edges'])} edges")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
