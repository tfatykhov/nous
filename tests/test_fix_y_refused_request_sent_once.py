"""The httpx backend sends a request the API refused with a non-retryable status once.

A 400 such as "tool_choice: type "tool" and "any" are not supported for this
model" will be refused again; resending it five more times, back to back,
only adds latency. Retryable statuses (408, 409, 429, 5xx) are still retried.
Every request goes to a stubbed transport: nothing reaches the network.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
import pytest

from nous.api import anthropic_client
from nous.config import Settings

_PAYLOAD = {
    "model": "claude-sonnet-5-5",
    "max_tokens": 16,
    "system": "s",
    "messages": [{"role": "user", "content": "x"}],
}
_OK = {"id": "m", "type": "message", "role": "assistant", "model": "m", "content": [{"type": "text", "text": "ok"}]}
_OK |= {"stop_reason": "end_turn", "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}}


async def _call(monkeypatch, statuses: list[int], headers: dict[str, str] | None = None) -> tuple[Any, int]:
    sent: list[int] = []

    def answer(request: httpx.Request) -> httpx.Response:
        status = statuses[min(len(sent), len(statuses) - 1)]
        sent.append(status)
        if status == 200:
            return httpx.Response(200, json=_OK)
        error = {"type": "error", "error": {"type": "some_error", "message": "refused"}}
        return httpx.Response(status, json=error, headers=headers or {})

    async def no_wait(_delay: float) -> None:
        return None

    monkeypatch.setattr(
        anthropic_client, "_build_transport_with_env_proxies", lambda **_: (httpx.MockTransport(answer), {})
    )
    monkeypatch.setattr(anthropic_client.asyncio, "sleep", no_wait)
    client = anthropic_client.create_client(
        Settings(_env_file=None, api_backend="httpx", ANTHROPIC_AUTH_TOKEN="sk-ant-oat-test-token")
    )
    await client.start()
    try:
        try:
            return await client.call(_PAYLOAD), len(sent)
        except RuntimeError as e:
            return e, len(sent)
    finally:
        await client.close()


@pytest.mark.parametrize("status", [400, 401, 403, 404, 413])
async def test_a_refused_request_is_sent_once(status, monkeypatch):
    error, sent = await _call(monkeypatch, [status])

    assert isinstance(error, RuntimeError) and f"Anthropic API error ({status})" in str(error)
    assert sent == 1, f"a request refused with {status} was sent {sent} times"


@pytest.mark.parametrize("status", [429, 529])
async def test_a_retryable_status_is_still_retried(status, monkeypatch):
    response, sent = await _call(monkeypatch, [status, 200])

    assert response.content == [{"type": "text", "text": "ok"}]
    assert sent == 2


@pytest.mark.parametrize(
    ("status", "sent_requests", "decision"),
    [(400, 1, "not retrying"), (429, 2, "retrying anyway per policy")],
)
async def test_the_x_should_retry_log_line_says_what_happens(status, sent_requests, decision, monkeypatch, caplog):
    """The API's x-should-retry: false header is logged with what the client then does."""
    with caplog.at_level(logging.INFO, logger="nous.api.anthropic_client"):
        _, sent = await _call(monkeypatch, [status, 200], headers={"x-should-retry": "false"})

    assert sent == sent_requests
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("x-should-retry")]
    assert lines == [f"x-should-retry: false (status {status}) — {decision}"]
