"""Per-goal tool grants on the Limits and tools screen: ``why_not_offered`` says a chain may be allowed a tool for one
goal only where that is so (a chain place, a tool a goal may be given, and an MCP server available to background
work), and with the flag off it says exactly what it said before; the screen's rows say it only for a tool the grant
route would accept now."""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest
from goals_support import GOALS
from sqlalchemy import delete
from starlette.applications import Starlette
from test_autonomy_routes import OWNER, _send, _settings

from nous.api import tool_classes, tool_policy
from nous.api.autonomy_routes import build_autonomy_routes
from nous.api.owner_guard import OwnerGuard
from nous.api.tool_classes import TOOL_CLASSES, McpIdentity
from nous.api.tools import ToolDispatcher
from nous.autonomy.offered import CHAIN_TOOLS_GRANT_CLAUSE, CHAIN_TOOLS_WORDS, PLACES, why_not_offered
from nous.autonomy.store import AutonomyStore
from nous.storage.models import AutonomySetting, AutonomySettingLog, ToolGuardRow

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


# ---- the screen's rows: only where the grant route would accept the tool now -----------------------------------

BARE = "mcp__bare__thing"  # registered with the MCP client, with no identity
CHAIN_PLACES = ("chain_decisions", "chain_work")
WITH_CLAUSE = CHAIN_TOOLS_WORDS + CHAIN_TOOLS_GRANT_CLAUSE


def _chain_sentences(rows, name):
    return [item["why"] for item in rows[name]["not_offered"] if item["place"] in CHAIN_PLACES]


@pytest.mark.postgres_only
async def test_the_screen_says_a_goal_can_be_allowed_a_tool_only_where_the_grant_route_would_accept_it(db, monkeypatch):
    agent = f"grant-offered-{uuid.uuid4().hex[:8]}"
    notes = McpIdentity(uuid.uuid4(), "a1" * 32, "search", server_name="notes", server_host="notes.example.com")
    mail = McpIdentity(uuid.uuid4(), "b2" * 32, "send", server_name="mail", server_host="mail.example.com")
    monkeypatch.setattr(tool_classes, "_MCP_TOOLS", {MCP_ON: True, MCP_OFF: False, BARE: True})
    monkeypatch.setattr(tool_classes, "_MCP_IDENTITIES", {MCP_ON: notes, MCP_OFF: mail})
    settings = _settings(
        agent,
        **GOALS,
        goal_tool_grants_enabled=True,
        autonomy_locked_tools="send_file",
        email_allowlist="",
        email_allowlist_file="",
        telegram_chat_id="4242",
    )
    assert settings.goal_tool_grants_enabled
    dispatcher = ToolDispatcher()
    for name in ("send_email", "send_file", "browser_task", "web_search", MCP_ON, MCP_OFF, BARE):
        dispatcher.register(name, MagicMock(), {"type": "object", "description": name, "properties": {}})
    store = AutonomyStore(db, settings)
    await store.load()
    try:
        await store.set_tool("browser_task", "denied", "owner")
        routes = build_autonomy_routes(
            database=db, settings=settings, store=store, dispatcher=dispatcher, guard=OwnerGuard(settings)
        )
        app = Starlette(routes=routes)

        async def rows():
            body = (await _send(app, "GET", "/autonomy/tools", headers=OWNER)).json()
            return {row["name"]: row for row in body["tools"]}

        listed = await rows()
        assert _chain_sentences(listed, MCP_ON) == [WITH_CLAUSE, WITH_CLAUSE]  # the grant route accepts it
        # Locked by the host, switched off by the owner, the allowlist empty, a server not available to background
        # work, a server whose identity is not known: the route refuses each, so the screen does not offer it.
        for name in ("send_file", "browser_task", "send_email", MCP_OFF, BARE):
            sentences = _chain_sentences(listed, name)
            assert sentences and all(not why.endswith(CHAIN_TOOLS_GRANT_CLAUSE) for why in sentences), name
        object.__setattr__(settings, "email_allowlist", "friend@example.com")
        assert _chain_sentences(await rows(), "send_email") == [WITH_CLAUSE, WITH_CLAUSE]
        object.__setattr__(settings, "goal_tool_grants_enabled", False)
        assert _chain_sentences(await rows(), MCP_ON) == [CHAIN_TOOLS_WORDS, CHAIN_TOOLS_WORDS]
    finally:
        async with db.session() as session:
            for model in (AutonomySetting, AutonomySettingLog, ToolGuardRow):
                await session.execute(delete(model).where(model.agent_id == agent))
            await session.commit()
