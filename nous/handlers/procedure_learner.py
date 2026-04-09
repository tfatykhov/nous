"""ProcedureLearner — auto-learns procedures from decision patterns and episode lessons.

Two learning pathways:
  1. Decision clustering: Groups similar successful decisions into procedures.
  2. Episode lesson learning: Clusters lessons_learned from completed episodes.

Also performs weak procedure review (revise/retire underperforming auto-learned procedures).

Runs during sleep cycles via run_sleep_learning().
"""

from __future__ import annotations

import json
import logging
import math
from datetime import UTC, datetime, timedelta
from typing import Any

from nous.brain.brain import Brain
from nous.brain.embeddings import EmbeddingProvider
from nous.config import Settings
from nous.handlers import LLMClient, call_background_llm, parse_llm_json
from nous.heart.heart import Heart
from nous.heart.schemas import ProcedureInput

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_DECISION_CLUSTER_PROMPT = """You are analyzing a cluster of similar successful decisions
to extract a reusable procedure.

Decisions in this cluster:
{decisions}

These decisions share a common pattern. Extract a procedure that captures how to handle this type of situation.
Output ONLY valid JSON:
{{
  "name": "<short descriptive name>",
  "domain": "<category/domain>",
  "description": "<when and why to apply this procedure>",
  "goals": ["<when to apply this>"],
  "core_patterns": ["<the steps/patterns used>"],
  "core_tools": ["<tools used>"],
  "core_concepts": ["<why this works>"],
  "implementation_notes": ["<edge cases, caveats>"]
}}"""

_EPISODE_LESSON_PROMPT = """You are analyzing a cluster of similar lessons learned from episodes
to extract a reusable procedure.

Lessons in this cluster:
{lessons}

These lessons share a common theme. Extract a procedure that captures this knowledge.
Output ONLY valid JSON:
{{
  "name": "<short descriptive name>",
  "domain": "<category/domain>",
  "description": "<when and why to apply this procedure>",
  "goals": ["<when to apply this>"],
  "core_patterns": ["<the steps/patterns from these lessons>"],
  "core_tools": ["<tools used>"],
  "core_concepts": ["<why this works>"],
  "implementation_notes": ["<edge cases, caveats>"]
}}"""

_WEAK_REVIEW_PROMPT = """You are reviewing an auto-learned procedure that is underperforming.

Procedure: {name}
Domain: {domain}
Description: {description}
Goals: {goals}
Core patterns: {core_patterns}
Effectiveness: {effectiveness}
Last activated: {last_activated}
Activation count: {activation_count}

Decide what to do. Output ONLY valid JSON:
{{
  "action": "keep" | "revise" | "retire",
  "reason": "<why this action>",
  "revised_description": "<new description if revising, null otherwise>",
  "revised_patterns": ["<new patterns if revising, null otherwise>"]
}}"""

_MONITOR_RECOVERY_PROMPT = """The agent encountered this error pattern multiple times and recovered the same way.

Error pattern: {error_pattern}
Recovery actions: {recovery_actions}
Context: {context}

Extract a recovery procedure. Output ONLY valid JSON:
{{
  "name": "<short descriptive name>",
  "domain": "<category/domain>",
  "description": "<when and why to apply this recovery>",
  "goals": ["<when to apply this recovery>"],
  "core_patterns": ["<the recovery steps>"],
  "core_tools": ["<tools used in recovery>"],
  "core_concepts": ["<why this recovery works>"],
  "implementation_notes": ["<edge cases, when NOT to use this>"]
}}"""


# ---------------------------------------------------------------------------
# Cosine similarity (pure Python — no numpy dependency)
# ---------------------------------------------------------------------------

def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors using pure Python."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _greedy_cluster(
    embeddings: list[list[float]],
    threshold: float,
    min_size: int,
) -> list[list[int]]:
    """Greedy clustering by cosine similarity.

    Iterates through items; each unassigned item starts a new cluster,
    pulling in other unassigned items above the similarity threshold.
    Returns clusters with at least min_size members.
    """
    n = len(embeddings)
    assigned: set[int] = set()
    clusters: list[list[int]] = []

    for i in range(n):
        if i in assigned:
            continue
        cluster = [i]
        assigned.add(i)
        for j in range(i + 1, n):
            if j in assigned:
                continue
            if _cosine_similarity(embeddings[i], embeddings[j]) >= threshold:
                cluster.append(j)
                assigned.add(j)
        if len(cluster) >= min_size:
            clusters.append(cluster)

    return clusters


# ---------------------------------------------------------------------------
# ProcedureLearner
# ---------------------------------------------------------------------------

class ProcedureLearner:
    """Auto-learns procedures from decision clusters and episode lessons.

    Public API:
        run_sleep_learning() -> dict  — run all pathways, return stats
    """

    def __init__(
        self,
        brain: Brain,
        heart: Heart,
        embeddings: EmbeddingProvider | None,
        settings: Settings,
        llm_client: LLMClient | None = None,
    ) -> None:
        self._brain = brain
        self._heart = heart
        self._embeddings = embeddings
        self._settings = settings
        self._llm = llm_client

    # ==================================================================
    # Public entry point
    # ==================================================================

    async def run_sleep_learning(self) -> dict[str, Any]:
        """Run all learning pathways. Returns stats dict.

        Respects procedure_learning_enabled and procedure_max_per_sleep.
        """
        if not self._settings.procedure_learning_enabled:
            return {"enabled": False, "decisions_learned": 0, "episodes_learned": 0, "weak_reviewed": 0}

        max_total = self._settings.procedure_max_per_sleep
        created = 0
        stats: dict[str, Any] = {
            "enabled": True,
            "decisions_learned": 0,
            "episodes_learned": 0,
            "weak_reviewed": 0,
        }

        # Pathway 1: Decision clustering
        if created < max_total:
            count = await self._learn_from_decisions(max_total - created)
            stats["decisions_learned"] = count
            created += count

        # Pathway 2: Episode lesson clustering
        if created < max_total:
            count = await self._learn_from_episodes(max_total - created)
            stats["episodes_learned"] = count
            created += count

        # Weak procedure review (doesn't count toward creation cap)
        reviewed = await self._review_weak_procedures()
        stats["weak_reviewed"] = reviewed

        return stats

    # ==================================================================
    # Pathway 1: Decision clustering
    # ==================================================================

    async def _learn_from_decisions(self, max_new: int) -> int:
        """Fetch reviewed successful decisions, cluster, extract procedures."""
        if not self._embeddings or not self._llm:
            return 0

        try:
            # Fetch reviewed successful decisions — pass filters to DB
            # to avoid the window problem where 100 most recent are all pending.
            # Issue #188 Bug 1: was list_decisions(limit=100) with no filters.
            decisions_success, _ = await self._brain.list_decisions(
                limit=50, outcome="success", reviewed=True
            )
            decisions_partial, _ = await self._brain.list_decisions(
                limit=50, outcome="partial", reviewed=True
            )
            successful = list(decisions_success) + list(decisions_partial)
            logger.info(
                "Decision pathway: %d success + %d partial = %d reviewed decisions",
                len(decisions_success), len(decisions_partial), len(successful),
            )
            if len(successful) < self._settings.procedure_cluster_min_size:
                logger.info(
                    "Decision pathway: too few reviewed decisions (%d < %d)",
                    len(successful), self._settings.procedure_cluster_min_size,
                )
                return 0

            # Get full details for bridge function text
            details = []
            for d in successful:
                detail = await self._brain.get(d.id)
                if detail and detail.bridge and detail.bridge.function:
                    details.append(detail)

            if len(details) < self._settings.procedure_cluster_min_size:
                # Fallback: use descriptions if no bridge functions
                texts = [d.description for d in successful]
                items = successful
            else:
                texts = [d.bridge.function for d in details]  # type: ignore[union-attr]
                items = details  # type: ignore[assignment]

            # Embed all texts
            embeddings = await self._embeddings.embed_batch(texts)

            # Cluster
            clusters = _greedy_cluster(
                embeddings,
                threshold=self._settings.procedure_similarity_threshold,
                min_size=self._settings.procedure_cluster_min_size,
            )

            created = 0
            for cluster_indices in clusters:
                if created >= max_new:
                    break

                # Gate: success rate within cluster
                cluster_items = [items[i] for i in cluster_indices]
                if not self._check_success_rate(cluster_items):
                    continue

                # Gate: recency — at least 1 decision in last 7 days
                if not self._check_recency(cluster_items):
                    continue

                # Build cluster text for LLM
                decision_texts = []
                for item in cluster_items:
                    desc = item.description if hasattr(item, "description") else str(item)
                    decision_texts.append(f"- {desc}")

                # Extract procedure via LLM
                procedure_data = await self._call_llm(
                    _DECISION_CLUSTER_PROMPT.format(decisions="\n".join(decision_texts))
                )
                if not procedure_data:
                    continue

                # Dedup check
                if await self._is_duplicate(procedure_data):
                    continue

                # Store
                tags = procedure_data.get("tags", [])
                tags.append("auto:decision_cluster")
                proc_input = ProcedureInput(
                    name=procedure_data.get("name", "Unnamed procedure"),
                    domain=procedure_data.get("domain"),
                    description=procedure_data.get("description"),
                    goals=procedure_data.get("goals", []),
                    core_patterns=procedure_data.get("core_patterns", []),
                    core_tools=procedure_data.get("core_tools", []),
                    core_concepts=procedure_data.get("core_concepts", []),
                    implementation_notes=procedure_data.get("implementation_notes", []),
                    tags=tags,
                )
                await self._heart.store_procedure(proc_input)
                created += 1
                logger.info("Learned procedure from decision cluster: %s", proc_input.name)

            return created

        except Exception:
            logger.exception("Decision clustering failed")
            return 0

    def _check_success_rate(self, items: list[Any]) -> bool:
        """Gate: cluster success rate must be >= threshold."""
        if not items:
            return False
        successes = sum(1 for i in items if getattr(i, "outcome", None) == "success")
        rate = successes / len(items)
        return rate >= self._settings.procedure_success_rate_min

    def _check_recency(self, items: list[Any]) -> bool:
        """Gate: at least 1 item created in the last 7 days."""
        cutoff = datetime.now(UTC) - timedelta(days=7)
        for item in items:
            created_at = getattr(item, "created_at", None)
            if created_at and created_at >= cutoff:
                return True
        return False

    # ==================================================================
    # Pathway 2: Episode lesson learning
    # ==================================================================

    async def _learn_from_episodes(self, max_new: int) -> int:
        """Collect lessons from completed episodes, cluster, extract procedures."""
        if not self._embeddings or not self._llm:
            return 0

        try:
            # Fetch completed/resolved episodes
            episodes_summary = await self._heart.list_episodes(limit=50)
            logger.info("Episode pathway: %d episodes fetched", len(episodes_summary))

            # Collect lessons from completed episodes (need full details)
            all_lessons: list[str] = []
            skipped_outcome = 0
            skipped_no_lessons = 0
            for ep_summary in episodes_summary:
                if ep_summary.outcome not in ("success", "partial"):
                    skipped_outcome += 1
                    continue
                try:
                    ep_detail = await self._heart.get_episode(ep_summary.id)
                    if ep_detail.lessons_learned:
                        all_lessons.extend(ep_detail.lessons_learned)
                    else:
                        skipped_no_lessons += 1
                except (ValueError, Exception):
                    continue

            logger.info(
                "Episode pathway: %d lessons collected (%d skipped: %d wrong outcome, %d no lessons)",
                len(all_lessons), skipped_outcome + skipped_no_lessons,
                skipped_outcome, skipped_no_lessons,
            )

            if len(all_lessons) < self._settings.procedure_cluster_min_size:
                logger.info(
                    "Episode pathway: too few lessons (%d < %d)",
                    len(all_lessons), self._settings.procedure_cluster_min_size,
                )
                return 0

            # Embed lessons
            embeddings = await self._embeddings.embed_batch(all_lessons)

            # Cluster
            clusters = _greedy_cluster(
                embeddings,
                threshold=self._settings.procedure_episode_similarity,
                min_size=self._settings.procedure_cluster_min_size,
            )
            logger.info(
                "Episode pathway: %d clusters found from %d embeddings (threshold=%.2f, min_size=%d)",
                len(clusters), len(embeddings),
                self._settings.procedure_episode_similarity,
                self._settings.procedure_cluster_min_size,
            )

            created = 0
            for cluster_indices in clusters:
                if created >= max_new:
                    break

                cluster_lessons = [all_lessons[i] for i in cluster_indices]

                # Extract procedure via LLM
                lessons_text = "\n".join(f"- {lesson}" for lesson in cluster_lessons)
                procedure_data = await self._call_llm(
                    _EPISODE_LESSON_PROMPT.format(lessons=lessons_text)
                )
                if not procedure_data:
                    continue

                # Dedup check
                if await self._is_duplicate(procedure_data):
                    continue

                # Store
                tags = procedure_data.get("tags", [])
                tags.append("auto:episode_lesson")
                proc_input = ProcedureInput(
                    name=procedure_data.get("name", "Unnamed procedure"),
                    domain=procedure_data.get("domain"),
                    description=procedure_data.get("description"),
                    goals=procedure_data.get("goals", []),
                    core_patterns=procedure_data.get("core_patterns", []),
                    core_tools=procedure_data.get("core_tools", []),
                    core_concepts=procedure_data.get("core_concepts", []),
                    implementation_notes=procedure_data.get("implementation_notes", []),
                    tags=tags,
                )
                await self._heart.store_procedure(proc_input)
                created += 1
                logger.info("Learned procedure from episode lessons: %s", proc_input.name)

            return created

        except Exception:
            logger.exception("Episode lesson learning failed")
            return 0

    # ==================================================================
    # Weak procedure review
    # ==================================================================

    async def _review_weak_procedures(self) -> int:
        """Find and review auto-learned procedures with low effectiveness or stale activation."""
        if not self._llm:
            return 0

        try:
            # Search for auto-learned procedures
            auto_procs = await self._heart.search_procedures("auto:", limit=50)

            reviewed = 0
            staleness_cutoff = datetime.now(UTC) - timedelta(days=self._settings.procedure_staleness_days)

            for proc_summary in auto_procs:
                # Get full detail
                proc_detail = await self._heart.get_procedure(proc_summary.id)

                # Check weakness criteria
                is_weak = False
                if (proc_detail.effectiveness is not None
                        and proc_detail.effectiveness < self._settings.procedure_weakness_threshold):
                    is_weak = True
                if proc_detail.last_activated and proc_detail.last_activated < staleness_cutoff:
                    is_weak = True
                if proc_detail.last_activated is None and proc_detail.created_at < staleness_cutoff:
                    is_weak = True

                if not is_weak:
                    continue

                # Ask LLM to evaluate
                review_data = await self._call_llm(
                    _WEAK_REVIEW_PROMPT.format(
                        name=proc_detail.name,
                        domain=proc_detail.domain or "unknown",
                        description=proc_detail.description or "",
                        goals=", ".join(proc_detail.goals),
                        core_patterns=", ".join(proc_detail.core_patterns),
                        effectiveness=proc_detail.effectiveness,
                        last_activated=proc_detail.last_activated,
                        activation_count=proc_detail.activation_count,
                    )
                )
                if not review_data:
                    continue

                action = review_data.get("action", "keep")
                if action == "retire":
                    await self._heart.retire_procedure(proc_detail.id)
                    logger.info("Retired weak procedure: %s (%s)", proc_detail.name, review_data.get("reason"))
                elif action == "revise":
                    # Update description and patterns if provided
                    # For now, retire and re-create with updated content
                    revised_desc = review_data.get("revised_description") or proc_detail.description
                    revised_patterns = review_data.get("revised_patterns") or proc_detail.core_patterns
                    await self._heart.retire_procedure(proc_detail.id)
                    revised_input = ProcedureInput(
                        name=proc_detail.name,
                        domain=proc_detail.domain,
                        description=revised_desc,
                        goals=proc_detail.goals,
                        core_patterns=revised_patterns,
                        core_tools=proc_detail.core_tools,
                        core_concepts=proc_detail.core_concepts,
                        implementation_notes=proc_detail.implementation_notes,
                        tags=proc_detail.tags,
                    )
                    await self._heart.store_procedure(revised_input)
                    logger.info("Revised weak procedure: %s (%s)", proc_detail.name, review_data.get("reason"))
                # "keep" — do nothing

                reviewed += 1

            return reviewed

        except Exception:
            logger.exception("Weak procedure review failed")
            return 0

    # ==================================================================
    # Shared helpers
    # ==================================================================

    async def _is_duplicate(self, procedure_data: dict[str, Any]) -> bool:
        """Check if a similar procedure already exists (>0.85 similarity)."""
        name = procedure_data.get("name", "")
        description = procedure_data.get("description", "")
        patterns = " ".join(procedure_data.get("core_patterns", []))
        query_text = f"{name} {description} {patterns}".strip()

        if not query_text:
            return False

        existing = await self._heart.search_procedures(query_text, limit=1)
        if existing and existing[0].score is not None and existing[0].score > 0.85:
            logger.debug("Skipping duplicate procedure: %s (score=%.2f)", name, existing[0].score)
            return True
        return False

    async def _call_llm(self, prompt: str) -> dict[str, Any] | None:
        """Call Anthropic API and parse JSON response."""
        if not self._llm:
            return None

        text = await call_background_llm(
            self._llm,
            model=self._settings.background_model,
            system_prompt="You are analyzing patterns to extract reusable procedures for an AI agent.",
            user_message=prompt,
            max_tokens=1500,
        )

        if not text:
            return None

        try:
            result = parse_llm_json(text)
            if isinstance(result, dict):
                return result
            return None
        except json.JSONDecodeError:
            logger.warning("LLM call returned invalid JSON")
            return None
