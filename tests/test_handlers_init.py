"""Tests for handlers/__init__.py — call_background_llm_structured()."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from nous.handlers import call_background_llm_structured


def _mock_tool_use_response(tool_input: dict, tool_name: str = "store_result") -> MagicMock:
    """Create a mock API response with a tool_use content block."""
    response = MagicMock()
    response.content = [
        {"type": "tool_use", "id": "toolu_test", "name": tool_name, "input": tool_input}
    ]
    response.stop_reason = "tool_use"
    return response


def _mock_client(response: MagicMock) -> AsyncMock:
    client = AsyncMock()
    client.call = AsyncMock(return_value=response)
    return client


SIMPLE_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "value": {"type": "integer"},
    },
    "required": ["name", "value"],
}


class TestCallBackgroundLlmStructured:
    """call_background_llm_structured uses tool_use trick for guaranteed JSON."""

    @pytest.mark.asyncio
    async def test_returns_parsed_dict(self):
        """Should return the tool input dict directly."""
        tool_input = {"name": "test", "value": 42}
        client = _mock_client(_mock_tool_use_response(tool_input))

        result = await call_background_llm_structured(
            client=client,
            model="claude-sonnet-4-5-20250514",
            system_prompt="You are a test.",
            user_message="Return test data.",
            tool_name="store_result",
            tool_description="Store the result.",
            output_schema=SIMPLE_SCHEMA,
        )

        assert result == {"name": "test", "value": 42}

    @pytest.mark.asyncio
    async def test_payload_includes_tools_and_tool_choice(self):
        """Payload sent to client must include tools array and forced tool_choice."""
        tool_input = {"name": "test", "value": 1}
        client = _mock_client(_mock_tool_use_response(tool_input))

        await call_background_llm_structured(
            client=client,
            model="claude-sonnet-4-5-20250514",
            system_prompt="sys",
            user_message="msg",
            tool_name="my_tool",
            tool_description="desc",
            output_schema=SIMPLE_SCHEMA,
        )

        payload = client.call.call_args[0][0]
        assert len(payload["tools"]) == 1
        assert payload["tools"][0]["name"] == "my_tool"
        assert payload["tools"][0]["input_schema"] == SIMPLE_SCHEMA
        assert payload["tool_choice"] == {"type": "tool", "name": "my_tool"}

    @pytest.mark.asyncio
    async def test_returns_none_on_api_error(self):
        """Should return None on API exception, not raise."""
        client = AsyncMock()
        client.call = AsyncMock(side_effect=RuntimeError("API error"))

        result = await call_background_llm_structured(
            client=client,
            model="claude-sonnet-4-5-20250514",
            system_prompt="sys",
            user_message="msg",
            tool_name="store_result",
            tool_description="desc",
            output_schema=SIMPLE_SCHEMA,
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_no_tool_use_block(self):
        """Should return None if response has no tool_use block."""
        response = MagicMock()
        response.content = [{"type": "text", "text": "no tool use here"}]
        client = _mock_client(response)

        result = await call_background_llm_structured(
            client=client,
            model="claude-sonnet-4-5-20250514",
            system_prompt="sys",
            user_message="msg",
            tool_name="store_result",
            tool_description="desc",
            output_schema=SIMPLE_SCHEMA,
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_max_tokens_passed_through(self):
        """max_tokens should be forwarded to the payload."""
        tool_input = {"name": "test", "value": 1}
        client = _mock_client(_mock_tool_use_response(tool_input))

        await call_background_llm_structured(
            client=client,
            model="claude-sonnet-4-5-20250514",
            system_prompt="sys",
            user_message="msg",
            tool_name="store_result",
            tool_description="desc",
            output_schema=SIMPLE_SCHEMA,
            max_tokens=2000,
        )

        payload = client.call.call_args[0][0]
        assert payload["max_tokens"] == 2000

    @pytest.mark.asyncio
    async def test_mixed_content_blocks_extracts_tool_use(self):
        """P2-3: Response with both text and tool_use blocks extracts tool_use correctly."""
        tool_input = {"name": "extracted", "value": 99}
        response = MagicMock()
        response.content = [
            {"type": "text", "text": "Here is the analysis..."},
            {"type": "tool_use", "id": "toolu_mixed", "name": "store_result", "input": tool_input},
        ]
        client = _mock_client(response)

        result = await call_background_llm_structured(
            client=client,
            model="claude-sonnet-4-5-20250514",
            system_prompt="sys",
            user_message="msg",
            tool_name="store_result",
            tool_description="desc",
            output_schema=SIMPLE_SCHEMA,
        )

        assert result == {"name": "extracted", "value": 99}
