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

    _CLASSIFICATION_PROMPT = """\
You are the Critic Agent for Nous, a cognitive AI system. Your role is to
analyze the user's message and decide how Nous should process it.

AVAILABLE FRAMES:
{available_frames}

USER MESSAGE:
{user_message}

DECIDE:
1. complexity: "simple" | "moderate" | "complex"
2. routing: "single" (one frame, best choice)
3. frames: list with exactly 1 frame name (the best choice for this message)
4. skills: list of skill names to activate (empty if none relevant)
5. rationale: brief explanation of why this frame
6. per_frame_instructions: {{}} (reserved for future phases)

Respond ONLY with valid JSON. No markdown, no explanation outside the JSON."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._api: Any = None
        # Known limitation: cooldowns are instance-level, not session-scoped.
        # In the current single-agent runtime this is acceptable. For multi-session
        # scenarios, scope to dict[str, dict[str, int]] keyed by session_id.
        self._diagnostic_cooldowns: dict[str, int] = {}
        self._current_turn: int = 0

    def set_api_client(self, client: Any) -> None:
        """Set the shared AnthropicClient for LLM calls."""
        self._api = client

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

    # ------------------------------------------------------------------
    # LLM Classification
    # ------------------------------------------------------------------

    async def classify(
        self,
        user_message: str,
        heuristic_frame: FrameSelection,
        available_frames: list[str],
        tool_call_history: list[dict[str, Any]] | None = None,
    ) -> CriticResult:
        """Classify user message and recommend frame.

        Uses call_background_llm() with timeout protection.
        Falls back to heuristic frame on any error.
        """
        start_ms = int(time.time() * 1000)

        if not self._needs_critic(user_message, tool_call_history or []):
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

        prompt = self._CLASSIFICATION_PROMPT.format(
            available_frames="\n".join(f"- {f}" for f in available_frames),
            user_message=user_message,
        )

        try:
            from nous.handlers import call_background_llm

            timeout_s = self._settings.critic_max_latency_ms / 1000.0
            raw_text = await asyncio.wait_for(
                call_background_llm(
                    self._api,
                    self._settings.critic_model,
                    "You are a cognitive routing classifier. Respond only with JSON.",
                    prompt,
                    max_tokens=512,
                ),
                timeout=timeout_s,
            )

            if raw_text is None:
                raw_text = ""

            parsed = self._parse_classification(raw_text, heuristic_frame)
            elapsed = int(time.time() * 1000) - start_ms
            parsed.heuristic_frame = heuristic_frame.frame_id
            parsed.latency_ms = elapsed
            return parsed

        except asyncio.TimeoutError:
            elapsed = int(time.time() * 1000) - start_ms
            logger.warning("CriticAgent classification timed out after %dms", elapsed)
            return CriticResult(
                routing=RoutingMode.SINGLE_ADVISED,
                recommended_frame=heuristic_frame.frame_id,
                rationale="Fallback: classification timed out",
                heuristic_frame=heuristic_frame.frame_id,
                latency_ms=elapsed,
            )
        except Exception as e:
            elapsed = int(time.time() * 1000) - start_ms
            logger.warning("CriticAgent classification failed: %s", e)
            return CriticResult(
                routing=RoutingMode.SINGLE_ADVISED,
                recommended_frame=heuristic_frame.frame_id,
                rationale="Fallback: classification error",
                heuristic_frame=heuristic_frame.frame_id,
                latency_ms=elapsed,
            )

    def _parse_classification(
        self,
        raw_text: str,
        heuristic_frame: FrameSelection,
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

            return CriticResult(
                routing=RoutingMode.SINGLE_ADVISED,
                recommended_frame=recommended,
                rationale=data.get("rationale", ""),
                complexity=data.get("complexity", "moderate"),
                skills=data.get("skills", []),
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

    def _check_confidence_drift(self, tool_history: list[dict[str, Any]]) -> DiagnosticResult:
        """Multiple low-confidence decisions in sequence."""
        name = "confidence_drift"
        if self._on_cooldown(name):
            return self._not_fired(name)
        decisions = [tc for tc in tool_history if tc.get("tool") == "record_decision"]
        if len(decisions) >= 3:
            confidences = [d.get("confidence", 0.5) for d in decisions[-3:]]
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
