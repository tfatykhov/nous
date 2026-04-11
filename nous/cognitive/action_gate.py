"""F026 Execution Integrity — Phase E: Action Gating.

Tiered gate that approves or blocks tool calls before dispatch:
  Tier 1 (read-only)       → always approved
  Tier 2 (local write)     → duplicate / consistency check
  Tier 3 (external/irrev.) → LLM full gate with 5 s timeout, fail-open
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from nous.cognitive.execution_ledger import ExecutedAction, classify_side_effect

if TYPE_CHECKING:
    from nous.cognitive.execution_ledger import ExecutionLedger
    from nous.config import Settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# F026.1: Iterative command patterns
# ---------------------------------------------------------------------------

# POSIX env var assignment prefix (e.g. PYTHONPATH=., CC=gcc)
_ENV_VAR_PREFIX = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=')

# First token of bash commands that are inherently iterative (build/test/lint)
ITERATIVE_COMMAND_TOKENS: frozenset[str] = frozenset({
    "make", "cmake", "ninja",           # Build systems
    "pytest", "python", "node",          # Test runners
    "npm", "yarn", "pnpm", "bun",       # JS package managers / runners
    "cargo", "go", "mvn", "gradle",     # Language build tools
    "docker", "docker-compose",          # Container builds
    "gcc", "g++", "clang", "rustc",     # Compilers
    "tsc", "eslint", "ruff", "mypy",    # Linters / type checkers
    "terraform", "ansible",              # IaC tools
    "gh",                                # GitHub CLI (PR creation retries)
})

# Sub-commands for multi-token tools that indicate iterative use
ITERATIVE_SUBCOMMANDS: dict[str, frozenset[str]] = {
    "npm": frozenset({"run", "test", "build", "start"}),
    "cargo": frozenset({"build", "test", "check", "run", "clippy"}),
    "go": frozenset({"build", "test", "run", "vet"}),
    "docker": frozenset({"build", "run"}),
    "gh": frozenset({"pr", "issue"}),
}


@dataclass
class GateResult:
    """Result of an ActionGate check."""

    approved: bool
    reason: str
    suggestion: str | None = None

    @classmethod
    def from_json(cls, text: str) -> GateResult:
        """Parse a GateResult from an LLM response string.

        Expects JSON with keys ``approved`` (bool) and ``reason`` (str).
        Optionally includes ``suggestion`` (str).  On any parse failure the
        gate fails **open** (approved=True) so a bad model response never
        silently blocks legitimate work.
        """
        try:
            # Strip markdown fences if present
            stripped = text.strip()
            if stripped.startswith("```"):
                lines = stripped.splitlines()
                # Drop opening and closing fence lines
                inner = [l for l in lines[1:] if not l.startswith("```")]
                stripped = "\n".join(inner).strip()

            data = json.loads(stripped)
            approved = bool(data.get("approved", True))
            reason = str(data.get("reason", "gate-response"))
            suggestion = str(data["suggestion"]) if "suggestion" in data else None
            return cls(approved=approved, reason=reason, suggestion=suggestion)
        except Exception as exc:  # noqa: BLE001
            logger.warning("GateResult.from_json parse error (%s) — failing open", exc)
            return cls(approved=True, reason=f"gate-parse-error-fail-open: {exc}")


class ActionGate:
    """Tiered pre-dispatch gate for tool calls.

    Parameters
    ----------
    settings:
        Nous settings (used for feature flags / model name).
    call_gate_model:
        Optional async callable ``(prompt: str) -> str`` used for Tier 3
        full-gate checks.  When *None*, Tier 3 always fails open.
    """

    def __init__(
        self,
        settings: Settings,
        call_gate_model: Callable[..., object] | None = None,
    ) -> None:
        self._settings = settings
        self._call_gate_model = call_gate_model

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def check(
        self,
        tool_name: str,
        tool_input: dict,
        ledger: ExecutionLedger,
        user_message: str = "",
    ) -> GateResult:
        """Gate a proposed tool call.

        Returns a :class:`GateResult` indicating whether the call should
        proceed.  Never raises — all exceptions are caught and the gate fails
        open so infrastructure errors never silently kill legitimate work.
        """
        try:
            side_effect = classify_side_effect(tool_name, tool_input)

            if side_effect == "none":
                # Tier 1: read-only — always approved
                return GateResult(approved=True, reason="read-only")

            if side_effect == "write":
                # Tier 2: local write — duplicate check
                if self._settings.action_gating_external_only:
                    return GateResult(approved=True, reason="external-only-mode")
                return self._consistency_check(tool_name, tool_input, ledger)

            if side_effect in ("external", "irreversible"):
                # Tier 3: high-stakes — LLM gate with timeout
                return await self._full_gate(tool_name, tool_input, ledger, user_message)

            # Unknown classification — fail open
            return GateResult(approved=True, reason=f"unknown-side-effect:{side_effect}")

        except Exception as exc:  # noqa: BLE001
            logger.warning("ActionGate.check error — failing open: %s", exc)
            return GateResult(approved=True, reason=f"gate-check-error-fail-open: {exc}")

    # ------------------------------------------------------------------
    # Tier 2: consistency / duplicate check
    # ------------------------------------------------------------------

    def _consistency_check(
        self,
        tool_name: str,
        tool_input: dict,
        ledger: ExecutionLedger,
    ) -> GateResult:
        """Multi-layer duplicate detection with change-awareness (F026.1)."""
        new_key_args = ledger._summarize_args(tool_name, tool_input)  # noqa: SLF001

        turn_window = self._settings.action_gating_turn_window
        min_turn = ledger.current_turn - turn_window

        # Find all matching prior successes in window
        matches: list[tuple[int, ExecutedAction]] = []
        actions_slice = ledger.actions[-20:]
        offset = max(0, len(ledger.actions) - 20)
        for i, action in enumerate(actions_slice):
            if (
                action.tool_name == tool_name
                and action.status == "success"
                and action.turn > min_turn
                and self._args_similar(action.key_args, new_key_args)
            ):
                matches.append((offset + i, action))

        if not matches:
            return GateResult(approved=True, reason="no-duplicates")

        # --- Layer 1: Change-Aware Bypass ---
        if self._settings.action_gating_change_aware:
            last_match_abs_idx = matches[-1][0]
            if self._has_state_change_since(ledger, last_match_abs_idx):
                logger.info(
                    "F026.1 change-aware bypass: %s allowed (state changed since action #%d)",
                    tool_name, last_match_abs_idx,
                )
                return GateResult(approved=True, reason="state-changed-since-last-run")

        # --- Layer 2 + 3: Threshold Check ---
        repeat_thresh, hard_thresh = self._effective_thresholds(tool_name, tool_input)
        count = len(matches)

        if count < repeat_thresh:
            return GateResult(approved=True, reason=f"under-threshold({count}/{repeat_thresh})")

        if count < hard_thresh:
            logger.warning(
                "F026.1 repeat warning: %s called %d/%d times (hard limit: %d)",
                tool_name, count, repeat_thresh, hard_thresh,
            )
            return GateResult(
                approved=True,
                reason=f"at-threshold-warning({count}/{hard_thresh})",
                suggestion=(
                    f"You've run {tool_name} with the same arguments {count} times "
                    f"in the last {turn_window} turns. If you're stuck, try a different approach."
                ),
            )

        # --- Layer 4: Hard Block ---
        logger.warning(
            "F026.1 doom-loop block: %s called %d times, no state changes", tool_name, count,
        )
        return GateResult(
            approved=False,
            reason=(
                f"Doom-loop protection: {tool_name} called {count} times with identical "
                f"arguments and no intervening state changes in the last {turn_window} turns."
            ),
            suggestion=(
                "You appear to be stuck in a loop. Try:\n"
                "1. Change your approach — different command, different file\n"
                "2. Read the error output carefully — what's actually failing?\n"
                "3. Ask the user for guidance if you're blocked"
            ),
        )

    def _has_state_change_since(self, ledger: ExecutionLedger, last_match_idx: int) -> bool:
        """Check if any successful write/external action occurred after last_match_idx."""
        intervening = ledger.actions[last_match_idx + 1:]
        return any(
            a.side_effect_type in ("write", "external")
            and a.status == "success"
            for a in intervening
        )

    def _is_iterative_command(self, tool_name: str, tool_input: dict) -> bool:
        """Return True if this tool call is a known-iterative pattern."""
        if tool_name == "write_file":
            return True

        if tool_name == "bash":
            # Support both "command" and "cmd" keys (matches execution_ledger)
            command = tool_input.get("command") or tool_input.get("cmd", "")
            tokens = command.strip().split()
            if not tokens:
                return False
            # Skip leading env var assignments (e.g. PYTHONPATH=. pytest)
            while tokens and _ENV_VAR_PREFIX.match(tokens[0]):
                tokens = tokens[1:]
            if not tokens:
                return False
            first = tokens[0].lower()
            # Check subcommands first — tools with subcommand entries
            # are only iterative for specific subcommands
            if first in ITERATIVE_SUBCOMMANDS:
                if len(tokens) > 1:
                    return tokens[1].lower() in ITERATIVE_SUBCOMMANDS[first]
                return False
            if first in ITERATIVE_COMMAND_TOKENS:
                return True

        return False

    def _effective_thresholds(self, tool_name: str, tool_input: dict) -> tuple[int, int]:
        """Return (repeat_threshold, hard_block_threshold) for this call."""
        base_repeat = self._settings.action_gating_repeat_threshold
        base_hard = self._settings.action_gating_hard_block_threshold

        if self._is_iterative_command(tool_name, tool_input):
            multiplier = self._settings.action_gating_iterative_multiplier
            return (int(base_repeat * multiplier), int(base_hard * multiplier))

        return (base_repeat, base_hard)

    # ------------------------------------------------------------------
    # Tier 3: full LLM gate
    # ------------------------------------------------------------------

    async def _full_gate(
        self,
        tool_name: str,
        tool_input: dict,
        ledger: ExecutionLedger,
        user_message: str,
    ) -> GateResult:
        """Run an LLM gate check with a 5-second timeout, failing open."""
        if not self._call_gate_model:
            return GateResult(approved=True, reason="no-gate-model")

        prompt = self._build_gate_prompt(tool_name, tool_input, ledger, user_message)

        try:
            response = await asyncio.wait_for(
                self._call_gate_model(prompt),  # type: ignore[arg-type]
                timeout=5.0,
            )
            return GateResult.from_json(str(response))
        except TimeoutError:
            logger.warning(
                "ActionGate: gate model call timed out for %s — failing open", tool_name
            )
            return GateResult(approved=True, reason="gate-timeout-fail-open")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "ActionGate: gate model call failed for %s (%s) — failing open",
                tool_name,
                exc,
            )
            return GateResult(approved=True, reason=f"gate-error-fail-open: {exc}")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _args_similar(
        self,
        prior_args: dict[str, str],
        new_args: dict[str, str],
    ) -> bool:
        """Return True if ALL shared keys have matching values.

        Comparison is case-insensitive with whitespace stripped.  Keys that
        represent file paths (``path``, ``file``) receive additional
        normalisation: trailing slashes are removed and leading ``./`` is
        stripped so ``./foo.py`` and ``foo.py`` are treated as identical.

        Returns False when there are no shared keys.
        """
        PATH_KEYS = {"path", "file", "file_path"}

        shared_keys = set(prior_args) & set(new_args)
        if not shared_keys:
            return False

        for key in shared_keys:
            prior_val = prior_args[key].strip().lower()
            new_val = new_args[key].strip().lower()

            if key in PATH_KEYS:
                prior_val = prior_val.rstrip("/").removeprefix("./")
                new_val = new_val.rstrip("/").removeprefix("./")

            if prior_val != new_val:
                return False

        return True

    def _build_gate_prompt(
        self,
        tool_name: str,
        tool_input: dict,
        ledger: ExecutionLedger,
        user_message: str,
    ) -> str:
        """Build the LLM prompt for a Tier 3 gate check."""
        safe_args = self._safe_args(tool_input)
        args_text = json.dumps(safe_args, ensure_ascii=False)[:500]
        user_text = user_message[:500]
        ledger_text = ledger.system_prompt_section()

        return (
            "You are a safety gate reviewing a proposed tool action.\n"
            "Answer ONLY with valid JSON: {\"approved\": true/false, \"reason\": \"...\"}.\n"
            "Do NOT add any other text.\n\n"
            f"USER REQUEST:\n{user_text}\n\n"
            f"PROPOSED ACTION:\n  tool: {tool_name}\n  args: {args_text}\n\n"
            f"EXECUTION HISTORY:\n{ledger_text}\n\n"
            "Approve if:\n"
            "  - The action clearly aligns with the user's request.\n"
            "  - It has not already been completed successfully.\n"
            "  - There are no obvious red flags (destructive, unintended, out-of-scope).\n"
            "Block if the action is a duplicate, misaligned, or potentially harmful.\n"
            "When in doubt, APPROVE (fail open)."
        )

    def _safe_args(self, tool_input: dict) -> dict:
        """Return a sanitised copy of tool_input safe to include in prompts.

        Removes the ``content`` key (may be large/sensitive) and truncates
        all remaining values to 200 characters.
        """
        result: dict[str, str] = {}
        for k, v in tool_input.items():
            if k == "content":
                continue
            result[k] = str(v)[:200]
        return result
