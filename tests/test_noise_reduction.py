"""Tests for 005.5 Noise Reduction — Episode Significance, Dedup, Decision Noise.

26 tests across 4 classes:
- TestEpisodeSignificance (8): _should_create_episode() in CognitiveLayer
- TestEpisodeDedup (7): _is_duplicate_episode() in CognitiveLayer
- TestDecisionNoise (7): _is_noise_decision() in Brain
- TestFrameInstructions (4): Updated frame instructions in AgentRunner

All tests use mocks — no real database needed.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nous.brain.schemas import ReasonInput
from nous.cognitive.schemas import FrameSelection, TurnContext


# ---------------------------------------------------------------------------
# SessionMetadata — imported from the real module (005.5 landed).
# ---------------------------------------------------------------------------
from nous.cognitive.schemas import SessionMetadata


# ---------------------------------------------------------------------------
# Constants from the spec (module-level in layer.py)
# ---------------------------------------------------------------------------

_MIN_CONTENT_LENGTH = 200
_MIN_TURNS_WITHOUT_TOOLS = 1


# ---------------------------------------------------------------------------
# Helpers: Build minimal instances for unit tests
# ---------------------------------------------------------------------------


def _make_cognitive_layer():
    """Construct a CognitiveLayer with fully-mocked dependencies.

    Patches all sub-engine constructors to avoid real DB/embedding access
    during __init__. Returns the layer with _session_metadata dict ready.
    """
    mock_brain = MagicMock()
    mock_brain.db = MagicMock()
    mock_brain.embeddings = None
    mock_brain.agent_id = "test-agent"

    mock_heart = MagicMock()
    mock_heart._episodes = MagicMock()
    mock_heart._episodes.embeddings = None
    mock_heart.start_episode = AsyncMock()
    mock_heart.end_episode = AsyncMock()
    mock_heart.deactivate_episode = AsyncMock()
    mock_heart.search_recent_episodes_by_embedding = AsyncMock(return_value=[])
    mock_heart.list_censors = AsyncMock(return_value=[])
    mock_heart.get_or_create_working_memory = AsyncMock()
    mock_heart.focus = AsyncMock()

    mock_settings = MagicMock()
    mock_settings.agent_id = "test-agent"

    with (
        patch("nous.cognitive.layer.FrameEngine"),
        patch("nous.cognitive.layer.IntentClassifier"),
        patch("nous.cognitive.layer.UsageTracker"),
        patch("nous.cognitive.layer.ConversationDeduplicator"),
        patch("nous.cognitive.layer.ContextEngine"),
        patch("nous.cognitive.layer.DeliberationEngine"),
        patch("nous.cognitive.layer.MonitorEngine"),
    ):
        from nous.cognitive.layer import CognitiveLayer

        layer = CognitiveLayer(
            mock_brain, mock_heart, mock_settings, identity_prompt="Test."
        )

    # Ensure _session_metadata exists (it should from __init__ after 005.5)
    if not hasattr(layer, "_session_metadata"):
        layer._session_metadata = {}

    # Re-attach the real mocked heart for tests that need it
    layer._heart = mock_heart
    layer._brain = mock_brain

    return layer


def _make_brain():
    """Construct a minimal Brain for _is_noise_decision tests.

    Since _is_noise_decision is a pure method that only uses module-level
    _NOISE_KEYWORDS and the method arguments, we just need a valid Brain
    instance.
    """
    mock_db = MagicMock()
    mock_settings = MagicMock()
    mock_settings.agent_id = "test-agent"

    with (
        patch("nous.brain.brain.GuardrailEngine"),
        patch("nous.brain.brain.CalibrationEngine"),
        patch("nous.brain.brain.BridgeExtractor"),
        patch("nous.brain.brain.QualityScorer"),
    ):
        from nous.brain.brain import Brain

        brain = Brain(database=mock_db, settings=mock_settings)

    return brain


def _make_runner():
    """Construct a minimal AgentRunner for frame instruction tests."""
    mock_cognitive = MagicMock()
    mock_brain = MagicMock()
    mock_heart = MagicMock()
    mock_settings = MagicMock()
    mock_settings.agent_id = "test-agent"

    from nous.api.runner import AgentRunner

    return AgentRunner(mock_cognitive, mock_brain, mock_heart, mock_settings)


def _make_turn_context(frame_id: str) -> TurnContext:
    """Build a TurnContext with a specific frame_id for frame instruction tests."""
    return TurnContext(
        system_prompt="Test prompt.",
        frame=FrameSelection(
            frame_id=frame_id,
            frame_name=frame_id.capitalize(),
            confidence=0.9,
            match_method="pattern",
        ),
        decision_id=None,
        active_censors=[],
        context_token_estimate=100,
    )


# ===========================================================================
# TestEpisodeSignificance — 8 tests for _should_create_episode()
# ===========================================================================


class TestEpisodeSignificance:
    """Tests for _should_create_episode() in CognitiveLayer.

    _should_create_episode is a sync method that checks session metadata
    to decide if an interaction warrants creating an episode.

    The turn_count is incremented in post_turn, so during turn N's pre_turn
    the count reflects completed turns (N-1). First turn has turn_count=0.
    """

    # 1. First turn (turn_count == 0) -> True
    def test_first_turn_creates_episode(self):
        """First turn of session always creates an episode (turn_count == 0).

        R-P0-1: Checks turn_count == 0 (not meta is None) because pre_turn
        creates metadata via setdefault() BEFORE _should_create_episode runs.
        """
        layer = _make_cognitive_layer()
        sid = f"test-{uuid.uuid4().hex[:8]}"

        # Simulate pre_turn creating metadata via setdefault()
        layer._session_metadata[sid] = SessionMetadata(turn_count=0)

        result = layer._should_create_episode(sid, "Hello!")
        assert result is True

    # 2. Second turn, no tools, short content -> False
    def test_second_turn_no_tools_short_content_skips(self):
        """Second turn with no tools and short content is not significant.

        After first turn completes: turn_count=1, no tools used, combined
        content well below 200 chars. Not enough signals for significance.
        """
        layer = _make_cognitive_layer()
        sid = f"test-{uuid.uuid4().hex[:8]}"

        # After first turn: turn_count=1, no tools, short content (50 < 200)
        layer._session_metadata[sid] = SessionMetadata(
            turn_count=1,
            tools_used=set(),
            total_user_chars=20,
            total_assistant_chars=30,
        )

        result = layer._should_create_episode(sid, "OK")
        assert result is False

    # 3. Second turn, tools used -> True
    def test_second_turn_with_tools_creates_episode(self):
        """Tools used indicates real work happened -- always significant."""
        layer = _make_cognitive_layer()
        sid = f"test-{uuid.uuid4().hex[:8]}"

        layer._session_metadata[sid] = SessionMetadata(
            turn_count=1,
            tools_used={"record_decision"},
            total_user_chars=20,
            total_assistant_chars=30,
        )

        result = layer._should_create_episode(sid, "What did you decide?")
        assert result is True

    # 4. Third turn, no tools -> True (multi-turn)
    def test_third_turn_multi_turn_creates_episode(self):
        """Multi-turn conversation (3+ actual turns) is always significant."""
        layer = _make_cognitive_layer()
        sid = f"test-{uuid.uuid4().hex[:8]}"

        # After two completed turns, turn_count=2
        layer._session_metadata[sid] = SessionMetadata(
            turn_count=2,
            tools_used=set(),
            total_user_chars=40,
            total_assistant_chars=60,
        )

        result = layer._should_create_episode(sid, "Continue")
        assert result is True

    # 5. Second turn, content > 200 chars -> True
    def test_second_turn_long_content_creates_episode(self):
        """Content exceeding 200 chars threshold triggers significance."""
        layer = _make_cognitive_layer()
        sid = f"test-{uuid.uuid4().hex[:8]}"

        # turn_count=1, no tools, but combined chars > 200
        layer._session_metadata[sid] = SessionMetadata(
            turn_count=1,
            tools_used=set(),
            total_user_chars=120,
            total_assistant_chars=100,  # total = 220 > 200
        )

        result = layer._should_create_episode(sid, "x")
        assert result is True

    # 6. Explicit "remember this" in input -> True
    def test_remember_keyword_creates_episode(self):
        """Explicit 'remember this' request always creates an episode."""
        layer = _make_cognitive_layer()
        sid = f"test-{uuid.uuid4().hex[:8]}"

        layer._session_metadata[sid] = SessionMetadata(
            turn_count=1,
            tools_used=set(),
            total_user_chars=20,
            total_assistant_chars=20,
            has_explicit_remember=True,
        )

        result = layer._should_create_episode(sid, "remember this: API uses v2")
        assert result is True

    # 7. Trivial episode discarded at end_session
    @pytest.mark.asyncio
    async def test_trivial_episode_discarded_at_end_session(self):
        """Single-turn, no tools, short content -> episode soft-deleted at end.

        D5: Trivial episodes are soft-deleted (active=False) instead of
        being kept as noise. This is the deferred evaluation approach:
        always create on first turn, discard at end if trivial.
        """
        layer = _make_cognitive_layer()
        sid = f"test-{uuid.uuid4().hex[:8]}"
        episode_id = str(uuid.uuid4())

        # Set up state: episode exists, metadata shows trivial session
        layer._active_episodes[sid] = episode_id
        layer._session_metadata[sid] = SessionMetadata(
            turn_count=1,
            tools_used=set(),
            total_user_chars=10,
            total_assistant_chars=15,  # total=25 < 200
        )

        # Mock event emission and monitor cleanup
        layer._brain.emit_event = AsyncMock()
        layer._monitor = MagicMock()
        layer._monitor._session_censor_counts = {}

        await layer.end_session("test-agent", sid)

        # deactivate_episode should be called (soft-delete)
        layer._heart.deactivate_episode.assert_called_once()
        # end_episode should NOT be called
        layer._heart.end_episode.assert_not_called()
        # Session tracking should be cleaned up
        assert sid not in layer._active_episodes
        assert sid not in layer._session_metadata

    # 8. Non-trivial episode kept at end_session
    @pytest.mark.asyncio
    async def test_nontrivial_episode_kept_at_end_session(self):
        """Multi-turn, tools-used session -> episode properly ended, not discarded."""
        layer = _make_cognitive_layer()
        sid = f"test-{uuid.uuid4().hex[:8]}"
        episode_id = str(uuid.uuid4())

        # Set up state: episode exists, metadata shows substantial session
        layer._active_episodes[sid] = episode_id
        layer._session_metadata[sid] = SessionMetadata(
            turn_count=3,
            tools_used={"record_decision", "recall_deep"},
            total_user_chars=300,
            total_assistant_chars=500,
        )

        # Mock event emission and monitor cleanup
        layer._brain.emit_event = AsyncMock()
        layer._monitor = MagicMock()
        layer._monitor._session_censor_counts = {}

        await layer.end_session("test-agent", sid, reflection="Good session.")

        # end_episode should be called (normal end with reflection)
        layer._heart.end_episode.assert_called_once()
        # deactivate_episode should NOT be called
        layer._heart.deactivate_episode.assert_not_called()
        # Session tracking should be cleaned up
        assert sid not in layer._active_episodes


# ===========================================================================
# TestEpisodeDedup — 7 tests for _is_duplicate_episode()
# ===========================================================================


class TestEpisodeDedup:
    """Tests for _is_duplicate_episode() in CognitiveLayer.

    _is_duplicate_episode is an async method that checks if a similar
    recent episode already exists (>0.85 cosine similarity within 48h).

    R-P0-2: Returns bool only, never stores reused IDs in _active_episodes.
    R-P1-2: Uses direct cosine similarity, not hybrid_search.
    R-P1-3: Filters to episodes within last 48 hours.
    """

    # 9. No similar episode -> returns False
    @pytest.mark.asyncio
    async def test_no_similar_episode_returns_false(self):
        """No similar recent episode found -> returns False, create the episode."""
        layer = _make_cognitive_layer()

        # Enable embeddings so dedup check runs
        mock_embeddings = MagicMock()
        mock_embeddings.embed = AsyncMock(return_value=[0.1] * 1536)
        layer._heart._episodes.embeddings = mock_embeddings

        # No similar episodes found
        layer._heart.search_recent_episodes_by_embedding = AsyncMock(return_value=[])

        result = await layer._is_duplicate_episode("Build a REST API")
        assert result is False

    # 10. Similar episode (>0.85 cosine) -> returns True
    @pytest.mark.asyncio
    async def test_similar_episode_above_threshold_returns_true(self):
        """Episode with >0.85 cosine similarity -> True, skip creation."""
        layer = _make_cognitive_layer()

        mock_embeddings = MagicMock()
        mock_embeddings.embed = AsyncMock(return_value=[0.1] * 1536)
        layer._heart._episodes.embeddings = mock_embeddings

        # Return a match above the 0.85 threshold
        matching_episode_id = uuid.uuid4()
        layer._heart.search_recent_episodes_by_embedding = AsyncMock(
            return_value=[(matching_episode_id, 0.92)]
        )

        result = await layer._is_duplicate_episode("Hello, what can you do?")
        assert result is True

    # 11. Similar but below threshold (0.80) -> returns False
    @pytest.mark.asyncio
    async def test_similar_episode_below_threshold_returns_false(self):
        """Episode at 0.80 cosine similarity (below 0.85 threshold) -> False."""
        layer = _make_cognitive_layer()

        mock_embeddings = MagicMock()
        mock_embeddings.embed = AsyncMock(return_value=[0.1] * 1536)
        layer._heart._episodes.embeddings = mock_embeddings

        # Return a match below the 0.85 threshold
        matching_episode_id = uuid.uuid4()
        layer._heart.search_recent_episodes_by_embedding = AsyncMock(
            return_value=[(matching_episode_id, 0.80)]
        )

        result = await layer._is_duplicate_episode("Build a REST API")
        assert result is False

    # 12. Embedding failure -> returns False (graceful degradation)
    @pytest.mark.asyncio
    async def test_embedding_failure_returns_false(self):
        """If embedding generation fails, return False and proceed with creation.

        Graceful degradation: never block episode creation due to infra failures.
        """
        layer = _make_cognitive_layer()

        mock_embeddings = MagicMock()
        mock_embeddings.embed = AsyncMock(side_effect=RuntimeError("API down"))
        layer._heart._episodes.embeddings = mock_embeddings

        result = await layer._is_duplicate_episode("Build something")
        assert result is False

    # 13. Empty search results -> returns False
    @pytest.mark.asyncio
    async def test_empty_search_results_returns_false(self):
        """Search returns empty list -> returns False."""
        layer = _make_cognitive_layer()

        mock_embeddings = MagicMock()
        mock_embeddings.embed = AsyncMock(return_value=[0.1] * 1536)
        layer._heart._episodes.embeddings = mock_embeddings

        layer._heart.search_recent_episodes_by_embedding = AsyncMock(
            return_value=[]
        )

        result = await layer._is_duplicate_episode("Totally new topic")
        assert result is False

    # 14. Duplicate skips episode creation (no episode in _active_episodes)
    @pytest.mark.asyncio
    async def test_duplicate_skips_episode_creation(self):
        """When duplicate detected in pre_turn, no new episode is created.

        R-P0-2: We never store reused episode IDs in _active_episodes because
        end_session would corrupt/delete the original episode. Instead, we
        simply skip creation entirely.
        """
        layer = _make_cognitive_layer()
        sid = f"test-{uuid.uuid4().hex[:8]}"

        # Precondition: no active episode for this session
        assert sid not in layer._active_episodes

        # Mock _should_create_episode to return True (significance passes)
        layer._should_create_episode = MagicMock(return_value=True)

        # Mock _is_duplicate_episode to return True (duplicate found)
        layer._is_duplicate_episode = AsyncMock(return_value=True)

        # Mock the rest of pre_turn dependencies
        mock_frame = FrameSelection(
            frame_id="conversation",
            frame_name="Conversation",
            confidence=0.9,
            match_method="default",
        )
        layer._frames = MagicMock()
        layer._frames.select = AsyncMock(return_value=mock_frame)
        layer._intent_classifier = MagicMock()
        layer._intent_classifier.classify = MagicMock(return_value=MagicMock())
        layer._intent_classifier.plan_retrieval = MagicMock(return_value=MagicMock())
        layer._context = MagicMock()
        layer._context.build = AsyncMock(
            return_value=MagicMock(
                system_prompt="Test",
                sections=[],
                recalled_ids={},
                recalled_content_map={},
            )
        )
        layer._deliberation = MagicMock()
        layer._deliberation.should_deliberate = AsyncMock(return_value=False)

        # Call pre_turn
        await layer.pre_turn("test-agent", sid, "Hello!", session=None)

        # Episode should NOT be in _active_episodes (duplicate was skipped)
        assert sid not in layer._active_episodes

    # 15. Old duplicate (>48h) not matched
    @pytest.mark.asyncio
    async def test_old_duplicate_not_matched(self):
        """Episodes older than 48h are filtered by the time window.

        The 48h filter is in the SQL query (search_recent_episodes_by_embedding).
        When similar episodes are older than 48h, the search returns nothing
        and _is_duplicate_episode returns False.
        """
        layer = _make_cognitive_layer()

        mock_embeddings = MagicMock()
        mock_embeddings.embed = AsyncMock(return_value=[0.1] * 1536)
        layer._heart._episodes.embeddings = mock_embeddings

        # The search method filters by 48h window and finds nothing
        # (the similar episode exists but is older than 48h)
        layer._heart.search_recent_episodes_by_embedding = AsyncMock(
            return_value=[]
        )

        result = await layer._is_duplicate_episode("Hello, what can you do?")
        assert result is False

        # Verify the search was called with hours=48
        layer._heart.search_recent_episodes_by_embedding.assert_called_once()
        call_kwargs = layer._heart.search_recent_episodes_by_embedding.call_args
        # Check hours parameter — could be positional or keyword
        if call_kwargs.kwargs:
            assert call_kwargs.kwargs.get("hours", 48) == 48
        args = call_kwargs.args
        if len(args) > 1:
            assert args[1] == 48


# ===========================================================================
# TestDecisionNoise — 7 tests for _is_noise_decision()
# ===========================================================================


class TestDecisionNoise:
    """Tests for _is_noise_decision() in Brain.

    _is_noise_decision is a sync method that detects obvious non-decisions
    (status reports, routine completions) based on:
    1. Very short description (<20 chars) with no reasons
    2. Description with >50% noise keywords and no reasons
    3. Empty description (no words after tokenization)

    R-P1-4: Uses re.findall(r'\\w+', ...) for tokenization so punctuation
    is stripped (e.g., "completed." matches "completed").
    """

    # 16. Short status report (<20 chars, no reasons) -> True
    def test_short_status_no_reasons_is_noise(self):
        """Very short description (<20 chars) with no reasons is noise."""
        brain = _make_brain()
        result = brain._is_noise_decision("Git clone done", [])
        assert result is True

    # 17. Real decision with alternatives -> False
    def test_real_decision_with_alternatives_not_noise(self):
        """Real decision with reasoning about alternatives is not noise."""
        brain = _make_brain()
        reasons = [
            ReasonInput(type="analysis", text="Compared PostgreSQL vs MongoDB for our use case"),
            ReasonInput(type="pattern", text="Follows established RDBMS patterns"),
        ]
        result = brain._is_noise_decision(
            "Use PostgreSQL instead of MongoDB for persistent storage", reasons
        )
        assert result is False

    # 18. Noise keywords >50% with no reasons -> True
    def test_noise_keywords_majority_no_reasons_is_noise(self):
        """Description with >50% noise keywords and no reasons is noise.

        _NOISE_KEYWORDS includes: completed, done, finished, success, started,
        status, progress, update, checked, confirmed.
        """
        brain = _make_brain()
        # Words: {completed, status, update, checked, successfully}
        # Noise keywords: completed, status, update, checked -> 4/5 = 80% > 50%
        result = brain._is_noise_decision(
            "Completed status update checked successfully", []
        )
        assert result is True

    # 19. Noise keywords but WITH reasons -> False
    def test_noise_keywords_with_reasons_not_noise(self):
        """Even descriptions full of noise keywords are NOT noise if reasons exist."""
        brain = _make_brain()
        reasons = [
            ReasonInput(type="analysis", text="Decided to mark as completed after review"),
        ]
        result = brain._is_noise_decision(
            "Completed the migration status update", reasons
        )
        assert result is False

    # 20. Empty description -> True
    def test_empty_description_is_noise(self):
        """Empty or whitespace-only description is noise."""
        brain = _make_brain()
        result = brain._is_noise_decision("", [])
        assert result is True

    # 21. Normal description with reasons -> False
    def test_normal_description_with_reasons_not_noise(self):
        """Normal-length description with reasons is not noise."""
        brain = _make_brain()
        reasons = [
            ReasonInput(type="analysis", text="Analyzed trade-offs between options"),
        ]
        result = brain._is_noise_decision(
            "Chose HNSW indexing over IVFFlat for vector similarity search", reasons
        )
        assert result is False

    # 22. Punctuation-attached keywords detected ("completed." matches)
    def test_punctuation_attached_keywords_detected(self):
        """Noise keywords with attached punctuation are still detected.

        R-P1-4: Uses re.findall(r'\\w+', ...) instead of split() to strip
        punctuation. "completed." -> "completed" (matches _NOISE_KEYWORDS).
        """
        brain = _make_brain()
        # "completed. confirmed. done." -> words: {completed, confirmed, done}
        # All 3 are noise keywords -> 3/3 = 100% > 50%
        result = brain._is_noise_decision("completed. confirmed. done.", [])
        assert result is True


# ===========================================================================
# TestFrameInstructions — 4 tests for updated frame instructions
# ===========================================================================


class TestFrameInstructions:
    """Tests for updated frame instructions in AgentRunner._get_frame_instructions().

    The 005.5 spec (Phase C1) tightens frame instructions to explicitly
    define what IS and IS NOT a decision, warn against routine completions
    and routine debug steps, etc.

    These tests verify the updated instruction strings contain the
    required language after the 005.5 changes are applied.
    """

    # 23. Decision frame mentions "what IS a decision"
    def test_decision_frame_defines_what_is_a_decision(self):
        """Decision frame instructions explicitly define what IS a decision."""
        runner = _make_runner()
        ctx = _make_turn_context("decision")

        instructions = runner._get_frame_instructions(ctx)

        # Must contain the positive definition of a decision
        assert "What IS a decision" in instructions
        # Must mention it involves choosing between alternatives
        assert "alternatives" in instructions.lower()

    # 24. Decision frame mentions "what is NOT a decision"
    def test_decision_frame_defines_what_is_not_a_decision(self):
        """Decision frame instructions explicitly define what is NOT a decision."""
        runner = _make_runner()
        ctx = _make_turn_context("decision")

        instructions = runner._get_frame_instructions(ctx)

        # Must contain the negative definition
        assert "What is NOT a decision" in instructions
        # Must mention status reports as a non-decision
        assert "Status reports" in instructions or "status reports" in instructions.lower()

    # 25. Task frame warns against routine completions
    def test_task_frame_warns_against_routine_completions(self):
        """Task frame instructions warn against recording routine task completions."""
        runner = _make_runner()
        ctx = _make_turn_context("task")

        instructions = runner._get_frame_instructions(ctx)

        # Must warn against routine completions
        assert "routine" in instructions.lower()
        # Must include explicit "Do NOT record" instruction
        assert "Do NOT record" in instructions

    # 26. Debug frame warns against routine debug steps
    def test_debug_frame_warns_against_routine_debug_steps(self):
        """Debug frame instructions warn against recording routine debug steps."""
        runner = _make_runner()
        ctx = _make_turn_context("debug")

        instructions = runner._get_frame_instructions(ctx)

        # Must warn against routine debug observations
        assert "routine" in instructions.lower()
        # Must specifically mention debug steps/observations
        assert "Do NOT record" in instructions or "do not record" in instructions.lower()
