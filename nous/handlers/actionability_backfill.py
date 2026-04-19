"""F047: Backfill handler for heart.facts.actionable.

Classifies legacy NULL rows (pre-F047 facts) in batches. Idempotent — only
picks up rows with actionable IS NULL. Safe to run at startup; safe to
run across multiple processes (advisory lock prevents double-classification).

Triggered once at startup when NOUS_ACTIONABILITY_BACKFILL_ON_STARTUP=True.
Can be re-run manually if needed.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import text

if TYPE_CHECKING:
    from nous.heart.actionability import ActionabilityClassifier
    from nous.storage.database import Database

logger = logging.getLogger(__name__)


class ActionabilityBackfillHandler:
    """Batch-classify NULL rows in heart.facts.actionable.

    Single-writer at a time via PG advisory lock keyed on agent_id.
    Rate-limited between batches so we don't saturate the LLM budget
    or the DB.
    """

    BATCH_SIZE = 100
    RATE_LIMIT_DELAY_S = 0.5
    # Estimated tokens per LLM tier-2 call (prompt + response, Haiku).
    # Used to translate the token budget into a hard call cap.
    _TOKENS_PER_LLM_CALL = 250

    def __init__(
        self,
        db: Database,
        classifier: ActionabilityClassifier,
        agent_id: str,
        token_budget: int = 10_000,
    ) -> None:
        self.db = db
        self.classifier = classifier
        self.agent_id = agent_id
        # Budget bookkeeping — budget_check() is passed to classifier so
        # tier-2 LLM calls stop when we've burned the daily cap.
        self._max_llm_calls = max(1, token_budget // self._TOKENS_PER_LLM_CALL)
        self._llm_calls_used = 0
        # Inject budget gate into the (shared) classifier for the duration
        # of the backfill. Stored on the classifier so self.classifier.classify
        # consults it automatically via the existing tier-2 path.
        self._prev_budget_check = classifier._budget_check
        classifier._budget_check = self._budget_ok

    def _budget_ok(self) -> bool:
        """Return True while we still have LLM budget for this backfill."""
        return self._llm_calls_used < self._max_llm_calls

    async def run_once(self) -> dict[str, Any]:
        """Process all NULL rows for this agent. Returns summary dict.

        Safe to call across processes: advisory lock ensures only one
        backfill per agent runs at a time. A second caller returns
        {"skipped": True, "reason": "lock_held"} immediately.
        """
        lock_key = self._advisory_lock_key()

        async with self.db.session() as session:
            try:
                got_lock_result = await session.execute(
                    text("SELECT pg_try_advisory_lock(:k)"),
                    {"k": lock_key},
                )
                got_lock = bool(got_lock_result.scalar())
            except Exception:
                # Not Postgres (e.g. SQLite tests) — skip the lock gate.
                logger.debug("F047: advisory_lock unsupported, proceeding without cross-process guard")
                got_lock = True

            if not got_lock:
                logger.info("F047 backfill: lock held by another process, skipping")
                return {"skipped": True, "reason": "lock_held"}

            try:
                result = await self._run_batches()
            finally:
                try:
                    await session.execute(
                        text("SELECT pg_advisory_unlock(:k)"),
                        {"k": lock_key},
                    )
                except Exception:
                    pass  # Best-effort; lock auto-releases on session close.
                # Restore classifier's prior budget gate so subsequent
                # non-backfill classify() calls aren't affected by our cap.
                self.classifier._budget_check = self._prev_budget_check

        return result

    def _advisory_lock_key(self) -> int:
        """Stable 64-bit signed int derived from agent_id.

        PG advisory locks take a single bigint; hash agent_id so different
        agents don't block each other.
        """
        digest = hashlib.sha256(self.agent_id.encode("utf-8")).digest()[:8]
        return int.from_bytes(digest, "big", signed=True)

    async def _run_batches(self) -> dict[str, Any]:
        total = 0
        classified = 0
        errors = 0
        tier_counts: dict[str, int] = {}
        start = time.monotonic()

        while True:
            batch = await self._fetch_batch()
            if not batch:
                break

            for fact_id, content, category, tags in batch:
                try:
                    actionable, conf, tier = await self.classifier.classify(
                        content, category, tags or [],
                    )
                    if tier == "llm":
                        self._llm_calls_used += 1
                    tier_counts[tier] = tier_counts.get(tier, 0) + 1
                    await self._update_actionable(fact_id, actionable, conf)
                    classified += 1
                except asyncio.CancelledError:
                    # Re-raise — don't swallow cancellation.
                    raise
                except Exception:
                    logger.warning(
                        "F047 backfill: classify/update failed for %s",
                        fact_id,
                        exc_info=True,
                    )
                    errors += 1
                total += 1

            # Rate-limit between batches
            await asyncio.sleep(self.RATE_LIMIT_DELAY_S)

        elapsed = time.monotonic() - start
        summary = {
            "total": total,
            "classified": classified,
            "errors": errors,
            "elapsed_s": round(elapsed, 2),
            "tiers": tier_counts,
            "llm_calls_used": self._llm_calls_used,
            "llm_budget": self._max_llm_calls,
        }
        logger.info("F047 backfill complete: %s", summary)
        if self._llm_calls_used >= self._max_llm_calls and tier_counts.get("default", 0) > 0:
            logger.warning(
                "F047 backfill: LLM budget exhausted (%d/%d calls) — %d fact(s) "
                "fell through to the default path. Raise "
                "NOUS_ACTIONABILITY_BACKFILL_TOKEN_BUDGET to cover them on the "
                "next run.",
                self._llm_calls_used,
                self._max_llm_calls,
                tier_counts.get("default", 0),
            )
        return summary

    async def _fetch_batch(self) -> list[tuple[UUID, str, str | None, list[str]]]:
        """Fetch next batch of NULL rows for this agent."""
        async with self.db.session() as session:
            result = await session.execute(
                text(
                    """
                    SELECT id, content, category, tags
                    FROM heart.facts
                    WHERE agent_id = :aid
                      AND actionable IS NULL
                      AND active = true
                    LIMIT :lim
                    """
                ),
                {"aid": self.agent_id, "lim": self.BATCH_SIZE},
            )
            return [
                (row.id, row.content, row.category, list(row.tags or []))
                for row in result.all()
            ]

    async def _update_actionable(
        self,
        fact_id: UUID,
        actionable: bool,
        confidence: float,
    ) -> None:
        async with self.db.session() as session:
            await session.execute(
                text(
                    """
                    UPDATE heart.facts
                    SET actionable = :a,
                        actionable_confidence = :c
                    WHERE id = :id
                    """
                ),
                {"a": actionable, "c": confidence, "id": fact_id},
            )
            await session.commit()


async def run_backfill_with_supervision(
    handler: ActionabilityBackfillHandler,
) -> None:
    """Fire-and-forget supervisor: catches + logs, re-raises CancelledError.

    Use this wrapper when scheduling the handler as a background task at
    startup so exceptions don't get swallowed by asyncio.
    """
    try:
        await handler.run_once()
    except asyncio.CancelledError:
        logger.info("F047 backfill cancelled at shutdown — will resume next startup")
        raise
    except Exception:
        logger.exception("F047 backfill failed")
