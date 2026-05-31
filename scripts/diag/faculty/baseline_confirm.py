"""Agentic end-to-end confirmation (advisor-mandated, two-lens completion).

Bare lens already showed a formed edge / resolver surfaces the right fact in recall_deep.
This tests the ballgame: does the AGENT'S ANSWER change when routed through the graph-aware
path? If yes, path-unification's value is real; if no, the plan is wrong (build saved).

  setup : (DB) add c12 bank facts + subject/event_date + inject c11 nh_pim edge
  ask   : (instance up, resolver+temporal ON) force recall_deep, check if answers flip

  uv run python scripts/diag/faculty/baseline_confirm.py setup
  # launch run_baseline_instance.py with NOUS_RECENCY_RESOLVER_ENABLED=true NOUS_TEMPORAL_EXTRACTION_ENABLED=true
  uv run python scripts/diag/faculty/baseline_confirm.py ask
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import date
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
    "DB_USER": "nous", "DB_PASSWORD": "nous_eval", "NOUS_AGENT_ID": "nous-baseline-eval",
})

import subprocess  # noqa: E402

from scripts.diag.faculty.baseline_corpus import NO_HANDLE_PAIRS  # noqa: E402

AGENT = "nous-baseline-eval"
BASE = "http://127.0.0.1:8079"
C12 = [("My primary bank is Halloway Federal.", "primary bank", "2025-06-15"),
       ("My primary bank is Pellan Mutual.", "primary bank", "2026-04-01")]


def psql(q: str) -> str:
    return subprocess.run(
        ["docker", "exec", "nous-eval-scratch", "psql", "-U", "nous", "-d", "nous_eval_live", "-tAc", q],
        capture_output=True, text=True).stdout.strip()


async def setup() -> None:
    from sqlalchemy import text

    from nous.brain.embeddings import EmbeddingProvider
    from nous.config import Settings
    from nous.storage.database import Database

    s = Settings()
    db = Database(s); await db.connect()
    emb = EmbeddingProvider(api_key=s.openai_api_key, model="text-embedding-3-large", dimensions=1536)
    # c12 facts (idempotent: delete prior, re-insert)
    psql(f"DELETE FROM heart.facts WHERE agent_id='{AGENT}' AND subject='primary bank'")
    async with db.session() as sess:
        for content, subj, d in C12:
            vec = await emb.embed(content)
            vlit = "[" + ",".join(f"{x:.6f}" for x in vec) + "]"
            await sess.execute(text(
                "INSERT INTO heart.facts (agent_id, content, source, subject, event_date, confidence, active, embedding) "
                "VALUES (:a,:c,'confirm',:s,:d,0.9,TRUE,CAST(:v AS vector))"
            ), {"a": AGENT, "c": content, "s": subj, "d": date.fromisoformat(d), "v": vlit})
        await sess.commit()
    await db.disconnect()
    # inject c11 nh_pim edge (Pim seed -> Galt target)
    pim = next(p for p in NO_HANDLE_PAIRS if p["id"] == "nh_pim")
    sid = psql(f"SELECT id FROM heart.facts WHERE agent_id='{AGENT}' AND content=$${pim['seed']}$$ LIMIT 1")
    tid = psql(f"SELECT id FROM heart.facts WHERE agent_id='{AGENT}' AND content=$${pim['target']}$$ LIMIT 1")
    psql(f"DELETE FROM brain.graph_edges WHERE agent_id='{AGENT}' AND extraction_method='heuristic'")
    if sid and tid:
        psql("INSERT INTO brain.graph_edges (source_id,target_id,source_type,target_type,"
             "agent_id,relation,weight,auto_linked,extraction_method) VALUES "
             f"('{sid}','{tid}','fact','fact','{AGENT}','related_to',0.9,false,'heuristic')")
    c12n = psql(f"SELECT count(*) FROM heart.facts WHERE agent_id='{AGENT}' AND subject='primary bank'")
    print(f"setup done: c12 facts={c12n} c11 edge seed={sid[:8] if sid else None} target={tid[:8] if tid else None}")


def ask(query: str) -> tuple[str, int]:
    sid = f"cf-{int(time.time()*1000)}"
    data: dict = {}
    prompt = ("Search your memory with the recall tool before answering. " + query)
    try:
        r = httpx.post(f"{BASE}/chat", json={"message": prompt, "session_id": sid, "debug": True}, timeout=280)
        if r.headers.get("content-type", "").startswith("application/json"):
            data = r.json()
    except Exception as ex:
        return f"(err {ex})", -1
    finally:
        try:
            httpx.delete(f"{BASE}/chat/{sid}", timeout=20)
        except Exception:
            pass
    txt = ""
    for k in ("response", "content", "message"):
        if isinstance(data.get(k), str) and data[k].strip():
            txt = data[k].strip(); break
    return (txt or "(empty)"), int((data.get("usage") or {}).get("tool_calls", -1))


def run_ask() -> None:
    try:
        httpx.get(f"{BASE}/health", timeout=5)
    except Exception as ex:
        print(f"!! instance not up on {BASE} ({ex})"); return
    print("=== AGENTIC CONFIRMATION (recall_deep forced, resolver+temporal ON) ===\n")
    # c12 resolution: does the answer flip from stale Halloway -> current Pellan Mutual?
    a, tc = ask("What is my current primary bank?")
    low = a.lower()
    print(f"[c12 resolution] tc={tc}  pellan={'Pellan'.lower() in low}  halloway={'halloway' in low}")
    print(f"   verdict: {'PASS (flipped to current)' if 'pellan' in low and 'halloway' not in low else ('CONFAB (stale)' if 'halloway' in low and 'pellan' not in low else 'mixed/other')}")
    print(f"   ans: {a[:200]}\n")
    # c11 formation (edge injected): does the agent SAY the co-experienced Galt fact?
    a2, tc2 = ask("Tell me about the day I adopted my greyhound Pim.")
    print(f"[c11 formation+injection] tc={tc2}  galt_surfaced={'galt' in a2.lower()}")
    print(f"   verdict: {'PASS (associative fact surfaced)' if 'galt' in a2.lower() else 'MISS (co-experienced fact not recalled)'}")
    print(f"   ans: {a2[:200]}")


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "ask"
    if cmd == "setup":
        asyncio.run(setup())
    else:
        run_ask()


if __name__ == "__main__":
    main()
