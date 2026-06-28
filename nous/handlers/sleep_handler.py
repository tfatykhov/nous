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
from nous.handlers.consolidation_audit import ConsolidationAuditor, preview
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
        self._episode_summarizer = None  # F060: Set externally if enabled
        # F035.6: per-cycle consolidation auditor. Set at _run_sleep start when
        # settings.consolidation_audit_enabled; None otherwise (= no audit, no
        # behavior change). Phases read self._auditor directly.
        self._auditor: ConsolidationAuditor | None = None
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

    async def _run_audited_phase(self, label, op, coro_factory, sleep_stats, count_keys):
        """F035.6: run a phase and record ONE summary action from its mutation delta.

        Used for the graph/episode phases that delegate to other modules and only
        return aggregate counts (no per-mutation ids). The per-cycle ``totals`` on
        the envelope already carries the absolute counts; this summary row records
        *which phase* produced *which* counts (the F035.3 drift attribution).
        Per-edge/per-episode action rows are a scoped follow-up (Phase 1c). The
        fact-mutation phases (reflect/stale_scan/F031/F027) record per-action and
        are NOT wrapped here, so there is no double-counting.

        ``count_keys`` is the EXPLICIT allowlist of memory-mutation counters for
        this phase — diagnostic counters (``*_error``, ``*_skipped_*``) are never
        in it, so a cycle that only bumped an error counter records no action
        (codex P2). The delta is recorded regardless of the phase's final success
        flag, because a phase can commit part of its work and then return False
        (e.g. densification commits edges before a later cluster step fails) — the
        committed mutations must still appear in the changelog (codex P2). Bool
        flags (e.g. ``rubric_evolved``) record only on a False->True flip.
        """
        before = {k: sleep_stats.get(k, 0) for k in count_keys}
        success = await coro_factory()
        if self._auditor is not None:
            delta: dict = {}
            for k in count_keys:
                cur = sleep_stats.get(k, 0)
                prev = before.get(k, 0)
                if isinstance(cur, bool):
                    if cur and not prev:
                        delta[k] = True
                elif isinstance(cur, int) and (cur - (prev if isinstance(prev, int) else 0)) > 0:
                    delta[k] = cur - (prev if isinstance(prev, int) else 0)
            if delta:
                self._auditor.record(label, op, after={"counts": delta}, rationale=f"{label} phase summary")
        return success

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

        # F035.6: open the consolidation-audit envelope (default-off kill-switch).
        # When disabled, self._auditor stays None and every phase's audit guard
        # is a no-op — sleep behaves byte-for-byte as before.
        self._auditor = None
        if self._settings.consolidation_audit_enabled:
            try:
                # Use the CONFIGURED agent id (== settings.agent_id, the id the
                # dashboard queries), NOT event.agent_id — the scheduler emits
                # sleep_started with agent_id="system" (session_monitor.py:347),
                # which would hide scheduled-sleep audit rows from the dashboard.
                # parent_trace_id still links to the triggering event causally.
                self._auditor = ConsolidationAuditor(
                    self._heart.db,
                    self._heart.agent_id,
                    max_inflight=self._settings.consolidation_audit_max_inflight,
                    parent_trace_id=event.trace_id,
                )
                await self._auditor.open()
            except Exception:
                logger.warning("F035.6: auditor init failed; continuing without audit", exc_info=True)
                self._auditor = None
        audit_status = "completed"

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

            # F060: recover abandoned episodes (NULL structured_summary +
            # last activity > N hours). Runs BEFORE graph_densification so
            # F040 picks up the freshly-populated structured_summary instead
            # of falling through F058's plain-summary fallback.
            if not self._interrupted:
                success = await self._run_audited_phase(
                    "recover_episode", "recover",
                    lambda: self._phase_recover_abandoned_episodes(sleep_stats), sleep_stats,
                    ("episodes_recovered", "episodes_marked_abandoned"))
                if success:
                    phases_completed.append("recover_abandoned_episodes")

            if not self._interrupted:
                success = await self._run_audited_phase(
                    "graph_densify", "edge_add",
                    lambda: self._phase_graph_densification(sleep_stats), sleep_stats,
                    ("orphan_edges_created", "temporal_chain_edges", "comention_edges", "bridge_edges_created"))
                if success:
                    phases_completed.append("graph_densification")

            # F057: re-link active episodes that the live F022 linker
            # missed (sessions that never received episode_ended → no
            # summarizer trigger → no link). Runs BEFORE prune_dead_edges
            # so the new edges (active→active endpoints) won't be touched
            # by F053 on this cycle.
            if not self._interrupted:
                success = await self._run_audited_phase(
                    "relink_episode", "relink",
                    lambda: self._phase_relink_open_episodes(sleep_stats), sleep_stats,
                    ("episodes_relinked", "episode_relink_edges"))
                if success:
                    phases_completed.append("relink_open_episodes")

            # F044 tinyHippo-Lite v1: STC promotion gate + telemetry. Runs
            # after densification/relink (count this cycle's re-derivations)
            # and before dead-edge prune. No-op unless tinyhippo_lite_enabled.
            if not self._interrupted:
                success = await self._run_audited_phase(
                    "stc_consolidate", "consolidate",
                    lambda: self._phase_stc_consolidation(sleep_stats), sleep_stats,
                    # F044 per-cycle MUTATION counters only (UPDATE rowcounts):
                    # promotions (consolidation_state), recall-buffer ltp writes,
                    # and weight downscale. The f044_n_*/ltp_ge*/reinforced_24h
                    # keys are STATE/WINDOW snapshots, not this-cycle mutations —
                    # excluding them avoids recording a bogus 15k-edge "delta".
                    ("f044_promoted", "f044_recall_touches_flushed", "f044_downscaled"))
                if success:
                    phases_completed.append("stc_consolidation")

            if not self._interrupted:
                success = await self._run_audited_phase(
                    "prune_dead_edges", "edge_prune",
                    lambda: self._phase_prune_dead_edges(sleep_stats), sleep_stats,
                    ("dead_edges_pruned",))
                if success:
                    phases_completed.append("prune_dead_edges")

            # F065 Phase 2: prune old hub-rank snapshots so the
            # brain.graph_hub_snapshots table doesn't grow monotonically.
            # Disabled when retention_days == 0.
            if not self._interrupted:
                success = await self._phase_prune_hub_snapshots(sleep_stats)
                if success:
                    phases_completed.append("prune_hub_snapshots")

            if not self._interrupted:
                success = await self._run_audited_phase(
                    "generalize", "create_proc",
                    lambda: self._phase_generalize(sleep_stats), sleep_stats,
                    ("procedures_created",))
                if success:
                    phases_completed.append("generalize")

            if not self._interrupted:
                success = await self._run_audited_phase(
                    "evolve_rubric", "evolve",
                    lambda: self._phase_evolve_rubric(sleep_stats), sleep_stats,
                    ("rubric_evolved",))
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
            audit_status = "failed"
        finally:
            # F035.6: drain pending action batches, write the terminal envelope
            # (status + totals + phases), then sweep old action rows. The drain
            # inside close() guarantees no `completed` row is observable before
            # its action rows (A9). All failures are suppressed inside the
            # auditor — a broken audit can never break a sleep cycle.
            if self._auditor is not None:
                try:
                    await self._auditor.close(audit_status, phases_completed, sleep_stats)
                    await self._phase_prune_consolidation_actions(sleep_stats)
                except Exception:
                    logger.warning("F035.6: audit finalize failed (suppressed)", exc_info=True)
                finally:
                    self._auditor = None
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
                    if self._auditor is not None:
                        _fid = getattr(result, "id", None)
                        self._auditor.record(
                            "reflect", "learn",
                            target_ids=[_fid] if _fid else None,
                            after={"subject": subject, "content_preview": preview(fact["content"])},
                            rationale="sleep_reflection fact",
                        )

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
                        if self._auditor is not None:
                            _fid = getattr(result, "id", None)
                            self._auditor.record(
                                "reflect", "learn",
                                target_ids=[_fid] if _fid else None,
                                after={"subject": "daily_reflection", "content_preview": preview(reflection["summary"])},
                                rationale="sleep_reflection summary",
                            )

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
                    if self._auditor is not None:
                        _fid = getattr(result, "id", None)
                        self._auditor.record(
                            "reflect", "learn",
                            target_ids=[_fid] if _fid else None,
                            after={"subject": "lesson_learned", "content_preview": preview(lesson)},
                            rationale="sleep_reflection lesson",
                        )

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

    def _record_reflect_learn(self, result, subject: str, content: str, rationale: str) -> None:
        """F035.6: record a reflect-phase fact creation (guarded no-op when audit off)."""
        if self._auditor is None:
            return
        _fid = getattr(result, "id", None)
        self._auditor.record(
            "reflect", "learn",
            target_ids=[_fid] if _fid else None,
            after={"subject": subject, "content_preview": preview(content)},
            rationale=rationale,
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
            # Audit HD-2 (2026-06-09): raw-cosine probe, NOT search_facts.
            # search_facts returns rank-encoded RRF scores (top hit ~0.95 for
            # ANY query), so the score<0.80 guard below never fired and the top
            # RRF hit was always superseded — even when it was an unrelated
            # fact. find_similar_facts returns thresholdable cosine similarity.
            results = await self._heart.find_similar_facts(referenced_content, limit=3)
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
                    self._record_reflect_learn(result, referenced_content, fact["content"], "UPDATES: no match")
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
                    self._record_reflect_learn(result, referenced_content, fact["content"], "UPDATES: low score")
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
            if self._auditor is not None:
                self._auditor.record(
                    "reflect", "supersede",
                    target_ids=[best_match.id],
                    before={"content_preview": preview(getattr(best_match, "content", ""))},
                    after={"content_preview": preview(fact["content"])},
                    rationale=f"UPDATES: prefix (score {getattr(best_match, 'score', None)})",
                )
            return True
        except Exception:
            logger.warning("UPDATES prefix handling failed", exc_info=True)
            return False

    async def _apply_supersede(self, winner_id, loser_id) -> bool:
        """2a (2026-06-13 audit): deactivate the loser AND preserve the supersede
        chain — set ``loser.superseded_by = winner`` and write the supersedes
        edge — all in one session+commit, mirroring the F031 MERGE atomicity and
        clobber guard. The SUPERSEDE_A/B branches previously called
        ``deactivate_fact`` alone, severing lineage (no column, no edge).

        Returns ``True`` iff a real supersede was committed, ``False`` on either
        no-op guard (loser gone / already superseded by a concurrent path). The
        caller gates ``contradictions_resolved`` + the F035.6 audit row on this,
        so a raced no-op never reports a supersession that did not occur."""
        async with self._heart.db.session() as session:
            orm = await session.get(Fact, loser_id)
            if orm is None:
                return False
            # codex P1 (PR #520): if a concurrent path already superseded this
            # fact, skip EVERYTHING — writing a winner->loser edge now would
            # record a second, conflicting winner while the column still names
            # the original. Mirror the MERGE path: leave the existing chain
            # intact (the active flip is paired — already-superseded is inactive).
            if orm.superseded_by is not None:
                logger.debug(
                    "F031 supersede: skip %s — already superseded by %s",
                    loser_id, orm.superseded_by,
                )
                return False
            orm.superseded_by = winner_id
            orm.active = False
            await self._heart.link_facts(winner_id, loser_id, "supersedes", 1.0, session)
            await session.commit()
            return True

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
                    # 300 truncated the tool call: the model writes a ~500-char
                    # `reason` (schema-ordered before `merged_content`) which
                    # exhausted the budget, so MERGE verdicts arrived with empty
                    # merged_content and the safety floor downgraded them to
                    # KEEP_BOTH. Prod: 708/774 (91%) of intended merges silently
                    # failed this way, leaving complementary facts unconsolidated.
                    # Probe (scripts/diag/probe_merge_truncation.py) recovered
                    # merged_content on 6/6 of those pairs at 800 (observed usage
                    # ~400 tok: reason ~170 + merged_content ~205). Set to 1000
                    # for tail headroom — max_tokens is a ceiling, so the extra
                    # costs nothing unless a generation actually needs it.
                    max_tokens=1000,
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

                # F035.6: snapshot the resolved counter so we can emit the audit
                # row only AFTER a verdict's mutation actually commits (codex P2 —
                # a rolled-back apply must not leave a phantom audit row).
                _resolved_before = sleep_stats.get("contradictions_resolved", 0)

                try:
                    if action == "SUPERSEDE_A":
                        # loser=fact1, winner=fact2 (already active). 2a: preserve
                        # the chain (superseded_by + edge), not a bare deactivate.
                        # Gate the counter/audit on a real commit — a raced no-op
                        # supersede must not report a resolution (codex P2).
                        if await self._apply_supersede(winner_id=fact2_id, loser_id=fact1_id):
                            sleep_stats["contradictions_resolved"] += 1
                            logger.info(
                                "F031 resolve: %s — superseded %s by %s (%.2f confidence)",
                                action, fact1_id, fact2_id, confidence,
                            )
                    elif action == "SUPERSEDE_B":
                        # loser=fact2, winner=fact1 (inverted from SUPERSEDE_A).
                        if await self._apply_supersede(winner_id=fact1_id, loser_id=fact2_id):
                            sleep_stats["contradictions_resolved"] += 1
                            logger.info(
                                "F031 resolve: %s — superseded %s by %s (%.2f confidence)",
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
                                    ),
                                    # 1c: exclude the still-active sources so
                                    # Leg-2 dedup can't confirm the merged
                                    # restatement AS a source and discard the
                                    # merged content (MERGE-collapse).
                                    exclude_ids=[fact1_id, fact2_id],
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
                                            if orm_fact is None:
                                                continue
                                            # Codex P1 on PR #412: do not
                                            # clobber an already-populated
                                            # superseded_by. A concurrent
                                            # path may have superseded
                                            # this fact between candidate
                                            # selection and now; blindly
                                            # overwriting would lose the
                                            # original chain target. The
                                            # active flip is paired —
                                            # already-superseded facts
                                            # are already inactive.
                                            if orm_fact.superseded_by is not None:
                                                logger.debug(
                                                    "F031 MERGE: skip supersede "
                                                    "of %s — already linked to %s",
                                                    orig_id,
                                                    orm_fact.superseded_by,
                                                )
                                                continue
                                            orm_fact.superseded_by = (
                                                merged_detail.id
                                            )
                                            orm_fact.active = False
                                            # A: persist the supersedes graph
                                            # edge alongside the column so the
                                            # MERGE reaches the graph layer
                                            # (2026-06-13 audit: column-only
                                            # writes left 259 supersessions
                                            # invisible to densifier/dashboards).
                                            await self._heart.link_facts(
                                                merged_detail.id, orig_id,
                                                "supersedes", 1.0, session,
                                            )
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
                        # KEEP_BOTH leaves two ACTIVE contradictory facts. Writing
                        # a contradicts edge here is deferred to the eval-gated
                        # contradiction-semantics work (item C): (1) this branch
                        # also catches downgraded SUPERSEDE/REMOVE/MERGE verdicts
                        # (confidence < 0.7 or content-less MERGE), which must NOT
                        # be recorded as contradictions; (2) at prod's 0.75
                        # cross-type linking threshold the pair usually already
                        # has a positive related_to edge, and spreading
                        # activation only filters the contradicts row — so a bare
                        # contradicts edge leaves the facts still reinforcing.
                        # Both need consumer-side handling, designed + measured
                        # together with detection-broadening.
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

                # F035.6: emit the unified action only if the verdict actually
                # mutated (contradictions_resolved advanced). KEEP_BOTH/downgraded/
                # unknown and any failed-apply path leave the counter unchanged.
                if (
                    self._auditor is not None
                    and sleep_stats.get("contradictions_resolved", 0) > _resolved_before
                    and action in ("SUPERSEDE_A", "SUPERSEDE_B", "MERGE", "REMOVE_A", "REMOVE_B")
                ):
                    _op = {"SUPERSEDE_A": "supersede", "SUPERSEDE_B": "supersede",
                           "MERGE": "merge", "REMOVE_A": "deactivate",
                           "REMOVE_B": "deactivate"}[action]
                    self._auditor.record(
                        "f031_contradiction", _op,
                        target_ids=[fact1_id, fact2_id],
                        before=[{"id": str(fact1_id)}, {"id": str(fact2_id)}],
                        after=({"content_preview": preview(merged_content)} if action == "MERGE" and merged_content else None),
                        rationale=f"{action} (conf {confidence:.2f}): {str(resolution.get('reason', ''))[:160]}",
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
                # Buffer the (id, subject, content) of each deactivated fact and
                # only emit audit rows AFTER the commit succeeds — a rolled-back
                # transaction must not leave the audit reporting deactivations
                # that never landed (codex P2).
                _deactivated: list[tuple] = []
                for fact in stale_facts:
                    fact.active = False
                    count += 1
                    if self._auditor is not None:
                        _deactivated.append((fact.id, fact.subject, fact.content))

                if count > 0:
                    await session.commit()

                if self._auditor is not None:
                    for _fid, _subj, _content in _deactivated:
                        self._auditor.record(
                            "stale_scan", "deactivate",
                            target_ids=[_fid],
                            before={"subject": _subj, "content_preview": preview(_content)},
                            rationale=f"stale: aged > {settings.stale_scan_age_days}d, no recall in window",
                        )

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
                    # Align with the pairwise resolver (600 -> 1000): same
                    # truncation class, and a multi-fact cluster merge can need a
                    # longer merged_content than the pairwise case. Precautionary
                    # (cluster path not separately measured); the ceiling costs
                    # nothing unless hit.
                    max_tokens=1000,
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

                # Create merged fact. 1c: exclude the still-active cluster
                # members so Leg-2 dedup can't confirm the merged restatement
                # AS one of them and discard the merged content.
                merged_detail = await self._heart.learn(
                    FactInput(
                        subject=subject,
                        content=merge_result["merged_content"],
                        source="cluster_consolidation",
                        confidence=float(merge_result.get("confidence", 0.8)),
                        category=facts[0].category,
                    ),
                    exclude_ids=[f.id for f in facts],
                )

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
                        if orm_fact is None:
                            continue
                        # Codex P1 on PR #412 (mirror): do not clobber
                        # an already-populated superseded_by. A
                        # concurrent path may have superseded this fact
                        # between candidate selection and now.
                        if orm_fact.superseded_by is not None:
                            logger.debug(
                                "F027 cluster_merge: skip supersede of %s "
                                "— already linked to %s",
                                fact.id,
                                orm_fact.superseded_by,
                            )
                            continue
                        orm_fact.superseded_by = merged_detail.id
                        orm_fact.active = False
                        # Mirror the supersedes edge (same fix as the F031
                        # MERGE path) so cluster merges don't recreate the
                        # column/graph mismatch the backfill repairs.
                        await self._heart.link_facts(
                            merged_detail.id, fact.id, "supersedes", 1.0, session,
                        )
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

                if self._auditor is not None:
                    self._auditor.record(
                        "f027_consolidate", "merge",
                        target_ids=[f.id for f in facts] + [merged_detail.id],
                        before=[{"id": str(f.id), "content_preview": preview(f.content)} for f in facts],
                        after={"id": merged_fact_id, "content_preview": preview(merge_result.get("merged_content", ""))},
                        rationale=f"cluster merge of {len(facts)} facts on '{str(subject)[:80]}'",
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
            # F075: pop temporal-chain count too — `_happened_before` is a
            # sleep-cycle metric, not a per-entity orphan-backfill edge.
            # Codex PR #461 round 3 fix: the leading-underscore convention
            # in graph_densifier's return dict must be honored by the caller
            # aggregation, not just by graph_densifier's internal logging.
            happened_before = result.pop("_happened_before", 0)
            # F076: pop co-mention count too — it's a sleep-cycle associative-edge
            # metric, not a per-entity orphan-backfill edge (same convention).
            comention = result.pop("_co_mention", 0)
            total_edges = sum(result.values())
            sleep_stats["orphan_edges_created"] = total_edges
            sleep_stats["temporal_chain_edges"] = happened_before
            sleep_stats["comention_edges"] = comention
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

    async def _phase_stc_consolidation(self, sleep_stats: dict) -> bool:
        """F044 tinyHippo-Lite v1: STC promotion gate + telemetry (flag-gated).

        Runs after F040 densification (so freshly re-derived edges are counted)
        and before F053 dead-edge prune. Promotes tagged edges whose ltp_count
        reached the PRP threshold and records the reinforcement distribution to
        sleep_stats. No weight change, no deletion — telemetry-only v1.
        """
        if not self._settings.tinyhippo_lite_enabled:
            return True
        try:
            from nous.brain.tinyhippo_lite import run_stc_consolidation
            stats = await run_stc_consolidation(
                self._heart.db,
                self._settings.agent_id,
                self._settings.tinyhippo_prp_threshold,
            )
            sleep_stats.update(stats)
            # F044 Phase 8d (spec): homeostatic α-downscale of TAGGED edge
            # weights (consolidated exempt). Runs AFTER promotion (8c) so
            # freshly-promoted edges aren't penalized this cycle. Opt-in
            # (default off) — the telemetry-only v1 leaves weights untouched.
            if getattr(self._settings, "tinyhippo_downscale_enabled", False):
                from nous.brain.tinyhippo_lite import homeostatic_downscale
                async with self._heart.db.session() as sess:
                    n_down = await homeostatic_downscale(
                        sess, self._settings.agent_id, self._settings.tinyhippo_alpha
                    )
                    await sess.commit()
                sleep_stats["f044_downscaled"] = n_down
                logger.info(
                    "F044 Phase 8d: downscaled %d tagged edges by α=%.2f",
                    n_down, self._settings.tinyhippo_alpha,
                )
            logger.info(
                "F044 STC: promoted=%d tagged=%d consolidated=%d "
                "ltp>=1/2/3=%d/%d/%d reinforced_24h=%d",
                stats["f044_promoted"], stats["f044_n_tagged"],
                stats["f044_n_consolidated"], stats["f044_ltp_ge1"],
                stats["f044_ltp_ge2"], stats["f044_ltp_ge3"],
                stats["f044_reinforced_24h"],
            )
            return True
        except Exception:
            logger.warning("F044 STC consolidation phase failed", exc_info=True)
            return False

    async def _phase_prune_dead_edges(self, sleep_stats: dict) -> bool:
        """F053: Delete graph edges incident to inactive nodes.

        Spreading activation walks ``brain.graph_edges`` without an
        ``active`` filter (see ``brain/spreading_activation.py``), so
        edges to deactivated facts/episodes/procedures/decisions waste
        per-hop activation budget on dead nodes. The orphan supersede
        chain audit (PR #412) found this exposure surface; this phase
        is the periodic housekeeping pass that recovers wasted hops.

        Bounded by ``dead_edge_pruning_max_per_cycle`` to keep the
        exclusive DELETE lock short. The bound is intentional: a single
        sleep cycle should not block other writers for more than ~1s
        worth of pruning. If more orphans accumulate than the per-cycle
        bound, subsequent sleep cycles will continue draining.

        SQL strategy: build the inactive-id pool with one UNION ALL,
        then DELETE edges whose source OR target appears in that pool.
        Agent-scoped both on the edges side and the inactive-id side
        defense-in-depth — no chance of cross-agent edge deletion even
        if a row was misclassified at write time.
        """
        try:
            if not self._settings.dead_edge_pruning_enabled:
                return True
            agent_id = self._settings.agent_id
            # Defensive int() — keeps tests that mock self._settings as a
            # bare MagicMock from raising TypeError on the `<= 0` comparison.
            max_per_cycle = int(self._settings.dead_edge_pruning_max_per_cycle)
            if max_per_cycle <= 0:
                return True
            from sqlalchemy import text
            async with self._heart.db.session() as session:
                # `brain.decisions` has no `active` column today — decisions
                # are append-only and reviewed-not-deleted. If/when a soft-
                # delete is added, append a UNION ALL branch here. Including
                # it pre-emptively would crash every sleep cycle until the
                # column lands (review caught this).
                sql = text("""
                    WITH inactive_nodes AS (
                        SELECT id, 'fact'::text AS node_type
                        FROM heart.facts
                        WHERE agent_id = :agent_id AND active = false
                        UNION ALL
                        SELECT id, 'episode'
                        FROM heart.episodes
                        WHERE agent_id = :agent_id AND active = false
                        UNION ALL
                        SELECT id, 'procedure'
                        FROM heart.procedures
                        WHERE agent_id = :agent_id AND active = false
                    ),
                    victim_edges AS (
                        SELECT e.id
                        FROM brain.graph_edges e
                        WHERE e.agent_id = :agent_id
                          -- Preserve supersedes lineage: these edges point AT
                          -- the superseded (inactive) fact by design — that is
                          -- the record of the supersession. Pruning them undoes
                          -- the 2026-06-13 edge-persistence fix (the original is
                          -- marked inactive in the same cycle the edge is
                          -- written). recall stays safe because brain._neighbors
                          -- filters inactive facts out of expansion.
                          AND e.relation <> 'supersedes'
                          AND (
                            (e.source_type, e.source_id) IN (
                              SELECT node_type, id FROM inactive_nodes
                            )
                            OR (e.target_type, e.target_id) IN (
                              SELECT node_type, id FROM inactive_nodes
                            )
                          )
                        LIMIT :max_per_cycle
                    )
                    DELETE FROM brain.graph_edges
                    WHERE id IN (SELECT id FROM victim_edges)
                    RETURNING id
                """)
                result = await session.execute(
                    sql,
                    {"agent_id": agent_id, "max_per_cycle": max_per_cycle},
                )
                deleted = len(result.all())
                await session.commit()
            sleep_stats["dead_edges_pruned"] = deleted
            logger.info(
                "F053 dead-edge prune: deleted %d edges (cap=%d)",
                deleted, max_per_cycle,
            )
            return True
        except Exception as exc:
            # Surface error type into sleep_stats so observability dashboards
            # can alert on silent regressions (review P2). The phase still
            # returns False so it's excluded from phases_completed.
            sleep_stats["dead_edges_prune_error"] = type(exc).__name__
            logger.warning("F053 dead-edge prune failed", exc_info=True)
            return False

    async def _phase_prune_hub_snapshots(self, sleep_stats: dict) -> bool:
        """F065 Phase 2: prune old rows from brain.graph_hub_snapshots.

        The autosurface hook in pre_turn writes one row per (agent, hub)
        when a rank changes (and one row per first-sight). Over time
        this table grows monotonically — this phase prunes rows older
        than NOUS_GRAPH_HUB_SNAPSHOT_RETENTION_DAYS so the table stays
        bounded.

        Disabled when retention_days <= 0. Errors are caught and logged;
        the phase returns False but pre_turn / other sleep phases are
        unaffected.
        """
        try:
            retention_days = int(self._settings.graph_hub_snapshot_retention_days)
            if retention_days <= 0:
                return True
            from nous.brain.hub_snapshots import HubSnapshotManager

            agent_id = self._settings.agent_id
            # Reuse the heart DB pool — HubSnapshotManager only writes
            # to brain.graph_hub_snapshots, no schema-isolation issue.
            mgr = HubSnapshotManager(self._heart.db, agent_id)
            deleted = await mgr.prune_older_than(days=retention_days)
            sleep_stats["hub_snapshots_pruned"] = deleted
            if deleted:
                logger.info(
                    "F065: pruned %d hub-snapshot rows older than %d days",
                    deleted, retention_days,
                )
            return True
        except Exception as exc:
            sleep_stats["hub_snapshot_prune_error"] = type(exc).__name__
            logger.warning("F065 hub-snapshot prune failed", exc_info=True)
            return False

    async def _phase_prune_consolidation_actions(self, sleep_stats: dict) -> bool:
        """F035.6: retention sweep for consolidation_actions rows.

        Runs LAST, after the envelope close/drain, so it can only ever delete
        rows from *prior* cycles (never the current cycle's still-emitting rows).
        The per-night ``consolidation_cycles`` totals are retained indefinitely;
        only the verbose per-action rows are swept. Gated by
        ``consolidation_audit_retention_days`` (0 disables). Errors are caught and
        never abort the sleep cycle.
        """
        if self._auditor is None:
            return True
        try:
            days = int(self._settings.consolidation_audit_retention_days)
            deleted = await self._auditor.prune_old_actions(days)
            if deleted:
                sleep_stats["consolidation_actions_pruned"] = deleted
                logger.info(
                    "F035.6: pruned %d consolidation_action rows older than %d days",
                    deleted, days,
                )
            return True
        except Exception:
            logger.warning("F035.6 consolidation-action retention sweep failed", exc_info=True)
            return False

    async def _phase_recover_abandoned_episodes(self, sleep_stats: dict) -> bool:
        """F060: Populate structured_summary or mark abandoned for stuck episodes.

        Three sub-operations in one phase:
          - F060 base: full recovery from persisted transcript (F025 P3-C).
          - F060.1 fallback: when transcript is missing/short, fall back to
            the plain `summary` field as input. Degraded but better than
            leaving the row stuck forever. Prod audit (2026-05-05): 0/103
            stuck-open episodes had transcripts; 93/103 had plain summary.
          - F060.2 mark abandoned: rows with NO usable content AND age >
            `mark_age_days` get `active=false, outcome='abandoned'`. Single
            UPDATE — no LLM, separate bound (default 200/cycle).

        Investigation context: F058 found 76+ active episodes on prod had
        `structured_summary=NULL`. The cause is sessions that never reach
        `cognitive.end_session` (process restart before idle timeout). The
        episode_summarizer never fires, the structured fields stay null
        forever, and downstream consumers (F040 graph_densifier, fact
        extraction, search relevance) lose signal.

        Recovered episodes stay `active=true` (matching F057). Marked-
        abandoned episodes go `active=false` so search excludes them.
        """
        if not getattr(self._settings, "abandoned_recovery_enabled", True):
            return True
        summarizer = self._episode_summarizer
        if summarizer is None:
            # No summarizer wired — phase is inert. Don't fail the cycle.
            return True
        try:
            min_age = int(self._settings.abandoned_recovery_min_age_hours)
            max_per_cycle = int(self._settings.abandoned_recovery_max_per_cycle)
            min_transcript = int(
                self._settings.abandoned_recovery_min_transcript_chars
            )
            fallback_enabled = bool(getattr(
                self._settings,
                "abandoned_recovery_summary_fallback_enabled",
                True,
            ))
            min_summary = int(getattr(
                self._settings,
                "abandoned_recovery_min_summary_chars",
                20,
            ))
            mark_enabled = bool(getattr(
                self._settings,
                "abandoned_recovery_mark_abandoned_enabled",
                True,
            ))
            mark_age_days = int(getattr(
                self._settings,
                "abandoned_recovery_mark_age_days",
                7,
            ))
            mark_max = int(getattr(
                self._settings,
                "abandoned_recovery_mark_max_per_cycle",
                200,
            ))
            if max_per_cycle <= 0:
                return True
            agent_id = self._settings.agent_id

            from sqlalchemy import text as sql_text
            recovered_full = 0
            recovered_summary_only = 0
            skipped_no_data = 0
            marked_abandoned = 0
            errors = 0

            # Loop A — recovery (F060 base + F060.1 fallback). Bounded by
            # max_per_cycle because each row costs an LLM call.
            async with self._heart.db.session() as session:
                rows = await session.execute(sql_text("""
                    SELECT e.id, e.transcript, e.summary
                    FROM heart.episodes e
                    WHERE e.agent_id = :agent_id
                      AND e.active = true
                      AND e.structured_summary IS NULL
                      AND e.started_at < now() - make_interval(hours => :hours)
                    ORDER BY e.started_at ASC
                    LIMIT :lim
                """), {
                    "agent_id": agent_id,
                    "hours": min_age,
                    "lim": max_per_cycle,
                })
                candidates = rows.all()

            for ep_id, transcript, plain_summary in candidates:
                if self._interrupted:
                    break

                # Pick the best available input: full transcript, plain
                # summary fallback, or skip.
                source: str | None = None
                source_kind: str | None = None
                if transcript and len(transcript) >= min_transcript:
                    source = transcript
                    source_kind = "transcript"
                elif (
                    fallback_enabled
                    and plain_summary
                    and len(plain_summary) >= min_summary
                ):
                    source = plain_summary
                    source_kind = "summary"
                else:
                    skipped_no_data += 1
                    continue

                try:
                    summary = await summarizer.summarize_episode(
                        episode_id=ep_id,
                        transcript=source,
                        agent_id=agent_id,
                    )
                    if summary is not None:
                        if source_kind == "transcript":
                            recovered_full += 1
                        else:
                            recovered_summary_only += 1
                except Exception:
                    errors += 1
                    logger.warning(
                        "F060 summarize failed for episode %s",
                        ep_id, exc_info=True,
                    )

            # Loop B — F060.2 mark abandoned. Targets rows with no usable
            # content AND age >= mark_age_days. Cheap SQL UPDATE; can clear
            # a large legacy backlog quickly.
            if mark_enabled and not self._interrupted:
                async with self._heart.db.session() as session:
                    result = await session.execute(sql_text("""
                        UPDATE heart.episodes
                        SET active = false,
                            outcome = 'abandoned',
                            ended_at = COALESCE(ended_at, now())
                        WHERE id IN (
                          SELECT id FROM heart.episodes
                          WHERE agent_id = :agent_id
                            AND active = true
                            AND structured_summary IS NULL
                            AND started_at < now() - make_interval(days => :days)
                            AND (transcript IS NULL OR length(transcript) < :min_t)
                            AND (summary IS NULL OR length(summary) < :min_s)
                          ORDER BY started_at ASC
                          LIMIT :lim
                        )
                    """), {
                        "agent_id": agent_id,
                        "days": mark_age_days,
                        "min_t": min_transcript,
                        "min_s": min_summary,
                        "lim": mark_max,
                    })
                    marked_abandoned = result.rowcount or 0
                    await session.commit()

            sleep_stats["episodes_recovered"] = (
                recovered_full + recovered_summary_only
            )
            if recovered_full:
                sleep_stats["episodes_recovered_full_transcript"] = recovered_full
            if recovered_summary_only:
                sleep_stats["episodes_recovered_summary_only"] = (
                    recovered_summary_only
                )
            if marked_abandoned:
                sleep_stats["episodes_marked_abandoned"] = marked_abandoned
            if skipped_no_data:
                sleep_stats["abandoned_recovery_skipped_no_data"] = skipped_no_data
            if errors:
                sleep_stats["abandoned_recovery_errors"] = errors
            logger.info(
                "F060 recovery: %d full + %d summary-only recovered, "
                "%d marked abandoned, %d skipped (%d errors)",
                recovered_full,
                recovered_summary_only,
                marked_abandoned,
                skipped_no_data,
                errors,
            )
            return True
        except Exception as exc:
            sleep_stats["abandoned_recovery_phase_error"] = type(exc).__name__
            logger.warning(
                "F060 abandoned-episode recovery phase failed",
                exc_info=True,
            )
            return False

    async def _phase_relink_open_episodes(self, sleep_stats: dict) -> bool:
        """F057: Backfill F022 episode-graph edges the live linker missed.

        Investigation (2026-05-04 prod audit on nous-default): 99 of 102
        active episodes were graph orphans — 94 stuck-open sessions never
        received ``episode_ended`` (no summarizer trigger, no F022 link).
        Of those 99, 27 already have facts referencing them via
        ``source_episode_id`` — those should have been linked by the live
        path; this phase backfills them.

        Approach: find active episodes with no incident graph edges,
        started >= ``episode_relink_min_age_hours`` ago (skip recent —
        the live linker handles those). For each, look up linkable
        anchors (facts via source_episode_id, decisions via
        episode_decisions). If anchors exist, call
        ``graph_linker.link_episode_deterministic``. Bounded by
        ``episode_relink_max_per_cycle`` to keep the cycle short.

        Episodes stay ``active=true`` so they remain searchable
        (heart.episodes.search filters on active=true). Only the
        linker's outputs change. F053 does NOT prune the new edges
        because both endpoints are active=true.
        """
        if not getattr(self._settings, "episode_relink_enabled", True):
            return True
        try:
            min_age = int(self._settings.episode_relink_min_age_hours)
            max_per_cycle = int(self._settings.episode_relink_max_per_cycle)
            if max_per_cycle <= 0:
                return True
            agent_id = self._settings.agent_id

            # Without a graph_linker dependency, the phase is a no-op.
            # Episode summarizer wires _graph_linker; sleep handler
            # constructor doesn't, so we lazily import + instantiate here.
            graph_linker = getattr(self, "_graph_linker", None)
            if graph_linker is None:
                from nous.brain.graph_linker import GraphLinker
                graph_linker = GraphLinker(
                    self._heart.db,
                    self._heart._embeddings,
                    self._settings,
                    agent_id,
                )

            from sqlalchemy import text as sql_text
            relinked = 0
            edges_created = 0
            errors = 0
            async with self._heart.db.session() as session:
                # Find active orphan episodes older than min_age_hours.
                # An "orphan" here = no incident graph_edges row at all.
                # Only surface episodes that have at least one linkable
                # anchor (active fact via source_episode_id, or
                # episode_decisions row). Legacy pre-F022 episodes have
                # neither and would just be skipped inside the loop —
                # filtering at SQL keeps the LIMIT meaningful.
                rows = await session.execute(sql_text("""
                    SELECT e.id
                    FROM heart.episodes e
                    WHERE e.agent_id = :agent_id
                      AND e.active = true
                      AND e.started_at < now() - make_interval(hours => :hours)
                      AND NOT EXISTS (
                        SELECT 1 FROM brain.graph_edges ge
                        WHERE ge.agent_id = :agent_id
                          AND ((ge.source_type='episode' AND ge.source_id=e.id)
                            OR (ge.target_type='episode' AND ge.target_id=e.id))
                      )
                      AND (
                        EXISTS (
                          SELECT 1 FROM heart.facts f
                          WHERE f.agent_id = :agent_id
                            AND f.source_episode_id = e.id
                            AND f.active = true
                        )
                        OR EXISTS (
                          SELECT 1 FROM heart.episode_decisions ed
                          WHERE ed.episode_id = e.id
                        )
                      )
                    ORDER BY e.started_at ASC
                    LIMIT :lim
                """), {
                    "agent_id": agent_id,
                    "hours": min_age,
                    "lim": max_per_cycle,
                })
                ep_ids = [r[0] for r in rows.all()]

                for ep_id in ep_ids:
                    if self._interrupted:
                        break
                    # Anchors: facts referencing this episode + decisions linked
                    f_rows = await session.execute(sql_text(
                        "SELECT id FROM heart.facts "
                        "WHERE agent_id=:aid AND source_episode_id=:eid AND active=true"
                    ), {"aid": agent_id, "eid": ep_id})
                    fact_ids = [r[0] for r in f_rows.all()]
                    d_rows = await session.execute(sql_text(
                        "SELECT decision_id FROM heart.episode_decisions "
                        "WHERE episode_id=:eid"
                    ), {"eid": ep_id})
                    decision_ids = [r[0] for r in d_rows.all()]

                    if not fact_ids and not decision_ids:
                        continue  # no anchors — this episode needs semantic backfill, not deterministic
                    try:
                        new_edges = await graph_linker.link_episode_deterministic(
                            episode_id=ep_id,
                            decision_ids=decision_ids,
                            fact_ids=fact_ids,
                            session=session,
                        )
                        relinked += 1
                        edges_created += len(new_edges) if new_edges else 0
                    except Exception:
                        errors += 1
                        logger.warning(
                            "F057 relink failed for episode %s", ep_id,
                            exc_info=True,
                        )
                await session.commit()

            sleep_stats["episodes_relinked"] = relinked
            sleep_stats["episode_relink_edges"] = edges_created
            if errors:
                sleep_stats["episode_relink_errors"] = errors
            logger.info(
                "F057 episode relink: %d episodes, %d edges (%d errors)",
                relinked, edges_created, errors,
            )
            return True
        except Exception as exc:
            sleep_stats["episode_relink_phase_error"] = type(exc).__name__
            logger.warning("F057 episode relink phase failed", exc_info=True)
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
