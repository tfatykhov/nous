"""F061 PR-2: structural validator tests.

Table-driven coverage of the ValidationResult outcomes and placeholder regex
list. Includes a critical positive-case test: a legitimate first-person
recommendation summary ("I will recommend...") MUST NOT match the
placeholder list. Adding generic verbs to the list would regress this case.
"""

from __future__ import annotations

import pytest

from nous.heart.subtask_report import SubtaskReport
from nous.heart.subtask_validator import ValidationResult, validate_report


# Convenience helper for building valid payloads.
def _payload(summary: str, **overrides) -> dict:
    base = {
        "summary": summary,
        "confidence": 0.7,
    }
    base.update(overrides)
    return base


_MIN = 50  # default min_summary_chars per spec


class TestValidationResultOk:
    """Valid payloads → outcome='completed', report populated."""

    def test_50_char_summary_passes(self):
        s = "x" * 50
        r = validate_report(_payload(s), min_summary_chars=_MIN)
        assert r.ok is True
        assert r.outcome == "completed"
        assert r.report is not None
        assert r.report.summary == s

    def test_full_report_passes(self):
        r = validate_report(
            _payload(
                "Found 3 candidates matching the criteria. Reviewed each.",
                findings=["a", "b", "c"],
                next_actions=["review A first"],
                evidence_refs=["fact-uuid"],
            ),
            min_summary_chars=_MIN,
        )
        assert r.ok is True
        assert r.report.findings == ["a", "b", "c"]

    def test_legitimate_first_person_recommendation_passes(self):
        """REGRESSION GUARD: 'I will recommend...' is NOT a placeholder.

        The placeholder regex narrowly matches verbs that signal NOT-DONE
        work ('research', 'investigate', 'analyze', 'look into', 'check').
        'recommend' is a verdict — the agent has done the work and is
        reporting a recommendation. Adding 'recommend' (or generic verbs
        like 'advise', 'suggest', 'consider') to the regex list would
        break this case.
        """
        s = (
            "I will recommend that we proceed with option A based on three "
            "considerations: cost, latency, and maintenance burden."
        )
        r = validate_report(_payload(s), min_summary_chars=_MIN)
        assert r.ok is True
        assert r.outcome == "completed"


class TestValidationResultIncompleteBlocked:
    """incomplete=true → outcome='incomplete_blocked', report still returned."""

    def test_incomplete_with_reason(self):
        r = validate_report(
            {
                "summary": "blocked",
                "confidence": 0.0,
                "incomplete": True,
                "blocked_reason": "permission denied",
            },
            min_summary_chars=_MIN,
        )
        assert r.ok is False
        assert r.outcome == "incomplete_blocked"
        assert r.reason == "permission denied"
        assert r.report is not None
        assert r.report.incomplete is True

    def test_incomplete_without_reason_uses_fallback(self):
        r = validate_report(
            {
                "summary": "x",
                "confidence": 0.0,
                "incomplete": True,
                "blocked_reason": "",
            },
            min_summary_chars=_MIN,
        )
        assert r.outcome == "incomplete_blocked"
        assert r.reason == "no_reason_given"

    def test_incomplete_short_summary_still_treats_as_blocked_not_failed(self):
        """When incomplete=true, the length floor does NOT apply.

        Otherwise an agent that genuinely cannot generate a summary (because
        the task is blocked) would loop forever on validation_failed.
        """
        r = validate_report(
            {
                "summary": "x",
                "confidence": 0.0,
                "incomplete": True,
                "blocked_reason": "external API down",
            },
            min_summary_chars=_MIN,
        )
        assert r.outcome == "incomplete_blocked"


class TestValidationResultIncompleteNoTerminal:
    """payload is None → outcome='incomplete_no_terminal'."""

    def test_none_payload(self):
        r = validate_report(None, min_summary_chars=_MIN)
        assert r.ok is False
        assert r.outcome == "incomplete_no_terminal"
        assert r.report is None
        assert "submit_final_report" in r.reason


class TestValidationResultFailed:
    """Schema / length / placeholder failures → outcome='validation_failed'."""

    def test_summary_49_chars_fails(self):
        r = validate_report(_payload("x" * 49), min_summary_chars=_MIN)
        assert r.outcome == "validation_failed"
        assert "summary_too_short" in r.reason
        assert "len=49" in r.reason

    def test_whitespace_only_summary_fails(self):
        r = validate_report(_payload("   " * 30), min_summary_chars=_MIN)
        assert r.outcome == "validation_failed"
        assert "summary_too_short" in r.reason

    def test_emoji_only_summary_fails(self):
        r = validate_report(_payload("🎉" * 5), min_summary_chars=_MIN)
        assert r.outcome == "validation_failed"

    def test_confidence_out_of_range_fails(self):
        r = validate_report({"summary": "x" * 60, "confidence": 1.5}, min_summary_chars=_MIN)
        assert r.outcome == "validation_failed"
        assert "schema_invalid" in r.reason

    def test_extra_field_fails(self):
        r = validate_report(
            {"summary": "x" * 60, "confidence": 0.5, "invented_field": "oops"},
            min_summary_chars=_MIN,
        )
        assert r.outcome == "validation_failed"
        assert "schema_invalid" in r.reason


class TestPlaceholderRegex:
    """Placeholder patterns trigger validation_failed."""

    @pytest.mark.parametrize("summary", [
        "TODO: investigate the database connection issues here please",
        "todo: investigate the database connection issues here please",
        "Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor.",
        "I will research the latest stable Postgres version and report back.",
        "I am going to investigate the issue and follow up shortly.",
        "Let me check the documentation and come back with details.",
    ])
    def test_placeholder_patterns_rejected(self, summary):
        # Each pattern above is already >= 50 chars, so the placeholder
        # check (not the length check) is the rejection cause.
        assert len(summary) >= _MIN
        r = validate_report(_payload(summary), min_summary_chars=_MIN)
        assert r.outcome == "validation_failed", f"expected rejection for: {summary!r}"
        assert "placeholder_summary" in r.reason

    def test_short_non_answer_caught_by_length_not_placeholder(self):
        """'n/a' / 'no answer' are NOT in the placeholder regex (would
        risk rejecting legitimate "No answer was found..." summaries).
        They're caught by the length floor instead.
        """
        r = validate_report(_payload("n/a"), min_summary_chars=_MIN)
        assert r.outcome == "validation_failed"
        assert "summary_too_short" in r.reason  # length floor, not placeholder

    def test_legitimate_no_answer_summary_passes(self):
        """A 50+ char summary starting with 'No answer was found' is legitimate."""
        s = (
            "No answer was found in the documentation; recommend reaching "
            "out to the API maintainer for clarification."
        )
        r = validate_report(_payload(s), min_summary_chars=_MIN)
        assert r.ok is True

    def test_placeholder_only_in_tail_passes(self):
        """Only the head (first 200 chars) is scanned for placeholders.

        A summary that starts with real content and mentions 'TODO:' deep
        in the prose should NOT be rejected.
        """
        s = ("The investigation found three concrete issues with the proposed migration. " * 4) + " TODO: verify on staging."
        assert len(s) > 200  # confirm tail-position is past the scan window
        r = validate_report(_payload(s), min_summary_chars=_MIN)
        assert r.ok is True


class TestValidationResultBoundary:
    """Exact-boundary cases."""

    def test_summary_exactly_min_chars_passes(self):
        s = "x" * _MIN
        r = validate_report(_payload(s), min_summary_chars=_MIN)
        assert r.ok is True

    def test_strip_then_check_length(self):
        """Summary length is checked AFTER strip()."""
        s = "  " + ("x" * 49) + "  "  # 49 chars after strip
        r = validate_report(_payload(s), min_summary_chars=_MIN)
        assert r.outcome == "validation_failed"
        assert "summary_too_short" in r.reason


class TestValidationResultClassConstructors:
    """ValidationResult classmethods produce expected shapes."""

    def test_passed_constructor(self):
        rep = SubtaskReport(summary="x" * 60, confidence=0.5)
        r = ValidationResult.passed(rep)
        assert r.ok is True
        assert r.outcome == "completed"
        assert r.report is rep

    def test_failed_constructor(self):
        r = ValidationResult.failed("validation_failed", "reason here")
        assert r.ok is False
        assert r.outcome == "validation_failed"
        assert r.reason == "reason here"
        assert r.report is None

    def test_incomplete_constructor(self):
        rep = SubtaskReport(summary="x", confidence=0.0, incomplete=True, blocked_reason="rb")
        r = ValidationResult.incomplete("rb", rep)
        assert r.ok is False
        assert r.outcome == "incomplete_blocked"
        assert r.reason == "rb"
        assert r.report is rep
