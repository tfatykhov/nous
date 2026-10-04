"""A structured reply that was cut off, refused, has no tool call or lacks a required key is not an answer.

At max_tokens the API stops inside the tool call, and the input it hands
back lacks every field the model had not written yet: at its production
size the sleep reflection came back without its facts, forced on Sonnet
4.6 and Haiku 4.5 and unforced on Sonnet 5.5, at 1,500 tokens. A refusal
carries no answer either, and nothing enforces the schema on a tool call.
The helper returns None and says which tool and model, why it stopped,
which kinds of block came back and which required keys are missing,
without the reply's text. Every request here goes to a stub: nothing
reaches the network.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from nous.api import anthropic_client
from nous.config import Settings
from nous.events import EventBus
from nous.handlers import call_background_llm_structured
from nous.handlers.sleep_handler import SleepHandler

_SCHEMA = {
    "type": "object",
    "properties": {"verdict": {"type": "string"}, "reason": {"type": "string"}},
    "required": ["verdict", "reason"],
}
_CUT_TOOL_USE = {"type": "tool_use", "id": "toolu_1", "name": "emit_verdict", "input": {"verdict": "ok"}}
_THINKING = {"type": "thinking", "thinking": "", "signature": "sig_1"}
_TEXT = {"type": "text", "text": "Step 1: both facts are about the same job."}


@pytest.mark.parametrize(
    ("content", "stop_reason", "missing"),
    [
        ([_CUT_TOOL_USE], "max_tokens", []),
        ([_THINKING], "max_tokens", []),
        ([_CUT_TOOL_USE], "refusal", []),
        ([], "refusal", []),
        ([_TEXT], "end_turn", []),
        ([_TEXT, _CUT_TOOL_USE], "tool_use", ["reason"]),
    ],
    ids=[
        "cut off inside the tool call",
        "cut off while thinking",
        "refused inside the tool call",
        "refused before any output",
        "no tool call",
        "a required key missing",
    ],
)
@pytest.mark.parametrize("backend", ["httpx", "sdk"])
async def test_a_reply_without_a_whole_tool_call_is_not_an_answer(
    backend, content, stop_reason, missing, monkeypatch, caplog
):
    def answer(request: httpx.Request) -> httpx.Response:
        message = {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-haiku-4-5-20251001"}
        message |= {"content": content, "stop_reason": stop_reason, "stop_sequence": None}
        return httpx.Response(200, json=message | {"usage": {"input_tokens": 1, "output_tokens": 1}})

    monkeypatch.setattr(
        anthropic_client, "_build_transport_with_env_proxies", lambda **_: (httpx.MockTransport(answer), {})
    )
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    client = anthropic_client.create_client(
        Settings(_env_file=None, api_backend=backend, ANTHROPIC_AUTH_TOKEN="sk-ant-oat-test-token")
    )
    await client.start()
    try:
        with caplog.at_level(logging.WARNING, logger="nous.handlers"):
            result = await call_background_llm_structured(
                client,
                "claude-haiku-4-5-20251001",
                "You classify a fact.",
                "this call's own input",
                "emit_verdict",
                "Emit the verdict.",
                _SCHEMA,
            )
    finally:
        await client.close()

    assert result is None, "a reply without a whole tool call was returned as an answer"
    (warning,) = [r.getMessage() for r in caplog.records if r.name == "nous.handlers"]
    blocks = [block["type"] for block in content]
    expected = f"Structured LLM call emit_verdict (model=claude-haiku-4-5-20251001) not used: stop_reason={stop_reason}"
    assert warning == f"{expected}, blocks={blocks}, missing={missing}", warning
    assert "Step 1" not in warning, "the warning carries the reply's text"


async def test_the_sleep_reflection_has_room_for_its_answer():
    """Measured with ten 500-character episodes and 15-20 orient facts: unforced on Sonnet 5.5 the call stopped
    at 1,500 in every run; at 4,000 Sonnet 5.5 took 2,078-2,510 tokens and Opus 5.5 up to 3,310. Forced on
    Sonnet 4.6 and Haiku 4.5 it stopped at 1,500 too. The ceiling costs nothing unless a reply uses it."""
    payloads: list[dict[str, Any]] = []
    llm = MagicMock()

    async def call(payload: dict[str, Any]) -> Any:
        payloads.append(payload)
        reflection = {"patterns": [], "lessons": [], "connections": [], "gaps": [], "summary": "s", "facts": []}
        return SimpleNamespace(
            content=[{"type": "tool_use", "id": "t", "name": "store_reflection", "input": reflection}]
        )

    llm.call = call
    heart = AsyncMock()
    heart.list_episodes = AsyncMock(
        return_value=[SimpleNamespace(summary="Moved the nightly export."), SimpleNamespace(summary="Renewed a cert.")]
    )
    heart.search_facts = AsyncMock(return_value=[])
    bus = MagicMock(spec=EventBus)
    handler = SleepHandler(AsyncMock(), heart, Settings(_env_file=None), bus, llm)

    assert await handler._phase_reflect({"facts_created": 0}) is True
    (payload,) = payloads
    assert payload["max_tokens"] == 6000


@pytest.mark.parametrize("backend", ["httpx", "sdk"])
async def test_a_failed_call_names_the_call(backend, monkeypatch, caplog):
    def answer(request: httpx.Request) -> httpx.Response:
        error = {"type": "invalid_request_error", "message": "tools.0.input_schema: bad"}
        return httpx.Response(400, json={"type": "error", "error": error})

    monkeypatch.setattr(
        anthropic_client, "_build_transport_with_env_proxies", lambda **_: (httpx.MockTransport(answer), {})
    )
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    client = anthropic_client.create_client(
        Settings(_env_file=None, api_backend=backend, ANTHROPIC_AUTH_TOKEN="sk-ant-oat-test-token")
    )
    await client.start()
    try:
        with caplog.at_level(logging.WARNING, logger="nous.handlers"):
            result = await call_background_llm_structured(
                client,
                "claude-haiku-4-5-20251001",
                "You classify a fact.",
                "this call's own input",
                "emit_verdict",
                "Emit the verdict.",
                _SCHEMA,
            )
    finally:
        await client.close()

    assert result is None
    (warning,) = [r.getMessage() for r in caplog.records if r.name == "nous.handlers"]
    assert warning.startswith("Structured LLM call emit_verdict (model=claude-haiku-4-5-20251001) failed: "), warning
