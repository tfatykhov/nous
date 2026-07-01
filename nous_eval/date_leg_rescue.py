"""F075 L3 Task 7: rescue-metric dev harness for the date-window retrieval leg.

Samples dated facts from the eval DB, generates time-framed Haiku queries,
runs vanilla vs vanilla+leg recall, and reports rescued / lost counts.

Usage::

    python -m nous_eval.date_leg_rescue [--sample N] [--top-k K]

This is a developer instrument — NOT shipped in the prod image. No unit tests
are required for ``main()``.  The only unit-tested surface is ``rrf_fuse``.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import logging
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pure function (unit-tested)
# ---------------------------------------------------------------------------


def rrf_fuse(vanilla_ids: list, leg_ids: list, k: int = 60) -> list:
    """Position-based RRF fusion of two ranked id lists.

    Each id contributes 1/(k + rank) from each list it appears in.  Ids are
    merged and re-sorted by descending total score.  Equal ids are deduplicated
    (higher contribution wins via accumulation).

    Args:
        vanilla_ids: Ordered ids from the main retrieval pipeline.
        leg_ids:     Ordered ids from the date-window leg.
        k:           RRF smoothing constant (default 60, validated).

    Returns:
        Fused list of ids, best-first.
    """
    score: dict[Any, float] = {}
    for rank, i in enumerate(vanilla_ids, 1):
        score[i] = score.get(i, 0.0) + 1.0 / (k + rank)
    for rank, i in enumerate(leg_ids, 1):
        score[i] = score.get(i, 0.0) + 1.0 / (k + rank)
    return [i for i, _ in sorted(score.items(), key=lambda kv: kv[1], reverse=True)]


# ---------------------------------------------------------------------------
# Dev harness (not unit-tested — runtime-only)
# ---------------------------------------------------------------------------


async def _sample_dated_facts(
    db: Any, agent_id: str, limit: int
) -> list[dict]:
    """Sample active facts with event_date from the eval DB."""
    from sqlalchemy import text

    sql = text("""
        SELECT id, content, event_date
        FROM heart.facts
        WHERE agent_id = :agent_id
          AND active = true
          AND event_date IS NOT NULL
          AND content IS NOT NULL
          AND length(content) > 40
        ORDER BY random()
        LIMIT :limit
    """)
    async with db.session() as session:
        result = await session.execute(sql, {"agent_id": agent_id, "limit": limit})
        return [
            {"id": row.id, "content": row.content, "event_date": row.event_date}
            for row in result.all()
        ]


async def _generate_temporal_query(
    client: Any, model: str, content: str, event_date: datetime.date
) -> str:
    """Ask Haiku to write a time-framed question about a fact."""
    from nous.api.anthropic_client import create_client  # noqa: F401 (type hint only)

    prompt = (
        f"The following fact was recorded on {event_date.isoformat()}:\n\n"
        f"{content}\n\n"
        "Write a short question (one sentence) that a user would ask when trying "
        "to recall this fact. The question MUST mention a specific date, month, "
        "or time period so it has a clear temporal anchor. Do not use the word "
        "'fact'. Reply with only the question, no preamble."
    )
    payload = {
        "model": model,
        "max_tokens": 128,
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        response = await asyncio.wait_for(client.call(payload), timeout=10.0)
        for block in response.content:
            if isinstance(block, dict) and block.get("type") == "text":
                return block["text"].strip()
            # SDK-style objects
            if hasattr(block, "type") and block.type == "text":
                return block.text.strip()
    except Exception:
        logger.warning("query generation failed for content %r", content[:40], exc_info=True)
    # Fallback: construct a simple query from content + date
    return f"What happened around {event_date.strftime('%B %Y')}? ({content[:60]})"


async def main() -> None:
    """CLI entry point for the date-leg rescue metric harness."""
    parser = argparse.ArgumentParser(description="Date-leg rescue-metric harness (F075 L3)")
    parser.add_argument("--sample", type=int, default=20, help="Dated facts to sample")
    parser.add_argument("--top-k", type=int, default=10, help="Retrieval top-K for comparison")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)

    # ── Build settings ──────────────────────────────────────────────────────
    from nous.config import Settings
    from nous.brain.embeddings import EmbeddingProvider
    from nous.storage.database import Database
    from nous_eval.config import EvalSettings
    from nous_eval.retrieval_runner import _settings_for_eval_db, _build_heart_for_eval
    from nous.api.retrieval_pipeline import run_recall_pipeline
    from nous.heart.date_window import DateWindowParser
    from nous.brain.brain import Brain

    eval_settings = EvalSettings()
    base_settings = Settings()
    settings = _settings_for_eval_db(eval_settings, base_settings)

    # Enable the date leg for the fused run
    fused_settings = settings.model_copy(update={"date_leg_enabled": True})

    # ── Connect to eval DB ──────────────────────────────────────────────────
    db = Database(settings=settings)
    await db.connect()

    embedding_provider: EmbeddingProvider | None = None
    if settings.openai_api_key:
        embedding_provider = EmbeddingProvider(
            api_key=settings.openai_api_key,
            model=settings.embedding_model,
            dimensions=settings.embedding_dimensions,
        )

    brain = Brain(database=db, settings=settings, embedding_provider=embedding_provider)

    # ── LLM client for query generation + date-window parsing ──────────────
    from nous.api.anthropic_client import create_client
    api_client = create_client(settings)
    await api_client.start()

    try:
        async with _build_heart_for_eval(db, settings) as heart:
            # Wire the DateWindowParser so the fused pipeline can call it
            heart.date_window_parser = DateWindowParser(api_client, fused_settings)

            # ── Sample dated facts ──────────────────────────────────────────
            facts = await _sample_dated_facts(db, settings.agent_id, args.sample)
            if not facts:
                print("No dated facts found in eval DB — run ingest first.")
                return

            n = len(facts)
            rescued = 0
            lost = 0
            vanilla_hits = 0
            fused_hits = 0
            today = datetime.date.today()

            print(f"Sampling {n} dated facts, top_k={args.top_k} …\n")

            for fact in facts:
                gold_id: UUID = fact["id"]
                content: str = fact["content"]
                event_date: datetime.date = fact["event_date"]

                # Generate a temporal query about this fact
                query = await _generate_temporal_query(
                    api_client,
                    settings.date_leg_model if hasattr(settings, "date_leg_model")
                    else "claude-haiku-4-5-20251001",
                    content,
                    event_date,
                )

                # Vanilla run (date_leg_enabled=False)
                vanilla_results, _ = await run_recall_pipeline(
                    query=query,
                    heart=heart,
                    brain=brain,
                    settings=settings,
                    limit=args.top_k,
                    memory_types=["fact"],
                )
                vanilla_ids = [r.id for r in vanilla_results]

                # Date-window leg: parse window then retrieve in-window facts
                date_window = await heart.date_window_parser.parse(query, today)
                leg_ids: list[UUID] = []
                if date_window is not None and embedding_provider is not None:
                    embedding = await embedding_provider.embed(query)
                    async with db.session() as session:
                        leg_rows = await heart.facts._date_window_leg(
                            session,
                            embedding,
                            date_window,
                            limit=fused_settings.date_leg_k
                            if hasattr(fused_settings, "date_leg_k") else 15,
                        )
                    leg_ids = [row[0] for row in leg_rows]

                # Fuse
                fused_ids = rrf_fuse(vanilla_ids, leg_ids)
                top_fused = fused_ids[: args.top_k]

                in_vanilla = gold_id in vanilla_ids
                in_fused = gold_id in top_fused

                if in_vanilla:
                    vanilla_hits += 1
                if in_fused:
                    fused_hits += 1
                if in_fused and not in_vanilla:
                    rescued += 1
                if in_vanilla and not in_fused:
                    lost += 1

            print(
                f"{'n':>6}  {'vanilla_top_k':>13}  {'fused_top_k':>11}  "
                f"{'rescued':>7}  {'lost':>4}"
            )
            print(
                f"{n:>6}  {vanilla_hits:>13}  {fused_hits:>11}  "
                f"{rescued:>7}  {lost:>4}"
            )

    finally:
        await api_client.close()
        await db.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
