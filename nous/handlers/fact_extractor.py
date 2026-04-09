"""Fact Extractor — proactively learns facts from episode summaries.

Listens to: episode_summarized
Uses the structured summary to identify facts worth remembering.
Deduplicates against existing facts before storing.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from nous.config import Settings
from nous.events import Event, EventBus
from nous.handlers import LLMClient, call_background_llm, parse_llm_json
from nous.heart.heart import Heart
from nous.heart.schemas import FactInput, FactRejected

logger = logging.getLogger(__name__)

_EXTRACT_PROMPT = """Review the following conversation summary and extract facts worth remembering long-term.

Focus on:
- User preferences (tools, formats, units, communication style)
- Project/system facts (architecture, constraints, conventions)
- People facts (roles, names, relationships)
- Explicit rules or directives from the user

Summary: {summary}
Key Points: {key_points}

Return ONLY a valid JSON array (empty array if nothing worth storing):
[
  {{
    "subject": "<who/what the fact is about>",
    "content": "<the fact, stated clearly>",
    "category": "<category>",
    "confidence": <0.0-1.0>
  }}
]

Categories (use the RIGHT one — this affects how facts are loaded):
- "preference" — User preferences (formats, units, style). Loaded EVERY turn.
- "person" — People facts (names, roles, relationships). Loaded EVERY turn.
- "rule" — ONLY explicit directives from the user (e.g., "never push to main"). Loaded EVERY turn.
- "technical" — Architecture, implementation, or project-specific knowledge. Loaded only when relevant.
- "concept" — General knowledge, research findings, theoretical insights. Loaded only when relevant.
- "tool" — Tool/library behavior, gotchas, configuration. Loaded only when relevant.

IMPORTANT: "rule" is for user-stated directives ONLY. Research findings, observations,
debug lessons, and architecture patterns should be "technical" or "concept".
If in doubt between "rule" and something else, choose the something else.

Only include facts genuinely useful across future conversations.
Skip transient, trivial, or already-known information.
Max 5 facts."""


class FactExtractor:
    """Extracts and stores facts from episode summaries.

    Listens to episode_summarized events. Calls LLM to identify facts,
    deduplicates against existing facts, stores new ones.
    Max 5 facts per episode.
    """

    def __init__(
        self,
        heart: Heart,
        settings: Settings,
        bus: EventBus,
        llm_client: LLMClient | None = None,
    ):
        self._heart = heart
        self._settings = settings
        self._bus = bus
        self._llm = llm_client
        bus.on("episode_summarized", self.handle)

    async def handle(self, event: Event) -> None:
        """Handle episode_summarized — extract and store facts.

        008.4: If candidate_facts present in event data, store them directly
        without calling the LLM. Falls back to LLM extraction otherwise.
        """
        summary = event.data.get("summary", {})
        if not summary:
            return

        # F025 P2-E: Extract transcript for fact grounding
        transcript = event.data.get("transcript")

        try:
            # 008.4: Use pre-extracted candidate_facts if available
            candidate_facts = event.data.get("candidate_facts", [])
            if candidate_facts:
                await self._store_candidate_facts(
                    candidate_facts, event.data.get("episode_id", "?"), transcript=transcript
                )
                return

            # Fallback: LLM extraction (backward compatibility)
            candidates = await self._extract_facts(summary)
            if not candidates:
                return

            stored = 0
            for fact in candidates[:5]:  # Max 5 per episode
                confidence = fact.get("confidence", 0.7)
                if confidence < 0.6:
                    logger.debug("Skipping low-confidence fact: %s", fact.get("content", "")[:50])
                    continue

                # Dedup: check if similar fact exists
                content = fact.get("content", "")
                existing = await self._heart.search_facts(content, limit=1)
                # P0-7 fix: use .score not .similarity for hybrid search
                # F025 P2-D: threshold now configurable (default 0.92, raised from 0.85).
                # heart.learn() has its own dedup (>0.95 cosine) and
                # subject-based supersession (same subject + >0.80 cosine).
                if (
                    existing
                    and existing[0].score is not None
                    and existing[0].score > self._settings.fact_dedup_threshold
                ):
                    logger.debug("Skipping duplicate fact: %s", content[:50])
                    continue

                # Store — P1-8 fix: pass category from LLM response
                fact_input = FactInput(
                    subject=fact.get("subject", "unknown"),
                    content=content,
                    source="fact_extractor",
                    confidence=confidence,
                    category=fact.get("category"),
                    source_text=transcript,  # F025 P2-E
                )
                result = await self._heart.learn(fact_input)
                if isinstance(result, FactRejected):
                    logger.debug("Admission rejected extracted fact: %s", content[:50])
                    continue
                stored += 1

            if stored:
                logger.info(
                    "Extracted %d facts from episode %s",
                    stored,
                    event.data.get("episode_id", "?"),
                )

        except Exception:
            logger.exception("Fact extraction failed for episode %s", event.data.get("episode_id"))

    async def _store_candidate_facts(
        self, candidates: list[str | dict], episode_id: str, transcript: str | None = None
    ) -> None:
        """008.4: Store pre-extracted candidate facts directly, with dedup.

        Accepts both structured dicts (with subject/category/content) and
        plain strings for backward compatibility.
        """
        stored = 0
        for item in candidates[:5]:  # Max 5 per episode
            # Handle both structured dicts and plain strings
            if isinstance(item, dict):
                content = item.get("content", "")
                subject = item.get("subject")
                category = item.get("category")
            else:
                content = item
                subject = None
                category = None

            if not content or not str(content).strip():
                continue

            # Dedup against existing facts
            existing = await self._heart.search_facts(content, limit=1)
            if existing and existing[0].score is not None and existing[0].score > self._settings.fact_dedup_threshold:
                logger.debug("Skipping duplicate candidate fact: %s", content[:50])
                continue

            fact_input = FactInput(
                content=content,
                subject=subject or "unknown",
                category=category,
                source="episode_summarizer",
                confidence=0.8,  # Default confidence for LLM-extracted candidates
                source_text=transcript,  # F025 P2-E
            )
            result = await self._heart.learn(fact_input)
            if isinstance(result, FactRejected):
                logger.debug("Admission rejected candidate fact: %s", content[:50])
                continue
            stored += 1

        if stored:
            logger.info(
                "Stored %d candidate facts from episode %s",
                stored,
                episode_id,
            )

    async def _extract_facts(self, summary: dict[str, Any]) -> list[dict[str, Any]]:
        """Call LLM to extract facts from episode summary."""
        if not self._llm:
            return []

        summary_text = summary.get("summary", "")
        key_points = ", ".join(summary.get("key_points", []))

        if not summary_text:
            return []

        prompt = _EXTRACT_PROMPT.format(summary=summary_text, key_points=key_points)

        text = await call_background_llm(
            self._llm,
            model=self._settings.background_model,
            system_prompt="You are extracting facts from an AI agent's conversation summary.",
            user_message=prompt,
            max_tokens=1500,
        )

        if not text:
            return []

        try:
            return parse_llm_json(text)
        except json.JSONDecodeError:
            return []
