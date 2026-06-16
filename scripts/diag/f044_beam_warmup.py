import asyncio, os, json, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from nous.config import Settings
from nous.storage.database import Database
from nous.heart import Heart
from nous.brain.brain import Brain
from nous.brain.embeddings import EmbeddingProvider
from nous.api.retrieval_pipeline import run_recall_pipeline
from nous.brain.tinyhippo_lite import flush_recall_touches, stc_promote_and_measure, _recall_buffer_size

def env_key(name):
    try:
        for line in open(".env", encoding="utf-8"):
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return os.environ.get(name, "")

def load_questions(conv):
    d = json.load(open(f"tools/beam/chats/100K/{conv}/probing_questions/probing_questions.json", encoding="utf-8"))
    out = []
    for v in d.values():
        if isinstance(v, list):
            for it in v:
                if isinstance(it, dict) and it.get("question"):
                    out.append(it["question"])
    return out

async def main():
    base = Settings()  # DB + F044 flags come from env (set in shell)
    embedder = EmbeddingProvider(env_key("OPENAI_API_KEY"), "text-embedding-3-large", 1536)
    db = Database(base); await db.connect()
    print(f"target {base.db_host}:{base.db_port}/{base.db_name} tinyhippo={base.tinyhippo_lite_enabled} recall_touch={base.tinyhippo_recall_touch_enabled}")
    agents = [f"beam-100K-conv-00{n}" for n in range(1, 6)]
    try:
        for n, agent in enumerate(agents, 1):
            s = base.model_copy(update={"agent_id": agent})
            heart = Heart(db, s, embedding_provider=embedder)
            brain = Brain(db, s, embedding_provider=embedder)
            qs = load_questions(n)
            ok = 0
            for q in qs:
                try:
                    await run_recall_pipeline(q, heart, brain, s, limit=10)
                    ok += 1
                except Exception as e:
                    print("  recall err:", str(e)[:140])
            print(f"{agent}: recalled {ok}/{len(qs)} | global buffer={_recall_buffer_size()} edges")
        async with db.session() as sess:
            touched = await flush_recall_touches(sess)
            await sess.commit()
        print(f"\nflushed {touched} distinct recall-touched edges -> ltp_count")
        print("=== consolidation per agent (PRP=3) ===")
        for agent in agents:
            async with db.session() as sess:
                st = await stc_promote_and_measure(sess, agent, 3)
                await sess.commit()
            print(f"  {agent}: edges={st['f044_n_edges']} ltp>=1={st['f044_ltp_ge1']} >=2={st['f044_ltp_ge2']} >=3={st['f044_ltp_ge3']} consolidated={st['f044_n_consolidated']}")
    finally:
        await db.disconnect()

asyncio.run(main())
