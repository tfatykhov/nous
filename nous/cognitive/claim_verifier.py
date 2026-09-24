"""claim_verifier.py — F026 Execution Integrity Phase D.

ClaimVerifier: detects ungrounded action claims in assistant responses.
IntentTracker: detects ghost planning (describing work without doing it).
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from typing import TYPE_CHECKING

from nous.cognitive.bash_side_effect import command_invocations, git_subcommand

if TYPE_CHECKING:
    from nous.cognitive.execution_ledger import ExecutionLedger


@dataclass
class ClaimViolation:
    """A single ungrounded action claim found in the assistant response."""

    claimed_text: str
    expected_tool: str
    found_in_turn: bool
    found_in_ledger: bool
    capable_tools: tuple[str, ...] = ()  # every tool that could have grounded it


@dataclass(frozen=True)
class Evidence:
    """One successful tool call a claim may be grounded in."""

    tool_name: str
    args: Mapping[str, str] = field(default_factory=dict)
    exit_code: int | None = None   # bash: a non-zero exit is not evidence
    side_effect: str = "write"


@dataclass(frozen=True)
class ClaimCheck:
    """One extracted claim and the best evidence found for it."""

    kind: str
    text: str
    evidence: str  # "exact" | "plausible" | "none"


@dataclass
class VerificationResult:
    """Outcome of verifying all action claims in one response."""

    verified: bool
    violations: list[ClaimViolation] = field(default_factory=list)
    correction: str | None = None
    claims: list[ClaimCheck] = field(default_factory=list)


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

# The signal an argument must carry for a capable tool to count as evidence.
# Every pattern is linear: this turn's evidence is untruncated and is scanned
# on the event loop, so no unbounded class may be followed by a search.
_MAIL_PROGRAMS = frozenset({"mail", "mailx", "sendmail", "mutt", "msmtp", "ssmtp", "swaks"})
_PY_WRITES = re.compile(
    r"open\([^)]{0,300}['\"][wax]b?\+?['\"]|\.write_(?:text|bytes)\(|\.to_(?:csv|json|excel|parquet)\("
    r"|savefig\(|json\.dump\(|shutil\.(?:copy\w*|move)\(")
_PY_SENDS = re.compile(r"\bsmtplib\b|\bsendmail\b|api\.telegram\.org")
_PY_GIT = {
    "vcs_push": re.compile(
        r"\bgit\b[^|;&\n]{0,200}?\bpush\b(?![^|;&\n]{0,200}?(?:--dry-run|[\s'\"]-n\b))"),
    "vcs_commit": re.compile(r"\bgit\b[^|;&\n]{0,200}?\bcommit\b(?![^|;&\n]{0,200}?--dry-run)"),
}
_DEPLOY_WORDS = re.compile(
    r"\b(?:deploy\w*|docker|kubectl|helm|systemctl|terraform|ansible|rsync|scp|ssh|gcloud|aws|az)\b",
    re.I)


def _names(target: str, text: str) -> bool:
    """True if ``text`` names the claimed target (full path or basename)."""
    return bool(target and text) and (target in text or target.rsplit("/", 1)[-1] in text)


@lru_cache(maxsize=32)
def _invocations(command: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Cached: each claim is checked against every evidence item."""
    return tuple((prog, tuple(args)) for prog, args in command_invocations(command))


def _dry_run(sub: str, args: tuple[str, ...]) -> bool:
    """`git push -n` / `--dry-run` change nothing; for commit `-n` is --no-verify."""
    for a in args:
        if a == "--":
            break
        name = a.split("=", 1)[0]
        if len(name) >= 5 and "--dry-run".startswith(name):  # getopt abbreviation
            return True
        if sub == "push" and a.startswith("-") and not a.startswith("--") and "n" in a[1:]:
            return True
    return False


def _runs_git(command: str, sub: str) -> bool:
    """True if ``command`` runs ``git <sub>`` for real (not a dry run)."""
    for prog, args in _invocations(command):
        if prog == "git":
            split = git_subcommand(list(args))
            if split is not None and split[0] == sub and not _dry_run(sub, tuple(split[1])):
                return True
    return False


def _sends_mail(command: str) -> bool:
    """True if ``command`` runs a mail client, or curl against an SMTP server."""
    for prog, args in _invocations(command):
        if prog in _MAIL_PROGRAMS:
            return True
        if prog == "curl" and any(
                a.lower().startswith(("smtp://", "smtps://", "--mail-rcpt")) for a in args):
            return True
    return False


def evidence_level(claim: Claim, ev: Evidence) -> str:
    """How well one successful call supports one claim: exact, plausible or none."""
    kind = CLAIM_KINDS[claim.kind]
    if ev.tool_name not in kind.capable_tools:
        return "none"
    if ev.tool_name == "bash" and ev.exit_code not in (None, 0):
        return "none"
    if not ev.args:  # names-only evidence from a legacy caller
        return "plausible"
    command = ev.args.get("command") or ev.args.get("cmd") or ""
    code = ev.args.get("code", "")
    if claim.kind == "file_write":
        if ev.tool_name == "write_file":
            path = ev.args.get("path") or ev.args.get("file_path") or ""
            return "exact" if not claim.target or _names(claim.target, path) else "none"
        if ev.tool_name == "bash":
            if ev.side_effect not in ("write", "external"):
                return "none"  # a read cannot have saved anything
            if claim.target:
                return "exact" if _names(claim.target, command) else "none"
            return "plausible"
        if not _PY_WRITES.search(code):
            return "none"
        if claim.target:
            return "exact" if _names(claim.target, code) else "none"
        return "plausible"
    if claim.kind == "email":
        if ev.tool_name == "send_email":
            recipients = f"{ev.args.get('to', '')} {ev.args.get('cc', '')}".lower()
            return "exact" if not claim.target or claim.target in recipients else "none"
        if ev.tool_name == "send_file":
            return "plausible"
        if ev.tool_name == "bash":
            # a mail command that actually leaves the host: `cat mail.log` is a read
            if ev.side_effect != "external" or not _sends_mail(command):
                return "none"
            body = command
        else:
            if not _PY_SENDS.search(code):
                return "none"
            body = code
        if claim.target and claim.target not in body.lower():
            return "none"
        return "plausible"
    if claim.kind in ("vcs_push", "vcs_commit"):
        if ev.tool_name == "run_python":
            return "plausible" if _PY_GIT[claim.kind].search(code) else "none"
        if not _runs_git(command, "push" if claim.kind == "vcs_push" else "commit"):
            return "none"
        needed = ("external",) if claim.kind == "vcs_push" else ("write", "external")
        return "exact" if ev.side_effect in needed else "none"
    # deploy: a deploy or transfer tool, not any network call (`curl` of an API is not a deploy)
    if ev.tool_name == "bash" and ev.side_effect != "none" and _DEPLOY_WORDS.search(command):
        return "plausible"
    if ev.tool_name == "run_python" and _DEPLOY_WORDS.search(code):
        return "plausible"
    return "none"


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
        ledger: ExecutionLedger | None,
        *,
        turn_evidence: list[Evidence] | None = None,
    ) -> VerificationResult:
        """Check every action claim against this turn's calls and the ledger.

        A claim is grounded when any successful call of a tool capable of the
        claimed effect carries the effect's signal in its arguments (see
        ``evidence_level``); a named path or recipient must match.

        Args:
            assistant_response: Full text of the assistant's reply.
            tool_calls_this_turn: Tool names dispatched in the current turn
                (names-only evidence when ``turn_evidence`` is not given).
            ledger: Session execution ledger for historical lookup.
            turn_evidence: This turn's successful calls with their full arguments.

        Returns:
            VerificationResult with verified=True when no violations are found.
        """
        claims = self._extract_claims(assistant_response)
        if not claims:
            return VerificationResult(verified=True)

        pool = list(turn_evidence) if turn_evidence is not None else [
            Evidence(name) for name in tool_calls_this_turn]
        if ledger is not None:
            # Audit CL-4 (2026-06-09): only SUCCESSFUL actions count -- a blocked
            # or failed call must not let "I pushed the code" verify.
            recent = ledger.actions[-10:]
            current = [a for a in ledger.actions if a.turn == ledger.current_turn]
            for action in {id(a): a for a in [*current, *recent]}.values():
                if action.status == "success":
                    pool.append(Evidence(action.tool_name, action.evidence_args,
                                         action.exit_code, action.side_effect_type))
        turn_names = set(tool_calls_this_turn) | {ev.tool_name for ev in turn_evidence or ()}

        checks: list[ClaimCheck] = []
        violations: list[ClaimViolation] = []
        for claim in claims:
            levels = {evidence_level(claim, ev) for ev in pool}
            level = "exact" if "exact" in levels else "plausible" if "plausible" in levels else "none"
            checks.append(ClaimCheck(claim.kind, claim.text, level))
            if level == "none":
                kind = CLAIM_KINDS[claim.kind]
                violations.append(ClaimViolation(
                    claimed_text=claim.text,
                    expected_tool=kind.primary_tool,
                    found_in_turn=kind.primary_tool in turn_names,
                    found_in_ledger=False,
                    capable_tools=tuple(sorted(kind.capable_tools)),
                ))

        if not violations:
            return VerificationResult(verified=True, claims=checks)

        return VerificationResult(
            verified=False,
            violations=violations,
            correction=self._build_correction(violations),
            claims=checks,
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
            capable = f"; any of: {', '.join(v.capable_tools)}" if v.capable_tools else ""
            lines.append(
                f'  - Claimed: "{v.claimed_text}" '
                f"(expected tool: {v.expected_tool}{capable}) — "
                "no successful call that could have done this was recorded."
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
