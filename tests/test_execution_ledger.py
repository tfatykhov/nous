"""Comprehensive tests for nous/cognitive/execution_ledger.py.

Focuses on coverage not already in test_execution_integrity.py:
  - redact_key_args
  - _classify_bash_command edge cases (env vars, parens, git subcommands)
  - _extract_bash_command (cmd vs command key)
  - _estimate_tokens
  - _group_summary
  - _format_key_args
  - _friendly_label (via one_line_summary)
  - _summarize_args for every registered tool
  - system_prompt_section formatting details (blocked/error markers, effects)
  - current_turn property
  - ExecutedAction dataclass
  - classify_side_effect for EXTERNAL_TOOLS / IRREVERSIBLE_TOOLS paths
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from nous.cognitive.execution_ledger import (
    EXTERNAL_TOOLS,
    IRREVERSIBLE_TOOLS,
    READ_TOOLS,
    WRITE_TOOLS,
    ExecutedAction,
    ExecutionLedger,
    _classify_bash_command,
    _estimate_tokens,
    _extract_bash_command,
    _format_key_args,
    _friendly_label,
    _group_summary,
    classify_side_effect,
    redact_key_args,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ledger(session_id: str = "sess-1") -> ExecutionLedger:
    return ExecutionLedger(session_id=session_id)


def _action(
    tool_name: str = "read_file",
    status: str = "success",
    turn: int = 1,
    side_effect_type: str = "none",
    key_args: dict | None = None,
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


# ===========================================================================
# ExecutedAction dataclass
# ===========================================================================


class TestExecutedAction:
    def test_fields_accessible(self):
        ts = datetime.now(UTC)
        a = ExecutedAction(
            turn=3,
            tool_name="bash",
            key_args={"command": "ls"},
            status="success",
            timestamp=ts,
            result_summary="file1\nfile2",
            side_effect_type="none",
        )
        assert a.turn == 3
        assert a.tool_name == "bash"
        assert a.key_args == {"command": "ls"}
        assert a.status == "success"
        assert a.timestamp is ts
        assert a.result_summary == "file1\nfile2"
        assert a.side_effect_type == "none"

    def test_all_status_values(self):
        for status in ("success", "error", "timeout", "blocked"):
            a = _action(status=status)
            assert a.status == status


# ===========================================================================
# ExecutionLedger.current_turn property
# ===========================================================================


class TestCurrentTurn:
    def test_defaults_to_zero(self):
        ledger = _ledger()
        assert ledger.current_turn == 0

    def test_set_turn_updates_current_turn(self):
        ledger = _ledger()
        ledger.set_turn(7)
        assert ledger.current_turn == 7

    def test_set_turn_repeatedly(self):
        ledger = _ledger()
        for n in (1, 2, 5, 10):
            ledger.set_turn(n)
            assert ledger.current_turn == n


# ===========================================================================
# ExecutionLedger._summarize_args — all registered tools
# ===========================================================================


class TestSummarizeArgsAllTools:
    """_summarize_args picks the first matching key for every registered tool."""

    def _s(self, tool_name: str, args: dict) -> dict:
        return _ledger()._summarize_args(tool_name, args)

    def test_write_file_path(self):
        result = self._s("write_file", {"path": "out.txt", "content": "data"})
        assert result == {"path": "out.txt"}

    def test_write_file_file_path_fallback(self):
        result = self._s("write_file", {"file_path": "out.txt"})
        assert result == {"file_path": "out.txt"}

    def test_read_file(self):
        result = self._s("read_file", {"path": "src.py"})
        assert result == {"path": "src.py"}

    def test_bash_command_key(self):
        result = self._s("bash", {"command": "ls -la"})
        assert result == {"command": "ls -la"}

    def test_bash_cmd_key(self):
        result = self._s("bash", {"cmd": "pwd"})
        assert result == {"cmd": "pwd"}

    def test_learn_fact_subject(self):
        result = self._s("learn_fact", {"subject": "Paris", "content": "is capital"})
        assert result == {"subject": "Paris"}

    def test_learn_fact_content_fallback(self):
        result = self._s("learn_fact", {"content": "important", "fact": "f"})
        assert result == {"content": "important"}

    def test_learn_fact_fact_key(self):
        result = self._s("learn_fact", {"fact": "some fact"})
        assert result == {"fact": "some fact"}

    def test_learn_skill_name(self):
        result = self._s("learn_skill", {"name": "my_skill", "url": "http://x"})
        assert result == {"name": "my_skill"}

    def test_learn_skill_url_fallback(self):
        result = self._s("learn_skill", {"url": "http://example.com"})
        assert result == {"url": "http://example.com"}

    def test_recall_deep_query(self):
        result = self._s("recall_deep", {"query": "search term"})
        assert result == {"query": "search term"}

    def test_recall_deep_q_fallback(self):
        result = self._s("recall_deep", {"q": "short"})
        assert result == {"q": "short"}

    def test_recall_recent_limit(self):
        result = self._s("recall_recent", {"limit": "10"})
        assert result == {"limit": "10"}

    def test_record_decision_title(self):
        result = self._s("record_decision", {"title": "deploy now", "confidence": "0.9"})
        assert result == {"title": "deploy now"}

    def test_create_censor_name(self):
        result = self._s("create_censor", {"name": "no-pii", "expression": "..."})
        assert result == {"name": "no-pii"}

    def test_store_identity_section(self):
        result = self._s("store_identity", {"section": "bio", "key": "name"})
        assert result == {"section": "bio"}

    def test_spawn_task_description(self):
        result = self._s("spawn_task", {"description": "do stuff", "task": "t"})
        assert result == {"description": "do stuff"}

    def test_schedule_task_description(self):
        result = self._s("schedule_task", {"description": "daily run"})
        assert result == {"description": "daily run"}

    def test_cancel_task_id(self):
        result = self._s("cancel_task", {"task_id": "abc-123"})
        assert result == {"task_id": "abc-123"}

    def test_web_search_query(self):
        result = self._s("web_search", {"query": "latest news"})
        assert result == {"query": "latest news"}

    def test_web_fetch_url(self):
        result = self._s("web_fetch", {"url": "https://example.com"})
        assert result == {"url": "https://example.com"}

    def test_run_python_no_keys(self):
        # run_python key list is [], so falls back to first arg
        result = self._s("run_python", {"code": "print(1)"})
        assert result == {"code": "print(1)"}

    def test_get_procedure_name(self):
        result = self._s("get_procedure", {"name": "my_proc"})
        assert result == {"name": "my_proc"}

    def test_truncates_at_80_chars(self):
        long_val = "x" * 100
        result = self._s("recall_deep", {"query": long_val})
        assert len(result["query"]) == 80

    def test_unknown_tool_uses_first_arg(self):
        result = self._s("mystery_tool", {"alpha": "a", "beta": "b"})
        # Should use first key only
        assert len(result) == 1
        assert "alpha" in result

    def test_empty_args_unknown_tool(self):
        result = self._s("mystery_tool", {})
        assert result == {}


# ===========================================================================
# classify_side_effect — EXTERNAL_TOOLS / IRREVERSIBLE_TOOLS
# ===========================================================================


class TestClassifySideEffectSets:
    """Verify that EXTERNAL_TOOLS and IRREVERSIBLE_TOOLS sets are respected."""

    def test_external_tools_set_currently_empty(self):
        # The sets are intentionally empty; verify classifying an unknown tool
        # that's NOT in either set falls through to "write"
        assert len(EXTERNAL_TOOLS) == 0
        assert len(IRREVERSIBLE_TOOLS) == 0

    def test_monkey_patch_external_tool(self, monkeypatch):
        """A tool added to EXTERNAL_TOOLS is classified as 'external'."""
        import nous.cognitive.execution_ledger as mod

        monkeypatch.setattr(mod, "EXTERNAL_TOOLS", {"send_email"})
        result = classify_side_effect("send_email", {})
        assert result == "external"

    def test_monkey_patch_irreversible_tool(self, monkeypatch):
        """A tool added to IRREVERSIBLE_TOOLS is classified as 'irreversible'."""
        import nous.cognitive.execution_ledger as mod

        monkeypatch.setattr(mod, "IRREVERSIBLE_TOOLS", {"delete_forever"})
        result = classify_side_effect("delete_forever", {})
        assert result == "irreversible"

    def test_irreversible_takes_precedence_over_external(self, monkeypatch):
        """IRREVERSIBLE_TOOLS check runs before EXTERNAL_TOOLS."""
        import nous.cognitive.execution_ledger as mod

        monkeypatch.setattr(mod, "IRREVERSIBLE_TOOLS", {"nuke"})
        monkeypatch.setattr(mod, "EXTERNAL_TOOLS", {"nuke"})
        result = classify_side_effect("nuke", {})
        assert result == "irreversible"

    def test_classify_bash_with_cmd_key(self):
        """classify_side_effect uses 'cmd' as an alias for 'command'."""
        result = classify_side_effect("bash", {"cmd": "ls -la"})
        assert result == "none"

    def test_classify_bash_cmd_key_write(self):
        result = classify_side_effect("bash", {"cmd": "rm file.txt"})
        assert result == "write"


# ===========================================================================
# _classify_bash_command
# ===========================================================================


class TestClassifyBashCommand:
    def test_empty_string_is_write(self):
        assert _classify_bash_command("") == "write"

    def test_read_commands(self):
        read_cmds = [
            "cat foo.txt",
            "ls -la",
            "ll /tmp",
            "grep pattern file.py",
            "rg TODO .",
            "find . -name '*.py'",
            "head -n 10 file.txt",
            "tail -f log.txt",
            "wc -l file.txt",
            "diff a.txt b.txt",
            "stat file.txt",
            "echo hello",
            "printf '%s' x",
            "which python",
            "pwd",
            "env",
            "printenv PATH",
            "sort file.txt",
            "uniq file.txt",
            "cut -d, -f1 csv.txt",
            "tr 'a-z' 'A-Z'",
            "basename /path/file.txt",
            "dirname /path/file.txt",
            "realpath ./file.txt",
            "readlink -f file",
        ]
        for cmd in read_cmds:
            assert _classify_bash_command(cmd) == "none", f"Expected 'none' for: {cmd!r}"

    def test_git_read_subcommands(self):
        for sub in ("log", "status", "diff", "show", "branch", "tag", "remote", "ls-files"):
            cmd = f"git {sub} --oneline"
            assert _classify_bash_command(cmd) == "none", f"Expected 'none' for: {cmd!r}"

    def test_git_push_is_external(self):
        assert _classify_bash_command("git push origin main") == "external"
        assert _classify_bash_command("git push-upstream") == "external"

    def test_git_write_subcommands(self):
        for sub in ("commit", "merge", "rebase", "reset", "checkout", "add"):
            cmd = f"git {sub}"
            assert _classify_bash_command(cmd) == "write", f"Expected 'write' for: {cmd!r}"

    def test_curl_is_external(self):
        assert _classify_bash_command("curl https://example.com") == "external"
        assert _classify_bash_command("curl -X POST https://api.example.com/data") == "external"

    def test_wget_is_external(self):
        assert _classify_bash_command("wget https://example.com/file.zip") == "external"

    def test_http_is_external(self):
        assert _classify_bash_command("http GET https://api.example.com") == "external"

    def test_httpie_is_external(self):
        assert _classify_bash_command("httpie POST https://api.example.com") == "external"

    def test_env_assignment_prefix_stripped(self):
        # "FOO=bar cat file.txt" — the first non-assignment token is "cat"
        assert _classify_bash_command("FOO=bar cat file.txt") == "none"

    def test_env_assignment_prefix_multiple(self):
        assert _classify_bash_command("FOO=1 BAR=2 ls") == "none"

    def test_env_assignment_then_write(self):
        assert _classify_bash_command("DEBUG=1 rm file.txt") == "write"

    def test_parenthesis_stripped_from_first_token(self):
        # "(cat file.txt)" — leading paren stripped
        assert _classify_bash_command("(cat file.txt)") == "none"

    def test_default_to_write_for_unknown(self):
        for cmd in ("rm -rf /tmp", "mv old new", "touch newfile", "chmod 755 f", "make build"):
            assert _classify_bash_command(cmd) == "write", f"Expected 'write' for: {cmd!r}"

    def test_sed_is_read(self):
        # sed is in _READ_COMMANDS
        assert _classify_bash_command("sed -n 's/foo/bar/p' file.txt") == "none"

    def test_awk_is_read(self):
        assert _classify_bash_command("awk '{print $1}' file.txt") == "none"

    def test_type_is_read(self):
        assert _classify_bash_command("type python3") == "none"

    def test_less_is_read(self):
        assert _classify_bash_command("less file.txt") == "none"

    def test_more_is_read(self):
        assert _classify_bash_command("more file.txt") == "none"


# ===========================================================================
# _extract_bash_command
# ===========================================================================


class TestExtractBashCommand:
    def test_command_key(self):
        assert _extract_bash_command({"command": "ls -la"}) == "ls -la"

    def test_cmd_key(self):
        assert _extract_bash_command({"cmd": "pwd"}) == "pwd"

    def test_command_takes_priority_over_cmd(self):
        result = _extract_bash_command({"command": "ls", "cmd": "pwd"})
        assert result == "ls"

    def test_empty_dict_returns_empty_string(self):
        assert _extract_bash_command({}) == ""

    def test_other_keys_ignored(self):
        assert _extract_bash_command({"script": "echo hi"}) == ""


# ===========================================================================
# _estimate_tokens
# ===========================================================================


class TestEstimateTokens:
    def test_empty_string(self):
        assert _estimate_tokens("") == 0

    def test_four_chars_is_one_token(self):
        assert _estimate_tokens("abcd") == 1

    def test_eight_chars_is_two_tokens(self):
        assert _estimate_tokens("abcdefgh") == 2

    def test_large_text(self):
        text = "a" * 400
        assert _estimate_tokens(text) == 100


# ===========================================================================
# _group_summary
# ===========================================================================


class TestGroupSummary:
    def test_empty_list(self):
        result = _group_summary([])
        assert result == ""

    def test_single_tool(self):
        actions = [_action("recall_deep")]
        result = _group_summary(actions)
        assert "recall_deep" in result
        assert "1x" in result

    def test_multiple_same_tool(self):
        actions = [_action("recall_deep"), _action("recall_deep"), _action("recall_deep")]
        result = _group_summary(actions)
        assert "3x recall_deep" in result

    def test_multiple_tools(self):
        actions = [
            _action("recall_deep"),
            _action("write_file"),
            _action("recall_deep"),
        ]
        result = _group_summary(actions)
        assert "2x recall_deep" in result
        assert "1x write_file" in result

    def test_most_common_first(self):
        actions = [_action("write_file"), _action("recall_deep"), _action("recall_deep")]
        result = _group_summary(actions)
        # recall_deep (2x) should appear before write_file (1x)
        assert result.index("recall_deep") < result.index("write_file")


# ===========================================================================
# _format_key_args
# ===========================================================================


class TestFormatKeyArgs:
    def test_empty_dict(self):
        assert _format_key_args({}) == ""

    def test_single_key(self):
        result = _format_key_args({"path": "foo.txt"})
        assert result == " path=foo.txt"

    def test_multiple_keys(self):
        result = _format_key_args({"path": "a.py", "query": "hello"})
        assert "path=a.py" in result
        assert "query=hello" in result
        assert result.startswith(" ")


# ===========================================================================
# _friendly_label
# ===========================================================================


class TestFriendlyLabel:
    def test_known_labels(self):
        assert _friendly_label("recall_deep") == "searches"
        assert _friendly_label("recall_recent") == "searches"
        assert _friendly_label("web_search") == "searches"
        assert _friendly_label("web_fetch") == "fetches"
        assert _friendly_label("read_file") == "file reads"
        assert _friendly_label("write_file") == "file writes"
        assert _friendly_label("bash") == "bash"
        assert _friendly_label("learn_fact") == "fact stores"
        assert _friendly_label("record_decision") == "decisions"
        assert _friendly_label("spawn_task") == "tasks spawned"
        assert _friendly_label("schedule_task") == "schedules"
        assert _friendly_label("run_python") == "python runs"

    def test_unknown_tool_returns_tool_name(self):
        assert _friendly_label("mystery_tool") == "mystery_tool"

    def test_via_one_line_summary(self):
        """_friendly_label is exercised through one_line_summary."""
        ledger = _ledger()
        ledger.set_turn(1)
        ledger.record("web_fetch", {"url": "https://x.com"}, "html", "success")
        ledger.record("web_fetch", {"url": "https://y.com"}, "html", "success")
        summary = ledger.one_line_summary()
        assert "2 fetches" in summary


# ===========================================================================
# redact_key_args
# ===========================================================================


class TestRedactKeyArgs:
    def test_non_bash_tool_returned_unchanged(self):
        key_args = {"path": "SECRET=xyz foo.txt", "Bearer secret-token": "val"}
        for tool in ("read_file", "write_file", "recall_deep", "web_fetch"):
            result = redact_key_args(tool, key_args)
            assert result is key_args  # exact same object — not copied

    def test_bash_env_var_assignment_redacted(self):
        key_args = {"command": "API_KEY=abc123 curl https://api.example.com"}
        result = redact_key_args("bash", key_args)
        assert "[REDACTED_ENV]" in result["command"]
        assert "abc123" not in result["command"]

    def test_bash_bearer_token_redacted(self):
        key_args = {"command": "curl -H 'Authorization: Bearer my-secret-token' https://api.com"}
        result = redact_key_args("bash", key_args)
        assert "Bearer [REDACTED]" in result["command"]
        assert "my-secret-token" not in result["command"]

    def test_bash_url_credentials_redacted(self):
        key_args = {"command": "git clone https://user:password123@github.com/repo"}
        result = redact_key_args("bash", key_args)
        assert "[REDACTED]@" in result["command"]
        assert "password123" not in result["command"]

    def test_bash_no_sensitive_data_unchanged(self):
        key_args = {"command": "ls -la /tmp"}
        result = redact_key_args("bash", key_args)
        assert result["command"] == "ls -la /tmp"

    def test_bash_multiple_patterns_applied(self):
        key_args = {
            "command": "AUTH_TOKEN=secret Bearer secret2 https://admin:pass@host"
        }
        result = redact_key_args("bash", key_args)
        assert "secret" not in result["command"]
        assert "[REDACTED" in result["command"]

    def test_bash_returns_new_dict(self):
        key_args = {"command": "ls"}
        result = redact_key_args("bash", key_args)
        # Even for bash with no redactable content, should return a dict
        assert isinstance(result, dict)

    def test_bearer_case_insensitive(self):
        key_args = {"command": "curl -H 'authorization: bearer abc' url"}
        result = redact_key_args("bash", key_args)
        assert "abc" not in result["command"]


# ===========================================================================
# system_prompt_section formatting details
# ===========================================================================


class TestSystemPromptSectionFormatting:
    def test_blocked_action_shows_blocked_marker(self):
        ledger = _ledger()
        ledger.set_turn(1)
        ledger.record("write_file", {"path": "f.txt"}, "gate blocked this", "blocked")
        section = ledger.system_prompt_section()
        assert "[BLOCKED]" in section

    def test_timeout_action_shows_timeout_marker(self):
        ledger = _ledger()
        ledger.set_turn(1)
        ledger.record("bash", {"command": "slow_cmd"}, "timed out", "timeout")
        section = ledger.system_prompt_section()
        assert "[TIMEOUT]" in section

    def test_read_only_action_no_effect_marker(self):
        ledger = _ledger()
        ledger.set_turn(1)
        ledger.record("recall_deep", {"query": "q"}, "results", "success")
        section = ledger.system_prompt_section()
        # Read-only actions should NOT show an effect marker
        assert "(none)" not in section
        assert "(write)" not in section

    def test_external_effect_marker_shown(self):
        ledger = _ledger()
        ledger.set_turn(1)
        # Simulate bash with git push (external)
        ledger.record("bash", {"command": "git push origin main"}, "ok", "success")
        section = ledger.system_prompt_section()
        assert "(external)" in section

    def test_error_result_summary_shown(self):
        ledger = _ledger()
        ledger.set_turn(1)
        ledger.record("bash", {"command": "bad cmd"}, "command not found", "error")
        section = ledger.system_prompt_section()
        assert "command not found" in section

    def test_blocked_result_summary_shown(self):
        ledger = _ledger()
        ledger.set_turn(1)
        ledger.record("write_file", {"path": "x"}, "duplicate action blocked", "blocked")
        section = ledger.system_prompt_section()
        assert "duplicate action blocked" in section

    def test_success_result_summary_not_shown(self):
        """Success result summaries are NOT shown in the ledger section."""
        ledger = _ledger()
        ledger.set_turn(1)
        ledger.record("read_file", {"path": "f.py"}, "def main(): pass", "success")
        section = ledger.system_prompt_section()
        assert "def main(): pass" not in section

    def test_turn_number_in_section(self):
        ledger = _ledger()
        ledger.set_turn(4)
        ledger.record("recall_deep", {"query": "test"}, "results", "success")
        section = ledger.system_prompt_section()
        assert "T4" in section

    def test_session_id_does_not_appear_in_section(self):
        ledger = _ledger("my-private-session-id")
        ledger.set_turn(1)
        ledger.record("read_file", {"path": "f.py"}, "ok", "success")
        section = ledger.system_prompt_section()
        # Session ID should not be exposed in the prompt
        assert "my-private-session-id" not in section

    def test_old_and_recent_actions_separated(self):
        ledger = _ledger()
        ledger.set_turn(1)
        ledger.record("recall_deep", {"query": "old query"}, "r", "success")
        ledger.set_turn(10)
        ledger.record("write_file", {"path": "new.txt"}, "ok", "success")
        section = ledger.system_prompt_section()
        assert "Prior turns" in section
        assert "recall_deep" in section
        assert "write_file" in section
        # Recent action is listed individually (has T10)
        assert "T10" in section

    def test_max_tokens_zero_returns_string(self):
        ledger = _ledger()
        ledger.set_turn(1)
        ledger.record("recall_deep", {"query": "q"}, "r", "success")
        # Even with 0 budget, must return a string and not crash
        section = ledger.system_prompt_section(max_tokens=0)
        assert isinstance(section, str)

    def test_many_actions_stays_within_rough_budget(self):
        ledger = _ledger()
        ledger.set_turn(1)
        for i in range(100):
            ledger.record("recall_deep", {"query": f"query {i}"}, "r", "success")
        section = ledger.system_prompt_section(max_tokens=200)
        # Rough check: each token = 4 chars, so 200 tokens = 800 chars
        # The section can exceed if truncation is the last resort but must be reasonable
        assert isinstance(section, str)
        assert len(section) < 5000  # sanity cap — not KB of text


# ===========================================================================
# ExecutionLedger.record — edge cases
# ===========================================================================


class TestLedgerRecord:
    def test_blocked_status_recorded(self):
        ledger = _ledger()
        ledger.set_turn(1)
        action = ledger.record("write_file", {"path": "f.txt"}, "gate blocked", "blocked")
        assert action.status == "blocked"
        assert action.side_effect_type == "write"

    def test_timeout_status_recorded(self):
        ledger = _ledger()
        ledger.set_turn(1)
        action = ledger.record("bash", {"command": "slow"}, "timed out", "timeout")
        assert action.status == "timeout"

    def test_result_summary_exactly_100_chars(self):
        ledger = _ledger()
        ledger.set_turn(1)
        result = "x" * 100
        action = ledger.record("read_file", {"path": "f"}, result, "success")
        assert action.result_summary == result

    def test_result_summary_over_100_chars_truncated(self):
        ledger = _ledger()
        ledger.set_turn(1)
        result = "x" * 150
        action = ledger.record("read_file", {"path": "f"}, result, "success")
        assert len(action.result_summary) == 100

    def test_record_timestamps_are_set(self):
        before = datetime.now(UTC)
        ledger = _ledger()
        ledger.set_turn(1)
        action = ledger.record("read_file", {"path": "f"}, "ok", "success")
        after = datetime.now(UTC)
        assert before <= action.timestamp <= after

    def test_bash_read_command_side_effect_none(self):
        ledger = _ledger()
        ledger.set_turn(1)
        action = ledger.record("bash", {"command": "cat file.txt"}, "content", "success")
        assert action.side_effect_type == "none"

    def test_bash_write_command_side_effect_write(self):
        ledger = _ledger()
        ledger.set_turn(1)
        action = ledger.record("bash", {"command": "rm file.txt"}, "ok", "success")
        assert action.side_effect_type == "write"

    def test_bash_external_command_side_effect_external(self):
        ledger = _ledger()
        ledger.set_turn(1)
        action = ledger.record("bash", {"command": "curl https://example.com"}, "html", "success")
        assert action.side_effect_type == "external"

    def test_unknown_tool_defaults_to_write_side_effect(self):
        ledger = _ledger()
        ledger.set_turn(1)
        action = ledger.record("unknown_tool", {}, "ok", "success")
        assert action.side_effect_type == "write"


# ===========================================================================
# Tool classification set membership sanity checks
# ===========================================================================


class TestToolClassificationSets:
    def test_read_tools_are_not_in_write_tools(self):
        assert READ_TOOLS.isdisjoint(WRITE_TOOLS)

    def test_expected_read_tools_present(self):
        expected = {"recall_deep", "recall_recent", "read_file", "web_search", "web_fetch"}
        assert expected.issubset(READ_TOOLS)

    def test_expected_write_tools_present(self):
        expected = {"write_file", "learn_fact", "record_decision", "spawn_task"}
        assert expected.issubset(WRITE_TOOLS)

    def test_all_read_tools_classify_as_none(self):
        for tool in READ_TOOLS:
            assert classify_side_effect(tool) == "none", f"{tool} should be 'none'"

    def test_all_write_tools_classify_as_write(self):
        for tool in WRITE_TOOLS:
            result = classify_side_effect(tool)
            assert result == "write", f"{tool} should be 'write', got {result!r}"
