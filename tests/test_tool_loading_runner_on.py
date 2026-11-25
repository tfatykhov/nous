"""F120 tool loading, On, in the runner: both tool loops, over the client double of ``test_runner_call_context``.

The three paths of ``test_tool_loading_runner`` (``run_turn`` and a background subtask over ``_tool_loop``, and
``stream_chat``) are driven with a client that can defer tools. In On a request differs from Watch only by the
deferral flags on exactly the tools outside the kept set and the loader last, the same array every request of the turn
whatever changes meanwhile; a load answers with references and the next request carries them; a call of a deferred tool
not loaded yet never runs and is no tool failure anywhere; the first loads of a turn are not tool calls; a request with
no tools carries words instead of references; the API refusing a request (400) or the window's own code failing sends
every tool in full for the rest of the turn; every condition On needs, and every layer that keeps a tool from a turn
today, still holds. The tests that read the pattern counts and the outside judge's counts write ``tool_call_log`` rows
to real Postgres under their own agent ids and delete them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx
import pytest
from test_runner_authorization import _RecordingDispatcher
from test_runner_background import _MockBrain, _MockCognitive, _MockHeart
from test_runner_call_context import _USAGE, _preset, _pump, _SpyClient, _text, _text_segment

from nous.api import runner as runner_module
from nous.api import tool_loading
from nous.api.anthropic_client import StreamEvent
from nous.api.compaction import ConversationCompactor
from nous.api.execution_context import ExecutionContext
from nous.api.models import ApiResponse
from nous.api.runner import AgentRunner
from nous.api.tool_loading import KINDS, LOADER_ALL_ATTACHED, LOADER_NAME, ToolLoadingConfig
from nous.autonomy import tool_guard
from nous.autonomy.tool_guard import ToolGuard
from nous.brain.intentions import AUTHORITY_INTERNAL
from nous.cognitive.monitor import MonitorEngine
from nous.config import Settings
from nous.heart.procedures import outcome_attribution
from nous.observability.model_calls import CallContext
from nous.runtime_config import RuntimeConfig

OFFERED = ("bash", "recall_deep", "db_query", "send_email", "dag_create")
DEFERRED = {"send_email", "dag_create"}
AGENT = "test-agent"
MODEL = "claude-opus-5-5"
ON_ALL = ToolLoadingConfig(mode="on", kinds=frozenset(KINDS))
WATCH_ALL = ToolLoadingConfig(mode="watch", kinds=frozenset(KINDS))
PATHS = ["run_turn", "stream_chat", "subtask"]
TEXT = "text"  # a step that answers with text (ends a turn)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    RuntimeConfig.reset()
    monkeypatch.setattr(runner_module, "_tool_loading_fault_logged", False)
    monkeypatch.setattr(runner_module, "_tool_window_logged", set())
    tool_guard.install(None)
    yield
    tool_guard.install(None)
    RuntimeConfig.reset()


def _settings(**over: Any) -> Settings:
    values = {"model": MODEL, "tool_loading_enabled": True, "action_gating_enabled": False, **over}
    return Settings(_env_file=None, ANTHROPIC_API_KEY="test-key", agent_id=AGENT, **values)


MALFORMED: dict[str, Any] = {}  # a tool call whose input the client could not parse (compared by identity)


def _use(step: int, calls: list[tuple[str, dict]]) -> ApiResponse:
    blocks = [
        {"type": "tool_use", "id": f"t{step}_{i}", "name": n, "input": {} if inp is MALFORMED else inp}
        for i, (n, inp) in enumerate(calls)
    ]
    errors = {f"t{step}_{i}": "Tool input JSON was malformed." for i, (_n, inp) in enumerate(calls) if inp is MALFORMED}
    return ApiResponse(content=blocks, stop_reason="tool_use", usage=dict(_USAGE), input_errors=errors or None)


def _segment(step: int, calls: list[tuple[str, dict]]) -> list[StreamEvent]:
    events = [StreamEvent(type="message_start", usage={k: v for k, v in _USAGE.items() if k != "output_tokens"})]
    for i, (name, tool_input) in enumerate(calls):
        events += [
            StreamEvent(type="tool_start", tool_name=name, tool_id=f"t{step}_{i}", block_index=i),
            StreamEvent(
                type="tool_input_delta",
                text='{"names": [' if tool_input is MALFORMED else json.dumps(tool_input),
                block_index=i,
            ),
            StreamEvent(type="block_stop", block_index=i),
        ]
    return [*events, StreamEvent(type="done", stop_reason="tool_use", usage={"output_tokens": 7})]


def _refused(status: int = 400) -> RuntimeError:
    """What the SDK client raises when the API refuses a request (the SDK's error as its cause)."""
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    cause = anthropic.APIStatusError(
        f"Error code: {status}", response=httpx.Response(status, request=request), body=None
    )
    error = RuntimeError(f"Anthropic SDK error: {cause}")
    error.__cause__ = cause
    return error


REFUSED = "refused"
STREAM_REFUSED = [StreamEvent(type="error", text="SDK stream error: Error code: 400", status=400)]


class _CallLog:
    """The tool-call recorder: every call record and turn record it is handed."""

    def __init__(self) -> None:
        self.calls: list[Any] = []
        self.turns: list[Any] = []

    def record(self, record: Any) -> None:
        self.calls.append(record)

    def record_turn(self, record: Any) -> None:
        self.turns.append(record)


class _Cognitive(_MockCognitive):
    """Keeps each turn's result as post_turn is handed it."""

    def __init__(self) -> None:
        super().__init__(_preset())
        self.results: list[Any] = []

    async def post_turn(self, agent_id, session_id, turn_result, turn_context, session=None, is_background=False):
        self.results.append(turn_result)
        return await super().post_turn(agent_id, session_id, turn_result, turn_context, session, is_background)


def _client(steps: list[Any], stream: bool, sent: list[dict], hook: Any = None) -> _SpyClient:
    """A client that can defer tools, answering ``steps`` in order (each a list of tool calls, ``TEXT``, or a refusal),
    then text; every payload is kept as it was sent. A stream's step answers from its segments; a plain call in a
    streaming turn (its closing call) answers with text."""
    replies: list[Any] = []
    segments: list[list[StreamEvent]] = []
    for step, item in enumerate([*steps, TEXT]):
        if item == TEXT:
            replies.append(_text())
            segments.append(_text_segment())
        elif item == REFUSED:
            replies.append(_refused())
            segments.append(STREAM_REFUSED)
        elif isinstance(item, list) and item and isinstance(item[0], StreamEvent):  # a stream's events as given
            replies.append(_text())
            segments.append(item)
        elif isinstance(item, BaseException):
            replies.append(item)
            segments.append(
                [StreamEvent(type="error", text=str(item), status=getattr(item.__cause__, "status_code", None))]
            )
        else:
            replies.append(_use(step, item))
            segments.append(_segment(step, item))
    queue = iter(replies)

    def keep(payload: dict[str, Any]) -> None:
        sent.append(json.loads(json.dumps(payload)))
        if hook is not None:
            hook(len(sent))

    def reply(payload: dict[str, Any]) -> Any:
        keep(payload)
        return _text("closing") if stream else next(queue)

    client = _SpyClient(reply, segments=segments if stream else None)
    client.supports_deferred_tools = True
    client.watches = []

    def watch(params: dict[str, Any], attempts: Any, text_sent: Any = None, watch: dict | None = None) -> None:
        client.rows.append(params)
        client.watches.append(None if watch is None else json.loads(watch["record"]))

    client.recorder._submit_call = watch
    if stream:
        streamed = client.stream

        async def stream_and_keep(payload: dict[str, Any], *, call_context: Any = None):
            keep(payload)
            async for event in streamed(payload, call_context=call_context):
                yield event

        client.stream = stream_and_keep
    return client


async def _turn(
    path: str,
    steps: list[Any],
    settings: Settings | None = None,
    *,
    context: ExecutionContext | None = None,
    tool_filter: list[str] | None = None,
    max_tool_calls: int | None = None,
    offered: tuple[str, ...] = OFFERED,
    preset: Any = None,
    hook: Any = None,
    turns: int = 1,
    defers: bool = True,
    dispatcher: _RecordingDispatcher | None = None,
    usage_out: dict[str, int] | None = None,
    setup: Any = None,
    **run_kwargs: Any,
) -> SimpleNamespace:
    """``turns`` turns on ``path`` answered by ``steps``: the payloads as sent, the watch records, the tool-call
    records, each turn's result, the stream's events, and the runner, client and dispatcher."""
    sent: list[dict] = []
    client = _client(steps, path == "stream_chat", sent, hook)
    client.supports_deferred_tools = defers
    cognitive = _Cognitive()
    if preset is not None:
        cognitive.preset = preset
    r = AgentRunner(cognitive, _MockBrain(), _MockHeart(), settings or _settings())
    dispatcher = dispatcher or _RecordingDispatcher(list(offered))
    r.set_dispatcher(dispatcher)
    r.set_api_client(client)
    r._log_f026_decision = lambda *a, **k: None
    log = _CallLog()
    r._tool_call_recorder = log
    blocked: list[Any] = []

    async def ledger_blocked(*args: Any, **kwargs: Any) -> None:
        blocked.append(args)

    r._ledger_blocked = ledger_blocked
    if setup is not None:
        setup(r)
    events: list[StreamEvent] = []
    raised: list[BaseException] = []  # a turn that ends in an error raises it after its records (run_turn)
    for _ in range(turns):
        if path == "stream_chat":
            events += await _pump(r.stream_chat("s1", "go"))
        elif (
            path == "run_turn"
            and context is None
            and tool_filter is None
            and max_tool_calls is None
            and not usage_out
            and not run_kwargs
        ):
            try:
                await asyncio.wait_for(r.run_turn("s1", "go"), 10)
            except RuntimeError as exc:
                raised.append(exc)
        else:
            ctx = context or ExecutionContext(kind="subtask", session_id="subtask-1", subtask_id=uuid.uuid4())
            try:
                await asyncio.wait_for(
                    r.run_turn(
                        ctx.session_id or "s",
                        "go",
                        is_subtask=ctx.kind == "subtask",
                        is_background=ctx.is_background,
                        context=ctx,
                        tool_filter=tool_filter,
                        max_tool_calls=max_tool_calls,
                        usage_out=usage_out,
                        **run_kwargs,
                    ),
                    10,
                )
            except RuntimeError as exc:
                raised.append(exc)
    session = "s1" if path != "subtask" and context is None else (context.session_id if context else "subtask-1")
    return SimpleNamespace(
        sent=sent,
        raised=raised,
        watches=client.watches,
        calls=log.calls,
        results=cognitive.results,
        events=events,
        runner=r,
        dispatched=[name for name, _ctx, _bg in dispatcher.calls],
        ledger=r._get_or_create_ledger(session).actions,
        blocked=blocked,
    )


def _names(payload: dict[str, Any]) -> list[str]:
    return [tool["name"] for tool in payload.get("tools", [])]


def _last_results(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """The last message's blocks, without the cache mark the payload builder puts on its last block."""
    return [{k: v for k, v in block.items() if k != "cache_control"} for block in payload["messages"][-1]["content"]]


def _on(config: ToolLoadingConfig = ON_ALL) -> None:
    RuntimeConfig.get().set_tool_loading(AGENT, config)


def _outcomes(turn: SimpleNamespace) -> list[tuple[str, str, bool]]:
    return [(c.tool_name, c.outcome, c.is_error) for c in turn.calls]


# ---- the request ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", PATHS)
async def test_on_differs_from_watch_only_by_the_flags_on_the_tools_outside_the_kept_set_and_the_loader(path):
    RuntimeConfig.get().set_tool_loading(AGENT, WATCH_ALL)
    watch = await _turn(path, [[("bash", {})]])
    _on()
    on = await _turn(path, [[("bash", {})]])
    assert len(watch.sent) == len(on.sent) == 2
    for watched, sent in zip(watch.sent, on.sent, strict=True):
        assert {k: v for k, v in watched.items() if k != "tools"} == {k: v for k, v in sent.items() if k != "tools"}
        offered = watched["tools"]
        wired = [{**tool, "defer_loading": True} if tool["name"] in DEFERRED else tool for tool in offered]
        loader = tool_loading.loader_tool([tool for tool in offered if tool["name"] in DEFERRED])
        assert sent["tools"] == [*wired, loader]
    assert on.sent[0]["tools"] == on.sent[1]["tools"]  # one array for the turn
    assert on.dispatched == ["bash"]


@pytest.mark.parametrize("path", PATHS)
async def test_a_load_answers_with_references_and_the_loaded_tool_then_runs(path):
    _on()
    turn = await _turn(path, [[(LOADER_NAME, {"names": ["send_email"]})], [("send_email", {"to": "a"})]])
    assert len(turn.sent) == 3
    assert _last_results(turn.sent[1]) == [
        {
            "type": "tool_result",
            "tool_use_id": "t0_0",
            "content": [{"type": "tool_reference", "tool_name": "send_email"}],
            "is_error": False,
        }
    ]
    assert turn.sent[0]["tools"] == turn.sent[1]["tools"] == turn.sent[2]["tools"]
    assert turn.dispatched == ["send_email"]
    assert _outcomes(turn) == [(LOADER_NAME, "loaded", False), ("send_email", "ran", False)]
    assert [(w["applied"], w["loaded"]) for w in turn.watches] == [(True, 0), (True, 1), (True, 1)]
    assert turn.results[0].tool_results[0].tool_name == "send_email"  # only the real call is a tool result
    assert len(turn.results[0].tool_results) == 1
    if path == "stream_chat":  # a client sees the load as an ordinary tool step
        steps = [(e.type, e.tool_name) for e in turn.events if e.type in ("tool_start", "tool_end")]
        assert steps[:2] == [("tool_start", LOADER_NAME), ("tool_end", LOADER_NAME)]


@pytest.mark.parametrize("path", PATHS)
async def test_with_reasons_on_the_loader_has_none_of_its_own_and_a_reason_given_is_recorded(path):
    _on()
    load = {"names": ["send_email"], "reason": "to send the report"}
    turn = await _turn(
        path, [[(LOADER_NAME, load)], [("send_email", {"reason": "the report"})]], _settings(tool_reason_enabled=True)
    )
    loader = turn.sent[0]["tools"][-1]
    assert loader["name"] == LOADER_NAME and "reason" not in loader["input_schema"]["properties"]
    assert all("reason" in t["input_schema"].get("properties", {}) for t in turn.sent[0]["tools"][:-1])
    assert [(c.tool_name, c.outcome, c.reason) for c in turn.calls] == [
        (LOADER_NAME, "loaded", "to send the report"),
        ("send_email", "ran", "the report"),
    ]


@pytest.mark.parametrize("path", PATHS)
async def test_what_a_load_did_not_load_follows_the_results_in_a_text_block_of_its_own(path):
    _on()
    turn = await _turn(path, [[(LOADER_NAME, {"names": ["send_email", "nope", "bash"]}), ("bash", {})]])
    blocks = _last_results(turn.sent[1])
    assert [b["type"] for b in blocks] == ["tool_result", "tool_result", "text"]
    assert blocks[0]["content"] == [{"type": "tool_reference", "tool_name": "send_email"}]  # references only
    assert blocks[2]["text"] == "load_tools: Already in your tool list: bash. Not available: nope."


@pytest.mark.parametrize("path", PATHS)
async def test_a_deferred_tool_called_before_it_was_loaded_never_runs_and_is_no_tool_failure(path):
    _on()
    turn = await _turn(path, [[("send_email", {"to": "a"})]])
    assert turn.dispatched == []
    assert turn.ledger == [] and turn.blocked == []  # no ledger row, no blocked record
    result = _last_results(turn.sent[1])[0]
    assert result["is_error"] is True and isinstance(result["content"], str)  # text only
    assert result["content"].startswith("send_email is not loaded yet, so this call did not run.")
    assert _outcomes(turn) == [("send_email", "not_loaded", False)]
    assert turn.results[0].tool_results == []


# ---- the places a not-loaded call is no failure (one each) ----------------------------------------------------------


async def _not_loaded_turn() -> SimpleNamespace:
    _on()
    return await _turn("run_turn", [[("send_email", {"to": "a"})], [(LOADER_NAME, {"names": ["nope"]})]])


async def test_a_not_loaded_call_leaves_the_decisions_confidence_alone():
    tool_results = (await _not_loaded_turn()).results[0].tool_results
    assert not any(tr.error for tr in tool_results)  # cognitive/layer.py: 0.5 when any, 0.8 otherwise


async def test_a_not_loaded_call_is_no_failed_tool_of_a_recalled_procedure():
    tool_results = (await _not_loaded_turn()).results[0].tool_results
    failed = [tr.tool_name for tr in tool_results if tr.error]  # as cognitive/layer.py builds it
    assert outcome_attribution(["send_email"], turn_error=False, failed_tools=failed) == "none"


async def test_a_not_loaded_call_is_no_surprise_and_no_error_to_recover_from_for_the_monitor():
    turn_result = (await _not_loaded_turn()).results[0]
    monitor = MonitorEngine(None, None, _settings())
    assessment = await monitor.assess(AGENT, "s1", turn_result)
    assert assessment.surprise_level == 0.0 and assessment.censor_candidates == []


@pytest.mark.parametrize("path", ["subtask"])
async def test_the_first_loads_of_a_turn_are_not_tool_calls_and_the_later_ones_reach_the_limit(path):
    """30 loads with a limit of 5 tool calls: 8 free, then 5 counted, then the closing call, with no reference."""
    _on()
    turn = await _turn(
        path, [[(LOADER_NAME, {"names": ["send_email"]})]] * 30, _settings(max_turns=50), max_tool_calls=5
    )
    assert len(turn.sent) == 8 + 5 + 1
    closing = turn.sent[-1]
    assert "tools" not in closing
    assert "tool_reference" not in json.dumps(closing["messages"])
    assert "Loaded: send_email." in json.dumps(closing["messages"])  # the first load, kept through pruning
    assert [c.outcome for c in turn.calls] == ["loaded"] + ["nothing_loaded"] * 12


async def test_the_free_loads_do_not_count_in_the_turns_tool_calls():
    _on()
    usage = {"input_tokens": 0, "output_tokens": 0, "tool_calls": 0}
    steps = [[(LOADER_NAME, {"names": ["send_email"]}), ("bash", {})], [("send_email", {})]]
    turn = await _turn("subtask", steps, usage_out=usage)
    assert turn.dispatched == ["bash", "send_email"]
    assert usage["tool_calls"] == 2  # bash and send_email; the load is not a tool call (heart.subtasks counts these)


# ---- one array for the turn -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", PATHS)
async def test_the_array_stays_the_same_for_the_turn_whatever_changes_meanwhile(path):
    _on()
    dispatcher = _RecordingDispatcher(list(OFFERED))

    def change(count: int) -> None:
        if count == 1:  # after the first request: the mode off, a new core set, a new tool registered
            RuntimeConfig.get().set_tool_loading(AGENT, ToolLoadingConfig(mode="off", core=("send_email",)))
            dispatcher.offered.append("web_fetch")

    steps = [[("bash", {})], [(LOADER_NAME, {"names": ["send_email"]})]]
    turn = await _turn(path, steps, hook=change, dispatcher=dispatcher)
    assert len(turn.sent) == 3
    assert turn.sent[0]["tools"] == turn.sent[1]["tools"] == turn.sent[2]["tools"]
    assert "web_fetch" not in _names(turn.sent[2]) and LOADER_NAME in _names(turn.sent[2])
    assert all(w["mode"] == "on" and w["applied"] for w in turn.watches)


@pytest.mark.parametrize("path", PATHS)
async def test_the_next_turn_starts_with_every_tool_outside_the_kept_set_deferred_and_no_reference(path):
    _on()
    steps = [[(LOADER_NAME, {"names": ["send_email"]})], TEXT, [("bash", {})]]
    turn = await _turn(path, steps, turns=2)
    first, second = turn.sent[0], turn.sent[2]
    assert second["tools"] == first["tools"]
    assert "tool_reference" not in json.dumps(second["messages"])


@pytest.mark.parametrize("path", ["run_turn", "stream_chat"])
async def test_the_closing_call_with_no_tools_carries_words_instead_of_references(path):
    _on()
    turn = await _turn(path, [[(LOADER_NAME, {"names": ["send_email"]})], [("bash", {})]], _settings(max_turns=2))
    closing = turn.sent[-1]
    assert "tools" not in closing
    assert "tool_reference" not in json.dumps(closing["messages"])
    assert "Loaded: send_email." in json.dumps(closing["messages"])


# ---- the fallback ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", PATHS)
async def test_a_refused_request_is_sent_once_more_in_full_and_so_is_the_rest_of_the_turn(path, caplog):
    _on()
    steps = [REFUSED, [(LOADER_NAME, {"names": ["send_email"]})], [("send_email", {})]]
    with caplog.at_level(logging.WARNING, logger="nous.api.runner"):
        turn = await _turn(path, steps)
    assert len(turn.sent) == 4
    deferred = turn.sent[0]["tools"]
    in_full = [{k: v for k, v in tool.items() if k != "defer_loading"} for tool in deferred]
    assert any(tool.get("defer_loading") for tool in deferred)
    assert turn.sent[1]["tools"] == turn.sent[2]["tools"] == turn.sent[3]["tools"] == in_full
    assert turn.sent[1]["messages"] == turn.sent[0]["messages"]  # the same request, sent again
    assert _last_results(turn.sent[2])[0]["content"] == LOADER_ALL_ATTACHED
    assert turn.dispatched == ["send_email"]
    assert [w["fallback"] for w in turn.watches if w is not None][-3:] == ["refused"] * 3
    faults = [r.getMessage() for r in caplog.records if r.getMessage().startswith("F120: tool loading fell back")]
    assert faults == ["F120: tool loading fell back (refused); this turn sends every tool in full"]


@pytest.mark.parametrize("path", ["run_turn", "stream_chat"])
async def test_a_second_refusal_or_another_status_is_not_sent_again(path):
    _on()
    twice = await _turn(path, [REFUSED, REFUSED])
    assert len(twice.sent) == 2  # the refused fallback is not sent a third time
    other = await _turn(path, [_refused(500)])
    assert len(other.sent) == 1
    if path == "stream_chat":  # the error reaches the client as today
        assert [e.type for e in twice.events if e.type == "error"] == ["error"]
    else:
        assert len(twice.raised) == len(other.raised) == 1


@pytest.mark.parametrize("path", PATHS)
async def test_a_fault_in_the_windows_own_code_sends_every_tool_in_full_from_then_on(path, monkeypatch, caplog):
    _on()

    def explode(self, name, tool_input, resolve=None):
        raise RuntimeError("loader broke")

    monkeypatch.setattr(tool_loading.ToolWindow, "answer", explode)
    with caplog.at_level(logging.WARNING, logger="nous.api.runner"):
        turn = await _turn(path, [[(LOADER_NAME, {"names": ["send_email"]}), ("send_email", {})]])
    assert turn.dispatched == []
    blocks = _last_results(turn.sent[1])
    assert blocks[0]["content"] == LOADER_ALL_ATTACHED
    assert blocks[1]["is_error"] is True and blocks[1]["content"].startswith("send_email did not run.")
    assert not any(tool.get("defer_loading") for tool in turn.sent[1]["tools"])
    assert turn.watches[-1]["fallback"] == "fault"
    faults = [r.getMessage() for r in caplog.records if r.getMessage().startswith("F120: tool loading fell back")]
    assert faults == ["F120: tool loading fell back (fault, RuntimeError); this turn sends every tool in full"]


# ---- when On applies ------------------------------------------------------------------------------------------------


async def _sent_off(path: str, **kwargs: Any) -> list[dict]:
    return (await _turn(path, [[("bash", {})]], _settings(tool_loading_enabled=False), **kwargs)).sent


@pytest.mark.parametrize(
    ("settings", "config", "client_defers", "why"),
    [
        ({}, ON_ALL, False, "client"),
        ({"model": "claude-sonnet-4-6"}, ON_ALL, True, "model"),
        ({}, ToolLoadingConfig(mode="on", kinds=frozenset(KINDS), core=("db_query", *DEFERRED)), True, "nothing"),
    ],
)
@pytest.mark.parametrize("path", PATHS)
async def test_on_that_cannot_defer_leaves_the_turn_as_today_and_records_why(
    path, settings, config, client_defers, why
):
    off = await _turn(path, [[("bash", {})]], _settings(tool_loading_enabled=False, **settings))
    _on(config)
    on = await _turn(path, [[("bash", {})]], _settings(**settings), defers=client_defers)
    assert on.sent == off.sent
    assert [(w["mode"], w["applied"], w["why"]) for w in on.watches] == [("on", False, why)] * 2


async def test_a_kind_switched_off_or_watch_only_records_as_before():
    _on(ToolLoadingConfig(mode="on", kinds=frozenset({"subtask"})))
    chat = await _turn("run_turn", [[("bash", {})]])
    assert chat.watches == [None, None] and chat.sent == await _sent_off("run_turn")
    RuntimeConfig.get().set_tool_loading(AGENT, WATCH_ALL)
    watched = await _turn("run_turn", [[("bash", {})]])
    assert [w["mode"] for w in watched.watches] == ["watch", "watch"]


async def test_a_turn_that_began_before_on_was_chosen_stays_as_it_began():
    RuntimeConfig.get().set_tool_loading(AGENT, WATCH_ALL)

    def switch(count: int) -> None:
        if count == 1:
            _on()

    turn = await _turn("run_turn", [[("bash", {})]], hook=switch)
    assert not any(tool.get("defer_loading") for payload in turn.sent for tool in payload["tools"])
    assert [(w["mode"], w.get("why")) for w in turn.watches] == [("watch", None), ("on", "turn")]


async def test_a_turn_of_internal_authority_and_one_with_its_own_tool_list_are_untouched():
    internal = ExecutionContext(
        kind="subtask", session_id="subtask-2", subtask_id=uuid.uuid4(), authority=AUTHORITY_INTERNAL
    )
    check = ExecutionContext(kind="heartbeat_check", session_id="dynamic-check-1")
    off_internal = await _sent_off("subtask", context=internal)
    off_filtered = await _sent_off("subtask", context=check, tool_filter=["bash", "send_email"])
    _on()
    on_internal = await _turn("subtask", [[("bash", {})]], context=internal)
    on_filtered = await _turn("subtask", [[("bash", {})]], context=check, tool_filter=["bash", "send_email"])
    assert on_internal.sent == off_internal and on_filtered.sent == off_filtered
    assert on_internal.watches == on_filtered.watches == [None, None]


# ---- what a turn may use is as today, layer by layer ----------------------------------------------------------------


async def _layer(path: str, tool: str, settings: Settings, **kwargs: Any) -> tuple[SimpleNamespace, SimpleNamespace]:
    """The same turn (load ``tool``, then call it) off and in On."""
    steps = [[(LOADER_NAME, {"names": [tool]})], [(tool, {})]]
    off = await _turn(path, steps, _off(settings), **kwargs)
    _on()
    on = await _turn(path, steps, settings, **kwargs)
    return off, on


def _off(settings: Settings) -> Settings:
    return settings.model_copy(update={"tool_loading_enabled": False})


def _kept_from(on: SimpleNamespace, tool: str) -> None:
    """``tool`` is neither in the array nor in the loader's list, the load names it not available, and the call to it
    is answered as today."""
    assert tool not in _names(on.sent[0])
    loader = on.sent[0]["tools"][-1]
    assert loader["name"] == LOADER_NAME and tool not in loader["description"]
    assert _last_results(on.sent[1])[0]["content"] == f"Nothing was loaded. Not available: {tool}."


async def test_a_tool_the_guard_holds_is_neither_listed_nor_loadable_nor_callable():
    tool_guard.install(ToolGuard(locked=frozenset({"send_email"})))
    off, on = await _layer("subtask", "send_email", _settings())
    _kept_from(on, "send_email")
    assert on.dispatched == [] and "send_email" not in off.dispatched
    refused = [c for c in on.calls if c.tool_name == "send_email"]
    assert [(c.outcome, c.refused_by) for c in refused] == [("refused", "tool_guard")]
    assert [(c.outcome, c.refused_by) for c in off.calls if c.tool_name == "send_email"][-1:] == [
        ("refused", "tool_guard")
    ]


async def test_a_tool_the_frame_does_not_offer_is_neither_listed_nor_loadable_nor_callable():
    settings = _settings(tool_offered_set_enforcement_mode="enforce")
    off, on = await _layer(
        "run_turn", "send_email", settings, offered=("bash", "recall_deep", "db_query", "dag_create")
    )
    _kept_from(on, "send_email")
    assert on.dispatched == [] and "send_email" not in off.dispatched
    assert [(c.outcome, c.refused_by) for c in on.calls if c.tool_name == "send_email"] == [("refused", "offered_set")]


async def test_a_tool_a_refuse_censor_strips_is_neither_listed_nor_loadable_nor_callable():
    settings = _settings(tool_offered_set_enforcement_mode="enforce")
    preset = _preset(refuse_active=True)
    # list_decisions only reads, so the censor leaves it, and it is outside the kept set: the turn still defers
    offered = (*OFFERED, "list_decisions")
    off, on = await _layer("run_turn", "send_email", settings, preset=preset, offered=offered)
    assert "list_decisions" in on.sent[0]["tools"][-1]["description"]
    _kept_from(on, "send_email")
    assert on.dispatched == [] and "send_email" not in off.dispatched
    assert [(c.outcome, c.refused_by) for c in on.calls if c.tool_name == "send_email"] == [("refused", "offered_set")]


async def test_a_tool_of_a_server_the_mcp_client_does_not_hold_is_neither_listed_nor_loadable_nor_callable():
    name = "mcp__nowhere__lookup"
    off, on = await _layer("run_turn", name, _settings(), offered=(*OFFERED, name))
    _kept_from(on, name)
    assert on.dispatched == [] and name not in off.dispatched
    assert [(c.outcome, c.refused_by) for c in on.calls if c.tool_name == name] == [("refused", "context_policy")]


# ---- the estimates --------------------------------------------------------------------------------------------------


class _Detector:
    def __init__(self) -> None:
        self.tools_json: list[str] = []

    def last_semi_stable_hash(self) -> None:
        return None

    def check(self, **kwargs: Any) -> None:
        self.tools_json.append(kwargs["tools_json"])
        return None


async def test_the_estimate_and_the_cache_check_count_only_the_tools_sent_in_full():
    turn = await _turn("run_turn", [])
    r = turn.runner
    r._cache_break_detector = detector = _Detector()
    tools = [{"name": n, "description": n, "input_schema": {"type": "object"}} for n in OFFERED]
    window = tool_loading.open_window(ON_ALL, "chat", tools)
    window.request_tools()
    system = {"static": "You are Nous.", "semi_stable": "", "dynamic": ""}
    messages = [{"role": "user", "content": "go"}]
    context = CallContext(tool_loading_kind="chat", tool_window=window)
    payload, deferring = r._build_api_payload_and_context(system, messages, window.tools, call_context=context)
    _payload, plain = r._build_api_payload_and_context(system, messages, tools, call_context=CallContext())
    in_full = [t for t in window.tools if not t.get("defer_loading")]
    assert payload["tools"] == window.tools
    assert detector.tools_json == [json.dumps(in_full), json.dumps(tools)]
    assert deferring.stats.section_tokens_est["tools_definition"] == len(json.dumps(in_full)) // 4
    assert plain.stats.section_tokens_est["tools_definition"] == len(json.dumps(tools)) // 4
    assert deferring.stats.tool_names == [*OFFERED, LOADER_NAME] and deferring.stats.tools_count == 6
    assert deferring.stats.tool_loading["applied"] is True


# ---- pruning --------------------------------------------------------------------------------------------------------


def test_pruning_keeps_a_loads_references_and_the_words_after_the_results():
    compactor = ConversationCompactor(_settings())
    load = {
        "type": "tool_result",
        "tool_use_id": "a",
        "content": [{"type": "tool_reference", "tool_name": "send_email"}],
    }
    note = {"type": "text", "text": "load_tools: Not available: nope."}
    messages: list[dict[str, Any]] = [{"role": "user", "content": "go"}]
    messages += [
        {"role": "assistant", "content": [{"type": "tool_use", "id": "a", "name": LOADER_NAME, "input": {}}]},
        {"role": "user", "content": [load, {"type": "tool_result", "tool_use_id": "b", "content": "x" * 900}, note]},
    ]
    for i in range(20):  # old enough for every tier
        messages += [
            {"role": "assistant", "content": [{"type": "tool_use", "id": f"c{i}", "name": "bash", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"c{i}", "content": "y" * 900}]},
        ]
    before = json.loads(json.dumps(messages[2]["content"]))  # pruning changes the blocks in place
    compactor.prune_tool_results(messages)
    first = messages[2]["content"]
    assert first[0] == before[0]  # the references are kept
    assert first[2] == before[2] and "content" not in first[2]  # the words are not a result: nothing added
    assert first[1]["content"] == "[Tool output cleared - content was processed in earlier turns]"  # as before


# ---- the counts that read tool_call_log ---------------------------------------------------------------------------


async def _rows(db, agent: str, session: str, turns: int) -> list[uuid.UUID]:
    from tests.test_work_view_dashboard import insert_call

    now = datetime.now(UTC)
    turn_ids = []
    for _ in range(turns):
        turn = uuid.uuid4()
        turn_ids.append(turn)
        await insert_call(
            db,
            agent,
            turn,
            0,
            session_id=session,
            tool="send_email",
            input_text='{"to": "a"}',
            outcome="not_loaded",
            started_at=now - timedelta(minutes=5),
        )
        await insert_call(
            db,
            agent,
            turn,
            1,
            session_id=session,
            tool=LOADER_NAME,
            input_text='{"names": ["x"]}',
            outcome="loaded",
            started_at=now - timedelta(minutes=4),
        )
        await insert_call(
            db,
            agent,
            turn,
            2,
            session_id=session,
            tool="send_email",
            input_text='{"to": "a"}',
            outcome="ran",
            started_at=now - timedelta(minutes=3),
        )
    return turn_ids


@pytest.mark.postgres_only
async def test_a_not_loaded_call_and_its_retry_are_no_repeat_and_no_call_in_the_pattern_counts(db):
    from nous.api.work_view_dashboard import get_patterns
    from nous.observability.tool_patterns import detect_patterns, pattern_counts
    from tests.test_work_view_dashboard import delete_agent_rows

    agent = f"test-f120-on-{uuid.uuid4().hex[:12]}"
    try:
        await _rows(db, agent, "s1", turns=3)
        now = datetime.now(UTC)
        async with db.session() as session:
            patterns, _skipped = await detect_patterns(session, agent, now=now, min_hits=1)
            counts = await pattern_counts(
                session, agent, "send_email", "call_repeat", "", since=now - timedelta(days=1), until=now
            )
            totals = await get_patterns(session, agent, days=1)
        assert patterns == []
        assert counts is not None and counts[1] == 3  # the three calls that ran, not the not-loaded answers
        assert (totals["wasted"]["repeated_calls"], totals["wasted"]["retried_and_failed_again"]) == (0, 0)
    finally:
        await delete_agent_rows(db, agent)


@pytest.mark.postgres_only
async def test_the_outside_judge_counts_no_load_and_no_not_loaded_answer(db):
    from nous.judge import evidence
    from tests.test_work_view_dashboard import delete_agent_rows

    agent = f"test-f120-on-{uuid.uuid4().hex[:12]}"
    try:
        await _rows(db, agent, "s1", turns=1)
        now = datetime.now(UTC)
        window = {"agent": agent, "session": "s1", "since": now - timedelta(hours=1), "until": now, "limit": 50}
        async with db.session() as session:
            counts = (await session.execute(evidence._CALL_COUNTS, window)).one()
            calls = (await session.execute(evidence._CALLS, window)).mappings().all()
        assert tuple(counts) == (1, 0)
        assert [(c["tool_name"], c["outcome"]) for c in calls] == [("send_email", "ran")]
    finally:
        await delete_agent_rows(db, agent)


# ---- found and loaded by the name shown; a fallback carries no reference; the rest of the review ----------------


@pytest.mark.parametrize("path", PATHS)
async def test_a_refusal_after_a_load_is_sent_again_in_full_with_no_reference_and_nothing_runs_twice(path):
    _on()
    steps = [[(LOADER_NAME, {"names": ["send_email"]})], REFUSED, [("send_email", {"to": "a"})]]
    turn = await _turn(path, steps)
    assert len(turn.sent) == 4
    refused, retried, after = turn.sent[1], turn.sent[2], turn.sent[3]
    assert "tool_reference" in json.dumps(refused["messages"])  # the request the API refused
    for payload in (retried, after):
        assert not any(tool.get("defer_loading") for tool in payload["tools"])
        assert _names(payload) == _names(refused)  # the same tools, in full
        assert "tool_reference" not in json.dumps(payload["messages"])
        assert "Loaded: send_email." in json.dumps(payload["messages"])
    assert turn.dispatched == ["send_email"]


@pytest.mark.parametrize("path", PATHS)
async def test_a_refusal_not_caused_by_deferral_surfaces_after_the_one_retry_and_no_tool_runs_twice(path):
    _on()
    turn = await _turn(path, [[("bash", {})], REFUSED, REFUSED])
    assert len(turn.sent) == 3 and turn.dispatched == ["bash"]
    if path == "stream_chat":
        assert [e.type for e in turn.events if e.type == "error"] == ["error"]
    else:
        assert len(turn.raised) == 1


async def test_a_stream_that_fails_after_its_first_content_is_not_sent_again():
    _on()
    started = [
        StreamEvent(type="message_start", usage={"input_tokens": 3}),
        StreamEvent(type="text_block_start", block_index=0),
        StreamEvent(type="text_delta", text="Let me", block_index=0),
        StreamEvent(type="error", text="SDK stream error: Error code: 400", status=400),
    ]
    turn = await _turn("stream_chat", [started])
    assert len(turn.sent) == 1
    assert [e.type for e in turn.events if e.type == "error"] == ["error"]


@pytest.mark.parametrize("path", PATHS)
async def test_a_load_a_kept_tool_and_a_call_not_loaded_in_one_step_with_the_cache_mark_on_the_last_block(path):
    _on()
    step = [(LOADER_NAME, {"names": ["send_email", "nope"]}), ("bash", {}), ("dag_create", {})]
    turn = await _turn(path, [step])
    blocks = turn.sent[1]["messages"][-1]["content"]
    assert [b["type"] for b in blocks] == ["tool_result", "tool_result", "tool_result", "text"]
    assert blocks[0]["content"] == [{"type": "tool_reference", "tool_name": "send_email"}]
    assert blocks[1]["is_error"] is False and blocks[2]["is_error"] is True and isinstance(blocks[2]["content"], str)
    assert [("cache_control" in b) for b in blocks] == [False, False, False, True]  # as the payload builder marks it
    assert turn.dispatched == ["bash"]


@pytest.mark.parametrize("path", PATHS)
async def test_a_loader_call_whose_input_could_not_be_read_is_answered_by_the_window_and_is_no_failure(path):
    _on()
    turn = await _turn(path, [[(LOADER_NAME, MALFORMED)], [(LOADER_NAME, {"names": ["send_email"]})]])
    result = _last_results(turn.sent[1])[0]
    assert result == {
        "type": "tool_result",
        "tool_use_id": "t0_0",
        "content": tool_loading.LOADER_UNREADABLE,
        "is_error": True,
    }
    assert _outcomes(turn) == [(LOADER_NAME, "nothing_loaded", False), (LOADER_NAME, "loaded", False)]
    assert turn.results[0].tool_results == []


async def test_a_malformed_loader_call_is_one_of_the_turns_free_loads():
    _on()
    usage = {"input_tokens": 0, "output_tokens": 0, "tool_calls": 0}
    await _turn("subtask", [[(LOADER_NAME, MALFORMED), ("bash", {})]], usage_out=usage)
    assert usage["tool_calls"] == 1  # bash only


@pytest.mark.parametrize("path", PATHS)
async def test_a_tool_already_named_like_the_loader_keeps_the_turn_as_today_and_says_why(path):
    offered = (*OFFERED, LOADER_NAME)
    off = await _turn(path, [[("bash", {})]], _settings(tool_loading_enabled=False), offered=offered)
    _on()
    on = await _turn(path, [[("bash", {})]], offered=offered)
    assert on.sent == off.sent
    assert [(w["applied"], w["why"]) for w in on.watches] == [(False, "name")] * 2


@pytest.mark.parametrize("path", PATHS)
async def test_a_fault_opening_the_window_leaves_the_turn_as_today_and_says_why(path, monkeypatch, caplog):
    off = await _turn(path, [[("bash", {})]], _settings(tool_loading_enabled=False))

    def explode(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("cannot open")

    monkeypatch.setattr(tool_loading, "open_window", explode)
    _on()
    with caplog.at_level(logging.WARNING, logger="nous.api.runner"):
        on = await _turn(path, [[("bash", {})]])
    assert on.sent == off.sent
    assert [(w["applied"], w["why"]) for w in on.watches] == [(False, "fault")] * 2
    faults = [r.getMessage() for r in caplog.records if r.getMessage().startswith("F120: tool loading fell back")]
    assert faults == ["F120: tool loading fell back (fault, RuntimeError); this turn sends every tool in full"]


async def test_the_estimators_calibration_skips_a_request_that_holds_a_load():
    _on()
    calibrated: list[int] = []

    def watch(r: AgentRunner) -> None:
        r._compactor = ConversationCompactor(_settings())
        r._compactor.estimator.calibrate = lambda chars, tokens: calibrated.append(chars)

    steps = [[("bash", {})], [(LOADER_NAME, {"names": ["send_email"]})], [("send_email", {})]]
    turn = await _turn("run_turn", steps, setup=watch)
    assert len(turn.sent) == 4
    assert len(calibrated) == 2  # the two requests before the load; not the two that carry its reference


async def test_the_free_loads_do_not_move_the_forced_reports_threshold():
    """With thinking off a subtask forces its report once it has made max_tool_calls - 2 tool calls: 8 loads first move
    nothing, so the 3rd tool call after them is the one that brings the forced choice."""
    _on()

    async def report(**_kwargs: Any) -> tuple[str, bool]:
        return "accepted", False

    schema = {
        "name": "submit_final_report",
        "description": "Hand in the final report.",
        "input_schema": {"type": "object", "properties": {"report": {"type": "string"}}},
    }
    steps = [*[[(LOADER_NAME, {"names": ["nope"]})]] * 8, *[[("bash", {})]] * 3]
    turn = await _turn(
        "subtask",
        steps,
        _settings(thinking_mode="off", max_turns=50),
        max_tool_calls=5,
        extra_tools={"submit_final_report": (schema, report)},
        force_tool_on_penultimate="submit_final_report",
    )
    forced = [i for i, payload in enumerate(turn.sent) if "tool_choice" in payload]
    assert forced and forced[0] == 8 + 3  # the request after the 3rd real tool call
    assert turn.sent[forced[0]]["tool_choice"] == {"type": "tool", "name": "submit_final_report"}
    assert "submit_final_report" in _names(turn.sent[forced[0]])
    assert not turn.sent[forced[0]]["tools"][_names(turn.sent[forced[0]]).index("submit_final_report")].get(
        "defer_loading"
    )


async def test_a_tool_a_goal_has_not_granted_is_as_today_because_a_goal_session_never_defers():
    """A goal's turns are of internal authority: a tool its goal has not granted is narrowed out of the offered set as
    today, and On neither lists it nor opens a window (the payload is the same as with the setting off)."""
    ctx = ExecutionContext(
        kind="subtask", session_id="goal-subtask-1", subtask_id=uuid.uuid4(), authority=AUTHORITY_INTERNAL
    )

    def no_grant(r: AgentRunner) -> None:
        r.set_goal_lift(lambda _ctx, _name: False)

    steps = [[("send_email", {"to": "a"})]]
    off = await _turn("subtask", steps, _settings(tool_loading_enabled=False), context=ctx, setup=no_grant)
    _on()
    on = await _turn("subtask", steps, context=ctx, setup=no_grant)
    assert on.sent == off.sent and on.watches == [None, None]
    assert "send_email" not in _names(on.sent[0]) and LOADER_NAME not in _names(on.sent[0])
    assert on.dispatched == off.dispatched == []
    assert [(c.outcome, c.refused_by) for c in on.calls] == [(c.outcome, c.refused_by) for c in off.calls]


@pytest.mark.postgres_only
async def test_the_work_views_session_list_leaves_loads_and_not_loaded_answers_out(db):
    from nous.api.work_view_dashboard import get_sessions
    from tests.test_work_view_dashboard import delete_agent_rows, insert_turn

    agent = f"test-f120-on-{uuid.uuid4().hex[:12]}"
    try:
        turn_ids = await _rows(db, agent, "s-work", turns=1)
        await insert_turn(db, agent, "s-work", turn_id=turn_ids[0], tool_call_count=3)
        async with db.session() as session:
            listed = await get_sessions(
                session, agent, kind="all", days=1, tool=None, q=None, lens=None, limit=10, before=None
            )
        (row,) = listed["sessions"]
        assert row["tool_calls"] == 1  # the call that ran
        assert "repeat" not in row["flags"]  # a not-loaded answer and its retry are not a repeat
        assert [t["tool"] for t in row["tools"]] == ["send_email"]
    finally:
        await delete_agent_rows(db, agent)


@pytest.mark.parametrize("path", PATHS)
async def test_after_a_load_a_call_no_longer_counts_the_loaded_definition_as_saved(path):
    _on()
    turn = await _turn(path, [[(LOADER_NAME, {"names": ["send_email"]})], [("send_email", {"to": "a"})]])
    before, after = turn.watches[0], turn.watches[1]
    send_email = next(t for t in turn.sent[0]["tools"] if t["name"] == "send_email")
    definition = tool_loading.tokens_est({k: v for k, v in send_email.items() if k != "defer_loading"})
    assert before["deferred_tokens_est"] - after["deferred_tokens_est"] == definition
    assert (before["loaded"], before["loaded_tokens_est"]) == (0, 0)
    assert (after["loaded"], after["loaded_tokens_est"]) == (1, definition)
    assert before["index_tokens_est"] == after["index_tokens_est"]
