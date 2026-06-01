"""One-time full-corpus co-occurrence edge backfill (Gap-1 formation).

The sleep-cycle builder (`GraphDensifier.build_cooccurrence_edges`) only scans the NEWEST
`cooccurrence_max_episodes_per_cycle` (default 2000) episodes, so on a corpus with more
episodes the older ones never get co_occurred edges (the scan re-hits the same recent window
each cycle and skips already-linked pairs). This script runs the SAME shipping builder over
the WHOLE corpus in a single pass, per agent, by lifting the episode cap to the agent's
episode count.

Forms relation='co_occurred', extraction_method='co_occurrence' edges between facts that
share a source_episode_id (mentioned together in one occasion), capped by the noise gate
(`cooccurrence_max_episode_facts`, default 6) and the prior-edge skip (any existing fact-fact
edge, incl. contradicts). Deterministic + idempotent: INSERT ... ON CONFLICT DO NOTHING.
No LLM, no embeddings.

SAFE BY DEFAULT: dry-run (preview yield only) unless you pass --commit. Prints the resolved
DB target so you can confirm you are hitting prod before writing.

  # preview on nous-default against the env-configured DB
  uv run python -m scripts.backfill_cooccurrence_edges
  # actually build
  uv run python -m scripts.backfill_cooccurrence_edges --commit
  # other agents
  uv run python -m scripts.backfill_cooccurrence_edges --agent nous-default --agent foo --commit

PREREQ: migration 055 deployed (relation 'co_occurred' + extraction_method 'co_occurrence').
Retrieval payoff is gated on the consumer flags (heart_graph_all_types_enabled,
graph_neighbor_seed_score_enabled, graph_adjacency_boost_enabled) AND the recall_deep
Graph-Connected-Memories prominence interleave (interleaved by score with a [via] marker).
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def _count(sess, sql: str, agent: str) -> int:
    from sqlalchemy import text
    return int((await sess.execute(text(sql), {"a": agent})).scalar() or 0)


async def backfill(agents: list[str], commit: bool, max_episodes_override: int | None) -> int:
    from nous.brain.graph_densifier import GraphDensifier
    from nous.brain.graph_linker import GraphLinker
    from nous.config import Settings
    from nous.storage.database import Database

    s = Settings()
    db = Database(s)
    await db.connect()
    print(f"DB target: host={s.db_host} port={s.db_port} db={s.db_name} user={s.db_user}")
    print(f"mode: {'COMMIT (writing edges)' if commit else 'DRY-RUN (preview only, no writes)'}")
    print(f"cooccurrence_weight (new edges) = {s.cooccurrence_weight}  "
          f"noise gate (max_episode_facts) = {s.cooccurrence_max_episode_facts}\n")

    total_built = 0
    try:
        for agent in agents:
            async with db.session() as sess:
                # episodes that hold >= 2 active facts (the candidate set the builder scans)
                n_eps = await _count(
                    sess,
                    "SELECT count(*) FROM (SELECT source_episode_id FROM heart.facts "
                    "WHERE agent_id=:a AND active=TRUE AND source_episode_id IS NOT NULL "
                    "GROUP BY source_episode_id HAVING count(*) >= 2) q",
                    agent,
                )
                n_existing = await _count(
                    sess,
                    "SELECT count(*) FROM brain.graph_edges WHERE agent_id=:a AND extraction_method='co_occurrence'",
                    agent,
                )
            max_eps = max_episodes_override if max_episodes_override is not None else max(n_eps, 1)
            # Force the builder on + lift the per-cycle episode cap to cover the whole corpus.
            s_agent = s.model_copy(update={
                "cooccurrence_linking_enabled": True,
                "cooccurrence_max_episodes_per_cycle": max_eps,
            })
            linker = GraphLinker(db, None, s_agent, agent)  # embedder unused by co-occurrence
            dens = GraphDensifier(db=db, graph_linker=linker, embedder=None,
                                  settings=s_agent, agent_id=agent)

            would = await dens.build_cooccurrence_edges(dry_run=True)
            print(f"[{agent}] multi-fact_episodes={n_eps} existing_co_occurrence={n_existing} "
                  f"scan_cap={max_eps}  -> WOULD build {would} new edges")
            if commit and would:
                built = await dens.build_cooccurrence_edges()
                async with db.session() as sess:
                    final = await _count(
                        sess,
                        "SELECT count(*) FROM brain.graph_edges WHERE agent_id=:a AND extraction_method='co_occurrence'",
                        agent,
                    )
                print(f"[{agent}] BUILT {built} edges  -> co_occurrence total now {final}")
                total_built += built
            elif commit:
                print(f"[{agent}] nothing to build")
    finally:
        await db.disconnect()

    print(f"\n{'Committed' if commit else 'Dry-run'} done. "
          f"{'Built' if commit else 'Would build'} {total_built if commit else '(see per-agent)'} edges.")
    if not commit:
        print("Re-run with --commit to write. Smoke the yield above first.")
    return total_built


def main() -> None:
    ap = argparse.ArgumentParser(description="Gap-1 one-time full-corpus co-occurrence edge backfill")
    ap.add_argument("--agent", action="append", dest="agents",
                    help="agent_id to backfill (repeatable; default nous-default)")
    ap.add_argument("--commit", action="store_true",
                    help="actually write edges (default: dry-run preview)")
    ap.add_argument("--max-episodes", type=int, default=None,
                    help="override episode scan cap (default: per-agent multi-fact episode count = full corpus)")
    args = ap.parse_args()
    agents = args.agents or ["nous-default"]
    asyncio.run(backfill(agents, args.commit, args.max_episodes))


if __name__ == "__main__":
    main()
