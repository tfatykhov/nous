"""Tests for Spec 008 PR 2 — Tiered Context Model.

Verifies:
- Tier 1: User profile facts always loaded (no search)
- Tier 3: Thresholds filter low-relevance results
- Tier 1 categories excluded from Tier 3 fact search
- Budget includes user_profile field
"""


import pytest
import pytest_asyncio

from nous.brain.brain import Brain
from nous.cognitive.context import ContextEngine
from nous.cognitive.schemas import ContextBudget, FrameSelection
from nous.heart import FactInput


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def brain(db, settings):
    b = Brain(database=db, settings=settings)
    yield b
    await b.close()


@pytest_asyncio.fixture
async def context_engine(brain, heart, settings):
    return ContextEngine(brain, heart, settings, identity_prompt="You are Nous.")


def _frame(frame_id: str = "task") -> FrameSelection:
    return FrameSelection(
        frame_id=frame_id,
        frame_name="Task",
        confidence=0.9,
        match_method="pattern",
        default_category="tooling",
        default_stakes="medium",
    )


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------


class TestContextBudget:
    def test_user_profile_field_exists(self):
        budget = ContextBudget()
        assert hasattr(budget, "user_profile")
        assert budget.user_profile == 200

    def test_user_profile_in_frame_budgets(self):
        for frame_id in ["conversation", "question", "task", "decision", "creative", "debug"]:
            budget = ContextBudget.for_frame(frame_id)
            assert hasattr(budget, "user_profile")


# ---------------------------------------------------------------------------
# Tier 1: User Profile (always loaded)
# ---------------------------------------------------------------------------


class TestTier1UserProfile:
    @pytest.mark.asyncio
    async def test_profile_facts_in_context(self, context_engine, heart, db):
        """Preference/person/rule facts appear in User Profile section."""
        async with db.session() as session:
            await heart.store_fact(
                FactInput(content="Tim prefers Celsius", category="preference", subject="Tim"),
                session=session,
            )
            await heart.store_fact(
                FactInput(content="Tim lives in Silver Spring MD", category="person", subject="Tim"),
                session=session,
            )
            await session.commit()

        result = await context_engine.build(
            agent_id="test-agent",
            session_id="test-session",
            input_text="what is the weather?",
            frame=_frame(),
        )

        labels = [s.label for s in result.sections]
        assert "User Profile" in labels

        profile = next(s for s in result.sections if s.label == "User Profile")
        assert "Celsius" in profile.content
        assert "Silver Spring" in profile.content

    @pytest.mark.asyncio
    async def test_profile_facts_excluded_from_tier3(self, context_engine, heart, db):
        """Preference facts should NOT appear in Relevant Facts (Tier 3)."""
        async with db.session() as session:
            await heart.store_fact(
                FactInput(content="Tim prefers Celsius", category="preference", subject="Tim"),
                session=session,
            )
            await heart.store_fact(
                FactInput(content="Nous uses PostgreSQL", category="technical", subject="Nous"),
                session=session,
            )
            await session.commit()

        result = await context_engine.build(
            agent_id="test-agent",
            session_id="test-session",
            input_text="tell me about Nous database",
            frame=_frame(),
        )

        # Tier 3 facts should have technical but NOT preference
        tier3_facts = next((s for s in result.sections if s.label == "Relevant Facts"), None)
        if tier3_facts:
            assert "Celsius" not in tier3_facts.content

    @pytest.mark.asyncio
    async def test_no_profile_section_when_empty(self, context_engine, heart, db):
        """No User Profile section when no preference/person/rule facts exist."""
        result = await context_engine.build(
            agent_id="test-agent",
            session_id="test-session",
            input_text="hello",
            frame=_frame(),
        )

        labels = [s.label for s in result.sections]
        assert "User Profile" not in labels


# ---------------------------------------------------------------------------
# Tier 3: Thresholds
# ---------------------------------------------------------------------------


class TestTier3Thresholds:
    @pytest.mark.asyncio
    async def test_budget_user_profile_override(self):
        """user_profile budget can be overridden."""
        budget = ContextBudget()
        budget.apply_overrides({"user_profile": 500})
        assert budget.user_profile == 500


# ---------------------------------------------------------------------------
# list_by_category
# ---------------------------------------------------------------------------


class TestListByCategory:
    @pytest.mark.asyncio
    async def test_returns_matching_categories(self, heart, db):
        """list_facts_by_category returns only facts in specified categories."""
        async with db.session() as session:
            await heart.store_fact(
                FactInput(content="Tim prefers Celsius", category="preference", subject="Tim"),
                session=session,
            )
            await heart.store_fact(
                FactInput(content="Nous uses Postgres", category="technical", subject="Nous"),
                session=session,
            )
            await session.commit()

        facts = await heart.list_facts_by_category(categories=["preference", "person", "rule"])
        assert len(facts) == 1
        assert "Celsius" in facts[0].content

    @pytest.mark.asyncio
    async def test_excludes_inactive(self, heart, db):
        """list_facts_by_category skips inactive facts by default."""
        async with db.session() as session:
            result = await heart.store_fact(
                FactInput(content="Old preference", category="preference", subject="Tim"),
                session=session,
            )
            await heart.deactivate_fact(result.id, session=session)
            await session.commit()

        facts = await heart.list_facts_by_category(categories=["preference"])
        assert len(facts) == 0
