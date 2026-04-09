"""Tests for F035.4: Context visibility — ContextLogger, section parser, payload store."""

from nous.observability.context_logger import (
    ContextLogEntry,
    ContextLogger,
    FullPayloadStore,
    parse_system_sections,
)

# ------------------------------------------------------------------
# parse_system_sections
# ------------------------------------------------------------------


class TestParseSystemSections:
    def test_basic_sections(self):
        prompt = (
            "Some preamble text\n"
            "## Identity\n"
            "I am Nous\n"
            "## Current Frame\n"
            "conversation\n"
            "## Relevant Facts\n"
            "- fact one\n"
            "- fact two"
        )
        sections = parse_system_sections(prompt)
        assert "identity" in sections
        assert "I am Nous" in sections["identity"]
        assert "frame" in sections
        assert "conversation" in sections["frame"]
        assert "relevant_facts" in sections
        assert "fact one" in sections["relevant_facts"]
        # Preamble becomes "other"
        assert "other" in sections
        assert "Some preamble" in sections["other"]

    def test_unknown_goes_to_other(self):
        prompt = "Just some text with no known markers"
        sections = parse_system_sections(prompt)
        assert "other" in sections
        assert "Just some text" in sections["other"]

    def test_empty_prompt(self):
        assert parse_system_sections("") == {}
        assert parse_system_sections(None) == {}

    def test_bracket_markers(self):
        prompt = (
            "## Identity\n"
            "I am Nous\n"
            "[Execution Ledger]\n"
            "Turn 1: action done\n"
            "[Previous Turn Corrections]\n"
            "Fix: something"
        )
        sections = parse_system_sections(prompt)
        assert "execution_ledger" in sections
        assert "Turn 1" in sections["execution_ledger"]
        assert "corrections" in sections
        assert "Fix: something" in sections["corrections"]

    def test_empty_sections_excluded(self):
        prompt = "## Identity\n\n## Current Frame\nconversation"
        sections = parse_system_sections(prompt)
        # Identity section has no content, should be excluded
        assert "identity" not in sections
        assert "frame" in sections


# ------------------------------------------------------------------
# ContextLogEntry.from_payload
# ------------------------------------------------------------------


class TestContextLogEntry:
    def test_token_estimation(self):
        system = "## Identity\nI am a test agent with some identity text here"
        messages = [
            {"role": "user", "content": "Hello world"},
            {"role": "assistant", "content": "Hi there"},
        ]
        tools = [{"name": "recall_deep", "input_schema": {"type": "object"}}]
        entry = ContextLogEntry.from_payload(
            session_id="s1",
            turn_number=1,
            call_type="chat",
            model="claude-sonnet-4-6",
            system_prompt=system,
            messages=messages,
            tools=tools,
            frame_id="conversation",
            context_window=200000,
        )
        assert entry.total_tokens_est > 0
        assert entry.context_window_size == 200000
        assert entry.utilization_pct > 0
        assert "identity" in entry.token_breakdown
        assert "tools_definition" in entry.token_breakdown
        assert "messages" in entry.token_breakdown

    def test_message_role_counts(self):
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"},
            {"role": "user", "content": "Bye"},
        ]
        entry = ContextLogEntry.from_payload(
            session_id="s1",
            turn_number=1,
            call_type="chat",
            model="test",
            system_prompt="## Identity\ntest",
            messages=messages,
            tools=None,
            frame_id="conv",
            context_window=200000,
        )
        assert entry.message_roles == {"user": 2, "assistant": 1}
        assert entry.messages_count == 3

    def test_tool_counting(self):
        tools = [
            {"name": "recall_deep", "input_schema": {}},
            {"name": "learn_fact", "input_schema": {}},
            {"name": "bash", "input_schema": {}},
        ]
        entry = ContextLogEntry.from_payload(
            session_id="s1",
            turn_number=1,
            call_type="chat",
            model="test",
            system_prompt="test",
            messages=[],
            tools=tools,
            frame_id="conv",
            context_window=200000,
        )
        assert entry.tools_count == 3
        assert entry.tool_names == ["recall_deep", "learn_fact", "bash"]

    def test_to_dict_roundtrip(self):
        entry = ContextLogEntry.from_payload(
            session_id="s1",
            turn_number=1,
            call_type="chat",
            model="test",
            system_prompt="## Identity\ntest",
            messages=[{"role": "user", "content": "hi"}],
            tools=None,
            frame_id="conv",
            context_window=200000,
        )
        d = entry.to_dict()
        assert d["session_id"] == "s1"
        assert d["turn_number"] == 1
        assert d["model"] == "test"
        assert isinstance(d["token_breakdown"], dict)

    def test_no_tools_handled(self):
        entry = ContextLogEntry.from_payload(
            session_id="s1",
            turn_number=1,
            call_type="chat",
            model="test",
            system_prompt="test",
            messages=[],
            tools=None,
            frame_id="conv",
            context_window=200000,
        )
        assert entry.tools_count == 0
        assert entry.tool_names == []


# ------------------------------------------------------------------
# FullPayloadStore
# ------------------------------------------------------------------


class TestFullPayloadStore:
    def test_store_and_retrieve(self):
        store = FullPayloadStore(max_per_session=5, max_total=20)
        store.capture("s1", "e1", {"data": "payload1"})
        assert store.get("e1") == {"data": "payload1"}
        assert store.get("nonexistent") is None

    def test_ring_buffer_eviction(self):
        store = FullPayloadStore(max_per_session=2, max_total=20)
        store.capture("s1", "e1", {"v": 1})
        store.capture("s1", "e2", {"v": 2})
        store.capture("s1", "e3", {"v": 3})
        # e1 should be evicted (ring size 2)
        assert store.get("e1") is None
        assert store.get("e2") == {"v": 2}
        assert store.get("e3") == {"v": 3}

    def test_session_retrieval(self):
        store = FullPayloadStore(max_per_session=5, max_total=20)
        store.capture("s1", "e1", {"v": 1})
        store.capture("s1", "e2", {"v": 2})
        store.capture("s2", "e3", {"v": 3})
        s1_entries = store.get_session("s1")
        assert len(s1_entries) == 2
        # Most recent first
        assert s1_entries[0].entry_id == "e2"
        assert s1_entries[1].entry_id == "e1"
        assert store.get_session("nonexistent") == []

    def test_global_cap(self):
        store = FullPayloadStore(max_per_session=5, max_total=3)
        store.capture("s1", "e1", {"v": 1})
        store.capture("s2", "e2", {"v": 2})
        store.capture("s3", "e3", {"v": 3})
        store.capture("s4", "e4", {"v": 4})
        # e1 (oldest globally) should be evicted
        assert store.get("e1") is None
        assert store._total_count == 3


# ------------------------------------------------------------------
# ContextLogger — memory leak prevention
# ------------------------------------------------------------------


class TestContextLogger:
    def test_entries_by_id_bounded(self):
        """Verify _entries_by_id doesn't grow beyond deque maxlen."""
        ctx_logger = ContextLogger()
        # Log more entries than deque maxlen (200)
        for i in range(210):
            ctx_logger.log(
                session_id="s1",
                turn_number=i,
                call_type="chat",
                model="test",
                system_prompt="test",
                messages=[],
                tools=None,
                frame_id="conv",
                context_window=200000,
            )
        # Deque has maxlen=200, so entries_by_id should be synced
        assert len(ctx_logger._entries) == 200
        # Index should not grow unbounded (synced when gap > 10)
        assert len(ctx_logger._entries_by_id) <= 210

    def test_get_recent_with_session_filter(self):
        ctx_logger = ContextLogger()
        ctx_logger.log(
            session_id="s1",
            turn_number=1,
            call_type="chat",
            model="test",
            system_prompt="test",
            messages=[],
            tools=None,
            frame_id="conv",
            context_window=200000,
        )
        ctx_logger.log(
            session_id="s2",
            turn_number=1,
            call_type="chat",
            model="test",
            system_prompt="test",
            messages=[],
            tools=None,
            frame_id="conv",
            context_window=200000,
        )
        assert len(ctx_logger.get_recent()) == 2
        assert len(ctx_logger.get_recent(session_id="s1")) == 1

    def test_update_response(self):
        ctx_logger = ContextLogger()
        entry = ctx_logger.log(
            session_id="s1",
            turn_number=1,
            call_type="chat",
            model="test",
            system_prompt="test",
            messages=[],
            tools=None,
            frame_id="conv",
            context_window=200000,
        )
        ctx_logger.update_response(
            entry.id,
            input_tokens=500,
            output_tokens=200,
            cache_creation=100,
            cache_read=50,
            duration_ms=1234.5,
            stop_reason="end_turn",
        )
        updated = ctx_logger.get_entry(entry.id)
        assert updated.input_tokens_actual == 500
        assert updated.output_tokens == 200
        assert updated.duration_ms == 1234.5
        assert updated.stop_reason == "end_turn"

    def test_payload_store_disabled_by_default(self):
        ctx_logger = ContextLogger()
        entry = ctx_logger.log(
            session_id="s1",
            turn_number=1,
            call_type="chat",
            model="test",
            system_prompt="test",
            messages=[],
            tools=None,
            frame_id="conv",
            context_window=200000,
            payload={"some": "data"},
        )
        assert ctx_logger.get_payload(entry.id) is None

    def test_payload_store_enabled(self):
        ctx_logger = ContextLogger(full_payload_enabled=True)
        entry = ctx_logger.log(
            session_id="s1",
            turn_number=1,
            call_type="chat",
            model="test",
            system_prompt="test",
            messages=[],
            tools=None,
            frame_id="conv",
            context_window=200000,
            payload={"some": "data"},
        )
        assert ctx_logger.get_payload(entry.id) == {"some": "data"}
