"""Old tool names keep working: an alias becomes the call to its target before any harness check, a retired name
answers with its message and runs nothing (nous/tool_names.py, ToolDispatcher.register_alias/retire,
AgentRunner._resolve_alias)."""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest
from test_runner_authorization import _MockBrain, _MockCognitive, _MockHeart, _run_loop, _settings
from test_runner_ledger import _FakeStore

from nous.api.anthropic_client import StreamEvent
from nous.api.execution_context import ExecutionContext
from nous.api.models import ApiResponse
from nous.api.runner import AgentRunner
from nous.api.tool_classes import TOOL_CLASSES
from nous.api.tools import ToolDispatcher
from nous.tool_names import CHANGED_TOOLS, canonical_tool_name, canonical_tool_names, changed_tools_note

RECENT_NOTE = "recall_recent is now recall_deep(hours=N): pass hours to list recent episodes."
RESOLVE_NOTE = "resolve_decision is now resolve_decisions with one item."
HUBS_MESSAGE = (
    "recall_hubs was retired (unused). To explore a topic's connected memories, call recall_deep with the topic."
)
DECISION = "00000000-0000-0000-0000-000000000001"


# ---- the mapping -------------------------------------------------------------------------------------------------


def test_the_mapping_holds_the_owner_approved_names_and_messages():
    assert {name: change.target for name, change in CHANGED_TOOLS.items()} == {
        "recall_recent": "recall_deep",
        "resolve_decision": "resolve_decisions",
        "recall_hubs": None,
    }
    assert CHANGED_TOOLS["recall_recent"].note == RECENT_NOTE
    assert CHANGED_TOOLS["resolve_decision"].note == RESOLVE_NOTE
    assert CHANGED_TOOLS["recall_hubs"].note == HUBS_MESSAGE


def test_the_translations():
    recent = CHANGED_TOOLS["recall_recent"].translate
    assert recent({"hours": 12, "limit": 3}) == {"hours": 12, "limit": 3}
    assert recent({}) == {"hours": 48}  # recall_recent() listed the last 48 hours
    resolve = CHANGED_TOOLS["resolve_decision"].translate
    call = {"decision_id": DECISION, "outcome": "noise", "resolution_note": "tick", "superseded_by": None}
    assert resolve(call) == {"resolutions": [call]}
    assert resolve({"decision_id": DECISION, "outcome": "noise"}) == {
        "resolutions": [{"decision_id": DECISION, "outcome": "noise"}]
    }
    original = {"hours": 5}
    recent(original)
    assert original == {"hours": 5}  # never mutated in place


def test_old_names_have_no_tool_class():
    """They never reach the ledger: the runner rewrites them first."""
    assert not set(CHANGED_TOOLS) & set(TOOL_CLASSES)


def test_canonical_names_map_aliases_drop_retired_and_dedup():
    assert canonical_tool_name("recall_recent") == "recall_deep"
    assert canonical_tool_name("bash") == "bash"
    assert canonical_tool_name("recall_hubs") is None
    assert canonical_tool_names(["recall_deep", "recall_recent", "recall_hubs", "bash", "bash"]) == [
        "recall_deep",
        "bash",
    ]


def test_the_recall_time_note_lists_only_the_names_present():
    assert changed_tools_note("use recall_deep and resolve_decisions") is None
    assert changed_tools_note("") is None
    note = changed_tools_note("Always call recall_recent first.")
    assert note == "Note: some memories name tools that changed: recall_recent → recall_deep(hours=…)."
    both = changed_tools_note("recall_hubs, then resolve_decision")
    assert "resolve_decision → resolve_decisions" in both and "recall_hubs" in both and "recall_recent" not in both
    assert changed_tools_note("my_recall_recent_helper") is None  # a whole word only


# ---- the dispatcher ----------------------------------------------------------------------------------------------


def _dispatcher(calls: list | None = None) -> ToolDispatcher:
    """recall_deep and resolve_decisions stand-ins, the two aliases and the retirement, as register_nous_tools
    wires them."""
    calls = calls if calls is not None else []

    def handler(name: str):
        async def run(**kwargs):
            calls.append((name, {k: v for k, v in kwargs.items() if not k.startswith("_")}))
            return {"content": [{"type": "text", "text": f"{name} ran"}]}

        return run

    d = ToolDispatcher()
    d.register(
        "recall_deep",
        handler("recall_deep"),
        {"type": "object", "properties": {"query": {"type": "string"}, "hours": {"type": "integer"}}},
    )
    d.register(
        "resolve_decisions",
        handler("resolve_decisions"),
        {"type": "object", "properties": {"resolutions": {"type": "array"}}, "required": ["resolutions"]},
    )
    for old, change in CHANGED_TOOLS.items():
        if change.target is None:
            d.retire(old, change.note)
        else:
            d.register_alias(old, change.target, change.translate, change.note)
    return d


def test_aliases_are_never_offered_nor_registered():
    d = _dispatcher()
    assert [t["name"] for t in d.tool_definitions()] == ["recall_deep", "resolve_decisions"]
    assert [t["name"] for t in d.available_tools("conversation")] == ["recall_deep", "resolve_decisions"]
    for old in CHANGED_TOOLS:
        assert not d.is_registered(old)
        assert d.alias_for(old) is not None
    assert d.alias_for("recall_deep") is None


def test_an_alias_cannot_shadow_a_tool_or_point_at_nothing():
    d = _dispatcher()
    with pytest.raises(ValueError):
        d.register_alias("recall_deep", "resolve_decisions", dict, "x")
    with pytest.raises(ValueError):
        d.register_alias("old_name", "not_registered", dict, "x")
    with pytest.raises(ValueError):
        d.retire("recall_deep", "x")


def test_validate_call_judges_an_alias_as_the_call_it_becomes():
    d = _dispatcher()
    assert d.validate_call("resolve_decision", {"decision_id": DECISION, "outcome": "noise"}) == []
    assert d.validate_call("recall_recent", {}) == []
    assert d.validate_call("recall_hubs", {}) == [HUBS_MESSAGE]
    assert d.validate_call("never_was", {}) == ["never_was is not a registered tool"]


# ---- the runner: both loops and the approved-action path ---------------------------------------------------------


class _InputStore(_FakeStore):
    """Also records the input each durable row was opened with."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.inputs: list[tuple[str, dict]] = []

    async def open_entry(self, *, context, tool_name, tool_input, turn, idempotency_key=None):
        self.inputs.append((tool_name, tool_input))
        return await super().open_entry(
            context=context, tool_name=tool_name, tool_input=tool_input, turn=turn, idempotency_key=idempotency_key
        )


def _runner(calls: list, store=None, **settings):
    r = AgentRunner(_MockCognitive(), _MockBrain(), _MockHeart(), _settings(**settings))
    r.set_dispatcher(_dispatcher(calls))
    if store is not None:
        r.set_ledger_store(store)
    events: list[tuple[str, dict]] = []
    r._log_f026_decision = lambda kind, data, session_id: events.append((kind, data))
    return r, events


def _one_call(name: str, tool_input: dict):
    sent: list[list] = []

    async def fake_call_api(
        system_prompt, messages, tools=None, skip_thinking=False, model_override=None, is_background=False
    ):
        sent.append(messages)
        if len(sent) == 1:
            return ApiResponse(
                content=[{"type": "tool_use", "id": "t1", "name": name, "input": tool_input}], stop_reason="tool_use"
            )
        return ApiResponse(content=[{"type": "text", "text": "done"}], stop_reason="end_turn")

    return fake_call_api, sent


def _tool_result(messages: list) -> dict:
    (block,) = [
        b
        for m in messages
        if isinstance(m.get("content"), list)
        for b in m["content"]
        if isinstance(b, dict) and b.get("type") == "tool_result"
    ]
    return block


@pytest.mark.asyncio
async def test_the_tool_loop_runs_an_alias_as_its_target():
    calls: list = []
    store = _InputStore()
    # enforce: an alias checked under its own name would be refused as not offered
    r, events = _runner(calls, store, tool_offered_set_enforcement_mode="enforce")
    seen: list[str] = []
    authorize = r._authorize_tool_call

    def recording(ctx, tool_name, offered_names, session_id, tool_input):
        seen.append(tool_name)
        return authorize(ctx, tool_name, offered_names, session_id, tool_input)

    r._authorize_tool_call = recording
    call = {"decision_id": DECISION, "outcome": "noise", "resolution_note": "tick"}
    r._call_api, sent = _one_call("resolve_decision", call)
    _text, results, _u, _t = await _run_loop(r, is_background=True, context=ExecutionContext(kind="subtask"))

    assert seen == ["resolve_decisions"]
    assert calls == [("resolve_decisions", {"resolutions": [call]})]
    assert store.inputs == [("resolve_decisions", {"resolutions": [call]})]  # the durable row
    assert store.keys == [None]  # its key is derived under the target too (an unkeyed tool has none)
    assert ("open", "resolve_decisions", "subtask") in store.events
    (res,) = results
    assert res.tool_name == "resolve_decisions" and res.arguments == {"resolutions": [call]}
    content = _tool_result(sent[1])["content"]
    assert content.split("\n")[0] == RESOLVE_NOTE and content.endswith("resolve_decisions ran")
    assert (
        "tool_alias_used",
        {"alias": "resolve_decision", "target": "resolve_decisions", "context_kind": "subtask"},
    ) in events
    # the assistant message keeps what the model emitted
    (assistant,) = [m for m in sent[1] if m["role"] == "assistant"]
    (use,) = [b for b in assistant["content"] if b.get("type") == "tool_use"]
    assert use["name"] == "resolve_decision" and use["input"] == call


@pytest.mark.asyncio
async def test_a_retired_name_answers_its_message_and_runs_nothing():
    calls: list = []
    store = _InputStore()
    r, events = _runner(calls, store)
    r._call_api, sent = _one_call("recall_hubs", {"limit": 5})
    _text, results, _u, _t = await _run_loop(r, is_background=True, context=ExecutionContext(kind="subtask"))
    assert calls == [] and store.events == []  # nothing dispatched, nothing in any ledger
    result = _tool_result(sent[1])
    assert result["content"] == HUBS_MESSAGE and result["is_error"] is True
    (res,) = results
    assert res.error == HUBS_MESSAGE
    assert ("tool_alias_used", {"alias": "recall_hubs", "target": None, "context_kind": "subtask"}) in events


@pytest.mark.asyncio
async def test_a_refused_alias_call_names_the_change_first():
    """A refusal names the target, so the note says which tool that is."""
    r, _events = _runner([], tool_offered_set_enforcement_mode="enforce")
    r._call_api, sent = _one_call("recall_recent", {"hours": 6})
    await _run_loop(r, tool_filter=["resolve_decisions"], context=ExecutionContext(kind="subtask"))
    content = _tool_result(sent[1])["content"]
    assert content.split("\n")[0] == RECENT_NOTE and "'recall_deep' is not available" in content


@pytest.mark.asyncio
async def test_stream_chat_runs_an_alias_as_its_target():
    from test_streaming import _make_mock_cognitive, _make_mock_settings, _make_runner

    cognitive, _ = _make_mock_cognitive()
    runner = _make_runner(cognitive, _make_mock_settings())
    calls: list = []
    runner.set_dispatcher(_dispatcher(calls))
    store = _InputStore()
    runner.set_ledger_store(store)
    events: list = []
    runner._log_f026_decision = lambda kind, data, session_id: events.append((kind, data))
    n = {"calls": 0}

    async def fake_stream(*args, **kwargs):
        n["calls"] += 1
        if n["calls"] == 1:
            yield StreamEvent(type="tool_start", tool_name="recall_recent", tool_id="t1", block_index=1)
            yield StreamEvent(type="tool_input_delta", text='{"hours": 24}', block_index=1)
            yield StreamEvent(type="block_stop", block_index=1)
            yield StreamEvent(type="done", stop_reason="tool_use")
        else:
            yield StreamEvent(type="text_delta", text="ok")
            yield StreamEvent(type="done", stop_reason="end_turn")

    runner._call_api_stream = MagicMock(side_effect=fake_stream)
    out = [e async for e in runner.stream_chat("s1", "what did we do")]

    assert calls == [("recall_deep", {"hours": 24})]
    assert store.inputs == [("recall_deep", {"hours": 24})]  # the durable row is the target's
    # tool_start carries the name the model used, tool_end the target (accepted)
    assert any(e.type == "tool_start" and e.tool_name == "recall_recent" for e in out)
    assert any(e.type == "tool_end" and e.tool_name == "recall_deep" for e in out)
    messages = runner._call_api_stream.call_args_list[1][0][1]
    content = _tool_result(messages)["content"]
    assert content.split("\n")[0] == RECENT_NOTE and content.endswith("recall_deep ran")
    (use,) = [b for m in messages if m["role"] == "assistant" for b in m["content"] if b.get("type") == "tool_use"]
    assert use["name"] == "recall_recent" and use["input"] == {"hours": 24}
    assert (
        "tool_alias_used",
        {"alias": "recall_recent", "target": "recall_deep", "context_kind": "interactive"},
    ) in events


@pytest.mark.asyncio
async def test_stream_chat_answers_a_retired_name_without_running_it():
    from test_streaming import _make_mock_cognitive, _make_mock_settings, _make_runner

    cognitive, _ = _make_mock_cognitive()
    runner = _make_runner(cognitive, _make_mock_settings())
    calls: list = []
    runner.set_dispatcher(_dispatcher(calls))
    runner._log_f026_decision = lambda kind, data, session_id: None
    n = {"calls": 0}

    async def fake_stream(*args, **kwargs):
        n["calls"] += 1
        if n["calls"] == 1:
            yield StreamEvent(type="tool_start", tool_name="recall_hubs", tool_id="t1", block_index=1)
            yield StreamEvent(type="tool_input_delta", text="{}", block_index=1)
            yield StreamEvent(type="block_stop", block_index=1)
            yield StreamEvent(type="done", stop_reason="tool_use")
        else:
            yield StreamEvent(type="text_delta", text="ok")
            yield StreamEvent(type="done", stop_reason="end_turn")

    runner._call_api_stream = MagicMock(side_effect=fake_stream)
    [e async for e in runner.stream_chat("s1", "hubs?")]
    assert calls == []
    result = _tool_result(runner._call_api_stream.call_args_list[1][0][1])
    assert result["content"] == HUBS_MESSAGE and result["is_error"] is True


def _approved(tool: str) -> ExecutionContext:
    proposal_id = uuid.uuid4()
    return ExecutionContext(
        kind="approved_action",
        session_id=f"proposal-{proposal_id}",
        proposal_id=proposal_id,
        declared_tools=(tool,),
        root_intention_id=uuid.uuid4(),
        intention_id=uuid.uuid4(),
    )


@pytest.mark.asyncio
async def test_an_approved_call_stored_under_an_old_name_runs_as_its_target():
    calls: list = []
    store = _InputStore()
    r, events = _runner(calls, store)
    call = {"decision_id": DECISION, "outcome": "superseded", "superseded_by": DECISION}
    out = await r.execute_single_call(_approved("resolve_decision"), "resolve_decision", dict(call))
    assert out.is_error is False and out.text.split("\n")[0] == RESOLVE_NOTE
    assert calls == [("resolve_decisions", {"resolutions": [call]})]
    assert store.inputs == [("resolve_decisions", {"resolutions": [call]})]
    assert [e[0] for e in events] == ["tool_alias_used"]


@pytest.mark.asyncio
async def test_an_approved_call_to_a_retired_name_runs_nothing():
    calls: list = []
    store = _InputStore()
    r, _events = _runner(calls, store)
    out = await r.execute_single_call(_approved("recall_hubs"), "recall_hubs", {})
    assert (out.text, out.is_error) == (HUBS_MESSAGE, True)
    assert calls == [] and store.events == []


@pytest.mark.asyncio
async def test_a_proposal_under_an_old_name_is_staged_as_its_target():
    from nous.handlers.continuation_runner import ArrivalState, make_propose_action_executor

    staged: list = []

    async def stage(tool, arguments, rationale):
        staged.append((tool, arguments))
        return uuid.uuid4()

    executor = make_propose_action_executor(
        ArrivalState(),
        ctx=ExecutionContext(
            kind="continuation",
            session_id="s",
            authority="internal_only",
            intention_id=uuid.uuid4(),
            root_intention_id=uuid.uuid4(),
            claim_token=uuid.uuid4(),
        ),
        dispatcher=_dispatcher(),
        stage=stage,
    )
    call = {"decision_id": DECISION, "outcome": "noise", "resolution_note": "tick"}
    text, is_error = await executor(tool="resolve_decision", arguments=call, rationale="close it")
    assert not is_error, text
    assert staged == [("resolve_decisions", {"resolutions": [call]})]
    text, is_error = await executor(tool="recall_hubs", arguments={}, rationale="r")
    assert is_error and HUBS_MESSAGE in text


def test_the_memory_tools_offer_three_fewer_names():
    """recall_recent, recall_hubs and resolve_decision left the offered set (12 memory tools became 9)."""
    from nous.api.tools import register_nous_tools

    d = ToolDispatcher()
    settings = MagicMock()
    settings.decision_resolution_enabled = True
    register_nous_tools(d, MagicMock(), MagicMock(), settings=settings)
    assert [t["name"] for t in d.tool_definitions()] == [
        "record_decision",
        "learn_fact",
        "recall_deep",
        "create_censor",
        "learn_skill",
        "get_procedure",
        "ingest_document",
        "resolve_decisions",
        "list_decisions",
    ]
    assert {old for old in CHANGED_TOOLS if d.alias_for(old) is not None} == set(CHANGED_TOOLS)


def test_with_resolution_off_the_resolve_alias_is_not_registered():
    """An alias is registered only next to its target (NOUS_DECISION_RESOLUTION_ENABLED off)."""
    from nous.api.tools import register_nous_tools

    d = ToolDispatcher()
    settings = MagicMock()
    settings.decision_resolution_enabled = False
    register_nous_tools(d, MagicMock(), MagicMock(), settings=settings)
    assert d.alias_for("resolve_decision") is None and d.alias_for("recall_recent") is not None
