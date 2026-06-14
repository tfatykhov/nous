"""F075.1 — one-time retrofit: classify event_date on existing heart.facts.

Standalone operational script (NOT a startup handler — runs once, manually,
per agent). For each fact with event_date_classified_at IS NULL, asks a
background LLM to extract the calendar date the fact's event happened on
(using the source episode chunk as context, not the lossy summary), writes
event_date (date | NULL) + stamps event_date_classified_at, then triggers
GraphDensifier to build happened_before edges over the freshly-dated facts.

Correctness patterns copied verbatim from the F075 spec §Layer 4 (hardened
across 17 codex rounds):
  - session-scoped pg_try_advisory_lock on a DEDICATED checked-out
    connection (never commits / never pool-released) so the lock survives
    every per-batch commit; namespaced "f075-temporal:" so it doesn't
    collide with F047's actionability lock.
  - per-batch work on fresh sessions; commit-before-early-return on budget
    exhaustion so spent LLM calls aren't rolled back.
  - eligibility = event_date_classified_at IS NULL (NOT event_date IS NULL):
    a stable fact correctly classified as "no date" stays classified and
    is never re-processed.
  - chunk context via cosine to fact.embedding (serialized pgvector literal,
    NULL-guarded both sides).

Usage:
    uv run python scripts/backfill_temporal_facts.py --agent-id nous-default
    uv run python scripts/backfill_temporal_facts.py --agent-id nous-default --dry-run
    uv run python scripts/backfill_temporal_facts.py --agent-id X --batch-size 100 --token-budget 50000
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
from datetime import date
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from nous.api.runner import create_client
from nous.config import Settings
from nous.handlers import call_background_llm_structured
from nous.heart.schemas import FactInput
from nous.storage.database import Database

logger = logging.getLogger(__name__)

# Salt by feature so F075's lock never collides with F047's actionability
# lock on the same agent (both would otherwise hash the bare agent_id).
_LOCK_NAMESPACE = "f075-temporal"
# Rough Haiku call cost; converts --token-budget into a hard call cap.
_TOKENS_PER_LLM_CALL = 250


def _advisory_lock_key(agent_id: str) -> int:
    """Stable signed bigint advisory-lock key, namespaced to F075."""
    salted = f"{_LOCK_NAMESPACE}:{agent_id}".encode("utf-8")
    digest = hashlib.sha256(salted).digest()[:8]
    return int.from_bytes(digest, byteorder="big", signed=True)


class BudgetTracker:
    """Call-count budget. consume() decrements by 1; ok() gates the next call."""

    def __init__(self, max_calls: int) -> None:
        self.remaining_calls = max_calls

    def ok(self) -> bool:
        return self.remaining_calls > 0

    def consume(self) -> None:
        self.remaining_calls -= 1


async def _fetch_chunk_context(
    session: AsyncSession,
    agent_id: str,
    episode_id: UUID | None,
    fact_id: UUID,
    has_embedding: bool,
) -> str | None:
    """Nearest source-episode chunk by cosine to the fact's embedding.

    Returns None for legacy rows (no source episode / no embedding) or when
    no embedded chunk matches — caller falls back to fact.content.

    The cosine is computed entirely in SQL via a correlated subquery on the
    fact's embedding — never round-tripping the vector through Python. A raw
    ``text()`` SELECT returns pgvector as a text literal (``"[...]"``), so the
    old serialize-in-Python path raised ValueError on '[' for every embedded
    fact; the repo's chunk↔fact cosine (graph_densifier) likewise keeps the
    vector in SQL (``c.embedding <=> f.embedding``).
    """
    if episode_id is None or not has_embedding:
        return None
    result = await session.execute(
        text(
            """
            SELECT content
            FROM heart.episode_chunks
            WHERE agent_id = :agent_id
              AND episode_id = :episode_id
              AND embedding IS NOT NULL
            ORDER BY embedding <=> (
                SELECT embedding FROM heart.facts WHERE id = :fact_id
            )
            LIMIT 1
            """
        ),
        {"agent_id": agent_id, "episode_id": episode_id, "fact_id": fact_id},
    )
    row = result.first()
    return row[0] if row else None


_CLASSIFY_SYSTEM = (
    "You extract the single calendar date an event happened on, from a memory "
    "fact and its source context. Only use a date explicitly present in the "
    "text — never guess. If there is no clear single event date, return null.\n"
    "Do NOT return the publication, arXiv, release, or version date of a paper, "
    "article, library, model, or other referenced artifact — that is "
    "bibliographic metadata, not an event. Return null for a fact that is just "
    "describing or citing such an artifact.\n"
    "Only return a date when you can pin the event to a specific DAY. If only a "
    "month or year is known, return null — never default to the 1st of the month."
)


async def _classify_event_date(
    client, model: str, row, chunk_context: str | None
) -> tuple[date | None, bool]:
    """LLM-extract the event date for one fact.

    Returns ``(event_date, should_stamp)``:
      - valid date string        -> (date,  True)   record + terminal
      - explicit null            -> (None,  True)   "no date", terminal
      - malformed non-null date  -> (None,  False)  leave NULL, retry next run
      - no structured result     -> (None,  False)  transient failure, retry

    Normalizes the raw LLM string through FactInput's validator so malformed
    shapes ('2024-3-10', ISO week, bad calendar dates) are dropped exactly as
    on the live path. A non-null raw the validator drops is malformed and is
    NOT stamped terminal — otherwise one bad model response would permanently
    lose a recoverable date on a legacy fact (mirrors the F075 live-path
    "malformed stays eligible" rule). The row is simply revisited next run.
    """
    content = row["content"]
    context = (chunk_context or content)[:1500]
    learned_at = row.get("learned_at")
    anchor = ""
    if learned_at is not None:
        # Year anchor: relative/ambiguous dates must resolve against when the
        # fact was recorded, not default to a prior year (fixes the wrong-year
        # bug behind the 365-day happened_before chains).
        anchor = (
            f"This fact was recorded on {learned_at:%Y-%m-%d}. Resolve the YEAR "
            f"of any relative or ambiguous date against that — never assume a "
            f"prior year.\n\n"
        )
    user_message = (
        f"Fact: {content}\n\n"
        f"Source context:\n{context}\n\n"
        f"{anchor}"
        "Return the YYYY-MM-DD date this fact's event happened on, or null if "
        "no clear single date is present."
    )
    result = await call_background_llm_structured(
        client,
        model=model,
        system_prompt=_CLASSIFY_SYSTEM,
        user_message=user_message,
        tool_name="record_event_date",
        tool_description="Record the ISO date the fact's event occurred on, or null.",
        output_schema={
            "type": "object",
            "properties": {
                "event_date": {
                    "type": ["string", "null"],
                    "description": "YYYY-MM-DD or null",
                }
            },
            "required": ["event_date"],
        },
        max_tokens=100,
    )
    if not result:
        # Transient LLM failure — don't stamp, retry on the next run.
        return (None, False)
    raw = result.get("event_date")
    validated = FactInput(content="_", event_date=raw).event_date
    malformed = raw is not None and validated is None
    return (validated, not malformed)


def _eligibility_clause(reclassify: bool) -> str:
    """SQL predicate selecting which facts to (re)classify.

    Default backfill: never-classified rows (``event_date_classified_at IS NULL``).
    ``--reclassify``: rows that ALREADY carry a date — used to re-examine the
    suspect population after a prompt fix (bibliographic / wrong-year dates from
    the pre-fix prompt). A re-examined bibliographic fact is corrected to NULL.
    """
    return "event_date IS NOT NULL" if reclassify else "event_date_classified_at IS NULL"


async def _process_batch(
    session: AsyncSession,
    agent_id: str,
    batch_size: int,
    budget: BudgetTracker,
    *,
    client,
    model: str,
    cursor: tuple | None,
    reclassify: bool = False,
) -> tuple[int, bool, tuple | None, int]:
    """Classify one keyset-paginated batch of never-classified facts.

    Returns ``(updated, stop, new_cursor, fetched)``. ``cursor`` is the
    ``(learned_at, id)`` of the last row processed in the previous batch (None
    for the first). Keyset pagination — rather than always re-taking the top-N
    by recency — lets us leave malformed/transient rows un-stamped without
    re-fetching them this run (they'd otherwise reappear forever, or a full
    page of them would stall older eligible rows behind a plain LIMIT). Still-
    NULL rows are naturally retried on the next run.
    """
    params: dict = {"agent_id": agent_id, "batch_size": batch_size}
    elig_clause = _eligibility_clause(reclassify)
    cursor_clause = ""
    if cursor is not None:
        cursor_clause = "AND (learned_at, id) < (:cur_la, :cur_id)"
        params["cur_la"], params["cur_id"] = cursor
    result = await session.execute(
        text(
            f"""
            SELECT id, subject, content,
                   (embedding IS NOT NULL) AS has_embedding,
                   source_episode_id, learned_at
            FROM heart.facts
            WHERE agent_id = :agent_id
              AND {elig_clause}
              AND active = TRUE
              {cursor_clause}
            ORDER BY learned_at DESC, id DESC
            LIMIT :batch_size
            """
        ),
        params,
    )
    rows = result.mappings().all()

    updated = 0
    new_cursor = cursor
    for row in rows:
        if not budget.ok():
            logger.info("F075 backfill: token budget exhausted, stopping at %d rows", updated)
            await session.commit()  # persist work-so-far before the early return
            return (updated, True, new_cursor, len(rows))

        chunk_ctx = await _fetch_chunk_context(
            session, agent_id, row["source_episode_id"], row["id"], row["has_embedding"]
        )
        classified, should_stamp = await _classify_event_date(client, model, row, chunk_ctx)
        budget.consume()
        # Advance the cursor for every consumed row, stamped or not, so a
        # skipped (malformed/transient) row isn't re-fetched within this run.
        new_cursor = (row["learned_at"], row["id"])

        if not should_stamp:
            # Malformed or transient — leave classified_at NULL; retry next run.
            continue

        # Stamp classified_at (even when event_date is NULL = "no date") so a
        # cleanly-answered row is never re-processed.
        await session.execute(
            text(
                """
                UPDATE heart.facts
                SET event_date = :event_date,
                    event_date_classified_at = NOW(),
                    updated_at = NOW()
                WHERE id = :id
                """
            ),
            {"event_date": classified, "id": row["id"]},
        )
        updated += 1

    await session.commit()
    return (updated, False, new_cursor, len(rows))


async def run_temporal_backfill(
    db: Database,
    client,
    settings: Settings,
    agent_id: str,
    batch_size: int,
    token_budget: int,
    reclassify: bool = False,
) -> dict:
    """Lock-protected batch loop + happened_before edge build. Returns stats."""
    key = _advisory_lock_key(agent_id)
    total = 0
    # Dedicated lock-holding connection: never commits, never pool-released,
    # so the session-scoped advisory lock survives every per-batch commit.
    async with db.engine.connect() as lock_conn:
        locked = await lock_conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key})
        if not locked.scalar():
            logger.info("F075 backfill: another process holds the lock for %s, exiting", agent_id)
            return {"agent_id": agent_id, "classified": 0, "skipped": "lock_held"}
        try:
            # Hard call cap. No max(1, ...): --token-budget 0 (or below one
            # call's cost) must perform zero LLM calls, matching the dry-run
            # estimate and honoring the operator's cost guard.
            budget = BudgetTracker(max(0, token_budget // _TOKENS_PER_LLM_CALL))
            cursor: tuple | None = None
            while True:
                async with db.session_factory() as work_session:
                    updated, stop, cursor, fetched = await _process_batch(
                        work_session, agent_id, batch_size, budget,
                        client=client, model=settings.background_model,
                        cursor=cursor, reclassify=reclassify,
                    )
                total += updated
                logger.info("F075 backfill: %d facts classified so far", total)
                # A short page means we've walked every eligible row once.
                if stop or fetched < batch_size:
                    break
        finally:
            await lock_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})

    # Build happened_before edges over the freshly-dated facts.
    from nous.brain.graph_densifier import GraphDensifier
    from nous.brain.graph_linker import GraphLinker
    from nous.brain.embeddings import EmbeddingProvider

    embedder = EmbeddingProvider(
        api_key=settings.openai_api_key,
        model=settings.embedding_model,
    )
    linker = GraphLinker(db, embedder, settings, agent_id)
    densifier = GraphDensifier(db, linker, embedder, settings, agent_id)
    edges = await densifier._build_happened_before_edges()

    return {"agent_id": agent_id, "classified": total, "happened_before_edges": edges}


async def _count_eligible(db: Database, agent_id: str, reclassify: bool = False) -> int:
    async with db.session_factory() as session:
        result = await session.execute(
            text(
                f"""
                SELECT COUNT(*) FROM heart.facts
                WHERE agent_id = :agent_id
                  AND {_eligibility_clause(reclassify)}
                  AND active = TRUE
                """
            ),
            {"agent_id": agent_id},
        )
        return result.scalar() or 0


async def _main(args) -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = Settings()
    # --token-budget falls back to NOUS_TEMPORAL_BACKFILL_DEFAULT_TOKEN_BUDGET
    # (Settings) when omitted, so an operator who lowers that env default — or
    # sets it to 0 to block spend unless explicitly overridden — is honored by
    # the documented flagless command.
    token_budget = (
        args.token_budget
        if args.token_budget is not None
        else settings.temporal_backfill_default_token_budget
    )
    db = Database(settings)
    await db.connect()
    try:
        eligible = await _count_eligible(db, args.agent_id, args.reclassify)
        if args.dry_run:
            calls = min(eligible, max(0, token_budget // _TOKENS_PER_LLM_CALL))
            est_tokens = calls * _TOKENS_PER_LLM_CALL
            mode = "already-dated (reclassify)" if args.reclassify else "never-classified"
            print(
                f"[dry-run] agent={args.agent_id}: {eligible} facts eligible "
                f"({mode}). With token budget "
                f"{token_budget}, would classify ~{calls} this run "
                f"(~{est_tokens} tokens, no LLM calls made)."
            )
            return
        if eligible == 0:
            print(f"agent={args.agent_id}: 0 eligible facts — nothing to backfill.")
            return

        client = create_client(settings)
        await client.start()
        try:
            stats = await run_temporal_backfill(
                db, client, settings,
                agent_id=args.agent_id,
                batch_size=args.batch_size,
                token_budget=token_budget,
                reclassify=args.reclassify,
            )
        finally:
            await client.close()
        print(f"F075 backfill complete: {stats}")
    finally:
        await db.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description="F075.1 one-time temporal-fact backfill")
    parser.add_argument("--agent-id", required=True, help="Agent whose facts to backfill")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument(
        "--token-budget",
        type=int,
        default=None,
        help="Hard Haiku call cap (tokens). Omit to use "
        "NOUS_TEMPORAL_BACKFILL_DEFAULT_TOKEN_BUDGET.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Estimate only; no LLM calls")
    parser.add_argument(
        "--reclassify",
        action="store_true",
        help="Re-examine facts that ALREADY have an event_date (corrects "
        "bibliographic / wrong-year dates from the pre-fix prompt) instead of "
        "only never-classified rows.",
    )
    asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    main()
