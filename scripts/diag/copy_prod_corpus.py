"""One-off: direct-copy the current prod corpus (agent_id=nous-default,
text-embedding-3-large @ 1536-dim) into the local eval DB under a distinct
agent_id, reusing the tested nous_eval.corpus_loader for the load side.

Run:
    export PROD_PW=...           # prod DB_PASSWORD
    PYTHONPATH=. DB_HOST=localhost DB_PORT=5432 uv run python scripts/diag/copy_prod_corpus.py
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import os
import tempfile
from pathlib import Path

import asyncpg

from nous.config import Settings
from nous.storage.database import Database
from nous_eval.corpus_loader import load_corpus_from_jsonl

PROD = dict(host="192.168.1.141", port=5432, user="nous",
            password=os.environ["PROD_PW"], database="nous")
PROD_AGENT = "nous-default"
LOCAL_AGENT = "nous-prod-fresh"
FIXTURE_VERSION = "prod-fresh-20260611"

TABLES = [
    ("facts.jsonl", "heart.facts"),
    ("episodes.jsonl", "heart.episodes"),
    ("decisions.jsonl", "brain.decisions"),
    ("procedures.jsonl", "heart.procedures"),
    ("graph_edges.jsonl", "brain.graph_edges"),
]


def _ser(o: object) -> str:
    if isinstance(o, _dt.datetime):
        return o.isoformat()
    if isinstance(o, _dt.date):
        return o.isoformat() + "T00:00:00"
    return str(o)


async def dump_prod(out_dir: Path) -> None:
    conn = await asyncpg.connect(**PROD)
    await conn.set_type_codec("vector", encoder=str, decoder=str,
                              schema="public", format="text")
    try:
        for fname, table in TABLES:
            rows = await conn.fetch(f"SELECT * FROM {table} WHERE agent_id = $1", PROD_AGENT)
            with (out_dir / fname).open("w", encoding="utf-8") as fh:
                for r in rows:
                    d = dict(r)
                    d.pop("search_tsv", None)
                    fh.write(json.dumps(d, default=_ser) + "\n")
            print(f"dumped {len(rows):>6} -> {fname}")
    finally:
        await conn.close()


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="prod-corpus-") as tmp:
        out_dir = Path(tmp)
        await dump_prod(out_dir)
        db = Database(Settings())
        await db.connect()
        try:
            from sqlalchemy import text as _t
            async with db.session() as s:
                for tbl in ("heart.facts", "heart.episodes", "brain.decisions",
                            "heart.procedures", "brain.graph_edges"):
                    await s.execute(_t(f"DELETE FROM {tbl} WHERE agent_id = :a"), {"a": LOCAL_AGENT})
                await s.commit()
            stats = await load_corpus_from_jsonl(db, out_dir, agent_id=LOCAL_AGENT,
                                                 fixture_version=FIXTURE_VERSION)
            print("LOADED:", stats.to_dict())
        finally:
            await db.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
