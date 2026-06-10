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

CRITICAL FAITHFULNESS RULE — silent fact loss is the highest-cost failure
mode of compaction. Before writing the summary, scan the conversation
for SPECIFIC VALUES the user or assistant stated. Every one of them
MUST appear verbatim in your summary (typically under "## Critical
Context"). NEVER paraphrase a specific value into a generic phrase.

Examples of values that MUST be preserved verbatim:
- Numbers: ports, port ranges, version numbers (e.g. 3.12.7), counts,
  percentages, prices, quantities, account IDs, PR numbers, UUIDs
- Network addresses: IPs, hostnames, URLs, bind addresses (0.0.0.0:8080)
- Identifiers: emails, phone numbers, usernames, role names
- File paths and config keys: env var names, secret names, table names,
  database names, file/directory paths
- Names: people, companies, products, projects, branches
- Dates and times: deadlines, timestamps, schedule changes
- Status changes: deprecation notices, "moved from X to Y" pairs

If a specific value appears in the conversation and is not pure noise,
list it explicitly. Brevity is NOT an excuse to drop a value.

REPETITIVE OPERATIONS RULE (#179): a sequence of similar tool calls or
one bulk operation (a sweep, a batch of queries, a mass update) MUST be
compressed to a single line stating the operation, its scale, and that
it COMPLETED (e.g. "Ran 350 search queries across 7 weight ratios —
COMPLETED; results saved to docs/results.json"). Never enumerate the
individual operations, and never describe a completed bulk operation in
a way that reads as a pending plan.

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
- **[Decision]**: [Rationale, including any specific values mentioned]

## Conversation Dynamics
- [User tone, preferences expressed]
- [Communication style, behavioral instructions given]
- [Unresolved questions]

## Next Steps
1. [Ordered list]

## Critical Context
This section is your fact ledger — it MUST list every specific value
from the conversation in raw form. Group by topic but do not omit.
Format: `- <topic>: <verbatim value or assertion>`
Example: `- Redis port (staging): 6380 (not the default 6379)`
Example: `- Primary contact: Marcus Webb (marcus.webb@acme.com)`
Example: `- Deploy region (default): us-east-2`
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
7. REPETITIVE OPERATIONS RULE (#179): a sequence of similar tool calls
   or one bulk operation MUST be compressed to a single line stating
   the operation, its scale, and that it COMPLETED. Never enumerate
   the individual operations, and never describe a completed bulk
   operation in a way that reads as a pending plan.

Output ONLY the updated summary."""

# Section patterns for validation (case-insensitive, flexible)
_SECTION_PATTERNS = [
    re.compile(r"##\s*goals?\b", re.IGNORECASE),
    re.compile(r"##\s*progress\b", re.IGNORECASE),
    re.compile(r"##\s*critical\s*context\b", re.IGNORECASE),
]


# F059 (2026-05-05): entity-token regexes for the compaction hallucination
# guard. These match the kinds of values that, if substituted by the
# summarizer, produce confident-looking-but-wrong summaries. Patterns are
# anchored to characters that cannot appear inside paraphrasing (`@`, `:`,
# `.` between digits, `/` between path segments) so the false-positive
# rate from prose match is low.
_HALLUCINATION_ENTITY_PATTERNS = [
    # Emails — primary substitution target
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    # URLs (http/https/ftp)
    re.compile(r"https?://[^\s<>'\"]+"),
    # IPv4 addresses (with or without :port)
    re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?\b"),
    # Version strings: 1.2, 1.2.3, 1.2.3-rc1
    re.compile(r"\b\d+\.\d+(?:\.\d+)?(?:-[A-Za-z0-9]+)?\b"),
    # File paths: at least two slash-separated segments with a word char start
    re.compile(r"(?:/[A-Za-z0-9_.-]+){2,}/?"),
    # Multi-word capitalized name tokens (`Marcus Webb`, `Sarah Chen`,
    # `Acme Corp`). At least 2 capitalized words back-to-back.
    re.compile(r"\b(?:[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b"),
    # Bare port numbers prefixed with `port ` or `:` (`port 6380`, `:8080`)
    re.compile(r"(?:\bport\s+|(?<=:))\d{2,5}\b", re.IGNORECASE),
    # Hex identifiers (8+ chars), commit SHAs, UUIDs
    re.compile(r"\b[A-Fa-f0-9]{8,}\b"),
]


def _extract_entities(text: str) -> set[str]:
    """Extract entity tokens worth defending against substitution.

    Skips markdown header lines (`## Critical Context` would otherwise
    match the multi-word capitalized name regex and produce structural
    false positives). Lowercases everything so the input/summary
    comparison is case-insensitive.
    """
    out: set[str] = set()
    if not text:
        return out
    body = "\n".join(
        line for line in text.split("\n") if not line.lstrip().startswith("#")
    )
    for pat in _HALLUCINATION_ENTITY_PATTERNS:
        for match in pat.findall(body):
            tok = match.strip().lower()
            if len(tok) >= 3:
                out.add(tok)
    return out


def detect_hallucinated_entities(input_text: str, summary: str) -> list[str]:
    """Return entities present in `summary` but absent from `input_text`.

    Two rules for "found":
      1. Direct substring match on lowercased input (catches verbatim
         preservation, including partial overlaps like `:8080` in
         `0.0.0.0:8080`).
      2. For multi-word entities (e.g. `marcus webb`), at least one
         word of length >= 4 appears as a substring in input. This
         covers the case where input has `marcus.webb@acme.com` and
         summary unpacks the name into `Marcus Webb` — same person,
         different surface form.
    """
    if not input_text or not summary:
        return []
    input_lc = input_text.lower()
    suspects: list[str] = []
    for ent in _extract_entities(summary):
        if ent in input_lc:
            continue
        words = ent.split()
        if len(words) > 1 and any(
            len(w) >= 4 and w in input_lc for w in words
        ):
            continue
        suspects.append(ent)
    return sorted(suspects)


# F058 follow-up (2026-05-01): tool-use schema for structured compaction.
# The eval (reports/eval_compaction_fidelity.md) measured 31% silent fact
# loss with the free-form prompt approach. By forcing the model to enumerate
# facts in a `critical_context` array (one entry per specific value), the
# fact ledger becomes part of the structured output and can't be silently
# paraphrased away. The legacy markdown format is reconstructed from the
# structured dict so downstream consumers (validate, prompt rendering) see
# the same shape they always have.
_CHECKPOINT_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "goal": {
            "type": "string",
            "description": "1-2 sentence goal of the conversation.",
        },
        "constraints": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Requirements, technical constraints, preferences.",
        },
        "progress_done": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Completed work items.",
        },
        "progress_in_progress": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Active/unfinished work.",
        },
        "key_decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "decision": {"type": "string"},
                    "rationale": {"type": "string"},
                },
                "required": ["decision", "rationale"],
            },
            "description": "Decisions made with their rationale.",
        },
        "conversation_dynamics": {
            "type": "array",
            "items": {"type": "string"},
            "description": "User tone, style preferences, behavioral instructions.",
        },
        "next_steps": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Ordered list of next actions.",
        },
        "critical_context": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "Short label for what this value is.",
                    },
                    "value": {
                        "type": "string",
                        "description": "Verbatim value. Numbers, IPs, emails, ports, version numbers, account IDs, file paths, names, dates, identifiers — copy as stated. Do NOT paraphrase.",
                    },
                },
                "required": ["topic", "value"],
            },
            "description": "Fact ledger — every specific value mentioned in the conversation. Each entry survives compaction verbatim. This is the load-bearing field.",
        },
    },
    "required": [
        "goal", "constraints", "progress_done", "progress_in_progress",
        "key_decisions", "conversation_dynamics", "next_steps",
        "critical_context",
    ],
}


def _format_structured_checkpoint(data: dict[str, Any]) -> str:
    """Render the structured tool output back into the legacy markdown format.

    Downstream consumers (validation, the conversation prefix, future
    update calls) expect the section-headed markdown. Build it from the
    structured dict so the fact ledger is guaranteed to be present.
    """
    lines: list[str] = []
    lines.append("## Goal")
    lines.append(str(data.get("goal") or "(unspecified)").strip())
    lines.append("")

    constraints = data.get("constraints") or []
    if constraints:
        lines.append("## Constraints & Preferences")
        for c in constraints:
            lines.append(f"- {c}")
        lines.append("")

    lines.append("## Progress")
    done = data.get("progress_done") or []
    if done:
        lines.append("### Done")
        for d in done:
            lines.append(f"- [x] {d}")
    in_progress = data.get("progress_in_progress") or []
    if in_progress:
        lines.append("### In Progress")
        for d in in_progress:
            lines.append(f"- [ ] {d}")
    lines.append("")

    decisions = data.get("key_decisions") or []
    if decisions:
        lines.append("## Key Decisions")
        for d in decisions:
            if isinstance(d, dict):
                lines.append(f"- **{d.get('decision','')}**: {d.get('rationale','')}")
            else:
                lines.append(f"- {d}")
        lines.append("")

    dynamics = data.get("conversation_dynamics") or []
    if dynamics:
        lines.append("## Conversation Dynamics")
        for d in dynamics:
            lines.append(f"- {d}")
        lines.append("")

    next_steps = data.get("next_steps") or []
    if next_steps:
        lines.append("## Next Steps")
        for i, s in enumerate(next_steps, 1):
            lines.append(f"{i}. {s}")
        lines.append("")

    # The fact ledger — always emitted, even if empty (validator requires the
    # section header).
    lines.append("## Critical Context")
    crit = data.get("critical_context") or []
    if crit:
        for entry in crit:
            if isinstance(entry, dict):
                topic = entry.get("topic", "fact")
                value = entry.get("value", "")
                lines.append(f"- {topic}: {value}")
            else:
                lines.append(f"- {entry}")
    else:
        lines.append("- (no specific values recorded)")
    return "\n".join(lines).strip()


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


class EventLogger(Protocol):
    """Fire-and-forget event sink for guard verdicts.

    AgentRunner injects a closure that wraps `Brain.emit_event` in
    `asyncio.create_task` so the compaction path never blocks on DB I/O.
    """

    def __call__(
        self,
        event_type: str,
        data: dict[str, Any],
        session_id: str,
    ) -> None: ...


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

    def __init__(
        self,
        settings: Settings,
        event_logger: "EventLogger | None" = None,
    ) -> None:
        self._settings = settings
        self.estimator = TokenEstimator()
        # Optional fire-and-forget callback for persisting guard fires
        # to nous_system.events. Lets us reconstruct what F059 saw
        # after Docker log rotation. Wired in by AgentRunner; unwired
        # in tests + scripts.
        self._event_logger = event_logger

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

    # #179: bulk-result handling. A single oversized tool result (one
    # run_python holding a 350-call sweep) ages as ONE block, so under
    # per-tool profiles it dominates context for many turns and the model
    # replays the completed operation. Bulk results escalate to the 'bulk'
    # profile and their degrade/clear stubs carry explicit anti-replay text.
    _TRIM_MARKER_RE = re.compile(
        r"--- trimmed \(kept \d+ head \+ \d+ tail of (\d+) chars\) ---"
    )
    _BULK_HINT = "bulk operation completed earlier — do NOT re-run it"
    # codex P1: a failed sweep must not be stamped as completed — that would
    # suppress a legitimate retry and corrupt later summaries.
    _BULK_ERROR_HINT = "bulk operation FAILED earlier — fix the cause before any retry"

    def _original_result_size(self, text: str) -> int:
        """Original size of a result, surviving prior soft-trims.

        The soft-trim marker records the pre-trim size; once a bulk result
        is trimmed its current length no longer reflects bulkness, so the
        marker (or, later, the bulk hint itself) is the durable signal.
        """
        m = self._TRIM_MARKER_RE.search(text)
        return int(m.group(1)) if m else len(text)

    def _item_is_bulk(self, item: dict[str, Any]) -> bool:
        """True if this single tool-result item is bulk-sized (codex P1:
        per-item, so a bulk run_python can't drag a small sibling result
        from the same parallel-call message onto bulk decay ages)."""
        threshold = self._settings.tool_bulk_result_chars
        if threshold <= 0:
            return False
        text = item.get("content", "")
        if not isinstance(text, str):
            return False
        if self._BULK_HINT in text or self._BULK_ERROR_HINT in text:
            return True
        return self._original_result_size(text) >= threshold

    def _bulk_hint_for(self, item: dict[str, Any]) -> str:
        return self._BULK_ERROR_HINT if item.get("is_error") else self._BULK_HINT

    def _bulk_clear_stub(self, tool_name: str | None, item: dict[str, Any]) -> str:
        """#179 anti-replay hard-clear stub, error-aware (codex P1)."""
        name = tool_name or "tool"
        if item.get("is_error"):
            return (
                f"[Bulk tool output cleared — this {name} operation FAILED in "
                f"an earlier turn and its error output was cleared. "
                f"{self._BULK_ERROR_HINT}.]"
            )
        return (
            f"[Bulk tool output cleared — this {name} operation ran and "
            f"COMPLETED in an earlier turn; its results were already "
            f"processed. {self._BULK_HINT}.]"
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

            # Per-ITEM profile resolution (codex P1): one user message may
            # carry several results from one assistant turn's parallel tool
            # calls — a bulk run_python must not drag a small sibling
            # web_fetch onto bulk ages, and each item keeps its own tool's
            # decay profile + conservative fact extraction.
            msg_cleared = False
            msg_degraded = False
            msg_trimmed = False

            for item in content:
                if not isinstance(item, dict) or self._has_image_content(item):
                    continue

                tool_use_id = item.get("tool_use_id")
                tool_use_block = tool_use_index.get(tool_use_id) if tool_use_id else None
                tool_name = tool_use_block.get("name") if tool_use_block else None
                base_profile = TOOL_DECAY_PROFILES.get(tool_name or "", "standard")
                # #179: size escalation — bulk items decay on (1, 2, 4)
                # regardless of tool; base_profile keeps driving
                # conservative-tool fact extraction.
                is_bulk = self._item_is_bulk(item)
                profile_name = "bulk" if is_bulk else base_profile
                _soft_age, degrade_age, clear_age = DECAY_PROFILE_AGES.get(
                    profile_name, (3, 8, 12)
                )

                text = item.get("content", "")
                # Conservative-tool facts must be captured before the FIRST
                # content-destroying tier (codex P1: on the incremental
                # path, degrade precedes clear, so clear-time extraction
                # only ever saw the degrade stub). Extraction fires only
                # when this pass actually mutates the item (clear always
                # does; degrade only when _metadata_degrade's >=200-char
                # condition holds), and never re-fires on a stub
                # (" | first: " / "output cleared" markers).
                will_mutate = age >= clear_age or (
                    age >= degrade_age
                    and isinstance(text, str)
                    and len(text) >= 200
                )
                if (
                    will_mutate
                    and base_profile == "conservative"
                    and isinstance(text, str)
                    and text
                    and " | first: " not in text
                    and "output cleared" not in text
                ):
                    extracted.extend(
                        self._extract_facts_before_clear(tool_name or "unknown", text)
                    )

                # Tier 4: Hard-clear (age >= clear_age)
                if age >= clear_age:
                    if is_bulk:
                        # #179: anti-replay stub — the generic cleared text
                        # leaves the model free to re-derive "I should run
                        # the sweep"; this one states the outcome explicitly
                        # (completed vs failed — codex P1).
                        item["content"] = self._bulk_clear_stub(tool_name, item)
                    else:
                        item["content"] = (
                            "[Tool output cleared - content was processed in earlier turns]"
                        )
                    msg_cleared = True
                    continue

                # Tier 3: Metadata degrade (age >= degrade_age)
                if age >= degrade_age:
                    self._metadata_degrade(item, tool_use_block)
                    # #179: stamp the bulk hint so (a) the model sees the
                    # outcome signal and (b) bulkness survives to the
                    # clear tier after the trim marker is gone.
                    new_text = item.get("content", "")
                    hint = self._bulk_hint_for(item)
                    if (
                        is_bulk
                        and isinstance(new_text, str)
                        and hint not in new_text
                    ):
                        item["content"] = f"{new_text} [{hint}]"
                    msg_degraded = True
                    continue

                # Tier 2: Soft-trim (oversized results)
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
                    msg_trimmed = True

            if msg_cleared:
                hard_cleared += 1
            elif msg_degraded:
                metadata_degraded += 1
            elif msg_trimmed:
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

        # F059 hallucination guard. Substring-check entity tokens in the
        # summary against the input. Suspect = entity in summary but not
        # in input. Default warn-only; fallback to truncation only when
        # the operator opts in. Every fire is persisted to
        # nous_system.events when an event logger is wired in, so log
        # rotation doesn't lose the evidence we'd use to flip the
        # fallback flag later.
        if self._settings.compaction_hallucination_guard_enabled:
            input_text = self._serialize_for_summary(
                old_messages[serialize_start:]
            )
            suspects = detect_hallucinated_entities(input_text, checkpoint_text)
            max_allowed = self._settings.compaction_hallucination_max_suspect_count
            exceeded = len(suspects) > max_allowed
            fallback_taken = (
                exceeded
                and self._settings.compaction_hallucination_fallback_enabled
            )

            if suspects:
                self._persist_guard_fire(
                    conversation,
                    suspects,
                    max_allowed,
                    exceeded,
                    fallback_taken,
                    summary_chars=len(checkpoint_text),
                    input_chars=len(input_text),
                )

            if exceeded:
                logger.warning(
                    "Compaction hallucination guard: %d suspect entities "
                    "(threshold %d, session=%s): %s",
                    len(suspects),
                    max_allowed,
                    conversation.session_id,
                    suspects[:10],
                )
                if fallback_taken:
                    logger.warning(
                        "Compaction hallucination guard: falling back to "
                        "truncation (session=%s)",
                        conversation.session_id,
                    )
                    conversation.messages = conversation.messages[cut_point:]
                    conversation.summary = None
                    conversation.compaction_count = max(
                        0, conversation.compaction_count - 1
                    )
                    return
            elif suspects:
                logger.info(
                    "Compaction hallucination guard: %d suspect entities "
                    "(below threshold %d, session=%s): %s",
                    len(suspects),
                    max_allowed,
                    conversation.session_id,
                    suspects,
                )

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
        """Generate structured checkpoint summary via LLM.

        F058 follow-up (2026-05-01): when
        `compaction_structured_facts_enabled` is true, use a tool-use
        schema that forces the model to enumerate facts in an explicit
        array. The summary is then rendered from the structured output,
        guaranteeing the fact ledger survives. Falls back to the legacy
        free-form prompt path on tool-call failure or when disabled.
        """
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

        if getattr(self._settings, "compaction_structured_facts_enabled", False):
            tool_text = await self._summarize_structured(
                user_content, system, call_api
            )
            if tool_text:
                return tool_text
            # Fall through to legacy path on structured-output failure.
            logger.warning(
                "Compaction structured output failed; falling back to free-form prompt"
            )

        response = await call_api(
            system_prompt=system,
            messages=[{"role": "user", "content": user_content}],
            tools=None,
            skip_thinking=True,
            model_override=self._settings.background_model,
        )
        return self.extract_text(response.content)

    async def _summarize_structured(
        self,
        user_content: str,
        system: str,
        call_api: ApiCaller,
    ) -> str | None:
        """Tool-use path: ask the model to fill in the checkpoint schema.

        Returns rendered markdown on success, None on any failure
        (parse error, missing tool_use block, schema fields missing).
        """
        tools = [
            {
                "name": "checkpoint_summary",
                "description": (
                    "Produce a structured checkpoint summary of the conversation. "
                    "Every specific value (number, IP, email, port, version, name, "
                    "date, file path, identifier) MUST appear verbatim in the "
                    "critical_context array — that's the fact ledger that survives "
                    "compaction."
                ),
                "input_schema": _CHECKPOINT_TOOL_SCHEMA,
            }
        ]
        try:
            response = await call_api(
                system_prompt=system,
                messages=[{"role": "user", "content": user_content}],
                tools=tools,
                skip_thinking=True,
                model_override=self._settings.background_model,
            )
        except Exception:
            logger.exception("Compaction structured tool call failed")
            return None

        # Extract the tool_use block.
        for block in response.content or []:
            if block.get("type") == "tool_use" and block.get("name") == "checkpoint_summary":
                payload = block.get("input") or {}
                if not isinstance(payload, dict):
                    return None
                rendered = _format_structured_checkpoint(payload)
                # Codex P1 follow-up to #395: align with the downstream
                # `_validate_summary` 200-char minimum. Returning at >=100
                # but failing validation at 200 wastes a fallback API call.
                # Returning None here triggers the fallback ONCE.
                if len(rendered) < 200:
                    logger.info(
                        "Compaction structured output rendered to %d chars (<200); "
                        "falling back to free-form prompt.",
                        len(rendered),
                    )
                    return None
                return rendered
        return None

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

    def _persist_guard_fire(
        self,
        conversation: Conversation,
        suspects: list[str],
        threshold: int,
        exceeded: bool,
        fallback_taken: bool,
        *,
        summary_chars: int,
        input_chars: int,
    ) -> None:
        """Best-effort persist of the F059 guard verdict.

        Skipped when persistence is disabled or no event logger is
        wired. Never raises — the compaction path must be unaffected.
        """
        if not self._settings.compaction_hallucination_persist_enabled:
            return
        if self._event_logger is None:
            return
        try:
            self._event_logger(
                "f059_hallucination_guard",
                {
                    "session_id": conversation.session_id,
                    "compaction_count": conversation.compaction_count + 1,
                    "suspect_count": len(suspects),
                    # Cap at 20 entries so we don't blow up the events
                    # table on a runaway summarizer. Hand-audit reads
                    # the first few anyway.
                    "suspects": suspects[:20],
                    "threshold": threshold,
                    "exceeded_threshold": exceeded,
                    "fallback_taken": fallback_taken,
                    "summary_chars": summary_chars,
                    "input_chars": input_chars,
                },
                conversation.session_id,
            )
        except Exception:  # noqa: BLE001
            logger.debug(
                "F059 guard persistence failed (suppressed)", exc_info=True
            )

    @staticmethod
    def extract_text(content: list[dict[str, Any]]) -> str:
        """Extract text from API response content blocks."""
        return "".join(
            block.get("text", "")
            for block in content
            if block.get("type") == "text"
        )
