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
    assert "not cut by SmartCompress" in text and "cat" in text
    assert f"{builtin_tools._READ_FILE_MAX_LINES} lines" in text
    assert f"{builtin_tools._READ_FILE_MAX_CHARS:,} characters" in text
    assert f"{builtin_tools._MAX_FILE_SIZE // (1024 * 1024)} MB" in text


async def test_read_file_states_its_refusals_as_the_handler_words_them(tmp_path):
    """A directory answers "Not a file" and a path outside the workspace is refused, as the description says."""
    text = _tools()["read_file"]["description"]
    assert "'Not a file'" in text and "bash ls" in text and "paths outside the workspace are refused" in text
    (tmp_path / "sub").mkdir()
    directory = await builtin_tools.read_file_tool("sub", _workspace_dir=str(tmp_path))
    assert directory.get("is_error") and "Not a file" in directory["content"][0]["text"]
    outside = await builtin_tools.read_file_tool(str(tmp_path.parent / "elsewhere.txt"), _workspace_dir=str(tmp_path))
    assert outside.get("is_error")


# -- the lineage rules (a continuation turn and the work it starts) ----------------------------------------------


def test_bash_names_the_lineage_allowlist_from_the_policy_tables():
    from nous.api.tool_policy import LINEAGE_BUILD_PROGRAMS, LINEAGE_READ_COMMANDS, LINEAGE_TURNS_TEXT

    text = _tools()["bash"]["description"]  # f099_lineage_shell and build commands default on
    assert f"{LINEAGE_TURNS_TEXT}, bash runs only allowlisted commands" in text
    assert all(f"{c}" in text for c in sorted(LINEAGE_READ_COMMANDS - {"cd", "echo", "pwd", "sleep", "true"}))
    assert "/".join(sorted(LINEAGE_BUILD_PROGRAMS)) in text and "runner.sh (not cancel)" in text
    no_builds = _tools(f099_lineage_build_commands=False)["bash"]["description"]
    assert LINEAGE_TURNS_TEXT in no_builds and "/".join(sorted(LINEAGE_BUILD_PROGRAMS)) not in no_builds
    # Without the lineage shell those turns are not offered bash, so the sentence is not there either.
    assert LINEAGE_TURNS_TEXT not in _tools(f099_lineage_shell=False)["bash"]["description"]


def test_write_file_names_the_lineage_directories_from_the_settings(tmp_path):
    from nous.api.tool_policy import LINEAGE_TURNS_TEXT

    text = _tools(workspace_dir=str(tmp_path))["write_file"]["description"]
    assert f"{LINEAGE_TURNS_TEXT}, write_file writes only under intentions/<root id>/ or claude-jobs/prompts/." in text
    extra = _tools(workspace_dir=str(tmp_path), f099_lineage_write_dirs=["research", "agent-notes"])
    assert (
        "intentions/<root id>/, claude-jobs/prompts/, research/ or agent-notes/." in extra["write_file"]["description"]
    )
    no_shell = _tools(workspace_dir=str(tmp_path), f099_lineage_shell=False)["write_file"]["description"]
    assert "write_file writes only under intentions/<root id>/." in no_shell and "claude-jobs" not in no_shell


def test_the_lineage_sentences_are_the_same_in_every_conversational_frame():
    """#528: they are decided at registration, so a frame change never changes the tool array's bytes."""
    settings = Settings(_env_file=None)
    d = ToolDispatcher(stable_tool_set_enabled=settings.stable_tool_set_enabled)
    register_builtin_tools(d, settings)
    frames = ("conversation", "task", "question", "decision", "debug", "creative")
    assert len({json.dumps(d.available_tools(frame)) for frame in frames}) == 1


def test_write_file_replaces_heredocs(tools):
    text = tools["write_file"]["description"]
    assert "heredocs" in text and "workspace" in text


@pytest.mark.parametrize(
    ("over", "revertible"),
    [
        ({}, False),  # the defaults, as prod runs: no snapshot, no review card
        ({"compensation_enabled": True}, False),  # snapshots, but no review card to revert from
        ({"compensation_enabled": True, "compensation_auto_review_enabled": True}, True),
    ],
    ids=["defaults", "compensation_only", "compensation_and_review"],
)
def test_write_file_promises_a_revert_only_where_the_review_card_can_do_it(over, revertible):
    text = _tools(**over)["write_file"]["description"]
    assert ("reverted from the action review" in text) is revertible


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
    # The node cap counts every node (schemas.py: len(self.nodes)); only the wave limits leave fix nodes out.
    assert f"{dag_schemas.MAX_NODES} nodes in all, fix nodes included;" in text
    assert f"{dag_schemas.MAX_WAVES} waves" in text
    assert f"{dag_schemas.MAX_PARALLEL_PER_WAVE} nodes per wave, fix nodes not counted." in text
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
    assert "7 nodes in all" in text and "3 waves" in text and "2 nodes per wave" in text


def test_the_caveats_the_code_needs_are_stated(tools):
    """A check disables itself only when its prompt says to; decision_sweep reads its own rows; the resolve
    tools say triage does not get them."""
    assert (
        "It can disable itself (its prompt calls heartbeat_check_manage"
        in tools["heartbeat_check_create"]["description"]
    )
    template = tools["push_surface"]["input_schema"]["properties"]["template"]["description"]
    assert "decision rows in params are ignored" in template
    for name in ("resolve_decision", "resolve_decisions"):
        assert tools[name]["description"].endswith("Not available in heartbeat triage turns."), name


def _dag_create(**over) -> dict:
    return _tools(**over)["dag_create"]["input_schema"]


@pytest.mark.parametrize("on", [False, True], ids=["off", "on"])
def test_dag_create_says_what_this_server_does_with_callbacks(on):
    schema = _dag_create(dag_callback_execution_enabled=on, dag_approval_nodes_enabled=True, a2ui_enabled=True)
    text = schema["properties"]["nodes"]["items"]["properties"]["type"]["description"]
    assert ("a callback completes at once without running" in text) is not on
    assert ("a callback runs nothing here" in text) is not on  # the approval wiring note
    assert "frame_type / model / timeout_seconds" in text


@pytest.mark.parametrize("on", [False, True], ids=["off", "on"])
def test_fix_actions_say_whether_an_amended_retry_amends(on):
    node = _dag_create(dag_fix_llm_dispatch_enabled=on)["properties"]["nodes"]["items"]["properties"]
    text = node["fix_actions"]["description"]
    assert ("or with amended instructions" in text) is on
    assert ("an amended retry also retries as is on this server" in text) is not on


@pytest.mark.parametrize("on", [False, True], ids=["off", "on"])
def test_token_budget_says_whether_it_is_enforced(on):
    text = _dag_create(dag_token_budget_enforcement_enabled=on)["properties"]["token_budget"]["description"]
    assert ("nodes that have not started are cancelled" in text) is on
    assert ("tracked and reported, not enforced" in text) is not on


def test_dag_create_and_its_edges_say_what_the_orchestrator_does(tools):
    schema = tools["dag_create"]["input_schema"]
    one_task = "use spawn_task, unless it must wait for a Claude Code job, a PR or a file (then a DAG with a wait node)"
    assert one_task in schema["description"]
    edge_type = schema["properties"]["edges"]["items"]["properties"]["edge_type"]["description"]
    # orchestrator._propagate_failures: a failed predecessor blocks its dependency successors, after a fix had its go
    assert "if from_node fails and no fix node recovers it, to_node and all below it are blocked" in edge_type
    action = tools["dag_manage"]["input_schema"]["properties"]["action"]["description"]
    # orchestrator._cancel_node: subtask rows are cancelled, checks disabled; runner.sh jobs are not touched
    assert "a running one is not interrupted" in action and "a check node's check is disabled" in action
    assert "a Claude Code job a node launched keeps running" in action


def test_dag_manage_describes_its_action_and_id(tools):
    props = tools["dag_manage"]["input_schema"]["properties"]
    assert all(action in props["action"]["description"] for action in props["action"]["enum"])
    assert "prefix" in props["dag_id"]["description"]
