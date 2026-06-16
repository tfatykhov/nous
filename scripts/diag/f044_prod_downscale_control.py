"""F044 α-downscale CONFOUND CONTROL on real prod graph-targeted data.

The +5pp from α-downscale could be a global artifact: downscaling ~all edges
sharpens the graph leg regardless of WHICH edges are exempt. Decisive control:
exempt the SAME COUNT of RANDOM edges instead of the consolidated ones.
  consolidation >> random  -> consolidation is load-bearing (F044 thesis real)
  consolidation ~= random  -> global sharpening artifact (thesis NOT supported)
Plus an all-downscale sanity arm (scale everything equally -> ranking unchanged
-> must ~= baseline).
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
from sqlalchemy import text as satext

AGENT = "nous-default"
QRELS_PATH = Path("E:/Projects/nous-eval-fixtures/v2026-Q2/qrels_graph_targeted.jsonl")
DSN = "postgresql://nous:nous_eval@localhost:5433/nous_eval_prod"
ALPHA = 0.42
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

async def edge_count(conn):
    return await conn.fetchval(f"SELECT count(*) FROM brain.graph_edges WHERE agent_id='{AGENT}'")

async def reset(conn):
    await conn.execute(f"UPDATE brain.graph_edges SET consolidation_state='tagged', ltp_count=0, last_ltp_at=NULL WHERE agent_id='{AGENT}' AND (ltp_count<>0 OR consolidation_state<>'tagged')")
    c = await conn.fetchval(f"SELECT count(*) FROM brain.graph_edges WHERE agent_id='{AGENT}' AND consolidation_state<>'tagged'")
    assert c == 0, f"reset failed: {c} non-tagged remain"

async def snapshot(conn):
    await conn.execute("DROP TABLE IF EXISTS public._f044_wsnap")
    await conn.execute(f"CREATE TABLE public._f044_wsnap AS SELECT id, weight FROM brain.graph_edges WHERE agent_id='{AGENT}'")

async def restore(conn):
    await conn.execute("UPDATE brain.graph_edges g SET weight = s.weight FROM public._f044_wsnap s WHERE g.id = s.id")

async def warmup(db, embedder, queries):
    _RECALL_TOUCH_BUFFER.clear()
    s = Settings().model_copy(update={"agent_id": AGENT})
    heart = Heart(db, s, embedding_provider=embedder)
    brain = Brain(db, s, embedding_provider=embedder)
    for q in queries:
        try:
            await run_recall_pipeline(q, heart, brain, s, limit=10)
        except Exception:
            pass
    async with db.session() as sess:
        await flush_recall_touches(sess)
        st = await stc_promote_and_measure(sess, AGENT, s.tinyhippo_prp_threshold)
        await sess.commit()
    return st["f044_n_consolidated"]

async def run_cfg(name, qrels, eval_settings, template, top_k):
    res = (await run_matrix([RetrievalConfig(name=name, flags=GRAPH)], qrels, eval_settings, template, top_k=top_k))[0]
    return compute_metrics(res.per_qrel, top_k)

async def main():
    qrels = load_qrels(QRELS_PATH, review_filter_enabled=False)
    eval_settings = EvalSettings(); top_k = eval_settings.top_k
    assert eval_settings.db_name == "nous_eval_prod" and eval_settings.agent_id == AGENT
    template = Settings()
    embedder = EmbeddingProvider(env_key("OPENAI_API_KEY"), "text-embedding-3-large", 1536)
    conn = await asyncpg.connect(DSN)
    db = Database(Settings().model_copy(update={"agent_id": AGENT})); await db.connect()
    print(f"CONFOUND CONTROL (α={ALPHA}), n={len(qrels)} qrels")
    try:
        await reset(conn); await snapshot(conn)
        e0 = await edge_count(conn)
        m_base = await run_cfg("baseline", qrels, eval_settings, template, top_k)
        print(f"baseline: MRR={m_base.mrr:.4f} nDCG@10={m_base.ndcg_at_10:.4f}\n")

        async def report(label, m, extra=""):
            dM = compute_delta(m_base, m, "mrr").absolute
            dN = compute_delta(m_base, m, "ndcg_at_10").absolute
            print(f"  {label:<24} MRR={m.mrr:.4f} ({dM:+.4f})  nDCG@10={m.ndcg_at_10:.4f} ({dN:+.4f})  {extra}")

        # 1. CONSOLIDATION arm (content warm-up) -> get N
        await reset(conn)
        ncons = await warmup(db, embedder, [r[0][:240] for r in await conn.fetch(f"SELECT content FROM heart.facts WHERE agent_id='{AGENT}' AND content IS NOT NULL AND length(content)>20 ORDER BY id LIMIT 120")])
        async with db.session() as sess:
            await homeostatic_downscale(sess, AGENT, ALPHA); await sess.commit()
        assert await edge_count(conn) == e0
        m_cons = await run_cfg("consolidation", qrels, eval_settings, template, top_k)
        await restore(conn)
        await report("consolidation(content)", m_cons, f"[exempt={ncons} consolidated]")

        # 2. RANDOM control: exempt the SAME count of random edges — but only
        # from rows the downscale would actually touch (tagged, non-deterministic),
        # so the consolidation vs random arms exempt the same eligible population.
        # Sampling from all edges would let the control "exempt" already-exempt
        # deterministic structural edges, biasing the verdict.
        await reset(conn)
        await conn.execute(f"UPDATE brain.graph_edges SET consolidation_state='consolidated' WHERE id IN (SELECT id FROM brain.graph_edges WHERE agent_id='{AGENT}' AND consolidation_state='tagged' AND extraction_method IS DISTINCT FROM 'deterministic' ORDER BY random() LIMIT {ncons})")
        async with db.session() as sess:
            await homeostatic_downscale(sess, AGENT, ALPHA); await sess.commit()
        assert await edge_count(conn) == e0
        m_rand = await run_cfg("random", qrels, eval_settings, template, top_k)
        await restore(conn); await reset(conn)
        await report("random-exempt", m_rand, f"[exempt={ncons} RANDOM eligible]")

        # 3. UNIFORM-scale sanity: scale EVERY tagged edge (bypassing the
        # deterministic exemption) so this arm is genuinely "nothing exempt".
        # Ranking is scale-invariant under a uniform multiply => must ~= baseline.
        # (Using homeostatic_downscale here would leave deterministic edges at
        # full weight and could legitimately move rankings — not a sanity null.)
        await reset(conn)
        await conn.execute(f"UPDATE brain.graph_edges SET weight = weight * {ALPHA} WHERE agent_id='{AGENT}' AND consolidation_state='tagged'")
        m_all = await run_cfg("all_downscale", qrels, eval_settings, template, top_k)
        await restore(conn)
        await report("uniform-scale(sanity)", m_all, "[nothing exempt -> expect ~baseline]")

        await reset(conn); await restore(conn)
        await conn.execute("DROP TABLE IF EXISTS public._f044_wsnap")
        print("\nVERDICT: consolidation - random =", round(m_cons.mrr - m_rand.mrr, 4), "MRR")
        print("  (>>0 => consolidation load-bearing; ~0 => global-sharpening artifact)")
    finally:
        await conn.close(); await db.disconnect()

asyncio.run(main())
