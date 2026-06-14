"""Delete + rebuild happened_before edges for one agent on the DB selected by
DB_* env, using the current happened_before_relatedness_threshold setting.

Used to measure the F075 edge-relatedness gate without re-classifying dates.

    DB_HOST=localhost DB_PORT=5433 DB_USER=nous DB_PASSWORD=nous_eval \\
    DB_NAME=nous_eval_prod AGENT=nous-default PYTHONPATH=. \\
    uv run python scripts/diag/rebuild_happened_before.py
"""

from __future__ import annotations

import asyncio
import os

from sqlalchemy import text

from nous.brain.embeddings import EmbeddingProvider
from nous.brain.graph_densifier import GraphDensifier
from nous.brain.graph_linker import GraphLinker
from nous.config import Settings
from nous.storage.database import Database

AGENT = os.environ.get("AGENT", "nous-default")


async def main() -> None:
    s = Settings()
    db = Database(s)
    await db.connect()
    try:
        async with db.session() as session:
            res = await session.execute(
                text(
                    "DELETE FROM brain.graph_edges WHERE agent_id = :a "
                    "AND relation = 'happened_before'"
                ),
                {"a": AGENT},
            )
            await session.commit()
            print(f"deleted {res.rowcount} existing happened_before edges")

        embedder = EmbeddingProvider(api_key=s.openai_api_key, model=s.embedding_model)
        linker = GraphLinker(db, embedder, s, AGENT)
        densifier = GraphDensifier(db, linker, embedder, s, AGENT)
        n = await densifier._build_happened_before_edges()
        print(f"built {n} happened_before edges "
              f"(threshold={s.happened_before_relatedness_threshold})")
    finally:
        await db.engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
