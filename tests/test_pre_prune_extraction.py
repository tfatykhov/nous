"""Tests for pre-prune fact extraction (F016 Phase 4.0.1)."""

from nous.api.compaction import ConversationCompactor
from nous.config import Settings


class TestPrePruneExtraction:
    def _make_compactor(self):
        return ConversationCompactor(Settings())

    def test_extracts_urls(self):
        c = self._make_compactor()
        facts = c._extract_facts_before_clear("web_search", "See https://example.com/api for docs")
        assert any("https://example.com/api" in f for f in facts)

    def test_extracts_file_paths(self):
        c = self._make_compactor()
        facts = c._extract_facts_before_clear("bash", "Error in /usr/local/bin/app")
        assert any("/usr/local/bin/app" in f for f in facts)

    def test_caps_at_10(self):
        c = self._make_compactor()
        content = "\n".join(f"https://example.com/{i}" for i in range(20))
        facts = c._extract_facts_before_clear("web_search", content)
        assert len(facts) <= 10

    def test_empty_content(self):
        c = self._make_compactor()
        assert c._extract_facts_before_clear("bash", "") == []

    def test_prune_returns_extracted(self):
        """Integration: prune_tool_results returns extracted facts."""
        c = self._make_compactor()
        # prune should return a list now
        result = c.prune_tool_results([])
        assert isinstance(result, list)

    def test_extracts_key_values(self):
        c = self._make_compactor()
        facts = c._extract_facts_before_clear("bash", "version: 3.12.1\nstatus: running")
        assert any("version:" in f or "status:" in f for f in facts)

    def test_hard_clear_extracts_from_content(self):
        """Hard-clear tier extracts facts before clearing."""
        c = self._make_compactor()
        # Build messages that will be hard-cleared (old enough)
        # We need enough tool results so that the first ones exceed hard_clear_after
        messages = []
        # Add an assistant message with tool_use so we can build the index
        messages.append({
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "tu_1", "name": "web_search", "input": {"query": "test"}}],
        })
        # Tool result with a URL that should be extracted
        messages.append({
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "tu_1", "content": "Found at https://important.example.com/docs"}],
        })
        # Add many more tool results to push the first one past hard_clear_after
        for i in range(2, 20):
            messages.append({
                "role": "assistant",
                "content": [{"type": "tool_use", "id": f"tu_{i}", "name": "bash", "input": {"command": "echo hi"}}],
            })
            messages.append({
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": f"tu_{i}", "content": "ok"}],
            })

        result = c.prune_tool_results(messages)
        assert isinstance(result, list)
        # The first tool result should have been hard-cleared and facts extracted
        assert any("https://important.example.com/docs" in f for f in result)
