"""F061 PR-2: tests for the runner's extra_tools + force_tool_on_penultimate + short-circuit + tool_calls hooks.

Tests _tool_loop directly with mocked dependencies — the focus is the new
tool-injection / dispatch-override / forced-tool-choice / short-circuit /
tool_calls-counter logic, not the full turn lifecycle.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from nous.api.anthropic_client import ApiResponse
from nous.api.runner import AgentRunner
from nous.api.subtask_tools import (
    SUBMIT_FINAL_REPORT_SCHEMA,
    SubtaskReportCollector,
    make_submit_final_report_executor,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _api_response(
    *,
    content: list[dict[str, Any]],
    stop_reason: str,
    input_tokens: int = 100,
    output_tokens: int = 50,
) -> ApiResponse:
    return ApiResponse(
        content=content,
        stop_reason=stop_reason,
        usage={"input_tokens": input_tokens, "output_tokens": output_tokens},
    )


def _tool_use(name: str, tool_input: dict, tool_use_id: str = "tu1") -> dict:
    return {"type": "tool_use", "name": name, "input": tool_input, "id": tool_use_id}


def _text(text: str) -> dict:
    return {"type": "text", "text": text}


def _make_runner(thinking_mode: str = "off"):
    """Build an AgentRunner with mocked cognitive/brain/heart and an
    AsyncMock API client. Forces haiku-only model so thinking is off and
    forced tool_choice is allowed.
    """
    from nous.config import Settings

    settings = Settings(
        _env_file=None,
        agent_id="test-runner-f061",
        model="claude-haiku-4-5-20251001",
        background_model="claude-haiku-4-5-20251001",
        thinking_mode=thinking_mode,
        max_turns=4,
        compaction_enabled=False,
        action_gating_enabled=False,
        claim_verification_enabled=False,
        execution_ledger_enabled=False,
        anti_hallucination_prompt=False,
        api_background_streaming_enabled=False,
        cache_split_system_prompt=False,
    )

    runner = AgentRunner(
        cognitive=MagicMock(),
        brain=MagicMock(),
        heart=MagicMock(),
        settings=settings,
    )
    runner._api = AsyncMock()
    runner._dispatcher = MagicMock()
    runner._dispatcher.available_tools = MagicMock(return_value=[])
    runner._dispatcher.dispatch = AsyncMock(return_value=("dispatched", False))
    return runner, settings


def _make_conversation():
    """Minimal Conversation with a single user message."""
    from nous.api.runner import Conversation, Message

    conv = Conversation(session_id="t-session")
    conv.messages.append(Message(role="user", content="hi"))
    return conv


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extra_tools_appended_to_per_call_tools_list():
    """extra_tools schemas appear in the API call's tools= list."""
    runner, _settings = _make_runner()
    collector = SubtaskReportCollector()
    extra = {
        "submit_final_report": (
            SUBMIT_FINAL_REPORT_SCHEMA,
            make_submit_final_report_executor(collector),
        ),
    }
    runner._api.call = AsyncMock(return_value=_api_response(
        content=[_text("done")], stop_reason="end_turn",
    ))

    await runner._tool_loop(
        system_prompt="sys",
        conversation=_make_conversation(),
        frame_id="task",
        is_subtask=True,
        max_tool_calls=10,
        extra_tools=extra,
    )

    # Inspect the payload sent on the first (only) API call
    payload = runner._api.call.await_args.args[0]
    tools_sent = payload.get("tools", [])
    names = [t["name"] for t in tools_sent]
    assert "submit_final_report" in names


@pytest.mark.asyncio
async def test_extra_tools_dispatch_routes_to_executor_not_global():
    """submit_final_report tool_use → executor stores payload (NOT 'Unknown tool')."""
    runner, _settings = _make_runner()
    collector = SubtaskReportCollector()
    extra = {
        "submit_final_report": (
            SUBMIT_FINAL_REPORT_SCHEMA,
            make_submit_final_report_executor(collector),
        ),
    }
    payload_in = {
        "summary": "x" * 60,
        "confidence": 0.9,
        "findings": ["f1"],
    }
    runner._api.call = AsyncMock(return_value=_api_response(
        content=[_tool_use("submit_final_report", payload_in)],
        stop_reason="tool_use",
    ))

    text, _tool_results, usage, _thinking = await runner._tool_loop(
        system_prompt="sys",
        conversation=_make_conversation(),
        frame_id="task",
        is_subtask=True,
        max_tool_calls=10,
        extra_tools=extra,
        force_tool_on_penultimate="submit_final_report",
    )

    # Collector got the FIRST payload (lock-on-first)
    assert collector.is_set() is True
    assert collector.get() == payload_in
    # Short-circuit happened: response_text is the marker, not real text
    assert text == "Report submitted."
    # Only ONE API call — no follow-up after the tool_results message
    assert runner._api.call.await_count == 1
    # Global dispatcher was NOT called for submit_final_report
    runner._dispatcher.dispatch.assert_not_called()
    # tool_calls counter populated (== 1 — one tool_use in the response)
    assert usage["tool_calls"] == 1
    # input/output tokens accumulated
    assert usage["input_tokens"] == 100
    assert usage["output_tokens"] == 50


@pytest.mark.asyncio
async def test_force_tool_haiku_sets_tool_choice_on_penultimate_turn():
    """When penultimate AND thinking off → tool_choice={'type':'tool',...}."""
    runner, _settings = _make_runner(thinking_mode="off")
    collector = SubtaskReportCollector()
    extra = {
        "submit_final_report": (
            SUBMIT_FINAL_REPORT_SCHEMA,
            make_submit_final_report_executor(collector),
        ),
    }
    # Drive the loop to a penultimate state via max_tool_calls.
    # max_tool_calls=2 + total_tool_calls becoming 1 after the first turn
    # → next call is penultimate (total_tool_calls >= max_tool_calls - 1).
    # Turn 0: random tool_use. Turn 1: should have tool_choice forced.
    runner._api.call = AsyncMock(side_effect=[
        _api_response(content=[_tool_use("noop", {})], stop_reason="tool_use"),
        _api_response(
            content=[_tool_use("submit_final_report", {
                "summary": "x" * 60, "confidence": 0.5,
            })],
            stop_reason="tool_use",
        ),
    ])

    await runner._tool_loop(
        system_prompt="sys",
        conversation=_make_conversation(),
        frame_id="task",
        is_subtask=True,
        max_tool_calls=2,
        extra_tools=extra,
        force_tool_on_penultimate="submit_final_report",
    )

    # Two calls. Inspect tool_choice on each.
    calls = runner._api.call.await_args_list
    assert len(calls) >= 2
    payload_turn0 = calls[0].args[0]
    payload_turn1 = calls[1].args[0]
    # Turn 0 not penultimate
    assert payload_turn0.get("tool_choice") is None
    # Turn 1 IS penultimate — forced
    assert payload_turn1.get("tool_choice") == {
        "type": "tool", "name": "submit_final_report",
    }


@pytest.mark.asyncio
async def test_force_tool_thinking_on_does_not_force_tool_choice():
    """Anthropic constraint: tool_choice in {auto,none} when thinking enabled.

    With thinking_mode!='off' AND non-haiku model, forced tool_choice is
    suppressed even on the penultimate turn.
    """
    runner, settings = _make_runner(thinking_mode="adaptive")
    collector = SubtaskReportCollector()
    extra = {
        "submit_final_report": (
            SUBMIT_FINAL_REPORT_SCHEMA,
            make_submit_final_report_executor(collector),
        ),
    }
    runner._api.call = AsyncMock(side_effect=[
        _api_response(content=[_tool_use("noop", {})], stop_reason="tool_use"),
        _api_response(content=[_text("done")], stop_reason="end_turn"),
    ])

    await runner._tool_loop(
        system_prompt="sys",
        conversation=_make_conversation(),
        frame_id="task",
        is_subtask=True,
        max_tool_calls=2,
        model_override="claude-sonnet-4-6",  # thinking-capable
        extra_tools=extra,
        force_tool_on_penultimate="submit_final_report",
    )

    # Inspect each call's payload — tool_choice MUST NOT be set.
    for call in runner._api.call.await_args_list:
        payload = call.args[0]
        assert payload.get("tool_choice") is None, (
            "thinking on → tool_choice must not be set"
        )


@pytest.mark.asyncio
async def test_no_extra_tools_default_no_changes_no_leak():
    """extra_tools=None / force_tool_on_penultimate=None → no behavior change."""
    runner, _settings = _make_runner()
    runner._api.call = AsyncMock(return_value=_api_response(
        content=[_text("ok")], stop_reason="end_turn",
    ))

    await runner._tool_loop(
        system_prompt="sys",
        conversation=_make_conversation(),
        frame_id="task",
        is_subtask=True,
    )

    payload = runner._api.call.await_args.args[0]
    tools_sent = payload.get("tools") or []
    assert all(t["name"] != "submit_final_report" for t in tools_sent)
    assert payload.get("tool_choice") is None


@pytest.mark.asyncio
async def test_usage_tool_calls_initialized_zero_when_no_tool_use():
    """usage['tool_calls'] is always present and 0 when no tools fire."""
    runner, _settings = _make_runner()
    runner._api.call = AsyncMock(return_value=_api_response(
        content=[_text("ok")], stop_reason="end_turn",
    ))

    _text_out, _tool_results, usage, _thinking = await runner._tool_loop(
        system_prompt="sys",
        conversation=_make_conversation(),
        frame_id="task",
        is_subtask=True,
    )
    assert "tool_calls" in usage
    assert usage["tool_calls"] == 0


@pytest.mark.asyncio
async def test_subsequent_tools_skipped_after_submit_final_report():
    """F061 PR-3 Codex review P2: when the model emits multiple tool_use
    blocks in a single response and ``submit_final_report`` succeeds, the
    subsequent tools in the SAME message MUST NOT execute. Otherwise
    side-effecting tools (bash, write_file) would run after termination
    has already been declared.
    """
    runner, _settings = _make_runner()
    collector = SubtaskReportCollector()
    extra = {
        "submit_final_report": (
            SUBMIT_FINAL_REPORT_SCHEMA,
            make_submit_final_report_executor(collector),
        ),
    }

    # Single API response with TWO tool_use blocks: submit_final_report FIRST,
    # then bash. bash MUST be skipped.
    payload_in = {"summary": "x" * 60, "confidence": 0.9}
    runner._api.call = AsyncMock(return_value=_api_response(
        content=[
            _tool_use("submit_final_report", payload_in, "tu1"),
            _tool_use("bash", {"command": "rm -rf /"}, "tu2"),  # MUST NOT run
        ],
        stop_reason="tool_use",
    ))
    # Spy on the dispatcher to confirm bash is never dispatched.
    runner._dispatcher.dispatch = AsyncMock(return_value=("dispatched", False))

    text, tool_results, usage, _thinking = await runner._tool_loop(
        system_prompt="sys",
        conversation=_make_conversation(),
        frame_id="task",
        is_subtask=True,
        max_tool_calls=10,
        extra_tools=extra,
        force_tool_on_penultimate="submit_final_report",
    )

    # submit_final_report ran (collector populated)
    assert collector.is_set()
    # bash was NEVER dispatched — short-circuit broke the loop after
    # submit_final_report succeeded
    runner._dispatcher.dispatch.assert_not_called()
    # tool_results only includes submit_final_report
    assert len(tool_results) == 1
    assert tool_results[0].tool_name == "submit_final_report"
    # Loop short-circuited
    assert text == "Report submitted."
