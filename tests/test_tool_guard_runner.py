"""F115: the tool guard in the runner: a tool switched off for background work is not offered to a background turn,
and a call to it anyway is refused with the ``tool_guard`` code in every enforcement mode, on the runner and on its
forks. Chat, an inbound MCP call and one call the owner approved are not held."""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest
from f099_support import (
    CONT,
    ScriptedModel,
    build_runner,
    env_factory,  # noqa: F401
    runner_env,  # noqa: F401
    say,
    use,
)

from nous.api.execution_context import ExecutionContext
from nous.autonomy import tool_guard
from nous.autonomy.tool_guard import ToolGuard
from nous.cognitive.ledger_store import REFUSAL_CODES
from nous.tool_names import CHANGED_TOOLS

ROOT = uuid.uuid4()
WARN = {"tool_offered_set_enforcement_mode": "warn", "tool_context_policy_mode": "warn"}
OFF = {"tool_offered_set_enforcement_mode": "off", "tool_context_policy_mode": "off"}
ENFORCE = {"tool_offered_set_enforcement_mode": "enforce", "tool_context_policy_mode": "enforce"}
SCHEMA = {"type": "object", "properties": {"query": {"type": "string"}}}
# (kind, authority, is_subtask, session id) of each kind of turn a background path starts, and of chat.
TURNS = [
    ("subtask", "owner", True, "subtask-1"),
    ("subtask", "internal_only", True, "subtask-2"),
    ("dag_node", "owner", True, "subtask-3"),
    ("scheduled", "owner", True, "schedule-1"),
    ("heartbeat_check", "owner", True, "dynamic-check-1"),
    ("heartbeat_triage", "owner", True, "heartbeat-1"),
    ("heartbeat_callback", "owner", True, "dynamic-callback-1"),
    ("continuation", "internal_only", False, "intent-1"),
    ("background", "owner", False, "subtask-5"),
    ("dag_summary", "owner", False, "dag-summary-1"),
]


def _ctx(kind: str, authority: str = "owner", session_id: str = "s", **extra) -> ExecutionContext:
    if kind == "continuation":
        extra = {"intention_id": ROOT, "root_intention_id": ROOT, **extra}
    if kind == "approved_action":
        extra = {"proposal_id": uuid.uuid4(), "declared_tools": ("web_search",), **extra}
    return ExecutionContext(kind=kind, session_id=session_id, authority=authority, **extra)


async def _runner(env_factory, **settings):  # noqa: F811
    env = await env_factory(**CONT, ANTHROPIC_API_KEY="test-key", **settings)
    runner, _cognitive, dispatcher = build_runner(env, ScriptedModel())
    for name in ("web_search", "web_fetch", "read_file"):
        if not dispatcher.is_registered(name):
            dispatcher.register(name, MagicMock(), SCHEMA)
    return env, runner


def _names(runner, ctx, is_subtask):
    tools = runner._offered_tools(ctx, "task", is_subtask=is_subtask, tool_filter=None, refuse_active=False)
    return [t["name"] for t in tools]


@pytest.mark.parametrize(("kind", "authority", "is_subtask", "session_id"), TURNS)
async def test_with_no_guard_or_an_empty_one_the_offered_tools_are_the_same(
    env_factory,  # noqa: F811
    kind,
    authority,
    is_subtask,
    session_id,
):
    _env, runner = await _runner(env_factory)
    ctx = _ctx(kind, authority, session_id)
    off = runner._offered_tools(ctx, "task", is_subtask=is_subtask, tool_filter=None, refuse_active=False)
    tool_guard.install(ToolGuard())
    empty = runner._offered_tools(ctx, "task", is_subtask=is_subtask, tool_filter=None, refuse_active=False)
    assert off == empty and off


@pytest.mark.parametrize(("kind", "authority", "is_subtask", "session_id"), TURNS)
async def test_a_denied_tool_is_not_offered_to_a_background_turn(
    env_factory,  # noqa: F811
    kind,
    authority,
    is_subtask,
    session_id,
):
    _env, runner = await _runner(env_factory)
    ctx = _ctx(kind, authority, session_id)
    before = _names(runner, ctx, is_subtask)
    assert "read_file" in before
    tool_guard.install(ToolGuard(denied=frozenset({"read_file"})))
    after = _names(runner, ctx, is_subtask)
    assert after == [name for name in before if name != "read_file"]


async def test_chat_and_an_inbound_mcp_call_are_still_offered_a_denied_tool(env_factory):  # noqa: F811
    _env, runner = await _runner(env_factory)
    tool_guard.install(ToolGuard(denied=frozenset({"web_search"}), locked=frozenset({"read_file"})))
    for kind in ("interactive", "mcp"):
        names = _names(runner, _ctx(kind, session_id="telegram-1"), False)
        assert "web_search" in names and "read_file" in names, kind
    # A conversation kind in a session a background path made is held.
    names = _names(runner, _ctx("interactive", session_id="claude-job-1"), False)
    assert "web_search" not in names and "read_file" not in names


@pytest.mark.parametrize("modes", [WARN, OFF, ENFORCE], ids=["warn", "off", "enforce"])
@pytest.mark.parametrize(("kind", "authority", "is_subtask", "session_id"), TURNS)
async def test_a_denied_tool_called_anyway_is_refused_in_every_mode(
    env_factory,  # noqa: F811
    modes,
    kind,
    authority,
    is_subtask,
    session_id,
):
    _env, runner = await _runner(env_factory, **modes)
    tool_guard.install(ToolGuard(denied=frozenset({"web_search"})))
    ctx = _ctx(kind, authority, session_id)
    offered = frozenset(_names(runner, ctx, is_subtask)) | {"web_search"}  # even when it was in the offered set
    refusal = runner._authorize_tool_call(ctx, "web_search", offered, session_id, {"query": "snow"})
    assert refusal is not None and refusal.code == "tool_guard"
    assert refusal.text == "Tool error: 'web_search' is switched off for background work."


async def test_a_host_locked_tool_is_refused_with_no_owner_setting(env_factory):  # noqa: F811
    _env, runner = await _runner(env_factory, **WARN)
    tool_guard.install(ToolGuard(locked=frozenset({"web_search"})))
    refusal = runner._authorize_tool_call(_ctx("scheduled"), "web_search", frozenset({"web_search"}), "s", {})
    assert refusal is not None and refusal.code == "tool_guard"


async def test_chat_mcp_an_approved_action_and_a_companion_tap_are_not_refused(env_factory):  # noqa: F811
    _env, runner = await _runner(env_factory, **WARN)
    for guard in (
        ToolGuard(denied=frozenset({"web_search"}), locked=frozenset({"web_search"})),
        ToolGuard(hold_all=True),
    ):
        tool_guard.install(guard)
        for ctx in (
            _ctx("interactive", session_id="telegram-1"),
            _ctx("mcp", session_id=None),
            _ctx("approved_action", session_id="proposal-1"),  # a background session prefix: the kind decides
            _ctx("agent_action", session_id="subtask-4"),  # the owner tapped a companion card
        ):
            refusal = runner._authorize_tool_call(ctx, "web_search", frozenset({"web_search"}), ctx.session_id, {})
            assert refusal is None or refusal.code != "tool_guard", ctx.kind
        assert "web_search" in _names(runner, _ctx("agent_action", session_id="subtask-4"), True)


async def test_a_refusal_writes_its_own_event_not_a_context_policy_violation(env_factory):  # noqa: F811
    _env, runner = await _runner(env_factory, **WARN)
    events = []
    runner._log_f026_decision = lambda event, data, session_id: events.append((event, data))
    tool_guard.install(ToolGuard(denied=frozenset({"web_search"}), locked=frozenset({"bash"})))
    for tool in ("web_search", "bash"):
        runner._authorize_tool_call(_ctx("scheduled", session_id="schedule-1"), tool, frozenset({tool}), "s", {})
    tool_guard.install(ToolGuard(hold_all=True))
    refusal = runner._authorize_tool_call(_ctx("scheduled"), "read_file", frozenset({"read_file"}), "s", {})
    assert refusal.code == "tool_guard" and "could not be read" in refusal.text
    assert events == [
        ("harness_tool_guard_refusal", {"tool_name": "web_search", "context_kind": "scheduled", "held_by": "owner"}),
        ("harness_tool_guard_refusal", {"tool_name": "bash", "context_kind": "scheduled", "held_by": "server"}),
        ("harness_tool_guard_refusal", {"tool_name": "read_file", "context_kind": "scheduled", "held_by": "not_read"}),
    ]


async def test_the_guard_comes_after_the_cancelled_root_and_before_the_lineage_floor(env_factory):  # noqa: F811
    _env, runner = await _runner(env_factory, **WARN)
    tool_guard.install(ToolGuard(denied=frozenset({"send_email", "web_search"})))
    runner.set_cancelled_roots(lambda root_id: root_id == ROOT)
    cancelled = _ctx("subtask", "internal_only", "subtask-1", root_intention_id=ROOT, intention_id=ROOT)
    refusal = runner._authorize_tool_call(cancelled, "web_search", frozenset(), "subtask-1", {})
    assert refusal.code == "root_cancelled"
    lineage = _ctx("subtask", "internal_only", "subtask-1")
    # send_email is not offered to a lineage, so the strict floor would refuse it too: the guard answers first.
    refusal = runner._authorize_tool_call(lineage, "send_email", frozenset(), "subtask-1", {})
    assert refusal.code == "tool_guard"


async def test_a_fork_reads_the_same_guard(env_factory):  # noqa: F811
    _env, runner = await _runner(env_factory, **WARN)
    fork = runner.fork(MagicMock())
    ctx = _ctx("heartbeat_check", session_id="dynamic-check-1")
    assert "web_search" in _names(fork, ctx, True)
    tool_guard.install(ToolGuard(denied=frozenset({"web_search"})))  # installed after the fork was made
    assert "web_search" not in _names(fork, ctx, True)
    refusal = fork._authorize_tool_call(ctx, "web_search", frozenset({"web_search"}), "dynamic-check-1", {})
    assert refusal is not None and refusal.code == "tool_guard"


def test_the_refusal_code_is_a_ledger_code():
    assert "tool_guard" in REFUSAL_CODES


# ---- through a real turn ------------------------------------------------------------------------------------------


class _Ledger:
    """A durable ledger that keeps the refusal codes it is given and rejects an unknown one, as the real one does."""

    def __init__(self) -> None:
        self.blocked: list[str] = []

    async def record_blocked(self, **kwargs):
        if kwargs["refused_by"] not in REFUSAL_CODES:
            raise ValueError(f"unknown refusal code {kwargs['refused_by']!r}")
        self.blocked.append(kwargs["refused_by"])

    def __getattr__(self, name):  # any other ledger call is not part of this test
        async def nothing(*args, **kwargs):
            return None

        return nothing


def _recording_tool(env, name: str) -> list[dict]:
    calls: list[dict] = []

    async def handler(**kwargs):
        calls.append(kwargs)
        return {"content": [{"type": "text", "text": "40 cm"}]}

    if env.dispatcher.is_registered(name):
        env.dispatcher.unregister(name)
    env.dispatcher.register(name, handler, SCHEMA)
    return calls


@pytest.mark.parametrize(
    ("kind", "session_id"),
    [("subtask", "subtask-9"), ("interactive", "claude-job-9")],
    ids=["subtask", "chat-kind-in-a-background-session"],
)
async def test_a_denied_tool_is_not_offered_and_not_run_in_a_real_turn(runner_env, kind, session_id):  # noqa: F811
    env = await runner_env([use("web_search", query="snow")], [say("I stop.")], **WARN)
    calls = _recording_tool(env, "web_search")
    ledger = _Ledger()
    env.runner.set_ledger_store(ledger)
    tool_guard.install(ToolGuard(denied=frozenset({"web_search"})))
    ctx = ExecutionContext(kind=kind, session_id=session_id)
    await env.runner.run_turn(
        session_id, "look", is_background=kind != "interactive", is_subtask=kind == "subtask", context=ctx
    )
    assert calls == [] and ledger.blocked == ["tool_guard"]
    offered = [tool["name"] for tool in env.model.calls[0]["tools"]]
    assert "web_search" not in offered
    assert "switched off for background work" in str(env.model.calls[-1]["messages"])


async def test_chat_still_runs_a_denied_tool_in_a_real_turn(runner_env):  # noqa: F811
    env = await runner_env([use("web_search", query="snow")], [say("40 cm.")], **WARN)
    calls = _recording_tool(env, "web_search")
    tool_guard.install(ToolGuard(denied=frozenset({"web_search"})))
    ctx = ExecutionContext(kind="interactive", session_id="telegram-7")
    await env.runner.run_turn("telegram-7", "look", context=ctx)
    assert calls == [{"query": "snow"}]
    assert "web_search" in [tool["name"] for tool in env.model.calls[0]["tools"]]


async def test_a_denied_tool_called_by_its_old_name_is_refused(runner_env):  # noqa: F811
    old = CHANGED_TOOLS["recall_recent"]
    env = await runner_env([use("recall_recent", hours=2)], [say("I stop.")], **WARN)
    calls = _recording_tool(env, old.target)
    env.dispatcher.register_alias("recall_recent", old.target, old.translate, old.note)
    ledger = _Ledger()
    env.runner.set_ledger_store(ledger)
    tool_guard.install(ToolGuard(denied=frozenset({old.target})))
    ctx = ExecutionContext(kind="scheduled", session_id="schedule-9")
    await env.runner.run_turn("schedule-9", "look", is_background=True, is_subtask=True, context=ctx)
    assert calls == [] and ledger.blocked == ["tool_guard"]


async def test_an_approved_action_runs_a_denied_tool(runner_env):  # noqa: F811
    env = await runner_env(**WARN)
    calls = _recording_tool(env, "web_search")
    tool_guard.install(ToolGuard(denied=frozenset({"web_search"}), locked=frozenset({"web_search"})))
    ctx = ExecutionContext(
        kind="approved_action",
        session_id="proposal-9",
        proposal_id=uuid.uuid4(),
        declared_tools=("web_search",),
    )
    result = await env.runner.execute_single_call(ctx, "web_search", {"query": "snow"})
    assert calls == [{"query": "snow"}] and not result.is_error
