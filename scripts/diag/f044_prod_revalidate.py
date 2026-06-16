"""F044 α-downscale RE-VALIDATION on real prod graph-targeted data.

Independent re-run of the decisive result + per-qrel paired significance:
  - reproduce baseline / consolidation / random-exempt / all-downscale
  - paired sign test + paired-t on per-qrel reciprocal rank (baseline vs
    consolidation), since n=22 demands the paired (not marginal) yardstick.
"""
import asyncio, os, sys, math
from math import comb
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

async def ecount(conn):
    return await conn.fetchval(f"SELECT count(*) FROM brain.graph_edges WHERE agent_id='{AGENT}'")
async def reset(conn):
    await conn.execute(f"UPDATE brain.graph_edges SET consolidation_state='tagged', ltp_count=0, last_ltp_at=NULL WHERE agent_id='{AGENT}' AND (ltp_count<>0 OR consolidation_state<>'tagged')")
    assert (await conn.fetchval(f"SELECT count(*) FROM brain.graph_edges WHERE agent_id='{AGENT}' AND consolidation_state<>'tagged'")) == 0
async def snapshot(conn):
    await conn.execute("DROP TABLE IF EXISTS public._f044_wsnap")
    await conn.execute(f"CREATE TABLE public._f044_wsnap AS SELECT id, weight FROM brain.graph_edges WHERE agent_id='{AGENT}'")
async def restore(conn):
    await conn.execute("UPDATE brain.graph_edges g SET weight=s.weight FROM public._f044_wsnap s WHERE g.id=s.id")

async def warmup(db, embedder, queries):
    _RECALL_TOUCH_BUFFER.clear()
    s = Settings().model_copy(update={"agent_id": AGENT, "tinyhippo_lite_enabled": True})
    heart = Heart(db, s, embedding_provider=embedder); brain = Brain(db, s, embedding_provider=embedder)
    for q in queries:
        try: await run_recall_pipeline(q, heart, brain, s, limit=10)
        except Exception: pass
    async with db.session() as sess:
        await flush_recall_touches(sess, AGENT)
        st = await stc_promote_and_measure(sess, AGENT, s.tinyhippo_prp_threshold)
        await sess.commit()
    return st["f044_n_consolidated"]

async def run_cfg(name, qrels, es, tmpl, k):
    res = (await run_matrix([RetrievalConfig(name=name, flags=GRAPH)], qrels, es, tmpl, top_k=k))[0]
    rr = [(1.0/q.rank_of_first_gold if q.rank_of_first_gold else 0.0) for q in res.per_qrel if q.error is None]
    return compute_metrics(res.per_qrel, k), rr

def paired(rr_a, rr_b):
    diffs = [b-a for a, b in zip(rr_a, rr_b)]
    n = len(diffs); mean = sum(diffs)/n
    sd = (sum((d-mean)**2 for d in diffs)/(n-1))**0.5 if n > 1 else 0.0
    se = sd/math.sqrt(n) if sd else 0.0
    pos = sum(1 for d in diffs if d > 1e-9); neg = sum(1 for d in diffs if d < -1e-9)
    m = pos+neg; kk = min(pos, neg)
    p = min(1.0, 2*sum(comb(m, i) for i in range(kk+1))/(2**m)) if m else 1.0
    t = mean/se if se else float("inf") if mean else 0.0
    return n, mean, se, t, pos, neg, p

async def main():
    qrels = load_qrels(QRELS_PATH, review_filter_enabled=False)
    es = EvalSettings(); k = es.top_k
    assert es.db_name == "nous_eval_prod" and es.agent_id == AGENT
    tmpl = Settings()
    embedder = EmbeddingProvider(env_key("OPENAI_API_KEY"), "text-embedding-3-large", 1536)
    conn = await asyncpg.connect(DSN)
    db = Database(Settings().model_copy(update={"agent_id": AGENT})); await db.connect()
    print(f"RE-VALIDATION (α={ALPHA}), n={len(qrels)} qrels")
    try:
        await reset(conn); await snapshot(conn); e0 = await ecount(conn)
        m_base, rr_base = await run_cfg("baseline", qrels, es, tmpl, k)
        print(f"baseline: MRR={m_base.mrr:.4f} nDCG@10={m_base.ndcg_at_10:.4f}")

        await reset(conn)
        ncons = await warmup(db, embedder, [r[0][:240] for r in await conn.fetch(f"SELECT content FROM heart.facts WHERE agent_id='{AGENT}' AND content IS NOT NULL AND length(content)>20 ORDER BY id LIMIT 120")])
        async with db.session() as sess: await homeostatic_downscale(sess, AGENT, ALPHA); await sess.commit()
        assert await ecount(conn) == e0
        m_cons, rr_cons = await run_cfg("consolidation", qrels, es, tmpl, k)
        await restore(conn)
        print(f"consolidation(content): MRR={m_cons.mrr:.4f} ({compute_delta(m_base,m_cons,'mrr').absolute:+.4f})  nDCG@10={m_cons.ndcg_at_10:.4f} ({compute_delta(m_base,m_cons,'ndcg_at_10').absolute:+.4f})  [exempt={ncons}]")

        # Random control: exempt the SAME count of DOWNSCALE-ELIGIBLE edges
        # (tagged, non-deterministic) so it matches the consolidation arm's
        # population — homeostatic_downscale skips the deterministic tier, so
        # sampling from all edges could "exempt" already-exempt structural rows.
        await reset(conn)
        await conn.execute(f"UPDATE brain.graph_edges SET consolidation_state='consolidated' WHERE id IN (SELECT id FROM brain.graph_edges WHERE agent_id='{AGENT}' AND consolidation_state='tagged' AND extraction_method IS DISTINCT FROM 'deterministic' ORDER BY random() LIMIT {ncons})")
        async with db.session() as sess: await homeostatic_downscale(sess, AGENT, ALPHA); await sess.commit()
        m_rand, _ = await run_cfg("random", qrels, es, tmpl, k)
        await restore(conn); await reset(conn)
        print(f"random-exempt:          MRR={m_rand.mrr:.4f} ({compute_delta(m_base,m_rand,'mrr').absolute:+.4f})  [exempt={ncons} RANDOM eligible]")

        # Uniform-scale sanity: scale EVERY tagged edge (bypassing the
        # deterministic exemption) so this arm is genuinely "nothing exempt" —
        # ranking is scale-invariant => expect ~0. homeostatic_downscale would
        # leave deterministic edges at full weight and could move ranking.
        await reset(conn)
        await conn.execute(f"UPDATE brain.graph_edges SET weight = weight * {ALPHA} WHERE agent_id='{AGENT}' AND consolidation_state='tagged'")
        m_all, _ = await run_cfg("all_downscale", qrels, es, tmpl, k)
        await restore(conn)
        print(f"uniform-scale(sanity):  MRR={m_all.mrr:.4f} ({compute_delta(m_base,m_all,'mrr').absolute:+.4f})  [expect ~0]")

        n, mean, se, t, pos, neg, p = paired(rr_base, rr_cons)
        print(f"\nPAIRED per-qrel RR (consolidation vs baseline): n={n} meanΔRR={mean:+.4f} SE={se:.4f} t={t:+.2f}")
        print(f"  nonzero={pos+neg} (improved={pos} worsened={neg})  sign_p={p:.3f}")
        print(f"  control: consolidation−random = {m_cons.mrr-m_rand.mrr:+.4f} MRR")

        await reset(conn); await restore(conn); await conn.execute("DROP TABLE IF EXISTS public._f044_wsnap")
        print("corpus restored pristine")
    finally:
        await conn.close(); await db.disconnect()

asyncio.run(main())
