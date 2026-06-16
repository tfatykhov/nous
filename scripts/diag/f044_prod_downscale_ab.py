"""F044 SPEC MECHANISM (Phase 8d α-downscale) on REAL PROD graph-targeted data.

Tests the spec's ACTUAL retrieval mechanism (not the B2 substitute): after
warm-up consolidation, multiplicatively decay TAGGED edge weights by α
(consolidated exempt) so consolidated edges become relatively dominant in every
weight-based graph consumer. Then measure graph-targeted retrieval vs baseline.

Isolation: snapshot ALL edge weights up front; restore after every arm; reset
consolidation + assert clean each arm; assert edge count stable.
"""
import asyncio, os, sys
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import asyncpg
from nous.config import Settings
from nous.storage.database import Database
from nous.heart import Heart
from nous.brain.brain import Brain
from nous.brain.embeddings import EmbeddingProvider
from nous.api.retrieval_pipeline import run_recall_pipeline
from nous.brain.tinyhippo_lite import (
    flush_recall_touches, stc_promote_and_measure, homeostatic_downscale, _RECALL_TOUCH_BUFFER,
)
from nous_eval.config import EvalSettings
from nous_eval.qrels_loader import load_qrels
from nous_eval.retrieval_runner import RetrievalConfig, run_matrix
from nous_eval.metrics import compute_metrics, compute_delta

AGENT = "nous-default"
QRELS_PATH = Path("E:/Projects/nous-eval-fixtures/v2026-Q2/qrels_graph_targeted.jsonl")
DSN = "postgresql://nous:nous_eval@localhost:5433/nous_eval_prod"
GRAPH = {"graph_recall_enabled": True, "graph_adjacency_boost_enabled": True,
         "heart_graph_all_types_enabled": True, "tinyhippo_lite_enabled": False}

def env_key(name):
    try:
        for line in open(".env", encoding="utf-8"):
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return os.environ.get(name, "")

async def counts(conn):
    cons = await conn.fetchval(f"SELECT count(*) FROM brain.graph_edges WHERE agent_id='{AGENT}' AND consolidation_state='consolidated'")
    edges = await conn.fetchval(f"SELECT count(*) FROM brain.graph_edges WHERE agent_id='{AGENT}'")
    ltp = await conn.fetchval(f"SELECT count(*) FROM brain.graph_edges WHERE agent_id='{AGENT}' AND ltp_count<>0")
    return cons, edges, ltp

async def reset_assert(conn):
    await conn.execute(f"UPDATE brain.graph_edges SET consolidation_state='tagged', ltp_count=0, last_ltp_at=NULL WHERE agent_id='{AGENT}' AND (ltp_count<>0 OR consolidation_state<>'tagged')")
    cons, edges, ltp = await counts(conn)
    assert cons == 0 and ltp == 0, f"RESET FAILED cons={cons} ltp={ltp}"
    return edges

async def snapshot_weights(conn):
    # Crash-safe DB-side snapshot: persists even if the script dies mid-run.
    await conn.execute("DROP TABLE IF EXISTS public._f044_wsnap")
    await conn.execute(f"CREATE TABLE public._f044_wsnap AS SELECT id, weight FROM brain.graph_edges WHERE agent_id='{AGENT}'")
    return await conn.fetchval("SELECT count(*) FROM public._f044_wsnap")

async def restore_weights(conn):
    await conn.execute("UPDATE brain.graph_edges g SET weight = s.weight FROM public._f044_wsnap s WHERE g.id = s.id")

async def warmup(db, embedder, queries):
    _RECALL_TOUCH_BUFFER.clear()
    s = Settings().model_copy(update={"agent_id": AGENT, "tinyhippo_lite_enabled": True})
    heart = Heart(db, s, embedding_provider=embedder)
    brain = Brain(db, s, embedding_provider=embedder)
    for q in queries:
        try:
            await run_recall_pipeline(q, heart, brain, s, limit=10)
        except Exception:
            pass
    async with db.session() as sess:
        await flush_recall_touches(sess, AGENT)
        st = await stc_promote_and_measure(sess, AGENT, s.tinyhippo_prp_threshold)
        await sess.commit()
    return st["f044_n_consolidated"]

async def run_cfg(name, qrels, eval_settings, template, top_k):
    res = (await run_matrix([RetrievalConfig(name=name, flags=GRAPH)], qrels, eval_settings, template, top_k=top_k))[0]
    return compute_metrics(res.per_qrel, top_k)

async def main():
    qrels = load_qrels(QRELS_PATH, review_filter_enabled=False)
    eval_settings = EvalSettings()
    assert eval_settings.db_name == "nous_eval_prod" and eval_settings.agent_id == AGENT
    template = Settings()
    top_k = eval_settings.top_k
    embedder = EmbeddingProvider(env_key("OPENAI_API_KEY"), "text-embedding-3-large", 1536)
    conn = await asyncpg.connect(DSN)
    db = Database(Settings().model_copy(update={"agent_id": AGENT}))
    await db.connect()
    print(f"prod graph-targeted A/B (α-downscale, spec Phase 8d), n={len(qrels)} qrels")
    try:
        edges0 = await reset_assert(conn)
        nsnap = await snapshot_weights(conn)
        print(f"snapshot {nsnap} edge weights (DB-side, crash-safe); edges={edges0}")

        m_base = await run_cfg("baseline", qrels, eval_settings, template, top_k)
        print(f"baseline: MRR={m_base.mrr:.4f} nDCG@10={m_base.ndcg_at_10:.4f}")

        async def downscale_arm(label, queries, alpha_eff):
            await reset_assert(conn)
            ncons = await warmup(db, embedder, queries)
            async with db.session() as sess:
                nd = await homeostatic_downscale(sess, AGENT, alpha_eff)
                await sess.commit()
            e = (await counts(conn))[1]; assert e == edges0, f"edges changed {e}"
            m = await run_cfg(f"f044_{label}", qrels, eval_settings, template, top_k)
            dM = compute_delta(m_base, m, "mrr").absolute
            dN = compute_delta(m_base, m, "ndcg_at_10").absolute
            print(f"  {label:<22} consolidated={ncons} downscaled={nd} α_eff={alpha_eff:.3f} | MRR={m.mrr:.4f} ({dM:+.4f}) nDCG@10={m.ndcg_at_10:.4f} ({dN:+.4f})")
            await restore_weights(conn)
            return m

        print("\n=== α-downscale arms (warm-up -> downscale tagged -> retrieve) ===")
        # content (non-leaky) at increasing downscale strength
        content_q = [r[0][:240] for r in await conn.fetch(f"SELECT content FROM heart.facts WHERE agent_id='{AGENT}' AND content IS NOT NULL AND length(content)>20 ORDER BY id LIMIT 120")]
        await downscale_arm("content_α0.75", content_q, 0.75)
        await downscale_arm("content_α0.42", content_q, 0.42)
        await downscale_arm("content_α0.18", content_q, 0.18)
        # self (best-case)
        await downscale_arm("self_α0.42", [q.query for q in qrels], 0.42)

        await reset_assert(conn)
        await restore_weights(conn)
        print("\nrestored weights + reset consolidation (corpus pristine)")
    finally:
        await conn.close()
        await db.disconnect()

asyncio.run(main())
