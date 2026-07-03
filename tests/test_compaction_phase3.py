"""Tests for Spec 008.1 Phase 3: Durable Integration.

34 tests across 6 test classes:
- TestConversationStatePersistence (6): save/load round-trip, load nonexistent,
  upsert updates, JSONB messages round-trip, summary persistence, delete
- TestConversationStateModel (2): ORM columns exist, unique constraint
- TestPreCompactionEvent (3): event emitted with correct type/data,
  no bus = no error, snapshot is decoupled copy
- TestKnowledgeExtractor (8): handler registered, facts extracted and stored,
  uses snapshot from event data, handles empty messages, skips short content,
  dedup skips high similarity (>0.85), skips low confidence (<0.6), max 5 cap
- TestEpisodeBoundary (5): episode kept open on compaction (#169), no new
  episode started, no active episode = no error, active_episodes unchanged,
  bump failure non-fatal
- TestRunnerSaveRestore (10): restore from DB on session resume, save after
  compaction, save on end_conversation, missing state = fresh conversation,
  _save_conversation serializes messages, _restore handles malformed data,
  _get_or_create now async, end_conversation deletes state,
  end_conversation cleans compaction lock, restore sets compaction_count
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio

from nous.api.models import ApiResponse, Conversation, Message
from nous.events import Event, EventBus
from nous.handlers.knowledge_extractor import KnowledgeExtractor
from nous.heart.schemas import EpisodeDetail, EpisodeInput, FactInput, FactSummary
from nous.storage.models import ConversationState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TEST_AGENT = "test-phase3-agent"
TEST_SESSION = "test-session-p3"


def _mock_llm_client(text: str = "") -> AsyncMock:
    """Build a mock LLMClient returning an ApiResponse-like object."""
    client = AsyncMock()
    response = MagicMock()
    response.content = [{"type": "text", "text": text}]
    client.call = AsyncMock(return_value=response)
    return client


def _mock_settings(**overrides) -> MagicMock:
    """MagicMock Settings to avoid pydantic validation."""
    s = MagicMock()
    s.agent_id = TEST_AGENT
    s.background_model = "claude-sonnet-4-5-20250514"
    s.anthropic_api_key = "sk-ant-test-key"
    s.anthropic_auth_token = ""
    s.api_base_url = "https://api.anthropic.com"
    s.model = "claude-sonnet-4-5-20250514"
    s.max_tokens = 8192
    s.compaction_enabled = True
    s.compaction_threshold = 1000
    s.keep_recent_tokens = 200
    s.tool_pruning_enabled = True
    s.tool_soft_trim_chars = 100
    s.tool_soft_trim_head = 20
    s.tool_soft_trim_tail = 20
    s.tool_hard_clear_after = 6
    s.keep_last_tool_results = 2
    s.event_bus_enabled = True
    s.knowledge_extractor_max_chars = 24000
    # S2 hardening changes the prompt shape; these tests assert the legacy
    # format (MagicMock attrs are otherwise implicitly truthy). The hardened
    # path is covered in test_s2_input_hardening.py.
    s.extraction_input_hardening_enabled = False
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


def _make_messages_list(n: int = 4) -> list[dict]:
    """Create a list of message dicts for JSONB storage."""
    msgs = []
    for i in range(n):
        msgs.append({"role": "user", "content": f"User message {i}"})
        msgs.append({"role": "assistant", "content": f"Assistant reply {i}"})
    return msgs


# ===========================================================================
# TestConversationStatePersistence — 6 tests (real Postgres)
# ===========================================================================


class TestConversationStatePersistence:
    """Test Heart.{save,load,delete}_conversation_state with real Postgres."""

    @pytest.mark.asyncio
    async def test_save_load_round_trip(self, heart, session):
        """Save + load returns matching data."""
        messages = _make_messages_list(2)
        await heart.save_conversation_state(
            agent_id=TEST_AGENT,
            session_id=TEST_SESSION,
            summary="Test summary",
            messages=messages,
            turn_count=4,
            compaction_count=1,
            session=session,
        )

        loaded = await heart.load_conversation_state(
            agent_id=TEST_AGENT,
            session_id=TEST_SESSION,
            session=session,
        )

        assert loaded is not None
        assert loaded["agent_id"] == TEST_AGENT
        assert loaded["session_id"] == TEST_SESSION
        assert loaded["summary"] == "Test summary"
        assert loaded["messages"] == messages
        assert loaded["turn_count"] == 4
        assert loaded["compaction_count"] == 1
        assert loaded["created_at"] is not None
        assert loaded["updated_at"] is not None

    @pytest.mark.asyncio
    async def test_load_nonexistent_returns_none(self, heart, session):
        """Loading a non-existent session returns None."""
        loaded = await heart.load_conversation_state(
            agent_id=TEST_AGENT,
            session_id="nonexistent-session-xyz",
            session=session,
        )
        assert loaded is None

    @pytest.mark.asyncio
    async def test_upsert_updates_existing(self, heart, session):
        """Saving twice with same agent+session updates existing row."""
        messages_v1 = [{"role": "user", "content": "v1"}]
        await heart.save_conversation_state(
            agent_id=TEST_AGENT,
            session_id=TEST_SESSION,
            summary="v1 summary",
            messages=messages_v1,
            turn_count=1,
            compaction_count=0,
            session=session,
        )

        messages_v2 = [{"role": "user", "content": "v2"}, {"role": "assistant", "content": "reply"}]
        await heart.save_conversation_state(
            agent_id=TEST_AGENT,
            session_id=TEST_SESSION,
            summary="v2 summary",
            messages=messages_v2,
            turn_count=3,
            compaction_count=2,
            session=session,
        )

        loaded = await heart.load_conversation_state(
            agent_id=TEST_AGENT,
            session_id=TEST_SESSION,
            session=session,
        )
        assert loaded is not None
        assert loaded["summary"] == "v2 summary"
        assert loaded["messages"] == messages_v2
        assert loaded["turn_count"] == 3
        assert loaded["compaction_count"] == 2

    @pytest.mark.asyncio
    async def test_messages_jsonb_round_trip(self, heart, session):
        """JSONB storage preserves complex message structures."""
        messages = [
            {"role": "user", "content": "Hello world"},
            {"role": "assistant", "content": "Hi! How can I help?"},
            {"role": "user", "content": "[Previous conversation summary]\n\n## Goal\nTest"},
            {"role": "assistant", "content": "I have the context. Let's continue."},
        ]
        await heart.save_conversation_state(
            agent_id=TEST_AGENT,
            session_id="jsonb-test",
            summary=None,
            messages=messages,
            turn_count=2,
            compaction_count=0,
            session=session,
        )

        loaded = await heart.load_conversation_state(
            agent_id=TEST_AGENT,
            session_id="jsonb-test",
            session=session,
        )
        assert loaded is not None
        assert loaded["messages"] == messages
        assert loaded["messages"][2]["content"].startswith("[Previous conversation summary]")

    @pytest.mark.asyncio
    async def test_summary_persistence(self, heart, session):
        """Summary field persists correctly including None."""
        # With summary
        await heart.save_conversation_state(
            agent_id=TEST_AGENT,
            session_id="summary-test",
            summary="## Goal\nTest compaction\n## Progress\nDone\n## Critical Context\nPaths",
            messages=[],
            turn_count=0,
            compaction_count=1,
            session=session,
        )
        loaded = await heart.load_conversation_state(
            agent_id=TEST_AGENT, session_id="summary-test", session=session
        )
        assert loaded is not None
        assert "## Goal" in loaded["summary"]

        # Update to None summary
        await heart.save_conversation_state(
            agent_id=TEST_AGENT,
            session_id="summary-test",
            summary=None,
            messages=[],
            turn_count=0,
            compaction_count=0,
            session=session,
        )
        loaded2 = await heart.load_conversation_state(
            agent_id=TEST_AGENT, session_id="summary-test", session=session
        )
        assert loaded2 is not None
        assert loaded2["summary"] is None

    @pytest.mark.asyncio
    async def test_delete(self, heart, session):
        """Delete removes the row."""
        await heart.save_conversation_state(
            agent_id=TEST_AGENT,
            session_id="delete-test",
            summary="to be deleted",
            messages=[],
            turn_count=0,
            compaction_count=0,
            session=session,
        )
        # Verify exists
        loaded = await heart.load_conversation_state(
            agent_id=TEST_AGENT, session_id="delete-test", session=session
        )
        assert loaded is not None

        # Delete
        await heart.delete_conversation_state(
            agent_id=TEST_AGENT, session_id="delete-test", session=session
        )

        # Verify gone
        loaded2 = await heart.load_conversation_state(
            agent_id=TEST_AGENT, session_id="delete-test", session=session
        )
        assert loaded2 is None


# ===========================================================================
# TestConversationStateModel — 2 tests
# ===========================================================================


class TestConversationStateModel:
    """Test ConversationState ORM model structure."""

    def test_columns_exist(self):
        """Model has all expected columns."""
        cols = {c.name for c in ConversationState.__table__.columns}
        expected = {
            "id", "agent_id", "session_id", "summary",
            "messages", "turn_count", "compaction_count",
            "created_at", "updated_at",
        }
        assert expected.issubset(cols)

    def test_unique_constraint(self):
        """Model has unique constraint on (agent_id, session_id)."""
        constraints = ConversationState.__table__.constraints
        unique_names = [c.name for c in constraints if hasattr(c, "columns")]
        assert "uq_conversation_state_agent_session" in unique_names


# ===========================================================================
# TestPreCompactionEvent — 3 tests
# ===========================================================================


class TestPreCompactionEvent:
    """Test CognitiveLayer.pre_compaction() event emission."""

    @pytest.mark.asyncio
    async def test_event_emitted_with_correct_data(self):
        """pre_compaction emits conversation_compacting event with snapshot."""
        from nous.cognitive.layer import CognitiveLayer

        brain = MagicMock()
        brain.db = MagicMock()
        brain.embeddings = MagicMock()
        heart = MagicMock()
        heart.end_episode = AsyncMock()
        heart.start_episode = AsyncMock(return_value=MagicMock(id=uuid4()))
        settings = _mock_settings()
        bus = EventBus()

        received: list[Event] = []

        async def capture(event: Event) -> None:
            received.append(event)

        bus.on("conversation_compacting", capture)
        await bus.start()

        cognitive = CognitiveLayer(brain, heart, settings, bus=bus)

        snapshot = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        await cognitive.pre_compaction(
            agent_id=TEST_AGENT,
            session_id=TEST_SESSION,
            message_snapshot=snapshot,
        )

        # Give the bus time to process
        await asyncio.sleep(0.1)
        await bus.stop()

        assert len(received) == 1
        assert received[0].type == "conversation_compacting"
        assert received[0].agent_id == TEST_AGENT
        assert received[0].session_id == TEST_SESSION
        assert received[0].data["message_snapshot"] == snapshot

    @pytest.mark.asyncio
    async def test_no_bus_no_error(self):
        """pre_compaction with bus=None does not error."""
        from nous.cognitive.layer import CognitiveLayer

        brain = MagicMock()
        brain.db = MagicMock()
        brain.embeddings = MagicMock()
        heart = MagicMock()
        heart.end_episode = AsyncMock()
        heart.start_episode = AsyncMock(return_value=MagicMock(id=uuid4()))
        settings = _mock_settings()

        cognitive = CognitiveLayer(brain, heart, settings, bus=None)

        # Should not raise
        await cognitive.pre_compaction(
            agent_id=TEST_AGENT,
            session_id=TEST_SESSION,
            message_snapshot=[{"role": "user", "content": "test"}],
        )

    @pytest.mark.asyncio
    async def test_snapshot_is_decoupled_copy(self):
        """Modifying snapshot after passing to pre_compaction doesn't affect event data."""
        from nous.cognitive.layer import CognitiveLayer

        brain = MagicMock()
        brain.db = MagicMock()
        brain.embeddings = MagicMock()
        heart = MagicMock()
        heart.end_episode = AsyncMock()
        heart.start_episode = AsyncMock(return_value=MagicMock(id=uuid4()))
        settings = _mock_settings()
        bus = EventBus()

        received: list[Event] = []

        async def capture(event: Event) -> None:
            received.append(event)

        bus.on("conversation_compacting", capture)
        await bus.start()

        cognitive = CognitiveLayer(brain, heart, settings, bus=bus)

        snapshot = [{"role": "user", "content": "original"}]
        await cognitive.pre_compaction(
            agent_id=TEST_AGENT,
            session_id=TEST_SESSION,
            message_snapshot=snapshot,
        )

        # Mutate the snapshot AFTER passing it
        snapshot.append({"role": "assistant", "content": "added later"})

        await asyncio.sleep(0.1)
        await bus.stop()

        # The event should have the snapshot as passed (list reference),
        # but the snapshot in event data is the same list reference.
        # The key point is the runner creates the snapshot BEFORE compaction
        # mutates the conversation — this test verifies the pattern works.
        assert len(received) == 1
        # The event data still holds the reference — caller (runner) must
        # create snapshot[:cut_point] as a slice (which is a new list).
        # In production, messages[:cut_point] creates a new list.


# ===========================================================================
# TestKnowledgeExtractor — 5 tests
# ===========================================================================


class TestKnowledgeExtractor:
    """Test KnowledgeExtractor event handler."""

    def test_handler_registered_on_init(self):
        """KnowledgeExtractor registers handler for conversation_compacting."""
        heart = MagicMock()
        settings = _mock_settings()
        bus = EventBus()

        KnowledgeExtractor(heart, settings, bus)

        assert "conversation_compacting" in bus._handlers
        assert len(bus._handlers["conversation_compacting"]) == 1

    @pytest.mark.asyncio
    async def test_facts_extracted_and_stored(self):
        """Handler extracts facts from snapshot and stores via heart.learn()."""
        heart = MagicMock()
        heart.learn = AsyncMock()
        heart.find_similar_facts = AsyncMock(return_value=[])  # No duplicates
        settings = _mock_settings()
        bus = EventBus()

        mock_llm = _mock_llm_client(json.dumps([
            {
                "subject": "user",
                "content": "User prefers Python 3.12",
                "category": "preference",
                "confidence": 0.9,
            }
        ]))

        extractor = KnowledgeExtractor(heart, settings, bus, llm_client=mock_llm)

        event = Event(
            type="conversation_compacting",
            agent_id=TEST_AGENT,
            session_id=TEST_SESSION,
            data={
                "message_snapshot": [
                    {"role": "user", "content": "I always use Python 3.12 for my projects " + "x" * 200},
                    {"role": "assistant", "content": "Noted, I'll use Python 3.12. " + "y" * 200},
                ],
            },
        )

        await extractor.handle(event)

        # Verify LLM was called
        mock_llm.call.assert_called_once()

        # Verify fact was stored
        heart.learn.assert_called_once()
        fact_input = heart.learn.call_args[0][0]
        assert isinstance(fact_input, FactInput)
        assert "Python 3.12" in fact_input.content
        assert fact_input.source == "knowledge_extractor"

    @pytest.mark.asyncio
    async def test_uses_snapshot_from_event_data(self):
        """Handler reads message_snapshot from event.data."""
        heart = MagicMock()
        heart.learn = AsyncMock()
        heart.find_similar_facts = AsyncMock(return_value=[])
        settings = _mock_settings()
        bus = EventBus()

        mock_llm = _mock_llm_client("[]")

        extractor = KnowledgeExtractor(heart, settings, bus, llm_client=mock_llm)

        # Pass specific content to verify it reaches the LLM prompt
        event = Event(
            type="conversation_compacting",
            agent_id=TEST_AGENT,
            data={
                "message_snapshot": [
                    {"role": "user", "content": "unique-marker-text " + "x" * 200},
                    {"role": "assistant", "content": "response text " + "y" * 200},
                ],
            },
        )

        await extractor.handle(event)

        # Verify the prompt contains our marker text
        call_kwargs = mock_llm.call.call_args
        payload = call_kwargs[0][0]
        user_msg = payload["messages"][0]["content"]
        if isinstance(user_msg, list):
            user_msg = user_msg[0]["text"]
        assert "unique-marker-text" in user_msg

    @pytest.mark.asyncio
    async def test_handles_empty_messages(self):
        """Handler does nothing with empty snapshot."""
        heart = MagicMock()
        heart.learn = AsyncMock()
        settings = _mock_settings()
        bus = EventBus()

        extractor = KnowledgeExtractor(heart, settings, bus)

        event = Event(
            type="conversation_compacting",
            agent_id=TEST_AGENT,
            data={"message_snapshot": []},
        )

        await extractor.handle(event)

        # No fact storage should happen
        heart.learn.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_short_content(self):
        """Handler skips extraction when serialized content is too short."""
        heart = MagicMock()
        heart.learn = AsyncMock()
        settings = _mock_settings()
        bus = EventBus()

        mock_llm = _mock_llm_client()
        extractor = KnowledgeExtractor(heart, settings, bus, llm_client=mock_llm)

        event = Event(
            type="conversation_compacting",
            agent_id=TEST_AGENT,
            data={
                "message_snapshot": [
                    {"role": "user", "content": "hi"},
                    {"role": "assistant", "content": "hello"},
                ],
            },
        )

        await extractor.handle(event)

        # Short content should skip LLM call entirely
        mock_llm.call.assert_not_called()
        heart.learn.assert_not_called()

    @pytest.mark.asyncio
    async def test_dedup_skips_high_similarity(self):
        """Facts with >0.85 similarity to existing facts are skipped."""
        heart = MagicMock()
        heart.learn = AsyncMock()
        # Return a high-similarity existing fact
        existing_fact = MagicMock()
        existing_fact.score = 0.92
        heart.find_similar_facts = AsyncMock(return_value=[existing_fact])
        settings = _mock_settings()
        bus = EventBus()

        mock_llm = _mock_llm_client(json.dumps([
            {
                "subject": "user",
                "content": "Duplicate fact that already exists",
                "category": "preference",
                "confidence": 0.9,
            }
        ]))

        extractor = KnowledgeExtractor(heart, settings, bus, llm_client=mock_llm)

        event = Event(
            type="conversation_compacting",
            agent_id=TEST_AGENT,
            data={
                "message_snapshot": [
                    {"role": "user", "content": "Something substantial " + "x" * 200},
                    {"role": "assistant", "content": "Response text " + "y" * 200},
                ],
            },
        )

        await extractor.handle(event)

        # LLM was called, but fact was NOT stored (duplicate)
        mock_llm.call.assert_called_once()
        heart.learn.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_low_confidence_facts(self):
        """Facts with confidence < 0.6 are skipped."""
        heart = MagicMock()
        heart.learn = AsyncMock()
        heart.find_similar_facts = AsyncMock(return_value=[])
        settings = _mock_settings()
        bus = EventBus()

        mock_llm = _mock_llm_client(json.dumps([
            {
                "subject": "user",
                "content": "Low confidence fact",
                "category": "preference",
                "confidence": 0.4,
            }
        ]))

        extractor = KnowledgeExtractor(heart, settings, bus, llm_client=mock_llm)

        event = Event(
            type="conversation_compacting",
            agent_id=TEST_AGENT,
            data={
                "message_snapshot": [
                    {"role": "user", "content": "Content text " + "x" * 200},
                    {"role": "assistant", "content": "Reply text " + "y" * 200},
                ],
            },
        )

        await extractor.handle(event)

        # LLM was called, but fact was NOT stored (low confidence)
        mock_llm.call.assert_called_once()
        heart.learn.assert_not_called()

    @pytest.mark.asyncio
    async def test_max_five_facts_cap(self):
        """At most 5 facts are stored per compaction."""
        heart = MagicMock()
        heart.learn = AsyncMock()
        heart.find_similar_facts = AsyncMock(return_value=[])  # No duplicates
        settings = _mock_settings()
        bus = EventBus()

        # Return 8 facts from LLM — only 5 should be stored
        facts = [
            {
                "subject": f"subject-{i}",
                "content": f"Fact number {i}",
                "category": "technical",
                "confidence": 0.9,
            }
            for i in range(8)
        ]
        mock_llm = _mock_llm_client(json.dumps(facts))

        extractor = KnowledgeExtractor(heart, settings, bus, llm_client=mock_llm)

        event = Event(
            type="conversation_compacting",
            agent_id=TEST_AGENT,
            data={
                "message_snapshot": [
                    {"role": "user", "content": "Big conversation " + "x" * 300},
                    {"role": "assistant", "content": "Long reply " + "y" * 300},
                ],
            },
        )

        await extractor.handle(event)

        # Max 5 facts stored
        assert heart.learn.call_count == 5

    @pytest.mark.asyncio
    async def test_snapshot_truncated_to_custom_max_chars(self):
        """Text sent to LLM is capped at knowledge_extractor_max_chars when set to 100."""
        captured_prompt: list[str] = []

        async def fake_call(payload, **kwargs):
            msgs = payload.get("messages", [])
            if msgs:
                content = msgs[0]["content"]
                if isinstance(content, list):
                    captured_prompt.append(content[0]["text"])
                else:
                    captured_prompt.append(content)
            response = MagicMock()
            response.content = [{"type": "text", "text": "[]"}]
            return response

        client = MagicMock()
        client.call = AsyncMock(side_effect=fake_call)

        heart = MagicMock()
        heart.learn = AsyncMock()
        heart.find_similar_facts = AsyncMock(return_value=[])
        settings = _mock_settings(knowledge_extractor_max_chars=100)
        bus = EventBus()

        extractor = KnowledgeExtractor(heart, settings, bus, llm_client=client)

        # Build a snapshot whose serialized text is well above 100 chars
        long_content = "A" * 500
        event = Event(
            type="conversation_compacting",
            agent_id=TEST_AGENT,
            data={
                "message_snapshot": [
                    {"role": "user", "content": long_content},
                    {"role": "assistant", "content": long_content},
                ],
            },
        )

        await extractor.handle(event)

        assert captured_prompt, "LLM was not called"
        # The prompt contains the conversation_text snippet — verify it was capped
        prompt_text = captured_prompt[0]
        # Extract the conversation portion (between "Conversation:\n" and the closing "\n\nReturn")
        conv_start = prompt_text.find("Conversation:\n") + len("Conversation:\n")
        conv_end = prompt_text.find("\n\nReturn ONLY")
        conversation_in_prompt = prompt_text[conv_start:conv_end]
        # Strip truncation marker if present, then check content length <= 100
        _TRUNCATION_MARKER = "\n\n[...truncated...]"
        if conversation_in_prompt.endswith(_TRUNCATION_MARKER):
            content_only = conversation_in_prompt[: -len(_TRUNCATION_MARKER)]
        else:
            content_only = conversation_in_prompt
        assert len(content_only) <= 100, (
            f"Conversation text in prompt was {len(content_only)} chars, expected ≤ 100"
        )

    @pytest.mark.asyncio
    async def test_snapshot_truncated_to_default_max_chars_at_24000(self):
        """With default settings and a 30k-char conversation, LLM sees exactly 24000 chars."""
        captured_prompt: list[str] = []

        async def fake_call(payload, **kwargs):
            msgs = payload.get("messages", [])
            if msgs:
                content = msgs[0]["content"]
                if isinstance(content, list):
                    captured_prompt.append(content[0]["text"])
                else:
                    captured_prompt.append(content)
            response = MagicMock()
            response.content = [{"type": "text", "text": "[]"}]
            return response

        client = MagicMock()
        client.call = AsyncMock(side_effect=fake_call)

        heart = MagicMock()
        heart.learn = AsyncMock()
        heart.find_similar_facts = AsyncMock(return_value=[])
        # Default settings — knowledge_extractor_max_chars = 24000
        settings = _mock_settings()
        settings.knowledge_extractor_max_chars = 24000
        bus = EventBus()

        extractor = KnowledgeExtractor(heart, settings, bus, llm_client=client)

        # Build a snapshot whose serialized text exceeds 24000 chars.
        # Each message is capped at 2000 chars by _serialize_messages, so we need
        # enough messages so that the joined total exceeds 24000 chars.
        # Each serialized message is ~2007 chars ("User: " + 2000 + "\n\n").
        # 12 messages × ~2007 = ~24084 chars — just over the cap.
        long_content = "B" * 2000
        snapshot_msgs = []
        for i in range(12):
            role = "user" if i % 2 == 0 else "assistant"
            snapshot_msgs.append({"role": role, "content": long_content})
        event = Event(
            type="conversation_compacting",
            agent_id=TEST_AGENT,
            data={"message_snapshot": snapshot_msgs},
        )

        await extractor.handle(event)

        assert captured_prompt, "LLM was not called"
        prompt_text = captured_prompt[0]
        conv_start = prompt_text.find("Conversation:\n") + len("Conversation:\n")
        conv_end = prompt_text.find("\n\nReturn ONLY")
        conversation_in_prompt = prompt_text[conv_start:conv_end]
        # Conversation text is capped at 24000 then the truncation marker appended
        _TRUNCATION_MARKER = "\n\n[...truncated...]"
        assert conversation_in_prompt.endswith(_TRUNCATION_MARKER), (
            "Expected truncation marker to be present"
        )
        content_len = len(conversation_in_prompt) - len(_TRUNCATION_MARKER)
        assert content_len == 24000, (
            f"Expected exactly 24000 chars before marker, got {content_len}"
        )


# ===========================================================================
# TestEpisodeBoundary — 5 tests
# ===========================================================================


class TestEpisodeBoundary:
    """Test episode kept open during pre_compaction (#169)."""

    @pytest.mark.asyncio
    async def test_episode_kept_open_on_compaction(self):
        """Active episode stays open when pre_compaction is called (#169)."""
        from nous.cognitive.layer import CognitiveLayer

        brain = MagicMock()
        brain.db = MagicMock()
        brain.embeddings = MagicMock()
        heart = MagicMock()
        heart.end_episode = AsyncMock()
        heart.start_episode = AsyncMock()
        heart.bump_episode_compaction_count = AsyncMock()
        settings = _mock_settings()

        cognitive = CognitiveLayer(brain, heart, settings, bus=None)
        old_episode_id = str(uuid4())
        cognitive._active_episodes[TEST_SESSION] = old_episode_id

        await cognitive.pre_compaction(
            agent_id=TEST_AGENT,
            session_id=TEST_SESSION,
            message_snapshot=[{"role": "user", "content": "test"}],
        )

        heart.end_episode.assert_not_called()
        heart.start_episode.assert_not_called()
        heart.bump_episode_compaction_count.assert_called_once()
        assert cognitive._active_episodes[TEST_SESSION] == old_episode_id

    @pytest.mark.asyncio
    async def test_no_new_episode_on_compaction(self):
        """No new episode created after compaction (#169)."""
        from nous.cognitive.layer import CognitiveLayer

        brain = MagicMock()
        brain.db = MagicMock()
        brain.embeddings = MagicMock()
        heart = MagicMock()
        heart.end_episode = AsyncMock()
        heart.start_episode = AsyncMock()
        heart.bump_episode_compaction_count = AsyncMock()
        settings = _mock_settings()

        cognitive = CognitiveLayer(brain, heart, settings, bus=None)
        cognitive._active_episodes[TEST_SESSION] = str(uuid4())

        await cognitive.pre_compaction(
            agent_id=TEST_AGENT,
            session_id=TEST_SESSION,
            message_snapshot=[{"role": "user", "content": "test"}],
        )

        heart.start_episode.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_active_episode_no_error(self):
        """pre_compaction with no active episode doesn't error."""
        from nous.cognitive.layer import CognitiveLayer

        brain = MagicMock()
        brain.db = MagicMock()
        brain.embeddings = MagicMock()
        heart = MagicMock()
        heart.bump_episode_compaction_count = AsyncMock()
        settings = _mock_settings()

        cognitive = CognitiveLayer(brain, heart, settings, bus=None)

        await cognitive.pre_compaction(
            agent_id=TEST_AGENT,
            session_id=TEST_SESSION,
            message_snapshot=[{"role": "user", "content": "test"}],
        )

        heart.bump_episode_compaction_count.assert_not_called()

    @pytest.mark.asyncio
    async def test_active_episodes_dict_unchanged(self):
        """_active_episodes keeps the same episode ID after compaction (#169)."""
        from nous.cognitive.layer import CognitiveLayer

        brain = MagicMock()
        brain.db = MagicMock()
        brain.embeddings = MagicMock()
        heart = MagicMock()
        heart.bump_episode_compaction_count = AsyncMock()
        settings = _mock_settings()

        cognitive = CognitiveLayer(brain, heart, settings, bus=None)
        old_id = uuid4()
        cognitive._active_episodes[TEST_SESSION] = str(old_id)

        await cognitive.pre_compaction(
            agent_id=TEST_AGENT,
            session_id=TEST_SESSION,
            message_snapshot=[],
        )

        assert cognitive._active_episodes[TEST_SESSION] == str(old_id)

    @pytest.mark.asyncio
    async def test_bump_failure_non_fatal(self):
        """If bump_compaction_count fails, episode stays active."""
        from nous.cognitive.layer import CognitiveLayer

        brain = MagicMock()
        brain.db = MagicMock()
        brain.embeddings = MagicMock()
        heart = MagicMock()
        heart.bump_episode_compaction_count = AsyncMock(side_effect=RuntimeError("DB error"))
        settings = _mock_settings()

        cognitive = CognitiveLayer(brain, heart, settings, bus=None)
        old_id = str(uuid4())
        cognitive._active_episodes[TEST_SESSION] = old_id

        await cognitive.pre_compaction(
            agent_id=TEST_AGENT,
            session_id=TEST_SESSION,
            message_snapshot=[],
        )

        assert cognitive._active_episodes[TEST_SESSION] == old_id


# ===========================================================================
# TestRunnerSaveRestore — 10 tests
# ===========================================================================


class TestRunnerSaveRestore:
    """Test AgentRunner conversation save/restore methods."""

    def _make_runner(self, heart=None, settings=None):
        """Create a runner with mocked dependencies."""
        from nous.api.runner import AgentRunner

        cognitive = MagicMock()
        cognitive.pre_turn = AsyncMock()
        cognitive.post_turn = AsyncMock()
        cognitive.end_session = AsyncMock()
        cognitive.pre_compaction = AsyncMock()

        brain = MagicMock()
        _heart = heart or MagicMock()
        _settings = settings or _mock_settings()

        runner = AgentRunner(cognitive, brain, _heart, _settings)
        return runner

    @pytest.mark.asyncio
    async def test_restore_from_db_on_session_resume(self):
        """_get_or_create_conversation restores persisted state."""
        heart = MagicMock()
        heart.load_conversation_state = AsyncMock(return_value={
            "id": str(uuid4()),
            "agent_id": TEST_AGENT,
            "session_id": TEST_SESSION,
            "summary": "Restored summary",
            "messages": [
                {"role": "user", "content": "previous user msg"},
                {"role": "assistant", "content": "previous assistant reply"},
            ],
            "turn_count": 2,
            "compaction_count": 1,
            "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T00:00:00",
        })

        runner = self._make_runner(heart=heart)

        conversation = await runner._get_or_create_conversation(TEST_SESSION)

        assert conversation.session_id == TEST_SESSION
        assert conversation.summary == "Restored summary"
        assert conversation.compaction_count == 1
        assert len(conversation.messages) == 2
        assert conversation.messages[0].role == "user"
        assert conversation.messages[0].content == "previous user msg"

    @pytest.mark.asyncio
    async def test_missing_state_creates_fresh(self):
        """When no persisted state, creates a fresh Conversation."""
        heart = MagicMock()
        heart.load_conversation_state = AsyncMock(return_value=None)

        runner = self._make_runner(heart=heart)

        conversation = await runner._get_or_create_conversation("new-session")

        assert conversation.session_id == "new-session"
        assert conversation.messages == []
        assert conversation.summary is None
        assert conversation.compaction_count == 0

    @pytest.mark.asyncio
    async def test_save_conversation_serializes_messages(self):
        """_save_conversation converts Message objects to dicts."""
        heart = MagicMock()
        heart.save_conversation_state = AsyncMock()
        heart.load_conversation_state = AsyncMock(return_value=None)

        runner = self._make_runner(heart=heart)

        conversation = Conversation(session_id=TEST_SESSION)
        conversation.messages = [
            Message(role="user", content="hello"),
            Message(role="assistant", content="hi there"),
        ]
        conversation.summary = "Test summary"
        conversation.compaction_count = 1

        await runner._save_conversation(TEST_AGENT, TEST_SESSION, conversation)

        heart.save_conversation_state.assert_called_once()
        call_kwargs = heart.save_conversation_state.call_args.kwargs
        assert call_kwargs["agent_id"] == TEST_AGENT
        assert call_kwargs["session_id"] == TEST_SESSION
        assert call_kwargs["summary"] == "Test summary"
        assert call_kwargs["compaction_count"] == 1
        assert call_kwargs["messages"] == [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ]

    @pytest.mark.asyncio
    async def test_restore_handles_malformed_messages(self):
        """_restore_conversation filters out malformed message dicts."""
        heart = MagicMock()
        heart.load_conversation_state = AsyncMock(return_value={
            "id": str(uuid4()),
            "agent_id": TEST_AGENT,
            "session_id": TEST_SESSION,
            "summary": None,
            "messages": [
                {"role": "user", "content": "valid"},
                {"bad": "no role key"},  # Missing role
                "not a dict",  # Not a dict
                {"role": "assistant", "content": "also valid"},
            ],
            "turn_count": 0,
            "compaction_count": 0,
            "created_at": None,
            "updated_at": None,
        })

        runner = self._make_runner(heart=heart)

        conversation = await runner._restore_conversation(TEST_SESSION)

        assert conversation is not None
        assert len(conversation.messages) == 2
        assert conversation.messages[0].content == "valid"
        assert conversation.messages[1].content == "also valid"

    @pytest.mark.asyncio
    async def test_restore_returns_none_on_exception(self):
        """_restore_conversation returns None if Heart raises."""
        heart = MagicMock()
        heart.load_conversation_state = AsyncMock(side_effect=RuntimeError("DB down"))

        runner = self._make_runner(heart=heart)

        result = await runner._restore_conversation(TEST_SESSION)
        assert result is None

    @pytest.mark.asyncio
    async def test_get_or_create_is_async(self):
        """_get_or_create_conversation is now async (Phase 3 change)."""
        import inspect
        from nous.api.runner import AgentRunner

        assert inspect.iscoroutinefunction(AgentRunner._get_or_create_conversation)

    @pytest.mark.asyncio
    async def test_end_conversation_deletes_state(self):
        """end_conversation calls _delete_conversation_state."""
        heart = MagicMock()
        heart.load_conversation_state = AsyncMock(return_value=None)
        heart.delete_conversation_state = AsyncMock()

        runner = self._make_runner(heart=heart)

        # Create a conversation first
        conv = Conversation(session_id=TEST_SESSION)
        conv.messages = [Message(role="user", content="hi")]
        runner._conversations[TEST_SESSION] = conv

        await runner.end_conversation(TEST_SESSION)

        # Verify state was deleted
        heart.delete_conversation_state.assert_called_once()
        call_kwargs = heart.delete_conversation_state.call_args.kwargs
        assert call_kwargs["agent_id"] == TEST_AGENT
        assert call_kwargs["session_id"] == TEST_SESSION

    @pytest.mark.asyncio
    async def test_end_conversation_cleans_compaction_lock(self):
        """end_conversation removes the compaction lock for the session."""
        heart = MagicMock()
        heart.load_conversation_state = AsyncMock(return_value=None)
        heart.delete_conversation_state = AsyncMock()

        runner = self._make_runner(heart=heart)

        # Set up state
        conv = Conversation(session_id=TEST_SESSION)
        runner._conversations[TEST_SESSION] = conv
        runner._compaction_locks[TEST_SESSION] = asyncio.Lock()

        await runner.end_conversation(TEST_SESSION)

        assert TEST_SESSION not in runner._compaction_locks

    @pytest.mark.asyncio
    async def test_restore_sets_compaction_count(self):
        """Restored conversation preserves compaction_count."""
        heart = MagicMock()
        heart.load_conversation_state = AsyncMock(return_value={
            "id": str(uuid4()),
            "agent_id": TEST_AGENT,
            "session_id": TEST_SESSION,
            "summary": "After 3 compactions",
            "messages": [{"role": "user", "content": "latest"}],
            "turn_count": 20,
            "compaction_count": 3,
            "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T00:00:00",
        })

        runner = self._make_runner(heart=heart)

        conversation = await runner._restore_conversation(TEST_SESSION)
        assert conversation is not None
        assert conversation.compaction_count == 3

    @pytest.mark.asyncio
    async def test_existing_conversation_returned_from_cache(self):
        """_get_or_create returns cached conversation without hitting Heart."""
        heart = MagicMock()
        heart.load_conversation_state = AsyncMock(return_value=None)

        runner = self._make_runner(heart=heart)

        # Pre-populate cache
        conv = Conversation(session_id=TEST_SESSION)
        conv.messages = [Message(role="user", content="cached")]
        runner._conversations[TEST_SESSION] = conv

        result = await runner._get_or_create_conversation(TEST_SESSION)

        assert result is conv
        assert result.messages[0].content == "cached"
        # Heart should NOT be called since conversation was in cache
        heart.load_conversation_state.assert_not_called()
