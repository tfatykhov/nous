"""F099 section 4.4: only a tool flagged terminal ends the loop.

Before F099 any successful extra tool ended it (submit_final_report was the only
one). propose_action must not: the model resolves after it.
"""

from __future__ import annotations

import pytest
from test_runner_authorization import _run_loop, _runner, _tool_calls_then_done_with

from nous.api.runner import TERMINAL_EXTRA_TOOLS


def _extra(name: str, calls: list[dict], *, error: bool = False):
    async def executor(**kwargs):
        calls.append(kwargs)
        return ("it failed" if error else "Recorded."), error

    schema = {"name": name, "description": name, "input_schema": {"type": "object"}}
    return {name: (schema, executor)}


def _loop_runner():
    # Both modes off: the extra tools here are not in the tool-class table.
    return _runner(["recall_deep"], tool_offered_set_enforcement_mode="off", tool_context_policy_mode="off")


def test_the_terminal_set_is_exactly_the_two_resolving_tools():  # PIN
    assert TERMINAL_EXTRA_TOOLS == frozenset({"submit_final_report", "resolve_intention"})


async def test_a_successful_non_terminal_extra_tool_returns_to_the_model():
    calls: list[dict] = []
    r, _ = _loop_runner()
    r._call_api = _tool_calls_then_done_with("propose_action", {"tool": "send_email"}, times=1)
    text, results, _usage, _thinking = await _run_loop(
        r, is_background=True, extra_tools=_extra("propose_action", calls)
    )
    assert calls == [{"tool": "send_email"}]
    assert text == "done"  # the model was called again and answered; the loop did not short-circuit
    assert [x.tool_name for x in results] == ["propose_action"]


@pytest.mark.parametrize("name", sorted(TERMINAL_EXTRA_TOOLS))
async def test_a_successful_terminal_extra_tool_ends_the_loop(name):  # PIN
    calls: list[dict] = []
    r, _ = _loop_runner()
    r._call_api = _tool_calls_then_done_with(name, {"decision": "report"}, times=3)
    text, _results, usage, _thinking = await _run_loop(r, is_background=True, extra_tools=_extra(name, calls))
    assert text == "Report submitted."
    assert len(calls) == 1 and usage["tool_calls"] == 1


async def test_a_failing_terminal_extra_tool_does_not_end_the_loop():  # PIN
    calls: list[dict] = []
    r, _ = _loop_runner()
    r._call_api = _tool_calls_then_done_with("resolve_intention", {}, times=1)
    text, _results, _usage, _thinking = await _run_loop(
        r, is_background=True, extra_tools=_extra("resolve_intention", calls, error=True)
    )
    assert len(calls) == 1 and text == "done"
