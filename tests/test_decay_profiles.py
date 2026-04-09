"""Tests for content-type-aware pruning profiles (F016 Phase 4)."""

from nous.api.compaction import ConversationCompactor
from nous.cognitive.schemas import DECAY_PROFILE_AGES, TOOL_DECAY_PROFILES
from nous.config import Settings


class TestDecayProfiles:
    def test_read_file_is_preserve(self):
        assert TOOL_DECAY_PROFILES["read_file"] == "preserve"

    def test_web_search_is_conservative(self):
        assert TOOL_DECAY_PROFILES["web_search"] == "conservative"

    def test_bash_is_standard(self):
        assert TOOL_DECAY_PROFILES["bash"] == "standard"

    def test_recall_deep_is_aggressive(self):
        assert TOOL_DECAY_PROFILES["recall_deep"] == "aggressive"

    def test_preserve_skips_metadata(self):
        _, degrade_age, _ = DECAY_PROFILE_AGES["preserve"]
        assert degrade_age == 999  # effectively never

    def test_aggressive_early_degrade(self):
        _, degrade_age, clear_age = DECAY_PROFILE_AGES["aggressive"]
        assert degrade_age == 4
        assert clear_age == 8


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


def _make_tool_pair(tool_name, tool_input, content, tool_use_id):
    """Create an assistant tool_use + user tool_result message pair."""
    assistant = {
        "role": "assistant",
        "content": [{"type": "tool_use", "id": tool_use_id, "name": tool_name, "input": tool_input}],
    }
    user = {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": content}],
    }
    return assistant, user


class TestProfileAwarePruning:
    """Test that prune_tool_results uses per-tool decay profiles."""

    def _make_session_mixed(self, tool_configs):
        """Build messages with different tool types.

        tool_configs: list of (tool_name, content) tuples.
        """
        messages = []
        for i, (name, content) in enumerate(tool_configs):
            asst, user = _make_tool_pair(name, {"arg": f"val_{i}"}, content, f"tu_{i}")
            messages.extend([asst, user])
        return messages

    def test_read_file_preserve_skips_hard_clear(self):
        """read_file (preserve profile) has clear_age=20, so at age 14 it should NOT be hard-cleared.

        With standard profile (clear_age=12), age 14 would be hard-cleared.
        """
        s = _make_settings()
        c = ConversationCompactor(s)

        # Build 16 tool results: first is read_file, rest are bash
        # At position 0 with 16 total tool msgs and keep_last=2, age = 16
        # preserve clear_age=20, so age 16 < 20 -> NOT hard-cleared (metadata degrade instead)
        # preserve degrade_age=999, so age 16 < 999 -> NOT degraded either (soft-trim only)
        configs = [("read_file", "x" * 500)] + [("bash", f"result_{i}\n" * 30) for i in range(15)]
        messages = self._make_session_mixed(configs)
        c.prune_tool_results(messages)

        # read_file at position 0 should NOT be hard-cleared (preserve profile)
        first_content = messages[1]["content"][0]["content"]
        assert "cleared" not in first_content.lower()

    def test_bash_standard_hard_clears_at_age_12(self):
        """bash (standard profile) has clear_age=12, so at age 14 it SHOULD be hard-cleared."""
        s = _make_settings()
        c = ConversationCompactor(s)

        # Build 16 tool results: first is bash, rest are bash
        configs = [("bash", f"output_{i}\n" * 30) for i in range(16)]
        messages = self._make_session_mixed(configs)
        c.prune_tool_results(messages)

        # bash at position 0 (age 16 >= 12) should be hard-cleared
        first_content = messages[1]["content"][0]["content"]
        assert "cleared" in first_content.lower()

    def test_recall_deep_aggressive_clears_early(self):
        """recall_deep (aggressive profile) has clear_age=8, so at age 9 it SHOULD be hard-cleared."""
        s = _make_settings()
        c = ConversationCompactor(s)

        # Build 12 tool results: first is recall_deep, rest are bash
        configs = [("recall_deep", "memory result\n" * 30)] + [("bash", f"r_{i}") for i in range(11)]
        messages = self._make_session_mixed(configs)
        c.prune_tool_results(messages)

        # recall_deep at position 0 (age 12 >= 8) should be hard-cleared
        first_content = messages[1]["content"][0]["content"]
        assert "cleared" in first_content.lower()

    def test_web_search_conservative_survives_longer(self):
        """web_search (conservative profile) has clear_age=15, so at age 12 it should NOT be hard-cleared."""
        s = _make_settings()
        c = ConversationCompactor(s)

        # Build 14 tool results: first is web_search, rest are bash
        configs = [("web_search", "search results\n" * 30)] + [("bash", f"r_{i}") for i in range(13)]
        messages = self._make_session_mixed(configs)
        c.prune_tool_results(messages)

        # web_search at position 0 (age 14 < 15) should NOT be hard-cleared
        first_content = messages[1]["content"][0]["content"]
        assert "cleared" not in first_content.lower()

    def test_unknown_tool_uses_standard(self):
        """Unknown tools default to standard profile (clear_age=12)."""
        s = _make_settings()
        c = ConversationCompactor(s)

        configs = [("some_custom_tool", "data\n" * 30)] + [("bash", f"r_{i}") for i in range(15)]
        messages = self._make_session_mixed(configs)
        c.prune_tool_results(messages)

        # unknown tool at position 0 (age 16 >= 12) should be hard-cleared
        first_content = messages[1]["content"][0]["content"]
        assert "cleared" in first_content.lower()
