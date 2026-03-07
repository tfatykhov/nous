"""Sleep Handler — runs reflection, compaction, and pruning during idle periods.

Listens to: sleep_started
Emits: sleep_completed

Mirrors how biological brains consolidate during sleep:
1. Review — check pending decision outcomes (free)
2. Prune — retire stale censors, clean working memory (free)
3. Compress — old episodes -> compressed summaries (LLM)
4. Reflect — cross-session pattern recognition (LLM)
5. Generalize — similar facts -> generalized facts (LLM)

Phases 1-2 are free (DB only). Phases 3-5 use LLM calls (background_model).
Sleep is interruptible — if a new message arrives, in-progress work completes
but remaining phases are skipped.

P0-11 fix: handle() spawns asyncio.Task and returns immediately to avoid
blocking the event bus dispatch loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx

from nous.brain.brain import Brain
from nous.config import Settings
from nous.events import Event, EventBus
from nous.handlers import build_anthropic_headers, parse_llm_json
from nous.heart.heart import Heart
from nous.heart.schemas import FactInput

logger = logging.getLogger(__name__)

_REFLECTION_PROMPT = """You are an AI agent reviewing your recent activity. Analyze the following
episode summaries from the past 24 hours and identify:

1. Patterns — recurring topics, user needs, or behaviors
2. Lessons — what worked well, what didn't
3. Connections — links between seemingly unrelated conversations
4. Gaps — knowledge you needed but didn't have

Episodes:
{episodes}

Return ONLY valid JSON:
{{
  "patterns": ["<pattern 1>", "<pattern 2>"],
  "lessons": ["<lesson 1>", "<lesson 2>"],
  "connections": ["<connection 1>"],
  "gaps": ["<gap 1>"],
  "summary": "<2-3 sentence reflection on the day>"
}}"""

_GENERALIZE_PROMPT = """These facts are about the same topic. Create one generalized fact
that captures the essential knowledge from all of them.

Facts:
{facts}

Return ONLY valid JSON:
{{
  "subject": "<who/what>",
  "content": "<generalized fact>",
  "confidence": <0.0-1.0>
}}"""


class SleepHandler:
    """Runs reflection and maintenance during idle periods.

    P0-11 fix: handle() spawns a background asyncio.Task so it returns
    immediately and does not block the event bus dispatch loop.

    Each phase checks self._interrupted before proceeding.
    A new message_received event sets _interrupted = True.
    """

    def __init__(
        self,
        brain: Brain,
        heart: Heart,
        settings: Settings,
        bus: EventBus,
        http_client: httpx.AsyncClient | None = None,
    ):
        self._brain = brain
        self._heart = heart
        self._settings = settings
        self._bus = bus
        self._http = http_client
        self._interrupted = False
        self._sleeping = False
        self._sleep_task: asyncio.Task | None = None

        bus.on("sleep_started", self.handle)
        bus.on("message_received", self._on_wake)

    async def _on_wake(self, event: Event) -> None:
        """Interrupt sleep on new activity."""
        if self._sleeping:
            logger.info("Sleep interrupted by new message")
            self._interrupted = True

    async def handle(self, event: Event) -> None:
        """Spawn sleep work as background task — return immediately to unblock bus.

        P0-11 fix: The actual sleep work runs as an independent asyncio.Task,
        NOT blocking the bus dispatch.
        """
        if self._sleeping:
            return  # Already sleeping
        self._sleep_task = asyncio.create_task(
            self._run_sleep(event), name="sleep-work"
        )

    async def _run_sleep(self, event: Event) -> None:
        """Actual sleep work — runs as independent task, NOT blocking bus."""
        self._sleeping = True
        self._interrupted = False
        phases_completed: list[str] = []

        try:
            logger.info("Sleep mode started — beginning consolidation")

            # Phase ordering: free first, LLM last
            if not self._interrupted:
                await self._phase_review_decisions()
                phases_completed.append("review")

            if not self._interrupted:
                await self._phase_prune()
                phases_completed.append("prune")

            if not self._interrupted:
                await self._phase_compress()
                phases_completed.append("compress")

            if not self._interrupted:
                await self._phase_reflect()
                phases_completed.append("reflect")

            if not self._interrupted:
                await self._phase_generalize()
                phases_completed.append("generalize")

            await self._bus.emit(Event(
                type="sleep_completed",
                agent_id=event.agent_id,
                data={
                    "phases_completed": phases_completed,
                    "interrupted": self._interrupted,
                },
            ))
            logger.info(
                "Sleep completed: %s (interrupted=%s)",
                phases_completed,
                self._interrupted,
            )

        except Exception:
            logger.exception("Sleep handler error")
        finally:
            self._sleeping = False
            self._sleep_task = None

    # ------------------------------------------------------------------
    # Free phases (no LLM)
    # ------------------------------------------------------------------

    async def _phase_review_decisions(self) -> None:
        """Phase 1: Check pending decisions for observable outcomes. Free."""
        try:
            # Get recent unreviewed decisions
            decisions, _total = await self._brain.list_decisions(limit=10)
            logger.debug(
                "Sleep phase: decision review — checked %d recent decisions",
                len(decisions),
            )
        except Exception:
            logger.warning("Decision review phase failed")

    async def _phase_prune(self) -> None:
        """Phase 2: Retire stale censors, clean working memory. Free."""
        try:
            logger.debug("Sleep phase: prune (stub)")
        except Exception:
            logger.warning("Prune phase failed")

    # ------------------------------------------------------------------
    # LLM phases
    # ------------------------------------------------------------------

    async def _phase_compress(self) -> None:
        """Phase 3: Compress old episodes (>7 days) without summaries."""
        if not self._http:
            return
        try:
            # Find episodes older than 7 days without structured_summary
            # Generate summaries for up to 5 per sleep cycle
            logger.debug("Sleep phase: compress old episodes (stub)")
        except Exception:
            logger.warning("Compress phase failed")

    async def _phase_reflect(self) -> None:
        """Phase 4: Cross-session reflection on recent activity."""
        if not self._http:
            return
        try:
            # Use list_recent instead of search_episodes("") — proper method
            recent = await self._heart.list_episodes(limit=10)
            if not recent or len(recent) < 2:
                logger.debug("Not enough recent episodes for reflection")
                return

            episodes_text = "\n\n".join(
                f"- {ep.summary[:200]}" for ep in recent if ep.summary
            )
            if not episodes_text:
                return

            prompt = _REFLECTION_PROMPT.format(episodes=episodes_text)
            headers = build_anthropic_headers(self._settings)

            response = await self._http.post(
                f"{self._settings.api_base_url}/v1/messages",
                json={
                    "model": self._settings.background_model,
                    "max_tokens": 500,
                    "messages": [{"role": "user", "content": prompt}],
                },
                headers=headers,
                timeout=30,
            )

            if response.status_code != 200:
                return

            data = response.json()
            # Find the text block — skip thinking blocks if present
            text = ""
            for block in data.get("content", []):
                if block.get("type") == "text":
                    text = block.get("text", "")
                    break
            reflection = parse_llm_json(text)

            # Store reflection summary as a fact
            if reflection.get("summary"):
                await self._heart.learn(FactInput(
                    subject="daily_reflection",
                    content=reflection["summary"],
                    source="sleep_reflection",
                    confidence=0.8,
                    category="concept",
                ))

            # Store lessons as individual facts
            for lesson in reflection.get("lessons", [])[:3]:
                if self._interrupted:
                    break
                await self._heart.learn(FactInput(
                    subject="lesson_learned",
                    content=lesson,
                    source="sleep_reflection",
                    confidence=0.7,
                    category="rule",
                ))

            logger.info(
                "Reflection complete: %d patterns, %d lessons",
                len(reflection.get("patterns", [])),
                len(reflection.get("lessons", [])),
            )

        except (json.JSONDecodeError, Exception):
            logger.warning("Reflection phase failed")

    async def _phase_generalize(self) -> None:
        """Phase 5: Merge similar facts into generalized facts."""
        if not self._http:
            return
        try:
            # Stub — needs Heart.find_fact_clusters() for full implementation
            logger.debug("Sleep phase: generalize facts (stub)")
        except Exception:
            logger.warning("Generalize phase failed")
