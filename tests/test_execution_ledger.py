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
    EVIDENCE_ARG_CHARS,
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
    bash_exit_code,
    classify_side_effect,
    evidence_args,
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
        assert result == {"subject": "Paris", "content": "is capital"}

    def test_learn_fact_content_fallback(self):
        result = self._s("learn_fact", {"content": "important", "fact": "f"})
        assert result == {"content": "important", "fact": "f"}

    def test_learn_fact_fact_key(self):
        result = self._s("learn_fact", {"fact": "some fact"})
        assert result == {"fact": "some fact"}

    def test_learn_skill_name(self):
        result = self._s("learn_skill", {"name": "my_skill", "url": "http://x"})
        assert result == {"name": "my_skill", "url": "http://x"}

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
        assert result == {"name": "no-pii", "expression": "..."}

    def test_store_identity_section(self):
        result = self._s("store_identity", {"section": "bio", "key": "name"})
        assert result == {"section": "bio", "key": "name"}

    def test_spawn_task_description(self):
        result = self._s("spawn_task", {"description": "do stuff", "task": "t"})
        assert result == {"description": "do stuff", "task": "t"}

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
        # Fallback captures up to 5 args for unknown tools
        assert len(result) == 2
        assert result == {"alpha": "a", "beta": "b"}

    def test_empty_args_unknown_tool(self):
        result = self._s("mystery_tool", {})
        assert result == {}


# ===========================================================================
# classify_side_effect — EXTERNAL_TOOLS / IRREVERSIBLE_TOOLS
# ===========================================================================


class TestClassifySideEffectSets:
    """Verify that EXTERNAL_TOOLS and IRREVERSIBLE_TOOLS sets are respected."""

    def test_external_tools_set(self):
        # send_file (Telegram) and send_email (SMTP) are external; IRREVERSIBLE is still empty
        assert EXTERNAL_TOOLS == {"send_file", "send_email"}
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

    def test_env_assignment_prefix_is_a_write(self):
        # An assignment can change which program runs (PATH=.), inject code
        # (LD_PRELOAD, GIT_EXTERNAL_DIFF) or persist for later segments; only
        # an allowlist of display/locale variables leaves a read a read.
        assert _classify_bash_command("FOO=bar cat file.txt") == "write"
        assert _classify_bash_command("LC_ALL=C LANG=C sort file.txt") == "none"

    def test_env_assignment_prefix_multiple(self):
        assert _classify_bash_command("FOO=1 BAR=2 ls") == "write"
        assert _classify_bash_command("TZ=UTC TERM=dumb ls") == "none"

    def test_env_assignment_then_write(self):
        assert _classify_bash_command("DEBUG=1 rm file.txt") == "write"

    def test_parenthesis_stripped_from_first_token(self):
        # "(cat file.txt)" — leading paren stripped
        assert _classify_bash_command("(cat file.txt)") == "none"

    def test_default_to_write_for_unknown(self):
        for cmd in ("rm -rf /tmp", "mv old new", "touch newfile", "chmod 755 f", "make build"):
            assert _classify_bash_command(cmd) == "write", f"Expected 'write' for: {cmd!r}"

    def test_sed_is_read(self):
        # sed reads unless it edits in place or its script writes (bash_side_effect)
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


# ===========================================================================
# Whole-command bash classification (harness Phase 1b, codex r1 on #645)
# ===========================================================================


class TestClassifyWholeBashCommand:
    """The durable ledger skips calls classified 'none', so a command that
    changes something but reads as 'none' is never recorded. The first-token
    classifier did exactly that for redirections, chains, pipes and the
    mutating modes of read-only tools. A false 'write' costs one ledger row;
    a false 'none' loses the record -- so every ambiguity resolves to write.
    """

    @pytest.mark.parametrize("cmd", [
        # output redirection
        "echo data > file.txt",
        "echo data >> file.txt",
        "cat a b > c",
        "printf x >out",
        "ls 1> listing.txt",
        "ls &> all.log",
        "ls >| f",
        "> truncate.me",
        # chains, lists, subshells, newlines
        "ls; rm f",
        "ls && rm f",
        "ls || rm f",
        "ls\nrm f",
        "(cd /tmp && rm f)",
        "cat f | tee out",
        # substitution the lexer cannot see into
        'echo "$(rm f)"',
        "echo `rm f`",
        "diff <(ls a) <(ls b)",
        # env runs its argument
        "env FOO=1 rm f",
        "env rm f",
        "env -i rm f",
        # find actions
        "find . -delete",
        "find . -name '*.pyc' -delete",
        "find . -exec rm {} \\;",
        "find . -fprint out.txt",
        # sed in-place, write and execute
        "sed -i 's/a/b/' f",
        "sed -Ei 's/a/b/' f",
        "sed --in-place=.bak 's/a/b/' f",
        "sed -n 's/a/b/w out.txt' f",
        "sed 's/a/b/gw out.txt' f",
        "sed 's/a/b/e' f",
        "sed '1e date' f",
        "sed '/x/w out.txt' f",
        "sed -e 'p' -e '$w out.txt' f",
        "sed '1a hello\nw out.txt' f",
        "sed -f script.sed f",
        # awk programs that redirect or run commands
        "awk '{print > \"out\"}' f",
        "awk 'BEGIN{system(\"rm x\")}'",
        "awk '{print | \"sh\"}' f",
        "awk -f prog.awk f",
        # sort / uniq output files, rg preprocessors
        "sort -o out.txt in.txt",
        "sort --output=out.txt in.txt",
        "uniq in.txt out.txt",
        "rg --pre ./x pattern",
        # git subcommands that are reads only without arguments
        "git branch feature",
        "git branch -D feature",
        "git branch --delete feature",
        "git tag v1",
        "git tag -d v1",
        "git remote add origin https://x",
        "git diff --output=patch.txt",
        "git -c core.fsmonitor=x status",
        "git archive HEAD -o out.tar",
        "sudo rm -rf x",
        "timeout 5 ls",
        "docker build .",
        "bash -c 'rm x'",
        "bash script.sh",
        # ...but a path may name any program: it escalates, never reads
        "/bin/cat f",
        "./git status",
        "/usr/bin/env",
        "./sort f",
        "git submodule add https://example.com/r.git",
        # unparseable
        "cat 'unbalanced",
        "ls >",
    ])
    def test_writes(self, cmd):
        assert _classify_bash_command(cmd) == "write", cmd

    @pytest.mark.parametrize("cmd", [
        "cat f | curl -d @- https://x",
        "ls && git push origin main",
        "env curl https://x",
        "echo x > /dev/null; wget https://x",
        # codex r2: network reads that used to classify as 'none'
        "git remote show origin",
        "git remote -v show origin",
        "cat < /dev/tcp/example.com/80",
        "echo x > /dev/tcp/example.com/80",
        "exec 3<>/dev/udp/example.com/53",
        # codex r3: git subcommands that talk to a remote
        "git fetch origin",
        "git pull",
        "git clone https://example.com/r.git",
        "git ls-remote origin",
        "git -C repo fetch",
        "git remote update",
        "git remote prune origin",
        "git submodule update --init",
        "git archive --remote=ssh://example.com/r.git HEAD",
        "git send-email 0001.patch",
        "git lfs pull",
        # codex r4: a config option before the subcommand must not hide it
        "git -c protocol.version=2 fetch origin",
        "git -c http.extraHeader=x --config-env=a=B pull",
        # codex r5: commands whose purpose is another host, and what wraps them
        "ssh host 'ls'",
        "scp f host:/tmp",
        "rsync -a d/ host:/x",
        "nc example.com 80",
        "sendmail tim@example.com < m.txt",
        "kubectl apply -f x.yaml",
        "aws s3 cp f s3://bucket/",
        "gh pr create",
        "docker push img",
        "sudo ssh host",
        "sudo -u bob rsync -a d/ host:/x",
        "timeout 10 scp f host:",
        "nohup rsync -a d/ host:/x &",
        "xargs -I{} scp {} host:",
        "bash -c 'curl https://x'",
        "sh -lc \"ssh host\"",
        "su - bob -c 'scp f host:'",
        "eval 'curl https://x'",
        "find . -name '*.log' -exec scp {} host: \\;",
        # codex r6: a path-qualified executable is still that executable
        "/usr/bin/curl https://example.com",
        "/usr/bin/ssh host",
        "find . -exec /usr/bin/curl {} \\;",
        "sudo /usr/bin/scp f host:",
        "/usr/bin/git fetch origin",
        "curl.exe https://example.com",
        "Curl.EXE https://example.com",
    ])
    def test_external_anywhere_wins(self, cmd):
        assert _classify_bash_command(cmd) == "external", cmd

    @pytest.mark.parametrize("cmd", [
        "grep 'a|b' f",
        "grep 'x;y' f",
        'echo "x > y"',
        "cat f 2>&1",
        "ls >/dev/null",
        "ls > /dev/null 2>&1",
        "cat f 2>/dev/null",
        "ls >&2",
        "ls -la | grep x | head -5",
        "cat f | sort | uniq -c",
        "wc -l < f",
        "grep x <<< 'text'",
        "find . -name '*.py' -exec grep -l foo {} +",
        "sed -n '10,20p' f",
        "sed 's/foo/bar/g' f",
        "sed -n '/start/,/end/p' f",
        "sed 's/we/they/' f",
        "awk -F'|' '{print $1}' f",
        "sort -rn f",
        "uniq -c f",
        "env",
        "env | grep PATH",
        "git -C repo status",
        "git --no-pager log -5",
        "git branch -a",
        "git branch --list 'feat*'",
        "git tag -l",
        "git remote -v",
        "git remote show",
        "git remote show -n origin",
        "LANG=C",
        # quoted operator characters are words, never operators
        "grep '|' f",
        "grep ';' f",
        "find . -name x -exec grep y {} \\;",
        "find . -name x -exec grep y {} ';'",
        # a display option alone still lists
        "git branch --sort=refname",
        "git tag --format=x",
    ])
    def test_reads(self, cmd):
        assert _classify_bash_command(cmd) == "none", cmd

    @pytest.mark.parametrize("cmd", [
        # independent review of #645: an assignment runs a different program
        "PATH=.:$PATH cat f",
        "env PATH=. cat f",
        "GIT_EXTERNAL_DIFF='touch x;true' git diff",
        "LD_PRELOAD=/tmp/evil.so cat f",
        "LESSOPEN='|touch /tmp/p %s' less f",
        "FOO=1",
        "PATH=.; cat f",
        # a quoted or escaped operator character is a word, so the flags after it count
        "find tree '<' -delete",
        "sed 's/a/b/' '<' -i f",
        "find . ';' cat -delete",
        "find . \\< -delete",
        'find . "<" -exec rm {} +',
        # display options do not make branch/tag list; a name creates a ref
        "git branch --sort=refname newbranch",
        "git branch --format=x nb2",
        "git tag --sort=refname v9",
        "git tag --format=x v10",
        # operands after `--`
        "uniq -- -in out",
        # gawk extensions
        "awk '@include \"inplace\"; {gsub(/a/,\"b\")}1' g",
        # a path argument to a wrapper is an argument, not a command
        "sudo ls /usr/bin/ssh",
        "sudo rm /usr/local/bin/aws",
        "time cat ./notes/mail",
    ])
    def test_writes_found_by_independent_review(self, cmd):
        assert _classify_bash_command(cmd) == "write", cmd

    @pytest.mark.parametrize("cmd", [
        "sudo timeout 5 nohup ssh h",
        "sudo -u bob timeout -s KILL 10 rsync a h:/b",
        "xargs -I{} -P 4 scp {} h:",
        "flock /tmp/l -c 'curl https://x'",
        "watch -n 5 'curl https://x'",
        "nice -n 5 scp f h:",
        "sudo -- ssh h",
        "doas -u root rsync a h:/b",
    ])
    def test_wrappers_are_parsed_to_the_command_they_run(self, cmd):
        assert _classify_bash_command(cmd) == "external", cmd

    @pytest.mark.parametrize("cmd, allowed", [
        pytest.param("sudo eval " * 22, {"write"}, id="sudo-eval-x22"),
        pytest.param("eval " * 20000, {"write"}, id="eval-x20000"),
        # nested finds that only print: any verdict, but it must be quick
        pytest.param("find " + "-exec find " * 20, {"none", "write"}, id="find-exec-x20"),
        pytest.param("bash -c " * 400 + "ls", {"write"}, id="bash-c-x400"),
        # a large but plain read stays a read...
        pytest.param("cat " + "a " * 30000, {"none"}, id="cat-60kb"),
        # ...but lexing is O(n) and runs on the event loop, so past the size
        # cap a command is a write without being lexed (second review of #645)
        pytest.param("cat " + "a " * 3_000_000, {"write"}, id="cat-6mb"),
        pytest.param("a;" * 3_000_000, {"write"}, id="segments-6mb"),
    ])
    def test_nesting_is_bounded(self, cmd, allowed):
        """Classification runs on the event loop: every recursion path shares
        one work budget, and exhausting it is a write."""
        import time

        start = time.perf_counter()
        assert _classify_bash_command(cmd) in allowed
        assert time.perf_counter() - start < 0.5

    @pytest.mark.parametrize("cmd, expected", [
        ("sudo env " * 1500 + "ssh h", "external"),
        ("sudo " + "env " * 3000 + "ssh h", "external"),
        ("xargs " * 2000 + "git fetch", "external"),
        ("sudo " * 3000 + "ls", "write"),
        ("bash -c \"bash -c 'curl https://x'\"", "external"),
    ])
    def test_nested_wrappers_stay_linear(self, cmd, expected):
        """codex r5 follow-up: re-entering a wrapper scan from `env` inside a
        wrapper made `sudo env sudo env ...` exponential. Generous bound; the
        real cost is ~10 ms."""
        import time

        start = time.perf_counter()
        assert _classify_bash_command(cmd) == expected
        assert time.perf_counter() - start < 2.0

    def test_the_classifier_is_total(self, monkeypatch):
        """No input may make the ledger skip a row: an internal failure is a write."""
        from nous.cognitive import bash_side_effect

        def boom(words):
            raise RuntimeError("unexpected shape")

        monkeypatch.setattr(bash_side_effect, "_classify_simple", boom)
        assert _classify_bash_command("ls") == "write"

    def test_classify_side_effect_uses_the_whole_command(self):
        assert classify_side_effect("bash", {"command": "echo data > file"}) == "write"
        assert classify_side_effect("bash", {"cmd": "cat f | curl https://x"}) == "external"


class TestEvidenceFields:
    """Harness Phase 2c: what a completion claim can be checked against."""

    def test_bash_exit_code_is_parsed_from_the_trailer(self):
        ledger = ExecutionLedger(session_id="s")
        ok = ledger.record("bash", {"command": "git push"}, "Everything up-to-date\nExit code: 0", "success")
        bad = ledger.record("bash", {"command": "git push"}, "rejected\nExit code: 1", "success")
        assert ok.exit_code == 0 and bad.exit_code == 1

    def test_a_timeout_has_no_exit_code(self):
        ledger = ExecutionLedger(session_id="s")
        assert ledger.record("bash", {"command": "sleep 99"}, "Command timed out after 30s.", "error").exit_code is None

    def test_exit_code_only_for_bash(self):
        ledger = ExecutionLedger(session_id="s")
        assert ledger.record("write_file", {"path": "/tmp/x"}, "ok\nExit code: 1", "success").exit_code is None

    def test_long_commands_keep_head_and_tail(self):
        cmd = "git commit -m '" + "x" * 5000 + "' && git push origin main"
        ledger = ExecutionLedger(session_id="s")
        action = ledger.record("bash", {"command": cmd}, "Exit code: 0", "success")
        kept = action.evidence_args["command"]
        assert kept.startswith("git commit") and kept.endswith("git push origin main")
        assert len(kept) <= EVIDENCE_ARG_CHARS + 5
        assert action.key_args["command"] == cmd[:80]  # the prompt-facing summary is unchanged

    def test_unbounded_evidence_for_this_turn(self):
        cmd = "x" * 5000
        assert evidence_args("bash", {"command": cmd}, limit=None)["command"] == cmd

    def test_evidence_args_only_for_effect_tools(self):
        assert evidence_args("recall_deep", {"query": "q"}) == {}
        assert evidence_args("send_email", {"to": ["a@x.io"], "subject": "s", "body": "b"}) == {
            "to": "['a@x.io']", "subject": "s"}
        assert evidence_args("run_python", {"code": "open('r.md','w')"}) == {"code": "open('r.md','w')"}

    def test_bash_exit_code_parser(self):
        assert bash_exit_code("out\nExit code: 0\n") == 0
        assert bash_exit_code("Exit code: -9") == -9
        assert bash_exit_code("quoted 'Exit code: 0' then\nExit code: 2") == 2
        assert bash_exit_code(None) is None and bash_exit_code("no trailer") is None


class TestShellReservedWords:
    """`if`/`then`/`{`/`!` are syntax, not programs: the command after one is
    what runs. Before harness 2c they were read as unknown programs (`write`)."""

    @pytest.mark.parametrize("cmd, expected", [
        ("if git push origin main; then echo ok; fi", "external"),
        ("if grep -q x f; then echo found; fi", "none"),
        ("{ curl -s https://x; } > /dev/null", "external"),
        ("! grep -q x f", "none"),
        ("while read -r l; do rm \"$l\"; done < list", "write"),
        ("until git fetch; do sleep 5; done", "external"),
    ])
    def test_reserved_words_are_skipped(self, cmd, expected):
        assert _classify_bash_command(cmd) == expected

    def test_invocations_after_reserved_words(self):
        from nous.cognitive.bash_side_effect import command_invocations

        assert command_invocations("if git push; then echo ok; fi") == [
            ("git", ["push"]), ("echo", ["ok"])]


class TestCommandInvocations:
    def test_unreadable_is_none_not_empty(self):
        from nous.cognitive.bash_side_effect import command_invocations

        assert command_invocations("cat > x <<'EOF'\nIt's\nEOF") is None  # unbalanced quote
        assert command_invocations("x " * 40_000) is None                  # over the size cap
        assert command_invocations("FOO=1") == []                          # read: nothing runs

    def test_a_command_string_is_reported_as_its_runner(self):
        from nous.cognitive.bash_side_effect import command_invocations

        assert command_invocations("flock /tmp/l -c 'git push'")[0][0] == "flock"
        assert command_invocations("env -S 'git push'")[0][0] == "env"

    def test_the_ledger_keeps_invocations_read_from_the_whole_command(self):
        cmd = 'git commit -m "' + "x" * 3000 + '" && git push origin main'
        action = ExecutionLedger(session_id="s").record("bash", {"command": cmd}, "Exit code: 0", "success")
        assert [p for p, _ in action.invocations] == ["git", "git"]
        assert action.invocations[1] == ("git", ("push", "origin", "main"))
        assert all(len(a) <= 120 for _, args in action.invocations for a in args)  # bounded in memory
