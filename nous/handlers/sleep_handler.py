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

# F031 SAFETY DOWNGRADE SEMANTICS — single source of truth.
#
# Used both by ``_phase_resolve_contradictions`` (the actual downgrade
# logic) and by ``nous_eval/probes/sleep_action_audit.py`` (which
# formats this into the F031 judge rubric so the judge correctly
# scores downgraded actions as intentional safety, not mistakes).
#
# Keep the floor + the downgrade matrix in sync with the corresponding
# ``if`` branches in ``_phase_resolve_contradictions`` below. A test in
# ``tests/test_sleep_action_audit_probe.py`` asserts the audit rubric
# references this constant so drift fails loud.
F031_SAFETY_FLOOR_DESCRIPTION = (
    "The agent has a safety floor: any action with confidence below "
    "0.7 OR a MERGE without merged_content is automatically downgraded "
    "to KEEP_BOTH before being applied. This is INTENTIONAL safety "
    "behavior, not a bug — KEEP_BOTH preserves both facts and is "
    "reversible. When you see 'applied=KEEP_BOTH (downgraded from "
    "MERGE)', evaluate the APPLIED action (KEEP_BOTH) — i.e., would "
    "keeping both facts be acceptable here? — not the raw model output."
)


# F031 prompt rewritten 2026-04-30 after the synthetic eval
# (reports/f031_resolution_eval.md) measured 33% accuracy and a strong
# directional bias (SUPERSEDE_A: 12/30 verdicts, SUPERSEDE_B: 1/30)
# plus 0/10 correct on REMOVE_A. Same pattern that fixed F027 in PR #383:
# explicit decision tree, mutability framing, examples, and a clear
# distinction between REMOVE (factually wrong) and SUPERSEDE (mutable
# state changed over time).
_CONTRADICTION_RESOLUTION_PROMPT = """Two facts about the same subject exist in memory. Apply this decision tree IN ORDER and return the FIRST matching action.

Fact A (stored {date_a}): {content_a}
Fact B (stored {date_b}): {content_b}

Step 1 — Subject overlap test. Are A and B about the SAME subject and SAME aspect/property?
- If they describe DIFFERENT subjects or different unrelated aspects → return KEEP_BOTH (no contradiction).

Step 2 — Compatibility test. Can A and B both be simultaneously true?
- Both describe COMPLEMENTARY aspects (different facets of the same subject) → return KEEP_BOTH.
- Both partial truths that combine into a richer single fact → return MERGE (provide merged_content).

Step 3 — Factual correctness test. Was either fact OBJECTIVELY WRONG at the time of writing?
- Fact A was wrong (factual error, not state change) → return REMOVE_A.
- Fact B was wrong (factual error, not state change) → return REMOVE_B.
- Note: REMOVE means the fact was never accurate. Do not use REMOVE for mutable-state changes — those are SUPERSEDE.

Step 4 — Temporal-update test. Could time passing reconcile the two facts (a mutable property changed)?
- Fact B reflects the CURRENT state, A is now stale → return SUPERSEDE_A.
- Fact A reflects the CURRENT state, B is now stale → return SUPERSEDE_B.
- Mutable properties: schedule, status, value, count, version, location, configuration, role, ownership, price, quantity.

Examples to disambiguate:
- "Pi equals 3.14" vs "Pi equals 4" → REMOVE_B (math is fixed; 4 was never correct).
- "Tim's flight is at 3pm" vs "Tim's flight is at 5pm" → SUPERSEDE_A (schedule moved; A was correct earlier).
- "API returns 200" vs "API returns 500" → SUPERSEDE_A (status changes; both true at different times).
- "X uses Postgres" + "X uses Redis for cache" → KEEP_BOTH (different layers, both valid).
- "Project is 50% done" + "Project finished on schedule" → MERGE (combine into a single fact spanning the timeline).

For `confidence`: use 0.85+ when one action is clearly right, 0.70-0.85 when borderline, below 0.70 when genuinely uncertain (will be downgraded to KEEP_BOTH by the caller).

CRITICAL: When you choose MERGE, you MUST also produce a non-empty `merged_content` field with the combined single-fact text. If you cannot produce a meaningful merge (the two facts genuinely require both being kept), return KEEP_BOTH instead — never return MERGE with empty merged_content.

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
        "should_merge": {
            "type": "boolean",
            "description": (
                "True when the source facts cohere into one merged fact. "
                "False ONLY when merging would lose load-bearing "
                "distinctions (different entities sharing a name, "
                "irreconcilable contradictions). For chronological "
                "updates or multi-aspect descriptions of the same "
                "topic, set true even if some specifics are lost — "
                "source facts remain accessible via supersede chain."
            ),
        },
        "merged_content": {
            "type": "string",
            "description": (
                "Single consolidated fact (1-3 sentences) capturing "
                "what matters about this topic. Required when "
                "should_merge is true; ignored when false."
            ),
        },
        "refuse_reason": {
            "type": "string",
            "description": (
                "When should_merge is false, briefly explain what "
                "load-bearing information would be lost by merging. "
                "Required when should_merge is false."
            ),
        },
        "confidence": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
            "description": "Confidence in the verdict (0.0 to 1.0)",
        },
    },
    "required": ["should_merge", "confidence"],
}


_CLUSTER_MERGE_PROMPT = """You are consolidating a cluster of facts about the same subject in a long-running agent's memory.

Subject: {subject}

Facts in this cluster ({n_facts}):
{facts}

Apply this decision tree IN ORDER:

Step 1 — Coherence test. Are all facts about the SAME entity/topic?
- Same entity (just different aspects, time points, or detail levels) → continue to Step 2.
- Different entities sharing only a string match (e.g., two people named "Tim") → set should_merge=false with refuse_reason explaining the entity collision.

Step 2 — Merge feasibility test. Can the facts be summarized in 1-3 sentences that preserves what MATTERS about this topic?
- Yes → set should_merge=true and write merged_content. Loss of specific dates, exact numbers, or fine-grained detail is ACCEPTABLE — source facts remain accessible via the supersede chain.
- No (genuinely contradictory facts that can't be reconciled) → set should_merge=false with refuse_reason naming the contradiction.

Examples:

GOOD MERGE — chronological updates of same topic:
  Inputs:
    - "Repo at commit abc123 on Mon"
    - "Repo at commit def456 on Tue"
    - "Repo at commit ghi789 on Wed"
  → merged_content: "Repo state tracked daily; HEAD has advanced through abc123 (Mon) → def456 (Tue) → ghi789 (Wed)."

GOOD MERGE — multi-aspect description of same entity:
  Inputs:
    - "Project X uses Postgres for storage"
    - "Project X uses pgvector extension for embeddings"
    - "Project X uses HNSW indexes"
  → merged_content: "Project X uses Postgres with pgvector + HNSW indexes for embedding-based storage."

GOOD REFUSE — different entities:
  Inputs about subject "John":
    - "John is the lead engineer on the API team"
    - "John is a customer at Acme Corp"
    - "John released the v2.1 changelog"
  → should_merge=false, refuse_reason: "Three different people named John (engineer, customer, and likely a third entity); merging would conflate distinct identities."

GOOD REFUSE — irreconcilable contradiction:
  Inputs:
    - "Tim's flight is at 3pm"
    - "Tim's flight is at 5pm"
    - "Tim's flight was cancelled"
  → should_merge=false, refuse_reason: "Three contradictory states of Tim's flight; merging would erase the cancellation which is the load-bearing latest state."

Default lean: if you can produce ANY coherent merged_content even with detail loss, prefer should_merge=true over refuse. The supersede chain preserves the source facts — they remain reachable via `superseded_by` links if the merged version turns out wrong, so an over-eager merge is recoverable. A refused-but-mergeable cluster, by contrast, sits as bloat in memory forever.

Use the merge_facts tool to return your verdict."""


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
        # PR #407: bounded retention for fire-and-forget per-action
        # event emission tasks. Strong references prevent the GC from
        # collecting a task mid-flight; add_done_callback(discard)
        # cleans up after completion. Standard asyncio idiom.
        self._pending_emits: set[asyncio.Task] = set()
        self._procedure_learner = None  # F012: Set externally if enabled
        self._rubric_evolver = None  # F024-3b: Set externally if enabled
        self._graph_densifier = None  # F040: Set externally if enabled
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

    def _emit_action_event(self, event_type: str, data: dict) -> None:
        """Fire-and-forget per-action event for sleep-eval audit (PR #3).

        Mirrors the F031 ``f031_contradiction_resolution`` and the
        runner.py ``f026_action_gate`` patterns: ``asyncio.create_task``
        so the sleep loop never blocks on DB I/O, and a debug-level
        suppression on emission failure so persistence problems can't
        break a sleep cycle.

        Holds a strong reference to the task in ``_pending_emits`` until
        completion so the GC can't collect a still-running fire-and-
        forget task mid-flight. ``add_done_callback(discard)`` is the
        standard idiom for this pattern.
        """
        try:
            task = asyncio.create_task(
                self._brain.emit_event(event_type, data)
            )
            self._pending_emits.add(task)
            task.add_done_callback(self._pending_emits.discard)
        except Exception:
            logger.debug("%s persistence failed (suppressed)",
                         event_type, exc_info=True)

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
                success = await self._phase_graph_densification(sleep_stats)
                if success:
                    phases_completed.append("graph_densification")

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

                raw_action = str(resolution.get("action", "")).upper().strip()
                action = raw_action
                confidence = float(resolution.get("confidence", 0.0))
                merged_content = str(resolution.get("merged_content", "") or "").strip()
                fact1_id = pair["fact1_id"]
                fact2_id = pair["fact2_id"]

                # Codex P2 follow-up to #396: evaluate BOTH downgrade
                # conditions against `raw_action` so a low-confidence
                # MERGE-without-content correctly sets both flags. The
                # earlier version checked the missing-content condition
                # AFTER the floor mutation had already reset action to
                # KEEP_BOTH, so missing-content downgrades on
                # low-confidence verdicts were silently undercounted.
                downgraded_by_floor = (
                    confidence < 0.7 and raw_action != "KEEP_BOTH"
                )
                downgraded_due_to_missing_content = (
                    raw_action == "MERGE" and not merged_content
                )
                if downgraded_by_floor:
                    logger.info(
                        "F031 resolve: confidence %.2f below 0.7 for %s, downgrading to KEEP_BOTH",
                        confidence, raw_action,
                    )
                if downgraded_due_to_missing_content:
                    logger.warning(
                        "F031 resolve: MERGE returned without merged_content for %s/%s — downgrading to KEEP_BOTH",
                        fact1_id, fact2_id,
                    )
                if downgraded_by_floor or downgraded_due_to_missing_content:
                    action = "KEEP_BOTH"

                # F031 persistence — log every resolution decision so a
                # retrospective accuracy eval can run against real prod data
                # (eval_f031_resolution.py used synthetic fixtures only).
                # Fire-and-forget via create_task to keep the sleep loop fast.
                # 2026-05-01: include `merged_content_present` so the
                # MERGE-without-content failure mode is observable in prod.
                try:
                    asyncio.create_task(
                        self._brain.emit_event(
                            "f031_contradiction_resolution",
                            {
                                "raw_action": raw_action,
                                "applied_action": action,
                                "confidence": confidence,
                                "downgraded_by_floor": downgraded_by_floor,
                                "downgraded_due_to_missing_content": downgraded_due_to_missing_content,
                                "merged_content_present": bool(merged_content),
                                "fact1_id": str(fact1_id),
                                "fact2_id": str(fact2_id),
                                "reason": str(resolution.get("reason", ""))[:300],
                            },
                        )
                    )
                except Exception:
                    logger.debug("F031 persistence failed (suppressed)", exc_info=True)

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
                        # merged_content is guaranteed non-empty here (the
                        # downgrade-to-KEEP_BOTH guard above catches the empty
                        # case); but keep a defensive check just in case.
                        if merged_content:
                            try:
                                merged_detail = await self._heart.learn(
                                    FactInput(
                                        subject=pair.get("subject", None),
                                        content=merged_content,
                                        source="contradiction_resolution",
                                        confidence=0.8,
                                        category=pair.get("category", None),
                                    )
                                )
                                # PR #411 follow-up (Codex P1): F031
                                # MERGE used to discard the learn()
                                # result and call deactivate_fact() on
                                # both originals — leaving them
                                # active=False with NO superseded_by
                                # link. That orphaned every successful
                                # MERGE's source facts (chain broken,
                                # recoverable only via manual SQL).
                                # Mirror F027's pattern: capture the
                                # merged ID, set superseded_by AND
                                # active=False on both originals in a
                                # single transaction.
                                if isinstance(merged_detail, FactRejected):
                                    # contradiction_resolution is in
                                    # bypass_sources so this should not
                                    # happen, but defend in depth.
                                    logger.warning(
                                        "F031 MERGE: heart.learn rejected "
                                        "the merged fact unexpectedly: %s",
                                        merged_detail.explanation,
                                    )
                                else:
                                    async with self._heart.db.session() as session:
                                        for orig_id in (fact1_id, fact2_id):
                                            # Defensive: never set
                                            # superseded_by to own id.
                                            # Heart.learn always returns
                                            # a fresh UUID today, but the
                                            # check is one comparison and
                                            # removes a class of "what if
                                            # learn ever changes" bugs.
                                            if orig_id == merged_detail.id:
                                                continue
                                            orm_fact = await session.get(
                                                Fact, orig_id
                                            )
                                            if orm_fact:
                                                orm_fact.superseded_by = (
                                                    merged_detail.id
                                                )
                                                orm_fact.active = False
                                        await session.commit()
                                    sleep_stats["contradictions_resolved"] += 1
                                    sleep_stats["facts_created"] += 1
                                    logger.info(
                                        "F031 resolve: MERGE — combined %s + %s "
                                        "into %s (%.2f confidence)",
                                        fact1_id, fact2_id,
                                        merged_detail.id, confidence,
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
        """Deactivate facts that are old AND have not been recalled recently.

        Original filter combined ``active=true`` with
        ``superseded_by IS NOT NULL`` — that intersection is empty by
        design because the supersede flow sets both at the same time.
        Sleep cycle health monitor (PR #404) caught this: the phase
        produced 0 deactivations across 14 consecutive prod cycles.

        New filter targets the actual stale-fact failure mode: a fact
        that has aged past ``stale_scan_age_days`` (default 60) AND
        has either NEVER been recalled or wasn't recalled within the
        same age window. The OR on the recall side is load-bearing:
        a ``recall_count == 0`` only filter would permanently exempt
        facts recalled once years ago and never since — the same
        silent-failure pattern the original code had in reverse.

        The ``rule`` category is excluded by default since rules
        represent explicit user directives that may be infrequently
        exercised but still in force; deactivating them on recall
        stats alone is unsafe.
        """
        try:
            settings = self._heart.settings
            cutoff = datetime.now(UTC) - timedelta(
                days=settings.stale_scan_age_days
            )
            excluded = list(settings.stale_scan_excluded_categories or [])
            async with self._heart.db.session() as session:
                stmt = (
                    select(Fact)
                    .where(
                        Fact.agent_id == self._heart.agent_id,
                        Fact.active == True,  # noqa: E712
                        Fact.created_at < cutoff,
                    )
                    .where(
                        (Fact.last_recalled_at.is_(None))
                        | (Fact.last_recalled_at < cutoff)
                    )
                )
                if excluded:
                    # NULL NOT IN (...) evaluates to UNKNOWN in SQL,
                    # so a plain notin_ would silently exclude every
                    # uncategorized fact from deactivation. Add the
                    # NULL branch explicitly so the exclusion only
                    # skips the NAMED categories. Codex P2 on PR #405.
                    stmt = stmt.where(
                        Fact.category.is_(None)
                        | Fact.category.notin_(excluded)
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
                logger.info(
                    "Stale scan: deactivated %d facts older than %d days "
                    "with no recall in the same window "
                    "(excluded categories: %s)",
                    count, settings.stale_scan_age_days, excluded,
                )
            return True
        except Exception:
            logger.warning("stale_scan phase failed", exc_info=True)
            return False

    async def _phase_cluster_consolidation(self, sleep_stats: dict) -> bool:
        """F027: Merge clusters of MIN-MAX active facts about the same subject.

        Prior code picked top-5 by size, which always landed on
        accumulating subjects like ``lesson_learned`` (164 facts) and
        ``Tim`` (36 facts) — the LLM correctly refuses to collapse those
        into a single fact. Sleep cycle health monitor (PR #404) caught
        this: 14 consecutive prod cycles merged 0 clusters. Cap the
        cluster size so accumulating subjects are skipped and the
        actually-mergeable small clusters (3-10 facts) get a chance.
        """
        if not self._llm:
            return True
        try:
            settings = self._heart.settings
            min_facts = settings.cluster_consolidation_min_facts
            max_facts = settings.cluster_consolidation_max_facts
            async with self._heart.db.session() as session:
                # Find subjects with min_facts <= count <= max_facts active facts
                stmt = (
                    select(Fact.subject, func.count().label("cnt"))
                    .where(
                        Fact.agent_id == self._heart.agent_id,
                        Fact.active == True,  # noqa: E712
                        Fact.subject.isnot(None),
                    )
                    .group_by(Fact.subject)
                    .having(func.count() >= min_facts)
                    .having(func.count() <= max_facts)
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
                    system_prompt=(
                        "You are a memory consolidation expert. "
                        "Apply the decision tree carefully and prefer "
                        "merging when subjects clearly cohere."
                    ),
                    user_message=_CLUSTER_MERGE_PROMPT.format(
                        subject=subject,
                        n_facts=len(facts),
                        facts=facts_text,
                    ),
                    tool_name="merge_facts",
                    tool_description=(
                        "Return verdict on whether the cluster merges. "
                        "Set should_merge=true with merged_content for "
                        "coherent topics; should_merge=false with "
                        "refuse_reason for entity collisions or "
                        "irreconcilable contradictions."
                    ),
                    output_schema=_CLUSTER_MERGE_SCHEMA,
                    max_tokens=600,
                )

                # Per-action event for retrospective audit (sleep eval
                # PR #3). Emitted on EVERY merge attempt — including
                # LLM refusals — so the audit can distinguish
                # "phase fired but LLM said no" from "phase didn't fire."
                # Outcome categories:
                #   llm_refused: explicit should_merge=false with reason
                #   llm_malformed: should_merge=true but empty content
                #     (PR #410 review): distinct from refused so the
                #     audit doesn't conflate "LLM said no on purpose"
                #     with "LLM responded incoherently"
                #   rejected_by_admission: heart.learn returned FactRejected
                #   merged: full success (originals deactivated, new fact stored)
                merged_fact_id: str | None = None

                # Classify the LLM's response into the right refusal
                # bucket. Order matters: explicit should_merge=false is
                # the cleanest signal; missing-content-with-true is a
                # response-quality issue, not an intentional refusal.
                if not merge_result:
                    merge_outcome = "llm_malformed"
                elif merge_result.get("should_merge") is False:
                    merge_outcome = "llm_refused"
                elif not merge_result.get("merged_content"):
                    merge_outcome = "llm_malformed"
                else:
                    merge_outcome = None  # proceed to merge

                if merge_outcome is not None:
                    self._emit_action_event(
                        "f027_cluster_merge",
                        {
                            "subject": str(subject)[:200],
                            "source_fact_ids": [str(f.id) for f in facts],
                            "source_count": len(facts),
                            "merged_content": None,
                            "merged_fact_id": None,
                            "confidence": (
                                float(merge_result.get("confidence", 0.0))
                                if merge_result else None
                            ),
                            "refuse_reason": (
                                str(merge_result.get("refuse_reason", ""))[:300]
                                if merge_result else None
                            ),
                            "outcome": merge_outcome,
                        },
                    )
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
                    merge_outcome = "rejected_by_admission"
                    self._emit_action_event(
                        "f027_cluster_merge",
                        {
                            "subject": str(subject)[:200],
                            "source_fact_ids": [str(f.id) for f in facts],
                            "source_count": len(facts),
                            "merged_content": str(
                                merge_result.get("merged_content", "")
                            )[:500],
                            "merged_fact_id": None,
                            "confidence": float(
                                merge_result.get("confidence", 0.8)
                            ),
                            "refuse_reason": None,
                            "outcome": merge_outcome,
                        },
                    )
                    continue

                # Deactivate originals
                async with self._heart.db.session() as session:
                    for fact in facts:
                        # Defensive (mirrors F031 fix on PR #412): never
                        # set superseded_by to own id. Heart.learn always
                        # returns a fresh UUID today, but cheap to guard.
                        if fact.id == merged_detail.id:
                            continue
                        orm_fact = await session.get(Fact, fact.id)
                        if orm_fact:
                            orm_fact.superseded_by = merged_detail.id
                            orm_fact.active = False
                    await session.commit()

                merged_fact_id = str(merged_detail.id)
                merge_outcome = "merged"
                self._emit_action_event(
                    "f027_cluster_merge",
                    {
                        "subject": str(subject)[:200],
                        "source_fact_ids": [str(f.id) for f in facts],
                        "source_count": len(facts),
                        "merged_content": str(
                            merge_result.get("merged_content", "")
                        )[:500],
                        "merged_fact_id": merged_fact_id,
                        "confidence": float(
                            merge_result.get("confidence", 0.8)
                        ),
                        "refuse_reason": None,
                        "outcome": merge_outcome,
                    },
                )

                merged_count += 1
                sleep_stats.setdefault("facts_created", 0)
                sleep_stats["facts_created"] += 1

            sleep_stats["clusters_merged"] = merged_count
            logger.info("F027 cluster consolidation: merged %d clusters", merged_count)
            return True

        except Exception:
            logger.warning("F027 cluster consolidation phase failed", exc_info=True)
            return False

    async def _phase_graph_densification(self, sleep_stats: dict) -> bool:
        """F040 Phase: Connect orphan nodes to the knowledge graph."""
        if not self._settings.graph_backfill_enabled or not self._graph_densifier:
            return True
        try:
            self._graph_densifier._interrupted = self._interrupted
            result = await self._graph_densifier.run_backfill_cycle()
            # F043: pop CE stats BEFORE sum so they don't inflate edge totals.
            ce_stats = result.pop("_ce_stats", {"survived": 0, "pruned": 0})
            total_edges = sum(result.values())
            sleep_stats["orphan_edges_created"] = total_edges
            sleep_stats["ce_backfill_survived"] = ce_stats.get("survived", 0)
            sleep_stats["ce_backfill_pruned"] = ce_stats.get("pruned", 0)

            if not self._interrupted:
                self._graph_densifier._interrupted = self._interrupted
                bridges = await self._graph_densifier.discover_clusters(max_bridges=20)
                sleep_stats["bridge_edges_created"] = bridges

            logger.info(
                "F040 graph densification: %d backfill edges (CE survived=%d pruned=%d), %d bridge edges",
                total_edges,
                sleep_stats.get("ce_backfill_survived", 0),
                sleep_stats.get("ce_backfill_pruned", 0),
                sleep_stats.get("bridge_edges_created", 0),
            )
            return True
        except Exception:
            logger.warning("F040 graph densification phase failed", exc_info=True)
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
