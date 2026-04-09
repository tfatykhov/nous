"""Context assembly engine — builds system prompt within token budgets.

Queries Brain and Heart, formats results as markdown sections,
and concatenates them in priority order within per-section token budgets.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from nous.brain.brain import Brain
from nous.cognitive.dedup import ConversationDeduplicator
from nous.cognitive.intent import RetrievalPlan
from nous.cognitive.schemas import BuildResult, ContextBudget, ContextSection, FrameSelection
from nous.cognitive.usage_tracker import UsageTracker
from nous.config import Settings
from nous.heart.heart import Heart
from nous.heart.search import _wrap_with_score, apply_frame_boost
from nous.utils import text_overlap

logger = logging.getLogger(__name__)

# Tier 1 fact categories — loaded by category (always-on), excluded from Tier 3 search
TIER1_FACT_CATEGORIES = ["preference", "person", "rule"]

# F036: Section tier classification for prompt cache optimization
SECTION_TIERS: dict[str, str] = {
    "Identity": "static",
    "Context Safety": "static",
    "User Profile": "semi_stable",
    "Active Censors": "semi_stable",
    "Current Frame": "semi_stable",
}
# Everything else defaults to "dynamic" via ContextSection.tier default

# Sources exempt from relevance filter gap detection
FILTER_EXEMPT_SOURCES: set[str] = {
    "pre_prune_extraction",
}

# Per-type result count bounds for adaptive relevance filtering
RELEVANCE_MIN_RESULTS: dict[str, int] = {
    "fact": 3, "decision": 2, "procedure": 2, "episode": 2,
}
RELEVANCE_MAX_RESULTS: dict[str, int] = {
    "fact": 12, "decision": 7, "procedure": 5, "episode": 6,
}

# Default per-type fetch limits when RetrievalPlan doesn't specify.
# Intentionally higher than RELEVANCE_MAX_RESULTS — upstream filters
# (staleness, diversity, dedup) reduce the candidate pool before the
# relevance filter caps results.
DEFAULT_FETCH_LIMITS: dict[str, int] = {
    "fact": 15, "decision": 8, "procedure": 5, "episode": 8,
}

# Markers that identify internal/system episodes (handler tasks, summarizers)
_SYSTEM_EPISODE_MARKERS = ("SYSTEM TASK", "SYSTEM:", "DO NOT USE TOOLS")

# Minimum text_overlap ratio to consider a fact redundant with identity prompt
_IDENTITY_OVERLAP_THRESHOLD = 0.6


def _is_system_episode(episode) -> bool:
    """Check if an episode is an internal/system episode that shouldn't surface."""
    summary = getattr(episode, "summary", "") or ""
    title = getattr(episode, "title", "") or ""
    return any(
        marker in summary or marker in title
        for marker in _SYSTEM_EPISODE_MARKERS
    )


class ContextEngine:
    """Assembles context from Brain and Heart within token budgets."""

    CHARS_PER_TOKEN = 4

    def __init__(
        self,
        brain: Brain,
        heart: Heart,
        settings: Settings,
        identity_prompt: str = "",
        deduplicator: ConversationDeduplicator | None = None,
    ) -> None:
        self._brain = brain
        self._heart = heart
        # Tier 3 thresholds only apply with embeddings — keyword-only scores
        # use ts_rank_cd which produces much lower values (0.01-0.15)
        self._has_embeddings = getattr(brain, "embeddings", None) is not None
        self._settings = settings
        self._identity_prompt = identity_prompt
        self._deduplicator = deduplicator

    async def build(
        self,
        agent_id: str,
        session_id: str,
        input_text: str,
        frame: FrameSelection,
        session: AsyncSession | None = None,
        *,
        conversation_messages: list[str] | None = None,
        retrieval_plan: RetrievalPlan | None = None,
        usage_tracker: UsageTracker | None = None,
        identity_override: str | None = None,
        temporal_boost: bool = False,  # 008.6
        critic_skills: list[str] | None = None,
    ) -> BuildResult:
        """Build system prompt + context sections within budget.

        Returns BuildResult with system_prompt, sections, recalled_ids,
        and recalled_content_map.

        New optional parameters (all backward-compatible):
        - conversation_messages: Recent messages for deduplication (D4).
        - retrieval_plan: Intent-driven retrieval plan (D2). Falls back to default.
        - usage_tracker: Feedback tracker for boost/penalty (D3).

        Assembly order (by priority):
        1. Identity prompt (always included, static)
        2. Active censors (always, action=block first)
        3. Frame description + questions_to_ask
        4. Working memory (current task + open threads)
        5. Similar decisions from Brain.query()
        6. Relevant facts from Heart.search_facts()
        7. Relevant procedures from Heart.search_procedures()
        8. Related episodes from Heart.search_episodes()

        Pipeline order per memory type (F10):
        retrieve -> apply_frame_boost -> dedup -> usage_boost -> truncate
        """
        budget = ContextBudget.for_frame(
            frame.frame_id,
            overrides=self._settings.context_budget_overrides or None,
        )
        sections: list[ContextSection] = []
        _active_censor_names: list[str] = []
        recalled_ids: dict[str, list[str]] = {
            "decision": [],
            "fact": [],
            "procedure": [],
            "episode": [],
        }
        recalled_content_map: dict[str, str] = {}
        recalled_score_map: dict[str, float] = {}

        # Apply budget overrides from retrieval plan (F6: REPLACE semantics)
        skip_types: set[str] = set()
        if retrieval_plan:
            if retrieval_plan.budget_overrides:
                budget.apply_overrides(retrieval_plan.budget_overrides)
            skip_types = retrieval_plan.skip_types

        # Determine per-type query text and limits from plan
        _query_texts: dict[str, str] = {}
        _limits: dict[str, int] = {}
        if retrieval_plan and retrieval_plan.queries:
            for q in retrieval_plan.queries:
                _limits[q.memory_type] = q.limit
                if q.query_text:
                    _query_texts[q.memory_type] = q.query_text

        # Trim conversation_messages to budget.conversation_window (F13)
        _conv_msgs = conversation_messages
        if _conv_msgs:
            _conv_msgs = _conv_msgs[-budget.conversation_window :]

        # Tier 0: Current date/time — always injected
        now_utc = datetime.now(UTC)
        datetime_text = now_utc.strftime("%A, %B %d, %Y %H:%M UTC")
        sections.append(
            ContextSection(
                priority=0,
                label="Current Date/Time",
                content=datetime_text,
                token_estimate=self._estimate_tokens(datetime_text),
                tier=SECTION_TIERS.get("Current Date/Time", "dynamic"),
            )
        )

        # 1. Identity (always included)
        # 008: Use identity_override from DB if available, fall back to static
        _effective_identity = identity_override or self._identity_prompt
        if _effective_identity:
            identity_text = self._truncate_to_budget(_effective_identity, budget.identity)
            sections.append(
                ContextSection(
                    priority=1,
                    label="Identity",
                    content=identity_text,
                    token_estimate=self._estimate_tokens(identity_text),
                    tier=SECTION_TIERS.get("Identity", "dynamic"),
                )
            )

        # F016 Phase 0: Anti-hallucination prompt
        if self._settings.anti_hallucination_prompt:
            anti_halluc = (
                "When you encounter a cleared or degraded tool result "
                "(marked with [Tool output cleared] or showing only metadata), "
                "do NOT attempt to reconstruct or guess the original content. "
                'Instead, say "I\'d need to re-read that file" or "Let me fetch '
                'that again" and call the tool again. Results marked with '
                '"re-fetchable" can be retrieved by calling the same tool '
                "with the same arguments."
            )
            sections.append(
                ContextSection(
                    priority=2,  # High priority, right after identity
                    label="Context Safety",
                    content=anti_halluc,
                    token_estimate=self._estimate_tokens(anti_halluc),
                    tier=SECTION_TIERS.get("Context Safety", "dynamic"),
                )
            )

        # F020: Cache availability hints
        if session_id and session:
            try:
                from nous.api.tool_cache import get_cache_hints
                cache_hints = await get_cache_hints(session, session_id)
                if cache_hints:
                    hint_text = "Compressed results available:\n" + "\n".join(cache_hints)
                    sections.append(
                        ContextSection(
                            priority=2,
                            label="Cached Results",
                            content=hint_text,
                            token_estimate=self._estimate_tokens(hint_text),
                            tier=SECTION_TIERS.get("Cached Results", "dynamic"),
                        )
                    )
            except Exception:
                logger.debug("Failed to load cache hints", exc_info=True)

        # 1b. User Profile (Tier 1 — always loaded, no semantic search)
        # Dedup against identity prompt to avoid repeating the same info
        if budget.user_profile > 0:
            try:
                profile_facts = await self._heart.list_facts_by_category(
                    categories=TIER1_FACT_CATEGORIES,
                    active_only=True,
                    session=session,
                )
                if profile_facts and _effective_identity:
                    # Filter out facts whose content overlaps with identity
                    profile_facts = [
                        f for f in profile_facts
                        if text_overlap(
                            (getattr(f, "content", "") or "")[:200],
                            _effective_identity,
                        ) < _IDENTITY_OVERLAP_THRESHOLD
                    ]
                if profile_facts:
                    profile_text = self._format_facts(profile_facts)
                    profile_text = self._truncate_to_budget(profile_text, self._scaled_budget(budget.user_profile))
                    sections.append(
                        ContextSection(
                            priority=1,
                            label="User Profile",
                            content=profile_text,
                            token_estimate=self._estimate_tokens(profile_text),
                            tier=SECTION_TIERS.get("User Profile", "dynamic"),
                        )
                    )
            except Exception:
                logger.warning("Failed to load user profile facts for Tier 1 context")

        # 2. Censors (P2-5: per-section isolation)
        if budget.censors > 0:
            try:
                censors = await self._heart.list_censors(session=session)
                if censors:
                    _active_censor_names = [str(getattr(c, "id", "")) for c in censors]
                    censor_text = self._format_censors(censors)
                    censor_text = self._truncate_to_budget(censor_text, budget.censors)
                    sections.append(
                        ContextSection(
                            priority=2,
                            label="Active Censors",
                            content=censor_text,
                            token_estimate=self._estimate_tokens(censor_text),
                            tier=SECTION_TIERS.get("Active Censors", "dynamic"),
                        )
                    )
            except Exception:
                logger.warning("Failed to load censors during context build")

        # 3. Frame
        if budget.frame > 0:
            frame_text = self._format_frame(frame)
            frame_text = self._truncate_to_budget(frame_text, budget.frame)
            sections.append(
                ContextSection(
                    priority=3,
                    label="Current Frame",
                    content=frame_text,
                    token_estimate=self._estimate_tokens(frame_text),
                    tier=SECTION_TIERS.get("Current Frame", "dynamic"),
                )
            )

        # 4. Working memory (P1-7: no agent_id param)
        # Hoisted: fetch wm early so current_topic is available for query enhancement (007.2)
        wm = None
        current_topic: str | None = None
        try:
            wm = await self._heart.get_working_memory(session_id, session=session)
        except Exception:
            logger.warning("Failed to load working memory during context build")

        if wm is not None:
            current_topic = getattr(wm, "current_task", None)

        if budget.working_memory > 0 and wm is not None:
            wm_text = self._format_working_memory(wm)
            wm_text = self._truncate_to_budget(wm_text, budget.working_memory)
            sections.append(
                ContextSection(
                    priority=4,
                    label="Working Memory",
                    content=wm_text,
                    token_estimate=self._estimate_tokens(wm_text),
                    tier=SECTION_TIERS.get("Working Memory", "dynamic"),
                )
            )

        # 007.2: Topic-enhanced default query — prefix with current_topic
        # Skip prefix if topic duplicates input (set from raw user_input in layer.py)
        if current_topic and current_topic.strip().lower() != input_text.strip().lower():
            _default_query = f"{current_topic}: {input_text}"
        else:
            _default_query = input_text
        logger.info(
            "Context build query: topic=%r, input=%r, default_query=%r",
            current_topic, input_text, _default_query,
        )

        # 5. Decisions (F26: skip_types is primary skip mechanism)
        if budget.decisions > 0 and "decision" not in skip_types:
            try:
                limit = _limits.get("decision", DEFAULT_FETCH_LIMITS.get("decision", 5))
                q_text = _query_texts.get("decision", _default_query)
                decisions = await self._brain.query(q_text, limit=limit, session=session)
                logger.info("Tier3 decisions: %d results, has_embeddings=%s, scores=%s, descs=%s",
                    len(decisions) if decisions else 0, self._has_embeddings,
                    [round(getattr(d, "score", 0) or 0, 3) for d in (decisions or [])[:5]],
                    [(getattr(d, "description", "") or "")[:50] for d in (decisions or [])[:3]])
                if decisions:
                    # F017: Staleness penalty (before boosts)
                    decisions = self._apply_staleness_penalty(decisions)
                    # 007.2: Diversity filter — use category as topic key
                    decisions = self._enforce_diversity(decisions, "category", max_per_subject=3)
                    # Adaptive relevance filter (min/max K + gap detection)
                    decisions = self._apply_relevance_filter(decisions, "decision")
                    # F1: Collect recalled IDs and scores
                    for d in decisions:
                        mid = str(getattr(d, "id", ""))
                        if mid:
                            recalled_ids["decision"].append(mid)
                            recalled_score_map[mid] = getattr(d, "score", 0) or 0
                    # Format content for each decision (F8: recalled_content_map)
                    dec_text = self._format_decisions(decisions)
                    for d in decisions:
                        mid = str(getattr(d, "id", ""))
                        if mid:
                            desc = getattr(d, "description", "")
                            recalled_content_map[mid] = desc
                    dec_text = self._truncate_to_budget(dec_text, self._scaled_budget(budget.decisions))
                    sections.append(
                        ContextSection(
                            priority=5,
                            label="Related Decisions",
                            content=dec_text,
                            token_estimate=self._estimate_tokens(dec_text),
                            tier=SECTION_TIERS.get("Related Decisions", "dynamic"),
                        )
                    )
            except Exception as e:
                logger.warning("Brain.query failed during context build: %s", e)

        # 6. Facts (F10: retrieve -> apply_frame_boost -> dedup -> usage_boost -> truncate)
        if budget.facts > 0 and "fact" not in skip_types:
            try:
                limit = _limits.get("fact", DEFAULT_FETCH_LIMITS.get("fact", 5))
                q_text = _query_texts.get("fact", _default_query)
                # Tier 3: exclude Tier 1 categories from semantic search
                facts = await self._heart.search_facts(
                    q_text, limit=limit, session=session,
                    exclude_categories=TIER1_FACT_CATEGORIES,
                )
                logger.info("Tier3 facts: %d results, has_embeddings=%s, scores=%s, subjects=%s",
                    len(facts) if facts else 0, self._has_embeddings,
                    [round(getattr(f, "score", 0) or 0, 3) for f in (facts or [])[:5]],
                    [(getattr(f, "subject", "") or "")[:30] for f in (facts or [])[:5]])
                if facts:
                    # F017: Staleness penalty (before boosts)
                    facts = self._apply_staleness_penalty(facts)
                    # F10: apply_frame_boost (preserved from existing pipeline)
                    facts = apply_frame_boost(facts, frame.frame_id, _active_censor_names)

                    # 007.2: Diversity filter — use subject as topic key
                    facts = self._enforce_diversity(facts, "subject", max_per_subject=2)

                    # Dedup against conversation
                    facts = await self._apply_dedup(facts, _conv_msgs, "content")

                    # Usage boost
                    facts = self._apply_usage_boost(facts, usage_tracker)
                    # Adaptive relevance filter (min/max K + gap detection)
                    facts = self._apply_relevance_filter(facts, "fact")

                    # F1: Collect recalled IDs AFTER filtering (P1-1 fix:
                    # collecting before dedup would penalize deduped memories
                    # in the usage tracker as "retrieved but not referenced")
                    for f in facts:
                        mid = str(getattr(f, "id", ""))
                        if mid:
                            recalled_ids["fact"].append(mid)
                            recalled_content_map[mid] = getattr(f, "content", "")
                            recalled_score_map[mid] = getattr(f, "score", 0) or 0

                    logger.info("Tier3 facts after pipeline: %d remaining", len(facts))
                    facts_text = self._format_facts(facts)
                    facts_text = self._truncate_to_budget(facts_text, self._scaled_budget(budget.facts))
                    sections.append(
                        ContextSection(
                            priority=6,
                            label="Relevant Facts",
                            content=facts_text,
                            token_estimate=self._estimate_tokens(facts_text),
                            tier=SECTION_TIERS.get("Relevant Facts", "dynamic"),
                        )
                    )
            except Exception as e:
                logger.warning("Heart.search_facts failed during context build: %s", e)

        # 7. Procedures (dual-track: Critic reserved slots + embedding slots, issue #229)
        if budget.procedures > 0 and "procedure" not in skip_types:
            try:
                injection_mode = self._settings.critic_skill_injection
                critic_slot_count = self._settings.critic_skill_slots
                embedding_slot_count = self._settings.embedding_skill_slots
                total_slots = critic_slot_count + embedding_slot_count

                # --- Track A: Critic-recommended skills ---
                critic_procedures: list = []
                critic_names: set[str] = set()

                if critic_skills and injection_mode in ("enabled", "log_only"):
                    # Deduplicate input (P1 review fix)
                    unique_skills = list(dict.fromkeys(critic_skills))
                    for skill_name in unique_skills[:critic_slot_count]:
                        try:
                            proc = await self._heart.get_procedure_by_name(
                                skill_name, session=session,
                            )
                            if proc:
                                critic_procedures.append(proc)
                                critic_names.add(skill_name)
                            else:
                                logger.debug(
                                    "Issue #229: Critic skill '%s' not found in DB, skipping",
                                    skill_name,
                                )
                        except Exception:
                            logger.warning(
                                "Issue #229: Failed to fetch Critic skill '%s'",
                                skill_name,
                            )

                    if injection_mode == "log_only":
                        logger.info(
                            "Issue #229 log_only: would inject Critic skills: %s",
                            [getattr(p, "name", "") for p in critic_procedures],
                        )
                        critic_procedures = []
                        critic_names = set()

                # --- Track B: Embedding similarity search ---
                unused_critic_slots = critic_slot_count - len(critic_procedures)
                embedding_limit = embedding_slot_count + unused_critic_slots  # rollover

                q_text = _query_texts.get("procedure", _default_query)
                embedding_procedures = await self._heart.search_procedures(
                    q_text, limit=embedding_limit, frame_type=frame.frame_id, session=session,
                )

                if embedding_procedures:
                    # Standard pipeline: staleness -> frame boost -> dedup -> usage boost -> relevance
                    embedding_procedures = self._apply_staleness_penalty(embedding_procedures)
                    embedding_procedures = apply_frame_boost(
                        embedding_procedures, frame.frame_id, _active_censor_names,
                    )
                    embedding_procedures = await self._apply_dedup(
                        embedding_procedures, _conv_msgs, "name",
                    )
                    embedding_procedures = self._apply_usage_boost(
                        embedding_procedures, usage_tracker,
                    )
                    # F038-2.1: Absolute procedure score floor (embedding mode only)
                    if self._has_embeddings and self._settings.procedure_score_floor > 0:
                        embedding_procedures = [
                            p for p in embedding_procedures
                            if (getattr(p, "score", 0) or 0) >= self._settings.procedure_score_floor
                        ]
                    embedding_procedures = self._apply_relevance_filter(
                        embedding_procedures, "procedure",
                    )

                    # Deduplicate: exclude Critic picks from embedding results
                    if critic_names:
                        embedding_procedures = [
                            p for p in embedding_procedures
                            if getattr(p, "name", "") not in critic_names
                        ]

                    embedding_procedures = embedding_procedures[:embedding_limit]

                # F038-1.3: Dedup procedures vs identity prompt
                _effective_identity = identity_override or self._identity_prompt
                if embedding_procedures and _effective_identity:
                    embedding_procedures = [
                        p for p in embedding_procedures
                        if text_overlap(
                            getattr(p, "body", "") or getattr(p, "steps_text", "") or "",
                            _effective_identity,
                        ) < _IDENTITY_OVERLAP_THRESHOLD
                    ]

                # --- Combine tracks ---
                all_procedures = critic_procedures + (embedding_procedures or [])
                all_procedures = all_procedures[:total_slots]

                if all_procedures:
                    for p in all_procedures:
                        mid = str(getattr(p, "id", ""))
                        if mid:
                            recalled_ids["procedure"].append(mid)
                            recalled_content_map[mid] = getattr(p, "name", "")
                            # ProcedureDetail (Critic track) has no .score — use 1.0 as priority
                            recalled_score_map[mid] = getattr(p, "score", None) or 1.0

                    proc_text = self._format_procedures(all_procedures)
                    proc_text = self._truncate_to_budget(
                        proc_text, self._scaled_budget(budget.procedures),
                    )
                    sections.append(
                        ContextSection(
                            priority=7,
                            label="Known Procedures",
                            content=proc_text,
                            token_estimate=self._estimate_tokens(proc_text),
                            tier=SECTION_TIERS.get("Known Procedures", "dynamic"),
                        )
                    )
            except Exception as e:
                logger.warning("Procedure context build failed: %s", e)

        # 7.5 Temporal awareness — always include recent episode titles (008.6)
        # Not gated by budget.episodes — this is a lightweight tier that shows
        # only titles (+ summaries when boosted), separate from heavy semantic retrieval
        _temporal_episode_ids: set[str] = set()
        if self._settings.temporal_context_enabled:
            try:
                recent = await self._heart.list_episodes(limit=5, hours=48)
                # Filter out system/internal episodes (handler tasks, summarization runs)
                if recent:
                    recent = [e for e in recent if not _is_system_episode(e)]
                if recent:
                    _temporal_episode_ids = {str(e.id) for e in recent}
                    recent_lines = []
                    for e in recent:
                        title = e.title or (e.summary[:60] if e.summary else "Untitled")
                        time_str = e.started_at.strftime("%b %d %H:%M")
                        recent_lines.append(f"- [{time_str}] {title}")
                        # 008.6: Include summaries when temporal boost is active
                        if temporal_boost and e.summary and e.summary != e.title:
                            recent_lines.append(f"  {e.summary[:200]}")
                    recent_text = "\n".join(recent_lines)
                    sections.append(
                        ContextSection(
                            priority=7,
                            label="Recent Conversations",
                            content=recent_text,
                            token_estimate=self._estimate_tokens(recent_text),
                            tier=SECTION_TIERS.get("Recent Conversations", "dynamic"),
                        )
                    )
            except Exception as e:
                logger.warning("Temporal tier failed: %s", e)

        # 8. Episodes
        if budget.episodes > 0 and "episode" not in skip_types:
            try:
                limit = _limits.get("episode", DEFAULT_FETCH_LIMITS.get("episode", 5))
                q_text = _query_texts.get("episode", _default_query)
                episodes = await self._heart.search_episodes(q_text, limit=limit, session=session)
                # Filter out system/internal episodes
                episodes = [e for e in episodes if not _is_system_episode(e)]
                # 008.6: Exclude episodes already shown in temporal tier
                if _temporal_episode_ids:
                    episodes = [e for e in episodes if str(e.id) not in _temporal_episode_ids]
                if episodes:
                    # F038-2.3: Episode-specific recency weighting (replaces general staleness)
                    episodes = self._apply_episode_recency(episodes)
                    # F10: apply_frame_boost
                    episodes = apply_frame_boost(episodes, frame.frame_id, _active_censor_names)

                    # 007.2: Diversity filter — use first tag as topic key
                    episodes = self._enforce_diversity(episodes, "tags", max_per_subject=2)

                    # Dedup + usage boost
                    episodes = await self._apply_dedup(episodes, _conv_msgs, "summary")
                    episodes = self._apply_usage_boost(episodes, usage_tracker)
                    # Adaptive relevance filter (min/max K + gap detection)
                    episodes = self._apply_relevance_filter(episodes, "episode")

                    # F1: Collect recalled IDs AFTER filtering (P1-1 fix)
                    for e in episodes:
                        mid = str(getattr(e, "id", ""))
                        if mid:
                            recalled_ids["episode"].append(mid)
                            recalled_content_map[mid] = getattr(e, "summary", "")
                            recalled_score_map[mid] = getattr(e, "score", 0) or 0

                    ep_text = self._format_episodes(episodes)
                    ep_text = self._truncate_to_budget(ep_text, self._scaled_budget(budget.episodes))
                    sections.append(
                        ContextSection(
                            priority=8,
                            label="Past Episodes",
                            content=ep_text,
                            token_estimate=self._estimate_tokens(ep_text),
                            tier=SECTION_TIERS.get("Past Episodes", "dynamic"),
                        )
                    )
            except Exception as e:
                logger.warning("Heart.search_episodes failed during context build: %s", e)

        # Assemble system prompt with markdown headers
        parts: list[str] = []
        for section in sorted(sections, key=lambda s: s.priority):
            parts.append(f"## {section.label}\n\n{section.content}")

        system_prompt = "\n\n".join(parts)

        total_budget = budget.total
        total_used = sum(s.token_estimate for s in sections)
        logger.info(
            "Context assembly: frame=%s, budget=%d, used=%d, fill_ratio=%.1f%%",
            frame.frame_id, total_budget, total_used,
            (total_used / total_budget * 100) if total_budget > 0 else 0,
        )

        # F036: Group sections by tier for cache-optimized system prompt splitting
        tier_groups: dict[str, list[str]] = {"static": [], "semi_stable": [], "dynamic": []}
        for section in sorted(sections, key=lambda s: s.priority):
            tier = section.tier
            if tier not in tier_groups:
                tier = "dynamic"
            tier_groups[tier].append(f"## {section.label}\n\n{section.content}")

        sections_by_tier = {
            tier: "\n\n".join(parts)
            for tier, parts in tier_groups.items()
            if parts
        }

        return BuildResult(
            system_prompt=system_prompt,
            sections=sections,
            recalled_ids=recalled_ids,
            recalled_content_map=recalled_content_map,
            recalled_score_map=recalled_score_map,
            sections_by_tier=sections_by_tier,
        )

    async def _apply_dedup(
        self,
        items: list,
        conversation_messages: list[str] | None,
        content_attr: str,
    ) -> list:
        """Apply conversation deduplication to retrieved items.

        Filters out items whose content is redundant with recent conversation.
        """
        if not self._deduplicator or not conversation_messages or not items:
            return items

        try:
            memories = [
                (str(getattr(item, "id", "")), getattr(item, content_attr, ""))
                for item in items
            ]
            results = await self._deduplicator.check(memories, conversation_messages)
            # Filter out redundant items
            redundant_ids = {r.memory_id for r in results if r.is_redundant}
            return [
                item for item in items if str(getattr(item, "id", "")) not in redundant_ids
            ]
        except Exception:
            logger.warning("Dedup failed, keeping all items")
            return items

    def _apply_usage_boost(self, items: list, usage_tracker: UsageTracker | None) -> list:
        """Re-rank items using usage-based boost factors.

        Items with high reference rates get boosted; items retrieved
        but rarely referenced get penalized.
        """
        if not usage_tracker or not items:
            return items

        boosted = []
        for item in items:
            mid = str(getattr(item, "id", ""))
            boost = usage_tracker.get_boost_factor(mid) if mid else 1.0
            wrapped = _wrap_with_score(item, (getattr(item, "score", 0) or 0) * boost)
            boosted.append((wrapped, boost))

        boosted.sort(key=lambda x: x[1], reverse=True)
        return [item for item, _ in boosted]

    def _apply_relevance_filter(self, results: list, memory_type: str) -> list:
        """Adaptive relevance filtering (replaces F017 floor + diminishing returns).

        Strategy: Keep top-K results, then cut at score gaps.
        - Always keep at least min_results (don't return empty)
        - Always keep at most max_results (don't flood context)
        - Between min and max, cut at sharp score drops
        - Items from exempt sources bypass gap detection
        """
        if not self._settings.relevance_floor_enabled:
            return results
        if not results:
            return results

        # Merge defaults with config overrides
        min_k = {**RELEVANCE_MIN_RESULTS, **self._settings.relevance_min_results}.get(memory_type, 2)
        max_k = {**RELEVANCE_MAX_RESULTS, **self._settings.relevance_max_results}.get(memory_type, 5)

        # Always keep at least min_k
        if len(results) <= min_k:
            return results

        # Cap at max_k
        results = results[:max_k]

        # Within [min_k, max_k], cut at sharp score drops
        # Track prev_score separately to avoid exempt items contaminating gap check
        drop_ratio = self._settings.relevance_drop_ratio
        prev_score = getattr(results[min_k - 1], "score", 0) or 0
        for i in range(min_k, len(results)):
            # Preserve exempt-source items (e.g. pre_prune_extraction)
            if getattr(results[i], "source", None) in FILTER_EXEMPT_SOURCES:
                continue
            score = getattr(results[i], "score", 0) or 0
            if prev_score > 0 and score < prev_score * drop_ratio:
                # Keep any exempt items beyond the cut point
                tail_exempt = [
                    r for r in results[i:]
                    if getattr(r, "source", None) in FILTER_EXEMPT_SOURCES
                ]
                return results[:i] + tail_exempt
            prev_score = score

        return results

    def _apply_staleness_penalty(self, results: list) -> list:
        """Apply time-decay penalty to relevance scores (F017 Phase 5)."""
        if not self._settings.staleness_penalty_enabled:
            return results
        half_life = self._settings.staleness_half_life_days
        now = datetime.now(UTC)
        adjusted = []
        for r in results:
            score = getattr(r, "score", None)
            if score is None:
                adjusted.append(r)
                continue
            created = getattr(r, "created_at", None)
            if not created:
                adjusted.append(r)
                continue
            category = getattr(r, "category", "")
            if category in {"rule", "preference", "technical", "concept", "person"}:
                adjusted.append(r)
                continue
            age_days = (now - created).days
            if age_days > 0:
                decay = 0.5 ** (age_days / half_life)
                adjusted.append(_wrap_with_score(r, score * max(decay, 0.3)))
            else:
                adjusted.append(r)
        return adjusted

    def _apply_episode_recency(self, episodes: list) -> list:
        """Apply linear time-decay to episode scores (F038-2.3).

        Uses linear decay instead of exponential staleness:
        final_score = score * max(0.5, 1.0 - (age_days / 60))
        Episodes >60 days old get 0.5x penalty, recent ones ~1.0x.
        """
        now = datetime.now(UTC)
        adjusted = []
        for ep in episodes:
            score = getattr(ep, "score", None)
            if score is None:
                adjusted.append(ep)
                continue
            started = getattr(ep, "started_at", None)
            if started is None:
                adjusted.append(ep)
                continue
            age_days = (now - started).total_seconds() / 86400
            decay = max(0.5, 1.0 - (age_days / 60))
            adjusted.append(_wrap_with_score(ep, score * decay))
        return adjusted

    def _enforce_diversity(self, items: list, topic_attr: str, max_per_subject: int = 2) -> list:
        """Prevent one topic from dominating recall results (007.2).

        Extracts a topic key from each item using topic_attr:
        - String attrs (e.g. 'subject', 'category'): full string, stripped and lowercased
        - List attrs (e.g. 'tags'): first element, lowercased
        Items without the attr default to 'unknown'.
        """
        if not items:
            return items

        seen: dict[str, int] = {}
        result = []
        for item in items:
            raw = getattr(item, topic_attr, None)
            if isinstance(raw, list):
                topic_key = raw[0].lower() if raw else "unknown"
            elif isinstance(raw, str) and raw:
                topic_key = raw.strip().lower()
            else:
                topic_key = "unknown"

            count = seen.get(topic_key, 0)
            if count < max_per_subject:
                result.append(item)
                seen[topic_key] = count + 1
        return result

    def _scaled_budget(self, base_budget: int) -> int:
        """Scale budget ceiling for larger context windows (F017 Phase 3)."""
        if not self._settings.budget_scale_enabled:
            return base_budget
        window = self._settings._get_context_window(self._settings.model)
        if window >= 1_000_000:
            return int(base_budget * 2.5)
        elif window >= 200_000:
            return int(base_budget * 1.5)
        return base_budget

    def _estimate_tokens(self, text: str) -> int:
        """Rough token count: len(text) / CHARS_PER_TOKEN."""
        return max(1, len(text) // self.CHARS_PER_TOKEN)

    def _truncate_to_budget(self, text: str, token_budget: int) -> str:
        """Truncate text to fit within token budget."""
        max_chars = token_budget * self.CHARS_PER_TOKEN
        if len(text) <= max_chars:
            return text
        return text[: max_chars - 3] + "..."

    def _format_decisions(self, decisions: list) -> str:
        """Format decision summaries for context.

        P1-10: No reasons field on DecisionSummary.
        Format: - [{outcome}] {description} (confidence: {confidence})
        """
        lines = []
        for d in decisions:
            outcome = getattr(d, "outcome", "pending") or "pending"
            desc = getattr(d, "description", "")
            conf = getattr(d, "confidence", 0.0)
            lines.append(f"- [{outcome}] {desc} (confidence: {conf:.2f})")
        return "\n".join(lines)

    def _format_facts(self, facts: list) -> str:
        """Format facts for context.

        Format: - [subject]: content_truncated [confidence: N.NN]
        Truncates content to 200 chars at word boundary.
        """
        lines = []
        for f in facts:
            content = getattr(f, "content", "")
            conf = getattr(f, "confidence", 1.0)
            subject = getattr(f, "subject", None)

            # Truncate at word boundary
            max_len = 200
            if len(content) > max_len:
                truncated = content[:max_len].rsplit(" ", 1)[0]
                content = truncated + "..."

            if subject:
                lines.append(f"- [{subject}] {content} [confidence: {conf:.2f}]")
            else:
                lines.append(f"- {content} [confidence: {conf:.2f}]")
        return "\n".join(lines)

    def _format_procedures(self, procedures: list) -> str:
        """Format procedures for context."""
        lines = []
        for p in procedures:
            name = getattr(p, "name", "")
            domain = getattr(p, "domain", None) or "general"
            desc = getattr(p, "description", None) or ""
            count = getattr(p, "activation_count", 0)
            eff = getattr(p, "effectiveness", None)
            eff_str = f", effectiveness: {eff:.0%}" if eff is not None else ""
            desc_str = f": {desc} | " if desc else ": "
            lines.append(f"- **{name}** ({domain}){desc_str}activated {count}x{eff_str}")
        return "\n".join(lines)

    def _format_episodes(self, episodes: list) -> str:
        """Format episodes for context.

        Format: - [{outcome}] {summary} ({started_at date})
        """
        lines = []
        for e in episodes:
            outcome = getattr(e, "outcome", None) or "ongoing"
            if outcome == "abandoned":
                continue
            summary = getattr(e, "summary", "")
            started = getattr(e, "started_at", None)
            date_str = started.strftime("%Y-%m-%d") if started else "unknown"
            lines.append(f"- [{outcome}] {summary} ({date_str})")
        return "\n".join(lines)

    def _format_censors(self, censors: list) -> str:
        """Format active censors.

        P1-4: Use action (not severity).
        Format: - **{ACTION}:** {trigger_pattern} -- {reason}
        F031: Append action_instruction for warn censors if present.
        """
        action_order = {"absolute": 0, "block": 1, "warn": 2}
        sorted_censors = sorted(
            censors,
            key=lambda c: action_order.get(getattr(c, "action", "warn"), 3),
        )
        lines = []
        for c in sorted_censors:
            action = getattr(c, "action", "warn").upper()
            pattern = getattr(c, "trigger_pattern", "")
            reason = getattr(c, "reason", "")
            line = f"- **{action}:** {pattern} -- {reason}"
            # F031: Append action_instruction for warn censors
            instruction = getattr(c, "action_instruction", None)
            if instruction and action == "WARN":
                line += f"\n  *Instruction:* {instruction}"
            lines.append(line)
        return "\n".join(lines)

    def _format_frame(self, frame: FrameSelection) -> str:
        """Format frame description and questions."""
        parts = [f"**{frame.frame_name}**: {frame.description or ''}"]
        if frame.questions_to_ask:
            parts.append("\nConsider asking:")
            for q in frame.questions_to_ask:
                parts.append(f"- {q}")
        return "\n".join(parts)

    def _format_working_memory(self, wm) -> str:
        """Format working memory state.

        Includes current_task, open_threads, and high-relevance items.
        """
        parts = []
        task = getattr(wm, "current_task", None)
        if task:
            parts.append(f"**Current task:** {task}")

        frame = getattr(wm, "current_frame", None)
        if frame:
            parts.append(f"**Frame:** {frame}")

        threads = getattr(wm, "open_threads", [])
        if threads:
            parts.append("\n**Open threads:**")
            for t in threads:
                desc = getattr(t, "description", "")
                priority = getattr(t, "priority", "medium")
                parts.append(f"- [{priority}] {desc}")

        items = getattr(wm, "items", [])
        # Only include high-relevance items (>= 0.7)
        high_rel = [i for i in items if getattr(i, "relevance", 0) >= 0.7]
        if high_rel:
            parts.append("\n**Loaded context:**")
            for item in high_rel:
                summary = getattr(item, "summary", "")
                rel = getattr(item, "relevance", 0)
                parts.append(f"- {summary} (relevance: {rel:.1f})")

        return "\n".join(parts) if parts else "No active working memory."

    async def refresh_needed(
        self,
        agent_id: str,
        session_id: str,
        new_input: str,
        current_frame: FrameSelection,
        session: AsyncSession | None = None,
    ) -> bool:
        """Check if context should be rebuilt.

        Returns True if:
        1. Working memory's current_frame differs from current_frame.frame_id
        2. No working memory exists for this session
        """
        try:
            wm = await self._heart.get_working_memory(session_id, session=session)
            if wm is None:
                return True
            return wm.current_frame != current_frame.frame_id
        except Exception:
            return True

    async def expand(
        self,
        memory_type: str,
        memory_id: str,
        session: AsyncSession | None = None,
    ) -> str:
        """Load full detail for a specific memory.

        memory_type: "decision", "fact", "episode", "procedure"
        Routes to Brain.get() or Heart managers accordingly.
        """
        uid = UUID(memory_id)

        if memory_type == "decision":
            detail = await self._brain.get(uid, session=session)
            if detail is None:
                return f"Decision {memory_id} not found."
            return (
                f"**{detail.description}**\n"
                f"Category: {detail.category} | Stakes: {detail.stakes} | "
                f"Confidence: {detail.confidence:.2f}\n"
                f"Context: {detail.context or 'None'}\n"
                f"Pattern: {detail.pattern or 'None'}"
            )

        if memory_type == "fact":
            detail = await self._heart.get_fact(uid, session=session)
            return (
                f"**{detail.content}**\n"
                f"Category: {detail.category or 'None'} | "
                f"Confidence: {detail.confidence:.2f} | "
                f"Source: {detail.source or 'unknown'}"
            )

        if memory_type == "episode":
            detail = await self._heart.get_episode(uid, session=session)
            return (
                f"**{detail.summary}**\n"
                f"Outcome: {detail.outcome or 'ongoing'} | "
                f"Started: {detail.started_at.strftime('%Y-%m-%d %H:%M')}\n"
                f"Lessons: {', '.join(detail.lessons_learned) if detail.lessons_learned else 'None'}"
            )

        if memory_type == "procedure":
            detail = await self._heart.get_procedure(uid, session=session)
            return (
                f"**{detail.name}** ({detail.domain or 'general'})\n"
                f"{detail.description or ''}\n"
                f"Effectiveness: {detail.effectiveness or 'unknown'}"
            )

        return f"Unknown memory type: {memory_type}"

    def _dedup_decisions(self, decisions: list) -> list:
        """Remove near-duplicate decisions, keeping the most recent (006.2).

        Preserves decisions with different outcomes even if descriptions overlap
        (e.g., success and failure on the same task are both valuable).
        """
        if len(decisions) <= 1:
            return decisions

        # Sort newest first so first-seen = most recent
        decisions = sorted(
            decisions,
            key=lambda d: getattr(d, "created_at", None) or "",
            reverse=True,
        )

        kept: list = []
        for d in decisions:
            desc = getattr(d, "description", "") or ""
            outcome = getattr(d, "outcome", "pending") or "pending"
            is_dup = False
            for k in kept:
                k_desc = getattr(k, "description", "") or ""
                k_outcome = getattr(k, "outcome", "pending") or "pending"
                # Only dedup if BOTH description similar AND same outcome
                if (outcome == k_outcome and
                        text_overlap(desc[:150], k_desc[:150]) > 0.80):
                    is_dup = True
                    break
            if not is_dup:
                kept.append(d)
        return kept
