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


@dataclass(frozen=True)
class ClaimKind:
    primary_tool: str          # kept as ClaimViolation.expected_tool (back-compat)
    capable_tools: frozenset[str]


CLAIM_KINDS: dict[str, ClaimKind] = {
    "file_write": ClaimKind("write_file", frozenset({"write_file", "bash", "run_python"})),
    "email": ClaimKind("send_email", frozenset({"send_email", "send_file", "bash", "run_python"})),
    "vcs_push": ClaimKind("bash", frozenset({"bash", "run_python"})),
    "vcs_commit": ClaimKind("bash", frozenset({"bash", "run_python"})),
    "deploy": ClaimKind("bash", frozenset({"bash", "run_python"})),
}


@dataclass(frozen=True)
class Claim:
    kind: str
    text: str
    target: str | None = None  # a path or recipient the claim names


# First person: "I", "I've", "I have", "I just", "I already". The "I" is
# case-SENSITIVE inside patterns compiled IGNORECASE: a lowercase "i " is not a subject.
_FIRST = r"\b(?-i:I)(?:['’]ve|\s+have|\s+just|\s+already)?\s+"
_CLAIM_VERBS = (r"(?:saved|wrote|written|created|generated|exported|stored|sent|emailed"
                r"|forwarded|mailed|pushed|committed|deployed)\b")
# The second clause of a FIRST-PERSON compound ("I saved X and sent Y"); only
# accepted when a first-person CLAIM ("I" + claim verb) appears earlier in the
# same sentence -- "I checked: the DAG ran and sent the email" is narration.
_AND = r"\band\s+(?:also\s+)?"
# One sentence: a dot ends it only when followed by whitespace or the end, so
# `config.yaml` and `v1.2` stay inside; "e.g." / "i.e." / "etc." do not end it.
# The abbreviation branches take ONLY a dot followed by whitespace, so they
# never overlap `\.(?=\S)` -- overlapping branches backtrack exponentially on
# a failed match ("etc.," x20 took 4.5 s; exclusive branches: 0.03 ms).
_SPAN = (r"(?:[^.\n]|\.(?=\S)|(?<=\be\.g)\.(?=\s)|(?<=\bi\.e)\.(?=\s)"
         r"|(?<=\betc)\.(?=\s)){0,160}?")
_PATH = r"(?P<target>(?:~|\.{0,2})/[\w./-]*\w|[\w-]+\.\w{1,6})"
_ADDRESS = r"(?P<target>[\w.+-]+@[\w-]+(?:\.[\w-]+)+)"
_SUBJECT = rf"(?P<subj>{_FIRST}|{_AND})"

# (pattern, kind). Patterns that open with the subject group are claims only
# under the first-person rule in _extract_claims; the targeted file pattern
# runs first so its target wins over the untargeted one on the same span.
_CLAIM_PATTERNS: list[tuple[str, str]] = [
    (rf"{_SUBJECT}(?:saved|wrote|written|exported|stored)\b{_SPAN}\s+(?:to|at|in)\s+{_PATH}",
     "file_write"),
    (rf"{_SUBJECT}(?:saved|wrote|written|created|generated)\b{_SPAN}\b(?:file|document|report)\b",
     "file_write"),
    (rf"\b(?:saved|written)\s+to[:\s]+{_PATH}", "file_write"),
    (rf"{_SUBJECT}(?:sent|emailed|forwarded|mailed)\b{_SPAN}\b(?:e-?mail|message|report)\b",
     "email"),
    (rf"\be-?mail(?:ed)?\s+sent\s+to\b(?:\s+{_ADDRESS})?", "email"),
    (rf"{_SUBJECT}pushed\b", "vcs_push"),
    (rf"{_SUBJECT}committed\b", "vcs_commit"),
    (rf"{_SUBJECT}deployed\b", "deploy"),
]
_FIRST_RE = re.compile(_FIRST, re.IGNORECASE)
_FIRST_CLAIM_RE = re.compile(_FIRST + _CLAIM_VERBS, re.IGNORECASE)
_SENTENCE_END = re.compile(r"[.!?](?=\s|$)|\n")


class ClaimVerifier:
    """Verifies that action claims in assistant responses are grounded in actual tool use."""

    def __init__(self) -> None:
        self._compiled: list[tuple[re.Pattern[str], str]] = [
            (re.compile(pattern, re.IGNORECASE), kind) for pattern, kind in _CLAIM_PATTERNS
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
        for claim in claims:
            expected_tool = CLAIM_KINDS[claim.kind].primary_tool
            found_in_turn = expected_tool in turn_tool_set
            found_in_ledger = expected_tool in recent_ledger_tools
            if not found_in_turn and not found_in_ledger:
                violations.append(
                    ClaimViolation(
                        claimed_text=claim.text,
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

    def _extract_claims(self, text: str) -> list[Claim]:
        """Every first-person completion claim in ``text``, in document order."""
        found: list[tuple[int, int, Claim]] = []
        for pattern, kind in self._compiled:
            for match in pattern.finditer(text):
                subj = match.groupdict().get("subj")
                if subj is not None and not _FIRST_RE.match(subj):
                    # an "and <verb>" clause: a claim only after a first-person
                    # CLAIM earlier in the same sentence
                    start = max(
                        (m.end() for m in _SENTENCE_END.finditer(text, 0, match.start())),
                        default=0,
                    )
                    if not _FIRST_CLAIM_RE.search(text, start, match.start()):
                        continue
                target = match.groupdict().get("target")
                if target:
                    target = target.rstrip(".,;:)")
                    if "@" in target:
                        target = target.lower()
                start, end = match.start(), match.end()
                if any(c.kind == kind and s < end and start < e for s, e, c in found):
                    continue  # same claim already captured; the targeted pattern runs first
                found.append((start, end, Claim(kind=kind, text=match.group(0).strip(), target=target)))
        found.sort(key=lambda item: item[0])
        return [claim for _, _, claim in found]

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
