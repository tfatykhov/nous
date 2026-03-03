"""Integration tests for CognitiveLayer — the full Nous Loop.

All tests use real Postgres via the SAVEPOINT fixture from conftest.py.
Tests exercise pre_turn, post_turn, end_session, and full loop scenarios.

Key plan adjustments applied:
- P1-1: brain.db (public), not brain._db
- P1-2: RecordInput with pydantic models
- P1-4: action not severity for censors
- P1-5: pydantic input models for Heart methods
- P2-2: episodes only end in end_session
- P2-3: surprise uses structural signals only
- P2-7: events emitted via Brain.emit_event()
- P3-9: test_post_turn without prior pre_turn
- P3-10: test_end_session without pre_turn
"""

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select

from nous.brain.brain import Brain
from nous.brain.schemas import ReasonInput, RecordInput
from nous.cognitive.layer import CognitiveLayer
from nous.cognitive.schemas import (
    Assessment,
    ToolResult,
    TurnContext,
    TurnResult,
)
from nous.heart import CensorInput, FactInput, ProcedureInput
from nous.storage.models import Event

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def brain(db, settings):
    """Brain without embeddings for integration tests."""
    b = Brain(database=db, settings=settings)
    yield b
    await b.close()


@pytest_asyncio.fixture
async def cognitive(brain, heart, settings):
    """CognitiveLayer wired to Brain and Heart."""
    return CognitiveLayer(brain, heart, settings, identity_prompt="You are Nous.")


def _record_input(**overrides) -> RecordInput:
    """Build a RecordInput with sensible defaults."""
    defaults = dict(
        description="Integration test decision",
        confidence=0.85,
        category="architecture",
        stakes="medium",
        reasons=[ReasonInput(type="analysis", text="Test")],
    )
    defaults.update(overrides)
    return RecordInput(**defaults)


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _seed_data(brain, heart, session):
    """Pre-seed Brain and Heart with realistic data."""
    # 3 decisions
    for i in range(3):
        await brain.record(
            _record_input(description=f"Seed decision {i}"),
            session=session,
        )

    # 5 facts
    for i in range(5):
        await heart.learn(
            FactInput(
                content=f"Seed fact {i}: important information for context",
                category="technical",
                confidence=0.9,
            ),
            session=session,
        )

    # 2 procedures
    for i in range(2):
        await heart.store_procedure(
            ProcedureInput(
                name=f"Seed procedure {i}",
                domain="testing",
                core_patterns=[f"seed-pattern-{i}"],
            ),
            session=session,
        )

    # 1 active censor
    await heart.add_censor(
        CensorInput(
            trigger_pattern="seed censor trigger",
            reason="Seed censor reason",
            action="warn",
        ),
        session=session,
    )


# ---------------------------------------------------------------------------
# pre_turn tests
# ---------------------------------------------------------------------------


async def test_pre_turn_selects_frame(cognitive, session):
    """pre_turn returns TurnContext with correct frame."""
    sid = f"test-pre-frame-{uuid.uuid4().hex[:8]}"
    ctx = await cognitive.pre_turn("nous-default", sid, "should we use Redis?", session=session)

    assert isinstance(ctx, TurnContext)
    assert ctx.frame.frame_id == "decision"


async def test_pre_turn_builds_context(cognitive, brain, heart, session):
    """pre_turn builds non-empty system prompt."""
    await _seed_data(brain, heart, session)
    sid = f"test-pre-ctx-{uuid.uuid4().hex[:8]}"
    ctx = await cognitive.pre_turn("nous-default", sid, "build something", session=session)

    assert len(ctx.system_prompt) > 0
    assert ctx.context_token_estimate > 0


async def test_pre_turn_starts_deliberation(cognitive, brain, session):
    """Decision frame -> decision_id is set."""
    sid = f"test-pre-delib-{uuid.uuid4().hex[:8]}"
    ctx = await cognitive.pre_turn("nous-default", sid, "should we migrate to PostgreSQL?", session=session)

    assert ctx.decision_id is not None
    # Verify decision exists in Brain
    detail = await brain.get(uuid.UUID(ctx.decision_id), session=session)
    assert detail is not None
    assert detail.description.startswith("Plan:")


async def test_pre_turn_no_deliberation_conversation(cognitive, session):
    """Conversation frame -> decision_id is None."""
    sid = f"test-pre-conv-{uuid.uuid4().hex[:8]}"
    ctx = await cognitive.pre_turn("nous-default", sid, "xyzzy foobar nonsense", session=session)

    # Conversation (default frame) should NOT start deliberation
    assert ctx.decision_id is None


async def test_pre_turn_starts_episode(cognitive, heart, session):
    """First pre_turn creates an episode for the session."""
    sid = f"test-pre-ep-{uuid.uuid4().hex[:8]}"
    await cognitive.pre_turn("nous-default", sid, "build something", session=session)

    # Episode should be tracked
    assert sid in cognitive._active_episodes


async def test_pre_turn_reuses_episode(cognitive, session):
    """Second pre_turn with same session reuses existing episode."""
    sid = f"test-pre-reuse-{uuid.uuid4().hex[:8]}"
    await cognitive.pre_turn("nous-default", sid, "build something", session=session)
    episode_id_1 = cognitive._active_episodes.get(sid)

    await cognitive.pre_turn("nous-default", sid, "continue building", session=session)
    episode_id_2 = cognitive._active_episodes.get(sid)

    assert episode_id_1 == episode_id_2


async def test_pre_turn_updates_working_memory(cognitive, heart, session):
    """pre_turn sets working memory with input and frame."""
    sid = f"test-pre-wm-{uuid.uuid4().hex[:8]}"
    await cognitive.pre_turn("nous-default", sid, "build the API endpoint", session=session)

    wm = await heart.get_working_memory(sid, session=session)
    assert wm is not None
    assert wm.current_task is not None
    assert "API endpoint" in wm.current_task or "build" in wm.current_task.lower()


# ---------------------------------------------------------------------------
# post_turn tests
# ---------------------------------------------------------------------------


async def test_post_turn_assesses(cognitive, session):
    """post_turn returns Assessment."""
    sid = f"test-post-assess-{uuid.uuid4().hex[:8]}"
    ctx = await cognitive.pre_turn("nous-default", sid, "build something", session=session)
    turn_result = TurnResult(response_text="Built successfully.")

    assessment = await cognitive.post_turn("nous-default", sid, turn_result, ctx, session=session)

    assert isinstance(assessment, Assessment)
    assert assessment.surprise_level == 0.0


async def test_post_turn_finalizes_deliberation(cognitive, brain, session):
    """If decision_id exists, post_turn calls Brain.update() to finalize."""
    sid = f"test-post-final-{uuid.uuid4().hex[:8]}"
    ctx = await cognitive.pre_turn("nous-default", sid, "should we use Redis?", session=session)
    assert ctx.decision_id is not None

    turn_result = TurnResult(response_text="Redis is a good choice for caching.")

    await cognitive.post_turn("nous-default", sid, turn_result, ctx, session=session)

    # Decision should be updated
    detail = await brain.get(uuid.UUID(ctx.decision_id), session=session)
    assert detail is not None
    # Confidence should be updated from initial 0.5
    assert detail.confidence >= 0.5


async def test_post_turn_no_finalize_without_decision(cognitive, session):
    """Without decision_id, post_turn doesn't try to update Brain."""
    sid = f"test-post-nofinal-{uuid.uuid4().hex[:8]}"
    ctx = await cognitive.pre_turn("nous-default", sid, "xyzzy nonsense input", session=session)
    assert ctx.decision_id is None

    turn_result = TurnResult(response_text="Hello there!")
    assessment = await cognitive.post_turn("nous-default", sid, turn_result, ctx, session=session)

    # Should complete without error
    assert isinstance(assessment, Assessment)


async def test_post_turn_creates_censor_on_failure(cognitive, heart, session):
    """Turn-level error with tool errors creates a censor via MonitorEngine.

    MonitorEngine.learn() only creates censors when surprise > 0.7.
    Tool errors alone give surprise=0.3 (below threshold).
    A turn-level error gives surprise=0.9 AND tool errors provide censor candidates.
    """
    sid = f"test-post-censor-{uuid.uuid4().hex[:8]}"
    ctx = await cognitive.pre_turn("nous-default", sid, "build something", session=session)

    turn_result = TurnResult(
        response_text="Failed to write file.",
        error="Turn failed due to permission error",
        tool_results=[
            ToolResult(
                tool_name="file_write",
                arguments={"path": "/restricted/path"},
                error="Permission denied: /restricted/path",
            )
        ],
    )

    await cognitive.post_turn("nous-default", sid, turn_result, ctx, session=session)

    # A censor should have been created (surprise=0.9 > 0.7 threshold)
    censors = await heart.list_censors(session=session)
    # At least one censor related to file_write
    assert any("file_write" in c.trigger_pattern or "restricted" in c.trigger_pattern for c in censors)


async def test_post_turn_emits_event(cognitive, session):
    """post_turn emits a turn_completed event."""
    sid = f"test-post-event-{uuid.uuid4().hex[:8]}"
    ctx = await cognitive.pre_turn("nous-default", sid, "build something", session=session)
    turn_result = TurnResult(response_text="Done.")

    await cognitive.post_turn("nous-default", sid, turn_result, ctx, session=session)

    # Check for turn_completed event
    result = await session.execute(
        select(Event).where(
            Event.agent_id == "nous-default",
            Event.event_type == "turn_completed",
        )
    )
    events = result.scalars().all()
    assert len(events) >= 1


# ---------------------------------------------------------------------------
# end_session tests
# ---------------------------------------------------------------------------


async def test_end_session_closes_episode(cognitive, heart, session):
    """end_session ends the active episode with 'completed'."""
    sid = f"test-end-ep-{uuid.uuid4().hex[:8]}"
    await cognitive.pre_turn("nous-default", sid, "build something", session=session)
    assert sid in cognitive._active_episodes
    episode_id = cognitive._active_episodes[sid]

    await cognitive.end_session("nous-default", sid, session=session)

    # Episode should be ended
    ep = await heart.get_episode(uuid.UUID(episode_id), session=session)
    assert ep.ended_at is not None
    assert ep.outcome == "success"

    # Session should be removed from tracking
    assert sid not in cognitive._active_episodes


async def test_end_session_emits_event(cognitive, session):
    """end_session emits a session_ended event."""
    sid = f"test-end-event-{uuid.uuid4().hex[:8]}"
    await cognitive.pre_turn("nous-default", sid, "build something", session=session)

    await cognitive.end_session("nous-default", sid, session=session)

    result = await session.execute(
        select(Event).where(
            Event.agent_id == "nous-default",
            Event.event_type == "session_ended",
        )
    )
    events = result.scalars().all()
    assert len(events) >= 1


async def test_end_session_idempotent(cognitive, session):
    """Calling end_session twice doesn't error."""
    sid = f"test-end-idemp-{uuid.uuid4().hex[:8]}"
    await cognitive.pre_turn("nous-default", sid, "build something", session=session)

    await cognitive.end_session("nous-default", sid, session=session)
    # Second call should be safe (no active episode)
    await cognitive.end_session("nous-default", sid, session=session)


async def test_end_session_with_reflection(cognitive, heart, session):
    """Reflection text stored as episode lessons."""
    sid = f"test-end-reflect-{uuid.uuid4().hex[:8]}"
    await cognitive.pre_turn("nous-default", sid, "build something", session=session)
    episode_id = cognitive._active_episodes[sid]

    await cognitive.end_session(
        "nous-default",
        sid,
        reflection="The task went well. We completed the API endpoint.",
        session=session,
    )

    ep = await heart.get_episode(uuid.UUID(episode_id), session=session)
    assert ep.ended_at is not None


async def test_end_session_reflection_extracts_facts(cognitive, heart, session):
    """'learned: X' lines in reflection become facts."""
    sid = f"test-end-facts-{uuid.uuid4().hex[:8]}"
    await cognitive.pre_turn("nous-default", sid, "build something", session=session)

    reflection = (
        "Session summary:\n"
        "- learned: Always validate input before database writes\n"
        "- learned: Use connection pooling for better performance\n"
        "The rest was straightforward."
    )

    await cognitive.end_session("nous-default", sid, reflection=reflection, session=session)

    # Check that facts were extracted
    facts = await heart.search_facts("validate input", session=session)
    facts2 = await heart.search_facts("connection pooling", session=session)

    # At least some of the learned facts should have been stored
    all_facts = facts + facts2
    assert len(all_facts) >= 1


async def test_end_session_without_pre_turn(cognitive, session):
    """end_session without prior pre_turn handles gracefully (P3-10)."""
    sid = f"test-end-nopre-{uuid.uuid4().hex[:8]}"
    # No pre_turn called — no episode exists
    await cognitive.end_session("nous-default", sid, session=session)
    # Should not raise


# ---------------------------------------------------------------------------
# full loop tests
# ---------------------------------------------------------------------------


async def test_full_loop_decision(cognitive, brain, heart, session):
    """Full loop: pre_turn(decision) -> post_turn(success) -> end_session."""
    await _seed_data(brain, heart, session)
    sid = f"test-loop-decision-{uuid.uuid4().hex[:8]}"

    # pre_turn
    ctx = await cognitive.pre_turn("nous-default", sid, "should we use Redis for caching?", session=session)
    assert ctx.frame.frame_id == "decision"
    assert ctx.decision_id is not None
    assert len(ctx.system_prompt) > 0

    # post_turn (success)
    turn_result = TurnResult(response_text="Yes, Redis is the best choice for our caching layer.")
    assessment = await cognitive.post_turn("nous-default", sid, turn_result, ctx, session=session)
    assert assessment.surprise_level == 0.0

    # end_session
    await cognitive.end_session(
        "nous-default",
        sid,
        reflection="- learned: Redis is optimal for ephemeral caching",
        session=session,
    )
    assert sid not in cognitive._active_episodes


async def test_full_loop_with_error(cognitive, brain, heart, session):
    """Full loop: pre_turn(task) -> post_turn(turn error + tool error) -> censor created.

    Censors only created when surprise > 0.7. Turn-level error gives 0.9,
    tool errors provide censor candidates via _error_to_censor_text().
    """
    sid = f"test-loop-error-{uuid.uuid4().hex[:8]}"

    # pre_turn
    ctx = await cognitive.pre_turn("nous-default", sid, "deploy the application", session=session)

    # post_turn with turn-level error AND tool error
    turn_result = TurnResult(
        response_text="Deployment failed.",
        error="Deployment process encountered a fatal error",
        tool_results=[
            ToolResult(
                tool_name="deploy",
                arguments={"target": "production"},
                error="Insufficient permissions for production deployment",
            )
        ],
    )
    assessment = await cognitive.post_turn("nous-default", sid, turn_result, ctx, session=session)
    assert assessment.surprise_level == 0.9

    # end_session
    await cognitive.end_session("nous-default", sid, session=session)

    # Verify censor was created (surprise=0.9 > 0.7 threshold)
    censors = await heart.list_censors(session=session)
    assert any("deploy" in c.trigger_pattern or "production" in c.trigger_pattern for c in censors)


async def test_full_loop_conversation(cognitive, session):
    """Full loop: conversation is lightweight, no deliberation, no censor."""
    sid = f"test-loop-conv-{uuid.uuid4().hex[:8]}"

    # pre_turn (conversation — no keyword match -> default)
    ctx = await cognitive.pre_turn("nous-default", sid, "xyzzy foobar gibberish", session=session)
    assert ctx.decision_id is None

    # post_turn
    turn_result = TurnResult(response_text="I'm not sure what you mean.")
    assessment = await cognitive.post_turn("nous-default", sid, turn_result, ctx, session=session)
    assert assessment.surprise_level == 0.0

    # end_session
    await cognitive.end_session("nous-default", sid, session=session)


# ---------------------------------------------------------------------------
# _is_informational pattern tests (009.5)
# ---------------------------------------------------------------------------


def _turn_result(text: str, tool_results: list[ToolResult] | None = None) -> TurnResult:
    """Build a TurnResult with defaults for pattern tests."""
    return TurnResult(
        response_text=text,
        tool_results=tool_results or [],
    )


# 009.5: Parametrized test for new informational patterns
@pytest.mark.parametrize(
    "pattern",
    [
        # Completion / status updates
        "Done!",
        "Done.",
        "Completed!",
        "Finished!",
        "On it!",
        "Created!",
        "Pushed to main successfully.",
        "Review complete — no issues found.",
        "Spec scores 8/10 on all criteria.",
        "Task is running in the background.",
        # Transition phrases
        "Now let me check the database schema.",
        "Next I'll update the configuration.",
        "Moving on to the deployment step.",
        "Let me check the test results.",
        "Let me look at the error logs.",
        "I'll start with the backend changes.",
        "Starting with the API endpoint refactor.",
        # Report phrases
        "Here's the result of the analysis.",
        "Here are the results from the test suite.",
        "PR #42 is ready for review.",
        "PR created and pushed to remote.",
    ],
)
async def test_is_informational_new_patterns(cognitive, pattern):
    """Each new 009.5 pattern is detected as informational."""
    tr = _turn_result(pattern)
    assert cognitive._is_informational(tr) is True


# ---------------------------------------------------------------------------
# _is_action_report tests (009.5)
# ---------------------------------------------------------------------------


_TOOL_RESULTS = [ToolResult(tool_name="write_file", result="ok")]


async def test_is_action_report_with_tools_and_markers(cognitive):
    """Tools used + 2+ report markers in first 300 chars -> True."""
    tr = _turn_result(
        "Done. I created the migration file and updated the schema.",
        tool_results=_TOOL_RESULTS,
    )
    assert cognitive._is_action_report(tr) is True


async def test_is_action_report_no_tools(cognitive):
    """No tool calls -> False regardless of markers."""
    tr = _turn_result(
        "Done. I created the migration file and updated the schema.",
    )
    assert cognitive._is_action_report(tr) is False


async def test_is_action_report_one_marker(cognitive):
    """Tools + only 1 marker -> False (threshold is 2)."""
    tr = _turn_result(
        "The file has been saved to disk.",
        tool_results=_TOOL_RESULTS,
    )
    assert cognitive._is_action_report(tr) is False


async def test_is_informational_delegates_to_action_report(cognitive):
    """_is_informational returns True when _is_action_report fires."""
    tr = _turn_result(
        "I pushed the changes and merged the PR successfully.",
        tool_results=_TOOL_RESULTS,
    )
    # This text doesn't match any keyword pattern, but has 2+ action markers
    # ("pushed", "merged") with tool results, so _is_action_report catches it
    assert cognitive._is_informational(tr) is True


# ---------------------------------------------------------------------------
# 009.5: Integration — task frame no longer triggers deliberation
# ---------------------------------------------------------------------------


async def test_task_frame_no_deliberation(cognitive):
    """009.5: Task frame no longer triggers auto-deliberation."""
    from nous.cognitive.schemas import FrameSelection

    frame = FrameSelection(
        frame_id="task",
        frame_name="Task Execution",
        confidence=0.9,
        match_method="pattern",
        default_category="tooling",
        default_stakes="medium",
    )
    result = await cognitive._deliberation.should_deliberate(frame)
    assert result is False


# ---------------------------------------------------------------------------
# Working memory items & threads (issue #34)
# ---------------------------------------------------------------------------


async def test_pre_turn_loads_recalled_items_to_working_memory(cognitive, heart, session):
    """_load_recalled_to_working_memory loads high-scoring items into working memory.

    Tests the helper directly to avoid pre-existing context build issues.
    With score >= 0.7, items are loaded; below 0.7 they're filtered out.
    """
    from nous.cognitive.schemas import BuildResult

    sid = f"test-wm-items-{uuid.uuid4().hex[:8]}"
    await heart.get_or_create_working_memory(sid, session=session)

    fact_id = str(uuid.uuid4())
    decision_id = str(uuid.uuid4())
    low_score_id = str(uuid.uuid4())

    build_result = BuildResult(
        system_prompt="test",
        recalled_ids={
            "fact": [fact_id],
            "decision": [decision_id, low_score_id],
            "procedure": [],
            "episode": [],
        },
        recalled_content_map={
            fact_id: "Important fact about testing",
            decision_id: "Use Redis for caching",
            low_score_id: "Low relevance decision",
        },
        recalled_score_map={
            fact_id: 0.85,
            decision_id: 0.75,
            low_score_id: 0.3,  # Below 0.7 threshold — should be filtered
        },
    )

    await cognitive._load_recalled_to_working_memory(sid, build_result, session=session)

    wm = await heart.get_working_memory(sid, session=session)
    assert wm is not None
    # Only items with score >= 0.7 should be loaded (fact + decision, not low_score)
    assert wm.item_count == 2
    summaries = {item.summary for item in wm.items}
    assert "Important fact about testing" in summaries
    assert "Use Redis for caching" in summaries


async def test_post_turn_adds_error_thread(cognitive, heart, session):
    """Tool errors in post_turn create high-priority open threads."""
    sid = f"test-wm-thread-err-{uuid.uuid4().hex[:8]}"

    # Manually create working memory (bypass pre_turn's context build)
    await heart.get_or_create_working_memory(sid, session=session)

    # Build a minimal TurnContext (skip pre_turn to avoid schema issues)
    from nous.cognitive.schemas import FrameSelection
    ctx = TurnContext(
        system_prompt="test",
        frame=FrameSelection(
            frame_id="task", frame_name="Task", confidence=0.9, match_method="pattern",
        ),
    )

    turn_result = TurnResult(
        response_text="Failed to write file.",
        tool_results=[
            ToolResult(
                tool_name="write_file",
                arguments={"path": "/test"},
                error="Permission denied",
            )
        ],
    )

    await cognitive.post_turn("nous-default", sid, turn_result, ctx, session=session)

    wm = await heart.get_working_memory(sid, session=session)
    assert wm is not None
    assert len(wm.open_threads) >= 1
    error_threads = [t for t in wm.open_threads if t.priority == "high"]
    assert len(error_threads) >= 1
    assert "write_file" in error_threads[0].description


async def test_post_turn_resolves_thread_on_success(cognitive, heart, session):
    """Successful tool calls resolve matching open threads."""
    sid = f"test-wm-thread-resolve-{uuid.uuid4().hex[:8]}"

    # Manually create working memory
    await heart.get_or_create_working_memory(sid, session=session)

    from nous.cognitive.schemas import FrameSelection
    ctx = TurnContext(
        system_prompt="test",
        frame=FrameSelection(
            frame_id="task", frame_name="Task", confidence=0.9, match_method="pattern",
        ),
    )

    # First: create an error thread via failed tool
    error_result = TurnResult(
        response_text="Failed.",
        tool_results=[
            ToolResult(tool_name="write_file", arguments={}, error="Permission denied"),
        ],
    )
    await cognitive.post_turn("nous-default", sid, error_result, ctx, session=session)

    wm = await heart.get_working_memory(sid, session=session)
    assert len(wm.open_threads) >= 1

    # Second turn: same tool succeeds
    success_result = TurnResult(
        response_text="File written.",
        tool_results=[
            ToolResult(tool_name="write_file", arguments={}, result="ok"),
        ],
    )
    await cognitive.post_turn("nous-default", sid, success_result, ctx, session=session)

    wm2 = await heart.get_working_memory(sid, session=session)
    # The thread mentioning write_file should be resolved
    write_threads = [t for t in wm2.open_threads if "write_file" in t.description]
    assert len(write_threads) == 0


async def test_end_session_clears_working_memory(cognitive, heart, session):
    """end_session clears working memory for the session."""
    sid = f"test-wm-clear-{uuid.uuid4().hex[:8]}"

    # Manually create working memory (bypass pre_turn)
    await heart.get_or_create_working_memory(sid, session=session)

    # Verify working memory exists
    wm = await heart.get_working_memory(sid, session=session)
    assert wm is not None

    await cognitive.end_session("nous-default", sid, session=session)

    # Working memory should be cleared
    wm_after = await heart.get_working_memory(sid, session=session)
    assert wm_after is None
