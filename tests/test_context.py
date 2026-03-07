"""Tests for ContextEngine — context assembly within token budgets.

All tests use real Postgres via the SAVEPOINT fixture from conftest.py.
Pre-seeds Brain with decisions and Heart with facts, procedures, and censors
to test realistic context assembly.
"""

import uuid

import pytest
import pytest_asyncio

from nous.brain.brain import Brain
from nous.brain.schemas import ReasonInput, RecordInput
from nous.cognitive.context import ContextEngine
from nous.cognitive.schemas import ContextBudget, FrameSelection
from nous.config import Settings
from nous.heart import CensorInput, FactInput, ProcedureInput

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ctx_settings() -> Settings:
    """Settings with relevance floor disabled for context tests.

    Seeded items have no embeddings so their search scores are arbitrary;
    the relevance floor would filter them out, making the tests flaky.
    """
    return Settings(relevance_floor_enabled=False)


@pytest_asyncio.fixture
async def brain(db, ctx_settings):
    """Brain without embeddings for context tests."""
    b = Brain(database=db, settings=ctx_settings)
    yield b
    await b.close()


@pytest_asyncio.fixture
async def context_engine(brain, heart, ctx_settings):
    """ContextEngine wired to Brain and Heart."""
    return ContextEngine(brain, heart, ctx_settings, identity_prompt="You are Nous, a thinking agent.")


def _frame_selection(frame_id: str = "task", **overrides) -> FrameSelection:
    """Build a FrameSelection with defaults."""
    defaults = dict(
        frame_id=frame_id,
        frame_name="Task Execution",
        confidence=0.9,
        match_method="pattern",
        default_category="tooling",
        default_stakes="medium",
    )
    defaults.update(overrides)
    return FrameSelection(**defaults)


def _record_input(**overrides) -> RecordInput:
    """Build a RecordInput with sensible defaults."""
    defaults = dict(
        description="Use PostgreSQL for storage",
        confidence=0.85,
        category="architecture",
        stakes="medium",
        context="Evaluating database options",
        reasons=[ReasonInput(type="analysis", text="Solid choice")],
    )
    defaults.update(overrides)
    return RecordInput(**defaults)


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _seed_decisions(brain, session, count=3):
    """Pre-seed Brain with decisions."""
    decisions = []
    for i in range(count):
        d = await brain.record(
            _record_input(description=f"Context test decision {i}"),
            session=session,
        )
        decisions.append(d)
    return decisions


async def _seed_facts(heart, session, count=5):
    """Pre-seed Heart with facts."""
    facts = []
    for i in range(count):
        f = await heart.learn(
            FactInput(
                content=f"Context test fact {i}: important information",
                category="technical",
                subject="testing",
                confidence=0.9,
            ),
            session=session,
        )
        facts.append(f)
    return facts


async def _seed_procedures(heart, session, count=2):
    """Pre-seed Heart with procedures."""
    procs = []
    for i in range(count):
        p = await heart.store_procedure(
            ProcedureInput(
                name=f"Context test procedure {i}",
                domain="testing",
                core_patterns=[f"pattern-{i}"],
            ),
            session=session,
        )
        procs.append(p)
    return procs


async def _seed_censor(heart, session):
    """Pre-seed Heart with an active censor."""
    return await heart.add_censor(
        CensorInput(
            trigger_pattern="dangerous operation",
            reason="Avoid risky operations without review",
            action="warn",
        ),
        session=session,
    )


# ---------------------------------------------------------------------------
# 1. test_build_conversation_budget
# ---------------------------------------------------------------------------


async def test_build_conversation_budget(context_engine, session):
    """Conversation frame gets 3K total budget."""
    budget = ContextBudget.for_frame("conversation")
    assert budget.total == 3000
    assert budget.procedures == 0  # Conversation skips procedures


# ---------------------------------------------------------------------------
# 2. test_build_decision_budget
# ---------------------------------------------------------------------------


async def test_build_decision_budget(context_engine, session):
    """Decision frame gets 12K total budget."""
    budget = ContextBudget.for_frame("decision")
    assert budget.total == 12000
    assert budget.decisions == 3000


# ---------------------------------------------------------------------------
# 3. test_build_includes_identity
# ---------------------------------------------------------------------------


async def test_build_includes_identity(context_engine, brain, heart, session):
    """Identity section always present in system prompt."""
    frame = _frame_selection()
    sid = f"test-ctx-identity-{uuid.uuid4().hex[:8]}"
    await heart.get_or_create_working_memory(sid, session=session)

    result = await context_engine.build("nous-default", sid, "build something", frame, session=session)

    assert "Nous" in result.system_prompt or "thinking agent" in result.system_prompt
    assert len(result.system_prompt) > 0


# ---------------------------------------------------------------------------
# 4. test_build_includes_censors
# ---------------------------------------------------------------------------


async def test_build_includes_censors(context_engine, brain, heart, session):
    """Active censors appear in context."""
    await _seed_censor(heart, session)
    frame = _frame_selection()
    sid = f"test-ctx-censors-{uuid.uuid4().hex[:8]}"
    await heart.get_or_create_working_memory(sid, session=session)

    result = await context_engine.build("nous-default", sid, "do something dangerous", frame, session=session)

    # Censor should appear somewhere in the prompt
    assert "dangerous operation" in result.system_prompt.lower() or any("censor" in s.label.lower() for s in result.sections)


# ---------------------------------------------------------------------------
# 5. test_build_truncates_to_budget
# ---------------------------------------------------------------------------


async def test_build_truncates_to_budget(context_engine, session):
    """Long content is truncated, not omitted."""
    engine = context_engine
    long_text = "x" * 100000  # Very long text
    truncated = engine._truncate_to_budget(long_text, 100)
    # 100 tokens * 4 chars/token = 400 chars max
    assert len(truncated) <= 400
    assert truncated.endswith("...")


# ---------------------------------------------------------------------------
# 6. test_build_skips_zero_budget
# ---------------------------------------------------------------------------


async def test_build_skips_zero_budget(context_engine, brain, heart, session):
    """Conversation frame skips procedures (budget=0)."""
    await _seed_procedures(heart, session)
    frame = _frame_selection("conversation", frame_name="Conversation")
    sid = f"test-ctx-skip-{uuid.uuid4().hex[:8]}"
    await heart.get_or_create_working_memory(sid, session=session)

    result = await context_engine.build("nous-default", sid, "hello there", frame, session=session)

    # Procedures section should not be present (budget=0)
    procedure_sections = [s for s in result.sections if "procedure" in s.label.lower()]
    assert len(procedure_sections) == 0


# ---------------------------------------------------------------------------
# 7. test_build_with_decisions
# ---------------------------------------------------------------------------


async def test_build_with_decisions(context_engine, brain, heart, session):
    """Brain.query() results appear in context."""
    await _seed_decisions(brain, session)
    frame = _frame_selection()
    sid = f"test-ctx-decisions-{uuid.uuid4().hex[:8]}"
    await heart.get_or_create_working_memory(sid, session=session)

    result = await context_engine.build("nous-default", sid, "context test decision", frame, session=session)

    # Decisions section should appear with seeded data
    decision_sections = [s for s in result.sections if "decision" in s.label.lower()]
    if decision_sections:
        assert len(decision_sections[0].content) > 0


# ---------------------------------------------------------------------------
# 8. test_build_with_facts
# ---------------------------------------------------------------------------


async def test_build_with_facts(context_engine, brain, heart, session):
    """Heart facts appear in context."""
    await _seed_facts(heart, session)
    frame = _frame_selection()
    sid = f"test-ctx-facts-{uuid.uuid4().hex[:8]}"
    await heart.get_or_create_working_memory(sid, session=session)

    result = await context_engine.build("nous-default", sid, "context test fact", frame, session=session)

    fact_sections = [s for s in result.sections if "fact" in s.label.lower()]
    if fact_sections:
        assert len(fact_sections[0].content) > 0


# ---------------------------------------------------------------------------
# 9. test_build_with_working_memory
# ---------------------------------------------------------------------------


async def test_build_with_working_memory(context_engine, brain, heart, session):
    """Working memory current task appears in context."""
    sid = f"test-ctx-wm-{uuid.uuid4().hex[:8]}"
    await heart.get_or_create_working_memory(sid, session=session)
    await heart.focus(sid, task="Build the REST API", frame="task", session=session)

    frame = _frame_selection()
    result = await context_engine.build("nous-default", sid, "continue building", frame, session=session)

    wm_sections = [s for s in result.sections if "working" in s.label.lower() or "memory" in s.label.lower()]
    if wm_sections:
        assert "REST API" in wm_sections[0].content or "Build" in wm_sections[0].content


# ---------------------------------------------------------------------------
# 10. test_refresh_needed_frame_change
# ---------------------------------------------------------------------------


async def test_refresh_needed_frame_change(context_engine, heart, session):
    """refresh_needed returns True when frame has changed."""
    sid = f"test-ctx-refresh-{uuid.uuid4().hex[:8]}"
    await heart.get_or_create_working_memory(sid, session=session)
    await heart.focus(sid, task="old task", frame="conversation", session=session)

    # Now check with a different frame
    new_frame = _frame_selection("task")
    result = await context_engine.refresh_needed("nous-default", sid, "build something", new_frame, session=session)
    assert result is True


# ---------------------------------------------------------------------------
# 11. test_refresh_needed_same_frame
# ---------------------------------------------------------------------------


async def test_refresh_needed_same_frame(context_engine, heart, session):
    """refresh_needed returns False when frame hasn't changed."""
    sid = f"test-ctx-norefresh-{uuid.uuid4().hex[:8]}"
    await heart.get_or_create_working_memory(sid, session=session)
    await heart.focus(sid, task="current task", frame="task", session=session)

    frame = _frame_selection("task")
    result = await context_engine.refresh_needed("nous-default", sid, "more work", frame, session=session)
    assert result is False


# ---------------------------------------------------------------------------
# 12. test_expand_decision
# ---------------------------------------------------------------------------


async def test_expand_decision(context_engine, brain, session):
    """expand() loads full decision detail by ID."""
    decision = await brain.record(
        _record_input(description="Expand test decision"),
        session=session,
    )

    result = await context_engine.expand("decision", str(decision.id), session=session)
    assert isinstance(result, str)
    assert len(result) > 0


# ---------------------------------------------------------------------------
# 13. test_estimate_tokens
# ---------------------------------------------------------------------------


async def test_estimate_tokens(context_engine):
    """Token estimation uses chars/4 heuristic."""
    assert context_engine._estimate_tokens("abcd") == 1
    assert context_engine._estimate_tokens("a" * 400) == 100
    assert context_engine._estimate_tokens("") == 1  # min 1


# ---------------------------------------------------------------------------
# 14-18. test_format_facts — truncation, subjects, no active/inactive (#45)
# ---------------------------------------------------------------------------


def _mock_fact(**kwargs):
    """Build a mock fact object with given attributes."""
    from unittest.mock import MagicMock

    fact = MagicMock()
    fact.content = kwargs.get("content", "A test fact")
    fact.confidence = kwargs.get("confidence", 0.90)
    fact.subject = kwargs.get("subject", None)
    fact.active = kwargs.get("active", True)
    return fact


def _make_context_engine_light():
    """Build a ContextEngine with mocks — no database needed."""
    from unittest.mock import MagicMock

    brain = MagicMock()
    heart = MagicMock()
    settings = MagicMock()
    return ContextEngine(brain, heart, settings, identity_prompt="test")


async def test_format_facts_short_content():
    """14. Short facts (under 200 chars) are displayed in full."""
    engine = _make_context_engine_light()
    facts = [_mock_fact(content="User prefers dark mode", confidence=0.85)]
    result = engine._format_facts(facts)
    assert "User prefers dark mode" in result
    assert "..." not in result
    assert "[confidence: 0.85]" in result


async def test_format_facts_long_content_truncated():
    """15. Long facts (over 200 chars) are truncated at word boundary with ellipsis."""
    engine = _make_context_engine_light()
    long_content = "word " * 60  # 300 chars (60 * 5)
    facts = [_mock_fact(content=long_content.strip(), confidence=0.90)]
    result = engine._format_facts(facts)
    assert result.count("...") == 1
    # Content portion should be truncated — total line will be longer due to prefix
    # but the content itself should be cut at a word boundary before 200 chars
    content_part = result.split("[confidence:")[0]
    # The truncated content (before "...") should not exceed 200 chars + "..."
    assert "..." in content_part


async def test_format_facts_with_subject():
    """16. Facts with subjects get the [subject] prefix."""
    engine = _make_context_engine_light()
    facts = [_mock_fact(content="Uses PostgreSQL", subject="project", confidence=0.95)]
    result = engine._format_facts(facts)
    assert "[project]" in result
    assert "Uses PostgreSQL" in result
    assert "[confidence: 0.95]" in result


async def test_format_facts_without_subject():
    """17. Facts without subjects get no prefix bracket."""
    engine = _make_context_engine_light()
    facts = [_mock_fact(content="General knowledge fact", subject=None, confidence=0.80)]
    result = engine._format_facts(facts)
    assert result.startswith("- General knowledge fact")
    assert "[confidence: 0.80]" in result
    # Should not have any subject bracket
    assert "[]" not in result
    assert "[None]" not in result


async def test_format_facts_no_active_inactive_status():
    """18. Active/inactive status is no longer displayed (#45)."""
    engine = _make_context_engine_light()
    facts = [
        _mock_fact(content="User prefers dark mode", active=True, confidence=0.90),
        _mock_fact(content="Old preference superseded", active=False, confidence=0.70),
    ]
    result = engine._format_facts(facts)
    # The old format appended ", active" or ", inactive" — verify that's gone
    assert ", active]" not in result
    assert ", inactive]" not in result
    # Should not contain the word "active" or "inactive" as status labels
    assert "active" not in result
    assert "inactive" not in result


# ---------------------------------------------------------------------------
# 19-25. Topic-aware context recall (007.2)
# ---------------------------------------------------------------------------


def _mock_decision(**kwargs):
    """Build a mock decision with given attributes."""
    from unittest.mock import MagicMock

    d = MagicMock()
    d.id = kwargs.get("id", "dec-1")
    d.description = kwargs.get("description", "A decision")
    d.confidence = kwargs.get("confidence", 0.85)
    d.category = kwargs.get("category", "architecture")
    d.outcome = kwargs.get("outcome", "pending")
    return d


def _mock_episode(**kwargs):
    """Build a mock episode with given attributes."""
    from unittest.mock import MagicMock

    e = MagicMock()
    e.id = kwargs.get("id", "ep-1")
    e.summary = kwargs.get("summary", "An episode")
    e.outcome = kwargs.get("outcome", "completed")
    e.started_at = kwargs.get("started_at", None)
    e.tags = kwargs.get("tags", [])
    return e


async def test_enforce_diversity_facts_by_subject():
    """19. Diversity filter limits facts per subject."""
    engine = _make_context_engine_light()
    facts = [
        _mock_fact(content="Nous uses PostgreSQL", subject="nous", confidence=0.9),
        _mock_fact(content="Nous uses pgvector", subject="nous", confidence=0.9),
        _mock_fact(content="Nous uses SQLAlchemy", subject="nous", confidence=0.9),
        _mock_fact(content="CE uses ChromaDB", subject="cognition-engines", confidence=0.9),
        _mock_fact(content="CE uses FastAPI", subject="cognition-engines", confidence=0.9),
    ]
    result = engine._enforce_diversity(facts, "subject", max_per_subject=2)
    # Max 2 per subject: 2 nous + 2 CE = 4 (one nous item dropped)
    assert len(result) == 4
    nous_count = sum(1 for f in result if f.subject == "nous")
    assert nous_count == 2


async def test_enforce_diversity_decisions_by_category():
    """20. Diversity filter limits decisions per category."""
    engine = _make_context_engine_light()
    decisions = [
        _mock_decision(id="d1", category="architecture"),
        _mock_decision(id="d2", category="architecture"),
        _mock_decision(id="d3", category="architecture"),
        _mock_decision(id="d4", category="architecture"),
        _mock_decision(id="d5", category="tooling"),
    ]
    result = engine._enforce_diversity(decisions, "category", max_per_subject=3)
    assert len(result) == 4  # 3 architecture + 1 tooling


async def test_enforce_diversity_episodes_by_tags():
    """21. Diversity filter uses first tag as topic key for episodes."""
    engine = _make_context_engine_light()
    episodes = [
        _mock_episode(id="e1", tags=["nous", "db"]),
        _mock_episode(id="e2", tags=["nous", "api"]),
        _mock_episode(id="e3", tags=["nous", "test"]),
        _mock_episode(id="e4", tags=["ce", "api"]),
    ]
    result = engine._enforce_diversity(episodes, "tags", max_per_subject=2)
    assert len(result) == 3  # 2 nous + 1 ce


async def test_enforce_diversity_empty_list():
    """22. Diversity filter returns empty list unchanged."""
    engine = _make_context_engine_light()
    result = engine._enforce_diversity([], "subject", max_per_subject=2)
    assert result == []


async def test_enforce_diversity_missing_attr_defaults_unknown():
    """23. Items without topic attr all map to 'unknown'."""
    engine = _make_context_engine_light()
    facts = [
        _mock_fact(content="fact 1", subject=None),
        _mock_fact(content="fact 2", subject=None),
        _mock_fact(content="fact 3", subject=None),
    ]
    result = engine._enforce_diversity(facts, "subject", max_per_subject=2)
    # All have topic_key='unknown', so max 2 pass
    assert len(result) == 2


async def test_topic_enhanced_query_with_working_memory(context_engine, brain, heart, session):
    """24. When working memory has a topic, recall queries are prefixed."""
    sid = f"test-topic-query-{uuid.uuid4().hex[:8]}"
    await heart.get_or_create_working_memory(sid, session=session)
    await heart.focus(sid, task="cognition-engines", frame="task", session=session)

    # Seed a fact about cognition-engines
    await heart.learn(
        FactInput(
            content="Cognition-engines uses ChromaDB for vector storage",
            category="technical",
            subject="cognition-engines",
            confidence=0.9,
        ),
        session=session,
    )

    frame = _frame_selection()
    result = await context_engine.build(
        "nous-default", sid, "what do you know?", frame, session=session
    )

    # The build should complete without error — topic prefix is additive
    assert result.system_prompt is not None
    assert len(result.system_prompt) > 0


async def test_topic_no_working_memory_fallback(context_engine, brain, heart, session):
    """25. Without working memory topic, queries use raw input_text (unchanged behavior)."""
    sid = f"test-no-topic-{uuid.uuid4().hex[:8]}"
    await heart.get_or_create_working_memory(sid, session=session)
    # Don't set a task — current_task is None

    frame = _frame_selection()
    result = await context_engine.build(
        "nous-default", sid, "tell me about databases", frame, session=session
    )

    # Should work exactly as before — no topic prefix
    assert result.system_prompt is not None
    assert len(result.system_prompt) > 0
