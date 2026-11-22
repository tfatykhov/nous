"""The model payload and the tool loop's call keywords, pinned as they are with the status line off (its default).

Written against the code before the status line existed: with ``NOUS_STATUS_LINE_ENABLED`` off, the payload of a
fixed input is the same bytes, and ``_tool_loop`` calls ``_call_api`` with the same keywords, on every kind.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from nous.api.anthropic_client import ApiResponse
from nous.api.execution_context import ExecutionContext
from nous.api.runner import AgentRunner, Conversation, Message
from nous.brain.intentions import AUTHORITY_INTERNAL
from nous.config import Settings

SYSTEM = {"static": "You are Nous.", "semi_stable": "Facts you know.", "dynamic": "Now: Sunday."}
TOOLS = [{"name": "read_file", "description": "Read a file.", "input_schema": {"type": "object", "properties": {}}}]
HISTORY = [
    {"role": "user", "content": "Summarise notes/a.md."},
    {
        "role": "assistant",
        "content": [{"type": "tool_use", "id": "tu1", "name": "read_file", "input": {"path": "notes/a.md"}}],
    },
    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu1", "content": "a", "is_error": False}]},
]
# Captured from the code before the status line (bf260047), with the settings of _runner() below.
EXPECTED_PAYLOAD = {
    "max_tokens": 4096,
    "messages": [
        {"content": "Summarise notes/a.md.", "role": "user"},
        {
            "content": [{"id": "tu1", "input": {"path": "notes/a.md"}, "name": "read_file", "type": "tool_use"}],
            "role": "assistant",
        },
        {
            "content": [
                {
                    "cache_control": {"type": "ephemeral"},
                    "content": "a",
                    "is_error": False,
                    "tool_use_id": "tu1",
                    "type": "tool_result",
                }
            ],
            "role": "user",
        },
    ],
    "model": "claude-haiku-4-5-20251001",
    "system": [
        {
            "cache_control": {"type": "ephemeral"},
            "text": "You are Claude Code, Anthropic's official CLI for Claude.",
            "type": "text",
        },
        {"cache_control": {"type": "ephemeral"}, "text": "You are Nous.", "type": "text"},
        {"cache_control": {"type": "ephemeral"}, "text": "Facts you know.", "type": "text"},
        {"text": "Now: Sunday.", "type": "text"},
    ],
    "tools": [
        {"description": "Read a file.", "input_schema": {"properties": {}, "type": "object"}, "name": "read_file"}
    ],
}
LOOP_KEYWORDS = {"system_prompt", "messages", "tools", "model_override", "is_background", "call_context"}


def _runner(**over: Any) -> AgentRunner:
    settings = Settings(
        _env_file=None,
        agent_id="test-status-line-parity",
        model="claude-haiku-4-5-20251001",
        NOUS_BACKGROUND_MODEL="claude-haiku-4-5-20251001",
        thinking_mode="off",
        max_turns=4,
        NOUS_COMPACTION_ENABLED=False,
        action_gating_enabled=False,
        claim_verification_enabled=False,
        execution_ledger_enabled=False,
        anti_hallucination_prompt=False,
        api_background_streaming_enabled=False,
        NOUS_CACHE_SPLIT_SYSTEM_PROMPT=False,
        **over,
    )
    runner = AgentRunner(cognitive=MagicMock(), brain=MagicMock(), heart=MagicMock(), settings=settings)
    runner._api = AsyncMock()
    runner._dispatcher = MagicMock()
    runner._dispatcher.available_tools = MagicMock(return_value=TOOLS)
    runner._dispatcher.dispatch = AsyncMock(return_value=("read", False))
    return runner


def _response(content: list[dict[str, Any]], stop_reason: str) -> ApiResponse:
    return ApiResponse(content=content, stop_reason=stop_reason, usage={"input_tokens": 10, "output_tokens": 5})


def test_the_payload_of_a_fixed_input_is_the_same_bytes() -> None:
    payload = _runner()._build_api_payload(SYSTEM, [dict(m) for m in HISTORY], TOOLS)
    assert json.dumps(payload, sort_keys=True) == json.dumps(EXPECTED_PAYLOAD, sort_keys=True)


def context_of(kind: str, session_id: str) -> ExecutionContext:
    """A context of ``kind`` as its caller builds one (a continuation names its intention and root)."""
    if kind == "continuation":
        return ExecutionContext(
            kind="continuation",
            session_id=session_id,
            authority=AUTHORITY_INTERNAL,
            intention_id=uuid4(),
            root_intention_id=uuid4(),
        )
    return ExecutionContext(kind=kind, session_id=session_id)


@pytest.mark.parametrize("kind", ["interactive", "subtask", "dag_node", "scheduled", "continuation", "background"])
async def test_the_tool_loop_calls_the_api_with_the_same_keywords_on_every_call(kind: str) -> None:
    runner = _runner()
    rounds = [
        _response([{"type": "tool_use", "id": f"tu{i}", "name": "read_file", "input": {"path": f"f{i}"}}], "tool_use")
        for i in range(3)
    ]
    runner._call_api = AsyncMock(side_effect=[*rounds, _response([{"type": "text", "text": "done"}], "end_turn")])
    conversation = Conversation(session_id="s-parity")
    conversation.messages.append(Message(role="user", content="go"))
    context = context_of(kind, "s-parity")

    text, _results, _usage, _thinking = await runner._tool_loop(
        system_prompt="sys", conversation=conversation, frame_id="task", session_id="s-parity", context=context
    )

    assert text == "done"
    assert runner._call_api.await_count == 4
    for call in runner._call_api.await_args_list:
        assert set(call.kwargs) == LOOP_KEYWORDS
        assert call.args == ()
