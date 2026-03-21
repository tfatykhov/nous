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

from nous.brain.brain import Brain
from nous.config import Settings
from nous.events import Event, EventBus
from nous.handlers import LLMClient, call_background_llm, parse_llm_json
from nous.heart.heart import Heart
from nous.heart.schemas import FactInput, FactRejected

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
  "summary": "<2-3 sentence reflection on the day>",
  "facts": [
    {{
      "subject": "<who/what the fact is about>",
      "content": "<the fact, stated clearly>",
      "category": "<preference|person|rule|technical|concept|tool>"
    }}
  ]
}}

Categories for facts:
- "preference" — User preferences (formats, units, style)
- "person" — People facts (names, roles, relationships)
- "rule" — ONLY explicit directives from the user
- "technical" — Architecture, implementation, project-specific knowledge
- "concept" — General knowledge, research findings, theoretical insights
- "tool" — Tool/library behavior, gotchas, configuration

For facts: Extract concrete, reusable knowledge from the reflection. Include the reflection
summary and any lessons as structured facts with meaningful subjects (not generic labels).
Max 5 facts."""

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
        llm_client: LLMClient | None = None,
    ):
        self._brain = brain
        self._heart = heart
        self._settings = settings
        self._bus = bus
        self._llm = llm_client
        self._interrupted = False
        self._sleeping = False
        self._sleep_task: asyncio.Task | None = None
        self._procedure_learner = None  # F012: Set externally if enabled

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

    @property
    def is_sleeping(self) -> bool:
        return self._sleeping

    async def _run_sleep(self, event: Event) -> None:
        """Actual sleep work — runs as independent task, NOT blocking bus."""
        self._sleeping = True
        self._interrupted = False
        phases_completed: list[str] = []
        sleep_stats = {"facts_created": 0, "procedures_created": 0, "censors_retired": 0}

        try:
            logger.info("Sleep mode started — beginning consolidation")

            # Phase ordering: free first, LLM last
            if not self._interrupted:
                success = await self._phase_review_decisions()
                if success:
                    phases_completed.append("review")

            if not self._interrupted:
                success = await self._phase_prune()
                if success:
                    phases_completed.append("prune")

            if not self._interrupted:
                success = await self._phase_compress()
                if success:
                    phases_completed.append("compress")

            if not self._interrupted:
                success = await self._phase_reflect(sleep_stats)
                if success:
                    phases_completed.append("reflect")

            if not self._interrupted:
                success = await self._phase_generalize(sleep_stats)
                if success:
                    phases_completed.append("generalize")

            await self._bus.emit(Event(
                type="sleep_completed",
                agent_id=event.agent_id,
                data={
                    "phases_completed": phases_completed,
                    "interrupted": self._interrupted,
                    **sleep_stats,
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

    async def _phase_review_decisions(self) -> bool:
        """Phase 1: Check pending decisions for observable outcomes. Free."""
        try:
            # Get recent unreviewed decisions
            decisions, _total = await self._brain.list_decisions(limit=10)
            logger.debug(
                "Sleep phase: decision review — checked %d recent decisions",
                len(decisions),
            )
            return True
        except Exception:
            logger.warning("Decision review phase failed", exc_info=True)
            return False

    async def _phase_prune(self) -> bool:
        """Phase 2: Retire stale censors, clean working memory. Free."""
        try:
            logger.debug("Sleep phase: prune (stub)")
            return True
        except Exception:
            logger.warning("Prune phase failed", exc_info=True)
            return False

    # ------------------------------------------------------------------
    # LLM phases
    # ------------------------------------------------------------------

    async def _phase_compress(self) -> bool:
        """Phase 3: Compress old episodes (>7 days) without summaries."""
        if not self._llm:
            return True
        try:
            # Find episodes older than 7 days without structured_summary
            # Generate summaries for up to 5 per sleep cycle
            logger.debug("Sleep phase: compress old episodes (stub)")
            return True
        except Exception:
            logger.warning("Compress phase failed", exc_info=True)
            return False

    async def _phase_reflect(self, sleep_stats: dict) -> bool:
        """Phase 4: Cross-session reflection on recent activity."""
        if not self._llm:
            return True
        try:
            # Use list_recent instead of search_episodes("") — proper method
            recent = await self._heart.list_episodes(limit=10)
            if not recent or len(recent) < 2:
                logger.debug("Not enough recent episodes for reflection")
                return True

            episodes_text = "\n\n".join(
                f"- {ep.summary[:200]}" for ep in recent if ep.summary
            )
            if not episodes_text:
                return True

            prompt = _REFLECTION_PROMPT.format(episodes=episodes_text)

            text = await call_background_llm(
                self._llm,
                model=self._settings.background_model,
                system_prompt="You are an AI agent reflecting on your recent activity.",
                user_message=prompt,
                max_tokens=500,
            )

            if not text:
                return True

            reflection = parse_llm_json(text)
            if isinstance(reflection, list):
                logger.warning(
                    "Reflection LLM returned list instead of dict (len=%d), treating as facts array. Raw: %s",
                    len(reflection), text[:500],
                )
                # Treat the list as a bare facts array
                reflection = {"facts": reflection}
            elif not isinstance(reflection, dict):
                logger.warning(
                    "Reflection LLM returned %s instead of dict, skipping. Raw: %s",
                    type(reflection).__name__, text[:500],
                )
                return True

            # Store structured facts from LLM (preferred path)
            structured_facts = reflection.get("facts", [])
            stored = 0
            for fact in structured_facts[:5]:
                if self._interrupted:
                    break
                if isinstance(fact, dict) and fact.get("content"):
                    result = await self._heart.learn(FactInput(
                        subject=fact.get("subject", "reflection"),
                        content=fact["content"],
                        source="sleep_reflection",
                        confidence=0.8,
                        category=fact.get("category", "concept"),
                    ))
                    if isinstance(result, FactRejected):
                        logger.debug("Admission rejected sleep-reflected fact: %s", fact["content"][:50])
                        continue
                    stored += 1
                    sleep_stats["facts_created"] += 1

            # Fallback: if LLM didn't return structured facts, store summary + lessons
            if not structured_facts:
                if reflection.get("summary"):
                    result = await self._heart.learn(FactInput(
                        subject="daily_reflection",
                        content=reflection["summary"],
                        source="sleep_reflection",
                        confidence=0.8,
                        category="concept",
                    ))
                    if isinstance(result, FactRejected):
                        logger.debug("Admission rejected sleep-reflected fact: %s", reflection["summary"][:50])
                    else:
                        stored += 1
                        sleep_stats["facts_created"] += 1

                for lesson in reflection.get("lessons", [])[:3]:
                    if self._interrupted:
                        break
                    result = await self._heart.learn(FactInput(
                        subject="lesson_learned",
                        content=lesson,
                        source="sleep_reflection",
                        confidence=0.7,
                        category="rule",
                    ))
                    if isinstance(result, FactRejected):
                        logger.debug("Admission rejected sleep-reflected fact: %s", lesson[:50])
                        continue
                    stored += 1
                    sleep_stats["facts_created"] += 1

            logger.info(
                "Reflection complete: %d patterns, %d lessons, %d facts stored",
                len(reflection.get("patterns", [])),
                len(reflection.get("lessons", [])),
                stored,
            )
            return True

        except Exception:
            logger.warning("Reflection phase failed", exc_info=True)
            return False

    async def _phase_generalize(self, sleep_stats: dict) -> bool:
        """Phase 5: K-line learning — auto-create procedures from patterns."""
        if self._procedure_learner:
            try:
                stats = await self._procedure_learner.run_sleep_learning()
                sleep_stats["procedures_created"] += stats.get("decisions_learned", 0)
                logger.info(
                    "Sleep generalize: %d decisions, %d episodes, %d reviewed",
                    stats.get("decisions_learned", 0),
                    stats.get("episodes_learned", 0),
                    stats.get("weak_reviewed", 0),
                )
                return True
            except Exception:
                logger.warning("Generalize phase (procedure learning) failed", exc_info=True)
                return False
        else:
            logger.debug("Sleep phase: generalize (no procedure learner configured)")
            return True
