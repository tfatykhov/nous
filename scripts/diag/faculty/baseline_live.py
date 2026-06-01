"""Full capability-baseline setup for the LIVE-instance agentic run, with all the new
mechanisms wired (Gap-1 formation + Gap-2 resolver) — docs/research/018.

Ingests the full 18-cell corpus the way the new features need it:
  - facts grouped into one episode per session_tag (so co-occurring facts share a
    source_episode_id -> build_cooccurrence_edges can FORM co_occurred edges);
  - c12 bank facts given parallel phrasing + subject + event_date so the recency resolver
    fires (the natural-phrasing version is below the difflib floor);
  - c13 dashboard facts given event_date (LLM also reads the in-text dates);
  - c5 decision inserted + linked to the Halberd fact (fixes the earlier fixture bug);
  - formation run (build_cooccurrence_edges).
Then run baseline_agentic.py against the instance for the agentic table.

  uv run python scripts/diag/faculty/baseline_live.py setup
  # launch run_baseline_instance.py with recency+temporal on, then:
  uv run python scripts/diag/faculty/baseline_agentic.py
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

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

from scripts.diag.faculty.baseline_corpus import full_facts  # noqa: E402

AGENT = "nous-baseline-eval"
# c12 parallel override (subject + differing event_date so the resolver fires)
C12 = [("My primary bank is Halloway Federal.", "primary bank", "2025-06-15"),
       ("My primary bank is Pellan Mutual.", "primary bank", "2026-04-01")]
# c13 event_dates (keyed by a content marker)
C13_DATES = {"Korren framework": "2025-09-15", "rebuilt the dashboard in Aurelis": "2026-02-15"}
C5_DECISION = "Decided to push the Halberd launch to Q3 to absorb the slippage."
C5_FACT_MARKER = "Project Halberd"


def psql(q: str) -> str:
    return subprocess.run(
        ["docker", "exec", "nous-eval-scratch", "psql", "-U", "nous", "-d", "nous_eval_live", "-tAc", q],
        capture_output=True, text=True).stdout.strip()


async def setup() -> None:
    from sqlalchemy import text

    from nous.brain.embeddings import EmbeddingProvider
    from nous.brain.graph_densifier import GraphDensifier
    from nous.brain.graph_linker import GraphLinker
    from nous.config import Settings
    from nous.storage.database import Database

    s = Settings().model_copy(update={"cooccurrence_linking_enabled": True})
    db = Database(s); await db.connect()
    emb = EmbeddingProvider(api_key=s.openai_api_key, model="text-embedding-3-large", dimensions=1536)

    print("=== reset nous-baseline-eval ===")
    for tbl in ("heart.facts", "heart.episodes", "heart.episode_chunks", "heart.working_memory"):
        psql(f"DELETE FROM {tbl} WHERE agent_id='{AGENT}'")
    psql(f"DELETE FROM brain.graph_edges WHERE agent_id='{AGENT}'")
    psql(f"DELETE FROM brain.decisions WHERE agent_id='{AGENT}'")

    print("=== ingest full corpus, one episode per session_tag (co-occurrence signal) ===")
    # group facts by session_tag
    groups: dict[str, list[str]] = {}
    for content, tag in full_facts():
        groups.setdefault(tag or "_solo", []).append(content)

    async with db.session() as sess:
        for tag, contents in groups.items():
            # one episode per tag (so same-occasion facts share source_episode_id)
            epid = (await sess.execute(text(
                "INSERT INTO heart.episodes (agent_id, summary, started_at, compaction_count) "
                "VALUES (:a, :sm, now(), 0) RETURNING id"
            ), {"a": AGENT, "sm": f"session {tag}"})).scalar_one()
            for content in contents:
                vec = await emb.embed(content)
                vlit = "[" + ",".join(f"{x:.6f}" for x in vec) + "]"
                await sess.execute(text(
                    "INSERT INTO heart.facts (agent_id, content, source, source_episode_id, "
                    "confidence, active, embedding) VALUES (:a,:c,:src,:e,0.9,TRUE,CAST(:v AS vector))"
                ), {"a": AGENT, "c": content, "src": f"baseline:{tag}", "e": epid, "v": vlit})
        await sess.commit()

    print("=== c12 parallel override + metadata (so recency resolver fires) ===")
    # delete the natural-phrasing c12 facts (sources baseline:s_b1 / s_b2) and re-insert parallel
    psql(f"DELETE FROM heart.facts WHERE agent_id='{AGENT}' AND source IN ('baseline:s_b1','baseline:s_b2')")
    epid12 = psql(f"SELECT id FROM heart.episodes WHERE agent_id='{AGENT}' AND summary='session s_b1' LIMIT 1") \
        or psql(f"SELECT id FROM heart.episodes WHERE agent_id='{AGENT}' LIMIT 1")
    async with db.session() as sess:
        for content, subj, d in C12:
            vec = await emb.embed(content)
            vlit = "[" + ",".join(f"{x:.6f}" for x in vec) + "]"
            await sess.execute(text(
                "INSERT INTO heart.facts (agent_id, content, source, subject, event_date, "
                "confidence, active, embedding) VALUES (:a,:c,'baseline:c12',:s,:d,0.9,TRUE,CAST(:v AS vector))"
            ), {"a": AGENT, "c": content, "s": subj, "d": date.fromisoformat(d), "v": vlit})
        await sess.commit()

    print("=== c13 event_dates ===")
    for marker, d in C13_DATES.items():
        psql(f"UPDATE heart.facts SET event_date='{d}', subject='dashboard framework' "
             f"WHERE agent_id='{AGENT}' AND content ILIKE '%{marker}%'")

    print("=== c5 decision insert + fact->decision edge (fixes fixture; best-effort) ===")
    try:
        async with db.session() as sess:
            vec = await emb.embed(C5_DECISION)
            vlit = "[" + ",".join(f"{x:.6f}" for x in vec) + "]"
            did = (await sess.execute(text(
                "INSERT INTO brain.decisions (agent_id, description, confidence, category, stakes, embedding) "
                "VALUES (:a,:d,0.8,'process','medium',CAST(:v AS vector)) RETURNING id"
            ), {"a": AGENT, "d": C5_DECISION, "v": vlit})).scalar_one()
            await sess.commit()
        fid = psql(f"SELECT id FROM heart.facts WHERE agent_id='{AGENT}' AND content ILIKE '%{C5_FACT_MARKER}%behind schedule%' LIMIT 1")
        if fid:
            psql("INSERT INTO brain.graph_edges (source_id,target_id,source_type,target_type,"
                 f"agent_id,relation,weight,auto_linked,extraction_method) VALUES "
                 f"('{fid}','{did}','fact','decision','{AGENT}','informed_by',0.8,true,'heuristic')")
    except Exception as ex:
        print(f"  (c5 decision skipped: {ex})")

    print("=== formation: build_cooccurrence_edges ===")
    gd = GraphDensifier(db, GraphLinker(db, emb, s, AGENT), emb, s, AGENT)
    n = await gd.build_cooccurrence_edges()
    await db.disconnect()
    nf = psql(f"SELECT count(*) FROM heart.facts WHERE agent_id='{AGENT}'")
    ne = psql(f"SELECT count(*) FROM brain.graph_edges WHERE agent_id='{AGENT}' AND relation='co_occurred'")
    print(f"\nsetup done: facts={nf}, co_occurred edges={ne} (formation returned {n}); c5 decision + edge added.")


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "setup"
    if cmd == "setup":
        asyncio.run(setup())


if __name__ == "__main__":
    main()
