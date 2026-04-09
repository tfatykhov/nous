"""Cognitive Layer — The Nous Loop orchestrator.

Wires Brain and Heart into a thinking loop:
Sense -> Frame -> Recall -> Deliberate -> Act -> Monitor -> Learn.

This is NOT an LLM wrapper. The LLM handles "Act". The Cognitive Layer
handles everything else.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func

from nous.brain.brain import Brain
from nous.cognitive.context import ContextEngine
from nous.cognitive.dedup import ConversationDeduplicator
from nous.cognitive.deliberation import DeliberationEngine
from nous.cognitive.frames import FrameEngine
from nous.cognitive.intent import IntentClassifier, IntentSignals
from nous.cognitive.monitor import MonitorEngine
from nous.cognitive.schemas import Assessment, BuildResult, SessionMetadata, TurnContext, TurnResult
from nous.cognitive.usage_tracker import UsageTracker
from nous.config import Settings
from nous.events import Event, EventBus
from nous.heart.censor_actions import CensorActionExecutor
from nous.heart.heart import Heart
from nous.heart.schemas import EpisodeInput, FactInput, OpenThread, WorkingMemoryItem
from nous.identity.manager import IdentityManager
from nous.storage.models import Agent

if TYPE_CHECKING:
    from nous.cognitive.critic import CriticAgent

logger = logging.getLogger(__name__)

# P2-9: Reflection parsing regex — case-insensitive, supports markdown bullets
_LEARNED_PATTERN = re.compile(r"^\s*[-*]?\s*learned:\s*(.+)$", re.IGNORECASE | re.MULTILINE)

# Significance threshold constants (005.5 Phase A)
_MIN_CONTENT_LENGTH = 200  # Combined user+assistant chars
_MIN_TURNS_WITHOUT_TOOLS = 1  # R: off-by-one fix — turn_count is incremented in post_turn,
# so during turn 2's pre_turn, turn_count==1

# 008.6: Recap query detection
_RECAP_PATTERNS = frozenset(
    {
        "what did we talk about",
        "what have we discussed",
        "what did we do",
        "recent conversations",
        "catch me up",
        "what happened recently",
        "what happened lately",
        "recap",
        "summary of recent",
    }
)


def _is_recap_query(user_input: str) -> bool:
    """Detect if user is asking for a temporal recap."""
    lower = user_input.lower().strip()
    return any(p in lower for p in _RECAP_PATTERNS)


def _format_subtask_results(subtasks: list) -> str:
    """Format undelivered subtask results for context injection."""
    if not subtasks:
        return ""

    lines: list[str] = []
    completed = [s for s in subtasks if s.status == "completed"]
    failed = [s for s in subtasks if s.status == "failed"]

    if completed:
        for s in completed:
            lines.append("=== Completed Subtask ===")
            lines.append(f"[subtask-{s.id.hex[:8]}] Task: {s.task}")
            lines.append(f"Result: {s.result}")
            lines.append("")

    if failed:
        for s in failed:
            lines.append("=== Failed Subtask ===")
            lines.append(f"[subtask-{s.id.hex[:8]}] Task: {s.task}")
            lines.append(f"Error: {s.error}")
            lines.append("")

    return "\n".join(lines).strip()


def _check_censor_compliance(
    censor_injected_context: dict[str, str],
    response_text: str,
) -> dict[str, bool]:
    """Check if the agent's response references censor-injected context.

    Returns a dict mapping censor_id -> True if the response appears to
    reference the injected content, False otherwise. Uses simple keyword
    overlap heuristic — not a semantic check.
    """
    results: dict[str, bool] = {}
    response_lower = response_text.lower()
    for censor_id, injected_text in censor_injected_context.items():
        # Extract meaningful words from injected text (skip formatting)
        words = set()
        for line in injected_text.split("\n"):
            line = line.strip()
            if line.startswith("[Censor"):
                continue  # Skip header lines
            for word in line.split():
                cleaned = word.strip(".,;:()[]'\"").lower()
                if len(cleaned) > 4:  # Skip short/common words
                    words.add(cleaned)
        # Consider compliant if at least 2 meaningful words appear in response
        matches = sum(1 for w in words if w in response_lower)
        results[censor_id] = matches >= 2
    return results


class CognitiveLayer:
    """The Nous Loop — orchestrates Brain and Heart into cognition.

    Usage:
        cognitive = CognitiveLayer(brain, heart, settings)

        # Before LLM turn
        ctx = await cognitive.pre_turn(agent_id, session_id, user_input)
        # ctx.system_prompt contains full context
        # ctx.decision_id set if deliberation started

        # After LLM turn
        result = TurnResult(response_text=llm_output, tool_results=[...])
        assessment = await cognitive.post_turn(agent_id, session_id, result, ctx)

        # End of conversation
        await cognitive.end_session(agent_id, session_id)
    """

    def __init__(
        self,
        brain: Brain,
        heart: Heart,
        settings: Settings,
        identity_prompt: str = "",
        *,
        bus: EventBus | None = None,
        identity_manager: IdentityManager | None = None,
        critic: CriticAgent | None = None,
    ) -> None:
        self._brain = brain
        self._heart = heart
        self._settings = settings
        self._bus = bus
        self._identity_manager = identity_manager
        self._identity_prompt_fallback = identity_prompt
        # P1-1: Use brain.db (public), not brain._db
        self._frames = FrameEngine(brain.db, settings)
        # F3: Instantiate IntentClassifier and UsageTracker
        self._intent_classifier = IntentClassifier()
        self._usage_tracker = UsageTracker()
        # F14: Pass EmbeddingProvider from brain.embeddings to deduplicator
        _deduplicator = ConversationDeduplicator(
            embedding_provider=brain.embeddings,
        )
        self._context = ContextEngine(brain, heart, settings, identity_prompt, deduplicator=_deduplicator)
        self._deliberation = DeliberationEngine(brain)
        self._monitor = MonitorEngine(brain, heart, settings)

        # F024: Critic Agent
        self._critic = critic
        self._session_tool_history: dict[str, list[dict]] = {}
        self._censor_executor = CensorActionExecutor(heart)
        self._pending_nudges: dict[str, str] = {}
        self._session_response_lengths: dict[str, list[int]] = {}
        self._session_user_messages: dict[str, list[str]] = {}

        # Track active episodes per session.
        # P2-10: Known race condition at await boundaries — two coroutines
        # calling pre_turn for same session_id can create duplicate episodes.
        # Acceptable for v0.1 (single agent, single runtime). Full fix with
        # asyncio.Lock per session can come later.
        self._active_episodes: dict[str, str] = {}  # session_id -> episode_id

        # 005.5: Session metadata for significance filtering
        # P1-8: Known memory leak on abandoned sessions (same as _active_episodes)
        self._session_metadata: dict[str, SessionMetadata] = {}  # session_id -> metadata

    async def list_frames(self, agent_id: str, session: AsyncSession | None = None) -> list:
        """Public delegation to FrameEngine.list_frames()."""
        return await self._frames.list_frames(agent_id, session=session)

    def get_active_episode_id(self, session_id: str) -> str | None:
        """Return the active episode UUID string for a session, or None.

        Used by the runner to auto-inject source_episode_id into learn_fact
        calls so fact→episode edges are created without requiring the model
        to know or pass the UUID explicitly.

        Limitation (P1-1): _active_episodes is in-memory only.  After a
        process restart the dict is empty and this returns None, causing
        injection to be silently skipped.  The proper fix is to add
        session_id to the Episode DB schema and fall back to a DB query here.
        Tracked as follow-up migration task.
        """
        episode_id = self._active_episodes.get(session_id)
        if episode_id is None:
            logger.debug(
                "get_active_episode_id: no active episode for session %s "
                "(in-memory miss — may be post-restart or low-significance turn)",
                session_id,
            )
        return episode_id

    async def pre_turn(
        self,
        agent_id: str,
        session_id: str,
        user_input: str,
        session: AsyncSession | None = None,
        *,
        conversation_messages: list[str] | None = None,
        user_id: str | None = None,
        user_display_name: str | None = None,
        skip_episode: bool = False,
        is_subtask: bool = False,
    ) -> TurnContext:
        """SENSE -> FRAME -> RECALL -> DELIBERATE — prepare for LLM turn.

        Steps:
        1. SENSE: Receive user_input (passed in)
        2. FRAME: Select cognitive frame via FrameEngine.select()
        2b. CLASSIFY: Extract intent signals and plan retrieval (005.1)
        3. RECALL: Build context via ContextEngine.build() with plan
        4. DELIBERATE: If frame warrants it (decision/task/debug),
           start deliberation and record decision_id
        5. EPISODE: If no active episode for this session, start one
        6. WORKING MEMORY: Update via Heart.focus()

        Return TurnContext with system_prompt, frame, decision_id, metadata.

        Args:
            conversation_messages: Recent user messages for dedup (F4).
                Optional — without it, dedup is skipped (backward compat).
            user_id: Optional user identifier for episode tracking.
            user_display_name: Optional user display name for episode tracking.
        """
        # 1b. Track user input for significance (005.5 Phase A)
        meta = self._session_metadata.setdefault(session_id, SessionMetadata())
        meta.total_user_chars += len(user_input)
        # Check for explicit remember request
        if any(kw in user_input.lower() for kw in ("remember this", "remember that", "don't forget", "save this")):
            meta.has_explicit_remember = True

        # 006: Transcript capture
        meta.transcript.append(f"User: {user_input[:500]}")

        # 007.4: Update agents.last_active timestamp
        try:
            async with self._brain.db.session() as _session:
                await _session.execute(sa_update(Agent).where(Agent.id == agent_id).values(last_active=func.now()))
                await _session.commit()
        except Exception:
            logger.debug("Failed to update last_active for agent %s", agent_id)

        # 008: Check initiation state before frame selection
        _is_initiation = False
        if self._identity_manager is not None:
            try:
                _is_initiated = await self._identity_manager.is_initiated(session=session)
                if not _is_initiated:
                    # P2-1: Atomically claim initiation to prevent race with concurrent sessions
                    claimed = await self._identity_manager.claim_initiation(session)
                    if claimed:
                        _is_initiation = True
                        logger.info("Agent %s not initiated — claimed initiation protocol", agent_id)
                    else:
                        # Another session is already running initiation — proceed normally
                        logger.info("Agent %s initiation already claimed by another session", agent_id)
            except Exception:
                logger.warning("Failed to check initiation state, proceeding normally")

        # 2. FRAME — select cognitive frame (F5: agent_id first)
        if _is_initiation:
            # 008: Force initiation frame — restricts tools to store_identity + complete_initiation
            from nous.cognitive.schemas import FrameSelection

            frame = FrameSelection(
                frame_id="initiation",
                frame_name="Initiation",
                confidence=1.0,
                description="Identity initiation protocol",
                questions_to_ask=[],
            )
        else:
            try:
                frame = await self._frames.select(agent_id, user_input, session=session)
            except Exception:
                logger.warning("Frame selection failed, falling back to conversation")
                frame = self._frames._default_selection()

        # Issue #229: Initialize critic skills (populated only in advised mode)
        _critic_skills: list[str] = []

        # F024: Track user messages for frustration detection
        if self._critic and self._settings.critic_enabled:
            user_msgs = self._session_user_messages.setdefault(session_id, [])
            user_msgs.append(user_input)
            if len(user_msgs) > 5:
                self._session_user_messages[session_id] = user_msgs[-5:]

        # F024: Critic Agent pre-turn classification
        if self._critic and self._settings.critic_enabled and not _is_initiation:
            from nous.cognitive.critic_schemas import RoutingMode

            heuristic_frame = frame  # preserve for shadow logging
            try:
                all_frames = await self._frames.list_frames(agent_id, session=session)
                available_frame_ids = [f.frame_id for f in all_frames if f.frame_id != "initiation"]
            except Exception:
                available_frame_ids = ["conversation", "task", "question", "decision", "debug", "creative"]

            tool_history = self._session_tool_history.get(session_id, [])
            critic_result = await self._critic.classify(
                user_message=user_input,
                heuristic_frame=frame,
                available_frames=available_frame_ids,
                tool_call_history=tool_history,
            )

            # In advised mode, override frame selection
            if (
                self._settings.critic_mode == "advised"
                and critic_result.routing == RoutingMode.SINGLE_ADVISED
                and critic_result.recommended_frame != frame.frame_id
            ):
                try:
                    frame = await self._frames.get(
                        critic_result.recommended_frame,
                        agent_id,
                        session=session,
                    )
                    logger.info(
                        "F024 Critic override: %s -> %s (reason: %s, latency=%dms)",
                        heuristic_frame.frame_id,
                        critic_result.recommended_frame,
                        critic_result.rationale,
                        critic_result.latency_ms,
                    )
                except ValueError:
                    logger.warning("F024 Critic recommended unknown frame: %s", critic_result.recommended_frame)
            elif self._settings.critic_mode == "advised":
                logger.info(
                    "F024 Critic advised agree: frame=%s, latency=%dms",
                    frame.frame_id,
                    critic_result.latency_ms,
                )
            elif self._settings.critic_mode == "shadow":
                if critic_result.recommended_frame != frame.frame_id:
                    logger.info(
                        "F024 Critic shadow disagree: heuristic=%s, critic=%s, reason=%s, latency=%dms",
                        frame.frame_id,
                        critic_result.recommended_frame,
                        critic_result.rationale,
                        critic_result.latency_ms,
                    )
                else:
                    logger.info(
                        "F024 Critic shadow agree: frame=%s, latency=%dms",
                        frame.frame_id,
                        critic_result.latency_ms,
                    )
                if critic_result.skills:
                    logger.info(
                        "F024 Critic shadow skills: %s",
                        critic_result.skills,
                    )

            # F024/issue-216: Activate Critic-recommended skills
            activated_skill_ids: list[str] = []
            if self._settings.critic_mode == "advised" and critic_result.skills:
                for skill_name in critic_result.skills:
                    try:
                        proc = await self._heart.get_procedure_by_name(
                            skill_name,
                            session=session,
                        )
                        if proc:
                            await self._heart.activate_procedure(
                                proc.id,
                                session=session,
                            )
                            activated_skill_ids.append(str(proc.id))
                            logger.info(
                                "F024 Critic activated skill: %s (id=%s)",
                                skill_name,
                                proc.id,
                            )
                    except Exception:
                        logger.warning("F024 Critic skill activation failed: %s", skill_name)

            # Emit critic_classified event
            if self._bus:
                try:
                    await self._bus.emit(
                        Event(
                            type="critic_classified",
                            agent_id=agent_id,
                            session_id=session_id,
                            data={
                                "heuristic_frame": heuristic_frame.frame_id,
                                "critic_frame": critic_result.recommended_frame,
                                "routing": critic_result.routing.value,
                                "rationale": critic_result.rationale,
                                "latency_ms": critic_result.latency_ms,
                                "mode": self._settings.critic_mode,
                                "agreed": heuristic_frame.frame_id == critic_result.recommended_frame,
                                "skills": critic_result.skills,
                                "activated_skills": activated_skill_ids,
                            },
                        )
                    )
                except Exception:
                    pass  # non-critical

            # Issue #229: Capture critic skills for context build (advised mode only)
            if self._settings.critic_mode == "advised" and critic_result.skills:
                _critic_skills = critic_result.skills

        # 2b. CLASSIFY — extract intent signals and plan retrieval (005.1)
        signals = self._intent_classifier.classify(user_input, frame)
        plan = self._intent_classifier.plan_retrieval(signals, input_text=user_input)

        # 008.6: Detect recap queries and set temporal boost
        _is_recap = _is_recap_query(user_input)
        _temporal_boost = _is_recap or signals.temporal_recency > 0.5
        # 008.6: Ensure budget boost fires even for bare recap queries without temporal words
        if _is_recap and signals.temporal_recency <= 0.5:
            _effective_recency = 0.8
            signals = IntentSignals(
                frame_type=signals.frame_type,
                entity_mentions=signals.entity_mentions,
                temporal_recency=_effective_recency,
                memory_type_hints=signals.memory_type_hints,
                is_question=signals.is_question,
                is_greeting=signals.is_greeting,
                topic_keywords=signals.topic_keywords,
            )
            plan = self._intent_classifier.plan_retrieval(signals, input_text=user_input)

        # 3. RECALL — build context (or initiation prompt)
        system_prompt = ""
        if _is_initiation:
            # 008: Use initiation prompt instead of normal context
            from nous.identity.protocol import INITIATION_PROMPT

            system_prompt = INITIATION_PROMPT
        recalled_decision_ids: list[str] = []
        recalled_fact_ids: list[str] = []
        recalled_procedure_ids: list[str] = []
        recalled_episode_ids: list[str] = []
        recalled_content_map: dict[str, str] = {}
        recalled_score_map: dict[str, float] = {}
        sections_by_tier: dict[str, str] = {}
        build_result = None
        context_token_estimate = 0
        if not _is_initiation:
            # 008: Load identity from DB for normal turns (review fix P1-3)
            _identity_override = None
            if self._identity_manager is not None:
                try:
                    identity_sections = await self._identity_manager.get_current(session=session)
                    if identity_sections:
                        _identity_override = self._identity_manager.assemble_prompt(identity_sections)
                except Exception:
                    logger.warning("Failed to load identity from DB, using fallback")
        if _is_initiation:
            # Skip normal context build — initiation prompt already set
            context_token_estimate = len(system_prompt) // 4
        try:
            if not _is_initiation:
                build_result = await self._context.build(
                    agent_id,
                    session_id,
                    user_input,
                    frame,
                    session=session,
                    conversation_messages=conversation_messages,
                    retrieval_plan=plan,
                    usage_tracker=self._usage_tracker,
                    identity_override=_identity_override,
                    temporal_boost=_temporal_boost,  # 008.6
                    critic_skills=_critic_skills,  # Issue #229
                )
                system_prompt = build_result.system_prompt
                context_token_estimate = sum(s.token_estimate for s in build_result.sections)
                # F1: Extract recalled IDs from BuildResult
                recalled_decision_ids = build_result.recalled_ids.get("decision", [])
                recalled_fact_ids = build_result.recalled_ids.get("fact", [])
                recalled_procedure_ids = build_result.recalled_ids.get("procedure", [])
                recalled_episode_ids = build_result.recalled_ids.get("episode", [])
                recalled_content_map = build_result.recalled_content_map
                recalled_score_map = build_result.recalled_score_map
                sections_by_tier = build_result.sections_by_tier
        except Exception:
            logger.warning("Context build failed, using identity prompt only")
            system_prompt = self._context._identity_prompt or ""
            build_result = None

        # 3b. SUBTASK RESULTS — inject undelivered results into context
        try:
            undelivered = await self._heart.subtasks.get_undelivered(session_id)
            if undelivered:
                subtask_context = _format_subtask_results(undelivered)
                if subtask_context:
                    system_prompt = system_prompt + "\n\n" + subtask_context
                    delivered_ids = [s.id for s in undelivered]
                    await self._heart.subtasks.mark_delivered(delivered_ids)
                    logger.info(
                        "Injected %d subtask results into session %s",
                        len(undelivered),
                        session_id,
                    )
        except Exception:
            logger.warning("Failed to inject subtask results for session %s", session_id)

        # 4. DELIBERATE — start if frame warrants it
        decision_id: str | None = None
        try:
            if await self._deliberation.should_deliberate(frame):
                decision_id = await self._deliberation.start(
                    agent_id,
                    user_input[:500],
                    frame,
                    session_id=session_id,
                    session=session,
                )
        except Exception:
            logger.warning("Deliberation start failed, continuing without decision_id")
            decision_id = None

        # 5. EPISODE — start if no active episode AND interaction is significant
        if not skip_episode and session_id not in self._active_episodes:
            if self._should_create_episode(session_id, user_input):
                try:
                    # B1: Check for duplicate — skip creation if found
                    # R-P0-2: Do NOT store existing episode IDs in _active_episodes
                    # because end_session would corrupt the original episode.
                    if await self._is_duplicate_episode(user_input[:200], session=session):
                        logger.debug("Skipping episode creation — duplicate found")
                    else:
                        episode_input = EpisodeInput(
                            summary=user_input[:200],
                            frame_used=frame.frame_id,
                            trigger="user_message",
                            user_id=user_id,
                            user_display_name=user_display_name,
                        )
                        episode = await self._heart.start_episode(episode_input, session=session)
                        self._active_episodes[session_id] = str(episode.id)
                except Exception:
                    logger.warning("Failed to start episode for session %s", session_id)

        # 6. WORKING MEMORY — update focus
        # P1-7: Must call get_or_create before focus
        # 007.2 spike: preserve current_task when input is ambiguous/short
        wm_ready = False
        try:
            await self._heart.get_or_create_working_memory(session_id, session=session)
            wm_ready = True
            focus_text = self._resolve_focus_text(user_input)
            if focus_text is not None:
                await self._heart.focus(session_id, focus_text, frame.frame_id, session=session)
            else:
                # F038-2.2: Synthesize task from conversation history when current is empty
                wm = await self._heart.get_working_memory(session_id, session=session)
                if wm and not wm.current_task and conversation_messages:
                    for msg in reversed(conversation_messages[:-1]):  # Skip current message
                        candidate = self._resolve_focus_text(msg)
                        if candidate:
                            await self._heart.focus(session_id, candidate, frame.frame_id, session=session)
                            break
        except Exception:
            logger.warning("Failed to update working memory for session %s", session_id, exc_info=True)

        # 6b. WORKING MEMORY — load recalled items (skip if step 6 failed)
        if build_result is not None and wm_ready:
            try:
                await self._load_recalled_to_working_memory(session_id, build_result, session=session)
            except Exception:
                logger.warning("Failed to load items to working memory", exc_info=True)

        # Get active censor patterns for TurnContext
        active_censors: list[str] = []
        try:
            censors = await self._heart.list_censors(session=session)
            active_censors = [c.trigger_pattern for c in censors]
        except Exception:
            pass

        # Programmatic censor check on user input (#160 follow-up).
        # Subtasks skip censor checks here — they are checked at creation
        # time in spawn_task instead (non-interactive, no user to see blocks).
        censor_injected: dict[str, str] = {}
        censor_blocked = False
        censor_block_reason: str | None = None
        if not is_subtask:
            try:
                matches = await self._heart.check_censors(user_input, session=session)
                for match in matches:
                    if match.action == "block":
                        # F031: Conditional unblock — if trigger_action + unblock_pattern,
                        # execute action and check if results match unblock_pattern.
                        # Match → downgrade to warn (skip block). No match → block as normal.
                        unblocked = False
                        action_result: str | None = None
                        if match.trigger_action:
                            try:
                                action_result = await self._censor_executor.execute(
                                    match.trigger_action,
                                    session=session,
                                )
                            except Exception:
                                logger.warning(
                                    "Censor block action failed (session=%s, censor=%s)",
                                    session_id,
                                    match.id,
                                    exc_info=True,
                                )

                            # Check unblock condition
                            if action_result and match.unblock_pattern:
                                try:
                                    if re.search(match.unblock_pattern, action_result, re.IGNORECASE):
                                        unblocked = True
                                        logger.info(
                                            "Censor UNBLOCK: pattern matched (session=%s, censor=%s)",
                                            session_id,
                                            match.id,
                                        )
                                except re.error:
                                    logger.warning("Invalid unblock_pattern regex: %s", match.unblock_pattern)

                        if unblocked:
                            # Downgrade to warn — inject context like a warn censor
                            logger.info(
                                "Censor BLOCK→WARN downgrade (session=%s, censor=%s): %s",
                                session_id,
                                match.id,
                                match.trigger_pattern,
                            )
                            if action_result:
                                censor_injected[str(match.id)] = action_result
                        else:
                            # Block as normal
                            censor_blocked = True
                            censor_block_reason = f"Blocked by censor: {match.reason or match.trigger_pattern}"
                            logger.warning(
                                "Censor BLOCK on user input (session=%s, censor=%s): %s",
                                session_id,
                                match.id,
                                match.trigger_pattern,
                            )
                            if action_result:
                                censor_block_reason += f"\n\nRelated context:\n{action_result}"
                            if match.action_instruction:
                                censor_block_reason += f"\n\n{match.action_instruction}"
                            break  # One block is enough
                    elif match.action == "warn":
                        logger.info(
                            "Censor WARN on user input (session=%s, censor=%s): %s",
                            session_id,
                            match.id,
                            match.trigger_pattern,
                        )
                        # F031: Execute trigger_action if present
                        if match.trigger_action:
                            try:
                                action_result = await self._censor_executor.execute(
                                    match.trigger_action,
                                    session=session,
                                )
                                if action_result:
                                    censor_injected[str(match.id)] = action_result
                                    logger.info(
                                        "Censor action executed (session=%s, censor=%s, tool=%s)",
                                        session_id,
                                        match.id,
                                        match.trigger_action.get("tool"),
                                    )
                            except Exception:
                                logger.warning(
                                    "Censor action failed (session=%s, censor=%s)",
                                    session_id,
                                    match.id,
                                    exc_info=True,
                                )
            except Exception:
                logger.debug("Censor check failed during pre_turn")

        # F031: Append censor-injected context to system prompt
        if censor_injected:
            injected_section = "\n\n## Censor-Injected Context\n"
            injected_section += "The following information was automatically retrieved by active censors. Use it to inform your response:\n\n"  # noqa: E501
            for censor_id, result_text in censor_injected.items():
                injected_section += f"{result_text}\n\n"
            system_prompt += injected_section

        # F024: Attach pending diagnostic nudges from previous turn
        _diagnostic_nudges = self._pending_nudges.pop(session_id, "")

        return TurnContext(
            system_prompt=system_prompt,
            frame=frame,
            decision_id=decision_id,
            active_censors=active_censors,
            censor_blocked=censor_blocked,
            censor_block_reason=censor_block_reason,
            context_token_estimate=context_token_estimate,
            recalled_decision_ids=recalled_decision_ids,
            recalled_fact_ids=recalled_fact_ids,
            recalled_procedure_ids=recalled_procedure_ids,
            recalled_episode_ids=recalled_episode_ids,
            recalled_content_map=recalled_content_map,
            recalled_score_map=recalled_score_map,
            diagnostic_nudges=_diagnostic_nudges,
            censor_injected_context=censor_injected,
            sections_by_tier=sections_by_tier,
        )

    async def post_turn(
        self,
        agent_id: str,
        session_id: str,
        turn_result: TurnResult,
        turn_context: TurnContext,
        session: AsyncSession | None = None,
    ) -> Assessment:
        """ACT (done) -> MONITOR -> LEARN — process LLM output.

        Steps:
        1. MONITOR: Assess the turn via MonitorEngine.assess()
        2. LEARN: Extract lessons via MonitorEngine.learn()
        3. DELIBERATION: If decision_id exists, finalize deliberation
           - Confidence: 0.8 if no errors, 0.5 if tool errors, 0.3 if turn error
        4. Emit "turn_completed" event via Brain.emit_event()

        Return Assessment.
        """
        decision_id = turn_context.decision_id
        episode_id = self._active_episodes.get(session_id)

        # 1. MONITOR — assess
        try:
            assessment = await self._monitor.assess(
                agent_id,
                session_id,
                turn_result,
                decision_id=decision_id,
                session=session,
            )
        except Exception:
            logger.warning("Assessment failed, using default")
            assessment = Assessment(actual=turn_result.response_text[:200])

        # 2. LEARN — extract lessons
        try:
            assessment = await self._monitor.learn(
                agent_id,
                session_id,
                assessment,
                turn_result,
                turn_context.frame,
                episode_id=episode_id,
                session=session,
            )
        except Exception:
            logger.warning("Learning failed during post_turn")

        # 3. DELIBERATION — finalize if decision exists
        if decision_id:
            if self._is_informational(turn_result):
                # 006.2: Delete orphaned deliberation for informational responses
                # (no value in keeping "[abandoned — informational response]" records)
                logger.debug("Deleting deliberation %s: informational response", decision_id)
                try:
                    await self._deliberation.delete(decision_id, session=session)
                except Exception:
                    logger.debug("Failed to delete deliberation %s", decision_id)
            else:
                # Capture thinking blocks as deliberation trace (best-effort)
                if turn_result.thinking_blocks:
                    for thinking in turn_result.thinking_blocks:
                        try:
                            await self._deliberation.think(
                                decision_id,
                                thinking[:2000],
                                agent_id,
                                session=session,
                            )
                        except Exception:
                            logger.debug("Failed to capture thinking block for %s", decision_id)
                    logger.info(
                        "Captured %d thinking blocks for decision %s",
                        len(turn_result.thinking_blocks),
                        decision_id,
                    )

                # Finalize deliberation (always attempted, even if think() failed)
                try:
                    has_tool_errors = any(tr.error for tr in turn_result.tool_results)
                    if turn_result.error is not None:
                        confidence = 0.3
                    elif has_tool_errors:
                        confidence = 0.5
                    else:
                        confidence = 0.8

                    await self._deliberation.finalize(
                        decision_id,
                        description=turn_result.response_text[:500],
                        confidence=confidence,
                        has_tool_errors=has_tool_errors,
                        session=session,
                    )
                except Exception:
                    logger.warning("Failed to finalize deliberation for %s", decision_id)

        # 4. USAGE TRACKING — record which recalled memories were referenced (005.1)
        if self._usage_tracker and turn_context.recalled_content_map:
            response_text = turn_result.response_text
            _all_recalled: list[tuple[str, str, str]] = []  # (memory_id, memory_type, content)
            for mid in turn_context.recalled_decision_ids:
                content = turn_context.recalled_content_map.get(mid, "")
                _all_recalled.append((mid, "decision", content))
            for mid in turn_context.recalled_fact_ids:
                content = turn_context.recalled_content_map.get(mid, "")
                _all_recalled.append((mid, "fact", content))
            for mid in turn_context.recalled_procedure_ids:
                content = turn_context.recalled_content_map.get(mid, "")
                _all_recalled.append((mid, "procedure", content))
            for mid in turn_context.recalled_episode_ids:
                content = turn_context.recalled_content_map.get(mid, "")
                _all_recalled.append((mid, "episode", content))

            for mid, mem_type, content in _all_recalled:
                if content:
                    overlap = UsageTracker.compute_overlap(content, response_text)
                    # F017 Phase 6: Multi-level strength detection
                    if overlap >= 0.5:
                        strength = 1.0  # Direct reference
                    elif overlap >= 0.25:
                        strength = 0.5  # Paraphrase
                    elif overlap >= 0.10:
                        strength = 0.2  # Topic overlap
                    else:
                        strength = 0.0  # Not referenced
                    self._usage_tracker.record_retrieval(
                        memory_id=mid,
                        memory_type=mem_type,
                        was_referenced=strength > 0,
                        overlap_score=overlap,
                    )

        # F012: Procedure reinforcement — record outcomes for procedures in context
        if turn_context.recalled_procedure_ids:
            has_any_error = turn_result.error is not None or any(tr.error for tr in turn_result.tool_results)
            proc_outcome = "failure" if has_any_error else "success"
            for proc_id_str in turn_context.recalled_procedure_ids:
                try:
                    from uuid import UUID as _UUID

                    pid = _UUID(proc_id_str)
                    await self._heart.activate_procedure(pid, session=session)
                    await self._heart.record_procedure_outcome(
                        pid,
                        proc_outcome,
                        frame_type=turn_context.frame.frame_id if turn_context.frame else None,
                        session=session,
                    )
                except Exception:
                    logger.debug("Failed to reinforce procedure %s", proc_id_str)

        # Post-turn censor check on model output (#160 follow-up).
        # Catches credential leaks, blocked content in model responses.
        # Only logs — doesn't block (response already sent in streaming).
        # Increments activation_count for monitoring/escalation.
        try:
            output_matches = await self._heart.check_censors(
                turn_result.response_text,
                session=session,
            )
            for match in output_matches:
                if match.action == "block":
                    logger.warning(
                        "Censor BLOCK on model output (session=%s, censor=%s): %s",
                        session_id,
                        match.id,
                        match.trigger_pattern,
                    )
                elif match.action == "warn":
                    logger.info(
                        "Censor WARN on model output (session=%s, censor=%s): %s",
                        session_id,
                        match.id,
                        match.trigger_pattern,
                    )
        except Exception:
            logger.debug("Censor check failed during post_turn")

        # F031: Post-turn compliance check for censor-injected context
        if turn_context.censor_injected_context:
            compliance = _check_censor_compliance(
                turn_context.censor_injected_context,
                turn_result.response_text,
            )
            for censor_id, used in compliance.items():
                if used:
                    logger.info(
                        "Censor compliance: agent referenced injected context (session=%s, censor=%s)",
                        session_id,
                        censor_id,
                    )
                else:
                    logger.warning(
                        "Censor compliance: agent did NOT reference injected context (session=%s, censor=%s)",
                        session_id,
                        censor_id,
                    )

        # 5. Update session metadata for significance tracking (005.5)
        meta = self._session_metadata.setdefault(session_id, SessionMetadata())
        meta.turn_count += 1
        meta.total_assistant_chars += len(turn_result.response_text)
        # Track tool usage (set — O(1) add)
        for tr in turn_result.tool_results:
            meta.tools_used.add(tr.tool_name)

        # 006: Transcript capture
        meta.transcript.append(f"Assistant: {turn_result.response_text[:500]}")

        # 6b. WORKING MEMORY — manage open threads based on tool results
        try:
            if any(tr.error for tr in turn_result.tool_results):
                error_tools = [tr.tool_name for tr in turn_result.tool_results if tr.error]
                thread = OpenThread(
                    description=f"Tool errors in: {', '.join(error_tools)}",
                    priority="high",
                    created_at=datetime.now(UTC),
                )
                await self._heart.add_thread(session_id, thread, session=session)

            successful_tools = [tr.tool_name for tr in turn_result.tool_results if not tr.error]
            for tool_name in successful_tools:
                await self._heart.resolve_thread(session_id, tool_name, session=session)
        except Exception:
            logger.warning("Failed to update working memory threads for session %s", session_id)

        # 7. EMIT EVENT — P1-1: bus.emit with backward compat else branch
        event_data = {
            "session_id": session_id,
            "frame": turn_context.frame.frame_id,
            "surprise_level": assessment.surprise_level,
            "decision_id": decision_id,
            "has_errors": turn_result.error is not None,
        }
        try:
            if self._bus:
                _turn_event = Event(
                    type="turn_completed",
                    agent_id=agent_id,
                    session_id=session_id,
                    data=event_data,
                )
                _turn_event.trace_id = _turn_event.event_id  # Root event
                await self._bus.emit(_turn_event)
            else:
                await self._brain.emit_event("turn_completed", event_data, session=session)
        except Exception:
            logger.warning("Failed to emit turn_completed event")

        # F024: Track tool calls and run diagnostic critics
        if self._critic and self._settings.critic_enabled:
            if turn_result.tool_results:
                history = self._session_tool_history.setdefault(session_id, [])
                for tr in turn_result.tool_results:
                    entry: dict[str, Any] = {"tool": tr.tool_name, "args": str(tr.arguments)[:200]}
                    if isinstance(tr.arguments, dict):
                        entry["query"] = tr.arguments.get("query", "")
                        entry["confidence"] = tr.arguments.get("confidence")
                    history.append(entry)
                self._session_tool_history[session_id] = history[-20:]

            resp_lengths = self._session_response_lengths.setdefault(session_id, [])
            resp_lengths.append(len(turn_result.response_text))

            meta_for_diag = self._session_metadata.get(session_id)
            turn_num = meta_for_diag.turn_count if meta_for_diag else 1
            diagnostics = self._critic.run_diagnostics(
                self._session_tool_history.get(session_id, []),
                turn_number=turn_num,
                current_frame=turn_context.frame.frame_id if turn_context else "",
                response_lengths=resp_lengths,
                recent_user_messages=self._session_user_messages.get(session_id),
            )
            nudges = self._critic.format_nudges(diagnostics)
            if nudges:
                self._pending_nudges[session_id] = nudges
                logger.info("F024 diagnostic nudge queued for session %s", session_id)

        return assessment

    # ------------------------------------------------------------------
    # Informational detection (006.2, expanded 007.3)
    # ------------------------------------------------------------------

    # 007.3: Expanded keyword patterns for informational detection
    _INFO_PATTERNS = [
        # Status & inventory
        "current status",
        "available tools",
        "here's what",
        "here is what",
        "here are the",
        "summary of",
        # Memory recall
        "i remember",
        "my memory",
        "what i know",
        "i recall",
        "from memory",
        "i found",
        # Git / repo status
        "repo pulled",
        "repo is at",
        "git pull",
        "latest commit",
        "new branch",
        "new pr",
        "commits since",
        "merged to main",
        # Acknowledgment / confirmation
        "got it",
        "understood",
        "noted",
        "will do",
        "sure thing",
        "okay,",
        "alright,",
        # Simple answers
        "the answer is",
        "it means",
        "this is because",
        "that's correct",
        "you're right",
        # Lists / enumerations
        "here's a list",
        "the following",
        # 009.5: Completion / status updates
        "done!",
        "done.",
        "completed!",
        "finished!",
        "on it!",
        "created!",
        "pushed to",
        "review complete",
        "spec scores",
        "task is running",
        # 009.5: Transition phrases
        "now let me",
        "next i'll",
        "moving on to",
        "let me check",
        "let me look",
        "i'll start",
        "starting with",
        # 009.5: Report phrases
        "here's the result",
        "here are the results",
        "pr #",
        "pr created",
    ]

    # 007.3: Emoji header pattern — status dump indicator
    _EMOJI_HEADER_RE = re.compile(r"^[\U0001f300-\U0001f9ff\u2600-\u27bf]\s")

    # 007.2 spike: pronouns and short phrases that signal a follow-up, not a new topic
    _FOLLOWUP_PRONOUNS = {"it", "that", "this", "them", "they", "those", "these", "he", "she"}
    _FOLLOWUP_STARTERS = (
        "what about",
        "how about",
        "tell me more",
        "more about",
        "and what",
        "and how",
        "what else",
        "anything else",
        "go on",
        "continue",
        "keep going",
        "elaborate",
    )
    # Single-word question starters — only treated as follow-up when alone
    _FOLLOWUP_QUESTION_WORDS = {"why", "how", "when", "where", "who"}
    _FOLLOWUP_STOP_WORDS = frozenset(
        {
            "the",
            "and",
            "for",
            "are",
            "was",
            "were",
            "has",
            "have",
            "does",
            "did",
            "can",
            "could",
            "would",
            "should",
            "will",
            "not",
            "but",
            "with",
            "from",
            "about",
            "what",
            "how",
            "is",
            "a",
            "an",
            "do",
            "its",
            "it's",
            "what's",
            "right",
            "really",
            "sure",
            "just",
            "so",
            "then",
            "well",
            "ok",
        }
    )

    def _resolve_focus_text(self, user_input: str) -> str | None:
        """Return the text to set as current_task, or None to preserve existing topic.

        Heuristic: if the input is short and looks like a follow-up
        (pronouns, continuation phrases), keep the existing topic.
        Only update when the user provides a clear new topic signal.
        """
        text = user_input.strip()
        # Very short inputs are almost always follow-ups
        if len(text) < 5:
            return None

        words = text.lower().split()
        # Single pronoun or short pronoun phrase ("it works", "that one")
        if len(words) <= 3 and words[0] in self._FOLLOWUP_PRONOUNS:
            return None

        text_lower = text.lower()

        # Bare question word ("why?", "how?") — preserve topic
        stripped = text_lower.rstrip("?!. ")
        if stripped in self._FOLLOWUP_QUESTION_WORDS:
            return None

        # Starts with a follow-up phrase (tuple for efficient startswith)
        for starter in self._FOLLOWUP_STARTERS:
            if text_lower.startswith(starter):
                # "tell me more about X" / "more about X" — if there's a clear object, use it
                remainder = text_lower[len(starter) :].strip()
                if starter in ("tell me more", "more about") and len(remainder) > 3:
                    return text[:200]
                return None

        # Pronoun-only subject (e.g., "what about that?", "is that right?")
        if len(words) <= 5:
            non_stop = [w.rstrip("?!.,") for w in words if w.rstrip("?!.,") not in self._FOLLOWUP_STOP_WORDS]
            if non_stop and all(w in self._FOLLOWUP_PRONOUNS for w in non_stop):
                return None

        return text[:200]

    async def _load_recalled_to_working_memory(
        self,
        session_id: str,
        build_result: BuildResult,
        session: AsyncSession | None = None,
    ) -> None:
        """Load high-scoring recalled items into working memory.

        Filters to items with score >= 0.7 (matches context engine render
        threshold), caps at 10 items (half of max_items=20 capacity), and
        sorts by score descending.

        Items loaded this turn appear in the NEXT turn's context because
        working memory is read at step 4 and loaded at step 6b.
        """
        score_map = build_result.recalled_score_map
        content_map = build_result.recalled_content_map
        recalled_ids = build_result.recalled_ids

        # Collect (memory_type, memory_id, score, content) tuples
        candidates: list[tuple[str, str, float, str]] = []
        for mem_type, id_list in recalled_ids.items():
            for mid in id_list:
                score = score_map.get(mid, 0)
                if score >= 0.7:
                    content = content_map.get(mid, "")
                    candidates.append((mem_type, mid, score, content))

        if not candidates:
            return

        # Sort by score descending, take top 10
        candidates.sort(key=lambda c: c[2], reverse=True)
        candidates = candidates[:10]

        now = datetime.now(UTC)
        for mem_type, mid, score, content in candidates:
            item = WorkingMemoryItem(
                type=mem_type,
                ref_id=UUID(mid),
                summary=content[:200],
                relevance=min(score, 1.0),
                loaded_at=now,
            )
            await self._heart.load_to_working_memory(session_id, item, session=session)

    def _is_informational(self, turn_result: TurnResult) -> bool:
        """Detect responses that are information, not decisions (006.2, 007.3, 009.5).

        Returns True when the response is a status dump, memory recall,
        acknowledgment, or list that should NOT be recorded as a decision.

        Checks (in order):
        1. If record_decision tool was called -> always a real decision
        2. Keyword patterns (expanded 007.3, 009.5)
        3. Structural: emoji header (status dump pattern)
        4. Structural: very short response (< 50 chars) without tools
        5. Structural: list-dominated response (> 60% bullet lines)
        6. Action report: tools used + response summarizes what was done (009.5)
        """
        # If agent explicitly recorded a decision, it's real
        tools_used = {r.tool_name for r in turn_result.tool_results}
        if "record_decision" in tools_used:
            return False

        response = turn_result.response_text
        response_lower = response[:500].lower()

        # 1. Keyword patterns
        if any(p in response_lower for p in self._INFO_PATTERNS):
            return True

        # 2. Emoji header (status dump pattern)
        if self._EMOJI_HEADER_RE.match(response[:10]):
            return True

        # 3. Very short response without tools = likely acknowledgment
        if len(response.strip()) < 50 and not tools_used:
            return True

        # 4. List-dominated response (> 60% lines start with bullets)
        lines = [line.strip() for line in response.split("\n") if line.strip()]
        if len(lines) > 3:
            list_lines = sum(1 for line in lines if line[:1] in ("-", "*", "\u2022"))
            if list_lines / len(lines) > 0.6:
                return True

        # 5. Action report: tools used + response summarizes what was done (009.5)
        if self._is_action_report(turn_result):
            return True

        return False

    # 009.5: Report markers for action report detection
    _ACTION_REPORT_MARKERS = [
        "done",
        "created",
        "updated",
        "fixed",
        "merged",
        "pushed",
        "committed",
        "deployed",
        "sent",
        "saved",
        "completed",
        "finished",
        "resolved",
        "applied",
    ]

    def _is_action_report(self, turn_result: TurnResult) -> bool:
        """Detect responses that report completed actions, not decisions (009.5).

        Pattern: tool calls happened + response summarizes what was done.
        If 2+ report markers in first 300 chars after tool use -> action report.
        """
        if not turn_result.tool_results:
            return False

        response_lower = turn_result.response_text[:300].lower()
        matches = sum(1 for m in self._ACTION_REPORT_MARKERS if m in response_lower)
        return matches >= 2

    # ------------------------------------------------------------------
    # Episode significance & dedup (005.5)
    # ------------------------------------------------------------------

    def _should_create_episode(self, session_id: str, user_input: str) -> bool:
        """Determine if this interaction is significant enough for an episode.

        Creates episode when ANY of:
        - First turn of session (turn_count == 0)
        - Session has 2+ turns (multi-turn conversation)
        - Tools were used (indicates real work)
        - Combined content exceeds 200 chars AND turn_count >= 1
        - User explicitly asks to remember something

        Always creates on first turn of a session to avoid losing
        the start of significant conversations. The episode will be
        retroactively discarded at end_session if it stays trivial.

        R-P0-1: Check turn_count == 0 (not meta is None) because
        pre_turn tracking creates metadata via setdefault() BEFORE
        this method runs. meta is never None after first pre_turn.
        """
        meta = self._session_metadata.get(session_id)
        if meta is None or meta.turn_count == 0:
            # First turn — always create (will filter at end if trivial)
            return True

        # Explicit remember request
        if meta.has_explicit_remember:
            return True

        # Tools were used — real work happened
        if meta.tools_used:
            return True

        # Multi-turn conversation
        if meta.turn_count >= _MIN_TURNS_WITHOUT_TOOLS:
            return True

        # Content threshold (need at least 1 prior turn)
        # R-P1-1: Don't add len(user_input) — already in meta.total_user_chars
        total_chars = meta.total_user_chars + meta.total_assistant_chars
        if total_chars >= _MIN_CONTENT_LENGTH and meta.turn_count >= 1:
            return True

        return False

    async def _is_duplicate_episode(
        self,
        summary: str,
        session: AsyncSession | None = None,
    ) -> bool:
        """Check if a similar recent episode already exists.

        Returns True if a recent episode (within 48h) with >0.85 cosine
        similarity exists, meaning we should skip creating a new episode.

        R-P0-2: Returns bool, NOT episode_id. We never store reused IDs in
        _active_episodes because end_session would corrupt/delete the
        original episode.

        R-P1-2: Uses direct cosine similarity via EmbeddingProvider, NOT
        hybrid_search (which returns vector*w + keyword*(1-w) combined scores
        that max at ~0.79 at default weight — making 0.85 unreachable).

        R-P1-3: Filters to episodes started within last 48 hours to avoid
        matching ancient episodes about similar topics.
        """
        if not self._heart.episodes.embeddings:
            return False  # No embeddings available — skip dedup

        try:
            # Generate embedding for current input
            query_embedding = await self._heart.episodes.embeddings.embed(summary)

            # Search recent episodes with direct cosine similarity
            results = await self._heart.search_recent_episodes_by_embedding(
                query_embedding,
                hours=48,
                limit=1,
                session=session,
            )
            if results and results[0][1] > 0.85:
                logger.debug(
                    "Found duplicate episode (%.2f cosine similarity), skipping creation",
                    results[0][1],
                )
                return True
        except Exception:
            logger.warning("Episode dedup check failed, proceeding with creation")
        return False

    # ------------------------------------------------------------------
    # Pre-compaction (008.1 Phase 3)
    # ------------------------------------------------------------------

    async def pre_compaction(
        self,
        agent_id: str,
        session_id: str,
        message_snapshot: list[dict[str, Any]],
    ) -> None:
        """Emit pre-compaction event and bump episode compaction count.

        Called by runner BEFORE compact() mutates the conversation.
        The message_snapshot is a copy of messages[:cut_point], decoupled
        from mutation timing so handlers can safely process it.

        Issue #169: Instead of ending the current episode and starting a new
        one (which polluted the graph with generic edges), we keep the
        episode open and increment its compaction_count.
        """
        # 1. Episode — keep open, bump compaction count
        episode_id = self._active_episodes.get(session_id)
        if episode_id:
            try:
                await self._heart.bump_episode_compaction_count(UUID(episode_id))
                logger.debug("Bumped compaction count on episode %s", episode_id)
            except Exception:
                logger.warning("Failed to bump compaction count on episode %s", episode_id, exc_info=True)

        # 2. Emit event — handlers get the snapshot, not live state
        if self._bus:
            await self._bus.emit(
                Event(
                    type="conversation_compacting",
                    agent_id=agent_id,
                    session_id=session_id,
                    data={"message_snapshot": message_snapshot},
                )
            )

    async def end_session(
        self,
        agent_id: str,
        session_id: str,
        reflection: str | None = None,
        session: AsyncSession | None = None,
    ) -> None:
        """Clean up session state with optional reflection.

        1. If active episode exists for this session:
           - Check if trivial (single turn, no tools, short content) — soft-delete
           - Otherwise end with outcome="success" and optional lessons
        2. If reflection provided, extract facts:
           - Parse for "learned: ..." lines (P2-9)
           - Store each as a fact via Heart.learn() with source="reflection"
           - P1-5: Construct FactInput pydantic model
        3. Remove from self._active_episodes and _session_metadata
        4. Emit "session_ended" event
        """
        # 1. End active episode (or discard if trivial)
        episode_id = self._active_episodes.pop(session_id, None)
        meta = self._session_metadata.pop(session_id, None)

        # F025 P3-C: Compute transcript before end_episode so it can be persisted
        transcript_text = "\n\n".join(meta.transcript) if meta else ""

        if episode_id:
            try:
                # Discard trivial episodes: single turn, no tools, short content
                is_trivial = (
                    meta is not None
                    and meta.turn_count <= 1
                    and not meta.tools_used
                    and (meta.total_user_chars + meta.total_assistant_chars) < _MIN_CONTENT_LENGTH
                )

                if is_trivial:
                    # Soft-delete the episode instead of keeping noise
                    await self._heart.deactivate_episode(UUID(episode_id), session=session)
                    logger.debug("Discarded trivial episode %s", episode_id)
                else:
                    lessons = None
                    if reflection:
                        lessons = [reflection[:500]]
                    await self._heart.end_episode(
                        UUID(episode_id),
                        outcome="success",
                        lessons_learned=lessons,
                        transcript=transcript_text or None,  # F025 P3-C
                        session=session,
                    )
            except Exception:
                logger.warning("Failed to end episode %s", episode_id)

        # 2. Extract facts from reflection
        facts_extracted = 0
        if reflection:
            # P2-9: Parse "learned: X" lines
            matches = _LEARNED_PATTERN.findall(reflection)
            for learned_text in matches:
                learned_text = learned_text.strip()
                if not learned_text:
                    continue
                try:
                    # P1-5: Construct FactInput pydantic model
                    fact_input = FactInput(
                        content=learned_text,
                        source="reflection",
                        category="rule",
                    )
                    await self._heart.learn(fact_input, session=session)
                    facts_extracted += 1
                except Exception:
                    logger.warning("Failed to extract fact from reflection: %s", learned_text[:50])

        # 3. Clean up working memory for this session
        try:
            await self._heart.clear_working_memory(session_id, session=session)
        except Exception:
            logger.warning("Failed to clear working memory for session %s", session_id)

        # 3b. Clean up monitor session censor counts
        self._monitor._session_censor_counts.pop(session_id, None)

        # F024: Clean up critic session state
        self._session_tool_history.pop(session_id, None)
        self._pending_nudges.pop(session_id, None)
        self._session_response_lengths.pop(session_id, None)
        self._session_user_messages.pop(session_id, None)

        # 4. Emit session_ended event — 006: bus.emit with backward compat
        # transcript_text already computed above for end_episode (F025 P3-C)
        event_data = {
            "session_id": session_id,
            "episode_id": episode_id,
            "transcript": transcript_text,
            "reflection": reflection[:200] if reflection else None,
            "had_reflection": reflection is not None,
            "facts_extracted": facts_extracted,
        }
        try:
            if self._bus:
                _session_event = Event(
                    type="session_ended",
                    agent_id=agent_id,
                    session_id=session_id,
                    data=event_data,
                )
                _session_event.trace_id = _session_event.event_id  # Root event
                await self._bus.emit(_session_event)
            else:
                await self._brain.emit_event("session_ended", event_data, session=session)
        except Exception:
            logger.warning("Failed to emit session_ended event")
