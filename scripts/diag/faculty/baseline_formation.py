"""Formation MVP (Gap 1) — prove the mechanism before prod code.

Cell 11 needs the co-occurrence EDGE to exist. The formatter fix already showed an injected
edge reaches the agent. This proves the edge can be FORMED from the co-occurrence signal
(shared source episode) instead of injected by hand:

  setup     : (DB) put each no-handle pair's two facts into ONE shared episode (source_episode_id)
  formation : (DB) the mechanism — for facts sharing an episode, create related_to co-activation
              edges (capped, weight 0.5). NO per-pair hand-injection. Noise gate: skip big episodes.
  ask       : (instance up) cell 11 linked-cue -> does the agent surface the co-experienced fact
              with the edge that FORMATION created (not injection)?

  uv run python scripts/diag/faculty/baseline_formation.py setup
  uv run python scripts/diag/faculty/baseline_formation.py formation
  # launch run_baseline_instance.py
  uv run python scripts/diag/faculty/baseline_formation.py ask
"""
from __future__ import annotations

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
    "DB_USER": "nous", "DB_PASSWORD": "nous_eval", "NOUS_AGENT_ID": "nous-baseline-eval",
})

from scripts.diag.faculty.baseline_corpus import NO_HANDLE_PAIRS  # noqa: E402

AGENT = "nous-baseline-eval"
BASE = "http://127.0.0.1:8079"
MAX_EPISODE_FACTS = 6   # noise gate: don't clique a rambling episode
COOCCUR_WEIGHT = 0.5
ASK_PAIRS = ["nh_pim", "nh_sable", "nh_osei"]


def psql(q: str) -> str:
    return subprocess.run(
        ["docker", "exec", "nous-eval-scratch", "psql", "-U", "nous", "-d", "nous_eval_live", "-tAc", q],
        capture_output=True, text=True).stdout.strip()


def setup() -> None:
    print("=== SETUP: put each no-handle pair's 2 facts into one shared episode ===")
    # clear any prior hand-injected edges so we only test FORMED edges
    psql(f"DELETE FROM brain.graph_edges WHERE agent_id='{AGENT}' AND extraction_method IN ('heuristic','co_occurrence')")
    for p in NO_HANDLE_PAIRS:
        epid = psql(
            "INSERT INTO heart.episodes (agent_id, summary, started_at, compaction_count) "
            f"VALUES ('{AGENT}', 'co-occurrence session for {p['id']}', now(), 0) RETURNING id"
        )
        if not epid:
            print(f"  !! could not create episode for {p['id']}"); continue
        for role in ("seed", "target"):
            psql(f"UPDATE heart.facts SET source_episode_id='{epid}' "
                 f"WHERE agent_id='{AGENT}' AND source='baseline:{p['id']}:{role}'")
        n = psql(f"SELECT count(*) FROM heart.facts WHERE agent_id='{AGENT}' AND source_episode_id='{epid}'")
        print(f"  {p['id']}: episode {epid[:8]} now holds {n} facts")


def formation() -> None:
    """Run the REAL feature: GraphDensifier.build_cooccurrence_edges (co_occurred edges)."""
    import asyncio
    asyncio.run(_formation_async())


async def _formation_async() -> None:
    from nous.brain.embeddings import EmbeddingProvider
    from nous.brain.graph_densifier import GraphDensifier
    from nous.brain.graph_linker import GraphLinker
    from nous.config import Settings
    from nous.storage.database import Database

    print("=== FORMATION: real GraphDensifier.build_cooccurrence_edges (relation='co_occurred') ===")
    # clear prior MVP/heuristic edges so the pre-existing-edge guard doesn't block the pairs
    psql(f"DELETE FROM brain.graph_edges WHERE agent_id='{AGENT}' AND extraction_method IN ('heuristic','co_occurrence')")
    s = Settings().model_copy(update={"cooccurrence_linking_enabled": True})
    db = Database(s); await db.connect()
    emb = EmbeddingProvider(api_key=s.openai_api_key, model="text-embedding-3-large", dimensions=1536)
    gd = GraphDensifier(db, GraphLinker(db, emb, s, AGENT), emb, s, AGENT)
    n = await gd.build_cooccurrence_edges()
    await db.disconnect()
    n_co = psql(f"SELECT count(*) FROM brain.graph_edges WHERE agent_id='{AGENT}' AND relation='co_occurred'")
    print(f"  build_cooccurrence_edges returned {n}; co_occurred edges in DB: {n_co}")


def ask(query: str) -> tuple[str, int]:
    sid = f"fm-{int(time.time()*1000)}"
    data: dict = {}
    try:
        r = httpx.post(f"{BASE}/chat", json={"message": "Search your memory with the recall tool first. " + query,
                       "session_id": sid, "debug": True}, timeout=280)
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
        print(f"!! instance not up ({ex})"); return
    print("=== ASK: cell 11 linked-cue, edge from FORMATION (no injection) ===\n")
    npass = 0
    for pid in ASK_PAIRS:
        p = next(x for x in NO_HANDLE_PAIRS if x["id"] == pid)
        seed_noun = p["seed"]
        cue = f"What memories do you have that are linked or connected to this: \"{seed_noun}\"?"
        a, tc = ask(cue)
        tok = p["answer_token"]
        hit = tok.lower() in a.lower()
        npass += hit
        print(f"[{pid}] surfaced '{tok}'={hit} tc={tc}")
        print(f"   ans: {a[:200]}\n")
    print(f"=== FORMATION GATE: {npass}/{len(ASK_PAIRS)} pairs surfaced the co-experienced fact WITHOUT injection ===")


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "ask"
    {"setup": setup, "formation": formation, "ask": run_ask}.get(cmd, run_ask)()


if __name__ == "__main__":
    main()
