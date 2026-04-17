"""Test fixtures for F047 project registry unit tests.

Reuses the session-scoped db and function-scoped session from the
parent conftest.py (tests/conftest.py).
"""

import pytest
import pytest_asyncio

from nous.config import Settings
from nous.projects.registry import ProjectRegistry


@pytest_asyncio.fixture
async def registry(db, settings):
    """ProjectRegistry instance for testing."""
    return ProjectRegistry(
        db=db,
        agent_id=settings.agent_id,
        embeddings=None,
    )


@pytest.fixture
def agent_id(settings) -> str:
    return settings.agent_id
