"""Test that get_by_name filters for active procedures (issue #229 P1 fix)."""

import pytest
import pytest_asyncio
from sqlalchemy import update

from nous.heart.schemas import ProcedureInput
from nous.storage.models import Procedure


@pytest.mark.asyncio
async def test_get_by_name_excludes_inactive(heart, session):
    """get_by_name should return None for inactive procedures."""
    # Store a procedure
    proc = await heart.store_procedure(
        ProcedureInput(name="test-inactive-skill", domain="test", core_patterns=["pattern1"]),
        session=session,
    )
    # Verify it's found when active
    found = await heart.get_procedure_by_name("test-inactive-skill", session=session)
    assert found is not None
    assert found.name == "test-inactive-skill"

    # Deactivate it
    await session.execute(
        update(Procedure).where(Procedure.id == proc.id).values(active=False)
    )
    await session.flush()

    # Should NOT be found anymore
    found2 = await heart.get_procedure_by_name("test-inactive-skill", session=session)
    assert found2 is None


@pytest.mark.asyncio
async def test_get_by_name_returns_active(heart, session):
    """get_by_name returns active procedures normally."""
    await heart.store_procedure(
        ProcedureInput(name="test-active-skill", domain="test", core_patterns=["pattern1"]),
        session=session,
    )
    found = await heart.get_procedure_by_name("test-active-skill", session=session)
    assert found is not None
    assert found.name == "test-active-skill"
