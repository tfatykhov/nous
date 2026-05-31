"""One-time full-corpus co-mention edge backfill (F076).

The sleep-cycle builder (`GraphDensifier.build_comention_edges`) only scans the
NEWEST `comention_max_facts_per_cycle` (default 5000) facts AND chunks, so on a
corpus larger than that the older nodes never get co-mention edges. This script runs
the SAME shipping builder over the WHOLE corpus in a single pass (co-mention MUST see
all nodes at once — paginating the entity match would miss cross-batch shared-entity
pairs), per agent. Links fact<->fact and chunk<->chunk (same-type only).

Deterministic + idempotent: regex entity extraction + INSERT ... ON CONFLICT DO
NOTHING, with the hub degree-cap, per-fact fan-out cap, and the prior-edge skip
(any existing fact-fact edge, incl. contradicts). No LLM, no embeddings.

SAFE BY DEFAULT: dry-run (preview yield only) unless you pass --commit. Connects to
whatever DB the environment points at — it prints the resolved target so you can
confirm you are hitting prod before writing.

  # preview on nous-default against the env-configured DB
  uv run python -m scripts.backfill_comention_edges
  # actually build
  uv run python -m scripts.backfill_comention_edges --commit
  # other agents
  uv run python -m scripts.backfill_comention_edges --agent nous-default --agent foo --commit

PREREQ: F076 must be deployed (this imports the F076 builder). The retrieval payoff
is gated on the consumer flags — prod has heart_graph_all_types + adjacency_boost ON
(so backfilled edges affect ranking immediately) but graph_neighbor_seed_score OFF.
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


async def backfill(agents: list[str], commit: bool, max_facts_override: int | None) -> int:
    from nous.brain.graph_densifier import GraphDensifier
    from nous.brain.graph_linker import GraphLinker
    from nous.config import Settings
    from nous.storage.database import Database

    s = Settings()
    db = Database(s)
    await db.connect()
    print(f"DB target: host={s.db_host} port={s.db_port} db={s.db_name} user={s.db_user}")
    print(f"mode: {'COMMIT (writing edges)' if commit else 'DRY-RUN (preview only, no writes)'}")
    print(f"comention_weight (new edges) = {s.comention_weight}\n")

    total_built = 0
    try:
        for agent in agents:
            async with db.session() as sess:
                n_facts = await _count(sess, "SELECT count(*) FROM heart.facts WHERE agent_id=:a AND active=TRUE", agent)
                n_existing = await _count(
                    sess,
                    "SELECT count(*) FROM brain.graph_edges WHERE agent_id=:a AND extraction_method='co_mention'",
                    agent,
                )
            max_facts = max_facts_override if max_facts_override is not None else max(n_facts, 1)
            # Force the builder on + lift the per-cycle cap to cover the whole corpus.
            s_agent = s.model_copy(update={
                "comention_linking_enabled": True,
                "comention_max_facts_per_cycle": max_facts,
            })
            linker = GraphLinker(db, None, s_agent, agent)  # embedder unused by co-mention
            dens = GraphDensifier(db=db, graph_linker=linker, embedder=None,
                                  settings=s_agent, agent_id=agent)

            would = await dens.build_comention_edges(dry_run=True)
            print(f"[{agent}] active_facts={n_facts} existing_co_mention={n_existing} "
                  f"scan_cap={max_facts}  -> WOULD build {would} new edges")
            if commit and would:
                built = await dens.build_comention_edges()
                async with db.session() as sess:
                    final = await _count(
                        sess,
                        "SELECT count(*) FROM brain.graph_edges WHERE agent_id=:a AND extraction_method='co_mention'",
                        agent,
                    )
                print(f"[{agent}] BUILT {built} edges  -> co_mention total now {final}")
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
    ap = argparse.ArgumentParser(description="F076 one-time full-corpus co-mention edge backfill")
    ap.add_argument("--agent", action="append", dest="agents",
                    help="agent_id to backfill (repeatable; default nous-default)")
    ap.add_argument("--commit", action="store_true",
                    help="actually write edges (default: dry-run preview)")
    ap.add_argument("--max-facts", type=int, default=None,
                    help="override scan cap (default: per-agent active fact count = full corpus)")
    args = ap.parse_args()
    agents = args.agents or ["nous-default"]
    asyncio.run(backfill(agents, args.commit, args.max_facts))


if __name__ == "__main__":
    main()
