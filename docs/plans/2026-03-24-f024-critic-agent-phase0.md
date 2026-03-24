# F024 Critic Agent Phase 0 — Smart Frame Selector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a lightweight Critic Agent that replaces the pattern-matching frame selector with LLM-based classification, adds diagnostic critics for stuck-pattern detection, and operates in shadow mode for safe validation.

**Architecture:** The CriticAgent sits between frame selection and context build in `CognitiveLayer.pre_turn()` (after line 265, before line 267 — inside the `else` branch, skipped during initiation). A fast heuristic gate decides if the Critic is needed (~30-40% passthrough). When invoked, the Critic makes one LLM call via `call_background_llm()` (shared `AnthropicClient`, same as all handlers) to classify complexity, pick the optimal frame, and recommend skill activations. Diagnostic critics run in `post_turn()` to detect dysfunction patterns (repetition, stuck loops, frame mismatch, scope creep, confidence drift, user frustration) and inject system-level nudges into the next turn's system prompt. Shadow mode logs Critic recommendations alongside heuristic choices without changing behavior.

**Tech Stack:** Python 3.12+, pydantic v2, async/await, `call_background_llm()` helper (same as all handlers), pytest

**Spec:** `docs/features/F024-critic-agent.md` (Phase 0 only — no parallelism, no transactions)

**Review:** 3-agent review (architect + integration + devil's advocate) completed. Fixes applied:
- P1: Use `FrameEngine.get()` instead of new `select_by_id` (wrong column bug)
- P1: Use `list_frames()` for available frames (undefined variable bug)
- P1: Add session cleanup in `end_session()`
- P1: Add `asyncio.wait_for` timeout on critic LLM call
- P2: Use `call_background_llm()` helper instead of raw payload
- P2: Save `heuristic_frame` before override to preserve for event emission
- P2: Implement all 6 spec diagnostic critics (not just 3)
- P2: Specify exact insertion point in `pre_turn()` (after line 265, inside `else`)
- P2: Add exception-raising test for API failures

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `nous/cognitive/critic.py` | **Create** | CriticAgent class: complexity gate, LLM classification, 6 diagnostic critics |
| `nous/cognitive/critic_schemas.py` | **Create** | Pydantic models: CriticResult, DiagnosticResult, RoutingMode |
| `nous/config.py` | **Modify** | Add `critic_*` settings (enabled, mode, model, max_latency_ms) |
| `nous/cognitive/schemas.py` | **Modify** | Add `diagnostic_nudges` field to TurnContext |
| `nous/cognitive/layer.py` | **Modify** | Wire CriticAgent into pre_turn (frame override) and post_turn (diagnostics), session cleanup |
| `nous/api/runner.py` | **Modify** | Inject diagnostic nudges into system prompt |
| `nous/main.py` | **Modify** | Wire CriticAgent with shared api_client |
| `tests/test_critic.py` | **Create** | Unit tests for CriticAgent |
| `tests/test_critic_integration.py` | **Create** | Integration tests for wiring |

---

## Task 1: Critic Schemas

**Files:**
- Create: `nous/cognitive/critic_schemas.py`
- Test: `tests/test_critic.py`

- [ ] **Step 1: Write failing test for CriticResult model**

```python
# tests/test_critic.py
"""Tests for F024 Critic Agent Phase 0."""
import pytest
from nous.cognitive.critic_schemas import (
    CriticResult,
    DiagnosticResult,
    RoutingMode,
)


class TestCriticSchemas:
    def test_critic_result_defaults(self):
        result = CriticResult(
            routing=RoutingMode.SINGLE_ADVISED,
            recommended_frame="task",
            rationale="User wants to build something",
        )
        assert result.routing == RoutingMode.SINGLE_ADVISED
        assert result.recommended_frame == "task"
        assert result.skills == []
        assert result.complexity == "moderate"
        assert result.diagnostics == []

    def test_critic_result_passthrough(self):
        result = CriticResult(
            routing=RoutingMode.PASSTHROUGH,
            recommended_frame="conversation",
            rationale="Simple greeting",
            complexity="simple",
        )
        assert result.routing == RoutingMode.PASSTHROUGH

    def test_diagnostic_result(self):
        diag = DiagnosticResult(
            critic_name="repetition",
            intervention="You've searched for similar things multiple times.",
            fired=True,
        )
        assert diag.fired is True
        assert diag.critic_name == "repetition"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_critic.py::TestCriticSchemas -v`
Expected: FAIL with ModuleNotFoundError

- [ ] **Step 3: Implement critic_schemas.py**

```python
# nous/cognitive/critic_schemas.py
"""Pydantic models for F024 Critic Agent."""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class RoutingMode(str, Enum):
    """Critic routing decision for the current turn."""
    PASSTHROUGH = "passthrough"
    SINGLE_ADVISED = "single_advised"


class DiagnosticResult(BaseModel):
    """Result from a single diagnostic critic."""
    critic_name: str
    intervention: str
    fired: bool = False


class CriticResult(BaseModel):
    """Output from CriticAgent classification."""
    routing: RoutingMode
    recommended_frame: str
    rationale: str
    complexity: str = "moderate"
    skills: list[str] = Field(default_factory=list)
    diagnostics: list[DiagnosticResult] = Field(default_factory=list)
    heuristic_frame: str | None = None
    latency_ms: int = 0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_critic.py::TestCriticSchemas -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add nous/cognitive/critic_schemas.py tests/test_critic.py
git commit -m "feat(f024): add Critic Agent pydantic schemas"
```

---

## Task 2: Configuration Settings

**Files:**
- Modify: `nous/config.py` (append after F026 section, ~line 279)
- Test: `tests/test_critic.py`

- [ ] **Step 1: Write failing test for critic config**

```python
# tests/test_critic.py — append
from nous.config import Settings


class TestCriticConfig:
    def test_critic_defaults(self):
        s = Settings(
            anthropic_api_key="test-key",
            openai_api_key="test-key",
        )
        assert s.critic_enabled is True
        assert s.critic_mode == "shadow"
        assert s.critic_model == "claude-sonnet-4-6"
        assert s.critic_max_latency_ms == 2000
        assert s.critic_passthrough_max_words == 5

    def test_critic_disabled(self):
        s = Settings(
            anthropic_api_key="test-key",
            openai_api_key="test-key",
            critic_enabled=False,
        )
        assert s.critic_enabled is False

    def test_critic_mode_values(self):
        for mode in ("shadow", "advised", "parallel"):
            s = Settings(
                anthropic_api_key="test-key",
                openai_api_key="test-key",
                critic_mode=mode,
            )
            assert s.critic_mode == mode
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_critic.py::TestCriticConfig -v`
Expected: FAIL

- [ ] **Step 3: Add critic settings to config.py**

Add after line 279 (after `action_gating_external_only`), before the `@model_validator` decorators:

```python
    # F024: Critic Agent
    critic_enabled: bool = True
    critic_mode: Literal["shadow", "advised", "parallel"] = "shadow"
    critic_model: str = "claude-sonnet-4-6"
    critic_max_latency_ms: int = 2000
    critic_passthrough_max_words: int = 5
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_critic.py::TestCriticConfig -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add nous/config.py tests/test_critic.py
git commit -m "feat(f024): add Critic Agent configuration settings"
```

---

## Task 3: Complexity Gate (Heuristic Passthrough)

**Files:**
- Create: `nous/cognitive/critic.py`
- Test: `tests/test_critic.py`

- [ ] **Step 1: Write failing tests for complexity gate**

```python
# tests/test_critic.py — append
from nous.cognitive.critic import CriticAgent


class TestComplexityGate:
    def _make_agent(self):
        return CriticAgent(Settings(
            anthropic_api_key="test", openai_api_key="test",
        ))

    def test_short_greeting_skips_critic(self):
        assert self._make_agent()._needs_critic("hi", []) is False

    def test_short_non_question_skips(self):
        assert self._make_agent()._needs_critic("thanks", []) is False

    def test_empty_message_skips(self):
        assert self._make_agent()._needs_critic("", []) is False

    def test_short_question_invokes(self):
        assert self._make_agent()._needs_critic("what?", []) is True

    def test_multi_sentence_invokes(self):
        msg = "Research how other agents handle memory. Then build a comparison. Also analyze costs."
        assert self._make_agent()._needs_critic(msg, []) is True

    def test_multiple_action_verbs_invokes(self):
        msg = "research the topic and build a summary"
        assert self._make_agent()._needs_critic(msg, []) is True

    def test_repeated_tool_calls_invokes(self):
        history = [
            {"tool": "recall_deep", "args": "memory"},
            {"tool": "recall_deep", "args": "memory search"},
            {"tool": "recall_deep", "args": "memory retrieval"},
        ]
        assert self._make_agent()._needs_critic("find it", history) is True

    def test_default_invokes_critic(self):
        assert self._make_agent()._needs_critic(
            "tell me about the architecture", []
        ) is True

    def test_emoji_only_skips(self):
        assert self._make_agent()._needs_critic("👍", []) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_critic.py::TestComplexityGate -v`
Expected: FAIL — CriticAgent not found

- [ ] **Step 3: Implement CriticAgent with complexity gate**

```python
# nous/cognitive/critic.py
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

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._api: Any = None
        self._diagnostic_cooldowns: dict[str, int] = {}
        self._current_turn: int = 0

    def set_api_client(self, client: Any) -> None:
        """Set the shared AnthropicClient for LLM calls."""
        self._api = client

    def _needs_critic(
        self,
        message: str,
        tool_call_history: list[dict[str, Any]],
    ) -> bool:
        """Fast heuristic — no LLM call. Errs toward invoking Critic."""
        words = message.split()
        max_words = self._settings.critic_passthrough_max_words

        if len(words) <= max_words and not message.rstrip().endswith("?"):
            return False

        sentence_endings = message.count(".") + message.count("?") + message.count("!")
        if sentence_endings > 2:
            return True

        if len(tool_call_history) >= 3:
            recent_tools = [tc.get("tool", "") for tc in tool_call_history[-5:]]
            counts = Counter(recent_tools)
            if counts.most_common(1)[0][1] >= 3:
                return True

        lower_words = set(message.lower().split())
        if len(lower_words & _ACTION_SIGNALS) >= 2:
            return True

        return True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_critic.py::TestComplexityGate -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add nous/cognitive/critic.py tests/test_critic.py
git commit -m "feat(f024): add CriticAgent with complexity gate heuristic"
```

---

## Task 4: LLM Classification

**Files:**
- Modify: `nous/cognitive/critic.py`
- Test: `tests/test_critic.py`

- [ ] **Step 1: Write failing tests for classification**

```python
# tests/test_critic.py — append
import json
from unittest.mock import AsyncMock, MagicMock


class TestCriticClassification:
    @pytest.mark.asyncio
    async def test_classify_parses_json_response(self):
        agent = CriticAgent(Settings(
            anthropic_api_key="test", openai_api_key="test",
        ))
        mock_api = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = [
            {"type": "text", "text": json.dumps({
                "complexity": "moderate",
                "routing": "single",
                "frames": ["task"],
                "skills": [],
                "rationale": "User wants to build something",
                "per_frame_instructions": {},
            })}
        ]
        mock_api.call = AsyncMock(return_value=mock_response)
        agent.set_api_client(mock_api)

        heuristic = FrameSelection(
            frame_id="conversation", frame_name="Conversation",
            confidence=0.5, match_method="default",
        )
        result = await agent.classify(
            user_message="Build a REST API for user management",
            heuristic_frame=heuristic,
            available_frames=["task", "conversation", "question"],
        )
        assert result.routing == RoutingMode.SINGLE_ADVISED
        assert result.recommended_frame == "task"
        assert result.heuristic_frame == "conversation"
        assert result.latency_ms >= 0

    @pytest.mark.asyncio
    async def test_classify_handles_malformed_json(self):
        agent = CriticAgent(Settings(
            anthropic_api_key="test", openai_api_key="test",
        ))
        mock_api = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = [{"type": "text", "text": "not json at all"}]
        mock_api.call = AsyncMock(return_value=mock_response)
        agent.set_api_client(mock_api)

        heuristic = FrameSelection(
            frame_id="task", frame_name="Task",
            confidence=0.8, match_method="pattern",
        )
        result = await agent.classify(
            user_message="do something",
            heuristic_frame=heuristic,
            available_frames=["task"],
        )
        assert result.routing == RoutingMode.SINGLE_ADVISED
        assert result.recommended_frame == "task"

    @pytest.mark.asyncio
    async def test_classify_no_api_client_falls_back(self):
        agent = CriticAgent(Settings(
            anthropic_api_key="test", openai_api_key="test",
        ))
        heuristic = FrameSelection(
            frame_id="task", frame_name="Task",
            confidence=0.8, match_method="pattern",
        )
        result = await agent.classify(
            user_message="do something",
            heuristic_frame=heuristic,
            available_frames=["task"],
        )
        assert result.routing == RoutingMode.PASSTHROUGH
        assert result.recommended_frame == "task"

    @pytest.mark.asyncio
    async def test_passthrough_skips_llm_call(self):
        agent = CriticAgent(Settings(
            anthropic_api_key="test", openai_api_key="test",
        ))
        mock_api = AsyncMock()
        agent.set_api_client(mock_api)

        heuristic = FrameSelection(
            frame_id="conversation", frame_name="Conversation",
            confidence=0.5, match_method="default",
        )
        result = await agent.classify(
            user_message="hi",
            heuristic_frame=heuristic,
            available_frames=["conversation"],
        )
        assert result.routing == RoutingMode.PASSTHROUGH
        mock_api.call.assert_not_called()

    @pytest.mark.asyncio
    async def test_classify_handles_api_exception(self):
        agent = CriticAgent(Settings(
            anthropic_api_key="test", openai_api_key="test",
        ))
        mock_api = AsyncMock()
        mock_api.call = AsyncMock(side_effect=RuntimeError("rate limited"))
        agent.set_api_client(mock_api)

        heuristic = FrameSelection(
            frame_id="task", frame_name="Task",
            confidence=0.8, match_method="pattern",
        )
        result = await agent.classify(
            user_message="build something complex here please",
            heuristic_frame=heuristic,
            available_frames=["task"],
        )
        assert result.routing == RoutingMode.SINGLE_ADVISED
        assert result.recommended_frame == "task"

    @pytest.mark.asyncio
    async def test_json_wrapped_in_code_fence(self):
        agent = CriticAgent(Settings(
            anthropic_api_key="test", openai_api_key="test",
        ))
        mock_api = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = [
            {"type": "text", "text": '```json\n{"complexity":"simple","routing":"single","frames":["question"],"skills":[],"rationale":"Simple question","per_frame_instructions":{}}\n```'}
        ]
        mock_api.call = AsyncMock(return_value=mock_response)
        agent.set_api_client(mock_api)

        heuristic = FrameSelection(
            frame_id="conversation", frame_name="Conversation",
            confidence=0.5, match_method="default",
        )
        result = await agent.classify(
            user_message="what is the meaning of life and how does it relate to our architecture?",
            heuristic_frame=heuristic,
            available_frames=["conversation", "question"],
        )
        assert result.recommended_frame == "question"

    @pytest.mark.asyncio
    async def test_empty_frames_list_uses_heuristic(self):
        agent = CriticAgent(Settings(
            anthropic_api_key="test", openai_api_key="test",
        ))
        mock_api = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = [
            {"type": "text", "text": json.dumps({
                "complexity": "simple", "routing": "single",
                "frames": [], "skills": [], "rationale": "",
                "per_frame_instructions": {},
            })}
        ]
        mock_api.call = AsyncMock(return_value=mock_response)
        agent.set_api_client(mock_api)

        heuristic = FrameSelection(
            frame_id="task", frame_name="Task",
            confidence=0.8, match_method="pattern",
        )
        result = await agent.classify(
            user_message="do something complex here please",
            heuristic_frame=heuristic,
            available_frames=["task"],
        )
        assert result.recommended_frame == "task"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_critic.py::TestCriticClassification -v`
Expected: FAIL — no `classify` method

- [ ] **Step 3: Implement classify method**

Add to `nous/cognitive/critic.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_critic.py::TestCriticClassification -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add nous/cognitive/critic.py tests/test_critic.py
git commit -m "feat(f024): add CriticAgent LLM classification with timeout and fallback"
```

---

## Task 5: Diagnostic Critics (All 6 from Spec)

**Files:**
- Modify: `nous/cognitive/critic.py`
- Test: `tests/test_critic.py`

The spec (lines 485-496) defines 6 diagnostic patterns: repetition, frame mismatch, stuck loop, scope creep, confidence drift, user frustration. All 6 are implemented here.

- [ ] **Step 1: Write failing tests for all 6 diagnostic critics**

```python
# tests/test_critic.py — append
from nous.cognitive.schemas import ToolResult, TurnResult


class TestDiagnosticCritics:
    def test_repetition_detector(self):
        agent = CriticAgent(Settings(
            anthropic_api_key="test", openai_api_key="test",
        ))
        tool_history = [
            {"tool": "recall_deep", "query": "how does memory work"},
            {"tool": "recall_deep", "query": "how does memory function"},
            {"tool": "recall_deep", "query": "how does the memory system work"},
        ]
        results = agent.run_diagnostics(tool_history, turn_number=1)
        fired = [d for d in results if d.fired]
        assert any(d.critic_name == "repetition" for d in fired)

    def test_stuck_loop_detector(self):
        agent = CriticAgent(Settings(
            anthropic_api_key="test", openai_api_key="test",
        ))
        tool_history = [
            {"tool": "bash", "args": "ls /tmp"},
            {"tool": "bash", "args": "ls /tmp"},
            {"tool": "bash", "args": "ls /tmp"},
        ]
        results = agent.run_diagnostics(tool_history, turn_number=1)
        fired = [d for d in results if d.fired]
        assert any(d.critic_name == "stuck_loop" for d in fired)

    def test_confidence_drift_detector(self):
        agent = CriticAgent(Settings(
            anthropic_api_key="test", openai_api_key="test",
        ))
        tool_history = [
            {"tool": "record_decision", "confidence": 0.3},
            {"tool": "record_decision", "confidence": 0.25},
            {"tool": "record_decision", "confidence": 0.2},
        ]
        results = agent.run_diagnostics(tool_history, turn_number=1)
        fired = [d for d in results if d.fired]
        assert any(d.critic_name == "confidence_drift" for d in fired)

    def test_frame_mismatch_detector(self):
        agent = CriticAgent(Settings(
            anthropic_api_key="test", openai_api_key="test",
        ))
        # Task-frame tools in a conversation frame
        tool_history = [
            {"tool": "bash", "args": "npm install"},
            {"tool": "write_file", "args": "main.py"},
            {"tool": "bash", "args": "pytest"},
        ]
        results = agent.run_diagnostics(
            tool_history, turn_number=1, current_frame="conversation"
        )
        fired = [d for d in results if d.fired]
        assert any(d.critic_name == "frame_mismatch" for d in fired)

    def test_scope_creep_detector(self):
        agent = CriticAgent(Settings(
            anthropic_api_key="test", openai_api_key="test",
        ))
        # Response lengths growing each turn
        turn_lengths = [100, 200, 500, 1200, 2500]
        results = agent.run_diagnostics(
            [], turn_number=1, response_lengths=turn_lengths,
        )
        fired = [d for d in results if d.fired]
        assert any(d.critic_name == "scope_creep" for d in fired)

    def test_user_frustration_detector(self):
        agent = CriticAgent(Settings(
            anthropic_api_key="test", openai_api_key="test",
        ))
        # Short user messages after long agent responses
        results = agent.run_diagnostics(
            [], turn_number=1,
            recent_user_messages=["no", "I already said that", "no I meant the other one"],
        )
        fired = [d for d in results if d.fired]
        assert any(d.critic_name == "user_frustration" for d in fired)

    def test_no_false_positives_on_clean_history(self):
        agent = CriticAgent(Settings(
            anthropic_api_key="test", openai_api_key="test",
        ))
        tool_history = [
            {"tool": "recall_deep", "query": "python async"},
            {"tool": "bash", "args": "pytest"},
            {"tool": "write_file", "args": "main.py"},
        ]
        results = agent.run_diagnostics(tool_history, turn_number=1)
        fired = [d for d in results if d.fired]
        assert len(fired) == 0

    def test_cooldown_prevents_refiring(self):
        agent = CriticAgent(Settings(
            anthropic_api_key="test", openai_api_key="test",
        ))
        tool_history = [
            {"tool": "recall_deep", "query": "memory"},
            {"tool": "recall_deep", "query": "memory"},
            {"tool": "recall_deep", "query": "memory"},
        ]
        results1 = agent.run_diagnostics(tool_history, turn_number=1)
        assert any(d.fired for d in results1)

        results2 = agent.run_diagnostics(tool_history, turn_number=2)
        assert not any(d.fired for d in results2)

        results3 = agent.run_diagnostics(tool_history, turn_number=5)
        assert any(d.fired for d in results3)

    def test_empty_tool_history(self):
        agent = CriticAgent(Settings(
            anthropic_api_key="test", openai_api_key="test",
        ))
        results = agent.run_diagnostics([], turn_number=1)
        assert not any(d.fired for d in results)

    def test_format_nudges_empty_when_none_fired(self):
        agent = CriticAgent(Settings(
            anthropic_api_key="test", openai_api_key="test",
        ))
        results = agent.run_diagnostics([], turn_number=1)
        assert agent.format_nudges(results) == ""

    def test_format_nudges_with_fired(self):
        agent = CriticAgent(Settings(
            anthropic_api_key="test", openai_api_key="test",
        ))
        tool_history = [
            {"tool": "recall_deep", "query": "memory"},
            {"tool": "recall_deep", "query": "memory"},
            {"tool": "recall_deep", "query": "memory"},
        ]
        results = agent.run_diagnostics(tool_history, turn_number=1)
        nudges = agent.format_nudges(results)
        assert "[Critic/repetition]" in nudges
        assert "[DIAGNOSTIC OBSERVATIONS]" in nudges
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_critic.py::TestDiagnosticCritics -v`
Expected: FAIL

- [ ] **Step 3: Implement all 6 diagnostic critics**

Add to `nous/cognitive/critic.py`:

```python
    # Tool sets for frame mismatch detection
    _TASK_TOOLS = frozenset({"bash", "write_file"})
    _CONVERSATION_TOOLS = frozenset({"recall_deep", "learn_fact"})
    _FRUSTRATION_SIGNALS = frozenset({
        "no", "nope", "wrong", "not what i", "i already", "i said",
        "no i meant", "that's not", "try again",
    })

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
                return self._fire(name, "You've searched for similar things multiple times. Reformulate the problem or try a different approach.")
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
            return self._fire(name, "Consider a completely different strategy. The same tool with similar arguments has been called multiple times.")
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
                return self._fire(name, "Pause. Multiple recent decisions have low confidence. What are you uncertain about? Ask the user.")
        return self._not_fired(name)

    def _check_frame_mismatch(self, tool_history: list[dict[str, Any]], current_frame: str) -> DiagnosticResult:
        """Task-frame tools in conversation context or vice versa."""
        name = "frame_mismatch"
        if self._on_cooldown(name) or not current_frame or len(tool_history) < 2:
            return self._not_fired(name)
        recent_tools = {tc.get("tool", "") for tc in tool_history[-5:]}
        if current_frame == "conversation" and len(recent_tools & self._TASK_TOOLS) >= 2:
            return self._fire(name, "You're using task-oriented tools in a conversation frame. Consider switching to task or debug frame.")
        if current_frame in ("task", "debug") and recent_tools <= self._CONVERSATION_TOOLS and len(recent_tools) >= 2:
            return self._fire(name, "You're only using conversation tools in a task frame. The frame may not match the actual work.")
        return self._not_fired(name)

    def _check_scope_creep(self, response_lengths: list[int]) -> DiagnosticResult:
        """Response lengths growing consistently."""
        name = "scope_creep"
        if self._on_cooldown(name) or len(response_lengths) < 3:
            return self._not_fired(name)
        recent = response_lengths[-4:]
        if len(recent) >= 3 and all(recent[i] > recent[i-1] * 1.5 for i in range(1, len(recent))):
            return self._fire(name, "Focus. Response length is growing rapidly. What was the user's core ask?")
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
            return self._fire(name, "The user may be frustrated. Acknowledge, clarify, and re-align with their actual request.")
        return self._not_fired(name)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_critic.py::TestDiagnosticCritics -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add nous/cognitive/critic.py tests/test_critic.py
git commit -m "feat(f024): add all 6 diagnostic critics from spec"
```

---

## Task 6: Wire into CognitiveLayer and Runner

**Files:**
- Modify: `nous/cognitive/schemas.py` (add diagnostic_nudges to TurnContext)
- Modify: `nous/cognitive/layer.py` (critic hook in pre_turn, diagnostics in post_turn, cleanup in end_session)
- Modify: `nous/api/runner.py` (inject diagnostic nudges in system prompt)
- Modify: `nous/main.py` (create CriticAgent with shared api_client)
- Test: `tests/test_critic_integration.py`

- [ ] **Step 1: Write failing integration tests**

```python
# tests/test_critic_integration.py
"""Integration tests for F024 Critic Agent wiring."""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from nous.cognitive.critic import CriticAgent
from nous.cognitive.critic_schemas import CriticResult, RoutingMode
from nous.cognitive.schemas import FrameSelection, TurnContext
from nous.config import Settings


class TestCriticLayerIntegration:
    @pytest.mark.asyncio
    async def test_shadow_mode_logs_but_keeps_heuristic(self):
        settings = Settings(
            anthropic_api_key="test", openai_api_key="test",
            critic_enabled=True, critic_mode="shadow",
        )
        critic = CriticAgent(settings)
        mock_api = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = [
            {"type": "text", "text": json.dumps({
                "complexity": "moderate", "routing": "single",
                "frames": ["decision"], "skills": [],
                "rationale": "Decision task", "per_frame_instructions": {},
            })}
        ]
        mock_api.call = AsyncMock(return_value=mock_response)
        critic.set_api_client(mock_api)

        heuristic = FrameSelection(
            frame_id="task", frame_name="Task",
            confidence=0.7, match_method="pattern",
        )
        result = await critic.classify(
            user_message="Should we use Redis or Memcached for our cache layer?",
            heuristic_frame=heuristic,
            available_frames=["task", "decision", "conversation"],
        )
        assert result.recommended_frame == "decision"
        assert result.heuristic_frame == "task"

    @pytest.mark.asyncio
    async def test_advised_mode_overrides_frame(self):
        settings = Settings(
            anthropic_api_key="test", openai_api_key="test",
            critic_enabled=True, critic_mode="advised",
        )
        critic = CriticAgent(settings)
        mock_api = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = [
            {"type": "text", "text": json.dumps({
                "complexity": "moderate", "routing": "single",
                "frames": ["debug"], "skills": [],
                "rationale": "Troubleshooting", "per_frame_instructions": {},
            })}
        ]
        mock_api.call = AsyncMock(return_value=mock_response)
        critic.set_api_client(mock_api)

        heuristic = FrameSelection(
            frame_id="conversation", frame_name="Conversation",
            confidence=0.3, match_method="default",
        )
        result = await critic.classify(
            user_message="The web search keeps timing out and I can't figure out why",
            heuristic_frame=heuristic,
            available_frames=["conversation", "debug", "task"],
        )
        assert result.recommended_frame == "debug"

    def test_diagnostic_nudges_in_turn_context(self):
        ctx = TurnContext(
            system_prompt="test",
            frame=FrameSelection(
                frame_id="task", frame_name="Task",
                confidence=0.5, match_method="pattern",
            ),
        )
        ctx.diagnostic_nudges = "[DIAGNOSTIC OBSERVATIONS]\n[Critic/repetition]: Stop repeating."
        assert "[DIAGNOSTIC OBSERVATIONS]" in ctx.diagnostic_nudges


class TestRunnerCriticWiring:
    def test_diagnostic_nudges_injected_in_system_prompt(self):
        from nous.api.runner import AgentRunner

        settings = Settings(
            anthropic_api_key="test", openai_api_key="test",
            critic_enabled=True,
        )
        runner = AgentRunner(
            cognitive=MagicMock(),
            brain=MagicMock(),
            heart=MagicMock(),
            settings=settings,
        )

        turn_context = MagicMock()
        turn_context.system_prompt = "Base system prompt"
        turn_context.frame = MagicMock()
        turn_context.frame.frame_id = "task"
        turn_context.diagnostic_nudges = (
            "\n\n[DIAGNOSTIC OBSERVATIONS]\n"
            "[Critic/repetition]: You've searched for similar things."
        )

        prompt = runner._build_system_prompt(turn_context)
        assert "[DIAGNOSTIC OBSERVATIONS]" in prompt
        assert "[Critic/repetition]" in prompt

    def test_no_nudges_when_empty(self):
        from nous.api.runner import AgentRunner

        settings = Settings(
            anthropic_api_key="test", openai_api_key="test",
            critic_enabled=True,
        )
        runner = AgentRunner(
            cognitive=MagicMock(),
            brain=MagicMock(),
            heart=MagicMock(),
            settings=settings,
        )

        turn_context = MagicMock()
        turn_context.system_prompt = "Base system prompt"
        turn_context.frame = MagicMock()
        turn_context.frame.frame_id = "task"
        turn_context.diagnostic_nudges = ""

        prompt = runner._build_system_prompt(turn_context)
        assert "[DIAGNOSTIC OBSERVATIONS]" not in prompt
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_critic_integration.py -v`
Expected: FAIL — TurnContext has no diagnostic_nudges

- [ ] **Step 3: Implement wiring changes**

**3a. Add `diagnostic_nudges` to TurnContext** (`nous/cognitive/schemas.py`):

Find the `TurnContext` dataclass and add at the end:

```python
    diagnostic_nudges: str = ""  # F024: Critic diagnostic observations
```

**3b. Modify `layer.py` __init__** — add critic param and session tracking dicts:

```python
    def __init__(
        self,
        brain: Brain,
        heart: Heart,
        settings: Settings,
        identity_prompt: str = "",
        *,
        bus: EventBus | None = None,
        identity_manager: "IdentityManager | None" = None,
        critic: "CriticAgent | None" = None,
    ) -> None:
        # ... existing code ...
        self._critic = critic
        self._session_tool_history: dict[str, list[dict]] = {}
        self._pending_nudges: dict[str, str] = {}
        self._session_response_lengths: dict[str, list[int]] = {}
        self._session_user_messages: dict[str, list[str]] = {}
```

**3c. Modify `pre_turn()`** — insert critic hook after frame selection (after line 265, inside the `else` block), before intent classification (line 267):

```python
            # F024: Critic Agent pre-turn classification
            if self._critic and self._settings.critic_enabled:
                heuristic_frame = frame  # preserve for shadow logging
                try:
                    all_frames = await self._frames.list_frames(agent_id, session=session)
                    available_frame_ids = [f.frame_id for f in all_frames
                                           if f.frame_id != "initiation"]
                except Exception:
                    available_frame_ids = ["conversation", "task", "question",
                                           "decision", "debug", "creative"]

                tool_history = self._session_tool_history.get(session_id, [])
                critic_result = await self._critic.classify(
                    user_message=user_input,
                    heuristic_frame=frame,
                    available_frames=available_frame_ids,
                    tool_call_history=tool_history,
                )

                # In advised mode, override frame selection
                if (self._settings.critic_mode == "advised"
                        and critic_result.routing == RoutingMode.SINGLE_ADVISED
                        and critic_result.recommended_frame != frame.frame_id):
                    try:
                        override = await self._frames.get(
                            critic_result.recommended_frame, agent_id, session=session,
                        )
                        logger.info(
                            "F024 Critic overriding frame: %s -> %s (reason: %s)",
                            frame.frame_id, critic_result.recommended_frame,
                            critic_result.rationale,
                        )
                        frame = override
                    except ValueError:
                        logger.warning("F024 Critic recommended unknown frame: %s",
                                       critic_result.recommended_frame)
                elif self._settings.critic_mode == "shadow":
                    if critic_result.recommended_frame != frame.frame_id:
                        logger.info(
                            "F024 Critic shadow: heuristic=%s, critic=%s, reason=%s",
                            frame.frame_id, critic_result.recommended_frame,
                            critic_result.rationale,
                        )

                # Emit critic_classified event
                if self._bus:
                    try:
                        await self._bus.emit(Event(
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
                            },
                        ))
                    except Exception:
                        pass  # non-critical
```

Also, attach pending diagnostic nudges to TurnContext (at the end of pre_turn, before return):

```python
        # F024: Attach pending diagnostic nudges from previous turn
        if session_id in self._pending_nudges:
            turn_context.diagnostic_nudges = self._pending_nudges.pop(session_id)
```

**Need to add import at top of layer.py:**
```python
from nous.cognitive.critic_schemas import RoutingMode
```

**3d. Modify `post_turn()`** — add diagnostic tracking and tool history:

```python
        # F024: Track tool calls and run diagnostics
        if self._critic and self._settings.critic_enabled:
            # Track tool call history
            if turn_result.tool_results:
                history = self._session_tool_history.setdefault(session_id, [])
                for tr in turn_result.tool_results:
                    entry: dict[str, Any] = {"tool": tr.tool_name, "args": str(tr.arguments)[:200]}
                    if isinstance(tr.arguments, dict):
                        entry["query"] = tr.arguments.get("query", "")
                        entry["confidence"] = tr.arguments.get("confidence")
                    history.append(entry)
                self._session_tool_history[session_id] = history[-20:]

            # Track response lengths for scope creep detection
            resp_lengths = self._session_response_lengths.setdefault(session_id, [])
            resp_lengths.append(len(turn_result.response_text))

            # Track recent user messages for frustration detection
            # (user_input available from turn_context or stored separately)

            # Run diagnostics
            meta = self._session_metadata.get(session_id)
            turn_num = meta.turn_count if meta else 1
            diagnostics = self._critic.run_diagnostics(
                self._session_tool_history.get(session_id, []),
                turn_number=turn_num,
                current_frame=turn_context.frame.frame_id if turn_context else "",
                response_lengths=resp_lengths,
            )
            nudges = self._critic.format_nudges(diagnostics)
            if nudges:
                self._pending_nudges[session_id] = nudges
                logger.info("F024 diagnostic nudge queued for session %s", session_id)
```

**3e. Modify `end_session()`** — add cleanup (after line 1105):

```python
        # F024: Clean up critic session state
        self._session_tool_history.pop(session_id, None)
        self._pending_nudges.pop(session_id, None)
        self._session_response_lengths.pop(session_id, None)
        self._session_user_messages.pop(session_id, None)
```

**3f. Modify `runner.py` `_build_system_prompt()`** — inject nudges after frame instructions, before ledger:

```python
        # F024: Diagnostic nudges from Critic
        if turn_context.diagnostic_nudges:
            parts.append(turn_context.diagnostic_nudges)
```

Insert between the frame instructions block and the F026 ledger block.

**3g. Modify `main.py`** — wire CriticAgent with shared api_client:

After the `api_client` creation (around line 110), before CognitiveLayer:

```python
    # F024: Critic Agent (uses shared api_client)
    critic = None
    if settings.critic_enabled:
        from nous.cognitive.critic import CriticAgent
        critic = CriticAgent(settings)
        critic.set_api_client(api_client)
        logger.info("F024: CriticAgent wired (mode=%s, model=%s)",
                     settings.critic_mode, settings.critic_model)
```

Update CognitiveLayer construction:

```python
    cognitive = CognitiveLayer(
        brain, heart, settings, settings.identity_prompt,
        bus=bus, identity_manager=identity_manager,
        critic=critic,
    )
```

- [ ] **Step 4: Run all tests**

Run: `uv run pytest tests/test_critic.py tests/test_critic_integration.py -v`
Expected: PASS

- [ ] **Step 5: Run existing test suite for regressions**

Run: `uv run pytest tests/ -x -q --timeout=30`
Expected: No new failures

- [ ] **Step 6: Commit**

```bash
git add nous/cognitive/schemas.py nous/cognitive/layer.py nous/api/runner.py nous/main.py tests/test_critic_integration.py
git commit -m "feat(f024): wire CriticAgent into layer, runner, and main"
```

---

## Task 7: Documentation Update

**Files:**
- Modify: `docs/features/INDEX.md`

- [ ] **Step 1: Update feature index**

Add F024 row to the appropriate section with status "Phase 0 Shipped".

- [ ] **Step 2: Commit**

```bash
git add docs/features/INDEX.md
git commit -m "docs(f024): update feature index — Phase 0 shipped"
```

---

## Summary

| Task | Description | Files Changed |
|------|-------------|---------------|
| 1 | Critic schemas | `critic_schemas.py`, `test_critic.py` |
| 2 | Configuration settings | `config.py`, `test_critic.py` |
| 3 | Complexity gate | `critic.py`, `test_critic.py` |
| 4 | LLM classification with timeout | `critic.py`, `test_critic.py` |
| 5 | All 6 diagnostic critics | `critic.py`, `test_critic.py` |
| 6 | Wire into layer, runner, main | `schemas.py`, `layer.py`, `runner.py`, `main.py`, `test_critic_integration.py` |
| 7 | Documentation update | `INDEX.md` |

**New files:** 4 (`critic.py`, `critic_schemas.py`, `test_critic.py`, `test_critic_integration.py`)
**Modified files:** 5 (`config.py`, `schemas.py`, `layer.py`, `runner.py`, `main.py`)
