"""F056 follow-up: generate Nous-centric qrels from prod snapshot.

For each sampled fact/decision/episode in `nous-prod-snapshot`, asks Haiku
to write 1-2 plausible questions a Nous user might ask whose answer
includes that row. Output is a qrels JSONL compatible with F051.4.

Sample size is configurable; default 40 spread across types.

Usage:
    NOUS_EVAL_DB_NAME=nous_eval_scratch \
      uv run python scripts/eval/gen_nous_centric_qrels.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import sys
from pathlib import Path

import asyncpg

from nous.api.anthropic_client import create_client
from nous.config import Settings


_AGENT_ID = "nous-prod-snapshot"
_HAIKU_MODEL = "claude-haiku-4-5-20251001"


_QUERY_GEN_PROMPT = """You are generating retrieval-eval queries for a memory system.

Given the following stored memory entry, write ONE plausible question a user
might ask whose answer requires this entry. The question should be:
- Natural and concise (8-20 words)
- Specific enough to identify this entry over generic memory
- Phrased the way someone would actually ask, not a database query

Entry type: {entity_type}
Entry content: {content}

Return ONLY the question text, no preamble, no quotes, no explanation."""


async def _gen_question(
    llm, content: str, entity_type: str,
) -> str | None:
    """Ask Haiku to draft a question. Returns None on failure."""
    payload = {
        "model": _HAIKU_MODEL,
        "max_tokens": 80,
        "temperature": 0,
        "system": "",
        "messages": [{
            "role": "user",
            "content": _QUERY_GEN_PROMPT.format(
                entity_type=entity_type,
                content=content[:1500],
            ),
        }],
    }
    try:
        response = await llm.call(payload)
    except Exception:
        logging.exception("query gen failed")
        return None
    text = ""
    for block in response.content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text", "").strip()
            break
    text = text.strip("\"'")
    if not text or len(text) < 8 or len(text) > 200:
        return None
    return text


async def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--n-facts", type=int, default=25)
    p.add_argument("--n-decisions", type=int, default=10)
    p.add_argument("--n-episodes", type=int, default=5)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--out", type=Path, default=Path("tests/fixtures/qrels_nous_prod.jsonl"))
    p.add_argument("--eval-host", default="127.0.0.1")
    p.add_argument("--eval-port", type=int, default=5433)
    p.add_argument("--eval-user", default="nous")
    p.add_argument("--eval-password", default="nous_eval")
    p.add_argument("--eval-db", default="nous_eval_scratch")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger = logging.getLogger(__name__)

    main_settings = Settings()
    if not (main_settings.anthropic_api_key or main_settings.anthropic_auth_token):
        print("ERROR: ANTHROPIC creds required.", file=sys.stderr)
        return 2

    rng = random.Random(args.seed)

    conn = await asyncpg.connect(
        host=args.eval_host, port=args.eval_port,
        user=args.eval_user, password=args.eval_password,
        database=args.eval_db,
    )
    # Honor --seed so two runs with the same CLI parameters sample identical
    # rows. Postgres setseed takes [-1.0, 1.0]; modulo + scale keeps it
    # deterministic and in range for any positive int seed. Same value covers
    # all three ORDER BY random() queries below since setseed is session-level.
    pg_seed = (args.seed % 10000) / 10000.0
    await conn.execute("SELECT setseed($1)", pg_seed)
    logger.info("Postgres random() seeded with %.4f (from --seed=%d)",
                pg_seed, args.seed)

    llm = create_client(main_settings)
    await llm.start()

    qrels: list[dict] = []

    def _flush() -> None:
        """Write current qrels to disk so a downstream crash does not lose work."""
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("w", encoding="utf-8") as f:
            for q in qrels:
                f.write(json.dumps(q) + "\n")

    try:
        # Sample facts
        fact_rows = await conn.fetch(
            """
            SELECT id, content, subject, category
            FROM heart.facts
            WHERE agent_id = $1
              AND active = true
              AND length(content) >= 80
              AND length(content) <= 800
            ORDER BY random()
            LIMIT $2
            """,
            _AGENT_ID, args.n_facts * 2,  # over-sample for filter losses
        )
        # Truncate to target N
        fact_sample = list(fact_rows)[:args.n_facts]
        logger.info("Sampled %d facts; generating questions", len(fact_sample))
        for i, row in enumerate(fact_sample, 1):
            q = await _gen_question(llm, row["content"], "fact")
            if q is None:
                continue
            qrels.append({
                "query": q,
                "gold_ids": [str(row["id"])],
                "memory_types": ["fact"],
                "source": "nous_prod_snapshot",
                "confidence": "high",
                "reasoning_type": "specific_lookup",
                "reviewed_by": None,
                "notes": {
                    "entity_type": "fact",
                    "subject": row["subject"],
                    "category": row["category"],
                },
            })
            if i % 5 == 0:
                logger.info("  facts: %d/%d", i, len(fact_sample))
        _flush()
        logger.info("Flushed %d fact qrels to %s", len(qrels), args.out)

        # Sample decisions
        decision_rows = await conn.fetch(
            """
            SELECT id, description, context, pattern
            FROM brain.decisions
            WHERE agent_id = $1
              AND length(description) >= 80
            ORDER BY random()
            LIMIT $2
            """,
            _AGENT_ID, args.n_decisions * 2,
        )
        decision_sample = list(decision_rows)[:args.n_decisions]
        logger.info("Sampled %d decisions; generating questions", len(decision_sample))
        for i, row in enumerate(decision_sample, 1):
            content_str = row["description"] or row["context"] or row["pattern"] or ""
            if len(content_str) < 80:
                continue
            q = await _gen_question(llm, content_str, "decision")
            if q is None:
                continue
            qrels.append({
                "query": q,
                "gold_ids": [str(row["id"])],
                "memory_types": ["decision"],
                "source": "nous_prod_snapshot",
                "confidence": "high",
                "reasoning_type": "specific_lookup",
                "reviewed_by": None,
                "notes": {"entity_type": "decision"},
            })
        _flush()
        logger.info("Flushed %d total qrels (incl. decisions)", len(qrels))

        # Sample episodes
        episode_rows = await conn.fetch(
            """
            SELECT id, summary, structured_summary
            FROM heart.episodes
            WHERE agent_id = $1
              AND structured_summary IS NOT NULL
              AND length(summary) >= 80
            ORDER BY random()
            LIMIT $2
            """,
            _AGENT_ID, args.n_episodes * 2,
        )
        episode_sample = list(episode_rows)[:args.n_episodes]
        logger.info("Sampled %d episodes; generating questions", len(episode_sample))
        for i, row in enumerate(episode_sample, 1):
            content = row["summary"] or ""
            if len(content) < 80:
                continue
            q = await _gen_question(llm, content, "episode")
            if q is None:
                continue
            qrels.append({
                "query": q,
                "gold_ids": [str(row["id"])],
                "memory_types": ["episode"],
                "source": "nous_prod_snapshot",
                "confidence": "high",
                "reasoning_type": "specific_lookup",
                "reviewed_by": None,
                "notes": {"entity_type": "episode"},
            })
    finally:
        await llm.close()
        await conn.close()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as f:
        for q in qrels:
            f.write(json.dumps(q) + "\n")

    print()
    print("=" * 70)
    print(f"QRELS GENERATED: {len(qrels)} -> {args.out}")
    print("=" * 70)
    by_type = {}
    for q in qrels:
        t = q["memory_types"][0]
        by_type[t] = by_type.get(t, 0) + 1
    for t, n in sorted(by_type.items()):
        print(f"  {t}: {n}")
    print("=" * 70)
    print()
    print("Sample questions:")
    for q in qrels[:5]:
        print(f"  - {q['query']}")
    print(f"  ... ({len(qrels) - 5} more)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
