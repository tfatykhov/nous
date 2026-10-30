"""A heartbeat triage turn may not resolve decisions, whatever the policy modes say.

Prod runs the offered-set rule in `warn`, so stripping resolve_decisions from the triage offered set alone refuses
nothing: the runner also refuses it in every mode, next to the spawn floor, and records the refusal as a
``harness_context_policy_violation`` (violation ``heartbeat_resolve``, mode ``enforce``). A call by the old name
resolve_decision is rewritten to resolve_decisions before the floor, so the floor refuses it too. Other turns keep
the tool.
"""

from __future__ import annotations

import pytest
from test_runner_authorization import (
    _MockBrain,
    _MockCognitive,
    _MockHeart,
    _RecordingDispatcher,
    _run_loop,
    _settings,
    _tool_calls_then_done_with,
)
from test_runner_authorization import _runner as _plain_runner

from nous.api.execution_context import ExecutionContext
from nous.api.runner import AgentRunner
from nous.api.tool_policy import HEARTBEAT_RESOLVE_TEXT, heartbeat_resolve_violation
from nous.tool_names import CHANGED_TOOLS

# The tool, and the old name the runner rewrites to it.
CALLED_AS = ("resolve_decisions", "resolve_decision")
_TRIAGE = ExecutionContext(kind="heartbeat_triage", session_id="s1")
_ITEM = {"decision_id": "00000000-0000-0000-0000-000000000001", "outcome": "noise"}
_INPUTS = {"resolve_decision": dict(_ITEM), "resolve_decisions": {"resolutions": [dict(_ITEM)]}}


class _AliasingDispatcher(_RecordingDispatcher):
    """Knows the old tool names, as register_nous_tools wires them."""

    def alias_for(self, name):
        change = CHANGED_TOOLS.get(name)
        return change if change is not None and change.target in self.offered else None


def _runner(offered, **settings_overrides):
    r = AgentRunner(_MockCognitive(), _MockBrain(), _MockHeart(), _settings(**settings_overrides))
    d = _AliasingDispatcher(offered)
    r.set_dispatcher(d)
    return r, d


def test_the_resolve_tool_is_a_violation_and_other_decision_tools_are_not():
    assert heartbeat_resolve_violation("resolve_decisions")
    for name in ("record_decision", "list_decisions", "recall_deep", "learn_fact"):
        assert not heartbeat_resolve_violation(name)


@pytest.mark.asyncio
@pytest.mark.parametrize("offered_mode", ["off", "warn", "enforce"])
@pytest.mark.parametrize("policy_mode", ["off", "warn", "enforce"])
@pytest.mark.parametrize("name", CALLED_AS)
async def test_a_triage_turn_cannot_resolve_in_any_mode(name, policy_mode, offered_mode):
    r, d = _runner(
        ["resolve_decisions"], tool_context_policy_mode=policy_mode, tool_offered_set_enforcement_mode=offered_mode
    )
    events = []
    r._log_f026_decision = lambda kind, data, session_id: events.append((kind, data))
    r._call_api = _tool_calls_then_done_with(name, _INPUTS[name], times=1)
    _text, results, _usage, _thinking = await _run_loop(r, is_background=True, context=_TRIAGE)
    assert d.calls == []
    (res,) = results
    assert res.tool_name == "resolve_decisions" and res.error.endswith(HEARTBEAT_RESOLVE_TEXT)
    assert (
        "harness_context_policy_violation",
        {
            "tool_name": "resolve_decisions",
            "context_kind": "heartbeat_triage",
            "violation": "heartbeat_resolve",
            "mode": "enforce",
        },
    ) in events


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["interactive", "background", "subtask"])
async def test_other_turns_still_resolve_under_warn(kind):
    """Other turns keep the tool (background kinds use it for noise/superseded), by either name."""
    for name in CALLED_AS:
        r, d = _runner(["resolve_decisions"], tool_context_policy_mode="warn", tool_offered_set_enforcement_mode="warn")
        r._log_f026_decision = lambda kind, data, session_id: None
        r._call_api = _tool_calls_then_done_with(name, _INPUTS[name], times=1)
        await _run_loop(r, is_background=kind != "interactive", context=ExecutionContext(kind=kind, session_id="s1"))
        assert [c[0] for c in d.calls] == ["resolve_decisions"], (kind, name)


def test_a_triage_turn_is_not_offered_the_resolve_tool():
    r, _d = _plain_runner(["resolve_decisions", "record_decision", "list_decisions", "recall_deep"])
    offered = r._offered_tools(_TRIAGE, "conversation", is_subtask=True, tool_filter=None, refuse_active=False)
    assert [t["name"] for t in offered] == ["record_decision", "list_decisions", "recall_deep"]


@pytest.mark.parametrize("kind", ["interactive", "background", "subtask", "scheduled"])
def test_other_turns_are_offered_the_resolve_tool(kind):
    r, _d = _plain_runner(["resolve_decisions", "recall_deep"])
    ctx = ExecutionContext(kind=kind, session_id="s1")
    offered = r._offered_tools(ctx, "conversation", is_subtask=False, tool_filter=None, refuse_active=False)
    assert [t["name"] for t in offered] == ["resolve_decisions", "recall_deep"]


def test_the_refusal_says_not_to_retry():
    assert "resolve decisions" in HEARTBEAT_RESOLVE_TEXT and "Do not retry" in HEARTBEAT_RESOLVE_TEXT
