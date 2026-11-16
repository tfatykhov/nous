"""F115: the tool guard's snapshot, its rule of which turns it holds, and the host's locked tools (no database)."""

from __future__ import annotations

import logging

import pytest

from nous.api import tool_policy
from nous.api.execution_context import BACKGROUND_SESSION_PREFIXES, CONTEXT_KINDS, FOREGROUND_KINDS
from nous.api.runner import TERMINAL_EXTRA_TOOLS
from nous.autonomy import tool_guard
from nous.autonomy.tool_guard import HARNESS_TOOLS, ToolGuard, guarded_turn, parse_locked_tools

BACKGROUND_KINDS = [kind for kind in CONTEXT_KINDS if kind not in FOREGROUND_KINDS and kind != "approved_action"]
SESSIONS = [None, "", "telegram-42", "abc", *(prefix + "x" for prefix in BACKGROUND_SESSION_PREFIXES)]


def _expected(kind: str, session_id: str | None) -> bool:
    """The rule, written out: an approved action never; a background kind always; a foreground kind (chat, an
    inbound MCP call) only in a session a background path made."""
    if kind == "approved_action":
        return False
    if kind not in ("interactive", "mcp"):
        return True
    return bool(session_id) and any(session_id.startswith(prefix) for prefix in BACKGROUND_SESSION_PREFIXES)


@pytest.mark.parametrize("kind", CONTEXT_KINDS)
@pytest.mark.parametrize("session_id", SESSIONS)
def test_which_turns_the_guard_holds(kind, session_id):
    assert guarded_turn(kind, session_id) is _expected(kind, session_id)


def test_every_background_kind_is_guarded_and_chat_is_not():
    assert set(BACKGROUND_KINDS) == {
        "subtask",
        "dag_node",
        "scheduled",
        "agent_action",
        "heartbeat_triage",
        "heartbeat_check",
        "heartbeat_callback",
        "dag_summary",
        "background",
        "continuation",
    }
    for kind in BACKGROUND_KINDS:
        assert guarded_turn(kind, "telegram-42")
    assert not guarded_turn("interactive", "telegram-42")
    assert not guarded_turn("mcp", None)
    assert guarded_turn("interactive", "subtask-1")
    assert not guarded_turn("approved_action", "proposal-1")  # the owner approved that one call


def test_denies_only_a_switched_off_tool_in_a_guarded_turn():
    guard = ToolGuard(denied=frozenset({"web_search"}), locked=frozenset({"bash"}))
    assert guard.denies("subtask", "subtask-1", "web_search")
    assert guard.denies("scheduled", "schedule-1", "bash")
    assert not guard.denies("subtask", "subtask-1", "web_fetch")
    assert not guard.denies("interactive", "telegram-42", "web_search")
    assert not guard.denies("interactive", "telegram-42", "bash")
    assert not guard.denies("approved_action", "proposal-1", "web_search")
    assert guard.denies("interactive", "claude-job-1", "web_search")


def test_the_harness_own_tools_are_never_held():
    assert TERMINAL_EXTRA_TOOLS <= HARNESS_TOOLS
    assert tool_policy.INTERNAL_ONLY_EXTRA_TOOLS <= HARNESS_TOOLS
    guard = ToolGuard(denied=frozenset(HARNESS_TOOLS | {"web_search"}), locked=frozenset(HARNESS_TOOLS))
    assert guard.denied == frozenset({"web_search"}) and guard.locked == frozenset()
    for tool in HARNESS_TOOLS:
        assert not guard.denies("continuation", "intent-1", tool)


def test_install_and_current():
    assert tool_guard.current() is None
    guard = ToolGuard(denied=frozenset({"web_search"}))
    tool_guard.install(guard)
    assert tool_guard.current() is guard
    tool_guard.install(None)
    assert tool_guard.current() is None


def test_parse_locked_tools(caplog):
    caplog.set_level(logging.WARNING)
    assert parse_locked_tools("") == frozenset()
    assert parse_locked_tools(" bash , web_search,,bash ") == frozenset({"bash", "web_search"})
    assert not caplog.records
    assert parse_locked_tools("bash,resolve_intention,submit_final_report") == frozenset({"bash"})
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "NOUS_AUTONOMY_LOCKED_TOOLS" in warnings[0].getMessage()


def test_the_refusal_names_the_tool():
    assert tool_guard.refusal_text("web_search") == "Tool error: 'web_search' is switched off for background work."
