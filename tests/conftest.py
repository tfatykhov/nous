"""Test fixtures using real Postgres via docker-compose, or SQLite in-memory for offline runs.

Set NOUS_TEST_DB=postgres to use a live PostgreSQL database.
The default (sqlite) runs with an in-memory SQLite backend; tests that
require Postgres-specific features are skipped automatically.
"""

import hashlib
import os
import random

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from nous.config import Settings
from nous.storage.database import Database
from nous.storage.models import Guardrail

# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------

USE_POSTGRES: bool = os.environ.get("NOUS_TEST_DB", "sqlite") == "postgres"

# ---------------------------------------------------------------------------
# Integration test gating
# ---------------------------------------------------------------------------


def pytest_addoption(parser: pytest.Parser) -> None:
    """Add --integration flag to run tests that require a live PostgreSQL database."""
    parser.addoption(
        "--integration",
        action="store_true",
        default=False,
        help="run integration tests that require a live PostgreSQL database",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip @pytest.mark.integration tests unless --integration flag is passed."""
    if config.getoption("--integration"):
        return
    skip_integration = pytest.mark.skip(reason="requires --integration flag (live PostgreSQL)")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_integration)


def pytest_runtest_setup(item: pytest.Item) -> None:
    """Skip tests marked postgres_only when running without a real Postgres connection."""
    if "postgres_only" in item.keywords and not USE_POSTGRES:
        pytest.skip("requires NOUS_TEST_DB=postgres (real PostgreSQL connection)")

# ---------------------------------------------------------------------------
# Mock embedding provider (P1-4 fix: PRNG-seeded, L2-normalized vectors)
# ---------------------------------------------------------------------------


class MockEmbeddingProvider:
    """Returns deterministic, L2-normalized embeddings seeded from text hash.

    Uses PRNG seeded from SHA-256 hash of the input text to produce
    genuinely different 1536-dim vectors for different inputs. Unlike
    the naive hash-cycling approach, cosine similarity between unrelated
    texts is near zero while identical texts produce identical vectors.
    """

    async def embed(self, text: str) -> list[float]:
        h = hashlib.sha256(text.encode()).hexdigest()
        rng = random.Random(h)
        vec = [rng.gauss(0, 1) for _ in range(1536)]
        norm = sum(x * x for x in vec) ** 0.5
        return [x / norm for x in vec]

    async def embed_near(self, text: str, noise: float = 0.05) -> list[float]:
        """Generate embedding similar to embed(text) but with controlled noise.

        Produces vectors with cosine similarity ~(1 - noise) to the base embedding.
        Used for testing near-duplicate detection and similarity thresholds.
        """
        base = await self.embed(text)
        rng = random.Random(f"{text}_near_{noise}")
        noisy = [v + rng.gauss(0, noise) for v in base]
        norm = sum(x * x for x in noisy) ** 0.5
        return [x / norm for x in noisy]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [await self.embed(t) for t in texts]

    async def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Database fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="session")
async def db():
    """Session-scoped database connection pool.

    Uses a real PostgreSQL connection when ``NOUS_TEST_DB=postgres``,
    or an in-memory SQLite database otherwise. Tests that require
    Postgres-specific features must be marked ``@pytest.mark.integration``
    or ``@pytest.mark.postgres_only``.
    """
    if USE_POSTGRES:
        settings = Settings()
        database = Database(settings)
        await database.connect()
        yield database
        await database.disconnect()
    else:
        from tests.sqlite_compat import TestDatabase

        database = TestDatabase()
        await database.connect()
        yield database
        await database.disconnect()


@pytest.fixture(scope="session")
def settings() -> Settings:
    """Session-scoped settings instance."""
    return Settings()


@pytest_asyncio.fixture
async def session(db):
    """Function-scoped session with transaction rollback isolation.

    Tests can use the session freely — the entire transaction is rolled
    back after each test via the outer connection transaction.
    """
    async with db.engine.connect() as conn:
        trans = await conn.begin()
        session = AsyncSession(bind=conn, expire_on_commit=False)
        yield session
        await session.close()
        await trans.rollback()


# ---------------------------------------------------------------------------
# Brain fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_embeddings() -> MockEmbeddingProvider:
    """Mock embedding provider for tests needing deterministic vectors."""
    return MockEmbeddingProvider()


@pytest_asyncio.fixture
async def heart(db, mock_embeddings):
    """Heart instance with mock embeddings for testing."""
    from nous.config import Settings
    from nous.heart import Heart

    settings = Settings()
    h = Heart(db, settings, embedding_provider=mock_embeddings)
    yield h
    await h.close()


@pytest_asyncio.fixture
async def heart_with_admission(db, mock_embeddings):
    """Heart with active admission control (LLM disabled, heuristic only)."""
    from nous.heart import Heart
    from nous.heart.admission import AdmissionConfig, AdmissionController

    config = AdmissionConfig(shadow_mode=False, utility_llm_enabled=False)
    controller = AdmissionController(config=config)
    settings = Settings()
    h = Heart(db, settings, embedding_provider=mock_embeddings)
    h.facts._admission_controller = controller
    yield h
    await h.close()


@pytest_asyncio.fixture
async def heart_with_strict_admission(db, mock_embeddings):
    """Heart with strict admission (threshold=0.99)."""
    from nous.heart import Heart
    from nous.heart.admission import AdmissionConfig, AdmissionController

    config = AdmissionConfig(shadow_mode=False, utility_llm_enabled=False, threshold=0.99)
    controller = AdmissionController(config=config)
    settings = Settings()
    h = Heart(db, settings, embedding_provider=mock_embeddings)
    h.facts._admission_controller = controller
    yield h
    await h.close()


@pytest_asyncio.fixture
async def heart_with_shadow_admission(db, mock_embeddings):
    """Heart with shadow mode admission."""
    from nous.heart import Heart
    from nous.heart.admission import AdmissionConfig, AdmissionController

    config = AdmissionConfig(shadow_mode=True, utility_llm_enabled=False)
    controller = AdmissionController(config=config)
    settings = Settings()
    h = Heart(db, settings, embedding_provider=mock_embeddings)
    h.facts._admission_controller = controller
    yield h
    await h.close()


GUARDRAIL_TEST_AGENT = "test-guardrail-agent"


@pytest_asyncio.fixture
async def seed_guardrails(session):
    """Insert the 4 default guardrails into the test session.

    Uses a test-specific agent_id to avoid unique constraint collisions
    with seed.sql data that is already loaded in the real database.

    Uses legacy JSONB format to test backward compatibility.
    """
    guardrails = [
        Guardrail(
            agent_id=GUARDRAIL_TEST_AGENT,
            name="no-high-stakes-low-confidence",
            description="Block high-stakes decisions with low confidence",
            condition={"stakes": "high", "confidence_lt": 0.5},  # Legacy JSONB
            severity="block",
            priority=100,
        ),
        Guardrail(
            agent_id=GUARDRAIL_TEST_AGENT,
            name="no-critical-without-review",
            description="Block critical-stakes without explicit review",
            condition={"stakes": "critical"},  # Legacy JSONB
            severity="block",
            priority=90,
        ),
        Guardrail(
            agent_id=GUARDRAIL_TEST_AGENT,
            name="require-reasons",
            description="Block decisions without at least one reason",
            condition={"reason_count_lt": 1},  # Legacy JSONB
            severity="block",
            priority=110,
        ),
        Guardrail(
            agent_id=GUARDRAIL_TEST_AGENT,
            name="low-quality-recording",
            description="Block low-quality decisions (missing tags/pattern)",
            condition={"quality_lt": 0.5},  # Legacy JSONB
            severity="block",
            priority=120,
        ),
    ]
    for g in guardrails:
        session.add(g)
    await session.flush()
    return guardrails
