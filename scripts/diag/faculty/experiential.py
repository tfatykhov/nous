"""Phase 0 EXPERIENTIAL class — born full-cycle, then measured on both lenses.

Two DISSIMILAR facts stated in ONE real session (a person-meeting + a tech-decision).
The query asks one via the other's *shared episode context*, not similarity. The only
bridge is co-occurrence in the same episode — exactly the experiential association the
current similarity pipe lacks.

Phase 1: ingest the session full-cycle via the live faculty instance (8078) -> session
end (fact extraction, shared source_episode_id) -> sleep (episode<->fact edges).
Phase 2: BARE lens — run_recall_pipeline, does the answer fact surface?
Phase 3: AGENTIC lens — /chat, does the agent bridge via the shared episode?

  uv run python scripts/diag/faculty/run_faculty_instance.py   # (already running)
  uv run python scripts/diag/faculty/experiential.py
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

from scripts.diag.faculty.corpus import EXPERIENTIAL  # noqa: E402

AGENT = "nous-faculty-eval"
BASE = "http://127.0.0.1:8078"
K = 10
ITEM = EXPERIENTIAL[0]
RUN_DIR = HERE / "runs" / (time.strftime("%Y%m%d-%H%M%S") + "-experiential")
RUN_DIR.mkdir(parents=True, exist_ok=True)
LOG = open(RUN_DIR / "result.txt", "w", encoding="utf-8")
PROMPT = "{q}\n\n(Answer from what you know about my work and contacts. If you don't have that information, just say so.)"
_ABSTAIN = ("don't have", "do not have", "don't know", "no record", "not aware",
            "nothing about", "don't have that information", "no mention", "couldn't find")


def out(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    LOG.write(s + "\n")
    LOG.flush()


def psql(q: str) -> str:
    return subprocess.run(
        ["docker", "exec", "nous-eval-scratch", "psql", "-U", "nous", "-d", "nous_eval_live", "-tAc", q],
        capture_output=True, text=True).stdout.strip()


def ingest_full_cycle() -> None:
    out("--- Phase 1: full-cycle ingest (one session, two dissimilar facts) ---")
    sid = f"exp-{int(time.time())}"
    prev = int(psql(f"SELECT count(structured_summary) FROM heart.episodes WHERE agent_id='{AGENT}'") or 0)
    for note in ITEM["session_notes"]:
        prompt = ("Remember this note about my life for later. Reply only 'noted'. "
                  "Do not use any tools.\n\n" + note)
        try:
            httpx.post(f"{BASE}/chat", json={"message": prompt, "session_id": sid}, timeout=280)
        except Exception as ex:
            out(f"  (chat err: {ex})")
    try:
        httpx.delete(f"{BASE}/chat/{sid}", timeout=40)
    except Exception:
        pass
    t0 = time.monotonic()
    while time.monotonic() - t0 < 120:
        cur = int(psql(f"SELECT count(structured_summary) FROM heart.episodes WHERE agent_id='{AGENT}'") or 0)
        if cur > prev:
            break
        time.sleep(4)
    try:
        httpx.post(f"{BASE}/sleep/trigger", timeout=300)
    except Exception as ex:
        out(f"  (sleep err: {ex})")
    time.sleep(6)
    # Did both facts form, sharing an episode?
    ans = ITEM["answer_token"].split()[0]
    nf = psql(f"SELECT count(*) FROM heart.facts WHERE agent_id='{AGENT}' AND content ILIKE '%{ans}%'")
    nshare = psql(
        f"SELECT count(DISTINCT source_episode_id) FROM heart.facts WHERE agent_id='{AGENT}' "
        f"AND (content ILIKE '%{ans}%' OR content ILIKE '%Halberd%') AND source_episode_id IS NOT NULL")
    out(f"  facts naming answer='{ITEM['answer_token']}': {nf}; distinct episodes shared by the two: {nshare}")


async def bare_lens() -> None:
    from nous.api.retrieval_pipeline import run_recall_pipeline
    from nous.brain.brain import Brain
    from nous.brain.embeddings import EmbeddingProvider
    from nous.config import Settings
    from nous.heart.heart import Heart
    from nous.storage.database import Database

    s = Settings().model_copy(update={
        "graph_neighbor_seed_score_enabled": True, "heart_graph_all_types_enabled": True,
        "graph_adjacency_boost_enabled": True, "residual_activation_enabled": False,
    })
    db = Database(s); await db.connect()
    emb = EmbeddingProvider(api_key=s.openai_api_key, model="text-embedding-3-large", dimensions=1536)
    heart = Heart(database=db, settings=s, embedding_provider=emb); brain = Brain(db, s, emb)
    out("\n--- Phase 2: BARE lens ---")
    res, _ = await run_recall_pipeline(ITEM["query"], heart, brain, s, limit=30, rerank_by_score=True)
    rank = next((i + 1 for i, r in enumerate(res) if ITEM["answer_token"].lower() in (r.description or "").lower()), None)
    out(f"  Q={ITEM['query']!r}  answer('{ITEM['answer_token']}')_rank={rank}  top{K}={bool(rank and rank <= K)}  predict=FAIL")
    await db.disconnect()


def agentic_lens() -> None:
    out("\n--- Phase 3: AGENTIC lens ---")
    sid = f"exp-q-{int(time.time()*1000)}"
    try:
        r = httpx.post(f"{BASE}/chat", json={"message": PROMPT.format(q=ITEM["query"]), "session_id": sid}, timeout=280)
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    except Exception as ex:
        out(f"  (chat err: {ex})"); return
    finally:
        try:
            httpx.delete(f"{BASE}/chat/{sid}", timeout=20)
        except Exception:
            pass
    text = ""
    for key in ("response", "content", "message"):
        if isinstance(data.get(key), str) and data[key].strip():
            text = data[key].strip(); break
    tc = int((data.get("usage") or {}).get("tool_calls", -1))
    a = text.lower()
    if ITEM["answer_token"].lower() in a:
        verdict = "PASS (bridged via shared experience)"
    elif any(c in a for c in _ABSTAIN):
        verdict = "FAIL (abstained — no experiential bridge)"
    else:
        verdict = f"FAIL (wrong: {text[:60]!r})"
    out(f"  Q={ITEM['query']!r}  tool_calls={tc}")
    out(f"  ans: {text[:300]}")
    out(f"  -> {verdict}")


def main() -> None:
    out(f"run dir: {RUN_DIR}")
    try:
        out(f"instance health: {httpx.get(f'{BASE}/health', timeout=5).text}")
    except Exception as ex:
        out(f"!! faculty instance not on {BASE} ({ex})"); return
    ingest_full_cycle()
    asyncio.run(bare_lens())
    agentic_lens()
    LOG.close()


if __name__ == "__main__":
    main()
