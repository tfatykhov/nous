"""Conversation compaction — tool result pruning and history management.

Spec 008.1: Two-layer approach:
  Layer 1: Tool output pruning (per-request, no LLM)
  Layer 2: History compaction (rare, LLM-powered)

This module is independent of AgentRunner to avoid circular imports
and keep runner.py focused on orchestration.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Protocol

from nous.api.models import ApiResponse, Conversation, Message
from nous.cognitive.schemas import DECAY_PROFILE_AGES, TOOL_DECAY_PROFILES
from nous.config import Settings

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Summarization Prompts (co-located with compaction logic)
# ------------------------------------------------------------------

CHECKPOINT_SYSTEM_PROMPT = """\
You are a conversation summarizer. Output ONLY a structured summary.
TARGET LENGTH: 800-1200 words. Prioritize precision over completeness.

## Format

## Goal
[1-2 sentences]

## Constraints & Preferences
- [Requirements, technical constraints]

## Progress
### Done
- [x] [Completed items]
### In Progress
- [ ] [Current work]

## Key Decisions
- **[Decision]**: [Rationale]

## Conversation Dynamics
- [User tone, frustration, preferences expressed]
- [Communication style, behavioral instructions given]
- [Unresolved questions]

## Next Steps
1. [Ordered list]

## Critical Context
- [File paths, error messages, API endpoints, variable names]
"""

UPDATE_SYSTEM_PROMPT = """\
You are updating a conversation summary with new messages.
TARGET LENGTH: 800-1200 words. If exceeding, prioritize:
1. Recent progress and decisions
2. Critical context (paths, errors, APIs)
3. Active constraints
4. Conversation dynamics
Drop older completed "Done" items if needed.

RULES:
1. PRESERVE existing info unless explicitly superseded
2. ADD new progress, decisions, context
3. MOVE In Progress -> Done when completed
4. UPDATE Conversation Dynamics with new signals
5. PRESERVE exact file paths, function names, error messages
6. Use SAME format as existing summary

Output ONLY the updated summary."""

# Section patterns for validation (case-insensitive, flexible)
_SECTION_PATTERNS = [
    re.compile(r"##\s*goals?\b", re.IGNORECASE),
    re.compile(r"##\s*progress\b", re.IGNORECASE),
    re.compile(r"##\s*critical\s*context\b", re.IGNORECASE),
]


# ------------------------------------------------------------------
# Protocol for API caller injection
# ------------------------------------------------------------------


class ApiCaller(Protocol):
    """Type-safe callable for AgentRunner._call_api injection."""

    async def __call__(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        skip_thinking: bool = False,
        model_override: str | None = None,
    ) -> ApiResponse: ...


# ------------------------------------------------------------------
# Token Estimator
# ------------------------------------------------------------------


class TokenEstimator:
    """Estimates token counts with optional calibration from API usage.

    Starts with chars/4 heuristic. Improves via calibrate() after each
    API response using actual input_tokens from usage data.

    Limitations (acknowledged):
    - Resets on container restart (ephemeral)
    - actual_tokens from API includes system prompt overhead
    - Alpha=0.1 means ~10 samples to ~65% convergence
    - 20K safety margin absorbs estimation error
    """

    def __init__(self) -> None:
        self._ratio: float = 0.25  # tokens per char (chars/4 default)
        self._samples: int = 0

    @property
    def samples(self) -> int:
        """Number of calibration samples received."""
        return self._samples

    @property
    def ratio(self) -> float:
        """Current tokens-per-char ratio."""
        return self._ratio

    def estimate(self, text: str | Any) -> int:
        """Estimate token count for text content."""
        if isinstance(text, str):
            return max(1, int(len(text) * self._ratio))
        return max(1, int(len(str(text)) * self._ratio))

    def estimate_messages(self, messages: list[dict[str, Any]]) -> int:
        """Estimate total tokens for a message list."""
        return sum(self.estimate(m.get("content", "")) + 4 for m in messages)

    def calibrate(self, input_chars: int, actual_tokens: int) -> None:
        """Update ratio from actual API input_tokens. EMA with alpha=0.1."""
        if input_chars <= 0 or actual_tokens <= 0:
            return
        observed = actual_tokens / input_chars
        self._ratio = 0.1 * observed + 0.9 * self._ratio
        self._samples += 1


# ------------------------------------------------------------------
# Conversation Compactor
# ------------------------------------------------------------------


class ConversationCompactor:
    """Manages tool result pruning (Layer 1) and history compaction (Layer 2).

    Layer 1: Prunes tool results during tool loops to prevent
    in-turn context window overflow. No LLM calls.

    Layer 2: Compacts conversation history via structured
    summarization when token budget is exceeded.

    Owns a TokenEstimator instance. AgentRunner accesses it via
    compactor.estimator for calibration after API responses.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.estimator = TokenEstimator()

    # ------------------------------------------------------------------
    # Layer 1: Tool Output Pruning
    # ------------------------------------------------------------------

    @staticmethod
    def is_tool_result_message(msg: dict[str, Any]) -> bool:
        """Check if a message contains tool results (not regular user text).

        Tool result messages have role="user" with content as a list of
        dicts containing type="tool_result". Regular user messages have
        string content.
        """
        content = msg.get("content")
        return (
            msg.get("role") == "user"
            and isinstance(content, list)
            and len(content) > 0
            and isinstance(content[0], dict)
            and content[0].get("type") == "tool_result"
        )

    def _build_tool_use_index(
        self, messages: list[dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        """Build tool_use_id -> tool_use block index. O(N) once."""
        index: dict[str, dict[str, Any]] = {}
        for msg in messages:
            if msg.get("role") != "assistant":
                continue
            content = msg.get("content", [])
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_id = block.get("id")
                    if tool_id:
                        index[tool_id] = block
        return index

    def _metadata_degrade(
        self, item: dict[str, Any], tool_use_block: dict[str, Any] | None
    ) -> None:
        """Replace tool result with descriptive metadata trace."""
        text = item.get("content", "")
        if not isinstance(text, str):
            return
        if len(text) < 200:
            return  # Small results: keep as-is

        tool_name = tool_use_block.get("name", "tool") if tool_use_block else "tool"
        tool_input = tool_use_block.get("input", {}) if tool_use_block else {}

        args_parts = []
        for k, v in (tool_input.items() if isinstance(tool_input, dict) else []):
            v_str = str(v)[:80]
            args_parts.append(f"{k}={v_str}")
        args_summary = ", ".join(args_parts[:3])

        first_line = ""
        for line in text.split("\n"):
            stripped = line.strip()
            if stripped and len(stripped) > 5:
                first_line = stripped[:120]
                break

        line_count = text.count("\n") + 1
        refetchable = tool_name in {"read_file", "list_files", "bash", "run_python"}
        refetch_hint = " | re-fetchable" if refetchable else ""

        item["content"] = (
            f"[{tool_name}({args_summary}): {line_count} lines, "
            f"{len(text)} chars | first: {first_line}{refetch_hint}]"
        )

    def _extract_facts_before_clear(self, tool_name: str, content: str) -> list[str]:
        """Extract URLs, paths, key-values before hard-clearing."""
        facts: list[str] = []
        # URLs
        facts.extend(re.findall(r'https?://[^\s\'"<>]+', content))
        # File paths
        facts.extend(re.findall(r'(?:/[\w.-]+){2,}', content))
        # Key-value patterns
        facts.extend(re.findall(r'\b\w+:\s+[\w.-]+', content)[:5])
        return facts[:10]

    def prune_tool_results(self, messages: list[dict[str, Any]]) -> list[str]:
        """Prune old tool results from in-turn message accumulation.

        Mutates messages in place. Four-tier approach:
        1. Full: Recent results kept as-is (protected zone)
        2. Soft-trim: Keep head + tail of oversized results
        3. Metadata-degrade: Replace with tool name, args, first line
        4. Hard-clear: Replace very old results with placeholder

        Never modifies user text messages or assistant content blocks.
        Protects the last keep_last_tool_results tool-result messages.

        Returns list of facts extracted from hard-cleared content.
        """
        if not self._settings.tool_pruning_enabled:
            return []

        tool_indices = [
            i for i, msg in enumerate(messages)
            if self.is_tool_result_message(msg)
        ]
        if not tool_indices:
            return []

        protected = set(tool_indices[-self._settings.keep_last_tool_results:])
        tool_use_index = self._build_tool_use_index(messages)

        extracted: list[str] = []
        soft_trimmed = 0
        metadata_degraded = 0
        hard_cleared = 0

        for pos, idx in enumerate(tool_indices):
            if idx in protected:
                continue

            msg = messages[idx]
            content = msg["content"]
            age = len(tool_indices) - pos

            # Get per-tool decay profile
            tool_name = None
            for item in content:
                tool_use_id = item.get("tool_use_id")
                if tool_use_id:
                    block = tool_use_index.get(tool_use_id)
                    if block:
                        tool_name = block.get("name")
                        break

            profile_name = TOOL_DECAY_PROFILES.get(tool_name or "", "standard")
            _soft_age, degrade_age, clear_age = DECAY_PROFILE_AGES.get(
                profile_name, (3, 8, 12)
            )

            # Tier 4: Hard-clear (age >= clear_age)
            if age >= clear_age:
                for item in content:
                    if self._has_image_content(item):
                        continue
                    text = item.get("content", "")
                    # Extract facts from conservative tools before clearing
                    if isinstance(text, str) and text and profile_name == "conservative":
                        extracted.extend(self._extract_facts_before_clear(tool_name or "unknown", text))
                    item["content"] = (
                        "[Tool output cleared - content was processed in earlier turns]"
                    )
                hard_cleared += 1
                continue

            # Tier 3: Metadata degrade (age >= degrade_age)
            if age >= degrade_age:
                for item in content:
                    if self._has_image_content(item):
                        continue
                    tool_use_id = item.get("tool_use_id")
                    tool_use_block = tool_use_index.get(tool_use_id) if tool_use_id else None
                    self._metadata_degrade(item, tool_use_block)
                metadata_degraded += 1
                continue

            # Tier 2: Soft-trim (oversized results)
            for item in content:
                if self._has_image_content(item):
                    continue
                text = item.get("content", "")
                if not isinstance(text, str):
                    continue
                if len(text) > self._settings.tool_soft_trim_chars:
                    head = self._settings.tool_soft_trim_head
                    tail = self._settings.tool_soft_trim_tail
                    original_len = len(text)
                    item["content"] = (
                        f"{text[:head]}\n\n"
                        f"--- trimmed (kept {head} head + {tail} tail "
                        f"of {original_len} chars) ---\n\n"
                        f"{text[-tail:]}"
                    )
                    soft_trimmed += 1

        if soft_trimmed or hard_cleared or metadata_degraded:
            logger.info(
                "Pruned tool results: soft_trimmed=%d, metadata_degraded=%d, "
                "hard_cleared=%d (total=%d, protected=%d)",
                soft_trimmed, metadata_degraded, hard_cleared,
                len(tool_indices), len(protected),
            )

        return extracted

    @staticmethod
    def _has_image_content(item: dict[str, Any]) -> bool:
        """Check if a tool result item contains image content."""
        content = item.get("content")
        if isinstance(content, list):
            return any(
                isinstance(block, dict) and block.get("type") == "image"
                for block in content
            )
        return False

    # ------------------------------------------------------------------
    # Layer 2: History Compaction
    # ------------------------------------------------------------------

    def should_compact(self, system_tokens: int, history_tokens: int) -> bool:
        """Check if compaction is needed before a turn."""
        if not self._settings.compaction_enabled:
            return False
        total = system_tokens + history_tokens
        return total > self._settings.effective_compaction_threshold

    def find_cut_point(
        self, messages: list[dict[str, Any]], keep_recent_tokens: int
    ) -> int:
        """Walk backwards accumulating tokens. Returns index of old/recent split.

        Returns 0 if all messages fit (no compaction needed).
        Always snaps to user message boundary.
        """
        accumulated = 0
        for i in range(len(messages) - 1, -1, -1):
            accumulated += self.estimator.estimate(messages[i].get("content", ""))
            if accumulated >= keep_recent_tokens:
                for j in range(i, len(messages)):
                    if messages[j].get("role") == "user":
                        return j
                return 0  # No user message found - keep everything
        return 0

    async def compact(
        self,
        conversation: Conversation,
        messages: list[dict[str, Any]],
        call_api: ApiCaller,
        cut_point: int,
    ) -> None:
        """Compact conversation history via structured summarization.

        Caller must verify cut_point > 0 before calling.
        Messages list must be 1:1 aligned with conversation.messages.
        """
        if cut_point <= 0:
            raise ValueError("cut_point must be > 0; caller should guard")
        if len(messages) != len(conversation.messages):
            raise ValueError(
                f"Index alignment required: {len(messages)} != "
                f"{len(conversation.messages)}"
            )

        old_messages = messages[:cut_point]
        start_time = time.monotonic()

        # Skip synthetic summary prefix from previous compaction
        serialize_start = 0
        if conversation.summary and len(old_messages) > 2:
            serialize_start = 2

        try:
            checkpoint_text = await self._summarize(
                old_messages[serialize_start:], conversation.summary, call_api
            )
            if not self._validate_summary(checkpoint_text):
                raise ValueError("Summary failed validation")
        except Exception as e:
            logger.error(
                "Compaction failed: %s - falling back to truncation", e
            )
            conversation.messages = conversation.messages[cut_point:]
            conversation.summary = None
            conversation.compaction_count = max(
                0, conversation.compaction_count - 1
            )
            return

        # Rebuild messages with summary prefix
        compacted_prefix = [
            Message(
                role="user",
                content=f"[Previous conversation summary]\n\n{checkpoint_text}",
            ),
            Message(
                role="assistant",
                content="I have the context. Let's continue.",
            ),
        ]

        recent_msgs = conversation.messages[cut_point:]
        if recent_msgs and recent_msgs[0].role != "user":
            found = False
            for i, msg in enumerate(recent_msgs):
                if msg.role == "user":
                    recent_msgs = recent_msgs[i:]
                    found = True
                    break
            if not found:
                recent_msgs = []

        conversation.summary = checkpoint_text
        conversation.messages = compacted_prefix + recent_msgs
        conversation.compaction_count += 1

        # Clean up turn_contexts
        keep_contexts = max(1, len(recent_msgs) // 2)
        if len(conversation.turn_contexts) > keep_contexts:
            conversation.turn_contexts = conversation.turn_contexts[-keep_contexts:]

        duration_ms = int((time.monotonic() - start_time) * 1000)
        logger.info(
            "Compacted conversation %s: %d messages -> %d + summary "
            "(%d chars, %d ms, compaction #%d)",
            conversation.session_id,
            len(messages),
            len(conversation.messages),
            len(checkpoint_text),
            duration_ms,
            conversation.compaction_count,
        )

    async def _summarize(
        self,
        old_messages: list[dict[str, Any]],
        existing_summary: str | None,
        call_api: ApiCaller,
    ) -> str:
        """Generate structured checkpoint summary via LLM."""
        if existing_summary:
            user_content = (
                f"## Existing Summary\n\n{existing_summary}\n\n"
                f"## New Conversation\n\n"
                f"{self._serialize_for_summary(old_messages)}"
            )
            system = UPDATE_SYSTEM_PROMPT
        else:
            user_content = self._serialize_for_summary(old_messages)
            system = CHECKPOINT_SYSTEM_PROMPT

        response = await call_api(
            system_prompt=system,
            messages=[{"role": "user", "content": user_content}],
            tools=None,
            skip_thinking=True,
            model_override=self._settings.background_model,
        )
        return self.extract_text(response.content)

    def _validate_summary(self, summary: str) -> bool:
        """Basic format + length check.

        Not content validation (acknowledged limitation). The real safety
        net is fallback to truncation - wrong summary is discarded.
        """
        if len(summary) < 200:
            logger.warning("Summary too short (%d chars)", len(summary))
            return False
        if len(summary) > 8000:
            logger.warning(
                "Summary exceeds 8000 chars (%d) - accepting with warning",
                len(summary),
            )
        found = sum(1 for pat in _SECTION_PATTERNS if pat.search(summary))
        if found < 2:
            logger.warning("Summary missing sections (%d/3)", found)
            return False
        return True

    @staticmethod
    def _serialize_for_summary(messages: list[dict[str, Any]]) -> str:
        """Serialize messages as readable text for summarization.

        conversation.messages stores plain text only (tool results are
        in-turn locals, never persisted). Content is always str here.
        """
        lines = []
        for msg in messages:
            role = "User" if msg.get("role") == "user" else "Assistant"
            content = msg.get("content", "")
            if isinstance(content, str):
                lines.append(f"**{role}:** {content}")
            elif isinstance(content, list):
                # Defensive: handle list content if it ever appears
                parts = [
                    (
                        item.get("content")
                        or item.get("text")
                        or str(item)
                    )
                    if isinstance(item, dict)
                    else str(item)
                    for item in content
                ]
                lines.append(f"**{role}:** {chr(10).join(parts)}")
        return "\n\n".join(lines)

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    @staticmethod
    def extract_text(content: list[dict[str, Any]]) -> str:
        """Extract text from API response content blocks."""
        return "".join(
            block.get("text", "")
            for block in content
            if block.get("type") == "text"
        )
