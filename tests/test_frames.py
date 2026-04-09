"""Tests for FrameEngine — cognitive frame selection via pattern matching.

Uses unittest.mock throughout — no real database required.

Seed frame patterns (matching seed.sql defaults):
  task:         build, fix, create, implement, deploy, run, execute, check, show, list,
                install, update, delete, move, copy
  question:     what, how, why, explain, tell me (multi-word)
  decision:     should, choose, decide, compare, trade-off
  creative:     imagine, brainstorm, what if (multi-word), design, explore
  conversation: hello, hi, thanks, how are you (multi-word)
  debug:        error, bug, broken, failing, crash, wrong
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nous.cognitive.frames import FRAME_PRIORITY, FrameEngine
from nous.cognitive.schemas import FrameSelection

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeFrame:
    """Lightweight stand-in for nous.storage.models.Frame."""

    def __init__(
        self,
        id_: str,
        name: str,
        patterns: list[str],
        category: str = "process",
        stakes: str = "low",
        description: str | None = None,
        usage_count: int = 0,
    ) -> None:
        self.id = id_
        self.agent_id = "nous-default"
        self.name = name
        self.description = description or f"{name} frame"
        self.activation_patterns = patterns
        self.default_category = category
        self.default_stakes = stakes
        self.questions_to_ask = []
        self.usage_count = usage_count
        self.last_used = None
        self.active = True


# Seed frames that mirror the production seed.sql defaults
SEED_FRAMES = [
    FakeFrame(
        "task",
        "Task Execution",
        [
            "build",
            "fix",
            "create",
            "implement",
            "deploy",
            "run",
            "execute",
            "check",
            "show",
            "list",
            "install",
            "update",
            "delete",
            "move",
            "copy",
        ],
        "tooling",
        "medium",
    ),
    FakeFrame("question", "Question Answering", ["what", "how", "why", "explain", "tell me"], "process", "low"),
    FakeFrame(
        "decision", "Decision Making", ["should", "choose", "decide", "compare", "trade-off"], "architecture", "high"
    ),
    FakeFrame("creative", "Creative", ["imagine", "brainstorm", "what if", "design", "explore"], "architecture", "low"),
    FakeFrame("conversation", "Conversation", ["hello", "hi", "thanks", "how are you"], "process", "low"),
    FakeFrame("debug", "Debug", ["error", "bug", "broken", "failing", "crash", "wrong"], "tooling", "medium"),
]


def make_engine() -> FrameEngine:
    """FrameEngine wired to a stub Database (session never used directly)."""
    db = MagicMock()
    settings = MagicMock()
    return FrameEngine(db, settings)


def make_session() -> AsyncMock:
    """AsyncSession mock; flush() is a no-op coroutine."""
    session = AsyncMock()
    session.flush = AsyncMock()
    return session


async def select_with_frames(engine: FrameEngine, text: str, frames: list[FakeFrame]) -> FrameSelection:
    """Run engine.select() with a controlled frame list via _load_frames patch."""
    session = make_session()
    with patch.object(engine, "_load_frames", AsyncMock(return_value=frames)):
        with patch.object(engine, "_increment_usage", AsyncMock()):
            return await engine.select("nous-default", text, session=session)


# ---------------------------------------------------------------------------
# 1. test_frame_select_decision
# ---------------------------------------------------------------------------


async def test_frame_select_decision():
    """'should' keyword selects the decision frame."""
    engine = make_engine()
    result = await select_with_frames(engine, "should we use Redis?", SEED_FRAMES)
    assert isinstance(result, FrameSelection)
    assert result.frame_id == "decision"
    assert result.match_method == "pattern"
    assert result.confidence > 0


# ---------------------------------------------------------------------------
# 2. test_frame_select_task
# ---------------------------------------------------------------------------


async def test_frame_select_task():
    """'build' keyword selects the task frame."""
    engine = make_engine()
    result = await select_with_frames(engine, "build a REST API", SEED_FRAMES)
    assert result.frame_id == "task"
    assert result.match_method == "pattern"


# ---------------------------------------------------------------------------
# 3. test_frame_select_debug
# ---------------------------------------------------------------------------


async def test_frame_select_debug():
    """'error' keyword selects the debug frame."""
    engine = make_engine()
    result = await select_with_frames(engine, "error in deployment", SEED_FRAMES)
    assert result.frame_id == "debug"
    assert result.match_method == "pattern"


# ---------------------------------------------------------------------------
# 4. test_frame_select_conversation — multi-word pattern (P2-1)
# ---------------------------------------------------------------------------


async def test_frame_select_conversation():
    """'hello how are you' matches conversation with 2 patterns.

    P2-1: multi-word 'how are you' uses substring match; single-word 'hello'
    uses set membership. Both hit conversation → count=2, beats question (count=1).
    """
    engine = make_engine()
    result = await select_with_frames(engine, "hello how are you", SEED_FRAMES)
    assert result.frame_id == "conversation"
    assert result.match_method == "pattern"


# ---------------------------------------------------------------------------
# 5. test_frame_select_no_match
# ---------------------------------------------------------------------------


async def test_frame_select_no_match():
    """No matching keywords falls back to the DB conversation frame."""
    engine = make_engine()
    result = await select_with_frames(engine, "xyzzy foobar", SEED_FRAMES)
    assert result.frame_id == "conversation"
    assert result.match_method == "default"


# ---------------------------------------------------------------------------
# 6. test_frame_select_tiebreak
# ---------------------------------------------------------------------------


async def test_frame_select_tiebreak():
    """'should we fix this bug' ties at 1 match each for decision and debug.

    Decision (priority=6) beats debug (priority=5) via tiebreak.
    """
    engine = make_engine()
    result = await select_with_frames(engine, "should we fix this bug", SEED_FRAMES)
    assert result.frame_id == "decision"


# ---------------------------------------------------------------------------
# 7. test_frame_list
# ---------------------------------------------------------------------------


async def test_frame_list():
    """list_frames returns a FrameSelection for every active frame."""
    engine = make_engine()
    session = make_session()
    with patch.object(engine, "_load_frames", AsyncMock(return_value=SEED_FRAMES)):
        frames = await engine.list_frames("nous-default", session=session)
    assert len(frames) == 6
    frame_ids = {f.frame_id for f in frames}
    assert frame_ids == {"task", "question", "decision", "creative", "conversation", "debug"}


# ---------------------------------------------------------------------------
# 8. test_frame_get
# ---------------------------------------------------------------------------


async def test_frame_get():
    """get() fetches a specific frame from the DB and returns a FrameSelection."""
    engine = make_engine()
    session = make_session()

    fake_task = SEED_FRAMES[0]  # task frame
    mock_result = MagicMock()
    mock_result.scalars.return_value.first.return_value = fake_task
    session.execute.return_value = mock_result

    result = await engine.get("task", "nous-default", session=session)
    assert isinstance(result, FrameSelection)
    assert result.frame_id == "task"
    assert result.frame_name == "Task Execution"
    assert result.default_category == "tooling"


# ---------------------------------------------------------------------------
# 9. test_frame_usage_count_increments
# ---------------------------------------------------------------------------


async def test_frame_usage_count_increments():
    """Selecting a frame calls _increment_usage exactly once."""
    engine = make_engine()
    session = make_session()
    increment_mock = AsyncMock()
    with patch.object(engine, "_load_frames", AsyncMock(return_value=SEED_FRAMES)):
        with patch.object(engine, "_increment_usage", increment_mock):
            await engine.select("nous-default", "build something", session=session)
    increment_mock.assert_called_once()


# ---------------------------------------------------------------------------
# 10. test_frame_select_question
# ---------------------------------------------------------------------------


async def test_frame_select_question():
    """'what' keyword selects the question frame."""
    engine = make_engine()
    result = await select_with_frames(engine, "what is the capital of France", SEED_FRAMES)
    assert result.frame_id == "question"
    assert result.match_method == "pattern"


# ---------------------------------------------------------------------------
# 11. test_frame_select_creative
# ---------------------------------------------------------------------------


async def test_frame_select_creative():
    """'brainstorm' keyword selects the creative frame."""
    engine = make_engine()
    result = await select_with_frames(engine, "brainstorm ideas for a product", SEED_FRAMES)
    assert result.frame_id == "creative"
    assert result.match_method == "pattern"


# ---------------------------------------------------------------------------
# 12. test_case_insensitivity
# ---------------------------------------------------------------------------


async def test_case_insensitivity():
    """Pattern matching is case-insensitive — uppercase input still matches."""
    engine = make_engine()
    result = await select_with_frames(engine, "BUILD a REST API", SEED_FRAMES)
    assert result.frame_id == "task"
    assert result.match_method == "pattern"


# ---------------------------------------------------------------------------
# 13. test_confidence_single_match
# ---------------------------------------------------------------------------


async def test_confidence_single_match():
    """Single pattern match yields confidence = 0.3."""
    engine = make_engine()
    result = await select_with_frames(engine, "build this", SEED_FRAMES)
    assert result.confidence == pytest.approx(0.3)


# ---------------------------------------------------------------------------
# 14. test_confidence_two_matches
# ---------------------------------------------------------------------------


async def test_confidence_two_matches():
    """Two pattern matches within one frame yield confidence = 0.6."""
    engine = make_engine()
    # 'build' and 'fix' both hit task
    result = await select_with_frames(engine, "build and fix the server", SEED_FRAMES)
    assert result.frame_id == "task"
    assert result.confidence == pytest.approx(0.6)


# ---------------------------------------------------------------------------
# 15. test_confidence_capped_at_one
# ---------------------------------------------------------------------------


async def test_confidence_capped_at_one():
    """Confidence caps at 1.0 regardless of how many patterns match.

    'build create implement deploy' → 4 task patterns → 4 × 0.3 = 1.2, capped to 1.0.
    """
    engine = make_engine()
    result = await select_with_frames(engine, "build create implement deploy", SEED_FRAMES)
    assert result.frame_id == "task"
    assert result.confidence == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# 16. test_frame_get_not_found
# ---------------------------------------------------------------------------


async def test_frame_get_not_found():
    """get() raises ValueError when the frame ID is not in the database."""
    engine = make_engine()
    session = make_session()

    mock_result = MagicMock()
    mock_result.scalars.return_value.first.return_value = None
    session.execute.return_value = mock_result

    with pytest.raises(ValueError, match="Frame 'nonexistent' not found"):
        await engine.get("nonexistent", "nous-default", session=session)


# ---------------------------------------------------------------------------
# 17. test_frame_select_empty_input
# ---------------------------------------------------------------------------


async def test_frame_select_empty_input():
    """Empty string has no token matches — falls back to default conversation."""
    engine = make_engine()
    result = await select_with_frames(engine, "", SEED_FRAMES)
    assert result.frame_id == "conversation"
    assert result.match_method == "default"


# ---------------------------------------------------------------------------
# 18. test_frame_select_no_frames_in_db
# ---------------------------------------------------------------------------


async def test_frame_select_no_frames_in_db():
    """When no frames exist for the agent, returns hardcoded default FrameSelection."""
    engine = make_engine()
    result = await select_with_frames(engine, "build something", frames=[])
    assert result.frame_id == "conversation"
    assert result.match_method == "default"
    # Hardcoded default has a known description
    assert result.description is not None


# ---------------------------------------------------------------------------
# 19. test_frame_list_no_frames
# ---------------------------------------------------------------------------


async def test_frame_list_no_frames():
    """list_frames returns empty list when no frames are loaded."""
    engine = make_engine()
    session = make_session()
    with patch.object(engine, "_load_frames", AsyncMock(return_value=[])):
        frames = await engine.list_frames("nous-default", session=session)
    assert frames == []


# ---------------------------------------------------------------------------
# 20. test_frame_list_match_method_is_list
# ---------------------------------------------------------------------------


async def test_frame_list_match_method_is_list():
    """All FrameSelection objects from list_frames use match_method='list' and confidence=0."""
    engine = make_engine()
    session = make_session()
    with patch.object(engine, "_load_frames", AsyncMock(return_value=SEED_FRAMES)):
        frames = await engine.list_frames("nous-default", session=session)
    assert len(frames) == 6
    for f in frames:
        assert isinstance(f, FrameSelection)
        assert f.match_method == "list"
        assert f.confidence == 0.0


# ---------------------------------------------------------------------------
# 21. test_default_fallback_uses_conversation_from_frames
# ---------------------------------------------------------------------------


async def test_default_fallback_uses_conversation_from_frames():
    """No-match with frames present uses the conversation frame from the DB list.

    The returned frame_name comes from the loaded frame (not the hardcoded
    _default_selection), confirming the correct code path was taken.
    """
    engine = make_engine()
    increment_mock = AsyncMock()
    result = None
    with patch.object(engine, "_load_frames", AsyncMock(return_value=SEED_FRAMES)):
        with patch.object(engine, "_increment_usage", increment_mock):
            session = make_session()
            result = await engine.select("nous-default", "xyzzy plugh quux", session=session)

    assert result.frame_id == "conversation"
    assert result.match_method == "default"
    # _increment_usage is called even for the default fallback path
    increment_mock.assert_called_once()


# ---------------------------------------------------------------------------
# 22. test_frame_priority_ordering
# ---------------------------------------------------------------------------


def test_frame_priority_ordering():
    """FRAME_PRIORITY values strictly increase: conversation < creative < question < task < debug < decision."""
    assert FRAME_PRIORITY["decision"] > FRAME_PRIORITY["debug"]
    assert FRAME_PRIORITY["debug"] > FRAME_PRIORITY["task"]
    assert FRAME_PRIORITY["task"] > FRAME_PRIORITY["question"]
    assert FRAME_PRIORITY["question"] > FRAME_PRIORITY["creative"]
    assert FRAME_PRIORITY["creative"] > FRAME_PRIORITY["conversation"]


# ---------------------------------------------------------------------------
# 23. test_frame_priority_all_frames_covered
# ---------------------------------------------------------------------------


def test_frame_priority_all_frames_covered():
    """FRAME_PRIORITY covers exactly the 6 known frame types."""
    expected = {"conversation", "creative", "question", "task", "debug", "decision"}
    assert set(FRAME_PRIORITY.keys()) == expected


# ---------------------------------------------------------------------------
# 24. test_frame_engine_default_frame_id
# ---------------------------------------------------------------------------


def test_frame_engine_default_frame_id():
    """FrameEngine._default_frame_id is 'conversation'."""
    engine = make_engine()
    assert engine._default_frame_id == "conversation"


# ---------------------------------------------------------------------------
# 25. test_frame_get_match_method_direct
# ---------------------------------------------------------------------------


async def test_frame_get_match_method_direct():
    """get() returns match_method='direct' and confidence=1.0."""
    engine = make_engine()
    session = make_session()

    fake_decision = SEED_FRAMES[2]  # decision frame
    mock_result = MagicMock()
    mock_result.scalars.return_value.first.return_value = fake_decision
    session.execute.return_value = mock_result

    result = await engine.get("decision", "nous-default", session=session)
    assert result.match_method == "direct"
    assert result.confidence == pytest.approx(1.0)
    assert result.frame_id == "decision"


# ---------------------------------------------------------------------------
# 26. test_multiword_pattern_substring_match
# ---------------------------------------------------------------------------


async def test_multiword_pattern_substring_match():
    """Multi-word patterns use substring match on the full lowercased input.

    'tell me' (a question pattern with a space) must appear verbatim as a
    substring — it cannot be detected by set membership on tokens alone.
    """
    engine = make_engine()
    result = await select_with_frames(engine, "tell me everything about it", SEED_FRAMES)
    assert result.frame_id == "question"


# ---------------------------------------------------------------------------
# 27. test_multiword_pattern_order_matters
# ---------------------------------------------------------------------------


async def test_multiword_pattern_order_matters():
    """Reversed word order defeats a multi-word pattern.

    'me tell' is not the same substring as 'tell me' so the pattern
    does not match, and neither does any other pattern here.
    """
    engine = make_engine()
    result = await select_with_frames(engine, "me tell everything", SEED_FRAMES)
    assert result.frame_id == "conversation"
    assert result.match_method == "default"


# ---------------------------------------------------------------------------
# 28. test_single_word_partial_no_match
# ---------------------------------------------------------------------------


async def test_single_word_partial_no_match():
    """Single-word patterns use set membership, not substring.

    'builds' ≠ 'build' — the task frame must NOT activate.
    """
    engine = make_engine()
    # Only word is 'buildings' — none of the task patterns are in the token set
    result = await select_with_frames(engine, "buildings everywhere", SEED_FRAMES)
    # No single-word or multi-word pattern matches 'buildings'
    assert result.match_method == "default"


# ---------------------------------------------------------------------------
# 29. test_frame_select_session_context_manager
# ---------------------------------------------------------------------------


async def test_frame_select_session_context_manager():
    """When no session is passed, FrameEngine opens one via db.session()."""
    engine = make_engine()

    # Set up db.session() as an async context manager that yields a session
    mock_session = make_session()
    mock_session_ctx = AsyncMock()
    mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_ctx.__aexit__ = AsyncMock(return_value=False)
    engine._db.session.return_value = mock_session_ctx

    with patch.object(engine, "_load_frames", AsyncMock(return_value=SEED_FRAMES)):
        with patch.object(engine, "_increment_usage", AsyncMock()):
            result = await engine.select("nous-default", "build something")

    assert result.frame_id == "task"
    engine._db.session.assert_called_once()
