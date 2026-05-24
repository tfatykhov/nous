"""F067 prod-shape A/B: chunks ON vs chunks OFF on real mined prod queries.

Uses ``tests/fixtures/qrels_prod_mined.jsonl`` (questions extracted from real
prod conversations — no human-labeled gold) and LLM-as-judge pairwise
preference to decide which retrieval config produces more useful memory
snippets per query.

For each query:
1. Retrieve top-K with chunks OFF (existing facts + episodes only)
2. Retrieve top-K with chunks ON (hybrid: facts/episodes + chunk-vector leg)
3. Format both sets as text snippets
4. Counterbalance A/B labels (random swap per query — avoids position bias)
5. Sonnet judge picks "A wins" / "B wins" / "tie"
6. Aggregate: win rate for chunks-on

Methodology:
- LLM-as-judge: prompt explains the task is comparing retrieval quality
- Counterbalancing: random A/B swap per query, recovered at scoring time
- N=30 queries, 1 judge call each → ~$0.15-0.30, ~3 min

Usage:
    uv run python scripts/eval/eval_prod_chunks_ab.py
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import json
import logging
import os
import random
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("ab")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("nous.heart.heart").setLevel(logging.WARNING)

QRELS_PATH = Path("tests/fixtures/qrels_prod_mined.jsonl")
DEFAULT_AGENT = "nous-prod-2026-05-24"
TOP_K = 10

JUDGE_PROMPT = """You are evaluating which set of memory snippets is more useful for answering a user's question.

User question: {query}

--- Set A ---
{memory_a}

--- Set B ---
{memory_b}

Which set is more useful for answering the question? Consider:
- Relevance to the specific question (does it mention the topic/entities asked about)
- Specificity (concrete facts beat vague summaries)
- Coverage (does it provide enough to actually answer)

Reply with EXACTLY one of: "A", "B", or "TIE".
"""


def _extract_text(res) -> str:
    return "".join(b.get("text", "") for b in res.content if b.get("type") == "text").strip()


async def _judge(client, rate_limiter, model, query, memory_a, memory_b):
    from nous_eval._oat_preamble import call_with_retries, with_oat_preamble
    res = await call_with_retries(client, {
        "model": model,
        "system": with_oat_preamble("You are an impartial evaluator. Reply with exactly one letter."),
        "messages": [{"role": "user", "content": JUDGE_PROMPT.format(
            query=query, memory_a=memory_a, memory_b=memory_b,
        )}],
        "max_tokens": 10, "temperature": 0.0,
    }, rate_limiter=rate_limiter)
    txt = _extract_text(res).upper().strip().strip('"').strip("'")
    if txt.startswith("A"):
        return "A"
    if txt.startswith("B"):
        return "B"
    return "TIE"


async def _retrieve_and_format(
    heart, brain, settings, query, top_k, label
) -> str:
    """Run recall_deep pipeline + format results as compact memory snippets."""
    from nous.api.retrieval_pipeline import run_recall_pipeline
    from sqlalchemy import text as sa_text
    try:
        # Mirror PR #443 prod gating exactly so the A/B reflects what
        # production actually does, not an isolated "add chunks only"
        # variable. chunks ON → score-rerank fires (chunks compete with
        # facts for top-K). chunks OFF → legacy stage-order (facts
        # dominate top-K). This couples the two changes that ship
        # together in F067.
        rerank = getattr(settings, "episode_chunks_enabled", False)
        results, _stats = await run_recall_pipeline(
            query=query, heart=heart, brain=brain, settings=settings,
            limit=top_k, rerank_by_score=rerank,
        )
    except Exception:
        logger.exception("[%s] recall failed for %r", label, query[:60])
        return "(retrieval failed)"

    if not results:
        return "(no results)"

    # Fetch content per result
    lines: list[str] = []
    async with heart.db.session() as s:
        for r in results[:top_k]:
            rid = str(r.id)
            if r.type == "fact":
                content = (await s.execute(sa_text(
                    "SELECT content FROM heart.facts WHERE id = :i AND agent_id = :a"
                ), {"i": rid, "a": heart.agent_id})).scalar() or ""
            elif r.type == "episode":
                content = (await s.execute(sa_text(
                    "SELECT COALESCE(structured_summary->>'summary', summary) "
                    "FROM heart.episodes WHERE id = :i AND agent_id = :a"
                ), {"i": rid, "a": heart.agent_id})).scalar() or ""
            elif r.type == "chunk":
                content = (await s.execute(sa_text(
                    "SELECT content FROM heart.episode_chunks WHERE id = :i AND agent_id = :a"
                ), {"i": rid, "a": heart.agent_id})).scalar() or ""
            elif r.type == "decision":
                content = r.description or ""
            else:
                content = r.description or ""
            content = str(content)[:300]
            lines.append(f"- ({r.type}) {content}")
    return "\n".join(lines)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent-id", default=DEFAULT_AGENT)
    parser.add_argument("--top-k", type=int, default=TOP_K)
    parser.add_argument("--judge-model", default="claude-sonnet-4-6")
    parser.add_argument("--seed", type=int, default=0, help="A/B swap seed")
    parser.add_argument("--out-dir", type=Path, default=Path("reports"))
    args = parser.parse_args()

    # Eval DB pointing
    for k, v in {
        "DB_HOST": "127.0.0.1", "DB_PORT": "5433", "DB_NAME": "nous_eval_scratch",
        "DB_USER": "nous", "DB_PASSWORD": "nous_eval",
        "NOUS_EMBEDDING_MODEL": "text-embedding-3-large",
        "NOUS_EMBEDDING_DIMENSIONS": "1536",
        "NOUS_HEARTBEAT_ENABLED": "false",
        "NOUS_SUBTASK_ENABLED": "false",
        "NOUS_SCHEDULE_ENABLED": "false",
        "NOUS_SLEEP_ENABLED": "false",
        "NOUS_EVENT_BUS_ENABLED": "false",
        "NOUS_ACTIONABILITY_BACKFILL_ON_STARTUP": "false",
        "NOUS_TELEGRAM_BOT_TOKEN": "",
    }.items():
        os.environ.setdefault(k, v)

    from nous.api.anthropic_client import create_client
    from nous.brain.brain import Brain
    from nous.brain.embeddings import EmbeddingProvider
    from nous.config import Settings
    from nous.heart.heart import Heart
    from nous.storage.database import Database
    from nous_eval._oat_preamble import RateLimiter

    qrels = [json.loads(l) for l in open(QRELS_PATH, encoding="utf-8")]
    print(f"Loaded {len(qrels)} prod-mined queries")

    settings = Settings()
    iso_settings = settings.model_copy(update={"agent_id": args.agent_id})

    db = Database(iso_settings)
    await db.connect()
    embedder = EmbeddingProvider(
        api_key=iso_settings.openai_api_key,
        model=iso_settings.embedding_model,
        dimensions=iso_settings.embedding_dimensions,
    )
    api_client = create_client(iso_settings)
    await api_client.start()
    rate_limiter = RateLimiter(min_interval_s=2.0)

    # Two configurations: chunks OFF and chunks ON
    settings_off = iso_settings.model_copy(update={"episode_chunks_enabled": False})
    settings_on = iso_settings.model_copy(update={"episode_chunks_enabled": True})
    heart_off = Heart(database=db, settings=settings_off, embedding_provider=embedder)
    heart_on = Heart(database=db, settings=settings_on, embedding_provider=embedder)
    brain_off = Brain(db, settings_off, embedder)
    brain_on = Brain(db, settings_on, embedder)

    rng = random.Random(args.seed)
    results = []
    try:
        for i, q in enumerate(qrels):
            query = q["query"]
            mem_off = await _retrieve_and_format(
                heart_off, brain_off, settings_off, query, args.top_k, "off")
            mem_on = await _retrieve_and_format(
                heart_on, brain_on, settings_on, query, args.top_k, "on")

            # Counterbalance: random swap so judge doesn't always see chunks=on as A
            swap = rng.random() < 0.5
            if swap:
                set_a, set_b = mem_on, mem_off
                a_label = "chunks_on"
                b_label = "chunks_off"
            else:
                set_a, set_b = mem_off, mem_on
                a_label = "chunks_off"
                b_label = "chunks_on"

            verdict_raw = await _judge(
                api_client, rate_limiter, args.judge_model,
                query, set_a, set_b,
            )
            # Translate verdict to chunks_on / chunks_off / tie
            if verdict_raw == "A":
                winner = a_label
            elif verdict_raw == "B":
                winner = b_label
            else:
                winner = "tie"

            results.append({
                "query": query,
                "swap": swap,
                "judge_verdict": verdict_raw,
                "winner": winner,
                "mem_off_chars": len(mem_off),
                "mem_on_chars": len(mem_on),
                "mem_off_snippet": mem_off[:400],
                "mem_on_snippet": mem_on[:400],
            })
            if (i + 1) % 5 == 0 or i + 1 == len(qrels):
                tally = collections.Counter(r["winner"] for r in results)
                logger.info("Progress %d/%d  tally=%s",
                            i + 1, len(qrels), dict(tally))
    finally:
        await api_client.close()
        await embedder.close()
        await db.disconnect()

    # Aggregate
    tally = collections.Counter(r["winner"] for r in results)
    n = len(results)
    on_wins = tally.get("chunks_on", 0)
    off_wins = tally.get("chunks_off", 0)
    ties = tally.get("tie", 0)

    print()
    print("=" * 60)
    print(f"F067 chunks A/B on real prod queries (n={n})")
    print("=" * 60)
    print(f"  chunks ON  wins:  {on_wins:3d}  ({on_wins/n:.1%})")
    print(f"  chunks OFF wins:  {off_wins:3d}  ({off_wins/n:.1%})")
    print(f"  tie:              {ties:3d}  ({ties/n:.1%})")
    print()
    decisive = on_wins + off_wins
    if decisive > 0:
        on_share = on_wins / decisive
        print(f"  Decisive verdicts: chunks_on win share = {on_share:.1%}")
    print()
    # Save raw
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%S")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    out = args.out_dir / f"{ts}_prod_chunks_ab.json"
    out.write_text(json.dumps({
        "timestamp": ts, "n": n, "agent_id": args.agent_id,
        "top_k": args.top_k, "judge_model": args.judge_model,
        "tally": dict(tally),
        "results": results,
    }, indent=2), encoding="utf-8")
    print(f"  Report: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
