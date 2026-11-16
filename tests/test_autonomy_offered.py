"""F115: ``why_not_offered`` says exactly what ``AgentRunner._offered_tools`` does for each kind of background work,
over every classified tool and an MCP tool of a server available to background work and of one that is not."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from nous.api import tool_classes
from nous.api.runner import AgentRunner
from nous.api.tool_classes import TOOL_CLASSES
from nous.api.tools import ToolDispatcher
from nous.autonomy.offered import PLACES, context_for, why_not_offered
from nous.config import Settings

MCP_ON = "mcp__notes__search"
MCP_OFF = "mcp__mail__send"
MCP_GONE = "mcp__gone__thing"


@pytest.fixture
def runner(monkeypatch):
    monkeypatch.setattr(tool_classes, "_MCP_TOOLS", {MCP_ON: True, MCP_OFF: False})
    dispatcher = ToolDispatcher()
    for name in [*TOOL_CLASSES, MCP_ON, MCP_OFF, MCP_GONE]:
        dispatcher.register(name, MagicMock(), {"type": "object", "properties": {}})
    settings = Settings(_env_file=None, ANTHROPIC_API_KEY="test-key")
    runner = AgentRunner(MagicMock(), None, MagicMock(), settings)
    runner.set_dispatcher(dispatcher)
    return runner, dispatcher, settings


@pytest.mark.parametrize("place", PLACES, ids=lambda place: place.key)
@pytest.mark.parametrize("lineage_shell", [True, False])
def test_the_reasons_match_the_runners_offered_set(runner, place, lineage_shell):
    runner, dispatcher, settings = runner
    object.__setattr__(settings, "f099_lineage_shell", lineage_shell)
    offered = {
        tool["name"]
        for tool in runner._offered_tools(
            context_for(place), "task", is_subtask=place.is_subtask, tool_filter=None, refuse_active=False
        )
    }
    listed = {tool["name"] for tool in dispatcher.available_tools("task")}
    said = {name for name in listed if why_not_offered(place, name, lineage_shell=lineage_shell) is None}
    assert said == offered
    assert offered  # each place is offered something


def test_the_places_and_some_reasons():
    assert [place.key for place in PLACES] == [
        "chain_decisions",
        "chain_work",
        "tasks",
        "workflow_steps",
        "schedules",
        "checks",
        "triage",
    ]
    tasks, triage, chain_work = PLACES[2], PLACES[6], PLACES[1]
    assert why_not_offered(tasks, "spawn_task", lineage_shell=True).startswith("Work started in the background")
    assert why_not_offered(triage, "dag_create", lineage_shell=True).startswith("Triage may not")
    assert why_not_offered(chain_work, "send_email", lineage_shell=True).startswith("A chain keeps")
    assert why_not_offered(tasks, "web_search", lineage_shell=True) is None
