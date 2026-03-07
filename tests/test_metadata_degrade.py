"""Tests for 4-tier pruning with metadata degradation (F016 Phase 1)."""

import pytest
from nous.api.compaction import ConversationCompactor
from nous.config import Settings


def _make_settings(**overrides):
    defaults = dict(
        NOUS_TOOL_PRUNING_ENABLED="true",
        NOUS_TOOL_HARD_CLEAR_AFTER="12",
        NOUS_TOOL_METADATA_DEGRADE_AFTER="8",
        NOUS_KEEP_LAST_TOOL_RESULTS="2",
        NOUS_TOOL_SOFT_TRIM_CHARS="4000",
        NOUS_TOOL_SOFT_TRIM_HEAD="1500",
        NOUS_TOOL_SOFT_TRIM_TAIL="1500",
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _make_tool_msg(tool_name, tool_input, content, tool_use_id="tu_1"):
    assistant = {
        "role": "assistant",
        "content": [{"type": "tool_use", "id": tool_use_id, "name": tool_name, "input": tool_input}],
    }
    user = {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": content}],
    }
    return assistant, user


class TestBuildToolUseIndex:
    def test_builds_index(self):
        c = ConversationCompactor(_make_settings())
        asst, user = _make_tool_msg("read_file", {"path": "app.py"}, "content", "tu_1")
        index = c._build_tool_use_index([asst, user])
        assert "tu_1" in index
        assert index["tu_1"]["name"] == "read_file"

    def test_empty_messages(self):
        c = ConversationCompactor(_make_settings())
        assert c._build_tool_use_index([]) == {}


class TestMetadataDegrade:
    def test_small_content_unchanged(self):
        c = ConversationCompactor(_make_settings())
        item = {"type": "tool_result", "content": "small"}
        c._metadata_degrade(item, {"name": "bash", "input": {"command": "ls"}})
        assert item["content"] == "small"

    def test_large_content_degraded(self):
        c = ConversationCompactor(_make_settings())
        content = "import os\n" + "x = 1\n" * 100
        item = {"type": "tool_result", "content": content}
        c._metadata_degrade(item, {"name": "read_file", "input": {"path": "app.py"}})
        assert item["content"].startswith("[read_file(")
        assert "re-fetchable" in item["content"]

    def test_no_refetch_for_web_search(self):
        c = ConversationCompactor(_make_settings())
        content = "result\n" * 50
        item = {"type": "tool_result", "content": content}
        c._metadata_degrade(item, {"name": "web_search", "input": {"query": "test"}})
        assert "re-fetchable" not in item["content"]

    def test_no_tool_block_graceful(self):
        c = ConversationCompactor(_make_settings())
        content = "data\n" * 50
        item = {"type": "tool_result", "content": content}
        c._metadata_degrade(item, None)
        assert item["content"].startswith("[tool(")


class TestFourTierPruning:
    def _make_session(self, n_tool_msgs, content_size=500):
        messages = []
        for i in range(n_tool_msgs):
            asst, user = _make_tool_msg(
                "read_file", {"path": f"file_{i}.py"},
                f"import os\n" + f"line {i}\n" * (content_size // 10),
                tool_use_id=f"tu_{i}",
            )
            messages.extend([asst, user])
        return messages

    def test_protected_messages_unchanged(self):
        s = _make_settings()
        c = ConversationCompactor(s)
        messages = self._make_session(4)
        original_last = messages[-1]["content"][0]["content"]
        c.prune_tool_results(messages)
        assert messages[-1]["content"][0]["content"] == original_last

    def test_hard_clear_at_age_12(self):
        s = _make_settings()
        c = ConversationCompactor(s)
        messages = self._make_session(16)
        c.prune_tool_results(messages)
        # Oldest tool result (position 0, age 16) should be cleared
        tool_msgs = [m for m in messages if m.get("role") == "user"
                     and isinstance(m.get("content"), list)
                     and len(m["content"]) > 0
                     and isinstance(m["content"][0], dict)
                     and m["content"][0].get("type") == "tool_result"]
        cleared = tool_msgs[0]["content"][0]["content"]
        assert "cleared" in cleared.lower()

    def test_metadata_degrade_at_age_8(self):
        s = _make_settings()
        c = ConversationCompactor(s)
        messages = self._make_session(14)
        c.prune_tool_results(messages)
        tool_msgs = [m for m in messages if m.get("role") == "user"
                     and isinstance(m.get("content"), list)
                     and len(m["content"]) > 0
                     and isinstance(m["content"][0], dict)
                     and m["content"][0].get("type") == "tool_result"]
        # Find one in the metadata-degraded range (age 8-11)
        # With 14 tool msgs and keep_last=2, tool_msgs[4] has age 10
        degraded = tool_msgs[4]["content"][0]["content"]
        assert degraded.startswith("[read_file(")


class TestConfigValidation:
    def test_degrade_must_be_less_than_hard_clear(self):
        with pytest.raises(ValueError, match="tool_metadata_degrade_after"):
            Settings(
                NOUS_TOOL_METADATA_DEGRADE_AFTER="12",
                NOUS_TOOL_HARD_CLEAR_AFTER="8",
            )

    def test_valid_tiers(self):
        s = Settings(
            NOUS_TOOL_METADATA_DEGRADE_AFTER="8",
            NOUS_TOOL_HARD_CLEAR_AFTER="12",
        )
        assert s.tool_metadata_degrade_after == 8
