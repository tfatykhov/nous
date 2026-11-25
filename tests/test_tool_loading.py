"""F120 tool loading, watch only: the pure parts (nous/api/tool_loading.py). No database.

The kinds-of-call table against every execution-context kind; the turns never watched; the pinned set against the
code it is derived from (the harness's own tools, a forced tool, every tool a prompt names in backticks); the split
and the per-call record; the list of names built for its size only; the stored value's parser, which never raises.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from uuid import uuid4

import pytest

from nous.api.execution_context import CONTEXT_KINDS, ExecutionContext
from nous.api.runner import TERMINAL_EXTRA_TOOLS
from nous.api.tool_classes import TOOL_CLASSES
from nous.api.tool_loading import (
    DEFAULT_CORE,
    INDEX_LINE_CHARS,
    KIND_OF_CONTEXT,
    KINDS,
    OFF,
    PINNED,
    ToolLoadingConfig,
    call_record,
    config_key,
    first_sentence,
    index_text,
    index_tokens,
    kind_of_turn,
    parse_tool_loading,
    split,
    tokens_est,
)
from nous.api.tool_policy import INTERNAL_ONLY_EXTRA_TOOLS
from nous.api.tools import _INITIATION_ONLY_TOOLS
from nous.autonomy.tool_guard import HARNESS_TOOLS
from nous.brain.intentions import AUTHORITY_INTERNAL
from nous.observability.model_calls import ContextStats, _tool_loading_value

ROOT = Path(__file__).resolve().parents[1]
WATCH_ALL = ToolLoadingConfig(mode="watch", kinds=frozenset(KINDS))


def _tool(name: str, description: str = "Does a thing.") -> dict:
    return {"name": name, "description": description, "input_schema": {"type": "object", "properties": {}}}


# ---- the kinds of call --------------------------------------------------------------------------------------------


def test_every_execution_context_kind_has_a_kind_of_call_or_none():
    """A kind added to ExecutionContext fails here until it is placed."""
    assert set(KIND_OF_CONTEXT) == set(CONTEXT_KINDS)
    assert {kind for kind in KIND_OF_CONTEXT.values() if kind is not None} == set(KINDS)


def test_chain_decisions_and_approved_actions_are_never_watched():
    assert KIND_OF_CONTEXT["continuation"] is None and KIND_OF_CONTEXT["approved_action"] is None


@pytest.mark.parametrize("kind", [k for k in CONTEXT_KINDS if k not in ("continuation", "approved_action")])
def test_a_turn_of_owner_authority_has_its_kind_of_call(kind):
    ctx = ExecutionContext(kind=kind, session_id="s")
    assert kind_of_turn(ctx, "task", None) == KIND_OF_CONTEXT[kind]


@pytest.mark.parametrize("kind", ["subtask", "dag_node", "interactive"])
def test_a_turn_of_internal_authority_is_never_watched(kind):
    ctx = ExecutionContext(kind=kind, session_id="s", authority=AUTHORITY_INTERNAL)
    assert kind_of_turn(ctx, "task", None) is None


def test_a_continuation_turn_is_never_watched():
    ctx = ExecutionContext(
        kind="continuation",
        session_id="intent-1",
        authority=AUTHORITY_INTERNAL,
        intention_id=uuid4(),
        root_intention_id=uuid4(),
    )
    assert kind_of_turn(ctx, "task", None) is None


def test_a_turn_with_its_own_tool_filter_and_the_initiation_frame_are_never_watched():
    ctx = ExecutionContext(kind="heartbeat_check", session_id="dynamic-check-1")
    assert kind_of_turn(ctx, "task", ["web_fetch"]) is None
    assert kind_of_turn(ctx, "task", []) is None  # an empty filter is a filter too
    assert kind_of_turn(ExecutionContext(kind="interactive"), "initiation", None) is None


# ---- the pinned set -----------------------------------------------------------------------------------------------


def test_the_harness_tools_are_pinned():
    assert HARNESS_TOOLS <= set(PINNED)
    assert TERMINAL_EXTRA_TOOLS <= set(PINNED)
    assert INTERNAL_ONLY_EXTRA_TOOLS <= set(PINNED)
    assert "commit" in PINNED
    assert all(isinstance(why, str) and why.endswith(".") for why in PINNED.values())


def _prompt_strings(path: Path) -> list[tuple[str, int]]:
    """Every string constant of ``path`` that is not a docstring, with its line."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
    return [
        (node.value, node.lineno)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings
    ]


def test_every_tool_a_prompt_names_is_pinned():
    """A tool named in single backticks in any string Nous builds (a prompt, an instruction) is one the model is told
    to call; it must be kept in full. The initiation frame is never watched, so its own tools need no pin."""
    named = re.compile(r"(?<!`)`([a-z_][a-z0-9_]*)`(?!`)")
    found: dict[str, str] = {}
    for path in (ROOT / "nous").rglob("*.py"):
        for value, line in _prompt_strings(path):
            for name in named.findall(value):
                if name in TOOL_CLASSES:
                    found.setdefault(name, f"{path.relative_to(ROOT)}:{line}")
    assert {"record_decision", "get_procedure", "recall_deep", "bash"} <= set(found)  # the scan reaches the prompts
    missing = {
        name: where for name, where in found.items() if name not in PINNED and name not in _INITIATION_ONLY_TOOLS
    }
    assert not missing, f"a prompt names a tool that is not pinned: {missing}"


def test_every_tool_a_harness_step_forces_is_pinned():
    """The forced tool_choice comes from ``force_tool_on_penultimate``: every string a caller passes or assigns for it
    is pinned."""
    forced: set[str] = set()
    for path in (ROOT / "nous").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            values = []
            if isinstance(node, ast.keyword) and node.arg == "force_tool_on_penultimate":
                values.append(node.value)
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and "force_tool" in t.id for t in node.targets
            ):
                values.append(node.value)
            for value in values:
                forced |= {c.value for c in ast.walk(value) if isinstance(c, ast.Constant) and isinstance(c.value, str)}
    assert "submit_final_report" in forced  # the scan reaches the subtask executor
    assert forced <= set(PINNED)


def test_the_default_core_set_is_registered_tool_names():
    assert DEFAULT_CORE and all(name in TOOL_CLASSES for name in DEFAULT_CORE)
    assert OFF.core is None and OFF.core_names() == DEFAULT_CORE


# ---- the split and the record -------------------------------------------------------------------------------------

TOOLS = [
    _tool("bash"),
    _tool("send_email", "Send an email from the agent's address. It goes out at once."),
    _tool("db_query"),
    _tool("dag_create"),
    _tool("mcp__ha__HassTurnOn", "[ha] Turn on a device."),
    _tool("submit_final_report"),
]


def test_the_split_keeps_the_core_set_and_the_pinned_tools_in_their_order():
    kept, left_out = split(TOOLS, ToolLoadingConfig(core=("dag_create",)))
    assert kept == ["bash", "dag_create", "submit_final_report"]  # bash and the report are pinned
    assert [t["name"] for t in left_out] == ["send_email", "db_query", "mcp__ha__HassTurnOn"]


def test_a_pinned_tool_cannot_be_left_out_by_the_core_set():
    kept, _ = split(TOOLS, ToolLoadingConfig(core=()))
    assert {"bash", "submit_final_report"} <= set(kept)


def test_the_split_reads_the_tools_only():
    before = json.dumps(TOOLS)
    split(TOOLS, OFF)
    call_record(WATCH_ALL, "chat", TOOLS)
    assert json.dumps(TOOLS) == before


def test_the_record_says_what_would_be_left_out():
    record = call_record(WATCH_ALL, "chat", TOOLS)
    left_out = [t for t in TOOLS if t["name"] in ("send_email", "dag_create", "mcp__ha__HassTurnOn")]
    assert record == {
        "mode": "watch",
        "kind": "chat",
        "sent": 6,
        "deferred": 3,
        "deferred_tokens_est": sum(tokens_est(t) for t in left_out),
        "index_tokens_est": index_tokens(left_out),
        "kept": ["bash", "db_query", "submit_final_report"],
    }
    assert json.loads(json.dumps(record)) == record  # stored as JSON


@pytest.mark.parametrize(
    ("config", "kind", "tools"),
    [
        (OFF, "chat", TOOLS),
        (ToolLoadingConfig(mode="off", kinds=frozenset(KINDS)), "chat", TOOLS),  # kinds on, mode off
        (ToolLoadingConfig(mode="watch", kinds=frozenset({"subtask"})), "chat", TOOLS),  # this kind off
        (WATCH_ALL, None, TOOLS),  # a turn never watched
        (WATCH_ALL, "chat", []),
        (WATCH_ALL, "chat", None),
    ],
)
def test_no_record_unless_watch_only_for_a_switched_on_kind(config, kind, tools):
    assert call_record(config, kind, tools) is None


def test_the_cache_state_is_set_from_the_calls_usage():
    stats = ContextStats({"tools_definition": 1000}, 0, 0, 0.0, [], 1, ["bash"], 0, {}, tool_loading={"kind": "chat"})
    assert _tool_loading_value(stats, {"cache_read_input_tokens": 1000})["cache"] == "read"
    assert _tool_loading_value(stats, {"cache_read_input_tokens": 999})["cache"] == "written"
    assert _tool_loading_value(stats, {"cache_read_input_tokens": 0})["cache"] == "written"
    assert _tool_loading_value(stats, {})["cache"] is None  # no usage: not weighed
    bare = ContextStats({"tools_definition": 1000}, 0, 0, 0.0, [], 1, ["bash"], 0, {})
    assert _tool_loading_value(bare, {"cache_read_input_tokens": 1000}) is None


# ---- the list of names (its size only) -----------------------------------------------------------------------------


def test_each_line_of_the_list_is_short_and_not_empty():
    long = _tool("long_tool", "word " * 60 + ". Second sentence.")
    text = index_text([long, *TOOLS])
    lines = [line for line in text.splitlines() if line.startswith("- ") and not line.startswith("- ha ")]
    assert lines and all(len(line.split(": ", 1)[1]) <= INDEX_LINE_CHARS for line in lines)
    assert all(line.split(": ", 1)[1].strip() for line in lines)
    assert first_sentence("[ha] Turn on a device. It does things.") == "Turn on a device."
    assert first_sentence("  Two\n  lines here  ") == "Two lines here"


def test_the_list_is_sorted_so_a_reload_never_changes_it():
    assert index_text(TOOLS) == index_text(list(reversed(TOOLS)))
    assert "- ha (1 tools): HassTurnOn" in index_text(TOOLS)


def test_a_server_with_many_tools_lists_forty_names_and_the_count():
    many = [_tool(f"mcp__big__t{i:02d}") for i in range(45)]
    line = next(line for line in index_text(many).splitlines() if line.startswith("- big "))
    assert line.startswith("- big (45 tools): t00, t01") and line.endswith(", and 5 more")


def test_nothing_left_out_needs_no_list():
    assert index_tokens([]) == 0
    assert index_tokens(TOOLS[:1]) > 0


# ---- the stored value ---------------------------------------------------------------------------------------------


def test_the_stored_value_round_trips():
    config = ToolLoadingConfig("watch", frozenset({"chat", "heartbeat"}), ("db_query", "mcp__ha__x"), "t", "proxy")
    stored = config.to_json()
    assert stored["kinds"] == ["chat", "heartbeat"]  # in the table's order
    assert parse_tool_loading(stored) == (config, None)
    assert parse_tool_loading(json.dumps(stored)) == (config, None)  # a jsonb read back as its text
    assert parse_tool_loading(OFF.to_json()) == (OFF, None)
    assert config_key("agent-1") == "tool_loading:agent-1"


@pytest.mark.parametrize(
    "value",
    [
        None,
        "not json",
        "[]",
        42,
        [],
        {"version": 1, "mode": "on"},
        {"version": 2, "mode": "watch"},
        {"mode": "watch"},
        {"version": 1, "mode": "watch", "kinds": ["chat", "nonsense"]},
        {"version": 1, "mode": "watch", "kinds": "chat"},
        {"version": 1, "mode": "watch", "kinds": ["chat", "chat"]},
        {"version": 1, "mode": "watch", "core": ["has space"]},
        {"version": 1, "mode": "watch", "core": "bash"},
        {"version": 1, "mode": "watch", "core": [1]},
        {"version": 1, "mode": "watch", "core": ["x"] * 2},
        {"version": 1, "mode": "watch", "core": [f"t{i}" for i in range(201)]},
        {"version": 1, "mode": "watch", "updated_by": "someone"},
        {"version": 1, "mode": "watch", "updated_at": 5},
        {"version": 1, "mode": "watch", "extra": True},
        {"version": 1, "mode": ["watch"]},
    ],
)
def test_a_malformed_value_is_off_with_a_sentence_and_never_raises(value):
    config, problem = parse_tool_loading(value)
    assert config == OFF
    assert isinstance(problem, str) and problem
    assert "nonsense" not in problem and "someone" not in problem  # the value is never repeated


def test_a_value_whose_reading_raises_is_off_with_its_class():
    class Explodes(dict):
        def get(self, *args):
            raise RuntimeError("boom")

    config, problem = parse_tool_loading(Explodes(version=1))
    assert config == OFF and "RuntimeError" in problem and "boom" not in problem
