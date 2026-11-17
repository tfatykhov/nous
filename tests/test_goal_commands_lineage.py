"""A goal that may run any command, through the rows a real goal session writes: the session's first task, a task a
decision turn spawns with the real spawn_task, and a workflow step the real DAG orchestrator launches each derive
their context from the stored subtask row (``ExecutionContext.for_subtask``) and are offered bash that runs any
command, with the description that says so; the same rows of a contained goal are not. Then, on POSIX, a real turn of
such a task runs a command the lineage allowlist refuses, through the real dispatcher and the real bash tool, and sees
the lineage variables only; the same command is refused once the end has passed, in another goal's task, in a
decision turn and in a background turn of another kind."""

from __future__ import annotations

import os
import sys
import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from f099_support import env_factory, runner_env, say, use  # noqa: F401
from goal_grants_baseline import SYNTHETIC_ENV
from goals_support import reset_goal_report_channel  # noqa: F401
from sqlalchemy import select, text, update
from test_goals_driver import _active, _started, _world

from nous import main
from nous.api import tool_policy
from nous.api.builtin_tools import _LINEAGE_BASH_ENV, register_builtin_tools
from nous.api.execution_context import ExecutionContext
from nous.api.runner import AgentRunner
from nous.api.tools import ToolDispatcher, register_subtask_tools
from nous.autonomy import tool_guard
from nous.autonomy.tool_guard import ToolGuard
from nous.brain.intentions import IntentionSpec
from nous.dag.orchestrator import DAGOrchestrator
from nous.dag.schemas import DAGCreateRequest, DAGNodeSpec, DAGNodeType
from nous.dag.store import DAGStore
from nous.goals.grants import GoalToolGrants
from nous.goals.store import GoalStore
from nous.storage.models import Goal, GoalSession, Subtask

pytestmark = pytest.mark.postgres_only

GRANTS = {"autonomy_settings_enabled": True, "tool_guard_enabled": True, "goal_tool_grants_enabled": True}
REFUSED = {"command": "curl https://example.com/data.json"}
NAMES = "env | cut -d= -f1 | sort"  # env is not on the lineage allowlist; names only


async def _mode(db, goal_id, *, left: timedelta = timedelta(hours=2)) -> None:
    """The goal's row in the mode, as an approval writes it."""
    async with db.session() as s:
        now = (await s.execute(text("SELECT clock_timestamp()"))).scalar_one()
        await s.execute(
            update(Goal).where(Goal.id == goal_id).values(mode="commands", mode_hours=24, mode_until=now + left)
        )
        await s.commit()


def _runner_over(settings) -> tuple[AgentRunner, ToolDispatcher]:
    dispatcher = ToolDispatcher()
    for name in ("bash", "read_file", "web_search"):
        dispatcher.register(name, MagicMock(), {"type": "object", "description": f"{name}.", "properties": {}})
    runner = AgentRunner(MagicMock(), None, MagicMock(), settings)
    runner.set_dispatcher(dispatcher)
    return runner, dispatcher


def _bash_description(runner: AgentRunner, ctx: ExecutionContext) -> str | None:
    tools = runner._offered_tools(
        ctx, "task", is_subtask=ctx.kind in ("subtask", "dag_node"), tool_filter=None, refuse_active=False
    )
    bash = next((t for t in tools if t["name"] == "bash"), None)
    return bash["description"] if bash is not None else None


async def _subtask(w, subtask_id) -> Subtask:
    async with w.db.session() as s:
        return (await s.execute(select(Subtask).where(Subtask.id == subtask_id))).scalar_one()


async def _lineage_world(env_factory, *, in_mode: bool):  # noqa: F811
    w = await _world(env_factory, **GRANTS)
    goal = await _active(w)
    if in_mode:
        await _mode(w.db, goal.id)
    tool_guard.install(ToolGuard())
    view = GoalToolGrants(w.db, w.env.settings, commands=True)
    assert await view.refresh()
    w.driver.tool_grants = view
    runner, dispatcher = _runner_over(w.env.settings)
    main._bind_goal_commands(view, runner, dispatcher)
    root, session = await _started(w, goal)
    first = ExecutionContext.for_subtask(await _subtask(w, session.first_subtask_id), "subtask-first")
    return w, goal, view, runner, root, session, first


@pytest.mark.parametrize("in_mode", [True, False])
async def test_the_first_task_of_a_session(env_factory, in_mode):  # noqa: F811
    _w, _goal, _view, runner, root, _session, first = await _lineage_world(env_factory, in_mode=in_mode)
    assert (first.kind, first.authority, first.root_intention_id) == ("subtask", "internal_only", root.id)
    description = _bash_description(runner, first)
    refusal = runner._authorize_tool_call(first, "bash", frozenset({"bash"}), "s", REFUSED)
    if in_mode:
        assert tool_policy.COMMANDS_BASH_NOTE in description and refusal is None
    else:
        assert (description is None or tool_policy.COMMANDS_BASH_NOTE not in description) and refusal is not None


@pytest.mark.parametrize("in_mode", [True, False])
async def test_a_task_a_decision_turn_spawns(env_factory, in_mode):  # noqa: F811
    w, _goal, _view, runner, root, session, _first = await _lineage_world(env_factory, in_mode=in_mode)
    spawning = ToolDispatcher()
    register_subtask_tools(spawning, w.env.heart, w.env.settings, runner=MagicMock())
    decision = ExecutionContext(
        kind="continuation", authority="internal_only", intention_id=root.id, root_intention_id=root.id
    )
    result, is_error = await spawning.dispatch(
        "spawn_task",
        {"task": "Push the fix to a branch", "intent": "Push the fix"},
        session_id="intent-1",
        context=decision,
    )
    assert is_error is False, result
    async with w.db.session() as s:
        rows = list(
            (
                await s.execute(
                    select(Subtask).where(Subtask.agent_id == w.agent, Subtask.id != session.first_subtask_id)
                )
            ).scalars()
        )
    [spawned] = rows
    task = ExecutionContext.for_subtask(spawned, f"subtask-{spawned.id}")
    assert (task.kind, task.authority, task.root_intention_id) == ("subtask", "internal_only", root.id)
    assert task.intention_id != root.id
    description = _bash_description(runner, task)
    if in_mode:
        assert tool_policy.COMMANDS_BASH_NOTE in description
        assert runner._authorize_tool_call(task, "bash", frozenset({"bash"}), "s", REFUSED) is None
    else:
        assert description is None or tool_policy.COMMANDS_BASH_NOTE not in description
    # The decision turn itself stays narrowed.
    assert runner._authorize_tool_call(decision, "bash", frozenset({"bash"}), "s", REFUSED) is not None


@pytest.mark.parametrize("in_mode", [True, False])
async def test_a_workflow_step_the_orchestrator_launches(env_factory, in_mode):  # noqa: F811
    w, _goal, _view, runner, root, _session, _first = await _lineage_world(env_factory, in_mode=in_mode)
    store = DAGStore(w.db, w.agent, w.env.settings)
    dag = await store.create(
        DAGCreateRequest(
            name="ship", nodes=[DAGNodeSpec(name="push", type=DAGNodeType.subtask, instructions="Push the fix")]
        ),
        intention=IntentionSpec(
            intent="Ship the fix", origin_kind="continuation", origin_authority="internal_only", parent_id=root.id
        ),
    )
    loader = AsyncMock()
    loader._registry = None
    orchestrator = DAGOrchestrator(
        store=store, subtask_mgr=w.env.heart.subtasks, dynamic_loader=loader, settings=w.env.settings
    )
    [node] = dag.nodes
    await orchestrator._launch_subtask_node(node, dag)
    async with w.db.session() as s:
        launched = (await s.execute(select(Subtask).where(Subtask.dag_node_id == node.id))).scalar_one()
    step = ExecutionContext.for_subtask(launched, f"subtask-{launched.id}")
    assert (step.kind, step.authority, step.root_intention_id) == ("dag_node", "internal_only", root.id)
    description = _bash_description(runner, step)
    if in_mode:
        assert tool_policy.COMMANDS_BASH_NOTE in description
        assert runner._authorize_tool_call(step, "bash", frozenset({"bash"}), "s", REFUSED) is None
    else:
        assert description is None or tool_policy.COMMANDS_BASH_NOTE not in description


# ---- a real command in a real turn (POSIX) ----------------------------------------------------------------------

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="the bash tool runs /bin/sh")


async def _shell_turn(make_env, tmp_path, *, which: str) -> str:
    """One real turn whose model calls bash with ``NAMES`` once, and the tool result it got back."""
    env = await make_env(
        [use("bash", command=NAMES)],
        [say("Done.")],
        workspace_dir=str(tmp_path),
        telegram_chat_id="",
        f099_lineage_shell=True,
        github_default_repo="settings/repo",
        **GRANTS,
    )
    register_builtin_tools(env.dispatcher, env.settings)
    store = GoalStore(env.db, env.agent, env.settings)
    goals = []
    for _ in range(2):
        goal = await store.create(
            title="Ship the fix",
            objective="Push the fix",
            done_when="Pushed",
            proposed_by="owner",
            origin_channel="telegram:4242",
        )
        goals.append(await store.approve(goal.id, actor="owner"))
    roots = []
    for goal in goals:
        root = uuid.uuid4()
        async with env.db.session() as s:
            s.add(
                GoalSession(
                    agent_id=env.agent,
                    goal_id=goal.id,
                    n=1,
                    trigger="timer",
                    first_subtask_id=uuid.uuid4(),
                    root_id=root,
                )
            )
            await s.commit()
        roots.append(root)
    await _mode(env.db, goals[0].id, left=timedelta(seconds=-1) if which == "ended" else timedelta(hours=2))
    tool_guard.install(ToolGuard())
    view = GoalToolGrants(env.db, env.settings, commands=True)
    assert await view.refresh()
    main._bind_goal_commands(view, env.runner, env.dispatcher)
    kinds = {"decision": "continuation", "background": "background", "step": "dag_node", "chat": "interactive"}
    kind = kinds.get(which, "subtask")
    root = roots[1] if which == "other_goal" else roots[0]
    ctx = ExecutionContext(
        kind=kind, authority="internal_only", session_id="subtask-90", intention_id=uuid.uuid4(), root_intention_id=root
    )
    with patch.dict(os.environ, SYNTHETIC_ENV, clear=True):
        await env.runner.run_turn(
            "subtask-90",
            "list the names",
            is_background=kind != "interactive",
            is_subtask=kind in ("subtask", "dag_node"),
            context=ctx,
        )
    return _tool_result_text(env.model.calls[1])


def _tool_result_text(call: dict) -> str:
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


@posix_only
@pytest.mark.parametrize("which", ["task", "step"])
async def test_a_task_of_the_goal_runs_a_command_the_allowlist_refuses_and_sees_the_lineage_names(
    runner_env,  # noqa: F811
    tmp_path,
    which,
):
    result = await _shell_turn(runner_env, tmp_path, which=which)
    assert "Exit code: 0" in result, result
    names = {line.strip() for line in result.splitlines() if line.strip() and "=" not in line and " " not in line}
    assert {"GITHUB_TOKEN", "GH_TOKEN", "GH_HOST", "GH_REPO", "PATH", "HOME"} <= names
    for secret in ("DB_PASSWORD", "DATABASE_URL", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "NOUS_TELEGRAM_BOT_TOKEN"):
        assert secret not in names
    assert not [name for name in names if name.startswith("GOOGLE_") or name == "NOUS_SYNTHETIC_OTHER"]
    assert not (names & set(SYNTHETIC_ENV)) - _LINEAGE_BASH_ENV - {"GH_REPO"}


@posix_only
@pytest.mark.parametrize("which", ["ended", "other_goal", "decision", "background", "chat"])
async def test_the_same_command_is_refused_elsewhere(runner_env, tmp_path, which):  # noqa: F811
    result = await _shell_turn(runner_env, tmp_path, which=which)
    assert result.startswith("Tool error: 'bash' is not allowed"), result
    assert "GITHUB_TOKEN" not in result
