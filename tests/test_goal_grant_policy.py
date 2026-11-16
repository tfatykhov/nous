"""Per-goal tool grants, the policy side: which tools a goal may be given (``grant_refusal``), the kinds of turn a
grant can reach (``GRANT_KINDS``), and what ``lifted`` changes in ``internal_only_allowed``,
``internal_only_call_violation`` and ``evaluate``: the class verdict only, never the denylist, the per-call path and
cancel rules, the undoable rule or the spawn rule; and the rules a granted call is held to instead (a granted send_file
or send_email reads only regular files under the root's own directory, and send_file deletes none). Also the MCP
client's record of which server and remote tool each registered MCP tool is (``mcp_tool_identity``)."""

from __future__ import annotations

import uuid
from types import MappingProxyType, SimpleNamespace

import pytest
from goal_grants_baseline import MCP_NOTED, SAMPLES, TOOLS, contexts, load

from nous.api import tool_classes, tool_policy
from nous.api.execution_context import CONTEXT_KINDS, ExecutionContext
from nous.api.tool_classes import (
    TOOL_CLASSES,
    McpIdentity,
    ToolClass,
    forget_mcp_tool,
    mcp_tool_identity,
    note_mcp_tool,
)
from nous.autonomy.tool_guard import HARNESS_TOOLS

ROOT = uuid.uuid4()
WS = "/tmp/nous-goal-grants-policy"
GRANTABLE = {"send_file", "send_email", "browser_task"}


def _cont(**extra) -> ExecutionContext:
    return ExecutionContext(
        kind="continuation", authority="internal_only", intention_id=ROOT, root_intention_id=ROOT, **extra
    )


def _sub(kind: str = "subtask", **extra) -> ExecutionContext:
    return ExecutionContext(kind=kind, authority="internal_only", intention_id=ROOT, root_intention_id=ROOT, **extra)


@pytest.fixture
def mcp_registry(monkeypatch):
    monkeypatch.setattr(tool_classes, "_MCP_TOOLS", dict(MCP_NOTED))
    monkeypatch.setattr(tool_classes, "_MCP_IDENTITIES", {})


# ---- grant_refusal ---------------------------------------------------------------------------------------------


def test_exactly_the_outward_tools_and_mcp_tools_may_be_given_to_a_goal():
    allowed = {name for name in TOOL_CLASSES if tool_policy.grant_refusal(name) is None}
    assert allowed == GRANTABLE
    assert tool_policy.grant_refusal("mcp__s__t") is None
    assert tool_policy.grant_refusal("mcp__notes__search") is None


@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("not_a_tool", "unclassified"),
        ("", "unclassified"),
        ("resolve_intention", "harness"),
        ("propose_action", "harness"),
        ("goal_note", "harness"),
        ("goal_plan", "harness"),
        ("submit_final_report", "harness"),
        ("bash", "never"),
        ("run_python", "never"),
        ("schedule_task", "never"),
        ("spawn_task", "never"),
        ("dag_create", "never"),
        ("spawn_sync", "never"),
        ("heartbeat_check_create", "never"),
        ("store_identity", "never"),
        ("web_search", "not_outward"),
        ("read_file", "not_outward"),
        ("write_file", "not_outward"),
        ("cancel_task", "not_outward"),
    ],
)
def test_each_refusal_code(name, code):
    assert tool_policy.grant_refusal(name) == code


def test_every_tool_is_judged_and_every_harness_tool_refused():
    for name in TOOL_CLASSES:
        assert tool_policy.grant_refusal(name) in (None, "harness", "never", "spawns", "not_outward"), name
    for name in HARNESS_TOOLS:
        assert tool_policy.grant_refusal(name) == "harness"
    for name in tool_policy.INTERNAL_ONLY_DENYLIST | tool_policy.INTERNAL_ONLY_SPAWN_TOOLS:
        assert tool_policy.grant_refusal(name) == "never", name


def test_an_outward_tool_on_the_denylist_can_never_be_given(monkeypatch):
    monkeypatch.setattr(tool_policy, "INTERNAL_ONLY_DENYLIST", tool_policy.INTERNAL_ONLY_DENYLIST | {"send_email"})
    assert tool_policy.grant_refusal("send_email") == "never"


def test_an_outward_tool_that_starts_work_can_never_be_given(monkeypatch):
    table = dict(TOOL_CLASSES)
    table["reach_and_spawn"] = ToolClass("external", spawns=True)
    monkeypatch.setattr(tool_classes, "TOOL_CLASSES", MappingProxyType(table))
    assert tool_policy.grant_refusal("reach_and_spawn") == "spawns"


def test_the_grant_kinds():
    assert tool_policy.GRANT_KINDS == frozenset({"continuation", "subtask", "dag_node"})
    assert tool_policy.GRANT_KINDS <= set(CONTEXT_KINDS)


# ---- lifted: off is the baseline -------------------------------------------------------------------------------


def _grid():
    for tool in TOOLS:
        for index, sample in enumerate(({}, *SAMPLES.get(tool, ()))):
            for key, ctx in contexts(tool):
                yield f"{key}|{tool}|{index}", tool, sample, ctx


def _pure(tool, sample, ctx, *, lifted, workspace_dir):
    return {
        "allowed": [
            tool_policy.internal_only_allowed(tool, ctx=ctx, lineage_shell=shell, lifted=lifted)
            for shell in (False, True)
        ],
        "call": [
            tool_policy.internal_only_call_violation(
                ctx, tool, sample, workspace_dir=workspace_dir, lineage_shell=shell, build_commands=True, lifted=lifted
            )
            for shell in (False, True)
        ],
        "evaluate": tool_policy.evaluate(ctx, tool, sample, workspace_dir=workspace_dir, lifted=lifted),
    }


def test_not_lifted_is_the_baseline(mcp_registry):
    from goal_grants_baseline import workspace

    _commit, baseline = load()
    workspace_dir = workspace()
    checked = 0
    for key, tool, sample, ctx in _grid():
        row = baseline["calls"][key]
        got = _pure(tool, sample, ctx, lifted=False, workspace_dir=workspace_dir)
        assert got == {name: row[name] for name in got}, key
        checked += 1
    assert checked == len(baseline["calls"])


def test_lifted_changes_only_the_class_verdict_of_a_grantable_tool(mcp_registry):
    from goal_grants_baseline import workspace

    workspace_dir = workspace()
    changed: set[str] = set()
    for key, tool, sample, ctx in _grid():
        off = _pure(tool, sample, ctx, lifted=False, workspace_dir=workspace_dir)
        on = _pure(tool, sample, ctx, lifted=True, workspace_dir=workspace_dir)
        if off == on:
            continue
        assert tool_policy.grant_refusal(tool) is None, key
        changed.add(tool)
        # Each difference is the class verdict going away: offered, no ``external`` call verdict, no level verdict.
        for shell in (0, 1):
            if off["allowed"][shell] != on["allowed"][shell]:
                assert (off["allowed"][shell], on["allowed"][shell]) == (False, True), key
            if off["call"][shell] != on["call"][shell]:
                if on["call"][shell] == "grant_file":  # a granted send_file call names no file
                    assert tool == "send_file" and "file_path" not in sample, key
                    assert off["call"][shell] == "external", key
                else:
                    assert (off["call"][shell], on["call"][shell]) == ("external", None), key
        if off["evaluate"] != on["evaluate"]:
            assert off["evaluate"].startswith("level:") and on["evaluate"] is None, key
    assert changed == GRANTABLE | {"mcp__notes__search", "mcp__mail__send", "mcp__gone__thing"}


def test_lifted_still_refuses_a_denylisted_outward_tool(monkeypatch):
    monkeypatch.setattr(tool_policy, "INTERNAL_ONLY_DENYLIST", tool_policy.INTERNAL_ONLY_DENYLIST | {"send_email"})
    assert tool_policy.internal_only_allowed("send_email", ctx=_cont(), lifted=True) is False
    assert tool_policy.internal_only_allowed("send_file", ctx=_cont(), lifted=True) is True


def test_lifted_does_not_reach_a_tool_that_cannot_be_given():
    # The spawn tools stay where the rule puts them, and so do the shell and Python, whatever a call does.
    assert tool_policy.internal_only_allowed("spawn_task", ctx=_sub(), lifted=True) is False
    assert tool_policy.internal_only_allowed("schedule_task", ctx=_cont(), lifted=True) is False
    network = {"code": "import requests"}
    assert tool_policy.internal_only_call_violation(_cont(), "run_python", network, workspace_dir=WS, lifted=True) == (
        "external"
    )
    assert tool_policy.evaluate(_cont(), "run_python", network, lifted=True) == "level:external"


def test_lifted_keeps_the_unclassified_refusal():
    assert tool_policy.internal_only_allowed("not_a_tool", ctx=_cont(), lifted=True) is False
    assert tool_policy.evaluate(_cont(), "not_a_tool", {}, lifted=True) == "unclassified"


def test_lifted_keeps_the_write_path_and_cancel_rules():
    outside = {"path": "elsewhere/x.md", "content": "x"}
    assert tool_policy.internal_only_call_violation(_cont(), "write_file", outside, workspace_dir=WS, lifted=True) == (
        "write_path"
    )
    bad = {"task_id": "not-a-uuid"}
    assert tool_policy.internal_only_call_violation(_cont(), "cancel_task", bad, workspace_dir=WS, lifted=True) == (
        "foreign_cancel"
    )


def test_lifted_skips_only_the_external_call_verdict():
    assert tool_policy.internal_only_call_violation(_cont(), "send_email", {}, workspace_dir=WS) == "external"
    assert tool_policy.internal_only_call_violation(_cont(), "send_email", {}, workspace_dir=WS, lifted=True) is None


def test_evaluate_lifted_skips_the_level_only():
    assert tool_policy.evaluate(_cont(), "send_email", {}) == "level:external"
    assert tool_policy.evaluate(_cont(), "send_email", {}, lifted=True) is None
    assert tool_policy.evaluate(_cont(), "mcp__x__y", {}, lifted=True) is None
    # An undoable node's outward call is still refused: a send cannot be undone.
    undoable = _sub("dag_node", undoable=True)
    assert tool_policy.evaluate(undoable, "send_email", {}, lifted=True) == "not_compensable"
    # The spawn rule still applies.
    assert tool_policy.evaluate(_sub(), "schedule_task", {}, lifted=True) == "spawn"
    # A declared tool list still applies.
    declared = ExecutionContext(kind="heartbeat_check", declared_tools=("web_search",))
    assert tool_policy.evaluate(declared, "send_email", {}, lifted=True) == "undeclared"


def test_the_defaults_are_not_lifted():
    assert tool_policy.internal_only_allowed("send_email", ctx=_cont()) is False
    assert tool_policy.internal_only_call_violation(_cont(), "send_email", {}, workspace_dir=WS) == "external"
    assert tool_policy.evaluate(_cont(), "send_email", {}) == "level:external"


def test_lifted_does_not_reach_the_mcp_floor(mcp_registry):
    # The MCP floor has no lifted keyword: a server not available to background work stays refused.
    assert tool_policy.mcp_client_violation("continuation", "mcp__mail__send") == "mcp_context"
    assert tool_policy.mcp_client_violation("continuation", "mcp__gone__thing") == "mcp_unregistered"
    assert tool_policy.mcp_client_violation("continuation", "mcp__notes__search") is None


# ---- a granted call's files -------------------------------------------------------------------------------------


@pytest.fixture
def tree(tmp_path):
    """A workspace with the root's directory, another root's, a file beside them and a directory outside."""
    ws = tmp_path / "ws"
    root_dir = ws / "intentions" / str(ROOT)
    (root_dir / "sub").mkdir(parents=True)
    (root_dir / "report.txt").write_text("r")
    (root_dir / "sub" / "deep.txt").write_text("d")
    other = ws / "intentions" / str(uuid.uuid4())
    other.mkdir(parents=True)
    (other / "x.txt").write_text("x")
    (ws / "elsewhere.txt").write_text("e")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("s")
    resolved = ws.resolve()
    return SimpleNamespace(
        ws=str(ws),
        root=resolved / "intentions" / str(ROOT),
        other=resolved / other.relative_to(ws),
        elsewhere=resolved / "elsewhere.txt",
        outside=outside.resolve(),
    )


def _link(link, target, *, is_dir: bool = False):
    try:
        link.symlink_to(target, target_is_directory=is_dir)
    except OSError as exc:  # Windows without the right to make links
        pytest.skip(f"cannot make a symbolic link here ({type(exc).__name__})")


def _send_file(tree, file_path, *, lifted=True, ctx=None, **extra):
    call = {"file_path": file_path, **extra}
    return tool_policy.internal_only_call_violation(
        ctx or _cont(), "send_file", call, workspace_dir=tree.ws, lifted=lifted
    )


def _send_email(tree, attachments, *, lifted=True):
    call = {**SEND_EMAIL, "attachments": attachments}
    return tool_policy.internal_only_call_violation(_cont(), "send_email", call, workspace_dir=tree.ws, lifted=lifted)


SEND_EMAIL = {"to": "a@example.com", "subject": "s", "body": "b"}


def test_a_granted_send_file_may_send_a_file_under_the_roots_directory(tree):
    assert _send_file(tree, str(tree.root / "report.txt")) is None
    assert _send_file(tree, str(tree.root / "sub" / "deep.txt")) is None


@pytest.mark.parametrize(
    "where",
    [
        "relative",
        "workspace",
        "outside",
        "other root",
        "dotdot out",
        "dotdot in",
        "the directory",
        "a subdirectory",
        "missing",
        "nul",
        "empty",
    ],
)
def test_a_granted_send_file_refuses_any_other_path(tree, where):
    path = {
        "relative": f"intentions/{ROOT}/report.txt",
        "workspace": str(tree.elsewhere),
        "outside": str(tree.outside / "secret.txt"),
        "other root": str(tree.other / "x.txt"),
        "dotdot out": str(tree.root) + f"/../{tree.other.name}/x.txt",
        "dotdot in": str(tree.root) + "/sub/../report.txt",
        "the directory": str(tree.root),
        "a subdirectory": str(tree.root / "sub"),
        "missing": str(tree.root / "nothing.txt"),
        "nul": str(tree.root / "report.txt") + "\x00",
        "empty": "",
    }[where]
    assert _send_file(tree, path) == "grant_file"
    assert _send_file(tree, path, lifted=False) == "external"


@pytest.mark.parametrize("value", [None, 7, ["a"], {"path": "x"}])
def test_a_granted_send_file_refuses_a_file_path_that_is_not_text(tree, value):
    assert _send_file(tree, value) == "grant_file"


def test_a_granted_send_file_refuses_a_file_reached_through_a_symbolic_link(tree):
    _link(tree.root / "link.txt", tree.outside / "secret.txt")
    assert _send_file(tree, str(tree.root / "link.txt")) == "grant_file"
    _link(tree.root / "linked", tree.outside, is_dir=True)
    assert _send_file(tree, str(tree.root / "linked" / "secret.txt")) == "grant_file"
    # Even a link that stays in the directory: the path must reach the file with no link at all.
    _link(tree.root / "alias.txt", tree.root / "report.txt")
    assert _send_file(tree, str(tree.root / "alias.txt")) == "grant_file"
    assert _send_file(tree, str(tree.root / "report.txt")) is None


@pytest.mark.parametrize("cleanup", [True, "true", "false", 1, "yes"])
def test_a_granted_send_file_may_not_delete_the_file(tree, cleanup):
    inside = str(tree.root / "report.txt")
    assert _send_file(tree, inside, cleanup=cleanup) == "grant_cleanup"
    assert _send_file(tree, inside, cleanup=cleanup, lifted=False) == "external"


@pytest.mark.parametrize("cleanup", [False, None, 0])
def test_a_granted_send_file_may_say_it_keeps_the_file(tree, cleanup):
    assert _send_file(tree, str(tree.root / "report.txt"), cleanup=cleanup) is None


def test_a_granted_send_file_with_no_root_sends_nothing(tree):
    no_root = ExecutionContext(kind="subtask", authority="internal_only")
    assert _send_file(tree, str(tree.root / "report.txt"), ctx=no_root) == "grant_file"


@pytest.mark.parametrize(
    "attachments",
    [None, "", [], [""], ["  "], "REPORT", ["REPORT"], ["REPORT", "DEEP"], ("REPORT",), "  REPORT  "],
)
def test_a_granted_send_email_may_attach_files_under_the_roots_directory(tree, attachments):
    report, deep = str(tree.root / "report.txt"), str(tree.root / "sub" / "deep.txt")

    def spelled(item):
        return item.replace("REPORT", report).replace("DEEP", deep)

    value = spelled(attachments) if isinstance(attachments, str) else attachments
    if isinstance(attachments, (list, tuple)):
        value = type(attachments)(spelled(item) for item in attachments)
    assert _send_email(tree, value) is None


@pytest.mark.parametrize("where", ["outside", "workspace", "relative", "dotdot", "mixed", "not text", "a mapping"])
def test_a_granted_send_email_refuses_any_other_attachment(tree, where):
    inside = str(tree.root / "report.txt")
    value = {
        "outside": str(tree.outside / "secret.txt"),
        "workspace": [str(tree.elsewhere)],
        "relative": [f"intentions/{ROOT}/report.txt"],
        "dotdot": [str(tree.root) + "/sub/../report.txt"],
        "mixed": [inside, str(tree.outside / "secret.txt")],
        "not text": [inside, 7],
        "a mapping": {inside: True},
    }[where]
    assert _send_email(tree, value) == "grant_file"
    assert _send_email(tree, value, lifted=False) == "external"


def test_a_granted_send_email_refuses_an_attachment_reached_through_a_symbolic_link(tree):
    _link(tree.root / "link.txt", tree.outside / "secret.txt")
    assert _send_email(tree, [str(tree.root / "link.txt")]) == "grant_file"


def test_only_send_file_and_send_email_have_the_file_rule(tree, mcp_registry):
    elsewhere = {"file_path": str(tree.outside / "secret.txt"), "cleanup": True}
    for tool in ("browser_task", "mcp__notes__search"):
        verdict = tool_policy.internal_only_call_violation(_cont(), tool, elsewhere, workspace_dir=tree.ws, lifted=True)
        assert verdict is None, tool


def test_the_refusal_sentences(tree):
    root = tree.root
    assert tool_policy.granted_call_hint(_cont(), "send_file", tree.ws, "grant_file") == (
        f"With this goal's grant, send_file sends only a regular file under {root}/, named by its full path, with no "
        "symbolic link and no '..' in it."
    )
    assert tool_policy.granted_call_hint(_cont(), "send_email", tree.ws, "grant_file") == (
        f"With this goal's grant, send_email attaches only regular files under {root}/, each named by its full path, "
        "with no symbolic link and no '..' in it."
    )
    assert tool_policy.granted_call_hint(_cont(), "send_file", tree.ws, "grant_cleanup") == (
        "With this goal's grant, send_file may not delete the file it sends: leave cleanup out."
    )
    no_root = ExecutionContext(kind="subtask", authority="internal_only")
    assert tool_policy.granted_call_hint(no_root, "send_file", tree.ws, "grant_file") == (
        "With this goal's grant, send_file may send no file in this turn: it has no root intention."
    )


# ---- the MCP client's identities --------------------------------------------------------------------------------


def test_an_mcp_tool_is_registered_with_its_identity_and_forgotten_with_it(mcp_registry):
    server = uuid.uuid4()
    identity = McpIdentity(server, "a" * 64, "search", server_name="notes", server_host="notes.example")
    note_mcp_tool("mcp__notes__find", background=True, identity=identity)
    assert mcp_tool_identity("mcp__notes__find") == identity
    assert tool_classes.mcp_tool_background("mcp__notes__find") is True
    forget_mcp_tool("mcp__notes__find")
    assert mcp_tool_identity("mcp__notes__find") is None
    assert tool_classes.mcp_tool_background("mcp__notes__find") is None


def test_a_tool_registered_again_without_an_identity_has_none(mcp_registry):
    identity = McpIdentity(uuid.uuid4(), "b" * 64, "send")
    note_mcp_tool("mcp__mail__post", background=False, identity=identity)
    note_mcp_tool("mcp__mail__post", background=False)
    assert mcp_tool_identity("mcp__mail__post") is None


def test_identities_compare_on_the_server_row_the_url_and_the_remote_name_only():
    server = uuid.uuid4()
    one = McpIdentity(server, "c" * 64, "search", server_name="notes", server_host="a.example")
    assert one == McpIdentity(server, "c" * 64, "search", server_name="renamed", server_host="b.example")
    assert one != McpIdentity(uuid.uuid4(), "c" * 64, "search")
    assert one != McpIdentity(server, "d" * 64, "search")
    assert one != McpIdentity(server, "c" * 64, "find")


def test_an_identity_needs_an_mcp_name(mcp_registry):
    with pytest.raises(ValueError):
        note_mcp_tool("send_email", background=True, identity=McpIdentity(uuid.uuid4(), "e" * 64, "x"))
