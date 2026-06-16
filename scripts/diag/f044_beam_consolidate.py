"""F044 BEAM consolidation driver: reset | questions | content warm-up.

reset    -> clear ltp_count/consolidation_state on beam edges (clean 'tagged').
questions-> warm-up using the eval probing questions as recall queries (self-referential).
content  -> warm-up using corpus FACTS as recall queries (non-leaky generalization arm).
Both warm-ups: drive run_recall_pipeline (F044 on) -> buffer recall touches -> flush -> promote.
"""
import asyncio, os, json, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from sqlalchemy import text
from nous.config import Settings
from nous.storage.database import Database
from nous.heart import Heart
from nous.brain.brain import Brain
from nous.brain.embeddings import EmbeddingProvider
from nous.api.retrieval_pipeline import run_recall_pipeline
from nous.brain.tinyhippo_lite import flush_recall_touches, stc_promote_and_measure, _recall_buffer_size, _RECALL_TOUCH_BUFFER

MODE = sys.argv[1]
N_FACTS = int(sys.argv[2]) if len(sys.argv) > 2 else 40
AGENTS = [f"beam-100K-conv-00{n}" for n in range(1, 6)]

def env_key(name):
    try:
        for line in open(".env", encoding="utf-8"):
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except FileNotFoundError: pass
    return os.environ.get(name, "")

def questions(conv):
    d = json.load(open(f"tools/beam/chats/100K/{conv}/probing_questions/probing_questions.json", encoding="utf-8"))
    return [it["question"] for v in d.values() if isinstance(v, list) for it in v if isinstance(it, dict) and it.get("question")]

async def main():
    base = Settings()
    db = Database(base); await db.connect()
    try:
        if MODE == "reset":
            async with db.session() as s:
                r = await s.execute(text(
                    "UPDATE brain.graph_edges SET consolidation_state='tagged', ltp_count=0, last_ltp_at=NULL "
                    "WHERE agent_id LIKE 'beam-100K-%' AND (ltp_count<>0 OR consolidation_state<>'tagged')"))
                await s.commit()
                print(f"reset: cleared {r.rowcount} beam edges to tagged/ltp0")
            _RECALL_TOUCH_BUFFER.clear()
            # Hard isolation assertion: verify the graph is actually pristine
            # after the reset (rowcount alone lied in the contaminated run).
            # Abort the whole experiment if any consolidated/ltp residue remains.
            async with db.session() as s:
                cons = await s.execute(text(
                    "SELECT count(*) FROM brain.graph_edges "
                    "WHERE agent_id LIKE 'beam-100K-%' AND consolidation_state <> 'tagged'"))
                ltp = await s.execute(text(
                    "SELECT count(*) FROM brain.graph_edges "
                    "WHERE agent_id LIKE 'beam-100K-%' AND ltp_count <> 0"))
                nc, nl = cons.scalar(), ltp.scalar()
            if nc != 0 or nl != 0:
                print(f"FATAL: reset did not isolate (consolidated={nc}, ltp!=0={nl}) — aborting")
                sys.exit(2)
            print("reset verified clean: consolidated=0 ltp!=0=0")
            return
        embedder = EmbeddingProvider(env_key("OPENAI_API_KEY"), "text-embedding-3-large", 1536)
        _RECALL_TOUCH_BUFFER.clear()
        for n, agent in enumerate(AGENTS, 1):
            s = base.model_copy(update={"agent_id": agent})
            heart = Heart(db, s, embedding_provider=embedder)
            brain = Brain(db, s, embedding_provider=embedder)
            if MODE == "questions":
                qs = questions(n)
            elif MODE == "content":
                async with db.session() as sess:
                    rows = (await sess.execute(text(
                        "SELECT content FROM heart.facts WHERE agent_id=:a AND content IS NOT NULL "
                        "AND length(content)>20 ORDER BY id LIMIT :n"), {"a": agent, "n": N_FACTS})).all()
                qs = [r[0][:240] for r in rows]
            else:
                raise SystemExit(f"bad mode {MODE}")
            ok = 0
            for q in qs:
                try:
                    await run_recall_pipeline(q, heart, brain, s, limit=10); ok += 1
                except Exception as e:
                    print("  recall err:", str(e)[:100])
            print(f"{agent}: {MODE} warm-up {ok}/{len(qs)} queries | buffer={_recall_buffer_size()}")
        async with db.session() as sess:
            touched = await flush_recall_touches(sess); await sess.commit()
        tot = 0
        for agent in AGENTS:
            async with db.session() as sess:
                st = await stc_promote_and_measure(sess, agent, base.tinyhippo_prp_threshold); await sess.commit()
            tot += st["f044_n_consolidated"]
        print(f"{MODE} warm-up: flushed {touched} edges -> {tot} consolidated across 5 agents")
    finally:
        await db.disconnect()
asyncio.run(main())
