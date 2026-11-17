"""A goal that may run any command, in the runner and the dispatcher: through the real ``_offered_tools`` and
``_authorize_tool_call``, bash without the lineage allowlist is offered to, and authorized in, exactly a task or a
workflow step of that goal's open session (internal_only, rooted at the session), in every enforcement mode, with the
lineage shell on or off; every other check still refuses it where it did; a bash call the dispatcher sends from such a
turn gets the lineage environment (never the build one) and no stdin; with no hook, or with the hook and every goal
contained, the runner, the tool loop, the dispatcher and the bash tool act as recorded. Also the flag's validator and
the binding of the one hook."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from f099_support import env_factory, runner_env  # noqa: F401
from goal_grants_baseline import (
    SYNTHETIC_ENV,
    TOOLS,
    _Process,
    _Recorder,
    _Shell,
    collect,
    collect_seams,
    load,
    load_seams,
    stated_difference,
    workspace,
)
from sqlalchemy import select, text, update

from nous import main
from nous.api import builtin_tools, tool_policy
from nous.api.builtin_tools import (
    _BASH_SCHEMA,
    _LINEAGE_BASH_ENV,
    _LINEAGE_BUILD_ENV,
    bash_waits,
    register_builtin_tools,
)
from nous.api.execution_context import BACKGROUND_BASH_TIMEOUT, CONTEXT_KINDS, ExecutionContext
from nous.api.runner import AgentRunner
from nous.api.tools import ToolDispatcher, register_heartbeat_tools
from nous.autonomy import tool_guard
from nous.autonomy.tool_guard import ToolGuard
from nous.config import Settings
from nous.goals.grants import GoalToolGrants
from nous.goals.store import GoalStore
from nous.storage.models import Goal, GoalSession

pytestmark = pytest.mark.postgres_only

MODES = ("off", "warn", "enforce")
REFUSED = {"command": "curl https://example.com/data.json"}  # not on the lineage allowlist
STANDING = (
    "heartbeat_check_create",
    "heartbeat_check_manage",
    "schedule_task",
    "create_censor",
    "learn_skill",
    "store_identity",
    "dag_manage",
    "push_surface",
    "compose_surface",
)
INSIDE_ONLY = ("submit_final_report", "resolve_intention", "propose_action", "goal_note", "goal_plan")
# The settings a real turn of a goal's task runs with here: the flags the mode needs.
RUNNER_FLAGS = {"workspace_dir": workspace(), "f099_lineage_shell": True}


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
        "f099_lineage_shell": True,
        "f099_lineage_build_commands": True,
        "tool_reason_enabled": False,
        "tool_offered_set_enforcement_mode": "warn",
        "tool_context_policy_mode": "warn",
        "github_default_repo": "settings/repo",
        **over,
    }
    return Settings(**values)


async def _goal(store: GoalStore) -> Goal:
    goal = await store.create(
        title="Ship the fix",
        objective="Push the fix to a branch",
        done_when="The branch is pushed",
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


async def _mode(db, goal_id, *, left: timedelta = timedelta(hours=2)) -> None:
    """The goal's row in the mode, as an approval writes it."""
    async with db.session() as s:
        now = (await s.execute(text("SELECT clock_timestamp()"))).scalar_one()
        await s.execute(
            update(Goal).where(Goal.id == goal_id).values(mode="commands", mode_hours=24, mode_until=now + left)
        )
        await s.commit()


async def _contained(db, goal_id) -> None:
    async with db.session() as s:
        await s.execute(
            update(Goal).where(Goal.id == goal_id).values(mode="contained", mode_hours=None, mode_until=None)
        )
        await s.commit()


@pytest.fixture
async def world(db):
    """A runner over every tool and a dispatcher of recording bash; a goal in the mode with an open session, a
    contained goal with one; the empty tool guard; the view bound to both as main.py binds it."""
    agent = f"commands-runner-{uuid.uuid4().hex[:8]}"
    settings = _settings(agent)
    dispatcher = ToolDispatcher()
    for name in TOOLS:
        dispatcher.register(name, MagicMock(), {"type": "object", "properties": {}})
    dispatcher.register("bash", _Recorder(), _BASH_SCHEMA)
    runner = AgentRunner(MagicMock(), None, MagicMock(), settings)
    runner.set_dispatcher(dispatcher)
    store = GoalStore(db, agent, settings)
    goal, contained = await _goal(store), await _goal(store)
    root, contained_root = await _session(db, store, goal), await _session(db, store, contained)
    await _mode(db, goal.id)
    tool_guard.install(ToolGuard())
    clock = Clock()
    view = GoalToolGrants(db, settings, clock=clock, commands=True)
    assert await view.refresh()
    runner.set_goal_lift(view.lifts, view.note_used)
    main._bind_goal_commands(view, runner, dispatcher)
    yield SimpleNamespace(
        db=db,
        settings=settings,
        runner=runner,
        dispatcher=dispatcher,
        store=store,
        goal=goal,
        contained=contained,
        root=root,
        contained_root=contained_root,
        view=view,
        clock=clock,
    )


def _ctx(kind: str, authority: str, root: uuid.UUID | None, **extra) -> ExecutionContext:
    if root is not None:
        extra = {"intention_id": uuid.uuid4(), "root_intention_id": root, **extra}
    if kind == "approved_action":
        extra = {"proposal_id": uuid.uuid4(), "declared_tools": ("bash",), **extra}
    return ExecutionContext(kind=kind, authority=authority, session_id="subtask-1", **extra)


def _task(w, kind: str = "subtask", **extra) -> ExecutionContext:
    return _ctx(kind, "internal_only", w.root, **extra)


def _offered(runner, ctx) -> list[dict]:
    is_subtask = ctx.kind in ("subtask", "dag_node")
    return runner._offered_tools(ctx, "task", is_subtask=is_subtask, tool_filter=None, refuse_active=False)


def _bash_of(runner, ctx) -> dict | None:
    return next((t for t in _offered(runner, ctx) if t["name"] == "bash"), None)


def _observe(w, ctx, tool: str = "bash", tool_input: dict | None = None) -> tuple:
    """(the offered bash's description or None, the refusal in each mode)."""
    offered = _offered(w.runner, ctx)
    names = frozenset({tool}) if ctx.kind == "approved_action" else frozenset(t["name"] for t in offered)
    refusals = {}
    for mode in MODES:
        object.__setattr__(w.settings, "tool_offered_set_enforcement_mode", mode)
        object.__setattr__(w.settings, "tool_context_policy_mode", mode)
        call = REFUSED if tool_input is None else tool_input
        refusal = w.runner._authorize_tool_call(ctx, tool, names, ctx.session_id, dict(call))
        refusals[mode] = None if refusal is None else (refusal.code, refusal.text)
    object.__setattr__(w.settings, "tool_offered_set_enforcement_mode", "warn")
    object.__setattr__(w.settings, "tool_context_policy_mode", "warn")
    bash = next((t for t in offered if t["name"] == tool), None)
    return (bash["description"] if bash is not None else None), refusals


def _unbound(w):
    w.runner.set_goal_commands(None)
    w.dispatcher.set_goal_commands(None)


def _rebound(w):
    main._bind_goal_commands(w.view, w.runner, w.dispatcher)


def _contexts(w):
    for kind in CONTEXT_KINDS:
        for name, authority, root in (
            ("lifted", "internal_only", w.root),
            ("contained", "internal_only", w.contained_root),
            ("other", "internal_only", uuid.uuid4()),
            ("none", "internal_only", None),
            ("owner", "owner", w.root),
        ):
            if kind == "continuation" and (authority != "internal_only" or root is None):
                continue
            yield f"{kind}|{name}", name, _ctx(kind, authority, root)


# ---- the kind matrix --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("shell", [True, False])
async def test_the_kind_matrix(world, shell):
    """Every kind, and the goal's root, a contained goal's, another root, none, and owner authority with the goal's
    root: bash without the allowlist is offered and authorized exactly in a task or a workflow step of the goal's
    session; every other turn is decided as with no hook at all."""
    w = world
    object.__setattr__(w.settings, "f099_lineage_shell", shell)
    lifted_seen = 0
    for key, name, ctx in _contexts(w):
        with_hook = _observe(w, ctx)
        _unbound(w)
        without = _observe(w, ctx)
        _rebound(w)
        if name == "lifted" and ctx.kind in tool_policy.COMMANDS_KINDS:
            lifted_seen += 1
            most, default = bash_waits(long_timeout=not ctx.is_short_turn)
            swapped = tool_policy.commands_bash_description(
                _BASH_SCHEMA["description"], build_commands=True, most=most, default=default
            )
            assert with_hook == (swapped, dict.fromkeys(MODES)), key
            description, refusals = without
            assert all(refusal is not None for refusal in refusals.values()), key
            assert description is None if not shell else tool_policy.COMMANDS_BASH_LEAD not in description, key
        else:
            assert with_hook == without, key
    assert lifted_seen == len(tool_policy.COMMANDS_KINDS)


async def test_the_offered_description_is_a_copy(world):
    w = world
    before = [dict(t) for t in w.runner._dispatcher.available_tools("task")]
    assert tool_policy.COMMANDS_BASH_LEAD in _bash_of(w.runner, _task(w))["description"]
    assert [dict(t) for t in w.runner._dispatcher.available_tools("task")] == before
    assert _bash_of(w.runner, _task(w, "continuation")) is None or (
        tool_policy.COMMANDS_BASH_LEAD not in _bash_of(w.runner, _task(w, "continuation"))["description"]
    )


async def test_every_other_tool_is_offered_and_refused_as_in_a_contained_task(world):
    """run_python, the tools that leave standing work behind, the spawn tools, the outward tools and the harness's
    own: in a task of the goal they are decided exactly as without the commands hook."""
    w = world
    for kind in tool_policy.COMMANDS_KINDS:
        ctx = _task(w, kind)
        for tool in TOOLS:
            if tool == "bash":
                continue
            with_hook = _observe(w, ctx, tool, {})
            _unbound(w)
            without = _observe(w, ctx, tool, {})
            _rebound(w)
            assert with_hook == without, (kind, tool)
        offered = {t["name"] for t in _offered(w.runner, ctx)}
        assert not offered & {"run_python", *STANDING, "spawn_task", "spawn_sync", "dag_create", "send_email"}


async def test_the_standing_tools_and_run_python_are_refused_under_the_lift(world):
    w = world
    ctx = _task(w)
    offered = frozenset(t["name"] for t in _offered(w.runner, ctx))
    for tool in ("run_python", *STANDING):
        for mode in MODES:
            object.__setattr__(w.settings, "tool_offered_set_enforcement_mode", mode)
            object.__setattr__(w.settings, "tool_context_policy_mode", mode)
            refusal = w.runner._authorize_tool_call(ctx, tool, offered, "s", {})
            assert refusal is not None and refusal.code == "internal_only", (tool, mode)


# ---- each row of the precedence table ---------------------------------------------------------------------------


async def test_a_cancelled_root_is_refused_first(world):
    w = world
    w.runner.set_cancelled_roots(lambda root: root == w.root)
    refusal = w.runner._authorize_tool_call(_task(w), "bash", frozenset({"bash"}), "s", REFUSED)
    assert refusal.code == "root_cancelled"


@pytest.mark.parametrize(
    ("guard", "held"),
    [
        (ToolGuard(locked=frozenset({"bash"})), tool_guard.LOCK),
        (ToolGuard(hold_all=True), tool_guard.NOT_READ),
        (ToolGuard(denied=frozenset({"bash"})), tool_guard.OWNER),
    ],
)
async def test_the_guard_still_holds_bash(world, guard, held):
    w = world
    tool_guard.install(guard)
    assert _bash_of(w.runner, _task(w)) is None
    refusal = w.runner._authorize_tool_call(_task(w), "bash", frozenset({"bash"}), "s", REFUSED)
    assert refusal.code == "tool_guard" and refusal.text == ToolGuard.refusal("bash", held)


async def test_with_no_guard_or_a_stale_view_nothing_is_lifted(world):
    w = world
    tool_guard.install(None)
    assert w.runner._authorize_tool_call(_task(w), "bash", frozenset({"bash"}), "s", REFUSED) is not None
    tool_guard.install(ToolGuard())
    assert w.runner._authorize_tool_call(_task(w), "bash", frozenset({"bash"}), "s", REFUSED) is None
    w.clock.now += 181  # stale, and before the end
    assert w.view.now() < w.view.commands_entry(w.root).expires_at
    assert w.runner._authorize_tool_call(_task(w), "bash", frozenset({"bash"}), "s", REFUSED) is not None


async def test_an_undoable_workflow_step_still_refuses_a_bash_call_that_cannot_be_undone(world):
    w = world
    ctx = _task(w, "dag_node", undoable=True)
    for mode in MODES:
        object.__setattr__(w.settings, "tool_context_policy_mode", mode)
        refusal = w.runner._authorize_tool_call(ctx, "bash", frozenset({"bash"}), "s", REFUSED)
        assert refusal is not None and "not_compensable" in refusal.text, mode


async def test_a_paused_goal_lifts_nothing_and_its_resume_lifts_again_until_the_same_end(world):
    w = world
    end = w.view.commands_entry(w.root).expires_at
    await w.store.pause(w.goal.id, actor="owner")
    await w.view.refresh_goal(w.goal.id)
    assert w.runner._authorize_tool_call(_task(w), "bash", frozenset({"bash"}), "s", REFUSED) is not None
    await w.store.resume(w.goal.id, actor="owner")
    await w.view.refresh_goal(w.goal.id)
    assert w.runner._authorize_tool_call(_task(w), "bash", frozenset({"bash"}), "s", REFUSED) is None
    assert abs(w.view.commands_entry(w.root).expires_at - end) < 5


@pytest.mark.parametrize("shell", [True, False])
async def test_an_end_between_two_calls_of_one_turn_refuses_the_second(world, shell):
    w = world
    object.__setattr__(w.settings, "f099_lineage_shell", shell)
    ctx = _task(w)
    offered = frozenset(t["name"] for t in _offered(w.runner, ctx))
    assert "bash" in offered
    allowlisted = {"command": "ls"}
    assert w.runner._authorize_tool_call(ctx, "bash", offered, "s", REFUSED) is None
    assert w.runner._authorize_tool_call(ctx, "bash", offered, "s", allowlisted) is None
    w.clock.now = w.view.commands_entry(w.root).expires_at  # the end passes; the turn's offered set is as it was
    refused = w.runner._authorize_tool_call(ctx, "bash", offered, "s", REFUSED)
    assert refused.code == "internal_only" and "(external)" in refused.text
    second = w.runner._authorize_tool_call(ctx, "bash", offered, "s", allowlisted)
    if shell:
        assert second is None  # an allowlisted command, as in any task
    else:
        assert second.code == "internal_only" and "(not_allowed)" in second.text


async def test_with_the_shell_off_the_harness_tools_of_a_task_are_decided_as_before(world):
    w = world
    object.__setattr__(w.settings, "f099_lineage_shell", False)
    for root in (w.root, w.contained_root):
        ctx = _ctx("subtask", "internal_only", root)
        for tool in INSIDE_ONLY:
            names = frozenset({tool})
            with_hook = w.runner._authorize_tool_call(ctx, tool, names, "s", {})
            _unbound(w)
            without = w.runner._authorize_tool_call(ctx, tool, names, "s", {})
            _rebound(w)
            assert with_hook == without, tool


async def test_with_the_shell_off_an_approved_bash_call_is_decided_as_before(world):
    w = world
    object.__setattr__(w.settings, "f099_lineage_shell", False)
    for authority in ("owner", "internal_only"):
        ctx = _ctx("approved_action", authority, w.root)
        refusal = w.runner._authorize_tool_call(ctx, "bash", frozenset({"bash"}), "s", {"command": "ls"})
        assert refusal is None, authority


async def test_in_enforce_an_external_command_runs_under_the_lift(world, monkeypatch):
    w = world
    object.__setattr__(w.settings, "tool_offered_set_enforcement_mode", "enforce")
    object.__setattr__(w.settings, "tool_context_policy_mode", "enforce")
    assert w.runner._authorize_tool_call(_task(w), "bash", frozenset({"bash"}), "s", REFUSED) is None
    monkeypatch.setattr(tool_policy, "classify_side_effect", lambda _name, _input: "irreversible")
    assert w.runner._authorize_tool_call(_task(w), "bash", frozenset({"bash"}), "s", REFUSED) is None
    _unbound(w)
    refusal = w.runner._authorize_tool_call(_task(w), "bash", frozenset({"bash"}), "s", REFUSED)
    assert refusal is not None


# ---- the hooks ----------------------------------------------------------------------------------------------------


async def test_the_runner_and_the_dispatcher_hold_the_same_hook(world):
    w = world
    assert w.runner._goal_commands is w.dispatcher._goal_commands
    assert (
        w.runner._goal_commands.__self__ is w.view and w.runner._goal_commands.__func__ is GoalToolGrants.commands_for
    )
    assert w.runner._goal_lift.__func__ is GoalToolGrants.lifts  # the per-goal tool grant hook is untouched


async def test_a_fork_of_a_fork_has_the_hook_and_a_later_one_reaches_it(world):
    w = world
    fork = w.runner.fork(MagicMock())
    grandchild = fork.fork(MagicMock())
    assert grandchild._goal_commands is w.runner._goal_commands
    assert tool_policy.COMMANDS_BASH_LEAD in _bash_of(grandchild, _task(w))["description"]
    w.runner.set_goal_commands(None)
    assert (
        _bash_of(grandchild, _task(w)) is None
        or tool_policy.COMMANDS_BASH_LEAD not in (_bash_of(grandchild, _task(w))["description"])
    )
    w.runner.set_goal_commands(w.view.commands_for)
    assert tool_policy.COMMANDS_BASH_LEAD in _bash_of(grandchild, _task(w))["description"]


async def test_the_tool_loop_hands_no_grant_lift_for_bash(world):
    """The commands hook never reaches the per-goal tool grant hook: the tool loop hands ``goal_lifted`` False for
    a lifted bash call, so no first-use row is written for it."""
    w = world
    lifted: dict[str, bool] = {}

    async def spy(_ctx, tool_name, _tool_input, *, goal_lifted=False, **_kw):
        from nous.api.runner import Dispatched

        lifted[tool_name] = goal_lifted
        return Dispatched("ok", False, None, None, None)

    steps = [
        [{"type": "tool_use", "id": "toolu_1", "name": "bash", "input": dict(REFUSED)}],
        [{"type": "text", "text": "done"}],
    ]

    async def model(*_args, **_kwargs):
        from nous.api.models import ApiResponse

        blocks = steps.pop(0)
        stop = "tool_use" if blocks[0]["type"] == "tool_use" else "end_turn"
        return ApiResponse(content=blocks, stop_reason=stop, usage={"input_tokens": 1, "output_tokens": 1})

    from nous.api.models import Conversation, Message

    w.runner._call_api = model
    w.runner._dispatch_with_ledger = spy
    conversation = Conversation(session_id="subtask-1")
    conversation.messages.append(Message(role="user", content="work"))
    ctx = _task(w)
    await w.runner._tool_loop("system", conversation, "task", session_id="subtask-1", is_subtask=True, context=ctx)
    assert lifted == {"bash": False}


# ---- the dispatcher and the bash tool ----------------------------------------------------------------------------


async def _dispatched(w, ctx, call: dict) -> dict:
    recorder = w.dispatcher._handlers["bash"]
    recorder.seen.clear()
    await w.dispatcher.dispatch("bash", dict(call), session_id="subtask-1", context=ctx)
    [seen] = recorder.seen
    return seen


async def test_a_lifted_call_carries_the_lineage_flags_and_never_the_build_one(world):
    w = world
    for command in ("ls", "git push origin fix", "curl https://example.com", "claude-jobs/runner.sh list"):
        seen = await _dispatched(w, _task(w), {"command": command})
        assert (seen["_lineage"], seen["_lineage_build"], seen["_goal_commands"]) == (True, False, True), command
        assert seen["_lineage_runner"] is tool_policy.lineage_runs_runner(command)
        assert seen["_long_timeout"] is True  # a task with no time limit of its own: 120 s with no timeout given
    short = await _dispatched(w, _task(w, turn_timeout=BACKGROUND_BASH_TIMEOUT), {"command": "ls"})
    assert short["_goal_commands"] is True and short["_long_timeout"] is False  # 30 s with no timeout given
    contained = await _dispatched(w, _ctx("subtask", "internal_only", w.contained_root), {"command": "git push"})
    assert contained["_lineage_build"] is True and "_goal_commands" not in contained


async def test_a_model_sent_flag_is_dropped(world):
    w = world
    sent = {"command": "git push", "_goal_commands": True, "_lineage_build": False, "_lineage": False}
    contained = await _dispatched(w, _ctx("subtask", "internal_only", w.contained_root), sent)
    assert contained["_lineage"] is True and contained["_lineage_build"] is True and "_goal_commands" not in contained
    owner = await _dispatched(w, _ctx("subtask", "owner", w.root), sent)
    assert "_goal_commands" not in owner and "_lineage" not in owner


async def _started(w, ctx, command: str, *, settings=None) -> dict:
    """The environment names and stdin the registered bash tool starts ``command`` with, the process environment
    being ``SYNTHETIC_ENV``."""
    real = ToolDispatcher()
    register_builtin_tools(real, settings or w.settings)
    real.set_goal_commands(w.dispatcher._goal_commands)
    shell = _Shell()
    with patch.dict(os.environ, SYNTHETIC_ENV, clear=True), patch("asyncio.create_subprocess_shell", shell):
        await real.dispatch("bash", {"command": command}, session_id="subtask-1", context=ctx)
    [started] = shell.seen
    return started


async def test_a_lifted_command_sees_the_lineage_variables_and_no_stdin(world):
    w = world
    allowed = sorted({*(_LINEAGE_BASH_ENV & set(SYNTHETIC_ENV)), "GH_REPO"})
    for command in ("ls", "curl https://example.com", "git push origin fix"):
        started = await _started(w, _task(w), command)
        assert started == {"env": allowed, "stdin": "devnull"}, command
        for name in ("DB_PASSWORD", "DATABASE_URL", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "NOUS_TELEGRAM_BOT_TOKEN"):
            assert name not in started["env"]
        assert not [name for name in started["env"] if name.startswith("GOOGLE_")]


async def test_git_push_keeps_the_code_host_token_only_under_the_lift(world):
    w = world
    lifted = await _started(w, _task(w), "git push origin fix")
    assert {"GITHUB_TOKEN", "GH_TOKEN", "GH_HOST"} <= set(lifted["env"])
    contained = await _started(w, _ctx("subtask", "internal_only", w.contained_root), "git push origin fix")
    assert not {"GITHUB_TOKEN", "GH_TOKEN", "GH_HOST"} & set(contained["env"])
    assert set(contained["env"]) >= _LINEAGE_BUILD_ENV & set(SYNTHETIC_ENV) and contained["stdin"] == "devnull"
    runner = await _started(w, _task(w), "claude-jobs/runner.sh launch fix")
    assert "GH_REPO" not in runner["env"] and "GITHUB_TOKEN" in runner["env"]


# ---- the waits bash's description states, and the ones the bash tool runs with -----------------------------------


def _stated_waits(description: str) -> tuple[int, int]:
    found = re.search(
        r"One call waits at most (\d+) seconds\. With no timeout given it waits (\d+) seconds\.", description
    )
    assert found is not None, description
    return int(found[1]), int(found[2])


async def _waited(w, ctx, call: dict) -> int:
    """The timeout the registered bash tool waits on its command with, for ``call`` dispatched in ``ctx``."""
    real = ToolDispatcher()
    register_builtin_tools(real, w.settings)
    real.set_goal_commands(w.dispatcher._goal_commands)
    waits: list[float] = []
    wait_for = asyncio.wait_for

    async def recorded(awaitable, timeout):
        if getattr(getattr(awaitable, "cr_code", None), "co_name", "") == "communicate":
            waits.append(timeout)
        return await wait_for(awaitable, timeout)

    with (
        patch.dict(os.environ, SYNTHETIC_ENV, clear=True),
        patch("asyncio.create_subprocess_shell", _Shell()),
        patch.object(builtin_tools.asyncio, "wait_for", recorded),
    ):
        await real.dispatch("bash", dict(call), session_id="subtask-1", context=ctx)
    [waited] = waits
    return int(waited)


@pytest.mark.parametrize(
    ("turn_timeout", "tool_timeout"),
    [(BACKGROUND_BASH_TIMEOUT, 120), (None, 120), (None, 1)],
    ids=["short_task", "long_task", "low_tool_timeout"],
)
@pytest.mark.parametrize("kind", sorted(tool_policy.COMMANDS_KINDS))
async def test_the_waits_the_description_states_are_the_ones_bash_runs_with(world, kind, turn_timeout, tool_timeout):
    w = world
    object.__setattr__(w.settings, "tool_timeout", tool_timeout)
    ctx = _task(w, kind, turn_timeout=turn_timeout)
    most, default = _stated_waits(_bash_of(w.runner, ctx)["description"])
    assert default == (BACKGROUND_BASH_TIMEOUT if turn_timeout is None else builtin_tools._DEFAULT_BASH_TIMEOUT)
    assert await _waited(w, ctx, {"command": "ls"}) == default
    assert await _waited(w, ctx, {"command": "ls", "timeout": most * 10}) == most
    assert await _waited(w, ctx, {"command": "ls", "timeout": most}) == most


async def test_a_low_tool_timeout_does_not_end_a_tasks_call(world):
    """``NOUS_TOOL_TIMEOUT`` bounds a tool call of a conversation's stream (``_dispatch_with_keepalive``); a task's
    call goes through the tool loop, which waits for the tool, so the waits bash's description states hold."""
    w = world
    object.__setattr__(w.settings, "tool_timeout", 1)
    recorder = w.dispatcher._handlers["bash"]

    async def slow(**kwargs):
        await asyncio.sleep(1.5)
        return await recorder(**kwargs)

    w.dispatcher._handlers["bash"] = slow
    steps = [
        [{"type": "tool_use", "id": "toolu_1", "name": "bash", "input": {"command": "ls"}}],
        [{"type": "text", "text": "done"}],
    ]

    async def model(*_args, **_kwargs):
        from nous.api.models import ApiResponse

        blocks = steps.pop(0)
        stop = "tool_use" if blocks[0]["type"] == "tool_use" else "end_turn"
        return ApiResponse(content=blocks, stop_reason=stop, usage={"input_tokens": 1, "output_tokens": 1})

    from nous.api.models import Conversation, Message

    w.runner._call_api = model
    conversation = Conversation(session_id="subtask-1")
    conversation.messages.append(Message(role="user", content="work"))
    try:
        _text, results, _usage, _thinking = await w.runner._tool_loop(
            "system", conversation, "task", session_id="subtask-1", is_subtask=True, context=_task(w)
        )
    finally:
        w.dispatcher._handlers["bash"] = recorder
    [result] = results
    assert (result.tool_name, result.result, result.error) == ("bash", "ok", None)
    assert len(recorder.seen) == 1


async def test_the_dispatcher_reads_the_hook_when_it_dispatches(world):
    """The dispatcher reads the commands hook when it dispatches a bash call: after the entry's end, a build command
    is started with the build environment."""
    w = world
    ctx = _task(w)
    assert w.runner._authorize_tool_call(ctx, "bash", frozenset({"bash"}), "s", {"command": "git push"}) is None
    w.clock.now = w.view.commands_entry(w.root).expires_at
    started = await _started(w, ctx, "git push origin fix")
    assert "GITHUB_TOKEN" not in started["env"]


async def test_heartbeat_check_create_passes_the_loader_no_metadata():
    """heartbeat_check_create calls the loader's create_check with no ``metadata`` argument."""
    loader = MagicMock()

    async def create_check(**kwargs):
        loader.seen = kwargs
        return {"name": kwargs["name"]}

    loader.create_check = create_check
    dispatcher = ToolDispatcher()
    register_heartbeat_tools(dispatcher, loader)
    task = ExecutionContext(
        kind="subtask", authority="internal_only", intention_id=uuid.uuid4(), root_intention_id=uuid.uuid4()
    )
    await dispatcher.dispatch(
        "heartbeat_check_create",
        {"name": "c", "description": "d", "prompt": "p", "interval_seconds": 3600},
        context=task,
    )
    assert "metadata" not in loader.seen


# ---- byte-for-byte while every goal is contained -----------------------------------------------------------------


async def test_with_the_hooks_bound_and_every_goal_contained_everything_acts_as_recorded(db):
    agent = f"commands-parity-{uuid.uuid4().hex[:8]}"
    settings = _settings(agent)
    store = GoalStore(db, agent, settings)
    goal = await _goal(store)
    await _session(db, store, goal)
    view = GoalToolGrants(db, settings, commands=True)
    assert await view.refresh()

    def prepare(runner: AgentRunner) -> None:
        tool_guard.install(ToolGuard())
        runner.set_goal_lift(view.lifts, view.note_used)
        runner.set_goal_commands(view.commands_for)

    def prepare_seams(runner: AgentRunner, dispatchers: list[ToolDispatcher]) -> None:
        prepare(runner)
        for dispatcher in dispatchers:
            dispatcher.set_goal_commands(view.commands_for)

    _commit, baseline = load()
    assert stated_difference(collect(prepare), baseline) == baseline
    assert await collect_seams(prepare_seams) == load_seams()


# ---- the flag ---------------------------------------------------------------------------------------------------

OWNER_DIGEST = "ab" * 32
PROXY_DIGEST = "cd" * 32
ALL_ON = {
    "goals_enabled": True,
    "continuation_enabled": True,
    "intentions_enabled": True,
    "result_inbox_enabled": True,
    "subtask_enabled": True,
    "autonomy_settings_enabled": True,
    "tool_guard_enabled": True,
    "goal_tool_grants_enabled": True,
    "owner_entry_mode": "enforce",
    "owner_route_secret_sha256": OWNER_DIGEST,
    "mcp_client_ui_secret_sha256": PROXY_DIGEST,
}


def test_the_flag_is_off_by_default_and_the_ceiling_is_a_day():
    settings = Settings(_env_file=None)
    assert settings.goal_commands_enabled is False and settings.goal_commands_max_hours == 24


def test_the_flag_stays_on_with_everything_it_needs():
    assert Settings(_env_file=None, goal_commands_enabled=True, **ALL_ON).goal_commands_enabled is True


@pytest.mark.parametrize(
    ("missing", "named"),
    [
        ({"goals_enabled": False}, ["NOUS_GOALS_ENABLED=true", "NOUS_GOAL_TOOL_GRANTS_ENABLED=true"]),
        (
            {"autonomy_settings_enabled": False},
            [
                "NOUS_AUTONOMY_SETTINGS_ENABLED=true",
                "NOUS_TOOL_GUARD_ENABLED=true",
                "NOUS_GOAL_TOOL_GRANTS_ENABLED=true",
            ],
        ),
        ({"tool_guard_enabled": False}, ["NOUS_TOOL_GUARD_ENABLED=true", "NOUS_GOAL_TOOL_GRANTS_ENABLED=true"]),
        ({"goal_tool_grants_enabled": False}, ["NOUS_GOAL_TOOL_GRANTS_ENABLED=true"]),
        ({"owner_entry_mode": "warn"}, ["NOUS_OWNER_ENTRY_MODE=enforce"]),
        ({"owner_entry_mode": "off"}, ["NOUS_OWNER_ENTRY_MODE=enforce"]),
        ({"owner_route_secret_sha256": ""}, ["NOUS_OWNER_ROUTE_SECRET_SHA256 (a SHA-256 digest)"]),
        ({"owner_route_secret_sha256": "not-a-digest"}, ["NOUS_OWNER_ROUTE_SECRET_SHA256 (a SHA-256 digest)"]),
        ({"mcp_client_ui_secret_sha256": ""}, ["NOUS_MCP_CLIENT_UI_SECRET_SHA256 (a SHA-256 digest)"]),
        ({"mcp_client_ui_secret_sha256": "ab" * 31}, ["NOUS_MCP_CLIENT_UI_SECRET_SHA256 (a SHA-256 digest)"]),
        (
            {"owner_entry_mode": "warn", "owner_route_secret_sha256": "", "mcp_client_ui_secret_sha256": ""},
            [
                "NOUS_OWNER_ENTRY_MODE=enforce",
                "NOUS_OWNER_ROUTE_SECRET_SHA256 (a SHA-256 digest)",
                "NOUS_MCP_CLIENT_UI_SECRET_SHA256 (a SHA-256 digest)",
            ],
        ),
    ],
)
def test_the_flag_is_forced_off_naming_everything_it_needs_in_one_warning(caplog, missing, named):
    with caplog.at_level(logging.WARNING):
        settings = Settings(_env_file=None, goal_commands_enabled=True, **{**ALL_ON, **missing})
    assert settings.goal_commands_enabled is False
    [line] = [r.getMessage() for r in caplog.records if "NOUS_GOAL_COMMANDS_ENABLED" in r.getMessage()]
    assert line == f"NOUS_GOAL_COMMANDS_ENABLED=true needs {' and '.join(named)}; running any command stays OFF."


@pytest.mark.parametrize("hours", [0, 2161])
def test_the_ceiling_is_bounded(hours):
    with pytest.raises(ValueError):
        Settings(_env_file=None, goal_commands_max_hours=hours)


# ---- the wiring ------------------------------------------------------------------------------------------------


class _Runner:
    def __init__(self) -> None:
        self.lift: list = []
        self.commands: list = []

    def set_goal_lift(self, lift, note_use=None) -> None:
        self.lift.append((lift, note_use))

    def set_goal_commands(self, commands) -> None:
        self.commands.append(commands)

    async def close(self) -> None:
        return None


class _Dispatcher:
    def __init__(self) -> None:
        self.commands: list = []

    def set_goal_commands(self, commands) -> None:
        self.commands.append(commands)


class _Continuation:
    def set_goal_turns(self, turns) -> None:
        self.turns = turns

    def set_goals(self, goals) -> None:
        self.goals = goals


def test_build_goals_binds_the_one_hook_to_both_only_with_the_flag(db):
    on = Settings(_env_file=None, goal_commands_enabled=True, **ALL_ON)
    runner, dispatcher = _Runner(), _Dispatcher()
    driver = main._build_goals(
        on, database=db, heart=MagicMock(), continuation_runner=_Continuation(), runner=runner, dispatcher=dispatcher
    )
    view = driver.tool_grants
    assert view.commands_on is True
    [hook] = runner.commands
    assert dispatcher.commands == [hook] and hook.__self__ is view and hook.__func__ is GoalToolGrants.commands_for
    assert runner.lift == [(view.lifts, view.note_used)]
    off = Settings(_env_file=None, **{**ALL_ON})
    runner, dispatcher = _Runner(), _Dispatcher()
    driver = main._build_goals(
        off, database=db, heart=MagicMock(), continuation_runner=_Continuation(), runner=runner, dispatcher=dispatcher
    )
    assert driver.tool_grants.commands_on is False and runner.commands == [] and dispatcher.commands == []


async def test_shutdown_unbinds_both_and_stops_the_views_loop(db):
    settings = _settings(f"commands-wiring-{uuid.uuid4().hex[:8]}")
    view = GoalToolGrants(db, settings, commands=True)
    view._tick_seconds = 3600
    view.start_loop()
    runner, dispatcher = _Runner(), _Dispatcher()
    await main.shutdown_components({"goal_tool_grants": view, "runner": runner, "dispatcher": dispatcher})
    assert runner.lift == [(None, None)] and runner.commands == [None] and dispatcher.commands == [None]
    assert view._loop_task is None


async def test_shutdown_with_the_flag_off_unbinds_only_the_grant_hook(db):
    view = GoalToolGrants(db, _settings("x"))
    runner, dispatcher = _Runner(), _Dispatcher()
    await main.shutdown_components({"goal_tool_grants": view, "runner": runner, "dispatcher": dispatcher})
    assert runner.lift == [(None, None)] and runner.commands == [] and dispatcher.commands == []


def test_the_process_stub_is_awaitable():
    assert asyncio.iscoroutinefunction(_Process.communicate)


# ---- beside the per-goal tool grants, and ActionGate ------------------------------------------------------------


async def test_a_grant_and_the_mode_lift_together_and_the_grant_outlives_the_return(world):
    w = world
    await w.store.grant_tool(w.goal.id, "send_email", None, actor="owner", proof="owner")
    assert await w.view.refresh()
    ctx = _task(w)
    offered = {t["name"]: t for t in _offered(w.runner, ctx)}
    assert "send_email" in offered and tool_policy.COMMANDS_BASH_LEAD in offered["bash"]["description"]
    names = frozenset(offered)
    assert w.runner._authorize_tool_call(ctx, "send_email", names, "s", {}) is None
    assert w.runner._authorize_tool_call(ctx, "bash", names, "s", REFUSED) is None
    await w.store.return_to_contained(w.goal.id, actor="owner", proof="owner")
    assert await w.view.refresh_goal(w.goal.id)
    assert w.runner._authorize_tool_call(ctx, "send_email", names, "s", {}) is None
    refused = w.runner._authorize_tool_call(ctx, "bash", names, "s", REFUSED)
    assert refused is not None and "(external)" in refused.text


async def test_action_gate_still_blocks_a_bash_call_of_a_task_that_may_run_any_command(runner_env):  # noqa: F811
    from f099_support import say, use

    from nous.cognitive.action_gate import GateResult

    env = await runner_env([use("bash", **REFUSED)], [say("I stop.")], telegram_chat_id="", **RUNNER_FLAGS)
    recorder = _Recorder()
    env.dispatcher.register("bash", recorder, _BASH_SCHEMA)
    store = GoalStore(env.db, env.agent, env.settings)
    goal = await _goal(store)
    root = await _session(env.db, store, goal)
    await _mode(env.db, goal.id)
    tool_guard.install(ToolGuard())
    view = GoalToolGrants(env.db, env.settings, commands=True)
    assert await view.refresh()
    main._bind_goal_commands(view, env.runner, env.dispatcher)
    checked: list[str] = []

    class Blocking:
        async def check(self, tool_name, tool_input, ledger, user_message=""):
            checked.append(tool_name)
            return GateResult(approved=False, reason="not now")

    env.runner._action_gate = Blocking()
    object.__setattr__(env.settings, "action_gating_mode", "enforce")
    ctx = ExecutionContext(
        kind="subtask", authority="internal_only", session_id="subtask-91", intention_id=root, root_intention_id=root
    )
    await env.runner.run_turn("subtask-91", "fetch the data", is_background=True, is_subtask=True, context=ctx)
    assert checked == ["bash"] and recorder.seen == []
    assert "BLOCKED by ActionGate" in str(env.model.calls[-1]["messages"])


async def test_a_goal_waiting_for_the_owner_keeps_its_mode_lifts_nothing_and_lifts_again_on_resume(world):
    """A done claim pauses the goal for the owner (the chain's own tool, which writes no mode): its tasks run no
    command while it waits, its mode stays, and resuming it lifts them again until the same end."""
    from nous.goals.tools import GoalSessionRef, GoalTools

    w = world
    end = w.view.commands_entry(w.root).expires_at
    async with w.db.session() as s:
        row = (await s.execute(select(GoalSession).where(GoalSession.root_id == w.root).with_for_update())).scalar_one()
        goal = (await s.execute(select(Goal).where(Goal.id == w.goal.id).with_for_update())).scalar_one()
        ref = GoalSessionRef(
            goal_id=goal.id, session_id=row.id, session_n=row.n, root_id=w.root, started_at=row.started_at
        )
        tools = GoalTools(w.db, w.settings, w.store, ref)
        paused, _told = await tools._wait_for_owner(s, goal, row, claim_seq=1)
        await s.commit()
    assert paused
    waiting = await w.store.get(w.goal.id)
    assert (waiting.state, waiting.pause_reason, waiting.mode) == ("paused", "waiting_for_owner", "commands")
    assert await w.view.refresh_goal(w.goal.id)
    assert w.view.commands_entry(w.root) is None
    for kind in tool_policy.COMMANDS_KINDS:
        assert w.runner._authorize_tool_call(_task(w, kind), "bash", frozenset({"bash"}), "s", REFUSED) is not None
    await w.store.resume(w.goal.id, actor="owner")
    assert await w.view.refresh_goal(w.goal.id)
    assert w.runner._authorize_tool_call(_task(w), "bash", frozenset({"bash"}), "s", REFUSED) is None
    assert abs(w.view.commands_entry(w.root).expires_at - end) < 5
