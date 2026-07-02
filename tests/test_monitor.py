"""Tests for MonitorEngine — post-turn assessment and learning.

All tests use real Postgres via the SAVEPOINT fixture from conftest.py.
MonitorEngine evaluates turn results and extracts lessons.

Key plan adjustments applied:
- P2-3: surprise heuristic uses structural signals only (turn_result.error,
  tool_result.error), NOT text-matching ("failed"/"error" in response)
- P1-4: censor field is 'action', not 'severity'
- P2-2: learn() does NOT end episodes (only end_session does)
- P2-4: censor deduplication before creating
"""

import pytest
import pytest_asyncio

from nous.brain.brain import Brain
from nous.brain.schemas import ReasonInput, RecordInput
from nous.cognitive.monitor import MonitorEngine
from nous.cognitive.schemas import Assessment, FrameSelection, ToolResult, TurnResult
from nous.config import Settings
from nous.heart import EpisodeInput

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def brain(db, settings):
    """Brain without embeddings for monitor tests."""
    b = Brain(database=db, settings=settings)
    yield b
    await b.close()


@pytest_asyncio.fixture
async def monitor(brain, heart, settings):
    """MonitorEngine wired to Brain and Heart."""
    return MonitorEngine(brain, heart, settings)


def _frame(frame_id: str = "task") -> FrameSelection:
    """Build a FrameSelection with defaults."""
    return FrameSelection(
        frame_id=frame_id,
        frame_name="Task Execution",
        confidence=0.9,
        match_method="pattern",
        default_category="tooling",
        default_stakes="medium",
    )


def _record_input(**overrides) -> RecordInput:
    """Build a RecordInput with sensible defaults."""
    defaults = dict(
        description="Monitor test decision",
        confidence=0.85,
        category="architecture",
        stakes="medium",
        reasons=[ReasonInput(type="analysis", text="Test reason")],
    )
    defaults.update(overrides)
    return RecordInput(**defaults)


# ---------------------------------------------------------------------------
# 1. test_assess_no_errors
# ---------------------------------------------------------------------------


async def test_assess_no_errors(monitor, session):
    """No errors -> surprise_level=0.0."""
    turn_result = TurnResult(response_text="Task completed successfully.")
    assessment = await monitor.assess("nous-default", "session-1", turn_result, session=session)

    assert isinstance(assessment, Assessment)
    assert assessment.surprise_level == 0.0
    assert len(assessment.censor_candidates) == 0


# ---------------------------------------------------------------------------
# 2. test_assess_tool_error
# ---------------------------------------------------------------------------


async def test_assess_tool_error(monitor, session):
    """Tool error -> surprise_level=0.3, censor candidate generated."""
    turn_result = TurnResult(
        response_text="Tried to execute but failed.",
        tool_results=[
            ToolResult(
                tool_name="file_write",
                arguments={"path": "/etc/config"},
                error="Permission denied: /etc/config",
            )
        ],
    )
    assessment = await monitor.assess("nous-default", "session-1", turn_result, session=session)

    assert assessment.surprise_level >= 0.3
    assert len(assessment.censor_candidates) >= 1
    assert "file_write" in assessment.censor_candidates[0]


# ---------------------------------------------------------------------------
# 3. test_assess_turn_error
# ---------------------------------------------------------------------------


async def test_assess_turn_error(monitor, session):
    """Turn-level error -> surprise_level=0.9."""
    turn_result = TurnResult(
        response_text="",
        error="LLM context window exceeded",
    )
    assessment = await monitor.assess("nous-default", "session-1", turn_result, session=session)

    assert assessment.surprise_level >= 0.9


# ---------------------------------------------------------------------------
# 4. test_transient_error_no_censor
# ---------------------------------------------------------------------------


async def test_transient_error_no_censor(monitor, session):
    """Transient errors (timeout, 429) don't create censor candidates."""
    turn_result = TurnResult(
        response_text="Request timed out.",
        tool_results=[
            ToolResult(
                tool_name="api_call",
                arguments={"url": "https://example.com"},
                error="timeout: request exceeded 30s",
            )
        ],
    )
    assessment = await monitor.assess("nous-default", "session-1", turn_result, session=session)

    # Transient error should NOT generate censor candidates
    assert len(assessment.censor_candidates) == 0


async def test_transient_429_no_censor(monitor, session):
    """429 rate limit error is transient, no censor candidate."""
    turn_result = TurnResult(
        response_text="Rate limited.",
        tool_results=[
            ToolResult(
                tool_name="api_call",
                arguments={},
                error="429 Too Many Requests",
            )
        ],
    )
    assessment = await monitor.assess("nous-default", "session-1", turn_result, session=session)
    assert len(assessment.censor_candidates) == 0


# ---------------------------------------------------------------------------
# 5. test_learn_creates_censors
# ---------------------------------------------------------------------------


async def test_learn_creates_censors(monitor, heart, session):
    """High surprise with censor candidates -> Heart.censors.add() called."""
    assessment = Assessment(
        actual="Tool failed with permission error",
        surprise_level=0.8,
        censor_candidates=["Avoid file_write with /etc paths -- Permission denied"],
    )
    turn_result = TurnResult(
        response_text="Failed to write.",
        tool_results=[
            ToolResult(
                tool_name="file_write",
                arguments={"path": "/etc/config"},
                error="Permission denied",
            )
        ],
    )
    frame = _frame()

    await monitor.learn("nous-default", "session-1", assessment, turn_result, frame, session=session)

    # Censor should have been created
    censors = await heart.list_censors(session=session)
    censor_patterns = [c.trigger_pattern for c in censors]
    # At least one censor should exist related to the candidate
    assert any("file_write" in p or "etc" in p.lower() for p in censor_patterns)


# ---------------------------------------------------------------------------
# 6. test_learn_does_not_end_episode (P2-2)
# ---------------------------------------------------------------------------


async def test_learn_does_not_end_episode(monitor, heart, session):
    """learn() does NOT end the episode — only end_session does (P2-2)."""
    episode = await heart.start_episode(
        EpisodeInput(summary="Monitor learn test episode", trigger="test"),
        session=session,
    )

    assessment = Assessment(actual="Completed successfully", surprise_level=0.0)
    turn_result = TurnResult(response_text="Done.")
    frame = _frame()

    await monitor.learn(
        "nous-default",
        "session-1",
        assessment,
        turn_result,
        frame,
        episode_id=str(episode.id),
        session=session,
    )

    # Episode should still be open (not ended)
    ep = await heart.get_episode(episode.id, session=session)
    assert ep.ended_at is None


# ---------------------------------------------------------------------------
# 7. test_learn_records_thought_on_success
# ---------------------------------------------------------------------------


async def test_learn_records_thought_on_success(monitor, brain, session):
    """If decision_id exists and no errors, records a thought."""
    decision = await brain.record(_record_input(), session=session)

    assessment = Assessment(
        actual="Success",
        surprise_level=0.0,
        decision_id=str(decision.id),
    )
    turn_result = TurnResult(response_text="All good.")
    frame = _frame()

    await monitor.learn(
        "nous-default",
        "session-1",
        assessment,
        turn_result,
        frame,
        session=session,
    )

    # Verify thought was recorded
    await session.flush()
    session.expire_all()
    detail = await brain.get(decision.id, session=session)
    assert len(detail.thoughts) >= 1


# ---------------------------------------------------------------------------
# 8. test_error_to_censor_text
# ---------------------------------------------------------------------------


async def test_error_to_censor_text(monitor):
    """_error_to_censor_text produces expected format."""
    tool_result = ToolResult(
        tool_name="shell_exec",
        arguments={"cmd": "rm -rf /"},
        error="Operation not permitted",
    )
    text = monitor._error_to_censor_text(tool_result)
    assert "shell_exec" in text
    assert "Operation not permitted" in text


# ---------------------------------------------------------------------------
# 9. test_is_transient_error
# ---------------------------------------------------------------------------


async def test_is_transient_error(monitor):
    """Transient error patterns correctly identified."""
    assert monitor._is_transient_error("timeout: request exceeded 30s") is True
    assert monitor._is_transient_error("429 Too Many Requests") is True
    assert monitor._is_transient_error("503 Service Unavailable") is True
    assert monitor._is_transient_error("connection refused") is True
    assert monitor._is_transient_error("ECONNRESET") is True
    assert monitor._is_transient_error("ETIMEDOUT") is True
    # Non-transient
    assert monitor._is_transient_error("Permission denied") is False
    assert monitor._is_transient_error("File not found") is False


# ---------------------------------------------------------------------------
# 10. F039 correction-extraction caps wired to Settings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_correction_prompt_uses_input_max_chars(heart, brain, monkeypatch, session):
    """(a) prompt sent to LLM contains chars past position 1000 when correction_input_max_chars=5000."""
    captured: dict = {}

    async def fake_call_background_llm(client, model, system_prompt, user_message, max_tokens=800):
        captured["user_message"] = user_message
        captured["max_tokens"] = max_tokens
        return '{"principle": "Always use uv, not pip when managing deps.", "subject": "tooling", "is_censor": false, "censor_pattern": "", "confidence": 0.9}'

    import nous.handlers as handlers_mod
    monkeypatch.setattr(handlers_mod, "call_background_llm", fake_call_background_llm)

    custom_settings = Settings(
        correction_input_max_chars=5000,
        correction_max_tokens=1024,
        correction_min_principle_chars=20,
    )
    engine = MonitorEngine(brain, heart, custom_settings)
    engine._llm_client = object()  # truthy sentinel — actual call is patched

    # Craft a user message longer than 1000 chars (old hardcoded cap)
    long_message = "no, actually " + "x" * 2000  # 2013 chars total

    await engine.detect_and_extract_correction(
        user_message=long_message,
        ai_response="short response",
        session_id="test-session",
        session=session,
    )

    # The fake LLM is called before the fact store — captured is populated regardless.
    assert "user_message" in captured, (
        "fake LLM was never called — correction pattern was not detected"
    )
    msg = captured["user_message"]
    # The prompt must include chars beyond position 1000
    assert len(msg) > 1000 + len("The user corrected the AI in this exchange:\n\nUser: "), (
        "prompt was truncated at 1000 chars — correction_input_max_chars was NOT wired"
    )
    # More specifically: the long_message starts with "no, actually " + 2000 x's;
    # chars past position 1000 should appear in the prompt.
    assert "x" * 100 in msg, "chars past position 1000 not present in prompt"


@pytest.mark.asyncio
async def test_correction_max_tokens_from_settings(heart, brain, monkeypatch):
    """(b) max_tokens kwarg passed to call_background_llm equals settings.correction_max_tokens."""
    captured: dict = {}

    async def fake_call_background_llm(client, model, system_prompt, user_message, max_tokens=800):
        captured["max_tokens"] = max_tokens
        return '{"principle": "Always use uv, not pip when managing deps.", "subject": "tooling", "is_censor": false, "censor_pattern": "", "confidence": 0.9}'

    import nous.handlers as handlers_mod
    monkeypatch.setattr(handlers_mod, "call_background_llm", fake_call_background_llm)

    custom_settings = Settings(correction_max_tokens=768)
    engine = MonitorEngine(brain, heart, custom_settings)
    engine._llm_client = object()

    await engine.detect_and_extract_correction(
        user_message="no, actually that's wrong",
        ai_response="some response",
        session_id="test-session",
    )

    assert captured.get("max_tokens") == 768, (
        f"Expected max_tokens=768 from settings, got {captured.get('max_tokens')} — "
        "correction_max_tokens was NOT wired"
    )


@pytest.mark.asyncio
async def test_correction_min_principle_chars_from_settings(heart, brain, monkeypatch, session):
    """(c) 25-char principle is stored at default min=20; 15-char principle is dropped."""
    PRINCIPLE_25 = "Always use uv, not pip."   # 23 chars — below old hardcoded 30, above new 20
    PRINCIPLE_15 = "Use uv always."            # 14 chars — below new default 20

    async def fake_llm_25(client, model, system_prompt, user_message, max_tokens=800):
        return (
            f'{{"principle": "{PRINCIPLE_25}", "subject": "tooling", '
            f'"is_censor": false, "censor_pattern": "", "confidence": 0.9}}'
        )

    async def fake_llm_15(client, model, system_prompt, user_message, max_tokens=800):
        return (
            f'{{"principle": "{PRINCIPLE_15}", "subject": "tooling", '
            f'"is_censor": false, "censor_pattern": "", "confidence": 0.9}}'
        )

    import nous.handlers as handlers_mod

    # --- 25-char principle at default min=20 → should be stored ---
    monkeypatch.setattr(handlers_mod, "call_background_llm", fake_llm_25)
    default_settings = Settings()  # correction_min_principle_chars=20
    engine = MonitorEngine(brain, heart, default_settings)
    engine._llm_client = object()

    result_25 = await engine.detect_and_extract_correction(
        user_message="no, actually that's wrong",
        ai_response="some response",
        session_id="test-session",
        session=session,
    )
    assert result_25 is not None, (
        f"Expected {len(PRINCIPLE_25)}-char principle to pass min=20, but got None — "
        "correction_min_principle_chars was NOT wired (still hardcoded 30)"
    )

    # --- 15-char principle at default min=20 → should be dropped ---
    monkeypatch.setattr(handlers_mod, "call_background_llm", fake_llm_15)
    engine2 = MonitorEngine(brain, heart, default_settings)
    engine2._llm_client = object()

    result_15 = await engine2.detect_and_extract_correction(
        user_message="no, actually that's wrong",
        ai_response="some response",
        session_id="test-session",
        session=session,
    )
    assert result_15 is None, (
        f"Expected {len(PRINCIPLE_15)}-char principle to be dropped at min=20, but got a result — "
        "correction_min_principle_chars was NOT wired"
    )
