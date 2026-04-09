"""Execution Ledger — F026 Execution Integrity, Phase B.

Tracks every tool call made within a session so the agent can be held
accountable for what it has (and has not) actually done.  The ledger is
session-scoped and in-memory only — no database dependency.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool classification constants (REVIEWED — matches actual registered tools)
# ---------------------------------------------------------------------------

# No side effects — pure reads
READ_TOOLS: set[str] = {
    "recall_deep",
    "recall_recent",
    "read_file",
    "get_procedure",
    "web_search",
    "web_fetch",
    "list_tasks",
    "cache_retrieve",
}

# Local writes — reversible
WRITE_TOOLS: set[str] = {
    "write_file",
    "learn_fact",
    "record_decision",
    "create_censor",
    "store_identity",
    "learn_skill",
    "complete_initiation",
    "spawn_task",
    "schedule_task",
    "cancel_task",
    "run_python",  # Can call learn_fact() and modify state
}

# External side effects — extend when email/notification tools are registered
EXTERNAL_TOOLS: set[str] = set()

# Irreversible — extend when irreversible tools are registered
IRREVERSIBLE_TOOLS: set[str] = set()

# Bash commands whose first token indicates a read-only operation
_READ_COMMANDS: frozenset[str] = frozenset(
    {
        "cat",
        "ls",
        "ll",
        "find",
        "grep",
        "rg",
        "awk",
        "sed",
        "head",
        "tail",
        "wc",
        "diff",
        "stat",
        "file",
        "echo",
        "printf",
        "which",
        "type",
        "pwd",
        "env",
        "printenv",
        "less",
        "more",
        "sort",
        "uniq",
        "cut",
        "tr",
        "basename",
        "dirname",
        "realpath",
        "readlink",
    }
)

# Key argument names per tool — used by _summarize_args
_KEY_ARGS: dict[str, list[str]] = {
    "write_file": ["path", "file_path"],
    "read_file": ["path", "file_path"],
    "bash": ["command", "cmd"],
    "learn_fact": ["subject", "content", "fact"],
    "learn_skill": ["name", "url", "path"],
    "recall_deep": ["query", "q"],
    "recall_recent": ["query", "q", "limit"],
    "record_decision": ["title", "decision", "description"],
    "create_censor": ["name", "expression"],
    "store_identity": ["section", "key"],
    "spawn_task": ["description", "task"],
    "schedule_task": ["description", "task", "schedule"],
    "cancel_task": ["task_id", "id"],
    "web_search": ["query", "q"],
    "web_fetch": ["url"],
    "run_python": [],  # Too large to summarize — spec says skip
    "get_procedure": ["name", "procedure_name"],
}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class ExecutedAction:
    """A single recorded tool execution within a session."""

    turn: int
    tool_name: str
    key_args: dict[str, str]
    status: str  # "success" | "error" | "timeout" | "blocked"
    timestamp: datetime
    result_summary: str  # First 100 chars of result
    side_effect_type: str  # "none" | "write" | "external" | "irreversible"


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


@dataclass
class ExecutionLedger:
    """Session-scoped, in-memory record of every tool call."""

    session_id: str
    actions: list[ExecutedAction] = field(default_factory=list)
    _current_turn: int = field(default=0, init=False, repr=False)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_turn(self, turn: int) -> None:
        """Called at the start of each agent turn."""
        self._current_turn = turn

    def record(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        result: str,
        status: str,
    ) -> ExecutedAction:
        """Record a completed (or blocked) tool execution and return it."""
        side_effect = self._classify_side_effect(tool_name, tool_input)
        action = ExecutedAction(
            turn=self._current_turn,
            tool_name=tool_name,
            key_args=self._summarize_args(tool_name, tool_input),
            status=status,
            timestamp=datetime.now(UTC),
            result_summary=str(result)[:100],
            side_effect_type=side_effect,
        )
        self.actions.append(action)
        if status == "blocked":
            logger.info(
                "F026 ledger: %s BLOCKED (turn %d, %s)",
                tool_name,
                self._current_turn,
                side_effect,
            )
        else:
            logger.info(
                "F026 ledger: %s → %s (turn %d, %s)",
                tool_name,
                status,
                self._current_turn,
                side_effect,
            )
        return action

    @property
    def current_turn(self) -> int:
        """Public accessor for the current turn number."""
        return self._current_turn

    @property
    def has_blocked_actions_this_turn(self) -> bool:
        """True if any action in the current turn has status 'blocked'."""
        return any(a.status == "blocked" and a.turn == self._current_turn for a in self.actions)

    def one_line_summary(self) -> str:
        """Human-readable single-line summary, e.g. '12 searches, 3 file writes, 1 bash'."""
        if not self.actions:
            return "no actions recorded"

        counts: Counter[str] = Counter()
        for a in self.actions:
            label = _friendly_label(a.tool_name)
            counts[label] += 1

        parts = [f"{n} {label}" for label, n in counts.most_common()]
        return ", ".join(parts)

    def system_prompt_section(self, max_tokens: int = 500) -> str:
        """Return a compact ledger section for the system prompt.

        Groups actions older than the last ``recent_turns`` turns into a
        summary line and lists recent actions individually.  Enforces the
        token budget by shrinking the recent window and then truncating the
        grouped summary.
        """
        if not self.actions:
            return ""

        # Try progressively smaller recent windows until we fit the budget
        for recent_turns in (5, 3, 1):
            text = self._build_section(recent_turns)
            if _estimate_tokens(text) <= max_tokens:
                logger.info(
                    "F026 ledger prompt: ~%d tokens, %d actions, session=%s",
                    _estimate_tokens(text),
                    len(self.actions),
                    self.session_id,
                )
                return text

        # Still over budget after window=1 — truncate grouped summary
        text = self._build_section(1, truncate_grouped=True, max_tokens=max_tokens)
        logger.info(
            "F026 ledger prompt (truncated): ~%d tokens, %d actions, session=%s",
            _estimate_tokens(text),
            len(self.actions),
            self.session_id,
        )
        return text

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_section(
        self,
        recent_turns: int,
        *,
        truncate_grouped: bool = False,
        max_tokens: int = 500,
    ) -> str:
        """Build the system prompt section with the given recent-turn window."""
        if not self.actions:
            return ""

        cutoff_turn = self._current_turn - recent_turns
        old_actions = [a for a in self.actions if a.turn < cutoff_turn]
        recent_actions = [a for a in self.actions if a.turn >= cutoff_turn]

        lines: list[str] = ["[Execution Ledger]"]

        # Grouped summary of older actions
        if old_actions:
            summary = _group_summary(old_actions)
            if truncate_grouped:
                budget_chars = max_tokens * 4 - len("\n".join(lines)) - 200
                if budget_chars > 0:
                    summary = summary[:budget_chars] + ("…" if len(summary) > budget_chars else "")
            lines.append(f"Prior turns: {summary}")

        # Individual lines for recent actions
        for a in recent_actions:
            arg_str = _format_key_args(a.key_args)
            status_marker = "" if a.status == "success" else f" [{a.status.upper()}]"
            effect_marker = "" if a.side_effect_type == "none" else f" ({a.side_effect_type})"
            line = f"  T{a.turn} {a.tool_name}{arg_str}{effect_marker}{status_marker}"
            if a.status in ("error", "blocked") and a.result_summary:
                line += f": {a.result_summary[:60]}"
            lines.append(line)

        return "\n".join(lines)

    def _classify_side_effect(
        self,
        tool_name: str,
        tool_input: dict[str, Any] | None = None,
    ) -> str:
        """Return 'none' | 'write' | 'external' | 'irreversible'."""
        if tool_name in IRREVERSIBLE_TOOLS:
            return "irreversible"
        if tool_name in EXTERNAL_TOOLS:
            return "external"
        if tool_name in READ_TOOLS:
            return "none"
        if tool_name in WRITE_TOOLS:
            return "write"
        if tool_name == "bash":
            command = _extract_bash_command(tool_input or {})
            return self._classify_bash(command)
        # Unknown tool — conservative default
        return "write"

    def _classify_bash(self, command: str) -> str:
        """Classify a bash command. Delegates to module-level function."""
        return _classify_bash_command(command)

    def _summarize_args(
        self,
        tool_name: str,
        args: dict[str, Any],
    ) -> dict[str, str]:
        """Extract key identifying args and truncate values to 80 chars."""
        key_names = _KEY_ARGS.get(tool_name, [])
        result: dict[str, str] = {}

        if key_names:
            for name in key_names:
                if name in args:
                    result[name] = str(args[name])[:80]
                    break  # Only first matching key
        else:
            # Fallback: first arg, truncated
            for k, v in args.items():
                result[k] = str(v)[:80]
                break

        return result


# ---------------------------------------------------------------------------
# Module-level helpers (no class state needed)
# ---------------------------------------------------------------------------


def classify_side_effect(tool_name: str, tool_input: dict[str, Any] | None = None) -> str:
    """Module-level classifier for use by ActionGate and other modules."""
    if tool_name in IRREVERSIBLE_TOOLS:
        return "irreversible"
    if tool_name in EXTERNAL_TOOLS:
        return "external"
    if tool_name in READ_TOOLS:
        return "none"
    if tool_name in WRITE_TOOLS:
        return "write"
    if tool_name == "bash":
        return _classify_bash_command(_extract_bash_command(tool_input or {}))
    return "write"


_REDACT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"[A-Z_]{2,}=\S+"), "[REDACTED_ENV]"),
    (re.compile(r"Bearer\s+\S+", re.IGNORECASE), "Bearer [REDACTED]"),
    (re.compile(r"://\w+:[^@\s]+@"), "://[REDACTED]@"),
]


def redact_key_args(tool_name: str, key_args: dict[str, str]) -> dict[str, str]:
    """Redact sensitive patterns from key_args before external exposure."""
    if tool_name != "bash":
        return key_args
    result = {}
    for k, v in key_args.items():
        redacted = v
        for pattern, replacement in _REDACT_PATTERNS:
            redacted = pattern.sub(replacement, redacted)
        result[k] = redacted
    return result


def _classify_bash_command(command: str) -> str:
    """Classify a bash command as 'none' | 'write' | 'external'.

    Only the first command token is inspected; pipes and chains are
    approximate — the conservative default is 'write'.
    """
    if not command:
        return "write"

    # Strip leading environment assignments (FOO=bar cmd ...)
    tokens = command.strip().split()
    first = ""
    for tok in tokens:
        if "=" not in tok:
            first = tok.lstrip("(").lower()
            break

    if first in _READ_COMMANDS:
        return "none"

    if first == "git":
        sub = tokens[tokens.index("git") + 1] if "git" in tokens else ""
        if sub in ("log", "status", "diff", "show", "branch", "tag", "remote", "ls-files"):
            return "none"
        if sub in ("push", "push-upstream"):
            return "external"
        return "write"

    if first in ("curl", "wget", "http", "httpie"):
        return "external"

    return "write"


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: chars // 4."""
    return len(text) // 4


def _group_summary(actions: list[ExecutedAction]) -> str:
    """Return a compact count-by-tool string for a list of actions."""
    counts: Counter[str] = Counter(a.tool_name for a in actions)
    return ", ".join(f"{n}x {name}" for name, n in counts.most_common())


def _format_key_args(key_args: dict[str, str]) -> str:
    """Format key args as a compact string, e.g. ' path=foo.py'."""
    if not key_args:
        return ""
    pairs = " ".join(f"{k}={v}" for k, v in key_args.items())
    return f" {pairs}"


def _extract_bash_command(tool_input: dict[str, Any]) -> str:
    """Pull the command string out of a bash tool_input dict."""
    for key in ("command", "cmd"):
        if key in tool_input:
            return str(tool_input[key])
    return ""


def _friendly_label(tool_name: str) -> str:
    """Map tool names to human-readable activity labels."""
    _LABELS: dict[str, str] = {
        "recall_deep": "searches",
        "recall_recent": "searches",
        "web_search": "searches",
        "web_fetch": "fetches",
        "read_file": "file reads",
        "write_file": "file writes",
        "bash": "bash",
        "learn_fact": "fact stores",
        "record_decision": "decisions",
        "spawn_task": "tasks spawned",
        "schedule_task": "schedules",
        "run_python": "python runs",
    }
    return _LABELS.get(tool_name, tool_name)
