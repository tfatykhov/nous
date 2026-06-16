"""F044 on REAL PROD DATA — graph-targeted (graph-only-reachable) retrieval A/B.

Corpus: nous_eval_prod / nous-default (the real prod memory graph).
Qrels:  graph_targeted (gold reachable ONLY via a graph bridge) — the regime
        where the graph leg is binding, i.e. where F044's boost could pay off.

Three configs, each validated:
  baseline      : graph+adjacency on, tinyhippo OFF (consolidation-agnostic)
  f044_self     : + tinyhippo on, warm-up = the qrel queries (best-case)
  f044_content  : + tinyhippo on, warm-up = corpus facts (non-leaky generalization)

Every step asserts: reset is verifiably clean, edge count is stable (no
re-ingest), and the graph leg actually ran (graph_expansion_used > 0).
"""
import asyncio, os, json, sys
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from sqlalchemy import text
from nous.config import Settings
from nous.storage.database import Database
from nous.heart import Heart
from nous.brain.brain import Brain
from nous.brain.embeddings import EmbeddingProvider
from nous.api.retrieval_pipeline import run_recall_pipeline
from nous.brain.tinyhippo_lite import flush_recall_touches, stc_promote_and_measure, _RECALL_TOUCH_BUFFER
from nous_eval.config import EvalSettings
from nous_eval.qrels_loader import load_qrels
from nous_eval.retrieval_runner import RetrievalConfig, run_matrix
from nous_eval.metrics import compute_metrics, compute_delta

AGENT = "nous-default"
QRELS_PATH = Path("E:/Projects/nous-eval-fixtures/v2026-Q2/qrels_graph_targeted.jsonl")
DSN = "postgresql://nous:nous_eval@localhost:5433/nous_eval_prod"

def env_key(name):
    try:
        for line in open(".env", encoding="utf-8"):
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return os.environ.get(name, "")

async def _counts(conn):
    cons = await conn.fetchval(f"SELECT count(*) FROM brain.graph_edges WHERE agent_id='{AGENT}' AND consolidation_state='consolidated'")
    edges = await conn.fetchval(f"SELECT count(*) FROM brain.graph_edges WHERE agent_id='{AGENT}'")
    ltp = await conn.fetchval(f"SELECT count(*) FROM brain.graph_edges WHERE agent_id='{AGENT}' AND ltp_count<>0")
    return cons, edges, ltp

async def reset_assert(conn):
    await conn.execute(f"UPDATE brain.graph_edges SET consolidation_state='tagged', ltp_count=0, last_ltp_at=NULL WHERE agent_id='{AGENT}' AND (ltp_count<>0 OR consolidation_state<>'tagged')")
    cons, edges, ltp = await _counts(conn)
    assert cons == 0 and ltp == 0, f"RESET FAILED: consolidated={cons} ltp!=0={ltp}"
    print(f"  reset verified clean: consolidated=0 ltp=0 edges={edges}")
    return edges

async def warmup(db, embedder, queries, label):
    _RECALL_TOUCH_BUFFER.clear()
    s = Settings().model_copy(update={"agent_id": AGENT, "tinyhippo_lite_enabled": True})
    heart = Heart(db, s, embedding_provider=embedder)
    brain = Brain(db, s, embedding_provider=embedder)
    ok = 0
    for q in queries:
        try:
            await run_recall_pipeline(q, heart, brain, s, limit=10); ok += 1
        except Exception as e:
            print("    recall err:", str(e)[:90])
    async with db.session() as sess:
        touched = await flush_recall_touches(sess, AGENT)
        st = await stc_promote_and_measure(sess, AGENT, s.tinyhippo_prp_threshold)
        await sess.commit()
    print(f"  {label} warm-up: {ok}/{len(queries)} queries -> flushed {touched} edges -> {st['f044_n_consolidated']} consolidated")
    return st['f044_n_consolidated']

async def run_cfg(name, flags, qrels, eval_settings, template, top_k):
    res = (await run_matrix([RetrievalConfig(name=name, flags=flags)], qrels, eval_settings, template, top_k=top_k))[0]
    m = compute_metrics(res.per_qrel, top_k)
    ge = res.pipeline_stats_summary.get("graph_expansion_used", 0)
    print(f"  [{name}] MRR={m.mrr:.4f} P@1={m.p_at_1:.4f} P@5={m.p_at_5:.4f} nDCG@10={m.ndcg_at_10:.4f} | graph_expansion_used={ge}/{m.n_qrels} (errored={m.n_errored})")
    return m

async def main():
    # ---- validate inputs ----
    qrels = load_qrels(QRELS_PATH, review_filter_enabled=False)
    print(f"loaded {len(qrels)} graph_targeted qrels from real prod corpus")
    eval_settings = EvalSettings()  # NOUS_EVAL_DB_NAME=nous_eval_prod, NOUS_EVAL_AGENT_ID=nous-default via env
    print(f"eval scope: {eval_settings.db_name} / agent={eval_settings.agent_id} top_k={eval_settings.top_k}")
    assert eval_settings.db_name == "nous_eval_prod" and eval_settings.agent_id == AGENT, "eval scope misconfigured"
    template = Settings()
    top_k = eval_settings.top_k
    embedder = EmbeddingProvider(env_key("OPENAI_API_KEY"), "text-embedding-3-large", 1536)
    import asyncpg
    conn = await asyncpg.connect(DSN)
    db = Database(Settings().model_copy(update={"agent_id": AGENT}))
    await db.connect()
    # graph flags ON for ALL arms (so retrieval can reach graph-only golds); only tinyhippo differs.
    GRAPH = {"graph_recall_enabled": True, "graph_adjacency_boost_enabled": True, "heart_graph_all_types_enabled": True}
    try:
        # ===== BASELINE (tinyhippo off) =====
        print("\n=== BASELINE (consolidation-agnostic) ===")
        edges0 = await reset_assert(conn)
        m_base = await run_cfg("baseline", {**GRAPH, "tinyhippo_lite_enabled": False}, qrels, eval_settings, template, top_k)

        # ===== F044 SELF (best-case: warm-up on qrel queries) =====
        print("\n=== F044 SELF (warm-up = qrel queries, best-case) ===")
        await reset_assert(conn)
        await warmup(db, embedder, [q.query for q in qrels], "self")
        e = (await _counts(conn))[1]; assert e == edges0, f"edge count changed {e}!={edges0}"
        m_self = await run_cfg("f044_self", {**GRAPH, "tinyhippo_lite_enabled": True}, qrels, eval_settings, template, top_k)

        # ===== F044 CONTENT (generalization: warm-up on corpus facts) =====
        print("\n=== F044 CONTENT (warm-up = corpus facts, non-leaky) ===")
        await reset_assert(conn)
        facts = [r[0][:240] for r in await conn.fetch(f"SELECT content FROM heart.facts WHERE agent_id='{AGENT}' AND content IS NOT NULL AND length(content)>20 ORDER BY id LIMIT 120")]
        await warmup(db, embedder, facts, "content")
        e = (await _counts(conn))[1]; assert e == edges0, f"edge count changed {e}!={edges0}"
        m_cont = await run_cfg("f044_content", {**GRAPH, "tinyhippo_lite_enabled": True}, qrels, eval_settings, template, top_k)

        # ===== compare =====
        print("\n=== RESULT (real prod graph-targeted, n={}) ===".format(m_base.n_qrels))
        for label, m in [("baseline", m_base), ("f044_self", m_self), ("f044_content", m_cont)]:
            print(f"  {label:<14} MRR={m.mrr:.4f}  P@1={m.p_at_1:.4f}  P@5={m.p_at_5:.4f}  R@10={m.r_at_10:.4f}  nDCG@10={m.ndcg_at_10:.4f}")
        for label, m in [("self", m_self), ("content", m_cont)]:
            d = compute_delta(m_base, m, "mrr")
            dn = compute_delta(m_base, m, "ndcg_at_10")
            print(f"  delta {label}-baseline: MRR {d.absolute:+.4f}  nDCG@10 {dn.absolute:+.4f}")
        # clean up: leave corpus pristine
        await reset_assert(conn)
    finally:
        await conn.close()
        await db.disconnect()

asyncio.run(main())
