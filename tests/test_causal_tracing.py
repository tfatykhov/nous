"""Tests for F035.2 Causal Chain Tracing."""

from nous.events import Event


class TestEventCausalFields:
    """Test Event dataclass causal chain fields."""

    def test_event_has_event_id(self):
        """Event should auto-generate a 12-char hex event_id."""
        e = Event(type="test", agent_id="a1")
        assert e.event_id is not None
        assert len(e.event_id) == 12
        assert all(c in "0123456789abcdef" for c in e.event_id)

    def test_event_ids_are_unique(self):
        """Each Event should get a unique event_id."""
        e1 = Event(type="test", agent_id="a1")
        e2 = Event(type="test", agent_id="a1")
        assert e1.event_id != e2.event_id

    def test_trace_id_defaults_none(self):
        """trace_id should default to None."""
        e = Event(type="test", agent_id="a1")
        assert e.trace_id is None

    def test_caused_by_defaults_none(self):
        """caused_by should default to None."""
        e = Event(type="test", agent_id="a1")
        assert e.caused_by is None

    def test_root_event_sets_trace_id(self):
        """Root event pattern: trace_id = event_id."""
        e = Event(type="session_ended", agent_id="a1")
        e.trace_id = e.event_id  # Root event pattern
        assert e.trace_id == e.event_id
        assert e.caused_by is None

    def test_child_event_inherits_trace(self):
        """Child event inherits trace_id and sets caused_by."""
        root = Event(type="session_ended", agent_id="a1")
        root.trace_id = root.event_id

        child = Event(
            type="episode_summarized",
            agent_id="a1",
            trace_id=root.trace_id,
            caused_by=root.event_id,
        )
        assert child.trace_id == root.trace_id
        assert child.caused_by == root.event_id
        assert child.event_id != root.event_id

    def test_modification_tagging(self):
        """Events can tag modifications in data dict."""
        e = Event(
            type="sleep_completed",
            agent_id="a1",
            data={"modifies": "memory", "phases_completed": ["consolidate"]},
        )
        assert e.data["modifies"] == "memory"

    def test_trace_propagation_chain(self):
        """Full chain: root -> child -> grandchild all share trace_id."""
        root = Event(type="session_ended", agent_id="a1")
        root.trace_id = root.event_id

        child = Event(
            type="episode_summarized",
            agent_id="a1",
            trace_id=root.trace_id,
            caused_by=root.event_id,
        )

        grandchild = Event(
            type="outcome_signals_detected",
            agent_id="a1",
            trace_id=child.trace_id,
            caused_by=child.event_id,
        )

        # All share the same trace_id (the root's event_id)
        assert root.trace_id == child.trace_id == grandchild.trace_id
        # Each points to its direct parent
        assert root.caused_by is None
        assert child.caused_by == root.event_id
        assert grandchild.caused_by == child.event_id
        # All have unique event_ids
        assert len({root.event_id, child.event_id, grandchild.event_id}) == 3

    def test_backward_compatible_construction(self):
        """Existing code that creates Events without new fields still works."""
        e = Event(type="test", agent_id="a1", data={"key": "val"}, session_id="s1")
        assert e.event_id is not None
        assert e.trace_id is None
        assert e.caused_by is None
        assert e.data == {"key": "val"}


class TestTelegramFormatting:
    """Test format_trace_summary."""

    def test_format_trace_summary_empty(self):
        from nous.telegram_bot import format_trace_summary

        result = format_trace_summary({"events": []})
        assert "No events found" in result

    def test_format_trace_summary_with_events(self):
        from nous.telegram_bot import format_trace_summary

        trace_data = {
            "trace_id": "abc123def456",
            "root_event": "session_ended",
            "depth": 3,
            "events": [
                {"type": "session_ended", "caused_by": None, "data": {}},
                {"type": "episode_summarized", "caused_by": "abc123def456", "data": {}},
                {"type": "sleep_completed", "caused_by": "xyz", "data": {"modifies": "memory"}},
            ],
        }
        result = format_trace_summary(trace_data)
        assert "Trace: abc123def456" in result
        assert "Root: session_ended" in result
        assert "Depth: 3" in result
        assert "session_ended" in result
        assert "[MOD]" in result
