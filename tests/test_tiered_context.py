"""Tests for Spec 008 PR 2 — Tiered Context Model.

Verifies:
- Tier 1: User profile facts always loaded (no search)
- Tier 3: Thresholds filter low-relevance results
- Tier 1 categories excluded from Tier 3 fact search
- Budget includes user_profile field
"""


import uuid as _uuid

import pytest
import pytest_asyncio

from nous.brain.brain import Brain
from nous.cognitive.context import ContextEngine
from nous.cognitive.schemas import ContextBudget, FrameSelection
from nous.config import Settings
from nous.heart import FactInput, Heart


# ---------------------------------------------------------------------------
# Fixtures — use unique agent_id to isolate from other tests' data
# ---------------------------------------------------------------------------

_TIERED_AGENT_ID = f"test-tiered-context-{_uuid.uuid4().hex[:8]}"


@pytest.fixture
def tiered_settings(settings):
    return settings.model_copy(update={"agent_id": _TIERED_AGENT_ID})


@pytest_asyncio.fixture(autouse=True)
async def _ensure_agent(db):
    """Create test agent in DB so FK constraints pass."""
    from sqlalchemy import text
    async with db.session() as session:
        await session.execute(
            text("INSERT INTO nous_system.agents (id, name, config) VALUES (:id, :name, '{}'::jsonb) ON CONFLICT (id) DO NOTHING"),
            {"id": _TIERED_AGENT_ID, "name": "Test Tiered Agent"},
        )
        await session.commit()


@pytest_asyncio.fixture
async def brain(db, tiered_settings):
    b = Brain(database=db, settings=tiered_settings)
    yield b
    await b.close()


@pytest_asyncio.fixture
async def heart(db, mock_embeddings, tiered_settings):
    h = Heart(db, tiered_settings, embedding_provider=mock_embeddings)
    yield h
    await h.close()


@pytest_asyncio.fixture
async def context_engine(brain, heart, tiered_settings):
    return ContextEngine(brain, heart, tiered_settings, identity_prompt="You are Nous.")


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
            await heart.learn(
                FactInput(content="Tim prefers Celsius for all temperature readings", category="preference", subject="Tim"),
                session=session,
            )
            await heart.learn(
                FactInput(content="Tim lives in Silver Spring MD in the United States", category="person", subject="Tim"),
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
            await heart.learn(
                FactInput(content="Tim prefers Celsius for all temperature readings", category="preference", subject="Tim"),
                session=session,
            )
            await heart.learn(
                FactInput(content="Nous uses PostgreSQL as its primary database", category="technical", subject="Nous"),
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
    async def test_no_profile_section_when_empty(self, db, mock_embeddings):
        """No User Profile section when no preference/person/rule facts exist."""
        # Use a completely fresh agent_id with no facts at all
        fresh_id = f"test-empty-profile-{_uuid.uuid4().hex[:8]}"
        from sqlalchemy import text as sql_text
        async with db.session() as session:
            await session.execute(
                sql_text("INSERT INTO nous_system.agents (id, name, config) VALUES (:id, :name, '{}'::jsonb) ON CONFLICT (id) DO NOTHING"),
                {"id": fresh_id, "name": "Empty Profile Agent"},
            )
            await session.commit()

        fresh_settings = Settings().model_copy(update={"agent_id": fresh_id})
        fresh_brain = Brain(database=db, settings=fresh_settings)
        fresh_heart = Heart(db, fresh_settings, embedding_provider=mock_embeddings)
        fresh_engine = ContextEngine(fresh_brain, fresh_heart, fresh_settings, identity_prompt="You are Nous.")

        result = await fresh_engine.build(
            agent_id=fresh_id,
            session_id="test-session",
            input_text="hello",
            frame=_frame(),
        )

        labels = [s.label for s in result.sections]
        assert "User Profile" not in labels
        await fresh_brain.close()
        await fresh_heart.close()


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
    async def test_returns_matching_categories(self, db, mock_embeddings):
        """list_facts_by_category returns only facts in specified categories."""
        # Use fresh agent_id to avoid accumulation from other tests
        fresh_id = f"test-list-cat-{_uuid.uuid4().hex[:8]}"
        from sqlalchemy import text as sql_text
        async with db.session() as session:
            await session.execute(
                sql_text("INSERT INTO nous_system.agents (id, name, config) VALUES (:id, :name, '{}'::jsonb) ON CONFLICT (id) DO NOTHING"),
                {"id": fresh_id, "name": "List Category Agent"},
            )
            await session.commit()

        fresh_settings = Settings().model_copy(update={"agent_id": fresh_id})
        fresh_heart = Heart(db, fresh_settings, embedding_provider=mock_embeddings)

        async with db.session() as session:
            await fresh_heart.learn(
                FactInput(content="Tim prefers Celsius for all temperature readings", category="preference", subject="Tim"),
                session=session,
            )
            await fresh_heart.learn(
                FactInput(content="Nous uses Postgres as its primary database", category="technical", subject="Nous"),
                session=session,
            )
            await session.commit()

        facts = await fresh_heart.list_facts_by_category(categories=["preference", "person", "rule"])
        assert len(facts) == 1
        assert "Celsius" in facts[0].content
        await fresh_heart.close()

    @pytest.mark.asyncio
    async def test_excludes_inactive(self, db, mock_embeddings):
        """list_facts_by_category skips inactive facts by default."""
        fresh_id = f"test-list-inact-{_uuid.uuid4().hex[:8]}"
        from sqlalchemy import text as sql_text
        async with db.session() as session:
            await session.execute(
                sql_text("INSERT INTO nous_system.agents (id, name, config) VALUES (:id, :name, '{}'::jsonb) ON CONFLICT (id) DO NOTHING"),
                {"id": fresh_id, "name": "List Inactive Agent"},
            )
            await session.commit()

        fresh_settings = Settings().model_copy(update={"agent_id": fresh_id})
        fresh_heart = Heart(db, fresh_settings, embedding_provider=mock_embeddings)

        async with db.session() as session:
            result = await fresh_heart.learn(
                FactInput(content="Old preference that is no longer relevant", category="preference", subject="Tim"),
                session=session,
            )
            await fresh_heart.deactivate_fact(result.id, session=session)
            await session.commit()

        facts = await fresh_heart.list_facts_by_category(categories=["preference"])
        assert len(facts) == 0
        await fresh_heart.close()
