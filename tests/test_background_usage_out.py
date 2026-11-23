"""F118: ``call_with_tool_choice`` and ``call_background_llm_structured`` take ``usage_out``.

Without it (the default, and every existing call site) the payloads, the client calls and the results are what they
were. With a dict, ``requests`` counts each request sent (before it is sent, so one that fails counts too), and each
response's ``input_tokens``, ``output_tokens``, ``cache_read_input_tokens`` and ``cache_creation_input_tokens`` are
added into it, on the forced request and on the request that asks for the tool in the prompt, also when the reply is
not usable. ``outside_judge`` is a purpose.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import nous
import nous.handlers
from nous.handlers import UnusableToolReply, call_background_llm_structured, call_with_tool_choice
from nous.observability.model_calls import PURPOSES, CallContext

_REJECTION = RuntimeError(
    'Anthropic API error (400): invalid_request_error - tool_choice: type "tool" and "any" are not supported '
    "for this model."
)
_SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}


@pytest.fixture(autouse=True)
def _no_model_remembered(monkeypatch):
    monkeypatch.setattr(nous.handlers, "_REJECTS_FORCED_TOOL_CHOICE", set())


def _reply(payload: dict[str, Any], usage: dict[str, int] | None, *, usable: bool = True) -> SimpleNamespace:
    name = payload["tools"][0]["name"]
    block = {"type": "tool_use", "id": "t1", "name": name, "input": {"answer": "yes"} if usable else {}}
    reply = SimpleNamespace(content=[block], stop_reason="tool_use")
    if usage is not None:
        reply.usage = usage
    return reply


class _Client:
    supports_call_context = True

    def __init__(
        self,
        *,
        rejects_forcing: bool = False,
        usage: dict[str, int] | None = None,
        usable: bool = True,
    ) -> None:
        self.rejects_forcing = rejects_forcing
        self.usage = usage
        self.usable = usable
        self.calls: list[tuple[dict[str, Any], CallContext | None]] = []

    async def call(self, payload: dict[str, Any], call_context: CallContext | None = None) -> Any:
        self.calls.append((payload, call_context))
        if self.rejects_forcing and "tool_choice" in payload:
            raise _REJECTION
        return _reply(payload, self.usage, usable=self.usable)


class _UsagelessClient:
    """A test double whose replies carry no ``usage`` attribute at all."""

    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    async def call(self, payload: dict[str, Any]) -> Any:
        self.payloads.append(payload)
        return _reply(payload, None)


def _forced_payload() -> dict[str, Any]:
    return {
        "model": "claude-test",
        "max_tokens": 50,
        "system": [{"type": "text", "text": "sys"}],
        "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        "tools": [{"name": "answer_tool", "description": "d", "input_schema": _SCHEMA}],
        "tool_choice": {"type": "tool", "name": "answer_tool"},
    }


async def _structured(client: Any, **kwargs: Any) -> Any:
    return await call_background_llm_structured(
        client, "claude-test", "sys", "user", "answer_tool", "d", _SCHEMA, purpose="critic", **kwargs
    )


def test_outside_judge_is_a_purpose():
    assert "outside_judge" in PURPOSES


# ---- without usage_out: what it was --------------------------------------------------------------------------------


async def test_without_usage_out_the_requests_and_results_are_unchanged():
    plain, given_none = _Client(usage={"input_tokens": 9, "output_tokens": 2}), _Client(usage={"input_tokens": 9})
    assert await call_with_tool_choice(plain, _forced_payload(), purpose="critic") == {"answer": "yes"}
    assert await call_with_tool_choice(given_none, _forced_payload(), purpose="critic", usage_out=None) == {
        "answer": "yes"
    }
    assert await _structured(plain) == {"answer": "yes"}
    assert await _structured(given_none, usage_out=None) == {"answer": "yes"}
    assert [p for p, _ in plain.calls] == [p for p, _ in given_none.calls]
    assert [(c.purpose, c.is_fallback) for _, c in plain.calls] == [("critic", False)] * 2


async def test_without_usage_out_the_structured_helper_calls_the_tool_choice_helper_as_before(monkeypatch):
    """A test double of ``call_with_tool_choice`` with the signature it had keeps working: the keyword is passed on
    only when there is a dict to fill."""
    seen: list[dict[str, Any]] = []

    async def old_signature(client: Any, payload: dict[str, Any], *, purpose: str = "unlabelled") -> dict[str, Any]:
        seen.append({"purpose": purpose})
        return {"answer": "old"}

    monkeypatch.setattr(nous.handlers, "call_with_tool_choice", old_signature)
    assert await _structured(_Client()) == {"answer": "old"}
    assert seen == [{"purpose": "critic"}]


async def test_with_usage_out_the_structured_helper_passes_it_on(monkeypatch):
    seen: list[Any] = []

    async def spy(client: Any, payload: dict[str, Any], *, purpose: str = "unlabelled", usage_out=None) -> Any:
        seen.append(usage_out)
        return {"answer": "spied"}

    monkeypatch.setattr(nous.handlers, "call_with_tool_choice", spy)
    usage: dict[str, int] = {}
    assert await _structured(_Client(), usage_out=usage) == {"answer": "spied"}
    assert seen == [usage] and seen[0] is usage


# ---- with usage_out: the tokens --------------------------------------------------------------------------------------


async def test_the_forced_request_fills_the_dict_with_every_kind_of_token_and_one_request():
    usage: dict[str, int] = {}
    client = _Client(
        usage={
            "input_tokens": 120,
            "output_tokens": 30,
            "cache_read_input_tokens": 500,
            "cache_creation_input_tokens": 900,
            "service_tier": 1,
        }
    )
    assert await call_with_tool_choice(client, _forced_payload(), usage_out=usage) == {"answer": "yes"}
    assert usage == {
        "requests": 1,
        "input_tokens": 120,
        "output_tokens": 30,
        "cache_read_input_tokens": 500,
        "cache_creation_input_tokens": 900,
    }


async def test_the_fallback_path_counts_both_requests():
    usage: dict[str, int] = {}
    client = _Client(rejects_forcing=True, usage={"input_tokens": 70, "output_tokens": 11})
    assert await _structured(client, usage_out=usage) == {"answer": "yes"}
    assert len(client.calls) == 2  # the forced request was refused, the asking one answered
    assert usage == {"requests": 2, "input_tokens": 70, "output_tokens": 11}


async def test_a_remembered_model_sends_one_request():
    nous.handlers._REJECTS_FORCED_TOOL_CHOICE.add("claude-test")
    usage: dict[str, int] = {}
    client = _Client(rejects_forcing=True, usage={"input_tokens": 70, "output_tokens": 11})
    assert await _structured(client, usage_out=usage) == {"answer": "yes"}
    assert usage == {"requests": 1, "input_tokens": 70, "output_tokens": 11}


async def test_every_request_of_a_call_is_summed_into_the_same_dict():
    usage: dict[str, int] = {"input_tokens": 5, "output_tokens": 1}
    client = _Client(usage={"input_tokens": 70, "output_tokens": 11})
    await call_with_tool_choice(client, _forced_payload(), usage_out=usage)
    await call_with_tool_choice(client, _forced_payload(), usage_out=usage)
    assert usage == {"requests": 2, "input_tokens": 145, "output_tokens": 23}


async def test_an_unusable_reply_still_counts_its_tokens_and_its_request():
    usage: dict[str, int] = {}
    client = _Client(usage={"input_tokens": 40, "output_tokens": 3}, usable=False)
    with pytest.raises(UnusableToolReply):
        await call_with_tool_choice(client, _forced_payload(), usage_out=usage)
    assert usage == {"requests": 1, "input_tokens": 40, "output_tokens": 3}
    usage.clear()
    assert await _structured(client, usage_out=usage) is None
    assert usage == {"requests": 1, "input_tokens": 40, "output_tokens": 3}


async def test_a_request_that_fails_is_counted():
    class Failing(_Client):
        async def call(self, payload: dict[str, Any], call_context: CallContext | None = None) -> Any:
            self.calls.append((payload, call_context))
            raise ConnectionError("no answer")

    usage: dict[str, int] = {}
    assert await _structured(Failing(), usage_out=usage) is None
    assert usage == {"requests": 1}


async def test_a_reply_without_usage_counts_only_its_request():
    usage: dict[str, int] = {}
    assert await _structured(_UsagelessClient(), usage_out=usage) == {"answer": "yes"}
    assert usage == {"requests": 1}
    usage.clear()
    client = _Client(usage={"cache_read_input_tokens": 3})  # no input or output count
    assert await _structured(client, usage_out=usage) == {"answer": "yes"}
    assert usage == {"requests": 1, "cache_read_input_tokens": 3}


async def test_a_usage_value_that_is_not_a_number_is_not_counted():
    usage: dict[str, int] = {}
    client = _Client(usage={"input_tokens": "many", "output_tokens": True})
    assert await _structured(client, usage_out=usage) == {"answer": "yes"}
    assert usage == {"requests": 1}


# ---- a bound on the requests one call may send (max_requests) ---------------------------------------------------


async def test_without_a_bound_a_model_that_rejects_forcing_gets_both_requests():
    client = _Client(rejects_forcing=True)
    assert await call_with_tool_choice(client, _forced_payload(), max_requests=None) == {"answer": "yes"}
    assert len(client.calls) == 2


async def test_a_bound_of_one_sends_no_fallback_request():
    from nous.handlers import RequestBoundReached

    usage: dict[str, int] = {}
    client = _Client(rejects_forcing=True)
    with pytest.raises(RequestBoundReached):
        await call_with_tool_choice(client, _forced_payload(), usage_out=usage, max_requests=1)
    assert len(client.calls) == 1 and usage == {"requests": 1}
    # The model is remembered as rejecting forcing: the next call asks in the prompt, one request within a bound of 1.
    client.calls.clear()
    assert await call_with_tool_choice(client, _forced_payload(), max_requests=1) == {"answer": "yes"}
    assert len(client.calls) == 1 and "tool_choice" not in client.calls[0][0]


async def test_the_structured_helper_passes_the_bound_on_and_answers_none_when_it_is_reached():
    usage: dict[str, int] = {}
    client = _Client(rejects_forcing=True)
    assert await _structured(client, usage_out=usage, max_requests=1) is None
    assert len(client.calls) == 1 and usage == {"requests": 1}


async def test_a_bound_of_two_allows_the_fallback_request():
    client = _Client(rejects_forcing=True)
    assert await _structured(client, max_requests=2) == {"answer": "yes"}
    assert len(client.calls) == 2


async def test_a_bound_of_zero_sends_nothing():
    from nous.handlers import RequestBoundReached

    client = _Client()
    with pytest.raises(RequestBoundReached):
        await call_with_tool_choice(client, _forced_payload(), max_requests=0)
    assert client.calls == []


async def test_without_a_bound_the_structured_helper_calls_the_tool_choice_helper_without_the_keyword(monkeypatch):
    seen: list[dict[str, Any]] = []

    async def today_signature(
        client: Any, payload: dict[str, Any], *, purpose: str = "unlabelled", usage_out=None
    ) -> dict[str, Any]:
        seen.append({"purpose": purpose, "usage_out": usage_out})
        return {"answer": "today"}

    monkeypatch.setattr(nous.handlers, "call_with_tool_choice", today_signature)
    assert await _structured(_Client(), usage_out={}) == {"answer": "today"}
    assert seen == [{"purpose": "critic", "usage_out": {}}]
