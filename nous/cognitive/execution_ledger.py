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

from nous.cognitive.bash_side_effect import classify_bash_command as _classify_bash_command
from nous.cognitive.bash_side_effect import command_runs as _command_runs

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
    "recall_hubs",
    "list_decisions",
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
    "heartbeat_check_manage",
    "heartbeat_check_create",
}

# External side effects — leave the host (message delivery, remote pushes)
EXTERNAL_TOOLS: set[str] = {
    "send_file",   # Sends files to Telegram
    "send_email",  # Guarded SMTP send (email_tools.py), registered after F026
}

# Irreversible — extend when irreversible tools are registered
IRREVERSIBLE_TOOLS: set[str] = set()

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
    "heartbeat_check_manage": ["action", "name"],
    "heartbeat_check_create": ["name", "prompt"],
    "send_file": ["file_path"],
    # Recipient + subject identify a send; the 5-arg fallback captured body[:80].
    "send_email": ["to", "cc", "subject"],
}

# Argument values a completion claim can be checked against (harness Phase 2c).
# In-memory only -- never persisted, never rendered into the prompt. Bounded
# head AND tail, so a long commit message still shows the `git push` after it.
EVIDENCE_ARG_CHARS = 2000
_CODE_CHARS_FACTOR = 8  # a script is judged from its syntax tree: keep more of it
# Marks a cut: what carries it cannot be parsed and is unreadable to the
# verifier, never regex-scanned (a cut string literal is not code).
EVIDENCE_TRUNCATED = "\n…[truncated]…\n"
_EVIDENCE_ARGS: dict[str, tuple[str, ...]] = {
    "bash": ("command", "cmd"),
    "run_python": ("code",),
    "write_file": ("path", "file_path"),
    "send_email": ("to", "cc", "subject"),
    "send_file": ("file_path", "chat_id", "caption"),
}
# bash_tool always appends this trailer: the authoritative wrapper status.
_BASH_EXIT_CODE = re.compile(r"(?:\A|\n)Exit code: (-?\d+)\s*\Z")


def _bounded(value: str, limit: int | None) -> str:
    if limit is None or len(value) <= limit:
        return value
    half = limit // 2
    return f"{value[:half]}{EVIDENCE_TRUNCATED}{value[-half:]}"


def evidence_args(
    tool_name: str, tool_input: dict[str, Any], *, limit: int | None = EVIDENCE_ARG_CHARS,
) -> dict[str, str]:
    """The argument values a claim about this call can be checked against."""
    return {
        key: _bounded(str(tool_input[key]),
                      limit * _CODE_CHARS_FACTOR if limit is not None and key == "code" else limit)
        for key in _EVIDENCE_ARGS.get(tool_name, ())
        if tool_input.get(key) is not None
    }


Invocation = tuple[str, tuple[str, ...], bool]  # program, arguments, certainly ran and succeeded
# Bounds on what the in-memory ledger keeps of a bash command's invocations.
# Past them the command is kept as UNREADABLE (None), never as fewer commands:
# a cut list could drop the `git push` at the end.
_MAX_INVOCATIONS = 64
_MAX_INVOCATION_ARGS = 24
_MAX_INVOCATION_ARG_CHARS = 120
_MAX_HEREDOC_CHARS = 4000  # a heredoc body (a "\n"-prefixed argument) is judged as code


def _bound_arg(arg: str) -> str:
    return arg[:_MAX_HEREDOC_CHARS] if arg.startswith("\n") else arg[:_MAX_INVOCATION_ARG_CHARS]


def bash_invocations(
    command: str, exit_code: int | None, *, bound: bool = True,
) -> tuple[Invocation, ...] | None:
    """What a bash command runs, read from the WHOLE command, each marked
    certain when it ran and succeeded (see ``command_runs``); None when it
    cannot be read.

    ``bound`` (the ledger) caps what is kept in memory. Read at record time
    because the ledger's bounded copy of the command can drop a `git push` in
    its middle, or cut a quote in half and become unreadable.
    """
    found = _command_runs(command, exit_code)
    if found is None:
        return None
    if not bound:
        return tuple((prog, tuple(args), certain) for prog, args, certain in found)
    if len(found) > _MAX_INVOCATIONS:
        return None
    return tuple(
        (prog, tuple(_bound_arg(a) for a in args[:_MAX_INVOCATION_ARGS]), certain)
        for prog, args, certain in found
    )


def bash_exit_code(result: str | None) -> int | None:
    """Exit code from bash_tool's trailer; None when absent (timeout, spawn error)."""
    if not result:
        return None
    match = _BASH_EXIT_CODE.search(result)
    return int(match.group(1)) if match else None


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
    exit_code: int | None = None  # bash only: a non-zero exit is not evidence
    evidence_args: dict[str, str] = field(default_factory=dict)
    # bash only: what the command runs, read at record time; None = unreadable
    invocations: tuple[Invocation, ...] | None = None


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
            exit_code=bash_exit_code(str(result)) if tool_name == "bash" else None,
            evidence_args=evidence_args(tool_name, tool_input),
            invocations=(bash_invocations(_extract_bash_command(tool_input), bash_exit_code(str(result)))
                         if tool_name == "bash" else None),
        )
        self.actions.append(action)
        if status == "blocked":
            logger.info(
                "F026 ledger: %s BLOCKED (turn %d, %s)",
                tool_name, self._current_turn, side_effect,
            )
        else:
            logger.info(
                "F026 ledger: %s → %s (turn %d, %s)",
                tool_name, status, self._current_turn, side_effect,
            )
        return action

    @property
    def current_turn(self) -> int:
        """Public accessor for the current turn number."""
        return self._current_turn

    @property
    def has_blocked_actions_this_turn(self) -> bool:
        """True if any action in the current turn has status 'blocked'."""
        return any(
            a.status == "blocked" and a.turn == self._current_turn
            for a in self.actions
        )

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
                    _estimate_tokens(text), len(self.actions), self.session_id,
                )
                return text

        # Still over budget after window=1 — truncate grouped summary
        text = self._build_section(1, truncate_grouped=True, max_tokens=max_tokens)
        logger.info(
            "F026 ledger prompt (truncated): ~%d tokens, %d actions, session=%s",
            _estimate_tokens(text), len(self.actions), self.session_id,
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
            effect_marker = (
                "" if a.side_effect_type == "none"
                else f" ({a.side_effect_type})"
            )
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
        return summarize_args(tool_name, args)


# ---------------------------------------------------------------------------
# Module-level helpers (no class state needed)
# ---------------------------------------------------------------------------


def summarize_args(tool_name: str, args: dict[str, Any]) -> dict[str, str]:
    """Extract key identifying args and truncate values to 80 chars.

    The in-memory session ledger's summary (prompt aid, ActionGate duplicate
    key). The durable ledger uses ``ledger_store.durable_key_args`` instead,
    which never stores bodies or code.
    """
    key_names = _KEY_ARGS.get(tool_name, [])
    result: dict[str, str] = {}

    if key_names:
        for name in key_names:
            if name in args:
                result[name] = str(args[name])[:80]
    else:
        # Fallback: capture up to 5 args for unknown tools
        for k, v in list(args.items())[:5]:
            result[k] = str(v)[:80]

    return result


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
    # user:password@host - the password may itself contain '@', so match to the LAST '@'
    (re.compile(r"://[^/\s:@]+:\S+@"), "://[REDACTED]@"),
    (re.compile(r"(-u\s+)[^\s:]+:\S+"), r"\1[REDACTED]"),
    # -p<password> only for tools that take it that way (not find -path, cp -pr, ssh -p 22)
    (re.compile(r"(\b(?:mysql|mysqldump|mysqladmin|mariadb)\b[^|;&]*?\s-p)(?!\s)\S+"), r"\1[REDACTED]"),
    (re.compile(r"(\bsshpass\s+-p\s*)\S+"), r"\1[REDACTED]"),
    (re.compile(r"(--(?:password|passwd|api-key|api_key|token|secret)[=\s])\S+", re.IGNORECASE),
     r"\1[REDACTED]"),
    # header value, including an optional scheme word (Basic / token); Bearer
    # is left to the Bearer pattern above so its output keeps the scheme.
    (re.compile(r"((?:x-api-key|api-key|authorization)\s*:\s*)(?!Bearer\s)(?:[A-Za-z]+\s+)?[^'\"\s]+",
                re.IGNORECASE),
     r"\1[REDACTED]"),
    (re.compile(r"((?:api_key|apikey|access_token|token|secret|password|passwd)=)[^&\s'\"]+", re.IGNORECASE),
     r"\1[REDACTED]"),
    (re.compile(r'("(?:password|passwd|secret|token|api_key|apikey)"\s*:\s*")[^"]*"', re.IGNORECASE),
     r'\1[REDACTED]"'),
]


def redact_text(text: str) -> str:
    """Apply every redaction pattern. Safe for any tool's argument or output."""
    for pattern, replacement in _REDACT_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact_key_args(tool_name: str, key_args: dict[str, str]) -> dict[str, str]:
    """Redact sensitive patterns from key_args before external exposure."""
    if tool_name != "bash":
        return key_args
    result = {}
    for k, v in key_args.items():
        result[k] = redact_text(v)
    return result


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
