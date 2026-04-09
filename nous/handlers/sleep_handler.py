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
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from nous.brain.brain import Brain
from nous.config import Settings
from nous.events import Event, EventBus
from nous.handlers import LLMClient, call_background_llm_structured
from nous.heart.heart import Heart
from nous.heart.schemas import FactInput, FactRejected
from nous.storage.models import Fact

logger = logging.getLogger(__name__)

_REFLECTION_PROMPT = """You are an AI agent reviewing your recent activity. Analyze the following
episode summaries from the past 24 hours and identify:

1. Patterns — recurring topics, user needs, or behaviors
2. Lessons — what worked well, what didn't
3. Connections — links between seemingly unrelated conversations
4. Gaps — knowledge you needed but didn't have

Episodes:
{episodes}
{orient_context}
Use the store_reflection tool to return your analysis.

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

# Tool-use structured output schema for sleep reflection (Issue #233)
_REFLECTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "patterns": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Recurring behavioral or workflow patterns observed across sessions",
        },
        "lessons": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Concrete lessons learned from failures or successes",
        },
        "connections": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Cross-session connections between seemingly separate topics",
        },
        "gaps": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Knowledge or implementation gaps identified",
        },
        "summary": {
            "type": "string",
            "description": "One-paragraph summary of the day's trajectory",
        },
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "subject": {
                        "type": "string",
                        "description": "Fact subject line, prefix with UPDATES: if superseding an existing fact",
                    },
                    "content": {
                        "type": "string",
                        "description": "Detailed fact content",
                    },
                    "category": {
                        "type": "string",
                        "enum": ["technical", "preference", "person", "tool", "concept", "rule"],
                        "description": "Fact category",
                    },
                },
                "required": ["subject", "content", "category"],
            },
            "description": "Structured facts to store in memory (max 5)",
        },
    },
    "required": ["patterns", "lessons", "connections", "gaps", "summary", "facts"],
}

_CONTRADICTION_RESOLUTION_PROMPT = """Two facts exist in memory about the same subject. Determine the correct action:

Fact A (stored {date_a}): {content_a}
Fact B (stored {date_b}): {content_b}

Actions:
- SUPERSEDE_A: Fact B is the current/correct version, retire Fact A
- SUPERSEDE_B: Fact A is the current/correct version, retire Fact B
- MERGE: Both contain partial truth, merge into single fact
- KEEP_BOTH: Genuinely different information, both valid
- REMOVE_A: Fact A is wrong/stale, remove it
- REMOVE_B: Fact B is wrong/stale, remove it

Use the resolve_contradiction tool to return your analysis."""

# Tool-use structured output schema for contradiction resolution (Issue #233)
_CONTRADICTION_RESOLUTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["SUPERSEDE_A", "SUPERSEDE_B", "MERGE", "KEEP_BOTH", "REMOVE_A", "REMOVE_B"],
            "description": "Resolution action to take",
        },
        "confidence": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
            "description": "Confidence in this resolution (0.0 to 1.0)",
        },
        "reason": {
            "type": "string",
            "description": "Brief explanation for the resolution",
        },
        "merged_content": {
            "type": "string",
            "description": "Merged fact content (only required if action is MERGE)",
        },
    },
    "required": ["action", "confidence", "reason"],
}


# F027: Tool-use structured output schema for cluster consolidation
_CLUSTER_MERGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "merged_content": {
            "type": "string",
            "description": "Single consolidated fact merging all input facts",
        },
        "confidence": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
            "description": "Confidence in the merged fact (0.0 to 1.0)",
        },
    },
    "required": ["merged_content", "confidence"],
}


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
        self._rubric_evolver = None  # F024-3b: Set externally if enabled
        # F035.1: Observability tracking
        self._total_sleeps: int = 0
        self._last_sleep_at: datetime | None = None
        self._last_phases: list[str] = []
        self._currently_sleeping: bool = False

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

    def get_stats(self) -> dict:
        """F035.1: Return sleep handler statistics."""
        return {
            "total_sleeps": self._total_sleeps,
            "last_sleep_at": self._last_sleep_at.isoformat() if self._last_sleep_at else None,
            "last_phases_completed": self._last_phases,
            "currently_sleeping": self._currently_sleeping,
        }

    async def _run_sleep(self, event: Event) -> None:
        """Actual sleep work — runs as independent task, NOT blocking bus."""
        self._sleeping = True
        self._interrupted = False
        self._currently_sleeping = True
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
                success = await self._phase_resolve_contradictions(sleep_stats)
                if success:
                    phases_completed.append("resolve_contradictions")

            if not self._interrupted:
                success = await self._phase_stale_scan(sleep_stats)
                if success:
                    phases_completed.append("stale_scan")

            if not self._interrupted:
                success = await self._phase_cluster_consolidation(sleep_stats)
                if success:
                    phases_completed.append("cluster_consolidation")

            if not self._interrupted:
                success = await self._phase_generalize(sleep_stats)
                if success:
                    phases_completed.append("generalize")

            if not self._interrupted:
                success = await self._phase_evolve_rubric(sleep_stats)
                if success:
                    phases_completed.append("evolve_rubric")

            await self._bus.emit(Event(
                type="sleep_completed",
                agent_id=event.agent_id,
                data={
                    "phases_completed": phases_completed,
                    "interrupted": self._interrupted,
                    "modifies": "memory",
                    **sleep_stats,
                },
                trace_id=event.trace_id,       # F035.2: inherit from parent
                caused_by=event.event_id,      # F035.2: point to parent
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
            self._currently_sleeping = False
            self._sleep_task = None
            self._total_sleeps += 1
            self._last_sleep_at = datetime.now(UTC)
            self._last_phases = phases_completed

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
        """Phase 4: Cross-session reflection on recent activity with orient context."""
        if not self._llm:
            return True
        try:
            recent = await self._heart.list_episodes(limit=10)
            if not recent or len(recent) < 2:
                logger.debug("Not enough recent episodes for reflection")
                return True

            episodes_text = "\n\n".join(
                f"- {ep.summary[:200]}" for ep in recent if ep.summary
            )
            if not episodes_text:
                return True

            # F031: Orient — gather existing facts related to episode topics
            orient_context = await self._build_orient_context(episodes_text)

            prompt = _REFLECTION_PROMPT.format(
                episodes=episodes_text,
                orient_context=orient_context,
            )

            reflection = await call_background_llm_structured(
                client=self._llm,
                model=self._settings.background_model,
                system_prompt="You are an AI agent reflecting on your recent activity.",
                user_message=prompt,
                tool_name="store_reflection",
                tool_description="Store the structured sleep reflection output. Call this with all reflection results.",
                output_schema=_REFLECTION_SCHEMA,
                max_tokens=1500,
            )

            if not reflection:
                return True

            # Store structured facts from LLM (preferred path)
            structured_facts = reflection.get("facts", [])
            stored = 0
            for fact in structured_facts[:5]:
                if self._interrupted:
                    break
                if isinstance(fact, dict) and fact.get("content"):
                    subject = fact.get("subject", "reflection")

                    # F031: UPDATES prefix — supersede existing fact (case-insensitive)
                    if isinstance(subject, str) and subject.upper().startswith("UPDATES:"):
                        updated = await self._handle_updates_prefix(
                            subject, fact, sleep_stats
                        )
                        if updated:
                            stored += 1
                        continue

                    result = await self._heart.learn(FactInput(
                        subject=subject,
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

            # Issue #233 fix: 0 stored from non-empty reflection is anomalous
            patterns = reflection.get("patterns", [])
            lessons = reflection.get("lessons", [])
            log_fn = logger.warning if stored == 0 and (patterns or lessons or structured_facts) else logger.info
            log_fn(
                "Reflection complete: %d patterns, %d lessons, %d facts stored",
                len(patterns),
                len(lessons),
                stored,
            )
            return True

        except Exception:
            logger.warning("Reflection phase failed", exc_info=True)
            return False

    async def _build_orient_context(self, episodes_text: str) -> str:
        """F031: Build orient context by searching for existing facts related to episodes.

        Uses truncated episode summaries as search queries for better semantic matching.
        """
        # Use episode summaries as search queries (max 5, truncated to 100 chars)
        queries = []
        for line in episodes_text.split("\n"):
            line = line.strip().lstrip("- ").strip()
            if line and len(line) > 10:
                queries.append(line[:100])
            if len(queries) >= 5:
                break

        if not queries:
            return ""

        existing_facts: dict = {}  # id -> fact, dedup by ID
        for query in queries:
            try:
                results = await self._heart.search_facts(query, limit=5)
                for f in results:
                    existing_facts[f.id] = f
            except Exception:
                logger.debug("Orient search failed for query: %s", query[:50])

        if not existing_facts:
            logger.info("F031 orient: no existing facts found for %d queries", len(queries))
            return ""

        # Format as orient context (max 20 facts)
        facts_list = list(existing_facts.values())[:20]
        logger.info("F031 orient: injecting %d existing facts from %d queries", len(facts_list), len(queries))
        facts_text = "\n".join(
            f"- [{f.category or 'unknown'}] {f.content}" for f in facts_list
        )
        return (
            f"\nEXISTING KNOWLEDGE (do NOT re-extract these — only extract genuinely NEW information):\n"
            f"{facts_text}\n\n"
            f"If you discover information that UPDATES or CONTRADICTS an existing fact above,\n"
            f'include it with a note: "UPDATES: <existing fact content>" in the fact\'s subject field.\n'
        )

    async def _handle_updates_prefix(
        self, subject: str, fact: dict, sleep_stats: dict
    ) -> bool:
        """F031: Handle UPDATES: prefix — find and supersede the referenced fact.

        Case-insensitive prefix detection. Requires similarity >0.80 to prevent
        wrong-fact supersession (review fix from devil's advocate P0-1).
        """
        referenced_content = subject[len("UPDATES:"):].strip()
        if not referenced_content:
            return False

        try:
            results = await self._heart.search_facts(referenced_content, limit=3)
            if not results:
                logger.debug("UPDATES: no matching fact found for '%s'", referenced_content[:50])
                # Fall back to learning as new fact
                result = await self._heart.learn(FactInput(
                    subject=referenced_content,
                    content=fact["content"],
                    source="sleep_reflection",
                    confidence=0.8,
                    category=fact.get("category", "concept"),
                ))
                if not isinstance(result, FactRejected):
                    sleep_stats["facts_created"] += 1
                    return True
                return False

            # Check similarity threshold before superseding (review fix P0-1)
            best_match = results[0]
            if hasattr(best_match, 'score') and best_match.score is not None and best_match.score < 0.80:
                logger.debug(
                    "UPDATES: best match score %.2f below threshold 0.80, learning as new fact",
                    best_match.score,
                )
                result = await self._heart.learn(FactInput(
                    subject=referenced_content,
                    content=fact["content"],
                    source="sleep_reflection",
                    confidence=0.8,
                    category=fact.get("category", "concept"),
                ))
                if not isinstance(result, FactRejected):
                    sleep_stats["facts_created"] += 1
                    return True
                return False

            await self._heart.supersede_fact(
                best_match.id,
                FactInput(
                    subject=best_match.subject or referenced_content,
                    content=fact["content"],
                    source="sleep_reflection",
                    confidence=0.8,
                    category=fact.get("category", best_match.category or "concept"),
                ),
            )
            sleep_stats["facts_created"] += 1
            logger.info("F031 orient: superseded fact %s with updated content", best_match.id)
            return True
        except Exception:
            logger.warning("UPDATES prefix handling failed", exc_info=True)
            return False

    async def _phase_resolve_contradictions(self, sleep_stats: dict) -> bool:
        """Phase 4.5: Find and resolve contradictory facts (F031)."""
        if not self._llm:
            return True
        try:
            candidates = await self._heart.find_contradiction_candidates(limit=10)
            if not candidates:
                logger.debug("No contradiction candidates found")
                return True

            sleep_stats["contradictions_found"] = len(candidates)
            sleep_stats["contradictions_resolved"] = 0

            for pair in candidates:
                if self._interrupted:
                    break

                prompt = _CONTRADICTION_RESOLUTION_PROMPT.format(
                    date_a=str(pair["date1"])[:10],
                    date_b=str(pair["date2"])[:10],
                    content_a=pair["content1"][:500],
                    content_b=pair["content2"][:500],
                )

                resolution = await call_background_llm_structured(
                    client=self._llm,
                    model=self._settings.background_model,
                    system_prompt="You are a memory management system resolving contradictory facts.",
                    user_message=prompt,
                    tool_name="resolve_contradiction",
                    tool_description="Resolve a contradiction between two facts in memory.",
                    output_schema=_CONTRADICTION_RESOLUTION_SCHEMA,
                    max_tokens=300,
                )

                if not resolution:
                    continue

                action = str(resolution.get("action", "")).upper().strip()
                confidence = float(resolution.get("confidence", 0.0))
                fact1_id = pair["fact1_id"]
                fact2_id = pair["fact2_id"]

                # Skip low-confidence actions (review fix: treat as KEEP_BOTH)
                if confidence < 0.7 and action != "KEEP_BOTH":
                    logger.info(
                        "F031 resolve: confidence %.2f below 0.7 for %s, downgrading to KEEP_BOTH",
                        confidence, action,
                    )
                    action = "KEEP_BOTH"

                try:
                    if action == "SUPERSEDE_A":
                        # Deactivate the loser — winner (fact2) already exists and is active
                        await self._heart.deactivate_fact(fact1_id)
                        sleep_stats["contradictions_resolved"] += 1
                        logger.info(
                            "F031 resolve: %s — deactivated %s, kept %s (%.2f confidence)",
                            action, fact1_id, fact2_id, confidence,
                        )
                    elif action == "SUPERSEDE_B":
                        await self._heart.deactivate_fact(fact2_id)
                        sleep_stats["contradictions_resolved"] += 1
                        logger.info(
                            "F031 resolve: %s — deactivated %s, kept %s (%.2f confidence)",
                            action, fact2_id, fact1_id, confidence,
                        )
                    elif action == "MERGE":
                        merged = resolution.get("merged_content", "")
                        if merged:
                            try:
                                await self._heart.learn(FactInput(
                                    subject=pair.get("subject", None),
                                    content=merged,
                                    source="contradiction_resolution",
                                    confidence=0.8,
                                    category=pair.get("category", None),
                                ))
                                await self._heart.deactivate_fact(fact1_id)
                                await self._heart.deactivate_fact(fact2_id)
                                sleep_stats["contradictions_resolved"] += 1
                                sleep_stats["facts_created"] += 1
                                logger.info(
                                    "F031 resolve: MERGE — combined %s + %s into new fact (%.2f confidence)",
                                    fact1_id, fact2_id, confidence,
                                )
                            except Exception:
                                logger.warning(
                                    "MERGE partially failed for %s/%s",
                                    fact1_id, fact2_id, exc_info=True,
                                )
                    elif action == "REMOVE_A":
                        await self._heart.deactivate_fact(fact1_id)
                        sleep_stats["contradictions_resolved"] += 1
                        logger.info("F031 resolve: REMOVE_A — deactivated %s (%.2f confidence)", fact1_id, confidence)
                    elif action == "REMOVE_B":
                        await self._heart.deactivate_fact(fact2_id)
                        sleep_stats["contradictions_resolved"] += 1
                        logger.info("F031 resolve: REMOVE_B — deactivated %s (%.2f confidence)", fact2_id, confidence)
                    elif action == "KEEP_BOTH":
                        logger.info(
                            "F031 resolve: KEEP_BOTH — %s and %s: %s",
                            fact1_id, fact2_id, resolution.get("reason", ""),
                        )
                    else:
                        logger.warning("Unknown resolution action: %s", action)
                except Exception:
                    logger.warning(
                        "Failed to execute resolution %s for %s/%s",
                        action, fact1_id, fact2_id, exc_info=True,
                    )

            logger.info(
                "Contradiction resolution: %d found, %d resolved",
                sleep_stats.get("contradictions_found", 0),
                sleep_stats.get("contradictions_resolved", 0),
            )
            return True

        except Exception:
            logger.warning("Contradiction resolution phase failed", exc_info=True)
            return False

    async def _phase_stale_scan(self, sleep_stats: dict) -> bool:
        """F027: Deactivate superseded facts that are stale and low-confidence."""
        try:
            async with self._heart.db.session() as session:
                cutoff = datetime.now(UTC) - timedelta(days=30)
                stmt = (
                    select(Fact)
                    .where(
                        Fact.agent_id == self._heart.agent_id,
                        Fact.active == True,  # noqa: E712
                        Fact.superseded_by.isnot(None),
                        Fact.confidence < 0.5,
                    )
                    .where(
                        (Fact.last_recalled_at.is_(None))
                        | (Fact.last_recalled_at < cutoff)
                    )
                )
                result = await session.execute(stmt)
                stale_facts = result.scalars().all()

                count = 0
                for fact in stale_facts:
                    fact.active = False
                    count += 1

                if count > 0:
                    await session.commit()

                sleep_stats["stale_deactivated"] = count
                logger.info("F027 stale scan: deactivated %d stale superseded facts", count)
            return True
        except Exception:
            logger.warning("F027 stale scan phase failed", exc_info=True)
            return False

    async def _phase_cluster_consolidation(self, sleep_stats: dict) -> bool:
        """F027: Merge clusters of 3+ active facts about the same subject."""
        if not self._llm:
            return True
        try:
            async with self._heart.db.session() as session:
                # Find subjects with 3+ active facts
                stmt = (
                    select(Fact.subject, func.count().label("cnt"))
                    .where(
                        Fact.agent_id == self._heart.agent_id,
                        Fact.active == True,  # noqa: E712
                        Fact.subject.isnot(None),
                    )
                    .group_by(Fact.subject)
                    .having(func.count() >= 3)
                    .order_by(func.count().desc())
                    .limit(5)
                )
                result = await session.execute(stmt)
                clusters = result.all()

            if not clusters:
                logger.debug("F027 cluster consolidation: no clusters found")
                sleep_stats["clusters_merged"] = 0
                return True

            merged_count = 0
            for subject, _count in clusters:
                if self._interrupted:
                    break

                # Fetch facts for this subject
                async with self._heart.db.session() as session:
                    fact_result = await session.execute(
                        select(Fact)
                        .where(
                            Fact.agent_id == self._heart.agent_id,
                            Fact.active == True,  # noqa: E712
                            Fact.subject == subject,
                        )
                        .order_by(Fact.created_at.desc())
                    )
                    facts = fact_result.scalars().all()

                if len(facts) < 3:
                    continue

                facts_text = "\n".join(
                    f"- [{f.category or 'unknown'}] {f.content}" for f in facts
                )

                merge_result = await call_background_llm_structured(
                    client=self._llm,
                    model=self._settings.background_model,
                    system_prompt="You are a memory consolidation system. Merge related facts into one.",
                    user_message=(
                        f"Subject: {subject}\n\n"
                        f"Facts to merge:\n{facts_text}\n\n"
                        f"Create a single consolidated fact that captures all information."
                    ),
                    tool_name="merge_facts",
                    tool_description="Return a single merged fact combining all input facts.",
                    output_schema=_CLUSTER_MERGE_SCHEMA,
                    max_tokens=500,
                )

                if not merge_result or not merge_result.get("merged_content"):
                    continue

                # Create merged fact
                merged_detail = await self._heart.learn(FactInput(
                    subject=subject,
                    content=merge_result["merged_content"],
                    source="cluster_consolidation",
                    confidence=float(merge_result.get("confidence", 0.8)),
                    category=facts[0].category,
                ))

                if isinstance(merged_detail, FactRejected):
                    continue

                # Deactivate originals
                async with self._heart.db.session() as session:
                    for fact in facts:
                        orm_fact = await session.get(Fact, fact.id)
                        if orm_fact:
                            orm_fact.superseded_by = merged_detail.id
                            orm_fact.active = False
                    await session.commit()

                merged_count += 1
                sleep_stats.setdefault("facts_created", 0)
                sleep_stats["facts_created"] += 1

            sleep_stats["clusters_merged"] = merged_count
            logger.info("F027 cluster consolidation: merged %d clusters", merged_count)
            return True

        except Exception:
            logger.warning("F027 cluster consolidation phase failed", exc_info=True)
            return False

    async def _phase_generalize(self, sleep_stats: dict) -> bool:
        """Phase 5: K-line learning — auto-create procedures from patterns."""
        if self._procedure_learner:
            try:
                stats = await self._procedure_learner.run_sleep_learning()
                sleep_stats["procedures_created"] += stats.get("decisions_learned", 0) + stats.get("episodes_learned", 0)
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

    async def _phase_evolve_rubric(self, sleep_stats: dict) -> bool:
        """Phase 6: Rubric evolution — adjust weights based on outcome correlations."""
        if self._rubric_evolver:
            try:
                report = await self._rubric_evolver.run_evolution_cycle()
                if report and report.suggested_weights:
                    sleep_stats["rubric_evolved"] = True
                    logger.info("Sleep rubric evolution: new weights suggested")
                else:
                    sleep_stats["rubric_evolved"] = False
                    logger.debug("Sleep rubric evolution: no changes")
                return True
            except Exception:
                logger.warning("Rubric evolution phase failed", exc_info=True)
                return False
        else:
            logger.debug("Sleep phase: rubric evolution (no evolver configured)")
            return True
