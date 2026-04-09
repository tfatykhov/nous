"""Tests for parse_llm_json and _extract_braces in nous.handlers."""

import json

import pytest

from nous.handlers import _extract_braces, parse_llm_json


class TestExtractBraces:
    """Unit tests for string-aware brace extraction."""

    def test_simple_object(self):
        assert _extract_braces('{"a": 1}', "{", "}") == '{"a": 1}'

    def test_simple_array(self):
        assert _extract_braces("[1, 2]", "[", "]") == "[1, 2]"

    def test_nested_braces(self):
        text = '{"a": {"b": 1}}'
        assert _extract_braces(text, "{", "}") == text

    def test_braces_inside_strings_ignored(self):
        text = '{"msg": "has } brace and { too", "x": 1}'
        assert _extract_braces(text, "{", "}") == text

    def test_escaped_quotes_inside_strings(self):
        text = r'{"msg": "say \"hello\"", "x": 1}'
        assert _extract_braces(text, "{", "}") == text

    def test_no_match_returns_none(self):
        assert _extract_braces("no json here", "{", "}") is None

    def test_unclosed_returns_none(self):
        assert _extract_braces('{"a": 1', "{", "}") is None

    def test_preamble_text_before_json(self):
        text = 'Here is the result: {"a": 1}'
        assert _extract_braces(text, "{", "}") == '{"a": 1}'

    def test_backslash_outside_string(self):
        # Backslash outside a string shouldn't cause issues
        text = r'\ {"a": 1}'
        assert _extract_braces(text, "{", "}") == '{"a": 1}'


class TestParseLlmJson:
    """Tests for parse_llm_json covering all extraction tiers."""

    # --- Tier 1: Direct parse ---

    def test_direct_parse_object(self):
        result = parse_llm_json('{"key": "value"}')
        assert result == {"key": "value"}

    def test_direct_parse_array(self):
        result = parse_llm_json("[1, 2, 3]")
        assert result == [1, 2, 3]

    # --- Tier 2: Markdown fence stripping ---

    def test_fenced_json(self):
        text = '```json\n{"patterns": ["a", "b"]}\n```'
        result = parse_llm_json(text)
        assert result == {"patterns": ["a", "b"]}

    def test_fenced_without_language_tag(self):
        text = '```\n{"x": 1}\n```'
        result = parse_llm_json(text)
        assert result == {"x": 1}

    def test_unclosed_fence(self):
        """LLM stopped without closing ``` — should still extract."""
        text = '```json\n{"summary": "hello", "lessons": []}\n'
        result = parse_llm_json(text)
        assert result == {"summary": "hello", "lessons": []}

    def test_fence_with_preamble(self):
        text = 'Here are the results:\n```json\n{"a": 1}\n```\n'
        result = parse_llm_json(text)
        assert result == {"a": 1}

    def test_unclosed_fence_with_preamble(self):
        text = "Here is my analysis:\n```json\n[1, 2, 3]\n"
        result = parse_llm_json(text)
        assert result == [1, 2, 3]

    # --- Tier 3: Brace-matching extraction ---

    def test_object_with_surrounding_text(self):
        text = 'The result is {"key": "val"} and that is all.'
        result = parse_llm_json(text)
        assert result == {"key": "val"}

    def test_object_preferred_over_array(self):
        """When { appears before [, object should be extracted."""
        text = 'result: {"items": [1, 2]}'
        result = parse_llm_json(text)
        assert result == {"items": [1, 2]}

    def test_braces_in_string_values(self):
        """Braces inside JSON string values shouldn't break extraction."""
        text = 'output: {"msg": "use } carefully and { too", "n": 1}'
        result = parse_llm_json(text)
        assert result == {"msg": "use } carefully and { too", "n": 1}

    def test_escaped_quotes_in_values(self):
        r"""Escaped quotes inside strings shouldn't break extraction."""
        text = r'result: {"msg": "say \"hi\"", "n": 1}'
        result = parse_llm_json(text)
        assert result == {"msg": 'say "hi"', "n": 1}

    # --- The exact production failure case ---

    def test_fenced_json_with_braces_in_strings(self):
        """Reproduces the production bug: fenced JSON with } inside string values."""
        text = (
            "```json\n"
            "{\n"
            '  "patterns": [\n'
            '    "Heavy focus on system introspection — the agent examined its own {codebase}"\n'
            "  ],\n"
            '  "summary": "Session involved self-analysis",\n'
            '  "lessons": ["Don\'t nest } braces in output"]\n'
            "}\n"
            "```"
        )
        result = parse_llm_json(text)
        assert isinstance(result, dict)
        assert "patterns" in result
        assert "summary" in result
        assert "lessons" in result

    def test_unclosed_fence_with_braces_in_strings(self):
        """Unclosed fence + braces in strings — the worst case."""
        text = '```json\n{\n  "patterns": ["pattern with } inside"],\n  "summary": "ok",\n  "lessons": []\n}\n'
        result = parse_llm_json(text)
        assert isinstance(result, dict)
        assert result["summary"] == "ok"

    # --- Error case ---

    def test_no_json_raises(self):
        with pytest.raises(json.JSONDecodeError):
            parse_llm_json("no json content here at all")

    def test_invalid_json_raises(self):
        with pytest.raises(json.JSONDecodeError):
            parse_llm_json("{not valid json}")

    # --- Trailing comma repair ---

    def test_trailing_comma_in_object(self):
        text = '{"a": 1, "b": 2,}'
        result = parse_llm_json(text)
        assert result == {"a": 1, "b": 2}

    def test_trailing_comma_in_array(self):
        text = '["a", "b",]'
        result = parse_llm_json(text)
        assert result == ["a", "b"]

    def test_trailing_comma_nested(self):
        text = '{"items": [1, 2,], "x": 3,}'
        result = parse_llm_json(text)
        assert result == {"items": [1, 2], "x": 3}

    def test_trailing_comma_in_fenced_json(self):
        text = '```json\n{"patterns": ["a", "b",], "summary": "ok",}\n```'
        result = parse_llm_json(text)
        assert isinstance(result, dict)
        assert result["patterns"] == ["a", "b"]

    # --- Dict preferred over array fallback ---

    def test_dict_preferred_when_both_extractable(self):
        """When fence body has a valid dict, should return dict not sub-array."""
        text = '```json\n{"patterns": ["p1"], "facts": [{"content": "f1"}]}\n```'
        result = parse_llm_json(text)
        assert isinstance(result, dict)
        assert "patterns" in result
        assert "facts" in result
