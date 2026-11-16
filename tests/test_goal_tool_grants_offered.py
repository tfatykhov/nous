"""Per-goal tool grants on the Limits and tools screen: ``why_not_offered`` says a chain may be allowed a tool for one
goal only where that is so (a chain place, a tool a goal may be given, and an MCP server available to background
work), and with the flag off it says exactly what it said before."""

from __future__ import annotations

import pytest

from nous.api import tool_classes, tool_policy
from nous.api.tool_classes import TOOL_CLASSES
from nous.autonomy.offered import CHAIN_TOOLS_GRANT_CLAUSE, CHAIN_TOOLS_WORDS, PLACES, why_not_offered

MCP_ON = "mcp__notes__search"
MCP_OFF = "mcp__mail__send"
MCP_GONE = "mcp__gone__thing"
NAMES = (*sorted(TOOL_CLASSES), "not_a_tool", MCP_ON, MCP_OFF, MCP_GONE)
# The sentences why_not_offered could say before per-goal tools.
BEFORE = {
    "Work started in the background may not start more work this way.",
    "Triage may not start work or resolve decisions.",
    "A chain keeps to a fixed set of tools that act inside Nous.",
    "Its server is not connected.",
    "Its server is not available to background work.",
}


@pytest.fixture(autouse=True)
def mcp_registry(monkeypatch):
    monkeypatch.setattr(tool_classes, "_MCP_TOOLS", {MCP_ON: True, MCP_OFF: False})


def test_the_chain_sentence_is_unchanged():
    assert CHAIN_TOOLS_WORDS == "A chain keeps to a fixed set of tools that act inside Nous."


@pytest.mark.parametrize("lineage_shell", [True, False])
def test_off_says_what_it_said_before(lineage_shell):
    for place in PLACES:
        for name in NAMES:
            default = why_not_offered(place, name, lineage_shell=lineage_shell)
            assert default == why_not_offered(place, name, lineage_shell=lineage_shell, goal_grants=False)
            assert default is None or default in BEFORE, (place.key, name)


@pytest.mark.parametrize("lineage_shell", [True, False])
def test_on_adds_the_clause_only_where_a_goal_can_be_allowed_the_tool(lineage_shell):
    changed = set()
    for place in PLACES:
        for name in NAMES:
            off = why_not_offered(place, name, lineage_shell=lineage_shell)
            on = why_not_offered(place, name, lineage_shell=lineage_shell, goal_grants=True)
            if on != off:
                assert on == off + CHAIN_TOOLS_GRANT_CLAUSE and off == CHAIN_TOOLS_WORDS, (place.key, name)
                changed.add((place.key, name))
    expected = {
        (place.key, name)
        for place in PLACES
        if place.authority == "internal_only" and place.kind in tool_policy.GRANT_KINDS
        for name in ("send_email", "send_file", "browser_task", MCP_ON)
    }
    assert changed == expected
    assert {key for key, _name in changed} == {"chain_decisions", "chain_work"}
