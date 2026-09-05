"""F061 PR-2: tests for submit_final_report tool — schema + collector + executor."""

from __future__ import annotations

import pytest

from nous.api.subtask_tools import (
    SUBMIT_FINAL_REPORT_SCHEMA,
    SubtaskReportCollector,
    make_submit_final_report_executor,
)
from nous.heart.subtask_report import DEFAULT_CONFIDENCE


class TestSchema:
    """Tool schema is well-formed and matches Anthropic's input_schema shape."""

    def test_required_top_level_keys(self):
        assert SUBMIT_FINAL_REPORT_SCHEMA["name"] == "submit_final_report"
        assert "description" in SUBMIT_FINAL_REPORT_SCHEMA
        assert "input_schema" in SUBMIT_FINAL_REPORT_SCHEMA

    def test_input_schema_required_fields(self):
        sch = SUBMIT_FINAL_REPORT_SCHEMA["input_schema"]
        assert sch["type"] == "object"
        assert sch["additionalProperties"] is False
        # confidence is intentionally NOT required — an omitted soft metadata
        # field must not discard a complete report.
        assert set(sch["required"]) == {"summary"}

    def test_summary_min_length_50(self):
        sch = SUBMIT_FINAL_REPORT_SCHEMA["input_schema"]
        assert sch["properties"]["summary"]["minLength"] == 50

    def test_confidence_range(self):
        c = SUBMIT_FINAL_REPORT_SCHEMA["input_schema"]["properties"]["confidence"]
        assert c["minimum"] == 0.0
        assert c["maximum"] == 1.0

    def test_confidence_has_default(self):
        """Tool schema must mirror SubtaskReport's fail-open default."""
        c = SUBMIT_FINAL_REPORT_SCHEMA["input_schema"]["properties"]["confidence"]
        assert c["default"] == DEFAULT_CONFIDENCE

    def test_optional_fields_have_defaults(self):
        props = SUBMIT_FINAL_REPORT_SCHEMA["input_schema"]["properties"]
        for k in ("findings", "next_actions", "evidence_refs"):
            assert props[k]["default"] == []
        assert props["incomplete"]["default"] is False
        assert props["blocked_reason"]["default"] == ""


class TestCollector:
    """Lock-on-first semantics + reset behavior."""

    def test_initial_state(self):
        c = SubtaskReportCollector()
        assert c.is_set() is False
        assert c.get() is None
        assert c.submission_count == 0

    def test_first_set_accepted(self):
        c = SubtaskReportCollector()
        accepted = c.set({"summary": "x", "confidence": 0.5})
        assert accepted is True
        assert c.is_set() is True
        assert c.get() == {"summary": "x", "confidence": 0.5}
        assert c.submission_count == 1

    def test_second_set_rejected_first_payload_preserved(self):
        c = SubtaskReportCollector()
        first = {"summary": "first valid", "confidence": 0.9}
        second = {"summary": "TODO bogus", "confidence": 0.0}
        assert c.set(first) is True
        assert c.set(second) is False
        assert c.get() == first  # first is locked, second is rejected
        assert c.submission_count == 2

    def test_reset_clears_state(self):
        c = SubtaskReportCollector()
        c.set({"summary": "x", "confidence": 0.5})
        c.reset()
        assert c.is_set() is False
        assert c.get() is None
        assert c.submission_count == 0
        # New first call accepted after reset
        assert c.set({"summary": "y", "confidence": 0.1}) is True


class TestExecutor:
    """Tool executor returns (text, is_error) per ToolDispatcher contract."""

    @pytest.mark.asyncio
    async def test_first_call_returns_success(self):
        c = SubtaskReportCollector()
        ex = make_submit_final_report_executor(c)
        text, is_error = await ex(summary="x", confidence=0.5)
        assert is_error is False
        assert "Report received" in text
        assert c.is_set() is True

    @pytest.mark.asyncio
    async def test_second_call_returns_error(self):
        c = SubtaskReportCollector()
        ex = make_submit_final_report_executor(c)
        await ex(summary="first", confidence=0.5)
        text, is_error = await ex(summary="second", confidence=0.1)
        assert is_error is True
        assert "ERROR" in text
        assert "already been called" in text
        # Collector still has the FIRST payload
        assert c.get()["summary"] == "first"
