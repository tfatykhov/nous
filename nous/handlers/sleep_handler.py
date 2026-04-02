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
{orient_context}
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

Return ONLY valid JSON:
{{
  "action": "<ACTION>",
  "confidence": <0.0 to 1.0>,
  "reason": "<brief explanation>",
  "merged_content": "<only if action is MERGE>"
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
        self._rubric_evolver = None  # F024-3b: Set externally if enabled

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
                success = await self._phase_resolve_contradictions(sleep_stats)
                if success:
                    phases_completed.append("resolve_contradictions")

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

            text = await call_background_llm(
                self._llm,
                model=self._settings.background_model,
                system_prompt="You are an AI agent reflecting on your recent activity.",
                user_message=prompt,
                max_tokens=1500,
            )

            if not text:
                return True

            reflection = parse_llm_json(text)
            if isinstance(reflection, list):
                logger.warning(
                    "Reflection LLM returned list instead of dict (len=%d), "
                    "treating as facts array. Raw response (%d chars):\n%s",
                    len(reflection), len(text), text,
                )
                reflection = {"facts": reflection}
            elif not isinstance(reflection, dict):
                logger.warning(
                    "Reflection LLM returned %s instead of dict, skipping. "
                    "Raw response (%d chars):\n%s",
                    type(reflection).__name__, len(text), text,
                )
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

                text = await call_background_llm(
                    self._llm,
                    model=self._settings.background_model,
                    system_prompt="You are a memory management system resolving contradictory facts.",
                    user_message=prompt,
                    max_tokens=300,
                )

                if not text:
                    continue

                try:
                    resolution = parse_llm_json(text)
                except Exception:
                    logger.warning("Failed to parse contradiction resolution response")
                    continue

                if not isinstance(resolution, dict):
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
