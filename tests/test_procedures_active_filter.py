"""Test that get_by_name filters for active procedures (issue #229 P1 fix)."""

import pytest
import pytest_asyncio
from sqlalchemy import update

from nous.config import Settings
from nous.heart import Heart
from nous.heart.schemas import ProcedureInput
from nous.storage.models import Procedure


@pytest_asyncio.fixture
async def heart_no_env(db, mock_embeddings):
    """Heart instance that ignores .env file."""
    settings = Settings(_env_file=None)
    h = Heart(db, settings, embedding_provider=mock_embeddings)
    yield h
    await h.close()


@pytest.mark.asyncio
async def test_get_by_name_excludes_inactive(heart_no_env, session):
    """get_by_name should return None for inactive procedures."""
    proc = await heart_no_env.store_procedure(
        ProcedureInput(name="test-inactive-skill", domain="test", core_patterns=["pattern1"]),
        session=session,
    )
    found = await heart_no_env.get_procedure_by_name("test-inactive-skill", session=session)
    assert found is not None
    assert found.name == "test-inactive-skill"

    # Deactivate it
    await session.execute(update(Procedure).where(Procedure.id == proc.id).values(active=False))
    await session.flush()

    # Should NOT be found anymore
    found2 = await heart_no_env.get_procedure_by_name("test-inactive-skill", session=session)
    assert found2 is None


@pytest.mark.asyncio
async def test_get_by_name_returns_active(heart_no_env, session):
    """get_by_name returns active procedures normally."""
    await heart_no_env.store_procedure(
        ProcedureInput(name="test-active-skill", domain="test", core_patterns=["pattern1"]),
        session=session,
    )
    found = await heart_no_env.get_procedure_by_name("test-active-skill", session=session)
    assert found is not None
    assert found.name == "test-active-skill"
