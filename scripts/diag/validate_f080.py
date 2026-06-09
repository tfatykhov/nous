"""F080 + §14 validation against the fresh prod snapshot (nous_eval_prod @ 5433).

Exercises the REAL code paths (run_recall_pipeline, ContextEngine._select_procedures)
against agent nous-default's post-dedup data + real graph, and prints the metrics the
turn-on decision needs:

  Part A (F080 coherent ranking): does recall_deep exclude censors+procedures when ON,
    keep them when OFF, and does excluding them surface MORE knowledge in the top-K?
  Part B (§14 graph-primary): structural coverage, realistic graph hit-rate over real
    queries, body-preload sample, and the active-filter (no archived skill resurfaces).

Run: uv run python scripts/diag/validate_f080.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# --- env: prod flags/keys, but point at the eval snapshot DB + nous-default ---
SNAP = Path(".env.prod-snapshot")
for raw in SNAP.read_text(encoding="utf-8").splitlines():
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    v = v.strip().strip('"').strip("'")
    os.environ[k.strip()] = v
os.environ.update({
    "DB_HOST": "127.0.0.1", "DB_PORT": "5433", "DB_NAME": "nous_eval_prod",
    "DB_USER": "nous", "DB_PASSWORD": "nous_eval", "NOUS_AGENT_ID": "nous-default",
    "NOUS_MCP_ENABLED": "false", "NOUS_HEARTBEAT_ENABLED": "false",
    "NOUS_SCHEDULE_ENABLED": "false", "NOUS_EVENT_BUS_ENABLED": "false",
})

from nous.config import Settings  # noqa: E402
from nous.storage.database import Database  # noqa: E402
from nous.brain.embeddings import EmbeddingProvider  # noqa: E402
from nous.brain.brain import Brain  # noqa: E402
from nous.heart.heart import Heart  # noqa: E402
from nous.cognitive.context import ContextEngine  # noqa: E402
from nous.api.retrieval_pipeline import run_recall_pipeline  # noqa: E402

QUERIES = [
    "how do I deploy nous to production",
    "run the retrieval evaluation harness",
    "send an email to a user",
    "investigate a production incident",
    "what did we decide about cross-encoder reranking",
    "consolidate duplicate procedures",
    "create a heartbeat check",
    "backfill graph edges during sleep",
    "conduct deep multi-source research",
    "fix a failing test in the retrieval pipeline",
    "review a pull request before merge",
    "summarize recent work and episodes",
]


async def main() -> None:
    s = Settings()
    db = Database(s)
    await db.connect()
    emb = EmbeddingProvider(
        api_key=s.openai_api_key, model=s.embedding_model,
        dimensions=getattr(s, "embedding_dimensions", 1536),
    )
    brain = Brain(db, s, emb)
    heart = Heart(db, s, emb, owns_embeddings=False)
    engine = ContextEngine(brain, heart, s, identity_prompt="You are Nous.")

    s_off = s.model_copy(update={"coherent_ranking_enabled": False})
    s_on = s.model_copy(update={"coherent_ranking_enabled": True})

    print(f"model={s.embedding_model} dims={getattr(s,'embedding_dimensions',1536)} "
          f"agent={s.agent_id} db={s.db_name}\n")

    # ---------------- Part A: F080 coherent ranking ----------------
    print("=" * 70)
    print("PART A — F080 coherent ranking (recall_deep type composition, top-10)")
    print("=" * 70)
    off_types_tot, on_types_tot = Counter(), Counter()
    a_violation = 0
    for q in QUERIES:
        roff, _ = await run_recall_pipeline(query=q, heart=heart, brain=brain,
                                            settings=s_off, limit=10, memory_types=["all"])
        ron, _ = await run_recall_pipeline(query=q, heart=heart, brain=brain,
                                           settings=s_on, limit=10, memory_types=["all"])
        coff = Counter(r.type for r in roff[:10])
        con = Counter(r.type for r in ron[:10])
        off_types_tot.update(coff)
        on_types_tot.update(con)
        bad = con.get("censor", 0) + con.get("procedure", 0)
        if bad:
            a_violation += 1
        print(f"  q={q[:42]:44s} OFF={dict(coff)}  ON={dict(con)}")
    print(f"\n  AGGREGATE top-10 types  OFF: {dict(off_types_tot)}")
    print(f"  AGGREGATE top-10 types  ON : {dict(on_types_tot)}")
    print(f"  ON censor/procedure leak: {a_violation}/{len(QUERIES)} queries  "
          f"(must be 0)")
    print(f"  knowledge (fact+episode+decision+chunk) in top-10  "
          f"OFF={sum(off_types_tot[t] for t in ('fact','episode','decision','chunk'))}  "
          f"ON={sum(on_types_tot[t] for t in ('fact','episode','decision','chunk'))}")

    # ---------------- Part B: §14 graph-primary ----------------
    print("\n" + "=" * 70)
    print("PART B — §14 graph-primary procedure selection")
    print("=" * 70)
    async with db.session() as sess:
        from sqlalchemy import text
        cov = (await sess.execute(text("""
            SELECT
              (SELECT count(DISTINCT seed) FROM (
                  SELECT source_id seed FROM brain.graph_edges e JOIN heart.procedures p
                    ON p.id=e.target_id AND p.active WHERE e.source_type='fact' AND e.target_type='procedure' AND e.agent_id='nous-default'
                  UNION SELECT target_id FROM brain.graph_edges e JOIN heart.procedures p
                    ON p.id=e.source_id AND p.active WHERE e.target_type='fact' AND e.source_type='procedure' AND e.agent_id='nous-default'
              ) x) facts_linked,
              (SELECT count(*) FROM heart.facts WHERE agent_id='nous-default') facts_total,
              (SELECT count(*) FROM heart.procedures WHERE agent_id='nous-default' AND NOT active
                 AND id IN (SELECT source_id FROM brain.graph_edges UNION SELECT target_id FROM brain.graph_edges)) archived_with_edges
        """))).first()
    print(f"  structural coverage: {cov.facts_linked}/{cov.facts_total} facts link to an "
          f"ACTIVE procedure ({100*cov.facts_linked/max(1,cov.facts_total):.1f}%)")
    print(f"  archived procedures that still carry graph edges (resurrection risk): "
          f"{cov.archived_with_edges}")

    hits = 0
    n_sel_tot = 0
    samples = []
    archived_leak = 0
    async with db.session() as sess:
        # ids of archived (inactive) procedures — for the active-filter assertion
        from sqlalchemy import text
        archived_ids = {
            str(r[0]) for r in (await sess.execute(text(
                "SELECT id FROM heart.procedures WHERE agent_id='nous-default' AND NOT active"
            ))).all()
        }
        for q in QUERIES:
            facts = await heart.search_facts(q, limit=12, session=sess)
            decs = await brain.query(q, limit=6)
            rid = {"fact": [str(f.id) for f in facts], "decision": [str(d.id) for d in decs]}
            smap = {}
            for f in facts:
                smap[str(f.id)] = getattr(f, "score", 0.0) or 0.0
            for d in decs:
                smap[str(d.id)] = getattr(d, "score", 0.0) or 0.0
            selected = await engine._select_procedures(
                slots=5, critic_skills=[], recalled_ids=rid,
                recalled_score_map=smap, session=sess,
            )
            if selected:
                hits += 1
                n_sel_tot += len(selected)
            for p in selected:
                if str(p.id) in archived_ids:
                    archived_leak += 1
                if not p.active:
                    archived_leak += 1
            if selected and len(samples) < 3:
                blocks = engine._format_procedure_bodies(selected, 240)
                samples.append((q, [p.name for p in selected], blocks[0] if blocks else ""))
            print(f"  q={q[:42]:44s} graph-selected: "
                  f"{[p.name for p in selected] or '—'}")

    print(f"\n  GRAPH HIT-RATE: {hits}/{len(QUERIES)} queries activated >=1 procedure "
          f"({100*hits/len(QUERIES):.0f}%)  avg/hit={n_sel_tot/max(1,hits):.1f}")
    print(f"  active-filter: archived/inactive procedures selected = {archived_leak} "
          f"(must be 0)")
    print("\n  --- sample preloaded body (truncated 240) ---")
    for q, names, body in samples:
        print(f"  [{q[:40]}] -> {names}")
        print("    " + body.replace("\n", "\n    ")[:300])
        print()

    await db.disconnect() if hasattr(db, "disconnect") else None


if __name__ == "__main__":
    asyncio.run(main())
