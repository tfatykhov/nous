"""F115: the tool guard's snapshot, which turns each of its three sources holds, and the host's locked tools (no
database)."""

from __future__ import annotations

import logging

import pytest

from nous.api import tool_policy
from nous.api.execution_context import BACKGROUND_SESSION_PREFIXES, CONTEXT_KINDS, FOREGROUND_KINDS
from nous.api.runner import TERMINAL_EXTRA_TOOLS
from nous.autonomy import tool_guard
from nous.autonomy.tool_guard import HARNESS_TOOLS, LOCK, NOT_READ, OWNER, ToolGuard, parse_locked_tools

BACKGROUND_KINDS = [
    kind for kind in CONTEXT_KINDS if kind not in FOREGROUND_KINDS and kind not in ("approved_action", "agent_action")
]
SESSIONS = [None, "", "telegram-42", "abc", *(prefix + "x" for prefix in BACKGROUND_SESSION_PREFIXES)]
# A guard in which only one source holds ``web_search``.
ONE_SOURCE = {
    OWNER: ToolGuard(denied=frozenset({"web_search"})),
    LOCK: ToolGuard(locked=frozenset({"web_search"})),
    NOT_READ: ToolGuard(hold_all=True),
}


def _expected(source: str, kind: str, session_id: str | None) -> bool:
    """The rule, written out. A conversation kind (chat, an inbound MCP call) is held by every source in a session a
    background path made and by none elsewhere. A companion tap is held by a host lock and while the settings could
    not be read, never by the owner's setting; an approved action only by a host lock. Every other kind is background
    work, held by all three."""
    if kind in ("interactive", "mcp"):
        return bool(session_id) and any(session_id.startswith(prefix) for prefix in BACKGROUND_SESSION_PREFIXES)
    if kind == "agent_action":
        return source in (LOCK, NOT_READ)
    if kind == "approved_action":
        return source == LOCK
    return True


@pytest.mark.parametrize("source", [OWNER, LOCK, NOT_READ])
@pytest.mark.parametrize("kind", CONTEXT_KINDS)
@pytest.mark.parametrize("session_id", SESSIONS)
def test_which_turns_each_source_holds(source, kind, session_id):
    guard = ONE_SOURCE[source]
    held = _expected(source, kind, session_id)
    assert guard.denies(kind, session_id, "web_search") is held
    assert guard.reason(kind, session_id, "web_search") == (source if held else None)
    assert (source in tool_guard.held_sources(kind, session_id)) is held


def test_every_background_kind_is_held_and_chat_is_not():
    assert set(BACKGROUND_KINDS) == {
        "subtask",
        "dag_node",
        "scheduled",
        "heartbeat_triage",
        "heartbeat_check",
        "heartbeat_callback",
        "dag_summary",
        "background",
        "continuation",
    }
    for guard in ONE_SOURCE.values():
        for kind in BACKGROUND_KINDS:
            assert guard.denies(kind, "telegram-42", "web_search")
        assert not guard.denies("interactive", "telegram-42", "web_search")
        assert not guard.denies("mcp", None, "web_search")
        assert guard.denies("interactive", "subtask-1", "web_search")
        assert guard.denies("mcp", "claude-job-1", "web_search")


def test_a_companion_tap_is_held_by_a_lock_and_while_unread_never_by_the_owner():
    tap = ("agent_action", "subtask-1")  # a background session prefix: the kind decides
    assert not ONE_SOURCE[OWNER].denies(*tap, "web_search")
    assert ONE_SOURCE[LOCK].denies(*tap, "web_search")
    assert ONE_SOURCE[NOT_READ].denies(*tap, "web_search")


def test_an_approved_action_is_held_by_a_lock_only():
    approved = ("approved_action", "proposal-1")
    assert not ONE_SOURCE[OWNER].denies(*approved, "web_search")
    assert ONE_SOURCE[LOCK].denies(*approved, "web_search")
    assert not ONE_SOURCE[NOT_READ].denies(*approved, "web_search")


def test_a_lock_is_named_before_the_owner_setting():
    guard = ToolGuard(denied=frozenset({"bash", "web_search"}), locked=frozenset({"bash"}))
    assert guard.reason("subtask", "subtask-1", "bash") == LOCK
    assert guard.reason("subtask", "subtask-1", "web_search") == OWNER
    assert guard.reason("subtask", "subtask-1", "web_fetch") is None
    assert guard.reason("agent_action", "s", "bash") == LOCK
    assert guard.reason("agent_action", "s", "web_search") is None
    assert ToolGuard(locked=frozenset({"bash"}), hold_all=True).reason("subtask", "s", "bash") == LOCK
    assert ToolGuard(locked=frozenset({"bash"}), hold_all=True).reason("subtask", "s", "web_search") == NOT_READ


def test_the_harness_own_tools_are_never_held():
    assert TERMINAL_EXTRA_TOOLS <= HARNESS_TOOLS
    assert tool_policy.INTERNAL_ONLY_EXTRA_TOOLS <= HARNESS_TOOLS
    guard = ToolGuard(denied=frozenset(HARNESS_TOOLS | {"web_search"}), locked=frozenset(HARNESS_TOOLS))
    assert guard.denied == frozenset({"web_search"}) and guard.locked == frozenset()
    for tool in HARNESS_TOOLS:
        assert not guard.denies("continuation", "intent-1", tool)
        assert not ToolGuard(hold_all=True).denies("continuation", "intent-1", tool)
        assert not ToolGuard(hold_all=True).denies("agent_action", "s", tool)


def test_holding_every_tool_holds_any_tool_name():
    guard = ToolGuard(hold_all=True)
    for tool in ("web_search", "bash", "read_file", "mcp__notes__search", "a_tool_added_later"):
        assert guard.denies("subtask", "subtask-1", tool)
        assert guard.denies("interactive", "claude-job-1", tool)
        assert guard.denies("agent_action", "telegram-1", tool)
        assert not guard.denies("interactive", "telegram-1", tool)
        assert not guard.denies("approved_action", "proposal-1", tool)


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


def test_the_refusal_names_the_tool_and_why():
    assert ToolGuard.refusal("web_search", OWNER) == "Tool error: 'web_search' is switched off for background work."
    assert ToolGuard.refusal("bash", LOCK) == "Tool error: 'bash' is locked by the server."
    assert ToolGuard.refusal("web_search", NOT_READ) == (
        "Tool error: the saved tool settings could not be read, so 'web_search' is held until they can."
    )
    assert (OWNER, LOCK, NOT_READ) == ("owner", "server", "not_read")
    assert tool_guard.REFUSAL_EVENT == "harness_tool_guard_refusal"


def test_the_table_names_every_kind_and_the_screen_lists_are_what_the_guard_holds():
    assert set(tool_guard.KINDS) == set(CONTEXT_KINDS)
    in_background_session = "a chat or MCP turn in a session background work started"
    words = {kind: text for kind, (text, _sources) in tool_guard.KINDS.items()}
    for source, guard in ONE_SOURCE.items():
        held = [words[kind] for kind in tool_guard.KINDS if guard.denies(kind, None, "web_search")]
        not_held = [words[kind] for kind in tool_guard.KINDS if not guard.denies(kind, None, "web_search")]
        assert tool_guard.places(source) == [*held, in_background_session], source
        assert tool_guard.places(source, held=False) == not_held, source
    assert tool_guard.places(OWNER, held=False) == [
        "chat in an ordinary session",
        "an MCP call from another agent in an ordinary session",
        "a tap on a companion card",
        "an action you approved",
    ]
    assert tool_guard.also_held(LOCK) == ["a tap on a companion card", "an action you approved"]
    assert tool_guard.also_held(NOT_READ) == ["a tap on a companion card"]
    assert tool_guard.also_held(OWNER) == []


def test_list_words():
    assert tool_guard.list_words([]) == ""
    assert tool_guard.list_words(["a"]) == "a"
    assert tool_guard.list_words(["a", "b"]) == "a and b"
    assert tool_guard.list_words(["a", "b", "c"]) == "a, b, and c"
