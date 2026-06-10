"""claim_verifier.py — F026 Execution Integrity Phase D.

ClaimVerifier: detects ungrounded action claims in assistant responses.
IntentTracker: detects ghost planning (describing work without doing it).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nous.cognitive.execution_ledger import ExecutionLedger


@dataclass
class ClaimViolation:
    """A single ungrounded action claim found in the assistant response."""

    claimed_text: str
    expected_tool: str
    found_in_turn: bool
    found_in_ledger: bool


@dataclass
class VerificationResult:
    """Outcome of verifying all action claims in one response."""

    verified: bool
    violations: list[ClaimViolation] = field(default_factory=list)
    correction: str | None = None


class ClaimVerifier:
    """Verifies that action claims in assistant responses are grounded in actual tool use."""

    # (compiled_pattern, expected_tool_name)
    ACTION_CLAIM_PATTERNS: list[tuple[str, str]] = [
        (
            r"(?:I |I've |I just )(?:saved|wrote|created|generated) .+(?:file|document|report)",
            "write_file",
        ),
        (
            r"(?:I |I've |I just )(?:sent|emailed|forwarded) .+(?:email|message|report)",
            "send_email",
        ),
        (
            r"(?:I |I've |I just )(?:pushed|committed|deployed)",
            "bash",
        ),
        (
            r"(?:saved|written) to[:\s]+[/\w.-]+",
            "write_file",
        ),
        (
            r"email sent to",
            "send_email",
        ),
    ]

    def __init__(self) -> None:
        self._compiled: list[tuple[re.Pattern[str], str]] = [
            (re.compile(pattern, re.IGNORECASE | re.DOTALL), tool)
            for pattern, tool in self.ACTION_CLAIM_PATTERNS
        ]

    def verify(
        self,
        assistant_response: str,
        tool_calls_this_turn: list[str],
        ledger: ExecutionLedger,
    ) -> VerificationResult:
        """Check every action claim against this turn's tool calls and the ledger.

        Args:
            assistant_response: Full text of the assistant's reply.
            tool_calls_this_turn: Tool names dispatched in the current turn.
            ledger: Session execution ledger for historical lookup.

        Returns:
            VerificationResult with verified=True when no violations are found.
        """
        claims = self._extract_claims(assistant_response)
        if not claims:
            return VerificationResult(verified=True)

        # Build a set of tool names seen in the last 10 ledger entries.
        # Audit CL-4 (2026-06-09): only count SUCCESSFUL actions. A blocked /
        # errored / timed-out tool call must not satisfy an action claim — e.g.
        # a censored or failed `bash` should not let "I pushed the code" verify.
        # (Arg-level matching — distinguishing `git push` from `ls` — is a
        # deeper follow-up; this closes the status hole the audit flagged.)
        recent_ledger_tools: set[str] = {
            action.tool_name
            for action in ledger.actions[-10:]
            if action.status == "success"
        }
        turn_tool_set = set(tool_calls_this_turn)

        violations: list[ClaimViolation] = []
        for matched_text, expected_tool in claims:
            found_in_turn = expected_tool in turn_tool_set
            found_in_ledger = expected_tool in recent_ledger_tools
            if not found_in_turn and not found_in_ledger:
                violations.append(
                    ClaimViolation(
                        claimed_text=matched_text,
                        expected_tool=expected_tool,
                        found_in_turn=found_in_turn,
                        found_in_ledger=found_in_ledger,
                    )
                )

        if not violations:
            return VerificationResult(verified=True)

        return VerificationResult(
            verified=False,
            violations=violations,
            correction=self._build_correction(violations),
        )

    def _extract_claims(self, text: str) -> list[tuple[str, str]]:
        """Return (matched_text, expected_tool) for every claim found in text."""
        results: list[tuple[str, str]] = []
        for pattern, tool in self._compiled:
            for match in pattern.finditer(text):
                results.append((match.group(0), tool))
        return results

    def _build_correction(self, violations: list[ClaimViolation]) -> str:
        """Build a correction message describing all ungrounded claims."""
        lines = [
            "[Execution Integrity] The previous response contained ungrounded action claims:"
        ]
        for v in violations:
            lines.append(
                f'  - Claimed: "{v.claimed_text}" '
                f"(expected tool: {v.expected_tool}) — "
                "no matching tool call was recorded."
            )
        lines.append(
            "Do not assert that an action was taken unless the corresponding tool "
            "was actually called and succeeded."
        )
        return "\n".join(lines)


class IntentTracker:
    """Detects ghost planning: describing or presenting work without executing it."""

    # Regex patterns that signal the assistant is narrating work rather than doing it.
    WORK_PRODUCT_SIGNALS: list[str] = [
        r"```[\w]*\n.{200,}```",
        r"(?:here'?s|below is) (?:the|a|my) (?:draft|plan|outline|report|email|message)",
        r"(?:I'?ll|let me|going to) (?:write|create|save|send|push)",
        r"[Ss]aved? to[:\s]+[/\w.-]+",
    ]

    def __init__(self) -> None:
        self._compiled: list[re.Pattern[str]] = [
            re.compile(pattern, re.IGNORECASE | re.DOTALL)
            for pattern in self.WORK_PRODUCT_SIGNALS
        ]

    def check_ghost_planning(
        self,
        response: str,
        tool_calls_this_turn: list[str],
        ledger: ExecutionLedger,  # noqa: ARG002 — reserved for future heuristics
    ) -> bool:
        """Return True if the response looks like ghost planning.

        Ghost planning is suppressed when real tool calls occurred this turn.
        Requires >= 2 signal matches to reduce false positives on explanations.

        Args:
            response: Full assistant response text.
            tool_calls_this_turn: Tool names used this turn.
            ledger: Session ledger (reserved for future density heuristics).

        Returns:
            True if ghost planning is detected, False otherwise.
        """
        if tool_calls_this_turn:
            return False

        signal_count = sum(
            1 for pattern in self._compiled if pattern.search(response)
        )
        return signal_count >= 2

    def build_nudge(self) -> str:
        """Return the correction message injected when ghost planning is detected."""
        return (
            "[Execution Integrity] The previous response described or presented work "
            "without calling any tools. If an action needs to be taken (write a file, "
            "run a command, search the web, etc.), use the appropriate tool rather than "
            "narrating the output. Only describe results after the tool has been called."
        )
