"""Tests for anthropic_client.py — SDK payload passthrough."""

from __future__ import annotations

from nous.api.anthropic_client import SdkAnthropicClient


class TestPayloadToKwargs:
    """SdkAnthropicClient._payload_to_kwargs passthrough tests."""

    def test_tool_choice_passed_through(self):
        """tool_choice must be forwarded to SDK kwargs."""
        client = SdkAnthropicClient.__new__(SdkAnthropicClient)
        payload = {
            "model": "claude-sonnet-4-5-20250514",
            "max_tokens": 1000,
            "system": [{"type": "text", "text": "test"}],
            "messages": [{"role": "user", "content": "test"}],
            "tools": [{"name": "test_tool", "description": "test",
                        "input_schema": {"type": "object", "properties": {}}}],
            "tool_choice": {"type": "tool", "name": "test_tool"},
        }
        kwargs = client._payload_to_kwargs(payload)
        assert kwargs["tool_choice"] == {"type": "tool", "name": "test_tool"}

    def test_tool_choice_omitted_when_absent(self):
        """tool_choice should not appear in kwargs when not in payload."""
        client = SdkAnthropicClient.__new__(SdkAnthropicClient)
        payload = {
            "model": "claude-sonnet-4-5-20250514",
            "max_tokens": 1000,
            "system": [{"type": "text", "text": "test"}],
            "messages": [{"role": "user", "content": "test"}],
        }
        kwargs = client._payload_to_kwargs(payload)
        assert "tool_choice" not in kwargs
