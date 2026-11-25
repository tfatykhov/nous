"""F120 tool loading, On: the pure parts (nous/api/tool_loading.py) and the SDK client's side. No database.

The window a turn opens (which tools are deferred, in what order, the loader last with its list), the conditions it
opens under, the loader's answers (references only; the same words for a name that does not exist and one the turn may
not use; what was not loaded after the results; nothing loadable: text only), the answer to a call of a tool not loaded
yet (never run, text only), the free loads of a turn, the fallback after a refused request or a fault, the record of a
call, the references a request that does not send their tool must not carry, and the SDK client's error status.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx
import pytest

from nous.api import tool_loading
from nous.api.anthropic_client import HttpxAnthropicClient, SdkAnthropicClient
from nous.api.runner import _http_status
from nous.api.tool_loading import (
    DEFERRING_MODELS,
    FREE_LOADS_PER_TURN,
    KINDS,
    LOADER_ALL_ATTACHED,
    LOADER_BAD_INPUT,
    LOADER_MAX_NAMES,
    LOADER_NAME,
    LOADER_UNREADABLE,
    ToolLoadingConfig,
    call_record,
    carries_reference,
    holds_reference,
    index_text,
    note_block,
    open_window,
    parse_tool_loading,
    references_outside,
    sent_in_full,
    tokens_est,
    why_not,
)
from nous.config import Settings

ON_ALL = ToolLoadingConfig(mode="on", kinds=frozenset(KINDS))
MODEL = "claude-opus-5-5"


def _tool(name: str, description: str = "Does a thing.") -> dict[str, Any]:
    return {"name": name, "description": description, "input_schema": {"type": "object", "properties": {}}}


# bash and db_query are kept (pinned, and in the default core set); the rest are deferred
TOOLS = [
    _tool("bash"),
    _tool("send_email", "Send an email. It goes out at once."),
    _tool("db_query"),
    _tool("dag_create"),
    _tool("mcp__ha__HassTurnOn", "[ha] Turn on a device."),
]
DEFERRED = ["send_email", "dag_create", "mcp__ha__HassTurnOn"]


def _window(tools: list[dict[str, Any]] | None = None, config: ToolLoadingConfig = ON_ALL) -> tool_loading.ToolWindow:
    window = open_window(config, "chat", tools or TOOLS)
    window.request_tools()
    return window


# ---- the window -----------------------------------------------------------------------------------------------------


def test_the_window_defers_exactly_the_tools_outside_the_kept_set_in_their_order_and_the_loader_is_last():
    before = json.dumps(TOOLS)
    window = open_window(ON_ALL, "chat", TOOLS)
    sent = window.tools
    assert [t["name"] for t in sent] == [t["name"] for t in TOOLS] + [LOADER_NAME]
    for original, wired in zip(TOOLS, sent, strict=False):
        if original["name"] in DEFERRED:
            assert wired == {**original, "defer_loading": True}
        else:
            assert wired is original  # kept in full, as offered
    loader = sent[-1]
    assert "defer_loading" not in loader and "cache_control" not in loader
    assert loader["description"] == index_text([t for t in TOOLS if t["name"] in DEFERRED])
    assert not any("cache_control" in t for t in sent)
    assert sum(1 for t in sent if not t.get("defer_loading")) >= 1
    assert json.dumps(TOOLS) == before  # the offered definitions are never changed
    assert window.deferred == frozenset(DEFERRED)
    assert window.in_full == {"bash", "db_query", LOADER_NAME}
    # the fallback is the same tools, every one in full
    assert [t["name"] for t in window.fallback] == [t["name"] for t in sent]
    assert not any(t.get("defer_loading") for t in window.fallback)


def test_the_list_of_names_is_the_same_whatever_the_order_the_tools_come_in():
    one = open_window(ON_ALL, "chat", TOOLS).tools[-1]
    two = open_window(ON_ALL, "chat", list(reversed(TOOLS))).tools[-1]
    assert one == two  # the same allowed set gives the same loader, turn after turn


def test_a_pinned_or_forced_tool_and_the_core_set_are_never_deferred():
    tools = [*TOOLS, _tool("submit_final_report"), _tool("compose_surface")]
    window = open_window(ToolLoadingConfig(mode="on", kinds=frozenset(KINDS), core=()), "chat", tools)
    assert {"submit_final_report", "compose_surface", "bash"} <= window.in_full
    assert "db_query" in window.deferred  # the core set chosen here is empty, and db_query is not pinned


@pytest.mark.parametrize(
    ("client", "model", "tools", "why"),
    [
        (False, MODEL, TOOLS, "client"),
        (True, "claude-sonnet-4-6", TOOLS, "model"),
        (True, "claude-opus-5", TOOLS, "model"),  # an id is listed only once the smoke script passed on it
        (True, None, TOOLS, "model"),
        (True, MODEL, [*TOOLS, _tool(LOADER_NAME)], "name"),  # two tools of one name would be refused
        (True, MODEL, [_tool("bash"), _tool("db_query")], "nothing"),
        (True, MODEL, [], "nothing"),
        (True, MODEL, TOOLS, None),
    ],
)
def test_on_defers_only_on_the_tested_client_and_models_and_when_something_is_left_out(client, model, tools, why):
    assert why_not(ON_ALL, tools, deferring_client=client, model=model) == why


def test_the_tested_models_are_the_three_the_smoke_run_passed():
    assert DEFERRING_MODELS == {"claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-2"}
    for model in DEFERRING_MODELS:
        assert why_not(ON_ALL, TOOLS, deferring_client=True, model=model) is None


def test_only_the_sdk_client_says_it_can_defer():
    assert SdkAnthropicClient.supports_deferred_tools is True
    assert getattr(HttpxAnthropicClient, "supports_deferred_tools", False) is False


# ---- the loader -----------------------------------------------------------------------------------------------------


def test_allowed_names_load_as_references_only():
    window = _window()
    answer = window.answer(LOADER_NAME, {"names": ["send_email", "mcp__ha__HassTurnOn", "send_email"]})
    assert answer.content == [
        {"type": "tool_reference", "tool_name": "send_email"},
        {"type": "tool_reference", "tool_name": "mcp__ha__HassTurnOn"},
    ]
    assert (answer.is_error, answer.outcome, answer.note) == (False, "loaded", None)
    assert answer.result("t1") == {
        "type": "tool_result",
        "tool_use_id": "t1",
        "content": answer.content,
        "is_error": False,
    }
    assert window.loaded == ["send_email", "mcp__ha__HassTurnOn"]
    sent = {t["name"] for t in window.tools}
    assert {block["tool_name"] for block in answer.content} <= sent  # every reference names a tool of the array


def test_a_name_that_does_not_exist_and_one_the_turn_may_not_use_get_the_same_words():
    # send_file is a real tool, but not one this turn was offered: the window cannot tell it from a made-up name
    window = _window()
    unknown = window.answer(LOADER_NAME, {"names": ["no_such_tool"]})
    not_allowed = window.answer(LOADER_NAME, {"names": ["send_file"]})
    assert unknown.content == "Nothing was loaded. Not available: no_such_tool."
    assert not_allowed.content == "Nothing was loaded. Not available: send_file."
    assert (unknown.is_error, unknown.outcome) == (False, "nothing_loaded")
    assert isinstance(unknown.content, str)  # text only: no reference, so nothing outside the array


def test_a_mix_loads_the_allowed_ones_and_says_the_rest_after_the_results():
    window = _window()
    window.answer(LOADER_NAME, {"names": ["dag_create"]})
    answer = window.answer(LOADER_NAME, {"names": ["send_email", "bash", "nope", "dag_create"]})
    assert answer.content == [{"type": "tool_reference", "tool_name": "send_email"}]  # references only
    assert answer.note == "load_tools: Already in your tool list: bash, dag_create. Not available: nope."
    assert note_block([answer.note]) == [{"type": "text", "text": answer.note}]
    assert note_block([]) == []


def test_nothing_loadable_is_text_only():
    window = _window()
    answer = window.answer(LOADER_NAME, {"names": ["bash", "loader_of_nothing"]})
    assert answer.content == "Nothing was loaded. Already in your tool list: bash. Not available: loader_of_nothing."
    assert (answer.outcome, answer.is_error, answer.note) == ("nothing_loaded", False, None)


@pytest.mark.parametrize("tool_input", [{}, {"names": []}, {"names": "send_email"}, {"names": [1]}, None, "x"])
def test_a_loader_call_without_a_list_of_names_loads_nothing(tool_input):
    window = _window()
    answer = window.answer(LOADER_NAME, tool_input)
    assert (answer.content, answer.outcome) == (LOADER_BAD_INPUT, "nothing_loaded")
    assert window.loaded == []


def test_only_the_first_names_of_a_call_are_read_and_long_names_are_cut():
    window = _window()
    names = ["send_email", *[f"made_up_{i}" for i in range(LOADER_MAX_NAMES)]]
    answer = window.answer(LOADER_NAME, {"names": names})
    assert answer.note.endswith(f"Only the first {LOADER_MAX_NAMES} names of a call are read.")
    assert f"made_up_{LOADER_MAX_NAMES - 2}" in answer.note and f"made_up_{LOADER_MAX_NAMES - 1}" not in answer.note
    long = window.answer(LOADER_NAME, {"names": ["x" * 500]})
    assert long.content == f"Nothing was loaded. Not available: {'x' * 64}."


def test_an_old_name_loads_the_tool_it_became():
    window = _window()
    answer = window.answer(
        LOADER_NAME, {"names": ["email_send"]}, lambda name: {"email_send": "send_email"}.get(name, name)
    )
    assert answer.content == [{"type": "tool_reference", "tool_name": "send_email"}]


SERVERS = [
    _tool("bash"),
    *(_tool(f"mcp__big__t{i:02d}") for i in range(45)),
    _tool("mcp__ha__t01"),
    _tool("mcp__ha__lamp"),
]


def test_every_deferred_tool_of_a_large_server_is_in_the_loaders_list_by_the_name_it_takes():
    description = _window(SERVERS).tools[-1]["description"]
    for i in range(45):
        assert f"mcp__big__t{i:02d}" in description
    assert "mcp__ha__lamp" in description and " more" not in description
    assert "load one by its full name" in description and "call one" not in description


def test_another_servers_tool_loads_by_its_full_name_and_by_its_short_name_when_only_one_has_it():
    window = _window(SERVERS)
    full = window.answer(LOADER_NAME, {"names": ["mcp__big__t44"]})
    short = window.answer(LOADER_NAME, {"names": ["lamp"]})
    assert full.content == [{"type": "tool_reference", "tool_name": "mcp__big__t44"}]
    assert short.content == [{"type": "tool_reference", "tool_name": "mcp__ha__lamp"}]
    again = window.answer(LOADER_NAME, {"names": ["lamp"]})
    assert again.content == "Nothing was loaded. Already in your tool list: lamp."


def test_a_short_name_several_tools_have_says_to_use_the_full_name():
    window = _window(SERVERS)
    answer = window.answer(LOADER_NAME, {"names": ["t01", "t02"]})
    assert answer.content == [{"type": "tool_reference", "tool_name": "mcp__big__t02"}]
    assert answer.note == "load_tools: t01 names 2 tools: load one by its full name, mcp__big__t01 or mcp__ha__t01."
    alone = window.answer(LOADER_NAME, {"names": ["t01"]})
    assert (
        alone.content
        == "Nothing was loaded. t01 names 2 tools: load one by its full name, mcp__big__t01 or mcp__ha__t01."
    )
    # a short name of a tool this turn may not use is not available, like any other name
    assert (
        window.answer(LOADER_NAME, {"names": ["HassTurnOn"]}).content
        == "Nothing was loaded. Not available: HassTurnOn."
    )


def test_a_loader_call_whose_input_could_not_be_read_is_a_text_error_that_says_how_to_call_it():
    window = _window()
    answer = window.unreadable()
    assert answer.content == LOADER_UNREADABLE and '{"names": ["tool_name"]}' in LOADER_UNREADABLE
    assert (answer.is_error, answer.outcome, answer.counted) == (True, "nothing_loaded", False)
    assert window.loads == 1  # counted under the loader's free calls


# ---- a call of a tool not loaded yet --------------------------------------------------------------------------------


def test_a_deferred_tool_called_before_it_was_loaded_is_answered_with_text_and_never_run():
    window = _window()
    answer = window.answer("send_email", {"to": "x"})
    assert answer.content == (
        'send_email is not loaded yet, so this call did not run. Call load_tools with ["send_email"], then call '
        "send_email again."
    )
    assert (answer.is_error, answer.outcome) == (True, "not_loaded")  # an error to the model, text only
    assert "send_email" not in window.loaded  # a text answer loads nothing
    assert window.answer("bash", {}) is None  # a kept tool runs as today


def test_a_tool_loaded_in_the_same_step_as_its_call_is_called_again_and_one_loaded_before_runs():
    window = _window()
    window.answer(LOADER_NAME, {"names": ["send_email"]})  # the same assistant message
    same_step = window.answer("send_email", {})
    assert same_step.content == (
        "send_email was loaded in this same step, after this call was written, so it did not run. Call it again."
    )
    window.request_tools()  # the next request carries the reference
    assert window.answer("send_email", {}) is None


def test_the_first_loads_of_a_turn_are_free_and_the_later_ones_count():
    window = _window()
    counted = [window.answer(LOADER_NAME, {"names": ["nope"]}).counted for _ in range(FREE_LOADS_PER_TURN)]
    counted.append(window.answer("send_email", {}).counted)
    counted.append(window.answer(LOADER_NAME, {"names": ["send_email"]}).counted)
    assert FREE_LOADS_PER_TURN == 8
    assert counted == [False] * 8 + [True, True]


# ---- the fallback ---------------------------------------------------------------------------------------------------


def test_a_refused_request_is_sent_once_more_in_full_and_the_window_stays_broken():
    window = _window()
    assert window.refused(500) is False and window.refused(None) is False and not window.broken
    assert window.refused(400) is True
    assert window.fault == "refused" and window.request_tools() is window.fallback
    assert window.refused(400) is False  # once a turn
    window.fail()
    assert window.fault == "refused"  # the first fallback is the one recorded


def test_after_a_fallback_nothing_is_refused_and_the_loader_says_every_tool_is_there():
    window = _window()
    window.fail()
    assert window.fault == "fault"
    window.request_tools()
    assert window.answer("send_email", {}) is None  # its definition was in that request
    answer = window.answer(LOADER_NAME, {"names": ["send_email"]})
    assert (answer.content, answer.outcome) == (LOADER_ALL_ATTACHED, "nothing_loaded")
    assert window.fault_answer("bash") is None
    assert window.fault_answer(LOADER_NAME).content == LOADER_ALL_ATTACHED
    assert window.fault_answer("dag_create").is_error is True


# ---- the record -----------------------------------------------------------------------------------------------------


def test_the_record_of_a_call_that_deferred_and_of_one_after_a_fallback():
    window = _window()
    window.answer(LOADER_NAME, {"names": ["send_email"]})
    sent = window.request_tools()
    deferred = [t for t in TOOLS if t["name"] in DEFERRED]
    assert window.record(sent) == {
        "mode": "on",
        "kind": "chat",
        "sent": 6,
        "applied": True,
        "why": None,
        "fallback": None,
        "full": 2,
        "deferred": 3,
        "deferred_tokens_est": sum(tokens_est(t) for t in deferred),
        "index_tokens_est": tokens_est(window.tools[-1]),
        "loaded": 1,
    }
    window.refused(400)
    after = window.record(window.request_tools())
    assert (after["applied"], after["fallback"], after["deferred"], after["full"]) == (False, "refused", 0, 5)
    assert after["index_tokens_est"] == tokens_est(window.tools[-1])  # the loader is still sent


def test_a_call_in_on_outside_a_window_records_why_it_was_sent_in_full():
    record = call_record(ON_ALL, "chat", TOOLS, "model")
    assert record["mode"] == "on" and record["applied"] is False and record["why"] == "model"
    assert (record["full"], record["deferred"], record["deferred_tokens_est"], record["index_tokens_est"]) == (
        5,
        0,
        0,
        0,
    )
    assert call_record(ON_ALL, "chat", TOOLS)["why"] == "turn"
    assert call_record(ToolLoadingConfig(mode="on", kinds=frozenset({"subtask"})), "chat", TOOLS, "model") is None
    assert call_record(ON_ALL, None, TOOLS, "model") is None


def test_on_is_a_stored_mode():
    config, problem = parse_tool_loading(ON_ALL.to_json())
    assert problem is None and config.mode == "on" and config.kinds == frozenset(KINDS)


# ---- what a request carries -----------------------------------------------------------------------------------------


def _loaded(*names: str, tool_use_id: str = "t1") -> dict[str, Any]:
    refs = [{"type": "tool_reference", "tool_name": name} for name in names]
    return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": refs}]}


def test_a_request_without_a_referenced_tool_carries_words_instead_of_the_reference():
    messages = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": []},
        _loaded("send_email", "dag_create"),
    ]
    before = json.dumps(messages)
    stripped = references_outside(messages, None)
    assert stripped[2]["content"][0]["content"] == [{"type": "text", "text": "Loaded: send_email, dag_create."}]
    assert stripped[2]["content"][0]["tool_use_id"] == "t1"
    assert stripped[0] is messages[0]
    assert json.dumps(messages) == before  # the turn's own messages are never changed
    # the window's array, deferred or in full after a fallback, holds every referenced tool: nothing changes
    window = _window()
    assert references_outside(messages, window.tools) is messages
    assert references_outside(messages, window.fallback) is messages
    # a list that lacks one of them: the whole result becomes words (a reference beside text is refused)
    partial = references_outside(messages, [_tool("send_email")])
    assert partial[2]["content"][0]["content"] == [{"type": "text", "text": "Loaded: send_email, dag_create."}]
    plain = [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "text"}]}]
    assert references_outside(plain, None) is plain


def test_the_tools_sent_in_full_and_a_result_that_loaded():
    window = _window()
    assert [t["name"] for t in sent_in_full(window.tools)] == ["bash", "db_query", LOADER_NAME]
    assert sent_in_full(TOOLS) is TOOLS and sent_in_full(None) is None
    assert holds_reference(_loaded("send_email")["content"][0])
    assert not holds_reference({"type": "tool_result", "content": "x"}) and not holds_reference({"type": "text"})
    assert carries_reference([{"role": "user", "content": "go"}, _loaded("send_email")])
    assert not carries_reference([{"role": "user", "content": "go"}])


def test_after_a_fallback_a_request_carries_no_reference():
    messages = [{"role": "user", "content": "go"}, {"role": "assistant", "content": []}, _loaded("send_email")]
    window = _window()
    assert window.request_messages(messages) is messages  # deferring: the references stay
    window.refused(400)
    sent = window.request_messages(messages)
    assert "tool_reference" not in json.dumps(sent) and "Loaded: send_email." in json.dumps(sent)
    assert "tool_reference" in json.dumps(messages)  # the turn's own messages are never changed


# ---- the SDK client's error status ----------------------------------------------------------------------------------


def _refusing_client() -> SdkAnthropicClient:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    refusal = anthropic.BadRequestError(
        "Error code: 400", response=httpx.Response(400, request=request), body={"type": "error"}
    )

    async def create(**_kwargs: Any) -> Any:
        raise refusal

    class _Opening:  # messages.stream(...) as the aggregated path opens it
        async def __aenter__(self) -> Any:
            raise refusal

        async def __aexit__(self, *exc: Any) -> bool:
            return False

    client = SdkAnthropicClient(Settings(_env_file=None, ANTHROPIC_API_KEY="test-key"))
    client._client = SimpleNamespace(messages=SimpleNamespace(create=create, stream=lambda **_kwargs: _Opening()))
    return client


PAYLOAD = {"model": MODEL, "max_tokens": 10, "system": [], "messages": [{"role": "user", "content": "hi"}]}


async def test_the_sdk_clients_refusal_carries_its_status_on_every_path():
    client = _refusing_client()
    with pytest.raises(RuntimeError) as raised:
        await client.call(dict(PAYLOAD))
    assert _http_status(raised.value) == 400
    with pytest.raises(RuntimeError) as aggregated:  # the background path
        await client.call_streaming_aggregated(dict(PAYLOAD))
    assert _http_status(aggregated.value) == 400
    events = [event async for event in client.stream(dict(PAYLOAD))]
    assert [(event.type, event.status) for event in events] == [("error", 400)]
    assert _http_status(RuntimeError("no answer")) is None
