"""Tests for F021.1 Admission Control Dashboard."""


import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from nous.brain.brain import Brain
from nous.cognitive.layer import CognitiveLayer
from nous.heart import FactInput

# ---------------------------------------------------------------------------
# Task 1: Schema tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fact_has_admission_scores_column(db):
    """Fact model has admission_scores JSONB column."""
    async with db.session() as session:
        result = await session.execute(
            text("""
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = 'heart'
                  AND table_name = 'facts'
                  AND column_name = 'admission_scores'
            """)
        )
        row = result.one_or_none()
        assert row is not None, "admission_scores column missing from heart.facts"
        assert row.data_type == "jsonb"


# ---------------------------------------------------------------------------
# Task 2: Persistence tests (use heart_with_shadow_admission fixture)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fact_persists_admission_scores(heart_with_shadow_admission, db):
    """Fact created with shadow admission stores per-dimension scores."""
    heart = heart_with_shadow_admission
    async with db.session() as session:
        await heart.learn(
            FactInput(
                content="Test fact for admission scores persistence",
                category="technical",
                confidence=0.9,
                source="knowledge_extractor",
            ),
            session=session,
        )
        await session.commit()

    # Read back and check
    async with db.session() as session:
        row = await session.execute(
            text("""
                SELECT admission_score, admission_scores
                FROM heart.facts
                WHERE content = 'Test fact for admission scores persistence'
            """)
        )
        fact = row.one_or_none()
        assert fact is not None
        assert fact.admission_score is not None, "admission_score should be set by shadow admission"
        assert fact.admission_scores is not None, "admission_scores JSONB should be set"
        assert "utility" in fact.admission_scores
        assert "confidence" in fact.admission_scores
        assert "novelty" in fact.admission_scores
        assert "recency" in fact.admission_scores
        assert "type_prior" in fact.admission_scores


@pytest.mark.asyncio
async def test_bypassed_fact_has_null_scores(heart_with_shadow_admission, db):
    """Bypassed facts get admission_scores=NULL, not empty dict."""
    heart = heart_with_shadow_admission
    async with db.session() as session:
        await heart.learn(
            FactInput(
                content="User stated fact for bypass test",
                category="preference",
                confidence=1.0,
                source="user_stated",
            ),
            session=session,
        )
        await session.commit()

    async with db.session() as session:
        row = await session.execute(
            text("""
                SELECT admission_score, admission_scores
                FROM heart.facts
                WHERE content = 'User stated fact for bypass test'
            """)
        )
        fact = row.one_or_none()
        assert fact is not None
        # Bypassed facts get composite_score=1.0 but scores={} -> stored as NULL
        assert fact.admission_scores is None


# ---------------------------------------------------------------------------
# Task 3: get_admission_data tests
# ---------------------------------------------------------------------------


from nous.api.dashboard_queries import get_admission_data  # noqa: E402


@pytest.mark.asyncio
async def test_get_admission_data_empty(db, settings):
    """get_admission_data returns valid structure with no data."""
    async with db.session() as session:
        data = await get_admission_data(session, settings.agent_id, days=30, threshold=0.55)

    assert "summary" in data
    assert "score_distribution" in data
    assert "dimension_stats" in data
    assert "by_source" in data
    assert "by_category" in data
    assert "daily_trend" in data
    assert "bypass_breakdown" in data
    assert data["summary"]["total_scored"] == 0
    assert "threshold_note" in data["summary"]


@pytest.mark.asyncio
async def test_get_admission_data_with_facts(heart_with_shadow_admission, db, settings):
    """get_admission_data returns correct aggregations with scored facts."""
    heart = heart_with_shadow_admission
    async with db.session() as session:
        for i in range(5):
            await heart.learn(
                FactInput(
                    content=f"Scored fact {i} for admission dashboard test",
                    category="technical",
                    confidence=0.8,
                    source="knowledge_extractor",
                ),
                session=session,
            )
        # Insert a bypassed fact
        await heart.learn(
            FactInput(
                content="Bypassed fact for admission test",
                category="preference",
                confidence=1.0,
                source="user_stated",
            ),
            session=session,
        )
        await session.commit()

    async with db.session() as session:
        data = await get_admission_data(session, settings.agent_id, days=30, threshold=0.55)

    assert data["summary"]["total_scored"] >= 5
    assert data["summary"]["bypassed"] >= 1
    assert isinstance(data["score_distribution"], list)
    assert isinstance(data["by_source"], dict)
    assert isinstance(data["by_category"], dict)


# ---------------------------------------------------------------------------
# Task 4: get_admission_rejected tests
# ---------------------------------------------------------------------------


from nous.api.dashboard_queries import get_admission_rejected  # noqa: E402


@pytest.mark.asyncio
async def test_get_admission_rejected_empty(db, settings):
    """get_admission_rejected returns valid structure with no data."""
    async with db.session() as session:
        data = await get_admission_rejected(
            session, settings.agent_id, threshold=0.55, days=30,
        )

    assert "facts" in data
    assert "total" in data
    assert data["total"] == 0
    assert data["facts"] == []


@pytest.mark.asyncio
async def test_get_admission_rejected_sort_allowlist(db, settings):
    """Invalid sort column falls back to admission_score."""
    async with db.session() as session:
        data = await get_admission_rejected(
            session, settings.agent_id, threshold=0.55, days=30,
            sort="DROP TABLE", order="asc",
        )
    assert "facts" in data


@pytest.mark.asyncio
async def test_get_admission_rejected_composite_score_alias(db, settings):
    """Sort by 'composite_score' maps to 'admission_score' column."""
    async with db.session() as session:
        data = await get_admission_rejected(
            session, settings.agent_id, threshold=0.55, days=30,
            sort="composite_score", order="asc",
        )
    assert "facts" in data


# ---------------------------------------------------------------------------
# Task 5: REST endpoint tests
# ---------------------------------------------------------------------------


class MockAgentRunner:
    def __init__(self):
        self._conversations = {}

    async def start(self):
        pass

    async def close(self):
        pass


@pytest_asyncio.fixture
async def brain(db, settings):
    b = Brain(database=db, settings=settings)
    yield b
    await b.close()


@pytest_asyncio.fixture
async def cognitive(brain, heart, settings):
    return CognitiveLayer(brain, heart, settings, identity_prompt="You are Nous.")


@pytest.fixture
def app(brain, heart, cognitive, db, settings):
    from nous.api.rest import create_app
    return create_app(MockAgentRunner(), brain, heart, cognitive, db, settings)


@pytest_asyncio.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_dashboard_admission_endpoint(client):
    """GET /dashboard/admission returns valid JSON."""
    resp = await client.get("/dashboard/admission")
    assert resp.status_code == 200
    data = resp.json()
    assert "config" in data
    assert "summary" in data
    assert "score_distribution" in data
    assert data["config"]["threshold"] == 0.55


@pytest.mark.asyncio
async def test_dashboard_admission_with_days_param(client):
    """GET /dashboard/admission?days=7 accepts days parameter."""
    resp = await client.get("/dashboard/admission?days=7")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_dashboard_admission_rejected_endpoint(client):
    """GET /dashboard/admission/rejected returns valid JSON."""
    resp = await client.get("/dashboard/admission/rejected")
    assert resp.status_code == 200
    data = resp.json()
    assert "facts" in data
    assert "total" in data
    assert "limit" in data
    assert "offset" in data


@pytest.mark.asyncio
async def test_dashboard_admission_rejected_pagination(client):
    """GET /dashboard/admission/rejected supports limit/offset."""
    resp = await client.get("/dashboard/admission/rejected?limit=10&offset=5")
    assert resp.status_code == 200
    data = resp.json()
    assert data["limit"] == 10
    assert data["offset"] == 5
