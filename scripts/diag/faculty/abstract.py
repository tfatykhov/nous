"""Phase 0 ABSTRACT class (the frontier, final experiment) — does Nous surface a
structurally-analogous, surface-DIFFERENT fact when there is NO shared token?

Three-way validity gate per item (pre-registered):
  (a) co-mention  -> 0 by construction (no shared >=2-token proper noun).
  (b) embedding cosine -> does the answer fact land in bare top-k for the query? THE
      load-bearing measurement: does text-embedding-3-large span the abstract structure?
  (c) agent reformulation -> does the agentic loop surface + connect it?
Failure split: never surfaces => REPRESENTATION gap; surfaces but agent doesn't connect
=> REASONING gap; surfaces+connects => solved (cosine-direct vs agent-reformulation).

Incremental: ingests the abstract answer facts into the EXISTING full-cycle faculty
corpus (no reset) so the ~27 facts provide top-k selectivity.

  uv run python scripts/diag/faculty/abstract.py ingest
  uv run python scripts/diag/faculty/abstract.py measure
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _load(path: Path) -> None:
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        os.environ[k] = v


_load(REPO / ".env.prod-snapshot")
os.environ.update({
    "DB_HOST": "127.0.0.1", "DB_PORT": "5433", "DB_NAME": "nous_eval_live",
    "DB_USER": "nous", "DB_PASSWORD": "nous_eval", "NOUS_AGENT_ID": "nous-faculty-eval",
    "NOUS_GRAPH_ADJACENCY_BOOST_ENABLED": "true",
})

from scripts.diag.faculty.corpus import ABSTRACT, ABSTRACT_CONTROL, abstract_facts  # noqa: E402

AGENT = "nous-faculty-eval"
BASE = "http://127.0.0.1:8078"
K = 10
PROMPT = "{q}\n\n(Answer from what you know about my work and contacts. If you don't have that information, just say so.)"
_ABSTAIN = ("don't have", "do not have", "don't know", "no record", "not aware", "nothing about",
            "don't have that information", "no mention", "couldn't find", "no entry", "i have no", "no one")
RUN_DIR = HERE / "runs" / (time.strftime("%Y%m%d-%H%M%S") + "-abstract")
RUN_DIR.mkdir(parents=True, exist_ok=True)
LOG = open(RUN_DIR / "result.txt", "w", encoding="utf-8")


def out(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    LOG.write(s + "\n")
    LOG.flush()


def psql(q: str) -> str:
    return subprocess.run(
        ["docker", "exec", "nous-eval-scratch", "psql", "-U", "nous", "-d", "nous_eval_live", "-tAc", q],
        capture_output=True, text=True).stdout.strip()


async def _ingest() -> None:
    from sqlalchemy import text

    from nous.brain.embeddings import EmbeddingProvider
    from nous.config import Settings
    from nous.storage.database import Database

    out("=== ABSTRACT ingest: direct-insert (controlled content) + SLEEP ===")
    out("NOTE: full-cycle /chat extraction DROPPED 3/6 abstract facts (Ravi Lund / Priya Sundar /")
    out("Nadia Frost) — a real extraction-COVERAGE gap. The abstract test is about RETRIEVAL of an")
    out("existing analogous fact, so we guarantee the material via direct-insert, then SLEEP (full-")
    out("cycle consolidation). The extraction miss is logged as a separate observation.")
    s = Settings()
    db = Database(s); await db.connect()
    emb = EmbeddingProvider(api_key=s.openai_api_key, model="text-embedding-3-large", dimensions=1536)
    facts = abstract_facts()
    async with db.session() as sess:
        # clear any partial extraction-created abstract facts to avoid dup/paraphrase mix
        for c in ABSTRACT + [ABSTRACT_CONTROL]:
            tok = c["answer_token"].split()[-1].replace("'", "''")
            await sess.execute(text(f"DELETE FROM heart.facts WHERE agent_id=:a AND content ILIKE '%{tok}%'"), {"a": AGENT})
        for content, src in facts:
            vec = await emb.embed(content)
            vlit = "[" + ",".join(f"{x:.6f}" for x in vec) + "]"
            await sess.execute(text(
                "INSERT INTO heart.facts (agent_id, content, source, confidence, active, embedding) "
                "VALUES (:a, :c, :src, 0.9, TRUE, CAST(:v AS vector))"
            ), {"a": AGENT, "c": content, "src": src, "v": vlit})
        await sess.commit()
    await db.disconnect()
    out(f"direct-inserted {len(facts)} abstract facts; triggering sleep...")
    try:
        httpx.post(f"{BASE}/sleep/trigger", timeout=400)
    except Exception as ex:
        out(f"  (sleep err: {ex})")
    time.sleep(8)
    for c in ABSTRACT + [ABSTRACT_CONTROL]:
        tok = c["answer_token"].split()[-1]
        n = psql(f"SELECT count(*) FROM heart.facts WHERE agent_id='{AGENT}' AND content ILIKE '%{tok}%'")
        out(f"  fact for '{c['answer_token']}': {n}")


def ingest() -> None:
    asyncio.run(_ingest())


def ask(query: str) -> tuple[str, int]:
    sid = f"abq-{int(time.time()*1000)}"
    data: dict = {}
    try:
        r = httpx.post(f"{BASE}/chat", json={"message": PROMPT.format(q=query), "session_id": sid}, timeout=280)
        if r.headers.get("content-type", "").startswith("application/json"):
            data = r.json()
    except Exception as ex:
        return f"(error {ex})", -1
    finally:
        try:
            httpx.delete(f"{BASE}/chat/{sid}", timeout=20)
        except Exception:
            pass
    text = ""
    for key in ("response", "content", "message"):
        if isinstance(data.get(key), str) and data[key].strip():
            text = data[key].strip(); break
    return (text or "(empty)"), int((data.get("usage") or {}).get("tool_calls", -1))


async def measure() -> None:
    from nous.api.retrieval_pipeline import run_recall_pipeline
    from nous.brain.brain import Brain
    from nous.brain.embeddings import EmbeddingProvider
    from nous.config import Settings
    from nous.heart.heart import Heart
    from nous.storage.database import Database

    out("=== ABSTRACT measure ===")
    out("PRE-REGISTERED: co-mention=0 by construction; embedding cosine = coin-flip "
        "(does the representation span the structure?); agent solves only if retrieval surfaces the candidate.")
    s = Settings().model_copy(update={
        "graph_neighbor_seed_score_enabled": True, "heart_graph_all_types_enabled": True,
        "graph_adjacency_boost_enabled": True, "residual_activation_enabled": False})
    db = Database(s); await db.connect()
    emb = EmbeddingProvider(api_key=s.openai_api_key, model="text-embedding-3-large", dimensions=1536)
    heart = Heart(database=db, settings=s, embedding_provider=emb); brain = Brain(db, s, emb)

    async def bare_rank(query, tok):
        res, _ = await run_recall_pipeline(query, heart, brain, s, limit=30, rerank_by_score=True)
        return next((i + 1 for i, r in enumerate(res) if tok.lower() in (r.description or "").lower()), None)

    # (b) embedding cosine reachability — twin control first (must pass), then the abstract items.
    out("\n--- (b) BARE / embedding-cosine reachability ---")
    tr = await bare_rank(ABSTRACT_CONTROL["query"], ABSTRACT_CONTROL["answer_token"])
    out(f"  [TWIN control] answer_rank={tr} top{K}={bool(tr and tr<=K)} (predict PASS — shares surface terms)")
    cosine_hits = []
    for c in ABSTRACT:
        r = await bare_rank(c["query"], c["answer_token"])
        reach = bool(r and r <= K)
        cosine_hits.append(reach)
        out(f"  [{c['id']:14}] pattern={c['pattern']:28} answer_rank={r} cosine_top{K}={reach}")
    await db.disconnect()

    # (c) agent reformulation
    out("\n--- (c) AGENTIC reformulation ---")
    agent_hits = []
    for c in ABSTRACT:
        a, tc = ask(c["query"])
        hit = c["answer_token"].lower() in a.lower()
        agent_hits.append(hit)
        abst = any(x in a.lower() for x in _ABSTAIN)
        out(f"\n  [{c['id']}] tc={tc} solved={hit} {'(abstained)' if abst and not hit else ''}")
        out(f"     ans: {a[:240]}")

    # Verdict + retrieval-vs-reasoning split
    out("\n" + "=" * 70 + "\n--- SUMMARY (abstract class) ---")
    out(f"(a) co-mention bridges: 0 (by construction — no shared proper noun)")
    out(f"(b) embedding-cosine reachable (top-{K}): {sum(cosine_hits)}/{len(ABSTRACT)}  {[c['id'] for c,h in zip(ABSTRACT,cosine_hits) if h]}")
    out(f"(c) agent solved: {sum(agent_hits)}/{len(ABSTRACT)}  {[c['id'] for c,h in zip(ABSTRACT,agent_hits) if h]}")
    rep_gap = [c["id"] for c, cb, ab in zip(ABSTRACT, cosine_hits, agent_hits) if not cb and not ab]
    cosine_solved = [c["id"] for c, cb, ab in zip(ABSTRACT, cosine_hits, agent_hits) if cb]
    agent_only = [c["id"] for c, cb, ab in zip(ABSTRACT, cosine_hits, agent_hits) if ab and not cb]
    out(f"\nFAILURE SPLIT:")
    out(f"  REPRESENTATION gap (never surfaces, neither lens): {len(rep_gap)}/{len(ABSTRACT)}  {rep_gap}")
    out(f"  solved via embedding cosine (representation spans it): {cosine_solved}")
    out(f"  solved via agent reformulation only (no cosine handle): {agent_only}")
    out("\nINTERPRETATION:")
    if rep_gap and not cosine_solved:
        out("  -> ABSTRACT association is a REPRESENTATION gap: the embedding does NOT span the structure, "
            "there's no lexical handle for the agent to reformulate, and it never surfaces. This RE-ELEVATES "
            "the substrate (representation/analogical encoding) — neither denser graphs nor agent-loop reliability.")
    elif cosine_solved:
        out("  -> the embedding DOES span some abstract structure (cosine-reachable items) -> abstract association "
            "is partly a REPRESENTATION win; lever shifts toward embedding/representation quality.")
    LOG.close()


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "measure"
    try:
        httpx.get(f"{BASE}/health", timeout=5)
    except Exception as ex:
        out(f"!! instance not on {BASE} ({ex})"); return
    if cmd == "ingest":
        ingest()
    else:
        asyncio.run(measure())


if __name__ == "__main__":
    main()
