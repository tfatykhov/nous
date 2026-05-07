"""F061 PR-2: tests for the rewritten _format_subtask_results.

Three rendering branches:
  - "=== Completed Subtask ===" — F061 row with valid report_jsonb (Summary,
    Findings, Confidence) OR legacy row with non-empty result text.
  - "=== Blocked Subtask ===" — final_outcome=incomplete_blocked, surfaces
    blocked_reason.
  - "=== Failed Subtask ===" — status='failed', surfaces final_outcome + error.

Empty-result rows are skipped silently (caller is responsible for
mark_delivered, tested in test_subtasks.py at the cognitive-layer integration
level).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from nous.cognitive.layer import _format_subtask_results


def _row(**overrides):
    base = dict(
        id=uuid.uuid4(),
        task="research X",
        status="completed",
        result=None,
        error=None,
        final_outcome=None,
        report_jsonb=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestCompletedRendering:
    """report_jsonb-aware completed branch."""

    def test_f061_row_renders_summary_findings_confidence(self):
        s = _row(
            status="completed",
            final_outcome="completed",
            report_jsonb={
                "summary": "Found 3 candidates matching the criteria.",
                "findings": ["a", "b", "c"],
                "next_actions": ["review A first"],
                "confidence": 0.85,
            },
        )
        out = _format_subtask_results([s])
        assert "=== Completed Subtask ===" in out
        assert "Summary: Found 3 candidates" in out
        assert "Findings:" in out
        assert "  - a" in out
        assert "Recommended next actions:" in out
        assert "  - review A first" in out
        assert "Confidence: 0.85" in out

    def test_legacy_row_falls_back_to_result(self):
        s = _row(status="completed", result="legacy text result")
        out = _format_subtask_results([s])
        assert "=== Completed Subtask ===" in out
        assert "Result: legacy text result" in out
        assert "Summary:" not in out  # legacy uses Result:, not Summary:

    def test_empty_completed_row_skipped(self):
        s = _row(status="completed", result="", report_jsonb=None)
        out = _format_subtask_results([s])
        assert out == ""

    def test_empty_report_summary_falls_back_to_legacy_result(self):
        s = _row(
            status="completed",
            result="legacy fallback text",
            report_jsonb={"summary": "", "confidence": 0.5},
        )
        out = _format_subtask_results([s])
        # Empty report.summary → uses legacy `result` instead.
        assert "Result: legacy fallback text" in out

    def test_findings_truncated_at_5(self):
        s = _row(
            status="completed",
            final_outcome="completed",
            report_jsonb={
                "summary": "x" * 60,
                "findings": [f"f{i}" for i in range(10)],
                "confidence": 0.5,
            },
        )
        out = _format_subtask_results([s])
        # First 5 rendered
        for i in range(5):
            assert f"  - f{i}" in out
        # 6th NOT rendered (truncation)
        assert "  - f5" not in out


class TestBlockedRendering:
    """final_outcome=incomplete_blocked (status='completed' but soft-fail)."""

    def test_blocked_renders_distinct_section_with_reason(self):
        s = _row(
            status="completed",
            final_outcome="incomplete_blocked",
            result="blocked",
            report_jsonb={
                "summary": "blocked",
                "incomplete": True,
                "blocked_reason": "permission denied",
                "confidence": 0.0,
            },
        )
        out = _format_subtask_results([s])
        assert "=== Blocked Subtask ===" in out
        assert "Blocked: permission denied" in out
        # Must NOT render as "Completed Subtask" — that would defeat the point
        assert "=== Completed Subtask ===" not in out

    def test_blocked_falls_back_to_no_reason_given(self):
        s = _row(
            status="completed",
            final_outcome="incomplete_blocked",
            report_jsonb={"summary": "x", "incomplete": True, "confidence": 0.0},
        )
        out = _format_subtask_results([s])
        assert "Blocked: no_reason_given" in out

    def test_blocked_with_partial_summary_renders_it(self):
        s = _row(
            status="completed",
            final_outcome="incomplete_blocked",
            report_jsonb={
                "summary": "Investigated but couldn't proceed past auth.",
                "incomplete": True,
                "blocked_reason": "auth required",
                "confidence": 0.2,
            },
        )
        out = _format_subtask_results([s])
        assert "Partial summary: Investigated but couldn't" in out


class TestFailedRendering:
    """status='failed' rendering with outcome+reason."""

    def test_failed_renders_outcome_and_error(self):
        s = _row(
            status="failed",
            final_outcome="validation_failed",
            error="summary_too_short: len=12 (min 50)",
        )
        out = _format_subtask_results([s])
        assert "=== Failed Subtask ===" in out
        assert "Outcome: validation_failed" in out
        assert "Reason: summary_too_short" in out

    def test_failed_pre_flag_row_uses_legacy_error_line(self):
        """Legacy failed row with no final_outcome → uses old 'Error: ...' line.

        Backward compat: pre-flag rows render byte-identical to pre-F061 so
        existing operator dashboards / log greps don't regress. F061 rows
        get the richer 'Outcome: ... / Reason: ...' format.
        """
        s = _row(status="failed", error="generic error")
        out = _format_subtask_results([s])
        assert "Error: generic error" in out
        assert "Outcome:" not in out


class TestEmptyAndOrdering:
    def test_empty_input(self):
        assert _format_subtask_results([]) == ""

    def test_mixed_renders_all_branches(self):
        rows = [
            _row(
                status="completed",
                final_outcome="completed",
                report_jsonb={"summary": "ok " * 20, "confidence": 0.9},
            ),
            _row(
                status="completed",
                final_outcome="incomplete_blocked",
                report_jsonb={
                    "summary": "no", "incomplete": True,
                    "blocked_reason": "rb", "confidence": 0.0,
                },
            ),
            _row(
                status="failed",
                final_outcome="errored",
                error="boom",
            ),
        ]
        out = _format_subtask_results(rows)
        assert "=== Completed Subtask ===" in out
        assert "=== Blocked Subtask ===" in out
        assert "=== Failed Subtask ===" in out
