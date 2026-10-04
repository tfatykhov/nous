"""The fix-node dispatcher and query expansion force their tool the same way the helper does.

Both send one request whose answer is a single tool call, to a model that is
a setting (``NOUS_DAG_FIX_LLM_MODEL``, ``NOUS_QUERY_EXPANSION_MODEL``). Like the
structured helper they go through ``call_with_tool_choice``: today's forced
request first, and on a model that rejects forcing, the tool asked for in the
system prompt. Each test records the requests a call would send: nothing here
reaches the network.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

import nous
import nous.handlers
from nous.config import Settings
from nous.dag.fix_executor import choose_action_llm
from nous.heart.query_expansion import QueryExpander

# How the httpx backend reports the API's rejection of a forced tool_choice.
_REJECTION = RuntimeError(
    'Anthropic API error (400): invalid_request_error - tool_choice: type "tool" and "any" are not supported '
    "for this model."
)


@pytest.fixture(autouse=True)
def _no_model_remembered(monkeypatch):
    monkeypatch.setattr(nous.handlers, "_REJECTS_FORCED_TOOL_CHOICE", set(), raising=False)


def _model(
    tool_use: dict[str, Any], payloads: list[dict[str, Any]], rejects_forcing: bool, stop_reason: str = "tool_use"
) -> MagicMock:
    llm = MagicMock()

    async def call(payload: dict[str, Any]) -> Any:
        payloads.append(payload)
        if rejects_forcing and "tool_choice" in payload:
            raise _REJECTION
        return SimpleNamespace(content=[tool_use], stop_reason=stop_reason)

    llm.call = call
    return llm


async def _dispatch(llm: Any, model: str) -> str:
    result = await choose_action_llm(
        parent_name="export_report",
        parent_instructions="Export the weekly failed-jobs report as CSV.",
        parent_error="TimeoutError: the export query exceeded 30 s",
        parent_result=None,
        fix_instructions=None,
        fix_actions=["skip", "abort"],
        llm_client=llm,
        model=model,
        timeout_seconds=5.0,
    )
    return result.action


_FIX = {"type": "tool_use", "id": "t", "name": "choose_fix_action", "input": {"action": "skip", "rationale": "ok"}}
_VARIANTS = ["staging deploy flake fix", "how the staging deploy was repaired"]
_EXPAND = {"type": "tool_use", "id": "t", "name": "expand_query", "input": {"alternative_queries": _VARIANTS}}
_ASK = "Respond only by calling the {} tool. Do not write any text before or after the call: "
_ASK += "work through any steps silently and put only the result in the call."


async def test_the_fix_dispatcher_forces_its_tool_where_the_model_allows_it():
    payloads: list[dict[str, Any]] = []

    assert await _dispatch(_model(_FIX, payloads, rejects_forcing=False), "claude-haiku-4-5-20251001") == "skip"

    (payload,) = payloads
    assert payload["tool_choice"] == {"type": "tool", "name": "choose_fix_action"}
    assert payload["system"] == ""


async def test_the_fix_dispatcher_asks_for_its_tool_where_the_model_rejects_forcing():
    payloads: list[dict[str, Any]] = []

    assert await _dispatch(_model(_FIX, payloads, rejects_forcing=True), "claude-sonnet-5-5") == "skip"

    forced, asked = payloads
    assert "tool_choice" in forced and "tool_choice" not in asked
    assert asked["system"] == _ASK.format("choose_fix_action")
    assert {k: v for k, v in asked.items() if k != "system"} == {
        k: v for k, v in forced.items() if k not in ("system", "tool_choice")
    }


async def test_query_expansion_forces_its_tool_where_the_model_allows_it():
    payloads: list[dict[str, Any]] = []
    llm = _model(_EXPAND, payloads, rejects_forcing=False)
    expander = QueryExpander(llm, Settings(_env_file=None), model="claude-haiku-4-5-20251001")

    assert await expander._call_haiku("how did we fix the flaky staging deploy") == _VARIANTS

    (payload,) = payloads
    assert payload["tool_choice"] == {"type": "tool", "name": "expand_query"}
    assert payload["system"].startswith("You rewrite search queries into semantic variants.")


async def test_query_expansion_asks_for_its_tool_where_the_model_rejects_forcing():
    payloads: list[dict[str, Any]] = []
    expander = QueryExpander(_model(_EXPAND, payloads, rejects_forcing=True), Settings(_env_file=None))

    assert await expander._call_haiku("how did we fix the flaky staging deploy") == _VARIANTS

    forced, asked = payloads
    assert "tool_choice" not in asked
    assert asked["system"] == f"{forced['system']}\n\n{_ASK.format('expand_query')}"


# Replies that are no whole answer, as (stop_reason, the reply's one block): cut off at max_tokens, refused,
# without a required key or without the tool call. A reply cut off at max_tokens can carry a partial call.
def _not_whole(
    tool: str, partial: dict[str, Any], whole: dict[str, Any], keyless: dict[str, Any]
) -> dict[str, tuple[str, dict[str, Any]]]:
    call = {"type": "tool_use", "id": "t", "name": tool}
    return {
        "cut off": ("max_tokens", {**call, "input": partial}),
        "cut off after the last field": ("max_tokens", {**call, "input": whole}),
        "refused": ("refusal", {**call, "input": whole}),
        "without a required key": ("tool_use", {**call, "input": keyless}),
        "without the call": ("end_turn", {"type": "text", "text": "skip"}),
    }


_FIX_NOT_WHOLE = _not_whole("choose_fix_action", {"action": "skip"}, _FIX["input"], {"action": "skip"})
_EXPAND_NOT_WHOLE = _not_whole("expand_query", {"alternative_queries": _VARIANTS[:1]}, _EXPAND["input"], {})


@pytest.mark.parametrize("rejects_forcing", [False, True], ids=["forced", "asked"])
@pytest.mark.parametrize("reply", list(_FIX_NOT_WHOLE))
async def test_the_fix_dispatcher_does_not_act_on_a_reply_that_is_no_whole_answer(reply, rejects_forcing):
    """The dispatcher raises, so the orchestrator takes its rule-based fallback, instead of acting on a partial call."""
    stop_reason, block = _FIX_NOT_WHOLE[reply]
    llm = _model(block, [], rejects_forcing, stop_reason)

    with pytest.raises(ValueError, match=f"no choose_fix_action call to use [(]stop_reason={stop_reason}, "):
        await _dispatch(llm, "claude-sonnet-5-5" if rejects_forcing else "claude-haiku-4-5-20251001")


@pytest.mark.parametrize("rejects_forcing", [False, True], ids=["forced", "asked"])
@pytest.mark.parametrize("reply", list(_EXPAND_NOT_WHOLE))
async def test_query_expansion_searches_the_bare_query_on_a_reply_that_is_no_whole_answer(reply, rejects_forcing):
    """Query expansion falls back to the query alone instead of using a partial call's variants."""
    stop_reason, block = _EXPAND_NOT_WHOLE[reply]
    query = "how did we fix the flaky staging deploy"
    llm = _model(block, [], rejects_forcing, stop_reason)
    expander = QueryExpander(llm, Settings(_env_file=None, query_expansion_enabled=True))

    assert await expander.expand(query, "agent-y") == [query]


@pytest.mark.parametrize(
    ("variants", "kept"),
    [("staging deploy flake fix", []), ([7, "staging deploy flake fix"], ["staging deploy flake fix"])],
)
async def test_query_expansion_keeps_only_a_list_of_strings(variants, kept):
    block = {**_EXPAND, "input": {"alternative_queries": variants}}
    expander = QueryExpander(_model(block, [], rejects_forcing=False), Settings(_env_file=None))

    assert await expander._call_haiku("how did we fix the flaky staging deploy") == kept


def test_every_forced_tool_choice_goes_through_call_with_tool_choice():
    """A module that writes a forced tool_choice sends it through call_with_tool_choice. The runner's force
    on a subtask's penultimate turn is the known exception: it is sent only when thinking is off, and
    NOUS_SUBTASK_FORCE_TOOL_ON_PENULTIMATE turns it off for a deployment that runs a 5.5 model with thinking
    off."""
    root = Path(nous.__file__).parent
    forced = re.compile(r"""["']type["']\s*:\s*["'](tool|any)["']""")
    unrouted = sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if forced.search(text := path.read_text("utf-8")) and "call_with_tool_choice(" not in text
    )
    assert unrouted == ["api/runner.py"]
