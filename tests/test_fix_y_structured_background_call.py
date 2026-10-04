"""A structured call forces its tool where the model allows it, and asks for it where it does not.

Claude Sonnet 5.5, Claude Opus 5.5 and Claude Fable 5.1 answer a forced
``tool_choice`` with a 400. The first request for a model is exactly
today's forced request; on that one rejection the model is remembered and
the request is sent again without ``tool_choice``, with an instruction to
answer by calling the tool. Every test here runs the real backends over a
stubbed transport: nothing reaches the network.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest

import nous.handlers
from nous.api import anthropic_client
from nous.config import Settings
from nous.handlers import call_background_llm_structured

_PREAMBLE = "You are Claude Code, Anthropic's official CLI for Claude."
_CACHED = {"type": "ephemeral"}
_SCHEMA = {"type": "object", "properties": {"verdict": {"type": "string"}}, "required": ["verdict"]}
_TOOL_USE = {"type": "tool_use", "id": "toolu_1", "name": "emit_verdict", "input": {"verdict": "ok"}}
_REJECTION = {
    "type": "error",
    "error": {
        "type": "invalid_request_error",
        "message": 'tool_choice: type "tool" and "any" are not supported for this model.',
    },
}
_INSTRUCTION = (
    "Respond only by calling the emit_verdict tool. Do not write any text before or after the call: "
    "work through any steps silently and put only the result in the call."
)
_OAUTH = {"ANTHROPIC_AUTH_TOKEN": "sk-ant-oat-test-token"}
_API_KEY = {"ANTHROPIC_API_KEY": "test-key"}


def _todays_request(model: str) -> dict[str, Any]:
    return {
        "model": model,
        "max_tokens": 1500,
        "system": [
            {"type": "text", "text": _PREAMBLE, "cache_control": _CACHED},
            {"type": "text", "text": "You classify a fact.", "cache_control": _CACHED},
        ],
        "messages": [{"role": "user", "content": [{"type": "text", "text": "this call's own input"}]}],
        "tools": [{"name": "emit_verdict", "description": "Emit the verdict.", "input_schema": _SCHEMA}],
        "tool_choice": {"type": "tool", "name": "emit_verdict"},
    }


@pytest.fixture(autouse=True)
def _no_model_remembered(monkeypatch):
    monkeypatch.setattr(nous.handlers, "_REJECTS_FORCED_TOOL_CHOICE", set(), raising=False)


def _serve(monkeypatch, reply) -> list[dict[str, Any]]:
    """Route both backends' HTTP to ``reply(body) -> (status, json)``; return the request bodies."""
    bodies: list[dict[str, Any]] = []

    def answer(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        status, payload = reply(body)
        return httpx.Response(status, json=payload)

    monkeypatch.setattr(
        anthropic_client, "_build_transport_with_env_proxies", lambda **_: (httpx.MockTransport(answer), {})
    )
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    return bodies


def _message(content: list[dict[str, Any]], stop_reason: str = "tool_use") -> dict[str, Any]:
    message = {"id": "msg_1", "type": "message", "role": "assistant", "model": "m"}
    message |= {"content": content, "stop_reason": stop_reason, "stop_sequence": None}
    return message | {"usage": {"input_tokens": 1, "output_tokens": 1}}


def _rejects_forcing(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    return (400, _REJECTION) if "tool_choice" in body else (200, _message([_TOOL_USE]))


async def _structured_calls(backend: str, credentials: dict[str, str], models: list[str]) -> list[Any]:
    client = anthropic_client.create_client(Settings(_env_file=None, api_backend=backend, **credentials))
    await client.start()
    try:
        return [
            await call_background_llm_structured(
                client,
                model,
                "You classify a fact.",
                "this call's own input",
                "emit_verdict",
                "Emit the verdict.",
                _SCHEMA,
            )
            for model in models
        ]
    finally:
        await client.close()


@pytest.mark.parametrize("credentials", [_OAUTH, _API_KEY], ids=["oauth", "api_key"])
@pytest.mark.parametrize("backend", ["httpx", "sdk"])
async def test_a_model_that_accepts_forcing_gets_todays_request(backend, credentials, monkeypatch):
    bodies = _serve(monkeypatch, lambda body: (200, _message([_TOOL_USE])))

    assert await _structured_calls(backend, credentials, ["claude-haiku-4-5-20251001"]) == [{"verdict": "ok"}]

    assert bodies == [_todays_request("claude-haiku-4-5-20251001")]


@pytest.mark.parametrize("credentials", [_OAUTH, _API_KEY], ids=["oauth", "api_key"])
@pytest.mark.parametrize("backend", ["httpx", "sdk"])
async def test_a_model_that_rejects_forcing_is_asked_for_the_tool(backend, credentials, monkeypatch):
    bodies = _serve(monkeypatch, _rejects_forcing)

    assert await _structured_calls(backend, credentials, ["claude-sonnet-5-5"]) == [{"verdict": "ok"}], (
        "the rejected forced request was not sent again without tool_choice"
    )

    forced = _todays_request("claude-sonnet-5-5")
    asked = {key: value for key, value in forced.items() if key != "tool_choice"}
    asked["system"] = [*forced["system"], {"type": "text", "text": _INSTRUCTION}]
    assert bodies == [forced, asked]


@pytest.mark.parametrize("backend", ["httpx", "sdk"])
async def test_the_rejection_is_remembered_per_model(backend, monkeypatch, caplog):
    bodies = _serve(monkeypatch, _rejects_forcing)

    with caplog.at_level(logging.INFO, logger="nous.handlers"):
        models = ["claude-sonnet-5-5", "claude-sonnet-5-5", "claude-opus-5-5"]
        results = await _structured_calls(backend, _OAUTH, models)

    assert results == [{"verdict": "ok"}] * 3
    assert [("tool_choice" in body, body["model"]) for body in bodies] == [
        (True, "claude-sonnet-5-5"),
        (False, "claude-sonnet-5-5"),
        (False, "claude-sonnet-5-5"),
        (True, "claude-opus-5-5"),
        (False, "claude-opus-5-5"),
    ], "a remembered model was forced again, or another model was not tried forced first"
    noted = [r.getMessage() for r in caplog.records if "rejects a forced tool_choice" in r.getMessage()]
    assert noted == [
        "Model claude-sonnet-5-5 rejects a forced tool_choice; asking for the tool in the prompt",
        "Model claude-opus-5-5 rejects a forced tool_choice; asking for the tool in the prompt",
    ]


# Other 400s on the 5.5 generation that also say "not supported for this model", as the API words them,
# and a tool_choice error that is not the rejection.
_OTHER_400S = [
    "tools.0.input_schema: bad",
    '"thinking.type.disabled" is not supported for this model. Use "thinking.type.adaptive" and '
    '"output_config.effort" to control thinking behavior.',
    '"thinking.type.between_tools" is not supported for this model.',
    "tool_choice.name: Tool 'emit_verdict' not found in provided tools",
]


@pytest.mark.parametrize(
    "message", _OTHER_400S, ids=["schema", "thinking disabled", "between_tools", "tool_choice name"]
)
@pytest.mark.parametrize("backend", ["httpx", "sdk"])
async def test_any_other_400_still_fails_the_call(backend, message, monkeypatch):
    error = {"type": "error", "error": {"type": "invalid_request_error", "message": message}}
    bodies = _serve(monkeypatch, lambda body: (400, error))

    assert await _structured_calls(backend, _OAUTH, ["claude-sonnet-5-5", "claude-sonnet-5-5"]) == [None, None]

    assert ["tool_choice" in body for body in bodies] == [True, True], "a different 400 was taken for the rejection"
    assert nous.handlers._REJECTS_FORCED_TOOL_CHOICE == set()


_THINKING = {"type": "thinking", "thinking": "", "signature": "sig_1"}
_TEXT = {"type": "text", "text": "Step 1: both facts are about the same job."}


@pytest.mark.parametrize(
    "content",
    [[_TOOL_USE], [_TEXT, _TOOL_USE], [_THINKING, _TOOL_USE]],
    ids=["tool call only", "text before the tool call", "thinking before the tool call"],
)
@pytest.mark.parametrize("backend", ["httpx", "sdk"])
async def test_the_tool_call_is_read_wherever_it_comes(backend, content, monkeypatch):
    """An unforced reply may open with text (Haiku 4.5 and Sonnet 4.6 walk a decision tree aloud) or with
    thinking (the 5.5 generation thinks unless told otherwise)."""
    _serve(monkeypatch, lambda body: (400, _REJECTION) if "tool_choice" in body else (200, _message(content)))

    assert await _structured_calls(backend, _OAUTH, ["claude-sonnet-5-5"]) == [{"verdict": "ok"}]
