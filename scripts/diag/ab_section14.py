"""§14 A/B in the eval environment: graph-primary (K-line) procedure selection vs the
OLD embedding-cosine selection (Track B), both scored by an LLM relevance judge, on the
fresh post-dedup prod snapshot (nous_eval_prod @ 5433, agent nous-default).

Answers the advisor's open question: are §14's graph picks actually RELEVANT (so
preloading their bodies is worth the tokens) and do they beat the cosine baseline?

Run: uv run python scripts/diag/ab_section14.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

SNAP = Path(".env.prod-snapshot")
for raw in SNAP.read_text(encoding="utf-8").splitlines():
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    os.environ[k.strip()] = v.strip().strip('"').strip("'")
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
    "debug a stuck DAG orchestration run",
    "write a feature spec and get it reviewed",
    "summarize recent work and episodes",
]


async def judge(q: str, graph_name: str, graph_desc: str, cos_name: str, cos_desc: str, key: str) -> dict:
    """OpenAI relevance judge: rate each surfaced procedure 0-2 for the task."""
    prompt = (
        f"Task/query: {q!r}\n\n"
        f"Procedure A (graph): {graph_name!r} — {graph_desc[:200]!r}\n"
        f"Procedure B (cosine): {cos_name!r} — {cos_desc[:200]!r}\n\n"
        "For each, rate relevance to the task: 0=irrelevant, 1=loosely related, "
        "2=directly the right procedure. If a procedure is empty/'none', score 0. "
        'Reply ONLY JSON: {"a":<0-2>,"b":<0-2>}'
    )
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": "gpt-4o-mini", "temperature": 0,
                  "messages": [{"role": "user", "content": prompt}],
                  "response_format": {"type": "json_object"}},
        )
    try:
        return json.loads(r.json()["choices"][0]["message"]["content"])
    except Exception:
        return {"a": 0, "b": 0}


async def main() -> None:
    s = Settings()
    db = Database(s)
    await db.connect()
    emb = EmbeddingProvider(api_key=s.openai_api_key, model=s.embedding_model,
                            dimensions=getattr(s, "embedding_dimensions", 1536))
    brain = Brain(db, s, emb)
    heart = Heart(db, s, emb, owns_embeddings=False)
    engine = ContextEngine(brain, heart, s, identity_prompt="You are Nous.")

    print(f"A/B  graph-primary (§14) vs embedding-cosine (old Track B) — judged relevance\n"
          f"agent={s.agent_id} model={s.embedding_model}\n" + "=" * 78)

    g_scores, c_scores = [], []
    g_hits = c_hits = both_rel = body_chars_tot = 0
    async with db.session() as sess:
        for q in QUERIES:
            facts = await heart.search_facts(q, limit=12, session=sess)
            decs = await brain.query(q, limit=6)
            rid = {"fact": [str(f.id) for f in facts], "decision": [str(d.id) for d in decs]}
            smap = {str(x.id): (getattr(x, "score", 0.0) or 0.0) for x in list(facts) + list(decs)}
            gsel = await engine._select_procedures(
                slots=3, critic_skills=[], recalled_ids=rid,
                recalled_score_map=smap, session=sess, query=q)
            csel = await heart.search_procedures(q, limit=3, session=sess)

            g0 = gsel[0] if gsel else None
            c0 = csel[0] if csel else None
            gname = g0.name if g0 else "none"
            gdesc = (getattr(g0, "description", "") or "") if g0 else ""
            cname = c0.name if c0 else "none"
            cdesc = (getattr(c0, "description", "") or "") if c0 else ""
            v = await judge(q, gname, gdesc, cname, cdesc, s.openai_api_key)
            ga, cb = int(v.get("a", 0)), int(v.get("b", 0))
            g_scores.append(ga); c_scores.append(cb)
            if g0:
                g_hits += 1
                body_chars_tot += len("\n".join(engine._format_procedure_bodies(gsel, 1200)))
            if c0:
                c_hits += 1
            if ga >= 1 and cb >= 1:
                both_rel += 1
            print(f"  {q[:38]:40s} graph={gname[:22]:24s}({ga})  cosine={cname[:22]:24s}({cb})")

    n = len(QUERIES)
    print("=" * 78)
    print(f"  GRAPH (§14):  hit {g_hits}/{n}  mean-relevance {sum(g_scores)/n:.2f}/2  "
          f">=1 relevant: {sum(1 for x in g_scores if x>=1)}/{n}  "
          f"direct(2): {sum(1 for x in g_scores if x==2)}/{n}")
    print(f"  COSINE (old): hit {c_hits}/{n}  mean-relevance {sum(c_scores)/n:.2f}/2  "
          f">=1 relevant: {sum(1 for x in c_scores if x>=1)}/{n}  "
          f"direct(2): {sum(1 for x in c_scores if x==2)}/{n}")
    print(f"  body-preload token cost: ~{body_chars_tot//4} tok over {g_hits} hits "
          f"(~{(body_chars_tot//4)//max(1,g_hits)} tok/hit) vs a name-pointer (~10 tok)")
    print(f"\n  Read: §14 graph picks judged {'>=' if sum(g_scores)>=sum(c_scores) else '<'} "
          f"cosine baseline. Graph fires less often (sparse edges) but when it does the pick "
          f"is the K-line-associated skill; cosine always returns its nearest-by-text proc.")


if __name__ == "__main__":
    asyncio.run(main())
