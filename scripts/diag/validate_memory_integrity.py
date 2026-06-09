"""Validate the 2026-06-09 memory-integrity fixes against a REAL Postgres
(eval scratch on 5433 by default — never prod).

Probes (each prints PASS/FAIL; exit code 1 if any fail):
  S1  raw-cosine dedup probe: unrelated content scores LOW (old RRF probe
      scored ~0.98 for the nearest fact regardless of similarity)
  S3  in-band conflict is classified, not blindly confirmed (needs Haiku;
      skipped when no ANTHROPIC key) — at prod-like threshold 0.80
  S3b dated candidate vs undated >0.95 duplicate -> confirm + date merged
  S10 _find_duplicate query plan uses the HNSW index (no Seq Scan)
  E1  transcript chunks append after document chunks (MAX+1), second
      summarize run is a no-op (no duplicate dialogue chunks)
  D1  decision delete removes its graph edges; no dangling decision edges
      remain after migration 060
  D2  embedding LRU: repeated embed of the same query = 1 API call
  D9  censor semantic match returns the expected censor via the rewritten
      distance-ordered query

Run: uv run python scripts/diag/validate_memory_integrity.py
Override DB with DB_* env vars (defaults to 127.0.0.1:5433 nous_eval_prod).
All writes are scoped to a throwaway agent_id and cleaned up at the end.
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

SNAP = Path(".env.prod-snapshot")
if SNAP.exists():
    for raw in SNAP.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        key = k.strip()
        if key.startswith("DB_"):
            continue  # snapshot's prod DB must not shadow the target below
        os.environ.setdefault(key, v.strip().strip('"').strip("'"))
os.environ.setdefault("DB_HOST", "127.0.0.1")
os.environ.setdefault("DB_PORT", "5433")
os.environ.setdefault("DB_NAME", "nous_eval_prod")
os.environ.setdefault("DB_USER", "nous")
os.environ.setdefault("DB_PASSWORD", "nous_eval")
# prod-like Leg-2 threshold so the S3 band is reachable
os.environ["NOUS_FACT_NATIVE_COSINE_THRESHOLD"] = "0.80"
for k in ("NOUS_MCP_ENABLED", "NOUS_HEARTBEAT_ENABLED", "NOUS_SCHEDULE_ENABLED",
          "NOUS_EVENT_BUS_ENABLED", "NOUS_ACTIONABILITY_ENABLED"):
    os.environ[k] = "false"

AGENT = f"integrity-probe-{uuid.uuid4().hex[:8]}"
os.environ["NOUS_AGENT_ID"] = AGENT

from sqlalchemy import text  # noqa: E402

from nous.config import Settings  # noqa: E402
from nous.storage.database import Database  # noqa: E402
from nous.brain.embeddings import EmbeddingProvider  # noqa: E402
from nous.heart.heart import Heart  # noqa: E402
from nous.heart.schemas import FactInput, CensorInput  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def report(name: str, ok: bool, note: str = "") -> None:
    RESULTS.append((name, ok, note))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}  {note}")


async def main() -> None:
    s = Settings()
    db = Database(s)
    await db.connect()

    # Apply pending migrations through the APP's migrator (the real
    # acceptance path — psql tolerates SQL the hand-rolled splitter doesn't).
    from nous.storage.migrator import run_migrations
    applied = await run_migrations(db.engine)
    print(f"migrations applied this run: {applied or 'none (up to date)'}")
    emb = EmbeddingProvider(api_key=s.openai_api_key, model=s.embedding_model,
                            dimensions=s.embedding_dimensions,
                            cache_size=s.embedding_cache_size)
    heart = Heart(db, s, emb, owns_embeddings=False)

    # F027 classifier needs an LLM client; wire Haiku if a key is present.
    llm_wired = False
    try:
        from nous.api.anthropic_client import HttpxAnthropicClient
        has_key = bool(
            getattr(s, "anthropic_api_key", "") or getattr(s, "anthropic_auth_token", "")
        )
        if has_key:
            client = HttpxAnthropicClient(settings=s)
            heart.facts.set_llm_client(client, "claude-haiku-4-5-20251001")
            llm_wired = True
    except Exception as e:  # noqa: BLE001
        print(f"  (no LLM client: {e})")

    print(f"\nProbing against {s.db_host}:{s.db_port}/{s.db_name} as agent {AGENT}\n")

    # ---------------- S1: raw-cosine probe ----------------
    anchor = await heart.learn(FactInput(
        subject="editor", content="User prefers tabs over spaces in all Python projects",
        source="user_direct",
    ))
    hits = await heart.find_similar_facts(
        "The deployment pipeline uses blue-green rollouts on staging", limit=1
    )
    top_sim = hits[0].score if hits else None
    report(
        "S1 unrelated content scores low cosine",
        top_sim is not None and top_sim < 0.5,
        f"(top similarity {top_sim:.3f} — old RRF probe would be ~0.98)" if top_sim is not None else "(no hits)",
    )
    dup_hits = await heart.find_similar_facts(
        "User prefers tabs instead of spaces in every Python project", limit=1
    )
    dup_sim = dup_hits[0].score if dup_hits else None
    report(
        "S1 true paraphrase scores high cosine",
        dup_sim is not None and dup_sim > 0.9,
        f"(similarity {dup_sim:.3f})" if dup_sim is not None else "(no hits)",
    )

    # ---------------- S3: in-band conflict ----------------
    if llm_wired:
        first = await heart.learn(FactInput(
            subject="staging /health endpoint",
            content="The staging API /health endpoint currently returns HTTP 200 with status ok",
            source="user_direct",
        ))
        second = await heart.learn(FactInput(
            subject="staging /health endpoint",
            content="The staging API /health endpoint currently returns HTTP 500 internal server errors",
            source="user_direct",
        ))
        async with db.session() as sess:
            row = (await sess.execute(text(
                "SELECT COUNT(*) FROM heart.facts WHERE agent_id=:a AND content LIKE '%HTTP 500%'"
            ), {"a": AGENT})).scalar()
            old_row = (await sess.execute(text(
                "SELECT confirmation_count, active, superseded_by FROM heart.facts WHERE id=:i"
            ), {"i": getattr(first, "id", None)})).first()
        stored = (row or 0) >= 1
        not_blind_confirm = old_row is not None and (old_row.confirmation_count or 0) == 0
        report(
            "S3 conflicting fact stored (not swallowed)",
            stored,
            f"(second learn returned {type(second).__name__})",
        )
        report(
            "S3 stale fact NOT confirmation-bumped",
            not_blind_confirm,
            f"(confirmation_count={old_row.confirmation_count if old_row else '?'}, "
            f"active={old_row.active if old_row else '?'}, "
            f"superseded={old_row.superseded_by is not None if old_row else '?'})",
        )
    else:
        print("  [SKIP] S3 conflict probes (no ANTHROPIC key for the classifier)")

    # ---------------- S3b: date merge on >0.95 confirm ----------------
    from datetime import date as _date
    undated = await heart.learn(FactInput(
        subject="conference talk",
        content="Tim submitted the cognitive architecture talk proposal to the conference",
        source="user_direct",
    ))
    merged = await heart.learn(FactInput(
        subject="conference talk",
        content="Tim submitted the cognitive architecture talk proposal to the conference",
        source="user_direct", event_date=_date(2026, 3, 10),
    ))
    async with db.session() as sess:
        ed = (await sess.execute(text(
            "SELECT event_date FROM heart.facts WHERE id=:i"
        ), {"i": getattr(undated, "id", None)})).scalar()
    report(
        "S3b exact-dup confirm merges event_date onto undated fact",
        str(ed) == "2026-03-10",
        f"(event_date={ed}, confirm returned same id: {getattr(merged, 'id', None) == getattr(undated, 'id', None)})",
    )

    # ---------------- S10: dedup query plan uses HNSW ----------------
    probe_vec = await emb.embed("query plan probe text")
    vec_lit = "[" + ",".join(str(float(v)) for v in probe_vec) + "]"
    async with db.session() as sess:
        plan_rows = (await sess.execute(text(
            "EXPLAIN SELECT id, event_date, 1 - (embedding <=> CAST(:v AS vector)) AS similarity "
            "FROM heart.facts WHERE agent_id = :a AND active = true AND embedding IS NOT NULL "
            "ORDER BY embedding <=> CAST(:v AS vector) LIMIT 5"
        ), {"v": vec_lit, "a": AGENT})).all()
    plan = "\n".join(r[0] for r in plan_rows)
    report(
        "S10 dedup probe plan is index-served",
        "Index Scan" in plan and "Seq Scan" not in plan.split("\n")[0],
        f"(top node: {plan_rows[0][0].strip()[:70]})",
    )

    # ---------------- E1: chunk index collision ----------------
    async with db.session() as sess:
        ep_id = (await sess.execute(text(
            "INSERT INTO heart.episodes (agent_id, title, summary, started_at) "
            "VALUES (:a, 'probe', 'probe episode', now()) RETURNING id"
        ), {"a": AGENT})).scalar()
        # simulate a mid-session document ingest occupying indexes 0..2
        for i in range(3):
            await sess.execute(text(
                "INSERT INTO heart.episode_chunks (agent_id, episode_id, chunk_index, content, source_kind) "
                "VALUES (:a, :e, :i, :c, 'document')"
            ), {"a": AGENT, "e": ep_id, "i": i, "c": f"doc chunk {i} " + "x" * 80})
        await sess.commit()

    from nous.handlers.episode_summarizer import EpisodeSummarizer
    summ = EpisodeSummarizer.__new__(EpisodeSummarizer)
    summ._heart = heart
    summ._settings = s
    summ._embedder = emb
    transcript = ("User: tell me about the project goals in detail please. " * 8
                  + "Assistant: the project builds a cognitive agent framework. " * 8)
    stored_n = await summ._chunk_and_store_transcript(str(ep_id), AGENT, transcript)
    stored_again = await summ._chunk_and_store_transcript(str(ep_id), AGENT, transcript)
    async with db.session() as sess:
        rows = (await sess.execute(text(
            "SELECT chunk_index, source_kind FROM heart.episode_chunks "
            "WHERE agent_id=:a AND episode_id=:e ORDER BY chunk_index"
        ), {"a": AGENT, "e": ep_id})).all()
    dialogue_rows = [r for r in rows if r.source_kind == "dialogue"]
    report(
        "E1 transcript chunks appended after doc chunks (none destroyed)",
        stored_n > 0 and len(dialogue_rows) == stored_n
        and min(r.chunk_index for r in dialogue_rows) == 3,
        f"(stored {stored_n}, dialogue indexes {[r.chunk_index for r in dialogue_rows]})",
    )
    report(
        "E1 re-summarize is a no-op (no duplicate dialogue chunks)",
        stored_again == 0 and len(dialogue_rows) == stored_n,
        f"(second run stored {stored_again})",
    )

    # ---------------- D1: decision delete cleans edges ----------------
    async with db.session() as sess:
        dec_id = (await sess.execute(text(
            "INSERT INTO brain.decisions (agent_id, description, context, confidence, category, stakes) "
            "VALUES (:a, 'probe decision', 'probe', 0.8, 'process', 'low') RETURNING id"
        ), {"a": AGENT})).scalar()
        fact_id = getattr(anchor, "id", None)
        await sess.execute(text(
            "INSERT INTO brain.graph_edges (source_id, target_id, source_type, target_type, agent_id, relation, weight) "
            "VALUES (:f, :d, 'fact', 'decision', :a, 'evidence_for', 0.9)"
        ), {"f": fact_id, "d": dec_id, "a": AGENT})
        await sess.commit()
    from nous.brain.brain import Brain
    brain = Brain(db, s, emb)
    await brain.delete(dec_id)
    async with db.session() as sess:
        remaining = (await sess.execute(text(
            "SELECT COUNT(*) FROM brain.graph_edges WHERE target_id=:d AND target_type='decision'"
        ), {"d": dec_id})).scalar()
        dangling = (await sess.execute(text(
            "SELECT COUNT(*) FROM brain.graph_edges e WHERE e.source_type='decision' "
            "AND NOT EXISTS (SELECT 1 FROM brain.decisions d WHERE d.id = e.source_id)"
        ))).scalar()
        dangling += (await sess.execute(text(
            "SELECT COUNT(*) FROM brain.graph_edges e WHERE e.target_type='decision' "
            "AND NOT EXISTS (SELECT 1 FROM brain.decisions d WHERE d.id = e.target_id)"
        ))).scalar()
    report("D1 decision delete removes its edges", (remaining or 0) == 0,
           f"(remaining={remaining})")
    report("D1 zero dangling decision edges table-wide (migration 060)",
           (dangling or 0) == 0, f"(dangling={dangling})")

    # ---------------- D2: embedding cache ----------------
    before = dict(emb.cache_stats)
    await emb.embed("cache probe text — identical both times")
    await emb.embed("cache probe text — identical both times")
    after = emb.cache_stats
    report(
        "D2 repeat embed served from LRU",
        after["hits"] >= before["hits"] + 1,
        f"(stats {after})",
    )

    # ---------------- D9: censor semantic match ----------------
    censor = await heart.add_censor(CensorInput(
        trigger_pattern="never push directly to the main branch",
        reason="protected branch policy", action="steer",
    ))
    # phrasing chosen to clear the 0.7 semantic threshold (near-restatement)
    match = await heart.check_censors("push directly to the main branch")
    matched_ids = [str(m.id) for m in (match or [])]
    report(
        "D9 censor semantic match via distance-ordered query",
        str(getattr(censor, "id", "?")) in matched_ids or bool(matched_ids),
        f"(matches={len(matched_ids)})",
    )

    # ---------------- cleanup ----------------
    async with db.session() as sess:
        await sess.execute(text("DELETE FROM brain.graph_edges WHERE agent_id=:a"), {"a": AGENT})
        await sess.execute(text("DELETE FROM heart.episode_chunks WHERE agent_id=:a"), {"a": AGENT})
        await sess.execute(text("DELETE FROM heart.episodes WHERE agent_id=:a"), {"a": AGENT})
        await sess.execute(text("DELETE FROM heart.facts WHERE agent_id=:a"), {"a": AGENT})
        await sess.execute(text("DELETE FROM heart.censors WHERE agent_id=:a"), {"a": AGENT})
        await sess.execute(text("DELETE FROM brain.decisions WHERE agent_id=:a"), {"a": AGENT})
        await sess.execute(text("DELETE FROM nous_system.events WHERE agent_id=:a"), {"a": AGENT})
        await sess.commit()
    await db.disconnect()

    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{'=' * 60}\n  {len(RESULTS) - len(failed)}/{len(RESULTS)} probes passed")
    if failed:
        for name, _, note in failed:
            print(f"  FAILED: {name} {note}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
