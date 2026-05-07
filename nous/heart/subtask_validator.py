"""F061: structural validator for SubtaskReport payloads.

NO LLM calls — purely structural. Snorkel's "self-critique paradox" research
showed LLM self-critique drops accuracy on tasks the model was already
solving. We validate via schema + length floor + placeholder regex instead.

Outcomes (also the ``final_outcome`` enum values used by the worker):

- ``completed`` — payload validated successfully (returned via ``ValidationResult.passed``).
- ``incomplete_blocked`` — agent self-reported the task as blocked
  (``incomplete=true``); not a validator failure but treated as a soft-fail
  by the DAG layer.
- ``incomplete_no_terminal`` — payload is None (loop exited without calling
  ``submit_final_report``).
- ``validation_failed`` — schema invalid, summary too short, or summary
  matched a placeholder pattern.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pydantic import ValidationError

from nous.heart.subtask_report import SubtaskReport

# Each pattern is anchored at start-of-summary and case-insensitive.
# The "I will" verb list is intentionally narrow: only verbs that signal the
# agent has NOT done the work yet. "I will recommend ..." is LEGITIMATE (the
# agent has already done the work and is reporting a recommendation), so
# "recommend" is deliberately NOT in the verb list.
#
# DO NOT add general verbs like "recommend", "advise", "suggest", "consider"
# to this list — see test_subtask_validator.py for a positive case that
# would regress.
# Conservative by design: only flags obviously-placeholder PROSE patterns.
# Short non-answers like "n/a" or "no answer" are caught by the length floor
# instead — adding them here would risk rejecting legitimate summaries like
# "No answer was found in the documentation; recommend reaching out to..."
_PLACEHOLDER_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^\s*todo[\s:]", re.IGNORECASE),
    re.compile(r"^\s*lorem\s+ipsum", re.IGNORECASE),
    re.compile(
        r"^\s*i\s+(will|am\s+going\s+to)\s+(research|investigate|analyze|look\s+into|check)\b",
        re.IGNORECASE,
    ),
    re.compile(r"^\s*let\s+me\s+(think|check|investigate|see)\b", re.IGNORECASE),
]

# Only inspect the head of the summary. Avoids false positives where a
# legitimate summary happens to mention "TODO:" deep in the prose.
_PLACEHOLDER_SCAN_CHARS = 200


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Outcome of structural validation of a submit_final_report payload.

    ``ok=True`` only for ``outcome="completed"``. ``incomplete_blocked`` and
    failures all have ``ok=False`` but are distinguished by ``outcome`` so
    the worker can map to the right ``final_outcome`` column value and the
    DAG layer can render them with a distinct UI.
    """

    ok: bool
    outcome: str
    reason: str = ""
    report: SubtaskReport | None = None

    @classmethod
    def passed(cls, report: SubtaskReport) -> "ValidationResult":
        return cls(ok=True, outcome="completed", report=report)

    @classmethod
    def failed(cls, outcome: str, reason: str) -> "ValidationResult":
        return cls(ok=False, outcome=outcome, reason=reason)

    @classmethod
    def incomplete(cls, blocked_reason: str, report: SubtaskReport) -> "ValidationResult":
        return cls(
            ok=False,
            outcome="incomplete_blocked",
            reason=blocked_reason or "no_reason_given",
            report=report,
        )


def validate_report(payload: dict | None, *, min_summary_chars: int) -> ValidationResult:
    """Validate a submit_final_report payload structurally.

    Returns a ``ValidationResult`` whose ``outcome`` field is one of the
    five-state enum values. The caller (worker) maps the outcome onto the
    ``final_outcome`` column.
    """
    if payload is None:
        return ValidationResult.failed(
            "incomplete_no_terminal",
            "Subtask exited without calling submit_final_report.",
        )

    try:
        report = SubtaskReport.model_validate(payload)
    except ValidationError as exc:
        return ValidationResult.failed("validation_failed", f"schema_invalid: {exc}")

    if report.incomplete:
        # incomplete=true is a self-reported block, not a validation failure.
        # We still construct the SubtaskReport so callers can inspect the
        # blocked_reason and (best-effort) summary.
        return ValidationResult.incomplete(report.blocked_reason, report)

    summary = report.summary.strip()
    if len(summary) < min_summary_chars:
        return ValidationResult.failed(
            "validation_failed",
            f"summary_too_short: len={len(summary)} (min {min_summary_chars})",
        )

    head = summary[:_PLACEHOLDER_SCAN_CHARS]
    if any(p.search(head) for p in _PLACEHOLDER_PATTERNS):
        return ValidationResult.failed(
            "validation_failed",
            f"placeholder_summary: {summary[:80]!r}",
        )

    return ValidationResult.passed(report)
