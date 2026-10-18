"""Every request method of both clients sends Haiku 5+ thinking disabled unless the payload says otherwise.

Haiku 5.5 thinks when a request has no ``thinking`` field (measured: a 512-token
text call spent 370-418 output tokens, against 127-148 on Haiku 4.5). Nous's
background requests were written for models that do not think unless asked, so
the client adds ``{"type": "disabled"}`` for those models. An explicit choice
stands, and a request to any other model goes out exactly as before. Every
request goes to a stubbed transport under the real backends: nothing reaches
the network.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from nous.api import anthropic_client
from nous.config import Settings

_PREAMBLE = "You are Claude Code, Anthropic's official CLI for Claude."
_METHODS = ["call", "stream", "call_streaming_aggregated"]
_BACKENDS = ["httpx", "sdk"]

_SSE_EVENTS = [
    {
        "type": "message_start",
        "message": {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "m",
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    },
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "ok"}},
    {"type": "content_block_stop", "index": 0},
    {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": {"output_tokens": 1},
    },
    {"type": "message_stop"},
]
_SSE = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in _SSE_EVENTS)


def _answer(bodies: list[bytes]):
    def answer(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        if json.loads(request.content).get("stream"):
            return httpx.Response(200, text=_SSE, headers={"content-type": "text/event-stream"})
        message = {"id": "msg_1", "type": "message", "role": "assistant", "model": "m"}
        message |= {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn", "stop_sequence": None}
        return httpx.Response(200, json=message | {"usage": {"input_tokens": 1, "output_tokens": 1}})

    return answer


async def _send(backend: str, method: str, payload: dict[str, Any], monkeypatch) -> bytes:
    """Send ``payload`` through ``method`` of the real ``backend`` client; return the request body it sent."""
    bodies: list[bytes] = []
    monkeypatch.setattr(
        anthropic_client, "_build_transport_with_env_proxies", lambda **_: (httpx.MockTransport(_answer(bodies)), {})
    )
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    client = anthropic_client.create_client(
        Settings(_env_file=None, api_backend=backend, ANTHROPIC_AUTH_TOKEN="sk-ant-oat-test-token")
    )
    await client.start()
    try:
        if method == "stream":
            events = [event async for event in client.stream(payload)]
            assert not [event for event in events if event.type == "error"], events
        else:
            response = await getattr(client, method)(payload)
            assert response.stop_reason == "end_turn"
    finally:
        await client.close()
    (body,) = bodies
    return body


def _payload(model: str, **extra: Any) -> dict[str, Any]:
    return {
        "model": model,
        "max_tokens": 512,
        "system": [{"type": "text", "text": _PREAMBLE}],
        "messages": [{"role": "user", "content": [{"type": "text", "text": "Is this reply grounded?"}]}],
        **extra,
    }


@pytest.mark.parametrize("method", _METHODS)
@pytest.mark.parametrize("backend", _BACKENDS)
async def test_haiku_5_without_thinking_goes_out_with_thinking_disabled(backend, method, monkeypatch):
    payload = _payload("claude-haiku-5-5")

    body = json.loads(await _send(backend, method, payload, monkeypatch))

    assert body["thinking"] == {"type": "disabled"}
    assert "thinking" not in payload, "the caller's payload was mutated"


@pytest.mark.parametrize("method", _METHODS)
@pytest.mark.parametrize("backend", _BACKENDS)
async def test_an_explicit_adaptive_thinking_is_kept(backend, method, monkeypatch):
    body = json.loads(
        await _send(backend, method, _payload("claude-haiku-5-5", thinking={"type": "adaptive"}), monkeypatch)
    )

    assert body["thinking"] == {"type": "adaptive"}


@pytest.mark.parametrize("model", ["claude-haiku-4-5-20251001", "claude-sonnet-5-5"])
@pytest.mark.parametrize("method", _METHODS)
@pytest.mark.parametrize("backend", _BACKENDS)
async def test_a_request_to_any_other_model_is_byte_identical_to_before(backend, method, model, monkeypatch):
    payload = _payload(model, tools=[{"name": "t", "description": "d", "input_schema": {"type": "object"}}])

    after = await _send(backend, method, payload, monkeypatch)
    monkeypatch.setattr(anthropic_client, "with_default_thinking", lambda payload: payload)
    before = await _send(backend, method, payload, monkeypatch)

    assert after == before
    assert "thinking" not in json.loads(after)
