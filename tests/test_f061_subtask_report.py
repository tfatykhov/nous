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
        # A string is coerced (TestTolerantParsing); other non-lists still fail.
        with pytest.raises(ValidationError):
            SubtaskReport.model_validate({
                "summary": "x",
                "confidence": 0.5,
                "findings": 42,
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


class TestConfidenceProvenanceSurvivesRoundTrips:
    """`confidence_reported` is stamped from the PRESENCE of `confidence`, but
    the dump used to always carry the defaulted 0.5 — so re-validating a dump
    flipped the flag False -> True and a persisted/reloaded report could
    masquerade as a genuine self-report, polluting the calibration the flag
    exists to protect.
    """

    def test_dump_omits_confidence_when_it_was_never_reported(self):
        d = SubtaskReport(summary="x").model_dump()
        assert "confidence" not in d
        assert d["confidence_reported"] is False

    def test_dump_keeps_confidence_when_it_was_reported(self):
        d = SubtaskReport(summary="x", confidence=0.7).model_dump()
        assert d["confidence"] == 0.7
        assert d["confidence_reported"] is True

    def test_unreported_flag_survives_revalidation(self):
        original = SubtaskReport(summary="x")
        reloaded = SubtaskReport.model_validate(original.model_dump())
        assert reloaded.confidence_reported is False
        assert reloaded.confidence == DEFAULT_CONFIDENCE

    def test_reported_flag_survives_revalidation(self):
        original = SubtaskReport(summary="x", confidence=0.9)
        reloaded = SubtaskReport.model_validate(original.model_dump())
        assert reloaded.confidence_reported is True
        assert reloaded.confidence == 0.9

    def test_provenance_is_stable_across_repeated_round_trips(self):
        r = SubtaskReport(summary="x")
        for _ in range(5):
            r = SubtaskReport.model_validate(r.model_dump())
        assert r.confidence_reported is False

    def test_reported_value_at_the_default_still_counts_as_reported(self):
        """Explicitly saying 0.5 is a real self-report, not an absence."""
        r = SubtaskReport(summary="x", confidence=DEFAULT_CONFIDENCE)
        assert r.confidence_reported is True
        d = r.model_dump()
        assert d["confidence"] == DEFAULT_CONFIDENCE
        assert SubtaskReport.model_validate(d).confidence_reported is True

    def test_json_round_trip_also_preserves_provenance(self):
        import json

        original = SubtaskReport(summary="x")
        reloaded = SubtaskReport.model_validate(
            json.loads(original.model_dump_json())
        )
        assert reloaded.confidence_reported is False

    def test_spoofing_is_still_rejected_after_the_round_trip_fix(self):
        """The dump no longer carries an unreported confidence, and a caller
        hand-setting the flag without a value is still corrected."""
        r = SubtaskReport.model_validate(
            {"summary": "x", "confidence_reported": True}
        )
        assert r.confidence_reported is False


class TestPersistedReportCarriesProvenance:
    """The collector's raw payload is what the MODEL emitted, so persisting it
    verbatim stored neither the derived confidence_reported flag nor any
    correction to a forged one (subtask_executor._persist_outcome ->
    report_jsonb). Only the validated copy had the right value.
    """

    def _norm(self, result, payload):
        from nous.handlers.subtask_executor import _normalized_report_payload

        return _normalized_report_payload(result, payload)

    def _ok(self, payload):
        from nous.heart.subtask_validator import validate_report

        result = validate_report(payload, min_summary_chars=1)
        assert result.ok, result.reason
        return result

    def test_omitted_confidence_is_persisted_as_not_reported(self):
        payload = {"summary": "did the thing"}
        stored = self._norm(self._ok(payload), payload)
        assert stored["confidence_reported"] is False
        # The key is absent rather than defaulted, so the durable record says
        # "none reported" instead of asserting a 0.5 nobody claimed. Both
        # report_jsonb readers (api/tools.py:3470, cognitive/layer.py:161)
        # use a guarded .get and simply omit the value.
        assert "confidence" not in stored

    def test_readers_surface_no_confidence_when_none_was_reported(self):
        payload = {"summary": "did the thing"}
        stored = self._norm(self._ok(payload), payload)
        c = stored.get("confidence")
        assert not isinstance(c, (int, float)), (
            "a defaulted 0.5 here would be reported to the parent as if the "
            "subtask had claimed it"
        )

    def test_reported_confidence_is_persisted_as_reported(self):
        payload = {"summary": "did the thing", "confidence": 0.8}
        stored = self._norm(self._ok(payload), payload)
        assert stored["confidence_reported"] is True
        assert stored["confidence"] == 0.8

    def test_forged_flag_is_not_persisted(self):
        """A model emitting confidence_reported: true had the forged value
        stored verbatim, even though SubtaskReport already overruled it."""
        payload = {"summary": "did the thing", "confidence_reported": True}
        stored = self._norm(self._ok(payload), payload)
        assert stored["confidence_reported"] is False

    def test_forged_flag_is_scrubbed_from_a_rejected_payload(self):
        """Validation failed, so there is no model to dump -- keep the raw
        payload for debugging but do not let it smuggle the flag."""
        from nous.heart.subtask_validator import validate_report

        payload = {"summary": "", "confidence_reported": True, "junk": 1}
        result = validate_report(payload, min_summary_chars=10)
        assert not result.ok
        stored = self._norm(result, payload)
        assert "confidence_reported" not in stored
        assert stored["junk"] == 1, "debugging detail must survive"

    def test_none_payload_stays_none(self):
        from nous.heart.subtask_validator import validate_report

        result = validate_report(None, min_summary_chars=1)
        assert self._norm(result, None) is None

    def test_persisted_form_round_trips_back_to_the_same_provenance(self):
        """report_jsonb is the durable record, so reloading it must not flip
        the flag -- this is what ties the serializer fix to persistence."""
        payload = {"summary": "did the thing"}
        stored = self._norm(self._ok(payload), payload)
        assert SubtaskReport.model_validate(stored).confidence_reported is False

    def test_dump_is_lossless_for_accepted_fields(self):
        """extra="forbid" means a successful validation saw no keys outside
        the model, so dumping the model cannot drop anything."""
        payload = {
            "summary": "s", "findings": ["f"], "next_actions": ["n"],
            "confidence": 0.9, "evidence_refs": ["e"],
        }
        stored = self._norm(self._ok(payload), payload)
        for k, v in payload.items():
            assert stored[k] == v


class TestPersistOutcomeUsesTheNormalizedPayload:
    """Wiring guard: normalizing in a helper is useless if _persist_outcome
    still hands the raw collector dict to the DB. Mutating the call site alone
    must fail something.
    """

    async def _run(self, payload):
        from unittest.mock import AsyncMock, MagicMock
        from uuid import uuid4

        from nous.handlers.subtask_executor import _persist_outcome
        from nous.heart.subtask_validator import validate_report

        heart = MagicMock()
        heart.subtasks.complete = AsyncMock()
        heart.subtasks.fail = AsyncMock()
        subtask = MagicMock(id=uuid4())

        await _persist_outcome(
            heart, subtask, validate_report(payload, min_summary_chars=1),
            payload, attempts=1, tokens_in=0, tokens_out=0, tool_calls_made=0,
        )
        return heart

    @pytest.mark.asyncio
    async def test_completed_row_stores_the_derived_flag(self):
        heart = await self._run({"summary": "did the thing"})
        heart.subtasks.complete.assert_awaited_once()
        stored = heart.subtasks.complete.await_args.kwargs["report_jsonb"]
        assert stored["confidence_reported"] is False
        assert "confidence" not in stored

    @pytest.mark.asyncio
    async def test_completed_row_does_not_store_a_forged_flag(self):
        heart = await self._run(
            {"summary": "did the thing", "confidence_reported": True}
        )
        stored = heart.subtasks.complete.await_args.kwargs["report_jsonb"]
        assert stored["confidence_reported"] is False


class TestTolerantParsing:
    """Friction scan F4: models send list fields as one string and a null or
    "" confidence. Each used to fail the whole subtask as validation_failed.
    """

    def test_json_string_findings_become_that_list(self):
        r = SubtaskReport.model_validate({
            "summary": "x",
            "findings": '["a", "b"]',
        })
        assert r.findings == ["a", "b"]

    def test_plain_string_findings_become_one_item(self):
        r = SubtaskReport.model_validate({
            "summary": "x",
            "findings": "Revenue rose 4%; churn flat.",
        })
        assert r.findings == ["Revenue rose 4%; churn flat."]

    def test_other_list_fields_are_coerced_too(self):
        r = SubtaskReport.model_validate({
            "summary": "x",
            "next_actions": '["rerun", "notify"]',
            "evidence_refs": "fact-uuid-1",
        })
        assert r.next_actions == ["rerun", "notify"]
        assert r.evidence_refs == ["fact-uuid-1"]

    def test_null_or_blank_lists_become_empty(self):
        r = SubtaskReport.model_validate({
            "summary": "x",
            "findings": None,
            "next_actions": "",
            "evidence_refs": "   ",
        })
        assert r.findings == []
        assert r.next_actions == []
        assert r.evidence_refs == []

    @pytest.mark.parametrize("confidence", [None, "", "  "])
    def test_null_or_blank_confidence_counts_as_unreported(self, confidence):
        r = SubtaskReport.model_validate({
            "summary": "x",
            "confidence": confidence,
        })
        assert r.confidence == DEFAULT_CONFIDENCE
        assert r.confidence_reported is False

    @pytest.mark.parametrize("confidence", [85, "high", -0.1])
    def test_out_of_range_or_word_confidence_still_rejected(self, confidence):
        """Parity pin (passes on base): tolerance stops at absence."""
        with pytest.raises(ValidationError):
            SubtaskReport.model_validate({
                "summary": "x",
                "confidence": confidence,
            })

    @pytest.mark.parametrize("payload", [
        {"summary": "x", "confidence_level": "high"},
        {"summary": "x", "findings": [1, 2]},
        {"summary": "x", "findings": '["a", 1]'},
        {"summary": "x", "evidence_refs": {"id": "f1"}},
    ])
    def test_unknown_key_and_non_string_items_still_rejected(self, payload):
        """Parity pin (passes on base): extra="forbid" and list[str] hold."""
        with pytest.raises(ValidationError):
            SubtaskReport.model_validate(payload)

    def test_blank_confidence_stays_unreported_across_a_dump_round_trip(self):
        r = SubtaskReport.model_validate({
            "summary": "x",
            "confidence": "",
            "findings": "one",
        })
        d = r.model_dump()
        assert "confidence" not in d
        reloaded = SubtaskReport.model_validate(d)
        assert reloaded.confidence_reported is False
        assert reloaded.findings == ["one"]

    def test_validator_completes_a_report_with_string_findings(self):
        from nous.heart.subtask_validator import validate_report

        result = validate_report(
            {
                "summary": "Checked the three feeds; all current as of today.",
                "findings": "all three feeds current",
                "confidence": "",
            },
            min_summary_chars=1,
        )
        assert result.ok, result.reason
        assert result.outcome == "completed"
        assert result.report.findings == ["all three feeds current"]
        assert result.report.confidence_reported is False


class TestSubmitFinalReportSchemaConfidenceDefault:
    """Parity pin (passes on base): the served schema advertises 0.5, never
    "", as the confidence default. The F4 plan blamed a "" default; on
    89ea70c3 there is none, so this guards against one being introduced.
    """

    @pytest.mark.parametrize("payload_enabled", [False, True])
    def test_confidence_default_is_numeric(self, payload_enabled):
        from nous.api.subtask_tools import build_submit_final_report_schema

        schema = build_submit_final_report_schema(payload_enabled)
        confidence = schema["input_schema"]["properties"]["confidence"]
        assert confidence["default"] == DEFAULT_CONFIDENCE
        assert confidence["default"] != ""
        assert confidence["type"] == "number"
