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
from unittest.mock import AsyncMock, MagicMock, patch

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
            action="steer",
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


async def test_pre_turn_steer_directive_reaches_sections_by_tier(cognitive, heart, settings, session, monkeypatch):
    """F078 R1: a directive-only steer match (no trigger_action) must land in
    TurnContext.sections_by_tier['dynamic'] so it survives the F036 cache-split
    in runner._build_system_prompt. This is the half that was the prod no-op:
    the layer used to append only to the flat system_prompt, which the runner
    ignores under cache_split_system_prompt=True (default).
    """
    from uuid import uuid4
    from nous.heart.schemas import CensorMatch

    # Ensure the cache-split path is the one under test.
    settings.cache_split_system_prompt = True

    directive = "VERIFY-RECIPIENT-BEFORE-SENDING-EMAIL-marker"

    async def _fake_check_censors(text, domain=None, session=None):
        return [
            CensorMatch(
                id=uuid4(),
                trigger_pattern="send.*email",
                action="steer",
                reason="fallback reason should be ignored when instruction present",
                domain=None,
                trigger_action=None,  # directive-only — the prod-shape steer case
                action_instruction=directive,
            )
        ]

    monkeypatch.setattr(heart, "check_censors", _fake_check_censors)

    sid = f"test-steer-tier-{uuid.uuid4().hex[:8]}"
    ctx = await cognitive.pre_turn(
        "nous-default", sid, "please send an email to the team", session=session,
    )

    # The directive must reach the dynamic tier (the cache-split read path),
    # NOT only the flat system_prompt. Reverting the layer's sections_by_tier
    # write turns this assertion red.
    assert ctx.sections_by_tier, "build should have populated sections_by_tier"
    assert directive in ctx.sections_by_tier.get("dynamic", "")
    # And it should NOT have set a blocking flag (steer never blocks).
    assert ctx.censor_blocked is False
    assert ctx.refuse_active is False


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


@pytest.mark.postgres_only
async def test_post_turn_finalizes_deliberation(cognitive, brain, session):
    """If decision_id exists, post_turn calls Brain.update() to finalize."""
    sid = f"test-post-final-{uuid.uuid4().hex[:8]}"
    ctx = await cognitive.pre_turn("nous-default", sid, "should we use Redis?", session=session)
    assert ctx.decision_id is not None

    turn_result = TurnResult(response_text="After evaluating the trade-offs between Redis and Memcached, Redis is the better choice for our caching layer because it supports complex data structures and persistence.")

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


@pytest.mark.postgres_only
async def test_end_session_closes_episode(cognitive, heart, session):
    """end_session ends the active episode with 'completed'."""
    sid = f"test-end-ep-{uuid.uuid4().hex[:8]}"
    ctx = await cognitive.pre_turn("nous-default", sid, "build something important for the project", session=session)
    assert sid in cognitive._active_episodes
    episode_id = cognitive._active_episodes[sid]

    # Do a post_turn to make the session non-trivial (otherwise trivial episodes are soft-deleted)
    turn_result = TurnResult(
        response_text="I've analyzed the requirements and built the initial implementation with proper error handling and test coverage.",
        tool_results=[ToolResult(tool_name="write_file", arguments={"path": "/test"}, result="ok")],
    )
    await cognitive.post_turn("nous-default", sid, turn_result, ctx, session=session)

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


@pytest.mark.postgres_only
async def test_end_session_with_reflection(cognitive, heart, session):
    """Reflection text stored as episode lessons."""
    sid = f"test-end-reflect-{uuid.uuid4().hex[:8]}"
    ctx = await cognitive.pre_turn("nous-default", sid, "build something important for the project", session=session)
    episode_id = cognitive._active_episodes[sid]

    # Do a post_turn to make the session non-trivial (otherwise trivial episodes are soft-deleted)
    turn_result = TurnResult(
        response_text="I've analyzed the requirements and built the initial implementation with proper error handling and test coverage.",
        tool_results=[ToolResult(tool_name="write_file", arguments={"path": "/test"}, result="ok")],
    )
    await cognitive.post_turn("nous-default", sid, turn_result, ctx, session=session)

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


# ---------------------------------------------------------------------------
# F038-2.2: Task field synthesis from conversation history
# ---------------------------------------------------------------------------


async def test_task_synthesis_empty_current_task_fallback(cognitive, heart, session):
    """When current_task is empty and user sends 'yes', scan back for substantive message."""
    sid = f"test-task-synth-{uuid.uuid4().hex[:8]}"

    # First turn with substantive input sets the task
    ctx1 = await cognitive.pre_turn(
        "nous-default", sid, "Help me design a database schema for users",
        session=session,
    )

    # Verify task was set
    wm = await heart.get_working_memory(sid, session=session)
    assert wm is not None
    assert wm.current_task is not None
    assert "database" in wm.current_task.lower()

    # Clear working memory to simulate empty current_task
    await heart.clear_working_memory(sid, session=session)
    await heart.get_or_create_working_memory(sid, session=session)

    # Verify task is now empty
    wm_empty = await heart.get_working_memory(sid, session=session)
    assert wm_empty is not None
    assert not wm_empty.current_task

    # Second turn with short "yes" and conversation history
    ctx2 = await cognitive.pre_turn(
        "nous-default", sid, "yes",
        session=session,
        conversation_messages=[
            "Help me design a database schema for users",
            "yes",
        ],
    )

    # Task should have been synthesized from conversation history
    wm_after = await heart.get_working_memory(sid, session=session)
    assert wm_after is not None
    assert wm_after.current_task is not None
    assert "database" in wm_after.current_task.lower()


async def test_task_synthesis_existing_task_preserved(cognitive, heart, session):
    """When current_task exists and user sends 'ok', preserve existing task."""
    sid = f"test-task-preserve-{uuid.uuid4().hex[:8]}"

    # First turn sets the task
    await cognitive.pre_turn(
        "nous-default", sid, "Build the authentication module",
        session=session,
    )

    wm = await heart.get_working_memory(sid, session=session)
    assert wm is not None
    original_task = wm.current_task
    assert original_task is not None

    # Second turn with "ok" — existing task should be preserved
    await cognitive.pre_turn(
        "nous-default", sid, "ok",
        session=session,
        conversation_messages=[
            "Build the authentication module",
            "ok",
        ],
    )

    wm_after = await heart.get_working_memory(sid, session=session)
    assert wm_after is not None
    # Task should remain the same (not overwritten)
    assert wm_after.current_task == original_task


# ---------------------------------------------------------------------------
# CR-4: stale in-process session-state sweep (orphaned-session leak guard)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cr4_sweep_evicts_stale_sessions(cognitive):
    import time as _time

    from nous.cognitive.schemas import SessionMetadata

    timeout = float(getattr(cognitive._settings, "session_timeout", 1800) or 1800)
    now = _time.monotonic()
    cognitive._session_metadata["old"] = SessionMetadata()
    cognitive._session_user_messages["old"] = ["x"]
    cognitive._session_metadata["live"] = SessionMetadata()
    cognitive._session_last_activity["old"] = now - (timeout * 2 + 10)
    cognitive._session_last_activity["live"] = now
    # Force a sweep regardless of the absolute monotonic clock: the rate-limit
    # guard compares (now - last_sweep) against max(timeout, 60), so 0.0 only
    # forces a sweep when monotonic() already exceeds the timeout (true on a
    # long-running host, false in a fresh CI container — that gap is what failed
    # CI). Subtract the interval explicitly instead.
    cognitive._last_session_sweep = now - max(timeout, 60.0) - 1.0

    cognitive._sweep_stale_sessions()

    assert "old" not in cognitive._session_metadata
    assert "old" not in cognitive._session_user_messages
    assert "old" not in cognitive._session_last_activity
    assert "live" in cognitive._session_metadata
    assert "live" in cognitive._session_last_activity


@pytest.mark.asyncio
async def test_cr4_sweep_is_rate_limited(cognitive):
    import time as _time

    from nous.cognitive.schemas import SessionMetadata

    timeout = float(getattr(cognitive._settings, "session_timeout", 1800) or 1800)
    now = _time.monotonic()
    cognitive._session_metadata["old"] = SessionMetadata()
    cognitive._session_last_activity["old"] = now - (timeout * 2 + 10)
    cognitive._last_session_sweep = now  # swept just now -> next call is a no-op

    cognitive._sweep_stale_sessions()

    assert "old" in cognitive._session_metadata  # rate-limited, not yet swept


# ---------------------------------------------------------------------------
# TestMemoryFidelityCaps — 2026-07-02: capture-time truncations honour Settings
# These tests use fully-mocked dependencies (no real DB) following the same
# pattern as test_noise_reduction.py::_make_cognitive_layer().
# ---------------------------------------------------------------------------


def _make_layer_with_settings(**setting_overrides):
    """Build a CognitiveLayer with mocked deps + specific Settings values.

    Follows the _make_cognitive_layer() pattern from test_noise_reduction.py.
    Returns (layer, mock_heart) so callers can inspect call args.
    """
    mock_brain = MagicMock()
    mock_brain.db = MagicMock()
    mock_brain.embeddings = None
    mock_brain.agent_id = "test-agent"

    mock_heart = MagicMock()
    mock_heart.episodes = MagicMock()
    mock_heart.episodes.embeddings = None
    mock_heart.start_episode = AsyncMock()
    mock_heart.end_episode = AsyncMock()
    mock_heart.deactivate_episode = AsyncMock()
    mock_heart.search_recent_episodes_by_embedding = AsyncMock(return_value=[])
    mock_heart.list_censors = AsyncMock(return_value=[])
    mock_heart.get_or_create_working_memory = AsyncMock()
    mock_heart.focus = AsyncMock()

    # Build a real Settings with default values, then apply overrides as
    # attribute mutations (Settings has no extra="forbid" on model_config).
    from nous.config import Settings as _Settings

    settings = _Settings()
    for key, val in setting_overrides.items():
        object.__setattr__(settings, key, val)

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
            mock_brain, mock_heart, settings, identity_prompt="Test."
        )

    layer._heart = mock_heart
    layer._brain = mock_brain
    return layer, mock_heart


@pytest.mark.asyncio
class TestMemoryFidelityCaps:
    """2026-07-02 scan: capture-time truncations honour Settings."""

    async def test_pre_turn_transcript_capture_honors_message_cap(self):
        """pre_turn appends 'User: <input[:cap]>' respecting transcript_message_max_chars.

        Covers the User-line seam at layer.py:462. pre_turn is invoked with
        fully-mocked dependencies; it may raise later in the loop (mocked frame
        engine etc.), which is fine — the transcript append happens early, and
        the assertion below only holds if the REAL code at the capture seam ran
        with the Settings-driven cap. If someone re-hardcodes [:500] there, the
        assertion fails.
        """
        from nous.cognitive.schemas import SessionMetadata

        layer, _ = _make_layer_with_settings(transcript_message_max_chars=2000)
        sid = "s-preturn-transcript-cap"
        long_input = "x" * 3000

        try:
            await layer.pre_turn("test-agent", sid, long_input)
        except Exception:
            # Later pipeline stages (frame selection, context build) run against
            # MagicMocks and may raise — the capture seam under test has already
            # executed by then.
            pass

        meta = layer._session_metadata[sid]
        assert meta.transcript, "pre_turn did not reach the transcript capture seam"
        assert meta.transcript[0] == f"User: {'x' * 2000}", (
            f"Expected 2000-char cap on User line, got {len(meta.transcript[0]) - 6} chars"
        )

    async def test_post_turn_transcript_capture_honors_message_cap(self):
        """post_turn appends 'Assistant: <text[:cap]>' respecting transcript_message_max_chars.

        Covers the Assistant-line seam at layer.py:1287. post_turn can be driven
        end-to-end with mocked async dependencies since every async step before
        the transcript append is guarded by try/except.
        """
        from nous.cognitive.schemas import SessionMetadata, TurnContext, TurnResult, ToolResult

        layer, _ = _make_layer_with_settings(transcript_message_max_chars=2000)
        sid = "s-transcript-cap"

        # Provide a session-level metadata so the setdefault path is populated
        layer._session_metadata[sid] = SessionMetadata()

        # Mock all async sub-engines so their await calls don't raise
        layer._monitor = MagicMock()
        layer._monitor.assess = AsyncMock(
            return_value=MagicMock(surprise_level=0.0, actual="")
        )
        layer._monitor.learn = AsyncMock(
            return_value=MagicMock(surprise_level=0.0, actual="")
        )
        layer._monitor.detect_and_extract_correction = AsyncMock(return_value=None)
        layer._deliberation = MagicMock()
        layer._usage_tracker = None  # skip usage tracking
        layer._brain.emit_event = AsyncMock()

        from nous.cognitive.schemas import FrameSelection

        long_response = "a" * 3000
        turn_result = TurnResult(response_text=long_response, tool_results=[])
        turn_context = TurnContext(
            system_prompt="",
            frame=FrameSelection(
                frame_id="conversation",
                frame_name="Conversation",
                confidence=1.0,
                match_method="default",
            ),
            decision_id=None,
        )

        await layer.post_turn("test-agent", sid, turn_result, turn_context)

        meta = layer._session_metadata[sid]
        last_transcript = meta.transcript[-1]
        assert last_transcript == f"Assistant: {'a' * 2000}", (
            f"Expected 2000-char cap, got {len(last_transcript) - 11} chars"
        )

    async def test_lessons_cap_honored(self):
        """end_session passes reflection[:episode_lessons_max_chars] to heart.end_episode."""
        import uuid as _uuid

        layer, mock_heart = _make_layer_with_settings(episode_lessons_max_chars=1000)

        sid = "s-lessons-cap"
        episode_id = str(_uuid.uuid4())
        from nous.cognitive.schemas import SessionMetadata

        # Non-trivial session so is_trivial=False and end_episode fires
        layer._active_episodes[sid] = episode_id
        layer._session_metadata[sid] = SessionMetadata(
            turn_count=2,
            tools_used={"bash"},
            total_user_chars=300,
            total_assistant_chars=300,
        )
        layer._brain.emit_event = AsyncMock()
        layer._monitor = MagicMock()
        layer._monitor._session_censor_counts = {}

        long_reflection = "r" * 1500  # exceeds 1000-char cap
        await layer.end_session("test-agent", sid, reflection=long_reflection)

        mock_heart.end_episode.assert_called_once()
        call_kwargs = mock_heart.end_episode.call_args.kwargs
        lessons = call_kwargs.get("lessons_learned")
        assert lessons is not None, "lessons_learned should not be None when reflection given"
        assert lessons == ["r" * 1000], (
            f"Expected lesson truncated to 1000 chars, got {len(lessons[0])}"
        )

    async def test_trivial_gate_uses_setting(self):
        """With episode_min_content_length=0, a tiny single-turn session is NOT soft-deleted."""
        import uuid as _uuid

        layer, mock_heart = _make_layer_with_settings(episode_min_content_length=0)

        sid = "s-trivial-gate"
        episode_id = str(_uuid.uuid4())
        from nous.cognitive.schemas import SessionMetadata

        # Single turn, no tools, tiny content — would normally be trivial at default 200
        layer._active_episodes[sid] = episode_id
        layer._session_metadata[sid] = SessionMetadata(
            turn_count=1,
            tools_used=set(),
            total_user_chars=5,
            total_assistant_chars=10,  # total=15, well below default 200
        )
        layer._brain.emit_event = AsyncMock()
        layer._monitor = MagicMock()
        layer._monitor._session_censor_counts = {}

        await layer.end_session("test-agent", sid)

        # With gate=0: 15 >= 0, so NOT trivial → end_episode called, deactivate NOT called
        mock_heart.deactivate_episode.assert_not_called()
        mock_heart.end_episode.assert_called_once()

    async def test_episode_seed_truncated_to_setting(self):
        """EpisodeInput.summary passed to heart.start_episode is capped at
        episode_seed_summary_chars — regression guard for layer.py:~821.

        We set the cap to 300 and pass a 500-char user_input.  The episode
        creation branch runs when no active episode exists AND the session
        passes _should_create_episode.  We mock _is_duplicate_episode to
        return False so the creation branch always fires, then inspect the
        EpisodeInput.summary captured by heart.start_episode.

        pre_turn runs through MagicMock-backed intent/context/deliberation
        engines that may raise; those are all wrapped in except blocks in
        the real code, so the episode seam (line ~821) is reachable.
        The IntentClassifier mock must return a real IntentSignals so the
        temporal_recency comparison at line ~664 doesn't TypeError first.
        """
        import uuid as _uuid
        from nous.cognitive.intent import IntentSignals, RetrievalPlan

        layer, mock_heart = _make_layer_with_settings(episode_seed_summary_chars=300)

        sid = "s-seed-cap"
        long_input = "z" * 500  # exceeds the 300-char cap

        # start_episode returns an object with .id; wire it up
        fake_episode = MagicMock()
        fake_episode.id = _uuid.uuid4()
        mock_heart.start_episode = AsyncMock(return_value=fake_episode)

        # Wire intent classifier to return real objects so temporal_recency
        # comparison (line ~664) doesn't crash with MagicMock > float TypeError.
        fake_signals = IntentSignals(frame_type="conversation")
        fake_plan = RetrievalPlan()
        layer._intent_classifier.classify = MagicMock(return_value=fake_signals)
        layer._intent_classifier.plan_retrieval = MagicMock(return_value=fake_plan)

        # Wire frame engine to return a real FrameSelection so EpisodeInput's
        # frame_used field gets a str rather than a MagicMock (which fails
        # Pydantic validation and causes the episode try/except to swallow it).
        from nous.cognitive.schemas import FrameSelection as _FS
        fake_frame = _FS(
            frame_id="conversation",
            frame_name="Conversation",
            confidence=1.0,
            match_method="default",
        )
        layer._frames._default_selection = MagicMock(return_value=fake_frame)

        # No pre-existing active episode → creation branch fires
        assert sid not in layer._active_episodes

        # Patch _is_duplicate_episode to return False (no duplicate)
        layer._is_duplicate_episode = AsyncMock(return_value=False)
        try:
            await layer.pre_turn("test-agent", sid, long_input)
        except Exception:
            # Later pipeline stages (context build, deliberation, working memory)
            # run against MagicMocks and may raise — the episode creation seam
            # at ~821 is protected by its own try/except, so start_episode
            # fires regardless of those later failures.
            pass

        # start_episode MUST have been called with the truncated seed
        mock_heart.start_episode.assert_called_once()
        call_args = mock_heart.start_episode.call_args
        # First positional arg is EpisodeInput
        episode_input = call_args.args[0] if call_args.args else call_args.kwargs.get("episode_input")
        assert episode_input is not None, "start_episode was not called with an EpisodeInput"
        assert len(episode_input.summary) == 300, (
            f"Expected seed truncated to 300 chars, got {len(episode_input.summary)}"
        )
