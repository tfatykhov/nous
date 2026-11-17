"""A goal that may run any command, on the Limits and tools screen: ``why_not_offered`` adds its clause only to the
chain sentence about bash in a place the mode reaches, and with the flag off says exactly what it said before; the
screen's rows say it only with the flag on."""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest
from goals_support import GOALS
from sqlalchemy import delete
from starlette.applications import Starlette
from test_autonomy_routes import OWNER, _send, _settings
from test_goal_tool_grants_offered import NAMES

from nous.api import tool_classes, tool_policy
from nous.api.autonomy_routes import build_autonomy_routes
from nous.api.owner_guard import OwnerGuard
from nous.api.tools import ToolDispatcher
from nous.autonomy.offered import CHAIN_TOOLS_COMMANDS_CLAUSE, CHAIN_TOOLS_WORDS, PLACES, why_not_offered
from nous.autonomy.store import AutonomyStore
from nous.storage.models import AutonomySetting, AutonomySettingLog, ToolGuardRow


@pytest.fixture(autouse=True)
def mcp_registry(monkeypatch):
    monkeypatch.setattr(tool_classes, "_MCP_TOOLS", {"mcp__notes__search": True, "mcp__mail__send": False})


def test_the_clause_says_what_the_mode_gives():
    assert CHAIN_TOOLS_COMMANDS_CLAUSE == (
        " A goal created to run any command gets the shell without the command list while that holds."
    )


@pytest.mark.parametrize("lineage_shell", [True, False])
@pytest.mark.parametrize("goal_grants", [True, False])
def test_off_says_what_it_said_before(lineage_shell, goal_grants):
    for place in PLACES:
        for name in NAMES:
            before = why_not_offered(place, name, lineage_shell=lineage_shell, goal_grants=goal_grants)
            off = why_not_offered(
                place, name, lineage_shell=lineage_shell, goal_grants=goal_grants, goal_commands=False
            )
            assert off == before, (place.key, name)


@pytest.mark.parametrize("goal_grants", [True, False])
def test_on_adds_the_clause_to_bash_in_chain_work_only(goal_grants):
    changed = set()
    for lineage_shell in (True, False):
        for place in PLACES:
            for name in NAMES:
                off = why_not_offered(place, name, lineage_shell=lineage_shell, goal_grants=goal_grants)
                on = why_not_offered(
                    place, name, lineage_shell=lineage_shell, goal_grants=goal_grants, goal_commands=True
                )
                if on != off:
                    assert (off, on) == (CHAIN_TOOLS_WORDS, CHAIN_TOOLS_WORDS + CHAIN_TOOLS_COMMANDS_CLAUSE)
                    changed.add((place.key, name, lineage_shell))
    # With the lineage shell on, a chain's work is offered bash already: there is no sentence to add to.
    assert changed == {("chain_work", "bash", False)}
    chain_work = next(place for place in PLACES if place.key == "chain_work")
    assert chain_work.kind in tool_policy.COMMANDS_KINDS and chain_work.authority == "internal_only"


@pytest.mark.postgres_only
async def test_the_screen_says_it_only_with_the_flag_on(db):
    agent = f"commands-offered-{uuid.uuid4().hex[:8]}"
    settings = _settings(
        agent,
        **GOALS,
        goal_tool_grants_enabled=True,
        owner_entry_mode="enforce",
        goal_commands_enabled=True,
        f099_lineage_shell=False,
    )
    assert settings.goal_commands_enabled
    dispatcher = ToolDispatcher()
    for name in ("bash", "web_search"):
        dispatcher.register(name, MagicMock(), {"type": "object", "description": name, "properties": {}})
    store = AutonomyStore(db, settings)
    await store.load()
    try:
        routes = build_autonomy_routes(
            database=db, settings=settings, store=store, dispatcher=dispatcher, guard=OwnerGuard(settings)
        )
        app = Starlette(routes=routes)

        async def bash_rows():
            body = (await _send(app, "GET", "/autonomy/tools", headers=OWNER)).json()
            [row] = [row for row in body["tools"] if row["name"] == "bash"]
            return {item["place"]: item["why"] for item in row["not_offered"]}

        on = await bash_rows()
        assert on["chain_work"] == CHAIN_TOOLS_WORDS + CHAIN_TOOLS_COMMANDS_CLAUSE
        assert on["chain_decisions"] == CHAIN_TOOLS_WORDS
        object.__setattr__(settings, "goal_commands_enabled", False)
        off = await bash_rows()
        assert off["chain_work"] == CHAIN_TOOLS_WORDS and off["chain_decisions"] == CHAIN_TOOLS_WORDS
        assert set(on) == set(off)
    finally:
        async with db.session() as session:
            for model in (AutonomySetting, AutonomySettingLog, ToolGuardRow):
                await session.execute(delete(model).where(model.agent_id == agent))
            await session.commit()
