"""Tests for F024 Phase 3b RubricManager."""
import uuid
from datetime import datetime, UTC
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nous.cognitive.rubric_schemas import RubricDimension, RubricVersionDetail


def _default_dimensions() -> list[dict]:
    return [
        {"name": "Recall", "weight": 0.25, "description": "Accuracy and completeness of memory retrieval", "scoring_criteria": "1-10"},
        {"name": "Tool Selection", "weight": 0.25, "description": "Choosing the right tool for the task", "scoring_criteria": "1-10"},
        {"name": "Confidence Calibration", "weight": 0.25, "description": "Accuracy of confidence estimates", "scoring_criteria": "1-10"},
        {"name": "Proactivity", "weight": 0.25, "description": "Anticipating needs without being asked", "scoring_criteria": "1-10"},
    ]


class AsyncContextMock:
    def __init__(self, session):
        self._session = session
    async def __aenter__(self):
        return self._session
    async def __aexit__(self, *args):
        pass


class TestRubricManagerGetActive:
    @pytest.mark.asyncio
    async def test_get_active_returns_none_when_no_rubric(self):
        from nous.cognitive.rubric import RubricManager
        db = MagicMock()
        mock_session = AsyncMock()
        mock_result = AsyncMock()
        mock_result.scalar_one_or_none = MagicMock(return_value=None)
        mock_session.execute = AsyncMock(return_value=mock_result)
        db.session = MagicMock(return_value=AsyncContextMock(mock_session))

        mgr = RubricManager(db=db, agent_id="test")
        result = await mgr.get_active()
        assert result is None

    @pytest.mark.asyncio
    async def test_seed_creates_v1_when_none_exists(self):
        from nous.cognitive.rubric import RubricManager

        db = MagicMock()
        mock_session = AsyncMock()
        mock_result_none = AsyncMock()
        mock_result_none.scalar_one_or_none = MagicMock(return_value=None)
        mock_session.execute = AsyncMock(return_value=mock_result_none)
        mock_session.add = MagicMock()
        mock_session.flush = AsyncMock()
        mock_session.commit = AsyncMock()
        db.session = MagicMock(return_value=AsyncContextMock(mock_session))

        mgr = RubricManager(db=db, agent_id="test")
        result = await mgr.seed_v1()
        mock_session.add.assert_called_once()
        added_obj = mock_session.add.call_args[0][0]
        assert added_obj.version == "1.0.0"
        assert len(added_obj.dimensions) == 4


class TestRubricManagerVersioning:
    @pytest.mark.asyncio
    async def test_create_version_supersedes_active(self):
        from nous.cognitive.rubric import RubricManager
        from nous.storage.models import RubricVersion

        db = MagicMock()
        mock_session = AsyncMock()
        active = MagicMock(spec=RubricVersion)
        active.id = uuid.uuid4()
        active.version = "1.0.0"
        active.status = "active"
        active.dimensions = _default_dimensions()
        mock_result = AsyncMock()
        mock_result.scalar_one_or_none = MagicMock(return_value=active)
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.add = MagicMock()
        mock_session.flush = AsyncMock()
        mock_session.commit = AsyncMock()
        db.session = MagicMock(return_value=AsyncContextMock(mock_session))

        mgr = RubricManager(db=db, agent_id="test")
        new_dims = _default_dimensions()
        new_dims[0]["weight"] = 0.30
        new_dims[3]["weight"] = 0.20
        await mgr.create_version(
            new_version="1.1.0",
            dimensions=new_dims,
            change_reason="Weight adjustment based on correlation",
        )
        assert active.status == "superseded"
        mock_session.add.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_version_rejects_bad_dimension_count(self):
        from nous.cognitive.rubric import RubricManager

        db = MagicMock()
        mgr = RubricManager(db=db, agent_id="test")
        with pytest.raises(ValueError, match="Dimension count"):
            await mgr.create_version(
                new_version="2.0.0",
                dimensions=[{"name": "A", "weight": 0.5}, {"name": "B", "weight": 0.5}],
                change_reason="Too few",
            )

    @pytest.mark.asyncio
    async def test_create_version_rejects_bad_weight_sum(self):
        from nous.cognitive.rubric import RubricManager

        db = MagicMock()
        mgr = RubricManager(db=db, agent_id="test")
        dims = _default_dimensions()
        dims[0]["weight"] = 0.50  # Sum will be 0.75 + 0.50 = too high
        with pytest.raises(ValueError, match="Weights sum"):
            await mgr.create_version(
                new_version="2.0.0",
                dimensions=dims,
                change_reason="Bad weights",
            )


class TestRubricManagerToDetail:
    def test_to_detail_converts_orm_to_pydantic(self):
        from nous.cognitive.rubric import RubricManager

        db = MagicMock()
        mgr = RubricManager(db=db, agent_id="test")

        mock_rv = MagicMock()
        mock_rv.id = uuid.uuid4()
        mock_rv.agent_id = "test"
        mock_rv.version = "1.0.0"
        mock_rv.parent_version = None
        mock_rv.change_reason = "Initial"
        mock_rv.dimensions = _default_dimensions()
        mock_rv.outcome_correlations = {}
        mock_rv.status = "active"
        mock_rv.created_at = datetime.now(UTC)

        detail = mgr.to_detail(mock_rv)
        assert isinstance(detail, RubricVersionDetail)
        assert detail.version == "1.0.0"
        assert len(detail.dimensions) == 4
        assert isinstance(detail.dimensions[0], RubricDimension)
