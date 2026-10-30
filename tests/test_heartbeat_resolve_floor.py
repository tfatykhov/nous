"""A heartbeat triage turn may not resolve decisions, whatever the policy modes say.

Prod runs the offered-set rule in `warn`, so stripping resolve_decision / resolve_decisions from the triage offered
set alone refuses nothing: the runner also refuses both in every mode, next to the spawn floor, and records the
refusal as a ``harness_context_policy_violation`` (violation ``heartbeat_resolve``, mode ``enforce``). Other
turns keep both tools.
"""

from __future__ import annotations

import pytest
from test_runner_authorization import _run_loop, _runner, _tool_calls_then_done_with

from nous.api.execution_context import ExecutionContext
from nous.api.tool_policy import HEARTBEAT_RESOLVE_TEXT, heartbeat_resolve_violation

RESOLVE_TOOLS = ("resolve_decision", "resolve_decisions")
_TRIAGE = ExecutionContext(kind="heartbeat_triage", session_id="s1")
_INPUTS = {
    "resolve_decision": {"decision_id": "00000000-0000-0000-0000-000000000001", "outcome": "noise"},
    "resolve_decisions": {"resolutions": [{"decision_id": "00000000-0000-0000-0000-000000000001", "outcome": "noise"}]},
}


@pytest.mark.parametrize("name", RESOLVE_TOOLS)
def test_both_resolve_tools_are_violations(name):
    assert heartbeat_resolve_violation(name)


def test_the_other_decision_tools_are_not_violations():
    for name in ("record_decision", "list_decisions", "recall_deep", "learn_fact"):
        assert not heartbeat_resolve_violation(name)


@pytest.mark.asyncio
@pytest.mark.parametrize("offered_mode", ["off", "warn", "enforce"])
@pytest.mark.parametrize("policy_mode", ["off", "warn", "enforce"])
@pytest.mark.parametrize("name", RESOLVE_TOOLS)
async def test_a_triage_turn_cannot_resolve_in_any_mode(name, policy_mode, offered_mode):
    r, d = _runner([name], tool_context_policy_mode=policy_mode, tool_offered_set_enforcement_mode=offered_mode)
    events = []
    r._log_f026_decision = lambda kind, data, session_id: events.append((kind, data))
    r._call_api = _tool_calls_then_done_with(name, _INPUTS[name], times=1)
    _text, results, _usage, _thinking = await _run_loop(r, is_background=True, context=_TRIAGE)
    assert d.calls == []
    (res,) = results
    assert res.tool_name == name and res.error == HEARTBEAT_RESOLVE_TEXT
    assert (
        "harness_context_policy_violation",
        {"tool_name": name, "context_kind": "heartbeat_triage", "violation": "heartbeat_resolve", "mode": "enforce"},
    ) in events


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["interactive", "background", "subtask"])
async def test_other_turns_still_resolve_under_warn(kind):
    """Other turns keep both tools (background kinds use them for noise/superseded)."""
    for name in RESOLVE_TOOLS:
        r, d = _runner([name], tool_context_policy_mode="warn", tool_offered_set_enforcement_mode="warn")
        r._call_api = _tool_calls_then_done_with(name, _INPUTS[name], times=1)
        await _run_loop(r, is_background=kind != "interactive", context=ExecutionContext(kind=kind, session_id="s1"))
        assert [c[0] for c in d.calls] == [name], (kind, name)


def test_a_triage_turn_is_not_offered_the_resolve_tools():
    r, _d = _runner([*RESOLVE_TOOLS, "record_decision", "list_decisions", "recall_deep"])
    offered = r._offered_tools(_TRIAGE, "conversation", is_subtask=True, tool_filter=None, refuse_active=False)
    assert [t["name"] for t in offered] == ["record_decision", "list_decisions", "recall_deep"]


@pytest.mark.parametrize("kind", ["interactive", "background", "subtask", "scheduled"])
def test_other_turns_are_offered_the_resolve_tools(kind):
    r, _d = _runner([*RESOLVE_TOOLS, "recall_deep"])
    ctx = ExecutionContext(kind=kind, session_id="s1")
    offered = r._offered_tools(ctx, "conversation", is_subtask=False, tool_filter=None, refuse_active=False)
    assert [t["name"] for t in offered] == [*RESOLVE_TOOLS, "recall_deep"]


def test_the_refusal_says_not_to_retry():
    assert "resolve decisions" in HEARTBEAT_RESOLVE_TEXT and "Do not retry" in HEARTBEAT_RESOLVE_TEXT
