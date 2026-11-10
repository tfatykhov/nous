"""F024 Critic Agent — Smart Frame Selector (Phase 0).

A lightweight secondary agent (B-Brain) that classifies user messages
and recommends the optimal cognitive frame. Operates in shadow mode
first, logging recommendations alongside heuristic choices.

Phase 0: Single LLM call for classification, no parallelism.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import Counter
from typing import Any

from nous.cognitive.critic_schemas import CriticResult, DiagnosticResult, RoutingMode
from nous.cognitive.schemas import FrameSelection
from nous.config import Settings
from nous.heart.procedures import ProcedureManager

_PREVIOUS_MESSAGE_CHARS = 1200  # F110: how much of the previous assistant message the critic sees

logger = logging.getLogger(__name__)

_ACTION_SIGNALS = frozenset({
    "research", "build", "compare", "analyze", "decide",
    "write", "find", "create", "review", "check", "implement",
    "design", "debug", "fix", "refactor", "test", "deploy",
})


class CriticAgent:
    """Critic Agent — pre-turn classification and post-turn diagnostics.

    Phase 0: Shadow mode frame selection + diagnostic critics.
    No parallelism, no transactions.
    """

    _DIAGNOSTIC_COOLDOWN = 3  # turns

    # Tool sets for frame mismatch detection
    _TASK_TOOLS = frozenset({"bash", "write_file"})
    _CONVERSATION_TOOLS = frozenset({"recall_deep", "learn_fact"})
    _FRUSTRATION_SIGNALS = frozenset({
        "no", "nope", "wrong", "not what i", "i already", "i said",
        "no i meant", "that's not", "try again",
    })

    # Closes the user turn. In the old single-message prompt the JSON-only rule closed the prompt; after the split it
    # closes the system block, so this repeats it last.
    _ANSWER_REMINDER = "Answer with the JSON object only, no other text."

    # The stable part of the prompt, sent as the cached system block; the user message goes in the user turn and is
    # never passed through .format().
    _SYSTEM_PROMPT = """\
You are the Critic Agent for Nous, a cognitive AI system. Your role is to
analyze the user's message and decide how Nous should process it.
The message to classify is in the user turn, under USER MESSAGE.

AVAILABLE FRAMES:
{available_frames}

AVAILABLE SKILLS:
{skill_catalog}

DECIDE:
1. complexity: "simple" | "moderate" | "complex"
2. routing: "single" (one frame, best choice)
3. frames: list with exactly 1 frame name (the best choice for this message)
4. skills: list of skill names from AVAILABLE SKILLS to activate (empty if none relevant, ONLY use names listed above)
5. rationale: brief explanation of why this frame
6. per_frame_instructions: {{}} (reserved for future phases)

Respond ONLY with valid JSON. No markdown, no explanation outside the JSON."""

    def __init__(self, settings: Settings, procedure_manager: ProcedureManager | None = None) -> None:
        self._settings = settings
        self._api: Any = None
        self._procedure_manager = procedure_manager
        # Known limitation: cooldowns are instance-level, not session-scoped.
        # In the current single-agent runtime this is acceptable. For multi-session
        # scenarios, scope to dict[str, dict[str, int]] keyed by session_id.
        self._diagnostic_cooldowns: dict[str, int] = {}
        self._current_turn: int = 0

    def set_api_client(self, client: Any) -> None:
        """Set the shared AnthropicClient for LLM calls."""
        self._api = client

    async def _build_skill_catalog(self) -> tuple[str, set[str]]:
        """Build compact skill catalog from registered procedures.

        Returns (formatted_catalog_text, set_of_valid_names).
        """
        if self._procedure_manager is None:
            return "No skills registered.", set()
        try:
            summaries, _total = await self._procedure_manager.list_all(
                limit=50, active_only=True,
            )
            if not summaries:
                return "No skills registered.", set()
            valid_names: set[str] = set()
            lines: list[str] = []
            # Name order, so the catalog (part of the cached system block) is byte-stable between calls.
            for s in sorted(summaries, key=lambda p: (p.name.casefold(), p.name)):
                valid_names.add(s.name)
                # Banded to 10 points: every recalled procedure records an outcome, and an exact percent would change
                # the cached block on most task turns.
                eff = f" (effectiveness: ~{round(s.effectiveness, 1):.0%})" if s.effectiveness is not None else ""
                # No brace escaping: the catalog is a .format() value, which str.format never re-parses.
                lines.append(f"- {s.name} — {s.description or 'No description'}{eff}")
            return "\n".join(lines), valid_names
        except Exception:
            logger.warning("Failed to build skill catalog for Critic")
            return "No skills registered.", set()

    # ------------------------------------------------------------------
    # Complexity Gate
    # ------------------------------------------------------------------

    def _needs_critic(
        self,
        message: str,
        tool_call_history: list[dict[str, Any]],
    ) -> bool:
        """Fast heuristic — no LLM call. Errs toward invoking Critic."""
        # Check stuck patterns first (regardless of message length)
        if len(tool_call_history) >= 3:
            recent_tools = [tc.get("tool", "") for tc in tool_call_history[-5:]]
            counts = Counter(recent_tools)
            if counts.most_common(1)[0][1] >= 3:
                return True

        words = message.split()
        max_words = self._settings.critic_passthrough_max_words

        if len(words) <= max_words and not message.rstrip().endswith("?"):
            return False

        sentence_endings = message.count(".") + message.count("?") + message.count("!")
        if sentence_endings > 2:
            return True

        lower_words = set(message.lower().split())
        if len(lower_words & _ACTION_SIGNALS) >= 2:
            return True

        return True

    @staticmethod
    def _previous_message_section(text: str | None) -> str:
        """F110: the PREVIOUS ASSISTANT MESSAGE block of the user turn, or "" without text.

        The text is redacted, stripped of NUL and other control characters (newline and tab stay), then cut to its
        last ``_PREVIOUS_MESSAGE_CHARS`` characters, where an offer usually sits. Fail-open: text that cannot be
        prepared is left out.
        """
        if not text or not isinstance(text, str):
            return ""
        try:
            from nous.cognitive.ledger_store import redact_output

            clean = "".join(c for c in redact_output(text) if c in "\n\t" or c.isprintable())
            tail = clean[-_PREVIOUS_MESSAGE_CHARS:]
        except Exception:
            logger.warning("F110: the critic could not prepare the previous assistant message", exc_info=True)
            return ""
        if not tail.strip():
            return ""
        return "PREVIOUS ASSISTANT MESSAGE (the user may be replying to it):\n" + tail + "\n\n"

    # ------------------------------------------------------------------
    # LLM Classification
    # ------------------------------------------------------------------

    async def classify(
        self,
        user_message: str,
        heuristic_frame: FrameSelection,
        available_frames: list[str],
        tool_call_history: list[dict[str, Any]] | None = None,
        *,
        last_assistant_text: str | None = None,
    ) -> CriticResult:
        """Classify user message and recommend frame.

        F110: with ``frame_context_enabled`` the user turn also carries the heuristic frame and, when
        ``last_assistant_text`` is given, the tail of the previous assistant message (redacted), so a reply such as
        "yes, do it" can be read as the answer to an offer. The system block is not touched.

        Uses call_background_llm() with timeout protection.
        Falls back to heuristic frame on any error.
        """
        start_ms = int(time.time() * 1000)

        if not self._needs_critic(user_message, tool_call_history or []):
            logger.info(
                "F024 Critic passthrough: frame=%s, message=%s",
                heuristic_frame.frame_id, user_message[:80],
            )
            return CriticResult(
                routing=RoutingMode.PASSTHROUGH,
                recommended_frame=heuristic_frame.frame_id,
                rationale="Passthrough: simple message",
                complexity="simple",
                heuristic_frame=heuristic_frame.frame_id,
                latency_ms=0,
            )

        if self._api is None:
            logger.warning("CriticAgent: no API client, falling back to heuristic")
            return CriticResult(
                routing=RoutingMode.PASSTHROUGH,
                recommended_frame=heuristic_frame.frame_id,
                rationale="No API client available",
                heuristic_frame=heuristic_frame.frame_id,
                latency_ms=0,
            )

        # Issue #216: Build skill catalog from registered procedures
        skill_catalog, valid_skill_names = await self._build_skill_catalog()

        # The frames come from an unordered query, and their order carries no meaning: sort them so the system block is
        # byte-stable between calls.
        frames = sorted(available_frames, key=lambda f: (f.casefold(), f))
        system_prompt = self._SYSTEM_PROMPT.format(
            available_frames="\n".join(f"- {f}" for f in frames),
            skill_catalog=skill_catalog,
        )
        prompt = "USER MESSAGE:\n" + user_message + "\n\n" + self._ANSWER_REMINDER
        if self._settings.frame_context_enabled:
            prompt = (
                self._previous_message_section(last_assistant_text)
                + "HEURISTIC FRAME: " + heuristic_frame.frame_id + "\n\n"
                + prompt
            )

        try:
            from nous.handlers import call_background_llm

            timeout_s = self._settings.critic_max_latency_ms / 1000.0
            logger.info(
                "F024 Critic calling LLM: model=%s, timeout=%.1fs, system_len=%d, prompt_len=%d",
                self._settings.critic_model, timeout_s, len(system_prompt), len(prompt),
            )
            raw_text = await asyncio.wait_for(
                call_background_llm(
                    self._api,
                    self._settings.critic_model,
                    system_prompt,
                    prompt,
                    max_tokens=512,
                    purpose="critic",
                ),
                timeout=timeout_s,
            )

            if raw_text is None:
                logger.warning("F024 Critic LLM returned None (call_background_llm failed silently)")
                raw_text = ""
            else:
                logger.info("F024 Critic LLM responded: %s", raw_text[:200])

            parsed = self._parse_classification(raw_text, heuristic_frame, valid_skill_names=valid_skill_names)
            elapsed = int(time.time() * 1000) - start_ms
            parsed.heuristic_frame = heuristic_frame.frame_id
            parsed.latency_ms = elapsed
            return parsed

        except asyncio.TimeoutError:
            elapsed = int(time.time() * 1000) - start_ms
            logger.warning("CriticAgent classification timed out after %dms (limit=%dms)",
                           elapsed, self._settings.critic_max_latency_ms)
            return CriticResult(
                routing=RoutingMode.PASSTHROUGH,
                recommended_frame=heuristic_frame.frame_id,
                rationale="Fallback: classification timed out",
                heuristic_frame=heuristic_frame.frame_id,
                latency_ms=elapsed,
            )
        except Exception as e:
            elapsed = int(time.time() * 1000) - start_ms
            logger.warning("CriticAgent classification failed (%dms): %s", elapsed, e)
            return CriticResult(
                routing=RoutingMode.PASSTHROUGH,
                recommended_frame=heuristic_frame.frame_id,
                rationale="Fallback: classification error",
                heuristic_frame=heuristic_frame.frame_id,
                latency_ms=elapsed,
            )

    def _parse_classification(
        self,
        raw_text: str,
        heuristic_frame: FrameSelection,
        valid_skill_names: set[str] | None = None,
    ) -> CriticResult:
        """Parse LLM JSON response into CriticResult."""
        try:
            text = raw_text.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1] if "\n" in text else text[3:]
                if text.endswith("```"):
                    text = text[:-3]
                text = text.strip()

            data = json.loads(text)
            frames = data.get("frames", [])
            recommended = frames[0] if frames else heuristic_frame.frame_id

            raw_skills = data.get("skills", [])
            if valid_skill_names is not None:
                skills = [s for s in raw_skills if s in valid_skill_names]
            else:
                skills = raw_skills

            return CriticResult(
                routing=RoutingMode.SINGLE_ADVISED,
                recommended_frame=recommended,
                rationale=data.get("rationale", ""),
                complexity=data.get("complexity", "moderate"),
                skills=skills,
                from_model=bool(frames),
            )
        except (json.JSONDecodeError, KeyError, IndexError) as e:
            logger.warning("CriticAgent: failed to parse JSON: %s", e)
            return CriticResult(
                routing=RoutingMode.SINGLE_ADVISED,
                recommended_frame=heuristic_frame.frame_id,
                rationale=f"Parse fallback: {e}",
            )

    # ------------------------------------------------------------------
    # Diagnostic Critics (all 6 from spec)
    # ------------------------------------------------------------------

    def run_diagnostics(
        self,
        tool_call_history: list[dict[str, Any]],
        turn_number: int,
        current_frame: str = "",
        response_lengths: list[int] | None = None,
        recent_user_messages: list[str] | None = None,
    ) -> list[DiagnosticResult]:
        """Run all 6 diagnostic critics against conversation state."""
        self._current_turn = turn_number
        return [
            self._check_repetition(tool_call_history),
            self._check_stuck_loop(tool_call_history),
            self._check_confidence_drift(tool_call_history),
            self._check_frame_mismatch(tool_call_history, current_frame),
            self._check_scope_creep(response_lengths or []),
            self._check_user_frustration(recent_user_messages or []),
        ]

    def format_nudges(self, diagnostics: list[DiagnosticResult]) -> str:
        """Format fired diagnostics as system prompt nudges."""
        fired = [d for d in diagnostics if d.fired]
        if not fired:
            return ""
        lines = [f"[Critic/{d.critic_name}]: {d.intervention}" for d in fired]
        return "\n\n[DIAGNOSTIC OBSERVATIONS]\n" + "\n".join(lines)

    # --- Internal helpers ---

    def _on_cooldown(self, critic_name: str) -> bool:
        last_fired = self._diagnostic_cooldowns.get(critic_name, -100)
        return (self._current_turn - last_fired) < self._DIAGNOSTIC_COOLDOWN

    def _fire(self, critic_name: str, intervention: str) -> DiagnosticResult:
        self._diagnostic_cooldowns[critic_name] = self._current_turn
        return DiagnosticResult(critic_name=critic_name, intervention=intervention, fired=True)

    @staticmethod
    def _not_fired(name: str) -> DiagnosticResult:
        return DiagnosticResult(critic_name=name, intervention="", fired=False)

    def _check_repetition(self, tool_history: list[dict[str, Any]]) -> DiagnosticResult:
        """3+ similar recall_deep queries."""
        name = "repetition"
        if self._on_cooldown(name):
            return self._not_fired(name)
        recall_queries = [
            tc.get("query", tc.get("args", ""))
            for tc in tool_history if tc.get("tool") == "recall_deep"
        ]
        if len(recall_queries) >= 3:
            recent = recall_queries[-3:]
            word_sets = [set(q.lower().split()) for q in recent]
            common = word_sets[0] & word_sets[1] & word_sets[2]
            all_words = word_sets[0] | word_sets[1] | word_sets[2]
            if all_words and len(common) / len(all_words) > 0.3:
                return self._fire(
                    name,
                    "You've searched for similar things multiple times. "
                    "Reformulate the problem or try a different approach.",
                )
        return self._not_fired(name)

    def _check_stuck_loop(self, tool_history: list[dict[str, Any]]) -> DiagnosticResult:
        """Same tool called 3+ times with similar args."""
        name = "stuck_loop"
        if self._on_cooldown(name) or len(tool_history) < 3:
            return self._not_fired(name)
        recent = tool_history[-5:]
        signatures = [f"{tc.get('tool', '')}:{tc.get('args', '')}" for tc in recent]
        counts = Counter(signatures)
        if counts.most_common(1)[0][1] >= 3:
            return self._fire(
                name,
                "Consider a completely different strategy. "
                "The same tool with similar arguments has been called multiple times.",
            )
        return self._not_fired(name)

    @staticmethod
    def _coerce_confidence(value: Any) -> float:
        """Coerce a tool-supplied confidence to float for numeric comparison.

        record_decision confidence arrives from LLM tool args and may be None,
        a float, or a numeric string ("0.8"). Parse numerics; map None and
        non-numeric values to a neutral 0.5 (preserves a real 0.0).
        """
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.5

    def _check_confidence_drift(self, tool_history: list[dict[str, Any]]) -> DiagnosticResult:
        """Multiple low-confidence decisions in sequence."""
        name = "confidence_drift"
        if self._on_cooldown(name):
            return self._not_fired(name)
        decisions = [tc for tc in tool_history if tc.get("tool") == "record_decision"]
        if len(decisions) >= 3:
            # Audit CL-5 (2026-06-09): `.get(k, default)` only applies the
            # default when the key is ABSENT. record_decision may store an
            # explicit confidence=None, which returns None here and crashes the
            # `None < 0.4` comparison below (TypeError) on the non-streaming
            # post_turn path. 2026-06-14: the LLM also passes confidence as a
            # STRING ("0.8") via tool args, which raised "'<' not supported
            # between instances of 'str' and 'float'" in prod. Coerce robustly:
            # parse numeric strings, map None/non-numeric to a neutral 0.5,
            # preserve a real 0.0.
            confidences = [
                self._coerce_confidence(d.get("confidence")) for d in decisions[-3:]
            ]
            if all(c < 0.4 for c in confidences):
                return self._fire(
                    name,
                    "Pause. Multiple recent decisions have low confidence. "
                    "What are you uncertain about? Ask the user.",
                )
        return self._not_fired(name)

    def _check_frame_mismatch(
        self, tool_history: list[dict[str, Any]], current_frame: str,
    ) -> DiagnosticResult:
        """Task-frame tools in conversation context or vice versa."""
        name = "frame_mismatch"
        if self._on_cooldown(name) or not current_frame or len(tool_history) < 2:
            return self._not_fired(name)
        recent_tools = {tc.get("tool", "") for tc in tool_history[-5:]}
        if current_frame == "conversation" and len(recent_tools & self._TASK_TOOLS) >= 2:
            return self._fire(
                name,
                "You're using task-oriented tools in a conversation frame. "
                "Consider switching to task or debug frame.",
            )
        if current_frame in ("task", "debug") and recent_tools <= self._CONVERSATION_TOOLS and len(recent_tools) >= 2:
            return self._fire(
                name,
                "You're only using conversation tools in a task frame. "
                "The frame may not match the actual work.",
            )
        return self._not_fired(name)

    def _check_scope_creep(self, response_lengths: list[int]) -> DiagnosticResult:
        """Response lengths growing consistently."""
        name = "scope_creep"
        if self._on_cooldown(name) or len(response_lengths) < 3:
            return self._not_fired(name)
        recent = response_lengths[-4:]
        if len(recent) >= 3 and all(recent[i] > recent[i - 1] * 1.5 for i in range(1, len(recent))):
            return self._fire(
                name,
                "Focus. Response length is growing rapidly. "
                "What was the user's core ask?",
            )
        return self._not_fired(name)

    def _check_user_frustration(self, recent_user_messages: list[str]) -> DiagnosticResult:
        """Short user messages indicating frustration."""
        name = "user_frustration"
        if self._on_cooldown(name) or len(recent_user_messages) < 2:
            return self._not_fired(name)
        frustration_count = sum(
            1 for msg in recent_user_messages[-3:]
            if any(sig in msg.lower() for sig in self._FRUSTRATION_SIGNALS)
        )
        if frustration_count >= 2:
            return self._fire(
                name,
                "The user may be frustrated. Acknowledge, clarify, "
                "and re-align with their actual request.",
            )
        return self._not_fired(name)
