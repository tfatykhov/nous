"""Tests for ReversibleCache — Postgres-backed tool result cache."""

import json
import pytest

from nous.api.tool_cache import (
    compute_hash_key,
    _keyword_filter,
    NON_REFETCHABLE_TOOLS,
)
from nous.api.tools import CACHE_RETRIEVE_TOOL_DEF


class TestHashKey:
    def test_deterministic(self):
        content = "some tool output"
        h1 = compute_hash_key(content)
        h2 = compute_hash_key(content)
        assert h1 == h2

    def test_32_chars(self):
        # CR-7: widened 16 -> 32 hex chars (128-bit) to make collisions negligible.
        h = compute_hash_key("test content")
        assert len(h) == 32

    def test_different_content_different_hash(self):
        h1 = compute_hash_key("content A")
        h2 = compute_hash_key("content B")
        assert h1 != h2

    def test_hex_chars_only(self):
        h = compute_hash_key("any content here")
        assert all(c in "0123456789abcdef" for c in h)


class TestNonRefetchableTools:
    def test_web_search_included(self):
        assert "web_search" in NON_REFETCHABLE_TOOLS

    def test_web_fetch_included(self):
        assert "web_fetch" in NON_REFETCHABLE_TOOLS

    def test_bash_not_included(self):
        assert "bash" not in NON_REFETCHABLE_TOOLS

    def test_read_file_not_included(self):
        assert "read_file" not in NON_REFETCHABLE_TOOLS


class TestKeywordFilter:
    def test_json_array_filter(self):
        items = [
            {"title": "Python tutorial", "url": "https://python.org"},
            {"title": "Rust guide", "url": "https://rust-lang.org"},
            {"title": "Python async", "url": "https://docs.python.org"},
        ]
        content = json.dumps(items)
        result = _keyword_filter(content, "python")
        parsed = json.loads(result)
        assert len(parsed) == 2
        assert all("python" in json.dumps(item).lower() for item in parsed)

    def test_json_no_match(self):
        items = [{"title": "A"}, {"title": "B"}]
        content = json.dumps(items)
        result = _keyword_filter(content, "zzz_nonexistent")
        assert "No items matching" in result

    def test_text_line_filter(self):
        content = "line with python\nline with rust\nline with python again"
        result = _keyword_filter(content, "python")
        lines = result.split("\n")
        assert len(lines) == 2
        assert all("python" in ln.lower() for ln in lines)

    def test_text_no_match(self):
        content = "line 1\nline 2\nline 3"
        result = _keyword_filter(content, "zzz_nonexistent")
        assert "No lines matching" in result

    def test_max_20_json_items(self):
        items = [{"title": f"python item {i}"} for i in range(30)]
        content = json.dumps(items)
        result = _keyword_filter(content, "python")
        parsed = json.loads(result)
        assert len(parsed) == 20

    def test_max_50_text_lines(self):
        content = "\n".join(f"match line {i}" for i in range(100))
        result = _keyword_filter(content, "match")
        assert len(result.split("\n")) == 50


class TestCacheRetrieveToolDef:
    def test_tool_def_has_name(self):
        assert CACHE_RETRIEVE_TOOL_DEF["name"] == "cache_retrieve"

    def test_tool_def_has_description(self):
        assert "SmartCompressed" in CACHE_RETRIEVE_TOOL_DEF["description"]

    def test_tool_def_requires_hash_key(self):
        schema = CACHE_RETRIEVE_TOOL_DEF["input_schema"]
        assert "hash_key" in schema["required"]

    def test_tool_def_query_optional(self):
        schema = CACHE_RETRIEVE_TOOL_DEF["input_schema"]
        assert "query" in schema["properties"]
        assert "query" not in schema["required"]


class TestFrameToolAccess:
    def test_cache_retrieve_in_conversation_frame(self):
        from nous.api.runner import FRAME_TOOLS
        assert "cache_retrieve" in FRAME_TOOLS["conversation"]

    def test_cache_retrieve_in_question_frame(self):
        from nous.api.runner import FRAME_TOOLS
        assert "cache_retrieve" in FRAME_TOOLS["question"]

    def test_cache_retrieve_in_decision_frame(self):
        from nous.api.runner import FRAME_TOOLS
        assert "cache_retrieve" in FRAME_TOOLS["decision"]

    def test_cache_retrieve_in_debug_frame(self):
        from nous.api.runner import FRAME_TOOLS
        assert "cache_retrieve" in FRAME_TOOLS["debug"]

    def test_cache_retrieve_in_task_via_wildcard(self):
        from nous.api.runner import FRAME_TOOLS
        assert "*" in FRAME_TOOLS["task"]  # wildcard covers all
