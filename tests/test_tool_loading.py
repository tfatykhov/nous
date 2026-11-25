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
from types import SimpleNamespace
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
from nous.observability.model_calls import ContextStats, ModelCallRecorder, cache_state

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


class _Unreadable(AssertionError):
    """A forced tool whose name the scan cannot read: it must be made readable (a literal or a module constant)."""


def _module_strings(tree: ast.Module) -> dict[str, str]:
    """A module's top-level ``NAME = "text"`` assignments."""
    found: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        value = node.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            found.update({t.id: value.value for t in targets if isinstance(t, ast.Name)})
    return found


def _names_of(value: ast.expr, local: dict[str, str], everywhere: dict[str, set[str]], where: str) -> set[str]:
    """The tool names a forced value can be. None gives none; a name holding ``force_tool`` is passed through (its own
    assignments and keywords are read where they are made); a constant name is read from its module, else from the one
    module-level string of that name in nous/. Anything else cannot be read: ``_Unreadable``."""
    if isinstance(value, ast.Constant):
        if value.value is None:
            return set()
        if isinstance(value.value, str):
            return {value.value}
    elif isinstance(value, ast.Name):
        if "force_tool" in value.id:
            return set()
        if value.id in local:
            return {local[value.id]}
        if len(everywhere.get(value.id, ())) == 1:
            return set(everywhere[value.id])
    elif isinstance(value, ast.IfExp):
        return _names_of(value.body, local, everywhere, where) | _names_of(value.orelse, local, everywhere, where)
    elif isinstance(value, ast.BoolOp):
        return set().union(*(_names_of(v, local, everywhere, where) for v in value.values))
    raise _Unreadable(f"{where}: cannot read the forced tool {ast.unparse(value)!r}")


def _forced_tools(sources: dict[str, str]) -> set[str]:
    """Every tool name a harness step can force, from ``sources`` (path to source): each ``force_tool_on_penultimate=``
    keyword, each assignment to a name holding ``force_tool``, and the name of each literal ``{"type": "tool", ...}``
    tool choice in the runner."""
    trees = {path: ast.parse(source) for path, source in sources.items()}
    everywhere: dict[str, set[str]] = {}
    for tree in trees.values():
        for name, value in _module_strings(tree).items():
            everywhere.setdefault(name, set()).add(value)
    forced: set[str] = set()
    for path, tree in trees.items():
        local = _module_strings(tree)
        for node in ast.walk(tree):
            values: list[ast.expr] = []
            if isinstance(node, ast.keyword) and node.arg == "force_tool_on_penultimate":
                values.append(node.value)
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and "force_tool" in t.id for t in node.targets
            ):
                values.append(node.value)
            if path.endswith("runner.py") and isinstance(node, ast.Dict):
                pairs = {k.value: v for k, v in zip(node.keys, node.values, strict=True) if isinstance(k, ast.Constant)}
                kind = pairs.get("type")
                if isinstance(kind, ast.Constant) and kind.value == "tool" and "name" in pairs:
                    values.append(pairs["name"])
            for value in values:
                forced |= _names_of(value, local, everywhere, f"{path}:{getattr(node, 'lineno', '?')}")
    return forced


def test_every_tool_a_harness_step_forces_is_pinned():
    """The forced tool_choice comes from ``force_tool_on_penultimate`` and the runner's own tool-choice dict: every name
    a caller can force is read from the code (a value that cannot be read fails) and is pinned."""
    sources = {
        path.relative_to(ROOT).as_posix(): path.read_text(encoding="utf-8") for path in (ROOT / "nous").rglob("*.py")
    }
    forced = _forced_tools(sources)
    assert "submit_final_report" in forced  # the scan reaches the subtask executor
    assert forced <= set(PINNED), forced - set(PINNED)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('X = "a_report"\nf(force_tool_on_penultimate=X)', {"a_report"}),
        ("f(force_tool_on_penultimate=None)", set()),
        ('force_tool = "b_report" if on else None', {"b_report"}),
        ("f(force_tool_on_penultimate=force_tool)", set()),  # passed through: read where it is assigned
    ],
)
def test_the_forced_tool_scan_reads_literals_constants_and_pass_throughs(source, expected):
    assert _forced_tools({"nous/x.py": source}) == expected


@pytest.mark.parametrize(
    "source",
    [
        "f(force_tool_on_penultimate=NEW_REPORT_TOOL)",  # a name with no string behind it
        "f(force_tool_on_penultimate=settings.report_tool)",
        'f(force_tool_on_penultimate=pick("a"))',
        "force_tool = choose()",
    ],
)
def test_a_forced_value_the_scan_cannot_read_fails(source):
    with pytest.raises(_Unreadable):
        _forced_tools({"nous/x.py": source})


def test_a_literal_tool_choice_in_the_runner_is_read():
    runner = 'tool_choice_arg = {"type": "tool", "name": "c_report"}'
    assert _forced_tools({"nous/api/runner.py": runner}) == {"c_report"}
    with pytest.raises(_Unreadable):
        _forced_tools({"nous/api/runner.py": 'tool_choice_arg = {"type": "tool", "name": other.name}'})


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


@pytest.mark.parametrize(
    ("usage", "state"),
    [
        ({"cache_read_input_tokens": 1, "cache_creation_input_tokens": 0}, "read"),
        ({"cache_read_input_tokens": 52000, "cache_creation_input_tokens": 900}, "read"),  # the tools lead the prefix
        ({"cache_read_input_tokens": 0, "cache_creation_input_tokens": 46000}, "written"),
        ({"cache_creation_input_tokens": 46000}, "written"),
        ({"cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}, None),  # neither: not weighed
        ({"input_tokens": 90000}, None),
        ({}, None),
    ],
)
def test_the_cache_state_is_the_calls_own_usage(usage, state):
    assert cache_state(usage) == state


def _stats(record: dict | None) -> ContextStats:
    return ContextStats({"tools_definition": 1000}, 0, 0, 0.0, [], 1, ["bash"], 0, {}, tool_loading=record)


def test_the_watch_row_carries_the_call_the_kind_the_model_and_the_cache_state():
    recorder = ModelCallRecorder(SimpleNamespace(), None, "a", SimpleNamespace())
    call_id, turn_id, started = uuid4(), uuid4(), object()
    params = {"id": call_id, "agent_id": "a", "started_at": started, "turn_id": turn_id, "model": "claude-x"}
    row = recorder._watch_row(params, _stats({"kind": "chat", "deferred": 3}), {"cache_creation_input_tokens": 5})
    assert {k: row[k] for k in ("call_id", "agent_id", "started_at", "turn_id", "kind")} == {
        "call_id": call_id,
        "agent_id": "a",
        "started_at": started,
        "turn_id": turn_id,
        "kind": "chat",
    }
    assert json.loads(row["record"]) == {"kind": "chat", "deferred": 3, "model": "claude-x", "cache": "written"}


def test_a_watch_row_that_cannot_be_built_is_none_and_logged_once(caplog):
    recorder = ModelCallRecorder(SimpleNamespace(), None, "a", SimpleNamespace())
    with caplog.at_level("WARNING", logger="nous.observability.model_calls"):
        assert recorder._watch_row({}, _stats({"kind": "chat"}), {}) is None  # no id
        assert recorder._watch_row({"id": 1}, _stats({}), {}) is None  # no kind
    assert [r.getMessage() for r in caplog.records if "F120" in r.getMessage()] == [
        "F120: a tool-loading watch row was not written (KeyError); model calls are recorded as before"
    ]


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
    assert "- ha (1 tools): mcp__ha__HassTurnOn" in index_text(TOOLS)


def test_a_server_with_many_tools_lists_every_tool_by_the_full_name_the_loader_takes():
    """No cap: the estimate in Watch only is the list On sends, where every deferred tool must be found."""
    many = [_tool(f"mcp__big__t{i:02d}") for i in range(45)]
    line = next(line for line in index_text(many).splitlines() if line.startswith("- big "))
    assert line == "- big (45 tools): " + ", ".join(f"mcp__big__t{i:02d}" for i in range(45))
    assert "Tools of other servers (load one by its full name, as listed):" in index_text(many)
    assert "call one" not in index_text(many)


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
        {"version": 1, "mode": "full"},
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


def _card_fields(*bodies: str) -> set[str]:
    return set(re.findall(r"(\w+)\??:", re.sub(r"/\*.*?\*/", "", ";".join(bodies), flags=re.S)))


def _card_source() -> str:
    return (ROOT / "dashboard-app/src/lib/tool_loading.ts").read_text(encoding="utf-8")


def test_a_numbers_row_has_exactly_the_fields_the_card_reads():
    """Both sides of the contract: the server's row and the card's ``KindNumbers`` (with ``KindSwitch``) name the same
    fields, so a keyed list on the card never meets a missing key; so do the On numbers (``OnNumbers``) and each reason
    calls were sent in full (``NotApplied``)."""
    from nous.api.tool_loading_routes import numbers_row, on_numbers

    source = _card_source()
    switch = re.search(r"export interface KindSwitch \{([^}]*)\}", source).group(1)
    numbers = re.search(r"export interface KindNumbers extends KindSwitch \{(.*?)\n\}", source, re.S).group(1)
    on_mode = re.search(r"export interface OnNumbers \{(.*?)\n\}", source, re.S).group(1)
    not_applied = re.search(r"export interface NotApplied \{([^}]*)\}", source).group(1)

    assert set(numbers_row("chat", "Chat", True, None, (0, 0))) == _card_fields(switch, numbers)
    sums = dict.fromkeys(
        ("calls", "applied", "engaged_turns", "read_calls", "written_calls", "read_net", "written_net"), 0
    ) | {"full_sent": 0, "deferred": 0, "fallbacks": 0}
    row = numbers_row("chat", "Chat", True, None, (0, 0), sums, (0, 0), [("model", 2)])
    assert set(row["on_mode"]) == _card_fields(on_mode)
    assert set(row["on_mode"]["not_applied"][0]) == _card_fields(not_applied)
    assert set(on_numbers(sums, None, [])) == _card_fields(on_mode)


def test_the_card_knows_every_mode_and_every_reason():
    """The modes the server stores and the card's ``Mode`` and words; the reasons a call in On is sent in full
    (``why_not``) and the card's words for them."""
    from nous.api import tool_loading

    source = _card_source()
    mode = re.search(r"export type Mode = ([^;]*);", source).group(1)
    assert set(re.findall(r"'(\w+)'", mode)) == set(tool_loading.MODES)
    words = re.search(r"export const MODE_WORDS: Record<Mode, string> = \{([^}]*)\}", source).group(1)
    assert set(re.findall(r"(\w+):", words)) == set(tool_loading.MODES)
    why = re.search(r"export const WHY_WORDS: Record<string, string> = \{(.*?)\n\};", source, re.S).group(1)
    reasons = {name: value for name, value in vars(tool_loading).items() if name.startswith("WHY_")}
    assert set(reasons) == {"WHY_CLIENT", "WHY_MODEL", "WHY_NAME", "WHY_NOTHING", "WHY_TURN", "WHY_FAULT"}
    reasons = set(reasons.values())
    assert set(re.findall(r"^\s*(\w+):", why, re.M)) == reasons
