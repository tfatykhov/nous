"""Session-level R@5 for LongMemEval — comparable to gbrain SOTA (0.976).

Different metric from our retrieval hit@K: session-level recall measures
whether retrieved items came from the GOLD sessions, not whether the
exact gold fact/episode UUID was hit. This is the metric gbrain reports.

Strategy:
  1. Per question, build {LME session_id → DB episode_id} map by hashing
     each haystack session's transcript and matching against episode_chunks
     (chunk_index=0, raw transcript verbatim).
  2. Look up gold session episode_ids via question.answer_session_ids.
  3. Run pipeline once chunks_on + once chunks_off.
  4. Apply each variant transform.
  5. For each variant's top-K, compute set of source episode_ids (chunks
     source via episode_id; facts via source_episode_id; episodes via id).
  6. R@K = |retrieved_eps ∩ gold_eps| / |gold_eps|
  7. Report per-variant overall + per question type.

Comparable target: gbrain R@5 = 0.976 on LongMemEval_S.
Caveat: gbrain methodology is per-question isolation. Use --mode per_haystack
to get the closest apples-to-apples.

Usage:
    uv run python scripts/eval/eval_session_level_recall.py --mode per_haystack --k 5
    uv run python scripts/eval/eval_session_level_recall.py --mode global --k 5
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import json
import logging
import os
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("session_recall")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("nous.heart.heart").setLevel(logging.WARNING)

QRELS_PATH = Path("tests/fixtures/qrels_longmemeval.jsonl")
LME_CACHE = Path.home() / ".cache" / "nous-eval" / "longmemeval" / "longmemeval_s_cleaned.json"
AGENT_ID = "nous-lme-corpus"
FACT_TARGET_MIN = 0.55
FACT_TARGET_MAX = 0.85
RRF_K_CONST = 60

EVAL = {
    "DB_HOST": "127.0.0.1", "DB_PORT": "5433",
    "DB_USER": "nous", "DB_PASSWORD": "nous_eval", "DB_NAME": "nous_eval_scratch",
    "NOUS_AGENT_ID": AGENT_ID,
    "NOUS_EMBEDDING_MODEL": "text-embedding-3-large",
    "NOUS_EMBEDDING_DIMENSIONS": "1536",
    "NOUS_EPISODE_CHUNKS_ENABLED": "true",
    "NOUS_HEARTBEAT_ENABLED": "false", "NOUS_SUBTASK_ENABLED": "false",
    "NOUS_SCHEDULE_ENABLED": "false", "NOUS_SLEEP_ENABLED": "false",
    "NOUS_EVENT_BUS_ENABLED": "false",
    "NOUS_ACTIONABILITY_BACKFILL_ON_STARTUP": "false",
    "NOUS_TELEGRAM_BOT_TOKEN": "",
}


def _baseline_sort(results: list) -> list:
    return sorted(results, key=lambda r: r.score or 0.0, reverse=True)


def _variant_c(results: list) -> list:
    chunks = [r for r in results if r.type == "chunk"]
    if len(chunks) < 2:
        return _baseline_sort(results)
    cmin = min(c.score for c in chunks)
    cmax = max(c.score for c in chunks)
    span = cmax - cmin if cmax > cmin else 1.0
    out = []
    for r in results:
        if r.type == "chunk":
            new_score = FACT_TARGET_MIN + (r.score - cmin) / span * (
                FACT_TARGET_MAX - FACT_TARGET_MIN
            )
            out.append(replace(r, score=new_score))
        else:
            out.append(r)
    out.sort(key=lambda r: r.score or 0.0, reverse=True)
    return out


def _variant_r(results: list) -> list:
    by_type: dict[str, list] = collections.defaultdict(list)
    for r in results:
        by_type[r.type].append(r)
    for t, lst in by_type.items():
        lst.sort(key=lambda r: r.score or 0.0, reverse=True)
    out = []
    for t, lst in by_type.items():
        for rank, r in enumerate(lst, 1):
            out.append(replace(r, score=1.0 / (RRF_K_CONST + rank)))
    out.sort(key=lambda r: r.score, reverse=True)
    return out


async def _resolve_source_episode(db, r) -> str | None:
    """Get the source episode_id for a result, regardless of type."""
    from sqlalchemy import text
    rid = str(r.id)
    if r.type == "episode":
        return rid
    if r.type == "fact":
        async with db.session() as s:
            v = (await s.execute(
                text("SELECT source_episode_id::text FROM heart.facts "
                     "WHERE id = :i AND agent_id = :a"),
                {"i": rid, "a": AGENT_ID},
            )).scalar()
        return v
    if r.type == "chunk":
        async with db.session() as s:
            v = (await s.execute(
                text("SELECT episode_id::text FROM heart.episode_chunks "
                     "WHERE id = :i AND agent_id = :a"),
                {"i": rid, "a": AGENT_ID},
            )).scalar()
        return v
    return None


async def _build_qid_to_gold_eps(db, qrels: list) -> dict[str, set[str]]:
    """Per question, identify the episode_ids that correspond to gold answer sessions.

    LME source has answer_session_ids (list of LME session ids), and each
    question's haystack_sessions is a parallel array to haystack_session_ids.
    So we find each answer_session_id's index, render that session's
    transcript, match against heart.episode_chunks.content (chunk_index=0)
    to recover episode_id.
    """
    if not LME_CACHE.exists():
        raise SystemExit(f"LME cache missing: {LME_CACHE}")
    from nous_eval.ingest_longmemeval import _session_to_transcript
    from sqlalchemy import text

    lme = json.loads(LME_CACHE.read_text(encoding="utf-8"))
    lme_by_qid = {q["question_id"]: q for q in lme}

    gold: dict[str, set[str]] = {}
    async with db.session() as s:
        for qrel in qrels:
            qid = qrel["notes"]["question_id"]
            answer_sids = qrel["notes"].get("answer_session_ids", [])
            lme_q = lme_by_qid.get(qid)
            if not lme_q or not answer_sids:
                gold[qid] = set()
                continue
            haystack_sids = lme_q.get("haystack_session_ids", [])
            haystack_sessions = lme_q.get("haystack_sessions", [])
            # Index sessions by sid
            sid_to_session = {
                haystack_sids[i]: haystack_sessions[i]
                for i in range(min(len(haystack_sids), len(haystack_sessions)))
            }
            ep_ids: set[str] = set()
            for sid in answer_sids:
                sess = sid_to_session.get(sid)
                if not sess:
                    continue
                tx = _session_to_transcript(sess)
                if not tx or len(tx) < 50:
                    continue
                prefix = tx[:100]
                row = (await s.execute(
                    text("SELECT episode_id::text FROM heart.episode_chunks "
                         "WHERE agent_id = :a AND chunk_index = 0 "
                         "AND LEFT(content, 100) = :p LIMIT 1"),
                    {"a": AGENT_ID, "p": prefix},
                )).scalar()
                if row:
                    ep_ids.add(row)
            gold[qid] = ep_ids
    return gold


async def _build_qid_to_haystack_eps(db, qrels: list) -> dict[str, set[str]]:
    """For per_haystack mode: per question, all episodes from that question's haystack."""
    if not LME_CACHE.exists():
        return {}
    from nous_eval.ingest_longmemeval import _session_to_transcript
    from sqlalchemy import text

    lme = json.loads(LME_CACHE.read_text(encoding="utf-8"))
    lme_by_qid = {q["question_id"]: q for q in lme}

    scope: dict[str, set[str]] = {}
    async with db.session() as s:
        for qrel in qrels:
            qid = qrel["notes"]["question_id"]
            lme_q = lme_by_qid.get(qid)
            if not lme_q:
                scope[qid] = set()
                continue
            ep_ids: set[str] = set()
            for sess in lme_q.get("haystack_sessions", []):
                tx = _session_to_transcript(sess)
                if not tx or len(tx) < 50:
                    continue
                prefix = tx[:100]
                row = (await s.execute(
                    text("SELECT episode_id::text FROM heart.episode_chunks "
                         "WHERE agent_id = :a AND chunk_index = 0 "
                         "AND LEFT(content, 100) = :p LIMIT 1"),
                    {"a": AGENT_ID, "p": prefix},
                )).scalar()
                if row:
                    ep_ids.add(row)
            scope[qid] = ep_ids
    return scope


async def _restrict_to_scope(results: list, db, scope_ep_ids: set[str]) -> list:
    if not scope_ep_ids:
        return results
    keep: list = []
    for r in results:
        src = await _resolve_source_episode(db, r)
        if src and src in scope_ep_ids:
            keep.append(r)
    return keep


async def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["global", "per_haystack"], default="per_haystack")
    parser.add_argument("--k", type=int, default=5, help="Top-K for R@K (default 5 = gbrain comparison)")
    args = parser.parse_args()
    for k, v in EVAL.items():
        os.environ.setdefault(k, v)

    from nous.api.retrieval_pipeline import run_recall_pipeline
    from nous.brain.brain import Brain
    from nous.brain.embeddings import EmbeddingProvider
    from nous.config import Settings
    from nous.heart.heart import Heart
    from nous.storage.database import Database

    settings = Settings()
    db = Database(settings)
    await db.connect()
    embedder = EmbeddingProvider(
        api_key=settings.openai_api_key,
        model=settings.embedding_model,
        dimensions=settings.embedding_dimensions,
    )
    heart = Heart(database=db, settings=settings, embedding_provider=embedder)
    brain = Brain(db, settings, embedder)

    settings_off = settings.model_copy(update={"episode_chunks_enabled": False})
    heart_off = Heart(database=db, settings=settings_off, embedding_provider=embedder)
    brain_off = Brain(db, settings_off, embedder)

    # Prod-parity wiring: main.py constructs an AnthropicClient and wires
    # F050 QueryExpander into Heart when ``query_expansion_enabled=true``.
    # The eval harness was missing this — Heart.recall took the no-expander
    # path even when prod had F050 active, so the LME baseline never
    # reflected production retrieval. Construct the same wiring here.
    #
    # F055 ResidualActivator is also wired in main.py but stateless eval
    # passes no ConversationState across turns, so the activator would
    # never fire even if wired. Skip for now; revisit if multi-turn evals
    # land. F023/F027/F047 are write-path wirings, irrelevant for retrieval.
    api_client = None
    if settings.query_expansion_enabled:
        from nous.api.anthropic_client import create_client
        from nous.heart.query_expansion import QueryExpander

        api_client = create_client(settings)
        await api_client.start()
        expander = QueryExpander(
            llm=api_client,
            settings=settings,
            db=db,
            model=settings.query_expansion_model,
        )
        heart.set_query_expander(expander)
        heart_off.set_query_expander(expander)
        print(
            f"F050: QueryExpander wired (model={settings.query_expansion_model}, "
            f"max_variants={settings.query_expansion_max_variants})"
        )
    else:
        print("F050: NOUS_QUERY_EXPANSION_ENABLED=false — eval runs without expansion")

    qrels = [json.loads(l) for l in open(QRELS_PATH, encoding="utf-8")]
    print(f"Loaded {len(qrels)} qrels   mode={args.mode}   K={args.k}")

    print("Building gold-session episode map...")
    gold_map = await _build_qid_to_gold_eps(db, qrels)
    n_gold = sum(1 for v in gold_map.values() if v)
    avg_gold = sum(len(v) for v in gold_map.values()) / max(n_gold, 1)
    print(f"  Resolved gold sessions for {n_gold}/{len(qrels)} questions, "
          f"avg {avg_gold:.1f} gold eps/question")

    haystack_scope_map: dict[str, set[str]] = {}
    if args.mode == "per_haystack":
        print("Building haystack-scope episode map...")
        haystack_scope_map = await _build_qid_to_haystack_eps(db, qrels)
        n_scope = sum(1 for v in haystack_scope_map.values() if v)
        avg_scope = sum(len(v) for v in haystack_scope_map.values()) / max(n_scope, 1)
        print(f"  Resolved scope for {n_scope}/{len(qrels)} questions, "
              f"avg {avg_scope:.1f} eps/scope")

    variants = ["chunks_off", "baseline", "C", "R"]
    recall_records: dict[str, dict] = {
        v: collections.defaultdict(list) for v in variants
    }
    hit_records: dict[str, dict] = {
        v: collections.defaultdict(list) for v in variants
    }
    mrr_records: dict[str, dict] = {
        v: collections.defaultdict(list) for v in variants
    }
    overall_recall: dict[str, list] = {v: [] for v in variants}
    overall_hit: dict[str, list] = {v: [] for v in variants}
    overall_mrr: dict[str, list] = {v: [] for v in variants}

    # Per BLOCKERS #2+#3 fix: pull a wider candidate pool, dedup to unique
    # sessions IN RANK ORDER, then take the top-K unique sessions.
    PIPELINE_LIMIT = 50

    try:
        for i, q in enumerate(qrels):
            query = q["query"]
            qtype = q["notes"]["question_type"]
            qid = q["notes"]["question_id"]
            gold_eps = gold_map.get(qid, set())
            if not gold_eps:
                continue
            scope_eps = haystack_scope_map.get(qid, set())

            try:
                raw_on, _ = await run_recall_pipeline(
                    query=query, heart=heart, brain=brain, settings=settings,
                    limit=PIPELINE_LIMIT, rerank_by_score=True,
                )
                # Codex round-1 P2 (PR #454): both variants must run with the
                # same ranking strategy for the chunks-on-vs-off comparison
                # to be apples-to-apples. Pre-fix, chunks_off used stage
                # order (rerank_by_score=False) while baseline used score
                # order, so the comparison conflated two variables. Now both
                # use rerank_by_score=True.
                raw_off, _ = await run_recall_pipeline(
                    query=query, heart=heart_off, brain=brain_off,
                    settings=settings_off, limit=PIPELINE_LIMIT,
                    rerank_by_score=True,
                )
            except Exception as e:
                logger.exception("recall failed for qid=%s: %s", qid, e)
                continue

            if args.mode == "per_haystack" and scope_eps:
                raw_on = await _restrict_to_scope(raw_on, db, scope_eps)
                raw_off = await _restrict_to_scope(raw_off, db, scope_eps)

            variant_ranked = {
                "chunks_off": raw_off,
                "baseline": _baseline_sort(raw_on),
                "C": _variant_c(raw_on),
                "R": _variant_r(raw_on),
            }

            for vname, ranked in variant_ranked.items():
                # Walk ranked items, resolve each to source episode, dedup
                # preserving rank order, take first K unique sessions
                seen: set[str] = set()
                top_unique_sessions: list[str] = []
                for r in ranked:
                    src = await _resolve_source_episode(db, r)
                    if src and src not in seen:
                        seen.add(src)
                        top_unique_sessions.append(src)
                        if len(top_unique_sessions) >= args.k:
                            break
                retrieved_set = set(top_unique_sessions)
                hit_eps = retrieved_set & gold_eps

                # recall@K: fraction of gold sessions in top-K unique
                recall = len(hit_eps) / len(gold_eps)
                # hit@K: binary, any gold session in top-K unique (gbrain metric)
                hit = 1.0 if hit_eps else 0.0
                # MRR@K: 1/rank of first gold session (0 if no gold in top-K)
                mrr = 0.0
                for pos, sess in enumerate(top_unique_sessions, 1):
                    if sess in gold_eps:
                        mrr = 1.0 / pos
                        break

                recall_records[vname][qtype].append(recall)
                hit_records[vname][qtype].append(hit)
                mrr_records[vname][qtype].append(mrr)
                overall_recall[vname].append(recall)
                overall_hit[vname].append(hit)
                overall_mrr[vname].append(mrr)

            if (i + 1) % 10 == 0:
                logger.info("Progress %d/%d", i + 1, len(qrels))
    finally:
        await embedder.close()
        if api_client is not None:
            try:
                await api_client.close()
            except Exception:
                pass
        await db.disconnect()

    print()
    print("=" * 90)
    print(f"Session-level metrics @ K={args.k}")
    print(f"mode={args.mode}   pipeline_limit={PIPELINE_LIMIT}   dedup-then-take-K-unique-sessions")
    n_q = len(overall_recall["baseline"])
    print(f"n_questions = {n_q}")
    print("=" * 90)
    print(f"\n{'VARIANT':<24} {'hit@K':>8} {'recall@K':>10} {'MRR@K':>8} {'n':>5}")
    print("-" * 63)
    for v in variants:
        if not overall_hit[v]:
            continue
        h = mean(overall_hit[v])
        r = mean(overall_recall[v])
        m = mean(overall_mrr[v])
        print(f"{v:<24} {h:>8.3f} {r:>10.3f} {m:>8.3f} {len(overall_hit[v]):>5}")

    print(f"\n{'PER QUESTION TYPE — hit@K (gbrain-comparable)':<24}")
    qtypes = sorted({q["notes"]["question_type"] for q in qrels})
    print(f"  {'type':<32} {'variant':<12} {'hit@K':>6}   n")
    print("  " + "-" * 60)
    for qt in qtypes:
        for v in variants:
            hits = hit_records[v][qt]
            if not hits:
                continue
            print(f"  {qt:<32} {v:<12} {mean(hits):>6.3f}  {len(hits)}")

    print(f"\n{'PER QUESTION TYPE — recall@K (our stricter metric)':<24}")
    print(f"  {'type':<32} {'variant':<12} {'recall@K':>8}   n")
    print("  " + "-" * 60)
    for qt in qtypes:
        for v in variants:
            recs = recall_records[v][qt]
            if not recs:
                continue
            print(f"  {qt:<32} {v:<12} {mean(recs):>8.3f}  {len(recs)}")

    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%S")
    out_dir = Path("reports")
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"{ts}_lme_session_recall_{args.mode}_k{args.k}.json"
    out.write_text(json.dumps({
        "timestamp": ts, "k": args.k, "mode": args.mode,
        "pipeline_limit": PIPELINE_LIMIT,
        "n_questions_with_gold": n_gold,
        "agent_id": AGENT_ID,
        "overall_hit": {v: mean(overall_hit[v]) if overall_hit[v] else 0.0 for v in variants},
        "overall_recall": {v: mean(overall_recall[v]) if overall_recall[v] else 0.0 for v in variants},
        "overall_mrr": {v: mean(overall_mrr[v]) if overall_mrr[v] else 0.0 for v in variants},
        "per_type_hit": {
            v: {qt: mean(hits) for qt, hits in hit_records[v].items()}
            for v in variants
        },
        "per_type_recall": {
            v: {qt: mean(recs) for qt, recs in recall_records[v].items()}
            for v in variants
        },
        "per_type_mrr": {
            v: {qt: mean(mrrs) for qt, mrrs in mrr_records[v].items()}
            for v in variants
        },
    }, indent=2), encoding="utf-8")
    print(f"\n  Report: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
