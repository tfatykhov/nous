"""Codex P1 on PR #773: a heartbeat triage turn may not spawn or delegate work, whatever the policy modes say.

The triage prompt calls the turn local-only, but heartbeat_triage's policy row (spawn=False) is only logged in
prod's `warn` mode, so a triage turn could still call dag_create, and the DAG's nodes run with work-level
permissions (email, web). The runner refuses every spawn tool and every dag_manage / heartbeat_check_manage action
that starts or re-arms work in a triage turn, and does not offer the spawn tools to it.
"""

from __future__ import annotations

import pytest
from test_runner_authorization import _run_loop, _runner, _tool_calls_then_done_with

from nous.api.execution_context import ExecutionContext
from nous.api.tool_policy import HEARTBEAT_SPAWN_TEXT, heartbeat_spawn_violation

SPAWN_TOOLS = ("dag_create", "spawn_task", "spawn_sync", "schedule_task", "heartbeat_check_create")
_TRIAGE = ExecutionContext(kind="heartbeat_triage", session_id="s1")


@pytest.mark.parametrize("name", SPAWN_TOOLS)
def test_every_spawn_tool_is_a_violation(name):
    assert heartbeat_spawn_violation(name, {})


@pytest.mark.parametrize(
    ("name", "action"),
    [
        ("dag_manage", "retry_node"),
        ("dag_manage", "RETRY_NODE"),
        ("dag_manage", "resume"),  # an action the allowlist does not know is refused
        ("dag_manage", None),
        ("dag_manage", 3),
        ("heartbeat_check_manage", "enable"),
        ("heartbeat_check_manage", "update"),  # rewrites what a check runs
        ("heartbeat_check_manage", None),
        # on a DAG node's check, the DAG loop reads a disable or delete as the node's completion
        ("heartbeat_check_manage", "disable"),
        ("heartbeat_check_manage", "delete"),
        ("dag_manage", " status "),  # the handler compares exactly
    ],
)
def test_actions_that_start_or_rearm_work_are_violations(name, action):
    assert heartbeat_spawn_violation(name, {} if action is None else {"action": action})


@pytest.mark.parametrize(
    ("name", "action"),
    [
        ("dag_manage", "list"),
        ("dag_manage", "recent"),
        ("dag_manage", "status"),
        ("dag_manage", "node_result"),
        ("dag_manage", "cancel"),
        ("heartbeat_check_manage", "list"),
    ],
)
def test_reads_and_stops_are_not_violations(name, action):
    assert not heartbeat_spawn_violation(name, {"action": action})


def test_local_tools_are_not_violations():
    for name in ("recall_deep", "read_file", "learn_fact", "record_decision", "cancel_task"):
        assert not heartbeat_spawn_violation(name, {})


@pytest.mark.asyncio
@pytest.mark.parametrize("policy_mode", ["off", "warn", "enforce"])
@pytest.mark.parametrize("name", SPAWN_TOOLS)
async def test_a_triage_turn_cannot_spawn_in_any_mode(policy_mode, name):
    r, d = _runner([name], tool_context_policy_mode=policy_mode, tool_offered_set_enforcement_mode="off")
    r._call_api = _tool_calls_then_done_with(name, {}, times=1)
    await _run_loop(r, is_background=True, context=_TRIAGE)
    assert d.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("policy_mode", ["off", "warn", "enforce"])
async def test_a_triage_turn_cannot_retry_a_dag_node_in_any_mode(policy_mode):
    r, d = _runner(["dag_manage"], tool_context_policy_mode=policy_mode, tool_offered_set_enforcement_mode="off")
    r._call_api = _tool_calls_then_done_with("dag_manage", {"action": "retry_node", "node_id": "n1"}, times=1)
    await _run_loop(r, is_background=True, context=_TRIAGE)
    assert d.calls == []


@pytest.mark.asyncio
async def test_a_triage_turn_can_still_read_a_dag_under_warn():
    r, d = _runner(["dag_manage"], tool_context_policy_mode="warn", tool_offered_set_enforcement_mode="off")
    r._call_api = _tool_calls_then_done_with("dag_manage", {"action": "status", "dag_id": "x"}, times=1)
    await _run_loop(r, is_background=True, context=_TRIAGE)
    assert [name for name, _, _ in d.calls] == ["dag_manage"]


def test_a_triage_turn_is_not_offered_the_spawn_tools():
    r, _d = _runner([*SPAWN_TOOLS, "dag_manage", "recall_deep"])
    offered = r._offered_tools(_TRIAGE, "conversation", is_subtask=True, tool_filter=None, refuse_active=False)
    assert [t["name"] for t in offered] == ["dag_manage", "recall_deep"]


@pytest.mark.asyncio
async def test_the_floor_is_only_for_heartbeat_triage():
    """An owner's chat turn creating a DAG is unchanged."""
    r, d = _runner(["dag_create"], tool_context_policy_mode="warn", tool_offered_set_enforcement_mode="off")
    r._call_api = _tool_calls_then_done_with("dag_create", {}, times=1)
    await _run_loop(r, is_background=False, context=ExecutionContext(kind="interactive", session_id="s1"))
    assert [name for name, _, _ in d.calls] == ["dag_create"]


def test_the_refusal_tells_the_model_to_escalate():
    assert "owner" in HEARTBEAT_SPAWN_TEXT and "Do not retry" in HEARTBEAT_SPAWN_TEXT
