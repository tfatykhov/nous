"""Phase 0 FULL-CYCLE — ingest the whole faculty corpus through the live instance
(extraction + sleep), then measure all four classes on both lenses. No direct-load.

  uv run python scripts/diag/faculty/run_faculty_instance.py   # instance on 8078 (with the tool fix)
  uv run python scripts/diag/faculty/fc_phase0.py ingest        # full-cycle ingest + sleep (long)
  uv run python scripts/diag/faculty/fc_phase0.py measure       # bare + agentic, all classes

Session layout (deliberate): concept-bridge hop1 facts and bridge facts go in DIFFERENT
sessions (so the only link between them is the shared single-token CONCEPT, not a shared
episode); the experiential pair goes in ONE session (the bridge IS the shared episode);
controls/goal-gated/used-before/filler spread across sessions.
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

from scripts.diag.faculty.corpus import (  # noqa: E402
    CONCEPT_BRIDGE, CONTROL_NEGATIVE, CONTROL_POSITIVE, EXPERIENTIAL,
    FILLER, GOAL_GATED, PREDICTIONS, USED_BEFORE,
)

AGENT = "nous-faculty-eval"
BASE = "http://127.0.0.1:8078"
K = 10
PROMPT = "{q}\n\n(Answer from what you know about my work and contacts. If you don't have that information, just say so.)"
_ABSTAIN = ("don't have", "do not have", "don't know", "no record", "not aware", "nothing about",
            "don't have that information", "no mention", "couldn't find", "no entry", "i have no")
RUN_DIR = HERE / "runs" / (time.strftime("%Y%m%d-%H%M%S") + "-fullcycle")
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


def _sessions() -> list[list[str]]:
    """Group notes into sessions (see module docstring for the deliberate layout)."""
    exp = list(EXPERIENTIAL[0]["session_notes"])                       # S0: shared-episode pair
    s_hop1 = [c["hop1"] for c in CONCEPT_BRIDGE] + [CONTROL_POSITIVE["facts"][0],
              GOAL_GATED[0]["facts"][0], USED_BEFORE[0]["facts"][0]]   # S1
    s_bridge = [c["bridge"] for c in CONCEPT_BRIDGE] + [GOAL_GATED[0]["facts"][1],
                USED_BEFORE[0]["facts"][1]]                             # S2
    f = FILLER
    return [exp, s_hop1, s_bridge, f[0:9], f[9:18], f[18:25]]


def ingest() -> None:
    out("=== FULL-CYCLE INGEST ===")
    for tbl in ("brain.graph_edges", "heart.episode_chunks", "heart.facts",
                "brain.decisions", "heart.working_memory", "nous_system.events", "heart.episodes"):
        psql(f"DELETE FROM {tbl} WHERE agent_id='{AGENT}'")
    out("reset namespace.")
    for si, notes in enumerate(_sessions()):
        sid = f"fc-{si}-{int(time.time())}"
        prev = int(psql(f"SELECT count(structured_summary) FROM heart.episodes WHERE agent_id='{AGENT}'") or 0)
        for note in notes:
            p = "Remember this note about my life for later. Reply only 'noted'. Do not use any tools.\n\n" + note
            try:
                httpx.post(f"{BASE}/chat", json={"message": p, "session_id": sid}, timeout=280)
            except Exception as ex:
                out(f"  (chat err: {ex})")
        try:
            httpx.delete(f"{BASE}/chat/{sid}", timeout=40)
        except Exception:
            pass
        t0 = time.monotonic()
        while time.monotonic() - t0 < 120:
            if int(psql(f"SELECT count(structured_summary) FROM heart.episodes WHERE agent_id='{AGENT}'") or 0) > prev:
                break
            time.sleep(4)
        nf = psql(f"SELECT count(*) FROM heart.facts WHERE agent_id='{AGENT}'")
        out(f"  session {si} ({len(notes)} notes) done; facts={nf}")
    out("\ntriggering sleep (cosine backfill + co_mention + episode edges + ...)...")
    try:
        httpx.post(f"{BASE}/sleep/trigger", timeout=400)
    except Exception as ex:
        out(f"  (sleep err: {ex})")
    time.sleep(8)
    nf = psql(f"SELECT count(*) FROM heart.facts WHERE agent_id='{AGENT}'")
    ne = psql(f"SELECT count(*) FROM brain.graph_edges WHERE agent_id='{AGENT}'")
    out(f"final: facts={nf} edges={ne}")
    # experiential episode-sharing check
    exp_ans = EXPERIENTIAL[0]["answer_token"].split()[0]
    nshare = psql(f"SELECT count(*) FROM (SELECT source_episode_id FROM heart.facts WHERE agent_id='{AGENT}' "
                  f"AND (content ILIKE '%{exp_ans}%' OR content ILIKE '%Halberd%') AND source_episode_id IS NOT NULL "
                  f"GROUP BY source_episode_id HAVING count(*)>=2) t")
    out(f"experiential: episodes containing BOTH the Halberd + Gus facts: {nshare}")


def ask(query: str) -> tuple[str, int]:
    sid = f"fcq-{int(time.time()*1000)}"
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


def _agentic_verdict(ans: str, token: str | None) -> str:
    a = ans.lower()
    if token is None:
        return "PASS(abstain)" if any(c in a for c in _ABSTAIN) else f"FAIL(fabricated:{ans[:50]!r})"
    if token.lower() in a:
        return "PASS"
    return "FAIL(abstain)" if any(c in a for c in _ABSTAIN) else f"FAIL(wrong:{ans[:50]!r})"


async def measure() -> None:
    from nous.api.retrieval_pipeline import run_recall_pipeline
    from nous.brain.brain import Brain
    from nous.brain.embeddings import EmbeddingProvider
    from nous.config import Settings
    from nous.heart.heart import Heart
    from nous.storage.database import Database

    out("=== FULL-CYCLE MEASURE ===\nPRE-REGISTERED:")
    for k, v in PREDICTIONS.items():
        out(f"   {k:18} -> {v}")
    s = Settings().model_copy(update={
        "graph_neighbor_seed_score_enabled": True, "heart_graph_all_types_enabled": True,
        "graph_adjacency_boost_enabled": True, "residual_activation_enabled": False})
    db = Database(s); await db.connect()
    emb = EmbeddingProvider(api_key=s.openai_api_key, model="text-embedding-3-large", dimensions=1536)
    heart = Heart(database=db, settings=s, embedding_provider=emb); brain = Brain(db, s, emb)

    async def bare_rank(query, ilike):
        res, _ = await run_recall_pipeline(query, heart, brain, s, limit=30, rerank_by_score=True)
        return next((i + 1 for i, r in enumerate(res) if ilike.lower() in (r.description or "").lower()), None)

    out("\n--- BARE lens ---")
    pr = await bare_rank(CONTROL_POSITIVE["query"], CONTROL_POSITIVE["answer_token"])
    out(f"  [control_pos] answer_rank={pr} top{K}={bool(pr and pr<=K)} (predict PASS)")
    for c in CONCEPT_BRIDGE:
        br = await bare_rank(c["query"], c["answer_token"])
        out(f"  [cb {c['id']:9}] answer_rank={br} disjoint={br is None or br>K} recovered={bool(br and br<=K)} (predict FAIL)")
    er = await bare_rank(EXPERIENTIAL[0]["query"], EXPERIENTIAL[0]["answer_token"])
    out(f"  [experiential] answer_rank={er} disjoint={er is None or er>K} recovered={bool(er and er<=K)} (predict FAIL)")
    await db.disconnect()

    out("\n--- AGENTIC lens ---")
    pa, ptc = ask(CONTROL_POSITIVE["query"]); out(f"  [control_pos] tc={ptc} -> {_agentic_verdict(pa, CONTROL_POSITIVE['answer_token'])}  ans={pa[:90]!r}")
    na, ntc = ask(CONTROL_NEGATIVE["query"]); out(f"  [control_neg] tc={ntc} -> {_agentic_verdict(na, None)}  ans={na[:90]!r}")
    for c in CONCEPT_BRIDGE:
        a, tc = ask(c["query"]); out(f"  [cb {c['id']:9}] tc={tc} -> {_agentic_verdict(a, c['answer_token'])}  ans={a[:90]!r}")
    ea, etc = ask(EXPERIENTIAL[0]["query"]); out(f"  [experiential] tc={etc} -> {_agentic_verdict(ea, EXPERIENTIAL[0]['answer_token'])}  ans={ea[:110]!r}")

    out("\n--- GOAL-GATED (agentic, 2 goals; does the goal change WHICH fact surfaces?) ---")
    g = GOAL_GATED[0]
    for goal, q, tok in [(g["goal_a"], g["query_a"], g["relevant_a_token"]), (g["goal_b"], g["query_b"], g["relevant_b_token"])]:
        a, tc = ask(f"{goal} {q}")
        hit = tok.lower() in a.lower()
        out(f"  goal={goal!r}\n    -> surfaces '{tok}'? {hit}  tc={tc}  ans={a[:120]!r}")

    out("\n--- USED-BEFORE (null by construction; live confirm edge weights don't change with use) ---")
    out("  (audit-established: track_access updates recall_count/last_recalled_at, NEVER edge weights;")
    out("   no co-activation edge forms. Plasticity is the Phase-1 build, absent today.)")
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
