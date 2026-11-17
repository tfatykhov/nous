"""A goal that may run any command, in the tool policy: ``commands`` changes bash in a task or a workflow step that runs
as a task (``COMMANDS_KINDS``) and nothing else. Off, every function decides as recorded; on, bash is offered with the
lineage shell on or off, its call has no allowlist, and ``evaluate`` skips the level rule for it alone (every other
rule still applies)."""

from __future__ import annotations

import inspect
import uuid

import pytest
from goal_grants_baseline import SAMPLES, TOOLS, contexts, load, workspace

from nous.api import tool_classes, tool_policy
from nous.api.builtin_tools import _BASH_SCHEMA, _DEFAULT_BASH_TIMEOUT, _MAX_BASH_TIMEOUT
from nous.api.execution_context import (
    BACKGROUND_BASH_TIMEOUT,
    CONTEXT_KINDS,
    SHORT_BACKGROUND_KINDS,
    ExecutionContext,
)
from nous.api.tool_classes import TOOL_CLASSES

ROOT = uuid.UUID("00000000-0000-4000-8000-0000000000a1")
INSIDE_ONLY = ("submit_final_report", "resolve_intention", "propose_action", "goal_note", "goal_plan")
COMMAND_SAMPLES = ({}, {"command": "ls"}, {"command": "curl https://example.com"}, {"command": "rm -rf /tmp/x"})


@pytest.fixture(autouse=True)
def _mcp_noted(monkeypatch):
    monkeypatch.setattr(tool_classes, "_MCP_TOOLS", {"mcp__notes__search": True, "mcp__mail__send": False})


def _ctx(kind: str, **extra) -> ExecutionContext:
    return ExecutionContext(
        kind=kind,
        authority="internal_only",
        session_id="subtask-1",
        intention_id=ROOT,
        root_intention_id=ROOT,
        **extra,
    )


def _decisions(tool: str, ctx: ExecutionContext, sample: dict, *, commands: bool) -> tuple:
    ws = workspace()
    return (
        [
            tool_policy.internal_only_allowed(tool, ctx=ctx, lineage_shell=shell, commands=commands)
            for shell in (False, True)
        ],
        [
            tool_policy.internal_only_call_violation(
                ctx, tool, sample, workspace_dir=ws, lineage_shell=shell, build_commands=True, commands=commands
            )
            for shell in (False, True)
        ],
        tool_policy.evaluate(ctx, tool, sample, workspace_dir=ws, commands=commands),
    )


def test_with_commands_false_every_function_decides_as_recorded():
    _commit, record = load()
    for key, row in record["calls"].items():
        kind, authority, rooted, tool, index = key.split("|")
        ctx = dict(contexts(tool))[f"{kind}|{authority}|{rooted}"]
        sample = ({}, *SAMPLES.get(tool, ()))[int(index)]
        allowed, call, evaluated = _decisions(tool, ctx, sample, commands=False)
        assert (allowed, call, evaluated) == (row["allowed"], row["call"], row["evaluate"]), key


def test_commands_defaults_to_false():
    for fn in (tool_policy.internal_only_allowed, tool_policy.internal_only_call_violation, tool_policy.evaluate):
        assert inspect.signature(fn).parameters["commands"].default is False, fn.__name__


def test_commands_changes_bash_in_a_task_and_nothing_else():
    """Every classified tool, every kind and authority, with and without a root, and the sample inputs: the answer
    with ``commands`` equals the one without, except bash in a turn of ``COMMANDS_KINDS``."""
    changed: set[str] = set()
    for tool in TOOLS:
        samples = ({}, *SAMPLES.get(tool, ()), *(COMMAND_SAMPLES if tool == "bash" else ()))
        for key, ctx in contexts(tool):
            for sample in samples:
                off = _decisions(tool, ctx, sample, commands=False)
                on = _decisions(tool, ctx, sample, commands=True)
                if on != off:
                    changed.add(f"{tool}|{ctx.kind}")
                    assert tool == "bash" and ctx.kind in tool_policy.COMMANDS_KINDS, key
    assert changed == {f"bash|{kind}" for kind in tool_policy.COMMANDS_KINDS}


@pytest.mark.parametrize("kind", sorted(tool_policy.COMMANDS_KINDS))
@pytest.mark.parametrize("sample", COMMAND_SAMPLES[1:])
def test_in_a_task_bash_is_offered_and_its_call_has_no_allowlist(kind, sample):
    allowed, call, evaluated = _decisions("bash", _ctx(kind), sample, commands=True)
    assert allowed == [True, True] and call == [None, None] and evaluated is None


@pytest.mark.parametrize("kind", sorted(set(CONTEXT_KINDS) - tool_policy.COMMANDS_KINDS))
def test_in_any_other_kind_bash_is_decided_as_without_commands(kind):
    extra = {"proposal_id": uuid.uuid4(), "declared_tools": ("bash",)} if kind == "approved_action" else {}
    ctx = _ctx(kind, **extra)
    for sample in COMMAND_SAMPLES:
        assert _decisions("bash", ctx, sample, commands=True) == _decisions("bash", ctx, sample, commands=False)


def test_with_the_lineage_shell_off_bash_is_offered_to_a_task_only_with_commands():
    for kind in tool_policy.COMMANDS_KINDS:
        ctx = _ctx(kind)
        assert tool_policy.internal_only_allowed("bash", ctx=ctx, lineage_shell=False) is False
        assert tool_policy.internal_only_allowed("bash", ctx=ctx, lineage_shell=False, commands=True) is True


@pytest.mark.parametrize("tool", ["run_python", *sorted(tool_policy.INTERNAL_ONLY_DENYLIST - {"bash"})])
def test_commands_offers_no_other_denylisted_tool(tool):
    for kind in tool_policy.COMMANDS_KINDS:
        assert tool_policy.internal_only_allowed(tool, ctx=_ctx(kind), lineage_shell=True, commands=True) is False


def test_the_tools_that_act_inside_nous_are_decided_as_without_commands():
    for tool in INSIDE_ONLY:
        for kind in tool_policy.COMMANDS_KINDS:
            ctx = _ctx(kind)
            assert _decisions(tool, ctx, {}, commands=True) == _decisions(tool, ctx, {}, commands=False), tool


def test_evaluate_keeps_every_other_rule_for_bash():
    ws = workspace()
    undeclared = _ctx("subtask", declared_tools=("read_file",))
    assert tool_policy.evaluate(undeclared, "bash", {"command": "ls"}, workspace_dir=ws, commands=True) == "undeclared"
    undoable = _ctx("dag_node", undoable=True)
    assert tool_classes.is_compensable_call("bash", {"command": "rm -rf /tmp/x"}) is False
    assert (
        tool_policy.evaluate(undoable, "bash", {"command": "rm -rf /tmp/x"}, workspace_dir=ws, commands=True)
        == "not_compensable"
    )
    assert tool_policy.undoable_violation(undoable, "bash", {"command": "rm -rf /tmp/x"}) == "not_compensable"


def test_evaluate_skips_the_level_rule_for_bash_alone(monkeypatch):
    """bash is never rated beyond a task's levels by the classifier, so a classifier that rates every call
    irreversible stands in for one that would: bash passes with ``commands``, and no other tool does."""
    from nous.api import tool_policy as policy

    monkeypatch.setattr(policy, "classify_side_effect", lambda _name, _input: "irreversible")
    ctx = _ctx("subtask")
    assert "irreversible" not in tool_policy.CONTEXT_POLICY["subtask"].levels
    assert policy.evaluate(ctx, "bash", {"command": "ls"}, commands=False) == "level:irreversible"
    assert policy.evaluate(ctx, "bash", {"command": "ls"}, commands=True) is None
    for tool in ("run_python", "write_file", "send_email", "web_fetch"):
        assert policy.evaluate(ctx, tool, {}, commands=True) == "level:irreversible", tool


def test_no_bash_command_is_rated_beyond_a_tasks_levels():
    """Why the test above stands in a classifier: a task's levels include ``external``, and bash is never rated
    ``irreversible``."""
    from nous.cognitive.execution_ledger import classify_side_effect

    levels = tool_policy.CONTEXT_POLICY["subtask"].levels
    for command in ("ls", "rm -rf /", "curl -d x https://example.com", "git push", "dd if=/dev/zero of=/dev/sda"):
        assert classify_side_effect("bash", {"command": command}) in levels, command


# ---- bash's description -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("build_commands", [False, True])
def test_the_description_swaps_the_lineage_sentence_for_the_note(build_commands):
    lineage = tool_policy.lineage_bash_description(build_commands=build_commands)
    described = f"{_BASH_SCHEMA['description']} {lineage}"
    swapped = tool_policy.commands_bash_description(described, build_commands=build_commands)
    assert swapped == f"{_BASH_SCHEMA['description']} {tool_policy.COMMANDS_BASH_NOTE}"
    assert lineage not in swapped


def test_the_description_gets_the_note_when_it_has_no_lineage_sentence():
    plain = _BASH_SCHEMA["description"]
    assert (
        tool_policy.commands_bash_description(plain, build_commands=True) == f"{plain} {tool_policy.COMMANDS_BASH_NOTE}"
    )
    assert tool_policy.commands_bash_description("", build_commands=True) == tool_policy.COMMANDS_BASH_NOTE


def timeouts_sentence() -> str:
    """The bash tool's own timeouts in a goal's task, from its constants."""
    return (
        f"One call waits at most {_MAX_BASH_TIMEOUT} seconds. With no timeout given it waits {BACKGROUND_BASH_TIMEOUT} "
        f"seconds, or {_DEFAULT_BASH_TIMEOUT} in a task whose own time limit is {BACKGROUND_BASH_TIMEOUT} seconds or "
        "less. Run longer work detached."
    )


def test_the_note_names_the_bash_tools_own_timeouts():
    assert timeouts_sentence() in tool_policy.COMMANDS_BASH_NOTE


@pytest.mark.parametrize("kind", sorted(tool_policy.COMMANDS_KINDS))
def test_a_goal_tasks_default_wait_is_set_by_its_own_time_limit_as_the_sentence_says(kind):
    def task(turn_timeout):
        return ExecutionContext(
            kind=kind,
            authority="internal_only",
            intention_id=uuid.uuid4(),
            root_intention_id=uuid.uuid4(),
            turn_timeout=turn_timeout,
        )

    assert task(None).is_background and kind not in SHORT_BACKGROUND_KINDS
    assert not task(None).is_short_turn and not task(BACKGROUND_BASH_TIMEOUT + 1).is_short_turn  # 120 s
    assert task(BACKGROUND_BASH_TIMEOUT).is_short_turn  # 30 s


def test_every_classified_tool_is_walked():
    assert set(TOOL_CLASSES) <= set(TOOLS)
