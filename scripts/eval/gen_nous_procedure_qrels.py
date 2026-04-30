"""F056 follow-up: generate procedure qrels from the prod snapshot.

Procedures are the least-validated memory type in F051 evals. This script
samples heart.procedures rows and asks Haiku to draft ONE task description
a Nous user might give whose execution should invoke that procedure.

Different prompt strategy than gen_nous_centric_qrels.py: facts/decisions
get question-shaped queries ("what is X?"); procedures get task-shaped
queries ("send an email to X with attachment Y").

Usage:
    NOUS_EVAL_DB_NAME=nous_eval_scratch \
      uv run python scripts/eval/gen_nous_procedure_qrels.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

import asyncpg

from nous.api.anthropic_client import create_client
from nous.config import Settings


_AGENT_ID = "nous-prod-snapshot"
_HAIKU_MODEL = "claude-haiku-4-5-20251001"


_QUERY_GEN_PROMPT = """You are generating retrieval-eval queries for a procedural-memory system.

Procedures are stored skills the agent uses to accomplish tasks. Given the
following procedure metadata, write ONE plausible task instruction a user
might give whose successful execution would invoke this exact procedure.

The instruction should be:
- Task-shaped, not question-shaped (e.g. "send X to Y" not "how do I send?")
- Natural and concise (8-25 words)
- Specific enough that this procedure is the obvious match
- Phrased the way a user would actually ask, with realistic specifics where appropriate

Procedure name: {name}
Domain: {domain}
Description: {description}
Goals: {goals}

Return ONLY the task instruction, no preamble, no quotes, no explanation."""


async def _gen_query(
    llm, name: str, domain: str, description: str, goals_csv: str,
) -> str | None:
    """Ask Haiku to draft a task instruction for this procedure."""
    payload = {
        "model": _HAIKU_MODEL,
        "max_tokens": 100,
        "temperature": 0,
        "system": "",
        "messages": [{
            "role": "user",
            "content": _QUERY_GEN_PROMPT.format(
                name=name[:200],
                domain=domain or "general",
                description=(description or "")[:1500],
                goals=goals_csv[:500] or "(none)",
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
    if not text or len(text) < 8 or len(text) > 250:
        return None
    return text


async def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--n-procedures", type=int, default=50)
    p.add_argument("--seed", type=int, default=42,
                   help="Seed for Postgres random() — controls procedure sampling.")
    p.add_argument("--out", type=Path, default=Path("tests/fixtures/qrels_nous_prod_procedures.jsonl"))
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

    conn = await asyncpg.connect(
        host=args.eval_host, port=args.eval_port,
        user=args.eval_user, password=args.eval_password,
        database=args.eval_db,
    )
    # Honor --seed so two runs with the same CLI parameters sample identical
    # procedures. Postgres setseed takes [-1.0, 1.0].
    pg_seed = (args.seed % 10000) / 10000.0
    await conn.execute("SELECT setseed($1)", pg_seed)
    logger.info("Postgres random() seeded with %.4f (from --seed=%d)",
                pg_seed, args.seed)

    llm = create_client(main_settings)
    await llm.start()

    qrels: list[dict] = []

    def _flush() -> None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("w", encoding="utf-8") as f:
            for q in qrels:
                f.write(json.dumps(q) + "\n")

    try:
        rows = await conn.fetch(
            """
            SELECT id, name, domain, description, goals
            FROM heart.procedures
            WHERE agent_id = $1
              AND active = true
              AND name IS NOT NULL
              AND length(coalesce(description, '')) >= 30
            ORDER BY random()
            LIMIT $2
            """,
            _AGENT_ID, args.n_procedures * 2,
        )
        sample = list(rows)[:args.n_procedures]
        logger.info("Sampled %d procedures; generating queries", len(sample))

        for i, row in enumerate(sample, 1):
            goals_list = list(row["goals"] or [])
            goals_csv = "; ".join(goals_list)
            q = await _gen_query(
                llm,
                name=row["name"],
                domain=row["domain"] or "",
                description=row["description"] or "",
                goals_csv=goals_csv,
            )
            if q is None:
                continue
            qrels.append({
                "query": q,
                "gold_ids": [str(row["id"])],
                "memory_types": ["procedure"],
                "source": "nous_prod_procedures",
                "confidence": "high",
                "reasoning_type": "task_lookup",
                "reviewed_by": None,
                "notes": {
                    "entity_type": "procedure",
                    "name": row["name"],
                    "domain": row["domain"] or "general",
                },
            })
            if i % 10 == 0:
                logger.info("  procedures: %d/%d", i, len(sample))
        _flush()
    finally:
        await llm.close()
        await conn.close()

    print()
    print("=" * 70)
    print(f"PROCEDURE QRELS GENERATED: {len(qrels)} -> {args.out}")
    print("=" * 70)
    print("Sample task instructions:")
    for q in qrels[:5]:
        print(f"  - [{q['notes']['name']}] {q['query']}")
    if len(qrels) > 5:
        print(f"  ... ({len(qrels) - 5} more)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
