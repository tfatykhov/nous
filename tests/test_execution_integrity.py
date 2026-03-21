"""Unit tests for F026 Execution Integrity.

Tests ExecutionLedger, ClaimVerifier, IntentTracker, and ActionGate.
No database required. All tests are pure unit tests.
"""

from __future__ import annotations

import asyncio
import types
from datetime import UTC, datetime

import pytest

from nous.cognitive.execution_ledger import (
    ExecutionLedger,
    ExecutedAction,
    classify_side_effect,
    READ_TOOLS,
    WRITE_TOOLS,
)
from nous.cognitive.claim_verifier import ClaimVerifier, IntentTracker
from nous.cognitive.action_gate import ActionGate, GateResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ledger(session_id: str = "test-session") -> ExecutionLedger:
    """Return a fresh ledger."""
    return ExecutionLedger(session_id=session_id)


def _make_action(
    turn: int = 1,
    tool_name: str = "read_file",
    key_args: dict | None = None,
    status: str = "success",
    side_effect_type: str = "none",
    result_summary: str = "ok",
) -> ExecutedAction:
    return ExecutedAction(
        turn=turn,
        tool_name=tool_name,
        key_args=key_args or {},
        status=status,
        timestamp=datetime.now(UTC),
        result_summary=result_summary,
        side_effect_type=side_effect_type,
    )


def _make_settings(**kwargs) -> types.SimpleNamespace:
    """Return a minimal settings namespace for ActionGate."""
    defaults = {
        "action_gating_mode": "enforce",
        "action_gating_model": "claude-haiku-4-5-20251001",
        "action_gating_enabled": True,
    }
    defaults.update(kwargs)
    return types.SimpleNamespace(**defaults)


# ===========================================================================
# ExecutionLedger Tests
# ===========================================================================


class TestExecutionLedgerRecord:
    def test_record_adds_action(self):
        ledger = _make_ledger()
        ledger.set_turn(1)
        action = ledger.record("read_file", {"path": "foo.py"}, "contents", "success")
        assert len(ledger.actions) == 1
        assert ledger.actions[0] is action
        assert action.tool_name == "read_file"
        assert action.status == "success"
        assert action.turn == 1

    def test_record_captures_result_summary_first_100_chars(self):
        ledger = _make_ledger()
        ledger.set_turn(1)
        long_result = "x" * 200
        action = ledger.record("read_file", {"path": "f.py"}, long_result, "success")
        assert len(action.result_summary) == 100

    def test_record_sets_side_effect_type(self):
        ledger = _make_ledger()
        ledger.set_turn(1)
        action = ledger.record("write_file", {"path": "out.txt"}, "ok", "success")
        assert action.side_effect_type == "write"

    def test_multiple_records_accumulate(self):
        ledger = _make_ledger()
        ledger.set_turn(1)
        ledger.record("read_file", {"path": "a.py"}, "a", "success")
        ledger.record("write_file", {"path": "b.py"}, "b", "success")
        assert len(ledger.actions) == 2


class TestSystemPromptSection:
    def test_system_prompt_section_empty(self):
        ledger = _make_ledger()
        assert ledger.system_prompt_section() == ""

    def test_system_prompt_section_single_action(self):
        ledger = _make_ledger()
        ledger.set_turn(1)
        ledger.record("read_file", {"path": "foo.py"}, "contents", "success")
        section = ledger.system_prompt_section()
        assert "[Execution Ledger]" in section
        assert "read_file" in section

    def test_system_prompt_section_shows_recent_individually(self):
        ledger = _make_ledger()
        ledger.set_turn(5)
        ledger.record("write_file", {"path": "report.txt"}, "ok", "success")
        section = ledger.system_prompt_section()
        # The action in current turn should appear as an individual line
        assert "write_file" in section
        assert "T5" in section

    def test_system_prompt_section_grouping_old_actions(self):
        """Actions older than the recent window are grouped into a summary."""
        ledger = _make_ledger()
        # Add actions at turn 1
        ledger.set_turn(1)
        ledger.record("recall_deep", {"query": "test"}, "results", "success")
        ledger.record("recall_deep", {"query": "other"}, "results", "success")
        # Move to a much later turn so turn-1 actions are "old"
        ledger.set_turn(10)
        ledger.record("write_file", {"path": "out.txt"}, "ok", "success")

        section = ledger.system_prompt_section()
        # Old actions appear in the "Prior turns" grouped summary
        assert "Prior turns" in section
        assert "recall_deep" in section
        # Recent write_file action appears individually
        assert "write_file" in section

    def test_system_prompt_section_token_budget_triggers_truncation(self):
        """With a tiny budget, section is still returned (possibly truncated)."""
        ledger = _make_ledger()
        ledger.set_turn(1)
        # Record many actions to inflate the section
        for i in range(50):
            ledger.record("recall_deep", {"query": f"query number {i}"}, "r", "success")
        # Very tight budget — must still return a string, not crash
        section = ledger.system_prompt_section(max_tokens=20)
        assert isinstance(section, str)
        assert "[Execution Ledger]" in section

    def test_system_prompt_section_includes_error_marker(self):
        ledger = _make_ledger()
        ledger.set_turn(1)
        ledger.record("bash", {"command": "rm -rf /"}, "permission denied", "error")
        section = ledger.system_prompt_section()
        assert "[ERROR]" in section

    def test_system_prompt_section_includes_write_effect_marker(self):
        ledger = _make_ledger()
        ledger.set_turn(1)
        ledger.record("write_file", {"path": "out.py"}, "ok", "success")
        section = ledger.system_prompt_section()
        assert "(write)" in section


class TestClassifySideEffect:
    def test_classify_side_effect_read_tools(self):
        for tool in READ_TOOLS:
            assert classify_side_effect(tool) == "none", f"{tool} should be 'none'"

    def test_classify_side_effect_write_tools(self):
        for tool in WRITE_TOOLS:
            result = classify_side_effect(tool)
            assert result == "write", f"{tool} should be 'write', got {result!r}"

    def test_classify_bash_read_commands(self):
        for cmd in ("ls -la", "cat file.txt", "grep pattern file.py"):
            result = classify_side_effect("bash", {"command": cmd})
            assert result == "none", f"'{cmd}' should be 'none', got {result!r}"

    def test_classify_bash_write_commands(self):
        for cmd in ("rm -rf /tmp/foo", "mv old.txt new.txt", "touch newfile"):
            result = classify_side_effect("bash", {"command": cmd})
            assert result == "write", f"'{cmd}' should be 'write', got {result!r}"

    def test_classify_bash_git_read(self):
        for cmd in ("git log --oneline", "git status", "git diff HEAD"):
            result = classify_side_effect("bash", {"command": cmd})
            assert result == "none", f"'{cmd}' should be 'none', got {result!r}"

    def test_classify_bash_git_push(self):
        result = classify_side_effect("bash", {"command": "git push origin main"})
        assert result == "external"

    def test_classify_bash_empty_command(self):
        result = classify_side_effect("bash", {"command": ""})
        assert result == "write"

    def test_classify_bash_no_input(self):
        result = classify_side_effect("bash", {})
        assert result == "write"

    def test_classify_side_effect_unknown_tool(self):
        # Unknown tools default to "write" (conservative)
        result = classify_side_effect("some_unknown_tool", {})
        assert result == "write"

    def test_classify_side_effect_module_function(self):
        """classify_side_effect as module-level function works independently."""
        assert classify_side_effect("recall_deep") == "none"
        assert classify_side_effect("write_file") == "write"
        assert classify_side_effect("bash", {"command": "git push"}) == "external"
        assert classify_side_effect("bash", {"command": "cat foo.py"}) == "none"

    def test_classify_bash_curl_is_external(self):
        result = classify_side_effect("bash", {"command": "curl https://example.com"})
        assert result == "external"

    def test_classify_bash_wget_is_external(self):
        result = classify_side_effect("bash", {"command": "wget https://example.com"})
        assert result == "external"


class TestSummarizeArgs:
    def test_summarize_args_write_file_extracts_path(self):
        ledger = _make_ledger()
        result = ledger._summarize_args("write_file", {"path": "/tmp/report.txt", "content": "lots of content"})
        assert "path" in result
        assert result["path"] == "/tmp/report.txt"
        # content should NOT appear (key_args only picks first matching key)
        assert "content" not in result

    def test_summarize_args_bash_extracts_command(self):
        ledger = _make_ledger()
        cmd = "a" * 100
        result = ledger._summarize_args("bash", {"command": cmd})
        assert "command" in result
        assert len(result["command"]) <= 80

    def test_summarize_args_truncates_long_values(self):
        ledger = _make_ledger()
        long_query = "q" * 200
        result = ledger._summarize_args("recall_deep", {"query": long_query})
        assert len(result["query"]) == 80

    def test_summarize_args_unknown_tool_uses_first_arg(self):
        ledger = _make_ledger()
        result = ledger._summarize_args("mystery_tool", {"foo": "bar"})
        assert "foo" in result
        assert result["foo"] == "bar"

    def test_summarize_args_prefer_first_matching_key(self):
        """For write_file, 'path' is preferred over 'file_path'."""
        ledger = _make_ledger()
        result = ledger._summarize_args("write_file", {"path": "p.txt", "file_path": "fp.txt"})
        assert result.get("path") == "p.txt"
        assert "file_path" not in result


class TestHasBlockedActionsThisTurn:
    def test_has_blocked_actions_this_turn_false_when_empty(self):
        ledger = _make_ledger()
        ledger.set_turn(1)
        assert not ledger.has_blocked_actions_this_turn

    def test_has_blocked_actions_this_turn(self):
        ledger = _make_ledger()
        ledger.set_turn(2)
        ledger.record("write_file", {"path": "f.txt"}, "blocked", "blocked")
        assert ledger.has_blocked_actions_this_turn

    def test_has_blocked_actions_only_current_turn(self):
        ledger = _make_ledger()
        ledger.set_turn(1)
        ledger.record("write_file", {"path": "f.txt"}, "blocked", "blocked")
        # Advance to turn 2 — prior block should not be visible
        ledger.set_turn(2)
        assert not ledger.has_blocked_actions_this_turn

    def test_has_blocked_does_not_trigger_on_error(self):
        ledger = _make_ledger()
        ledger.set_turn(1)
        ledger.record("bash", {"command": "bad"}, "error msg", "error")
        assert not ledger.has_blocked_actions_this_turn


class TestOneLineSummary:
    def test_one_line_summary_empty(self):
        ledger = _make_ledger()
        assert ledger.one_line_summary() == "no actions recorded"

    def test_one_line_summary_single_tool(self):
        ledger = _make_ledger()
        ledger.set_turn(1)
        ledger.record("recall_deep", {"query": "q"}, "results", "success")
        summary = ledger.one_line_summary()
        assert "searches" in summary
        assert "1" in summary

    def test_one_line_summary_multiple_tools(self):
        ledger = _make_ledger()
        ledger.set_turn(1)
        ledger.record("recall_deep", {"query": "q1"}, "r", "success")
        ledger.record("recall_deep", {"query": "q2"}, "r", "success")
        ledger.record("write_file", {"path": "f.txt"}, "ok", "success")
        summary = ledger.one_line_summary()
        assert "2 searches" in summary
        assert "file writes" in summary


# ===========================================================================
# ClaimVerifier Tests
# ===========================================================================


class TestClaimVerifier:
    def _ledger(self) -> ExecutionLedger:
        ledger = _make_ledger()
        ledger.set_turn(1)
        return ledger

    def test_no_claims_verified(self):
        cv = ClaimVerifier()
        ledger = self._ledger()
        result = cv.verify("Here is what I found in the data.", [], ledger)
        assert result.verified is True
        assert result.violations == []
        assert result.correction is None

    def test_claim_with_matching_tool_call(self):
        cv = ClaimVerifier()
        ledger = self._ledger()
        response = "I've saved the report to a file."
        result = cv.verify(response, ["write_file"], ledger)
        assert result.verified is True

    def test_claim_without_tool_call(self):
        """A claim with no corresponding tool call is a violation."""
        cv = ClaimVerifier()
        ledger = self._ledger()
        response = "I've saved the report file for you."
        result = cv.verify(response, [], ledger)
        assert result.verified is False
        assert len(result.violations) >= 1
        assert result.correction is not None

    def test_claim_matched_by_ledger(self):
        """Claim passes if the tool appears in recent ledger entries."""
        cv = ClaimVerifier()
        ledger = self._ledger()
        # Record write_file in the ledger
        ledger.record("write_file", {"path": "out.txt"}, "ok", "success")
        response = "I've saved the report file."
        # No tool calls this turn, but ledger has write_file
        result = cv.verify(response, [], ledger)
        assert result.verified is True

    def test_multiple_violations(self):
        cv = ClaimVerifier()
        ledger = self._ledger()
        response = (
            "I've saved the report file. "
            "I've sent the summary email."
        )
        result = cv.verify(response, [], ledger)
        assert result.verified is False
        assert len(result.violations) >= 2

    def test_build_correction_message(self):
        cv = ClaimVerifier()
        ledger = self._ledger()
        response = "I've created the document file."
        result = cv.verify(response, [], ledger)
        assert result.correction is not None
        assert "ungrounded action claims" in result.correction
        assert "write_file" in result.correction
        assert "Do not assert" in result.correction

    def test_email_claim_patterns(self):
        cv = ClaimVerifier()
        ledger = self._ledger()
        # "I've sent the email" pattern
        result = cv.verify("I've sent the email report to the team.", [], ledger)
        assert result.verified is False
        violations_tools = [v.expected_tool for v in result.violations]
        assert "send_email" in violations_tools

    def test_email_sent_to_pattern(self):
        cv = ClaimVerifier()
        ledger = self._ledger()
        result = cv.verify("email sent to alice@example.com", [], ledger)
        assert result.verified is False
        violations_tools = [v.expected_tool for v in result.violations]
        assert "send_email" in violations_tools

    def test_file_claim_patterns(self):
        """'saved to /path/file' triggers write_file claim."""
        cv = ClaimVerifier()
        ledger = self._ledger()
        result = cv.verify("The output was saved to /tmp/report.txt", [], ledger)
        assert result.verified is False
        violations_tools = [v.expected_tool for v in result.violations]
        assert "write_file" in violations_tools

    def test_git_claim_patterns(self):
        """'I pushed' triggers bash claim."""
        cv = ClaimVerifier()
        ledger = self._ledger()
        result = cv.verify("I've pushed the changes to the repository.", [], ledger)
        assert result.verified is False
        violations_tools = [v.expected_tool for v in result.violations]
        assert "bash" in violations_tools

    def test_no_false_positive_on_plans(self):
        """Future-tense plans ('I'll write...') should NOT be flagged."""
        cv = ClaimVerifier()
        ledger = self._ledger()
        # These are intentions, not claims of completed actions
        response = "I'll write the file next. Let me also create the report."
        result = cv.verify(response, [], ledger)
        # Should be verified (no past-tense action claims matched)
        assert result.verified is True

    def test_violation_records_found_in_turn_and_ledger_flags(self):
        cv = ClaimVerifier()
        ledger = self._ledger()
        response = "I've created the report file."
        result = cv.verify(response, [], ledger)
        assert not result.verified
        violation = result.violations[0]
        assert violation.found_in_turn is False
        assert violation.found_in_ledger is False


# ===========================================================================
# IntentTracker Tests
# ===========================================================================


class TestIntentTracker:
    def _ledger(self) -> ExecutionLedger:
        ledger = _make_ledger()
        ledger.set_turn(1)
        return ledger

    def test_no_ghost_planning_with_tools(self):
        """When tools were called this turn, ghost planning is suppressed."""
        tracker = IntentTracker()
        ledger = self._ledger()
        response = (
            "Here's the draft email:\n\n"
            "```\nDear team,\n" + "x" * 250 + "\n```\n\n"
            "I'll send it now. Below is the plan I've outlined."
        )
        result = tracker.check_ghost_planning(response, ["write_file"], ledger)
        assert result is False

    def test_ghost_planning_detected(self):
        """Code block + narration without tool calls triggers ghost planning."""
        tracker = IntentTracker()
        ledger = self._ledger()
        # Need >= 2 signal matches: a long code block and a "here's the draft" phrase
        long_code = "x" * 250
        response = (
            f"Here's the draft report:\n\n```python\n{long_code}\n```"
        )
        result = tracker.check_ghost_planning(response, [], ledger)
        assert result is True

    def test_single_signal_not_enough(self):
        """One signal match is insufficient — requires >= 2."""
        tracker = IntentTracker()
        ledger = self._ledger()
        # Only one signal: "I'll write..."
        response = "I'll write the file once I have the data."
        result = tracker.check_ghost_planning(response, [], ledger)
        assert result is False

    def test_two_signals_without_tools_triggers(self):
        """Two signals without tool calls should trigger ghost planning."""
        tracker = IntentTracker()
        ledger = self._ledger()
        # Signal 1: "I'll write..." pattern
        # Signal 2: "below is the plan" pattern
        response = "I'll write the report. Below is the plan I've put together."
        result = tracker.check_ghost_planning(response, [], ledger)
        assert result is True

    def test_build_nudge_format(self):
        tracker = IntentTracker()
        nudge = tracker.build_nudge()
        assert "[Execution Integrity]" in nudge
        assert "tool" in nudge.lower()
        assert "narrat" in nudge.lower()

    def test_no_ghost_planning_plain_response(self):
        """Plain factual responses with no signals are not ghost planning."""
        tracker = IntentTracker()
        ledger = self._ledger()
        response = "The capital of France is Paris."
        assert tracker.check_ghost_planning(response, [], ledger) is False


# ===========================================================================
# ActionGate Tests
# ===========================================================================


class TestGateResult:
    def test_gate_result_from_json_valid(self):
        text = '{"approved": true, "reason": "looks good"}'
        result = GateResult.from_json(text)
        assert result.approved is True
        assert result.reason == "looks good"
        assert result.suggestion is None

    def test_gate_result_from_json_with_suggestion(self):
        text = '{"approved": false, "reason": "duplicate", "suggestion": "check first result"}'
        result = GateResult.from_json(text)
        assert result.approved is False
        assert result.reason == "duplicate"
        assert result.suggestion == "check first result"

    def test_gate_result_from_invalid_json_fails_open(self):
        """Parse error must fail open (approved=True)."""
        result = GateResult.from_json("this is not json at all")
        assert result.approved is True
        assert "gate-parse-error-fail-open" in result.reason

    def test_gate_result_from_json_strips_markdown_fences(self):
        text = "```json\n{\"approved\": false, \"reason\": \"blocked\"}\n```"
        result = GateResult.from_json(text)
        assert result.approved is False
        assert result.reason == "blocked"

    def test_gate_result_from_empty_string_fails_open(self):
        result = GateResult.from_json("")
        assert result.approved is True

    def test_blocked_result_format(self):
        result = GateResult(approved=False, reason="Duplicate: already ran", suggestion="adjust args")
        assert result.approved is False
        assert "Duplicate" in result.reason
        assert result.suggestion == "adjust args"


class TestActionGate:
    def _ledger(self) -> ExecutionLedger:
        ledger = _make_ledger()
        ledger.set_turn(1)
        return ledger

    @pytest.mark.asyncio
    async def test_read_only_passes(self):
        settings = _make_settings()
        gate = ActionGate(settings)
        ledger = self._ledger()
        result = await gate.check("recall_deep", {"query": "hello"}, ledger)
        assert result.approved is True
        assert result.reason == "read-only"

    @pytest.mark.asyncio
    async def test_write_consistency_pass(self):
        """First write of a path is approved."""
        settings = _make_settings()
        gate = ActionGate(settings)
        ledger = self._ledger()
        result = await gate.check("write_file", {"path": "new_file.txt"}, ledger)
        assert result.approved is True
        assert result.reason == "consistency-pass"

    @pytest.mark.asyncio
    async def test_write_duplicate_blocked(self):
        """Same write_file(path=X) after a successful write is blocked."""
        settings = _make_settings()
        gate = ActionGate(settings)
        ledger = self._ledger()
        # Record the first write as successful
        ledger.record("write_file", {"path": "report.txt"}, "ok", "success")
        # Attempt the same write again
        result = await gate.check("write_file", {"path": "report.txt"}, ledger)
        assert result.approved is False
        assert "Duplicate" in result.reason
        assert result.suggestion is not None

    @pytest.mark.asyncio
    async def test_write_duplicate_not_blocked_on_error(self):
        """Prior failed writes do not trigger duplicate block."""
        settings = _make_settings()
        gate = ActionGate(settings)
        ledger = self._ledger()
        # Record the first write as failed
        ledger.record("write_file", {"path": "report.txt"}, "error", "error")
        result = await gate.check("write_file", {"path": "report.txt"}, ledger)
        assert result.approved is True

    @pytest.mark.asyncio
    async def test_args_similar_summarized_comparison(self):
        """Consistency check summarizes new args the same way as recorded args."""
        settings = _make_settings()
        gate = ActionGate(settings)
        ledger = self._ledger()
        # Record with path arg
        ledger.record("write_file", {"path": "output/report.txt", "content": "data"}, "ok", "success")
        # Check with same path (content differs but path is the key)
        result = await gate.check("write_file", {"path": "output/report.txt", "content": "new data"}, ledger)
        assert result.approved is False

    @pytest.mark.asyncio
    async def test_args_similar_case_insensitive(self):
        """Path comparison is case-insensitive."""
        settings = _make_settings()
        gate = ActionGate(settings)
        ledger = self._ledger()
        ledger.record("write_file", {"path": "Report.TXT"}, "ok", "success")
        result = await gate.check("write_file", {"path": "report.txt"}, ledger)
        assert result.approved is False

    @pytest.mark.asyncio
    async def test_external_with_no_gate_model_fails_open(self):
        """Tier 3 (external) with no gate model callable fails open."""
        settings = _make_settings()
        gate = ActionGate(settings, call_gate_model=None)
        ledger = self._ledger()
        # Simulate an external call — need to add something to EXTERNAL_TOOLS temporarily
        # We can instead call _full_gate directly
        result = await gate._full_gate("bash", {"command": "git push"}, ledger, "push changes")
        assert result.approved is True
        assert result.reason == "no-gate-model"

    @pytest.mark.asyncio
    async def test_full_gate_timeout_fails_open(self):
        """Gate model that takes too long causes fail-open result."""
        settings = _make_settings()

        async def slow_model(prompt: str) -> str:
            await asyncio.sleep(10)  # Far beyond the 5s timeout
            return '{"approved": false, "reason": "should not reach here"}'

        gate = ActionGate(settings, call_gate_model=slow_model)
        ledger = self._ledger()
        result = await gate._full_gate("bash", {"command": "curl https://api.evil.com"}, ledger, "")
        assert result.approved is True
        assert "timeout" in result.reason

    @pytest.mark.asyncio
    async def test_full_gate_approved_from_model(self):
        """Gate model returning approved=true passes through."""
        settings = _make_settings()

        async def approve_model(prompt: str) -> str:
            return '{"approved": true, "reason": "looks fine"}'

        gate = ActionGate(settings, call_gate_model=approve_model)
        ledger = self._ledger()
        result = await gate._full_gate("bash", {"command": "curl https://safe.com"}, ledger, "")
        assert result.approved is True
        assert result.reason == "looks fine"

    @pytest.mark.asyncio
    async def test_full_gate_blocked_from_model(self):
        """Gate model returning approved=false results in blocked GateResult."""
        settings = _make_settings()

        async def block_model(prompt: str) -> str:
            return '{"approved": false, "reason": "suspicious action", "suggestion": "check intent"}'

        gate = ActionGate(settings, call_gate_model=block_model)
        ledger = self._ledger()
        result = await gate._full_gate("bash", {"command": "curl https://target.com"}, ledger, "")
        assert result.approved is False
        assert result.reason == "suspicious action"
        assert result.suggestion == "check intent"

    @pytest.mark.asyncio
    async def test_gate_check_exception_fails_open(self):
        """Unexpected exceptions in check() always fail open."""
        settings = _make_settings()

        async def crash_model(prompt: str) -> str:
            raise RuntimeError("model service down")

        gate = ActionGate(settings, call_gate_model=crash_model)
        ledger = self._ledger()
        # Trigger a Tier 3 check via _full_gate directly
        result = await gate._full_gate("bash", {"command": "curl x"}, ledger, "")
        assert result.approved is True
        assert "fail-open" in result.reason

    @pytest.mark.asyncio
    async def test_read_multiple_read_tools(self):
        """All read tools pass Tier 1 gate without consulting ledger."""
        settings = _make_settings()
        gate = ActionGate(settings)
        ledger = self._ledger()
        for tool in READ_TOOLS:
            result = await gate.check(tool, {}, ledger)
            assert result.approved is True, f"{tool} should pass Tier 1"
            assert result.reason == "read-only"
