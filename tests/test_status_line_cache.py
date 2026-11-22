"""The request builder's status lines (``tail_notes``): each goes after its message's content, the newest after the
cache breakpoint, with no ``cache_control`` of its own; nothing the caller passed is changed; and a request that asks
for thinking gets none."""

from __future__ import annotations

import copy
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from nous.api.runner import AgentRunner
from nous.config import Settings

SYSTEM = {"static": "You are Nous.", "semi_stable": "Facts you know.", "dynamic": "Now: Sunday."}
TOOLS = [{"name": "read_file", "description": "Read a file.", "input_schema": {"type": "object", "properties": {}}}]
TASK = {"role": "user", "content": "Summarise notes/a.md."}
CALL = {
    "role": "assistant",
    "content": [{"type": "tool_use", "id": "tu1", "name": "read_file", "input": {"path": "notes/a.md"}}],
}
RESULT = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu1", "content": "a", "is_error": False}]}
N1 = "[Run status. Round 1 of 10.]"
N2 = "[Run status. Round 2 of 10.]"


def _runner(**over: Any) -> AgentRunner:
    settings = Settings(
        _env_file=None,
        agent_id="test-status-line-cache",
        model=over.pop("model", "claude-haiku-4-5-20251001"),
        thinking_mode=over.pop("thinking_mode", "off"),
        cache_split_system_prompt=False,
        **over,
    )
    runner = AgentRunner(cognitive=MagicMock(), brain=MagicMock(), heart=MagicMock(), settings=settings)
    runner._api = AsyncMock()
    return runner


def _payload(messages: list[dict[str, Any]], notes: list[tuple[int, str]] | None, **over: Any) -> dict[str, Any]:
    """A fresh runner each time: the system blocks' markers depend on the cache-break detector's state."""
    payload, _ctx = _runner(**over)._build_api_payload_and_context(SYSTEM, messages, TOOLS, tail_notes=notes)
    return payload


def _marks(node: Any) -> int:
    if isinstance(node, dict):
        return ("cache_control" in node) + sum(_marks(v) for v in node.values())
    if isinstance(node, list):
        return sum(_marks(v) for v in node)
    return 0


def _strip_marks(node: Any) -> Any:
    if isinstance(node, dict):
        out = {k: _strip_marks(v) for k, v in node.items() if k != "cache_control"}
        if "role" in out and isinstance(out.get("content"), str):
            out["content"] = [{"type": "text", "text": out["content"]}]
        return out
    if isinstance(node, list):
        return [_strip_marks(v) for v in node]
    return node


def _history() -> list[dict[str, Any]]:
    return copy.deepcopy([TASK, CALL, RESULT])


def test_the_newest_line_goes_after_the_marked_block_and_changes_nothing_before_it():
    plain = _payload(_history(), None)
    noted = _payload(_history(), [(2, N2)])
    for key in ("system", "tools", "model", "max_tokens"):
        assert noted[key] == plain[key]
    assert noted["messages"][:-1] == plain["messages"][:-1]
    last = noted["messages"][-1]
    assert {**last, "content": last["content"][:-1]} == plain["messages"][-1]
    assert last["content"][-1] == {"type": "text", "text": N2}
    assert last["content"][-2]["cache_control"] == {"type": "ephemeral"}  # the marked block is the same block
    assert _marks(noted["messages"]) == 1 and _marks(noted) <= 4


def test_an_earlier_line_goes_back_at_its_place_with_no_marker():
    noted = _payload(_history(), [(0, N1), (2, N2)])
    assert noted["messages"][0] == {
        "role": "user",
        "content": [{"type": "text", "text": TASK["content"]}, {"type": "text", "text": N1}],
    }
    assert noted["messages"][1] == CALL
    assert noted["messages"][2]["content"][-1] == {"type": "text", "text": N2}
    assert _marks(noted["messages"]) == 1


def test_the_requests_are_append_only_across_calls():
    """Call 1 sends the task with its line; call 2 sends call 1's messages again, byte for byte (markers aside),
    then the new ones."""
    first = _payload([copy.deepcopy(TASK)], [(0, N1)])
    second = _payload(_history(), [(0, N1), (2, N2)])
    assert first["messages"][0]["content"][-1] == {"type": "text", "text": N1}
    assert first["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert _strip_marks(second["messages"][: len(first["messages"])]) == _strip_marks(first["messages"])


@pytest.mark.parametrize(
    "content",
    ["Summarise notes/a.md.", [{"type": "tool_result", "tool_use_id": "tu1", "content": "a"}], []],
    ids=["string", "list", "empty"],
)
def test_what_the_caller_passed_is_left_as_it_was(content):
    messages = [CALL, {"role": "user", "content": content}]
    before = copy.deepcopy(messages)
    payload = _payload(messages, [(1, N2)])
    assert messages == before
    if content:
        assert payload["messages"][-1]["content"][-1] == {"type": "text", "text": N2}
    else:  # nothing was marked there: no line either
        assert payload["messages"][-1]["content"] == []


def test_a_request_that_asks_for_thinking_gets_no_line():
    over = {"model": "claude-opus-4-8", "thinking_mode": "adaptive"}
    noted = _payload(_history(), [(0, N1), (2, N2)], **over)
    assert "thinking" in noted
    assert json.dumps(noted, sort_keys=True) == json.dumps(_payload(_history(), None, **over), sort_keys=True)


def test_a_thinking_block_in_the_history_keeps_its_lines():
    """The lines are never removed once sent, so a thinking block produced after one keeps the prefix it was made on."""
    history = _history()
    history[1] = {
        "role": "assistant",
        "content": [{"type": "thinking", "thinking": "", "signature": "s"}, *CALL["content"]],
    }
    noted = _payload(history, [(0, N1), (2, N2)])
    assert noted["messages"][1] == history[1]
    assert [m["content"][-1] for m in (noted["messages"][0], noted["messages"][2])] == [
        {"type": "text", "text": N1},
        {"type": "text", "text": N2},
    ]


def test_a_line_whose_message_is_not_the_users_is_not_added():
    plain = _payload(_history(), None)
    assert _payload(_history(), [(1, N1), (7, N2)]) == plain
