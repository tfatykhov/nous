"""F061 PR-1: tests for the SubtaskReport pydantic schema."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nous.heart.subtask_report import DEFAULT_CONFIDENCE, SubtaskReport


class TestSubtaskReportRoundTrip:
    """Valid payloads parse and serialize correctly."""

    def test_minimal_valid_payload(self):
        r = SubtaskReport.model_validate({
            "summary": "x",  # 1 char passes min_length=1; validator enforces 50
            "confidence": 0.5,
        })
        assert r.summary == "x"
        assert r.confidence == 0.5
        assert r.findings == []
        assert r.next_actions == []
        assert r.evidence_refs == []
        assert r.incomplete is False
        assert r.blocked_reason == ""

    def test_full_payload(self):
        payload = {
            "summary": "Done.",
            "findings": ["a", "b"],
            "next_actions": ["next"],
            "confidence": 0.9,
            "evidence_refs": ["fact-uuid-1"],
            "incomplete": False,
            "blocked_reason": "",
        }
        r = SubtaskReport.model_validate(payload)
        assert r.findings == ["a", "b"]
        assert r.next_actions == ["next"]
        assert r.evidence_refs == ["fact-uuid-1"]
        # Round-trip dump matches input plus two derived/optional fields:
        # the F062 `payload` (None by default — kept in the dump so the F062
        # contract is explicit) and `confidence_reported` (stamped True here
        # because the payload carried an explicit confidence).
        expected = {**payload, "payload": None, "confidence_reported": True}
        assert r.model_dump() == expected

    def test_incomplete_with_reason(self):
        r = SubtaskReport.model_validate({
            "summary": "blocked",
            "confidence": 0.0,
            "incomplete": True,
            "blocked_reason": "permission denied",
        })
        assert r.incomplete is True
        assert r.blocked_reason == "permission denied"

    def test_confidence_inclusive_lower_bound(self):
        """Field(ge=0.0) — exactly 0.0 must be accepted."""
        r = SubtaskReport.model_validate({"summary": "x", "confidence": 0.0})
        assert r.confidence == 0.0

    def test_confidence_inclusive_upper_bound(self):
        """Field(le=1.0) — exactly 1.0 must be accepted."""
        r = SubtaskReport.model_validate({"summary": "x", "confidence": 1.0})
        assert r.confidence == 1.0

    def test_summary_minimum_length_one(self):
        """Field(min_length=1) — single character is the absolute floor.

        The 50-char floor enforced by the structural validator
        (nous/heart/subtask_validator.py) is intentionally NOT enforced here
        so the threshold remains tunable via NOUS_SUBTASK_REPORT_MIN_SUMMARY_CHARS.
        """
        r = SubtaskReport.model_validate({"summary": "x", "confidence": 0.5})
        assert r.summary == "x"


class TestSubtaskReportValidation:
    """Invalid payloads raise ValidationError."""

    def test_missing_summary_rejected(self):
        with pytest.raises(ValidationError):
            SubtaskReport.model_validate({"confidence": 0.5})

    def test_missing_confidence_accepted_with_default(self):
        """Regression: an omitted confidence must NOT discard a complete run.

        This inverts the original F061 contract on purpose — see the module
        docstring in nous/heart/subtask_report.py.
        """
        r = SubtaskReport.model_validate({"summary": "ok"})
        assert r.confidence == DEFAULT_CONFIDENCE
        assert r.confidence_reported is False

    def test_empty_summary_rejected(self):
        # min_length=1 on the pydantic side
        with pytest.raises(ValidationError):
            SubtaskReport.model_validate({"summary": "", "confidence": 0.5})

    def test_confidence_below_zero_rejected(self):
        with pytest.raises(ValidationError):
            SubtaskReport.model_validate({"summary": "x", "confidence": -0.1})

    def test_confidence_above_one_rejected(self):
        with pytest.raises(ValidationError):
            SubtaskReport.model_validate({"summary": "x", "confidence": 1.1})

    def test_extra_field_rejected(self):
        """extra='forbid' guards against model-invented keys."""
        with pytest.raises(ValidationError):
            SubtaskReport.model_validate({
                "summary": "x",
                "confidence": 0.5,
                "confidence_level": "high",  # invented synonym
            })

    def test_wrong_findings_type_rejected(self):
        with pytest.raises(ValidationError):
            SubtaskReport.model_validate({
                "summary": "x",
                "confidence": 0.5,
                "findings": "should be a list",
            })


class TestSubtaskReportDump:
    """model_dump produces a plain dict suitable for JSONB persistence."""

    def test_dump_is_dict(self):
        r = SubtaskReport(summary="hello", confidence=0.7)
        d = r.model_dump()
        assert isinstance(d, dict)
        assert set(d.keys()) == {
            "summary", "findings", "next_actions", "confidence",
            "confidence_reported",
            "evidence_refs", "incomplete", "blocked_reason",
            # F062: schema-typed payload field — None when absent.
            "payload",
        }


class TestConfidenceReportedFlag:
    """`confidence_reported` is derived from presence, not caller-supplied."""

    def test_true_when_confidence_supplied(self):
        r = SubtaskReport.model_validate({"summary": "x", "confidence": 0.9})
        assert r.confidence == 0.9
        assert r.confidence_reported is True

    def test_true_even_for_zero_confidence(self):
        """0.0 is a real self-report, not an absence — guards `or`-style bugs."""
        r = SubtaskReport.model_validate({"summary": "x", "confidence": 0.0})
        assert r.confidence == 0.0
        assert r.confidence_reported is True

    def test_flag_cannot_be_forged(self):
        """A caller claiming confidence_reported=True without a value is corrected."""
        r = SubtaskReport.model_validate({
            "summary": "x",
            "confidence_reported": True,
        })
        assert r.confidence_reported is False
        assert r.confidence == DEFAULT_CONFIDENCE

    def test_flag_cannot_be_suppressed(self):
        r = SubtaskReport.model_validate({
            "summary": "x",
            "confidence": 0.8,
            "confidence_reported": False,
        })
        assert r.confidence_reported is True

    def test_kwargs_construction_sets_flag(self):
        assert SubtaskReport(summary="x", confidence=0.7).confidence_reported is True
        assert SubtaskReport(summary="x").confidence_reported is False

    def test_out_of_range_still_rejected_when_supplied(self):
        """Fail-open on absence must not weaken the range check on presence."""
        with pytest.raises(ValidationError):
            SubtaskReport.model_validate({"summary": "x", "confidence": 1.5})
