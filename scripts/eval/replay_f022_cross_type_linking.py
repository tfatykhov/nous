"""F056 follow-up D1: replay F022 cross-type linking on existing facts.

Production GraphLinker fires on every `fact_learned` event via FactGraphLinker
handler (`nous/handlers/fact_graph_linker.py`). F051.5 ingest never wired the
event bus, so the 929 facts in the LongMemEval corpus never went through F022
cross-type linking. This script retroactively runs `link_fact_to_decisions` +
`link_fact_to_facts` for every existing fact.

Usage (from repo root):

    NOUS_EVAL_DB_NAME=nous_eval_scratch \
      uv run python scripts/eval/replay_f022_cross_type_linking.py

Cost: $0 (only OpenAI embeddings — facts already have embeddings cached;
new edges trigger occasional re-embed for common-template construction).
Runtime: ~5-10 min on 929 facts depending on how many decisions exist.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time

from sqlalchemy import text

from nous.brain.embeddings import EmbeddingProvider
from nous.brain.graph_linker import GraphLinker
from nous.config import Settings
from nous.storage.database import Database
from nous_eval.config import EvalSettings
from nous_eval.density_eval import _snapshot
from nous_eval.retrieval_runner import _settings_for_eval_db


_AGENT_ID = "nous-lme-corpus"


async def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger = logging.getLogger(__name__)

    eval_settings = EvalSettings()
    main_settings = Settings().model_copy(update={"agent_id": _AGENT_ID})
    eval_scoped = _settings_for_eval_db(eval_settings, main_settings).model_copy(update={
        "agent_id": _AGENT_ID,
        "cross_type_linking_enabled": True,
    })

    if not eval_scoped.openai_api_key:
        print("ERROR: OPENAI_API_KEY required.", file=sys.stderr)
        return 2

    db = Database(eval_scoped)
    await db.connect()

    embedder = EmbeddingProvider(
        api_key=eval_scoped.openai_api_key,
        model=eval_scoped.embedding_model,
        dimensions=eval_scoped.embedding_dimensions,
    )

    try:
        # Snapshot BEFORE
        before = await _snapshot(db, _AGENT_ID)
        logger.info("BEFORE D1: %d edges", before.edge_count_total)

        graph_linker = GraphLinker(
            db=db, embedder=embedder,
            settings=eval_scoped, agent_id=_AGENT_ID,
        )

        # Fetch all facts for the LongMemEval agent_id
        async with db.session() as session:
            result = await session.execute(
                text(
                    "SELECT id, content FROM heart.facts "
                    "WHERE agent_id = :aid AND active = true "
                    "ORDER BY created_at"
                ),
                {"aid": _AGENT_ID},
            )
            facts = list(result)

        logger.info("Replaying F022 on %d facts", len(facts))

        t0 = time.monotonic()
        decision_edges_total = 0
        fact_edges_total = 0
        errors = 0

        for i, row in enumerate(facts, start=1):
            fact_id = row.id
            fact_content = row.content
            try:
                async with db.session() as link_session:
                    de = await graph_linker.link_fact_to_decisions(
                        fact_id=fact_id, fact_content=fact_content,
                        session=link_session,
                    )
                    fe = await graph_linker.link_fact_to_facts(
                        fact_id=fact_id, fact_content=fact_content,
                        session=link_session,
                    )
                    if de or fe:
                        await link_session.commit()
                    decision_edges_total += len(de)
                    fact_edges_total += len(fe)
            except Exception:
                errors += 1
                if errors <= 5:
                    logger.exception("F022 link failed for fact %s", fact_id)

            if i % 100 == 0:
                elapsed = time.monotonic() - t0
                logger.info(
                    "Progress: %d/%d facts (%.1fs elapsed, %d dec edges + %d fact edges, %d errors)",
                    i, len(facts), elapsed, decision_edges_total, fact_edges_total, errors,
                )

        wall = time.monotonic() - t0

        # Snapshot AFTER
        after = await _snapshot(db, _AGENT_ID)

        # Report
        edge_delta = after.edge_count_total - before.edge_count_total
        print()
        print("=" * 70)
        print(f"F022 CROSS-TYPE REPLAY (D1) DELTA REPORT")
        print("=" * 70)
        print(f"Wall time: {wall:.1f}s ({wall/60:.1f} min)")
        print(f"Facts processed: {len(facts)}")
        print(f"Errors: {errors}")
        print()
        print(f"link_fact_to_decisions edges: {decision_edges_total}")
        print(f"link_fact_to_facts edges:     {fact_edges_total}")
        print(f"Reported total:               {decision_edges_total + fact_edges_total}")
        print()
        print(f"Edges in DB:  {before.edge_count_total} -> {after.edge_count_total} (d={edge_delta:+d})")
        for relation in sorted(set(before.edge_count_per_relation) | set(after.edge_count_per_relation)):
            b = before.edge_count_per_relation.get(relation, 0)
            a = after.edge_count_per_relation.get(relation, 0)
            print(f"  {relation:30s}: {b} -> {a} (d={a-b:+d})")
        print()
        print("Orphans by type (lower = better):")
        for t in sorted(set(before.orphan_count_per_type) | set(after.orphan_count_per_type)):
            b = before.orphan_count_per_type.get(t, 0)
            a = after.orphan_count_per_type.get(t, 0)
            print(f"  {t:20s}: {b} -> {a} (d={a-b:+d})")
        print("=" * 70)

    finally:
        await db.disconnect()

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
