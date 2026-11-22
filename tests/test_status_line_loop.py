"""The status line in the tool loop (NOUS_STATUS_LINE_ENABLED): which calls carry it, what it says, and that nothing
but the outgoing request ever holds it.

A fake model answers from a fixed script, so a run with the setting on and one with it off make the same calls; each
request is deep-copied when it is sent.
"""

from __future__ import annotations

import copy
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from nous.api.anthropic_client import ApiResponse
from nous.api.execution_context import ExecutionContext
from nous.api.runner import AgentRunner, Conversation, Message
from nous.cognitive.execution_ledger import ExecutionLedger
from nous.cognitive.schemas import FrameSelection, TurnContext
from nous.config import Settings

MARK = "[Run status"
SESSION = "s-status-line"
TOOLS = [
    {"name": "write_file", "description": "Write a file.", "input_schema": {"type": "object", "properties": {}}},
    {"name": "read_file", "description": "Read a file.", "input_schema": {"type": "object", "properties": {}}},
]
BIG = "r" * 5000  # over the soft-trim size: pruning rewrites an older result


class FakeApi:
    """Answers each call from ``script`` and keeps a deep copy of every request."""

    records_calls = False

    def __init__(self, script: list[ApiResponse]) -> None:
        self.script = list(script)
        self.payloads: list[dict[str, Any]] = []

    async def call(self, payload: dict[str, Any], **_kw: Any) -> ApiResponse:
        self.payloads.append(copy.deepcopy(payload))
        return self.script.pop(0)

    call_streaming_aggregated = call

    async def close(self) -> None:
        return None


def _tool_round(i: int, name: str = "write_file") -> ApiResponse:
    args = {"path": f"notes/{i}.md", "content": "x"} if name == "write_file" else {"path": f"notes/{i}.md"}
    return ApiResponse(
        content=[{"type": "tool_use", "id": f"tu{i}", "name": name, "input": args}],
        stop_reason="tool_use",
        usage={"input_tokens": 10, "output_tokens": 5},
    )


def _reply(text: str = "done") -> ApiResponse:
    return ApiResponse(
        content=[{"type": "text", "text": text}], stop_reason="end_turn", usage={"input_tokens": 10, "output_tokens": 5}
    )


def _runner(*, on: bool, pruning: bool = False, max_turns: int = 10, **over: Any) -> AgentRunner:
    """Settings with a validation alias are set by that alias: Settings ignores their field names."""
    values: dict[str, Any] = {
        "agent_id": "test-status-line-loop",
        "model": "claude-haiku-4-5-20251001",
        "NOUS_BACKGROUND_MODEL": "claude-haiku-4-5-20251001",
        "thinking_mode": "off",
        "max_turns": max_turns,
        "NOUS_COMPACTION_ENABLED": False,
        "NOUS_TOOL_PRUNING_ENABLED": pruning,
        "action_gating_enabled": False,
        "claim_verification_enabled": False,
        "anti_hallucination_prompt": False,
        "api_background_streaming_enabled": False,
        "NOUS_CACHE_SPLIT_SYSTEM_PROMPT": False,
        "status_line_enabled": on,
        **over,
    }
    settings = Settings(_env_file=None, **values)
    assert (settings.tool_pruning_enabled, settings.compaction_enabled, settings.status_line_enabled) == (
        pruning,
        False,
        on,
    )
    runner = AgentRunner(cognitive=MagicMock(), brain=MagicMock(), heart=MagicMock(), settings=settings)
    runner._dispatcher = MagicMock()
    runner._dispatcher.available_tools = MagicMock(return_value=TOOLS)
    runner._dispatcher.dispatch = AsyncMock(return_value=(BIG, False))
    return runner


def _conversation() -> Conversation:
    conversation = Conversation(session_id=SESSION)
    conversation.messages.append(Message(role="user", content="Write the notes."))
    return conversation


async def _loop(
    runner: AgentRunner,
    script: list[ApiResponse],
    *,
    kind: str = "subtask",
    max_tool_calls: int | None = 20,
    ledger: ExecutionLedger | None = None,
    **kw: Any,
) -> FakeApi:
    api = FakeApi(script)
    runner._api = api
    await runner._tool_loop(
        system_prompt="sys",
        conversation=_conversation(),
        frame_id="task",
        session_id=SESSION,
        max_tool_calls=max_tool_calls,
        ledger=ledger if ledger is not None else ExecutionLedger(session_id=SESSION),
        context=ExecutionContext(kind=kind, session_id=SESSION),
        **kw,
    )
    return api


def _blocks(message: dict[str, Any]) -> list[Any]:
    content = message["content"]
    return [{"type": "text", "text": content}] if isinstance(content, str) else list(content)


def _is_note(block: Any) -> bool:
    return isinstance(block, dict) and block.get("type") == "text" and str(block.get("text", "")).startswith(MARK)


def _notes(payload: dict[str, Any]) -> list[str]:
    return [b["text"] for m in payload["messages"] for b in _blocks(m) if _is_note(b)]


def _without_notes(payload: dict[str, Any]) -> dict[str, Any]:
    """The payload with every status-line block taken out; the blocks beside them stay."""
    out = copy.deepcopy(payload)
    for message in out["messages"]:
        if isinstance(message["content"], list):
            message["content"] = [b for b in message["content"] if not _is_note(b)]
    return out


def _strip_marks(node: Any) -> Any:
    """``cache_control`` keys removed (the moving breakpoint is not part of the cached bytes), and a string content
    as the one text block the API reads it as."""
    if isinstance(node, dict):
        out = {k: _strip_marks(v) for k, v in node.items() if k != "cache_control"}
        if "role" in out and isinstance(out.get("content"), str):
            out["content"] = [{"type": "text", "text": out["content"]}]
        return out
    if isinstance(node, list):
        return [_strip_marks(v) for v in node]
    return node


def _dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True)


def _as_blocks(payload: dict[str, Any]) -> dict[str, Any]:
    """Every string content as the one text block the API reads it as (a message that carried a line is sent as
    blocks); everything else, markers included, as it was."""
    out = copy.deepcopy(payload)
    for message in out["messages"]:
        message["content"] = _blocks(message)
    return out


SCRIPT = [_tool_round(1), _tool_round(2, "read_file"), _tool_round(3), _reply()]


# ---------------------------------------------------------------------------------------------------------------------
# Off, and the kinds that never get it
# ---------------------------------------------------------------------------------------------------------------------


async def test_off_no_call_carries_a_line_and_no_call_gets_the_keyword():
    runner = _runner(on=False)
    seen: list[set[str]] = []
    real = runner._call_api

    async def spy(**kwargs: Any) -> ApiResponse:
        seen.append(set(kwargs))
        return await real(**kwargs)

    runner._call_api = spy  # type: ignore[method-assign]
    api = await _loop(runner, list(SCRIPT))
    assert len(api.payloads) == 4
    assert all(_notes(p) == [] for p in api.payloads)
    assert seen and all("tail_notes" not in keys for keys in seen)


@pytest.mark.parametrize("kind", ["interactive", "heartbeat_check", "background", "dag_summary"])
async def test_a_kind_outside_the_list_gets_no_line_and_the_same_requests(kind):
    on = await _loop(_runner(on=True), list(SCRIPT), kind=kind)
    off = await _loop(_runner(on=False), list(SCRIPT), kind=kind)
    assert [_dumps(p) for p in on.payloads] == [_dumps(p) for p in off.payloads]
    assert all(_notes(p) == [] for p in on.payloads)


# ---------------------------------------------------------------------------------------------------------------------
# On: what each call carries
# ---------------------------------------------------------------------------------------------------------------------


async def test_each_call_ends_with_this_rounds_line_and_keeps_every_earlier_line_at_its_place():
    api = await _loop(_runner(on=True), list(SCRIPT))
    assert len(api.payloads) == 4
    for k, payload in enumerate(api.payloads, start=1):
        notes = _notes(payload)
        assert len(notes) == k
        assert f"Round {k} of 10." in notes[-1]
        last = payload["messages"][-1]["content"]
        assert _is_note(last[-1]) and "cache_control" not in last[-1]
        assert last[-2]["cache_control"] == {"type": "ephemeral"}  # the newest line follows the breakpoint
        if k > 1:
            assert notes[:-1] == _notes(api.payloads[k - 2])


@pytest.mark.parametrize("on", [True, False])
async def test_every_request_holds_the_one_before_it_byte_for_byte(on):
    """Append-only (tool pruning off): call N+1 sends call N's messages again, lines included, then the new ones.
    Markers aside: the moving breakpoint is not part of the cached bytes and is not an edit of the history."""
    script = [_tool_round(i, "write_file" if i % 2 else "read_file") for i in range(1, 6)] + [_reply()]
    api = await _loop(_runner(on=on), script)
    assert len(api.payloads) == 6
    for before, after in zip(api.payloads, api.payloads[1:], strict=False):
        sent = _strip_marks(before["messages"])
        assert _strip_marks(after["messages"])[: len(sent)] == sent
        assert _strip_marks(after["system"]) == _strip_marks(before["system"]) and after["tools"] == before["tools"]


@pytest.mark.parametrize("pruning", [False, True])
async def test_with_the_lines_taken_out_the_requests_are_the_same_bytes_as_with_the_setting_off(pruning):
    script = [_tool_round(i) for i in range(1, 7)] + [_reply()]
    on = await _loop(_runner(on=True, pruning=pruning), list(script))
    off = await _loop(_runner(on=False, pruning=pruning), list(script))
    assert len(on.payloads) == len(off.payloads) == 7
    assert [_dumps(_as_blocks(_without_notes(p))) for p in on.payloads] == [_dumps(_as_blocks(p)) for p in off.payloads]
    if pruning:  # the pruned run really rewrote an older result, so this compared something
        unpruned = await _loop(_runner(on=False, pruning=False), list(script))
        assert [_dumps(p) for p in off.payloads] != [_dumps(p) for p in unpruned.payloads]


async def test_the_numbers_follow_the_loop():
    ledger = ExecutionLedger(session_id=SESSION)
    api = await _loop(_runner(on=True, max_turns=4), list(SCRIPT), ledger=ledger)
    notes = [_notes(p)[-1] for p in api.payloads]
    assert notes[0].startswith("[Run status. Round 1 of 4. Tool calls used: 0 of 20. After round 4, or once")
    assert notes[0].endswith("Changes so far in this task: none.]")
    assert "Tool calls used: 1 of 20." in notes[1]
    assert notes[1].endswith("Changes so far in this task: 1; files written: notes/1.md.]")
    assert "Tool calls used: 2 of 20." in notes[2]  # the read changed nothing
    assert notes[2].endswith("Changes so far in this task: 1; files written: notes/1.md.]")
    assert notes[3].startswith("[Run status. Round 4 of 4. Tool calls used: 3 of 20. This is the last round")
    assert notes[3].endswith("files written: notes/1.md, notes/3.md.]")


async def test_the_tool_calls_count_each_call_of_a_round():
    both = ApiResponse(
        content=[*_tool_round(1).content, *_tool_round(2, "read_file").content],
        stop_reason="tool_use",
        usage={"input_tokens": 10, "output_tokens": 5},
    )
    api = await _loop(_runner(on=True), [both, _reply()])
    assert "Round 2 of 10. Tool calls used: 2 of 20." in _notes(api.payloads[1])[-1]


FORCED_LINE = (
    "[Run status. This call must be the final report. "
    "Changes so far in this task: 2; files written: notes/1.md, notes/2.md.]"
)


@pytest.mark.parametrize(
    ("max_turns", "max_tool_calls"),
    [(4, 20), (10, 4)],
    ids=["by_rounds", "by_tool_calls"],
)
async def test_a_forced_report_call_says_it_must_be_the_final_report_and_names_no_budget(max_turns, max_tool_calls):
    """The report is forced from the last two rounds by count, or from two tool calls before the limit: that call's
    line says so, and the lines before it stay as they were sent."""
    from nous.api.subtask_tools import (
        SUBMIT_FINAL_REPORT_SCHEMA,
        SubtaskReportCollector,
        make_submit_final_report_executor,
    )

    collector = SubtaskReportCollector()
    extra = {"submit_final_report": (SUBMIT_FINAL_REPORT_SCHEMA, make_submit_final_report_executor(collector))}
    report = ApiResponse(
        content=[
            {
                "type": "tool_use",
                "id": "fr",
                "name": "submit_final_report",
                "input": {"summary": "x" * 60, "confidence": 0.9},
            }
        ],
        stop_reason="tool_use",
        usage={"input_tokens": 1, "output_tokens": 1},
    )
    api = await _loop(
        _runner(on=True, max_turns=max_turns),
        [_tool_round(1), _tool_round(2), report],
        max_tool_calls=max_tool_calls,
        is_subtask=True,
        extra_tools=extra,
        force_tool_on_penultimate="submit_final_report",
    )
    assert collector.is_set() and len(api.payloads) == 3
    first, second, forced = api.payloads
    assert "tool_choice" in forced and not any("tool_choice" in p for p in (first, second))
    assert _notes(forced)[-1] == FORCED_LINE
    assert _notes(forced)[:-1] == _notes(second)  # the earlier lines, unchanged
    assert "Round 2 of" in _notes(second)[-1]


async def test_a_line_that_cannot_be_written_is_left_out_of_that_round_only(monkeypatch, caplog):
    import nous.api.runner as runner_module

    real = runner_module.render_status_line
    calls = 0

    def flaky(**kwargs: Any) -> str:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ValueError("a path the line could not take")
        return real(**kwargs)

    monkeypatch.setattr(runner_module, "render_status_line", flaky)
    with caplog.at_level("WARNING", logger="nous.api.runner"):
        api = await _loop(_runner(on=True), list(SCRIPT))
    assert len(api.payloads) == 4  # the run went on
    assert [len(_notes(p)) for p in api.payloads] == [1, 1, 2, 3]
    assert _notes(api.payloads[1]) == _notes(api.payloads[0])  # round 2: no line, round 1's still sent
    assert "Round 3 of 10." in _notes(api.payloads[2])[-1]
    warnings = [r.getMessage() for r in caplog.records if "status line" in r.getMessage().lower()]
    assert warnings and all("ValueError" in w and "a path the line" not in w for w in warnings)


async def test_the_closing_call_after_the_tool_call_limit_adds_no_line_and_keeps_the_earlier_ones():
    api = await _loop(_runner(on=True), [_tool_round(1), _tool_round(2), _reply("closing")], max_tool_calls=2)
    *loop, closing = api.payloads
    assert "tools" not in closing and len(loop) == 2
    assert _notes(closing) == _notes(loop[-1]) and len(_notes(closing)) == 2  # the earlier lines; none for this call


async def test_the_closing_call_after_the_last_round_adds_no_line_and_keeps_the_earlier_ones():
    api = await _loop(_runner(on=True, max_turns=2), [_tool_round(1), _tool_round(2), _reply("closing")])
    *loop, closing = api.payloads
    assert "tools" not in closing and len(loop) == 2
    assert _notes(closing) == _notes(loop[-1]) and len(_notes(closing)) == 2


def _thinking(response: ApiResponse) -> ApiResponse:
    response.content = [{"type": "thinking", "thinking": "", "signature": "sig"}, *response.content]
    return response


async def test_the_lines_go_on_while_the_model_thinks_unasked():
    """A model that thinks with no thinking parameter: every thinking block was made on a prefix whose lines stay."""
    script = [_thinking(_tool_round(1)), _thinking(_tool_round(2)), _thinking(_tool_round(3)), _reply()]
    api = await _loop(_runner(on=True), script)
    assert [len(_notes(p)) for p in api.payloads] == [1, 2, 3, 4]
    for before, after in zip(api.payloads, api.payloads[1:], strict=False):
        sent = _strip_marks(before["messages"])
        assert _strip_marks(after["messages"])[: len(sent)] == sent


async def test_a_request_that_asks_for_thinking_gets_no_line():
    over = {"model": "claude-opus-4-8", "NOUS_BACKGROUND_MODEL": "claude-opus-4-8", "thinking_mode": "adaptive"}
    on = await _loop(_runner(on=True, **over), list(SCRIPT))
    off = await _loop(_runner(on=False, **over), list(SCRIPT))
    assert all("thinking" in p for p in on.payloads)
    assert [_dumps(p) for p in on.payloads] == [_dumps(p) for p in off.payloads]


def test_the_loop_asks_for_no_line_when_the_request_asks_for_thinking():
    """The loop's own test, before the builder's: a line it records as sent is one the request carries."""
    runner = _runner(on=True)
    ctx = ExecutionContext(kind="subtask", session_id=SESSION)
    assert runner._status_note(ctx, 0, 10, 0, 20, None, thinking_off=True) is not None
    assert runner._status_note(ctx, 0, 10, 0, 20, None, thinking_off=False) is None


async def test_the_list_pruning_reads_never_holds_a_line():
    runner = _runner(on=True, pruning=True)
    seen: list[str] = []
    real = runner._compactor.prune_tool_results

    def spy(messages: list[dict[str, Any]]) -> list[str]:
        seen.append(json.dumps(messages))
        return real(messages)

    runner._compactor.prune_tool_results = spy  # type: ignore[method-assign]
    api = await _loop(runner, list(SCRIPT))
    assert seen and all(MARK not in s for s in seen)
    assert all(_notes(p) for p in api.payloads)


# ---------------------------------------------------------------------------------------------------------------------
# Through run_turn: the saved conversation, the claim check, compaction, and the next run
# ---------------------------------------------------------------------------------------------------------------------


class _Cognitive:
    def __init__(self) -> None:
        self.context = TurnContext(
            system_prompt="You are Nous.",
            frame=FrameSelection(frame_id="task", frame_name="Task", confidence=0.9, match_method="default"),
            decision_id=None,
            active_censors=[],
            context_token_estimate=100,
        )

    async def pre_turn(self, *_a: Any, **_k: Any) -> TurnContext:
        return self.context

    async def post_turn(self, *_a: Any, **_k: Any) -> Any:
        from nous.cognitive.schemas import Assessment

        return Assessment(actual="")

    async def end_session(self, *_a: Any, **_k: Any) -> None:
        return None

    async def pre_compaction(self, *_a: Any, **_k: Any) -> None:
        return None

    async def list_frames(self, *_a: Any, **_k: Any) -> list:
        return []


class _Stub:
    async def close(self) -> None:
        return None


def _turn_runner(**over: Any) -> AgentRunner:
    """Compaction and tool pruning at their defaults (on), as a deployment runs them."""
    settings = Settings(
        _env_file=None,
        ANTHROPIC_API_KEY="test-key",
        agent_id="test-status-line-turn",
        model="claude-haiku-4-5-20251001",
        NOUS_BACKGROUND_MODEL="claude-haiku-4-5-20251001",
        thinking_mode="off",
        action_gating_enabled=False,
        api_background_streaming_enabled=False,
        status_line_enabled=True,
        **over,
    )
    runner = AgentRunner(_Cognitive(), _Stub(), _Stub(), settings)
    runner._dispatcher = MagicMock()
    runner._dispatcher.available_tools = MagicMock(return_value=TOOLS)
    runner._dispatcher.dispatch = AsyncMock(return_value=("written", False))
    runner._dispatcher.is_registered = MagicMock(return_value=False)
    return runner


async def _run_turn(runner: AgentRunner, message: str) -> str:
    text, _ctx, _usage = await runner.run_turn(
        SESSION,
        message,
        is_subtask=True,
        is_background=True,
        max_tool_calls=20,
        context=ExecutionContext(kind="subtask", session_id=SESSION),
    )
    return text


def _thinking_blocks(messages: list[dict[str, Any]]) -> list[Any]:
    return [b for m in messages for b in _blocks(m) if b.get("type") in ("thinking", "redacted_thinking")]


async def test_the_saved_conversation_and_the_claim_check_never_see_a_line_and_the_next_run_replays_no_thinking():
    runner = _turn_runner()
    checked: list[str] = []
    real_verify = runner._verify_claims

    def verify(session_id, response_text, tool_results, ledger):
        checked.append(json.dumps([response_text, [(r.tool_name, r.arguments, r.result) for r in tool_results]]))
        return real_verify(session_id, response_text, tool_results, ledger)

    runner._verify_claims = verify  # type: ignore[method-assign]
    api = FakeApi([_thinking(_tool_round(1)), _thinking(_reply("first")), _thinking(_reply("second"))])
    runner._api = api
    try:
        assert await _run_turn(runner, "Write the notes.") == "first"
        history = [{"role": m.role, "content": m.content} for m in runner._conversations[SESSION].messages]
        assert all(isinstance(m["content"], str) for m in history if m["role"] == "assistant")
        assert _thinking_blocks(history) == [] and MARK not in json.dumps(history)
        assert await _run_turn(runner, "And the index.") == "second"
        history = [{"role": m.role, "content": m.content} for m in runner._conversations[SESSION].messages]
        assert _thinking_blocks(history) == [] and MARK not in json.dumps(history)
    finally:
        await runner.close()
    assert len(api.payloads) == 3 and all(_notes(p) for p in api.payloads)
    # The next run starts from the conversation, text only: no thinking block made on a line is replayed in it.
    second_run = api.payloads[2]
    assert _thinking_blocks(second_run["messages"]) == []
    assert len(_notes(second_run)) == 1 and "Round 1 of" in _notes(second_run)[0]
    assert len(checked) == 2 and all(MARK not in c for c in checked)


async def test_a_compaction_call_carries_no_line():
    runner = _turn_runner()
    assert runner._settings.compaction_enabled is True
    runner._compactor.should_compact = MagicMock(return_value=True)  # type: ignore[method-assign]
    runner._compactor.find_cut_point = MagicMock(return_value=2)  # type: ignore[method-assign]
    conversation = Conversation(session_id=SESSION)
    conversation.messages += [Message(role="user", content="Old ask."), Message(role="assistant", content="Old reply.")]
    runner._conversations[SESSION] = conversation
    purposes: list[tuple[str | None, bool]] = []
    real = runner._call_api

    async def spy(*args: Any, **kwargs: Any) -> ApiResponse:
        purposes.append((getattr(kwargs.get("call_context"), "purpose", None), "tail_notes" in kwargs))
        return await real(*args, **kwargs)

    runner._call_api = spy  # type: ignore[method-assign]
    # However many calls the summary takes, each answer below ends it; then one tool round and the reply.
    summaries = [_reply("Summary: the owner asked for an old thing and got it.") for _ in range(3)]
    api = FakeApi([*summaries, _tool_round(1), _reply("done")])
    runner._api = api
    try:
        await _run_turn(runner, "Write the notes.")
    finally:
        await runner.close()
    sent = list(zip(purposes, api.payloads, strict=True))
    compaction = [p for (purpose, _), p in sent if purpose == "compaction"]
    loop = [(noted, p) for (purpose, noted), p in sent if purpose != "compaction"]
    assert compaction and all(_notes(p) == [] for p in compaction)
    assert "Old ask." in json.dumps(compaction[0]["messages"])
    assert loop and all(noted and _notes(p) for noted, p in loop)
