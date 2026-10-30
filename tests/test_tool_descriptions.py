"""The agent tools' descriptions follow one style and state the server's limits from the code.

A description says in plain words what the tool does and when to use another tool. It names no internal
term (an organ, a feature number, an env var, a table outside db_query), and a limit the server enforces is
built from the code constant, so the text cannot drift from the check.
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace
from typing import get_args
from unittest.mock import AsyncMock, MagicMock

import pytest

from nous.a2ui import tools as a2ui_tools
from nous.api import builtin_tools, web_tools
from nous.api.builtin_tools import register_builtin_tools
from nous.api.db_query import DB_QUERY_SCHEMA
from nous.api.telegram_tools import register_telegram_tools
from nous.api.tools import (
    _MAX_WRITES,
    ToolDispatcher,
    register_cache_retrieve_tool,
    register_dag_tools,
    register_heartbeat_tools,
    register_nous_tools,
    register_programmatic_tools,
    register_subtask_tools,
)
from nous.api.web_tools import register_web_tools
from nous.browser.tools import register_browser_tools
from nous.config import Settings
from nous.dag import schemas as dag_schemas
from nous.heartbeat.dynamic import ALLOWED_TOOLS, MIN_INTERVAL_SECONDS
from nous.skills.parser import SkillParser

ALL_ON = {
    "intentions_enabled": True,
    "browser_task_enabled": True,
    "dag_approval_nodes_enabled": True,
    "a2ui_enabled": True,
    "compensation_enabled": True,
    "subtask_payload_schema_enabled": True,
    "subtask_hardening_enabled": True,
}
# An organ, a feature or phase number, an env var, or a table: words the model cannot act on.
INTERNAL = re.compile(
    r"\bF\d{3}\b|\bNOUS_[A-Z_]+|\bHeart\b|\bBrain\b|\borgan\b|\bPhase \d|\b(?:heart|brain|nous_system)\.\w+"
)


def _tools(**over) -> dict[str, dict]:
    """Every agent tool's definition as the dispatcher serves it, registered the way main.py does."""
    settings = Settings(_env_file=None, **over)
    d = ToolDispatcher()
    register_nous_tools(d, MagicMock(), MagicMock(), settings=settings)
    register_builtin_tools(d, settings)
    register_web_tools(d, settings, MagicMock(), router=MagicMock(), bus=None)
    register_telegram_tools(d, settings, MagicMock())
    if settings.browser_task_enabled:
        register_browser_tools(d, settings, bus=None)
    register_cache_retrieve_tool(d, MagicMock())
    d.register("db_query", lambda **_: None, DB_QUERY_SCHEMA)
    register_subtask_tools(d, MagicMock(), settings, runner=object(), bus=None)
    register_programmatic_tools(d, MagicMock(), MagicMock(), settings, cognitive=None)
    register_heartbeat_tools(d, MagicMock())
    orchestrator = SimpleNamespace(clock_wired=True, approvals_wired=False, _settings=settings, start_dag=AsyncMock())
    register_dag_tools(d, MagicMock(), orchestrator, settings=settings)
    a2ui_tools.register_a2ui_tools(d, MagicMock(), brain=MagicMock(), dag_store=MagicMock(), composer=None)
    d.register("compose_surface", lambda **_: None, a2ui_tools._COMPOSE_SURFACE_SCHEMA)
    return {t["name"]: t for t in d.tool_definitions()}


@pytest.fixture(scope="module")
def tools() -> dict[str, dict]:
    return _tools(**ALL_ON)


@pytest.mark.parametrize("over", [{}, ALL_ON], ids=["defaults", "all_on"])
def test_no_description_names_an_internal_term(over):
    found = {}
    for name, definition in _tools(**over).items():
        text = json.dumps(definition)
        if name == "db_query":  # it reads tables by name: that is its job
            text = re.sub(r"\b(?:heart|brain|nous_system|public)\.\w+", "", text)
        hits = sorted({m.group(0) for m in INTERNAL.finditer(text)})
        if hits:
            found[name] = hits
    assert found == {}


def test_every_top_level_property_is_described(tools):
    missing = {
        name: [k for k, prop in d["input_schema"].get("properties", {}).items() if not prop.get("description")]
        for name, d in tools.items()
    }
    assert {name: keys for name, keys in missing.items() if keys} == {}


# -- the file and shell tools ---------------------------------------------------------------------------------


def test_bash_points_at_the_file_tools_and_states_the_compress_threshold(tools):
    text = tools["bash"]["description"]
    assert "read_file" in text and "write_file" in text and "cache_retrieve" in text
    assert f"{Settings.model_fields['smart_compress_min_chars'].default:,} characters" in text


def test_read_file_states_its_page_caps_from_the_constants(tools):
    text = tools["read_file"]["description"]
    assert "never compressed" in text and "cat" in text
    assert f"{builtin_tools._READ_FILE_MAX_LINES} lines" in text
    assert f"{builtin_tools._READ_FILE_MAX_CHARS:,} characters" in text
    assert f"{builtin_tools._MAX_FILE_SIZE // (1024 * 1024)} MB" in text


def test_write_file_replaces_heredocs(tools):
    text = tools["write_file"]["description"]
    assert "heredocs" in text and "workspace" in text and "reverted" in text


# -- web --------------------------------------------------------------------------------------------------------


def test_web_fetch_states_its_ceiling_from_the_constant(tools):
    schema = tools["web_fetch"]["input_schema"]["properties"]["max_chars"]
    assert schema["maximum"] == web_tools._WEB_FETCH_CEILING_CHARS
    assert f"{web_tools._WEB_FETCH_CEILING_CHARS:,}" in schema["description"]
    assert f"{Settings.model_fields['web_fetch_max_chars'].default:,}" in schema["description"]
    assert "web_fetch" in tools["web_search"]["description"] and "web_search" in tools["web_fetch"]["description"]


def test_web_fetch_names_browser_task_only_where_it_is_registered():
    assert "browser_task" in _tools(browser_task_enabled=True)["web_fetch"]["description"]
    without = _tools(browser_task_enabled=False)
    assert "browser_task" not in without and "browser_task" not in without["web_fetch"]["description"]


# -- memory -----------------------------------------------------------------------------------------------------


def test_the_memory_tools_say_when_to_use_another(tools):
    assert "recall_recent" in tools["recall_deep"]["description"]
    assert "recall_deep" in tools["recall_recent"]["description"]
    assert "recall_deep" in tools["recall_hubs"]["description"]
    assert "chatter" in tools["learn_fact"]["description"]
    assert "learn_fact" in tools["create_censor"]["description"]
    assert "alternatives" in tools["record_decision"]["description"]
    action = tools["create_censor"]["input_schema"]["properties"]["action"]["description"]
    assert all(word in action for word in ("steer", "refuse", "abort"))


def test_ingest_document_states_its_minimum_from_the_setting(tools):
    content = tools["ingest_document"]["input_schema"]["properties"]["content"]["description"]
    assert f"{Settings.model_fields['document_chunk_min_chars'].default} characters" in content


def test_the_learn_skill_format_is_the_one_the_parser_accepts(tools):
    text = tools["learn_skill"]["description"]
    for needed in ("'---'", "'name: ...'", "'description: ...'", "both required", "'domain: ...'", "'requires'"):
        assert needed in text, needed
    # The file the description spells out parses; one without a required key does not.
    manifest = SkillParser().parse(
        "---\nname: snow-report\ndescription: Read the snow report\ndomain: travel\n"
        "triggers: [snow, ski]\ntools:\n  - web_fetch\nrequires: []\n---\n\n## Steps\n1. Fetch the report.\n"
    )
    assert (manifest.name, manifest.domain, manifest.tools) == ("snow-report", "travel", ["web_fetch"])
    for key in ("name", "description"):
        bad = "---\nname: x\ndescription: y\n---\n".replace(f"{key}: ", "other: ")
        with pytest.raises(ValueError, match=key):
            SkillParser().parse(bad)


def test_run_python_states_its_limits_from_the_code(tools):
    text = tools["run_python"]["description"]
    assert f"at most {_MAX_WRITES}" in text
    assert f"{Settings.model_fields['programmatic_tools_timeout'].default} s" in text


# -- background work ----------------------------------------------------------------------------------------------


def test_the_background_family_says_when_to_pick_each(tools):
    assert "schedule_task" in tools["spawn_task"]["description"] and "dag_create" in tools["spawn_task"]["description"]
    assert "spawn_task" in tools["schedule_task"]["description"]
    assert "heartbeat_check_create" in tools["schedule_task"]["description"]
    assert "schedule_task" in tools["heartbeat_check_create"]["description"]
    assert "spawn_task" in tools["dag_create"]["description"]
    assert "cancel_task" in tools["list_tasks"]["description"]


def test_heartbeat_check_create_lists_the_allowed_tools_and_minimum_from_the_code(tools):
    props = tools["heartbeat_check_create"]["input_schema"]["properties"]
    assert ", ".join(sorted(ALLOWED_TOOLS)) in props["tools"]["description"]
    assert f"at least {MIN_INTERVAL_SECONDS}" in props["interval_seconds"]["description"]


def test_dag_create_states_its_limits_and_the_source_values(tools):
    schema = tools["dag_create"]["input_schema"]
    text = schema["description"]
    assert f"{dag_schemas.MAX_NODES} nodes" in text and f"{dag_schemas.MAX_WAVES} waves" in text
    assert f"{dag_schemas.MAX_PARALLEL_PER_WAVE} nodes per wave" in text
    source = schema["properties"]["source"]
    assert source["enum"] == list(get_args(dag_schemas.DAGCreateRequest.model_fields["source"].annotation))
    assert source["enum"] == ["conversation", "critic", "heartbeat", "schedule"]
    for prop in ("description", "nodes", "edges", "source", "token_budget"):
        assert schema["properties"][prop]["description"], prop
    edge_type = schema["properties"]["edges"]["items"]["properties"]["edge_type"]["description"]
    assert all(kind in edge_type for kind in ("dependency", "context_flow", "on_failure", "cancel_cascade"))


def test_dag_create_reads_its_limits_at_registration(monkeypatch):
    """The text follows the constants: change them and the description changes with them."""
    monkeypatch.setattr(dag_schemas, "MAX_NODES", 7)
    monkeypatch.setattr(dag_schemas, "MAX_WAVES", 3)
    monkeypatch.setattr(dag_schemas, "MAX_PARALLEL_PER_WAVE", 2)
    text = _tools()["dag_create"]["description"]
    assert "7 nodes, 3 waves" in text and "2 nodes per wave" in text


def test_dag_manage_describes_its_action_and_id(tools):
    props = tools["dag_manage"]["input_schema"]["properties"]
    assert all(action in props["action"]["description"] for action in props["action"]["enum"])
    assert "prefix" in props["dag_id"]["description"]
