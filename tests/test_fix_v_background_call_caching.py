"""Background LLM calls mark for caching only a prefix the next call reuses.

A cache breakpoint on content that differs from call to call writes a new
cache entry every time, at the cache-write price, and nothing ever reads it.
Each test records the request a call would send, from a stand-in client or
from a stubbed transport under the real client: nothing here reaches the
network.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from nous.api import anthropic_client
from nous.config import Settings
from nous.handlers import call_background_llm, call_background_llm_structured
from nous.heart.admission import AdmissionLLMClient

_PREAMBLE = "You are Claude Code, Anthropic's official CLI for Claude."
_CACHED = {"type": "ephemeral"}
_SCHEMA = {"type": "object", "properties": {"verdict": {"type": "string"}}, "required": ["verdict"]}


class _Recorder:
    """Stands in for the Anthropic client: records every payload, answers with ``content``."""

    def __init__(self, content: list[dict[str, Any]]) -> None:
        self.payloads: list[dict[str, Any]] = []
        self._content = content

    async def call(self, payload: dict[str, Any]) -> Any:
        self.payloads.append(payload)
        response = MagicMock()
        response.content = self._content
        return response


async def _plain(client: Any, per_call: str) -> Any:
    return await call_background_llm(client, "claude-sonnet-4-6", "You summarize an episode.", per_call)


async def _structured(client: Any, per_call: str) -> Any:
    return await call_background_llm_structured(
        client, "claude-sonnet-4-6", "You classify a fact.", per_call, "emit_verdict", "Emit the verdict.", _SCHEMA
    )


async def _admission(client: Any, per_call: str) -> Any:
    return await AdmissionLLMClient(api_client=client).complete("claude-sonnet-4-6", per_call)


_TEXT = [{"type": "text", "text": "ok"}]
_TOOL_USE = [{"type": "tool_use", "id": "toolu_1", "name": "emit_verdict", "input": {"verdict": "ok"}}]
_CALLERS = [(_plain, _TEXT), (_structured, _TOOL_USE), (_admission, _TEXT)]
_IDS = ["call_background_llm", "call_background_llm_structured", "admission utility score"]


def _cached_prefixes(payload: dict[str, Any]) -> list[list[dict[str, Any]]]:
    """The prefix each cache breakpoint marks, in the order the API caches a request: tools, system, messages."""
    blocks = [*payload.get("tools", []), *payload["system"]]
    for message in payload["messages"]:
        blocks += message["content"]
    unmarked = [{key: value for key, value in block.items() if key != "cache_control"} for block in blocks]
    return [unmarked[: i + 1] for i, block in enumerate(blocks) if "cache_control" in block]


@pytest.mark.parametrize(("call", "content"), _CALLERS, ids=_IDS)
async def test_a_background_call_marks_for_caching_only_what_the_next_call_reuses(call, content):
    recorder = _Recorder(content)

    await call(recorder, "the first call's own input")
    await call(recorder, "the second call's own input")

    first, second = recorder.payloads
    assert _cached_prefixes(first), "the prefix every call shares is no longer marked for caching"
    assert _cached_prefixes(first) == _cached_prefixes(second), (
        "a cache breakpoint covers content that differs from call to call"
    )


@pytest.mark.parametrize(
    ("call", "content", "rest"),
    [
        (_plain, _TEXT, {"max_tokens": 800, "system_prompt": "You summarize an episode."}),
        (
            _structured,
            _TOOL_USE,
            {
                "max_tokens": 1500,
                "system_prompt": "You classify a fact.",
                "tools": [{"name": "emit_verdict", "description": "Emit the verdict.", "input_schema": _SCHEMA}],
                "tool_choice": {"type": "tool", "name": "emit_verdict"},
            },
        ),
        (
            _admission,
            _TEXT,
            {
                "max_tokens": 10,
                "system_prompt": "You are scoring the utility of a candidate fact for an AI agent's long-term memory.",
            },
        ),
    ],
    ids=_IDS,
)
async def test_the_request_is_otherwise_unchanged(call, content, rest):
    """The preamble stays block 0 with its marker (the OAuth path needs it first), and so does the system prompt."""
    recorder = _Recorder(content)

    await call(recorder, "this call's own input")

    expected = {
        "model": "claude-sonnet-4-6",
        "max_tokens": rest["max_tokens"],
        "system": [
            {"type": "text", "text": _PREAMBLE, "cache_control": _CACHED},
            {"type": "text", "text": rest["system_prompt"], "cache_control": _CACHED},
        ],
        "messages": [{"role": "user", "content": [{"type": "text", "text": "this call's own input"}]}],
    }
    if "tools" in rest:
        expected["tools"] = rest["tools"]
        expected["tool_choice"] = rest["tool_choice"]
    assert recorder.payloads == [expected]


@pytest.mark.parametrize("auth", ["oauth", "api_key"])
@pytest.mark.parametrize("backend", ["httpx", "sdk"])
async def test_both_backends_send_the_request_as_built(backend, auth, monkeypatch):
    """What reaches the wire, with an OAuth token and with an API key.

    The preamble is still system block 0 (the OAuth path needs it first), both backends send the same
    breakpoints, and neither adds request-wide caching on top.
    """
    bodies: list[dict[str, Any]] = []
    betas: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        betas.append(request.headers.get("anthropic-beta", ""))
        message = {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-4-6"}
        message |= {"content": _TOOL_USE, "stop_reason": "tool_use", "stop_sequence": None}
        return httpx.Response(200, json=message | {"usage": {"input_tokens": 1, "output_tokens": 1}})

    monkeypatch.setattr(
        anthropic_client, "_build_transport_with_env_proxies", lambda **_: (httpx.MockTransport(answer), {})
    )
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    credentials = (
        {"ANTHROPIC_AUTH_TOKEN": "sk-ant-oat-test-token"} if auth == "oauth" else {"ANTHROPIC_API_KEY": "test-key"}
    )
    client = anthropic_client.create_client(Settings(_env_file=None, api_backend=backend, **credentials))
    await client.start()
    try:
        assert await _structured(client, "the first call's own input") == {"verdict": "ok"}
        assert await _structured(client, "the second call's own input") == {"verdict": "ok"}
    finally:
        await client.close()

    first, second = bodies
    assert all(("oauth-2025-04-20" in beta) == (auth == "oauth") for beta in betas), (
        "a request took the other auth path"
    )
    assert first["system"][0] == {"type": "text", "text": _PREAMBLE, "cache_control": _CACHED}, (
        "the Claude Code preamble is no longer system block 0"
    )
    assert "cache_control" not in first, "the request asks to cache everything up to its last block"
    assert _cached_prefixes(first), "the prefix every call shares is no longer marked for caching"
    assert _cached_prefixes(first) == _cached_prefixes(second), (
        "a cache breakpoint covers content that differs from call to call"
    )
