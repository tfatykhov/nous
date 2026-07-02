"""Monitor engine — post-turn self-assessment and learning.

After each turn:
1. Assess: Was the outcome surprising? Did censors fire?
2. Learn: Extract facts, record episode, create censors from failures.
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from nous.brain.brain import Brain
from nous.cognitive.schemas import Assessment, FrameSelection, ToolResult, TurnResult
from nous.config import Settings
from nous.heart.heart import Heart
from nous.heart.schemas import CensorInput, FactInput

logger = logging.getLogger(__name__)

# F039: Patterns indicating user corrections
_CORRECTION_PATTERNS = [
    "no, actually", "that's wrong", "that's not right", "not what i",
    "you misunderstood", "i meant", "correction:", "no no",
    "wrong,", "that's incorrect", "don't do that", "never do that",
    "stop doing", "i said", "i already told you",
]

# Patterns that indicate transient errors (shouldn't create censors)
_TRANSIENT_PATTERNS = [
    "timeout",
    "rate limit",
    "rate_limit",
    "429",
    "503",
    "connection refused",
    "network error",
    "econnreset",
    "etimedout",
]

# Max auto-created censors per session (P2-4 circuit breaker)
_MAX_CENSORS_PER_SESSION = 3


class MonitorEngine:
    """Post-turn self-assessment and learning.

    After each turn:
    1. Assess: Was the outcome surprising? Did censors fire?
    2. Learn: Extract facts, record episode, create censors from failures.
    """

    def __init__(self, brain: Brain, heart: Heart, settings: Settings) -> None:
        self._brain = brain
        self._heart = heart
        self._settings = settings
        # P2-4: Track censors created per session to enforce cap
        self._session_censor_counts: dict[str, int] = {}
        # F012: Track error→recovery pairs for procedure learning
        self._error_recovery_pairs: dict[str, list[dict]] = {}
        self._last_errors: dict[str, list[dict]] = {}
        self._session_procedure_counts: dict[str, int] = {}
        self._procedure_learner = None  # Set externally if F012 enabled
        self._llm_client = None  # Set externally if F039 correction extraction enabled

    async def assess(
        self,
        agent_id: str,
        session_id: str,
        turn_result: TurnResult,
        decision_id: str | None = None,
        session: AsyncSession | None = None,
    ) -> Assessment:
        """Evaluate what happened during the turn.

        Steps:
        1. If decision_id exists, fetch the decision from Brain
        2. Calculate surprise_level (P2-3: structural checks only):
           - 0.9 if turn_result.error is not None (turn-level error)
           - 0.3 if any tool_result.error exists
           - 0.0 otherwise
        3. Generate censor_candidates from non-transient tool errors
        4. Return Assessment
        """
        intended = None
        if decision_id:
            try:
                detail = await self._brain.get(UUID(decision_id), session=session)
                if detail:
                    intended = detail.description
            except Exception:
                logger.warning("Failed to fetch decision %s for assessment", decision_id)

        # P2-3: Structural surprise only -- no text matching
        surprise_level = 0.0
        if turn_result.error is not None:
            surprise_level = 0.9
        elif any(tr.error for tr in turn_result.tool_results):
            surprise_level = 0.3

        # Generate censor candidates from non-transient tool errors
        censor_candidates: list[str] = []
        for tr in turn_result.tool_results:
            if tr.error and not self._is_transient_error(tr.error):
                censor_candidates.append(self._error_to_censor_text(tr))

        return Assessment(
            decision_id=decision_id,
            intended=intended,
            actual=turn_result.response_text[:200],
            surprise_level=surprise_level,
            censor_candidates=censor_candidates,
        )

    async def learn(
        self,
        agent_id: str,
        session_id: str,
        assessment: Assessment,
        turn_result: TurnResult,
        frame: FrameSelection,
        episode_id: str | None = None,
        session: AsyncSession | None = None,
    ) -> Assessment:
        """Post-assessment learning -- update state and create artifacts.

        Steps:
        1. If surprise_level > 0.7 and censor_candidates exist:
           - Create censors via Heart.add_censor() for each candidate
           - P2-4: Deduplicate by trigger_pattern, cap at 3 per session
           - P1-5: Construct CensorInput pydantic model
           - P1-4: Use action="warn" (not severity)

        2. If decision_id exists and turn_result has no errors:
           - Record thought "Turn completed successfully" via Brain.think()

        3. P2-2: Do NOT end episode here -- only end in end_session()

        4. Update Assessment with facts_extracted count and episode_recorded flag.

        Returns updated Assessment.
        """
        facts_extracted = 0

        # 1. Create censors from high-surprise tool errors
        if assessment.surprise_level > 0.7 and assessment.censor_candidates:
            session_count = self._session_censor_counts.get(session_id, 0)

            # P2-4: Get existing censors for deduplication
            existing_patterns: set[str] = set()
            try:
                existing = await self._heart.list_censors(session=session)
                existing_patterns = {c.trigger_pattern for c in existing}
            except Exception:
                logger.warning("Failed to load existing censors for dedup check")

            for candidate_text in assessment.censor_candidates:
                # P2-4: Cap at max censors per session
                if session_count >= _MAX_CENSORS_PER_SESSION:
                    break

                # P2-4: Skip if censor with same trigger already exists
                if candidate_text in existing_patterns:
                    continue

                try:
                    # P1-5: Construct CensorInput pydantic model
                    # F078: auto-learned censors are provenance="auto" -> capped to steer.
                    censor_input = CensorInput(
                        trigger_pattern=candidate_text,
                        reason="Auto-created from tool error",
                        action="steer",
                        provenance="auto",
                    )
                    await self._heart.add_censor(censor_input, session=session)
                    existing_patterns.add(candidate_text)
                    session_count += 1
                except Exception:
                    logger.warning("Failed to create censor for: %s", candidate_text[:50])

            self._session_censor_counts[session_id] = session_count

        # F012: Track error→recovery pairs for procedure learning
        has_tool_errors = any(tr.error for tr in turn_result.tool_results)
        if has_tool_errors:
            # Record pending errors for this session
            for tr in turn_result.tool_results:
                if tr.error and not self._is_transient_error(tr.error):
                    if session_id not in self._last_errors:
                        self._last_errors[session_id] = []
                    self._last_errors[session_id].append({
                        "tool": tr.tool_name,
                        "error": tr.error[:200],
                    })
        elif self._last_errors.get(session_id):
            # Successful turn after errors = recovery
            recovery_tools = [
                tr.tool_name for tr in turn_result.tool_results if not tr.error
            ]
            if recovery_tools:
                pending = self._last_errors.pop(session_id, [])
                if session_id not in self._error_recovery_pairs:
                    self._error_recovery_pairs[session_id] = []
                for err_info in pending:
                    self._error_recovery_pairs[session_id].append({
                        "error": err_info,
                        "recovery": recovery_tools,
                        "context": turn_result.response_text[:200],
                    })

                # Check if trigger count reached
                pairs = self._error_recovery_pairs[session_id]
                trigger = getattr(self._settings, "procedure_monitor_trigger_count", 3)
                session_proc_count = self._session_procedure_counts.get(session_id, 0)
                max_per_session = getattr(self._settings, "procedure_max_per_session", 1)

                if (
                    len(pairs) >= trigger
                    and session_proc_count < max_per_session
                    and self._procedure_learner is not None
                ):
                    try:
                        await self._try_create_recovery_procedure(session_id, pairs)
                    except Exception:
                        logger.warning("Recovery procedure creation failed")

        # 2. Record success thought if deliberation active and no errors
        if assessment.decision_id and turn_result.error is None:
            has_tool_errors = any(tr.error for tr in turn_result.tool_results)
            if not has_tool_errors:
                try:
                    await self._brain.think(
                        UUID(assessment.decision_id),
                        "Turn completed successfully",
                        session=session,
                    )
                except Exception:
                    logger.warning("Failed to record success thought")

        # 3. P2-2: Do NOT end episode here -- only end in end_session()

        # 4. Update assessment
        assessment.facts_extracted = facts_extracted
        return assessment

    def _is_transient_error(self, error: str) -> bool:
        """Check if error is transient (shouldn't create censors).

        Transient patterns: timeout, rate limit, 429, 503, connection refused,
        network error, ECONNRESET, ETIMEDOUT.
        """
        error_lower = error.lower()
        return any(pattern in error_lower for pattern in _TRANSIENT_PATTERNS)

    async def _try_create_recovery_procedure(
        self, session_id: str, pairs: list[dict]
    ) -> None:
        """F012: Create a recovery procedure from error→recovery pairs."""
        from nous.handlers.procedure_learner import _MONITOR_RECOVERY_PROMPT

        error_summary = "; ".join(
            f"{p['error']['tool']}:{p['error']['error'][:50]}" for p in pairs[:5]
        )
        recovery_summary = "; ".join(
            f"{','.join(p['recovery'])}" for p in pairs[:5]
        )

        result = await self._procedure_learner._call_llm(
            _MONITOR_RECOVERY_PROMPT.format(
                error_pattern=error_summary,
                recovery_actions=recovery_summary,
                context=pairs[-1].get("context", ""),
            )
        )
        if result:
            stored = await self._procedure_learner._is_duplicate(result)
            if not stored:
                from nous.heart.schemas import ProcedureInput
                tags = result.get("tags", [])
                tags.append("auto:monitor_recovery")
                proc_input = ProcedureInput(
                    name=result.get("name", "Recovery procedure"),
                    domain=result.get("domain"),
                    description=result.get("description"),
                    goals=result.get("goals", []),
                    core_patterns=result.get("core_patterns", []),
                    core_tools=result.get("core_tools", []),
                    core_concepts=result.get("core_concepts", []),
                    implementation_notes=result.get("implementation_notes", []),
                    tags=tags,
                )
                await self._procedure_learner._heart.store_procedure(proc_input)
                count = self._session_procedure_counts.get(session_id, 0)
                self._session_procedure_counts[session_id] = count + 1
                logger.info("Created recovery procedure from %d error→recovery pairs", len(pairs))

    async def detect_and_extract_correction(
        self,
        user_message: str,
        ai_response: str,
        session_id: str,
        session: AsyncSession | None = None,
        episode_id: str | None = None,
    ) -> dict | None:
        """F039: Detect if user_message is a correction and extract the principle.

        Returns extraction dict with keys: principle, subject, is_censor,
        censor_pattern, confidence — or None if no correction detected.
        """
        if not self._settings.correction_extraction_enabled:
            return None

        lower = user_message.lower()
        if not any(p in lower for p in _CORRECTION_PATTERNS):
            return None

        if not self._llm_client:
            logger.debug("F039: Correction pattern matched but no LLM client available")
            return None

        from nous.handlers import call_background_llm, parse_llm_json

        cap = self._settings.correction_input_max_chars
        prompt = (
            "The user corrected the AI in this exchange:\n\n"
            f"User: {user_message[:cap]}\n"
            f"AI response: {ai_response[:cap]}\n\n"
            "Extract:\n"
            '1. principle: A generalizable rule the AI should follow (1-2 sentences)\n'
            '2. subject: What topic/domain this rule applies to\n'
            '3. is_censor: true if this is a "never do X" pattern, false if "prefer X over Y"\n'
            '4. censor_pattern: If is_censor=true, a short trigger phrase\n'
            '5. confidence: 0.0-1.0 how generalizable this rule is\n\n'
            "Return ONLY valid JSON."
        )

        correction_max_tokens = self._settings.correction_max_tokens
        correction_min_principle_chars = self._settings.correction_min_principle_chars
        try:
            raw = await call_background_llm(
                self._llm_client,
                self._settings.background_model,
                "You are a correction analysis system. Respond only with JSON.",
                prompt,
                max_tokens=correction_max_tokens,
            )
            if not raw:
                return None

            extraction = parse_llm_json(raw)

            principle = extraction.get("principle", "")
            if not principle or len(principle) < correction_min_principle_chars:
                return None

            # Store as fact
            # F022 follow-up (2026-05-01): tag with source_episode_id so the
            # deterministic linker creates the extracted_from edge.
            ep_uuid = None
            if episode_id:
                try:
                    from uuid import UUID as _UUID
                    ep_uuid = _UUID(episode_id)
                except (ValueError, TypeError):
                    ep_uuid = None
            fact_input = FactInput(
                content=principle,
                category="rule",
                subject=extraction.get("subject"),
                confidence=max(0.0, min(1.0, float(extraction.get("confidence", 0.7)))),
                source="inline_correction",
                tags=["correction", "auto:f039"],
                source_episode_id=ep_uuid,
            )
            await self._heart.learn(fact_input, session=session)

            # Optionally create censor
            if extraction.get("is_censor") and extraction.get("censor_pattern"):
                # F078: auto-learned (F039) censors are provenance="auto" -> capped to steer.
                censor_input = CensorInput(
                    trigger_pattern=extraction["censor_pattern"],
                    reason=f"F039: Inline correction — {principle[:100]}",
                    action="steer",
                    provenance="auto",
                )
                await self._heart.add_censor(censor_input, session=session)

            return extraction

        except Exception:
            logger.warning("F039: Inline correction extraction failed")
            return None

    def _error_to_censor_text(self, tool_result: ToolResult) -> str:
        """Convert a tool error to a censor trigger pattern.

        Format: "Avoid using {tool_name} when {simplified args} -- caused: {error[:100]}"
        """
        args_desc = ", ".join(f"{k}={v}" for k, v in list(tool_result.arguments.items())[:3])
        if args_desc:
            args_desc = f"with {args_desc}"
        error_snippet = (tool_result.error or "")[:100]
        return f"Avoid using {tool_result.tool_name} {args_desc} -- caused: {error_snippet}".strip()
