"""F112 MCP client PR 1: flag off, the class prefix rule, the background floor and ToolDispatcher.unregister."""

from __future__ import annotations

import ast
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from test_runner_authorization import _one_tool_call_then_done, _run_loop, _runner

from nous.api import tool_classes, tool_policy
from nous.api.execution_context import CONTEXT_KINDS, ExecutionContext
from nous.api.idempotency import idempotency_key
from nous.api.tool_classes import forget_mcp_tool, note_mcp_tool, refuse_denylist, tool_class
from nous.api.tools import ToolDispatcher
from nous.cognitive.execution_ledger import classify_ingress, classify_side_effect
from nous.config import Settings

ROOT = Path(__file__).resolve().parents[1]
BOTH_WARN = {"tool_offered_set_enforcement_mode": "warn", "tool_context_policy_mode": "warn"}
BG_TOOL = "mcp__bgsrv__search"  # a tool of a server marked available to background work
FG_TOOL = "mcp__fgsrv__search"  # a tool of a server that is not


@pytest.fixture(autouse=True)
def _mcp_tools_noted():
    """The two tools are registered by the MCP client for the test; the module-level set is restored after."""
    saved = dict(tool_classes._MCP_TOOLS)
    note_mcp_tool(BG_TOOL, background=True)
    note_mcp_tool(FG_TOOL, background=False)
    yield
    tool_classes._MCP_TOOLS.clear()
    tool_classes._MCP_TOOLS.update(saved)


# --- flag off --------------------------------------------------------------------------------------------------------


def test_the_flag_is_off_by_default():
    assert Settings(_env_file=None, ANTHROPIC_API_KEY="k").mcp_client_enabled is False


def _imports_of(tree: ast.AST) -> list[tuple[ast.AST, str]]:
    out: list[tuple[ast.AST, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [(node, alias.name) for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.append((node, node.module))
    return out


def _gated_by_the_flag(tree: ast.AST, target: ast.AST) -> bool:
    """Whether ``target`` sits in the body of an ``if`` whose test reads ``mcp_client_enabled``."""
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and "mcp_client_enabled" in ast.unparse(node.test):
            if any(target is inner for stmt in node.body for inner in ast.walk(stmt)):
                return True
    return False


def test_main_imports_and_builds_the_client_only_under_the_flag():
    tree = ast.parse((ROOT / "nous" / "main.py").read_text(encoding="utf-8"))
    imports = [node for node, module in _imports_of(tree) if module.startswith("nous.mcp_client")]
    assert imports, "main.py no longer imports the MCP client: update this test"
    assert all(_gated_by_the_flag(tree, node) for node in imports)
    builds = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "McpClientManager"
    ]
    assert len(builds) == 1 and _gated_by_the_flag(tree, builds[0])


def test_nothing_else_imports_the_client_and_only_transport_imports_the_sdk():
    # nous/api/mcp_client_routes.py is the routes' module: only rest.py imports it, under the flag (next test).
    allowed = ("nous/main.py", "nous/api/mcp_client_routes.py")
    for path in (ROOT / "nous").rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        for _node, module in _imports_of(ast.parse(path.read_text(encoding="utf-8"))):
            if module.startswith("nous.mcp_client"):
                assert rel in allowed or rel.startswith("nous/mcp_client/"), rel
            if module == "nous.api.mcp_client_routes":
                assert rel == "nous/api/rest.py", rel
            if rel.startswith("nous/mcp_client/") and (module == "mcp" or module.startswith("mcp.")):
                assert rel == "nous/mcp_client/transport.py", rel


def test_rest_imports_the_routes_only_under_the_flag():
    tree = ast.parse((ROOT / "nous" / "api" / "rest.py").read_text(encoding="utf-8"))
    imports = [node for node, module in _imports_of(tree) if module == "nous.api.mcp_client_routes"]
    assert len(imports) == 1 and _gated_by_the_flag(tree, imports[0])


@pytest.mark.parametrize("module", ["nous.main", "nous.api.rest"])
def test_importing_main_or_rest_imports_nothing_of_the_client(module):
    code = (
        f"import sys, {module}\n"
        "print(sorted(m for m in sys.modules if m.startswith(('nous.mcp_client', 'nous.api.mcp_client_routes'))))"
    )
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip() == "[]"


# --- the class prefix rule -------------------------------------------------------------------------------------------


def test_any_mcp_name_is_external_registered_or_not():
    assert classify_side_effect("mcp__never__registered") == "external"
    assert classify_side_effect(BG_TOOL, {"anything": 1}) == "external"
    cls = tool_class("mcp__a__b")
    assert cls is not None and cls.side_effect == "external" and cls.ingress == "outside"
    assert not cls.spawns and not cls.compensable
    assert classify_ingress("mcp__a__b") == "outside"


def test_other_names_keep_their_class():
    assert tool_class("recall_deep").side_effect == "none"
    assert tool_class("not_a_tool") is None
    assert classify_side_effect("not_a_tool") == "write"


def test_the_refuse_denylist_follows_the_registered_mcp_tools():
    note_mcp_tool("mcp__other__write", background=False)
    assert "mcp__other__write" in refuse_denylist()
    forget_mcp_tool("mcp__other__write")
    assert "mcp__other__write" not in refuse_denylist()
    forget_mcp_tool("mcp__other__write")  # forgetting twice is not an error


def test_only_mcp_names_can_be_noted():
    with pytest.raises(ValueError):
        note_mcp_tool("bash", background=True)


def test_an_mcp_call_gets_no_idempotency_key():
    ctx = ExecutionContext(kind="dag_node", session_id="s", dag_id=uuid.uuid4(), dag_node_name="n")
    assert idempotency_key(ctx, BG_TOOL, {"to": "x"}) is None


# --- the floor -------------------------------------------------------------------------------------------------------

# kind -> (allowed for a server NOT available to background work, allowed for one that is)
EXPECTED = {
    "interactive": (True, True),
    "approved_action": (True, True),
    "mcp": (False, False),
    "subtask": (False, True),
    "dag_node": (False, True),
    "scheduled": (False, True),
    "agent_action": (False, True),
    "heartbeat_triage": (False, True),
    "heartbeat_check": (False, True),
    "heartbeat_callback": (False, True),
    "dag_summary": (False, True),
    "background": (False, True),
    "continuation": (False, True),
}


def test_the_table_names_every_execution_kind():
    assert set(EXPECTED) == set(CONTEXT_KINDS) == set(tool_policy.MCP_CLIENT_KIND_RULES)


@pytest.mark.parametrize("kind", CONTEXT_KINDS)
def test_every_kind_gets_its_documented_answer(kind):
    assert (
        tool_policy.mcp_client_tool_allowed(kind, False),
        tool_policy.mcp_client_tool_allowed(kind, True),
    ) == EXPECTED[kind]


def test_an_unknown_kind_is_refused():
    assert not tool_policy.mcp_client_tool_allowed("something_new", True)


def _auth(ctx: ExecutionContext, tool: str, **settings):
    r, _d = _runner([tool], **{**BOTH_WARN, **settings})
    return r._authorize_tool_call(ctx, tool, frozenset({tool}), "s1", {})


@pytest.mark.parametrize("kind", ["subtask", "dag_node", "scheduled", "heartbeat_check", "dag_summary"])
def test_a_background_turn_needs_a_background_server_with_both_modes_at_warn(kind):
    ctx = ExecutionContext(kind=kind, session_id="s1")
    refusal = _auth(ctx, FG_TOOL)
    assert refusal is not None and refusal.code == "context_policy"
    assert refusal.text == tool_policy.MCP_CLIENT_CONTEXT_TEXT.format(tool=FG_TOOL)
    if kind in ("subtask", "dag_node", "scheduled"):  # a work-level row: nothing else refuses an external tool
        assert _auth(ctx, BG_TOOL) is None


BOTH_OFF = {"tool_offered_set_enforcement_mode": "off", "tool_context_policy_mode": "off"}


@pytest.mark.parametrize("modes", [BOTH_WARN, BOTH_OFF])
def test_an_inbound_mcp_caller_never_gets_an_mcp_client_tool(modes):
    ctx = ExecutionContext(kind="mcp", session_id="s1")
    for tool in (FG_TOOL, BG_TOOL):
        refusal = _auth(ctx, tool, **modes)
        assert refusal is not None and refusal.code == "context_policy"


def test_a_chat_may_call_any_registered_mcp_tool_but_not_an_unregistered_one():
    ctx = ExecutionContext(kind="interactive", session_id="s1")
    assert _auth(ctx, FG_TOOL) is None
    assert _auth(ctx, BG_TOOL) is None
    refusal = _auth(ctx, "mcp__x__y")
    assert refusal is not None and refusal.code == "context_policy"
    assert refusal.text == tool_policy.MCP_CLIENT_UNREGISTERED_TEXT.format(tool="mcp__x__y")


def test_an_approved_action_may_call_its_declared_mcp_tool():
    ctx = ExecutionContext(
        kind="approved_action", session_id="s1", proposal_id=uuid.uuid4(), declared_tools=(FG_TOOL,)
    )
    assert _auth(ctx, FG_TOOL) is None


def test_the_earlier_floors_still_refuse_a_background_server_tool():
    # Stricter than the table, on purpose: a triage turn may not act outward, an internal_only turn may not call an
    # external tool, and an MCP tool is external.
    triage = _auth(ExecutionContext(kind="heartbeat_triage", session_id="s1"), BG_TOOL)
    assert triage is not None and triage.text == tool_policy.HEARTBEAT_OUTWARD_TEXT
    lineage = ExecutionContext(
        kind="continuation",
        session_id="s1",
        authority="internal_only",
        intention_id=uuid.uuid4(),
        root_intention_id=uuid.uuid4(),
    )
    refusal = _auth(lineage, BG_TOOL)
    assert refusal is not None and refusal.code == "internal_only"


async def test_the_real_tool_loop_refuses_a_forged_background_call():
    r, d = _runner([FG_TOOL], **BOTH_WARN)
    r._call_api = _one_tool_call_then_done(FG_TOOL)
    _text, results, _usage, _thinking = await _run_loop(
        r, is_background=True, context=ExecutionContext(kind="subtask", session_id="s1")
    )
    assert d.calls == []
    assert "this turn may not use it" in (results[0].error or "")


def _offered(kind: str, tools: list[str]) -> list[str]:
    r, _d = _runner(tools, **BOTH_WARN)
    ctx = ExecutionContext(kind=kind, session_id="s1")
    return [t["name"] for t in r._offered_tools(ctx, "task", is_subtask=False, tool_filter=None, refuse_active=False)]


def test_the_offered_set_drops_what_the_floor_refuses():
    tools = ["recall_deep", BG_TOOL, FG_TOOL, "mcp__gone__tool"]
    assert _offered("interactive", tools) == ["recall_deep", BG_TOOL, FG_TOOL]
    assert _offered("subtask", tools) == ["recall_deep", BG_TOOL]
    assert _offered("mcp", tools) == ["recall_deep"]


def test_extra_tools_are_appended_after_the_drop():
    r, _d = _runner(["recall_deep", FG_TOOL], **BOTH_WARN)
    ctx = ExecutionContext(kind="subtask", session_id="s1")
    extra = {"submit_final_report": ({"name": "submit_final_report", "input_schema": {"type": "object"}}, None)}
    offered = r._offered_tools(
        ctx, "task", is_subtask=False, tool_filter=None, refuse_active=False, extra_tools=extra
    )
    assert [t["name"] for t in offered] == ["recall_deep", "submit_final_report"]


# --- ToolDispatcher.unregister ---------------------------------------------------------------------------------------


async def _handler(**_kwargs):
    return {"content": [{"type": "text", "text": "ok"}]}


def test_unregister_clears_every_structure_register_fills():
    d = ToolDispatcher()
    d.register("mcp__s__t", _handler, {"type": "object", "description": "t"}, origin_aware=True)
    d.register("recall_deep", _handler, {"type": "object", "description": "r"})
    assert "mcp__s__t" in [t["name"] for t in d.available_tools("task")]  # fills the per-frame cache
    assert d._tool_schema_cache

    assert d.unregister("mcp__s__t") is True
    assert not d.is_registered("mcp__s__t")
    assert "mcp__s__t" not in d._handlers and "mcp__s__t" not in d._schemas
    assert "mcp__s__t" not in d._origin_aware
    assert not d._tool_schema_cache
    assert [t["name"] for t in d.available_tools("task")] == ["recall_deep"]
    assert d.unregister("mcp__s__t") is False


def test_unregister_refuses_an_alias_and_a_retired_name():
    d = ToolDispatcher()
    d.register("target", _handler, {"type": "object"})
    d.register_alias("old_name", "target", lambda args: args, "note")
    d.retire("gone_name", "gone")
    for name in ("old_name", "gone_name"):
        with pytest.raises(ValueError):
            d.unregister(name)
    assert d.is_registered("target")


# --- the floor also needs the tool in the turn's offered set --------------------------------------------------------


@pytest.mark.parametrize("kind", ["interactive", "subtask"])
def test_an_mcp_tool_the_turn_was_not_offered_is_refused_with_both_modes_at_warn(kind):
    r, _d = _runner([BG_TOOL], **BOTH_WARN)
    ctx = ExecutionContext(kind=kind, session_id="s1")
    refusal = r._authorize_tool_call(ctx, BG_TOOL, frozenset({"recall_deep"}), "s1", {})
    assert refusal is not None and refusal.code == "context_policy"
    assert refusal.text == tool_policy.MCP_CLIENT_NOT_OFFERED_TEXT.format(tool=BG_TOOL)
    # Under `warn` the ordinary offered-set rule lets an unoffered non-MCP tool through: the floor is what refuses.
    assert r._authorize_tool_call(ctx, "recall_deep", frozenset(), "s1", {}) is None


def test_a_refuse_censor_strips_a_registered_mcp_tool_and_the_call_is_then_refused():
    r, _d = _runner(["recall_deep", BG_TOOL], **BOTH_WARN)
    ctx = ExecutionContext(kind="interactive", session_id="s1")
    offered = r._offered_tools(ctx, "task", is_subtask=False, tool_filter=None, refuse_active=True)
    names = frozenset(t["name"] for t in offered)
    assert names == {"recall_deep"}
    refusal = r._authorize_tool_call(ctx, BG_TOOL, names, "s1", {})
    assert refusal is not None and refusal.text == tool_policy.MCP_CLIENT_NOT_OFFERED_TEXT.format(tool=BG_TOOL)


# --- small guards ---------------------------------------------------------------------------------------------------


async def test_an_array_whose_items_schema_is_true_does_not_break_the_type_check():
    d = ToolDispatcher()
    d.register("tags_tool", _handler, {"type": "object", "properties": {"tags": {"type": "array", "items": True}}})
    text, is_error = await d.dispatch("tags_tool", {"tags": "a,b"})
    assert is_error and "tags must be array, got string" in text and "JSON array of strings" in text
    assert await d.dispatch("tags_tool", {"tags": ["a", "b"]}) == ("ok", False)


def test_classify_side_effect_takes_a_name_that_is_not_a_string():
    assert classify_side_effect(42) == "write"  # type: ignore[arg-type]
