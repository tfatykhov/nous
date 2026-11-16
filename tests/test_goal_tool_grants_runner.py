"""Per-goal tool grants in the runner: through the real ``_offered_tools`` and ``_authorize_tool_call``, a tool the
owner allowed a goal is offered to, and authorized in, exactly the turns of that goal's open session's lineage (a chain
decision, a task or a workflow step that runs as a task, internal_only, rooted at the session), in every enforcement
mode; every other check still refuses it where it did; and with no grant hook, or with the hook and no grant, the
runner decides as recorded. Also the flag's validator and the refresh points the wiring adds."""

from __future__ import annotations

import asyncio
import logging
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
from f099_support import (
    SEND_EMAIL_ARGS,
    env_factory,  # noqa: F401
    register_send_email,
    runner_env,  # noqa: F401
    say,
    use,
)
from goal_grants_baseline import TOOLS, collect, load, workspace
from sqlalchemy import select, text

from nous import main
from nous.api import tool_classes, tool_policy
from nous.api.execution_context import CONTEXT_KINDS, ExecutionContext
from nous.api.runner import AgentRunner
from nous.api.telegram_tools import _SEND_FILE_SCHEMA, create_send_file_tool
from nous.api.tool_classes import McpIdentity
from nous.api.tools import ToolDispatcher
from nous.autonomy import tool_guard
from nous.autonomy.tool_guard import ToolGuard
from nous.config import Settings
from nous.goals.grants import GoalToolGrants
from nous.goals.store import GoalStore
from nous.handlers.continuation_runner import ArrivalState, make_propose_action_executor
from nous.storage.models import Goal, GoalJournal, GoalSession

pytestmark = pytest.mark.postgres_only

ON = "mcp__notes__search"  # granted, its server available to background work
OFF = "mcp__mail__send"  # granted, its server not available to background work
GONE = "mcp__gone__thing"  # granted, not registered with the MCP client now
MOVED = "mcp__moved__find"  # granted, its server re-pointed since
NOTES = McpIdentity(uuid.uuid4(), "a1" * 32, "search", server_name="notes", server_host="notes.example.com")
MAIL = McpIdentity(uuid.uuid4(), "b2" * 32, "send", server_name="mail", server_host="mail.example.com")
GONE_ID = McpIdentity(uuid.uuid4(), "c3" * 32, "thing", server_name="gone", server_host="gone.example.com")
MOVED_OLD = McpIdentity(uuid.uuid4(), "d4" * 32, "find", server_name="moved", server_host="moved.example.com")
MOVED_NEW = McpIdentity(uuid.uuid4(), "d4" * 32, "find", server_name="moved", server_host="moved.example.com")
MATRIX_TOOLS = ("send_email", "send_file", "bash", "write_file", ON, OFF, GONE, MOVED)
LIFTED_TOOLS = {"send_email", ON}
MODES = ("off", "warn", "enforce")


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _settings(agent: str, **over) -> Settings:
    values = {
        "_env_file": None,
        "ANTHROPIC_API_KEY": "test-key",
        "agent_id": agent,
        "workspace_dir": workspace(),
        "f026_persistence_enabled": False,
        "telegram_chat_id": "",
        "goals_tick_seconds": 60,
        "tool_offered_set_enforcement_mode": "warn",
        "tool_context_policy_mode": "warn",
        **over,
    }
    return Settings(**values)


async def _goal(store: GoalStore) -> Goal:
    goal = await store.create(
        title="Tidy the notes",
        objective="Merge the duplicate notes",
        done_when="None are left",
        proposed_by="owner",
        origin_channel="telegram:4242",
    )
    return await store.approve(goal.id, actor="owner")


async def _session(db, store: GoalStore, goal: Goal, n: int = 1) -> uuid.UUID:
    root = uuid.uuid4()
    async with db.session() as s:
        s.add(
            GoalSession(
                agent_id=store.agent_id,
                goal_id=goal.id,
                n=n,
                trigger="timer",
                first_subtask_id=uuid.uuid4(),
                root_id=root,
            )
        )
        await s.commit()
    return root


@pytest.fixture
def mcp_registry(monkeypatch):
    monkeypatch.setattr(tool_classes, "_MCP_TOOLS", {ON: True, OFF: False, MOVED: True})
    monkeypatch.setattr(tool_classes, "_MCP_IDENTITIES", {ON: NOTES, OFF: MAIL, MOVED: MOVED_NEW})


@pytest.fixture
async def granted(db, mcp_registry):
    """A runner over every tool, a goal with an open session whose grants are send_email and four MCP tools, a
    second goal with an open session and none, the empty tool guard, and the view bound to the runner."""
    agent = f"grant-runner-{uuid.uuid4().hex[:8]}"
    settings = _settings(agent)
    dispatcher = ToolDispatcher()
    for name in (*TOOLS, MOVED):
        dispatcher.register(name, MagicMock(), {"type": "object", "properties": {}})
    runner = AgentRunner(MagicMock(), None, MagicMock(), settings)
    runner.set_dispatcher(dispatcher)
    store = GoalStore(db, agent, settings)
    goal, other = await _goal(store), await _goal(store)
    root, other_root = await _session(db, store, goal), await _session(db, store, other)
    await store.grant_tool(goal.id, "send_email", None, actor="owner", proof="owner")
    for tool, identity in ((ON, NOTES), (OFF, MAIL), (GONE, GONE_ID), (MOVED, MOVED_OLD)):
        await store.grant_tool(goal.id, tool, identity, actor="owner", proof="owner")
    tool_guard.install(ToolGuard())
    clock = Clock()
    view = GoalToolGrants(db, settings, clock=clock)
    assert await view.refresh()
    runner.set_goal_lift(view.lifts, view.note_used)
    world = SimpleNamespace(
        db=db,
        settings=settings,
        runner=runner,
        store=store,
        goal=goal,
        other=other,
        root=root,
        other_root=other_root,
        view=view,
        clock=clock,
        agent=agent,
    )
    yield world
    await asyncio.gather(*view._tasks, return_exceptions=True)


def _ctx(kind: str, authority: str, root: uuid.UUID | None, tool: str = "send_email", **extra) -> ExecutionContext:
    if root is not None:
        extra = {"intention_id": uuid.uuid4(), "root_intention_id": root, **extra}
    if kind == "approved_action":
        extra = {"proposal_id": uuid.uuid4(), "declared_tools": (tool,), **extra}
    return ExecutionContext(kind=kind, authority=authority, session_id="subtask-1", **extra)


def _contexts(w, tool: str):
    for kind in CONTEXT_KINDS:
        for authority in ("owner", "internal_only"):
            for name, root in (
                ("goal", w.root),
                ("other goal", w.other_root),
                ("ordinary", uuid.uuid4()),
                ("none", None),
            ):
                if kind == "continuation" and (authority != "internal_only" or root is None):
                    continue
                yield f"{kind}|{authority}|{name}", _ctx(kind, authority, root, tool)


def _offered(runner, ctx, **kw) -> list[str]:
    is_subtask = ctx.kind in ("subtask", "dag_node")
    tools = runner._offered_tools(ctx, "task", is_subtask=is_subtask, tool_filter=None, refuse_active=False, **kw)
    return [t["name"] for t in tools]


def _observe(w, ctx, tool: str) -> tuple[bool, dict]:
    names = frozenset({tool}) if ctx.kind == "approved_action" else frozenset(_offered(w.runner, ctx))
    refusals = {}
    for mode in MODES:
        object.__setattr__(w.settings, "tool_offered_set_enforcement_mode", mode)
        object.__setattr__(w.settings, "tool_context_policy_mode", mode)
        refusal = w.runner._authorize_tool_call(ctx, tool, names, ctx.session_id, {})
        refusals[mode] = None if refusal is None else (refusal.code, refusal.text)
    object.__setattr__(w.settings, "tool_offered_set_enforcement_mode", "warn")
    object.__setattr__(w.settings, "tool_context_policy_mode", "warn")
    return tool in names, refusals


# ---- the kind matrix --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("tool", MATRIX_TOOLS)
async def test_the_kind_matrix(granted, tool):
    """Every kind, authority and root: with the view bound, a granted tool is offered and authorized exactly in a
    turn of the goal's own lineage; every other turn is decided as with no hook at all."""
    w = granted
    lifted_seen = 0
    for key, ctx in _contexts(w, tool):
        with_hook = _observe(w, ctx, tool)
        w.runner.set_goal_lift(None)
        without = _observe(w, ctx, tool)
        w.runner.set_goal_lift(w.view.lifts, w.view.note_used)
        expected = (
            ctx.kind in tool_policy.GRANT_KINDS
            and ctx.authority == "internal_only"
            and ctx.root_intention_id == w.root
            and tool in LIFTED_TOOLS
        )
        if expected:
            lifted_seen += 1
            assert with_hook == (True, dict.fromkeys(MODES)), key
            offered, refusals = without
            assert not offered and all(refusal is not None for refusal in refusals.values()), key
        else:
            assert with_hook == without, key
    assert lifted_seen == (len(tool_policy.GRANT_KINDS) if tool in LIFTED_TOOLS else 0)


async def test_a_workflow_check_step_with_the_goals_stamp_is_not_lifted(granted):
    w = granted
    check = _ctx("heartbeat_check", "internal_only", w.root)
    assert "send_email" not in _offered(w.runner, check)
    refusal = w.runner._authorize_tool_call(check, "send_email", frozenset({"send_email"}), "s", {})
    assert refusal is not None


async def test_an_approved_action_with_the_goals_root_runs_as_before(granted):
    w = granted
    approved = _ctx("approved_action", "internal_only", w.root, "send_email")
    with_hook = _observe(w, approved, "send_email")
    w.runner.set_goal_lift(None)
    assert _observe(w, approved, "send_email") == with_hook


# ---- each row of the precedence table ---------------------------------------------------------------------------


def _goal_turn(w, kind: str = "continuation") -> ExecutionContext:
    return _ctx(kind, "internal_only", w.root)


async def test_a_cancelled_root_is_refused_first(granted):
    w = granted
    w.runner.set_cancelled_roots(lambda root: root == w.root)
    refusal = w.runner._authorize_tool_call(_goal_turn(w), "send_email", frozenset({"send_email"}), "s", {})
    assert refusal.code == "root_cancelled"


@pytest.mark.parametrize(
    ("guard", "text"),
    [
        (ToolGuard(locked=frozenset({"send_email"})), "locked by the server"),
        (ToolGuard(hold_all=True), "could not be read"),
        (ToolGuard(denied=frozenset({"send_email"})), "switched off for background work"),
    ],
    ids=["host lock", "not read", "owner switched it off"],
)
async def test_the_guard_still_holds_a_granted_tool(granted, guard, text):
    w = granted
    tool_guard.install(guard)
    for kind in tool_policy.GRANT_KINDS:
        ctx = _goal_turn(w, kind)
        assert "send_email" not in _offered(w.runner, ctx)
        refusal = w.runner._authorize_tool_call(ctx, "send_email", frozenset({"send_email"}), "s", {})
        assert refusal.code == "tool_guard" and text in refusal.text


async def test_with_no_guard_installed_nothing_is_lifted(granted):
    w = granted
    tool_guard.install(None)
    assert "send_email" not in _offered(w.runner, _goal_turn(w))
    refusal = w.runner._authorize_tool_call(_goal_turn(w), "send_email", frozenset({"send_email"}), "s", {})
    assert refusal.code == "internal_only"


@pytest.mark.parametrize("tool", [OFF, GONE, MOVED])
async def test_an_mcp_grant_is_refused_when_its_server_is_off_gone_or_moved(granted, tool):
    w = granted
    for kind in tool_policy.GRANT_KINDS:
        ctx = _goal_turn(w, kind)
        assert tool not in _offered(w.runner, ctx)
        refusal = w.runner._authorize_tool_call(ctx, tool, frozenset(_offered(w.runner, ctx)), "s", {})
        assert refusal is not None


async def test_the_mcp_floor_refuses_a_granted_tool_of_a_server_off_even_when_named_offered(granted):
    w = granted
    refusal = w.runner._authorize_tool_call(_goal_turn(w), OFF, frozenset({OFF}), "s", {})
    assert refusal.code == "context_policy" and "may not use it" in refusal.text


async def test_a_refuse_censor_and_a_tool_filter_still_take_a_granted_tool_out(granted):
    w = granted
    ctx = _goal_turn(w)
    refused = w.runner._offered_tools(ctx, "task", is_subtask=False, tool_filter=None, refuse_active=True)
    assert "send_email" not in [t["name"] for t in refused]
    filtered = w.runner._offered_tools(ctx, "task", is_subtask=False, tool_filter=["web_search"], refuse_active=False)
    assert [t["name"] for t in filtered] == ["web_search"]


async def test_an_undoable_workflow_step_still_refuses_a_send(granted):
    w = granted
    ctx = _ctx("dag_node", "internal_only", w.root, undoable=True)
    refusal = w.runner._authorize_tool_call(ctx, "send_email", frozenset({"send_email"}), "s", {})
    assert refusal.code == "context_policy" and "not_compensable" in refusal.text


async def test_a_paused_goal_an_ended_session_a_finished_goal_and_a_stale_view_lift_nothing(granted):
    w = granted
    ctx = _goal_turn(w)

    def refused() -> bool:
        refusal = w.runner._authorize_tool_call(ctx, "send_email", frozenset({"send_email"}), "s", {})
        return refusal is not None and "send_email" not in _offered(w.runner, ctx)

    assert not refused()
    await w.store.pause(w.goal.id, actor="owner")
    await w.view.refresh_goal(w.goal.id)
    assert refused()
    await w.store.resume(w.goal.id, actor="owner")
    await w.view.refresh_goal(w.goal.id)
    assert not refused()  # the grant stays and applies again on resume
    w.clock.now += 10_000
    assert refused()
    await w.view.refresh()
    assert not refused()
    async with w.db.session() as s:
        await s.execute(
            text("UPDATE brain.goal_sessions SET ended_at = now(), outcome = 'no_step' WHERE root_id = :r"),
            {"r": w.root},
        )
        await s.commit()
    await w.view.refresh()
    assert refused()
    await w.store.cancel(w.goal.id, actor="owner")
    await w.view.refresh()
    assert refused()


async def test_a_revoke_between_two_calls_of_one_turn_refuses_the_second(granted):
    w = granted
    ctx = _goal_turn(w)
    offered = frozenset(_offered(w.runner, ctx))
    assert "send_email" in offered
    assert w.runner._authorize_tool_call(ctx, "send_email", offered, "s", {}) is None
    await w.store.revoke_tool(w.goal.id, "send_email", actor="owner", proof="owner")
    await w.view.refresh_goal(w.goal.id)
    refusal = w.runner._authorize_tool_call(ctx, "send_email", offered, "s", {})  # still in the turn's offered set
    assert refusal.code == "internal_only" and "(external)" in refusal.text


async def test_a_fork_has_the_hooks_and_a_later_one_reaches_it(granted):
    w = granted
    fork = w.runner.fork(MagicMock())
    assert "send_email" in _offered(fork, _goal_turn(w))
    w.runner.set_goal_lift(None)
    assert "send_email" not in _offered(fork, _goal_turn(w))
    w.runner.set_goal_lift(w.view.lifts, w.view.note_used)
    assert "send_email" in _offered(fork, _goal_turn(w))


async def _progress_rows(db, goal_id) -> list:
    async with db.session() as s:
        query = select(GoalJournal).where(GoalJournal.goal_id == goal_id, GoalJournal.kind == "progress")
        return list((await s.execute(query)).scalars())


async def test_authorizing_a_lifted_call_writes_no_first_use_row(granted):
    """The row is written when the call is dispatched (``_dispatch_with_ledger``), not when it is authorized."""
    w = granted
    ctx = _goal_turn(w)
    assert w.runner._authorize_tool_call(ctx, "send_email", frozenset({"send_email"}), "s", {}) is None
    assert w.view.lifts(ctx, "send_email") is True
    tool_guard.install(ToolGuard(denied=frozenset({ON})))
    assert w.runner._authorize_tool_call(ctx, ON, frozenset({ON}), "s", {}) is not None  # refused by the guard
    await asyncio.gather(*w.view._tasks)
    assert await _progress_rows(w.db, w.goal.id) == []


async def test_a_granted_tool_can_still_be_proposed_as_before(granted):
    """propose_action is not lifted: a chain may still stage a proposal of a tool its goal was given."""
    w = granted
    ctx = _goal_turn(w)
    staged: list[str] = []

    async def stage(tool, arguments, rationale):
        staged.append(tool)
        return uuid.uuid4()

    dispatcher = w.runner._dispatcher
    dispatcher.validate_call = lambda tool, arguments: []
    results = []
    for hook in ((w.view.lifts, w.view.note_used), (None,)):
        w.runner.set_goal_lift(*hook)
        propose = make_propose_action_executor(ArrivalState(), ctx=ctx, dispatcher=dispatcher, stage=stage)
        results.append(await propose(tool="send_email", arguments=dict(SEND_EMAIL_ARGS), rationale="Send it."))
    for result, is_error in results:
        assert is_error is False and result.startswith("Staged proposal ") and "(send_email)" in result
    assert staged == ["send_email", "send_email"]


async def test_a_real_turn_of_a_goal_task_calls_the_granted_tool(runner_env):  # noqa: F811
    env = await runner_env([use("send_email", **SEND_EMAIL_ARGS)], [say("Sent.")], telegram_chat_id="")
    calls = register_send_email(env)
    store = GoalStore(env.db, env.agent, env.settings)
    goal = await _goal(store)
    root = await _session(env.db, store, goal)
    await store.grant_tool(goal.id, "send_email", None, actor="owner", proof="owner")
    tool_guard.install(ToolGuard())
    view = GoalToolGrants(env.db, env.settings)
    await view.refresh()
    env.runner.set_goal_lift(view.lifts, view.note_used)
    ctx = ExecutionContext(
        kind="subtask", authority="internal_only", session_id="subtask-77", intention_id=root, root_intention_id=root
    )
    await env.runner.run_turn("subtask-77", "send the note", is_background=True, is_subtask=True, context=ctx)
    assert calls == [SEND_EMAIL_ARGS]
    assert "send_email" in [tool["name"] for tool in env.model.calls[0]["tools"]]
    await asyncio.gather(*view._tasks)
    async with env.db.session() as s:
        row = (
            await s.execute(select(GoalJournal).where(GoalJournal.goal_id == goal.id, GoalJournal.kind == "progress"))
        ).scalar_one()
    assert row.text == "Session 1 was allowed to use send_email without asking, as you set for this goal."


async def test_a_real_turn_after_the_grant_is_removed_does_not_call_it(runner_env):  # noqa: F811
    env = await runner_env([use("send_email", **SEND_EMAIL_ARGS)], [say("I stop.")], telegram_chat_id="")
    calls = register_send_email(env)
    store = GoalStore(env.db, env.agent, env.settings)
    goal = await _goal(store)
    root = await _session(env.db, store, goal)
    await store.grant_tool(goal.id, "send_email", None, actor="owner", proof="owner")
    await store.revoke_tool(goal.id, "send_email", actor="owner", proof="owner")
    tool_guard.install(ToolGuard())
    view = GoalToolGrants(env.db, env.settings)
    await view.refresh()
    env.runner.set_goal_lift(view.lifts, view.note_used)
    ctx = ExecutionContext(
        kind="subtask", authority="internal_only", session_id="subtask-78", intention_id=root, root_intention_id=root
    )
    await env.runner.run_turn("subtask-78", "send the note", is_background=True, is_subtask=True, context=ctx)
    assert calls == []
    assert "send_email" not in [tool["name"] for tool in env.model.calls[0]["tools"]]


# ---- a granted send_file in a real turn --------------------------------------------------------------------------


class _Telegram:
    """A stand-in for the Telegram API behind the real send_file tool: it keeps each upload and answers ok."""

    def __init__(self) -> None:
        self.uploads: list[bytes] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.uploads.append(request.read())
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})


def _tool_result_text(call: dict) -> str:
    """The text of the last tool result in one model call's messages."""
    for message in reversed(call["messages"]):
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    inner = block.get("content")
                    if isinstance(inner, list):
                        return " ".join(part.get("text", "") for part in inner if isinstance(part, dict))
                    return str(inner)
    raise AssertionError("no tool result in the call")


async def _file_turn(make_env, tmp_path, file_path_of, *, cleanup=None):
    """One real subtask turn of a goal with send_file granted, whose model calls send_file once with the path
    ``file_path_of(root_dir)``. Returns ``(telegram, the tool result's text, the root's directory)``."""
    workspace_dir = tmp_path / "ws"
    workspace_dir.mkdir()
    holder: dict = {}

    async def call_send_file(_kwargs):
        args = {"file_path": file_path_of(holder["root_dir"])}
        if cleanup is not None:
            args["cleanup"] = cleanup
        return [use("send_file", **args)]

    env = await make_env(
        call_send_file,
        [say("Done.")],
        workspace_dir=str(workspace_dir),
        telegram_bot_token="test-bot",
        telegram_chat_id="4242",
    )
    telegram = _Telegram()
    client = httpx.AsyncClient(transport=httpx.MockTransport(telegram))
    env.dispatcher.register("send_file", create_send_file_tool(env.settings, client), _SEND_FILE_SCHEMA)
    store = GoalStore(env.db, env.agent, env.settings)
    goal = await _goal(store)
    root = await _session(env.db, store, goal)
    root_dir = workspace_dir.resolve() / "intentions" / str(root)
    root_dir.mkdir(parents=True)
    (root_dir / "report.txt").write_bytes(b"the weekly report")
    (tmp_path / "secret.txt").write_bytes(b"not for sending")
    holder["root_dir"] = root_dir
    await store.grant_tool(goal.id, "send_file", None, actor="owner", proof="owner")
    tool_guard.install(ToolGuard())
    view = GoalToolGrants(env.db, env.settings)
    await view.refresh()
    env.runner.set_goal_lift(view.lifts, view.note_used)
    ctx = ExecutionContext(
        kind="subtask", authority="internal_only", session_id="subtask-81", intention_id=root, root_intention_id=root
    )
    try:
        await env.runner.run_turn("subtask-81", "send the report", is_background=True, is_subtask=True, context=ctx)
    finally:
        await client.aclose()
        await asyncio.gather(*view._tasks)
    assert "send_file" in [tool["name"] for tool in env.model.calls[0]["tools"]]
    return telegram, _tool_result_text(env.model.calls[1]), root_dir


async def test_a_granted_send_file_sends_a_file_under_the_roots_directory(runner_env, tmp_path):  # noqa: F811
    telegram, result, root_dir = await _file_turn(runner_env, tmp_path, lambda root_dir: str(root_dir / "report.txt"))
    assert len(telegram.uploads) == 1 and b"the weekly report" in telegram.uploads[0]
    assert "File sent successfully: report.txt" in result
    assert (root_dir / "report.txt").exists()


@pytest.mark.parametrize("where", ["outside", "dotdot", "relative"])
async def test_a_granted_send_file_of_another_path_is_refused(runner_env, tmp_path, where):  # noqa: F811
    paths = {
        "outside": lambda root_dir: str(tmp_path / "secret.txt"),
        "dotdot": lambda root_dir: str(root_dir) + "/../../../secret.txt",
        "relative": lambda root_dir: f"intentions/{root_dir.name}/report.txt",
    }
    telegram, result, root_dir = await _file_turn(runner_env, tmp_path, paths[where])
    assert telegram.uploads == []
    assert result == (
        "Tool error: 'send_file' is not allowed in this turn (grant_file). With this goal's grant, send_file sends "
        f"only a regular file under {root_dir}/, named by its full path, with no symbolic link and no '..' in it."
    )


async def test_a_granted_send_file_that_would_delete_the_file_is_refused(runner_env, tmp_path):  # noqa: F811
    telegram, result, root_dir = await _file_turn(
        runner_env, tmp_path, lambda root_dir: str(root_dir / "report.txt"), cleanup=True
    )
    assert telegram.uploads == []
    assert result == (
        "Tool error: 'send_file' is not allowed in this turn (grant_cleanup). With this goal's grant, send_file may "
        "not delete the file it sends: leave cleanup out."
    )
    assert (root_dir / "report.txt").read_bytes() == b"the weekly report"


# ---- byte-for-byte with no grants -------------------------------------------------------------------------------


async def test_with_the_hook_bound_and_no_grant_the_runner_decides_as_recorded(db):
    agent = f"grant-parity-{uuid.uuid4().hex[:8]}"
    view = GoalToolGrants(db, _settings(agent))
    assert await view.refresh()

    def prepare(runner: AgentRunner) -> None:
        tool_guard.install(ToolGuard())
        runner.set_goal_lift(view.lifts, view.note_used)

    _commit, baseline = load()
    assert collect(prepare) == baseline


# ---- the flag ---------------------------------------------------------------------------------------------------

ALL_ON = {
    "goals_enabled": True,
    "continuation_enabled": True,
    "intentions_enabled": True,
    "result_inbox_enabled": True,
    "subtask_enabled": True,
    "autonomy_settings_enabled": True,
    "tool_guard_enabled": True,
}


def test_the_flag_is_off_by_default():
    assert Settings(_env_file=None).goal_tool_grants_enabled is False


def test_the_flag_stays_on_with_everything_it_needs():
    assert Settings(_env_file=None, goal_tool_grants_enabled=True, **ALL_ON).goal_tool_grants_enabled is True


@pytest.mark.parametrize(
    ("missing", "named"),
    [
        ({"goals_enabled": False}, ["NOUS_GOALS_ENABLED=true"]),
        ({"autonomy_settings_enabled": False}, ["NOUS_AUTONOMY_SETTINGS_ENABLED=true", "NOUS_TOOL_GUARD_ENABLED=true"]),
        ({"tool_guard_enabled": False}, ["NOUS_TOOL_GUARD_ENABLED=true"]),
        ({"subtask_enabled": False}, ["NOUS_GOALS_ENABLED=true"]),
        (
            {"goals_enabled": False, "autonomy_settings_enabled": False, "tool_guard_enabled": False},
            ["NOUS_GOALS_ENABLED=true", "NOUS_AUTONOMY_SETTINGS_ENABLED=true", "NOUS_TOOL_GUARD_ENABLED=true"],
        ),
    ],
)
def test_the_flag_is_forced_off_naming_what_it_needs(caplog, missing, named):
    with caplog.at_level(logging.WARNING):
        settings = Settings(_env_file=None, goal_tool_grants_enabled=True, **{**ALL_ON, **missing})
    assert settings.goal_tool_grants_enabled is False
    [line] = [r.getMessage() for r in caplog.records if "NOUS_GOAL_TOOL_GRANTS_ENABLED" in r.getMessage()]
    assert line == f"NOUS_GOAL_TOOL_GRANTS_ENABLED=true needs {' and '.join(named)}; goal tool grants stay OFF."


# ---- the wiring ------------------------------------------------------------------------------------------------


class _Runner:
    def __init__(self) -> None:
        self.bound = []

    def set_goal_lift(self, lift, note_use=None) -> None:
        self.bound.append((lift, note_use))

    async def close(self) -> None:
        return None


class _Continuation:
    def set_goal_turns(self, turns) -> None:
        self.turns = turns

    def set_goals(self, goals) -> None:
        self.goals = goals


def test_build_goals_binds_the_view_only_with_the_flag(db):
    on = Settings(_env_file=None, goal_tool_grants_enabled=True, **ALL_ON)
    runner = _Runner()
    driver = main._build_goals(on, database=db, heart=MagicMock(), continuation_runner=_Continuation(), runner=runner)
    view = driver.tool_grants
    assert isinstance(view, GoalToolGrants)
    assert runner.bound == [(view.lifts, view.note_used)]
    off = Settings(_env_file=None, **ALL_ON)
    runner = _Runner()
    driver = main._build_goals(off, database=db, heart=MagicMock(), continuation_runner=_Continuation(), runner=runner)
    assert driver.tool_grants is None and runner.bound == []


async def test_shutdown_unbinds_the_hook():
    runner = _Runner()
    await main.shutdown_components({"goal_tool_grants": object(), "runner": runner})
    assert runner.bound == [(None, None)]


async def test_action_gate_still_blocks_a_granted_call_in_a_real_turn(runner_env):  # noqa: F811
    from nous.cognitive.action_gate import GateResult

    env = await runner_env([use("send_email", **SEND_EMAIL_ARGS)], [say("I stop.")], telegram_chat_id="")
    calls = register_send_email(env)
    store = GoalStore(env.db, env.agent, env.settings)
    goal = await _goal(store)
    root = await _session(env.db, store, goal)
    await store.grant_tool(goal.id, "send_email", None, actor="owner", proof="owner")
    tool_guard.install(ToolGuard())
    view = GoalToolGrants(env.db, env.settings)
    await view.refresh()
    env.runner.set_goal_lift(view.lifts, view.note_used)
    checked: list[str] = []

    class Blocking:
        async def check(self, tool_name, tool_input, ledger, user_message=""):
            checked.append(tool_name)
            return GateResult(approved=False, reason="not now")

    env.runner._action_gate = Blocking()
    object.__setattr__(env.settings, "action_gating_mode", "enforce")
    ctx = ExecutionContext(
        kind="subtask", authority="internal_only", session_id="subtask-79", intention_id=root, root_intention_id=root
    )
    await env.runner.run_turn("subtask-79", "send the note", is_background=True, is_subtask=True, context=ctx)
    assert checked == ["send_email"] and calls == []
    assert "BLOCKED by ActionGate" in str(env.model.calls[-1]["messages"])
    await asyncio.gather(*view._tasks)
    assert await _progress_rows(env.db, goal.id) == []  # the call never ran: no row says the session used it
