"""Tests for F036 tool schema cache in ToolDispatcher."""

from __future__ import annotations

from unittest.mock import patch

from nous.api.tools import ToolDispatcher

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_dispatcher() -> ToolDispatcher:
    """Create a ToolDispatcher with two registered dummy tools."""
    dispatcher = ToolDispatcher()

    async def dummy(**kwargs):
        return {"content": [{"type": "text", "text": "ok"}]}

    dispatcher.register("tool_a", dummy, {"description": "Tool A", "type": "object", "properties": {}})
    dispatcher.register("tool_b", dummy, {"description": "Tool B", "type": "object", "properties": {}})
    return dispatcher


MOCK_FRAME_TOOLS = {
    "task": ["tool_a"],
    "debug": ["tool_a", "tool_b"],
    "all": ["*"],
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestToolSchemaCache:
    """F036: ToolDispatcher.available_tools caching."""

    def test_cache_hit_same_frame(self):
        """Calling available_tools twice with the same frame_id returns
        identical results and the second call uses the cache (no rebuild)."""
        dispatcher = _make_dispatcher()

        with patch("nous.api.runner.FRAME_TOOLS", MOCK_FRAME_TOOLS):
            first = dispatcher.available_tools("task")
            # Cache should now be populated
            assert "task" in dispatcher._tool_schema_cache

            second = dispatcher.available_tools("task")

        assert first == second
        # Both calls return lists with exactly one tool (tool_a)
        assert len(first) == 1
        assert first[0]["name"] == "tool_a"

    def test_cache_miss_different_frame(self):
        """Different frame_id triggers a new cache entry."""
        dispatcher = _make_dispatcher()

        with patch("nous.api.runner.FRAME_TOOLS", MOCK_FRAME_TOOLS):
            task_tools = dispatcher.available_tools("task")
            debug_tools = dispatcher.available_tools("debug")

        assert len(task_tools) == 1
        assert len(debug_tools) == 2
        # Both frames should be cached
        assert "task" in dispatcher._tool_schema_cache
        assert "debug" in dispatcher._tool_schema_cache

    def test_cache_invalidation_on_register(self):
        """register() clears the entire cache."""
        dispatcher = _make_dispatcher()

        with patch("nous.api.runner.FRAME_TOOLS", MOCK_FRAME_TOOLS):
            dispatcher.available_tools("task")
            assert "task" in dispatcher._tool_schema_cache

            # Register a new tool -- cache must be cleared
            async def dummy(**kwargs):
                return {"content": [{"type": "text", "text": "ok"}]}

            dispatcher.register("tool_c", dummy, {"description": "Tool C", "type": "object", "properties": {}})
            assert dispatcher._tool_schema_cache == {}

    def test_deep_copy_prevents_cache_corruption(self):
        """Mutating the returned list must NOT corrupt the cached copy.

        This is the key test for the P1-4 fix: available_tools returns
        a deep copy so callers cannot accidentally alter the cache.
        """
        dispatcher = _make_dispatcher()

        with patch("nous.api.runner.FRAME_TOOLS", MOCK_FRAME_TOOLS):
            first = dispatcher.available_tools("debug")

            # Mutate the returned list: add an item and change existing
            first.append({"name": "injected", "description": "bad"})
            first[0]["description"] = "CORRUPTED"

            # Fetch again -- should be the original, uncorrupted data
            second = dispatcher.available_tools("debug")

        assert len(second) == 2
        assert all(t["description"] != "CORRUPTED" for t in second)
        assert all(t["name"] != "injected" for t in second)

    def test_wildcard_frame_returns_all_tools(self):
        """A frame whose allowed list contains '*' returns every registered tool."""
        dispatcher = _make_dispatcher()

        with patch("nous.api.runner.FRAME_TOOLS", MOCK_FRAME_TOOLS):
            tools = dispatcher.available_tools("all")

        names = {t["name"] for t in tools}
        assert names == {"tool_a", "tool_b"}

    def test_unknown_frame_returns_empty(self):
        """A frame not present in FRAME_TOOLS returns an empty list."""
        dispatcher = _make_dispatcher()

        with patch("nous.api.runner.FRAME_TOOLS", MOCK_FRAME_TOOLS):
            tools = dispatcher.available_tools("nonexistent_frame")

        assert tools == []
        # Empty result should still be cached
        assert "nonexistent_frame" in dispatcher._tool_schema_cache
