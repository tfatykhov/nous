"""F039 — Correction Learning Pipeline: Batch Extractor.

Listens to: outcome_signals_detected
Extracts generalizable principles from user corrections and stores them
as facts (+ optional censors for "never do" patterns).

Inspired by Databricks' MemAlign paper — dual-memory correction learning.
"""

from __future__ import annotations

import logging
from typing import Any

from nous.config import Settings
from nous.events import Event, EventBus
from nous.handlers import LLMClient, call_background_llm, parse_llm_json
from nous.heart.heart import Heart
from nous.heart.schemas import CensorInput, FactInput
from nous.storage.database import Database

logger = logging.getLogger(__name__)

_EXTRACTION_PROMPT = """\
Given this conversation where the user corrected the AI:

Episode summary: {summary}
Transcript excerpt (last 2000 chars): {transcript_tail}
Correction evidence: {evidence}

Extract:
1. principle: A generalizable rule the AI should follow (1-2 sentences)
2. subject: What topic/domain this rule applies to
3. is_censor: true if this is a "never do X" pattern, false if it's a "prefer X over Y" pattern
4. censor_pattern: If is_censor=true, a short trigger phrase for the censor
5. confidence: 0.0-1.0 how generalizable this rule is

Return ONLY valid JSON:
{{"principle": "...", "subject": "...", "is_censor": false, "censor_pattern": null, "confidence": 0.8}}"""


class CorrectionExtractor:
    """Extracts correction principles from outcome signals."""

    def __init__(
        self,
        db: Database,
        settings: Settings,
        bus: EventBus,
        llm_client: LLMClient | None,
        heart: Heart,
        agent_id: str,
    ) -> None:
        self._db = db
        self._settings = settings
        self._bus = bus
        self._llm = llm_client
        self._heart = heart
        self._agent_id = agent_id
        bus.on("outcome_signals_detected", self.handle)

    async def handle(self, event: Event) -> None:
        """Handle outcome_signals_detected — extract principles from corrections."""
        if not self._settings.correction_extraction_enabled:
            return

        signals = event.data.get("signals", [])
        corrected_signals = [s for s in signals if s.get("type") == "corrected"]
        if not corrected_signals:
            return

        episode_id = event.data.get("episode_id")
        if not episode_id:
            return

        # Get episode data from DB
        transcript = ""
        summary: dict[str, Any] = {}
        try:
            async with self._db.session() as session:
                from uuid import UUID

                from sqlalchemy import select

                from nous.storage.models import Episode

                result = await session.execute(select(Episode).where(Episode.id == UUID(episode_id)))
                episode = result.scalar_one_or_none()
                if episode:
                    transcript = episode.transcript or ""
                    summary = episode.structured_summary or {}
                    if not summary and episode.summary:
                        summary = {"summary": episode.summary}
        except Exception:
            logger.warning("F039: Failed to fetch episode %s", episode_id)

        # Extract a principle for each corrected signal
        for sig in corrected_signals:
            try:
                extraction = await self._extract_principle(
                    summary=summary,
                    transcript=transcript,
                    evidence=sig.get("evidence", ""),
                )
                if not extraction:
                    continue

                principle = extraction.get("principle", "")
                if not principle or len(principle) < 30:
                    continue

                # Store as fact
                fact_input = FactInput(
                    content=principle,
                    category="rule",
                    subject=extraction.get("subject"),
                    confidence=max(0.0, min(1.0, float(extraction.get("confidence", 0.7)))),
                    source="correction_extraction",
                    tags=["correction", "auto:f055"],
                )
                await self._heart.learn(fact_input)
                logger.info("F039: Stored correction fact: %s", principle[:80])

                # Optionally create censor for "never do" patterns
                if extraction.get("is_censor") and extraction.get("censor_pattern"):
                    censor_input = CensorInput(
                        trigger_pattern=extraction["censor_pattern"],
                        reason=f"F039: Learned from correction — {principle[:100]}",
                        action="warn",
                    )
                    await self._heart.add_censor(censor_input)
                    logger.info("F039: Created censor for correction: %s", extraction["censor_pattern"])

            except Exception:
                logger.exception("F039: Failed to extract correction principle")

    async def _extract_principle(
        self,
        summary: dict,
        transcript: str,
        evidence: str,
    ) -> dict | None:
        """Use LLM to extract a generalizable principle from a correction."""
        if not self._llm:
            return None

        import json

        prompt = _EXTRACTION_PROMPT.format(
            summary=json.dumps(summary, indent=2)[:2000],
            transcript_tail=transcript[-2000:] if transcript else "(no transcript)",
            evidence=evidence or "(no evidence)",
        )

        try:
            raw = await call_background_llm(
                self._llm,
                self._settings.background_model,
                "You are a correction analysis system. Extract generalizable rules "
                "from user corrections. Respond only with JSON.",
                prompt,
                max_tokens=512,
            )
            if not raw:
                return None

            return parse_llm_json(raw)
        except Exception:
            logger.warning("F039: LLM extraction failed for correction principle")
            return None
