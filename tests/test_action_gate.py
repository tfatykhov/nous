"""Comprehensive tests for nous/cognitive/action_gate.py.

Focuses on coverage not already in test_execution_integrity.py:
  - GateResult.from_json edge cases (empty dict, missing keys, None values)
  - ActionGate._args_similar — all path normalization branches
  - ActionGate._safe_args — content removal, value truncation
  - ActionGate._build_gate_prompt — structure and content
  - ActionGate.check with external_only mode
  - ActionGate.check with irreversible side effects
  - ActionGate._consistency_check — only last 20 actions inspected
  - ActionGate.check outer exception handling
  - ActionGate.check unknown classification fails open
"""

from __future__ import annotations

import asyncio
import types
from datetime import UTC, datetime

import pytest

from nous.cognitive.action_gate import ActionGate, GateResult
from nous.cognitive.execution_ledger import ExecutedAction, ExecutionLedger


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings(**kwargs) -> types.SimpleNamespace:
    defaults = {
        "action_gating_mode": "enforce",
        "action_gating_model": "claude-haiku-4-5-20251001",
        "action_gating_enabled": True,
        "action_gating_external_only": False,
        "action_gating_turn_window": 5,
        # F026.1 defaults
        "action_gating_change_aware": True,
        "action_gating_repeat_threshold": 3,
        "action_gating_hard_block_threshold": 5,
        "action_gating_iterative_multiplier": 2.0,
    }
    defaults.update(kwargs)
    return types.SimpleNamespace(**defaults)


def _ledger(session_id: str = "test-sess") -> ExecutionLedger:
    ledger = ExecutionLedger(session_id=session_id)
    ledger.set_turn(1)
    return ledger


def _action(
    tool_name: str = "write_file",
    key_args: dict | None = None,
    status: str = "success",
    turn: int = 1,
    side_effect_type: str = "write",
) -> ExecutedAction:
    return ExecutedAction(
        turn=turn,
        tool_name=tool_name,
        key_args=key_args or {},
        status=status,
        timestamp=datetime.now(UTC),
        result_summary="ok",
        side_effect_type=side_effect_type,
    )


# ===========================================================================
# GateResult — construction and from_json
# ===========================================================================


class TestGateResultConstruction:
    def test_approved_true(self):
        r = GateResult(approved=True, reason="ok")
        assert r.approved is True
        assert r.reason == "ok"
        assert r.suggestion is None

    def test_approved_false_with_suggestion(self):
        r = GateResult(approved=False, reason="dup", suggestion="try again")
        assert r.approved is False
        assert r.suggestion == "try again"

    def test_default_suggestion_is_none(self):
        r = GateResult(approved=True, reason="x")
        assert r.suggestion is None


class TestGateResultFromJson:
    def test_valid_approved_true(self):
        r = GateResult.from_json('{"approved": true, "reason": "all good"}')
        assert r.approved is True
        assert r.reason == "all good"

    def test_valid_approved_false(self):
        r = GateResult.from_json('{"approved": false, "reason": "blocked"}')
        assert r.approved is False
        assert r.reason == "blocked"

    def test_with_suggestion(self):
        r = GateResult.from_json('{"approved": false, "reason": "dup", "suggestion": "adjust"}')
        assert r.suggestion == "adjust"

    def test_missing_approved_defaults_to_true(self):
        r = GateResult.from_json('{"reason": "no approved key"}')
        assert r.approved is True

    def test_missing_reason_defaults_to_gate_response(self):
        r = GateResult.from_json('{"approved": true}')
        assert r.reason == "gate-response"

    def test_empty_object_defaults(self):
        r = GateResult.from_json("{}")
        assert r.approved is True
        assert r.reason == "gate-response"

    def test_invalid_json_fails_open(self):
        r = GateResult.from_json("not json")
        assert r.approved is True
        assert "gate-parse-error-fail-open" in r.reason

    def test_empty_string_fails_open(self):
        r = GateResult.from_json("")
        assert r.approved is True

    def test_null_json_fails_open(self):
        r = GateResult.from_json("null")
        # json.loads("null") returns None, which has no .get() — should fail open
        assert r.approved is True

    def test_strips_markdown_fences(self):
        text = "```json\n{\"approved\": false, \"reason\": \"bad\"}\n```"
        r = GateResult.from_json(text)
        assert r.approved is False
        assert r.reason == "bad"

    def test_strips_plain_code_fences(self):
        text = "```\n{\"approved\": true, \"reason\": \"ok\"}\n```"
        r = GateResult.from_json(text)
        assert r.approved is True

    def test_approved_value_coerced_to_bool(self):
        # approved=1 should coerce to True
        r = GateResult.from_json('{"approved": 1, "reason": "ok"}')
        assert r.approved is True

    def test_approved_zero_coerces_to_false(self):
        r = GateResult.from_json('{"approved": 0, "reason": "no"}')
        assert r.approved is False

    def test_suggestion_none_when_not_present(self):
        r = GateResult.from_json('{"approved": true, "reason": "ok"}')
        assert r.suggestion is None

    def test_extra_whitespace_around_json(self):
        r = GateResult.from_json('   {"approved": true, "reason": "padded"}   ')
        assert r.approved is True
        assert r.reason == "padded"


# ===========================================================================
# ActionGate._args_similar — path normalization
# ===========================================================================


class TestArgsSimilar:
    def _gate(self) -> ActionGate:
        return ActionGate(_make_settings())

    def test_matching_path_values(self):
        gate = self._gate()
        assert gate._args_similar({"path": "foo.txt"}, {"path": "foo.txt"}) is True

    def test_non_matching_path_values(self):
        gate = self._gate()
        assert gate._args_similar({"path": "foo.txt"}, {"path": "bar.txt"}) is False

    def test_case_insensitive(self):
        gate = self._gate()
        assert gate._args_similar({"path": "Foo.TXT"}, {"path": "foo.txt"}) is True

    def test_whitespace_stripped(self):
        gate = self._gate()
        assert gate._args_similar({"path": "  foo.txt  "}, {"path": "foo.txt"}) is True

    def test_trailing_slash_removed_from_path(self):
        gate = self._gate()
        assert gate._args_similar({"path": "dir/"}, {"path": "dir"}) is True

    def test_leading_dot_slash_stripped(self):
        gate = self._gate()
        assert gate._args_similar({"path": "./foo.py"}, {"path": "foo.py"}) is True

    def test_both_path_normalizations(self):
        gate = self._gate()
        assert gate._args_similar({"path": "./dir/"}, {"path": "dir"}) is True

    def test_file_key_normalized(self):
        gate = self._gate()
        assert gate._args_similar({"file": "./report.txt"}, {"file": "report.txt"}) is True

    def test_no_shared_keys_returns_false(self):
        gate = self._gate()
        assert gate._args_similar({"path": "foo"}, {"query": "foo"}) is False

    def test_one_side_missing_key(self):
        gate = self._gate()
        assert gate._args_similar({"path": "foo", "extra": "x"}, {"path": "foo"}) is True

    def test_non_path_key_not_normalized(self):
        # 'command' is not a PATH_KEY — ./foo is NOT the same as foo
        gate = self._gate()
        assert gate._args_similar({"command": "./foo"}, {"command": "foo"}) is False

    def test_all_shared_keys_must_match(self):
        # ALL shared keys must match — path matches but query differs → False
        gate = self._gate()
        assert gate._args_similar(
            {"path": "out.txt", "query": "a"},
            {"path": "out.txt", "query": "b"},
        ) is False

    def test_all_shared_keys_matching_returns_true(self):
        # ALL shared keys match → True
        gate = self._gate()
        assert gate._args_similar(
            {"path": "out.txt", "query": "a"},
            {"path": "out.txt", "query": "a"},
        ) is True

    def test_empty_dicts(self):
        gate = self._gate()
        assert gate._args_similar({}, {}) is False

    def test_empty_prior_nonempty_new(self):
        gate = self._gate()
        assert gate._args_similar({}, {"path": "f"}) is False

    def test_nonempty_prior_empty_new(self):
        gate = self._gate()
        assert gate._args_similar({"path": "f"}, {}) is False


# ===========================================================================
# ActionGate._safe_args
# ===========================================================================


class TestSafeArgs:
    def _gate(self) -> ActionGate:
        return ActionGate(_make_settings())

    def test_content_key_removed(self):
        gate = self._gate()
        result = gate._safe_args({"path": "f.txt", "content": "lots of private data"})
        assert "content" not in result
        assert "path" in result

    def test_values_truncated_to_200(self):
        gate = self._gate()
        long_val = "x" * 300
        result = gate._safe_args({"query": long_val})
        assert len(result["query"]) == 200

    def test_short_values_unchanged(self):
        gate = self._gate()
        result = gate._safe_args({"path": "short.txt"})
        assert result["path"] == "short.txt"

    def test_values_converted_to_str(self):
        gate = self._gate()
        result = gate._safe_args({"count": 42, "active": True})
        assert result["count"] == "42"
        assert result["active"] == "True"

    def test_empty_input(self):
        gate = self._gate()
        assert gate._safe_args({}) == {}

    def test_only_content_key_returns_empty(self):
        gate = self._gate()
        result = gate._safe_args({"content": "secret"})
        assert result == {}

    def test_multiple_keys_all_preserved_except_content(self):
        gate = self._gate()
        result = gate._safe_args({"path": "f", "mode": "w", "content": "data"})
        assert set(result.keys()) == {"path", "mode"}


# ===========================================================================
# ActionGate._build_gate_prompt
# ===========================================================================


class TestBuildGatePrompt:
    def _gate(self) -> ActionGate:
        return ActionGate(_make_settings())

    def test_prompt_contains_tool_name(self):
        gate = self._gate()
        ledger = _ledger()
        prompt = gate._build_gate_prompt("write_file", {"path": "f.txt"}, ledger, "save the file")
        assert "write_file" in prompt

    def test_prompt_contains_user_message(self):
        gate = self._gate()
        ledger = _ledger()
        prompt = gate._build_gate_prompt("bash", {}, ledger, "run the tests please")
        assert "run the tests please" in prompt

    def test_prompt_truncates_user_message(self):
        gate = self._gate()
        ledger = _ledger()
        long_msg = "u" * 600
        prompt = gate._build_gate_prompt("bash", {}, ledger, long_msg)
        # Only 500 chars of message should appear
        assert "u" * 501 not in prompt
        assert "u" * 500 in prompt

    def test_prompt_truncates_args(self):
        gate = self._gate()
        ledger = _ledger()
        long_val = "v" * 600
        prompt = gate._build_gate_prompt("bash", {"path": long_val}, ledger, "")
        # After JSON serialization, value must be <= 500 chars total
        assert "v" * 501 not in prompt

    def test_prompt_excludes_content_arg(self):
        gate = self._gate()
        ledger = _ledger()
        prompt = gate._build_gate_prompt(
            "write_file", {"path": "f.txt", "content": "private-data"}, ledger, ""
        )
        assert "private-data" not in prompt

    def test_prompt_is_a_string(self):
        gate = self._gate()
        ledger = _ledger()
        result = gate._build_gate_prompt("bash", {}, ledger, "test")
        assert isinstance(result, str)
        assert len(result) > 50

    def test_prompt_includes_safety_instructions(self):
        gate = self._gate()
        ledger = _ledger()
        prompt = gate._build_gate_prompt("bash", {}, ledger, "do something")
        assert "approved" in prompt.lower()
        assert "JSON" in prompt


# ===========================================================================
# ActionGate.check — Tier 1 (read-only)
# ===========================================================================


class TestActionGateCheckTier1:
    @pytest.mark.asyncio
    async def test_all_read_tools_approved(self):
        from nous.cognitive.execution_ledger import READ_TOOLS
        gate = ActionGate(_make_settings())
        ledger = _ledger()
        for tool in READ_TOOLS:
            result = await gate.check(tool, {}, ledger)
            assert result.approved is True
            assert result.reason == "read-only"

    @pytest.mark.asyncio
    async def test_read_tool_ignores_ledger_history(self):
        gate = ActionGate(_make_settings())
        ledger = _ledger()
        # Even if there's history, read-only always passes
        ledger.record("recall_deep", {"query": "q"}, "results", "success")
        result = await gate.check("recall_deep", {"query": "q"}, ledger)
        assert result.approved is True
        assert result.reason == "read-only"


# ===========================================================================
# ActionGate.check — Tier 2 (local write / consistency check)
# ===========================================================================


class TestActionGateCheckTier2:
    @pytest.mark.asyncio
    async def test_first_write_approved(self):
        gate = ActionGate(_make_settings())
        ledger = _ledger()
        result = await gate.check("write_file", {"path": "new.txt"}, ledger)
        assert result.approved is True
        assert result.reason == "no-duplicates"

    @pytest.mark.asyncio
    async def test_duplicate_write_allowed_under_threshold(self):
        """With F026.1, a single duplicate is under the repeat threshold (3) and allowed."""
        gate = ActionGate(_make_settings())
        ledger = _ledger()
        ledger.record("write_file", {"path": "dup.txt"}, "ok", "success")
        result = await gate.check("write_file", {"path": "dup.txt"}, ledger)
        # write_file is iterative → threshold is 6 (3 * 2.0), so 1 match is allowed
        assert result.approved is True

    @pytest.mark.asyncio
    async def test_duplicate_check_ignores_failed_writes(self):
        gate = ActionGate(_make_settings())
        ledger = _ledger()
        ledger.record("write_file", {"path": "f.txt"}, "error", "error")
        result = await gate.check("write_file", {"path": "f.txt"}, ledger)
        assert result.approved is True

    @pytest.mark.asyncio
    async def test_duplicate_check_ignores_blocked_writes(self):
        gate = ActionGate(_make_settings())
        ledger = _ledger()
        ledger.record("write_file", {"path": "f.txt"}, "gate blocked", "blocked")
        result = await gate.check("write_file", {"path": "f.txt"}, ledger)
        assert result.approved is True

    @pytest.mark.asyncio
    async def test_different_path_not_blocked(self):
        gate = ActionGate(_make_settings())
        ledger = _ledger()
        ledger.record("write_file", {"path": "a.txt"}, "ok", "success")
        result = await gate.check("write_file", {"path": "b.txt"}, ledger)
        assert result.approved is True

    @pytest.mark.asyncio
    async def test_consistency_check_window_is_20(self):
        """Only the last 20 actions are checked for duplicates."""
        gate = ActionGate(_make_settings())
        ledger = _ledger()
        # Record 21 write_file actions all at "dup.txt" — but make them non-"dup.txt"
        # Record one "dup.txt" success at action #0, then 20 different ones
        ledger.record("write_file", {"path": "dup.txt"}, "ok", "success")
        for i in range(20):
            ledger.record("write_file", {"path": f"other_{i}.txt"}, "ok", "success")
        # Now the "dup.txt" action is beyond the 20-action window
        result = await gate.check("write_file", {"path": "dup.txt"}, ledger)
        assert result.approved is True

    @pytest.mark.asyncio
    async def test_external_only_mode_skips_tier2(self):
        """In external_only mode, write operations bypass Tier 2."""
        settings = _make_settings(action_gating_external_only=True)
        gate = ActionGate(settings)
        ledger = _ledger()
        ledger.record("write_file", {"path": "dup.txt"}, "ok", "success")
        result = await gate.check("write_file", {"path": "dup.txt"}, ledger)
        assert result.approved is True
        assert result.reason == "external-only-mode"


# ===========================================================================
# ActionGate.check — Tier 3 (external / irreversible)
# ===========================================================================


class TestActionGateCheckTier3:
    @pytest.mark.asyncio
    async def test_external_bash_with_no_gate_model_fails_open(self):
        gate = ActionGate(_make_settings(), call_gate_model=None)
        ledger = _ledger()
        result = await gate.check("bash", {"command": "curl https://api.example.com"}, ledger)
        assert result.approved is True
        assert result.reason == "no-gate-model"

    @pytest.mark.asyncio
    async def test_external_bash_approved_by_model(self):
        async def approve(prompt: str) -> str:
            return '{"approved": true, "reason": "safe curl"}'

        gate = ActionGate(_make_settings(), call_gate_model=approve)
        ledger = _ledger()
        result = await gate.check("bash", {"command": "curl https://safe.api.com"}, ledger)
        assert result.approved is True
        assert result.reason == "safe curl"

    @pytest.mark.asyncio
    async def test_external_bash_blocked_by_model(self):
        async def block(prompt: str) -> str:
            return '{"approved": false, "reason": "suspicious curl", "suggestion": "review"}'

        gate = ActionGate(_make_settings(), call_gate_model=block)
        ledger = _ledger()
        result = await gate.check("bash", {"command": "curl https://evil.com"}, ledger)
        assert result.approved is False
        assert result.reason == "suspicious curl"

    @pytest.mark.asyncio
    async def test_tier3_timeout_fails_open(self):
        async def slow(prompt: str) -> str:
            await asyncio.sleep(10)
            return '{"approved": false}'

        gate = ActionGate(_make_settings(), call_gate_model=slow)
        ledger = _ledger()
        result = await gate.check("bash", {"command": "wget https://example.com"}, ledger)
        assert result.approved is True
        assert "timeout" in result.reason

    @pytest.mark.asyncio
    async def test_tier3_model_exception_fails_open(self):
        async def crash(prompt: str) -> str:
            raise ConnectionError("network down")

        gate = ActionGate(_make_settings(), call_gate_model=crash)
        ledger = _ledger()
        result = await gate.check("bash", {"command": "curl https://example.com"}, ledger)
        assert result.approved is True
        assert "fail-open" in result.reason

    @pytest.mark.asyncio
    async def test_git_push_is_tier3(self):
        """git push is classified as 'external', so goes to Tier 3."""
        calls = []

        async def record_call(prompt: str) -> str:
            calls.append(prompt)
            return '{"approved": true, "reason": "push ok"}'

        gate = ActionGate(_make_settings(), call_gate_model=record_call)
        ledger = _ledger()
        result = await gate.check("bash", {"command": "git push origin main"}, ledger)
        assert len(calls) == 1
        assert result.approved is True

    @pytest.mark.asyncio
    async def test_monkey_patched_irreversible_tool_is_tier3(self, monkeypatch):
        """A tool in IRREVERSIBLE_TOOLS goes to Tier 3."""
        import nous.cognitive.execution_ledger as mod

        monkeypatch.setattr(mod, "IRREVERSIBLE_TOOLS", {"nuke_database"})

        calls = []

        async def approve(prompt: str) -> str:
            calls.append(prompt)
            return '{"approved": true, "reason": "approved"}'

        gate = ActionGate(_make_settings(), call_gate_model=approve)
        ledger = _ledger()
        result = await gate.check("nuke_database", {"target": "all"}, ledger)
        assert len(calls) == 1
        assert result.approved is True


# ===========================================================================
# ActionGate.check — outer exception handling
# ===========================================================================


class TestActionGateCheckExceptionHandling:
    @pytest.mark.asyncio
    async def test_classify_side_effect_exception_fails_open(self, monkeypatch):
        """If classify_side_effect itself raises, check() catches it and fails open."""
        import nous.cognitive.action_gate as mod

        def boom(tool_name, tool_input):
            raise ValueError("classification failure")

        monkeypatch.setattr(mod, "classify_side_effect", boom)
        gate = ActionGate(_make_settings())
        ledger = _ledger()
        result = await gate.check("write_file", {"path": "f.txt"}, ledger)
        assert result.approved is True
        assert "gate-check-error-fail-open" in result.reason

    @pytest.mark.asyncio
    async def test_consistency_check_exception_fails_open(self, monkeypatch):
        """If _consistency_check raises, check() outer handler fails open."""
        gate = ActionGate(_make_settings())
        ledger = _ledger()

        def boom(*a, **kw):
            raise RuntimeError("internal error")

        monkeypatch.setattr(gate, "_consistency_check", boom)
        result = await gate.check("write_file", {"path": "f.txt"}, ledger)
        assert result.approved is True
        assert "gate-check-error-fail-open" in result.reason


# ===========================================================================
# ActionGate.check — unknown side effect
# ===========================================================================


class TestActionGateCheckUnknownSideEffect:
    @pytest.mark.asyncio
    async def test_unknown_side_effect_fails_open(self, monkeypatch):
        """If classify_side_effect returns an unknown value, gate fails open."""
        import nous.cognitive.action_gate as mod

        monkeypatch.setattr(mod, "classify_side_effect", lambda *a, **kw: "completely_unknown")
        gate = ActionGate(_make_settings())
        ledger = _ledger()
        result = await gate.check("some_tool", {}, ledger)
        assert result.approved is True
        assert "unknown-side-effect" in result.reason


# ===========================================================================
# ActionGate._full_gate — direct tests
# ===========================================================================


class TestActionGateFullGateDirect:
    @pytest.mark.asyncio
    async def test_no_gate_model_returns_no_gate_model_reason(self):
        gate = ActionGate(_make_settings(), call_gate_model=None)
        ledger = _ledger()
        result = await gate._full_gate("bash", {"command": "x"}, ledger, "do it")
        assert result.approved is True
        assert result.reason == "no-gate-model"

    @pytest.mark.asyncio
    async def test_gate_model_receives_prompt(self):
        received = []

        async def capture(prompt: str) -> str:
            received.append(prompt)
            return '{"approved": true, "reason": "ok"}'

        gate = ActionGate(_make_settings(), call_gate_model=capture)
        ledger = _ledger()
        ledger.record("recall_deep", {"query": "prev"}, "r", "success")
        await gate._full_gate("bash", {"command": "ls"}, ledger, "list files")
        assert len(received) == 1
        assert "bash" in received[0]
        assert "list files" in received[0]

    @pytest.mark.asyncio
    async def test_gate_model_bad_json_fails_open(self):
        async def bad_response(prompt: str) -> str:
            return "I think you should proceed."

        gate = ActionGate(_make_settings(), call_gate_model=bad_response)
        ledger = _ledger()
        result = await gate._full_gate("bash", {}, ledger, "")
        assert result.approved is True

    @pytest.mark.asyncio
    async def test_gate_model_with_suggestion_propagated(self):
        async def with_suggestion(prompt: str) -> str:
            return '{"approved": false, "reason": "bad action", "suggestion": "use read instead"}'

        gate = ActionGate(_make_settings(), call_gate_model=with_suggestion)
        ledger = _ledger()
        result = await gate._full_gate("write_file", {"path": "f"}, ledger, "")
        assert result.approved is False
        assert result.suggestion == "use read instead"


# ===========================================================================
# ActionGate._consistency_check — duplicate report content
# ===========================================================================


class TestConsistencyCheckDuplicateReport:
    def test_doom_loop_reason_mentions_tool(self):
        """When hard block triggers, reason mentions the tool name."""
        # write_file is iterative → hard threshold = 10
        # Use same turn so all actions are within window
        gate = ActionGate(_make_settings())
        ledger = _ledger()
        ledger.set_turn(1)
        for _ in range(10):
            ledger.record("write_file", {"path": "out.txt"}, "ok", "success")

        result = gate._consistency_check("write_file", {"path": "out.txt"}, ledger)
        assert result.approved is False
        assert "write_file" in result.reason
        assert "Doom-loop" in result.reason

    def test_no_duplicate_empty_ledger(self):
        gate = ActionGate(_make_settings())
        ledger = _ledger()
        result = gate._consistency_check("learn_fact", {"subject": "new"}, ledger)
        assert result.approved is True

    def test_single_duplicate_with_path_normalization_allowed(self):
        """Single duplicate is under threshold — allowed with F026.1."""
        gate = ActionGate(_make_settings())
        ledger = _ledger()
        ledger.record("write_file", {"path": "./output/report.txt/"}, "ok", "success")
        result = gate._consistency_check(
            "write_file", {"path": "output/report.txt"}, ledger
        )
        # write_file is iterative → threshold = 6, only 1 match → allowed
        assert result.approved is True


# ===========================================================================
# Issue #285: Multi-target scenarios (acceptance criteria)
# ===========================================================================


class TestMultiTargetNotBlocked:
    """Disabling two different heartbeat checks in the same turn should not be blocked."""

    @pytest.mark.asyncio
    async def test_different_heartbeat_checks_not_blocked(self):
        gate = ActionGate(_make_settings())
        ledger = _ledger()
        ledger.set_turn(1)
        ledger.record(
            "heartbeat_check_manage",
            {"action": "disable", "name": "ci-schema-sync-monitor"},
            "ok",
            "success",
        )
        result = await gate.check(
            "heartbeat_check_manage",
            {"action": "disable", "name": "pr-fixes-monitor"},
            ledger,
        )
        assert result.approved is True

    @pytest.mark.asyncio
    async def test_same_heartbeat_check_blocked_at_threshold(self):
        """Same heartbeat check repeated at hard_block_threshold times is blocked."""
        gate = ActionGate(_make_settings())
        ledger = _ledger()
        # Use same turn so all actions are within window
        ledger.set_turn(1)
        for _ in range(5):
            ledger.record(
                "heartbeat_check_manage",
                {"action": "disable", "name": "ci-schema-sync-monitor"},
                "ok",
                "success",
            )
        result = await gate.check(
            "heartbeat_check_manage",
            {"action": "disable", "name": "ci-schema-sync-monitor"},
            ledger,
        )
        assert result.approved is False
        assert "Doom-loop" in result.reason

    @pytest.mark.asyncio
    async def test_different_bash_targets_not_blocked(self):
        gate = ActionGate(_make_settings())
        ledger = _ledger()
        ledger.set_turn(1)
        ledger.record(
            "bash",
            {"command": "psql -c 'UPDATE dynamic_checks SET enabled=false WHERE name=$$ci-schema$$'"},
            "ok",
            "success",
        )
        result = await gate.check(
            "bash",
            {"command": "psql -c 'UPDATE dynamic_checks SET enabled=false WHERE name=$$pr-fixes$$'"},
            ledger,
        )
        assert result.approved is True

    @pytest.mark.asyncio
    async def test_exact_duplicate_bash_allowed_under_threshold(self):
        """Single duplicate bash is under threshold with F026.1."""
        gate = ActionGate(_make_settings())
        ledger = _ledger()
        ledger.set_turn(1)
        cmd = "psql -c 'UPDATE dynamic_checks SET enabled=false WHERE name=$$check-a$$'"
        ledger.record("bash", {"command": cmd}, "ok", "success")
        result = await gate.check("bash", {"command": cmd}, ledger)
        # psql is not iterative → threshold = 3, only 1 match → allowed
        assert result.approved is True


# ===========================================================================
# Issue #285: Turn-distance window
# ===========================================================================


class TestTurnWindow:
    @pytest.mark.asyncio
    async def test_old_action_beyond_window_not_blocked(self):
        """Action beyond turn window should not be blocked."""
        gate = ActionGate(_make_settings(action_gating_turn_window=5))
        ledger = _ledger()
        ledger.set_turn(1)
        ledger.record("write_file", {"path": "f.txt"}, "ok", "success")
        ledger.set_turn(7)  # 6 turns later, beyond window of 5
        result = await gate.check("write_file", {"path": "f.txt"}, ledger)
        assert result.approved is True

    @pytest.mark.asyncio
    async def test_recent_action_within_window_allowed_under_threshold(self):
        """Single duplicate within window is allowed (under threshold with F026.1)."""
        gate = ActionGate(_make_settings(action_gating_turn_window=5))
        ledger = _ledger()
        ledger.set_turn(3)
        ledger.record("write_file", {"path": "f.txt"}, "ok", "success")
        ledger.set_turn(7)  # 4 turns later, within window of 5
        result = await gate.check("write_file", {"path": "f.txt"}, ledger)
        # write_file is iterative → threshold = 6, only 1 match → allowed
        assert result.approved is True

    @pytest.mark.asyncio
    async def test_turn_window_boundary_exact(self):
        """Action exactly at window boundary (turn > min_turn) should be blocked."""
        gate = ActionGate(_make_settings(action_gating_turn_window=5))
        ledger = _ledger()
        ledger.set_turn(3)
        ledger.record("write_file", {"path": "f.txt"}, "ok", "success")
        ledger.set_turn(8)  # 5 turns later, min_turn=3, turn 3 is NOT > 3
        result = await gate.check("write_file", {"path": "f.txt"}, ledger)
        assert result.approved is True  # Boundary: exactly at window edge passes

    @pytest.mark.asyncio
    async def test_turn_window_same_turn_allowed_under_threshold(self):
        """Single duplicate on same turn is allowed (under threshold with F026.1)."""
        gate = ActionGate(_make_settings(action_gating_turn_window=5))
        ledger = _ledger()
        ledger.set_turn(1)
        ledger.record("learn_fact", {"subject": "sky", "content": "blue"}, "ok", "success")
        result = await gate.check(
            "learn_fact", {"subject": "sky", "content": "blue"}, ledger
        )
        # learn_fact is not iterative → threshold = 3, only 1 match → allowed
        assert result.approved is True

    @pytest.mark.asyncio
    async def test_turn_window_configurable(self):
        """A window of 1 only blocks actions from the current or previous turn."""
        gate = ActionGate(_make_settings(action_gating_turn_window=1))
        ledger = _ledger()
        ledger.set_turn(1)
        ledger.record("write_file", {"path": "f.txt"}, "ok", "success")
        ledger.set_turn(3)  # 2 turns later, beyond window of 1
        result = await gate.check("write_file", {"path": "f.txt"}, ledger)
        assert result.approved is True


# ===========================================================================
# F026.1: _has_state_change_since
# ===========================================================================


class TestHasStateChangeSince:
    def _gate(self) -> ActionGate:
        return ActionGate(_make_settings())

    def test_intervening_write_returns_true(self):
        gate = self._gate()
        ledger = _ledger()
        # action 0: bash build (the match)
        ledger.record("bash", {"command": "make build"}, "ok", "success")
        # action 1: write_file (intervening state change)
        ledger.record("write_file", {"path": "foo.c"}, "ok", "success")
        assert gate._has_state_change_since(ledger, 0) is True

    def test_no_intervening_write_returns_false(self):
        gate = self._gate()
        ledger = _ledger()
        # action 0: bash build
        ledger.record("bash", {"command": "make build"}, "ok", "success")
        # action 1: read_file (not a state change)
        ledger.record("read_file", {"path": "foo.c"}, "contents", "success")
        assert gate._has_state_change_since(ledger, 0) is False

    def test_failed_write_returns_false(self):
        gate = self._gate()
        ledger = _ledger()
        ledger.record("bash", {"command": "make build"}, "ok", "success")
        # Failed write — state didn't actually change
        ledger.record("write_file", {"path": "foo.c"}, "error", "error")
        assert gate._has_state_change_since(ledger, 0) is False

    def test_intervening_external_returns_true(self):
        gate = self._gate()
        ledger = _ledger()
        ledger.record("bash", {"command": "make build"}, "ok", "success")
        ledger.record("bash", {"command": "curl -X POST http://api.example.com"}, "ok", "success")
        assert gate._has_state_change_since(ledger, 0) is True

    def test_no_actions_after_returns_false(self):
        gate = self._gate()
        ledger = _ledger()
        ledger.record("bash", {"command": "make build"}, "ok", "success")
        assert gate._has_state_change_since(ledger, 0) is False


# ===========================================================================
# F026.1: _is_iterative_command
# ===========================================================================


class TestIsIterativeCommand:
    def _gate(self) -> ActionGate:
        return ActionGate(_make_settings())

    def test_make_is_iterative(self):
        gate = self._gate()
        assert gate._is_iterative_command("bash", {"command": "make build"}) is True

    def test_pytest_is_iterative(self):
        gate = self._gate()
        assert gate._is_iterative_command("bash", {"command": "pytest tests/"}) is True

    def test_cargo_build_is_iterative(self):
        gate = self._gate()
        assert gate._is_iterative_command("bash", {"command": "cargo build"}) is True

    def test_cargo_unknown_subcommand_not_iterative(self):
        gate = self._gate()
        assert gate._is_iterative_command("bash", {"command": "cargo install foo"}) is False

    def test_docker_build_is_iterative(self):
        gate = self._gate()
        assert gate._is_iterative_command("bash", {"command": "docker build ."}) is True

    def test_docker_unknown_not_iterative(self):
        gate = self._gate()
        assert gate._is_iterative_command("bash", {"command": "docker rm container"}) is False

    def test_write_file_is_iterative(self):
        gate = self._gate()
        assert gate._is_iterative_command("write_file", {"path": "foo.py"}) is True

    def test_rm_rf_not_iterative(self):
        gate = self._gate()
        assert gate._is_iterative_command("bash", {"command": "rm -rf /tmp"}) is False

    def test_learn_fact_not_iterative(self):
        gate = self._gate()
        assert gate._is_iterative_command("learn_fact", {"subject": "test"}) is False

    def test_empty_bash_command_not_iterative(self):
        gate = self._gate()
        assert gate._is_iterative_command("bash", {"command": ""}) is False

    def test_bash_no_command_key_not_iterative(self):
        gate = self._gate()
        assert gate._is_iterative_command("bash", {}) is False

    def test_gcc_is_iterative(self):
        gate = self._gate()
        assert gate._is_iterative_command("bash", {"command": "gcc -o main main.c"}) is True

    def test_npm_run_is_iterative(self):
        gate = self._gate()
        assert gate._is_iterative_command("bash", {"command": "npm run build"}) is True

    def test_npm_install_not_iterative(self):
        gate = self._gate()
        assert gate._is_iterative_command("bash", {"command": "npm install"}) is False

    def test_ruff_is_iterative(self):
        gate = self._gate()
        assert gate._is_iterative_command("bash", {"command": "ruff check ."}) is True

    def test_go_test_is_iterative(self):
        gate = self._gate()
        assert gate._is_iterative_command("bash", {"command": "go test ./..."}) is True

    def test_go_mod_not_iterative(self):
        gate = self._gate()
        assert gate._is_iterative_command("bash", {"command": "go mod tidy"}) is False

    def test_cmd_key_is_iterative(self):
        """bash tool_input with 'cmd' key (alternative to 'command') is recognised."""
        gate = self._gate()
        assert gate._is_iterative_command("bash", {"cmd": "pytest tests/"}) is True

    def test_cmd_key_non_iterative(self):
        """bash tool_input with 'cmd' key for non-iterative command."""
        gate = self._gate()
        assert gate._is_iterative_command("bash", {"cmd": "rm -rf /tmp"}) is False

    def test_env_prefix_pytest_is_iterative(self):
        """Env var prefix like PYTHONPATH=. before pytest is still iterative."""
        gate = self._gate()
        assert gate._is_iterative_command("bash", {"command": "PYTHONPATH=. pytest tests/"}) is True

    def test_multiple_env_prefixes_iterative(self):
        """Multiple env vars before command still detected as iterative."""
        gate = self._gate()
        assert gate._is_iterative_command("bash", {"command": "CC=gcc CFLAGS=-O2 make build"}) is True

    def test_env_prefix_non_iterative(self):
        """Env var prefix before non-iterative command stays non-iterative."""
        gate = self._gate()
        assert gate._is_iterative_command("bash", {"command": "FOO=bar rm -rf /tmp"}) is False

    def test_only_env_vars_not_iterative(self):
        """Command that is only env var assignments (no actual command) is not iterative."""
        gate = self._gate()
        assert gate._is_iterative_command("bash", {"command": "FOO=bar"}) is False

    def test_cmd_key_with_env_prefix(self):
        """Combinatorial: cmd key + env prefix both handled together."""
        gate = self._gate()
        assert gate._is_iterative_command("bash", {"cmd": "PYTHONPATH=. pytest tests/"}) is True

    def test_equals_in_non_env_token_not_stripped(self):
        """Tokens with = that aren't POSIX env vars (e.g. flags) are not stripped."""
        gate = self._gate()
        # awk -F= is not an env var assignment — first token 'awk' should be checked
        assert gate._is_iterative_command("bash", {"command": "awk -F= '{print}'"}) is False


# ===========================================================================
# F026.1: _effective_thresholds
# ===========================================================================


class TestEffectiveThresholds:
    def _gate(self, **kwargs) -> ActionGate:
        return ActionGate(_make_settings(**kwargs))

    def test_base_thresholds_for_non_iterative(self):
        gate = self._gate()
        repeat, hard = gate._effective_thresholds("learn_fact", {"subject": "test"})
        assert repeat == 3
        assert hard == 5

    def test_elevated_thresholds_for_iterative(self):
        gate = self._gate()
        repeat, hard = gate._effective_thresholds("bash", {"command": "make build"})
        assert repeat == 6  # 3 * 2.0
        assert hard == 10  # 5 * 2.0

    def test_write_file_gets_iterative_thresholds(self):
        gate = self._gate()
        repeat, hard = gate._effective_thresholds("write_file", {"path": "foo.py"})
        assert repeat == 6
        assert hard == 10

    def test_custom_settings_honored(self):
        gate = self._gate(
            action_gating_repeat_threshold=2,
            action_gating_hard_block_threshold=4,
            action_gating_iterative_multiplier=3.0,
        )
        repeat, hard = gate._effective_thresholds("bash", {"command": "pytest tests/"})
        assert repeat == 6  # 2 * 3.0
        assert hard == 12  # 4 * 3.0

    def test_custom_non_iterative_thresholds(self):
        gate = self._gate(
            action_gating_repeat_threshold=2,
            action_gating_hard_block_threshold=4,
        )
        repeat, hard = gate._effective_thresholds("learn_fact", {"subject": "x"})
        assert repeat == 2
        assert hard == 4


# ===========================================================================
# F026.1: Full _consistency_check integration tests
# ===========================================================================


class TestConsistencyCheckF0261:
    """Integration tests for the 4-layer consistency check."""

    def test_debug_loop_build_edit_build_allowed(self):
        """Layer 1: build → edit → build should be allowed (state changed)."""
        gate = ActionGate(_make_settings())
        ledger = _ledger()
        ledger.set_turn(1)
        ledger.record("bash", {"command": "make build"}, "error: foo.c", "success")
        ledger.set_turn(2)
        ledger.record("write_file", {"path": "foo.c"}, "ok", "success")
        ledger.set_turn(3)

        result = gate._consistency_check("bash", {"command": "make build"}, ledger)
        assert result.approved is True
        assert result.reason == "state-changed-since-last-run"

    def test_doom_loop_blocked(self):
        """Layer 4: 5x same command with no writes → BLOCKED."""
        gate = ActionGate(_make_settings())
        ledger = _ledger()
        # Use same turn so all actions are within window
        ledger.set_turn(1)
        for _ in range(5):
            ledger.record("bash", {"command": "echo hello"}, "hello", "success")

        result = gate._consistency_check("bash", {"command": "echo hello"}, ledger)
        assert result.approved is False
        assert "Doom-loop" in result.reason
        assert result.suggestion is not None
        assert "stuck in a loop" in result.suggestion

    def test_warn_at_threshold(self):
        """Layer 2: 3rd identical call → allowed with warning (base threshold = 3)."""
        gate = ActionGate(_make_settings())
        ledger = _ledger()
        # Use same turn so all actions are within window
        ledger.set_turn(1)
        for _ in range(3):
            ledger.record("learn_fact", {"subject": "sky"}, "ok", "success")

        result = gate._consistency_check("learn_fact", {"subject": "sky"}, ledger)
        assert result.approved is True
        assert "at-threshold-warning" in result.reason
        assert result.suggestion is not None
        assert "learn_fact" in result.suggestion

    def test_under_threshold_no_warning(self):
        """Layer 2: 2nd identical call → allowed silently (under threshold 3)."""
        gate = ActionGate(_make_settings())
        ledger = _ledger()
        ledger.set_turn(1)
        for _ in range(2):
            ledger.record("learn_fact", {"subject": "sky"}, "ok", "success")

        result = gate._consistency_check("learn_fact", {"subject": "sky"}, ledger)
        assert result.approved is True
        assert "under-threshold" in result.reason
        assert result.suggestion is None

    def test_iterative_command_gets_higher_threshold(self):
        """Layer 3: make build gets 2x threshold (6 allowed, blocked at 10)."""
        gate = ActionGate(_make_settings())
        ledger = _ledger()
        # Use same turn so all actions are within window
        ledger.set_turn(1)
        for _ in range(6):
            ledger.record("bash", {"command": "make build"}, "error", "success")

        result = gate._consistency_check("bash", {"command": "make build"}, ledger)
        # 6 matches, iterative threshold = 6 → at-threshold-warning (allowed with warning)
        assert result.approved is True
        assert "at-threshold-warning" in result.reason

    def test_iterative_command_blocked_at_hard_limit(self):
        """Layer 4: iterative command blocked at elevated hard limit (10)."""
        gate = ActionGate(_make_settings())
        ledger = _ledger()
        # Use same turn so all actions are within window
        ledger.set_turn(1)
        for _ in range(10):
            ledger.record("bash", {"command": "make build"}, "error", "success")

        result = gate._consistency_check("bash", {"command": "make build"}, ledger)
        assert result.approved is False
        assert "Doom-loop" in result.reason

    def test_write_file_same_path_iterative_threshold(self):
        """write_file is iterative → gets elevated threshold."""
        gate = ActionGate(_make_settings())
        ledger = _ledger()
        # Use same turn so all actions are within window
        ledger.set_turn(1)
        for _ in range(5):
            ledger.record("write_file", {"path": "app.py"}, "ok", "success")

        result = gate._consistency_check("write_file", {"path": "app.py"}, ledger)
        # 5 matches, iterative threshold = 6 → under threshold → allowed
        assert result.approved is True
        assert "under-threshold" in result.reason

    def test_change_aware_false_disables_layer1(self):
        """When change_aware=False, Layer 1 is skipped entirely."""
        gate = ActionGate(_make_settings(action_gating_change_aware=False))
        ledger = _ledger()
        ledger.set_turn(1)
        ledger.record("bash", {"command": "echo hello"}, "hello", "success")
        # Intervening write — would bypass with Layer 1 enabled
        ledger.set_turn(2)
        ledger.record("write_file", {"path": "foo.txt"}, "ok", "success")
        ledger.set_turn(3)

        result = gate._consistency_check("bash", {"command": "echo hello"}, ledger)
        # Layer 1 disabled, but only 1 match → under threshold → allowed anyway
        assert result.approved is True
        assert "under-threshold" in result.reason  # NOT "state-changed"

    def test_change_aware_false_blocks_at_threshold(self):
        """change_aware=False + enough duplicates → blocked without Layer 1 bypass."""
        gate = ActionGate(_make_settings(action_gating_change_aware=False))
        ledger = _ledger()
        # Use same turn so all actions are within window
        ledger.set_turn(1)
        for _ in range(5):
            ledger.record("bash", {"command": "echo hello"}, "hello", "success")
            # Intervening write each time — would bypass with Layer 1
            ledger.record("write_file", {"path": "foo.txt"}, "ok", "success")

        result = gate._consistency_check("bash", {"command": "echo hello"}, ledger)
        # Layer 1 disabled → writes don't help. 5 matches, threshold 3/5 → blocked
        assert result.approved is False

    def test_custom_thresholds_honored(self):
        """Custom repeat/hard thresholds override defaults."""
        gate = ActionGate(_make_settings(
            action_gating_repeat_threshold=1,
            action_gating_hard_block_threshold=2,
        ))
        ledger = _ledger()
        ledger.set_turn(1)
        ledger.record("learn_fact", {"subject": "test"}, "ok", "success")
        ledger.set_turn(2)

        # 1st match → at repeat_threshold=1 → warning
        result = gate._consistency_check("learn_fact", {"subject": "test"}, ledger)
        assert result.approved is True
        assert "at-threshold-warning" in result.reason

        # Record 2nd
        ledger.record("learn_fact", {"subject": "test"}, "ok", "success")
        ledger.set_turn(3)

        # 2nd match → at hard_block=2 → blocked
        result = gate._consistency_check("learn_fact", {"subject": "test"}, ledger)
        assert result.approved is False
        assert "Doom-loop" in result.reason

    def test_failed_prior_calls_dont_count(self):
        """Failed actions are not counted toward the threshold."""
        gate = ActionGate(_make_settings())
        ledger = _ledger()
        ledger.set_turn(1)
        for _ in range(5):
            ledger.record("bash", {"command": "echo hello"}, "error", "error")

        result = gate._consistency_check("bash", {"command": "echo hello"}, ledger)
        assert result.approved is True
        assert result.reason == "no-duplicates"

    def test_different_tools_dont_interfere(self):
        """3x write_file + 3x bash should not cross-contaminate counts."""
        gate = ActionGate(_make_settings())
        ledger = _ledger()
        ledger.set_turn(1)
        for _ in range(3):
            ledger.record("write_file", {"path": "a.py"}, "ok", "success")
            ledger.record("bash", {"command": "echo hello"}, "hello", "success")

        # Each tool only has 3 matches — bash: echo is not iterative → threshold 3 → warning
        result_bash = gate._consistency_check("bash", {"command": "echo hello"}, ledger)
        assert result_bash.approved is True
        assert "at-threshold-warning" in result_bash.reason

        # write_file is iterative → threshold 6 → under threshold
        result_write = gate._consistency_check("write_file", {"path": "a.py"}, ledger)
        assert result_write.approved is True
        assert "under-threshold" in result_write.reason

    def test_test_fix_test_cycle_allowed(self):
        """pytest → edit → pytest → edit → pytest → all allowed by Layer 1."""
        gate = ActionGate(_make_settings())
        ledger = _ledger()
        for i in range(5):
            ledger.set_turn(i * 2 + 1)
            ledger.record("bash", {"command": "pytest tests/test_foo.py"}, "FAIL", "success")
            ledger.set_turn(i * 2 + 2)
            ledger.record("write_file", {"path": "foo.py"}, "ok", "success")
        ledger.set_turn(11)

        result = gate._consistency_check("bash", {"command": "pytest tests/test_foo.py"}, ledger)
        assert result.approved is True
        assert result.reason == "state-changed-since-last-run"

    def test_mixed_files_tracked_independently(self):
        """3x write_file(a.py) + 3x write_file(b.py) tracked independently."""
        gate = ActionGate(_make_settings())
        ledger = _ledger()
        ledger.set_turn(1)
        for _ in range(3):
            ledger.record("write_file", {"path": "a.py"}, "ok", "success")
            ledger.record("write_file", {"path": "b.py"}, "ok", "success")

        # Each path has 3 matches, but write_file is iterative → threshold 6
        result_a = gate._consistency_check("write_file", {"path": "a.py"}, ledger)
        assert result_a.approved is True

        result_b = gate._consistency_check("write_file", {"path": "b.py"}, ledger)
        assert result_b.approved is True

    def test_no_duplicates_returns_approved(self):
        """No matching prior calls → approved with 'no-duplicates' reason."""
        gate = ActionGate(_make_settings())
        ledger = _ledger()
        result = gate._consistency_check("bash", {"command": "echo hello"}, ledger)
        assert result.approved is True
        assert result.reason == "no-duplicates"
